"""Verified, compressed, size-capped SQLite backups for JSE.

Every startup takes a backup. Uncompressed, a ~490 MB database times twelve
retained copies plus one-off snapshots grew the Backups folder past 10 GB, so:

- each backup is verified (PRAGMA integrity_check) and then gzipped, which
  suits a database that is mostly advert text;
- retention is tiered: the newest `retain` backups, plus the newest backup of
  each of the previous `weekly` ISO weeks, so twelve restarts in two days no
  longer push out everything older;
- the whole folder is held under `max_total_mb`. Over budget, the oldest
  JSE-managed backups go first. The newest startup backup is never removed,
  and files JSE did not create are never touched; they are reported instead.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


BACKUP_PREFIX = "startup_job_applications_"
SAFETY_PREFIX = "pre_restore_job_applications_"
COMPRESSED_SUFFIX = ".db.gz"
DEFAULT_RETAIN = 3
DEFAULT_WEEKLY = 4
DEFAULT_MAX_TOTAL_MB = 1024
_CHUNK = 4 * 1024 * 1024


def _integrity(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"Backup integrity check failed: {result}")


def compress_file(source: Path, destination: Path) -> Path:
    """gzip `source` to `destination` atomically, then re-read it to prove the CRC."""
    partial = destination.with_name(destination.name + ".partial")
    try:
        with open(source, "rb") as raw, gzip.open(partial, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed, _CHUNK)
        with gzip.open(partial, "rb") as check:
            while check.read(_CHUNK):
                pass
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return destination


def decompress_file(source: Path, destination: Path) -> Path:
    with gzip.open(source, "rb") as packed, open(destination, "wb") as raw:
        shutil.copyfileobj(packed, raw, _CHUNK)
    return destination


def snapshot_database(source: Path, destination: Path) -> Path:
    """Consistent online copy of a live WAL database, verified, then gzipped."""
    plain = destination.with_name(destination.name.removesuffix(".gz") + ".partial")
    try:
        source_db = sqlite3.connect(str(source), timeout=30)
        backup_db = sqlite3.connect(str(plain), timeout=30)
        try:
            source_db.backup(backup_db, pages=2048, sleep=0.05)
        finally:
            backup_db.close()
            source_db.close()
        _integrity(plain)
        compress_file(plain, destination)
    finally:
        plain.unlink(missing_ok=True)
    return destination


def _stamp_of(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime)


def _managed(path: Path) -> bool:
    return path.is_file() and path.name.startswith((BACKUP_PREFIX, SAFETY_PREFIX)) and not path.name.endswith(".partial")


def apply_retention(backup_dir: Path, retain: int = DEFAULT_RETAIN, weekly: int = DEFAULT_WEEKLY,
                    max_total_mb: float = DEFAULT_MAX_TOTAL_MB) -> dict:
    """Tiered retention for startup backups, then the folder-wide size budget."""
    removed = []
    startups = sorted(
        (p for p in backup_dir.glob(f"{BACKUP_PREFIX}*") if _managed(p)),
        key=_stamp_of, reverse=True,
    )
    keep = set(startups[:max(1, retain)])
    # Weeks already covered by the newest backups do not need a weekly copy too.
    covered = {_stamp_of(path).isocalendar()[:2] for path in keep}
    weeks_seen = set()
    for path in startups[max(1, retain):]:
        week = _stamp_of(path).isocalendar()[:2]
        if week not in covered and week not in weeks_seen and len(weeks_seen) < weekly:
            weeks_seen.add(week)
            keep.add(path)
    for path in startups:
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(path.name)

    budget = max_total_mb * 1024 * 1024
    files = [p for p in backup_dir.iterdir() if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    newest = startups[0] if startups else None
    candidates = sorted((p for p in files if _managed(p) and p != newest and p.exists()), key=_stamp_of)
    for path in candidates:
        if total <= budget:
            break
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        removed.append(path.name)
    unmanaged = [p.name for p in backup_dir.iterdir() if p.is_file() and not _managed(p)]
    return {
        "removed": removed,
        "total_mb": round(total / 1048576, 1),
        "over_budget": total > budget,
        "unmanaged_files": unmanaged,
    }


def create_backup(source: Path, backup_dir: Path, retain: int = DEFAULT_RETAIN,
                  weekly: int = DEFAULT_WEEKLY, max_total_mb: float = DEFAULT_MAX_TOTAL_MB) -> Path:
    source = source.resolve()
    backup_dir = backup_dir.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    final_path = snapshot_database(source, backup_dir / f"{BACKUP_PREFIX}{stamp}{COMPRESSED_SUFFIX}")
    apply_retention(backup_dir, retain, weekly, max_total_mb)
    return final_path


def recompress_legacy_backups(backup_dir: Path) -> list:
    """Convert uncompressed startup/pre-restore .db backups to verified .db.gz.

    Each original is removed only after its integrity check passed and the
    compressed copy read back cleanly. Stray -wal/-shm sidecars of a backup
    copy belong to no live database and go with it.
    """
    converted = []
    for path in sorted(backup_dir.glob("*.db")):
        if not _managed(path):
            continue
        _integrity(path)
        target = path.with_name(path.name + ".gz")
        compress_file(path, target)
        mtime = path.stat().st_mtime
        os.utime(target, (mtime, mtime))
        path.unlink()
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        converted.append(target.name)
    return converted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("backup_dir", type=Path)
    parser.add_argument("--retain", type=int, default=DEFAULT_RETAIN)
    parser.add_argument("--weekly", type=int, default=DEFAULT_WEEKLY)
    parser.add_argument("--max-total-mb", type=float, default=DEFAULT_MAX_TOTAL_MB)
    parser.add_argument("--recompress-legacy", action="store_true")
    args = parser.parse_args()
    if args.recompress_legacy:
        print(json.dumps({"converted": recompress_legacy_backups(args.backup_dir.resolve())}))
        return 0
    created = create_backup(args.source, args.backup_dir, args.retain, args.weekly, args.max_total_mb)
    print(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

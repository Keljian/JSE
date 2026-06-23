"""Validate and restore a JSE SQLite database with a pre-restore backup."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path


def _validate(path: Path) -> dict:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity}")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"jobs", "profiles"}.issubset(tables):
            raise RuntimeError("The selected file is not a JSE database backup.")
        return {
            "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "profiles": conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0],
        }
    finally:
        conn.close()


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_db = sqlite3.connect(str(source), timeout=30)
    destination_db = sqlite3.connect(str(destination), timeout=30)
    try:
        source_db.backup(destination_db, pages=2048, sleep=0.05)
    finally:
        destination_db.close()
        source_db.close()


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


def restore_database(source: Path, target: Path, backup_dir: Path) -> dict:
    source, target, backup_dir = source.resolve(), target.resolve(), backup_dir.resolve()
    if source == target:
        raise ValueError("Choose a backup file, not the active database.")
    source_summary = _validate(source)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safety_backup = backup_dir / f"pre_restore_job_applications_{stamp}.db"
    partial = target.with_suffix(target.suffix + ".restore-partial")
    if target.exists():
        _sqlite_backup(target, safety_backup)
        _validate(safety_backup)
    try:
        partial.unlink(missing_ok=True)
        _sqlite_backup(source, partial)
        restored_summary = _validate(partial)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        _replace_with_retry(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return {
        **restored_summary,
        "source": str(source),
        "safety_backup": str(safety_backup) if safety_backup.exists() else None,
        "source_jobs": source_summary["jobs"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("backup_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(restore_database(args.source, args.target, args.backup_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

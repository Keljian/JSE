"""Create a verified rolling SQLite backup for JSE startup."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


BACKUP_PREFIX = "startup_job_applications_"


def create_backup(source: Path, backup_dir: Path, retain: int = 12) -> Path:
    source = source.resolve()
    backup_dir = backup_dir.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    final_path = backup_dir / f"{BACKUP_PREFIX}{stamp}.db"
    partial_path = final_path.with_suffix(".db.partial")
    try:
        source_db = sqlite3.connect(str(source), timeout=30)
        backup_db = sqlite3.connect(str(partial_path), timeout=30)
        try:
            source_db.backup(backup_db, pages=2048, sleep=0.05)
            integrity = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
        finally:
            backup_db.close()
            source_db.close()
        partial_path.replace(final_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    automatic = sorted(
        backup_dir.glob(f"{BACKUP_PREFIX}*.db"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for expired in automatic[max(1, retain):]:
        expired.unlink(missing_ok=True)
    return final_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("backup_dir", type=Path)
    parser.add_argument("--retain", type=int, default=12)
    args = parser.parse_args()
    created = create_backup(args.source, args.backup_dir, args.retain)
    print(created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

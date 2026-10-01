"""Tests for startup backup compression, retention, the size budget and recovery."""
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from tools.backup_database import (
    apply_retention,
    create_backup,
    decompress_file,
    recompress_legacy_backups,
)
from tools.restore_database import restore_database


def _database(path, jobs):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE profiles (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, profile_id INTEGER)")
        conn.execute("INSERT INTO profiles VALUES (1, 'Lane')")
        conn.executemany("INSERT INTO jobs VALUES (?, ?, 1)", [(index, f"Job {index}") for index in range(1, jobs + 1)])
        conn.commit()
    finally:
        conn.close()


def _count_jobs(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()


def _age(path, days):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def _at(path, when):
    stamp = datetime.fromisoformat(when).timestamp()
    os.utime(path, (stamp, stamp))


class DatabaseBackupTests(unittest.TestCase):
    def test_startup_backups_are_compressed_verified_and_rotated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, backups = root / "job_applications.db", root / "Backups"
            _database(source, 3)
            for _ in range(3):
                newest = create_backup(source, backups, retain=2, weekly=0)
            self.assertTrue(newest.name.endswith(".db.gz"))
            self.assertEqual(2, len(list(backups.glob("startup_job_applications_*.db.gz"))))
            self.assertEqual([], list(backups.glob("*.partial")))
            plain = decompress_file(newest, root / "check.db")
            self.assertEqual(3, _count_jobs(plain))

    def test_weekly_tier_keeps_one_backup_per_older_week(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, backups = root / "job_applications.db", root / "Backups"
            _database(source, 1)
            made = [create_backup(source, backups, retain=50, weekly=0) for _ in range(5)]
            # Two in the newest slot, then two on one Wednesday and one a fortnight earlier.
            for path, when in zip(made[2:], ("2026-09-09T13:00", "2026-09-09T12:00", "2026-08-26T12:00")):
                _at(path, when)
            apply_retention(backups, retain=2, weekly=4)
            self.assertEqual(4, len(list(backups.glob("startup_*.db.gz"))))

    def test_size_budget_drops_oldest_managed_files_but_never_the_newest_or_unknown_files(self):
        with tempfile.TemporaryDirectory() as folder:
            backups = Path(folder)
            old = backups / "pre_restore_job_applications_1.db.gz"
            mid = backups / "startup_job_applications_1.db.gz"
            new = backups / "startup_job_applications_2.db.gz"
            mine = backups / "my_archive.7z"
            for path, days in ((old, 30), (mid, 10), (new, 0), (mine, 60)):
                path.write_bytes(b"x" * 400_000)
                _age(path, days)
            result = apply_retention(backups, retain=5, weekly=0, max_total_mb=1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(mine.exists())
            self.assertIn("my_archive.7z", result["unmanaged_files"])
            # Still over budget after removing everything it may remove: says so.
            apply_retention(backups, retain=5, weekly=0, max_total_mb=0.1)
            self.assertTrue(new.exists())

    def test_legacy_uncompressed_backups_are_converted_after_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            backups = Path(folder)
            legacy = backups / "startup_job_applications_20260901.db"
            _database(legacy, 4)
            (backups / "startup_job_applications_20260901.db-wal").write_bytes(b"")
            converted = recompress_legacy_backups(backups)
            self.assertEqual(["startup_job_applications_20260901.db.gz"], converted)
            self.assertFalse(legacy.exists())
            self.assertFalse((backups / "startup_job_applications_20260901.db-wal").exists())
            self.assertEqual(4, _count_jobs(decompress_file(backups / converted[0], backups / "check.db")))

    def test_restore_from_compressed_backup_keeps_a_compressed_safety_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target, source, backups = root / "job_applications.db", root / "source.db", root / "Backups"
            _database(target, 2)
            _database(source, 5)
            packed = create_backup(source, backups, retain=5)
            result = restore_database(packed, target, backups)
            self.assertEqual(5, result["jobs"])
            self.assertEqual(5, _count_jobs(target))
            safety = Path(result["safety_backup"])
            self.assertTrue(safety.name.endswith(".db.gz"))
            self.assertEqual(2, _count_jobs(decompress_file(safety, root / "safety.db")))
            self.assertEqual([], list(root.glob("*.restore-source")))

    def test_restore_still_accepts_an_uncompressed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target, selected, backups = root / "job_applications.db", root / "selected.db", root / "Backups"
            _database(target, 2)
            _database(selected, 5)
            result = restore_database(selected, target, backups)
            self.assertEqual(5, result["jobs"])
            self.assertEqual(5, _count_jobs(target))


if __name__ == "__main__":
    unittest.main()

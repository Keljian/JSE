"""Tests for startup backup rotation and database recovery."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.backup_database import create_backup
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


class DatabaseBackupTests(unittest.TestCase):
    def test_startup_backups_are_verified_and_rotated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, backups = root / "job_applications.db", root / "Backups"
            _database(source, 3)
            create_backup(source, backups, retain=2)
            create_backup(source, backups, retain=2)
            newest = create_backup(source, backups, retain=2)
            self.assertEqual(2, len(list(backups.glob("startup_job_applications_*.db"))))
            conn = sqlite3.connect(newest)
            try:
                self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                conn.close()

    def test_restore_preserves_current_database_before_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target, selected, backups = root / "job_applications.db", root / "selected.db", root / "Backups"
            _database(target, 2)
            _database(selected, 5)
            result = restore_database(selected, target, backups)
            self.assertEqual(5, result["jobs"])
            self.assertTrue(Path(result["safety_backup"]).exists())
            conn = sqlite3.connect(target)
            try:
                self.assertEqual(5, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            finally:
                conn.close()
            conn = sqlite3.connect(result["safety_backup"])
            try:
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

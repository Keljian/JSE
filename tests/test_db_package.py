"""Invariants of the db/ package split and the database_manager facade.

The split moved 407 definitions out of a 9,191-line module. Three things must
stay true or the consequences are severe and silent:

1. Every name callers used is still reachable through `database_manager`.
2. `DB_FILE` resolves to exactly one binding. The test suite repoints it at a
   throwaway database; a second, stale binding would mean tests write to the
   real one while appearing to pass. See tests/conftest.py for the incident.
3. `APP_ROOT` still points at the application root, not at db/. Getting it
   wrong moves the whole data directory.
"""
import ast
import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database_manager as db  # noqa: E402
import db as db_package  # noqa: E402

LAYERS = [
    "connection", "constants", "text", "companies", "settings", "scrapers",
    "lanes", "outcomes", "jobs", "campaign", "intel", "dashboard",
]


def _module_names(path):
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


class FacadeSurfaceTests(unittest.TestCase):
    def test_every_name_in_the_package_is_reachable_on_the_facade(self):
        unreachable = []
        for module in LAYERS:
            for name in sorted(_module_names(ROOT / "db" / f"{module}.py")):
                if not hasattr(db, name):
                    unreachable.append(f"{module}.{name}")
        self.assertEqual(unreachable, [], "names lost in the split")

    def test_no_name_is_defined_in_two_modules(self):
        seen = {}
        duplicates = []
        for module in LAYERS:
            for name in _module_names(ROOT / "db" / f"{module}.py"):
                if name in seen:
                    duplicates.append(f"{name}: {seen[name]} and {module}")
                seen[name] = module
        self.assertEqual(duplicates, [], "a duplicated definition makes the facade order-dependent")

    def test_the_facade_carries_no_implementation(self):
        # If logic creeps back into the facade the split starts to unwind.
        body = ast.parse((ROOT / "database_manager.py").read_text(encoding="utf-8")).body
        functions = [n.name for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertEqual(functions, [], "database_manager.py should re-export, not implement")


class MutableStateProxyTests(unittest.TestCase):
    """The dangerous part: DB_FILE must have exactly one binding."""

    def setUp(self):
        self.original_db_file = db.DB_FILE
        self.original_wal = db._wal_enabled

    def tearDown(self):
        db.DB_FILE = self.original_db_file
        db._wal_enabled = self.original_wal
        self._release_keepalive()

    @staticmethod
    def _release_keepalive():
        """Drop the pinned idle connection.

        db.connection holds one connection open for the process lifetime to keep
        the WAL index alive. It holds no lock, but on Windows it does hold a file
        handle, so a test that repoints DB_FILE has to release it before the old
        file can be deleted.
        """
        conn = db_package.connection._keepalive_conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            db_package.connection._keepalive_conn = None

    def test_writing_through_the_facade_moves_the_canonical_binding(self):
        db.DB_FILE = "/tmp/jse-proxy-check.db"
        self.assertEqual(db_package.connection.DB_FILE, "/tmp/jse-proxy-check.db")
        self.assertEqual(db.DB_FILE, "/tmp/jse-proxy-check.db")

    def test_writing_the_canonical_binding_is_visible_through_the_facade(self):
        db_package.connection.DB_FILE = "/tmp/jse-proxy-reverse.db"
        self.assertEqual(db.DB_FILE, "/tmp/jse-proxy-reverse.db")

    def test_the_connection_helper_follows_a_repointed_db_file(self):
        # The actual guarantee the test suite depends on: repointing DB_FILE
        # through the facade changes where connections are opened.
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="jse_proxy_probe_")
        try:
            target = str(Path(tmp) / "probe.db")
            self._release_keepalive()
            db.DB_FILE = target
            with db.get_db_connection() as conn:
                conn.execute("CREATE TABLE probe (id INTEGER)")
                conn.commit()
            self.assertTrue(Path(target).exists(),
                            "get_db_connection ignored a repointed DB_FILE")
        finally:
            self._release_keepalive()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_wal_flag_is_proxied_too(self):
        db._wal_enabled = True
        self.assertIs(db_package.connection._wal_enabled, True)
        db._wal_enabled = False
        self.assertIs(db_package.connection._wal_enabled, False)

    def test_forwarded_names_are_not_shadowed_on_the_facade(self):
        # A plain re-export would create the stale second binding this proxy
        # exists to prevent. The facade raises at import time if one appears;
        # assert the absence directly as well.
        source = (ROOT / "database_manager.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                imported |= {alias.asname or alias.name for alias in node.names}
        for name in ("DB_FILE", "DATA_DIR", "_wal_enabled"):
            self.assertNotIn(name, imported, f"{name} must stay proxied, not re-exported")

    def test_unknown_attributes_still_raise(self):
        with self.assertRaises(AttributeError):
            db.definitely_not_a_real_attribute


class LayeringTests(unittest.TestCase):
    def test_modules_only_import_from_earlier_layers_at_module_scope(self):
        index = {name: i for i, name in enumerate(LAYERS)}
        violations = []
        for module in LAYERS:
            tree = ast.parse((ROOT / "db" / f"{module}.py").read_text(encoding="utf-8"))
            for node in tree.body:  # module scope only
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in index:
                    if index[node.module] >= index[module]:
                        violations.append(f"{module} imports {node.module} at module scope")
        self.assertEqual(violations, [],
                         "a same-or-later layer import at module scope is an import cycle")

    def test_cycle_crossings_are_function_local_and_explained(self):
        # Where the domain is genuinely cyclic the import sits inside the
        # function. Each one must carry the comment explaining why.
        for module in LAYERS:
            source = (ROOT / "db" / f"{module}.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for stmt in node.body:
                    if isinstance(stmt, ast.ImportFrom) and stmt.level == 1:
                        window = "\n".join(source.splitlines()[stmt.lineno - 1:stmt.lineno + 4])
                        self.assertIn(
                            "module that imports this one back", window,
                            f"unexplained function-local import in {module}.{node.name}",
                        )

    def test_package_imports_cleanly_from_scratch(self):
        for name in [f"db.{m}" for m in LAYERS] + ["db", "database_manager"]:
            self.assertIsNotNone(importlib.import_module(name))


class PathTests(unittest.TestCase):
    def test_app_root_is_the_repository_root_not_the_package(self):
        self.assertEqual(Path(db.APP_ROOT).name, ROOT.name)
        self.assertTrue((Path(db.APP_ROOT) / "python_bridge.py").exists(),
                        "APP_ROOT no longer points at the application root")

    def test_no_module_in_the_package_derives_paths_from_its_own_location(self):
        # Only connection.py may use __file__, and it must compensate for the
        # extra directory level.
        for module in LAYERS:
            source = (ROOT / "db" / f"{module}.py").read_text(encoding="utf-8")
            if "__file__" in source:
                self.assertEqual(module, "connection")
                self.assertIn("parents[1]", source)


if __name__ == "__main__":
    unittest.main()

"""Guards on what the installer is allowed to contain.

The bug this prevents: the electron-builder `files` list packaged
`scraper_plugins/**/*` and `search_terms.json` from the repo root, but both are
gitignored personal runtime data. A build on a developer machine therefore
embedded personal search terms and local council/university scrapers, while a
CI build from a clean checkout shipped neither — the same commit produced two
different applications depending on who built it. That is invisible in a diff,
so it is asserted here instead.
"""
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _gitignore_entries():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


class PackagingManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        cls.files = cls.package["build"]["files"]

    def test_personal_runtime_paths_are_not_packaged(self):
        forbidden = ("scraper_plugins", "search_terms.json", "settings", "Resumes",
                     "applications", "Backups", "job_applications.db")
        for pattern in self.files:
            for name in forbidden:
                self.assertFalse(
                    pattern.startswith(name),
                    f"electron-builder packages personal runtime data: {pattern!r}",
                )

    def test_neutral_defaults_are_packaged(self):
        self.assertTrue(
            any(pattern.startswith("defaults") for pattern in self.files),
            "defaults/ must be packaged — it is the first-run seed for search terms and plugins",
        )

    def test_defaults_directory_exists_and_is_tracked(self):
        defaults = ROOT / "defaults"
        self.assertTrue(defaults.is_dir())
        self.assertTrue((defaults / "search_terms.json").exists())
        # A leading slash in .gitignore keeps the personal root copies ignored
        # without also ignoring defaults/scraper_plugins.
        entries = _gitignore_entries()
        self.assertIn("/scraper_plugins/", entries)
        self.assertIn("/search_terms.json", entries)
        self.assertNotIn("scraper_plugins/", entries)
        self.assertNotIn("search_terms.json", entries)

    def test_default_search_terms_are_neutral_and_valid(self):
        terms = json.loads((ROOT / "defaults" / "search_terms.json").read_text(encoding="utf-8"))
        self.assertIsInstance(terms, list)
        self.assertTrue(terms)
        self.assertTrue(all(isinstance(term, str) and term.strip() for term in terms))

    def test_first_run_seeds_from_defaults(self):
        main_cjs = (ROOT / "electron" / "main.cjs").read_text(encoding="utf-8")
        self.assertIn('"defaults"', main_cjs,
                      "prepareWritableWorkspace must seed first-run content from defaults/")

    def test_requirements_lock_pins_every_package(self):
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        pins = [line.strip() for line in lock.splitlines()
                if line.strip() and not line.startswith("#")]
        self.assertTrue(pins, "requirements.lock is empty")
        for pin in pins:
            self.assertIn("==", pin, f"unpinned entry in requirements.lock: {pin!r}")

    def test_lock_covers_every_direct_requirement(self):
        locked = {
            line.split("==")[0].strip().lower().replace("_", "-")
            for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
            if "==" in line and not line.startswith("#")
        }
        for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            name = line.split(">=")[0].split("==")[0].strip().lower().replace("_", "-")
            self.assertIn(name, locked, f"{name} is in requirements.txt but not locked")

    def test_ci_gates_the_build_on_tests(self):
        workflow = (ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertIn("pytest", workflow, "CI must run the test suite")
        self.assertIn("ruff", workflow, "CI must lint Python")
        self.assertIn("npm run lint", workflow, "CI must lint the renderer")
        # Every platform build has to depend on the gate, or it is not a gate.
        self.assertEqual(
            workflow.count("      - test\n"), 3,
            "all three platform builds must depend on the test job",
        )


if __name__ == "__main__":
    unittest.main()

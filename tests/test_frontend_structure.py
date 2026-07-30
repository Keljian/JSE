"""Structural invariants of the split renderer.

There are no frontend unit tests, so these are deliberately structural rather
than behavioural: they assert the shape the split established, which is what
would quietly erode. `npm run lint` and `npm run build` cover the rest.
"""
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "src"
LIB = SRC / "lib"
COMPONENTS = SRC / "components"


def _sources():
    return sorted(list(LIB.glob("*.jsx")) + list(COMPONENTS.glob("*.jsx")) + [SRC / "main.jsx"])


class CompositionRootTests(unittest.TestCase):
    def test_main_is_a_composition_root_not_a_component_library(self):
        source = (SRC / "main.jsx").read_text(encoding="utf-8")
        # Only App itself should be declared here now.
        declared = re.findall(r"^function ([A-Z]\w+)", source, re.M)
        self.assertEqual(declared, ["App"],
                         f"main.jsx should declare only App, found {declared}")

    def test_main_is_no_longer_a_monolith(self):
        lines = len((SRC / "main.jsx").read_text(encoding="utf-8").splitlines())
        self.assertLess(lines, 2500, "main.jsx is growing back toward a monolith")

    def test_component_modules_exist_and_are_navigable(self):
        self.assertTrue(LIB.is_dir())
        self.assertTrue(COMPONENTS.is_dir())
        oversized = [
            path.name for path in _sources()
            if path.name != "main.jsx"
            and len(path.read_text(encoding="utf-8").splitlines()) > 1200
        ]
        self.assertEqual(oversized, [], "split modules are drifting back toward monoliths")

    def test_every_module_exports_something(self):
        for path in _sources():
            if path.name == "main.jsx":
                continue
            source = path.read_text(encoding="utf-8")
            self.assertTrue(
                re.search(r"^export \{", source, re.M),
                f"{path.name} exports nothing",
            )


class ErrorBoundaryTests(unittest.TestCase):
    def test_the_boundary_exists_and_is_a_class(self):
        source = (COMPONENTS / "ErrorBoundary.jsx").read_text(encoding="utf-8")
        # There is no hook equivalent of componentDidCatch.
        self.assertIn("class ErrorBoundary", source)
        self.assertIn("getDerivedStateFromError", source)
        self.assertIn("componentDidCatch", source)

    def test_the_app_is_mounted_inside_the_boundary(self):
        source = (SRC / "main.jsx").read_text(encoding="utf-8")
        self.assertIn("ErrorBoundary", source, "main.jsx does not import the boundary")
        # The createRoot argument contains its own parentheses, so match from
        # `.render(` to the end of the file rather than trying to balance them.
        mount = re.search(r"\.render\((.*)$", source, re.S)
        self.assertIsNotNone(mount, "could not find the render call")
        rendered = mount.group(1)
        self.assertIn("<ErrorBoundary>", rendered)
        self.assertIn("<App />", rendered)
        self.assertLess(rendered.index("<ErrorBoundary>"), rendered.index("<App />"),
                        "App must be mounted inside the boundary, not around it")

    def test_the_fallback_offers_a_way_out(self):
        # A desktop app has no address bar; the fallback has to provide recovery.
        source = (COMPONENTS / "ErrorBoundary.jsx").read_text(encoding="utf-8")
        self.assertIn("window.location.reload", source)

    def test_the_fallback_has_styles(self):
        css = (SRC / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".render-error", css)


class NoNativeDialogTests(unittest.TestCase):
    """window.confirm/alert/prompt freeze input in this Electron build."""

    def test_components_use_the_in_app_dialogs(self):
        offenders = []
        for path in _sources():
            source = path.read_text(encoding="utf-8")
            for call in ("window.confirm(", "window.alert(", "window.prompt("):
                if call in source and path.name != "dialogs.jsx":
                    offenders.append(f"{path.name}: {call}")
        self.assertEqual(offenders, [],
                         "use appConfirm/appNotice/appPrompt; native dialogs freeze input")


class LintConfigTests(unittest.TestCase):
    def test_lint_and_build_scripts_exist(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        for script in ("lint", "build"):
            self.assertIn(script, package["scripts"])

    def test_eslint_covers_the_new_directories(self):
        config = (ROOT / "eslint.config.mjs").read_text(encoding="utf-8")
        self.assertIn("src/**/*.{js,jsx}", config,
                      "the eslint glob must reach src/lib and src/components")


if __name__ == "__main__":
    unittest.main()

"""Invariants of the bridge/ package split.

python_bridge.py went from 3,509 lines to an entrypoint plus a merged dispatch
table. The command implementations moved into bridge/, grouped by prefix. Three
things must hold:

1. Every command the renderer calls is still dispatchable, with no duplicates.
2. The protocol stream stays a single binding. `serve()` pins stdout inside
   bridge.runtime; if it bound the name on python_bridge instead, emit() would
   keep writing to the redirected stdout — that is, stderr — and the worker
   would hang forever waiting for replies.
3. APP_ROOT still points at the application root, not at bridge/.
"""
import ast
import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import python_bridge  # noqa: E402
from bridge import runtime  # noqa: E402

COMMAND_MODULES = ["documents", "lanes", "jobs", "scrapers", "intel", "insights", "corpus", "settings"]


class DispatchTableTests(unittest.TestCase):
    def test_commands_are_merged_from_every_module(self):
        from importlib import import_module
        expected = {}
        for name in COMMAND_MODULES:
            expected.update(import_module(f"bridge.{name}").COMMANDS)
        self.assertEqual(set(python_bridge.COMMANDS), set(expected))
        self.assertGreater(len(python_bridge.COMMANDS), 100)

    def test_every_handler_is_callable(self):
        for name, handler in python_bridge.COMMANDS.items():
            self.assertTrue(callable(handler), f"{name} is not callable")

    def test_no_command_key_is_claimed_twice(self):
        from importlib import import_module
        seen = {}
        for name in COMMAND_MODULES:
            for key in import_module(f"bridge.{name}").COMMANDS:
                self.assertNotIn(key, seen, f"{key} declared in both {seen.get(key)} and {name}")
                seen[key] = name

    def test_a_handler_lives_in_the_module_that_declares_it(self):
        # Otherwise the grouping is decorative and the next command lands in an
        # arbitrary file.
        from importlib import import_module
        for name in COMMAND_MODULES:
            module = import_module(f"bridge.{name}")
            for key, handler in module.COMMANDS.items():
                self.assertEqual(
                    handler.__module__, f"bridge.{name}",
                    f"{key} is declared in bridge.{name} but defined in {handler.__module__}",
                )

    def test_the_entrypoint_holds_no_command_implementations(self):
        body = ast.parse((ROOT / "python_bridge.py").read_text(encoding="utf-8")).body
        commands = [n.name for n in body
                    if isinstance(n, ast.FunctionDef) and n.name.startswith("command_")]
        self.assertEqual(commands, [], "command implementations belong in bridge/")

    def test_the_entrypoint_is_still_runnable_as_a_script(self):
        # Electron spawns this path directly; losing the guard breaks the app
        # without breaking any import.
        source = (ROOT / "python_bridge.py").read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', source)
        self.assertIn('sys.argv[1] == "--serve"', source)


class ProtocolStreamTests(unittest.TestCase):
    def tearDown(self):
        runtime.use_protocol_stream(None)
        runtime.set_request_id(None)

    def test_emit_writes_to_the_pinned_stream(self):
        buffer = io.StringIO()
        runtime.use_protocol_stream(buffer)
        runtime.emit("log", message="hello")
        self.assertIn('"type": "log"', buffer.getvalue())
        self.assertIn("hello", buffer.getvalue())

    def test_pinning_from_the_entrypoint_reaches_the_binding_emit_reads(self):
        # The failure this guards: assigning _OUTPUT_STREAM on python_bridge
        # would leave runtime's binding at None, emit() would fall back to
        # sys.stdout (redirected to stderr in worker mode), and Electron would
        # never see a reply.
        buffer = io.StringIO()
        runtime.use_protocol_stream(buffer)
        self.assertIs(runtime._OUTPUT_STREAM, buffer)
        runtime.emit("result", data={"ok": True})
        self.assertIn('"ok": true', buffer.getvalue())

    def test_request_id_is_attached_to_frames(self):
        buffer = io.StringIO()
        runtime.use_protocol_stream(buffer)
        runtime.set_request_id("req-42")
        runtime.emit("result", data={})
        self.assertIn('"id": "req-42"', buffer.getvalue())

    def test_frames_are_one_json_object_per_line(self):
        buffer = io.StringIO()
        runtime.use_protocol_stream(buffer)
        runtime.emit("log", message="first")
        runtime.emit("log", message="second")
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        import json
        for line in lines:
            json.loads(line)


class PathTests(unittest.TestCase):
    def test_app_root_is_the_repository_root_not_the_package(self):
        self.assertEqual(Path(runtime.APP_ROOT).resolve(), ROOT.resolve())
        self.assertTrue((Path(runtime.APP_ROOT) / "python_bridge.py").exists())

    def test_only_runtime_derives_paths_from_its_own_location(self):
        for name in ["runtime"] + COMMAND_MODULES:
            source = (ROOT / "bridge" / f"{name}.py").read_text(encoding="utf-8")
            if "__file__" in source:
                self.assertEqual(name, "runtime")
                self.assertIn("parents[1]", source)


if __name__ == "__main__":
    unittest.main()

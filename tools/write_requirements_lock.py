"""Regenerate requirements.lock from a pip resolution report.

requirements.txt states only lower bounds, so a transitive release can change
what an installer ships with no diff anywhere in the repo. The lock pins the
whole tree; this script is how it gets refreshed.

Usage:
    python -m pip install --dry-run --ignore-installed --report resolve.json -r requirements.txt
    python tools/write_requirements_lock.py resolve.json
"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HEADER = """# Fully pinned dependency set, including transitives.
#
# requirements.txt states only lower bounds, and the CI python-runtime cache is
# keyed on its hash — so a transitive release could change what an installer
# shipped with no diff anywhere in the repo. This file is what the runtime-prep
# scripts install; requirements.txt stays as the human-readable statement of
# intent.
#
# Regenerate after changing requirements.txt:
#   python -m pip install --dry-run --ignore-installed --report resolve.json -r requirements.txt
#   python tools/write_requirements_lock.py resolve.json
#
# Resolved {resolved_on} for CPython 3.11 on Windows/macOS/Linux x64.

# --- Direct dependencies (mirrors requirements.txt) ---
"""


def _normalise(name):
    return name.lower().replace("_", "-")


def direct_requirement_names(requirements_path):
    names = set()
    for raw in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        for separator in (">=", "==", "<=", "~=", ">", "<", "!=", "["):
            if separator in line:
                line = line.split(separator, 1)[0]
        names.add(_normalise(line.strip()))
    return names


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    direct = direct_requirement_names(ROOT / "requirements.txt")

    pins = sorted(
        ((item["metadata"]["name"], item["metadata"]["version"]) for item in report["install"]),
        key=lambda row: row[0].lower(),
    )
    if not pins:
        print("Resolution report contained no packages; refusing to write an empty lock.", file=sys.stderr)
        return 1

    direct_pins = [f"{name}=={version}" for name, version in pins if _normalise(name) in direct]
    transitive_pins = [f"{name}=={version}" for name, version in pins if _normalise(name) not in direct]

    body = (
        HEADER.format(resolved_on=date.today().isoformat())
        + "\n".join(direct_pins)
        + "\n\n# --- Transitive dependencies ---\n"
        + "\n".join(transitive_pins)
        + "\n"
    )
    (ROOT / "requirements.lock").write_text(body, encoding="utf-8")
    print(f"Wrote requirements.lock with {len(pins)} pins ({len(direct_pins)} direct).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

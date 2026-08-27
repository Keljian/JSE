"""Check the nightly run can actually do its work, before it spends four hours finding out.

On 27 August the analysis pass failed silently for days because JSE was configured
with a model name the endpoint does not serve, so every scoring call 404'd and the
packet came out 14% scored while looking otherwise healthy. That is exactly the
failure this catches: it costs two seconds and turns a wasted night into a line in
nightly_status.json.

Exit code 0 = ready. 1 = analysis will not work. Prints JSON either way.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, r"C:\JSE")

result = {"ok": True, "checks": [], "problems": []}


def check(name, ok, detail=""):
    result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        result["ok"] = False
        result["problems"].append(f"{name}: {detail}")


try:
    import database_manager as db
except Exception as exc:  # pragma: no cover
    print(json.dumps({"ok": False, "problems": [f"cannot import JSE: {exc}"]}))
    sys.exit(1)

db_path = Path(r"C:\JSE\settings\job_applications.db")
check("database present", db_path.exists(), str(db_path))

provider = (db.get_app_setting("scoring_ai_provider") or "local").lower()
result["scoring_provider"] = provider

if provider == "local":
    base = (db.get_app_setting("local_base_url") or "").rstrip("/")
    key = db.get_app_setting("local_api_key") or ""
    model = db.get_app_setting("local_model") or ""
    result["local_model"] = model
    result["local_base_url"] = base

    served = []
    try:
        request = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            served = [m.get("id") for m in json.load(response).get("data", [])]
        check("local endpoint reachable", True, f"{len(served)} models served")
    except urllib.error.HTTPError as exc:
        check("local endpoint reachable", False, f"HTTP {exc.code} from {base}/models")
    except Exception as exc:
        check(
            "local endpoint reachable",
            False,
            f"{base} did not answer ({exc}). Is Unsloth Studio running?",
        )

    if served:
        result["models_served"] = served
        if not model:
            check("model configured", False, "no local_model set in JSE settings")
        elif model in served:
            check("model served", True, model)
        else:
            check(
                "model served",
                False,
                f"JSE is configured for {model!r}, which this endpoint does not serve. "
                f"Available: {', '.join(served[:6])}",
            )
else:
    check(f"provider {provider}", True, "not the local endpoint, skipping model check")

print(json.dumps(result, indent=2))
sys.exit(0 if result["ok"] else 1)

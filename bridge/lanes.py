"""Lane/profile commands: settings, resumes, search terms, candidate memory.

Split out of python_bridge.py, which re-exports everything here.
"""
import contextlib
import re
import sys
from pathlib import Path

import database_manager as db
from .runtime import (
    _clean_text,
    _json_loads_maybe,
    copy_into_workspace,
    emit,
    import_app_logic,
    row_to_dict,
)
from .documents import (
    read_resume_text,
)

def _memory_fragment_to_dict(row):
    data = row_to_dict(row)
    data["skills"] = _json_loads_maybe(data.pop("skills_json", None), [])
    data["domains"] = _json_loads_maybe(data.pop("domains_json", None), [])
    data["source_job_ids"] = _json_loads_maybe(data.pop("source_job_ids_json", None), [])
    data["source_doc_paths"] = _json_loads_maybe(data.pop("source_doc_paths_json", None), [])
    return data


def _search_term_candidate(value):
    text = _clean_text(value)
    if not text:
        return ""
    text = re.split(r"\s[-|]\s", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    text = re.sub(
        r"\b(?:contract|temporary|temp|permanent|full[- ]?time|part[- ]?time|remote|hybrid|melbourne|sydney|brisbane|vic|nsw|qld)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = _clean_text(text.strip(" -|,/"))
    if len(text) < 4 or len(text.split()) > 6:
        return ""
    return text


def _compute_evolved_profile_terms(existing_terms, sources, fragments, max_terms=12):
    candidates = []
    candidates.extend(existing_terms)
    candidates.extend(_search_term_candidate(source.get("title")) for source in sources or [])

    theme_terms = {
        "Automation": "Automation Project Manager",
        "Cloud": "Cloud Infrastructure Manager",
        "Cost Optimisation": "IT Operations Manager",
        "Cybersecurity": "Cyber Security Manager",
        "Digital Transformation": "Digital Transformation Manager",
        "Incident Response": "IT Operations Manager",
        "IT Strategy": "IT Strategy Manager",
        "Service Delivery": "Service Delivery Manager",
        "Systems Integration": "Systems Integration Manager",
        "Team Leadership": "IT Manager",
        "Vendor Management": "IT Vendor Manager",
    }
    for fragment in fragments or []:
        mapped = theme_terms.get(str(fragment.get("theme") or "").strip())
        if mapped:
            candidates.append(mapped)

    evolved = []
    seen = set()
    for candidate in candidates:
        clean = _search_term_candidate(candidate)
        key = clean.casefold()
        if clean and key not in seen:
            evolved.append(clean)
            seen.add(key)
        if len(evolved) >= max_terms:
            break

    return evolved


def _evolve_profile_terms_from_memory(profile_id, sources, fragments, max_terms=12):
    evolved = _compute_evolved_profile_terms(db.get_lane_terms(profile_id), sources, fragments, max_terms)
    # Use the merge-aware writer so manual / interview-validated entries are
    # preserved. save_lane_terms (which the original code called) overwrites
    # the source/confidence of *every* lane term — that destroyed provenance.
    db.merge_lane_terms(profile_id, evolved, source="memory_evolution", confidence=0.78)
    return evolved


def import_resume_file(source_path):
    source = Path(source_path)
    if source.suffix.lower() != ".docx":
        raise ValueError("Profile resumes must be .docx files.")
    return copy_into_workspace(source, Path.cwd() / "Resumes")


def _resume_search_roots():
    return [
        Path.cwd() / "Resumes",
        Path.cwd() / "Application templates" / "CVs",
        Path.cwd(),
    ]


def _resume_option(path, source_label):
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "folder": source_label,
        "modified_at": stat.st_mtime,
        "size": stat.st_size,
    }


def command_resumes_list(payload):
    query = _clean_text(payload.get("query")).casefold()
    current = payload.get("current")
    resumes = {}

    for root in _resume_search_roots():
        if not root.exists():
            continue
        source_label = root.name if root != Path.cwd() else "Workspace"
        for path in root.glob("*.docx"):
            if path.name.startswith("~$"):
                continue
            haystack = f"{path.name} {path.parent} {source_label}".casefold()
            if query and query not in haystack:
                continue
            try:
                resolved = str(path.resolve())
                resumes[resolved] = _resume_option(path, source_label)
            except OSError:
                continue

    if current:
        current_path = Path(current)
        if not current_path.is_absolute():
            current_path = Path.cwd() / current_path
        if current_path.exists() and current_path.suffix.lower() == ".docx":
            try:
                resolved = str(current_path.resolve())
                if not query or query in f"{current_path.name} {current_path.parent}".casefold():
                    resumes[resolved] = _resume_option(current_path, "Current selection")
            except OSError:
                pass

    sorted_resumes = sorted(
        resumes.values(),
        key=lambda item: (-float(item["modified_at"]), item["name"].casefold()),
    )
    return {"resumes": sorted_resumes[:50]}


def command_profiles_list(_payload):
    return {"profiles": [row_to_dict(row) for row in db.get_all_lanes()]}


def command_lanes_list(payload):
    return {"lanes": [row_to_dict(row) for row in db.get_all_lanes(bool(payload.get("include_inactive", True)))]}


def command_profiles_add(payload):
    resume_path = import_resume_file(payload["resume_path"])
    if not db.add_lane(payload["name"], resume_path, payload.get("settings")):
        raise ValueError("Could not add profile. The name may already exist.")
    return command_profiles_list(payload)


def command_lanes_add(payload):
    data = command_profiles_add(payload)
    return {"lanes": data["profiles"], "profiles": data["profiles"]}


def command_profiles_update(payload):
    resume_path = import_resume_file(payload["resume_path"])
    lane_id = payload.get("lane_id") or payload.get("profile_id")
    if not db.update_lane(lane_id, payload["name"], resume_path, payload.get("settings")):
        raise ValueError("Could not update profile. The name may already exist.")
    return command_profiles_list(payload)


def command_lanes_bootstrap(payload):
    """Finish a new lane's optional LLM-assisted setup in the background."""
    profile_id = int(payload.get("profile_id") or 0)
    lane = db.get_lane_by_id(profile_id)
    if not lane:
        raise ValueError("Lane not found.")

    settings = db.get_lane_settings(profile_id)
    resume_text = read_resume_text(profile_id)
    if not resume_text.strip():
        raise ValueError("The selected base resume did not contain readable text.")

    keyword_mode = str(payload.get("keyword_mode") or "manual").strip().lower()
    manual_terms = [
        str(term).strip()
        for term in (payload.get("terms") or [])
        if str(term).strip()
    ]
    if keyword_mode == "manual":
        db.save_lane_terms(profile_id, manual_terms, source="manual", confidence=0.8)
        emit("log", message=f"Saved {len(manual_terms)} manual search terms for {lane['name']}.")

    fragment_count = 0
    fragment_provider = None
    if payload.get("generate_fragments", True):
        with contextlib.redirect_stdout(sys.stderr):
            import corpus_miner

        emit("status", message=f"Mining reusable fragments for {lane['name']}…")
        fragments, fragment_provider = corpus_miner.mine_documents(
            [{"filename": Path(lane["resume_path"]).name or "base-resume.docx", "text": resume_text}],
            settings,
            lambda message: emit("log", message=message),
        )
        person_id = lane["person_id"] if "person_id" in lane.keys() and lane["person_id"] else 1
        db.upsert_candidate_fragments(person_id, fragments, replace=False)
        db.upsert_profile_memory_fragments(profile_id, fragments, replace=False)
        suggestions = db.suggest_lane_fragment_affinity(profile_id, limit=200)
        db.upsert_lane_fragment_affinity(profile_id, suggestions)
        fragment_count = len(fragments)
        emit("log", message=f"Stored {fragment_count} base-resume fragments for {lane['name']}.")

    terms = manual_terms
    if keyword_mode == "generate":
        emit("status", message=f"Generating search terms for {lane['name']} with the local LLM…")
        app_logic = import_app_logic()
        terms = app_logic.execute_keyword_generation(
            payload.get("optimism", 3),
            resume_text,
            lambda message: emit("log", message=message),
            profile_id,
        )

    return {
        "profile_id": profile_id,
        "terms": terms,
        "fragments": fragment_count,
        "fragment_provider": fragment_provider,
    }


def command_lanes_update(payload):
    payload = {**payload, "profile_id": payload.get("lane_id") or payload.get("profile_id")}
    data = command_profiles_update(payload)
    return {"lanes": data["profiles"], "profiles": data["profiles"]}


def command_profiles_delete(payload):
    profile_id = payload.get("lane_id") or payload["profile_id"]
    if len(db.get_all_lanes()) <= 1:
        raise ValueError("Cannot delete the last remaining lane.")
    db.delete_profile(profile_id)
    return command_profiles_list(payload)


def command_lanes_delete(payload):
    data = command_profiles_delete({**payload, "profile_id": payload.get("lane_id") or payload.get("profile_id")})
    return {"lanes": data["profiles"], "profiles": data["profiles"]}


def command_resume_import(payload):
    return {"resume_path": import_resume_file(payload["path"])}


def command_candidate_fragments_list(payload):
    person_id = payload.get("person_id") or 1
    return {
        "fragments": [_memory_fragment_to_dict(row) for row in db.get_candidate_fragments(person_id, payload.get("limit") or 500, payload.get("query"))]
    }


def command_lanes_fragments_list(payload):
    lane_id = payload.get("lane_id") or payload.get("profile_id", 1)
    return {
        "fragments": [_memory_fragment_to_dict(row) for row in db.get_lane_fragments(lane_id, payload.get("limit") or 180)]
    }


def command_lanes_fragments_update(payload):
    lane_id = payload.get("lane_id") or payload.get("profile_id", 1)
    count = db.upsert_lane_fragment_affinity(lane_id, payload.get("affinities") or [])
    return {"updated": count, "fragments": command_lanes_fragments_list({"lane_id": lane_id})["fragments"]}


def command_lanes_fragments_suggest(payload):
    lane_id = payload.get("lane_id") or payload.get("profile_id", 1)
    suggestions = db.suggest_lane_fragment_affinity(lane_id, payload.get("limit") or 80)
    return {"suggestions": suggestions}


def command_lanes_learning_refresh(payload):
    return db.refresh_lane_learning_metrics(payload.get("lane_id") or payload.get("profile_id"))


def command_terms_get(payload):
    return {"terms": db.get_lane_terms(payload.get("lane_id") or payload.get("profile_id", 1))}


def command_terms_save(payload):
    terms = [term.strip() for term in payload.get("terms", []) if str(term).strip()]
    db.save_lane_terms(payload.get("lane_id") or payload.get("profile_id", 1), terms, source="manual", confidence=0.8)
    return {"terms": terms}


def command_terms_generate(payload):
    app_logic = import_app_logic()
    profile_id = payload.get("profile_id", 1)
    resume_text = read_resume_text(profile_id)
    terms = app_logic.execute_keyword_generation(
        payload.get("optimism", 3),
        resume_text,
        lambda message: emit("log", message=message),
        profile_id,
    )
    return {"terms": terms}


def _person_id_for(profile_id):
    lane = db.get_lane_by_id(profile_id)
    return lane["person_id"] if lane and "person_id" in lane.keys() and lane["person_id"] else 1


# Commands this module contributes to the bridge dispatch table.
# python_bridge.py merges these; adding a command here needs no edit there.
COMMANDS = {
    "lanes:list": command_lanes_list,
    "lanes:add": command_lanes_add,
    "lanes:update": command_lanes_update,
    "lanes:delete": command_lanes_delete,
    "candidate:fragments:list": command_candidate_fragments_list,
    "lanes:fragments:list": command_lanes_fragments_list,
    "lanes:fragments:update": command_lanes_fragments_update,
    "lanes:fragments:suggest": command_lanes_fragments_suggest,
    "lanes:learning:refresh": command_lanes_learning_refresh,
    "profiles:list": command_profiles_list,
    "profiles:add": command_profiles_add,
    "profiles:update": command_profiles_update,
    "profiles:delete": command_profiles_delete,
    "lanes:bootstrap": command_lanes_bootstrap,
    "resume:import": command_resume_import,
    "resumes:list": command_resumes_list,
    "terms:get": command_terms_get,
    "terms:save": command_terms_save,
    "terms:generate": command_terms_generate,
}

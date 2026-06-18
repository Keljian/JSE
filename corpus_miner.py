"""
corpus_miner.py — Phase 3 fragment re-mining.

The existing memory:scan mines a handful of *applied* jobs and produces generic
fragments ("experienced IT leader…"). This mines the FULL evidence corpus
(context_documents: 335 real resumes / cover letters / KSC responses) and extracts
SPECIFIC, quantified, evidence-anchored fragments (named employers, metrics, tools).

Resume/cover variants are near-duplicates, so we de-dupe by token overlap before
mining, batch documents to stay in budget, and let upsert_candidate_fragments'
fingerprint + support_count handle cross-document reinforcement.
"""
from __future__ import annotations

import json
import re
import sqlite3

import context_library as clib
from llm_handler import _call_document_ai, _model_name, _settings_for_ai_task


def _fast_caller(settings):
    """Use the provider selected for evidence and memory processing."""
    resolved = _settings_for_ai_task(settings, "memory_ai_provider")
    provider = resolved["doc_ai_provider"]
    if provider == "gemini":
        resolved["doc_ai_model"] = "gemini-2.5-flash"
    model = _model_name(resolved, provider)

    def call(system, user):
        response, _label = _call_document_ai(
            resolved,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.15,
            max_tokens=8192,
            json_mode=True,
        )
        return response

    return call, f"{provider.title()} ({model})"

EXTRACT_SYSTEM = """You extract reusable, SPECIFIC career fragments from a candidate's real prior documents (resumes, cover letters, key selection criteria responses).

Each fragment must capture ONE concrete, quantified achievement or capability, anchored in named evidence — a real employer, metric, dollar figure, team size, tool, or outcome. REJECT anything generic. "Experienced IT leader with 10+ years" is USELESS. "Delivered $15M in operational savings at Flavorite Group through infrastructure modernisation and MPLS-to-SD-WAN transition" is GOOD.

Return ONLY a JSON array (no markdown fence). Each item:
{
  "fragment_type": "achievement|evidence|capability|skill|positioning|domain|cover_angle",
  "theme": "<short label, e.g. 'Cloud cost optimisation'>",
  "claim": "<1-2 sentences, MUST contain a concrete fact: number, named employer, tool, or outcome>",
  "supporting_detail": "<extra context if available>",
  "skills": ["..."],
  "domains": ["..."],
  "job_families": ["it_management|infrastructure|business_analyst|project_delivery|engineering|transformation"],
  "keywords": ["ATS terms a matching job ad would use"],
  "seniority": "<e.g. manager, senior, lead>",
  "reuse_guidance": "<when/how to deploy this in an application>",
  "confidence": "high|medium|low"
}
De-duplicate within the batch. Prefer fewer, stronger, fact-rich fragments over many weak ones."""


def _select_distinct(docs, max_n, sim_threshold=0.72):
    """Greedily keep documents that aren't near-duplicates of already-kept ones."""
    selected, sets = [], []
    for d in sorted(docs, key=lambda x: -len(x["text"])):
        toks = set(clib._tokenize(d["text"]))
        if len(toks) < 30:
            continue
        if any(len(toks & s) / max(1, len(toks | s)) > sim_threshold for s in sets):
            continue
        selected.append(d)
        sets.append(toks)
        if len(selected) >= max_n:
            break
    return selected


def _batches(docs, budget=26000, per_doc_cap=9000):
    batch, size = [], 0
    for d in docs:
        t = d["text"][:per_doc_cap]
        if size + len(t) > budget and batch:
            yield batch
            batch, size = [], 0
        batch.append({"filename": d["filename"], "text": t})
        size += len(t)
    if batch:
        yield batch


def _parse(raw):
    raw = re.sub(r"^```(json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else []


def mine_documents(documents, settings, log=print):
    """Mine reusable fragments from an explicit set of extracted documents.

    Lane onboarding uses this narrow entry point for the selected base resume,
    without re-mining the candidate's entire evidence corpus.
    """
    caller, label = _fast_caller(settings)
    docs = [
        {
            "filename": str(document.get("filename") or "document"),
            "text": str(document.get("text") or "").strip(),
        }
        for document in (documents or [])
        if str(document.get("text") or "").strip()
    ]
    fragments = []
    for batch in _batches(docs):
        user = "DOCUMENTS:\n\n" + "\n\n".join(f"[{d['filename']}]\n{d['text']}" for d in batch)
        try:
            parsed = _parse(caller(EXTRACT_SYSTEM, user))
        except Exception as exc:
            log(f"Fragment mining failed: {exc}")
            continue
        names = [d["filename"] for d in batch]
        for fragment in parsed if isinstance(parsed, list) else []:
            if not isinstance(fragment, dict) or not fragment.get("claim") or not fragment.get("theme"):
                continue
            fragment["source_doc_paths"] = names
            fragment.setdefault("status", "established")
            fragments.append(fragment)
        log(f"Mined {len(fragments)} reusable fragments from {len(batch)} document(s).")
    return fragments, label


def mine_corpus(settings, log=print, limits=None):
    caller, label = _fast_caller(settings)
    limits = limits or {"resume": 18, "ksc_response": 8, "cover_letter": 14, "capability_statement": 2}

    conn = sqlite3.connect(str(clib.DB_PATH)); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT filename, doc_type, text FROM context_documents "
        "WHERE char_len >= 200 AND filename NOT LIKE '~$%'"
    ).fetchall()
    conn.close()

    by_type = {}
    for r in rows:
        by_type.setdefault(r["doc_type"], []).append({"filename": r["filename"], "text": r["text"]})

    fragments = []
    for dt, cap in limits.items():
        docs = _select_distinct(by_type.get(dt, []), cap)
        if not docs:
            continue
        log(f"Mining {dt}: {len(docs)} distinct documents (of {len(by_type.get(dt, []))})")
        for batch in _batches(docs):
            user = "DOCUMENTS:\n\n" + "\n\n".join(f"[{d['filename']}]\n{d['text']}" for d in batch)
            try:
                frs = _parse(caller(EXTRACT_SYSTEM, user))
            except Exception as e:
                log(f"  batch failed: {e}")
                continue
            names = [d["filename"] for d in batch]
            for f in frs:
                if isinstance(f, dict):
                    f["source_doc_paths"] = names
                    f.setdefault("status", "established")
            kept = [f for f in frs if isinstance(f, dict) and f.get("claim") and f.get("theme")]
            fragments.extend(kept)
            log(f"  +{len(kept)} fragments (batch of {len(batch)})")
    return fragments, label


if __name__ == "__main__":
    import os, sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    data = os.environ.get("JSE_DATA_DIR") or os.path.join(os.environ["APPDATA"], "JobApplicationAssistant")
    conn = sqlite3.connect(os.path.join(data, "job_applications.db")); conn.row_factory = sqlite3.Row
    settings = dict(conn.execute("SELECT * FROM profiles WHERE id=1").fetchone())
    frs, label = mine_corpus(settings)
    print(f"\nMined {len(frs)} fragments via {label}. Samples:")
    for f in frs[:12]:
        print(f"  [{f.get('fragment_type')}] {f.get('theme')}: {f.get('claim','')[:110]}")

"""The funnel feedback loop: outcome snapshots, priors, targeting, channel warmth.

Split out of database_manager.py, which re-exports everything here.
"""
import sqlite3
import re
import json
from datetime import datetime, timedelta
from .connection import (
    get_db_connection,
)
from .constants import (
    AUTO_REJECT_THRESHOLD,
    HIDDEN_MARKET_SOURCE,
    MANUAL_SOURCE,
)
from .text import (
    _ROLE_SIG_JACCARD,
    _advertiser_key,
    _company_key,
    _desc_signature,
    _normalized_title_key,
    _signature_similarity,
    _within_days,
)
from .settings import (
    get_kv_setting,
    set_kv_setting,
)
from .lanes import (
    _profile_filter_clause,
    get_all_lanes,
)

# Composite weighting. Named so the balance can be tuned without hunting the
# formula, and so llm_handler/the renderer read the same numbers.
#
# Rebalanced 2026-07-30 from 80/20 to 60/40. Outcome evidence (156 applications,
# 9 interviews) showed match_score does not separate outcomes at all: the 70-79
# band converted at 5.6% and the 80-89 band at 6.5%, against a 5.8% baseline.
# 80% of the ranking weight was sitting on a non-predictive input. fragment_score
# (alignment with evidence that has actually carried prior applications) gets the
# larger share of what remains. Note this only moves jobs that have a fragment
# score at all — most scored jobs do not, and for those composite == match.
COMPOSITE_MATCH_WEIGHT = 0.60


COMPOSITE_FRAGMENT_WEIGHT = 0.40


def calculate_composite_score(match_score, fragment_score):
    """Canonical score formula: COMPOSITE_MATCH_WEIGHT * final match +
    COMPOSITE_FRAGMENT_WEIGHT * fragment alignment."""
    if match_score is None:
        return None
    if fragment_score is None:
        return int(round(float(match_score)))
    return int(round(
        COMPOSITE_MATCH_WEIGHT * float(match_score)
        + COMPOSITE_FRAGMENT_WEIGHT * float(fragment_score)
    ))


def recalculate_composite_scores():
    """Repair stale composites left by older analysis/gatekeeper write ordering.

    Prior-aware: recomputes with the same bounded conversion prior the analysis
    path applies (item 6), so this repair pass does not strip the prior nudge.
    Priors are loaded once, not per row.
    """
    changed = 0
    priors = get_kv_setting(FUNNEL_CONVERSION_PRIORS_KEY)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, match_score, fragment_score, composite_score,
                   title, company, advertiser_company, employer_type, source
            FROM jobs WHERE match_score IS NOT NULL
            """
        ).fetchall()
        updates = []
        for row in rows:
            expected = composite_score_with_prior(row["match_score"], row["fragment_score"], dict(row), priors)
            if row["composite_score"] != expected:
                updates.append((expected, row["id"]))
        if updates:
            conn.executemany("UPDATE jobs SET composite_score = ? WHERE id = ?", updates)
            conn.commit()
            changed = len(updates)
    return changed


_OUTCOME_BACKFILL_FLAG = "application_outcomes_backfilled_v1"


# Terminal-vs-open outcomes. `pending` = applied, no signal yet; `interview`
# once any interview happens; `offer`/`declined` on explicit stage moves; the
# 50-day silent auto-decline writes `ghosted` (distinct from an employer's
# explicit no); `withdrawn` when the candidate pulls out.
OUTCOME_PENDING = "pending"


OUTCOME_INTERVIEW = "interview"


OUTCOME_FINAL_ROUND = "final_round"


OUTCOME_RUNNER_UP = "runner_up"


OUTCOME_OFFER = "offer"


OUTCOME_DECLINED = "declined"


OUTCOME_GHOSTED = "ghosted"


OUTCOME_WITHDRAWN = "withdrawn"


APPLICATION_OUTCOMES = (
    OUTCOME_PENDING, OUTCOME_INTERVIEW, OUTCOME_FINAL_ROUND, OUTCOME_RUNNER_UP,
    OUTCOME_OFFER, OUTCOME_DECLINED, OUTCOME_GHOSTED, OUTCOME_WITHDRAWN,
)


# How far an outcome can advance but not regress. interview never reverts to
# pending; a real offer/decline is not overwritten by a later ghosted sweep.
# final_round and runner_up rank above interview: a "second by a small margin"
# carries the strongest signal we have short of an offer, and collapsing it into
# `interview` then `declined` was hiding five of the most informative results in
# the whole funnel.
_OUTCOME_RANK = {
    OUTCOME_PENDING: 0, OUTCOME_GHOSTED: 1, OUTCOME_WITHDRAWN: 1,
    OUTCOME_DECLINED: 2, OUTCOME_INTERVIEW: 3,
    OUTCOME_FINAL_ROUND: 4, OUTCOME_RUNNER_UP: 5, OUTCOME_OFFER: 6,
}


# Human labels for the outcome vocabulary (bridge -> UI).
OUTCOME_LABELS = {
    OUTCOME_PENDING: "Awaiting response",
    OUTCOME_INTERVIEW: "Interviewed",
    OUTCOME_FINAL_ROUND: "Reached final round",
    OUTCOME_RUNNER_UP: "Runner-up",
    OUTCOME_OFFER: "Offer",
    OUTCOME_DECLINED: "Unsuccessful",
    OUTCOME_GHOSTED: "No response",
    OUTCOME_WITHDRAWN: "Withdrawn",
}


# How the application reached the employer. Board applications are the contested
# channel — the one where five final-round losses were decided by a candidate
# with more directly matched experience. Warm and direct approaches are the
# channel where that comparison does not happen, which is why conversion is
# reported per channel rather than pooled.
CHANNEL_BOARD = "board"


CHANNEL_RECRUITER = "recruiter"


CHANNEL_WARM_REFERRAL = "warm_referral"


CHANNEL_DIRECT_OUTREACH = "direct_outreach"


APPLICATION_CHANNELS = (
    CHANNEL_BOARD, CHANNEL_RECRUITER, CHANNEL_WARM_REFERRAL, CHANNEL_DIRECT_OUTREACH,
)


WARM_CHANNELS = (CHANNEL_WARM_REFERRAL, CHANNEL_DIRECT_OUTREACH)


CHANNEL_LABELS = {
    CHANNEL_BOARD: "Job board",
    CHANNEL_RECRUITER: "Recruiter",
    CHANNEL_WARM_REFERRAL: "Warm referral",
    CHANNEL_DIRECT_OUTREACH: "Direct outreach",
}


# Stage → outcome for manual pipeline transitions (update_job_application).
_STAGE_OUTCOME = {
    "interviewing": OUTCOME_INTERVIEW,
    "offer": OUTCOME_OFFER,
    "rejected_by_company": OUTCOME_DECLINED,
}


def application_channel(job):
    """Derive how an application reached (or would reach) the employer.

    An explicit stored `channel` always wins. Otherwise: hidden-market
    conversions are direct outreach by construction; a recruiter-advertised role
    is the recruiter channel; everything scraped is a board application.
    Externally-logged (`Manual`) applications are left unattributed rather than
    guessed — the user knows whether that one came from a referral, and a wrong
    guess would pollute the very comparison this dimension exists to make.
    """
    stored = str(job.get("channel") or "").strip()
    if stored in APPLICATION_CHANNELS:
        return stored
    source = str(job.get("source") or "").strip()
    if source == HIDDEN_MARKET_SOURCE:
        return CHANNEL_DIRECT_OUTREACH
    if source == MANUAL_SOURCE:
        return "unknown"
    if str(job.get("employer_type") or "").strip().lower() in ("recruiter", "recruitment_agency"):
        return CHANNEL_RECRUITER
    if source:
        return CHANNEL_BOARD
    return "unknown"


def _stored_channel(job):
    """application_channel, but None instead of "unknown" — what gets persisted.
    A NULL channel means "not attributed yet" and is what the user is prompted to
    fill in; the string "unknown" is a display bucket, not a stored value."""
    channel = application_channel(job)
    return channel if channel in APPLICATION_CHANNELS else None


# --- Channel warmth ---------------------------------------------------------
# Channel says how an application reaches the employer; warmth says how much
# that route is worth. Warmth is derived rather than stored as a fifth channel
# value, so Funnel Insights keeps reporting against the four persisted channels
# it has already backfilled while ranking gets the finer distinction between
# "a human is named" and "nobody is on the other end".
WARMTH_COLD = 0


WARMTH_NAMED = 1


WARMTH_WARM = 2


WARMTH_LABELS = {WARMTH_COLD: "Cold", WARMTH_NAMED: "Named contact", WARMTH_WARM: "Warm"}


def channel_warmth(job, warm_path=None):
    """How warm the route to this employer is: 0 cold, 1 named contact, 2 warm.

    Warm means the application is not decided by side-by-side comparison
    against a more directly matched candidate — a referral, or direct outreach
    to a person. Named means a human is identified but the route is still the
    portal. Cold means a board or recruiter submission with nobody on the other
    end, which is where applications lose to better-matched candidates.
    """
    if application_channel(job) in WARM_CHANNELS:
        return WARMTH_WARM
    if warm_path:
        return WARMTH_NAMED
    if str(job.get("contact_person") or "").strip() or str(job.get("contact_email") or "").strip():
        return WARMTH_NAMED
    return WARMTH_COLD


def warm_contact_index(profile_id=None, include_all_profiles=False):
    """Map of organisation_key -> known contacts at that employer.

    Same shape and purpose as recurrence_index: one grouped query so a board of
    jobs can be annotated with possible warm paths without an N+1 lookup.
    """
    clauses = ["organisation_key IS NOT NULL", "organisation_key <> ''"]
    params = []
    if profile_id and not include_all_profiles:
        clauses.append("(profile_id IS NULL OR profile_id = ?)")
        params.append(profile_id)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT name, organisation, organisation_key, role_title, relationship, email
            FROM warm_contacts WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchall()
    index = {}
    for row in rows:
        index.setdefault(row["organisation_key"], []).append(dict(row))
    return index


def warm_path_for_job(job, index):
    """Contacts already known at this job's employer, if any.

    Checks the real employer first, then the advertised and advertiser names, so
    a recruiter-listed role still finds the contact at the end client.
    """
    if not index:
        return []
    seen = set()
    matches = []
    for value in (job.get("actual_company"), job.get("company"), job.get("advertiser_company")):
        key = _company_key(value or "")
        if not key or key in seen:
            continue
        seen.add(key)
        matches.extend(index.get(key) or [])
    return matches


def annotate_channel_warmth(jobs, index=None):
    """Attach channel, warmth, and any known warm path to job dicts, in place."""
    for job in jobs:
        warm_path = warm_path_for_job(job, index)
        stored = str(job.get("channel") or "").strip()
        channel = application_channel(job)
        # The UI needs to distinguish a channel the user set from one derived
        # from the source, so it can show "derived" rather than a false choice.
        job["channel_source"] = "stored" if stored in APPLICATION_CHANNELS else "derived"
        job["channel"] = channel
        job["channel_label"] = CHANNEL_LABELS.get(channel, "Unattributed")
        job["warmth"] = channel_warmth(job, warm_path)
        job["warmth_label"] = WARMTH_LABELS[job["warmth"]]
        job["warm_path"] = [
            {
                "name": contact.get("name"),
                "organisation": contact.get("organisation"),
                "role_title": contact.get("role_title"),
                "relationship": contact.get("relationship"),
            }
            for contact in warm_path[:3]
        ]
    return jobs


# --- Two-track document strategy --------------------------------------------
# Overqualification screening is a measured rejection cause on coordinator and
# support-grade roles: the same senior evidence that wins a Head-of role is what
# gets the application binned. The fix already existed as a manual practice
# (a stripped-back CV); this makes the track selection explicit so document
# generation can act on it.
DOC_TRACK_SENIOR = "senior"


DOC_TRACK_STRIPPED = "stripped_back"


DOC_TRACKS = (DOC_TRACK_SENIOR, DOC_TRACK_STRIPPED)


DOC_TRACK_LABELS = {
    DOC_TRACK_SENIOR: "Full senior",
    DOC_TRACK_STRIPPED: "Stripped back",
}


# Bands that read as below a head-of-function resume's ceiling.
_STRIPPED_TRACK_BANDS = ("ic",)


_STRIPPED_TITLE_TERMS = (
    "coordinator", "officer", "administrator", "assistant", "support",
    "graduate", "junior", "trainee", "intern", "helpdesk", "service desk",
    "level 1", "level 2", "l1", "l2", "technician",
)


def document_track(job, flag_details=None):
    """Which document strategy this role needs, and why.

    Derived rather than asked of another LLM call: triage already reported the
    seniority direction, the salary band is already parsed, and the title band
    is already classified. An explicit stored track always wins, so a manual
    decision survives re-analysis.
    """
    from .campaign import campaign_salary_band
    # Imported here rather than at module scope: document_track needs a
    # module that imports this one back.
    stored = str(job.get("document_track") or "").strip().lower()
    if stored in DOC_TRACKS:
        return {"track": stored, "source": "manual", "reasons": ["Set manually for this role."]}

    reasons = []
    details = flag_details or {}
    if str(details.get("seniority_direction") or "").strip().lower() == "below":
        reasons.append("Triage placed the role below the resume's ceiling.")

    title = str(job.get("title") or "").lower()
    matched_titles = [term for term in _STRIPPED_TITLE_TERMS if term in title]
    if matched_titles:
        reasons.append(f"Title reads as support-grade: {', '.join(matched_titles[:3])}.")

    if _seniority_band(job.get("title")) in _STRIPPED_TRACK_BANDS and matched_titles:
        reasons.append("Individual-contributor band with a support-grade title.")

    if campaign_salary_band(job.get("salary")) == "below_target":
        reasons.append("Advertised salary sits below the target band.")

    # One weak signal is noise; the risk is real when the level reads low from
    # more than one direction, or when the gate said so outright.
    triage_said_below = any("Triage placed" in reason for reason in reasons)
    if triage_said_below or len(reasons) >= 2:
        return {"track": DOC_TRACK_STRIPPED, "source": "derived", "reasons": reasons}
    return {
        "track": DOC_TRACK_SENIOR,
        "source": "derived",
        "reasons": reasons or ["No overqualification signal; lead with full senior evidence."],
    }


def set_job_document_track(job_id, track):
    """Pin a document track for one role, or clear back to derived."""
    track = str(track or "").strip().lower()
    if track and track not in DOC_TRACKS:
        raise ValueError(f"Unknown document track: {track}")
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE jobs SET document_track = ?, updated_at = ? WHERE id = ?",
            (track or None, now, job_id),
        )
        conn.commit()
    return track or None


def resolve_document_track(job_id):
    """The effective track for a job, with the gate's own judgement folded in."""
    from .jobs import get_job_details, get_job_flags
    # Imported here rather than at module scope: resolve_document_track needs a
    # module that imports this one back.
    job = get_job_details(job_id)
    if not job:
        raise ValueError(f"Job {job_id} was not found.")
    flags = get_job_flags(job_id) or {}
    return document_track(dict(job), flags)


def set_job_channel(job_id, channel):
    """Record how this application reaches (or would reach) the employer.

    An empty channel clears the attribution back to derived, which is not the
    same as guessing: a NULL channel means the derivation in application_channel
    applies, and the user has not overruled it.
    """
    channel = str(channel or "").strip().lower()
    if channel and channel not in APPLICATION_CHANNELS:
        raise ValueError(f"Unknown application channel: {channel}")
    now = datetime.now().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE jobs SET channel = ?, updated_at = ? WHERE id = ?",
            (channel or None, now, job_id),
        )
        conn.commit()
    return channel or None


# Title-keyword → seniority band. "bridging" = broad-technologist roles that
# convert (technical manager/lead, platforms, system engineer, BA, vendor
# manager, team leader). Order matters: bridging is tested before exec so a
# "Technical Lead" is bridging, not manager-lead. Pure exec and deep single-
# specialty IC roles are the segments that did NOT convert.
#
# Coverage extended 2026-07-30 from the titles that actually produced interviews
# (see tests/test_targeting_strategy.py for the labelled fixture set). Two
# pre-existing behaviours were checked and kept deliberately:
#   - "Manager Platform Integration" is bridging via `platform` — correct, the
#     hybrid platform remit is the thing that converts.
#   - "Infrastructure Team Leader" is bridging via `team leader` — also kept: the
#     interviews came from team-leader titles (Senior Technical Solution Team
#     Leader), and the manager-lead 1.4% rate is driven by "IT Manager" /
#     "Head of ICT" style titles, not by team-leader ones.
_SENIORITY_BRIDGING = (
    "technical lead", "technical manager", "tech lead", "solution ", "solutions",
    "system engineer", "systems engineer", "platform", "business analyst",
    "vendor manager", "team leader", "team lead", "bridging",
    "technical solution", "systems and data", "business systems",
    "hybrid cloud", "technical delivery", "delivery lead", "engineering manager",
    "systems lead", "technical services", "practice lead",
)


_SENIORITY_EXEC = (
    "head of", "chief", " cio", " cto", "director", "general manager", "vice president",
    "executive", " vp ",
)


_SENIORITY_MANAGER = ("manager", "lead", "leader", "principal", "head")


def _seniority_band(title):
    text = f" {str(title or '').strip().lower()} "
    if text.strip() == "":
        return "unknown"
    if any(term in text for term in _SENIORITY_BRIDGING):
        return "bridging"
    if any(term in text for term in _SENIORITY_EXEC):
        return "exec"
    if any(term in text for term in _SENIORITY_MANAGER):
        return "manager-lead"
    return "ic"


def _match_score_band(score):
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "unscored"
    if value < 0:
        return "unscored"
    decade = max(0, min(90, (value // 10) * 10))
    return f"{decade}-{decade + 9}"


def _build_outcome_snapshot(job, doc_method=None):
    """Immutable dimensional copy captured at the applied transition."""
    from .campaign import campaign_salary_band
    # Imported here rather than at module scope: _build_outcome_snapshot needs a
    # module that imports this one back.
    salary = job.get("salary")
    return {
        "title": job.get("title"),
        "company": job.get("company"),
        "advertiser_company": job.get("advertiser_company"),
        "actual_company": job.get("actual_company"),
        "employer_type": job.get("employer_type"),
        "source": job.get("source"),
        "location": job.get("location"),
        "salary": salary,
        "salary_band": campaign_salary_band(salary),
        "match_score": job.get("match_score"),
        "match_score_band": _match_score_band(job.get("match_score")),
        "fragment_score": job.get("fragment_score"),
        "composite_score": job.get("composite_score"),
        "profile_id": job.get("profile_id"),
        "lane_id": job.get("profile_id"),
        "seniority_band": _seniority_band(job.get("title")),
        # None (not the string "unknown") when the channel cannot be derived, so
        # "not attributed" stays distinguishable from a real bucket. Aggregation
        # still buckets it as `unknown` for display.
        "channel": _stored_channel(job),
        "description_fingerprint": job.get("description_fingerprint"),
        "desc_sig": _desc_signature(job.get("description")),
        "doc_generation_method": doc_method,
    }


def _latest_doc_method(conn, job_id):
    if not job_id:
        return None
    row = conn.execute(
        """
        SELECT title FROM application_events
        WHERE job_id = ? AND event_type = 'documents'
        ORDER BY COALESCE(event_date, created_at) DESC, id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return None
    title = str(row["title"] or "")
    marker = "generated with "
    return title.split(marker, 1)[1].strip() if marker in title else title or None


def _compute_role_key(conn, job, applied_at=None):
    """Link re-advertised roles: same advertiser + (identical description
    fingerprint OR identical normalized title) inside a 90-day window collapse
    to one role_key. Reuses description_fingerprint / normalized-title
    machinery already used for dedupe."""
    adv_key = _advertiser_key(job)
    fingerprint = job.get("description_fingerprint")
    title_key = _normalized_title_key(job.get("title"))
    job_sig = _desc_signature(job.get("description"))
    job_id = job.get("id")
    if adv_key:
        rows = conn.execute(
            """
            SELECT job_id, role_key, snapshot_json, applied_at
            FROM application_outcomes
            WHERE role_key IS NOT NULL
            ORDER BY id DESC
            LIMIT 4000
            """
        ).fetchall()
        for row in rows:
            if job_id and row["job_id"] == job_id:
                continue
            try:
                snap = json.loads(row["snapshot_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            other_adv = _company_key(snap.get("advertiser_company") or snap.get("company") or "")
            if other_adv != adv_key:
                continue
            if not _within_days(applied_at, row["applied_at"], 90):
                continue
            other_fp = snap.get("description_fingerprint")
            same_desc = bool(fingerprint and other_fp and fingerprint == other_fp)
            similar_desc = _signature_similarity(job_sig, snap.get("desc_sig")) >= _ROLE_SIG_JACCARD
            same_title = bool(title_key and title_key == _normalized_title_key(snap.get("title")))
            if same_desc or similar_desc or same_title:
                return row["role_key"]
    if job_id:
        return f"rk-{job_id}"
    import uuid
    return f"rk-{uuid.uuid4().hex[:12]}"


def _apply_outcome_rank(existing, candidate):
    """Never regress a stored outcome (interview stays interview under a later
    ghosted sweep); advance monotonically otherwise."""
    if not existing:
        return candidate
    if _OUTCOME_RANK.get(candidate, 0) >= _OUTCOME_RANK.get(existing, 0):
        return candidate
    return existing


def ensure_application_outcome(conn, job_id, applied_at=None, outcome=None):
    """Create the immutable snapshot row for a job entering `applied` (idempotent
    per job_id). Runs inside the caller's transaction. Returns the outcome id."""
    existing = conn.execute(
        "SELECT id FROM application_outcomes WHERE job_id = ? ORDER BY id ASC LIMIT 1",
        (job_id,),
    ).fetchone()
    if existing:
        if outcome:
            set_application_outcome(conn, job_id, outcome)
        return existing["id"]
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        return None
    job = dict(job)
    applied_at = applied_at or job.get("application_date") or datetime.now().isoformat(timespec="seconds")
    snapshot = _build_outcome_snapshot(job, _latest_doc_method(conn, job_id))
    role_key = _compute_role_key(conn, job, applied_at)
    rounds = conn.execute(
        "SELECT COUNT(*) FROM interviews WHERE job_id = ?", (job_id,)
    ).fetchone()[0]
    resolved_outcome = outcome or (OUTCOME_INTERVIEW if rounds else OUTCOME_PENDING)
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO application_outcomes
            (job_id, role_key, snapshot_json, applied_at, outcome, outcome_at,
             interview_rounds, channel, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, role_key, json.dumps(snapshot, ensure_ascii=False),
            applied_at, resolved_outcome,
            now if resolved_outcome != OUTCOME_PENDING else None,
            rounds, snapshot.get("channel"), now, now,
        ),
    )
    return cursor.lastrowid


def set_application_outcome(conn, job_id, outcome, outcome_at=None, interview_rounds=None,
                            interview_stage_reached=None, loss_reason=None):
    """Advance a job's outcome (monotonic — see _apply_outcome_rank). Creates the
    snapshot first if the applied transition was somehow missed. Runs inside the
    caller's transaction.

    interview_stage_reached / loss_reason record how far a near miss actually got
    and why it ended. Both advance monotonically too: a later, vaguer update can
    add detail but never erase it."""
    if outcome not in APPLICATION_OUTCOMES:
        return
    row = conn.execute(
        "SELECT id, outcome, interview_rounds, interview_stage_reached FROM application_outcomes"
        " WHERE job_id = ? ORDER BY id ASC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not row:
        ensure_application_outcome(conn, job_id, outcome=outcome)
        row = conn.execute(
            "SELECT id, outcome, interview_rounds, interview_stage_reached FROM application_outcomes"
            " WHERE job_id = ? ORDER BY id ASC LIMIT 1",
            (job_id,),
        ).fetchone()
        if not row:
            return
    resolved = _apply_outcome_rank(row["outcome"], outcome)
    now = datetime.now().isoformat(timespec="seconds")
    rounds = interview_rounds
    if rounds is None:
        rounds = conn.execute(
            "SELECT COUNT(*) FROM interviews WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    stage = interview_stage_reached
    if stage is not None:
        try:
            stage = max(int(stage), int(row["interview_stage_reached"] or 0))
        except (TypeError, ValueError):
            stage = None
    conn.execute(
        """
        UPDATE application_outcomes
        SET outcome = ?,
            outcome_at = COALESCE(?, outcome_at),
            interview_rounds = MAX(COALESCE(interview_rounds, 0), ?),
            interview_stage_reached = COALESCE(?, interview_stage_reached),
            loss_reason = COALESCE(?, loss_reason),
            updated_at = ?
        WHERE id = ?
        """,
        (
            resolved,
            outcome_at or (now if resolved != OUTCOME_PENDING else None),
            rounds or 0,
            stage,
            (str(loss_reason).strip() or None) if loss_reason else None,
            now,
            row["id"],
        ),
    )


def record_application_outcome_detail(job_id, outcome=None, interview_stage_reached=None,
                                      loss_reason=None, channel=None):
    """User-facing write for the extended outcome vocabulary (item 4).

    Called from the outcome-hygiene nudge and the job view: "how far did it go?"
    rather than just "did it happen?". Also allows correcting the channel on an
    externally-logged application, which is the one case JSE cannot infer.
    """
    if outcome is not None and outcome not in APPLICATION_OUTCOMES:
        raise ValueError(f"Invalid application outcome: {outcome}")
    if channel is not None and channel not in APPLICATION_CHANNELS:
        raise ValueError(f"Invalid application channel: {channel}")
    with get_db_connection() as conn:
        if outcome is not None:
            set_application_outcome(
                conn, job_id, outcome,
                interview_stage_reached=interview_stage_reached,
                loss_reason=loss_reason,
            )
        elif interview_stage_reached is not None or loss_reason is not None:
            row = conn.execute(
                "SELECT id, outcome FROM application_outcomes WHERE job_id = ? ORDER BY id ASC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row:
                set_application_outcome(
                    conn, job_id, row["outcome"] or OUTCOME_PENDING,
                    interview_stage_reached=interview_stage_reached,
                    loss_reason=loss_reason,
                )
        if channel is not None:
            _set_outcome_channel(conn, job_id, channel)
        conn.commit()
    return get_application_outcome(job_id)


def _set_outcome_channel(conn, job_id, channel):
    """Write the channel to both the column and the immutable snapshot copy so
    aggregation reads the same value whichever it reaches for."""
    row = conn.execute(
        "SELECT id, snapshot_json FROM application_outcomes WHERE job_id = ? ORDER BY id ASC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not row:
        return
    try:
        snapshot = json.loads(row["snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        snapshot = {}
    snapshot["channel"] = channel
    conn.execute(
        "UPDATE application_outcomes SET channel = ?, snapshot_json = ?, updated_at = ? WHERE id = ?",
        (channel, json.dumps(snapshot, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds"), row["id"]),
    )


def get_application_outcome(job_id):
    """The outcome row for a job as a plain dict, snapshot parsed."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM application_outcomes WHERE job_id = ? ORDER BY id ASC LIMIT 1",
            (job_id,),
        ).fetchone()
    if not row:
        return None
    outcome = dict(row)
    try:
        outcome["snapshot"] = json.loads(row["snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        outcome["snapshot"] = {}
    outcome["outcome_label"] = OUTCOME_LABELS.get(outcome.get("outcome"), outcome.get("outcome"))
    outcome["channel"] = outcome.get("channel") or outcome["snapshot"].get("channel")
    return outcome


def _sync_outcome_for_stage(conn, job_id, stage, applied_at=None):
    """Bridge a pipeline stage transition to the outcome snapshot."""
    if stage == "applied":
        ensure_application_outcome(conn, job_id, applied_at=applied_at)
    elif stage in _STAGE_OUTCOME:
        set_application_outcome(conn, job_id, _STAGE_OUTCOME[stage])


def backfill_application_outcomes(conn=None):
    """One-time reconstruction of outcome snapshots from application history.

    Gated by an app_settings flag so it scans once. Candidate jobs are anything
    that ever reached `applied`: current post-applied stage, an application_date,
    a "Moved to Applied" stage event, or an interview row. Orphan events whose
    jobs row is gone (hard-deleted by the old lane cascade) still yield a row
    with whatever the events preserved. New applications get their snapshot from
    the applied-stage hook, not here.
    """
    from .jobs import normalize_stage
    # Imported here rather than at module scope: backfill_application_outcomes needs a
    # module that imports this one back.
    owns_conn = conn is None
    cm = get_db_connection() if owns_conn else None
    conn = cm.__enter__() if owns_conn else conn
    try:
        done = conn.execute(
            "SELECT value_json FROM app_settings WHERE key = ?", (_OUTCOME_BACKFILL_FLAG,)
        ).fetchone()
        if done and str(done["value_json"]).strip() in ("true", '"true"', "1"):
            return 0

        # 1. Jobs still present that ever reached applied.
        job_rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE pipeline_stage IN ('applied','interviewing','offer','rejected_by_company')
               OR status IN ('applied','interviewing','offer','rejected_by_company')
               OR application_date IS NOT NULL
               OR id IN (SELECT DISTINCT job_id FROM interviews)
               OR id IN (
                    SELECT DISTINCT job_id FROM application_events
                    WHERE event_type = 'stage' AND title LIKE 'Moved to Applied%'
               )
            """
        ).fetchall()
        created = 0
        seen_job_ids = set()
        for row in job_rows:
            job = dict(row)
            job_id = job["id"]
            seen_job_ids.add(job_id)
            already = conn.execute(
                "SELECT id FROM application_outcomes WHERE job_id = ? LIMIT 1", (job_id,)
            ).fetchone()
            if already:
                continue
            applied_at = (
                job.get("application_date")
                or _first_applied_event_date(conn, job_id)
                or job.get("date_scraped")
                or job.get("updated_at")
            )
            stage = normalize_stage(job.get("pipeline_stage") or job.get("status") or "applied")
            rounds = conn.execute(
                "SELECT COUNT(*) FROM interviews WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            outcome = _backfill_outcome_for(stage, rounds, job)
            snapshot = _build_outcome_snapshot(job, _latest_doc_method(conn, job_id))
            role_key = _compute_role_key(conn, job, applied_at)
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO application_outcomes
                    (job_id, role_key, snapshot_json, applied_at, outcome, outcome_at,
                     interview_rounds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, role_key, json.dumps(snapshot, ensure_ascii=False),
                    applied_at, outcome,
                    now if outcome != OUTCOME_PENDING else None,
                    rounds, now, now,
                ),
            )
            created += 1

        # 2. Orphan interviews/events whose jobs row is gone. These preserve the
        # fact of an interview even though every dimensional field is lost.
        orphan_ids = conn.execute(
            """
            SELECT DISTINCT job_id FROM (
                SELECT job_id FROM interviews
                UNION
                SELECT job_id FROM application_events
                WHERE event_type = 'stage' AND title LIKE 'Moved to Applied%'
            )
            WHERE job_id NOT IN (SELECT id FROM jobs)
              AND job_id NOT IN (SELECT job_id FROM application_outcomes WHERE job_id IS NOT NULL)
            """
        ).fetchall()
        for orphan in orphan_ids:
            job_id = orphan["job_id"]
            rounds = conn.execute(
                "SELECT COUNT(*) FROM interviews WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
            applied_at = _first_applied_event_date(conn, job_id)
            outcome = OUTCOME_INTERVIEW if rounds else OUTCOME_PENDING
            now = datetime.now().isoformat(timespec="seconds")
            # Reconstruct from job_postings / application_events before falling
            # back to a dimensionless stub (item 5).
            snapshot = _orphan_snapshot(conn, job_id, _latest_doc_method(conn, job_id))
            conn.execute(
                """
                INSERT INTO application_outcomes
                    (job_id, role_key, snapshot_json, applied_at, outcome, outcome_at,
                     interview_rounds, channel, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, f"rk-{job_id}", json.dumps(snapshot, ensure_ascii=False),
                    applied_at, outcome, now if outcome != OUTCOME_PENDING else None,
                    rounds, snapshot.get("channel"), now, now,
                ),
            )
            created += 1

        conn.execute(
            """
            INSERT INTO app_settings (key, value_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (_OUTCOME_BACKFILL_FLAG, json.dumps(True)),
        )
        conn.commit()
        return created
    finally:
        if owns_conn:
            cm.__exit__(None, None, None)


def _first_applied_event_date(conn, job_id):
    row = conn.execute(
        """
        SELECT COALESCE(event_date, created_at) AS at
        FROM application_events
        WHERE job_id = ? AND event_type = 'stage' AND title LIKE 'Moved to Applied%'
        ORDER BY COALESCE(event_date, created_at) ASC, id ASC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return row["at"] if row else None


_ORPHAN_REPAIR_FLAG = "application_outcomes_orphan_repair_v1"


_CHANNEL_BACKFILL_FLAG = "application_outcomes_channel_backfill_v1"


# Filename slug left behind by document/prompt events, e.g.
# "IT_Infrastructure_Cloud_Manager_external_llm_prompt.md" -> "IT Infrastructure
# Cloud Manager". Last-resort title recovery when job_postings has nothing.
_DOC_SLUG_RE = re.compile(r"([^\\/]+?)_(?:external_llm_prompt|resume|cover_letter|application)", re.I)


# The company-intelligence event stores a JSON blob that is frequently truncated
# mid-string, so it is scraped with a regex rather than parsed.
_EVENT_COMPANY_RE = re.compile(r'"(?:advertiser_company|actual_company)"\s*:\s*"([^"]+)"')


def _recover_job_dimensions(conn, job_id):
    """Reconstruct a deleted job's dimensional fields for outcome snapshots.

    Priority order, most complete first:
      1. `job_postings` — the normalized posting survives the lane-deletion
         cascade that removed the `jobs` row and carries every dimension.
      2. `application_events` — the company-intelligence blob yields advertiser
         and employer type; document/prompt filenames yield a title slug.
    Returns {} when nothing is recoverable, which is the signal to exclude the
    row from dimension aggregation rather than bucket it as `unknown`.
    """
    try:
        posting = conn.execute(
            """
            SELECT title, company, advertiser_company, actual_company, employer_type,
                   source, location, salary, description, description_fingerprint
            FROM job_postings WHERE legacy_job_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        posting = None
    if posting and (posting["title"] or posting["company"]):
        recovered = dict(posting)
        recovered["recovered_from"] = "job_postings"
        return recovered

    title = company = employer_type = None
    for event in conn.execute(
        "SELECT event_type, details FROM application_events WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall():
        details = str(event["details"] or "")
        if not company and event["event_type"] == "company":
            match = _EVENT_COMPANY_RE.search(details)
            if match:
                company = match.group(1)
            type_match = re.search(r'"employer_type"\s*:\s*"([^"]+)"', details)
            if type_match:
                employer_type = type_match.group(1)
        if not title:
            slug = _DOC_SLUG_RE.search(details)
            if slug:
                title = slug.group(1).replace("_", " ").strip()
    if not title and not company:
        return {}
    return {
        "title": title, "company": company, "advertiser_company": company,
        "employer_type": employer_type, "recovered_from": "application_events",
    }


def _orphan_snapshot(conn, job_id, doc_method=None):
    """Snapshot for a job whose `jobs` row is gone. Reconstructs what it can and
    marks the rest `unresolved` — an unresolved row is excluded from dimension
    aggregation (it has nothing to teach) instead of being bucketed as `unknown`,
    where it used to dilute every dimension it appeared in."""
    recovered = _recover_job_dimensions(conn, job_id)
    if not recovered.get("title"):
        return {"orphaned": True, "unresolved": True, "title": None, "seniority_band": "unknown"}
    snapshot = _build_outcome_snapshot(recovered, doc_method)
    snapshot["orphaned"] = True
    snapshot["recovered_from"] = recovered.get("recovered_from")
    return snapshot


def repair_orphaned_outcome_snapshots(conn=None):
    """Re-derive dimensional fields for snapshots written as orphaned stubs
    before event/posting recovery existed (item 5). Flag-gated: runs once.
    Returns the number of snapshots repaired."""
    owns_conn = conn is None
    cm = get_db_connection() if owns_conn else None
    conn = cm.__enter__() if owns_conn else conn
    try:
        done = conn.execute(
            "SELECT value_json FROM app_settings WHERE key = ?", (_ORPHAN_REPAIR_FLAG,)
        ).fetchone()
        if done and str(done["value_json"]).strip() in ("true", '"true"', "1"):
            return 0
        rows = conn.execute(
            "SELECT id, job_id, snapshot_json FROM application_outcomes WHERE job_id IS NOT NULL"
        ).fetchall()
        repaired = 0
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            if snapshot.get("title") or not snapshot.get("orphaned"):
                continue
            rebuilt = _orphan_snapshot(conn, row["job_id"], _latest_doc_method(conn, row["job_id"]))
            if not rebuilt.get("title"):
                # Still nothing recoverable: persist the unresolved marker so the
                # exclusion is explicit and countable rather than inferred.
                snapshot["unresolved"] = True
                conn.execute(
                    "UPDATE application_outcomes SET snapshot_json = ? WHERE id = ?",
                    (json.dumps(snapshot, ensure_ascii=False), row["id"]),
                )
                continue
            conn.execute(
                """
                UPDATE application_outcomes
                SET snapshot_json = ?, channel = COALESCE(channel, ?), updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(rebuilt, ensure_ascii=False), rebuilt.get("channel"),
                 datetime.now().isoformat(timespec="seconds"), row["id"]),
            )
            repaired += 1
        conn.execute(
            """
            INSERT INTO app_settings (key, value_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (_ORPHAN_REPAIR_FLAG, json.dumps(True)),
        )
        if repaired:
            # The cached insights still bucket these rows as `unknown`. Drop the
            # cache so the dashboard reflects the repair on next read instead of
            # waiting for the user to hit Recompute.
            conn.execute("DELETE FROM app_settings WHERE key = ?", (FUNNEL_INSIGHTS_CACHE_KEY,))
        conn.commit()
        return repaired
    finally:
        if owns_conn:
            cm.__exit__(None, None, None)


def backfill_outcome_channels(conn=None):
    """Attribute existing outcome snapshots to an application channel (item 6).

    Everything already recorded predates channel tracking and came off a job
    board, so `board` is the correct backfill — except externally-logged
    (`Manual`) applications, which are left unattributed for the user to set,
    and hidden-market conversions, which are direct outreach by construction.
    Flag-gated: runs once.
    """
    owns_conn = conn is None
    cm = get_db_connection() if owns_conn else None
    conn = cm.__enter__() if owns_conn else conn
    try:
        done = conn.execute(
            "SELECT value_json FROM app_settings WHERE key = ?", (_CHANNEL_BACKFILL_FLAG,)
        ).fetchone()
        if done and str(done["value_json"]).strip() in ("true", '"true"', "1"):
            return 0
        rows = conn.execute(
            "SELECT id, snapshot_json, channel FROM application_outcomes"
        ).fetchall()
        updated = 0
        for row in rows:
            if row["channel"]:
                continue
            try:
                snapshot = json.loads(row["snapshot_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                snapshot = {}
            channel = application_channel(snapshot)
            if channel == "unknown" and not snapshot.get("unresolved"):
                channel = CHANNEL_BOARD if snapshot.get("source") not in (None, "", MANUAL_SOURCE) else None
            if channel in ("unknown", None):
                continue
            snapshot["channel"] = channel
            conn.execute(
                "UPDATE application_outcomes SET channel = ?, snapshot_json = ? WHERE id = ?",
                (channel, json.dumps(snapshot, ensure_ascii=False), row["id"]),
            )
            updated += 1
        conn.execute(
            """
            INSERT INTO app_settings (key, value_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (_CHANNEL_BACKFILL_FLAG, json.dumps(True)),
        )
        if updated:
            # New channel dimension: the cached insights predate it.
            conn.execute("DELETE FROM app_settings WHERE key = ?", (FUNNEL_INSIGHTS_CACHE_KEY,))
        conn.commit()
        return updated
    finally:
        if owns_conn:
            cm.__exit__(None, None, None)


def _backfill_outcome_for(stage, rounds, job):
    if stage == "offer":
        return OUTCOME_OFFER
    if rounds or stage == "interviewing":
        return OUTCOME_INTERVIEW
    if stage == "rejected_by_company":
        # A silent 50-day auto-decline reads as ghosted; an explicit reason is a
        # real employer decline.
        reason = str(job.get("retired_reason") or "").lower()
        return OUTCOME_GHOSTED if "no interview recorded" in reason else OUTCOME_DECLINED
    return OUTCOME_PENDING


FUNNEL_INSIGHTS_CACHE_KEY = "funnel_insights_cache"


FUNNEL_CONVERSION_PRIORS_KEY = "funnel_conversion_priors"


MIN_SEGMENT_APPLICATIONS = 3


# A dimension bucket needs at least this many outcomes before its conversion
# prior is allowed to nudge composite_score (item 6). Buckets below this
# contribute 0 so a single lucky/unlucky application can't move scoring.
MIN_PRIOR_OUTCOMES = 5


# Outcomes that count as "reached an interview" for conversion.
_POSITIVE_OUTCOMES = {
    OUTCOME_INTERVIEW, OUTCOME_FINAL_ROUND, OUTCOME_RUNNER_UP, OUTCOME_OFFER,
}


# Outcomes that count as "reached a final round" — the second conversion rate.
_FINAL_ROUND_OUTCOMES = {OUTCOME_FINAL_ROUND, OUTCOME_RUNNER_UP, OUTCOME_OFFER}


# Dimensions that feed the composite-score conversion prior.
_PRIOR_DIMENSIONS = ("advertiser", "channel", "employer_type", "seniority_band", "source")


# How much authority each dimension's observed conversion rate is allowed over a
# composite score, and how sharply an observed lift converts into score points.
#
# The default ±10 / x40 was correct while every prior was unproven. The seniority
# evidence now exceeds what that can express: bridging titles convert at 29% and
# manager-lead at 1.4% against a 5.8% baseline — a ~20x spread that x40 maps to
# barely ±9 points before clamping. seniority_band therefore gets a wider clamp
# and a steeper scale; every other dimension keeps the conservative default.
DEFAULT_PRIOR_CLAMP = 10


DEFAULT_PRIOR_SCALE = 40


PRIOR_CLAMP_BY_DIMENSION = {"seniority_band": 25}


PRIOR_SCALE_BY_DIMENSION = {"seniority_band": 100}


def prior_clamp_for(dimension):
    return PRIOR_CLAMP_BY_DIMENSION.get(dimension, DEFAULT_PRIOR_CLAMP)


def prior_scale_for(dimension):
    return PRIOR_SCALE_BY_DIMENSION.get(dimension, DEFAULT_PRIOR_SCALE)


def _role_records():
    """Collapse application_outcomes into one record per role_key.

    Records whose snapshot has no title at all (a deleted job nothing could be
    recovered for) are marked `unresolved`. They still count toward the headline
    application/interview totals — the application really happened — but are
    excluded from every dimension breakdown, because bucketing them as `unknown`
    made a data gap look like a segment and diluted every dimension they touched
    (item 5). compute_funnel_insights reports the excluded count so the gap stays
    visible instead of silently shrinking the denominators.
    """
    lane_names = {lane["id"]: lane["name"] for lane in get_all_lanes()}
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT job_id, role_key, snapshot_json, outcome, interview_rounds,
                   applied_at, channel, interview_stage_reached, loss_reason
            FROM application_outcomes
            """
        ).fetchall()
    by_role = {}
    for row in rows:
        role_key = row["role_key"] or f"job-{row['job_id']}"
        try:
            snap = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            snap = {}
        record = by_role.setdefault(role_key, {
            "role_key": role_key, "reached_interview": False, "reached_final_round": False,
            "best_rank": 0, "outcome": OUTCOME_PENDING, "snap": {}, "rounds": 0,
            "channel": None, "stage_reached": 0, "loss_reason": None,
        })
        if row["outcome"] in _POSITIVE_OUTCOMES:
            record["reached_interview"] = True
        if row["outcome"] in _FINAL_ROUND_OUTCOMES:
            record["reached_final_round"] = True
        rank = _OUTCOME_RANK.get(row["outcome"], 0)
        if rank >= record["best_rank"]:
            record["best_rank"] = rank
            record["outcome"] = row["outcome"]
        record["rounds"] = max(record["rounds"], row["interview_rounds"] or 0)
        record["stage_reached"] = max(record["stage_reached"], row["interview_stage_reached"] or 0)
        record["loss_reason"] = record["loss_reason"] or row["loss_reason"]
        record["channel"] = record["channel"] or row["channel"] or snap.get("channel")
        record["applied_at"] = record.get("applied_at") or row["applied_at"]
        # Prefer the most complete (non-orphaned, titled) snapshot as canonical.
        if not record["snap"].get("title") and snap.get("title"):
            record["snap"] = snap
        elif not record["snap"]:
            record["snap"] = snap
    for record in by_role.values():
        snap = record["snap"]
        lane_id = snap.get("lane_id") or snap.get("profile_id")
        record["unresolved"] = not snap.get("title")
        record["dimensions"] = {
            "source": snap.get("source") or "unknown",
            "advertiser": snap.get("advertiser_company") or snap.get("company") or "unknown",
            "channel": record["channel"] or "unknown",
            "employer_type": snap.get("employer_type") or "unknown",
            "match_score_band": snap.get("match_score_band") or "unscored",
            "salary_band": snap.get("salary_band") or "unknown",
            "seniority_band": snap.get("seniority_band") or "unknown",
            "lane": lane_names.get(lane_id, "unknown"),
        }
    return list(by_role.values())


def compute_funnel_insights(store=True):
    """Conversion-to-interview by dimension, aggregated by role_key.

    Also derives the bounded per-dimension conversion priors used by composite
    scoring (item 6) and caches both in app_settings so the dashboard card and
    scoring path read without recomputing.
    """
    records = _role_records()
    total = len(records)
    interviews = sum(1 for r in records if r["reached_interview"])
    final_rounds = sum(1 for r in records if r["reached_final_round"])
    baseline = (interviews / total) if total else 0.0
    # The second conversion rate. Application -> interview and interview -> final
    # round have different causes and different fixes: the first is an allocation
    # problem (which jobs get applied to), the second is a competition problem
    # (who else is in the room). Pooling them hides which one is failing.
    final_round_rate = (final_rounds / interviews) if interviews else 0.0

    # Dimension breakdowns run on resolved records only; unresolved ones have no
    # dimensions to attribute and would otherwise all pile into `unknown`.
    resolved = [r for r in records if not r["unresolved"]]
    excluded = total - len(resolved)

    dimension_labels = {
        "source": "Source", "advertiser": "Advertiser / recruiter",
        "channel": "Channel", "employer_type": "Employer type",
        "match_score_band": "Match-score band",
        "salary_band": "Salary band", "seniority_band": "Seniority band", "lane": "Lane",
    }
    dimensions = {}
    for dim in dimension_labels:
        buckets = {}
        for record in resolved:
            value = record["dimensions"].get(dim) or "unknown"
            bucket = buckets.setdefault(value, {"value": value, "applications": 0, "interviews": 0})
            bucket["applications"] += 1
            if record["reached_interview"]:
                bucket["interviews"] += 1
        segments = []
        for bucket in buckets.values():
            if bucket["applications"] < MIN_SEGMENT_APPLICATIONS:
                continue
            rate = bucket["interviews"] / bucket["applications"]
            bucket["rate"] = round(rate, 4)
            bucket["lift"] = round(rate - baseline, 4)
            segments.append(bucket)
        segments.sort(key=lambda b: (b["rate"], b["applications"]), reverse=True)
        dimensions[dim] = {"label": dimension_labels[dim], "segments": segments}

    ranked = [
        {**seg, "dimension": dim, "dimension_label": dimension_labels[dim]}
        for dim, data in dimensions.items()
        for seg in data["segments"]
    ]
    ranked.sort(key=lambda b: (b["rate"], b["applications"]), reverse=True)
    top_segments = [s for s in ranked if s["rate"] > baseline][:6]
    worst_segments = [s for s in reversed(ranked) if s["rate"] < baseline][:6]

    insights = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_applications": total,
        "total_interviews": interviews,
        "total_final_rounds": final_rounds,
        "baseline_rate": round(baseline, 4),
        "final_round_rate": round(final_round_rate, 4),
        "excluded_unresolved": excluded,
        "min_segment_applications": MIN_SEGMENT_APPLICATIONS,
        "dimensions": dimensions,
        "top_segments": top_segments,
        "worst_segments": worst_segments,
    }

    priors = _derive_conversion_priors(resolved, baseline)
    if store:
        set_kv_setting(FUNNEL_INSIGHTS_CACHE_KEY, insights)
        set_kv_setting(FUNNEL_CONVERSION_PRIORS_KEY, priors)
    return insights


def _derive_conversion_priors(records, baseline):
    """Per-dimension conversion priors for composite scoring. delta is a bounded
    nudge derived from a bucket's lift over baseline, scaled and clamped per
    dimension (see PRIOR_SCALE_BY_DIMENSION / PRIOR_CLAMP_BY_DIMENSION); support
    < MIN_PRIOR_OUTCOMES buckets are recorded with delta 0 so they never move a
    score (item 6). `clamp` is stored alongside so consumers know how much
    authority the dimension carried when the prior was derived."""
    priors = {"baseline_rate": round(baseline, 4), "dimensions": {}}
    for dim in _PRIOR_DIMENSIONS:
        clamp = prior_clamp_for(dim)
        scale = prior_scale_for(dim)
        buckets = {}
        for record in records:
            value = record["dimensions"].get(dim) or "unknown"
            bucket = buckets.setdefault(value, {"applications": 0, "interviews": 0})
            bucket["applications"] += 1
            if record["reached_interview"]:
                bucket["interviews"] += 1
        dim_priors = {}
        for value, bucket in buckets.items():
            support = bucket["applications"]
            rate = bucket["interviews"] / support if support else 0.0
            if support >= MIN_PRIOR_OUTCOMES:
                delta = max(-clamp, min(clamp, int(round((rate - baseline) * scale))))
            else:
                delta = 0
            dim_priors[value] = {
                "support": support, "rate": round(rate, 4),
                "delta": delta, "clamp": clamp,
            }
        priors["dimensions"][dim] = dim_priors
    return priors


def get_funnel_insights(recompute=False):
    if recompute:
        return compute_funnel_insights(store=True)
    cached = get_kv_setting(FUNNEL_INSIGHTS_CACHE_KEY)
    if cached:
        return cached
    return compute_funnel_insights(store=True)


def _job_prior_dimensions(job):
    """Extract the prior-dimension bucket values for a job row/dict."""
    return {
        "advertiser": (job.get("advertiser_company") or job.get("company") or "unknown"),
        "channel": application_channel(job),
        "employer_type": job.get("employer_type") or "unknown",
        "seniority_band": _seniority_band(job.get("title")),
        "source": job.get("source") or "unknown",
    }


def conversion_prior_delta(job, priors=None, explain=False):
    """Bounded composite-score adjustment from cached conversion priors.

    Combines the qualifying per-dimension deltas as an average weighted by each
    dimension's clamp, so no single dimension dominates by accident but a
    dimension explicitly granted more authority (seniority_band, ±25) is not
    diluted back to ±10 by three ±10 neighbours. With equal clamps this reduces
    exactly to the previous arithmetic mean. Buckets below MIN_PRIOR_OUTCOMES
    already carry delta 0 and contribute nothing.

    With explain=True, returns (delta, reasons) where reasons describe each
    contributing dimension — the job view renders these so a demotion is never
    silent.
    """
    priors = priors if priors is not None else get_kv_setting(FUNNEL_CONVERSION_PRIORS_KEY)
    if not priors or not priors.get("dimensions"):
        return (0, []) if explain else 0
    dims = _job_prior_dimensions(job)
    weighted_sum = 0.0
    weight_total = 0.0
    widest_clamp = DEFAULT_PRIOR_CLAMP
    reasons = []
    for dim, value in dims.items():
        bucket = (priors["dimensions"].get(dim) or {}).get(value)
        if not bucket or bucket.get("support", 0) < MIN_PRIOR_OUTCOMES or not bucket.get("delta"):
            continue
        clamp = bucket.get("clamp") or prior_clamp_for(dim)
        weighted_sum += float(bucket["delta"]) * clamp
        weight_total += clamp
        widest_clamp = max(widest_clamp, clamp)
        reasons.append({
            "dimension": dim,
            "value": value,
            "delta": bucket["delta"],
            "rate": bucket.get("rate"),
            "support": bucket.get("support"),
        })
    if not weight_total:
        return (0, []) if explain else 0
    delta = max(-widest_clamp, min(widest_clamp, int(round(weighted_sum / weight_total))))
    return (delta, reasons) if explain else delta


def composite_score_with_prior(match_score, fragment_score, job, priors=None):
    """calculate_composite_score + a bounded conversion-prior nudge that can
    never, on its own, push a job across the auto-reject threshold (respects the
    2026-06-23 fix: scoring alone must not condemn or rescue a job). The wider
    seniority-band clamp does not relax that guard — it only changes how far a
    score can move on the same side of the line."""
    base = calculate_composite_score(match_score, fragment_score)
    if base is None:
        return None
    delta = conversion_prior_delta(job, priors)
    if not delta:
        return base
    adjusted = base + delta
    if base >= AUTO_REJECT_THRESHOLD:
        adjusted = max(adjusted, AUTO_REJECT_THRESHOLD)
    else:
        adjusted = min(adjusted, AUTO_REJECT_THRESHOLD - 1)
    return max(0, min(100, adjusted))


def explain_composite_score(job, priors=None):
    """Why a job's composite sits where it does: the base blend plus every
    conversion-prior dimension that moved it, with the observed rate that
    justified the move. Powers the score explanation in the job view so a
    band demotion is visible rather than silent (item 3)."""
    match_score = job.get("match_score")
    fragment_score = job.get("fragment_score")
    base = calculate_composite_score(match_score, fragment_score)
    delta, reasons = conversion_prior_delta(job, priors, explain=True)
    final = composite_score_with_prior(match_score, fragment_score, job, priors)
    clipped = bool(delta) and final is not None and base is not None and final != base + delta
    return {
        "match_score": match_score,
        "fragment_score": fragment_score,
        "match_weight": COMPOSITE_MATCH_WEIGHT,
        "fragment_weight": COMPOSITE_FRAGMENT_WEIGHT,
        "base": base,
        "prior_delta": delta,
        "composite": final,
        "clamped_by_auto_reject": clipped,
        "seniority_band": _seniority_band(job.get("title")),
        "channel": application_channel(job),
        "reasons": reasons,
    }


SENIORITY_BANDS = ("bridging", "exec", "manager-lead", "ic", "unknown")


SENIORITY_BAND_LABELS = {
    "bridging": "Bridging (hybrid technical/commercial)",
    "exec": "Executive",
    "manager-lead": "Manager / lead",
    "ic": "Individual contributor",
    "unknown": "Unclassified",
}


# A band needs at least this many observed applications before its rate is shown
# as guidance rather than noise.
MIN_BAND_YIELD_SUPPORT = MIN_PRIOR_OUTCOMES


def seniority_band_yields(priors=None):
    """Observed interview rate per seniority band, from the cached priors.

    Returned as {band: {rate, support, delta, low_yield}} for the triage gate and
    the Targeting card. `low_yield` marks a band whose observed rate is below the
    overall baseline with enough support to be believed — those are flagged, not
    auto-rejected: band alone must never reject a job.
    """
    priors = priors if priors is not None else get_kv_setting(FUNNEL_CONVERSION_PRIORS_KEY)
    baseline = (priors or {}).get("baseline_rate") or 0.0
    buckets = ((priors or {}).get("dimensions") or {}).get("seniority_band") or {}
    yields = {}
    for band in SENIORITY_BANDS:
        bucket = buckets.get(band) or {}
        support = bucket.get("support", 0)
        rate = bucket.get("rate")
        credible = support >= MIN_BAND_YIELD_SUPPORT and rate is not None
        yields[band] = {
            "band": band,
            "label": SENIORITY_BAND_LABELS.get(band, band),
            "support": support,
            "rate": rate,
            "delta": bucket.get("delta", 0),
            "credible": credible,
            "low_yield": bool(credible and rate < baseline),
            "high_yield": bool(credible and rate > baseline),
        }
    return {"baseline_rate": round(baseline, 4), "bands": yields}


def band_triage_note(title, priors=None):
    """One-line, human-readable band verdict for a job title.

    Used by the analysis path to append the observed rate to the stored analysis
    text, so the reason a job was demoted is visible in the job view rather than
    buried in a score. Returns None when there is nothing credible to say.
    """
    band = _seniority_band(title)
    data = seniority_band_yields(priors)
    entry = (data.get("bands") or {}).get(band) or {}
    if not entry.get("credible"):
        return None
    rate = f"{round((entry.get('rate') or 0) * 100, 1)}%"
    baseline = f"{round((data.get('baseline_rate') or 0) * 100, 1)}%"
    if entry.get("high_yield"):
        return (f"Seniority band: {entry['label']}. Observed interview rate {rate} "
                f"across {entry['support']} applications, above the {baseline} baseline. "
                f"This is the band that converts.")
    if entry.get("low_yield"):
        return (f"Seniority band: {entry['label']}. Observed interview rate {rate} "
                f"across {entry['support']} applications, below the {baseline} baseline. "
                f"Low-yield band — flagged, not rejected; band alone never rejects a job.")
    return (f"Seniority band: {entry['label']}. Observed interview rate {rate} "
            f"across {entry['support']} applications (baseline {baseline}).")


def get_targeting_summary(profile_id=None, include_all_profiles=False, days=90):
    """Allocation view for the trailing window (item 7).

    Applications by seniority band, conversion by band and by channel, and the
    proportion of applications landing in bands that convert below baseline —
    the single number that says whether effort is going where it pays.
    """
    cutoff = (datetime.now() - timedelta(days=int(days or 90))).date().isoformat()
    lifetime = [r for r in _role_records() if not r["unresolved"]]
    records = [r for r in lifetime if str(r.get("applied_at") or "")[:10] >= cutoff]
    lifetime_total = len(lifetime)
    lifetime_interviews = sum(1 for r in lifetime if r["reached_interview"])
    baseline = (lifetime_interviews / lifetime_total) if lifetime_total else 0.0

    def _breakdown(key, order=None, labels=None):
        buckets = {}
        for record in records:
            value = record["dimensions"].get(key) or "unknown"
            bucket = buckets.setdefault(value, {
                "value": value, "label": (labels or {}).get(value, value),
                "applications": 0, "interviews": 0, "final_rounds": 0,
            })
            bucket["applications"] += 1
            if record["reached_interview"]:
                bucket["interviews"] += 1
            if record["reached_final_round"]:
                bucket["final_rounds"] += 1
        rows = []
        for bucket in buckets.values():
            rate = bucket["interviews"] / bucket["applications"] if bucket["applications"] else 0.0
            bucket["rate"] = round(rate, 4)
            bucket["lift"] = round(rate - baseline, 4)
            bucket["below_baseline"] = rate < baseline
            rows.append(bucket)
        if order:
            rows.sort(key=lambda b: order.index(b["value"]) if b["value"] in order else len(order))
        else:
            rows.sort(key=lambda b: (b["applications"], b["rate"]), reverse=True)
        return rows

    by_band = _breakdown("seniority_band", SENIORITY_BANDS, SENIORITY_BAND_LABELS)
    by_channel = _breakdown("channel", None, CHANNEL_LABELS)
    total = len(records)
    # Applications that landed in a band converting below the lifetime baseline,
    # with enough history behind that judgement to be worth acting on.
    yields = seniority_band_yields()
    misallocated = sum(
        row["applications"] for row in by_band
        if (yields["bands"].get(row["value"]) or {}).get("low_yield")
    )
    warm = sum(
        row["applications"] for row in by_channel if row["value"] in WARM_CHANNELS
    )
    return {
        "window_days": int(days or 90),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_applications": total,
        "total_interviews": sum(1 for r in records if r["reached_interview"]),
        "total_final_rounds": sum(1 for r in records if r["reached_final_round"]),
        "baseline_rate": round(baseline, 4),
        "by_band": by_band,
        "by_channel": by_channel,
        "below_baseline_applications": misallocated,
        "below_baseline_share": round(misallocated / total, 4) if total else 0.0,
        "warm_channel_applications": warm,
        "warm_channel_share": round(warm / total, 4) if total else 0.0,
        "band_yields": yields,
    }


CHANNEL_MIX_MIN_APPLICATIONS = 5


CHANNEL_MIX_WARM_TARGET = 0.2


def get_channel_mix(profile_id=None, include_all_profiles=False, days=30):
    """How cold the recent application mix is, and what warm work is available.

    Warm-channel activity (get_warm_channel_activity) answers "did any
    hidden-market work happen"; this answers the sharper question "of the
    applications actually sent, how many went through a channel where the
    application is not judged side by side against a better-matched candidate".
    A run of cold portal submissions is the mix worth naming, and it is only
    actionable if the nudge also says which live roles already have a contact
    behind them — so it carries those too.
    """
    days = max(1, int(days or 30))
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        applied_rows = conn.execute(
            f"""
            SELECT jobs.channel, jobs.source, jobs.employer_type
            FROM jobs
            WHERE jobs.application_date IS NOT NULL
            AND date(jobs.application_date) >= date(?)
            {profile_clause}
            """,
            [cutoff] + params,
        ).fetchall()
        open_rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.actual_company,
                   jobs.advertiser_company, jobs.channel, jobs.source,
                   jobs.employer_type, jobs.contact_person, jobs.contact_email
            FROM jobs
            WHERE jobs.pipeline_stage IN ('new', 'interested')
            {profile_clause}
            """,
            params,
        ).fetchall()

    counts = {}
    for row in applied_rows:
        counts[application_channel(dict(row))] = counts.get(application_channel(dict(row)), 0) + 1
    total = sum(counts.values())
    warm = sum(count for channel, count in counts.items() if channel in WARM_CHANNELS)
    cold_share = round((total - warm) / total, 4) if total else 0.0

    index = warm_contact_index(profile_id, include_all_profiles)
    untapped = []
    for row in open_rows:
        job = dict(row)
        path = warm_path_for_job(job, index)
        if not path or application_channel(job) in WARM_CHANNELS:
            continue
        untapped.append({
            "id": job["id"],
            "title": job.get("title"),
            "company": job.get("actual_company") or job.get("company"),
            "contacts": [contact.get("name") for contact in path[:3] if contact.get("name")],
        })

    return {
        "window_days": days,
        "applications": total,
        "warm_applications": warm,
        "warm_share": round(warm / total, 4) if total else 0.0,
        "cold_share": cold_share,
        "by_channel": [
            {"value": channel, "label": CHANNEL_LABELS.get(channel, "Unattributed"), "applications": count}
            for channel, count in sorted(counts.items(), key=lambda item: -item[1])
        ],
        # Only nudge once there is enough history for the ratio to mean anything.
        "skewed_cold": total >= CHANNEL_MIX_MIN_APPLICATIONS and (warm / total) < CHANNEL_MIX_WARM_TARGET,
        "untapped_warm_paths": untapped[:5],
        "untapped_count": len(untapped),
    }

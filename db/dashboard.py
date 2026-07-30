"""Aggregate views: dashboard, activity stats, and calendar items.

Split out of database_manager.py, which re-exports everything here.
"""
from datetime import datetime
from .connection import (
    get_db_connection,
)
from .text import (
    normalize_source,
)
from .lanes import (
    _profile_filter_clause,
)
from .outcomes import (
    get_channel_mix,
)
from .intel import (
    get_warm_channel_activity,
)

def get_dashboard(profile_id=None, include_all_profiles=False):
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        stage_rows = conn.execute(
            f"""
            SELECT pipeline_stage, COUNT(*) AS count
            FROM jobs
            WHERE 1 = 1 {profile_clause}
            GROUP BY pipeline_stage
            """,
            params,
        ).fetchall()
        due_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.next_action_date IS NOT NULL
            AND jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            {profile_clause}
            ORDER BY jobs.next_action_date ASC
            LIMIT 12
            """,
            params,
        ).fetchall()
        top_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.match_score IS NOT NULL
            AND jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            {profile_clause}
            ORDER BY jobs.match_score DESC, jobs.id DESC
            LIMIT 8
            """,
            params,
        ).fetchall()
        feedback_rows = conn.execute(
            f"""
            SELECT jobs.*, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage IN ('applied', 'interviewing')
            AND (jobs.feedback IS NULL OR jobs.feedback = '')
            {profile_clause}
            ORDER BY COALESCE(jobs.application_date, jobs.last_interaction_at, jobs.id) DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
        cleanup_rows = conn.execute(
            f"""
            SELECT
                jobs.*,
                profiles.name AS profile_name,
                CAST(julianday('now') - julianday(jobs.application_date) AS INTEGER) AS days_since_application
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage = 'applied'
            AND jobs.application_date IS NOT NULL
            AND date(jobs.application_date) <= date('now', '-30 days')
            AND (jobs.feedback IS NULL OR jobs.feedback = '')
            AND NOT EXISTS (
                SELECT 1 FROM interviews
                WHERE interviews.job_id = jobs.id
            )
            {profile_clause}
            ORDER BY date(jobs.application_date) ASC, jobs.id ASC
            """,
            params,
        ).fetchall()
        last_scrape = conn.execute(
            """
            SELECT *
            FROM scraper_runs
            WHERE (? OR profile_id = ? OR profile_id IS NULL)
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (1 if include_all_profiles else 0, profile_id),
        ).fetchone()
    return {
        "stage_counts": {row["pipeline_stage"] or "new": row["count"] for row in stage_rows},
        "due_actions": due_rows,
        "top_matches": top_rows,
        "awaiting_feedback": feedback_rows,
        "cleanup_due": cleanup_rows,
        "last_scrape": last_scrape,
        "interview_nudges": get_interview_hygiene_nudges(profile_id, include_all_profiles),
        # A week with zero warm-channel activity is the condition worth naming:
        # the board channel is where applications lose to a more directly matched
        # candidate, and the warm channel is the one where that comparison does
        # not happen (item 6).
        "warm_channel": get_warm_channel_activity(profile_id, include_all_profiles, days=7),
        # Activity is not the same as allocation: a week can contain plenty of
        # hidden-market work while every application still went out cold.
        "channel_mix": get_channel_mix(profile_id, include_all_profiles, days=30),
    }


def get_interview_hygiene_nudges(profile_id=None, include_all_profiles=False, limit=10):
    """Interviews whose date has passed but whose outcome is still blank (item 7).

    Surfaces a non-blocking "how did it go?" prompt so the funnel keeps learning.
    Dismissal is client-side; resolution writes back via interviews:update and/or
    a stage move (which advances the outcome snapshot)."""
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT interviews.id AS interview_id, interviews.job_id, interviews.round_number,
                   interviews.title AS interview_title, interviews.interview_date,
                   interviews.interview_type,
                   jobs.title AS job_title, jobs.company, jobs.pipeline_stage,
                   profiles.name AS profile_name
            FROM interviews
            JOIN jobs ON jobs.id = interviews.job_id
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE (interviews.outcome IS NULL OR TRIM(interviews.outcome) = '')
              AND interviews.interview_date IS NOT NULL
              AND date(interviews.interview_date) < date('now')
              AND jobs.pipeline_stage NOT IN ('rejected', 'archived')
              {profile_clause}
            ORDER BY interviews.interview_date DESC, interviews.id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return rows


def get_activity_stats(profile_id=None, include_all_profiles=False, days=7):
    """Weekly/monthly rollup: the market, the user's applications, and general
    activity — current window plus the previous window so the UI can show deltas.

    All comparisons use SQL datetime('now', offset) so they line up with the
    UTC timestamps the app writes (date_scraped, created_at, etc.)."""
    days = max(1, int(days or 7))
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    event_profile_clause = profile_clause  # both alias the jobs table

    def collect(conn, start_offset, end_offset=None):
        def time_clause(column):
            clause = f"{column} >= datetime('now', ?)"
            if end_offset:
                clause += f" AND {column} < datetime('now', ?)"
            return clause

        def time_params():
            return [start_offset] + ([end_offset] if end_offset else [])

        scraped_where = f"{time_clause('jobs.date_scraped')} {profile_clause}"
        scraped_params = time_params() + params

        scraped = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {scraped_where}", scraped_params
        ).fetchone()[0]
        analyzed = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {scraped_where} AND jobs.match_score IS NOT NULL",
            scraped_params,
        ).fetchone()[0]
        band_rows = conn.execute(
            f"""
            SELECT CASE
                WHEN jobs.match_score IS NULL THEN 'unscored'
                WHEN jobs.match_score >= 78 THEN '78+'
                WHEN jobs.match_score >= 70 THEN '70-77'
                WHEN jobs.match_score >= 60 THEN '60-69'
                WHEN jobs.match_score >= 45 THEN '45-59'
                ELSE '<45'
            END AS band, COUNT(*) AS count
            FROM jobs WHERE {scraped_where}
            GROUP BY band
            """,
            scraped_params,
        ).fetchall()
        band_counts = {row["band"]: row["count"] for row in band_rows}
        bands = [{"band": band, "count": band_counts.get(band, 0)} for band in ("78+", "70-77", "60-69", "45-59", "<45", "unscored")]

        sources = conn.execute(
            f"""
            SELECT jobs.source, COUNT(*) AS count FROM jobs
            WHERE {scraped_where}
            GROUP BY jobs.source ORDER BY count DESC LIMIT 6
            """,
            scraped_params,
        ).fetchall()
        employers = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(jobs.actual_company, ''), jobs.company) AS employer, COUNT(*) AS count
            FROM jobs
            WHERE {scraped_where}
            AND jobs.employer_type = 'direct_employer'
            AND COALESCE(jobs.match_score, 0) >= 60
            AND COALESCE(NULLIF(jobs.actual_company, ''), jobs.company, '') NOT IN ('', 'Unknown')
            GROUP BY employer ORDER BY count DESC LIMIT 6
            """,
            scraped_params,
        ).fetchall()

        applied = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {time_clause('jobs.application_date')} {profile_clause}",
            time_params() + params,
        ).fetchone()[0]
        interviews = conn.execute(
            f"""
            SELECT COUNT(*) FROM interviews JOIN jobs ON jobs.id = interviews.job_id
            WHERE {time_clause("COALESCE(interviews.interview_date, interviews.created_at)")} {event_profile_clause}
            """,
            time_params() + params,
        ).fetchone()[0]

        def event_count(where, extra_params=()):
            return conn.execute(
                f"""
                SELECT COUNT(*) FROM application_events JOIN jobs ON jobs.id = application_events.job_id
                WHERE {time_clause('application_events.created_at')} AND {where} {event_profile_clause}
                """,
                time_params() + list(extra_params) + params,
            ).fetchone()[0]

        offers = event_count("application_events.event_type = 'stage' AND application_events.title LIKE 'Moved to Offer%'")
        docs_generated = event_count("application_events.event_type = 'documents'")
        prompts_generated = event_count("application_events.event_type = 'prompt'")
        auto_rejected = event_count("application_events.title = 'Auto-rejected low match'")
        archived = event_count("application_events.event_type IN ('cleanup', 'retired') OR application_events.title LIKE 'Archived%'")

        stage_moves = conn.execute(
            f"""
            SELECT application_events.title, COUNT(*) AS count
            FROM application_events JOIN jobs ON jobs.id = application_events.job_id
            WHERE {time_clause('application_events.created_at')}
            AND application_events.event_type = 'stage'
            AND application_events.title LIKE 'Moved to %'
            {event_profile_clause}
            GROUP BY application_events.title ORDER BY count DESC LIMIT 8
            """,
            time_params() + params,
        ).fetchall()

        return {
            "scraped": scraped,
            "analyzed": analyzed,
            "bands": bands,
            "top_sources": [{"source": normalize_source(row["source"]), "count": row["count"]} for row in sources],
            "top_employers": [{"employer": row["employer"], "count": row["count"]} for row in employers],
            "applied": applied,
            "interviews": interviews,
            "offers": offers,
            "docs_generated": docs_generated,
            "prompts_generated": prompts_generated,
            "auto_rejected": auto_rejected,
            "archived": archived,
            "stage_moves": [{"title": row["title"], "count": row["count"]} for row in stage_moves],
        }

    with get_db_connection() as conn:
        current = collect(conn, f"-{days} days")
        previous = collect(conn, f"-{days * 2} days", f"-{days} days")
        last_scrape = conn.execute(
            "SELECT started_at, finished_at, status, summary FROM scraper_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    return {
        "window_days": days,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "current": current,
        "previous": previous,
        "last_scrape": {key: last_scrape[key] for key in last_scrape.keys()} if last_scrape else None,
    }


def get_calendar_items(profile_id=None, include_all_profiles=False, days=45):
    profile_clause, params = _profile_filter_clause(profile_id, include_all_profiles)
    with get_db_connection() as conn:
        job_rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.pipeline_stage, jobs.next_action,
                   jobs.next_action_date, jobs.interview_date, jobs.closing_date,
                   NULL AS interview_round, profiles.name AS profile_name
            FROM jobs
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            AND (jobs.match_score IS NULL OR jobs.match_score >= 50)
            AND (
                jobs.next_action_date IS NOT NULL OR
                jobs.closing_date IS NOT NULL
            )
            {profile_clause}
            LIMIT 100
            """,
            params,
        ).fetchall()
        interview_rows = conn.execute(
            f"""
            SELECT jobs.id, jobs.title, jobs.company, jobs.pipeline_stage,
                   COALESCE(interviews.next_action, 'Interview') AS next_action,
                   interviews.next_action_date,
                   interviews.interview_date,
                   jobs.closing_date,
                   interviews.round_number AS interview_round,
                   profiles.name AS profile_name
            FROM interviews
            JOIN jobs ON jobs.id = interviews.job_id
            LEFT JOIN profiles ON profiles.id = jobs.profile_id
            WHERE jobs.pipeline_stage NOT IN ('rejected', 'rejected_by_company', 'archived')
            AND (jobs.match_score IS NULL OR jobs.match_score >= 50)
            AND interviews.interview_date IS NOT NULL
            {profile_clause}
            LIMIT 100
            """,
            params,
        ).fetchall()
    rows = list(job_rows) + list(interview_rows)
    return sorted(
        rows,
        key=lambda row: row["next_action_date"] or row["interview_date"] or row["closing_date"] or "9999-12-31",
    )[:100]

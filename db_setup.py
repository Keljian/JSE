"""Create and migrate the SQLite schema used by the assistant."""
import sqlite3
import sys

from database_manager import DB_FILE, DEFAULT_PROFILE_SETTINGS, extract_job_metadata

PIPELINE_STAGES = (
    "new",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "rejected_by_company",
    "archived",
)


def _add_column(cursor, table, column, definition):
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass

def setup_database():
    conn = sqlite3.connect(DB_FILE)
    # Schema setup/migrations run on every startup; tune the same way as the
    # main connection helper so the DDL pass is not throttled by FULL sync.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    cursor = conn.cursor()
    
    # Create table to store job listings
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            url TEXT NOT NULL UNIQUE,
            description TEXT,
            status TEXT DEFAULT 'new', -- new, approved, rejected, stale, applied
            match_score INTEGER,
            ai_analysis TEXT,
            source TEXT NOT NULL,
            pdf_text TEXT,
            date_scraped TEXT,
            profile_id INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    ''')
    
    # Create profiles table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            resume_path TEXT NOT NULL,
            preferred_location TEXT DEFAULT 'Melbourne VIC',
            seek_location TEXT DEFAULT 'Melbourne VIC',
            linkedin_location TEXT DEFAULT 'Australia',
            work_modes TEXT DEFAULT 'hybrid,remote,wfh,onsite',
            max_pages INTEGER DEFAULT 30,
            default_min_score INTEGER DEFAULT 60,
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')
    
    # Create profile_terms table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profile_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    ''')
    
    # Existing database migrations
    _add_column(cursor, "jobs", "date_scraped", "TEXT")
    _add_column(cursor, "jobs", "profile_id", "INTEGER NOT NULL DEFAULT 1")
    _add_column(cursor, "jobs", "pipeline_stage", "TEXT DEFAULT 'new'")
    _add_column(cursor, "jobs", "closing_date", "TEXT")
    _add_column(cursor, "jobs", "last_interaction_at", "TEXT")
    _add_column(cursor, "jobs", "next_action", "TEXT")
    _add_column(cursor, "jobs", "next_action_date", "TEXT")
    _add_column(cursor, "jobs", "priority", "TEXT DEFAULT 'normal'")
    _add_column(cursor, "jobs", "application_date", "TEXT")
    _add_column(cursor, "jobs", "application_url", "TEXT")
    _add_column(cursor, "jobs", "contact_person", "TEXT")
    _add_column(cursor, "jobs", "contact_email", "TEXT")
    _add_column(cursor, "jobs", "contact_phone", "TEXT")
    _add_column(cursor, "jobs", "resume_used", "TEXT")
    _add_column(cursor, "jobs", "resume_text", "TEXT")
    _add_column(cursor, "jobs", "cover_letter_path", "TEXT")
    _add_column(cursor, "jobs", "cover_letter_text", "TEXT")
    _add_column(cursor, "jobs", "position_description_path", "TEXT")
    _add_column(cursor, "jobs", "position_description_text", "TEXT")
    _add_column(cursor, "jobs", "interview_date", "TEXT")
    _add_column(cursor, "jobs", "interview_type", "TEXT")
    _add_column(cursor, "jobs", "interview_people", "TEXT")
    _add_column(cursor, "jobs", "feedback", "TEXT")
    _add_column(cursor, "jobs", "salary", "TEXT")
    _add_column(cursor, "jobs", "closing_date_source", "TEXT")
    _add_column(cursor, "jobs", "notes", "TEXT")
    _add_column(cursor, "jobs", "retired_reason", "TEXT")
    _add_column(cursor, "jobs", "description_fingerprint", "TEXT")
    _add_column(cursor, "jobs", "analysis_signature", "TEXT")
    _add_column(cursor, "jobs", "updated_at", "TEXT")
    _add_column(cursor, "jobs", "advertiser_company", "TEXT")
    _add_column(cursor, "jobs", "actual_company", "TEXT")
    _add_column(cursor, "jobs", "employer_type", "TEXT")
    _add_column(cursor, "jobs", "company_confidence", "TEXT")
    _add_column(cursor, "jobs", "company_intelligence", "TEXT")
    _add_column(cursor, "jobs", "company_research_updated_at", "TEXT")
    _add_column(cursor, "jobs", "last_seen_at", "TEXT")
    _add_column(cursor, "jobs", "missing_sweeps", "INTEGER DEFAULT 0")
    _add_column(cursor, "profiles", "preferred_location", "TEXT DEFAULT 'Melbourne VIC'")
    _add_column(cursor, "profiles", "seek_location", "TEXT DEFAULT 'Melbourne VIC'")
    _add_column(cursor, "profiles", "linkedin_location", "TEXT DEFAULT 'Australia'")
    _add_column(cursor, "profiles", "work_modes", "TEXT DEFAULT 'hybrid,remote,wfh,onsite'")
    _add_column(cursor, "profiles", "max_pages", "INTEGER DEFAULT 30")
    _add_column(cursor, "profiles", "default_min_score", "INTEGER DEFAULT 60")
    _add_column(cursor, "profiles", "boost_terms", "TEXT")
    _add_column(cursor, "profiles", "penalty_terms", "TEXT")
    _add_column(cursor, "profiles", "resume_triage_summary", "TEXT")
    _add_column(cursor, "profiles", "resume_triage_hash", "TEXT")
    _add_column(cursor, "profiles", "doc_ai_provider", "TEXT DEFAULT 'local'")
    _add_column(cursor, "profiles", "doc_ai_model", "TEXT")
    _add_column(cursor, "profiles", "openai_api_key", "TEXT")
    _add_column(cursor, "profiles", "openai_base_url", "TEXT DEFAULT 'https://api.openai.com/v1'")
    _add_column(cursor, "profiles", "claude_api_key", "TEXT")
    _add_column(cursor, "profiles", "claude_model", "TEXT DEFAULT 'claude-3-5-sonnet-latest'")
    _add_column(cursor, "profiles", "gemini_api_key", "TEXT")
    _add_column(cursor, "profiles", "gemini_model", "TEXT DEFAULT 'gemini-2.5-pro'")
    _add_column(cursor, "profiles", "local_model", "TEXT")
    _add_column(cursor, "profiles", "resume_template_path", "TEXT")
    _add_column(cursor, "profiles", "cover_letter_template_path", "TEXT")
    _add_column(cursor, "profiles", "person_id", "INTEGER DEFAULT 1")
    _add_column(cursor, "profiles", "lane_intent", "TEXT")
    _add_column(cursor, "profiles", "target_titles", "TEXT")
    _add_column(cursor, "profiles", "target_domains", "TEXT")
    _add_column(cursor, "profiles", "seniority", "TEXT")
    _add_column(cursor, "profiles", "must_have_terms", "TEXT")
    _add_column(cursor, "profiles", "avoid_terms", "TEXT")
    _add_column(cursor, "profiles", "document_strategy", "TEXT")
    _add_column(cursor, "profiles", "active", "INTEGER DEFAULT 1")
    # Composite scoring on jobs: match_score is the resume-vs-ad analysis,
    # fragment_score is the lane-fragment-bank alignment, composite_score is
    # the weighted combination the UI sorts by.
    _add_column(cursor, "jobs", "fragment_score", "INTEGER")
    _add_column(cursor, "jobs", "composite_score", "INTEGER")
    _add_column(cursor, "jobs", "fragment_alignment_json", "TEXT")
    _add_column(cursor, "jobs", "fragment_alignment_updated_at", "TEXT")

    cursor.execute(
        """
        UPDATE profiles
        SET preferred_location = COALESCE(preferred_location, ?),
            seek_location = COALESCE(seek_location, ?),
            linkedin_location = COALESCE(linkedin_location, ?),
            work_modes = COALESCE(work_modes, ?),
            max_pages = COALESCE(max_pages, ?),
            default_min_score = COALESCE(default_min_score, ?),
            boost_terms = COALESCE(boost_terms, ?),
            penalty_terms = COALESCE(penalty_terms, ?),
            doc_ai_provider = COALESCE(doc_ai_provider, ?),
            openai_base_url = COALESCE(openai_base_url, ?),
            claude_model = COALESCE(claude_model, ?),
            gemini_model = COALESCE(gemini_model, ?),
            resume_template_path = COALESCE(resume_template_path, ?),
            cover_letter_template_path = COALESCE(cover_letter_template_path, ?)
        """,
        (
            DEFAULT_PROFILE_SETTINGS["preferred_location"],
            DEFAULT_PROFILE_SETTINGS["seek_location"],
            DEFAULT_PROFILE_SETTINGS["linkedin_location"],
            ",".join(DEFAULT_PROFILE_SETTINGS["work_modes"]),
            DEFAULT_PROFILE_SETTINGS["max_pages"],
            DEFAULT_PROFILE_SETTINGS["default_min_score"],
            DEFAULT_PROFILE_SETTINGS["boost_terms"],
            DEFAULT_PROFILE_SETTINGS["penalty_terms"],
            DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
            DEFAULT_PROFILE_SETTINGS["openai_base_url"],
            DEFAULT_PROFILE_SETTINGS["claude_model"],
            DEFAULT_PROFILE_SETTINGS["gemini_model"],
            DEFAULT_PROFILE_SETTINGS["resume_template_path"],
            DEFAULT_PROFILE_SETTINGS["cover_letter_template_path"],
        ),
    )
    # Gemini retired the 1.x/2.0 model families; lanes still pointing at them
    # 404 on every generation call (the API key is shared across lanes, but the
    # model name is per-lane, so lanes the user never re-saved kept dead names).
    # Rewrite retired names to the current default on every launch.
    cursor.execute(
        """
        UPDATE profiles
        SET gemini_model = ?
        WHERE gemini_model IN ('gemini-pro', 'gemini-pro-vision')
           OR gemini_model LIKE 'gemini-1.0%'
           OR gemini_model LIKE 'gemini-1.5%'
           OR gemini_model LIKE 'gemini-2.0%'
        """,
        (DEFAULT_PROFILE_SETTINGS["gemini_model"],),
    )

    cursor.execute(
        """
        UPDATE jobs
        SET source = CASE LOWER(source)
            WHEN 'seek' THEN 'Seek'
            WHEN 'linkedin' THEN 'LinkedIn'
            WHEN 'deakin' THEN 'Deakin University'
            WHEN 'deakin university' THEN 'Deakin University'
            WHEN 'monash' THEN 'Monash University'
            WHEN 'monash university' THEN 'Monash University'
            WHEN 'latrobe' THEN 'LaTrobe University'
            WHEN 'latrobe university' THEN 'LaTrobe University'
            WHEN 'la trobe' THEN 'LaTrobe University'
            WHEN 'la trobe university' THEN 'LaTrobe University'
            WHEN 'swinburne' THEN 'Swinburne University'
            WHEN 'swinburne university' THEN 'Swinburne University'
            WHEN 'knox' THEN 'Knox City Council'
            WHEN 'knox city council' THEN 'Knox City Council'
            WHEN 'maroondah' THEN 'Maroondah City Council'
            WHEN 'maroondah city council' THEN 'Maroondah City Council'
            ELSE source
        END
        WHERE source IS NOT NULL
        """
    )

    cursor.execute("""
        UPDATE jobs
        SET pipeline_stage = CASE
            WHEN status = 'approved' THEN 'interested'
            WHEN status = 'applied' THEN 'applied'
            WHEN status IN ('interview_1', 'interview_2', 'interview_3', 'interviewing') THEN 'interviewing'
            WHEN status = 'rejected' THEN 'rejected'
            WHEN status = 'stale' THEN 'archived'
            ELSE COALESCE(pipeline_stage, 'new')
        END
        WHERE pipeline_stage IS NULL OR pipeline_stage = 'new'
    """)

    cursor.execute("""
        UPDATE jobs
        SET last_interaction_at = COALESCE(last_interaction_at, date_scraped, datetime('now')),
            updated_at = COALESCE(updated_at, datetime('now')),
            last_seen_at = COALESCE(last_seen_at, date_scraped, updated_at, datetime('now')),
            missing_sweeps = COALESCE(missing_sweeps, 0)
    """)

    cursor.execute("""
        UPDATE jobs
        SET status = pipeline_stage
        WHERE pipeline_stage IS NOT NULL
        AND status IN ('approved', 'stale')
    """)

    cursor.execute("""
        UPDATE jobs
        SET pipeline_stage = 'interviewing',
            status = 'interviewing'
        WHERE pipeline_stage IN ('interview_1', 'interview_2', 'interview_3')
           OR status IN ('interview_1', 'interview_2', 'interview_3')
    """)

    cursor.execute("""
        UPDATE jobs
        SET pipeline_stage = 'interested',
            status = 'interested'
        WHERE pipeline_stage = 'docs_drafted'
           OR status = 'docs_drafted'
    """)

    cursor.execute("""
        UPDATE profiles
        SET linkedin_location = COALESCE(NULLIF(preferred_location, ''), NULLIF(seek_location, ''), ?)
        WHERE linkedin_location IS NULL
           OR TRIM(linkedin_location) = ''
           OR LOWER(TRIM(linkedin_location)) = 'australia'
    """, (DEFAULT_PROFILE_SETTINGS["linkedin_location"],))

    cursor.execute("""
        UPDATE jobs
        SET closing_date = date(COALESCE(date_scraped, updated_at, last_interaction_at, datetime('now')), '+14 days')
        WHERE closing_date IS NULL
    """)

    # One-time legacy data backfills are gated behind user_version: add_job()
    # and sync_legacy_job_to_lane_model() keep every new job's metadata and the
    # lane-model tables current, so re-running these whole-table passes on each
    # startup is pure waste (the per-row metadata loop below alone cost ~8s on a
    # large DB, and never "completed" because a missing phone/salary is normal,
    # so the same rows were re-scanned every launch).
    LEGACY_BACKFILL_VERSION = 1
    legacy_backfill_done = cursor.execute("PRAGMA user_version").fetchone()[0] >= LEGACY_BACKFILL_VERSION

    if not legacy_backfill_done:
        cursor.execute("""
            SELECT id, title, company, description, pdf_text
            FROM jobs
            WHERE contact_person IS NULL OR contact_email IS NULL OR contact_phone IS NULL OR salary IS NULL
        """)
        metadata_updates = []
        for row in cursor.fetchall():
            metadata = extract_job_metadata(
                {
                    "title": row[1],
                    "company": row[2],
                    "description": row[3],
                    "pdf_text": row[4],
                }
            )
            metadata_updates.append(
                (
                    metadata.get("contact_person"),
                    metadata.get("contact_email"),
                    metadata.get("contact_phone"),
                    metadata.get("salary"),
                    row[0],
                )
            )
        if metadata_updates:
            cursor.executemany(
                """
                UPDATE jobs
                SET contact_person = COALESCE(contact_person, ?),
                    contact_email = COALESCE(contact_email, ?),
                    contact_phone = COALESCE(contact_phone, ?),
                    salary = COALESCE(salary, ?)
                WHERE id = ?
                """,
                metadata_updates,
            )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT,
            event_date TEXT DEFAULT (datetime('now')),
            due_date TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            title TEXT,
            interview_date TEXT,
            interview_type TEXT,
            people_met TEXT,
            notes TEXT,
            outcome TEXT,
            next_action TEXT,
            next_action_date TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute("""
        INSERT INTO interviews (
            job_id, round_number, title, interview_date, interview_type,
            people_met, notes, next_action, next_action_date
        )
        SELECT
            jobs.id,
            CASE jobs.pipeline_stage
                WHEN 'interview_2' THEN 2
                WHEN 'interview_3' THEN 3
                ELSE 1
            END,
            'Interview',
            jobs.interview_date,
            jobs.interview_type,
            jobs.interview_people,
            jobs.notes,
            jobs.next_action,
            jobs.next_action_date
        FROM jobs
        WHERE (jobs.interview_date IS NOT NULL OR jobs.interview_people IS NOT NULL OR jobs.pipeline_stage = 'interviewing')
        AND NOT EXISTS (
            SELECT 1 FROM interviews
            WHERE interviews.job_id = jobs.id
        )
    """)

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scraper_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER,
            scope TEXT NOT NULL DEFAULT 'profile',
            sources TEXT,
            started_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            summary TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scraper_plugins (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_name TEXT NOT NULL,
            version TEXT,
            enabled INTEGER DEFAULT 1,
            install_type TEXT NOT NULL DEFAULT 'bundled',
            install_path TEXT,
            manifest_json TEXT NOT NULL,
            config_json TEXT,
            installed_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lane_scraper_settings (
            lane_id INTEGER NOT NULL,
            scraper_id TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            config_json TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (lane_id, scraper_id),
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (scraper_id) REFERENCES scraper_plugins(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS company_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_key TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            employer_type TEXT,
            website_domain TEXT,
            intelligence TEXT,
            confidence TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            created_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profile_memory_fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            fragment_type TEXT NOT NULL,
            theme TEXT NOT NULL,
            claim TEXT NOT NULL,
            supporting_detail TEXT,
            skills_json TEXT,
            domains_json TEXT,
            seniority TEXT,
            source_job_ids_json TEXT,
            source_doc_paths_json TEXT,
            reuse_guidance TEXT,
            confidence TEXT DEFAULT 'medium',
            fingerprint TEXT NOT NULL,
            last_seen_at TEXT DEFAULT (datetime('now')),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
            UNIQUE(profile_id, fingerprint)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profile_memory_remine_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL UNIQUE,
            cadence_days INTEGER DEFAULT 7,
            last_remine_at TEXT,
            next_due_at TEXT,
            last_outcome_recompute_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS profile_memory_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            scanned_at TEXT DEFAULT (datetime('now')),
            applications_scanned_count INTEGER DEFAULT 0,
            fragments_upserted_count INTEGER DEFAULT 0,
            newest_application_date TEXT,
            summary TEXT,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS candidate_fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            fragment_type TEXT NOT NULL,
            theme TEXT NOT NULL,
            claim TEXT NOT NULL,
            supporting_detail TEXT,
            skills_json TEXT,
            domains_json TEXT,
            seniority TEXT,
            source_job_ids_json TEXT,
            source_doc_paths_json TEXT,
            reuse_guidance TEXT,
            confidence TEXT DEFAULT 'medium',
            fingerprint TEXT NOT NULL,
            last_seen_at TEXT DEFAULT (datetime('now')),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
            UNIQUE(person_id, fingerprint)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lane_fragment_affinity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lane_id INTEGER NOT NULL,
            fragment_id INTEGER NOT NULL,
            weight REAL DEFAULT 0.5,
            reason TEXT,
            source TEXT DEFAULT 'suggested',
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (fragment_id) REFERENCES candidate_fragments(id) ON DELETE CASCADE,
            UNIQUE(lane_id, fragment_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lane_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lane_id INTEGER NOT NULL,
            term TEXT NOT NULL,
            source TEXT DEFAULT 'generated',
            confidence REAL DEFAULT 0.5,
            performance_score REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE CASCADE,
            UNIQUE(lane_id, term)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_postings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_job_id INTEGER UNIQUE,
            title TEXT NOT NULL,
            company TEXT,
            location TEXT,
            url TEXT NOT NULL UNIQUE,
            description TEXT,
            source TEXT NOT NULL,
            pdf_text TEXT,
            date_scraped TEXT,
            closing_date TEXT,
            closing_date_source TEXT,
            contact_person TEXT,
            contact_email TEXT,
            contact_phone TEXT,
            salary TEXT,
            description_fingerprint TEXT,
            advertiser_company TEXT,
            actual_company TEXT,
            employer_type TEXT,
            company_confidence TEXT,
            company_intelligence TEXT,
            company_research_updated_at TEXT,
            job_intelligence_json TEXT,
            job_intelligence_updated_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS search_runs_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lane_id INTEGER,
            scope TEXT NOT NULL DEFAULT 'lane',
            sources TEXT,
            status TEXT DEFAULT 'running',
            summary TEXT,
            started_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT,
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS search_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_run_id INTEGER,
            lane_id INTEGER NOT NULL,
            job_posting_id INTEGER NOT NULL,
            source TEXT,
            keyword TEXT,
            route_score REAL,
            route_reason TEXT,
            discovered_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (search_run_id) REFERENCES search_runs_v2(id) ON DELETE SET NULL,
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (job_posting_id) REFERENCES job_postings(id) ON DELETE CASCADE,
            UNIQUE(lane_id, job_posting_id, source, keyword)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lane_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_job_id INTEGER UNIQUE,
            lane_id INTEGER NOT NULL,
            job_posting_id INTEGER NOT NULL,
            pipeline_stage TEXT DEFAULT 'new',
            status TEXT DEFAULT 'new',
            match_score INTEGER,
            ai_analysis TEXT,
            analysis_signature TEXT,
            priority TEXT DEFAULT 'normal',
            notes TEXT,
            next_action TEXT,
            next_action_date TEXT,
            application_date TEXT,
            feedback TEXT,
            retired_reason TEXT,
            discovered_at TEXT DEFAULT (datetime('now')),
            last_interaction_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (job_posting_id) REFERENCES job_postings(id) ON DELETE CASCADE,
            UNIQUE(lane_id, job_posting_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_kits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_job_id INTEGER,
            lane_opportunity_id INTEGER,
            lane_id INTEGER NOT NULL,
            job_posting_id INTEGER NOT NULL,
            resume_path TEXT,
            resume_text TEXT,
            cover_letter_path TEXT,
            cover_letter_text TEXT,
            prompt_path TEXT,
            structured_content_path TEXT,
            position_description_path TEXT,
            position_description_text TEXT,
            generated_at TEXT DEFAULT (datetime('now')),
            applied_at TEXT,
            outcome TEXT,
            review_json TEXT,
            review_updated_at TEXT,
            notes TEXT,
            FOREIGN KEY (lane_opportunity_id) REFERENCES lane_opportunities(id) ON DELETE SET NULL,
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (job_posting_id) REFERENCES job_postings(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS local_llm_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            lane_id INTEGER,
            status TEXT DEFAULT 'pending',
            input_hash TEXT,
            output_json TEXT,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY (lane_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS application_kit_fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_kit_id INTEGER NOT NULL,
            fragment_id INTEGER NOT NULL,
            usage_type TEXT,
            weight REAL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (application_kit_id) REFERENCES application_kits(id) ON DELETE CASCADE,
            FOREIGN KEY (fragment_id) REFERENCES candidate_fragments(id) ON DELETE CASCADE,
            UNIQUE(application_kit_id, fragment_id)
        )
    ''')

    # Migrations for tables created above. These must run AFTER the CREATE TABLE
    # statements: on a fresh database the ALTERs would otherwise silently no-op
    # (table missing) and the fragment tables would be created without the
    # activation-metadata columns, breaking every fragment upsert until the
    # next launch re-ran setup.
    _add_column(cursor, "job_postings", "job_intelligence_json", "TEXT")
    _add_column(cursor, "job_postings", "job_intelligence_updated_at", "TEXT")
    _add_column(cursor, "application_kits", "review_json", "TEXT")
    _add_column(cursor, "application_kits", "review_updated_at", "TEXT")

    # Typed fragment fields the LLM produces but the original schema dropped.
    # `keywords` drive activation, `anti_keywords` block misuse, `status`
    # separates established vs emerging, `support_count`/`outcomes_json`/
    # `outcome_score` carry the outcome-weighted confidence signal.
    for _table in ("profile_memory_fragments", "candidate_fragments"):
        _add_column(cursor, _table, "keywords_json", "TEXT")
        _add_column(cursor, _table, "anti_keywords_json", "TEXT")
        _add_column(cursor, _table, "job_families_json", "TEXT")
        _add_column(cursor, _table, "status", "TEXT DEFAULT 'established'")
        _add_column(cursor, _table, "confidence_reasoning", "TEXT")
        _add_column(cursor, _table, "support_count", "INTEGER DEFAULT 1")
        _add_column(cursor, _table, "outcomes_json", "TEXT")
        _add_column(cursor, _table, "outcome_score", "REAL DEFAULT 0")
        _add_column(cursor, _table, "last_outcome_at", "TEXT")
        _add_column(cursor, _table, "reinforces_themes_json", "TEXT")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pipeline_stage ON jobs(pipeline_stage)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_next_action_date ON jobs(next_action_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_profile_stage ON jobs(profile_id, pipeline_stage)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source_seen ON jobs(profile_id, source, last_seen_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_employer_type ON jobs(employer_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_actual_company ON jobs(actual_company)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_application_events_job ON application_events(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_interviews_job ON interviews(job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_interviews_date ON interviews(interview_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_memory_profile ON profile_memory_fragments(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_memory_theme ON profile_memory_fragments(profile_id, theme)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_memory_scans_profile ON profile_memory_scans(profile_id, scanned_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profile_memory_remine_due ON profile_memory_remine_schedule(next_due_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_composite_score ON jobs(composite_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_candidate_fragments_person ON candidate_fragments(person_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_lane_fragment_affinity_lane ON lane_fragment_affinity(lane_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_lane_terms_lane ON lane_terms(lane_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_postings_url ON job_postings(url)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_hits_lane ON search_hits(lane_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_lane_opportunities_lane_stage ON lane_opportunities(lane_id, pipeline_stage)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_application_kits_lane ON application_kits(lane_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_local_llm_tasks_status ON local_llm_tasks(status, task_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_local_llm_tasks_entity ON local_llm_tasks(entity_type, entity_id)")
    
    # Create default "General" profile if it doesn't exist
    cursor.execute("SELECT COUNT(*) FROM profiles")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO profiles (name, resume_path) VALUES (?, ?)",
                       ("General", ""))

    cursor.execute("SELECT COUNT(*) FROM people")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO people (id, name, contact_json) VALUES (1, ?, ?)",
            (
                "Candidate",
                '{"source":"config","note":"Default candidate created by lane migration."}',
            ),
        )
    cursor.execute("UPDATE profiles SET person_id = COALESCE(person_id, 1), active = COALESCE(active, 1)")

    # Continue the one-time legacy -> lane-model port (gated above via
    # legacy_backfill_done). These INSERT ... SELECT FROM jobs passes are kept by
    # add_job()/sync_legacy_job_to_lane_model() thereafter, so they only run once.
    if not legacy_backfill_done:
        cursor.execute('''
            INSERT OR IGNORE INTO candidate_fragments (
                person_id, fragment_type, theme, claim, supporting_detail, skills_json,
                domains_json, seniority, source_job_ids_json, source_doc_paths_json,
                reuse_guidance, confidence, fingerprint, last_seen_at, created_at, updated_at
            )
            SELECT
                COALESCE(profiles.person_id, 1), profile_memory_fragments.fragment_type,
                profile_memory_fragments.theme, profile_memory_fragments.claim,
                profile_memory_fragments.supporting_detail, profile_memory_fragments.skills_json,
                profile_memory_fragments.domains_json, profile_memory_fragments.seniority,
                profile_memory_fragments.source_job_ids_json, profile_memory_fragments.source_doc_paths_json,
                profile_memory_fragments.reuse_guidance, profile_memory_fragments.confidence,
                profile_memory_fragments.fingerprint, profile_memory_fragments.last_seen_at,
                profile_memory_fragments.created_at, profile_memory_fragments.updated_at
            FROM profile_memory_fragments
            JOIN profiles ON profiles.id = profile_memory_fragments.profile_id
        ''')

        cursor.execute('''
            INSERT OR IGNORE INTO lane_fragment_affinity (lane_id, fragment_id, weight, reason, source)
            SELECT
                profile_memory_fragments.profile_id,
                candidate_fragments.id,
                0.9,
                'Backfilled from existing profile memory.',
                'migration'
            FROM profile_memory_fragments
            JOIN profiles ON profiles.id = profile_memory_fragments.profile_id
            JOIN candidate_fragments
              ON candidate_fragments.person_id = COALESCE(profiles.person_id, 1)
             AND candidate_fragments.fingerprint = profile_memory_fragments.fingerprint
        ''')

        cursor.execute('''
            INSERT OR IGNORE INTO lane_terms (lane_id, term, source, confidence)
            SELECT profile_id, keyword, 'legacy_profile_terms', 0.7
            FROM profile_terms
        ''')

        cursor.execute('''
            INSERT OR IGNORE INTO job_postings (
                legacy_job_id, title, company, location, url, description, source, pdf_text,
                date_scraped, closing_date, closing_date_source, contact_person, contact_email,
                contact_phone, salary, description_fingerprint, advertiser_company, actual_company,
                employer_type, company_confidence, company_intelligence, company_research_updated_at,
                job_intelligence_json, job_intelligence_updated_at, created_at, updated_at
            )
            SELECT
                id, title, company, location, url, description, source, pdf_text,
                date_scraped, closing_date, closing_date_source, contact_person, contact_email,
                contact_phone, salary, description_fingerprint, advertiser_company, actual_company,
                employer_type, company_confidence, company_intelligence, company_research_updated_at,
                NULL, NULL,
                COALESCE(date_scraped, updated_at, last_interaction_at, datetime('now')),
                COALESCE(updated_at, last_interaction_at, date_scraped, datetime('now'))
            FROM jobs
        ''')

        cursor.execute('''
            INSERT OR IGNORE INTO lane_opportunities (
                legacy_job_id, lane_id, job_posting_id, pipeline_stage, status, match_score,
                ai_analysis, analysis_signature, priority, notes, next_action, next_action_date,
                application_date, feedback, retired_reason, discovered_at, last_interaction_at, updated_at
            )
            SELECT
                jobs.id, jobs.profile_id, job_postings.id,
                COALESCE(jobs.pipeline_stage, jobs.status, 'new'),
                COALESCE(jobs.status, jobs.pipeline_stage, 'new'),
                jobs.match_score, jobs.ai_analysis, jobs.analysis_signature,
                COALESCE(jobs.priority, 'normal'), jobs.notes, jobs.next_action,
                jobs.next_action_date, jobs.application_date, jobs.feedback,
                jobs.retired_reason, COALESCE(jobs.date_scraped, jobs.updated_at, datetime('now')),
                COALESCE(jobs.last_interaction_at, jobs.updated_at, datetime('now')),
                COALESCE(jobs.updated_at, jobs.last_interaction_at, datetime('now'))
            FROM jobs
            JOIN job_postings ON job_postings.legacy_job_id = jobs.id
        ''')

        cursor.execute('''
            INSERT INTO application_kits (
                legacy_job_id, lane_opportunity_id, lane_id, job_posting_id,
                resume_path, resume_text, cover_letter_path, cover_letter_text,
                position_description_path, position_description_text, generated_at,
                applied_at, outcome, notes
            )
            SELECT
                jobs.id, lane_opportunities.id, jobs.profile_id, job_postings.id,
                jobs.resume_used, jobs.resume_text, jobs.cover_letter_path, jobs.cover_letter_text,
                jobs.position_description_path, jobs.position_description_text,
                COALESCE(jobs.updated_at, jobs.last_interaction_at, datetime('now')),
                jobs.application_date, jobs.status, 'Backfilled from legacy job document fields.'
            FROM jobs
            JOIN job_postings ON job_postings.legacy_job_id = jobs.id
            JOIN lane_opportunities ON lane_opportunities.legacy_job_id = jobs.id
            WHERE (NULLIF(jobs.resume_used, '') IS NOT NULL OR NULLIF(jobs.cover_letter_path, '') IS NOT NULL)
            AND NOT EXISTS (
                SELECT 1 FROM application_kits
                WHERE application_kits.legacy_job_id = jobs.id
                  AND COALESCE(application_kits.resume_path, '') = COALESCE(jobs.resume_used, '')
                  AND COALESCE(application_kits.cover_letter_path, '') = COALESCE(jobs.cover_letter_path, '')
            )
        ''')

        cursor.execute(f"PRAGMA user_version = {LEGACY_BACKFILL_VERSION}")

    conn.commit()
    conn.close()
    print("Database 'job_applications.db' is ready.", file=sys.stderr)

if __name__ == '__main__':
    setup_database()

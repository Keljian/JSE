"""Business workflow orchestration for searches, analysis, and applications.

The Electron bridge calls these functions for long-running work. This layer
coordinates keyword generation, scraper execution, LLM analysis, database
updates, and cancellation-aware progress logging.
"""
import json
import re
from datetime import datetime
import llm_handler
import scraper_dispatcher
import scraper_plugins
import database_manager as db
import region
from concurrency import OperationCancelledError, cancel_event
import concurrent.futures

class LogicError(Exception):
    pass

def _lane_location(profile_id, search_settings=None):
    """The lane's search location, for region.use_location (see region.py)."""
    try:
        settings = search_settings or db.get_lane_settings(profile_id) or {}
        return settings.get("preferred_location") or region.active_location()
    except Exception:
        return region.active_location()


def fallback_search_terms(profile_id, search_settings=None):
    """Search terms for a lane that has none saved, without the LLM.

    Uses the lane's target titles, then its name. The scrape uses these for the
    one sweep only; analysis generates and saves proper terms when it next runs.
    """
    try:
        settings = search_settings or db.get_lane_settings(profile_id) or {}
    except Exception:
        settings = {}
    terms, seen = [], set()
    for chunk in re.split(r"[,;\n]+", str(settings.get("target_titles") or "")):
        clean = re.sub(r"\s+", " ", chunk).strip(" -*\u2022\t")
        if len(clean) > 2 and clean.casefold() not in seen:
            terms.append(clean)
            seen.add(clean.casefold())
    if not terms:
        lane = db.get_profile_by_id(profile_id)
        name = (lane["name"] if lane else "") or ""
        if name.strip():
            terms.append(name.strip())
    return terms


def execute_keyword_generation(optimism, resume_text, log_callback, profile_id=1):
    # Search titles follow the lane's market: a Manchester lane gets UK titles.
    with region.use_location(_lane_location(profile_id)):
        return _execute_keyword_generation(optimism, resume_text, log_callback, profile_id)


def _execute_keyword_generation(optimism, resume_text, log_callback, profile_id=1):
    """Generates and saves search terms for a lane using candidate fragments and lane definition."""
    log_callback("Asking Unsloth Studio to generate lane search terms...")
    try:
        context = db.build_lane_context(profile_id, include_terms=False, include_fragments=True)
        lane = context.get("lane") or {}
        settings = context.get("settings") or {}
        fragments = context.get("fragments") or []
        fragment_lines = []
        for fragment in fragments[:40]:
            fragment_lines.append(
                f"- {fragment.get('theme')}: {fragment.get('claim')} "
                f"Skills={fragment.get('skills_json') or ''} Domains={fragment.get('domains_json') or ''}"
            )
        lane_context = f"""
LANE / SEARCH FOCUS:
Name: {lane.get('name') or ''}
Intent: {settings.get('lane_intent') or ''}
Target titles: {settings.get('target_titles') or ''}
Target domains: {settings.get('target_domains') or ''}
Seniority: {settings.get('seniority') or ''}
Must-have signals: {settings.get('must_have_terms') or settings.get('boost_terms') or ''}
Avoid signals: {settings.get('avoid_terms') or settings.get('penalty_terms') or ''}

RELEVANT CANDIDATE FRAGMENTS:
{chr(10).join(fragment_lines) if fragment_lines else 'No shared candidate fragments are available yet.'}

{llm_handler.resolve_positioning_doctrine(settings)}

BASE RESUME:
{resume_text}
"""
    except Exception as exc:
        log_callback(f"Lane context unavailable; falling back to resume-only term generation: {exc}")
        lane_context = resume_text
    
    llm_response = llm_handler.derive_search_terms_from_resume(optimism, lane_context)
    log_callback(f"LLM returned: {llm_response[:100]}...")
    
    match = re.search(r'(\[.*?\])', llm_response, re.DOTALL)
    if not match:
        raise LogicError("Could not find a valid list in the LLM's response.")
        
    json_string = match.group(1)
    raw_data = json.loads(json_string)
    keywords = [item['title'] if isinstance(item, dict) else item for item in raw_data if isinstance(item, (dict, str))]
    sanitized = []
    seen = set()
    for kw in keywords:
        if not isinstance(kw, str):
            continue
        clean = re.sub(r"\s+", " ", kw).strip()
        key = clean.casefold()
        if len(clean) > 3 and key not in seen:
            sanitized.append(clean)
            seen.add(key)
    
    if not sanitized:
        raise LogicError("LLM list was empty or in an unrecognized format.")

    db.save_lane_terms(profile_id, sanitized, source="lane_context", confidence=0.8)
        
    log_callback("Search terms saved successfully.")
    return sanitized

def _run_scraper_task(source, keyword, resume_text, status_callback, log_callback, profile_id=1, search_settings=None):
    """Wrapper function for running a single scraper task in a thread."""
    if cancel_event.is_set(): return None

    status_callback(f"Scraping '{keyword}' from {source}...", True)
    search = scraper_dispatcher.run_scraper_search(
        source,
        keyword,
        status_callback,
        log_callback,
        profile_id=profile_id,
        search_settings=search_settings,
    )

    # The retry logic is now handled in the main thread after the futures complete.
    return {
        'source': source,
        'keyword': keyword,
        'success': search["saved"],
        'outcome': search["outcome"],
        'listings': search["listings"],
    }


# Seniority and level words a board's own search treats as a hard filter. Dropping
# them is the one broadening that is safe to do without judgement; anything
# subtler (which qualifier is the specialised one) is left to analysis, which
# stores an LLM-suggested broader term for the next sweep.
_LEVEL_WORDS = re.compile(
    r"\b(senior|snr|sr|lead|principal|junior|jnr|jr|graduate|grad|entry[- ]level|"
    r"experienced|trainee|level\s*\d+|i{1,3}|iv)\b\.?",
    re.IGNORECASE,
)


def broaden_search_term(term):
    """A broader version of a search term, without the LLM. None if there is none."""
    original = re.sub(r"\s+", " ", str(term or "")).strip()
    broader = re.sub(r"[\(\[].*?[\)\]]", " ", original)
    broader = re.split(r"\s+[-|\u2013\u2014]\s+", broader)[0]
    broader = _LEVEL_WORDS.sub(" ", broader)
    broader = re.sub(r"\s+", " ", broader).strip(" ,/-")
    if not broader or broader.casefold() == original.casefold():
        return None
    return broader


def _search_found_listings(result):
    """Whether a search proves its source is up, for missing-job accounting.

    A source that returned nothing (an outage, a bad location) must not count
    as a completed sweep, or every unreviewed job from it ticks toward being
    archived as "no longer listed".
    """
    return bool(result) and result.get('outcome') in (scraper_dispatcher.SAVED, scraper_dispatcher.NO_NEW)


def _retry_term_for(task, profile_id):
    """The term to retry an empty search with: analysis' stored suggestion first."""
    try:
        suggested = db.get_search_term_suggestion(profile_id, task['keyword'])
    except Exception:
        suggested = None
    if suggested and suggested.casefold() != task['keyword'].casefold():
        return suggested, "suggested by analysis"
    broader = broaden_search_term(task['keyword'])
    return (broader, "seniority words dropped") if broader else (None, None)

def execute_scraping_and_analysis(keywords, sources, resume_text, status_callback, log_callback, update_keywords_callback=None, live_analysis_stop_event=None, profile_id=1, search_settings=None, progress_callback=None):
    """
    Runs scrapers concurrently for multiple sources and handles retries.

    `progress_callback(current, total, phase=…, detail=…)` reports countable
    progress for the UI. The unit is one (source, keyword) scraper task. Total
    is unknown until the fan-out is submitted, so the first call passes
    total=None and the UI shows an indeterminate state rather than a
    percentage it would have to invent.
    """
    report = progress_callback or (lambda *args, **kwargs: None)
    report(0, None, phase="preparing", detail="Cleaning up duplicates…")
    db.dedupe_database(log_callback)
    
    keyword_independent_scrapers = {
        source for source in sources
        if scraper_plugins.plugin_mode(source, profile_id=profile_id) == "sweep"
    }
    failed_tasks = []
    completed_sweep_sources = set()
    total_tasks = 0
    completed_tasks = 0
    sweep_started_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    search_settings = search_settings or db.get_lane_settings(profile_id)
    if search_settings:
        log_callback(
            "Search settings: "
            f"default location '{search_settings.get('preferred_location')}', "
            f"work modes {', '.join(search_settings.get('work_modes') or [])}."
        )
    
    # Use a ThreadPoolExecutor to run scrapers concurrently.
    # Workers are capped to avoid overwhelming the system or getting IP-banned.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        
        # --- Submit all initial tasks ---
        for source in sources:
            if cancel_event.is_set(): break
            
            if source in keyword_independent_scrapers:
                task_keyword = source # Use source name as placeholder keyword
                future = executor.submit(_run_scraper_task, source, task_keyword, resume_text, status_callback, log_callback, profile_id, search_settings)
                futures.append(future)
                total_tasks += 1
            else:
                for keyword in keywords:
                    if cancel_event.is_set(): break
                    future = executor.submit(_run_scraper_task, source, keyword, resume_text, status_callback, log_callback, profile_id, search_settings)
                    futures.append(future)
                    total_tasks += 1

        # Every task is queued by now, so the total is final and the UI can
        # switch from indeterminate to a real bar.
        report(0, total_tasks, phase="scraping", detail=f"Searching {len(sources)} source(s)…")

        # --- Process results as they complete ---
        for future in concurrent.futures.as_completed(futures):
            if cancel_event.is_set(): break
            try:
                result = future.result()
                completed_tasks += 1
                report(
                    completed_tasks,
                    total_tasks,
                    phase="scraping",
                    detail=f"{result['source']}: '{result['keyword']}'" if result else None,
                )
                # Only a search that found no listings at all says anything
                # about the term. Duplicate-only or rate-limited searches did
                # find the market; retrying them with a broader term was noise.
                if result and result.get('outcome') == scraper_dispatcher.EMPTY:
                    # Don't retry keyword-independent scrapers
                    if result['source'] not in keyword_independent_scrapers:
                         failed_tasks.append(result)
                if _search_found_listings(result):
                    completed_sweep_sources.add(result['source'])
            except Exception as e:
                log_callback(f"A scraper thread generated an exception: {e}")

    # --- Retry searches that found nothing. No LLM here: a scrape has to work
    # with the endpoint down. Each empty term is recorded; analysis later asks
    # the LLM for a broader one and stores it, and the retry below uses that
    # stored term, or failing that drops seniority words.
    for task in failed_tasks:
        try:
            db.record_empty_search(profile_id, task['keyword'], task['source'])
        except Exception as exc:
            log_callback(f"Could not record empty search '{task['keyword']}': {exc}")
    retry_plan = []
    for task in failed_tasks:
        new_keyword, reason = _retry_term_for(task, profile_id)
        if new_keyword:
            retry_plan.append((task, new_keyword, reason))
        else:
            log_callback(
                f"'{task['keyword']}' on {task['source']} found no listings. "
                "No broader term yet; analysis will suggest one for the next search."
            )
    if retry_plan and not cancel_event.is_set():
        log_callback(f"\n--- Retrying {len(retry_plan)} empty searches with broader terms... ---")

        # Retries are extra units of work, so grow the total rather than
        # letting the bar sit at 100% while the retry pass still runs.
        total_tasks += len(retry_plan)
        report(completed_tasks, total_tasks, phase="retrying",
               detail=f"Retrying {len(retry_plan)} empty search(es) with broader terms…")

        def _retry_failed_task(task, new_keyword, reason):
            if cancel_event.is_set():
                return None
            log_callback(
                f"'{task['keyword']}' on {task['source']} found no listings. "
                f"Retrying as '{new_keyword}' ({reason})."
            )
            # The broader term is used for this retry only. Saving it over the
            # lane's terms let one empty search rewrite a curated list, and
            # repeated retries collapsed several terms into the same generic one.
            return _run_scraper_task(task['source'], new_keyword, resume_text, status_callback, log_callback, profile_id, search_settings)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as retry_executor:
            retry_futures = [retry_executor.submit(_retry_failed_task, *planned) for planned in retry_plan]

            # Wait for retries to complete
            for future in concurrent.futures.as_completed(retry_futures):
                 if cancel_event.is_set(): break
                 try:
                     result = future.result()
                     if _search_found_listings(result):
                         completed_sweep_sources.add(result['source'])
                 except Exception as e:
                     log_callback(f"A retry scraper thread generated an exception: {e}")
                 completed_tasks += 1
                 report(completed_tasks, total_tasks, phase="retrying")
                 log_callback("Retry task finished.")

    log_callback("\n--- Main scraping tasks complete. ---")
    report(completed_tasks, total_tasks, phase="finishing", detail="Tidying up the sweep…")
    if completed_sweep_sources and not cancel_event.is_set():
        retired = db.mark_missing_new_jobs_after_sweep(
            profile_id,
            completed_sweep_sources,
            sweep_started_at,
            threshold=3,
            log_callback=log_callback,
        )
        if retired.get("incremented") and not retired.get("archived"):
            log_callback(
                f"Marked {retired['incremented']} unseen new job(s) as missing from this sweep. "
                "They will be archived after 3 consecutive misses."
            )
    if live_analysis_stop_event:
        live_analysis_stop_event.set() # Signal the live analysis thread to stop
        log_callback("Live analysis thread has been signalled to stop.")


def _scoring_endpoint_unreachable():
    """True when job matching runs locally and nothing is listening there.

    A refused connection is the common case (the server isn't started), and
    checking it takes milliseconds where the request path retries with backoff.
    Hosted providers are not probed.
    """
    import socket
    from urllib.parse import urlparse
    from llm import providers as llm_providers
    try:
        settings = db.get_app_settings() or {}
        if (settings.get("scoring_ai_provider") or "local").lower() != "local":
            return False
        parsed = urlparse(llm_providers._local_ai_settings()["base_url"])
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:
        return False
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=2):
            return False
    except OSError:
        return True


def prepare_lane_search_terms(profile_id, resume_text, log_callback, optimism=3):
    """The LLM side of searching, run from analysis rather than the scrape.

    Generates terms for a lane that has none, and stores a broader term for each
    search the scrape recorded as finding no listings, so the next scrape can
    retry with it. Never raises: analysis carries on without it.
    """
    try:
        has_terms = bool(db.get_profile_terms(profile_id))
        pending = db.get_empty_searches_needing_suggestion(profile_id)
    except Exception as exc:
        log_callback(f"Search-term upkeep skipped: {exc}")
        return {"generated": 0, "suggested": 0}
    if has_terms and not pending:
        return {"generated": 0, "suggested": 0}
    if _scoring_endpoint_unreachable():
        log_callback("Search-term upkeep skipped: the LLM endpoint is not reachable.")
        return {"generated": 0, "suggested": 0}

    generated = 0
    if not has_terms and resume_text:
        try:
            log_callback("Lane has no saved search terms. Generating them...")
            generated = len(execute_keyword_generation(optimism, resume_text, log_callback, profile_id) or [])
        except OperationCancelledError:
            raise
        except Exception as exc:
            log_callback(f"Search-term generation failed: {exc}")

    suggested = 0
    with region.use_location(_lane_location(profile_id)):
        for term in pending:
            if cancel_event.is_set():
                raise OperationCancelledError("Operation cancelled.")
            new_term = llm_handler.generalize_search_term(term, resume_text or "")
            if new_term and new_term.casefold() != term.casefold():
                db.save_search_term_suggestion(profile_id, term, new_term)
                suggested += 1
                log_callback(f"Empty search '{term}': the next search will retry it as '{new_term}'.")
            elif _scoring_endpoint_unreachable():
                # generalize_search_term swallows errors and returns the term
                # unchanged; leave the rest pending for the next analysis.
                log_callback("LLM endpoint went away; remaining empty searches left for the next analysis.")
                break
            else:
                db.save_search_term_suggestion(profile_id, term, None)
    return {"generated": generated, "suggested": suggested}


def execute_live_analysis(stop_event, resume_text, log_callback, profile_id=1):
    """
    Periodically checks for and analyzes new jobs until a stop signal is received.
    """
    log_callback("Live analysis thread started.")
    while not stop_event.is_set():
        try:
            # Check stop event before starting analysis
            if stop_event.is_set():
                break
                
            log_callback("Live analysis: Checking for new jobs to analyze...")
            
            try:
                llm_handler.analyze_jobs(
                    log_callback=log_callback, resume_text=resume_text,
                    re_analyze=False, status_filter='new', profile_id=profile_id
                )
            except OperationCancelledError:
                log_callback("Live analysis interrupted by cancellation.")
                break
            except Exception as e:
                log_callback(f"Error during live analysis: {e}")
            
            log_callback("Live analysis: Waiting for 30 seconds or stop signal.")
            # wait() returns True if the event is set, False on timeout
            if stop_event.wait(timeout=30):
                break # Exit loop if stop event is set during wait

        except OperationCancelledError:
            log_callback("Live analysis thread received cancellation request.")
            break
        except Exception as e:
            log_callback(f"Error in live analysis loop: {e}")
            # Wait before retrying after an error
            if stop_event.wait(timeout=30):
                 break
    
    log_callback("Live analysis thread finished.")

def run_analysis_on_existing(resume_text, re_analyze, status_filter, log_callback, profile_id=1, progress_callback=None):
    """Analyzes existing jobs in the database based on a filter."""
    llm_handler.analyze_jobs(
        log_callback=log_callback, resume_text=resume_text,
        re_analyze=re_analyze, status_filter=status_filter, profile_id=profile_id,
        progress_callback=progress_callback,
    )

def run_analysis_on_specific_jobs(job_ids, resume_text, log_callback, profile_id=1, progress_callback=None):
    """Analyzes a specific list of jobs by their IDs."""
    llm_handler.analyze_specific_jobs(
        job_ids=job_ids, log_callback=log_callback, resume_text=resume_text, profile_id=profile_id,
        progress_callback=progress_callback,
    )

def run_application_engine_prep(
    job_id,
    resume_text,
    log_callback,
    profile_id=1,
    position_description_text="",
    position_description_path=None,
):
    """Generates tailored documents and prepares data for the Application Engine window."""
    log_callback("Generating application documents with LLM...")
    tailored_resume, cover_letter = llm_handler.generate_application_documents(
        resume_text,
        job_id,
        log_callback,
        profile_id=profile_id,
        position_description_text=position_description_text,
    )
    
    if cancel_event.is_set():
        raise OperationCancelledError("Operation cancelled.")
        
    log_callback("Documents generated. Launching Application Engine UI...")
    
    return {
        "job_id": job_id,
        "tailored_resume": tailored_resume,
        "cover_letter": cover_letter,
        "position_description_path": position_description_path,
    }

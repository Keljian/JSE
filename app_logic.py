"""Business workflow orchestration for searches, analysis, and applications.

The Electron bridge calls these functions for long-running work. This layer
coordinates keyword generation, scraper execution, LLM analysis, database
updates, and cancellation-aware progress logging.
"""
import json
import re
import threading
from datetime import datetime
import llm_handler
import scraper_dispatcher
import scraper_plugins
import database_manager as db
from concurrency import OperationCancelledError, cancel_event
import concurrent.futures

class LogicError(Exception):
    pass

def execute_keyword_generation(optimism, resume_text, log_callback, profile_id=1):
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
    success = scraper_dispatcher.run_scraper_for_keyword(
        source,
        keyword,
        status_callback,
        log_callback,
        profile_id=profile_id,
        search_settings=search_settings,
    )
    
    # The retry logic is now handled in the main thread after the futures complete.
    return {'source': source, 'keyword': keyword, 'success': success}

def execute_scraping_and_analysis(keywords, sources, resume_text, status_callback, log_callback, update_keywords_callback=None, live_analysis_stop_event=None, profile_id=1, search_settings=None):
    """
    Runs scrapers concurrently for multiple sources and handles retries.
    """
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

        # --- Process results as they complete ---
        for future in concurrent.futures.as_completed(futures):
            if cancel_event.is_set(): break
            try:
                result = future.result()
                completed_tasks += 1
                if result and not result['success']:
                    # Don't retry keyword-independent scrapers
                    if result['source'] not in keyword_independent_scrapers:
                         failed_tasks.append(result)
                if result:
                    completed_sweep_sources.add(result['source'])
            except Exception as e:
                log_callback(f"A scraper thread generated an exception: {e}")

    # --- Handle failures concurrently: each worker generalizes its own keyword
    # with the LLM, then re-runs the scraper, so one slow LLM call cannot
    # serialize the whole retry pass. The shared keyword list is lock-guarded.
    if failed_tasks and not cancel_event.is_set() and resume_text:
        log_callback(f"\n--- Retrying {len(failed_tasks)} failed searches with new keywords... ---")

        current_keywords = list(keywords)
        keywords_lock = threading.Lock()

        def _retry_failed_task(task):
            if cancel_event.is_set():
                return None
            log_callback(f"'{task['keyword']}' on {task['source']} yielded no results. Asking LLM for a better term...")
            new_keyword = llm_handler.generalize_search_term(task['keyword'], resume_text)
            if not new_keyword or new_keyword.lower() == task['keyword'].lower():
                return None
            log_callback(f"LLM suggested '{new_keyword}'. Retrying on {task['source']}.")

            # Update keywords list
            if update_keywords_callback:
                with keywords_lock:
                    try:
                        idx = current_keywords.index(task['keyword'])
                        current_keywords[idx] = new_keyword
                        update_keywords_callback(list(current_keywords))
                        db.save_profile_terms(profile_id, current_keywords)
                    except ValueError:
                        pass # Original keyword might have already been replaced

            return _run_scraper_task(task['source'], new_keyword, resume_text, status_callback, log_callback, profile_id, search_settings)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as retry_executor:
            retry_futures = [retry_executor.submit(_retry_failed_task, task) for task in failed_tasks]

            # Wait for retries to complete
            for future in concurrent.futures.as_completed(retry_futures):
                 if cancel_event.is_set(): break
                 try:
                     result = future.result()
                     if result:
                         completed_sweep_sources.add(result['source'])
                 except Exception as e:
                     log_callback(f"A retry scraper thread generated an exception: {e}")
                 log_callback("Retry task finished.")

    log_callback("\n--- Main scraping tasks complete. ---")
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

def run_analysis_on_existing(resume_text, re_analyze, status_filter, log_callback, profile_id=1):
    """Analyzes existing jobs in the database based on a filter."""
    llm_handler.analyze_jobs(
        log_callback=log_callback, resume_text=resume_text,
        re_analyze=re_analyze, status_filter=status_filter, profile_id=profile_id
    )

def run_analysis_on_specific_jobs(job_ids, resume_text, log_callback, profile_id=1):
    """Analyzes a specific list of jobs by their IDs."""
    llm_handler.analyze_specific_jobs(
        job_ids=job_ids, log_callback=log_callback, resume_text=resume_text, profile_id=profile_id
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

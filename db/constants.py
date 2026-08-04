"""Shared vocabularies, thresholds, and term lists.

Split out of database_manager.py, which re-exports everything here.
"""
from .connection import (
    APP_ROOT,
    DATA_DIR,
)

DEFAULT_APP_SETTINGS = {
    "settings_dir": str(DATA_DIR),
    "applications_dir": str(APP_ROOT / "applications"),
    "older_applications_dir": str(APP_ROOT / "older_applications"),
    "onboarding_completed": False,
    "onboarding_version": 0,
}


PIPELINE_STAGES = [
    "new",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "rejected_by_company",
    "archived",
]


ACTIVE_PRE_APPLICATION_STAGES = ["interested"]


APPLIED_EMPLOYER_DECLINE_DAYS = 50


# Jobs analysed below this score are auto-rejected out of the active pipeline.
# Relaxed June 2026 (was 50) — keep aligned with llm_handler.TRIAGE_KEEP_THRESHOLD.
AUTO_REJECT_THRESHOLD = 45


WORK_MODE_OPTIONS = ["hybrid", "remote", "wfh", "onsite"]


DEFAULT_PROFILE_SETTINGS = {
    "preferred_location": "Melbourne VIC",
    "seek_location": "Melbourne VIC",
    "linkedin_location": "Melbourne VIC",
    "work_modes": ["hybrid", "remote", "wfh", "onsite"],
    "max_pages": 30,
    "default_min_score": 60,
    "boost_terms": "",
    "penalty_terms": "",
    "doc_ai_provider": "local",
    "doc_ai_model": "",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "claude_api_key": "",
    "claude_model": "claude-sonnet-4-6",
    "gemini_api_key": "",
    "gemini_model": "gemini-3.1-pro-preview",
    "local_base_url": "http://localhost:1234/v1",
    "local_api_key": "",
    "local_model": "",
    "resume_template_path": "",
    "cover_letter_template_path": "",
    "lane_intent": "",
    "target_titles": "",
    "target_domains": "",
    "seniority": "",
    "must_have_terms": "",
    "avoid_terms": "",
    "document_strategy": "",
    # Blank means the lane is scored against the global positioning doctrine in
    # llm.prompts. Set it per lane when the lane's market is not the candidate's
    # primary one.
    "positioning_doctrine": "",
    "active": 1,
}


# API keys are account-level credentials, not per-lane preferences. A key entered
# on any lane is shared by every lane (see _get_global_credentials / propagation
# in update_profile_settings) so document generation works regardless of which
# lane is active.
GLOBAL_CREDENTIAL_FIELDS = ("openai_api_key", "claude_api_key", "gemini_api_key", "local_api_key")


DEFAULT_APP_SETTINGS.update({
    "doc_ai_provider": DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
    # Blank document/research values inherit the legacy provider until the user
    # makes an explicit per-workflow selection. Memory remains local by default.
    "document_ai_provider": "",
    "research_ai_provider": "",
    "memory_ai_provider": DEFAULT_PROFILE_SETTINGS["doc_ai_provider"],
    "doc_ai_model": DEFAULT_PROFILE_SETTINGS["doc_ai_model"],
    "openai_api_key": "",
    "openai_base_url": DEFAULT_PROFILE_SETTINGS["openai_base_url"],
    "claude_api_key": "",
    "claude_model": DEFAULT_PROFILE_SETTINGS["claude_model"],
    "gemini_api_key": "",
    "gemini_model": DEFAULT_PROFILE_SETTINGS["gemini_model"],
    "local_base_url": DEFAULT_PROFILE_SETTINGS["local_base_url"],
    "local_api_key": "",
    "local_model": DEFAULT_PROFILE_SETTINGS["local_model"],
    # Job-matching (triage/scoring/analysis) provider. Defaults to local so
    # behaviour is unchanged; scoring_model is an independent model override for
    # this workflow. Free / OpenAI-compatible endpoint credentials (Groq,
    # Cerebras, OpenRouter, OpenCode Zen, custom) live under compat_*.
    "scoring_ai_provider": "local",
    "scoring_model": "",
    "compat_base_url": "",
    "compat_api_key": "",
    "compat_model": "",
    # Max simultaneous LLM requests: sizes the analysis worker pool and the
    # shared LLM concurrency gate (clamped 1-8 in llm_handler). Default 1 so a
    # single-slot local server is never sent overlapping requests (it answers
    # HTTP 429). Stored as text like every other app setting.
    "analysis_workers": "1",
})


SOURCE_ALIASES = {
    "seek": "Seek",
    "seek.com.au": "Seek",
    "linkedin": "LinkedIn",
    "deakin": "Deakin University",
    "deakin university": "Deakin University",
    "monash": "Monash University",
    "monash university": "Monash University",
    "latrobe": "LaTrobe University",
    "latrobe university": "LaTrobe University",
    "la trobe": "LaTrobe University",
    "la trobe university": "LaTrobe University",
    "swinburne": "Swinburne University",
    "swinburne university": "Swinburne University",
    "knox": "Knox City Council",
    "knox city council": "Knox City Council",
    "maroondah": "Maroondah City Council",
    "maroondah city council": "Maroondah City Council",
}


# Sources whose jobs skip the broad-feed plausibility pre-filter: keyword
# searches are already targeted, and manual adds are intentional by definition.
KEYWORD_FILTERED_SOURCES = {"manual"}


ROLE_STOPWORDS = {
    "and", "or", "the", "for", "with", "role", "jobs", "job", "position", "senior", "junior",
    "lead", "head", "chief", "principal", "officer", "advisor", "adviser", "specialist",
    "consultant", "manager", "coordinator", "administrator", "assistant", "executive",
    "melbourne", "victoria", "australia", "vic",
}


BROAD_RELEVANT_TITLES = {
    "application", "applications", "analyst", "architecture", "automation", "business",
    "change", "cloud", "commercial", "compliance", "continuous", "customer", "cyber",
    "data", "delivery", "digital", "enablement", "enterprise", "governance", "ict",
    "implementation", "information", "innovation", "integration", "it", "leadership",
    "operations", "portfolio", "process", "product", "program", "programme", "project",
    "quality", "risk", "service", "software", "solution", "solutions", "stakeholder",
    "strategy", "systems", "technical", "technology", "transformation", "vendor",
}


BROAD_UNRELATED_TITLES = {
    "apprentice", "barista", "bartender", "carer", "chef", "childcare", "cleaner",
    "cook", "dentist", "doctor", "driver", "educator", "electrician", "gardener",
    "hospitality", "labourer", "lifeguard", "mechanic", "nurse", "pharmacist",
    "plumber", "receptionist", "retail", "security", "surgeon", "teacher", "vet",
    "waiter", "waitress", "warehouse",
}


KNOWN_RECRUITERS = {
    "accent group recruitment", "adecco", "ambition", "ashdown people", "bluefin resources",
    "charterhouse", "circuit recruitment", "davidson", "deloitte recruitment", "finite",
    "halcyon knights", "hays", "hudson", "ignite", "korn ferry", "michael page",
    "page executive", "paxus", "peoplebank", "randstad", "robert half", "sharp & carter",
    "six degrees executive", "talent", "talent international", "talent – specialists in tech, transformation & beyond",
    "the network", "u&u", "underwood executive", "vertical talent", "west recruitment",
    "zone IT solutions", "horizontal talent", "hamilton barnes", "pacific search",
}


RECRUITER_PHRASES = [
    "our client", "we are partnering", "we're partnering", "on behalf of", "confidential client",
    "client is seeking", "client are seeking", "recruitment consultant", "advising consultant",
    "specialists in tech", "staffing", "recruiting", "recruitment agency", "executive search",
]


DIRECT_EMPLOYER_PHRASES = [
    "about us", "about the company", "our organisation", "our organization", "our team",
    "we are seeking", "we're seeking", "join us", "life at", "our values",
]


COMPANY_CANDIDATE_STOPWORDS = {
    "a", "about", "about us", "about you", "all", "and", "are", "as", "at", "business",
    "candidate", "client", "company", "confidential", "department", "employer", "for",
    "group", "here", "hiring", "if", "in", "it", "its", "join", "key responsibilities",
    "new", "our", "people", "position", "responsibilities", "role", "team", "the",
    "the company", "the opportunity", "the role", "their", "this", "this role", "we",
    "what you", "who", "with", "work", "you", "your",
}


MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


# Google retired the Gemini 1.x/2.0 families; calling them 404s. The db_setup
# migration rewrites stored names at launch, and this sanitiser is the runtime
# belt-and-braces so a stale name can never reach an API call.
RETIRED_GEMINI_MODELS = {"gemini-pro", "gemini-pro-vision"}


RETIRED_GEMINI_MODEL_PREFIXES = ("gemini-1.0", "gemini-1.5", "gemini-2.0")


RETIRED_CLAUDE_MODELS = {"claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022"}


HIDDEN_MARKET_SOURCE = "Hidden Market"


MANUAL_SOURCE = "Manual"

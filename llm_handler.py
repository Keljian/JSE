"""LLM integration for job triage, analysis, research, and document generation.

This module is now a facade. The implementation lives in the `llm/` package —
see `llm/__init__.py` for the layering — and everything it defines is re-exported
here so every existing caller (`import llm_handler`) keeps working unchanged.

Attribute writes are forwarded into the package by `facade.install` so that
`llm_handler._call_unsloth = stub` still reaches the code that runs — see
facade.py for why that matters.
"""
import facade
from llm import (
    analysis as _analysis,
    documents as _documents,
    memory as _memory,
    parsing as _parsing,
    prompts as _prompts,
    providers as _providers,
    research as _research,
)

from llm.providers import (  # noqa: F401
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MODEL,
    LLMHTTPError,
    LLMRequestError,
    UNSLOTH_API_KEY,
    UNSLOTH_BASE_URL,
    UNSLOTH_IS_CONFIGURED,
    UNSLOTH_MAX_RETRIES,
    UNSLOTH_MODEL,
    UNSLOTH_RETRY_DELAY,
    _analysis_worker_count,
    _call_claude,
    _call_document_ai,
    _call_gemini,
    _call_openai_compatible,
    _call_scoring_ai,
    _call_unsloth,
    _discover_local_model,
    _get_json,
    _llm_gate,
    _llm_gate_lock,
    _llm_gate_size,
    _llm_slot,
    _local_ai_settings,
    _local_auth_headers,
    _local_is_configured,
    _messages_to_text,
    _model_name,
    _post_json,
    _scoring_settings,
    _serialized_llm_call,
    _settings_for_ai_task,
    list_models_for_provider,
)
from llm.parsing import (  # noqa: F401
    _IMAGE_REF_RE,
    _OPEN_THINK_RE,
    _THINK_BLOCK_RE,
    _bullet_section,
    _coerce_list,
    _escape_control_chars_in_json_strings,
    _extract_json,
    _extract_json_list,
    _json_object_candidate,
    _repair_json_via_llm,
    _strip_image_references,
    _strip_reasoning_blocks,
)
from llm.prompts import (  # noqa: F401
    ANALYSIS_SYSTEM_PROMPT,
    APPLICATION_DOCUMENT_SYSTEM_PROMPT,
    COMPANY_RESEARCH_SYSTEM_PROMPT,
    DEEP_GATEKEEPER_SYSTEM_PROMPT,
    FULL_ANALYSIS_TRIAGE_THRESHOLD,
    POSITIONING_DOCTRINE,
    TRIAGE_KEEP_THRESHOLD,
    TRIAGE_SYSTEM_PROMPT,
)
from llm.analysis import (  # noqa: F401
    JOB_FLAG_LABELS,
    JOB_FLAG_TYPES,
    _format_flags_section,
    _normalise_job_flags,
    _persist_flags,
    _CREDENTIAL_CUES,
    _MANDATORY_CUES,
    _REQUIREMENT_SPLIT_RE,
    _analysis_fragment_alignment,
    _analysis_preferences,
    _analyze_single_job,
    _apply_preference_weight,
    _band_block,
    _coerce_fragment_score,
    _compose_score,
    _extract_mandatory_requirements,
    _format_analysis_text,
    _format_fragment_context,
    _format_gatekeeper_section,
    _get_resume_triage_summary,
    _lane_title_overlap,
    _maybe_align_fragments,
    _perform_analysis_loop,
    _resume_hash,
    _run_deep_gatekeeper,
    _triage_job,
    analyze_jobs,
    analyze_specific_jobs,
    check_job_relevance,
    derive_search_terms_from_resume,
    generalize_search_term,
)
from llm.documents import (  # noqa: F401
    generate_application_documents,
    generate_template_application_content,
    review_application_kit,
)
from llm.memory import (  # noqa: F401
    _normalise_memory_fragments,
    align_memory_fragments_to_role,
    consolidate_memory_fragments,
    derive_search_terms_from_fragments,
    extract_application_memory_fragments,
    promote_emerging_fragments,
)
from llm.research import (  # noqa: F401
    _fallback_job_intelligence,
    _hidden_market_strategy_text_legacy,
    extract_job_intelligence,
    hidden_market_strategy,
    research_company_for_job,
)

# Writes through this module must reach the implementation; see facade.py.
facade.install(__name__, (
    _providers, _parsing, _prompts, _analysis, _documents, _memory, _research,
))

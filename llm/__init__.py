"""LLM integration, split out of the former 3,644-line llm_handler.

Import `llm_handler` rather than this package: it is the facade every caller
already uses.

Modules are layered — each may import only from the layers above it:

    providers   the concurrency gate, HTTP transport, the call* family
    parsing     reasoning-block stripping and JSON recovery
    prompts     system prompts and the thresholds they are written against
    analysis    triage (which also raises flags), full analysis, deep gatekeeping
    documents   application document content generation
    memory      candidate-memory fragment extraction and consolidation
    research    company/job intelligence and hidden-market strategy

A few crossings are genuinely cyclic (a provider repairing malformed JSON needs
the parser, which needs a provider to do the repair). Those use a function-local
import marked with a comment.
"""
from .providers import (  # noqa: F401
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
from .parsing import (  # noqa: F401
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
from .prompts import (  # noqa: F401
    ANALYSIS_SYSTEM_PROMPT,
    ANALYSIS_SYSTEM_PROMPT_BASE,
    APPLICATION_DOCUMENT_SYSTEM_PROMPT,
    COMPANY_RESEARCH_SYSTEM_PROMPT,
    DEEP_GATEKEEPER_SYSTEM_PROMPT,
    DEEP_GATEKEEPER_SYSTEM_PROMPT_BASE,
    FULL_ANALYSIS_TRIAGE_THRESHOLD,
    POSITIONING_DOCTRINE,
    TRIAGE_KEEP_THRESHOLD,
    TRIAGE_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT_BASE,
    lane_brief,
    resolve_positioning_doctrine,
    with_doctrine,
)
from .analysis import (  # noqa: F401
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
from .documents import (  # noqa: F401
    generate_application_documents,
    generate_template_application_content,
    review_application_kit,
)
from .memory import (  # noqa: F401
    _normalise_memory_fragments,
    align_memory_fragments_to_role,
    consolidate_memory_fragments,
    derive_search_terms_from_fragments,
    extract_application_memory_fragments,
    promote_emerging_fragments,
)
from .research import (  # noqa: F401
    _fallback_job_intelligence,
    _hidden_market_strategy_text_legacy,
    extract_job_intelligence,
    hidden_market_strategy,
    research_company_for_job,
)

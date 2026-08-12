/** Shared vocabularies and layout constants. */

const PIPELINE = [
  { id: "new", label: "New", defaultAction: "Review fit", actionOffset: 2 },
  { id: "interested", label: "Interested", defaultAction: "Prepare application", actionOffset: 3 },
  { id: "applied", label: "Applied", defaultAction: "Follow up", actionOffset: 7 },
  { id: "interviewing", label: "Interviewing", defaultAction: "Prepare for interview", actionOffset: 2 },
  { id: "offer", label: "Offer / Final", defaultAction: "Review offer", actionOffset: 2 },
  { id: "rejected", label: "Rejected", defaultAction: "", actionOffset: 0 },
  { id: "rejected_by_company", label: "Declined by Company", defaultAction: "", actionOffset: 0 },
  { id: "archived", label: "Archived", defaultAction: "", actionOffset: 0 }
];

const WORKSPACE_TABS = ["Details", "Company", "Application", "Interviews", "Feedback", "Notes", "Timeline"];

// Rendering thousands of cards (rejected/archived columns grow forever) keeps
// hundreds of thousands of DOM nodes alive and makes renderer memory scale
// with database size. Column header counts remain exact; only rendering is capped.
const KANBAN_COLUMN_RENDER_CAP = 60;

const WORK_MODES = [
  { id: "hybrid", label: "Hybrid" },
  { id: "remote", label: "Remote" },
  { id: "wfh", label: "WFH" },
  { id: "onsite", label: "On site" }
];

// Compass points as seen from the metro centre, clockwise from north. Used to
// let a lane say "eastern and northern suburbs" without the app needing to know
// what any suburb in any country is called.
const COMPASS_SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

const LOCAL_AI_RUNTIMES = {
  lmstudio: {
    label: "LM Studio",
    downloadUrl: "https://lmstudio.ai/download",
    baseUrl: "http://localhost:1234/v1",
    model: "",
  },
  ollama: {
    label: "Ollama",
    downloadUrl: "https://ollama.com/download/windows",
    baseUrl: "http://localhost:11434/v1",
    model: "qwen2.5:7b",
  },
};

const DOCUMENT_AI_PROVIDERS = [
  { id: "local", label: "Local endpoint" },
  { id: "gemini", label: "Gemini" },
  { id: "compat", label: "Free / OpenAI-compatible" },
  { id: "chatgpt", label: "ChatGPT" },
  { id: "claude", label: "Claude" }
];

// Presets for the free / OpenAI-compatible endpoint. base_url is filled in; the
// user supplies a model (and an API key, which most free tiers still require).
const COMPAT_PRESETS = {
  groq: { label: "Groq", baseUrl: "https://api.groq.com/openai/v1", model: "llama-3.3-70b-versatile", keyUrl: "https://console.groq.com/keys" },
  cerebras: { label: "Cerebras", baseUrl: "https://api.cerebras.ai/v1", model: "llama-3.3-70b", keyUrl: "https://cloud.cerebras.ai" },
  openrouter: { label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1", model: "", keyUrl: "https://openrouter.ai/keys" },
  opencode: { label: "OpenCode Zen", baseUrl: "https://opencode.ai/zen/v1", model: "", keyUrl: "https://opencode.ai/auth" }
};

const SUPPORT_MESSAGE = "JSE is open-source and free to use. If it saved you time or sanity on the job hunt, a coffee keeps the project caffeinated and the commits coming:";

const SUPPORT_URL = "https://ko-fi.com/keljian";

const RELEASES_URL = "https://github.com/Keljian/JSE/releases";

const SETTINGS_SECTIONS = [
  { id: "profile", label: "Lane", scope: "lane" },
  { id: "search", label: "Search", scope: "lane" },
  { id: "matching", label: "Matching", scope: "lane" },
  { id: "documents", label: "Documents", scope: "lane" },
  { id: "evidence", label: "Evidence", scope: "lane" },
  { id: "searchers", label: "Searchers", scope: "general" },
  { id: "folders", label: "Folders", scope: "general" },
  { id: "ai", label: "AI", scope: "general" },
  { id: "templates", label: "Templates", scope: "general" },
  { id: "maintenance", label: "Maintenance", scope: "general" }
];

const CORPUS_DOC_TYPES = ["resume", "cover_letter", "ksc_response", "position_description", "capability_statement", "other"];

// Mirrors COMPOSITE_MATCH_WEIGHT / COMPOSITE_FRAGMENT_WEIGHT in
// database_manager.py. Rebalanced from 80/20 on 2026-07-30: match_score was
// carrying most of the ranking weight while separating outcomes not at all.
// The stored composite_score is authoritative; these are only used to render a
// preview when a job has not been re-scored yet.
const COMPOSITE_MATCH_WEIGHT = 0.60;

const COMPOSITE_FRAGMENT_WEIGHT = 0.40;

const APPLY_CHANNEL_LABELS = {
  recruiter: "Recruiter",
  ats: "ATS apply",
  email_direct: "Direct email",
  board_apply: "Board apply",
};

// Flags raised at triage. They are observations shown beside the score; no
// code path branches on them, so a flagged role behaves like any other.
const JOB_FLAG_CHIPS = {
  credential_gate: ["bad", "Credential gate"],
  domain_mismatch: ["bad", "Domain mismatch"],
  seniority_below: ["warn", "Below your level"],
  seniority_above: ["warn", "Above your level"],
  evidence_gap: ["muted", "Evidence gap"],
};

const JOB_FLAG_FILTERS = [
  { value: "", label: "Any flags" },
  { value: "credential_gate", label: "Credential gate" },
  { value: "domain_mismatch", label: "Domain mismatch" },
  { value: "seniority_below", label: "Below your level" },
  { value: "seniority_above", label: "Above your level" },
  { value: "evidence_gap", label: "Evidence gap" },
];

// Channel warmth. Warm routes convert far better than cold portals, so the
// board shows warmth next to the score rather than burying it in the workspace.
const CHANNEL_OPTIONS = [
  { value: "", label: "Derive from source" },
  { value: "board", label: "Job board" },
  { value: "recruiter", label: "Recruiter" },
  { value: "warm_referral", label: "Warm referral" },
  { value: "direct_outreach", label: "Direct outreach" },
];

const WARMTH_CHIPS = {
  2: ["good", "Warm"],
  1: ["warn", "Named contact"],
};

const DOC_TRACK_OPTIONS = [
  { value: "", label: "Derive from the role" },
  { value: "senior", label: "Full senior" },
  { value: "stripped_back", label: "Stripped back" },
];

// ---------------------------------------------------------------------------
// Structured analysis rendering. ai_analysis is stored as labelled plain text
// (produced by llm_handler._format_analysis_text / _format_gatekeeper_section);
// this parser turns that stable format — including thousands of historical
// analyses — into sections the UI can style. Unparseable text falls back to
// the old <pre> rendering.
// ---------------------------------------------------------------------------
const ANALYSIS_TOP_FIELDS = new Set(["Match Score", "Triage Match Score", "Fit Level", "Recommended Action"]);

const SCOPE_FIELD_KEYS = new Set([
  "Decision", "Gate Score", "Original Full-Analysis Score", "Score Cap Applied",
  "Confidence", "Role Family", "Seniority Fit", "Application ROI",
  "Application Angle", "Reason", "Fragment Score"
]);

// How far an interview actually got. A first-round screen-out and a runner-up
// finish used to collapse to the same "Unsuccessful", which hid the results
// carrying the strongest signal in the whole funnel.
const NEAR_MISS_RESOLUTIONS = [
  { id: "offer", label: "Progressed / offer" },
  { id: "runner_up", label: "Runner-up" },
  { id: "final_round", label: "Reached final round" },
  { id: "declined", label: "Unsuccessful" },
];

const HM_STATUS_LABELS = { todo: "To do", contacted: "Contacted", awaiting: "Awaiting reply", done: "Done" };

const HM_OUTCOME_LABELS = {
  "": "—",
  replied: "Replied",
  meeting: "Meeting booked",
  no_response: "No response",
  dead_end: "Dead end",
  converted: "Converted",
};

const HM_TYPE_LABELS = { recruiter: "Recruiter", direct_employer: "Direct employer", leadership_gap: "Leadership gap" };

const PLAN_KIND_META = {
  interview: { label: "Interview", cls: "u0" },
  offer: { label: "Offer", cls: "u0" },
  closing: { label: "Closing soon", cls: "u1" },
  overdue: { label: "Due now", cls: "u2" },
  followup: { label: "Follow up", cls: "u3" },
  stage: { label: "New opportunity", cls: "u4" }
};

export { JOB_FLAG_CHIPS, JOB_FLAG_FILTERS, PIPELINE, WORKSPACE_TABS, KANBAN_COLUMN_RENDER_CAP, WORK_MODES, COMPASS_SECTORS, LOCAL_AI_RUNTIMES, DOCUMENT_AI_PROVIDERS, COMPAT_PRESETS, SUPPORT_MESSAGE, SUPPORT_URL, RELEASES_URL, SETTINGS_SECTIONS, CORPUS_DOC_TYPES, COMPOSITE_MATCH_WEIGHT, COMPOSITE_FRAGMENT_WEIGHT, APPLY_CHANNEL_LABELS, CHANNEL_OPTIONS, WARMTH_CHIPS, DOC_TRACK_OPTIONS, ANALYSIS_TOP_FIELDS, SCOPE_FIELD_KEYS, NEAR_MISS_RESOLUTIONS, HM_STATUS_LABELS, HM_OUTCOME_LABELS, HM_TYPE_LABELS, PLAN_KIND_META };

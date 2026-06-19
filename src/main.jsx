import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BarChart3,
  BriefcaseBusiness,
  CalendarDays,
  Check,
  ChevronRight,
  CircleStop,
  Download,
  ExternalLink,
  FileText,
  Filter,
  FolderOpen,
  ArrowRightLeft,
  CalendarClock,
  KanbanSquare,
  Lightbulb,
  ListTodo,
  Loader2,
  NotebookTabs,
  Play,
  Plus,
  Radar,
  RefreshCw,
  Send,
  Settings,
  Sparkles,
  Target,
  TrendingUp,
  Trash2,
  Wrench,
  X
} from "lucide-react";
import "./styles.css";

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

function normalizeStage(stage, status) {
  const value = String(stage || status || "new").toLowerCase();
  const mapping = {
    approved: "interested",
    stale: "archived",
    docs_drafted: "interested",
    rejected: "rejected",
    company_rejected: "rejected_by_company",
    declined_by_company: "rejected_by_company",
    applied: "applied"
  };
  const normalized = mapping[value] || value;
  return PIPELINE.some((item) => item.id === normalized) ? normalized : "new";
}

function canMoveToInterested(job) {
  const rawStage = String(job?.pipeline_stage || job?.status || "new").toLowerCase();
  return normalizeStage(job?.pipeline_stage, job?.status) === "new" || rawStage === "approved";
}

const DOCUMENT_AI_PROVIDERS = [
  { id: "local", label: "Local endpoint" },
  { id: "chatgpt", label: "ChatGPT" },
  { id: "claude", label: "Claude" },
  { id: "gemini", label: "Gemini" }
];

const SUPPORT_MESSAGE = "JSE is open-source and free to use. If it saved you time or sanity on the job hunt, a coffee keeps the project caffeinated and the commits coming:";
const SUPPORT_URL = "https://ko-fi.com/keljian";

function openSupportLink(event) {
  event.preventDefault();
  window.jobAssistant.openExternal(SUPPORT_URL);
}

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

function documentAiLabel(settings) {
  const providerId = settings?.document_ai_provider || settings?.doc_ai_provider || "local";
  const provider = DOCUMENT_AI_PROVIDERS.find((item) => item.id === providerId);
  const model = settings?.doc_ai_model || settings?.[`${providerId}_model`] || settings?.local_model;
  return `${provider?.label || "Local"}${model ? ` (${model})` : ""}`;
}

function todayPlus(days) {
  if (!days) return "";
  const date = new Date();
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

function formatDate(value) {
  if (!value) return "Not set";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function closingDateSourceMeta(source) {
  const normalized = String(source || "default").toLowerCase();
  if (["advertisement", "provided", "actual"].includes(normalized)) {
    return { label: "Actual", className: "actual", title: "Pulled from the job ad" };
  }
  return { label: "Assigned", className: "assigned", title: "Assigned by the software or edited manually" };
}

function ClosingDateSourceBadge({ source }) {
  const meta = closingDateSourceMeta(source);
  return <span className={`source-badge ${meta.className}`} title={meta.title}>{meta.label}</span>;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

function toErrorMessage(error) {
  return error?.message || String(error);
}

function scoreClass(value) {
  const score = Number(value || 0);
  return score >= 80 ? "high" : score >= 60 ? "mid" : "low";
}

function primaryScore(job) {
  const match = Number(job?.match_score || 0);
  const hasFragment = job?.fragment_score !== null && job?.fragment_score !== undefined;
  return hasFragment ? Math.round((0.80 * match) + (0.20 * Number(job.fragment_score || 0))) : match;
}

function Score({ value, label = "" }) {
  const score = Number(value || 0);
  if (!score) return <span className="muted">Unscored</span>;
  return <span className={`score ${scoreClass(score)}`}>{label ? `${label} ` : ""}{score}%</span>;
}

function ScoreStack({ job, compact = false }) {
  const match = Number(job?.match_score || 0);
  const fragment = Number(job?.fragment_score || 0);
  const hasFragment = job?.fragment_score !== null && job?.fragment_score !== undefined;
  const composite = hasFragment ? Math.round((0.80 * match) + (0.20 * fragment)) : match;
  const hasComposite = hasFragment && composite > 0;
  const primary = hasComposite ? composite : match;
  if (!primary && !fragment) return <span className="muted">Unscored</span>;
  return (
    <span className={`score-stack ${compact ? "compact" : ""}`} title={hasFragment ? `Composite = 80% match (${match}) + 20% fragment alignment (${fragment})` : "Final match score"}>
      {primary ? <Score value={primary} label={hasComposite ? "Comp" : "Match"} /> : null}
      {!compact && hasComposite && match ? <span className="score-chip">Match {match}%</span> : null}
      {fragment ? <span className={`score-chip ${scoreClass(fragment)}`}>Frag {fragment}%</span> : null}
    </span>
  );
}

function Modal({ title, children, onClose, wide = false, closeDisabled = false }) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className={`modal ${wide ? "wide-modal" : ""}`}>
        <header className="modal-head">
          <h2>{title}</h2>
          <button className="icon secondary" disabled={closeDisabled} onClick={onClose} aria-label="Close"><X size={18} /></button>
        </header>
        {children}
      </section>
    </div>
  );
}

// In-app replacements for window.confirm / prompt / alert. Electron's native
// synchronous dialogs intermittently break mouse/keyboard input in the window
// after they close (a long-standing Chromium-on-Windows focus bug), which left
// parts of the UI unclickable until the window was refocused. The App mounts a
// dialog host into dialogBridge; these helpers fall back to the native dialogs
// only if the host is somehow not mounted.
const dialogBridge = { current: null };

function requestDialog(request) {
  if (dialogBridge.current) return dialogBridge.current(request);
  if (request.kind === "confirm") return Promise.resolve(window.confirm(request.message));
  if (request.kind === "prompt") return Promise.resolve(window.prompt(request.message || request.title));
  window.alert(request.message);
  return Promise.resolve(true);
}

const appConfirm = (options) => requestDialog({ kind: "confirm", ...options });
const appPrompt = (options) => requestDialog({ kind: "prompt", ...options });
const appNotice = (options) => requestDialog({ kind: "notice", ...options });

function DialogModal({ dialog, onClose }) {
  const [text, setText] = useState(dialog.defaultValue || "");
  useEffect(() => setText(dialog.defaultValue || ""), [dialog]);
  const confirm = () => onClose(dialog.kind === "prompt" ? text.trim() : true);
  const cancel = () => onClose(dialog.kind === "prompt" ? null : dialog.kind === "notice");
  return (
    <Modal title={dialog.title || "Confirm"} onClose={cancel}>
      {dialog.message ? <div className="modal-copy">{dialog.message}</div> : null}
      {dialog.kind === "prompt" ? (
        <label className="field">
          <span>{dialog.label || "Value"}</span>
          <input
            autoFocus
            value={text}
            placeholder={dialog.placeholder || ""}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter") confirm(); }}
          />
        </label>
      ) : null}
      <footer className="modal-actions">
        {dialog.kind !== "notice" ? <button className="secondary" onClick={cancel}>Cancel</button> : null}
        <button autoFocus={dialog.kind !== "prompt"} className={dialog.danger ? "danger" : ""} onClick={confirm}>
          {dialog.danger ? <Trash2 size={16} /> : <Check size={16} />} {dialog.confirmLabel || "OK"}
        </button>
      </footer>
    </Modal>
  );
}

const JobCard = React.memo(function JobCard({ job, onOpen, onDragStart, onReject }) {
  return (
    <article
      className={`kanban-card priority-${job.priority || "normal"}`}
      draggable
      onDragStart={(event) => onDragStart(event, job)}
      onDoubleClick={() => onOpen(job)}
    >
      <div className="card-title-row">
        <strong>{job.title}</strong>
      </div>
      <div className="card-score-row">
        <ScoreStack job={job} compact />
      </div>
      <p>{job.company || "Unknown company"}</p>
      <small>{job.profile_name || "Lane"} · {job.source || "Unknown source"}</small>
      <div className="card-meta">
        {job.next_action ? <span>{job.next_action}</span> : <span>No next action</span>}
        <time>{job.next_action_date ? formatDate(job.next_action_date) : "No due date"}</time>
      </div>
      <div className="card-actions">
        <button className="secondary" onClick={(event) => { event.stopPropagation(); onOpen(job); }}>Open</button>
        <button className="danger" onClick={(event) => { event.stopPropagation(); onReject(job); }}>Reject</button>
      </div>
    </article>
  );
});

function RejectJobModal({ job, onSave, onClose }) {
  const [reason, setReason] = useState(job?.retired_reason || "");
  const title = job?.title || "job";
  return (
    <Modal title={`Reject ${title}`} onClose={onClose}>
      <div className="modal-copy">Move this job to rejected and keep it in the history.</div>
      <label className="field"><span>Reason</span><textarea value={reason} placeholder="Not a fit, salary, location, timing..." onChange={(event) => setReason(event.target.value)} /></label>
      <footer className="modal-actions">
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button className="danger" onClick={() => onSave(reason.trim())}><X size={16} /> Reject job</button>
      </footer>
    </Modal>
  );
}

function QuickStageForm({ job, stage, onSave, onClose }) {
  const stageInfo = PIPELINE.find((item) => item.id === stage) || PIPELINE[0];
  const [form, setForm] = useState({
    pipeline_stage: stage,
    next_action: stage === "new" ? "" : stageInfo.defaultAction,
    next_action_date: stage === "new" ? "" : todayPlus(stageInfo.actionOffset),
    closing_date: job?.closing_date || "",
    closing_date_source: job?.closing_date_source || (job?.closing_date ? "provided" : "default"),
    application_date: stage === "applied" ? new Date().toISOString().slice(0, 10) : (job?.application_date || ""),
    application_url: job?.application_url || job?.url || "",
    contact_person: job?.contact_person || "",
    contact_email: job?.contact_email || "",
    contact_phone: job?.contact_phone || "",
    interview_date: stage === "interviewing" ? todayPlus(3) : (job?.interview_date || ""),
    interview_type: job?.interview_type || "Video",
    interview_people: job?.interview_people || "",
    feedback: job?.feedback || "",
    priority: job?.priority || "normal",
    salary: job?.salary || "",
    notes: job?.notes || ""
  });

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const updateClosingDate = (value) => setForm((current) => ({
    ...current,
    closing_date: value,
    closing_date_source: value === (job?.closing_date || "") ? current.closing_date_source : "assigned"
  }));

  return (
    <Modal title={`Move to ${stageInfo.label}`} onClose={onClose}>
      <div className="form-grid">
        <label><span>Next action</span><input value={form.next_action} onChange={(event) => update("next_action", event.target.value)} /></label>
        <label><span>Due date</span><input type="date" value={form.next_action_date || ""} onChange={(event) => update("next_action_date", event.target.value)} /></label>
        <label>
          <span className="label-row">Closing date <ClosingDateSourceBadge source={form.closing_date_source} /></span>
          <input type="date" value={form.closing_date || ""} onChange={(event) => updateClosingDate(event.target.value)} />
        </label>
        <label><span>Priority</span><select value={form.priority} onChange={(event) => update("priority", event.target.value)}><option>high</option><option>normal</option><option>low</option></select></label>
        <label><span>Application date</span><input type="date" value={form.application_date || ""} onChange={(event) => update("application_date", event.target.value)} /></label>
        <label><span>Application URL</span><input value={form.application_url || ""} onChange={(event) => update("application_url", event.target.value)} /></label>
        <label><span>Contact person</span><input value={form.contact_person || ""} onChange={(event) => update("contact_person", event.target.value)} /></label>
        <label><span>Contact email</span><input value={form.contact_email || ""} onChange={(event) => update("contact_email", event.target.value)} /></label>
        <label><span>Contact phone</span><input value={form.contact_phone || ""} onChange={(event) => update("contact_phone", event.target.value)} /></label>
        <label><span>Interview date</span><input type="datetime-local" value={form.interview_date || ""} onChange={(event) => update("interview_date", event.target.value)} /></label>
        <label><span>Interview type</span><input value={form.interview_type || ""} onChange={(event) => update("interview_type", event.target.value)} /></label>
        <label><span>People met with</span><input value={form.interview_people || ""} onChange={(event) => update("interview_people", event.target.value)} /></label>
        <label><span>Salary / rate</span><input value={form.salary || ""} onChange={(event) => update("salary", event.target.value)} /></label>
        <label className="full"><span>Feedback</span><textarea value={form.feedback || ""} onChange={(event) => update("feedback", event.target.value)} /></label>
        <label className="full"><span>Notes</span><textarea value={form.notes || ""} onChange={(event) => update("notes", event.target.value)} /></label>
      </div>
      <footer className="modal-actions">
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button onClick={() => onSave(form)}><Check size={16} /> Save move</button>
      </footer>
    </Modal>
  );
}

function AddJobModal({ busy, onSave, onClose }) {
  const [form, setForm] = useState({
    title: "",
    company: "",
    url: "",
    location: "",
    salary: "",
    closing_date: "",
    description: "",
    stage: "new",
    analyze: true
  });
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  return (
    <Modal title="Add Job Manually" onClose={onClose}>
      <div className="modal-copy">
        Track a role that never came through the scrapers — a recruiter call, referral, or careers-page find. It joins the pipeline like any scraped job.
      </div>
      <div className="form-grid">
        <label className="full"><span>Job title (required)</span><input autoFocus value={form.title} placeholder="Head of IT" onChange={(event) => update("title", event.target.value)} /></label>
        <label><span>Company</span><input value={form.company} placeholder="Employer or agency" onChange={(event) => update("company", event.target.value)} /></label>
        <label><span>Job URL (optional)</span><input value={form.url} placeholder="https://..." onChange={(event) => update("url", event.target.value)} /></label>
        <label><span>Location</span><input value={form.location} placeholder="Melbourne VIC" onChange={(event) => update("location", event.target.value)} /></label>
        <label><span>Salary / rate</span><input value={form.salary} onChange={(event) => update("salary", event.target.value)} /></label>
        <label><span>Closing date</span><input type="date" value={form.closing_date} onChange={(event) => update("closing_date", event.target.value)} /></label>
        <label><span>Starting stage</span>
          <select value={form.stage} onChange={(event) => update("stage", event.target.value)}>
            <option value="new">New</option>
            <option value="interested">Interested</option>
            <option value="applied">Applied (already submitted)</option>
          </select>
        </label>
        <label className="full"><span>Description / ad text (paste for analysis)</span><textarea value={form.description} placeholder="Paste the job ad, position description, or what the recruiter told you..." onChange={(event) => update("description", event.target.value)} /></label>
        <label className="check-row full"><input type="checkbox" checked={form.analyze} onChange={(event) => update("analyze", event.target.checked)} /> Run fit analysis after adding</label>
      </div>
      <footer className="modal-actions">
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button disabled={busy || !form.title.trim()} onClick={() => onSave(form)}><Plus size={16} /> Add job</button>
      </footer>
    </Modal>
  );
}

function RunSearchModal({ sources, activeProfileId, busy, onRun, onClose }) {
  const [selectedSources, setSelectedSources] = useState(sources);
  const [includeAllProfiles, setIncludeAllProfiles] = useState(false);
  const [optimism, setOptimism] = useState(3);
  const hasSources = sources.length > 0;

  useEffect(() => {
    setSelectedSources((current) => {
      const valid = current.filter((source) => sources.includes(source));
      if (valid.length) return valid;
      return sources;
    });
  }, [sources]);

  return (
    <Modal title="Run Search" onClose={onClose}>
      <div className="modal-copy">Manual search uses saved terms for each selected lane. If a lane has no terms, they will be generated first.</div>
      <label className="check-row"><input type="checkbox" checked={includeAllProfiles} onChange={(event) => setIncludeAllProfiles(event.target.checked)} /> Run across all lanes</label>
      <label className="field"><span>Optimism for generated terms</span><input type="range" min="1" max="5" value={optimism} onChange={(event) => setOptimism(Number(event.target.value))} /></label>
      <div className="source-grid">
        {hasSources ? sources.map((source) => (
          <label key={source} className="check-row">
            <input
              type="checkbox"
              checked={selectedSources.includes(source)}
              onChange={(event) => setSelectedSources((current) => event.target.checked ? [...new Set([...current, source])] : current.filter((item) => item !== source))}
            />
            {source}
          </label>
        )) : <p className="empty-inline">No scraper plugins are available. Import a plugin or create one in Settings &gt; Searchers.</p>}
      </div>
      <footer className="modal-actions">
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button disabled={busy || !hasSources || selectedSources.length === 0} onClick={() => onRun({ profile_id: activeProfileId, include_all_profiles: includeAllProfiles, sources: selectedSources, optimism })}><Play size={16} /> Run search</button>
      </footer>
    </Modal>
  );
}

function AnalysisModal({ activeProfileId, busy, onRun, onClose }) {
  const [includeAllProfiles, setIncludeAllProfiles] = useState(false);
  const [stage, setStage] = useState("new");

  return (
    <Modal title="Run Analysis" onClose={onClose}>
      <label className="check-row"><input type="checkbox" checked={includeAllProfiles} onChange={(event) => setIncludeAllProfiles(event.target.checked)} /> Run across all lanes</label>
      <label className="field"><span>Stage</span><select value={stage} onChange={(event) => setStage(event.target.value)}>{PIPELINE.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <footer className="modal-actions">
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button disabled={busy} onClick={() => onRun({ profile_id: activeProfileId, include_all_profiles: includeAllProfiles, stage, re_analyze: false })}><Sparkles size={16} /> Run analysis</button>
      </footer>
    </Modal>
  );
}

function OnboardingWizard({ prerequisites, profile, busy, onComplete, onSkip }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState(profile?.name === "General" ? "My search" : (profile?.name || "My search"));
  const [resumePath, setResumePath] = useState(profile?.resume_path || "");
  const [error, setError] = useState("");
  const [localRuntime, setLocalRuntime] = useState("lmstudio");
  const chromeReady = Boolean(prerequisites?.chrome?.found);
  const pythonReady = Boolean(prerequisites?.python?.found);

  const chooseResume = async () => {
    const selected = await window.jobAssistant.chooseResume?.();
    if (selected) setResumePath(selected);
  };
  const finish = async () => {
    setError("");
    try {
      const runtime = LOCAL_AI_RUNTIMES[localRuntime];
      await onComplete({
        name: name.trim() || "My search",
        resume_path: resumePath.trim(),
        local_base_url: runtime.baseUrl,
        local_model: runtime.model,
      });
    } catch (nextError) {
      setError(toErrorMessage(nextError));
    }
  };

  return (
    <div className="modal-backdrop onboarding-backdrop" role="presentation">
      <section className="modal onboarding-modal" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
        <div className="onboarding-brand"><BriefcaseBusiness size={24} /><strong>JSE setup</strong><span>Step {step + 1} of 3</span></div>
        {step === 0 ? (
          <>
            <h2 id="onboarding-title">Welcome to JSE</h2>
            <p className="modal-copy">Let’s check the two things JSE needs before you start searching.</p>
            <div className="prerequisite-list">
              <article className={pythonReady ? "prerequisite-card ready" : "prerequisite-card warning"}>
                {pythonReady ? <Check /> : <X />}<div><strong>JSE runtime</strong><span>{pythonReady ? "Bundled and ready" : "Runtime not found — reinstall this build"}</span></div>
              </article>
              <article className={chromeReady ? "prerequisite-card ready" : "prerequisite-card warning"}>
                {chromeReady ? <Check /> : <ExternalLink />}<div><strong>Google Chrome</strong><span>{chromeReady ? "Detected and ready for job searches" : "Required by browser-based searchers"}</span></div>
                {!chromeReady ? <button className="secondary" onClick={() => window.jobAssistant.openExternal("https://www.google.com/chrome/")}>Get Chrome</button> : null}
              </article>
            </div>
            {prerequisites?.unsigned_build ? <div className="unsigned-note"><strong>Unsigned beta</strong><span>Windows may show SmartScreen. If you downloaded JSE from the official release, choose <b>More info</b>, then <b>Run anyway</b>. Never disable SmartScreen globally.</span></div> : null}
          </>
        ) : null}
        {step === 1 ? (
          <>
            <h2 id="onboarding-title">Set up your first search lane</h2>
            <p className="modal-copy">JSE keeps each kind of role in its own lane. Your base resume anchors matching and document generation.</p>
            <label className="field"><span>Lane name</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Product leadership" /></label>
            <label className="field"><span>Base resume</span><div className="resume-picker"><input value={resumePath} readOnly placeholder="Choose a .docx resume" /><button className="secondary" onClick={chooseResume}><FolderOpen size={16} /> Choose</button></div></label>
            <p className="onboarding-privacy">Your database, resumes, templates, and generated applications stay inside the JSE installation folder. Nothing is uploaded unless you configure an AI provider or open an employer site.</p>
          </>
        ) : null}
        {step === 2 ? (
          <>
            <h2 id="onboarding-title">Choose your local AI</h2>
            <p className="modal-copy">JSE uses a local model for private, high-volume job matching. Choose and install <strong>one</strong> runtime below—you do not need both—then download a chat/instruct model inside it and start its local server.</p>
            <div className="local-runtime-options">
              {Object.entries(LOCAL_AI_RUNTIMES).map(([id, runtime]) => (
                <article key={id} className={localRuntime === id ? "local-runtime-card selected" : "local-runtime-card"}>
                  <label><input type="radio" name="local-runtime" checked={localRuntime === id} onChange={() => setLocalRuntime(id)} /><strong>{runtime.label}</strong></label>
                  <span>{id === "lmstudio" ? "Friendly desktop UI; load a model and start the Local Server." : "Lightweight service; install, then pull and run a model."}</span>
                  <button className="secondary" onClick={() => window.jobAssistant.openExternal(runtime.downloadUrl)}><ExternalLink size={15} /> Install {runtime.label}</button>
                </article>
              ))}
            </div>
            <div className="onboarding-ready"><Check size={32} /><div><strong>Then test the connection in Settings</strong><span>The preset endpoint will be saved now. The first browser search can also take longer while Selenium prepares Chrome’s matching driver.</span></div></div>
            <div className="install-location"><span>Local data location</span><code>{prerequisites?.data_dir || "JSE/settings"}</code></div>
            {error ? <p className="settings-alert">{error}</p> : null}
          </>
        ) : null}
        <div className="modal-actions onboarding-actions">
          <button className="ghost" disabled={busy} onClick={onSkip}>Set up later</button>
          <div />
          {step > 0 ? <button className="secondary" disabled={busy} onClick={() => setStep((value) => value - 1)}>Back</button> : null}
          {step < 2 ? <button disabled={step === 1 && !resumePath.trim()} onClick={() => setStep((value) => value + 1)}>Continue <ChevronRight size={16} /></button> : <button disabled={busy || !resumePath.trim()} onClick={finish}>{busy ? <Loader2 className="spin" size={16} /> : <Check size={16} />} Finish setup</button>}
        </div>
      </section>
    </div>
  );
}

function CreateLaneModal({ busy, onCreate, onClose }) {
  const [form, setForm] = useState({
    name: "",
    resume_path: "",
    lane_intent: "",
    target_titles: "",
    target_domains: "",
    seniority: "",
    preferred_location: "Melbourne VIC",
    work_modes: WORK_MODES.map((mode) => mode.id),
    must_have_terms: "",
    avoid_terms: "",
    keyword_mode: "generate",
    keywords: "",
    optimism: 3,
    generate_fragments: true,
  });
  const [error, setError] = useState("");
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const toggleMode = (mode, checked) => update(
    "work_modes",
    checked
      ? [...new Set([...form.work_modes, mode])]
      : form.work_modes.filter((item) => item !== mode)
  );
  const chooseResume = async () => {
    const resumePath = await window.jobAssistant.chooseResume();
    if (resumePath) update("resume_path", resumePath);
  };
  const submit = async () => {
    setError("");
    try {
      await onCreate({
        ...form,
        terms: form.keywords.split(/[\n,;]+/).map((term) => term.trim()).filter(Boolean),
      });
    } catch (createError) {
      setError(toErrorMessage(createError));
    }
  };
  const canCreate = form.name.trim() && form.resume_path.trim() && form.work_modes.length
    && (form.keyword_mode !== "manual" || form.keywords.trim());

  return (
    <Modal title="Create lane" onClose={onClose} closeDisabled={busy} wide>
      <div className="modal-copy">Define the kind of work this lane should find. JSE will create it first, then finish any LLM-assisted setup in the background.</div>
      <div className="lane-setup-body">
        <section className="lane-setup-section">
          <div className="lane-setup-heading"><span>1</span><div><h3>Lane identity</h3><p>Name the lane and give it the résumé that matching should treat as ground truth.</p></div></div>
          <div className="lane-setup-grid">
            <label><span>Lane name</span><input autoFocus value={form.name} placeholder="e.g. IT Leadership" onChange={(event) => update("name", event.target.value)} /></label>
            <label><span>Preferred location</span><input value={form.preferred_location} placeholder="Melbourne VIC" onChange={(event) => update("preferred_location", event.target.value)} /></label>
            <div className="resume-picker full">
              <div><span>Base résumé</span><strong title={form.resume_path}>{displayFileName(form.resume_path) || "No résumé selected"}</strong><small>DOCX · required for fit analysis, search terms and truthful fragments</small></div>
              <button type="button" className="secondary" disabled={busy} onClick={chooseResume}><FolderOpen size={16} /> Choose résumé</button>
            </div>
          </div>
        </section>

        <section className="lane-setup-section">
          <div className="lane-setup-heading"><span>2</span><div><h3>Targeting</h3><p>These particulars steer scraping, scoring and application positioning for this lane.</p></div></div>
          <div className="lane-setup-grid">
            <label className="full"><span>Lane intent</span><textarea rows={2} value={form.lane_intent} placeholder="Senior technology leadership roles bridging systems, operations and business outcomes…" onChange={(event) => update("lane_intent", event.target.value)} /></label>
            <label><span>Target titles</span><textarea rows={2} value={form.target_titles} placeholder="IT Manager, Head of Technology, Digital Systems Manager" onChange={(event) => update("target_titles", event.target.value)} /></label>
            <label><span>Target domains</span><textarea rows={2} value={form.target_domains} placeholder="Infrastructure, platforms, transformation, service delivery" onChange={(event) => update("target_domains", event.target.value)} /></label>
            <label><span>Seniority</span><input value={form.seniority} placeholder="Manager, senior manager, head of" onChange={(event) => update("seniority", event.target.value)} /></label>
            <div className="lane-mode-picker">
              <span>Work modes</span>
              <div>{WORK_MODES.map((mode) => <label key={mode.id} className="check-row"><input type="checkbox" checked={form.work_modes.includes(mode.id)} onChange={(event) => toggleMode(mode.id, event.target.checked)} /> {mode.label}</label>)}</div>
            </div>
            <label><span>Must-have signals</span><textarea rows={2} value={form.must_have_terms} placeholder="Stakeholder leadership, vendor governance, systems delivery" onChange={(event) => update("must_have_terms", event.target.value)} /></label>
            <label><span>Avoid signals</span><textarea rows={2} value={form.avoid_terms} placeholder="Junior support, shift work, pure coding" onChange={(event) => update("avoid_terms", event.target.value)} /></label>
          </div>
        </section>

        <section className="lane-setup-section">
          <div className="lane-setup-heading"><span>3</span><div><h3>Search terms and memory</h3><p>Seed the lane manually or let the local model derive terms after it mines the résumé.</p></div></div>
          <div className="lane-setup-options">
            <label className={`lane-setup-option ${form.keyword_mode === "generate" ? "active" : ""}`}>
              <input type="radio" name="keyword-mode" checked={form.keyword_mode === "generate"} onChange={() => update("keyword_mode", "generate")} />
              <span><strong>Generate with local LLM</strong><small>Uses the résumé, lane strategy and newly mined fragments.</small></span>
            </label>
            <label className={`lane-setup-option ${form.keyword_mode === "manual" ? "active" : ""}`}>
              <input type="radio" name="keyword-mode" checked={form.keyword_mode === "manual"} onChange={() => update("keyword_mode", "manual")} />
              <span><strong>Add keywords manually</strong><small>Enter one title or search phrase per line.</small></span>
            </label>
          </div>
          {form.keyword_mode === "manual" ? <label className="lane-keywords"><span>Search terms</span><textarea rows={3} value={form.keywords} placeholder={"IT Manager\nTechnology Business Partner\nDigital Systems Manager"} onChange={(event) => update("keywords", event.target.value)} /></label> : (
            <label className="lane-optimism"><span>Term breadth</span><select value={form.optimism} onChange={(event) => update("optimism", Number(event.target.value))}><option value={2}>Focused</option><option value={3}>Balanced</option><option value={4}>Broad</option></select></label>
          )}
          <label className={`lane-setup-option fragment-option ${form.generate_fragments ? "active" : ""}`}>
            <input type="checkbox" checked={form.generate_fragments} onChange={(event) => update("generate_fragments", event.target.checked)} />
            <span><strong>Mine reusable fragments from the base résumé</strong><small>Creates evidence-backed achievements, capabilities, skills and domain signals using the configured memory AI provider.</small></span>
          </label>
        </section>
      </div>
      {error ? <p className="lane-setup-error">{error}</p> : null}
      <footer className="modal-actions">
        <button className="secondary" disabled={busy} onClick={onClose}>Cancel</button>
        <button disabled={busy || !canCreate} onClick={submit}>{busy ? <Loader2 className="spin" size={16} /> : <Plus size={16} />} {busy ? "Creating lane…" : "Create lane"}</button>
      </footer>
    </Modal>
  );
}

function displayFileName(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.split(/[\\/]/).filter(Boolean).pop() || text;
}

function DropZone({ label, value, text, onDrop, onView, onDownload, onReveal }) {
  const inputRef = useRef(null);
  const uploadFile = (file) => {
    if (file) onDrop(file);
  };
  const fileName = displayFileName(value);

  return (
    <div
      className="drop-zone"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        uploadFile(event.dataTransfer.files?.[0]);
      }}
    >
      <div>
        <strong>{label}</strong>
        <span title={value || ""}>{fileName || "Drop .docx, .doc, .pdf, .txt, or .md here"}</span>
      </div>
      <div className="drop-zone-actions">
        <input
          ref={inputRef}
          className="file-input"
          type="file"
          accept=".docx,.doc,.pdf,.txt,.md"
          onChange={(event) => {
            uploadFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
        <button className="secondary" onClick={() => inputRef.current?.click()}><FolderOpen size={16} /> Upload</button>
        <button className="secondary" disabled={!text} onClick={onView}><FileText size={16} /> Open text</button>
        <button className="secondary" disabled={!value} onClick={onDownload}><Download size={16} /> Download</button>
        <button className="secondary" disabled={!value} onClick={onReveal}><ExternalLink size={16} /> Show</button>
      </div>
    </div>
  );
}

function DocumentTextModal({ title, text, onClose }) {
  return (
    <Modal title={title} onClose={onClose} wide>
      <pre className="document-text">{text || "No extracted text available."}</pre>
    </Modal>
  );
}

function CleanupModal({ jobs, onClose, onArchive, onOpenJob }) {
  const [selectedIds, setSelectedIds] = useState(() => new Set((jobs || []).map((job) => job.id)));
  const selectedCount = selectedIds.size;
  const toggle = (jobId, checked) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (checked) next.add(jobId);
      else next.delete(jobId);
      return next;
    });
  };
  const selectAll = () => setSelectedIds(new Set((jobs || []).map((job) => job.id)));
  const clearAll = () => setSelectedIds(new Set());
  const archiveSelected = async () => {
    if (!selectedCount) return;
    const confirmed = await appConfirm({
      title: "Archive stale applications",
      message: `Archive ${selectedCount} stale application${selectedCount === 1 ? "" : "s"} as no response?`,
      confirmLabel: "Archive",
      danger: true
    });
    if (confirmed) onArchive(Array.from(selectedIds));
  };

  return (
    <Modal title="Cleanup Stale Applications" onClose={onClose} wide>
      <div className="modal-copy">
        Applied jobs older than 30 days with no feedback and no interview rounds are selected for cleanup. Jobs still not interviewed after 50 days are moved automatically to declined by employer.
      </div>
      <div className="cleanup-list">
        {(jobs || []).length === 0 ? <p className="empty-inline">No stale applications need cleanup.</p> : jobs.map((job) => (
          <article key={job.id} className="cleanup-row">
            <label className="inline-check">
              <input type="checkbox" checked={selectedIds.has(job.id)} onChange={(event) => toggle(job.id, event.target.checked)} />
            </label>
            <button className="cleanup-main" onClick={() => onOpenJob(job.id)}>
              <strong>{job.title}</strong>
              <span>{job.company || "Unknown company"} · {job.profile_name || "Lane"}</span>
            </button>
            <div>
              <span>Applied</span>
              <strong>{formatDate(job.application_date)}</strong>
            </div>
            <div>
              <span>Age</span>
              <strong>{job.days_since_application || 30}+ days</strong>
            </div>
            <small>{job.next_action || "No active follow-up task"}</small>
          </article>
        ))}
      </div>
      <footer className="modal-actions">
        <button className="secondary" onClick={selectAll}>Select all</button>
        <button className="secondary" onClick={clearAll}>Deselect all</button>
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button className="danger" disabled={!selectedCount} onClick={archiveSelected}><Trash2 size={16} /> Archive selected as no response</button>
      </footer>
    </Modal>
  );
}

function LinkedText({ text }) {
  const parts = String(text || "").split(/(mailto:[^\s),;]+|tel:\+?[\d\s().-]+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/gi);
  return (
    <>
      {parts.map((part, index) => {
        if (/^mailto:/i.test(part)) {
          return <a key={`${part}-${index}`} href={part} onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(part); }}>{part}</a>;
        }
        if (/^tel:/i.test(part)) {
          return <a key={`${part}-${index}`} href={part} onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(part); }}>{part}</a>;
        }
        if (/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(part)) {
          return <a key={`${part}-${index}`} href={`mailto:${part}`} onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(`mailto:${part}`); }}>{part}</a>;
        }
        return <React.Fragment key={`${index}-${part.slice(0, 8)}`}>{part}</React.Fragment>;
      })}
    </>
  );
}

function parseJsonObject(value) {
  if (!value) return {};
  if (typeof value === "object") return value;
  try {
    return JSON.parse(value);
  } catch {
    return { raw: value };
  }
}

function ValueList({ values }) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  if (!list.length) return <p className="empty-inline">No entries yet.</p>;
  return <ul className="compact-list">{list.map((item, index) => <li key={`${index}-${String(item).slice(0, 18)}`}>{String(item)}</li>)}</ul>;
}

function isWeakCompanyName(value) {
  const key = String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  if (!key) return true;
  const weak = new Set(["a", "about", "all", "and", "are", "as", "at", "business", "candidate", "client", "company", "confidential", "employer", "for", "group", "hiring", "if", "in", "it", "join", "new", "our", "people", "position", "role", "team", "the", "their", "this", "we", "who", "with", "work", "you", "your", "unknown"]);
  const words = key.split(" ");
  return weak.has(key) || weak.has(words[0]) || (words.length === 1 && words[0].length < 4);
}

function CompanyPanel({ job, onResearch, researching }) {
  const intelligence = parseJsonObject(job.company_intelligence);
  const evidence = intelligence.evidence || {};
  const aiResearch = intelligence.ai_research || {};
  const employerType = job.employer_type || intelligence.employer_type || "unknown";
  const rawActualCompany = job.actual_company || intelligence.actual_company || "";
  const hasResearched = Boolean(aiResearch.company_summary || intelligence.cached_company_profile);
  const actualCompany = isWeakCompanyName(rawActualCompany)
    ? (employerType === "direct_employer" ? (job.advertiser_company || job.company || "Unknown") : (hasResearched ? "Unknown end client" : "Needs research"))
    : rawActualCompany;
  const advertiser = job.advertiser_company || intelligence.advertiser_company || job.company || "Unknown";
  const confidence = job.company_confidence || intelligence.confidence || "unknown";
  const summary = aiResearch.company_summary || (
    isWeakCompanyName(rawActualCompany) && employerType !== "direct_employer"
      ? `Advertiser is ${advertiser}. Classified as ${employerType.replace("_", " ")} with ${confidence} confidence. End client has not been identified yet.`
      : intelligence.summary
  ) || "No company summary yet.";
  return (
    <div className="workspace-panel company-panel">
      <section className="company-summary">
        <div className="section-head">
          <h3>Company Intelligence</h3>
          <button className="secondary" disabled={researching} onClick={onResearch}>{researching ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />} {researching ? "Researching..." : "Research company"}</button>
        </div>
        <div className="company-grid">
          <div><span>Employer type</span><strong>{employerType.replace("_", " ")}</strong></div>
          <div><span>Confidence</span><strong>{confidence}</strong></div>
          <div><span>Advertiser</span><strong>{advertiser}</strong></div>
          <div><span>End client / employer</span><strong className={actualCompany === "Needs research" || actualCompany === "Unknown end client" ? "muted-value" : ""}>{actualCompany}</strong></div>
          <div><span>Research</span><strong>{researching ? "Running" : hasResearched ? "Researched" : "Not run"}</strong></div>
        </div>
        <p>{summary}</p>
        <p><strong>Application angle:</strong> {aiResearch.application_angle || intelligence.application_angle || "Not yet assessed."}</p>
        {aiResearch.recruiter_warning ? <p><strong>Recruiter warning:</strong> {aiResearch.recruiter_warning}</p> : null}
      </section>
      <section>
        <h3>Evidence</h3>
        <ValueList values={[
          ...(evidence.recruiter_signals || []).map((item) => `Recruiter signal: ${item}`),
          ...(evidence.direct_employer_signals || []).map((item) => `Direct employer signal: ${item}`),
          evidence.named_company_in_ad ? `Named company in ad: ${evidence.named_company_in_ad}` : "",
          ...(evidence.email_domains || []).map((item) => `Email domain: ${item}`),
          evidence.application_domain ? `Application domain: ${evidence.application_domain}` : "",
          ...(aiResearch.evidence || [])
        ].filter(Boolean)} />
      </section>
      <section>
        <h3>Business Context</h3>
        <ValueList values={aiResearch.business_context} />
      </section>
      <section>
        <h3>Questions To Clarify</h3>
        <ValueList values={aiResearch.questions_to_clarify || intelligence.questions_to_clarify} />
      </section>
      <section>
        <h3>Risks</h3>
        <ValueList values={aiResearch.risks || intelligence.risks} />
      </section>
    </div>
  );
}

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

function parseAnalysisReport(text) {
  const value = String(text || "").trim();
  if (!value || value.startsWith("Analysis failed") || value.startsWith("Failed to find JSON")) return null;
  const report = { fields: {}, sections: {}, notes: [], gate: null, fragment: null };
  let scope = report;
  let section = null;
  const ensure = (holder, name) => {
    holder.sections[name] = holder.sections[name] || { text: [], bullets: [] };
    return holder.sections[name];
  };

  for (const raw of value.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line === "Deep Gatekeeper Review:") {
      report.gate = { fields: {}, sections: {} };
      scope = report.gate;
      section = null;
      continue;
    }
    if (line === "Fragment Alignment:") {
      report.fragment = { fields: {}, sections: {} };
      scope = report.fragment;
      section = null;
      continue;
    }
    const bullet = line.match(/^-\s*(.*)$/);
    if (bullet) {
      const item = bullet[1].trim();
      const kv = item.match(/^([A-Za-z][A-Za-z /-]*):\s*(.*)$/);
      if (kv && scope !== report && !section && SCOPE_FIELD_KEYS.has(kv[1])) {
        scope.fields[kv[1]] = kv[2];
      } else if (section && item && item !== "N/A") {
        ensure(scope, section).bullets.push(item);
      }
      continue;
    }
    const heading = line.match(/^([A-Za-z][A-Za-z /-]*):$/);
    if (heading) {
      section = heading[1];
      ensure(scope, section);
      continue;
    }
    const inline = line.match(/^([A-Za-z][A-Za-z /-]*):\s+(.+)$/);
    if (inline && ANALYSIS_TOP_FIELDS.has(inline[1])) {
      report.fields[inline[1]] = inline[2];
      section = null;
      continue;
    }
    if (section) ensure(scope, section).text.push(line);
    else report.notes.push(line);
  }
  return (report.fields["Match Score"] || report.fields["Triage Match Score"]) ? report : null;
}

function actionMeta(action) {
  const value = String(action || "").toLowerCase();
  if (value.startsWith("apply")) return "go";
  if (value.startsWith("prepare")) return "prep";
  if (value.startsWith("research")) return "hold";
  if (value.startsWith("reject")) return "stop";
  return "";
}

function gateDecisionMeta(decision) {
  const value = String(decision || "").toLowerCase();
  if (value.includes("apply")) return { cls: "go", label: "Apply now" };
  if (value.includes("research")) return { cls: "hold", label: "Research first" };
  if (value.includes("reject")) return { cls: "stop", label: "Reject" };
  return { cls: "", label: decision || "Reviewed" };
}

function AnalysisBullets({ title, items, tone = "" }) {
  if (!items?.length) return null;
  return (
    <section className={`analysis-section ${tone}`}>
      <h4>{title}</h4>
      <ul>{items.map((item, index) => <li key={`${index}-${item.slice(0, 16)}`}>{item}</li>)}</ul>
    </section>
  );
}

function EvidenceMatches({ items }) {
  if (!items?.length) return null;
  return (
    <section className="analysis-section pro">
      <h4>Evidence Matches</h4>
      <ul className="evidence-list">
        {items.map((item, index) => {
          const [artefact, requirement] = item.split(/\s*->\s*/);
          return requirement
            ? <li key={index}><span>{artefact}</span><em>→</em><span>{requirement}</span></li>
            : <li key={index}><span>{item}</span></li>;
        })}
      </ul>
    </section>
  );
}

function AnalysisReport({ text, matchScore = null }) {
  const report = useMemo(() => parseAnalysisReport(text), [text]);
  if (!String(text || "").trim()) return <p className="empty-inline">No analysis yet. Run Analyze to score this role.</p>;
  if (!report) return <pre className="analysis">{text}</pre>;

  const fields = report.fields;
  const triageOnly = !fields["Match Score"] && Boolean(fields["Triage Match Score"]);
  const reportScore = parseInt(fields["Match Score"] || fields["Triage Match Score"], 10) || 0;
  const score = matchScore === null || matchScore === undefined ? reportScore : Number(matchScore);
  const para = (holder, name) => (holder.sections[name]?.text || []).join(" ");
  const bullets = (holder, name) => holder.sections[name]?.bullets || [];
  const gate = report.gate;
  const fragment = report.fragment;
  const gateMeta = gate ? gateDecisionMeta(gate.fields["Decision"]) : null;
  const gateCap = gate?.fields["Score Cap Applied"];

  return (
    <div className="analysis-report">
      <header className="analysis-head">
        <span className={`analysis-score ${scoreClass(score)}`}>{score}%</span>
        <div className="analysis-head-meta">
          <strong>{triageOnly ? "Triage score" : `Final match · ${fields["Fit Level"] || "analysed"}`}</strong>
          {fields["Recommended Action"] ? (
            <span className={`action-pill ${actionMeta(fields["Recommended Action"])}`}>{fields["Recommended Action"]}</span>
          ) : null}
          {triageOnly ? <span className="action-pill hold">Triage only — skipped full analysis</span> : null}
        </div>
      </header>

      {para(report, "Suitability Summary") ? <p className="analysis-summary">{para(report, "Suitability Summary")}</p> : null}
      {para(report, "Triage Result") ? <p className="analysis-summary">{para(report, "Triage Result")}</p> : null}
      {para(report, "High-Fit Rationale") ? (
        <section className="analysis-section highlight">
          <h4>How To Win This One</h4>
          <p>{para(report, "High-Fit Rationale")}</p>
        </section>
      ) : null}

      {bullets(report, "Key Skills Required").length ? (
        <section className="analysis-section">
          <h4>Key Skills The Ad Wants</h4>
          <div className="skill-chips">
            {bullets(report, "Key Skills Required").map((skill, index) => <span key={`${index}-${skill.slice(0, 14)}`}>{skill}</span>)}
          </div>
        </section>
      ) : null}

      <div className="analysis-cols">
        <AnalysisBullets title="Strengths" items={bullets(report, "Strengths")} tone="pro" />
        <AnalysisBullets title="Weaknesses / Risks" items={bullets(report, "Weaknesses / Risks")} tone="con" />
      </div>
      <AnalysisBullets title="Application Focus" items={bullets(report, "Application Focus Points")} />
      <AnalysisBullets title="Resume Focus" items={bullets(report, "Resume Focus")} />
      {para(report, "Cover Letter Angle") ? (
        <section className="analysis-section">
          <h4>Cover Letter Angle</h4>
          <p>{para(report, "Cover Letter Angle")}</p>
        </section>
      ) : null}
      <AnalysisBullets title="Interview Prep" items={bullets(report, "Interview Focus")} />
      {report.notes.length ? <p className="analysis-note">{report.notes.join(" ")}</p> : null}

      {fragment ? (
        <section className="gate-card neutral">
          <header>
            <span className="gate-pill neutral">Fragment Alignment</span>
            {fragment.fields["Fragment Score"] ? <strong>{fragment.fields["Fragment Score"]}</strong> : null}
            {fragment.fields["Confidence"] ? <span className="gate-chip">confidence {fragment.fields["Confidence"]}</span> : null}
          </header>
          <div className="analysis-cols">
            <AnalysisBullets title="Activated Fragments" items={bullets(fragment, "Activated Fragments")} tone="pro" />
            <AnalysisBullets title="Capability Gaps" items={bullets(fragment, "Fragment Capability Gaps")} tone="con" />
          </div>
          {para(fragment, "Fragment Angle") ? <p className="gate-angle">{para(fragment, "Fragment Angle")}</p> : null}
        </section>
      ) : null}

      {gate ? (
        <section className={`gate-card ${gateMeta.cls}`}>
          <header>
            <span className={`gate-pill ${gateMeta.cls}`}>Gate: {gateMeta.label}</span>
            {gate.fields["Gate Score"] ? <strong>{gate.fields["Gate Score"]}</strong> : null}
            {gate.fields["Original Full-Analysis Score"] ? <span className="gate-chip">first pass {gate.fields["Original Full-Analysis Score"]}</span> : null}
            {gateCap && gateCap !== "None" ? <span className="gate-chip">capped at {gateCap}</span> : null}
            {gate.fields["Application ROI"] ? <span className="gate-chip">ROI {gate.fields["Application ROI"]}</span> : null}
            {gate.fields["Confidence"] ? <span className="gate-chip">confidence {gate.fields["Confidence"]}</span> : null}
          </header>
          {gate.fields["Application Angle"] && gate.fields["Application Angle"] !== "N/A" ? (
            <p className="gate-angle">{gate.fields["Application Angle"]}</p>
          ) : null}
          {gate.fields["Reason"] && gate.fields["Reason"] !== "N/A" ? <p className="gate-reason">{gate.fields["Reason"]}</p> : null}
          <AnalysisBullets title="Knockouts" items={bullets(gate, "Gatekeeper Knockouts")} tone="stop" />
          <div className="analysis-cols">
            <EvidenceMatches items={bullets(gate, "Evidence Matches")} />
            <AnalysisBullets title="Missing / Weak Evidence" items={bullets(gate, "Missing / Weak Evidence")} tone="con" />
          </div>
          <AnalysisBullets title="False-Positive Risks" items={bullets(gate, "False Positive Risks")} tone="con" />
          {(gate.fields["Role Family"] || gate.fields["Seniority Fit"]) ? (
            <p className="gate-meta">
              {[gate.fields["Role Family"], gate.fields["Seniority Fit"]].filter((part) => part && part !== "N/A").join(" · ")}
            </p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function hasCompanyResearch(job) {
  // Compact list rows carry a precomputed flag (the full JSON blob is no
  // longer shipped with lists); full detail rows still have the blob.
  if (job && job.has_company_research !== undefined) return Boolean(job.has_company_research);
  const intelligence = parseJsonObject(job?.company_intelligence);
  return Boolean(intelligence.ai_research || intelligence.cached_company_profile);
}

function toDateTimeInputValue(value) {
  if (!value) return "";
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return `${text}T00:00`;
  return text.replace(" ", "T").slice(0, 16);
}

function WorkspaceModal({ job, events, interviews, profiles, activeTab, setActiveTab, onClose, onSave, onApplicationDateApplied, onGenerateDocs, onGeneratePrompt, onCompanyResearch, onAddEvent, onAddInterview, onUpdateInterview, onDocumentDrop, onViewDocument, onDownloadDocument, onRevealDocument, onAnalyzeJob, onMoveProfile, analyzing, generatingDocs, researchingCompany, documentAiName, onRejectJob, onMoveInterested }) {
  const [form, setForm] = useState(job || {});
  const [targetProfileId, setTargetProfileId] = useState(job?.profile_id || "");
  const [profileMoving, setProfileMoving] = useState(false);
  const [eventText, setEventText] = useState("");
  const [selectedInterviewId, setSelectedInterviewId] = useState(null);
  const applicationDatePromptedRef = useRef(false);
  const [interviewForm, setInterviewForm] = useState({
    title: "",
    interview_date: "",
    interview_type: "Video",
    people_met: "",
    notes: "",
    outcome: "",
    next_action: "Follow up",
    next_action_date: ""
  });

  useEffect(() => setForm(job || {}), [job]);
  useEffect(() => setTargetProfileId(job?.profile_id || ""), [job?.id, job?.profile_id]);
  useEffect(() => {
    setSelectedInterviewId(null);
    applicationDatePromptedRef.current = false;
    resetInterviewForm();
  }, [job?.id]);
  useEffect(() => {
    if (!selectedInterviewId) return;
    const selected = (interviews || []).find((interview) => interview.id === selectedInterviewId);
    if (!selected) {
      setSelectedInterviewId(null);
      resetInterviewForm();
      return;
    }
    setInterviewForm(interviewToForm(selected));
  }, [interviews, selectedInterviewId]);
  if (!job) return null;

  function interviewToForm(interview) {
    return {
      title: interview.title || "",
      interview_date: toDateTimeInputValue(interview.interview_date),
      interview_type: interview.interview_type || "Video",
      people_met: interview.people_met || "",
      notes: interview.notes || "",
      outcome: interview.outcome || "",
      next_action: interview.next_action || "Follow up",
      next_action_date: interview.next_action_date || ""
    };
  }

  function emptyInterviewForm() {
    return {
      title: "",
      interview_date: "",
      interview_type: "Video",
      people_met: "",
      notes: "",
      outcome: "",
      next_action: "Follow up",
      next_action_date: ""
    };
  }

  function resetInterviewForm() {
    setInterviewForm(emptyInterviewForm());
  }

  const set = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const setClosingDate = (value) => setForm((current) => ({
    ...current,
    closing_date: value,
    closing_date_source: value === (job?.closing_date || "") ? current.closing_date_source : "assigned"
  }));
  const setInterview = (key, value) => setInterviewForm((current) => ({ ...current, [key]: value }));
  const save = () => onSave(form);
  const setApplicationDate = async (value) => {
    const shouldOfferAppliedMove =
      value
      && value !== (job?.application_date || "")
      && normalizeStage(form.pipeline_stage, form.status) !== "applied"
      && !applicationDatePromptedRef.current;

    if (!shouldOfferAppliedMove) {
      set("application_date", value);
      return;
    }

    applicationDatePromptedRef.current = true;
    const shouldMoveToApplied = await appConfirm({
      title: "Move to Applied?",
      message: "Move this application to Applied now?",
      confirmLabel: "Move to Applied"
    });
    const nextForm = shouldMoveToApplied
      ? {
          ...form,
          application_date: value,
          pipeline_stage: "applied",
          status: "applied",
          next_action: form.next_action || "Follow up",
          next_action_date: form.next_action_date || todayPlus(7)
        }
      : { ...form, application_date: value };

    setForm(nextForm);
    if (shouldMoveToApplied) {
      onApplicationDateApplied(value).catch((error) => {
        appNotice({ title: "Could not move to Applied", message: toErrorMessage(error) });
      });
    }
  };
  const changeProfile = async (value) => {
    const nextProfileId = Number(value);
    const previousProfileId = targetProfileId;
    setTargetProfileId(nextProfileId);
    if (!nextProfileId || nextProfileId === Number(job.profile_id)) return;
    setProfileMoving(true);
    try {
      await onMoveProfile(nextProfileId);
    } catch (error) {
      setTargetProfileId(previousProfileId);
    } finally {
      setProfileMoving(false);
    }
  };
  const submitInterview = () => {
    if (selectedInterviewId) {
      onUpdateInterview(selectedInterviewId, interviewForm);
    } else {
      onAddInterview(interviewForm);
      resetInterviewForm();
    }
  };
  const startNewInterview = () => {
    setSelectedInterviewId(null);
    resetInterviewForm();
  };

  return (
    <Modal title="Application Workspace" onClose={onClose} wide>
      <div className="workspace-title">
        <div>
          <h2>{job.title}</h2>
          <p>{job.company || "Unknown company"} · {job.profile_name || "Lane"} · <ScoreStack job={job} /></p>
        </div>
        <div className="button-row">
          <button className="secondary" disabled={analyzing} onClick={onAnalyzeJob}>{analyzing ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />} {analyzing ? "Thinking..." : job.ai_analysis ? "Re-analyze" : "Analyze"}</button>
          <button onClick={() => window.jobAssistant.openExternal(job.url)}><ExternalLink size={16} /> Open job</button>
          <button className="danger" onClick={() => onRejectJob(job)}><X size={16} /> Reject</button>
          {canMoveToInterested(job) ? <button className="secondary" onClick={() => onMoveInterested(job)}><ChevronRight size={16} /> Interested</button> : null}
        </div>
      </div>
      <nav className="workspace-tabs">
        {WORKSPACE_TABS.map((tab) => <button key={tab} className={activeTab === tab ? "active" : ""} onClick={() => setActiveTab(tab)}>{tab}</button>)}
      </nav>

      {activeTab === "Details" ? (
        <div className="workspace-panel two-col">
          <section>
            <h3>Analysis</h3>
            {analyzing ? (
              <div className="thinking-card">
                <Loader2 className="spin" size={18} />
                <div><strong>Analyzing fit...</strong><span>Running triage and full fit analysis if the role clears the threshold.</span></div>
              </div>
            ) : null}
            <AnalysisReport text={job.ai_analysis} matchScore={job.match_score} />
            <h3>Description</h3>
            <p className="description"><LinkedText text={job.description || "No description captured."} /></p>
          </section>
          <section className="form-grid stacked">
            <label><span>Stage</span><select value={form.pipeline_stage || "new"} onChange={(event) => set("pipeline_stage", event.target.value)}>{PIPELINE.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
            <label><span>Priority</span><select value={form.priority || "normal"} onChange={(event) => set("priority", event.target.value)}><option>high</option><option>normal</option><option>low</option></select></label>
            <label><span>Lane</span><select value={targetProfileId} disabled={profileMoving} onChange={(event) => changeProfile(event.target.value)}>{(profiles || []).map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>
            {profileMoving ? <p className="field-note">Moving lane...</p> : null}
            <label>
              <span className="label-row">Closing date <ClosingDateSourceBadge source={form.closing_date_source} /></span>
              <input type="date" value={form.closing_date || ""} onChange={(event) => setClosingDate(event.target.value)} />
            </label>
            <label><span>Salary / rate</span><input value={form.salary || ""} onChange={(event) => set("salary", event.target.value)} /></label>
            <button onClick={save}><Check size={16} /> Save details</button>
          </section>
        </div>
      ) : null}

      {activeTab === "Company" ? (
        <CompanyPanel job={job} onResearch={onCompanyResearch} researching={researchingCompany} />
      ) : null}

      {activeTab === "Application" ? (
        <div className="workspace-panel form-grid">
          <label><span>Application date</span><input type="date" value={form.application_date || ""} onChange={(event) => setApplicationDate(event.target.value)} /></label>
          <label><span>Application URL</span><input value={form.application_url || ""} onChange={(event) => set("application_url", event.target.value)} /></label>
          <label><span>Contact person</span><input value={form.contact_person || ""} onChange={(event) => set("contact_person", event.target.value)} /></label>
          <label><span>Contact email</span><input value={form.contact_email || ""} onChange={(event) => set("contact_email", event.target.value)} /></label>
          <label><span>Contact phone</span><input value={form.contact_phone || ""} onChange={(event) => set("contact_phone", event.target.value)} /></label>
          <label><span>Salary / rate</span><input value={form.salary || ""} onChange={(event) => set("salary", event.target.value)} /></label>
          <label className="full candidate-context-field">
            <div className="candidate-context-heading">
              <span>Additional candidate evidence</span>
              <small>Optional</small>
            </div>
            <textarea
              id="additional-candidate-context"
              rows={4}
              maxLength={8000}
              value={form.additional_candidate_context || ""}
              placeholder="Add truthful details that are relevant to this application but missing from your base resume — for example recent achievements, project context, domain exposure, tools, qualifications in progress, or availability."
              aria-describedby="additional-candidate-context-help"
              onChange={(event) => set("additional_candidate_context", event.target.value)}
            />
            <p id="additional-candidate-context-help">
              Saved with this application and treated as candidate-supplied evidence when generating documents or an LLM prompt. It does not alter your base resume.
            </p>
          </label>
          <div className="full document-grid">
            <DropZone
              label="Cover letter"
              value={form.cover_letter_path}
              text={form.cover_letter_text}
              onDrop={(file) => onDocumentDrop("cover_letter", file)}
              onView={() => onViewDocument("Cover letter text", form.cover_letter_text)}
              onDownload={() => onDownloadDocument(form.cover_letter_path)}
              onReveal={() => onRevealDocument(form.cover_letter_path)}
            />
            <DropZone
              label="Resume"
              value={form.resume_used}
              text={form.resume_text}
              onDrop={(file) => onDocumentDrop("resume", file)}
              onView={() => onViewDocument("Resume text", form.resume_text)}
              onDownload={() => onDownloadDocument(form.resume_used)}
              onReveal={() => onRevealDocument(form.resume_used)}
            />
            <DropZone
              label="Position description"
              value={form.position_description_path}
              text={form.position_description_text}
              onDrop={(file) => onDocumentDrop("position_description", file)}
              onView={() => onViewDocument("Position description text", form.position_description_text)}
              onDownload={() => onDownloadDocument(form.position_description_path)}
              onReveal={() => onRevealDocument(form.position_description_path)}
            />
          </div>
          <label><span>Next action</span><input value={form.next_action || ""} onChange={(event) => set("next_action", event.target.value)} /></label>
          <label><span>Next action date</span><input type="date" value={form.next_action_date || ""} onChange={(event) => set("next_action_date", event.target.value)} /></label>
          <footer className="full button-row">
            <button onClick={save}><Check size={16} /> Save application</button>
            <button className="secondary" disabled={generatingDocs} onClick={() => onGenerateDocs(form.additional_candidate_context || "")}>{generatingDocs ? <Loader2 className="spin" size={16} /> : <FileText size={16} />} {generatingDocs ? "Generating..." : "Generate documents"}</button>
            <button className="secondary" disabled={!form.resume_used && !form.cover_letter_path} onClick={() => window.jobAssistant.showPath(form.resume_used || form.cover_letter_path || "applications")}><ExternalLink size={16} /> Open documents</button>
            <button className="secondary" onClick={() => onGeneratePrompt(form.additional_candidate_context || "")}><FileText size={16} /> Save LLM prompt</button>
          </footer>
          <div className="full ai-provider-note">
            Documents are generated with <strong>{documentAiName}</strong>, grounded in your prior applications and fact-checked against your evidence.
          </div>
        </div>
      ) : null}

      {activeTab === "Interviews" ? (
        <div className="workspace-panel interview-workspace">
          <section className="interview-list">
            {(interviews || []).length === 0 ? <p className="empty-inline">No interview rounds yet.</p> : interviews.map((interview) => (
              <button
                key={interview.id}
                className={selectedInterviewId === interview.id ? "interview-row selected" : "interview-row"}
                onClick={() => {
                  setSelectedInterviewId(interview.id);
                  setInterviewForm(interviewToForm(interview));
                }}
              >
                <div>
                  <strong>{interview.title || `Interview ${interview.round_number}`}</strong>
                  <span>{formatDate(interview.interview_date)} · {interview.interview_type || "Type not set"}</span>
                </div>
                <p>{interview.people_met ? `People: ${interview.people_met}` : "People not recorded"}</p>
                {interview.notes ? <p>{interview.notes}</p> : null}
                {interview.outcome ? <small>Outcome: {interview.outcome}</small> : null}
              </button>
            ))}
          </section>
          <section className="form-grid">
            <div className="full section-head interview-editor-head">
              <h3>{selectedInterviewId ? "Edit interview round" : "Add interview round"}</h3>
              {selectedInterviewId ? <button className="secondary" onClick={startNewInterview}><Plus size={16} /> New round</button> : null}
            </div>
            <label><span>Round title</span><input value={interviewForm.title} placeholder={`Interview ${(interviews || []).length + 1}`} onChange={(event) => setInterview("title", event.target.value)} /></label>
            <label><span>Interview date</span><input type="datetime-local" value={interviewForm.interview_date} onChange={(event) => setInterview("interview_date", event.target.value)} /></label>
            <label><span>Interview type</span><input value={interviewForm.interview_type} onChange={(event) => setInterview("interview_type", event.target.value)} /></label>
            <label><span>People met with</span><input value={interviewForm.people_met} onChange={(event) => setInterview("people_met", event.target.value)} /></label>
            <label><span>Outcome</span><input value={interviewForm.outcome} onChange={(event) => setInterview("outcome", event.target.value)} /></label>
            <label><span>Next action date</span><input type="date" value={interviewForm.next_action_date} onChange={(event) => setInterview("next_action_date", event.target.value)} /></label>
            <label><span>Next action</span><input value={interviewForm.next_action} onChange={(event) => setInterview("next_action", event.target.value)} /></label>
            <label className="full"><span>Notes</span><textarea value={interviewForm.notes} onChange={(event) => setInterview("notes", event.target.value)} /></label>
            <button onClick={submitInterview}>{selectedInterviewId ? <Check size={16} /> : <Plus size={16} />} {selectedInterviewId ? "Save interview round" : "Add interview round"}</button>
          </section>
        </div>
      ) : null}

      {activeTab === "Feedback" ? (
        <div className="workspace-panel form-grid">
          <label className="full"><span>Feedback</span><textarea value={form.feedback || ""} onChange={(event) => set("feedback", event.target.value)} /></label>
          <label><span>Next action</span><input value={form.next_action || ""} onChange={(event) => set("next_action", event.target.value)} /></label>
          <label><span>Due date</span><input type="date" value={form.next_action_date || ""} onChange={(event) => set("next_action_date", event.target.value)} /></label>
          <button onClick={save}><Check size={16} /> Save feedback</button>
        </div>
      ) : null}

      {activeTab === "Notes" ? (
        <div className="workspace-panel form-grid">
          <label className="full"><span>Notes</span><textarea value={form.notes || ""} onChange={(event) => set("notes", event.target.value)} /></label>
          <label className="full"><span>Add timeline note</span><textarea value={eventText} onChange={(event) => setEventText(event.target.value)} /></label>
          <footer className="full button-row">
            <button onClick={save}><Check size={16} /> Save notes</button>
            <button className="secondary" onClick={() => { onAddEvent(eventText); setEventText(""); }} disabled={!eventText.trim()}><Plus size={16} /> Add event</button>
          </footer>
        </div>
      ) : null}

      {activeTab === "Timeline" ? (
        <div className="workspace-panel timeline">
          {events.length === 0 ? <p className="empty-inline">No timeline events yet.</p> : events.map((event) => (
            <article key={event.id} className="timeline-row">
              <time>{formatDate(event.event_date || event.created_at)}</time>
              <strong>{event.title}</strong>
              {event.details ? <p>{event.details}</p> : null}
            </article>
          ))}
        </div>
      ) : null}
    </Modal>
  );
}

function Dashboard({ dashboard, calendar, onOpenJob, onOpenCleanup }) {
  const stageCounts = dashboard?.stage_counts || {};
  const cleanupCount = (dashboard?.cleanup_due || []).length;
  return (
    <section className="dashboard">
      <div className="metric-grid">
        {PIPELINE.slice(0, 8).map((stage) => (
          <article key={stage.id} className="metric">
            <span>{stage.label}</span>
            <strong>{stageCounts[stage.id] || 0}</strong>
          </article>
        ))}
      </div>

      {cleanupCount ? (
        <button className="cleanup-banner" onClick={onOpenCleanup}>
          <div>
            <strong>{cleanupCount} stale application{cleanupCount === 1 ? "" : "s"} need cleanup</strong>
            <span>Applied over 30 days with no feedback or interviews; 50-day no-interview applications auto-move to declined.</span>
          </div>
          <Trash2 size={18} />
        </button>
      ) : null}

      <div className="dashboard-grid">
        <section className="dash-section">
          <h2><CalendarDays size={18} /> Calendar / To-do</h2>
          {(calendar || []).slice(0, 10).map((item) => (
            <button key={`${item.id}-${item.next_action_date || item.interview_date || item.closing_date}`} className="agenda-row" onClick={() => onOpenJob(item.id)}>
              <time>{formatDate(item.next_action_date || item.interview_date || item.closing_date)}</time>
              <span>{item.interview_round ? `Interview ${item.interview_round}` : (item.next_action || "Closing date")}</span>
              <small>{item.title} · {item.profile_name}</small>
            </button>
          ))}
        </section>

        <section className="dash-section">
          <h2><Sparkles size={18} /> Top Matches</h2>
          {(dashboard?.top_matches || []).slice().sort((left, right) => primaryScore(right) - primaryScore(left)).map((job) => (
            <button key={job.id} className="compact-job" onClick={() => onOpenJob(job.id)}>
              <strong>{job.title}</strong>
              <span>{job.company} · <ScoreStack job={job} compact /></span>
            </button>
          ))}
        </section>

        <section className="dash-section">
          <h2><NotebookTabs size={18} /> Awaiting Feedback</h2>
          {(dashboard?.awaiting_feedback || []).map((job) => (
            <button key={job.id} className="compact-job" onClick={() => onOpenJob(job.id)}>
              <strong>{job.title}</strong>
              <span>{job.pipeline_stage?.replace("_", " ")} · {job.profile_name}</span>
            </button>
          ))}
        </section>

        <section className="dash-section">
          <h2><RefreshCw size={18} /> Scraper Status</h2>
          {dashboard?.last_scrape ? (
            <div className="scrape-status">
              <strong>{dashboard.last_scrape.status}</strong>
              <span>{formatDate(dashboard.last_scrape.finished_at || dashboard.last_scrape.started_at)}</span>
              <p>{dashboard.last_scrape.summary || dashboard.last_scrape.sources || "No summary recorded."}</p>
            </div>
          ) : <p className="empty-inline">No scraper run recorded yet.</p>}
        </section>
      </div>
    </section>
  );
}

function CampaignSection({ title, icon, items, empty, children }) {
  return (
    <section className="campaign-section">
      <header>
        <h2>{icon} {title}</h2>
        <strong>{items?.length || 0}</strong>
      </header>
      {children || ((items || []).length ? null : <p className="empty-inline">{empty}</p>)}
    </section>
  );
}

function HiddenMarketTarget({ name, meta, detail, titles, chip, tracked, onTrack, onStrategy, strategy, strategyBusy }) {
  return (
    <article className="hidden-target">
      <div>
        <strong>{name}</strong>
        {chip ? <span className={`hm-chip ${chip.tone || ""}`}>{chip.label}</span> : null}
        <span>{meta}</span>
      </div>
      {detail ? <p><LinkedText text={detail} /></p> : null}
      {titles?.length ? <small>{titles.join(" · ")}</small> : null}
      {(onTrack || onStrategy) ? (
        <div className="hidden-target-actions">
          {onTrack ? (
            <button className="secondary" disabled={tracked} onClick={onTrack}>
              {tracked ? <><Check size={14} /> Tracking</> : <><Plus size={14} /> Track</>}
            </button>
          ) : null}
          {onStrategy ? (
            <button className="secondary" disabled={strategyBusy} onClick={onStrategy}>
              {strategyBusy ? <Loader2 className="spin" size={14} /> : <Lightbulb size={14} />} AI angle
            </button>
          ) : null}
        </div>
      ) : null}
      {strategy ? <div className="hm-strategy"><LinkedText text={strategy} /></div> : null}
    </article>
  );
}

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

function HiddenMarketLeadCard({ lead, onUpdate, onTouch, onConvert, onDelete, onOpenJob }) {
  const [note, setNote] = useState("");
  const [touchStatus, setTouchStatus] = useState(lead.status === "done" ? "contacted" : lead.status || "contacted");
  const [touchDate, setTouchDate] = useState("");
  const [showLog, setShowLog] = useState(false);
  const [busy, setBusy] = useState(false);
  const touchpoints = lead.touchpoints || [];
  const isDone = lead.status === "done";

  const logTouch = async () => {
    if (!note.trim()) return;
    setBusy(true);
    try {
      await onTouch(lead.id, { note: note.trim(), status: touchStatus, next_step_date: touchDate || null });
      setNote(""); setTouchDate("");
    } finally { setBusy(false); }
  };

  return (
    <article className={`hm-lead ${isDone ? "done" : ""}`}>
      <div className="hm-lead-head">
        <div className="hm-lead-title">
          <strong>{lead.target_name}</strong>
          <span className="hm-chip soft">{HM_TYPE_LABELS[lead.target_type] || lead.target_type}</span>
          {lead.outcome ? <span className={`hm-chip ${lead.outcome === "converted" ? "good" : ""}`}>{HM_OUTCOME_LABELS[lead.outcome] || lead.outcome}</span> : null}
        </div>
        <div className="hm-lead-controls">
          <select value={lead.status || "todo"} aria-label="Lead status" onChange={(event) => onUpdate(lead.id, { status: event.target.value })}>
            {Object.entries(HM_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          {isDone ? (
            <select value={lead.outcome || ""} aria-label="Outcome" onChange={(event) => onUpdate(lead.id, { outcome: event.target.value })}>
              {Object.entries(HM_OUTCOME_LABELS).map(([value, label]) => <option key={value || "none"} value={value}>{label}</option>)}
            </select>
          ) : null}
        </div>
      </div>

      {lead.action ? <p className="hm-lead-action">{lead.action}</p> : null}
      {[lead.contact_person, lead.contact_email, lead.contact_phone].filter(Boolean).length ? (
        <p className="hm-lead-contact"><LinkedText text={[lead.contact_person, lead.contact_email, lead.contact_phone].filter(Boolean).join(" · ")} /></p>
      ) : null}

      <label className="hm-lead-notes"><span>Notes</span>
        <textarea rows={2} defaultValue={lead.notes || ""} placeholder="Running notes about this lead..."
          onBlur={(event) => { if ((event.target.value || "") !== (lead.notes || "")) onUpdate(lead.id, { notes: event.target.value }); }} />
      </label>

      {touchpoints.length ? (
        <button className="hm-log-toggle" onClick={() => setShowLog((value) => !value)}>
          <ChevronRight size={13} className={showLog ? "rot90" : ""} /> {touchpoints.length} touchpoint{touchpoints.length === 1 ? "" : "s"}
        </button>
      ) : null}
      {showLog ? (
        <ul className="hm-touchlog">
          {touchpoints.map((tp, index) => (
            <li key={index}>
              <time>{formatDate(tp.at)}</time>
              {tp.status ? <span className="hm-chip soft">{HM_STATUS_LABELS[tp.status] || tp.status}</span> : null}
              <span>{tp.note}</span>
              {tp.next_step_date ? <em>next: {formatDate(tp.next_step_date)}</em> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {!isDone ? (
        <div className="hm-touch-form">
          <input value={note} placeholder="Log a touchpoint (what happened)..." onChange={(event) => setNote(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") logTouch(); }} />
          <select value={touchStatus} aria-label="Touch status" onChange={(event) => setTouchStatus(event.target.value)}>
            <option value="contacted">Contacted</option>
            <option value="awaiting">Awaiting reply</option>
          </select>
          <input type="date" value={touchDate} aria-label="Next step date" title="Next step date" onChange={(event) => setTouchDate(event.target.value)} />
          <button className="secondary" disabled={busy || !note.trim()} onClick={logTouch}>{busy ? <Loader2 className="spin" size={14} /> : <Send size={14} />} Log</button>
        </div>
      ) : null}

      <div className="hm-lead-footer">
        {lead.next_step_date && !isDone ? <span className="hm-next"><CalendarClock size={13} /> next {formatDate(lead.next_step_date)}</span> : <span />}
        <div className="hm-lead-actions">
          {lead.converted_job_id ? (
            <button className="secondary" onClick={() => onOpenJob(lead.converted_job_id)}><ExternalLink size={14} /> Open job</button>
          ) : (
            <button className="secondary" onClick={() => onConvert(lead)}><ArrowRightLeft size={14} /> Convert to applied</button>
          )}
          <button className="icon danger" aria-label="Delete lead" title="Delete lead" onClick={() => onDelete(lead)}><Trash2 size={14} /></button>
        </div>
      </div>
    </article>
  );
}

function HiddenMarketPanel({ data, busy, days, onDaysChange, onRefresh, onTrack, onStrategy, onLeadUpdate, onTouch, onConvert, onDeleteLead, onOpenJob }) {
  const [strategies, setStrategies] = useState({});
  const [strategyBusy, setStrategyBusy] = useState("");
  const intel = data?.intel || {};
  const overview = data?.overview || {};
  const leads = data?.leads || [];
  const counts = overview.status_counts || {};

  const runStrategy = async (target) => {
    const key = target.target_key || target.name;
    setStrategyBusy(key);
    try {
      const text = await onStrategy(target);
      if (text) setStrategies((current) => ({ ...current, [key]: text }));
    } finally { setStrategyBusy(""); }
  };

  const renderTarget = (item, extra) => (
    <HiddenMarketTarget
      key={item.target_key || item.name}
      name={item.name}
      tracked={item.tracked}
      onTrack={() => onTrack(item)}
      onStrategy={() => runStrategy(item)}
      strategy={strategies[item.target_key || item.name]}
      strategyBusy={strategyBusy === (item.target_key || item.name)}
      {...extra}
    />
  );

  return (
    <section className="campaign-view hidden-market-view">
      <div className="campaign-hero">
        <div className="plan-hero-main">
          <h2><Radar size={20} /> Hidden Market</h2>
          <p>Outreach intelligence mined from every advert seen — including the reject pile. Track leads here; they have their own to-do flow, separate from the application pipeline.</p>
          <div className="plan-progress hm-overview">
            <span className="gate-chip">{overview.targets_surfaced || 0} targets surfaced</span>
            <span className="gate-chip">{overview.tracked_total || 0} tracked</span>
            <span className="gate-chip">{overview.open_total || 0} open</span>
            <span className="gate-chip">{counts.todo || 0} to do · {counts.contacted || 0} contacted · {counts.awaiting || 0} awaiting · {counts.done || 0} done</span>
            {overview.due_followups ? <span className="gate-chip warn">{overview.due_followups} follow-up{overview.due_followups === 1 ? "" : "s"} due</span> : null}
            {overview.converted ? <span className="gate-chip good">{overview.converted} converted</span> : null}
          </div>
        </div>
        <div className="campaign-actions">
          <label className="hm-window"><span>Window</span>
            <select value={days} onChange={(event) => onDaysChange(Number(event.target.value))}>
              <option value={30}>30 days</option>
              <option value={60}>60 days</option>
              <option value={90}>90 days</option>
            </select>
          </label>
          <button className="secondary" disabled={busy} onClick={onRefresh}>{busy ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />} Rescan</button>
        </div>
      </div>

      <section className="campaign-section hm-todo">
        <header>
          <h2><ListTodo size={18} /> Outreach To-Do</h2>
          <strong>{leads.length}</strong>
        </header>
        {!leads.length ? (
          <p className="empty-inline">No outreach leads yet. Track a recruiter, employer, or leadership-gap target below to start a to-do.</p>
        ) : (
          <div className="hm-lead-list">
            {leads.map((lead) => (
              <HiddenMarketLeadCard key={lead.id} lead={lead} onUpdate={onLeadUpdate} onTouch={onTouch} onConvert={onConvert} onDelete={onDeleteLead} onOpenJob={onOpenJob} />
            ))}
          </div>
        )}
      </section>

      <header className="hm-head">
        <span>Mined from the last {overview.window_days || days} days. Identities are cross-checked against contact domains and ad language. "Track" adds a target to the to-do above; "AI angle" asks the local model for an approach.</span>
      </header>
      <div className="hm-grid">
        <CampaignSection title="Recruiter Ledger" icon={<Send size={18} />} items={intel.recruiters} empty="No recruiters carrying relevant roles in the window yet.">
          {(intel.recruiters || []).map((recruiter) => renderTarget(recruiter, {
            meta: `${recruiter.roles} relevant role${recruiter.roles === 1 ? "" : "s"} · best ${recruiter.best_score}% · ${formatDate(recruiter.last_seen)}`,
            detail: [recruiter.contact_person, recruiter.contact_email, recruiter.contact_phone].filter(Boolean).join(" · ") || "No direct contact captured — find the consultant on the agency site.",
            titles: recruiter.sample_titles,
          }))}
        </CampaignSection>

        <CampaignSection title="Direct Employer Watchlist" icon={<BriefcaseBusiness size={18} />} items={intel.direct_employers} empty="No verified direct employers with relevant roles in the window yet.">
          {(intel.direct_employers || []).map((employer) => renderTarget(employer, {
            chip: employer.verified === "contact domain" ? { label: "Verified · contact domain", tone: "good" } : { label: "Unconfirmed · ad signals", tone: "soft" },
            meta: `${employer.roles} relevant role${employer.roles === 1 ? "" : "s"} · best ${employer.best_score}% · ${formatDate(employer.last_seen)}${employer.domain ? ` · ${employer.domain}` : ""}`,
            detail: `Has hired this role family${employer.locations?.length ? ` (${employer.locations.join(", ")})` : ""} — a direct approach beats the next ad.`,
            titles: employer.sample_titles,
          }))}
        </CampaignSection>

        <CampaignSection title="Leadership Gap Signals" icon={<Target size={18} />} items={intel.leadership_gaps} empty="No employers showing a junior-heavy, leaderless hiring pattern in the window.">
          {(intel.leadership_gaps || []).map((gap) => renderTarget(gap, {
            meta: `${gap.ic_count} junior/IC tech hires, no leadership posting · ${formatDate(gap.last_seen)}${gap.domain ? ` · ${gap.domain}` : ""}`,
            detail: "Hiring hands without a head — a speculative leadership approach may land before any ad exists.",
            titles: gap.sample_titles,
          }))}
        </CampaignSection>
      </div>
    </section>
  );
}

const PLAN_KIND_META = {
  interview: { label: "Interview", cls: "u0" },
  offer: { label: "Offer", cls: "u0" },
  closing: { label: "Closing soon", cls: "u1" },
  overdue: { label: "Due now", cls: "u2" },
  followup: { label: "Follow up", cls: "u3" },
  stage: { label: "New opportunity", cls: "u4" }
};

function PlanItem({ item, rank, docsBusy, onOpenJob, onStageJob, onFollowedUp, onGenerateDocs, onGeneratePack }) {
  const meta = PLAN_KIND_META[item.kind] || { label: item.kind, cls: "u4" };
  const job = item.job;
  return (
    <article className={`plan-item ${meta.cls}`}>
      <span className="plan-rank">{rank}</span>
      <div className="plan-main">
        <div className="plan-title-row">
          <strong>{item.title}</strong>
          <span className={`plan-kind ${meta.cls}`}>{meta.label}</span>
          {item.due ? <time>{item.kind === "followup" ? "today" : formatDate(item.due)}</time> : null}
        </div>
        <p>{item.detail}</p>
      </div>
      <div className="plan-actions">
        {job ? (
          <>
            {item.kind === "interview" ? (
              <button onClick={() => onOpenJob(job.id, "Interviews")}><Sparkles size={15} /> Prepare</button>
            ) : null}
            {item.kind === "offer" ? (
              <button onClick={() => onOpenJob(job.id)}><Check size={15} /> Review</button>
            ) : null}
            {item.kind === "closing" ? (
              <>
                {job.pipeline_stage === "new" ? <button onClick={() => onStageJob(job)}><Target size={15} /> Stage</button> : null}
                <button className="secondary" disabled={docsBusy} onClick={() => onGenerateDocs(job)}><FileText size={15} /> Docs</button>
              </>
            ) : null}
            {item.kind === "overdue" || item.kind === "followup" ? (
              <button onClick={() => onFollowedUp(job)}><Check size={15} /> Done</button>
            ) : null}
            {item.kind === "stage" ? (
              <>
                <button onClick={() => onStageJob(job)}><Target size={15} /> Stage</button>
                <button className="secondary" onClick={() => onGeneratePack(job)}><FileText size={15} /> Pack</button>
              </>
            ) : null}
            <button className="secondary" onClick={() => onOpenJob(job.id)}>Open</button>
          </>
        ) : null}
      </div>
    </article>
  );
}

function CampaignPanel({ plan, busy, docsBusy, onStageAttack, onRefreshActions, onOpenJob, onStageJob, onFollowedUp, onGenerateDocs, onGeneratePack }) {
  const progress = plan?.progress || {};
  const goal = progress.weekly_goal || 6;
  const goalPct = Math.min(100, Math.round(((progress.applied_week || 0) / goal) * 100));

  return (
    <section className="campaign-view">
      <div className="campaign-hero">
        <div className="plan-hero-main">
          <h2><Target size={20} /> Today's Plan</h2>
          <p>The kanban is the database — this is what to actually do next, in order.</p>
          <div className="plan-progress">
            <div className="plan-goal">
              <span>Applications this week: <strong>{progress.applied_week || 0}</strong> / {goal}</span>
              <span className="stat-bar-track"><span className="stat-bar-fill" style={{ width: `${goalPct}%` }} /></span>
            </div>
            <span className="gate-chip">{progress.actions_today || 0} action{progress.actions_today === 1 ? "" : "s"} today</span>
            <span className="gate-chip">{progress.interviews_upcoming || 0} interview{progress.interviews_upcoming === 1 ? "" : "s"} ahead</span>
            <span className="gate-chip">{progress.queue_depth || 0} in the new queue</span>
          </div>
        </div>
        <div className="campaign-actions">
          <button disabled={busy} onClick={onStageAttack}><Target size={16} /> Stage Top Roles</button>
          <button className="secondary" disabled={busy} onClick={onRefreshActions}><Send size={16} /> Refresh Follow-Ups</button>
        </div>
      </div>

      <section className="plan-list">
        {!plan ? <p className="empty-inline">{busy ? "Building today's plan..." : "Loading today's plan..."}</p> : null}
        {plan && !(plan.plan || []).length ? (
          <p className="empty-inline">Nothing urgent on the board. Run a search to feed the queue, or open the Hidden Market tab for outreach targets.</p>
        ) : null}
        {(plan?.plan || []).map((item, index) => (
          <PlanItem
            key={`${item.kind}-${item.job?.id || index}`}
            item={item}
            rank={index + 1}
            docsBusy={docsBusy}
            onOpenJob={onOpenJob}
            onStageJob={onStageJob}
            onFollowedUp={onFollowedUp}
            onGenerateDocs={onGenerateDocs}
            onGeneratePack={onGeneratePack}
          />
        ))}
      </section>

    </section>
  );
}

function StatDelta({ current, previous }) {
  const delta = Number(current || 0) - Number(previous || 0);
  if (!delta) return <small className="stat-delta">level with prior period</small>;
  return (
    <small className={`stat-delta ${delta > 0 ? "up" : "down"}`}>
      {delta > 0 ? "+" : ""}{delta} vs prior period
    </small>
  );
}

function StatBars({ items, labelKey, countKey }) {
  const max = Math.max(1, ...(items || []).map((item) => Number(item[countKey] || 0)));
  return (
    <div className="stat-bars">
      {(items || []).map((item) => (
        <div key={item[labelKey]} className="stat-bar-row">
          <span className="stat-bar-label">{item[labelKey]}</span>
          <span className="stat-bar-track">
            <span className="stat-bar-fill" style={{ width: `${(Number(item[countKey] || 0) / max) * 100}%` }} />
          </span>
          <strong>{item[countKey]}</strong>
        </div>
      ))}
    </div>
  );
}

function StatsPanel({ stats, period, onPeriodChange, busy }) {
  const current = stats?.current || {};
  const previous = stats?.previous || {};
  const strongFits = (bands) => (bands || [])
    .filter((band) => band.band === "78+" || band.band === "70-77")
    .reduce((sum, band) => sum + band.count, 0);
  const conversion = current.applied ? Math.round((current.interviews / current.applied) * 100) : 0;
  const hm = stats?.hidden_market || null;
  const hmCurrent = hm?.current || {};
  const hmPrevious = hm?.previous || {};
  const hmFunnel = hm ? [
    { label: "Surfaced", count: hm.funnel?.surfaced || 0 },
    { label: "Tracked", count: hm.funnel?.tracked || 0 },
    { label: "Contacted+", count: hm.funnel?.contacted_plus || 0 },
    { label: "Replied/meeting", count: hm.funnel?.replied_plus || 0 },
    { label: "Converted", count: hm.funnel?.converted || 0 },
  ] : [];
  const hmMix = hm ? [
    { label: "Recruiter-carried", count: hm.market_mix?.recruiter_carried || 0 },
    { label: "Direct employer", count: hm.market_mix?.direct || 0 },
    { label: "Leadership gap", count: hm.market_mix?.leadership_gaps || 0 },
  ] : [];

  return (
    <section className="stats-view">
      <div className="section-head">
        <h2><TrendingUp size={18} /> Ongoing Stats</h2>
        <div className="stats-period">
          {busy ? <Loader2 className="spin" size={16} /> : null}
          <button className={period === 7 ? "" : "secondary"} onClick={() => onPeriodChange(7)}>Weekly</button>
          <button className={period === 30 ? "" : "secondary"} onClick={() => onPeriodChange(30)}>Monthly</button>
        </div>
      </div>

      {!stats ? <p className="empty-inline">{busy ? "Crunching the numbers..." : "No stats loaded yet."}</p> : (
        <>
          <div className="metric-grid stats-metrics">
            <article className="metric"><span>Jobs scraped</span><strong>{current.scraped || 0}</strong><StatDelta current={current.scraped} previous={previous.scraped} /></article>
            <article className="metric"><span>Analyzed</span><strong>{current.analyzed || 0}</strong><StatDelta current={current.analyzed} previous={previous.analyzed} /></article>
            <article className="metric"><span>Strong fits (70+)</span><strong>{strongFits(current.bands)}</strong><StatDelta current={strongFits(current.bands)} previous={strongFits(previous.bands)} /></article>
            <article className="metric"><span>Applied</span><strong>{current.applied || 0}</strong><StatDelta current={current.applied} previous={previous.applied} /></article>
            <article className="metric"><span>Interviews</span><strong>{current.interviews || 0}</strong><StatDelta current={current.interviews} previous={previous.interviews} /></article>
            <article className="metric"><span>Offers</span><strong>{current.offers || 0}</strong><StatDelta current={current.offers} previous={previous.offers} /></article>
            {hm ? <article className="metric"><span>Outreach touches</span><strong>{hmCurrent.touchpoints || 0}</strong><StatDelta current={hmCurrent.touchpoints} previous={hmPrevious.touchpoints} /></article> : null}
            {hm ? <article className="metric"><span>Leads converted</span><strong>{hmCurrent.conversions || 0}</strong><StatDelta current={hmCurrent.conversions} previous={hmPrevious.conversions} /></article> : null}
          </div>

          <div className="stats-grid">
            <section className="dash-section">
              <h2><BarChart3 size={18} /> The Market</h2>
              <h3>Fit distribution of newly scraped roles</h3>
              <StatBars items={current.bands} labelKey="band" countKey="count" />
              <h3>Where roles came from</h3>
              <StatBars items={current.top_sources} labelKey="source" countKey="count" />
              {current.top_employers?.length ? (
                <>
                  <h3>Direct employers hiring your role family</h3>
                  <StatBars items={current.top_employers} labelKey="employer" countKey="count" />
                </>
              ) : null}
            </section>

            <section className="dash-section">
              <h2><Send size={18} /> Your Applications</h2>
              <div className="stats-kv">
                <div><span>Applications submitted</span><strong>{current.applied || 0}</strong></div>
                <div><span>Interview conversion</span><strong>{current.applied ? `${conversion}%` : "—"}</strong></div>
                <div><span>Documents generated</span><strong>{current.docs_generated || 0}</strong></div>
                <div><span>Prompts exported</span><strong>{current.prompts_generated || 0}</strong></div>
                <div><span>Offers</span><strong>{current.offers || 0}</strong></div>
              </div>
              {current.applied === 0 && (current.docs_generated || 0) > 0 ? (
                <p className="settings-hint">Documents were generated but nothing was submitted this period — finish the loop on the strongest ones.</p>
              ) : null}
              {(stats.band_funnel || []).some((band) => band.applied || band.interviews) ? (
                <>
                  <h3>Conversion by score band</h3>
                  <div className="stat-bars">
                    {(stats.band_funnel || []).filter((band) => band.applied || band.interviews).map((band) => (
                      <div key={band.band} className="stat-bar-row">
                        <span className="stat-bar-label">{band.band}</span>
                        <span className="stat-bar-track">
                          <span className="stat-bar-fill" style={{ width: `${band.applied ? Math.min(100, (band.interviews / band.applied) * 100) : 0}%` }} />
                        </span>
                        <strong>{band.applied ? `${band.interviews}/${band.applied}` : `${band.interviews}`}</strong>
                      </div>
                    ))}
                  </div>
                  <p className="settings-hint">Interviews per application by match band — if a lower band converts like 78+, the gatekeeper is over-strict; if a band never converts, tighten it.</p>
                </>
              ) : null}
              {(stats.recommendations || []).length ? (
                <>
                  <h3>Read on the week</h3>
                  {(stats.recommendations || []).map((item) => <p key={item} className="settings-hint">{item}</p>)}
                </>
              ) : null}
            </section>

            <section className="dash-section">
              <h2><NotebookTabs size={18} /> What's Happening</h2>
              {current.stage_moves?.length ? (
                <>
                  <h3>Pipeline movement</h3>
                  <StatBars items={current.stage_moves} labelKey="title" countKey="count" />
                </>
              ) : <p className="empty-inline">No pipeline movement recorded this period.</p>}
              <div className="stats-kv">
                <div><span>Auto-rejected by scoring</span><strong>{current.auto_rejected || 0}</strong></div>
                <div><span>Archived / retired</span><strong>{current.archived || 0}</strong></div>
              </div>
              {stats.last_scrape ? (
                <p className="settings-hint">
                  Last scrape: {stats.last_scrape.status} · {formatDate(stats.last_scrape.finished_at || stats.last_scrape.started_at)}
                </p>
              ) : null}
            </section>

            {hm ? (
              <section className="dash-section">
                <h2><Radar size={18} /> Hidden Market</h2>
                <h3>Outreach funnel</h3>
                <StatBars items={hmFunnel} labelKey="label" countKey="count" />
                <div className="stats-kv">
                  <div><span>Targets tracked</span><strong>{hm.coverage?.tracked || 0} / {hm.coverage?.surfaced || 0}</strong></div>
                  <div><span>Response rate</span><strong>{hm.funnel?.contacted_plus ? `${hm.response_rate}%` : "—"}</strong></div>
                  <div><span>Conversion rate</span><strong>{hm.funnel?.tracked ? `${hm.conversion_rate}%` : "—"}</strong></div>
                  <div><span>Follow-ups due</span><strong>{hm.coverage?.due_followups || 0}</strong></div>
                </div>
                {hm.market_mix?.targets ? (
                  <>
                    <h3>Market mix (last 60 days)</h3>
                    <StatBars items={hmMix} labelKey="label" countKey="count" />
                  </>
                ) : null}
                {(hm.reads || []).length ? (
                  <>
                    <h3>Read on outreach</h3>
                    {(hm.reads || []).map((item) => <p key={item} className="settings-hint">{item}</p>)}
                  </>
                ) : null}
              </section>
            ) : null}
          </div>
        </>
      )}
    </section>
  );
}

function countBy(items, key, fallback = "unknown") {
  return (items || []).reduce((counts, item) => {
    const value = String(item?.[key] || fallback).toLowerCase();
    counts[value] = (counts[value] || 0) + 1;
    return counts;
  }, {});
}

function MemoryPanel({ memoryStatus, memoryFragments, memoryBusy, onScanMemory }) {
  const fragmentCount = Number(memoryStatus?.fragment_count || memoryFragments?.length || 0);
  const unscanned = Number(memoryStatus?.recent_unscanned_count || 0);
  const threshold = Number(memoryStatus?.reminder_threshold || 6);
  const lastScan = memoryStatus?.last_scan;
  const latestScanSummary = lastScan?.summary || "";
  const byConfidence = countBy(memoryFragments, "confidence");
  const byStatus = countBy(memoryFragments, "status", "established");
  const preview = (memoryFragments || []).slice(0, 5);
  const needsScan = unscanned > 0;
  const urgency = unscanned >= threshold ? "due" : needsScan ? "pending" : "current";
  const scanCopy = memoryBusy
    ? "Mining saved applications..."
    : needsScan
      ? `Generate ${unscanned} waiting`
      : "Refresh fragments";

  return (
    <section className={`settings-section full-settings memory-panel ${memoryBusy ? "busy" : ""}`}>
      <div className="memory-head">
        <div>
          <h3>Lane Application Memory</h3>
          <p className="settings-hint">Fragments turn submitted applications into reusable evidence, search terms, and composite fit scores.</p>
        </div>
        <button className="secondary" disabled={memoryBusy} onClick={onScanMemory}>
          {memoryBusy ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
          {scanCopy}
        </button>
      </div>

      <div className="memory-metrics">
        <article>
          <span>Fragments</span>
          <strong>{fragmentCount}</strong>
          <small>{byStatus.established || 0} established · {byStatus.emerging || 0} emerging</small>
        </article>
        <article className={`memory-urgency ${urgency}`}>
          <span>Waiting docs</span>
          <strong>{unscanned}</strong>
          <small>{needsScan ? "Applied kits ready to mine" : "No saved applied kits waiting"}</small>
        </article>
        <article>
          <span>Confidence</span>
          <strong>{byConfidence.high || 0}/{byConfidence.medium || 0}/{byConfidence.low || 0}</strong>
          <small>high / medium / low</small>
        </article>
        <article>
          <span>Last scan</span>
          <strong>{lastScan?.scanned_at ? formatDate(lastScan.scanned_at) : "Never"}</strong>
          <small>{lastScan?.applications_scanned_count || 0} applications scanned</small>
        </article>
      </div>

      {memoryBusy ? (
        <div className="memory-progress">
          <Loader2 className="spin" size={18} />
          <span>Extracting fragments, consolidating repeated themes, and updating search terms.</span>
        </div>
      ) : null}

      {latestScanSummary ? <p className="memory-summary">{latestScanSummary}</p> : null}

      <div className="memory-preview">
        <div className="memory-preview-head">
          <strong>Strongest fragments</strong>
          <span>{preview.length ? "Used for prompt alignment and composite scoring" : "Run generation after saving application docs"}</span>
        </div>
        {preview.length ? preview.map((fragment) => (
          <article key={fragment.id || `${fragment.theme}-${fragment.claim}`} className="fragment-preview">
            <div>
              <strong>{fragment.theme || "Untitled fragment"}</strong>
              <span>{fragment.fragment_type || "evidence"} · {fragment.confidence || "medium"} · {fragment.status || "established"}</span>
            </div>
            <p>{fragment.claim || fragment.supporting_detail || "No claim captured."}</p>
            {(fragment.keywords || []).length ? <small>Activates on: {fragment.keywords.slice(0, 5).join(", ")}</small> : null}
          </article>
        )) : (
          <p className="empty-inline">No fragments loaded for this lane yet.</p>
        )}
      </div>
    </section>
  );
}

function EvidenceLibraryPanel({ profileId }) {
  const [stats, setStats] = useState(null);
  const [docs, setDocs] = useState([]);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const streamingTaskRef = useRef(null);

  const inv = useCallback((cmd, payload = {}) => window.jobAssistant.invoke(cmd, { profile_id: profileId, ...payload }), [profileId]);
  const loadStats = useCallback(() => inv("corpus:stats").then(setStats).catch(() => {}), [inv]);
  const loadDocs = useCallback((q = "") => inv("corpus:list", { query: q, limit: 400 }).then((d) => setDocs(d.documents || [])).catch(() => {}), [inv]);

  useEffect(() => { loadStats(); loadDocs(); }, [loadStats, loadDocs]);
  // Unsubscribe any in-flight streaming listener on unmount; otherwise the IPC
  // listener (and this component's state setters it closes over) leak every
  // time the user navigates away mid-index/mine. The task itself keeps running.
  useEffect(() => () => {
    streamingTaskRef.current?.unsubscribe();
    streamingTaskRef.current = null;
  }, []);

  const run = async (label, fn) => {
    setBusy(label); setNote("");
    try {
      const message = await fn();
      if (message) setNote(message);
      await loadStats(); await loadDocs(query);
    } catch (error) {
      setNote(`Error: ${error?.message || error}`);
    } finally { setBusy(""); }
  };

  // Long operations stream progress (they can run minutes) instead of blocking.
  const runStreaming = (label, command, summarize) => {
    if (busy) return;
    setBusy(label); setNote(`${label}…`);
    const task = window.jobAssistant.startTask(command, { profile_id: profileId }, (event) => {
      if ((event.type === "log" || event.type === "status") && event.message) setNote(event.message);
      else if (event.type === "result") { setNote(summarize ? summarize(event.data || {}) : "Done."); setBusy(""); task.unsubscribe(); streamingTaskRef.current = null; loadStats(); loadDocs(query); }
      else if (event.type === "error") { setNote(`Error: ${event.message || "failed"}`); setBusy(""); task.unsubscribe(); streamingTaskRef.current = null; }
    });
    streamingTaskRef.current = task;
  };
  const reindex = () => runStreaming("Re-indexing", "corpus:reindex", (d) => `Indexed ${d.total} documents from your corpus folder.`);
  const remine = () => runStreaming("Mining fragments", "corpus:mine", (d) => `Mined ${d.mined} fragments (${d.candidate_upserted} stored) via ${d.provider}.`);
  const reclassify = () => run("Reclassifying", async () => { const r = await inv("corpus:reclassify"); return `Reclassified ${r.reclassified} documents, removed ${r.removed_temp} temp files.`; });
  const clearDocs = async () => {
    const confirmed = await appConfirm({
      title: "Clear indexed documents",
      message: "Clear ALL indexed documents? You can re-index from your corpus folder afterwards.",
      confirmLabel: "Clear documents",
      danger: true
    });
    if (confirmed) run("Clearing documents", async () => { const r = await inv("corpus:clearDocs"); return `Cleared ${r.cleared_documents} documents.`; });
  };
  const clearFrags = async () => {
    const confirmed = await appConfirm({
      title: "Clear mined fragments",
      message: "Clear ALL mined fragments? Re-mine to rebuild them from your documents.",
      confirmLabel: "Clear fragments",
      danger: true
    });
    if (confirmed) run("Clearing fragments", async () => { const r = await inv("corpus:clearFragments"); return `Cleared ${r.cleared_candidate_fragments} fragments.`; });
  };
  const removeDoc = (id) => run("Removing", async () => { await inv("corpus:removeDoc", { id }); return ""; });
  const setType = (id, doc_type) => run("Updating", async () => { await inv("corpus:setType", { id, doc_type }); return ""; });

  return (
    <section className={`settings-section full-settings memory-panel ${busy ? "busy" : ""}`}>
      <div className="memory-head">
        <div>
          <h3>Evidence Library</h3>
          <p className="settings-hint">Your prior resumes, cover letters and KSC responses ground every generated application. Source: {stats?.source || "—"}</p>
        </div>
        <button className="secondary" disabled={!!busy} onClick={reindex}>{busy === "Re-indexing" ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />} Re-index</button>
      </div>

      <div className="memory-metrics">
        <article><span>Documents</span><strong>{stats?.total ?? "—"}</strong><small>indexed</small></article>
        <article><span>Fragments</span><strong>{stats?.fragments ?? "—"}</strong><small>mined evidence</small></article>
        {(stats?.by_type || []).slice(0, 2).map((t) => (
          <article key={t.doc_type}><span>{t.doc_type.replace(/_/g, " ")}</span><strong>{t.count}</strong><small>documents</small></article>
        ))}
      </div>

      <div className="section-actions">
        <button className="secondary" disabled={!!busy} onClick={remine}>{busy === "Mining fragments" ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />} Re-mine fragments</button>
        <button className="secondary" disabled={!!busy} onClick={reclassify}><Wrench size={16} /> Reclassify</button>
        <button className="secondary" disabled={!!busy} onClick={clearDocs}><Trash2 size={16} /> Clear documents</button>
        <button className="secondary" disabled={!!busy} onClick={clearFrags}><Trash2 size={16} /> Clear fragments</button>
      </div>

      {busy ? <div className="memory-progress"><Loader2 className="spin" size={18} /><span>{busy}… this can take up to a minute.</span></div> : null}
      {note ? <p className="memory-summary">{note}</p> : null}

      <div className="memory-preview">
        <div className="memory-preview-head">
          <strong>Documents ({docs.length})</strong>
          <input value={query} placeholder="Filter by file name" onChange={(event) => { setQuery(event.target.value); loadDocs(event.target.value); }} />
        </div>
        <div className="corpus-doc-list">
          {docs.length ? docs.map((doc) => (
            <article key={doc.id} className="fragment-preview corpus-doc-row">
              <span className="corpus-doc-name" title={doc.filename}><FileText size={14} /> {doc.filename}</span>
              <select value={doc.doc_type || "other"} disabled={!!busy} onChange={(event) => setType(doc.id, event.target.value)}>
                {CORPUS_DOC_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
              </select>
              <button className="secondary icon-only" disabled={!!busy} title="Remove from library" onClick={() => removeDoc(doc.id)}><Trash2 size={14} /></button>
            </article>
          )) : <p className="empty-inline">No documents indexed. Click Re-index to load your corpus.</p>}
        </div>
      </div>
    </section>
  );
}

function ScraperPluginBuilder({ profileId, busy, onBuild, onTest }) {
  const [form, setForm] = useState({
    source_name: "",
    company_name: "",
    careers_url: "",
    mode: "keyword",
    platform_hint: "",
    location: "",
    test_keyword: "business analyst",
    max_pages: 2,
    notes: ""
  });
  const [result, setResult] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const disabled = busy || working;
  const canBuild = form.source_name.trim() && form.careers_url.trim() && !disabled;

  const build = async () => {
    setError("");
    setTestResult(null);
    setWorking(true);
    try {
      const data = await onBuild({ ...form, profile_id: profileId });
      setResult(data);
    } catch (buildError) {
      setError(toErrorMessage(buildError));
    } finally {
      setWorking(false);
    }
  };

  const test = async () => {
    if (!result?.plugin?.id) return;
    setError("");
    setWorking(true);
    try {
      const data = await onTest(result.plugin.id, form.test_keyword, form.max_pages);
      setTestResult(data);
    } catch (testError) {
      setError(toErrorMessage(testError));
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="scraper-builder">
      <div className="settings-section-head">
        <h4>Build A Scraper Plugin</h4>
        <button className="secondary" disabled={!canBuild} onClick={build}><Sparkles size={16} /> Generate</button>
      </div>
      <div className="form-grid compact">
        <label><span>Source name</span><input value={form.source_name} placeholder="Example Careers" onChange={(event) => update("source_name", event.target.value)} /></label>
        <label><span>Company</span><input value={form.company_name} placeholder="Example Pty Ltd" onChange={(event) => update("company_name", event.target.value)} /></label>
        <label className="full"><span>Careers or search URL</span><input value={form.careers_url} placeholder="https://example.com/jobs" onChange={(event) => update("careers_url", event.target.value)} /></label>
        <label><span>Mode</span><select value={form.mode} onChange={(event) => update("mode", event.target.value)}><option value="keyword">Keyword search</option><option value="sweep">Sweep all listings</option></select></label>
        <label><span>Platform hint</span><input value={form.platform_hint} placeholder="PageUp, Workday, SmartRecruiters, custom" onChange={(event) => update("platform_hint", event.target.value)} /></label>
        <label><span>Location default</span><input value={form.location} placeholder="Melbourne VIC" onChange={(event) => update("location", event.target.value)} /></label>
        <label><span>Test keyword</span><input value={form.test_keyword} onChange={(event) => update("test_keyword", event.target.value)} /></label>
        <label><span>Page limit</span><input type="number" min="1" max="5" value={form.max_pages} onChange={(event) => update("max_pages", event.target.value)} /></label>
        <label className="full"><span>Notes for the local LLM</span><textarea rows={3} value={form.notes} placeholder="Known listing card CSS, pagination notes, details page patterns, fields to capture..." onChange={(event) => update("notes", event.target.value)} /></label>
      </div>
      {error ? <p className="settings-alert">{error}</p> : null}
      {result ? (
        <div className="builder-result">
          <div>
            <strong>{result.plugin?.name || result.manifest?.name}</strong>
            <small>{result.plugin_dir}</small>
          </div>
          {result.reconnaissance ? (
            <small className="builder-recon">
              {result.reconnaissance.fetched
                ? `Recon: ${result.reconnaissance.jsonld_jobposting ? "JSON-LD JobPosting found · " : ""}${result.reconnaissance.candidate_links} job link(s)${(result.reconnaissance.embedded_state || []).length ? ` · ${result.reconnaissance.embedded_state.join(", ")}` : ""} · ${result.reconnaissance.render_hint || ""}`
                : `Recon unavailable — generated without live page evidence${result.reconnaissance.error ? ` (${result.reconnaissance.error})` : ""}`}
            </small>
          ) : null}
          {typeof result.verified === "boolean" ? (
            <small className={result.verified ? "builder-verified ok" : "builder-verified warn"}>
              {result.verified
                ? `Verified by dry run after ${result.attempts} attempt(s)`
                : `Not verified after ${result.attempts} attempt(s) — review and edit before relying on it`}
            </small>
          ) : null}
          <button className="secondary" disabled={disabled} onClick={test}><Play size={15} /> Dry run</button>
          {(result.notes || []).length ? <ul>{result.notes.slice(0, 4).map((note, index) => <li key={`${note}-${index}`}>{note}</li>)}</ul> : null}
        </div>
      ) : null}
      {testResult ? (
        <div className={`builder-test ${testResult.ok ? "ok" : "bad"}`}>
          <strong>{testResult.ok ? "Dry run passed" : "Dry run needs review"}</strong>
          <span>{JSON.stringify(testResult.result || {}).slice(0, 500)}</span>
          {(testResult.logs || []).length ? <small>{testResult.logs.slice(-4).join(" | ")}</small> : null}
        </div>
      ) : null}
    </div>
  );
}

function SettingsPanel({ profile, settings, globalSettings, scrapers, scraperError, memoryStatus, memoryFragments, memoryBusy, onSave, onSaveGlobal, onSaveProfile, onApplyFilters, onCompactDatabase, onImportResume, onSearchResumes, onScanMemory, onImportScraper, onBuildScraper, onTestScraper, onUpdateScraper, onUpdateLaneScraper, onRemoveScraper }) {
  const [form, setForm] = useState(settings || {});
  const [globalForm, setGlobalForm] = useState(globalSettings || {});
  const [profileForm, setProfileForm] = useState({ name: "", resume_path: "" });
  const [resumeQuery, setResumeQuery] = useState("");
  const [resumeOptions, setResumeOptions] = useState([]);
  const [resumeSearchBusy, setResumeSearchBusy] = useState(false);
  const [settingsScope, setSettingsScope] = useState("lane");
  const [section, setSection] = useState("profile");
  const [compacting, setCompacting] = useState(false);
  const [compactResult, setCompactResult] = useState(null);
  const [providerTests, setProviderTests] = useState({});

  useEffect(() => setForm(settings || {}), [settings]);
  useEffect(() => setGlobalForm(globalSettings || {}), [globalSettings]);
  useEffect(() => {
    setProfileForm({
      name: profile?.name || "",
      resume_path: profile?.resume_path || ""
    });
  }, [profile]);
  useEffect(() => {
    const firstSection = SETTINGS_SECTIONS.find((item) => item.scope === settingsScope)?.id || "profile";
    if (!SETTINGS_SECTIONS.some((item) => item.id === section && item.scope === settingsScope)) {
      setSection(firstSection);
    }
  }, [settingsScope, section]);
  useEffect(() => {
    if (section !== "profile" || !onSearchResumes) return;
    let active = true;
    setResumeSearchBusy(true);
    onSearchResumes(resumeQuery, profileForm.resume_path)
      .then((items) => {
        if (active) setResumeOptions(items);
      })
      .catch(() => {
        if (active) setResumeOptions([]);
      })
      .finally(() => {
        if (active) setResumeSearchBusy(false);
      });
    return () => {
      active = false;
    };
  }, [section, resumeQuery, profileForm.resume_path, onSearchResumes]);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const updateGlobal = (key, value) => setGlobalForm((current) => ({ ...current, [key]: value }));
  const updateProfile = (key, value) => setProfileForm((current) => ({ ...current, [key]: value }));
  const toggleMode = (mode, checked) => {
    update("work_modes", checked
      ? [...new Set([...(form.work_modes || []), mode])]
      : (form.work_modes || []).filter((item) => item !== mode));
  };
  const chooseResume = async () => {
    const resumePath = await window.jobAssistant.chooseResume();
    if (resumePath) {
      const importedPath = await onImportResume(resumePath);
      updateProfile("resume_path", importedPath);
      setResumeQuery("");
    }
  };
  const selectSavedResume = (resume) => {
    updateProfile("resume_path", resume.path);
    setResumeQuery(resume.name.replace(/\.docx$/i, ""));
  };
  const formatResumeModified = (resume) => {
    const value = Number(resume.modified_at || 0);
    if (!value) return "Unknown date";
    return new Date(value * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  };
  const chooseTemplate = async (key) => {
    const templatePath = await window.jobAssistant.chooseTemplate();
    if (templatePath) update(key, templatePath);
  };
  const chooseFolder = async (key, title) => {
    const folderPath = await window.jobAssistant.chooseFolder(title);
    if (folderPath) updateGlobal(key, folderPath);
  };
  const compactDatabase = async () => {
    setCompacting(true);
    try {
      const result = await onCompactDatabase();
      setCompactResult(result);
    } finally {
      setCompacting(false);
    }
  };
  const pluginConfigValue = (plugin, key) => {
    const config = plugin.config || {};
    const laneConfig = plugin.lane_config || {};
    const schemaItem = (plugin.config_schema || []).find((item) => item.key === key) || {};
    return laneConfig[key] ?? config[key] ?? schemaItem.default ?? "";
  };
  const updatePluginConfig = (plugin, key, value, laneOnly = false) => {
    const next = { ...(laneOnly ? plugin.lane_config : plugin.config), [key]: value };
    if (laneOnly) {
      onUpdateLaneScraper(plugin.id, { config: next });
    } else {
      onUpdateScraper(plugin.id, { config: next });
    }
  };

  const workflowProvider = (key) => globalForm[key] || globalForm.doc_ai_provider || "local";
  const providerIsConfigured = (provider) => {
    if (provider === "local") return Boolean((globalForm.local_base_url || "").trim());
    if (provider === "chatgpt") return Boolean((globalForm.openai_api_key || "").trim());
    if (provider === "claude") return Boolean((globalForm.claude_api_key || "").trim());
    if (provider === "gemini") return Boolean((globalForm.gemini_api_key || "").trim());
    return false;
  };
  const providerIsUsed = (provider) => [
    workflowProvider("document_ai_provider"),
    workflowProvider("research_ai_provider"),
    workflowProvider("memory_ai_provider"),
    "local"
  ].includes(provider);
  const providerStatus = (provider) => {
    const test = providerTests[provider];
    if (test?.ok) return "Verified";
    if (test?.warning) return "Model not loaded";
    if (test && !test.testing && !test.ok) return "Test failed";
    if (providerIsConfigured(provider)) return "Configured";
    return providerIsUsed(provider) ? "Needs setup" : "Not configured";
  };
  const providerStatusClass = (provider) => {
    const test = providerTests[provider];
    if (test?.ok) return "ready";
    if (test?.warning) return "warning";
    if (test && !test.testing && !test.ok) return "failed";
    return providerIsConfigured(provider) ? "ready" : "missing";
  };
  const testProvider = async (provider) => {
    setProviderTests((current) => ({ ...current, [provider]: { testing: true } }));
    try {
      const result = await new Promise((resolve, reject) => {
        let task;
        task = window.jobAssistant.startTask(
          "ai:testProvider",
          { provider, settings: globalForm },
          (event) => {
            if (event.type === "result") {
              task?.unsubscribe();
              resolve(event.data);
            } else if (event.type === "error") {
              task?.unsubscribe();
              reject(new Error(event.message || "Provider test failed."));
            }
          }
        );
      });
      if (provider === "local" && result.model) {
        updateGlobal("local_model", result.model);
      }
      setProviderTests((current) => ({
        ...current,
        [provider]: result.ok
          ? { ok: true, message: `${result.label} responded in ${(result.elapsed_ms / 1000).toFixed(1)}s` }
          : { ok: false, warning: Boolean(result.reachable), message: result.message || "Provider test failed." }
      }));
    } catch (error) {
      setProviderTests((current) => ({
        ...current,
        [provider]: { ok: false, message: toErrorMessage(error) }
      }));
    }
  };

  return (
    <section className="settings-view">
      <div className="section-head">
        <h2><Settings size={18} /> Settings</h2>
        <span>{profile?.name || "Lane"}</span>
      </div>
      <div className="settings-scope" role="tablist" aria-label="Settings type">
        <button className={settingsScope === "general" ? "active" : ""} onClick={() => setSettingsScope("general")}>General</button>
        <button className={settingsScope === "lane" ? "active" : ""} onClick={() => setSettingsScope("lane")}>Lane</button>
      </div>
      <nav className="settings-tabs" aria-label="Settings sections">
        {SETTINGS_SECTIONS.filter((item) => item.scope === settingsScope).map((item) => (
          <button key={item.id} className={section === item.id ? "active" : ""} onClick={() => setSection(item.id)}>{item.label}</button>
        ))}
      </nav>
      <div className="settings-grid">
        {section === "searchers" ? (
        <section className="settings-section full-settings">
          <div className="settings-section-head">
            <h3>Searchers</h3>
            <button className="secondary" onClick={onImportScraper}><FolderOpen size={16} /> Import plugin</button>
          </div>
          {scraperError ? <p className="settings-alert">{scraperError}</p> : null}
          <ScraperPluginBuilder
            profileId={profile?.id || 1}
            onBuild={onBuildScraper}
            onTest={onTestScraper}
          />
          <div className="scraper-list">
            {(scrapers || []).map((plugin) => (
              <article key={plugin.id} className="scraper-item">
                <div>
                  <strong>{plugin.name}</strong>
                  <small>{plugin.install_type || "plugin"} · {plugin.mode || "keyword"} · {plugin.source_name}</small>
                  {plugin.install_path ? <small title={plugin.install_path}>{plugin.install_path}</small> : null}
                </div>
                <div className="scraper-controls">
                  <label className="check-row"><input type="checkbox" checked={Boolean(plugin.enabled)} onChange={(event) => onUpdateScraper(plugin.id, { enabled: event.target.checked })} /> Available</label>
                  <label className="check-row"><input type="checkbox" checked={plugin.lane_enabled !== false} onChange={(event) => onUpdateLaneScraper(plugin.id, { enabled: event.target.checked })} /> This lane</label>
                  <button className="ghost danger" onClick={() => onRemoveScraper(plugin.id)}><Trash2 size={15} /> {plugin.install_type === "bundled" ? "Disable" : "Remove"}</button>
                </div>
                {(plugin.config_schema || []).length ? (
                  <div className="form-grid compact scraper-config">
                    {(plugin.config_schema || []).map((item) => (
                      <label key={`${plugin.id}-${item.key}`}>
                        <span>{item.label || item.key}</span>
                        <input
                          type={item.type === "number" ? "number" : "text"}
                          value={pluginConfigValue(plugin, item.key)}
                          onChange={(event) => updatePluginConfig(plugin, item.key, event.target.value, true)}
                        />
                      </label>
                    ))}
                  </div>
                ) : null}
              </article>
            ))}
            {!(scrapers || []).length ? <p className="empty-inline">No scraper plugins registered yet. Import one or use the builder above to create a custom scraper plugin.</p> : null}
          </div>
        </section>
        ) : null}
        {section === "ai" ? (
        <section className="settings-section full-settings ai-settings">
          <header className="ai-settings-intro">
            <div className="ai-settings-icon"><Sparkles size={19} /></div>
            <div>
              <h3>AI routing</h3>
              <p>Choose the engine for each kind of work, then configure only the providers you use.</p>
            </div>
          </header>

          <div className="ai-block-heading">
            <div><strong>Workflow assignments</strong><span>Each workflow can use a different provider.</span></div>
          </div>
          <div className="ai-route-grid">
            <article className="ai-route-card">
              <div className="ai-route-copy"><FileText size={17} /><div><strong>Application documents</strong><span>Resumes, cover letters and fact-checking</span></div></div>
              <select aria-label="Application document provider" value={workflowProvider("document_ai_provider")} onChange={(event) => updateGlobal("document_ai_provider", event.target.value)}>{DOCUMENT_AI_PROVIDERS.map((provider) => <option key={provider.id} value={provider.id}>{provider.label}</option>)}</select>
            </article>
            <article className="ai-route-card">
              <div className="ai-route-copy"><BriefcaseBusiness size={17} /><div><strong>Employer research</strong><span>Company context and application angles</span></div></div>
              <select aria-label="Employer research provider" value={workflowProvider("research_ai_provider")} onChange={(event) => updateGlobal("research_ai_provider", event.target.value)}>{DOCUMENT_AI_PROVIDERS.map((provider) => <option key={provider.id} value={provider.id}>{provider.label}</option>)}</select>
            </article>
            <article className="ai-route-card">
              <div className="ai-route-copy"><NotebookTabs size={17} /><div><strong>Evidence & memory</strong><span>Corpus mining and reusable career evidence</span></div></div>
              <select aria-label="Evidence and memory provider" value={workflowProvider("memory_ai_provider")} onChange={(event) => updateGlobal("memory_ai_provider", event.target.value)}>{DOCUMENT_AI_PROVIDERS.map((provider) => <option key={provider.id} value={provider.id}>{provider.label}</option>)}</select>
            </article>
            <article className="ai-route-card fixed">
              <div className="ai-route-copy"><Radar size={17} /><div><strong>Job matching</strong><span>High-volume triage and fit analysis</span></div></div>
              <div className="ai-fixed-provider"><span className="status-dot configured" />Local endpoint <small>Fixed</small></div>
            </article>
          </div>

          <div className="ai-block-heading providers-heading">
            <div><strong>Provider connections</strong><span>Credentials stay on this device.</span></div>
          </div>
          <div className="ai-provider-grid">
            <article className={`ai-provider-card ${providerIsUsed("local") ? "in-use" : ""}`}>
              <header><div><span className="provider-mark local">L</span><div><strong>Local endpoint</strong><small>Private, on-device inference</small></div></div><div className="provider-card-actions"><span className={`provider-status ${providerStatusClass("local")}`}><i />{providerStatus("local")}</span><button type="button" className="secondary ai-test-button" disabled={providerTests.local?.testing} onClick={() => testProvider("local")}>{providerTests.local?.testing ? <Loader2 className="spin" size={12} /> : <Play size={12} />}Test</button></div></header>
              <div className="ai-provider-fields">
                <label className="full"><span>Base URL</span><input value={globalForm.local_base_url || ""} placeholder="http://localhost:1234/v1" onChange={(event) => updateGlobal("local_base_url", event.target.value)} /></label>
                <label><span>Model</span><input value={globalForm.local_model || ""} placeholder="Loaded model name" onChange={(event) => updateGlobal("local_model", event.target.value)} /></label>
                <label><span>API key</span><input type="password" value={globalForm.local_api_key || ""} placeholder="Optional" onChange={(event) => updateGlobal("local_api_key", event.target.value)} /></label>
              </div>
              <div className="local-ai-quickstart">
                <span>Choose one local model server:</span>
                {Object.entries(LOCAL_AI_RUNTIMES).map(([id, runtime]) => (
                  <div key={id}>
                    <button type="button" className="ghost" onClick={() => {
                      updateGlobal("local_base_url", runtime.baseUrl);
                      if (runtime.model) updateGlobal("local_model", runtime.model);
                    }}>Use {runtime.label} preset</button>
                    <button type="button" className="ghost" onClick={() => window.jobAssistant.openExternal(runtime.downloadUrl)}><ExternalLink size={13} /> Install</button>
                  </div>
                ))}
              </div>
              {providerTests.local && !providerTests.local.testing ? <div className={`ai-test-result ${providerTests.local.ok ? "ok" : providerTests.local.warning ? "warning" : "bad"}`}>{providerTests.local.message}</div> : null}
            </article>
            <article className={`ai-provider-card ${providerIsUsed("gemini") ? "in-use" : ""}`}>
              <header><div><span className="provider-mark gemini">G</span><div><strong>Gemini</strong><small>Google AI models</small></div></div><div className="provider-card-actions"><span className={`provider-status ${providerStatusClass("gemini")}`}><i />{providerStatus("gemini")}</span><button type="button" className="secondary ai-test-button" disabled={providerTests.gemini?.testing} onClick={() => testProvider("gemini")}>{providerTests.gemini?.testing ? <Loader2 className="spin" size={12} /> : <Play size={12} />}Test</button></div></header>
              <div className="ai-provider-fields">
                <label><span>API key</span><input type="password" value={globalForm.gemini_api_key || ""} placeholder="Required" onChange={(event) => updateGlobal("gemini_api_key", event.target.value)} /></label>
                <label><span>Model</span><input value={globalForm.gemini_model || ""} placeholder="gemini-3.1-pro-preview" onChange={(event) => updateGlobal("gemini_model", event.target.value)} /></label>
              </div>
              {providerTests.gemini && !providerTests.gemini.testing ? <div className={`ai-test-result ${providerTests.gemini.ok ? "ok" : "bad"}`}>{providerTests.gemini.message}</div> : null}
            </article>
            <article className={`ai-provider-card ${providerIsUsed("chatgpt") ? "in-use" : ""}`}>
              <header><div><span className="provider-mark openai">O</span><div><strong>OpenAI</strong><small>ChatGPT and compatible APIs</small></div></div><div className="provider-card-actions"><span className={`provider-status ${providerStatusClass("chatgpt")}`}><i />{providerStatus("chatgpt")}</span><button type="button" className="secondary ai-test-button" disabled={providerTests.chatgpt?.testing} onClick={() => testProvider("chatgpt")}>{providerTests.chatgpt?.testing ? <Loader2 className="spin" size={12} /> : <Play size={12} />}Test</button></div></header>
              <div className="ai-provider-fields">
                <label><span>API key</span><input type="password" value={globalForm.openai_api_key || ""} placeholder="Required" onChange={(event) => updateGlobal("openai_api_key", event.target.value)} /></label>
                <label><span>Base URL</span><input value={globalForm.openai_base_url || ""} placeholder="https://api.openai.com/v1" onChange={(event) => updateGlobal("openai_base_url", event.target.value)} /></label>
              </div>
              {providerTests.chatgpt && !providerTests.chatgpt.testing ? <div className={`ai-test-result ${providerTests.chatgpt.ok ? "ok" : "bad"}`}>{providerTests.chatgpt.message}</div> : null}
            </article>
            <article className={`ai-provider-card ${providerIsUsed("claude") ? "in-use" : ""}`}>
              <header><div><span className="provider-mark claude">C</span><div><strong>Claude</strong><small>Anthropic models</small></div></div><div className="provider-card-actions"><span className={`provider-status ${providerStatusClass("claude")}`}><i />{providerStatus("claude")}</span><button type="button" className="secondary ai-test-button" disabled={providerTests.claude?.testing} onClick={() => testProvider("claude")}>{providerTests.claude?.testing ? <Loader2 className="spin" size={12} /> : <Play size={12} />}Test</button></div></header>
              <div className="ai-provider-fields">
                <label><span>API key</span><input type="password" value={globalForm.claude_api_key || ""} placeholder="Required" onChange={(event) => updateGlobal("claude_api_key", event.target.value)} /></label>
                <label><span>Model</span><input value={globalForm.claude_model || ""} placeholder="claude-sonnet-4-6" onChange={(event) => updateGlobal("claude_model", event.target.value)} /></label>
              </div>
              {providerTests.claude && !providerTests.claude.testing ? <div className={`ai-test-result ${providerTests.claude.ok ? "ok" : "bad"}`}>{providerTests.claude.message}</div> : null}
            </article>
          </div>

          <div className="ai-advanced-row">
            <div><strong>Global model override</strong><span>Optional. Overrides the provider-specific model for every cloud workflow.</span></div>
            <input aria-label="Global model override" value={globalForm.doc_ai_model || ""} placeholder="Leave blank to use provider models" onChange={(event) => updateGlobal("doc_ai_model", event.target.value)} />
          </div>
        </section>
        ) : null}
        {section === "folders" ? (
        <section className="settings-section full-settings">
          <h3>Local Folders</h3>
          <div className="form-grid compact">
            <label><span>Current applications</span><input value={globalForm.applications_dir || ""} onChange={(event) => updateGlobal("applications_dir", event.target.value)} /></label>
            <label><span>Older applications corpus</span><input value={globalForm.older_applications_dir || ""} onChange={(event) => updateGlobal("older_applications_dir", event.target.value)} /></label>
            <label><span>Settings directory</span><input value={globalForm.settings_dir || ""} readOnly /></label>
          </div>
          <div className="section-actions">
            <button className="secondary" onClick={() => chooseFolder("applications_dir", "Select current applications folder")}><FolderOpen size={16} /> Choose applications</button>
            <button className="secondary" onClick={() => chooseFolder("older_applications_dir", "Select older applications folder")}><FolderOpen size={16} /> Choose older applications</button>
          </div>
          <p className="settings-hint">Generated and uploaded application documents go to the current applications folder. Evidence Library re-indexing mines the older applications corpus folder.</p>
        </section>
        ) : null}
        {section === "templates" ? (
        <section className="settings-section full-settings">
          <h3>Application Templates</h3>
          <div className="form-grid compact">
            <label><span>Resume template</span><input value={form.resume_template_path || ""} onChange={(event) => update("resume_template_path", event.target.value)} /></label>
            <label><span>Cover letter template</span><input value={form.cover_letter_template_path || ""} onChange={(event) => update("cover_letter_template_path", event.target.value)} /></label>
          </div>
          <div className="section-actions">
            <button className="secondary" onClick={() => chooseTemplate("resume_template_path")}><FolderOpen size={16} /> Choose resume template</button>
            <button className="secondary" onClick={() => chooseTemplate("cover_letter_template_path")}><FolderOpen size={16} /> Choose cover template</button>
          </div>
        </section>
        ) : null}
        {section === "profile" ? (
        <section className="settings-section">
          <h3>Lane</h3>
          <div className="form-grid compact">
            <label><span>Lane name</span><input value={profileForm.name} onChange={(event) => updateProfile("name", event.target.value)} /></label>
            <div className="resume-picker">
              <label><span>Resume path</span><input value={profileForm.resume_path} onChange={(event) => updateProfile("resume_path", event.target.value)} /></label>
              <label><span>Search saved resumes</span><input value={resumeQuery} placeholder="Filter by file name or folder" onChange={(event) => setResumeQuery(event.target.value)} /></label>
              <div className="resume-results">
                {resumeSearchBusy ? <span className="resume-loading"><Loader2 className="spin" size={14} /> Searching resumes...</span> : null}
                {!resumeSearchBusy && resumeOptions.length === 0 ? <span className="empty-inline">No saved resumes found.</span> : null}
                {!resumeSearchBusy && resumeOptions.slice(0, 8).map((resume) => (
                  <button
                    key={resume.path}
                    type="button"
                    className={resume.path === profileForm.resume_path ? "resume-option active" : "resume-option"}
                    onClick={() => selectSavedResume(resume)}
                    title={resume.path}
                  >
                    <FileText size={15} />
                    <span>
                      <strong>{resume.name}</strong>
                      <small>{resume.folder} · {formatResumeModified(resume)} · {formatBytes(resume.size)}</small>
                    </span>
                  </button>
                ))}
              </div>
            </div>
            <label><span>Lane intent</span><textarea value={form.lane_intent || ""} placeholder="Senior IT leadership, engineering systems, business partnering..." onChange={(event) => update("lane_intent", event.target.value)} /></label>
            <label><span>Target titles</span><textarea value={form.target_titles || ""} placeholder="IT Manager, Digital Systems Manager, Technology Business Partner" onChange={(event) => update("target_titles", event.target.value)} /></label>
            <label><span>Target domains</span><input value={form.target_domains || ""} placeholder="systems, platforms, operations, transformation" onChange={(event) => update("target_domains", event.target.value)} /></label>
            <label><span>Seniority</span><input value={form.seniority || ""} placeholder="manager, senior manager, lead" onChange={(event) => update("seniority", event.target.value)} /></label>
            <label><span>Must-have signals</span><textarea value={form.must_have_terms || ""} placeholder="stakeholder leadership, vendor governance, systems delivery" onChange={(event) => update("must_have_terms", event.target.value)} /></label>
            <label><span>Avoid signals</span><textarea value={form.avoid_terms || ""} placeholder="junior support, shift work, pure coding" onChange={(event) => update("avoid_terms", event.target.value)} /></label>
          </div>
          <div className="section-actions">
            <button className="secondary" onClick={chooseResume}><FolderOpen size={16} /> Choose resume</button>
            <button disabled={!profile || !profileForm.name.trim() || !profileForm.resume_path.trim()} onClick={() => onSaveProfile(profileForm)}><Check size={16} /> Save lane</button>
            <button onClick={() => onSave(form)}><Check size={16} /> Save lane strategy</button>
          </div>
        </section>
        ) : null}
        {section === "search" ? (
        <section className="settings-section full-settings">
          <h3>Locations</h3>
          <div className="form-grid compact">
            <label><span>Default job location</span><input value={form.preferred_location || ""} placeholder="Melbourne VIC" onChange={(event) => update("preferred_location", event.target.value)} /></label>
            <label><span>Scraper page limit</span><input type="number" min="1" max="100" value={form.max_pages || 30} onChange={(event) => update("max_pages", event.target.value)} /></label>
          </div>
          <div className="lane-source-list">
            {(scrapers || []).filter((plugin) => plugin.enabled).map((plugin) => (
              <label key={plugin.id} className="check-row">
                <input type="checkbox" checked={plugin.lane_enabled !== false} onChange={(event) => onUpdateLaneScraper(plugin.id, { enabled: event.target.checked })} />
                {plugin.name}
              </label>
            ))}
          </div>
          <div className="scraper-list lane-scraper-configs">
            {(scrapers || []).filter((plugin) => plugin.enabled && plugin.lane_enabled !== false && (plugin.config_schema || []).length).map((plugin) => (
              <article key={`${plugin.id}-lane-config`} className="scraper-item">
                <div>
                  <strong>{plugin.name}</strong>
                  <small>Lane search defaults</small>
                </div>
                <div className="form-grid compact scraper-config">
                  {(plugin.config_schema || []).map((item) => (
                    <label key={`${plugin.id}-lane-${item.key}`}>
                      <span>{item.label || item.key}</span>
                      <input
                        type={item.type === "number" ? "number" : "text"}
                        value={pluginConfigValue(plugin, item.key)}
                        onChange={(event) => updatePluginConfig(plugin, item.key, event.target.value, true)}
                      />
                    </label>
                  ))}
                </div>
              </article>
            ))}
          </div>
        </section>
        ) : null}
        {section === "matching" ? (
        <>
        <section className="settings-section full-settings">
          <h3>Working Mode</h3>
          <div className="mode-grid">
            {WORK_MODES.map((mode) => (
              <label key={mode.id} className="check-row">
                <input
                  type="checkbox"
                  checked={(form.work_modes || []).includes(mode.id)}
                  onChange={(event) => toggleMode(mode.id, event.target.checked)}
                />
                {mode.label}
              </label>
            ))}
          </div>
          <label className="field"><span>Default minimum score</span><input type="number" min="0" max="100" value={form.default_min_score ?? 60} onChange={(event) => update("default_min_score", event.target.value)} /></label>
        </section>
        <section className="settings-section full-settings">
          <h3>Match Weighting Flags</h3>
          <div className="form-grid compact">
            <label><span>Add weight when present</span><input value={form.boost_terms || ""} placeholder="robotics, product strategy, transformation" onChange={(event) => update("boost_terms", event.target.value)} /></label>
            <label><span>Subtract weight when present</span><input value={form.penalty_terms || ""} placeholder="shift work, on call, weekend work" onChange={(event) => update("penalty_terms", event.target.value)} /></label>
          </div>
        </section>
        </>
        ) : null}
        {section === "documents" ? (
        <>
        <section className="settings-section full-settings">
          <h3>Application Documents</h3>
          <div className="form-grid compact">
            <label className="full"><span>Document strategy</span><textarea value={form.document_strategy || ""} placeholder="Lead with delivery leadership, business outcomes, and credible technical depth." onChange={(event) => update("document_strategy", event.target.value)} /></label>
          </div>
          <p className="settings-hint">Documents are authored from your Evidence Library and rendered with the hybrid renderer (model decides structure; clean styling guaranteed), then fact-checked. Use the Evidence tab to manage the corpus.</p>
        </section>
        <MemoryPanel
          memoryStatus={memoryStatus}
          memoryFragments={memoryFragments}
          memoryBusy={memoryBusy}
          onScanMemory={onScanMemory}
        />
        </>
        ) : null}
        {section === "evidence" ? (
          <EvidenceLibraryPanel profileId={profile?.id || 1} />
        ) : null}
        {section === "maintenance" ? (
        <section className="settings-section full-settings">
          <h3>Maintenance</h3>
          <div className="maintenance-row">
            <button className="secondary" disabled={compacting} onClick={compactDatabase}><RefreshCw size={16} /> {compacting ? "Compacting..." : "Compact database"}</button>
            {compactResult ? (
              <span>
                Total {formatBytes(compactResult.before_bytes)} to {formatBytes(compactResult.after_bytes)}
                {compactResult.reclaimed_bytes
                  ? `, reclaimed ${formatBytes(compactResult.reclaimed_bytes)}`
                  : compactResult.delta_bytes > 0
                    ? `, grew by ${formatBytes(compactResult.delta_bytes)} after merging WAL`
                    : ", no space reclaimed"}
                . Main DB {formatBytes(compactResult.before_main_bytes)} to {formatBytes(compactResult.after_main_bytes)}.
              </span>
            ) : <span>Checkpoint WAL and vacuum the local SQLite database.</span>}
          </div>
        </section>
        ) : null}
      </div>
      <footer className="settings-actions">
        {settingsScope === "lane" ? <button className="secondary" onClick={() => onApplyFilters(form)}><Filter size={16} /> Apply to filters</button> : null}
        {settingsScope === "lane" ? <button onClick={() => onSave(form)}><Check size={16} /> Save lane settings</button> : null}
        {settingsScope === "general" ? (
          section === "folders"
            ? <button onClick={() => onSaveGlobal(globalForm)}><Check size={16} /> Save folder settings</button>
            : section === "ai"
              ? <button onClick={() => onSaveGlobal(globalForm)}><Check size={16} /> Save AI settings</button>
            : <button onClick={() => onSave(form)}><Check size={16} /> Save general settings</button>
        ) : null}
      </footer>
    </section>
  );
}

function UpdateToast({ update, onDismiss }) {
  if (!update || update.status === "idle") return null;

  const versionLabel = update.version ? ` ${update.version}` : "";
  const downloading = update.status === "downloading";

  return (
    <aside className="update-toast" role="status" aria-live="polite" aria-label="Software update">
      <div className="update-toast-icon"><Download size={20} /></div>
      <div className="update-toast-body">
        {update.status === "available" ? (
          <>
            <strong>JSE{versionLabel} is available</strong>
            <span>A newer version is ready to download from GitHub.</span>
          </>
        ) : null}
        {downloading ? (
          <>
            <strong>Downloading update…</strong>
            <span>{update.percent || 0}% complete</span>
            <div className="update-progress" aria-label={`${update.percent || 0}% downloaded`}>
              <span style={{ width: `${update.percent || 0}%` }} />
            </div>
          </>
        ) : null}
        {update.status === "ready" ? (
          <>
            <strong>JSE{versionLabel} is ready</strong>
            <span>Restart JSE to finish installing the update.</span>
          </>
        ) : null}
        {update.status === "error" ? (
          <>
            <strong>Update download failed</strong>
            <span>{update.message || "Please try again later."}</span>
          </>
        ) : null}
        <div className="update-toast-actions">
          {update.status === "available" ? (
            <>
              <button onClick={() => window.jobAssistant.downloadUpdate()}><Download size={14} /> Update</button>
              <button className="secondary" onClick={onDismiss}>Later</button>
            </>
          ) : null}
          {update.status === "ready" ? <button onClick={() => window.jobAssistant.installUpdate()}><RefreshCw size={14} /> Restart & install</button> : null}
          {update.status === "error" ? <button className="secondary" onClick={onDismiss}>Dismiss</button> : null}
        </div>
      </div>
      <button className="update-toast-close" aria-label="Dismiss update notification" onClick={onDismiss}><X size={16} /></button>
    </aside>
  );
}

function App() {
  const [booting, setBooting] = useState(true);
  const [status, setStatus] = useState("Idle");
  const [logs, setLogs] = useState([]);
  const [latestLog, setLatestLog] = useState("");
  const [profiles, setProfiles] = useState([]);
  const [activeProfileId, setActiveProfileId] = useState(1);
  const [includeAllProfiles, setIncludeAllProfiles] = useState(false);
  const [sources, setSources] = useState([]);
  const [searchSources, setSearchSources] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [dashboard, setDashboard] = useState(null);
  const [calendar, setCalendar] = useState([]);
  const [campaignPlan, setCampaignPlan] = useState(null);
  const [hiddenMarket, setHiddenMarket] = useState(null);
  const [hiddenMarketDays, setHiddenMarketDays] = useState(60);
  const [hiddenMarketBusy, setHiddenMarketBusy] = useState(false);
  const [stats, setStats] = useState(null);
  const [statsPeriod, setStatsPeriod] = useState(7);
  const [statsBusy, setStatsBusy] = useState(false);
  const [settings, setSettings] = useState(null);
  const [globalSettings, setGlobalSettings] = useState(null);
  const [scrapers, setScrapers] = useState([]);
  const [scraperError, setScraperError] = useState("");
  const [memoryStatus, setMemoryStatus] = useState(null);
  const [memoryFragments, setMemoryFragments] = useState([]);
  const [view, setView] = useState("dashboard");
  const [filters, setFilters] = useState({ query: "", stage: "", source: "", company: "", location: "", work_modes: [], min_score: "", max_score: "", date_from: "", has_interview: false, has_feedback: false });
  const [interestedSort, setInterestedSort] = useState("match");
  const [activeTasks, setActiveTasks] = useState({});
  const [docsBatchProgress, setDocsBatchProgress] = useState(null);
  const [runSearchOpen, setRunSearchOpen] = useState(false);
  const [addLaneOpen, setAddLaneOpen] = useState(false);
  const [addLaneBusy, setAddLaneBusy] = useState(false);
  const [addJobOpen, setAddJobOpen] = useState(false);
  const [addJobBusy, setAddJobBusy] = useState(false);
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [quickMove, setQuickMove] = useState(null);
  const [rejectJob, setRejectJob] = useState(null);
  const [workspace, setWorkspace] = useState({ job: null, events: [], interviews: [], tab: "Details" });
  const [documentViewer, setDocumentViewer] = useState(null);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [campaignBusy, setCampaignBusy] = useState(false);
  const [dialog, setDialog] = useState(null);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [onboardingBusy, setOnboardingBusy] = useState(false);
  const [prerequisites, setPrerequisites] = useState(null);
  const [appUpdate, setAppUpdate] = useState(null);
  const [updateToastVisible, setUpdateToastVisible] = useState(false);
  const refreshRequestId = useRef(0);

  // Host for appConfirm / appPrompt / appNotice (replaces Electron-breaking
  // native dialogs). Rendered last in the tree so it paints above other modals.
  useEffect(() => {
    dialogBridge.current = (request) => new Promise((resolve) => setDialog({ ...request, resolve }));
    return () => { dialogBridge.current = null; };
  }, []);

  useEffect(() => {
    const receiveUpdate = (nextUpdate) => {
      setAppUpdate(nextUpdate);
      if (["available", "ready", "error"].includes(nextUpdate?.status)) {
        setUpdateToastVisible(true);
      }
    };
    window.jobAssistant.getUpdateStatus?.().then(receiveUpdate).catch(() => {});
    const unsubscribe = window.jobAssistant.onUpdateStatus?.(receiveUpdate);
    return () => unsubscribe?.();
  }, []);
  const closeDialog = (result) => {
    if (dialog) dialog.resolve(result);
    setDialog(null);
  };

  // A file dropped outside a drop zone would otherwise navigate the whole
  // window to that file, replacing the app until restart.
  useEffect(() => {
    const preventDefault = (event) => event.preventDefault();
    window.addEventListener("dragover", preventDefault);
    window.addEventListener("drop", preventDefault);
    return () => {
      window.removeEventListener("dragover", preventDefault);
      window.removeEventListener("drop", preventDefault);
    };
  }, []);

  const activeProfile = profiles.find((profile) => profile.id === activeProfileId);
  const runningTaskKeys = Object.keys(activeTasks);
  const busy = runningTaskKeys.length > 0;
  const searchBusy = Boolean(activeTasks.search);
  const analysisBusy = Boolean(activeTasks.analysis);
  const docsBusy = Boolean(activeTasks.docs);
  const memoryBusy = Boolean(activeTasks.memory);

  const appendLog = useCallback((message) => {
    const text = typeof message === "string" ? message : JSON.stringify(message);
    setLatestLog(text);
    setLogs((current) => [...current.slice(-250), { at: new Date().toLocaleTimeString(), text }]);
  }, []);

  const invoke = useCallback((command, payload = {}) => window.jobAssistant.invoke(command, payload), []);

  const updateFilter = (key, value) => setFilters((current) => ({ ...current, [key]: value }));
  const toggleFilterMode = (mode, checked) => setFilters((current) => ({
    ...current,
    work_modes: checked
      ? [...new Set([...(current.work_modes || []), mode])]
      : (current.work_modes || []).filter((item) => item !== mode)
  }));

  const applySettingsToFilters = useCallback((nextSettings) => {
    setFilters((current) => ({
      ...current,
      location: nextSettings?.preferred_location || "",
      work_modes: nextSettings?.work_modes || [],
      min_score: nextSettings?.default_min_score ?? ""
    }));
  }, []);

  const requestPayload = useMemo(() => ({
    ...filters,
    profile_id: activeProfileId,
    include_all_profiles: includeAllProfiles
  }), [activeProfileId, filters, includeAllProfiles]);

  const refresh = useCallback(async (profileIdOverride = null) => {
    const requestId = refreshRequestId.current + 1;
    refreshRequestId.current = requestId;
    const data = await invoke("app:refresh", {
      ...requestPayload,
      ...(profileIdOverride ? { profile_id: profileIdOverride, include_all_profiles: false } : {}),
      fragment_limit: 12,
      campaign_limit: 12,
      campaign_min_score: 65
    });
    if (requestId !== refreshRequestId.current) return;
    setProfiles(data.profiles);
    setSources(Array.from(new Set(data.sources || [])));
    setSearchSources(Array.from(new Set(data.search_sources || data.sources || [])));
    setJobs(data.jobs || []);
    setDashboard(data.dashboard);
    setCalendar(data.calendar || []);
    setMemoryStatus(data.memory);
    setMemoryFragments(data.fragments || []);
  }, [activeProfileId, includeAllProfiles, invoke, requestPayload]);

  useEffect(() => {
    Promise.all([invoke("app:init"), window.jobAssistant.getPrerequisites?.() || Promise.resolve(null)])
      .then(([data, prerequisiteData]) => {
        setProfiles(data.profiles);
        setActiveProfileId(data.active_profile_id);
        setSources(Array.from(new Set(data.sources || [])));
        setSearchSources(Array.from(new Set(data.search_sources || data.sources || [])));
        setGlobalSettings(data.app_settings || {});
        setPrerequisites(prerequisiteData);
        setOnboardingOpen(Boolean(data.needs_onboarding));
      })
      .catch((error) => appendLog(`Startup failed: ${toErrorMessage(error)}`))
      .finally(() => setBooting(false));
  }, [appendLog, invoke]);

  useEffect(() => {
    if (booting) return undefined;
    // Debounce so rapid filter changes (e.g. typing in the search box) coalesce
    // into a single app:refresh instead of spawning a Python process per keystroke.
    const handle = setTimeout(() => {
      refresh().catch((error) => appendLog(toErrorMessage(error)));
    }, 250);
    return () => clearTimeout(handle);
  }, [booting, refresh, appendLog]);

  useEffect(() => {
    setHiddenMarket(null);
    setStats(null);
    setCampaignPlan(null);
  }, [activeProfileId, includeAllProfiles]);

  const loadCampaignPlan = useCallback(async () => {
    try {
      const data = await invoke("campaign:plan", {
        profile_id: activeProfileId,
        include_all_profiles: includeAllProfiles,
      });
      setCampaignPlan(data);
    } catch (error) {
      appendLog(`Today's plan failed to load: ${toErrorMessage(error)}`);
    }
  }, [activeProfileId, appendLog, includeAllProfiles, invoke]);

  // Reload the plan whenever the Campaign view is visible and the underlying
  // jobs change (every refresh produces a new jobs array identity).
  useEffect(() => {
    if (view !== "campaign" || booting) return;
    loadCampaignPlan();
  }, [view, booting, jobs, loadCampaignPlan]);

  useEffect(() => {
    if (view !== "stats" || booting) return undefined;
    let active = true;
    setStatsBusy(true);
    invoke("stats:summary", { profile_id: activeProfileId, include_all_profiles: includeAllProfiles, days: statsPeriod })
      .then((data) => { if (active) setStats(data); })
      .catch((error) => appendLog(`Stats load failed: ${toErrorMessage(error)}`))
      .finally(() => { if (active) setStatsBusy(false); });
    return () => { active = false; };
  }, [view, statsPeriod, activeProfileId, includeAllProfiles, booting, invoke, appendLog]);

  useEffect(() => {
    if (booting || !activeProfileId) return;
    invoke("settings:get", { profile_id: activeProfileId })
      .then((data) => {
        setSettings(data.settings);
        applySettingsToFilters(data.settings);
      })
      .catch((error) => appendLog(`Settings load failed: ${toErrorMessage(error)}`));
    invoke("settings:globalGet")
      .then((data) => setGlobalSettings(data.settings))
      .catch((error) => appendLog(`Global settings load failed: ${toErrorMessage(error)}`));
    invoke("scrapers:list", { profile_id: activeProfileId })
      .then((data) => {
        setScrapers(data.scrapers || []);
        setScraperError("");
      })
      .catch((error) => {
        const message = `Scraper load failed: ${toErrorMessage(error)}`;
        setScraperError(message);
        appendLog(message);
      });
  }, [activeProfileId, appendLog, applySettingsToFilters, booting, invoke]);

  const taskKindForCommand = (command) => {
    if (command.startsWith("scrape:")) return "search";
    if (command.startsWith("analysis:")) return "analysis";
    if (command.startsWith("docs:")) return "docs";
    if (command.startsWith("company:")) return "company";
    if (command.startsWith("memory:")) return "memory";
    if (command.startsWith("campaign:")) return "campaign";
    if (command.startsWith("lanes:")) return "laneSetup";
    return command;
  };

  const runTask = useCallback((command, payload, doneMessage, refreshProfileId = null) => {
    const taskKind = taskKindForCommand(command);
    if (activeTasks[taskKind]) {
      appendLog(`${taskKind} is already running.`);
      return;
    }
    setStatus("Running");
    appendLog(`Started ${command}`);
    const task = window.jobAssistant.startTask(command, payload, (event) => {
      if (event.type === "log") appendLog(event.message);
      if (event.type === "status") setStatus(event.message || "Running");
      if (event.type === "result") {
        appendLog(doneMessage || "Task complete");
        if (command.startsWith("docs:") && event.data) {
          const paths = [event.data.resume_path, event.data.cover_letter_path].filter(Boolean);
          if (paths.length) appendLog(`Documents saved: ${paths.join(" | ")}`);
          if (event.data.review?.verdict) {
            appendLog(`Document review: ${event.data.review.verdict}${event.data.review.summary ? ` - ${event.data.review.summary}` : ""}`);
          }
          if (payload.job_id) {
            setWorkspace((current) => {
              if (!current.job || current.job.id !== payload.job_id) return current;
              return {
                ...current,
                tab: "Application",
                job: {
                  ...current.job,
                  resume_used: event.data.resume_path || current.job.resume_used,
                  resume_text: event.data.resume_text || current.job.resume_text,
                  cover_letter_path: event.data.cover_letter_path || current.job.cover_letter_path,
                  cover_letter_text: event.data.cover_letter_text || current.job.cover_letter_text,
                }
              };
            });
          }
        }
        setActiveTasks((current) => {
          const next = { ...current };
          delete next[taskKind];
          setStatus(Object.keys(next).length ? "Running" : "Idle");
          return next;
        });
        task.unsubscribe();
        refresh(refreshProfileId)
          .then(() => {
            if (command === "analysis:job" && payload.job_id) return openJob(payload.job_id);
            if (command === "company:research" && payload.job_id) return openJob(payload.job_id, "Company");
            if (command.startsWith("docs:") && payload.job_id) return openJob(payload.job_id, "Application");
            return null;
          })
          .catch((error) => appendLog(toErrorMessage(error)));
      }
      if (event.type === "error") {
        appendLog(`Error: ${event.message}`);
        setActiveTasks((current) => {
          const next = { ...current };
          delete next[taskKind];
          setStatus(Object.keys(next).length ? "Running" : "Idle");
          return next;
        });
        task.unsubscribe();
      }
    });
    setActiveTasks((current) => ({ ...current, [taskKind]: task }));
  }, [activeTasks, appendLog, refresh]);

  const stopAllTasks = () => {
    for (const task of Object.values(activeTasks)) {
      task.cancel();
      task.unsubscribe();
    }
    window.jobAssistant.stopAllTasks?.();
    setActiveTasks({});
    setDocsBatchProgress((current) => current?.running
      ? { ...current, running: false, status: "cancelled", message: "Batch cancelled." }
      : current);
    setStatus("Idle");
    appendLog("Stop requested. Search, analysis, document, and company tasks were terminated.");
  };

  const openJob = useCallback(async (jobOrId, tab = "Details") => {
    const jobId = typeof jobOrId === "object" ? jobOrId.id : jobOrId;
    const data = await invoke("jobs:detail", { job_id: jobId });
    setWorkspace({ job: data.job, events: data.events, interviews: data.interviews || [], tab });
  }, [invoke]);

  const onDragStart = useCallback((event, job) => {
    event.dataTransfer.setData("text/plain", JSON.stringify({ id: job.id }));
  }, []);

  const onDropStage = (event, stage) => {
    event.preventDefault();
    let data;
    try {
      data = JSON.parse(event.dataTransfer.getData("text/plain") || "");
    } catch {
      return; // not a kanban card payload (e.g. external file or text drag)
    }
    const job = jobs.find((item) => item.id === data.id);
    if (job && job.pipeline_stage !== stage) setQuickMove({ job, stage });
  };

  const saveQuickMove = async (updates) => {
    if (updates.pipeline_stage === "interviewing") {
      await invoke("interviews:add", {
        job_id: quickMove.job.id,
        interview: {
          title: "Interview",
          interview_date: updates.interview_date,
          interview_type: updates.interview_type,
          people_met: updates.interview_people,
          notes: updates.notes,
          next_action: updates.next_action,
          next_action_date: updates.next_action_date
        }
      });
    } else {
      await invoke("jobs:update", { job_id: quickMove.job.id, updates });
    }
    setQuickMove(null);
    await refresh();
  };

  const rejectFromWorkspace = (job) => {
    setWorkspace({ job: null, events: [], interviews: [], tab: "Details" });
    setRejectJob(job);
  };

  const moveInterestedFromWorkspace = (job) => {
    setWorkspace({ job: null, events: [], interviews: [], tab: "Details" });
    setQuickMove({ job, stage: "interested" });
  };

  const rejectSelectedJob = async (reason) => {
    if (!rejectJob) return;
    const updates = {
      pipeline_stage: "rejected",
      next_action: "",
      next_action_date: "",
      retired_reason: reason || "Rejected manually"
    };
    const data = await invoke("jobs:update", { job_id: rejectJob.id, updates });
    if (workspace.job?.id === rejectJob.id) {
      setWorkspace((current) => ({ ...current, job: data.job, events: data.events, interviews: data.interviews || current.interviews }));
    }
    setRejectJob(null);
    await refresh();
  };

  const archiveCleanupJobs = async (jobIds) => {
    const data = await invoke("jobs:cleanupArchive", {
      job_ids: jobIds,
      reason: "No response after 30 days",
    });
    appendLog(`Archived ${data.count} stale application${data.count === 1 ? "" : "s"} as no response.`);
    setCleanupOpen(false);
    await refresh();
  };

  const stageCampaignAttackQueue = async () => {
    const confirmed = await appConfirm({
      title: "Stage Attack Queue",
      message: "Stage the top campaign-scored new roles into Interested?",
      confirmLabel: "Stage roles"
    });
    if (!confirmed) return;
    setCampaignBusy(true);
    try {
      const data = await invoke("campaign:stageAttackQueue", {
        profile_id: activeProfileId,
        include_all_profiles: includeAllProfiles,
        limit: 12,
        min_score: 65,
      });
      appendLog(`Campaign staged ${data.moved?.length || 0} role${data.moved?.length === 1 ? "" : "s"} for attack.`);
      await refresh();
    } catch (error) {
      appendLog(`Campaign staging failed: ${toErrorMessage(error)}`);
    } finally {
      setCampaignBusy(false);
    }
  };

  const refreshCampaignActions = async () => {
    setCampaignBusy(true);
    try {
      const data = await invoke("campaign:refreshActions", {
        profile_id: activeProfileId,
        include_all_profiles: includeAllProfiles,
      });
      appendLog(`Campaign refreshed ${data.changed?.length || 0} active action${data.changed?.length === 1 ? "" : "s"}.`);
      await refresh();
    } catch (error) {
      appendLog(`Campaign action refresh failed: ${toErrorMessage(error)}`);
    } finally {
      setCampaignBusy(false);
    }
  };

  const stageJobFromPlan = (job) => setQuickMove({ job, stage: "interested" });

  const addManualJob = async (form) => {
    setAddJobBusy(true);
    try {
      const data = await invoke("jobs:addManual", { profile_id: activeProfileId, ...form });
      setAddJobOpen(false);
      appendLog(data.added ? `Added job manually: ${form.title}` : `Not added — ${data.message}`);
      await refresh();
      if (data.job_id) {
        await openJob(data.job_id);
        if (form.analyze && data.added && form.description.trim()) {
          runTask("analysis:job", { job_id: data.job_id }, "Job analysis complete.");
        }
      }
    } catch (error) {
      appendLog(`Add job failed: ${toErrorMessage(error)}`);
    } finally {
      setAddJobBusy(false);
    }
  };

  const markFollowedUp = async (job) => {
    try {
      await invoke("events:add", { job_id: job.id, event_type: "note", title: "Followed up", details: "Logged from Today's Plan." });
      await invoke("jobs:update", { job_id: job.id, updates: { next_action: "Await response", next_action_date: todayPlus(5) } });
      appendLog(`Follow-up logged for ${job.title}; next check in 5 days.`);
      await refresh();
    } catch (error) {
      appendLog(`Could not log follow-up: ${toErrorMessage(error)}`);
    }
  };

  const generateDocsForJob = (job) => {
    if (!job) return;
    appendLog(`Generating context-grounded documents for ${job.title} with ${documentAiLabel(settings)}.`);
    runTask(
      "docs:generateRich",
      { profile_id: job.profile_id, job_id: job.id },
      "Application documents generated (with evidence review)."
    );
  };

  const generateInterestedDocs = async () => {
    const candidates = groupedJobs.interested || [];
    if (!candidates.length) {
      appendLog("There are no jobs in the current Interested list.");
      return;
    }
    if (activeTasks.docs) {
      appendLog("Document generation is already running.");
      return;
    }
    const confirmed = await appConfirm({
      title: "Generate Interested documents",
      message: `Generate a tailored resume and cover letter for all ${candidates.length} job${candidates.length === 1 ? "" : "s"} currently shown in Interested? Jobs are processed one at a time and existing generated files for the same company and role are replaced.`,
      confirmLabel: `Generate ${candidates.length} job${candidates.length === 1 ? "" : "s"}`
    });
    if (!confirmed) return;

    const initial = {
      current: 0,
      total: candidates.length,
      succeeded: 0,
      failed: 0,
      skipped: 0,
      running: true,
      status: "starting",
      message: `Preparing ${candidates.length} Interested job${candidates.length === 1 ? "" : "s"}…`
    };
    setDocsBatchProgress(initial);
    setStatus("Generating Interested docs");
    appendLog(`Started document batch for ${candidates.length} Interested job${candidates.length === 1 ? "" : "s"}.`);

    let task;
    task = window.jobAssistant.startTask(
      "docs:generateInterestedBatch",
      { job_ids: candidates.map((job) => job.id) },
      (event) => {
        if (event.type === "log") appendLog(event.message);
        if (event.type === "status") setStatus(event.message || "Generating Interested docs");
        if (event.type === "progress") {
          setDocsBatchProgress({ ...event, running: true });
          setStatus(event.message || "Generating Interested docs");
        }
        if (event.type === "result") {
          const result = event.data || {};
          setDocsBatchProgress({
            current: result.total || candidates.length,
            total: result.total || candidates.length,
            succeeded: result.succeeded || 0,
            failed: result.failed || 0,
            skipped: result.skipped || 0,
            running: false,
            status: result.failed ? "completed_with_errors" : "completed",
            message: `Finished: ${result.succeeded || 0} generated${result.skipped ? `, ${result.skipped} closed and skipped` : ""}${result.failed ? `, ${result.failed} failed` : ""}.`
          });
          appendLog(`Interested document batch complete: ${result.succeeded || 0} generated, ${result.skipped || 0} closed and skipped, ${result.failed || 0} failed.`);
          setActiveTasks((current) => {
            const next = { ...current };
            delete next.docs;
            setStatus(Object.keys(next).length ? "Running" : "Idle");
            return next;
          });
          task.unsubscribe();
          refresh().catch((error) => appendLog(toErrorMessage(error)));
        }
        if (event.type === "error") {
          const cancelled = /cancel/i.test(event.message || "");
          setDocsBatchProgress((current) => ({
            ...(current || initial),
            running: false,
            status: cancelled ? "cancelled" : "failed",
            message: cancelled ? "Batch cancelled." : `Batch stopped: ${event.message}`
          }));
          appendLog(cancelled ? "Interested document batch cancelled." : `Interested document batch failed: ${event.message}`);
          setActiveTasks((current) => {
            const next = { ...current };
            delete next.docs;
            setStatus(Object.keys(next).length ? "Running" : "Idle");
            return next;
          });
          task.unsubscribe();
        }
      }
    );
    setActiveTasks((current) => ({ ...current, docs: task }));
  };

  const loadHiddenMarket = useCallback(async () => {
    setHiddenMarketBusy(true);
    try {
      const data = await invoke("hiddenMarket:get", {
        profile_id: activeProfileId,
        include_all_profiles: includeAllProfiles,
        days: hiddenMarketDays,
      });
      setHiddenMarket(data);
    } catch (error) {
      appendLog(`Hidden market scan failed: ${toErrorMessage(error)}`);
    } finally {
      setHiddenMarketBusy(false);
    }
  }, [activeProfileId, includeAllProfiles, hiddenMarketDays, invoke, appendLog]);

  // Auto-load the Hidden Market tab on open and whenever the lane, scope, or
  // window changes. Declared after loadHiddenMarket so the dependency is not in
  // the temporal dead zone on first render.
  useEffect(() => {
    if (view !== "hiddenMarket" || booting) return;
    loadHiddenMarket();
  }, [view, booting, loadHiddenMarket]);

  const trackHiddenTarget = async (target) => {
    try {
      await invoke("hiddenMarket:track", {
        profile_id: activeProfileId,
        target_type: target.target_type,
        target_name: target.name,
        contact_person: target.contact_person,
        contact_email: target.contact_email,
        contact_phone: target.contact_phone,
        domain: target.domain,
      });
      await loadHiddenMarket();
    } catch (error) {
      appendLog(`Could not track target: ${toErrorMessage(error)}`);
    }
  };

  const hiddenLeadUpdate = async (leadId, updates) => {
    try {
      await invoke("hiddenMarket:leadUpdate", { id: leadId, updates });
      await loadHiddenMarket();
    } catch (error) {
      appendLog(`Lead update failed: ${toErrorMessage(error)}`);
    }
  };

  const hiddenLeadTouch = async (leadId, touch) => {
    await invoke("hiddenMarket:touch", { id: leadId, ...touch });
    await loadHiddenMarket();
  };

  const hiddenLeadConvert = async (lead) => {
    const ok = await appConfirm({
      title: "Convert to applied",
      message: `Convert "${lead.target_name}" into a tracked job at the Applied stage?`,
      confirmLabel: "Convert",
    });
    if (!ok) return;
    try {
      const result = await invoke("hiddenMarket:convert", { id: lead.id });
      appendLog(`Converted "${lead.target_name}" to an applied job.`);
      await Promise.all([loadHiddenMarket(), refresh()]);
      if (result?.job_id) openJob(result.job_id);
    } catch (error) {
      appendLog(`Convert failed: ${toErrorMessage(error)}`);
    }
  };

  const hiddenLeadDelete = async (lead) => {
    const ok = await appConfirm({
      title: "Delete outreach lead",
      message: `Delete the outreach lead for "${lead.target_name}"? This does not affect any converted job.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await invoke("hiddenMarket:leadDelete", { id: lead.id });
      await loadHiddenMarket();
    } catch (error) {
      appendLog(`Delete failed: ${toErrorMessage(error)}`);
    }
  };

  const hiddenStrategy = async (target) => {
    try {
      const data = await invoke("hiddenMarket:strategy", { profile_id: activeProfileId, target });
      return data?.strategy || "";
    } catch (error) {
      appendLog(`AI angle failed: ${toErrorMessage(error)}`);
      return "";
    }
  };

  const saveWorkspace = async (updates) => {
    const data = await invoke("jobs:update", { job_id: workspace.job.id, updates });
    setWorkspace((current) => ({ ...current, job: data.job, events: data.events, interviews: data.interviews || current.interviews }));
    await refresh();
  };

  const moveToAppliedFromApplicationDate = async (applicationDate) => {
    if (!workspace.job) return;
    const updates = {
      application_date: applicationDate,
      pipeline_stage: "applied",
      status: "applied",
      next_action: workspace.job.next_action || "Follow up",
      next_action_date: workspace.job.next_action_date || todayPlus(7),
    };
    const data = await invoke("jobs:update", { job_id: workspace.job.id, updates });
    setWorkspace((current) => ({ ...current, job: data.job, events: data.events, interviews: data.interviews || current.interviews }));
    appendLog("Application date saved and job moved to Applied.");
    await refresh();
  };

  const moveWorkspaceProfile = async (profileId) => {
    if (!workspace.job) return;
    if (Number(profileId) === Number(workspace.job.profile_id)) return;
    const targetProfile = profiles.find((profile) => Number(profile.id) === Number(profileId));
    let data;
    try {
      data = await invoke("jobs:moveProfile", { job_id: workspace.job.id, profile_id: profileId });
    } catch (error) {
      appendLog(`Lane move failed: ${toErrorMessage(error)}`);
      throw error;
    }
    setWorkspace((current) => ({ ...current, job: data.job, events: data.events, interviews: data.interviews || current.interviews }));
    const profileName = targetProfile?.name || "selected lane";
    appendLog(`Moved ${workspace.job.title} to ${profileName}. Fit analysis was cleared for re-review.`);
    await refresh();
    const shouldAnalyze = await appConfirm({
      title: "Re-run fit analysis?",
      message: `Moved to ${profileName}. Re-run the AI fit review with this lane now?`,
      confirmLabel: "Re-analyze"
    });
    if (shouldAnalyze) {
      runTask("analysis:job", { job_id: data.job.id }, "Job re-analysis complete.");
    }
    return data;
  };

  const addInterview = async (interview) => {
    const data = await invoke("interviews:add", { job_id: workspace.job.id, interview });
    setWorkspace((current) => ({ ...current, job: data.job, events: data.events, interviews: data.interviews || [] }));
    await refresh();
  };

  const updateInterview = async (interviewId, interview) => {
    const data = await invoke("interviews:update", { interview_id: interviewId, interview });
    setWorkspace((current) => ({ ...current, job: data.job, events: data.events, interviews: data.interviews || [] }));
    await refresh();
  };

  const addWorkspaceEvent = async (details) => {
    const data = await invoke("events:add", { job_id: workspace.job.id, event_type: "note", title: "Note", details });
    setWorkspace((current) => ({ ...current, events: data.events }));
    await refresh();
  };

  const generateDocs = (additionalCandidateContext = "") => {
    if (!workspace.job) return;
    appendLog(`Generating context-grounded documents with ${documentAiLabel(settings)}.`);
    runTask(
      "docs:generateRich",
      {
        profile_id: workspace.job.profile_id,
        job_id: workspace.job.id,
        position_description_text: workspace.job.position_description_text || "",
        additional_candidate_context: additionalCandidateContext
      },
      "Application documents generated (with evidence review)."
    );
  };

  const downloadDocument = async (filePath) => {
    if (!filePath) return;
    try {
      if (!window.jobAssistant.downloadFile) {
        appendLog("Download needs an app restart to activate; showing the document location instead.");
        await window.jobAssistant.showPath(filePath);
        return;
      }
      const result = await window.jobAssistant.downloadFile(filePath);
      if (result?.canceled) {
        appendLog("Document download cancelled.");
      } else {
        appendLog(`Document downloaded: ${result?.path || filePath}`);
      }
    } catch (error) {
      const message = toErrorMessage(error);
      if (message.includes("No handler registered for 'shell:downloadFile'")) {
        appendLog("Download handler will activate after an app restart; showing the document location instead.");
        try {
          await window.jobAssistant.showPath(filePath);
        } catch (fallbackError) {
          appendLog(`Open document location failed: ${toErrorMessage(fallbackError)}`);
        }
        return;
      }
      appendLog(`Document download failed: ${message}`);
    }
  };

  const revealDocument = async (filePath) => {
    if (!filePath) return;
    try {
      await window.jobAssistant.showPath(filePath);
    } catch (error) {
      appendLog(`Open document location failed: ${toErrorMessage(error)}`);
    }
  };

  const scanProfileMemory = () => {
    if (!activeProfileId) return;
    runTask(
      "memory:scan",
      { profile_id: activeProfileId, limit: 100 },
      "Lane application memory updated."
    );
  };

  const generateApplicationPrompt = async (additionalCandidateContext = "") => {
    if (!workspace.job) return;
    const data = await invoke("application:prompt", {
      profile_id: workspace.job.profile_id,
      job_id: workspace.job.id,
      additional_candidate_context: additionalCandidateContext
    });
    appendLog(`External LLM prompt saved: ${data.prompt_path}`);
    if (data.memory_alignment?.selected_fragments?.length) {
      appendLog(`Prompt includes ${data.memory_alignment.selected_fragments.length} lane memory fragment${data.memory_alignment.selected_fragments.length === 1 ? "" : "s"}.`);
    }
    setDocumentViewer({ title: "External LLM application prompt", text: data.prompt });
    const detail = await invoke("jobs:detail", { job_id: workspace.job.id });
    setWorkspace((current) => ({ ...current, job: detail.job, events: detail.events, interviews: detail.interviews || current.interviews }));
    await refresh();
  };

  const generateCampaignPack = async (job) => {
    if (!job) return;
    try {
      const data = await invoke("application:prompt", { profile_id: job.profile_id, job_id: job.id });
      appendLog(`Campaign attack pack saved: ${data.prompt_path}`);
      setDocumentViewer({ title: `Attack pack: ${job.title}`, text: data.prompt });
      await refresh();
    } catch (error) {
      appendLog(`Campaign attack pack failed: ${toErrorMessage(error)}`);
    }
  };

  const researchCompany = () => {
    if (!workspace.job) return;
    runTask("company:research", { job_id: workspace.job.id }, "Company intelligence updated.");
  };

  const researchStageCompanies = (stageId) => {
    const candidates = (groupedJobs[stageId] || []).filter((job) => !hasCompanyResearch(job));
    if (!candidates.length) {
      appendLog("No employer intel gaps in Interested. Already researched or cached.");
      return;
    }
    runTask(
      "company:researchBatch",
      { job_ids: candidates.map((job) => job.id), stage: stageId },
      `Employer intel research complete for ${candidates.length} Interested jobs.`
    );
  };

  const analyzeJob = (job) => {
    if (!job) return;
    runTask("analysis:job", { job_id: job.id }, job.ai_analysis ? "Job re-analysis complete." : "Job analysis complete.");
  };

  const extractDroppedDocument = async (docType, file) => {
    const filePath = window.jobAssistant.getPathForFile?.(file) || file.path || file.webkitRelativePath;
    if (!filePath) {
      appendLog("Could not read the dropped file path. Use a normal filesystem file, not a browser/cloud placeholder.");
      return;
    }
    try {
      const data = await invoke("document:extract", {
        job_id: workspace.job.id,
        doc_type: docType,
        path: filePath
      });
      const detail = await invoke("jobs:detail", { job_id: workspace.job.id });
      setWorkspace((current) => ({ ...current, job: detail.job, events: detail.events, interviews: detail.interviews || current.interviews }));
      if (!data.text || !data.text.trim()) {
        appendLog(`Uploaded ${file.name}, but no text could be extracted (scanned/image PDF or empty doc?). It won't contribute to analysis.`);
      } else {
        appendLog(`Uploaded ${file.name}; extracted ${data.text.length} characters and autosaved.`);
      }
      await refresh();
    } catch (error) {
      appendLog(`Could not attach ${file.name}: ${toErrorMessage(error)}`);
    }
  };

  const deleteJob = async (jobId) => {
    await invoke("jobs:delete", { job_id: jobId });
    setWorkspace({ job: null, events: [], interviews: [], tab: "Details" });
    await refresh();
  };

  const createLane = async (setup) => {
    setAddLaneBusy(true);
    try {
      const data = await invoke("profiles:add", {
        name: setup.name.trim(),
        resume_path: setup.resume_path.trim(),
        settings: {
          lane_intent: setup.lane_intent.trim(),
          target_titles: setup.target_titles.trim(),
          target_domains: setup.target_domains.trim(),
          seniority: setup.seniority.trim(),
          preferred_location: setup.preferred_location.trim(),
          work_modes: setup.work_modes,
          must_have_terms: setup.must_have_terms.trim(),
          avoid_terms: setup.avoid_terms.trim(),
        },
      });
      const lane = (data.profiles || []).find((profile) => profile.name === setup.name.trim());
      if (!lane) throw new Error("The lane was created but could not be selected.");

      setProfiles(data.profiles || []);
      setIncludeAllProfiles(false);
      setActiveProfileId(lane.id);
      setAddLaneOpen(false);
      appendLog(`Created lane: ${lane.name}.`);
      applySettingsToFilters({
        preferred_location: setup.preferred_location.trim(),
        work_modes: setup.work_modes,
      });

      runTask(
        "lanes:bootstrap",
        {
          profile_id: lane.id,
          keyword_mode: setup.keyword_mode,
          terms: setup.terms,
          optimism: setup.optimism,
          generate_fragments: setup.generate_fragments,
        },
        `Lane setup complete for ${lane.name}.`,
        lane.id,
      );
    } finally {
      setAddLaneBusy(false);
    }
  };

  const importResume = async (resumePath) => {
    const imported = await invoke("resume:import", { path: resumePath });
    appendLog(`Resume imported to ${imported.resume_path}`);
    return imported.resume_path;
  };

  const searchResumes = useCallback(async (query, current) => {
    const data = await invoke("resumes:list", { query, current });
    return data.resumes || [];
  }, [invoke]);

  const saveSettings = async (nextSettings) => {
    const data = await invoke("settings:update", { profile_id: activeProfileId, settings: nextSettings });
    setSettings(data.settings);
    applySettingsToFilters(data.settings);
    appendLog("Settings saved.");
    await refresh();
  };

  const saveGlobalSettings = async (nextSettings) => {
    const data = await invoke("settings:globalUpdate", { settings: nextSettings });
    setGlobalSettings(data.settings);
    setSettings((current) => ({ ...(current || {}), ...(data.settings || {}) }));
    appendLog("Global settings saved.");
  };

  const finishOnboarding = async ({ name, resume_path, local_base_url, local_model }) => {
    setOnboardingBusy(true);
    try {
      const data = await invoke("profiles:update", {
        profile_id: activeProfileId,
        name,
        resume_path,
      });
      setProfiles(data.profiles || []);
      const saved = await invoke("settings:globalUpdate", { settings: {
        onboarding_completed: true,
        onboarding_version: 1,
        local_base_url,
        local_model,
      } });
      setGlobalSettings(saved.settings || {});
      setOnboardingOpen(false);
      appendLog("First-run setup complete.");
      await refresh(activeProfileId);
    } finally {
      setOnboardingBusy(false);
    }
  };

  const skipOnboarding = async () => {
    await invoke("settings:globalUpdate", { settings: { onboarding_completed: true, onboarding_version: 1 } });
    setOnboardingOpen(false);
    appendLog("First-run setup skipped. You can finish configuration in Settings.");
  };

  const saveProfile = async (profileUpdates) => {
    const data = await invoke("profiles:update", {
      profile_id: activeProfileId,
      name: profileUpdates.name.trim(),
      resume_path: profileUpdates.resume_path.trim()
    });
    setProfiles(data.profiles);
    appendLog("Lane saved.");
    await refresh();
  };

  const compactDatabase = async () => {
    appendLog("Compacting database...");
    const result = await invoke("database:compact");
    appendLog(`Database compacted. Reclaimed ${formatBytes(result.reclaimed_bytes)}.`);
    return result;
  };
  const refreshScrapers = async () => {
    const data = await invoke("scrapers:list", { profile_id: activeProfileId });
    setScrapers(data.scrapers || []);
    const sourceData = await invoke("sources:list", { profile_id: activeProfileId, include_all_profiles: includeAllProfiles });
    setSources(Array.from(new Set(sourceData.sources || [])));
    const scraperData = await invoke("scrapers:list", { profile_id: activeProfileId });
    setSearchSources((scraperData.scrapers || []).filter((plugin) => plugin.enabled && plugin.lane_enabled !== false && !plugin.missing).map((plugin) => plugin.source_name));
  };
  const importScraper = async () => {
    try {
      setScraperError("");
      const pluginPath = await window.jobAssistant.chooseScraperPlugin?.();
      if (!pluginPath) return;
      const data = await invoke("scrapers:import", { profile_id: activeProfileId, path: pluginPath });
      setScrapers(data.scrapers || []);
      await refreshScrapers();
      appendLog("Scraper plugin imported.");
    } catch (error) {
      const message = `Scraper import failed: ${toErrorMessage(error)}`;
      setScraperError(message);
      appendLog(message);
    }
  };
  const buildScraper = async (answers) => {
    try {
      setScraperError("");
      appendLog(`Building scraper plugin for ${answers.source_name || answers.careers_url} with local LLM...`);
      const data = await invoke("scrapers:build", { profile_id: activeProfileId, answers });
      setScrapers(data.scrapers || []);
      await refreshScrapers();
      appendLog(`Scraper plugin built: ${data.plugin?.name || data.manifest?.name}.`);
      return data;
    } catch (error) {
      const message = `Scraper builder failed: ${toErrorMessage(error)}`;
      setScraperError(message);
      appendLog(message);
      throw error;
    }
  };
  const testScraper = async (pluginId, keyword, maxPages) => {
    try {
      setScraperError("");
      appendLog(`Testing scraper plugin ${pluginId}...`);
      const data = await invoke("scrapers:test", {
        profile_id: activeProfileId,
        id: pluginId,
        keyword,
        max_pages: maxPages || 1
      });
      appendLog(data.ok ? `Scraper dry run passed: ${pluginId}.` : `Scraper dry run needs review: ${pluginId}.`);
      return data;
    } catch (error) {
      const message = `Scraper test failed: ${toErrorMessage(error)}`;
      setScraperError(message);
      appendLog(message);
      throw error;
    }
  };
  const updateScraper = async (pluginId, updates) => {
    try {
      setScraperError("");
      const data = await invoke("scrapers:update", { profile_id: activeProfileId, id: pluginId, ...updates });
      setScrapers(data.scrapers || []);
      await refreshScrapers();
    } catch (error) {
      const message = `Scraper update failed: ${toErrorMessage(error)}`;
      setScraperError(message);
      appendLog(message);
    }
  };
  const updateLaneScraper = async (pluginId, updates) => {
    try {
      setScraperError("");
      const data = await invoke("scrapers:laneUpdate", { profile_id: activeProfileId, id: pluginId, ...updates });
      setScrapers(data.scrapers || []);
      await refreshScrapers();
    } catch (error) {
      const message = `Lane scraper update failed: ${toErrorMessage(error)}`;
      setScraperError(message);
      appendLog(message);
    }
  };
  const removeScraper = async (pluginId) => {
    try {
      setScraperError("");
      const data = await invoke("scrapers:remove", { profile_id: activeProfileId, id: pluginId });
      setScrapers(data.scrapers || []);
      await refreshScrapers();
    } catch (error) {
      const message = `Scraper removal failed: ${toErrorMessage(error)}`;
      setScraperError(message);
      appendLog(message);
    }
  };

  const groupedJobs = useMemo(() => {
    const groups = Object.fromEntries(PIPELINE.map((stage) => [stage.id, []]));
    for (const job of jobs) groups[normalizeStage(job.pipeline_stage, job.status)]?.push(job);
    const priorityWeight = { high: 0, normal: 1, low: 2 };
    const compareByMatch = (left, right) => {
      const scoreDelta = primaryScore(right) - primaryScore(left);
      if (scoreDelta) return scoreDelta;
      const priorityDelta = (priorityWeight[left.priority] ?? 1) - (priorityWeight[right.priority] ?? 1);
      if (priorityDelta) return priorityDelta;
      return Number(right.id || 0) - Number(left.id || 0);
    };
    const compareByDueDate = (left, right) => {
      const priorityDelta = (priorityWeight[left.priority] ?? 1) - (priorityWeight[right.priority] ?? 1);
      if (priorityDelta) return priorityDelta;
      const leftDue = left.next_action_date || "9999-12-31";
      const rightDue = right.next_action_date || "9999-12-31";
      if (leftDue !== rightDue) return leftDue.localeCompare(rightDue);
      return compareByMatch(left, right);
    };
    const compareByMostRecent = (left, right) => {
      const recent = (job) => job.updated_at || job.last_interaction_at || job.date_scraped || job.id || "";
      const leftRecent = recent(left);
      const rightRecent = recent(right);
      if (leftRecent !== rightRecent) return String(rightRecent).localeCompare(String(leftRecent));
      return compareByMatch(left, right);
    };
    groups.new.sort(compareByMatch);
    groups.interested.sort(interestedSort === "due" ? compareByDueDate : interestedSort === "recent" ? compareByMostRecent : compareByMatch);
    return groups;
  }, [jobs, interestedSort]);

  if (booting) return (
    <main className="boot">
      <div className="boot-panel">
        <div className="boot-loading"><Loader2 className="spin" /> Loading JSE</div>
        <p>{SUPPORT_MESSAGE}</p>
        <a href={SUPPORT_URL} onClick={openSupportLink}>☕ ko-fi.com/keljian</a>
      </div>
    </main>
  );

  const viewTitle = {
    dashboard: "Dashboard",
    campaign: "Campaign",
    hiddenMarket: "Hidden Market",
    pipeline: "Pipeline",
    stats: "Stats",
    activity: "Activity",
    settings: "Settings",
  }[view] || "Dashboard";

  return (
    <main className="ats-shell">
      {onboardingOpen ? <OnboardingWizard prerequisites={prerequisites} profile={activeProfile} busy={onboardingBusy} onComplete={finishOnboarding} onSkip={skipOnboarding} /> : null}
      <aside className="nav-rail">
        <div className="brand">
          <BriefcaseBusiness />
          <div><strong>JSE</strong><span>Application ATS</span></div>
        </div>
        <button className={view === "dashboard" ? "active nav-btn" : "nav-btn"} onClick={() => setView("dashboard")}><BarChart3 size={18} /> Dashboard</button>
        <button className={view === "campaign" ? "active nav-btn" : "nav-btn"} onClick={() => setView("campaign")}><Target size={18} /> Campaign</button>
        <button className={view === "hiddenMarket" ? "active nav-btn" : "nav-btn"} onClick={() => setView("hiddenMarket")}><Radar size={18} /> Hidden Market</button>
        <button className={view === "pipeline" ? "active nav-btn" : "nav-btn"} onClick={() => setView("pipeline")}><KanbanSquare size={18} /> Pipeline</button>
        <button className={view === "stats" ? "active nav-btn" : "nav-btn"} onClick={() => setView("stats")}><TrendingUp size={18} /> Stats</button>
        <button className={view === "activity" ? "active nav-btn" : "nav-btn"} onClick={() => setView("activity")}><NotebookTabs size={18} /> Activity</button>
        <button className={view === "settings" ? "active nav-btn" : "nav-btn"} onClick={() => setView("settings")}><Settings size={18} /> Settings</button>
        <div className="nav-spacer" />
        <button className="secondary wide" disabled={Boolean(activeTasks.laneSetup)} title={activeTasks.laneSetup ? "Finish the current lane setup first" : "Create a new lane"} onClick={() => setAddLaneOpen(true)}><Plus size={16} /> Add lane</button>
      </aside>

      <section className="ats-main">
        <header className="toolbar">
          <div>
            <h1>{viewTitle}</h1>
            <p>{includeAllProfiles ? "All lanes" : activeProfile?.name || "Lane"} · {status}</p>
          </div>
          <div className="toolbar-actions">
            <button onClick={() => setRunSearchOpen(true)}><Play size={16} /> Run Search</button>
            <button className="secondary" onClick={() => setAddJobOpen(true)}><Plus size={16} /> Add Job</button>
            <button className="secondary" onClick={() => setAnalysisOpen(true)}><Sparkles size={16} /> Run Analysis</button>
            <button className="secondary" onClick={() => refresh()}><RefreshCw size={16} /> Refresh</button>
            <button className="danger" onClick={stopAllTasks}><CircleStop size={16} /> Stop</button>
          </div>
        </header>

        <section className={view === "settings" ? "filter-bar settings-filter-bar" : "filter-bar"}>
          <div className="filter-search-row">
            <label className="profile-filter"><span>Lane</span><select value={activeProfileId} onChange={(event) => setActiveProfileId(Number(event.target.value))}>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>
            {view !== "settings" ? (
              <>
                <label className="search-field"><span>Search</span><input value={filters.query} placeholder="Title, company, notes, analysis, lane..." onChange={(event) => updateFilter("query", event.target.value)} /></label>
                <label className="filter-chip all-profiles"><input type="checkbox" checked={includeAllProfiles} onChange={(event) => setIncludeAllProfiles(event.target.checked)} /> All lanes</label>
              </>
            ) : null}
          </div>
          {view !== "settings" ? (
            <div className="filter-options-row">
              <label className="stage-filter"><span>Stage</span><select value={filters.stage} onChange={(event) => updateFilter("stage", event.target.value)}><option value="">All stages</option>{PIPELINE.map((stage) => <option key={stage.id} value={stage.id}>{stage.label}</option>)}</select></label>
              <label className="source-filter"><span>Source</span><select value={filters.source} onChange={(event) => updateFilter("source", event.target.value)}><option value="">All sources</option>{sources.map((source) => <option key={source} value={source}>{source}</option>)}</select></label>
              <label className="location-filter"><span>Location</span><input value={filters.location} placeholder="Melbourne VIC" onChange={(event) => updateFilter("location", event.target.value)} /></label>
              <label className="score-filter"><span>Min score</span><input type="number" min="0" max="100" value={filters.min_score} placeholder="Any" onChange={(event) => updateFilter("min_score", event.target.value)} /></label>
              <label className="date-filter"><span>Posted since</span><input type="date" value={filters.date_from} onChange={(event) => updateFilter("date_from", event.target.value)} /></label>
              <div className="filter-choice-group" role="group" aria-label="Work mode">
                <span>Work mode</span>
                <div className="filter-choice-options">
                  {WORK_MODES.map((mode) => (
                    <label key={mode.id} className="filter-chip">
                      <input type="checkbox" checked={(filters.work_modes || []).includes(mode.id)} onChange={(event) => toggleFilterMode(mode.id, event.target.checked)} />
                      {mode.label}
                    </label>
                  ))}
                </div>
              </div>
              <div className="filter-choice-group activity-filter" role="group" aria-label="Activity">
                <span>Activity</span>
                <div className="filter-choice-options">
                  <label className="filter-chip"><input type="checkbox" checked={filters.has_interview} onChange={(event) => updateFilter("has_interview", event.target.checked)} /> Interviews</label>
                  <label className="filter-chip"><input type="checkbox" checked={filters.has_feedback} onChange={(event) => updateFilter("has_feedback", event.target.checked)} /> Feedback</label>
                </div>
              </div>
            </div>
          ) : null}
        </section>

        {view === "dashboard" ? <Dashboard dashboard={dashboard} calendar={calendar} onOpenJob={openJob} onOpenCleanup={() => setCleanupOpen(true)} /> : null}

        {view === "campaign" ? (
          <CampaignPanel
            plan={campaignPlan}
            busy={campaignBusy}
            docsBusy={docsBusy}
            onStageAttack={stageCampaignAttackQueue}
            onRefreshActions={refreshCampaignActions}
            onOpenJob={openJob}
            onStageJob={stageJobFromPlan}
            onFollowedUp={markFollowedUp}
            onGenerateDocs={generateDocsForJob}
            onGeneratePack={generateCampaignPack}
          />
        ) : null}

        {view === "hiddenMarket" ? (
          <HiddenMarketPanel
            data={hiddenMarket}
            busy={hiddenMarketBusy}
            days={hiddenMarketDays}
            onDaysChange={setHiddenMarketDays}
            onRefresh={loadHiddenMarket}
            onTrack={trackHiddenTarget}
            onStrategy={hiddenStrategy}
            onLeadUpdate={hiddenLeadUpdate}
            onTouch={hiddenLeadTouch}
            onConvert={hiddenLeadConvert}
            onDeleteLead={hiddenLeadDelete}
            onOpenJob={openJob}
          />
        ) : null}

        {view === "pipeline" ? (
          <section className="kanban-board">
            {PIPELINE.map((stage) => (
              <section key={stage.id} className="kanban-column" onDragOver={(event) => event.preventDefault()} onDrop={(event) => onDropStage(event, stage.id)}>
                <header>
                  <span className="kanban-heading">
                    {stage.label}
                    {stage.id === "interested" ? (
                      <button
                        className="icon secondary subtle-icon"
                        title="Research employer intel"
                        aria-label="Research employer intel for interested jobs"
                        disabled={Boolean(activeTasks.company)}
                        onClick={() => researchStageCompanies(stage.id)}
                      >
                        {activeTasks.company ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}
                      </button>
                    ) : null}
                  </span>
                  <div className="kanban-header-actions">
                    {stage.id === "interested" ? (
                      <select
                        className="kanban-sort"
                        value={interestedSort}
                        aria-label="Sort interested jobs"
                        onChange={(event) => setInterestedSort(event.target.value)}
                      >
                        <option value="match">Match</option>
                        <option value="recent">Most recent</option>
                        <option value="due">Due date</option>
                      </select>
                    ) : null}
                    <strong>{groupedJobs[stage.id]?.length || 0}</strong>
                  </div>
                </header>
                {stage.id === "interested" && !docsBatchProgress ? (
                  <div className="interested-batch-toolbar">
                    <button
                      className="secondary"
                      disabled={docsBusy || !(groupedJobs.interested?.length)}
                      onClick={generateInterestedDocs}
                    >
                      {docsBusy ? <Loader2 className="spin" size={14} /> : <FileText size={14} />}
                      Generate all docs
                      <span>{groupedJobs.interested?.length || 0}</span>
                    </button>
                  </div>
                ) : null}
                {stage.id === "interested" && docsBatchProgress ? (
                  <div className={`docs-batch-progress ${docsBatchProgress.status || ""}`}>
                    <div className="docs-batch-progress-head">
                      <span>{docsBatchProgress.running ? <Loader2 className="spin" size={13} /> : <FileText size={13} />}<strong>Application documents</strong></span>
                      <span>{docsBatchProgress.current || 0}/{docsBatchProgress.total || 0}</span>
                      {!docsBatchProgress.running ? <button className="icon secondary" aria-label="Dismiss document batch progress" onClick={() => setDocsBatchProgress(null)}><X size={12} /></button> : null}
                    </div>
                    <div className="docs-batch-track" role="progressbar" aria-valuemin="0" aria-valuemax={docsBatchProgress.total || 0} aria-valuenow={docsBatchProgress.current || 0}>
                      <span style={{ width: `${docsBatchProgress.total ? Math.round((docsBatchProgress.current / docsBatchProgress.total) * 100) : 0}%` }} />
                    </div>
                    <p>{docsBatchProgress.message}</p>
                    <small>{docsBatchProgress.succeeded || 0} complete{docsBatchProgress.skipped ? ` · ${docsBatchProgress.skipped} closed` : ""}{docsBatchProgress.failed ? ` · ${docsBatchProgress.failed} failed` : ""}</small>
                  </div>
                ) : null}
                <div className="kanban-stack">
                  {(groupedJobs[stage.id] || []).slice(0, KANBAN_COLUMN_RENDER_CAP).map((job) => <JobCard key={job.id} job={job} onOpen={openJob} onDragStart={onDragStart} onReject={setRejectJob} />)}
                  {(groupedJobs[stage.id]?.length || 0) > KANBAN_COLUMN_RENDER_CAP ? (
                    <p className="kanban-overflow">
                      +{groupedJobs[stage.id].length - KANBAN_COLUMN_RENDER_CAP} more not shown — use the filters above to narrow this column.
                    </p>
                  ) : null}
                </div>
              </section>
            ))}
          </section>
        ) : null}

        {view === "stats" ? (
          <StatsPanel stats={stats} period={statsPeriod} onPeriodChange={setStatsPeriod} busy={statsBusy} />
        ) : null}

        {view === "activity" ? (
          <section className="activity-view">
            <div className="section-head"><h2>Activity Log</h2><span>{logs.length} entries</span></div>
            <div className="logs">
              {logs.map((line, index) => <div key={`${line.at}-${index}`}><time>{line.at}</time><span>{line.text}</span></div>)}
            </div>
          </section>
        ) : null}

        {view === "settings" ? (
          <SettingsPanel
            profile={activeProfile}
            settings={settings}
            globalSettings={globalSettings}
            scrapers={scrapers}
            scraperError={scraperError}
            memoryStatus={memoryStatus}
            memoryFragments={memoryFragments}
            memoryBusy={memoryBusy}
            onSave={saveSettings}
            onSaveGlobal={saveGlobalSettings}
            onSaveProfile={saveProfile}
            onApplyFilters={applySettingsToFilters}
            onCompactDatabase={compactDatabase}
            onImportResume={importResume}
            onSearchResumes={searchResumes}
            onScanMemory={scanProfileMemory}
            onImportScraper={importScraper}
            onBuildScraper={buildScraper}
            onTestScraper={testScraper}
            onUpdateScraper={updateScraper}
            onUpdateLaneScraper={updateLaneScraper}
            onRemoveScraper={removeScraper}
          />
        ) : null}
      </section>

      {runSearchOpen ? <RunSearchModal sources={searchSources} activeProfileId={activeProfileId} busy={searchBusy} onClose={() => setRunSearchOpen(false)} onRun={(payload) => { setRunSearchOpen(false); runTask("scrape:run", payload, "Search complete."); }} /> : null}
      {addLaneOpen ? <CreateLaneModal busy={addLaneBusy} onClose={() => setAddLaneOpen(false)} onCreate={createLane} /> : null}
      {addJobOpen ? <AddJobModal busy={addJobBusy} onClose={() => setAddJobOpen(false)} onSave={addManualJob} /> : null}
      {analysisOpen ? <AnalysisModal activeProfileId={activeProfileId} busy={analysisBusy} onClose={() => setAnalysisOpen(false)} onRun={(payload) => { setAnalysisOpen(false); runTask("analysis:run", payload, "Analysis complete."); }} /> : null}
      {quickMove ? <QuickStageForm job={quickMove.job} stage={quickMove.stage} onClose={() => setQuickMove(null)} onSave={saveQuickMove} /> : null}
      {rejectJob ? <RejectJobModal job={rejectJob} onClose={() => setRejectJob(null)} onSave={rejectSelectedJob} /> : null}
      {workspace.job ? (
        <WorkspaceModal
          job={workspace.job}
          events={workspace.events}
          profiles={profiles}
          activeTab={workspace.tab}
          setActiveTab={(tab) => setWorkspace((current) => ({ ...current, tab }))}
          interviews={workspace.interviews}
          onClose={() => setWorkspace({ job: null, events: [], interviews: [], tab: "Details" })}
          onSave={saveWorkspace}
          onApplicationDateApplied={moveToAppliedFromApplicationDate}
          onGenerateDocs={generateDocs}
          onGeneratePrompt={generateApplicationPrompt}
          onCompanyResearch={researchCompany}
          onAddEvent={addWorkspaceEvent}
          onAddInterview={addInterview}
          onUpdateInterview={updateInterview}
          onDocumentDrop={extractDroppedDocument}
          onViewDocument={(title, text) => setDocumentViewer({ title, text })}
          onDownloadDocument={downloadDocument}
          onRevealDocument={revealDocument}
          onAnalyzeJob={() => analyzeJob(workspace.job)}
          onMoveProfile={moveWorkspaceProfile}
          analyzing={analysisBusy}
          generatingDocs={docsBusy}
          researchingCompany={Boolean(activeTasks.company)}
          documentAiName={documentAiLabel(settings)}
          onRejectJob={rejectFromWorkspace}
          onMoveInterested={moveInterestedFromWorkspace}
        />
      ) : null}
      {documentViewer ? <DocumentTextModal title={documentViewer.title} text={documentViewer.text} onClose={() => setDocumentViewer(null)} /> : null}
      {cleanupOpen ? (
        <CleanupModal
          jobs={dashboard?.cleanup_due || []}
          onClose={() => setCleanupOpen(false)}
          onArchive={archiveCleanupJobs}
          onOpenJob={(jobId) => {
            setCleanupOpen(false);
            openJob(jobId);
          }}
        />
      ) : null}
      {dialog ? <DialogModal dialog={dialog} onClose={closeDialog} /> : null}
      {updateToastVisible ? <UpdateToast update={appUpdate} onDismiss={() => setUpdateToastVisible(false)} /> : null}
      <footer className="status-strip">
        <strong>{busy ? runningTaskKeys.join(" + ") : "Idle"}</strong>
        <span>{latestLog || "Ready"}</span>
        <a href={SUPPORT_URL} onClick={openSupportLink} title={SUPPORT_MESSAGE}>☕ ko-fi.com/keljian</a>
      </footer>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);

/** Task modals: add/reject/stage a job, run a search, onboarding. */
import React, { useEffect, useState } from "react";
import { Check, ChevronRight, ExternalLink, FolderOpen, ClipboardCheck, Loader2, Play, Plus, Sparkles, Trash2, X } from "lucide-react";
import jseIcon from "../../assets/jse-icon.png";
import { LOCAL_AI_RUNTIMES, PIPELINE, WORK_MODES } from "../lib/constants";
import { displayFileName, formatDate, toErrorMessage, todayPlus } from "../lib/format";
import { appConfirm } from "../lib/dialogs";
import { ClosingDateSourceBadge, Modal } from "../components/primitives";

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

function LogExternalModal({ busy, onSave, onClose }) {
  const [form, setForm] = useState({
    title: "",
    company: "",
    url: "",
    location: "",
    salary: "",
    application_date: new Date().toISOString().slice(0, 10),
    doc_used: "",
    description: "",
  });
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  return (
    <Modal title="Log external application" onClose={onClose}>
      <div className="modal-copy">
        Record an application you made outside JSE — a careers-page submission, referral, or one a recruiter put you forward for. It enters the pipeline at <strong>Applied</strong> and its outcome feeds Funnel Insights, so interviews that happen off-platform stop being a blind spot.
      </div>
      <div className="form-grid">
        <label className="full"><span>Role title (required)</span><input autoFocus value={form.title} placeholder="Business Analyst" onChange={(event) => update("title", event.target.value)} /></label>
        <label><span>Company</span><input value={form.company} placeholder="Employer or agency" onChange={(event) => update("company", event.target.value)} /></label>
        <label><span>Job URL (optional)</span><input value={form.url} placeholder="https://..." onChange={(event) => update("url", event.target.value)} /></label>
        <label><span>Location</span><input value={form.location} placeholder="Melbourne VIC" onChange={(event) => update("location", event.target.value)} /></label>
        <label><span>Salary / rate</span><input value={form.salary} onChange={(event) => update("salary", event.target.value)} /></label>
        <label><span>Date applied</span><input type="date" value={form.application_date} onChange={(event) => update("application_date", event.target.value)} /></label>
        <label><span>Document used (optional)</span><input value={form.doc_used} placeholder="Resume / cover letter name" onChange={(event) => update("doc_used", event.target.value)} /></label>
        <label className="full"><span>Notes / ad text (optional)</span><textarea value={form.description} placeholder="Anything you know about the role..." onChange={(event) => update("description", event.target.value)} /></label>
      </div>
      <footer className="modal-actions">
        <button className="secondary" onClick={onClose}>Cancel</button>
        <button disabled={busy || !form.title.trim()} onClick={() => onSave(form)}><ClipboardCheck size={16} /> Log application</button>
      </footer>
    </Modal>
  );
}

function RunSearchModal({ sources = [], activeProfileId, busy, onRun, onClose }) {
  const safeSources = Array.isArray(sources) ? sources : [];
  const [selectedSources, setSelectedSources] = useState(safeSources);
  const [includeAllProfiles, setIncludeAllProfiles] = useState(false);
  const [autoRunAnalysis, setAutoRunAnalysis] = useState(false);
  const [optimism, setOptimism] = useState(3);
  const hasSources = safeSources.length > 0;
  const hasLaneScope = includeAllProfiles || Boolean(activeProfileId);

  useEffect(() => {
    setSelectedSources((current) => {
      const valid = current.filter((source) => safeSources.includes(source));
      if (valid.length) return valid;
      return safeSources;
    });
  }, [safeSources]);

  return (
    <Modal title="Run Search" onClose={onClose}>
      <div className="modal-copy">Manual search uses saved terms for each selected lane. If a lane has no terms, they will be generated first.</div>
      <label className="check-row"><input type="checkbox" checked={includeAllProfiles} onChange={(event) => setIncludeAllProfiles(event.target.checked)} /> Run across all lanes</label>
      <label className="check-row"><input type="checkbox" checked={autoRunAnalysis} onChange={(event) => setAutoRunAnalysis(event.target.checked)} /> Auto-run analysis after search</label>
      <label className="field"><span>Optimism for generated terms</span><input type="range" min="1" max="5" value={optimism} onChange={(event) => setOptimism(Number(event.target.value))} /></label>
      <div className="source-grid">
        {hasSources ? safeSources.map((source) => (
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
        <button disabled={busy || !hasLaneScope || !hasSources || selectedSources.length === 0} onClick={() => onRun({ profile_id: activeProfileId, include_all_profiles: includeAllProfiles, sources: selectedSources, optimism, auto_run_analysis: autoRunAnalysis })}><Play size={16} /> Run search</button>
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
        <div className="onboarding-brand"><img className="onboarding-brand-icon" src={jseIcon} alt="" /><strong>JSE setup</strong><span>Step {step + 1} of 3</span></div>
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

export { RejectJobModal, QuickStageForm, AddJobModal, LogExternalModal, RunSearchModal, AnalysisModal, OnboardingWizard, CreateLaneModal, CleanupModal };

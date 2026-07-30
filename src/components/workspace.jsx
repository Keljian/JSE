/** The per-job application workspace. */
import React, { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, ExternalLink, FileText, Loader2, Plus, Sparkles, X } from "lucide-react";
import { PIPELINE, WORKSPACE_TABS } from "../lib/constants";
import { canMoveToInterested, formatDate, normalizeStage, toDateTimeInputValue, toErrorMessage, todayPlus } from "../lib/format";
import { appConfirm, appNotice } from "../lib/dialogs";
import { ClosingDateSourceBadge, DropZone, LinkedText, Modal, ScoreStack } from "../components/primitives";
import { AdSignalsBlock } from "../components/chips";
import { AnalysisReport, BlockerGateBlock, ChannelBlock, CompanyPanel, DocumentTrackBlock } from "../components/panels";

function WorkspaceModal({ job, events, interviews, profiles, activeTab, setActiveTab, onClose, onSave, onApplicationDateApplied, onGenerateDocs, onGeneratePrompt, onCompanyResearch, onAddEvent, onAddInterview, onUpdateInterview, onDocumentDrop, onViewDocument, onDownloadDocument, onRevealDocument, onConvertDocumentPdf, onAnalyzeJob, onMoveProfile, analyzing, generatingDocs, researchingCompany, documentAiName, onRejectJob, onMoveInterested, onSetBlockerVerdict, onSetChannel, onSetDocumentTrack }) {
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
    } catch {
      // The caller logs the failure; here we only roll the selector back.
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
    <Modal title="Application Workspace" onClose={onClose} wide expandable>
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
            <AdSignalsBlock signals={job.ad_signals} />
            <BlockerGateBlock job={job} onSetVerdict={onSetBlockerVerdict} />
            <ChannelBlock job={job} onSetChannel={onSetChannel} />
            <DocumentTrackBlock job={job} onSetTrack={onSetDocumentTrack} />
            <h3>Analysis</h3>
            {analyzing ? (
              <div className="thinking-card">
                <Loader2 className="spin" size={18} />
                <div><strong>Analyzing fit...</strong><span>Running triage and full fit analysis if the role clears the threshold.</span></div>
              </div>
            ) : null}
            <AnalysisReport text={job.ai_analysis} matchScore={job.match_score} />
            <div className="role-source-texts">
              <section className="role-source-text job-ad-text">
                <div className="role-source-heading">
                  <span>Job advertisement</span>
                  <small>Scraped from the listing</small>
                </div>
                <p className="description"><LinkedText text={job.description || "No job advertisement captured."} /></p>
              </section>
              {job.position_description_text ? (
                <section className="role-source-text position-description-text">
                  <div className="role-source-heading">
                    <span>Position description</span>
                    <small>PDF / attached document</small>
                  </div>
                  <p className="description"><LinkedText text={job.position_description_text} /></p>
                </section>
              ) : null}
            </div>
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
          <details className="full candidate-context-field">
            <summary className="candidate-context-heading">
              <span>Additional candidate evidence</span>
              <span className="candidate-context-meta"><small>Optional</small><ChevronRight size={16} /></span>
            </summary>
            <div className="candidate-context-body">
            <textarea
              id="additional-candidate-context"
              aria-label="Additional candidate evidence"
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
            </div>
          </details>
          <div className="full document-grid">
            <DropZone
              label="Cover letter"
              value={form.cover_letter_path}
              text={form.cover_letter_text}
              onDrop={(file) => onDocumentDrop("cover_letter", file)}
              onView={() => onViewDocument("Cover letter text", form.cover_letter_text)}
              onDownload={() => onDownloadDocument(form.cover_letter_path)}
              onReveal={() => onRevealDocument(form.cover_letter_path)}
              onConvertPdf={() => onConvertDocumentPdf(form.cover_letter_path, "cover_letter")}
            />
            <DropZone
              label="Resume"
              value={form.resume_used}
              text={form.resume_text}
              onDrop={(file) => onDocumentDrop("resume", file)}
              onView={() => onViewDocument("Resume text", form.resume_text)}
              onDownload={() => onDownloadDocument(form.resume_used)}
              onReveal={() => onRevealDocument(form.resume_used)}
              onConvertPdf={() => onConvertDocumentPdf(form.resume_used, "resume")}
            />
            <DropZone
              label="Position description"
              value={form.position_description_path}
              text={form.position_description_text}
              onDrop={(file) => onDocumentDrop("position_description", file)}
              onView={() => onViewDocument("Position description text", form.position_description_text)}
              onDownload={() => onDownloadDocument(form.position_description_path)}
              onReveal={() => onRevealDocument(form.position_description_path)}
              onConvertPdf={() => onConvertDocumentPdf(form.position_description_path, "position_description")}
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

export { WorkspaceModal };

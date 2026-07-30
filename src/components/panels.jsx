/** Detail panels: blocker gate, channel, document track, company, analysis. */
import React, { useMemo } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { BLOCKER_CHIPS, CHANNEL_OPTIONS, DOC_TRACK_OPTIONS } from "../lib/constants";
import { actionMeta, blockerVerdictOf, gateDecisionMeta, isWeakCompanyName, parseAnalysisReport, parseJsonObject, scoreClass } from "../lib/format";
import { ValueList } from "../components/primitives";
import { BlockerChip, WarmthChip } from "../components/chips";

function DocumentTrackBlock({ job, onSetTrack }) {
  const resolved = job?.document_track_resolved;
  if (!resolved) return null;
  const stripped = resolved.track === "stripped_back";
  return (
    <section className="doc-track-block">
      <h3>
        Document track
        <span className={`ad-chip ${stripped ? "warn" : "good"}`}>
          {stripped ? "Stripped back" : "Full senior"}
        </span>
      </h3>
      <p className="muted">
        {stripped
          ? "This role reads as below your demonstrated ceiling, so documents are written to the ad's scope and the cover letter answers the level question directly."
          : "Documents lead with ownership, scope and quantified outcomes at your demonstrated level."}
      </p>
      <ul>{(resolved.reasons || []).map((reason, i) => <li key={i}>{reason}</li>)}</ul>
      <label className="field">
        <span>Track {resolved.source === "manual" ? "(set manually)" : "(derived)"}</span>
        <select
          value={resolved.source === "manual" ? resolved.track : ""}
          onChange={(event) => onSetTrack(event.target.value)}
        >
          {DOC_TRACK_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </label>
    </section>
  );
}

function ChannelBlock({ job, onSetChannel }) {
  const contacts = job?.warm_path || [];
  return (
    <section className="channel-block">
      <h3>Application channel <WarmthChip job={job} /></h3>
      <p className="muted">
        {job?.channel_source === "stored"
          ? "Set explicitly for this role."
          : `Derived from the source (${job?.channel_label || "unattributed"}). Set it if you know better.`}
      </p>
      <label className="field">
        <span>Channel</span>
        <select
          value={job?.channel_source === "stored" ? (job?.channel || "") : ""}
          onChange={(event) => onSetChannel(event.target.value)}
        >
          {CHANNEL_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </label>
      {contacts.length ? (
        <>
          <h4>Known contacts at this employer</h4>
          <ul>
            {contacts.map((contact, i) => (
              <li key={i}>
                <strong>{contact.name}</strong>
                {contact.role_title ? ` — ${contact.role_title}` : ""}
                {contact.relationship ? ` (${contact.relationship})` : ""}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

function BlockerGateBlock({ job, onSetVerdict }) {
  const verdict = blockerVerdictOf(job);
  const gate = job?.blocker_gate?.details || {};
  const hardBlockers = Array.isArray(gate.hard_blockers) ? gate.hard_blockers : [];
  const namedGaps = Array.isArray(gate.named_gaps) ? gate.named_gaps : [];
  if (!BLOCKER_CHIPS[verdict]) {
    return (
      <section className="blocker-gate-block">
        <h3>Hard-blocker gate</h3>
        <p className="muted">Not yet checked. The gate runs between triage and full analysis.</p>
        <div className="button-row">
          <button className="secondary" onClick={() => onSetVerdict("skip", "Marked as skip manually.")}>Mark as skip</button>
        </div>
      </section>
    );
  }
  return (
    <section className="blocker-gate-block">
      <h3>Hard-blocker gate <BlockerChip job={job} /></h3>
      <p>{String(job?.blocker_reason || "").trim() || "No reason recorded."}</p>
      {gate.confidence ? <p className="muted">Confidence: {gate.confidence}{gate.downgraded_from ? ` · downgraded from ${gate.downgraded_from}` : ""}</p> : null}
      {hardBlockers.length ? (
        <>
          <h4>Hard blockers</h4>
          <ul>{hardBlockers.map((item, i) => <li key={i}><strong>{item.requirement}</strong>{item.why_unmet ? ` — ${item.why_unmet}` : ""}</li>)}</ul>
        </>
      ) : null}
      {namedGaps.length ? (
        <>
          <h4>Named gaps</h4>
          <ul>{namedGaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul>
        </>
      ) : null}
      <div className="button-row">
        <button className="secondary" onClick={() => onSetVerdict(null, "Cleared from the workspace.")}>Clear verdict</button>
        {verdict === "skip"
          ? <button className="secondary" onClick={() => onSetVerdict("stretch", "Overruled from the workspace.")}>Overrule to stretch</button>
          : <button className="secondary" onClick={() => onSetVerdict("skip", "Marked as skip from the workspace.")}>Mark as skip</button>}
      </div>
    </section>
  );
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

export { DocumentTrackBlock, ChannelBlock, BlockerGateBlock, CompanyPanel, AnalysisBullets, EvidenceMatches, AnalysisReport };

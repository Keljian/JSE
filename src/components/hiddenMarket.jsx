/** Hidden-market intelligence, targets, and lead tracking. */
import React, { useState } from "react";
import { BriefcaseBusiness, Check, ChevronRight, ExternalLink, AlertTriangle, ArrowRightLeft, CalendarClock, Lightbulb, ListTodo, Loader2, Plus, Radar, RefreshCw, Send, Target, Trash2 } from "lucide-react";
import { HM_OUTCOME_LABELS, HM_STATUS_LABELS, HM_TYPE_LABELS } from "../lib/constants";
import { formatDate } from "../lib/format";
import { LinkedText } from "../components/primitives";
import { CampaignSection } from "../components/campaign";

function IntelligenceStrategy({ strategy }) {
  if (!strategy || typeof strategy !== "object" || !Object.keys(strategy).length) return null;
  return (
    <div className="hm-strategy intelligence-strategy">
      {strategy.positioning_angle ? <p><strong>Positioning:</strong> {strategy.positioning_angle}</p> : null}
      <div className="intelligence-strategy-meta">
        {strategy.contact_persona ? <span><strong>Contact:</strong> {strategy.contact_persona}</span> : null}
        {strategy.recommended_channel ? <span><strong>Channel:</strong> {strategy.recommended_channel}</span> : null}
      </div>
      {strategy.opening_message ? <blockquote>{strategy.opening_message}</blockquote> : null}
      {(strategy.evidence_to_reference || []).length ? <p><strong>Reference:</strong> {strategy.evidence_to_reference.join(" · ")}</p> : null}
      {(strategy.questions_to_ask || []).length ? <p><strong>Ask:</strong> {strategy.questions_to_ask.join(" · ")}</p> : null}
      {(strategy.follow_up_sequence || []).length ? <ol>{strategy.follow_up_sequence.map((step, index) => <li key={`${step}-${index}`}>{step}</li>)}</ol> : null}
      {(strategy.cautions || []).length ? <p className="intelligence-caution"><strong>Caution:</strong> {strategy.cautions.join(" · ")}</p> : null}
    </div>
  );
}

function IntelligenceContactResearch({ research, onSelect, busy }) {
  if (!research || !Object.keys(research).length) return null;
  const candidates = research.candidates || [];
  const visibleIds = new Set(research.visible_candidate_ids || []);
  const visible = candidates.filter((candidate) => visibleIds.has(candidate.candidate_id)).slice(0, 3);
  const shown = visible.length ? visible : candidates.slice(0, 3);
  const hidden = candidates.filter((candidate) => !shown.some((item) => item.candidate_id === candidate.candidate_id));
  const renderCandidate = (candidate, selectable = true) => (
    <label key={candidate.candidate_id} className={`${research.selected_candidate_id === candidate.candidate_id ? "selected" : ""} ${research.recommended_candidate_id === candidate.candidate_id ? "recommended" : ""}`}>
      {selectable ? <input type="radio" name={`contact-${research.target_name}`} checked={research.selected_candidate_id === candidate.candidate_id} disabled={busy} onChange={() => onSelect(candidate.candidate_id)} /> : null}
      <span>
        <strong>{candidate.name}{research.recommended_candidate_id === candidate.candidate_id ? <em>Recommended</em> : null}</strong>
        <small>{candidate.role || "Role not independently confirmed"} · {candidate.confidence} confidence ({candidate.confidence_score})</small>
        <small>{[candidate.email, candidate.phone].filter(Boolean).join(" · ") || "No direct details confirmed"}</small>
      </span>
      {candidate.profile_url ? <button type="button" className="link-button" onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(candidate.profile_url); }}><ExternalLink size={13} /> Profile</button> : null}
    </label>
  );
  return (
    <section className={`contact-research ${research.requires_selection ? "needs-selection" : ""}`}>
      <header><strong>Contact research</strong><span>{research.public_results_checked || 0} public result{research.public_results_checked === 1 ? "" : "s"} checked</span></header>
      {(research.conflicts || []).length ? <p className="contact-conflict"><AlertTriangle size={13} /> Independent sources disagree about this contact. Choose the best-supported person.</p> : null}
      {research.requires_selection ? <p className="contact-prompt">Two well-supported contacts are close. Choose who the strategy should address.</p> : null}
      <div className="contact-candidates">{shown.map((candidate) => renderCandidate(candidate))}</div>
      {(hidden.length || research.discarded_labels_count) ? (
        <details className="contact-diagnostics">
          <summary><ChevronRight size={13} /> Extraction diagnostics ({hidden.length} other, {research.discarded_labels_count || 0} noisy label{research.discarded_labels_count === 1 ? "" : "s"} ignored)</summary>
          {hidden.length ? <div className="contact-candidates compact">{hidden.map((candidate) => renderCandidate(candidate, false))}</div> : null}
          {research.discarded_labels_count ? <small>JSE ignored nearby prose that did not reliably identify a person.</small> : null}
        </details>
      ) : null}
      {(research.warnings || []).map((warning) => <small className="contact-warning" key={warning}>{warning}</small>)}
      <small>{research.research_policy}</small>
    </section>
  );
}

function HiddenMarketTarget({ name, meta, detail, titles, chip, tracked, onTrack, onStrategy, strategy, strategyBusy, target, onOpenJob, contactResearch, onSelectContact }) {
  return (
    <article className="hidden-target">
      <div>
        <strong>{name}</strong>
        {target?.opportunity_score !== undefined ? <span className="hm-score">{target.opportunity_score}</span> : null}
        {target?.confidence ? <span className={`hm-chip confidence-${target.confidence}`}>{target.confidence} confidence</span> : null}
        {chip ? <span className={`hm-chip ${chip.tone || ""}`}>{chip.label}</span> : null}
        <span>{meta}</span>
      </div>
      {detail ? <p><LinkedText text={detail} /></p> : null}
      {target?.recommended_action ? <p className="intelligence-next"><strong>Next:</strong> {target.recommended_action}</p> : null}
      {titles?.length ? <small>{titles.join(" · ")}</small> : null}
      {(target?.score_reasons || []).length ? <small className="intelligence-why">Why {target.opportunity_score}: {target.score_reasons.join(" · ")}</small> : null}
      {(target?.evidence || []).length ? (
        <details className="intelligence-evidence">
          <summary><ChevronRight size={13} /> {target.evidence.length} source role{target.evidence.length === 1 ? "" : "s"} and classification evidence</summary>
          <div>
            {(target.classification_reasons || []).map((reason) => <p key={reason}><Check size={12} /> {reason}</p>)}
            {(target.counter_evidence || []).map((reason) => <p key={reason} className="counter"><AlertTriangle size={12} /> {reason}</p>)}
            <ul>{target.evidence.map((item) => <li key={item.job_id}><button className="link-button" onClick={() => onOpenJob(item.job_id)}>{item.title}</button><span>{item.company} · {item.score || 0}% · {formatDate(item.seen)} · {item.source}</span></li>)}</ul>
          </div>
        </details>
      ) : null}
      {(onTrack || onStrategy) ? (
        <div className="hidden-target-actions">
          {onTrack ? (
            <button className="secondary" disabled={tracked} onClick={onTrack}>
              {tracked ? <><Check size={14} /> Tracking</> : <><Plus size={14} /> Track</>}
            </button>
          ) : null}
          {onStrategy ? (
            <button className="secondary" disabled={strategyBusy} onClick={onStrategy}>
              {strategyBusy ? <Loader2 className="spin" size={14} /> : <Lightbulb size={14} />} Build strategy
            </button>
          ) : null}
        </div>
      ) : null}
      <IntelligenceContactResearch research={contactResearch} onSelect={onSelectContact} busy={strategyBusy} />
      <IntelligenceStrategy strategy={strategy} />
    </article>
  );
}

function IntelligenceSignals({ signals, freshness, history, momentum, sourceRoi }) {
  const groups = [
    ["Title families", signals?.title_families], ["Skills in demand", signals?.skills],
    ["Locations", signals?.locations],
    ["Work modes", signals?.work_modes], ["Sources", signals?.sources],
  ];
  return (
    <section className="intelligence-signals">
      <div className="intelligence-freshness">
        <span><strong>{freshness?.jobs_considered || 0}</strong> jobs considered</span>
        <span>Coverage: {freshness?.coverage?.structured_role_data || 0}% structured · {freshness?.coverage?.contact || 0}% contact</span>
        <span>Updated {formatDate(freshness?.as_of)}</span>
      </div>
      <div className="intelligence-signal-grid">
        {groups.map(([title, items]) => (
          <section className="campaign-section" key={title}>
            <header><h2>{title}</h2><strong>{items?.length || 0}</strong></header>
            {(items || []).length ? <div className="signal-list">{items.map((item) => (
              <div key={item.label}><span>{item.label}</span><strong>{item.current}</strong><small className={item.delta > 0 ? "up" : item.delta < 0 ? "down" : ""}>{item.delta > 0 ? "+" : ""}{item.delta}</small></div>
            ))}</div> : <p className="empty-inline">Not enough structured data yet.</p>}
          </section>
        ))}
      </div>
      <div className="intelligence-signal-grid">
        <section className="campaign-section">
          <header><h2>Employer hiring momentum</h2><strong>{momentum?.length || 0}</strong></header>
          {(momentum || []).length ? <div className="signal-list">{momentum.map((item) => (
            <div key={item.employer}><span title={item.employer}>{item.employer}</span><strong>{item.current}</strong><small className={item.delta > 0 ? "up" : item.delta < 0 ? "down" : ""}>{item.delta > 0 ? "+" : ""}{item.delta}</small></div>
          ))}</div> : <p className="empty-inline">No employer running multiple roles yet.</p>}
        </section>
        <section className="campaign-section">
          <header><h2>Source ROI</h2><strong>{sourceRoi?.length || 0}</strong></header>
          {(sourceRoi || []).length ? <div className="signal-list">{sourceRoi.map((item) => (
            <div key={item.source}><span>{item.source}</span><strong title="avg fit score">{item.avg_score}</strong><small title={`${item.high_fit} high-fit of ${item.roles}`}>{item.high_fit_rate}%</small></div>
          ))}</div> : <p className="empty-inline">No source data yet.</p>}
        </section>
      </div>
      {(history || []).length > 1 ? <section className="campaign-section intelligence-history"><header><h2>Saved market snapshots</h2><strong>{history.length}</strong></header><div>{history.map((item) => <span key={item.date}><time>{formatDate(item.date)}</time><strong>{(item.recruiters || 0) + (item.direct_employers || 0) + (item.leadership_gaps || 0)}</strong></span>)}</div></section> : null}
    </section>
  );
}

function IntelligencePerformance({ performance }) {
  const groups = [
    ["By target type", performance?.type_performance],
    ["By outreach channel", performance?.channel_performance],
    ["Opportunity score calibration", performance?.score_calibration],
  ];
  return (
    <section className="intelligence-performance">
      <div className="intelligence-kpis">
        <article><span>Response rate</span><strong>{performance?.funnel?.contacted_plus ? `${performance.response_rate}%` : "—"}</strong></article>
        <article><span>Conversion rate</span><strong>{performance?.funnel?.tracked ? `${performance.conversion_rate}%` : "—"}</strong></article>
        <article><span>Follow-ups due</span><strong>{performance?.coverage?.due_followups || 0}</strong></article>
      </div>
      {groups.map(([title, items]) => (
        <section className="campaign-section" key={title}>
          <header><h2>{title}</h2><strong>{items?.length || 0}</strong></header>
          {(items || []).length ? <div className="performance-table">
            <div className="performance-head"><span>Segment</span><span>Tracked</span><span>Response</span><span>Meeting</span><span>Converted</span></div>
            {items.map((item) => <div key={item.label}><strong>{item.label}</strong><span>{item.tracked}</span><span>{item.response_rate}%</span><span>{item.meetings}</span><span>{item.conversion_rate}%</span></div>)}
          </div> : <p className="empty-inline">Log outreach channels and outcomes to build this comparison.</p>}
        </section>
      ))}
      {(performance?.reads || []).length ? <section className="campaign-section"><header><h2>What the outcomes suggest</h2></header>{performance.reads.map((read) => <p className="settings-hint" key={read}>{read}</p>)}</section> : null}
    </section>
  );
}

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
          <select value={lead.outreach_channel || ""} aria-label="Outreach channel" onChange={(event) => onUpdate(lead.id, { outreach_channel: event.target.value })}>
            <option value="">Channel</option><option value="email">Email</option><option value="linkedin">LinkedIn</option><option value="phone">Phone</option><option value="warm introduction">Warm introduction</option><option value="company site">Company site</option>
          </select>
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
      {lead.opportunity_score ? <small className="intelligence-lead-score">Opportunity {lead.opportunity_score} · {(lead.score_reasons || []).join(" · ")}</small> : null}
      {[lead.contact_person, lead.contact_email, lead.contact_phone].filter(Boolean).length ? (
        <p className="hm-lead-contact"><LinkedText text={[lead.contact_person, lead.contact_email, lead.contact_phone].filter(Boolean).join(" · ")} /></p>
      ) : null}

      <IntelligenceStrategy strategy={lead.strategy} />

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

function ManualWarmLead({ onCreate }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ target_name: "", action: "", contact_person: "", contact_email: "", domain: "" });

  const set = (key) => (event) => setForm((current) => ({ ...current, [key]: event.target.value }));

  const submit = async () => {
    if (!form.target_name.trim()) return;
    setBusy(true);
    try {
      await onCreate(form);
      setForm({ target_name: "", action: "", contact_person: "", contact_email: "", domain: "" });
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="secondary hm-add-target" onClick={() => setOpen(true)}>
        <Plus size={15} /> Add a target employer
      </button>
    );
  }

  return (
    <div className="hm-manual-lead">
      <p className="hint">
        An employer you want to approach directly — no advertised role required. Contested board
        applications are where a more directly matched candidate wins; this channel avoids that comparison.
      </p>
      <div className="hm-manual-grid">
        <label><span>Employer *</span>
          <input type="text" value={form.target_name} onChange={set("target_name")} placeholder="Organisation name" autoFocus />
        </label>
        <label><span>Approach</span>
          <input type="text" value={form.action} onChange={set("action")} placeholder="What is the opening move?" />
        </label>
        <label><span>Contact</span>
          <input type="text" value={form.contact_person} onChange={set("contact_person")} placeholder="Name (optional)" />
        </label>
        <label><span>Contact email</span>
          <input type="text" value={form.contact_email} onChange={set("contact_email")} placeholder="Optional" />
        </label>
        <label><span>Domain</span>
          <input type="text" value={form.domain} onChange={set("domain")} placeholder="Optional" />
        </label>
      </div>
      <div className="hm-manual-actions">
        <button disabled={busy || !form.target_name.trim()} onClick={submit}>
          {busy ? <Loader2 className="spin" size={15} /> : <Plus size={15} />} Create lead
        </button>
        <button className="link-button" onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </div>
  );
}

function HiddenMarketPanel({ data, busy, days, onDaysChange, onRefresh, onTrack, onAddTarget, onStrategy, onContactSelect, onLeadUpdate, onTouch, onConvert, onDeleteLead, onOpenJob }) {
  const [section, setSection] = useState("signals");
  const [strategies, setStrategies] = useState({});
  const [contactResearch, setContactResearch] = useState({});
  const [strategyBusy, setStrategyBusy] = useState("");
  const intel = data?.intel || {};
  const overview = data?.overview || {};
  const leads = data?.leads || [];
  const performance = data?.performance || {};
  const counts = overview.status_counts || {};

  const runStrategy = async (target) => {
    const key = target.target_key || target.name;
    setStrategyBusy(key);
    try {
      const result = await onStrategy(target);
      if (result?.contact_research) setContactResearch((current) => ({ ...current, [key]: result.contact_research }));
      if (result?.strategy) setStrategies((current) => ({ ...current, [key]: result.strategy }));
    } finally { setStrategyBusy(""); }
  };

  const selectContact = async (target, candidateId) => {
    const key = target.target_key || target.name;
    setStrategyBusy(key);
    try {
      const research = await onContactSelect(target, candidateId);
      if (research) setContactResearch((current) => ({ ...current, [key]: research }));
      const result = await onStrategy(target);
      if (result?.contact_research) setContactResearch((current) => ({ ...current, [key]: result.contact_research }));
      if (result?.strategy) setStrategies((current) => ({ ...current, [key]: result.strategy }));
    } finally { setStrategyBusy(""); }
  };

  const renderTarget = (item, extra) => (
    <HiddenMarketTarget
      key={item.target_key || item.name}
      name={item.name}
      tracked={item.tracked}
      onTrack={() => onTrack(item)}
      onStrategy={() => runStrategy(item)}
      strategy={strategies[item.target_key || item.name] || item.saved_strategy}
      strategyBusy={strategyBusy === (item.target_key || item.name)}
      target={item}
      onOpenJob={onOpenJob}
      contactResearch={contactResearch[item.target_key || item.name] || item.contact_research}
      onSelectContact={(candidateId) => selectContact(item, candidateId)}
      {...extra}
    />
  );

  return (
    <section className="campaign-view hidden-market-view">
      <div className="campaign-hero">
        <div className="plan-hero-main">
          <h2><Radar size={20} /> Intelligence</h2>
          <p>Evidence-backed market signals, ranked targets, outreach work and outcome learning from every advert seen — including the reject pile.</p>
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

      <nav className="intelligence-tabs" aria-label="Intelligence views">
        <button className={section === "signals" ? "active" : ""} onClick={() => setSection("signals")}>Market Signals</button>
        <button className={section === "targets" ? "active" : ""} onClick={() => setSection("targets")}>Targets</button>
        <button className={section === "outreach" ? "active" : ""} onClick={() => setSection("outreach")}>Outreach <span>{overview.open_total || 0}</span></button>
        <button className={section === "outcomes" ? "active" : ""} onClick={() => setSection("outcomes")}>Outcomes</button>
      </nav>

      {section === "signals" ? <IntelligenceSignals signals={intel.signals} freshness={intel.freshness} history={intel.snapshot_history} momentum={intel.employer_momentum} sourceRoi={intel.source_roi} /> : null}

      {section === "outreach" ? <section className="campaign-section hm-todo">
        <header>
          <h2><ListTodo size={18} /> Outreach To-Do</h2>
          <strong>{leads.length}</strong>
        </header>
        {/* Every other path into this tracker starts from a target mined out of
            advert data, which is why it stayed empty: the employers worth a warm
            approach are precisely the ones not currently advertising. */}
        <ManualWarmLead onCreate={onAddTarget} />
        {!leads.length ? (
          <p className="empty-inline">No outreach leads yet. Add a target employer above, or track a recruiter or leadership-gap target below.</p>
        ) : (
          <div className="hm-lead-list">
            {leads.map((lead) => (
              <HiddenMarketLeadCard key={lead.id} lead={lead} onUpdate={onLeadUpdate} onTouch={onTouch} onConvert={onConvert} onDelete={onDeleteLead} onOpenJob={onOpenJob} />
            ))}
          </div>
        )}
      </section> : null}

      {section === "targets" ? <><header className="hm-head">
        <span>Mined from the last {overview.window_days || days} days. Identities are cross-checked against contact domains and ad language. Expand the evidence before acting; Build strategy asks the local model for a saved outreach approach.</span>
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
      </div></> : null}
      {section === "outcomes" ? <IntelligencePerformance performance={performance} /> : null}
    </section>
  );
}

export { IntelligenceStrategy, IntelligenceContactResearch, HiddenMarketTarget, IntelligenceSignals, IntelligencePerformance, HiddenMarketLeadCard, ManualWarmLead, HiddenMarketPanel };

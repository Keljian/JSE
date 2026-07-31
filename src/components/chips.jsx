/** Job card and the compact signal chips it renders. */
import React from "react";
import { APPLY_CHANNEL_LABELS, JOB_FLAG_CHIPS, WARMTH_CHIPS } from "../lib/constants";
import { formatDate, jobFlagTypesOf, jobFlagsOf } from "../lib/format";
import { ScoreStack } from "../components/primitives";

function JobFlagChips({ job }) {
  const flags = jobFlagsOf(job);
  const types = jobFlagTypesOf(job);
  if (!types.length) return null;
  const detailFor = (type) => flags
    .filter((flag) => flag.type === type)
    .map((flag) => `${flag.requirement}${flag.detail ? ` — ${flag.detail}` : ""}`)
    .join("\n");
  return (
    <>
      {types.map((type) => {
        const entry = JOB_FLAG_CHIPS[type];
        if (!entry) return null;
        const [tone, label] = entry;
        return <span key={type} className={`ad-chip ${tone}`} title={detailFor(type) || label}>{label}</span>;
      })}
    </>
  );
}

function WarmthChip({ job }) {
  const entry = WARMTH_CHIPS[Number(job?.warmth) || 0];
  if (!entry) return null;
  const [tone, label] = entry;
  const names = (job?.warm_path || []).map((contact) => contact.name).filter(Boolean);
  const title = names.length
    ? `Possible warm path: ${names.join(", ")}`
    : `${job?.channel_label || "Warm"} channel`;
  return <span className={`ad-chip ${tone}`} title={title}>{label}</span>;
}

function WarmPathHint({ job }) {
  const contacts = job?.warm_path || [];
  if (!contacts.length) return null;
  const described = contacts
    .map((contact) => (contact.role_title ? `${contact.name} (${contact.role_title})` : contact.name))
    .filter(Boolean);
  if (!described.length) return null;
  return <div className="warm-path-hint">Possible warm path: {described.join(" · ")}</div>;
}

function AdSignalChips({ signals }) {
  if (!signals) return null;
  const chips = [];
  if (signals.urgency === "closing_soon") chips.push(["warn", `Closes ${signals.closes_in_days}d`]);
  else if (signals.urgency === "fresh") chips.push(["good", "Fresh"]);
  else if (signals.urgency === "stale") chips.push(["muted", `${signals.age_days}d old`]);
  if (signals.is_recurring) chips.push(["warn", `Repost ×${signals.recurrence_count}`]);
  if (signals.friction?.length) chips.push(["warn", `${signals.friction.length} hurdle${signals.friction.length > 1 ? "s" : ""}`, signals.friction.join(", ")]);
  if (signals.apply_channel === "recruiter") chips.push(["muted", "Recruiter"]);
  else if (signals.apply_channel === "ats") chips.push(["muted", "ATS"]);
  if (!signals.salary_disclosed) chips.push(["muted", "No $"]);
  if (!chips.length) return null;
  return (
    <div className="card-signals">
      {chips.map(([tone, label, title], i) => (
        <span key={i} className={`ad-chip ${tone}`} title={title || label}>{label}</span>
      ))}
    </div>
  );
}

function AdSignalsBlock({ signals }) {
  if (!signals) return null;
  const rows = [];
  if (signals.hiring_trigger && signals.hiring_trigger !== "unknown") rows.push(["Hiring trigger", signals.hiring_trigger]);
  if (signals.reporting_line) rows.push(["Reports to", signals.reporting_line]);
  if (signals.team_size != null) rows.push(["Team size", String(signals.team_size)]);
  rows.push(["Apply via", APPLY_CHANNEL_LABELS[signals.apply_channel] || "Unknown"]);
  rows.push(["Salary disclosed", signals.salary_disclosed ? "Yes" : "No"]);
  if (signals.is_recurring) rows.push(["Recurrence", `Seen ${signals.recurrence_count}× (likely repost)`]);
  if (signals.age_days != null) rows.push(["Vacancy age", `${signals.age_days} day${signals.age_days === 1 ? "" : "s"}`]);
  if (signals.closes_in_days != null) rows.push(["Closes in", `${signals.closes_in_days} day${signals.closes_in_days === 1 ? "" : "s"}`]);
  return (
    <section className="ad-signals-block">
      <h3>Ad signals</h3>
      <AdSignalChips signals={signals} />
      <dl className="ad-signals-grid">
        {rows.map(([k, v]) => (<div key={k}><dt>{k}</dt><dd>{v}</dd></div>))}
      </dl>
      {signals.friction?.length ? <p className="ad-signals-friction"><strong>Application hurdles:</strong> {signals.friction.join(", ")}</p> : null}
      {signals.ats_keywords?.length ? <p className="ad-signals-ats"><strong>ATS keywords:</strong> {signals.ats_keywords.join(" · ")}</p> : null}
    </section>
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
        <JobFlagChips job={job} />
        <WarmthChip job={job} />
      </div>
      <p>{job.company || "Unknown company"}</p>
      <small>{job.profile_name || "Lane"} · {job.source || "Unknown source"}</small>
      <WarmPathHint job={job} />
      <AdSignalChips signals={job.ad_signals} />
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

export { JobFlagChips, WarmthChip, WarmPathHint, AdSignalChips, AdSignalsBlock, JobCard };

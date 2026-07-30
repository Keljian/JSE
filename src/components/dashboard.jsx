/** Dashboard cards, nudges, and the about/update surfaces. */
import React, { useCallback, useEffect, useState } from "react";
import { CalendarDays, Check, ChevronRight, Coffee, Crosshair, Download, ExternalLink, Gauge, GraduationCap, Lightbulb, Loader2, NotebookTabs, Radar, RefreshCw, Sparkles, Trash2, X } from "lucide-react";
import { NEAR_MISS_RESOLUTIONS, PIPELINE, RELEASES_URL, SUPPORT_URL } from "../lib/constants";
import { formatDate, primaryScore, toErrorMessage } from "../lib/format";
import { ScoreStack } from "../components/primitives";
import aboutArtwork from "../assets/jse-about.png";

function Dashboard({ dashboard, calendar, invoke, onOpenJob, onOpenCleanup, dismissedNudges, onResolveNudge, onOpenHiddenMarket }) {
  const stageCounts = dashboard?.stage_counts || {};
  const cleanupCount = (dashboard?.cleanup_due || []).length;
  const nudges = (dashboard?.interview_nudges || []).filter((nudge) => !dismissedNudges?.has(nudge.interview_id));
  const warm = dashboard?.warm_channel;
  const mix = dashboard?.channel_mix;
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

      {nudges.map((nudge) => (
        <InterviewNudge key={nudge.interview_id} nudge={nudge} onOpenJob={onOpenJob} onResolve={onResolveNudge} />
      ))}

      {warm?.idle ? (
        <div className="warm-nudge">
          <div className="warm-nudge-copy">
            <strong>No warm-channel activity in the last {warm.window_days} days</strong>
            <span>
              Board applications are the channel where a more directly matched candidate wins the
              comparison. {warm.open_leads ? `${warm.open_leads} open lead${warm.open_leads === 1 ? "" : "s"} waiting.` : "No leads tracked yet."}
            </span>
          </div>
          <button onClick={onOpenHiddenMarket}><Radar size={15} /> Open Hidden Market</button>
        </div>
      ) : null}

      {mix?.skewed_cold ? (
        <div className="warm-nudge">
          <div className="warm-nudge-copy">
            <strong>
              {Math.round((mix.cold_share || 0) * 100)}% of the last {mix.applications} application
              {mix.applications === 1 ? "" : "s"} went out cold
            </strong>
            <span>
              {mix.untapped_count
                ? `${mix.untapped_count} live role${mix.untapped_count === 1 ? "" : "s"} already have a contact behind them: ${mix.untapped_warm_paths.map((item) => `${item.company} (${item.contacts.join(", ")})`).join(" · ")}.`
                : "No live role currently has a known contact behind it — worth researching one before the next batch."}
            </span>
          </div>
          {mix.untapped_warm_paths?.length ? (
            <button onClick={() => onOpenJob({ id: mix.untapped_warm_paths[0].id })}>
              <ChevronRight size={15} /> Open {mix.untapped_warm_paths[0].company}
            </button>
          ) : (
            <button onClick={onOpenHiddenMarket}><Radar size={15} /> Open Hidden Market</button>
          )}
        </div>
      ) : null}

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

        <FunnelInsightsCard invoke={invoke} />

        {/* Above the scraper/volume statistics on purpose: discovery is not the
            constraint (16,157 jobs scraped against 156 applications), allocation
            is. This card is what makes misallocation visible at the point of
            decision. */}
        <TargetingCard invoke={invoke} />

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

function InterviewNudge({ nudge, onOpenJob, onResolve }) {
  const [detail, setDetail] = useState(null);
  const [lossReason, setLossReason] = useState("");

  // "Unsuccessful" alone is the answer that loses information, so it is the one
  // that asks a follow-up: how far did it go, and what ended it?
  const needsDetail = detail === "declined" || detail === "final_round" || detail === "runner_up";

  return (
    <div className="interview-nudge">
      <div className="interview-nudge-copy">
        <strong>How far did the {nudge.job_title} interview go?</strong>
        <span>{[nudge.company, nudge.interview_title || `Round ${nudge.round_number}`, formatDate(nudge.interview_date)].filter(Boolean).join(" · ")}</span>
      </div>
      {needsDetail ? (
        <div className="interview-nudge-detail">
          <input
            type="text"
            value={lossReason}
            placeholder="What ended it? (e.g. competitor had direct sector experience)"
            onChange={(event) => setLossReason(event.target.value)}
          />
          <button onClick={() => onResolve(nudge, detail, { loss_reason: lossReason, stage: nudge.round_number })}>Save</button>
          <button className="link-button" onClick={() => { setDetail(null); setLossReason(""); }}>Cancel</button>
        </div>
      ) : (
        <div className="interview-nudge-actions">
          {NEAR_MISS_RESOLUTIONS.map((option) => (
            <button
              key={option.id}
              className={option.id === "offer" ? "" : "secondary"}
              onClick={() => (option.id === "offer"
                ? onResolve(nudge, "offer", { stage: nudge.round_number })
                : setDetail(option.id))}
            >
              {option.label}
            </button>
          ))}
          <button className="secondary" onClick={() => onOpenJob(nudge.job_id)}>Open</button>
          <button className="link-button" onClick={() => onResolve(nudge, "waiting")} aria-label="Dismiss"><X size={15} /></button>
        </div>
      )}
    </div>
  );
}

function TargetingCard({ invoke }) {
  const [summary, setSummary] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const data = await invoke("targeting:summary", { days: 90 });
      setSummary(data.summary);
    } catch (err) {
      setError(toErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [invoke]);

  useEffect(() => { load(); }, [load]);

  const pct = (rate) => `${Math.round((rate || 0) * 100)}%`;
  const total = summary?.total_applications || 0;

  return (
    <section className="dash-section targeting-card">
      <h2><Crosshair size={18} /> Targeting</h2>
      {error ? <p className="empty-inline">Could not load targeting: {error}</p> : null}
      {!summary && !error ? <p className="empty-inline">{busy ? "Computing…" : "No data yet."}</p> : null}
      {summary ? (
        <>
          <div className="targeting-headline">
            <div>
              <strong>{total}</strong>
              <span>applications · last {summary.window_days} days</span>
            </div>
            <div className={summary.below_baseline_share > 0.4 ? "bad" : ""}>
              <strong>{pct(summary.below_baseline_share)}</strong>
              <span>in below-baseline bands</span>
            </div>
            <div className={summary.warm_channel_applications ? "good" : "bad"}>
              <strong>{summary.warm_channel_applications}</strong>
              <span>via warm channel</span>
            </div>
          </div>

          {total ? (
            <>
              <h3>By seniority band</h3>
              <div className="targeting-rows">
                {(summary.by_band || []).map((row) => (
                  <div key={row.value} className={`targeting-row ${row.below_baseline ? "bad" : "good"}`}>
                    <span title={row.label}>{row.label}</span>
                    <small>{row.applications} app{row.applications === 1 ? "" : "s"} · {pct(row.rate)} interview</small>
                  </div>
                ))}
              </div>
              <h3>By channel</h3>
              <div className="targeting-rows">
                {(summary.by_channel || []).map((row) => (
                  <div key={row.value} className={`targeting-row ${row.below_baseline ? "bad" : "good"}`}>
                    <span title={row.label}>{row.label}</span>
                    <small>{row.applications} app{row.applications === 1 ? "" : "s"} · {pct(row.rate)} interview</small>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="empty-inline">No applications in the last {summary.window_days} days.</p>
          )}

          <div className="funnel-footer">
            <small>Baseline {pct(summary.baseline_rate)} across all history</small>
            <button className="link-button" disabled={busy} onClick={load}><RefreshCw size={13} /> Refresh</button>
          </div>
        </>
      ) : null}
    </section>
  );
}

function FunnelInsightsCard({ invoke }) {
  const [insights, setInsights] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async (recompute = false) => {
    setBusy(true);
    setError(null);
    try {
      const data = await invoke("funnel:insights", recompute ? { recompute: true } : {});
      setInsights(data.insights);
    } catch (err) {
      setError(toErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [invoke]);

  useEffect(() => { load(false); }, [load]);

  const pct = (rate) => `${Math.round((rate || 0) * 100)}%`;
  const segLabel = (seg) => `${seg.dimension_label}: ${seg.value}`;
  const total = insights?.total_applications || 0;

  return (
    <section className="dash-section funnel-insights">
      <h2><Gauge size={18} /> Funnel Insights</h2>
      {error ? <p className="empty-inline">Could not load insights: {error}</p> : null}
      {!insights && !error ? <p className="empty-inline">{busy ? "Computing…" : "No data yet."}</p> : null}
      {insights ? (
        <>
          <div className="funnel-baseline">
            <strong>{pct(insights.baseline_rate)}</strong>
            <span>interview rate · {insights.total_interviews}/{total} role{total === 1 ? "" : "s"}</span>
          </div>
          {/* The two rates have different causes: getting interviews is an
              allocation problem, converting them is a competition problem. */}
          {insights.total_interviews ? (
            <div className="funnel-baseline secondary-rate">
              <strong>{pct(insights.final_round_rate)}</strong>
              <span>interview → final round · {insights.total_final_rounds}/{insights.total_interviews}</span>
            </div>
          ) : null}
          {insights.excluded_unresolved ? (
            <p className="empty-inline">
              {insights.excluded_unresolved} application{insights.excluded_unresolved === 1 ? "" : "s"} excluded
              from segment breakdowns — the job record was deleted and nothing was recoverable.
            </p>
          ) : null}
          {total < insights.min_segment_applications ? (
            <p className="empty-inline">Log more applications to unlock segment breakdowns (min {insights.min_segment_applications} per segment).</p>
          ) : (
            <>
              {(insights.top_segments || []).length ? (
                <div className="funnel-segments">
                  <h3>Converting best</h3>
                  {insights.top_segments.slice(0, 4).map((seg) => (
                    <div key={`top-${seg.dimension}-${seg.value}`} className="funnel-segment good">
                      <span title={segLabel(seg)}>{seg.value}</span>
                      <small>{pct(seg.rate)} · n={seg.applications}</small>
                    </div>
                  ))}
                </div>
              ) : null}
              {(insights.worst_segments || []).length ? (
                <div className="funnel-segments">
                  <h3>Converting worst</h3>
                  {insights.worst_segments.slice(0, 4).map((seg) => (
                    <div key={`bad-${seg.dimension}-${seg.value}`} className="funnel-segment bad">
                      <span title={segLabel(seg)}>{seg.value}</span>
                      <small>{pct(seg.rate)} · n={seg.applications}</small>
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          )}
          <div className="funnel-footer">
            <small>Updated {formatDate(insights.generated_at)}</small>
            <button className="link-button" disabled={busy} onClick={() => load(true)}><RefreshCw size={13} /> Recompute</button>
          </div>
        </>
      ) : null}
    </section>
  );
}

function InterviewLearningsPanel({ invoke, runTask, activeTasks, profileId, includeAllProfiles, onOpenJob }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const mining = Boolean(activeTasks["learnings"]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await invoke("funnel:interviewLearnings", {
        profile_id: profileId,
        include_all_profiles: includeAllProfiles,
      });
      setData(result);
    } catch (err) {
      setData({ interviewed_jobs: [], fragments: [], error: toErrorMessage(err) });
    } finally {
      setLoading(false);
    }
  }, [invoke, profileId, includeAllProfiles]);

  useEffect(() => { load(); }, [load]);

  const jobs = data?.interviewed_jobs || [];
  const fragments = data?.fragments || [];
  const unmined = jobs.filter((job) => !job.mined);

  const mine = (jobId) => runTask("funnel:mineInterviewFragments", { job_id: jobId }, "Interview-validated mining complete.", null, load);

  const mineAll = () => {
    const pending = unmined.map((job) => job.id);
    const step = (index) => {
      if (index >= pending.length) { load(); return; }
      runTask("funnel:mineInterviewFragments", { job_id: pending[index] }, "Interview-validated mining complete.", null, () => step(index + 1));
    };
    if (pending.length) step(0);
  };

  const outcomeTone = (score) => {
    const value = Number(score || 0);
    if (value > 0) return "good";
    if (value < 0) return "bad";
    return "";
  };

  return (
    <section className="learnings-view">
      <div className="section-head">
        <div>
          <h2><GraduationCap size={18} /> Interview Learnings</h2>
          <p className="settings-hint">Evidence mined from jobs that actually reached an interview — your strongest signal. These fragments are weighted above ordinary resume material in scoring and document generation. Mining runs automatically when a job first reaches an interview; you can also (re)run it here.</p>
        </div>
        <div className="learnings-actions">
          <button className="secondary" disabled={loading || mining} onClick={load}>
            {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />} Refresh
          </button>
          {unmined.length ? (
            <button disabled={mining} onClick={mineAll}>
              {mining ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />} Mine {unmined.length} un-mined
            </button>
          ) : null}
        </div>
      </div>

      {data?.error ? <p className="empty-inline">Could not load learnings: {data.error}</p> : null}

      <div className="learnings-metrics">
        <article><span>Interviewed roles</span><strong>{jobs.length}</strong><small>{unmined.length} not yet mined</small></article>
        <article><span>Validated fragments</span><strong>{fragments.length}</strong><small>Weighted above submitted evidence</small></article>
        <article><span>Interview rounds</span><strong>{jobs.reduce((sum, job) => sum + Number(job.interview_rounds || 0), 0)}</strong><small>Across all interviewed roles</small></article>
      </div>

      <section className="learnings-section">
        <h3>Interviewed roles</h3>
        {jobs.length ? (
          <div className="learnings-jobs">
            {jobs.map((job) => (
              <article key={job.id} className={`learnings-job ${job.mined ? "mined" : ""}`}>
                <div className="learnings-job-main">
                  <button className="link-button" onClick={() => onOpenJob(job.id)}>{job.title || "Untitled role"}</button>
                  <span>{[job.company, job.profile_name, `${job.interview_rounds} round${job.interview_rounds === 1 ? "" : "s"}`, formatDate(job.latest_interview_date)].filter(Boolean).join(" · ")}</span>
                </div>
                <div className="learnings-job-actions">
                  {job.mined ? <span className="learnings-badge mined"><Check size={13} /> Mined</span> : <span className="learnings-badge">Not mined</span>}
                  <button className="secondary" disabled={mining} onClick={() => mine(job.id)}>
                    {mining ? <Loader2 className="spin" size={14} /> : <Sparkles size={14} />} {job.mined ? "Re-mine" : "Mine"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="empty-inline">No interviews recorded yet. Once a job reaches an interview, it appears here for mining.</p>
        )}
      </section>

      <section className="learnings-section">
        <h3>Interview-validated fragments</h3>
        {fragments.length ? (
          <div className="learnings-fragments">
            {fragments.map((fragment) => (
              <article key={fragment.id} className="learnings-fragment">
                <header>
                  <strong>{fragment.theme || "Untitled fragment"}</strong>
                  <div className="learnings-fragment-tags">
                    <span>{fragment.fragment_type || "evidence"}</span>
                    <span>{fragment.confidence || "medium"} confidence</span>
                    {Number(fragment.support_count || 0) > 1 ? <span>seen ×{fragment.support_count}</span> : null}
                    {Number(fragment.outcome_score) ? <span className={`learnings-outcome ${outcomeTone(fragment.outcome_score)}`}>outcome {Number(fragment.outcome_score) > 0 ? "+" : ""}{Number(fragment.outcome_score).toFixed(1)}</span> : null}
                  </div>
                </header>
                <p>{fragment.claim || fragment.supporting_detail || "No claim captured."}</p>
                {fragment.reuse_guidance ? <p className="learnings-guidance"><Lightbulb size={13} /> {fragment.reuse_guidance}</p> : null}
                {(fragment.keywords || []).length ? <small>Activates on: {fragment.keywords.slice(0, 6).join(", ")}</small> : null}
                {(fragment.source_job_ids || []).length ? <small className="learnings-source">From interviewed job{fragment.source_job_ids.length === 1 ? "" : "s"}: {fragment.source_job_ids.slice(0, 4).map((id) => (
                  <button key={id} className="link-button" onClick={() => onOpenJob(id)}>#{id}</button>
                ))}</small> : null}
              </article>
            ))}
          </div>
        ) : (
          <p className="empty-inline">No interview-validated fragments yet. Mine an interviewed role above to extract the evidence that helped it convert.</p>
        )}
      </section>
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

function AboutPanel({ version, update, onCheckForUpdates }) {
  const updateStatus = update?.status || "idle";
  const checking = updateStatus === "checking";
  const statusMessage = {
    idle: "JSE checks for new releases automatically, or you can check now.",
    checking: "Checking the JSE release channel…",
    current: `You’re up to date with JSE ${update?.version || version}.`,
    development: update?.message || "Update checks are available in installed builds of JSE.",
    available: `JSE ${update?.version || "a newer version"} is available.`,
    downloading: `Downloading the update — ${update?.percent || 0}% complete.`,
    ready: `JSE ${update?.version || "the update"} is ready to install.`,
    error: update?.message || "JSE could not check for updates."
  }[updateStatus] || "JSE checks for new releases automatically, or you can check now.";

  return (
    <section className="about-view" aria-labelledby="about-title">
      <div className="about-hero">
        <img src={aboutArtwork} alt="Developer working at a laptop, surrounded by code and cloud symbols" />
        <div className="about-story">
          <span className="about-kicker">Open source · Local first</span>
          <h2 id="about-title">A calmer command centre for the job hunt.</h2>
          <p>JSE began with a simple observation: finding work is work. The useful information is usually scattered across job boards, browser tabs, documents, notes, calendars, and half-remembered conversations.</p>
          <p>JSE brings those moving parts into one private desktop workspace. It helps you discover roles, judge fit, research employers, manage the application pipeline, and prepare tailored documents, while keeping your evidence and decisions close at hand.</p>
          <p>The aim is not to take the human out of the process. It is to reduce the clerical drag, preserve the story of your experience, and give you more room for the judgement and care that good applications deserve.</p>
          <div className="about-links">
            <button onClick={() => window.jobAssistant.openExternal(RELEASES_URL)}><ExternalLink size={16} /> Project releases</button>
            <button className="secondary" onClick={() => window.jobAssistant.openExternal(SUPPORT_URL)}><Coffee size={16} /> Support on Ko-fi</button>
          </div>
        </div>
      </div>

      <article className="about-update-card">
        <div>
          <span className="about-version-label">Installed version</span>
          <strong>JSE {version || "—"}</strong>
          <p className={`about-update-status status-${updateStatus}`} aria-live="polite">{statusMessage}</p>
        </div>
        <div className="about-update-actions">
          {updateStatus === "available" ? <button onClick={() => window.jobAssistant.downloadUpdate()}><Download size={16} /> Download update</button> : null}
          {updateStatus === "ready" ? <button onClick={() => window.jobAssistant.installUpdate()}><RefreshCw size={16} /> Restart & install</button> : null}
          <button className="secondary" disabled={checking || updateStatus === "downloading"} onClick={onCheckForUpdates}>
            <RefreshCw className={checking ? "spin" : ""} size={16} /> {checking ? "Checking…" : "Check for updates"}
          </button>
        </div>
      </article>
    </section>
  );
}

export { Dashboard, InterviewNudge, TargetingCard, FunnelInsightsCard, InterviewLearningsPanel, UpdateToast, AboutPanel };

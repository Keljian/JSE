/** Campaign plan, stats, memory, and evidence panels. */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { BarChart3, Check, FileText, Loader2, NotebookTabs, Radar, RefreshCw, Send, Sparkles, Target, TrendingUp, Trash2, Wrench } from "lucide-react";
import { CORPUS_DOC_TYPES, PLAN_KIND_META } from "../lib/constants";
import { countBy, formatDate } from "../lib/format";
import { appConfirm } from "../lib/dialogs";
import { StatBars, StatDelta } from "../components/primitives";

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
          <button data-tooltip="Stage the highest-fit roles for action" aria-description="Stage the highest-fit roles for action" disabled={busy} onClick={onStageAttack}><Target size={16} /> Stage Top Roles</button>
          <button className="secondary" data-tooltip="Rebuild follow-up actions for applied jobs" aria-description="Rebuild follow-up actions for applied jobs" disabled={busy} onClick={onRefreshActions}><Send size={16} /> Refresh Follow-Ups</button>
        </div>
      </div>

      <section className="plan-list">
        {!plan ? <p className="empty-inline">{busy ? "Building today's plan..." : "Loading today's plan..."}</p> : null}
        {plan && !(plan.plan || []).length ? (
          <p className="empty-inline">Nothing urgent on the board. Run a search to feed the queue, or open Intelligence for market signals and outreach targets.</p>
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
                <h2><Radar size={18} /> Intelligence Outreach</h2>
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

export { CampaignSection, PlanItem, CampaignPanel, StatsPanel, MemoryPanel, EvidenceLibraryPanel };

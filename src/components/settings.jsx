/** Settings and the scraper plugin builder. */
import React, { useEffect, useState } from "react";
import { BriefcaseBusiness, Check, ChevronRight, ExternalLink, FileText, Filter, FolderOpen, Loader2, NotebookTabs, Play, Plus, Radar, RefreshCw, Settings, Sparkles, Trash2, Wrench, X } from "lucide-react";
import { COMPAT_PRESETS, DOCUMENT_AI_PROVIDERS, LOCAL_AI_RUNTIMES, SETTINGS_SECTIONS, WORK_MODES } from "../lib/constants";
import { formatBytes, toErrorMessage } from "../lib/format";
import { appConfirm } from "../lib/dialogs";
import { ModelSelect } from "../components/primitives";
import { EvidenceLibraryPanel, MemoryPanel } from "../components/campaign";

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
  const [expanded, setExpanded] = useState(false);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const disabled = busy || working;
  const hasRequiredFields = Boolean(form.source_name.trim() && form.careers_url.trim());
  const canBuild = hasRequiredFields && !disabled;

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

  if (!expanded) {
    return (
      <div className="scraper-builder-launch">
        <span className="scraper-builder-launch-icon"><Wrench size={18} /></span>
        <div><strong>Create a custom searcher</strong><span>Give JSE a careers page and it will inspect, generate and verify a scraper plugin.</span></div>
        <button className="secondary" onClick={() => setExpanded(true)}><Plus size={15} /> New scraper</button>
      </div>
    );
  }

  return (
    <section className="scraper-builder">
      <header className="scraper-builder-head">
        <div className="scraper-builder-title">
          <span><Wrench size={18} /></span>
          <div><h4>Create a custom searcher</h4><p>JSE will inspect the source, write the plugin and run a verification pass.</p></div>
        </div>
        <button className="ghost icon-only" disabled={working} aria-label="Close scraper builder" title="Close builder" onClick={() => setExpanded(false)}><X size={17} /></button>
      </header>

      <div className="scraper-builder-step">
        <div className="scraper-builder-step-head"><span>1</span><div><strong>Source</strong><small>Where should JSE look for roles?</small></div></div>
        <div className="scraper-builder-fields source-fields">
          <label><span>Source name <b>Required</b></span><input value={form.source_name} placeholder="My source" onChange={(event) => update("source_name", event.target.value)} /></label>
          <label><span>Company <em>Optional</em></span><input value={form.company_name} placeholder="My company" onChange={(event) => update("company_name", event.target.value)} /></label>
          <label className="wide"><span>Careers or search URL <b>Required</b></span><input type="url" value={form.careers_url} placeholder="https://careers.example.com/jobs" onChange={(event) => update("careers_url", event.target.value)} /></label>
        </div>
      </div>

      <div className="scraper-builder-step">
        <div className="scraper-builder-step-head"><span>2</span><div><strong>Search behaviour</strong><small>Use conservative test settings for the first run.</small></div></div>
        <div className="scraper-builder-fields behaviour-fields">
          <label><span>Mode</span><select value={form.mode} onChange={(event) => update("mode", event.target.value)}><option value="keyword">Keyword search</option><option value="sweep">Sweep all listings</option></select></label>
          <label><span>Default location</span><input value={form.location} placeholder="Melbourne VIC" onChange={(event) => update("location", event.target.value)} /></label>
          <label><span>Test keyword</span><input value={form.test_keyword} placeholder="business analyst" onChange={(event) => update("test_keyword", event.target.value)} /></label>
          <label><span>Test pages</span><input type="number" min="1" max="5" value={form.max_pages} onChange={(event) => update("max_pages", event.target.value)} /></label>
        </div>
      </div>

      <details className="scraper-builder-advanced">
        <summary><span><strong>Advanced guidance</strong><small>Optional platform clues or selector notes</small></span><ChevronRight size={16} /></summary>
        <div className="scraper-builder-fields advanced-fields">
          <label><span>Platform hint</span><input value={form.platform_hint} placeholder="PageUp, Workday, SmartRecruiters or custom" onChange={(event) => update("platform_hint", event.target.value)} /></label>
          <label><span>Notes for the local LLM</span><textarea rows={2} value={form.notes} placeholder="Known listing-card selectors, pagination behaviour, detail-page patterns or fields to capture…" onChange={(event) => update("notes", event.target.value)} /></label>
        </div>
      </details>

      {error ? <p className="settings-alert">{error}</p> : null}

      <footer className="scraper-builder-actions">
        <span>{working ? "Inspecting the source and generating the plugin…" : hasRequiredFields ? "Ready to inspect the source" : "Add a source name and careers URL to continue"}</span>
        <button disabled={!canBuild} onClick={build}>{working ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />} {working ? "Generating…" : "Generate plugin"}</button>
      </footer>
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
    </section>
  );
}

function SettingsPanel({ profile, laneCount, settings, globalSettings, scrapers, scraperError, memoryStatus, memoryFragments, memoryBusy, onSave, onSaveGlobal, onSaveProfile, onDeleteLane, onApplyFilters, onCompactDatabase, onRecoverDatabase, onResetRejected, onImportResume, onSearchResumes, onScanMemory, onImportScraper, onBuildScraper, onTestScraper, onDiagnoseScraper, onRepairScraper, onRollbackScraper, onUpdateScraper, onUpdateLaneScraper, onRemoveScraper }) {
  const [form, setForm] = useState(settings || {});
  const [globalForm, setGlobalForm] = useState(globalSettings || {});
  const [profileForm, setProfileForm] = useState({ name: "", resume_path: "" });
  const [resumeQuery, setResumeQuery] = useState("");
  const [resumeOptions, setResumeOptions] = useState([]);
  const [resumeSearchBusy, setResumeSearchBusy] = useState(false);
  const [settingsScope, setSettingsScope] = useState("lane");
  const [section, setSection] = useState("profile");
  const [compacting, setCompacting] = useState(false);
  const [recoveringDatabase, setRecoveringDatabase] = useState(false);
  const [resettingRejected, setResettingRejected] = useState(false);
  const [resetRejectedResult, setResetRejectedResult] = useState(null);
  const [compactResult, setCompactResult] = useState(null);
  const [providerTests, setProviderTests] = useState({});
  const [modelOptions, setModelOptions] = useState({});
  const [scraperActionId, setScraperActionId] = useState("");
  const [scraperActionMessage, setScraperActionMessage] = useState("");
  const [deletingLane, setDeletingLane] = useState(false);

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
  const recoverDatabase = async () => {
    const backupPath = await window.jobAssistant.chooseDatabaseBackup?.();
    if (!backupPath) return;
    const confirmed = await appConfirm({
      title: "Recover database?",
      message: "JSE will verify this backup, preserve the current database as a pre-restore backup, restore the selected file, and restart. Changes made after that backup will no longer be active.",
      confirmLabel: "Restore and restart",
      danger: true,
    });
    if (!confirmed) return;
    setRecoveringDatabase(true);
    try {
      await onRecoverDatabase(backupPath);
    } finally {
      setRecoveringDatabase(false);
    }
  };
  const resetRejected = async () => {
    const confirmed = await appConfirm({
      title: "Reset all rejected jobs?",
      message: "All jobs in the Rejected column will be moved back to New and their analysis cleared, so they are re-scored on the next analysis run. Jobs rejected by the company are not affected.",
      confirmLabel: "Reset to new",
    });
    if (!confirmed) return;
    setResettingRejected(true);
    setResetRejectedResult(null);
    try {
      const result = await onResetRejected();
      setResetRejectedResult(result.count);
    } finally {
      setResettingRejected(false);
    }
  };
  const deleteLane = async () => {
    if (!profile) return;
    const confirmed = await appConfirm({
      title: `Delete lane "${profile.name}"?`,
      message: "This permanently deletes the lane and everything scoped to it — jobs, pipeline history, search terms, application kits, and hidden-market records. This cannot be undone.",
      confirmLabel: "Delete lane",
      danger: true,
    });
    if (!confirmed) return;
    setDeletingLane(true);
    try {
      await onDeleteLane(profile.id);
    } finally {
      setDeletingLane(false);
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
  const runScraperAction = async (plugin, action, label) => {
    setScraperActionId(plugin.id);
    setScraperActionMessage("");
    try {
      const result = await action(plugin.id);
      setScraperActionMessage(result?.ok === false
        ? `${plugin.name}: ${result.error || `${label} needs review.`}`
        : `${plugin.name}: ${label} completed.`);
    } catch (error) {
      setScraperActionMessage(`${plugin.name}: ${toErrorMessage(error)}`);
    } finally {
      setScraperActionId("");
    }
  };

  const workflowProvider = (key) => globalForm[key] || globalForm.doc_ai_provider || "local";
  const scoringProvider = globalForm.scoring_ai_provider || "local";
  const providerIsConfigured = (provider) => {
    if (provider === "local") return Boolean((globalForm.local_base_url || "").trim());
    if (provider === "chatgpt") return Boolean((globalForm.openai_api_key || "").trim());
    if (provider === "claude") return Boolean((globalForm.claude_api_key || "").trim());
    if (provider === "gemini") return Boolean((globalForm.gemini_api_key || "").trim());
    if (provider === "compat") return Boolean((globalForm.compat_base_url || "").trim() && (globalForm.compat_model || "").trim());
    return false;
  };
  const providerIsUsed = (provider) => [
    workflowProvider("document_ai_provider"),
    workflowProvider("research_ai_provider"),
    workflowProvider("memory_ai_provider"),
    scoringProvider,
    "local"
  ].includes(provider);
  const PROVIDER_LABELS = { local: "Local endpoint", gemini: "Gemini", compat: "Free / OpenAI-compatible", chatgpt: "ChatGPT", claude: "Claude" };
  // Switching triage/scoring off the local engine sends every scraped ad (and
  // resume context) to a third party — make the user confirm that explicitly.
  const changeScoringProvider = async (value) => {
    if (value !== "local") {
      const confirmed = await appConfirm({
        title: "Send job data off your device?",
        message: `Job matching runs on every scraped ad in bulk. Using ${PROVIDER_LABELS[value] || value} for triage/scoring sends each job advert and your resume context to that provider — including free tiers, which may log or train on your data. Your local-first privacy no longer applies to matching.`,
        confirmLabel: `Use ${PROVIDER_LABELS[value] || value}`,
        warning: true
      });
      if (!confirmed) return;
    }
    updateGlobal("scoring_ai_provider", value);
    if (value !== "local") loadModels(value);
  };
  const applyCompatPreset = (presetId) => {
    const preset = COMPAT_PRESETS[presetId];
    if (!preset) return;
    updateGlobal("compat_base_url", preset.baseUrl);
    if (preset.model) updateGlobal("compat_model", preset.model);
  };
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

  const loadModels = (provider) => {
    if (!provider || provider === "claude") return;
    setModelOptions((current) => ({ ...current, [provider]: { ...(current[provider] || {}), loading: true } }));
    let task;
    task = window.jobAssistant.startTask(
      "ai:listModels",
      { provider, settings: globalForm },
      (event) => {
        if (event.type === "result") {
          task?.unsubscribe();
          setModelOptions((current) => ({ ...current, [provider]: { loading: false, models: event.data?.models || [] } }));
        } else if (event.type === "error") {
          task?.unsubscribe();
          setModelOptions((current) => ({ ...current, [provider]: { loading: false, models: [] } }));
        }
      }
    );
  };

  // Auto-discover model names for any configured provider when the AI section
  // opens, so the model fields can offer a dropdown instead of free text.
  useEffect(() => {
    if (section !== "ai") return;
    if ((globalForm.gemini_api_key || "").trim()) loadModels("gemini");
    if ((globalForm.compat_base_url || "").trim()) loadModels("compat");
    if ((globalForm.local_base_url || "").trim()) loadModels("local");
    if ((globalForm.openai_api_key || "").trim()) loadModels("chatgpt");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);

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
          {scraperActionMessage ? <p className="settings-alert scraper-action-message">{scraperActionMessage}</p> : null}
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
                  <small className={`scraper-health ${plugin.health?.status || "unknown"}`}>
                    Health: {plugin.health?.status || "unknown"}
                    {plugin.health?.last_error ? ` · ${plugin.health.last_error}` : ""}
                  </small>
                </div>
                <div className="scraper-controls">
                  <label className="check-row"><input type="checkbox" checked={Boolean(plugin.enabled)} onChange={(event) => onUpdateScraper(plugin.id, { enabled: event.target.checked })} /> Available</label>
                  <label className="check-row"><input type="checkbox" checked={plugin.lane_enabled !== false} onChange={(event) => onUpdateLaneScraper(plugin.id, { enabled: event.target.checked })} /> This lane</label>
                  <button className="secondary" disabled={Boolean(scraperActionId)} onClick={() => runScraperAction(plugin, onDiagnoseScraper, "diagnosis")}><Radar size={15} /> Diagnose</button>
                  <button disabled={Boolean(scraperActionId)} onClick={() => runScraperAction(plugin, onRepairScraper, "repair")}>
                    {scraperActionId === plugin.id ? <Loader2 className="spin" size={15} /> : <Wrench size={15} />} Repair
                  </button>
                  {plugin.can_rollback ? <button className="secondary" disabled={Boolean(scraperActionId)} onClick={() => runScraperAction(plugin, onRollbackScraper, "rollback")}><RefreshCw size={15} /> Roll back</button> : null}
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
            <article className="ai-route-card">
              <div className="ai-route-copy"><Radar size={17} /><div><strong>Job matching</strong><span>High-volume triage, scoring & analysis</span></div></div>
              <div className="ai-route-controls">
                <select aria-label="Job matching provider" value={scoringProvider} onChange={(event) => changeScoringProvider(event.target.value)}>{DOCUMENT_AI_PROVIDERS.map((provider) => <option key={provider.id} value={provider.id}>{provider.label}</option>)}</select>
                {scoringProvider !== "local" ? (
                  <label className="ai-route-model"><span>Matching model</span>
                    <ModelSelect
                      value={globalForm.scoring_model || ""}
                      options={modelOptions[scoringProvider]?.models}
                      loading={modelOptions[scoringProvider]?.loading}
                      placeholder={scoringProvider === "gemini" ? "e.g. gemini-2.5-flash" : "Model name"}
                      onChange={(model) => updateGlobal("scoring_model", model)}
                      onRefresh={() => loadModels(scoringProvider)}
                    />
                  </label>
                ) : null}
                <label className="ai-route-model"><span>Simultaneous LLM requests</span>
                  <select
                    aria-label="Simultaneous LLM requests"
                    value={scoringProvider === "local" ? "1" : String(globalForm.analysis_workers || "1")}
                    disabled={scoringProvider === "local"}
                    onChange={(event) => updateGlobal("analysis_workers", event.target.value)}
                  >
                    <option value="1">1 — one at a time</option>
                    <option value="2">2 in parallel</option>
                    <option value="3">3 in parallel</option>
                    <option value="4">4 in parallel</option>
                    <option value="6">6 in parallel</option>
                    <option value="8">8 in parallel</option>
                  </select>
                  <small className="field-hint">{scoringProvider === "local"
                    ? "The local endpoint runs one request at a time — it returns 429 if sent overlapping requests. Switch matching to a hosted or free endpoint to raise this."
                    : "Caps concurrent requests across matching, analysis and document generation. Raise only if the endpoint genuinely serves parallel requests."}</small>
                </label>
              </div>
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
                    <button type="button" className="secondary ai-test-button" onClick={() => {
                      updateGlobal("local_base_url", runtime.baseUrl);
                      if (runtime.model) updateGlobal("local_model", runtime.model);
                    }}>Use {runtime.label} preset</button>
                    <button type="button" className="secondary ai-test-button" onClick={() => window.jobAssistant.openExternal(runtime.downloadUrl)}><ExternalLink size={13} /> Install</button>
                  </div>
                ))}
              </div>
              {providerTests.local && !providerTests.local.testing ? <div className={`ai-test-result ${providerTests.local.ok ? "ok" : providerTests.local.warning ? "warning" : "bad"}`}>{providerTests.local.message}</div> : null}
            </article>
            <article className={`ai-provider-card ${providerIsUsed("gemini") ? "in-use" : ""}`}>
              <header><div><span className="provider-mark gemini">G</span><div><strong>Gemini</strong><small>Google AI models</small></div></div><div className="provider-card-actions"><span className={`provider-status ${providerStatusClass("gemini")}`}><i />{providerStatus("gemini")}</span><button type="button" className="secondary ai-test-button" disabled={providerTests.gemini?.testing} onClick={() => testProvider("gemini")}>{providerTests.gemini?.testing ? <Loader2 className="spin" size={12} /> : <Play size={12} />}Test</button></div></header>
              <div className="ai-provider-fields">
                <label><span>API key</span><input type="password" value={globalForm.gemini_api_key || ""} placeholder="Required" onChange={(event) => updateGlobal("gemini_api_key", event.target.value)} /></label>
                <label><span>Model</span>
                  <ModelSelect value={globalForm.gemini_model || ""} options={modelOptions.gemini?.models} loading={modelOptions.gemini?.loading} placeholder="gemini-3.1-pro-preview" onChange={(model) => updateGlobal("gemini_model", model)} onRefresh={() => loadModels("gemini")} />
                </label>
              </div>
              {providerTests.gemini && !providerTests.gemini.testing ? <div className={`ai-test-result ${providerTests.gemini.ok ? "ok" : "bad"}`}>{providerTests.gemini.message}</div> : null}
            </article>
            <article className={`ai-provider-card ${providerIsUsed("compat") ? "in-use" : ""}`}>
              <header><div><span className="provider-mark compat">F</span><div><strong>Free / OpenAI-compatible</strong><small>Groq, Cerebras, OpenRouter, OpenCode Zen…</small></div></div><div className="provider-card-actions"><span className={`provider-status ${providerStatusClass("compat")}`}><i />{providerStatus("compat")}</span><button type="button" className="secondary ai-test-button" disabled={providerTests.compat?.testing} onClick={() => testProvider("compat")}>{providerTests.compat?.testing ? <Loader2 className="spin" size={12} /> : <Play size={12} />}Test</button></div></header>
              <div className="ai-provider-fields">
                <label className="full"><span>Base URL</span><input value={globalForm.compat_base_url || ""} placeholder="https://api.groq.com/openai/v1" onChange={(event) => updateGlobal("compat_base_url", event.target.value)} /></label>
                <label><span>Model</span>
                  <ModelSelect value={globalForm.compat_model || ""} options={modelOptions.compat?.models} loading={modelOptions.compat?.loading} placeholder="Model name" onChange={(model) => updateGlobal("compat_model", model)} onRefresh={() => loadModels("compat")} />
                </label>
                <label><span>API key</span><input type="password" value={globalForm.compat_api_key || ""} placeholder="Optional on some endpoints" onChange={(event) => updateGlobal("compat_api_key", event.target.value)} /></label>
              </div>
              <div className="local-ai-quickstart">
                <span>Quick presets:</span>
                {Object.entries(COMPAT_PRESETS).map(([id, preset]) => (
                  <div key={id}>
                    <button type="button" className="secondary ai-test-button" onClick={() => applyCompatPreset(id)}>Use {preset.label}</button>
                    <button type="button" className="secondary ai-test-button" onClick={() => window.jobAssistant.openExternal(preset.keyUrl)}><ExternalLink size={13} /> Get key</button>
                  </div>
                ))}
              </div>
              {providerTests.compat && !providerTests.compat.testing ? <div className={`ai-test-result ${providerTests.compat.ok ? "ok" : "bad"}`}>{providerTests.compat.message}</div> : null}
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
            <label className="full"><span>Positioning doctrine</span><textarea rows={8} value={form.positioning_doctrine || ""} placeholder="Leave blank to score this lane against the default doctrine. Set it when this lane hunts a different role family or level to your primary market — otherwise the default retires roles this lane exists to find." onChange={(event) => update("positioning_doctrine", event.target.value)} /></label>
          </div>
          <p className="settings-hint">The positioning doctrine is the market view every scoring pass is judged against: which role families are on target, which level, which salary band. Blank uses the app default.</p>
          <div className="section-actions">
            <button className="secondary" onClick={chooseResume}><FolderOpen size={16} /> Choose resume</button>
            <button disabled={!profile || !profileForm.name.trim() || !profileForm.resume_path.trim()} onClick={() => onSaveProfile(profileForm)}><Check size={16} /> Save lane</button>
            <button onClick={() => onSave(form)}><Check size={16} /> Save lane strategy</button>
            <button
              className="ghost danger"
              disabled={!profile || deletingLane || laneCount <= 1}
              data-tooltip={laneCount <= 1 ? "At least one lane must remain" : "Permanently delete this lane and its data"}
              onClick={deleteLane}
            >{deletingLane ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />} Delete lane</button>
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
            <label><span>Add weight when present</span><input value={form.boost_terms || ""} placeholder="robotics; product strategy; transformation" onChange={(event) => update("boost_terms", event.target.value)} /></label>
            <label><span>Subtract weight when present</span><input value={form.penalty_terms || ""} placeholder="shift work; on call; weekend work" onChange={(event) => update("penalty_terms", event.target.value)} /></label>
          </div>
          <p className="settings-hint">Separate terms with semicolons, commas, or new lines. Each term found in the ad shifts the triage score by up to +10 / -15 in total.</p>
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
          <div className="maintenance-row">
            <button className="secondary" disabled={recoveringDatabase} onClick={recoverDatabase}><FolderOpen size={16} /> {recoveringDatabase ? "Recovering..." : "Recover database"}</button>
            <span>Restore a verified JSE backup. The current database is backed up first, then JSE restarts.</span>
          </div>
          <div className="maintenance-row">
            <button className="secondary" disabled={resettingRejected} onClick={resetRejected}><RefreshCw size={16} /> {resettingRejected ? "Resetting..." : "Reset rejected to new"}</button>
            {resetRejectedResult != null
              ? <span>{resetRejectedResult} job{resetRejectedResult === 1 ? "" : "s"} reset. Run analysis to re-score them.</span>
              : <span>Move all rejected jobs back to New and clear their scores so they are re-analysed from scratch.</span>}
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

export { ScraperPluginBuilder, SettingsPanel };

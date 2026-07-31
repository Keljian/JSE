/**
 * JSE renderer entry point and composition root.
 *
 * This file owns application state and wires the panels together. The ~110
 * components and helpers it used to also define now live in ./components and
 * ./lib; see those modules for the layering.
 *
 * Everything is mounted inside an ErrorBoundary. JSE is a desktop app with no
 * address bar and no reload button, so an uncaught render error would otherwise
 * leave a blank window with no way to recover or report it.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BarChart3, CircleStop, Download, FileText, ClipboardCheck, GraduationCap, KanbanSquare, Loader2, NotebookTabs, Info, Play, Plus, Radar, RefreshCw, Settings, Sparkles, Target, TrendingUp, X } from "lucide-react";
import "./styles.css";
import jseIcon from "../assets/jse-icon.png";

import { KANBAN_COLUMN_RENDER_CAP, PIPELINE, SUPPORT_MESSAGE, SUPPORT_URL, WORK_MODES } from "./lib/constants";
import { documentAiLabel, formatBytes, hasCompanyResearch, normalizeStage, openSupportLink, jobFlagTypesOf, primaryScore, toErrorMessage, todayPlus } from "./lib/format";
import { appConfirm, appNotice, dialogBridge } from "./lib/dialogs";
import { DialogModal, DocumentTextModal } from "./components/primitives";
import { JobCard } from "./components/chips";
import { AddJobModal, AnalysisModal, CleanupModal, CreateLaneModal, LogExternalModal, OnboardingWizard, QuickStageForm, RejectJobModal, RunSearchModal } from "./components/modals";
import { WorkspaceModal } from "./components/workspace";
import { AboutPanel, Dashboard, InterviewLearningsPanel, UpdateToast } from "./components/dashboard";
import { CampaignPanel, StatsPanel } from "./components/campaign";
import { HiddenMarketPanel } from "./components/hiddenMarket";
import { SettingsPanel } from "./components/settings";
import { ErrorBoundary } from "./components/ErrorBoundary";

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
  const [exportingShortlist, setExportingShortlist] = useState(false);
  const [runSearchOpen, setRunSearchOpen] = useState(false);
  const [addLaneOpen, setAddLaneOpen] = useState(false);
  const [addLaneBusy, setAddLaneBusy] = useState(false);
  const [addJobOpen, setAddJobOpen] = useState(false);
  const [addExternalOpen, setAddExternalOpen] = useState(false);
  const [addJobBusy, setAddJobBusy] = useState(false);
  const [dismissedNudges, setDismissedNudges] = useState(() => new Set());
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
  const hasActiveProfile = profiles.some((profile) => Number(profile.id) === Number(activeProfileId));
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
  const normalizeActiveProfile = useCallback((nextProfiles, preferredId = 0) => {
    const lanes = Array.isArray(nextProfiles) ? nextProfiles : [];
    if (!lanes.length) {
      setActiveProfileId(0);
      setRunSearchOpen(false);
      setAnalysisOpen(false);
      return 0;
    }
    const preferred = lanes.find((profile) => Number(profile.id) === Number(preferredId));
    const nextId = preferred?.id || lanes[0].id;
    setActiveProfileId((currentId) => Number(nextId) === Number(currentId) ? currentId : nextId);
    return nextId;
  }, []);

  const checkForAppUpdates = async () => {
    try {
      const nextUpdate = await window.jobAssistant.checkForUpdates?.();
      if (nextUpdate) setAppUpdate(nextUpdate);
    } catch (error) {
      setAppUpdate({ status: "error", message: toErrorMessage(error) });
    }
  };

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
    const nextProfiles = data.profiles || [];
    setProfiles(nextProfiles);
    normalizeActiveProfile(nextProfiles, profileIdOverride || activeProfileId);
    setSources(Array.from(new Set(data.sources || [])));
    setSearchSources(Array.from(new Set(data.search_sources || data.sources || [])));
    setJobs(data.jobs || []);
    setDashboard(data.dashboard);
    setCalendar(data.calendar || []);
    setMemoryStatus(data.memory);
    setMemoryFragments(data.fragments || []);
  }, [activeProfileId, includeAllProfiles, invoke, normalizeActiveProfile, requestPayload]);

  useEffect(() => {
    Promise.all([invoke("app:init"), window.jobAssistant.getPrerequisites?.() || Promise.resolve(null)])
      .then(([data, prerequisiteData]) => {
        setProfiles(data.profiles);
        normalizeActiveProfile(data.profiles, data.active_profile_id);
        setSources(Array.from(new Set(data.sources || [])));
        setSearchSources(Array.from(new Set(data.search_sources || data.sources || [])));
        setGlobalSettings(data.app_settings || {});
        setPrerequisites(prerequisiteData);
        setOnboardingOpen(Boolean(data.needs_onboarding));
      })
      .catch((error) => appendLog(`Startup failed: ${toErrorMessage(error)}`))
      .finally(() => setBooting(false));
  }, [appendLog, invoke, normalizeActiveProfile]);

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
    if (command.startsWith("funnel:")) return "learnings";
    return command;
  };

  // Human-friendly names for the status strip and activity log, so a running
  // task reads "Mining interview fragments" rather than "funnel:mineInterviewFragments".
  const taskLabel = (kind) => ({
    search: "Searching sources",
    analysis: "Analysing jobs",
    docs: "Generating documents",
    company: "Researching employers",
    memory: "Mining application memory",
    campaign: "Updating campaign",
    laneSetup: "Setting up lane",
    learnings: "Mining interview fragments",
  }[kind] || kind);

  const runTask = useCallback((command, payload, doneMessage, refreshProfileId = null, onComplete = null) => {
    const taskKind = taskKindForCommand(command);
    if (activeTasks[taskKind]) {
      appendLog(`${taskLabel(taskKind)} is already running.`);
      return;
    }
    setStatus("Running");
    appendLog(`Started: ${taskLabel(taskKind)}`);
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
        if (onComplete) onComplete();
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
        if (command.startsWith("scrape:")) {
          refresh(refreshProfileId).catch((error) => appendLog(toErrorMessage(error)));
        }
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

  const logExternalJob = async (form) => {
    setAddJobBusy(true);
    try {
      const data = await invoke("jobs:logExternal", { profile_id: activeProfileId, ...form, stage: "applied" });
      setAddExternalOpen(false);
      appendLog(data.added ? `Logged external application: ${form.title}` : `Not logged — ${data.message}`);
      await refresh();
      if (data.job_id) await openJob(data.job_id);
    } catch (error) {
      appendLog(`Log external application failed: ${toErrorMessage(error)}`);
    } finally {
      setAddJobBusy(false);
    }
  };

  // Non-blocking outcome hygiene nudge resolution (item 7): record how a past
  // interview went. "waiting" just dismisses locally; offer/declined advance the
  // pipeline stage (and thus the outcome snapshot) and record the interview's
  // own outcome text.
  // Resolutions map to both the interview row and the outcome snapshot. The
  // near-miss states (final_round, runner_up) still move the job to
  // rejected_by_company in the pipeline — the role is over — but the outcome
  // snapshot keeps the distinction, because "second by a small margin" and
  // "screened out in round one" are different results with different fixes.
  const NUDGE_OUTCOMES = {
    offer: { label: "Progressed / offer", stage: "offer", outcome: "offer" },
    final_round: { label: "Reached final round", stage: "rejected_by_company", outcome: "final_round" },
    runner_up: { label: "Runner-up", stage: "rejected_by_company", outcome: "runner_up" },
    declined: { label: "Unsuccessful", stage: "rejected_by_company", outcome: "declined" },
  };

  const resolveInterviewNudge = async (nudge, resolution, detail = {}) => {
    try {
      const mapped = NUDGE_OUTCOMES[resolution];
      if (mapped) {
        await invoke("interviews:update", { interview_id: nudge.interview_id, interview: { outcome: mapped.label } });
        await invoke("jobs:updateStatus", { job_id: nudge.job_id, status: mapped.stage });
        await invoke("funnel:outcomeDetail", {
          job_id: nudge.job_id,
          outcome: mapped.outcome,
          interview_stage_reached: detail.stage || nudge.round_number || null,
          loss_reason: detail.loss_reason || null,
        });
        appendLog(`Recorded "${mapped.label}" for ${nudge.job_title}.`);
        await refresh();
      }
      setDismissedNudges((current) => new Set(current).add(nudge.interview_id));
    } catch (error) {
      appendLog(`Could not record interview outcome: ${toErrorMessage(error)}`);
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

  const generateDocsForJob = async (job) => {
    if (!job) return;
    // Flags are noted in the log, not enforced. They are already on the card
    // and in the workspace; if the call is to apply anyway, that is the call.
    const flagCount = jobFlagTypesOf(job).length;
    if (flagCount) {
      appendLog(`${job.title} has ${flagCount} flag${flagCount === 1 ? "" : "s"}; generating anyway.`);
    }
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
    const flagged = candidates.filter((job) => jobFlagTypesOf(job).length);
    const flaggedNote = flagged.length
      ? `\n\n${flagged.length} of these carr${flagged.length === 1 ? "ies" : "y"} flags worth a look first: ${flagged.map((job) => job.title).join(", ")}.`
      : "";
    const confirmed = await appConfirm({
      title: "Generate Interested documents",
      message: `Generate a tailored resume and cover letter for all ${candidates.length} job${candidates.length === 1 ? "" : "s"} currently shown in Interested? Jobs are processed one at a time and existing generated files for the same company and role are replaced.${flaggedNote}`,
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
            message: `Finished: ${result.succeeded || 0} generated${result.skipped ? `, ${result.skipped} skipped (closed or gated)` : ""}${result.failed ? `, ${result.failed} failed` : ""}.`
          });
          appendLog(`Interested document batch complete: ${result.succeeded || 0} generated, ${result.skipped || 0} skipped (closed or gated), ${result.failed || 0} failed.`);
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

  // Auto-load Intelligence on open and whenever the lane, scope, or
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
        target_key: target.target_key,
        target_name: target.name,
        contact_person: target.contact_person,
        contact_email: target.contact_email,
        contact_phone: target.contact_phone,
        domain: target.domain,
        action: target.recommended_action,
        outreach_channel: target.saved_strategy?.recommended_channel,
        strategy: target.saved_strategy,
        opportunity_score: target.opportunity_score,
        score_reasons: target.score_reasons,
      });
      await loadHiddenMarket();
    } catch (error) {
      appendLog(`Could not track target: ${toErrorMessage(error)}`);
    }
  };

  // A warm lead created against a named employer, independent of any scraped
  // job. This is the entry point the hidden-market modules were missing.
  const addHiddenMarketTarget = async (form) => {
    try {
      const result = await invoke("hiddenMarket:addTarget", {
        profile_id: activeProfileId,
        target_name: form.target_name,
        action: form.action,
        contact_person: form.contact_person,
        contact_email: form.contact_email,
        domain: form.domain,
      });
      appendLog(`Warm lead created for ${result?.lead?.target_name || form.target_name}.`);
      await loadHiddenMarket();
      await refresh();
    } catch (error) {
      appendLog(`Could not create the warm lead: ${toErrorMessage(error)}`);
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
      return data || {};
    } catch (error) {
      appendLog(`AI angle failed: ${toErrorMessage(error)}`);
      return {};
    }
  };
  const hiddenContactSelect = async (target, candidateId) => {
    const data = await invoke("hiddenMarket:contactSelect", { profile_id: activeProfileId, target, candidate_id: candidateId });
    return data?.contact_research || null;
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

  const runFlagCommand = async (command, payload, note) => {
    if (!workspace.job) return;
    try {
      await invoke(command, { job_id: workspace.job.id, ...payload });
      appendLog(`${note} for ${workspace.job.title}.`);
      const detail = await invoke("jobs:detail", { job_id: workspace.job.id });
      setWorkspace((current) => ({ ...current, job: detail.job, events: detail.events }));
      await refresh();
    } catch (error) {
      appendLog(`Could not update flags: ${toErrorMessage(error)}`);
    }
  };

  const addWorkspaceFlag = (draft) => runFlagCommand("jobs:addFlag", draft, "Flag added");
  const dismissWorkspaceFlag = (requirement) => runFlagCommand("jobs:dismissFlag", { requirement }, "Flag dismissed");
  const clearWorkspaceFlags = () => runFlagCommand("jobs:clearFlags", {}, "Flags cleared");

  const exportShortlist = async () => {
    setExportingShortlist(true);
    try {
      const result = await invoke("jobs:exportShortlist", {
        profile_id: activeProfileId,
        include_all_profiles: includeAllProfiles
      });
      if (!result.count) {
        appendLog("No roles survived triage for the packet; nothing exported.");
        return;
      }
      appendLog(`Triage packet written for ${result.count} role${result.count === 1 ? "" : "s"}: ${result.files.join(", ")}`);
      await appNotice({
        title: "Triage packet exported",
        message: `${result.count} role${result.count === 1 ? "" : "s"} written to:\n${result.folder}`
      });
      await window.jobAssistant.showPath(result.folder);
    } catch (error) {
      appendLog(`Could not export the triage packet: ${toErrorMessage(error)}`);
    } finally {
      setExportingShortlist(false);
    }
  };

  const setWorkspaceDocumentTrack = async (track) => {
    if (!workspace.job) return;
    try {
      const result = await invoke("jobs:setDocumentTrack", { job_id: workspace.job.id, track });
      appendLog(`Document track for ${workspace.job.title}: ${result.resolved.track.replace("_", " ")} (${result.resolved.source}).`);
      const detail = await invoke("jobs:detail", { job_id: workspace.job.id });
      setWorkspace((current) => ({ ...current, job: detail.job, events: detail.events }));
    } catch (error) {
      appendLog(`Could not update the document track: ${toErrorMessage(error)}`);
    }
  };

  const setWorkspaceChannel = async (channel) => {
    if (!workspace.job) return;
    try {
      await invoke("jobs:setChannel", { job_id: workspace.job.id, channel });
      appendLog(channel
        ? `Application channel set for ${workspace.job.title}.`
        : `Application channel cleared for ${workspace.job.title}; it will be derived from the source.`);
      const detail = await invoke("jobs:detail", { job_id: workspace.job.id });
      setWorkspace((current) => ({ ...current, job: detail.job, events: detail.events }));
      await refresh();
    } catch (error) {
      appendLog(`Could not update the application channel: ${toErrorMessage(error)}`);
    }
  };

  const addWorkspaceEvent = async (details) => {
    const data = await invoke("events:add", { job_id: workspace.job.id, event_type: "note", title: "Note", details });
    setWorkspace((current) => ({ ...current, events: data.events }));
    await refresh();
  };

  const generateDocs = async (additionalCandidateContext = "") => {
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

  const convertDocumentPdf = async (filePath, docType) => {
    if (!workspace.job || !filePath) return;
    try {
      if (!window.jobAssistant.convertDocumentToPdf) {
        appendLog("PDF conversion needs an app restart to activate.");
        return;
      }
      const data = await window.jobAssistant.convertDocumentToPdf(filePath);
      const docLabel = {
        resume: "Resume",
        cover_letter: "Cover letter",
        position_description: "Position description"
      }[docType] || "Application document";
      await invoke("events:add", {
        job_id: workspace.job.id,
        event_type: "documents",
        title: `${docLabel} converted to PDF`,
        details: `Source: ${data.source_path || filePath}\nPDF: ${data.pdf_path}`
      });
      appendLog(`PDF created: ${data.pdf_path}`);
      const detail = await invoke("jobs:detail", { job_id: workspace.job.id });
      setWorkspace((current) => ({
        ...current,
        job: detail.job,
        events: detail.events,
        interviews: detail.interviews || current.interviews
      }));
      await window.jobAssistant.showPath(data.pdf_path);
    } catch (error) {
      appendLog(`PDF conversion failed: ${toErrorMessage(error)}`);
      throw error;
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
    let filePath = file?.path || file?.webkitRelativePath;
    try {
      filePath = filePath || window.jobAssistant.getPathForFile?.(file);
    } catch (error) {
      appendLog(`Could not read the selected file path: ${toErrorMessage(error)}`);
      return;
    }
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

  const deleteLane = async (laneId) => {
    const deleted = profiles.find((profile) => profile.id === laneId);
    const data = await invoke("profiles:delete", { profile_id: laneId });
    const nextProfiles = data.profiles || [];
    setProfiles(nextProfiles);
    appendLog(`Deleted lane: ${deleted?.name || laneId}.`);
    const nextActiveId = normalizeActiveProfile(
      nextProfiles,
      Number(laneId) === Number(activeProfileId) ? 0 : activeProfileId
    );
    if (nextActiveId) {
      await refresh(nextActiveId);
    } else {
      setJobs([]);
      setDashboard(null);
      setCalendar([]);
      setSearchSources([]);
    }
  };

  const compactDatabase = async () => {
    appendLog("Compacting database...");
    const result = await invoke("database:compact");
    appendLog(`Database compacted. Reclaimed ${formatBytes(result.reclaimed_bytes)}.`);
    return result;
  };
  const resetRejectedJobs = async () => {
    const result = await invoke("jobs:resetRejected", { profile_id: activeProfileId });
    appendLog(`Reset ${result.count} rejected job${result.count === 1 ? "" : "s"} to new.`);
    await refresh();
    return result;
  };
  const recoverDatabase = async (backupPath) => {
    appendLog(`Recovering database from ${backupPath}...`);
    const result = await window.jobAssistant.restoreDatabase(backupPath);
    appendLog(`Database recovered (${result.jobs} jobs). Restarting JSE...`);
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
  const diagnoseScraper = async (pluginId) => {
    setScraperError("");
    appendLog(`Diagnosing scraper plugin ${pluginId}...`);
    const data = await invoke("scrapers:diagnose", {
      profile_id: activeProfileId,
      id: pluginId,
      max_pages: 1
    });
    setScrapers(data.scrapers || []);
    appendLog(data.ok ? `Scraper diagnosis passed: ${pluginId}.` : `Scraper diagnosis found a problem: ${pluginId}.`);
    return data;
  };
  const repairScraper = async (pluginId) => {
    setScraperError("");
    appendLog(`Attempting a verified repair for scraper plugin ${pluginId}...`);
    const data = await invoke("scrapers:repair", {
      profile_id: activeProfileId,
      id: pluginId,
      max_pages: 1,
      max_attempts: 3
    });
    setScrapers(data.scrapers || []);
    await refreshScrapers();
    appendLog(data.ok ? `Verified scraper repair applied: ${pluginId}.` : `No verified repair was applied: ${pluginId}.`);
    return data;
  };
  const rollbackScraper = async (pluginId) => {
    setScraperError("");
    appendLog(`Rolling back scraper repair ${pluginId}...`);
    const data = await invoke("scrapers:rollback", { profile_id: activeProfileId, id: pluginId });
    setScrapers(data.scrapers || []);
    await refreshScrapers();
    appendLog(`Scraper repair rolled back: ${pluginId}.`);
    return data;
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
    hiddenMarket: "Intelligence",
    pipeline: "Pipeline",
    stats: "Stats",
    learnings: "Interview Learnings",
    activity: "Activity",
    settings: "Settings",
    about: "About",
  }[view] || "Dashboard";

  // Views that use the pipeline query filters (search, stage, source, score,
  // work mode). Analytics-style views (learnings, stats, activity) only need the
  // lane scope, so the heavy filter row is hidden there.
  const showPipelineFilters = ["dashboard", "campaign", "hiddenMarket", "pipeline"].includes(view);

  return (
    <main className="ats-shell">
      {onboardingOpen ? <OnboardingWizard prerequisites={prerequisites} profile={activeProfile} busy={onboardingBusy} onComplete={finishOnboarding} onSkip={skipOnboarding} /> : null}
      <aside className="nav-rail">
        <div className="brand">
          <img className="brand-icon" src={jseIcon} alt="" />
          <div><strong>JSE</strong><span>Application ATS</span></div>
        </div>
        <button className={view === "dashboard" ? "active nav-btn" : "nav-btn"} onClick={() => setView("dashboard")}><BarChart3 size={18} /> Dashboard</button>
        <button className={view === "campaign" ? "active nav-btn" : "nav-btn"} onClick={() => setView("campaign")}><Target size={18} /> Campaign</button>
        <button className={view === "hiddenMarket" ? "active nav-btn" : "nav-btn"} onClick={() => setView("hiddenMarket")}><Radar size={18} /> Intelligence</button>
        <button className={view === "pipeline" ? "active nav-btn" : "nav-btn"} onClick={() => setView("pipeline")}><KanbanSquare size={18} /> Pipeline</button>
        <button className={view === "stats" ? "active nav-btn" : "nav-btn"} onClick={() => setView("stats")}><TrendingUp size={18} /> Stats</button>
        <button className={view === "learnings" ? "active nav-btn" : "nav-btn"} onClick={() => setView("learnings")}><GraduationCap size={18} /> Learnings</button>
        <button className={view === "activity" ? "active nav-btn" : "nav-btn"} onClick={() => setView("activity")}><NotebookTabs size={18} /> Activity</button>
        <button className={view === "settings" ? "active nav-btn" : "nav-btn"} onClick={() => setView("settings")}><Settings size={18} /> Settings</button>
        <div className="nav-spacer" />
        <button className={view === "about" ? "active nav-btn" : "nav-btn"} onClick={() => setView("about")}><Info size={18} /> About</button>
        <button
          className="secondary wide nav-add-lane"
          disabled={Boolean(activeTasks.laneSetup)}
          data-tooltip={activeTasks.laneSetup ? "Finish the current lane setup first" : "Create a new job-search lane"}
          aria-description={activeTasks.laneSetup ? "Finish the current lane setup first" : "Create a new job-search lane"}
          onClick={() => setAddLaneOpen(true)}
        ><Plus size={16} /> Add lane</button>
      </aside>

      <section className="ats-main">
        <header className="toolbar">
          <div>
            <h1>{viewTitle}</h1>
            <p>{view === "about" ? `JSE ${prerequisites?.app_version || ""}` : `${includeAllProfiles ? "All lanes" : activeProfile?.name || "Lane"} · ${status}`}</p>
          </div>
          {view !== "about" ? <div className="toolbar-actions">
            <button
              disabled={!hasActiveProfile}
              data-tooltip={hasActiveProfile ? "Search enabled sources for new roles" : "Add a lane before running search"}
              aria-description={hasActiveProfile ? "Search enabled sources for new roles" : "Add a lane before running search"}
              onClick={() => {
                if (!hasActiveProfile) {
                  appendLog("Add a lane before running search.");
                  return;
                }
                setRunSearchOpen(true);
              }}
            ><Play size={16} /> Run Search</button>
            <button className="secondary" data-tooltip="Add a job listing manually" aria-description="Add a job listing manually" onClick={() => setAddJobOpen(true)}><Plus size={16} /> Add Job</button>
            {view === "pipeline" ? <button className="secondary" data-tooltip="Log an application you made outside JSE" aria-description="Log an application you made outside JSE" onClick={() => setAddExternalOpen(true)}><ClipboardCheck size={16} /> Log External</button> : null}
            <button className="secondary" data-tooltip="Analyse unreviewed jobs for fit" aria-description="Analyse unreviewed jobs for fit" onClick={() => setAnalysisOpen(true)}><Sparkles size={16} /> Run Analysis</button>
            <button className="secondary" data-tooltip="Reload jobs and dashboard data" aria-description="Reload jobs and dashboard data" onClick={() => refresh()}><RefreshCw size={16} /> Refresh</button>
            <button className="danger" data-tooltip="Stop all running searches and tasks" aria-description="Stop all running searches and tasks" onClick={stopAllTasks}><CircleStop size={16} /> Stop</button>
          </div> : null}
        </header>

        {view !== "about" ? <section className={view === "settings" ? "filter-bar settings-filter-bar" : "filter-bar"}>
          <div className="filter-search-row">
            <label className="profile-filter"><span>Lane</span><select value={activeProfileId} disabled={!profiles.length} onChange={(event) => setActiveProfileId(Number(event.target.value))}>{profiles.length ? profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>) : <option value={0}>No lanes</option>}</select></label>
            {showPipelineFilters ? (
              <label className="search-field"><span>Search</span><input value={filters.query} placeholder="Title, company, notes, analysis, lane..." onChange={(event) => updateFilter("query", event.target.value)} /></label>
            ) : null}
            {view !== "settings" ? (
              <label className="filter-chip all-profiles"><input type="checkbox" checked={includeAllProfiles} onChange={(event) => setIncludeAllProfiles(event.target.checked)} /> All lanes</label>
            ) : null}
          </div>
          {showPipelineFilters ? (
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
        </section> : null}

        {view === "about" ? <AboutPanel version={prerequisites?.app_version} update={appUpdate} onCheckForUpdates={checkForAppUpdates} /> : null}

        {view === "dashboard" ? <Dashboard dashboard={dashboard} calendar={calendar} invoke={invoke} onOpenJob={openJob} onOpenCleanup={() => setCleanupOpen(true)} dismissedNudges={dismissedNudges} onResolveNudge={resolveInterviewNudge} onOpenHiddenMarket={() => setView("hiddenMarket")} /> : null}

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
            onAddTarget={addHiddenMarketTarget}
            onStrategy={hiddenStrategy}
            onContactSelect={hiddenContactSelect}
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
                {stage.id === "new" ? (
                  <div className="interested-batch-toolbar">
                    <button
                      className="secondary"
                      disabled={exportingShortlist || !(groupedJobs.new?.length)}
                      onClick={exportShortlist}
                      title="Write one triage packet — ad text, scores, gate verdict, warm paths — for the survivors of this sweep"
                    >
                      {exportingShortlist ? <Loader2 className="spin" size={14} /> : <Download size={14} />}
                      Export triage packet
                      <span>{groupedJobs.new?.length || 0}</span>
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

        {view === "learnings" ? (
          <InterviewLearningsPanel
            invoke={invoke}
            runTask={runTask}
            activeTasks={activeTasks}
            profileId={activeProfileId}
            includeAllProfiles={includeAllProfiles}
            onOpenJob={openJob}
          />
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
            onDeleteLane={deleteLane}
            laneCount={profiles.length}
            onApplyFilters={applySettingsToFilters}
            onCompactDatabase={compactDatabase}
            onRecoverDatabase={recoverDatabase}
            onResetRejected={resetRejectedJobs}
            onImportResume={importResume}
            onSearchResumes={searchResumes}
            onScanMemory={scanProfileMemory}
            onImportScraper={importScraper}
            onBuildScraper={buildScraper}
            onTestScraper={testScraper}
            onDiagnoseScraper={diagnoseScraper}
            onRepairScraper={repairScraper}
            onRollbackScraper={rollbackScraper}
            onUpdateScraper={updateScraper}
            onUpdateLaneScraper={updateLaneScraper}
            onRemoveScraper={removeScraper}
          />
        ) : null}
      </section>

      {runSearchOpen ? <RunSearchModal sources={searchSources} activeProfileId={activeProfileId} busy={searchBusy} onClose={() => setRunSearchOpen(false)} onRun={(payload) => { setRunSearchOpen(false); runTask("scrape:run", payload, "Search complete.", null, payload.auto_run_analysis ? () => runTask("analysis:run", { profile_id: payload.profile_id, include_all_profiles: payload.include_all_profiles, stage: "new" }, "Analysis complete.") : null); }} /> : null}
      {addLaneOpen ? <CreateLaneModal busy={addLaneBusy} onClose={() => setAddLaneOpen(false)} onCreate={createLane} /> : null}
      {addJobOpen ? <AddJobModal busy={addJobBusy} onClose={() => setAddJobOpen(false)} onSave={addManualJob} /> : null}
      {addExternalOpen ? <LogExternalModal busy={addJobBusy} onClose={() => setAddExternalOpen(false)} onSave={logExternalJob} /> : null}
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
          onConvertDocumentPdf={convertDocumentPdf}
          onAnalyzeJob={() => analyzeJob(workspace.job)}
          onMoveProfile={moveWorkspaceProfile}
          analyzing={analysisBusy}
          generatingDocs={docsBusy}
          researchingCompany={Boolean(activeTasks.company)}
          documentAiName={documentAiLabel(settings)}
          onRejectJob={rejectFromWorkspace}
          onMoveInterested={moveInterestedFromWorkspace}
          onAddFlag={addWorkspaceFlag}
          onDismissFlag={dismissWorkspaceFlag}
          onClearFlags={clearWorkspaceFlags}
          onSetChannel={setWorkspaceChannel}
          onSetDocumentTrack={setWorkspaceDocumentTrack}
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
        <strong>{busy ? runningTaskKeys.map(taskLabel).join(" + ") : "Idle"}</strong>
        <span>{latestLog || "Ready"}</span>
        <a href={SUPPORT_URL} onClick={openSupportLink} title={SUPPORT_MESSAGE}>☕ ko-fi.com/keljian</a>
      </footer>
    </main>
  );
}


createRoot(document.getElementById("root")).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);

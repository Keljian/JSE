/** Pure formatting, parsing, and classification helpers. No JSX. */
import { ANALYSIS_TOP_FIELDS, DOCUMENT_AI_PROVIDERS, PIPELINE, SCOPE_FIELD_KEYS, SUPPORT_URL } from "../lib/constants";

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

function openSupportLink(event) {
  event.preventDefault();
  window.jobAssistant.openExternal(SUPPORT_URL);
}

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

// Flags arrive either as the parsed record (workspace) or as the denormalised
// type list the board query returns. Read both so callers do not have to care.
const jobFlagsOf = (job) => {
  const record = job?.job_flags;
  if (record && Array.isArray(record.flags)) return record.flags;
  if (typeof job?.job_flags_json === "string" && job.job_flags_json) {
    try {
      const parsed = JSON.parse(job.job_flags_json);
      if (Array.isArray(parsed?.flags)) return parsed.flags;
    } catch {
      return [];
    }
  }
  return [];
};

const jobFlagTypesOf = (job) => {
  const types = String(job?.job_flags_types || "").split(",").filter(Boolean);
  return types.length ? types : jobFlagsOf(job).map((flag) => flag.type).filter(Boolean);
};

function displayFileName(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.split(/[\\/]/).filter(Boolean).pop() || text;
}

function isWordDocumentPath(value) {
  return /\.docx?$/i.test(String(value || "").trim());
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

function isWeakCompanyName(value) {
  const key = String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  if (!key) return true;
  const weak = new Set(["a", "about", "all", "and", "are", "as", "at", "business", "candidate", "client", "company", "confidential", "employer", "for", "group", "hiring", "if", "in", "it", "join", "new", "our", "people", "position", "role", "team", "the", "their", "this", "we", "who", "with", "work", "you", "your", "unknown"]);
  const words = key.split(" ");
  return weak.has(key) || weak.has(words[0]) || (words.length === 1 && words[0].length < 4);
}

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

function countBy(items, key, fallback = "unknown") {
  return (items || []).reduce((counts, item) => {
    const value = String(item?.[key] || fallback).toLowerCase();
    counts[value] = (counts[value] || 0) + 1;
    return counts;
  }, {});
}

export { normalizeStage, canMoveToInterested, openSupportLink, documentAiLabel, todayPlus, formatDate, closingDateSourceMeta, formatBytes, toErrorMessage, scoreClass, primaryScore, jobFlagsOf, jobFlagTypesOf, displayFileName, isWordDocumentPath, parseJsonObject, isWeakCompanyName, parseAnalysisReport, actionMeta, gateDecisionMeta, hasCompanyResearch, toDateTimeInputValue, countBy };

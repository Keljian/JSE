/** Small reusable pieces: modal shell, scores, drop zones, the error boundary. */
import React, { useEffect, useRef, useState } from "react";
import { Check, Download, ExternalLink, FileText, FolderOpen, AlertTriangle, Loader2, Maximize2, Minimize2, RefreshCw, Trash2, X } from "lucide-react";
import { COMPOSITE_FRAGMENT_WEIGHT, COMPOSITE_MATCH_WEIGHT } from "../lib/constants";
import { closingDateSourceMeta, displayFileName, isWordDocumentPath, scoreClass } from "../lib/format";

function ClosingDateSourceBadge({ source }) {
  const meta = closingDateSourceMeta(source);
  return <span className={`source-badge ${meta.className}`} title={meta.title}>{meta.label}</span>;
}

function Score({ value, label = "" }) {
  const score = Number(value || 0);
  if (!score) return <span className="muted">Unscored</span>;
  return <span className={`score ${scoreClass(score)}`}>{label ? `${label} ` : ""}{score}%</span>;
}

function ScoreStack({ job, compact = false }) {
  const match = Number(job?.match_score || 0);
  const fragment = Number(job?.fragment_score || 0);
  const hasFragment = job?.fragment_score !== null && job?.fragment_score !== undefined;
  const composite = hasFragment
    ? Math.round((COMPOSITE_MATCH_WEIGHT * match) + (COMPOSITE_FRAGMENT_WEIGHT * fragment))
    : match;
  const hasComposite = hasFragment && composite > 0;
  const primary = hasComposite ? composite : match;
  if (!primary && !fragment) return <span className="muted">Unscored</span>;
  const weightLabel = `${Math.round(COMPOSITE_MATCH_WEIGHT * 100)}% match (${match}) + ${Math.round(COMPOSITE_FRAGMENT_WEIGHT * 100)}% fragment alignment (${fragment})`;
  return (
    <span className={`score-stack ${compact ? "compact" : ""}`} title={hasFragment ? `Composite = ${weightLabel}` : "Final match score"}>
      {primary ? <Score value={primary} label={hasComposite ? "Comp" : "Match"} /> : null}
      {!compact && hasComposite && match ? <span className="score-chip">Match {match}%</span> : null}
      {fragment ? <span className={`score-chip ${scoreClass(fragment)}`}>Frag {fragment}%</span> : null}
    </span>
  );
}

function Modal({ title, children, onClose, wide = false, closeDisabled = false, expandable = false }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="modal-backdrop" role="presentation">
      <section className={`modal ${wide ? "wide-modal" : ""} ${expanded ? "expanded-modal" : ""}`}>
        <header className="modal-head">
          <h2>{title}</h2>
          <div className="modal-head-actions">
            {expandable ? (
              <button
                className="icon secondary"
                onClick={() => setExpanded((value) => !value)}
                aria-label={expanded ? "Restore window" : "Expand window"}
                title={expanded ? "Restore window" : "Expand window"}
              >
                {expanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
              </button>
            ) : null}
            <button className="icon secondary" disabled={closeDisabled} onClick={onClose} aria-label="Close"><X size={18} /></button>
          </div>
        </header>
        {children}
      </section>
    </div>
  );
}

function DialogModal({ dialog, onClose }) {
  const [text, setText] = useState(dialog.defaultValue || "");
  useEffect(() => setText(dialog.defaultValue || ""), [dialog]);
  const confirm = () => onClose(dialog.kind === "prompt" ? text.trim() : true);
  const cancel = () => onClose(dialog.kind === "prompt" ? null : dialog.kind === "notice");
  return (
    <Modal title={dialog.title || "Confirm"} onClose={cancel}>
      {dialog.message ? <div className="modal-copy">{dialog.message}</div> : null}
      {dialog.kind === "prompt" ? (
        <label className="field">
          <span>{dialog.label || "Value"}</span>
          <input
            autoFocus
            value={text}
            placeholder={dialog.placeholder || ""}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter") confirm(); }}
          />
        </label>
      ) : null}
      <footer className="modal-actions">
        {dialog.kind !== "notice" ? <button className="secondary" onClick={cancel}>Cancel</button> : null}
        <button autoFocus={dialog.kind !== "prompt"} className={dialog.danger ? "danger" : ""} onClick={confirm}>
          {dialog.danger ? <Trash2 size={16} /> : dialog.warning ? <AlertTriangle size={16} /> : <Check size={16} />} {dialog.confirmLabel || "OK"}
        </button>
      </footer>
    </Modal>
  );
}

function DropZone({ label, value, text, onDrop, onView, onDownload, onReveal, onConvertPdf }) {
  const inputRef = useRef(null);
  const [converting, setConverting] = useState(false);
  const uploadFile = (file) => {
    if (file) onDrop(file);
  };
  const fileName = displayFileName(value);
  const canConvertPdf = Boolean(value && onConvertPdf && isWordDocumentPath(value));
  const browseForFile = async () => {
    if (!window.jobAssistant.chooseDocument) {
      inputRef.current?.click();
      return;
    }
    const path = await window.jobAssistant.chooseDocument(`Select ${label.toLowerCase()}`);
    if (path) uploadFile({ name: displayFileName(path), path });
  };
  const convertPdf = async () => {
    if (!canConvertPdf || converting) return;
    setConverting(true);
    try {
      await onConvertPdf();
    } finally {
      setConverting(false);
    }
  };

  return (
    <div
      className="drop-zone"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        uploadFile(event.dataTransfer.files?.[0]);
      }}
    >
      <div>
        <strong>{label}</strong>
        <span title={value || ""}>{fileName || (text ? "Extracted document text attached" : "Drop .docx, .doc, .pdf, .txt, or .md here")}</span>
      </div>
      <div className="drop-zone-actions">
        <input
          ref={inputRef}
          className="file-input"
          type="file"
          accept=".docx,.doc,.pdf,.txt,.md"
          onChange={(event) => {
            uploadFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
        <button className="secondary" onClick={browseForFile}><FolderOpen size={16} /> Upload</button>
        <button className="secondary" disabled={!text} onClick={onView}><FileText size={16} /> Open text</button>
        <button className="secondary" disabled={!canConvertPdf || converting} onClick={convertPdf}>{converting ? <Loader2 className="spin" size={16} /> : <FileText size={16} />} PDF</button>
        <button className="secondary" disabled={!value} onClick={onDownload}><Download size={16} /> Download</button>
        <button className="secondary" disabled={!value} onClick={onReveal}><ExternalLink size={16} /> Show</button>
      </div>
    </div>
  );
}

function DocumentTextModal({ title, text, onClose }) {
  return (
    <Modal title={title} onClose={onClose} wide>
      <pre className="document-text">{text || "No extracted text available."}</pre>
    </Modal>
  );
}

function LinkedText({ text }) {
  const parts = String(text || "").split(/(mailto:[^\s),;]+|tel:\+?[\d\s().-]+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/gi);
  return (
    <>
      {parts.map((part, index) => {
        if (/^mailto:/i.test(part)) {
          return <a key={`${part}-${index}`} href={part} onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(part); }}>{part}</a>;
        }
        if (/^tel:/i.test(part)) {
          return <a key={`${part}-${index}`} href={part} onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(part); }}>{part}</a>;
        }
        if (/^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(part)) {
          return <a key={`${part}-${index}`} href={`mailto:${part}`} onClick={(event) => { event.preventDefault(); window.jobAssistant.openExternal(`mailto:${part}`); }}>{part}</a>;
        }
        return <React.Fragment key={`${index}-${part.slice(0, 8)}`}>{part}</React.Fragment>;
      })}
    </>
  );
}

function ValueList({ values }) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  if (!list.length) return <p className="empty-inline">No entries yet.</p>;
  return <ul className="compact-list">{list.map((item, index) => <li key={`${index}-${String(item).slice(0, 18)}`}>{String(item)}</li>)}</ul>;
}

function StatDelta({ current, previous }) {
  const delta = Number(current || 0) - Number(previous || 0);
  if (!delta) return <small className="stat-delta">level with prior period</small>;
  return (
    <small className={`stat-delta ${delta > 0 ? "up" : "down"}`}>
      {delta > 0 ? "+" : ""}{delta} vs prior period
    </small>
  );
}

function StatBars({ items, labelKey, countKey }) {
  const max = Math.max(1, ...(items || []).map((item) => Number(item[countKey] || 0)));
  return (
    <div className="stat-bars">
      {(items || []).map((item) => (
        <div key={item[labelKey]} className="stat-bar-row">
          <span className="stat-bar-label">{item[labelKey]}</span>
          <span className="stat-bar-track">
            <span className="stat-bar-fill" style={{ width: `${(Number(item[countKey] || 0) / max) * 100}%` }} />
          </span>
          <strong>{item[countKey]}</strong>
        </div>
      ))}
    </div>
  );
}

// Model picker that prefers an auto-discovered dropdown but keeps a custom-entry
// escape hatch (and preserves any value not in the discovered list).
function ModelSelect({ value, options, loading, placeholder, onChange, onRefresh }) {
  const [customMode, setCustomMode] = useState(false);
  const list = options || [];
  const merged = value && !list.includes(value) ? [value, ...list] : list;
  if (customMode) {
    return (
      <div className="model-select">
        <input value={value || ""} placeholder={placeholder || "Model name"} onChange={(event) => onChange(event.target.value)} />
        <div className="model-select-actions">
          <button type="button" className="secondary ai-test-button" onClick={() => setCustomMode(false)}>Pick from list</button>
        </div>
      </div>
    );
  }
  return (
    <div className="model-select">
      <select value={value || ""} onChange={(event) => { if (event.target.value === "__custom__") setCustomMode(true); else onChange(event.target.value); }}>
        <option value="">{loading ? "Loading models…" : merged.length ? "Provider default" : "Provider default (load to list)"}</option>
        {merged.map((model) => <option key={model} value={model}>{model}</option>)}
        <option value="__custom__">Custom…</option>
      </select>
      <div className="model-select-actions">
        {onRefresh ? <button type="button" className="secondary ai-test-button" onClick={onRefresh} disabled={loading}>{loading ? <Loader2 className="spin" size={12} /> : <RefreshCw size={12} />} Reload models</button> : null}
      </div>
    </div>
  );
}

export { ClosingDateSourceBadge, Score, ScoreStack, Modal, DialogModal, DropZone, DocumentTextModal, LinkedText, ValueList, StatDelta, StatBars, ModelSelect };

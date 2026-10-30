import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ClipboardCopy,
  FileText,
  FolderOpen,
  Gavel,
  Landmark,
  ListChecks,
  Loader2,
  Plus,
  Printer,
  Search,
  Swords,
  Upload,
  X,
} from "lucide-react";

import { api } from "../api/client.js";
import { saveResponseAsFile } from "./downloadFile.js";
import { useModalDismiss } from "../hooks/useModalDismiss.js";
import {
  CASE_CONTEXT_CHOICES,
  COURT_RULE_MODES,
  JURISDICTION_MODES,
  RUN_POLL_MS,
  RUN_POLL_TIMEOUT_MS,
  checkStatusSummary,
  reviewCheckGroups,
  availableFilters,
  canStartRun,
  caseOptions,
  challengeSummary,
  challengeReview,
  challengePreview,
  challengeGroups,
  priorityForChallenge,
  overviewLenses,
  checklistItemsFromText,
  checklistItemsToText,
  cleanJurisdictionDetail,
  complianceGroups,
  complianceSummary,
  copyTextForChallenge,
  courtSummary,
  coverageSummary,
  defaultFilter,
  effectiveSelection,
  elementState,
  emptyStateMessage,
  evidenceCount,
  exhibitSummary,
  groupChecks,
  isRunFinished,
  materialsByOrigin,
  matterFilterOptions,
  replaceChallenge,
  rerunSummary,
  revisionTargets,
  runActionsDisabled,
  runProgressFraction,
  runProgressLabel,
  runView,
  sessionStatus,
  sessionSubtitle,
  severityTone,
  shortTitle,
  skippedChecksSummary,
  sortSessions,
  targetLabel,
  toggleCheck,
  truncationNotice,
  updatePlanItem,
  usesMunicipality,
} from "./argumentGym.js";
import { PanelHeading } from "./PanelHeading.jsx";
import { InfoHint } from "./InfoHint.jsx";

function RunProgress({ run }) {
  return (
    <section className="gym-progress" role="status" aria-live="polite">
      <Loader2 className="spin" size={18} />
      <div>
        <strong>{runProgressLabel(run)}</strong>
        <p className="muted">
          A full pass takes a few minutes. You can leave this open, or come back to it from Open session — the run
          keeps going on the server.
        </p>
        <div className="gym-progress-track">
          <div className="gym-progress-bar" style={{ width: `${Math.round(runProgressFraction(run) * 100)}%` }} />
        </div>
      </div>
    </section>
  );
}

function RunFailed({ run, busy, onRetry, onBack }) {
  return (
    <section className="gym-run-failed">
      <h4>
        <AlertTriangle size={18} /> This run did not finish
      </h4>
      <p>{run.error || "The run failed before it produced any challenges."}</p>
      <p className="muted">Nothing was written to your document.</p>
      <div className="button-row compact">
        <button className="btn btn-primary" type="button" disabled={busy} onClick={onRetry}>
          {busy ? <Loader2 className="spin" size={16} /> : <Swords size={16} />} Try again
        </button>
        <button className="btn btn-light" type="button" onClick={onBack}>
          Back to setup
        </button>
      </div>
    </section>
  );
}

function SessionBrowser({ open, sessions, matters, matterId, onMatterChange, query, onQueryChange, activeId, onOpen, onNew, onClose, busy }) {
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: open });
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation">
      <aside className="editor-modal gym-sessions" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Sessions">
      <div className="modal-heading">
        <h4>Open a session</h4>
        <div className="button-row compact">
          <button className="btn btn-primary" type="button" onClick={onNew} disabled={busy}>
            <Plus size={16} /> New session
          </button>
          <button className="btn btn-light icon-button" type="button" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
      </div>
      <label className="form-label">
        Case
        <select className="form-select" value={matterId} onChange={(event) => onMatterChange(event.target.value)}>
          {matterFilterOptions(matters).map((option) => (
            <option key={option.id || "all"} value={option.id}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label className="form-label" htmlFor="gym-session-search">Find a session</label>
      <input
        className="form-control"
        type="search"
        id="gym-session-search"
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
      />
      {sessions.length === 0 ? (
        <p className="muted">No sessions yet. Start one with New.</p>
      ) : (
        <ul className="gym-session-list">
          {sortSessions(sessions).map((session) => (
            <li key={session.id}>
              <button
                type="button"
                className={`gym-session${session.id === activeId ? " active" : ""}`}
                onClick={() => onOpen(session)}
              >
                <strong title={session.title}>{shortTitle(session.title, 60)}</strong>
                <span className="muted" title={sessionSubtitle(session)}>{shortTitle(sessionSubtitle(session), 70)}</span>
                <span className="muted">{sessionStatus(session)}</span>
                {session.verdict && <span className="gym-session-verdict">{session.verdict}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      </aside>
    </div>
  );
}

function JurisdictionControls({ workspace, courts, courtTypes, detection, busy, onChange, onDetect }) {
  const detail = workspace?.jurisdictionDetail || {};
  const manual = workspace?.jurisdictionMode === "manual";
  const courtType = detail.courtType || workspace?.court?.courtType || "";
  const municipalityApplies = usesMunicipality(courtType, courtTypes);

  const patchDetail = (patch) =>
    onChange({
      jurisdictionMode: "manual",
      jurisdictionDetail: cleanJurisdictionDetail({ ...detail, ...patch }, courtTypes),
    });

  return (
    <div className="gym-jurisdiction">
      <div className="gym-jurisdiction-body">
        <div className="gym-mode-row">
          {JURISDICTION_MODES.map((mode) => (
            <label key={mode.id} className={`gym-mode ${workspace?.jurisdictionMode === mode.id ? "active" : ""}`}>
              <input
                type="radio"
                name="gym-jurisdiction-mode"
                checked={workspace?.jurisdictionMode === mode.id}
                onChange={() => onChange({ jurisdictionMode: mode.id })}
              />
              {mode.label}
            </label>
          ))}
          <button className="btn btn-light" type="button" onClick={onDetect} disabled={busy}>
            <Search size={16} /> What would detection pick?
          </button>
        </div>

        {detection && (
          <p className="muted gym-detection">
            {detection.detected
              ? `Detection picks ${detection.court?.label}. ${detection.reason}`
              : detection.reason}
          </p>
        )}

        {manual && (
          <div className="gym-jurisdiction-fields">
            <label className="form-label">
              Court type
              <select
                className="form-select"
                value={courtType}
                onChange={(event) => patchDetail({ courtType: event.target.value })}
              >
                <option value="">Choose…</option>
                {courtTypes.map((type) => (
                  <option key={type.id} value={type.id}>
                    {type.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-label">
              State
              <input className="form-control" value={detail.state || ""} onChange={(event) => patchDetail({ state: event.target.value })} />
            </label>
            <label className="form-label">
              County
              <input className="form-control" value={detail.county || ""} onChange={(event) => patchDetail({ county: event.target.value })} />
            </label>
            {municipalityApplies ? (
              <label className="form-label">
                Municipality
                <input
                  className="form-control"
                  value={detail.municipality || ""}
                  onChange={(event) => patchDetail({ municipality: event.target.value })}
                />
              </label>
            ) : (
              <label className="form-label">
                Division or district
                <input
                  className="form-control"
                  value={detail.division || ""}
                  onChange={(event) => patchDetail({ division: event.target.value })}
                />
              </label>
            )}
          </div>
        )}

        <div className="gym-mode-row">
          {COURT_RULE_MODES.map((mode) => (
            <label key={mode.id} className={`gym-mode ${workspace?.courtRuleMode === mode.id ? "active" : ""}`}>
              <input
                type="radio"
                name="gym-rule-mode"
                checked={workspace?.courtRuleMode === mode.id}
                onChange={() => onChange({ courtRuleMode: mode.id })}
              />
              {mode.label}
            </label>
          ))}
        </div>

        {workspace?.courtRuleMode === "manual" && (
          <label className="form-label">
            Court whose rules apply
            <select
              className="form-select"
              value={workspace?.court?.slug || ""}
              onChange={(event) => onChange({ courtSlug: event.target.value })}
            >
              <option value="">Choose a court…</option>
              {courts.map((court) => (
                <option key={court.slug} value={court.slug}>
                  {court.label}
                  {court.place ? ` — ${court.place}` : ""}
                  {court.verification === "verified" ? "" : " (unverified)"}
                </option>
              ))}
            </select>
          </label>
        )}
        <p className="muted">{courtSummary({ court: workspace?.court, detection: { mode: workspace?.courtRuleMode } })}</p>
      </div>
    </div>
  );
}

function FindingLines({ findings }) {
  if (!findings.length) return <p className="muted">Nothing found.</p>;
  return (
    <ul className="gym-finding-lines">
      {findings.map((finding) => (
        <li key={finding.findingId} className={`tone-${finding.severity}`}>
          <span className="gym-finding-target">{finding.target}</span>
          {finding.message}
        </li>
      ))}
    </ul>
  );
}

function ChecklistEditor({ checklists, activeId, busy, onSave, onDelete, onSelect }) {
  const active = checklists.find((item) => String(item.id) === String(activeId)) || null;
  const [title, setTitle] = useState(active?.title || "");
  const [text, setText] = useState(checklistItemsToText(active?.items || []));

  useEffect(() => {
    setTitle(active?.title || "");
    setText(checklistItemsToText(active?.items || []));
  }, [active?.id]);

  return (
    <div className="gym-checklist-editor">
      <div className="gym-checks-body">
        <p className="muted">
          One review question per line. An item can look things up — the case record, an authority, a passage of the
          brief — and the run reports what it read before answering.
        </p>
        <label className="form-label">
          Checklist
          <select className="form-select" value={activeId || ""} onChange={(event) => onSelect(event.target.value)}>
            <option value="">New checklist…</option>
            {checklists.map((checklist) => (
              <option key={checklist.id} value={checklist.id}>
                {checklist.title}
              </option>
            ))}
          </select>
        </label>
        <label className="form-label">
          Name
          <input className="form-control" value={title} onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label className="form-label">
          Items
          <span id="gym-checklist-help" className="muted">For example: Every date in the statement of facts appears in a document in the file.</span>
          <textarea
            className="form-control"
            rows={6}
            aria-describedby="gym-checklist-help"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
        </label>
        <div className="button-row compact">
          <button
            className="btn btn-primary"
            type="button"
            disabled={busy || !title.trim()}
            onClick={() => onSave({ id: active?.id, title: title.trim(), items: checklistItemsFromText(text) })}
          >
            {busy ? <Loader2 className="spin" size={16} /> : <ListChecks size={16} />} {active ? "Save" : "Create"}
          </button>
          {active && (
            <button className="btn btn-light" type="button" disabled={busy} onClick={() => onDelete(active.id)}>
              <X size={16} /> Delete
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function CheckSelector({ catalog, selected, checklists, checklistId, busy, onToggle, onChecklist, onManageChecklists, onManagePassive }) {
  return (
    <div className="gym-checks">
      <div className="gym-checks-body">
        <p className="muted">Pick which tests you want to run.</p>
        {groupChecks(catalog).map((group) => (
          <fieldset key={group.id} className="gym-check-group">
            <legend>{group.label}</legend>
            {group.checks.map((check) => (
              <label key={check.id} className="gym-check">
                <input
                  type="checkbox"
                  checked={selected.includes(check.id)}
                  disabled={busy}
                  onChange={() => onToggle(check.id)}
                />
                <span className="gym-check-line">
                  <strong>{check.label}</strong>
                  {check.kind === "model" && <span className="gym-check-kind">AI</span>}
                  <InfoHint text={check.description} label={`What "${check.label}" checks`} />
                  {check.id === "custom_checklist" && (
                    <button
                      className="btn btn-light btn-inline"
                      type="button"
                      onClick={(event) => {
                        event.preventDefault();
                        onManageChecklists();
                      }}
                    >
                      Manage checklists
                    </button>
                  )}
                  {check.id === "passive_voice" && (
                    <button
                      className="btn btn-light btn-inline"
                      type="button"
                      onClick={(event) => {
                        event.preventDefault();
                        onManagePassive();
                      }}
                    >
                      Manage passive phrases
                    </button>
                  )}
                </span>
              </label>
            ))}
          </fieldset>
        ))}

        {selected.includes("custom_checklist") && !checklistId && (
          <p className="gym-needs-attention">
            Attach a checklist below, or this check will not run.
          </p>
        )}
        {selected.includes("custom_checklist") && (
          <label className="form-label gym-inline-field">
            Checklist to apply
            <select className="form-select" value={checklistId || ""} onChange={(event) => onChecklist(event.target.value)}>
              <option value="">Choose a checklist…</option>
              {checklists.map((checklist) => (
                <option key={checklist.id} value={checklist.id}>
                  {checklist.title} ({checklist.items.length} item{checklist.items.length === 1 ? "" : "s"})
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
    </div>
  );
}

function ChecklistModal({ open, checklists, activeId, busy, onClose, onSelect, onSave, onDelete }) {
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: open });
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="editor-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Checklists">
        <div className="modal-heading">
          <h4>Custom checklists</h4>
          <button className="btn btn-light icon-button" type="button" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <ChecklistEditor
          checklists={checklists}
          activeId={activeId}
          busy={busy}
          onSelect={onSelect}
          onSave={onSave}
          onDelete={onDelete}
        />
      </div>
    </div>
  );
}

function PassivePhraseModal({ open, phrases, busy, onClose, onSave }) {
  const dialogRef = useRef(null);
  const [text, setText] = useState((phrases || []).join("\n"));
  useModalDismiss(dialogRef, onClose, { active: open });
  useEffect(() => setText((phrases || []).join("\n")), [open, phrases]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="editor-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Passive phrases">
        <div className="modal-heading">
          <h4>Passive phrases to allow</h4>
          <button className="btn btn-light icon-button" type="button" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <p className="muted">
          One per line. The passive-voice check stays quiet about these — "service was perfected" is the register a
          court expects, not a mistake.
        </p>
        <textarea className="form-control" rows={8} value={text} onChange={(event) => setText(event.target.value)} />
        <div className="button-row step-actions">
          <button className="btn btn-light" type="button" onClick={onClose}>Cancel</button>
          <button
            className="btn btn-primary"
            type="button"
            disabled={busy}
            onClick={() => onSave(text.split("\n").map((line) => line.trim()).filter(Boolean))}
          >
            {busy ? <Loader2 className="spin" size={16} /> : null} Save
          </button>
        </div>
      </div>
    </div>
  );
}

function AuditSummary({ run, catalog, onOpenArtifact, actionsDisabled }) {
  const skipped = skippedChecksSummary(run.checksRun || []);
  const { primary: checkGroups, style, styleChecks, styleCount } = reviewCheckGroups(run, catalog);
  const compliance = complianceGroups(run.compliance || {});
  const rules = run.ruleAudit || [];
  // Findings first; the rules that were satisfied are a list, not a report.
  const unmetRules = rules.filter((audit) => audit.unmetCount > 0);
  const metRules = rules.filter((audit) => !audit.unmetCount);
  const checklist = run.checklistResults?.results || [];

  return (
    <section className="gym-audit" aria-label="Filing and document review">
      <h4>Filing and document review</h4>
      <p className="muted">{checkStatusSummary(run.checksRun) || "Check execution was not recorded for this run."}</p>
      {!compliance.checked && <p className="muted">{compliance.reason || "Filing-format rules were not applied."}</p>}
      {compliance.checked && compliance.total === 0 && <p className="muted">Filing format: no findings from the rules checked.</p>}

      {unmetRules.length > 0 && (
        <details className="gym-audit-detail" open>
          <summary>Unmet elements</summary>
          {unmetRules.map((audit) => (
            <div key={audit.slug} className="gym-audit-rule">
              <span className="gym-context-label">{audit.citation}</span>
              <ul className="gym-finding-lines">
                {audit.elements
                  .filter((element) => element.unmet)
                  .map((element) => (
                    <li key={element.id} className="tone-warning">
                      <span className="gym-finding-target">{element.label}</span>
                      {elementState(element)}
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </details>
      )}

      {metRules.length > 0 && (
        <details className="gym-audit-detail">
          <summary>Other rules reviewed ({metRules.length})</summary>
          <ul className="gym-plain-list">
            {metRules.map((audit) => (
              <li key={audit.slug}>{audit.label}</li>
            ))}
          </ul>
        </details>
      )}

      {compliance.checked && compliance.total > 0 && (
        <details className="gym-audit-detail" open>
          <summary>Filing format · {compliance.total} to review</summary>
          <FindingLines findings={[...compliance.errors, ...compliance.warnings, ...compliance.unmeasured]} />
        </details>
      )}

      {checkGroups.some((group) => group.findings.length > 0) && (
        <details className="gym-audit-detail" open>
          <summary>Document checks</summary>
          {checkGroups
            .filter((group) => group.findings.length > 0)
            .map((group) => (
              <div key={group.id} className="gym-audit-rule">
                <span className="gym-context-label">{group.label}</span>
                <FindingLines findings={group.findings} />
              </div>
            ))}
        </details>
      )}

      {checklist.length > 0 && (
        <details className="gym-audit-detail" open={checklist.some((item) => item.outcome !== "pass")}>
          <summary>Your checklist</summary>
          <ul className="gym-finding-lines">
            {checklist.map((item) => (
              <li key={item.itemId} className={item.outcome === "fail" ? "tone-warning" : ""}>
                <span className="gym-finding-target">{item.item}</span>
                {item.finding}
              </li>
            ))}
          </ul>
        </details>
      )}

      {(style.length > 0 || styleChecks.length > 0) && (
        <details className="gym-audit-detail gym-style-review">
          <summary>Language and style <span className="gym-badge-inline">{styleCount} {styleCount === 1 ? "note" : "notes"} · optional review</span></summary>
          <p className="muted">{checkStatusSummary(styleChecks) || "Check execution was not recorded."} Opening these notes does not change which checks run.</p>
          {style.map((group) => (
            <div key={group.id} className="gym-audit-rule">
              <h5>{group.label}</h5>
              {group.summary && <p className="muted">{group.summary}</p>}
              {group.findings.length > 0 && <FindingLines findings={group.findings} />}
              {group.findings.length === 0 && (
                <p className="muted">{group.errors.length ? "Errors appear in Document checks above." : styleChecks.some((check) => check.id === group.id && check.status === "on") ? "No findings from this check." : "No findings recorded; see check status below."}</p>
              )}
            </div>
          ))}
          {styleChecks.filter((check) => check.status !== "on").map((check) => (
            <p key={check.id} className="muted">{check.label} — {check.status === "off" ? "off" : `could not run: ${check.reason || "No reason recorded"}`}</p>
          ))}
        </details>
      )}

      {skipped && (
        <details className="gym-audit-detail gym-audit-skipped">
          <summary>
            Not run <span className="gym-badge-inline">{skipped.label}</span>
          </summary>
          <p className="muted">A check that did not run is not a pass.</p>
          <ul className="gym-plain-list">
            {skipped.unavailable.map((entry) => (
              <li key={entry.id}>
                {entry.label} — {entry.reason}
              </li>
            ))}
            {skipped.off.map((entry) => (
              <li key={entry.id} className="muted">
                {entry.label} — off
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="gym-audit-actions">
        <button className="text-link-button" type="button" disabled={actionsDisabled} onClick={() => onOpenArtifact("prep_sheet")}>
          Opposition prep sheet
        </button>
        <button className="text-link-button" type="button" disabled={actionsDisabled} onClick={() => onOpenArtifact("report")}>
          Stress-test report
        </button>
      </div>
    </section>
  );
}

function SourceList({ sources = [], emptyLabel }) {
  if (!sources.length) return <p className="muted">{emptyLabel}</p>;
  return (
    <ul className="gym-source-list">
      {sources.map((source, index) => (
        <li key={`${source.externalId || source.title}-${index}`}>
          <strong>{source.citation || source.title}</strong>
          {source.sourceLabel && <span className="muted"> · {source.sourceLabel}</span>}
          {source.snippet && <p className="gym-source-snippet">{source.snippet}</p>}
          {source.url && (
            <a href={source.url} target="_blank" rel="noreferrer">
              Open source
            </a>
          )}
        </li>
      ))}
    </ul>
  );
}

function RecordList({ sources = [] }) {
  if (!sources.length) return <p className="muted">No case material was matched to this challenge.</p>;
  return (
    <ul className="gym-source-list">
      {sources.map((source) => (
        <li key={source.materialId}>
          <strong>{source.title}</strong>
          {source.status && <span className="muted"> · {source.status.replace("_", " ")}</span>}
          {source.quote && <p className="gym-source-snippet">“{source.quote}”</p>}
        </li>
      ))}
    </ul>
  );
}

function EvidenceModal({ challenge, onClose }) {
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: Boolean(challenge) });
  if (!challenge) return null;
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="editor-modal gym-evidence-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Evidence">
        <div className="modal-heading">
          <h4>Evidence</h4>
          <button className="btn btn-light icon-button" type="button" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <section>
          <h5>Legal authority</h5>
          <SourceList sources={challenge.legalSources} emptyLabel="No retrieved authority backs this yet." />
        </section>
        <section>
          <h5>Case record</h5>
          <RecordList sources={challenge.recordSources} />
        </section>
        <section>
          <h5>Research coverage</h5>
          <p className="muted">{coverageSummary(challenge.researchCoverage)}</p>
        </section>
      </div>
    </div>
  );
}

function ChallengeCard({ challenge, busy, queued, onDisposition, onResearch, onQueueRevision, onCopy, onEvidence }) {
  const target = targetLabel(challenge.target);
  const evidence = evidenceCount(challenge);
  const response = challenge.recommendation;
  const remaining = challenge.researchCoverage?.remainingVulnerability;

  return (
    <article className={`gym-challenge tone-${severityTone(challenge)} gym-${challenge.disposition}`}>
      <header className="gym-challenge-header">
        <span className="gym-severity-dot" aria-hidden="true" />
        <span className="gym-category">{challenge.categoryLabel}</span>
        {target && <span className="gym-target">{target}</span>}
        <span className="gym-severity-word">{challenge.severity}</span>
        {challenge.disposition !== "open" && <span className="gym-disposition">{challenge.disposition}</span>}
        {challenge.recurring && <span className="gym-recurring">raised again</span>}
      </header>

      <p className="gym-category">{priorityForChallenge(challenge).label}</p>
      <h3 className="gym-finding-title">{challengePreview(challenge).title}</h3>
      {challenge.whyItMatters && <p><strong>Why it matters:</strong> {challenge.whyItMatters}</p>}
      {challenge.briefCurrentlySays && <blockquote className="gym-quote">{challenge.briefCurrentlySays}</blockquote>}
      <div className="gym-fix">
        <h4>What to do</h4>
        <p className="gym-response-text">{response || "No suggested response was recorded. Review the reasoning and sources before deciding how to address this."}</p>
      </div>
      {challenge.suggestedResponse && <details className="gym-fix">
        <summary>Suggested wording — review before using</summary>
        <p className="gym-response-text">{challenge.suggestedResponse}</p>
      </details>}
      {remaining && <p className="gym-remaining">Still exposed: {remaining}</p>}

      <div className="gym-card-actions">
        {challenge.target?.blockKey && (
          <button
            className={`btn ${queued ? "btn-light" : "btn-primary"}`}
            aria-pressed={queued}
            type="button"
            onClick={() => onQueueRevision(challenge)}
          >
            {queued ? <Check size={14} /> : <FileText size={14} />} {queued ? "Remove from revision plan" : "Add to revision plan"}
          </button>
        )}
        <button className="text-link-button" type="button" onClick={() => onCopy(challenge)}>
          <ClipboardCopy size={14} /> Copy
        </button>
        <button
          className="text-link-button"
          type="button"
          disabled={busy}
          onClick={() => onDisposition(challenge, challenge.disposition === "addressed" ? "open" : "addressed")}
        >
          <Check size={14} /> {challenge.disposition === "addressed" ? "Reopen" : "Addressed"}
        </button>
        <button
          className="text-link-button"
          type="button"
          disabled={busy}
          onClick={() => onDisposition(challenge, challenge.disposition === "dismissed" ? "open" : "dismissed")}
        >
          <X size={14} /> {challenge.disposition === "dismissed" ? "Undismiss" : "Dismiss"}
        </button>
      </div>
      <details className="gym-audit-detail">
        <summary>Why: what opposing counsel and the judge said</summary>
        <div className="gym-card-context">
          <p><span className="gym-context-label">Opposing counsel</span>{challenge.opponentArgument}</p>
          <p><span className="gym-context-label">Judge · {challenge.judgeVerdict || "assessment"}</span>{challenge.judgeAssessment || "No judge assessment was recorded."}</p>
          {challenge.whyItMatters && <p><span className="gym-context-label">Why it matters</span>{challenge.whyItMatters}</p>}
          {challenge.suggestedResponse && challenge.recommendation && <p><span className="gym-context-label">Coaching</span>{challenge.recommendation}</p>}
        </div>
      </details>
      <details className="gym-audit-detail">
        <summary>What this was based on{evidence > 0 ? ` · ${evidence} sources` : ""}</summary>
        <p className="muted">{coverageSummary(challenge.researchCoverage)}</p>
        <button className="text-link-button" type="button" onClick={() => onEvidence(challenge)}>Review evidence and coverage</button>
        <button className="text-link-button" type="button" disabled={busy} onClick={() => onResearch(challenge)}>
          {busy ? <Loader2 className="spin" size={14} /> : <Search size={14} />} Research
        </button>
      </details>
    </article>
  );
}

function CompactOverview({ run, catalog, active, onOpenChallenge }) {
  const [lensId, setLensId] = useState("elements");
  const lenses = overviewLenses(run, catalog);
  const lens = lenses.find((item) => item.id === lensId) || lenses[0];
  if (!active) return null;

  return (
    <section className="gym-overview" aria-label="Compact overview">
      <h3>Review at a glance</h3>
      <p className="muted">Choose a lens. These are results from this run; switching lenses does not run a check.</p>
      <div className="gym-lens-layout">
        <nav className="gym-lenses" aria-label="Review lenses">
          {lenses.map((item) => (
            <button key={item.id} type="button" className="gym-lens" aria-pressed={lens.id === item.id} aria-controls="gym-lens-content" onClick={() => setLensId(item.id)}>
              <strong>{item.label}</strong><span>{item.summary}</span><small>{item.status}</small>
            </button>
          ))}
        </nav>
        <section id="gym-lens-content" className="gym-lens-content" aria-label={lens.label}>
          <h4>{lens.question}</h4>
          <p className="muted">{lens.status}</p>
          {lens.challenges && (
            lens.challenges.length ? <ul className="gym-overview-findings">
              {lens.challenges.map((challenge) => <li key={challenge.id}>
                <button className="text-link-button" type="button" onClick={() => onOpenChallenge(challenge.id)}>{challengePreview(challenge).title}</button>
                <span className="muted">{targetLabel(challenge.target)} · {challenge.disposition || "open"} · {challenge.severity || "severity not recorded"}</span>
              </li>)}
            </ul> : <p>No challenges recorded for this lens. Check execution and coverage before drawing a conclusion.</p>
          )}
          {lens.matrix && (lens.matrix.length ? lens.matrix.map((rule) => (
            <section className="gym-matrix-rule" key={rule.slug}>
              <h5>{rule.label || rule.citation || rule.name}</h5>
              <p className="muted">{rule.verificationLabel}</p>
              {rule.requiresApplicabilityReview && <p>Possible rule match: confirm that this rule applies.</p>}
              {rule.source && <p className="muted">Source: {rule.source}</p>}
              {rule.sourceUrl && <a href={rule.sourceUrl} target="_blank" rel="noreferrer">Open rule source</a>}
              <div className="gym-table-scroll" tabIndex={0} role="region" aria-label={`${rule.label || rule.citation || rule.name} element matrix`}>
                <table className="gym-element-matrix">
                  <thead><tr><th scope="col">Element</th><th scope="col">Brief says it</th><th scope="col">Record supports it</th><th scope="col">Basis recorded</th></tr></thead>
                  <tbody>{rule.rows.map((row) => <tr key={row.id}>
                    <th scope="row">{row.label}{row.unmet && <small>Needs review</small>}</th>
                    <td>{row.pleadedLabel}</td><td>{row.supportedLabel}</td>
                    <td>
                      {row.explanation && <p>{row.explanation}</p>}
                      {row.quote && <blockquote>{row.quote}</blockquote>}
                      {row.materials.length > 0 && <ul>{row.materials.map((title, index) => <li key={index}>{title}</li>)}</ul>}
                      {!row.explanation && !row.quote && !row.materials.length && <span className="muted">No basis recorded.</span>}
                    </td>
                  </tr>)}</tbody>
                </table>
              </div>
              {rule.rows.length === 0 && <p>No element results recorded for this rule.</p>}
            </section>
          )) : <p>No element audit recorded. This does not establish that every element is pleaded or supported.</p>)}
          {lens.compliance && <>
            <h5>Filing rules</h5>
            <p className="muted">{!lens.compliance.checked ? lens.compliance.reason || "Filing-format rules were not applied." : lens.compliance.total ? `${lens.compliance.unmeasured.length} properties could not be measured.` : "No findings from the filing rules checked."}</p>
            {lens.compliance.total > 0 && <FindingLines findings={[...lens.compliance.errors, ...lens.compliance.warnings, ...lens.compliance.unmeasured]} />}
          </>}
          {lens.groups && (lens.groups.length ? lens.groups.map((group) => <section key={group.id} className="gym-audit-rule">
            <h5>{group.label}</h5>
            {group.summary && <p>{group.summary}</p>}
            {group.findings.length ? <FindingLines findings={group.findings} /> : <p className="muted">No findings recorded in this lens. Check execution is reported above.</p>}
            {lens.id === "style" && group.errors.length > 0 && <p>Errors from this check appear under Filing &amp; form.</p>}
          </section>) : <p>No document findings recorded in this lens.</p>)}
          {lens.checks && lens.checks.filter((check) => check.status !== "on").map((check) => <p key={check.id} className="muted">{check.label} — {check.status === "off" ? "off" : `could not run: ${check.reason || "No reason recorded"}`}</p>)}
          {lens.checklist && (lens.checklist.length ? <ul className="gym-finding-lines">{lens.checklist.map((item) => <li key={item.itemId}><strong>{item.item}</strong><p>{item.outcome?.replaceAll("_", " ") || "Outcome not recorded"}: {item.finding}</p></li>)}</ul> : <p>No checklist answers recorded.</p>)}
        </section>
      </div>
    </section>
  );
}

function ChallengeReview({ challenges, filter, requestedChallenge, busyChallengeId, queued, onDisposition, onResearch, onCopy, onEvidence, onQueueRevision }) {
  const [showAll, setShowAll] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const review = challengeReview(challenges, { filter, showAll, selectedId });
  const headingRef = useRef(null);
  const returnFocusId = useRef(null);
  const listRef = useRef(null);

  useEffect(() => {
    if (requestedChallenge) {
      setShowAll(true);
      setSelectedId(requestedChallenge.id);
    }
  }, [requestedChallenge]);

  useEffect(() => {
    if (review.selected) {
      returnFocusId.current = review.selected.id;
      headingRef.current?.focus();
    } else if (returnFocusId.current !== null) {
      const button = listRef.current?.querySelector(`[data-challenge-id="${returnFocusId.current}"]`);
      (button || headingRef.current)?.focus();
      returnFocusId.current = null;
      setSelectedId(null);
    }
  }, [review.selected?.id]);

  const openFinding = (id) => {
    returnFocusId.current = id;
    setSelectedId(id);
  };

  if (review.selected) return (
    <section className="gym-focused-review">
      <div className="gym-finding-nav" ref={headingRef} tabIndex={-1} aria-label={`Finding ${review.selectedIndex + 1} of ${review.shown.length} shown`}>
        <button className="text-link-button" type="button" onClick={() => setSelectedId(null)}>← Back to priorities</button>
        <span className="muted">{review.selectedIndex + 1} of {review.shown.length} shown{review.hidden ? ` · ${review.hidden} more in the list` : ""}</span>
      </div>
      <ChallengeCard
        key={review.selected.id}
        challenge={review.selected}
        busy={busyChallengeId === review.selected.id}
        queued={queued.includes(review.selected.id)}
        onDisposition={onDisposition} onResearch={onResearch} onCopy={onCopy}
        onEvidence={onEvidence} onQueueRevision={onQueueRevision}
      />
      <nav className="gym-finding-nav" aria-label="Findings">
        {review.previous && <button className="text-link-button" type="button" onClick={() => openFinding(review.previous.id)}>← Previous finding</button>}
        {review.next ? <button className="text-link-button" type="button" onClick={() => openFinding(review.next.id)}>Next finding →</button> : <button className="text-link-button" type="button" onClick={() => setSelectedId(null)}>Return to priorities</button>}
      </nav>
    </section>
  );

  return (
    <section className="gym-priorities" ref={listRef}>
      <h3 className="gym-priorities-title" ref={headingRef} tabIndex={-1}>{filter === "resolved" ? "Handled challenges" : filter === "all" ? "All challenges" : "What matters most"}</h3>
      {review.total > 0 ? <>
        <p className="muted">{filter === "open" ? "Start with the highest-ranked open challenges. Open one to decide what to do." : "Open a finding to review its response and disposition."}</p>
        {challengeGroups(review.shown).map((group) => <section key={group.id}>
        <h4>{group.label}</h4>
        <ol className="gym-priority-list">
          {group.challenges.map((challenge) => {
            const preview = challengePreview(challenge);
            return <li key={challenge.id}>
              <button type="button" className="gym-priority" data-challenge-id={challenge.id} onClick={() => openFinding(challenge.id)}>
                <strong>{preview.title}</strong>
                <span>{preview.target ? `${preview.target} · ` : ""}{preview.action}</span>
                {challenge.disposition && challenge.disposition !== "open" && <small>{challenge.disposition}</small>}
              </button>
            </li>;
          })}
        </ol>
        </section>)}
        {review.hidden > 0 && <button className="text-link-button gym-show-more" type="button" onClick={() => setShowAll(true)}>Show {review.hidden} more {review.hidden === 1 ? "challenge" : "challenges"}</button>}
        {showAll && review.total > 3 && <button className="text-link-button gym-show-more" type="button" onClick={() => setShowAll(false)}>Show the first three</button>}
      </> : <p>{emptyStateMessage(challenges, filter)}</p>}
    </section>
  );
}

function MaterialsConsidered({ run, materials, onToggle, busy }) {
  const considered = materialsByOrigin(run?.materials || []);
  return (
    <details className="gym-materials">
      <summary>
        <FolderOpen size={16} /> Materials considered ({(run?.materials || []).length})
      </summary>
      {(run?.materials || []).length === 0 ? (
        <p className="muted">This run tested the legal argument only. No case record was read.</p>
      ) : (
        <ul className="gym-source-list">
          {[...considered.matter_document, ...considered.upload].map((material) => (
            <li key={material.id}>
              <strong>{material.title}</strong>
              <span className="muted"> · {material.origin === "upload" ? "uploaded" : "case file"}</span>
              {material.reason && <p className="gym-source-snippet">{material.reason}</p>}
            </li>
          ))}
        </ul>
      )}
      {materials.length > 0 && (
        <>
          <h5>Available case materials</h5>
          <p className="muted">Uncheck a document to leave it out of the next run. Nothing is copied into the gym.</p>
          <ul className="gym-material-toggles">
            {materials.map((material) => (
              <li key={material.id}>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={!material.excluded}
                    disabled={busy}
                    onChange={(event) => onToggle(material, !event.target.checked)}
                  />
                  <span>{material.title}</span>
                  <span className="muted">{material.origin === "upload" ? "uploaded" : "case file"}</span>
                </label>
              </li>
            ))}
          </ul>
        </>
      )}
    </details>
  );
}

function ArtifactModal({ artifact, onClose }) {
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: Boolean(artifact) });
  if (!artifact) return null;
  const isPrepSheet = artifact.kind === "opposition_prep_sheet";
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="editor-modal gym-artifact-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-label={artifact.title}>
        <div className="modal-heading">
          <h4>{artifact.title}</h4>
          <div className="button-row compact">
            <button className="btn btn-light" type="button" onClick={() => window.print()}>
              <Printer size={16} /> Print
            </button>
            <button className="btn btn-light icon-button" type="button" onClick={onClose} aria-label="Close">
              <X size={16} />
            </button>
          </div>
        </div>
        {artifact.assessment && (
          <div className="gym-artifact-assessment">
            {artifact.verdict && <h5><Gavel size={16} /> {artifact.verdict}</h5>}
            <p>{artifact.assessment}</p>
          </div>
        )}
        {(artifact.summary || artifact.executiveSummary) && (
          <div className="gym-artifact-summary">{artifact.summary || artifact.executiveSummary}</div>
        )}
        {artifact.compliance?.checked && (
          <p className="muted">Filing format: {complianceSummary(artifact.compliance)}</p>
        )}
        {isPrepSheet ? (
          <div className="gym-table-scroll">
            <table className="table gym-prep-sheet">
              <thead>
                <tr>
                  <th>Likely opposition point</th>
                  <th>Strongest authority</th>
                  <th>Strongest adverse record</th>
                  <th>Current response</th>
                  <th>Suggested response</th>
                  <th>Remaining vulnerability</th>
                </tr>
              </thead>
              <tbody>
                {artifact.rows.map((row) => (
                  <tr key={row.challengeId}>
                    <td>
                      <strong>{row.category}</strong>
                      <p>{row.likelyOppositionPoint}</p>
                      <span className="muted">{targetLabel(row.target)}</span>
                    </td>
                    <td>{row.strongestAuthority?.citation || row.strongestAuthority?.title || "—"}</td>
                    <td>{row.strongestAdverseRecord?.title || "—"}</td>
                    <td>{row.currentResponse || "—"}</td>
                    <td>{row.suggestedResponse || "—"}</td>
                    <td>{row.remainingVulnerability || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="gym-report">
            <h5>Ranked vulnerabilities</h5>
            {artifact.vulnerabilities.length === 0 ? (
              <p className="muted">Nothing is still open.</p>
            ) : (
              <ol>
                {artifact.vulnerabilities.map((item) => (
                  <li key={item.challengeId}>
                    <strong>{item.category}</strong> ({item.severity}) — {item.finding || item.argument}
                    {item.recommendation && <p className="muted">{item.recommendation}</p>}
                  </li>
                ))}
              </ol>
            )}
            <h5>Challenges already handled well</h5>
            {artifact.handledWell.length === 0 ? (
              <p className="muted">None yet.</p>
            ) : (
              <ul>
                {artifact.handledWell.map((item) => (
                  <li key={item.challengeId}>
                    <strong>{item.category}</strong> — {item.finding || item.argument}
                  </li>
                ))}
              </ul>
            )}
            <h5>Unresolved research gaps</h5>
            {artifact.researchGaps.length === 0 && artifact.unresolvedNotes.length === 0 ? (
              <p className="muted">The adversarial research covered every challenge raised.</p>
            ) : (
              <ul>
                {artifact.researchGaps.map((gap) => (
                  <li key={gap}>{gap}</li>
                ))}
                {artifact.unresolvedNotes.map((note) => (
                  <li key={note.challengeId}>{note.note}</li>
                ))}
              </ul>
            )}
            <h5>Materials reviewed</h5>
            {artifact.materialsReviewed.length === 0 ? (
              <p className="muted">No case record was read.</p>
            ) : (
              <ul>
                {artifact.materialsReviewed.map((material) => (
                  <li key={material.id}>{material.title}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function GymRevisionModal({ plan, busy, onClose, onUpdateItem, onApply }) {
  const dialogRef = useRef(null);
  useModalDismiss(dialogRef, onClose, { active: Boolean(plan) });
  if (!plan) return null;
  const items = plan.plan || [];
  return (
    <div className="modal-backdrop" role="presentation">
      <div className="editor-modal revision-plan-modal" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Gym revision plan">
        <div className="modal-heading">
          <h4>
            <Swords size={16} /> Revision plan from challenges
          </h4>
          <button className="btn btn-light icon-button" type="button" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <p className="muted">
          Nothing is edited until you apply this. Each instruction regenerates one block and is recorded as a reviewable
          change on the document.
        </p>
        {items.length === 0 && <div className="empty-state compact"><p>No selected challenge targets a block of this document.</p></div>}
        <div className="revision-plan-list">
          {items.map((item) => (
            <div className={`revision-plan-item ${item.include ? "" : "excluded"}`} key={item.blockKey}>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={item.include}
                  onChange={(event) => onUpdateItem(item.blockKey, { include: event.target.checked })}
                />
                <strong>{item.sectionLabel}</strong>
                <span className="muted">
                  {item.challengeIds.length} challenge{item.challengeIds.length === 1 ? "" : "s"}
                </span>
              </label>
              <textarea
                className="form-control"
                rows={5}
                value={item.instruction}
                disabled={!item.include}
                onChange={(event) => onUpdateItem(item.blockKey, { instruction: event.target.value })}
              />
            </div>
          ))}
        </div>
        {plan.copyOnly?.length > 0 && (
          <div className="revision-plan-unscoped">
            <strong>Not tied to a block — copy these into the brief yourself:</strong>
            <ul>
              {plan.copyOnly.map((item) => (
                <li key={item.challengeId}>{item.instruction}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="button-row step-actions">
          <button className="btn btn-light" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" type="button" disabled={busy || items.length === 0} onClick={onApply}>
            {busy ? <Loader2 className="spin" size={16} /> : <Swords size={16} />} Apply to draft
          </button>
        </div>
      </div>
    </div>
  );
}

// `workspaceId` and `runId` are what the URL names; `onNavigate({ workspaceId,
// runId }, options)` moves it. A session or run that does not open says so --
// the latest session is never shown in its place.
export function ArgumentGymPanel({ matter = null, cases = [], focusRun = null, onFocusRunHandled = () => {}, workspaceId = null, runId = null, onNavigate = () => {} }) {
  const [workspace, setWorkspace] = useState(null);
  const [routeMissing, setRouteMissing] = useState("");
  const workspaceIdRef = useRef(null);
  workspaceIdRef.current = workspace?.id ?? null;
  const [brief, setBrief] = useState(null);
  const [caseContext, setCaseContext] = useState(matter ? "existing_case" : "none");
  const [caseMaterials, setCaseMaterials] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [selectedMatterId, setSelectedMatterId] = useState(matter?.id || "");
  const [run, setRun] = useState(null);
  const [filter, setFilter] = useState("open");
  const [reviewMode, setReviewMode] = useState("priorities");
  const reviewViewRef = useRef(null);
  const changeReviewMode = (mode) => {
    setReviewMode(mode);
    requestAnimationFrame(() => reviewViewRef.current?.scrollIntoView({ block: "start" }));
  };
  const [requestedChallenge, setRequestedChallenge] = useState(null);
  useEffect(() => {
    setReviewMode("priorities");
    setRequestedChallenge(null);
  }, [run?.id]);
  const [queued, setQueued] = useState([]);
  const [plan, setPlan] = useState(null);
  const [artifact, setArtifact] = useState(null);
  const [busy, setBusy] = useState(false);
  const [busyChallengeId, setBusyChallengeId] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [sessions, setSessions] = useState([]);
  const [sessionMatters, setSessionMatters] = useState([]);
  const [sessionMatterId, setSessionMatterId] = useState("");
  const [sessionQuery, setSessionQuery] = useState("");
  const [courts, setCourts] = useState([]);
  const [courtTypes, setCourtTypes] = useState([]);
  const [detection, setDetection] = useState(null);
  const [uploadNote, setUploadNote] = useState("");
  const [checkCatalog, setCheckCatalog] = useState([]);
  const [checkDefaults, setCheckDefaults] = useState([]);
  const [checklists, setChecklists] = useState([]);
  const [editingChecklistId, setEditingChecklistId] = useState("");
  const [checklistModalOpen, setChecklistModalOpen] = useState(false);
  const [passiveModalOpen, setPassiveModalOpen] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [evidenceChallenge, setEvidenceChallenge] = useState(null);

  const loadSessions = useCallback(async ({ matterId = sessionMatterId, query = sessionQuery } = {}) => {
    try {
      const response = await api.gymWorkspaces({ matterId, query });
      setSessions(response.workspaces || []);
      setSessionMatters(response.matters || []);
    } catch (err) {
      setError(err.message);
    }
  }, [sessionMatterId, sessionQuery]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const loadChecklists = useCallback(async () => {
    try {
      const response = await api.gymChecklists();
      setChecklists(response.checklists || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    loadChecklists();
    api
      .gymChecks()
      .then((response) => {
        setCheckCatalog(response.checks || []);
        setCheckDefaults(response.defaults || []);
      })
      .catch((err) => setError(err.message));
  }, [loadChecklists]);

  useEffect(() => {
    api
      .gymCourts()
      .then((response) => {
        setCourts(response.courts || []);
        setCourtTypes(response.courtTypes || []);
      })
      .catch((err) => setError(err.message));
  }, []);

  // The run is started, not awaited. It takes minutes -- longer than a worker
  // may hold a request -- so the server hands back a run to poll.
  const pollRun = useCallback(async (runId) => {
    const deadline = Date.now() + RUN_POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, RUN_POLL_MS));
      let latest;
      try {
        latest = (await api.gymRun(runId)).run;
      } catch (err) {
        // A single failed poll is a blip, not a failed run; keep waiting and let
        // the deadline decide.
        continue;
      }
      // The page has moved to another session: stop following this run.
      if (workspaceIdRef.current !== latest.workspaceId) return null;
      setRun(latest);
      if (isRunFinished(latest)) return latest;
    }
    setError("This run is taking longer than expected. It may still finish — reopen the session to check.");
    return null;
  }, []);

  useEffect(() => {
    if (!focusRun) return;
    setWorkspace(focusRun.workspace);
    workspaceIdRef.current = focusRun.workspace.id;
    setRun(focusRun.run);
    onNavigate({ workspaceId: focusRun.workspace.id, runId: focusRun.run.id }, { replace: true });
    setBrief({ id: focusRun.run.briefId, title: focusRun.run.briefTitle });
    setCaseContext("existing_case");
    setFilter("open");
    loadSessions();
    onFocusRunHandled();
    // A stress test started from the editor is handed over still running, so it
    // has to be followed here just like one started in this panel.
    if (!isRunFinished(focusRun.run)) pollRun(focusRun.run.id);
  }, [focusRun, onFocusRunHandled, loadSessions, pollRun]);

  const loadMaterials = useCallback(async (workspaceId) => {
    if (!workspaceId) return;
    try {
      const response = await api.gymMaterials(workspaceId);
      setMaterials(response.materials || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    if (workspace?.id) loadMaterials(workspace.id);
  }, [workspace?.id, loadMaterials]);

  const ensureWorkspace = useCallback(async () => {
    if (workspace) return workspace;
    const response = await api.createGymWorkspace({
      title: "Argument gym",
      matterId: caseContext === "existing_case" ? selectedMatterId : "",
      jurisdiction: matter?.jurisdiction || "",
    });
    setWorkspace(response.workspace);
    workspaceIdRef.current = response.workspace.id;
    // The new session has an address from the moment it exists.
    onNavigate({ workspaceId: response.workspace.id }, { replace: true });
    return response.workspace;
  }, [workspace, caseContext, selectedMatterId, matter, onNavigate]);

  const openSession = (session) => {
    setSessionsOpen(false);
    onNavigate({ workspaceId: session.id });
  };

  // Load the session the URL names (and the run, if it names one). Reading
  // only: nothing is started or rerun by opening a link.
  const loadSessionById = async (id, wantedRunId = null) => {
    setBusy(true);
    setError("");
    setNotice("");
    setRouteMissing("");
    try {
      let response;
      try {
        response = await api.gymWorkspace(id);
      } catch (err) {
        if (err.status === 404) {
          setRouteMissing("session");
          return;
        }
        throw err;
      }
      workspaceIdRef.current = response.workspace.id;
      setWorkspace(response.workspace);
      setRun(response.latestRun);
      setDetection(null);
      setQueued([]);
      setFilter(defaultFilter(response.latestRun?.challenges || []));
      const briefDocument = (response.workspace.documents || []).find((item) => item.role === "brief_under_test");
      setBrief(briefDocument || null);
      setCaseMaterials((response.workspace.documents || []).filter((item) => item.role === "case_record"));
      setCaseContext(response.workspace.matterId ? "existing_case" : "none");
      setSelectedMatterId(response.workspace.matterId || "");
      if (wantedRunId) await loadRunById(response.workspace.id, wantedRunId);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const loadRunById = async (id, wantedRunId) => {
    let named;
    try {
      named = (await api.gymRun(wantedRunId)).run;
    } catch (err) {
      if (err.status === 404) {
        setRouteMissing("run");
        return;
      }
      throw err;
    }
    // A run from another session is not this session's run, whatever its id.
    if (named.workspaceId !== id) {
      setRouteMissing("run");
      return;
    }
    setRouteMissing("");
    setRun(named);
    setFilter(defaultFilter(named.challenges || []));
    if (!isRunFinished(named)) pollRun(named.id);
  };

  const previousRouteWorkspace = useRef(workspaceId);
  useEffect(() => {
    const previous = previousRouteWorkspace.current;
    previousRouteWorkspace.current = workspaceId;
    if (!workspaceId) {
      setRouteMissing("");
      // Back to /argument-gym from a session: that session leaves the screen.
      // Only on that move -- a run handed over from the editor arrives on
      // /argument-gym and is given its own URL a moment later.
      if (previous && workspaceIdRef.current) startNewSession();
      return;
    }
    if (workspace?.id !== workspaceId) {
      loadSessionById(workspaceId, runId);
      return;
    }
    if (runId && run?.id !== runId) {
      setBusy(true);
      loadRunById(workspaceId, runId).catch((err) => setError(err.message)).finally(() => setBusy(false));
    }
  }, [workspaceId, runId]);

  const startNewSession = () => {
    setSessionsOpen(false);
    workspaceIdRef.current = null;
    setRouteMissing("");
    if (workspaceId) onNavigate({});
    setWorkspace(null);
    setRun(null);
    setBrief(null);
    setCaseMaterials([]);
    setMaterials([]);
    setDetection(null);
    setQueued([]);
    setUploadNote("");
    setError("");
    setNotice("");
    setCaseContext(matter ? "existing_case" : "none");
    setSelectedMatterId(matter?.id || "");
  };

  const patchWorkspace = async (payload) => {
    setBusy(true);
    setError("");
    try {
      const target = await ensureWorkspace();
      const response = await api.updateGymWorkspace(target.id, payload);
      setWorkspace(response.workspace);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const selectedChecks = effectiveSelection(workspace?.enabledChecks ?? null, checkCatalog, checkDefaults);

  const changeChecks = async (checkId) => {
    await patchWorkspace({ enabledChecks: toggleCheck(selectedChecks, checkId) });
  };

  const saveChecklist = async ({ id, title, items }) => {
    setBusy(true);
    setError("");
    try {
      const response = id
        ? await api.updateGymChecklist(id, { title, items })
        : await api.createGymChecklist({ title, items });
      await loadChecklists();
      setEditingChecklistId(String(response.checklist.id));
      setNotice(`Saved “${response.checklist.title}”.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const removeChecklist = async (id) => {
    setBusy(true);
    try {
      await api.deleteGymChecklist(id);
      setEditingChecklistId("");
      await loadChecklists();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const previewDetection = async () => {
    setBusy(true);
    try {
      const target = await ensureWorkspace();
      const response = await api.gymCourtDetection(target.id);
      setDetection(response.detection);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const uploadDocument = async (file, role) => {
    setError("");
    setBusy(true);
    try {
      const target = await ensureWorkspace();
      const formData = new FormData();
      formData.append("file", file);
      formData.append("role", role);
      const response = await api.uploadGymDocument(target.id, formData);
      if (role === "brief_under_test") {
        setBrief(response.document);
        setUploadNote(
          [exhibitSummary(response.document), truncationNotice(response.document)].filter(Boolean).join(" "),
        );
        if ((response.exhibits || []).length) {
          setCaseMaterials((current) => [...current, ...response.exhibits]);
        }
      } else {
        setCaseMaterials((current) => [...current, response.document]);
      }
      await loadMaterials(target.id);
      await loadSessions();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const startRun = async () => {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      let target = await ensureWorkspace();
      const wantedMatterId = caseContext === "existing_case" ? selectedMatterId : "";
      if ((target.matterId || "") !== wantedMatterId) {
        const updated = await api.updateGymWorkspace(target.id, { matterId: wantedMatterId });
        target = updated.workspace;
        setWorkspace(target);
      }
      const response = await api.runArgumentGym(target.id, { briefId: brief?.id });
      setRun(response.run);
      // The run has its own address; a reload follows it rather than starting another.
      onNavigate({ workspaceId: target.id, runId: response.run.id });
      setQueued([]);
      if (!isRunFinished(response.run)) {
        const finished = await pollRun(response.run.id);
        if (finished) setFilter(defaultFilter(finished.challenges || []));
      } else {
        setFilter(defaultFilter(response.run.challenges || []));
      }
      await loadMaterials(target.id);
      await loadSessions();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const setDisposition = async (challenge, disposition) => {
    setBusyChallengeId(challenge.id);
    try {
      const response = await api.setChallengeDisposition(challenge.id, { disposition });
      setRun((current) => ({ ...current, challenges: replaceChallenge(current.challenges, response.challenge) }));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyChallengeId(null);
    }
  };

  const researchChallenge = async (challenge) => {
    setBusyChallengeId(challenge.id);
    setNotice("");
    try {
      const response = await api.researchChallenge(challenge.id);
      setRun((current) => ({ ...current, challenges: replaceChallenge(current.challenges, response.challenge) }));
      setNotice(
        response.addedSourceCount
          ? `Added ${response.addedSourceCount} source${response.addedSourceCount === 1 ? "" : "s"} to this challenge.`
          : "That search found nothing this challenge does not already cite.",
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyChallengeId(null);
    }
  };

  const copyChallenge = async (challenge) => {
    const text = copyTextForChallenge(challenge);
    try {
      await navigator.clipboard.writeText(text);
      setNotice("Suggested response copied.");
    } catch {
      setNotice("Copying is blocked in this browser. Select the suggested response text instead.");
    }
  };

  const openRevisionPlan = async () => {
    setBusy(true);
    setError("");
    try {
      const response = await api.gymRevisionPlan(run.id, queued);
      setPlan(response.revisionPlan);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const applyPlan = async () => {
    setBusy(true);
    try {
      const response = await api.applyGymRevision(run.id, { plan: plan.plan });
      setRun(response.run);
      setPlan(null);
      setQueued([]);
      setNotice("The draft was revised. Review the changes in the editor's document history.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const downloadAnnotated = async () => {
    setBusy(true);
    setError("");
    try {
      await saveResponseAsFile(await api.gymArtifact(run.id, "annotated"));
      setNotice("Annotated copy downloaded. Review all comments, including unplaced notes.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const openArtifact = async (kind) => {
    setBusy(true);
    try {
      const response = await api.gymArtifact(run.id, kind);
      setArtifact(response.artifact);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleMaterial = async (material, excluded) => {
    setBusy(true);
    try {
      const response = await api.setGymMaterialExcluded(workspace.id, { materialId: material.id, excluded });
      setMaterials(response.materials || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const challenges = run?.challenges || [];
  const readiness = canStartRun({
    briefDocument: brief,
    caseContext,
    caseMaterials,
    matterId: selectedMatterId,
  });
  const { canRevise } = revisionTargets(challenges, queued);
  // A prep sheet built from half a run is worse than no prep sheet.
  const actionsDisabled = runActionsDisabled({ run, busy });
  const filters = availableFilters(challenges, filter);
  // A run exists the moment it starts; results are a different thing.
  const view = runView(run);

  return (
    <section className="panel gym-panel">
      <PanelHeading
        icon={<Swords size={18} />}
        title="Argument gym"
        description={workspace
          ? sessionSubtitle(workspace)
          : "An opponent attacks the brief, a judge weighs it, and a coach proposes answers. Nothing here edits your document."}
      >
        <button className="btn btn-light" type="button" onClick={() => setSessionsOpen(true)}>
          <FolderOpen size={16} /> Open session{sessions.length ? ` (${sessions.length})` : ""}
        </button>
        <button className="btn btn-light" type="button" onClick={startNewSession} disabled={busy}>
          <Plus size={16} /> New
        </button>
      </PanelHeading>
      {routeMissing && (
        <div className="empty-state compact-empty" role="status">
          <strong className="empty-state-title">{routeMissing === "run" ? "This run is not part of this session" : "This session is not available"}</strong>
          <p>{routeMissing === "run" ? "The session opened, but it has no run with this number." : "No argument gym session with this number is available to you."} Nothing else is shown in its place.</p>
        </div>
      )}

      {error && <div className="alert alert-danger">{error}</div>}
      {notice && <div className="alert alert-info">{notice}</div>}

      <div className="gym-main">

      {view === "setup" && (
        <div className="gym-setup">
          <section>
            <h4>1. Brief under test</h4>
            {brief ? (
              <>
                <p className="gym-brief-name">
                  <FileText size={16} /> <span title={brief.title}>{shortTitle(brief.title)}</span>
                  {brief.unitCount ? <span className="muted"> · {brief.unitCount} addressable passages</span> : null}
                  {brief.pleadingType ? <span className="muted"> · read as a {brief.pleadingType.replace("_", " ")}</span> : null}
                </p>
                {uploadNote && <p className="muted gym-split-note">{uploadNote}</p>}
              </>
            ) : (
              <label className="btn btn-light gym-upload">
                <Upload size={16} /> Upload a brief (PDF, DOCX, or text)
                <input
                  type="file"
                  accept=".pdf,.docx,.txt,.md"
                  hidden
                  onChange={(event) => event.target.files?.[0] && uploadDocument(event.target.files[0], "brief_under_test")}
                />
              </label>
            )}
          </section>

          <section>
            <h4>2. Case context</h4>
            <div className="gym-context-choices">
              {CASE_CONTEXT_CHOICES.map((choice) => (
                <label key={choice.id} className={`gym-context-choice ${caseContext === choice.id ? "active" : ""}`}>
                  <input
                    type="radio"
                    name="gym-case-context"
                    value={choice.id}
                    checked={caseContext === choice.id}
                    onChange={() => setCaseContext(choice.id)}
                  />
                  <strong>{choice.label}</strong>
                  <span className="muted">{choice.description}</span>
                </label>
              ))}
            </div>
            {caseContext === "existing_case" && (
              <label className="form-label">
                Case
                <select
                  className="form-select"
                  value={selectedMatterId}
                  onChange={(event) => setSelectedMatterId(event.target.value)}
                >
                  <option value="">Choose a case…</option>
                  {caseOptions(cases).map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {caseContext === "uploaded" && (
              <>
                <ul className="gym-source-list">
                  {caseMaterials.map((document) => (
                    <li key={document.id}>{document.title}</li>
                  ))}
                </ul>
                <label className="btn btn-light gym-upload">
                  <Upload size={16} /> Add a case document
                  <input
                    type="file"
                    accept=".pdf,.docx,.txt,.md"
                    hidden
                    onChange={(event) => event.target.files?.[0] && uploadDocument(event.target.files[0], "case_record")}
                  />
                </label>
              </>
            )}
          </section>

          <details className="disclosure gym-config">
            <summary>
              <ListChecks size={16} /> Checks to run
              <span className="disclosure-summary">{selectedChecks.length} of {checkCatalog.length} selected</span>
            </summary>
            <CheckSelector
              catalog={checkCatalog}
              selected={selectedChecks}
              checklists={checklists}
              checklistId={workspace?.checklist?.id}
              busy={busy}
              onToggle={changeChecks}
              onChecklist={(value) => patchWorkspace({ checklistId: value ? Number(value) : null })}
              onManageChecklists={() => setChecklistModalOpen(true)}
              onManagePassive={() => setPassiveModalOpen(true)}
            />
          </details>

          <details className="disclosure gym-config">
            <summary>
              <Landmark size={16} /> Jurisdiction and filing rules
              <span className="disclosure-summary">
                {workspace?.court?.label || (workspace?.courtRuleMode === "off" ? "off" : "detected from the brief")}
              </span>
            </summary>
            <JurisdictionControls
              workspace={workspace}
              courts={courts}
              courtTypes={courtTypes}
              detection={detection}
              busy={busy}
              onChange={patchWorkspace}
              onDetect={previewDetection}
            />
          </details>

          <div className="button-row step-actions">
            {!readiness.ready && <span className="muted">{readiness.reason}</span>}
            <button className="btn btn-primary" type="button" disabled={busy || !readiness.ready} onClick={startRun}>
              {busy ? <Loader2 className="spin" size={16} /> : <Swords size={16} />} Run the gym
            </button>
          </div>
        </div>
      )}

      {view === "running" && <RunProgress run={run} />}

      {view === "failed" && (
        <RunFailed
          run={run}
          busy={busy}
          onRetry={startRun}
          onBack={() => {
            setRun(null);
            setError("");
          }}
        />
      )}

      {view === "results" && (
        <>
          {/* One slim bar: what was tested, how it read, and the controls. */}
          <div className="gym-runbar">
            <div className="gym-runbar-text">
              <span className="gym-runbar-title" title={run.briefTitle}>{shortTitle(run.briefTitle, 40)}</span>
              <span className="muted">{challengeSummary(challenges)}</span>
              {rerunSummary(run.comparison) && <span className="muted">{rerunSummary(run.comparison)}</span>}
            </div>
            <button className="btn btn-light" type="button" disabled={actionsDisabled} onClick={startRun}>
              {busy ? <Loader2 className="spin" size={16} /> : <Swords size={16} />} Run again
            </button>
          </div>

          <div className="gym-annotated-download">
            <button className="btn btn-light" type="button" disabled={actionsDisabled} onClick={downloadAnnotated}>
              <FileText size={16} /> Download annotated copy
            </button>
            <p className="muted">All challenges as Word comments or PDF page notes, including handled findings. Unmatched passages are labeled unplaced. Suggested edits remain comments for your review.</p>
          </div>

          {run.assessment && (
            <section className="gym-assessment">
              {run.verdict && <span className="gym-verdict-chip">{run.verdict}</span>}
              <details className="gym-assessment-detail"><summary>Read the overall assessment</summary><p>{run.assessment}</p></details>
            </section>
          )}

          <nav ref={reviewViewRef} className="gym-view-switch" aria-label="Review view">
            <button className={`btn ${reviewMode === "priorities" ? "btn-primary" : "btn-light"}`} type="button" aria-pressed={reviewMode === "priorities"} onClick={() => changeReviewMode("priorities")}>Focused review</button>
            <button className={`btn ${reviewMode === "overview" ? "btn-primary" : "btn-light"}`} type="button" aria-pressed={reviewMode === "overview"} onClick={() => changeReviewMode("overview")}>Compact overview</button>
          </nav>

          {((reviewMode === "priorities" && filters.length > 0) || queued.length > 0) && (
            <div className="gym-filter button-row compact">
              {reviewMode === "priorities" && filters.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`btn ${filter === item.id ? "btn-primary" : "btn-light"}`}
                  aria-pressed={filter === item.id}
                  onClick={() => { setRequestedChallenge(null); setFilter(item.id); }}
                >
                  {item.label} ({item.count})
                </button>
              ))}
              {queued.length > 0 && (
                <button
                  className="btn btn-primary gym-filter-action"
                  type="button"
                  disabled={actionsDisabled || !canRevise}
                  onClick={openRevisionPlan}
                >
                  <FileText size={16} /> Open revision plan ({queued.length})
                </button>
              )}
            </div>
          )}

          <div hidden={reviewMode !== "overview"}>
            <CompactOverview active={reviewMode === "overview"} key={run.id} run={run} catalog={checkCatalog} onOpenChallenge={(id) => {
              setFilter("all");
              setRequestedChallenge({ id });
              setReviewMode("priorities");
            }} />
          </div>
          <div hidden={reviewMode !== "priorities"}>
          <div className="gym-results">
            <ChallengeReview
              key={`${run.id}-${filter}`}
              requestedChallenge={requestedChallenge} challenges={challenges} filter={filter} busyChallengeId={busyChallengeId} queued={queued}
              onDisposition={setDisposition} onResearch={researchChallenge} onCopy={copyChallenge} onEvidence={setEvidenceChallenge}
              onQueueRevision={(item) => setQueued((current) => current.includes(item.id) ? current.filter((id) => id !== item.id) : [...current, item.id])}
            />
            <AuditSummary key={run.id} run={run} catalog={checkCatalog} onOpenArtifact={openArtifact} actionsDisabled={actionsDisabled} />
          </div>
          </div>

          <details className="disclosure gym-config">
            <summary>
              <ListChecks size={16} /> Checks and jurisdiction
              <span className="muted">change what the next run does</span>
            </summary>
            <CheckSelector
              catalog={checkCatalog}
              selected={selectedChecks}
              checklists={checklists}
              checklistId={workspace?.checklist?.id}
              busy={actionsDisabled}
              onToggle={changeChecks}
              onChecklist={(value) => patchWorkspace({ checklistId: value ? Number(value) : null })}
              onManageChecklists={() => setChecklistModalOpen(true)}
              onManagePassive={() => setPassiveModalOpen(true)}
            />
            <JurisdictionControls
              workspace={workspace}
              courts={courts}
              courtTypes={courtTypes}
              detection={detection}
              busy={busy}
              onChange={patchWorkspace}
              onDetect={previewDetection}
            />
          </details>

          <p className="muted gym-coverage-line">{coverageSummary(run.coverage)}</p>
          <MaterialsConsidered run={run} materials={materials} onToggle={toggleMaterial} busy={actionsDisabled} />
        </>
      )}

      </div>

      <SessionBrowser
        open={sessionsOpen}
        sessions={sessions}
        matters={sessionMatters}
        matterId={sessionMatterId}
        onMatterChange={(value) => {
          setSessionMatterId(value);
          loadSessions({ matterId: value });
        }}
        query={sessionQuery}
        onQueryChange={(value) => {
          setSessionQuery(value);
          loadSessions({ query: value });
        }}
        activeId={workspace?.id ?? null}
        onOpen={openSession}
        onNew={startNewSession}
        onClose={() => setSessionsOpen(false)}
        busy={busy}
      />
      <ChecklistModal
        open={checklistModalOpen}
        checklists={checklists}
        activeId={editingChecklistId}
        busy={busy}
        onClose={() => setChecklistModalOpen(false)}
        onSelect={setEditingChecklistId}
        onSave={saveChecklist}
        onDelete={removeChecklist}
      />
      <PassivePhraseModal
        open={passiveModalOpen}
        phrases={workspace?.checkSettings?.passive_voice?.acceptedPassivePhrases || []}
        busy={busy}
        onClose={() => setPassiveModalOpen(false)}
        onSave={async (phrases) => {
          await patchWorkspace({ checkSettings: { passive_voice: { acceptedPassivePhrases: phrases } } });
          setPassiveModalOpen(false);
        }}
      />
      <EvidenceModal challenge={evidenceChallenge} onClose={() => setEvidenceChallenge(null)} />
      <ArtifactModal artifact={artifact} onClose={() => setArtifact(null)} />
      <GymRevisionModal
        plan={plan}
        busy={busy}
        onClose={() => setPlan(null)}
        onUpdateItem={(blockKey, patch) => setPlan((current) => updatePlanItem(current, blockKey, patch))}
        onApply={applyPlan}
      />
    </section>
  );
}

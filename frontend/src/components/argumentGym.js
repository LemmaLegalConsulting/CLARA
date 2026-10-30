// Derivation rules for the Argument Gym panel. The panel renders challenge
// cards; everything about *which* cards, in what order, and what each one is
// allowed to claim lives here so it can be tested without a browser.

export const DISPOSITIONS = ["open", "addressed", "dismissed"];

export const CASE_CONTEXT_CHOICES = [
  {
    id: "none",
    label: "No case record",
    description: "Test the legal argument only. Nothing is checked against a client file.",
  },
  {
    id: "existing_case",
    label: "Existing case",
    description: "Test the brief against the documents already on a case in this tool.",
  },
  {
    id: "uploaded",
    label: "Uploaded case materials",
    description: "Test the brief against files you attach here. PDF, DOCX, or text.",
  },
];

// A municipality identifies a trial court. An appellate district is not in a
// city, so the form must not ask for one and a session must not record one.
export const MUNICIPALITY_COURT_TYPES = ["municipal", "county", "common_pleas", "administrative"];

export const JURISDICTION_MODES = [
  { id: "auto", label: "Detect from the brief" },
  { id: "manual", label: "Set by hand" },
];

export const COURT_RULE_MODES = [
  { id: "auto", label: "Use the detected court's rules" },
  { id: "manual", label: "Use a court I choose" },
  { id: "off", label: "Skip filing-format rules" },
];

const SEVERITY_ORDER = { high: 0, medium: 1, low: 2 };
const DISPOSITION_ORDER = { open: 0, addressed: 1, dismissed: 2 };

export function isOpen(challenge) {
  return (challenge?.disposition || "open") === "open";
}

// Open challenges first, then the judge's ranking. A card the advocate has
// already dealt with should not sit above one they have not seen.
export function rankedChallenges(challenges = [], { filter = "all" } = {}) {
  const visible = challenges.filter((challenge) => {
    if (filter === "open") return isOpen(challenge);
    if (filter === "resolved") return !isOpen(challenge);
    return true;
  });
  return [...visible].sort((left, right) => {
    const byDisposition =
      (DISPOSITION_ORDER[left.disposition] ?? 0) - (DISPOSITION_ORDER[right.disposition] ?? 0);
    if (byDisposition !== 0) return byDisposition;
    const byPriority = priorityForChallenge(left).rank - priorityForChallenge(right).rank;
    if (byPriority !== 0) return byPriority;
    const bySeverity = (SEVERITY_ORDER[left.severity] ?? 1) - (SEVERITY_ORDER[right.severity] ?? 1);
    if (bySeverity !== 0) return bySeverity;
    const byImportance = (right.importance ?? 0) - (left.importance ?? 0);
    if (byImportance !== 0) return byImportance;
    return (left.ordinal ?? 0) - (right.ordinal ?? 0);
  });
}

export function challengeCounts(challenges = []) {
  const counts = { total: challenges.length, open: 0, addressed: 0, dismissed: 0, recurring: 0 };
  for (const challenge of challenges) {
    const disposition = challenge.disposition || "open";
    if (disposition in counts) counts[disposition] += 1;
    if (challenge.recurring) counts.recurring += 1;
  }
  return counts;
}

// Deliberately not a score. A count of what is still open is a fact about the
// review; a number out of ten would be a claim about the brief that nothing
// here can support.
export function challengeSummary(challenges = []) {
  const counts = challengeCounts(challenges);
  if (!counts.total) return "No challenges were raised.";
  const parts = [`${counts.open} open`];
  if (counts.addressed) parts.push(`${counts.addressed} addressed`);
  if (counts.dismissed) parts.push(`${counts.dismissed} dismissed`);
  return `${counts.total} challenge${counts.total === 1 ? "" : "s"}: ${parts.join(", ")}.`;
}

export function targetLabel(target = {}) {
  const parts = [];
  if (target.section) parts.push(target.section);
  if (target.paragraph) parts.push(`para. ${target.paragraph}`);
  if (target.page) parts.push(`p. ${target.page}`);
  if (!parts.length && target.blockKey) parts.push(target.blockKey);
  return parts.join(", ");
}

export function coverageSummary(coverage = {}) {
  const queryCount = (coverage.queries || []).length;
  if (!queryCount) return "No adversarial research ran for this brief.";
  const base = `${queryCount} adversarial quer${queryCount === 1 ? "y" : "ies"} returned ${coverage.resultCount || 0} source${
    coverage.resultCount === 1 ? "" : "s"
  }.`;
  if (coverage.adequate) return `${base} Coverage looks complete.`;
  const gaps = coverage.gaps || [];
  return gaps.length ? `${base} Gaps: ${gaps.join(" ")}` : `${base} Coverage is thin.`;
}

export function materialsByOrigin(materials = []) {
  const groups = { matter_document: [], upload: [] };
  for (const material of materials) {
    (groups[material.origin] ||= []).push(material);
  }
  return groups;
}

// A rerun is only worth reporting when it can say what changed.
export function rerunSummary(comparison = {}) {
  if (!comparison.previousRunId) return "";
  const recurring = (comparison.recurring || []).length;
  const resolved = (comparison.resolved || []).length;
  const added = (comparison.new || []).length;
  const parts = [];
  if (recurring) parts.push(`${recurring} still raised`);
  if (resolved) parts.push(`${resolved} no longer raised`);
  if (added) parts.push(`${added} new`);
  return parts.length ? `Compared with the previous run: ${parts.join(", ")}.` : "Nothing changed since the previous run.";
}

export function copyTextForChallenge(challenge = {}) {
  const lines = [
    ...(challenge.finding ? [`Finding: ${challenge.finding}`] : []),
    ...(challenge.recommendation ? [`What to do: ${challenge.recommendation}`] : []),
    `Opposition point (${challenge.categoryLabel || challenge.category || "challenge"}): ${challenge.opponentArgument || ""}`,
  ];
  const target = targetLabel(challenge.target);
  if (target) lines.push(`Targets: ${target}`);
  if (challenge.judgeAssessment) lines.push(`Judge: ${challenge.judgeAssessment}`);
  if (challenge.suggestedResponse) lines.push("", challenge.suggestedResponse);
  else if (challenge.recommendation) lines.push("", challenge.recommendation);
  const citations = (challenge.legalSources || [])
    .map((source) => source.citation || source.title)
    .filter(Boolean);
  if (citations.length) lines.push("", `Authority: ${citations.join("; ")}`);
  return lines.join("\n");
}

// Only native drafts have blocks a revision can target; an external brief's
// recommendations are copyable text and must not pretend otherwise.
export function revisionTargets(challenges = [], selectedIds = []) {
  const selected = new Set(selectedIds.map(Number));
  const actionable = [];
  const copyOnly = [];
  for (const challenge of challenges) {
    if (selected.size && !selected.has(Number(challenge.id))) continue;
    (challenge.target?.blockKey ? actionable : copyOnly).push(challenge);
  }
  return { actionable, copyOnly, canRevise: actionable.length > 0 };
}

export function updatePlanItem(plan, blockKey, patch) {
  return { ...plan, plan: (plan.plan || []).map((item) => (item.blockKey === blockKey ? { ...item, ...patch } : item)) };
}

export function replaceChallenge(challenges = [], updated) {
  return challenges.map((challenge) => (challenge.id === updated.id ? updated : challenge));
}

// The standalone flow cannot run until it has a brief, and cannot claim to test
// against a case record it was never given.
export function canStartRun({ briefDocument, caseContext, caseMaterials = [], matterId = "" } = {}) {
  if (!briefDocument) return { ready: false, reason: "Upload the brief you want to test." };
  if (caseContext === "existing_case" && !matterId) {
    return { ready: false, reason: "Choose the case whose record should be used." };
  }
  if (caseContext === "uploaded" && !caseMaterials.length) {
    return { ready: false, reason: "Add at least one case document, or switch to a doctrinal-only test." };
  }
  return { ready: true, reason: "" };
}


// Sessions

export function sessionSubtitle(workspace = {}) {
  const parts = [];
  if (workspace.matterName) parts.push(workspace.matterName);
  else parts.push("No case record");
  if (workspace.briefTitle) parts.push(workspace.briefTitle);
  return parts.join(" · ");
}

export function sessionStatus(workspace = {}) {
  if (!workspace.runCount) return "Not run yet";
  const open = workspace.openChallengeCount || 0;
  return `${workspace.runCount} run${workspace.runCount === 1 ? "" : "s"} · ${open} open`;
}

// Newest activity first: a session someone ran today is the one they mean.
export function sortSessions(workspaces = []) {
  return [...workspaces].sort((left, right) => {
    const leftAt = left.lastRunAt || left.updatedAt || "";
    const rightAt = right.lastRunAt || right.updatedAt || "";
    if (leftAt === rightAt) return (right.id ?? 0) - (left.id ?? 0);
    return rightAt.localeCompare(leftAt);
  });
}

export function matterFilterOptions(matters = []) {
  return [
    { id: "", label: "All cases" },
    { id: "none", label: "No case record" },
    ...matters.map((matter) => ({ id: matter.id, label: matterFilterLabel(matter) })),
  ];
}

// Jurisdiction

export function usesMunicipality(courtType, courtTypes = []) {
  const known = courtTypes.find((item) => item.id === courtType);
  if (known) return Boolean(known.usesMunicipality);
  return MUNICIPALITY_COURT_TYPES.includes(courtType);
}

// Drop a value the chosen court type has no place for, rather than sending a
// municipality the backend will silently discard.
export function cleanJurisdictionDetail(detail = {}, courtTypes = []) {
  const courtType = detail.courtType || "";
  const cleaned = { ...detail };
  if (courtType && !usesMunicipality(courtType, courtTypes)) cleaned.municipality = "";
  return Object.fromEntries(Object.entries(cleaned).filter(([, value]) => String(value || "").trim()));
}

export function jurisdictionLabel(detail = {}) {
  return [
    detail.municipality,
    detail.county ? `${detail.county} County` : "",
    detail.division,
    detail.state,
  ]
    .filter(Boolean)
    .join(", ");
}

export function courtSummary({ court, detection } = {}) {
  if (detection?.mode === "off") return "Filing-format rules are off for this session.";
  if (!court) return detection?.reason || "No court is selected, so filing-format rules were not applied.";
  const verified =
    court.verification === "verified"
      ? `Checked against ${court.source || "this court's recorded rules"}.`
      : "Unverified starter profile — its findings are warnings, not rule violations.";
  return `${court.label || court.name}. ${verified}`;
}

// Filing-format compliance

export function complianceGroups(compliance = {}) {
  const findings = compliance.findings || [];
  return {
    checked: Boolean(compliance.checked),
    reason: compliance.reason || "",
    pleadingType: compliance.pleadingType || "",
    errors: findings.filter((finding) => finding.severity === "error"),
    warnings: findings.filter((finding) => finding.severity === "warning"),
    unmeasured: findings.filter((finding) => finding.severity === "info"),
    total: findings.length,
  };
}

export function complianceSummary(compliance = {}) {
  const groups = complianceGroups(compliance);
  if (!groups.checked) return groups.reason || "Filing-format rules were not applied.";
  if (!groups.total) return "The document meets every filing rule on file for this court.";
  const parts = [];
  if (groups.errors.length) parts.push(`${groups.errors.length} would be rejected`);
  if (groups.warnings.length) parts.push(`${groups.warnings.length} to check`);
  // An unmeasured property is not a pass, and the summary has to say so or the
  // absence of a finding reads as compliance.
  if (groups.unmeasured.length) parts.push(`${groups.unmeasured.length} could not be measured`);
  return parts.join(", ") + ".";
}

// Uploads

export function exhibitSummary(document = {}) {
  const split = document.split;
  if (!split || !(split.exhibits || []).length) return "";
  const pages = split.briefPageCount;
  return `Read pages 1-${pages} as the brief and split ${split.exhibits.length} attachment${
    split.exhibits.length === 1 ? "" : "s"
  } into case materials. ${split.boundaryReason}`;
}

export function truncationNotice(document = {}) {
  return document.truncated
    ? "This brief is longer than one run reads. Only the earlier part was analyzed; split it or narrow it before relying on the result."
    : "";
}


// Checks

export const CHECK_CATEGORY_LABELS = {
  argument: "Argument",
  form: "Form of the filing",
  language: "Language",
};

export function groupChecks(catalog = []) {
  const groups = [];
  for (const check of catalog) {
    let group = groups.find((item) => item.id === check.category);
    if (!group) {
      group = { id: check.category, label: CHECK_CATEGORY_LABELS[check.category] || check.category, checks: [] };
      groups.push(group);
    }
    group.checks.push(check);
  }
  return groups;
}

// An empty selection means the catalog's defaults, so the first toggle has to
// start from those rather than from nothing.
export function effectiveSelection(selected, catalog = [], defaults = []) {
  if (selected === null || selected === undefined) {
    return defaults.length ? [...defaults] : catalog.filter((check) => check.defaultEnabled).map((check) => check.id);
  }
  return [...selected];
}

export function toggleCheck(selected, checkId) {
  return selected.includes(checkId) ? selected.filter((id) => id !== checkId) : [...selected, checkId];
}

export function checkStatusSummary(checksRun = []) {
  if (!checksRun.length) return "";
  const counts = { on: 0, off: 0, unavailable: 0 };
  for (const entry of checksRun) counts[entry.status] = (counts[entry.status] || 0) + 1;
  const parts = [`${counts.on} ran`];
  if (counts.off) parts.push(`${counts.off} off`);
  // A check that could not run is not a check that passed, so the count is
  // always shown rather than folded into "off".
  if (counts.unavailable) parts.push(`${counts.unavailable} could not run`);
  return parts.join(", ") + ".";
}

export function checksByStatus(checksRun = [], status) {
  return checksRun.filter((entry) => entry.status === status);
}

// Findings grouped by the check that produced them, so "no findings" is always
// attributable to a check that actually ran.
export function findingsByCheck(checkResults = {}, checksRun = []) {
  const labels = Object.fromEntries(checksRun.map((entry) => [entry.id, entry.label]));
  return Object.entries(checkResults)
    .map(([checkId, result]) => {
      const findings = result.findings || [];
      return {
        id: checkId,
        label: labels[checkId] || checkId,
        summary: result.summary || "",
        errors: findings.filter((finding) => finding.severity === "error"),
        warnings: findings.filter((finding) => finding.severity === "warning"),
        infos: findings.filter((finding) => finding.severity === "info"),
        findings,
      };
    })
    .sort((left, right) => right.errors.length - left.errors.length || right.findings.length - left.findings.length);
}

export function findingCounts(groups = []) {
  return groups.reduce(
    (totals, group) => ({
      errors: totals.errors + group.errors.length,
      warnings: totals.warnings + group.warnings.length,
      infos: totals.infos + group.infos.length,
    }),
    { errors: 0, warnings: 0, infos: 0 },
  );
}

// Rule elements

const PLED_LABELS = { yes: "pleaded", partial: "partly pleaded", no: "not pleaded" };
const SUPPORT_LABELS = {
  yes: "supported",
  partial: "partly supported",
  no: "unsupported",
  nothing_supplied: "nothing supplied supports it",
};

// Pleading an element and supporting it are different findings and the card has
// to keep them apart; an assertion is not support.
export function elementState(element = {}) {
  const pled = PLED_LABELS[element.pled] || "unknown";
  const supported = SUPPORT_LABELS[element.supported] || "";
  return supported ? `${pled}, ${supported}` : pled;
}

export function ruleAuditSummary(ruleAudit = []) {
  if (!ruleAudit.length) return "No maintained rule was invoked by this brief.";
  const unmet = ruleAudit.reduce((total, audit) => total + (audit.unmetCount || 0), 0);
  const rules = `${ruleAudit.length} rule${ruleAudit.length === 1 ? "" : "s"}`;
  if (!unmet) return `${rules} invoked; every element on file is carried.`;
  return `${rules} invoked; ${unmet} element${unmet === 1 ? "" : "s"} not carried.`;
}

export function unverifiedRules(ruleAudit = []) {
  return ruleAudit.filter((audit) => audit.verification !== "verified").map((audit) => audit.citation);
}

// Checklist

export function checklistSummary(results = {}) {
  const items = results.results || [];
  if (!items.length) return "";
  const counts = items.reduce((totals, item) => ({ ...totals, [item.outcome]: (totals[item.outcome] || 0) + 1 }), {});
  return Object.entries(counts)
    .map(([outcome, count]) => `${count} ${outcome.replace("_", " ")}`)
    .join(", ");
}

export function checklistItemsFromText(text = "") {
  return text
    .split("\n")
    .map((line) => line.replace(/^\s*[-*\d.)\s]+/, "").trim())
    .filter(Boolean)
    .map((line, index) => ({ id: `item-${index + 1}`, text: line }));
}

export function checklistItemsToText(items = []) {
  return items.map((item) => item.text).join("\n");
}


// Run progress

export const RUN_POLL_MS = 3000;
// Nothing here waits on a single request: a run outlives the worker timeout, and
// a killed worker returns no headers at all, which the browser reports as a CORS
// failure rather than as the timeout it is.
export const RUN_POLL_TIMEOUT_MS = 15 * 60 * 1000;

const STAGE_LABELS = {
  compliance: "Checking the court's filing rules",
  document_checks: "Running the document checks",
  materials: "Choosing case materials",
  argument_map: "Mapping the argument",
  record_audit: "Checking the brief against the record",
  research_queries: "Writing adversarial research queries",
  research: "Researching",
  opponent: "Opposing counsel is building its attacks",
  rule_elements: "Auditing the elements of the rules invoked",
  custom_checklist: "Applying your checklist",
  judge: "The judge is weighing the attacks",
  coach: "Drafting responses",
  assessment: "Writing the assessment",
};

export function isRunFinished(run) {
  return ["complete", "failed"].includes(run?.status);
}

// Name the stage rather than showing a spinner: a run takes minutes, and an
// advocate watching a blank panel cannot tell slow research from a wedged run.
export function runProgressLabel(run) {
  if (!run) return "";
  if (run.status === "failed") return run.error || "The run failed.";
  if (run.status === "complete") return "";
  const stages = run.stageTrace || [];
  const last = stages[stages.length - 1];
  const label = last ? STAGE_LABELS[last.stage] || last.stage.replace(/_/g, " ") : "Starting";
  return `${label}… (step ${stages.length + (last ? 0 : 1)} of about ${Object.keys(STAGE_LABELS).length})`;
}

export function runProgressFraction(run) {
  const done = (run?.stageTrace || []).length;
  return Math.min(done / Object.keys(STAGE_LABELS).length, 0.95);
}


// Filters

// Keep an emptied active filter reachable after the last disposition changes,
// alongside the filter containing the work. Otherwise there is no way back.
export function availableFilters(challenges = [], activeFilter = null) {
  const counts = challengeCounts(challenges);
  const options = [
    { id: "open", label: "Open", count: counts.open },
    { id: "resolved", label: "Handled", count: counts.addressed + counts.dismissed },
  ].filter((option) => option.count > 0 || (challenges.length > 0 && option.id === activeFilter));
  if (options.length < 2) return [];
  return [{ id: "all", label: "All", count: counts.total }, ...options];
}

// Land on the filter that has the challenges in it, so the first thing an
// advocate sees is never an empty list.
export function defaultFilter(challenges = []) {
  const counts = challengeCounts(challenges);
  if (counts.open > 0) return "open";
  return counts.total > 0 ? "all" : "open";
}

export function emptyStateMessage(challenges = [], filter = "all") {
  if (!challenges.length) {
    return "This run raised no challenges. That is a statement about the review, not a finding that the brief is sound — check the research coverage below.";
  }
  if (filter === "open") return "Every challenge from this run has been handled.";
  return "Nothing has been handled yet.";
}

// While a run is going, everything that reads or changes it is unavailable:
// a prep sheet built from half a run is worse than no prep sheet.
export function runActionsDisabled({ run, busy } = {}) {
  return Boolean(busy) || Boolean(run && !isRunFinished(run));
}

// A long filename must not push the layout sideways.
export function shortTitle(title = "", limit = 52) {
  const text = String(title);
  if (text.length <= limit) return text;
  const extension = text.includes(".") ? text.slice(text.lastIndexOf(".")) : "";
  const stem = extension ? text.slice(0, text.length - extension.length) : text;
  return `${stem.slice(0, Math.max(limit - extension.length - 1, 8))}…${extension}`;
}


// Cases

// A case is the client, not the docket number. `matter_to_dict` calls the name
// `client`; reading `clientName` silently rendered an empty label beside a bare
// id like "26-0123", which is no way to find a case in a list.
export function caseLabel(matter = {}) {
  const name = String(matter.client || matter.clientName || matter.title || "").trim();
  const reference = String(matter.caseNumber || matter.id || "").trim();
  if (name && reference) return `${name} — ${reference}`;
  return name || reference || "Untitled case";
}

export function caseOptions(cases = []) {
  return cases.map((item) => ({ id: item.id, label: caseLabel(item) }));
}

export function matterFilterLabel(matter = {}) {
  return String(matter.name || "").trim() || String(matter.id || "").trim() || "Untitled case";
}


// Which of the panel's four states to draw. A run exists the moment it is
// started, so "there is a run" is not the same as "there are results": drawing
// the results early showed "this run raised no challenges" over a run that had
// not looked for any yet.
export function runView(run) {
  if (!run) return "setup";
  if (run.status === "failed") return "failed";
  return isRunFinished(run) ? "results" : "running";
}


// Presentation rules for the challenge cards and the audit sidebar.
//
// The panel reads as a worklist, not a report: what to do, then the judge's
// reason, then everything else on request. Severity is carried by colour and a
// short word rather than by heavy type, and the tool's own analysis is
// visually separated from text quoted out of the brief or the record.

export const SEVERITY_TONE = { high: "high", medium: "medium", low: "low" };

export function severityTone(challenge = {}) {
  return SEVERITY_TONE[challenge.severity] || "medium";
}

// Everything a card would otherwise print in full. Counted here so the card can
// offer "Evidence (4)" instead of four stacked blocks of citation text.
export function evidenceCount(challenge = {}) {
  return (challenge.legalSources || []).length + (challenge.recordSources || []).length;
}

// A suggested fix reads as steps, not as a paragraph. Split on sentence ends
// only when there are several; a single sentence stays a single sentence.
export function responseBullets(text = "", { max = 4 } = {}) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return [];
  const parts = trimmed
    .split(/(?<=[.;])\s+(?=[A-Z"“(])/)
    .map((part) => part.trim())
    .filter(Boolean);
  return parts.length > 1 ? parts.slice(0, max) : [trimmed];
}

// Long model prose is cut to a scannable length with the full text still
// available; a card should never open with six lines of summary.
export function clamp(text = "", limit = 220) {
  const value = String(text || "").trim();
  if (value.length <= limit) return { text: value, truncated: false };
  const cut = value.slice(0, limit);
  const boundary = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf(" "));
  return { text: `${cut.slice(0, boundary > 80 ? boundary : limit).trimEnd()}…`, truncated: true };
}

// The audit sidebar: a number and a word per area, detail only on request.
// Technical checks are status, not reading material.
export function auditBadges(run = {}) {
  const badges = [];
  const compliance = complianceGroups(run.compliance || {});
  if (compliance.checked) {
    const problems = compliance.errors.length + compliance.warnings.length;
    badges.push({
      id: "compliance",
      label: "Filing format",
      count: problems,
      tone: compliance.errors.length ? "high" : problems ? "medium" : "ok",
      note: compliance.unmeasured.length ? `${compliance.unmeasured.length} not measurable` : "",
    });
  }
  const findings = findingsByCheck(run.checkResults || {}, run.checksRun || []);
  const counts = findingCounts(findings);
  if (findings.length) {
    badges.push({
      id: "checks",
      label: "Document checks",
      count: counts.errors + counts.warnings,
      tone: counts.errors ? "high" : counts.errors + counts.warnings ? "medium" : "ok",
      note: counts.infos ? `${counts.infos} note${counts.infos === 1 ? "" : "s"}` : "",
    });
  }
  const unmet = (run.ruleAudit || []).reduce((total, audit) => total + (audit.unmetCount || 0), 0);
  if ((run.ruleAudit || []).length) {
    badges.push({
      id: "rules",
      label: "Rule elements",
      count: unmet,
      tone: unmet ? "high" : "ok",
      note: `${run.ruleAudit.length} rule${run.ruleAudit.length === 1 ? "" : "s"} invoked`,
    });
  }
  const checklist = (run.checklistResults?.results || []).filter((item) => item.outcome === "fail");
  if ((run.checklistResults?.results || []).length) {
    badges.push({
      id: "checklist",
      label: "Your checklist",
      count: checklist.length,
      tone: checklist.length ? "medium" : "ok",
      note: `${run.checklistResults.results.length} item${run.checklistResults.results.length === 1 ? "" : "s"}`,
    });
  }
  return badges;
}

// A check that did not run is a badge, not a section. It still has to be
// reachable -- it is not a pass -- but it does not belong in the reading order.
export function skippedChecksSummary(checksRun = []) {
  const off = checksByStatus(checksRun, "off");
  const unavailable = checksByStatus(checksRun, "unavailable");
  if (!off.length && !unavailable.length) return null;
  return {
    off,
    unavailable,
    label: [
      unavailable.length ? `${unavailable.length} could not run` : "",
      off.length ? `${off.length} off` : "",
    ]
      .filter(Boolean)
      .join(" · "),
  };
}

// Presentation only: opening style details never changes the checks selected
// for a run. Unknown checks stay in the main review; errors are never tucked
// away with optional language suggestions.
export function reviewCheckGroups(run = {}, catalog = []) {
  const languageIds = new Set(["grammar", "confused_words", "passive_voice", "readability"]);
  for (const check of catalog) {
    if (check.category === "language") languageIds.add(check.id);
  }
  const primary = [];
  const style = [];
  for (const group of findingsByCheck(run.checkResults || {}, run.checksRun || [])) {
    if (!languageIds.has(group.id)) {
      primary.push(group);
      continue;
    }
    if (group.errors.length) primary.push({ ...group, findings: group.errors });
    style.push({ ...group, findings: group.findings.filter((finding) => finding.severity !== "error") });
  }
  const styleChecks = (run.checksRun || []).filter((check) => languageIds.has(check.id));
  return {
    primary,
    style,
    styleChecks,
    styleCount: style.reduce((count, group) => count + group.findings.length, 0),
  };
}

// A short entry point, not a new legal assessment. The existing judge ranking
// determines order, and the remainder stays explicitly reachable.
export function challengeReview(challenges = [], { filter = "open", showAll = false, selectedId = null } = {}) {
  const ranked = rankedChallenges(challenges, { filter });
  const shown = showAll ? ranked : ranked.slice(0, 3);
  const selectedIndex = shown.findIndex((challenge) => challenge.id === selectedId);
  return {
    shown,
    total: ranked.length,
    hidden: ranked.length - shown.length,
    selected: shown[selectedIndex] || null,
    selectedIndex,
    previous: shown[selectedIndex - 1] || null,
    next: selectedIndex >= 0 ? shown[selectedIndex + 1] || null : null,
  };
}

// Certainty and consequence are separate: an uncertain serious concern calls
// for verification, not an instruction to concede or rewrite the argument.
export function priorityForChallenge(challenge = {}) {
  if (challenge.reviewAction === "verify" || challenge.confidence === "low" || ["weak", "misplaced"].includes(challenge.judgeVerdict)) {
    return { id: "verify", rank: 2, label: "Verify before changing" };
  }
  if (challenge.reviewAction === "strategy") return { id: "strategy", rank: 3, label: "Decide the approach" };
  if (challenge.severity === "low") return { id: "optional", rank: 4, label: "Optional improvements" };
  if (challenge.severity === "high") return { id: "first", rank: 0, label: "Address first" };
  return { id: "next", rank: 1, label: "Address next" };
}

export function challengeGroups(challenges = []) {
  const groups = [];
  for (const challenge of challenges) {
    const priority = !isOpen(challenge)
      ? { id: challenge.disposition, rank: challenge.disposition === "addressed" ? 10 : 11,
          label: challenge.disposition === "addressed" ? "Addressed" : "Dismissed" }
      : priorityForChallenge(challenge);
    let group = groups.find((item) => item.id === priority.id);
    if (!group) { group = { ...priority, challenges: [] }; groups.push(group); }
    group.challenges.push(challenge);
  }
  return groups.sort((left, right) => left.rank - right.rank);
}

export function challengePreview(challenge = {}) {
  return {
    title: clamp(challenge.finding || challenge.opponentArgument || "Review this finding", 160).text,
    action: clamp(challenge.recommendation || challenge.suggestedResponse || "Open this finding to review the reasoning and evidence.", 150).text,
    target: targetLabel(challenge.target),
  };
}

export function elementMatrix(ruleAudit = [], materials = []) {
  const titles = new Map(materials.map((material) => [String(material.id), material.title]));
  return ruleAudit.map((audit) => ({
    ...audit,
    verificationLabel: audit.verification === "verified" ? "Verified element list" : "Unverified element list — confirm the rule and its elements before relying on this audit.",
    rows: (audit.elements || []).map((element) => ({
      ...element,
      pleadedLabel: PLED_LABELS[element.pled] || "Unknown / not assessed",
      supportedLabel: element.needsRecordSupport === false
        ? "Not required by this profile"
        : SUPPORT_LABELS[element.supported] || "Unknown / not assessed",
      materials: (element.materialIds || []).map((id) => titles.get(String(id)) || `Material ${id} (details unavailable)`),
    })),
  }));
}

// Lenses describe the saved run, never the next run's selected checks. Counts
// are findings, not scores; no recorded result is not a passed check.
export function overviewLenses(run = {}, catalog = []) {
  const countLabel = (count, unit) => `${count} ${unit}${count === 1 ? "" : "s"}`;
  const challenges = rankedChallenges(run.challenges || []);
  const record = challenges.filter((item) => ["factual_support", "record_conflict"].includes(item.category));
  const groups = reviewCheckGroups(run, catalog);
  const compliance = complianceGroups(run.compliance || {});
  const matrix = elementMatrix(run.ruleAudit || [], run.materials || []);
  const checklist = run.checklistResults?.results || [];
  const status = (ids) => {
    const entries = (run.checksRun || []).filter((check) => ids.includes(check.id));
    return checkStatusSummary(entries) || "Execution not recorded.";
  };
  return [
    { id: "argument", label: "Argument", question: "What could opposing counsel argue?", summary: challengeSummary(challenges), status: status(["adversarial"]), challenges },
    { id: "elements", label: "Elements & evidence", question: "What is pleaded, and what supports it?", summary: `${countLabel(matrix.length, "rule list")} · ${countLabel(matrix.reduce((sum, rule) => sum + rule.rows.filter((row) => row.unmet).length, 0), "element")} flagged for review`, status: status(["rule_elements"]), matrix },
    { id: "record", label: "Case record", question: "Where does the record need attention?", summary: countLabel(record.length, "record-related challenge"), status: status(["record_audit"]), challenges: record },
    { id: "filing", label: "Filing & form", question: "Which filing and document checks need attention?", summary: `${countLabel(compliance.total, "filing finding")} · ${countLabel(groups.primary.reduce((sum, group) => sum + group.findings.length, 0), "document finding")}`, status: status(["court_formatting", "pleading_form", "draft_validation"]), compliance, groups: groups.primary },
    { id: "style", label: "Language & style", question: "Would you like to review the writing?", summary: `${countLabel(groups.styleCount, "note")} · optional review`, status: checkStatusSummary(groups.styleChecks) || "Execution not recorded.", groups: groups.style, checks: groups.styleChecks },
    { id: "checklist", label: "Your checklist", question: "What did your review questions find?", summary: `${countLabel(checklist.length, "answer")} recorded`, status: status(["custom_checklist"]), checklist },
  ];
}

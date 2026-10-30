import assert from "node:assert/strict";
import test from "node:test";

import {
  RUN_POLL_MS,
  auditBadges,
  availableFilters,
  caseLabel,
  clamp,
  caseOptions,
  defaultFilter,
  emptyStateMessage,
  evidenceCount,
  runActionsDisabled,
  shortTitle,
  canStartRun,
  isRunFinished,
  runProgressFraction,
  runProgressLabel,
  runView,
  responseBullets,
  severityTone,
  skippedChecksSummary,
  checkStatusSummary,
  checklistItemsFromText,
  checklistItemsToText,
  checklistSummary,
  checksByStatus,
  effectiveSelection,
  elementState,
  findingCounts,
  findingsByCheck,
  groupChecks,
  ruleAuditSummary,
  toggleCheck,
  unverifiedRules,
  cleanJurisdictionDetail,
  complianceGroups,
  complianceSummary,
  courtSummary,
  exhibitSummary,
  jurisdictionLabel,
  matterFilterOptions,
  sessionStatus,
  sessionSubtitle,
  sortSessions,
  truncationNotice,
  usesMunicipality,
  challengeCounts,
  challengeSummary,
  copyTextForChallenge,
  coverageSummary,
  materialsByOrigin,
  rankedChallenges,
  replaceChallenge,
  rerunSummary,
  revisionTargets,
  targetLabel,
  updatePlanItem,
} from "../src/components/argumentGym.js";

const CHALLENGES = [
  { id: 1, ordinal: 1, severity: "medium", importance: 60, disposition: "addressed", target: { blockKey: "defenses", section: "III.A" } },
  { id: 2, ordinal: 2, severity: "high", importance: 55, disposition: "open", target: { section: "III.B", paragraph: 14, page: 7 } },
  { id: 3, ordinal: 3, severity: "medium", importance: 90, disposition: "open", target: {} },
  { id: 4, ordinal: 4, severity: "low", importance: 99, disposition: "dismissed", target: {}, recurring: true },
];

test("open challenges rank above resolved ones, then by severity and importance", () => {
  const ranked = rankedChallenges(CHALLENGES).map((challenge) => challenge.id);
  assert.deepEqual(ranked, [2, 3, 1, 4]);
});

test("filters keep the resolved work reachable without mixing it into the queue", () => {
  assert.deepEqual(rankedChallenges(CHALLENGES, { filter: "open" }).map((item) => item.id), [2, 3]);
  assert.deepEqual(rankedChallenges(CHALLENGES, { filter: "resolved" }).map((item) => item.id), [1, 4]);
});

test("counts report dispositions and recurrences without inventing a score", () => {
  assert.deepEqual(challengeCounts(CHALLENGES), { total: 4, open: 2, addressed: 1, dismissed: 1, recurring: 1 });
  assert.equal(challengeSummary(CHALLENGES), "4 challenges: 2 open, 1 addressed, 1 dismissed.");
  assert.equal(challengeSummary([]), "No challenges were raised.");
});

test("a target reads as a place in the brief a person could turn to", () => {
  assert.equal(targetLabel({ section: "III.A", paragraph: 14, page: 7 }), "III.A, para. 14, p. 7");
  assert.equal(targetLabel({ blockKey: "defenses" }), "defenses");
  assert.equal(targetLabel({}), "");
});

test("coverage says what was searched and admits when it was thin", () => {
  assert.equal(coverageSummary({}), "No adversarial research ran for this brief.");
  assert.match(coverageSummary({ queries: ["a"], resultCount: 6, adequate: true }), /Coverage looks complete/);
  assert.match(
    coverageSummary({ queries: ["a"], resultCount: 1, adequate: false, gaps: ["Only 1 source result was retrieved."] }),
    /Gaps: Only 1 source result was retrieved\./,
  );
});

test("materials stay grouped by where they came from", () => {
  const groups = materialsByOrigin([
    { id: "matter:1", origin: "matter_document" },
    { id: "upload:2", origin: "upload" },
    { id: "matter:3", origin: "matter_document" },
  ]);
  assert.equal(groups.matter_document.length, 2);
  assert.equal(groups.upload.length, 1);
});

test("a rerun reports what recurred, what went away, and what is new", () => {
  assert.equal(rerunSummary({}), "");
  assert.equal(
    rerunSummary({ previousRunId: 3, recurring: [{}, {}], resolved: [{}], new: [] }),
    "Compared with the previous run: 2 still raised, 1 no longer raised.",
  );
  assert.equal(rerunSummary({ previousRunId: 3 }), "Nothing changed since the previous run.");
});

test("copied text carries the challenge, the response, and the authority behind it", () => {
  const text = copyTextForChallenge({
    categoryLabel: "Legal authority",
    opponentArgument: "The notice defect was waived.",
    target: { section: "III.A" },
    judgeAssessment: "A court would reach this.",
    suggestedResponse: "Waiver requires knowledge of the defect.",
    legalSources: [{ citation: "R.C. 1923.04" }, { title: "Untitled source" }],
  });
  assert.match(text, /Opposition point \(Legal authority\): The notice defect was waived\./);
  assert.match(text, /Targets: III\.A/);
  assert.match(text, /Waiver requires knowledge of the defect\./);
  assert.match(text, /Authority: R\.C\. 1923\.04; Untitled source/);
});

test("only block-anchored challenges can drive a revision", () => {
  const all = revisionTargets(CHALLENGES);
  assert.equal(all.canRevise, true);
  assert.deepEqual(all.actionable.map((item) => item.id), [1]);
  assert.deepEqual(all.copyOnly.map((item) => item.id), [2, 3, 4]);

  const narrowed = revisionTargets(CHALLENGES, [2, 3]);
  assert.equal(narrowed.canRevise, false);
  assert.deepEqual(narrowed.copyOnly.map((item) => item.id), [2, 3]);
});

test("editing one plan item leaves the others alone", () => {
  const plan = { plan: [{ blockKey: "a", include: true, instruction: "x" }, { blockKey: "b", include: true, instruction: "y" }] };
  const next = updatePlanItem(plan, "b", { include: false });
  assert.deepEqual(next.plan[0], { blockKey: "a", include: true, instruction: "x" });
  assert.equal(next.plan[1].include, false);
  assert.equal(plan.plan[1].include, true);
});

test("a disposition change replaces exactly one card", () => {
  const next = replaceChallenge(CHALLENGES, { ...CHALLENGES[1], disposition: "dismissed" });
  assert.equal(next[1].disposition, "dismissed");
  assert.equal(next[0].disposition, "addressed");
});

test("a run cannot start without the things it claims to test", () => {
  assert.deepEqual(canStartRun({ caseContext: "none" }), {
    ready: false,
    reason: "Upload the brief you want to test.",
  });
  assert.equal(canStartRun({ briefDocument: { id: 1 }, caseContext: "existing_case" }).ready, false);
  assert.equal(canStartRun({ briefDocument: { id: 1 }, caseContext: "existing_case", matterId: "LS-1" }).ready, true);
  assert.equal(canStartRun({ briefDocument: { id: 1 }, caseContext: "uploaded" }).ready, false);
  assert.equal(
    canStartRun({ briefDocument: { id: 1 }, caseContext: "uploaded", caseMaterials: [{ id: 2 }] }).ready,
    true,
  );
  assert.equal(canStartRun({ briefDocument: { id: 1 }, caseContext: "none" }).ready, true);
});


const COURT_TYPES = [
  { id: "municipal", label: "Municipal court", usesMunicipality: true },
  { id: "appellate", label: "Court of appeals", usesMunicipality: false },
];

test("a session row says which case it belongs to and how far along it is", () => {
  assert.equal(
    sessionSubtitle({ matterName: "Jane Tenant", briefTitle: "Answer" }),
    "Jane Tenant · Answer",
  );
  assert.equal(sessionSubtitle({ briefTitle: "Uploaded brief" }), "No case record · Uploaded brief");
  assert.equal(sessionStatus({ runCount: 0 }), "Not run yet");
  assert.equal(sessionStatus({ runCount: 2, openChallengeCount: 3 }), "2 runs · 3 open");
});

test("sessions are ordered by the most recent activity, run or edit", () => {
  const ordered = sortSessions([
    { id: 1, updatedAt: "2026-01-01T00:00:00" },
    { id: 2, lastRunAt: "2026-03-01T00:00:00", updatedAt: "2026-01-01T00:00:00" },
    { id: 3, updatedAt: "2026-02-01T00:00:00" },
  ]).map((item) => item.id);
  assert.deepEqual(ordered, [2, 3, 1]);
});

test("the case filter offers all, none, and each case that has a session", () => {
  assert.deepEqual(
    matterFilterOptions([{ id: "LS-1", name: "Jane Tenant" }]).map((item) => item.id),
    ["", "none", "LS-1"],
  );
});

test("a municipality belongs to a trial court and not to an appellate district", () => {
  assert.equal(usesMunicipality("municipal", COURT_TYPES), true);
  assert.equal(usesMunicipality("appellate", COURT_TYPES), false);
  // Falls back to the shared list when the server's court types are not loaded.
  assert.equal(usesMunicipality("common_pleas"), true);
  assert.equal(usesMunicipality("supreme"), false);
});

test("choosing an appellate court drops a municipality rather than sending it", () => {
  const cleaned = cleanJurisdictionDetail(
    { state: "Ohio", municipality: "Cleveland", division: "Eighth Appellate District", courtType: "appellate" },
    COURT_TYPES,
  );
  assert.deepEqual(cleaned, { state: "Ohio", division: "Eighth Appellate District", courtType: "appellate" });
});

test("a trial court keeps its municipality and county", () => {
  const cleaned = cleanJurisdictionDetail(
    { state: "Ohio", county: "Cuyahoga", municipality: "Cleveland", courtType: "municipal" },
    COURT_TYPES,
  );
  assert.equal(cleaned.municipality, "Cleveland");
  assert.equal(jurisdictionLabel(cleaned), "Cleveland, Cuyahoga County, Ohio");
  assert.equal(
    jurisdictionLabel({ state: "Ohio", division: "Eighth Appellate District" }),
    "Eighth Appellate District, Ohio",
  );
});

test("the court summary distinguishes a verified profile from a starter one", () => {
  assert.match(
    courtSummary({ court: { label: "Test Court", verification: "verified", source: "Local Rule 3.1" } }),
    /Checked against Local Rule 3\.1\./,
  );
  assert.match(
    courtSummary({ court: { label: "Test Court", verification: "unverified" } }),
    /Unverified starter profile/,
  );
  assert.equal(courtSummary({ detection: { mode: "off" } }), "Filing-format rules are off for this session.");
  assert.match(courtSummary({ detection: { reason: "Nothing matched." } }), /Nothing matched\./);
});

test("compliance separates rejections, checks, and what could not be measured", () => {
  const compliance = {
    checked: true,
    findings: [
      { severity: "error", ruleCode: "E900" },
      { severity: "warning", ruleCode: "W910" },
      { severity: "info", ruleCode: "I953" },
      { severity: "info", ruleCode: "I950" },
    ],
  };
  const groups = complianceGroups(compliance);
  assert.equal(groups.errors.length, 1);
  assert.equal(groups.warnings.length, 1);
  assert.equal(groups.unmeasured.length, 2);
  assert.equal(
    complianceSummary(compliance),
    "1 would be rejected, 1 to check, 2 could not be measured.",
  );
});

test("an unchecked document reports why, and a clean one says so plainly", () => {
  assert.equal(
    complianceSummary({ checked: false, reason: "No court profile is selected." }),
    "No court profile is selected.",
  );
  assert.equal(
    complianceSummary({ checked: true, findings: [] }),
    "The document meets every filing rule on file for this court.",
  );
});

test("an upload that was split says where the brief stopped", () => {
  assert.equal(exhibitSummary({}), "");
  assert.match(
    exhibitSummary({
      split: { briefPageCount: 12, boundaryReason: "The certificate of service on page 12 ends the brief.", exhibits: [{}, {}] },
    }),
    /Read pages 1-12 as the brief and split 2 attachments/,
  );
});

test("a brief too long to read whole says so instead of reporting a clean run", () => {
  assert.equal(truncationNotice({}), "");
  assert.match(truncationNotice({ truncated: true }), /Only the earlier part was analyzed/);
});


const CATALOG = [
  { id: "adversarial", label: "Opponent, judge, and coach", category: "argument", defaultEnabled: true },
  { id: "rule_elements", label: "Elements of the rules invoked", category: "argument", defaultEnabled: true },
  { id: "pleading_form", label: "Form of the pleading", category: "form", defaultEnabled: true },
  { id: "passive_voice", label: "Passive voice", category: "language", defaultEnabled: false },
];

test("checks are grouped the way an author reads them", () => {
  const groups = groupChecks(CATALOG);
  assert.deepEqual(groups.map((group) => group.id), ["argument", "form", "language"]);
  assert.equal(groups[0].label, "Argument");
  assert.equal(groups[0].checks.length, 2);
});

test("a session that has chosen nothing starts from the defaults, not from nothing", () => {
  assert.deepEqual(effectiveSelection(null, CATALOG), ["adversarial", "rule_elements", "pleading_form"]);
  assert.deepEqual(effectiveSelection(null, CATALOG, ["grammar"]), ["grammar"]);
  // An explicit empty choice is respected as an empty choice.
  assert.deepEqual(effectiveSelection([], CATALOG), []);
});

test("toggling a check adds or removes exactly that one", () => {
  assert.deepEqual(toggleCheck(["a", "b"], "c"), ["a", "b", "c"]);
  assert.deepEqual(toggleCheck(["a", "b"], "a"), ["b"]);
});

test("a check that could not run is counted separately from one turned off", () => {
  const checksRun = [
    { id: "a", status: "on" },
    { id: "b", status: "on" },
    { id: "c", status: "off" },
    { id: "d", status: "unavailable" },
  ];
  assert.equal(checkStatusSummary(checksRun), "2 ran, 1 off, 1 could not run.");
  assert.deepEqual(checksByStatus(checksRun, "unavailable").map((item) => item.id), ["d"]);
  assert.equal(checkStatusSummary([]), "");
});

test("findings stay attributed to the check that produced them", () => {
  const groups = findingsByCheck(
    {
      grammar: { findings: [{ severity: "warning" }, { severity: "info" }] },
      pleading_form: { findings: [{ severity: "error" }] },
      readability: { findings: [], summary: "Flesch-Kincaid 12." },
    },
    [
      { id: "grammar", label: "Grammar and mechanics" },
      { id: "pleading_form", label: "Form of the pleading" },
      { id: "readability", label: "Readability" },
    ],
  );
  assert.deepEqual(groups.map((group) => group.id), ["pleading_form", "grammar", "readability"]);
  assert.equal(groups[0].label, "Form of the pleading");
  assert.equal(groups[2].summary, "Flesch-Kincaid 12.");
  assert.deepEqual(findingCounts(groups), { errors: 1, warnings: 1, infos: 1 });
});

test("an element says separately whether it is pleaded and whether it is supported", () => {
  assert.equal(elementState({ pled: "yes", supported: "no" }), "pleaded, unsupported");
  assert.equal(
    elementState({ pled: "no", supported: "nothing_supplied" }),
    "not pleaded, nothing supplied supports it",
  );
  assert.equal(elementState({ pled: "partial" }), "partly pleaded");
  assert.equal(elementState({}), "unknown");
});

test("the rule audit summary counts elements the brief did not carry", () => {
  assert.equal(ruleAuditSummary([]), "No maintained rule was invoked by this brief.");
  assert.equal(
    ruleAuditSummary([{ citation: "R.C. 1923.04", unmetCount: 2 }, { citation: "R.C. 5321.04", unmetCount: 0 }]),
    "2 rules invoked; 2 elements not carried.",
  );
  assert.equal(
    ruleAuditSummary([{ citation: "R.C. 1923.04", unmetCount: 0 }]),
    "1 rule invoked; every element on file is carried.",
  );
});

test("an unverified element list is named so a reader knows what it rests on", () => {
  assert.deepEqual(
    unverifiedRules([
      { citation: "R.C. 1923.04", verification: "unverified" },
      { citation: "R.C. 5321.04", verification: "verified" },
    ]),
    ["R.C. 1923.04"],
  );
});

test("checklist outcomes are counted in the advocate's own words", () => {
  assert.equal(checklistSummary({}), "");
  assert.equal(
    checklistSummary({ results: [{ outcome: "pass" }, { outcome: "fail" }, { outcome: "needs_review" }] }),
    "1 pass, 1 fail, 1 needs review",
  );
});

test("a checklist is written as lines and comes back as numbered items", () => {
  const items = checklistItemsFromText("- Every date is in a document.\n2. Each authority is good law.\n\n  \n");
  assert.deepEqual(items, [
    { id: "item-1", text: "Every date is in a document." },
    { id: "item-2", text: "Each authority is good law." },
  ]);
  assert.equal(checklistItemsToText(items), "Every date is in a document.\nEach authority is good law.");
});


test("a run is finished only when it says so", () => {
  assert.equal(isRunFinished({ status: "complete" }), true);
  assert.equal(isRunFinished({ status: "failed" }), true);
  assert.equal(isRunFinished({ status: "running" }), false);
  assert.equal(isRunFinished({ status: "pending" }), false);
  assert.equal(isRunFinished(null), false);
});

test("progress names the stage rather than showing an unlabelled wait", () => {
  assert.match(runProgressLabel({ status: "pending", stageTrace: [] }), /^Starting…/);
  assert.match(
    runProgressLabel({ status: "running", stageTrace: [{ stage: "compliance" }, { stage: "opponent" }] }),
    /Opposing counsel is building its attacks…/,
  );
  // An unmapped stage still reads as words rather than a key.
  assert.match(runProgressLabel({ status: "running", stageTrace: [{ stage: "some_new_stage" }] }), /some new stage/);
});

test("a finished run shows no progress, and a failed one shows why", () => {
  assert.equal(runProgressLabel({ status: "complete", stageTrace: [{ stage: "coach" }] }), "");
  assert.equal(runProgressLabel({ status: "failed", error: "This brief has no readable text." }), "This brief has no readable text.");
});

test("the progress bar never claims to be finished before the run is", () => {
  assert.equal(runProgressFraction({ stageTrace: [] }), 0);
  assert.ok(runProgressFraction({ stageTrace: new Array(50).fill({ stage: "x" }) }) <= 0.95);
  assert.ok(RUN_POLL_MS > 0);
});


test("a filter with nothing behind it is not offered", () => {
  const allOpen = [{ id: 1, disposition: "open" }, { id: 2, disposition: "open" }];
  // One bucket is not a choice; showing "Open | All | Handled" over an
  // all-open run is three buttons that do the same thing.
  assert.deepEqual(availableFilters(allOpen), []);
  assert.deepEqual(availableFilters([]), []);

  const mixed = [{ id: 1, disposition: "open" }, { id: 2, disposition: "addressed" }];
  assert.deepEqual(availableFilters(mixed).map((f) => f.id), ["all", "open", "resolved"]);
  assert.deepEqual(availableFilters(mixed).map((f) => f.count), [2, 1, 1]);
});

test("the default filter lands where the challenges actually are", () => {
  assert.equal(defaultFilter([{ disposition: "open" }, { disposition: "addressed" }]), "open");
  // Everything handled: "open" would open on an empty list.
  assert.equal(defaultFilter([{ disposition: "addressed" }, { disposition: "dismissed" }]), "all");
  assert.equal(defaultFilter([]), "open");
});

test("an empty list explains itself instead of blaming a filter", () => {
  assert.match(emptyStateMessage([], "all"), /not a finding that the brief is sound/);
  assert.equal(emptyStateMessage([{ disposition: "addressed" }], "open"), "Every challenge from this run has been handled.");
  assert.equal(emptyStateMessage([{ disposition: "open" }], "resolved"), "Nothing has been handled yet.");
});

test("actions that read a run are unavailable while it is still being produced", () => {
  assert.equal(runActionsDisabled({ run: { status: "running" } }), true);
  assert.equal(runActionsDisabled({ run: { status: "pending" } }), true);
  assert.equal(runActionsDisabled({ run: { status: "complete" } }), false);
  assert.equal(runActionsDisabled({ run: { status: "complete" }, busy: true }), true);
  assert.equal(runActionsDisabled({}), false);
});

test("a long filename is shortened rather than pushing the layout sideways", () => {
  assert.equal(shortTitle("answer.docx"), "answer.docx");
  const long = "2026-09-01 Draft Answer and Counterclaims FINAL v3 reviewed.docx";
  const short = shortTitle(long);
  assert.ok(short.length <= 52, short);
  assert.ok(short.endsWith(".docx"), short);
  assert.ok(short.includes("…"));
  assert.equal(shortTitle("no-extension-but-really-quite-long-indeed-and-then-some-more"), "no-extension-but-really-quite-long-indeed-and-then-…");
});


test("a case is named by its client, not only by its docket number", () => {
  // matter_to_dict calls the name `client`; reading `clientName` rendered an
  // empty label beside a bare id.
  assert.equal(caseLabel({ client: "Jane Tenant", caseNumber: "26-0123" }), "Jane Tenant — 26-0123");
  assert.equal(caseLabel({ client: "Jane Tenant", id: "LS-9" }), "Jane Tenant — LS-9");
  assert.equal(caseLabel({ id: "26-0123" }), "26-0123");
  assert.equal(caseLabel({ client: "Jane Tenant" }), "Jane Tenant");
  assert.equal(caseLabel({}), "Untitled case");
  assert.deepEqual(caseOptions([{ id: "LS-1", client: "Jane Tenant", caseNumber: "26-0123" }]), [
    { id: "LS-1", label: "Jane Tenant — 26-0123" },
  ]);
});


test("results are only drawn once there are results", () => {
  assert.equal(runView(null), "setup");
  // A run exists the instant it is started. Drawing results here reported
  // "this run raised no challenges" over a run that had not looked yet.
  assert.equal(runView({ status: "pending", challenges: [] }), "running");
  assert.equal(runView({ status: "running", challenges: [] }), "running");
  assert.equal(runView({ status: "complete", challenges: [] }), "results");
  assert.equal(runView({ status: "failed", error: "no readable text" }), "failed");
});


test("evidence is counted so a card can offer it rather than print it", () => {
  assert.equal(evidenceCount({ legalSources: [{}, {}], recordSources: [{}] }), 3);
  assert.equal(evidenceCount({}), 0);
});

test("severity is a tone, carried by colour rather than by heavy type", () => {
  assert.equal(severityTone({ severity: "high" }), "high");
  assert.equal(severityTone({ severity: "nonsense" }), "medium");
  assert.equal(severityTone({}), "medium");
});

test("a suggested fix reads as steps when it has several", () => {
  assert.deepEqual(
    responseBullets("Cite the notice statute. Attach the certified mail receipt. Say when service ran."),
    ["Cite the notice statute.", "Attach the certified mail receipt.", "Say when service ran."],
  );
  // One sentence stays one sentence rather than becoming a one-item list.
  assert.deepEqual(responseBullets("Narrow the request for relief."), ["Narrow the request for relief."]);
  assert.deepEqual(responseBullets(""), []);
  assert.equal(responseBullets("A. B. C. D. E. F.", { max: 2 }).length, 2);
});

test("long model prose is cut to something scannable, and says it was cut", () => {
  const short = clamp("Short enough.");
  assert.equal(short.text, "Short enough.");
  assert.equal(short.truncated, false);
  const long = clamp("word ".repeat(120));
  assert.ok(long.truncated);
  assert.ok(long.text.length <= 221, long.text.length);
  assert.ok(long.text.endsWith("…"));
});

test("the audit sidebar is a number and a word per area", () => {
  const badges = auditBadges({
    compliance: { checked: true, findings: [{ severity: "error" }, { severity: "info" }] },
    checkResults: { grammar: { findings: [{ severity: "warning" }] } },
    checksRun: [{ id: "grammar", label: "Grammar", status: "on" }],
    ruleAudit: [{ unmetCount: 7, elements: [] }],
    checklistResults: { results: [{ outcome: "fail" }, { outcome: "pass" }] },
  });
  const byId = Object.fromEntries(badges.map((b) => [b.id, b]));
  assert.equal(byId.compliance.count, 1);
  assert.equal(byId.compliance.tone, "high");
  assert.equal(byId.compliance.note, "1 not measurable");
  assert.equal(byId.checks.count, 1);
  assert.equal(byId.rules.count, 7);
  assert.equal(byId.rules.note, "1 rule invoked");
  assert.equal(byId.checklist.count, 1);
});

test("an area with nothing to report is absent rather than shown as zero", () => {
  assert.deepEqual(auditBadges({}), []);
});

test("checks that did not run are one badge, not a section", () => {
  assert.equal(skippedChecksSummary([{ status: "on" }]), null);
  const summary = skippedChecksSummary([
    { id: "a", status: "off" },
    { id: "b", status: "unavailable" },
    { id: "c", status: "unavailable" },
  ]);
  assert.equal(summary.label, "2 could not run · 1 off");
  assert.equal(summary.unavailable.length, 2);
});

test("optional language review keeps errors and unknown checks in the main review", async () => {
  const { reviewCheckGroups } = await import("../src/components/argumentGym.js");
  const error = { severity: "error", message: "Missing closing quote" };
  const note = { severity: "info", message: "Consider a shorter sentence" };
  const run = {
    checksRun: [{ id: "grammar", label: "Grammar", status: "on" }],
    checkResults: {
      grammar: { findings: [note, error] },
      new_check: { findings: [note] },
      custom_style: { findings: [note] },
    },
  };
  const before = JSON.stringify(run);
  const groups = reviewCheckGroups(run, [{ id: "custom_style", category: "language" }]);
  assert.deepEqual(groups.primary.find((group) => group.id === "grammar").findings, [error]);
  assert.ok(groups.primary.some((group) => group.id === "new_check"));
  assert.equal(groups.styleCount, 2);
  assert.deepEqual(groups.style.find((group) => group.id === "grammar").findings, [note]);
  assert.equal(JSON.stringify(run), before);
});

test("style review preserves off, unavailable, and checked with no findings", async () => {
  const { reviewCheckGroups } = await import("../src/components/argumentGym.js");
  const checksRun = [
    { id: "grammar", status: "on" },
    { id: "passive_voice", status: "off" },
    { id: "readability", status: "unavailable", reason: "No text" },
  ];
  const groups = reviewCheckGroups({ checksRun, checkResults: { grammar: { findings: [] } } });
  assert.equal(groups.styleCount, 0);
  assert.equal(groups.style.length, 1);
  assert.deepEqual(groups.styleChecks, checksRun);
  assert.equal(checkStatusSummary(groups.styleChecks), "1 ran, 1 off, 1 could not run.");
  assert.deepEqual(reviewCheckGroups().styleChecks, []);
});

test("the priority overview caps the existing ranking and keeps the rest reachable", async () => {
  const { challengeReview, challengePreview } = await import("../src/components/argumentGym.js");
  const challenges = [
    { id: 1, severity: "low", disposition: "open" },
    { id: 2, severity: "high", disposition: "addressed" },
    { id: 3, severity: "high", disposition: "open" },
    { id: 4, severity: "medium", disposition: "open" },
    { id: 5, severity: "medium", disposition: "open" },
  ];
  const before = JSON.stringify(challenges);
  const initial = challengeReview(challenges);
  assert.deepEqual(initial.shown.map((item) => item.id), [3, 4, 5]);
  assert.equal(initial.hidden, 1);
  assert.equal(initial.total, 4);
  const expanded = challengeReview(challenges, { showAll: true, selectedId: 1 });
  assert.equal(expanded.selected.id, 1);
  assert.equal(expanded.previous.id, 5);
  assert.equal(expanded.next, null);
  assert.equal(expanded.hidden, 0);
  assert.equal(challengeReview(challenges, { selectedId: 2 }).selected, null);
  assert.equal(challengeReview(challenges, { filter: "resolved", selectedId: 2 }).selected.id, 2);
  assert.equal(JSON.stringify(challenges), before);
  assert.equal(challengeReview([]).total, 0);
  assert.equal(challengeReview([]).next, null);
  const preview = challengePreview({ opponentArgument: "Actual argument", recommendation: "Actual recommendation" });
  assert.equal(preview.title, "Actual argument");
  assert.equal(preview.action, "Actual recommendation");
});


test("handling the last open challenge keeps a route to handled findings", () => {
  const filters = availableFilters([{ id: 1, disposition: "addressed" }], "open");
  assert.deepEqual(filters.map((filter) => [filter.id, filter.count]), [["all", 1], ["open", 0], ["resolved", 1]]);
  assert.deepEqual(availableFilters([], "open"), []);
});

test("the matrix separates pleading from support and preserves uncertain provenance", async () => {
  const { elementMatrix } = await import("../src/components/argumentGym.js");
  const rules = [{ slug: "sample", verification: "starter", elements: [
    { id: "notice", label: "Notice", pled: "yes", supported: "nothing_supplied", materialIds: [7, 8], unmet: true },
    { id: "timing", label: "Timing", pled: "unknown", supported: "unknown" },
    { id: "relief", label: "Relief", pled: "yes", supported: "yes", needsRecordSupport: false },
  ] }];
  const before = JSON.stringify(rules);
  const matrix = elementMatrix(rules, [{ id: "7", title: "Sample notice" }]);
  assert.match(matrix[0].verificationLabel, /Unverified/);
  assert.equal(matrix[0].rows[0].pleadedLabel, "pleaded");
  assert.equal(matrix[0].rows[0].supportedLabel, "nothing supplied supports it");
  assert.deepEqual(matrix[0].rows[0].materials, ["Sample notice", "Material 8 (details unavailable)"]);
  assert.equal(matrix[0].rows[1].pleadedLabel, "Unknown / not assessed");
  assert.equal(matrix[0].rows[1].supportedLabel, "Unknown / not assessed");
  assert.equal(matrix[0].rows[2].supportedLabel, "Not required by this profile");
  assert.equal(JSON.stringify(rules), before);
});

test("overview lenses describe the saved run, including off and unavailable checks", async () => {
  const { overviewLenses } = await import("../src/components/argumentGym.js");
  const lenses = overviewLenses({
    challenges: [{ id: 1, category: "record_conflict", disposition: "addressed" }, { id: 2, category: "legal_authority" }],
    checksRun: [{ id: "rule_elements", status: "off" }, { id: "record_audit", status: "unavailable" }],
    compliance: { checked: true, findings: [{ severity: "info", message: "Unmeasured" }] },
    checkResults: { grammar: { findings: [{ severity: "error" }, { severity: "info" }] } },
  });
  assert.equal(lenses.find((lens) => lens.id === "argument").challenges.length, 2);
  assert.equal(lenses.find((lens) => lens.id === "record").challenges[0].disposition, "addressed");
  assert.match(lenses.find((lens) => lens.id === "record").status, /could not run/);
  assert.match(lenses.find((lens) => lens.id === "elements").status, /off/);
  assert.equal(lenses.find((lens) => lens.id === "filing").compliance.unmeasured.length, 1);
  assert.equal(lenses.find((lens) => lens.id === "style").groups[0].findings.length, 1);
  assert.ok(overviewLenses().every((lens) => lens.status === "Execution not recorded."));
});

test("plain finding headings and verification groups precede optional prose", async () => {
  const { challengePreview, challengeGroups, rankedChallenges } = await import("../src/components/argumentGym.js");
  const rows = [
    { id: 1, severity: "high", confidence: "low", importance: 100 },
    { id: 2, severity: "medium", confidence: "high" },
    { id: 3, severity: "high", confidence: "high" },
    { id: 4, severity: "low", confidence: "high" },
  ];
  const groups = challengeGroups(rankedChallenges(rows));
  assert.deepEqual(groups.map(g => g.label), ["Address first", "Address next", "Verify before changing", "Optional improvements"]);
  assert.deepEqual(groups.flatMap(g => g.challenges.map(c => c.id)), [3, 2, 1, 4]);
  assert.equal(challengePreview({ finding: "The notice element has weak support", opponentArgument: "An adversarial speech" }).title, "The notice element has weak support");
});

test("explicit verification and strategy actions do not become instructions to revise", async () => {
  const { priorityForChallenge } = await import("../src/components/argumentGym.js");
  assert.equal(priorityForChallenge({ reviewAction: "verify", confidence: "high", severity: "high" }).id, "verify");
  assert.equal(priorityForChallenge({ reviewAction: "strategy", severity: "high" }).label, "Decide the approach");
});

test("handled findings stay below open work across priority groups", async () => {
  const { challengeGroups, rankedChallenges } = await import("../src/components/argumentGym.js");
  const groups = challengeGroups(rankedChallenges([
    { id: 1, severity: "high", disposition: "addressed" },
    { id: 2, severity: "medium", disposition: "open" },
  ]));
  assert.deepEqual(groups.map(g => g.label), ["Address next", "Addressed"]);
});

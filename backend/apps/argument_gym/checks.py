"""The catalog of checks a session can run, and the author's choice among them.

Every check the gym can make is declared here with what it needs to run. The
author picks; nothing is added to a run because it seemed useful. A check that
was turned off and a check that could not apply are different things and are
reported differently, because "no findings" from a check that never ran is the
one result that must never look like a clean bill.

`requires` names a precondition the session either has or does not:

* ``native_draft`` -- the brief is a CLARA document, so the drafting
  validation rules have a template and a session to read.
* ``court_profile`` -- a court's filing rules are selected or detected.
* ``case_record`` -- there are case materials to check the brief against.
* ``checklist`` -- the author attached one of their own checklists.
"""

from dataclasses import dataclass, field


DETERMINISTIC = "deterministic"
MODEL = "model"


@dataclass(frozen=True)
class CheckDefinition:
    id: str
    label: str
    description: str
    kind: str
    category: str
    default_enabled: bool = True
    requires: tuple = ()
    settings_help: str = ""

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "category": self.category,
            "defaultEnabled": self.default_enabled,
            "requires": list(self.requires),
            "settingsHelp": self.settings_help,
        }


CHECK_CATALOG = (
    CheckDefinition(
        id="adversarial",
        label="Opposing counsel, a judge, and a coach",
        description=(
            "The core of the gym: opposing counsel makes the strongest arguments available "
            "against the brief, a judge weighs them, and a coach proposes answers."
        ),
        kind=MODEL,
        category="argument",
    ),
    CheckDefinition(
        id="record_audit",
        label="Check the brief against the case record",
        description="Whether the case materials actually establish what the brief asserts.",
        kind=MODEL,
        category="argument",
        requires=("case_record",),
    ),
    CheckDefinition(
        id="rule_elements",
        label="Audit the elements of the rules invoked",
        description=(
            "Detects the rules the brief cites or invokes by name, then audits each element "
            "of those rules: is it pleaded, and is it supported."
        ),
        kind=MODEL,
        category="argument",
    ),
    CheckDefinition(
        id="custom_checklist",
        label="Custom checklist",
        description=(
            "Applies review questions you wrote. An item can look things up -- authority, the "
            "case record, passages of the brief -- rather than answering from the brief alone. "
            "Attach a checklist for this to run."
        ),
        kind=MODEL,
        category="argument",
        default_enabled=False,
        requires=("checklist",),
    ),
    CheckDefinition(
        id="court_formatting",
        label="This court's filing rules",
        description="Required elements, type size, spacing, margins, and page limits for the selected court.",
        kind=DETERMINISTIC,
        category="form",
        requires=("court_profile",),
    ),
    CheckDefinition(
        id="pleading_form",
        label="Form of the pleading",
        description=(
            "Conventions of practice rather than any one court's rules: numbered paragraphs "
            "running in order, a prayer for relief, a signature block, exhibit references that "
            "resolve, no placeholder left in the text."
        ),
        kind=DETERMINISTIC,
        category="form",
    ),
    CheckDefinition(
        id="draft_validation",
        label="Draft-mode validation",
        description=(
            "The same checks Draft mode runs: unresolved template data and placeholders, draft "
            "structure, rendered Word consistency, citation linting, source support, and "
            "filing-package consistency."
        ),
        kind=DETERMINISTIC,
        category="form",
        requires=("native_draft",),
    ),
    CheckDefinition(
        id="grammar",
        label="Grammar and mechanics",
        description="Doubled words, missing sentence spacing, unbalanced quotes and parentheses.",
        kind=DETERMINISTIC,
        category="language",
    ),
    CheckDefinition(
        id="confused_words",
        label="Misspelled and easily confused words",
        description=(
            "Not a dictionary spell check, on purpose: a general dictionary flags half of every "
            "case name. This looks for the words legal writing actually gets wrong, and for real "
            "words used in place of other real words."
        ),
        kind=DETERMINISTIC,
        category="language",
    ),
    CheckDefinition(
        id="passive_voice",
        label="Passive voice",
        description=(
            "Reported as a nudge, never an error. Phrases a court expects to read are on an "
            "accepted list you can add to for this session."
        ),
        kind=DETERMINISTIC,
        category="language",
        default_enabled=False,
        settings_help="acceptedPassivePhrases: phrases this court expects, which the check stays quiet about.",
    ),
    CheckDefinition(
        id="readability",
        label="Readability",
        description="Sentence length and reading-level measures, reported as several formulas rather than one score.",
        kind=DETERMINISTIC,
        category="language",
        default_enabled=False,
    ),
)

CHECKS_BY_ID = {check.id: check for check in CHECK_CATALOG}
# Stored when the author turns every check off. An empty list cannot carry that:
# a new session also has an empty list, and there it means "the defaults". Making
# the two the same would silently re-enable everything the author switched off.
NONE_SELECTED = "__none__"
DEFAULT_CHECK_IDS = [check.id for check in CHECK_CATALOG if check.default_enabled]
REQUIREMENT_REASONS = {
    "native_draft": "This brief was uploaded rather than drafted here, so there is no template or draft session to validate.",
    "court_profile": "No court is selected for this session, so there are no filing rules to apply.",
    "case_record": "No case materials are in scope for this session.",
    "checklist": "No checklist is attached to this session.",
}


def catalog():
    return [check.to_dict() for check in CHECK_CATALOG]


def normalize_selection(selected):
    """The author's explicit choice, keeping only checks that exist.

    An empty selection means the catalog defaults. It never means "run
    everything": widening a choice the author made is the same error as
    narrowing it.
    """
    if selected and NONE_SELECTED in selected:
        return []
    if not selected:
        return list(DEFAULT_CHECK_IDS)
    return [check_id for check_id in dict.fromkeys(selected) if check_id in CHECKS_BY_ID]


def plan_checks(selected, capabilities):
    """Decide, before anything runs, which checks will run and why the rest will not."""
    chosen = set(normalize_selection(selected))
    plan = []
    for check in CHECK_CATALOG:
        if check.id not in chosen:
            plan.append({**check.to_dict(), "status": "off", "reason": "You turned this check off for this session."})
            continue
        unmet = [name for name in check.requires if not capabilities.get(name)]
        if unmet:
            plan.append(
                {
                    **check.to_dict(),
                    "status": "unavailable",
                    "reason": " ".join(REQUIREMENT_REASONS.get(name, name) for name in unmet),
                }
            )
            continue
        plan.append({**check.to_dict(), "status": "on", "reason": ""})
    return plan


def will_run(plan, check_id):
    return any(entry["id"] == check_id and entry["status"] == "on" for entry in plan)

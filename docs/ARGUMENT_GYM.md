# Argument Gym

The Argument Gym reads a brief the way an opponent would, weighs what it finds
the way a judge would, and answers it the way a colleague would. It produces a
ranked list of **challenges** — never a score, and never an edit.

It runs in two places, over one pipeline:

- **From Draft mode.** The **Stress test** button in the editor persists the
  open draft, then tests the document that is actually stored. Challenges land
  on block keys, so they can feed the existing revision machinery.
- **As its own mode.** Upload a brief, choose the case context, and run. The
  brief can be an external DOCX, PDF, or text file that CLARA never wrote.

## Sessions

A `GymWorkspace` is a session: one brief, the case context behind it, and every
run over it. Sessions are listed in the panel's left column, filterable by the
case they belong to (`?matterId=`, with `none` for standalone tests) and
searchable by brief or client name. Opening one returns its most recent complete
run with its challenges and dispositions intact, so a session is somewhere to
come back to rather than something to redo. **New** starts a fresh one.

## The author chooses the checks

Every check the gym can make is declared in `apps/argument_gym/checks.py` with
what it needs to run, served at `GET /api/argument-gym/checks/`, and selected per
session. Nothing is added to a run because it seemed useful.

Three states, kept distinct because they mean different things:

| State | Meaning |
| --- | --- |
| `on` | The author selected it and it ran. |
| `off` | The author turned it off. It produced nothing, and nothing is claimed. |
| `unavailable` | Selected, but its precondition is absent — an uploaded brief has no draft session to validate, a session with no court has no filing rules. **This is not a pass**, and the panel says so under its own heading. |

An empty `enabled_checks` means the catalog's defaults, which is what a new
session has. Turning *every* check off stores a sentinel instead, because an
empty list and a new session must not mean the same thing — otherwise the next
run would silently re-enable everything the author switched off.

### The catalog

| Check | Kind | Needs |
| --- | --- | --- |
| Opponent, judge, and coach | AI | — |
| Brief against the case record | AI | case materials |
| Elements of the rules the brief invoked | AI | — |
| Your own checklist | AI | a checklist |
| This court's filing rules | deterministic | a court profile |
| Form of the pleading | deterministic | — |
| Draft-mode validation | deterministic | a native draft |
| Grammar and mechanics | deterministic | — |
| Commonly misspelled and confused words | deterministic | — |
| Passive voice | deterministic | — |
| Readability | deterministic | — |

**Draft-mode validation** is the same `apps.validation.services.validate_document`
Draft mode runs — template data, unresolved placeholders, structure, rendered
DOCX consistency, citation linting, source support, package consistency. It needs
a `DraftDocument`, so on an uploaded brief it reports itself unavailable rather
than silently passing.

**Form of the pleading** (`apps/validation/pleading_form.py`, codes
E/W/I1000-1099) is deliberately separate from the court's own rules: numbered
paragraphs running in order, a prayer for relief, a signature block, exhibit
references that resolve against what was actually attached, no placeholder left
in the text. A finding here never cites a court; a finding from
`court_formatting` always does.

**Language** (`apps/validation/language.py`, codes E/W/I1100-1199) has three
deliberate limits, each of them the reason the module exists:

- *No dictionary spell check.* A general dictionary flags "replevin", "forcible
  entry and detainer", "estoppel", and half of every case name, and an advocate
  who dismisses forty false positives stops reading the check. What runs instead
  is a curated list of the words legal writing actually gets wrong, plus
  real-word confusions (principal/principle) raised only when *both* appear.
- *Passive voice is not an error.* "Service was perfected" is the register a
  court expects. Accepted phrases live in
  `content/drafting-rules/checks/legal-language.yaml`, a session can add the
  phrases its court expects through `checkSettings.passive_voice`, and everything
  else is reported at info severity.
- *Only high-precision grammar.* Doubled words, missing sentence spacing,
  unbalanced delimiters. Subject-verb agreement is left out because getting it
  wrong on legal prose is worse than not checking.

## The pipeline

```text
brief ingestion
  -> filing-format compliance           (deterministic, no model call)
  -> the author's deterministic checks  (form, language, draft validation)
  -> argument map
  -> brief-to-record support check      (only when case materials exist)
  -> adversarial research queries
  -> augmented_search over the existing sources
  -> opponent generates the strongest attacks
  -> elements of the rules the brief invoked
  -> the author's own checklist, with the lookups its items need
  -> an independent judge filters and ranks them
  -> a coach proposes responses
  -> stored GymChallenge records
  -> the run's opening assessment
```

Compliance runs first and without a model, so an advocate gets that answer even
if every model call after it fails.

Opponent, judge, and coach are **separate model calls**. One call asked to
attack, weigh, and answer produces attacks it has already decided are
answerable, which is the failure this feature exists to prevent.

Every stage has a deterministic fallback and a single bounded repair back to it
(`apps.ai.tool_loop.run_tool_with_repair`), so a run always produces reviewable
output and the whole pipeline is testable with `AI_DRAFTING_ENABLED=False`.

Prompts are file-backed in `prompts/argument_gym.*.yaml`. Changing a stage's
required variables means changing the YAML and the call site in
`apps/argument_gym/pipeline.py` together.

## Models

| Model | What it holds |
| --- | --- |
| `GymWorkspace` | One brief under test plus the case context it is tested against. A workspace with a `Matter` is governed by that case's access; a workspace without one is private to its owner. |
| `GymDocument` | The brief, or a piece of the case record. `source_type` says whether the text is uploaded, a `DraftDocument`, or a reference to a case-file document. An exhibit split out of a filing keeps its `page_range` and its `split_from`. |
| `CourtProfile` (in `apps.rules`) | A court's identity and its filing-format requirements, with its own verification status. It lives outside the gym because draft validation wants the same answers. |
| `GymRun` | One pass of the pipeline: the brief snapshot it read, which checks ran and why the rest did not, the findings each produced, the rule-element audit, the checklist results, the research it ran, its opening assessment, and how it compares with the previous run. |
| `GymChecklist` | An advocate's own review questions, in their words. |
| `LegalRuleProfile` (in `apps.rules`) | A rule, how to tell it was invoked, and the elements it requires — with its own verification status. |
| `GymChallenge` | One opposition argument, judged and answered, anchored to a passage. Carries the advocate's disposition, which survives reruns. |

Gym state is deliberately **not** `DraftDocument.validation_flags` or a
`ChatConversation`. A challenge has a disposition and a rerun history; a
validation flag and a chat message have neither.

## Runs are started, not awaited

A run is eight sequential model calls and several retrieval rounds — minutes,
not seconds. Holding the HTTP request open for that does not merely feel slow:
gunicorn kills the worker at its timeout, and **a killed worker returns no
headers at all**, so the browser reports a CORS failure rather than the timeout
it actually is. That is what `POST /runs/` did before this was fixed.

So the request starts the run on a background thread and returns it immediately:

| Status | Code | Meaning |
| --- | --- | --- |
| `pending` / `running` | 202 | Accepted. Poll `GET /runs/<id>/`. |
| `complete` | 200 | Finished. |
| `failed` | 502 | Recorded on the run with a reason. |

`GymRun.status` already existed for this; the pipeline is unchanged. Each stage
is saved to `stage_trace` as it finishes, so a client polling can name the stage
rather than showing an unlabelled wait.

A replica that dies mid-run would otherwise leave a row claiming to be running
forever, so `fail_if_stalled` reports a run past
`ARGUMENT_GYM_RUN_TIMEOUT_SECONDS` (default 30 minutes) as failed, saying it was
interrupted and that nothing was written to the draft.

`ARGUMENT_GYM_BACKGROUND_RUNS=False` runs the pipeline inline, which is how the
tests assert on a finished run.

## The opening assessment

Each run writes one paragraph, stored on the run and shown at the top of both
the panel and the stress-test report: whether the brief persuades as written,
and the two or three flaws that most need addressing. It carries a short verdict
phrase — a characterization such as "persuasive but exposed on the notice
defect", never a score, grade, or rating. The deterministic fallback builds the
same paragraph from the challenges' severities when no model is available.

## Jurisdiction and the court's filing rules

A session resolves two related things, each either detected or set by hand.

**Jurisdiction** (`jurisdiction_mode`) can be typed in: state, county, and then
either a municipality or a division, depending on the court type. A municipality
identifies a trial court and means nothing for an appellate district, so the
form asks for a division instead and a municipality sent for an appellate court
is dropped rather than stored.

**Whose filing rules apply** (`court_rule_mode`) is `auto`, `manual`, or `off`.
Automatic detection matches the brief's caption — and, failing that, the case
record — against maintained `CourtProfile` aliases. It is string matching, not
inference, so a run always reports the phrase that decided it, and reports
nothing rather than guessing when nothing matches. Only the first few thousand
characters are searched: a case cited on page nine must not outrank the court
the paper is filed in.

## Filing-format checks

`apps/validation/court_formatting.py` checks a document against the selected
profile with no model call: required elements for that pleading type, minimum
type size, permitted typefaces, line spacing, margins, and page limits. Rule
codes are E/W/I900-999.

Two rules govern the report:

- **An unverified profile can only warn.** Its requirements were not read off the
  court's own local rules, so its findings are downgraded and labelled as coming
  from a starter profile.
- **A property that could not be measured is reported as unmeasured, never as a
  pass.** A DOCX has no page count until something renders it; a scanned PDF has
  no type size to read. Silently skipping either would tell an advocate their
  fifty-page brief fits in fifteen.

Profiles are file-backed in `content/court-rules/*.yaml`, seeded into
`CourtProfile` by `sync_content_library`, and edited in Django admin under
**Court profiles**. Editing one there marks it `is_locally_edited`, and
re-seeding — even with `--update-court-rules` — skips it. See
[`content/court-rules/README.md`](../content/court-rules/README.md) for the
schema.

## Large filings and exhibits

A filed brief usually arrives with its exhibits attached, and most of the file
is not the brief. Pushing three hundred pages of a lease and a rent ledger
through a model to find the argument is both expensive and worse at finding it.

`split_brief_and_exhibits` reads the boundary off the pages themselves: a
certificate of service ends the brief, and an exhibit cover sheet starts the
attachments. An index of exhibits *inside* the brief does not count. Where
nothing marks the boundary, the first `BRIEF_PAGE_LIMIT` (30) pages are the
brief. The reason is recorded either way, so the split is reviewable.

Each attachment becomes its own `GymDocument` with `role="case_record"`,
`split_from` pointing at the upload, and its page range — so it is available as
material the record audit can read, and excludable like any other. A page limit
is then checked against the brief, not against what was stapled behind it.

Beyond the split, `MAX_BRIEF_CHARS` caps what any run reads, and a brief that
hit the cap says so rather than reporting a clean result on a partial read.

## Rules the brief invoked

A cited rule is a candidate for an element audit. Applicability and the party's
burden still need review before a missing element is treated as a defect.

`apps/rules/legal_rules.py` detects which maintained rules a brief invoked —
deterministically, by citation pattern or by a well-known phrase — and reports
which words decided it. A rule invoked only by a phrase is labelled as such:
"three-day notice" in a sentence about the other side's notice is not the same as
citing the statute.

`apps/argument_gym/rule_audit.py` then asks two **separate** questions per
element: is it *pleaded*, and is it *supported*. An assertion is not support, and
the audit never merges the two. A deterministic pattern miss reports uncertain
wording rather than absence. Missing record documents and partial support remain
visible in the audit but do not by themselves create adverse challenge cards.
Phrase-only matches require an applicability review before any element becomes
a challenge. Conclusive adverse assessments can still become `GymChallenge`
records for the ranked cards, prep sheet, and revision plan.

### Reusing the decision tables

Where a published `DecisionTable` row already encodes a rule's requirements, a
profile names it instead of restating it:

```yaml
decision_table_key: eviction_answer_issue_selection
decision_table_row: notice_defect
```

The row's `missing_facts` become elements, and the facts its conditions depend on
become elements too — the same requirements seen from the pleading side rather
than the issue-selection side. Those merge with the profile's own elements.

Profiles are file-backed in `content/legal-rules/*.yaml`, seeded into
`LegalRuleProfile`, and edited in Django admin under **Legal rule profiles**.
Like court profiles, each states its own `verification`, and only a verified
element list reports at error severity. **Every profile shipped here is
unverified**: the elements are substantive law, and a wrong element list tells an
advocate their pleading is complete when it is not.

## Your own checklist

A `GymChecklist` is prose, one review question per line. "Every date in the
statement of facts appears in a document in the file" is a legitimate item — and
answering it means going and reading the file.

So the model answering an item can look things up first, through three bounded,
read-only tools:

| Tool | What it reaches |
| --- | --- |
| `search_law` | The maintained research libraries, through `augmented_search` |
| `search_case_record` | The case materials this session is allowed to read |
| `quote_brief` | Passages of the brief itself |

The protocol is JSON in the message body rather than provider-native function
calling, matching the rest of the codebase: the same loop has to work against any
OpenAI-compatible endpoint. Lookups are capped per item, run under the session's
own matter access, and every one is reported with the query it asked — an
advocate can see what an item read before believing what it says. A failed item
becomes a challenge. Without a model, items are reported `needs_review` with the
passages that matched, never `pass`.

Checklists are the author's own; `shared: true` offers one to the deployment,
readable by everyone and editable only by its author.

## Anchors

A native draft is already addressable: every section is a `DocumentComponent`
with a stable key. An uploaded brief is not, so `apps/argument_gym/ingestion.py`
gives it a lightweight structure — section, paragraph, argument, asserted fact,
citation, requested relief — each with a run-local id and a locator:

```json
{"section": "III.A", "paragraph": 14, "page": 7, "excerpt": "..."}
```

DOCX structure comes from heading styles and paragraph numbering; PDF keeps page
locations. This is not a legal knowledge graph and should not grow into one: it
exists so a card can say where the problem is and a reader can find it again.

## Case materials

Existing case files are **referenced, never copied**. A run resolves matter
documents through `apps.matters.document_context`, under the same access control
as the rest of the app, and reads their text only at the moment a stage needs
it. The gym stores a pointer and — when the advocate excludes a document — that
decision. Ranking reuses the case-chat salience ranking rather than inventing a
second answer to the same question.

## Reruns

A rerun snapshots the current component versions and compares its challenges
against the previous run's by fingerprint. A challenge the advocate **dismissed**
carries that dismissal forward: the argument has not changed. A challenge marked
**addressed** that comes back is reopened and flagged as recurring, because the
brief moved and the challenge survived it. Challenges the previous run raised
that this one does not are reported as no longer raised.

## Artifacts

All three are projected from stored challenges, so they cannot disagree with
each other or with the cards:

- **Opposition prep sheet** — one row per challenge: likely opposition point,
  strongest authority, strongest adverse record material, current response,
  suggested response, remaining vulnerability.
- **Stress-test report** — executive summary, ranked vulnerabilities, challenges
  already handled well, unresolved research gaps, materials reviewed.
- **Revision plan** — block-scoped instructions. Actionable on a native draft
  through `apps.validation.revision.apply_revision_plan`; copyable text for an
  external brief.

The one model call among them writes the narrative summary, once per run.

## Applying a revision

Nothing auto-edits. "Add to revision plan" queues a challenge; opening the plan
shows editable, block-scoped instructions; applying it runs each one through
`regenerate_draft_block`, which records a `DraftOperation` and a new
`ComponentVersion`. The challenge is then marked addressed and linked to the
operation that answered it.

## API

| Method and path | Purpose |
| --- | --- |
| `GET /api/argument-gym/checks/` | The check catalog and its defaults |
| `GET POST /api/argument-gym/checklists/` | List or create your own checklists |
| `GET PATCH DELETE /api/argument-gym/checklists/<id>/` | Read, edit, or remove one |
| `GET /api/argument-gym/courts/` | Court profiles and which court types use a municipality |
| `GET /api/argument-gym/legal-rules/` | The rules the element audit recognizes, and what each requires |
| `GET POST /api/argument-gym/workspaces/` | List (filter with `matterId`, `q`) or create a session |
| `GET /api/argument-gym/workspaces/<id>/court-detection/` | What detection would pick for this brief, and why |
| `GET PATCH DELETE /api/argument-gym/workspaces/<id>/` | Read a session with its latest run; set jurisdiction, court, selected checks, check settings, and checklist; or remove it |
| `GET POST /api/argument-gym/workspaces/<id>/documents/` | Upload a brief or case file, or attach a draft |
| `GET POST /api/argument-gym/workspaces/<id>/materials/` | List available case materials, or exclude one |
| `GET POST /api/argument-gym/workspaces/<id>/runs/` | List runs, or launch one |
| `PATCH DELETE /api/argument-gym/documents/<id>/` | Rename or remove a gym document |
| `GET /api/argument-gym/runs/<id>/` | One run with its challenges |
| `GET /api/argument-gym/runs/<id>/artifacts/<prep_sheet\|report>/` | Output artifacts |
| `GET POST /api/argument-gym/runs/<id>/revision/` | Build or apply a revision plan |
| `POST /api/argument-gym/challenges/<id>/` | Set a disposition |
| `POST /api/argument-gym/challenges/<id>/research/` | Research one challenge further |
| `POST /api/drafts/<id>/stress-test/` | Run the gym on a native draft |

Every lookup resolves the workspace's linked matter through
`apps.matters.services.user_can_access_matter`, so a case a viewer cannot reach
is a gym run they cannot reach either.

# Argument Gym reasoning benchmark

`minimal-pairs.yaml` is a maintained, synthetic, closed-world evaluation corpus.
It is not legal authority, a retrieval library, or a source of production legal
rules. The district hierarchy, city coverage, transition dates, and holdings are
stipulated assumptions. No real case names, client records, or current-law claims
are used. Before using realistic variants in a paper, have attorneys verify and
version their assumptions and gold proposition/span bindings.

The first 12 scenarios form six pairs. Each pair expands a shared input and
changes exactly one named field; its expected decisions must change. Both halves
must match their gold answers for the pair to pass.

| Pair | Variable | Required change |
| --- | --- | --- |
| Appellate district (P0) | Forum district | Binding case A vs. B, with the other characterized as persuasive |
| Municipality (P0) | Property municipality | Pay-to-stay available vs. unavailable despite a shared court |
| Amendment (P1) | Filing date | Old vs. amended notice rule |
| Entailment (P0) | Bound source ID | Supporting holding vs. keyword-related discussion |
| Goal alignment (P0) | Advocate goal | Payment proposal with no suitable template vs. litigation filing |
| Factual certainty (P1) | Record passage | Qualified, not established vs. definite, supported |

Six additional poisoned/control scenarios test contradictory authority, wrong
precedential level, overruled authority, exact quotation, paraphrase inside a
quotation, and attribution to the wrong case. `SUPPORTED`, `CONTRADICTED`, and
`NOT_ESTABLISHED` are distinct outcomes. Complete coverage is stipulated for the
municipality pair: an absent real-world ordinance is **not** proof of no right.
The amendment pair explicitly stipulates that filing date controls; real rules
may instead turn on accrual, notice, conduct, or a savings clause.

## Run and review

From the repository root:

```sh
# Offline harness and workflow regressions; makes no model calls.
.venv/bin/python backend/manage.py test apps.argument_gym.test_benchmarks apps.drafting.test_audit apps.drafting.test_operations

# Export only the model inputs, for an external runner.
.venv/bin/python backend/manage.py run_gym_benchmark --export-inputs --output /tmp/gym-inputs.json

# Grade recorded structured responses, keyed by scenario ID.
.venv/bin/python backend/manage.py run_gym_benchmark --responses /tmp/gym-responses.json --output /tmp/gym-report.json

# Optional live benchmark, using the configured OpenAI-compatible provider.
# Makes 18 model calls and incurs API costs. No deterministic fallback.
.venv/bin/python backend/manage.py run_gym_benchmark --live --output /tmp/gym-live-report.json
```

`--suite` selects another reviewed suite; `--model` and `--reasoning-level`
override live prompt defaults. Live prompts are loaded exclusively from
`prompts/argument_gym.benchmark.yaml`; admin overrides are disabled. The model
receives the scenario and decision field names, never gold values, pair IDs, or
sibling responses. Each call is independent. External runners should likewise
send only each exported value, not its scenario ID, to the model.

A response has exactly `decisions` (the requested fields) and `evidence` (objects
with `source_id` and `span`). JSON types matter. Evidence must match the curated
source/span bindings, allowing only whitespace normalization and reordered
bindings. Different quotes, extra bindings, duplicates, wrong sources, omitted
spans, and keyword overlap cannot pass. The live prompt specifies empty evidence
for action selection, exclusion without a governing case, and quote-checking
probes, whose decisions are graded directly.

Reports contain per-scenario findings, pair outcomes, input/suite/prompt hashes,
timestamp, responses, and live model/reasoning settings and raw outputs. Missing
responses and provider errors are `not_run`; malformed answers are `fail`.
Failures or unrun scenarios produce a nonzero command exit after writing the
report. Replay model identity is explicitly unverified. The prompt hash in a
replay identifies the current benchmark prompt, not proof that the external
runner used it. Keep provider identity, repository revision, external execution
settings, and repeated-run artifacts alongside any paper results. Avoid storing
private endpoint credentials in artifacts.

## What passing establishes

Offline regression tests prove the grader rejects targeted bad answers, checks
pair isolation, and cannot turn a missing run into success. They **do not prove
that an AI answered correctly**. Gold-response replay is a harness check only.
No live model measurements are included with this corpus.

Live results measure constrained reasoning with supplied sources and metadata.
They do not measure retrieval, the production opponent/judge/coach pipeline,
free-form drafting, or complete legal reasoning. Exact curated spans make grading
deterministic but may reject another substantively adequate passage; attorneys
should adjudicate and version alternative gold spans before comparing runs.
This is proposition-level evaluation against human labels, not a new general
purpose semantic entailment validator. No LLM judge assigns quality scores.

The companion drafting regressions verify that model-supplied review flags do
not remove the exported attorney-review notice, a model proposal cannot apply
itself through its payload, and plain-text auto-repair preserves the notice and
AI provenance. Draft components currently have no dedicated attorney-review
state; these tests do not establish a full actor-authorized review transition
or cover every regeneration/repair path.

## Exploratory runs on private revision snapshots

`scripts/benchmark_real_briefs.py` exercises the production Gym stages on local
DOCX revisions. This is separate from the synthetic fixed-answer benchmark.
For a small paired live run:

```sh
.venv/bin/python scripts/benchmark_real_briefs.py --live --endpoints --family L_Moore --output-dir cle_real_briefs/benchmark_outputs/my-live-run
```

Omit `--live` for offline checks with empty retrieval. `--endpoints` selects the
first/last filenames only in families containing at least three snapshots;
different appellate filings are not treated as revisions. `--model` defaults to
`gpt-5.5`, and `--reasoning` to `medium`. Live use is explicit and incurs API
costs. The source corpus must already be available locally.

The runner requires a new directory under ignored `cle_real_briefs/`, creates a
private SQLite copy of the app database, and refreshes file-backed court/legal
seeds there while preserving locally edited profiles. It saves code snapshots,
Git state, dependency versions, effective profiles, input hashes, actual model
requests/responses, source query results, complete findings and challenges, and
stage traces. Results are checkpointed per document; call logs are written before
and after each request. Credentials are used from local configuration and are
not written to manifests or call logs. The private DB copy itself must remain
private because it includes the local database's configuration and case data.

Live runs use the real Ohio case/statute/ordinance/treatise connectors, file-backed
prompts, a fixed requested model/reasoning setting, and a 180-second per-call
timeout. The SDK may omit unsupported temperature settings. Model aliases and
changing retrieval corpora mean reruns need not be bit-for-bit identical; saved
requests, responses, and returned source spans are the execution record.

No case-record attachments are supplied by this runner. Do not treat missing
record coverage as a defect in a filing. Compare findings and dispositions
within each revision family, distinguish `unmeasured` from `pass`, and report
fallbacks explicitly. There is no aggregate brief-quality score.

## Materiality and actionable findings

The judge now supplies `finding` (a short statement naming the actual problem)
and `reviewAction` (`revise`, `verify`, or `strategy`). The API exposes those
fields on each challenge. The UI groups demonstrated concerns by consequence,
then verification tasks, strategy choices, and optional improvements. An
instruction to the author is separate from proposed replacement prose. Old runs
retain their original text; rerun them to obtain the new headings and assessment.
Apply the new `argument_gym` migration before running the application.

There is no minimum finding count. A valid empty opponent response and a judge
that rejects every attack are successful reviews, not reasons to manufacture
findings. Weak/misplaced attacks are excluded; the judge's decisions, including
rejections, remain in the stage trace. Rank precedes the maximum-count cap.
Duplicates should identify their retained representative. A missing exhibit is
not missing evidence in the litigation. Fallback reviews are labeled as
verification tasks rather than asserting that a source proves the concern.

All extracted brief units and complete paragraphs now reach the argument map,
opponent, judge, and coach. The former 80-unit/900-character cutoff could hide
an entire argument section while reporting its analysis as absent. The upload
size limit and its truncation notice still apply. Larger inputs can increase
model latency and cost; processing remains asynchronous.

`actionability.yaml` supplies a supported-brief negative control and a decisive
notice-timing positive control, with duplicate and style distractors. Its rule
is fictional. Run the actual judge prompt against these controls:

```sh
.venv/bin/python scripts/benchmark_gym_actionability.py --output-dir /tmp/gym-actionability-new-run
```

This makes live calls to the configured provider (`--model` defaults to
`gpt-5.5`, medium reasoning). Requests, responses, and per-case expected/actual
retained attack ids are saved. Provider failures/fallbacks are `not_run`, never
passes. A lower finding count alone is not success: the consequential positive
control must survive. These are small screening controls, not an attorney-labeled
measure of legal accuracy.

To isolate filtering changes on an existing private real-brief capture:

```sh
.venv/bin/python scripts/replay_gym_findings.py \
  --capture-dir cle_real_briefs/benchmark_outputs/<run>/document-02 \
  --output-dir cle_real_briefs/benchmark_outputs/<new-replay> --coach
```

Repeat `--capture-dir` for multiple documents. The capture must have the
`result.json` and `call-*.json` layout from `benchmark_real_briefs.py`. The replay
uses the original candidates, argument map, and retrieved sources, rereads the
complete local DOCX, and runs the current judge and optional separate coach.
It does **not** rerun retrieval or measure recall of new attacks. The supplied
DOCX may contain exhibits; this diagnostic replay preserves the original
runner's `split_exhibits=False` behavior rather than benchmarking the upload
boundary detector. Outputs must remain under ignored `cle_real_briefs/`.

For substantive review, label each distinct issue for materiality, support,
already-answered context, duplication, and whether the proposed action is safe.
Assess false negatives as well as noise. Do not assume an accepted or filed
brief is flawless, and do not reward finding more issues. Keep brief text,
reviewer packets, and model outputs private.

This evaluation design draws on Harvey's [Legal Agent Benchmark](https://www.harvey.ai/blog/introducing-harveys-legal-agent-benchmark)
use of reviewable work product and explicit task criteria, and
[BigLaw Bench: Arena](https://www.harvey.ai/blog/introducing-biglaw-bench-arena)
use of reasoned comparisons. These are methodological references, not evidence
that CLARA meets those benchmarks. Independent model reviews are diagnostic
opinions, not attorney ground truth.

### Exploratory results, October 29, 2026

Independent Claude and Argon CLI reviews of three saved real-brief outputs
identified duplicate concerns, missing-exhibit assumptions, and argument sections
omitted by the 80-unit cap. These reviews are model opinions, not legal labels.
The reviewed sample consists of three summary-judgment briefs; it does not
establish performance on appellate briefs or other filing types.

With `gpt-5.5` at medium reasoning, replaying their saved candidates against
complete extracted text and the revised judge/coach produced **3, 6, and 4**
retained findings, compared with **7 each** in the saved outputs. All six model
calls completed without fallback. The supported synthetic control retained zero
findings; the timing-defect control retained the material issue and rejected
duplicate/style distractors. Both control calls completed without fallback.

These counts are diagnostic, not a precision estimate or proof of improvement:
the replay holds earlier retrieval and candidate attacks fixed, and expert labels
are still needed for the remaining findings and missed issues. Inspection also
found overly technical headings and strategy recommendations phrased as edits;
the subsequent language instructions and verification/strategy prose guard were
added after this replay, with focused regression tests. Exact private requests,
responses, reviewer outputs, and reports are in
`cle_real_briefs/actionability-experiment/`; none are versioned here.

# Repository maintenance pattern

Reusable legal-content defaults belong in the top-level [`content/`](content/README.md)
library, not inside a Django app or embedded in Python constants.

- Put shared DOCX snippets in `content/docx-snippets/_shared/blocks/` and
  pathway overrides in `private-content/docx-snippets/<template-slug>/blocks/`.
- Put organization letterheads in `<provider>/letterheads/<slug>/` with a
  `manifest.yaml` and `letterhead.docx`. Real stationery is private; keep a
  neutral placeholder in `content/letterheads/` so a fresh checkout can draft.
- Put authoritative treatise PDFs under `content/treatises/source/`; keep
  generated Markdown under `content/treatises/markdown/` and do not hand-edit
  it.
- Put one court's filing-format rules in `content/court-rules/<slug>.yaml` and
  seed them into `CourtProfile`. Every profile states its own `verification`, and
  only a verified one -- whose `source` cites the court's published local rule --
  may produce error-level findings; a starter profile warns. A wrong filing rule
  is worse than a missing one: it reports a filing as clean when a clerk would
  reject it. Never overwrite a profile marked `is_locally_edited`.
- Report a format property that could not be measured as unmeasured, never as a
  pass. A DOCX has no page count until something renders it and a scanned PDF has
  no type size to read; silently skipping either tells an advocate their
  fifty-page brief fits in fifteen.
- Put a legal rule's elements in `content/legal-rules/<slug>.yaml` and seed them
  into `LegalRuleProfile`. Where a published decision table already encodes the
  rule, name its row rather than restating it. The same verification rule applies
  as for court rules: only a verified element list may report an error, because a
  wrong element list tells an advocate their pleading is complete when it is not.
- Never add a dictionary spell check to a filing. A general dictionary flags
  "replevin", "estoppel", and half of every case name, and an advocate who
  dismisses forty false positives stops reading the check. Check the words legal
  writing actually gets wrong, in
  `content/drafting-rules/checks/legal-language.yaml`. Passive voice is likewise
  never an error in a filing: "service was perfected" is the register a court
  expects, so accepted phrases are file-backed and extendable per session.
- Put local law under `content/ordinances/`, one generated document per
  municipality, in the same manifest-and-chunks shape as the statute corpus.
  Maintain `scope.yaml` and `datasets/*.yaml` by hand; regenerate the rest with
  `scripts/ingest_local_ordinances.py`.
- Do not commit the generated ordinance corpus or the documents it was read
  from. Like case-law bundles, it is public but unversioned data: megabytes of
  machine-written manifests and chunks that the ingest script rewrites wholesale.
  It is uploaded to `raw/content/ordinances/` and published by the bootstrap job
  to `published/content/ordinances/`, which `PUBLISHED_CONTENT_LIBRARY_DIR`
  points at. What belongs in git is what a person maintains and reviews: the
  coverage scope, the datasets, and the supplied transcriptions.
- Never scrape a commercial codifier. American Legal Publishing answers a
  script with a Cloudflare bot challenge and Municode's content API requires a
  token; both are citation targets, not fetch targets. Ordinance text comes from
  the city's own Legistar legislative record or an official published document,
  and a record built from an act is stamped `text_basis: enacted_act` because
  the act a council passed is not the chapter as it stands today.
- Record a declared authority that cannot be retrieved as `pending` with the
  reason. It produces no chunk, so retrieval can never return it as law, and it
  still appears in coverage. An unreachable ordinance that is simply absent
  reads to an advocate as "no local law here", which is the wrong answer.
- Put a controlled vocabulary in `content/jurisdictions/` and read it only
  through `apps.core.jurisdictions`. Counties arrive from scanned documents, so
  the same one reaches the corpus as `Cuyahoga` and as `Cuyahoga County`; left
  alone that splits a shelf and shows a reader half the cases without saying
  so. Canonicalize on ingestion, because the database is the only place the fix
  is durable, *and* on read, because material imported earlier is still there.
  Match exactly after mechanical tidying and never to a nearest neighbour: the
  corpus holds `Miami-Dade County`, and an out-of-state county rewritten to the
  nearest Ohio one sits on an Ohio shelf with nothing looking wrong. Leave what
  does not match alone and report it.
- Keep research search deterministic and keep the boundary structural.
  `backend/apps/sources/research/` answers a search without importing `apps.ai`,
  and a test enforces that. Reranking and synthesis are opt-in per search,
  applied on top of a finished result set, and every response reports in words
  what a model did -- including that it did nothing. A reader who has to infer
  "no AI" from a missing badge is guessing, and a search that dies because a
  provider is down is not a library.
- Expand a query from files, not from a model. Reviewed terms of art live in
  `content/research-index/thesaurus.yaml` with their own `verification`; the
  long tail is learned offline by `manage.py build_research_index` from corpus
  co-occurrence and is reported as learned rather than reviewed. Never expand an
  exact phrase or a citation: those are requirements, and a near neighbour
  answers a question nobody asked. Where a phrase or citation matches nothing,
  say which one missed -- ranking something else into an empty result reads as
  an answer.
- Add a known-answer query to `content/research-index/regression-queries.yaml`
  rather than to the test. Retrieval gets worse the way nothing else does:
  silently, with no error and no failing test, until an advocate cannot find
  what they know is there. An entry names the corpora it `requires`, and a
  checkout without them reports those entries as skipped, never as passing.
- Put default triage rubrics in `content/triage-rubrics/*.yaml`. Seed new files
  into the database; do not silently overwrite existing admin-managed records.
- Put the rules mapping a triage outcome to LegalServer case properties in
  `content/legalserver-field-maps/*.yaml`. Which fields an outcome should change
  is office policy about a site's own schema, not application logic; ship new
  maps disabled and in dry run so the hook exists before the fields do.
- Read generated manifests through `apps.sources.library.load_manifest()`, never
  with a bare `yaml.safe_load`. It caches each parse against the file's mtime and
  size and uses libyaml when available; a code manifest is megabytes of YAML that
  the pure-Python parser needs seconds to read, and browsing, retrieval, and every
  citation preview all parse the same files.
- Treat `CONTENT_LIBRARY_DIR` as a content-provider boundary. Future SharePoint
  support must preserve the same logical paths and record remote provenance
  before writing derived data.
- Side-loaded documents go through `apps.core.storage`, never through direct
  filesystem or SDK calls. Every store has a `raw/` area an operator uploads into
  and a `published/` area the application reads; nothing serves out of `raw/`,
  and only an ingest or publish step writes to `published/`. Keep provider APIs
  behind `DocumentStorage` so moving between a mounted share and S3 stays a
  configuration change.
- Keep the published layout re-ingestable. Ingestion once recognized only the
  download naming (`*.pdf.txt`) while storage wrote the published naming
  (`<sha>.txt`), so a complete corpus reported `missing_text` on every group and
  imported nothing, silently. A round trip through storage must be a no-op, not a
  loss — and a corpus that reports itself incomplete deserves suspicion of the
  reader before the data.

See [`content/README.md`](content/README.md) for the complete maintenance,
precedence, and future SharePoint-provider guidance.

## Development workflow

This is a Django + React prototype. Work from the repository root unless a
command says otherwise.

- Backend setup and checks use the local virtualenv:
  - `.venv/bin/pip install -r requirements.txt`
  - `.venv/bin/python backend/manage.py migrate`
  - `.venv/bin/python backend/manage.py check`
  - `(cd backend && ../.venv/bin/python manage.py test)` -- every app; from the
    repository root, label-less discovery finds no tests and still exits 0
- Frontend work happens under `frontend/`:
  - `npm install`
  - `npm run test`
  - `npm run build`
- When changing both backend and frontend behavior, verify the API contract and
  the UI path that consumes it.

## Application boundaries

Keep changes aligned with the existing workflow boundaries:

- Retrieval/source integrations belong under `backend/apps/sources/`; new
  connectors should implement `SourceConnector.search()` and be registered in
  `backend/apps/sources/registry.py`.
- Drafting orchestration belongs under `backend/apps/drafting/`. Write draft
  section JSON only through `apps.drafting.components.record_sections()`, and
  express changes to an existing document as `apps.drafting.operations`
  proposals, so component history and provenance stay complete.
- AI prompt execution and model-facing logic belong under `backend/apps/ai/`.
- Template and reusable block behavior belongs under
  `backend/apps/templates_app/`.
- Say what a maintained original's editorial passages mean in its private
  `template-refinements/<template-slug>.yaml`, not in the converter: an
  "[OR]", a "[written/oral]", a "Case Caption" marker, or a sample "$X" is a
  decision about one document. Each edit names the exact wording it changes and
  fails ingestion when it no longer matches, so a revised original cannot drop
  a reviewer's decision silently. Check a refined template by rendering it with
  every field answered; `apps.templates_app.test_refinements` does that for
  every refined package in the checkout.
- A prepared template keeps the maintained original's wording. Convert only
  language the author marked as variable (`[...]`, `____`, highlighting) through
  `apps.templates_app.placeholders`; never rebind ordinary prose to a model-written
  slot. Rewrite paragraphs run by run so inline formatting survives.
- Express either/or clauses as a named choice with Docassemble/AssemblyLine
  conventions: a snake_case variable, snake_case option values, and
  paragraph-level `{%p if %}`/`{%p elif %}` tags. Make the first alternative the
  default so an unanswered choice never deletes the passage.
- Express how much the model may write as a block's `ai_latitude`
  (`locked`/`guided`/`generate`). Latitude constrains the model only: a human
  edit must always reach the export through `blocks[<key>]["revision"]`.
- Client advice letters are catalogued as `AdviceLetterSection`, not as
  `DocumentTemplate`. The unit that gets picked and reviewed is the section. Keep
  assembly deterministic: the wording is what the working group revised for
  readability, and regenerating it discards that work.
- Offer every advice-letter section, and say which ones need checking. Text that
  carries accepted tracked changes, a merge-boundary passage, or AI drafting is
  flagged with `needs_attorney_review` and a one-line reason rather than hidden;
  withholding it once hid the best match for a case behind a stale status.
- Do not let `sync_advice_letters` overwrite a section marked `is_locally_edited`.
  An attorney's read and correction must survive the next ingest.
- Score client-facing text with `apps.validation.readability`, whose rules are
  file-backed in `content/drafting-rules/checks/`. Report several formulas rather
  than treating one as authoritative, and never rewrite text purely to move a
  score.
- Letterhead behavior belongs in `apps.templates_app.letterheads`. A letterhead
  prepared for sharing must carry no trace of the advocate whose file seeded it,
  including document properties and `mailto:`/`attachedTemplate` relationships.
- Export formats belong behind `backend/apps/exporting/`.
- Write work product back to LegalServer through `apps.matters.legalserver_delivery`,
  never by calling the client from a view. Every attempt is recorded as a
  `LegalServerDelivery`, and a delivery must never raise into its caller: an
  advocate who exported a document still gets the document when the upload
  fails, and hears that it failed. Documents save by default; research answers
  and triage assessments do not.
- Frontend API calls should go through `frontend/src/api/client.js`; avoid
  scattering fetch logic through components.
- Frontend derivation and state logic belongs in plain `.js` modules with
  `node --test` coverage under `frontend/test/`; keep `.jsx` components
  presentational so the rules stay testable without a browser runtime.
- Never use placeholder text in a form control -- no HTML `placeholder`
  attribute and no editor `placeholder` prop carrying guidance or an example
  value. Grey text in an empty box reads as a filled-in answer, which is exactly
  the confusion a review screen exists to prevent: an advocate skims past a
  blank that looks answered, or mistakes "Enter hearing date" for data. It also
  vanishes the moment someone types, fails contrast, and is not a reliable
  accessible name. Put the instruction in the label or in help text beneath the
  control, linked with `aria-describedby`, and let an empty field look empty.
  Older screens still carry some placeholders; remove them when you touch those
  components rather than adding more.

Do not replace reviewable workflow steps with a single free-form agent flow.
- A filed brief arrives with its exhibits attached and is mostly not a brief.
  Detect the boundary (`apps.argument_gym.ingestion.split_brief_and_exhibits`) and
  ingest the attachments as case-record material; never send a three-hundred-page
  filing to a model to find the argument in it. Where nothing marks the boundary,
  the page cap applies and the reason is recorded.
- Let the author choose which checks run, and keep "turned off", "could not run",
  and "ran and found nothing" three distinct states. A check that never ran must
  never read as a clean result, and an empty selection must not silently mean
  "run everything" -- `apps.argument_gym.checks` stores an explicit sentinel for
  a session with every check off.
- Never hold a request open for work that runs longer than the worker timeout.
  A killed gunicorn worker returns no headers at all, so the browser reports a
  CORS failure and the real cause -- a timeout -- is invisible. Long work starts
  on a background thread, returns 202 with a row to poll, and saves its progress
  as it goes.
- Adversarial review of a brief belongs under `backend/apps/argument_gym/`. Keep
  the opponent, the judge, and the coach as separate model calls: one call asked
  to attack, weigh, and answer produces attacks it has already decided are
  answerable, which is the failure the feature exists to prevent. A gym run
  references case documents and never copies them, and it never edits a
  document -- a challenge reaches the draft only through an
  `apps.validation.revision` plan a human applied. Report challenges, coverage,
  and dispositions; never a numeric score for a brief.
Preserve human review points for facts, template choices, source support,
validation, and export.

## Prompt catalog rules

Runtime prompts belong in file-backed YAML under `prompts/`, not hardcoded in
Python or React.

- Use one `*.yaml` or `*.yml` file per prompt key.
- Keep prompt placeholders explicit and named with Python `{name}` syntax.
- Do not rely on Django admin prompt overrides for benchmark variants or
  source-controlled behavior.
- If a prompt's required variables, model default, or reasoning default changes,
  update the YAML and any tests or call sites together.

## Legal content and generated assets

Never commit client documents, credentials, private organization templates, or
other confidential material.

- Public reusable defaults belong under `content/`.
- Private organization material belongs under `ORGANIZATION_CONTENT_LIBRARY_DIR`,
  normally `private-content/`, which should remain git-ignored.
- Do not hand-edit generated Markdown, generated statute chunks, generated
  manifests, or ingested template package outputs. Fix the source document,
  converter, or ingestion script and regenerate.
- Record where an extracted date came from, not just its value. Dates on case-law
  decisions are model-read out of scanned paper, so `apps.caselaw.dates` writes a
  `CaseLawDateProvenance` row per field carrying the source sidecar, its checksum,
  and the passage in the document's own OCR text that shows the date. Corroborated
  means the text contains it, never that it has been confirmed to mean what the
  field says. An uncorroborated date is unverifiable, not wrong: the scans carry
  complete OCR, so the date is simply not printed on the page in readable form.
- Preserve provenance for legal authorities and remote/private content,
  including source path, checksum, modified time, fetch/import time, and remote
  IDs where available.
- Treat generated legal-source records as research aids only; do not imply they
  replace checking current law before filing.

## Environment and secrets

Runtime configuration belongs in `.env`; `.env` must stay untracked.

- Keep `.env.example` current when adding or renaming settings.
- Do not add API keys, access tokens, tenant secrets, LegalServer credentials,
  SharePoint tokens, or client-identifying data to tests, fixtures, screenshots,
  docs, or sample content.
- Prefer deterministic fallbacks for local development unless a change
  explicitly needs live AI, LegalServer, SharePoint, or Office 365 access.

## Change style

Prefer small, reviewable changes that keep legal content, prompts, provider
integrations, and UI behavior auditable.

- Document new management commands, environment variables, and content-provider
  paths in the relevant README.
- Add or update tests for behavior changes in backend services, prompt loading,
  content ingestion, retrieval connectors, or frontend data handling.
- Keep naming lowercase kebab-case for content-library files and directories.

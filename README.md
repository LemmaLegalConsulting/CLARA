# CLARA — Cleveland Legal Aid Research Assistant

CLARA uses the provisional lighthouse logo supplied in `clara-logos.zip`. The
light, dark, and icon SVG assets live in `frontend/public/`.

This repository contains a working Django + React prototype for a housing court document drafting workspace. The app supports three modes:

- Research across configured source connectors.
- Draft from a structured template with human review points.
- Draft from scratch using a pleading shell and constrained section generation.

The implementation is intentionally modular so real integrations can replace the current stubs without rewriting the workflow.

## Requirements

- Python 3.12+
- Node.js 24+ (the active LTS line; the image builds the frontend on `node:24-slim`)
- npm 11+

## First-Time Setup

From the repository root:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env

cd frontend
npm install
cd ..

.venv/bin/python backend/manage.py migrate
.venv/bin/python backend/manage.py createsuperuser
```

The app seeds templates through the API bootstrap path. Sample matters are opt-in with `ENABLE_DEMO_MATTERS=true`; by default, missing LegalServer access shows an unconnected/empty state instead of fake case data. Seeded sample matters are stored with `source_system="Demo"`, and the flag grants access only to those rows: it never widens who can read a real LegalServer case.

## Integration Configuration

Runtime settings are loaded from `.env` in the repository root. `.env` is intentionally ignored by git; `.env.example` documents the required keys.

- OpenAI-compatible drafting calls use `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`. Set `AI_DRAFTING_ENABLED=false` to keep deterministic drafting fallbacks during local development.
- Case action recommendations default to `CASE_ACTION_MODEL` or `OPENAI_MODEL` when unset, so that workflow suggestions can use a different model from document drafting.
- AI prompts are file-backed YAML entries in [`prompts/`](prompts/README.md). Set `PROMPT_CATALOG_DIR` to a directory containing a benchmark variant catalog; enabled **Prompt overrides** in Django admin take precedence for operational edits.
- Research is constrained to a jurisdiction. Set the organization fallback with `DEFAULT_JURISDICTION`; Django admin's **Organization settings** can override it, and each user can set a **Default research jurisdiction** in Profile. A selected matter's jurisdiction takes precedence over both.
- Local ordinances and municipal court rules are a first-class source alongside statutes and case law. Coverage and acquisition routes are configured in [`content/ordinances/scope.yaml`](content/ordinances/scope.yaml) and refreshed with `scripts/ingest_local_ordinances.py`; see [`docs/LOCAL_ORDINANCES.md`](docs/LOCAL_ORDINANCES.md).
- Public reusable legal-content defaults are maintained in [`content/`](content/README.md). Private organization templates live outside source control under `ORGANIZATION_CONTENT_LIBRARY_DIR` (default `private-content/`) and override public defaults; `CONTENT_LIBRARY_DIR` configures only the public provider. Triage YAML seeds new database records without replacing admin edits.
- Case-law PDFs, sidecar artifacts, and private organization content are side-loaded outside git through the document storage layer in [`apps.core.storage`](backend/apps/core/storage.py). Every store is split into `raw/`, `validated/`, and `published/` areas, so a partial upload is never visible to retrieval. Local development and the mounted Azure Files deployment use `filesystem`; native Azure Blob and S3-compatible object stores are configuration changes. Authorized staff can version, review, publish, retire, and roll back treatises, local cases, and ordinances in Django admin; see [`docs/CONTENT_MAINTENANCE.md`](docs/CONTENT_MAINTENANCE.md) and [`docs/CASELAW_INGESTION.md`](docs/CASELAW_INGESTION.md).
- Case chat document text extraction uses `DOCUMENT_TEXT_EXTRACTOR=stdlib` by default. Optional values are `markitdown` or `docling` when those packages are installed; the extractor interface is intentionally pluggable for custom backends.
- LegalServer uses `LEGALSERVER_BASE_URL`, `LEGALSERVER_API_TOKEN`, `LEGALSERVER_MATTERS_PATH`, `LEGALSERVER_MATTERS_RESULTS`, `LEGALSERVER_MATTER_DOCUMENTS_PATH`, and `LEGALSERVER_MATTER_PROFILE_PATH`. Matter search uses the v2 `/api/v2/matters` endpoint with `results=full`, `page_size`, and the documented text search keys. `LEGALSERVER_MATTER_PROFILE_PATH` controls the in-app deep link to the official case profile and receives the LegalServer database ID as `{matter_id}`. User access filtering is applied inside the app after LegalServer returns authorized records.
- Writing back to LegalServer uses `LEGALSERVER_NOTES_PATH`, `LEGALSERVER_DOCUMENTS_PATH`, and `LEGALSERVER_MATTER_UPDATE_PATH` (with `LEGALSERVER_MATTER_UPDATE_METHOD` and the optional `LEGALSERVER_DOCUMENT_TYPE`). All three address a matter by its UUID, which is how the v2 API identifies one. See [Saving work back to LegalServer](#saving-work-back-to-legalserver).
- SharePoint Online uses Microsoft Graph with `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`, and either a delegated `ms_graph_access_token` in the Django session or a service token in `SHAREPOINT_ACCESS_TOKEN`. Case document lookup uses `SHAREPOINT_CASE_FOLDER_TEMPLATE`.

Authentication uses Django's standard auth framework. Manual accounts authenticate through `/api/auth/login/`; Office 365 SSO can be fronted by an upstream OIDC/proxy layer and passed into Django with `ENABLE_REMOTE_USER_AUTH=true`.

Configure live connections in Django admin at `/admin/` under **Source configurations**. Admin rows use typed fields for each connection type and override `.env`; if a field is missing or no enabled row exists for a connection, the app falls back to `.env`.

Office 365 / SharePoint supports two access paths:

- **User delegated access:** save an enabled Office 365 entry under **User OAuth connections** for a Django user. SharePoint searches use that user's Graph token first, so the user sees documents their account can access.
- **Server fallback access:** configure SharePoint server credentials under **Source configurations**. The connector uses those only when there is no delegated user token, which supports volunteers or external users who should only see documents exposed through the server/legal-system integration.

Office 365 sign-in requires a concrete tenant UUID in `OFFICE365_TENANT_ID`. Before rollout, run migrations and create an enabled **Office365 principal** in Django admin for each approved user, using the tenant and object IDs verified in Entra. Existing email-based accounts are never automatically linked. Keep a local administrator login available during this explicit migration. MSAL manages the authorization-code flow, PKCE and nonce validation; PyJWT separately verifies signature, issuer, audience and timestamps; database-backed transactions expire after ten minutes and are single-use. Entra assignment, conditional access and MFA policies must also be checked by the operator.

## Start The Application

Run the backend and frontend in separate terminals.

Terminal 1, from the repository root:

```bash
.venv/bin/python backend/manage.py runserver 0.0.0.0:8000
```

Terminal 2, from the repository root:

```bash
cd frontend
npm run dev -- --port 5173
```

Then open:

- Frontend: http://localhost:5173/
- Django admin, proxied through Vite: http://localhost:5173/admin/
- Backend direct URL, when needed: http://localhost:8000/

The dev frontend proxies `/api`, `/admin`, `/static`, and `/favicon.ico` to Django, so normal local use happens through `http://localhost:5173/`. Django is still available directly at `http://localhost:8000/` when needed.

Django admin's **View site** link uses `FRONTEND_SITE_URL`, which defaults to `http://localhost:5173`.

## Useful Verification Commands

From the repository root:

```bash
.venv/bin/python backend/manage.py check
```

Dry-run and import a local case-law corpus:

```bash
.venv/bin/python backend/manage.py ingest_caselaw ~/cases --dry-run
.venv/bin/python backend/manage.py ingest_caselaw ~/cases
```

Run backend tests:

```bash
(cd backend && ../.venv/bin/python manage.py test)   # every app

# Build the term-neighbour table research-mode query expansion reads. Counting
# only -- no model is called, during the build or at query time. Not committed:
# it is derived from the imported case law as well as from the content library.
.venv/bin/python backend/manage.py build_research_index
.venv/bin/python backend/manage.py build_research_index --status

# Bring already-imported decisions onto the shared county vocabulary, so
# "Cuyahoga" and "Cuyahoga County" stop being two shelves. --report lists the
# spellings the vocabulary does not hold instead of guessing at them.
.venv/bin/python backend/manage.py normalize_case_counties --report
.venv/bin/python backend/manage.py normalize_case_counties
```

Run a quick backend workflow smoke test:

```bash
ENABLE_DEMO_MATTERS=true .venv/bin/python backend/manage.py shell -c "import json; from django.contrib.auth import get_user_model; from django.test import Client; User=get_user_model(); User.objects.update_or_create(username='smoke', defaults={'is_staff': True, 'is_superuser': True}); u=User.objects.get(username='smoke'); u.set_password('smoke-pass'); u.save(); c=Client(); assert c.login(username='smoke', password='smoke-pass'); assert c.get('/api/bootstrap/').status_code == 200; s=c.post('/api/drafting-sessions/', data=json.dumps({'mode':'draft_from_template','matterId':'LS-24018','templateId':1}), content_type='application/json').json()['session']; d=c.post(f\"/api/drafting-sessions/{s['id']}/draft/\", content_type='application/json').json()['draft']; print(d['title'], len(d['plainText']))"
```

Build the frontend:

```bash
cd frontend
npm run build
```

## Deployment

Production runs on Azure Container Apps with a managed PostgreSQL Flexible
Server, at https://cle-draft.lemmalegal.com.

**Code changes deploy themselves.** Merging to `main` runs
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), which tests,
builds, migrates, rolls the app, and verifies the live site. It updates only the
container image, so no production configuration lives in GitHub.

**Configuration changes need the script.** New environment variables or scale
changes come from `.env.containerapps` and only apply when you run:

```bash
./scripts/deploy_azure_containerapps.sh
```

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the topology, the `raw/` vs
`published/` document storage boundary, and DNS.

### Sleeping and waking

The app scales to zero when it is idle for five minutes, so the next request
after that waits for a replica to start. The application itself is serving under
a second after its container starts; the rest is Azure scheduling a node,
pulling the image, and deciding the replica may take traffic. That last part
turns out to be set largely by the health probes, which is where most of the
tuning went — see [`docs/WARM_START.md`](docs/WARM_START.md) for the measured
numbers. It cannot be made short, only shorter.

To remove it entirely, keep one replica running:

```bash
MIN_REPLICAS=1 ./scripts/deploy_azure_containerapps.sh
```

That costs roughly $22/month always-on, or about $8 warm on weekday hours only.
[`docs/WARM_START.md`](docs/WARM_START.md) has the rates, the schedule options,
and the one way a later deploy can quietly undo it.

While the app is asleep it cannot serve the notice saying so — the page itself
comes out of the same container. What the frontend does cover is the case that
actually bites: a tab left open past the idle timeout, where the next click
would otherwise hang with no explanation. See
[`frontend/src/api/wakeNotice.js`](frontend/src/api/wakeNotice.js).

## Repository Layout

```text
.
├── AGENTS.md                 Repository maintenance patterns
├── backend/                  Django project and backend apps
├── brainstorming/            Original planning documents
├── clickable_prototype.js    Original clickable React prototype
├── content/                  Maintained DOCX snippets, treatise source/Markdown, and triage rubrics
├── docker/                   Container entrypoints: bootstrap (migrate/ingest) and web (nginx + gunicorn)
├── docs/                     Architecture and deployment notes
├── frontend/                 Vite + React + Lexical frontend
├── private-content/          Private organization templates
├── prompts/                  LLM system/user messages
├── scripts/                  Helper scripts and utilities
├── requirements.txt          Python dependencies
└── Dockerfile, nginx.conf, docker/  Image and container entrypoints
```

## Backend Layout

```text
backend/
├── manage.py
├── config/
│   ├── settings.py           Django settings, dev CORS/CSRF config
│   └── urls.py               API and admin routes
└── apps/
    ├── ai/                   Constrained drafting service boundary
    ├── core/                 Shared JSON helpers, bootstrap, dev CORS middleware
    ├── argument_gym/          Adversarial stress testing of briefs
    ├── drafting/             Drafting sessions, draft documents, workflow endpoints
    ├── exporting/            Export adapters
    ├── facts/                Extracted facts and review states
    ├── issues/               Candidate issues and review states
    ├── matters/              Case/matter data and candidate facts
    ├── rules/                Rules engine and decision tables
    ├── sources/              Retrieval connector registry and source search
    ├── templates_app/        Document templates, blocks, template-from-example service
    └── validation/           Draft validation checks
```

Inside `apps/drafting/`, the document artifact layer sits under `DraftDocument`:

- `components.py`: each section is also a durable `DocumentComponent` with
  append-only `ComponentVersion` history. `record_sections()` is the one write
  path, so generation, editor saves, and auto-repair are all attributable.
- `operations.py`: typed, reviewable changes (`replace`, `insert`, `delete`,
  `move`, `revert`). Proposals are inert until applied.
- `source_bindings.py`: which component version used which source, typed by
  whether that source is record evidence, authority, procedure, or example
  language only.
- `packages.py`: the documents a plan generates, their package roles, and the
  relationships between them.

Validation rules for the last two live in `apps/validation/source_integrity.py`
and `apps/validation/packages.py`. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the model and API details.

Key extension points:

- Add a retrieval source by implementing `SourceConnector.search()` under `backend/apps/sources/connectors/` and registering it in `backend/apps/sources/registry.py`.
- Declare a document's role in a filing package with `metadata.packageRole` on its template, so cross-document validation understands the package without new Python.
- Add or change document structure through `DocumentTemplate` and `TemplateBlock` in `backend/apps/templates_app/models.py`.
- Replace the organization's letterhead in Django admin under **Letterheads**; see [Letterheads and letters](#letterheads-and-letters).
- Replace deterministic AI placeholders inside `backend/apps/ai/services.py`.
- Maintain LLM system/user messages in `prompts/*.yaml`; see [`prompts/README.md`](prompts/README.md) for the schema, benchmark workflow, and database-override behavior.
- Maintain reusable legal-content files in [`content/`](content/README.md). Run `.venv/bin/python backend/manage.py sync_content_library` to seed new triage-rubric files; use `--update-triage-rubrics` only when intentionally replacing existing database values.
- Add export formats in `backend/apps/exporting/services.py`.

- Stress-test a brief against an opponent, a judge, and a coach through `backend/apps/argument_gym/`; see [`docs/ARGUMENT_GYM.md`](docs/ARGUMENT_GYM.md).
- Add a legal rule's elements as `content/legal-rules/<slug>.yaml`, or point a profile at a published decision-table row, and maintain them in Django admin under **Legal rule profiles**.
- Add a court's filing-format rules as `content/court-rules/<slug>.yaml`, seed them with `sync_content_library`, and maintain them in Django admin under **Court profiles**. Only a profile marked `verified`, citing the court's own local rule, reports error-level findings.
## Prepared Templates

### Court filings: motions, memoranda, and replies

Motion for Summary Judgment, Motion to Dismiss, and a reply in support of each
are maintained as YAML in `content/filing-templates/<slug>.yaml` rather than as
Word originals: a motion with a memorandum, tables of contents and authorities,
an exhibit index, and a certificate of service has a structure worth stating
instead of guessing. Generate their packages after editing a spec:

```bash
.venv/bin/python backend/manage.py build_filing_templates          # write and re-index
.venv/bin/python backend/manage.py build_filing_templates --check  # fail if a package is stale
```

The output (`content/document-templates/<slug>/` and
`content/docx-snippets/<slug>/`) is generated and committed; do not edit it by
hand. The maintained legal standards are marked `verification: starter`.

**Table of authorities.** Any exported document that contains a Word `TOA`
field gets its citations marked with Word's own `TA` fields and each `TOA`
field filled, grouped by the categories in
`content/drafting-rules/table-of-authorities.yaml`. Page numbers are filled in
by Word when it updates the document's fields, which it offers to do on open;
nothing here computes them, and validation reports them as unmeasured. A
citation that could not be marked (a decision cited only by docket number, a
case whose name could not be read) is a validation warning. A document whose
author already marked citations in Word is left as the author built it.

**Responding to a filing.** A template's `responds_to` says whether it answers
the other side's filing: a reply requires the brief in opposition, a motion to
dismiss recommends the complaint. The plan screen asks for it: a case-file
document is linked by reference and read through LegalServer when drafting, or a
copy that is not in the case file is uploaded to the `raw/` storage area with
its exhibits set aside (`/api/drafting-sessions/<id>/opposing-filing/`). A
template that requires the filing will not generate without it.


`ingest_document_templates` converts maintained originals into template packages
that keep the author's wording:

```bash
.venv/bin/python backend/manage.py ingest_document_templates --force
```

DOCX and XLSX sources are both supported. Conversion edits WordprocessingML in
place, so styles, numbering, tables, headers, footers, and images survive. Only
language the author marked as variable is rebound:

| Marked as | Becomes |
| --- | --- |
| `[DATE]`, `[PHA]`, `[ADDRESS]` | `{{ fields.* }}` or a system alias such as `{{ defendant }}` |
| `________` | the field the surrounding sentence implies |
| A highlighted value | the matching field |
| A highlighted sentence | `{% if include_… %}…{% endif %}`, keeping the original wording |
| Two clauses separated by `[OR]` | `{%p if … %}` / `{%p elif … %}` / `{%p endif %}` over a named choice |

Alternative clauses follow Docassemble/AssemblyLine conventions: a snake_case
variable with snake_case option values, and paragraph-level `{%p %}` tags. A
certificate of service written both ways becomes:

```jinja
{%p if service_method == "email" or not service_method %}
Pursuant to Civ.R. 5(B)(4), I certify that on {{ fields.service_date }} … by email
to {{ fields.plaintiff_email }}, pursuant to Civ.R. 5(B)(2)(f).
{%p elif service_method == "mail" %}
Pursuant to Civ.R. 5(B)(4), I certify that on {{ fields.service_date }} … by United
States mail to {{ fields.plaintiff_address }}, pursuant to Civ.R. 5(B)(2)(c).
{%p endif %}
```

The first alternative is also the default, so an unanswered choice still renders a
complete certificate. Options are named for what distinguishes them (`email`,
`mail`, `personal`, `courier`, `fax`) and fall back to `option_1`, `option_2`.
Each choice is declared in the manifest, surfaced on the template as
`metadata.choices`, and offered as a select under **Either/or clauses**; the
advocate's answer travels in the session's template data.

Every block records an `ai_latitude` that governs how much of it the model may
write:

- **locked** - captions, certificates of service, signature blocks, and passages
  carrying quoted statutes or citations. Rendered verbatim.
- **guided** - the maintained prose is a starting draft that still needs adapting.
  It renders literally and accepts a reviewed rewrite.
- **generate** - the original said "insert case specific facts"; the instruction
  becomes the model's prompt and the model supplies the paragraphs.

Latitude constrains the model, not the advocate. An edit made in the editor always
reaches the export through `blocks[<key>]["revision"]`, whatever the latitude.
Adjust a block's latitude in Django admin under **Template blocks**.

## Letterheads and Letters

One parameterized letterhead serves every advocate. Its contact block is filled
from the author's profile at render time, so adding an advocate does not mean
adding a document.

```bash
# Turn one advocate's letterhead into the organization-wide template.
.venv/bin/python backend/manage.py prepare_letterhead path/to/letterhead.docx \
    --slug my-org --title "My Legal Aid" --organization "My Legal Aid" --default

# Regenerate the neutral placeholder a fresh checkout draws letters on.
.venv/bin/python backend/manage.py build_placeholder_letterhead
```

Preparation replaces the advocate's name, phone, fax, and email lines with
variables, parameterizes the continuation header, and strips the source
advocate's identity from document properties and from `mailto:`/`attachedTemplate`
relationships. The masthead image, margins, and page setup are untouched.

Available variables: `advocate_name`, `advocate_title`, `advocate_phone`,
`advocate_fax`, `advocate_email`, `office_name`, `office_address`,
`letter_subject`, `letter_date`. An advocate with no fax gets no fax line rather
than an empty label.

Organization stationery is private, so it belongs under
`ORGANIZATION_CONTENT_LIBRARY_DIR` (normally the `private-content/` submodule).
A neutral `example-legal-aid` placeholder ships in `content/letterheads/` so a
fresh install can draft and export a letter before anyone uploads their own.
Non-technical staff replace it in Django admin under **Letterheads**, which
explains the expected layout and reports which contact lines it found.

Letter drafting lives in `backend/apps/drafting/letters.py` with its prompt in
`prompts/drafting.letter.yaml`. The letterhead supplies the advocate's identity,
so the drafted body never restates it.

## Client Advice Letters

Advice letters are catalogued separately from litigation templates, because the
unit an advocate picks is the section rather than the document. A letter is the
Model Letter's opening, however many sections the tenant's situation calls for,
and its closing — composed onto the letterhead.

```bash
.venv/bin/python backend/manage.py ingest_advice_letters path/to/letter-folder
```

The source folder is the working group's own: `Client Letters.xlsx`,
`Model Letter.docx`, and `Letter Sub-Sections/`. Ingestion handles three things
the maintained files need before the text is usable:

- **Tracked changes are accepted, then copy-edited.** Nine sections carry
  unresolved edits. A run inside `w:ins` is not paragraph content, so reading
  them naively yields shredded prose. The accepted text is written to
  `advice-letters/accepted/` so a reviewer can open the resolved version in Word.
- **Repeated wrappers are stripped.** Five sections restate the whole Model
  Letter; assembling those as written would greet the client several times.
- **Authoring notes become composition slots.** `[Insert next defense/advice]`
  points at another section and must never print.

### Using it

The **Advice letter** mode in the app lists the catalog grouped by topic, with a
"needs review" badge and reason on anything unverified. Set what is true about
the case, ask for suggestions, then pick sections — they appear in the letter in
the order you choose them, reorderable afterwards. The preview shows the
assembled body with its reading grade and page estimate; **Download letter**
renders it onto the organization's letterhead.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/advice-letters/sections/` | Catalog with review state, grouped topics |
| `POST /api/advice-letters/recommend/` | Ranked sections for one case, with reasons |
| `POST /api/advice-letters/preview/` | Assembled body, warnings, readability |
| `GET /api/advice-letters/addressing/` | Recipient, address, and Re: line from the case |
| `POST /api/advice-letters/export/` | The letter as DOCX on letterhead |

Downloads are named to be findable later:

```
2026-08-02-garcia-robert-advice-letter-security-deposit-nonpayment-rent.docx
```

Date first so letters sort chronologically, then the client surname-first so a
client's letters group together, then up to three section names. The advocate
can rename any single letter before downloading. The pattern is editable at
**Organization settings** in Django admin with `{date}`, `{client}`,
`{sections}`, `{case}`, and `{kind}` — an organization that files by case number
can use `{case}-{date}-{kind}` instead.

### Attorney review

Every section is loaded and offered, including ones the source documents never
finished — the practical way to review this corpus is to read it in place.
`needs_attorney_review`, not the absence of a row, marks unchecked text, and
each flagged section carries a one-line `review_reason` shown next to it in the
picker and in any letter that uses it.

Withholding flagged sections was the earlier behaviour and it hid the best match
for a case: the 3-day-notice defect scored highest but never appeared, because
its file still had tracked changes.

Sections are flagged when tracked changes were accepted here, when a passage sat
on a merge boundary, when the text was drafted here rather than maintained, or
when reviewer comments were dropped. Review them at
**Templates › Advice letter sections** in Django admin, which explains each
reason inline and shows the readability and copy-edit reports. Filter by
**Needs attorney review**, edit the body if it needs it, clear the flag, and
save — or use the **Mark as reviewed** bulk action.

Saving in admin sets `is_locally_edited`, and `ingest_advice_letters` will not
overwrite the text, status, or review state of such a section. Editing
`advice-letters/catalog.yaml` and `selection-hints.yaml` in the private content
repository remains the right path for a change the whole organization should
keep; it simply will not undo anything already decided in admin.

### Copy-editing accepted text

Accepting an editor's marks reproduces their mistakes faithfully. In the
security-deposit section the editor deleted "returning some or all of" and
inserted "all", so the accepted text reads "explain why they're not all the
deposit to you" — correct as a merge, and ungrammatical.

So the copy-edit pass splits the work. Mechanical damage is repaired: 41
non-breaking spaces and 7 doubled spaces across the corpus, plus spaces before
punctuation, missing spaces after it, and doubled punctuation. Legal citations
are protected, so `R.C. 5321.04` and `Civ.R. 5(B)(4)` survive intact.

Anything needing judgment is reported and left alone. Every paragraph that sat
on a merge boundary is flagged for a human read, because that is where a
half-finished edit surfaces; a section with such a flag stays out of the default
picker. Wording is never rewritten — a copy-edit that quietly changed the advice
would be worse than the artifact it fixed. Sentences broken by the editor's own
edit are repaired by name in `advice_letter_completions.MERGE_REPAIRS`, each
recording what the accepted text said and why the replacement is what the editor
meant.

**Selection hints** live in `selection-hints.yaml` beside the catalog, written
once and never overwritten so edits survive re-ingestion. Each says when to send
a section — `triggers` matched against case facts, `requires` conditions that
must hold, `excludes` sections that contradict it. Ranking runs through
`recommend_advice_sections()`, alongside the litigation template scorer, and
every result carries its reasons.

## Plain-Language Checking

Client-facing text is scored against
`content/drafting-rules/checks/plain-language.yaml`, which encodes the working
group's own guidance: sentences under 14 words, a jargon substitution list
(premises→home, vacate→move), terms to define or avoid, and page targets.

```bash
.venv/bin/python backend/manage.py check_readability --advice-sections
.venv/bin/python backend/manage.py check_readability letter.txt --verbose-findings
```

Several formulas run together — Flesch-Kincaid, SMOG, Flesch reading ease,
Gunning fog — and are reported side by side rather than reconciled. None is
authoritative: they count syllables and sentence length and cannot tell whether
a word is familiar, so a disagreement between them is a reason to read the
passage. The organization's concrete rules carry more weight than any score.
The check is invoked deliberately — from review, the command, or advice-letter
ingest — and is not folded into generation.

## Saving Work Back To LegalServer

Anything the tool produces can be written to the LegalServer case file: a
generated document as an uploaded case document, and a research answer or triage
assessment as a case note.

Two controls, chosen by how the work is produced:

- **A checkbox on the action**, for one-shot work. The draft export defaults to
  saving, because a generated document is lost work if it is not filed; the
  advocate clears the box to opt out. Triage and research default to not
  saving, because an assessment or a search answer is a working judgment that
  may not belong on a client's file. Change these with
  `LEGALSERVER_SAVE_DOCUMENTS_DEFAULT`, `LEGALSERVER_SAVE_RESEARCH_DEFAULT`, and
  `LEGALSERVER_SAVE_TRIAGE_DEFAULT`.
- **An explicit Save to LegalServer button**, for work revised in place: the
  advice letter and case chat. Downloading an advice letter does not file it.
  These get a button instead of a checkbox because they are rewritten several
  times in a sitting, and a checkbox on each download would leave five letters
  on the case with no way to tell which was sent.

A repeat save from the button **updates what it filed before** rather than
adding a copy. Each artifact carries a scope key — one advice-letter draft, one
chat thread — and the button reads "Update in LegalServer" once this session has
filed one. Notes are matched by an `external_id` the tool sets; documents are
matched by name **scoped to the matter**. That scoping is not optional: an
unscoped name match, posted from a different case, was observed to match a
document on the first case and reattach it to the second, moving one client's
document onto another client's file.

When a draft with recorded AI component versions is filed, the app also creates
an **AI usage audit** case note. Refiling the same draft updates that note by its
stable `ai-audit:draft:<id>` external id. The note lists each model-written
component version (including versions later superseded by an attorney edit),
the output paragraph by paragraph, the refinement instruction when one was
recorded, and its typed source bindings. The document upload remains available
if this second write fails, and the export response reports the note failure
separately.

Every DOCX exported from a saved draft carries the same structured record in
the `Legal Drafting Tool AI Audit JSON` OOXML custom property. Companion custom
properties expose the schema version and interaction, paragraph, and source
counts. An empty versioned payload means no AI component version was recorded;
the absence of the property means the document did not pass through this export
pipeline. Custom properties preserve the full provenance record without adding
audit text to the document's visible or filed prose.

Starting a new chat thread starts a new note.

Every case note the tool writes ends with a line saying it was machine-written
and has not been reviewed by an attorney.

`LEGALSERVER_ALLOW_WRITES=false` turns off every write while leaving retrieval
working. It is forced off while the test suite runs: a developer's `.env` points
at a real site, and a document uploaded to a client's file cannot be taken
back.

### Endpoints and permissions

Each write follows its published v2 contract, and each needs its own role
permission on the site. A missing permission answers 403, which the delivery
record reports verbatim.

| Write | Endpoint | Names the matter by | Permission |
| --- | --- | --- | --- |
| Case note | `POST /api/v2/notes` | `module_id`, the **numeric** matter id | API Create Note |
| Document | `POST /api/v2/documents`, multipart | `module_uuid`, the matter **UUID** | API Create Document |
| Case properties | `PATCH /api/v2/matters/{case_uuid}` | the matter **UUID** in the path | API Matter: Update (Premium) |

Note the inconsistency in the middle column, which was confirmed against a live
site: the notes endpoint wants the numeric matter id and rejects a UUID with
`invalid_values`, even though the published request schema types `module_id` as
a UUID. The notes endpoint also requires `note_type`, which the schema marks
required only on the response; set it with `LEGALSERVER_CASE_NOTE_TYPE` and see
`/api/v2/lookups/note_type` for a site's values.

Neither identifier is the case number this app stores as a matter's external id.
A case whose synced payload carries neither a `matter_uuid` nor a numeric id is
not written to at all, and each write checks for the one it needs. In
particular, a numeric id is never derived from the digits on the end of a case
number: case numbers restart each year, so that guess would eventually file a
note on a different client's case. Re-syncing the case from LegalServer
populates both identifiers.

Uploads send the file as multipart alongside its metadata and answer 201 for a
new document, or 200 when an upsert matched an existing one. Set
`LEGALSERVER_DOCUMENT_TYPE` to file every upload under a document-type lookup
value such as `Brief`; leave it blank to keep the site's default.

An upload never stands between an advocate and their document. If LegalServer is
unreachable, misconfigured, or rejects the write, the export still downloads and
the response carries `X-LegalServer-Delivery: failed` with a readable message.
Each attempt — saved, skipped, failed, or previewed — is recorded as a
`LegalServerDelivery` row, readable in Django admin under **LegalServer
deliveries** and over the API at `/api/cases/<matter_id>/legalserver/`. Cases
typed in by hand and seeded samples have no LegalServer matter behind them, so
the control says so instead of offering a save that would do nothing.

### Triage outcomes and case properties

A triage outcome can also set case properties on the matter. Which properties,
and to what, is office policy and depends on fields a site may not have yet, so
the rules are file-backed in `content/legalserver-field-maps/triage-outcome.yaml`
rather than written in Python:

```yaml
enabled: false   # nothing is sent while this is false
dry_run: true    # evaluate and record the values, but do not write
rules:
  - name: priority-full-representation
    when:
      priority_label: [Full rep]
    custom_fields:
      ai_triage_outcome: "{priority_label} ({confidence})"
```

The shipped map is turned off and names no fields, so the hook is in place and
inert. To adopt it: fill in the field names your site actually uses, set
`enabled: true` while leaving `dry_run: true`, run a triage, and read the
previewed values the triage panel lists back. Turn `dry_run` off once they are
right. A rule may test `priority`, `priority_label`, `confidence`, `case_type`,
`rubric`, `matched_criteria_contains`, and `missing_information`; values may
embed any assessment field in `{braces}`. An unknown condition or placeholder is
rejected when the file loads rather than sent to LegalServer.

`set:` names ordinary matter fields from the Update A Matter body, such as
`case_status` or `legal_problem_code`. `custom_fields:` names custom fields **by
their database name** — `ai_triage_outcome_24`, not the label shown in the UI —
which is what the API matches on. A `null` value is ignored by the API rather
than clearing the field; map an empty string to blank one.

## Advocate Profiles

The letterhead and filing signature blocks need a title, direct phone, fax,
office, and bar number. LegalServer's users endpoint carries all of them:

```bash
.venv/bin/python backend/manage.py sync_author_profiles --dry-run
```

The command fills blank profile fields and leaves an advocate's own corrections
alone; pass `--overwrite` to replace them. Mapping lives in
`backend/apps/core/legalserver_profile.py`.

## Frontend Layout

```text
frontend/
├── package.json
├── index.html
└── src/
    ├── App.jsx               Main workspace state and mode orchestration
    ├── api/client.js         Backend API client and CSRF header handling
    ├── components/           Case, fact, research, template, and workflow panels
    ├── state/                Tested reducers for workspace state
    ├── editor/DraftEditor.jsx Lexical draft editor wrapper
    ├── main.jsx              React entry point
    └── styles/app.css        Application styling
```

Important frontend components:

- `CaseSelector`: selects and displays LegalServer-style matters.
- `ResearchPanel`: queries source connectors and shows retrieved support.
- `LibraryBrowser`: browses the imported corpus without a query — faceted case law, and treatises and statutes as a table of contents.
- `FactReview`: lets a human select candidate facts before drafting.
- `TemplatePicker`: selects templates and optional prewritten clauses.
- `TemplateBuilder`: creates a structured template outline from example text.
- `DraftEditor`: Lexical editing surface for generated draft text.
- `DocumentHistoryPanel`: per-section version history, the sources each version relied on, and restoring an earlier version.
- `PackagePanel`: the documents a plan produced, how they relate, and cross-document validation findings.

- `ArgumentGymPanel`: ranked opposition challenges against a brief, with the authority and case-record material behind each one.
Drafting state lives in `src/state/draftWorkspace.js`, a reducer covering the
rules for a multi-document session: the document list and the open document
move together, and validation state belongs to the document that produced it.
Derivation logic for the panels lives in plain `.js` modules
(`components/documentHistory.js`, `components/documentPackage.js`,
`components/caseCatalog.js`, `components/libraryBrowse.js`,
`components/argumentGym.js`) so it is
covered by `npm run test`; the `.jsx` components stay presentational.

## Searching the Corpus Without AI

Research has three views, and **Search the corpus** is the default. It searches
everything this application holds — case law, statutes, local ordinances,
treatises and handbooks — and never calls a model. The code that answers a
search, `backend/apps/sources/research/`, does not import `apps.ai` at all, and
a test enforces that: whatever happens to a model provider, a search still
returns the library's own answer.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/research/search/` | One search. `GET` with `?q=` works too, so a search can be linked to |
| `GET /api/research/search/status/` | What the index holds, what expansion can do, whether AI is available |

**What a query can say.** A quoted `"phrase"` and a citation (`R.C. 5321.04`,
`2023-Ohio-3480`, `197 Ohio App. 3d 484`, `24 C.F.R. 982.310`) are requirements,
not hints: only text that literally contains them comes back, and the section a
citation names outranks the chapter of commentary that discusses it. When
nothing in the corpus contains one, the search says which phrase or citation
went unmatched rather than ranking something else into the gap.

A subdivision is the one deliberate exception, and the response states it
rather than leaving it to be discovered. `R.C. 5321.04(A)(2)` **requires** the
section and **ranks** the division higher, because the section's own text
prints its number once in the heading and sets out `(A)(2)` separately, so
requiring the joined-up string would exclude the statute being asked for. Each
parsed citation reports `required` and `ranksHigher` so the difference is
visible. `court:`,
`county:`, `municipality:`, `district:`, `year:`, `status:`, `judge:`,
`title:`, `source:` narrow; `-word` excludes.

**Narrowing.** Results carry source type, court, county, appellate district,
municipality, year, publication status and judge, and each facet is counted
against the *other* narrowing in force, so the alternatives on offer are the
ones that would return something. County and appellate district both come from
`content/jurisdictions/ohio-counties.yaml`, so "Cuyahoga" and "Cuyahoga County"
are one value on the shelf and narrowing by either finds every decision.

**Expansion.** A researcher types "deficient notice"; the opinion says
"defective". Both bridges are file-backed and neither calls a model: a reviewed
thesaurus of terms of art, and a table of neighbours learned offline from corpus
co-occurrence by `manage.py build_research_index`. Every result says which of
the reader's own words it contains, which it does not, and which matches came
from an expansion. An exact query is never broadened.

The learned table records which corpus it was built from, and is used only when
that matches the corpus being searched — otherwise it is reported as built but
not applicable, with the command to rebuild it. The recorded identity is the
corpus's composition rather than its state, so correcting one decision does not
invalidate a table, while a re-ingest or a file copied from another deployment
does.

**AI, when it is asked for.** Reranking and synthesis are off by default, opt-in
per search, and applied on top of a finished result set. Every response reports
in words what a model did — including that it did nothing — and a provider that
is down or switched off degrades to the deterministic results with the reason
stated, rather than to an error. Reranking reorders the page being viewed and
never removes a result; a model that answers with ids it was not given is
reported as unusable rather than as a rerank that happened to change nothing.

**A `GET` cannot start generative work.** The linkable form of a search is
deterministic by construction: `aiRerank` and `aiSynthesis` in a query string
are refused and reported, not honoured. A link gets forwarded, crawled and
prefetched, and opening one should not spend money on a model call or hand it
the summary of whichever matter the link names. Send the same search as a
`POST` to run either.

See [`content/research-index/README.md`](content/research-index/README.md) for
the maintained thesaurus, the district map, the known-answer regression queries,
and how to build the learned neighbour table.

## Browsing the Library

The other two views. **Ask a question** runs the connectors and returns a cited
answer written by a model. **Browse the library** opens the same material
without a question, because a reader who does not yet know what to ask still
needs to see what has been imported.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/caselaw/catalog/` | The whole approved corpus, narrowed by metadata, with facet counts and paging |
| `GET /api/library/` | Every indexed treatise, handbook, and statute collection |
| `GET /api/library/<document-slug>/` | One document's table of contents as a tree, optionally filtered |

**Cases** lists every decision approved for search and narrows by county, court,
judge, year, authority level, publication and treatment status, case type,
subsidy program, and the statutes, regulations, issues, and cases each decision
cites. Values inside one facet are alternatives; facets combine. Each facet is
counted against the *other* narrowing in force, so the alternatives on offer are
the ones that would actually return something.

Metadata came out of documents rather than a controlled vocabulary, so the same
judge arrives with and without the honorific. Those are grouped as one value,
labelled with the spelling the documents use most; narrowing by either finds
both. Counties are handled further upstream: ingestion writes the canonical name
from `content/jurisdictions/ohio-counties.yaml`, and
`manage.py normalize_case_counties` brings a corpus imported earlier onto it. Facets a corpus
never filled in are not offered — a field no document supplied shows no chips
rather than an empty group.

### Where a date came from

Every date on a decision was read out of a scanned document by a model, so on
its own it is an assertion. `CaseLawDateProvenance` records one row per date
field: the wording the metadata sidecar carried, the sidecar's storage key and
checksum, and — where the document's own OCR text contains the date — the
passage that shows it, how it was written, and the label beside it.

The scanner reads the orders courts actually write dates in, including the
clerk's file stamp (`2009 FEB 17 PM 2:47`) that carries most trial-court
decision dates, and tolerates the ways this corpus was scanned: days split
across a space (`MAR 1 6 2005`), missing punctuation, two-digit years. Roughly
three quarters of dated decisions are corroborated this way.

`corroborated` means the document contains the date, not that the date has been
confirmed to mean what its field says. Every scanned decision in the database
carries complete OCR — median 1,437 characters per page, none truncated — so an
uncorroborated date is one the document does not print in a readable form, not
evidence of a missing text layer. Usually the document shows other dates from
the same year or month, which is what OCR damage to a file stamp looks like, and
what a date recorded from a docket rather than from the page looks like. It is
shown with that caveat instead of hidden.
Review them at **Case law › Case law date provenances** in Django admin, or
inline on the decision.

`python backend/manage.py backfill_decision_dates` fills dates that an earlier
import dropped and records their provenance. It leaves a date that is already
set alone, so it is a no-op once the corpus is dated; `--overwrite` regenerates,
and `--dry-run` reports without writing.

Opening a document is fast because parsed manifests are cached per process,
keyed by each file's modification time and size — a regenerated manifest is
picked up on the next request, never served stale. That parse is not cheap: the
Ohio Revised Code manifest is 2.5 MB of generated YAML, and the pure-Python
parser spent nearly four seconds on it alone. Reads go through
`apps.sources.library.load_manifest()`, which also uses libyaml where PyYAML
provides it; do not parse a manifest directly.

**Treatises and handbooks** and **Statutes** open a document as a tree built
from the same generated manifests retrieval reads, so a section reached by
walking the contents opens the chunk a citation points at, with the same
identifiers, PDF page, and provenance. A section that produced one chunk is the
row itself; a section split across several keeps one row per part. Filtering
matches headings, section paths, and citations — full-text lookup is what the
research search is for. A private edition of a document shadows the public one
here exactly as it does during retrieval.

## Current Prototype Notes

- The source connectors are stubs with realistic contracts, not production LegalServer, SharePoint, or vector search integrations.
- SQLite is used for local development.
- Export currently returns editable plain text. DOCX export belongs behind `backend/apps/exporting/`.
- More detailed architecture notes are in `docs/ARCHITECTURE.md`.

### Argument Gym counterfactual benchmark

The [synthetic reasoning benchmark](content/argument-gym/README.md) includes six
minimal pairs (12 scenarios) and six adversarial/control cases for authority
weight, local applicability, temporal validity, proposition support, goal
alignment, factual certainty, and quotation fidelity. Run
`.venv/bin/python backend/manage.py test apps.argument_gym.test_benchmarks` for
offline harness checks. `run_gym_benchmark` exports inputs, grades recorded
responses, or explicitly runs a live model; see the linked guide for commands
and the distinction between harness tests and model-performance evidence.

### Import an approved SharePoint library item

Configure the SharePoint site, drive and service token in Source configurations
(or `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`, `SHAREPOINT_ACCESS_TOKEN`). In
managed sources, create a source and set **Source system id** to
`sharepoint:<configured-drive-id>:<approved-item-id>`. This explicit operator
binding approves the item for staging into the shared corpus; select only
material suitable for publication to the application's users.

```bash
.venv/bin/python backend/manage.py import_sharepoint_source approved-source-slug
```

The command downloads at most 25 MiB, verifies that the ETag did not change during
retrieval, stages the bytes through `DocumentStorage`, and runs the existing
publication parser. Review metadata and publish using the managed-source admin.
No request waits for the download or extraction. Re-running unchanged bytes is a
no-op; failed extraction can be retried through the same command. Scans without
readable text report failure rather than producing empty retrieval chunks.

Graph 401/403/404/429 failures are explicit. A short throttle delay is retried
once; longer delays are reported for later retry. Renew expired tokens before
retrying. Download redirects use a separate unauthenticated HTTPS session for
standard `*.sharepoint.com` / `*.1drv.com` hosts; sovereign-cloud download hosts
are not supported by this integration. Temporary download URLs are never saved.
See [the SharePoint boundary](docs/ARCHITECTURE.md#sharepoint-document-boundary).

Authenticated `GET /api/sharepoint/precedents/?q=lease&limit=25` searches the
configured library and returns the existing source-result shape with Graph
provenance. `q` is required (up to 500 characters); `limit` is 1–50, default 25.
Results keep Graph's ordering. The response explains that AI did nothing and
that this is a bounded result window, not the whole library. Requests cannot
select another site or drive. Search stores no results or document bytes.
An unconfigured connection returns 503; Graph authentication, permission,
not-found, and throttle failures return 401/403/404/429 with an error code.

In **Research → Browse the library → SharePoint precedents**, enter a search to
see document names, metadata, and links that open originals in SharePoint.
Empty results, missing configuration, denied access, and throttling are shown as
distinct states. Searching and opening links do not publish documents.


## Security deployment requirements

LegalServer identities are administrator-managed. Existing mappings migrate as unverified; an administrator must verify the local login email and remote account, then mark the mapping verified in Django admin. Admin changes are recorded in Django's audit log. Self-service remapping is disabled,
and generic Django staff status does not grant all-case access. Exact unique active
remote users must match the approved local login email before remote roles apply.
Grant the separate `write_legalserver_documents`, `write_legalserver_notes`, and `write_legalserver_properties` permissions only to the intended users/groups. Ordinary case access alone never grants those writes. LegalServer case reads and deliveries recheck current assignments; unavailable
upstream authorization denies access. There is no offline permission grace period.

Example-derived templates require template-add permission and remain inactive until
reviewed and enabled in admin. Shared rendering prohibits internal attribute access
and arbitrary calls. Draft/regeneration text and uploaded-template fill rendering run in the bounded worker. Printed containers are checked before string conversion. Maintained-template export rendering uses the same restricted environment; full export process isolation remains a release verification item.

Case documents preview inline only with a PDF signature and an explicit PDF MIME type;
other documents download as attachments. Attachment responses carry sandbox CSP; PDF previews retain the framing policy, and both send nosniff.
nginx proxies `/media/` to Django. Only users with the matching template, block, or letterhead view permission may download registered assets; anonymous access and arbitrary storage paths are denied. Downloads are attachments with no-store caching. Admin uploads and server-side exports continue to use storage directly.

General extraction accepts at most 15 MB, 40 MB expanded DOCX, 2,048 archive members,
300 PDF pages and 4 MB of text. Linux parser subprocesses have a 20-second CPU budget,
25-second wall timeout, 768 MB memory limit and two concurrent slots per container/host. The worker receives no application environment credentials and its process group is terminated on timeout. This is resource containment, not a filesystem or network sandbox.
Optional extraction backends must fit these same limits; temporary files are private
and removed after conversion. These limits do not replace deployment-wide quotas.

The deployment workflow uses the `production` environment and job-scoped OIDC.
Configure required environment reviewers, reviewed PRs, required status checks and
push/force-push/deletion restrictions in GitHub; the workflow alone does not enforce
those repository settings. Check Azure's federated trust matches the environment.
Do not treat passing local tests as verification of the deployed image or controls.

### Argument Gym annotated downloads

After a completed run, **Download annotated copy** exports all stored challenges
(including addressed and dismissed ones) as native DOCX comments or PDF page
annotations. This performs no new AI calls and does not apply suggested edits.
Word comments anchor to uniquely matched containing paragraphs, including table
cells. PDF notes anchor to a uniquely matched page, not text coordinates. Missing
or ambiguous quotations become explicitly unplaced notes (an appended paragraph
in Word, or notes on PDF page 1). The export annotates challenges, not every
format/compliance result; use the run report for full check coverage.

New uploads retain immutable originals through `apps.core.storage`, in
`raw/argument-gym/<workspace>/<unique-id>.<extension>` and, following successful
ingestion, the corresponding `published/` path. Downloads require workspace
access and verify the original's SHA-256. Earlier uploads without originals
must be uploaded and reviewed again. Native drafts export as DOCX only while
their run snapshot remains current. PDF exhibits remain in the downloaded file,
but annotation matching is limited to the brief's reviewed page range.

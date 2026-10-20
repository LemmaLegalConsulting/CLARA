# CLARA — Cleveland Legal Aid Research Assistant Architecture

This implementation is organized around reviewable workflow boundaries rather than a single free-form agent.

## Backend

- `apps.matters`: LegalServer-style matter records and extracted candidate facts.
- `apps.sources`: pluggable retrieval connectors. New source types implement `SourceConnector.search()` and are registered in `registry.py`.
- `apps.templates_app`: reusable document templates, deterministic blocks, optional clauses, and template creation from example text.
- `apps.templates_app` also owns the loss-minimizing DOCX ingestion and manifest
  index. `content/document-templates/` is authoritative for file-managed
  templates; database rows are a query/index cache and never replace the files.
- `content/`: provider-neutral, file-backed defaults for reusable DOCX snippets, authoritative treatise PDFs and Markdown derivatives, and triage rubric seeds. Django packages consume logical content paths rather than owning these legal assets.
- `apps.drafting`: drafting sessions, selected facts/source results/block keys, draft documents, and workflow advancement.
- `apps.drafting.components`: the durable artifact layer under `DraftDocument`. See [Document components and versions](#document-components-and-versions).
- `apps.ai`: constrained AI service boundary, including the YAML prompt catalog and optional database overrides. Default prompts are repository files in `prompts/`, which keeps benchmark variants reviewable and independent from application code.
- `apps.validation`: output checks for missing facts, tentative language, citation-like strings, and length.
- `apps.exporting`: export adapters. The first adapter exports editable plain text; this is the boundary for future DOCX generation.

## Document components and versions

`DraftDocument.sections` is the JSON shape the editor, validation, and export
paths read, and it stays that way. Underneath it, every section is also stored as
a durable domain object:

- `DocumentComponent`: one addressable part of a document, identified by a
  `stable_key` (normally the template block key) that survives rewrites of the
  section JSON. A component dropped from the document is retired with
  `removed_at` rather than deleted, so its history stays auditable.
- `ComponentVersion`: an append-only body/`structured_content` snapshot with the
  `origin` that produced it (`template`, `ai`, `human`, `validation_repair`,
  `rollback`) and the instruction that was given.

`apps.drafting.components.record_sections()` is the single write path: it saves
the section JSON, refreshes plain text, and records a version for each component
whose content actually changed. Generation, block regeneration, reviewer edits
through `PATCH /api/drafts/<id>/`, and validation auto-repair all go through it,
so each one is attributable. `GET /api/drafts/<id>/components/` returns the
per-component version history.

This is what makes narrower operations possible: regenerating one section no
longer discards the text it replaced, and the document's prior states remain
recoverable.

## Filing packages

A plan can produce a motion, a memorandum, a declaration, and a proposed order.
They are filed together and must agree with each other, so `apps.drafting.packages`
gives each document in a session a package role and records the relationships
between them as `PackageRelationship` rows (`implements_relief`, `incorporates`,
`depends_on`, `cites`, `authenticates_exhibit`).

Roles come from the template's `metadata.packageRole` when it declares one, then
from the document title, then from the template kind. Declaring the role in
template metadata is the preferred path: a new document family should arrive as
content, not as Python.

`apps.validation.packages` (rule codes 800-899) is the first validation that
looks across documents: a caption case number that disagrees with the rest of
the package (`W800`), an exhibit no declaration identifies (`W810`), and a
companion document the text promises but the package does not contain (`W820`).
A session with one document is not package-validated.

`GET|POST /api/drafting-sessions/<id>/package/` returns the package composition
and relationship graph; POST re-derives the relationships.

## Source bindings

A drafting session holds one flat list of selected sources. `SourceBinding`
records which component version actually used which source, in which role:

- `record_evidence` — a case document or matter fact; supports a fact, not a
  legal rule.
- `legal_authority` / `procedural_rule` — the only roles a draft may cite as
  authority.
- `example_language` — a prior filing that may guide wording only
  (`support_type = style_only`).
- `background_reference` — context that supports nothing on its own.

`apps.drafting.source_bindings` derives bindings from how a section was
composed (facts blocks bind selected facts; constrained-generation blocks bind
the reviewer-approved sources) and records them against the component version,
which is immutable, so history keeps the provenance of each earlier draft.

`apps.validation.source_integrity` (rule codes 700-799) uses them to check the
source *type* against the assertion, which citation-string linting cannot do: a
section that states a legal proposition backed only by example language is
flagged (`W700`), as is one with no citable authority bound at all (`W710`).
`SourceBinding.verified` is the hook for the next layer — checking that a
locator resolves and that quoted text matches the source.

## Draft operations

`apps.drafting.operations` describes a change to a document before it is made.
A `DraftOperation` names its type (`replace_component`, `insert_component`,
`delete_component`, `move_component`, `revert_component`), its target component,
a payload, a rationale, and its status (`proposed`, `applied`, `rejected`).

Proposal and application are separate. `propose()` validates the described
change against the current document and stores it without touching the draft;
`apply()` runs the deterministic applier in a transaction and records the
resulting component versions. A resolved operation cannot be applied twice.

Block regeneration and reviewer-initiated edits go through this path, so the
draft's change log is the operation list rather than a series of anonymous
overwrites. `revert_component` restores an earlier component version as a new
version, which is how a bad regeneration is undone without losing the record
that it happened.

The API is `GET|POST /api/drafts/<id>/operations/` and
`POST /api/drafts/<id>/operations/<operation_id>/decision/` with
`{"decision": "apply" | "reject"}`. Proposing with `{"apply": true}` records and
applies in one request, which is what an already-approved reviewer edit does.

This is also the seam for any future model-driven drafting stage: something that
may propose operations but not write documents needs no new permission model,
because proposals are inert until a reviewer applies them.

## Frontend

The React app mirrors the backend boundaries:

- `CaseSelector`: matter selection and case summary.
- `ResearchPanel`: source-aware retrieval across LegalServer, SharePoint, RAG content, local cases, and user resources.
- `FactReview`: human review of candidate facts before generation.
- `TemplatePicker`: deterministic template and clause selection.
- `TemplateBuilder`: create a structured template outline from example text.
- `DraftEditor`: Lexical editor boundary for draft review and manual edits.

## Workflow Modes

- `research`: retrieval only, no draft required.
- `draft_from_template`: linear case -> facts -> template -> law/source support -> draft -> validation/export.
- `draft_from_scratch`: uses a pleading shell and instructions for section-level constrained generation.

The stub connectors are intentionally replaceable. They make the application runnable now while preserving clean integration points for real LegalServer, Microsoft Graph/SharePoint, vector search, archived case indexes, and user-upload pipelines.

## Maintained legal-content library

The repository's top-level [`content/`](../content/README.md) directory is the
default local content provider. It has three independently auditable areas:

- `docx-snippets/` provides shared blocks and per-template-pathway overrides.
  Admin uploads remain the highest-precedence operational override.
- `document-templates/` provides full-document DOCX/Jinja packages. Its manifest
  maps input fields and list expectations to Lexical blocks. Full-document export
  preserves the source package's headers, tables, numbering, media, and sections;
  pathway snippets remain available for block composition and overrides.
- `treatises/source/` retains authoritative incoming PDFs; `treatises/markdown/`
  holds deterministic heading-preserving derivatives for a future chunking and
  retrieval ingestion workflow.
- `triage-rubrics/` holds YAML seed records. Synchronization creates missing
  database records and does not overwrite an existing admin-managed rubric
  unless the explicit management-command update flag is used.

`CONTENT_LIBRARY_DIR` permits a staged local copy of this layout. A SharePoint
implementation belongs behind this provider boundary, selected explicitly by an
administrator at the organization/package level. It should preserve remote item
identity, ETag, modified time, source path, checksum, and import time alongside
every processed record; it should not silently replace repository defaults.

### SharePoint document boundary

SharePoint is an optional, read-only upstream integration. The supported library
workflow searches the configured Graph site/drive with the effective delegated
or configured service identity, opens original documents in SharePoint, and lets
an operator stage individually approved items into managed-source publication.
Graph enforces that identity's permissions. A delegated authentication failure
never retries with service credentials; expired connections require sign-in or
operator token renewal. Service credentials should be scoped to the approved
library, since all application users using that fallback share its permissions.

Graph metadata and bounded byte downloads live in `apps.sources.connectors.sharepoint`.
The adapter `apps.sources.sharepoint_import` uses the existing publication parser
and `DocumentStorage` raw/validated/published areas, including Azure Blob when
configured. Azure-native published storage remains the runtime corpus. Searches
do not import documents, and importing does not publish them. No SharePoint
writeback, storage provider replacement, or synchronization daemon is introduced.

For approval, an operator creates/edits a managed source and sets its
`source_system_id` to `sharepoint:<configured-drive-id>:<approved-item-id>`.
The import command accepts that source's slug, never an arbitrary URL or drive.
The command uses the configured service identity; Graph may still deny an
approved item. Approval means this material may enter the application's shared
corpus after publication review, independently of the original library's ACLs.
Version provenance records site/drive/item IDs, original web URL and parent path,
name, MIME type, modified time, ETag, fetch time and SHA-256 before parsing. It
never records a temporary authenticated download URL. Duplicate bytes preserve
the first imported version and its provenance; a changed version with new bytes
creates a new candidate. Failed extraction remains visible on the version.

The existing case-folder inventory remains available, but CLE's use of SharePoint
for case-document content is not confirmed in this repository. Connecting those
bytes to case-context selection (#45) is deferred until that workflow is confirmed.
OCR/full-text corpus coverage (#60) is a separate workstream.

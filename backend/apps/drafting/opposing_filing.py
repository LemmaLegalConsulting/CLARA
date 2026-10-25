"""The opposing filing a draft answers, identified once and used throughout.

A reply is written against the brief in opposition and a motion to dismiss
against the complaint. The template says whether it answers a filing
(``metadata.respondsTo``), and this module records which one, reads its text
for drafting, and names it in the finished document.

The filing is either in the case file or it is not. One in LegalServer is
referenced, never copied: its text is read through the case-file connector when
a draft is generated, so the advocate's own access decides what is readable.
One that never reached the case file -- served by email, handed over at a
hearing -- is uploaded, stored in the document store's ``raw/`` area, and its
exhibits separated from the brief so only the brief is sent to a model.

A template that requires the filing refuses to generate without it. A reply
drafted without the opposition answers arguments the other side never made,
and reads as though it answered the ones they did.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date

from django.utils.text import get_valid_filename

from apps.core.storage import RAW, get_document_storage
from apps.drafting.models import OpposingFiling


# What a drafting prompt receives of the filing. A section prompt repeats it,
# so it is capped well below what a single read of a brief could take, and the
# cap is stated in the prompt rather than applied silently.
PROMPT_TEXT_LIMIT = 30_000
UPLOAD_PREFIX = "drafting/opposing-filings"

# How a filing kind reads in a sentence when the advocate has not described it.
KIND_LABELS = {
    "complaint": "Plaintiff's Complaint",
    "amended_complaint": "Plaintiff's Amended Complaint",
    "counterclaim": "the counterclaim",
    "motion": "the motion",
    "brief_in_opposition": "Plaintiff's Brief in Opposition",
}
MISSING_MARKER = "[Attorney review required: identify the filing this document answers]"


class OpposingFilingError(ValueError):
    pass


def requirement(template):
    """What the template says about the filing it answers ({} when nothing)."""
    return dict(((getattr(template, "metadata", None) or {}).get("respondsTo")) or {})


def filing_for(session):
    # Queried rather than read through ``session.opposing_filing``, whose cache
    # keeps answering with a filing after it has been replaced or removed.
    return OpposingFiling.objects.filter(session=session).first()


def missing_requirement(session, template=None):
    """The reason generation must wait, or "" when it need not."""
    template = template or session.template
    rule = requirement(template)
    if not rule.get("required") or filing_for(session):
        return ""
    title = getattr(template, "title", "This document")
    return (
        f"{title} answers the other side's filing. "
        f"{rule.get('question') or 'Identify the filing it answers'} "
        "Choose it from the case file or upload it before generating the draft."
    )


def missing_for_session(session):
    """The first unmet requirement among the templates this session will draft."""
    from apps.templates_app.models import DocumentTemplate

    templates = [session.template] if session.template else []
    templates += list(
        DocumentTemplate.objects.filter(id__in=session.selected_template_ids or [], is_active=True).exclude(
            id=getattr(session.template, "id", None)
        )
    )
    return next((message for message in (missing_requirement(session, template) for template in templates) if message), "")


def _default_description(title, kind):
    """A readable name for the filing, from its title or its kind."""
    stem = re.sub(r"\.(pdf|docx?|txt|rtf)$", "", str(title or ""), flags=re.IGNORECASE)
    stem = re.sub(r"^\d{4}[-.]\d{2}[-.]\d{2}\s*[-_]?\s*", "", stem)
    stem = re.sub(r"\s*-\s*FILED$", "", stem, flags=re.IGNORECASE).strip()
    return stem or KIND_LABELS.get(kind, "")


def _default_kind(template):
    expects = requirement(template).get("expects") or []
    return expects[0] if expects else ""


def _document_date(document):
    raw = document.get("raw") or {}
    for value in (document.get("date"), raw.get("date"), raw.get("filed_date"), raw.get("date_filed"), raw.get("created_at"), raw.get("date_created")):
        if value:
            return str(value)
    return ""


def _reference_for(matter, document):
    return {
        "system": matter.source_system,
        "matterExternalId": matter.external_id,
        "documentId": document["id"],
        "kind": document.get("kind", ""),
        "title": document.get("title", ""),
        "date": _document_date(document),
        "url": (document.get("raw") or {}).get("url", "") or document.get("url", ""),
    }


def case_file_choices(session, *, client=None):
    """``(choices, problem)``: case-file documents the advocate may pick, newest first.

    This tool's own drafts are left out: a reply "answering" an earlier draft of
    itself is answering nothing the other side filed. ``problem`` says why the
    list may be incomplete, so an unreachable case file never reads as an empty
    one.
    """
    from apps.matters.document_context import get_case_documents_with_status

    documents, problem = get_case_documents_with_status(session.matter, client=client)
    documents = [
        document
        for document in documents
        if not document.get("workProduct") and document.get("kind") != "case_note"
    ]
    documents.sort(key=_document_date, reverse=True)
    return [
        {
            "documentId": document["id"],
            "title": document.get("title") or "Case document",
            "date": _document_date(document),
            "kind": document.get("kind", ""),
            "snippet": document.get("snippet", ""),
        }
        for document in documents
    ], problem


def _replace(session, **values):
    OpposingFiling.objects.filter(session=session).delete()
    return OpposingFiling.objects.create(session=session, **values)


def choose_case_document(session, document_id, *, user=None, client=None, filing_kind="", description="", filed_on=None):
    from apps.matters.document_context import get_case_documents

    document = next(
        (
            item
            for item in get_case_documents(session.matter, client=client)
            if str(item["id"]) == str(document_id) and not item.get("workProduct")
        ),
        None,
    )
    if not document:
        raise OpposingFilingError("That document is not in this case file, or is not available to you.")
    kind = filing_kind or _default_kind(session.template)
    title = document.get("title") or "Case document"
    return _replace(
        session,
        source_type=OpposingFiling.MATTER_DOCUMENT,
        filing_kind=kind,
        external_reference=_reference_for(session.matter, document),
        title=title,
        description=description or _default_description(title, kind),
        filed_on=filed_on or _parse_date(_document_date(document)),
        created_by=user,
    )


def upload_filing(session, content, *, filename, content_type="", user=None, filing_kind="", description="", filed_on=None):
    from apps.argument_gym.ingestion import ingest_upload
    from apps.sources.document_text import DocumentExtractionError

    try:
        ingested = ingest_upload(content, filename=filename, content_type=content_type)
    except DocumentExtractionError as exc:
        raise OpposingFilingError(str(exc)) from exc
    if not ingested["text"].strip():
        raise OpposingFilingError(
            "No readable text could be extracted from this file. A scanned filing needs OCR before it can be read."
        )
    checksum = hashlib.sha256(content).hexdigest()
    key = f"{UPLOAD_PREFIX}/{session.id}/{uuid.uuid4().hex}/{get_valid_filename(filename) or 'filing'}"
    get_document_storage(RAW).put_bytes(
        key=key, content=content, content_type=content_type or "application/octet-stream"
    )
    kind = filing_kind or _default_kind(session.template)
    metadata = dict(ingested["metadata"])
    # The attachments are recorded by title and page range; their text is not
    # sent to a model as though it were the brief.
    metadata["exhibits"] = [
        {"title": exhibit["title"], "pageRange": exhibit["pageRange"]}
        for exhibit in ingested.get("exhibits") or []
    ]
    return _replace(
        session,
        source_type=OpposingFiling.UPLOAD,
        filing_kind=kind,
        storage_key=key,
        checksum=checksum,
        title=filename,
        description=description or _default_description(filename, kind),
        filed_on=filed_on,
        original_filename=filename,
        content_type=content_type,
        extracted_text=ingested["text"],
        extraction_metadata=metadata,
        created_by=user,
    )


def _parse_date(value):
    if isinstance(value, date):
        return value
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_filed_on(value):
    """A filing date from the API, or None when none was given."""
    if not value:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        raise OpposingFilingError("Give the filing date as YYYY-MM-DD.")
    return parsed


def update_details(filing, *, description=None, filed_on=None, filing_kind=None):
    changed = []
    if description is not None:
        filing.description = str(description).strip()[:500]
        changed.append("description")
    if filed_on is not None:
        filing.filed_on = parse_filed_on(filed_on)
        changed.append("filed_on")
    if filing_kind is not None:
        filing.filing_kind = str(filing_kind).strip()[:60]
        changed.append("filing_kind")
    if changed:
        filing.save(update_fields=[*changed, "updated_at"])
    return filing


def remove(session):
    filing = filing_for(session)
    if not filing:
        return
    if filing.storage_key:
        try:
            get_document_storage(RAW).delete(filing.storage_key)
        except OSError:
            pass
    filing.delete()


def _format_date(value):
    return f"{value:%B} {value.day}, {value.year}" if value else ""


def display_name(filing):
    """How the document names the filing: its description, and the filing date."""
    if not filing:
        return MISSING_MARKER
    name = filing.description or _default_description(filing.title, filing.filing_kind) or filing.title
    return f"{name}, filed {_format_date(filing.filed_on)}" if filing.filed_on else name


def render_value(session):
    """The ``responding_to`` value maintained wording and the Word export read."""
    filing = filing_for(session)
    return {
        "description": display_name(filing),
        "title": filing.title if filing else "",
        "kind": filing.filing_kind if filing else "",
        "filed_on": _format_date(filing.filed_on) if filing else "",
        "identified": bool(filing),
    }


def filing_text(filing, *, client=None):
    """``(text, problem)``: the filing's text, and why it is missing ("" if not). Never raises."""
    if filing.source_type == OpposingFiling.UPLOAD:
        return filing.extracted_text, ""
    from apps.matters.document_context import _document_url, get_case_documents, get_document_file, get_document_text
    from apps.argument_gym.ingestion import ingest_upload

    reference = filing.external_reference or {}
    try:
        document = next(
            (
                item
                for item in get_case_documents(filing.session.matter, client=client)
                if str(item["id"]) == str(reference.get("documentId"))
            ),
            None,
        )
    except Exception:  # noqa: BLE001 - the case file being unreachable is reported, not raised
        document = None
    if not document:
        return "", "The case file document could not be read; it may have been removed or the case file is unreachable."
    try:
        if _document_url(document.get("raw") or {}):
            downloaded = get_document_file(document, client=client)
            ingested = ingest_upload(
                downloaded["content"],
                filename=document.get("filename") or downloaded["filename"],
                content_type=downloaded["content_type"],
            )
        else:
            ingested = ingest_upload(
                get_document_text(document, client=client).encode("utf-8"),
                filename="filing.txt",
                content_type="text/plain",
            )
        # Keep the external reference and extraction facts, without persisting
        # another copy of the case file's brief or attachments.
        metadata = dict(ingested["metadata"])
        metadata.pop("units", None)
        if metadata.get("split"):
            metadata["split"] = {
                key: value for key, value in metadata["split"].items() if key != "exhibits"
            }
        metadata["exhibits"] = [
            {"title": exhibit["title"], "pageRange": exhibit["pageRange"]}
            for exhibit in ingested.get("exhibits") or []
        ]
        filing.extraction_metadata = metadata
        filing.save(update_fields=["extraction_metadata", "updated_at"])
        return ingested["text"], ""
    except Exception:  # noqa: BLE001 - never fall back to an unsplit filing
        return "", "The case file document could not be extracted and separated from its exhibits."


def prompt_context(session, *, client=None):
    """What a drafting prompt is told about the filing, or None when there is none."""
    filing = filing_for(session)
    if not filing:
        return None
    text, problem = filing_text(filing, client=client)
    note = ""
    if not text.strip():
        note = problem or "No readable text was found in it."
    elif len(text) > PROMPT_TEXT_LIMIT:
        note = f"Only the first {PROMPT_TEXT_LIMIT:,} characters of the filing are included; it is longer."
        text = text[:PROMPT_TEXT_LIMIT]
    exhibits = (filing.extraction_metadata or {}).get("exhibits") or []
    return {
        "description": display_name(filing),
        "kind": filing.filing_kind,
        "text": text,
        "note": note,
        "exhibits": [exhibit["title"] for exhibit in exhibits],
    }


def fingerprint(session):
    """Identity of the filing for deciding whether a generation is a repeat."""
    filing = filing_for(session)
    if not filing:
        return None
    return {
        "id": filing.id,
        "source": filing.source_type,
        "document": (filing.external_reference or {}).get("documentId"),
        "checksum": filing.checksum,
        "description": filing.description,
        "filedOn": str(filing.filed_on or ""),
    }


def to_dict(filing):
    if not filing:
        return None
    metadata = filing.extraction_metadata or {}
    return {
        "id": filing.id,
        "sourceType": filing.source_type,
        "filingKind": filing.filing_kind,
        "title": filing.title,
        "description": filing.description,
        "displayName": display_name(filing),
        "filedOn": filing.filed_on.isoformat() if filing.filed_on else "",
        "documentId": (filing.external_reference or {}).get("documentId", ""),
        "originalFilename": filing.original_filename,
        "pageCount": metadata.get("pageCount"),
        "exhibits": metadata.get("exhibits") or [],
        "truncated": bool(metadata.get("truncated")),
        "updatedAt": filing.updated_at.isoformat() if filing.updated_at else "",
    }

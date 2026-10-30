"""Review copies of a gym brief. No model calls and no changes to the source."""
import hashlib
import io
import re
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph
from django.http import HttpResponse
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Text

from apps.core.storage import get_document_storage


class AnnotationUnavailable(ValueError):
    pass


def _normal(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _quote(challenge, brief):
    target = challenge.target or {}
    quote = target.get("excerpt") or challenge.brief_currently_says
    if not quote:
        unit = next((u for u in brief.structure_units if u.get("id") == target.get("unitId")), {})
        quote = unit.get("text", "")
    return _normal(quote)


def _note(challenge, run):
    parts = [f"Argument Gym · run {run.pk} · challenge {challenge.pk}",
             f"{challenge.get_category_display()} · {challenge.severity} · {challenge.disposition}"]
    for label, value in (
        ("Finding", challenge.finding),
        ("What to do", challenge.coaching_recommendation),
        ("Passage", _quote(challenge, run.brief)),
        ("Opponent", challenge.opponent_argument),
        ("Why it matters", challenge.why_it_matters),
        ("Judge", challenge.judge_assessment),
        ("Suggested response (review before using)", challenge.suggested_response),
        ("Disposition note", challenge.disposition_note),
    ):
        if value:
            parts.append(f"{label}: {value}")
    for source in challenge.legal_sources or []:
        parts.append("Authority (verify before filing): " + " — ".join(str(source[k]) for k in ("title", "citation", "url") if source.get(k)))
    parts.append("AI-assisted review; verify facts and current law. This copy does not apply suggested edits.")
    return "\n\n".join(parts)


def annotate_docx(content, run):
    document = Document(io.BytesIO(content))
    # Includes table cells; do not rebuild paragraphs or discard run formatting.
    paragraphs = [Paragraph(p, document._body) for p in document.element.body.xpath(".//w:p")]
    texts = [_normal(p.text) for p in paragraphs]
    for challenge in run.challenges.all():
        quote = _quote(challenge, run.brief)
        matches = [p for p, text in zip(paragraphs, texts) if quote and quote in text and p.runs]
        note = _note(challenge, run)
        if len(matches) == 1:
            runs = matches[0].runs
            note = "Anchored to the containing paragraph.\n\n" + note
        else:
            paragraph = document.add_paragraph(f"Argument Gym — unplaced review note #{challenge.pk}")
            runs = paragraph.runs
            note = "UNPLACED: the passage was missing or ambiguous. Locate it manually.\n\n" + note
        document.add_comment(runs, text=note, author="CLARA Argument Gym", initials="AG")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def annotate_pdf(content, run):
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter(clone_from=reader)
    end = (run.brief.page_range or {}).get("end") or len(reader.pages)
    page_texts = [_normal(page.extract_text()) for page in reader.pages[:end]]
    counts = {}
    for challenge in run.challenges.all():
        quote = _quote(challenge, run.brief)
        matches = [i for i, text in enumerate(page_texts) if quote and quote in text]
        if len(matches) == 1:
            page_index = matches[0]
            placement = "Page-level note: the quoted passage occurs on this page; the icon is not a text highlight."
        else:
            page_index = 0
            placement = "UNPLACED: the passage was missing or ambiguous. This note is collected on page 1; locate the passage manually."
        page = writer.pages[page_index]
        count = counts.get(page_index, 0)
        counts[page_index] = count + 1
        box = page.cropbox
        x = float(box.left) + 12 + (count // 20) * 22
        y = float(box.top) - 30 - (count % 20) * 22
        annotation = Text(rect=(x, y, x + 18, y + 18), text=placement + "\n\n" + _note(challenge, run), open=False)
        writer.add_annotation(page_number=page_index, annotation=annotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def annotated_download(run):
    if run.status != "complete":
        raise AnnotationUnavailable("Finish the gym run before downloading an annotated copy.")
    brief = run.brief
    extension = Path(brief.original_filename).suffix.lower()
    if brief.source_type == "draft_document" and brief.draft_document:
        from apps.argument_gym.pipeline import brief_snapshot
        from apps.argument_gym.ingestion import text_checksum
        from apps.exporting.services import render_docx_bytes
        current = brief_snapshot(brief)
        current["checksum"] = text_checksum(brief.draft_document.plain_text or "")
        if current != run.snapshot:
            raise AnnotationUnavailable("This draft has changed since the run. Run the gym again before annotating it.")
        content = render_docx_bytes(brief.draft_document)
        extension = ".docx"
    elif brief.storage_key and extension in {".docx", ".pdf"}:
        try:
            with get_document_storage().open(brief.storage_key) as stream:
                content = stream.read()
        except FileNotFoundError as exc:
            raise AnnotationUnavailable("The original file is unavailable. Upload it again and run the gym.") from exc
        expected = (brief.extraction_metadata or {}).get("sourceChecksum")
        if not expected or hashlib.sha256(content).hexdigest() != expected:
            raise AnnotationUnavailable("The original file could not be verified. Upload it again and run the gym.")
    else:
        raise AnnotationUnavailable("An original DOCX or PDF is required. Older uploads must be uploaded again and reviewed in a new run.")
    payload = annotate_pdf(content, run) if extension == ".pdf" else annotate_docx(content, run)
    mime = "application/pdf" if extension == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    response = HttpResponse(payload, content_type=mime)
    response["Content-Disposition"] = f'attachment; filename="argument-gym-{run.pk}-annotated{extension}"'
    response["Cache-Control"] = "private, no-store"
    return response

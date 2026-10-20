"""Structured ingestion for externally authored briefs.

A CLARA-native draft already has stable anchors: every section is a
``DocumentComponent`` with a key that survives a rewrite. An uploaded brief has
none, so a challenge against "the third paragraph of Part III.A" would have
nothing to point at after the file is re-uploaded.

This module gives an external document the same kind of anchor: a lightweight
structure of sections, paragraphs, propositions, asserted facts, citations, and
requested relief, each with a run-local id and a source locator. It is
deliberately not a legal knowledge graph -- it exists so a card can say *where*
in the brief the problem is and so a reader can find that place again.
"""

import hashlib
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from apps.sources.document_text import DocumentExtractionError, extract_text


WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# A filed brief with its exhibits attached is mostly not a brief. Thirty pages
# is the practical ceiling on the argument in the filings this is built for, and
# it is a cap rather than a guess: the real boundary is detected below, and this
# only stops a missed boundary from sending three hundred pages to a model.
BRIEF_PAGE_LIMIT = 30
# Even within the brief, a stage's prompt gets a bounded slice. Text past this
# is dropped from the model's view and the run records that it was.
MAX_BRIEF_CHARS = 120_000

SECTION = "section"
PARAGRAPH = "paragraph"
ARGUMENT = "argument"
ASSERTED_FACT = "asserted_fact"
CITATION = "citation"
REQUESTED_RELIEF = "requested_relief"

# "III.", "A.", "1.", "(a)" -- the numbering a brief actually uses for its parts.
_NUMBERING = re.compile(
    r"^\(?(?P<label>[IVXLC]{1,6}|[A-Z]|\d{1,2})[.)]\s+(?P<rest>\S.*)$"
)
_ROMAN = re.compile(r"^[IVXLC]{1,6}$")
_RELIEF = re.compile(
    r"\b(wherefore|respectfully requests?|respectfully move[sd]?|prays? for|"
    r"requests? that (?:this|the) court|relief requested)\b",
    re.IGNORECASE,
)
_FACT_HEADING = re.compile(r"\b(statement of (?:the )?facts?|factual background|background)\b", re.IGNORECASE)
_ARGUMENT_HEADING = re.compile(r"\b(argument|discussion|analysis|law and argument)\b", re.IGNORECASE)
# Reporter cites, statutory sections, and short-form cites. Deliberately broad:
# a false positive costs a stray citation unit, a miss costs an anchor.
# An exhibit cover sheet is a nearly empty page that says what follows it.
_EXHIBIT_MARKER = re.compile(
    r"^\s*(?P<kind>exhibit|attachment|appendix)\s+(?P<label>[A-Z0-9]{1,4})\b[.:\s-]*$",
    re.IGNORECASE,
)
_EXHIBIT_ANYWHERE = re.compile(r"\b(exhibit|attachment|appendix)\s+([A-Z0-9]{1,4})\b", re.IGNORECASE)
_END_OF_BRIEF = re.compile(r"certificate of service", re.IGNORECASE)
_EXHIBIT_INDEX = re.compile(r"\b(index (of|to) exhibits|table of exhibits|exhibit list)\b", re.IGNORECASE)

_CITATION_PATTERNS = [
    re.compile(r"\b\d+\s+[A-Z][A-Za-z.]{1,12}(?:\s+[A-Za-z.]{1,8})?\s+\d+(?:\s*\([^)]{2,40}\))?"),
    re.compile(r"\b(?:R\.C\.|O\.R\.C\.|U\.S\.C\.|C\.F\.R\.|O\.A\.C\.)\s*§*\s*[\d.]+[A-Za-z\d.()]*"),
    re.compile(r"§+\s*[\d.]+[A-Za-z\d.()\-]*"),
    re.compile(r"\b[A-Z][A-Za-z'.\-]+\s+v\.\s+[A-Z][A-Za-z'.\-]+"),
]


def _collapse(text):
    return re.sub(r"[ \t]+", " ", str(text or "")).strip()


def _docx_archive(content):
    try:
        return zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise DocumentExtractionError("Could not read DOCX text") from exc


def _attribute(node, name):
    return node.get(f"{WORD_NAMESPACE}{name}") if node is not None else None


def _twips_to_inches(value):
    try:
        return round(int(value) / 1440, 3)
    except (TypeError, ValueError):
        return None


def _docx_default_size(archive):
    """The size in the document's default run properties, in points."""
    try:
        styles = ElementTree.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ElementTree.ParseError):
        return None
    size = styles.find(f"{WORD_NAMESPACE}docDefaults/{WORD_NAMESPACE}rPrDefault/{WORD_NAMESPACE}rPr/{WORD_NAMESPACE}sz")
    half_points = _attribute(size, "val")
    try:
        return round(int(half_points) / 2, 1)
    except (TypeError, ValueError):
        return None


def _docx_run_fonts(root, default_size, default_family):
    """Count the text set in each typeface and size, so a stray run cannot outvote the body."""
    counts = {}
    for run in root.iter(f"{WORD_NAMESPACE}r"):
        text = "".join(node.text or "" for node in run.iter(f"{WORD_NAMESPACE}t"))
        if not text.strip():
            continue
        properties = run.find(f"{WORD_NAMESPACE}rPr")
        size = default_size
        family = default_family
        if properties is not None:
            half_points = _attribute(properties.find(f"{WORD_NAMESPACE}sz"), "val")
            if half_points:
                try:
                    size = round(int(half_points) / 2, 1)
                except (TypeError, ValueError):
                    pass
            declared = _attribute(properties.find(f"{WORD_NAMESPACE}rFonts"), "ascii")
            if declared:
                family = declared
        if size is None:
            continue
        counts[(family or "unknown", size)] = counts.get((family or "unknown", size), 0) + 1
    return [
        {"family": family, "sizePt": size, "runs": runs}
        for (family, size), runs in sorted(counts.items(), key=lambda item: -item[1])
    ]


def _docx_spacing(root):
    """The body's line spacing, read from the paragraph that most text sits in."""
    counts = {}
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        if not "".join(node.text or "" for node in paragraph.iter(f"{WORD_NAMESPACE}t")).strip():
            continue
        spacing = paragraph.find(f"{WORD_NAMESPACE}pPr/{WORD_NAMESPACE}spacing")
        line = _attribute(spacing, "line")
        rule = _attribute(spacing, "lineRule") or "auto"
        if not line or rule not in {"auto", None}:
            counts["single"] = counts.get("single", 0) + 1
            continue
        try:
            multiple = int(line) / 240
        except (TypeError, ValueError):
            continue
        if multiple >= 1.9:
            name = "double"
        elif multiple >= 1.4:
            name = "one_and_a_half"
        else:
            name = "single"
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def _docx_margins(root):
    page_margin = root.find(f".//{WORD_NAMESPACE}sectPr/{WORD_NAMESPACE}pgMar")
    if page_margin is None:
        return {}
    margins = {
        edge: _twips_to_inches(_attribute(page_margin, edge))
        for edge in ("top", "bottom", "left", "right")
    }
    return {edge: value for edge, value in margins.items() if value is not None}


def _docx_document(content):
    with _docx_archive(content) as archive:
        try:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        except (KeyError, ElementTree.ParseError) as exc:
            raise DocumentExtractionError("Could not read DOCX text") from exc
        default_size = _docx_default_size(archive)

    paragraphs = []
    for node in root.iter(f"{WORD_NAMESPACE}p"):
        text = _collapse("".join(run.text or "" for run in node.iter(f"{WORD_NAMESPACE}t")))
        if not text:
            continue
        style_node = node.find(f"{WORD_NAMESPACE}pPr/{WORD_NAMESPACE}pStyle")
        style = style_node.get(f"{WORD_NAMESPACE}val", "") if style_node is not None else ""
        paragraphs.append({"text": text, "style": style, "page": None})

    fonts = _docx_run_fonts(root, default_size, None)
    spacing = _docx_spacing(root)
    margins = _docx_margins(root)
    measured = [name for name, present in (("fonts", fonts), ("lineSpacing", spacing), ("margins", margins)) if present]
    formatting = {
        "pageCount": 0,
        # A DOCX is repaginated by whatever opens it, so it has no page count to
        # check a limit against. Saying so is the finding.
        "countedPageCount": None,
        "fonts": fonts,
        "bodyFontSizePt": fonts[0]["sizePt"] if fonts else default_size,
        "lineSpacing": spacing,
        "marginsIn": margins,
        "measured": measured,
        "unavailable": [
            name for name in ("fonts", "lineSpacing", "margins", "pageCount") if name not in measured
        ],
    }
    return paragraphs, formatting


# Sizes outside this range are a scaling artifact of the text matrix rather than
# a typeface size anyone set, and reporting one would be worse than reporting
# that type size could not be read.
PLAUSIBLE_POINT_RANGE = (4.0, 48.0)


def _pdf_pages(content):
    """Read a PDF page by page, collecting type sizes where they can be trusted."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentExtractionError(
            "PDF ingestion requires installing pypdf or choosing another extractor"
        ) from exc
    reader = PdfReader(io.BytesIO(content))
    pages = []
    font_runs = {}

    def visit(text, _cm, _tm, font_dict, font_size):
        if not str(text or "").strip():
            return
        try:
            size = round(float(font_size), 1)
        except (TypeError, ValueError):
            return
        if not PLAUSIBLE_POINT_RANGE[0] <= size <= PLAUSIBLE_POINT_RANGE[1]:
            return
        base_font = ""
        if isinstance(font_dict, dict):
            base_font = str(font_dict.get("/BaseFont") or "")
        family = re.sub(r"^/?[A-Z]{6}\+", "", base_font).lstrip("/") or "unknown"
        font_runs[(family, size)] = font_runs.get((family, size), 0) + 1

    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text(visitor_text=visit) or ""
        except Exception:  # noqa: BLE001 - one unreadable page must not lose the rest
            text = ""
        pages.append({"page": page_number, "text": text})

    fonts = [
        {"family": family, "sizePt": size, "runs": runs}
        for (family, size), runs in sorted(font_runs.items(), key=lambda item: -item[1])
    ]
    measured = ["pageCount"]
    unavailable = ["lineSpacing", "margins"]
    if fonts:
        measured.append("fonts")
    else:
        unavailable.append("fonts")
    formatting = {
        "pageCount": len(reader.pages),
        # A PDF is already paginated, so its page count is the one a clerk counts.
        "countedPageCount": len(reader.pages),
        "fonts": fonts,
        "bodyFontSizePt": fonts[0]["sizePt"] if fonts else None,
        "lineSpacing": None,
        "marginsIn": {},
        "measured": measured,
        "unavailable": unavailable,
    }
    return pages, formatting


def _paragraphs_from_pages(pages):
    paragraphs = []
    for page in pages:
        for block in re.split(r"\n\s*\n", page["text"] or ""):
            text = _collapse(block.replace("\n", " "))
            if text:
                paragraphs.append({"text": text, "style": "", "page": page["page"]})
    return paragraphs


def _plain_paragraphs(text):
    return [
        {"text": _collapse(block.replace("\n", " ")), "style": "", "page": None}
        for block in re.split(r"\n\s*\n", text or "")
        if _collapse(block)
    ]


EMPTY_FORMATTING = {
    "pageCount": 0,
    "countedPageCount": None,
    "fonts": [],
    "bodyFontSizePt": None,
    "lineSpacing": None,
    "marginsIn": {},
    "measured": [],
    "unavailable": ["fonts", "lineSpacing", "margins", "pageCount"],
}


def extract_document(content, *, filename="", content_type=""):
    """Extract text while keeping the page locations and format facts a check needs."""
    suffix = Path(filename or "").suffix.casefold()
    kind = (content_type or "").casefold()
    pages = []
    if suffix == ".docx" or "wordprocessingml" in kind:
        paragraphs, formatting = _docx_document(content)
        extractor = "docx-structure"
    elif suffix == ".pdf" or "pdf" in kind:
        pages, formatting = _pdf_pages(content)
        paragraphs = _paragraphs_from_pages(pages)
        extractor = "pdf-pages"
    else:
        extracted = extract_text(content, filename=filename, content_type=content_type)
        paragraphs = _plain_paragraphs(extracted["text"])
        formatting = dict(EMPTY_FORMATTING)
        extractor = extracted["extractor"]
    text = "\n\n".join(paragraph["text"] for paragraph in paragraphs)
    return {
        "text": text,
        "extractor": extractor,
        "paragraphs": paragraphs,
        "pages": pages,
        "pageCount": formatting.get("pageCount", 0),
        "formatting": formatting,
    }


def _is_heading(paragraph):
    text = paragraph["text"]
    if paragraph.get("style", "").casefold().startswith("heading"):
        return True
    if len(text) > 120:
        return False
    if text.endswith((".", ";", ",")) and not _NUMBERING.match(text):
        return False
    stripped = _NUMBERING.match(text)
    body = stripped.group("rest") if stripped else text
    if len(body) > 90:
        return False
    letters = [character for character in body if character.isalpha()]
    if letters and all(character.isupper() for character in letters):
        return True
    return bool(stripped) and len(body.split()) <= 14


def _heading_level(paragraph):
    style = paragraph.get("style", "").casefold()
    match = re.search(r"heading\s*(\d)", style)
    if match:
        return min(int(match.group(1)), 4)
    numbering = _NUMBERING.match(paragraph["text"])
    if numbering:
        label = numbering.group("label")
        # Roman parts, then lettered sub-parts, then numbered points: the
        # ordering briefs conventionally use when the style name says nothing.
        if _ROMAN.match(label):
            return 1
        if label.isalpha():
            return 2
        return 3
    return 1


def _section_label(stack):
    return ".".join(part for part in stack if part)


def _citations(text):
    found = []
    seen = set()
    for pattern in _CITATION_PATTERNS:
        for match in pattern.finditer(text):
            value = _collapse(match.group(0)).strip(" ,;")
            key = value.casefold()
            if len(value) < 4 or key in seen:
                continue
            seen.add(key)
            found.append(value)
    return found


def _sentences(text):
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]


def structure_units(paragraphs):
    """Turn located paragraphs into addressable units with run-local ids."""
    units = []
    section_stack = []
    paragraph_number = 0
    in_facts = False
    in_argument = False

    def add(unit_type, text, *, page, parent_id="", extra=None):
        unit = {
            "id": f"u{len(units) + 1}",
            "type": unit_type,
            "text": text,
            "locator": {
                "section": _section_label(section_stack),
                "paragraph": paragraph_number,
                "page": page,
                "excerpt": text[:300],
            },
        }
        if parent_id:
            unit["parentId"] = parent_id
        unit.update(extra or {})
        units.append(unit)
        return unit

    for paragraph in paragraphs:
        text = paragraph["text"]
        page = paragraph.get("page")
        if _is_heading(paragraph):
            level = _heading_level(paragraph)
            numbering = _NUMBERING.match(text)
            label = numbering.group("label") if numbering else text[:24]
            title = numbering.group("rest") if numbering else text
            while len(section_stack) >= level:
                section_stack.pop()
            while len(section_stack) < level - 1:
                section_stack.append("")
            section_stack.append(label)
            # A lettered sub-part of ARGUMENT is still argument. Only a new
            # top-level part changes what kind of text we are reading.
            if level <= 1:
                in_facts = bool(_FACT_HEADING.search(title))
                in_argument = bool(_ARGUMENT_HEADING.search(title))
            else:
                in_facts = in_facts or bool(_FACT_HEADING.search(title))
                in_argument = in_argument or bool(_ARGUMENT_HEADING.search(title))
            add(SECTION, title, page=page, extra={"heading": text, "level": level})
            continue

        paragraph_number += 1
        citations = _citations(text)
        if _RELIEF.search(text):
            unit_type = REQUESTED_RELIEF
        elif in_facts and not citations:
            unit_type = ASSERTED_FACT
        elif citations or in_argument:
            unit_type = ARGUMENT
        else:
            unit_type = PARAGRAPH
        parent = add(unit_type, text, page=page)
        if unit_type == ASSERTED_FACT:
            continue
        for citation in citations:
            add(CITATION, citation, page=page, parent_id=parent["id"])
        if unit_type == ARGUMENT:
            # The first sentence of an argument paragraph is normally the
            # proposition the rest of it supports, and it is what an opponent
            # attacks. Recording it separately gives a challenge a tighter anchor.
            sentences = _sentences(text)
            if len(sentences) > 1:
                add(ARGUMENT, sentences[0], page=page, parent_id=parent["id"], extra={"role": "proposition"})
    return units


def units_from_sections(sections):
    """Structure a CLARA-native document from its component sections.

    A native draft is already anchored, so each unit carries the block key the
    revision machinery uses rather than a synthesized locator.
    """
    units = []
    for position, section in enumerate(sections or [], start=1):
        key = str(section.get("key") or f"section-{position}")
        label = str(section.get("label") or key)
        body = str(section.get("body") or "")
        units.append(
            {
                "id": f"u{len(units) + 1}",
                "type": SECTION,
                "text": label,
                "blockKey": key,
                "locator": {"section": label, "paragraph": 0, "page": None, "excerpt": label},
            }
        )
        for paragraph_number, block in enumerate(re.split(r"\n\s*\n", body), start=1):
            text = _collapse(block.replace("\n", " "))
            if not text:
                continue
            citations = _citations(text)
            if _RELIEF.search(text):
                unit_type = REQUESTED_RELIEF
            elif citations:
                unit_type = ARGUMENT
            else:
                unit_type = PARAGRAPH
            parent = {
                "id": f"u{len(units) + 1}",
                "type": unit_type,
                "text": text,
                "blockKey": key,
                "locator": {
                    "section": label,
                    "paragraph": paragraph_number,
                    "page": None,
                    "excerpt": text[:300],
                },
            }
            units.append(parent)
            for citation in citations:
                units.append(
                    {
                        "id": f"u{len(units) + 1}",
                        "type": CITATION,
                        "text": citation,
                        "blockKey": key,
                        "parentId": parent["id"],
                        "locator": {
                            "section": label,
                            "paragraph": paragraph_number,
                            "page": None,
                            "excerpt": citation,
                        },
                    }
                )
    return units


def text_checksum(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# Exhibit splitting


def _is_exhibit_cover(page_text):
    """A cover sheet is a nearly blank page whose only line names an exhibit."""
    lines = [line.strip() for line in (page_text or "").splitlines() if line.strip()]
    if not lines or len(page_text or "") > 400:
        return None
    for line in lines[:3]:
        match = _EXHIBIT_MARKER.match(line)
        if match:
            return f"{match.group('kind').title()} {match.group('label').upper()}"
    return None


def _first_exhibit_heading(page_text):
    """A page that opens by naming an exhibit, even without a blank cover sheet."""
    head = "\n".join([line for line in (page_text or "").splitlines() if line.strip()][:2])
    match = _EXHIBIT_ANYWHERE.search(head)
    if match and not _EXHIBIT_INDEX.search(head):
        return f"{match.group(1).title()} {match.group(2).upper()}"
    return None


def split_brief_and_exhibits(pages, *, page_limit=BRIEF_PAGE_LIMIT):
    """Find where the brief stops and its attachments start.

    A filing with exhibits is mostly not a brief, and pushing three hundred
    pages of a lease and a rent ledger through a model to find the argument is
    both expensive and worse at finding it. The boundary is usually printed on
    the page: a certificate of service ends the brief, and an exhibit cover
    sheet starts the attachments. Where nothing says, the page cap applies, and
    the reason is recorded either way so the split is reviewable.
    """
    if not pages:
        return {"briefPageCount": 0, "boundaryReason": "This file has no pages.", "exhibits": []}

    boundary = len(pages)
    reason = "The whole file was read as the brief; nothing marked an exhibit."

    exhibit_start = None
    for page in pages:
        label = _is_exhibit_cover(page["text"]) or _first_exhibit_heading(page["text"])
        if label and page["page"] > 1:
            exhibit_start = page["page"]
            break

    certificate_page = next(
        (page["page"] for page in pages if _END_OF_BRIEF.search(page["text"] or "")),
        None,
    )

    if certificate_page and (exhibit_start is None or certificate_page < exhibit_start):
        boundary = certificate_page
        reason = f"The certificate of service on page {certificate_page} ends the brief."
    elif exhibit_start:
        boundary = exhibit_start - 1
        reason = f"An exhibit begins on page {exhibit_start}."

    if boundary > page_limit:
        boundary = page_limit
        reason = (
            f"No exhibit boundary was found, so the first {page_limit} pages were read as the brief "
            "and the rest as attachments."
        )

    exhibits = []
    current = None
    for page in pages:
        if page["page"] <= boundary:
            continue
        label = _is_exhibit_cover(page["text"]) or _first_exhibit_heading(page["text"])
        if label or current is None:
            current = {
                "label": label or "Attachment",
                "startPage": page["page"],
                "endPage": page["page"],
                "text": page["text"] or "",
            }
            exhibits.append(current)
        else:
            current["endPage"] = page["page"]
            current["text"] = f"{current['text']}\n\n{page['text'] or ''}"
    return {"briefPageCount": boundary, "boundaryReason": reason, "exhibits": exhibits}


def _bounded(text):
    """Cap the text a run will read, and say when the cap bit."""
    if len(text) <= MAX_BRIEF_CHARS:
        return text, False
    return text[:MAX_BRIEF_CHARS], True


def ingest_upload(content, *, filename="", content_type="", split_exhibits=True):
    """Extract and structure an uploaded document, separating attachments from the brief."""
    extracted = extract_document(content, filename=filename, content_type=content_type)
    pages = extracted["pages"]
    split = None
    paragraphs = extracted["paragraphs"]
    exhibits = []

    if split_exhibits and pages:
        split = split_brief_and_exhibits(pages)
        if split["exhibits"]:
            brief_pages = [page for page in pages if page["page"] <= split["briefPageCount"]]
            paragraphs = _paragraphs_from_pages(brief_pages)
            exhibits = [
                {
                    "title": f"{exhibit['label']} (pages {exhibit['startPage']}-{exhibit['endPage']})",
                    "text": _collapse(exhibit["text"].replace("\n", " ")),
                    "pageRange": {"start": exhibit["startPage"], "end": exhibit["endPage"]},
                }
                for exhibit in split["exhibits"]
            ]

    text = "\n\n".join(paragraph["text"] for paragraph in paragraphs)
    text, truncated = _bounded(text)
    units = structure_units(paragraphs)
    formatting = dict(extracted["formatting"])
    if split and split["exhibits"]:
        # The page limit applies to the brief, not to the exhibits stapled to it.
        formatting["countedPageCount"] = split["briefPageCount"]
    return {
        "text": text,
        "metadata": {
            "extractor": extracted["extractor"],
            "pageCount": extracted["pageCount"],
            "paragraphCount": len(paragraphs),
            "units": units,
            "checksum": text_checksum(text),
            "formatting": formatting,
            "split": split,
            "truncated": truncated,
        },
        "exhibits": exhibits,
    }

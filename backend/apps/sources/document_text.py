import html
import io
import re
import zipfile
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from django.conf import settings


MAX_INPUT_BYTES = 15 * 1024 * 1024
MAX_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_MEMBERS = 2048
MAX_PAGES = 300
MAX_TEXT_CHARS = 4 * 1024 * 1024


def validate_input(content, filename="", content_type=""):
    if len(content) > MAX_INPUT_BYTES:
        raise DocumentExtractionError("Document exceeds 15 MB input limit")
    suffix = Path(filename).suffix.casefold()
    if suffix == ".docx" or "wordprocessingml" in content_type:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                    raise DocumentExtractionError("DOCX exceeds archive limits")
                if "word/document.xml" not in archive.namelist():
                    raise DocumentExtractionError("Invalid DOCX document")
        except zipfile.BadZipFile as exc:
            raise DocumentExtractionError("Invalid DOCX archive") from exc
    if suffix == ".pdf" or "pdf" in content_type:
        if not content.startswith(b"%PDF-"):
            raise DocumentExtractionError("Invalid PDF signature")
        from pypdf import PdfReader
        if len(PdfReader(io.BytesIO(content)).pages) > MAX_PAGES:
            raise DocumentExtractionError("PDF exceeds 300 page limit")


class DocumentExtractionError(RuntimeError):
    pass


class _HTMLTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())

    def text(self):
        return "\n".join(self.parts)


def _collapse(text):
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


class StdlibTextExtractor:
    name = "stdlib"

    def extract(self, content, *, filename="", content_type=""):
        validate_input(content, filename, content_type)
        suffix = Path(filename or "").suffix.casefold()
        kind = (content_type or "").casefold()
        if kind.startswith("text/") or suffix in {".txt", ".md", ".csv", ".json"}:
            return _collapse(content.decode("utf-8", errors="replace"))
        if "html" in kind or suffix in {".html", ".htm"}:
            parser = _HTMLTextParser()
            parser.feed(content.decode("utf-8", errors="replace"))
            return _collapse(html.unescape(parser.text()))
        if suffix == ".docx" or "wordprocessingml" in kind:
            return self._extract_docx(content)
        if suffix == ".pdf" or "pdf" in kind:
            return self._extract_pdf(content)
        raise DocumentExtractionError(f"No stdlib extractor for {filename or content_type or 'document'}")

    def _extract_docx(self, content):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                xml = archive.read("word/document.xml")
        except (KeyError, zipfile.BadZipFile) as exc:
            raise DocumentExtractionError("Could not read DOCX text") from exc
        root = ElementTree.fromstring(xml)
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(f"{namespace}p"):
            texts = [node.text or "" for node in paragraph.iter(f"{namespace}t")]
            if "".join(texts).strip():
                paragraphs.append("".join(texts))
        return _collapse("\n".join(paragraphs))

    def _extract_pdf(self, content):
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DocumentExtractionError("PDF extraction requires installing pypdf or choosing another extractor") from exc
        reader = PdfReader(io.BytesIO(content))
        return _collapse("\n".join(page.extract_text() or "" for page in reader.pages))


class MarkItDownTextExtractor:
    name = "markitdown"

    def extract(self, content, *, filename="", content_type=""):
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise DocumentExtractionError("MarkItDown is not installed") from exc
        result = MarkItDown().convert_stream(io.BytesIO(content), file_extension=Path(filename).suffix or None)
        return _collapse(result.text_content)


class DoclingTextExtractor:
    name = "docling"

    def extract(self, content, *, filename="", content_type=""):
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise DocumentExtractionError("Docling is not installed") from exc
        with tempfile.TemporaryDirectory(prefix="clara-extract-") as directory:
            path = Path(directory) / Path(filename or "document").name
            path.write_bytes(content)
            result = DocumentConverter().convert(str(path))
            return _collapse(result.document.export_to_markdown())


def get_extractor(name=None):
    extractor = (name or settings.DOCUMENT_TEXT_EXTRACTOR or "stdlib").casefold()
    if extractor == "stdlib":
        return StdlibTextExtractor()
    if extractor == "markitdown":
        return MarkItDownTextExtractor()
    if extractor == "docling":
        return DoclingTextExtractor()
    raise DocumentExtractionError(f"Unknown document text extractor: {extractor}")


def _extract_in_worker(content, *, filename="", content_type="", extractor=None):
    validate_input(content, filename, content_type)
    backend = get_extractor(extractor)
    text = backend.extract(content, filename=filename, content_type=content_type)
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentExtractionError("Extracted text exceeds 4 MB limit")
    return {"extractor": backend.name, "text": text}


def extract_text(content, *, filename="", content_type="", extractor=None):
    if len(content) > MAX_INPUT_BYTES:
        raise DocumentExtractionError("Document exceeds 15 MB input limit")
    from apps.core.constrained_worker import run_extraction
    try:
        return run_extraction(content, filename=filename, content_type=content_type,
                              extractor=extractor or settings.DOCUMENT_TEXT_EXTRACTOR or "stdlib")
    except (ValueError, OSError) as exc:
        raise DocumentExtractionError(str(exc)) from exc

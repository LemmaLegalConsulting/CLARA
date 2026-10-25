"""Prepared XLSX templates, for exhibits that are spreadsheets.

Rent ledgers are filed as exhibits and are maintained as workbooks, not Word
documents, so the DOCX converter cannot take them. They are much simpler than a
filing: the placeholder text lives in the shared-string table and the rest is
column headings and formulas that must be preserved untouched.

Only the shared strings are rewritten. Cell formulas, styles, column widths, and
the sheet layout stay exactly as maintained, which is what keeps a rendered
ledger's arithmetic intact.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml
from django.utils.text import slugify

from apps.templates_app.placeholders import (
    PLACEHOLDER_ALIASES,
    convert_text,
    looks_like_placeholder,
    placeholder_expression,
)


SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SHARED_STRINGS_PATH = "xl/sharedStrings.xml"
MANIFEST_VERSION = 2

# "RENT LEDGER - CLIENT NAME | ADDRESS": the maintained files mark fill-ins by
# capitalising them rather than by bracketing them.
CAPS_TOKEN_RE = re.compile(r"\b[A-Z][A-Z'’]*(?:\s+[A-Z][A-Z'’]*)*\b")
# Headings that are simply shouted, not placeholders.
CAPS_KEEP = {
    "rent ledger",
    "total requested",
    "total",
    "ledger",
    "note",
    "notes",
    "date",
    "charge",
    "balance",
    "n/a",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def convert_caps_placeholders(text: str, fallback_prefix: str):
    """Bind shouted fill-ins such as CLIENT NAME while keeping shouted headings."""
    fields = set()

    def replace(match):
        token = match.group(0)
        normalized = " ".join(token.split()).casefold()
        if normalized in CAPS_KEEP or len(normalized) < 4:
            return token
        if normalized not in PLACEHOLDER_ALIASES and not looks_like_placeholder(token):
            return token
        expression = placeholder_expression(token, fallback_prefix)
        inner = expression.strip("{} ").strip()
        if inner.startswith("fields."):
            fields.add(inner)
        return expression

    converted = CAPS_TOKEN_RE.sub(replace, text)
    return converted, fields


def _convert_shared_string(text: str, fallback_prefix: str):
    converted, conversion = convert_text(text, fallback_prefix)
    converted, caps_fields = convert_caps_placeholders(converted, fallback_prefix)
    return converted, set(conversion.fields) | caps_fields


def prepare_workbook(source: Path, output: Path) -> list[str]:
    """Copy a workbook, rewriting only its shared-string placeholders."""
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)

    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if SHARED_STRINGS_PATH not in names:
            return []
        payload = archive.read(SHARED_STRINGS_PATH)
        members = {name: archive.read(name) for name in names}

    ET.register_namespace("", SHEET_NS)
    root = ET.fromstring(payload)
    fields = set()
    for index, item in enumerate(root.findall(f"{{{SHEET_NS}}}si"), start=1):
        for node in item.iter(f"{{{SHEET_NS}}}t"):
            if not (node.text or "").strip():
                continue
            converted, found = _convert_shared_string(node.text, f"cell_{index}")
            fields.update(found)
            node.text = converted
    members[SHARED_STRINGS_PATH] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return sorted(fields)


def render_workbook(template_path: Path, context: dict, output_path: Path, *, jinja_env=None):
    """Fill a prepared workbook's shared strings, leaving formulas alone."""
    from .jinja_filters import template_environment

    environment = jinja_env or template_environment()
    with zipfile.ZipFile(template_path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}

    payload = members.get(SHARED_STRINGS_PATH)
    if payload:
        ET.register_namespace("", SHEET_NS)
        root = ET.fromstring(payload)
        for node in root.iter(f"{{{SHEET_NS}}}t"):
            text = node.text or ""
            if "{{" not in text and "{%" not in text:
                continue
            node.text = environment.from_string(text).render(**context)
        members[SHARED_STRINGS_PATH] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output_path


def ingest_xlsx(source: Path, prepared_root: Path, *, force=False) -> Path:
    """Register a maintained workbook as a prepared, renderable template."""
    source = source.resolve()
    slug = slugify(source.stem) or "worksheet-template"
    package_dir = prepared_root / slug
    package_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = package_dir / "manifest.yaml"
    source_checksum = sha256_file(source)
    if manifest_path.exists() and not force:
        existing = yaml.safe_load(manifest_path.read_text()) or {}
        if existing.get("source", {}).get("sha256") == source_checksum:
            return manifest_path

    workbook_path = package_dir / "template.xlsx"
    fields = prepare_workbook(source, workbook_path)

    manifest = {
        "schema_version": MANIFEST_VERSION,
        "slug": slug,
        "title": source.stem,
        "kind": "worksheet",
        "description": f"Maintained spreadsheet exhibit for {source.stem}.",
        "goal": f"Produce the {source.stem} exhibit from the case's payment history.",
        "negative_goal": "",
        "aliases": [],
        "jurisdiction": "Ohio",
        "source_label": "Content library",
        "active": True,
        "render": {"strategy": "workbook", "xlsx": "template.xlsx"},
        "source": {
            "path": source.as_posix(),
            "sha256": source_checksum,
            "converted_at": datetime.now(timezone.utc).isoformat(),
            "converter": "apps.templates_app.spreadsheets",
            "format_preservation": "in_place_xlsx",
        },
        "fields": fields,
        "flags": [],
        # A workbook has no reviewable prose blocks; the advocate fills the rows.
        "blocks": [],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
    return manifest_path

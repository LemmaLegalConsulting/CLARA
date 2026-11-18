#!/usr/bin/env python3
"""Fetch, provenance-stamp, and chunk selected Ohio Administrative Code chapters.

Reads ``content/statutes/ohio-administrative-code/scope.yaml`` and writes the
same manifest-and-chunks shape as the Revised Code corpus, so retrieval and the
research engine read it without special casing. One official chapter page
carries every rule in the chapter, so a refresh is one request per chapter.

Examples:
  python scripts/ingest_ohio_administrative_code.py --all
  python scripts/ingest_ohio_administrative_code.py --chapter 4112-5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
SLUG = "ohio-administrative-code"
LIBRARY = ROOT / "content" / "statutes" / SLUG
SCOPE_PATH = LIBRARY / "scope.yaml"
MANIFEST_PATH = LIBRARY / "manifest.yaml"
SECTIONS_DIR = LIBRARY / "sections"
CHUNKS_DIR = LIBRARY / "chunks"
MAX_WORDS = 1100
USER_AGENT = "clara-legal-research statute indexer/1.0"
RULE_HEADING = re.compile(r"^Rule\s+([0-9:.-]+)\s*\|\s*(.+)$")
TOP_LEVEL_LABEL = re.compile(r"^\(([A-Z]{1,2})\)")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def front(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip() + "\n---\n"


class ChapterParser(HTMLParser):
    """Each rule on an official chapter page: heading, effective date, PDF, and paragraphs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rules: list[dict] = []
        self._in_heading = 0
        self._heading: list[str] = []
        self._label = None
        self._value_for = None
        self._capture: list[str] | None = None
        self._in_body = 0
        self._paragraph: list[str] | None = None

    @property
    def current(self):
        return self.rules[-1] if self.rules else None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "span" and self._in_heading:
            self._in_heading += 1  # the "|" separator is a span inside the heading
        elif tag == "span" and "content-head-text" in classes:
            self._in_heading, self._heading = 1, []
        elif tag == "div" and "label" in classes:
            self._capture, self._label = [], "label"
        elif tag == "div" and "value" in classes and self._value_for:
            self._capture, self._label = [], "value"
        elif tag == "a" and self.current is not None and "authenticated" in (values.get("href") or ""):
            self.current["pdf"] = values["href"]
        elif tag == "section" and "laws-body" in classes:
            self._in_body = 1
        elif tag == "section" and self._in_body:
            self._in_body += 1
        elif tag == "p" and self._in_body:
            self._paragraph = []

    def handle_endtag(self, tag):
        if tag == "span" and self._in_heading > 1:
            self._in_heading -= 1
        elif tag == "span" and self._in_heading:
            self._in_heading = 0
            match = RULE_HEADING.match(tidy("".join(self._heading)))
            if match:
                self.rules.append({"rule": match.group(1), "title": match.group(2).strip(), "paragraphs": [],
                                   "effective": "", "pdf": ""})
        elif tag == "div" and self._capture is not None:
            text = tidy("".join(self._capture))
            if self._label == "label":
                self._value_for = text.rstrip(":").casefold()
            elif self._label == "value" and self.current is not None:
                if self._value_for == "effective":
                    self.current["effective"] = text
                self._value_for = None
            self._capture, self._label = None, None
        elif tag == "p" and self._paragraph is not None:
            text = tidy("".join(self._paragraph))
            if text and self.current is not None:
                self.current["paragraphs"].append(text)
            self._paragraph = None
        elif tag == "section" and self._in_body:
            self._in_body -= 1

    def handle_data(self, data):
        if self._in_heading:
            self._heading.append(data)
        elif self._capture is not None:
            self._capture.append(data)
        elif self._paragraph is not None:
            self._paragraph.append(data)


def fetch(session: requests.Session, url: str) -> str:
    response = None
    for attempt in range(4):
        response = session.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
        if response.status_code not in {429, 502, 503, 504}:
            break
        time.sleep(min(2**attempt, 30))
    assert response is not None
    response.raise_for_status()
    return response.text


def next_label(label: str) -> str:
    if not label:
        return "A"
    if label == "Z":
        return "AA"
    if len(label) == 2:
        return chr(ord(label[0]) + 1) * 2
    return chr(ord(label) + 1)


def chunk(paragraphs):
    chunks, current, words = [], [], 0
    for label, paragraph in paragraphs:
        count = len(paragraph.split())
        if current and words + count > MAX_WORDS:
            chunks.append(current)
            current, words = [], 0
        current.append((label, paragraph))
        words += count
    if current:
        chunks.append(current)
    return chunks


def write_rule(record: dict, target: dict, chapter_url: str, html: str, *, dry_run: bool) -> dict:
    number = record["rule"]
    ident = "oac-" + re.sub(r"[^0-9a-z]+", "-", number.casefold()).strip("-")
    labelled, label = [], ""
    for paragraph in record["paragraphs"]:
        match = TOP_LEVEL_LABEL.match(paragraph)
        if match and match.group(1) == next_label(label):
            label = match.group(1)
        labelled.append((label, paragraph))
    text = "\n\n".join(paragraph for _label, paragraph in labelled)
    citation = f"Ohio Adm.Code {number}"
    heading = f"{citation} — {record['title']}"
    meta = {
        "rule": number,
        "title": record["title"],
        "citation": citation,
        "url": f"{chapter_url.rsplit('/', 1)[0]}/rule-{number}",
        "acquired_from_url": chapter_url,
        "authenticated_pdf_url": urljoin(chapter_url, record["pdf"]) if record["pdf"] else "",
        "effective_date": record["effective"],
        "raw_html_sha256": sha256(html),
        "normalized_text_sha256": sha256(text),
        "fetched_at": now(),
        "source_name": "Ohio Administrative Code",
        "publisher": "Ohio Legislative Service Commission",
        "jurisdiction": "Ohio",
        "content_kind": "statute",
        "scope_label": target.get("label", ""),
        "record_path": f"sections/{ident}.md",
    }
    pieces = chunk(labelled)
    inventory = []
    for part, piece in enumerate(pieces, 1):
        chunk_id = f"{ident}-{part:02d}"
        labels = [item for item, _text in piece if item]
        span = "" if len(pieces) == 1 or not labels else (
            f"({labels[0]})" if labels[0] == labels[-1] else f"({labels[0]})–({labels[-1]})"
        )
        chunk_heading = f"{heading} {span}".strip()
        inventory.append({
            "id": chunk_id,
            "file": f"chunks/{chunk_id}.md",
            "heading": chunk_heading,
            "path": ["Ohio Administrative Code", f"Chapter {target['chapter']}", citation],
            "content_kind": "regulation-section",
            "section": number,
            "chapter": f"Chapter {target['chapter']}",
            "citation": citation,
            "url": meta["url"],
            "effective_date": record["effective"],
            "retrieval_hints": [str(hint) for hint in target.get("retrieval_hints") or []],
        })
        if not dry_run:
            body = "\n\n".join(paragraph for _label, paragraph in piece)
            (CHUNKS_DIR / f"{chunk_id}.md").write_text(
                front({**meta, "chunk_id": chunk_id, "chunk_part": part, "chunk_parts_in_section": len(pieces)})
                + f"# {chunk_heading}\n\n## Source text\n\n{body}\n",
                encoding="utf-8",
            )
    if not dry_run:
        keep = {item["file"].split("/", 1)[1] for item in inventory}
        for stale in CHUNKS_DIR.glob(f"{ident}-*.md"):
            if stale.name not in keep:
                stale.unlink()
        (SECTIONS_DIR / f"{ident}.md").write_text(front(meta) + f"# {heading}\n\n{text}\n", encoding="utf-8")
    return {**meta, "chunks": inventory}


def rule_sort_key(item):
    return [int(part) if part.isdigit() else part for part in re.split(r"[:.-]", item["rule"])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--ring", choices=["1", "2"])
    selection.add_argument("--chapter")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause", type=float, default=2.0)
    args = parser.parse_args()

    scope = yaml.safe_load(SCOPE_PATH.read_text(encoding="utf-8")) or {}
    base_url = str(scope["source"]["base_url"]).rstrip("/")
    targets = scope.get("targets", [])
    if args.ring:
        targets = [t for t in targets if str(t.get("ring")) == args.ring]
    elif args.chapter:
        targets = [t for t in targets if str(t.get("chapter")) == args.chapter]
        if not targets:
            parser.error(f"Chapter {args.chapter} is not configured in {SCOPE_PATH}")

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.is_file() else {}
    prior = {item["rule"]: item for item in (manifest or {}).get("sections", []) if item.get("rule")}
    if not args.dry_run:
        SECTIONS_DIR.mkdir(parents=True, exist_ok=True)
        CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    changed, unchanged, failures, selected = [], [], [], set()
    for target in targets:
        chapter_url = f"{base_url}/chapter-{target['chapter']}"
        try:
            html = fetch(session, chapter_url)
            parsed = ChapterParser()
            parsed.feed(html)
            rules = [rule for rule in parsed.rules if rule["paragraphs"]]
            if not rules:
                raise ValueError(f"No rules with text found at {chapter_url}")
        except (requests.RequestException, ValueError) as exc:
            failures.append({"chapter": target["chapter"], "error": str(exc)})
            continue
        for rule in rules:
            selected.add(rule["rule"])
            text_hash = sha256("\n\n".join(rule["paragraphs"]))
            if not args.force and prior.get(rule["rule"], {}).get("normalized_text_sha256") == text_hash:
                unchanged.append(rule["rule"])
                continue
            prior[rule["rule"]] = write_rule(rule, target, chapter_url, html, dry_run=args.dry_run)
            changed.append(rule["rule"])
        time.sleep(max(args.pause, 0))

    if not args.dry_run:
        if args.all and not failures:
            for number in [number for number in prior if number not in selected]:
                item = prior.pop(number)
                for path in [item.get("record_path", ""), *[c.get("file", "") for c in item.get("chunks", [])]]:
                    if path and (LIBRARY / path).is_file():
                        (LIBRARY / path).unlink()
        sections = sorted(prior.values(), key=rule_sort_key)
        chunks = [item for section in sections for item in section.get("chunks", [])]
        MANIFEST_PATH.write_text(yaml.safe_dump({
            "schema_version": 1, "document_slug": SLUG, "document_title": "Ohio Administrative Code",
            "jurisdiction": "Ohio", "content_kind": "statute", "source_name": "Ohio Administrative Code",
            "publisher": "Ohio Legislative Service Commission", "source_base_url": base_url,
            "generated_at": now(), "generator": "scripts/ingest_ohio_administrative_code.py",
            "section_count": len(sections), "chunk_count": len(chunks), "sections": sections, "chunks": chunks,
        }, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(json.dumps({"changed": len(changed), "unchanged": len(unchanged), "failures": failures,
                      "dry_run": args.dry_run}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

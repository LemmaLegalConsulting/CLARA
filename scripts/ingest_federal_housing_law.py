#!/usr/bin/env python3
"""Fetch, provenance-stamp, and chunk selected federal housing law.

Reads ``content/statutes/federal-housing-law/scope.yaml`` and writes the same
manifest-and-chunks shape as the Ohio Revised Code corpus, so the RAG
connector and the research engine pick it up without special casing.

Sources are official and public:

* U.S. Code sections from the Office of the Law Revision Counsel
  (uscode.house.gov), which states the public law its text is current through;
  if it cannot be read, from the GovInfo annual edition, which lags enactments.
  Each record says which it came from and how current it is.
* CFR parts (24 C.F.R., and 7 C.F.R. for USDA rural housing) from the eCFR versioner API, as of the latest date the eCFR
  reports itself up to date. Each record keeps that date.

Editorial notes (amendment notes, codification notes, references in text) are
not law and are not ingested. A section's source credit (U.S.C.) or amendment
citation (C.F.R.) is kept, because it is how a reader tells that a rule was
recently changed.

Examples:
  python scripts/ingest_federal_housing_law.py --all
  python scripts/ingest_federal_housing_law.py --ring 1
  python scripts/ingest_federal_housing_law.py --cfr-part 966
  python scripts/ingest_federal_housing_law.py --cfr-part 7:3560
  python scripts/ingest_federal_housing_law.py --usc 15:9058
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
SLUG = "federal-housing-law"
LIBRARY = ROOT / "content" / "statutes" / SLUG
SCOPE_PATH = LIBRARY / "scope.yaml"
MANIFEST_PATH = LIBRARY / "manifest.yaml"
SECTIONS_DIR = LIBRARY / "sections"
CHUNKS_DIR = LIBRARY / "chunks"
MAX_WORDS = 1100
USER_AGENT = "clara-legal-research statute indexer/1.0"
TOP_LEVEL_LABEL = re.compile(r"^\(([a-z]{1,2})\)")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def front(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip() + "\n---\n"


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fetch(session: requests.Session, url: str) -> requests.Response:
    response = None
    for attempt in range(5):
        try:
            response = session.get(url, timeout=120, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        except (requests.Timeout, requests.ConnectionError):
            # A slow publisher is the common failure, not a missing section.
            if attempt == 4:
                raise
            time.sleep(min(5 * 2**attempt, 60))
            continue
        if response.status_code not in {429, 502, 503, 504}:
            break
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(float(retry_after), 60)
        except ValueError:
            delay = 2**attempt
        time.sleep(delay)
    assert response is not None
    response.raise_for_status()
    return response


# U.S. Code, from GovInfo's per-section HTML


class UscSectionParser(HTMLParser):
    """Collect the head, statutory text, and source credit of one GovInfo section page.

    GovInfo marks each region with ``<!-- field-start:NAME -->`` comments. Only
    the ``statute`` field is law; notes are editorial and are skipped.
    """

    BLOCKS = {"p", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.field = ""
        self.head: list[str] = []
        self.paragraphs: list[tuple[str, str]] = []  # (css class, text)
        self.source_credit: list[str] = []
        self.current_through = ""
        self.edition = ""
        self.chapter = ""
        self._block_class = None
        self._buffer: list[str] = []

    def handle_comment(self, data):
        data = data.strip()
        if data.startswith("field-start:"):
            self.field = data.split(":", 1)[1]
        elif data.startswith("field-end:"):
            self.field = ""
        elif "currentthrough:" in data:
            match = re.search(r"currentthrough:(\d{8})", data)
            if match:
                value = match.group(1)
                self.current_through = f"{value[:4]}-{value[4:6]}-{value[6:]}"

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self._block_class = dict(attrs).get("class", "") or tag
            self._buffer = []

    def handle_endtag(self, tag):
        if tag in self.BLOCKS and self._block_class is not None:
            text = tidy("".join(self._buffer))
            if text:
                if self.field == "head":
                    self.head.append(text)
                elif self.field == "statute":
                    self.paragraphs.append((self._block_class, text))
                elif self.field == "sourcecredit":
                    self.source_credit.append(text)
            self._block_class = None

    def handle_data(self, data):
        if self._block_class is not None:
            self._buffer.append(data)
        else:
            if not self.edition:
                match = re.search(r"United States Code, (\d{4}) Edition", data)
                if match:
                    self.edition = match.group(1)
            if not self.chapter:
                match = re.match(r"\s*CHAPTER\s+([0-9A-Z]+)\s*-\s*(.+)", data)
                if match:
                    self.chapter = f"Chapter {match.group(1)} — {match.group(2).strip().title()}"


def next_label(label: str) -> str:
    """The top-level paragraph label that follows ``label``: a, b, ... z, aa, bb.

    Regulations number top-level paragraphs with letters and clauses with
    roman numerals, so "(i)" is either the ninth paragraph or a first clause.
    Only the label that comes next in sequence can open a new top-level
    paragraph; anything else is a lower level.
    """
    if not label:
        return "a"
    if label == "z":
        return "aa"
    if len(label) == 2:
        return chr(ord(label[0]) + 1) * 2
    return chr(ord(label) + 1)


class OlrcSectionParser(HTMLParser):
    """One section page from uscode.house.gov: head, statutory paragraphs, source credit.

    Statutory text sits in ``statutory-body*`` paragraphs inside the
    ``statutory-body-container``; notes use ``note-*`` classes and are skipped.
    A top-level subsection's paragraph id is a bare letter (``id="a"``).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.head = ""
        self.chapter = ""
        self.current_through = ""
        self.paragraphs: list[tuple[str, str]] = []  # (top-level label, text)
        self.source_credit = ""
        self._capture = None
        self._buffer: list[str] = []
        self._id = ""
        self._label = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        css = values.get("class") or ""
        if tag == "h3" and "section-head" in css:
            self._capture, self._buffer = "head", []
        elif tag == "p" and css.startswith("statutory-body"):
            self._capture, self._buffer, self._id = "body", [], values.get("id") or ""
        elif tag == "p" and "source-credit" in css:
            self._capture, self._buffer = "credit", []
        elif tag == "span" and css in {"parent1ExpCite", "lawsInEffect"}:
            self._capture, self._buffer = css, []

    def handle_endtag(self, tag):
        if self._capture is None or tag not in {"h3", "p", "span"}:
            return
        if tag == "span" and self._capture not in {"parent1ExpCite", "lawsInEffect"}:
            return
        text = tidy("".join(self._buffer).replace("\u2060", ""))
        if self._capture == "head":
            self.head = text
        elif self._capture == "body" and text:
            if re.fullmatch(r"[a-z]{1,2}", self._id):
                self._label = self._id
            self.paragraphs.append((self._label, text))
        elif self._capture == "credit":
            self.source_credit = text
        elif self._capture == "parent1ExpCite":
            match = re.match(r"CHAPTER\s+([0-9A-Z]+)\s*-\s*(.+)", text)
            if match:
                self.chapter = f"Chapter {match.group(1)} — {match.group(2).strip().title()}"
        elif self._capture == "lawsInEffect":
            match = re.search(r"through (Public Law [0-9-]+ \(([^)]+)\))", text)
            if match:
                self.current_through = match.group(1)
        self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._buffer.append(data)


def olrc_record(session: requests.Session, title: str, section: str) -> dict:
    url = f"https://uscode.house.gov/us/usc/t{title}/s{section}/"
    response = fetch(session, url)
    response.encoding = "utf-8"  # the page declares no charset; it is UTF-8
    parser = OlrcSectionParser()
    parser.feed(response.text)
    if not parser.head or not parser.paragraphs:
        raise ValueError(f"No statutory text found for {title} U.S.C. {section} at {url}")
    heading = re.sub(r"^§\s*" + re.escape(section) + r"\.\s*", "", parser.head).strip()
    text = "\n\n".join(paragraph for _label, paragraph in parser.paragraphs)
    return {
        "code": "usc",
        "key": f"{title} U.S.C. {section}",
        "title_number": str(title),
        "section": section,
        "title": heading,
        "citation": f"{title} U.S.C. § {section}",
        "url": url,
        "acquired_from_url": response.url,
        "edition": "OLRC current",
        "chapter": parser.chapter,
        "current_through": parser.current_through,
        "effective_date": "",
        "history": parser.source_credit,
        "text_basis": "olrc_current",
        "raw_sha256": sha256(response.text),
        "normalized_text_sha256": sha256(text),
        "_paragraphs": parser.paragraphs,
    }


def usc_record(session: requests.Session, base_url: str, title: str, section: str) -> dict:
    """The Law Revision Counsel's current text, or GovInfo's annual edition if that cannot be read."""
    try:
        return olrc_record(session, title, section)
    except (requests.RequestException, ValueError):
        return govinfo_record(session, base_url, title, section)


def govinfo_record(session: requests.Session, base_url: str, title: str, section: str) -> dict:
    url = f"{base_url}/{title}/{section}?link-type=html"
    response = fetch(session, url)
    parser = UscSectionParser()
    parser.feed(response.text)
    if not parser.head or not parser.paragraphs:
        raise ValueError(f"No statutory text found for {title} U.S.C. {section} at {url}")
    heading = re.sub(r"^§\s*" + re.escape(section) + r"\.\s*", "", parser.head[0]).strip()
    paragraphs, label = [], ""
    for css, text in parser.paragraphs:
        if css == "subsection-head":
            match = TOP_LEVEL_LABEL.match(text)
            label = match.group(1) if match else label
        paragraphs.append((label, text))
    text = "\n\n".join(paragraph for _label, paragraph in paragraphs)
    return {
        "code": "usc",
        "key": f"{title} U.S.C. {section}",
        "title_number": str(title),
        "section": section,
        "title": heading,
        "citation": f"{title} U.S.C. § {section}",
        "url": f"https://www.govinfo.gov/link/uscode/{title}/{section}",
        "acquired_from_url": response.url,
        "edition": parser.edition,
        "chapter": parser.chapter,
        "current_through": parser.current_through,
        "effective_date": "",
        "history": " ".join(parser.source_credit),
        "text_basis": "official_annual_edition",
        "raw_sha256": sha256(response.text),
        "normalized_text_sha256": sha256(text),
        "_paragraphs": paragraphs,
    }


# Code of Federal Regulations, from the eCFR versioner API


def ecfr_date(session: requests.Session, base_url: str, title: str) -> str:
    payload = fetch(session, f"{base_url}/api/versioner/v1/titles.json").json()
    for item in payload.get("titles", []):
        if str(item.get("number")) == str(title):
            return item["up_to_date_as_of"]
    raise ValueError(f"eCFR does not list title {title}")


def _element_text(element: ET.Element) -> str:
    return tidy("".join(element.itertext()))


def cfr_records(session: requests.Session, base_url: str, title: str, part: str, subpart: str, as_of: str) -> list[dict]:
    query = f"part={part}" + (f"&subpart={subpart}" if subpart else "")
    url = f"{base_url}/api/versioner/v1/full/{as_of}/title-{title}.xml?{query}"
    response = fetch(session, url)
    root = ET.fromstring(response.content)
    records = []
    for div in root.iter("DIV8"):
        if div.get("TYPE") != "SECTION":
            continue
        number = div.get("N", "")
        head = div.find("HEAD")
        heading = _element_text(head) if head is not None else number
        heading = re.sub(r"^§\s*" + re.escape(number) + r"\s*", "", heading).strip()
        paragraphs, history, notices, label = [], [], [], ""
        for child in div:
            if child.tag == "HEAD":
                continue
            text = _element_text(child)
            if not text:
                continue
            if child.tag in {"CITA", "SECAUTH"}:
                history.append(text)
                continue
            if child.tag == "XREF":
                # eCFR's pointer to a published amendment (or its delay) that
                # is not yet in the text. It says the rule is about to change,
                # which is exactly what a reader checking currency needs.
                notices.append(text)
                continue
            match = TOP_LEVEL_LABEL.match(text)
            if match and match.group(1) == next_label(label):
                label = match.group(1)
            paragraphs.append((label, text))
        if notices and paragraphs:
            paragraphs.insert(0, ("", "[eCFR notice: " + " ".join(notices) + "]"))
        if not paragraphs:
            # A reserved section has a heading and nothing else; it is not law.
            continue
        text = "\n\n".join(paragraph for _label, paragraph in paragraphs)
        records.append({
            "code": "cfr",
            "key": f"{title} C.F.R. {number}",
            "title_number": str(title),
            "part": str(part),
            "subpart": subpart,
            "section": number,
            "title": heading,
            "citation": f"{title} C.F.R. § {number}",
            "url": f"https://www.ecfr.gov/current/title-{title}/section-{number}",
            "acquired_from_url": url,
            "ecfr_as_of": as_of,
            "effective_date": "",
            "history": " ".join(history),
            "pending_amendments": notices,
            "raw_sha256": sha256(ET.tostring(div, encoding="unicode")),
            "normalized_text_sha256": sha256(text),
            "_paragraphs": paragraphs,
        })
    if not records:
        raise ValueError(f"No sections found for {title} CFR part {part}{' subpart ' + subpart if subpart else ''}")
    return records


# Writing


def record_id(record: dict) -> str:
    return f"{record['code']}-{record['title_number']}-{record['section']}".replace(".", "-").casefold()


def chunk_paragraphs(paragraphs: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
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


def label_span(chunk: list[tuple[str, str]]) -> str:
    labels = [label for label, _text in chunk if label]
    if not labels:
        return ""
    first, last = labels[0], labels[-1]
    return f"({first})" if first == last else f"({first})–({last})"


def write_record(record: dict, target: dict, *, dry_run: bool) -> list[dict]:
    paragraphs = record.pop("_paragraphs")
    ident = record_id(record)
    heading = f"{record['citation']} — {record['title']}"
    chunks = chunk_paragraphs(paragraphs)
    # The second path element is the unit whose applicability and definition
    # sections speak for its members: a U.S. Code chapter, or one C.F.R. part.
    # Title alone is too wide -- 42 U.S.C. holds both Section 8 and the Fair
    # Housing Act, and their definitions are not each other's.
    if record["code"] == "usc":
        unit = f"{record['title_number']} U.S.C. {record.get('chapter') or 'Title ' + record['title_number']}"
        path = ["United States Code", unit, record["citation"]]
    else:
        path = [f"{record['title_number']} C.F.R.", f"{record['title_number']} C.F.R. Part {record['part']}", record["citation"]]
    hints = [str(hint) for hint in target.get("retrieval_hints") or []]
    inventory = []
    for part, chunk in enumerate(chunks, 1):
        chunk_id = f"{ident}-{part:02d}"
        span = label_span(chunk) if len(chunks) > 1 else ""
        chunk_heading = f"{heading} {span}".strip()
        text = "\n\n".join(paragraph for _label, paragraph in chunk)
        if part == len(chunks) and record["history"]:
            text += f"\n\n[History: {record['history']}]"
        front_matter = {**record, "chunk_id": chunk_id, "chunk_part": part, "chunk_parts_in_section": len(chunks)}
        inventory.append({
            "id": chunk_id,
            "file": f"chunks/{chunk_id}.md",
            "heading": chunk_heading,
            "path": path,
            "content_kind": "regulation-section" if record["code"] == "cfr" else "statute-section",
            "section": record["key"],
            "chapter": path[1],
            "citation": record["citation"],
            "url": record["url"],
            "effective_date": record["effective_date"],
            "text_basis": record.get("text_basis") or target["_text_basis"],
            "retrieval_hints": hints,
        })
        if not dry_run:
            (CHUNKS_DIR / f"{chunk_id}.md").write_text(
                front(front_matter) + f"# {chunk_heading}\n\n## Source text\n\n{text}\n", encoding="utf-8"
            )
    if not dry_run:
        keep = {item["file"].split("/", 1)[1] for item in inventory}
        for stale in CHUNKS_DIR.glob(f"{ident}-*.md"):
            if stale.name not in keep:
                stale.unlink()
        body = "\n\n".join(paragraph for _label, paragraph in paragraphs)
        (SECTIONS_DIR / f"{ident}.md").write_text(front(record) + f"# {heading}\n\n{body}\n", encoding="utf-8")
    return inventory


def select_targets(scope: dict, args: argparse.Namespace) -> list[dict]:
    targets = scope.get("targets", [])
    if args.all:
        return targets
    if args.ring:
        return [target for target in targets if str(target.get("ring")) == args.ring]
    if args.cfr_part:
        title, _, part = args.cfr_part.rpartition(":")
        title = title or "24"
        selected = [
            t for t in targets
            if t.get("code") == "cfr" and str(t.get("part")) == part and str(t.get("title")) == title
        ]
        if not selected:
            raise ValueError(f"{title} CFR part {part} is not configured in {SCOPE_PATH}")
        return selected
    if args.usc:
        title, _, section = args.usc.partition(":")
        return [{"code": "usc", "title": title, "sections": [section], "ring": "ad-hoc"}]
    raise ValueError("Choose --all, --ring, --cfr-part, or --usc.")


def section_sort_key(item: dict):
    code_order = 0 if item.get("code") == "usc" else 1
    parts = re.findall(r"\d+|[a-z]+", str(item.get("section", "")).casefold())
    return (code_order, int(item.get("title_number") or 0), [(0, int(p)) if p.isdigit() else (1, p) for p in parts])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="Refresh every configured target and prune what left scope.")
    selection.add_argument("--ring", choices=["1", "2"])
    selection.add_argument("--cfr-part", help="One configured CFR part, as PART (title 24) or TITLE:PART, e.g. 966 or 7:3560")
    selection.add_argument("--usc", help="One U.S. Code section as TITLE:SECTION, e.g. 15:9058")
    parser.add_argument("--force", action="store_true", help="Rewrite records even when the normalized text is unchanged.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds to wait between requests.")
    args = parser.parse_args()

    scope = yaml.safe_load(SCOPE_PATH.read_text(encoding="utf-8")) or {}
    sources = scope["sources"]
    targets = select_targets(scope, args)
    session = requests.Session()
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.is_file() else {}
    prior = {item["key"]: item for item in (manifest or {}).get("sections", []) if item.get("key")}
    if not args.dry_run:
        SECTIONS_DIR.mkdir(parents=True, exist_ok=True)
        CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

    ecfr_dates: dict[str, str] = {}
    changed, unchanged, failures, selected_keys = [], [], [], set()
    for target in targets:
        code = target["code"]
        source = sources[code]
        target = {**target, "_text_basis": source["text_basis"]}
        try:
            if code == "usc":
                # One section that cannot be read must not cost the others in
                # the same target; it is reported and its old record is kept.
                records = []
                for section in target.get("sections", []):
                    try:
                        records.append(usc_record(session, source["base_url"].rstrip("/"), str(target["title"]), str(section)))
                    except (requests.RequestException, ValueError) as exc:
                        failures.append({"target": f"{target['title']} U.S.C. {section}", "error": str(exc)})
                    time.sleep(max(args.pause, 0))
            else:
                title = str(target["title"])
                if title not in ecfr_dates:
                    ecfr_dates[title] = ecfr_date(session, source["base_url"].rstrip("/"), title)
                records = cfr_records(
                    session, source["base_url"].rstrip("/"), title, str(target["part"]),
                    str(target.get("subpart") or ""), ecfr_dates[title],
                )
                time.sleep(max(args.pause, 0))
        except (requests.RequestException, ValueError, ET.ParseError) as exc:
            failures.append({"target": target.get("label") or target, "error": str(exc)})
            continue
        for record in records:
            key = record["key"]
            selected_keys.add(key)
            old = prior.get(key, {})
            if not args.force and old.get("normalized_text_sha256") == record["normalized_text_sha256"]:
                unchanged.append(key)
                continue
            record.update({
                "fetched_at": now(),
                "source_name": source["name"],
                "publisher": source["publisher"],
                "jurisdiction": "United States",
                "content_kind": "statute",
                "text_basis": record.get("text_basis") or source["text_basis"],
                "scope_label": target.get("label", ""),
                "record_path": f"sections/{record_id(record)}.md",
            })
            prior[key] = {**{k: v for k, v in record.items() if k != "_paragraphs"},
                          "chunks": write_record(record, target, dry_run=args.dry_run)}
            changed.append(key)

    if not args.dry_run:
        if args.all and not failures:
            # A complete, successful refresh is authoritative for its declared
            # scope; a partial one must not prune what it failed to read.
            for key in [key for key in prior if key not in selected_keys]:
                item = prior.pop(key)
                for path in [item.get("record_path", ""), *[chunk.get("file", "") for chunk in item.get("chunks", [])]]:
                    if path and (LIBRARY / path).is_file():
                        (LIBRARY / path).unlink()
        sections = sorted(prior.values(), key=section_sort_key)
        chunks = [chunk for item in sections for chunk in item.get("chunks", [])]
        output = {
            "schema_version": 1,
            "document_slug": SLUG,
            "document_title": "Federal housing law (selected U.S. Code, 24 C.F.R., and 7 C.F.R.)",
            "jurisdiction": "United States",
            "content_kind": "statute",
            "source_name": "United States Code; Electronic Code of Federal Regulations",
            "publisher": "U.S. Government Publishing Office; Office of the Federal Register",
            "generated_at": now(),
            "generator": "scripts/ingest_federal_housing_law.py",
            "ecfr_as_of": ecfr_dates or (manifest or {}).get("ecfr_as_of", {}),
            "section_count": len(sections),
            "chunk_count": len(chunks),
            "sections": sections,
            "chunks": chunks,
        }
        MANIFEST_PATH.write_text(yaml.safe_dump(output, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(json.dumps({
        "changed": len(changed), "unchanged": len(unchanged), "failures": failures,
        "ecfr_as_of": ecfr_dates, "dry_run": args.dry_run,
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Whether each authority a brief relies on actually reaches the brief's facts.

A brief can cite a rule correctly and still lose on it, because the rule does
not cover this tenant, this building, this program, or these dates. The
opponent may notice; nothing made it ask. This check asks it of every
authority, and answers from the authority's own scope text rather than from a
model's memory of it:

    inventory   every authority the brief names: citations, popular-name acts,
                and what the argument map read as authority (deterministic)
    resolve     each to the library's own text, or "not in the library"
    scope       the section itself, its part or chapter's applicability,
                definitions, and scope sections, and one hop of the
                definitions it cross-references (deterministic)
    assess      applies / does not apply / unclear on these facts, quoting the
                scope text that decides it (model)
    ground      the quote must be in the scope text supplied; an assessment
                whose quote is not there is reported as unclear (deterministic)

Nothing about any particular statute is written here. What a rule covers is in
its text; a maintained rule profile can add elements on top, but this check
works without one. Authorities the library does not hold, and assessments left
unclear, are what the web lookup check starts from.
"""

from __future__ import annotations

import re

from apps.argument_gym.pipeline import Stage, choice, clean, dumps
from apps.sources.research.citations import find_citations


MAX_AUTHORITIES = 4
MAX_SECTION_CHUNKS = 3
MAX_SCOPE_CHUNKS = 3
MAX_CROSS_REFERENCES = 3
CHUNK_CHARS = 5000
VERDICTS = {"applies", "does_not_apply", "unclear_on_facts"}
ISSUES = {"coverage", "timing", "jurisdiction", "other"}
SCOPE_HEADING = re.compile(r"applicab|definition|scope|purpose|coverage|covered|who is", re.I)
DEFINITION_CUE = re.compile(r"\bmeans\b|defined in|as defined|meaning given|has the meaning", re.I)
NAMED_ACT = re.compile(r"\b((?:[A-Z][A-Za-z.&'-]*\s+){0,6}?(?:[A-Z][A-Za-z.&'-]*)\s+Act)\b")
LEADING_WORDS = re.compile(r"^(?:The|the|Under|under|And|and|Of|of)\s+")
CASE_KINDS = {"reporter", "ohio_public_domain"}
CITATION_CORPORA = {
    "revised_code": ["statutes"],
    "administrative_code": ["statutes"],
    "federal_code": ["statutes"],
    "ohio_public_domain": ["cases"],
    "reporter": ["cases"],
}


def authority_key(text):
    """One spelling per section: ``Ohio Rev. Code § 5321.04`` and ``R.C. 5321.04(A)`` agree."""
    parsed = find_citations(text or "")
    if not parsed:
        return ""
    citation = parsed[0]
    normalized = citation.normalized
    if citation.subdivision and normalized.endswith(citation.subdivision):
        normalized = normalized[: -len(citation.subdivision)]
    return re.sub(r"\(.*$", "", normalized).strip()


def _normalize_quote(text):
    text = str(text or "")
    for old, new in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("–", "-"), ("—", "-")):
        text = text.replace(old, new)
    return " ".join(text.split()).casefold()


class LibraryAuthorities:
    """The enacted-law records in the research index, grouped by the section they are."""

    def __init__(self, index=None):
        if index is None:
            from apps.sources.research.index import research_index

            index = research_index()
        self.index = index
        self.by_key = {}
        self.by_group = {}
        for record in index.records:
            if record.corpus not in {"statutes", "ordinances"} or not record.authority_citation:
                continue
            key = authority_key(record.authority_citation)
            if key:
                self.by_key.setdefault(key, []).append(record)
            path = record.metadata.get("sectionPath") or []
            if len(path) >= 2:
                self.by_group.setdefault((record.metadata.get("documentSlug"), path[1]), []).append(record)

    def section(self, key):
        return sorted(self.by_key.get(key, []), key=lambda record: str(record.metadata.get("chunkId")))

    def by_name(self, name):
        """A popular-name act, resolved by searching the statutes for it."""
        from apps.sources.research import engine

        try:
            payload = engine.search(f'"{name}"', corpora=["statutes"], limit=1, index=self.index)
        except Exception:  # noqa: BLE001 - an unsearchable index resolves nothing, it does not crash a run
            return ""
        for result in payload.get("results", []):
            key = authority_key(result.get("authorityCitation") or result.get("citation") or "")
            if key and key in self.by_key:
                return key
        return ""

    def holds_case(self, citation):
        from apps.sources.research import engine

        try:
            payload = engine.search(citation, corpora=["cases"], limit=1, index=self.index)
        except Exception:  # noqa: BLE001
            return False
        return bool(payload.get("results"))


def authority_inventory(brief_text, argument_map=(), *, library=None):
    """Every authority the brief names, and whether and where the library holds it."""
    library = library or LibraryAuthorities()
    found = {}

    def add(label, kind):
        if kind in CASE_KINDS:
            key = label
        elif kind == "named_act":
            key = library.by_name(label) or f"name:{label}"
        else:
            key = authority_key(label) or f"name:{label}"
        entry = found.setdefault(key, {"key": key, "authority": label, "kind": kind, "spellings": []})
        if label not in entry["spellings"]:
            entry["spellings"].append(label)

    for citation in find_citations(brief_text):
        add(citation.normalized, citation.kind)
    for match in NAMED_ACT.finditer(brief_text or ""):
        name = LEADING_WORDS.sub("", match.group(1)).strip()
        if len(name.split()) >= 2:
            add(name, "named_act")
    for claim in argument_map or []:
        for authority in claim.get("citedAuthority") or []:
            authority = clean(authority, limit=160)
            if not authority:
                continue
            parsed = find_citations(authority)
            add(parsed[0].normalized if parsed else authority, parsed[0].kind if parsed else "described")

    inventory = []
    for entry in found.values():
        if entry["kind"] in CASE_KINDS:
            held = library.holds_case(entry["key"])
            inventory.append({**entry, "held": held, "resolvedTo": entry["key"] if held else ""})
            continue
        held = entry["key"] in library.by_key
        inventory.append({**entry, "held": held, "resolvedTo": entry["key"] if held else ""})
    return inventory


def _terms(text):
    return {term for term in re.findall(r"[a-z0-9]{4,}", str(text or "").casefold())}


def _excerpt(record, source_id, role):
    return {
        "id": source_id,
        "role": role,
        "citation": record.authority_citation or record.citation,
        "heading": record.metadata.get("heading", ""),
        "recordId": record.key,
        "text": record.text[:CHUNK_CHARS],
        "url": record.url,
    }


def scope_text(key, library, *, claim_text="", start=1):
    """The library text that says what ``key`` covers, with an id per passage."""
    section = library.section(key)
    if not section:
        return []
    chosen = [section[0]]
    claim_terms = _terms(claim_text)
    rest = sorted(section[1:], key=lambda record: -len(claim_terms & _terms(record.text)))
    chosen.extend(rest[: MAX_SECTION_CHUNKS - 1])

    scope = []
    path = section[0].metadata.get("sectionPath") or []
    if len(path) >= 2:
        group = library.by_group.get((section[0].metadata.get("documentSlug"), path[1]), [])
        scope = [
            record
            for record in group
            if record not in section and SCOPE_HEADING.search(str(record.metadata.get("heading", "")))
        ][:MAX_SCOPE_CHUNKS]

    references = []
    seen = {key}
    for record in chosen:
        for cue in DEFINITION_CUE.finditer(record.text):
            window = record.text[max(cue.start() - 40, 0) : cue.end() + 300]
            for citation in find_citations(window):
                target = authority_key(citation.normalized)
                if target and target not in seen and target in library.by_key:
                    seen.add(target)
                    references.append(library.section(target)[0])
            if len(references) >= MAX_CROSS_REFERENCES:
                break

    excerpts = []
    for role, records in (("section", chosen), ("scope", scope), ("cross_reference", references[:MAX_CROSS_REFERENCES])):
        for record in records:
            excerpts.append(_excerpt(record, f"ap{start + len(excerpts)}", role))
    return excerpts


def _claim_text(argument_map, entry):
    spellings = [spelling.casefold() for spelling in entry["spellings"]]
    related = [
        claim
        for claim in argument_map or []
        if any(
            spelling in " ".join([claim.get("proposition", ""), *(claim.get("citedAuthority") or [])]).casefold()
            for spelling in spellings
        )
    ]
    return related


def assess(units, argument_map, inventory, library, *, jurisdiction="", llm_client=None):
    """Assess every held statute or rule the brief relies on. Returns (report, attacks-ready items, trace)."""
    candidates = [entry for entry in inventory if entry["held"] and entry["kind"] not in CASE_KINDS]
    # Authorities the argument map ties to a proposition come first: those are
    # the ones the brief is actually standing on.
    candidates.sort(key=lambda entry: 0 if _claim_text(argument_map, entry) else 1)
    candidates = candidates[:MAX_AUTHORITIES]

    authorities, excerpts_by_id = [], {}
    for entry in candidates:
        claims = _claim_text(argument_map, entry)
        claim_text = " ".join(
            " ".join([claim.get("proposition", ""), *(claim.get("assertedFacts") or [])]) for claim in claims
        )
        excerpts = scope_text(entry["key"], library, claim_text=claim_text, start=len(excerpts_by_id) + 1)
        if not excerpts:
            continue
        excerpts_by_id.update({excerpt["id"]: excerpt for excerpt in excerpts})
        authorities.append(
            {
                "authority": entry["key"],
                "asCitedInBrief": entry["spellings"],
                "claims": [
                    {
                        "unitId": claim["unitId"],
                        "proposition": claim.get("proposition", ""),
                        "assertedFacts": claim.get("assertedFacts", []),
                    }
                    for claim in claims
                ],
                "scopeText": [
                    {key: excerpt[key] for key in ("id", "role", "citation", "heading", "text")} for excerpt in excerpts
                ],
            }
        )

    if not authorities:
        return [], excerpts_by_id, {"stage": "applicability", "method": "none", "count": 0, "trace": []}

    unit_ids = {unit["id"] for unit in units}
    names = {item["authority"] for item in authorities}

    def parse(payload):
        cleaned = []
        for item in payload.get("assessments") or []:
            if not isinstance(item, dict) or item.get("authority") not in names:
                continue
            cleaned.append(
                {
                    "authority": item["authority"],
                    "verdict": choice(item.get("verdict"), VERDICTS, "unclear_on_facts"),
                    "issue": choice(item.get("issue"), ISSUES, "coverage"),
                    "unitIds": [value for value in item.get("unitIds") or [] if value in unit_ids][:3],
                    "condition": clean(item.get("condition"), limit=600),
                    "briefFacts": clean(item.get("briefFacts"), limit=600),
                    "quote": clean(item.get("quote"), limit=800),
                    "sourceId": str(item.get("sourceId") or ""),
                    "explanation": clean(item.get("explanation"), limit=1200),
                }
            )
        return cleaned

    def fallback():
        # No model, no reading of scope: say so for each authority rather
        # than reporting that it applies.
        return [
            {
                "authority": item["authority"],
                "verdict": "not_assessed",
                "issue": "coverage",
                "unitIds": [claim["unitId"] for claim in item["claims"]][:3],
                "condition": "",
                "briefFacts": "",
                "quote": "",
                "sourceId": "",
                "explanation": "No model was available to read the scope text against the brief's facts.",
            }
            for item in authorities
        ]

    results, trace = Stage("applicability", llm_client=llm_client).run(
        prompt_key="argument_gym.applicability",
        context={
            "jurisdiction": jurisdiction or "the filing jurisdiction",
            "authorities": dumps(authorities),
            "brief_units": dumps([{"id": unit["id"], "text": unit["text"][:1200]} for unit in units][:60]),
        },
        parse=parse,
        fallback=fallback,
        temperature=0.1,
    )
    for result in results:
        ground(result, excerpts_by_id)
    return results, excerpts_by_id, trace


def ground(result, excerpts_by_id):
    """Keep a verdict only if its quote is in the scope text it names."""
    if result["verdict"] not in {"applies", "does_not_apply"} and result["verdict"] != "unclear_on_facts":
        result["grounded"] = False
        return result
    excerpt = excerpts_by_id.get(result["sourceId"])
    quote = _normalize_quote(result["quote"])
    grounded = bool(excerpt and quote and len(quote) >= 12 and quote in _normalize_quote(excerpt["text"]))
    result["grounded"] = grounded
    if not grounded and result["verdict"] in {"applies", "does_not_apply"}:
        result["ungroundedVerdict"] = result["verdict"]
        result["verdict"] = "unclear_on_facts"
        result["explanation"] = (
            "The reading could not be confirmed against the scope text: the passage it quoted is not in "
            f"the text supplied. {result['explanation']}"
        ).strip()
    if excerpt:
        result["source"] = {key: excerpt[key] for key in ("citation", "heading", "recordId", "url")}
    return result


def run_applicability(units, argument_map, brief_text, *, jurisdiction="", llm_client=None, index=None):
    """The full check. Returns (report, assessments, scope excerpts by id). Never raises."""
    try:
        library = LibraryAuthorities(index)
        inventory = authority_inventory(brief_text, argument_map, library=library)
    except Exception as exc:  # noqa: BLE001 - an unreadable library is a reason, not a crash
        return {
            "status": "could_not_run",
            "summary": f"Applicability could not run: the library index could not be read ({exc}).",
            "authorities": [],
            "assessments": [],
        }, [], {}
    assessments, excerpts, trace = assess(
        units, argument_map, inventory, library, jurisdiction=jurisdiction, llm_client=llm_client
    )
    not_held = [entry for entry in inventory if not entry["held"]]
    counts = {verdict: sum(1 for item in assessments if item["verdict"] == verdict) for verdict in [*VERDICTS, "not_assessed"]}
    if not inventory:
        summary = "The brief names no authority this check could find."
    elif assessments and counts["not_assessed"] == len(assessments):
        summary = "Applicability was not assessed: no model was available to read the scope text against the brief's facts."
    else:
        summary = (
            f"{len(assessments)} authorit{'y' if len(assessments) == 1 else 'ies'} read against "
            f"{'its' if len(assessments) == 1 else 'their'} own scope text: {counts['does_not_apply']} may not reach "
            f"the brief's facts, {counts['unclear_on_facts']} turn on a fact the brief does not settle, "
            f"{counts['applies']} appear to apply."
        )
    if not_held:
        summary += (
            f" {len(not_held)} cited authorit{'y is' if len(not_held) == 1 else 'ies are'} not in CLARA's library, "
            "so applicability could not be checked against the text."
        )
    report = {
        "status": "ran",
        "summary": summary,
        "authorities": inventory,
        "assessments": assessments,
        "trace": trace,
    }
    return report, assessments, excerpts


def applicability_attacks(assessments, units, start_index, *, anchor):
    """An authority that may not reach the facts, or turns on an unstated fact, as an attack."""
    attacks = []
    units_by_id = {unit["id"]: unit for unit in units}
    for result in assessments:
        if result["verdict"] not in {"does_not_apply", "unclear_on_facts"}:
            continue
        if result["verdict"] == "unclear_on_facts" and not result.get("grounded"):
            # An unconfirmed "unclear" is no finding at all; it stays in the report.
            continue
        unit = next((units_by_id[unit_id] for unit_id in result["unitIds"] if unit_id in units_by_id), None)
        unit = unit or anchor(units, result["authority"])
        if not unit:
            continue
        source = result.get("source") or {}
        if result["verdict"] == "does_not_apply":
            argument = (
                f"{result['authority']} does not reach these facts. {result['condition']} "
                f"The brief states: {result['briefFacts']} {result['explanation']}"
            )
            why = (
                f"The brief stands on {result['authority']}; if it does not cover this {result['issue']}, "
                "the argument built on it falls with it."
            )
        else:
            argument = (
                f"Whether {result['authority']} applies turns on a fact the brief does not establish: "
                f"{result['condition']} {result['explanation']}"
            )
            why = (
                f"Opposing counsel will say {result['authority']} does not apply, and the brief gives the court "
                "nothing to find otherwise."
            )
        attacks.append(
            {
                "id": f"ap{start_index + len(attacks)}",
                "unitId": unit["id"],
                "category": "legal_authority",
                "argument": " ".join(argument.split()),
                "whyItMatters": f"{why} Scope text relied on ({source.get('citation', 'library')}): “{result['quote']}”",
                "legalSourceIds": [result["sourceId"]] if result.get("sourceId") else [],
                "recordMaterialIds": [],
                "origin": "applicability",
            }
        )
    return attacks

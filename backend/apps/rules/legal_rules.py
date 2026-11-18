"""Loading legal rule profiles, and detecting which ones a brief invoked.

An advocate who cites a rule has taken on its elements. Detection is
deterministic -- a citation or a well-known phrase is either in the text or it
is not -- so an audit can always say which words in the brief made it check a
given rule.

Elements come from the profile's own YAML and, where the profile names one, from
a published decision-table row. The issue-selection tables already encode what a
notice defense turns on; reading that row as an element checklist keeps the two
from drifting apart.
"""

import re
from datetime import date

import yaml

from apps.core.content_library import content_path
from apps.rules.engine import condition_fields
from apps.rules.models import CourtProfile, DecisionTable, LegalRuleProfile


CONTENT_DIRECTORY = ("legal-rules",)
REQUIRED_FIELDS = {"slug", "name", "citation"}
RULE_TYPES = {choice for choice, _label in LegalRuleProfile.RULE_TYPE_CHOICES}
VERIFICATIONS = {choice for choice, _label in CourtProfile.VERIFICATION_CHOICES}
ELEMENT_SEVERITIES = {"error", "warning", "info"}


# Normalized spellings (see apps.sources.jurisdiction.normalize) of a profile
# jurisdiction that means "federal law, applicable wherever the brief is filed".
FEDERAL_JURISDICTIONS = {"unitedstates", "federal", "us"}


def _string_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _clean_elements(payload, path):
    elements = payload.get("elements") or []
    if not isinstance(elements, list):
        raise ValueError(f"Legal rule {path}: elements must be a list.")
    cleaned = []
    seen = set()
    for element in elements:
        if not isinstance(element, dict):
            raise ValueError(f"Legal rule {path}: every element must be a mapping.")
        element_id = str(element.get("id") or "").strip()
        label = str(element.get("label") or "").strip()
        if not element_id or not label:
            raise ValueError(f"Legal rule {path}: every element needs an id and a label.")
        if element_id in seen:
            raise ValueError(f"Legal rule {path}: duplicate element id {element_id!r}.")
        seen.add(element_id)
        severity = str(element.get("severity", "error")).strip()
        cleaned.append(
            {
                "id": element_id,
                "label": label,
                "requirement": str(element.get("requirement", "")).strip(),
                "severity": severity if severity in ELEMENT_SEVERITIES else "error",
                "needsRecordSupport": bool(element.get("needs_record_support", False)),
                "patterns": _string_list(element.get("patterns")),
                # Wording that, in a brief invoking this rule, says the element
                # is *not* met -- "a market-rate building with no federal
                # subsidy" under the CARES Act. A match is the brief's own
                # words pointing the other way, so it is reported even when a
                # model read the element as pleaded.
                "contradictionPatterns": _string_list(element.get("contradiction_patterns")),
                "note": str(element.get("note", "")).strip(),
                "origin": "profile",
            }
        )
    return cleaned


def load_legal_rule_file(path):
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read legal rule {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Legal rule {path} must contain a YAML mapping.")
    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        raise ValueError(f"Legal rule {path} is missing: {', '.join(sorted(missing))}.")
    rule_type = str(payload.get("rule_type", LegalRuleProfile.STATUTE)).strip()
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Legal rule {path} has unknown rule_type {rule_type!r}.")
    verification = str(payload.get("verification", CourtProfile.UNVERIFIED)).strip()
    if verification not in VERIFICATIONS:
        raise ValueError(f"Legal rule {path} has unknown verification {verification!r}.")
    for pattern in _string_list(payload.get("citation_patterns")):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Legal rule {path} has an invalid citation pattern {pattern!r}: {exc}") from exc
    elements = _clean_elements(payload, path)
    if not elements and not payload.get("decision_table_row"):
        raise ValueError(f"Legal rule {path} declares no elements and names no decision-table row.")
    return {
        "slug": str(payload["slug"]).strip(),
        "name": str(payload["name"]).strip(),
        "citation": str(payload["citation"]).strip(),
        "rule_type": rule_type,
        "jurisdiction": str(payload.get("jurisdiction", "")).strip(),
        "summary": str(payload.get("summary", "")).strip(),
        "citation_patterns": _string_list(payload.get("citation_patterns")),
        "aliases": _string_list(payload.get("aliases")),
        "elements": elements,
        "decision_table_key": str(payload.get("decision_table_key", "")).strip(),
        "decision_table_row": str(payload.get("decision_table_row", "")).strip(),
        "verification": verification,
        "source": str(payload.get("source", "")).strip(),
        "source_url": str(payload.get("source_url", "")).strip(),
        "verified_on": _parse_date(payload.get("verified_on")),
        "notes": str(payload.get("notes", "")).strip(),
        "active": bool(payload.get("active", True)),
    }


def legal_rule_seeds():
    directory = content_path(*CONTENT_DIRECTORY)
    if not directory.exists():
        return []
    return [load_legal_rule_file(path) for path in sorted(directory.glob("*.yaml"))]


def sync_legal_rule_seeds(*, update_existing=False):
    synced = []
    for seed in legal_rule_seeds():
        profile, created = LegalRuleProfile.objects.get_or_create(slug=seed["slug"], defaults=seed)
        if not created and update_existing and not profile.is_locally_edited:
            for field, value in seed.items():
                if field != "slug":
                    setattr(profile, field, value)
            profile.save()
        synced.append((profile, created))
    return synced


def ensure_legal_rule_profiles():
    """Seed the maintained rules the first time something asks for them.

    The audit is only as good as the rules on file, and a fresh deployment that
    silently audited nothing would report every brief as carrying every element
    it invoked. Seeding is skipped once anything exists, so an office that has
    curated its own list is never re-seeded behind its back.
    """
    if LegalRuleProfile.objects.exists():
        return []
    return sync_legal_rule_seeds()


# Elements borrowed from the decision tables


def _humanize(field):
    return field.replace("_", " ").replace(".", ": ")


def elements_from_decision_table(key, row_id):
    """Read a published decision-table row as an element checklist.

    The row's conditions name the facts the rule turns on, and its outputs
    already carry the questions someone has to answer before the issue is real.
    Both are elements of the same rule seen from the pleading side.
    """
    if not key or not row_id:
        return []
    table = DecisionTable.objects.filter(key=key, status="published").order_by("-version").first()
    if not table:
        return []
    row = table.rows.filter(row_id=row_id, enabled=True).first()
    if not row:
        return []
    elements = []
    for question in (row.outputs or {}).get("missing_facts") or []:
        question = str(question).strip()
        if not question:
            continue
        elements.append(
            {
                "id": f"table-{re.sub(r'[^a-z0-9]+', '-', question.casefold())[:60].strip('-')}",
                "label": question,
                "requirement": f"Carried from decision table {table.key} v{table.version}, row {row.row_id}.",
                "severity": "warning",
                "needsRecordSupport": True,
                "patterns": [],
                "note": "",
                "origin": "decision_table",
                "source": {"tableKey": table.key, "tableVersion": table.version, "rowId": row.row_id},
            }
        )
    for field in sorted(condition_fields(row.conditions)):
        elements.append(
            {
                "id": f"table-fact-{re.sub(r'[^a-z0-9]+', '-', field.casefold())}",
                "label": f"The brief establishes {_humanize(field)}",
                "requirement": f"The rule row {row.row_id} turns on this fact.",
                "severity": "warning",
                "needsRecordSupport": True,
                "patterns": [],
                "note": "",
                "origin": "decision_table",
                "source": {"tableKey": table.key, "tableVersion": table.version, "rowId": row.row_id, "field": field},
            }
        )
    return elements


def rule_elements(profile):
    """The profile's own elements, plus anything the decision tables already say."""
    elements = list(profile.elements or [])
    known = {element.get("id") for element in elements}
    for element in elements_from_decision_table(profile.decision_table_key, profile.decision_table_row):
        if element["id"] not in known:
            elements.append(element)
            known.add(element["id"])
    return elements


# Detection


def _matches(profile, text):
    """Where in the brief this rule was invoked, and by what words."""
    hits = []
    for pattern in profile.citation_patterns or []:
        try:
            match = re.search(pattern, text, flags=re.IGNORECASE)
        except re.error:
            continue
        if match:
            hits.append({"kind": "citation", "matched": match.group(0), "offset": match.start()})
            break
    if not hits:
        for alias in profile.aliases or []:
            match = re.search(rf"\b{re.escape(alias)}\b", text, flags=re.IGNORECASE)
            if match:
                hits.append({"kind": "phrase", "matched": match.group(0), "offset": match.start()})
                break
    return hits


def detect_invoked_rules(text, *, profiles=None, jurisdiction=""):
    """Which maintained rules this brief invoked, and the words that show it.

    A rule invoked only by a phrase is reported as such: "three-day notice" in a
    sentence about the other side's notice is not the same as citing the statute,
    and the audit says which happened.
    """
    text = str(text or "")
    if profiles is None:
        profiles = LegalRuleProfile.objects.filter(active=True)
        if jurisdiction:
            # A rule from another state is not invoked by an Ohio brief that
            # happens to share a phrase.
            from apps.sources.jurisdiction import normalize

            # Federal law binds in every state, so a federal rule is never
            # filtered out by the filing jurisdiction.
            profiles = [
                profile
                for profile in profiles
                if not profile.jurisdiction
                or normalize(profile.jurisdiction) in FEDERAL_JURISDICTIONS
                or normalize(profile.jurisdiction) in normalize(jurisdiction)
            ]
    invoked = []
    for profile in profiles:
        hits = _matches(profile, text)
        if not hits:
            continue
        hit = hits[0]
        start = max(hit["offset"] - 200, 0)
        invoked.append(
            {
                "profile": profile,
                "invokedBy": hit["kind"],
                "matched": hit["matched"],
                "excerpt": re.sub(r"\s+", " ", text[start : hit["offset"] + 300]).strip(),
                "offset": hit["offset"],
            }
        )
    invoked.sort(key=lambda item: item["offset"])
    return invoked

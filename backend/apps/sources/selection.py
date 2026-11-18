"""Logical source selection backed by the maintained content library."""

from __future__ import annotations

from collections import Counter

import yaml

from apps.core.content_library import content_path


GUIDANCE_PATH = ("research-sources", "auto-source-guidance.yaml")

# This fallback keeps source selection usable for a partially staged provider.
# The repository guidance file is the authoritative, reviewable configuration.
FALLBACK_GUIDANCE = {
    "sources": {
        "ohio-statutes": {
            "kind": "rag",
            "label": "Ohio Statutes",
            "default_reason": "Primary-law baseline for general Ohio housing-law questions.",
        },
        "treatise": {
            "kind": "rag",
            "label": "Ohio eviction treatise",
            "default_reason": "Secondary-source baseline for issue spotting and practical context.",
        },
        "ohio-ordinances": {
            "kind": "rag",
            "label": "Ohio local ordinances and court rules",
            "default_reason": "Local law that can change the outcome where the case is filed.",
        },
    }
}


def source_guidance():
    """Load routing policy through the content-provider boundary."""
    try:
        payload = yaml.safe_load(content_path(*GUIDANCE_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return FALLBACK_GUIDANCE
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
        return FALLBACK_GUIDANCE
    return payload


def source_kinds(source_ids):
    sources = source_guidance()["sources"]
    return list(dict.fromkeys(sources[source_id]["kind"] for source_id in source_ids if source_id in sources))


def automatic_source_selection(query, *, matter=None):
    """Return Auto mode's selected sources and an explainable routing record."""
    sources = source_guidance()["sources"]
    text = (query or "").casefold()
    selected = []
    reasons = {}

    for source_id, source in sources.items():
        for rule in source.get("when", []):
            if not isinstance(rule, dict):
                continue
            if rule.get("requires_matter") and not matter:
                continue
            matched_terms = [str(term) for term in rule.get("terms", []) if str(term).casefold() in text]
            if matched_terms:
                selected.append(source_id)
                reasons[source_id] = rule.get("reason") or f"Matched: {', '.join(matched_terms)}."
                break

    if not selected:
        for source_id, source in sources.items():
            if source.get("default"):
                selected.append(source_id)
                reasons[source_id] = source.get("default_reason") or "Default source for general legal questions."

    # A malformed file should not make an Auto research request search nothing.
    if not selected:
        selected = list(FALLBACK_GUIDANCE["sources"])
        reasons = {source_id: source["default_reason"] for source_id, source in FALLBACK_GUIDANCE["sources"].items()}

    annotations = [
        {"id": source_id, "label": sources.get(source_id, FALLBACK_GUIDANCE["sources"].get(source_id, {})).get("label", source_id), "reason": reasons[source_id]}
        for source_id in dict.fromkeys(selected)
    ]
    return {"source_ids": list(dict.fromkeys(selected)), "annotations": annotations}


def automatic_source_ids(query, *, matter=None):
    """Compatibility wrapper for callers that only need logical source IDs."""
    return automatic_source_selection(query, matter=matter)["source_ids"]


def source_decision_with_counts(selection, results):
    """Add retrieval counts so an empty selected source is visible, not implicit."""
    counts_by_id = Counter()
    for result in results:
        # RAG results carry the document slug rather than the picker ID.  Map
        # by connector kind where a logical ID cannot be recovered precisely.
        for item in selection["annotations"]:
            if item["id"] == "ohio-cases" and result.source_kind == "local_cases":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "case-file" and result.source_kind == "legalserver":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "sharepoint" and result.source_kind == "sharepoint":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "user-resources" and result.source_kind == "user_resources":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "ohio-statutes" and result.metadata.get("documentSlug") in {"ohio-revised-code", "ohio-administrative-code"}:
                counts_by_id[item["id"]] += 1
            elif item["id"] == "federal-housing-law" and result.metadata.get("documentSlug") == "federal-housing-law":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "treatise" and result.metadata.get("documentSlug") == "ohio-eviction-landlord-tenant-law-6e":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "hud-handbook" and result.metadata.get("documentSlug") == "hud-4350-3-rev-1":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "green-book" and result.metadata.get("documentSlug") == "green-book":
                counts_by_id[item["id"]] += 1
            elif item["id"] == "ohio-ordinances" and result.metadata.get("documentKind") == "ordinance":
                counts_by_id[item["id"]] += 1
    return {
        "mode": "auto",
        "summary": "Auto sources selected the sources below based on the maintained routing guidance.",
        "sources": [{**item, "resultCount": counts_by_id[item["id"]]} for item in selection["annotations"]],
    }

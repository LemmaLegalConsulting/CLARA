"""Portable audit records for model-written parts of a draft.

Component versions and source bindings are the durable drafting record.  This
module projects them into one JSON-safe payload that can travel with an export
and be rendered as a LegalServer case note.  It deliberately includes every AI
version, not only the current version: a reviewer's edit must not erase what the
model originally wrote.
"""

import json
import re

from django.utils import timezone

from apps.drafting.components import ensure_components
from apps.drafting.models import DocumentComponent
from apps.drafting.source_bindings import binding_to_dict


AUDIT_SCHEMA_VERSION = 1


def _paragraphs(body):
    return [part.strip() for part in re.split(r"\n{2,}|\n", body or "") if part.strip()]


def draft_ai_audit(draft):
    """Return the AI interactions and sources retained for ``draft``."""
    is_persisted_draft = bool(getattr(draft, "pk", None) and hasattr(draft, "_meta"))
    if is_persisted_draft:
        ensure_components(draft)
        components = list(
            DocumentComponent.objects.filter(document=draft)
            .prefetch_related("versions__source_bindings")
            .order_by("position", "id")
        )
    else:
        components = []
    interactions = []
    sources = {}
    for component in components:
        versions = sorted(component.versions.all(), key=lambda version: version.sequence)
        current = versions[-1] if versions else None
        for version in versions:
            if version.origin != "ai":
                continue
            version_sources = [binding_to_dict(binding) for binding in version.source_bindings.all()]
            for source in version_sources:
                identity = (
                    source.get("sourceKey", ""),
                    source.get("role", ""),
                    json.dumps(source.get("locator") or {}, sort_keys=True),
                )
                sources.setdefault(identity, source)
            interactions.append(
                {
                    "componentKey": component.stable_key,
                    "componentLabel": component.label,
                    "componentVersion": version.sequence,
                    "createdAt": version.created_at.isoformat(),
                    "instruction": version.instruction,
                    "isCurrentVersion": bool(current and current.pk == version.pk),
                    "paragraphs": [
                        {"index": index, "text": paragraph}
                        for index, paragraph in enumerate(_paragraphs(version.body), start=1)
                    ],
                    "sources": version_sources,
                }
            )

    session = getattr(draft, "session", None)
    matter = getattr(session, "matter", None)
    return {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "createdAt": timezone.now().isoformat(),
        "generator": "CLARA",
        "document": {
            "draftId": getattr(draft, "pk", None) or getattr(draft, "id", None),
            "title": getattr(draft, "title", ""),
            "matterExternalId": getattr(matter, "external_id", ""),
        },
        "aiInteractions": interactions,
        "sources": list(sources.values()),
    }


def ai_audit_counts(audit):
    interactions = audit.get("aiInteractions") or []
    return {
        "interactions": len(interactions),
        "paragraphs": sum(len(item.get("paragraphs") or []) for item in interactions),
        "sources": len(audit.get("sources") or []),
    }

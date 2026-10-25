"""Saving drafting work without losing anyone's edits.

Two tabs -- or two advocates -- can have the same session or document open.
Last write wins would quietly throw one of them away, so every save names the
revision it was made against, and a save against an older revision is refused
with the current state attached. The person whose save was refused keeps their
edits on screen and decides what to do; the server never merges prose.

Validation is recorded against the document revision it checked. Findings for
an older revision are stale, not wrong -- they describe text that has changed
since -- and a reader is told which.
"""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from apps.drafting import opposing_filing
from apps.drafting.models import DraftDocument, DraftingSession


class StaleRevision(Exception):
    """A save was based on a revision someone else has already replaced."""

    def __init__(self, current):
        super().__init__("This was changed in another window since you opened it.")
        self.current = current


def parse_revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("A save must say which revision it was made against (an integer `revision`).")
    return value


# What a checkpoint may change, by API name. Anything else in the body is
# ignored: a checkpoint saves choices, it never plans, generates, or advances.
CHECKPOINT_FIELDS = {
    "goal": ("goal", str),
    "instructions": ("instructions", str),
    "selectedFactIds": ("selected_fact_ids", list),
    "selectedCuratedFacts": ("selected_curated_facts", list),
    "selectedSourceResults": ("selected_source_results", list),
    "selectedBlockKeys": ("selected_block_keys", list),
    "selectedTemplateIds": ("selected_template_ids", list),
    "templateData": ("template_data", dict),
    "authorProfile": ("author_profile", dict),
    "workflowOptions": ("workflow_options", dict),
}

WORKFLOW_OPTION_KEYS = {
    "planningMode": {"suggest", "known"},
    "allowMultipleDocuments": {True, False},
    "clarifyMissingFactsBeforeDraft": {True, False},
}


def _clean_workflow_options(value):
    cleaned = {}
    for key, allowed in WORKFLOW_OPTION_KEYS.items():
        if key in value:
            if value[key] not in allowed:
                raise ValueError(f"workflowOptions.{key} has an unsupported value.")
            cleaned[key] = value[key]
    return cleaned


def checkpoint_session(session_id, payload):
    """Save the advocate's choices on a session, if nobody saved since.

    Only the fields in CHECKPOINT_FIELDS change. Returns the saved session;
    raises StaleRevision (carrying the current session) or ValueError.
    """
    expected = parse_revision(payload.get("revision"))
    updates = {}
    for api_name, (field, kind) in CHECKPOINT_FIELDS.items():
        if api_name not in payload:
            continue
        value = payload[api_name]
        if not isinstance(value, kind):
            raise ValueError(f"{api_name} must be a {kind.__name__}.")
        if api_name == "selectedTemplateIds":
            from apps.drafting.services import active_template_ids
            value = active_template_ids(value)
        if api_name == "workflowOptions":
            value = _clean_workflow_options(value)
        updates[field] = value
    with transaction.atomic():
        session = DraftingSession.objects.select_for_update().get(pk=session_id)
        if session.revision != expected:
            raise StaleRevision(session)
        for field, value in updates.items():
            setattr(session, field, value)
        if updates:
            session.save(update_fields=[*updates, "updated_at"])
    return session


def check_session_revision(session, payload):
    """For writes that may name a revision: refuse one made against an older save."""
    if "revision" not in payload:
        return
    expected = parse_revision(payload.get("revision"))
    current = DraftingSession.objects.filter(pk=session.pk).values_list("revision", flat=True).first()
    if current != expected:
        session.refresh_from_db()
        raise StaleRevision(session)


def locked_draft_at_revision(draft_id, payload):
    """The draft, locked for this transaction, if the save names its revision."""
    expected = parse_revision(payload.get("revision"))
    draft = _locked_draft_queryset().get(pk=draft_id)
    if draft.revision != expected:
        raise StaleRevision(draft)
    return draft


def _locked_draft_queryset():
    """Lock the draft row while eager-loading its nullable template relation.

    PostgreSQL rejects an unrestricted ``FOR UPDATE`` across a nullable
    ``select_related`` join. Limiting the lock to this queryset's base row
    keeps the eager load and protects the revision check.
    """
    return DraftDocument.objects.select_for_update(of=("self",)).select_related("session", "template")


def validation_state(draft):
    """Whether the stored findings describe the text as it is now."""
    if draft.validated_revision is None:
        state = "never"
    elif draft.validated_revision == draft.revision:
        state = "current"
    else:
        state = "stale"
    return {
        "state": state,
        "checkedRevision": draft.validated_revision,
        "validatedAt": draft.validated_at.isoformat() if draft.validated_at else "",
        "summary": draft.validation_summary or {},
    }


def record_validation(draft, summary):
    """Stamp the findings just stored with the revision they checked."""
    draft.refresh_from_db(fields=["revision", "validation_flags"])
    draft.validated_revision = draft.revision
    draft.validation_summary = summary or {}
    draft.validated_at = timezone.now()
    draft.save(update_fields=["validated_revision", "validation_summary", "validated_at", "updated_at"])
    return draft


def generation_inputs_digest(session):
    """A fingerprint of what a generation drafts from."""
    material = {
        "plan": session.draft_plan or {},
        "facts": session.selected_fact_ids or [],
        "curated": session.selected_curated_facts or [],
        "sources": session.selected_source_results or [],
        "blocks": session.selected_block_keys or [],
        "templates": session.selected_template_ids or [],
        "templateData": session.template_data or {},
        # A reply to a different opposition is a different draft.
        "opposingFiling": opposing_filing.fingerprint(session),
    }
    encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

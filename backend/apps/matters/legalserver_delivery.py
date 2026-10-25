"""Saving generated work product back to the LegalServer case file.

A document that only ever existed in a browser download is lost work, so
document delivery defaults on and the advocate opts out. A research answer or a
triage assessment is a working note that may not belong on the file, so those
default off and the advocate opts in.

Nothing here raises into the caller. An export must still hand the advocate
their document when LegalServer is unreachable; the delivery record carries the
failure so the UI can say the upload did not happen.
"""

import logging

from django.conf import settings

from apps.matters.legalserver_field_map import triage_outcome_updates
from apps.matters.models import LegalServerDelivery
from apps.sources.connectors.legalserver import (
    LegalServerClient,
    LegalServerError,
    legalserver_matter_write_id,
    legalserver_matter_uuid,
)


logger = logging.getLogger(__name__)

# Cases the advocate typed in by hand, and the seeded samples, have no matter in
# LegalServer to write to.
LOCAL_SOURCE_SYSTEMS = {"Manual", "Demo"}

REASON_LABELS = {
    "authorization_unavailable_or_revoked": "Your current case access or permission to save this kind of work could not be verified; nothing was uploaded.",
    "writes_disabled": "Writing to LegalServer is turned off on this server, so nothing was uploaded.",
    "not_configured": "LegalServer is not configured, so nothing was uploaded.",
    "local_case": "This case does not exist in LegalServer, so nothing was uploaded.",
    "no_matter_id": (
        "This case record has no LegalServer matter id, which the notes API needs to "
        "identify the matter. Re-sync the case from LegalServer and try again."
    ),
    "no_matter_uuid": (
        "This case record has no LegalServer matter UUID, which the write API needs to "
        "identify the matter. Re-sync the case from LegalServer and try again."
    ),
    "empty": "There was nothing to save.",
    "no_field_map": "No triage field map is configured, so no case properties were changed.",
    "field_map_error": "The triage field map could not be read, so no case properties were changed.",
    "no_updates": "The triage outcome matched no field-mapping rule, so no case properties were changed.",
    "field_map_disabled": "The triage field map is turned off, so no case properties were changed.",
}


def _remote_id(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "note_uuid", "uuid", "note_id", "document_id", "case_note_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)[:255]
    return ""


def _remote_url(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("url", "web_url", "download_url", "profile_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def delivery_defaults():
    """The default state of every "save to LegalServer" checkbox."""
    return {
        "documents": bool(getattr(settings, "LEGALSERVER_SAVE_DOCUMENTS_DEFAULT", True)),
        "research": bool(getattr(settings, "LEGALSERVER_SAVE_RESEARCH_DEFAULT", False)),
        "triage": bool(getattr(settings, "LEGALSERVER_SAVE_TRIAGE_DEFAULT", False)),
    }


def wants_delivery(body, key, *, default=None):
    """Read a save flag from a request body, falling back to the default.

    An absent flag means the caller did not offer the choice, which is not the
    same as the advocate clearing the box: only an explicit false opts out.
    """
    if default is None:
        default = delivery_defaults()[key]
    for name in ("saveToLegalServer", "save_to_legalserver"):
        if name in (body or {}):
            value = (body or {})[name]
            if isinstance(value, str):
                return value.strip().lower() not in ("", "0", "false", "no", "off")
            return bool(value)
    return bool(default)


def matter_uuid_for(matter):
    """The matter UUID, which document and case-property writes address."""
    return legalserver_matter_uuid(getattr(matter, "raw_payload", None) or {})


def matter_database_id_for(matter):
    """The numeric matter id, which the notes endpoint addresses.

    The two write endpoints disagree about how to name a matter: documents take
    the UUID, notes take this. Resolving them separately keeps a case that is
    missing one from silently sending the other.
    """
    return legalserver_matter_write_id(getattr(matter, "raw_payload", None) or {})


def can_deliver(matter, *, client=None):
    """Return (ok, reason) for whether this matter can be written to at all."""
    if not getattr(settings, "LEGALSERVER_ALLOW_WRITES", True):
        return False, "writes_disabled"
    if not matter:
        return False, "local_case"
    if (matter.source_system or "") in LOCAL_SOURCE_SYSTEMS:
        return False, "local_case"
    client = client or LegalServerClient()
    if not client.configured:
        return False, "not_configured"
    if not matter_uuid_for(matter) and not matter_database_id_for(matter):
        return False, "no_matter_uuid"
    return True, ""


def previous_delivery(matter, *, kind, scope_key):
    """The record this save should replace, if this session already made one."""
    if not scope_key:
        return None
    return (
        matter.legalserver_deliveries.filter(
            kind=kind, scope_key=scope_key, status=LegalServerDelivery.SAVED
        )
        .exclude(remote_id="")
        .first()
    )


def _record(matter, *, user, kind, origin, status, reason="", **extra):
    return LegalServerDelivery.objects.create(
        matter=matter,
        created_by=user if user and getattr(user, "is_authenticated", False) else None,
        kind=kind,
        origin=origin,
        status=status,
        reason=reason,
        **extra,
    )


def _skip(matter, *, user, kind, origin, reason, title="", filename="", scope_key="", request_payload=None):
    return _record(
        matter,
        user=user,
        kind=kind,
        origin=origin,
        status=LegalServerDelivery.SKIPPED,
        reason=reason,
        title=title[:500],
        filename=filename[:500],
        scope_key=scope_key[:255],
        request_payload=request_payload or {},
    )


def _write_authorized(user, matter, client, permission):
    from apps.matters.services import user_can_access_matter
    try:
        return bool(user and user.has_perm(f"matters.{permission}") and
                    user_can_access_matter(user, matter, client=client, require_remote=True))
    except Exception:
        logger.warning("LegalServer write authorization could not be verified", exc_info=True)
        return False


def save_case_note(
    matter,
    *,
    user,
    title,
    body,
    origin,
    requested=True,
    scope_key="",
    extra_fields=None,
    client=None,
):
    """Post a case note.

    Returns the delivery record, or None when the advocate opted out -- an
    opt-out is not an event worth keeping on the case. Never raises.
    """
    kind = LegalServerDelivery.CASENOTE
    title = (title or "").strip()
    body = (body or "").strip()
    if not requested:
        return None
    if not body:
        return _skip(matter, user=user, kind=kind, origin=origin, reason="empty", title=title)
    client = client or LegalServerClient()
    ok, reason = can_deliver(matter, client=client)
    if ok and not _write_authorized(user, matter, client, "write_legalserver_notes"):
        ok, reason = False, "authorization_unavailable_or_revoked"
    if not ok:
        return _skip(matter, user=user, kind=kind, origin=origin, reason=reason, title=title)
    database_id = matter_database_id_for(matter)
    if not database_id:
        return _skip(
            matter, user=user, kind=kind, origin=origin, reason="no_matter_id", title=title, scope_key=scope_key
        )

    earlier = previous_delivery(matter, kind=kind, scope_key=scope_key)
    request_payload = {"title": title, "characters": len(body), "scopeKey": scope_key}
    try:
        response = client.create_note(
            database_id,
            subject=title,
            body=body,
            external_id=scope_key,
            upsert=bool(earlier),
            extra_fields=extra_fields,
        )
    except LegalServerError as exc:
        logger.warning("LegalServer case note failed for matter %s: %s", matter.external_id, exc)
        return _record(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            status=LegalServerDelivery.FAILED,
            reason=str(exc)[:255],
            title=title[:500],
            request_payload=request_payload,
        )
    return _record(
        matter,
        user=user,
        kind=kind,
        origin=origin,
        status=LegalServerDelivery.SAVED,
        title=title[:500],
        scope_key=scope_key[:255],
        updated_existing=bool(earlier),
        remote_id=_remote_id(response),
        remote_url=_remote_url(response),
        request_payload=request_payload,
        response_payload=response if isinstance(response, dict) else {},
    )


def save_draft_ai_audit(draft, *, user, requested=True, client=None):
    """Create or update the one AI audit case note associated with a draft.

    A draft with no recorded AI component versions needs no case note.  The
    DOCX still carries an empty, versioned audit payload so downstream readers
    can distinguish "no AI recorded" from "metadata missing".
    """
    from apps.drafting.audit import draft_ai_audit
    from apps.matters.legalserver_notes import ai_audit_case_note

    audit = draft_ai_audit(draft)
    if not audit.get("aiInteractions"):
        return None
    return save_case_note(
        draft.session.matter,
        user=user,
        title=f"AI usage audit — {draft.title}"[:500],
        body=ai_audit_case_note(audit),
        origin="ai_audit",
        requested=requested,
        scope_key=f"ai-audit:draft:{draft.id}",
        client=client,
    )


def save_document(
    matter,
    *,
    user,
    filename,
    content,
    content_type="",
    title="",
    origin,
    requested=True,
    scope_key="",
    extra_fields=None,
    client=None,
):
    """Upload a generated document.

    Returns the delivery record, or None when the advocate opted out.
    """
    kind = LegalServerDelivery.DOCUMENT
    filename = (filename or "").strip() or "document.docx"
    title = (title or "").strip() or filename
    # LegalServer uses multipart `name` as the saved document filename.  The
    # file part's filename alone is not enough: giving `name` the human title
    # caused the case file to show an extensionless document even though the
    # bytes and MIME type were DOCX.
    remote_name = filename
    if not requested:
        return None
    if not content:
        return _skip(matter, user=user, kind=kind, origin=origin, reason="empty", title=title, filename=filename)
    client = client or LegalServerClient()
    ok, reason = can_deliver(matter, client=client)
    if ok and not _write_authorized(user, matter, client, "write_legalserver_documents"):
        ok, reason = False, "authorization_unavailable_or_revoked"
    if not ok:
        return _skip(matter, user=user, kind=kind, origin=origin, reason=reason, title=title, filename=filename)
    if not matter_uuid_for(matter):
        return _skip(
            matter, user=user, kind=kind, origin=origin, reason="no_matter_uuid", title=title, filename=filename
        )

    earlier = previous_delivery(matter, kind=kind, scope_key=scope_key)
    request_payload = {
        "filename": filename,
        "title": title,
        "bytes": len(content),
        "contentType": content_type,
        "scopeKey": scope_key,
        "remoteName": remote_name,
    }
    try:
        response = client.upload_matter_document(
            matter_uuid_for(matter),
            filename=filename,
            content=content,
            content_type=content_type,
            title=title,
            # Replace by the name we gave it last time, which may differ from
            # the name being sent now.
            replace_name=(earlier.request_payload.get("remoteName") or earlier.title if earlier else ""),
            extra_fields={**(extra_fields or {}), "name": remote_name},
        )
    except LegalServerError as exc:
        logger.warning("LegalServer document upload failed for matter %s: %s", matter.external_id, exc)
        return _record(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            status=LegalServerDelivery.FAILED,
            reason=str(exc)[:255],
            title=title[:500],
            filename=filename[:500],
            request_payload=request_payload,
        )
    return _record(
        matter,
        user=user,
        kind=kind,
        origin=origin,
        status=LegalServerDelivery.SAVED,
        title=title[:500],
        filename=filename[:500],
        scope_key=scope_key[:255],
        updated_existing=bool(earlier),
        remote_id=_remote_id(response),
        remote_url=_remote_url(response),
        request_payload=request_payload,
        response_payload=response if isinstance(response, dict) else {},
    )


def apply_triage_outcome(matter, assessment, *, user, origin="triage", client=None, slug=None, field_map=None):
    """Set case properties implied by a triage outcome.

    The mapping from an outcome to a site's fields is file-backed and ships
    turned off, so the usual result here is a recorded preview of what would be
    written rather than a write. That is deliberate: the hook is in place, and
    an office fills in its own field names before anything reaches a case.
    """
    kind = LegalServerDelivery.CASE_UPDATE
    title = f"Triage outcome: {assessment.priority_label or assessment.confidence or 'assessed'}"
    outcome = triage_outcome_updates(assessment, slug=slug, field_map=field_map)
    payload = outcome.as_payload()
    request_payload = {
        "fieldMap": outcome.slug,
        "matchedRules": outcome.matched_rules,
        "fields": payload,
        "assessmentId": assessment.id,
    }

    if outcome.error == "no_field_map":
        return _skip(matter, user=user, kind=kind, origin=origin, reason="no_field_map", title=title)
    if outcome.error:
        return _record(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            status=LegalServerDelivery.FAILED,
            reason=outcome.error[:255],
            title=title[:500],
            request_payload=request_payload,
        )
    if not payload:
        return _skip(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            reason="no_updates",
            title=title,
            request_payload=request_payload,
        )
    if not outcome.enabled:
        return _skip(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            reason="field_map_disabled",
            title=title,
            request_payload=request_payload,
        )
    if outcome.dry_run:
        # A dry run is a successful evaluation that deliberately stops short of
        # the write, so it is recorded with the values it would have sent.
        return _record(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            status=LegalServerDelivery.DRY_RUN,
            reason="dry_run",
            title=title[:500],
            request_payload=request_payload,
        )

    client = client or LegalServerClient()
    ok, reason = can_deliver(matter, client=client)
    if ok and not _write_authorized(user, matter, client, "write_legalserver_properties"):
        ok, reason = False, "authorization_unavailable_or_revoked"
    if not ok:
        return _record(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            status=LegalServerDelivery.SKIPPED,
            reason=reason,
            title=title[:500],
            request_payload=request_payload,
        )

    try:
        response = client.update_matter(matter_uuid_for(matter), payload)
    except LegalServerError as exc:
        logger.warning("LegalServer case update failed for matter %s: %s", matter.external_id, exc)
        return _record(
            matter,
            user=user,
            kind=kind,
            origin=origin,
            status=LegalServerDelivery.FAILED,
            reason=str(exc)[:255],
            title=title[:500],
            request_payload=request_payload,
        )
    return _record(
        matter,
        user=user,
        kind=kind,
        origin=origin,
        status=LegalServerDelivery.SAVED,
        title=title[:500],
        remote_id=_remote_id(response),
        request_payload=request_payload,
        response_payload=response if isinstance(response, dict) else {},
    )


def delivery_to_dict(delivery):
    if delivery is None:
        return None
    return {
        "id": delivery.id,
        "kind": delivery.kind,
        "origin": delivery.origin,
        "status": delivery.status,
        "reason": delivery.reason,
        "message": delivery_message(delivery),
        "title": delivery.title,
        "filename": delivery.filename,
        "scopeKey": delivery.scope_key,
        "updatedExisting": delivery.updated_existing,
        "remoteId": delivery.remote_id,
        "remoteUrl": delivery.remote_url,
        "fields": (delivery.request_payload or {}).get("fields") or {},
        "matchedRules": (delivery.request_payload or {}).get("matchedRules") or [],
        "createdAt": delivery.created_at.isoformat() if delivery.created_at else "",
    }


def delivery_message(delivery):
    """One sentence an advocate can act on."""
    label = {
        LegalServerDelivery.CASENOTE: "case note",
        LegalServerDelivery.DOCUMENT: "document",
        LegalServerDelivery.CASE_UPDATE: "case property update",
    }.get(delivery.kind, "record")
    if delivery.status == LegalServerDelivery.SAVED:
        if delivery.updated_existing:
            return f"Updated the {label} already on the LegalServer case file."
        return f"Saved the {label} to LegalServer."
    if delivery.status == LegalServerDelivery.DRY_RUN:
        return f"Previewed the {label}; the field map is in dry-run mode, so LegalServer was not changed."
    if delivery.status == LegalServerDelivery.FAILED:
        return f"Could not save the {label} to LegalServer: {delivery.reason}"
    return REASON_LABELS.get(delivery.reason, f"The {label} was not sent to LegalServer.")


def attach_delivery_headers(response, delivery, *, audit_delivery=None):
    """Report an upload's outcome alongside a binary download.

    The document itself is the response body, so the only place left to say
    whether it also reached LegalServer is a header the browser can read.
    """
    if delivery is None:
        return response
    response["X-LegalServer-Delivery"] = delivery.status
    message = delivery_message(delivery).encode("ascii", "replace").decode("ascii")
    response["X-LegalServer-Delivery-Message"] = message[:300]
    if audit_delivery is not None:
        response["X-LegalServer-AI-Audit"] = audit_delivery.status
        audit_message = delivery_message(audit_delivery).encode("ascii", "replace").decode("ascii")
        response["X-LegalServer-AI-Audit-Message"] = audit_message[:300]
    return response


def deliveries_for_matter(matter, *, limit=25):
    return list(matter.legalserver_deliveries.all()[:limit])

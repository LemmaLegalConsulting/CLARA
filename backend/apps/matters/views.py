import json
import mimetypes
import re

from django.conf import settings
from django.db.models import prefetch_related_objects
from django.http import HttpResponse, JsonResponse
from django.utils.http import content_disposition_header

from apps.ai.case_chat import case_chat_reply
from apps.ai.chat_history import (
    append_message,
    archive_current_conversation,
    clear_messages,
    conversation_list,
    current_conversation,
    is_current_thread,
    messages_for_user,
    read_conversation,
)
from apps.ai.models import ChatConversation
from apps.core.http import allow_document_framing, api_login_required
from apps.core.http import json_body
from apps.matters.document_context import (
    case_materials_payload,
    chunk_text,
    custom_fields_inventory,
    document_to_public_dict,
    get_case_document,
    get_case_documents_with_status,
    get_document_file,
    get_document_text,
    search_chunks,
    summarize_text,
)
from apps.matters import case_list
from apps.matters import services as matter_services
from apps.matters.legalserver_delivery import (
    REASON_LABELS,
    apply_triage_outcome,
    can_deliver,
    deliveries_for_matter,
    delivery_defaults,
    delivery_to_dict,
    save_case_note,
    save_document,
    wants_delivery,
)
from apps.matters.legalserver_notes import triage_case_note_body
from apps.matters.models import MatterFact, TriageRubric
from apps.matters.route_aliases import is_remote_lookup_key, resolve_matter_route_key
from apps.matters.seed import seed_matters
from apps.matters.serializers import fact_to_dict, matter_to_dict, triage_assessment_to_dict, triage_rubric_to_dict
from apps.matters.services import (
    DEMO_SOURCE_SYSTEM,
    create_manual_matter_for_user,
    create_legalserver_draft_intake_from_manual_matter,
    legalserver_account_status,
    local_matters_for_user,
    matter_for_user,
    sync_legalserver_matter,
    sync_legalserver_matters_for_user,
    update_manual_matter_for_user,
)
from apps.matters.triage import ensure_default_triage_rubric, run_triage
from apps.sources.document_text import DocumentExtractionError, extract_text
from apps.sources.connectors.legalserver import UUID_RE, LegalServerError
from apps.sources.models import UserSourceIdentity


def _stale_thread():
    return JsonResponse(
        {
            "error": "This conversation is no longer the current one -- a new chat was started in another window. Open the current chat to continue.",
            "conflict": "thread",
        },
        status=409,
    )


def _matter_or_404(user, matter_id):
    matter = matter_for_user(user, matter_id)
    if matter:
        return matter, None
    return None, JsonResponse({"error": "Case not found or not available to this user"}, status=404)


@api_login_required
def triage_rubrics(request):
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    ensure_default_triage_rubric()
    rubrics = TriageRubric.objects.filter(active=True)
    return JsonResponse({"rubrics": [triage_rubric_to_dict(rubric) for rubric in rubrics]})


@api_login_required
def cases(request):
    if request.method == "POST":
        return create_manual_case(request)
    if request.method != "GET":
        return JsonResponse({"error": "GET or POST required"}, status=405)

    query = request.GET.get("q", "").strip()
    status = case_list.normalize_status(request.GET.get("status"), searching=bool(query))
    assigned = case_list.normalize_assigned(request.GET.get("assigned"))
    problem_code = request.GET.get("problem", "").strip()
    sort = case_list.normalize_sort(request.GET.get("sort"))
    limit = case_list.normalize_page_size(request.GET.get("limit"))
    offset = case_list.normalize_offset(request.GET.get("offset"))

    legalserver_client = matter_services.LegalServerClient()
    sync = sync_legalserver_matters_for_user(
        request.user,
        query=query,
        limit=case_list.SYNC_FETCH_LIMIT,
        restrict_to_user=not bool(query),
        client=legalserver_client,
    )
    if settings.ENABLE_DEMO_MATTERS and not sync.matters:
        seed_matters()
    local_matters = [] if query else local_matters_for_user(request.user)
    matters_by_id = {matter.external_id: matter for matter in [*local_matters, *sync.matters]}
    if settings.ENABLE_DEMO_MATTERS:
        # Sample data supplements what the user can already reach; it never
        # stands in for the access check on real cases.
        for matter in demo_matters():
            matters_by_id.setdefault(matter.external_id, matter)
    matters = list(matters_by_id.values())
    prefetch_related_objects(matters, "route_aliases")
    account = legalserver_account_status(request.user, client=legalserver_client)
    serialized = [
        matter_to_dict(
            matter,
            legalserver_client=legalserver_client,
            viewer=request.user,
            viewer_identifier=account.get("identifier", ""),
        )
        for matter in matters
    ]
    # Facets describe everything the viewer can reach, not the current page, so
    # the filter does not lose the option that would widen the list again.
    problem_codes = case_list.legal_problem_options(serialized)
    matched = case_list.sort_cases(
        case_list.filter_cases(serialized, status=status, assigned=assigned, problem_code=problem_code),
        sort=sort,
    )
    page, has_more = case_list.paginate(matched, limit=limit, offset=offset)
    return JsonResponse(
        {
            "cases": page,
            "total": len(matched),
            "hasMore": has_more,
            "problemCodes": problem_codes,
            "filters": {
                "q": query,
                "status": status,
                "assigned": assigned,
                "problem": problem_code,
                "sort": sort,
                "limit": limit,
                "offset": offset,
            },
            "legalserver": {
                **account,
                "syncError": sync.error,
            },
        }
    )


def _request_value(request, key, default=""):
    if request.content_type and request.content_type.startswith("multipart/"):
        return request.POST.get(key, default)
    return json_body(request).get(key, default)


def _create_fact_from_upload(matter, upload):
    extracted = extract_text(upload.read(), filename=upload.name, content_type=upload.content_type or "")
    return _create_case_fact(
        matter,
        title=upload.name,
        text=extracted["text"],
        source_label=f"Uploaded document: {upload.name}",
    )


def create_manual_case(request):
    if not (request.content_type and request.content_type.startswith("multipart/")):
        body = json_body(request)
        notes = body.get("notes") or body.get("summary") or ""
        if not notes.strip():
            return JsonResponse({"error": "Add intake notes or upload at least one file"}, status=400)
        matter = create_manual_matter_for_user(
            request.user,
            client_name=body.get("clientName") or body.get("client_name") or "",
            matter_type=body.get("matterType") or body.get("matter_type") or "",
            jurisdiction=body.get("jurisdiction") or "",
            posture=body.get("posture") or "",
            summary=notes,
        )
        created = []
        fact = _create_case_fact(matter, title="Intake notes", text=notes, source_label="Typed intake notes")
        if fact:
            created.append(fact)
        matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)
        return JsonResponse(
            {
                "case": matter_to_dict(matter, include_facts=True),
                "created": [fact_to_dict(fact) for fact in created],
            },
            status=201,
        )

    notes = _request_value(request, "notes")
    uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not notes.strip() and not uploads:
        return JsonResponse({"error": "Add intake notes or upload at least one file"}, status=400)

    matter = create_manual_matter_for_user(
        request.user,
        client_name=_request_value(request, "clientName") or _request_value(request, "client_name"),
        matter_type=_request_value(request, "matterType") or _request_value(request, "matter_type"),
        jurisdiction=_request_value(request, "jurisdiction"),
        posture=_request_value(request, "posture"),
        summary=notes,
    )
    created = []
    fact = _create_case_fact(matter, title="Intake notes", text=notes, source_label="Typed intake notes")
    if fact:
        created.append(fact)
    try:
        for upload in uploads:
            upload_fact = _create_fact_from_upload(matter, upload)
            if upload_fact:
                created.append(upload_fact)
    except DocumentExtractionError as exc:
        matter.delete()
        return JsonResponse({"error": str(exc)}, status=400)

    matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)
    return JsonResponse(
        {
            "case": matter_to_dict(matter, include_facts=True),
            "created": [fact_to_dict(fact) for fact in created],
        },
        status=201,
    )


def demo_matters():
    from apps.matters.models import Matter

    return Matter.objects.filter(source_system=DEMO_SOURCE_SYSTEM)


@api_login_required
def legalserver_account(request):
    if request.method == "GET":
        return JsonResponse({"legalserver": legalserver_account_status(request.user)})
    if request.method not in ("POST", "PATCH", "DELETE"):
        return JsonResponse({"error": "GET, POST, PATCH, or DELETE required"}, status=405)

    if request.method == "DELETE":
        UserSourceIdentity.objects.filter(user=request.user, provider="legalserver").update(enabled=False)
        return JsonResponse({"legalserver": legalserver_account_status(request.user)})

    return JsonResponse({"error": "Ask an administrator to verify and configure your LegalServer identity."}, status=403)


@api_login_required
def case_detail(request, matter_id):
    matter = matter_for_user(request.user, matter_id)
    if not matter:
        sync_legalserver_matter(matter_id, user=request.user)
        matter = matter_for_user(request.user, matter_id)
    if not matter and settings.ENABLE_DEMO_MATTERS:
        seed_matters()
        matter = matter_for_user(request.user, matter_id)
    if not matter:
        return JsonResponse({"error": "Case not found or not available to this user"}, status=404)
    if request.method == "GET":
        matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)
        return JsonResponse({"case": matter_to_dict(matter, include_facts=True)})
    if request.method == "PATCH":
        try:
            matter = update_manual_matter_for_user(matter, request.user, json_body(request))
        except PermissionError as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)
        return JsonResponse({"case": matter_to_dict(matter, include_facts=True)})
    if request.method == "POST":
        body = json_body(request)
        if body.get("action") != "legalserver_draft_intake":
            return JsonResponse({"error": "Unsupported case action"}, status=400)
        try:
            preview = create_legalserver_draft_intake_from_manual_matter(matter, request.user)
        except PermissionError as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        return JsonResponse({"legalserverDraftIntake": preview})
    return JsonResponse({"error": "GET, PATCH, or POST required"}, status=405)


def _legalserver_account_reason(user):
    """Why this account could not reach any LegalServer case, if that is why.

    It describes the viewer, never the case: an account with no LegalServer
    connection gets the same reason for every case number, real or not, so it
    reveals nothing about which cases exist -- and it tells the advocate the
    one thing they can fix, instead of suggesting the case is someone else's.
    """
    client = matter_services.LegalServerClient()
    if not client.configured:
        return ""
    profile = matter_services.legalserver_access_profile_for_user(user, client=client)
    if not profile.identifier:
        return "legalserver_not_connected"
    if profile.error == "identity_mismatch":
        return "legalserver_identity_mismatch"
    return ""


@api_login_required
def case_by_route_key(request):
    """Open the case a URL names by its readable case number.

    Read-only: it resolves, checks access, and serializes. A key that names no
    case and a key that names someone else's case answer the same 404, so a
    guessed URL never confirms that a case exists.
    """
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    route_key = request.GET.get("key", "").strip()
    if not route_key:
        return JsonResponse({"error": "A case key is required"}, status=400)
    matter = resolve_matter_route_key(request.user, route_key)
    if not matter and is_remote_lookup_key(route_key):
        # Not imported here yet -- a link to a case this browser never listed.
        # LegalServer fetches a matter only by UUID ("Invalid matter_uuid" for
        # a case number), so a readable key is found by search, through the
        # same access-filtered sync as the case list. Importing it records its
        # alias, so the second lookup can find it.
        sync_legalserver_matters_for_user(request.user, query=route_key, limit=5, restrict_to_user=False)
        matter = resolve_matter_route_key(request.user, route_key)
        if not matter and UUID_RE.match(route_key):
            sync_legalserver_matter(route_key, user=request.user)
            matter = resolve_matter_route_key(request.user, route_key)
    if not matter and settings.ENABLE_DEMO_MATTERS:
        seed_matters()
        matter = resolve_matter_route_key(request.user, route_key)
    if not matter:
        payload = {"error": "Case not found or not available to this user"}
        reason = _legalserver_account_reason(request.user)
        if reason:
            payload["reason"] = reason
        return JsonResponse(payload, status=404)
    matter = matter.__class__.objects.prefetch_related("facts", "route_aliases").get(id=matter.id)
    return JsonResponse({"case": matter_to_dict(matter, include_facts=True)})


@api_login_required
def case_legalserver(request, matter_id):
    """Report whether this case can be written to, and what has been sent."""
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    can_save, reason = can_deliver(matter)
    return JsonResponse(
        {
            "canSave": can_save,
            "reason": reason,
            "message": REASON_LABELS.get(reason, ""),
            "defaults": delivery_defaults(),
            "deliveries": [delivery_to_dict(delivery) for delivery in deliveries_for_matter(matter)],
        }
    )


@api_login_required
def case_legalserver_casenote(request, matter_id):
    """Save a case note. Used by research, triage, and case chat."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    body = json_body(request)
    delivery = save_case_note(
        matter,
        user=request.user,
        title=str(body.get("title") or "Note from the drafting tool"),
        body=str(body.get("body") or body.get("note") or ""),
        origin=str(body.get("origin") or "manual")[:60],
        # A scope key names the artifact rather than the moment, so saving the
        # same chat thread twice updates one note instead of leaving two.
        scope_key=str(body.get("scopeKey") or "")[:255],
        requested=True,
    )
    return JsonResponse({"delivery": delivery_to_dict(delivery)}, status=201)


@api_login_required
def case_legalserver_document(request, matter_id):
    """Upload a document the advocate is holding in the browser."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"error": "A file is required"}, status=400)
    delivery = save_document(
        matter,
        user=request.user,
        filename=upload.name,
        content=upload.read(),
        content_type=upload.content_type or "",
        title=str(request.POST.get("title") or upload.name),
        origin=str(request.POST.get("origin") or "manual")[:60],
        scope_key=str(request.POST.get("scopeKey") or "")[:255],
        requested=True,
    )
    return JsonResponse({"delivery": delivery_to_dict(delivery)}, status=201)


@api_login_required
def case_documents(request, matter_id):
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    documents, problem = get_case_documents_with_status(matter)
    return JsonResponse(
        {"documents": [document_to_public_dict(document) for document in documents], "documentsUnavailable": problem}
    )


@api_login_required
def case_materials(request, matter_id):
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)
    return JsonResponse(
        case_materials_payload(
            matter,
            force_refresh=request.GET.get("refresh", "").strip().lower() in {"1", "true", "yes"},
        )
    )


@api_login_required
def case_custom_fields(request, matter_id):
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse({"fields": custom_fields_inventory(matter)})
    if request.method != "POST":
        return JsonResponse({"error": "GET or POST required"}, status=405)
    body = json_body(request)
    field_keys = set(body.get("fieldKeys") or [])
    if not field_keys:
        return JsonResponse({"error": "Select at least one custom field"}, status=400)
    try:
        from apps.ai.case_chat import refresh_matter_payload

        refresh_matter_payload(matter)
    except Exception:
        pass
    fields = custom_fields_inventory(matter)
    selected = [field for field in fields if field["key"] in field_keys]
    raw_payload = matter.raw_payload or {}
    raw_payload["custom_fields_normalized"] = fields
    matter.raw_payload = raw_payload
    matter.save(update_fields=["raw_payload", "updated_at"])
    return JsonResponse(
        {
            "fields": selected,
            "errors": [] if selected else ["Selected custom fields were not returned by LegalServer."],
        }
    )


@api_login_required
def case_document_context(request, matter_id, document_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    document = get_case_document(matter, document_id)
    if not document:
        return JsonResponse({"error": "Document not found"}, status=404)

    body = json_body(request)
    level = body.get("level", "summary")
    text = get_document_text(document)
    chunks = chunk_text(text)
    payload = {"document": document_to_public_dict(document)}
    if level == "full":
        payload["text"] = text
        payload["summary"] = summarize_text(text)
        payload["chunks"] = chunks
    elif level == "chunks":
        payload["summary"] = summarize_text(text)
        payload["chunks"] = chunks
    elif level == "search":
        payload["summary"] = summarize_text(text)
        payload["chunks"] = search_chunks(chunks, body.get("query", ""), limit=body.get("limit", 5))
    else:
        payload["summary"] = summarize_text(text)
    return JsonResponse(payload)


@api_login_required
def case_document_file(request, matter_id, document_id):
    if request.method != "GET":
        return JsonResponse({"error": "GET required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    document = get_case_document(matter, document_id)
    if not document:
        return JsonResponse({"error": "Document not found"}, status=404)
    try:
        downloaded = get_document_file(document)
    except LegalServerError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    filename = document.get("filename") or downloaded.get("filename") or "case-document"
    # Neither the upstream MIME header nor a filename proves a safe preview.
    content = downloaded["content"]
    is_pdf = content.startswith(b"%PDF-")
    response = HttpResponse(content, content_type="application/pdf" if is_pdf else "application/octet-stream")
    response["Content-Disposition"] = content_disposition_header(not is_pdf, filename)
    response["X-Content-Type-Options"] = "nosniff"
    allow_document_framing(response)
    if not is_pdf:
        response["Content-Security-Policy"] += "; sandbox; default-src 'none'"
    return response


def _fact_slug(matter, title):
    base = re.sub(r"[^a-z0-9]+", "-", (title or "added fact").lower()).strip("-") or "added-fact"
    slug = base
    index = 2
    while MatterFact.objects.filter(matter=matter, slug=slug).exists():
        slug = f"{base}-{index}"
        index += 1
    return slug


def _fact_title(text, fallback):
    first_line = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    if not first_line:
        return fallback
    return first_line[:80]


def _create_case_fact(matter, *, title, text, source_label):
    text = (text or "").strip()
    if not text:
        return None
    title = (title or "").strip() or _fact_title(text, "Added fact")
    return MatterFact.objects.create(
        matter=matter,
        slug=_fact_slug(matter, title),
        title=title,
        text=text,
        source_label=source_label or "Added during drafting",
        confidence="user_added",
        selected_by_default=False,
    )


@api_login_required
def case_facts(request, matter_id):
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error

    if request.method == "GET":
        return JsonResponse({"facts": [fact_to_dict(fact) for fact in matter.facts.all()]})

    if request.method != "POST":
        return JsonResponse({"error": "GET or POST required"}, status=405)

    created = []
    if request.content_type and request.content_type.startswith("multipart/"):
        upload = request.FILES.get("file")
        if not upload:
            return JsonResponse({"error": "Upload a document file"}, status=400)
        try:
            extracted = extract_text(upload.read(), filename=upload.name, content_type=upload.content_type or "")
        except DocumentExtractionError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        fact = _create_case_fact(
            matter,
            title=request.POST.get("title") or upload.name,
            text=extracted["text"],
            source_label=f"Uploaded document: {upload.name}",
        )
        if fact:
            created.append(fact)
    else:
        body = json_body(request)
        fact = _create_case_fact(
            matter,
            title=body.get("title") or "",
            text=body.get("text") or "",
            source_label=body.get("source") or "Typed during drafting",
        )
        if fact:
            created.append(fact)

    if not created:
        return JsonResponse({"error": "Fact text is required"}, status=400)

    matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)
    return JsonResponse(
        {
            "facts": [fact_to_dict(fact) for fact in matter.facts.all()],
            "created": [fact_to_dict(fact) for fact in created],
            "case": matter_to_dict(matter, include_facts=True),
        },
        status=201,
    )


@api_login_required
def case_fact_recommendations(request, matter_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)

    from apps.ai.services import drafting_ai

    recommended_slugs = set(drafting_ai.recommend_fact_slugs(matter))
    recommended = [
        fact
        for fact in matter.facts.all()
        if fact.slug in recommended_slugs or fact.selected_by_default
    ]
    return JsonResponse(
        {
            "factIds": [fact.id for fact in recommended],
            "facts": [fact_to_dict(fact) for fact in recommended],
        }
    )


@api_login_required
def case_triage(request, matter_id):
    if request.method not in ("GET", "POST"):
        return JsonResponse({"error": "GET or POST required"}, status=405)
    matter, error = _matter_or_404(request.user, matter_id)
    if error:
        return error
    matter = matter.__class__.objects.prefetch_related("facts").get(id=matter.id)

    if request.method == "GET":
        return JsonResponse(
            {
                "assessments": [
                    triage_assessment_to_dict(assessment)
                    for assessment in matter.triage_assessments.select_related("rubric").all()
                ]
            }
        )

    body = {}
    if request.content_type and request.content_type.startswith("application/json") and request.body:
        body = json_body(request)
    rubric = None
    rubric_id = body.get("rubricId") or body.get("rubric_id")
    rubric_slug = body.get("rubricSlug") or body.get("rubric_slug")
    if rubric_id:
        rubric = TriageRubric.objects.filter(id=rubric_id, active=True).first()
    elif rubric_slug:
        rubric = TriageRubric.objects.filter(slug=rubric_slug, active=True).first()
    else:
        rubric = ensure_default_triage_rubric()
    if not rubric:
        return JsonResponse({"error": "Active triage rubric not found"}, status=404)

    assessment = run_triage(matter, rubric=rubric, user=request.user)

    # A triage assessment is a working judgment, so the case note is opt-in.
    # The case-property update is governed by the field map instead of a
    # checkbox: until an office fills the map in, it evaluates to a preview.
    note = None
    if wants_delivery(body, "triage"):
        note = save_case_note(
            matter,
            user=request.user,
            title=f"AI triage: {assessment.priority_label or assessment.confidence or rubric.name}",
            body=triage_case_note_body(assessment),
            origin="triage",
        )
    case_update = None
    if body.get("applyCaseProperties", True):
        case_update = apply_triage_outcome(matter, assessment, user=request.user)
    return JsonResponse(
        {
            "assessment": triage_assessment_to_dict(assessment),
            "legalserver": {
                "casenote": delivery_to_dict(note),
                "caseUpdate": delivery_to_dict(case_update),
            },
        },
        status=201,
    )


@api_login_required
def case_chat(request, matter_id):
    if request.method not in {"GET", "POST", "DELETE"}:
        return JsonResponse({"error": "GET, POST, or DELETE required"}, status=405)
    matter = matter_for_user(request.user, matter_id)
    if not matter:
        sync_legalserver_matter(matter_id, user=request.user)
        matter = matter_for_user(request.user, matter_id)
    if not matter:
        return JsonResponse({"error": "Case not found or not available to this user"}, status=404)
    scope_key = str(matter.id)
    chat = {"user": request.user, "kind": ChatConversation.CASE, "scope_key": scope_key}
    if request.method == "GET":
        # Reading never creates a conversation, and a thread named in the URL
        # is that thread or a 404 -- never the current one standing in for it.
        try:
            conversation, messages = read_conversation(**chat, conversation_id=request.GET.get("threadId") or None)
        except LookupError:
            return JsonResponse({"error": "Chat thread not found"}, status=404)
        current = current_conversation(**chat)
        return JsonResponse(
            {
                "messages": messages,
                "threadId": conversation.id if conversation else None,
                "currentThreadId": current.id if current else None,
                "threads": conversation_list(**chat),
            }
        )
    if request.method == "DELETE":
        if not is_current_thread(**chat, thread_id=request.GET.get("threadId")):
            return _stale_thread()
        clear_messages(**chat)
        return JsonResponse({"ok": True})
    body = json.loads(request.body.decode("utf-8") or "{}")
    if body.get("action") != "new_thread" and not is_current_thread(**chat, thread_id=body.get("threadId")):
        return _stale_thread()
    if body.get("action") == "new_thread":
        archive_current_conversation(user=request.user, kind=ChatConversation.CASE, scope_key=scope_key)
        return JsonResponse({"messages": [], "threads": conversation_list(user=request.user, kind=ChatConversation.CASE, scope_key=scope_key)})
    content = str(body.get("content") or "").strip()
    if not content:
        supplied = body.get("messages") or []
        content = str(supplied[-1].get("content") or "").strip() if supplied else ""
    if not content:
        return JsonResponse({"error": "A chat message is required"}, status=400)
    history = messages_for_user(user=request.user, kind=ChatConversation.CASE, scope_key=scope_key)
    reply = case_chat_reply(matter=matter, messages=[*history, {"role": "user", "content": content}])
    append_message(user=request.user, kind=ChatConversation.CASE, scope_key=scope_key, role="user", content=content)
    append_message(
        user=request.user,
        kind=ChatConversation.CASE,
        scope_key=scope_key,
        role="assistant",
        content=reply["message"],
        metadata={"toolsUsed": reply.get("toolsUsed", []), "actions": reply.get("actions", [])},
    )
    current = current_conversation(**chat)
    return JsonResponse({**reply, "threadId": current.id if current else None})

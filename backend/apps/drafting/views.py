import re

from django.db import transaction
from django.http import JsonResponse

from apps.core.http import api_login_required, json_body, method_not_allowed
from apps.drafting import operations, opposing_filing
from apps.drafting.components import component_history, record_sections
from apps.drafting.generation_jobs import fail_if_stalled, job_to_dict, start_job
from apps.drafting.models import DraftDocument, DraftingSession, DraftGenerationJob
from apps.drafting.operations import operation_to_dict
from apps.drafting.packages import derive_relationships, package_payload
from apps.drafting.revisions import (
    StaleRevision,
    check_session_revision,
    checkpoint_session,
    locked_draft_at_revision,
    record_validation,
)
from apps.drafting.resume import (
    WORKSPACE_MODES,
    page_bounds,
    resume_payload,
    session_in_route,
    session_summary,
    sessions_for_matter,
)
from apps.drafting.serializers import draft_to_dict, session_to_dict
from apps.drafting.services import (
    advance,
    active_template_ids,
    apply_plan_edits,
    create_draft,
    create_or_update_plan,
    initialize_session,
    unanswered_missing_information,
    outline_for_session,
    recommend_goal_candidates,
    recommend_session_fact_ids,
    recommend_support_candidates,
    regenerate_draft_block,
    template_for_reference,
)
from apps.exporting.services import draft_export_filename, export_document
from apps.matters.legalserver_delivery import (
    attach_delivery_headers,
    save_document,
    save_draft_ai_audit,
    wants_delivery,
)
from apps.matters.models import MatterFact
from apps.matters.serializers import fact_to_dict, matter_to_dict
from apps.matters.route_aliases import resolve_matter_route_key
from apps.matters.services import accessible_matters_for_user, matter_for_user, user_can_access_matter
from apps.templates_app.models import DocumentTemplate
from apps.validation.repair import validate_with_auto_repair
from apps.validation.revision import apply_revision_plan, build_revision_plan
from apps.validation.services import validate_document


SHELL_TEMPLATE_SLUG = "novel-motion-shell"


def _session_or_404(user, session_id, *, with_template=False, case_key="", workspace=""):
    queryset = DraftingSession.objects.select_related("matter", "template")
    if with_template:
        queryset = queryset.prefetch_related("template__blocks")
    session = queryset.filter(id=session_id).first()
    # A session on another case, or in another workspace, answers exactly as a
    # missing one: a URL must not reveal which case a session belongs to.
    if (
        not session
        or not user_can_access_matter(user, session.matter)
        or not session_in_route(user, session, case_key=case_key, workspace=workspace)
    ):
        return None, JsonResponse({"error": "Drafting session not found"}, status=404)
    if session.mode == "template_fill":
        return None, JsonResponse({"error": "Use the Fill template workspace for this session."}, status=400)
    return session, None


def _draft_or_404(user, draft_id, *, allow_fill=False):
    draft = DraftDocument.objects.select_related("session", "session__matter", "session__template").filter(id=draft_id).first()
    if not draft or not user_can_access_matter(user, draft.session.matter):
        return None, JsonResponse({"error": "Draft not found"}, status=404)
    if draft.session.mode == "template_fill" and not allow_fill:
        return None, JsonResponse({"error": "Edit this document in Fill template or download it for Word."}, status=400)
    return draft, None


def _session_conflict(stale):
    """409 with what is saved now, so the refused tab can compare and choose."""
    return JsonResponse(
        {
            "error": str(stale),
            "conflict": "session",
            "currentRevision": stale.current.revision,
            "session": session_to_dict(stale.current),
        },
        status=409,
    )


def _draft_conflict(stale):
    return JsonResponse(
        {
            "error": str(stale),
            "conflict": "draft",
            "currentRevision": stale.current.revision,
            "draft": draft_to_dict(stale.current),
        },
        status=409,
    )


def _advance_or_400(session, payload):
    try:
        session = advance(session, payload)
        # Session setup runs inside the same guard: it reads the template the
        # payload just selected, so an unusable reference must not escape as a 500.
        return initialize_session(session), None
    except ValueError as exc:
        return None, JsonResponse({"error": str(exc)}, status=400)


@api_login_required
def sessions(request):
    if request.method == "GET" and (request.GET.get("caseKey") or request.GET.get("matterId")):
        return _session_summaries(request)
    if request.method == "GET":
        accessible_ids = [matter.id for matter in accessible_matters_for_user(request.user)]
        sessions = DraftingSession.objects.select_related("matter", "template").filter(matter_id__in=accessible_ids)
        return JsonResponse({"sessions": [session_to_dict(session) for session in sessions]})
    if request.method != "POST":
        return method_not_allowed(["GET", "POST"])

    body = json_body(request)
    if body.get("mode") == "template_fill":
        return JsonResponse({"error": "Use /api/template-fill/start/ for this mode."}, status=400)
    matter = matter_for_user(request.user, body.get("matterId", ""))
    if not matter:
        return JsonResponse({"error": "Case not found or not available to this user"}, status=404)
    template = None
    if body.get("templateId"):
        template = template_for_reference(body["templateId"])
        if not template:
            return JsonResponse({"error": "Selected template was not found."}, status=404)
    elif body.get("mode") == "draft_from_scratch":
        template = DocumentTemplate.objects.filter(slug=SHELL_TEMPLATE_SLUG).first()
        if not template:
            return JsonResponse(
                {"error": "The drafting shell template is not seeded yet. Load the app bootstrap first."},
                status=503,
            )

    try:
        selected_ids = active_template_ids(body.get("selectedTemplateIds", [template.pk] if template else []))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    session = DraftingSession.objects.create(
        mode=body.get("mode", "draft_from_template"),
        matter=matter,
        template=template,
        author_profile=body.get("authorProfile", {}),
        template_data=body.get("templateData", {}),
        goal=body.get("goal", ""),
        selected_template_ids=selected_ids,
        instructions=body.get("instructions", ""),
    )
    initialize_session(session)
    return JsonResponse({"session": session_to_dict(session)}, status=201)


def _session_summaries(request):
    """One case's saved sessions, newest first, as light rows for a list screen."""
    case_key = request.GET.get("caseKey", "").strip()
    matter = (
        resolve_matter_route_key(request.user, case_key)
        if case_key
        else matter_for_user(request.user, request.GET.get("matterId", "").strip())
    )
    if not matter:
        return JsonResponse({"error": "Case not found or not available to this user"}, status=404)
    workspace = request.GET.get("workspace", "drafting")
    modes = WORKSPACE_MODES.get(workspace)
    if modes is None:
        return JsonResponse({"error": f"Unknown workspace {workspace!r}."}, status=400)
    mode = request.GET.get("mode", "").strip()
    if mode:
        modes = {mode} & modes
    limit, offset = page_bounds(request.GET.get("limit"), request.GET.get("offset"))
    page, total = sessions_for_matter(matter, modes=modes, limit=limit, offset=offset)
    return JsonResponse(
        {
            "sessions": [session_summary(session) for session in page],
            "total": total,
            "hasMore": offset + len(page) < total,
        }
    )


@api_login_required
def session_detail(request, session_id):
    """A saved session, read-only, with where reopening it should land.

    `caseKey` and `workspace` name the URL the session was opened from; when
    given, the session must belong to that case and that workspace.
    """
    if request.method not in {"GET", "PATCH"}:
        return method_not_allowed(["GET", "PATCH"])
    session, error = _session_or_404(
        request.user,
        session_id,
        case_key=request.GET.get("caseKey", "").strip(),
        workspace=request.GET.get("workspace", "").strip(),
    )
    if error:
        return error
    if request.method == "PATCH":
        # A checkpoint: saves the advocate's choices and nothing else. It never
        # plans, recommends, initializes, or advances the session.
        try:
            session = checkpoint_session(session.id, json_body(request))
        except StaleRevision as stale:
            return _session_conflict(stale)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    session = DraftingSession.objects.select_related("matter", "template").prefetch_related("matter__facts", "template__blocks").get(id=session.id)
    return JsonResponse({"session": session_to_dict(session), "resume": resume_payload(session)})


@api_login_required
def advance_session(request, session_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    session, error = _session_or_404(request.user, session_id)
    if error:
        return error
    body = json_body(request)
    try:
        check_session_revision(session, body)
    except StaleRevision as stale:
        return _session_conflict(stale)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    session, error = _advance_or_400(session, body)
    if error:
        return error
    return JsonResponse({"session": session_to_dict(session)})


@api_login_required
def recommend_session_facts(request, session_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    body = json_body(request)
    fact_ids = recommend_session_fact_ids(session)
    facts = MatterFact.objects.filter(id__in=fact_ids).order_by("id")
    if body.get("apply", True):
        session.selected_fact_ids = fact_ids
        session.save(update_fields=["selected_fact_ids", "updated_at"])
    matter = session.matter.__class__.objects.prefetch_related("facts").get(id=session.matter.id)
    return JsonResponse(
        {
            "factIds": fact_ids,
            "facts": [fact_to_dict(fact) for fact in facts],
            "case": matter_to_dict(matter, include_facts=True),
            "session": session_to_dict(session),
            "guidance": "Suggested facts are preselected from the template, case facts, notes, and document text. Review them before continuing.",
        }
    )


@api_login_required
def recommend_session_goals(request, session_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    recommendations = recommend_goal_candidates(session, limit=json_body(request).get("limit", 5))
    return JsonResponse(
        {
            **recommendations,
            "session": session_to_dict(session),
        }
    )


@api_login_required
def recommend_session_support(request, session_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    body = json_body(request)
    recommendations = recommend_support_candidates(session, user=request.user, request=request)
    selected = [candidate for candidate in recommendations["candidates"] if candidate.get("selectedByDefault")]
    if body.get("apply", True):
        session.selected_source_results = selected
        session.save(update_fields=["selected_source_results", "updated_at"])
    return JsonResponse(
        {
            **recommendations,
            "selectedResults": selected,
            "session": session_to_dict(session),
            "guidance": "Suggested support is ranked from the selected template, blocks, facts, jurisdiction, and instructions. Confirm what the draft may rely on.",
        }
    )


@api_login_required
def session_outline(request, session_id):
    if request.method not in {"GET", "POST"}:
        return method_not_allowed(["GET", "POST"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    if request.method == "POST":
        body = json_body(request)
        if "selectedBlockKeys" in body:
            session.selected_block_keys = body["selectedBlockKeys"]
        author_profile = {**(session.author_profile or {}), "outlineApproved": True}
        session.author_profile = author_profile
        session, error = _advance_or_400(session, {"status": "outline_review"})
        if error:
            return error
    return JsonResponse(
        {
            "outline": outline_for_session(session),
            "session": session_to_dict(session),
            "guidance": "Approve the section plan before generating prose.",
        }
    )


@api_login_required
def draft_plan(request, session_id):
    if request.method not in {"GET", "POST", "PATCH"}:
        return method_not_allowed(["GET", "POST", "PATCH"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse({"plan": session.draft_plan, "session": session_to_dict(session)})
    body = json_body(request)
    try:
        check_session_revision(session, body)
        if request.method == "PATCH":
            session = apply_plan_edits(session, body)
        else:
            session = create_or_update_plan(session, body)
    except StaleRevision as stale:
        return _session_conflict(stale)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"plan": session.draft_plan, "session": session_to_dict(session)})


@api_login_required
def update_session_template_data(request, session_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    session, error = _session_or_404(request.user, session_id)
    if error:
        return error
    updates = json_body(request).get("templateData") or {}
    session.template_data = {**(session.template_data or {}), **updates}
    session.save(update_fields=["template_data", "updated_at"])
    return JsonResponse({"session": session_to_dict(session)})


@api_login_required
def generate_plan_drafts(request, session_id):
    if request.method not in {"GET", "POST"}:
        return method_not_allowed(["GET", "POST"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    if request.method == "GET":
        job_id = request.GET.get("job")
        if job_id:
            job = session.generation_jobs.filter(id=job_id).first()
            if not job:
                return JsonResponse({"error": "No such generation for this session."}, status=404)
            job = fail_if_stalled(job)
            payload = {"job": job_to_dict(job)}
            if job.status == DraftGenerationJob.COMPLETE:
                drafts = session.drafts.select_related("template").filter(id__in=job.draft_ids).order_by("created_at")
                payload["drafts"] = [draft_to_dict(draft) for draft in drafts]
            return JsonResponse(payload)
        # A plan can produce several documents, so reopening a session has to be
        # able to recover all of them, not just the one that was generated last.
        drafts = session.drafts.select_related("template").order_by("created_at")
        return JsonResponse({"drafts": [draft_to_dict(draft) for draft in drafts]})
    body = json_body(request)
    missing_filing = opposing_filing.missing_for_session(session)
    if missing_filing:
        return JsonResponse({"error": missing_filing, "opposingFilingRequired": True}, status=400)
    plan = session.draft_plan or {}
    blocking_missing = unanswered_missing_information(
        plan,
        require_all=bool(body.get("requireAllMissingInformation")),
    )
    if blocking_missing:
        return JsonResponse(
            {
                "error": "Answer the remaining drafting questions before generating the draft.",
                "missingInformation": blocking_missing,
            },
            status=400,
        )
    # Generation runs longer than a request may be held open, so it runs in the
    # background and the client polls the job (apps.drafting.generation_jobs).
    job = start_job(session, user=request.user, idempotency_key=str(body.get("idempotencyKey") or "")[:80])
    if job.status == DraftGenerationJob.FAILED:
        return JsonResponse({"error": job.error, "job": job_to_dict(job)}, status=400)
    if job.status == DraftGenerationJob.COMPLETE:
        drafts = session.drafts.select_related("template").filter(id__in=job.draft_ids).order_by("created_at")
        return JsonResponse({"drafts": [draft_to_dict(draft) for draft in drafts], "job": job_to_dict(job)}, status=201)
    return JsonResponse({"job": job_to_dict(job)}, status=202)


@api_login_required
def session_opposing_filing(request, session_id):
    """The other side's filing this session's document answers.

    GET reports it, what the template asks for, and the case-file documents to
    choose from. PUT picks one of those; POST uploads a copy that is not in the
    case file; PATCH changes how the draft names it; DELETE clears it.
    """
    if request.method not in {"GET", "PUT", "POST", "PATCH", "DELETE"}:
        return method_not_allowed(["GET", "PUT", "POST", "PATCH", "DELETE"])
    session, error = _session_or_404(request.user, session_id)
    if error:
        return error

    def payload(**extra):
        return {
            "opposingFiling": opposing_filing.to_dict(opposing_filing.filing_for(session)),
            "requirement": opposing_filing.requirement(session.template),
            "missing": opposing_filing.missing_for_session(session),
            **extra,
        }

    try:
        if request.method == "GET":
            choices, problem = opposing_filing.case_file_choices(session)
            return JsonResponse(payload(caseFileDocuments=choices, caseFileProblem=problem))
        if request.method == "DELETE":
            opposing_filing.remove(session)
            return JsonResponse(payload())
        if request.method == "POST":
            upload = request.FILES.get("file")
            if not upload:
                return JsonResponse({"error": "Choose the file to upload."}, status=400)
            opposing_filing.upload_filing(
                session,
                upload.read(),
                filename=upload.name,
                content_type=upload.content_type or "",
                user=request.user,
                filing_kind=request.POST.get("filingKind", ""),
                description=request.POST.get("description", ""),
                filed_on=opposing_filing.parse_filed_on(request.POST.get("filedOn", "")),
            )
            return JsonResponse(payload(), status=201)
        body = json_body(request)
        if request.method == "PUT":
            if not body.get("documentId"):
                return JsonResponse({"error": "Choose a document from the case file."}, status=400)
            opposing_filing.choose_case_document(
                session,
                body["documentId"],
                user=request.user,
                filing_kind=str(body.get("filingKind") or ""),
                description=str(body.get("description") or ""),
                filed_on=opposing_filing.parse_filed_on(body.get("filedOn") or ""),
            )
            return JsonResponse(payload())
        filing = opposing_filing.filing_for(session)
        if not filing:
            return JsonResponse({"error": "Identify the filing before describing it."}, status=400)
        opposing_filing.update_details(
            filing,
            description=body.get("description"),
            filed_on=body.get("filedOn"),
            filing_kind=body.get("filingKind"),
        )
        return JsonResponse(payload())
    except opposing_filing.OpposingFilingError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@api_login_required
def session_package(request, session_id):
    """The documents this session produced, their roles, and how they relate."""
    if request.method not in {"GET", "POST"}:
        return method_not_allowed(["GET", "POST"])
    session, error = _session_or_404(request.user, session_id)
    if error:
        return error
    if request.method == "POST":
        derive_relationships(session)
    return JsonResponse({"package": package_payload(session)})


@api_login_required
def generate_draft(request, session_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    session, error = _session_or_404(request.user, session_id, with_template=True)
    if error:
        return error
    try:
        draft = create_draft(session, user=request.user, request=request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"draft": draft_to_dict(draft)}, status=201)


@api_login_required
def draft_detail(request, draft_id):
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse({"draft": draft_to_dict(draft)})
    if request.method == "PATCH":
        body = json_body(request)
        try:
            with transaction.atomic():
                # Locked and compared first: an edit made against an older
                # revision is refused, never laid over someone else's.
                draft = locked_draft_at_revision(draft.id, body)
                if "sections" in body:
                    # Reviewer edits are recorded as human component versions, so the
                    # AI or template text they replaced stays recoverable.
                    record_sections(
                        draft,
                        body["sections"],
                        origin="human",
                        editor_state=body.get("editorState", draft.editor_state),
                    )
                    if "plainText" in body:
                        draft.plain_text = body["plainText"]
                        draft.save(update_fields=["plain_text", "updated_at"])
                else:
                    draft.plain_text = body.get("plainText", draft.plain_text)
                    draft.editor_state = body.get("editorState", draft.editor_state)
                    draft.save()
        except StaleRevision as stale:
            return _draft_conflict(stale)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        draft.refresh_from_db()
        return JsonResponse({"draft": draft_to_dict(draft)})
    return method_not_allowed(["GET", "PATCH"])


@api_login_required
def draft_components(request, draft_id):
    if request.method != "GET":
        return method_not_allowed(["GET"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    return JsonResponse({"components": component_history(draft)})


@api_login_required
def draft_operations(request, draft_id):
    """List the change history for a document, or propose a new change to it."""
    if request.method not in {"GET", "POST"}:
        return method_not_allowed(["GET", "POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse(
            {
                "operations": [
                    operation_to_dict(operation)
                    for operation in draft.operations.select_related("target_component", "requested_by")
                ]
            }
        )

    body = json_body(request)
    try:
        operation = operations.propose(
            draft,
            body.get("operationType", ""),
            payload=body.get("payload") or {},
            rationale=body.get("rationale", ""),
            requested_by=request.user,
        )
        # Applying on propose keeps the reviewer's own edits one request, while
        # still recording what the change was and what it replaced.
        if body.get("apply"):
            operations.apply(operation)
            draft.refresh_from_db()
    except operations.OperationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(
        {"operation": operation_to_dict(operation), "draft": draft_to_dict(draft)},
        status=201,
    )


@api_login_required
def draft_operation_decision(request, draft_id, operation_id):
    """Apply or reject a proposed change."""
    if request.method != "POST":
        return method_not_allowed(["POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    operation = draft.operations.filter(id=operation_id).first()
    if not operation:
        return JsonResponse({"error": "Operation not found"}, status=404)
    body = json_body(request)
    decision = body.get("decision")
    try:
        if decision == "apply":
            operations.apply(operation)
        elif decision == "reject":
            operations.reject(operation, body.get("note", ""))
        else:
            return JsonResponse({"error": "Decision must be 'apply' or 'reject'."}, status=400)
    except operations.OperationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    operation.refresh_from_db()
    draft.refresh_from_db()
    return JsonResponse({"operation": operation_to_dict(operation), "draft": draft_to_dict(draft)})


@api_login_required
def regenerate_block(request, draft_id, block_key):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    draft = regenerate_draft_block(draft, block_key, json_body(request).get("instruction", ""))
    return JsonResponse({"draft": draft_to_dict(draft)})


@api_login_required
def validate_draft(request, draft_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    draft, validation_summary = validate_with_auto_repair(draft)
    draft = record_validation(draft, validation_summary)
    draft.session.status = "validation"
    draft.session.save(update_fields=["status", "updated_at"])
    return JsonResponse({"draft": draft_to_dict(draft), "validation": validation_summary})


@api_login_required
def draft_revision_plan(request, draft_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    findings = validate_document(draft)
    return JsonResponse({"revisionPlan": build_revision_plan(draft, findings)})


@api_login_required
def apply_draft_revision(request, draft_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    plan_items = json_body(request).get("plan") or []
    draft = apply_revision_plan(draft, plan_items)
    draft, validation_summary = validate_with_auto_repair(draft)
    draft = record_validation(draft, validation_summary)
    return JsonResponse({"draft": draft_to_dict(draft), "validation": validation_summary})


@api_login_required
def export_draft(request, draft_id):
    if request.method != "GET":
        return method_not_allowed(["GET"])
    draft, error = _draft_or_404(request.user, draft_id, allow_fill=True)
    if error:
        return error
    if draft.session.mode == "template_fill":
        # Delivery was recorded by the background fill export; this is a read.
        return export_document(draft)
    draft.session.status = "export"
    draft.session.save(update_fields=["status", "updated_at"])
    response = export_document(draft)
    # The export is a plain download link, so the opt-out travels as a query
    # parameter rather than a request body.
    requested = wants_delivery(request.GET.dict(), "documents")
    if not requested:
        return response
    delivery = save_document(
        draft.session.matter,
        user=request.user,
        filename=_download_filename(response, fallback=draft_export_filename(draft)),
        content=response.content,
        content_type=response["Content-Type"],
        title=draft.title or getattr(draft.session.template, "name", "") or f"Draft {draft.id}",
        origin="draft_export",
        scope_key=f"draft-export:draft:{draft.id}",
    )
    audit_delivery = save_draft_ai_audit(draft, user=request.user)
    return attach_delivery_headers(response, delivery, audit_delivery=audit_delivery)


def _download_filename(response, *, fallback):
    match = re.search(r'filename="([^"]+)"', response.get("Content-Disposition", ""))
    return match.group(1) if match else fallback

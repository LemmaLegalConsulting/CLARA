"""HTTP boundary for the Argument Gym.

Every lookup resolves the workspace's linked matter through the same user-scoped
access boundary the rest of the app uses. A workspace with no matter is a
standalone stress test and belongs to whoever created it.
"""

import hashlib
import uuid
from pathlib import Path

from django.db import models
from django.http import JsonResponse
from django.utils import timezone

from apps.argument_gym import artifacts, record
from apps.argument_gym.ingestion import ingest_upload
from apps.argument_gym import checks as check_catalog
from apps.argument_gym.models import GymChallenge, GymChecklist, GymDocument, GymRun, GymWorkspace
from apps.argument_gym.pipeline import fail_if_stalled, research_coverage, run_research, start_run
from apps.argument_gym.serializers import (
    challenge_to_dict,
    checklist_to_dict,
    court_to_dict,
    document_to_dict,
    run_to_dict,
    workspace_to_dict,
)
from apps.rules.court_profiles import detect_court, detect_pleading_type, sync_court_profile_seeds
from apps.rules.legal_rules import ensure_legal_rule_profiles, rule_elements
from apps.rules.models import CourtProfile, LegalRuleProfile
from apps.core.http import api_login_required, json_body, method_not_allowed
from apps.core.views import default_jurisdiction_for_user
from apps.drafting.models import DraftDocument
from apps.matters.services import legalserver_access_profile_for_user, matter_for_user, user_can_access_matter
from apps.sources.document_text import DocumentExtractionError
from apps.validation.revision import apply_revision_plan


def can_read_workspace(user, workspace, *, access_profile=None):
    """A gym workspace is exactly as reachable as the case behind it.

    Without a matter there is no case boundary to defer to, so a standalone
    stress test stays with whoever created it.
    """
    if workspace.matter:
        return user_can_access_matter(user, workspace.matter, access_profile=access_profile)
    return not workspace.owner_id or workspace.owner_id == user.id


def _workspace_or_404(user, workspace_id):
    workspace = GymWorkspace.objects.select_related("matter", "owner").filter(id=workspace_id).first()
    if not workspace or not can_read_workspace(user, workspace):
        return None, JsonResponse({"error": "Gym workspace not found"}, status=404)
    return workspace, None


def _run_or_404(user, run_id):
    run = GymRun.objects.select_related("workspace", "workspace__matter", "brief").filter(id=run_id).first()
    if not run:
        return None, JsonResponse({"error": "Gym run not found"}, status=404)
    _workspace, error = _workspace_or_404(user, run.workspace_id)
    if error:
        return None, JsonResponse({"error": "Gym run not found"}, status=404)
    return run, None


def _challenge_or_404(user, challenge_id):
    challenge = GymChallenge.objects.select_related("run", "run__workspace", "run__brief").filter(id=challenge_id).first()
    if not challenge:
        return None, JsonResponse({"error": "Challenge not found"}, status=404)
    _run, error = _run_or_404(user, challenge.run_id)
    if error:
        return None, JsonResponse({"error": "Challenge not found"}, status=404)
    return challenge, None


def _draft_or_404(user, draft_id):
    draft = DraftDocument.objects.select_related("session", "session__matter").filter(id=draft_id).first()
    if not draft or not user_can_access_matter(user, draft.session.matter):
        return None, JsonResponse({"error": "Draft not found"}, status=404)
    return draft, None


@api_login_required
def workspaces(request):
    if request.method == "GET":
        # One access profile for the whole list: resolving it per workspace would
        # ask LegalServer who this user is once for every row.
        access_profile = legalserver_access_profile_for_user(request.user)
        queryset = GymWorkspace.objects.select_related("matter", "court").prefetch_related(
            "documents", "runs", "runs__brief"
        )
        matter_id = request.GET.get("matterId", "").strip()
        if matter_id == "none":
            # Standalone sessions -- a brief tested with no case file behind it.
            queryset = queryset.filter(matter__isnull=True)
        elif matter_id:
            queryset = queryset.filter(matter__external_id=matter_id)
        search = request.GET.get("q", "").strip().casefold()
        visible = [
            workspace
            for workspace in queryset
            if can_read_workspace(request.user, workspace, access_profile=access_profile)
        ]
        payload = [workspace_to_dict(workspace) for workspace in visible]
        if search:
            payload = [
                item
                for item in payload
                if search in f"{item['title']} {item['briefTitle']} {item['matterName']}".casefold()
            ]
        # Every case these sessions touch, so the filter can offer the ones that
        # actually have sessions rather than the whole case list.
        matters = {
            item["matterId"]: item["matterName"]
            for item in (workspace_to_dict(workspace, include_documents=False) for workspace in visible)
            if item["matterId"]
        }
        return JsonResponse(
            {
                "workspaces": payload,
                "matters": [{"id": key, "name": value} for key, value in sorted(matters.items(), key=lambda pair: pair[1])],
            }
        )
    if request.method != "POST":
        return method_not_allowed(["GET", "POST"])

    body = json_body(request)
    matter = None
    if body.get("matterId"):
        matter = matter_for_user(request.user, body["matterId"])
        if not matter:
            return JsonResponse({"error": "Case not found or not available to this user"}, status=404)
    workspace = GymWorkspace.objects.create(
        owner=request.user,
        matter=matter,
        jurisdiction=(
            str(body.get("jurisdiction") or "").strip()
            or getattr(matter, "jurisdiction", "")
            or default_jurisdiction_for_user(request.user)
        ),
        title=str(body.get("title") or "").strip() or "Argument gym",
    )
    error = _apply_jurisdiction(workspace, body) or _apply_check_selection(workspace, body, request.user)
    if error:
        return error
    workspace.save()
    return JsonResponse({"workspace": workspace_to_dict(workspace)}, status=201)


JURISDICTION_DETAIL_FIELDS = ("state", "county", "municipality", "division", "courtType")


def _apply_jurisdiction(workspace, body):
    """Set how jurisdiction and filing rules are resolved for this session."""
    mode = body.get("jurisdictionMode")
    if mode in dict(GymWorkspace.RESOLUTION_CHOICES):
        workspace.jurisdiction_mode = mode
    rule_mode = body.get("courtRuleMode")
    if rule_mode in dict(GymWorkspace.RULE_MODE_CHOICES):
        workspace.court_rule_mode = rule_mode
    if "courtSlug" in body:
        slug = str(body["courtSlug"] or "").strip()
        if not slug:
            workspace.court = None
        else:
            court = CourtProfile.objects.filter(slug=slug, active=True).first()
            if not court:
                return JsonResponse({"error": f"No court profile named '{slug}'."}, status=400)
            workspace.court = court
    if "jurisdictionDetail" in body:
        detail = body["jurisdictionDetail"] or {}
        if not isinstance(detail, dict):
            return JsonResponse({"error": "jurisdictionDetail must be an object."}, status=400)
        cleaned = {field: str(detail.get(field) or "").strip() for field in JURISDICTION_DETAIL_FIELDS}
        court_type = cleaned["courtType"]
        if cleaned["municipality"] and court_type and court_type not in CourtProfile.MUNICIPAL_TYPES:
            # An appellate district is not in a city. Silently keeping the value
            # would put a municipality on a court that has none.
            cleaned["municipality"] = ""
        workspace.jurisdiction_detail = {field: value for field, value in cleaned.items() if value}
        workspace.jurisdiction = (
            str(body.get("jurisdiction") or "").strip()
            or ", ".join(
                value
                for value in (cleaned["municipality"], cleaned["county"] and f"{cleaned['county']} County", cleaned["division"], cleaned["state"])
                if value
            )
            or workspace.jurisdiction
        )
    elif "jurisdiction" in body:
        workspace.jurisdiction = str(body["jurisdiction"] or "").strip()
    return None


def _apply_check_selection(workspace, body, user):
    """Record the author's explicit choice of checks, and nothing wider."""
    if "enabledChecks" in body:
        selected = body["enabledChecks"]
        if not isinstance(selected, list):
            return JsonResponse({"error": "enabledChecks must be a list of check ids."}, status=400)
        unknown = [str(item) for item in selected if str(item) not in check_catalog.CHECKS_BY_ID]
        if unknown:
            return JsonResponse({"error": f"Unknown check(s): {', '.join(sorted(unknown))}."}, status=400)
        # An author who turns everything off gets a run that reports it ran
        # nothing, not a run of the defaults.
        workspace.enabled_checks = [str(item) for item in selected] or [check_catalog.NONE_SELECTED]
    if "checkSettings" in body:
        settings_payload = body["checkSettings"]
        if not isinstance(settings_payload, dict):
            return JsonResponse({"error": "checkSettings must be an object."}, status=400)
        workspace.check_settings = {**(workspace.check_settings or {}), **settings_payload}
    if "checklistId" in body:
        checklist_id = body["checklistId"]
        if not checklist_id:
            workspace.checklist = None
        else:
            checklist = _readable_checklists(user).filter(id=checklist_id).first()
            if not checklist:
                return JsonResponse({"error": "Checklist not found"}, status=404)
            workspace.checklist = checklist
    return None


def _readable_checklists(user):
    """A checklist is the author's own, or one shared with the deployment."""
    return GymChecklist.objects.filter(models.Q(owner=user) | models.Q(shared=True)).distinct()


@api_login_required
def workspace_detail(request, workspace_id):
    workspace, error = _workspace_or_404(request.user, workspace_id)
    if error:
        return error
    if request.method == "GET":
        runs = list(workspace.runs.select_related("brief", "court"))
        # Returning to a past session should show it, not an index of it.
        latest = next((run for run in runs if run.status == GymRun.COMPLETE), None) or (runs[0] if runs else None)
        return JsonResponse(
            {
                "workspace": workspace_to_dict(workspace),
                "runs": [run_to_dict(run, include_challenges=False) for run in runs],
                "latestRun": run_to_dict(latest) if latest else None,
            }
        )
    if request.method == "PATCH":
        body = json_body(request)
        if "title" in body:
            workspace.title = str(body["title"]).strip() or workspace.title
        if "jurisdiction" in body:
            workspace.jurisdiction = str(body["jurisdiction"]).strip()
        if "matterId" in body:
            matter = matter_for_user(request.user, body["matterId"]) if body["matterId"] else None
            if body["matterId"] and not matter:
                return JsonResponse({"error": "Case not found or not available to this user"}, status=404)
            workspace.matter = matter
        error = _apply_jurisdiction(workspace, body) or _apply_check_selection(workspace, body, request.user)
        if error:
            return error
        workspace.save()
        return JsonResponse({"workspace": workspace_to_dict(workspace)})
    if request.method == "DELETE":
        workspace.delete()
        return JsonResponse({"ok": True})
    return method_not_allowed(["GET", "PATCH", "DELETE"])


def _upsert_matter_material(workspace, reference, *, excluded):
    """Record a decision about one case-file document without copying it.

    A case document only earns a row here when the advocate says something about
    it -- normally that this run should leave it out. `update_or_create` cannot
    do this: the JSON key lookup that finds the row is not a field it can build
    a new row from.
    """
    document = workspace.documents.filter(
        role=GymDocument.CASE_RECORD,
        source_type=GymDocument.MATTER_DOCUMENT,
        external_reference__documentId=reference.get("documentId"),
    ).first()
    if document:
        document.title = reference.get("title") or document.title
        document.external_reference = reference
        document.excluded = excluded
        document.save(update_fields=["title", "external_reference", "excluded", "updated_at"])
        return document
    return GymDocument.objects.create(
        workspace=workspace,
        role=GymDocument.CASE_RECORD,
        source_type=GymDocument.MATTER_DOCUMENT,
        title=reference.get("title") or "Case document",
        external_reference=reference,
        excluded=excluded,
    )


@api_login_required
def workspace_documents(request, workspace_id):
    workspace, error = _workspace_or_404(request.user, workspace_id)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse({"documents": [document_to_dict(document) for document in workspace.documents.all()]})
    if request.method != "POST":
        return method_not_allowed(["GET", "POST"])

    if request.content_type and request.content_type.startswith("multipart/"):
        upload = request.FILES.get("file")
        if not upload:
            return JsonResponse({"error": "Upload a brief or case document"}, status=400)
        role = request.POST.get("role") or GymDocument.BRIEF_UNDER_TEST
        content = upload.read()
        try:
            ingested = ingest_upload(
                content, filename=upload.name, content_type=upload.content_type or ""
            )
        except DocumentExtractionError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if not ingested["text"].strip():
            return JsonResponse({"error": "No readable text could be extracted from this file"}, status=400)
        role = role if role in dict(GymDocument.ROLE_CHOICES) else GymDocument.BRIEF_UNDER_TEST
        split = (ingested["metadata"].get("split") or {}) if role == GymDocument.BRIEF_UNDER_TEST else {}
        from apps.core.storage import RAW, get_document_storage

        storage_key = f"argument-gym/{workspace.pk}/{uuid.uuid4().hex}{Path(upload.name).suffix.lower()}"
        get_document_storage(RAW).put_bytes(content=content, key=storage_key, content_type=upload.content_type or "application/octet-stream")
        # Successful text ingestion publishes the immutable original for review exports.
        get_document_storage().put_bytes(content=content, key=storage_key, content_type=upload.content_type or "application/octet-stream")
        ingested["metadata"]["sourceChecksum"] = hashlib.sha256(content).hexdigest()
        document = GymDocument.objects.create(
            workspace=workspace,
            storage_key=storage_key,
            role=role,
            source_type=GymDocument.UPLOAD,
            title=request.POST.get("title") or upload.name,
            original_filename=upload.name,
            content_type=upload.content_type or "",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
            pleading_type=(
                request.POST.get("pleadingType")
                or detect_pleading_type(ingested["text"], title=upload.name)["pleadingType"]
            ),
            page_range={"start": 1, "end": split["briefPageCount"]} if split.get("briefPageCount") else {},
        )
        # A filing arrives with its exhibits attached. They are case-record
        # material, not part of the argument being tested, so they are split out
        # rather than sent to a model as if they were the brief.
        exhibits = [
            GymDocument.objects.create(
                workspace=workspace,
                role=GymDocument.CASE_RECORD,
                source_type=GymDocument.UPLOAD,
                split_from=document,
                title=exhibit["title"],
                original_filename=upload.name,
                content_type=upload.content_type or "",
                extracted_text=exhibit["text"],
                extraction_metadata={"extractor": ingested["metadata"]["extractor"], "units": []},
                page_range=exhibit["pageRange"],
            )
            for exhibit in ingested["exhibits"]
        ]
        return JsonResponse(
            {
                "document": document_to_dict(document),
                "exhibits": [document_to_dict(exhibit) for exhibit in exhibits],
                "split": ingested["metadata"].get("split"),
            },
            status=201,
        )

    body = json_body(request)
    if body.get("draftId"):
        draft, draft_error = _draft_or_404(request.user, body["draftId"])
        if draft_error:
            return draft_error
        document = GymDocument.objects.create(
            workspace=workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.DRAFT_DOCUMENT,
            draft_document=draft,
            title=draft.title,
        )
        return JsonResponse({"document": document_to_dict(document)}, status=201)

    reference = body.get("reference") or {}
    if not reference.get("documentId"):
        return JsonResponse({"error": "Provide a file, a draftId, or a case-document reference."}, status=400)
    document = _upsert_matter_material(workspace, reference, excluded=bool(body.get("excluded", False)))
    return JsonResponse({"document": document_to_dict(document)}, status=201)


@api_login_required
def document_detail(request, document_id):
    document = GymDocument.objects.select_related("workspace", "workspace__matter").filter(id=document_id).first()
    if not document:
        return JsonResponse({"error": "Document not found"}, status=404)
    _workspace, error = _workspace_or_404(request.user, document.workspace_id)
    if error:
        return JsonResponse({"error": "Document not found"}, status=404)
    if request.method == "PATCH":
        body = json_body(request)
        if "excluded" in body:
            document.excluded = bool(body["excluded"])
        if "title" in body:
            document.title = str(body["title"]).strip() or document.title
        if "pleadingType" in body:
            document.pleading_type = str(body["pleadingType"] or "").strip()
        document.save()
        return JsonResponse({"document": document_to_dict(document)})
    if request.method == "DELETE":
        document.delete()
        return JsonResponse({"ok": True})
    return method_not_allowed(["PATCH", "DELETE"])


@api_login_required
def workspace_materials(request, workspace_id):
    """Every case-record source available to this workspace, excluded ones marked."""
    workspace, error = _workspace_or_404(request.user, workspace_id)
    if error:
        return error
    if request.method == "GET":
        materials = record.available_materials(workspace)
        return JsonResponse({"materials": [record.public_material(material) for material in materials]})
    if request.method != "POST":
        return method_not_allowed(["GET", "POST"])

    body = json_body(request)
    material_id = str(body.get("materialId") or "")
    excluded = bool(body.get("excluded", True))
    if material_id.startswith(record.MATTER_MATERIAL_PREFIX):
        material = next(
            (item for item in record.available_materials(workspace) if item["id"] == material_id),
            None,
        )
        if not material:
            return JsonResponse({"error": "Case material not found"}, status=404)
        _upsert_matter_material(workspace, material["reference"], excluded=excluded)
    else:
        document = workspace.documents.filter(id=material_id.removeprefix("upload:")).first()
        if not document:
            return JsonResponse({"error": "Case material not found"}, status=404)
        document.excluded = excluded
        document.save(update_fields=["excluded", "updated_at"])
    materials = record.available_materials(workspace)
    return JsonResponse({"materials": [record.public_material(material) for material in materials]})


def _run_status_code(run):
    """202 while the work is still going, 200 once it finished, 502 if it failed."""
    if run.status == GymRun.FAILED:
        return 502
    return 200 if run.status == GymRun.COMPLETE else 202


def _launch_run(workspace, brief, *, user, request, configuration=None):
    previous = workspace.runs.filter(brief=brief, status=GymRun.COMPLETE).order_by("-created_at").first()
    run = GymRun.objects.create(
        workspace=workspace,
        brief=brief,
        previous_run=previous,
        configuration=configuration or {},
        created_by=user,
    )
    return start_run(run, user=user, request=request)


@api_login_required
def workspace_runs(request, workspace_id):
    workspace, error = _workspace_or_404(request.user, workspace_id)
    if error:
        return error
    if request.method == "GET":
        return JsonResponse(
            {"runs": [run_to_dict(run, include_challenges=False) for run in workspace.runs.all()]}
        )
    if request.method != "POST":
        return method_not_allowed(["GET", "POST"])

    body = json_body(request)
    brief = workspace.documents.filter(role=GymDocument.BRIEF_UNDER_TEST)
    if body.get("briefId"):
        brief = brief.filter(id=body["briefId"])
    brief = brief.order_by("-id").first()
    if not brief:
        return JsonResponse({"error": "Attach a brief before running the gym."}, status=400)
    run = _launch_run(
        workspace,
        brief,
        user=request.user,
        request=request,
        configuration={"sourceIds": body.get("sourceIds") or []},
    )
    return JsonResponse({"run": run_to_dict(run)}, status=_run_status_code(run))


@api_login_required
def run_detail(request, run_id):
    if request.method != "GET":
        return method_not_allowed(["GET"])
    run, error = _run_or_404(request.user, run_id)
    if error:
        return error
    run = fail_if_stalled(run)
    return JsonResponse({"run": run_to_dict(run), "workspace": workspace_to_dict(run.workspace)})


@api_login_required
def run_artifact(request, run_id, kind):
    if request.method != "GET":
        return method_not_allowed(["GET"])
    run, error = _run_or_404(request.user, run_id)
    if error:
        return error
    if kind == "annotated":
        from apps.exporting.gym_annotations import AnnotationUnavailable, annotated_download

        try:
            return annotated_download(run)
        except AnnotationUnavailable as exc:
            return JsonResponse({"error": str(exc)}, status=409)
    builder = artifacts.ARTIFACT_BUILDERS.get(kind)
    if not builder:
        return JsonResponse({"error": f"Unknown artifact '{kind}'"}, status=404)
    return JsonResponse({"artifact": builder(run)})


@api_login_required
def run_revision(request, run_id):
    """Build a block-scoped revision plan, or apply an edited one to a native draft."""
    if request.method not in {"GET", "POST"}:
        return method_not_allowed(["GET", "POST"])
    run, error = _run_or_404(request.user, run_id)
    if error:
        return error
    if request.method == "GET":
        challenge_ids = [item for item in request.GET.getlist("challengeId") if item.isdigit()]
        return JsonResponse({"revisionPlan": artifacts.revision_plan(run, challenge_ids=challenge_ids or None)})

    draft = run.brief.draft_document
    if not draft:
        return JsonResponse(
            {"error": "This brief was not drafted in this tool, so it has no sections to revise."},
            status=400,
        )
    body = json_body(request)
    plan_items = body.get("plan")
    if plan_items is None:
        plan_items = artifacts.revision_plan(run, challenge_ids=body.get("challengeIds"))["plan"]
    draft = apply_revision_plan(draft, plan_items)
    _link_applied_operations(run, draft, plan_items)
    run.refresh_from_db()
    return JsonResponse({"run": run_to_dict(run), "draftId": draft.id})


def _link_applied_operations(run, draft, plan_items):
    """Record which change a challenge produced, and mark the challenge addressed."""
    for item in plan_items or []:
        if not item.get("include", True):
            continue
        block_key = item.get("blockKey")
        challenge_ids = item.get("challengeIds") or []
        if not block_key or not challenge_ids:
            continue
        operation = (
            draft.operations.filter(target_component__stable_key=block_key, status="applied")
            .order_by("-id")
            .first()
        )
        run.challenges.filter(id__in=challenge_ids).update(
            resulting_operation=operation,
            disposition=GymChallenge.ADDRESSED,
            disposition_note="Sent to the revision plan and applied to the draft.",
            updated_at=timezone.now(),
        )


@api_login_required
def challenge_detail(request, challenge_id):
    if request.method != "POST":
        return method_not_allowed(["POST"])
    challenge, error = _challenge_or_404(request.user, challenge_id)
    if error:
        return error
    body = json_body(request)
    disposition = body.get("disposition")
    if disposition not in dict(GymChallenge.DISPOSITION_CHOICES):
        return JsonResponse({"error": "Disposition must be open, addressed, or dismissed."}, status=400)
    challenge.disposition = disposition
    challenge.disposition_note = str(body.get("note") or "").strip()
    challenge.save(update_fields=["disposition", "disposition_note", "updated_at"])
    return JsonResponse({"challenge": challenge_to_dict(challenge)})


@api_login_required
def challenge_research(request, challenge_id):
    """Research one challenge further, adding what it finds to that challenge."""
    if request.method != "POST":
        return method_not_allowed(["POST"])
    challenge, error = _challenge_or_404(request.user, challenge_id)
    if error:
        return error
    workspace = challenge.run.workspace
    body = json_body(request)
    query = str(body.get("query") or "").strip() or challenge.opponent_argument[:300]
    sources, trace = run_research(
        [{"query": query, "targets": [], "purpose": "Follow-up research on one challenge."}],
        matter=workspace.matter,
        jurisdiction=workspace.jurisdiction,
        user=request.user,
        request=request,
        registry=None,
        source_ids=body.get("sourceIds") or None,
    )
    known = {(source.get("sourceKind"), source.get("externalId")) for source in challenge.legal_sources or []}
    added = [source for source in sources if (source["sourceKind"], source["externalId"]) not in known]
    for offset, source in enumerate(added, start=len(challenge.legal_sources or []) + 1):
        source["id"] = str(offset)
    challenge.legal_sources = [*(challenge.legal_sources or []), *added]
    coverage = research_coverage(trace)
    previous = challenge.research_coverage or {}
    challenge.research_coverage = {
        **previous,
        "queries": [*previous.get("queries", []), *coverage["queries"]],
        "resultCount": previous.get("resultCount", 0) + coverage["resultCount"],
        "adequate": coverage["adequate"],
        "gaps": coverage["gaps"],
    }
    challenge.save(update_fields=["legal_sources", "research_coverage", "updated_at"])
    return JsonResponse({"challenge": challenge_to_dict(challenge), "addedSourceCount": len(added)})


@api_login_required
def draft_stress_test(request, draft_id):
    """Run the gym on a native draft, reusing the workspace the draft already has."""
    if request.method != "POST":
        return method_not_allowed(["POST"])
    draft, error = _draft_or_404(request.user, draft_id)
    if error:
        return error
    matter = draft.session.matter
    workspace = (
        GymWorkspace.objects.filter(matter=matter, documents__draft_document=draft).distinct().first()
        or GymWorkspace.objects.create(
            owner=request.user,
            matter=matter,
            jurisdiction=matter.jurisdiction or default_jurisdiction_for_user(request.user),
            title=f"Stress test: {draft.title}",
        )
    )
    brief, _created = GymDocument.objects.get_or_create(
        workspace=workspace,
        role=GymDocument.BRIEF_UNDER_TEST,
        source_type=GymDocument.DRAFT_DOCUMENT,
        draft_document=draft,
        defaults={"title": draft.title},
    )
    run = _launch_run(
        workspace,
        brief,
        user=request.user,
        request=request,
        configuration={"sourceIds": json_body(request).get("sourceIds") or []},
    )
    return JsonResponse(
        {"run": run_to_dict(run), "workspace": workspace_to_dict(workspace)},
        status=_run_status_code(run),
    )


@api_login_required
def courts(request):
    """The court profiles a session can be pointed at, and what they can check.

    Seeding happens here so a fresh checkout offers the maintained starter
    profiles the first time someone opens the picker, without overwriting
    anything an office has since edited.
    """
    if request.method != "GET":
        return method_not_allowed(["GET"])
    sync_court_profile_seeds()
    queryset = CourtProfile.objects.filter(active=True)
    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(name__icontains=search)
    return JsonResponse(
        {
            "courts": [court_to_dict(court) for court in queryset],
            "courtTypes": [
                {
                    "id": value,
                    "label": label,
                    "usesMunicipality": value in CourtProfile.MUNICIPAL_TYPES,
                }
                for value, label in CourtProfile.COURT_TYPE_CHOICES
            ],
        }
    )


@api_login_required
def workspace_court_detection(request, workspace_id):
    """What automatic detection would choose for this session's brief, and why."""
    if request.method != "GET":
        return method_not_allowed(["GET"])
    workspace, error = _workspace_or_404(request.user, workspace_id)
    if error:
        return error
    brief = workspace.documents.filter(role=GymDocument.BRIEF_UNDER_TEST).order_by("-id").first()
    text = brief.extracted_text if brief else ""
    sync_court_profile_seeds()
    detection = detect_court(text, matter=workspace.matter)
    pleading = detect_pleading_type(text, title=brief.title if brief else "")
    return JsonResponse(
        {
            "detection": {
                "detected": detection["detected"],
                "reason": detection["reason"],
                "matched": detection["matched"],
                "where": detection["where"],
                "court": court_to_dict(detection["profile"]),
            },
            "pleadingType": pleading["pleadingType"],
            "pleadingTypeMatched": pleading["matched"],
        }
    )


@api_login_required
def check_catalog_view(request):
    """Every check the gym can run, so the author can choose among them."""
    if request.method != "GET":
        return method_not_allowed(["GET"])
    return JsonResponse({"checks": check_catalog.catalog(), "defaults": check_catalog.DEFAULT_CHECK_IDS})


@api_login_required
def checklists(request):
    if request.method == "GET":
        return JsonResponse(
            {"checklists": [checklist_to_dict(item) for item in _readable_checklists(request.user)]}
        )
    if request.method != "POST":
        return method_not_allowed(["GET", "POST"])
    body = json_body(request)
    title = str(body.get("title") or "").strip()
    if not title:
        return JsonResponse({"error": "Give the checklist a name."}, status=400)
    checklist = GymChecklist.objects.create(
        owner=request.user,
        title=title,
        description=str(body.get("description") or "").strip(),
        items=_clean_checklist_items(body.get("items")),
        shared=bool(body.get("shared", False)),
    )
    return JsonResponse({"checklist": checklist_to_dict(checklist)}, status=201)


def _clean_checklist_items(items):
    cleaned = []
    for index, item in enumerate(items or [], start=1):
        text = str((item or {}).get("text") if isinstance(item, dict) else item or "").strip()
        if not text:
            continue
        identifier = str((item or {}).get("id") or "").strip() if isinstance(item, dict) else ""
        cleaned.append({"id": identifier or f"item-{index}", "text": text})
    return cleaned


@api_login_required
def checklist_detail(request, checklist_id):
    checklist = _readable_checklists(request.user).filter(id=checklist_id).first()
    if not checklist:
        return JsonResponse({"error": "Checklist not found"}, status=404)
    if request.method == "GET":
        return JsonResponse({"checklist": checklist_to_dict(checklist)})
    # A shared checklist is readable by everyone and editable only by its author.
    if checklist.owner_id and checklist.owner_id != request.user.id:
        return JsonResponse({"error": "This checklist belongs to someone else."}, status=403)
    if request.method == "PATCH":
        body = json_body(request)
        if "title" in body:
            checklist.title = str(body["title"]).strip() or checklist.title
        if "description" in body:
            checklist.description = str(body["description"] or "").strip()
        if "items" in body:
            checklist.items = _clean_checklist_items(body["items"])
        if "shared" in body:
            checklist.shared = bool(body["shared"])
        checklist.save()
        return JsonResponse({"checklist": checklist_to_dict(checklist)})
    if request.method == "DELETE":
        checklist.delete()
        return JsonResponse({"ok": True})
    return method_not_allowed(["GET", "PATCH", "DELETE"])


@api_login_required
def legal_rules(request):
    """The rules the element audit can recognize, and what each one requires."""
    if request.method != "GET":
        return method_not_allowed(["GET"])
    ensure_legal_rule_profiles()
    return JsonResponse(
        {
            "rules": [
                {
                    "slug": profile.slug,
                    "name": profile.name,
                    "citation": profile.citation,
                    "label": profile.label(),
                    "jurisdiction": profile.jurisdiction,
                    "ruleType": profile.rule_type,
                    "summary": profile.summary,
                    "verification": profile.verification,
                    "source": profile.source,
                    "aliases": profile.aliases,
                    "decisionTable": (
                        {"key": profile.decision_table_key, "row": profile.decision_table_row}
                        if profile.decision_table_key
                        else None
                    ),
                    "elements": [
                        {
                            "id": element["id"],
                            "label": element["label"],
                            "requirement": element.get("requirement", ""),
                            "needsRecordSupport": element.get("needsRecordSupport", False),
                            "origin": element.get("origin", "profile"),
                        }
                        for element in rule_elements(profile)
                    ],
                }
                for profile in LegalRuleProfile.objects.filter(active=True)
            ]
        }
    )

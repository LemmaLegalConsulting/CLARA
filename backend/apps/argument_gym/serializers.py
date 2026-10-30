from apps.argument_gym import checks as check_catalog
from apps.argument_gym.models import GymChallenge


def court_to_dict(court):
    if not court:
        return None
    return {
        "slug": court.slug,
        "name": court.name,
        "label": court.label(),
        "courtType": court.court_type,
        "courtTypeLabel": court.get_court_type_display(),
        "state": court.state,
        "county": court.county,
        "municipality": court.municipality,
        "division": court.division,
        "place": court.place(),
        # An appellate district has no municipality, so the form must not ask
        # for one and a profile must not claim one.
        "usesMunicipality": court.uses_municipality,
        "verification": court.verification,
        "source": court.source,
        "sourceUrl": court.source_url,
        "verifiedOn": court.verified_on.isoformat() if court.verified_on else "",
        "pleadingTypes": court.pleading_types,
        "notes": court.notes,
    }


def workspace_to_dict(workspace, *, include_documents=True):
    runs = list(workspace.runs.all())
    latest = runs[0] if runs else None
    payload = {
        "id": workspace.id,
        "title": workspace.title,
        "jurisdiction": workspace.jurisdiction,
        "jurisdictionMode": workspace.jurisdiction_mode,
        "jurisdictionDetail": workspace.jurisdiction_detail,
        "courtRuleMode": workspace.court_rule_mode,
        "court": court_to_dict(workspace.court),
        "enabledChecks": check_catalog.normalize_selection(workspace.enabled_checks),
        "checkSettings": workspace.check_settings,
        "checklist": checklist_to_dict(workspace.checklist) if workspace.checklist else None,
        "matterId": workspace.matter.external_id if workspace.matter else None,
        "matterName": workspace.matter.client_name if workspace.matter else "",
        "createdAt": workspace.created_at.isoformat(),
        "updatedAt": workspace.updated_at.isoformat(),
        "runCount": len(runs),
        "latestRunId": latest.id if latest else None,
        "lastRunAt": latest.created_at.isoformat() if latest else "",
        "briefTitle": latest.brief.title if latest else _brief_title(workspace),
        "openChallengeCount": (
            latest.challenges.filter(disposition="open").count() if latest else 0
        ),
        "verdict": latest.assessment_verdict if latest else "",
    }
    if include_documents:
        payload["documents"] = [document_to_dict(document) for document in workspace.documents.all()]
    return payload


def _brief_title(workspace):
    brief = next(
        (document for document in workspace.documents.all() if document.role == "brief_under_test"),
        None,
    )
    return brief.title if brief else ""


def document_to_dict(document):
    metadata = document.extraction_metadata or {}
    return {
        "id": document.id,
        "role": document.role,
        "sourceType": document.source_type,
        "title": document.title,
        "originalFilename": document.original_filename,
        "contentType": document.content_type,
        "draftId": document.draft_document_id,
        "reference": document.external_reference,
        "excluded": document.excluded,
        "pleadingType": document.pleading_type,
        "pageRange": document.page_range,
        "splitFromId": document.split_from_id,
        "extractor": metadata.get("extractor", ""),
        "pageCount": metadata.get("pageCount", 0),
        "unitCount": len(metadata.get("units") or []),
        "hasText": bool(document.extracted_text.strip()),
        "formatting": metadata.get("formatting") or {},
        "split": metadata.get("split") or None,
        "truncated": bool(metadata.get("truncated")),
    }


def challenge_to_dict(challenge):
    return {
        "id": challenge.id,
        "runId": challenge.run_id,
        "ordinal": challenge.ordinal,
        "category": challenge.category,
        "categoryLabel": challenge.get_category_display(),
        "target": challenge.target,
        "finding": challenge.finding,
        "reviewAction": challenge.review_action,
        "opponentArgument": challenge.opponent_argument,
        "whyItMatters": challenge.why_it_matters,
        "briefCurrentlySays": challenge.brief_currently_says,
        "legalSources": challenge.legal_sources,
        "recordSources": challenge.record_sources,
        "judgeAssessment": challenge.judge_assessment,
        "judgeVerdict": challenge.judge_verdict,
        "recommendation": challenge.coaching_recommendation,
        "suggestedResponse": challenge.suggested_response,
        "severity": challenge.severity,
        "importance": challenge.importance,
        "confidence": challenge.confidence,
        "researchCoverage": challenge.research_coverage,
        "disposition": challenge.disposition,
        "dispositionNote": challenge.disposition_note,
        "operationId": challenge.resulting_operation_id,
        # A challenge the advocate already answered, raised again after the
        # brief changed, is a different thing from a challenge seen for the
        # first time, and the card has to be able to say so.
        "previousDisposition": challenge.carried_from.disposition if challenge.carried_from else "",
        "recurring": challenge.carried_from_id is not None,
    }


def run_to_dict(run, *, include_challenges=True):
    payload = {
        "id": run.id,
        "workspaceId": run.workspace_id,
        "briefId": run.brief_id,
        "briefTitle": run.brief.title,
        "status": run.status,
        "error": run.error,
        "summary": run.summary,
        "assessment": run.assessment,
        "verdict": run.assessment_verdict,
        "court": court_to_dict(run.court),
        "courtDetection": run.court_detection,
        "compliance": run.compliance,
        "checksRun": run.checks_run,
        "checkResults": run.check_results,
        "ruleAudit": run.rule_audit,
        "checklistResults": run.checklist_results,
        "configuration": run.configuration,
        "materials": run.materials,
        "researchTrace": run.research_trace,
        "stageTrace": run.stage_trace,
        "comparison": run.comparison,
        "previousRunId": run.previous_run_id,
        "createdAt": run.created_at.isoformat(),
        "completedAt": run.completed_at.isoformat() if run.completed_at else None,
        "coverage": _coverage(run),
    }
    if include_challenges:
        payload["challenges"] = [challenge_to_dict(challenge) for challenge in run.challenges.all()]
    return payload


def _coverage(run):
    """Research coverage is a property of the run; every challenge carries the same one."""
    first = run.challenges.first()
    coverage = (first.research_coverage or {}) if first else {}
    return {
        "queries": coverage.get("queries", []),
        "resultCount": coverage.get("resultCount", 0),
        "adequate": coverage.get("adequate", False),
        "gaps": coverage.get("gaps", []),
    }


def disposition_choices():
    return [choice for choice, _label in GymChallenge.DISPOSITION_CHOICES]


def checklist_to_dict(checklist):
    return {
        "id": checklist.id,
        "title": checklist.title,
        "description": checklist.description,
        "items": checklist.items,
        "shared": checklist.shared,
        "ownerId": checklist.owner_id,
        "updatedAt": checklist.updated_at.isoformat(),
    }

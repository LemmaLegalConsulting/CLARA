"""Output artifacts, assembled from stored challenges rather than re-analyzed.

A prep sheet and a stress-test report are two readings of the same run. Asking
the model to produce each of them independently would let them disagree about
what the run found, so everything here is projected from `GymChallenge` rows.
The one model call is the narrative orientation at the top of the report, and it
is written once per run and reused.
"""

from apps.ai.openai_client import OpenAIBackendError, OpenAICompatibleClient
from apps.ai.prompt_catalog import PromptCatalogError, render_prompt
from apps.argument_gym.models import GymChallenge
from apps.argument_gym.pipeline import dumps, ai_enabled


HANDLED_VERDICTS = {"weak", "misplaced"}


def _strongest_authority(challenge):
    sources = challenge.legal_sources or []
    if not sources:
        return {}
    # The retrieval order is the ranking; the first source an attack cited is
    # the one opposing counsel would lead with.
    source = sources[0]
    return {
        "title": source.get("title", ""),
        "citation": source.get("citation", ""),
        "snippet": source.get("snippet", ""),
        "url": source.get("url", ""),
        "sourceLabel": source.get("sourceLabel", ""),
    }


def _strongest_adverse_record(challenge):
    adverse = [
        source
        for source in challenge.record_sources or []
        if source.get("status") in {"contradicted", "unsupported", "partially_supported"}
    ]
    pool = adverse or (challenge.record_sources or [])
    return pool[0] if pool else {}


def prep_sheet_rows(run):
    """One row per challenge: what they will say, and what we say back."""
    rows = []
    for challenge in run.challenges.all():
        rows.append(
            {
                "challengeId": challenge.id,
                "category": challenge.get_category_display(),
                "target": challenge.target,
                "finding": challenge.finding,
                "reviewAction": challenge.review_action,
                "recommendation": challenge.coaching_recommendation,
                "likelyOppositionPoint": challenge.opponent_argument,
                "strongestAuthority": _strongest_authority(challenge),
                "strongestAdverseRecord": _strongest_adverse_record(challenge),
                "currentResponse": challenge.brief_currently_says,
                "suggestedResponse": challenge.suggested_response or challenge.coaching_recommendation,
                "remainingVulnerability": (challenge.research_coverage or {}).get("remainingVulnerability", ""),
                "disposition": challenge.disposition,
            }
        )
    return rows


def narrative_summary(run, *, llm_client=None, refresh=False):
    """Write the run's orientation paragraph once, then reuse it."""
    if run.summary and not refresh:
        return run.summary
    challenges = list(run.challenges.all())
    if not challenges or not ai_enabled():
        return run.summary
    coverage = (challenges[0].research_coverage or {}) if challenges else {}
    try:
        prompt = render_prompt(
            "argument_gym.prep_sheet",
            jurisdiction=run.workspace.jurisdiction or "the filing jurisdiction",
            brief_title=run.brief.title,
            matter_summary=run.workspace.matter.summary if run.workspace.matter else "No case record was provided.",
            challenges=dumps(
                [
                    {
                        "category": challenge.get_category_display(),
                        "target": challenge.target.get("section", ""),
                        "finding": challenge.finding,
                        "reviewAction": challenge.review_action,
                        "argument": challenge.opponent_argument,
                        "judge": challenge.judge_assessment,
                        "severity": challenge.severity,
                        "response": challenge.suggested_response or challenge.coaching_recommendation,
                        "disposition": challenge.disposition,
                    }
                    for challenge in challenges
                ]
            ),
            coverage=dumps({"queries": coverage.get("queries", []), "gaps": coverage.get("gaps", [])}),
            materials=dumps([material["title"] for material in run.materials or []]),
        )
        client = llm_client or OpenAICompatibleClient()
        summary = client.complete(
            system=prompt.system,
            user=prompt.user,
            temperature=0.2,
            model=prompt.default_model,
            reasoning_level=prompt.default_reasoning_level,
        )
    except (OpenAIBackendError, PromptCatalogError):
        return run.summary
    run.summary = summary.strip()
    run.save(update_fields=["summary"])
    return run.summary


def opposition_prep_sheet(run, *, llm_client=None):
    return {
        "kind": "opposition_prep_sheet",
        "title": f"Opposition prep sheet - {run.brief.title}",
        "summary": narrative_summary(run, llm_client=llm_client),
        "rows": prep_sheet_rows(run),
        "materials": run.materials or [],
    }


def stress_test_report(run, *, llm_client=None):
    challenges = list(run.challenges.all())
    coverage = (challenges[0].research_coverage or {}) if challenges else {}
    vulnerabilities = []
    handled = []
    for challenge in challenges:
        entry = {
            "challengeId": challenge.id,
            "category": challenge.get_category_display(),
            "severity": challenge.severity,
            "importance": challenge.importance,
            "confidence": challenge.confidence,
            "target": challenge.target,
            "finding": challenge.finding,
            "reviewAction": challenge.review_action,
            "argument": challenge.opponent_argument,
            "whyItMatters": challenge.why_it_matters,
            "judgeAssessment": challenge.judge_assessment,
            "judgeVerdict": challenge.judge_verdict,
            "recommendation": challenge.coaching_recommendation,
            "disposition": challenge.disposition,
        }
        already_answered = (
            challenge.disposition in {GymChallenge.ADDRESSED, GymChallenge.DISMISSED}
            or challenge.judge_verdict in HANDLED_VERDICTS
        )
        (handled if already_answered else vulnerabilities).append(entry)
    return {
        "kind": "stress_test_report",
        "title": f"Stress-test report - {run.brief.title}",
        # The paragraph an advocate reads first: does this brief persuade, and
        # what most needs fixing. Written once by the run, never re-derived here.
        "assessment": run.assessment,
        "verdict": run.assessment_verdict,
        "executiveSummary": narrative_summary(run, llm_client=llm_client),
        "compliance": run.compliance or {},
        # Which checks ran, which the author turned off, and which could not
        # apply. A report that showed only findings would let a check that never
        # ran read as a clean result.
        "checksRun": run.checks_run or [],
        "checkResults": run.check_results or {},
        "ruleAudit": run.rule_audit or [],
        "checklistResults": run.checklist_results or {},
        "vulnerabilities": vulnerabilities,
        "handledWell": handled,
        "researchGaps": coverage.get("gaps", []),
        "unresolvedNotes": [
            {"challengeId": challenge.id, "note": (challenge.research_coverage or {}).get("note", "")}
            for challenge in challenges
            if (challenge.research_coverage or {}).get("note")
        ],
        "materialsReviewed": run.materials or [],
        "comparison": run.comparison or {},
    }


def revision_plan(run, *, challenge_ids=None):
    """Block-scoped recommendations, directly actionable on a native draft.

    The items are the same shape the validation revision plan uses, so a native
    draft can hand them straight to the existing revision machinery and an
    external brief can show the identical text as something to copy.
    """
    draft = run.brief.draft_document
    selected = run.challenges.all()
    if challenge_ids:
        selected = selected.filter(id__in=challenge_ids)
    by_block = {}
    unscoped = []
    for challenge in selected:
        block_key = (challenge.target or {}).get("blockKey", "")
        instruction = (challenge.research_coverage or {}).get("blockInstruction") or (
            f"Address this opposition argument without asserting new facts: {challenge.opponent_argument}"
        )
        if challenge.coaching_recommendation:
            instruction = f"{instruction}\nCoaching: {challenge.coaching_recommendation}"
        item = {
            "challengeId": challenge.id,
            "category": challenge.get_category_display(),
            "target": challenge.target,
            "instruction": instruction,
            "suggestedResponse": challenge.suggested_response,
        }
        if block_key and draft:
            by_block.setdefault(block_key, []).append(item)
        else:
            unscoped.append(item)

    section_labels = {section.get("key"): section.get("label") for section in (draft.sections if draft else []) or []}
    plan = [
        {
            "blockKey": block_key,
            "sectionLabel": section_labels.get(block_key, block_key),
            "challengeIds": [item["challengeId"] for item in items],
            "instruction": "\n\n".join(item["instruction"] for item in items),
            "include": True,
        }
        for block_key, items in by_block.items()
    ]
    return {
        "kind": "revision_plan",
        "draftId": draft.id if draft else None,
        "actionable": bool(plan),
        "plan": plan,
        "copyOnly": unscoped,
    }


ARTIFACT_BUILDERS = {
    "prep_sheet": opposition_prep_sheet,
    "report": stress_test_report,
}

"""The one adversarial pipeline both gym modes run.

    brief ingestion
        -> argument map
        -> brief-to-record support check (when case materials exist)
        -> adversarial research queries
        -> existing research / augmented_search
        -> opponent generates the strongest attacks
        -> an independent judge filters and ranks them
        -> a coach proposes responses
        -> stored GymChallenge records

Opponent, judge, and coach are separate model calls on purpose. One call asked
to attack, weigh, and answer produces an attack it has already decided is
answerable, which is the failure mode this whole feature exists to avoid: a
brief that reads as fine because the thing reading it wanted it to be fine.

Every stage falls back to a deterministic result when the model is unavailable
or returns something unusable, so a run always produces reviewable output and
the pipeline is testable without live AI.
"""

import hashlib
import json
import logging
import re
import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from apps.ai.case_chat import compact_case_context
from apps.ai.openai_client import OpenAIBackendError, OpenAICompatibleClient
from apps.ai.prompt_catalog import PromptCatalogError, render_prompt
from apps.ai.tool_loop import ToolEvaluation, run_tool_with_repair
from apps.argument_gym import checks as check_catalog
from apps.argument_gym import ingestion, record
from apps.argument_gym.models import GymChallenge, GymDocument, GymRun, GymWorkspace
from apps.rules.court_profiles import detect_court, detect_pleading_type
from apps.sources.augmentation import augmented_search
from apps.sources.models import SourceConfiguration
from apps.sources.registry import connector_registry as default_connector_registry
from apps.sources.selection import automatic_source_selection, source_kinds
from apps.validation.court_formatting import check_court_compliance
from apps.validation.language import check_language
from apps.validation.pleading_form import check_pleading_form
from apps.validation.readability import check_readability, summarize as summarize_readability


MAX_QUERIES = 5
MAX_ATTACKS = 10
MAX_CHALLENGES = 7
MAX_MATERIALS = 6
SEVERITY_PREFIX = {"error": "E", "warning": "W", "info": "I"}
logger = logging.getLogger(__name__)

CATEGORIES = {choice for choice, _label in GymChallenge.CATEGORY_CHOICES}
SEVERITIES = {choice for choice, _label in GymChallenge.SEVERITY_CHOICES}
CONFIDENCES = {choice for choice, _label in GymChallenge.CONFIDENCE_CHOICES}


def ai_enabled():
    config = SourceConfiguration.effective_settings("openai", {"enabled": settings.AI_DRAFTING_ENABLED})
    return str(config.get("enabled", "")).lower() not in {"0", "false", "no", "off"}


def json_object(text):
    """Read the first JSON object out of a model reply, or nothing."""
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def dumps(value):
    return json.dumps(value, indent=2, default=str)


def clean(value, *, limit=4000):
    return str(value or "").strip()[:limit]


VERDICT_LIMIT = GymRun._meta.get_field("assessment_verdict").max_length


def choice(value, allowed, default):
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in allowed else default


class Stage:
    """One pipeline step: a model call with a deterministic result behind it."""

    def __init__(self, name, *, llm_client=None):
        self.name = name
        self.llm_client = llm_client

    def run(self, *, prompt_key, context, parse, fallback, temperature=0.2, empty_key=None):
        method = "llm" if ai_enabled() else "deterministic"

        def execute(plan):
            if plan["method"] == "deterministic":
                return {"method": "deterministic", "items": fallback()}
            try:
                prompt = render_prompt(prompt_key, **context)
                client = self.llm_client or OpenAICompatibleClient()
                response = client.complete(
                    system=prompt.system,
                    user=prompt.user,
                    temperature=temperature,
                    model=prompt.default_model,
                    reasoning_level=prompt.default_reasoning_level,
                )
            except (OpenAIBackendError, PromptCatalogError):
                return {"method": "llm", "items": []}
            payload = json_object(response)
            items = parse(payload)
            return {"method": "llm", "items": items,
                    "valid_empty": empty_key is not None and payload.get(empty_key) == []}

        def evaluate(_plan, result):
            if not result["items"] and not result.get("valid_empty"):
                return ToolEvaluation(False, f"{self.name}_empty", f"The {self.name} stage produced nothing usable.")
            return ToolEvaluation(True, f"{self.name}_complete", f"{self.name} produced {len(result['items'])} item(s).")

        def repair(plan, _result, evaluation):
            if plan["method"] == "deterministic" or evaluation.code != f"{self.name}_empty":
                return None
            return {"method": "deterministic", "repair": f"fallback_{self.name}"}

        loop = run_tool_with_repair(
            {"method": method},
            execute=execute,
            evaluate=evaluate,
            repair=repair,
            max_attempts=2,
        )
        return loop.result["items"], {
            "stage": self.name,
            "method": loop.result["method"],
            "count": len(loop.result["items"]),
            "trace": loop.trace(),
        }


# Stage 1: brief ingestion


def brief_units(brief):
    """The current structure of the brief under test.

    A native draft is re-read from its components every run, so a rerun tests
    what the document says now rather than what it said when it was attached.
    """
    if brief.source_type == GymDocument.DRAFT_DOCUMENT and brief.draft_document:
        draft = brief.draft_document
        units = ingestion.units_from_sections(draft.sections)
        text = draft.plain_text or ""
        brief.extracted_text = text
        brief.extraction_metadata = {
            "extractor": "draft_components",
            "pageCount": 0,
            "paragraphCount": len(draft.sections or []),
            "units": units,
            "checksum": ingestion.text_checksum(text),
        }
        brief.save(update_fields=["extracted_text", "extraction_metadata", "updated_at"])
        return units
    return brief.structure_units


def brief_snapshot(brief):
    """What the brief was when a run read it, in whatever terms it has identity."""
    snapshot = {"checksum": (brief.extraction_metadata or {}).get("checksum", "")}
    if brief.source_type == GymDocument.DRAFT_DOCUMENT and brief.draft_document:
        draft = brief.draft_document
        snapshot["draftId"] = draft.id
        snapshot["componentVersions"] = {
            component.stable_key: getattr(component.current_version, "id", None)
            for component in draft.components.filter(removed_at__isnull=True).prefetch_related("versions")
        }
    return snapshot


def _units_of_type(units, *types):
    return [unit for unit in units if unit["type"] in types]


def _unit_payload(units, *, limit=None, types=None):
    selected = [unit for unit in units if not types or unit["type"] in types]
    return [
        {
            "id": unit["id"],
            "type": unit["type"],
            "section": unit["locator"]["section"],
            "paragraph": unit["locator"]["paragraph"],
            "page": unit["locator"]["page"],
            "text": unit["text"],
        }
        for unit in selected[:limit]
    ]


# Stage 2: argument map


def _fallback_argument_map(units):
    claims = []
    for unit in _units_of_type(units, ingestion.ARGUMENT, ingestion.REQUESTED_RELIEF):
        if unit.get("parentId"):
            continue
        citations = [
            citation["text"]
            for citation in units
            if citation["type"] == ingestion.CITATION and citation.get("parentId") == unit["id"]
        ]
        claims.append(
            {
                "unitId": unit["id"],
                "proposition": unit["text"][:300],
                "reliefSought": unit["text"][:200] if unit["type"] == ingestion.REQUESTED_RELIEF else "",
                "elements": [],
                "citedAuthority": citations,
                "assertedFacts": [],
                "impliedSteps": [],
                "weakestLink": "" if citations else "This passage argues without citing authority.",
            }
        )
    return claims[:12]


def argument_map_stage(units, *, brief_title, jurisdiction, matter_summary, llm_client=None):
    unit_ids = {unit["id"] for unit in units}

    def parse(payload):
        claims = payload.get("claims")
        if not isinstance(claims, list):
            return []
        cleaned = []
        for claim in claims:
            if not isinstance(claim, dict) or claim.get("unitId") not in unit_ids:
                continue
            cleaned.append(
                {
                    "unitId": claim["unitId"],
                    "proposition": clean(claim.get("proposition"), limit=600),
                    "reliefSought": clean(claim.get("reliefSought"), limit=400),
                    "elements": [clean(item, limit=300) for item in claim.get("elements") or [] if clean(item)],
                    "citedAuthority": [clean(item, limit=200) for item in claim.get("citedAuthority") or [] if clean(item)],
                    "assertedFacts": [clean(item, limit=300) for item in claim.get("assertedFacts") or [] if clean(item)],
                    "impliedSteps": [clean(item, limit=300) for item in claim.get("impliedSteps") or [] if clean(item)],
                    "weakestLink": clean(claim.get("weakestLink"), limit=400),
                }
            )
        return [claim for claim in cleaned if claim["proposition"]][:12]

    return Stage("argument_map", llm_client=llm_client).run(
        prompt_key="argument_gym.argument_map",
        context={
            "brief_title": brief_title,
            "jurisdiction": jurisdiction,
            "matter_summary": matter_summary,
            "brief_units": dumps(_unit_payload(units)),
        },
        parse=parse,
        fallback=lambda: _fallback_argument_map(units),
    )


# Stage 3: brief-to-record support check


def _terms(text):
    return {term for term in re.findall(r"[a-z0-9]{4,}", str(text or "").casefold())}


def _fallback_record_audit(fact_units, excerpts):
    findings = []
    for unit in fact_units:
        unit_terms = _terms(unit["text"])
        if not unit_terms:
            continue
        best = None
        best_overlap = 0
        for excerpt in excerpts:
            overlap = len(unit_terms & _terms(excerpt["text"]))
            if overlap > best_overlap:
                best, best_overlap = excerpt, overlap
        supported = best_overlap >= max(3, len(unit_terms) // 6)
        findings.append(
            {
                "unitId": unit["id"],
                "status": "supported" if supported else "unsupported",
                "explanation": (
                    f"Wording from this assertion also appears in {best['title']}."
                    if supported
                    else "No supplied case material repeats the terms of this assertion."
                ),
                "materialIds": [best["id"]] if supported and best else [],
                "quote": "",
            }
        )
    return findings


def record_audit_stage(units, excerpts, argument_map, *, jurisdiction, llm_client=None):
    if not excerpts:
        return [], {"stage": "record_audit", "method": "skipped", "count": 0, "trace": []}
    fact_units = _units_of_type(units, ingestion.ASSERTED_FACT, ingestion.ARGUMENT)
    unit_ids = {unit["id"] for unit in fact_units}
    material_ids = {excerpt["id"] for excerpt in excerpts}
    statuses = {"supported", "unsupported", "contradicted", "partially_supported"}

    def parse(payload):
        findings = payload.get("findings")
        if not isinstance(findings, list):
            return []
        cleaned = []
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("unitId") not in unit_ids:
                continue
            cleaned.append(
                {
                    "unitId": finding["unitId"],
                    "status": choice(finding.get("status"), statuses, "unsupported"),
                    "explanation": clean(finding.get("explanation"), limit=800),
                    "materialIds": [item for item in finding.get("materialIds") or [] if item in material_ids],
                    "quote": clean(finding.get("quote"), limit=600),
                }
            )
        return cleaned

    return Stage("record_audit", llm_client=llm_client).run(
        prompt_key="argument_gym.record_audit",
        context={
            "jurisdiction": jurisdiction,
            "argument_map": dumps(argument_map),
            "asserted_facts": dumps(_unit_payload(fact_units)),
            "record_excerpts": dumps(excerpts),
        },
        parse=parse,
        fallback=lambda: _fallback_record_audit(fact_units, excerpts),
    )


# Stage 4: adversarial research queries


def _fallback_research_queries(argument_map, jurisdiction):
    queries = []
    for claim in argument_map:
        subject = claim["proposition"]
        terms = " ".join(re.findall(r"[A-Za-z][A-Za-z'-]{3,}", subject)[:8])
        if not terms:
            continue
        queries.append(
            {
                "query": f"{terms} {jurisdiction}".strip(),
                "targets": [claim["unitId"]],
                "purpose": "Authority bearing on this proposition, including authority against it.",
            }
        )
        if len(queries) >= MAX_QUERIES:
            break
    return queries


def research_queries_stage(argument_map, record_findings, *, jurisdiction, llm_client=None):
    unit_ids = {claim["unitId"] for claim in argument_map}

    def parse(payload):
        queries = payload.get("queries")
        if not isinstance(queries, list):
            return []
        cleaned = []
        seen = set()
        for query in queries:
            if not isinstance(query, dict):
                continue
            text = clean(query.get("query"), limit=300)
            targets = [target for target in query.get("targets") or [] if target in unit_ids]
            if not text or not targets or text.casefold() in seen:
                continue
            seen.add(text.casefold())
            cleaned.append({"query": text, "targets": targets, "purpose": clean(query.get("purpose"), limit=300)})
        return cleaned[:MAX_QUERIES]

    return Stage("research_queries", llm_client=llm_client).run(
        prompt_key="argument_gym.research_queries",
        context={
            "jurisdiction": jurisdiction,
            "argument_map": dumps(argument_map),
            "record_findings": dumps(record_findings),
            "max_queries": MAX_QUERIES,
        },
        parse=parse,
        fallback=lambda: _fallback_research_queries(argument_map, jurisdiction),
    )


# Stage 5: retrieval


def run_research(queries, *, matter, jurisdiction, user, request, registry, source_ids=None):
    """Run the adversarial queries through the existing augmented search."""
    registry = registry or default_connector_registry
    sources = []
    trace = []
    seen = set()
    for query in queries:
        selection = automatic_source_selection(query["query"], matter=matter)
        selected_ids = source_ids or selection["source_ids"]
        payload = augmented_search(
            query["query"],
            connector_registry=registry,
            kinds=source_kinds(selected_ids),
            source_ids=selected_ids,
            matter=matter,
            jurisdiction=jurisdiction,
            limit_per_source=4,
            user=user,
            request=request,
            max_rounds=2,
            allow_source_expansion=source_ids is None,
        )
        for result in payload["results"]:
            key = (result.source_kind, str(result.id))
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "id": str(len(sources) + 1),
                    "title": result.title,
                    "citation": result.citation,
                    "snippet": result.snippet,
                    "sourceKind": result.source_kind,
                    "sourceLabel": result.source_label,
                    "url": result.url,
                    "externalId": str(result.id),
                    "queries": [query["query"]],
                }
            )
        trace.append(
            {
                "query": query["query"],
                "purpose": query.get("purpose", ""),
                "targets": query.get("targets", []),
                "sourceIds": payload["selected_source_ids"],
                "resultCount": len(payload["results"]),
                "augmentation": payload["augmentation"],
            }
        )
    return sources, trace


def research_coverage(trace):
    """Whether the adversarial research actually found anything to argue from."""
    evaluations = [item["augmentation"].get("finalEvaluation") or {} for item in trace]
    reasons = sorted({reason for evaluation in evaluations for reason in evaluation.get("reasons") or []})
    return {
        "queries": [item["query"] for item in trace],
        "resultCount": sum(item["resultCount"] for item in trace),
        "adequate": bool(trace) and all(evaluation.get("adequate") for evaluation in evaluations),
        "gaps": reasons,
    }


# Stage 6: opponent


def _fallback_attacks(units, argument_map, record_findings, legal_sources):
    findings_by_unit = {finding["unitId"]: finding for finding in record_findings}
    attacks = []

    def add(unit_id, category, argument, why, source_ids=(), material_ids=()):
        attacks.append(
            {
                "id": f"a{len(attacks) + 1}",
                "unitId": unit_id,
                "category": category,
                "argument": argument,
                "whyItMatters": why,
                "legalSourceIds": list(source_ids),
                "recordMaterialIds": list(material_ids),
            }
        )

    for claim in argument_map:
        finding = findings_by_unit.get(claim["unitId"])
        if finding and finding["status"] in {"unsupported", "contradicted"}:
            add(
                claim["unitId"],
                GymChallenge.RECORD_CONFLICT if finding["status"] == "contradicted" else GymChallenge.FACTUAL_SUPPORT,
                f"The record does not establish what this passage assumes. {finding['explanation']}",
                "An assertion the record does not carry is one opposing counsel can ask the court to disregard.",
                material_ids=finding["materialIds"],
            )
        if not claim["citedAuthority"]:
            add(
                claim["unitId"],
                GymChallenge.LEGAL_AUTHORITY,
                "This proposition is advanced without citing authority, so the court has nothing to check it against.",
                "An uncited proposition is the cheapest thing for an opponent to contest.",
                source_ids=[source["id"] for source in legal_sources[:1]],
            )
        if claim["weakestLink"]:
            add(
                claim["unitId"],
                GymChallenge.MISSING_ELEMENT,
                f"The weakest step here is exposed: {claim['weakestLink']}",
                "An argument fails at its weakest step, not its strongest.",
                source_ids=[source["id"] for source in legal_sources[:1]],
            )
        if len(attacks) >= MAX_ATTACKS:
            break

    return attacks[:MAX_ATTACKS]


def opponent_stage(units, argument_map, record_findings, legal_sources, *, jurisdiction, matter_summary, llm_client=None):
    unit_ids = {unit["id"] for unit in units}
    source_ids = {source["id"] for source in legal_sources}
    material_ids = {material for finding in record_findings for material in finding["materialIds"]}

    def parse(payload):
        attacks = payload.get("attacks")
        if not isinstance(attacks, list):
            return []
        cleaned = []
        for index, attack in enumerate(attacks, start=1):
            if not isinstance(attack, dict) or attack.get("unitId") not in unit_ids:
                continue
            argument = clean(attack.get("argument"), limit=1500)
            if not argument:
                continue
            cleaned.append(
                {
                    "id": clean(attack.get("id"), limit=20) or f"a{index}",
                    "unitId": attack["unitId"],
                    "category": choice(attack.get("category"), CATEGORIES, GymChallenge.LEGAL_AUTHORITY),
                    "argument": argument,
                    "whyItMatters": clean(attack.get("whyItMatters"), limit=800),
                    "legalSourceIds": [str(item) for item in attack.get("legalSourceIds") or [] if str(item) in source_ids],
                    "recordMaterialIds": [item for item in attack.get("recordMaterialIds") or [] if item in material_ids],
                }
            )
        return cleaned[:MAX_ATTACKS]

    return Stage("opponent", llm_client=llm_client).run(
        prompt_key="argument_gym.opponent",
        context={
            "jurisdiction": jurisdiction,
            "matter_summary": matter_summary,
            "argument_map": dumps(argument_map),
            "brief_units": dumps(_unit_payload(units)),
            "record_findings": dumps(record_findings),
            "legal_sources": dumps(legal_sources),
            "max_attacks": MAX_ATTACKS,
        },
        parse=parse,
        fallback=lambda: _fallback_attacks(units, argument_map, record_findings, legal_sources),
        temperature=0.4,
        empty_key="attacks",
    )


# Stage 7: judge


SEVERITY_BY_CATEGORY = {
    GymChallenge.RECORD_CONFLICT: "high",
    GymChallenge.MISSING_ELEMENT: "high",
    GymChallenge.LEGAL_AUTHORITY: "medium",
    GymChallenge.FACTUAL_SUPPORT: "medium",
    GymChallenge.PROCEDURAL: "medium",
    GymChallenge.REMEDY_SCOPE: "low",
    GymChallenge.FRAMING: "low",
}
SEVERITY_WEIGHT = {"high": 85, "medium": 55, "low": 30}


def _fallback_assessments(attacks):
    assessments = []
    for attack in attacks:
        grounded = bool(attack["legalSourceIds"] or attack["recordMaterialIds"])
        severity = SEVERITY_BY_CATEGORY.get(attack["category"], "medium")
        assessments.append(
            {
                "attackId": attack["id"],
                "keep": True,
                "verdict": "answerable",
                "finding": "Check this passage against its sources",
                "reviewAction": "verify",
                "assessment": (
                    "A source was supplied, but this fallback did not assess whether it supports the challenge."
                    if grounded
                    else "No source supports this fallback concern. Verify it before changing the brief."
                ),
                "briefCurrentlySays": "",
                "severity": severity,
                "importance": SEVERITY_WEIGHT[severity] + (10 if grounded else 0),
                "confidence": "low",
                "coverageNote": "" if grounded else "No retrieved authority bears on this yet.",
            }
        )
    assessments.sort(key=lambda item: item["importance"], reverse=True)
    for rank, assessment in enumerate(assessments):
        assessment["keep"] = rank < MAX_CHALLENGES
    return assessments


def select_challenges(attacks, assessments):
    """Keep the judge's supported decisions, preserving rejected items in trace."""
    by_id = {item["attackId"]: item for item in assessments}
    candidates = [attack for attack in attacks
                  if by_id.get(attack["id"], {}).get("keep") is True
                  and by_id[attack["id"]].get("verdict") in {"serious", "answerable"}]
    candidate_ids = {attack["id"] for attack in candidates}
    # Only suppress a duplicate when its retained representative exists. Bad
    # references and cycles must not silently delete substantive findings.
    candidates = [attack for attack in candidates
                  if not (by_id[attack["id"]].get("duplicateOf") in candidate_ids
                          and not by_id[by_id[attack["id"]]["duplicateOf"]].get("duplicateOf"))]
    return sorted(candidates, key=lambda attack: -by_id[attack["id"]].get("importance", 0))[:MAX_CHALLENGES]


def readable_references(text, units):
    """Internal model anchors are useful links, not prose for the advocate."""
    labels = {}
    for unit in units:
        locator = unit.get("locator") or {}
        parts = []
        if locator.get("section"):
            parts.append(f"section {locator['section']}")
        if locator.get("paragraph"):
            parts.append(f"paragraph {locator['paragraph']}")
        labels[unit["id"]] = ", ".join(parts) or "the cited passage"
    return re.sub(r"\bu\d+\b", lambda match: labels.get(match[0], match[0]), text)


def judge_stage(units, argument_map, attacks, legal_sources, *, jurisdiction, llm_client=None):
    attack_ids = {attack["id"] for attack in attacks}
    verdicts = {"serious", "answerable", "weak", "misplaced"}

    def parse(payload):
        assessments = payload.get("assessments")
        if not isinstance(assessments, list):
            return []
        cleaned = []
        for assessment in assessments:
            if not isinstance(assessment, dict) or assessment.get("attackId") not in attack_ids:
                continue
            try:
                importance = int(assessment.get("importance", 50))
            except (TypeError, ValueError):
                importance = 50
            cleaned.append(
                {
                    "attackId": assessment["attackId"],
                    "keep": assessment.get("keep") is True and assessment.get("verdict") in {"serious", "answerable"},
                    "finding": clean(assessment.get("finding"), limit=160),
                    "reviewAction": choice(assessment.get("reviewAction"), {"revise", "verify", "strategy"}, "verify"),
                    "duplicateOf": clean(assessment.get("duplicateOf"), limit=20),
                    "verdict": choice(assessment.get("verdict"), verdicts, "answerable"),
                    "assessment": readable_references(clean(assessment.get("assessment"), limit=1200), units),
                    "briefCurrentlySays": clean(assessment.get("briefCurrentlySays"), limit=800),
                    "severity": choice(assessment.get("severity"), SEVERITIES, "medium"),
                    "importance": max(0, min(importance, 100)),
                    "confidence": choice(assessment.get("confidence"), CONFIDENCES, "medium"),
                    "coverageNote": clean(assessment.get("coverageNote"), limit=600),
                }
            )
        return cleaned

    return Stage("judge", llm_client=llm_client).run(
        prompt_key="argument_gym.judge",
        context={
            "jurisdiction": jurisdiction,
            "argument_map": dumps(argument_map),
            "brief_units": dumps(_unit_payload(units)),
            "legal_sources": dumps(legal_sources),
            "attacks": dumps(attacks),
            "max_kept": MAX_CHALLENGES,
        },
        parse=parse,
        fallback=lambda: _fallback_assessments(attacks),
        temperature=0.1,
        empty_key="assessments" if not attacks else None,
    )


# Stage 8: coach


def _fallback_responses(challenges):
    responses = []
    for challenge in challenges:
        target = challenge["target"]["section"] or "this passage"
        responses.append(
            {
                "attackId": challenge["attackId"],
                "recommendation": (
                    f"Answer this in {target}: say plainly why the point does not defeat the argument, "
                    "or narrow the argument so it does not depend on the contested step."
                ),
                "suggestedResponse": "",
                "blockInstruction": (
                    f"Address this opposition argument without asserting new facts: {challenge['argument'][:300]}"
                ),
                "remainingVulnerability": "",
            }
        )
    return responses


def coach_stage(units, challenges, legal_sources, record_findings, *, jurisdiction, llm_client=None):
    attack_ids = {challenge["attackId"] for challenge in challenges}
    actions = {challenge["attackId"]: challenge.get("reviewAction") for challenge in challenges}

    def parse(payload):
        responses = payload.get("responses")
        if not isinstance(responses, list):
            return []
        cleaned = []
        for response in responses:
            if not isinstance(response, dict) or response.get("attackId") not in attack_ids:
                continue
            cleaned.append(
                {
                    "attackId": response["attackId"],
                    "recommendation": readable_references(clean(response.get("recommendation"), limit=1200), units),
                    "suggestedResponse": ("" if actions.get(response["attackId"]) in {"verify", "strategy"}
                                          else readable_references(clean(response.get("suggestedResponse"), limit=3000), units)),
                    "blockInstruction": clean(response.get("blockInstruction"), limit=1000),
                    "remainingVulnerability": clean(response.get("remainingVulnerability"), limit=800),
                }
            )
        return cleaned

    return Stage("coach", llm_client=llm_client).run(
        prompt_key="argument_gym.coach",
        context={
            "jurisdiction": jurisdiction,
            "brief_units": dumps(_unit_payload(units)),
            "legal_sources": dumps(legal_sources),
            "record_findings": dumps(record_findings),
            "challenges": dumps(challenges),
        },
        parse=parse,
        fallback=lambda: _fallback_responses(challenges),
        empty_key="responses" if not challenges else None,
    )


# Stage 9: the headline assessment


SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
MAX_ASSESSMENT_WORDS = 130


def _fallback_assessment(challenges):
    """A plain reading of what the run found, when no model wrote one."""
    if not challenges:
        return [{
            "verdict": "no challenges raised",
            "assessment": (
                "This run raised no challenges against the brief. That is a statement about the review, "
                "not a finding that the brief is sound: check the research coverage below before relying on it."
            ),
        }]
    ranked = sorted(challenges, key=lambda item: (SEVERITY_RANK.get(item["severity"], 1), -item["importance"]))
    leading = ranked[:2]
    verdict = "review the identified concerns"
    opening = (
        f"This run identified {len(challenges)} concerns to review. "
        "Verification tasks require checking the named source before changing the brief; "
        "they are not established defects. This summary does not assess the brief's overall persuasiveness."
    )
    points = " ".join(
        f"{index}. {item['categoryLabel']} at {item['target'].get('section') or 'the passage cited'}: "
        f"{item['argument'][:160].rstrip()}."
        for index, item in enumerate(leading, start=1)
    )
    return [{
        "verdict": verdict,
        "assessment": f"{opening} The most important to address: {points}",
    }]


def _one_paragraph(text, *, max_words=MAX_ASSESSMENT_WORDS):
    """Keep the first paragraph, capped. The report has room for one."""
    paragraph = re.split(r"\n\s*\n", str(text or "").strip())[0]
    paragraph = re.sub(r"\s+", " ", paragraph).strip()
    words = paragraph.split()
    if len(words) <= max_words:
        return paragraph
    return " ".join(words[:max_words]).rstrip(",;:") + "..."


def assessment_stage(challenges, coverage, *, brief_title, jurisdiction, matter_summary, llm_client=None):
    def parse(payload):
        # Measured against the column, not a number chosen here. A verdict
        # longer than the column takes the whole run down at the final save --
        # after every model call has been paid for -- and the two limits drifted
        # apart precisely because nothing tied them together.
        verdict = clean(payload.get("verdict"), limit=VERDICT_LIMIT)
        assessment = _one_paragraph(payload.get("assessment"))
        if not assessment:
            return []
        return [{"verdict": verdict, "assessment": assessment}]

    return Stage("assessment", llm_client=llm_client).run(
        prompt_key="argument_gym.assessment",
        context={
            "jurisdiction": jurisdiction or "the filing jurisdiction",
            "brief_title": brief_title,
            "matter_summary": matter_summary,
            "challenges": dumps(challenges),
            "coverage": dumps(coverage),
        },
        parse=parse,
        fallback=lambda: _fallback_assessment(challenges),
        temperature=0.1,
    )


# Filing-format compliance


def resolve_court(workspace, brief_text, *, matter=None):
    """Whose filing rules apply, and why -- chosen by hand or detected.

    Detection is string matching against maintained court profiles, not an
    inference, so a run can always say which phrase decided it.
    """
    if workspace.court_rule_mode == GymWorkspace.OFF:
        return None, {"mode": "off", "reason": "Filing-format rules were turned off for this workspace."}
    if workspace.court_rule_mode == GymWorkspace.MANUAL:
        if workspace.court:
            return workspace.court, {
                "mode": "manual",
                "reason": f"{workspace.court.name} was selected for this workspace.",
                "courtSlug": workspace.court.slug,
            }
        return None, {"mode": "manual", "reason": "No court is selected, so filing-format rules were not applied."}
    detection = detect_court(brief_text, matter=matter)
    return detection["profile"], {
        "mode": "auto",
        "reason": detection["reason"],
        "matched": detection["matched"],
        "where": detection["where"],
        "courtSlug": detection["profile"].slug if detection["profile"] else "",
    }


def compliance_stage(brief, workspace, brief_text, *, matter=None):
    """Deterministic filing-format checks. No model call belongs in this answer."""
    profile, detection = resolve_court(workspace, brief_text, matter=matter)
    pleading_type = brief.pleading_type or detect_pleading_type(brief_text, title=brief.title)["pleadingType"]
    formatting = (brief.extraction_metadata or {}).get("formatting") or {}
    compliance = check_court_compliance(
        profile=profile,
        formatting=formatting,
        text=brief_text,
        pleading_type=pleading_type,
        document_id=brief.id,
    )
    compliance["detection"] = detection
    compliance["pleadingType"] = pleading_type
    return profile, compliance, {
        "stage": "compliance",
        "method": "deterministic",
        "count": len(compliance.get("findings", [])),
        "trace": [],
    }


# Deterministic document checks the author selected


def readability_findings(text, document_id):
    """Reading-level measures, reported as several formulas rather than one score."""
    report = check_readability(text, kind="filing")
    findings = []
    for index, finding in enumerate(report.findings, start=1):
        severity = finding.severity if finding.severity in {"error", "warning", "info"} else "info"
        findings.append(
            {
                "findingId": f"readability-{document_id}-{index}",
                "ruleCode": f"{SEVERITY_PREFIX[severity]}1150",
                "severity": severity,
                "outcome": "review" if severity != "info" else "pass",
                "category": "readability",
                "target": finding.rule,
                "location": {"view": "json", "blockKey": None, "sectionLabel": None, "lineStart": None, "lineEnd": None, "excerpt": ""},
                "message": finding.message,
                "action": {"type": "human_review", "label": "Read the passage rather than moving the number.", "payload": {}},
                "manualReview": severity != "info",
                "details": {"metrics": report.metrics},
            }
        )
    return findings, summarize_readability(report)


def draft_validation_findings(brief):
    """The same checks Draft mode runs, for a brief that is a native draft."""
    from apps.validation.services import validate_document

    draft = brief.draft_document
    if not draft:
        return []
    try:
        # The Word render is the slowest part and can fail on a template this
        # session does not own; its absence is not a finding about the brief.
        return validate_document(draft, include_docx=True)
    except Exception:  # noqa: BLE001 - a validation crash must not fail the run
        return validate_document(draft, include_docx=False)


def run_document_checks(plan, brief, *, text, pleading_type, attached_labels, settings_by_check):
    """Every deterministic check the author left on, keyed by which check found it."""
    results = {}
    if check_catalog.will_run(plan, "pleading_form"):
        results["pleading_form"] = {
            "findings": check_pleading_form(
                text,
                pleading_type=pleading_type,
                document_id=brief.id,
                attached_labels=attached_labels,
            )
        }
    language_parts = [
        ("grammar", ("grammar",)),
        ("confused_words", ("confused_words", "confusable_pairs")),
        ("passive_voice", ("passive_voice",)),
    ]
    for check_id, include in language_parts:
        if not check_catalog.will_run(plan, check_id):
            continue
        accepted = (settings_by_check.get("passive_voice") or {}).get("acceptedPassivePhrases") or []
        results[check_id] = {
            "findings": check_language(
                text,
                document_id=brief.id,
                include=include,
                accepted_passive=accepted,
            )
        }
    if check_catalog.will_run(plan, "readability"):
        findings, summary = readability_findings(text, brief.id)
        results["readability"] = {"findings": findings, "summary": summary}
    if check_catalog.will_run(plan, "draft_validation"):
        results["draft_validation"] = {"findings": draft_validation_findings(brief)}
    return results


def anchor_unit(units, *args):
    """The passage a finding should point at, from the words it quoted.

    A finding that cannot name where it lands is much harder to act on, so this
    falls back to the first argument in the brief rather than to nothing.
    """
    for text in args:
        needle = re.sub(r"\s+", " ", str(text or "")).strip()[:60]
        if len(needle) < 12:
            continue
        for unit in units:
            if needle.casefold() in re.sub(r"\s+", " ", unit["text"]).casefold():
                return unit
    return next(
        (unit for unit in units if unit["type"] in {ingestion.ARGUMENT, ingestion.REQUESTED_RELIEF}),
        units[0] if units else None,
    )


def attacks_from_rule_audit(audits, units, start_index):
    """A rule element the brief did not carry, in the shape every other attack has."""
    from apps.argument_gym.rule_audit import challenges_from_audit

    attacks = []
    for offset, attack in enumerate(challenges_from_audit(audits), start=start_index):
        unit = anchor_unit(units, attack.get("quote"))
        if not unit:
            continue
        attacks.append(
            {
                "id": f"r{offset}",
                "unitId": unit["id"],
                "category": GymChallenge.MISSING_ELEMENT,
                "argument": attack["argument"],
                "whyItMatters": attack["whyItMatters"],
                "legalSourceIds": [],
                "recordMaterialIds": attack.get("materialIds", []),
                "origin": f"rule:{attack['ruleSlug']}",
            }
        )
    return attacks


def attacks_from_checklist(applied, units, start_index):
    from apps.argument_gym.checklist import checklist_challenges

    attacks = []
    for offset, attack in enumerate(checklist_challenges(applied), start=start_index):
        unit = anchor_unit(units, attack.get("suggestion"))
        if not unit:
            continue
        attacks.append(
            {
                "id": f"c{offset}",
                "unitId": unit["id"],
                "category": GymChallenge.FRAMING,
                "argument": attack["argument"],
                "whyItMatters": attack["whyItMatters"],
                "legalSourceIds": [],
                "recordMaterialIds": [],
                "origin": f"checklist:{attack['itemId']}",
            }
        )
    return attacks


# Assembly


def challenge_fingerprint(category, target, argument):
    """Identify a challenge across runs by what it says, not where it landed.

    A re-uploaded brief renumbers its units, so the unit id cannot carry
    identity. The block key can, when there is one.
    """
    anchor = target.get("blockKey") or target.get("section") or ""
    normalized = re.sub(r"[^a-z0-9 ]+", " ", f"{category} {anchor} {argument}".casefold())
    return hashlib.sha256(" ".join(normalized.split()).encode("utf-8")).hexdigest()


def _target_for(unit, units_by_id):
    parent = units_by_id.get(unit.get("parentId", ""), unit)
    return {
        "unitId": unit["id"],
        "section": unit["locator"]["section"],
        "paragraph": unit["locator"]["paragraph"],
        "page": unit["locator"]["page"],
        "excerpt": parent["text"][:300],
        "blockKey": unit.get("blockKey", ""),
    }


def compare_with_previous(run, challenges):
    """Say which challenges recurred, which are new, and which stopped being raised."""
    previous_run = run.previous_run
    if not previous_run:
        return {}
    previous = list(previous_run.challenges.all())
    current_by_fingerprint = {challenge.fingerprint: challenge for challenge in challenges}
    recurring = []
    resolved = []
    for earlier in previous:
        match = current_by_fingerprint.get(earlier.fingerprint)
        if match:
            match.carried_from = earlier
            if earlier.disposition == GymChallenge.DISMISSED:
                # A dismissal is a judgment about the argument, and the argument
                # has not changed. Marking it addressed is not: the brief moved,
                # and the challenge came back anyway.
                match.disposition = GymChallenge.DISMISSED
                match.disposition_note = earlier.disposition_note
            match.save(update_fields=["carried_from", "disposition", "disposition_note", "updated_at"])
            recurring.append({"challengeId": match.id, "previousChallengeId": earlier.id, "previousDisposition": earlier.disposition})
        else:
            resolved.append(
                {
                    "previousChallengeId": earlier.id,
                    "category": earlier.category,
                    "argument": earlier.opponent_argument[:300],
                    "previousDisposition": earlier.disposition,
                }
            )
    return {
        "previousRunId": previous_run.id,
        "recurring": recurring,
        "resolved": resolved,
        "new": [
            {"challengeId": challenge.id}
            for challenge in challenges
            if challenge.carried_from_id is None
        ],
    }


def execute_run(run, *, user=None, request=None, llm_client=None, connector_registry=None):
    """Run every stage and persist the challenges. Never raises into the view."""
    run.status = GymRun.RUNNING
    run.save(update_fields=["status"])

    def note_stage(stage):
        """Save each stage as it finishes, so a client polling can say where it is.

        A run takes minutes. Without this the only observable states are
        "running" and "done", and an advocate watching a blank panel cannot tell
        a slow research round from a wedged one.
        """
        stages.append(stage)
        run.stage_trace = list(stages)
        run.save(update_fields=["stage_trace"])

    workspace = run.workspace
    matter = workspace.matter
    jurisdiction = workspace.jurisdiction or getattr(matter, "jurisdiction", "") or ""
    matter_summary = dumps(compact_case_context(matter)) if matter else "No case record was provided."
    stages = []

    try:
        units = brief_units(run.brief)
        if not units:
            raise ValueError("This brief has no readable text to test.")
        run.snapshot = brief_snapshot(run.brief)
        units_by_id = {unit["id"]: unit for unit in units}

        brief_text = run.brief.extracted_text
        attached_labels = [
            (document.extraction_metadata or {}).get("exhibitLabel")
            or document.title.split(" (pages")[0]
            for document in workspace.documents.filter(split_from=run.brief)
        ]
        court, detection = resolve_court(workspace, brief_text, matter=matter)
        available_materials = record.included_materials(workspace)

        # The author's selection is resolved before anything runs, so the run can
        # say what it did not do as precisely as what it did.
        plan = check_catalog.plan_checks(
            workspace.enabled_checks,
            {
                "native_draft": bool(run.brief.draft_document_id),
                "court_profile": court is not None,
                "case_record": bool(available_materials),
                "checklist": workspace.checklist_id is not None,
            },
        )
        run.checks_run = plan

        # Deterministic first: whether the paper meets the court's filing rules
        # is a fact about the document, and an advocate should get that answer
        # even if every model call after this one fails.
        if check_catalog.will_run(plan, "court_formatting"):
            court, compliance, trace = compliance_stage(run.brief, workspace, brief_text, matter=matter)
            run.court = court
            run.compliance = compliance
            run.court_detection = compliance["detection"]
            note_stage(trace)
        else:
            run.court = court
            run.court_detection = detection
            run.compliance = {
                "checked": False,
                "reason": next(
                    (entry["reason"] for entry in plan if entry["id"] == "court_formatting"),
                    "Filing-format rules were not applied.",
                ),
                "findings": [],
                "detection": detection,
            }

        pleading_type = run.brief.pleading_type or run.compliance.get("pleadingType", "")
        check_results = run_document_checks(
            plan,
            run.brief,
            text=brief_text,
            pleading_type=pleading_type,
            attached_labels=attached_labels,
            settings_by_check=workspace.check_settings or {},
        )
        if run.compliance.get("findings"):
            check_results["court_formatting"] = {"findings": run.compliance["findings"]}
        run.check_results = check_results
        note_stage(
            {
                "stage": "document_checks",
                "method": "deterministic",
                "count": sum(len(result["findings"]) for result in check_results.values()),
                "trace": [{"check": key, "count": len(value["findings"])} for key, value in check_results.items()],
            }
        )

        selected_materials, ranking_trace = record.rank_materials(
            available_materials,
            compact_case_context(matter) if matter else {},
            f"Test this brief against the case record: {run.brief.title}",
            limit=MAX_MATERIALS,
            llm_client=llm_client,
        )
        for material in selected_materials:
            reasons = {item["id"]: item.get("reason", "") for item in ranking_trace.get("selected", [])}
            material["reason"] = reasons.get(material["id"], "")
        excerpts = [
            {
                "id": material["id"],
                "title": material["title"],
                "text": record.material_text(material, workspace=workspace),
            }
            for material in selected_materials
        ]
        excerpts = [excerpt for excerpt in excerpts if excerpt["text"].strip()]
        note_stage({"stage": "materials", "method": ranking_trace.get("method", "none"), "count": len(excerpts), "trace": ranking_trace.get("trace", [])})

        argument_map, trace = argument_map_stage(
            units,
            brief_title=run.brief.title,
            jurisdiction=jurisdiction,
            matter_summary=matter_summary,
            llm_client=llm_client,
        )
        note_stage(trace)

        if check_catalog.will_run(plan, "record_audit"):
            record_findings, trace = record_audit_stage(
                units, excerpts, argument_map, jurisdiction=jurisdiction, llm_client=llm_client
            )
        else:
            record_findings, trace = [], {
                "stage": "record_audit",
                "method": "off",
                "count": 0,
                "trace": [],
            }
        note_stage(trace)

        queries, trace = research_queries_stage(
            argument_map, record_findings, jurisdiction=jurisdiction, llm_client=llm_client
        )
        note_stage(trace)

        legal_sources, research_trace = run_research(
            queries,
            matter=matter,
            jurisdiction=jurisdiction,
            user=user,
            request=request,
            registry=connector_registry,
            source_ids=(run.configuration or {}).get("sourceIds") or None,
        )
        note_stage({"stage": "research", "method": "retrieval", "count": len(legal_sources), "trace": []})

        adversarial = check_catalog.will_run(plan, "adversarial")
        if adversarial:
            attacks, trace = opponent_stage(
                units,
                argument_map,
                record_findings,
                legal_sources,
                jurisdiction=jurisdiction,
                matter_summary=matter_summary,
                llm_client=llm_client,
            )
        else:
            attacks, trace = [], {"stage": "opponent", "method": "off", "count": 0, "trace": []}
        note_stage(trace)

        # A rule the brief invoked without carrying its elements, and a failed
        # item from the author's own checklist, are challenges like any other:
        # they go into the same ranked cards, prep sheet, and revision plan.
        if check_catalog.will_run(plan, "rule_elements"):
            from apps.argument_gym.rule_audit import run_rule_audit

            audits, audit_traces = run_rule_audit(
                brief_text, excerpts, jurisdiction=jurisdiction, llm_client=llm_client
            )
            run.rule_audit = audits
            note_stage(
                {
                    "stage": "rule_elements",
                    "method": "deterministic" if not audit_traces else audit_traces[0]["method"],
                    "count": len(audits),
                    "trace": audit_traces,
                }
            )
            attacks.extend(attacks_from_rule_audit(audits, units, len(attacks) + 1))

        if check_catalog.will_run(plan, "custom_checklist"):
            from apps.argument_gym.checklist import apply_checklist

            applied = apply_checklist(
                workspace.checklist,
                brief_text=brief_text,
                brief_units=units,
                workspace=workspace,
                materials=selected_materials,
                matter_summary=matter_summary,
                jurisdiction=jurisdiction,
                user=user,
                request=request,
                registry=connector_registry,
                llm_client=llm_client,
            )
            run.checklist_results = applied
            note_stage(
                {
                    "stage": "custom_checklist",
                    "method": "model",
                    "count": len(applied["results"]),
                    "trace": [{"item": item["item"], "outcome": item["outcome"]} for item in applied["results"]],
                }
            )
            attacks.extend(attacks_from_checklist(applied, units, len(attacks) + 1))

        if adversarial:
            assessments, trace = judge_stage(
                units, argument_map, attacks, legal_sources, jurisdiction=jurisdiction, llm_client=llm_client
            )
        else:
            # Without the judge the cards still have to be ranked and shown, so
            # the deterministic assessment stands in and the trace says so.
            assessments = _fallback_assessments(attacks)
            trace = {"stage": "judge", "method": "off", "count": len(assessments), "trace": []}
        trace["assessments"] = assessments
        note_stage(trace)

        assessment_by_attack = {assessment["attackId"]: assessment for assessment in assessments}
        # A rejected attack must never be restored to fill a quota. Rank before
        # capping so a late, consequential finding is not silently dropped.
        kept = select_challenges(attacks, assessments)

        sources_by_id = {source["id"]: source for source in legal_sources}
        findings_by_unit = {finding["unitId"]: finding for finding in record_findings}
        coverage = research_coverage(research_trace)
        coach_input = []
        for attack in kept:
            unit = units_by_id[attack["unitId"]]
            assessment = assessment_by_attack.get(attack["id"], {})
            coach_input.append(
                {
                    "attackId": attack["id"],
                    "category": attack["category"],
                    "argument": attack["argument"],
                    "whyItMatters": attack["whyItMatters"],
                    "judgeAssessment": assessment.get("assessment", ""),
                    "finding": assessment.get("finding", ""),
                    "reviewAction": assessment.get("reviewAction", "verify"),
                    "briefCurrentlySays": assessment.get("briefCurrentlySays", "") or unit["text"][:600],
                    "target": _target_for(unit, units_by_id),
                    "legalSources": [sources_by_id[source_id] for source_id in attack["legalSourceIds"] if source_id in sources_by_id],
                }
            )

        if adversarial:
            responses, trace = coach_stage(
                units, coach_input, legal_sources, record_findings, jurisdiction=jurisdiction, llm_client=llm_client
            )
        else:
            responses = _fallback_responses(coach_input)
            trace = {"stage": "coach", "method": "off", "count": len(responses), "trace": []}
        note_stage(trace)
        response_by_attack = {response["attackId"]: response for response in responses}

        run.challenges.all().delete()
        challenges = []
        for ordinal, attack in enumerate(kept, start=1):
            unit = units_by_id[attack["unitId"]]
            assessment = assessment_by_attack.get(attack["id"], {})
            response = response_by_attack.get(attack["id"], {})
            finding = findings_by_unit.get(attack["unitId"])
            target = _target_for(unit, units_by_id)
            record_sources = [
                {
                    "materialId": material_id,
                    "title": next((material["title"] for material in selected_materials if material["id"] == material_id), material_id),
                    "status": finding["status"] if finding else "",
                    "quote": finding["quote"] if finding else "",
                }
                for material_id in dict.fromkeys([*attack["recordMaterialIds"], *(finding["materialIds"] if finding else [])])
            ]
            challenges.append(
                GymChallenge.objects.create(
                    run=run,
                    ordinal=ordinal,
                    category=attack["category"],
                    fingerprint=challenge_fingerprint(attack["category"], target, attack["argument"]),
                    target=target,
                    finding=assessment.get("finding", ""),
                    review_action=assessment.get("reviewAction", "verify"),
                    opponent_argument=attack["argument"],
                    why_it_matters=attack["whyItMatters"],
                    brief_currently_says=assessment.get("briefCurrentlySays", "") or unit["text"][:600],
                    legal_sources=[sources_by_id[source_id] for source_id in attack["legalSourceIds"] if source_id in sources_by_id],
                    record_sources=record_sources,
                    judge_assessment=assessment.get("assessment", ""),
                    judge_verdict=assessment.get("verdict", ""),
                    coaching_recommendation=response.get("recommendation", ""),
                    suggested_response=response.get("suggestedResponse", ""),
                    severity=assessment.get("severity", "medium"),
                    importance=assessment.get("importance", 50),
                    confidence=assessment.get("confidence", "medium"),
                    research_coverage={
                        **coverage,
                        "note": assessment.get("coverageNote", ""),
                        "blockInstruction": response.get("blockInstruction", ""),
                        "remainingVulnerability": response.get("remainingVulnerability", ""),
                    },
                )
            )

        assessment, trace = assessment_stage(
            [
                {
                    "categoryLabel": challenge.get_category_display(),
                    "severity": challenge.severity,
                    "importance": challenge.importance,
                    "finding": challenge.finding,
                    "reviewAction": challenge.review_action,
                    "confidence": challenge.confidence,
                    "target": challenge.target,
                    "argument": challenge.opponent_argument,
                    "judge": challenge.judge_assessment,
                    "response": challenge.suggested_response or challenge.coaching_recommendation,
                }
                for challenge in challenges
            ],
            coverage,
            brief_title=run.brief.title,
            jurisdiction=jurisdiction,
            matter_summary=matter_summary,
            llm_client=llm_client,
        )
        note_stage(trace)
        if assessment:
            run.assessment = assessment[0]["assessment"]
            run.assessment_verdict = assessment[0]["verdict"]

        run.materials = [record.public_material(material) for material in selected_materials]
        run.research_trace = research_trace
        run.stage_trace = stages
        run.comparison = compare_with_previous(run, challenges)
        run.status = GymRun.COMPLETE
        run.completed_at = timezone.now()
        run.save()
    except Exception as exc:  # noqa: BLE001 - a failed run is reported, never raised at the advocate
        run.status = GymRun.FAILED
        run.error = str(exc)
        run.stage_trace = stages
        run.completed_at = timezone.now()
        # Only the fields this handler sets. A plain save() rewrites every
        # column, including whichever one raised on the way in, so the handler
        # raises too and the row keeps saying "running" -- a run that hangs
        # forever instead of reporting the failure it already knows about.
        run.save(update_fields=["status", "error", "stage_trace", "completed_at"])
    return run


def start_run(run, *, user=None, request=None, llm_client=None, connector_registry=None):
    """Begin a run, on a background thread unless configured otherwise.

    A run is eight sequential model calls and several retrieval rounds. Holding
    an HTTP request open for that long does not merely feel slow: gunicorn kills
    the worker at its timeout, and a killed worker returns no headers at all, so
    the browser reports a CORS failure rather than anything an advocate could
    act on.

    The request therefore returns the run immediately, still pending, and the
    client polls it. `GymRun.status` already existed for this; nothing about the
    pipeline changes.
    """
    if not getattr(settings, "ARGUMENT_GYM_BACKGROUND_RUNS", True):
        return execute_run(run, user=user, request=request, llm_client=llm_client, connector_registry=connector_registry)

    def work():
        # The request's connection belongs to the request. A thread gets its own
        # and has to hand it back, or the pool leaks one per run.
        close_old_connections()
        try:
            execute_run(run, user=user, llm_client=llm_client, connector_registry=connector_registry)
        except Exception:  # noqa: BLE001 - execute_run records failure; this is the last resort
            logger.exception("Argument Gym run %s died outside its own error handling", run.id)
        finally:
            close_old_connections()

    # `request` is deliberately not passed to the thread: it belongs to the
    # response cycle that is about to end. Retrieval only reads the user from it.
    threading.Thread(target=work, name=f"gym-run-{run.id}", daemon=True).start()
    return run


def fail_if_stalled(run):
    """Report a run whose worker died, instead of one that claims to run forever.

    A replica restarted mid-run leaves a row saying "running" that nothing will
    ever finish. A client polling it would wait forever, which is worse than a
    failure it can retry.
    """
    if run.status != GymRun.RUNNING:
        return run
    limit = getattr(settings, "ARGUMENT_GYM_RUN_TIMEOUT_SECONDS", 1800)
    age = (timezone.now() - run.created_at).total_seconds()
    if age < limit:
        return run
    run.status = GymRun.FAILED
    run.error = (
        f"This run stopped reporting progress after {int(age // 60)} minutes and was "
        "most likely interrupted by a restart. Nothing was written to your draft. Run it again."
    )
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error", "completed_at"])
    return run

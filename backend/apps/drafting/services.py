import json
import re

from django.conf import settings
from django.db import OperationalError, ProgrammingError
from django.utils.text import slugify

from apps.ai.openai_client import OpenAIBackendError, OpenAICompatibleClient
from apps.ai.prompt_catalog import PromptCatalogError, PromptRenderError, render_prompt
from apps.ai.services import GenerationContext, drafting_ai
from apps.drafting import operations, opposing_filing
from apps.drafting.components import plain_text_from_sections, sync_components
from apps.drafting.field_answers import resolve_field_requests
from apps.drafting.packages import derive_relationships
from apps.drafting.source_bindings import bind_current_versions
from apps.drafting.models import DraftDocument
from apps.matters.client_letter_context import letter_template_fields
from apps.matters.document_context import chunk_text, custom_fields_inventory, get_case_documents, get_document_text, search_chunks, summarize_text
from apps.matters.models import MatterFact
from apps.matters.work_product import evidence_only
from apps.sources.models import SourceConfiguration
from apps.templates_app.field_questions import (
    KIND_NARRATIVE,
    KIND_UNUSABLE,
    KIND_VALUE,
    field_applies,
    field_keys_for_answer,
    template_field_requests,
)
from apps.templates_app.models import DocumentTemplate
from apps.templates_app.recommendations import recommend_templates
from apps.templates_app.serializers import template_to_dict
from apps.templates_app.template_variables import field_defaults


WORKFLOW_STEPS = [
    {
        "id": "setup",
        "label": "Choose document",
        "help": "Choose what you are drafting and confirm the template or drafting instructions.",
    },
    {
        "id": "facts_review",
        "label": "Review facts",
        "help": "Review the facts the draft may use. Suggested facts are preselected, but a human should confirm them.",
    },
    {
        "id": "support_review",
        "label": "Review support",
        "help": "Confirm authorities, examples, and references the draft may rely on. Case evidence belongs in the facts step.",
    },
    {
        "id": "law_review",
        "label": "Review legal issues",
        "help": "Approve or reject candidate legal issues before they activate draft sections.",
    },
    {
        "id": "outline_review",
        "label": "Approve outline",
        "help": "Review the sections and supporting inputs before generating prose.",
    },
    {
        "id": "draft_review",
        "label": "Review draft",
        "help": "Edit, refine, and save each generated section before validation.",
    },
    {
        "id": "validation",
        "label": "Validate",
        "help": "Run checks for missing facts, tentative language, citations, and length.",
    },
    {
        "id": "export",
        "label": "Export",
        "help": "Export only after the human reviewer is comfortable with the draft and remaining flags.",
    },
]
STEP_ORDER = [step["id"] for step in WORKFLOW_STEPS]

LEGACY_STATUS_MAP = {
    "case": "setup",
    "facts": "facts_review",
    "template": "setup",
    "law": "law_review",
    "draft": "draft_review",
}

SUPPORT_PURPOSE_LABELS = {
    "legal_authority": "Legal authority",
    "example_language": "Example language",
    "background_reference": "Background reference",
}
GENERIC_TEMPLATE_DESCRIPTIONS = {
    "prepared from the maintained original word template.",
}

FACT_TERM_GROUPS = {
    "notice": {"notice", "served", "service", "quit", "termination", "summons", "complaint"},
    "hearing": {"hearing", "trial", "court", "deadline", "continued", "continuance", "date"},
    "payment": {"rent", "payment", "paid", "balance", "ledger", "arrears", "money order", "receipt"},
    "conditions": {"repair", "repairs", "mold", "leak", "condition", "habitability", "inspection", "code"},
    "disability": {"disability", "disabled", "accommodation", "medical", "doctor", "records"},
    "assistance": {"assistance", "rental assistance", "application", "erap", "voucher", "subsidy"},
    "bankruptcy": {"bankruptcy", "debtor", "petition", "automatic stay", "discharge", "chapter"},
}
FACT_STOP_WORDS = {
    "about", "after", "again", "against", "because", "before", "being", "client", "could", "draft", "from",
    "have", "into", "matter", "other", "should", "tenant", "that", "their", "there", "these", "this", "with",
}
FACT_CATEGORY_REQUIRED_PATTERNS = {
    "rental-assistance": (
        "rental assistance",
        "emergency rental assistance",
        "erap",
        "assistance application",
    ),
}


# Workflow/status helpers


def normalize_status(status):
    return LEGACY_STATUS_MAP.get(status, status)


def workflow_step_payload():
    return WORKFLOW_STEPS


def _ordered_blocks(session):
    if not session.template:
        return []
    blocks = list(session.template.blocks.all())
    selected = set(session.selected_block_keys or [])
    if not selected:
        return blocks
    return [block for block in blocks if block.required or block.key in selected]


def initialize_session(session):
    if session.status in LEGACY_STATUS_MAP:
        session.status = normalize_status(session.status)
    if session.template and not session.selected_block_keys:
        session.selected_block_keys = drafting_ai.recommend_blocks(session.template, _selected_fact_slugs_for_blocks(session))
    if not session.selected_fact_ids:
        session.selected_fact_ids = recommend_fact_ids(session)
    session.save()
    return session


def _validate_transition(session, target_status):
    if target_status == "support_review" and not session.selected_fact_ids:
        raise ValueError("Review and select facts before choosing drafting support.")
    if target_status in {"law_review", "outline_review", "draft_review"} and not session.selected_fact_ids:
        raise ValueError("Review and select facts before continuing.")
    if target_status in {"outline_review", "draft_review"} and not session.selected_block_keys:
        raise ValueError("Select at least one draft section before continuing.")


def advance(session, payload):
    if "selectedFactIds" in payload:
        session.selected_fact_ids = payload["selectedFactIds"]
    if "selectedCuratedFacts" in payload:
        session.selected_curated_facts = payload["selectedCuratedFacts"]
    if "selectedSourceResults" in payload:
        session.selected_source_results = payload["selectedSourceResults"]
    if "selectedBlockKeys" in payload:
        session.selected_block_keys = payload["selectedBlockKeys"]
    if "authorProfile" in payload:
        session.author_profile = payload["authorProfile"] or {}
    if "templateData" in payload:
        session.template_data = payload["templateData"] or {}
    if "goal" in payload:
        session.goal = payload["goal"] or ""
    if "draftPlan" in payload:
        session.draft_plan = payload["draftPlan"] or {}
    if "missingInformation" in payload:
        session.missing_information = payload["missingInformation"] or []
    if "selectedTemplateIds" in payload:
        session.selected_template_ids = payload["selectedTemplateIds"] or []
    if "instructions" in payload:
        session.instructions = payload["instructions"]
    if "template" in payload:
        # Assigning the raw value would persist an unusable foreign key and only
        # fail later, when something first dereferences session.template.
        reference = payload["template"]
        if reference in (None, ""):
            session.template = None
        else:
            template = template_for_reference(reference)
            if not template:
                raise ValueError("Selected template was not found.")
            session.template = template

    current_status = normalize_status(session.status)
    requested_status = payload.get("status")
    if requested_status:
        target_status = normalize_status(requested_status)
        if target_status not in STEP_ORDER:
            raise ValueError("Unsupported drafting workflow step.")
    elif current_status in STEP_ORDER:
        index = STEP_ORDER.index(current_status)
        target_status = STEP_ORDER[min(index + 1, len(STEP_ORDER) - 1)]
    else:
        target_status = "setup"

    _validate_transition(session, target_status)
    session.status = target_status
    session.save()
    return session


# Draft plan helpers


def _matter_details_text(matter):
    details = [
        matter.summary or "",
        matter.matter_type or "",
        matter.posture or "",
        matter.jurisdiction or "",
    ]
    return "\n".join(item for item in details if item)


def _available_templates():
    return DocumentTemplate.objects.filter(is_active=True).prefetch_related("blocks").order_by("title")


def _unfilled_field_requests(session, template):
    """Template blanks that still need an answer from somebody.

    Blanks the Word conversion invented are dropped rather than asked about, and
    a blank the case record or a previous answer already fills is not asked
    again under any of the names the template binds it to.
    """
    template_data = session.template_data or {}
    case_fields, _field_sources = letter_template_fields(session.matter)
    return [
        request
        for request in template_field_requests(template)
        if request.kind != KIND_UNUSABLE
        and field_applies(template, request.key, template_data)
        and not any(
            str(template_data.get(key, case_fields.get(key, ""))).strip()
            for key in field_keys_for_answer(request.key)
        )
    ]


def _plan_missing_information(session, template):
    missing = []
    if not session.author_profile or not (session.author_profile.get("displayName") or session.author_profile.get("email")):
        missing.append(
            {
                "field": "author_profile",
                "question": "Who should appear in the signature block?",
                "kind": KIND_VALUE,
                "required_for_generation": False,
            }
        )
    requests = _unfilled_field_requests(session, template)
    defaults = field_defaults(template)
    # The case record answers a good share of these, so read it before handing
    # the list to a person.
    resolved = resolve_field_requests(
        session,
        template,
        requests,
        facts=_selected_facts(session) or list(session.matter.facts.all()),
        enabled=_ai_review_enabled(),
    )
    for request in requests:
        answer = resolved.get(request.key) or {}
        value = answer.get("value") or ""
        default = "" if value else defaults.get(request.key, "")
        missing.append(
            {
                "field": request.path,
                "question": answer.get("question") or request.question,
                "context": request.context,
                "section": request.block_label,
                "kind": request.kind,
                "answer": value or default,
                "answer_source": "case_record" if value else ("template_default" if default else ""),
                "basis": answer.get("basis") or ("The template's default; change it if it no longer applies." if default else ""),
                # A drafting direction is the model's to carry out, so leaving it
                # blank is a choice to let the draft write it, not an omission.
                "ai_completable": request.kind == KIND_NARRATIVE,
                "optional": request.optional,
                "has_default": bool(defaults.get(request.key)),
                "required_for_generation": False,
            }
        )
    return missing


def _source_plan(session):
    return [
        {
            "source_id": source.get("id") or source.get("sourceKind") or source.get("citation") or source.get("title"),
            "reason": source.get("reason") or source.get("snippet") or "Selected drafting support.",
        }
        for source in (session.selected_source_results or [])
    ]


def _requested_custom_fields(session):
    goal_terms = set(_fact_terms(" ".join([session.goal or "", session.instructions or "", session.matter.summary or ""])))
    requested = []
    for field in custom_fields_inventory(session.matter)[:12]:
        field_terms = set(_fact_terms(" ".join([field["key"], field["label"], field.get("category", "")])))
        if field["confidence"] == "likely_useful" and (goal_terms.intersection(field_terms) or field["category"] == "narrative"):
            requested.append(
                {
                    "fieldKey": field["key"],
                    "reason": field["reason"],
                }
            )
    return requested[:5]


def _is_generic_template_description(value):
    return " ".join((value or "").split()).casefold() in GENERIC_TEMPLATE_DESCRIPTIONS


def _template_goal_text(template):
    if template.goal:
        return template.goal
    if template.description and not _is_generic_template_description(template.description):
        return template.description
    return f"Draft {template.title}."


def _template_drafting_instructions(session, template, goal):
    if session.instructions:
        return session.instructions
    if template.goal:
        return template.goal
    return (
        f"Use the selected {template.title} template structure and draft the active blocks "
        "with case-specific facts, requested relief, and reviewer-approved sources."
    )


def _document_item_for_template(session, template, recommendation=None):
    fact_slugs = [fact.slug for fact in MatterFact.objects.filter(id__in=session.selected_fact_ids)]
    selected_keys = drafting_ai.recommend_blocks(template, fact_slugs)
    selected_keys = selected_keys or [block.key for block in template.blocks.all()]
    missing_information = _plan_missing_information(session, template)
    goal = session.goal or session.instructions or _template_goal_text(template)
    reason = "; ".join((recommendation or {}).get("reasons") or [template.goal or template.description or "Selected template."])
    if _is_generic_template_description(reason):
        reason = "Selected template."
    return {
        "id": template.slug,
        "template_slug": template.slug,
        "template_id": template.id,
        "title": template.title,
        "goal": goal,
        "reason": reason,
        "selected_block_keys": selected_keys,
        "drafting_instructions": _template_drafting_instructions(session, template, goal),
        "missing_information": missing_information,
    }


def _plan_summary(session, selected_templates):
    if session.goal or session.instructions:
        return session.goal or session.instructions
    if len(selected_templates) == 1:
        template = selected_templates[0]
        return _template_goal_text(template)
    if selected_templates:
        return "Make the selected documents: " + ", ".join(template.title for template in selected_templates) + "."
    return _matter_details_text(session.matter) or "Draft a housing case document."


def _fallback_plan(session, *, allow_multiple=False):
    templates = list(_available_templates())
    selected_templates = []
    if session.selected_template_ids:
        selected_ids = {int(value) for value in session.selected_template_ids if str(value).isdigit()}
        selected_templates = [template for template in templates if template.id in selected_ids]
    elif session.template_id:
        selected_templates = [template for template in templates if template.id == session.template_id]
    if not selected_templates:
        limit = 3 if allow_multiple else 1
        selected_templates = [item["template"] for item in recommend_templates(session.goal or session.instructions, session.matter, templates, limit=3)[:limit]]
    recommendation_by_id = {
        item["template"].id: item
        for item in recommend_templates(session.goal or session.instructions, session.matter, templates, limit=3)
    }
    document_items = [
        _document_item_for_template(session, template, recommendation_by_id.get(template.id))
        for template in selected_templates
    ]
    missing = [item for document in document_items for item in document.get("missing_information", [])]
    return {
        "summary": _plan_summary(session, selected_templates),
        "document_items": document_items,
        "source_plan": _source_plan(session),
        "author_requirements": {
            "needed_before_generation": False,
            "used_in": ["caption", "signature"],
        },
        "available_templates": [template_to_dict(template, include_blocks=True) for template in templates],
        "selected_facts": session.selected_fact_ids or [],
        "selected_sources": session.selected_source_results or [],
        "missing_information": missing,
        "requestedCustomFields": _requested_custom_fields(session),
    }


def missing_information_items(plan):
    """The plan's live questions, dropping the ones a reviewer skipped.

    The per-document lists are authoritative whenever the plan has documents:
    the reviewer edits those, and the plan's flat `missing_information` copy is
    only a summary written when the plan was built. Falling back to the summary
    when the per-document lists come back empty is what made skipping every
    question resurrect all of them.
    """
    document_items = plan.get("document_items") if isinstance(plan, dict) else []
    if document_items:
        return [
            item
            for document in document_items
            for item in (document.get("missing_information") or [])
            if not item.get("not_needed")
        ]
    return [
        item
        for item in (plan.get("missing_information") or [] if isinstance(plan, dict) else [])
        if not item.get("not_needed")
    ]


def unanswered_missing_information(plan, *, require_all=False):
    """Questions that should stop generation.

    A blank the draft can write for itself never stops it, even when the
    reviewer asked to answer everything first: it is a drafting direction the
    template author left behind, not a fact the reviewer is withholding.
    """
    return [
        item
        for item in missing_information_items(plan)
        if not item.get("answer")
        and not item.get("not_needed")
        and (
            item.get("required_for_generation")
            or (require_all and not item.get("ai_completable") and not item.get("optional"))
        )
    ]


def _field_key_for_missing_information(item):
    field = str(item.get("field") or "").strip()
    if not field:
        return ""
    return field.removeprefix("fields.").replace(".", "_")


def _template_data_with_missing_information_answers(template_data, missing_information):
    merged = dict(template_data or {})
    for item in missing_information or []:
        answer = str(item.get("answer") or "").strip()
        field_key = _field_key_for_missing_information(item)
        if field_key and item.get("has_default") and (not answer or item.get("not_needed")):
            # The reviewer cleared or skipped a pre-filled default -- a judge
            # no longer sitting. Record the blank, or the default comes back.
            for key in field_keys_for_answer(field_key):
                merged.setdefault(key, "")
            continue
        if not answer or not field_key or item.get("not_needed"):
            continue
        # One answer fills every binding of the same fact, so a template that
        # says "landlord" in one paragraph and "plaintiff" in the next does not
        # print a placeholder in one of them.
        for key in field_keys_for_answer(field_key):
            merged.setdefault(key, answer)
    return merged


def _missing_information_answer_text(missing_information):
    answers = []
    directions = []
    for item in missing_information or []:
        if item.get("not_needed"):
            continue
        question = item.get("question") or item.get("field") or "Missing information"
        answer = str(item.get("answer") or "").strip()
        if answer:
            answers.append(f"- {question}: {answer}")
        elif item.get("ai_completable"):
            # An unanswered drafting direction stays the model's to carry out.
            directions.append(f"- {question}")
    sections = []
    if answers:
        sections.append("Reviewer-approved answers to drafting questions:\n" + "\n".join(answers))
    if directions:
        sections.append(
            "Drafting directions from the template, to be written from the selected facts "
            "and left as a bracketed note when the facts do not support them:\n" + "\n".join(directions)
        )
    return "\n\n".join(sections)


def create_or_update_plan(session, payload):
    if "goal" in payload:
        session.goal = payload.get("goal") or ""
    if "instructions" in payload:
        session.instructions = payload.get("instructions") or session.goal
    if "templateData" in payload:
        session.template_data = payload.get("templateData") or {}
    if "authorProfile" in payload:
        session.author_profile = payload.get("authorProfile") or {}
    if "selectedFactIds" in payload:
        session.selected_fact_ids = payload.get("selectedFactIds") or []
    if "selectedCuratedFacts" in payload:
        session.selected_curated_facts = payload.get("selectedCuratedFacts") or []
    if "selectedSourceResults" in payload:
        session.selected_source_results = payload.get("selectedSourceResults") or []
    if "selectedTemplateIds" in payload:
        session.selected_template_ids = [int(value) for value in (payload.get("selectedTemplateIds") or []) if str(value).isdigit()]
    elif "templateId" in payload and payload.get("templateId"):
        session.selected_template_ids = [int(payload["templateId"])]
    plan = _fallback_plan(session, allow_multiple=bool(payload.get("allowMultipleDocuments")))
    session.draft_plan = plan
    session.missing_information = missing_information_items(plan)
    if plan.get("document_items"):
        first = plan["document_items"][0]
        session.template_id = first.get("template_id") or session.template_id
        session.selected_block_keys = first.get("selected_block_keys") or session.selected_block_keys
    session.status = "outline_review"
    session.save()
    return session


def apply_plan_edits(session, payload):
    plan = payload.get("draftPlan") or payload.get("plan") or payload
    if not isinstance(plan, dict):
        raise ValueError("Draft plan must be an object.")
    document_items = plan.get("document_items") or []
    if document_items:
        # The reviewer edits the per-document lists; the plan's flat copy is a
        # summary, and a stale summary is indistinguishable from unanswered work.
        plan = {**plan, "missing_information": missing_information_items(plan)}
    session.draft_plan = plan
    session.goal = payload.get("goal", session.goal)
    session.selected_template_ids = [
        item.get("template_id")
        for item in document_items
        if item.get("template_id")
    ]
    if not session.selected_template_ids:
        slugs = [item.get("template_slug") for item in document_items if item.get("template_slug")]
        session.selected_template_ids = list(DocumentTemplate.objects.filter(slug__in=slugs).values_list("id", flat=True))
    session.missing_information = missing_information_items(plan)
    session.template_data = _template_data_with_missing_information_answers(
        session.template_data,
        session.missing_information,
    )
    if document_items:
        first = document_items[0]
        template = _template_for_plan_item(first)
        if template:
            session.template = template
        session.selected_block_keys = first.get("selected_block_keys") or session.selected_block_keys
    session.save()
    return session


def template_for_reference(value):
    """Resolve a template id supplied by a client, without raising on junk input."""
    if value in (None, ""):
        return None
    try:
        template_id = int(value)
    except (TypeError, ValueError):
        return None
    return DocumentTemplate.objects.filter(id=template_id).first()


def _template_for_plan_item(item):
    if item.get("template_id"):
        return DocumentTemplate.objects.filter(id=item["template_id"], is_active=True).prefetch_related("blocks").first()
    if item.get("template_slug"):
        return DocumentTemplate.objects.filter(slug=item["template_slug"], is_active=True).prefetch_related("blocks").first()
    return None


# Fact recommendation helpers


def _selected_fact_slugs_for_blocks(session):
    slugs = []
    for block in _ordered_blocks(session):
        for slug in block.selection_rule.get("fact_slugs", []):
            if slug not in slugs:
                slugs.append(slug)
    return slugs


def _ai_review_enabled():
    config = SourceConfiguration.effective_settings("openai", {"enabled": settings.AI_DRAFTING_ENABLED})
    return str(config.get("enabled", "")).lower() not in {"0", "false", "no", "off"}


def _parse_json_response(text):
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _ai_json(system, user):
    if not _ai_review_enabled():
        return None
    try:
        response = OpenAICompatibleClient().complete(system=system, user=user, temperature=0.0)
    except OpenAIBackendError:
        return None
    return _parse_json_response(response)


def _fact_review_payload(session, facts):
    blocks = [f"- {block.key}: {block.label} ({block.block_type})" for block in _ordered_blocks(session)]
    fact_lines = [f"- id={fact.id}; slug={fact.slug}; title={fact.title}; text={fact.text}; source={fact.source_label}" for fact in facts]
    return "\n".join(
        [
            f"Matter summary: {session.matter.summary}",
            f"Jurisdiction: {session.matter.jurisdiction}",
            f"Template: {session.template.title if session.template else 'No template'}",
            f"Instructions: {session.instructions or '-'}",
            "Draft sections needing facts:",
            "\n".join(blocks) or "- None",
            "Available facts:",
            "\n".join(fact_lines) or "- None",
        ]
    )


def _ai_recommend_fact_ids(session, facts):
    payload = _fact_review_payload(session, facts)
    data = _ai_json(
        "You select facts for a legal drafting workflow. Select only facts relevant to the selected template, active sections, jurisdiction, and instructions. Return strict JSON.",
        f"{payload}\n\nReturn JSON with this shape: {{\"selected_ids\": [1, 2], \"reason\": \"short explanation\"}}",
    )
    if not isinstance(data, dict):
        return None
    available = {fact.id for fact in facts}
    selected = [int(value) for value in data.get("selected_ids", []) if str(value).isdigit() and int(value) in available]
    return selected or None


def recommend_fact_ids(session):
    """Have AI suggest facts from the template/block needs, with deterministic fallback."""
    # Derived document-search facts are rebuilt and deduplicated separately for
    # the current retrieval plan. Excluding older derived rows here prevents a
    # prior broad search from leaking stale or overlapping excerpts into a new
    # template recommendation.
    facts = list(
        MatterFact.objects.filter(matter=session.matter)
        .exclude(confidence="ai_document_search")
        .order_by("id")
    )
    ai_selected = _ai_recommend_fact_ids(session, facts)
    if ai_selected:
        return ai_selected

    recommended_slugs = list(drafting_ai.recommend_fact_slugs(session.matter))
    for slug in _selected_fact_slugs_for_blocks(session):
        if slug not in recommended_slugs:
            recommended_slugs.append(slug)
    selected = [fact.id for fact in facts if fact.selected_by_default or fact.slug in recommended_slugs]
    if selected:
        return selected
    return [fact.id for fact in facts[:5]]


def _fact_terms(value):
    return [
        term
        for term in re.findall(r"[a-z0-9']+", (value or "").casefold())
        if len(term) > 2 and term not in FACT_STOP_WORDS
    ]


def _expanded_fact_terms(seed_terms):
    expanded = list(seed_terms)
    seed_set = set(seed_terms)
    for terms in FACT_TERM_GROUPS.values():
        single_words = {term for term in terms if " " not in term}
        if seed_set.intersection(single_words):
            expanded.extend(sorted(terms))
    return list(dict.fromkeys(expanded))


def fact_retrieval_plan(session):
    """Build deterministic fact categories and progressively broader search patterns."""
    block_labels_by_slug = {}
    for block in _ordered_blocks(session):
        for slug in block.selection_rule.get("fact_slugs", []):
            block_labels_by_slug.setdefault(slug, []).append(block.label)

    categories = []
    for slug in _selected_fact_slugs_for_blocks(session):
        labels = block_labels_by_slug.get(slug) or [slug.replace("-", " ")]
        label = labels[0]
        seed_terms = list(dict.fromkeys([*_fact_terms(slug.replace("-", " ")), *_fact_terms(" ".join(labels))]))
        expanded_terms = _expanded_fact_terms(seed_terms)
        instruction_terms = _fact_terms(session.instructions)[:5]
        patterns = [
            label,
            slug.replace("-", " "),
            " ".join(expanded_terms[:8]),
            " ".join(dict.fromkeys([*expanded_terms[:6], *instruction_terms])),
        ]
        categories.append(
            {
                "key": slug,
                "label": label,
                "terms": expanded_terms,
                "patterns": [pattern for pattern in dict.fromkeys(patterns) if pattern.strip()],
            }
        )

    if categories:
        return categories

    context_terms = _fact_terms(" ".join([session.instructions or "", session.matter.summary or ""]))
    expanded_terms = _expanded_fact_terms(context_terms)
    if expanded_terms:
        categories.append(
            {
                "key": "case-background",
                "label": "Case background",
                "terms": expanded_terms,
                "patterns": [
                    " ".join(context_terms[:8]),
                    " ".join(expanded_terms[:10]),
                ],
            }
        )
    return categories


def _score_fact_chunk(chunk, category, pattern):
    text = chunk["text"].casefold()
    required_patterns = FACT_CATEGORY_REQUIRED_PATTERNS.get(category["key"], ())
    if required_patterns and not any(required in text for required in required_patterns):
        return 0
    category_terms = category["terms"]
    score = sum(text.count(term) for term in category_terms)
    pattern_terms = _fact_terms(pattern)
    score += 2 * sum(text.count(term) for term in pattern_terms if term in category_terms)
    return score


def _prepare_fact_documents(documents):
    prepared = []
    for document in documents:
        chunks = chunk_text(get_document_text(document))
        if chunks:
            prepared.append((document, chunks))
    return prepared


def _best_fact_excerpts(prepared, category, *, limit=2):
    def search(patterns):
        matches = {}
        for pattern in patterns:
            for document, chunks in prepared:
                for chunk in search_chunks(chunks, pattern, limit=3):
                    score = _score_fact_chunk(chunk, category, pattern)
                    if score:
                        key = (document["id"], chunk["index"])
                        candidate = (score, -chunk["index"], document, chunk)
                        if key not in matches or candidate[:2] > matches[key][:2]:
                            matches[key] = candidate
        return list(matches.values())

    patterns = category["patterns"]
    matches = search(patterns[:2])
    if not matches or max(match[0] for match in matches) < 3:
        matches.extend(search(patterns[2:]))
    ranked = sorted(matches, key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return []
    best_score = ranked[0][0]
    selected = []
    seen_documents = set()
    for score, _position, document, chunk in ranked:
        if document["id"] in seen_documents:
            continue
        if selected and score < max(2, best_score * 0.35):
            continue
        selected.append((document, chunk))
        seen_documents.add(document["id"])
        if len(selected) >= limit:
            break
    return selected


def _document_fact_source(document, chunk):
    source = document.get("source") or "Case document"
    citation = document.get("citation") or document.get("title") or "case record"
    return f"{source}: {citation}, excerpt {chunk['index']}"[:255]


def _concise_fact_text(text, category):
    """Turn retrieved note/chunk text into short evidence, not an intake-note dump."""
    cleaned = re.sub(
        r"\b(?:INTAKE NOTES|LEGAL ISSUES(?:\s*&\s*DEFENSES)? IDENTIFIED|CASE STATUS)\s*:\s*",
        " ",
        text or "",
        flags=re.IGNORECASE,
    )
    segments = [
        re.sub(r"\s+", " ", segment).strip(" -•")
        for segment in re.split(r"\s*<br\s*/?>\s*|\s+-\s+|(?<=[.!?])\s+", cleaned, flags=re.IGNORECASE)
    ]
    segments = [
        segment
        for segment in segments
        if segment
        and not re.match(r"^(?:client|location|housing type)\s*:", segment, flags=re.IGNORECASE)
        and not re.match(
            r"^(?:file|request|consider|argue|investigate|obtain|review)\b",
            segment,
            flags=re.IGNORECASE,
        )
        and not re.search(r"\bunder\s+(?:O\.?R\.?C\.?|R\.?C\.?)$", segment, flags=re.IGNORECASE)
    ]
    terms = set(category.get("terms") or [])

    def score(segment):
        value = segment.casefold()
        return sum(1 for term in terms if term in value)

    ranked = sorted(enumerate(segments), key=lambda item: (-score(item[1]), item[0]))
    selected_indexes = sorted(index for index, segment in ranked[:4] if score(segment) > 0)
    if not selected_indexes:
        selected_indexes = list(range(min(2, len(segments))))
    selected = [segments[index] for index in selected_indexes]
    return summarize_text(" ".join(selected), max_sentences=4, max_chars=600)


def _create_document_fact(matter, category, document, chunk):
    text = _concise_fact_text(chunk.get("text", ""), category)
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text).strip().casefold()
    for fact in MatterFact.objects.filter(matter=matter).only("id", "text"):
        if re.sub(r"\s+", " ", fact.text).strip().casefold() == normalized:
            return fact

    source_label = _document_fact_source(document, chunk)
    existing = MatterFact.objects.filter(
        matter=matter,
        confidence="ai_document_search",
        source_label=source_label,
        title__startswith=category["label"][:80],
    ).first()
    if existing:
        if existing.text != text:
            existing.text = text
            existing.save(update_fields=["text"])
        return existing

    base_slug = slugify(f"document-{category['key']}-{document['id']}")[:110] or "document-fact"
    slug = base_slug
    suffix = 2
    while MatterFact.objects.filter(matter=matter, slug=slug).exists():
        slug = f"{base_slug[:116 - len(str(suffix))]}-{suffix}"
        suffix += 1
    return MatterFact.objects.create(
        matter=matter,
        slug=slug,
        title=f"{category['label']} — {document.get('title') or 'case record'}"[:255],
        text=text,
        source_label=source_label,
        confidence="ai_document_search",
        ai_suggested=True,
        selected_by_default=False,
    )


def recommend_document_fact_ids(session, limit=8):
    plan = fact_retrieval_plan(session)
    if not plan:
        return []
    # Never mine this tool's own drafts and notes for facts: an earlier draft's
    # excerpt once became the next motion's Relevant Facts, placeholders and all.
    prepared_documents = _prepare_fact_documents(evidence_only(get_case_documents(session.matter)))
    selected = []
    for category in plan:
        for document, chunk in _best_fact_excerpts(prepared_documents, category):
            fact = _create_document_fact(session.matter, category, document, chunk)
            if fact and fact.id not in selected:
                selected.append(fact.id)
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break
    return selected


def recommend_session_fact_ids(session):
    """Recommend existing facts and source-cited facts recovered from case notes/documents."""
    document_fact_ids = recommend_document_fact_ids(session)
    return list(dict.fromkeys([*recommend_fact_ids(session), *document_fact_ids]))


# Goal recommendation helpers


def _goal_recommendation_payload(session, facts, templates):
    fact_payload = [
        {
            "id": fact.id,
            "slug": fact.slug,
            "title": fact.title,
            "text": fact.text,
            "source": fact.source_label,
        }
        for fact in facts
    ]
    template_payload = [
        {
            "id": template.id,
            "title": template.title,
            "slug": template.slug,
            "kind": template.kind,
            "goal": template.goal,
            "aliases": template.aliases or [],
            "negative_goal": template.negative_goal,
            "jurisdiction": template.jurisdiction,
        }
        for template in templates
    ]
    return {
        "matter_summary": session.matter.summary or "",
        "matter_type": session.matter.matter_type or "",
        "posture": session.matter.posture or "",
        "jurisdiction": session.matter.jurisdiction or "",
        "current_goal": "\n".join(
            part
            for part in [
                f"Goal: {session.goal}" if session.goal else "",
                f"Instructions: {session.instructions}" if session.instructions else "",
            ]
            if part
        )
        or "-",
        "facts": json.dumps(fact_payload, ensure_ascii=True, indent=2),
        "templates": json.dumps(template_payload, ensure_ascii=True, indent=2),
    }


def _goal_candidate_id(title, goal, existing):
    base = slugify(str(title or goal))[:80] or "suggested-goal"
    candidate_id = base
    suffix = 2
    while candidate_id in existing:
        suffix_text = f"-{suffix}"
        candidate_id = f"{base[:80 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    existing.add(candidate_id)
    return candidate_id


def _normalize_goal_candidates(candidates, facts, templates, *, limit=5):
    fact_ids = {fact.id for fact in facts}
    templates_by_slug = {template.slug: template for template in templates if template.is_active}
    active_templates = list(templates_by_slug.values())
    normalized = []
    seen_ids = set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title") or "").strip()
        goal = str(candidate.get("goal") or "").strip()
        if not goal:
            continue
        instructions = str(candidate.get("instructions") or "").strip()
        reason = str(candidate.get("reason") or "").strip()
        confidence = str(candidate.get("confidence") or "medium").strip().casefold()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        supporting_fact_ids = []
        for value in candidate.get("supporting_fact_ids", candidate.get("supportingFactIds", [])) or []:
            if str(value).isdigit() and int(value) in fact_ids and int(value) not in supporting_fact_ids:
                supporting_fact_ids.append(int(value))
        template_slugs = []
        for slug in candidate.get("template_slugs", candidate.get("templateSlugs", [])) or []:
            slug = str(slug).strip()
            if slug in templates_by_slug and slug not in template_slugs:
                template_slugs.append(slug)
        if not template_slugs:
            template_slugs = [
                item["template"].slug
                for item in recommend_templates(goal, None, active_templates, limit=2)
            ]
        template_ids = [templates_by_slug[slug].id for slug in template_slugs if slug in templates_by_slug]
        normalized.append(
            {
                "id": _goal_candidate_id(candidate.get("id") or title, goal, seen_ids),
                "title": title or goal[:80],
                "goal": goal,
                "instructions": instructions,
                "reason": reason,
                "confidence": confidence,
                "supportingFactIds": supporting_fact_ids,
                "templateIds": template_ids,
                "templateSlugs": template_slugs,
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def _ai_recommend_goals(session, facts, templates, *, limit=5):
    payload = _goal_recommendation_payload(session, facts, templates)
    try:
        prompt = render_prompt(
            "drafting.goal_recommendations",
            **payload,
            limit=limit,
        )
    except (PromptCatalogError, PromptRenderError):
        return None
    data = _ai_json(prompt.system, prompt.user)
    if not isinstance(data, dict):
        return None
    goals = _normalize_goal_candidates(data.get("goals"), facts, templates, limit=limit)
    return goals or None


def _facts_matching_terms(facts, terms):
    selected = []
    for fact in facts:
        text = " ".join([fact.title or "", fact.text or "", fact.slug or ""]).casefold()
        if any(term in text for term in terms):
            selected.append(fact.id)
    return selected


def _fallback_goal_candidate(title, goal, instructions, reason, confidence, fact_ids, templates, matter):
    ranked_templates = recommend_templates(goal, matter, templates, limit=2)
    return {
        "title": title,
        "goal": goal,
        "instructions": instructions,
        "reason": reason,
        "confidence": confidence,
        "supporting_fact_ids": fact_ids,
        "template_slugs": [item["template"].slug for item in ranked_templates],
    }


def _fallback_goal_candidates(session, facts, templates, *, limit=5):
    text = " ".join(
        [
            session.matter.summary or "",
            session.matter.matter_type or "",
            session.matter.posture or "",
            session.matter.jurisdiction or "",
            session.goal or "",
            session.instructions or "",
            " ".join(f"{fact.title} {fact.text}" for fact in facts),
        ]
    ).casefold()
    candidates = []

    def add_candidate(term_groups, title, goal, instructions, reason, confidence="medium"):
        if len(candidates) >= limit:
            return
        if not all(any(term in text for term in terms) for terms in term_groups):
            return
        fact_terms = [term for terms in term_groups for term in terms]
        fact_ids = _facts_matching_terms(facts, fact_terms)
        candidates.append(_fallback_goal_candidate(title, goal, instructions, reason, confidence, fact_ids, templates, session.matter))

    add_candidate(
        [
            ("rental assistance", "erap", "application pending", "assistance application"),
            ("hearing", "court", "deadline", "continuance", "continue"),
        ],
        "Ask for more time for rental assistance",
        "Ask the court to continue the hearing so pending rental assistance can be processed.",
        "Emphasize the pending rental assistance application and the need for more time before the next hearing or deadline.",
        "The case facts mention rental assistance and a hearing or deadline.",
        "high",
    )
    add_candidate(
        [("repair", "repairs", "mold", "leak", "habitability", "inspection", "code")],
        "Raise repair and habitability issues",
        "Prepare a filing that explains repair, habitability, inspection, or code issues relevant to the eviction case.",
        "Use only confirmed condition facts and identify what relief or defenses the reviewer wants to preserve.",
        "The case facts mention repair or habitability conditions.",
    )
    add_candidate(
        [("notice", "service", "served", "quit", "termination", "summons", "complaint")],
        "Address notice or service problems",
        "Prepare a filing that raises confirmed notice, service, termination, summons, or complaint problems for review.",
        "Describe the specific notice or service facts without assuming a legal defect beyond the supplied facts.",
        "The case facts mention notice, service, or case-starting papers.",
    )
    add_candidate(
        [("payment", "paid", "rent", "ledger", "receipt", "arrears", "balance", "money order")],
        "Explain payment or arrears dispute",
        "Prepare a filing that presents the tenant's payment, rent ledger, receipt, or arrears dispute.",
        "Ground the draft in specific payment records, amounts, dates, or disputed ledger entries supplied by the case facts.",
        "The case facts mention payment history, rent ledger, receipts, or arrears.",
    )
    add_candidate(
        [("disability", "disabled", "accommodation", "medical", "doctor", "records")],
        "Request or explain accommodation needs",
        "Prepare a filing that explains disability, medical, or reasonable-accommodation facts for human review.",
        "Avoid disclosing unnecessary medical detail and focus on the requested accommodation or case impact stated in the facts.",
        "The case facts mention disability, medical needs, or accommodation.",
    )
    add_candidate(
        [("hearing", "court", "deadline", "continuance", "continue", "extension")],
        "Ask for a continuance or extension",
        "Ask the court for a continuance or extension based on the scheduled hearing, court date, or deadline.",
        "Include the known date or deadline if available and explain the practical reason more time is needed.",
        "The case facts mention a hearing, court date, deadline, or need for more time.",
    )

    if not candidates and (session.matter.summary or facts):
        candidates.append(
            _fallback_goal_candidate(
                "Draft from case facts",
                "Prepare a housing-court filing grounded in the selected case facts for human review.",
                "Review the selected facts and available templates before making the draft plan.",
                "The case has facts available for a drafting workflow, but no stronger drafting goal pattern was detected.",
                "low",
                [fact.id for fact in facts[:3]],
                templates,
                session.matter,
            )
        )
    return _normalize_goal_candidates(candidates, facts, templates, limit=limit)


def recommend_goal_candidates(session, *, limit=5):
    facts = list(MatterFact.objects.filter(matter=session.matter).order_by("id"))
    templates = list(_available_templates())
    try:
        limit = int(limit or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 10))
    goals = _ai_recommend_goals(session, facts, templates, limit=limit)
    if not goals:
        goals = _fallback_goal_candidates(session, facts, templates, limit=limit)
    return {
        "goals": goals,
        "guidance": "Review and edit a suggested goal before making a draft plan.",
    }


# Support recommendation helpers


def _selected_facts(session):
    return list(MatterFact.objects.filter(id__in=session.selected_fact_ids).order_by("id"))


def support_query_for_session(session):
    parts = [session.matter.summary, session.matter.jurisdiction, session.instructions]
    if session.template:
        parts.append(session.template.title)
        parts.extend(block.label for block in _ordered_blocks(session))
    for fact in _selected_facts(session)[:8]:
        parts.append(fact.text)
    return " ".join(part for part in parts if part).strip()


def support_purpose_for_result(result):
    title = (getattr(result, "title", "") or "").casefold()
    source_kind = getattr(result, "source_kind", "")
    metadata = getattr(result, "metadata", {}) or {}
    resource_type = str(metadata.get("resourceType") or metadata.get("resource_type") or "").casefold()
    citation = getattr(result, "citation", "") or ""
    if source_kind in {"local_cases", "rag"} and citation:
        return "legal_authority"
    if source_kind in {"sharepoint", "user_resources"} or resource_type in {"brief", "example"}:
        if any(term in title for term in ["answer", "motion", "brief", "pleading", "filing"]):
            return "example_language"
    return "background_reference"


def result_to_support_candidate(result):
    purpose = support_purpose_for_result(result)
    payload = result.to_dict() if hasattr(result, "to_dict") else {
        "id": getattr(result, "id", ""),
        "title": getattr(result, "title", ""),
        "snippet": getattr(result, "snippet", ""),
        "sourceKind": getattr(result, "source_kind", ""),
        "sourceLabel": getattr(result, "source_label", ""),
        "url": getattr(result, "url", ""),
        "citation": getattr(result, "citation", ""),
        "metadata": getattr(result, "metadata", {}) or {},
    }
    return {
        **payload,
        "purpose": purpose,
        "purposeLabel": SUPPORT_PURPOSE_LABELS[purpose],
        "selectedByDefault": purpose in {"legal_authority", "example_language"},
    }


def _source_catalog_for_ai():
    from apps.sources.selection import source_guidance

    sources = source_guidance()["sources"]
    return "\n".join(
        f"- id={source_id}; label={source.get('label', source_id)}; kind={source.get('kind')}; reason={source.get('default_reason', '')}"
        for source_id, source in sources.items()
    )


def _ai_select_source_ids(query, session):
    from apps.sources.selection import source_guidance

    available = set(source_guidance()["sources"])
    data = _ai_json(
        "You select source libraries for a legal drafting support review. Pick only libraries likely to contain relevant authorities, examples, or references. Return strict JSON.",
        "\n".join(
            [
                f"Drafting support query: {query}",
                f"Matter summary: {session.matter.summary}",
                f"Jurisdiction: {session.matter.jurisdiction}",
                "Available source libraries:",
                _source_catalog_for_ai(),
                "Return JSON with this shape: {\"selected_source_ids\": [\"ohio-statutes\"], \"reason\": \"short explanation\"}",
            ]
        ),
    )
    if not isinstance(data, dict):
        return None
    selected = [source_id for source_id in data.get("selected_source_ids", []) if source_id in available]
    return selected or None


def _ai_select_candidate_ids(query, candidates):
    candidate_lines = []
    for candidate in candidates[:20]:
        candidate_lines.append(
            f"- id={candidate.get('id')}; purpose={candidate.get('purpose')}; title={candidate.get('title')}; source={candidate.get('sourceLabel')}; citation={candidate.get('citation')}; snippet={candidate.get('snippet')}"
        )
    data = _ai_json(
        "You select drafting support candidates. Select only sources the draft may rely on. Prefer legal authority and high-value example language. Return strict JSON.",
        "\n".join(
            [
                f"Drafting support query: {query}",
                "Candidate sources:",
                "\n".join(candidate_lines) or "- None",
                "Return JSON with this shape: {\"selected_candidate_ids\": [\"id\"], \"reason\": \"short explanation\"}",
            ]
        ),
    )
    if not isinstance(data, dict):
        return None
    available = {str(candidate.get("id")) for candidate in candidates}
    selected = [str(candidate_id) for candidate_id in data.get("selected_candidate_ids", []) if str(candidate_id) in available]
    return selected or None


def recommend_support_candidates(session, *, user=None, request=None, limit_per_source=3):
    query = support_query_for_session(session)
    if not query:
        return {"query": "", "candidates": [], "selectedSourceIds": []}

    from apps.sources.augmentation import augmented_search
    from apps.sources.registry import connector_registry
    from apps.sources.selection import automatic_source_selection, source_kinds

    ai_source_ids = _ai_select_source_ids(query, session)
    selection = automatic_source_selection(query, matter=session.matter)
    source_ids = ai_source_ids or selection["source_ids"]
    search_payload = augmented_search(
        query,
        connector_registry=connector_registry,
        kinds=source_kinds(source_ids),
        source_ids=source_ids,
        matter=session.matter,
        jurisdiction=session.matter.jurisdiction,
        limit_per_source=limit_per_source,
        user=user,
        request=request,
        max_rounds=2,
    )
    results = search_payload["results"]
    source_ids = search_payload["selected_source_ids"]
    candidates = [result_to_support_candidate(result) for result in results]
    ai_candidate_ids = _ai_select_candidate_ids(query, candidates)
    if ai_candidate_ids is not None:
        selected = set(ai_candidate_ids)
        candidates = [{**candidate, "selectedByDefault": str(candidate.get("id")) in selected} for candidate in candidates]
    return {
        "query": query,
        "selectedSourceIds": source_ids,
        "sourceDecision": {**selection, "source_ids": source_ids, "aiReviewed": bool(ai_source_ids)},
        "candidates": candidates,
        "aiReviewed": ai_candidate_ids is not None,
        "searchAugmentation": search_payload["augmentation"],
    }


def _missing_information_gap_query(session, missing_information):
    questions = [
        item.get("question") or item.get("field") or ""
        for item in missing_information or []
        # A drafting direction is written from the case record, so it points at
        # no gap in the authorities.
        if not item.get("answer") and not item.get("not_needed") and not item.get("ai_completable")
    ]
    questions = [question.strip() for question in questions if question and question.strip()]
    if not questions:
        return ""
    return "\n".join([
        support_query_for_session(session),
        "Drafting questions that may reveal research gaps:",
        *questions[:5],
    ]).strip()


def augment_support_for_missing_information(session, *, missing_information=None, user=None, request=None):
    author_profile = session.author_profile or {}
    if author_profile.get("missingInformationResearchAugmented"):
        return {"expanded": False, "reason": "Missing-information research augmentation already ran."}
    query = _missing_information_gap_query(session, missing_information if missing_information is not None else session.missing_information)
    if not query:
        return {"expanded": False, "reason": "No unanswered drafting questions required source augmentation."}

    from apps.sources.augmentation import augmented_search
    from apps.sources.registry import connector_registry
    from apps.sources.selection import automatic_source_selection, source_kinds

    selection = automatic_source_selection(query, matter=session.matter)
    search_payload = augmented_search(
        query,
        connector_registry=connector_registry,
        kinds=source_kinds(selection["source_ids"]),
        source_ids=selection["source_ids"],
        matter=session.matter,
        jurisdiction=session.matter.jurisdiction,
        limit_per_source=2,
        user=user,
        request=request,
        max_rounds=1,
        minimum_results=3,
    )
    existing = {str(candidate.get("id")) for candidate in session.selected_source_results or []}
    added = [
        {**result_to_support_candidate(result), "addedForMissingInformation": True}
        for result in search_payload["results"]
        if str(getattr(result, "id", "")) not in existing
    ]
    if added:
        session.selected_source_results = [*(session.selected_source_results or []), *added]
    session.author_profile = {**author_profile, "missingInformationResearchAugmented": True}
    session.save(update_fields=["selected_source_results", "author_profile", "updated_at"])
    return {**search_payload["augmentation"], "addedCount": len(added)}


# Draft generation helpers


def create_draft(session, *, template=None, block_keys=None, title=None, instructions=None, missing_by_block=None, missing_information=None, user=None, request=None):
    augment_support_for_missing_information(
        session,
        missing_information=missing_information if missing_information is not None else session.missing_information,
        user=user,
        request=request,
    )
    answered_context = _missing_information_answer_text(missing_information if missing_information is not None else session.missing_information)
    scoped_instructions = "\n\n".join(part for part in [instructions if instructions is not None else session.instructions, answered_context] if part)
    context = regeneration_context(session, template=template, instructions=scoped_instructions)
    active_template = template or session.template
    if not active_template:
        # compose_document walks template.blocks, so there is no usable
        # template-less path here despite the fallbacks further down.
        raise ValueError("Choose a template before generating a draft.")
    missing_filing = opposing_filing.missing_requirement(session, active_template)
    if missing_filing:
        raise ValueError(missing_filing)
    block_keys = block_keys or session.selected_block_keys or [block.key for block in active_template.blocks.all()]
    sections = drafting_ai.compose_document(context, block_keys)
    if missing_by_block:
        sections = [
            {
                **section,
                "missingInformation": missing_by_block.get(section.get("key"), section.get("missingInformation", [])),
            }
            for section in sections
        ]
    draft = DraftDocument.objects.create(
        session=session,
        template=active_template,
        title=title or active_template.title,
        sections=sections,
        plain_text=plain_text_from_sections(sections),
        editor_state={"format": "plain_text"},
    )
    sync_components(draft)
    bind_current_versions(draft, facts=context.selected_facts, source_results=context.selected_sources)
    session.status = "draft_review"
    session.save()
    return draft


def regeneration_context(session, *, template=None, instructions=None):
    return GenerationContext(
        matter=session.matter,
        selected_facts=_selected_facts(session),
        selected_curated_facts=session.selected_curated_facts,
        selected_sources=session.selected_source_results,
        template=template or session.template,
        mode=session.mode,
        instructions=instructions if instructions is not None else session.instructions,
        author_profile=session.author_profile,
        template_data=session.template_data,
        responding_to=opposing_filing.prompt_context(session),
    )


def _missing_by_block_for_item(item):
    missing = item.get("missing_information") or []
    if not missing:
        return {}
    selected_keys = item.get("selected_block_keys") or []
    target_key = selected_keys[0] if selected_keys else ""
    return {
        target_key: [
            {
                "question": entry.get("question") or entry.get("field") or "Missing information",
                "severity": "required" if entry.get("required_for_generation") else "helpful",
                "field": entry.get("field", ""),
            }
            for entry in missing
            if not entry.get("not_needed")
        ]
    } if target_key else {}


def create_drafts_from_plan(session, *, user=None, request=None):
    plan = session.draft_plan or _fallback_plan(session)
    drafts = []
    for item in plan.get("document_items") or []:
        template = _template_for_plan_item(item)
        if not template:
            continue
        drafts.append(
            create_draft(
                session,
                template=template,
                block_keys=item.get("selected_block_keys") or None,
                title=item.get("title") or template.title,
                instructions=item.get("drafting_instructions") or item.get("goal") or session.instructions,
                missing_by_block=_missing_by_block_for_item(item),
                missing_information=item.get("missing_information") or [],
                user=user,
                request=request,
            )
        )
    if not drafts and session.template:
        drafts.append(create_draft(session, user=user, request=request))
    derive_relationships(session)
    return drafts


def regenerate_draft_block(draft, block_key, instruction=""):
    """Regenerate one section as a recorded replace operation, not a whole-document rewrite."""
    section = next((item for item in draft.sections or [] if item.get("key") == block_key), None)
    if section is None:
        return draft
    if section.get("aiLatitude") == "locked":
        # A locked template block is still human-editable, but a redraft request
        # must not replace its maintained, case-bound wording with model prose.
        return draft
    context = regeneration_context(draft.session)
    body = drafting_ai.regenerate_section(section=section, context=context, instruction=instruction)
    operations.propose_and_apply(
        draft,
        "replace_component",
        payload={
            "stableKey": block_key,
            "body": body,
            "structuredContent": {"origin": "ai", "sourceEditorState": None},
        },
        rationale=instruction,
        origin="ai",
    )
    editor_state = draft.editor_state if isinstance(draft.editor_state, dict) else {}
    blocks = dict(editor_state.get("blocks") or {})
    blocks.pop(block_key, None)
    draft.editor_state = {"format": "lexical_blocks", "blocks": blocks}
    draft.save(update_fields=["editor_state", "updated_at"])
    bind_current_versions(draft)
    return draft


# Outline helpers


def outline_for_session(session):
    from apps.issues.models import CandidateIssue

    try:
        issues = list(CandidateIssue.objects.filter(case_id=session.matter.external_id, status="approved"))
    except (OperationalError, ProgrammingError):
        # Tolerate an unmigrated issues table only. Swallowing everything here
        # silently drops reviewer-approved issues from the outline.
        issues = []
    approved_issue_blocks = set()
    for issue in issues:
        approved_issue_blocks.update(issue.outputs.get("activate_blocks_after_approval", []))
    selected_keys = set(session.selected_block_keys or []) | approved_issue_blocks
    blocks = []
    template_blocks = list(session.template.blocks.all()) if session.template else []
    for block in template_blocks:
        selected = block.required or block.key in selected_keys
        if not selected:
            continue
        blocks.append(
            {
                "key": block.key,
                "label": block.label,
                "blockType": block.block_type,
                "required": block.required,
                "selected": selected,
                "aiFillMode": block.ai_fill_mode,
                "supportCount": len(block.supporting_sources or []),
            }
        )
    return {
        "blocks": blocks,
        "selectedFactCount": len(session.selected_fact_ids or []),
        "selectedCuratedFactCount": len(session.selected_curated_facts or []),
        "selectedSupportCount": len(session.selected_source_results or []),
        "approvedIssues": [issue.title for issue in issues],
    }

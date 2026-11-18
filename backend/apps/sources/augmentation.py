from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.sources.selection import automatic_source_selection, source_guidance, source_kinds


FACET_FIELDS = [
    ("statutesCited", "statute"),
    ("regulationsCited", "regulation"),
    ("casesCited", "caseCitation"),
    ("issues", "issue"),
    # An ordinance names the statutes it operates against -- R.C. 5321.19 for
    # preemption, R.C. 1923 for the eviction it defends.  Following those makes
    # a local-law hit pull in the state law that decides whether it stands.
    ("relatedStatutes", "statute"),
]


@dataclass
class SearchPlan:
    query: str
    source_ids: list[str]
    reason: str
    facet: dict = field(default_factory=dict)


def _result_key(result):
    return (result.source_kind, str(result.id))


def _dedupe_results(results):
    seen = set()
    deduped = []
    for result in results:
        key = _result_key(result)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


def _valid_source_ids(source_ids):
    available = source_guidance()["sources"]
    return [source_id for source_id in source_ids if source_id in available]


def _search_key(query, source_ids):
    return (str(query or "").casefold(), tuple(sorted(source_ids or [])))


def _query_terms(value):
    return {term for term in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(term) > 2}


def _result_matches_query(result, query):
    terms = _query_terms(query)
    if not terms:
        return True
    text = f"{result.title} {result.snippet} {result.citation}".casefold()
    return bool(terms & _query_terms(text))


def evaluate_search_results(query, results, *, minimum_results=4, expect_source_diversity=True):
    """Cheap coverage check used before spending another retrieval round."""
    if not results:
        return {"adequate": False, "reasons": ["No sources were retrieved."]}
    relevant = [result for result in results if _result_matches_query(result, query)]
    source_kinds_seen = {result.source_kind for result in results}
    legal_sources = [
        result for result in results
        if result.source_kind in {"rag", "local_cases"} or result.citation
    ]
    reasons = []
    if len(results) < minimum_results:
        reasons.append(f"Only {len(results)} source result(s) were retrieved.")
    if not relevant:
        reasons.append("The retrieved snippets do not share clear terms with the research question.")
    if not legal_sources:
        reasons.append("No legal authority or secondary source was retrieved.")
    if expect_source_diversity and len(source_kinds_seen) == 1 and len(results) < minimum_results + 2:
        reasons.append("The result set is narrow across source types.")
    return {"adequate": not reasons, "reasons": reasons}


def _facet_values(results):
    values = []
    seen = set()
    for result in results:
        metadata = result.metadata or {}
        for field, facet in FACET_FIELDS:
            for value in metadata.get(field) or []:
                normalized = str(value).strip()
                if not normalized:
                    continue
                key = (facet, normalized.casefold())
                if key in seen:
                    continue
                seen.add(key)
                values.append({"facet": facet, "value": normalized, "sourceResultId": result.id})
    return values


def _default_rag_source_ids():
    available = source_guidance()["sources"]
    return [
        source_id for source_id in ["ohio-statutes", "federal-housing-law", "treatise", "ohio-ordinances", "hud-handbook", "green-book"]
        if source_id in available
    ]


def related_source_plans(query, results, selected_source_ids, attempted_searches, *, allowed_source_ids=None):
    """Build conservative follow-up searches from case facets and source gaps.

    ``allowed_source_ids`` restricts every plan to an explicit user selection:
    follow-up rounds may vary the query, but never search sources the user
    excluded (for example, "Cases only" must never fall through to the treatise).
    """
    plans = []
    selected = set(selected_source_ids)
    allowed = None if allowed_source_ids is None else set(allowed_source_ids)

    def _permitted(source_ids):
        if allowed is None:
            return source_ids
        return [source_id for source_id in source_ids if source_id in allowed]

    facets = _facet_values(results)
    for facet in facets:
        if facet["facet"] in {"statute", "regulation"}:
            source_ids = _permitted(_valid_source_ids(_default_rag_source_ids()))
            followup = f"{facet['value']} {query}".strip()
            reason = f"Expanded from related {facet['facet']} facet: {facet['value']}."
        elif facet["facet"] in {"caseCitation", "issue"}:
            source_ids = _permitted(_valid_source_ids(["ohio-cases"]))
            followup = facet["value"]
            reason = f"Expanded from related {facet['facet']} facet: {facet['value']}."
        else:
            continue
        if not source_ids or _search_key(followup, source_ids) in attempted_searches:
            continue
        plans.append(SearchPlan(query=followup, source_ids=source_ids, reason=reason, facet=facet))

    if allowed is not None:
        return plans

    if "ohio-cases" not in selected and "ohio-cases" in source_guidance()["sources"]:
        followup = query
        if _search_key(followup, ["ohio-cases"]) not in attempted_searches:
            plans.append(SearchPlan(
                query=followup,
                source_ids=["ohio-cases"],
                reason="Added local case law because the initial result set looked incomplete.",
            ))

    if not any(source_id in selected for source_id in _default_rag_source_ids()):
        followup = query
        source_ids = _valid_source_ids(_default_rag_source_ids()[:2])
        if source_ids and _search_key(followup, source_ids) not in attempted_searches:
            if source_ids:
                plans.append(SearchPlan(
                    query=followup,
                    source_ids=source_ids,
                    reason="Added primary and secondary sources because the initial result set lacked legal-library coverage.",
                ))
    return plans


def augmented_search(
    query,
    *,
    connector_registry,
    source_ids=None,
    kinds=None,
    matter=None,
    jurisdiction="",
    limit_per_source=5,
    user=None,
    request=None,
    max_rounds=2,
    minimum_results=4,
    allow_source_expansion=True,
):
    """Run retrieval once, then add bounded related-source follow-ups when needed."""
    try:
        max_rounds = int(max_rounds or 0)
    except (TypeError, ValueError):
        max_rounds = 2
    max_rounds = max(0, min(max_rounds, 3))
    selected_source_ids = list(dict.fromkeys(source_ids or []))
    selected_kinds = kinds or source_kinds(selected_source_ids)
    allowed_source_ids = None if allow_source_expansion else selected_source_ids
    expect_source_diversity = allow_source_expansion or len(selected_kinds) > 1
    results = connector_registry.search(
        query,
        kinds=selected_kinds,
        source_ids=selected_source_ids,
        matter=matter,
        jurisdiction=jurisdiction,
        limit_per_source=limit_per_source,
        user=user,
        request=request,
    )
    rounds = [{
        "query": query,
        "sourceIds": selected_source_ids,
        "resultCount": len(results),
        "reason": "Initial search.",
    }]
    attempted_searches = {_search_key(query, selected_source_ids)}
    added_source_ids = []
    evaluations = []

    for _round_index in range(max_rounds):
        evaluation = evaluate_search_results(
            query, results,
            minimum_results=minimum_results,
            expect_source_diversity=expect_source_diversity,
        )
        evaluations.append(evaluation)
        if evaluation["adequate"]:
            break
        plans = related_source_plans(
            query, results,
            selected_source_ids + added_source_ids,
            attempted_searches,
            allowed_source_ids=allowed_source_ids,
        )
        if not plans and allow_source_expansion:
            auto_selection = automatic_source_selection(query, matter=matter)
            if set(auto_selection["source_ids"]) - set(selected_source_ids + added_source_ids):
                plans = [SearchPlan(
                    query=query,
                    source_ids=auto_selection["source_ids"],
                    reason="Expanded to Auto-selected sources after a weak initial result set.",
                )]
        if not plans:
            break
        for plan in plans[:2]:
            attempted_searches.add(_search_key(plan.query, plan.source_ids))
            plan_kinds = source_kinds(plan.source_ids)
            followup_results = connector_registry.search(
                plan.query,
                kinds=plan_kinds,
                source_ids=plan.source_ids,
                matter=matter,
                jurisdiction=jurisdiction,
                limit_per_source=max(2, min(limit_per_source, 4)),
                user=user,
                request=request,
            )
            results.extend(followup_results)
            added_source_ids.extend(source_id for source_id in plan.source_ids if source_id not in selected_source_ids + added_source_ids)
            rounds.append({
                "query": plan.query,
                "sourceIds": plan.source_ids,
                "resultCount": len(followup_results),
                "reason": plan.reason,
                "facet": plan.facet,
            })
        results = _dedupe_results(results)

    final_evaluation = evaluate_search_results(
        query, results,
        minimum_results=minimum_results,
        expect_source_diversity=expect_source_diversity,
    )
    return {
        "results": results,
        "selected_source_ids": list(dict.fromkeys(selected_source_ids + added_source_ids)),
        "augmentation": {
            "enabled": True,
            "rounds": rounds,
            "evaluations": evaluations,
            "finalEvaluation": final_evaluation,
            "expanded": len(rounds) > 1,
            "maxRounds": max_rounds,
        },
    }

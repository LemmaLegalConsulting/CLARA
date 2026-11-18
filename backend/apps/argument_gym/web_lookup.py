"""Web lookup: the gym's least-trusted check, run after everything else.

The main run argues from the incorporated library: text CLARA holds, with
provenance. This check asks the web the questions that library could not
answer -- an authority the brief cites that the library does not hold, a rule
whose reach the applicability check could not settle, or a rule the brief
relies on loosely, without a citation, that the argument map shows. It is kept apart on purpose:

    open questions       (from the applicability check: authorities the library does not
                          hold, and readings it could not settle)
      -> question planner  (model, inside the provider boundary: de-identified legal questions)
      -> privacy guard     (deterministic: drop any question carrying case facts that identify)
      -> web search        (provider web-search tool, outside the boundary, trusted domains only)
      -> reviewer          (model, inside the boundary: what the web answers mean for this brief)

Nothing here feeds the opponent, judge, or coach; the main run has finished
before this starts and its verdict does not count these findings. Each finding
is stored with ``basis="web"``, carries the trust tier of every source it rests
on, is never more than medium severity, and is marked a lead to check when its
only sources are legal aid sites. A finding that disagrees with the library is
reported as a conflict, and the library is presumed right.

Why the question planner exists: looking a citation up on its own cannot find
the commonest error, which is a correct rule applied to facts it does not
reach. "What does 15 U.S.C. 9058 say?" has a true answer that does not mention
the brief's building. The question that finds the flaw carries the legally
relevant facts -- the kind of property, the program, the financing -- and
nothing that identifies the client. On Azure OpenAI the search runs on
Grounding with Bing outside the Azure data boundary, so the guard is enforced
in code, not left to the planner's instructions.
"""

from __future__ import annotations

import re
from functools import lru_cache

from apps.ai.web_search import (
    UNLISTED,
    WebSearchClient,
    WebSearchUnavailable,
    trust_tiers,
    web_search_enabled,
)
from apps.argument_gym.pipeline import Stage, choice, clean, dumps
from apps.sources.research.citations import find_citation_spans


MAX_QUESTIONS = 4
MAX_FINDINGS = 6
WEB_SEVERITY_CAP = {"high": "medium", "medium": "medium", "low": "low"}
FINDING_KINDS = {"challenge", "corroborates", "library_conflict"}
SEVERITIES = {"high", "medium", "low"}
CONFIDENCES = {"high", "medium", "low"}
LEAD_ONLY_NOTE = (
    "Lead to check: rests only on legal aid, self-help, or unreviewed websites, which may explain the law "
    "but are not authority."
)
OFFICIAL_NOTE = (
    "Rests on an official government website found by web search, not on CLARA's library; "
    "confirm the text and that it is current before relying on it."
)



# What the main run could not settle


def open_questions(applicability_report):
    """Authorities the library does not hold, and readings the applicability check left open."""
    report = applicability_report or {}
    missing = [
        {"authority": entry["authority"], "kind": entry["kind"], "reason": "not_in_library"}
        for entry in report.get("authorities") or []
        if not entry.get("held")
    ]
    unsettled = [
        {
            "authority": item["authority"],
            "reason": "applicability_unclear" if item["verdict"] == "unclear_on_facts" else "not_assessed",
            "condition": item.get("condition", ""),
            "grounded": item.get("grounded", False),
        }
        for item in report.get("assessments") or []
        if item["verdict"] in {"unclear_on_facts", "not_assessed"}
    ]
    return missing + unsettled


# Privacy guard


IDENTIFYING_PATTERNS = (
    ("a street address", re.compile(
        r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|"
        r"Boulevard|Blvd|Way|Place|Pl|Circle|Cir|Parkway|Pkwy|Terrace|Ter)\b\.?")),
    ("a date", re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
        r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b")),
    ("a dollar amount", re.compile(r"\$\s?\d")),
    ("a case number", re.compile(r"\b(?:case\s+no\.?|CV|CVG|CVF|CA)[\s-]*\d{2,}[-\d]*", re.I)),
    ("an email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("a telephone number", re.compile(r"\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")),
    ("a unit number", re.compile(r"\b(?:Unit|Apt\.?|Apartment|Suite)\s+#?\w+\b")),
)
VERBATIM_WORDS = 8


@lru_cache(maxsize=1)
def _legal_vocabulary():
    """Words the enacted-law corpora use; a capitalized word outside it is probably a name.

    Read from the statutes so the vocabulary is the law's own rather than a
    list someone has to keep up, and so a generic term ("Metropolitan Housing
    Authority") passes while a particular one ("Lakeview") does not.
    """
    from apps.sources.research.index import research_index

    words = set()
    try:
        for record in research_index().records:
            if record.corpus == "statutes":
                words.update(re.findall(r"[a-z]{3,}", record.text.casefold()))
    except Exception:  # noqa: BLE001 - without a vocabulary every proper noun is treated as identifying
        return frozenset()
    return frozenset(words)


def identifying_phrases(brief_text, matter_context=None):
    """Names in the brief and the matter that must not leave the boundary."""
    vocabulary = _legal_vocabulary()
    phrases = set()
    for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|of|and|&))*", brief_text or ""):
        words = re.findall(r"[A-Za-z]+", match.group(0))
        for word in words:
            if word[0].isupper() and word.casefold() not in vocabulary and len(word) > 2:
                phrases.add(word)
    # A caption sets the parties in capitals ("RIVERBEND METROPOLITAN HOUSING
    # AUTHORITY, Plaintiff"), and the body then calls them "Plaintiff", so a
    # name can appear nowhere in title case. Read runs of two or more capitalized
    # words; a lone acronym ("the CARES Act", "HUD") is a term, not a name.
    for match in re.finditer(r"\b[A-Z][A-Z'.&-]+(?:[ \t]+[A-Z][A-Z'.&-]+)+\b", brief_text or ""):
        for word in re.findall(r"[A-Z]{3,}", match.group(0)):
            if word.casefold() not in vocabulary:
                phrases.update({word, word.title()})
    context = matter_context or {}
    for value in [context.get("client"), context.get("id"), *(context.get("details") or {}).values()]:
        value = str(value or "").strip()
        if 2 < len(value) <= 80:
            phrases.add(value)
            phrases.update(word for word in re.findall(r"[A-Za-z]{3,}", value) if word.casefold() not in vocabulary)
    return phrases


def _without_citations(text):
    text = str(text or "")
    for _citation, (start, end) in sorted(find_citation_spans(text), key=lambda item: -item[1][0]):
        text = text[:start] + " " + text[end:]
    return text


def privacy_check(question, *, blocked_phrases, brief_text=""):
    """The reason a question may not be sent, or empty if it may."""
    for label, pattern in IDENTIFYING_PATTERNS:
        if pattern.search(question):
            return f"It contains {label}."
    for phrase in blocked_phrases:
        if re.search(rf"\b{re.escape(phrase)}\b", question):
            return f"It contains a name from the brief or the case ({phrase[:1]}…)."
    # Citations are how a legal question names its subject, and they identify
    # no one; a question repeating "15 U.S.C. 9058(a)(2)" from the brief is not
    # quoting the brief. Compare what is left once they are taken out.
    brief_words = re.findall(r"\w+", _without_citations(brief_text).casefold())
    if len(brief_words) >= VERBATIM_WORDS:
        shingles = {" ".join(brief_words[i : i + VERBATIM_WORDS]) for i in range(len(brief_words) - VERBATIM_WORDS + 1)}
        question_words = re.findall(r"\w+", _without_citations(question).casefold())
        for i in range(len(question_words) - VERBATIM_WORDS + 1):
            if " ".join(question_words[i : i + VERBATIM_WORDS]) in shingles:
                return f"It repeats {VERBATIM_WORDS} or more words of the brief verbatim."
    return ""


# Question planning


def _fallback_questions(open_items):
    """Without a model: one plain question per statute or rule the library does not hold."""
    questions = []
    for item in open_items:
        if item["reason"] != "not_in_library" or item.get("kind") in {"reporter", "ohio_public_domain"}:
            continue
        questions.append(
            {
                "question": (
                    f"What does {item['authority']} require, to whom and to what kinds of property or "
                    "programs does it apply, and is it currently in effect or recently amended?"
                ),
                "authority": item["authority"],
                "why": "The brief relies on this authority and CLARA's library does not hold it.",
                "unitIds": [],
            }
        )
    return questions[:MAX_QUESTIONS]


def plan_questions(argument_map, open_items, main_findings, *, jurisdiction, llm_client=None):
    def parse(payload):
        cleaned = []
        for item in payload.get("questions") or []:
            if not isinstance(item, dict):
                continue
            question = clean(item.get("question"), limit=600)
            if not question:
                continue
            cleaned.append(
                {
                    "question": question,
                    "authority": clean(item.get("authority"), limit=200),
                    "why": clean(item.get("why"), limit=400),
                    "unitIds": [str(value) for value in item.get("unitIds") or []][:4],
                }
            )
        return cleaned[:MAX_QUESTIONS]

    return Stage("web_questions", llm_client=llm_client).run(
        prompt_key="argument_gym.web_questions",
        context={
            "jurisdiction": jurisdiction or "the filing jurisdiction",
            "argument_map": dumps(argument_map),
            "open_questions": dumps(open_items),
            "main_findings": dumps(main_findings),
            "max_questions": MAX_QUESTIONS,
        },
        parse=parse,
        fallback=lambda: _fallback_questions(open_items),
        empty_key="questions",
    )


# Review


def review_results(units, argument_map, main_findings, searched, *, jurisdiction, llm_client=None):
    unit_ids = {unit["id"] for unit in units}
    usable_urls = {
        citation["url"]
        for item in searched
        for citation in item["result"]["citations"]
        if citation["trustTier"] != UNLISTED
    }

    def parse(payload):
        cleaned = []
        for item in payload.get("findings") or []:
            if not isinstance(item, dict):
                continue
            urls = [url for url in item.get("sourceUrls") or [] if url in usable_urls]
            if not urls:
                # A web finding with no source from the reviewed list is the
                # model's own opinion, which this check does not report.
                continue
            unit_id = str(item.get("unitId") or "")
            cleaned.append(
                {
                    "kind": choice(item.get("kind"), FINDING_KINDS, "challenge"),
                    "unitId": unit_id if unit_id in unit_ids else "",
                    "question": clean(item.get("question"), limit=600),
                    "finding": clean(item.get("finding"), limit=160),
                    "argument": clean(item.get("argument"), limit=2000),
                    "whyItMatters": clean(item.get("whyItMatters"), limit=1000),
                    "suggestedResponse": clean(item.get("suggestedResponse"), limit=1500),
                    "severity": choice(item.get("severity"), SEVERITIES, "medium"),
                    "confidence": choice(item.get("confidence"), CONFIDENCES, "low"),
                    "sourceUrls": urls,
                    "libraryCandidate": clean(item.get("libraryCandidate"), limit=200),
                }
            )
        return cleaned[:MAX_FINDINGS]

    return Stage("web_review", llm_client=llm_client).run(
        prompt_key="argument_gym.web_review",
        context={
            "jurisdiction": jurisdiction or "the filing jurisdiction",
            "brief_units": dumps([{"id": unit["id"], "text": unit["text"][:1200]} for unit in units][:60]),
            "argument_map": dumps(argument_map),
            "main_findings": dumps(main_findings),
            "web_results": dumps(
                [
                    {
                        "question": item["question"]["question"],
                        "answer": item["result"]["answer"][:4000],
                        "sources": [
                            {"url": c["url"], "title": c["title"], "trust": c["trustLabel"]}
                            for c in item["result"]["citations"]
                            if c["trustTier"] != UNLISTED
                        ],
                    }
                    for item in searched
                ]
            ),
            "trust_tiers": dumps([tier.to_dict() for tier in trust_tiers()]),
        },
        parse=parse,
        # Without a model nothing can say what a web answer means for this
        # brief, so nothing is reported as a finding; the answers stay visible.
        fallback=lambda: [],
        empty_key="findings",
    )


def grade(finding, citations_by_url):
    """Cap a web finding's severity and say how far the sources it rests on go."""
    sources = [citations_by_url[url] for url in finding["sourceUrls"] if url in citations_by_url]
    best = min((source["trustRank"] for source in sources), default=99)
    official = [tier for tier in trust_tiers() if tier.id == "official_government"]
    official_rank = official[0].rank if official else 2
    legal_aid_only = best > official_rank
    severity = WEB_SEVERITY_CAP[finding["severity"]]
    confidence = finding["confidence"]
    if legal_aid_only:
        severity, confidence = "low", "low"
    if confidence == "high":
        # The model chose the quote, and nobody has read the page in CLARA.
        confidence = "medium"
    return {
        **finding,
        "severity": severity,
        "confidence": confidence,
        "sources": sources,
        "bestTrustRank": best,
        "leadOnly": legal_aid_only,
        "trustNote": LEAD_ONLY_NOTE if legal_aid_only else OFFICIAL_NOTE,
    }


# The check


def summarize_main_findings(challenges, rule_audit):
    return {
        "challenges": [
            {"finding": challenge.finding, "argument": challenge.opponent_argument[:600], "category": challenge.category}
            for challenge in challenges
        ],
        "ruleAudit": [
            {
                "citation": audit["citation"],
                "unmet": [element["label"] for element in audit["elements"] if element.get("unmet")],
            }
            for audit in rule_audit or []
        ],
    }


def run_web_lookup(
    units,
    argument_map,
    brief_text,
    main_findings,
    *,
    jurisdiction="",
    matter_context=None,
    applicability_report=None,
    llm_client=None,
    search_client=None,
):
    """Plan, guard, search, and review. Returns (report, graded findings). Never raises."""
    report = {
        "status": "ran",
        "basis": "web",
        "summary": "",
        "openQuestions": [],
        "questions": [],
        "withheld": [],
        "searches": [],
        "findings": [],
        "trustTiers": [tier.to_dict() for tier in trust_tiers()],
        "stages": [],
    }
    if not web_search_enabled():
        report.update(
            status="unavailable",
            summary="Web lookup could not run: web search is turned off for this installation (AI_WEB_SEARCH_ENABLED).",
        )
        return report, []
    try:
        client = search_client or WebSearchClient()
    except WebSearchUnavailable as exc:
        report.update(status="unavailable", summary=f"Web lookup could not run: {exc}")
        return report, []

    open_items = open_questions(applicability_report)
    report["openQuestions"] = open_items
    questions, trace = plan_questions(
        argument_map, open_items, main_findings, jurisdiction=jurisdiction, llm_client=llm_client
    )
    report["stages"].append(trace)

    blocked = identifying_phrases(brief_text, matter_context)
    allowed = []
    for question in questions:
        reason = privacy_check(question["question"], blocked_phrases=blocked, brief_text=brief_text)
        if reason:
            # The question itself is not echoed: it is what failed the guard.
            report["withheld"].append({"authority": question["authority"], "reason": reason})
        else:
            allowed.append(question)
    report["questions"] = allowed

    from apps.argument_gym.pipeline import ai_enabled  # local: keeps import order simple

    instructions = (
        "You answer a lawyer's research question about United States and Ohio housing law using web search. "
        "Quote or closely paraphrase the governing text and cite the page for every statement of law. "
        "Say plainly when the sources do not answer the question, when they conflict, or when a rule "
        "may have been amended, delayed, or repealed. Do not guess."
    )
    searched = []
    for question in allowed:
        try:
            result = client.search(question["question"], instructions=instructions)
        except (WebSearchUnavailable, ValueError) as exc:
            report["searches"].append({"question": question["question"], "status": "failed", "error": str(exc)[:300]})
            continue
        payload = result.to_dict()
        report["searches"].append({**payload, "status": "complete"})
        searched.append({"question": question, "result": payload})

    if not allowed:
        report["summary"] = (
            "Web lookup ran and asked nothing: the main checks left no open question of law for the web."
            if not report["withheld"]
            else "Web lookup ran but withheld every question it planned, because each carried case details."
        )
        return report, []
    if not searched:
        report.update(status="failed", summary="Web lookup could not run: every web search failed. See the searches below.")
        return report, []

    findings, trace = review_results(
        units, argument_map, main_findings, searched, jurisdiction=jurisdiction, llm_client=llm_client
    )
    report["stages"].append(trace)
    citations_by_url = {
        citation["url"]: citation for item in searched for citation in item["result"]["citations"]
    }
    graded = [grade(finding, citations_by_url) for finding in findings]
    report["findings"] = graded
    if not ai_enabled():
        report["summary"] = (
            "Web lookup searched but could not review the answers, because no model is available; "
            "the answers are shown below without findings."
        )
    else:
        counts = {kind: sum(1 for item in graded if item["kind"] == kind) for kind in FINDING_KINDS}
        report["summary"] = (
            f"Web lookup asked {len(searched)} question(s) of trusted websites and reported "
            f"{counts['challenge']} challenge(s), {counts['library_conflict']} conflict(s) with CLARA's library, "
            f"and {counts['corroborates']} corroboration(s). These come from web search, not CLARA's library."
        )
    return report, graded

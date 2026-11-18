"""Web search through the provider's Responses API, labelled by how far to trust it.

This is the least trustworthy source CLARA consults, and everything here exists
to keep that visible:

* ``content/web-research/trusted-domains.yaml`` decides what is searched. In
  ``prefer`` mode the search is open, the model is told which sources to
  prefer, and blocked domains are excluded; in ``restrict`` mode only listed
  domains are searched.
* Every cited URL carries the trust tier of its domain. In prefer mode a site
  on no list falls in the file's catch-all tier (rank 4, "other website"). In
  restrict mode -- or with no catch-all -- it is ``unlisted`` and callers must
  not use it as a source.
* The question sent is the caller's, and callers must send only legal
  questions about named authorities. On Azure OpenAI the search runs on
  Grounding with Bing, outside the Azure compliance boundary and the Microsoft
  DPA; brief text and client facts must never be in it. ``WebSearchClient``
  refuses a question longer than ``MAX_QUESTION_CHARS`` as a backstop, because
  a pasted passage is the likeliest way that rule gets broken.

Works with OpenAI and with Azure OpenAI's v1 endpoint
(``https://<resource>.openai.azure.com/openai/v1/``), which accept the same
``responses.create`` call through the standard ``OpenAI`` client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

import yaml
from django.conf import settings

from apps.ai.openai_client import OpenAIBackendError, OpenAICompatibleClient
from apps.core.content_library import content_path


TRUSTED_DOMAINS_PATH = ("web-research", "trusted-domains.yaml")
MAX_ALLOWED_DOMAINS = 100
MAX_QUESTION_CHARS = 600
UNLISTED = "unlisted"
# Rank 1 is the incorporated library, which is not web search; see the YAML.
LIBRARY_RANK = 1


class WebSearchUnavailable(RuntimeError):
    """Web search is turned off, misconfigured, or the provider refused it."""


@dataclass(frozen=True)
class TrustTier:
    id: str
    rank: int
    label: str
    reader_note: str
    domains: tuple
    domain_suffixes: tuple = ()
    catch_all: bool = False

    def to_dict(self):
        return {"id": self.id, "rank": self.rank, "label": self.label, "readerNote": self.reader_note}

    def hosts(self, host):
        if any(host == domain or host.endswith(f".{domain}") for domain in self.domains):
            return True
        return any(host.endswith(suffix) for suffix in self.domain_suffixes)


UNLISTED_TIER = TrustTier(
    id=UNLISTED,
    rank=99,
    label="Unlisted website (not used)",
    reader_note="This site is not on the reviewed list of domains, so nothing it says was used.",
    domains=(),
)
PREFER, RESTRICT = "prefer", "restrict"


@dataclass(frozen=True)
class SearchPolicy:
    mode: str
    tiers: tuple
    blocked_domains: tuple

    @property
    def listed_domains(self):
        return list(dict.fromkeys(domain for tier in self.tiers for domain in tier.domains))

    @property
    def catch_all(self):
        if self.mode != PREFER:
            return None
        return next((tier for tier in self.tiers if tier.catch_all), None)


@dataclass
class WebSearchResult:
    question: str
    answer: str
    citations: list = field(default_factory=list)
    searches: list = field(default_factory=list)
    model: str = ""

    @property
    def usable_citations(self):
        return [citation for citation in self.citations if citation["trustTier"] != UNLISTED]

    def to_dict(self):
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": self.citations,
            "searches": self.searches,
            "model": self.model,
        }


def _clean_domain(value):
    value = str(value or "").strip().casefold()
    value = value.split("://", 1)[-1].split("/", 1)[0]
    return value[4:] if value.startswith("www.") else value


@lru_cache(maxsize=4)
def _load_policy(path_text, _mtime):
    payload = yaml.safe_load(open(path_text, encoding="utf-8")) or {}
    mode = str(payload.get("mode") or PREFER).strip()
    if mode not in {PREFER, RESTRICT}:
        raise ValueError(f"Web search mode must be {PREFER!r} or {RESTRICT!r}, not {mode!r}.")
    tiers = []
    for tier_id, tier in (payload.get("tiers") or {}).items():
        domains = tuple(dict.fromkeys(_clean_domain(domain) for domain in tier.get("domains") or [] if domain))
        tiers.append(
            TrustTier(
                id=str(tier_id),
                rank=int(tier.get("rank", 50)),
                label=str(tier.get("label") or tier_id),
                reader_note=str(tier.get("reader_note") or "").strip(),
                domains=domains,
                domain_suffixes=tuple(str(item).casefold() for item in tier.get("domain_suffixes") or []),
                catch_all=bool(tier.get("catch_all", False)),
            )
        )
    for tier in tiers:
        if tier.rank <= LIBRARY_RANK:
            raise ValueError(
                f"Web tier {tier.id!r} claims rank {tier.rank}; rank {LIBRARY_RANK} and above is reserved "
                "for the incorporated library, which no website outranks."
            )
    blocked = tuple(dict.fromkeys(_clean_domain(domain) for domain in payload.get("blocked_domains") or [] if domain))
    return SearchPolicy(mode=mode, tiers=tuple(sorted(tiers, key=lambda tier: tier.rank)), blocked_domains=blocked)


def search_policy():
    """The reviewed search policy; an absent file means no web search at all."""
    path = content_path(*TRUSTED_DOMAINS_PATH)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return SearchPolicy(mode=RESTRICT, tiers=(), blocked_domains=())
    return _load_policy(str(path), mtime)


def trust_tiers():
    """The reviewed trust tiers, most trusted first."""
    return search_policy().tiers


def allowed_domains():
    """Domains passed as a hard allow-list: only in restrict mode."""
    policy = search_policy()
    if policy.mode != RESTRICT:
        return []
    domains = policy.listed_domains
    if len(domains) > MAX_ALLOWED_DOMAINS:
        raise ValueError(f"{len(domains)} domains are listed; providers accept at most {MAX_ALLOWED_DOMAINS}.")
    return domains


def preference_instructions():
    """What the model is told to prefer, naming the listed sources as examples."""
    policy = search_policy()
    lines = []
    for tier in policy.tiers:
        if tier.catch_all or not (tier.domains or tier.domain_suffixes):
            continue
        examples = ", ".join([*tier.domains[:14], *(f"any {suffix} site" for suffix in tier.domain_suffixes)])
        lines.append(f"- {tier.label.split(' (')[0]}: {examples}")
    if not lines:
        return ""
    return (
        "Prefer, in this order, official government sources and then trustworthy nonprofit legal aid "
        "content, especially from Ohio. Examples:\n" + "\n".join(lines) + "\n"
        "Avoid law-firm marketing, forums, and pages whose author or date you cannot tell. "
        "When a primary source (statute, regulation, court opinion) is available, cite it rather than a summary of it."
    )


def tier_for_url(url):
    """The most trusted tier that hosts ``url``; the catch-all in prefer mode; otherwise unlisted."""
    host = _clean_domain(urlsplit(str(url or "")).netloc or url)
    policy = search_policy()
    if any(host == domain or host.endswith(f".{domain}") for domain in policy.blocked_domains):
        return UNLISTED_TIER
    for tier in policy.tiers:
        if not tier.catch_all and tier.hosts(host):
            return tier
    return policy.catch_all or UNLISTED_TIER


def web_search_enabled():
    policy = search_policy()
    return bool(getattr(settings, "AI_WEB_SEARCH_ENABLED", False)) and bool(policy.tiers)


def _get(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def parse_response(response, *, question, model=""):
    """Pull the answer, its URL citations, and the searches out of a Responses API reply."""
    answer_parts, citations, searches = [], [], []
    seen = set()
    for item in _get(response, "output", None) or []:
        kind = _get(item, "type")
        if kind == "web_search_call":
            action = _get(item, "action") or {}
            searches.append(
                {
                    "type": _get(action, "type", ""),
                    "query": _get(action, "query", "") or "",
                    "sources": [_get(source, "url", "") for source in _get(action, "sources", None) or []],
                }
            )
        elif kind == "message":
            for content in _get(item, "content", None) or []:
                if _get(content, "type") != "output_text":
                    continue
                answer_parts.append(_get(content, "text", "") or "")
                for annotation in _get(content, "annotations", None) or []:
                    if _get(annotation, "type") != "url_citation":
                        continue
                    url = _get(annotation, "url", "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    tier = tier_for_url(url)
                    citations.append(
                        {
                            "url": url,
                            "title": _get(annotation, "title", "") or "",
                            "domain": _clean_domain(urlsplit(url).netloc),
                            "trustTier": tier.id,
                            "trustRank": tier.rank,
                            "trustLabel": tier.label,
                        }
                    )
    return WebSearchResult(
        question=question,
        answer="\n".join(part for part in answer_parts if part).strip(),
        citations=citations,
        searches=searches,
        model=model,
    )


class WebSearchClient:
    """One web-search question at a time, restricted to the trusted domains."""

    def __init__(self, *, client=None, model=None):
        if not web_search_enabled():
            raise WebSearchUnavailable(
                "Web search is turned off (AI_WEB_SEARCH_ENABLED) or content/web-research/trusted-domains.yaml lists no tiers."
            )
        self._client = client
        self.model = model or getattr(settings, "AI_WEB_SEARCH_MODEL", "") or ""

    def _transport(self):
        if self._client is not None:
            return self._client
        try:
            return OpenAICompatibleClient().client
        except OpenAIBackendError as exc:
            raise WebSearchUnavailable(str(exc)) from exc

    def search(self, question, *, instructions, model=None, reasoning_level=None):
        question = str(question or "").strip()
        if not question:
            raise ValueError("A web search needs a question.")
        if len(question) > MAX_QUESTION_CHARS:
            # A long question is almost always a pasted passage, and a pasted
            # passage is how client text would leave the data boundary.
            raise ValueError(
                f"A web-search question is limited to {MAX_QUESTION_CHARS} characters so brief text cannot be sent."
            )
        selected_model = model or self.model or OpenAICompatibleClient_model()
        policy = search_policy()
        filters = {}
        if policy.mode == RESTRICT:
            filters["allowed_domains"] = allowed_domains()
        if policy.blocked_domains:
            filters["blocked_domains"] = list(policy.blocked_domains)
        tool = {"type": getattr(settings, "AI_WEB_SEARCH_TOOL", "web_search") or "web_search"}
        if filters:
            tool["filters"] = filters
        preference = preference_instructions() if policy.mode == PREFER else ""
        request = {
            "model": selected_model,
            "instructions": f"{instructions}\n\n{preference}".strip(),
            "input": question,
            "tools": [tool],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
        }
        if reasoning_level:
            request["reasoning"] = {"effort": reasoning_level}
        transport = self._transport()
        try:
            response = transport.responses.create(**request)
        except Exception as exc:  # noqa: BLE001 - provider errors vary; the caller reports, never raises
            if "blocked_domains" not in filters:
                raise WebSearchUnavailable(f"The provider did not complete the web search: {exc}") from exc
            # Not every deployment accepts a block list. Retry without it; the
            # tiering below still refuses a blocked domain as a source.
            filters.pop("blocked_domains")
            if not filters:
                tool.pop("filters")
            try:
                response = transport.responses.create(**request)
            except Exception as retry_exc:  # noqa: BLE001
                raise WebSearchUnavailable(f"The provider did not complete the web search: {retry_exc}") from retry_exc
        return parse_response(response, question=question, model=selected_model)


def OpenAICompatibleClient_model():  # noqa: N802 - mirrors the client it reads from
    """The configured default model, without building a transport."""
    from apps.sources.models import SourceConfiguration

    return SourceConfiguration.effective_settings("openai", {"model": settings.OPENAI_MODEL})["model"]

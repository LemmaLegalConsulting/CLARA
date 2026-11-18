"""Retrieval connector for generated, provider-neutral legal-content chunks."""

from __future__ import annotations

import json
import re

from django.conf import settings

from apps.ai.openai_client import OpenAIBackendError, OpenAICompatibleClient
from apps.ai.prompt_catalog import render_prompt
from apps.core.content_library import content_path
from apps.sources.connectors.base import SourceConnector, SourceResult
from apps.sources.library import load_manifest
from apps.sources.ordinances import notice_snippet, pending_notices


QUERY_STOPWORDS = {
    "about", "after", "against", "also", "and", "are", "can", "does", "for", "from", "have", "how",
    "into", "may", "must", "not", "of", "or", "the", "this", "to", "under", "what", "when", "with",
}
CONCEPT_EXPANSIONS = {
    "habitability": ("defective", "condition", "repair", "health", "safety", "premises"),
    "condition": ("defective", "repair", "premises", "health", "safety"),
    "conditions": ("defective", "repair", "premises", "health", "safety"),
    "defense": ("eviction", "counterclaim", "remedy", "claim"),
    "disputed": ("nonpayment", "payment", "rent", "deposit"),
    "assistance": ("assisted", "housing", "hud", "subsidy", "voucher", "termination"),
    "rental": ("assisted", "housing", "subsidy", "voucher"),
    "subsidized": ("hud", "assisted", "section 8", "voucher", "tenant rent"),
    "voucher": ("hud", "section 8", "assisted housing"),
    "recertification": ("hud", "tenant rent", "household income"),
}
SEMANTIC_GROUPS = {
    "habitability": ("defective", "fit and habitable", "maintain", "repair", "health", "safety"),
    "rent": ("rent abatement", "nonpayment", "rent depositing", "payment", "deposit"),
    "assistance": ("assisted", "housing choice voucher", "hud", "subsidy", "rental assistance"),
    "defense": ("common defenses", "defense", "counterclaim"),
}

# UI source IDs deliberately map to logical content-library documents rather
# than filesystem paths.  This preserves the content-provider boundary.
RAG_SOURCE_DOCUMENTS = {
    "ohio-statutes": {"ohio-revised-code", "ohio-administrative-code"},
    "federal-housing-law": {"federal-housing-law"},
    "treatise": {"ohio-eviction-landlord-tenant-law-6e"},
    "hud-handbook": {"hud-4350-3-rev-1"},
    "green-book": {"green-book"},
}

# Local law is one logical source made of many documents -- one per
# municipality -- and municipalities are added by editing scope.yaml, not this
# file.  Selecting it by the kind its manifests declare keeps a newly ingested
# city searchable the moment its manifest exists.
RAG_SOURCE_CONTENT_KINDS = {"ohio-ordinances": "ordinance"}


def _terms(value):
    return [term for term in re.findall(r"[a-z0-9]+", (value or "").casefold()) if len(term) > 2 and term not in QUERY_STOPWORDS]


def _expanded_terms(query):
    """Add legal-concept neighbors before ranking; this is the offline semantic fallback."""
    original = _terms(query)
    expanded = list(original)
    for term in original:
        expanded.extend(CONCEPT_EXPANSIONS.get(term, ()))
    return original, list(dict.fromkeys(expanded))


def _active_semantic_groups(original_terms):
    groups = set()
    for term in original_terms:
        if term in {"habitability", "condition", "conditions", "repair", "repairs"}:
            groups.add("habitability")
        if term in {"rent", "disputed", "payment", "nonpayment"}:
            groups.add("rent")
        if term in {"assistance", "rental", "subsidy", "voucher"}:
            groups.add("assistance")
        if term in {"defense", "defences", "defenses", "counterclaim"}:
            groups.add("defense")
    return groups


def _source_text(path):
    """Return the substantive portion of a generated chunk, not its repeated metadata."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "## Source text"
    return text.split(marker, 1)[-1].strip() if marker in text else text.strip()


def _snippet(text, terms, *, length=500):
    compact = " ".join(text.split())
    if not compact:
        return ""
    positions = [compact.casefold().find(term) for term in terms]
    start_at = min((position for position in positions if position >= 0), default=0)
    start = max(start_at - 100, 0)
    end = min(start + length, len(compact))
    return f"{'… ' if start else ''}{compact[start:end]}{' …' if end < len(compact) else ''}"


class ContentLibraryTreatiseConnector(SourceConnector):
    """Search Markdown chunks generated from the legal content library.

    Manifests are the index boundary.  The connector deliberately uses logical
    paths under ``CONTENT_LIBRARY_DIR`` so it can be backed by a staged
    SharePoint provider later without changing callers.
    """

    kind = "rag"
    label = "Treatises and handbooks"
    status = "Indexed"
    detail = "Heading-preserving chunks from the managed legal-content library"

    def __init__(self):
        self._cache_key = None
        self._chunks = []

    def _manifest_paths(self):
        return sorted([
            *content_path("treatises", "markdown").glob("*/*/manifest.yaml"),
            *content_path("statutes").glob("*/manifest.yaml"),
            *content_path("ordinances").glob("*/manifest.yaml"),
        ])

    def _load_chunks(self):
        manifests = self._manifest_paths()
        cache_key = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in manifests)
        if cache_key == self._cache_key:
            return self._chunks

        chunks = []
        for manifest_path in manifests:
            manifest = load_manifest(manifest_path)
            if not manifest:
                continue
            for item in manifest.get("chunks", []):
                if not isinstance(item, dict) or not item.get("file"):
                    continue
                chunk_path = manifest_path.parent / item["file"]
                if not chunk_path.is_file():
                    continue
                chunks.append({
                    "path": chunk_path,
                    "id": item.get("id"),
                    "heading": item.get("heading", "Untitled section"),
                    "section_path": item.get("path", []),
                    "pages": item.get("pages", []),
                    "content_kind": item.get("content_kind", "substantive-section"),
                    "document_slug": manifest.get("document_slug", ""),
                    "document_title": manifest.get("document_title", "Treatise"),
                    "document_kind": manifest.get("content_kind", "treatise"),
                    "document_version": manifest.get("document_version", ""),
                    "source_path": item.get("source_path") or manifest.get("source_path", ""),
                    "source_sha256": item.get("source_sha256") or manifest.get("source_sha256", ""),
                    "retrieval_hints": manifest.get("retrieval_hints", []) + item.get("retrieval_hints", []),
                    "citation": item.get("citation", ""),
                    "url": item.get("url", ""),
                    "effective_date": item.get("effective_date", ""),
                    "jurisdiction": manifest.get("jurisdiction", ""),
                    # An ordinance record states what its text *is* -- an
                    # enacted act, a published court rule -- because that
                    # decides whether a reader may rely on it as it stands.
                    "text_basis": item.get("text_basis", ""),
                })
        self._cache_key, self._chunks = cache_key, chunks
        return chunks

    @staticmethod
    def _score(chunk, original_terms, expanded_terms, semantic_groups):
        if re.search(r"\bv\.\s|\bWL\s+\d|\bF\.\s*(?:2d|3d|Supp)", chunk["heading"]):
            # A converter artifact promoted this case citation to a heading.
            # The surrounding actual section remains searchable.
            return 0
        haystack = "\n".join([chunk["heading"], *chunk["section_path"], _source_text(chunk["path"])]).casefold()
        retrieval_hints = " ".join(chunk.get("retrieval_hints") or []).casefold()
        original_hits = sum(term in haystack for term in original_terms)
        hint_hits = sum(term in retrieval_hints for term in expanded_terms)
        if not original_hits and not hint_hits:
            return 0
        heading = " ".join([chunk["heading"], *chunk["section_path"]]).casefold()
        expanded_hits = sum(term in haystack for term in expanded_terms)
        # Heading/path hits are stronger than coincidental hits in a case citation.
        semantic_hits = sum(any(phrase in heading for phrase in SEMANTIC_GROUPS[group]) for group in semantic_groups)
        return original_hits * 12 + expanded_hits * 3 + sum(term in heading for term in expanded_terms) * 6 + semantic_hits * 20 + hint_hits * 25

    @staticmethod
    def _ai_rerank(query, ranked):
        """Use only chunk metadata to check relevance; source text stays for the answer stage."""
        if not settings.AI_DRAFTING_ENABLED or not ranked:
            return ranked
        candidates = [
            {"id": chunk["id"], "kind": chunk["content_kind"], "path": " > ".join(chunk["section_path"])}
            for _score, chunk in ranked[:24]
        ]
        prompt = render_prompt(
            "research.treatise_relevance",
            query=query,
            candidates=json.dumps(candidates, ensure_ascii=False),
        )
        try:
            response = OpenAICompatibleClient().complete(
                system=prompt.system,
                user=prompt.user,
                temperature=0,
                model=prompt.default_model,
                reasoning_level=prompt.default_reasoning_level,
            )
            selected = json.loads(response)
            identifiers = selected.get("relevant_chunk_ids", []) if isinstance(selected, dict) else []
            order = {identifier: index for index, identifier in enumerate(identifiers) if isinstance(identifier, str)}
            return sorted(ranked, key=lambda item: (order.get(item[1]["id"], len(order)), -item[0], item[1]["id"] or ""))
        except (OpenAIBackendError, ValueError, TypeError, json.JSONDecodeError):
            return ranked

    @staticmethod
    def _citation(chunk):
        if chunk["citation"]:
            effective = f" (effective {chunk['effective_date']})" if chunk["effective_date"] else ""
            return f"{chunk['citation']}{effective}"
        path = " > ".join(chunk["section_path"])
        pages = chunk["pages"]
        page_text = f"PDF p. {pages[0]}" if len(pages) == 1 or pages[0] == pages[-1] else f"PDF pp. {pages[0]}–{pages[-1]}"
        version = f", {chunk['document_version']}" if chunk["document_version"] else ""
        return f"{chunk['document_title']}{version}, {path} ({page_text})"

    def _coverage_results(self, query):
        """Results that report a gap instead of letting another city fill it."""
        results = []
        for notice in pending_notices(query):
            results.append(SourceResult(
                id=notice["id"],
                title=f"{notice['municipality']} — {notice['topicLabel'] or notice['title']} (text not held)",
                snippet=notice_snippet(notice),
                source_kind=self.kind,
                source_label="Local law coverage",
                citation=notice["citation"],
                url=notice["url"],
                metadata={
                    "resultType": "coverage-notice",
                    "documentKind": "ordinance",
                    "textBasis": "not_acquired",
                    "municipality": notice["municipality"],
                    "topic": notice["topic"],
                    "pendingReason": notice["reason"],
                },
            ))
        return results

    def search(self, query, *, matter=None, jurisdiction="", limit=5, user=None, request=None, source_ids=None):
        original_terms, expanded_terms = _expanded_terms(query)
        if not original_terms:
            return []
        ranked = []
        semantic_groups = _active_semantic_groups(original_terms)
        selected_documents = set()
        selected_kinds = set()
        for source_id in source_ids or []:
            selected_documents.update(RAG_SOURCE_DOCUMENTS.get(source_id, set()))
            if source_id in RAG_SOURCE_CONTENT_KINDS:
                selected_kinds.add(RAG_SOURCE_CONTENT_KINDS[source_id])
        ordinances_in_scope = not source_ids or "ordinance" in selected_kinds
        chunks = self._load_chunks()
        if selected_documents or selected_kinds:
            chunks = [
                chunk for chunk in chunks
                if chunk["document_slug"] in selected_documents or chunk["document_kind"] in selected_kinds
            ]
        for chunk in chunks:
            score = self._score(chunk, original_terms, expanded_terms, semantic_groups)
            if score:
                ranked.append((score, chunk))
        ranked.sort(key=lambda item: (-item[0], item[1]["document_title"], item[1]["id"] or ""))
        ranked = self._ai_rerank(query, ranked)
        # A gap in local-law coverage outranks a different city's ordinance:
        # the reader asked about their own city, and a confident answer drawn
        # from somewhere else is the wrong one.
        results = self._coverage_results(query) if ordinances_in_scope else []
        limit = max(limit - len(results), 1) if results else limit
        for _score_value, chunk in ranked[:limit]:
            text = _source_text(chunk["path"])
            results.append(SourceResult(
                id=f"content:{chunk['document_slug']}:{chunk['id']}",
                title=f"{chunk['document_title']} — {chunk['heading']}",
                snippet=_snippet(text, original_terms),
                source_kind=self.kind,
                source_label="Managed legal content library",
                citation=self._citation(chunk),
                url=chunk["url"],
                metadata={
                    "chunkId": chunk["id"],
                    "documentSlug": chunk["document_slug"],
                    "documentVersion": chunk["document_version"],
                    "sectionPath": chunk["section_path"],
                    "contentKind": chunk["content_kind"],
                    "pdfPages": chunk["pages"],
                    "sourcePath": chunk["source_path"],
                    "sourceSha256": chunk["source_sha256"],
                    "jurisdiction": chunk["jurisdiction"],
                    "effectiveDate": chunk["effective_date"],
                    "documentKind": chunk["document_kind"],
                    "textBasis": chunk.get("text_basis", ""),
                    "retrievalHints": chunk.get("retrieval_hints", []),
                    "retrieval": "hybrid-conceptual-with-metadata-rerank" if settings.AI_DRAFTING_ENABLED else "hybrid-conceptual",
                },
            ))
        return results


# Retain the old public class name while replacing its placeholder implementation.
RagDatabaseConnector = ContentLibraryTreatiseConnector

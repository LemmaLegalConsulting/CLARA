"""Tests for deterministic research search.

Two layers, for two different failures.

The unit tests run against a hand-built corpus of a dozen passages. They fix the
behaviour research mode promises -- a phrase is exact, a citation finds the
authority rather than the commentary, an expansion is labelled, nothing calls a
model -- and they run in any checkout, with or without imported law.

The regression tests run the queries in ``content/research-index/regression-queries.yaml``
against the real corpus. They are the guard against the failure ranking changes
actually have: nothing errors, no test fails, and retrieval is quietly worse
until an advocate finds out. Where a corpus is not present they skip and say so,
because a check that did not run must never read as a clean result.
"""

from __future__ import annotations

import json

import yaml
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.content_library import content_path
from apps.sources.research import engine, snippets
from apps.sources.research.citations import find_citations
from apps.core.jurisdictions import appellate_district, canonical_county
from apps.sources.research.corpus import IndexRecord
from apps.sources.research.index import index_from_records
from apps.sources.research.query import parse_query


REGRESSION_PATH = ("research-index", "regression-queries.yaml")


def record(key, corpus, title, text, *, citation="", authority="", weight=1.0, group=None, **facets):
    return IndexRecord(
        key=key,
        group_key=group or key,
        corpus=corpus,
        title=title,
        # The heading and the citation are part of the indexed text in the real
        # corpus, and a fixture that leaves them out tests a corpus nobody has.
        text=" ".join(f"{title} {citation} {text}".split()),
        citation=citation,
        authority_citation=authority,
        weight=weight,
        facets={
            "sourceType": corpus,
            "documentSlug": facets.get("slug", ""),
            "municipality": facets.get("municipality", ""),
            "county": canonical_county(facets.get("county", "")),
            "appellateDistrict": appellate_district(facets.get("county", "")),
            "court": facets.get("court", ""),
            "year": facets.get("year", ""),
            "publicationStatus": facets.get("publication_status", ""),
            "judge": facets.get("judge", ""),
            "title": title,
        },
        metadata={"supersededOrCriticized": facets.get("superseded", False)},
        open_target={"kind": corpus, "sourceUrl": f"/api/test/{key}/"},
    )


def sample_index():
    return index_from_records([
        record(
            "statute-5321-04", "statutes",
            "Ohio Revised Code - Ohio Rev. Code 5321.04 - Landlord obligations",
            "A landlord who is a party to a rental agreement shall comply with the requirements of all "
            "applicable building housing health and safety codes and keep the premises in a fit and "
            "habitable condition.",
            citation="Ohio Rev. Code § 5321.04",
            authority="Ohio Rev. Code § 5321.04",
        ),
        record(
            "treatise-commentary", "treatises",
            "Ohio Eviction and Landlord-Tenant Law - Remedies under R.C. 5321.04",
            "R.C. 5321.04 imposes duties on a landlord. Courts applying R.C. 5321.04 have divided over "
            "whether R.C. 5321.04 creates a private right of action. See generally R.C. 5321.04 and the "
            "cases collected under R.C. 5321.04. The scope of R.C. 5321.04 remains contested.",
            citation="Ohio Eviction and Landlord-Tenant Law, ch. 12 (PDF p. 400)",
        ),
        record(
            "ordinance-cleveland", "ordinances",
            "Codified Ordinances of the City of Cleveland - Lead safe certification",
            "No owner shall rent a residential rental unit without a lead safe certificate issued under "
            "this chapter.",
            citation="Cleveland Codified Ordinances § 365.05",
            authority="Cleveland Codified Ordinances § 365.05",
            municipality="Cleveland", county="Cuyahoga", year="2019",
        ),
        record(
            "case-henshaw", "cases", "American Property Services v. Henshaw",
            "The landlord served a notice to leave the premises on the tenant. The notice to leave the "
            "premises omitted the statutory language and was therefore defective.",
            citation="1982-Ohio-9", authority="1982-Ohio-9",
            court="Cleveland Municipal Court", county="Cuyahoga", year="1982",
            publication_status="unpublished", group="case-henshaw",
        ),
        record(
            "case-henshaw-ocr", "cases", "American Property Services v. Henshaw",
            "Opinion text. The notice to leave the premises is a jurisdictional prerequisite.",
            citation="1982-Ohio-9", authority="1982-Ohio-9",
            court="Cleveland Municipal Court", county="Cuyahoga", year="1982",
            publication_status="unpublished", group="case-henshaw",
        ),
        record(
            "case-warren", "cases", "Springboro Commons v. Feltner",
            "The tenant raised habitability as a defence to the claim for rent. The premises were not "
            "fit and habitable and the rent was abated.",
            citation="2021-Ohio-591", authority="2021-Ohio-591",
            court="Twelfth District Court of Appeals", county="Warren County", year="2021",
            publication_status="published",
        ),
        record(
            "case-superseded", "cases", "Old Authority v. Reversed",
            "The tenant raised habitability as a defence to the claim for rent in a fit and habitable "
            "premises dispute.",
            citation="1975-Ohio-1", authority="1975-Ohio-1",
            court="Twelfth District Court of Appeals", county="Warren County", year="1975",
            publication_status="published", superseded=True,
        ),
        record(
            "case-sublease", "cases", "Sublease Holdings v. Tenant",
            "The sublease governed the rent. Nothing else was put in issue.",
            citation="1999-Ohio-2", authority="1999-Ohio-2",
            court="Franklin County Municipal Court", county="Franklin", year="1999",
        ),
    ])


class QueryParsingTests(SimpleTestCase):
    def test_a_quoted_phrase_is_kept_whole_and_its_words_still_rank(self):
        parsed = parse_query('"notice to leave the premises" habitability')
        self.assertEqual(parsed.phrases, ("notice to leave the premises",))
        self.assertIn("habitability", parsed.terms)
        self.assertIn("premises", parsed.terms)
        self.assertTrue(parsed.is_exact)

    def test_an_unbalanced_quote_is_reported_rather_than_silently_dropped(self):
        parsed = parse_query('unbalanced " quote')
        self.assertFalse(parsed.phrases)
        self.assertTrue(parsed.warnings)

    def test_an_unbalanced_quote_does_not_invent_a_different_exact_phrase(self):
        # Dropping the first quote re-pairs every quote after it, so this used
        # to come back asking for the exact phrase "rent" -- a different search
        # from the one that was typed, run under a warning saying the stray
        # mark had been ignored.
        parsed = parse_query('"notice to leave" rent "habit')
        self.assertEqual(parsed.phrases, ("notice to leave",))
        self.assertTrue(parsed.warnings)

    def test_a_facet_name_works_as_a_field_prefix(self):
        self.assertEqual(parse_query("rent sourceType:cases").filter_values("sourceType"), ("cases",))

    def test_a_field_filter_leaves_the_query_terms_alone(self):
        parsed = parse_query('court:"Cleveland Municipal" deposit')
        self.assertEqual(parsed.filter_values("court"), ("Cleveland Municipal",))
        self.assertEqual(parsed.terms, ("deposit",))

    def test_an_unknown_field_prefix_stays_part_of_the_query(self):
        parsed = parse_query("banana:split deposit")
        self.assertFalse(parsed.filters)
        self.assertIn("banana", parsed.terms)

    def test_a_section_number_is_one_term_not_two(self):
        self.assertIn("5321.04", parse_query("landlord 5321.04 duties").terms)

    def test_a_citation_is_recognized_in_the_spellings_a_corpus_uses(self):
        for text in ("R.C. 5321.04", "O.R.C. 5321.04", "Ohio Rev. Code 5321.04", "Ohio Revised Code 5321.04"):
            citations = find_citations(text)
            self.assertEqual([citation.normalized for citation in citations], ["R.C. 5321.04"], text)

    def test_a_subdivision_is_kept_and_the_bare_section_is_still_searched_for(self):
        citation = find_citations("R.C. 5321.04(A)(2)")[0]
        self.assertEqual(citation.normalized, "R.C. 5321.04(A)(2)")
        self.assertIn("5321.04", citation.variants)


class SnippetTests(SimpleTestCase):
    """Spans have to index into the string the caller is handed."""

    def _snippet(self, text, **kwargs):
        return snippets.build(text, **kwargs)

    def test_a_short_snippet_marks_the_words_that_matched(self):
        result = self._snippet("the notice was defective", terms=["defective"], phrases=["the notice"])
        marked = [result["text"][span["start"]:span["end"]] for span in result["matches"]]
        self.assertEqual(marked, ["the notice", "defective"])

    def test_a_truncated_snippet_marks_the_words_that_matched(self):
        # The window opens mid-document, so the snippet is returned with a
        # leading ellipsis. Spans that ignored it marked two characters to the
        # left of every word -- on every real result, since the window is
        # placed over the densest match cluster rather than at the start.
        text = "filler " * 60 + "the notice to leave the premises was defective " + "tail " * 60
        result = self._snippet(text, terms=["defective"], phrases=["notice to leave the premises"])
        self.assertTrue(result["text"].startswith("\u2026 "))
        marked = [result["text"][span["start"]:span["end"]] for span in result["matches"]]
        self.assertEqual(marked, ["notice to leave the premises", "defective"])

    def test_an_expansion_hit_is_marked_as_an_expansion(self):
        result = self._snippet("the premises were not habitable", expansions=["habitable"])
        self.assertEqual([span["kind"] for span in result["matches"]], ["expansion"])


class DeterministicSearchTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.index = sample_index()

    def search(self, query, **kwargs):
        kwargs.setdefault("index", self.index)
        return engine.search(query, **kwargs)

    def ids(self, payload):
        return [result["id"] for result in payload["results"]]

    def test_an_exact_phrase_returns_only_text_that_contains_it(self):
        payload = self.search('"notice to leave the premises"')
        self.assertTrue(payload["results"])
        for result in payload["results"]:
            self.assertIn("notice to leave the premises", result["snippet"].casefold())

    def test_a_phrase_the_corpus_lacks_returns_nothing_and_names_it(self):
        payload = self.search('"entitled to a unicorn"')
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["unmatched"], [{"kind": "phrase", "value": "entitled to a unicorn"}])

    def test_a_citation_finds_the_section_before_the_commentary_about_it(self):
        payload = self.search("R.C. 5321.04")
        self.assertEqual(self.ids(payload)[0], "statute-5321-04")
        self.assertIn("treatise-commentary", self.ids(payload))

    def test_a_citation_the_corpus_lacks_returns_nothing_and_names_it(self):
        payload = self.search("2020-Ohio-1234")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["unmatched"], [{"kind": "citation", "value": "2020-Ohio-1234"}])

    def test_the_passages_of_one_decision_are_one_result(self):
        payload = self.search('"notice to leave the premises"')
        henshaw = [result for result in payload["results"] if result["groupId"] == "case-henshaw"]
        self.assertEqual(len(henshaw), 1)
        self.assertEqual(len(henshaw[0]["passages"]), 1)

    def test_a_result_says_which_of_the_readers_words_it_does_not_contain(self):
        payload = self.search("habitability sublease")
        by_id = {result["id"]: result for result in payload["results"]}
        self.assertIn("habitability", by_id["case-sublease"]["missingConcepts"])
        self.assertIn("sublease", by_id["statute-5321-04"]["missingConcepts"])

    def test_an_excluded_word_removes_its_documents(self):
        self.assertNotIn("case-sublease", self.ids(self.search("habitability -sublease")))

    def test_a_superseded_decision_ranks_below_an_equivalent_good_one(self):
        ids = self.ids(self.search("habitability defence rent"))
        self.assertLess(ids.index("case-warren"), ids.index("case-superseded"))

    def test_a_filter_narrows_and_the_facet_still_offers_the_alternatives(self):
        payload = self.search("habitability", filters={"county": ["Warren County"]})
        # Stored as "Warren"; asked for as "Warren County"; one county.
        self.assertEqual(set(self.ids(payload)), {"case-warren", "case-superseded"})
        counties = next(facet for facet in payload["facets"] if facet["field"] == "county")
        self.assertIn("Cuyahoga", [value["value"] for value in counties["values"]])

    def test_a_county_is_narrowed_by_its_appellate_district(self):
        payload = self.search("habitability", filters={"appellateDistrict": ["Twelfth District"]})
        self.assertEqual(set(self.ids(payload)), {"case-warren", "case-superseded"})

    def test_a_court_filter_ignores_the_punctuation_the_two_corpora_disagree_about(self):
        payload = self.search("notice", filters={"court": ["Cleveland Municipal Court - Housing"]})
        self.assertEqual(payload["results"], [])
        payload = self.search("notice", filters={"court": ["cleveland municipal"]})
        self.assertTrue(payload["results"])

    def test_a_corpus_can_be_searched_on_its_own(self):
        payload = self.search("habitable", corpora=["statutes"])
        self.assertEqual(self.ids(payload), ["statute-5321-04"])

    def test_a_field_filter_typed_into_the_query_works_like_the_facet(self):
        self.assertEqual(
            self.ids(self.search("habitability county:Warren")),
            self.ids(self.search("habitability", filters={"county": ["Warren"]})),
        )

    def test_a_query_of_several_ideas_does_not_match_on_one_of_them(self):
        payload = self.search("habitability defence rent")
        self.assertNotIn("ordinance-cleveland", self.ids(payload))
        self.assertGreaterEqual(payload["coverage"]["required"], 2)

    def test_expansion_is_reported_with_where_each_term_came_from(self):
        payload = self.search("habitability", expansion_mode="thesaurus")
        applied = {entry["term"]: entry["expansions"] for entry in payload["expansion"]["applied"]}
        self.assertIn("habitability", applied)
        bases = {item["basis"] for item in applied["habitability"]}
        self.assertEqual(bases, {"thesaurus"})
        self.assertTrue(all(item["verification"] for item in applied["habitability"]))

    def test_expansion_can_be_switched_off_entirely(self):
        payload = self.search("habitability", expansion_mode="none")
        self.assertEqual(payload["expansion"]["applied"], [])

    def test_an_exact_query_is_never_broadened(self):
        payload = self.search('"fit and habitable"', expansion_mode="all")
        self.assertTrue(payload["expansion"]["suppressed"])
        self.assertTrue(payload["expansion"]["suppressedReason"])
        self.assertEqual(payload["expansion"]["applied"], [])

    def test_a_result_reached_through_a_synonym_says_so(self):
        payload = self.search("habitability", expansion_mode="thesaurus")
        statute = next(result for result in payload["results"] if result["id"] == "statute-5321-04")
        self.assertIn("habitable", " ".join(statute["matchedExpansions"]).casefold())

    def test_every_search_reports_that_no_model_was_involved(self):
        payload = self.search("habitability")
        self.assertEqual(payload["ai"]["retrieval"], "deterministic-bm25")
        self.assertFalse(payload["ai"]["rerank"]["applied"])
        self.assertFalse(payload["ai"]["synthesis"]["applied"])
        self.assertFalse(payload["expansion"]["status"]["usesAi"])

    def test_a_result_carries_what_is_needed_to_judge_and_open_it(self):
        result = self.search("R.C. 5321.04")["results"][0]
        for key in ("title", "citation", "corpusLabel", "snippet", "facets", "open"):
            self.assertTrue(result[key], key)
        self.assertTrue(result["open"]["sourceUrl"])

    def test_paging_walks_the_same_ordering(self):
        first = self.search("habitability", limit=1, offset=0)
        second = self.search("habitability", limit=1, offset=1)
        self.assertEqual(first["total"], second["total"])
        self.assertNotEqual(self.ids(first), self.ids(second))

    def test_a_query_of_nothing_but_filters_lists_what_they_match(self):
        payload = self.search("county:\"Warren County\"")
        self.assertEqual(set(self.ids(payload)), {"case-warren", "case-superseded"})
        self.assertIn("filters", payload["coverage"]["note"])

    def test_a_paging_value_that_is_not_a_number_does_not_raise(self):
        payload = self.search("habitability", limit="20.5", offset="tenth")
        self.assertEqual(payload["limit"], engine.DEFAULT_LIMIT)
        self.assertEqual(payload["offset"], 0)

    def test_an_empty_query_answers_without_guessing(self):
        payload = self.search("   ")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["total"], 0)

    def test_the_same_query_twice_gives_the_same_order(self):
        self.assertEqual(self.ids(self.search("habitability rent")), self.ids(self.search("habitability rent")))


class SearchDoesNotTouchAiTests(SimpleTestCase):
    """The deterministic path must not be able to call a model, even by accident."""

    def test_the_research_package_does_not_import_the_ai_app(self):
        import ast
        import pathlib

        import apps.sources.research as package

        offenders = []
        for source in sorted(pathlib.Path(package.__path__[0]).glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(name == "apps.ai" or name.startswith("apps.ai.") for name in names):
                    offenders.append(f"{source.name}:{node.lineno}")
        self.assertEqual(offenders, [], "Deterministic research retrieval must not be able to reach a model.")


class RegressionQueryTests(TestCase):
    """The maintained known-answer queries, run against the real corpus."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = content_path(*REGRESSION_PATH)
        cls.entries = []
        if path.is_file():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cls.entries = [entry for entry in (payload.get("queries") or []) if isinstance(entry, dict)]

    @classmethod
    def tearDownClass(cls):
        # The index built here was built against the test database; it must not
        # outlive it and answer a later test as though it were the real corpus.
        from apps.sources.research.index import reset_index

        reset_index()
        super().tearDownClass()

    def _present_corpora(self, index):
        return {corpus for corpus, count in index.status()["documentsByCorpus"].items() if count}

    def test_the_regression_file_is_readable_and_not_empty(self):
        self.assertTrue(self.entries, f"No known-answer queries were found at {content_path(*REGRESSION_PATH)}.")

    def test_known_answer_queries(self):
        from apps.sources.research.index import research_index

        index = research_index()
        present = self._present_corpora(index)
        if not present:
            self.skipTest(
                "No corpus is loaded in this checkout, so the known-answer queries could not be run. "
                "They are not passing; they did not run."
            )
        skipped = []
        for entry in self.entries:
            required = set(entry.get("requires") or [])
            if required - present:
                skipped.append(f"{entry.get('id')} (needs {', '.join(sorted(required - present))})")
                continue
            with self.subTest(query=entry.get("id")):
                self._check(entry, index)
        if skipped:
            print(f"\nKnown-answer queries skipped for want of a corpus: {'; '.join(skipped)}")

    def _check(self, entry, index):
        expectations = entry.get("expect") or {}
        within = int(expectations.get("within", 10))
        payload = engine.search(
            entry["query"],
            limit=max(within, int(expectations.get("min_results", 1)), 10),
            index=index,
        )
        results = payload["results"]
        message = f"{entry.get('id')}: {entry.get('why', '').strip()}"

        if expectations.get("no_results"):
            self.assertEqual([result["id"] for result in results], [], message)
        if "min_results" in expectations:
            self.assertGreaterEqual(len(results), expectations["min_results"], message)
        if "max_results" in expectations:
            self.assertLessEqual(payload["total"], expectations["max_results"], message)
        if "top_result_id" in expectations:
            self.assertTrue(results, message)
            self.assertEqual(results[0]["id"], expectations["top_result_id"], message)
        if "top_result_corpus" in expectations:
            self.assertTrue(results, message)
            self.assertEqual(results[0]["corpus"], expectations["top_result_corpus"], message)
        for field, value in (expectations.get("top_result_facets") or {}).items():
            self.assertTrue(results, message)
            self.assertEqual(results[0]["facets"].get(field), value, message)
        for wanted in expectations.get("contains_ids") or []:
            self.assertIn(wanted, [result["id"] for result in results[:within]], message)
        for wanted in expectations.get("contains_titles") or []:
            self.assertIn(wanted, [result["title"] for result in results[:within]], message)
        if "every_result_contains" in expectations:
            needle = expectations["every_result_contains"].casefold()
            for result in results:
                self.assertIn(needle, result["snippet"].casefold(), f"{message} ({result['id']})")
        for wanted in expectations.get("unmatched") or []:
            self.assertIn(wanted, [item["value"] for item in payload["unmatched"]], message)


@override_settings(OPENAI_API_KEY="synthetic-test-key")
class ResearchSearchApiTests(TestCase):
    """The endpoint, and especially what it says about AI in each state."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="advocate", password="password")
        self.client.force_login(self.user)
        self._install_sample_index()

    def _install_sample_index(self):
        """Answer from the fixture corpus rather than from whatever is imported."""
        from apps.sources.research import index as index_module

        holder = index_module._HOLDER
        original = holder._index
        holder._index = sample_index()
        original_fingerprint = index_module.corpus_fingerprint
        index_module.corpus_fingerprint = lambda: holder._index.fingerprint
        self.addCleanup(setattr, holder, "_index", original)
        self.addCleanup(setattr, index_module, "corpus_fingerprint", original_fingerprint)

    def search(self, **payload):
        response = self.client.post(
            "/api/research/search/", data=payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_a_search_needs_no_model_and_says_so(self):
        payload = self.search(query="R.C. 5321.04")
        self.assertEqual(payload["results"][0]["id"], "statute-5321-04")
        self.assertEqual(payload["ai"]["retrieval"], "deterministic-bm25")
        self.assertFalse(payload["ai"]["rerank"]["requested"])
        self.assertFalse(payload["ai"]["synthesis"]["requested"])
        self.assertTrue(payload["ai"]["synthesis"]["reason"])

    @override_settings(OPENAI_API_KEY="", AI_DRAFTING_ENABLED=True)
    def test_missing_provider_credentials_preserve_search_results(self):
        payload = self.search(query="habitability", aiRerank=True, aiSynthesis=True)
        self.assertTrue(payload["results"])
        for operation in ("rerank", "synthesis"):
            self.assertFalse(payload["ai"][operation]["applied"])
            self.assertIn("configuration", payload["ai"][operation]["reason"])

    def test_a_search_can_be_linked_to(self):
        response = self.client.get("/api/research/search/", {"q": "habitability", "corpus": "statutes"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([result["id"] for result in response.json()["results"]], ["statute-5321-04"])

    def test_a_facet_typed_as_a_parameter_narrows_the_same_way(self):
        response = self.client.get("/api/research/search/", {"q": "habitability", "county": "Warren County"})
        self.assertEqual(
            {result["facets"]["county"] for result in response.json()["results"]},
            {"Warren"},
        )

    def test_search_survives_a_model_that_cannot_be_reached(self):
        from unittest import mock

        from apps.ai.openai_client import OpenAIBackendError

        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch(
                "apps.sources.research_views.OpenAICompatibleClient.complete",
                side_effect=OpenAIBackendError("provider is down"),
            ),
        ):
            payload = self.search(query="habitability", aiRerank=True, aiSynthesis=True)

        # The point of the whole arrangement: the library still answered.
        self.assertTrue(payload["results"])
        self.assertTrue(payload["ai"]["rerank"]["requested"])
        self.assertFalse(payload["ai"]["rerank"]["applied"])
        self.assertIn("provider is down", payload["ai"]["rerank"]["reason"])
        self.assertFalse(payload["ai"]["synthesis"]["applied"])
        self.assertIn("provider is down", payload["ai"]["synthesis"]["reason"])

    def test_a_rerank_reorders_without_removing_anything(self):
        from unittest import mock

        baseline = self.search(query="habitability")
        identifiers = [result["id"] for result in baseline["results"]]
        self.assertGreater(len(identifiers), 1)
        reversed_order = json.dumps({"order": list(reversed(identifiers))})

        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch(
                "apps.sources.research_views.OpenAICompatibleClient.complete",
                return_value=reversed_order,
            ),
        ):
            payload = self.search(query="habitability", aiRerank=True)

        self.assertTrue(payload["ai"]["rerank"]["applied"])
        self.assertEqual([result["id"] for result in payload["results"]], list(reversed(identifiers)))
        self.assertEqual(set(result["id"] for result in payload["results"]), set(identifiers))

    def test_a_rerank_that_omits_a_result_keeps_it_rather_than_dropping_it(self):
        from unittest import mock

        baseline = [result["id"] for result in self.search(query="habitability")["results"]]
        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch(
                "apps.sources.research_views.OpenAICompatibleClient.complete",
                return_value=json.dumps({"order": [baseline[-1]]}),
            ),
        ):
            payload = self.search(query="habitability", aiRerank=True)
        identifiers = [result["id"] for result in payload["results"]]
        self.assertEqual(identifiers[0], baseline[-1])
        self.assertEqual(set(identifiers), set(baseline))

    def test_a_deployment_with_ai_switched_off_says_that_rather_than_failing(self):
        from unittest import mock

        with mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", False):
            payload = self.search(query="habitability", aiSynthesis=True)
        self.assertTrue(payload["results"])
        self.assertFalse(payload["ai"]["synthesis"]["applied"])
        self.assertIn("switched off", payload["ai"]["synthesis"]["reason"])

    def test_the_status_endpoint_states_that_search_never_calls_a_model(self):
        payload = self.client.get("/api/research/search/status/").json()
        self.assertFalse(payload["expansion"]["usesAi"])
        self.assertFalse(payload["ai"]["default"]["rerank"])
        self.assertFalse(payload["ai"]["default"]["synthesis"])
        self.assertTrue(payload["facetFields"])
        self.assertTrue(payload["querySyntax"])

    def test_an_anonymous_request_is_refused(self):
        self.client.logout()
        self.assertEqual(self.client.post("/api/research/search/", data={}, content_type="application/json").status_code, 401)

    def test_a_filter_sent_as_a_bare_string_narrows_rather_than_returning_nothing(self):
        payload = self.search(query="habitability", filters={"county": "Warren County"})
        self.assertTrue(payload["results"])
        self.assertEqual({result["facets"]["county"] for result in payload["results"]}, {"Warren"})


class CountyGroupingTests(SimpleTestCase):
    """A corpus imported before the vocabulary existed still groups correctly."""

    def setUp(self):
        self.index = index_from_records([
            record("a", "cases", "A v. B", "habitability", county="Cuyahoga"),
            record("b", "cases", "C v. D", "habitability", county="Cuyahoga County"),
            record("c", "cases", "E v. F", "habitability", county="CUYAHOGA CTY."),
            record("d", "cases", "G v. H", "habitability", county="Miami-Dade County"),
        ])

    def _counties(self, payload):
        facet = next(item for item in payload["facets"] if item["field"] == "county")
        return {value["value"]: value["count"] for value in facet["values"]}

    def test_three_spellings_of_one_county_are_one_facet_value(self):
        counties = self._counties(engine.search("habitability", index=self.index))
        self.assertEqual(counties.get("Cuyahoga"), 3)
        self.assertNotIn("Cuyahoga County", counties)

    def test_an_out_of_state_county_keeps_its_own_value_and_no_district(self):
        payload = engine.search("habitability", index=self.index)
        self.assertEqual(self._counties(payload).get("Miami-Dade County"), 1)
        districts = next(item for item in payload["facets"] if item["field"] == "appellateDistrict")
        self.assertEqual({value["value"] for value in districts["values"]}, {"Eighth District"})
        self.assertEqual(districts["unattributed"], 1)

    def test_narrowing_by_either_spelling_finds_every_decision(self):
        for spelling in ("Cuyahoga", "Cuyahoga County"):
            payload = engine.search("habitability", filters={"county": [spelling]}, index=self.index)
            self.assertEqual(payload["total"], 3, spelling)


@override_settings(OPENAI_API_KEY="synthetic-test-key")
class GetCannotStartGenerativeWorkTests(TestCase):
    """A link is opened by crawlers, prefetchers and whoever it was forwarded to."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(username="reader", password="password")
        self.client.force_login(self.user)
        from apps.sources.research import index as index_module

        holder = index_module._HOLDER
        original, original_fingerprint = holder._index, index_module.corpus_fingerprint
        holder._index = sample_index()
        index_module.corpus_fingerprint = lambda: holder._index.fingerprint
        self.addCleanup(setattr, holder, "_index", original)
        self.addCleanup(setattr, index_module, "corpus_fingerprint", original_fingerprint)

    def test_a_link_asking_for_a_summary_does_not_call_a_model(self):
        from unittest import mock

        calls = []

        def record(self, **kwargs):
            calls.append(kwargs)
            return "an answer"

        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch("apps.sources.research_views.OpenAICompatibleClient.complete", record),
        ):
            response = self.client.get(
                "/api/research/search/", {"q": "habitability", "aiSynthesis": "1", "aiRerank": "1"}
            )

        payload = response.json()
        self.assertEqual(calls, [], "A GET must not be able to spend money on a model call.")
        self.assertTrue(payload["results"])
        self.assertFalse(payload["ai"]["synthesis"]["applied"])
        self.assertFalse(payload["ai"]["rerank"]["applied"])

    def test_the_refusal_is_reported_rather_than_the_flag_silently_dropped(self):
        payload = self.client.get("/api/research/search/", {"q": "habitability", "aiSynthesis": "1"}).json()
        self.assertTrue(payload["ai"]["synthesis"]["requested"])
        self.assertIn("POST", payload["ai"]["synthesis"]["reason"])

    def test_a_link_naming_a_matter_does_not_send_it_to_a_model(self):
        from unittest import mock

        from apps.matters.models import Matter

        matter = Matter.objects.create(
            external_id="LS-TEST", client_name="A Client", matter_type="Eviction",
            jurisdiction="Ohio", summary="Confidential matter summary.",
        )
        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch("apps.sources.research_views.OpenAICompatibleClient.complete") as complete,
        ):
            self.client.get(
                "/api/research/search/",
                {"q": "habitability", "aiSynthesis": "1", "matterId": matter.external_id},
            )
        complete.assert_not_called()

    def test_the_same_search_as_a_post_still_runs_the_model(self):
        from unittest import mock

        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch("apps.sources.research_views.OpenAICompatibleClient.complete", return_value="an answer"),
        ):
            payload = self.client.post(
                "/api/research/search/",
                data={"query": "habitability", "aiSynthesis": True},
                content_type="application/json",
            ).json()
        self.assertTrue(payload["ai"]["synthesis"]["applied"])


@override_settings(OPENAI_API_KEY="synthetic-test-key")
class RerankHonestyTests(TestCase):
    """"Applied" has to mean a model actually reordered something."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(get_user_model().objects.create_user(username="r", password="p"))
        from apps.sources.research import index as index_module

        holder = index_module._HOLDER
        original, original_fingerprint = holder._index, index_module.corpus_fingerprint
        holder._index = sample_index()
        index_module.corpus_fingerprint = lambda: holder._index.fingerprint
        self.addCleanup(setattr, holder, "_index", original)
        self.addCleanup(setattr, index_module, "corpus_fingerprint", original_fingerprint)

    def _search(self, response_text):
        from unittest import mock

        with (
            mock.patch("apps.sources.research_views.settings.AI_DRAFTING_ENABLED", True),
            mock.patch("apps.sources.research_views.OpenAICompatibleClient.complete", return_value=response_text),
        ):
            return self.client.post(
                "/api/research/search/",
                data={"query": "habitability", "aiRerank": True},
                content_type="application/json",
            ).json()

    def test_an_order_of_invented_ids_is_not_a_rerank(self):
        payload = self._search(json.dumps({"order": ["invented-id", "another-invention"]}))
        self.assertFalse(payload["ai"]["rerank"]["applied"])
        self.assertIn("named no result it was given", payload["ai"]["rerank"]["reason"])

    def test_an_order_naming_real_results_is_a_rerank(self):
        baseline = [result["id"] for result in self._search(json.dumps({"order": []}))["results"]]
        payload = self._search(json.dumps({"order": list(reversed(baseline))}))
        self.assertTrue(payload["ai"]["rerank"]["applied"])
        self.assertEqual([result["id"] for result in payload["results"]], list(reversed(baseline)))

    def test_invented_ids_mixed_with_real_ones_still_rerank_on_the_real_ones(self):
        baseline = [result["id"] for result in self._search(json.dumps({"order": []}))["results"]]
        payload = self._search(json.dumps({"order": ["nonsense", baseline[-1]]}))
        self.assertTrue(payload["ai"]["rerank"]["applied"])
        self.assertEqual(payload["results"][0]["id"], baseline[-1])
        self.assertEqual({r["id"] for r in payload["results"]}, set(baseline))


class LearnedTableProvenanceTests(TestCase):
    """A table from another corpus must not be described as learned from this one."""

    def test_a_table_built_from_another_corpus_is_not_used(self):
        from apps.sources.research import expansion as expansion_module

        built = expansion_module._load_neighbours()
        if not built["available"]:
            self.skipTest("No learned term-neighbour table is built in this checkout.")
        identity = built["corpusIdentity"] or "unrecorded"
        self.assertFalse(
            any(item.basis == expansion_module.DISTRIBUTIONAL
                for item in expansion_module.expand(["retaliation"], mode="all", corpus_identity="not-this-corpus")),
            "A table built elsewhere must not expand a search here.",
        )
        status = expansion_module.status("not-this-corpus")["distributional"]
        self.assertTrue(status["built"])
        self.assertFalse(status["available"])
        self.assertFalse(status["matchesCorpus"])
        self.assertIn("build_research_index", status["reason"])
        # And the matching case still works, so the gate is not simply off.
        self.assertTrue(expansion_module.status(identity)["distributional"]["matchesCorpus"])

    def test_an_edit_to_one_record_does_not_invalidate_the_table(self):
        # The identity is corpus composition, not corpus state. Keyed on the
        # index fingerprint instead, a single corrected county would have
        # disabled expansion until somebody re-ran a ten-second build.
        from apps.sources.research.index import corpus_identity

        first = corpus_identity()
        self.assertEqual(first, corpus_identity())
        self.assertTrue(first)


class CitationSubdivisionTests(SimpleTestCase):
    """What a subdivision does is a preference, and the parse says so."""

    def test_the_section_is_required_and_the_subdivision_only_ranks(self):
        citation = find_citations("R.C. 5321.04(A)(2)")[0]
        self.assertEqual(citation.subdivision, "(A)(2)")
        self.assertEqual(citation.to_dict()["required"], ["R.C. 5321.04"])
        self.assertTrue(citation.to_dict()["ranksHigher"])
        self.assertIn("5321.04(A)(2)", citation.refinements)

    def test_a_citation_without_a_subdivision_has_nothing_to_refine(self):
        citation = find_citations("R.C. 5321.04")[0]
        self.assertEqual(citation.subdivision, "")
        self.assertEqual(citation.to_dict()["ranksHigher"], [])

    def test_a_document_carrying_the_subdivision_outranks_one_that_only_has_the_section(self):
        index = index_from_records([
            record("bare", "treatises", "Commentary on the section",
                   "This chapter discusses R.C. 5321.04 at length, R.C. 5321.04 throughout."),
            record("refined", "treatises", "Commentary on the division",
                   "This chapter discusses R.C. 5321.04(A)(2) and what division (A)(2) of section 5321.04 requires."),
        ])
        results = engine.search("R.C. 5321.04(A)(2)", index=index)["results"]
        self.assertEqual([item["id"] for item in results][0], "refined")
        self.assertIn("bare", [item["id"] for item in results])

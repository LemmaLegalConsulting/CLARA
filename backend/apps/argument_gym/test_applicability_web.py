"""Applicability against the library's own scope text, and the web lookup kept apart from it.

The briefs are the seeded flaws in content/argument-gym/seeded-flaws.yaml. The
model is scripted: what is under test is what the pipeline does with an answer
-- grounding it, refusing it, labelling it -- not what a model would say.
"""

import json
from types import SimpleNamespace
from unittest import mock

import yaml
from django.contrib.auth.models import User
from django.test import TestCase
from django.test.utils import override_settings

from apps.ai import web_search
from apps.argument_gym import applicability, ingestion, web_lookup
from apps.argument_gym.models import GymChallenge, GymDocument, GymRun, GymWorkspace
from apps.argument_gym.pipeline import execute_run
from apps.argument_gym.tests import StubRegistry
from apps.core.content_library import content_path


def seeded(variant_id):
    suite = yaml.safe_load(content_path("argument-gym", "seeded-flaws.yaml").read_text(encoding="utf-8"))
    flaw = suite["flaws"][0]
    return next(variant for variant in flaw["variants"] if variant["id"] == variant_id)["brief"]


class ScriptedLLM:
    """Answers each stage from a script keyed by a phrase in its system prompt."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def complete(self, *, system, user, **_kwargs):
        for marker, reply in self.script.items():
            if marker in system:
                self.calls.append((marker, user))
                return json.dumps(reply(user) if callable(reply) else reply)
        return "{}"


class StubSearch:
    """A web-search client that records the questions it was sent."""

    def __init__(self, citations):
        self.citations = citations
        self.questions = []

    def search(self, question, *, instructions):
        self.questions.append(question)
        return web_search.WebSearchResult(
            question=question,
            answer="Covered property means property in a covered housing program or with a federally backed mortgage.",
            citations=[
                {
                    "url": url,
                    "title": url,
                    "domain": web_search._clean_domain(url.split("/")[2]),
                    **{
                        "trustTier": tier.id,
                        "trustRank": tier.rank,
                        "trustLabel": tier.label,
                    },
                }
                for url in self.citations
                for tier in [web_search.tier_for_url(url)]
            ],
            searches=[{"type": "search", "query": question, "sources": []}],
        )


def units_for(text):
    return ingestion.structure_units(ingestion._plain_paragraphs(text))


class TrustTierTests(TestCase):
    def test_prefer_mode_tiers_government_legal_aid_and_everything_else(self):
        self.assertEqual(web_search.search_policy().mode, "prefer")
        self.assertEqual(web_search.tier_for_url("https://www.ecfr.gov/current/title-24").id, "official_government")
        # Any .gov host, not only the listed ones: another state's court is still a government site.
        self.assertEqual(web_search.tier_for_url("https://www.courts.state.example.gov/x").id, "official_government")
        self.assertEqual(web_search.tier_for_url("https://codes.ohio.gov/section-5321.04").id, "official_government")
        self.assertEqual(web_search.tier_for_url("https://www.ohiolegalhelp.org/topic/eviction").id, "legal_aid")
        self.assertEqual(web_search.tier_for_url("https://some-law-firm.example.com/blog").id, "other_web")
        # Blocked sites are never a source, whatever the mode.
        self.assertEqual(web_search.tier_for_url("https://www.reddit.com/r/legaladvice").id, web_search.UNLISTED)

    def test_no_web_tier_may_claim_the_rank_reserved_for_the_library(self):
        for tier in web_search.trust_tiers():
            self.assertGreater(tier.rank, web_search.LIBRARY_RANK)

    @override_settings(AI_WEB_SEARCH_ENABLED=True)
    def test_prefer_mode_names_preferred_sources_and_does_not_restrict_the_search(self):
        transport = SimpleNamespace(responses=SimpleNamespace(create=mock.Mock(return_value={"output": []})))
        client = web_search.WebSearchClient(client=transport, model="test-model")
        client.search("Does the CARES Act notice apply to housing with no federal assistance?", instructions="Answer.")
        request = transport.responses.create.call_args.kwargs
        filters = request["tools"][0]["filters"]
        self.assertNotIn("allowed_domains", filters)
        self.assertIn("reddit.com", filters["blocked_domains"])
        self.assertIn("ohiolegalhelp.org", request["instructions"])

    @override_settings(AI_WEB_SEARCH_ENABLED=True)
    def test_a_long_question_is_refused_so_a_pasted_passage_cannot_leave(self):
        transport = SimpleNamespace(responses=SimpleNamespace(create=mock.Mock()))
        client = web_search.WebSearchClient(client=transport, model="test-model")
        with self.assertRaises(ValueError):
            client.search("x " * 400, instructions="Answer.")
        transport.responses.create.assert_not_called()

    def test_a_responses_api_reply_yields_tiered_citations_and_the_searches_run(self):
        response = {
            "output": [
                {"type": "web_search_call", "action": {"type": "search", "query": "cares act covered property"}},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Covered property is defined in 15 U.S.C. 9058(a)(2).",
                            "annotations": [
                                {"type": "url_citation", "url": "https://www.govinfo.gov/x", "title": "15 USC 9058"},
                                {"type": "url_citation", "url": "https://www.reddit.com/y", "title": "thread"},
                            ],
                        }
                    ],
                },
            ]
        }
        result = web_search.parse_response(response, question="q")
        self.assertEqual(result.searches[0]["query"], "cares act covered property")
        self.assertEqual([c["trustTier"] for c in result.citations], ["official_government", web_search.UNLISTED])
        self.assertEqual(len(result.usable_citations), 1)

    @override_settings(AI_WEB_SEARCH_ENABLED=False)
    def test_web_search_is_off_unless_an_installation_turns_it_on(self):
        self.assertFalse(web_search.web_search_enabled())
        with self.assertRaises(web_search.WebSearchUnavailable):
            web_search.WebSearchClient()


class PrivacyGuardTests(TestCase):
    def setUp(self):
        self.brief = seeded("explicit")
        self.blocked = web_lookup.identifying_phrases(self.brief, {"client": "Jordan Avery", "id": "LS-1"})

    def check(self, question):
        return web_lookup.privacy_check(question, blocked_phrases=self.blocked, brief_text=self.brief)

    def test_a_general_legal_question_with_relevant_facts_may_be_sent(self):
        self.assertEqual(
            self.check(
                "Does the CARES Act 30-day notice to vacate (15 U.S.C. 9058(c)) apply to a market-rate unit owned "
                "by a nonprofit affiliate of a public housing authority, with a private mortgage and no federal assistance?"
            ),
            "",
        )

    def test_repeating_the_brief_s_citations_is_not_quoting_the_brief(self):
        brief = seeded("control-pbv")
        blocked = web_lookup.identifying_phrases(brief, {})
        question = (
            "Under 15 U.S.C. 9058(a)(2)(A)(i), 42 U.S.C. 1437f(o)(13), and 34 U.S.C. 12491(a)(3)(H), "
            "is a unit with a project-based voucher contract a covered dwelling?"
        )
        self.assertEqual(web_lookup.privacy_check(question, blocked_phrases=blocked, brief_text=brief), "")

    def test_names_addresses_dates_amounts_and_quotations_are_withheld(self):
        for question in [
            "Does the CARES Act protect Jordan Avery?",
            "Is a building owned by Lakeview Community Housing Corp. a covered property?",
            "Is 1200 Larch Street a covered property?",
            "Was a notice served March 2, 2026 valid under the CARES Act?",
            "Can a landlord evict for $1,200 under the CARES Act?",
            "Does Unit 4B count as a covered dwelling?",
            "Is it true that the building receives no federal subsidy, does not participate in any HUD program?",
        ]:
            with self.subTest(question=question):
                self.assertTrue(self.check(question))


@override_settings(AI_DRAFTING_ENABLED=True)
class ApplicabilityTests(TestCase):
    def assess(self, variant, reply):
        brief = seeded(variant)
        units = units_for(brief)
        argument_map = [
            {
                "unitId": next(unit["id"] for unit in units if "CARES Act" in unit["text"]),
                "proposition": "The CARES Act 30-day notice applies and was not given.",
                "citedAuthority": ["15 U.S.C. 9058(c)"],
                "assertedFacts": [],
            }
        ]
        llm = ScriptedLLM({"reaches the facts the brief states": reply})
        return applicability.run_applicability(units, argument_map, brief, jurisdiction="Ohio", llm_client=llm), units, llm

    def test_the_cares_act_resolves_by_name_and_its_scope_includes_the_definition_it_cross_references(self):
        library = applicability.LibraryAuthorities()
        self.assertEqual(library.by_name("CARES Act"), "15 U.S.C. 9058")
        excerpts = applicability.scope_text("15 U.S.C. 9058", library)
        citations = {excerpt["citation"] for excerpt in excerpts}
        self.assertIn("15 U.S.C. § 9058", citations)
        # "covered housing program (as defined in section 12491(a) of title 34)"
        self.assertIn("34 U.S.C. § 12491", citations)

    def test_a_grounded_reading_that_the_rule_does_not_reach_the_facts_becomes_an_attack(self):
        def reply(user):
            source_id = "ap1"
            return {
                "assessments": [
                    {
                        "authority": "15 U.S.C. 9058",
                        "verdict": "does_not_apply",
                        "issue": "coverage",
                        "condition": "The dwelling must be on a covered property.",
                        "briefFacts": "A market-rate unit with no federal subsidy and a private mortgage.",
                        "quote": 'The term "covered dwelling" means a dwelling that',
                        "sourceId": source_id,
                        "explanation": "None of the covered-property bases is present.",
                    }
                ]
            }

        (report, assessments, excerpts), units, _llm = self.assess("explicit", reply)
        self.assertEqual(assessments[0]["verdict"], "does_not_apply")
        self.assertTrue(assessments[0]["grounded"])
        from apps.argument_gym.pipeline import anchor_unit

        attacks = applicability.applicability_attacks(assessments, units, 1, anchor=anchor_unit)
        self.assertEqual(len(attacks), 1)
        self.assertIn("does not reach these facts", attacks[0]["argument"])
        self.assertEqual(attacks[0]["legalSourceIds"], ["ap1"])

    def test_a_reading_whose_quote_is_not_in_the_scope_text_is_reported_as_unsettled(self):
        reply = {
            "assessments": [
                {
                    "authority": "15 U.S.C. 9058",
                    "verdict": "does_not_apply",
                    "quote": "This section applies only to buildings built after 1990.",
                    "sourceId": "ap1",
                }
            ]
        }
        (report, assessments, _excerpts), units, _llm = self.assess("explicit", reply)
        self.assertEqual(assessments[0]["verdict"], "unclear_on_facts")
        self.assertEqual(assessments[0]["ungroundedVerdict"], "does_not_apply")
        from apps.argument_gym.pipeline import anchor_unit

        self.assertEqual(applicability.applicability_attacks(assessments, units, 1, anchor=anchor_unit), [])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_without_a_model_nothing_is_said_to_apply(self):
        (report, assessments, _excerpts), _units, _llm = self.assess("explicit", {})
        self.assertEqual({item["verdict"] for item in assessments}, {"not_assessed"})
        self.assertIn("not assessed", report["summary"])


@override_settings(AI_DRAFTING_ENABLED=True, AI_WEB_SEARCH_ENABLED=True)
class WebLookupTests(TestCase):
    def run_lookup(self, *, questions, citations, findings, applicability_report=None):
        brief = seeded("implicit")
        units = units_for(brief)
        llm = ScriptedLLM(
            {
                "You plan web research": {"questions": questions},
                "You review web research": {"findings": findings},
            }
        )
        search = StubSearch(citations)
        report, graded = web_lookup.run_web_lookup(
            units,
            [],
            brief,
            {"challenges": [], "ruleAudit": []},
            jurisdiction="Ohio",
            matter_context={"client": "Jordan Avery"},
            applicability_report=applicability_report or {"authorities": [], "assessments": []},
            llm_client=llm,
            search_client=search,
        )
        return report, graded, search, units

    def test_only_questions_that_pass_the_guard_are_sent_and_the_withheld_ones_are_reported(self):
        report, _graded, search, _units = self.run_lookup(
            questions=[
                {"question": "Does the CARES Act notice apply to workforce housing owned by a housing authority affiliate with a conventional loan?", "authority": "CARES Act"},
                {"question": "Is Jordan Avery's home covered by the CARES Act?", "authority": "CARES Act"},
            ],
            citations=["https://www.govinfo.gov/a"],
            findings=[],
        )
        self.assertEqual(len(search.questions), 1)
        self.assertNotIn("Jordan", search.questions[0])
        self.assertEqual(len(report["withheld"]), 1)
        self.assertNotIn("Jordan", json.dumps(report["withheld"]))

    def test_findings_are_capped_and_a_finding_resting_only_on_legal_aid_is_a_lead(self):
        question = {"question": "Does the CARES Act notice apply to housing with no federal assistance?", "authority": "CARES Act"}
        _report, graded, _search, _units = self.run_lookup(
            questions=[question],
            citations=["https://www.govinfo.gov/a", "https://www.ohiolegalhelp.org/b", "https://www.reddit.com/c"],
            findings=[
                {"kind": "challenge", "finding": "Not a covered property", "argument": "a", "severity": "high",
                 "confidence": "high", "sourceUrls": ["https://www.govinfo.gov/a"]},
                {"kind": "challenge", "finding": "Legal aid says so", "argument": "b", "severity": "high",
                 "confidence": "high", "sourceUrls": ["https://www.ohiolegalhelp.org/b"]},
                {"kind": "challenge", "finding": "Forum says so", "argument": "c", "severity": "high",
                 "confidence": "high", "sourceUrls": ["https://www.reddit.com/c"]},
            ],
        )
        self.assertEqual([item["finding"] for item in graded], ["Not a covered property", "Legal aid says so"])
        official, legal_aid = graded
        self.assertEqual((official["severity"], official["confidence"], official["leadOnly"]), ("medium", "medium", False))
        self.assertEqual((legal_aid["severity"], legal_aid["confidence"], legal_aid["leadOnly"]), ("low", "low", True))

    def test_an_authority_missing_from_the_library_is_asked_about_even_without_a_planner(self):
        with override_settings(AI_DRAFTING_ENABLED=False):
            report, graded, search, _units = self.run_lookup(
                questions=[],
                citations=["https://www.govinfo.gov/a"],
                findings=[],
                applicability_report={
                    "authorities": [{"authority": "42 U.S.C. 1437z-9", "kind": "federal_code", "held": False}],
                    "assessments": [],
                },
            )
        self.assertEqual(len(search.questions), 1)
        self.assertIn("42 U.S.C. 1437z-9", search.questions[0])
        self.assertEqual(graded, [])
        self.assertIn("could not review", report["summary"])

    @override_settings(AI_WEB_SEARCH_ENABLED=False)
    def test_a_lookup_that_cannot_run_says_so_rather_than_reporting_nothing_found(self):
        report, graded = web_lookup.run_web_lookup([], [], "", {})
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(graded, [])


@override_settings(AI_DRAFTING_ENABLED=False, ARGUMENT_GYM_BACKGROUND_RUNS=False)
class PipelineWebLookupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Seeded flaw", jurisdiction="Ohio")
        ingested = ingestion.ingest_upload(seeded("explicit").encode("utf-8"), filename="brief.txt", content_type="text/plain")
        self.brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Seeded flaw",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )

    def run_gym(self):
        run = GymRun.objects.create(workspace=self.workspace, brief=self.brief)
        return execute_run(run, connector_registry=StubRegistry())

    def test_web_lookup_is_on_by_default_but_cannot_run_until_the_installation_allows_it(self):
        run = self.run_gym()
        entry = next(check for check in run.checks_run if check["id"] == "web_lookup")
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("AI_WEB_SEARCH_ENABLED", entry["reason"])
        self.assertFalse(run.challenges.filter(basis=GymChallenge.WEB).exists())

    def test_an_author_who_turns_web_lookup_off_gets_it_off(self):
        from apps.argument_gym import checks

        self.workspace.enabled_checks = [check for check in checks.DEFAULT_CHECK_IDS if check != "web_lookup"]
        self.workspace.save()
        with override_settings(AI_WEB_SEARCH_ENABLED=True):
            run = self.run_gym()
        self.assertEqual(next(check for check in run.checks_run if check["id"] == "web_lookup")["status"], "off")

    def test_selected_but_not_enabled_for_the_installation_reads_as_could_not_run(self):
        from apps.argument_gym import checks

        self.workspace.enabled_checks = [*checks.DEFAULT_CHECK_IDS, "web_lookup"]
        self.workspace.save()
        run = self.run_gym()
        entry = next(check for check in run.checks_run if check["id"] == "web_lookup")
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("AI_WEB_SEARCH_ENABLED", entry["reason"])

    def test_the_rule_profile_finding_is_a_library_challenge_and_the_run_completes(self):
        run = self.run_gym()
        self.assertEqual(run.status, GymRun.COMPLETE)
        self.assertTrue(
            any("covered property" in challenge.opponent_argument.casefold() for challenge in run.challenges.all())
        )
        self.assertEqual(set(run.challenges.values_list("basis", flat=True)), {GymChallenge.LIBRARY})

    def test_an_applicability_failure_is_reported_on_the_check_and_the_run_still_completes(self):
        with mock.patch("apps.argument_gym.applicability.run_applicability", side_effect=RuntimeError("index unreadable")):
            run = self.run_gym()
        self.assertEqual(run.status, GymRun.COMPLETE)
        self.assertEqual(run.applicability["status"], "could_not_run")
        self.assertIn("index unreadable", run.applicability["summary"])

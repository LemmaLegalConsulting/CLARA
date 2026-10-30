import json
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from apps.argument_gym import artifacts, ingestion, record
from apps.argument_gym.models import GymChallenge, GymDocument, GymRun, GymWorkspace
from apps.argument_gym.pipeline import execute_run
from apps.argument_gym.testing import build_pdf
from apps.rules.models import CourtProfile
from apps.drafting.models import DraftDocument, DraftingSession
from apps.drafting.services import create_draft
from apps.matters.models import Matter
from apps.sources.connectors.base import SourceResult
from apps.templates_app.models import DocumentTemplate, TemplateBlock


BRIEF = """IN THE HOUSING COURT

I. INTRODUCTION

Jane Tenant answers the complaint and asks the court to dismiss it.

II. STATEMENT OF FACTS

Jane Tenant has rented the property at 12 Oak Street since 2019 under a written lease.

The landlord did not repair the furnace after repeated written notice from the tenant.

III. ARGUMENT

A. The notice was defective.

The three-day notice omitted the statutory language required by R.C. 1923.04, so the complaint must be dismissed. The notice also arrived after the filing date.

B. The landlord breached the duty to repair.

The landlord's failure to repair the furnace violates R.C. 5321.04 and supports a rent abatement.

IV. CONCLUSION

WHEREFORE, Jane Tenant respectfully requests that the court dismiss the complaint and award an abatement.
"""


class StubRegistry:
    """Retrieval that answers every adversarial query the same way.

    The pipeline is what is under test here, not the connectors, and a real
    search would make these assertions depend on the content library.
    """

    def __init__(self):
        self.queries = []

    def search(self, query, **_kwargs):
        self.queries.append(query)
        return [
            SourceResult(
                id=f"stub-{index}",
                title=f"Authority {index} on notice and repair",
                snippet="A landlord notice that omits the statutory language is defective under Ohio law.",
                source_kind="rag" if index % 2 else "local_cases",
                source_label="Ohio Statutes" if index % 2 else "Ohio cases",
                citation=f"R.C. 1923.0{index}",
            )
            for index in range(1, 7)
        ]


# A brief is characterized in words, never rated. Prose may say the word
# "score" -- the readability check's own description does -- so this looks for a
# field that would carry a rating rather than for the word anywhere in the JSON.
SCORE_KEYS = ("score", "grade", "rating", "points", "percent")


def scored_values(payload, path=""):
    """Every place a report attaches a number to a name that reads as a rating."""
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else key
            if any(word in key.casefold() for word in SCORE_KEYS) and isinstance(value, (int, float)):
                found.append(here)
            found.extend(scored_values(value, here))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(scored_values(item, f"{path}[{index}]"))
    return found


def gym_run(workspace, brief, **kwargs):
    run = GymRun.objects.create(workspace=workspace, brief=brief, **kwargs)
    return execute_run(run, connector_registry=StubRegistry())


class IngestionTests(TestCase):
    """An uploaded brief gets the stable anchors a native draft already has."""

    def setUp(self):
        self.units = ingestion.structure_units(ingestion._plain_paragraphs(BRIEF))

    def test_section_numbering_nests_lettered_subparts(self):
        sections = [unit["locator"]["section"] for unit in self.units if unit["type"] == ingestion.ARGUMENT]
        self.assertIn("III.A", sections)
        self.assertIn("III.B", sections)

    def test_statement_of_facts_paragraphs_are_asserted_facts(self):
        facts = [unit for unit in self.units if unit["type"] == ingestion.ASSERTED_FACT]
        self.assertTrue(any("12 Oak Street" in unit["text"] for unit in facts))

    def test_citations_become_units_anchored_to_their_paragraph(self):
        citations = [unit for unit in self.units if unit["type"] == ingestion.CITATION]
        self.assertTrue(any("1923.04" in unit["text"] for unit in citations))
        self.assertTrue(all(unit.get("parentId") for unit in citations))

    def test_requested_relief_is_identified(self):
        relief = [unit for unit in self.units if unit["type"] == ingestion.REQUESTED_RELIEF]
        self.assertEqual(len(relief), 1)
        self.assertIn("dismiss the complaint", relief[0]["text"])

    def test_every_unit_carries_a_locator_a_reader_can_follow(self):
        for unit in self.units:
            self.assertTrue(unit["id"])
            self.assertIn("section", unit["locator"])
            self.assertIn("excerpt", unit["locator"])

    def test_native_draft_units_carry_the_block_key_the_editor_uses(self):
        units = ingestion.units_from_sections(
            [{"key": "defenses", "label": "Defenses", "body": "The notice is defective under R.C. 1923.04."}]
        )
        self.assertTrue(all(unit["blockKey"] == "defenses" for unit in units))
        self.assertTrue(any(unit["type"] == ingestion.ARGUMENT for unit in units))


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class StandalonePipelineTests(TestCase):
    """A brief with no case record still gets a legal stress test."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.workspace = GymWorkspace.objects.create(
            owner=self.user, title="Uploaded brief", jurisdiction="Cleveland Housing Court"
        )
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt", content_type="text/plain")
        self.brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Answer",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )

    def test_a_run_produces_ranked_sourced_challenges(self):
        run = gym_run(self.workspace, self.brief)
        self.assertEqual(run.status, GymRun.COMPLETE, run.error)
        challenges = list(run.challenges.all())
        self.assertGreater(len(challenges), 0)
        self.assertLessEqual(len(challenges), 7)
        self.assertEqual([challenge.ordinal for challenge in challenges], list(range(1, len(challenges) + 1)))
        importances = [challenge.importance for challenge in challenges]
        self.assertEqual(importances, sorted(importances, reverse=True))

    def test_rejected_attacks_are_not_restored_and_are_auditable(self):
        from apps.argument_gym import pipeline
        from apps.argument_gym.serializers import run_to_dict
        original = pipeline._fallback_assessments
        def reject(attacks):
            return [{**a, "keep": False, "verdict": "misplaced"} for a in original(attacks)]
        with patch.object(pipeline, "_fallback_assessments", side_effect=reject):
            run = gym_run(self.workspace, self.brief)
        self.assertEqual(run.status, GymRun.COMPLETE, run.error)
        self.assertEqual(run.challenges.count(), 0)
        judge = next(stage for stage in run.stage_trace if stage["stage"] == "judge")
        self.assertTrue(judge["assessments"])
        self.assertTrue(all(not a["keep"] for a in judge["assessments"]))
        self.assertEqual(run_to_dict(run)["challenges"], [])

    def test_finding_and_action_are_exposed_in_api(self):
        from apps.argument_gym.serializers import challenge_to_dict
        run = gym_run(self.workspace, self.brief)
        challenge = run.challenges.first()
        challenge.finding = "The notice element has weak support"
        challenge.review_action = "verify"
        challenge.save()
        challenge.refresh_from_db()
        payload = challenge_to_dict(challenge)
        self.assertEqual(payload["finding"], challenge.finding)
        self.assertEqual(payload["reviewAction"], "verify")

    def test_every_challenge_points_at_a_passage_of_the_brief(self):
        run = gym_run(self.workspace, self.brief)
        for challenge in run.challenges.all():
            self.assertTrue(challenge.target.get("unitId"))
            self.assertTrue(challenge.target.get("excerpt"))

    def test_opponent_judge_and_coach_are_separate_recorded_stages(self):
        run = gym_run(self.workspace, self.brief)
        stages = [stage["stage"] for stage in run.stage_trace]
        # No court matched this brief, so the filing-format stage reports itself
        # unavailable in the check plan rather than running.
        self.assertEqual(
            stages,
            [
                "document_checks",
                "materials",
                "argument_map",
                "record_audit",
                "research_queries",
                "research",
                "opponent",
                "rule_elements",
                "judge",
                "coach",
                "assessment",
            ],
        )

    def test_research_coverage_records_the_adversarial_queries(self):
        run = gym_run(self.workspace, self.brief)
        coverage = run.challenges.first().research_coverage
        self.assertTrue(coverage["queries"])
        self.assertGreater(coverage["resultCount"], 0)

    def test_a_brief_with_no_readable_text_fails_the_run_rather_than_the_request(self):
        empty = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Empty",
        )
        run = gym_run(self.workspace, empty)
        self.assertEqual(run.status, GymRun.FAILED)
        self.assertIn("no readable text", run.error)

    def test_a_rerun_carries_a_dismissal_forward_and_reports_what_recurred(self):
        first = gym_run(self.workspace, self.brief)
        dismissed = first.challenges.first()
        dismissed.disposition = GymChallenge.DISMISSED
        dismissed.disposition_note = "Opposing counsel waived this below."
        dismissed.save()

        second = gym_run(self.workspace, self.brief, previous_run=first)
        carried = second.challenges.filter(carried_from=dismissed).first()
        self.assertIsNotNone(carried)
        self.assertEqual(carried.disposition, GymChallenge.DISMISSED)
        self.assertEqual(carried.disposition_note, "Opposing counsel waived this below.")
        self.assertEqual(second.comparison["previousRunId"], first.id)
        self.assertTrue(second.comparison["recurring"])


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class RecordAuditTests(TestCase):
    """Case materials are referenced, ranked, and excludable."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.matter = Matter.objects.create(
            external_id="LS-GYM-1",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            summary="Tenant disputes a three-day notice.",
            source_system="Demo",
        )
        self.workspace = GymWorkspace.objects.create(
            owner=self.user, matter=self.matter, title="Answer", jurisdiction="Cleveland Housing Court"
        )
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt", content_type="text/plain")
        self.brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Answer",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )
        self.lease = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.CASE_RECORD,
            source_type=GymDocument.UPLOAD,
            title="Lease",
            extracted_text="Jane Tenant rented the property at 12 Oak Street beginning in 2019 under a written lease.",
        )

    def test_the_run_reads_uploaded_case_materials_and_names_them(self):
        run = gym_run(self.workspace, self.brief)
        titles = [material["title"] for material in run.materials]
        self.assertIn("Lease", titles)
        audit = next(stage for stage in run.stage_trace if stage["stage"] == "record_audit")
        self.assertNotEqual(audit["method"], "skipped")

    def test_an_excluded_material_is_still_listed_but_not_read(self):
        self.lease.excluded = True
        self.lease.save()
        run = gym_run(self.workspace, self.brief)
        self.assertNotIn("Lease", [material["title"] for material in run.materials])
        available = record.available_materials(self.workspace)
        excluded = [material for material in available if material["excluded"]]
        self.assertEqual([material["title"] for material in excluded], ["Lease"])

    def test_an_unsupported_assertion_becomes_a_factual_challenge(self):
        run = gym_run(self.workspace, self.brief)
        categories = {challenge.category for challenge in run.challenges.all()}
        self.assertTrue(
            categories & {GymChallenge.FACTUAL_SUPPORT, GymChallenge.RECORD_CONFLICT},
            f"expected a record-based challenge, got {categories}",
        )


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class ArtifactTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Answer", jurisdiction="Ohio")
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt", content_type="text/plain")
        self.brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Answer",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )
        self.run = gym_run(self.workspace, self.brief)

    def test_prep_sheet_has_one_row_per_challenge_with_the_columns_an_advocate_argues_from(self):
        sheet = artifacts.opposition_prep_sheet(self.run)
        self.assertEqual(len(sheet["rows"]), self.run.challenges.count())
        row = sheet["rows"][0]
        for key in (
            "likelyOppositionPoint",
            "strongestAuthority",
            "strongestAdverseRecord",
            "currentResponse",
            "suggestedResponse",
            "remainingVulnerability",
        ):
            self.assertIn(key, row)

    def test_the_report_separates_open_vulnerabilities_from_handled_challenges(self):
        challenge = self.run.challenges.first()
        challenge.disposition = GymChallenge.ADDRESSED
        challenge.save()
        report = artifacts.stress_test_report(self.run)
        self.assertIn(challenge.id, [item["challengeId"] for item in report["handledWell"]])
        self.assertNotIn(challenge.id, [item["challengeId"] for item in report["vulnerabilities"]])

    def test_the_report_carries_no_numeric_score(self):
        self.assertEqual(scored_values(artifacts.stress_test_report(self.run)), [])

    def test_an_external_brief_gets_copyable_recommendations_not_a_block_plan(self):
        plan = artifacts.revision_plan(self.run)
        self.assertFalse(plan["actionable"])
        self.assertEqual(plan["plan"], [])
        self.assertTrue(plan["copyOnly"])


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False, ENABLE_DEMO_MATTERS=True)
class NativeDraftTests(TestCase):
    """Starting from Draft mode tests the document the editor is showing."""

    def setUp(self):
        self.user = User.objects.create_superuser("advocate", "advocate@example.com", "secret")
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(
            external_id="LS-GYM-2",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            summary="Tenant disputes a three-day notice.",
            source_system="Demo",
        )
        self.template = DocumentTemplate.objects.create(
            title="Answer", slug="answer-gym-test", kind="answer_counterclaims"
        )
        TemplateBlock.objects.create(
            template=self.template,
            key="defenses",
            label="Defenses",
            block_type="argument",
            order=10,
            body="The three-day notice omitted the language required by R.C. 1923.04, so the complaint must be dismissed.",
            required=True,
        )
        self.session = DraftingSession.objects.create(
            mode="draft_from_template",
            matter=self.matter,
            template=self.template,
            selected_block_keys=["defenses"],
        )
        self.draft = create_draft(self.session)

    def _stress_test(self):
        with patch("apps.argument_gym.pipeline.default_connector_registry", StubRegistry()):
            return self.client.post(
                reverse("api_draft_stress_test", args=[self.draft.id]),
                data="{}",
                content_type="application/json",
            )

    def test_stress_testing_a_draft_creates_a_workspace_linked_to_its_case(self):
        response = self._stress_test()
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["workspace"]["matterId"], self.matter.external_id)
        self.assertTrue(payload["run"]["challenges"])

    def test_rerunning_reuses_the_same_workspace_and_brief(self):
        self._stress_test()
        self._stress_test()
        self.assertEqual(GymWorkspace.objects.count(), 1)
        self.assertEqual(GymDocument.objects.filter(role=GymDocument.BRIEF_UNDER_TEST).count(), 1)
        self.assertEqual(GymRun.objects.count(), 2)
        self.assertEqual(GymRun.objects.order_by("created_at").last().previous_run_id, GymRun.objects.order_by("created_at").first().id)

    def test_a_native_challenge_targets_a_block_the_revision_machinery_can_reach(self):
        run_id = self._stress_test().json()["run"]["id"]
        run = GymRun.objects.get(id=run_id)
        self.assertTrue(any(challenge.target.get("blockKey") == "defenses" for challenge in run.challenges.all()))
        plan = artifacts.revision_plan(run)
        self.assertTrue(plan["actionable"])
        self.assertEqual(plan["plan"][0]["blockKey"], "defenses")

    def test_applying_the_revision_plan_records_the_change_and_marks_the_challenge_addressed(self):
        run_id = self._stress_test().json()["run"]["id"]
        run = GymRun.objects.get(id=run_id)
        plan = artifacts.revision_plan(run)["plan"]
        response = self.client.post(
            reverse("api_gym_run_revision", args=[run.id]),
            data=json.dumps({"plan": plan}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        addressed = run.challenges.filter(disposition=GymChallenge.ADDRESSED)
        self.assertTrue(addressed.exists())
        self.assertTrue(all(challenge.resulting_operation_id for challenge in addressed))
        self.assertTrue(DraftDocument.objects.get(id=self.draft.id).operations.filter(status="applied").exists())

    def test_the_snapshot_records_the_component_versions_the_run_read(self):
        run_id = self._stress_test().json()["run"]["id"]
        run = GymRun.objects.get(id=run_id)
        self.assertEqual(run.snapshot["draftId"], self.draft.id)
        self.assertIn("defenses", run.snapshot["componentVersions"])


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class AccessTests(TestCase):
    """A gym lookup is only ever as permissive as the case behind it."""

    def setUp(self):
        self.owner = User.objects.create_user("owner", password="secret")
        self.other = User.objects.create_user("other", password="secret")
        self.workspace = GymWorkspace.objects.create(owner=self.owner, title="Uploaded brief")

    def test_a_standalone_workspace_is_private_to_its_owner(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("api_gym_workspace_detail", args=[self.workspace.id]))
        self.assertEqual(response.status_code, 404)

    def test_the_owner_can_read_their_own_workspace(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("api_gym_workspace_detail", args=[self.workspace.id]))
        self.assertEqual(response.status_code, 200)

    def test_a_matter_linked_workspace_follows_the_case_access_boundary(self):
        matter = Matter.objects.create(
            external_id="LS-GYM-3",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="Demo",
        )
        self.workspace.matter = matter
        self.workspace.save()
        self.client.force_login(self.other)
        with patch("apps.argument_gym.views.user_can_access_matter", return_value=False):
            self.assertEqual(
                self.client.get(reverse("api_gym_workspace_detail", args=[self.workspace.id])).status_code, 404
            )
        with patch("apps.argument_gym.views.user_can_access_matter", return_value=True):
            self.assertEqual(
                self.client.get(reverse("api_gym_workspace_detail", args=[self.workspace.id])).status_code, 200
            )

    def test_an_anonymous_request_is_rejected(self):
        self.assertEqual(self.client.get(reverse("api_gym_workspaces")).status_code, 401)

    def test_the_list_shows_only_workspaces_the_viewer_can_read(self):
        GymWorkspace.objects.create(owner=self.other, title="Someone else's brief")
        self.client.force_login(self.owner)
        titles = [item["title"] for item in self.client.get(reverse("api_gym_workspaces")).json()["workspaces"]]
        self.assertEqual(titles, ["Uploaded brief"])


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class UploadApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.client.force_login(self.user)
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Uploaded brief")

    def test_uploading_a_brief_stores_its_structure(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("api_gym_workspace_documents", args=[self.workspace.id]),
            {"file": SimpleUploadedFile("answer.txt", BRIEF.encode("utf-8"), content_type="text/plain")},
        )
        self.assertEqual(response.status_code, 201, response.content)
        document = response.json()["document"]
        self.assertEqual(document["role"], GymDocument.BRIEF_UNDER_TEST)
        self.assertGreater(document["unitCount"], 5)

    def test_an_unreadable_upload_is_a_400_not_a_500(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("api_gym_workspace_documents", args=[self.workspace.id]),
            {"file": SimpleUploadedFile("empty.txt", b"   ", content_type="text/plain")},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_run_without_a_brief_is_refused(self):
        response = self.client.post(
            reverse("api_gym_workspace_runs", args=[self.workspace.id]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_a_disposition_must_be_one_of_the_known_values(self):
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt", content_type="text/plain")
        brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Answer",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )
        run = gym_run(self.workspace, brief)
        challenge = run.challenges.first()
        bad = self.client.post(
            reverse("api_gym_challenge_detail", args=[challenge.id]),
            data=json.dumps({"disposition": "resolved"}),
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 400)
        good = self.client.post(
            reverse("api_gym_challenge_detail", args=[challenge.id]),
            data=json.dumps({"disposition": "addressed", "note": "Answered in reply."}),
            content_type="application/json",
        )
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["challenge"]["disposition"], "addressed")


class RecordingClient:
    """Renders every prompt for real, then answers nothing usable.

    The point is the render: a placeholder the YAML declares but the pipeline
    never passes raises `PromptRenderError`, and that failure would otherwise
    only surface with live AI configured.
    """

    def __init__(self):
        self.prompts = []

    def complete(self, *, system, user, **_kwargs):
        self.prompts.append({"system": system, "user": user})
        return ""


@override_settings(AI_DRAFTING_ENABLED=True)
class PromptRenderTests(TestCase):
    """Every gym prompt renders from the context its stage actually supplies."""

    def test_each_stage_renders_its_prompt_and_still_falls_back(self):
        user = User.objects.create_user("advocate", password="secret")
        matter = Matter.objects.create(
            external_id="LS-GYM-4",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            summary="Tenant disputes a three-day notice.",
            source_system="Demo",
        )
        workspace = GymWorkspace.objects.create(
            owner=user, matter=matter, title="Answer", jurisdiction="Cleveland Housing Court"
        )
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt", content_type="text/plain")
        brief = GymDocument.objects.create(
            workspace=workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Answer",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )
        GymDocument.objects.create(
            workspace=workspace,
            role=GymDocument.CASE_RECORD,
            source_type=GymDocument.UPLOAD,
            title="Lease",
            extracted_text="Jane Tenant rented 12 Oak Street beginning in 2019 under a written lease.",
        )
        client = RecordingClient()
        run = execute_run(
            GymRun.objects.create(workspace=workspace, brief=brief),
            connector_registry=StubRegistry(),
            llm_client=client,
        )

        self.assertEqual(run.status, GymRun.COMPLETE, run.error)
        rendered = " ".join(prompt["system"] + prompt["user"] for prompt in client.prompts)
        unfilled = re.search(r"\{[a-z_]+\}", rendered)
        self.assertIsNone(unfilled, f"unfilled placeholder {unfilled.group(0) if unfilled else ''}")
        self.assertGreaterEqual(len(client.prompts), 6)
        # The stages still finished on their deterministic results.
        for stage in run.stage_trace:
            if stage["stage"] in {"materials", "record_audit", "research"}:
                continue
            self.assertEqual(stage["method"], "deterministic", stage)
        self.assertTrue(run.challenges.exists())


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False, ENABLE_DEMO_MATTERS=True)
class SessionManagementTests(TestCase):
    """Past sessions stay reachable, and filterable by the case they belong to."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(
            external_id="LS-GYM-10",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="Demo",
        )
        self.other_matter = Matter.objects.create(
            external_id="LS-GYM-11",
            client_name="Sam Renter",
            matter_type="Eviction",
            jurisdiction="Toledo Municipal Court",
            source_system="Demo",
        )
        self.on_case = self._workspace("Answer for Tenant", self.matter)
        self.other_case = self._workspace("Answer for Renter", self.other_matter)
        self.standalone = self._workspace("Uploaded appellate brief", None)

    def _workspace(self, title, matter):
        workspace = GymWorkspace.objects.create(owner=self.user, matter=matter, title=title)
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt", content_type="text/plain")
        GymDocument.objects.create(
            workspace=workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title=title,
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )
        return workspace

    def _titles(self, query=""):
        response = self.client.get(f"{reverse('api_gym_workspaces')}{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return [item["title"] for item in response.json()["workspaces"]]

    def test_every_session_is_listed_by_default(self):
        self.assertEqual(
            sorted(self._titles()),
            ["Answer for Renter", "Answer for Tenant", "Uploaded appellate brief"],
        )

    def test_sessions_filter_to_one_case(self):
        self.assertEqual(self._titles("?matterId=LS-GYM-10"), ["Answer for Tenant"])

    def test_standalone_sessions_are_reachable_as_their_own_filter(self):
        self.assertEqual(self._titles("?matterId=none"), ["Uploaded appellate brief"])

    def test_the_filter_offers_only_cases_that_have_sessions(self):
        response = self.client.get(reverse("api_gym_workspaces"))
        self.assertEqual(
            sorted(item["id"] for item in response.json()["matters"]),
            ["LS-GYM-10", "LS-GYM-11"],
        )

    def test_a_session_can_be_found_by_the_name_of_its_brief_or_client(self):
        self.assertEqual(self._titles("?q=renter"), ["Answer for Renter"])

    def test_the_list_carries_what_a_person_needs_to_choose_a_session(self):
        gym_run(self.on_case, self.on_case.documents.first())
        row = next(item for item in self.client.get(reverse("api_gym_workspaces")).json()["workspaces"] if item["id"] == self.on_case.id)
        self.assertEqual(row["matterName"], "Jane Tenant")
        self.assertEqual(row["runCount"], 1)
        self.assertGreater(row["openChallengeCount"], 0)
        self.assertTrue(row["lastRunAt"])

    def test_reopening_a_session_returns_its_last_run_with_the_challenges(self):
        run = gym_run(self.on_case, self.on_case.documents.first())
        payload = self.client.get(reverse("api_gym_workspace_detail", args=[self.on_case.id])).json()
        self.assertEqual(payload["latestRun"]["id"], run.id)
        self.assertEqual(len(payload["latestRun"]["challenges"]), run.challenges.count())
        self.assertTrue(payload["latestRun"]["assessment"])


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class JurisdictionTests(TestCase):
    """Jurisdiction is chosen by hand or detected, and never invented."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.client.force_login(self.user)
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Uploaded brief")
        self.housing = CourtProfile.objects.create(
            slug="cleveland-housing",
            name="Cleveland Municipal Court, Housing Division",
            court_type=CourtProfile.MUNICIPAL,
            state="Ohio",
            county="Cuyahoga",
            municipality="Cleveland",
            aliases=["Cleveland Housing Court"],
            verification=CourtProfile.VERIFIED,
            pleading_types=["motion", "answer"],
            required_elements=[
                {"id": "certificate_of_service", "label": "Certificate of service", "severity": "error", "patterns": ["certificate of service"]}
            ],
        )

    def _patch(self, payload):
        return self.client.patch(
            reverse("api_gym_workspace_detail", args=[self.workspace.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_a_municipality_can_be_set_by_hand(self):
        response = self._patch(
            {
                "jurisdictionMode": "manual",
                "jurisdictionDetail": {"state": "Ohio", "county": "Cuyahoga", "municipality": "Cleveland", "courtType": "municipal"},
            }
        )
        self.assertEqual(response.status_code, 200, response.content)
        workspace = response.json()["workspace"]
        self.assertEqual(workspace["jurisdictionMode"], "manual")
        self.assertEqual(workspace["jurisdictionDetail"]["municipality"], "Cleveland")
        self.assertEqual(workspace["jurisdiction"], "Cleveland, Cuyahoga County, Ohio")

    def test_an_appellate_division_keeps_its_district_and_drops_the_municipality(self):
        response = self._patch(
            {
                "jurisdictionMode": "manual",
                "jurisdictionDetail": {
                    "state": "Ohio",
                    "municipality": "Cleveland",
                    "division": "Eighth Appellate District",
                    "courtType": "appellate",
                },
            }
        )
        detail = response.json()["workspace"]["jurisdictionDetail"]
        self.assertNotIn("municipality", detail)
        self.assertEqual(detail["division"], "Eighth Appellate District")
        self.assertEqual(response.json()["workspace"]["jurisdiction"], "Eighth Appellate District, Ohio")

    def test_a_court_can_be_selected_by_hand_for_its_filing_rules(self):
        response = self._patch({"courtRuleMode": "manual", "courtSlug": "cleveland-housing"})
        self.assertEqual(response.json()["workspace"]["court"]["slug"], "cleveland-housing")
        self.assertFalse(response.json()["workspace"]["court"]["usesMunicipality"] is None)

    def test_an_unknown_court_is_refused(self):
        self.assertEqual(self._patch({"courtSlug": "not-a-court"}).status_code, 400)

    def test_detection_reports_the_phrase_that_decided_it(self):
        GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Motion",
            extracted_text="IN THE CLEVELAND MUNICIPAL COURT, HOUSING DIVISION\nMOTION TO DISMISS",
        )
        payload = self.client.get(reverse("api_gym_workspace_court_detection", args=[self.workspace.id])).json()
        self.assertTrue(payload["detection"]["detected"])
        self.assertEqual(payload["detection"]["court"]["slug"], "cleveland-housing")
        self.assertIn("Cleveland", payload["detection"]["matched"])
        self.assertEqual(payload["pleadingType"], "motion")

    def test_the_courts_endpoint_says_which_types_use_a_municipality(self):
        payload = self.client.get(reverse("api_gym_courts")).json()
        by_id = {item["id"]: item["usesMunicipality"] for item in payload["courtTypes"]}
        self.assertTrue(by_id["municipal"])
        self.assertFalse(by_id["appellate"])
        self.assertTrue(any(court["slug"] == "generic-ohio-trial-court" for court in payload["courts"]))


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class RunComplianceTests(TestCase):
    """A run reports whether the paper meets the court's filing rules."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Motion")
        self.court = CourtProfile.objects.create(
            slug="test-court",
            name="Test Municipal Court",
            court_type=CourtProfile.MUNICIPAL,
            state="Ohio",
            municipality="Cleveland",
            aliases=["Test Municipal Court"],
            verification=CourtProfile.VERIFIED,
            source="Local Rule 3.1",
            pleading_types=["motion"],
            formatting={"page_limits": [{"pleading_types": ["motion"], "max_pages": 2}]},
            required_elements=[
                {"id": "certificate_of_service", "label": "Certificate of service", "severity": "error", "patterns": ["certificate of service"]}
            ],
        )

    def _brief(self, text, *, filename="motion.txt"):
        ingested = ingestion.ingest_upload(text if isinstance(text, bytes) else text.encode("utf-8"), filename=filename)
        return GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Motion to dismiss",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )

    def test_the_detected_court_is_recorded_with_the_evidence_for_it(self):
        brief = self._brief("IN THE TEST MUNICIPAL COURT\n\nMOTION TO DISMISS\n\nCERTIFICATE OF SERVICE")
        run = gym_run(self.workspace, brief)
        self.assertEqual(run.court, self.court)
        self.assertEqual(run.court_detection["mode"], "auto")
        self.assertIn("Test Municipal Court", run.court_detection["matched"])
        self.assertEqual(run.compliance["pleadingType"], "motion")

    def test_a_missing_required_element_is_reported_as_a_filing_finding(self):
        brief = self._brief("IN THE TEST MUNICIPAL COURT\n\nMOTION TO DISMISS\n\nThe notice was defective.")
        run = gym_run(self.workspace, brief)
        codes = [finding["ruleCode"] for finding in run.compliance["findings"]]
        self.assertIn("E900", codes)

    def test_a_page_limit_is_checked_against_the_brief_not_its_exhibits(self):
        pages = [
            "IN THE TEST MUNICIPAL COURT\nMOTION TO DISMISS",
            "The notice was defective.\nCERTIFICATE OF SERVICE",
            "EXHIBIT A",
            "lease text",
            "more lease text",
        ]
        brief = self._brief(build_pdf(pages), filename="motion.pdf")
        run = gym_run(self.workspace, brief)
        codes = [finding["ruleCode"] for finding in run.compliance["findings"]]
        self.assertNotIn("E940", codes)

    def test_turning_the_rules_off_says_so_rather_than_reporting_a_clean_filing(self):
        self.workspace.court_rule_mode = GymWorkspace.OFF
        self.workspace.save()
        run = gym_run(self.workspace, self._brief("IN THE TEST MUNICIPAL COURT"))
        self.assertFalse(run.compliance["checked"])
        self.assertIn("turned off", run.compliance["detection"]["reason"])

    def test_choosing_a_court_by_hand_overrides_what_the_caption_says(self):
        other = CourtProfile.objects.create(
            slug="other-court", name="Other Municipal Court", court_type=CourtProfile.MUNICIPAL, state="Ohio"
        )
        self.workspace.court_rule_mode = GymWorkspace.MANUAL
        self.workspace.court = other
        self.workspace.save()
        run = gym_run(self.workspace, self._brief("IN THE TEST MUNICIPAL COURT"))
        self.assertEqual(run.court, other)
        self.assertEqual(run.court_detection["mode"], "manual")


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class AssessmentTests(TestCase):
    """The report opens with a judgment about the brief, in words rather than a score."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Answer")
        ingested = ingestion.ingest_upload(BRIEF.encode("utf-8"), filename="answer.txt")
        self.brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.UPLOAD,
            title="Answer",
            extracted_text=ingested["text"],
            extraction_metadata=ingested["metadata"],
        )
        self.run = gym_run(self.workspace, self.brief)

    def test_a_run_writes_a_verdict_and_one_paragraph(self):
        self.assertTrue(self.run.assessment_verdict)
        self.assertTrue(self.run.assessment)
        self.assertNotIn("\n\n", self.run.assessment)
        self.assertLessEqual(len(self.run.assessment.split()), 200)

    def test_the_assessment_names_what_most_needs_addressing(self):
        self.assertIn("most important to address", self.run.assessment)

    def test_the_report_leads_with_the_assessment_and_still_carries_no_score(self):
        report = artifacts.stress_test_report(self.run)
        self.assertEqual(report["assessment"], self.run.assessment)
        self.assertEqual(report["verdict"], self.run.assessment_verdict)
        self.assertEqual(scored_values(report), [])
        self.assertNotRegex(report["assessment"], r"\b\d+\s*(/|out of)\s*\d+\b")

    def test_a_paragraph_longer_than_the_report_has_room_for_is_cut_to_one(self):
        from apps.argument_gym.pipeline import _one_paragraph

        self.assertEqual(_one_paragraph("First para.\n\nSecond para."), "First para.")
        self.assertTrue(_one_paragraph(" ".join(["word"] * 400)).endswith("..."))


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class ExhibitUploadTests(TestCase):
    """A filing uploaded with its exhibits is split before anything reads it."""

    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.client.force_login(self.user)
        self.workspace = GymWorkspace.objects.create(owner=self.user, title="Filing")

    def test_exhibits_become_case_record_material_rather_than_part_of_the_brief(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        content = build_pdf(
            [
                "IN THE TEST COURT\nMOTION TO DISMISS",
                "The notice was defective.\nCERTIFICATE OF SERVICE",
                "EXHIBIT A",
                "The lease sets rent at $900 per month.",
            ]
        )
        response = self.client.post(
            reverse("api_gym_workspace_documents", args=[self.workspace.id]),
            {"file": SimpleUploadedFile("filing.pdf", content, content_type="application/pdf")},
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertEqual(payload["document"]["role"], GymDocument.BRIEF_UNDER_TEST)
        self.assertEqual(payload["document"]["pleadingType"], "motion")
        self.assertEqual(payload["document"]["pageRange"], {"start": 1, "end": 2})
        self.assertEqual(len(payload["exhibits"]), 1)
        exhibit = GymDocument.objects.get(id=payload["exhibits"][0]["id"])
        self.assertEqual(exhibit.role, GymDocument.CASE_RECORD)
        self.assertEqual(exhibit.split_from_id, payload["document"]["id"])
        self.assertIn("$900", exhibit.extracted_text)
        self.assertNotIn("$900", GymDocument.objects.get(id=payload["document"]["id"]).extracted_text)

    def test_a_split_exhibit_is_offered_as_case_material_the_run_can_read(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        content = build_pdf(
            [
                "IN THE TEST COURT\nMOTION TO DISMISS",
                "The notice was defective.\nCERTIFICATE OF SERVICE",
                "EXHIBIT A",
                "The lease sets rent at $900 per month.",
            ]
        )
        self.client.post(
            reverse("api_gym_workspace_documents", args=[self.workspace.id]),
            {"file": SimpleUploadedFile("filing.pdf", content, content_type="application/pdf")},
        )
        materials = self.client.get(reverse("api_gym_workspace_materials", args=[self.workspace.id])).json()["materials"]
        self.assertTrue(any("Exhibit A" in material["title"] for material in materials))

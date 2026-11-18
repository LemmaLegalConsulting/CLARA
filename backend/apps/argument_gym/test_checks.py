"""The author's selection of checks, and the checks themselves."""

import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from apps.argument_gym import checks, ingestion
from apps.argument_gym.models import GymChecklist, GymDocument, GymRun, GymWorkspace
from apps.argument_gym.pipeline import execute_run
from apps.argument_gym.tests import BRIEF, StubRegistry
from apps.rules.models import CourtProfile


def run_with(workspace, brief, **kwargs):
    return execute_run(
        GymRun.objects.create(workspace=workspace, brief=brief, **kwargs),
        connector_registry=StubRegistry(),
    )


class CatalogTests(TestCase):
    def test_the_catalog_declares_every_check_with_what_it_needs(self):
        catalog = checks.catalog()
        ids = {check["id"] for check in catalog}
        self.assertIn("adversarial", ids)
        self.assertIn("draft_validation", ids)
        self.assertIn("passive_voice", ids)
        self.assertIn("rule_elements", ids)
        for check in catalog:
            self.assertIn(check["kind"], {"deterministic", "model", "web"})
            self.assertTrue(check["description"])

    def test_an_empty_selection_means_the_defaults(self):
        self.assertEqual(checks.normalize_selection([]), checks.DEFAULT_CHECK_IDS)

    def test_turning_everything_off_is_distinguishable_from_a_new_session(self):
        self.assertEqual(checks.normalize_selection([checks.NONE_SELECTED]), [])

    def test_an_unknown_check_id_is_dropped_rather_than_running_something_else(self):
        self.assertEqual(checks.normalize_selection(["grammar", "telepathy"]), ["grammar"])

    def test_a_check_that_was_turned_off_reads_differently_from_one_that_cannot_run(self):
        plan = checks.plan_checks(
            ["grammar", "draft_validation"],
            {"native_draft": False, "court_profile": False, "case_record": False, "checklist": False},
        )
        by_id = {entry["id"]: entry for entry in plan}
        self.assertEqual(by_id["grammar"]["status"], "on")
        self.assertEqual(by_id["draft_validation"]["status"], "unavailable")
        self.assertIn("uploaded rather than drafted here", by_id["draft_validation"]["reason"])
        self.assertEqual(by_id["passive_voice"]["status"], "off")
        self.assertIn("turned this check off", by_id["passive_voice"]["reason"])


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class SelectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.client.force_login(self.user)
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

    def _patch(self, payload):
        return self.client.patch(
            reverse("api_gym_workspace_detail", args=[self.workspace.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_the_catalog_is_served_with_its_defaults(self):
        payload = self.client.get(reverse("api_gym_checks")).json()
        self.assertTrue(payload["checks"])
        self.assertEqual(payload["defaults"], checks.DEFAULT_CHECK_IDS)

    def test_an_author_can_choose_the_checks_and_the_choice_sticks(self):
        response = self._patch({"enabledChecks": ["grammar", "pleading_form"]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["workspace"]["enabledChecks"], ["grammar", "pleading_form"])
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.enabled_checks, ["grammar", "pleading_form"])

    def test_an_unknown_check_is_refused_rather_than_ignored(self):
        response = self._patch({"enabledChecks": ["grammar", "telepathy"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("telepathy", response.json()["error"])

    def test_turning_every_check_off_does_not_quietly_restore_the_defaults(self):
        self._patch({"enabledChecks": []})
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.enabled_checks, [checks.NONE_SELECTED])
        self.assertEqual(
            self.client.get(reverse("api_gym_workspace_detail", args=[self.workspace.id])).json()["workspace"][
                "enabledChecks"
            ],
            [],
        )

    def test_a_run_records_which_checks_ran_and_why_the_rest_did_not(self):
        self._patch({"enabledChecks": ["pleading_form", "grammar", "draft_validation"]})
        self.workspace.refresh_from_db()
        run = run_with(self.workspace, self.brief)
        by_id = {entry["id"]: entry for entry in run.checks_run}
        self.assertEqual(by_id["pleading_form"]["status"], "on")
        self.assertEqual(by_id["draft_validation"]["status"], "unavailable")
        self.assertEqual(by_id["adversarial"]["status"], "off")
        self.assertIn("pleading_form", run.check_results)

    def test_a_check_the_author_turned_off_produces_no_findings_at_all(self):
        self._patch({"enabledChecks": ["grammar"]})
        self.workspace.refresh_from_db()
        run = run_with(self.workspace, self.brief)
        self.assertNotIn("passive_voice", run.check_results)
        self.assertNotIn("pleading_form", run.check_results)

    def test_passive_voice_accepts_phrases_this_court_expects(self):
        self._patch(
            {
                "enabledChecks": ["passive_voice"],
                "checkSettings": {"passive_voice": {"acceptedPassivePhrases": ["was defective"]}},
            }
        )
        self.workspace.refresh_from_db()
        run = run_with(self.workspace, self.brief)
        phrases = [
            finding["details"]["phrase"]
            for finding in run.check_results.get("passive_voice", {}).get("findings", [])
        ]
        self.assertFalse(any("was defective" in phrase for phrase in phrases))

    def test_the_adversarial_check_can_be_turned_off_and_the_cards_still_rank(self):
        self._patch({"enabledChecks": ["rule_elements"]})
        self.workspace.refresh_from_db()
        run = run_with(self.workspace, self.brief)
        stages = {stage["stage"]: stage["method"] for stage in run.stage_trace}
        self.assertEqual(stages["opponent"], "off")
        self.assertEqual(stages["judge"], "off")
        self.assertTrue(run.rule_audit)


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class DraftValidationCheckTests(TestCase):
    """The checks Draft mode already runs are visible here too."""

    def setUp(self):
        from apps.drafting.models import DraftingSession
        from apps.drafting.services import create_draft
        from apps.matters.models import Matter
        from apps.templates_app.models import DocumentTemplate, TemplateBlock

        self.user = User.objects.create_user("advocate", password="secret")
        matter = Matter.objects.create(
            external_id="LS-GYM-20",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="Demo",
        )
        template = DocumentTemplate.objects.create(title="Answer", slug="answer-checks-test", kind="answer_counterclaims")
        TemplateBlock.objects.create(
            template=template,
            key="defenses",
            label="Defenses",
            block_type="argument",
            order=10,
            body="The notice omitted the language required by R.C. 1923.04.",
            required=True,
        )
        session = DraftingSession.objects.create(
            mode="draft_from_template", matter=matter, template=template, selected_block_keys=["defenses"]
        )
        draft = create_draft(session)
        self.workspace = GymWorkspace.objects.create(
            owner=self.user, matter=matter, title="Answer", enabled_checks=["draft_validation"]
        )
        self.brief = GymDocument.objects.create(
            workspace=self.workspace,
            role=GymDocument.BRIEF_UNDER_TEST,
            source_type=GymDocument.DRAFT_DOCUMENT,
            draft_document=draft,
            title=draft.title,
        )

    @override_settings(ENABLE_DEMO_MATTERS=True)
    def test_draft_mode_validation_runs_on_a_native_draft(self):
        run = run_with(self.workspace, self.brief)
        by_id = {entry["id"]: entry for entry in run.checks_run}
        self.assertEqual(by_id["draft_validation"]["status"], "on")
        self.assertIn("draft_validation", run.check_results)
        for finding in run.check_results["draft_validation"]["findings"]:
            self.assertIn("ruleCode", finding)
            self.assertIn("severity", finding)


@override_settings(ARGUMENT_GYM_BACKGROUND_RUNS=False, AI_DRAFTING_ENABLED=False)
class ChecklistApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("advocate", password="secret")
        self.other = User.objects.create_user("other", password="secret")
        self.client.force_login(self.user)

    def test_a_checklist_is_created_with_its_items_numbered(self):
        response = self.client.post(
            reverse("api_gym_checklists"),
            data=json.dumps(
                {
                    "title": "Pre-filing review",
                    "items": [
                        {"text": "Every date in the statement of facts appears in a document in the file."},
                        {"text": "Each authority cited is still good law."},
                        {"text": "   "},
                    ],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        items = response.json()["checklist"]["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "item-1")

    def test_a_checklist_without_a_name_is_refused(self):
        response = self.client.post(
            reverse("api_gym_checklists"), data=json.dumps({"items": []}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_someone_elses_private_checklist_is_not_listed(self):
        GymChecklist.objects.create(owner=self.other, title="Private list")
        shared = GymChecklist.objects.create(owner=self.other, title="Shared list", shared=True)
        titles = [item["title"] for item in self.client.get(reverse("api_gym_checklists")).json()["checklists"]]
        self.assertEqual(titles, ["Shared list"])
        self.assertEqual(
            self.client.get(reverse("api_gym_checklist_detail", args=[shared.id])).status_code, 200
        )

    def test_a_shared_checklist_is_readable_but_not_editable_by_others(self):
        shared = GymChecklist.objects.create(owner=self.other, title="Shared list", shared=True)
        response = self.client.patch(
            reverse("api_gym_checklist_detail", args=[shared.id]),
            data=json.dumps({"title": "Mine now"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_attaching_a_checklist_makes_the_custom_check_available(self):
        checklist = GymChecklist.objects.create(owner=self.user, title="Mine", items=[{"id": "i1", "text": "Check it."}])
        workspace = GymWorkspace.objects.create(owner=self.user, title="Answer")
        response = self.client.patch(
            reverse("api_gym_workspace_detail", args=[workspace.id]),
            data=json.dumps({"checklistId": checklist.id, "enabledChecks": ["custom_checklist"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["workspace"]["checklist"]["title"], "Mine")

    def test_a_checklist_the_user_cannot_read_cannot_be_attached(self):
        checklist = GymChecklist.objects.create(owner=self.other, title="Private")
        workspace = GymWorkspace.objects.create(owner=self.user, title="Answer")
        response = self.client.patch(
            reverse("api_gym_workspace_detail", args=[workspace.id]),
            data=json.dumps({"checklistId": checklist.id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class SavedCheckDefaultsTests(TestCase):
    """A person's saved default checks: every check, and only where a session made no choice."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user("defaults-owner", password="secret")
        self.client.force_login(self.user)

    def save(self, enabled):
        return self.client.put(
            "/api/argument-gym/checks/defaults/", data=json.dumps({"enabledChecks": enabled}), content_type="application/json"
        )

    def test_web_lookup_is_a_default_check_that_an_installation_can_still_rule_out(self):
        self.assertIn("web_lookup", checks.DEFAULT_CHECK_IDS)
        plan = checks.plan_checks([], {"web_search": False})
        entry = next(item for item in plan if item["id"] == "web_lookup")
        self.assertEqual(entry["status"], "unavailable")

    def test_saved_defaults_apply_to_a_session_that_made_no_choice_and_keep_what_was_off(self):
        response = self.save(["adversarial", "grammar"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["savedDefaults"])
        defaults = checks.defaults_for(self.user)
        self.assertEqual(set(defaults), {"adversarial", "grammar"})
        plan = checks.plan_checks([], {"web_search": True}, defaults=defaults)
        self.assertEqual(next(item for item in plan if item["id"] == "web_lookup")["status"], "off")
        # A session's own choice still wins over the saved defaults.
        explicit = checks.plan_checks(["web_lookup"], {"web_search": True}, defaults=defaults)
        self.assertEqual(next(item for item in explicit if item["id"] == "web_lookup")["status"], "on")

    def test_a_check_added_after_the_defaults_were_saved_takes_its_catalog_default(self):
        seen_without_applicability = [check_id for check_id in checks.CHECKS_BY_ID if check_id != "applicability"]
        resolved = checks.saved_defaults(["adversarial"], seen_without_applicability)
        self.assertIn("applicability", resolved)
        self.assertNotIn("grammar", resolved)

    def test_resetting_returns_to_the_catalog_defaults(self):
        self.save(["grammar"])
        response = self.client.delete("/api/argument-gym/checks/defaults/")
        self.assertFalse(response.json()["savedDefaults"])
        self.assertEqual(response.json()["defaults"], checks.DEFAULT_CHECK_IDS)

    def test_an_unknown_check_is_refused(self):
        self.assertEqual(self.save(["no_such_check"]).status_code, 400)

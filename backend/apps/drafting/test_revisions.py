"""Two tabs editing the same work produce a conflict, never a silent overwrite."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.drafting.generation_jobs import start_job
from apps.drafting.models import DraftDocument, DraftGenerationJob, DraftingSession
from apps.drafting.revisions import record_validation
from apps.drafting.serializers import draft_to_dict
from apps.matters.models import Matter


class NotStarted:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class RevisionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("advocate", "a@example.com", "pw", is_staff=True)
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(source_system="Manual", raw_payload={"created_by_user_id": self.user.pk},
            external_id="MID-1", client_name="Jane Tenant", matter_type="Eviction", jurisdiction="Cleveland"
        )
        self.session = DraftingSession.objects.create(
            mode="draft_from_template", matter=self.matter, goal="Answer", status="setup",
            draft_plan={"documents": [{"title": "Answer"}]},
        )
        self.draft = DraftDocument.objects.create(
            session=self.session, title="Answer", sections=[], plain_text="Original text."
        )

    def patch(self, url, body):
        return self.client.patch(url, data=json.dumps(body), content_type="application/json")

    # -- session checkpoints ------------------------------------------------

    def test_checkpoint_saves_choices_and_nothing_else(self):
        revision = self.session.revision
        response = self.patch(
            f"/api/drafting-sessions/{self.session.id}/",
            {
                "revision": revision,
                "goal": "Answer and counterclaims",
                "selectedFactIds": [4, 5],
                "workflowOptions": {"planningMode": "known", "allowMultipleDocuments": True, "bogus": 1},
                "status": "export",
                "draftPlan": {"documents": []},
            },
        )
        self.assertEqual(response.status_code, 200)
        saved = response.json()["session"]
        self.assertEqual(saved["goal"], "Answer and counterclaims")
        self.assertEqual(saved["selectedFactIds"], [4, 5])
        self.assertEqual(saved["workflowOptions"], {"planningMode": "known", "allowMultipleDocuments": True})
        self.assertEqual(saved["revision"], revision + 1)
        self.session.refresh_from_db()
        # Not in the allowlist: a checkpoint never advances or replans.
        self.assertEqual(self.session.status, "setup")
        self.assertEqual(self.session.draft_plan, {"documents": [{"title": "Answer"}]})

    def test_second_tab_checkpoint_is_refused_with_the_current_session(self):
        base = self.session.revision
        first = self.patch(f"/api/drafting-sessions/{self.session.id}/", {"revision": base, "goal": "Tab one"})
        second = self.patch(f"/api/drafting-sessions/{self.session.id}/", {"revision": base, "goal": "Tab two"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        payload = second.json()
        self.assertEqual(payload["conflict"], "session")
        self.assertEqual(payload["session"]["goal"], "Tab one")
        self.assertEqual(payload["currentRevision"], base + 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.goal, "Tab one")

    def test_checkpoint_requires_a_revision_and_well_typed_fields(self):
        url = f"/api/drafting-sessions/{self.session.id}/"
        self.assertEqual(self.patch(url, {"goal": "No revision"}).status_code, 400)
        self.assertEqual(self.patch(url, {"revision": self.session.revision, "selectedFactIds": "4"}).status_code, 400)
        self.assertEqual(
            self.patch(url, {"revision": self.session.revision, "workflowOptions": {"planningMode": "guess"}}).status_code, 400
        )

    def test_plan_edit_against_an_old_revision_is_refused(self):
        stale = self.session.revision
        self.patch(f"/api/drafting-sessions/{self.session.id}/", {"revision": stale, "goal": "Moved on"})
        response = self.patch(
            f"/api/drafting-sessions/{self.session.id}/plan/",
            {"revision": stale, "draftPlan": {"documents": [{"title": "Other"}]}},
        )
        self.assertEqual(response.status_code, 409)

    def test_workflow_status_saves_do_not_count_as_edits(self):
        revision = self.session.revision
        self.session.status = "validation"
        self.session.save(update_fields=["status", "updated_at"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.revision, revision)

    # -- documents --------------------------------------------------------------

    def test_document_edit_against_an_old_revision_is_refused_and_changes_nothing(self):
        base = self.draft.revision
        url = f"/api/drafts/{self.draft.id}/"
        first = self.patch(url, {"revision": base, "plainText": "Tab one."})
        second = self.patch(url, {"revision": base, "plainText": "Tab two."})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["draft"]["revision"], base + 1)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["draft"]["plainText"], "Tab one.")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.plain_text, "Tab one.")

    def test_document_edit_must_name_its_revision(self):
        response = self.patch(f"/api/drafts/{self.draft.id}/", {"plainText": "Unversioned."})
        self.assertEqual(response.status_code, 400)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.plain_text, "Original text.")

    # -- validation provenance ---------------------------------------------------

    def test_findings_are_current_until_the_text_changes(self):
        self.assertEqual(draft_to_dict(self.draft)["validation"]["state"], "never")
        self.draft.validation_flags = []
        self.draft.save(update_fields=["validation_flags", "updated_at"])
        record_validation(self.draft, {"remainingErrorCount": 0})
        self.draft.refresh_from_db()
        state = draft_to_dict(self.draft)["validation"]
        self.assertEqual(state["state"], "current")
        self.assertEqual(state["checkedRevision"], self.draft.revision)
        self.patch(f"/api/drafts/{self.draft.id}/", {"revision": self.draft.revision, "plainText": "Changed."})
        self.draft.refresh_from_db()
        self.assertEqual(draft_to_dict(self.draft)["validation"]["state"], "stale")

    def test_validate_endpoint_records_which_revision_it_checked(self):
        response = self.client.post(f"/api/drafts/{self.draft.id}/validate/")
        self.assertEqual(response.status_code, 200)
        validation = response.json()["draft"]["validation"]
        self.assertEqual(validation["state"], "current")
        self.assertEqual(validation["checkedRevision"], response.json()["draft"]["revision"])


@override_settings(DRAFT_GENERATION_BACKGROUND=True)
@patch("apps.drafting.generation_jobs.threading.Thread", NotStarted)
class GenerationGuardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("advocate", "a@example.com", "pw", is_staff=True)
        matter = Matter.objects.create(source_system="Manual", raw_payload={"created_by_user_id": self.user.pk}, external_id="MID-1", client_name="Jane", matter_type="Eviction", jurisdiction="X")
        self.session = DraftingSession.objects.create(
            mode="draft_from_template", matter=matter, draft_plan={"documents": [{"title": "Answer"}]}
        )

    def test_a_retried_request_gets_the_same_job(self):
        first = start_job(self.session, user=self.user, idempotency_key="k-1")
        first.status = DraftGenerationJob.COMPLETE
        first.save(update_fields=["status"])
        again = start_job(self.session, user=self.user, idempotency_key="k-1")
        self.assertEqual(again.id, first.id)
        self.assertEqual(DraftGenerationJob.objects.count(), 1)

    def test_a_second_start_while_one_runs_joins_it(self):
        first = start_job(self.session, user=self.user, idempotency_key="k-1")
        second = start_job(self.session, user=self.user, idempotency_key="k-2")
        self.assertEqual(second.id, first.id)

    def test_the_database_refuses_two_active_generations(self):
        DraftGenerationJob.objects.create(session=self.session, status=DraftGenerationJob.PENDING)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DraftGenerationJob.objects.create(session=self.session, status=DraftGenerationJob.RUNNING)

    def test_a_job_records_what_it_drafted_from(self):
        job = start_job(self.session, user=self.user)
        self.assertEqual(job.input_revision, self.session.revision)
        self.assertEqual(len(job.input_hash), 64)

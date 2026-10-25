"""Draft generation answers at once and is polled, never held open.

Held open, it outlived nginx: the browser saw "Failed to fetch" while the draft
was saved anyway, and clicking Generate again made a second one.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.drafting.generation_jobs import _execute
from apps.drafting.models import DraftGenerationJob, DraftingSession
from apps.drafting.services import create_or_update_plan
from apps.matters.models import Matter, MatterFact
from apps.templates_app.models import DocumentTemplate, TemplateBlock


class NotStarted:
    """Stands in for threading.Thread: the job is created and left pending."""

    def __init__(self, *args, **kwargs):
        self.target = kwargs.get("target")

    def start(self):
        pass


@override_settings(DRAFT_GENERATION_BACKGROUND=True, AI_DRAFTING_ENABLED=False)
class DraftGenerationJobTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("advocate", "advocate@example.com", "pw", is_staff=True)
        self.client.force_login(self.user)
        matter = Matter.objects.create(source_system="Manual", raw_payload={"created_by_user_id": self.user.pk},
            external_id="LS-300", client_name="Jane Tenant", matter_type="Eviction",
            jurisdiction="Cleveland Housing Court", summary="Rent dispute and repairs.",
        )
        MatterFact.objects.create(
            matter=matter, slug="rent", title="Rent", text="Disputed balance.",
            source_label="LegalServer", selected_by_default=True,
        )
        template = DocumentTemplate.objects.create(title="Job Motion", slug="job-motion", kind="motion", is_active=True)
        TemplateBlock.objects.create(
            template=template, key="job-body", label="Body", block_type="argument",
            order=10, body="Body text.", required=True,
        )
        session = DraftingSession.objects.create(
            mode="draft_from_template", matter=matter, selected_template_ids=[template.id],
        )
        self.session = create_or_update_plan(session, {})
        self.url = f"/api/drafting-sessions/{self.session.id}/drafts/"

    def _post(self):
        return self.client.post(self.url, data="{}", content_type="application/json")

    def test_generation_answers_202_with_a_job_to_poll(self):
        with patch("apps.drafting.generation_jobs.threading.Thread", NotStarted):
            response = self._post()

        self.assertEqual(response.status_code, 202)
        job = response.json()["job"]
        self.assertEqual(job["status"], "pending")
        polled = self.client.get(f"{self.url}?job={job['id']}")
        self.assertEqual(polled.json()["job"]["status"], "pending")
        self.assertNotIn("drafts", polled.json())

    def test_a_second_click_waits_on_the_first_instead_of_drafting_twice(self):
        with patch("apps.drafting.generation_jobs.threading.Thread", NotStarted):
            first = self._post().json()["job"]
            second = self._post().json()["job"]

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(DraftGenerationJob.objects.filter(session=self.session).count(), 1)

    def test_a_finished_job_returns_its_drafts(self):
        with patch("apps.drafting.generation_jobs.threading.Thread", NotStarted):
            job_id = self._post().json()["job"]["id"]
        _execute(DraftGenerationJob.objects.get(id=job_id), user=self.user)

        polled = self.client.get(f"{self.url}?job={job_id}").json()

        self.assertEqual(polled["job"]["status"], "complete")
        self.assertEqual(len(polled["drafts"]), 1)
        self.assertEqual(polled["drafts"][0]["id"], polled["job"]["draftIds"][0])

    def test_a_job_whose_worker_died_reports_failure_and_frees_the_session(self):
        with patch("apps.drafting.generation_jobs.threading.Thread", NotStarted):
            job_id = self._post().json()["job"]["id"]
        DraftGenerationJob.objects.filter(id=job_id).update(created_at=timezone.now() - timedelta(hours=1))

        polled = self.client.get(f"{self.url}?job={job_id}").json()
        self.assertEqual(polled["job"]["status"], "failed")
        self.assertIn("Generate the draft again", polled["job"]["error"])
        with patch("apps.drafting.generation_jobs.threading.Thread", NotStarted):
            self.assertNotEqual(self._post().json()["job"]["id"], job_id)

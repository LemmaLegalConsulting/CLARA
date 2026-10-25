"""Reopening saved drafting work by URL reads, and only reads.

A link has to reach the exact saved session it names, through a parent chain
that is checked link by link, and opening it must never generate, approve, or
save anything.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.drafting.models import DraftDocument, DraftGenerationJob, DraftingSession
from apps.matters.models import Matter
from apps.matters.route_aliases import sync_matter_route_alias
from apps.templates_app.models import DocumentTemplate


def make_matter(external_id, case_number):
    matter = Matter.objects.create(
        external_id=external_id,
        client_name=f"Client {external_id}",
        matter_type="Eviction",
        jurisdiction="Cleveland Housing Court",
        raw_payload={"case_number": case_number},
    )
    sync_matter_route_alias(matter)
    return matter


class ResumeTests(TestCase):
    def setUp(self):
        # This fixture has explicit locally administered all-case permission.
        self.user = get_user_model().objects.create_user("advocate", "a@example.com", "pw", is_superuser=True)
        self.client.force_login(self.user)
        self.matter = make_matter("MID-1", "26-0222")
        self.other = make_matter("MID-2", "26-0333")
        self.template = DocumentTemplate.objects.create(title="Answer", slug="answer", kind="answer", is_active=True)

    def session(self, matter=None, **fields):
        return DraftingSession.objects.create(
            mode=fields.pop("mode", "draft_from_template"),
            matter=matter or self.matter,
            template=self.template,
            **fields,
        )

    def detail(self, session, **params):
        return self.client.get(f"/api/drafting-sessions/{session.id}/", params)

    def test_new_session_resumes_at_its_goal(self):
        response = self.detail(self.session(goal="Answer the complaint"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resume"]["recommendedView"], "goal")

    def test_planned_session_resumes_at_its_plan(self):
        session = self.session(draft_plan={"documents": [{"title": "Answer"}]})
        self.assertEqual(self.detail(session).json()["resume"]["recommendedView"], "plan")

    def test_session_with_drafts_resumes_at_the_last_edited_one(self):
        session = self.session(draft_plan={"documents": [{"title": "Answer"}, {"title": "Motion"}]})
        first = DraftDocument.objects.create(session=session, title="Answer", sections=[], plain_text="a")
        second = DraftDocument.objects.create(session=session, title="Motion", sections=[], plain_text="b")
        first.plain_text = "edited"
        first.save()
        resume = self.detail(session).json()["resume"]
        self.assertEqual(resume["recommendedView"], "draft")
        self.assertEqual(resume["draftIds"], [first.id, second.id])
        self.assertEqual(resume["lastDraftId"], first.id)

    def test_running_generation_is_reconnected_to_not_restarted(self):
        session = self.session(draft_plan={"documents": [{"title": "Answer"}]})
        job = DraftGenerationJob.objects.create(session=session, status=DraftGenerationJob.RUNNING)
        resume = self.detail(session).json()["resume"]
        self.assertEqual(resume["recommendedView"], "job")
        self.assertEqual(resume["activeJobId"], job.id)
        self.assertEqual(DraftGenerationJob.objects.filter(session=session).count(), 1)

    def test_opening_a_session_changes_nothing(self):
        session = self.session(draft_plan={"documents": [{"title": "Answer"}]}, status="setup")
        before = DraftingSession.objects.get(id=session.id).updated_at
        self.detail(session, caseKey="26-0222", workspace="drafting")
        after = DraftingSession.objects.get(id=session.id)
        self.assertEqual(after.updated_at, before)
        self.assertEqual(after.status, "setup")
        self.assertFalse(DraftDocument.objects.filter(session=session).exists())
        self.assertFalse(DraftGenerationJob.objects.filter(session=session).exists())

    def test_session_on_another_case_answers_as_missing(self):
        session = self.session(matter=self.other)
        wrong_case = self.detail(session, caseKey="26-0222")
        missing = self.client.get("/api/drafting-sessions/999999/", {"caseKey": "26-0222"})
        self.assertEqual(wrong_case.status_code, 404)
        self.assertEqual(wrong_case.json(), missing.json())
        self.assertEqual(self.detail(session, caseKey="26-0333").status_code, 200)

    def test_session_is_reachable_by_an_old_case_number(self):
        session = self.session()
        self.matter.raw_payload = {"case_number": "26-0999"}
        self.matter.save()
        sync_matter_route_alias(self.matter)
        self.assertEqual(self.detail(session, caseKey="26-0222").status_code, 200)

    def test_session_from_another_workspace_answers_as_missing(self):
        letter = self.session(mode="advice_letter")
        self.assertEqual(self.detail(letter, workspace="drafting").status_code, 404)
        self.assertEqual(self.detail(letter, workspace="advice-letters").status_code, 200)

    def test_case_list_shows_only_that_cases_drafting_sessions(self):
        mine = self.session(goal="Answer")
        DraftDocument.objects.create(session=mine, title="Answer", sections=[], plain_text="a")
        self.session(mode="advice_letter")
        self.session(matter=self.other)
        response = self.client.get("/api/drafting-sessions/", {"caseKey": "26-0222"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        row = payload["sessions"][0]
        self.assertEqual(row["id"], mine.id)
        self.assertEqual(row["draftCount"], 1)
        self.assertEqual(row["routeCaseKey"], "26-0222")
        self.assertEqual(row["templateTitle"], "Answer")
        self.assertNotIn("matter", row)

    def test_case_list_pages(self):
        for index in range(3):
            self.session(goal=f"Goal {index}")
        first = self.client.get("/api/drafting-sessions/", {"caseKey": "26-0222", "limit": 2}).json()
        second = self.client.get("/api/drafting-sessions/", {"caseKey": "26-0222", "limit": 2, "offset": 2}).json()
        self.assertEqual((len(first["sessions"]), first["hasMore"]), (2, True))
        self.assertEqual((len(second["sessions"]), second["hasMore"]), (1, False))

    def test_case_list_for_an_unknown_case_is_not_found(self):
        self.assertEqual(self.client.get("/api/drafting-sessions/", {"caseKey": "26-4444"}).status_code, 404)

    def test_case_list_by_stable_matter_id(self):
        self.session()
        self.assertEqual(self.client.get("/api/drafting-sessions/", {"matterId": "MID-1"}).json()["total"], 1)

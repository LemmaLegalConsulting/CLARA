"""Malformed or unresolvable drafting request input must not surface as a 500."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.drafting.models import DraftingSession
from apps.drafting.services import create_draft
from apps.matters.models import Matter
from apps.templates_app.models import DocumentTemplate, TemplateBlock


class DraftingRequestValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("advocate", "advocate@example.com", "pw", is_staff=True)
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(source_system="Manual", raw_payload={"created_by_user_id": self.user.pk},
            external_id="LS-500",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            summary="Rent dispute.",
        )
        self.template = DocumentTemplate.objects.create(
            title="Answer", slug="answer-validation", kind="answer_counterclaims"
        )
        TemplateBlock.objects.create(
            template=self.template, key="body", label="Body", block_type="argument",
            order=10, body="Body.", required=True,
        )

    def _session(self, **kwargs):
        return DraftingSession.objects.create(mode="draft_from_template", matter=self.matter, **kwargs)

    def test_malformed_json_body_is_rejected(self):
        response = self.client.post(
            "/api/drafting-sessions/", data="{not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid JSON", response.json()["error"])

    def test_non_object_json_body_is_rejected(self):
        response = self.client.post(
            "/api/drafting-sessions/", data="[1, 2]", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.json()["error"])

    def test_unknown_template_id_returns_404(self):
        response = self.client.post(
            "/api/drafting-sessions/",
            data=json.dumps({"mode": "draft_from_template", "matterId": "LS-500", "templateId": 999999}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_non_numeric_template_id_returns_404(self):
        response = self.client.post(
            "/api/drafting-sessions/",
            data=json.dumps({"mode": "draft_from_template", "matterId": "LS-500", "templateId": "not-an-id"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_advance_with_blank_template_clears_it_instead_of_corrupting_the_row(self):
        session = self._session(template=self.template)
        response = self.client.post(
            f"/api/drafting-sessions/{session.id}/advance/",
            data=json.dumps({"status": "facts_review", "template": ""}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertIsNone(session.template_id)

    def test_advance_with_unknown_template_returns_400(self):
        session = self._session(template=self.template)
        response = self.client.post(
            f"/api/drafting-sessions/{session.id}/advance/",
            data=json.dumps({"status": "facts_review", "template": 999999}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.template_id, self.template.id)

    def test_generate_draft_without_a_template_returns_400(self):
        session = self._session()
        response = self.client.post(f"/api/drafting-sessions/{session.id}/draft/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("template", response.json()["error"].casefold())

    def test_create_draft_without_a_template_raises_value_error(self):
        with self.assertRaises(ValueError):
            create_draft(self._session())

    def test_create_draft_without_a_template_raises_even_with_selected_blocks(self):
        # compose_document dereferences context.template unconditionally, so
        # selected block keys alone are not enough to stand in for a template.
        with self.assertRaises(ValueError):
            create_draft(self._session(selected_block_keys=["body"]))

"""A draft must keep rendering from its own template after it is edited.

editor_state is the editor's own scratch space and is overwritten wholesale on
every save, so the template a document was generated from cannot live there.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.drafting.models import DraftDocument, DraftingSession
from apps.drafting.services import create_drafts_from_plan, create_or_update_plan, regenerate_draft_block
from apps.exporting.services import _draft_template
from apps.matters.models import Matter, MatterFact
from apps.templates_app.models import DocumentTemplate, TemplateBlock


class DraftTemplateIdentityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("advocate", "advocate@example.com", "pw", is_staff=True)
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(source_system="Manual", raw_payload={"created_by_user_id": self.user.pk},
            external_id="LS-200",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            summary="Rent dispute and repairs.",
        )
        MatterFact.objects.create(
            matter=self.matter, slug="rent", title="Rent", text="Disputed balance.",
            source_label="LegalServer", selected_by_default=True,
        )
        self.template_a = self._template("template-a", "Document A")
        self.template_b = self._template("template-b", "Document B")

    def _template(self, slug, title):
        template = DocumentTemplate.objects.create(title=title, slug=slug, kind="motion", is_active=True)
        TemplateBlock.objects.create(
            template=template, key=f"{slug}-body", label="Body", block_type="argument",
            order=10, body="Body text.", required=True,
        )
        return template

    def _two_document_drafts(self):
        session = DraftingSession.objects.create(
            mode="draft_from_template",
            matter=self.matter,
            selected_template_ids=[self.template_a.id, self.template_b.id],
        )
        session = create_or_update_plan(session, {"allowMultipleDocuments": True})
        drafts = create_drafts_from_plan(session)
        self.assertEqual(len(drafts), 2)
        return session, drafts

    def test_each_planned_draft_records_its_own_template(self):
        _session, drafts = self._two_document_drafts()
        self.assertEqual(
            [draft.template.slug for draft in drafts], ["template-a", "template-b"]
        )

    def test_editor_save_does_not_change_the_export_template(self):
        session, drafts = self._two_document_drafts()
        second = drafts[-1]
        # The session points at the first planned document, so a lost template
        # reference on the second draft silently exports the wrong document.
        self.assertEqual(session.template, self.template_a)

        response = self.client.patch(
            f"/api/drafts/{second.id}/",
            data=json.dumps({"editorState": {"format": "lexical_blocks", "blocks": {}}, "revision": second.revision}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        second.refresh_from_db()
        self.assertEqual(_draft_template(second), self.template_b)

    def test_block_regeneration_does_not_change_the_export_template(self):
        _session, drafts = self._two_document_drafts()
        second = drafts[-1]
        regenerate_draft_block(second, second.sections[0]["key"], "tighten this")
        second.refresh_from_db()
        self.assertEqual(_draft_template(second), self.template_b)

    def test_every_planned_draft_is_listed_for_the_session(self):
        session, drafts = self._two_document_drafts()
        response = self.client.get(f"/api/drafting-sessions/{session.id}/drafts/")
        self.assertEqual(response.status_code, 200)
        listed = response.json()["drafts"]
        self.assertEqual([item["id"] for item in listed], [draft.id for draft in drafts])
        self.assertEqual(
            [item["templateId"] for item in listed], [self.template_a.id, self.template_b.id]
        )

    def test_draft_listing_is_scoped_to_users_who_can_reach_the_case(self):
        session, _drafts = self._two_document_drafts()
        outsider = get_user_model().objects.create_user("outsider", "outsider@example.com", "pw")
        self.client.force_login(outsider)
        response = self.client.get(f"/api/drafting-sessions/{session.id}/drafts/")
        self.assertEqual(response.status_code, 404)

    def test_legacy_drafts_still_resolve_from_editor_state(self):
        session = DraftingSession.objects.create(
            mode="draft_from_template", matter=self.matter, template=self.template_a
        )
        legacy = DraftDocument.objects.create(
            session=session,
            title="Pre-migration draft",
            sections=[],
            plain_text="",
            editor_state={"format": "plain_text", "templateSlug": "template-b"},
        )
        self.assertEqual(_draft_template(legacy), self.template_b)

    def test_draft_without_any_template_reference_falls_back_to_the_session(self):
        session = DraftingSession.objects.create(
            mode="draft_from_template", matter=self.matter, template=self.template_a
        )
        orphan = DraftDocument.objects.create(
            session=session, title="No reference", sections=[], plain_text="", editor_state={}
        )
        self.assertEqual(_draft_template(orphan), self.template_a)

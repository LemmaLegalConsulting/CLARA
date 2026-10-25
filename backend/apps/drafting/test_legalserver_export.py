"""Exporting a draft also files it, unless the advocate says otherwise.

The download itself is the point of the endpoint. These tests pin down that the
LegalServer upload rides along without ever standing between the advocate and
their document.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.drafting.models import DraftDocument, DraftingSession
from apps.matters.models import LegalServerDelivery, Matter
from apps.sources.connectors.legalserver import LegalServerError


@override_settings(
    LEGALSERVER_BASE_URL="https://example.legalserver.org",
    LEGALSERVER_API_TOKEN="token",
    LEGALSERVER_ALLOW_WRITES=True,
    LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False,
)
class DraftExportDeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("quinten", "quinten@example.org", "pw", is_staff=True)
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(
            external_id="LS-1",
            client_name="Real Client",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="LegalServer",
            raw_payload={"assigned_user_email": "quinten@example.org", "matter_uuid": "1f689912-a490-4ced-a99d-a21d7a5caeb2"},
        )
        session = DraftingSession.objects.create(matter=self.matter, status="draft")
        self.draft = DraftDocument.objects.create(
            session=session,
            title="Answer and counterclaims",
            sections=[{"heading": "Answer", "text": "Defendant answers."}],
            plain_text="Defendant answers.",
        )
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    def test_the_export_uploads_by_default_and_reports_it_in_a_header(self):
        with patch("apps.sources.connectors.legalserver.LegalServerClient.upload_matter_document") as upload:
            upload.return_value = {"id": "doc-1"}
            response = self.client.get(f"/api/drafts/{self.draft.id}/export/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content)
        self.assertEqual(response["X-LegalServer-Delivery"], "saved")
        filename = f"answer-and-counterclaims-draft-{self.draft.id}.docx"
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="{filename}"')
        self.assertEqual(upload.call_args.kwargs["filename"], filename)
        self.assertEqual(upload.call_args.kwargs["extra_fields"]["name"], filename)
        delivery = LegalServerDelivery.objects.get()
        self.assertEqual(delivery.origin, "draft_export")
        self.assertEqual(delivery.scope_key, f"draft-export:draft:{self.draft.id}")

    def test_repeated_export_replaces_the_same_remote_document(self):
        with patch("apps.sources.connectors.legalserver.LegalServerClient.upload_matter_document") as upload:
            upload.return_value = {"id": "doc-1"}
            self.client.get(f"/api/drafts/{self.draft.id}/export/")
            self.client.get(f"/api/drafts/{self.draft.id}/export/")

        self.assertEqual(upload.call_count, 2)
        filename = f"answer-and-counterclaims-draft-{self.draft.id}.docx"
        self.assertEqual(upload.call_args.kwargs["replace_name"], filename)
        self.assertEqual(
            upload.call_args.kwargs["extra_fields"]["name"],
            filename,
        )
        deliveries = list(LegalServerDelivery.objects.order_by("id"))
        self.assertFalse(deliveries[0].updated_existing)
        self.assertTrue(deliveries[1].updated_existing)
        self.assertEqual(deliveries[0].scope_key, deliveries[1].scope_key)

    def test_opting_out_downloads_without_uploading(self):
        with patch("apps.sources.connectors.legalserver.LegalServerClient.upload_matter_document") as upload:
            response = self.client.get(f"/api/drafts/{self.draft.id}/export/?saveToLegalServer=0")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content)
        upload.assert_not_called()
        self.assertFalse(response.has_header("X-LegalServer-Delivery"))
        self.assertEqual(LegalServerDelivery.objects.count(), 0)

    def test_a_failed_upload_still_returns_the_document(self):
        with patch("apps.sources.connectors.legalserver.LegalServerClient.upload_matter_document") as upload:
            upload.side_effect = LegalServerError("LegalServer POST failed with status 500")
            response = self.client.get(f"/api/drafts/{self.draft.id}/export/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content)
        self.assertEqual(response["X-LegalServer-Delivery"], "failed")
        self.assertIn("Could not save", response["X-LegalServer-Delivery-Message"])
        self.assertEqual(LegalServerDelivery.objects.get().status, LegalServerDelivery.FAILED)

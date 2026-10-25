import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.matters.models import Matter, MatterFact, TriageAssessment, TriageRubric
from apps.matters.document_context import case_materials_payload
from apps.matters.serializers import matter_to_dict
from apps.matters.services import legalserver_id, upsert_matter_from_legalserver
from apps.matters.triage import load_triage_rubric_file, normalize_triage_payload, sync_triage_rubric_seeds
from apps.sources.models import UserSourceIdentity
from apps.sources.connectors.legalserver import LegalServerClient, LegalServerError


class FakeLegalServerClient:
    configured = True
    user_filter_param = "assigned_user_email"

    def __init__(self):
        self.calls = []

    def find_user(self, identifier):
        return {"email": identifier}

    def search_matters(self, *, query="", user_email="", limit=50):
        self.calls.append({"query": query, "user_email": user_email, "limit": limit})
        return [
            {
                "id": "LS-REAL-1",
                "client_name": "Real Client",
                "matter_type": "Eviction defense",
                "court": "Housing Court",
                "assignments": [{"user": {"user_name": "quinten"}}],
            }
        ]

    def get_matter(self, matter_id):
        return {
            "id": matter_id,
            "client_name": "Direct Match",
            "matter_type": "Conditions",
            "court": "Housing Court",
        }


@override_settings(ENABLE_DEMO_MATTERS=False)
class CaseConnectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="quinten@lemmalegal.com",
            email="quinten@lemmalegal.com",
            password="password",
        )
        self.client.force_login(self.user)

    @patch("apps.matters.services.LegalServerClient")
    def test_unconnected_user_sees_no_cases_instead_of_demo_seed(self, client_class):
        fake_client = FakeLegalServerClient()
        client_class.return_value = fake_client

        response = self.client.get("/api/cases/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["cases"], [])
        self.assertFalse(payload["legalserver"]["connected"])
        self.assertEqual(payload["legalserver"]["syncError"], "not_connected")
        self.assertEqual(payload["legalserver"]["suggestedIdentifier"], "quinten@lemmalegal.com")
        self.assertFalse(Matter.objects.exists())
        self.assertEqual(fake_client.calls, [])

    @patch("apps.matters.services.LegalServerClient")
    def test_unconnected_user_sees_their_manual_cases(self, client_class):
        fake_client = FakeLegalServerClient()
        client_class.return_value = fake_client
        manual = Matter.objects.create(
            external_id="MANUAL-1",
            client_name="Manual Client",
            matter_type="Eviction defense",
            jurisdiction="Housing Court",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )

        response = self.client.get("/api/cases/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([case["id"] for case in payload["cases"]], [manual.external_id])
        self.assertEqual(payload["cases"][0]["sourceSystem"], "Manual")
        self.assertEqual(fake_client.calls, [])

    @patch("apps.matters.services.LegalServerClient")
    def test_connected_user_filters_legalserver_by_saved_identifier(self, client_class):
        fake_client = FakeLegalServerClient()
        client_class.return_value = fake_client
        UserSourceIdentity.objects.create(is_verified=True,
            user=self.user,
            provider="legalserver",
            identifier="quinten@lemmalegal.com",
        )

        response = self.client.get("/api/cases/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["cases"][0]["id"], "LS-REAL-1")
        self.assertTrue(payload["legalserver"]["connected"])
        self.assertEqual(fake_client.calls[0]["user_email"], "")

    @patch("apps.matters.services.LegalServerClient")
    def test_case_search_does_not_limit_to_primary_assignment(self, client_class):
        fake_client = FakeLegalServerClient()
        client_class.return_value = fake_client
        UserSourceIdentity.objects.create(is_verified=True,
            user=self.user,
            provider="legalserver",
            identifier="quinten@lemmalegal.com",
        )

        response = self.client.get("/api/cases/?q=Acme")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cases"][0]["id"], "LS-REAL-1")
        self.assertEqual(fake_client.calls[0]["query"], "Acme")
        self.assertEqual(fake_client.calls[0]["user_email"], "")

    def test_legalserver_identifier_prefers_human_case_number_over_guid(self):
        payload = {
            "id": "d019be06-6d12-47a5-bdfb-2a8a6f71d9ac",
            "matter_uuid": "e11620a6-8b6f-4f40-95a5-250511d9ecf3",
            "case_number": "25-000085",
            "client_name": "Real Client",
            "matter_type": "Eviction defense",
            "court": "Housing Court",
        }

        self.assertEqual(legalserver_id(payload), "25-000085")

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTER_PROFILE_PATH="/matter/dynamic-profile/view/{matter_id}",
    )
    def test_case_detail_includes_title_basic_information_and_legalserver_url(self):
        matter = Matter.objects.create(
            external_id="26-000034",
            client_name="Jordan Tenant",
            matter_type="Eviction defense",
            jurisdiction="Housing Court",
            posture="Open",
            source_system="LegalServer",
            raw_payload={
                "id": "d019be06-6d12-47a5-bdfb-2a8a6f71d9ac",
                "case_title": "Jordan Tenant v. Acme Properties",
                "date_opened": "2026-07-15",
            },
        )

        payload = matter_to_dict(matter, legalserver_client=LegalServerClient())

        self.assertEqual(payload["title"], "Jordan Tenant v. Acme Properties")
        self.assertEqual(payload["legalserverUrl"], "https://example.legalserver.org/matter/dynamic-profile/view/34")
        details = {item["label"]: item["value"] for item in payload["details"]}
        self.assertEqual(details["Client or household"], "Jordan Tenant")
        self.assertEqual(details["Opened"], "2026-07-15")

    def test_case_materials_refreshes_legalserver_notes_and_tries_stable_document_ids(self):
        matter = Matter.objects.create(
            external_id="26-000035",
            client_name="Case Materials Client",
            matter_type="Conditions",
            jurisdiction="Housing Court",
            source_system="LegalServer",
            raw_payload={"matter_uuid": "matter-uuid-35", "case_number": "26-000035"},
        )

        class MaterialsClient:
            configured = True

            def get_matter(self, identifier):
                if identifier == "26-000035":
                    raise LegalServerError("Case-number lookup is unavailable")
                return {
                    "id": 35,
                    "matter_uuid": "matter-uuid-35",
                    "case_number": "26-000035",
                    "notes": [{"id": "note-35", "subject": "Status update", "body": "The hearing is August 10."}],
                }

            def get_matter_documents(self, identifier):
                if identifier != "matter-uuid-35":
                    return []
                return [{"id": "doc-35", "filename": "Notice.pdf", "download_url": "https://files.example/notice.pdf"}]

            def get_matter_notes(self, identifier):
                return []

        payload = case_materials_payload(matter, client=MaterialsClient())

        self.assertEqual(payload["summary"]["noteCount"], 1)
        self.assertEqual(payload["summary"]["documentCount"], 1)
        self.assertEqual(payload["notes"][0]["title"], "Status update")
        self.assertEqual(payload["documents"][0]["title"], "Notice.pdf")
        matter.refresh_from_db()
        self.assertEqual(matter.raw_payload["notes"][0]["id"], "note-35")

    def test_case_materials_can_force_refresh_cached_legalserver_notes(self):
        matter_uuid = "d019be06-6d12-47a5-bdfb-2a8a6f71d9ac"
        matter = Matter.objects.create(
            external_id="26-000036",
            client_name="Cached Notes Client",
            matter_type="Conditions",
            source_system="LegalServer",
            raw_payload={
                "matter_uuid": matter_uuid,
                "case_number": "26-000036",
                "notes": [{"id": "old", "subject": "Old note", "body": "Old body"}],
            },
        )

        class RefreshingClient:
            configured = True

            def get_matter(self, _identifier):
                return {"id": 36, "matter_uuid": matter_uuid, "case_number": "26-000036"}

            def get_matter_notes(self, _identifier):
                return [{"id": "new", "subject": "New note", "body": "New body"}]

            def get_matter_documents(self, _identifier):
                return []

        cached = case_materials_payload(matter, client=RefreshingClient())
        refreshed = case_materials_payload(matter, client=RefreshingClient(), force_refresh=True)

        self.assertEqual([note["title"] for note in cached["notes"]], ["Old note"])
        self.assertEqual([note["title"] for note in refreshed["notes"]], ["New note"])

    def _rate_limited_matter(self, number):
        return Matter.objects.create(
            external_id=number,
            client_name="Rate Limited Client",
            matter_type="Conditions",
            source_system="LegalServer",
            raw_payload={
                "matter_uuid": "72d4848e-7187-11f1-bb35-0a2523f9a7d8",
                "case_number": number,
                "notes": [{"id": "n", "subject": "Intake", "body": "Intake note."}],
            },
        )

    def test_a_legalserver_failure_is_reported_not_read_as_an_empty_case_file(self):
        # A 429 from LegalServer used to become documentCount 0 and "No case
        # documents were returned" for a case holding twenty-two.
        matter = self._rate_limited_matter("26-000040")
        asked = []

        class RateLimitedClient:
            configured = True

            def get_matter_documents(self, identifier):
                asked.append(identifier)
                raise LegalServerError("LegalServer request failed with status 429: Request rate limit exceeded")

        with self.assertLogs("apps.matters.document_context", level="WARNING"):
            payload = case_materials_payload(matter, client=RateLimitedClient())

        self.assertEqual(payload["summary"]["documentCount"], 0)
        self.assertIn("429", payload["documentsUnavailable"])
        # The UUID is the identifier the documents API answers to, so it goes first.
        self.assertEqual(asked[0], "72d4848e-7187-11f1-bb35-0a2523f9a7d8")

    def test_a_case_with_no_documents_is_not_reported_as_a_failure(self):
        matter = self._rate_limited_matter("26-000041")

        class EmptyClient:
            configured = True

            def get_matter_documents(self, identifier):
                if identifier == "26-000041":
                    raise LegalServerError("LegalServer request failed with status 404: Matter not found")
                return []

        payload = case_materials_payload(matter, client=EmptyClient())

        self.assertEqual(payload["summary"]["documentCount"], 0)
        self.assertEqual(payload["documentsUnavailable"], "")

    @override_settings(LEGALSERVER_DOCUMENT_CACHE_SECONDS=60)
    def test_a_failed_fetch_falls_back_to_the_last_list_and_says_so(self):
        from django.core.cache import cache

        cache.clear()
        matter = self._rate_limited_matter("26-000042")

        class FlakyClient:
            configured = True
            calls = 0

            def get_matter(self, _identifier):
                return dict(matter.raw_payload)

            def get_matter_notes(self, _identifier):
                return []

            def get_matter_documents(self, identifier):
                FlakyClient.calls += 1
                if FlakyClient.calls > 1:
                    raise LegalServerError("LegalServer request failed with status 429: Request rate limit exceeded")
                return [{"id": "doc", "filename": "Notice.pdf", "download_url": "https://files.example/n.pdf"}]

        first = case_materials_payload(matter, client=FlakyClient())
        cached = case_materials_payload(matter, client=FlakyClient())
        self.assertEqual(FlakyClient.calls, 1, "a second load within the TTL must not ask LegalServer again")
        self.assertEqual(cached["summary"]["documentCount"], 1)

        with self.assertLogs("apps.matters.document_context", level="WARNING"):
            refreshed = case_materials_payload(matter, client=FlakyClient(), force_refresh=True)
        self.assertEqual(first["documentsUnavailable"], "")
        self.assertEqual(refreshed["summary"]["documentCount"], 1)
        self.assertIn("earlier load", refreshed["documentsUnavailable"])
        cache.clear()

    def test_legalserver_upsert_renames_existing_guid_keyed_matter_to_case_number(self):
        existing = Matter.objects.create(
            external_id="d019be06-6d12-47a5-bdfb-2a8a6f71d9ac",
            client_name="Old Client",
            matter_type="Old type",
            jurisdiction="",
        )
        payload = {
            "id": existing.external_id,
            "case_number": "25-000085",
            "client_name": "Real Client",
            "matter_type": "Eviction defense",
            "court": "Housing Court",
        }

        matter = upsert_matter_from_legalserver(payload)

        self.assertEqual(matter.id, existing.id)
        self.assertEqual(matter.external_id, "25-000085")
        self.assertEqual(matter.client_name, "Real Client")
        self.assertEqual(Matter.objects.count(), 1)

    def test_user_cannot_remap_legalserver_identity_without_admin(self):
        response = self.client.post(
            "/api/legalserver/account/",
            data=json.dumps({"identifier": "quinten@lemmalegal.com"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserSourceIdentity.objects.filter(user=self.user, provider="legalserver").exists())

    def test_case_document_context_summarizes_and_searches_case_notes(self):
        matter = Matter.objects.create(
            external_id="LS-DOC-1",
            client_name="Document Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            source_system="Manual",
            summary="Case summary adds that the hearing is next week.",
            raw_payload={
                "created_by_user_id": self.user.id,
                "case_notes": [
                    "Tenant has a disability and asked for more time to gather records. Landlord received the request.",
                    "Tenant paid April rent by money order.",
                ]
            },
        )

        list_response = self.client.get(f"/api/cases/{matter.external_id}/documents/")

        self.assertEqual(list_response.status_code, 200)
        documents = list_response.json()["documents"]
        self.assertEqual(documents[0]["kind"], "case_note")
        self.assertEqual(documents[-1]["title"], "Case summary")

        context_response = self.client.post(
            f"/api/cases/{matter.external_id}/documents/{documents[0]['id']}/context/",
            data=json.dumps({"level": "search", "query": "disability records"}),
            content_type="application/json",
        )

        self.assertEqual(context_response.status_code, 200)
        payload = context_response.json()
        self.assertIn("disability", payload["summary"])
        self.assertEqual(payload["chunks"][0]["index"], 1)

    @override_settings(FRAME_ANCESTORS=["https://app.example.org"])
    @patch("apps.matters.document_context.LegalServerClient")
    def test_case_document_file_streams_an_authenticated_pdf_inline(self, client_class):
        legalserver = client_class.return_value
        legalserver.configured = False
        legalserver.download_document.return_value = {
            "content": b"%PDF-1.7 preview bytes",
            "content_type": "application/pdf",
            "filename": "notice.pdf",
        }
        matter = Matter.objects.create(
            external_id="MANUAL-PREVIEW-1",
            client_name="Preview Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            source_system="Manual",
            raw_payload={
                "created_by_user_id": self.user.id,
                "documents": [
                    {
                        "id": "pdf-1",
                        "filename": "Notice of Hearing.pdf",
                        "download_url": "https://files.example/notice.pdf",
                        "mime_type": "application/pdf",
                    }
                ],
            },
        )
        documents = self.client.get(f"/api/cases/{matter.external_id}/documents/").json()["documents"]

        response = self.client.get(
            f"/api/cases/{matter.external_id}/documents/{documents[0]['id']}/file/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.7 preview bytes")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertIn("Notice of Hearing.pdf", response["Content-Disposition"])
        self.assertEqual(response["Content-Security-Policy"], "frame-ancestors 'self' https://app.example.org")
        # SAMEORIGIN names this API's origin, not the app's, so the browser
        # refused to display a PDF the app had already fetched.
        self.assertIsNone(response.headers.get("X-Frame-Options"))

    def test_case_fact_recommendations_select_relevant_and_default_facts(self):
        matter = Matter.objects.create(
            external_id="LS-FACT-1",
            client_name="Fact Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            summary="Tenant disputes rent and reported mold repairs.",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )
        rent = MatterFact.objects.create(
            matter=matter,
            slug="rent-dispute",
            title="Rent dispute",
            text="Tenant disputes rent.",
            source_label="LegalServer",
            selected_by_default=False,
        )
        default = MatterFact.objects.create(
            matter=matter,
            slug="default-note",
            title="Default note",
            text="Selected by default.",
            source_label="LegalServer",
            selected_by_default=True,
        )
        MatterFact.objects.create(
            matter=matter,
            slug="unrelated",
            title="Unrelated",
            text="Not relevant.",
            source_label="LegalServer",
            selected_by_default=False,
        )

        response = self.client.post(f"/api/cases/{matter.external_id}/facts/recommend/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()["factIds"]), {rent.id, default.id})

    def test_user_can_add_typed_case_fact(self):
        matter = Matter.objects.create(
            external_id="LS-FACT-2",
            client_name="Fact Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )

        response = self.client.post(
            f"/api/cases/{matter.external_id}/facts/",
            data=json.dumps({
                "title": "New payment",
                "text": "Client paid $500 after the ledger was printed.",
                "source": "Client call",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        created = response.json()["created"][0]
        self.assertEqual(created["title"], "New payment")
        self.assertEqual(created["source"], "Client call")
        self.assertTrue(MatterFact.objects.filter(matter=matter, slug="new-payment").exists())

    def test_user_can_add_case_fact_from_uploaded_text_document(self):
        matter = Matter.objects.create(
            external_id="LS-FACT-3",
            client_name="Fact Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )

        response = self.client.post(
            f"/api/cases/{matter.external_id}/facts/",
            data={
                "title": "Uploaded repairs",
                "file": SimpleUploadedFile(
                    "repairs.txt",
                    b"Tenant texted landlord about no heat on January 5.",
                    content_type="text/plain",
                ),
            },
        )

        self.assertEqual(response.status_code, 201)
        created = response.json()["created"][0]
        self.assertEqual(created["title"], "Uploaded repairs")
        self.assertIn("no heat", created["text"])

    def test_triage_rubrics_seeds_default_cleveland_rtc_standard(self):
        TriageRubric.objects.all().delete()

        response = self.client.get("/api/triage/rubrics/")

        self.assertEqual(response.status_code, 200)
        rubrics = response.json()["rubrics"]
        self.assertEqual(rubrics[0]["slug"], "cleveland-rtc-priority")
        self.assertIn("Cleveland", rubrics[0]["standard"])

    def test_triage_rubric_file_seed_preserves_existing_admin_record(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            rubric_dir = library / "triage-rubrics"
            rubric_dir.mkdir()
            (rubric_dir / "file-backed.yaml").write_text(
                """slug: file-backed\nname: File backed\nstandard: Classify it.\ncriteria:\n  - First criterion\n"""
            )
            with override_settings(CONTENT_LIBRARY_DIR=library):
                synced = sync_triage_rubric_seeds()
                self.assertTrue(synced[0][1])
                rubric = TriageRubric.objects.get(slug="file-backed")
                rubric.name = "Admin managed name"
                rubric.save(update_fields=["name"])
                sync_triage_rubric_seeds()
                rubric.refresh_from_db()
                self.assertEqual(rubric.name, "Admin managed name")

    def test_triage_rubric_file_requires_string_criteria(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.yaml"
            path.write_text("slug: invalid\nname: Invalid\nstandard: Nope\ncriteria: invalid\n")
            with self.assertRaises(ValueError):
                load_triage_rubric_file(path)

    def test_triage_normalization_accepts_list_like_summary_fields(self):
        fallback = {
            "case_type": "Eviction",
            "priority": False,
            "priority_label": "needs_review",
            "confidence": "low",
            "summary": "Fallback summary",
            "reasoning": "Fallback reasoning",
            "matched_criteria": [],
            "missing_information": [],
            "evidence": [],
        }
        payload = {
            "summary": [
                "- Tenant is facing an eviction action.",
                "- Hearing date is documented.",
            ],
            "reasoning": "['- Mold repairs are unresolved.', '- Rent is saved.']",
            "matched_criteria": "['- Eviction risk', '- Vulnerability']",
            "missing_information": "- Confirm rent ledger\n- Confirm notice date",
        }

        normalized = normalize_triage_payload(payload, fallback)

        self.assertEqual(
            normalized["summary"],
            "- Tenant is facing an eviction action.\n- Hearing date is documented.",
        )
        self.assertEqual(
            normalized["reasoning"],
            "- Mold repairs are unresolved.\n- Rent is saved.",
        )
        self.assertEqual(normalized["matched_criteria"], ["Eviction risk", "Vulnerability"])
        self.assertEqual(normalized["missing_information"], ["Confirm rent ledger", "Confirm notice date"])

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_user_can_run_triage_for_manual_case_with_fallback(self):
        matter = Matter.objects.create(
            external_id="MANUAL-TRIAGE-1",
            client_name="Triage Client",
            matter_type="Eviction defense",
            jurisdiction="Cleveland Municipal Court Housing Division",
            summary="Tenant has an eviction hearing tomorrow and has two children in the home.",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )
        MatterFact.objects.create(
            matter=matter,
            slug="voucher-risk",
            title="Voucher risk",
            text="Client has a Section 8 voucher and received a notice to leave.",
            source_label="Intake",
        )

        response = self.client.post(
            f"/api/cases/{matter.external_id}/triage/",
            data=json.dumps({"rubricSlug": "cleveland-rtc-priority"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        assessment = response.json()["assessment"]
        self.assertTrue(assessment["priority"])
        self.assertEqual(assessment["priorityLabel"], "priority_full_representation")
        self.assertEqual(assessment["rubric"]["slug"], "cleveland-rtc-priority")
        self.assertEqual(TriageAssessment.objects.get().created_by, self.user)

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_user_can_list_prior_triage_assessments(self):
        matter = Matter.objects.create(
            external_id="MANUAL-TRIAGE-2",
            client_name="Triage Client",
            matter_type="Advice",
            jurisdiction="Cuyahoga County",
            summary="Client asks about repairs.",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )
        self.client.post(f"/api/cases/{matter.external_id}/triage/")

        response = self.client.get(f"/api/cases/{matter.external_id}/triage/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["assessments"]), 1)

    @patch("apps.matters.services.LegalServerClient")
    def test_user_can_create_manual_case_with_notes_and_multiple_files(self, client_class):
        client_class.return_value = FakeLegalServerClient()

        response = self.client.post(
            "/api/cases/",
            data={
                "clientName": "Local Client",
                "matterType": "Eviction defense",
                "jurisdiction": "Cleveland Housing Court",
                "posture": "Pre-filing intake",
                "notes": "Client says the landlord refused repairs before filing.",
                "files": [
                    SimpleUploadedFile(
                        "notice.txt",
                        b"Three-day notice was posted on the door.",
                        content_type="text/plain",
                    ),
                    SimpleUploadedFile(
                        "ledger.txt",
                        b"Ledger does not credit the February money order.",
                        content_type="text/plain",
                    ),
                ],
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["case"]["client"], "Local Client")
        self.assertEqual(payload["case"]["sourceSystem"], "Manual")
        self.assertEqual(len(payload["created"]), 3)
        matter = Matter.objects.get(external_id=payload["case"]["id"])
        self.assertEqual(matter.raw_payload["created_by_user_id"], self.user.id)
        self.assertEqual(matter.facts.count(), 3)

        detail_response = self.client.get(f"/api/cases/{matter.external_id}/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(len(detail_response.json()["case"]["facts"]), 3)

    def test_user_can_edit_own_manual_case_and_preview_legalserver_intake(self):
        matter = Matter.objects.create(
            external_id="MANUAL-EDIT-1",
            client_name="Old Client",
            matter_type="Old type",
            jurisdiction="Old court",
            posture="Old posture",
            summary="Old summary",
            source_system="Manual",
            raw_payload={"created_by_user_id": self.user.id},
        )

        response = self.client.patch(
            f"/api/cases/{matter.external_id}/",
            data=json.dumps(
                {
                    "clientName": "Quick Client",
                    "matterType": "Eviction defense",
                    "jurisdiction": "Cleveland Housing Court",
                    "posture": "Pre-hearing",
                    "summary": "Updated quick case notes.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["case"]
        self.assertEqual(payload["client"], "Quick Client")
        self.assertEqual(payload["summary"], "Updated quick case notes.")

        preview_response = self.client.post(
            f"/api/cases/{matter.external_id}/",
            data=json.dumps({"action": "legalserver_draft_intake"}),
            content_type="application/json",
        )

        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()["legalserverDraftIntake"]
        self.assertFalse(preview["posted"])
        self.assertEqual(preview["payload"]["client_name"], "Quick Client")

    def test_case_materials_groups_notes_documents_custom_fields_and_facts(self):
        matter = Matter.objects.create(
            external_id="MANUAL-MATERIALS-1",
            client_name="Materials Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            summary="",
            source_system="Manual",
            raw_payload={
                "created_by_user_id": self.user.id,
                "notes": [
                    {
                        "id": "note-1",
                        "subject": "Documents Received",
                        "body": "Documents received via webhook.",
                        "note_has_document_attached": True,
                        "attachments": [
                            {"id": "doc-1", "filename": "Rent Ledger.pdf", "download_url": "https://example.test/ledger.pdf"}
                        ],
                    }
                ],
                "documents": [{"id": "doc-2", "filename": "Lease.pdf", "body": "Lease text"}],
                "custom_fields": {
                    "case_narrative": "Client reports a long timeline about rent, notice, and repairs.",
                    "internal_code": "ABC",
                },
            },
        )
        MatterFact.objects.create(
            matter=matter,
            slug="selected-fact",
            title="Selected fact",
            text="Client paid rent.",
            source_label="Case note",
        )

        response = self.client.get(f"/api/cases/{matter.external_id}/materials/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"], {"noteCount": 1, "documentCount": 1, "customFieldCount": 2, "draftingFactCount": 1})
        self.assertTrue(payload["notes"][0]["isWebhookDocumentNotice"])
        self.assertEqual(payload["notes"][0]["text"], "Documents received via webhook.")
        self.assertEqual(payload["notes"][0]["attachedDocuments"][0]["filename"], "Rent Ledger.pdf")
        self.assertEqual(payload["customFields"][0]["key"], "case_narrative")
        self.assertEqual(payload["customFields"][0]["confidence"], "likely_useful")

    def test_custom_field_fetch_caches_normalized_fields(self):
        matter = Matter.objects.create(
            external_id="MANUAL-FIELDS-1",
            client_name="Field Client",
            matter_type="Eviction",
            jurisdiction="Housing Court",
            source_system="Manual",
            raw_payload={
                "created_by_user_id": self.user.id,
                "custom_fields": {"housing_conditions_summary": "Mold and leaks in the apartment."},
            },
        )

        response = self.client.post(
            f"/api/cases/{matter.external_id}/custom-fields/fetch/",
            data=json.dumps({"fieldKeys": ["housing_conditions_summary"], "reason": "Need conditions details."}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fields"][0]["key"], "housing_conditions_summary")
        matter.refresh_from_db()
        self.assertIn("custom_fields_normalized", matter.raw_payload)

    def test_cannot_create_empty_manual_case(self):
        response = self.client.post(
            "/api/cases/",
            data=json.dumps({"clientName": "Blank Client"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Matter.objects.filter(client_name="Blank Client").exists())

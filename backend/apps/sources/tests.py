import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings

from apps.matters.models import Matter
from apps.sources.connectors.base import SourceResult
from apps.sources.connectors.legalserver import LegalServerClient, LegalServerConnector, LegalServerError, matter_payload_to_defaults
from apps.sources.connectors.sharepoint import SharePointClient, SharePointConnector, graph_token_for_request
from apps.sources.connectors.user_resources import UserResourceConnector
from apps.sources.connectors.rag import ContentLibraryTreatiseConnector
from apps.sources.library import library_documents, load_manifest
from apps.sources.ordinance_storage import store_upload
from apps.sources.ordinances import (
    coverage as ordinance_coverage,
    cross_references,
    dataset as ordinance_dataset,
)
from apps.sources.models import (
    OrdinanceDocument,
    OrdinanceOverride,
    SourceConfiguration,
    UserOAuthConnection,
    UserResource,
)
from apps.sources import jurisdiction
from apps.sources.augmentation import augmented_search, evaluate_search_results
from apps.sources.registry import ConnectorRegistry
from apps.sources.selection import automatic_source_selection


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, text=""):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.text = text
        self.content = payload if isinstance(payload, bytes) else b""

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, payload, status_code=200, headers=None, text=""):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers
        self.text = text
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return FakeResponse(self.payload, status_code=self.status_code, headers=self.headers, text=self.text)


class LegalServerClientTests(TestCase):
    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
    )
    def test_document_download_resolves_relative_legalserver_url(self):
        session = FakeSession(b"document bytes", headers={"content-type": "application/pdf"})
        client = LegalServerClient(session=session)

        result = client.download_document("/modules/document/download.php?id=7")

        self.assertEqual(session.calls[0]["url"], "https://example.legalserver.org/modules/document/download.php?id=7")
        self.assertEqual(result["content"], b"document bytes")

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
    )
    def test_unreachable_site_raises_the_error_callers_already_handle(self):
        """A site that does not answer must look like every other LegalServer failure.

        `requests` raises its own exception type for a timeout or a refused
        connection, so without this the callers that catch LegalServerError --
        the case list, the account status -- let it through as an unhandled
        500 on the very endpoints an advocate hits first.
        """

        class UnreachableSession:
            def get(self, *args, **kwargs):
                raise requests.ConnectTimeout("connection timed out")

            def request(self, *args, **kwargs):
                raise requests.ConnectionError("name resolution failed")

        client = LegalServerClient(session=UnreachableSession())

        with self.assertRaises(LegalServerError):
            client.search_matters(limit=5)
        with self.assertRaises(LegalServerError):
            client.download_document("/modules/document/download.php?id=7")

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
    )
    def test_document_download_rejects_foreign_host_before_sending_credentials(self):
        session = FakeSession(b"document bytes")
        client = LegalServerClient(session=session)

        with self.assertRaises(LegalServerError):
            client.download_document("https://attacker.example/client-file")

        self.assertEqual(session.calls, [])

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTER_PROFILE_PATH="/matter/dynamic-profile/view/{matter_id}",
    )
    def test_matter_profile_url_uses_database_id_on_configured_site(self):
        client = LegalServerClient(session=FakeSession({}))

        url = client.matter_profile_url({"id": 34, "case_number": "26-000034"})

        self.assertEqual(url, "https://example.legalserver.org/matter/dynamic-profile/view/34")

    @override_settings(
        LEGALSERVER_BASE_URL="https://lemma-demo.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTER_PROFILE_PATH="/matter/dynamic-profile/view/{matter_id}",
    )
    def test_matter_profile_url_derives_sequential_id_from_case_number(self):
        client = LegalServerClient(session=FakeSession({}))

        url = client.matter_profile_url(
            {
                "id": "d019be06-6d12-47a5-bdfb-2a8a6f71d9ac",
                "case_number": "26-0000085",
            }
        )

        self.assertEqual(url, "https://lemma-demo.legalserver.org/matter/dynamic-profile/view/85")

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTER_PROFILE_PATH="/matter/dynamic-profile/view/{matter_id}",
    )
    def test_matter_profile_url_rejects_foreign_payload_url(self):
        client = LegalServerClient(session=FakeSession({}))

        url = client.matter_profile_url({"id": 34, "web_url": "https://malicious.example/matter/34"})

        self.assertEqual(url, "https://example.legalserver.org/matter/dynamic-profile/view/34")

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTERS_PATH="/api/v2/matters",
        LEGALSERVER_MATTERS_RESULTS="full",
    )
    def test_search_matters_uses_v2_full_results_shape(self):
        session = FakeSession({"results": [{"id": "123"}]})
        client = LegalServerClient(session=session)

        matters = client.search_matters(query="repair", user_email="advocate@example.org", limit=7)

        self.assertEqual(matters, [{"id": "123"}])
        call = session.calls[0]
        self.assertEqual(call["url"], "https://example.legalserver.org/api/v2/matters")
        self.assertEqual(call["headers"]["Authorization"], "Bearer token")
        self.assertEqual(call["params"]["page_size"], 7)
        searched_fields = {
            next(key for key in call["params"] if key not in ("page_size", "results")) for call in session.calls
        }
        self.assertEqual(searched_fields, {"case_number", "case_title", "external_id", "first", "last"})
        self.assertTrue(all(call["params"]["results"] == "full" for call in session.calls))
        self.assertTrue(all("caseworker" not in call["params"] for call in session.calls))

    def test_matter_payload_normalization_accepts_common_field_names(self):
        defaults = matter_payload_to_defaults(
            {
                "client": "Jane Tenant",
                "case_type": "Eviction",
                "court": "Housing Court",
                "case_summary": "Nonpayment case with repair issues.",
            }
        )

        self.assertEqual(defaults["client_name"], "Jane Tenant")
        self.assertEqual(defaults["matter_type"], "Eviction")
        self.assertEqual(defaults["jurisdiction"], "Housing Court")
        self.assertIn("repair", defaults["summary"])

    @override_settings(
        LEGALSERVER_BASE_URL="https://env.legalserver.org",
        LEGALSERVER_API_TOKEN="env-token",
        LEGALSERVER_MATTERS_PATH="/env/matters",
        LEGALSERVER_MATTERS_RESULTS="full",
        LEGALSERVER_USER_FILTER_PARAM="env_user",
    )
    def test_admin_source_configuration_overrides_legalserver_env_defaults(self):
        SourceConfiguration.objects.create(
            name="LegalServer",
            kind="legalserver",
            legalserver_base_url="https://admin.legalserver.org",
            legalserver_api_token="admin-token",
            legalserver_matters_path="/admin/matters",
            legalserver_user_filter_param="admin_user",
        )
        session = FakeSession({"results": []})
        client = LegalServerClient(session=session)

        client.search_matters(user_email="advocate@example.org")

        call = session.calls[0]
        self.assertEqual(call["url"], "https://admin.legalserver.org/admin/matters")
        self.assertEqual(call["headers"]["Authorization"], "Bearer admin-token")
        self.assertEqual(call["params"], {"page_size": 25, "results": "full"})

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTERS_PATH="/api/v2/matters",
    )
    def test_error_includes_legalserver_response_detail(self):
        session = FakeSession({"detail": "Unknown query parameter assigned_user_email"}, status_code=400)
        client = LegalServerClient(session=session)

        with self.assertRaises(LegalServerError) as context:
            client.search_matters(user_email="advocate@example.org")

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("Unknown query parameter assigned_user_email", str(context.exception))

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTERS_PATH="/api/v2/matters",
        LEGALSERVER_MATTERS_RESULTS="full",
        LEGALSERVER_USER_FILTER_PARAM="",
    )
    def test_user_email_is_not_sent_as_a_matter_search_filter(self):
        session = FakeSession({"results": []})
        client = LegalServerClient(session=session)

        client.search_matters(user_email="advocate@example.org")

        self.assertEqual(session.calls[0]["params"], {"page_size": 25, "results": "full"})

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTERS_PATH="/api/v2/matters",
        LEGALSERVER_MATTERS_RESULTS="full",
    )
    def test_case_number_search_makes_one_exact_api_request(self):
        session = FakeSession({"data": [{"matter_uuid": "matter-1", "case_number": "26-0000045"}]})
        client = LegalServerClient(session=session)

        matters = client.search_matters(query="26-0000045")

        self.assertEqual(len(matters), 1)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["params"]["case_number"], "26-0000045")

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTERS_PATH="/api/v2/matters",
        LEGALSERVER_DOCUMENTS_PATH="/api/v2/documents",
    )
    def test_v2_detail_and_matter_documents_unwrap_data_envelopes(self):
        matter_uuid = "d019be06-6d12-47a5-bdfb-2a8a6f71d9ac"
        session = FakeSession({"data": {"matter_uuid": matter_uuid, "case_number": "26-000035"}})
        client = LegalServerClient(session=session)

        matter = client.get_matter(matter_uuid)

        self.assertEqual(matter["case_number"], "26-000035")
        session.payload = {"data": [{"uuid": "doc-uuid", "title": "Notice.pdf"}]}
        documents = client.get_matter_documents(matter_uuid)
        self.assertEqual(documents[0]["title"], "Notice.pdf")
        self.assertEqual(session.calls[-1]["url"], "https://example.legalserver.org/api/v2/documents")
        self.assertEqual(
            session.calls[-1]["params"],
            {"module": "matter", "module_uuid": matter_uuid, "page_size": 100},
        )

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_MATTERS_PATH="/api/v2/matters",
    )
    def test_matter_notes_use_the_v2_uuid_scoped_endpoint(self):
        matter_uuid = "d019be06-6d12-47a5-bdfb-2a8a6f71d9ac"
        session = FakeSession({"data": [{"casenote_uuid": "note-1", "subject": "Intake"}]})
        client = LegalServerClient(session=session)

        notes = client.get_matter_notes(matter_uuid)

        self.assertEqual(notes[0]["subject"], "Intake")
        self.assertEqual(
            session.calls[0]["url"],
            f"https://example.legalserver.org/api/v2/matters/{matter_uuid}/notes",
        )


class LegalServerConnectorTests(TestCase):
    def test_connector_returns_case_documents_for_selected_matter(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Housing Court",
        )
        client = SimpleNamespace(
            configured=True,
            get_matter_documents=lambda matter_id: [
                {
                    "id": "doc-1",
                    "title": "Notice to Quit",
                    "storage_provider": "LegalServer",
                    "download_url": "https://files.example/doc-1",
                }
            ],
        )
        connector = LegalServerConnector(client=client)

        results = connector.search("notice", matter=matter)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Notice to Quit")
        self.assertEqual(results[0].metadata["storage"], "LegalServer")


class SharePointClientTests(TestCase):
    @override_settings(SHAREPOINT_SITE_ID="site", SHAREPOINT_DRIVE_ID="drive", SHAREPOINT_ACCESS_TOKEN="token")
    def test_search_drive_uses_graph_with_bearer_token(self):
        session = FakeSession({"value": [{"id": "item-1", "name": "Guide.docx"}]})
        client = SharePointClient(session=session)

        items = client.search_drive("habitability", limit=3)

        self.assertEqual(items[0]["name"], "Guide.docx")
        call = session.calls[0]
        self.assertIn("graph.microsoft.com/v1.0/sites/site/drives/drive/root/search", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer token")
        self.assertEqual(call["params"]["$top"], 3)

    def test_graph_token_prefers_django_session(self):
        request = RequestFactory().get("/")
        request.session = {"ms_graph_access_token": "delegated"}

        self.assertEqual(graph_token_for_request(request), "delegated")

    @override_settings(SHAREPOINT_SITE_ID="env-site", SHAREPOINT_DRIVE_ID="env-drive", SHAREPOINT_ACCESS_TOKEN="env-token")
    def test_admin_source_configuration_overrides_sharepoint_env_defaults(self):
        SourceConfiguration.objects.create(
            name="SharePoint",
            kind="sharepoint",
            sharepoint_site_id="admin-site",
            sharepoint_drive_id="admin-drive",
            sharepoint_server_access_token="admin-token",
        )
        session = FakeSession({"value": []})
        client = SharePointClient(session=session)

        client.search_drive("rent")

        call = session.calls[0]
        self.assertIn("/sites/admin-site/drives/admin-drive/", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer admin-token")

    @override_settings(SHAREPOINT_ACCESS_TOKEN="server-token")
    def test_graph_token_prefers_saved_office365_user_connection_over_server_token(self):
        user = User.objects.create_user(username="advocate", email="advocate@example.org")
        UserOAuthConnection.objects.create(user=user, provider="office365", access_token="user-token")
        request = RequestFactory().get("/")
        request.session = {}
        request.user = user

        self.assertEqual(graph_token_for_request(request), "user-token")


class SharePointConnectorTests(TestCase):
    def test_connector_lists_case_folder_documents(self):
        matter = Matter.objects.create(
            external_id="LS-1",
            client_name="Jane Tenant",
            matter_type="Eviction",
            jurisdiction="Housing Court",
        )
        client = SimpleNamespace(
            configured=True,
            list_case_documents=lambda matter_id, limit=5: [
                {"id": "sp-1", "name": "Photos.pdf", "webUrl": "https://sharepoint/doc"}
            ],
        )
        connector = SharePointConnector(client=client)

        results = connector.search("photos", matter=matter)

        self.assertEqual(results[0].source_kind, "sharepoint")
        self.assertEqual(results[0].url, "https://sharepoint/doc")


class UserResourceConnectorTests(TestCase):
    def test_connector_returns_only_current_users_private_references(self):
        user = User.objects.create_user(username="advocate")
        other = User.objects.create_user(username="other")
        UserResource.objects.create(
            user=user,
            title="Habitability brief",
            resource_type="brief",
            text="Example argument about mold and repair evidence.",
        )
        UserResource.objects.create(
            user=other,
            title="Other user's brief",
            resource_type="brief",
            text="Example argument about mold and repair evidence.",
        )

        results = UserResourceConnector().search("mold repair", user=user)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Habitability brief")
        self.assertEqual(results[0].source_label, "Private reference")
        self.assertTrue(results[0].metadata["private"])


class ContentLibraryTreatiseConnectorTests(TestCase):
    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_searches_generated_chunks_and_retains_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            version = library / "treatises" / "markdown" / "green-book" / "2026-01"
            chunks = version / "chunks"
            chunks.mkdir(parents=True)
            (chunks / "0001-repairs.md").write_text(
                "---\nchunk_id: 0001-repairs\n---\n\n# Repairs\n\n## Source text\n\n"
                "A tenant may raise unsafe repair conditions after giving the landlord notice.\n",
                encoding="utf-8",
            )
            (version / "manifest.yaml").write_text(
                "document_slug: green-book\n"
                "document_title: Sample Housing Treatise\n"
                "document_version: 2026 edition\n"
                "source_path: treatises/source/sample/2026.pdf\n"
                "source_sha256: abc123\n"
                "chunks:\n"
                "- id: 0001-repairs\n"
                "  file: chunks/0001-repairs.md\n"
                "  heading: Repair conditions\n"
                "  path: [Chapter 1, Repair conditions]\n"
                "  pages: [12, 13]\n"
                "  content_kind: substantive-section\n"
                "  source_path: treatises/source/green-book/repairs.pdf\n"
                "  source_sha256: per-file-sha\n",
                encoding="utf-8",
            )
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                results = ContentLibraryTreatiseConnector().search(
                    "habitability repair defense", source_ids=["green-book"]
                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_kind, "rag")
        self.assertIn("Sample Housing Treatise", results[0].citation)
        self.assertIn("PDF pp. 12–13", results[0].citation)
        self.assertEqual(results[0].metadata["chunkId"], "0001-repairs")
        self.assertEqual(results[0].metadata["sourceSha256"], "per-file-sha")
        self.assertEqual(results[0].metadata["sourcePath"], "treatises/source/green-book/repairs.pdf")

    @override_settings(FRAME_ANCESTORS=["https://app.example.org"])
    def test_content_source_detail_and_pdf_endpoints_use_content_provider_paths(self):
        user = User.objects.create_user(username="viewer", password="password")
        self.client.force_login(user)
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            chunks = library / "treatises" / "markdown" / "sample-book" / "2026" / "chunks"
            chunks.mkdir(parents=True)
            source_pdf = library / "treatises" / "source" / "sample-book" / "2026.pdf"
            source_pdf.parent.mkdir(parents=True)
            source_pdf.write_bytes(b"%PDF-1.4 tiny fixture")
            (chunks / "0001.md").write_text(
                "# Repairs\n\n## Source text\n\nA tenant may raise repair conditions.\n",
                encoding="utf-8",
            )
            (chunks.parent / "manifest.yaml").write_text(
                "document_slug: sample-book\n"
                "document_title: Sample Book\n"
                "document_version: 2026\n"
                "source_path: treatises/source/sample-book/2026.pdf\n"
                "chunks:\n"
                "- id: 0001\n"
                "  file: chunks/0001.md\n"
                "  heading: Repairs\n"
                "  path: [Chapter 1, Repairs]\n"
                "  pages: [12]\n",
                encoding="utf-8",
            )
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                detail = self.client.get("/api/sources/content/sample-book/0001/")
                pdf = self.client.get("/api/sources/content/sample-book/0001/pdf/")

        self.assertEqual(detail.status_code, 200)
        payload = detail.json()["source"]
        self.assertEqual(payload["documentTitle"], "Sample Book")
        self.assertEqual(payload["pdfPages"], [12])
        self.assertTrue(payload["hasPdf"])
        self.assertIn("repair conditions", payload["sourceText"])
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertEqual(pdf["Content-Security-Policy"], "frame-ancestors 'self' https://app.example.org")
        # SAMEORIGIN names this API's origin, not the app's, so the browser
        # refused to display a PDF the app had already fetched.
        self.assertIsNone(pdf.headers.get("X-Frame-Options"))

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_searches_generated_statute_chunks_with_official_citation_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            statute = library / "statutes" / "ohio-revised-code"
            chunks = statute / "chunks"
            chunks.mkdir(parents=True)
            (chunks / "orc-5321-04-01.md").write_text(
                "# Ohio Rev. Code § 5321.04 — Landlord obligations\n\n## Source text\n\n"
                "A landlord shall make repairs necessary to keep premises fit and habitable.\n",
                encoding="utf-8",
            )
            (statute / "manifest.yaml").write_text(
                "document_slug: ohio-revised-code\n"
                "document_title: Ohio Revised Code\n"
                "jurisdiction: Ohio\n"
                "chunks:\n"
                "- id: orc-5321-04-01\n"
                "  file: chunks/orc-5321-04-01.md\n"
                "  heading: Ohio Rev. Code § 5321.04 — Landlord obligations\n"
                "  path: [Ohio Revised Code, Chapter 5321, § 5321.04]\n"
                "  content_kind: statute-section\n"
                "  citation: Ohio Rev. Code § 5321.04\n"
                "  url: https://codes.ohio.gov/ohio-revised-code/section-5321.04\n"
                "  effective_date: September 28, 2012\n",
                encoding="utf-8",
            )
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                results = ContentLibraryTreatiseConnector().search(
                    "landlord repair habitability", source_ids=["ohio-statutes"]
                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].citation, "Ohio Rev. Code § 5321.04 (effective September 28, 2012)")
        self.assertEqual(results[0].url, "https://codes.ohio.gov/ohio-revised-code/section-5321.04")
        self.assertEqual(results[0].metadata["jurisdiction"], "Ohio")

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_retrieval_hints_can_rank_hud_content_without_literal_hud_body_text(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            version = library / "treatises" / "markdown" / "hud-4350-3-rev-1" / "2026-01"
            chunks = version / "chunks"
            chunks.mkdir(parents=True)
            (chunks / "0001-rent.md").write_text(
                "# Tenant rent\n\n## Source text\n\n"
                "Owners must process household income changes and tenant rent adjustments.\n",
                encoding="utf-8",
            )
            (version / "manifest.yaml").write_text(
                "document_slug: hud-4350-3-rev-1\n"
                "document_title: HUD Handbook 4350.3\n"
                "retrieval_hints:\n"
                "- subsidized housing\n"
                "- voucher\n"
                "- tenant rent\n"
                "- interim recertification\n"
                "chunks:\n"
                "- id: 0001-rent\n"
                "  file: chunks/0001-rent.md\n"
                "  heading: Tenant rent changes\n"
                "  path: [Chapter 7, Tenant rent]\n"
                "  pages: [10]\n"
                "  content_kind: substantive-section\n",
                encoding="utf-8",
            )
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                results = ContentLibraryTreatiseConnector().search("voucher recertification", source_ids=["hud-handbook"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata["retrievalHints"], ["subsidized housing", "voucher", "tenant rent", "interim recertification"])


class UserResourceViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="advocate", password="password")
        self.client.force_login(self.user)

    def test_user_can_upload_private_reference_document(self):
        response = self.client.post(
            "/api/user-resources/",
            data={
                "title": "Sample opposition",
                "resourceType": "brief",
                "file": SimpleUploadedFile(
                    "opposition.txt",
                    b"This example brief argues that rent should be abated for unrepaired mold.",
                    content_type="text/plain",
                ),
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["resource"]["title"], "Sample opposition")
        resource = UserResource.objects.get()
        self.assertEqual(resource.user, self.user)
        self.assertEqual(resource.resource_type, "brief")
        self.assertIn("unrepaired mold", resource.text)

    def test_user_resource_list_is_owner_scoped(self):
        other = User.objects.create_user(username="other")
        mine = UserResource.objects.create(user=self.user, title="My case", resource_type="case", text="Tenant won.")
        UserResource.objects.create(user=other, title="Other case", resource_type="case", text="Hidden.")

        response = self.client.get("/api/user-resources/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["resources"]], [mine.id])


class ResearchViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="advocate", password="password")
        self.client.force_login(self.user)
        self.result = SourceResult(
            id="source:1",
            title="Habitability guide",
            snippet="Tenant may raise repair conditions as a defense.",
            source_kind="rag",
            source_label="Treatise",
            citation="Housing guide",
        )

    def test_research_without_ai_returns_only_retrieval_results(self):
        registry = SimpleNamespace(search=lambda *args, **kwargs: [self.result])

        with patch("apps.sources.views.connector_registry", registry), patch("apps.sources.views.OpenAICompatibleClient") as ai_client:
            response = self.client.post(
                "/api/research/",
                data=json.dumps({"query": "habitability", "useAi": False, "sourceMode": "auto"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["usedAi"])
        self.assertNotIn("answer", payload)
        self.assertEqual(payload["results"][0]["title"], "Habitability guide")
        self.assertEqual(payload["sourceDecision"]["mode"], "auto")
        self.assertEqual(
            [item["id"] for item in payload["sourceDecision"]["sources"]],
            ["ohio-statutes", "treatise", "ohio-cases"],
        )
        ai_client.assert_not_called()

    def test_research_with_ai_returns_answer_from_retrieved_sources(self):
        registry = SimpleNamespace(search=lambda *args, **kwargs: [self.result])
        fake_client = SimpleNamespace(complete=lambda **kwargs: "Yes, based on Housing guide.")

        with patch("apps.sources.views.connector_registry", registry), patch("apps.sources.views.OpenAICompatibleClient", return_value=fake_client):
            response = self.client.post(
                "/api/research/",
                data=json.dumps({
                    "query": "Can the tenant raise repairs?",
                    "useAi": True,
                    "messages": [{"role": "user", "content": "Earlier question"}],
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["usedAi"])
        self.assertEqual(payload["answer"], "Yes, based on Housing guide.")
        self.assertEqual(payload["results"][0]["citation"], "Housing guide")
        history = self.client.get("/api/research/").json()["messages"]
        self.assertEqual([message["role"] for message in history], ["user", "assistant"])
        self.assertEqual(history[1]["citations"][0]["citation"], "Housing guide")

    def test_research_uses_the_users_default_jurisdiction_in_the_ai_guardrail(self):
        from apps.core.models import AuthorProfile

        AuthorProfile.objects.create(user=self.user, default_jurisdiction="Ohio")
        registry = SimpleNamespace(search=lambda *args, **kwargs: [self.result])
        captured = {}
        fake_client = SimpleNamespace(complete=lambda **kwargs: captured.update(kwargs) or "Ohio answer [1]")

        with patch("apps.sources.views.connector_registry", registry), patch("apps.sources.views.OpenAICompatibleClient", return_value=fake_client):
            response = self.client.post(
                "/api/research/",
                data=json.dumps({"query": "Can the tenant raise repairs?", "useAi": True}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("selected jurisdiction is Ohio", captured["system"])


class AutomaticSourceSelectionTests(TestCase):
    def test_general_question_explains_the_primary_and_secondary_baseline(self):
        selection = automatic_source_selection("What defenses can a tenant raise in an eviction?")

        self.assertEqual(selection["source_ids"], ["ohio-statutes", "treatise", "ohio-cases"])
        self.assertIn("Primary-law baseline", selection["annotations"][0]["reason"])
        self.assertIn("Secondary-source baseline", selection["annotations"][1]["reason"])
        self.assertIn("case-law", selection["annotations"][2]["reason"])

    def test_hud_question_explains_specialized_routing(self):
        selection = automatic_source_selection("What HUD voucher termination rules apply?")

        # The handbook explains; the regulation it explains is searched beside it.
        self.assertEqual(selection["source_ids"], ["hud-handbook", "federal-housing-law"])
        self.assertEqual(selection["annotations"][0]["reason"], "The question concerns federally assisted housing or HUD Handbook 4350.3.")

    def test_hud_source_auto_selects_expanded_terms(self):
        for query in ["subsidized rent", "tenant rent calculation", "interim recertification", "termination of assistance"]:
            with self.subTest(query=query):
                selection = automatic_source_selection(query)
                self.assertEqual(selection["source_ids"][0], "hud-handbook")

    def test_public_housing_question_routes_to_green_book(self):
        selection = automatic_source_selection("What rules govern a public housing authority homeownership program?")

        self.assertEqual(selection["source_ids"], ["green-book", "federal-housing-law"])
        self.assertIn("Green Book", selection["annotations"][0]["reason"])


class ConnectorRegistryTests(TestCase):
    def test_registry_passes_user_and_request_to_connectors(self):
        seen = {}

        class RecordingConnector:
            kind = "recording"

            def search(self, query, **kwargs):
                seen.update(kwargs)
                return []

        registry = ConnectorRegistry()
        registry.register(RecordingConnector())
        user = User(username="advocate", email="advocate@example.org")
        request = SimpleNamespace(session={})

        registry.search("repair", user=user, request=request)

        self.assertEqual(seen["user"], user)
        self.assertEqual(seen["request"], request)

    def test_registry_interleaves_results_across_source_kinds(self):
        def make_connector(kind, count):
            class FakeConnector:
                def __init__(self):
                    self.kind = kind

                def search(self, query, **kwargs):
                    return [
                        SourceResult(id=f"{kind}-{index}", title=f"{kind} {index}", snippet="", source_kind=kind, source_label=kind)
                        for index in range(count)
                    ]
            return FakeConnector()

        registry = ConnectorRegistry()
        registry.register(make_connector("rag", 3))
        registry.register(make_connector("local_cases", 2))

        results = registry.search("notice")

        self.assertEqual(
            [result.id for result in results],
            ["rag-0", "local_cases-0", "rag-1", "local_cases-1", "rag-2"],
        )


class AugmentedSearchTests(TestCase):
    def test_evaluation_accepts_enough_mixed_legal_sources(self):
        results = [
            SourceResult(id="statute-1", title="Repair statute", snippet="repair escrow", source_kind="rag", source_label="Ohio Statutes"),
            SourceResult(id="case-1", title="Repair case", snippet="habitability repair", source_kind="local_cases", source_label="Cases"),
            SourceResult(id="treatise-1", title="Treatise", snippet="rent deposit repair", source_kind="rag", source_label="Treatise"),
            SourceResult(id="statute-2", title="Notice statute", snippet="notice repair", source_kind="rag", source_label="Ohio Statutes"),
        ]

        evaluation = evaluate_search_results("repair", results)

        self.assertTrue(evaluation["adequate"])
        self.assertEqual(evaluation["reasons"], [])

    def test_augmented_search_expands_from_case_statute_facets(self):
        calls = []

        class FakeRegistry:
            def search(self, query, **kwargs):
                calls.append({"query": query, "source_ids": kwargs.get("source_ids"), "kinds": kwargs.get("kinds")})
                if len(calls) == 1:
                    return [
                        SourceResult(
                            id="local-case:1:1",
                            title="Tenant v Landlord",
                            snippet="Repair defects and rent escrow.",
                            source_kind="local_cases",
                            source_label="Local case law",
                            metadata={
                                "statutesCited": ["R.C. 5321.07"],
                                "issues": ["rent deposit"],
                                "casesCited": [],
                                "regulationsCited": [],
                            },
                        )
                    ]
                if "5321.07" in query:
                    return [
                        SourceResult(id="orc-5321-07", title="R.C. 5321.07", snippet="repair notice", source_kind="rag", source_label="Ohio Statutes"),
                    ]
                if query == "rent deposit":
                    return [
                        SourceResult(id="local-case:2:2", title="Rent deposit case", snippet="rent deposit repair", source_kind="local_cases", source_label="Local case law"),
                    ]
                return []

        payload = augmented_search(
            "repair escrow",
            connector_registry=FakeRegistry(),
            source_ids=["ohio-cases"],
            kinds=["local_cases"],
            max_rounds=2,
        )

        self.assertTrue(payload["augmentation"]["expanded"])
        self.assertIn("ohio-statutes", payload["selected_source_ids"])
        self.assertEqual(len(payload["results"]), 3)
        self.assertLessEqual(len(payload["augmentation"]["rounds"]), 5)
        self.assertTrue(any("related statute facet" in round_item["reason"] for round_item in payload["augmentation"]["rounds"][1:]))

    def test_augmented_search_does_not_expand_when_results_are_adequate(self):
        calls = []

        class FakeRegistry:
            def search(self, query, **kwargs):
                calls.append(query)
                return [
                    SourceResult(id="statute-1", title="Repair statute", snippet="repair", source_kind="rag", source_label="Ohio Statutes"),
                    SourceResult(id="case-1", title="Repair case", snippet="repair", source_kind="local_cases", source_label="Cases"),
                    SourceResult(id="treatise-1", title="Treatise", snippet="repair", source_kind="rag", source_label="Treatise"),
                    SourceResult(id="statute-2", title="Notice statute", snippet="repair", source_kind="rag", source_label="Ohio Statutes"),
                ]

        payload = augmented_search(
            "repair",
            connector_registry=FakeRegistry(),
            source_ids=["ohio-statutes", "treatise", "ohio-cases"],
            kinds=["rag", "local_cases"],
        )

        self.assertFalse(payload["augmentation"]["expanded"])
        self.assertEqual(calls, ["repair"])

    def test_cases_only_search_never_expands_to_other_sources(self):
        calls = []

        class FakeRegistry:
            def search(self, query, **kwargs):
                calls.append((query, tuple(kwargs.get("source_ids") or [])))
                return []

        payload = augmented_search(
            "deficient notice",
            connector_registry=FakeRegistry(),
            source_ids=["ohio-cases"],
            kinds=["local_cases"],
            allow_source_expansion=False,
        )

        self.assertEqual(payload["selected_source_ids"], ["ohio-cases"])
        self.assertTrue(all(source_ids == ("ohio-cases",) for _query, source_ids in calls))
        self.assertFalse(payload["augmentation"]["expanded"])

    def test_cases_only_facet_followups_vary_query_within_selected_sources(self):
        calls = []

        class FakeRegistry:
            def search(self, query, **kwargs):
                calls.append((query, tuple(kwargs.get("source_ids") or [])))
                if len(calls) == 1:
                    return [
                        SourceResult(
                            id="local-case:1:1",
                            title="Tenant v Landlord",
                            snippet="Repair defects and rent escrow.",
                            source_kind="local_cases",
                            source_label="Local case law",
                            metadata={
                                "statutesCited": ["R.C. 5321.07"],
                                "issues": ["rent deposit"],
                                "casesCited": [],
                                "regulationsCited": [],
                            },
                        )
                    ]
                return []

        payload = augmented_search(
            "repair escrow",
            connector_registry=FakeRegistry(),
            source_ids=["ohio-cases"],
            kinds=["local_cases"],
            allow_source_expansion=False,
            max_rounds=2,
        )

        self.assertIn(("rent deposit", ("ohio-cases",)), calls)
        self.assertTrue(all(source_ids == ("ohio-cases",) for _query, source_ids in calls))
        self.assertEqual(payload["selected_source_ids"], ["ohio-cases"])

    def test_single_source_selection_is_not_penalized_for_missing_diversity(self):
        case_results = [
            SourceResult(id=f"case-{index}", title="Notice case", snippet="deficient notice", source_kind="local_cases", source_label="Cases")
            for index in range(4)
        ]
        calls = []

        class FakeRegistry:
            def search(self, query, **kwargs):
                calls.append(query)
                return list(case_results)

        payload = augmented_search(
            "deficient notice",
            connector_registry=FakeRegistry(),
            source_ids=["ohio-cases"],
            kinds=["local_cases"],
            allow_source_expansion=False,
        )

        self.assertEqual(calls, ["deficient notice"])
        self.assertTrue(payload["augmentation"]["finalEvaluation"]["adequate"])

    def test_augmented_search_can_retry_same_query_against_new_sources(self):
        calls = []

        class FakeRegistry:
            def search(self, query, **kwargs):
                calls.append((query, tuple(kwargs.get("source_ids") or [])))
                if kwargs.get("source_ids") == ["ohio-cases"]:
                    return [
                        SourceResult(id="case-1", title="Repair case", snippet="repair", source_kind="local_cases", source_label="Cases"),
                    ]
                return []

        payload = augmented_search(
            "repair",
            connector_registry=FakeRegistry(),
            source_ids=["ohio-statutes", "treatise"],
            kinds=["rag"],
            max_rounds=1,
        )

        self.assertTrue(payload["augmentation"]["expanded"])
        self.assertIn(("repair", ("ohio-cases",)), calls)


class JurisdictionMatchingTests(TestCase):
    """A matter's jurisdiction and a decision's rarely agree on punctuation."""

    def test_normalize_drops_punctuation_spacing_and_case(self):
        self.assertEqual(
            jurisdiction.normalize("Cleveland Municipal Court - Housing Division"),
            jurisdiction.normalize("cleveland municipal court, housing division"),
        )
        self.assertEqual(jurisdiction.normalize("St. Mary's/Auglaize"), "stmarysauglaize")
        self.assertEqual(jurisdiction.normalize(""), "")
        self.assertEqual(jurisdiction.normalize(None), "")

    def test_hyphen_and_comma_forms_of_the_same_court_match(self):
        self.assertTrue(
            jurisdiction.matches(
                "Cleveland Municipal Court - Housing Division",
                "Cleveland Municipal Court, Housing Division, Cuyahoga County, Ohio",
            )
        )

    def test_curly_and_straight_apostrophes_match(self):
        self.assertTrue(jurisdiction.matches("St. Mary’s", "Court of St. Mary's County"))

    def test_containment_still_narrows_to_the_right_state(self):
        self.assertTrue(jurisdiction.matches("Ohio", "Ohio Municipal Court, Cuyahoga County"))
        self.assertFalse(jurisdiction.matches("Michigan", "Ohio Municipal Court, Cuyahoga County"))

    def test_any_haystack_may_carry_the_match(self):
        self.assertTrue(jurisdiction.matches("Cuyahoga", "Ohio", "Cleveland Municipal Court", "Cuyahoga"))
        self.assertFalse(jurisdiction.matches("Cuyahoga", "Ohio", "Franklin County Municipal Court", ""))

    def test_a_needle_with_no_letters_matches_nothing(self):
        # "No jurisdiction given" is not a reason to hand out a relevance bonus.
        self.assertFalse(jurisdiction.matches("", "Ohio"))
        self.assertFalse(jurisdiction.matches("  --, ", "Ohio"))


class ContentLibraryBrowseTests(TestCase):
    """Reading the library as a shelf: what is on it, and what is inside one book.

    The tree is built from the same generated manifests retrieval reads, so a
    section found by walking it opens the chunk a citation would.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="reader", password="password")
        self.client.force_login(self.user)

    def _library(self, directory):
        library = Path(directory)
        treatise = library / "treatises" / "markdown" / "sample-book" / "2026" / "chunks"
        treatise.mkdir(parents=True)
        for name in ("0001", "0002", "0003"):
            (treatise / f"{name}.md").write_text(f"# {name}\n\n## Source text\n\nText {name}.\n", encoding="utf-8")
        (treatise.parent / "manifest.yaml").write_text(
            "document_slug: sample-book\n"
            "document_title: Sample Book\n"
            "document_version: 2026\n"
            "source_path: treatises/source/sample-book/2026.pdf\n"
            "chunk_count: 4\n"
            "chunks:\n"
            "- id: '0001'\n"
            "  file: chunks/0001.md\n"
            "  heading: Repairs\n"
            "  path: [Chapter 1, Repairs]\n"
            "- id: '0002'\n"
            "  file: chunks/0002.md\n"
            "  heading: Eviction defenses, part 1\n"
            "  path: [Chapter 2, Eviction defenses]\n"
            "- id: '0003'\n"
            "  file: chunks/0003.md\n"
            "  heading: Eviction defenses, part 2\n"
            "  path: [Chapter 2, Eviction defenses]\n"
            "- id: '0004'\n"
            "  file: chunks/missing.md\n"
            "  heading: Section whose file never landed\n"
            "  path: [Chapter 2, Missing]\n",
            encoding="utf-8",
        )
        statute = library / "statutes" / "sample-code" / "chunks"
        statute.mkdir(parents=True)
        (statute / "s-1.md").write_text("# 5321.04\n\n## Source text\n\nLandlord obligations.\n", encoding="utf-8")
        (statute.parent / "manifest.yaml").write_text(
            "document_slug: sample-code\n"
            "document_title: Sample Revised Code\n"
            "jurisdiction: Ohio\n"
            "content_kind: statute\n"
            "chunks:\n"
            "- id: s-1\n"
            "  file: chunks/s-1.md\n"
            "  heading: Sample Code § 5321.04 - Landlord obligations\n"
            "  path: [Sample Revised Code, Chapter 5321, § 5321.04]\n"
            "  citation: Sample Code § 5321.04\n"
            "  effective_date: March 27, 1985\n",
            encoding="utf-8",
        )
        return library

    def test_library_lists_treatises_and_statutes_a_reader_can_open(self):
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                payload = self.client.get("/api/library/").json()

        documents = {item["slug"]: item for item in payload["documents"]}
        self.assertEqual(set(documents), {"sample-book", "sample-code"})
        self.assertEqual(documents["sample-code"]["contentKind"], "statute")
        self.assertEqual(documents["sample-code"]["jurisdiction"], "Ohio")
        self.assertEqual(documents["sample-book"]["contentKind"], "treatise")
        self.assertEqual(documents["sample-book"]["version"], "2026")

    def test_a_document_opens_as_a_tree_that_mirrors_the_document(self):
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                payload = self.client.get("/api/library/sample-book/").json()

        chapters = {node["label"]: node for node in payload["tree"]}
        self.assertEqual(set(chapters), {"Chapter 1", "Chapter 2"})
        # A section that produced one chunk is the row itself.
        repairs = chapters["Chapter 1"]["children"][0]
        self.assertEqual((repairs["label"], repairs["chunkId"], repairs["children"]), ("Repairs", "0001", []))
        # A section split across chunks keeps one row per part.
        defenses = chapters["Chapter 2"]["children"][0]
        self.assertEqual(defenses["label"], "Eviction defenses")
        self.assertEqual([child["chunkId"] for child in defenses["children"]], ["0002", "0003"])
        self.assertEqual(chapters["Chapter 2"]["count"], 2)
        # A manifest row with no generated file behind it would be a heading
        # that opens nothing, so it is not offered.
        self.assertNotIn("Missing", [child["label"] for child in chapters["Chapter 2"]["children"]])
        self.assertEqual(payload["document"]["readableCount"], 3)
        self.assertTrue(payload["document"]["hasPdf"] is False)

    def test_a_code_opens_on_its_chapters_and_carries_the_official_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                payload = self.client.get("/api/library/sample-code/").json()

        # The single root that names the code itself is not a row the reader
        # has to expand before seeing any chapter.
        self.assertEqual([node["label"] for node in payload["tree"]], ["Chapter 5321"])
        section = payload["tree"][0]["children"][0]
        self.assertEqual(section["label"], "§ 5321.04")
        self.assertEqual(section["chunkId"], "s-1")
        self.assertEqual(section["citation"], "Sample Code § 5321.04")
        self.assertEqual(section["effectiveDate"], "March 27, 1985")

    def test_filtering_narrows_the_contents_and_keeps_the_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                payload = self.client.get("/api/library/sample-book/?q=eviction").json()

        self.assertEqual(payload["matchCount"], 2)
        self.assertEqual([node["label"] for node in payload["tree"]], ["Chapter 2"])
        self.assertEqual(payload["tree"][0]["children"][0]["label"], "Eviction defenses")

    def test_a_private_edition_shadows_the_public_one_as_it_does_in_search(self):
        with tempfile.TemporaryDirectory() as directory:
            public = self._library(directory)
            private = Path(directory) / "private"
            version = private / "treatises" / "markdown" / "sample-book" / "2027" / "chunks"
            version.mkdir(parents=True)
            (version / "0001.md").write_text("# Repairs\n\n## Source text\n\nPrivate text.\n", encoding="utf-8")
            (version.parent / "manifest.yaml").write_text(
                "document_slug: sample-book\n"
                "document_title: Sample Book\n"
                "document_version: 2027 organization edition\n"
                "chunks:\n"
                "- id: '0001'\n"
                "  file: chunks/0001.md\n"
                "  heading: Repairs\n"
                "  path: [Chapter 1, Repairs]\n",
                encoding="utf-8",
            )
            with override_settings(CONTENT_LIBRARY_DIR=public, ORGANIZATION_CONTENT_LIBRARY_DIR=private):
                payload = self.client.get("/api/library/").json()

        editions = [item["version"] for item in payload["documents"] if item["slug"] == "sample-book"]
        self.assertEqual(editions, ["2027 organization edition"])

    def test_an_unknown_document_is_not_found_rather_than_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                response = self.client.get("/api/library/no-such-book/")

        self.assertEqual(response.status_code, 404)


class ContentLibraryManifestCacheTests(TestCase):
    """Parsed manifests are reused, but a regenerated one is never served stale.

    A code manifest is megabytes of generated YAML and parsing it took seconds,
    which every reader waited through on every request.  Caching that parse is
    only safe if ingestion rewriting the file is noticed.
    """

    def _write(self, version, path, title):
        (version / "manifest.yaml").write_text(
            "document_slug: sample-book\n"
            f"document_title: {title}\n"
            "chunks:\n"
            "- id: '0001'\n"
            f"  file: {path}\n"
            "  heading: Repairs\n"
            "  path: [Chapter 1, Repairs]\n",
            encoding="utf-8",
        )

    def test_a_regenerated_manifest_replaces_the_cached_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            version = library / "treatises" / "markdown" / "sample-book" / "2026"
            (version / "chunks").mkdir(parents=True)
            (version / "chunks" / "0001.md").write_text("# Repairs\n\n## Source text\n\nText.\n", encoding="utf-8")
            self._write(version, "chunks/0001.md", "Sample Book")
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                first = library_documents()
                second = library_documents()
                self._write(version, "chunks/0001.md", "Sample Book, Second Edition")
                after_regeneration = library_documents()

        self.assertEqual(first[0]["title"], "Sample Book")
        self.assertEqual(second[0]["title"], "Sample Book")
        self.assertEqual(after_regeneration[0]["title"], "Sample Book, Second Edition")

    def test_an_unreadable_manifest_is_skipped_rather_than_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            version = library / "statutes" / "broken-code"
            version.mkdir(parents=True)
            (version / "manifest.yaml").write_text("document_slug: [unclosed\n", encoding="utf-8")
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "private-missing"):
                self.assertEqual(library_documents(), [])
                self.assertIsNone(load_manifest(version / "manifest.yaml"))
                self.assertIsNone(load_manifest(version / "does-not-exist.yaml"))


ORDINANCE_MANIFEST = """document_slug: ordinances-testville
document_title: Codified Ordinances of Testville
jurisdiction: Testville, Ohio
content_kind: ordinance
municipality: Testville
municipality_slug: testville
county: Testcounty
code_short_name: Testville Codified Ordinances
codifier: american-legal
source_base_url: https://example.invalid/testville
preemption_statute: '5321.19'
section_count: 1
pending_count: 1
chunk_count: 1
sections:
- key: pay-to-stay
  topic: pay-to-stay
  topic_label: Pay-to-stay / right to cure
  priority: 1
  citation: Testville Codified Ordinances Ch. 900
  title: Pay to stay
  status: ingested
  text_basis: enacted_act
  acquisition_method: legistar
  act_file_number: O-1-24
  enacted_date: '2024-01-02'
  amended_date: ''
  related_statutes: ['5321.19']
  treatise_chunks: ['0609-pay-to-stay']
  related_cases: ['Nobody v. Nobody, 2026-Ohio-1']
  preemption:
    status: unadjudicated
    confidence: unreviewed
  chunks:
  - id: ord-testville-pay-to-stay-01
    file: chunks/ord-testville-pay-to-stay-01.md
- key: late-fees
  topic: late-fees
  citation: Testville Codified Ordinances Ch. 901
  title: Late fees
  status: pending
  pending_reason: Codifier serves a bot challenge.
  text_basis: not_acquired
  acquisition_method: pending
  chunks: []
chunks:
- id: ord-testville-pay-to-stay-01
  file: chunks/ord-testville-pay-to-stay-01.md
  heading: Testville Codified Ordinances Ch. 900 - Pay to stay
  path: [Part Nine, Chapter 900]
  content_kind: ordinance-section
  section: '900'
  chapter: '900'
  citation: Testville Codified Ordinances Ch. 900
  url: https://example.invalid/testville
  effective_date: '2024-01-02'
  text_basis: enacted_act
  retrieval_hints: [Testville, pay to stay]
"""

STATUTE_MANIFEST = """document_slug: ohio-revised-code
document_title: Ohio Revised Code
jurisdiction: Ohio
content_kind: statute
chunks:
- id: orc-5321-19-01
  file: chunks/orc-5321-19-01.md
  heading: Ohio Rev. Code 5321.19
  path: [Ohio Revised Code, Chapter 5321]
  content_kind: statute-section
  section: '5321.19'
  chapter: '5321'
  citation: Ohio Rev. Code 5321.19
"""


HEIGHTS_MANIFEST = """document_slug: ordinances-testville-heights
document_title: Codified Ordinances of Testville Heights
jurisdiction: Testville Heights, Ohio
content_kind: ordinance
municipality: Testville Heights
municipality_slug: testville-heights
county: Testcounty
source_base_url: https://example.invalid/testville-heights
section_count: 0
pending_count: 1
chunk_count: 0
sections:
- key: pay-to-stay
  topic: pay-to-stay
  topic_label: Pay-to-stay / right to cure
  citation: Testville Heights Codified Ordinances Ch. 767
  title: Pay to stay
  status: pending
  pending_reason: Codifier serves a bot challenge.
  text_basis: not_acquired
  acquisition_method: pending
  codifier_url: https://example.invalid/testville-heights
  chunks: []
chunks: []
"""


def _write_ordinance_library(root):
    """A minimal two-document library: one municipal code, one statute code."""
    ordinance_dir = root / "ordinances" / "testville"
    (ordinance_dir / "chunks").mkdir(parents=True)
    (ordinance_dir / "manifest.yaml").write_text(ORDINANCE_MANIFEST, encoding="utf-8")
    (ordinance_dir / "chunks" / "ord-testville-pay-to-stay-01.md").write_text(
        "# Pay to stay\n\n## Source text\n\nA tenant who tenders all past due rent with "
        "reasonable late fees before filing may assert the tender as a defense.\n",
        encoding="utf-8",
    )
    statute_dir = root / "statutes" / "ohio-revised-code"
    (statute_dir / "chunks").mkdir(parents=True)
    (statute_dir / "manifest.yaml").write_text(STATUTE_MANIFEST, encoding="utf-8")
    (statute_dir / "chunks" / "orc-5321-19-01.md").write_text(
        "# Ohio Rev. Code 5321.19\n\n## Source text\n\nNo political subdivision shall enact.\n",
        encoding="utf-8",
    )
    heights_dir = root / "ordinances" / "testville-heights"
    heights_dir.mkdir(parents=True)
    (heights_dir / "manifest.yaml").write_text(HEIGHTS_MANIFEST, encoding="utf-8")

    datasets = root / "ordinances" / "datasets"
    datasets.mkdir(parents=True)
    (datasets / "pay-to-stay.yaml").write_text(
        "title: Ohio pay-to-stay ordinances\n"
        "sources:\n"
        "  testville-900:\n"
        "    document: ordinances-testville\n"
        "    chunk: ord-testville-pay-to-stay-01\n"
        "    citation: Testville Codified Ordinances Ch. 900\n"
        "  handbook:\n"
        "    citation: Sample Handbook section 1\n"
        "records:\n"
        "- municipality: testville\n"
        "  citation: Testville Codified Ordinances Ch. 900\n"
        "  fields:\n"
        "    effect:\n"
        "      value: landlord_must_accept\n"
        "      basis: ordinance-text\n"
        "      source: testville-900\n"
        "    frequency_limit:\n"
        "      value: unknown\n"
        "      basis: unknown\n"
        "- municipality: farville\n"
        "  citation: Farville Codified Ordinances Ch. 900\n"
        "  fields:\n"
        "    effect:\n"
        "      value: landlord_must_accept\n"
        "      basis: treatise\n"
        "      source: handbook\n"
        "    frequency_limit:\n"
        "      value: unknown\n"
        "      basis: unknown\n"
        "- municipality: testville-heights\n"
        "  citation: Testville Heights Codified Ordinances Ch. 767\n"
        "  status: repealed\n"
        "  repeal_date: '2024-06-04'\n"
        "  fields:\n"
        "    effect:\n"
        "      value: landlord_must_accept\n"
        "      basis: treatise\n"
        "      source: handbook\n"
        "    frequency_limit:\n"
        "      value: unknown\n"
        "      basis: unknown\n",
        encoding="utf-8",
    )
    (root / "ordinances" / "scope.yaml").write_text(
        "topics:\n"
        "  pay-to-stay:\n"
        "    label: Pay-to-stay / right to cure\n"
        "    dataset: pay-to-stay\n"
        "  late-fees:\n"
        "    label: Limit on late payment fees\n"
        "declared_municipalities:\n"
        "- {slug: elsewhere, name: Elsewhere, county: Testcounty, topics: [pay-to-stay]}\n"
        "- {slug: farville, name: Farville, county: Testcounty, topics: [pay-to-stay]}\n",
        encoding="utf-8",
    )


class OrdinanceLibraryTests(TestCase):
    """Local law has to behave like a statute collection, not like a special case."""

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_ordinance_documents_shelve_and_search_as_their_own_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                documents = {document["slug"]: document for document in library_documents()}
                results = ContentLibraryTreatiseConnector().search(
                    "pay to stay tender defense", source_ids=["ohio-ordinances"],
                )

        ordinance = documents["ordinances-testville"]
        self.assertEqual(ordinance["contentKind"], "ordinance")
        self.assertEqual(ordinance["municipality"], "Testville")
        # A declared-but-unacquired authority must stay visible on the shelf.
        self.assertEqual(ordinance["pendingCount"], 1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].metadata["documentKind"], "ordinance")
        self.assertEqual(results[0].metadata["textBasis"], "enacted_act")
        self.assertEqual(results[0].metadata["documentSlug"], "ordinances-testville")

    @override_settings(AI_DRAFTING_ENABLED=False)
    def test_selecting_ordinances_excludes_other_content_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                ordinance_only = ContentLibraryTreatiseConnector().search(
                    "political subdivision tender", source_ids=["ohio-ordinances"],
                )
                statute_only = ContentLibraryTreatiseConnector().search(
                    "political subdivision tender", source_ids=["ohio-statutes"],
                )

        self.assertEqual([result.metadata["documentKind"] for result in ordinance_only], ["ordinance"])
        self.assertEqual([result.metadata["documentSlug"] for result in statute_only], ["ohio-revised-code"])

    def test_coverage_reports_pending_authorities_and_declared_municipalities(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                payload = ordinance_coverage()

        self.assertEqual(payload["ingestedCount"], 1)
        # Both the pending late-fee chapter and the neighbouring municipality
        # whose whole code is unreachable are counted.
        self.assertEqual(payload["pendingCount"], 2)
        municipality = next(item for item in payload["municipalities"] if item["municipality"] == "Testville")
        pending = next(item for item in municipality["sections"] if item["status"] == "pending")
        self.assertEqual(pending["pendingReason"], "Codifier serves a bot challenge.")
        self.assertEqual(pending["textBasis"], "not_acquired")
        self.assertEqual([item["municipality"] for item in payload["declared"]], ["Elsewhere", "Farville"])

    def test_dataset_keeps_field_basis_and_resolves_its_source_to_a_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                payload = ordinance_dataset("pay-to-stay")
                self.assertIsNone(ordinance_dataset("../escape"))

        record = payload["records"][0]
        self.assertEqual(record["fields"]["effect"]["basis"], "ordinance-text")
        self.assertEqual(
            record["fields"]["effect"]["source"]["sourceId"],
            "content:ordinances-testville:ord-testville-pay-to-stay-01",
        )
        # An unknown field keeps its basis rather than being dropped or upgraded.
        self.assertEqual(record["fields"]["frequency_limit"]["basis"], "unknown")
        self.assertIsNone(record["fields"]["frequency_limit"]["source"])

    def test_cross_references_resolve_outward_and_back_to_the_statute(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                outward = cross_references("ordinances-testville", "ord-testville-pay-to-stay-01")
                inbound = cross_references("ohio-revised-code", "orc-5321-19-01")

        self.assertEqual(
            [item["sourceId"] for item in outward["outward"]["statutes"]],
            ["content:ohio-revised-code:orc-5321-19-01"],
        )
        # A case this corpus does not hold is reported, not silently dropped.
        self.assertEqual(outward["outward"]["unresolvedCases"], ["Nobody v. Nobody, 2026-Ohio-1"])
        # Reading the preemption statute has to surface the local law it reaches.
        self.assertEqual(
            [item["citation"] for item in inbound["ordinancesCiting"]],
            ["Testville Codified Ordinances Ch. 900"],
        )

    def test_auto_source_routing_selects_local_law_for_a_local_question(self):
        selection = automatic_source_selection("Does the Cleveland pay to stay ordinance apply?")
        self.assertIn("ohio-ordinances", selection["source_ids"])


class OrdinanceCoverageNoticeTests(TestCase):
    """A gap has to answer the search, or another city's law answers it instead."""

    def _search(self, library, query, **kwargs):
        with override_settings(
            CONTENT_LIBRARY_DIR=library,
            ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing",
            AI_DRAFTING_ENABLED=False,
        ):
            return ContentLibraryTreatiseConnector().search(query, source_ids=["ohio-ordinances"], **kwargs)

    def test_a_question_about_an_unheld_ordinance_is_answered_by_the_gap_not_by_another_city(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            results = self._search(library, "Testville Heights pay to stay tender")

        first = results[0]
        self.assertEqual(first.metadata["resultType"], "coverage-notice")
        self.assertEqual(first.metadata["municipality"], "Testville Heights")
        self.assertEqual(first.metadata["textBasis"], "not_acquired")
        # The notice sends the reader to the publisher rather than to text the
        # corpus does not have.
        self.assertEqual(first.url, "https://example.invalid/testville-heights")
        self.assertIn("does not hold", first.snippet)
        # Testville's actual ordinance still ranks, below the gap.
        self.assertIn("ordinances-testville", [result.metadata.get("documentSlug", "") for result in results[1:]])

    def test_a_nested_city_name_does_not_pull_in_its_neighbour(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            heights = self._search(library, "Testville Heights pay to stay")
            testville = self._search(library, "Testville late fees")

        notices = [result for result in heights if result.metadata.get("resultType") == "coverage-notice"]
        self.assertEqual([notice.metadata["municipality"] for notice in notices], ["Testville Heights"])
        # The shorter name still works on its own.
        self.assertEqual(
            [result.metadata["municipality"] for result in testville
             if result.metadata.get("resultType") == "coverage-notice"],
            ["Testville"],
        )

    def test_a_question_that_names_no_municipality_gets_no_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            results = self._search(library, "pay to stay tender defense")

        self.assertEqual([result for result in results if result.metadata.get("resultType") == "coverage-notice"], [])

    def test_a_notice_repeats_a_secondary_source_and_names_it_as_one(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            described = self._search(library, "Farville pay to stay")[0]
            undescribed = self._search(library, "Testville late fees")[0]

        self.assertIn("Sample Handbook section 1", described.snippet)
        self.assertIn("secondary source, not the ordinance text", described.snippet)
        self.assertIn("effect: landlord must accept", described.snippet)
        # A field nobody has established stays out of the summary entirely.
        self.assertNotIn("frequency limit", described.snippet)
        # No attributable source means no summary, rather than a written-up guess.
        self.assertNotIn("Described by", undescribed.snippet)

    def test_a_declared_municipality_with_no_manifest_still_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            results = self._search(library, "Elsewhere pay to stay")

        first = results[0]
        self.assertEqual(first.metadata["resultType"], "coverage-notice")
        self.assertEqual(first.metadata["municipality"], "Elsewhere")
        self.assertIn("No publisher link has been established", first.snippet)


class OrdinanceProvenanceTests(TestCase):
    """What a record is, and whether it is still law, must survive to the reader."""

    def test_a_repealed_provision_is_described_in_the_past_tense_and_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            _write_ordinance_library(library)
            with override_settings(
                CONTENT_LIBRARY_DIR=library,
                ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing",
                AI_DRAFTING_ENABLED=False,
            ):
                notice = ContentLibraryTreatiseConnector().search(
                    "Testville Heights pay to stay", source_ids=["ohio-ordinances"],
                )[0]
                payload = ordinance_dataset("pay-to-stay")

        self.assertIn("NOT CURRENT LAW", notice.snippet)
        self.assertIn("repealed on 2024-06-04", notice.snippet)
        # The live-ordinance phrasing must not also appear.
        self.assertNotIn("secondary source, not the ordinance text", notice.snippet)

        record = next(item for item in payload["records"] if item["municipality"] == "testville-heights")
        self.assertEqual(record["status"], "repealed")
        self.assertEqual(record["repealDate"], "2024-06-04")
        # An in-force record keeps the default status rather than an empty one.
        live = next(item for item in payload["records"] if item["municipality"] == "testville")
        self.assertEqual(live["status"], "in_force")


class OrdinanceAdminManagementTests(TestCase):
    """A person has to be able to correct and re-source the corpus in place."""

    def _library(self, directory):
        library = Path(directory)
        _write_ordinance_library(library)
        return library

    def test_an_override_patches_only_the_fields_it_fills_in(self):
        OrdinanceOverride.objects.create(
            municipality_slug="testville",
            target_key="pay-to-stay",
            citation="Testville Codified Ordinances Ch. 900 (as amended)",
            legal_status="repealed",
            repeal_date="2025-01-15",
            preemption_status="challenged",
            reviewed_by="quinten",
            verified=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                payload = ordinance_coverage()

        section = next(
            item for municipality in payload["municipalities"]
            for item in municipality["sections"] if item["key"] == "pay-to-stay"
            and municipality["municipality"] == "Testville"
        )
        self.assertEqual(section["citation"], "Testville Codified Ordinances Ch. 900 (as amended)")
        self.assertEqual(section["legalStatus"], "repealed")
        self.assertEqual(section["repealDate"], "2025-01-15")
        self.assertEqual(section["preemption"]["status"], "challenged")
        # An untouched field keeps its generated value, and the patch says so.
        self.assertEqual(section["actFileNumber"], "O-1-24")
        self.assertIn("citation", section["overriddenFields"])
        self.assertNotIn("actFileNumber", section["overriddenFields"])
        self.assertTrue(section["verified"])

    def test_documents_attach_to_an_authority_and_a_superseded_one_is_kept(self):
        packet = OrdinanceDocument.objects.create(
            municipality_slug="testville", target_key="pay-to-stay",
            title="Council packet", source_type="council_packet", url="https://example.invalid/packet",
        )
        signed = OrdinanceDocument.objects.create(
            municipality_slug="testville", target_key="pay-to-stay",
            title="Signed ordinance", source_type="signed_ordinance", url="https://example.invalid/signed",
        )
        packet.status = "superseded"
        packet.superseded_by = signed
        packet.save()

        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                payload = ordinance_coverage()

        section = next(
            item for municipality in payload["municipalities"]
            for item in municipality["sections"]
            if municipality["municipality"] == "Testville" and item["key"] == "pay-to-stay"
        )
        titles = [document["title"] for document in section["documents"]]
        # Active first, and the replaced document is still on the record.
        self.assertEqual(titles, ["Signed ordinance", "Council packet"])
        superseded = section["documents"][1]
        self.assertEqual(superseded["status"], "superseded")
        self.assertEqual(superseded["supersededById"], signed.id)

    def test_an_admin_repeal_reaches_the_search_notice(self):
        OrdinanceOverride.objects.create(
            municipality_slug="testville-heights", target_key="pay-to-stay",
            legal_status="expired", repeal_date="2025-03-31",
        )
        with tempfile.TemporaryDirectory() as directory:
            library = self._library(directory)
            with override_settings(
                CONTENT_LIBRARY_DIR=library,
                ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing",
                AI_DRAFTING_ENABLED=False,
            ):
                notice = ContentLibraryTreatiseConnector().search(
                    "Testville Heights pay to stay", source_ids=["ohio-ordinances"],
                )[0]

        self.assertIn("NOT CURRENT LAW", notice.snippet)
        self.assertIn("expired on 2025-03-31", notice.snippet)

    def test_an_upload_is_stored_by_content_hash_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(DOCUMENT_STORAGE_BACKEND="filesystem", DOCUMENT_STORAGE_ROOT=Path(directory)):
                first = store_upload(
                    content=b"%PDF-1.4 ordinance", municipality_slug="testville",
                    target_key="pay-to-stay", filename="Ord-1-24.pdf", content_type="application/pdf",
                )
                second = store_upload(
                    content=b"%PDF-1.4 ordinance", municipality_slug="testville",
                    target_key="pay-to-stay", filename="Ord-1-24.pdf", content_type="application/pdf",
                )

        self.assertEqual(first["storage_key"], second["storage_key"])
        self.assertTrue(first["storage_key"].endswith(".pdf"))
        self.assertEqual(first["sha256"], hashlib.sha256(b"%PDF-1.4 ordinance").hexdigest())
        self.assertEqual(first["size_bytes"], len(b"%PDF-1.4 ordinance"))


class RepealedOrdinanceVisibilityTests(TestCase):
    """A repeal recorded in the corpus has to reach the library and the search.

    It produces no chunk, so a city whose one relevant chapter was repealed
    opened in the library as "0 sections", and a search naming it led with a
    treatise describing the repealed chapter as current law.
    """

    def _library_with_repeal(self, root):
        _write_ordinance_library(root)
        manifest = (root / "ordinances" / "testville-heights" / "manifest.yaml")
        manifest.write_text(
            HEIGHTS_MANIFEST.replace("pending_count: 1", "pending_count: 0")
            .replace("  status: pending\n  pending_reason: Codifier serves a bot challenge.\n", (
                "  status: no_current_provision\n"
                "  not_in_force_reason: Ord. 2024-27 repealed the whole chapter.\n"
                "  repeal_date: '2024-06-04'\n"
            ))
            .replace("  text_basis: not_acquired\n", "  text_basis: no_current_provision\n"),
            encoding="utf-8",
        )

    def setUp(self):
        user = User.objects.create_user("repeal-reader", password="unused-here")
        self.client.force_login(user)

    def test_the_library_document_states_the_repeal(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            self._library_with_repeal(library)
            with override_settings(CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing"):
                response = self.client.get("/api/library/ordinances-testville-heights/")

        self.assertEqual(response.status_code, 200)
        notices = response.json()["coverageNotices"]
        self.assertEqual(len(notices), 1)
        self.assertIn("No Pay-to-stay / right to cure provision is in force in Testville Heights", notices[0]["snippet"])
        self.assertIn("2024-06-04", notices[0]["snippet"])

    def test_a_search_naming_the_city_carries_the_repeal(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory)
            self._library_with_repeal(library)
            with override_settings(
                CONTENT_LIBRARY_DIR=library, ORGANIZATION_CONTENT_LIBRARY_DIR=library / "missing", AI_DRAFTING_ENABLED=False
            ):
                response = self.client.post(
                    "/api/research/search/",
                    data=json.dumps({"query": "Testville Heights pay to stay"}),
                    content_type="application/json",
                )

        self.assertEqual(response.status_code, 200)
        notices = response.json()["coverageNotices"]
        self.assertEqual([notice["municipality"] for notice in notices], ["Testville Heights"])
        self.assertFalse(notices[0]["inForce"])

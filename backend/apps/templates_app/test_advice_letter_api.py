import json
import zipfile
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings

from apps.ai.services import drafting_ai
from apps.drafting.models import DraftDocument
from apps.matters.models import Matter
from apps.templates_app.models import AdviceLetterSection


class AdviceLetterApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="advocate", email="a@example.org", password="pw"
        )
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(
            external_id="2026-CVG-77",
            client_name="Maria Alvarez",
            matter_type="Eviction",
            jurisdiction="Cleveland Municipal Court",
            summary="The 3-day notice names a different landlord than the complaint.",
            raw_payload={
                "client_full_name": "MARIA ALVAREZ",
                "case_number": "2026 CVG 011123",
                "legal_problem_code": "06 Eviction / Ejectment",
                "client_address_home": {
                    "street": "123 W 25th St",
                    "city": "Cleveland",
                    "state": "OH",
                    "zip": "44113",
                },
            },
        )
        AdviceLetterSection.objects.create(
            slug="letter-opening", title="Opening", role="intro", body="Thank you for asking Legal Aid."
        )
        AdviceLetterSection.objects.create(
            slug="letter-closing", title="Closing", role="closing", body="I have closed your file."
        )
        AdviceLetterSection.objects.create(
            slug="decarlo",
            title="DeCarlo",
            topic="Presenting Defenses",
            region="CLE",
            body="There is a legal issue with the 3-Day Notice.",
            status="needs_review",
            needs_attorney_review=True,
            review_reason="2 reviewer comment(s) dropped",
            selection_hints={
                "triggers": ["3-day notice names a different landlord than the complaint"],
                "requires": ["has_3_day_notice"],
                "summary": "Notice and complaint name different parties.",
            },
        )
        AdviceLetterSection.objects.create(
            slug="seal", title="Motion to Seal", topic="Pro se How-To", region="CLE",
            body="You can ask the Court to seal the record.",
            content_path="docx-snippets/advice/seal.md",
            source_checksum="seal-checksum",
        )

    def post(self, path, payload):
        return self.client.post(path, data=json.dumps(payload), content_type="application/json")

    def test_sections_list_reports_review_state(self):
        response = self.client.get("/api/advice-letters/sections/?region=CLE")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        slugs = {section["slug"]: section for section in data["sections"]}
        self.assertIn("decarlo", slugs)
        self.assertTrue(slugs["decarlo"]["needsReview"])
        self.assertIn("comment", slugs["decarlo"]["reviewReason"])
        self.assertEqual(data["awaitingReview"], 1)
        self.assertIn("Presenting Defenses", data["topics"])

    def test_wrapper_is_returned_separately_from_body_sections(self):
        data = self.client.get("/api/advice-letters/sections/").json()

        self.assertEqual(set(data["wrapper"]), {"intro", "closing"})
        self.assertNotIn("letter-opening", {row["slug"] for row in data["sections"]})

    def test_recommendations_explain_themselves(self):
        response = self.post(
            "/api/advice-letters/recommend/",
            {"matterId": "2026-CVG-77", "region": "CLE", "conditions": {"has_3_day_notice": True}},
        )

        self.assertEqual(response.status_code, 200)
        top = response.json()["recommendations"][0]
        self.assertEqual(top["section"]["slug"], "decarlo")
        self.assertTrue(top["needsReview"])
        self.assertTrue(any("3-day notice" in reason for reason in top["reasons"]))

    def test_recommendations_need_a_case(self):
        response = self.post("/api/advice-letters/recommend/", {"matterId": "nope"})

        self.assertEqual(response.status_code, 404)

    def test_preview_assembles_in_the_order_requested(self):
        response = self.post(
            "/api/advice-letters/preview/",
            {"matterId": "2026-CVG-77", "sectionSlugs": ["seal", "decarlo"]},
        )

        self.assertEqual(response.status_code, 200)
        letter = response.json()["letter"]
        slugs = [section["slug"] for section in letter["sections"]]
        self.assertEqual(slugs, ["letter-opening", "seal", "decarlo", "letter-closing"])
        self.assertIn("flesch_kincaid_grade", letter["readability"]["metrics"])

    def test_preview_warns_about_an_unreviewed_section(self):
        letter = self.post(
            "/api/advice-letters/preview/", {"sectionSlugs": ["decarlo"]}
        ).json()["letter"]

        self.assertTrue(any("DeCarlo" in warning for warning in letter["warnings"]))

    def test_preview_needs_at_least_one_section(self):
        response = self.post("/api/advice-letters/preview/", {"sectionSlugs": []})

        self.assertEqual(response.status_code, 400)

    def test_an_unknown_section_is_reported(self):
        response = self.post("/api/advice-letters/preview/", {"sectionSlugs": ["nope"]})

        self.assertEqual(response.status_code, 404)
        self.assertIn("nope", response.json()["error"])

    def test_addressing_is_prefilled_from_the_case(self):
        response = self.client.get("/api/advice-letters/addressing/?matterId=2026-CVG-77")

        self.assertEqual(response.status_code, 200)
        addressing = response.json()["addressing"]
        self.assertEqual(addressing["recipientName"], "MARIA ALVAREZ")
        self.assertIn("123 W 25th St", addressing["recipientAddress"])
        self.assertEqual(addressing["matterSubject"], "eviction")

    def test_the_letter_greets_the_client_by_name(self):
        """It shipped saying "Dear [Client]:" because nothing read the case."""
        response = self.post(
            "/api/advice-letters/export/",
            {"matterId": "2026-CVG-77", "sectionSlugs": ["seal"]},
        )

        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Dear MARIA ALVAREZ:", document)
        self.assertNotIn("[Client]", document)
        self.assertIn("2026 CVG 011123", document)

    def test_the_opening_names_what_the_case_is_about(self):
        AdviceLetterSection.objects.filter(slug="letter-opening").update(
            body="Thank you for asking Legal Aid for help with your {{ matter_subject }}."
        )

        letter = self.post(
            "/api/advice-letters/preview/",
            {"matterId": "2026-CVG-77", "sectionSlugs": ["seal"]},
        ).json()["letter"]

        self.assertIn("help with your eviction.", letter["body"])
        self.assertNotIn("[eviction/housing issue]", letter["body"])

    def test_legacy_bracket_fields_are_resolved_before_the_advice_editor(self):
        self.matter.raw_payload = {
            **self.matter.raw_payload,
            "custom_fields": {
                "Plaintiff Name": "Example Homes LLC",
                "Filing Date": "July 12, 2026",
            },
        }
        self.matter.save(update_fields=["raw_payload", "updated_at"])
        AdviceLetterSection.objects.filter(slug="seal").update(
            body=(
                "The Magistrate decided your landlord can evict you for [Plaintiff Name].\n\n"
                "Objections are due by [Filing Date]. If the record lacks [Unknown Document Name], "
                "ask your advocate."
            )
        )

        response = self.post(
            "/api/advice-letters/drafts/",
            {"matterId": self.matter.external_id, "sectionSlugs": ["seal"]},
        )

        self.assertEqual(response.status_code, 201)
        seal = next(section for section in response.json()["draft"]["sections"] if section["key"] == "seal")
        self.assertIn("Example Homes LLC", seal["body"])
        self.assertIn("July 12, 2026", seal["body"])
        self.assertNotIn("[Plaintiff Name]", seal["body"])
        self.assertNotIn("[Filing Date]", seal["body"])
        self.assertIn("[Unknown Document Name]", seal["body"])

    def test_export_returns_a_word_document(self):
        response = self.post(
            "/api/advice-letters/export/",
            {
                "sectionSlugs": ["seal"],
                "recipientName": "Ms. Alvarez",
                "letterDate": "August 2, 2026",
                "subject": "Sealing your eviction record",
                "authorProfile": {"displayName": "Dana Ruiz", "title": "Staff Attorney"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'filename="2026-08-02-alvarez-advice-letter-motion-seal.docx"',
            response["Content-Disposition"],
        )
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Ms. Alvarez", document)
        self.assertIn("seal the record", document)

    def test_advice_draft_uses_shared_blocks_and_records_catalog_provenance(self):
        fact = self.matter.facts.create(
            slug="notice-party",
            title="Notice names a different landlord",
            text="The notice names a different landlord than the complaint.",
            source_label="Client interview",
        )
        source = {
            "id": "ohio-housing-rule",
            "title": "Ohio housing rule",
            "citation": "O.R.C. 5321.04",
            "sourceKind": "court_rules",
            "snippet": "A landlord must maintain fit premises.",
            "metadata": {"contentPath": "rules/ohio-housing.md", "sourceChecksum": "rule-checksum"},
        }

        response = self.post(
            "/api/advice-letters/drafts/",
            {
                "matterId": self.matter.external_id,
                "sectionSlugs": ["seal"],
                "goal": "Explain the next step.",
                "selectedFactIds": [fact.id],
                "selectedSourceResults": [source],
            },
        )

        self.assertEqual(response.status_code, 201)
        draft_payload = response.json()["draft"]
        self.assertEqual(draft_payload["editorState"]["format"], "lexical_blocks")
        self.assertEqual(
            [section["key"] for section in draft_payload["sections"]],
            ["letter-opening", "seal", "letter-closing"],
        )
        draft = DraftDocument.objects.get(id=draft_payload["id"])
        self.assertEqual(draft.session.mode, "advice_letter")
        seal_version = draft.components.get(stable_key="seal").current_version
        bindings = {binding.source_key: binding for binding in seal_version.source_bindings.all()}
        self.assertIn("advice-letter:seal", bindings)
        self.assertEqual(bindings["advice-letter:seal"].locator["contentPath"], "docx-snippets/advice/seal.md")
        self.assertEqual(bindings["advice-letter:seal"].locator["sourceChecksum"], "seal-checksum")

    def test_advice_draft_carries_source_lexical_formatting_into_each_block(self):
        state = {
            "root": {
                "children": [
                    {
                        "children": [
                            {"text": "Heading.  ", "format": 1, "type": "text"},
                            {"text": "Body.", "format": 0, "type": "text"},
                        ],
                        "type": "paragraph",
                    },
                    {"children": [], "type": "paragraph"},
                ],
                "type": "root",
            }
        }
        AdviceLetterSection.objects.filter(slug="seal").update(editor_state=state)

        response = self.post(
            "/api/advice-letters/drafts/",
            {"matterId": self.matter.external_id, "sectionSlugs": ["seal"]},
        )

        self.assertEqual(response.status_code, 201)
        editor_state = response.json()["draft"]["editorState"]["blocks"]["seal"]
        first = editor_state["root"]["children"][0]
        self.assertEqual(first["children"][0]["format"], 1)
        self.assertEqual(first["children"][0]["text"], "Heading.  ")
        self.assertEqual(editor_state["root"]["children"][1]["children"], [])

    def test_advice_draft_exposes_the_opening_issue_as_a_locked_block(self):
        state = {
            "root": {
                "children": [
                    {
                        "children": [{"text": "The notice has a legal defect.", "format": 1, "type": "text"}],
                        "type": "paragraph",
                    },
                    {"children": [], "type": "paragraph"},
                    {
                        "children": [{"text": "The notice and complaint name different parties.", "format": 0, "type": "text"}],
                        "type": "paragraph",
                    },
                    {"children": [], "type": "paragraph"},
                    {
                        "children": [{"text": "Ask the Court to dismiss the case.", "format": 0, "type": "text"}],
                        "type": "paragraph",
                    },
                ],
                "type": "root",
            }
        }
        AdviceLetterSection.objects.create(
            slug="notice-issue",
            title="Notice issue",
            role="body",
            body="The notice has a legal defect.\nThe notice and complaint name different parties.\nAsk the Court to dismiss the case.",
            editor_state=state,
        )

        response = self.post(
            "/api/advice-letters/drafts/",
            {"matterId": self.matter.external_id, "sectionSlugs": ["notice-issue"]},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        blocks = payload["draft"]["sections"]
        self.assertEqual(
            [section["key"] for section in blocks],
            ["letter-opening", "issue-statement-notice-issue", "notice-issue", "letter-closing"],
        )
        issue = blocks[1]
        self.assertEqual(issue["label"], "Notice issue: issue statement")
        self.assertEqual(issue["aiLatitude"], "locked")
        self.assertEqual(issue["adviceBlockRole"], "issue_statement")
        self.assertEqual(issue["sourceEditorState"]["root"]["children"][0]["children"][0]["format"], 1)
        self.assertIn("The notice has a legal defect.", issue["body"])
        self.assertIn("The notice and complaint name different parties.", issue["body"])
        self.assertNotIn("Ask the Court to dismiss", issue["body"])
        self.assertEqual(
            [section["slug"] for section in payload["letter"]["sections"]],
            ["letter-opening", "notice-issue", "letter-closing"],
        )
        self.assertEqual(payload["letter"]["body"].count("The notice has a legal defect."), 1)
        self.assertEqual(payload["letter"]["body"].count("Ask the Court to dismiss"), 1)

    def test_advice_redraft_uses_the_shared_ai_operation_and_binds_support(self):
        fact = self.matter.facts.create(
            slug="notice-party",
            title="Notice names a different landlord",
            text="The notice names a different landlord than the complaint.",
            source_label="Client interview",
        )
        source = {
            "id": "ohio-housing-rule",
            "title": "Ohio housing rule",
            "citation": "O.R.C. 5321.04",
            "sourceKind": "court_rules",
            "snippet": "A landlord must maintain fit premises.",
        }
        draft = self.post(
            "/api/advice-letters/drafts/",
            {
                "matterId": self.matter.external_id,
                "sectionSlugs": ["seal"],
                "selectedFactIds": [fact.id],
                "selectedSourceResults": [source],
            },
        ).json()["draft"]

        with patch.object(drafting_ai, "regenerate_section", return_value="AI redrafted advice."):
            response = self.client.post(
                f"/api/drafts/{draft['id']}/blocks/seal/regenerate/",
                data=json.dumps({"instruction": "Make the next step clearer."}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        document = DraftDocument.objects.get(id=draft["id"])
        seal = document.components.get(stable_key="seal")
        versions = list(seal.versions.order_by("sequence"))
        self.assertEqual([version.origin for version in versions], ["template", "ai"])
        self.assertEqual(versions[-1].body, "AI redrafted advice.")
        source_keys = set(versions[-1].source_bindings.values_list("source_key", flat=True))
        self.assertTrue({"advice-letter:seal", f"fact:{fact.id}", "ohio-housing-rule"}.issubset(source_keys))

    def test_advice_editor_changes_are_exported_from_the_saved_draft(self):
        draft_payload = self.post(
            "/api/advice-letters/drafts/",
            {"matterId": self.matter.external_id, "sectionSlugs": ["seal"]},
        ).json()["draft"]
        edited_sections = [
            {**section, "body": "The Court can seal the record after you file the request."}
            if section["key"] == "seal"
            else section
            for section in draft_payload["sections"]
        ]
        patch_response = self.client.patch(
            f"/api/drafts/{draft_payload['id']}/",
            data=json.dumps({"sections": edited_sections, "editorState": {"format": "lexical_blocks", "blocks": {}}, "revision": draft_payload["revision"]}),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)

        response = self.post(
            f"/api/advice-letters/drafts/{draft_payload['id']}/export/",
            {
                "letterFields": {
                    "recipientName": "Maria Alvarez",
                    "letterDate": "August 2, 2026",
                    "subject": "Sealing the record",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Court can seal the record", document)
        self.assertIn("August 2, 2026", document)

    @override_settings(
        LEGALSERVER_BASE_URL="https://example.legalserver.org",
        LEGALSERVER_API_TOKEN="token",
        LEGALSERVER_ALLOW_WRITES=True,
        LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False,
    )
    def test_advice_letter_save_uses_its_download_filename_in_legalserver(self):
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)
        self.matter.raw_payload = {
            **self.matter.raw_payload,
            "matter_uuid": "1f689912-a490-4ced-a99d-a21d7a5caeb2",
        }
        self.matter.save(update_fields=["raw_payload", "updated_at"])
        draft = self.post(
            "/api/advice-letters/drafts/",
            {"matterId": self.matter.external_id, "sectionSlugs": ["seal"]},
        ).json()["draft"]

        with patch("apps.sources.connectors.legalserver.LegalServerClient.upload_matter_document") as upload:
            upload.return_value = {"id": "doc-1"}
            response = self.post(
                f"/api/advice-letters/drafts/{draft['id']}/legalserver/",
                {
                    "letterFields": {
                        "recipientName": "Ms. Alvarez",
                        "letterDate": "August 2, 2026",
                    }
                },
            )

        self.assertEqual(response.status_code, 201)
        filename = "2026-08-02-alvarez-advice-letter-motion-seal.docx"
        self.assertEqual(upload.call_args.kwargs["filename"], filename)
        self.assertEqual(upload.call_args.kwargs["extra_fields"]["name"], filename)

    def test_endpoints_require_a_login(self):
        self.client.logout()

        self.assertIn(
            self.client.get("/api/advice-letters/sections/").status_code, {302, 401, 403}
        )

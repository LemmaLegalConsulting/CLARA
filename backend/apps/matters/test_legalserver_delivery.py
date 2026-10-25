"""Saving generated work product back to the LegalServer case file.

The behavior worth pinning down is what happens when the save cannot succeed.
An advocate who exported a letter still needs the letter, so a LegalServer
outage has to leave the download intact and say so, not raise.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings

from apps.matters.legalserver_delivery import (
    apply_triage_outcome,
    can_deliver,
    delivery_defaults,
    save_case_note,
    save_draft_ai_audit,
    save_document,
    wants_delivery,
)
from apps.drafting.components import record_sections
from apps.drafting.models import DraftDocument, DraftingSession
from apps.matters.legalserver_field_map import load_field_map, triage_outcome_updates
from apps.matters.legalserver_notes import triage_case_note_body
from apps.matters.models import LegalServerDelivery, Matter, TriageAssessment, TriageRubric
from apps.sources.connectors.legalserver import LegalServerClient


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def json(self):
        return self.payload


class RecordingSession:
    """A requests-shaped session that remembers what was written."""

    def __init__(self, payload=None, status_code=200):
        self.payload = payload if payload is not None else {"id": 991}
        self.status_code = status_code
        self.calls = []

    def request(self, method, url, headers=None, json=None, files=None, data=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "json": json, "files": files, "data": data}
        )
        return FakeResponse(self.payload, status_code=self.status_code)


CONFIGURED = dict(
    LEGALSERVER_BASE_URL="https://example.legalserver.org",
    LEGALSERVER_API_TOKEN="token",
    LEGALSERVER_ALLOW_WRITES=True,
)


def legalserver_matter():
    return Matter.objects.create(
        external_id="LS-1",
        client_name="Real Client",
        matter_type="Eviction",
        jurisdiction="Cleveland Housing Court",
        source_system="LegalServer",
        raw_payload={
            "assigned_user_email": "bob@example.org",
            "matter_uuid": "1f689912-a490-4ced-a99d-a21d7a5caeb2",
            "case_id": 9,
        },
    )


class DeliveryDefaultTests(TestCase):
    def test_documents_default_on_and_working_notes_default_off(self):
        defaults = delivery_defaults()

        self.assertTrue(defaults["documents"])
        self.assertFalse(defaults["research"])
        self.assertFalse(defaults["triage"])

    def test_an_absent_flag_keeps_the_default(self):
        self.assertTrue(wants_delivery({}, "documents"))
        self.assertFalse(wants_delivery({}, "research"))

    def test_an_explicit_opt_out_wins_over_the_default(self):
        self.assertFalse(wants_delivery({"saveToLegalServer": False}, "documents"))
        self.assertFalse(wants_delivery({"saveToLegalServer": "0"}, "documents"))
        self.assertTrue(wants_delivery({"saveToLegalServer": True}, "research"))


class DeliveryEligibilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    @override_settings(**{**CONFIGURED, "LEGALSERVER_ALLOW_WRITES": False})
    def test_a_server_with_writes_turned_off_sends_nothing(self):
        """A test run must never be able to write to a real case file."""
        ok, reason = can_deliver(legalserver_matter())

        self.assertFalse(ok)
        self.assertEqual(reason, "writes_disabled")

    @override_settings(**CONFIGURED)
    def test_a_case_with_no_numeric_id_cannot_take_a_note(self):
        """Documents and notes name a matter differently, so each is checked."""
        matter = Matter.objects.create(
            external_id="LS-NO-ID",
            client_name="Real Client",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="LegalServer",
            raw_payload={"matter_uuid": "1f689912-a490-4ced-a99d-a21d7a5caeb2"},
        )

        delivery = save_case_note(
            matter, user=self.user, title="Note", body="Body", origin="research"
        )

        self.assertEqual(delivery.status, LegalServerDelivery.SKIPPED)
        self.assertEqual(delivery.reason, "authorization_unavailable_or_revoked")

    @override_settings(**CONFIGURED)
    def test_a_case_with_no_matter_uuid_is_not_addressed_by_case_number(self):
        """The v2 write API identifies a matter by UUID, not by case number.

        Sending the case number would either fail confusingly or match the
        wrong record, so an unknown UUID stops the write.
        """
        matter = Matter.objects.create(
            external_id="26-000034",
            client_name="Real Client",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="LegalServer",
            raw_payload={"case_number": "26-000034"},
        )

        ok, reason = can_deliver(matter)

        self.assertFalse(ok)
        self.assertEqual(reason, "no_matter_uuid")

    @override_settings(**CONFIGURED)
    def test_a_case_number_in_the_uuid_field_is_not_mistaken_for_a_uuid(self):
        matter = Matter.objects.create(
            external_id="26-000035",
            client_name="Real Client",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="LegalServer",
            raw_payload={"matter_uuid": "26-000035"},
        )

        self.assertEqual(can_deliver(matter), (False, "no_matter_uuid"))

    @override_settings(**CONFIGURED)
    def test_a_quick_case_has_no_legalserver_matter_to_write_to(self):
        matter = Matter.objects.create(
            external_id="MANUAL-1",
            client_name="Typed by hand",
            matter_type="Eviction",
            jurisdiction="Cleveland",
            source_system="Manual",
        )

        ok, reason = can_deliver(matter)

        self.assertFalse(ok)
        self.assertEqual(reason, "local_case")

    @override_settings(LEGALSERVER_BASE_URL="", LEGALSERVER_API_TOKEN="", LEGALSERVER_ALLOW_WRITES=True)
    def test_an_unconfigured_site_cannot_be_written_to(self):
        ok, reason = can_deliver(legalserver_matter())

        self.assertFalse(ok)
        self.assertEqual(reason, "not_configured")

    @override_settings(LEGALSERVER_BASE_URL="", LEGALSERVER_API_TOKEN="", LEGALSERVER_ALLOW_WRITES=True)
    def test_an_unconfigured_site_records_the_skip_instead_of_raising(self):
        matter = legalserver_matter()

        delivery = save_document(
            matter,
            user=self.user,
            filename="answer.docx",
            content=b"docx bytes",
            origin="draft_export",
        )

        self.assertEqual(delivery.status, LegalServerDelivery.SKIPPED)
        self.assertEqual(delivery.reason, "not_configured")


@override_settings(**CONFIGURED)
class CaseNoteDeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        self.matter = legalserver_matter()
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    def test_a_note_reaches_the_matter_and_is_recorded(self):
        session = RecordingSession({"data": {"id": 4242, "note_uuid": "note-uuid"}})

        delivery = save_case_note(
            self.matter,
            user=self.user,
            title="AI research: habitability",
            body="Answer text",
            origin="research",
            client=LegalServerClient(session=session),
        )

        self.assertEqual(delivery.status, LegalServerDelivery.SAVED)
        self.assertEqual(delivery.remote_id, "4242")
        self.assertEqual(delivery.origin, "research")
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://example.legalserver.org/api/v2/notes")
        # The notes endpoint takes the numeric matter id, not the UUID, and
        # rejects a request with no note_type. Both were confirmed against a
        # live site and contradict the published request schema.
        self.assertEqual(
            session.calls[0]["json"],
            {
                "module": "matter",
                "module_id": "9",
                "subject": "AI research: habitability",
                "body": "Answer text",
                "note_type": "Case Notes",
                "is_html": False,
            },
        )

    def test_a_rejected_note_is_recorded_as_failed_rather_than_raised(self):
        client = LegalServerClient(session=RecordingSession({"detail": "no such matter"}, status_code=404))

        delivery = save_case_note(
            self.matter,
            user=self.user,
            title="AI research",
            body="Answer text",
            origin="research",
            client=client,
        )

        self.assertEqual(delivery.status, LegalServerDelivery.FAILED)
        self.assertIn("404", delivery.reason)

    def test_an_opt_out_writes_nothing_and_records_nothing(self):
        delivery = save_case_note(
            self.matter,
            user=self.user,
            title="AI research",
            body="Answer text",
            origin="research",
            requested=False,
        )

        self.assertIsNone(delivery)
        self.assertEqual(LegalServerDelivery.objects.count(), 0)

    def test_a_draft_ai_audit_note_is_updated_by_stable_external_id(self):
        session = DraftingSession.objects.create(mode="draft_from_template", matter=self.matter)
        draft = DraftDocument.objects.create(session=session, title="Answer", sections=[], plain_text="")
        record_sections(
            draft,
            [{"key": "argument", "label": "Argument", "body": "AI-created paragraph."}],
            origin="ai",
        )
        first_session = RecordingSession({"data": {"id": 4242}})
        first = save_draft_ai_audit(
            draft,
            user=self.user,
            client=LegalServerClient(session=first_session),
        )
        second_session = RecordingSession({"data": {"id": 4242}})
        second = save_draft_ai_audit(
            draft,
            user=self.user,
            client=LegalServerClient(session=second_session),
        )

        scope = f"ai-audit:draft:{draft.id}"
        self.assertEqual(first.scope_key, scope)
        self.assertTrue(second.updated_existing)
        self.assertEqual(second_session.calls[0]["json"]["external_id"], scope)
        self.assertEqual(second_session.calls[0]["json"]["update"], {"external_id": scope})
        self.assertIn("AI-created paragraph.", second_session.calls[0]["json"]["body"])


@override_settings(**CONFIGURED)
class DocumentDeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        self.matter = legalserver_matter()
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    def test_a_document_is_uploaded_as_multipart_with_its_filename(self):
        session = RecordingSession({"data": {"id": "doc-9"}})

        delivery = save_document(
            self.matter,
            user=self.user,
            filename="answer-and-counterclaims.docx",
            content=b"docx bytes",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            title="Answer and counterclaims",
            origin="draft_export",
            client=LegalServerClient(session=session),
        )

        self.assertEqual(delivery.status, LegalServerDelivery.SAVED)
        self.assertEqual(delivery.remote_id, "doc-9")
        call = session.calls[0]
        self.assertEqual(call["url"], "https://example.legalserver.org/api/v2/documents")
        self.assertEqual(call["files"]["file"][0], "answer-and-counterclaims.docx")
        self.assertEqual(call["files"]["file"][1], b"docx bytes")
        self.assertEqual(call["data"]["module"], "matter")
        self.assertEqual(call["data"]["module_uuid"], "1f689912-a490-4ced-a99d-a21d7a5caeb2")
        self.assertEqual(call["data"]["name"], "answer-and-counterclaims.docx")
        self.assertEqual(call["data"]["title"], "Answer and counterclaims")
        self.assertNotIn("type", call["data"])

    def test_the_legalserver_name_matches_the_actual_filename_including_extension(self):
        session = RecordingSession({"data": {"id": "doc-10"}})

        delivery = save_document(
            self.matter,
            user=self.user,
            filename="answer.docx",
            content=b"docx bytes",
            title="Answer and counterclaims",
            scope_key="draft-export:draft:42",
            origin="draft_export",
            client=LegalServerClient(session=session),
        )

        self.assertEqual(session.calls[0]["data"]["name"], "answer.docx")
        self.assertEqual(session.calls[0]["data"]["title"], "Answer and counterclaims")
        self.assertEqual(delivery.request_payload["remoteName"], "answer.docx")

    @override_settings(LEGALSERVER_DOCUMENT_TYPE="Brief")
    def test_a_configured_document_type_is_applied_to_the_upload(self):
        session = RecordingSession({"data": {"id": "doc-9"}})

        save_document(
            self.matter,
            user=self.user,
            filename="answer.docx",
            content=b"docx bytes",
            origin="draft_export",
            client=LegalServerClient(session=session),
        )

        self.assertEqual(session.calls[0]["data"]["type"], "Brief")


class TriageFieldMapTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        self.matter = legalserver_matter()
        self.rubric = TriageRubric.objects.create(
            slug="test-priority-rubric",
            name="Cleveland RTC priority",
            standard="...",
            criteria=["A tenant facing eviction"],
        )
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    def field_map(self, *, enabled, dry_run):
        return {
            "slug": "test",
            "enabled": enabled,
            "dry_run": dry_run,
            "rules": [{"name": "always", "when": {}, "set": {"case_status": "Screened"}, "custom_fields": {}}],
        }

    def assessment(self, **overrides):
        return TriageAssessment.objects.create(
            matter=self.matter,
            rubric=self.rubric,
            created_by=self.user,
            case_type="Eviction",
            priority=overrides.pop("priority", True),
            priority_label=overrides.pop("priority_label", "Full rep"),
            confidence=overrides.pop("confidence", "Likely full rep"),
            summary="Tenant has a strong defense.",
            matched_criteria=["A tenant facing eviction"],
            missing_information=[],
            **overrides,
        )

    def test_the_shipped_map_is_valid_and_turned_off(self):
        field_map = load_field_map("triage-outcome")

        self.assertIsNotNone(field_map)
        self.assertFalse(field_map["enabled"])
        self.assertTrue(field_map["dry_run"])

    def test_rules_matching_the_outcome_contribute_their_fields(self):
        outcome = triage_outcome_updates(
            self.assessment(),
            field_map={
                "slug": "test",
                "enabled": True,
                "dry_run": False,
                "rules": [
                    {"name": "always", "when": {}, "set": {"case_status": "Screened"}, "custom_fields": {}},
                    {
                        "name": "full-rep",
                        "when": {"priority_label": ["Full rep"]},
                        "set": {},
                        "custom_fields": {"ai_triage_outcome": "{priority_label} ({confidence})"},
                    },
                    {
                        "name": "brief-advice",
                        "when": {"priority_label": ["Brief advice"]},
                        "set": {"case_status": "Advice only"},
                        "custom_fields": {},
                    },
                ],
            },
        )

        self.assertEqual(outcome.matched_rules, ["always", "full-rep"])
        self.assertEqual(
            outcome.as_payload(),
            {
                "case_status": "Screened",
                "custom_fields": {"ai_triage_outcome": "Full rep (Likely full rep)"},
            },
        )

    def test_a_missing_map_is_reported_rather_than_guessed(self):
        outcome = triage_outcome_updates(self.assessment(), slug="does-not-exist")

        self.assertEqual(outcome.error, "no_field_map")
        self.assertEqual(outcome.as_payload(), {})

    def test_a_placeholder_no_assessment_supplies_is_rejected_at_load(self):
        path = Path(self.temp.name) / "broken.yaml"
        path.write_text(
            "slug: broken\nenabled: true\nrules:\n"
            "  - name: r\n    set:\n      case_status: '{unknown_field}'\n",
            encoding="utf-8",
        )

        with patch("apps.matters.legalserver_field_map.content_path", return_value=path):
            with self.assertRaises(ValueError) as raised:
                load_field_map("broken")

        self.assertIn("unknown placeholder", str(raised.exception))

    def test_a_rule_testing_a_condition_that_does_not_exist_is_rejected(self):
        path = Path(self.temp.name) / "bad-condition.yaml"
        path.write_text(
            "slug: bad\nrules:\n  - name: r\n    when:\n      priorty: true\n    set: {}\n",
            encoding="utf-8",
        )

        with patch("apps.matters.legalserver_field_map.content_path", return_value=path):
            with self.assertRaises(ValueError) as raised:
                load_field_map("bad-condition")

        self.assertIn("unknown conditions", str(raised.exception))

    def test_a_dry_run_records_the_values_without_writing(self):
        session = RecordingSession()

        with override_settings(**CONFIGURED):
            delivery = apply_triage_outcome(
                self.matter,
                self.assessment(),
                user=self.user,
                client=LegalServerClient(session=session),
                field_map=self.field_map(enabled=True, dry_run=True),
            )

        self.assertEqual(delivery.status, LegalServerDelivery.DRY_RUN)
        self.assertEqual(delivery.request_payload["fields"], {"case_status": "Screened"})
        self.assertEqual(session.calls, [])

    def test_the_shipped_map_names_no_fields_so_nothing_is_sent(self):
        session = RecordingSession()

        with override_settings(**CONFIGURED):
            delivery = apply_triage_outcome(
                self.matter,
                self.assessment(),
                user=self.user,
                client=LegalServerClient(session=session),
            )

        self.assertEqual(delivery.status, LegalServerDelivery.SKIPPED)
        self.assertEqual(delivery.reason, "no_updates")
        self.assertEqual(session.calls, [])

    def test_a_disabled_map_with_fields_is_skipped_and_says_why(self):
        session = RecordingSession()

        with override_settings(**CONFIGURED):
            delivery = apply_triage_outcome(
                self.matter,
                self.assessment(),
                user=self.user,
                client=LegalServerClient(session=session),
                field_map=self.field_map(enabled=False, dry_run=False),
            )

        self.assertEqual(delivery.status, LegalServerDelivery.SKIPPED)
        self.assertEqual(delivery.reason, "field_map_disabled")
        self.assertEqual(delivery.request_payload["fields"], {"case_status": "Screened"})
        self.assertEqual(session.calls, [])

    def test_an_enabled_map_writes_the_fields_to_the_matter(self):
        session = RecordingSession()

        with override_settings(**CONFIGURED):
            delivery = apply_triage_outcome(
                self.matter,
                self.assessment(),
                user=self.user,
                client=LegalServerClient(session=session),
                field_map=self.field_map(enabled=True, dry_run=False),
            )

        self.assertEqual(delivery.status, LegalServerDelivery.SAVED)
        self.assertEqual(session.calls[0]["method"], "PATCH")
        self.assertEqual(session.calls[0]["url"], "https://example.legalserver.org/api/v2/matters/1f689912-a490-4ced-a99d-a21d7a5caeb2")
        self.assertEqual(session.calls[0]["json"], {"case_status": "Screened"})


class TriageCaseNoteBodyTests(TestCase):
    def test_the_note_says_it_has_not_been_reviewed(self):
        matter = legalserver_matter()
        rubric = TriageRubric.objects.create(slug="r", name="Rubric", standard="...", criteria=[])
        assessment = TriageAssessment.objects.create(
            matter=matter,
            rubric=rubric,
            priority=True,
            priority_label="Full rep",
            confidence="Likely full rep",
            summary="Strong defense.",
            matched_criteria=["Tenant faces eviction"],
            missing_information=["Lease is missing"],
        )

        body = triage_case_note_body(assessment)

        self.assertIn("Full rep", body)
        self.assertIn("- Lease is missing", body)
        self.assertIn("has not been reviewed", body)


@override_settings(
    LEGALSERVER_BASE_URL="https://example.legalserver.org",
    LEGALSERVER_API_TOKEN="token",
    LEGALSERVER_ALLOW_WRITES=True,
    LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False,
)
class TriageEndpointDeliveryTests(TestCase):
    """The triage endpoint's two LegalServer outcomes, reported separately."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "quinten", "quinten@example.org", "pw", is_staff=True
        )
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(
            external_id="LS-2",
            client_name="Real Client",
            matter_type="Eviction",
            jurisdiction="Cleveland Housing Court",
            source_system="LegalServer",
            raw_payload={
                "assigned_user_email": "quinten@example.org",
                "matter_uuid": "1f689912-a490-4ced-a99d-a21d7a5caeb2",
                "case_id": 9,
            },
        )
        self.rubric = TriageRubric.objects.create(
            slug="endpoint-rubric", name="Rubric", standard="...", criteria=["A criterion"]
        )
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    def run_triage(self, payload):
        def fake_triage(matter, *, rubric, user=None):
            return TriageAssessment.objects.create(
                matter=matter,
                rubric=rubric,
                created_by=user,
                priority=True,
                priority_label="Full rep",
                confidence="Likely full rep",
                summary="Strong defense.",
            )

        with patch("apps.matters.views.run_triage", side_effect=fake_triage):
            with patch("apps.sources.connectors.legalserver.LegalServerClient.create_note") as note:
                note.return_value = {"id": 77}
                response = self.client.post(
                    f"/api/cases/{self.matter.external_id}/triage/",
                    data=payload,
                    content_type="application/json",
                )
        return response, note

    def test_the_assessment_is_not_filed_unless_it_is_asked_for(self):
        response, note = self.run_triage({"rubricId": self.rubric.id})

        self.assertEqual(response.status_code, 201)
        note.assert_not_called()
        self.assertIsNone(response.json()["legalserver"]["casenote"])

    def test_asking_to_save_files_the_assessment_as_a_case_note(self):
        response, note = self.run_triage({"rubricId": self.rubric.id, "saveToLegalServer": True})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["legalserver"]["casenote"]["status"], "saved")
        self.assertIn("Full rep", note.call_args.kwargs["subject"])
        self.assertIn("has not been reviewed", note.call_args.kwargs["body"])

    def test_the_case_property_hook_reports_that_no_fields_are_mapped_yet(self):
        response, _ = self.run_triage({"rubricId": self.rubric.id})

        case_update = response.json()["legalserver"]["caseUpdate"]
        self.assertEqual(case_update["status"], "skipped")
        self.assertEqual(case_update["reason"], "no_updates")


@override_settings(**CONFIGURED)
class ScopedUpdateTests(TestCase):
    """A revised artifact replaces what it filed before, rather than adding a copy.

    An advocate rewrites an advice letter several times in one sitting. Each
    revision filing itself separately would leave the case holding five letters
    with no way to tell which one was sent.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        self.matter = legalserver_matter()
        from apps.matters.testing import responding_legalserver
        responding_legalserver(self)

    def test_a_second_note_under_one_scope_key_replaces_the_first(self):
        session = RecordingSession({"data": {"id": 4176}})
        client = LegalServerClient(session=session)

        first = save_case_note(
            self.matter, user=self.user, title="Case chat", body="One",
            origin="case_chat", scope_key="case-chat:LS-1:7", client=client,
        )
        second = save_case_note(
            self.matter, user=self.user, title="Case chat", body="One and two",
            origin="case_chat", scope_key="case-chat:LS-1:7", client=client,
        )

        self.assertFalse(first.updated_existing)
        self.assertTrue(second.updated_existing)
        # The first call claims the external id; the second asks to replace it.
        self.assertEqual(session.calls[0]["json"]["external_id"], "case-chat:LS-1:7")
        self.assertNotIn("update", session.calls[0]["json"])
        self.assertEqual(session.calls[1]["json"]["update"], {"external_id": "case-chat:LS-1:7"})

    def test_a_different_thread_files_its_own_note(self):
        client = LegalServerClient(session=RecordingSession({"data": {"id": 1}}))

        save_case_note(
            self.matter, user=self.user, title="Chat", body="One",
            origin="case_chat", scope_key="case-chat:LS-1:7", client=client,
        )
        other = save_case_note(
            self.matter, user=self.user, title="Chat", body="Two",
            origin="case_chat", scope_key="case-chat:LS-1:8", client=client,
        )

        self.assertFalse(other.updated_existing)

    def test_a_replaced_document_scopes_the_match_to_this_matter(self):
        """An unscoped match moves another case's document onto this one.

        Observed against a live site: posting update[name] alone from a
        different matter matched a document on the first case and reattached it
        to the second. The module must be part of the match.
        """
        session = RecordingSession({"data": {"id": 1164}})
        client = LegalServerClient(session=session)

        for body in (b"one", b"two"):
            save_document(
                self.matter, user=self.user, filename="letter.docx", content=body,
                title="Advice letter", origin="advice_letter",
                scope_key="advice-letter:draft:5", client=client,
            )

        replace = session.calls[1]["data"]
        self.assertEqual(replace["update[name]"], "letter.docx")
        self.assertEqual(replace["update[module]"], "matter")
        self.assertEqual(replace["update[module_uuid]"], "1f689912-a490-4ced-a99d-a21d7a5caeb2")
        # The first upload has nothing to replace.
        self.assertNotIn("update[name]", session.calls[0]["data"])

    def test_an_unscoped_save_still_files_a_separate_copy(self):
        client = LegalServerClient(session=RecordingSession({"data": {"id": 7}}))

        second = save_case_note(
            self.matter, user=self.user, title="Note", body="Body",
            origin="research", client=client,
        )

        self.assertFalse(second.updated_existing)

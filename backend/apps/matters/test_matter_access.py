"""Per-matter access control boundaries.

Enabling sample data and running with DEBUG are configuration conveniences.
Neither is a statement about who may read a real client's case file.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings

from apps.matters.models import Matter
from apps.matters.seed import seed_matters
from apps.matters.services import (
    DEMO_SOURCE_SYSTEM,
    payload_matches_legalserver_identifier,
    user_can_access_matter,
)


def legalserver_matter(external_id, payload):
    return Matter.objects.create(
        external_id=external_id,
        client_name="Real Client",
        matter_type="Eviction",
        jurisdiction="Cleveland Housing Court",
        source_system="LegalServer",
        raw_payload=payload,
    )


@override_settings(LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False)
class MatterAccessScopeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")

    @override_settings(ENABLE_DEMO_MATTERS=True)
    def test_demo_data_does_not_open_real_matters(self):
        matter = legalserver_matter("LS-REAL", {"assigned_user_email": "someone-else@example.org"})
        self.assertFalse(user_can_access_matter(self.user, matter))

    @override_settings(ENABLE_DEMO_MATTERS=True)
    def test_seeded_sample_matters_stay_readable(self):
        seed_matters()
        demo = Matter.objects.filter(source_system=DEMO_SOURCE_SYSTEM).first()
        self.assertIsNotNone(demo)
        self.assertTrue(user_can_access_matter(self.user, demo))

    @override_settings(ENABLE_DEMO_MATTERS=False)
    def test_sample_matters_are_hidden_when_demo_data_is_off(self):
        seed_matters()
        demo = Matter.objects.filter(source_system=DEMO_SOURCE_SYSTEM).first()
        self.assertFalse(user_can_access_matter(self.user, demo))

    @override_settings(ENABLE_DEMO_MATTERS=False, DEBUG=True)
    def test_debug_does_not_open_real_matters(self):
        matter = legalserver_matter("LS-REAL-2", {"assigned_user_email": "someone-else@example.org"})
        self.assertFalse(user_can_access_matter(self.user, matter))

    @override_settings(ENABLE_DEMO_MATTERS=False)
    def test_staff_do_not_receive_all_case_access(self):
        staff = get_user_model().objects.create_user("admin", "admin@example.org", "pw", is_staff=True)
        matter = legalserver_matter("LS-REAL-3", {"assigned_user_email": "someone-else@example.org"})
        self.assertFalse(user_can_access_matter(staff, matter))


class LegalServerIdentifierMatchTests(TestCase):
    def test_matches_the_assigned_advocate(self):
        payload = {"assigned_user_email": "bob@example.org"}
        self.assertTrue(payload_matches_legalserver_identifier(payload, "bob@example.org"))

    def test_matches_a_decorated_identity_value(self):
        payload = {"assigned_user": "Bob Smith <bob@example.org>"}
        self.assertTrue(payload_matches_legalserver_identifier(payload, "bob@example.org"))

    def test_matches_a_bare_username(self):
        payload = {"assignments": [{"user": {"user_name": "bob"}}]}
        self.assertTrue(payload_matches_legalserver_identifier(payload, "bob@example.org"))

    def test_does_not_match_a_longer_address_containing_the_identifier(self):
        payload = {"assigned_user_email": "robert.bobson@example.org"}
        self.assertFalse(payload_matches_legalserver_identifier(payload, "bob@example.org"))

    def test_does_not_match_a_colleague_named_in_assignment_notes(self):
        payload = {
            "assignments": [
                {"email": "carol@example.org", "notes": "Case reassigned from bob to carol"}
            ]
        }
        self.assertFalse(payload_matches_legalserver_identifier(payload, "bob@example.org"))

    def test_does_not_match_an_unrelated_case(self):
        payload = {"assigned_user_email": "carol@example.org"}
        self.assertFalse(payload_matches_legalserver_identifier(payload, "bob@example.org"))

    def test_blank_identifier_never_matches(self):
        self.assertFalse(payload_matches_legalserver_identifier({"assigned_user_email": "bob@example.org"}, ""))

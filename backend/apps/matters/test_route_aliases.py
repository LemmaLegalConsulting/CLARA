"""Readable case numbers in URLs must open exactly one matter, or none.

A route key that opens the wrong client's file is worse than a broken link, so
these tests pin the collision, renumbering, and access rules as much as the
happy path.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.matters.models import Matter, MatterRouteAlias
from apps.matters.route_aliases import (
    is_remote_lookup_key,
    normalize_route_key,
    resolve_matter_route_key,
    route_key_for_matter,
    sync_matter_route_alias,
)
from apps.matters.serializers import matter_to_dict
from apps.matters.services import upsert_matter_from_legalserver
from apps.sources.models import UserSourceIdentity


class FixtureLegalServerClient:
    """Models a responding upstream with the synthetic route-test records."""

    configured = True
    user_filter_param = "assigned_user_email"
    matter_profile_url = None

    def find_user(self, identifier):
        return {"email": identifier}

    def get_matter(self, matter_id):
        matter = Matter.objects.filter(external_id=matter_id).first()
        return dict(matter.raw_payload) if matter else {}



offline = patch("apps.matters.services.LegalServerClient", FixtureLegalServerClient)


def make_matter(external_id, case_number=None, *, assigned="bob@example.org", source_system="LegalServer"):
    payload = {"assigned_user_email": assigned}
    if case_number is not None:
        payload["case_number"] = case_number
    matter = Matter.objects.create(
        external_id=external_id,
        client_name=f"Client {external_id}",
        matter_type="Eviction",
        jurisdiction="Cleveland Housing Court",
        source_system=source_system,
        raw_payload=payload,
    )
    sync_matter_route_alias(matter)
    return matter


def renumber(matter, case_number):
    matter.raw_payload = {**matter.raw_payload, "case_number": case_number}
    matter.save()
    sync_matter_route_alias(matter)


@offline
@override_settings(LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False, ENABLE_DEMO_MATTERS=False)
class RouteAliasTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        UserSourceIdentity.objects.create(is_verified=True, user=self.user, provider="legalserver", identifier="bob@example.org")

    def test_case_number_becomes_the_route_key(self):
        matter = make_matter("MID-1", "26-0222")
        self.assertEqual(route_key_for_matter(matter), "26-0222")
        self.assertEqual(resolve_matter_route_key(self.user, "26-0222"), matter)

    def test_external_id_still_resolves(self):
        matter = make_matter("MID-1", "26-0222")
        self.assertEqual(resolve_matter_route_key(self.user, "MID-1"), matter)

    def test_matter_without_a_case_number_routes_by_external_id(self):
        matter = make_matter("MANUAL-7-abc", None)
        self.assertFalse(MatterRouteAlias.objects.filter(matter=matter).exists())
        self.assertEqual(route_key_for_matter(matter), "MANUAL-7-abc")
        self.assertEqual(resolve_matter_route_key(self.user, "MANUAL-7-abc"), matter)

    def test_renumbered_matter_keeps_its_old_link(self):
        matter = make_matter("MID-1", "26-0222")
        renumber(matter, "26-0999")
        self.assertEqual(route_key_for_matter(matter), "26-0999")
        self.assertEqual(resolve_matter_route_key(self.user, "26-0222"), matter)
        self.assertEqual(resolve_matter_route_key(self.user, "26-0999"), matter)
        old = MatterRouteAlias.objects.get(alias="26-0222")
        self.assertFalse(old.is_current)

    def test_renumbering_back_reuses_the_old_alias(self):
        matter = make_matter("MID-1", "26-0222")
        renumber(matter, "26-0999")
        renumber(matter, "26-0222")
        self.assertEqual(route_key_for_matter(matter), "26-0222")
        self.assertEqual(MatterRouteAlias.objects.filter(matter=matter).count(), 2)
        self.assertEqual(MatterRouteAlias.objects.filter(matter=matter, is_current=True).count(), 1)

    def test_colliding_case_number_never_opens_the_other_matter(self):
        first = make_matter("MID-1", "26-0222")
        with self.assertLogs("apps.matters.route_aliases", level="WARNING"):
            second = make_matter("MID-2", "26-0222")
        self.assertEqual(route_key_for_matter(second), "MID-2")
        self.assertEqual(resolve_matter_route_key(self.user, "26-0222"), first)
        self.assertEqual(resolve_matter_route_key(self.user, "MID-2"), second)

    def test_case_number_equal_to_another_matters_external_id_is_not_taken(self):
        owner = make_matter("26-0222", None)
        with self.assertLogs("apps.matters.route_aliases", level="WARNING"):
            other = make_matter("MID-2", "26-0222")
        self.assertEqual(route_key_for_matter(other), "MID-2")
        self.assertEqual(resolve_matter_route_key(self.user, "26-0222"), owner)

    def test_external_id_claims_back_a_shadowing_alias(self):
        aliased = make_matter("MID-1", "26-0222")
        with self.assertLogs("apps.matters.route_aliases", level="WARNING"):
            owner = make_matter("26-0222", None)
        self.assertEqual(route_key_for_matter(aliased), "MID-1")
        self.assertEqual(resolve_matter_route_key(self.user, "26-0222"), owner)
        self.assertEqual(resolve_matter_route_key(self.user, "MID-1"), aliased)

    def test_normalization_ignores_case_and_surrounding_space_but_keeps_punctuation(self):
        matter = make_matter("MID-1", "26-AB22")
        self.assertEqual(resolve_matter_route_key(self.user, " 26-ab22 "), matter)
        self.assertIsNone(resolve_matter_route_key(self.user, "26AB22"))
        self.assertEqual(normalize_route_key("２６-０２２２"), "26-0222")

    def test_inaccessible_matter_resolves_like_an_unknown_one(self):
        make_matter("MID-9", "26-0333", assigned="carol@example.org")
        self.assertIsNone(resolve_matter_route_key(self.user, "26-0333"))
        self.assertIsNone(resolve_matter_route_key(self.user, "MID-9"))
        self.assertIsNone(resolve_matter_route_key(self.user, "26-4444"))

    def test_upsert_from_legalserver_records_the_alias(self):
        matter = upsert_matter_from_legalserver(
            {"matter_identification_number": "MID-5", "case_number": "26-0555", "client_name": "Pat"}
        )
        self.assertEqual(route_key_for_matter(matter), "26-0555")

    def test_serializer_carries_route_case_key_beside_the_identity(self):
        matter = make_matter("MID-1", "26-0222")
        data = matter_to_dict(matter, legalserver_client=FixtureLegalServerClient())
        self.assertEqual(data["id"], "MID-1")
        self.assertEqual(data["caseNumber"], "26-0222")
        self.assertEqual(data["routeCaseKey"], "26-0222")

    def test_remote_lookup_refuses_keys_that_could_leave_their_path_segment(self):
        self.assertTrue(is_remote_lookup_key("26-0222"))
        for key in ("", "../users", "a/b", "a\\b", "26..0222", ".hidden", "x" * 121, "26 0222"):
            self.assertFalse(is_remote_lookup_key(key), key)


@offline
@patch("apps.sources.connectors.legalserver.LegalServerClient", FixtureLegalServerClient)
@override_settings(LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False, ENABLE_DEMO_MATTERS=False)
class CaseByRouteKeyApiTests(TestCase):
    url = "/api/cases/by-route-key/"

    def setUp(self):
        self.user = get_user_model().objects.create_user("bob", "bob@example.org", "pw")
        UserSourceIdentity.objects.create(is_verified=True, user=self.user, provider="legalserver", identifier="bob@example.org")
        self.client.force_login(self.user)

    def test_opens_a_case_by_its_readable_number(self):
        make_matter("MID-1", "26-0222")
        response = self.client.get(self.url, {"key": "26-0222"})
        self.assertEqual(response.status_code, 200)
        case = response.json()["case"]
        self.assertEqual(case["id"], "MID-1")
        self.assertEqual(case["routeCaseKey"], "26-0222")
        self.assertIn("facts", case)

    def test_old_number_opens_the_case_and_reports_the_current_key(self):
        matter = make_matter("MID-1", "26-0222")
        renumber(matter, "26-0999")
        response = self.client.get(self.url, {"key": "26-0222"})
        self.assertEqual(response.json()["case"]["routeCaseKey"], "26-0999")

    @patch("apps.matters.views.sync_legalserver_matters_for_user")
    def test_unknown_and_inaccessible_cases_answer_identically(self, _sync):
        make_matter("MID-9", "26-0333", assigned="carol@example.org")
        hidden = self.client.get(self.url, {"key": "26-0333"})
        unknown = self.client.get(self.url, {"key": "26-4444"})
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(hidden.json(), unknown.json())

    @patch("apps.matters.views.sync_legalserver_matter")
    @patch("apps.matters.views.sync_legalserver_matters_for_user")
    def test_a_case_not_yet_imported_is_found_by_search(self, search, fetch):
        # LegalServer's matter endpoint takes only a UUID; a case number is
        # found by searching, which is the access-filtered case-list sync.
        search.side_effect = lambda user, query, **kwargs: make_matter("MID-7", query)
        response = self.client.get(self.url, {"key": "26-0777"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["case"]["id"], "MID-7")
        search.assert_called_once_with(self.user, query="26-0777", limit=5, restrict_to_user=False)
        fetch.assert_not_called()

    @patch("apps.matters.views.sync_legalserver_matter")
    @patch("apps.matters.views.sync_legalserver_matters_for_user")
    def test_a_uuid_key_is_fetched_directly_when_search_misses(self, search, fetch):
        key = "0f8fad5b-d9cb-469f-a165-70867728950e"
        fetch.side_effect = lambda matter_id, user=None: make_matter(matter_id, None)
        response = self.client.get(self.url, {"key": key})
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with(key, user=self.user)

    @patch("apps.matters.views.sync_legalserver_matter")
    @patch("apps.matters.views.sync_legalserver_matters_for_user")
    def test_unsafe_key_is_never_sent_to_legalserver(self, search, fetch):
        response = self.client.get(self.url, {"key": "../users"})
        self.assertEqual(response.status_code, 404)
        search.assert_not_called()
        fetch.assert_not_called()

    def test_key_is_required(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_is_read_only(self):
        make_matter("MID-1", "26-0222")
        self.assertEqual(self.client.post(f"{self.url}?key=26-0222").status_code, 405)

    def test_requires_sign_in(self):
        self.client.logout()
        self.assertIn(self.client.get(self.url, {"key": "26-0222"}).status_code, (401, 403))


@offline
class RouteAliasWriteTests(TestCase):
    """Re-syncing an unchanged matter must not write: the case list does it for every row."""

    def test_resyncing_an_unchanged_matter_writes_nothing(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        matter = make_matter("MID-1", "26-0222")
        with CaptureQueriesContext(connection) as queries:
            sync_matter_route_alias(matter)
        writes = [q["sql"] for q in queries if not q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(writes, [])

    def test_a_matter_without_a_number_writes_nothing_either(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        matter = make_matter("MANUAL-1", None)
        with CaptureQueriesContext(connection) as queries:
            sync_matter_route_alias(matter)
        writes = [q["sql"] for q in queries if not q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(writes, [])


class ConfiguredLegalServerClient(FixtureLegalServerClient):
    configured = True

    def find_user(self, identifier):
        return {}


@patch("apps.matters.views.matter_services.LegalServerClient", ConfiguredLegalServerClient)
@patch("apps.matters.services.LegalServerClient", ConfiguredLegalServerClient)
@patch("apps.matters.views.sync_legalserver_matters_for_user")
@override_settings(ENABLE_DEMO_MATTERS=False)
class UnreachableReasonTests(TestCase):
    url = "/api/cases/by-route-key/"

    def setUp(self):
        self.user = get_user_model().objects.create_user("carol", "carol@example.org", "pw")
        self.client.force_login(self.user)

    def test_an_unconnected_account_is_told_so_for_any_case_number(self, _sync):
        make_matter("MID-1", "26-0222", assigned="carol@example.org")
        real = self.client.get(self.url, {"key": "26-0222"})
        invented = self.client.get(self.url, {"key": "26-9999"})
        self.assertEqual(real.status_code, 404)
        # Identical answers: the reason is about the account, not the case.
        self.assertEqual(real.json(), invented.json())
        self.assertEqual(real.json()["reason"], "legalserver_not_connected")

    @override_settings(LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=True)
    def test_a_mismatched_identity_is_told_so(self, _sync):
        UserSourceIdentity.objects.create(is_verified=True, user=self.user, provider="legalserver", identifier="someone-else@example.org")
        response = self.client.get(self.url, {"key": "26-0222"})
        self.assertEqual(response.json()["reason"], "legalserver_identity_mismatch")

    @override_settings(LEGALSERVER_REQUIRE_OFFICE365_EMAIL_MATCH=False)
    def test_an_unverified_account_is_told_identity_could_not_be_verified(self, _sync):
        UserSourceIdentity.objects.create(is_verified=True, user=self.user, provider="legalserver", identifier="carol@example.org")
        response = self.client.get(self.url, {"key": "26-9999"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["reason"], "legalserver_identity_mismatch")

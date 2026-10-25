from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.matters.models import Matter
from apps.sources.connectors.legalserver import LegalServerError
from apps.sources.models import UserSourceIdentity


def legalserver_matter(index, *, closed=False, problem="63 Private Landlord/Tenant", assigned=True, activity="08"):
    return {
        "id": f"LS-{index:03d}",
        "case_number": f"26-{index:04d}",
        "client_full_name": f"Client {index:03d}",
        "client_name": f"Client {index:03d}",
        "matter_type": problem,
        "legal_problem_code": problem,
        "court": "Housing Court",
        "case_disposition": "Closed" if closed else "Open",
        "date_closed": {"raw_value": "2026-05-01" if closed else None, "text_value": ""},
        "date_opened": {"raw_value": f"2026-01-{(index % 28) + 1:02d}", "text_value": ""},
        "updated_at": f"2026-{activity}-{(index % 28) + 1:02d}T09:00:00-04:00",
        "assignments": [{"user": {"user_name": "quinten" if assigned else "someone else"}}],
    }


class ListingLegalServerClient:
    configured = True
    user_filter_param = "assigned_user_email"

    def __init__(self, matters):
        self.matters = matters
        self.calls = []

    def find_user(self, identifier):
        return {"email": identifier}

    def search_matters(self, *, query="", user_email="", limit=50):
        self.calls.append({"query": query, "limit": limit})
        if query:
            return [matter for matter in self.matters if query in matter["case_number"]]
        return list(self.matters)

    def get_matter(self, matter_id):
        return next((matter for matter in self.matters if matter["case_number"] == matter_id), None)


class FailingLegalServerClient(ListingLegalServerClient):
    def search_matters(self, *, query="", user_email="", limit=50):
        raise LegalServerError("temporary LegalServer failure")


@override_settings(ENABLE_DEMO_MATTERS=False)
class CaseListApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="quinten@lemmalegal.com",
            email="quinten@lemmalegal.com",
            password="password",
        )
        self.client.force_login(self.user)
        UserSourceIdentity.objects.create(is_verified=True,
            user=self.user, provider="legalserver", identifier="quinten@lemmalegal.com"
        )
        self.matters = [legalserver_matter(index) for index in range(1, 26)]
        self.matters.append(legalserver_matter(90, closed=True))
        self.matters.append(legalserver_matter(91, problem="51 Medicaid"))

    def get(self, query=""):
        with patch("apps.matters.services.LegalServerClient") as client_class:
            client_class.return_value = ListingLegalServerClient(self.matters)
            response = self.client.get(f"/api/cases/{query}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_first_screen_is_twenty_open_cases(self):
        payload = self.get()

        self.assertEqual(len(payload["cases"]), 20)
        self.assertTrue(payload["hasMore"])
        self.assertEqual(payload["total"], 26)
        self.assertTrue(all(case["isOpen"] for case in payload["cases"]))

    def test_show_more_returns_the_next_screen(self):
        second = self.get("?offset=20")

        self.assertEqual(len(second["cases"]), 6)
        self.assertFalse(second["hasMore"])

    def test_closed_cases_are_hidden_until_asked_for(self):
        self.assertNotIn("26-0090", [case["caseNumber"] for case in self.get()["cases"]])

        closed = self.get("?status=closed")

        self.assertEqual([case["caseNumber"] for case in closed["cases"]], ["26-0090"])

    def test_search_reaches_closed_cases(self):
        payload = self.get("?q=0090")

        self.assertEqual([case["caseNumber"] for case in payload["cases"]], ["26-0090"])
        self.assertEqual(payload["filters"]["status"], "all")

    def test_exact_search_uses_authorized_cached_matter_during_transient_sync_failure(self):
        self.get("?q=26-0001")
        with patch("apps.matters.services.LegalServerClient") as client_class:
            client_class.return_value = FailingLegalServerClient(self.matters)
            response = self.client.get("/api/cases/?q=26-0001")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([case["caseNumber"] for case in payload["cases"]], ["26-0001"])
        self.assertEqual(payload["legalserver"]["syncError"], "temporary LegalServer failure")

    def test_legal_problem_codes_are_offered_and_filter_the_list(self):
        payload = self.get()
        self.assertIn("51 Medicaid", payload["problemCodes"])

        filtered = self.get("?problem=51%20Medicaid")

        self.assertEqual([case["caseNumber"] for case in filtered["cases"]], ["26-0091"])

    def test_sorting_by_opened_date_differs_from_last_activity(self):
        self.matters = [
            legalserver_matter(1, activity="01"),
            legalserver_matter(2, activity="08"),
        ]
        self.matters[0]["date_opened"] = {"raw_value": "2026-07-01", "text_value": ""}
        self.matters[1]["date_opened"] = {"raw_value": "2026-02-01", "text_value": ""}

        by_activity = [case["caseNumber"] for case in self.get("?sort=activity")["cases"]]
        by_opened = [case["caseNumber"] for case in self.get("?sort=opened")["cases"]]

        self.assertEqual(by_activity, ["26-0002", "26-0001"])
        self.assertEqual(by_opened, ["26-0001", "26-0002"])

    def test_quick_cases_survive_the_assignment_filter(self):
        Matter.objects.create(
            external_id=f"MANUAL-{self.user.id}-abc",
            client_name="Walk-in",
            matter_type="Housing matter",
            jurisdiction="",
            source_system="Manual",
            raw_payload={"manual_entry": True, "created_by_user_id": self.user.id},
        )

        payload = self.get("?assigned=mine")

        self.assertIn("Walk-in", [case["client"] for case in payload["cases"]])

    def test_the_applied_filters_come_back_with_the_page(self):
        payload = self.get("?status=all&sort=opened&limit=5")

        self.assertEqual(payload["filters"]["status"], "all")
        self.assertEqual(payload["filters"]["sort"], "opened")
        self.assertEqual(payload["filters"]["limit"], 5)
        self.assertEqual(len(payload["cases"]), 5)

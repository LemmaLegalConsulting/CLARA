import base64
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.return_paths import safe_return_path


class SafeReturnPathTests(SimpleTestCase):
    def test_accepts_task_routes(self):
        for path in (
            "/cases",
            "/cases/26-0222",
            "/drafting/26-0222/new",
            "/drafting/26-0222/sessions/184/plan",
            "/drafting/MANUAL-3-abc123",
            "/research/search",
            "/drafting/26%200222",
        ):
            self.assertEqual(safe_return_path(path), path, path)

    def test_rejects_anything_that_could_leave_the_app(self):
        for path in (
            "",
            None,
            "drafting/26-0222",
            "https://evil.example/drafting",
            "//evil.example/drafting",
            "/\\evil.example",
            "/drafting/%2F%2Fevil.example",
            "/%2F%2Fevil.example",
            "/drafting/%5Cevil",
            "/drafting/../admin/",
            "/drafting/%2e%2e/admin",
            "/drafting/%252e%252e/admin",
            "/drafting/26-0222?next=https://evil.example",
            "/drafting/26-0222#frag",
            "/drafting/a%0d%0aLocation:%20x",
            "/drafting/" + "x" * 600,
        ):
            self.assertEqual(safe_return_path(path), "", path)

    def test_rejects_server_paths(self):
        for path in ("/api/cases/", "/admin/", "/api/auth/office365/callback/", "/static/x.js", "/readyz", "/"):
            self.assertEqual(safe_return_path(path), "", path)


OFFICE365 = {
    "OFFICE365_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "OFFICE365_CLIENT_ID": "client-id",
    "OFFICE365_CLIENT_SECRET": "secret",
    "OFFICE365_REDIRECT_URI": "http://localhost:5173/api/auth/office365/callback/",
    "OFFICE365_SCOPES": "openid profile email",
    "FRONTEND_SITE_URL": "http://localhost:5173",
}


@override_settings(**OFFICE365)
class Office365ReturnPathTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.core.models import Office365Principal
        user = get_user_model().objects.create_user("approved", "advocate@example.invalid")
        Office365Principal.objects.create(user=user, tenant_id=OFFICE365["OFFICE365_TENANT_ID"],
                                         object_id="22222222-2222-2222-2222-222222222222")
        patcher = patch("apps.core.oidc.application")
        self.app = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.app.initiate_auth_code_flow.return_value = {"state": "state", "auth_uri": "https://login.microsoftonline.com/authorize"}
        self.app.acquire_token_by_auth_code_flow.return_value = {"id_token_claims": {
            "tid": OFFICE365["OFFICE365_TENANT_ID"], "oid": "22222222-2222-2222-2222-222222222222"}}
        claims_patch = patch("apps.core.oidc.validated_claims", return_value=self.app.acquire_token_by_auth_code_flow.return_value["id_token_claims"])
        claims_patch.start()
        self.addCleanup(claims_patch.stop)

    def sign_in(self):
        state = self.client.session["office365_flow"]["state"]
        return self.client.get(f"/api/auth/office365/callback/?code=code&state={state}")

    def test_returns_to_the_page_that_was_asked_for(self):
        self.client.get("/api/auth/office365/start/", {"returnTo": "/drafting/26-0222/sessions/184/plan"})
        response = self.sign_in()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "http://localhost:5173/drafting/26-0222/sessions/184/plan")

    def test_unsafe_return_target_lands_on_the_home_screen(self):
        self.client.get("/api/auth/office365/start/", {"returnTo": "//evil.example/drafting"})
        response = self.sign_in()
        self.assertEqual(response["Location"], "http://localhost:5173")

    def test_no_return_target_lands_on_the_home_screen(self):
        self.client.get("/api/auth/office365/start/")
        response = self.sign_in()
        self.assertEqual(response["Location"], "http://localhost:5173")

    def test_return_target_is_not_sent_to_microsoft(self):
        response = self.client.get("/api/auth/office365/start/", {"returnTo": "/drafting/26-0222"})
        self.assertNotIn("drafting", response.json()["authUrl"])

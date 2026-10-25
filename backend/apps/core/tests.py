import json
import base64
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings

from apps.ai.chat_history import append_message, messages_for_user
from apps.ai.models import ChatConversation
from apps.core.models import AuthorProfile, OrganizationSettings
from apps.core.views import default_jurisdiction_for_user


class AuthViewTests(TestCase):
    def test_manual_login_uses_django_auth_and_stores_graph_token(self):
        User.objects.create_user(username="advocate", password="secret", email="advocate@example.org")

        response = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": "advocate", "password": "secret", "msGraphAccessToken": "graph-token"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["user"]["isAuthenticated"])
        self.assertEqual(response.json()["user"]["profile"]["email"], "advocate@example.org")
        self.assertEqual(self.client.session["ms_graph_access_token"], "graph-token")

    def test_author_profile_can_be_updated(self):
        User.objects.create_user(username="advocate", password="secret", email="advocate@example.org")
        self.client.login(username="advocate", password="secret")

        response = self.client.patch(
            "/api/author-profile/",
            data=json.dumps({
                "displayName": "Ada Advocate",
                "salutation": "Dear Clerk:",
                "signoff": "Respectfully,",
                "phone": "555-0100",
                "defaultJurisdiction": "Cuyahoga County, Ohio",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(profile["displayName"], "Ada Advocate")
        self.assertEqual(profile["signoff"], "Respectfully,")
        self.assertEqual(profile["defaultJurisdiction"], "Cuyahoga County, Ohio")

    @override_settings(DEFAULT_JURISDICTION="Ohio")
    def test_user_jurisdiction_overrides_organization_and_environment_defaults(self):
        user = User.objects.create_user(username="jurisdiction-user", password="secret")
        OrganizationSettings.objects.create(default_jurisdiction="Michigan")

        self.assertEqual(default_jurisdiction_for_user(user), "Michigan")
        profile = AuthorProfile.objects.get(user=user)
        profile.default_jurisdiction = "Cuyahoga County, Ohio"
        profile.save()
        self.assertEqual(default_jurisdiction_for_user(user), "Cuyahoga County, Ohio")

    def test_case_chat_history_is_separated_by_case_scope(self):
        user = User.objects.create_user(username="chat-user", password="secret")
        append_message(user=user, kind=ChatConversation.CASE, scope_key="case-1", role="user", content="First case")
        append_message(user=user, kind=ChatConversation.CASE, scope_key="case-2", role="user", content="Second case")

        self.assertEqual([item["content"] for item in messages_for_user(user=user, kind=ChatConversation.CASE, scope_key="case-1")], ["First case"])
        self.assertEqual([item["content"] for item in messages_for_user(user=user, kind=ChatConversation.CASE, scope_key="case-2")], ["Second case"])

    def test_me_reports_anonymous_user(self):
        response = self.client.get("/api/auth/me/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["user"]["isAuthenticated"])
        self.assertIn("csrftoken", response.cookies)

    @override_settings(OFFICE365_TENANT_ID="", OFFICE365_CLIENT_ID="")
    def test_office365_start_reports_not_configured(self):
        response = self.client.get("/api/auth/office365/start/")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["configured"])

    @override_settings(
        OFFICE365_TENANT_ID="tenant",
        OFFICE365_CLIENT_ID="client-id",
        OFFICE365_REDIRECT_URI="http://localhost:8000/api/auth/office365/callback/",
        OFFICE365_SCOPES="openid profile email",
    )
    def test_office365_start_returns_authorization_url(self):
        with patch("apps.core.oidc.application") as app:
            app.return_value.initiate_auth_code_flow.return_value = {
                "state": "state", "auth_uri": "https://login.microsoftonline.com/authorize?client_id=client-id"}
            response = self.client.get("/api/auth/office365/start/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlparse(response.json()["authUrl"]).netloc, "login.microsoftonline.com")
        self.assertEqual(self.client.session["office365_flow"]["state"], "state")

    @override_settings(FRONTEND_SITE_URL="http://localhost:5173")
    def test_admin_view_site_points_to_frontend(self):
        self.assertEqual(admin.site.site_url, "http://localhost:5173")

    @override_settings(
        OFFICE365_TENANT_ID="organizations",
        OFFICE365_CLIENT_ID="client-id",
        OFFICE365_CLIENT_SECRET="secret",
        OFFICE365_REDIRECT_URI="http://localhost:5173/api/auth/office365/callback/",
        OFFICE365_SCOPES="openid profile email",
        FRONTEND_SITE_URL="http://localhost:5173",
    )
    def test_office365_callback_logs_in_with_model_backend(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.core import oidc
        from apps.core.models import OAuthTransaction, Office365Principal
        tenant = "11111111-1111-1111-1111-111111111111"
        object_id = "22222222-2222-2222-2222-222222222222"
        user = User.objects.create_user(username="approved", email="advocate@example.invalid")
        Office365Principal.objects.create(user=user, tenant_id=tenant, object_id=object_id)
        session = self.client.session
        session["office365_flow"] = {"state": "state"}
        session.save()
        OAuthTransaction.objects.create(state_digest=oidc.state_digest("state"), expires_at=timezone.now()+timedelta(minutes=10))
        with override_settings(OFFICE365_TENANT_ID=tenant), patch("apps.core.oidc.application") as app, patch("apps.core.oidc.validated_claims", return_value={"tid": tenant, "oid": object_id}):
            app.return_value.acquire_token_by_auth_code_flow.return_value = {
                "id_token_claims": {"tid": tenant, "oid": object_id}, "expires_in": 3600}
            response = self.client.get("/api/auth/office365/callback/?code=code&state=state")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "http://localhost:5173")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)


class ReadinessProbeTests(TestCase):
    """The endpoint the platform uses to decide a replica may take traffic."""

    def test_readyz_answers_without_authentication(self):
        response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ready\n")

    def test_readyz_answers_the_probe_arriving_over_loopback(self):
        # nginx pins Host to loopback for this one location, because the probe
        # presents the replica's own address and Django would otherwise reject
        # it as a DisallowedHost and the replica would never come ready.
        with self.settings(ALLOWED_HOSTS=["cle-draft.example.org", "127.0.0.1"]):
            response = self.client.get("/readyz", headers={"host": "127.0.0.1"})

        self.assertEqual(response.status_code, 200)

    def test_loopback_is_allowed_however_the_environment_is_configured(self):
        from django.conf import settings

        self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)

    def test_readyz_does_not_depend_on_the_database(self):
        # Readiness failure pulls the replica out of rotation and restarts it,
        # so a database blip must not be able to take every replica down at
        # once. Nothing here may touch the connection.
        with self.assertNumQueries(0):
            self.client.get("/readyz")


class PreloadWarmupTests(TransactionTestCase):
    """What the gunicorn master leaves behind for the workers it forks."""

    def test_warmup_closes_the_connections_it_opened_before_workers_fork(self):
        # The warm-up in config.wsgi is a real request, and the first request in
        # a process runs the prepared-template sync, which queries the database.
        # Under --preload that happens in the master. A connection still open
        # there is inherited by every worker, and several processes sharing one
        # Postgres socket read each other's results: after a cold start, one
        # request in four failed with a 500, a 504, or a signed-in user's
        # session coming back empty. The in-memory test database never really
        # closes, so this checks the order: the sync ran, then everything closed.
        import importlib

        from django.db import connections

        # First import also warms up; do not close this TestCase's live PostgreSQL transaction.
        with patch.object(connections, "close_all"):
            import config.wsgi
        from apps.templates_app import signals

        seen = []

        def record_close():
            seen.append(signals._initial_sync_done)

        with patch.object(signals, "_initial_sync_done", False), patch.object(
            connections, "close_all", side_effect=record_close
        ):
            importlib.reload(config.wsgi)

        self.assertEqual(seen, [True], "the warm-up must close connections after its first-request work")


@override_settings(
    CORS_ALLOWED_ORIGINS=["https://cle-draft.lemmalegal.com"],
    MIDDLEWARE=[
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "apps.core.middleware.CorsMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ],
)
class CorsMiddlewareTests(TestCase):
    """Cross-origin access for the split deployment, where the app is served
    from a static host and the API from a sibling subdomain."""

    APP_ORIGIN = "https://cle-draft.lemmalegal.com"

    def test_allowed_origin_may_send_credentials(self):
        response = self.client.get("/readyz", headers={"origin": self.APP_ORIGIN})

        self.assertEqual(response["Access-Control-Allow-Origin"], self.APP_ORIGIN)
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")
        # Without this a cache could hand one origin's response to another.
        self.assertEqual(response["Vary"], "Origin")

    def test_unknown_origin_gets_no_cors_headers(self):
        response = self.client.get("/readyz", headers={"origin": "https://not-ours.example"})

        self.assertNotIn("Access-Control-Allow-Origin", response)

    def test_preflight_is_answered_without_reaching_a_view(self):
        response = self.client.options(
            "/api/author-profile/",
            headers={"origin": self.APP_ORIGIN, "access-control-request-method": "PATCH"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("PATCH", response["Access-Control-Allow-Methods"])
        self.assertIn("x-csrftoken", response["Access-Control-Allow-Headers"])

    def test_preflight_from_an_unknown_origin_is_refused(self):
        response = self.client.options(
            "/api/author-profile/",
            headers={"origin": "https://not-ours.example", "access-control-request-method": "PATCH"},
        )

        self.assertEqual(response.status_code, 403)

    def test_headers_the_frontend_reads_are_exposed(self):
        # Every one of these fails silently when unexposed: the header arrives,
        # headers.get() returns null, and the feature quietly does nothing.
        # Content-Disposition carries download filenames; the LegalServer ones
        # carry whether a save actually landed.
        response = self.client.get("/readyz", headers={"origin": self.APP_ORIGIN})
        exposed = {h.strip().lower() for h in response["Access-Control-Expose-Headers"].split(",")}

        self.assertIn("content-disposition", exposed)
        for header in (
            "x-legalserver-delivery",
            "x-legalserver-delivery-message",
            "x-legalserver-ai-audit",
            "x-legalserver-ai-audit-message",
        ):
            self.assertIn(header, exposed)


class ChangePasswordTests(TestCase):
    """Changing your own password, which until now nobody could do."""

    def setUp(self):
        self.user = User.objects.create_user(username="advocate", password="original-password-9271")
        self.client.force_login(self.user)

    def test_a_correct_current_password_replaces_it(self):
        response = self.client.post(
            "/api/auth/change-password/",
            data=json.dumps({"currentPassword": "original-password-9271", "newPassword": "replacement-password-4417"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("replacement-password-4417"))

    def test_the_session_survives_the_change(self):
        # Changing a password rotates the hash the session is keyed against, so
        # without update_session_auth_hash this signs you out of the very tab
        # you did it in.
        self.client.post(
            "/api/auth/change-password/",
            data=json.dumps({"currentPassword": "original-password-9271", "newPassword": "replacement-password-4417"}),
            content_type="application/json",
        )

        still_here = self.client.get("/api/auth/me/")
        self.assertTrue(still_here.json()["user"]["isAuthenticated"])

    def test_the_wrong_current_password_changes_nothing(self):
        response = self.client.post(
            "/api/auth/change-password/",
            data=json.dumps({"currentPassword": "not-the-password", "newPassword": "replacement-password-4417"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("original-password-9271"))

    @override_settings(
        AUTH_PASSWORD_VALIDATORS=[
            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
        ]
    )
    def test_a_weak_new_password_is_refused_with_the_reason(self):
        response = self.client.post(
            "/api/auth/change-password/",
            data=json.dumps({"currentPassword": "original-password-9271", "newPassword": "short"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("12 characters", response.json()["error"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("original-password-9271"))

    def test_signed_out_callers_cannot_change_anything(self):
        self.client.logout()

        response = self.client.post(
            "/api/auth/change-password/",
            data=json.dumps({"currentPassword": "original-password-9271", "newPassword": "replacement-password-4417"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_get_is_not_a_way_to_do_this(self):
        response = self.client.get("/api/auth/change-password/")

        self.assertEqual(response.status_code, 405)

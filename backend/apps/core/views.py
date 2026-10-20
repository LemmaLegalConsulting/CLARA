import base64
import json
import re
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import OperationalError, ProgrammingError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.views.decorators.csrf import ensure_csrf_cookie

from apps.core.http import api_login_required, json_body, json_body_errors_to_400, method_not_allowed
from apps.core.models import AuthorProfile, OrganizationSettings
from apps.core.return_paths import safe_return_path
from apps.matters.seed import seed_matters
from apps.matters.legalserver_delivery import delivery_defaults
from apps.matters.services import legalserver_account_status
from apps.sources.models import UserOAuthConnection, UserSourceIdentity
from apps.sources.registry import connector_registry
from apps.templates_app.seed import seed_templates
from apps.templates_app.content_library import sync_prepared_templates


@ensure_csrf_cookie
@api_login_required
def bootstrap(_request):
    if settings.ENABLE_DEMO_MATTERS:
        seed_matters()
    seed_templates()
    sync_prepared_templates()
    sources = [connector.metadata() for connector in connector_registry.all()]
    legalserver_status = legalserver_account_status(_request.user)
    for source in sources:
        if source["kind"] == "legalserver":
            if not legalserver_status["configured"]:
                source["status"] = "Configure LegalServer API credentials"
            elif not legalserver_status["connected"]:
                source["status"] = "Connect LegalServer account"
            else:
                source["status"] = f"Connected as {legalserver_status['identifier']}"
    return JsonResponse(
        {
            "product": "CLARA",
            "jurisdictions": [
                "Cleveland Municipal Court - Housing Division",
                "Cuyahoga County Court of Common Pleas",
            ],
            "sources": sources,
            "legalserverSave": {
                "configured": legalserver_status["configured"],
                "defaults": delivery_defaults(),
            },
        }
    )


@api_login_required
def modes(_request):
    return JsonResponse(
        {
            "modes": [
                {"id": "template_fill", "label": "Fill template — no AI", "description": "Map case fields, optionally complete blanks, and download a DOCX with highlighted prompts."},
                {
                    "id": "research",
                    "label": "Research",
                    "description": "Search case file, sources, local cases, and user material without generating a document.",
                },
                {
                    "id": "draft_from_template",
                    "label": "Draft from template",
                    "description": "Linear workflow: select case, facts, template, law/source blocks, draft, validate, export.",
                },
                {
                    "id": "advice_letter",
                    "label": "Client advice letter",
                    "description": "Assemble a plain-language letter to a client from maintained advice sections, on organization letterhead.",
                },
                {
                    "id": "draft_from_scratch",
                    "label": "Draft from scratch",
                    "description": "Use a pleading shell and constrained section generation for novel arguments.",
                },
            ]
        }
    )


def normalize_ai_text(text):
    return re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)


def profile_to_dict(profile, user=None):
    # A real name only. A login or email address standing in for one signed
    # client letters "e2e-browser@example.invalid"; an empty name is caught and
    # asked for instead.
    fallback_name = user.get_full_name() if user else ""
    return {
        "displayName": profile.display_name or fallback_name,
        "salutation": profile.salutation,
        "signoff": profile.signoff,
        "organization": profile.organization,
        "title": profile.title,
        "barNumber": profile.bar_number,
        "phone": profile.phone,
        "fax": profile.fax,
        "email": profile.email or (getattr(user, "email", "") if user else ""),
        "officeName": profile.office_name,
        "address": profile.address,
        "signatureImage": profile.signature_image,
        "defaultJurisdiction": profile.default_jurisdiction,
        "preferences": profile.preferences,
    }


def profile_for_user(user):
    try:
        profile, _created = AuthorProfile.objects.get_or_create(
            user=user,
            defaults={
                "display_name": user.get_full_name(),
                "email": getattr(user, "email", ""),
            },
        )
        return profile
    except (OperationalError, ProgrammingError):
        return AuthorProfile(
            user=user,
            display_name=user.get_full_name(),
            email=getattr(user, "email", ""),
        )


def auth_user_to_dict(user):
    profile = profile_for_user(user) if user and user.is_authenticated else None
    return {
        "isAuthenticated": bool(user and user.is_authenticated),
        "username": user.get_username() if user and user.is_authenticated else "",
        "email": getattr(user, "email", "") if user and user.is_authenticated else "",
        "name": user.get_full_name() if user and user.is_authenticated else "",
        "profile": profile_to_dict(profile, user) if profile else None,
    }


@ensure_csrf_cookie
def me(request):
    return JsonResponse({"user": auth_user_to_dict(request.user)})


@api_login_required
def change_password(request):
    """Let a signed-in user change their own password.

    Accounts are created by an administrator, who necessarily knows the
    password they set. Until there was a way to change it, every account was
    permanently using a password someone else had also seen -- including over
    whatever channel it was delivered.

    The current password is required even though the session already proves who
    this is: it is what stops an unattended screen being enough to lock the real
    owner out of their account.
    """
    if request.method != "POST":
        return method_not_allowed(["POST"])
    body = json_body(request)
    current = body.get("currentPassword") or ""
    replacement = body.get("newPassword") or ""

    if not request.user.check_password(current):
        return JsonResponse({"error": "Your current password is not correct."}, status=400)
    if not replacement:
        return JsonResponse({"error": "Enter a new password."}, status=400)
    try:
        validate_password(replacement, request.user)
    except DjangoValidationError as error:
        return JsonResponse({"error": " ".join(error.messages)}, status=400)

    request.user.set_password(replacement)
    request.user.save(update_fields=["password"])
    # Changing a password rotates the hash the session is keyed against, which
    # would otherwise sign the user out of the tab they just did this in.
    update_session_auth_hash(request, request.user)
    return JsonResponse({"ok": True})


@api_login_required
def author_profile(request):
    profile = profile_for_user(request.user)
    if not profile.pk and request.method != "GET":
        return JsonResponse({"error": "Author profile storage is not migrated yet. Run backend migrations."}, status=503)
    if request.method == "GET":
        return JsonResponse({"profile": profile_to_dict(profile, request.user)})
    if request.method != "PATCH":
        return method_not_allowed(["GET", "PATCH"])
    body = json_body(request)
    field_map = {
        "displayName": "display_name",
        "salutation": "salutation",
        "signoff": "signoff",
        "organization": "organization",
        "title": "title",
        "barNumber": "bar_number",
        "phone": "phone",
        "fax": "fax",
        "email": "email",
        "officeName": "office_name",
        "address": "address",
        "signatureImage": "signature_image",
        "defaultJurisdiction": "default_jurisdiction",
        "preferences": "preferences",
    }
    for api_name, model_name in field_map.items():
        if api_name in body:
            setattr(profile, model_name, body[api_name])
    profile.save()
    return JsonResponse({"profile": profile_to_dict(profile, request.user)})


def default_jurisdiction_for_user(user):
    """Resolve research jurisdiction without requiring the frontend to trust itself."""
    profile = profile_for_user(user)
    if profile.default_jurisdiction.strip():
        return profile.default_jurisdiction.strip()
    try:
        organization = OrganizationSettings.objects.only("default_jurisdiction").first()
        if organization and organization.default_jurisdiction.strip():
            return organization.default_jurisdiction.strip()
    except (OperationalError, ProgrammingError):
        pass
    return settings.DEFAULT_JURISDICTION.strip()


@json_body_errors_to_400
def login_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    body = json_body(request)
    user = authenticate(request, username=body.get("username", ""), password=body.get("password", ""))
    if user is None:
        return JsonResponse({"error": "Invalid username or password"}, status=401)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if body.get("msGraphAccessToken"):
        request.session["ms_graph_access_token"] = body["msGraphAccessToken"]
    return JsonResponse({"user": auth_user_to_dict(user)})


def logout_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    logout(request)
    return JsonResponse({"ok": True})


def office365_configured():
    return bool(settings.OFFICE365_TENANT_ID and settings.OFFICE365_CLIENT_ID)


def office365_start(request):
    if not office365_configured():
        return JsonResponse(
            {
                "configured": False,
                "error": "Office 365 sign-in is not configured. Set OFFICE365_TENANT_ID and OFFICE365_CLIENT_ID.",
            },
            status=503,
        )
    state = secrets.token_urlsafe(24)
    request.session["office365_oauth_state"] = state
    # Kept server-side beside the state, never passed through Microsoft, so
    # the only return target is one this session asked for and we validated.
    request.session["office365_return_path"] = safe_return_path(request.GET.get("returnTo", ""))
    params = {
        "client_id": settings.OFFICE365_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.OFFICE365_REDIRECT_URI,
        "response_mode": "query",
        "scope": settings.OFFICE365_SCOPES,
        "state": state,
    }
    tenant = settings.OFFICE365_TENANT_ID
    return JsonResponse(
        {
            "configured": True,
            "authUrl": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}",
        }
    )


def decode_jwt_payload(token):
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}


def default_legalserver_identity_from_office365(user, claims):
    if not getattr(settings, "LEGALSERVER_AUTO_MAP_OFFICE365_EMAIL", True):
        return
    identifier = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or getattr(user, "email", "")
        or user.get_username()
    )
    identifier = (identifier or "").strip()
    if not identifier:
        return
    identity, created = UserSourceIdentity.objects.get_or_create(
        user=user,
        provider="legalserver",
        defaults={"identifier": identifier, "enabled": True},
    )
    if created:
        return
    if identity.enabled and not identity.identifier:
        identity.identifier = identifier
        identity.save(update_fields=["identifier", "updated_at"])


def office365_callback(request):
    if request.GET.get("state") != request.session.get("office365_oauth_state"):
        return JsonResponse({"error": "Invalid Office 365 sign-in state"}, status=400)
    if request.GET.get("error"):
        return JsonResponse({"error": request.GET.get("error_description") or request.GET["error"]}, status=400)
    code = request.GET.get("code")
    if not code:
        return JsonResponse({"error": "Missing Office 365 authorization code"}, status=400)

    token_url = f"https://login.microsoftonline.com/{settings.OFFICE365_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": settings.OFFICE365_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.OFFICE365_REDIRECT_URI,
        "scope": settings.OFFICE365_SCOPES,
    }
    if settings.OFFICE365_CLIENT_SECRET:
        data["client_secret"] = settings.OFFICE365_CLIENT_SECRET

    response = requests.post(token_url, data=data, timeout=20)
    if response.status_code >= 400:
        return JsonResponse({"error": "Office 365 token exchange failed"}, status=502)
    token_payload = response.json()
    claims = decode_jwt_payload(token_payload.get("id_token", ""))
    username = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
    if not username:
        return JsonResponse({"error": "Office 365 did not return a usable username"}, status=502)

    User = get_user_model()
    user, _created = User.objects.update_or_create(
        username=username,
        defaults={
            "email": claims.get("email") or username,
            "first_name": claims.get("given_name", ""),
            "last_name": claims.get("family_name", ""),
        },
    )
    UserOAuthConnection.objects.update_or_create(
        user=user,
        provider="office365",
        defaults={
            "enabled": True,
            "tenant_id": claims.get("tid", settings.OFFICE365_TENANT_ID),
            "client_id": settings.OFFICE365_CLIENT_ID,
            "access_token": token_payload.get("access_token", ""),
            "refresh_token": token_payload.get("refresh_token", ""),
            "scopes": token_payload.get("scope", settings.OFFICE365_SCOPES),
        },
    )
    default_legalserver_identity_from_office365(user, claims)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.pop("office365_oauth_state", None)
    return_path = safe_return_path(request.session.pop("office365_return_path", ""))
    return redirect(f"{settings.FRONTEND_SITE_URL.rstrip('/')}{return_path}" if return_path else settings.FRONTEND_SITE_URL)


def readyz(_request):
    """Readiness signal for the platform: gunicorn is up and serving Django.

    /healthz is answered by nginx, which comes up long before gunicorn has
    imported Django, so a readiness probe pointed at it admits traffic to a
    replica that will still answer 502. This endpoint is the other half: it can
    only respond once the WSGI application is loaded, so the probe can poll
    often and flip the replica live as soon as it truly is.

    It deliberately does not touch the database. Readiness failure takes the
    replica out of rotation and eventually restarts it, so a database blip would
    turn into every replica restarting at once -- exactly when the site can
    least afford it.
    """
    return HttpResponse("ready\n", content_type="text/plain")


def favicon(_request):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="10" fill="#112029"/>'
        '<path d="M18 43h28M24 43V24m16 19V24M20 24h24M26 18h12" '
        'fill="none" stroke="#f7fafc" stroke-width="4" stroke-linecap="round"/>'
        '<path d="M32 18v-6" fill="none" stroke="#78c2b3" stroke-width="4" stroke-linecap="round"/>'
        "</svg>"
    )
    return HttpResponse(svg, content_type="image/svg+xml")

import re
from dataclasses import dataclass, field
from uuid import uuid4

from django.conf import settings

from apps.matters.models import Matter
from apps.matters.route_aliases import sync_matter_route_alias
from apps.sources.connectors.legalserver import (
    LegalServerClient,
    LegalServerError,
    legalserver_matter_identifier,
    legalserver_matter_identifier_candidates,
    legalserver_matter_uuid,
    matter_payload_to_defaults,
)
from apps.sources.models import UserSourceIdentity


DEMO_SOURCE_SYSTEM = "Demo"


@dataclass
class LegalServerSyncResult:
    matters: list
    connected: bool
    configured: bool
    identifier: str = ""
    error: str = ""


@dataclass
class LegalServerAccessProfile:
    identifier: str = ""
    login_email: str = ""
    roles: list[str] = field(default_factory=list)
    user_payload: dict = field(default_factory=dict)
    is_superuser: bool = False
    email_matches_login: bool = False
    identity_required: bool = True
    error: str = ""

    @property
    def access_level(self):
        if self.is_superuser:
            return "superuser"
        if not self.identifier:
            return "unconnected"
        if self.error:
            return self.error
        return "regular"


def legalserver_id(payload):
    return legalserver_matter_identifier(payload)


def upsert_matter_from_legalserver(payload):
    external_id = legalserver_id(payload)
    if not external_id:
        return None
    defaults = matter_payload_to_defaults(payload)
    known_ids = list(legalserver_matter_identifier_candidates(payload))
    matter = Matter.objects.filter(external_id=external_id).first()
    if not matter:
        matter = Matter.objects.filter(
            external_id__in=[known_id for known_id in known_ids if known_id != external_id]
        ).first()
    if matter:
        for field, value in defaults.items():
            setattr(matter, field, value)
        matter.external_id = external_id
        matter.save()
    else:
        matter = Matter.objects.create(external_id=external_id, **defaults)
    sync_matter_route_alias(matter)
    return matter


def suggested_legalserver_identifier(user):
    if user and getattr(user, "is_authenticated", False):
        return getattr(user, "email", "") or getattr(user, "username", "")
    return ""


def legalserver_identifier_for_user(user):
    return UserSourceIdentity.identifier_for(user, "legalserver")


def login_email_for_user(user):
    return (getattr(user, "email", "") or getattr(user, "username", "") or "").casefold().strip()


def _normalized_identifier_candidates(identifier):
    normalized = (identifier or "").casefold().strip()
    if not normalized:
        return set()
    candidates = {normalized}
    if "@" in normalized:
        candidates.add(normalized.split("@", 1)[0])
    return candidates


IDENTITY_TOKEN_RE = re.compile(r"[a-z0-9._%+-]+(?:@[a-z0-9.-]+)?")


def _identity_value_matches(value, candidates):
    """Match an assignment value against a user identifier by whole token.

    Substring matching would make one advocate's identifier match another's:
    "bob@x.org" is a substring of "robert.bobson@x.org", and the bare local
    part "bob" appears inside plenty of unrelated names.
    """
    if value in (None, ""):
        return False
    normalized = str(value).casefold().strip()
    if normalized in candidates:
        return True
    # Identity fields are sometimes decorated, e.g. "Bob Smith <bob@x.org>".
    return bool(candidates & set(IDENTITY_TOKEN_RE.findall(normalized)))


def _assignment_identity_values(payload):
    top_level_keys = (
        "assigned_user_email",
        "assigned_user",
        "assigned_to",
        "caseworker",
        "advocate_email",
        "advocate",
        "attorney_email",
        "staff_email",
        "user_email",
        "email",
        "username",
        "user_name",
        "user_id",
        "user_uuid",
    )
    for key in top_level_keys:
        value = payload.get(key)
        if isinstance(value, dict):
            yield from _assignment_identity_values(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _assignment_identity_values(item)
                else:
                    yield item
        else:
            yield value

    assignment_lists = (
        "assignments",
        "assigned_users",
        "users",
        "advocates",
        "staff_assignments",
        "case_assignments",
    )
    # "notes" and "assigned_by" are free text describing the assignment, not the
    # assignee, so a passing mention of a colleague must not grant them access.
    nested_keys = (
        "name",
        "email",
        "user_name",
        "user_id",
        "user_uuid",
        "assigned_user_email",
    )
    for list_key in assignment_lists:
        for assignment in payload.get(list_key) or []:
            if not isinstance(assignment, dict):
                continue
            for key in nested_keys:
                yield assignment.get(key)
            user = assignment.get("user")
            if isinstance(user, dict):
                for key in nested_keys:
                    yield user.get(key)


def payload_matches_legalserver_identifier(payload, identifier):
    candidates = _normalized_identifier_candidates(identifier)
    if not candidates or not isinstance(payload, dict):
        return False
    return any(_identity_value_matches(value, candidates) for value in _assignment_identity_values(payload))


def _user_group_names(user):
    if not user or not getattr(user, "is_authenticated", False):
        return set()
    try:
        return {name.casefold() for name in user.groups.values_list("name", flat=True)}
    except Exception:
        return set()


def django_user_has_legalserver_superuser_role(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    configured_groups = {group.casefold() for group in getattr(settings, "LEGALSERVER_SUPERUSER_GROUPS", [])}
    return bool(configured_groups & _user_group_names(user))


def _flatten_values(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _flatten_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _flatten_values(nested)
    elif value not in (None, ""):
        yield str(value)


def legalserver_user_roles(payload):
    role_fields = (
        "role",
        "roles",
        "role_name",
        "user_role",
        "user_roles",
        "groups",
        "permissions",
        "permission_groups",
        "profile",
        "access_level",
    )
    roles = []
    if not isinstance(payload, dict):
        return roles
    for field_name in role_fields:
        for role in _flatten_values(payload.get(field_name)):
            roles.append(role)
    return sorted({role for role in roles if role})


def legalserver_user_email(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("email", "user_email", "email_address", "work_email", "primary_email", "username", "user_name"):
        value = payload.get(key)
        if value:
            return str(value).casefold().strip()
    return ""


def legalserver_roles_include_superuser(roles):
    configured_roles = {role.casefold() for role in getattr(settings, "LEGALSERVER_SUPERUSER_ROLES", [])}
    normalized_roles = {role.casefold() for role in roles}
    return bool(configured_roles & normalized_roles)


def legalserver_access_profile_for_user(user, *, client=None):
    identifier = legalserver_identifier_for_user(user)
    login_email = login_email_for_user(user)
    profile = LegalServerAccessProfile(
        identifier=identifier,
        login_email=login_email,
        is_superuser=django_user_has_legalserver_superuser_role(user),
        identity_required=True,
    )
    if not identifier:
        return profile

    # A local, administrator-approved identity is required. Never elevate from
    # a remote role until that exact active account proves the identity match.
    try:
        client = client or LegalServerClient()
        if getattr(client, "configured", False) and hasattr(client, "find_user"):
            profile.user_payload = client.find_user(identifier)
    except (LegalServerError, AttributeError):
        profile.user_payload = {}
    remote = profile.user_payload if isinstance(profile.user_payload, dict) else {}
    disabled = any(str(remote[key]).strip().casefold() not in {"false", "0", "no", ""}
                   for key in ("disabled", "is_disabled") if key in remote)
    disabled = disabled or any(str(remote[key]).strip().casefold() not in {"true", "1", "yes", "active", "enabled"}
                               for key in ("active", "is_active", "enabled") if key in remote)
    profile.email_matches_login = bool(login_email and legalserver_user_email(remote) == login_email)
    if not remote or disabled or not profile.email_matches_login:
        if not profile.is_superuser:
            profile.error = "identity_mismatch"
        # Explicit local administrators remain independent of remote mappings.
        return profile
    profile.roles = legalserver_user_roles(remote)
    profile.is_superuser = profile.is_superuser or legalserver_roles_include_superuser(profile.roles)

    return profile


def user_can_access_matter(user, matter, *, access_profile=None, client=None, require_remote=False):
    if not user or not getattr(user, "is_authenticated", False) or not matter:
        return False
    raw_payload = matter.raw_payload or {}
    if matter.source_system == "Manual" and raw_payload.get("created_by_user_id") == user.id:
        return True
    if matter.source_system == DEMO_SOURCE_SYSTEM:
        # Seeded sample data carries no client information, so it is readable
        # whenever it is enabled at all.
        return settings.ENABLE_DEMO_MATTERS
    # Explicit locally administered all-case permission does not depend on an
    # upstream assignment. Deliveries still require a responding upstream.
    if not require_remote and django_user_has_legalserver_superuser_role(user):
        return True
    client = client or LegalServerClient()
    access_profile = access_profile or legalserver_access_profile_for_user(user, client=client)
    if matter.source_system == "LegalServer":
        # No offline permission snapshots: revalidate on every sensitive access.
        if not client.configured:
            return False
        try:
            current = client.get_matter(legalserver_matter_uuid(raw_payload) or matter.external_id)
        except LegalServerError:
            return False
        if not current:
            return False
        if access_profile.is_superuser:
            return True
        return bool(access_profile.identifier and not access_profile.error and
                    payload_matches_legalserver_identifier(current, access_profile.identifier))
    if access_profile.is_superuser:
        return True
    return False


def local_matters_for_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return []
    return list(Matter.objects.filter(source_system="Manual", raw_payload__created_by_user_id=user.id))


def accessible_matters_for_user(user):
    if django_user_has_legalserver_superuser_role(user):
        return list(Matter.objects.all())
    local = local_matters_for_user(user)
    if settings.ENABLE_DEMO_MATTERS:
        local += list(Matter.objects.filter(source_system=DEMO_SOURCE_SYSTEM))
    client = LegalServerClient()
    profile = legalserver_access_profile_for_user(user, client=client)
    if not client.configured or not profile.identifier or profile.error:
        return local
    # One current assignment search, not one network request per cached case.
    try:
        payloads = client.search_matters(user_email=profile.identifier, limit=1000)
    except LegalServerError:
        return local
    current_ids = set()
    for payload in payloads:
        if profile.is_superuser or payload_matches_legalserver_identifier(payload, profile.identifier):
            current_ids.update(legalserver_matter_identifier_candidates(payload))
    return local + list(Matter.objects.filter(source_system="LegalServer", external_id__in=current_ids))


def matter_for_user(user, external_id):
    matter = Matter.objects.filter(external_id=external_id).first()
    if matter and user_can_access_matter(user, matter):
        return matter
    return None


def create_manual_matter_for_user(user, *, client_name="", matter_type="", jurisdiction="", posture="", summary=""):
    external_id = f"MANUAL-{user.id}-{uuid4().hex[:12]}"
    return Matter.objects.create(
        external_id=external_id,
        client_name=(client_name or "").strip() or "New quick case",
        matter_type=(matter_type or "").strip() or "Housing matter",
        jurisdiction=(jurisdiction or "").strip(),
        posture=(posture or "").strip(),
        summary=(summary or "").strip(),
        source_system="Manual",
        raw_payload={
            "created_by_user_id": user.id,
            "created_by_username": getattr(user, "username", ""),
            "manual_entry": True,
        },
    )


def update_manual_matter_for_user(matter, user, payload):
    raw_payload = matter.raw_payload or {}
    if matter.source_system != "Manual" or raw_payload.get("created_by_user_id") != user.id:
        raise PermissionError("Only the creator can edit a quick case.")
    field_map = {
        "clientName": "client_name",
        "client_name": "client_name",
        "matterType": "matter_type",
        "matter_type": "matter_type",
        "jurisdiction": "jurisdiction",
        "posture": "posture",
        "summary": "summary",
        "notes": "summary",
    }
    changed = []
    for api_name, model_name in field_map.items():
        if api_name in payload:
            setattr(matter, model_name, (payload.get(api_name) or "").strip())
            if model_name not in changed:
                changed.append(model_name)
    if changed:
        matter.raw_payload = {**raw_payload, "manual_entry": True}
        matter.save(update_fields=[*changed, "raw_payload", "updated_at"])
    return matter


def create_legalserver_draft_intake_from_manual_matter(matter, user):
    raw_payload = matter.raw_payload or {}
    if matter.source_system != "Manual" or raw_payload.get("created_by_user_id") != user.id:
        raise PermissionError("Only the creator can export a quick case intake preview.")
    payload = {
        "client_name": matter.client_name,
        "matter_type": matter.matter_type,
        "jurisdiction": matter.jurisdiction,
        "posture": matter.posture,
        "summary": matter.summary,
        "source": {
            "system": "Manual",
            "matter_id": matter.external_id,
            "created_by_user_id": user.id,
        },
    }
    return {
        "posted": False,
        "reason": "LegalServer draft-intake posting is not configured.",
        "payload": payload,
    }


def legalserver_account_status(user, *, client=None):
    client = client or LegalServerClient()
    identifier = legalserver_identifier_for_user(user)
    access_profile = legalserver_access_profile_for_user(user, client=client)
    return {
        "configured": client.configured,
        "connected": bool(identifier),
        "identifier": identifier,
        "suggestedIdentifier": suggested_legalserver_identifier(user),
        "userFilterParam": client.user_filter_param,
        "accessLevel": access_profile.access_level,
        "roles": access_profile.roles,
        "emailMatchesLogin": access_profile.email_matches_login,
        "requiresOffice365EmailMatch": access_profile.identity_required,
        "autoMapOffice365Email": False,
        "mappingManagedByAdministrator": True,
    }


def sync_legalserver_matters_for_user(user, *, query="", limit=50, restrict_to_user=True, client=None):
    client = client or LegalServerClient()
    if not client.configured:
        return LegalServerSyncResult(matters=[], connected=False, configured=False, error="not_configured")
    access_profile = legalserver_access_profile_for_user(user, client=client)
    if not access_profile.identifier:
        return LegalServerSyncResult(matters=[], connected=False, configured=True, error="not_connected")
    if access_profile.error:
        return LegalServerSyncResult(
            matters=[],
            connected=True,
            configured=True,
            identifier=access_profile.identifier,
            error=access_profile.error,
        )
    try:
        payloads = client.search_matters(query=query, user_email="", limit=limit)
        if not access_profile.is_superuser:
            payloads = [payload for payload in payloads if payload_matches_legalserver_identifier(payload, access_profile.identifier)]
        if query and not payloads:
            payload = client.get_matter(query)
            if access_profile.is_superuser:
                payloads = [payload] if payload else []
            else:
                payloads = [payload] if payload and payload_matches_legalserver_identifier(payload, access_profile.identifier) else []
    except LegalServerError as exc:
        # A transient LegalServer failure should not blank an exact case-number
        # search when this user has already synchronized and remains authorized
        # for that matter. Keep the error visible so the UI does not imply the
        # cached record is fresh.
        cached = matter_for_user(user, query) if query else None
        return LegalServerSyncResult(
            matters=[cached] if cached else [],
            connected=True,
            configured=True,
            identifier=access_profile.identifier,
            error=str(exc) or "request_failed",
        )
    matters = [matter for matter in (upsert_matter_from_legalserver(payload) for payload in payloads) if matter]
    return LegalServerSyncResult(matters=matters, connected=True, configured=True, identifier=access_profile.identifier)


def sync_legalserver_matter(matter_id, *, user=None, client=None):
    client = client or LegalServerClient()
    if not client.configured:
        return None
    access_profile = legalserver_access_profile_for_user(user, client=client) if user is not None else None
    if access_profile and access_profile.error:
        return None
    try:
        payload = client.get_matter(matter_id)
    except LegalServerError:
        return None
    if access_profile and not access_profile.is_superuser:
        if not payload_matches_legalserver_identifier(payload, access_profile.identifier):
            return None
    return upsert_matter_from_legalserver(payload)

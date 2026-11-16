"""Microsoft's maintained authorization-code client and explicit admission policy."""
import hashlib
from functools import lru_cache
from uuid import UUID

import msal
import jwt
from django.conf import settings


MULTI_TENANT_AUTHORITIES = frozenset({"organizations", "common"})


def is_multi_tenant():
    return (settings.OFFICE365_TENANT_ID or "").strip().lower() in MULTI_TENANT_AUTHORITIES


def tenant_id():
    if is_multi_tenant():
        return settings.OFFICE365_TENANT_ID.strip().lower()
    return str(UUID(settings.OFFICE365_TENANT_ID))


def allowed_tenant_ids():
    raw = getattr(settings, "OFFICE365_ALLOWED_TENANT_IDS", ()) or ()
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    return {str(UUID(str(item).strip())) for item in raw if str(item).strip()}


def tenant_is_allowed(tenant):
    return str(UUID(str(tenant))) in allowed_tenant_ids()


def application():
    return msal.ConfidentialClientApplication(
        settings.OFFICE365_CLIENT_ID,
        client_credential=settings.OFFICE365_CLIENT_SECRET or None,
        authority=f"https://login.microsoftonline.com/{tenant_id()}",
        timeout=20,
    )


def state_digest(state):
    return hashlib.sha256(state.encode()).hexdigest()


@lru_cache(maxsize=4)
def signing_keys(tenant):
    # Never trust an issuer/jku supplied by an unvalidated token.
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=10)


def validated_claims(token):
    authority = tenant_id()
    try:
        key = signing_keys(authority).get_signing_key_from_jwt(token).key
        if is_multi_tenant():
            claims = jwt.decode(
                token,
                key,
                algorithms=['RS256'],
                audience=settings.OFFICE365_CLIENT_ID,
                options={'verify_iss': False, 'require': ['exp', 'iat', 'nbf', 'aud', 'iss', 'tid', 'oid', 'sub']},
            )
            token_tenant = str(UUID(str(claims.get('tid', ''))))
            if claims.get('iss') != f'https://login.microsoftonline.com/{token_tenant}/v2.0':
                raise ValueError('Invalid Office 365 ID token issuer')
            return claims
        return jwt.decode(
            token,
            key,
            algorithms=['RS256'],
            audience=settings.OFFICE365_CLIENT_ID,
            issuer=f'https://login.microsoftonline.com/{authority}/v2.0',
            options={'require': ['exp', 'iat', 'nbf', 'aud', 'iss', 'tid', 'oid', 'sub']},
        )
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        raise ValueError('Invalid Office 365 ID token') from exc

"""Microsoft's maintained authorization-code client and explicit admission policy."""
import hashlib
from functools import lru_cache
from uuid import UUID

import msal
import jwt
from django.conf import settings


def tenant_id():
    # Deliberately single tenant. 'common' and 'organizations' are not admission policies.
    return str(UUID(settings.OFFICE365_TENANT_ID))


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
    tenant = tenant_id()
    try:
        key = signing_keys(tenant).get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=['RS256'], audience=settings.OFFICE365_CLIENT_ID,
                          issuer=f'https://login.microsoftonline.com/{tenant}/v2.0',
                          options={'require': ['exp', 'iat', 'nbf', 'aud', 'iss', 'tid', 'oid', 'sub']})
    except jwt.PyJWTError as exc:
        raise ValueError('Invalid Office 365 ID token') from exc

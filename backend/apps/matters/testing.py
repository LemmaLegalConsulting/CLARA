"""Synthetic upstream authorization fixtures for workflow/transport tests."""
from unittest.mock import patch

from apps.matters.models import Matter
from apps.sources.models import UserSourceIdentity
from apps.sources.connectors.legalserver import legalserver_matter_uuid


def responding_legalserver(testcase):
    """Treat explicitly authored test records as the responding remote fixture.

    Revocation/outage regressions supply their own independent upstream responses;
    this helper is for tests of routing, export payloads and delivery reporting.
    """
    user = testcase.user
    from django.contrib.auth.models import Permission
    user.user_permissions.add(*Permission.objects.filter(codename__in=[
        'write_legalserver_documents', 'write_legalserver_notes', 'write_legalserver_properties']))
    UserSourceIdentity.objects.get_or_create(user=user, provider='legalserver',
                                            defaults={'identifier': user.email, 'is_verified': True})

    def get_matter(key):
        for matter in Matter.objects.filter(source_system='LegalServer'):
            if key in (matter.external_id, legalserver_matter_uuid(matter.raw_payload)):
                return {'id': matter.external_id, **matter.raw_payload}
        return {}

    for target, effect in (
        ('apps.sources.connectors.legalserver.LegalServerClient.find_user', lambda identifier: {'email': identifier}),
        ('apps.sources.connectors.legalserver.LegalServerClient.get_matter', get_matter),
    ):
        patcher = patch(target, side_effect=effect)
        patcher.start()
        testcase.addCleanup(patcher.stop)

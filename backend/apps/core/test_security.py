"""Synthetic regressions for authentication, rendering and authorization boundaries."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID
import io
import zipfile

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from jinja2.sandbox import SecurityError

from apps.core import oidc
from apps.core.models import Office365Principal, OAuthTransaction
from apps.matters.models import Matter
from apps.matters.services import legalserver_access_profile_for_user, user_can_access_matter
from apps.sources.models import UserSourceIdentity
from apps.sources.connectors.legalserver import LegalServerClient, LegalServerError
from apps.sources.document_text import extract_text, _extract_in_worker, DocumentExtractionError
from apps.templates_app.jinja_filters import template_environment

TENANT = '11111111-1111-1111-1111-111111111111'
OBJECT = '22222222-2222-2222-2222-222222222222'


class RestrictedRendererTests(SimpleTestCase):
    def test_internal_traversal_and_methods_rejected(self):
        for text in ['{{ cycler.__init__.__globals__ }}', '{{ matter.delete() }}',
                     '{{ fields.items() }}', '{{ "x" * 100000000 }}',
                     '{{ fields|attr("__class__") }}']:
            with self.subTest(text=text), self.assertRaises(SecurityError):
                template_environment().from_string(text).render(fields={}, matter={})

    def test_substitution_and_reviewed_helpers(self):
        self.assertEqual(template_environment().from_string(
            '{{ matter.client_name }}: {{ amount|currency }}; {{ comma_and_list(people) }}'
        ).render(matter={'client_name': 'Example'}, amount='1250', people=['A', 'B']),
                         'Example: $1,250.00; A and B')


@override_settings(OFFICE365_TENANT_ID=TENANT, OFFICE365_CLIENT_ID='client', OFFICE365_CLIENT_SECRET='synthetic')
class OAuthSecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('existing', 'person@example.invalid')
        self.principal = Office365Principal.objects.create(user=self.user, tenant_id=TENANT, object_id=OBJECT)
        patcher = patch('apps.core.oidc.validated_claims', return_value={'tid': TENANT, 'oid': OBJECT})
        self.claims = patcher.start()
        self.addCleanup(patcher.stop)

    def transaction(self, state='synthetic-state', expired=False):
        session = self.client.session
        session['office365_flow'] = {'state': state}
        session.save()
        OAuthTransaction.objects.create(state_digest=oidc.state_digest(state),
            expires_at=timezone.now() + timedelta(minutes=-1 if expired else 10))

    def callback(self, state='synthetic-state'):
        return self.client.get('/api/auth/office365/callback/', {'state': state, 'code': 'synthetic'})

    @patch('apps.core.oidc.application')
    def test_absent_mismatched_expired_and_replayed_state(self, app):
        self.assertEqual(self.client.get('/api/auth/office365/callback/?code=x').status_code, 400)
        self.transaction(expired=True)
        self.assertEqual(self.callback().status_code, 400)
        self.transaction('valid')
        self.assertEqual(self.callback('different').status_code, 400)
        app.assert_not_called()

    @patch('apps.core.oidc.application')
    def test_approved_immutable_principal_and_single_use(self, app):
        self.transaction()
        app.return_value.acquire_token_by_auth_code_flow.return_value = {
            'id_token_claims': {'tid': TENANT, 'oid': OBJECT, 'email': 'changed@example.invalid'},
            'expires_in': 3600,
        }
        self.assertEqual(self.callback().status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)
        # Recreate the pre-callback session to model a concurrent worker/replay.
        session = self.client.session
        session['office365_flow'] = {'state': 'synthetic-state'}
        session.save()
        self.assertEqual(self.callback().status_code, 400)
        self.assertEqual(app.return_value.acquire_token_by_auth_code_flow.call_count, 1)

    @patch('apps.core.oidc.application')
    def test_email_collision_does_not_link(self, app):
        self.transaction()
        app.return_value.acquire_token_by_auth_code_flow.return_value = {'id_token_claims': {
            'tid': TENANT, 'oid': '33333333-3333-3333-3333-333333333333', 'email': self.user.email}}
        self.claims.return_value = app.return_value.acquire_token_by_auth_code_flow.return_value['id_token_claims']
        self.assertEqual(self.callback().status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    @patch('apps.core.oidc.application')
    def test_wrong_tenant_and_provider_validation_failure(self, app):
        self.transaction()
        app.return_value.acquire_token_by_auth_code_flow.return_value = {'id_token_claims': {
            'tid': OBJECT, 'oid': OBJECT}}
        self.claims.return_value = {'tid': OBJECT, 'oid': OBJECT}
        self.assertEqual(self.callback().status_code, 400)
        self.transaction('next')
        app.return_value.acquire_token_by_auth_code_flow.side_effect = ValueError('invalid nonce/audience/expiry')
        self.assertEqual(self.callback('next').status_code, 400)


@override_settings(LEGALSERVER_SUPERUSER_ROLES=['Administrator'])
class LegalServerSecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('person', 'person@example.invalid')
        UserSourceIdentity.objects.create(is_verified=True, user=self.user, provider='legalserver', identifier='other@example.invalid')
        self.remote = Mock(configured=True)
        self.remote.find_user.return_value = {'email': 'other@example.invalid', 'roles': ['Administrator']}
        self.matter = Matter.objects.create(external_id='synthetic', source_system='LegalServer',
            raw_payload={'assigned_user_email': 'person@example.invalid'})

    def test_another_administrator_never_grants_roles(self):
        profile = legalserver_access_profile_for_user(self.user, client=self.remote)
        self.assertFalse(profile.is_superuser)
        self.assertEqual(profile.error, 'identity_mismatch')
        self.assertEqual(profile.roles, [])

    def test_staff_is_not_all_case_permission(self):
        self.user.is_staff = True
        self.assertFalse(legalserver_access_profile_for_user(self.user, client=self.remote).is_superuser)

    def test_current_revocation_and_outage_deny_cached_assignment(self):
        identity = self.user.source_identities.first() if hasattr(self.user, 'source_identities') else UserSourceIdentity.objects.get(user=self.user)
        identity.identifier = self.user.email
        identity.save()
        self.remote.find_user.return_value = {'email': self.user.email}
        self.remote.get_matter.return_value = {'assigned_user_email': self.user.email}
        self.assertTrue(user_can_access_matter(self.user, self.matter, client=self.remote))
        self.remote.get_matter.return_value = {'assigned_user_email': 'other@example.invalid'}
        self.assertFalse(user_can_access_matter(self.user, self.matter, client=self.remote))
        self.remote.get_matter.side_effect = LegalServerError('offline')
        self.assertFalse(user_can_access_matter(self.user, self.matter, client=self.remote))

    def test_self_mapping_denied(self):
        self.client.force_login(self.user)
        response = self.client.patch('/api/legalserver/account/', data='{"identifier":"admin@example.invalid"}', content_type='application/json')
        self.assertEqual(response.status_code, 403)


class ExtractionSecurityTests(SimpleTestCase):
    def test_expansion_rejected_before_reading_xml(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', b'x' * 1024)
        with patch('apps.sources.document_text.MAX_EXPANDED_BYTES', 100):
            with self.assertRaises(DocumentExtractionError):
                _extract_in_worker(buffer.getvalue(), filename='synthetic.docx')

    def test_false_pdf_signature_rejected(self):
        with self.assertRaises(DocumentExtractionError):
            extract_text(b'<html>synthetic</html>', filename='synthetic.pdf')


class DocumentDeliverySecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('viewer', 'viewer@example.invalid')
        self.client.force_login(self.user)
        self.matter = Matter.objects.create(external_id='manual-security', source_system='Manual',
            raw_payload={'created_by_user_id': self.user.pk})

    @patch('apps.matters.views.get_document_file')
    @patch('apps.matters.views.get_case_document')
    def test_active_content_is_attachment_even_with_pdf_mime(self, document, download):
        document.return_value = {'filename': 'synthetic.pdf'}
        for content in (b'<html><script>window.marker=1</script></html>', b'<svg onload="window.marker=1"/>'):
            download.return_value = {'content': content, 'content_type': 'application/pdf'}
            response = self.client.get('/api/cases/manual-security/documents/synthetic/file/')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response['Content-Disposition'].startswith('attachment'))
            self.assertEqual(response['Content-Type'], 'application/octet-stream')
            self.assertIn('sandbox;', response['Content-Security-Policy'])
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    @patch('apps.matters.views.get_document_file')
    @patch('apps.matters.views.get_case_document')
    def test_pdf_preview_and_unauthorized_request(self, document, download):
        document.return_value = {'filename': 'synthetic.pdf'}
        download.return_value = {'content': b'%PDF-1.7\nsynthetic', 'content_type': 'text/html'}
        url = '/api/cases/manual-security/documents/synthetic/file/'
        self.assertTrue(self.client.get(url)['Content-Disposition'].startswith('inline'))
        other = get_user_model().objects.create_user('other', 'other@example.invalid')
        self.client.force_login(other)
        download.reset_mock()
        self.assertEqual(self.client.get(url).status_code, 404)
        download.assert_not_called()


class ExactUserLookupTests(TestCase):
    def client_with(self, users):
        client = LegalServerClient()
        client.users_path = '/users'
        client.user_search_fields = ['email']
        client._get = Mock(return_value={'data': users})
        return client

    def test_absent_fuzzy_and_ambiguous_do_not_bind(self):
        for users in ([], [{'id': 1, 'email': 'other@example.invalid'}],
                      [{'id': 1, 'email': 'person@example.invalid'}, {'id': 2, 'email': 'person@example.invalid'}]):
            self.assertEqual(self.client_with(users).find_user('person@example.invalid'), {})

    def test_exact_unique_match(self):
        record = {'id': 1, 'email': 'person@example.invalid'}
        self.assertEqual(self.client_with([record]).find_user('person@example.invalid'), record)


class WorkerBudgetTests(SimpleTestCase):
    def test_timeout_is_reported_and_slot_released(self):
        import subprocess
        from apps.core.constrained_worker import run_extraction
        with patch('apps.core.constrained_worker.subprocess.Popen', side_effect=subprocess.TimeoutExpired('parser', 25)):
            for _ in range(3):
                with self.assertRaisesMessage(ValueError, 'time limit'):
                    run_extraction(b'x', filename='test.txt', content_type='text/plain', extractor='stdlib')

    def test_plain_text_roundtrip_in_worker(self):
        self.assertEqual(extract_text(b'Synthetic text', filename='test.txt')['text'], 'Synthetic text')


class QuarantinedTemplateTests(TestCase):
    def test_inactive_templates_cannot_be_selected_by_any_id_entrypoint(self):
        from apps.templates_app.models import DocumentTemplate
        from apps.drafting.services import template_for_reference, active_template_ids
        template = DocumentTemplate.objects.create(slug='quarantine', title='Unreviewed', is_active=False)
        self.assertIsNone(template_for_reference(template.pk))
        with self.assertRaises(ValueError):
            active_template_ids([template.pk])

    def test_unverified_legacy_identity_is_not_used(self):
        from apps.matters.services import legalserver_identifier_for_user
        user = get_user_model().objects.create_user('legacy', 'legacy@example.invalid')
        UserSourceIdentity.objects.create(user=user, provider='legalserver', identifier=user.email)
        self.assertEqual(legalserver_identifier_for_user(user), '')


class RendererBudgetTests(SimpleTestCase):
    def test_amplification_and_nested_loops_fail_before_render(self):
        from apps.templates_app.fill_templates import fill_environment
        for factory in (template_environment, fill_environment):
            for source in ("{{ 'x'|center(1000000000) }}", "{{ '%1000000000s'|format('x') }}",
                           "{% set a='x' %}" + "{% set a=a~a %}" * 22 + "{{ a }}",
                           "{% for a in values %}{% for b in values %}x{% endfor %}{% endfor %}"):
                with self.subTest(factory=factory.__name__, source=source), self.assertRaises(SecurityError):
                    factory().from_string(source).render(values=['a'])


@override_settings(OFFICE365_TENANT_ID=TENANT, OFFICE365_CLIENT_ID='client')
class SignedOIDCTokenTests(SimpleTestCase):
    def test_signature_audience_issuer_and_expiry_are_validated(self):
        import jwt
        import time
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'iss': f'https://login.microsoftonline.com/{TENANT}/v2.0', 'aud': 'client',
                  'tid': TENANT, 'oid': OBJECT, 'sub': 'synthetic',
                  'iat': int(time.time()), 'nbf': int(time.time())-1, 'exp': int(time.time())+60}
        with patch('apps.core.oidc.signing_keys') as keys:
            keys.return_value.get_signing_key_from_jwt.return_value.key = key.public_key()
            self.assertEqual(oidc.validated_claims(jwt.encode(claims, key, algorithm='RS256'))['oid'], OBJECT)
            for changed in ({'aud': 'wrong'}, {'iss': 'https://example.invalid'}, {'exp': 1}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    oidc.validated_claims(jwt.encode({**claims, **changed}, key, algorithm='RS256'))
            other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            with self.assertRaises(ValueError):
                oidc.validated_claims(jwt.encode(claims, other, algorithm='RS256'))


class PrivateMediaTests(TestCase):
    def test_download_requires_explicit_asset_permission(self):
        import tempfile
        from django.contrib.auth.models import Permission
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.templates_app.models import Letterhead
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            record = Letterhead.objects.create(slug='synthetic', title='Synthetic',
                docx=SimpleUploadedFile('synthetic.docx', b'Synthetic attachment'))
            url = record.docx.url
            self.assertEqual(self.client.get(url).status_code, 401)
            user = get_user_model().objects.create_user('staff', is_staff=True)
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
            user.user_permissions.add(Permission.objects.get(codename='view_letterhead'))
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'Synthetic attachment')
            self.assertTrue(response['Content-Disposition'].startswith('attachment'))
            self.assertEqual(self.client.get('/media/arbitrary.docx').status_code, 404)


@override_settings(LEGALSERVER_ALLOW_WRITES=True)
class WriteAuthorizationTests(TestCase):
    def test_read_access_does_not_imply_note_or_document_write(self):
        from apps.matters.legalserver_delivery import save_case_note, save_document
        user = get_user_model().objects.create_user('reader', 'reader@example.invalid')
        matter = Matter.objects.create(external_id='synthetic-write', source_system='LegalServer',
            raw_payload={'case_id': 1, 'matter_uuid': OBJECT, 'assigned_user_email': user.email})
        remote = Mock(configured=True)
        remote.allow_writes = True
        note = save_case_note(matter, user=user, title='Synthetic', body='Synthetic', origin='test', client=remote)
        document = save_document(matter, user=user, title='Synthetic', filename='test.docx',
            content=b'Synthetic', origin='test', client=remote)
        self.assertEqual(note.reason, 'authorization_unavailable_or_revoked')
        self.assertEqual(document.reason, 'authorization_unavailable_or_revoked')
        remote.create_note.assert_not_called()
        remote.upload_matter_document.assert_not_called()


class PrintedContainerBudgetTests(SimpleTestCase):
    def test_compact_nested_container_cannot_expand_before_output_check(self):
        source = "{% set a=['xxxxxxxxxx'] %}" + "{% set a=[a,a,a,a,a,a,a,a,a,a] %}" * 6 + "{{ a }}"
        for expression in ("{{ a }}", "{{ a|string }}", '{{ a ~ "" }}', "{{ a|comma_and_list }}"):
            with self.subTest(expression=expression), self.assertRaises(SecurityError):
                template_environment().from_string(source.replace("{{ a }}", expression)).render()


class WorkerFailureHandlingTests(SimpleTestCase):
    def test_started_worker_tree_is_killed_after_timeout(self):
        import signal
        import subprocess
        from apps.core.constrained_worker import run_extraction
        process = Mock(pid=123456)
        process.communicate.side_effect = subprocess.TimeoutExpired('synthetic', 25)
        with patch('apps.core.constrained_worker.subprocess.Popen', return_value=process), \
             patch('apps.core.constrained_worker.os.killpg') as kill:
            with self.assertRaisesMessage(ValueError, 'time limit'):
                run_extraction(b'x', filename='test.txt', content_type='text/plain', extractor='stdlib')
        kill.assert_called_once_with(123456, signal.SIGKILL)
        process.wait.assert_called_once()

    def test_busy_worker_is_a_retryable_api_response(self):
        from django.test import RequestFactory
        from apps.core.http import api_login_required
        from apps.core.constrained_worker import WorkerBusyError
        @api_login_required
        def view(request):
            raise WorkerBusyError('Document processing is busy; retry shortly')
        request = RequestFactory().get('/synthetic/')
        request.user = SimpleNamespace(is_authenticated=True)
        response = view(request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Retry-After'], '5')

import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.core.storage import PUBLISHED, RAW, VALIDATED, get_document_storage
from apps.sources.admin import ManagedSourceVersionAdmin
from apps.sources.models import ManagedSource, ManagedSourceEvent, ManagedSourceVersion
from apps.sources.publication import (
    STALE_VALIDATION, PublicationError, import_version, is_resumable, publish_version,
    retire_source, rollback_source,
)
from apps.sources.research.corpus import managed_records


class FakePdfPage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class ManagedSourceFixture:
    """Storage rooted in a temporary directory, with one draft source."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = override_settings(
            DOCUMENT_STORAGE_BACKEND="filesystem",
            DOCUMENT_STORAGE_ROOT=Path(self.temp.name),
        )
        self.settings.enable()
        self.user = User.objects.create_user("operator")
        self.source = ManagedSource.objects.create(
            slug="iskin-treatise", title="Iskin Treatise", kind="treatise",
            jurisdiction="Ohio", created_by=self.user,
        )

    def tearDown(self):
        self.settings.disable()
        self.temp.cleanup()

    def import_text(self, text, *, publish=True, filename="iskin.txt"):
        return import_version(
            source=self.source, content=text.encode(), filename=filename,
            content_type="text/plain", actor=self.user, publish=publish,
        )


class ManagedSourcePublicationTests(ManagedSourceFixture, TestCase):
    def test_create_writes_each_area_and_makes_chunks_retrievable_with_provenance(self):
        version, created = self.import_text("Security deposits\n\nA landlord must itemize deductions.")

        self.assertTrue(created)
        self.source.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(self.source.current_version, version)
        self.assertEqual(self.source.state, "published")
        self.assertTrue(get_document_storage(RAW).exists(version.raw_key))
        self.assertTrue(get_document_storage(VALIDATED).exists(version.validated_manifest_key))
        self.assertTrue(get_document_storage(PUBLISHED).exists(version.published_manifest_key))
        self.assertTrue(get_document_storage(PUBLISHED).exists(version.published_source_key))
        record = managed_records()[0]
        self.assertEqual(record.metadata["sourceSha256"], version.sha256)
        self.assertEqual(record.metadata["managedSourceVersionId"], version.id)
        self.assertIn("itemize", record.text)
        self.client.force_login(self.user)
        response = self.client.get(record.open_target["sourceUrl"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"]["sourceSha256"], version.sha256)

    def test_update_replaces_live_chunks_without_deleting_history(self):
        first, _ = self.import_text("old unique wording")
        second, _ = self.import_text("new unique wording")

        self.source.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(self.source.current_version, second)
        self.assertEqual(first.status, "superseded")
        live_text = " ".join(record.text for record in managed_records())
        self.assertIn("new unique wording", live_text)
        self.assertNotIn("old unique wording", live_text)
        self.assertTrue(get_document_storage(RAW).exists(first.raw_key))

    def test_unchanged_import_is_an_idempotent_no_op(self):
        first, created = self.import_text("same source")
        again, created_again = self.import_text("same source")

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, again.id)
        self.assertEqual(self.source.versions.count(), 1)

    def test_reupload_resumes_an_uploaded_version_after_an_interrupted_worker(self):
        content = b"source text waiting for its worker"
        with patch("apps.sources.publication.threading.Thread") as thread:
            with self.captureOnCommitCallbacks(execute=True):
                queued, created = import_version(
                    source=self.source, content=content, filename="source.txt",
                    content_type="text/plain", actor=self.user, background=True,
                )

        self.assertTrue(created)
        self.assertEqual(queued.status, "uploaded")
        thread.return_value.start.assert_called_once()

        resumed, created_again = import_version(
            source=self.source, content=content, filename="source.txt",
            content_type="text/plain", actor=self.user,
        )

        resumed.refresh_from_db()
        self.assertFalse(created_again)
        self.assertEqual(resumed.id, queued.id)
        self.assertEqual(resumed.status, "pending_review")
        self.assertEqual(self.source.versions.count(), 1)

    def test_invalid_import_never_changes_the_live_version(self):
        live, _ = self.import_text("valid published source")
        failed, created = import_version(
            source=self.source, content=b"not a supported format", filename="scan.bin",
            actor=self.user, publish=True,
        )

        self.assertTrue(created)
        failed.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(failed.status, "failed")
        self.assertEqual(self.source.current_version, live)
        self.assertEqual(self.source.state, "published")
        self.assertFalse(get_document_storage(PUBLISHED).exists(failed.raw_key))

    def test_retire_and_rollback_preserve_an_attributable_history(self):
        first, _ = self.import_text("first edition")
        self.import_text("second edition")
        retire_source(self.source, actor=self.user)
        self.source.refresh_from_db()
        self.assertEqual(self.source.state, "retired")
        self.assertEqual(managed_records(), [])

        rollback_source(self.source, first, actor=self.user)
        self.source.refresh_from_db()
        self.assertEqual(self.source.current_version, first)
        self.assertEqual(self.source.state, "published")
        self.assertIn("first edition", managed_records()[0].text)
        self.assertTrue(ManagedSourceEvent.objects.filter(action="retired", actor=self.user).exists())
        self.assertTrue(ManagedSourceEvent.objects.filter(action="rolled_back", actor=self.user).exists())

    def test_storage_failure_cannot_move_the_live_pointer(self):
        live, _ = self.import_text("live edition")
        candidate, _ = self.import_text("candidate edition", publish=False)
        published = get_document_storage(PUBLISHED)
        def refuse(*_args, **_kwargs):
            raise OSError("down")

        with patch("apps.sources.publication.get_document_storage", side_effect=lambda area: (
            type("Broken", (), {"put_bytes": refuse, "put_file": refuse, "exists": refuse})()
            if area == PUBLISHED else get_document_storage(area)
        )):
            with self.assertRaises(PublicationError):
                publish_version(candidate, actor=self.user)

        self.source.refresh_from_db()
        self.assertEqual(self.source.current_version, live)
        self.assertTrue(published.exists(live.published_manifest_key))

        # The validation work remains reusable after a transient provider
        # outage; an operator can retry publication without re-uploading.
        publish_version(candidate, actor=self.user)
        self.source.refresh_from_db()
        self.assertEqual(self.source.current_version, candidate)

    def test_raw_storage_failure_is_recorded_instead_of_leaving_an_uploaded_row(self):
        class BrokenStorage:
            def put_bytes(self, **_kwargs):
                raise OSError("provider unavailable")

        with patch("apps.sources.publication.get_document_storage", return_value=BrokenStorage()):
            version, created = import_version(
                source=self.source, content=b"readable text", filename="source.txt",
                content_type="text/plain", actor=self.user,
            )

        self.assertTrue(created)
        self.source.refresh_from_db()
        self.assertEqual(version.status, "failed")
        self.assertIn("Raw storage failed", version.error)
        self.assertEqual(self.source.state, "failed")
        self.assertTrue(ManagedSourceEvent.objects.filter(version=version, action="failed").exists())

        resumed, created_again = import_version(
            source=self.source, content=b"readable text", filename="source.txt",
            content_type="text/plain", actor=self.user,
        )
        resumed.refresh_from_db()
        self.assertFalse(created_again)
        self.assertEqual(resumed.status, "pending_review")
        self.assertEqual(resumed.error, "")
        self.source.refresh_from_db()
        self.assertEqual(self.source.state, "draft")

    def test_case_import_proposes_metadata_but_waits_for_review(self):
        case = ManagedSource.objects.create(slug="tenant-v-landlord", title="Uploaded decision", kind="case")
        text = """IN THE COURT OF APPEALS OF OHIO\nTenant v. Landlord\n2026-Ohio-123\nSeptember 4, 2026\n\nOPINION\nThe judgment is affirmed."""
        version, _ = import_version(
            source=case, content=text.encode(), filename="decision.txt", content_type="text/plain",
            actor=self.user, publish=False,
        )

        case.refresh_from_db()
        self.assertEqual(version.status, "pending_review")
        self.assertEqual(case.state, "draft")
        self.assertEqual(case.citation, "2026-Ohio-123")
        self.assertEqual(case.decision_date.isoformat(), "2026-09-04")
        self.assertIn("Tenant v. Landlord", case.title)

class BackgroundImportTests(ManagedSourceFixture, TestCase):
    """The worker runs on its own connection, so what it can see matters."""

    def test_worker_starts_only_after_the_caller_commits(self):
        started = []
        with patch("apps.sources.publication.threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: started.append(True)
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                import_version(
                    source=self.source, content=b"deferred until commit", filename="source.txt",
                    content_type="text/plain", actor=self.user, background=True,
                )
                # Inside the caller's transaction the row is not yet durable:
                # a worker on another connection could not read it.
                self.assertEqual(started, [])

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(started, [True])

    def test_a_worker_that_dies_records_the_failure_instead_of_staying_silent(self):
        with patch("apps.sources.publication.threading.Thread") as thread, patch("django.db.close_old_connections"):
            # This fake runs inline; closing connections would abort TestCase's PostgreSQL transaction.
            thread.side_effect = lambda target, **_kwargs: SimpleNamespace(start=target)
            # The worker looks the function up on the module; this call site
            # holds the real one, so only the worker's re-entry is broken.
            with patch(
                "apps.sources.publication.import_version",
                side_effect=OSError("the row is not visible on this connection"),
            ):
                with self.captureOnCommitCallbacks(execute=True):
                    version, _created = import_version(
                        source=self.source, content=b"text the worker never reaches",
                        filename="source.txt", content_type="text/plain", actor=self.user,
                        background=True,
                    )

        version.refresh_from_db()
        self.assertEqual(version.status, "failed")
        self.assertIn("Background validation failed", version.error)
        self.assertFalse(version.validation_report["valid"])
        self.assertTrue(
            ManagedSourceEvent.objects.filter(version=version, action="failed").exists()
        )

    def test_a_stale_validating_row_is_resumable_but_a_running_one_is_not(self):
        content = b"a long document whose worker was killed mid-parse"
        with patch("apps.sources.publication.threading.Thread"):
            with self.captureOnCommitCallbacks(execute=True):
                version, _created = import_version(
                    source=self.source, content=content, filename="source.txt",
                    content_type="text/plain", actor=self.user, background=True,
                )
        ManagedSourceVersion.objects.filter(pk=version.pk).update(
            status="validating", validation_started_at=timezone.now(),
        )
        version.refresh_from_db()
        self.assertFalse(is_resumable(version))

        ManagedSourceVersion.objects.filter(pk=version.pk).update(
            validation_started_at=timezone.now() - STALE_VALIDATION - timedelta(minutes=1),
        )
        resumed, created_again = import_version(
            source=self.source, content=content, filename="source.txt",
            content_type="text/plain", actor=self.user,
        )

        resumed.refresh_from_db()
        self.assertFalse(created_again)
        self.assertEqual(resumed.id, version.id)
        self.assertEqual(resumed.status, "pending_review")
        self.assertEqual(self.source.versions.count(), 1)

    def test_reuploading_a_failed_version_retries_it_without_raising(self):
        failed, _created = import_version(
            source=self.source, content=b"%PDF-1.4 truncated", filename="brief.pdf",
            content_type="application/pdf", actor=self.user, publish=True,
        )
        self.assertEqual(failed.status, "failed")

        with patch(
            "apps.sources.publication.PdfReader",
            return_value=SimpleNamespace(pages=[FakePdfPage("The judgment is affirmed.")]),
        ):
            retried, created_again = import_version(
                source=self.source, content=b"%PDF-1.4 truncated", filename="brief.pdf",
                content_type="application/pdf", actor=self.user, publish=True,
            )

        retried.refresh_from_db()
        self.source.refresh_from_db()
        self.assertFalse(created_again)
        self.assertEqual(retried.id, failed.id)
        self.assertEqual(retried.status, "published")
        self.assertEqual(self.source.current_version, retried)
        self.assertEqual(retried.chunks.count(), 1)


class ValidationReportTests(ManagedSourceFixture, TestCase):
    def test_a_format_without_pages_reports_them_unmeasured_not_zero(self):
        version, _created = self.import_text("A landlord must itemize deductions.", publish=False)

        version.refresh_from_db()
        self.assertIsNone(version.validation_report["pages"])
        self.assertEqual(version.validation_report["pages_status"], "unmeasured")
        self.assertIsNone(version.chunks.first().page_start)

    def test_a_pdf_reports_its_pages_and_stamps_each_chunk(self):
        with patch("apps.sources.publication.PdfReader", return_value=SimpleNamespace(pages=[
            FakePdfPage("First page paragraph."),
            FakePdfPage("Second page paragraph."),
        ])):
            version, _created = import_version(
                source=self.source, content=b"%PDF-1.4 stand-in", filename="treatise.pdf",
                content_type="application/pdf", actor=self.user, publish=False,
            )

        version.refresh_from_db()
        self.assertEqual(version.validation_report["pages"], 2)
        self.assertEqual(version.validation_report["pages_status"], "measured")
        chunk = version.chunks.first()
        self.assertEqual((chunk.page_start, chunk.page_end), (1, 2))

    def test_a_text_content_type_does_not_wave_through_rtf(self):
        version, _created = import_version(
            source=self.source, content=rb"{\rtf1\ansi Rich text body}", filename="notice.rtf",
            content_type="text/rtf", actor=self.user,
        )

        self.assertEqual(version.status, "failed")
        self.assertIn("Upload a PDF, DOCX, Markdown, or UTF-8 plain-text file.", version.error)

    def test_a_reviewed_title_matching_the_filename_survives_import(self):
        case = ManagedSource.objects.create(
            slug="tenant-v-landlord-2", title="Tenant v Landlord", kind="case",
        )
        import_version(
            source=case, content=b"IN THE COURT OF APPEALS\n\nV. CONCLUSION\n\nAffirmed.",
            filename="Tenant v Landlord.txt", content_type="text/plain", actor=self.user,
        )

        case.refresh_from_db()
        self.assertEqual(case.title, "Tenant v Landlord")


class PublishCommandTests(ManagedSourceFixture, TestCase):
    def test_publish_without_a_version_skips_one_that_never_validated(self):
        live, _created = self.import_text("first edition")
        failed, _created = import_version(
            source=self.source, content=b"not a supported format", filename="scan.bin",
            actor=self.user,
        )
        self.assertEqual(failed.status, "failed")

        call_command("manage_content_source", "publish", self.source.slug, stdout=StringIO())

        self.source.refresh_from_db()
        self.assertEqual(self.source.current_version, live)

    def test_publish_reports_a_refusal_as_a_command_error(self):
        candidate, _created = self.import_text("candidate edition", publish=False)
        get_document_storage(VALIDATED).delete(candidate.validated_manifest_key)

        with self.assertRaises(CommandError):
            call_command(
                "manage_content_source", "publish", self.source.slug, "--source-version",
                str(candidate.number),
            )


class AdminActionTests(ManagedSourceFixture, TestCase):
    def test_a_bulk_publish_names_the_rows_it_could_not_publish(self):
        live, _created = self.import_text("live edition")
        failed, _created = import_version(
            source=self.source, content=b"not a supported format", filename="scan.bin",
            actor=self.user,
        )
        self.assertEqual(failed.status, "failed")
        request = RequestFactory().post("/admin/")
        request.user = self.user
        seen = []

        with patch.object(
            ManagedSourceVersionAdmin, "message_user",
            lambda _self, _request, message, **_kwargs: seen.append(str(message)),
        ):
            ManagedSourceVersionAdmin(ManagedSourceVersion, site).publish_selected(
                request, ManagedSourceVersion.objects.filter(pk__in=[live.pk, failed.pk]),
            )

        self.source.refresh_from_db()
        self.assertEqual(self.source.current_version, live)
        self.assertIn("Published 1 version(s).", seen)
        self.assertTrue(any(message.startswith("Not changed:") for message in seen))

from django.contrib import admin, messages
from django import forms
from django.utils import timezone
from apps.sources.ordinance_storage import store_upload

from apps.sources.models import (
    ManagedSource, ManagedSourceChunk, ManagedSourceEvent, ManagedSourceVersion,
    OrdinanceDocument, OrdinanceOverride, RetrievedDocument, SourceConfiguration,
    UserOAuthConnection, UserResource, UserSourceIdentity,
)
from apps.sources.publication import (
    PublicationError, import_version, publish_version, retire_source, rollback_source,
)


def _report_refusals(model_admin, request, refused):
    """Name every selected row the pipeline would not act on.

    The changelist offers rows in every status, so a bulk action routinely
    meets one it cannot publish. Each is reported by name; the rows around it
    still go through.
    """
    if refused:
        model_admin.message_user(
            request,
            "Not changed: " + "; ".join(refused),
            level=messages.ERROR,
        )


@admin.register(SourceConfiguration)
class SourceConfigurationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "enabled", "updated_at")
    list_filter = ("kind", "enabled")
    search_fields = ("name",)
    legalserver_fields = (
        "legalserver_base_url",
        "legalserver_api_token",
    )
    legalserver_advanced_fields = (
        "legalserver_matters_path",
        "legalserver_matter_documents_path",
        "legalserver_user_filter_param",
    )
    sharepoint_fields = (
        "sharepoint_site_id",
        "sharepoint_drive_id",
        "sharepoint_case_folder_template",
        "sharepoint_server_access_token",
    )
    openai_fields = ("openai_base_url", "openai_api_key", "openai_model", "openai_enabled")

    def get_fieldsets(self, request, obj=None):
        base = [(None, {"fields": ("name", "kind", "enabled")})]
        if obj and obj.kind == "legalserver":
            return base + [
                ("LegalServer API", {"fields": self.legalserver_fields}),
                (
                    "Advanced LegalServer API overrides",
                    {
                        "classes": ("collapse",),
                        "fields": self.legalserver_advanced_fields,
                        "description": "Most sites should leave these blank. Use only for a proxy, API gateway, or confirmed nonstandard endpoint shape.",
                    },
                ),
            ]
        if obj and obj.kind == "sharepoint":
            return base + [
                (
                    "SharePoint Online",
                    {
                        "fields": self.sharepoint_fields,
                        "description": "Delegated Office 365 user connections are preferred at runtime. These server credentials are the fallback.",
                    },
                )
            ]
        if obj and obj.kind == "openai":
            return base + [("OpenAI-compatible AI backend", {"fields": self.openai_fields})]
        return base + [
            ("LegalServer API", {"classes": ("collapse",), "fields": self.legalserver_fields}),
            (
                "Advanced LegalServer API overrides",
                {
                    "classes": ("collapse",),
                    "fields": self.legalserver_advanced_fields,
                    "description": "Most sites should leave these blank. Use only for a proxy, API gateway, or confirmed nonstandard endpoint shape.",
                },
            ),
            (
                "SharePoint Online",
                {
                    "classes": ("collapse",),
                    "fields": self.sharepoint_fields,
                    "description": "Delegated Office 365 user connections are preferred at runtime. These server credentials are the fallback.",
                },
            ),
            ("OpenAI-compatible AI backend", {"classes": ("collapse",), "fields": self.openai_fields}),
        ]


@admin.register(UserOAuthConnection)
class UserOAuthConnectionAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "enabled", "tenant_id", "client_id", "expires_at", "updated_at")
    list_filter = ("provider", "enabled")
    search_fields = ("user__username", "user__email", "tenant_id", "client_id")
    fieldsets = (
        (None, {"fields": ("user", "provider", "enabled")}),
        ("Office 365 app", {"fields": ("tenant_id", "client_id", "scopes", "expires_at")}),
        ("Tokens", {"fields": ("access_token", "refresh_token")}),
    )


@admin.register(UserSourceIdentity)
class UserSourceIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "identifier", "enabled", "is_verified", "updated_at")
    list_filter = ("provider", "enabled", "is_verified")
    search_fields = ("user__username", "user__email", "identifier")
    fieldsets = (
        (None, {"fields": ("user", "provider", "enabled")}),
        (
            "External identity mapping",
            {
                "fields": ("identifier", "is_verified"),
                "description": "For LegalServer, this is the email or username used to map the logged-in app user to LegalServer permissions. Disable this row to turn off an individual mapping.",
            },
        ),
    )

    def save_model(self, request, obj, form, change):
        if change and {"identifier", "user", "provider"}.intersection(form.changed_data):
            obj.is_verified = False
            self.message_user(request, "Identity changed. Verify the new mapping and mark it verified in a separate save.")
        super().save_model(request, obj, form, change)


@admin.register(RetrievedDocument)
class RetrievedDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "source_kind", "source_label", "citation", "created_at")
    list_filter = ("source_kind",)
    search_fields = ("title", "snippet", "citation")


@admin.register(UserResource)
class UserResourceAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "resource_type", "original_filename", "updated_at")
    list_filter = ("resource_type",)
    search_fields = ("title", "original_filename", "text", "user__username", "user__email")


class ManagedSourceForm(forms.ModelForm):
    upload = forms.FileField(
        required=False,
        help_text="PDF, DOCX, Markdown, or plain text. The immutable original goes to raw storage.",
    )
    plain_text = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 8}),
        help_text="Alternatively paste the source's plain-text representation.",
    )
    version_label = forms.CharField(required=False, help_text="Edition, date, or other operator-facing version label.")
    publish_immediately = forms.BooleanField(
        required=False,
        help_text="Publish after validation. Leave off for a new case so proposed metadata can be reviewed first.",
    )

    class Meta:
        model = ManagedSource
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("upload") and cleaned.get("plain_text"):
            raise forms.ValidationError("Provide either a source document or plain text, not both.")
        if not self.instance.pk and not cleaned.get("upload") and not cleaned.get("plain_text"):
            raise forms.ValidationError("Upload a source document or paste its plain text.")
        return cleaned


class ManagedSourceVersionInline(admin.TabularInline):
    model = ManagedSourceVersion
    extra = 0
    fields = ("number", "label", "status", "sha256", "chunk_count", "imported_by", "imported_at", "published_at")
    readonly_fields = fields
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ManagedSourceEventInline(admin.TabularInline):
    model = ManagedSourceEvent
    extra = 0
    fields = ("action", "version", "actor", "detail", "created_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ManagedSource)
class ManagedSourceAdmin(admin.ModelAdmin):
    form = ManagedSourceForm
    list_display = (
        "title", "kind", "state", "current_version", "current_checksum",
        "last_indexed_at", "last_successful_refresh_at",
    )
    list_filter = ("kind", "state", "jurisdiction", "publication_status")
    search_fields = ("slug", "title", "citation", "court", "county", "municipality", "source_locator")
    readonly_fields = (
        "state", "current_version", "last_successful_refresh_at", "last_indexed_at",
        "created_by", "created_at", "updated_at",
    )
    actions = ("retire_selected", "restore_selected")
    inlines = (ManagedSourceVersionInline, ManagedSourceEventInline)
    fieldsets = (
        ("Source", {"fields": ("slug", "title", "kind", "jurisdiction", "description")}),
        ("Import a new version", {"fields": ("upload", "plain_text", "version_label", "publish_immediately")}),
        ("Provenance", {"fields": ("source_locator", "source_system_id", "refresh_command")}),
        ("Case metadata — review before publishing", {
            "fields": ("court", "county", "municipality", "appellate_district", "decision_date",
                       "citation", "publication_status"),
            "description": "A document import may propose blank fields. An authorized person remains responsible for correction and publication.",
        }),
        ("Publication", {"fields": ("state", "current_version", "last_successful_refresh_at", "last_indexed_at")}),
        ("Audit", {"fields": ("created_by", "created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        upload = form.cleaned_data.get("upload")
        plain_text = form.cleaned_data.get("plain_text")
        if upload or plain_text:
            content = upload.read() if upload else plain_text.encode("utf-8")
            filename = upload.name if upload else f"{obj.slug}.txt"
            content_type = (getattr(upload, "content_type", "") if upload else "text/plain") or ""
            requested_publish = form.cleaned_data.get("publish_immediately", False)
            may_publish = request.user.has_perm("sources.publish_managedsource")
            try:
                version, created = import_version(
                    source=obj, content=content, filename=filename, content_type=content_type,
                    label=form.cleaned_data.get("version_label", ""), actor=request.user,
                    publish=requested_publish and may_publish, background=True,
                )
            except PublicationError as exc:
                self.message_user(request, f"The version was not published: {exc}", level=messages.ERROR)
                return
            if requested_publish and not may_publish:
                self.message_user(
                    request,
                    "The version will remain unpublished after validation: publication permission is required.",
                    level=messages.WARNING,
                )
            if version.status == "uploaded":
                self.message_user(request, f"Version {version.number} was uploaded; validation is running in the background.")
            elif version.status == "failed":
                self.message_user(
                    request, f"Version {version.number} failed: {version.error}", level=messages.ERROR,
                )
            elif created:
                self.message_user(
                    request,
                    f"Version {version.number} {'published' if version.status == 'published' else 'validated and awaiting review'}.",
                )
            elif version.status == "validating":
                self.message_user(
                    request, f"Version {version.number} is already being validated; no duplicate was created.",
                )
            else:
                self.message_user(
                    request,
                    f"That exact file is already recorded as version {version.number} "
                    f"({version.get_status_display().lower()}); no duplicate version was created.",
                )

    @admin.display(description="SHA-256")
    def current_checksum(self, obj):
        return obj.current_version.sha256[:12] if obj.current_version else "—"

    def has_publish_permission(self, request):
        return request.user.has_perm("sources.publish_managedsource")

    @admin.action(description="Retire selected sources from research", permissions=("publish",))
    def retire_selected(self, request, queryset):
        for source in queryset:
            retire_source(source, actor=request.user)
        self.message_user(request, f"Retired {queryset.count()} source(s); immutable version history was retained.")

    @admin.action(description="Restore selected retired sources at their current version", permissions=("publish",))
    def restore_selected(self, request, queryset):
        restored, refused = 0, []
        for source in queryset:
            if not source.current_version:
                refused.append(f"{source.slug}: no current version to restore")
                continue
            try:
                publish_version(source.current_version, actor=request.user)
            except PublicationError as exc:
                refused.append(f"{source.slug}: {exc}")
                continue
            restored += 1
        self.message_user(request, f"Restored {restored} source(s).")
        _report_refusals(self, request, refused)


@admin.register(ManagedSourceVersion)
class ManagedSourceVersionAdmin(admin.ModelAdmin):
    list_display = (
        "source", "number", "label", "status", "chunk_count", "short_checksum",
        "imported_by", "imported_at", "published_at",
    )
    list_filter = ("status", "source__kind", "parser_version", "chunker_version")
    search_fields = ("source__title", "source__slug", "original_filename", "sha256", "error")
    readonly_fields = (
        "source", "number", "status", "original_filename", "content_type", "size_bytes", "sha256",
        "source_modified_at", "source_etag", "source_provenance", "imported_at", "imported_by", "parser_version",
        "chunker_version", "raw_key", "validated_manifest_key", "published_manifest_key", "published_source_key",
        "chunk_count", "validation_report", "error", "published_at", "retired_at",
    )
    actions = ("publish_selected", "rollback_selected")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="SHA-256")
    def short_checksum(self, obj):
        return obj.sha256[:12]

    def has_publish_permission(self, request):
        return request.user.has_perm("sources.publish_managedsource")

    @admin.action(description="Publish selected validated versions", permissions=("publish",))
    def publish_selected(self, request, queryset):
        published, refused = 0, []
        for version in queryset:
            try:
                publish_version(version, actor=request.user)
            except PublicationError as exc:
                refused.append(f"{version}: {exc}")
                continue
            published += 1
        self.message_user(request, f"Published {published} version(s).")
        _report_refusals(self, request, refused)

    @admin.action(description="Roll each source back to the selected version", permissions=("publish",))
    def rollback_selected(self, request, queryset):
        rolled_back, refused = 0, []
        for version in queryset:
            try:
                rollback_source(version.source, version, actor=request.user)
            except PublicationError as exc:
                refused.append(f"{version}: {exc}")
                continue
            rolled_back += 1
        self.message_user(request, f"Rolled back {rolled_back} source(s).")
        _report_refusals(self, request, refused)


@admin.register(ManagedSourceChunk)
class ManagedSourceChunkAdmin(admin.ModelAdmin):
    list_display = ("version", "ordinal", "heading", "short_checksum")
    search_fields = ("version__source__title", "heading", "text", "sha256")
    readonly_fields = ("version", "ordinal", "heading", "text", "page_start", "page_end", "sha256")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="SHA-256")
    def short_checksum(self, obj):
        return obj.sha256[:12]


@admin.register(ManagedSourceEvent)
class ManagedSourceEventAdmin(admin.ModelAdmin):
    list_display = ("source", "action", "version", "actor", "created_at")
    list_filter = ("action", "source__kind")
    search_fields = ("source__title", "source__slug", "actor__username")
    readonly_fields = ("source", "version", "action", "actor", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OrdinanceDocumentForm(forms.ModelForm):
    """Upload a document, or point at one, without touching the filesystem."""

    upload = forms.FileField(
        required=False,
        help_text="Optional. Uploading replaces this row's stored file; the publisher URL can be kept alongside it.",
    )

    class Meta:
        model = OrdinanceDocument
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("upload") and not cleaned.get("url") and not self.instance.storage_key:
            raise forms.ValidationError("Give the document a publisher URL, an upload, or both.")
        return cleaned

    def save(self, commit=True):
        document = super().save(commit=False)
        upload = self.cleaned_data.get("upload")
        if upload:
            stored = store_upload(
                content=upload.read(),
                municipality_slug=document.municipality_slug,
                target_key=document.target_key,
                filename=upload.name,
                content_type=getattr(upload, "content_type", "") or "",
            )
            for field, value in stored.items():
                setattr(document, field, value)
        if commit:
            document.save()
        return document


@admin.register(OrdinanceDocument)
class OrdinanceDocumentAdmin(admin.ModelAdmin):
    form = OrdinanceDocumentForm
    list_display = (
        "municipality_slug", "target_key", "title", "source_type",
        "status", "verified", "has_file", "updated_at",
    )
    list_filter = ("status", "source_type", "verified", "municipality_slug")
    search_fields = ("municipality_slug", "target_key", "title", "url", "notes", "sha256")
    readonly_fields = ("storage_key", "sha256", "size_bytes", "original_filename", "created_at", "updated_at")
    actions = ("mark_verified", "mark_superseded", "mark_active")
    fieldsets = (
        ("Authority", {"fields": ("municipality_slug", "target_key", "title", "source_type")}),
        ("Document", {"fields": ("url", "upload", "storage_key", "original_filename", "content_type",
                                 "size_bytes", "sha256")}),
        ("Where the authority sits in it", {
            "fields": ("extract_start", "extract_end", "extract_pages"),
            "description": "A council packet is mostly other business. Without these the whole packet is the record.",
        }),
        ("Standing", {"fields": ("status", "superseded_by", "verified", "verified_by", "verified_at")}),
        ("Notes", {"fields": ("notes", "added_by", "created_at", "updated_at")}),
    )

    @admin.display(boolean=True, description="File")
    def has_file(self, obj):
        return bool(obj.storage_key)

    @admin.action(description="Mark selected documents verified against the publisher")
    def mark_verified(self, request, queryset):
        updated = queryset.update(
            verified=True, verified_by=request.user.get_username(), verified_at=timezone.now(),
        )
        self.message_user(request, f"{updated} document(s) marked verified.")

    @admin.action(description="Mark selected documents superseded")
    def mark_superseded(self, request, queryset):
        # Deliberately does not delete: which document an assertion rested on is
        # part of the assertion.
        updated = queryset.update(status="superseded")
        self.message_user(request, f"{updated} document(s) marked superseded and kept on the record.")

    @admin.action(description="Mark selected documents active")
    def mark_active(self, request, queryset):
        updated = queryset.update(status="active", superseded_by=None)
        self.message_user(request, f"{updated} document(s) marked active.")


@admin.register(OrdinanceOverride)
class OrdinanceOverrideAdmin(admin.ModelAdmin):
    list_display = (
        "municipality_slug", "target_key", "citation", "legal_status",
        "verified", "is_active", "changed_fields", "updated_at",
    )
    list_filter = ("is_active", "verified", "legal_status", "municipality_slug")
    search_fields = ("municipality_slug", "target_key", "citation", "notes")
    readonly_fields = ("created_at", "updated_at")
    actions = ("mark_reviewed",)
    fieldsets = (
        ("Authority", {"fields": ("municipality_slug", "target_key", "is_active")}),
        ("Citation", {
            "fields": ("citation", "title", "act_file_number", "enacted_as", "source_type"),
            "description": "Leave a field blank to keep the generated value. An override is a patch, not a replacement.",
        }),
        ("Dates and standing", {
            "fields": ("legal_status", "enacted_date", "effective_date", "amended_date", "repeal_date"),
        }),
        ("Preemption", {
            "fields": ("preemption_status", "preemption_note", "controlling_case",
                       "court_treatment", "preemption_confidence"),
        }),
        ("Review", {"fields": ("verified", "reviewed_by", "reviewed_at", "notes", "created_at", "updated_at")}),
    )

    @admin.display(description="Overrides")
    def changed_fields(self, obj):
        return ", ".join(sorted(obj.applied_fields())) or "—"

    @admin.action(description="Mark reviewed by me, now")
    def mark_reviewed(self, request, queryset):
        updated = queryset.update(
            verified=True, reviewed_by=request.user.get_username(), reviewed_at=timezone.now(),
        )
        self.message_user(request, f"{updated} override(s) marked reviewed.")

from django.db import models
from django.db.utils import DatabaseError, OperationalError, ProgrammingError


class SourceConfiguration(models.Model):
    KIND_CHOICES = [
        ("legalserver", "LegalServer"),
        ("sharepoint", "SharePoint"),
        ("openai", "OpenAI-compatible AI backend"),
        ("rag", "RAG database"),
        ("local_cases", "Local archived cases"),
        ("user_resources", "User-specific resources"),
    ]

    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=80, choices=KIND_CHOICES)
    enabled = models.BooleanField(default=True)

    legalserver_base_url = models.URLField("LegalServer base URL", blank=True)
    legalserver_api_token = models.CharField("API token", max_length=500, blank=True)
    legalserver_matters_path = models.CharField(
        "Matters path",
        max_length=255,
        blank=True,
        help_text="Advanced override. Leave blank to use /api/v2/matters.",
    )
    legalserver_matter_documents_path = models.CharField(
        "Matter documents path",
        max_length=255,
        blank=True,
        help_text="Advanced override. Leave blank to use /api/v1/matters/{matter_id}/documents.",
    )
    legalserver_user_filter_param = models.CharField(
        "User filter parameter",
        max_length=120,
        blank=True,
        help_text="Advanced override. Leave blank to disable server-side user filtering.",
    )

    sharepoint_site_id = models.CharField(max_length=255, blank=True)
    sharepoint_drive_id = models.CharField(max_length=255, blank=True)
    sharepoint_case_folder_template = models.CharField(max_length=500, blank=True)
    sharepoint_server_access_token = models.TextField(blank=True)

    openai_base_url = models.URLField(blank=True)
    openai_api_key = models.CharField(max_length=500, blank=True)
    openai_model = models.CharField(max_length=120, blank=True)
    openai_enabled = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self):
        return self.name

    @classmethod
    def effective_settings(cls, kind, fallback):
        try:
            config = cls.objects.filter(kind=kind, enabled=True).order_by("-updated_at").first()
        except (DatabaseError, OperationalError, ProgrammingError):
            return fallback
        if not config:
            return fallback
        overrides = config.as_settings()
        return {**fallback, **{key: value for key, value in overrides.items() if value not in ("", None)}}

    def as_settings(self):
        if self.kind == "legalserver":
            return {
                "base_url": self.legalserver_base_url,
                "api_token": self.legalserver_api_token,
                "matters_path": self.legalserver_matters_path,
                "matter_documents_path": self.legalserver_matter_documents_path,
                "user_filter_param": self.legalserver_user_filter_param,
            }
        if self.kind == "sharepoint":
            return {
                "site_id": self.sharepoint_site_id,
                "drive_id": self.sharepoint_drive_id,
                "case_folder_template": self.sharepoint_case_folder_template,
                "access_token": self.sharepoint_server_access_token,
            }
        if self.kind == "openai":
            return {
                "base_url": self.openai_base_url,
                "api_key": self.openai_api_key,
                "model": self.openai_model,
                "enabled": self.openai_enabled,
            }
        return {}


class UserOAuthConnection(models.Model):
    PROVIDER_CHOICES = [
        ("office365", "Office 365"),
    ]

    user = models.ForeignKey("auth.User", related_name="oauth_connections", on_delete=models.CASCADE)
    provider = models.CharField(max_length=80, choices=PROVIDER_CHOICES)
    enabled = models.BooleanField(default=True)
    tenant_id = models.CharField(max_length=255, blank=True)
    client_id = models.CharField(max_length=255, blank=True)
    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    scopes = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username", "provider"]
        unique_together = [("user", "provider")]

    def __str__(self):
        return f"{self.user} - {self.get_provider_display()}"

    @classmethod
    def access_token_for(cls, user, provider):
        if not user or not getattr(user, "is_authenticated", False):
            return ""
        try:
            connection = cls.objects.filter(user=user, provider=provider, enabled=True).first()
        except (DatabaseError, OperationalError, ProgrammingError):
            return ""
        return connection.access_token if connection and connection.access_token else ""


class UserSourceIdentity(models.Model):
    PROVIDER_CHOICES = [
        ("legalserver", "LegalServer"),
    ]

    user = models.ForeignKey("auth.User", related_name="source_identities", on_delete=models.CASCADE)
    provider = models.CharField(max_length=80, choices=PROVIDER_CHOICES)
    identifier = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False, help_text="Administrator has verified this principal and its LegalServer account.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username", "provider"]
        unique_together = [("user", "provider")]

    def __str__(self):
        return f"{self.user} - {self.get_provider_display()}: {self.identifier}"

    @classmethod
    def identifier_for(cls, user, provider):
        if not user or not getattr(user, "is_authenticated", False):
            return ""
        try:
            identity = cls.objects.filter(user=user, provider=provider, enabled=True, is_verified=True).first()
        except (DatabaseError, OperationalError, ProgrammingError):
            return ""
        return identity.identifier if identity and identity.identifier else ""


class UserResource(models.Model):
    RESOURCE_TYPE_CHOICES = [
        ("case", "Case"),
        ("brief", "Brief"),
        ("example", "Example"),
        ("other", "Other"),
    ]

    user = models.ForeignKey("auth.User", related_name="knowledge_resources", on_delete=models.CASCADE)
    title = models.CharField(max_length=500)
    resource_type = models.CharField(max_length=40, choices=RESOURCE_TYPE_CHOICES, default="other")
    original_filename = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    extractor = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class RetrievedDocument(models.Model):
    source_kind = models.CharField(max_length=80)
    source_label = models.CharField(max_length=255)
    external_id = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=500)
    snippet = models.TextField(blank=True)
    url = models.URLField(blank=True)
    citation = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class ManagedSource(models.Model):
    """One operator-maintained legal source and the version currently in research."""

    KIND_CHOICES = [
        ("treatise", "Treatise or handbook"),
        ("case", "Case decision"),
        ("ordinance", "Local ordinance"),
        ("statute", "Statute or regulation"),
        ("other", "Other approved legal source"),
    ]
    STATE_CHOICES = [
        ("draft", "Draft / no published version"),
        ("published", "Published"),
        ("retired", "Retired"),
        ("failed", "Latest import failed"),
    ]

    slug = models.SlugField(max_length=180, unique=True)
    title = models.CharField(max_length=500)
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    jurisdiction = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    source_locator = models.TextField(blank=True, help_text="Publisher URL, path, or other human-readable locator.")
    source_system_id = models.CharField(max_length=255, blank=True)
    refresh_command = models.CharField(
        max_length=80, blank=True,
        choices=[("", "Manual upload"), ("ohio_revised_code", "Ohio Revised Code"),
                 ("local_ordinances", "Local ordinances")],
    )

    # Case-specific fields are deliberately reviewable columns rather than an
    # opaque model response. They are harmlessly blank for other source kinds.
    court = models.CharField(max_length=255, blank=True)
    county = models.CharField(max_length=255, blank=True)
    municipality = models.CharField(max_length=255, blank=True)
    appellate_district = models.CharField(max_length=120, blank=True)
    decision_date = models.DateField(null=True, blank=True)
    citation = models.CharField(max_length=500, blank=True)
    publication_status = models.CharField(max_length=80, blank=True)

    state = models.CharField(max_length=20, choices=STATE_CHOICES, default="draft")
    current_version = models.ForeignKey(
        "ManagedSourceVersion", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="current_for_sources", editable=False,
    )
    last_successful_refresh_at = models.DateTimeField(null=True, blank=True)
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="created_managed_sources",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "title"]
        permissions = [("publish_managedsource", "Can publish, retire, and roll back managed sources")]

    def __str__(self):
        return self.title


class ManagedSourceVersion(models.Model):
    STATUS_CHOICES = [
        ("uploaded", "Uploaded"),
        ("validating", "Validating"),
        ("pending_review", "Pending metadata review"),
        ("published", "Published"),
        ("superseded", "Superseded"),
        ("failed", "Failed validation or indexing"),
    ]

    source = models.ForeignKey(ManagedSource, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="uploaded")
    original_filename = models.CharField(max_length=500, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64)
    source_modified_at = models.DateTimeField(null=True, blank=True)
    source_etag = models.CharField(max_length=500, blank=True)
    source_provenance = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="imported_source_versions",
    )
    parser_version = models.CharField(max_length=120)
    chunker_version = models.CharField(max_length=120)
    raw_key = models.TextField()
    validated_manifest_key = models.TextField(blank=True)
    published_manifest_key = models.TextField(blank=True)
    published_source_key = models.TextField(blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    validation_report = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    # When validation last started. A "validating" row with an old timestamp is
    # an interrupted worker rather than work in progress.
    validation_started_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["source", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["source", "number"], name="sources_unique_managed_version"),
            models.UniqueConstraint(fields=["source", "sha256"], name="sources_unique_managed_checksum"),
        ]

    def __str__(self):
        return f"{self.source.title} v{self.number}"


class ManagedSourceChunk(models.Model):
    version = models.ForeignKey(ManagedSourceVersion, on_delete=models.CASCADE, related_name="chunks")
    ordinal = models.PositiveIntegerField()
    heading = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    page_start = models.PositiveIntegerField(null=True, blank=True)
    page_end = models.PositiveIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ["version", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["version", "ordinal"], name="sources_unique_managed_chunk"),
        ]

    def __str__(self):
        return f"{self.version} chunk {self.ordinal}"


class ManagedSourceEvent(models.Model):
    source = models.ForeignKey(ManagedSource, on_delete=models.CASCADE, related_name="events")
    version = models.ForeignKey(
        ManagedSourceVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="events",
    )
    action = models.CharField(max_length=40)
    actor = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source}: {self.action}"


class OrdinanceDocument(models.Model):
    """A document standing behind one local-law authority, managed by a person.

    The generated corpus under ``content/ordinances/`` is the product of an
    automated pass over sources a scope file names.  That pass will always be
    behind the people using it: a clerk answers a records request, someone finds
    the signed ordinance the packet only summarized, a codifier reissues a
    chapter.  This is where that arrives without waiting for a code change.

    Documents are never edited in place and never silently replaced.  A better
    copy supersedes the one it replaces and both stay on the record, because
    which document an assertion rested on is part of the assertion -- the Akron
    late-fee discrepancy was only visible because two sources disagreed and both
    were still there to compare.
    """

    SOURCE_TYPE_CHOICES = [
        ("signed_ordinance", "Signed ordinance"),
        ("council_packet", "Council packet"),
        ("official_minutes", "Official minutes"),
        ("official_publication", "Official publication (city record/journal)"),
        ("codifier", "Codifier"),
        ("secondary_reproduction", "Secondary reproduction"),
        ("transcription", "Hand transcription"),
        ("other", "Other"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),
        ("superseded", "Superseded"),
        ("rejected", "Rejected"),
    ]

    municipality_slug = models.CharField(max_length=120, help_text="Scope slug, e.g. lakewood")
    target_key = models.CharField(max_length=120, help_text="Target key within that municipality, e.g. pay-to-stay")

    title = models.CharField(max_length=500)
    source_type = models.CharField(max_length=40, choices=SOURCE_TYPE_CHOICES, default="signed_ordinance")
    url = models.URLField(max_length=1000, blank=True, help_text="Publisher URL, when the document is hosted.")

    storage_key = models.TextField(blank=True, help_text="Set automatically when a file is uploaded.")
    original_filename = models.CharField(max_length=500, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    superseded_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="supersedes",
        help_text="The document that replaced this one. Setting it does not delete anything.",
    )

    # Where in the document the authority actually is.  A council packet is
    # mostly other business; without these the ingest would take the lot.
    extract_start = models.CharField(max_length=500, blank=True, help_text="Text marking the start of the authority.")
    extract_end = models.CharField(max_length=500, blank=True, help_text="Text marking where it ends.")
    extract_pages = models.CharField(max_length=40, blank=True, help_text="PDF page range, e.g. 12-15.")

    verified = models.BooleanField(
        default=False,
        help_text="A person compared this document against the publisher's copy. Nothing automated sets this.",
    )
    verified_by = models.CharField(max_length=255, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)
    added_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["municipality_slug", "target_key", "-created_at"]
        indexes = [
            models.Index(fields=["municipality_slug", "target_key"]),
            models.Index(fields=["status"]),
            models.Index(fields=["source_type"]),
        ]

    def __str__(self):
        return f"{self.municipality_slug}/{self.target_key}: {self.title}"

    @property
    def authority_key(self):
        return (self.municipality_slug, self.target_key)


class OrdinanceOverride(models.Model):
    """Corrections a person makes to one authority's generated metadata.

    Only fields that are filled in take effect; a blank field means "leave the
    generated value alone" rather than "blank it out".  That keeps an override
    a patch rather than a replacement, so a later ingestion improving an
    untouched field is not silently reverted by an old admin edit.
    """

    STATUS_CHOICES = [
        ("", "Leave as generated"),
        ("in_force", "In force"),
        ("repealed", "Repealed"),
        ("expired", "Expired"),
        ("unknown", "Unknown"),
    ]

    municipality_slug = models.CharField(max_length=120)
    target_key = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True, help_text="Clear this to park an override without deleting it.")

    citation = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=500, blank=True)
    act_file_number = models.CharField(max_length=120, blank=True)
    enacted_as = models.CharField(max_length=255, blank=True, help_text="The chapter it was enacted under, if it has since moved.")
    source_type = models.CharField(max_length=40, blank=True, choices=OrdinanceDocument.SOURCE_TYPE_CHOICES)

    legal_status = models.CharField(max_length=20, blank=True, choices=STATUS_CHOICES)
    enacted_date = models.DateField(null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    amended_date = models.DateField(null=True, blank=True)
    repeal_date = models.DateField(null=True, blank=True)

    preemption_status = models.CharField(max_length=80, blank=True)
    preemption_note = models.TextField(blank=True)
    controlling_case = models.CharField(max_length=500, blank=True)
    court_treatment = models.TextField(blank=True)
    preemption_confidence = models.CharField(max_length=80, blank=True)

    verified = models.BooleanField(
        default=False,
        help_text="A person confirmed this authority's text against the publisher.",
    )
    reviewed_by = models.CharField(max_length=255, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["municipality_slug", "target_key"]
        unique_together = [("municipality_slug", "target_key")]

    def __str__(self):
        return f"Override: {self.municipality_slug}/{self.target_key}"

    def applied_fields(self):
        """The generated keys this override actually changes, camelCased."""
        mapping = {
            "citation": self.citation,
            "title": self.title,
            "actFileNumber": self.act_file_number,
            "enactedAs": self.enacted_as,
            "sourceType": self.source_type,
            "legalStatus": self.legal_status,
            "enactedDate": self.enacted_date.isoformat() if self.enacted_date else "",
            "effectiveDate": self.effective_date.isoformat() if self.effective_date else "",
            "amendedDate": self.amended_date.isoformat() if self.amended_date else "",
            "repealDate": self.repeal_date.isoformat() if self.repeal_date else "",
        }
        applied = {key: value for key, value in mapping.items() if value}
        preemption = {
            "status": self.preemption_status,
            "note": self.preemption_note,
            "controlling_case": self.controlling_case,
            "court_treatment": self.court_treatment,
            "confidence": self.preemption_confidence,
        }
        preemption = {key: value for key, value in preemption.items() if value}
        if preemption:
            applied["preemption"] = preemption
        if self.verified:
            applied["verified"] = True
        return applied

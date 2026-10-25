from django.db import models
from django.conf import settings


class Matter(models.Model):
    external_id = models.CharField(max_length=80, unique=True)
    client_name = models.CharField(max_length=255)
    matter_type = models.CharField(max_length=255)
    jurisdiction = models.CharField(max_length=255)
    posture = models.CharField(max_length=255, blank=True)
    risk = models.CharField(max_length=120, blank=True)
    summary = models.TextField(blank=True)
    source_system = models.CharField(max_length=120, default="LegalServer")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["client_name"]
        permissions = [
            ("write_legalserver_documents", "Can upload documents to LegalServer"),
            ("write_legalserver_notes", "Can save case notes to LegalServer"),
            ("write_legalserver_properties", "Can update LegalServer case properties"),
        ]

    def __str__(self):
        return f"{self.external_id} - {self.client_name}"


class MatterRouteAlias(models.Model):
    """A readable case number that addresses a matter in an application URL.

    `Matter.external_id` stays the matter's identity; this is only how a link
    names it. LegalServer can renumber a case, and when it does the old number
    is kept here, not current, so a bookmark made before the change still opens
    the same matter. Read and write it only through `apps.matters.route_aliases`.
    """

    matter = models.ForeignKey(Matter, related_name="route_aliases", on_delete=models.CASCADE)
    # Where the number came from. Provenance only: a URL carries no source
    # system, so an alias must identify one matter across all of them.
    source_system = models.CharField(max_length=120)
    alias = models.CharField(max_length=120)
    normalized_alias = models.CharField(max_length=120, unique=True)
    is_current = models.BooleanField(default=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_current", "-last_seen_at"]
        verbose_name_plural = "matter route aliases"

    def __str__(self):
        return f"{self.alias} -> {self.matter.external_id}"


class MatterFact(models.Model):
    matter = models.ForeignKey(Matter, related_name="facts", on_delete=models.CASCADE)
    slug = models.SlugField(max_length=120)
    title = models.CharField(max_length=255)
    text = models.TextField()
    source_label = models.CharField(max_length=255)
    confidence = models.CharField(max_length=80, default="candidate")
    ai_suggested = models.BooleanField(default=False)
    selected_by_default = models.BooleanField(default=True)

    class Meta:
        unique_together = [("matter", "slug")]
        ordering = ["id"]

    def __str__(self):
        return self.title


class TriageRubric(models.Model):
    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    standard = models.TextField()
    criteria = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TriageAssessment(models.Model):
    matter = models.ForeignKey(Matter, related_name="triage_assessments", on_delete=models.CASCADE)
    rubric = models.ForeignKey(TriageRubric, related_name="assessments", on_delete=models.PROTECT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="triage_assessments",
    )
    case_type = models.CharField(max_length=255, blank=True)
    priority = models.BooleanField(default=False)
    priority_label = models.CharField(max_length=120, blank=True)
    confidence = models.CharField(max_length=80, default="needs_review")
    summary = models.TextField(blank=True)
    reasoning = models.TextField(blank=True)
    matched_criteria = models.JSONField(default=list, blank=True)
    missing_information = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    llm_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.matter} - {self.rubric}"


class LegalServerDelivery(models.Model):
    """One attempt to write work product back to LegalServer.

    Every attempt is recorded, including the ones that were skipped or failed,
    so an advocate can tell whether the letter they exported reached the case
    file. A silent failure here looks exactly like a successful save.
    """

    CASENOTE = "casenote"
    DOCUMENT = "document"
    CASE_UPDATE = "case_update"
    KIND_CHOICES = [
        (CASENOTE, "Case note"),
        (DOCUMENT, "Document"),
        (CASE_UPDATE, "Case property update"),
    ]

    SAVED = "saved"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    STATUS_CHOICES = [
        (SAVED, "Saved to LegalServer"),
        (SKIPPED, "Not sent"),
        (FAILED, "Failed"),
        (DRY_RUN, "Previewed only"),
    ]

    matter = models.ForeignKey(Matter, related_name="legalserver_deliveries", on_delete=models.CASCADE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="legalserver_deliveries",
    )
    kind = models.CharField(max_length=40, choices=KIND_CHOICES)
    # Which workflow asked for the save: draft_export, advice_letter, triage,
    # research, case_chat, or manual.
    origin = models.CharField(max_length=60, blank=True)
    # Identifies the logical artifact this delivery represents, e.g. one advice
    # letter draft or one chat thread. A repeat save with the same scope key
    # updates the record already on the case instead of adding a copy.
    scope_key = models.CharField(max_length=255, blank=True)
    updated_existing = models.BooleanField(default=False)
    status = models.CharField(max_length=40, choices=STATUS_CHOICES)
    reason = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=500, blank=True)
    filename = models.CharField(max_length=500, blank=True)
    remote_id = models.CharField(max_length=255, blank=True)
    remote_url = models.URLField(blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "LegalServer deliveries"

    def __str__(self):
        return f"{self.matter} - {self.get_kind_display()} ({self.status})"

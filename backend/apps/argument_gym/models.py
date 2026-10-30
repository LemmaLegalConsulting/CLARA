"""Durable state for the Argument Gym: what was tested, against what, and how it held up.

A gym run is an adversarial read of a brief, not an edit of it. The brief and
the case file are *referenced*; nothing is copied into gym storage because a run
looked at it. What the gym owns is its own output: the challenges an opponent
raised, what a judge made of them, and what the advocate decided to do about
each one. That disposition has to survive a rerun, which is why this is not
`DraftDocument.validation_flags` or a chat thread.
"""

from django.conf import settings
from django.db import models


class GymWorkspace(models.Model):
    """One brief under test, together with the case context it is tested against."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="gym_workspaces",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    matter = models.ForeignKey(
        "matters.Matter",
        related_name="gym_workspaces",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Set when the brief is tested against a real case file. Access is resolved through this.",
    )
    AUTO = "auto"
    MANUAL = "manual"
    OFF = "off"
    RESOLUTION_CHOICES = [(AUTO, "Detect automatically"), (MANUAL, "Set by hand")]
    RULE_MODE_CHOICES = [(AUTO, "Apply the detected court's rules"), (MANUAL, "Apply a chosen court's rules"), (OFF, "Skip filing-format rules")]

    jurisdiction = models.CharField(max_length=255, blank=True)
    jurisdiction_mode = models.CharField(max_length=20, choices=RESOLUTION_CHOICES, default=AUTO)
    jurisdiction_detail = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Jurisdiction set by hand: {'state', 'county', 'municipality', 'division', 'courtType'}. "
            "Municipality is left out for an appellate division, where it means nothing."
        ),
    )
    court = models.ForeignKey(
        "rules.CourtProfile",
        related_name="gym_workspaces",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Whose filing-format rules apply. Detected or chosen, according to court_rule_mode.",
    )
    court_rule_mode = models.CharField(max_length=20, choices=RULE_MODE_CHOICES, default=AUTO)
    enabled_checks = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Ids of the checks this session runs. An empty list means the catalog's defaults; "
            "the author's explicit choice is stored here and is never widened silently."
        ),
    )
    check_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-check options, such as passive-voice phrases this court expects to read.",
    )
    checklist = models.ForeignKey(
        "argument_gym.GymChecklist",
        related_name="workspaces",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The author's own checklist, applied when the custom_checklist check is on.",
    )
    title = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return self.title or f"Gym workspace {self.pk}"


class GymChecklist(models.Model):
    """An advocate's own review checklist, applied by the gym.

    Items are prose, not rules: "check that every date in the statement of facts
    appears in a document in the file" is a legitimate item, and answering it
    means going and reading the file. The model applying an item can make bounded
    read-only lookups to do that.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="gym_checklists",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    items = models.JSONField(
        default=list,
        blank=True,
        help_text="[{'id', 'text'}] -- one review question per item, in the author's own words.",
    )
    shared = models.BooleanField(
        default=False,
        help_text="Shared checklists are offered to everyone in this deployment, not only their author.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]

    def __str__(self):
        return self.title


class GymDocument(models.Model):
    """A document a run may read: the brief itself, or a piece of the case record.

    ``source_type`` says where the text came from and therefore what may be
    trusted about it. A ``matter_document`` keeps only the reference; its text is
    fetched through the case-file connector at run time, under the same access
    control as everywhere else in the app.
    """

    BRIEF_UNDER_TEST = "brief_under_test"
    CASE_RECORD = "case_record"
    ROLE_CHOICES = [
        (BRIEF_UNDER_TEST, "Brief under test"),
        (CASE_RECORD, "Case record"),
    ]

    UPLOAD = "upload"
    DRAFT_DOCUMENT = "draft_document"
    MATTER_DOCUMENT = "matter_document"
    SOURCE_TYPE_CHOICES = [
        (UPLOAD, "Uploaded file"),
        (DRAFT_DOCUMENT, "Draft from this tool"),
        (MATTER_DOCUMENT, "Case file document"),
    ]

    workspace = models.ForeignKey(GymWorkspace, related_name="documents", on_delete=models.CASCADE)
    role = models.CharField(max_length=40, choices=ROLE_CHOICES)
    source_type = models.CharField(max_length=40, choices=SOURCE_TYPE_CHOICES)
    draft_document = models.ForeignKey(
        "drafting.DraftDocument",
        related_name="gym_documents",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    external_reference = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Stable pointer to a document that lives in another system: "
            "{'system', 'matterExternalId', 'documentId', 'title', 'url'}. "
            "The bytes stay there; only the reference is stored here."
        ),
    )
    storage_key = models.CharField(
        max_length=500,
        blank=True,
        help_text="Key in apps.core.storage for an uploaded file, when one was retained.",
    )
    title = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=500, blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    extracted_text = models.TextField(blank=True)
    extraction_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extractor name, page count, and the structural units found in the text.",
    )
    excluded = models.BooleanField(
        default=False,
        help_text="Excluded case materials stay listed but are not read by the next run.",
    )
    pleading_type = models.CharField(
        max_length=60,
        blank=True,
        help_text="What kind of paper this is, which decides which of a court's rules apply to it.",
    )
    page_range = models.JSONField(
        default=dict,
        blank=True,
        help_text="{'start', 'end'} when this document is one span of pages split out of a larger upload.",
    )
    split_from = models.ForeignKey(
        "self",
        related_name="split_parts",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text="The upload this was separated out of, for an exhibit detached from a filed brief.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "id"]

    def __str__(self):
        return f"{self.get_role_display()}: {self.title}"

    @property
    def structure_units(self):
        units = (self.extraction_metadata or {}).get("units")
        return units if isinstance(units, list) else []


class GymRun(models.Model):
    """One pass of the adversarial pipeline over one version of one brief."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (COMPLETE, "Complete"),
        (FAILED, "Failed"),
    ]

    workspace = models.ForeignKey(GymWorkspace, related_name="runs", on_delete=models.CASCADE)
    brief = models.ForeignKey(GymDocument, related_name="runs", on_delete=models.CASCADE)
    previous_run = models.ForeignKey(
        "self",
        related_name="reruns",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "What the brief was when this run read it: component version ids for a native "
            "draft, a text checksum otherwise. A rerun diffs against this."
        ),
    )
    configuration = models.JSONField(default=dict, blank=True)
    research_trace = models.JSONField(
        default=list,
        blank=True,
        help_text="Adversarial queries run, the sources searched, and the augmentation rounds each took.",
    )
    materials = models.JSONField(
        default=list,
        blank=True,
        help_text="Every document this run actually read, and why it was chosen.",
    )
    stage_trace = models.JSONField(default=list, blank=True)
    comparison = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "How this run relates to the one before it: which challenges recurred, which "
            "are new, and which stopped being raised once the brief changed."
        ),
    )
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=PENDING)
    summary = models.TextField(
        blank=True,
        help_text="Narrative orientation written once from the stored challenges and reused by every artifact.",
    )
    assessment = models.TextField(
        blank=True,
        help_text="One paragraph at the top of the report: how persuasive the brief is and what most needs fixing.",
    )
    assessment_verdict = models.CharField(
        max_length=60,
        blank=True,
        help_text="The headline judgment the paragraph expands on. A characterization, never a score.",
    )
    court = models.ForeignKey(
        "rules.CourtProfile",
        related_name="gym_runs",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    court_detection = models.JSONField(
        default=dict,
        blank=True,
        help_text="How the court was arrived at: detected and on what evidence, or set by hand.",
    )
    compliance = models.JSONField(
        default=dict,
        blank=True,
        help_text="Deterministic filing-format findings, and which properties could not be measured.",
    )
    checks_run = models.JSONField(
        default=list,
        blank=True,
        help_text="Which checks this run actually ran, and which the author turned off or that could not apply.",
    )
    check_results = models.JSONField(
        default=dict,
        blank=True,
        help_text="Findings by check id, so a report can say which check produced what.",
    )
    rule_audit = models.JSONField(
        default=list,
        blank=True,
        help_text="The rules the brief invoked and whether each of their elements is pleaded and supported.",
    )
    checklist_results = models.JSONField(
        default=dict,
        blank=True,
        help_text="The author's own checklist applied to this brief, with the lookups each item made.",
    )
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="gym_runs",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Gym run {self.pk} ({self.status})"


class GymChallenge(models.Model):
    """One opposition argument, judged and answered, against one passage of the brief."""

    LEGAL_AUTHORITY = "legal_authority"
    FACTUAL_SUPPORT = "factual_support"
    RECORD_CONFLICT = "record_conflict"
    PROCEDURAL = "procedural"
    REMEDY_SCOPE = "remedy_scope"
    FRAMING = "framing"
    MISSING_ELEMENT = "missing_element"
    CATEGORY_CHOICES = [
        (LEGAL_AUTHORITY, "Legal authority"),
        (FACTUAL_SUPPORT, "Factual support"),
        (RECORD_CONFLICT, "Record conflict"),
        (PROCEDURAL, "Procedural"),
        (REMEDY_SCOPE, "Scope of relief"),
        (FRAMING, "Framing"),
        (MISSING_ELEMENT, "Missing element"),
    ]

    OPEN = "open"
    ADDRESSED = "addressed"
    DISMISSED = "dismissed"
    DISPOSITION_CHOICES = [
        (OPEN, "Open"),
        (ADDRESSED, "Addressed"),
        (DISMISSED, "Dismissed"),
    ]

    SEVERITY_CHOICES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
    CONFIDENCE_CHOICES = [("high", "High"), ("medium", "Medium"), ("low", "Low")]

    run = models.ForeignKey(GymRun, related_name="challenges", on_delete=models.CASCADE)
    ordinal = models.PositiveIntegerField(default=0)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES, default=LEGAL_AUTHORITY)
    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        help_text="Stable hash of target and argument, so a rerun can tell a repeat from a new challenge.",
    )
    carried_from = models.ForeignKey(
        "self",
        related_name="recurrences",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    target = models.JSONField(
        default=dict,
        blank=True,
        help_text="Where in the brief this lands: {'unitId', 'section', 'paragraph', 'page', 'excerpt', 'blockKey'}.",
    )
    finding = models.CharField(max_length=160, blank=True, help_text="Plain-language statement of the specific problem.")
    review_action = models.CharField(max_length=20, blank=True, choices=[("revise", "Revise"), ("verify", "Verify first"), ("strategy", "Strategy choice")])
    opponent_argument = models.TextField()
    why_it_matters = models.TextField(blank=True)
    brief_currently_says = models.TextField(blank=True)
    legal_sources = models.JSONField(default=list, blank=True)
    record_sources = models.JSONField(default=list, blank=True)
    judge_assessment = models.TextField(blank=True)
    judge_verdict = models.CharField(max_length=60, blank=True)
    coaching_recommendation = models.TextField(blank=True)
    suggested_response = models.TextField(blank=True)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default="medium")
    importance = models.PositiveSmallIntegerField(default=50)
    confidence = models.CharField(max_length=20, choices=CONFIDENCE_CHOICES, default="medium")
    research_coverage = models.JSONField(default=dict, blank=True)
    disposition = models.CharField(max_length=30, choices=DISPOSITION_CHOICES, default=OPEN)
    disposition_note = models.TextField(blank=True)
    resulting_operation = models.ForeignKey(
        "drafting.DraftOperation",
        related_name="gym_challenges",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ordinal", "id"]

    def __str__(self):
        return f"{self.get_category_display()}: {self.opponent_argument[:60]}"

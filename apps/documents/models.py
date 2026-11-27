import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from apps.extraction.invoice import EXTRACTORS


def upload_path(instance: "Document", filename: str) -> str:
    ext = Path(filename).suffix.lower()[:10]
    return f"org-{instance.organization_id}/documents/{instance.uid}/original{ext}"


class DocumentType(models.Model):
    """A kind of document a client processes (e.g. purchase invoices) and how it is handled."""

    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="document_types")
    slug = models.SlugField()
    name = models.CharField(max_length=100)
    extractor = models.CharField(max_length=50, choices=[(k, v.name) for k, v in EXTRACTORS.items()],
                                 default="invoice")
    confidence_threshold = models.FloatField(
        default=0.7, help_text="Required fields below this confidence block auto-approval")
    default_decision = models.CharField(
        max_length=10, default="review", choices=[("approve", "Approve"), ("review", "Review")],
        help_text="Decision when no rule matches")
    auto_approve_enabled = models.BooleanField(
        default=True, help_text="Turn off to send everything to a person, whatever the rules say")

    class Meta:
        unique_together = [("organization", "slug")]

    def __str__(self):
        return self.name


class Document(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PROCESSING = "processing", "Processing"
        NEEDS_REVIEW = "needs_review", "Needs review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    class DecidedBy(models.TextChoices):
        RULES = "rules", "Rules"
        PERSON = "person", "Person"

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="documents")
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="documents")
    file = models.FileField(upload_to=upload_path)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED, db_index=True)
    decided_by = models.CharField(max_length=10, choices=DecidedBy.choices, blank=True)
    queue = models.CharField(max_length=100, blank=True, help_text="Review queue chosen by the matching rule")
    reasons = models.JSONField(default=list, blank=True)

    ocr_engine = models.CharField(max_length=30, blank=True)
    ocr_seconds = models.FloatField(null=True, blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)
    ocr = models.JSONField(null=True, blank=True, help_text="Pages, lines, confidences and boxes")
    facts = models.JSONField(default=dict, blank=True, help_text="Snapshot of facts the rules saw")
    rule_results = models.JSONField(default=list, blank=True, help_text="Per-rule-set evaluation trace")
    error = models.TextField(blank=True)

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                    related_name="+")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self):
        return f"{self.original_name} ({self.status})"

    @property
    def pages_dir(self) -> Path:
        return Path(settings.MEDIA_ROOT) / f"org-{self.organization_id}" / "documents" / str(self.uid) / "pages"

    def field_values(self) -> dict:
        return {f.key: f.final_value for f in self.fields.all()}


class ExtractedField(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="fields")
    key = models.CharField(max_length=60)
    value = models.JSONField(null=True, blank=True)
    raw_text = models.TextField(blank=True)
    confidence = models.FloatField(default=0)
    page = models.PositiveIntegerField(null=True, blank=True)
    bbox = models.JSONField(null=True, blank=True)
    method = models.CharField(max_length=20, blank=True)
    corrected_value = models.JSONField(null=True, blank=True)
    corrected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    corrected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("document", "key")]
        ordering = ["id"]

    @property
    def is_corrected(self) -> bool:
        return self.corrected_at is not None

    @property
    def final_value(self):
        return self.corrected_value if self.is_corrected else self.value

    @property
    def final_confidence(self) -> float:
        # a person typed or confirmed it: trust it fully
        return 1.0 if self.is_corrected else self.confidence


class AuditEvent(models.Model):
    """Append-only log of everything that happened to a document or a rule."""

    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="audit_events")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    action = models.CharField(max_length=50)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M}"


def log_event(org, action: str, document=None, actor=None, **data) -> AuditEvent:
    return AuditEvent.objects.create(organization=org, document=document, actor=actor, action=action, data=data)

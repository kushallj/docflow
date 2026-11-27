from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from apps.core.models import Vendor
from apps.extraction.invoice import get_extractor
from apps.extraction.normalize import gstin_is_valid
from apps.ocr.service import IMAGE_EXTS

from .models import AuditEvent, Document, DocumentType, ExtractedField

ALLOWED_EXTS = IMAGE_EXTS | {".pdf"}


class DocumentTypeSerializer(serializers.ModelSerializer):
    fields_schema = serializers.SerializerMethodField()

    class Meta:
        model = DocumentType
        fields = ["id", "slug", "name", "extractor", "confidence_threshold", "default_decision",
                  "auto_approve_enabled", "fields_schema"]

    def get_fields_schema(self, obj):
        return get_extractor(obj.extractor).field_schema()


class ExtractedFieldSerializer(serializers.ModelSerializer):
    final_value = serializers.JSONField(read_only=True)
    is_corrected = serializers.BooleanField(read_only=True)

    class Meta:
        model = ExtractedField
        fields = ["key", "value", "final_value", "confidence", "raw_text", "page", "bbox", "method",
                  "is_corrected", "corrected_value", "corrected_at"]


class AuditEventSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.username", default=None)

    class Meta:
        model = AuditEvent
        fields = ["action", "actor", "data", "created_at"]


class DocumentListSerializer(serializers.ModelSerializer):
    document_type = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    summary = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ["id", "uid", "original_name", "document_type", "status", "decided_by", "queue", "reasons",
                  "summary", "created_at", "processed_at"]

    def get_summary(self, obj):
        f = obj.facts or {}
        return {k: f.get(k) for k in ("vendor_name", "invoice_number", "total_amount", "currency")}


class DocumentDetailSerializer(DocumentListSerializer):
    fields_ = ExtractedFieldSerializer(source="fields", many=True, read_only=True)
    events = AuditEventSerializer(many=True, read_only=True)
    file_url = serializers.SerializerMethodField()
    pages = serializers.SerializerMethodField()

    class Meta(DocumentListSerializer.Meta):
        fields = DocumentListSerializer.Meta.fields + [
            "file_url", "sha256", "ocr_engine", "ocr_seconds", "ocr_confidence", "error", "pages",
            "fields_", "facts", "rule_results", "events", "reviewed_at"]

    def get_file_url(self, obj):
        url = reverse("protected-file", args=[obj.file.name])
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def get_pages(self, obj):
        pages = (obj.ocr or {}).get("pages", [])
        return [{"number": p["number"], "width": p["width"], "height": p["height"],
                 "image_url": reverse("protected-file", args=[p["image_path"]]) if p.get("image_path") else None,
                 "line_count": len(p["lines"])} for p in pages]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["fields"] = data.pop("fields_")
        return data


class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    document_type = serializers.SlugField()

    def validate_file(self, f):
        ext = Path(f.name).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise serializers.ValidationError(f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTS))}")
        if f.size > settings.MAX_UPLOAD_BYTES:
            raise serializers.ValidationError(f"File is larger than {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        return f


def normalise_field_value(field_type: str, value):
    """Validate a reviewer's correction against the field type and return the stored form."""
    if value in (None, ""):
        return None
    if field_type == "number":
        try:
            return str(Decimal(str(value).replace(",", "")))
        except InvalidOperation as exc:
            raise serializers.ValidationError("Enter a number") from exc
    if field_type == "date":
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise serializers.ValidationError("Enter a date as YYYY-MM-DD") from exc
    if field_type == "bool":
        return str(value).lower() in ("true", "1", "yes")
    return str(value).strip()[:500]


class FieldCorrectionSerializer(serializers.Serializer):
    value = serializers.JSONField(allow_null=True)


class ReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    note = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class VendorSerializer(serializers.ModelSerializer):
    gstin_valid = serializers.BooleanField(read_only=True)

    class Meta:
        model = Vendor
        fields = ["id", "name", "gstin", "approved", "notes", "gstin_valid"]

    def validate_gstin(self, v):
        v = (v or "").strip().upper()
        if v and not gstin_is_valid(v):
            raise serializers.ValidationError("Not a valid GSTIN (shape or checksum)")
        return v

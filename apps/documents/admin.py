from django.contrib import admin

from .models import AuditEvent, Document, DocumentType, ExtractedField


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "organization", "extractor", "confidence_threshold", "auto_approve_enabled"]


class FieldInline(admin.TabularInline):
    model = ExtractedField
    extra = 0
    fields = ["key", "value", "confidence", "method", "corrected_value", "corrected_by"]
    readonly_fields = fields


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ["original_name", "organization", "status", "decided_by", "queue", "created_at"]
    list_filter = ["organization", "status", "decided_by"]
    search_fields = ["original_name", "sha256"]
    inlines = [FieldInline]
    readonly_fields = ["uid", "sha256", "ocr", "facts", "rule_results", "reasons"]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "organization", "action", "actor", "document"]
    list_filter = ["organization", "action"]

    def has_change_permission(self, request, obj=None):
        return False   # append-only

    def has_delete_permission(self, request, obj=None):
        return False

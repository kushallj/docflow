from django.contrib import admin

from .models import RuleSet


@admin.register(RuleSet)
class RuleSetAdmin(admin.ModelAdmin):
    list_display = ["name", "document_type", "action", "route_to", "priority", "is_active", "version"]
    list_filter = ["organization", "document_type", "action", "is_active"]
    readonly_fields = ["version", "created_at", "updated_at"]

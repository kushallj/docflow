from django.contrib import admin

from .models import Membership, Organization, Vendor


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "gstin"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "role"]
    list_filter = ["organization", "role"]


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ["name", "gstin", "approved", "organization"]
    list_filter = ["organization", "approved"]
    search_fields = ["name", "gstin"]

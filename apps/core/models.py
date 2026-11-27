"""Tenancy: every document, rule and vendor belongs to one organisation (client)."""
from django.conf import settings
from django.db import models

from apps.extraction.normalize import gstin_is_valid


class Organization(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    gstin = models.CharField(max_length=15, blank=True, help_text="Used to check invoices are billed to us")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"          # manages rules, vendors and users
        REVIEWER = "reviewer", "Reviewer"  # corrects fields, approves / rejects
        VIEWER = "viewer", "Viewer"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="membership")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.REVIEWER)

    def __str__(self):
        return f"{self.user} @ {self.organization} ({self.role})"


class Vendor(models.Model):
    """Vendor master: lets rules ask "is this an approved vendor?"."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendors")
    name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=15, blank=True)
    approved = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "gstin"], condition=~models.Q(gstin=""),
                                    name="unique_vendor_gstin_per_org"),
        ]
        ordering = ["name"]

    @property
    def gstin_valid(self) -> bool:
        return gstin_is_valid(self.gstin)

    def __str__(self):
        return self.name

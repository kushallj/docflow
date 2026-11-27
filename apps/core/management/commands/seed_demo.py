"""Create a demo client with users, a vendor master and a starter rule set.

    python manage.py seed_demo            # idempotent
    python manage.py seed_demo --password s3cret
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from apps.core.models import Membership, Organization, Vendor
from apps.documents.models import DocumentType
from apps.rules.models import RuleSet
from apps.extraction.normalize import gstin_check_char


def gstin(state: str, pan: str) -> str:
    first14 = f"{state}{pan}1Z"
    return first14 + gstin_check_char(first14)

STARTER_RULES = [
    {
        "name": "Reject: vendor GSTIN fails checksum",
        "priority": 10, "action": "reject",
        "reason": "Vendor GSTIN is not a valid GST number (possible fake or mistyped invoice)",
        "tree": {"type": "all", "rules": [
            {"fact": "vendor_gstin", "operator": "exists"},
            {"fact": "vendor_gstin_valid", "operator": "equalTo", "value": False},
        ]},
    },
    {
        "name": "Reject: duplicate invoice",
        "priority": 20, "action": "reject",
        "reason": "Same vendor and invoice number were already submitted",
        "tree": {"type": "all", "rules": [{"fact": "is_duplicate", "operator": "equalTo", "value": True}]},
    },
    {
        "name": "Review: high value or unknown vendor",
        "priority": 30, "action": "review", "route_to": "finance-manager",
        "reason": "Above ₹2,00,000, or the vendor is not approved in the vendor master",
        "tree": {"type": "any", "rules": [
            {"fact": "total_amount", "operator": "greaterThan", "value": 200000},
            {"fact": "vendor_approved", "operator": "equalTo", "value": False},
        ]},
    },
    {
        "name": "Review: numbers or billing do not add up",
        "priority": 40, "action": "review", "route_to": "ap-clerk",
        "reason": "Subtotal + tax does not equal the total, the invoice is not billed to us, or it is overdue",
        "tree": {"type": "any", "rules": [
            {"fact": "totals_consistent", "operator": "equalTo", "value": False},
            {"fact": "billed_to_us", "operator": "equalTo", "value": False},
            {"fact": "days_until_due", "operator": "lessThan", "value": 0},
        ]},
    },
    {
        "name": "Auto-approve: clean invoice from approved vendor",
        "priority": 90, "action": "approve",
        "reason": "Approved vendor, valid GSTIN, totals add up, within limit",
        "tree": {"type": "all", "rules": [
            {"fact": "vendor_approved", "operator": "equalTo", "value": True},
            {"fact": "vendor_gstin_valid", "operator": "equalTo", "value": True},
            {"fact": "totals_consistent", "operator": "equalTo", "value": True},
            {"fact": "total_amount", "operator": "lessThanOrEqual", "value": 200000},
            {"type": "any", "rules": [
                {"fact": "tax_rate_percent", "operator": "in", "value": [0, 5, 12, 18, 28]},
                {"fact": "currency", "operator": "notEqualTo", "value": "INR"},
            ]},
        ]},
    },
]


class Command(BaseCommand):
    help = "Seed a demo organisation, users, vendors and rules"

    def add_arguments(self, parser):
        parser.add_argument("--password", default="docflow-demo")

    @transaction.atomic
    def handle(self, *args, password, **opts):
        org, _ = Organization.objects.update_or_create(
            slug="acme-retail", defaults={"name": "Acme Retail Pvt Ltd", "gstin": gstin("09", "AAACA1234F")})
        User = get_user_model()
        for username, role, staff in [("admin", "admin", True), ("reviewer", "reviewer", False)]:
            user, created = User.objects.get_or_create(username=username, defaults={"is_staff": staff,
                                                                                     "is_superuser": staff})
            if created:
                user.set_password(password)
                user.save()
            Membership.objects.update_or_create(user=user, defaults={"organization": org, "role": role})
            token, _ = Token.objects.get_or_create(user=user)
            self.stdout.write(f"user {username!r} ({role}) API token: {token.key}")

        dtype, _ = DocumentType.objects.update_or_create(
            organization=org, slug="invoice",
            defaults={"name": "Purchase invoice", "extractor": "invoice", "confidence_threshold": 0.7})

        vendors = [
            ("Sharma Stationers", gstin("07", "ABCPS4821K"), True),
            ("Northwind IT Solutions", gstin("06", "AAFCN7342Q"), True),
            ("Quickfix Facility Services", "", True),
        ]
        for name, g, approved in vendors:
            Vendor.objects.update_or_create(organization=org, name=name, defaults={"gstin": g, "approved": approved})

        for spec in STARTER_RULES:
            RuleSet.objects.update_or_create(
                organization=org, document_type=dtype, name=spec["name"],
                defaults={k: v for k, v in spec.items() if k != "name"})
        self.stdout.write(self.style.SUCCESS(
            f"Seeded '{org.name}': {len(vendors)} vendors, {len(STARTER_RULES)} rule sets. "
            f"Sign in as admin / reviewer with password '{password}'."))

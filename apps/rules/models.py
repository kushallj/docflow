from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Action(models.TextChoices):
    APPROVE = "approve", "Auto-approve"
    REVIEW = "review", "Send to review"
    REJECT = "reject", "Reject"


# When several rule sets match, the most severe action wins.
SEVERITY = {Action.APPROVE: 1, Action.REVIEW: 2, Action.REJECT: 3}


class RuleSet(models.Model):
    """One named rule: a nested condition tree plus the action to take when it matches."""

    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE, related_name="rulesets")
    document_type = models.ForeignKey("documents.DocumentType", on_delete=models.CASCADE, related_name="rulesets")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.PositiveIntegerField(default=100, help_text="Lower runs first")
    tree = models.JSONField(help_text="Nested rule tree, same shape as the Flutter rule builder")
    action = models.CharField(max_length=10, choices=Action.choices)
    route_to = models.CharField(max_length=100, blank=True, help_text="Review queue, e.g. 'finance-manager'")
    reason = models.CharField(max_length=300, blank=True, help_text="Shown to reviewers when this rule matches")
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.name} → {self.action}"

    def clean(self):
        from .services import catalog_for

        if self.document_type_id and self.organization_id and self.document_type.organization_id != self.organization_id:
            raise ValidationError("Document type belongs to another organisation")
        from .engine import validate

        errors = validate(self.tree, catalog_for(self.document_type))
        if errors:
            raise ValidationError({"tree": errors})

    def save(self, *args, **kwargs):
        if self.pk:
            old = RuleSet.objects.filter(pk=self.pk).values("tree", "action", "route_to").first()
            if old and (old["tree"] != self.tree or old["action"] != self.action or old["route_to"] != self.route_to):
                self.version += 1
        super().save(*args, **kwargs)

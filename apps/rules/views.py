from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import OrgScopedMixin
from apps.documents.models import AuditEvent, Document, DocumentType, log_event

from .engine import RuleError, evaluate, validate
from .models import RuleSet
from .services import build_facts, catalog_for


class RuleSetSerializer(serializers.ModelSerializer):
    document_type = serializers.SlugRelatedField(slug_field="slug", queryset=DocumentType.objects.none())

    class Meta:
        model = RuleSet
        fields = ["id", "name", "description", "document_type", "priority", "tree", "action", "route_to",
                  "reason", "is_active", "version", "updated_at"]
        read_only_fields = ["version", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        org = self.context.get("org")
        if org is not None:
            self.fields["document_type"].queryset = DocumentType.objects.filter(organization=org)

    def validate(self, attrs):
        dtype = attrs.get("document_type") or getattr(self.instance, "document_type", None)
        tree = attrs.get("tree", getattr(self.instance, "tree", None))
        errors = validate(tree, catalog_for(dtype))
        if errors:
            raise serializers.ValidationError({"tree": errors})
        return attrs


class RuleTestSerializer(serializers.Serializer):
    tree = serializers.JSONField(required=False)
    document_type = serializers.SlugField(required=False)
    document_id = serializers.IntegerField(required=False)
    facts = serializers.DictField(required=False)


class RuleSetViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    queryset = RuleSet.objects.select_related("document_type")
    serializer_class = RuleSetSerializer
    filterset_fields = ["document_type__slug", "action", "is_active"]

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "org": self.org}

    def perform_create(self, serializer):
        rs = serializer.save(organization=self.org, created_by=self.request.user)
        log_event(self.org, "ruleset.created", actor=self.request.user, ruleset=rs.id, name=rs.name, tree=rs.tree,
                  rule_action=rs.action)

    def perform_update(self, serializer):
        rs = serializer.save()
        log_event(self.org, "ruleset.updated", actor=self.request.user, ruleset=rs.id, name=rs.name,
                  version=rs.version, tree=rs.tree, rule_action=rs.action)

    def perform_destroy(self, instance):
        log_event(self.org, "ruleset.deleted", actor=self.request.user, ruleset=instance.id, name=instance.name)
        instance.delete()

    @action(detail=False, methods=["post"])
    def test(self, request):
        """Dry-run a rule tree without saving it.

        Body: {"tree": {...}, "document_type": "invoice", and either "document_id": 12 or "facts": {...}}
        Returns whether it matches and the full condition-by-condition trace.
        """
        s = RuleTestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        doc = None
        if data.get("document_id"):
            doc = Document.objects.filter(organization=self.org, pk=data["document_id"]).first()
            if doc is None:
                raise serializers.ValidationError({"document_id": "Not found"})
            dtype = doc.document_type
        else:
            dtype = DocumentType.objects.filter(organization=self.org, slug=data.get("document_type")).first()
            if dtype is None:
                raise serializers.ValidationError({"document_type": "Required (or pass document_id)"})
        catalog = catalog_for(dtype)
        tree = data.get("tree")
        if tree is None:
            raise serializers.ValidationError({"tree": "Required"})
        errors = validate(tree, catalog)
        if errors:
            return Response({"valid": False, "errors": errors}, status=400)
        facts = build_facts(doc) if doc else data.get("facts", {})
        try:
            trace = evaluate(tree, facts, catalog)
        except RuleError as exc:
            return Response({"valid": False, "errors": [str(exc)]}, status=400)
        return Response({"valid": True, "matched": trace.passed, "trace": trace.as_dict(), "facts": facts})

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        rs = self.get_object()
        events = AuditEvent.objects.filter(organization=self.org, action__startswith="ruleset.",
                                           data__ruleset=rs.id).select_related("actor")
        return Response([{"action": e.action, "actor": getattr(e.actor, "username", None), "data": e.data,
                          "created_at": e.created_at} for e in events])

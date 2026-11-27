from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.models import Membership, Vendor
from apps.core.permissions import OrgScopedMixin
from apps.rules.services import catalog_for

from . import services
from .models import Document, DocumentType
from .pipeline import evaluate_document
from .serializers import (DocumentDetailSerializer, DocumentListSerializer, DocumentTypeSerializer,
                          FieldCorrectionSerializer, ReviewSerializer, UploadSerializer, VendorSerializer)

REVIEWERS = (Membership.Role.ADMIN, Membership.Role.REVIEWER)


class DocumentTypeViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    queryset = DocumentType.objects.all()
    serializer_class = DocumentTypeSerializer
    lookup_field = "slug"

    @action(detail=True, methods=["get"])
    def facts(self, request, slug=None):
        """Fact catalog with the operators allowed for each fact type (for the Flutter rule builder)."""
        return Response(catalog_for(self.get_object()).as_list())


class DocumentViewSet(OrgScopedMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                      mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = Document.objects.select_related("document_type").prefetch_related("fields", "events__actor")
    filterset_fields = ["status", "document_type__slug", "queue", "decided_by"]
    ordering_fields = ["created_at", "processed_at"]
    write_roles = REVIEWERS

    def get_serializer_class(self):
        if self.action == "create":
            return UploadSerializer
        return DocumentListSerializer if self.action == "list" else DocumentDetailSerializer

    def create(self, request, *args, **kwargs):
        s = UploadSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        dtype = DocumentType.objects.filter(organization=self.org, slug=s.validated_data["document_type"]).first()
        if dtype is None:
            raise ValidationError({"document_type": "Unknown document type"})
        doc = services.upload_document(self.org, dtype, s.validated_data["file"], request.user)
        doc = self.get_queryset().get(pk=doc.pk)
        return Response(DocumentDetailSerializer(doc, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def reprocess(self, request, pk=None):
        """Run OCR and extraction again (keeps reviewer corrections)."""
        doc = self.get_object()
        if doc.decided_by == Document.DecidedBy.PERSON:
            raise ValidationError("A person already decided this document; reopen it first")
        services.enqueue(doc)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def reevaluate(self, request, pk=None):
        """Re-run only the rules, e.g. after an admin changed a rule set."""
        doc = evaluate_document(self.get_object(), actor=request.user)
        return Response(DocumentDetailSerializer(doc, context={"request": request}).data)

    @action(detail=True, methods=["patch"], url_path=r"fields/(?P<key>[a-z_]+)")
    def correct_field(self, request, pk=None, key=None):
        doc = self.get_object()
        s = FieldCorrectionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            services.correct_field(doc, key, s.validated_data["value"], request.user)
        except KeyError:
            raise ValidationError({"key": f"Unknown field '{key}'"})
        doc = self.get_queryset().get(pk=doc.pk)
        return Response(DocumentDetailSerializer(doc, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        doc = self.get_object()
        if doc.status in (Document.Status.PROCESSING, Document.Status.UPLOADED, Document.Status.FAILED):
            raise ValidationError(f"Cannot review a document that is {doc.get_status_display().lower()}")
        s = ReviewSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        services.review(doc, s.validated_data["decision"], request.user, s.validated_data.get("note", ""))
        doc = self.get_queryset().get(pk=doc.pk)
        return Response(DocumentDetailSerializer(doc, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        doc = services.reopen(self.get_object(), request.user)
        return Response(DocumentDetailSerializer(doc, context={"request": request}).data)


class VendorViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer
    filterset_fields = ["approved"]

    def perform_create(self, serializer):
        self._unique_gstin(serializer)
        serializer.save(organization=self.org)

    def perform_update(self, serializer):
        self._unique_gstin(serializer)
        serializer.save()

    def _unique_gstin(self, serializer):
        g = serializer.validated_data.get("gstin")
        if g and Vendor.objects.filter(organization=self.org, gstin=g).exclude(
                pk=getattr(serializer.instance, "pk", None)).exists():
            raise ValidationError({"gstin": "A vendor with this GSTIN already exists"})


def get_document_type(org, slug):
    return get_object_or_404(DocumentType, organization=org, slug=slug)

"""Actions shared by the REST API and the web dashboard."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.extraction.invoice import get_extractor

from .models import Document, ExtractedField, log_event
from .pipeline import evaluate_document, sha256_of
from .serializers import normalise_field_value


def upload_document(org, document_type, fileobj, user) -> Document:
    doc = Document(organization=org, document_type=document_type, original_name=fileobj.name[:255],
                   sha256=sha256_of(fileobj), uploaded_by=user)
    doc.file.save(fileobj.name, fileobj, save=False)
    doc.save()
    log_event(org, "document.uploaded", doc, actor=user, name=doc.original_name, sha256=doc.sha256)
    enqueue(doc)
    doc.refresh_from_db()
    return doc


def enqueue(doc: Document) -> None:
    from .tasks import process_document_task

    # run after the upload transaction commits, so the worker can see the row
    transaction.on_commit(lambda: process_document_task.delay(doc.pk))


def correct_field(doc: Document, key: str, value, user) -> ExtractedField:
    schema = {s["key"]: s for s in get_extractor(doc.document_type.extractor).field_schema()}
    if key not in schema:
        raise KeyError(key)
    normalised = normalise_field_value(schema[key]["type"], value)
    field, _ = ExtractedField.objects.get_or_create(document=doc, key=key)
    before = field.final_value
    field.corrected_value, field.corrected_by, field.corrected_at = normalised, user, timezone.now()
    field.save()
    log_event(doc.organization, "field.corrected", doc, actor=user, key=key, before=before, after=normalised)
    # re-run the rules so the decision reflects the corrected value
    if doc.decided_by != Document.DecidedBy.PERSON and doc.status != Document.Status.PROCESSING:
        evaluate_document(doc, actor=user)
    return field


def review(doc: Document, decision: str, user, note: str = "") -> Document:
    doc.status = Document.Status.APPROVED if decision == "approve" else Document.Status.REJECTED
    doc.decided_by = Document.DecidedBy.PERSON
    doc.reviewed_by, doc.reviewed_at = user, timezone.now()
    doc.save(update_fields=["status", "decided_by", "reviewed_by", "reviewed_at"])
    log_event(doc.organization, f"review.{decision}d", doc, actor=user, note=note,
              overrode=[r["name"] for r in doc.rule_results if r.get("matched")])
    return doc


def reopen(doc: Document, user) -> Document:
    """Undo a person's decision and hand the document back to the rules."""
    doc.decided_by = ""
    doc.save(update_fields=["decided_by"])
    log_event(doc.organization, "review.reopened", doc, actor=user)
    return evaluate_document(doc, actor=user)

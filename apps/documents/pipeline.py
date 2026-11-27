"""Document pipeline: OCR → field extraction → facts → rules → decision.

Each step is idempotent, so a document can be reprocessed (new OCR) or only
re-evaluated (after a reviewer fixes a field or an admin changes a rule).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.extraction.invoice import get_extractor
from apps.ocr.base import OcrResult
from apps.ocr.service import run_ocr
from apps.rules.models import Action
from apps.rules.services import build_facts, decide

from .models import Document, ExtractedField, log_event

log = logging.getLogger(__name__)

STATUS_FOR_ACTION = {
    Action.APPROVE: Document.Status.APPROVED,
    Action.REVIEW: Document.Status.NEEDS_REVIEW,
    Action.REJECT: Document.Status.REJECTED,
}


def sha256_of(fileobj) -> str:
    h = hashlib.sha256()
    for chunk in fileobj.chunks():
        h.update(chunk)
    fileobj.seek(0)
    return h.hexdigest()


def process_document(document_id: int) -> Document:
    """Full run. Safe to call again: replaces OCR output and machine-extracted values."""
    doc = Document.objects.select_related("document_type", "organization").get(pk=document_id)
    doc.status, doc.error = Document.Status.PROCESSING, ""
    doc.save(update_fields=["status", "error"])
    try:
        ocr = run_ocr(Path(doc.file.path), doc.pages_dir, settings.OCR_ENGINE, Path(settings.MEDIA_ROOT))
        extracted = get_extractor(doc.document_type.extractor).extract(ocr)
        with transaction.atomic():
            doc.ocr = ocr.to_json()
            doc.ocr_engine, doc.ocr_seconds = ocr.engine, round(ocr.seconds, 2)
            doc.ocr_confidence = round(ocr.mean_confidence, 4)
            doc.save(update_fields=["ocr", "ocr_engine", "ocr_seconds", "ocr_confidence"])
            _store_fields(doc, extracted)
            log_event(doc.organization, "ocr.completed", doc, engine=ocr.engine, seconds=doc.ocr_seconds,
                      lines=len(ocr.lines), pages=len(ocr.pages), fields=sorted(extracted))
        return evaluate_document(doc)
    except Exception as exc:  # noqa: BLE001 - any failure must leave the document in a visible state
        log.exception("Processing failed for document %s", doc.pk)
        doc.status, doc.error = Document.Status.FAILED, f"{type(exc).__name__}: {exc}"
        doc.save(update_fields=["status", "error"])
        log_event(doc.organization, "processing.failed", doc, error=doc.error)
        return doc


def _store_fields(doc: Document, extracted: dict) -> None:
    """Upsert machine values. Corrections a reviewer already made are kept."""
    existing = {f.key: f for f in doc.fields.all()}
    schema = get_extractor(doc.document_type.extractor).field_schema()
    for spec in schema:
        key = spec["key"]
        e = extracted.get(key)
        f = existing.get(key) or ExtractedField(document=doc, key=key)
        f.value = e.value if e else None
        f.raw_text = e.raw_text if e else ""
        f.confidence = round(e.confidence, 4) if e else 0.0
        f.page = e.page if e else None
        f.bbox = list(e.bbox) if e and e.bbox else None
        f.method = e.method if e else ""
        f.save()


def evaluate_document(doc: Document, actor=None) -> Document:
    """Rebuild facts and run the rules. Does not touch a decision a person already made."""
    if doc.decided_by == Document.DecidedBy.PERSON:
        return doc
    facts = build_facts(doc)
    decision = decide(doc, facts)
    doc.facts = facts
    doc.rule_results = decision.results
    doc.reasons = decision.reasons
    doc.queue = decision.queue
    doc.status = STATUS_FOR_ACTION[decision.action]
    doc.decided_by = Document.DecidedBy.RULES
    doc.processed_at = timezone.now()
    doc.save(update_fields=["facts", "rule_results", "reasons", "queue", "status", "decided_by", "processed_at"])
    log_event(doc.organization, "rules.evaluated", doc, actor=actor, decision=decision.action,
              queue=decision.queue, reasons=decision.reasons,
              matched=[r["name"] for r in decision.results if r.get("matched")])
    return doc


def ocr_result(doc: Document) -> OcrResult | None:
    return OcrResult.from_json(doc.ocr) if doc.ocr else None

"""Server-rendered review dashboard (no build step, works on any laptop)."""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from rest_framework.exceptions import ValidationError

from apps.core.models import Membership
from apps.documents import services
from apps.documents.models import AuditEvent, Document, DocumentType
from apps.documents.serializers import ALLOWED_EXTS
from apps.extraction.invoice import get_extractor
from apps.rules.models import RuleSet
from apps.rules.services import DERIVED_FACTS

REVIEW_ROLES = (Membership.Role.ADMIN, Membership.Role.REVIEWER)


def _member(request) -> Membership:
    m = getattr(request.user, "membership", None)
    if m is None:
        raise PermissionDenied("Your account is not linked to an organisation")
    return m


def _require_reviewer(request) -> Membership:
    m = _member(request)
    if m.role not in REVIEW_ROLES:
        raise PermissionDenied("Reviewer access required")
    return m


@login_required
def document_list(request):
    m = _member(request)
    docs = Document.objects.filter(organization=m.organization).select_related("document_type")
    status = request.GET.get("status", "")
    if status:
        docs = docs.filter(status=status)
    counts = dict(Document.objects.filter(organization=m.organization).values_list("status")
                  .annotate(n=Count("id")).values_list("status", "n"))
    tabs = [("", "All", sum(counts.values()))] + [
        (s.value, s.label, counts.get(s.value, 0)) for s in Document.Status if s != Document.Status.UPLOADED]
    return render(request, "dashboard/list.html", {
        "docs": docs[:200], "status": status, "tabs": tabs, "member": m,
        "doc_types": DocumentType.objects.filter(organization=m.organization),
        "accept": ",".join(sorted(ALLOWED_EXTS)),
    })


@login_required
@require_POST
def upload(request):
    m = _require_reviewer(request)
    dtype = get_object_or_404(DocumentType, organization=m.organization, slug=request.POST.get("document_type"))
    files = request.FILES.getlist("files")
    if not files:
        messages.error(request, "Choose at least one file")
        return redirect("dashboard:list")
    done = 0
    for f in files[:20]:
        if Path(f.name).suffix.lower() not in ALLOWED_EXTS or f.size > settings.MAX_UPLOAD_BYTES:
            messages.error(request, f"Skipped {f.name}: unsupported type or too large")
            continue
        services.upload_document(m.organization, dtype, f, request.user)
        done += 1
    if done:
        messages.success(request, f"Uploaded {done} document(s)")
    return redirect("dashboard:list")


@login_required
def document_detail(request, pk):
    m = _member(request)
    doc = get_object_or_404(Document.objects.select_related("document_type", "reviewed_by"),
                            organization=m.organization, pk=pk)
    schema = get_extractor(doc.document_type.extractor).field_schema()
    stored = {f.key: f for f in doc.fields.all()}
    fields = []
    for spec in schema:
        f = stored.get(spec["key"])
        fields.append({"spec": spec, "f": f,
                       "low": bool(f and not f.is_corrected and f.confidence < doc.document_type.confidence_threshold),
                       "missing": not f or f.final_value in (None, "")})
    derived = [(d, doc.facts.get(d.key)) for d in DERIVED_FACTS]
    pages = (doc.ocr or {}).get("pages", [])
    boxes = [{"key": f.key, "page": f.page, "bbox": f.bbox} for f in stored.values() if f.bbox]
    return render(request, "dashboard/detail.html", {
        "doc": doc, "fields": fields, "derived": derived, "pages": pages, "member": m,
        "can_review": m.role in REVIEW_ROLES,
        "boxes": boxes,
        "events": doc.events.select_related("actor"),
        "threshold": doc.document_type.confidence_threshold,
    })


@login_required
@require_POST
def correct(request, pk):
    m = _require_reviewer(request)
    doc = get_object_or_404(Document, organization=m.organization, pk=pk)
    key = request.POST.get("key", "")
    try:
        services.correct_field(doc, key, request.POST.get("value", ""), request.user)
        messages.success(request, f"Saved {key.replace('_', ' ')} and re-ran the rules")
    except KeyError:
        messages.error(request, "Unknown field")
    except ValidationError as exc:
        messages.error(request, f"{key.replace('_', ' ')}: {' '.join(map(str, exc.detail))}")
    return redirect("dashboard:detail", pk=pk)


@login_required
@require_POST
def decide(request, pk):
    m = _require_reviewer(request)
    doc = get_object_or_404(Document, organization=m.organization, pk=pk)
    decision = request.POST.get("decision")
    if decision == "reopen":
        services.reopen(doc, request.user)
        messages.info(request, "Reopened and handed back to the rules")
    elif decision in ("approve", "reject"):
        services.review(doc, decision, request.user, request.POST.get("note", ""))
        messages.success(request, f"Document {decision}d")
    elif decision == "reprocess":
        services.enqueue(doc)
        messages.info(request, "Queued for OCR again")
    return redirect("dashboard:detail", pk=pk)


@login_required
def rules(request):
    m = _member(request)
    rulesets = RuleSet.objects.filter(organization=m.organization).select_related("document_type")
    changes = AuditEvent.objects.filter(organization=m.organization, action__startswith="ruleset.") \
        .select_related("actor").order_by("-created_at")[:20]
    return render(request, "dashboard/rules.html", {"rulesets": rulesets, "changes": changes, "member": m})


@login_required
def protected_file(request, path):
    """Serve an uploaded file or page image only to members of the owning organisation."""
    m = _member(request)
    if ".." in Path(path).parts or not path.startswith(f"org-{m.organization_id}/"):
        raise Http404()
    full = (Path(settings.MEDIA_ROOT) / path).resolve()
    if not full.is_file() or Path(settings.MEDIA_ROOT).resolve() not in full.parents:
        raise Http404()
    ctype = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
    return FileResponse(open(full, "rb"), content_type=ctype)

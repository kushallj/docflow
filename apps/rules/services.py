"""Glue between documents and the rule engine: fact catalog, fact values, decision."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from apps.extraction.invoice import get_extractor
from apps.extraction.normalize import gstin_is_valid

from .engine import FactCatalog, FactDef, RuleError, evaluate
from .models import SEVERITY, Action, RuleSet

# Facts computed by DocFlow on top of the OCR fields. These are what make rules
# useful: "the GSTIN checksum fails", "the vendor is not in our master",
# "this invoice number was already submitted".
DERIVED_FACTS = [
    FactDef("vendor_gstin_valid", "Vendor GSTIN checksum valid", "bool",
            "Shape and mod-36 checksum of the vendor GSTIN"),
    FactDef("buyer_gstin_valid", "Buyer GSTIN checksum valid", "bool"),
    FactDef("billed_to_us", "Billed to our GSTIN", "bool",
            "Buyer GSTIN equals the organisation's GSTIN"),
    FactDef("vendor_known", "Vendor in vendor master", "bool"),
    FactDef("vendor_approved", "Vendor approved", "bool", "Vendor exists in the master and is approved"),
    FactDef("is_duplicate", "Duplicate invoice", "bool",
            "Same vendor GSTIN and invoice number already uploaded"),
    FactDef("totals_consistent", "Subtotal + tax = total", "bool", "Within ₹1 rounding"),
    FactDef("tax_rate_percent", "Effective tax rate (%)", "number"),
    FactDef("days_until_due", "Days until due", "number", "Negative when overdue"),
    FactDef("invoice_age_days", "Invoice age (days)", "number"),
    FactDef("min_field_confidence", "Lowest required-field confidence", "number", "0 to 1"),
    FactDef("low_confidence_count", "Required fields below threshold", "number"),
    FactDef("missing_required_count", "Required fields missing", "number"),
    FactDef("ocr_confidence", "Mean OCR confidence", "number", "0 to 1"),
    FactDef("page_count", "Page count", "number"),
]


def catalog_for(document_type) -> FactCatalog:
    extractor = get_extractor(document_type.extractor)
    fields = [FactDef(f["key"], f["label"], f["type"]) for f in extractor.field_schema()]
    return FactCatalog(fields + DERIVED_FACTS)


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _today() -> date:
    return timezone.localdate()


def build_facts(document, today: date | None = None) -> dict:
    """Field values (corrections win) plus derived facts, as plain JSON values."""
    from apps.core.models import Vendor
    from apps.documents.models import Document, ExtractedField

    today = today or _today()
    dtype = document.document_type
    schema = get_extractor(dtype.extractor).field_schema()
    # query fresh: the caller may hold a prefetched (stale) copy from before a correction
    fields = {f.key: f for f in ExtractedField.objects.filter(document=document)}
    facts: dict = {k: f.final_value for k, f in fields.items()}

    # -- confidence and completeness of required fields
    required = [s["key"] for s in schema if s["required"]]
    present = [k for k in required if k in fields and fields[k].final_value not in (None, "")]
    confs = [fields[k].final_confidence for k in present]
    facts["missing_required_count"] = len(required) - len(present)
    facts["low_confidence_count"] = sum(1 for c in confs if c < dtype.confidence_threshold)
    facts["min_field_confidence"] = round(min(confs), 3) if confs else 0
    facts["ocr_confidence"] = round(document.ocr_confidence or 0, 3)
    facts["page_count"] = len((document.ocr or {}).get("pages", []))

    # -- GSTINs
    vendor_gstin = (facts.get("vendor_gstin") or "").upper()
    buyer_gstin = (facts.get("buyer_gstin") or "").upper()
    facts["vendor_gstin_valid"] = gstin_is_valid(vendor_gstin)
    facts["buyer_gstin_valid"] = gstin_is_valid(buyer_gstin)
    org_gstin = (document.organization.gstin or "").upper()
    facts["billed_to_us"] = bool(org_gstin) and buyer_gstin == org_gstin

    # -- vendor master: match on GSTIN, fall back to exact name
    vendors = Vendor.objects.filter(organization=document.organization)
    vendor = None
    if vendor_gstin:
        vendor = vendors.filter(gstin__iexact=vendor_gstin).first()
    if vendor is None and facts.get("vendor_name"):
        vendor = vendors.filter(name__iexact=str(facts["vendor_name"]).strip()).first()
    facts["vendor_known"] = vendor is not None
    facts["vendor_approved"] = bool(vendor and vendor.approved)

    # -- duplicates: same vendor + invoice number, or the exact same file
    number = facts.get("invoice_number")
    dupes = Document.objects.filter(organization=document.organization).exclude(pk=document.pk).exclude(
        status=Document.Status.FAILED)
    cond = Q(sha256=document.sha256)
    if number and vendor_gstin:
        cond |= Q(fields__key="invoice_number", fields__value=number) & Q(
            pk__in=Document.objects.filter(fields__key="vendor_gstin", fields__value=vendor_gstin).values("pk"))
    facts["is_duplicate"] = dupes.filter(cond).exists()

    # -- arithmetic
    sub, tax, total = _dec(facts.get("subtotal")), _dec(facts.get("tax_amount")), _dec(facts.get("total_amount"))
    facts["totals_consistent"] = (sub is not None and tax is not None and total is not None
                                  and abs(sub + tax - total) <= 1)
    facts["tax_rate_percent"] = str(round(tax / sub * 100, 2)) if sub and tax is not None else None

    # -- dates
    due, issued = _date(facts.get("due_date")), _date(facts.get("invoice_date"))
    facts["days_until_due"] = (due - today).days if due else None
    facts["invoice_age_days"] = (today - issued).days if issued else None
    return facts


@dataclass
class Decision:
    action: str
    queue: str = ""
    reasons: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)


def decide(document, facts: dict) -> Decision:
    """Run every active rule set; the most severe matching action wins."""
    dtype = document.document_type
    catalog = catalog_for(dtype)
    rulesets = RuleSet.objects.filter(organization=document.organization, document_type=dtype, is_active=True)

    results, matched, why = [], [], {}
    for rs in rulesets:
        entry = {"id": rs.id, "name": rs.name, "version": rs.version, "action": rs.action, "route_to": rs.route_to}
        try:
            trace = evaluate(rs.tree, facts, catalog)
            entry.update(matched=trace.passed, trace=trace.as_dict())
            if trace.passed:
                matched.append(rs)
                why[rs.id] = _passed_conditions(trace.as_dict())
        except RuleError as exc:
            # a broken rule must never approve anything: treat as "send to review"
            entry.update(matched=False, error=str(exc))
            matched.append(RuleSet(name=rs.name, action=Action.REVIEW, reason=f"Rule '{rs.name}' is invalid: {exc}"))
        results.append(entry)

    if matched:
        worst = max(matched, key=lambda r: SEVERITY[r.action])
        action = worst.action
        queue = next((r.route_to for r in matched if r.action == action and r.route_to), "")
        reasons = [_reason(r, why.get(r.id, [])) for r in matched if r.action == action]
    else:
        action, queue = dtype.default_decision, ""
        reasons = ["No rule matched; using the document type's default"]

    # Safety net: never auto-approve something we could not read reliably.
    if action == Action.APPROVE:
        blockers = []
        if not dtype.auto_approve_enabled:
            blockers.append("auto-approval is turned off for this document type")
        if facts.get("missing_required_count"):
            blockers.append(f"{facts['missing_required_count']} required field(s) missing")
        if facts.get("low_confidence_count"):
            blockers.append(f"{facts['low_confidence_count']} required field(s) below "
                            f"{dtype.confidence_threshold:.0%} confidence")
        if blockers:
            action = Action.REVIEW
            reasons = [f"Auto-approval blocked: {', '.join(blockers)}"]

    return Decision(action=action, queue=queue, reasons=reasons, results=results)


def _passed_conditions(trace: dict) -> list[str]:
    """Descriptions of the leaf conditions that made a matched rule true."""
    if trace["kind"] == "condition":
        return [trace["description"]] if trace["passed"] else []
    if not trace["passed"] or trace["description"].startswith("NOT"):
        return []
    return [d for c in trace.get("children", []) for d in _passed_conditions(c)]


def _reason(rule: RuleSet, conditions: list[str]) -> str:
    base = rule.reason or f"Matched rule '{rule.name}'"
    # only worth spelling out for ANY-style rules, where the reason text lists alternatives
    if conditions and rule.tree.get("type") == "any":
        return f"{base} (matched: {'; '.join(conditions[:3])})"
    return base

"""GST invoice extractor (Indian tax invoices)."""
from __future__ import annotations

import re
from decimal import Decimal

from apps.ocr.base import OcrLine, OcrResult

from .base import Extracted, Extractor, Locator, reading_order, x_overlap
from .normalize import as_iso, detect_currency, find_gstins, parse_amount, parse_date

I = re.IGNORECASE

INVOICE_NO = re.compile(r"\b(invoice|inv|bill)\s*(no\.?|number|num|#)", I)
INVOICE_DATE = re.compile(r"\b(invoice|inv|bill)?\s*date\b", I)
DUE_DATE = re.compile(r"\b(due\s*date|payment\s*due|due\s*by|due\s*on)\b", I)
PO_NO = re.compile(r"\b(p\.?\s?o\.?\s*(no\b\.?|number|#)|purchase\s*order(\s*(no\b\.?|number|#))?)", I)
SUBTOTAL = re.compile(r"\b(sub\s*-?\s*total|taxable\s*(value|amount)|amount\s*before\s*tax)\b", I)
CGST = re.compile(r"\bcgst\b", I)
SGST = re.compile(r"\b(sgst|utgst)\b", I)
IGST = re.compile(r"\bigst\b", I)
TAX_TOTAL = re.compile(r"\b(total\s*tax|tax\s*amount|total\s*gst|gst\s*amount)\b", I)
GRAND_TOTAL = re.compile(
    r"\b(grand\s*total|total\s*amount|amount\s*payable|net\s*payable|invoice\s*total|"
    r"total\s*(payable|due)|balance\s*due|total\s*\(?(inr|rs\.?|₹)\)?|^total)\b", I)
NOT_GRAND = re.compile(r"sub\s*-?\s*total|total\s*tax|taxable|total\s*gst|in\s*words|total\s*qty|total\s*items", I)
BUYER_HDR = re.compile(r"\b(bill(ed)?\s*to|buyer|customer|ship(ped)?\s*to|consignee|recipient)\b", I)
SELLER_HDR = re.compile(r"\b(from|seller|supplier|sold\s*by|vendor)\b", I)
TITLE_WORDS = re.compile(r"^(tax\s*)?invoice$|^bill\s*of\s*supply$|^original|^duplicate|^gst\s*invoice$", I)
PERCENT = re.compile(r"@?\s*\d+(\.\d+)?\s*%")
LABELISH = re.compile(r"(invoice|date|gstin|bill|ship|phone|email|address|tel|www\.|@)", I)


def _invoice_no(text: str) -> str | None:
    text = text.strip().lstrip(":#.- ")
    m = re.match(r"([A-Z0-9][A-Z0-9\-/_.]{1,30})", text, I)
    if not m:
        return None
    token = m.group(1).rstrip(".-/")
    if not re.search(r"\d", token) or parse_date(token) is not None and "/" in token and len(token) <= 10:
        return None
    return token.upper()


def _date(text: str):
    d = parse_date(text)
    return as_iso(d) if d else None


def _money(text: str):
    a = parse_amount(PERCENT.sub(" ", text))
    return str(a) if a is not None else None


class InvoiceExtractor(Extractor):
    slug = "invoice"
    name = "GST invoice"
    FIELDS = [
        ("invoice_number", "Invoice number", "string", True),
        ("invoice_date", "Invoice date", "date", True),
        ("due_date", "Due date", "date", False),
        ("po_number", "PO number", "string", False),
        ("vendor_name", "Vendor name", "string", True),
        ("vendor_gstin", "Vendor GSTIN", "string", True),
        ("buyer_gstin", "Buyer GSTIN", "string", False),
        ("subtotal", "Subtotal (taxable value)", "number", False),
        ("tax_amount", "Total tax (CGST+SGST+IGST)", "number", False),
        ("total_amount", "Grand total", "number", True),
        ("currency", "Currency", "string", False),
    ]

    def extract(self, ocr: OcrResult) -> dict[str, Extracted]:
        loc = Locator(ocr)
        out: dict[str, Extracted] = {}

        def first(key, label, parse, exclude=None):
            hits = loc.labelled(key, label, parse, exclude)
            if hits:
                out[key] = max(hits, key=lambda h: h.confidence)

        first("invoice_number", INVOICE_NO, _invoice_no, exclude=re.compile(r"date", I))
        first("invoice_date", INVOICE_DATE, _date, exclude=re.compile(r"due|order|p\.?o\.?\b|delivery", I))
        first("due_date", DUE_DATE, _date)
        first("po_number", PO_NO, _invoice_no, exclude=re.compile(r"date|box", I))
        first("subtotal", SUBTOTAL, _money)

        # if the invoice-date label was missed, fall back to the first date on page 1
        if "invoice_date" not in out:
            for ln in loc.lines:
                if (v := _date(ln.text)) and not DUE_DATE.search(ln.text):
                    out["invoice_date"] = Extracted("invoice_date", v, ln.text, ln.confidence * 0.6, ln.page,
                                                    ln.bbox, "heuristic")
                    break

        self._tax(loc, out)
        self._total(loc, out)
        self._parties(loc, out)
        self._currency(ocr, out)
        return out

    # -- tax: CGST + SGST (intra-state) or IGST (inter-state), else a "total tax" line
    def _tax(self, loc: Locator, out: dict) -> None:
        parts = []
        for key, label in (("cgst", CGST), ("sgst", SGST), ("igst", IGST)):
            hits = [h for h in loc.labelled(key, label, _money) if Decimal(h.value) > 0]
            if hits:
                parts.append(max(hits, key=lambda h: h.confidence))
        if parts:
            total = sum(Decimal(p.value) for p in parts)
            out["tax_amount"] = Extracted(
                "tax_amount", str(total), " + ".join(p.raw_text for p in parts),
                min(p.confidence for p in parts), parts[0].page, parts[0].bbox, "derived")
            return
        hits = loc.labelled("tax_amount", TAX_TOTAL, _money)
        if hits:
            out["tax_amount"] = max(hits, key=lambda h: h.confidence)

    # -- grand total: the largest of all "total"-like amounts, excluding subtotal / tax lines
    def _total(self, loc: Locator, out: dict) -> None:
        hits = loc.labelled("total_amount", GRAND_TOTAL, _money, exclude=NOT_GRAND)
        if not hits:
            return
        best = max(hits, key=lambda h: (Decimal(h.value), h.confidence))
        sub, tax = out.get("subtotal"), out.get("tax_amount")
        if sub and tax:
            expected = Decimal(sub.value) + Decimal(tax.value)
            # agreeing with subtotal + tax raises our confidence that we picked the right line
            if abs(expected - Decimal(best.value)) <= 1:
                best.confidence = min(1.0, best.confidence * 1.05)
        out["total_amount"] = best

    # -- vendor name and the two GSTINs (seller vs buyer)
    def _parties(self, loc: Locator, out: dict) -> None:
        lines = loc.lines
        headers = [ln for ln in lines if BUYER_HDR.search(ln.text) or SELLER_HDR.search(ln.text)]

        def section(ln: OcrLine) -> str | None:
            above = [h for h in headers if h.page == ln.page and h.y0 <= ln.y0 + 2 and x_overlap(h, ln, 120)]
            if not above:
                return None
            h = max(above, key=lambda h: h.y0)
            return "buyer" if BUYER_HDR.search(h.text) else "seller"

        gstin_hits: list[tuple[str, OcrLine, str | None]] = []
        for ln in lines:
            for g in find_gstins(ln.text):
                gstin_hits.append((g, ln, section(ln)))

        seller = next((h for h in gstin_hits if h[2] == "seller"), None)
        buyer = next((h for h in gstin_hits if h[2] == "buyer"), None)
        unassigned = [h for h in gstin_hits if h[2] is None]
        if seller is None and unassigned:
            seller = unassigned.pop(0)
        if buyer is None:
            rest = [h for h in gstin_hits if h is not seller and h[0] != (seller[0] if seller else None)]
            buyer = rest[0] if rest else None

        for key, hit in (("vendor_gstin", seller), ("buyer_gstin", buyer)):
            if hit:
                g, ln, sec = hit
                # an unlabelled GSTIN is still reliable when the other one sits under a buyer header
                weight = 0.97 if sec else (0.93 if buyer and buyer[2] == "buyer" else 0.8)
                out[key] = Extracted(key, g, ln.text, ln.confidence * weight, ln.page, ln.bbox,
                                     "heuristic" if sec is None else "below")

        # vendor name: line after a seller header, else the most prominent line at the top of page 1
        name = None
        for h in headers:
            if SELLER_HDR.search(h.text) and not BUYER_HDR.search(h.text):
                tail = SELLER_HDR.sub("", h.text).strip(" :-")
                if len(tail) > 2 and not LABELISH.search(tail):
                    name = (tail, h, 0.9)
                    break
                nxt = [o for o in lines if o.page == h.page and o.y0 > h.y0 and x_overlap(h, o, 60)]
                nxt = sorted(nxt, key=lambda o: o.y0)
                if nxt and not LABELISH.search(nxt[0].text):
                    name = (nxt[0].text, nxt[0], 0.9)
                    break
        if name is None:
            page1 = [ln for ln in lines if ln.page == 1]
            if page1:
                top_limit = min(ln.y0 for ln in page1) + 0.25 * (max(ln.y1 for ln in page1) - min(ln.y0 for ln in page1))
                cands = [ln for ln in page1 if ln.y0 <= top_limit and not TITLE_WORDS.search(ln.text.strip())
                         and not LABELISH.search(ln.text) and len(ln.text) > 3
                         and not re.search(r"\d{4,}", ln.text)]
                if cands:
                    big = max(cands, key=lambda ln: (ln.height, -ln.y0))
                    # clearly the largest text in the header band -> letterhead name
                    second = sorted((c.height for c in cands), reverse=True)[1:2]
                    name = (big.text, big, 0.9 if not second or big.height > 1.2 * second[0] else 0.75)
        if name:
            text, ln, weight = name
            out["vendor_name"] = Extracted("vendor_name", text.strip(), ln.text, ln.confidence * weight,
                                           ln.page, ln.bbox, "heuristic")

    def _currency(self, ocr: OcrResult, out: dict) -> None:
        cur = detect_currency(ocr.text)
        if cur is None and ("vendor_gstin" in out or "buyer_gstin" in out):
            cur = "INR"
        if cur:
            out["currency"] = Extracted("currency", cur, cur, 0.9, None, None, "heuristic")


EXTRACTORS: dict[str, type[Extractor]] = {InvoiceExtractor.slug: InvoiceExtractor}


def get_extractor(slug: str) -> Extractor:
    try:
        return EXTRACTORS[slug]()
    except KeyError as exc:
        raise ValueError(f"No extractor registered for document type '{slug}'") from exc


__all__ = ["InvoiceExtractor", "get_extractor", "EXTRACTORS", "reading_order"]

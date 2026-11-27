"""Generate sample GST invoices (PNG + PDF) for demos and end-to-end tests.

    python samples/make_invoices.py            # writes into samples/invoices/

All companies, people and GSTINs are fictional. GSTINs are generated with a
valid checksum unless a scenario needs an invalid one.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from apps.extraction.normalize import gstin_check_char  # noqa: E402

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
BOLD_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in BOLD_CANDIDATES if bold else FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def gstin(state: str, pan: str, entity: str = "1") -> str:
    first14 = f"{state}{pan}{entity}Z"
    return first14 + gstin_check_char(first14)


def inr(v: Decimal) -> str:
    """Format with Indian digit grouping: 1,23,456.00"""
    s = f"{v:.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{whole}.{frac}"


@dataclass
class Invoice:
    file: str
    vendor: str
    vendor_addr: str
    vendor_gstin: str
    buyer: str
    buyer_addr: str
    buyer_gstin: str
    number: str
    issued: date
    due: date
    po: str
    items: list[tuple[str, int, Decimal]]
    gst_rate: Decimal = Decimal("18")
    inter_state: bool = False
    noise: bool = False
    note: str = ""
    total_override: Decimal | None = None
    extra: dict = field(default_factory=dict)


def render(inv: Invoice, out_dir: Path) -> None:
    W, H = 1654, 2339   # A4 at 200 dpi
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_title, f_big, f_b, f, f_s = font(64, True), font(44, True), font(30, True), font(30), font(26)
    ink, grey, line = (20, 20, 20), (90, 90, 90), (160, 160, 160)

    d.text((100, 90), inv.vendor, font=f_big, fill=ink)
    y = 160
    for part in inv.vendor_addr.split("\n"):
        d.text((100, y), part, font=f_s, fill=grey)
        y += 36
    d.text((100, y + 4), f"GSTIN: {inv.vendor_gstin}", font=f, fill=ink)
    d.text((1140, 90), "TAX INVOICE", font=f_title, fill=ink)

    d.line((100, 400, W - 100, 400), fill=line, width=3)
    # invoice meta (left) and buyer (right)
    meta = [("Invoice No:", inv.number), ("Invoice Date:", inv.issued.strftime("%d/%m/%Y")),
            ("Due Date:", inv.due.strftime("%d-%b-%Y")), ("PO Number:", inv.po)]
    y = 440
    for label, value in meta:
        d.text((100, y), label, font=f_b, fill=ink)
        d.text((360, y), value, font=f, fill=ink)
        y += 52
    d.text((900, 440), "Bill To:", font=f_b, fill=ink)
    d.text((900, 492), inv.buyer, font=f, fill=ink)
    y = 540
    for part in inv.buyer_addr.split("\n"):
        d.text((900, y), part, font=f_s, fill=grey)
        y += 36
    d.text((900, y + 4), f"GSTIN: {inv.buyer_gstin}", font=f, fill=ink)

    # items table
    top = 780
    cols = [100, 220, 900, 1080, 1300]
    d.rectangle((100, top, W - 100, top + 60), fill=(235, 238, 242))
    for x, h in zip(cols, ["#", "Description", "Qty", "Rate", "Amount"]):
        d.text((x + 14, top + 14), h, font=f_b, fill=ink)
    y = top + 80
    subtotal = Decimal("0")
    for i, (desc, qty, rate) in enumerate(inv.items, start=1):
        amount = rate * qty
        subtotal += amount
        for x, v in zip(cols, [str(i), desc, str(qty), inr(rate), inr(amount)]):
            d.text((x + 14, y), v, font=f, fill=ink)
        y += 58
        d.line((100, y - 8, W - 100, y - 8), fill=(225, 225, 225), width=1)

    # totals block
    y += 40
    rate = inv.gst_rate
    tax_rows = ([(f"IGST @ {rate}%", subtotal * rate / 100)] if inv.inter_state else
                [(f"CGST @ {rate / 2}%", subtotal * rate / 200), (f"SGST @ {rate / 2}%", subtotal * rate / 200)])
    tax = sum(v for _, v in tax_rows)
    total = inv.total_override if inv.total_override is not None else subtotal + tax
    rows = [("Sub Total", subtotal)] + tax_rows
    for label, value in rows:
        d.text((1000, y), label, font=f, fill=ink)
        d.text((1300, y), inr(value.quantize(Decimal("0.01"))), font=f, fill=ink)
        y += 52
    d.line((1000, y, W - 100, y), fill=line, width=2)
    y += 16
    d.text((1000, y), "Grand Total", font=f_b, fill=ink)
    d.text((1300, y), f"Rs. {inr(total.quantize(Decimal('0.01')))}", font=f_b, fill=ink)

    y += 120
    d.text((100, y), "Bank: HDFC Bank, A/C 50200012345678, IFSC HDFC0001234", font=f_s, fill=grey)
    d.text((100, y + 40), "Payment terms: Net 30. Please quote the invoice number with payment.", font=f_s, fill=grey)
    if inv.note:
        d.text((100, y + 80), inv.note, font=f_s, fill=grey)
    d.text((1200, H - 260), f"For {inv.vendor}", font=f_s, fill=ink)
    d.text((1200, H - 160), "Authorised Signatory", font=f_s, fill=grey)

    if inv.noise:   # simulate a phone photo / fax: slight blur, rotation, grey background
        im = im.rotate(1.2, expand=False, fillcolor="white", resample=Image.BICUBIC)
        im = im.filter(ImageFilter.GaussianBlur(1.1))
        bg = Image.new("RGB", im.size, (238, 235, 228))
        im = Image.blend(im, bg, 0.18)

    out_dir.mkdir(parents=True, exist_ok=True)
    im.save(out_dir / f"{inv.file}.png")
    if inv.extra.get("pdf"):
        im.save(out_dir / f"{inv.file}.pdf", "PDF", resolution=200)


def scenarios() -> list[Invoice]:
    today = date.today()
    buyer = "Acme Retail Pvt Ltd"
    buyer_addr = "Plot 14, Sector 62\nNoida, Uttar Pradesh 201301"
    buyer_g = gstin("09", "AAACA1234F")
    return [
        Invoice(
            "01-approved-office-supplies", "Sharma Stationers", "22 Karol Bagh\nNew Delhi 110005",
            gstin("07", "ABCPS4821K"), buyer, buyer_addr, buyer_g,
            "SS/2026/1187", today - timedelta(days=3), today + timedelta(days=27), "PO-55102",
            [("A4 paper ream (500 sheets)", 40, Decimal("285.00")), ("Ball pens, box of 50", 10, Decimal("420.00")),
             ("Stapler, heavy duty", 5, Decimal("650.00"))],
            gst_rate=Decimal("12"), inter_state=True, extra={"pdf": True},
        ),
        Invoice(
            "02-review-high-value", "Northwind IT Solutions", "4th Floor, Cyber Hub\nGurugram, Haryana 122002",
            gstin("06", "AAFCN7342Q"), buyer, buyer_addr, buyer_g,
            "NW-INV-24090", today - timedelta(days=5), today + timedelta(days=10), "PO-55188",
            [("Dell Latitude 5450 laptop", 6, Decimal("78500.00")), ("27in monitor", 6, Decimal("16400.00")),
             ("On-site setup and imaging", 1, Decimal("12000.00"))],
            inter_state=True,
        ),
        Invoice(
            "03-reject-invalid-gstin", "Quickfix Facility Services", "Shop 9, Lajpat Nagar II\nNew Delhi 110024",
            "07AAGFQ5521M1ZX", buyer, buyer_addr, buyer_g,     # checksum deliberately wrong
            "QF-0931", today - timedelta(days=2), today + timedelta(days=15), "PO-55190",
            [("Deep cleaning, 3 floors", 1, Decimal("24000.00")), ("Pest control", 1, Decimal("6500.00"))],
            inter_state=True,
        ),
        Invoice(
            "04-review-unknown-vendor-scan", "Ganesh Packaging Co", "MIDC Bhosari\nPune, Maharashtra 411026",
            gstin("27", "AAKFG3390B"), buyer, buyer_addr, buyer_g,
            "GPC/26-27/0412", today - timedelta(days=12), today + timedelta(days=18), "PO-55077",
            [("Corrugated boxes 5-ply", 800, Decimal("38.50")), ("Packing tape 2in", 120, Decimal("62.00"))],
            inter_state=True, noise=True,
        ),
        Invoice(
            "05-review-totals-mismatch", "Sharma Stationers", "22 Karol Bagh\nNew Delhi 110005",
            gstin("07", "ABCPS4821K"), buyer, buyer_addr, buyer_g,
            "SS/2026/1204", today - timedelta(days=1), today + timedelta(days=29), "PO-55203",
            [("Whiteboard markers, box", 20, Decimal("310.00")), ("File folders, pack of 25", 12, Decimal("275.00"))],
            gst_rate=Decimal("12"), inter_state=True, total_override=Decimal("11250.00"),
        ),
    ]


if __name__ == "__main__":
    out = ROOT / "invoices"
    for inv in scenarios():
        render(inv, out)
        print("wrote", inv.file)

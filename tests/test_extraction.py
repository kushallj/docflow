from datetime import date
from decimal import Decimal

import pytest

from apps.extraction.invoice import get_extractor
from apps.extraction.normalize import (clean_gstin, find_gstins, gstin_check_char, gstin_is_valid, parse_amount,
                                       parse_date)
from apps.ocr.base import OcrLine, OcrPage, OcrResult


class TestNormalize:
    @pytest.mark.parametrize("text,expected", [
        ("Rs. 1,23,456.00", "123456.00"),
        ("₹ 6,86,052.00", "686052.00"),
        ("Grand Total 59,000", "59000"),
        ("CGST @ 9% 4,500.00", "4500.00"),   # last number wins
        ("123456.5", "123456.5"),
        ("no amount", None),
    ])
    def test_amounts(self, text, expected):
        got = parse_amount(text)
        assert (str(got) if got is not None else None) == expected

    @pytest.mark.parametrize("text,expected", [
        ("24/11/2026", date(2026, 11, 24)),
        ("05-01-26", date(2026, 1, 5)),          # day first, Indian format
        ("2026-11-27", date(2026, 11, 27)),
        ("24-Dec-2026", date(2026, 12, 24)),
        ("3rd March, 2026", date(2026, 3, 3)),
        ("Nov 27, 2026", date(2026, 11, 27)),
        ("31/02/2026", None),
    ])
    def test_dates(self, text, expected):
        assert parse_date(text) == expected

    def test_gstin_checksum(self):
        assert gstin_is_valid("27AAPFU0939F1ZV")      # published example GSTIN
        assert not gstin_is_valid("27AAPFU0939F1ZW")  # one character off
        assert not gstin_is_valid("27AAPFU0939F1Z")   # too short
        first14 = "07ABCPS4821K1Z"
        assert gstin_is_valid(first14 + gstin_check_char(first14))

    def test_ocr_confusions_are_fixed_by_position(self):
        # O read instead of 0 in the state code, 5 instead of S in the PAN
        assert clean_gstin("O7ABCP54821K1ZH") == "07ABCPS4821K1ZH"
        assert find_gstins("GSTIN: O7ABCP54821K1ZH") == ["07ABCPS4821K1ZH"]


# Real PaddleOCR output for the five sample invoices (samples/make_invoices.py)
EXPECTED = {
    "01-approved-office-supplies": {
        "invoice_number": "SS/2026/1187", "invoice_date": "2026-11-24", "due_date": "2026-12-24",
        "po_number": "PO-55102", "vendor_name": "Sharma Stationers", "vendor_gstin": "07ABCPS4821K1ZH",
        "buyer_gstin": "09AAACA1234F1Z8", "subtotal": "18850.00", "tax_amount": "2262.00",
        "total_amount": "21112.00", "currency": "INR",
    },
    "02-review-high-value": {
        "invoice_number": "NW-INV-24090", "vendor_name": "Northwind IT Solutions",
        "subtotal": "581400.00", "tax_amount": "104652.00", "total_amount": "686052.00",
    },
    "03-reject-invalid-gstin": {"vendor_gstin": "07AAGFQ5521M1ZX", "total_amount": "35990.00"},
    "04-review-unknown-vendor-scan": {
        "invoice_number": "GPC/26-27/0412", "vendor_name": "Ganesh Packaging Co",
        "tax_amount": "6883.20", "total_amount": "45123.20",
    },
    "05-review-totals-mismatch": {"subtotal": "9500.00", "tax_amount": "1140.00", "total_amount": "11250.00"},
}


@pytest.mark.parametrize("name", EXPECTED)
def test_invoice_extraction_on_real_paddleocr_output(name, ocr_fixture):
    fields = get_extractor("invoice").extract(ocr_fixture(name))
    for key, value in EXPECTED[name].items():
        assert key in fields, f"{key} not extracted"
        assert fields[key].value == value, f"{key}: {fields[key].value!r} != {value!r}"
        assert 0 < fields[key].confidence <= 1


def test_vendor_and_buyer_gstin_are_not_swapped(ocr_fixture):
    fields = get_extractor("invoice").extract(ocr_fixture("01-approved-office-supplies"))
    assert fields["vendor_gstin"].value.startswith("07")   # Delhi seller
    assert fields["buyer_gstin"].value.startswith("09")    # Uttar Pradesh buyer


def test_extracted_boxes_point_at_the_value(ocr_fixture):
    fields = get_extractor("invoice").extract(ocr_fixture("01-approved-office-supplies"))
    x0, y0, x1, y1 = fields["total_amount"].bbox
    assert x1 > x0 and y1 > y0 and fields["total_amount"].page == 1


def _ocr(*rows):
    """Build an OcrResult from (text, x0, y0) rows."""
    lines = [OcrLine(t, 0.99, (x, y, x + 12 * len(t), y + 24)) for t, x, y in rows]
    return OcrResult("test", [OcrPage(1, 1200, 1600, lines)])


def test_label_value_stacked_vertically():
    ocr = _ocr(("Invoice Number", 100, 100), ("INV-778", 100, 132), ("Grand Total", 700, 900),
               ("45,000.00", 700, 932))
    f = get_extractor("invoice").extract(ocr)
    assert f["invoice_number"].value == "INV-778" and f["invoice_number"].method == "below"
    assert Decimal(f["total_amount"].value) == Decimal("45000.00")


def test_grand_total_prefers_largest_total_line():
    ocr = _ocr(("Sub Total", 700, 800), ("50,000.00", 1000, 800), ("IGST @ 18%", 700, 840),
               ("9,000.00", 1000, 840), ("Total", 700, 880), ("59,000.00", 1000, 880),
               ("Total Qty", 100, 700), ("12", 300, 700))
    f = get_extractor("invoice").extract(ocr)
    assert f["subtotal"].value == "50000.00"
    assert f["tax_amount"].value == "9000.00"
    assert f["total_amount"].value == "59000.00"


def test_missing_fields_are_simply_absent():
    f = get_extractor("invoice").extract(_ocr(("Hello world", 10, 10)))
    assert "total_amount" not in f and "invoice_number" not in f

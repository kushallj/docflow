"""Parsers for values found on Indian business documents."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
# Amounts
# --------------------------------------------------------------------------- #

# 1,23,456.78 (Indian grouping), 123,456.78, 123456.78, 1.234,56 is not supported on purpose
AMOUNT_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\w])")
CURRENCY_RE = re.compile(r"(₹|rs\.?|inr|usd|\$|eur|€)", re.IGNORECASE)


def parse_amount(text: str) -> Decimal | None:
    """Return the last amount in the text (labels come first, amounts last)."""
    if not text:
        return None
    cleaned = text.replace("O", "0") if re.fullmatch(r"[\d,.O]+", text.strip()) else text
    matches = AMOUNT_RE.findall(cleaned.replace(" ", "") if re.search(r"\d\s+,|,\s+\d", cleaned) else cleaned)
    if not matches:
        return None
    try:
        return Decimal(matches[-1].replace(",", ""))
    except InvalidOperation:
        return None


def detect_currency(text: str) -> str | None:
    m = CURRENCY_RE.search(text or "")
    if not m:
        return None
    token = m.group(1).lower().rstrip(".")
    return {"₹": "INR", "rs": "INR", "inr": "INR", "usd": "USD", "$": "USD", "eur": "EUR", "€": "EUR"}[token]


# --------------------------------------------------------------------------- #
# Dates (Indian documents are day-first)
# --------------------------------------------------------------------------- #

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

DATE_PATTERNS = [
    # 2026-11-27
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    # 27/11/2026, 27-11-26, 27.11.2026
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b"), "dmy"),
    # 27 Nov 2026, 27-Nov-2026, 27th November, 2026
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([A-Za-z]{3,9})[\s\-/,]+(\d{2,4})\b"), "dMy"),
    # Nov 27, 2026
    (re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"), "Mdy"),
]


def _year(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


def parse_date(text: str) -> date | None:
    if not text:
        return None
    for pattern, kind in DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                if kind == "ymd":
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if kind == "dmy":
                    return date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
                if kind == "dMy":
                    month = MONTHS.get(m.group(2)[:3].lower())
                    if month:
                        return date(_year(m.group(3)), month, int(m.group(1)))
                if kind == "Mdy":
                    month = MONTHS.get(m.group(1)[:3].lower())
                    if month:
                        return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- #
# GSTIN (Indian GST registration number)
# --------------------------------------------------------------------------- #

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_SHAPE = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
# loose pattern used to find candidates in OCR text before validation
GSTIN_CANDIDATE = re.compile(r"\b[0-9O]{2}[A-Z0-9]{10}[0-9A-Z]{3}\b")


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str | None) -> bool:
    """Shape check plus the official mod-36 checksum on the 15th character."""
    if not gstin:
        return False
    g = gstin.strip().upper()
    if not GSTIN_SHAPE.fullmatch(g):
        return False
    return gstin_check_char(g[:14]) == g[14]


def clean_gstin(raw: str) -> str:
    """Fix the OCR confusions that are unambiguous given GSTIN positions."""
    g = re.sub(r"[^0-9A-Z]", "", raw.upper())
    if len(g) != 15:
        return g
    chars = list(g)
    digit_pos = {0, 1, 7, 8, 9, 10}
    alpha_pos = {2, 3, 4, 5, 6, 11}
    to_digit = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"}
    to_alpha = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z"}
    for i in digit_pos:
        chars[i] = to_digit.get(chars[i], chars[i])
    for i in alpha_pos:
        chars[i] = to_alpha.get(chars[i], chars[i])
    if chars[13] == "2":
        chars[13] = "Z"
    return "".join(chars)


def find_gstins(text: str) -> list[str]:
    found = []
    for m in GSTIN_CANDIDATE.finditer(re.sub(r"(?<=[0-9A-Z])\s(?=[0-9A-Z])", "", text.upper())):
        g = clean_gstin(m.group(0))
        if GSTIN_SHAPE.fullmatch(g) and g not in found:
            found.append(g)
    return found


def as_iso(d: date | datetime | None) -> str | None:
    return d.isoformat() if d else None

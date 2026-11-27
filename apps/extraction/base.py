"""Layout-aware helpers shared by document extractors."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from apps.ocr.base import OcrLine, OcrResult


@dataclass
class Extracted:
    key: str
    value: Any                       # JSON-serialisable normalised value
    raw_text: str
    confidence: float                # 0.0 – 1.0, OCR confidence x how the value was found
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    method: str = ""                 # "same-line" | "right" | "below" | "heuristic" | "derived"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "raw_text": self.raw_text,
            "confidence": round(self.confidence, 3),
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "method": self.method,
        }


# How much we trust a value depending on where it sat relative to its label.
POSITION_WEIGHT = {"same-line": 1.0, "right": 0.95, "below": 0.85}


def reading_order(lines: Iterable[OcrLine]) -> list[OcrLine]:
    return sorted(lines, key=lambda ln: (ln.page, round(ln.cy / 12), ln.x0))


def same_row(a: OcrLine, b: OcrLine) -> bool:
    """True if the two lines overlap vertically by at least half the smaller height."""
    if a.page != b.page:
        return False
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    return overlap >= 0.5 * min(a.height, b.height)


def x_overlap(a: OcrLine, b: OcrLine, slack: float = 40) -> bool:
    return min(a.x1, b.x1) + slack >= max(a.x0, b.x0)


class Locator:
    """Find the value that belongs to a label, using the label's position on the page."""

    def __init__(self, ocr: OcrResult):
        self.lines = reading_order(ocr.lines)

    def labelled(
        self,
        key: str,
        label: re.Pattern,
        parse: Callable[[str], Any],
        exclude: re.Pattern | None = None,
        lines: list[OcrLine] | None = None,
    ) -> list[Extracted]:
        """Every (label, value) pair found, best first."""
        found: list[Extracted] = []
        for ln in lines or self.lines:
            m = label.search(ln.text)
            if not m or (exclude and exclude.search(ln.text)):
                continue
            # 1. value after the label on the same OCR line: "Invoice No: INV-42"
            tail = ln.text[m.end():].lstrip(" :#-.\t")
            if tail and (v := parse(tail)) is not None:
                found.append(self._make(key, v, tail, ln, "same-line"))
                continue
            # 2. nearest line to the right on the same row (two-column tables)
            right = sorted((o for o in self.lines if o is not ln and same_row(ln, o) and o.x0 >= ln.x1 - 5),
                           key=lambda o: o.x0 - ln.x1)
            hit = next(((o, v) for o in right if (v := parse(o.text)) is not None), None)
            if hit:
                found.append(self._make(key, hit[1], hit[0].text, hit[0], "right"))
                continue
            # 3. the line directly below the label (stacked label / value)
            below = sorted((o for o in self.lines if o.page == ln.page and o.y0 >= ln.y1 - 2
                            and o.y0 - ln.y1 < 2.5 * max(ln.height, 1) and x_overlap(ln, o)),
                           key=lambda o: o.y0)
            hit = next(((o, v) for o in below[:2] if (v := parse(o.text)) is not None), None)
            if hit:
                found.append(self._make(key, hit[1], hit[0].text, hit[0], "below"))
        return found

    @staticmethod
    def _make(key: str, value: Any, raw: str, ln: OcrLine, method: str) -> Extracted:
        return Extracted(key, value, raw.strip(), ln.confidence * POSITION_WEIGHT[method], ln.page, ln.bbox, method)


class Extractor:
    """Base class: subclasses declare FIELDS and implement extract()."""

    slug: str = ""
    name: str = ""
    # (key, label, type, required) – types match the rule engine fact types
    FIELDS: list[tuple[str, str, str, bool]] = []

    def extract(self, ocr: OcrResult) -> dict[str, Extracted]:
        raise NotImplementedError

    @classmethod
    def field_schema(cls) -> list[dict]:
        return [{"key": k, "label": label, "type": t, "required": r} for k, label, t, r in cls.FIELDS]

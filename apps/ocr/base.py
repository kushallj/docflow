"""Engine-independent OCR data types.

Every engine returns the same structure, so extraction and the review UI never
depend on which OCR library produced the text.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class OcrLine:
    text: str
    confidence: float                       # 0.0 – 1.0
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in page pixels
    page: int = 1

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


@dataclass
class OcrPage:
    number: int
    width: int
    height: int
    lines: list[OcrLine] = field(default_factory=list)
    image_path: str | None = None           # rendered page image, relative to MEDIA_ROOT


@dataclass
class OcrResult:
    engine: str
    pages: list[OcrPage]
    seconds: float = 0.0

    @property
    def lines(self) -> list[OcrLine]:
        return [ln for p in self.pages for ln in p.lines]

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def mean_confidence(self) -> float:
        lines = self.lines
        return sum(ln.confidence for ln in lines) / len(lines) if lines else 0.0

    def to_json(self) -> dict:
        return {
            "engine": self.engine,
            "seconds": round(self.seconds, 3),
            "pages": [
                {
                    "number": p.number,
                    "width": p.width,
                    "height": p.height,
                    "image_path": p.image_path,
                    "lines": [asdict(ln) for ln in p.lines],
                }
                for p in self.pages
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "OcrResult":
        pages = [
            OcrPage(
                number=p["number"],
                width=p["width"],
                height=p["height"],
                image_path=p.get("image_path"),
                lines=[OcrLine(ln["text"], ln["confidence"], tuple(ln["bbox"]), ln.get("page", p["number"]))
                       for ln in p["lines"]],
            )
            for p in data["pages"]
        ]
        return cls(engine=data["engine"], pages=pages, seconds=data.get("seconds", 0.0))


class OcrEngine(Protocol):
    name: str

    def run(self, image_paths: list[Path]) -> list[OcrPage]:
        """OCR one image per page and return the pages in order."""
        ...

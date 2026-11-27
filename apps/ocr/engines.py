"""OCR engines.

PaddleOCREngine is the production engine. TextEngine reads a ``.txt`` sidecar
file instead of running a model; it keeps the test suite fast and lets the
pipeline run on machines without PaddlePaddle installed.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from PIL import Image

from .base import OcrLine, OcrPage

log = logging.getLogger(__name__)


class PaddleOCREngine:
    """PaddleOCR 3.x (PP-OCRv5) text detection + recognition.

    The model is loaded once per process and reused, because loading takes
    several seconds and Celery workers process many documents.
    """

    name = "paddleocr"
    _instance = None
    _lock = threading.Lock()

    def __init__(self, lang: str = "en", det_model: str = "PP-OCRv5_mobile_det", rec_model: str = ""):
        self.lang, self.det_model, self.rec_model = lang, det_model, rec_model

    def _model(self):
        with PaddleOCREngine._lock:
            if PaddleOCREngine._instance is None:
                from paddleocr import PaddleOCR  # heavy import, only when actually used

                log.info("Loading PaddleOCR (lang=%s, det=%s)", self.lang, self.det_model)
                kwargs = {"text_detection_model_name": self.det_model} if self.det_model else {}
                if self.rec_model:
                    kwargs["text_recognition_model_name"] = self.rec_model
                PaddleOCREngine._instance = PaddleOCR(
                    lang=self.lang,
                    **kwargs,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                )
            return PaddleOCREngine._instance

    def run(self, image_paths: list[Path]) -> list[OcrPage]:
        model = self._model()
        pages: list[OcrPage] = []
        for number, path in enumerate(image_paths, start=1):
            with Image.open(path) as im:
                width, height = im.size
            lines: list[OcrLine] = []
            for res in model.predict(str(path)):
                data = res.json.get("res", res.json) if hasattr(res, "json") else res
                texts = data.get("rec_texts", [])
                scores = data.get("rec_scores", [])
                boxes = data.get("rec_boxes", [])
                for text, score, box in zip(texts, scores, boxes):
                    text = str(text).strip()
                    if not text:
                        continue
                    x0, y0, x1, y1 = (float(v) for v in list(box)[:4])
                    lines.append(OcrLine(text=text, confidence=float(score), bbox=(x0, y0, x1, y1), page=number))
            pages.append(OcrPage(number=number, width=width, height=height, lines=lines))
        return pages


class TextEngine:
    """Reads ``<image>.txt`` next to each page image: one OCR line per text line.

    Bounding boxes are synthesised from line order. Optional per-line
    confidence can be given as a ``@@0.42`` suffix.
    """

    name = "text"
    LINE_HEIGHT = 30

    def run(self, image_paths: list[Path]) -> list[OcrPage]:
        pages = []
        for number, path in enumerate(image_paths, start=1):
            sidecar = Path(path).with_suffix(".txt")
            raw = sidecar.read_text(encoding="utf-8") if sidecar.exists() else ""
            lines = []
            for i, row in enumerate(raw.splitlines()):
                if not row.strip():
                    continue
                conf = 0.99
                if "@@" in row:
                    row, _, c = row.rpartition("@@")
                    conf = float(c)
                y = 20 + i * self.LINE_HEIGHT
                lines.append(OcrLine(row.strip(), conf, (20.0, float(y), 20.0 + 9 * len(row), float(y + 22)), number))
            pages.append(OcrPage(number=number, width=1000, height=40 + self.LINE_HEIGHT * max(len(lines), 1), lines=lines))
        return pages


_ENGINES = {"paddleocr": PaddleOCREngine, "text": TextEngine}


def get_engine(name: str, **kwargs):
    if name == "paddleocr" and not kwargs:
        from django.conf import settings

        kwargs = {"lang": settings.OCR_LANG, "det_model": settings.OCR_DET_MODEL, "rec_model": settings.OCR_REC_MODEL}
    try:
        return _ENGINES[name](**kwargs)
    except KeyError as exc:
        raise ValueError(f"Unknown OCR engine '{name}'. Choose from: {', '.join(_ENGINES)}") from exc

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.OCR_ENGINE = "text"            # fast, no model needed
    settings.CELERY_TASK_ALWAYS_EAGER = True


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    """The fixture invoices are dated around 27 Nov 2026; keep due-date maths stable."""
    from datetime import date

    monkeypatch.setattr("apps.rules.services._today", lambda: date(2026, 11, 27))


@pytest.fixture
def ocr_fixture():
    from apps.ocr.base import OcrResult

    def load(name: str) -> OcrResult:
        return OcrResult.from_json(json.loads((FIXTURES / "ocr" / f"{name}.json").read_text()))

    return load

"""Turn an uploaded file (image or PDF) into page images and run OCR on them."""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageOps

from .base import OcrResult
from .engines import get_engine

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PDF_DPI = 200
MAX_PAGES = 20
MAX_SIDE = 2400   # downscale huge photos; OCR accuracy does not improve beyond this


class UnsupportedFile(ValueError):
    pass


def render_pages(source: Path, out_dir: Path) -> list[Path]:
    """Write one normalised PNG per page into out_dir and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = source.suffix.lower()
    images: list[Image.Image] = []

    if ext == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(source))
        try:
            if len(pdf) > MAX_PAGES:
                raise UnsupportedFile(f"PDF has {len(pdf)} pages; the limit is {MAX_PAGES}")
            for page in pdf:
                images.append(page.render(scale=PDF_DPI / 72).to_pil())
        finally:
            pdf.close()
    elif ext in IMAGE_EXTS:
        with Image.open(source) as im:
            frames = getattr(im, "n_frames", 1)
            if frames > MAX_PAGES:
                raise UnsupportedFile(f"Image has {frames} frames; the limit is {MAX_PAGES}")
            for i in range(frames):
                im.seek(i)
                images.append(ImageOps.exif_transpose(im.copy()))
    else:
        raise UnsupportedFile(f"Unsupported file type '{ext}'. Upload a PDF or an image.")

    paths = []
    for n, img in enumerate(images, start=1):
        img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE))
        p = out_dir / f"page-{n}.png"
        img.save(p)
        paths.append(p)
    return paths


def run_ocr(source: Path, pages_dir: Path, engine_name: str, media_root: Path | None = None) -> OcrResult:
    start = time.perf_counter()
    page_paths = render_pages(source, pages_dir)
    engine = get_engine(engine_name)
    pages = engine.run(page_paths)
    for page, path in zip(pages, page_paths):
        page.image_path = str(path.relative_to(media_root)).replace("\\", "/") if media_root else str(path)
    return OcrResult(engine=engine.name, pages=pages, seconds=time.perf_counter() - start)

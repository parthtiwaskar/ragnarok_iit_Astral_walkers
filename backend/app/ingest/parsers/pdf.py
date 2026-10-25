"""PDF text extraction: PyMuPDF first, pdfplumber as fallback."""

from __future__ import annotations

import io

from app.ingest.parsers import Extracted, ExtractError
from app.ingest.text import normalize


def extract_pdf(data: bytes) -> Extracted:
    errors: list[str] = []
    opened = False
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            opened = True
            pages = [page.get_text("text", sort=True) for page in doc]
        text = normalize("\n".join(pages))
        if text:
            return Extracted(text=text, fmt="pdf")
        errors.append("pymupdf: no text")
    except Exception as exc:  # pymupdf.FileDataError, RuntimeError, ...
        errors.append(f"pymupdf: {type(exc).__name__}: {exc}")
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            opened = True
            pages = [(p.extract_text() or "") for p in pdf.pages]
        text = normalize("\n".join(pages))
        if text:
            return Extracted(text=text, fmt="pdf", notes=["pdfplumber fallback", *errors])
        errors.append("pdfplumber: no text")
    except Exception as exc:
        errors.append(f"pdfplumber: {type(exc).__name__}: {exc}")
    if opened:
        # a structurally valid PDF with no extractable text: the EMPTY damage check handles it
        return Extracted(text="", fmt="pdf", notes=errors)
    raise ExtractError("CORRUPT_PDF", "; ".join(errors)[:300])

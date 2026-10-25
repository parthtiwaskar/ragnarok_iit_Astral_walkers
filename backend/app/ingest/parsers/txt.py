"""TXT extraction: read bytes, detect encoding with charset-normalizer, repair with ftfy."""

from __future__ import annotations

from app.ingest.parsers import Extracted
from app.ingest.text import mojibake_score, normalize


def extract_txt(data: bytes, keep_spacing: bool = False) -> Extracted:
    repaired = False
    notes: list[str] = []
    text: str | None = None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(data).best()
            if best is not None:
                text = str(best)
                repaired = True
                notes.append(f"decoded as {best.encoding}")
        except Exception as exc:  # pragma: no cover - charset-normalizer is robust
            notes.append(f"charset-normalizer failed: {exc}")
        if text is None:
            text = data.decode("latin-1", errors="replace")
            repaired = True
            notes.append("latin-1 fallback")
    if text.startswith("﻿"):
        text = text[1:]
    try:
        import ftfy

        fixed = ftfy.fix_text(text)
        if fixed != text and mojibake_score(text) > 0:
            repaired = True
            notes.append("ftfy repaired mojibake")
        text = fixed
    except Exception as exc:  # pragma: no cover
        notes.append(f"ftfy failed: {exc}")
    return Extracted(
        text=normalize(text, keep_spacing=keep_spacing), fmt="txt", repaired=repaired, notes=notes
    )

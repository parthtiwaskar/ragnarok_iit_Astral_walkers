"""TXT extraction: read bytes, detect encoding with charset-normalizer, repair with ftfy."""

from __future__ import annotations

from app.ingest.parsers import Extracted
from app.ingest.sniff import utf16_split
from app.ingest.text import mojibake_score, normalize


def _split_mixed_encoding(data: bytes) -> tuple[str, str] | None:
    """A UTF-8 prefix followed by a UTF-16 run partway in: decode each half separately.

    Decoding the whole file either way loses one half, so split at the boundary `utf16_split`
    found. Falls back to the normal path if either half does not decode.
    """
    found = utf16_split(data)
    if found is None:
        return None
    at, encoding = found
    try:
        head = data[:at].decode("utf-8")
        tail = data[at + 2 :].decode(encoding)
    except UnicodeDecodeError:
        return None
    return head + tail, f"MIXED_ENCODING: {encoding} body after a utf-8 header"


def extract_txt(data: bytes, keep_spacing: bool = False) -> Extracted:
    repaired = False
    notes: list[str] = []
    text: str | None = None
    mixed = _split_mixed_encoding(data) if b"\x00" in data else None
    if mixed is not None:
        text, note = mixed
        repaired = True
        notes.append(note)
    try:
        text = data.decode("utf-8") if text is None else text
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

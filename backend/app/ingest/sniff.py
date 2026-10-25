"""File-type sniffing by magic bytes, never by extension alone (DATA_PIPELINE.md §4)."""

from __future__ import annotations

from typing import Literal

FileKind = Literal["pdf", "docx", "txt", "zip", "empty", "unsupported"]

UTF16_BOMS = {b"\xff\xfe": "utf-16-le", b"\xfe\xff": "utf-16-be"}


def sniff(data: bytes) -> FileKind:
    if len(data) == 0:
        return "empty"
    head = data[:2048]
    if head.lstrip()[:4] == b"%PDF":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        # DOCX is a zip whose members live under word/. A truncated zip may still show the name in raw bytes.
        if b"word/" in data[:65536] or b"[Content_Types].xml" in data[:65536]:
            return "docx"
        return "zip"
    if _looks_like_text(head):
        return "txt"
    return "unsupported"


def utf16_split(data: bytes) -> tuple[int, str] | None:
    """Offset and encoding of a UTF-16 run that starts partway into `data`, if there is one.

    Some packs glue a plain-text metadata header to a UTF-16 body. A BOM alone is not enough
    evidence — arbitrary binary contains the byte pairs — so the bytes after it must also be
    shaped like UTF-16: every other one a NUL, for text in the Latin range.
    """
    for bom, encoding in UTF16_BOMS.items():
        at = data.find(bom)
        if at <= 0:
            continue
        sample = data[at + 2 : at + 2 + 64]
        if len(sample) >= 8 and sample.count(0) >= len(sample) * 0.4:
            return at, encoding
    return None


def _looks_like_text(head: bytes) -> bool:
    threshold = 0.85
    if b"\x00" in head:
        # UTF-16 text has NULs. A verified UTF-16 run partway in is repaired by txt extraction.
        # Without one the NULs could still be UTF-16 with no BOM, so strip them and retest —
        # but at a bar arbitrary binary cannot clear.
        if head[:2] in UTF16_BOMS or utf16_split(head) is not None:
            return True
        head = head.replace(b"\x00", b"")
        if not head:
            return False
        threshold = 0.97
    printable = sum(1 for b in head if b >= 32 or b in (9, 10, 13))
    return printable / max(1, len(head)) > threshold

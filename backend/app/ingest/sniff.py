"""File-type sniffing by magic bytes, never by extension alone (DATA_PIPELINE.md §4)."""

from __future__ import annotations

from typing import Literal

FileKind = Literal["pdf", "docx", "txt", "zip", "empty", "unsupported"]


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


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        # UTF-16 text has NULs; accept it only with a BOM
        return head[:2] in (b"\xff\xfe", b"\xfe\xff")
    printable = sum(1 for b in head if b >= 32 or b in (9, 10, 13))
    return printable / max(1, len(head)) > 0.85

"""Text normalisation shared by all extractors."""

from __future__ import annotations

import re
import unicodedata

_SPACES = re.compile(r"[ \t  -​]+")
_MANY_BLANK = re.compile(r"\n{3,}")


def normalize(text: str, keep_spacing: bool = False) -> str:
    """NFC, unify newlines, collapse runs of spaces within lines (line structure is kept)."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if keep_spacing:
        lines = [ln.rstrip() for ln in text.split("\n")]
    else:
        lines = [_SPACES.sub(" ", ln).strip() for ln in text.split("\n")]
    out = "\n".join(lines)
    return _MANY_BLANK.sub("\n\n", out).strip()


def replacement_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = text.count("�")
    return bad / max(1, len(text))


_MOJIBAKE = re.compile(r"[ÃÂ][\u0080-¿]|â€|Ã¸|Ã©|Ã¥")


def mojibake_score(text: str) -> float:
    if not text:
        return 0.0
    return len(_MOJIBAKE.findall(text)) / max(1, len(text) / 100)


def non_ws_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def norm_for_match(text: str) -> str:
    """Normalisation used for quote matching: lowercase, unify quotes/dashes, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    return re.sub(r"\s+", " ", text).strip()

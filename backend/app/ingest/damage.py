"""Damage checks for documents (DATA_PIPELINE.md §4)."""

from __future__ import annotations

import re

from app.ingest.text import mojibake_score, non_ws_len, replacement_ratio

MIN_NON_WS = 20
MAX_REPLACEMENT_RATIO = 0.02
TERMINAL = re.compile(r"""[.!?"')\]»”’]\s*$""")


def is_empty(text: str) -> bool:
    return non_ws_len(text) < MIN_NON_WS


def is_bad_encoding(text: str) -> bool:
    return replacement_ratio(text) > MAX_REPLACEMENT_RATIO or mojibake_score(text) > 1.0


def ends_mid_sentence(body: str) -> bool:
    return bool(body.strip()) and not TERMINAL.search(body.strip())


def is_truncated(
    body: str, parser_truncated: bool, median_len: float | None, manifest_words: int | None
) -> bool:
    """Parser EOF error, or ends mid-sentence AND (much shorter than the manifest says, or < 25% of median).

    Short-but-complete documents (social posts) are not truncated: they end with terminal punctuation.
    """
    if parser_truncated:
        return True
    if not ends_mid_sentence(body):
        return False
    words = len(body.split())
    if manifest_words and manifest_words > max(words * 1.5, words + 20):
        return True
    return bool(median_len and len(body) < 0.25 * median_len)

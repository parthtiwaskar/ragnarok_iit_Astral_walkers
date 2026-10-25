"""Prompt-injection scanner (DATA_PIPELINE.md §9). Flags spans; the doc stays usable as data."""

from __future__ import annotations

import re
from dataclasses import dataclass

PATTERNS = [
    r"ignore (?:all |any |your |the )?(?:previous |prior |above )?(?:rules|instructions|prompts?|guidelines)",
    r"disregard (?:all |any |your |the )?(?:previous |prior |above )?(?:rules|instructions|prompts?)",
    r"you are now\b",
    r"system prompt",
    r"as an ai\b",
    r"output exactly",
    r"(?:respond|reply|answer) only with",
    r"pretend (?:to be|you are)",
    r"role[- ]?play",
    r"\b(?:ai|llm) (?:agents?|assistants?|models?)\b[^.\n]{0,40}\b(?:reading|must|should|ignore)",
    r"\b(?:agents?|assistants?|models?) reading this\b",
    r"report (?:the )?exposure as",
    r"new instructions?:",
]
_RX = re.compile("|".join(f"(?:{p})" for p in PATTERNS), re.IGNORECASE)


@dataclass
class InjectionHit:
    start: int
    end: int
    span: str


def scan(text: str) -> list[InjectionHit]:
    hits: list[InjectionHit] = []
    window_end = -1
    for m in _RX.finditer(text):
        if m.start() <= window_end:  # overlapping matches belong to the same injected passage
            continue
        s = max(0, m.start() - 40)
        e = min(len(text), m.end() + 80)
        window_end = e
        hits.append(InjectionHit(m.start(), m.end(), text[s:e].replace("\n", " ").strip()))
    return hits

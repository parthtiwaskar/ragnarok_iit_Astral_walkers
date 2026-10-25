"""Format extractors. Each returns `Extracted` or raises `ExtractError` with a quarantine code."""

from __future__ import annotations

from dataclasses import dataclass, field


class ExtractError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Extracted:
    text: str
    fmt: str
    repaired: bool = False
    truncated: bool = False  # the parser hit EOF / a broken structure but recovered some text
    notes: list[str] = field(default_factory=list)

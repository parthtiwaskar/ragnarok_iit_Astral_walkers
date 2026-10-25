"""Prompt files are loaded at runtime from `agents/prompts/*.md`; the hash of each rendered prompt is logged."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


@lru_cache(maxsize=64)
def load(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, values: dict[str, Any]) -> str:
    """Fill {UPPER_CASE} placeholders. Unknown ones become a neutral pointer, never raw braces."""

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            v = values[key]
            return v if isinstance(v, str) else str(v)
        return "(given in the turn message)"

    return _PLACEHOLDER.sub(sub, template)


def system_prompt(agent: str, values: dict[str, Any]) -> str:
    return render(load("shared") + "\n\n" + load(agent), {"AGENT_NAME": agent.capitalize(), **values})


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_hashes() -> dict[str, str]:
    return {p.stem: sha(p.read_text(encoding="utf-8")) for p in sorted(PROMPT_DIR.glob("*.md"))}

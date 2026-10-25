"""Tiny .env loader (stdlib). Existing environment variables win; the file is never printed or logged."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _value(raw: str) -> str:
    """The value part of KEY=VALUE, with the usual .env rules: a quoted value is taken literally (a # inside it stays),
    and in an unquoted value a # that starts the value or follows whitespace begins a comment."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    if raw.startswith("#"):
        return ""  # e.g. "NINE_REALMS_PROVIDER=   # optional ..." is empty, not the comment
    return _INLINE_COMMENT.sub("", raw).strip()


def load_env(path: str | Path | None = None) -> list[str]:
    """Load KEY=VALUE lines from .env into os.environ. Returns the names that were set (never the values)."""
    p = Path(path) if path else ROOT / ".env"
    if not p.is_file():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key, value = key.strip(), _value(raw)
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded

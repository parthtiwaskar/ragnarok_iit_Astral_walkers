"""The .env loader follows the usual rules, so a commented .env (like .env.example) configures the server correctly."""
from __future__ import annotations

import os

from nine_realms.env import load_env

KEYS = ("NR_A", "NR_B", "NR_C", "NR_D", "NR_E", "NR_F", "NR_G", "NR_URL", "NR_SET")


def test_inline_comments_quotes_and_existing_variables(tmp_path, monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NR_SET", "from-shell")
    f = tmp_path / ".env"
    f.write_text("\n".join([
        "# a full-line comment",
        "NR_A=anthropic   # use the Anthropic key",
        "NR_B=1.00 # hard cap",
        "NR_C=               # optional: force deepseek | anthropic",
        'NR_D="value # kept inside quotes"',
        "NR_E='single quoted'",
        "NR_F=plain",
        "NR_G=sk-abc#def",                       # a # with no space before it is part of the value
        "NR_URL=https://openrouter.ai/api/v1",
        "NR_SET=from-file",
        "not a setting",
    ]), encoding="utf-8")
    loaded = load_env(f)
    assert os.environ["NR_A"] == "anthropic" and os.environ["NR_B"] == "1.00"
    assert "NR_C" not in os.environ and "NR_C" not in loaded    # an empty value with a comment stays unset
    assert os.environ["NR_D"] == "value # kept inside quotes" and os.environ["NR_E"] == "single quoted"
    assert os.environ["NR_F"] == "plain" and os.environ["NR_G"] == "sk-abc#def"
    assert os.environ["NR_URL"] == "https://openrouter.ai/api/v1"
    assert os.environ["NR_SET"] == "from-shell"                  # the shell wins over the file

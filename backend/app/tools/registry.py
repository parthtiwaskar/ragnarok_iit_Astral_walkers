"""Tool registry: pydantic arg models -> JSON schemas for both providers, and the per-agent allowlist."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.llm.client import ToolSpec

ToolFn = Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]


@dataclass
class ToolDef:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn | None = None  # None for terminal submit tools (handled by the agent)
    terminal: bool = False
    dedupe: bool = True
    canonical: Callable[[BaseModel], Any] | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=json_schema(self.args_model))


# Tool allowlist matrix (AGENT_SPECS.md §0). Enforced by the guard chain, not just the prompt.
AGENT_TOOLS: dict[str, frozenset[str]] = {
    "heimdall": frozenset(
        {
            "search_documents",
            "read_document",
            "list_events",
            "event_profile",
            "find_related_docs",
            "list_trade_tables",
            "read_trade_table",
            "trade_stats",
            "get_reference",
            "verify_citation",
            "flag_injection",
            "flag_data_issue",
            "memory_recall",
            "memory_note",
            "submit_case_file",
            "submit_lessons",
        }
    ),
    "thor": frozenset(
        {
            "search_documents",
            "read_document",
            "find_related_docs",
            "list_trade_tables",
            "read_trade_table",
            "trade_stats",
            "get_reference",
            "compute_exposure",
            "verify_citation",
            "flag_injection",
            "flag_data_issue",
            "memory_recall",
            "memory_note",
            "submit_claims",
            "submit_attacks",
            "submit_responses",
            "submit_lessons",
        }
    ),
    "loki": frozenset(
        {
            "search_documents",
            "read_document",
            "find_related_docs",
            "list_trade_tables",
            "read_trade_table",
            "trade_stats",
            "get_reference",
            "compute_exposure",
            "verify_citation",
            "flag_injection",
            "flag_data_issue",
            "memory_recall",
            "memory_note",
            "submit_claims",
            "submit_attacks",
            "submit_responses",
            "submit_lessons",
        }
    ),
    # Odin: no search, no tool that addresses another agent.
    "odin": frozenset(
        {
            "read_document",
            "find_related_docs",
            "list_trade_tables",
            "read_trade_table",
            "trade_stats",
            "get_reference",
            "compute_exposure",
            "verify_citation",
            "flag_injection",
            "flag_data_issue",
            "memory_recall",
            "memory_note",
            "submit_rulings",
            "submit_sufficiency",
            "submit_briefing",
            "submit_lessons",
        }
    ),
}

# Terminal tool(s) per (agent, phase). A turn ends when the last one listed is accepted.
TERMINAL_BY_PHASE: dict[tuple[str, str], tuple[str, ...]] = {
    ("heimdall", "GATHERING"): ("submit_case_file",),
    ("heimdall", "RESEARCH"): ("submit_case_file",),
    ("thor", "R1_SUBMIT"): ("submit_claims",),
    ("loki", "R1_SUBMIT"): ("submit_claims",),
    ("thor", "R2_ATTACK"): ("submit_attacks",),
    ("loki", "R2_ATTACK"): ("submit_attacks",),
    ("thor", "R2_RESPOND"): ("submit_responses",),
    ("loki", "R2_RESPOND"): ("submit_responses",),
    ("odin", "JUDGING"): ("submit_rulings", "submit_sufficiency"),
    ("odin", "BRIEFING"): ("submit_briefing",),
    ("heimdall", "REFLECT"): ("submit_lessons",),
    ("thor", "REFLECT"): ("submit_lessons",),
    ("loki", "REFLECT"): ("submit_lessons",),
    ("odin", "REFLECT"): ("submit_lessons",),
}


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline $refs and drop titles: both providers accept the result."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                target = copy.deepcopy(defs.get(name, {}))
                merged = {**resolve(target), **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: resolve(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    out: dict[str, Any] = resolve(schema)
    out.setdefault("type", "object")
    return out

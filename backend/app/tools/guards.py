"""The guard chain every tool call passes through (SEARCH_AND_TOOLS.md §3).

allowlist -> budget -> clock -> dedupe -> execute (timeout/retry) -> post-process -> log -> error wrap.
Nothing here raises into the agent loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from app.tools.catalog import TOOLS
from app.tools.context import CaseContext, LedgerEntry, fingerprint
from app.tools.registry import AGENT_TOOLS
from app.trial.limits import TurnBudget

if TYPE_CHECKING:
    from app.memory.store import MemoryStore

TRANSIENT = (sqlite3.OperationalError, TimeoutError, OSError)


@dataclass
class CallCtx:
    agent: str
    phase: str
    run: int | None
    call_id: str
    fingerprint: str
    memory: MemoryStore | None
    recalled: set[str] = field(default_factory=set)


def _canonical(tool: str, args: BaseModel) -> Any:
    td = TOOLS[tool]
    if td.canonical is not None:
        return td.canonical(args)
    return args.model_dump(mode="json")


def _result_count(res: dict[str, Any]) -> int | None:
    for key in ("results", "rows", "tables", "candidates", "memories"):
        if isinstance(res.get(key), list):
            return len(res[key])
    return None


async def run_tool(
    ctx: CaseContext,
    agent: str,
    phase: str,
    run: int | None,
    turn: TurnBudget,
    name: str,
    raw_args: dict[str, Any],
    memory: MemoryStore | None,
    recalled: set[str],
) -> dict[str, Any]:
    t0 = time.monotonic()
    # 1. allowlist
    if name not in AGENT_TOOLS.get(agent, frozenset()) or name not in TOOLS:
        ctx.emit("tool_denied", "tool", {"agent": agent, "tool": name, "reason": "not in allowlist"})
        return {"error": f"tool '{name}' is not permitted for {agent}", "retryable": False}
    # 2. budget
    blocked = ctx.budget.can_call_tool(turn)
    if blocked:
        ctx.budget.record_limit(blocked)
        turn.limit_hit = blocked
        ctx.emit(
            "limit_hit",
            "thing",
            {
                "limit": blocked,
                "agent": agent,
                "phase": phase,
                "value": turn.tool_calls if "TURN" in blocked else ctx.budget.tool_calls,
                "action_taken": "tool call refused; the turn must submit now",
            },
        )
        return {
            "error": "limit_hit",
            "limit": blocked,
            "retryable": False,
            "message": "Tool budget exhausted. Submit your result now with the evidence you have.",
        }
    ctx.budget.charge_tool(turn)
    # parse args (invalid args still count against the budget, so loops terminate)
    td = TOOLS[name]
    try:
        args = td.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        err = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:6])
        ctx.emit(
            "tool_call",
            agent,
            {"tool": name, "agent": agent, "args": raw_args, "status": "invalid_args", "error": err},
        )
        return {"error": f"invalid arguments: {err}", "retryable": True}
    # 3. clock: as_of is injected from the case context; tools clamp dates themselves (never from args)
    # 4. dedupe
    canon = _canonical(name, args)
    fp = fingerprint(name, canon)
    prev = ctx.ledger.get(fp) if td.dedupe else None
    if prev is None and td.dedupe and fp in ctx.ledger.inflight:
        # the same call is running for another agent right now (Thor and Loki act in parallel): wait for it
        await ctx.ledger.inflight[fp].wait()
        prev = ctx.ledger.get(fp)
    if prev is not None:
        ctx.budget.duplicates_blocked += 1
        ctx.emit(
            "duplicate_blocked",
            "tool",
            {
                "fingerprint": fp,
                "original_call_id": prev.call_id,
                "tool": name,
                "agent": agent,
                "original_agent": prev.agent,
                "args": canon,
            },
        )
        return {
            "duplicate_of": prev.call_id,
            "result_ref": prev.call_id,
            "cached_result": prev.result,
            "note": "identical call already executed in this case; it was NOT run again. Use the cached "
            "result or change the query/filters.",
        }
    call_id = ctx.next_call_id()
    call = CallCtx(
        agent=agent, phase=phase, run=run, call_id=call_id, fingerprint=fp, memory=memory, recalled=recalled
    )
    done = asyncio.Event()
    if td.dedupe:
        ctx.ledger.inflight[fp] = done  # reserve the fingerprint before the first await
    # 5. execute with timeout + retry on transient errors
    result: dict[str, Any]
    attempts = 0
    while True:
        attempts += 1
        try:
            fn = td.fn
            assert fn is not None
            out = await asyncio.wait_for(
                asyncio.to_thread(fn, ctx, call, args), timeout=ctx.limits.TOOL_TIMEOUT_S
            )
            result = out if isinstance(out, dict) else {"result": out}
            break
        except TRANSIENT as exc:
            if attempts <= ctx.limits.TOOL_MAX_RETRIES:
                await asyncio.sleep(0.2 * attempts)
                continue
            result = {"error": f"{type(exc).__name__}: {exc}", "retryable": True}
            break
        except Exception as exc:  # 8. errors become structured results
            result = {"error": f"{type(exc).__name__}: {exc}", "retryable": False}
            break
    result["call_id"] = call_id
    latency = int((time.monotonic() - t0) * 1000)
    status = "error" if "error" in result else "ok"
    # 7. log
    seq = ctx.emit(
        "tool_call",
        agent,
        {
            "tool": name,
            "agent": agent,
            "args": canon,
            "fingerprint": fp,
            "call_id": call_id,
            "status": status,
            "error": result.get("error"),
            "result_count": _result_count(result),
            "attempts": attempts,
        },
        latency_ms=latency,
    )
    _log_retrieval(ctx, name, result, call_id, seq)
    # every executed call is in the ledger, failed ones too: an identical retry would fail the same way
    ctx.ledger.put(fp, LedgerEntry(call_id, agent, name, canon, result, run, phase))
    ctx.ledger.inflight.pop(fp, None)
    done.set()
    if status == "ok" and name == "search_documents" and phase == "R1_SUBMIT" and run is not None:
        ctx.sides_searched.setdefault(agent, set()).add(run)
    return result


def _log_retrieval(ctx: CaseContext, name: str, result: dict[str, Any], call_id: str, parent: int) -> None:
    items: list[dict[str, Any]] = []
    if name == "search_documents":
        for rank, r in enumerate(result.get("results", []), start=1):
            items.append(
                {
                    "doc_id": r["doc_id"],
                    "rank": rank,
                    "scores": r.get("scores"),
                    "tier": r.get("tier"),
                    "date": r.get("pub_date"),
                    "status": r.get("status"),
                    "event_id": r.get("event_id"),
                    "retracted": r.get("as_of_status", {}).get("retracted"),
                    "flags": r.get("flags"),
                }
            )
    elif name == "read_document" and "doc_id" in result and "document" in result:
        items.append(
            {
                "doc_id": result["doc_id"],
                "rank": 1,
                "tier": result.get("tier"),
                "date": result.get("pub_date"),
                "status": result.get("status"),
                "flags": result.get("flags"),
            }
        )
    elif name == "read_trade_table" and "rows" in result:
        items = [
            {
                "row_ref": f"{result['table_id']}#r{(r['row'] or 0):02d}",
                "month": r["month"],
                "quality": r["quality"],
            }
            for r in result["rows"]
        ]
    elif name == "list_trade_tables":
        items = [{"table_id": t["table_id"], "quality": t["quality"]} for t in result.get("tables", [])]
    if items:
        ctx.emit("retrieval", "tool", {"call_id": call_id, "tool": name, "items": items}, parent_seq=parent)

"""Event-agnostic rule-based policies for PolicyLLM (tests and offline demo only).

Every decision is derived from the rendered Case File view and tool results. There are no document ids,
event ids, names or numbers from any corpus in this file (invariant 16). Real runs use a real LLM.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from datetime import date, timedelta
from typing import Any

from app.llm.heuristics import clean, extract, param_keywords, params_in, sentences

Policy = Generator[list[tuple[str, dict[str, Any]]], Any, None]
PARAMS = ("route_share", "severity", "duration_months", "reroute_offset")
INCREASING = {"route_share", "severity", "duration_months"}  # higher value = higher exposure
STOP = set(
    [
        "how",
        "what",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "by",
        "with",
        "exposed",
        "exposure",
        "sector",
        "sectors",
        "country",
        "countries",
        "much",
        "will",
        "be",
        "it",
        "its",
        "as",
        "at",
        "from",
        "this",
        "that",
    ]
)


def one(name: str, args: dict[str, Any]) -> Generator[list[tuple[str, dict[str, Any]]], Any, dict[str, Any]]:
    res = yield [(name, args)]
    r = res[0] if isinstance(res, list) and res else (res or {})
    if isinstance(r, dict) and "cached_result" in r and isinstance(r["cached_result"], dict):
        return dict(r["cached_result"], duplicate_of=r.get("duplicate_of"))
    return r if isinstance(r, dict) else {}


def make_policy(agent: str, view: dict[str, Any], mems: list[dict[str, Any]], settings: Any) -> Policy:
    phase = view.get("phase")
    h = float(getattr(settings, "HORIZON_MONTHS", 3))
    if agent == "heimdall" and phase == "GATHERING":
        return heimdall_gather(view, settings)
    if agent == "heimdall" and phase == "RESEARCH":
        return heimdall_research(view)
    if agent in ("thor", "loki") and phase == "R1_SUBMIT":
        return side_submit(agent, view, mems, h)
    if agent in ("thor", "loki") and phase == "R2_ATTACK":
        return side_attack(agent, view, h)
    if agent in ("thor", "loki") and phase == "R2_RESPOND":
        return side_respond(agent, view, h)
    if agent == "odin" and phase == "JUDGING":
        return odin_judge(view)
    if agent == "odin" and phase == "BRIEFING":
        return odin_brief(view)
    if phase == "REFLECT":
        return reflect(agent, view)
    return _noop()


def _noop() -> Policy:
    if False:  # pragma: no cover
        yield []


def _as_of(view: dict[str, Any]) -> date:
    return date.fromisoformat(str(view.get("as_of"))[:10])


def _core_query(question: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z']+", question) if w.lower().strip("'s") not in STOP]
    return " ".join(w.replace("'s", "") for w in words)[:200]


# ======================================================================== Heimdall
def heimdall_gather(v: dict[str, Any], settings: Any) -> Policy:
    q = str(v.get("question", ""))
    as_of = _as_of(v)
    pool_size = int(v.get("pool_size", 20))
    ev = yield from one("list_events", {"query": q})
    cands = [c for c in ev.get("candidates", []) if c.get("score", 0) > 0]
    if not cands or ev.get("future_match"):
        verdict = "NOT_YET_KNOWN" if ev.get("future_match") else "NOT_FOUND"
        yield from one(
            "submit_case_file",
            {"verdict": verdict, "reasons": [ev.get("note") or "no visible event matches the question"]},
        )
        return
    top = cands[0]
    eid = top["event_id"]
    alts = [c["event_id"] for c in cands[1:3]]
    prof = yield from one("event_profile", {"event_id": eid})
    rule = prof.get("rule_verdict", "NOT_FOUND")
    key_docs = [d["doc_id"] for d in prof.get("key_docs", [])]
    if rule != "VERIFIED":
        yield from one(
            "submit_case_file",
            {
                "verdict": rule,
                "reasons": prof.get("reasons", []),
                "event_id": eid,
                "supporting_docs": key_docs,
                "contradicting_docs": prof.get("denials", []),
                "retractions_applied": [r["doc_id"] for r in prof.get("retractions", [])],
                "alternatives": alts,
            },
        )
        return
    countries = yield from one("get_reference", {"sheet": "countries"})
    sectors = yield from one("get_reference", {"sheet": "sectors"})
    chokes = yield from one("get_reference", {"sheet": "chokepoints"})
    named_c = [
        r["name"]
        for r in countries.get("rows", [])
        if r.get("name") and re.search(rf"\b{re.escape(r['name'])}", q, re.I)
    ]
    named_s = [
        r["name"]
        for r in sectors.get("rows", [])
        if r.get("name") and re.search(rf"\b{re.escape(r['name'])}", q, re.I)
    ]
    tables = yield from one("list_trade_tables", {"event_id": eid})
    rows = []
    for c in named_c:
        for s in named_s:
            tabs = [
                t["table_id"]
                for t in tables.get("tables", [])
                if (t.get("sector") or "").lower() == s.lower()
                and c.lower() in {(t.get("exporter") or "").lower(), (t.get("importer") or "").lower()}
            ]
            rows.append({"country": c, "sector": s, "trade_tables": tabs})
    core = _core_query(q)
    searches = [
        {"query": core, "filters": {"event_id": eid}, "k": 6},
        {"query": f"{core} official notice", "filters": {"event_id": eid, "tier_max": 2}, "k": 5},
        {
            "query": "correction retraction clarification",
            "filters": {"event_id": eid, "source_type": ["correction", "retraction"]},
            "k": 4,
        },
        # a deliberately narrow probe for the latest official update (often empty; remembered if so)
        {
            "query": "latest official update reopening date",
            "filters": {"event_id": eid, "tier_max": 1, "date_from": (as_of - timedelta(days=7)).isoformat()},
            "k": 3,
        },
    ]
    pool: list[str] = []
    results: list[dict[str, Any]] = []
    for s in searches:
        r = yield from one("search_documents", s)
        for item in r.get("results", []):
            results.append(item)
            if item["doc_id"] not in pool and len(pool) < pool_size:
                pool.append(item["doc_id"])
    inj = next((x for x in results if "INJECTION_DETECTED" in (x.get("flags") or [])), None)
    if inj is not None:
        yield from one(
            "flag_injection", {"doc_id": inj["doc_id"], "span": clean(inj.get("snippet", ""))[:200]}
        )
    t1 = next((x for x in results if x.get("tier") == 1 and not x["as_of_status"]["retracted"]), None) or (
        results[0] if results else None
    )
    summary = ""
    if t1 is not None:
        doc = yield from one("read_document", {"doc_id": t1["doc_id"]})
        sent = sentences(doc.get("document", ""))
        first = next((s for s in sent if len(s) > 40), sent[0] if sent else "")
        summary = f"{first.rstrip('.')} [{t1['doc_id']}, {t1['pub_date']}]."
    choke_names = [
        r["name"]
        for r in chokes.get("rows", [])
        if r.get("name")
        and (re.search(re.escape(r["name"]), q, re.I) or re.search(re.escape(r["name"]), summary, re.I))
    ]
    yield from one(
        "submit_case_file",
        {
            "verdict": "VERIFIED",
            "reasons": prof.get("reasons", []),
            "event_id": eid,
            "event_summary": summary,
            "event_date": prof.get("first_date"),
            "chokepoints": choke_names,
            "rows": rows,
            "supporting_docs": [d for d in key_docs if d in pool][:6] or key_docs[:4],
            "contradicting_docs": prof.get("denials", []),
            "retractions_applied": [r["doc_id"] for r in prof.get("retractions", [])],
            "alternatives": alts,
            "pool": pool
            + [
                t["table_id"]
                for t in tables.get("tables", [])
                if any(t["table_id"] in r["trade_tables"] for r in rows)
            ],
        },
    )


def heimdall_research(v: dict[str, Any]) -> Policy:
    eid = (v.get("scope") or {}).get("event_id")
    defs = v.get("deficiencies", [])
    pool = {p["ref"] for p in v.get("pool", [])}
    found: dict[str, list[str]] = {d["id"]: [] for d in defs}
    new: list[str] = []
    queries_run = 0

    def collect(res: dict[str, Any], def_ids: list[str]) -> None:
        for item in res.get("results", []):
            if (
                item["doc_id"] not in pool
                and item["doc_id"] not in new
                and not item["as_of_status"]["retracted"]
            ):
                new.append(item["doc_id"])
                for d in def_ids:
                    found[d].append(item["doc_id"])

    # 1. earlier searches that returned nothing may find something now; memory may veto the repeat
    for pq in v.get("past_queries", []):
        if pq.get("tool") != "search_documents" or pq.get("result_count") != 0:
            continue
        args = pq.get("args", {})
        r = yield from one(
            "search_documents", {"query": args.get("query", ""), "filters": args.get("filters", {})}
        )
        queries_run += 1
        if r.get("skipped_by_memory") or r.get("duplicate_of"):
            # reformulate: drop the narrow date window and tier cap, filter by source type instead
            r = yield from one(
                "search_documents",
                {
                    "query": f"{args.get('query', '')} notice",
                    "filters": {"event_id": eid, "source_type": ["official_notice"]},
                    "k": 5,
                },
            )
            queries_run += 1
        collect(
            r, [d["id"] for d in defs if "duration" in d["statement"].lower() or d["check"] in ("S6", "S4")]
        )
    # 2. one query per parameter named in a deficiency
    per_param: dict[str, list[str]] = {}
    general: list[str] = []
    for d in defs:
        ps = params_in(d["statement"])
        for p in ps:
            per_param.setdefault(p, []).append(d["id"])
        if not ps:
            general.append(d["id"])
    for p, ids in per_param.items():
        if queries_run >= 8:
            break
        r = yield from one(
            "search_documents", {"query": param_keywords(p), "filters": {"event_id": eid}, "k": 6}
        )
        queries_run += 1
        collect(r, ids)
        r = yield from one(
            "search_documents",
            {"query": param_keywords(p), "filters": {"event_id": eid, "tier_max": 2}, "k": 4},
        )
        queries_run += 1
        collect(r, ids)
    if general and queries_run < 9:
        r = yield from one(
            "search_documents",
            {"query": "official confirmation update", "filters": {"event_id": eid, "tier_max": 2}, "k": 6},
        )
        collect(r, general)
    if any("trade evidence" in d["statement"].lower() for d in defs):
        t = yield from one("list_trade_tables", {"event_id": eid})
        for tab in t.get("tables", []):
            if tab["table_id"] not in pool and tab["table_id"] not in new:
                new.append(tab["table_id"])
    per = {d: ("ADDRESSED" if found[d] else "NO_EVIDENCE_FOUND") for d in found}
    yield from one(
        "submit_case_file",
        {
            "new_items": new[:10],
            "per_deficiency": per,
            "notes": f"{len(new[:10])} new items from {queries_run} searches",
        },
    )


# ======================================================================== Thor / Loki
def _cands_from_result(item: dict[str, Any], text: str, as_of: date, h: float) -> list[dict[str, Any]]:
    out = []
    for c in extract(text, item.get("pub_date"), as_of, h):
        out.append(
            {
                "param": c.param,
                "value": c.value,
                "quote": c.quote,
                "doc_id": item["doc_id"],
                "tier": item.get("tier") or 4,
                "pub_date": item.get("pub_date") or "",
                "event_id": item.get("event_id"),
                "retracted": bool(item.get("as_of_status", {}).get("retracted")),
                "corrected_by": item.get("as_of_status", {}).get("corrected_by"),
                "countries": [x.lower() for x in item.get("country") or []],
            }
        )
    return out


def _pick(side: str, param: str, cands: list[dict[str, Any]], tier_cap: int) -> dict[str, Any] | None:
    pool = [c for c in cands if c["param"] == param and not c["retracted"] and c["tier"] <= tier_cap]
    if side == "loki":
        pool = [c for c in pool if not c["corrected_by"]]
    if not pool:
        return None
    high_is_bad = param in INCREASING
    want_max = (side == "thor") == high_is_bad

    def key(c: dict[str, Any]) -> tuple[float, int, str]:
        return (c["value"] if want_max else -c["value"], -c["tier"], c["pub_date"])

    return max(pool, key=key)


def _consensus(param: str, cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    pool = [c for c in cands if c["param"] == param and not c["retracted"] and not c["corrected_by"]]
    if not pool:
        return None
    return min(
        pool, key=lambda c: (c["tier"], tuple(-int(x) for x in c["pub_date"].split("-") if x.isdigit()))
    )


def _interval(side: str, param: str, value: float, h: float, tight: bool) -> list[float]:
    w = (0.03 if tight else 0.10) * (h if param == "duration_months" else 1.0)
    hi_dom = h if param == "duration_months" else 1.0
    increasing = param in INCREASING
    if (side == "thor") == increasing:
        lo, hi = value - w / 2, value + w  # Thor argues upward from the cited value, Loki downward
    else:
        lo, hi = value - w, value + w / 2
    if tight:
        lo, hi = value - w / 2, value + w / 2
    return [round(max(0.0, lo), 4), round(min(hi_dom, hi), 4)]


def _statement(row: dict[str, Any], c: dict[str, Any]) -> str:
    quote = re.sub(r"[.!?]+$", "", c["quote"]).replace(". ", "; ")
    return (
        f"{row['country']}/{row['sector']} {c['param']} = {c['value']:.2f}, as stated in {c['doc_id']} "
        f'(Tier {c["tier"]}): "{quote}"'
    )


def side_submit(side: str, v: dict[str, Any], mems: list[dict[str, Any]], h: float) -> Policy:
    as_of = _as_of(v)
    run = int(v.get("run", 1))
    scope = v.get("scope") or {}
    eid = scope.get("event_id")
    rows = scope.get("rows", [])
    deficient = {p for d in v.get("deficiencies", []) for p in params_in(d["statement"])}
    # memory: a procedural lesson to corroborate changes which tiers I accept for the named parameters
    influence: list[dict[str, Any]] = []
    strict: set[str] = set()
    for m in mems:
        if m["kind"] in ("procedural", "precedent") and re.search(r"corroborat", m["text"], re.I):
            ps = params_in(m["text"]) or list(PARAMS)
            strict.update(ps)
            influence.append(
                {
                    "memory_id": m["id"],
                    "effect": f"required Tier 1-2 support for {', '.join(ps)} "
                    "and searched with tier_max=2 first",
                }
            )
    avoid_corrected = side == "loki"
    for m in mems:
        if (
            m["kind"] in ("procedural", "precedent")
            and re.search(r"correct", m["text"], re.I)
            and side == "thor"
        ):
            avoid_corrected = True
            influence.append(
                {
                    "memory_id": m["id"],
                    "effect": "skipped figures from reports that were corrected as of the date",
                }
            )
    mine_all = [c for c in v.get("earlier_claims", []) if c["side"] == side]
    if any(c["status"] == "CONCEDED" for c in mine_all):
        avoid_corrected = True  # learned within this case
    cands: list[dict[str, Any]] = []
    tier_cap = 3
    base_filters: dict[str, Any] = {"event_id": eid}
    side_filters = {"tier_max": 3} if side == "thor" else {"date_to": as_of.isoformat()}
    searched = False
    for row in rows[:2] if run == 1 else []:
        q = f"{row['country']} {row['sector']} disruption impact trade"
        r = yield from one(
            "search_documents", {"query": q, "filters": {**base_filters, **side_filters}, "k": 8}
        )
        searched = searched or not r.get("duplicate_of")
        for item in r.get("results", []):
            cands += _cands_from_result(item, item.get("snippet", ""), as_of, h)
    focus = sorted(strict | (deficient if run > 1 else set()))
    where = f"{rows[0]['country']} {rows[0]['sector']}" if rows else ""
    for p in focus:
        r = yield from one(
            "search_documents",
            {
                "query": f"{param_keywords(p)} {where}".strip(),
                "filters": {**base_filters, "tier_max": 2},
                "k": 10,
            },
        )
        searched = searched or not r.get("duplicate_of")
        for item in r.get("results", []):
            cands += _cands_from_result(item, item.get("snippet", ""), as_of, h)
    if not searched:
        r = yield from one(
            "search_documents",
            {
                "query": f"{where} recent developments".strip(),
                "filters": {**base_filters, **side_filters},
                "k": 6,
            },
        )
        for item in r.get("results", []):
            cands += _cands_from_result(item, item.get("snippet", ""), as_of, h)
    if run > 1:
        doc_refs = {p["ref"] for p in v.get("pool", []) if p.get("kind") == "doc"}
        for ref in [x for x in v.get("new_evidence", []) if x in doc_refs][:4]:
            d = yield from one("read_document", {"doc_id": ref})
            if d.get("document"):
                meta = {
                    "doc_id": ref,
                    "tier": d.get("tier"),
                    "pub_date": d.get("pub_date"),
                    "event_id": d.get("event_id"),
                    "as_of_status": d.get("as_of_status", {}),
                    "country": d.get("country"),
                }
                cands += _cands_from_result(meta, d["document"], as_of, h)
    # only this event's documents count as evidence
    cands = [c for c in cands if (c["event_id"] or "").lower() == (eid or "").lower()]
    if avoid_corrected:
        cands = [c for c in cands if not c["corrected_by"]]
    # trade-row evidence for an observed post-event drop (Thor) or resilience (Loki)
    trade_ev: dict[str, Any] | None = None
    tables = [t for row in rows[:1] for t in row.get("trade_tables", [])] if run == 1 else []
    best: tuple[float, str, dict[str, Any]] | None = None
    for t in tables[:2]:
        st = yield from one("trade_stats", {"table_id": t})
        ch = st.get("observed_change")
        if ch is None:
            continue
        score = -ch if side == "thor" else ch
        if best is None or score > best[0]:
            best = (score, t, st)
    if best is not None and best[2].get("post_event_months"):
        tbl = yield from one("read_trade_table", {"table_id": best[1]})
        month = best[2]["post_event_months"][0]
        row_rec = next((r for r in tbl.get("rows", []) if r["month"] == month and r["quality"] == "OK"), None)
        if row_rec is not None:
            trade_ev = {"table_id": best[1], "row": row_rec["row"], "change": best[2]["observed_change"]}
    claims: list[dict[str, Any]] = []
    positions = []
    my_prior = {
        c["id"]: c for c in v.get("earlier_claims", []) if c["side"] == side and c["status"] == "ACTIVE"
    }
    carry: list[str] = []
    n = 0
    for row in rows:
        params: dict[str, list[float]] = {}
        pclaims: dict[str, list[str]] = {}
        for p in PARAMS:
            prior = next(
                (
                    c
                    for c in my_prior.values()
                    if c["parameter"] == p and c["country"] == row["country"] and c["sector"] == row["sector"]
                ),
                None,
            )
            tight = run > 1 and p in deficient
            cap = 2 if p in strict else tier_cap
            pick = _consensus(p, cands) if tight else _pick(side, p, cands, cap)
            if prior is not None and not tight:
                carry.append(prior["id"])
                params[p] = _interval(side, p, float(prior["bound_value"] or 0.0), h, False)
                pclaims[p] = [prior["id"]]
                continue
            if pick is None:
                if prior is not None:
                    carry.append(prior["id"])
                    params[p] = _interval(side, p, float(prior["bound_value"] or 0.0), h, False)
                    pclaims[p] = [prior["id"]]
                continue  # no evidence: interval left out (assumed full domain by code)
            n += 1
            local = f"C{n:02d}"
            evidence: list[dict[str, Any]] = [{"doc_id": pick["doc_id"], "quote": pick["quote"]}]
            if p == "severity" and trade_ev is not None and side == "thor" and row is rows[0]:
                evidence.append({"table_id": trade_ev["table_id"], "row": trade_ev["row"]})
            claims.append(
                {
                    "id": local,
                    "statement": _statement(row, pick),
                    "type": "SOURCED_FACT",
                    "country": row["country"],
                    "sector": row["sector"],
                    "parameter": p,
                    "bound_value": round(pick["value"], 4),
                    "evidence": evidence,
                    "supersedes": prior["id"] if prior is not None else None,
                }
            )
            params[p] = _interval(side, p, pick["value"], h, tight)
            pclaims[p] = [local]
        positions.append(
            {"country": row["country"], "sector": row["sector"], "params": params, "param_claims": pclaims}
        )
    for pos in positions[:1]:
        yield from one(
            "compute_exposure",
            {
                "country": pos["country"],
                "sector": pos["sector"],
                "params": pos["params"],
                "param_claims": pos["param_claims"],
            },
        )
    payload = {
        "claims": claims,
        "positions": positions,
        "carry_forward": sorted(set(carry)),
        "memory_influence": influence,
    }
    res = yield from one("submit_claims", payload)
    if res.get("accepted") is False:
        errors = " ".join(res.get("errors", []))
        if "search_documents" in errors:
            yield from one(
                "search_documents",
                {
                    "query": f"{where} sources evidence {side}".strip(),
                    "filters": {**base_filters, "include_background": True},
                    "k": 4,
                },
            )
        yield from one("submit_claims", payload)


def _min_tier(claim: dict[str, Any]) -> int:
    tiers = [e.get("tier") for e in claim.get("evidence", []) if e.get("doc_id")]
    if any(e.get("table_id") and e.get("quality") == "OK" for e in claim.get("evidence", [])):
        return 0
    return min([t for t in tiers if t] or [9])


def _issues(claim: dict[str, Any]) -> set[str]:
    return {i for x in claim.get("validation", []) for i in x.get("issues", [])}


def _doc_ev(claim: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"doc_id": e["doc_id"], "quote": e["quote"]} for e in claim.get("evidence", []) if e.get("doc_id")
    ]


def side_attack(side: str, v: dict[str, Any], h: float) -> Policy:
    opp = [c for c in v.get("opponent_claims", []) if c["status"] == "ACTIVE"]
    mine = [c for c in v.get("my_claims", []) if c["status"] == "ACTIVE"]
    attacks: list[dict[str, Any]] = []
    for c in opp:
        iss = _issues(c)
        valid = [x for x in c.get("validation", []) if x.get("valid")]
        if "RETRACTED_AS_OF" in iss or "CORRECTED_AS_OF" in iss:
            sup = next(
                (x.get("superseded_by") for x in c.get("validation", []) if x.get("superseded_by")), None
            )
            attacks.append(
                {
                    "target_claim": c["id"],
                    "type": "RETRACTED_OR_CORRECTED",
                    "argument": f"The cited source was {'corrected by ' + sup if sup else 'retracted'} as of "
                    "the case date; the original figure no longer stands.",
                }
            )
        elif "WRONG_EVENT" in iss or ("BACKGROUND_ONLY" in iss and not valid):
            attacks.append(
                {
                    "target_claim": c["id"],
                    "type": "WRONG_EVENT",
                    "argument": "The cited document belongs to a different event or is background only.",
                }
            )
        elif "AFTER_AS_OF" in iss:
            attacks.append(
                {"target_claim": c["id"], "type": "AFTER_AS_OF", "argument": "Cited after the as-of date."}
            )
        elif "QUOTE_MISMATCH" in iss:
            attacks.append(
                {
                    "target_claim": c["id"],
                    "type": "QUOTE_MISMATCH",
                    "argument": "The quoted text does not appear in the cited document.",
                }
            )
        elif _min_tier(c) >= 4:
            attacks.append(
                {
                    "target_claim": c["id"],
                    "type": "SOURCE_RELIABILITY",
                    "argument": "The claim rests only on a Tier 4 source with no corroboration.",
                }
            )
        else:
            mine_same = [m for m in mine if m["parameter"] == c["parameter"] and m["country"] == c["country"]]
            for m in mine_same:
                dom = h if c["parameter"] == "duration_months" else 1.0
                differs = abs((m["bound_value"] or 0) - (c["bound_value"] or 0)) > 0.05 * dom
                if differs and _min_tier(m) < _min_tier(c) and _doc_ev(m):
                    attacks.append(
                        {
                            "target_claim": c["id"],
                            "type": "COUNTER_EVIDENCE",
                            "argument": f"A stronger source (Tier {_min_tier(m)}) puts {c['parameter']} at "
                            f"{m['bound_value']}, not {c['bound_value']}.",
                            "evidence": _doc_ev(m)[:1],
                        }
                    )
                    break
                if differs and _min_tier(m) == _min_tier(c) and _doc_ev(m) and _newer(m, c):
                    attacks.append(
                        {
                            "target_claim": c["id"],
                            "type": "COUNTER_EVIDENCE",
                            "argument": f"A more recent source of the same tier puts {c['parameter']} at "
                            f"{m['bound_value']}, not {c['bound_value']}.",
                            "evidence": _doc_ev(m)[:1],
                        }
                    )
                    break
    if not attacks and opp:
        weakest = max(opp, key=lambda c: (_min_tier(c), c["id"]))
        attacks.append(
            {
                "target_claim": weakest["id"],
                "type": "LOGIC_GAP",
                "argument": "The claim rests on a single source; the cited text does not establish the bound "
                "for the whole horizon.",
            }
        )
    yield from one("submit_attacks", {"attacks": attacks[:4]})


def _newer(a: dict[str, Any], b: dict[str, Any]) -> bool:
    da = max((str(e.get("pub_date")) for e in a.get("evidence", []) if e.get("doc_id")), default="")
    db = max((str(e.get("pub_date")) for e in b.get("evidence", []) if e.get("doc_id")), default="")
    return da > db


def side_respond(side: str, v: dict[str, Any], h: float) -> Policy:
    as_of = _as_of(v)
    responses = []
    cache: dict[str, list[dict[str, Any]]] = {}
    for item in v.get("attacks_on_me", []):
        a = item["attack"]
        c = item["target_claim"] or {}
        cited = set(item.get("already_cited", []))
        iss = _issues(c)
        p = c.get("parameter")
        my_tier = _min_tier(c)
        if a["type"] == "RETRACTED_OR_CORRECTED" and "CORRECTED_AS_OF" in iss:
            sup = next(
                (x.get("superseded_by") for x in c.get("validation", []) if x.get("superseded_by")), None
            )
            responses.append(
                {
                    "attack_id": a["id"],
                    "action": "CONCEDE",
                    "argument": f"The cited figure was corrected{' by ' + sup if sup else ''} as of the "
                    "case date; the claim is withdrawn and the corrected figure stands.",
                }
            )
            continue
        if a["type"] in ("RETRACTED_OR_CORRECTED", "WRONG_EVENT", "AFTER_AS_OF", "QUOTE_MISMATCH") and iss & {
            "RETRACTED_AS_OF",
            "WRONG_EVENT",
            "AFTER_AS_OF",
            "QUOTE_MISMATCH",
            "BACKGROUND_ONLY",
        }:
            responses.append(
                {
                    "attack_id": a["id"],
                    "action": "CONCEDE",
                    "argument": "The validator confirms the citation defect; the claim is withdrawn.",
                }
            )
            continue
        # look for fresh support for the same parameter
        fresh: dict[str, Any] | None = None
        if p:
            if p not in cache:
                r = yield from one(
                    "search_documents",
                    {
                        "query": f"{param_keywords(p)} corroboration",
                        "filters": {"event_id": _event_of(c), "tier_max": 2},
                        "k": 5,
                    },
                )
                cache[p] = []
                for it in r.get("results", []):
                    cache[p] += _cands_from_result(it, it.get("snippet", ""), as_of, h)
            dom = h if p == "duration_months" else 1.0
            fresh = next(
                (
                    x
                    for x in cache[p]
                    if x["param"] == p
                    and x["doc_id"] not in cited
                    and not x["retracted"]
                    and abs(x["value"] - (c.get("bound_value") or 0)) <= 0.25 * dom
                ),
                None,
            )
        att_ev = [e for e in a.get("evidence", []) if e.get("doc_id")]
        att_tier = min([e.get("tier") or 9 for e in att_ev] or [9])
        att_date = max([str(e.get("pub_date")) for e in att_ev] or [""])
        my_date = max([str(e.get("pub_date")) for e in c.get("evidence", []) if e.get("doc_id")] or [""])
        if a["type"] == "SOURCE_RELIABILITY":
            if fresh is not None:
                responses.append(_defend(a, fresh))
            else:
                responses.append(
                    {
                        "attack_id": a["id"],
                        "action": "CONCEDE",
                        "argument": "No corroborating Tier 1-2 source was found; the claim is withdrawn.",
                    }
                )
            continue
        if a["type"] in ("COUNTER_EVIDENCE", "NUMERIC") and att_ev:
            stronger = att_tier < my_tier or (att_tier == my_tier and att_date > my_date)
            if stronger and (my_tier >= 3 or att_tier == 1):
                responses.append(
                    {
                        "attack_id": a["id"],
                        "action": "CONCEDE",
                        "argument": f"A stronger or more recent source (Tier {att_tier}) contradicts the "
                        "claim; conceding.",
                    }
                )
                continue
            if fresh is not None and not stronger:
                responses.append(_defend(a, fresh))
                continue
            att_vals = [
                x.value
                for e in att_ev
                for x in extract(e.get("quote", ""), e.get("pub_date"), as_of, h)
                if x.param == p
            ]
            target = att_vals[0] if att_vals else c.get("bound_value")
            mid = round(((c.get("bound_value") or 0) + (target or 0)) / 2, 4)
            responses.append(
                {
                    "attack_id": a["id"],
                    "action": "REVISE",
                    "revised_bound": mid,
                    "argument": "Narrowing the estimate toward the cited counter-evidence.",
                    "new_evidence": [{"doc_id": e["doc_id"], "quote": e["quote"]} for e in att_ev[:1]],
                }
            )
            continue
        if fresh is not None:
            responses.append(_defend(a, fresh))
        else:
            responses.append(
                {
                    "attack_id": a["id"],
                    "action": "REVISE",
                    "revised_bound": c.get("bound_value"),
                    "argument": "The estimate is kept; it is the figure the cited source states.",
                }
            )
    yield from one("submit_responses", {"responses": responses})


def _event_of(c: dict[str, Any]) -> str | None:
    return next((e.get("event_id") for e in c.get("evidence", []) if e.get("event_id")), None)


def _defend(a: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    return {
        "attack_id": a["id"],
        "action": "DEFEND",
        "argument": f"An additional Tier {fresh['tier']} source supports the estimate ({fresh['value']:.2f}).",
        "new_evidence": [{"doc_id": fresh["doc_id"], "quote": fresh["quote"]}],
    }


# ======================================================================== Odin
def odin_judge(v: dict[str, Any]) -> Policy:
    claims = v.get("claims_to_rule", [])
    attacks = v.get("attacks", [])
    responses = v.get("responses", [])
    unreb = set(v.get("unrebutted_attacks", []))
    rulings = []
    for c in claims:
        val = c.get("validation", [])
        valid_refs = {x["ref"] for x in val if x.get("valid")}
        ev = c.get("evidence", [])
        valid_ev = [
            e
            for e in ev
            if (e.get("doc_id") or f"{e.get('table_id')}#r{(e.get('row') or 0):02d}") in valid_refs
        ]
        tiers = [e.get("tier") for e in valid_ev if e.get("doc_id")]
        clean_row = any(e.get("table_id") and e.get("quality") == "OK" for e in valid_ev)
        strong = clean_row or any(t is not None and t <= 2 for t in tiers)
        t1 = clean_row or 1 in tiers
        atks = [a for a in attacks if a["target_claim"] == c["id"]]
        resps = [r for r in responses if any(r["attack_id"] == a["id"] for a in atks)]
        sims_ok = all(
            (x.get("quote_similarity") or 0) >= 90 for x in val if x.get("quote_similarity") is not None
        )
        basis = "SOURCED_FACT" if c["type"] == "SOURCED_FACT" and sims_ok else "INFERENCE"
        decisive = [e.get("doc_id") or f"{e['table_id']}#r{e['row']:02d}" for e in valid_ev[:2]]
        rul: dict[str, Any] = {"claim_id": c["id"], "decisive_evidence": decisive}
        if c["status"] == "CONCEDED":
            rul.update(
                verdict="CONCEDED",
                basis=basis if valid_ev else "UNSUPPORTED",
                finding="CONCEDED",
                rationale="The claim was withdrawn by its side after cross-examination.",
            )
        elif not valid_ev:
            issues = sorted({i for x in val for i in x.get("issues", [])})
            rul.update(
                verdict="REJECTED",
                basis="UNSUPPORTED",
                finding="REJECTED",
                rationale=f"No citation survives validation ({', '.join(issues) or 'invalid'}); a claim "
                "without valid support is rejected.",
            )
        elif tiers and all(t == 4 for t in tiers) and not clean_row:
            rul.update(
                verdict="REJECTED",
                basis="UNSUPPORTED",
                finding="REJECTED",
                rationale="The only support is a Tier 4 source with no corroboration.",
            )
        elif any(a["id"] in unreb and a.get("evidence") for a in atks):
            rul.update(
                verdict="REJECTED",
                basis=basis,
                finding="REJECTED",
                rationale="An attack with valid evidence was left unrebutted.",
            )
        elif any(r["action"] == "REVISE" for r in resps):
            rev = next(r for r in resps if r["action"] == "REVISE")
            rul.update(
                verdict="PARTIALLY_UPHELD",
                basis="INFERENCE",
                finding="CONTESTED",
                adjusted_bound=rev.get("revised_bound"),
                rationale="The side revised its estimate under cross-examination; the revised bound is kept and "
                "the point remains contested.",
            )
        elif any(r["action"] == "DEFEND" for r in resps) and any(
            a["type"] == "COUNTER_EVIDENCE" for a in atks
        ):
            rul.update(
                verdict="UPHELD",
                basis=basis,
                finding="CONTESTED",
                rationale="Defended with new evidence, but credible counter-evidence remains: contested.",
            )
        elif c.get("unchallenged") and not t1:
            rul.update(
                verdict="UPHELD",
                basis=basis,
                finding="CONTESTED",
                rationale="Unchallenged because the other side failed; without Tier 1 support it is at most "
                "contested.",
            )
        else:
            rul.update(
                verdict="UPHELD",
                basis=basis,
                finding="ESTABLISHED" if strong else "CONTESTED",
                rationale=(
                    "Supported by a valid Tier 1-2 source or a clean trade row."
                    if strong
                    else "Supported only by a Tier 3 source; plausible but not established."
                ),
            )
        rulings.append(rul)
    res = yield from one("submit_rulings", {"rulings": rulings})
    checklist = res.get("checklist", {})
    # judgment: a parameter whose surviving support is only Tier 3+ is a gap, even if nothing failed formally
    by_id = {c["id"]: c for c in claims}
    ruled = {r["claim_id"]: r for r in rulings}
    gaps = []
    for fr in res.get("final_ranges", []):
        for p in fr.get("params", {}):
            alive = [
                by_id[cid]
                for cid, r in ruled.items()
                if r["verdict"] in ("UPHELD", "PARTIALLY_UPHELD")
                and by_id[cid].get("parameter") == p
                and by_id[cid].get("country") == fr["country"]
            ]
            if alive and min(_min_tier(c) for c in alive) >= 3:
                gaps.append(
                    {
                        "check": "S2",
                        "related": [c["id"] for c in alive],
                        "statement": f"No Tier 1-2 source on {p} for {fr['country']}/{fr['sector']} dated on or "
                        f"before {v.get('as_of')}; the surviving bound rests on Tier "
                        f"{min(_min_tier(c) for c in alive)} sources only.",
                    }
                )
    verdict = "SUFFICIENT" if checklist and all(checklist.values()) and not gaps else "INSUFFICIENT"
    yield from one(
        "submit_sufficiency", {"verdict": verdict, "s5_contradictions_resolved": True, "deficiencies": gaps}
    )


def odin_brief(v: dict[str, Any]) -> Policy:
    d = v.get("digest", {})
    allowed = set(v.get("allowed_citations", []))

    def lines(key: str) -> str:
        out = []
        for e in d.get(key, []):
            cites = " ".join(c["cite"] for c in e.get("citations", [])[:2] if c["cite"] in allowed)
            label = " (inference)" if e.get("basis") == "INFERENCE" else ""
            out.append(f"- {e['ruling']}: {e['statement']}{label} {cites}".rstrip())
        return "\n".join(out) or "None."

    def any_cite() -> str:
        for key in ("established", "contested", "conceded"):
            for e in d.get(key, []):
                for c in e.get("citations", []):
                    if c["cite"] in allowed:
                        return str(c["cite"])
        return sorted(allowed)[0] if allowed else ""

    ver_docs = [
        c
        for c in sorted(allowed)
        if any(c.startswith(f"[{x},") for x in d.get("verification", {}).get("docs", []))
    ]
    ver_cite = ver_docs[0] if ver_docs else any_cite()
    exec_parts = [f"Heimdall verified the event before the trial {ver_cite}."]
    for fr in d.get("final_ranges", []):
        cite = _cite_for_rulings(d, fr.get("rulings", []), allowed) or any_cite()
        exec_parts.append(
            f"For {fr['country']}/{fr['sector']}, Odin's final range is {fr['low']:.1f}-{fr['high']:.1f}% "
            f"of baseline trade value over the next quarter {cite}."
        )
    if d.get("status") == "INCOMPLETE":
        exec_parts.append(f"This case is INCOMPLETE: {', '.join(d.get('status_reasons', []))}.")
    outlook = []
    assumptions = []
    for fr in d.get("final_ranges", []):
        cite = _cite_for_rulings(d, fr.get("rulings", []), allowed) or any_cite()
        outlook.append(
            f"Next quarter, {fr['country']}/{fr['sector']} trade value is expected to run "
            f"{fr['low']:.1f}-{fr['high']:.1f}% below its pre-event baseline under the assumptions below {cite}."
        )
        for i, rid in enumerate(fr.get("rulings", [])[:4], start=len(assumptions) + 1):
            assumptions.append(
                {
                    "id": f"A{i}",
                    "text": f"the bound ruled in {rid} holds through the horizon",
                    "ruling_ids": [rid],
                }
            )
    yield from one(
        "submit_briefing",
        {
            "executive_summary": " ".join(exec_parts),
            "verification_note": "",
            "established": lines("established"),
            "contested": lines("contested"),
            "conceded": lines("conceded"),
            "outlook": " ".join(outlook) or "No final range could be computed.",
            "assumptions": assumptions,
        },
    )


def _cite_for_rulings(d: dict[str, Any], rulings: list[str], allowed: set[str]) -> str:
    for key in ("established", "contested", "conceded"):
        for e in d.get(key, []):
            if e["ruling"] in rulings:
                for c in e.get("citations", []):
                    if c["cite"] in allowed:
                        return str(c["cite"])
    return ""


# ======================================================================== reflection
def reflect(agent: str, v: dict[str, Any]) -> Policy:
    outcomes = v.get("my_outcomes", [])
    lessons: list[dict[str, Any]] = []
    if agent in ("thor", "loki"):
        for o in outcomes:
            if (
                o.get("claim")
                and o.get("verdict") in ("REJECTED", "CONCEDED")
                and "RETRACTED_OR_CORRECTED" in (o.get("attacked_by") or [])
            ):
                lessons.append(
                    {
                        "kind": "procedural",
                        "tags": [o.get("parameter") or "claim", "corrections"],
                        "confidence": 0.75,
                        "text": f"A {o.get('parameter')} figure from a report that was later corrected had to be "
                        f"{o['verdict'].lower()}; check find_related_docs for corrections before citing "
                        "a number.",
                    }
                )
            elif (
                o.get("claim")
                and o.get("verdict") in ("REJECTED", "CONCEDED")
                and o.get("evidence_tiers")
                and min(o["evidence_tiers"]) >= 3
            ):
                lessons.append(
                    {
                        "kind": "procedural",
                        "tags": [o.get("parameter") or "claim", "corroborate"],
                        "confidence": 0.7,
                        "text": f"{o.get('parameter')} claims backed only by Tier {min(o['evidence_tiers'])} "
                        f"sources were {o['verdict'].lower()}; corroborate with a Tier 1-2 source "
                        "before claiming.",
                    }
                )
            elif o.get("claim") and o.get("verdict") == "PARTIALLY_UPHELD":
                lessons.append(
                    {
                        "kind": "procedural",
                        "tags": [o.get("parameter") or "claim", "corrections"],
                        "confidence": 0.6,
                        "text": f"A {o.get('parameter')} figure had to be revised under cross-examination; check "
                        "for corrections and newer official notices before claiming.",
                    }
                )
        if not lessons:
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["evidence"],
                    "confidence": 0.5,
                    "text": "Claims backed by Tier 1-2 sources or clean trade rows were upheld.",
                }
            )
    elif agent == "odin":
        if any(o.get("verdict") == "REJECTED" and o.get("evidence_tiers") == [4] for o in outcomes):
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["tier4", "precedent"],
                    "confidence": 0.8,
                    "text": "A claim resting only on Tier 4 sources is REJECTED with basis UNSUPPORTED.",
                }
            )
        if any(o.get("verdict") == "PARTIALLY_UPHELD" for o in outcomes):
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["revise", "precedent"],
                    "confidence": 0.6,
                    "text": "A claim revised under cross-examination is PARTIALLY_UPHELD at the revised bound "
                    "and marked CONTESTED.",
                }
            )
        if not lessons:
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["precedent"],
                    "confidence": 0.5,
                    "text": "Upheld claims with Tier 1-2 support and no credible contradiction are ESTABLISHED.",
                }
            )
    else:
        if any(o.get("research_for_run") for o in outcomes):
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["search", "remand"],
                    "confidence": 0.6,
                    "text": "Narrow recent-window searches capped at Tier 1 often return nothing; on a remand, "
                    "search by parameter keywords and source type instead.",
                }
            )
        if any(o.get("verdict") in ("UNCONFIRMED", "REFUTED") for o in outcomes):
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["verification", "rumour"],
                    "confidence": 0.8,
                    "text": "Event reports carried only by Tier 3-4 outlets, or denied by a Tier 1 notice, "
                    "were not verified; check for denials before opening a trial.",
                }
            )
        if not lessons:
            lessons.append(
                {
                    "kind": "procedural",
                    "tags": ["verification"],
                    "confidence": 0.5,
                    "text": "Events confirmed by a Tier 1 notice or two independent Tier 2 outlets were "
                    "verified.",
                }
            )
    yield from one("submit_lessons", {"lessons": lessons[:3]})

"""Deterministic exposure calculator (EXPOSURE_MODEL.md). LLMs argue parameters; this code computes numbers.

E = 100 * route_share * severity * (duration_months / H) * (1 - reroute_offset)    [% of baseline]
E is monotonic in every parameter, so the range comes from the corners.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.trial.casefile import PARAMETERS, Claim, Parameter, Position, Ruling

Interval = tuple[float, float]
SURVIVING = frozenset({"UPHELD", "PARTIALLY_UPHELD"})


def param_bounds(p: str, horizon: float) -> Interval:
    return (0.0, float(horizon)) if p == "duration_months" else (0.0, 1.0)


def normalize_interval(p: str, iv: Sequence[float], horizon: float, min_width: float) -> Interval:
    """Clamp into the parameter's domain, order the ends, widen degenerate intervals to the minimum width."""
    lo_b, hi_b = param_bounds(p, horizon)
    a, b = float(iv[0]), float(iv[1]) if len(iv) > 1 else float(iv[0])
    lo, hi = (a, b) if a <= b else (b, a)
    lo, hi = max(lo_b, min(hi_b, lo)), max(lo_b, min(hi_b, hi))
    width = min_width * (hi_b - lo_b)
    if hi - lo < width:
        mid = (lo + hi) / 2
        lo, hi = mid - width / 2, mid + width / 2
        if lo < lo_b:
            lo, hi = lo_b, lo_b + width
        if hi > hi_b:
            lo, hi = hi_b - width, hi_b
    return (round(lo, 6), round(hi, 6))


def exposure_pct(
    route_share: float, severity: float, duration: float, reroute: float, horizon: float
) -> float:
    return 100.0 * route_share * severity * (duration / horizon) * (1.0 - reroute)


def calc_id_for(inputs: Mapping[str, Any]) -> str:
    blob = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return "CALC-" + hashlib.sha256(blob.encode()).hexdigest()[:10]


@dataclass
class ExposureResult:
    low: float
    high: float
    params: dict[str, Interval]
    breakdown: dict[str, Any]
    anchor_applied: bool
    anchor_conflict: bool
    calc_id: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "unit": "pct_of_baseline_trade_value",
            "params": {k: list(v) for k, v in self.params.items()},
            "breakdown": self.breakdown,
            "anchor_applied": self.anchor_applied,
            "anchor_conflict": self.anchor_conflict,
            "calc_id": self.calc_id,
        }


def compute(
    params: Mapping[Any, Sequence[float]],
    horizon: float = 3.0,
    min_width: float = 0.05,
    k_obs: float | None = None,
    anchor_tol: float = 0.3,
    defaults: Mapping[str, Interval] | None = None,
) -> ExposureResult:
    """Pure function: same inputs, same outputs. Missing parameters fall back to `defaults` or the full domain."""
    norm: dict[str, Interval] = {}
    assumed: list[str] = []
    for p in PARAMETERS:
        if p in params and params[p] is not None and len(params[p]) >= 1:
            norm[p] = normalize_interval(p, params[p], horizon, min_width)
        else:
            assumed.append(p)
            base = defaults.get(p) if defaults else None
            norm[p] = normalize_interval(p, base or param_bounds(p, horizon), horizon, min_width)
    rs, sv, du, ro = (norm["route_share"], norm["severity"], norm["duration_months"], norm["reroute_offset"])
    k_low = rs[0] * sv[0] * (1 - ro[1])
    k_high = rs[1] * sv[1] * (1 - ro[0])
    anchor_applied = False
    anchor_conflict = False
    k_lo_used, k_hi_used = k_low, k_high
    if k_obs is not None:
        a_lo, a_hi = k_obs * (1 - anchor_tol), k_obs * (1 + anchor_tol)
        lo, hi = max(k_low, a_lo), min(k_high, a_hi)
        if lo <= hi:
            k_lo_used, k_hi_used = lo, hi
            anchor_applied = True
        else:
            anchor_conflict = True
    low = 100.0 * k_lo_used * du[0] / horizon
    high = 100.0 * k_hi_used * du[1] / horizon
    inputs = {
        "params": {k: list(v) for k, v in norm.items()},
        "horizon": horizon,
        "min_width": min_width,
        "k_obs": k_obs,
        "anchor_tol": anchor_tol,
    }
    return ExposureResult(
        low=round(low, 2),
        high=round(high, 2),
        params=norm,
        breakdown={
            "k_low": round(k_low, 4),
            "k_high": round(k_high, 4),
            "k_used": [round(k_lo_used, 4), round(k_hi_used, 4)],
            "duration_fraction": [round(du[0] / horizon, 4), round(du[1] / horizon, 4)],
            "assumed_params": assumed,
            "k_obs": k_obs,
        },
        anchor_applied=anchor_applied,
        anchor_conflict=anchor_conflict,
        calc_id=calc_id_for(inputs),
        inputs=inputs,
    )


# ---------------------------------------------------------------- Odin final range (§6)
ParamStatus = Literal["INTERSECTION", "CONTESTED", "THOR_ONLY", "LOKI_ONLY", "UNSUPPORTED", "MISSING"]


@dataclass
class FinalParam:
    interval: Interval
    status: ParamStatus
    rulings: list[str]
    note: str


def _surviving_interval(
    pos: Position | None,
    p: Parameter,
    claims: Mapping[str, Claim],
    rulings: Mapping[str, Ruling],
) -> tuple[Interval | None, list[str]]:
    """The side's interval for p if at least one backing claim survives; PARTIALLY_UPHELD may adjust a bound."""
    if pos is None or p not in pos.params:
        return None, []
    backing = pos.param_claims.get(p, [])
    alive = [cid for cid in backing if (r := rulings.get(cid)) is not None and r.verdict in SURVIVING]
    if not alive:
        return None, []
    lo, hi = pos.params[p]
    for cid in alive:
        r = rulings[cid]
        if r.verdict == "PARTIALLY_UPHELD" and r.adjusted_bound is not None:
            c = claims.get(cid)
            if c is not None and c.bound_value is not None:
                # the adjusted bound replaces the end of the interval the claim argued for
                if abs(c.bound_value - hi) <= abs(c.bound_value - lo):
                    hi = r.adjusted_bound
                else:
                    lo = r.adjusted_bound
    return (min(lo, hi), max(lo, hi)), [rulings[c].id for c in alive]


def final_params(
    thor: Position | None,
    loki: Position | None,
    claims: Mapping[str, Claim],
    rulings: Mapping[str, Ruling],
    horizon: float,
) -> dict[Parameter, FinalParam]:
    out: dict[Parameter, FinalParam] = {}
    for p in PARAMETERS:
        t_iv, t_r = _surviving_interval(thor, p, claims, rulings)
        l_iv, l_r = _surviving_interval(loki, p, claims, rulings)
        if t_iv and l_iv:
            lo, hi = max(t_iv[0], l_iv[0]), min(t_iv[1], l_iv[1])
            if lo <= hi:
                out[p] = FinalParam(
                    (lo, hi), "INTERSECTION", t_r + l_r, "both sides' bounds survive and overlap"
                )
            else:
                out[p] = FinalParam(
                    (min(t_iv[0], l_iv[0]), max(t_iv[1], l_iv[1])),
                    "CONTESTED",
                    t_r + l_r,
                    "both survive but are disjoint: hull, contested",
                )
        elif t_iv:
            out[p] = FinalParam(t_iv, "THOR_ONLY", t_r, "only Thor's bound survives")
        elif l_iv:
            out[p] = FinalParam(l_iv, "LOKI_ONLY", l_r, "only Loki's bound survives")
        else:
            ivs = [pos.params[p] for pos in (thor, loki) if pos is not None and p in pos.params]
            if ivs:
                out[p] = FinalParam(
                    (min(i[0] for i in ivs), max(i[1] for i in ivs)),
                    "UNSUPPORTED",
                    [],
                    "no surviving claim: hull of both, unsupported",
                )
            else:
                lo_b, hi_b = param_bounds(p, horizon)
                out[p] = FinalParam((lo_b, hi_b), "MISSING", [], "no side gave an interval: full domain")
    return out

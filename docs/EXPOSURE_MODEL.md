# Exposure Model

**Principle:** LLMs argue, code computes. Each exposure number in the system comes from `compute_exposure`, a pure deterministic function of **cited parameter ranges**. The debate is about the parameters. Odin's rulings decide which parameter bounds survive. This gives grounded, reproducible and calibrated ranges (FR2.2, FR3.2, FR7.3, scoring: range accuracy + calibration).

Code: `backend/app/tools/exposure.py`

## 1. Metric

> **Exposure(c, s)** = expected loss of trade value for country *c*, sector *s*, over the **next quarter after the as-of date**, as a **% of the pre-event baseline** trade value for that period.

The absolute value is reported alongside: `value_at_risk = Exposure% × baseline_monthly_value × 3` (in table units).

> ⚠️ The organizers' definition of "true exposure" isn't public. `EXPOSURE_METRIC` (`pct_value` | `pct_volume` | `abs_value`) lets you switch the headline unit without code changes once the OC clarifies (see [BRAINSTORM.md §8](../BRAINSTORM.md#8-open-questions-to-ask-the-organizers-early)).

## 2. Parameters

| Parameter | Meaning | Range | Typical evidence |
|---|---|---|---|
| `route_share` | Fraction of c/s baseline trade value that uses the disrupted route or chokepoint | 0–1 | trade tables per partner + reference route map, shipping explainers |
| `severity` | Fraction of that route's capacity lost while disrupted | 0–1 | official notices (full closure = 1, partial restriction < 1), shipping updates |
| `duration_months` | Months of disruption **inside the horizon** | 0–3 | official reopening dates, analyst estimates, strike notices |
| `reroute_offset` | Fraction of affected flow that still arrives via alternatives within the horizon | 0–1 | alternative port/rail capacity notes, firm statements, observed trade rows |

## 3. Formula

```
H = 3                                              # horizon months (next quarter)
E = 100 × route_share × severity × (duration_months / H) × (1 − reroute_offset)     # % of baseline
```

E is **monotonic** in every parameter (↑ route_share, severity, duration; ↓ reroute_offset), so range bounds come from the corners:

```
E_low  = f(route_share.lo, severity.lo, duration.lo, reroute_offset.hi)
E_high = f(route_share.hi, severity.hi, duration.hi, reroute_offset.lo)
```

### route_share from trade tables

For the tables `t` involving country c in sector s (imports and exports), with baselines `B_t` (from `trade_stats`) and route flags `a_t ∈ [0,1]` (share of table t's flow on the disrupted route):

```
route_share = Σ a_t·B_t / Σ B_t
```

`a_t` comes from the reference route map (if Runemark provides one) or from cited docs. Agents argue the `a_t` values they need, and `compute_exposure` can take either `route_share` directly or `{table_id: a_t}`.

### Baseline and observed change (`trade_stats`)

- `baseline` = mean value over the last `BASELINE_MONTHS = 6` **clean** pre-event months (rows flagged as damaged are excluded and reported).
- `observed_change` = mean of clean post-event months **visible as of the date** ÷ baseline − 1. `None` if no post-event month has completed by the as-of date.

## 4. Observed-data anchor (narrows ranges honestly)

If at least one clean post-event month is visible, the realised monthly hit `k_obs = max(0, −observed_change)` constrains the product `k = route_share × severity × (1 − reroute_offset)`:

```
k ∈ [k_obs × (1 − ANCHOR_TOL), k_obs × (1 + ANCHOR_TOL)]   ∩   [k_low, k_high]     # ANCHOR_TOL = 0.3
E = 100 × k × duration_months / H
```

If the intersection is empty, the anchor is **reported as a conflict** (it shows up as a Contested finding) and is not applied. Agents can argue that the early months are unrepresentative (e.g. inventories masked the drop), which is a legitimate Loki-or-Thor point.

## 5. Side positions

Each side submits, per (country, sector), an interval for every parameter plus the claim IDs backing each bound. `compute_exposure` returns `{low, high}`:
- **Thor's headline = high**, **Loki's headline = low**.
- A parameter interval with **no** backing claim is accepted for computation but marked `ASSUMED`. Odin treats it as unsupported (sufficiency check S4).
- Both prompts tell the agents that **their own range must contain the truth** (it's scored), which discourages absurd extremes.

## 6. Odin final range

For each (country, sector) and each parameter `p`:

| Thor's bound for p | Loki's bound for p | Final interval for p |
|---|---|---|
| survives | survives, **overlapping** | **intersection** (both honest sides contain the truth → the truth lies in the overlap) |
| survives | survives, **disjoint** | **hull** `[min lo, max hi]`, and the parameter is marked **CONTESTED** |
| survives | rejected / conceded | Thor's interval |
| rejected / conceded | survives | Loki's interval |
| rejected | rejected | hull of both, marked `UNSUPPORTED` → deficiency S4 (or INCOMPLETE on the last run) |

"Survives" means the parameter's backing claims have an effective ruling of `UPHELD` or `PARTIALLY_UPHELD`. A `PARTIALLY_UPHELD` ruling may carry an Odin-adjusted bound.

Then the observed anchor is applied (if available), `compute_exposure` runs with the final intervals, and the result is checked against `MAX_CONFIDENT_WIDTH_PP` (sufficiency S6).

### Why this calibrates well
- The **intersection rule** narrows wherever both sides' evidence agrees.
- **Rejected or conceded claims remove bounds**, so debate outcomes directly tighten the range.
- The **observed anchor** ties the range to realised data when it exists.
- Ranges only get wide where the evidence really conflicts, and then the finding is marked Contested.

## 7. Worked example (illustrative numbers only)

Scope row: Country A / Electronics. H = 3. Baseline 1 000 units/month.

| Param | Thor (claims) | Loki (claims) | Rulings | Final |
|---|---|---|---|---|
| route_share | 0.55–0.65 (T-C01, trade tables) | 0.40–0.60 (L-C02, trade tables + rail note) | both upheld, overlap | **0.55–0.60** |
| severity | 0.9–1.0 (T-C02, Tier 1 closure notice) | 0.5–0.7 (L-C03, Tier 4 post "partial") | L-C03 rejected (Tier 4 only) | **0.9–1.0** |
| duration_months | 2–3 (T-C03, analyst) | 0.5–1.5 (L-C04, Tier 1 reopening notice) | T-C03 conceded after attack | **0.5–1.5** |
| reroute_offset | 0.1–0.2 (T-C04) | 0.2–0.4 (L-C05, rail capacity note) | both upheld, overlap at 0.2 | **0.2–0.2** → widened to the minimum width 0.05: **0.175–0.225** |

```
E_low  = 100 × 0.55 × 0.9 × (0.5/3) × (1 − 0.225) ≈ 6.4 %
E_high = 100 × 0.60 × 1.0 × (1.5/3) × (1 − 0.175) ≈ 24.8 %
```

Thor's own range (his intervals) ≈ 26–59%, and Loki's ≈ 2–17%. Odin's 6.4–24.8% is much narrower than the hull (2–59%) and every bound traces to a ruling. Its width (18.4 pp) still exceeds `MAX_CONFIDENT_WIDTH_PP = 15`, so check S6 fails. Odin must either justify the width (here: the reopening date is really contested) or remand for evidence on duration. That is exactly the Run 1 → Run 2 dynamic.

> `MIN_PARAM_WIDTH = 0.05` stops degenerate zero-width intervals. Tune `MAX_CONFIDENT_WIDTH_PP` with the self-eval harness.

## 8. Edge cases

| Case | Handling |
|---|---|
| No trade table for the scope row | `route_share` must come from cited docs. Absolute value = N/A. Flag in data quality. |
| All post-event rows damaged | No anchor, rows listed as excluded |
| Disruption already over before the horizon | `duration_months` can be 0 → E = 0. Loki should cite the reopening notice. |
| Event affects only volume (prices up) | Switch `EXPOSURE_METRIC=pct_volume`, or report both (stretch) |
| Trade diversion **gains** for a country | Out of scope for E (bounded 0–100). Mention qualitatively as a Contested/Established finding |

## 9. Determinism and logging

Every `compute_exposure` call logs `{calc_id, inputs, param_claims, outputs, anchor_applied}`. The same inputs always give the same outputs, and `replay` recomputes every range from the saved inputs.

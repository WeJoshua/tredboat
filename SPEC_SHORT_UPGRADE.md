# Specification: Symmetric Short-Side Adaptation (BTC/USD Research)

Status: **Phase 1 implemented and verified on 2026-10-12** (111 tests; research
only). Signal generation, short replay, `optimize --direction short`, and
fail-closed execution guards are in place. Execution (Phase 2) remains blocked
by venue capability (Alpaca crypto is spot) and is explicitly deferred.
The user has now requested Phase 2. Its market/broker selection is pending;
shared discipline and resource upgrades are recorded in
[UPGRADE_STATUS.md](UPGRADE_STATUS.md). Historical deferral lists below describe
the original Phase 1 scope; the new two-loss/manual-mistake controls supersede
their loss-lockout deferral.
The mirror rules below follow the v2 long rules
([SPEC_ORDERFLOW_V2.md](SPEC_ORDERFLOW_V2.md)): overlap absorption mirror,
in-stack dominance mirror, and target escalation to the prior window low.
This document is a strict mirror of the implemented long rules, constructed
for research replay only. Execution is blocked by venue capability and is
explicitly deferred.
Source digests: [Champion_Orderflow_Strategy.md](Champion_Orderflow_Strategy.md),
[Master Order Flow Trading - Full Transcript.md](Master%20Order%20Flow%20Trading%20-%20Full%20Transcript.md).

## 0. Purpose and honesty statement

The source digest's worked example is **long-only**. Its short statements are
limited to value-down structure (`:63-72`) and prior lows as targets
(`:185-190`); it never provides a complete short algorithm. This specification
therefore defines the short side as an **explicit symmetric adaptation**, not a
reproduction of the source, and inherits every proxy and limitation of the long
spec.

Phasing, in priority order:

- **Phase 1 (this spec's implementable scope):** short signal generation,
  short replay, tests, and fail-closed execution guards. No order submission.
- **Phase 2 (blocked, out of scope):** short execution. Alpaca crypto is spot
  and does not support short sales. Enabling execution requires a separate
  venue decision (futures or another short-capable paper broker), margin/borrow
  modeling, and a new execution ownership model. None of that is claimed here.

The currently running 24-hour long observation session
(`data/observe-24h.log`, started 2026-10-12T01:56:41Z) must not be disturbed by
Phase 1 work; implementation happens only after that session or on a copy.

## 1. Mirror principle

For every long condition in SPEC_ORDERFLOW_UPGRADE.md §3 there is exactly one
short condition, produced by reflecting the price axis:

| Concept | Long (implemented) | Short (mirror) |
|---|---|---|
| Location closure | setup closes above zone upper edge | setup closes below zone lower edge |
| Absorption direction | bullish close, lower wick, negative delta, sell imbalance at/below body low | bearish close, upper wick, positive delta, buy imbalance at/above body high |
| First-failure extreme | setup low | setup high |
| Retry | confirmation trades below open and setup close | confirmation trades above open and setup close |
| Second failure | confirmation low stays above setup low | confirmation high stays below setup high |
| Dominance | bullish close above a buy-imbalance stack | bearish close below a sell-imbalance stack |
| Stop | below setup low/zone minus buffer | above setup high/zone plus buffer |
| Target | nearest node above entry, else prior high | nearest node below entry, else prior low |

Any implementation must keep the long path stable: with `direction = "long"`
(the shipped default) the decision fields for identical inputs must not change
(status, reason, features, entry, stop, target). Signal identity intentionally
includes the direction component, so stored signal IDs change once when
direction support lands; every signal, long or short, carries a `direction`
field in its payload.

Cluster detection, tie-breaks (lowest center), activity floors, the window, and
gap rules stay identical for both directions. The tie-break is deliberately not
reflected: each direction selects its best zone with the same deterministic
rule applied to its own candidate set. Dual qualification is impossible by
construction because the setup bar cannot be simultaneously bullish and
bearish.

Deterministic check precedence is identical to the long spec: missing/stale
bar, insufficient history, gap, activity floor, location, absorption, second
failure, dominance, target/stop validity, cost-adjusted reward/risk.

## 2. Disposition of original requirements (short-specific)

All long-spec deferrals remain deferred (structure regimes, gamma, VAH/VAL,
Fibonacci, sessions, 20,000 contracts, depth, loss lockouts, effort-vs-result
management). Short-specific items:

| Original requirement (source lines) | Disposition | Adaptation or blocker |
|---|---|---|
| Value-down structure (`:63-72`) | Deferred | Same POC-zone proxy as the long spec; no higher-timeframe regime model |
| Prior lows as targets (`:185-190`) | Adapted | Nearest qualifying prior profile node below entry, else prior-window low; source priority (swing points primary) is not reproduced |
| Complete short entry algorithm | Not in source | This spec defines it as a symmetric construction; no equivalence claim |
| Short execution | Deferred (Phase 2) | Alpaca crypto is spot; no short sales, margin, borrow, or locate modeling |

## 3. Deterministic short rules

Let `P = profile_bars` (288). The analysis window is `P + 2` consecutive closed
bars: `prior = bars[-(P+2):-2]`, `setup = bars[-2]`, `confirmation = bars[-1]`.
All long-spec window, gap, and closed-bar requirements apply unchanged.

### 3.1 Location (premium above the zone)

Profile, POC list, cluster detection, and tie-breaks are unchanged and use
`prior` only. The **setup bar** must touch the selected zone and close below
its lower edge:

```text
setup.high >= zone_low
setup.low <= zone_high
setup.close < zone_low
```

Otherwise: `no_poc_zone_rejection`. (Long requires `setup.close > zone_high`;
the two location states are mutually exclusive for the same zone. The parallel
reason name is intentional, not a drift from `no_poc_zone_reaction`.)

### 3.2 First failure / bearish absorption proxy (setup bar)

All conditions must hold on `setup`:

1. `setup.close < setup.open` (bearish close).
2. `body_high = max(setup.open, setup.close)`; `body_high < setup.high`
   (a non-empty upper wick).
3. `setup.footprint["delta"] > 0` (aggressive buyers net dominant).
4. At least one buy-imbalance row reaches the body high (v2 overlap mirror):
   `row_price(row + 1) >= body_high` for some `row` in
   `setup.footprint["buy_imbalance_rows"]`.

Otherwise: `no_bearish_absorption`.

Asymmetry note: the long spec's lower-wick guard is mathematically implied by
its imbalance-row test (floor semantics guarantee `row_price(row + 1) > low`).
The short guard is **not** redundant: a trade exactly at a row edge can satisfy
`row_price(row) >= body_high` with an empty wick, so condition 2 is
independently binding and must be tested.

### 3.3 Second failure / bearish dominance shift (confirmation bar)

All conditions must hold on `confirmation`:

1. Buyers retried: `confirmation.high > confirmation.open` and
   `confirmation.high > setup.close`.
2. Buyers failed lower: `confirmation.high < setup.high`.
   Otherwise: `no_lower_second_failure`.
3. Bearish flip: `confirmation.close < confirmation.open`.
4. Sell dominance: at least one sell-imbalance **stack** exists (a run of
   `stacked_levels` consecutive imbalance rows via `_stacks`) and
   `confirmation.close <= row_price(stack[0] + 1)` (close within the bottom
   row of the stack; the v2 in-stack mirror of the long
   `close >= row_price(stack[-1])`). Otherwise: `no_bearish_dominance_shift`.

### 3.4 Entry, stop, target, costs, sizing

- Entry = `confirmation.close` (signal emitted after that bar closes).
- Stop = `max(setup.high, zone_high) + stop_buffer_rows * row_size`.
- Target = nearest prior profile node below entry (volume >= 10% of profile POC
  and >= both neighboring rows, where an absent neighbor counts as zero; choose
  the highest row whose span ends strictly below entry), escalating to the
  minimum prior-window low when the node fails the cost-adjusted reward/risk
  gate (`target_source` is `prior_profile_node` or `prior_low`).
- Require `0 < target < entry < stop`.
- Short cost model (mirror of the long model; adversarial on both sides):

```text
proceeds      = entry * (1 - slip) * (1 - fee)
stop_buyback  = stop   * (1 + slip) * (1 + fee)
target_buyback= target * (1 + slip) * (1 + fee)
risk_per_unit = stop_buyback - proceeds
reward_per_unit= proceeds - target_buyback
net_reward_risk = reward_per_unit / risk_per_unit
expected_cost_fraction = ((entry - proceeds) + (target_buyback - target)) / entry
```

- Require `risk_per_unit > 0` and
  `net_reward_risk >= min_net_reward_risk`.
- Signal reason: `absorption_second_failure_confirmation`; the signal payload
  gains `"direction": "short"` (long signals carry `"direction": "long"`).
  The deterministic ID hashes config (including direction), bar end, entry,
  stop, and target.
- Sizing, two variants mirroring the code:
  - Library `position_size`: `quantity = min(equity * risk_fraction /
    risk_per_unit, max_notional / entry_fill)` where
    `entry_fill = entry * (1 - slip)` for shorts.
  - Replay entry branch: mirrors `strategy.py:424-427` with
    `available_risk = min(cash * risk_fraction, remaining daily-loss budget)`,
    `quantity = min(available_risk / risk_per_unit, max_notional / fill)` where
    `fill = bar.open * (1 - slip)`.
  - Deviation from the long replay, stated deliberately: there is no
    `cash / (fill * (1 + fee))` cap because a short credits cash; no margin,
    borrow, or locate model exists and the notional cap stands in for
    collateral.

### 3.5 Activity floor

Unchanged from the long spec (setup, confirmation, and aggregate prior
floors). Otherwise: `insufficient_trade_activity`.

### 3.6 Replay mirror (bounded)

Buyback arithmetic for every short exit: `fill = reference * (1 + slip)`,
`cost = quantity * fill`, `fee = cost * fee`, `cash -= cost + fee`.

- Entry at the next bar's first trade: `fill = bar.open * (1 - slip)`,
  `proceeds = quantity * fill`, `fee = proceeds * fee`,
  `cash += proceeds - fee`.
- Mark-to-market: `cash - quantity * price * (1 + slip) * (1 + fee)`.
- Stop-first resolution when stop and target are both touched in one bar.
- Stop: if `bar.high >= effective_stop`, reference =
  `max(bar.open, effective_stop)` in the buyback arithmetic.
- Target: if `bar.low <= target`, reference = `target`.
- Daily loss: liquidation price solves `mark <= day_start * (1 - d)`, giving
  `daily_price = (cash - day_start * (1 - d)) / (quantity * (1 + slip) * (1 + fee))`;
  `effective_stop = min(stop, daily_price)` and
  `daily_exit = daily_price < stop` (the price-rising mirror of the long
  replay's `daily_price > position["stop"]`). A daily exit halts the replay day.
- Holding limit: buy back at `bar.close` using the buyback arithmetic.
- End of data: buy back at the final closed bar's close.
- One venue, OHLC, no queue, no cross-venue fills.

## 4. Configuration and guards

| Setting | Change | Notes |
|---|---|---|
| `direction` | **new** `StrategyConfig` field: `"long"` (default), `"short"`, or `"both"` | Validated in `__post_init__`; invalid values raise |
| `config.toml [strategy]` | add `direction = "long"` | Shipped default preserves current behavior |
| Warmup / window | unchanged | `P + 2` applies to both directions |
| Risk, fees, slippage, caps | unchanged | Phase 1 is research-only |

Fail-closed guards (mandatory):

- `execution_gate` returns `(None, "short_execution_unavailable")` for any
  signal whose direction is not `"long"`; a missing direction defaults to
  `"long"` for backward compatibility. Placement is specified: the check must
  occur after the `if not signal` step (`cli.py:110-111`) and before the price
  geometry check (`cli.py:114`), otherwise a short signal would first fail
  `invalid_entry_stop_target` and the promised reason would never be emitted.
- `run --mode paper`, `smoke`, and `paper-test` must refuse to start when
  `strategy.direction != "long"`, with an explicit error naming the spot-venue
  limitation. Placement is specified: in `main()` before `load_env()`,
  `PaperClient()`, or the execution lock is constructed (`cli.py:275-279`),
  using `getattr(args, "mode", None)` so non-run commands are unaffected.
- `observe` and `backtest` may run `long`, `short`, or `both`.
- With `direction = "both"`, long is evaluated first and, if it signals, it is
  authoritative; short is evaluated otherwise. Dual qualification is
  unreachable (a setup bar cannot be both bullish and bearish); the fixed order
  exists purely for determinism.
- Documented backstop, independent of the new guards: the executor's entry
  geometry (`0 < stop < ask < target`, `execution.py:436`) is unsatisfiable for
  a short signal, `_start` submits only `side: "buy"` (`execution.py:401`), and
  ownership recovery refuses negative quantities (`execution.py:226-227`).
  Three layers must all fail before any short order could be placed.

## 5. Rejection reason vocabulary

All long-spec reasons, plus: `no_poc_zone_rejection`, `no_bearish_absorption`,
`no_lower_second_failure`, `no_bearish_dominance_shift`,
`short_execution_unavailable`.

## 6. Safety invariants

- Phase 1 submits no orders of any kind for shorts under any mode.
- Long behavior with `direction = "long"` is regression-locked by tests.
- Existing risk caps, one-position rule, and paper-only routing are unchanged.
- The running 24-hour observation is unaffected; no shared mutable state is
  introduced.

## 7. Acceptance criteria and required tests

| Criterion | Required test |
|---|---|
| Deterministic short signal | `test_synthetic_short_setup_generates_deterministic_signal` |
| Input order independence | same test (reversed tape equality) |
| Future trades cannot change a past decision | `test_short_future_trades_cannot_change_past_signal` |
| Setup bar alone insufficient | `test_short_setup_bar_alone_is_insufficient` |
| Location requires close below zone | `test_short_location_requires_break_below_zone` |
| Bearish close, delta, and wick-imbalance conditions | `test_bearish_absorption_conditions` (isolated fixtures per condition) |
| Buy imbalance in the body rejected | `test_bearish_absorption_imbalance_in_body_rejected` |
| Upper-wick guard independently binding | `test_upper_wick_guard_binding_at_row_edge` (row-edge equality, empty wick) |
| Second failure pinned (retry above open/setup close; high below setup high) | `test_lower_second_failure_conditions` |
| Dominance pinned (bearish close; close below bottom edge) | `test_bearish_dominance_requires_sell_stack`, `test_bearish_dominance_requires_bearish_close` |
| Sell-stack absence rejected | `test_bearish_dominance_requires_sell_stack` (no consecutive run) |
| `close == row_price(stack[0])` qualifies | `test_sell_stack_boundary_qualifies` |
| Target below entry and stop above entry | `test_short_stop_target_geometry` |
| Profile-node-below target and prior-low fallback | `test_short_target_selection` |
| Costs can reject a positive gross short | `test_short_cost_hurdle` |
| Short sizing (risk-per-unit, notional cap, no cash term) | `test_short_position_size_includes_notional_cap` |
| Signal payload carries `direction` | `test_short_signal_payload_carries_direction` |
| Long decision fields unchanged with `direction="long"` | `test_long_path_unchanged_by_direction_support` |
| `both` mode evaluates the qualifying side | `test_both_mode_selects_qualifying_side` |
| Invalid direction rejected | `test_direction_validation` |
| Paper modes fail closed for short/both | `test_paper_modes_reject_short_direction` |
| Gate rejects non-long signals | `test_gate_rejects_short_execution` |
| Gate defaults a missing direction to long | `test_gate_missing_direction_defaults_to_long` |
| Replay entry timing and costs | `test_short_replay_enters_only_after_signal` |
| Replay stop-first when both touched | `test_short_stop_wins_when_both_bounds_touched` |
| Replay stop gap fills at worst open | `test_short_stop_gap_fills_at_worse_open` |
| Replay entry gap invalidates reward | `test_short_entry_gap_invalidates_reward` |
| Replay daily-liquidation price pin | `test_short_daily_loss_limit_caps_entry_risk` |
| Replay holding limit | `test_short_replay_max_holding_closes_position` |
| Replay end-of-data buyback includes fee | `test_short_end_of_data_buyback_includes_fee` |
| Profile excludes setup/confirmation | `test_short_profile_excludes_setup_and_confirmation` |
| Activity, gap, and 4:1 boundary rules | reuse long fixtures with mirrored direction |
| Warmup window unchanged | existing `test_aligned_start_requests_two_extra_bars` |

For every short test, the equivalent long test remains in the suite and must
stay green.

## 8. Explicit non-goals

No execution enablement, no Alpaca short support, no futures/margin/borrow
modeling, no venue change, no gamma/depth/session/Fib/value-area regimes, no
loss lockouts, no short performance claims, and no equivalence claim with the
source digest. Deferred long-spec items remain deferred for shorts.

## 9. Implementation status

Phase 1 **implemented** on 2026-10-12 (111 tests passing): mirrored
`_analyze_short`, `_replay_short`, `_short_costs`, `short_position_size`, the
`direction` config field (long/short/both with "both" long-first in analysis,
and "both" rejected by replay), `optimize --direction`, `backtest --direction`,
the `short_execution_unavailable` gate guard placed before price geometry, and
paper-mode refusals for non-long directions. `config.toml` stays `direction =
"long"` because Alpaca crypto cannot short.

Phase 2 (execution) requires a separate venue decision and a new specification.

Six-month flipped comparison (Apr 13 - Oct 12, 2026; same windows, same $100
notional caps): short signals 21 / trades 19 / net -$8.12 vs long signals 37 /
trades 30 / net -$20.44. The flip halves the loss and nearly doubles the win
rate, but every month is still non-positive; the 6-month short grid selection
is recorded in `data/optimization-6mo-short.log` and `data/optimization-6mo.json`.

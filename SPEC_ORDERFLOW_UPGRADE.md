# Specification: Absorption + Second-Failure Upgrade (BTC/USD Adaptation)

Status: approved for implementation on 2026-10-12. Implementation and audit
status are recorded in the "Implementation status" section at the end of this
document and in [TEST_RESULTS.md](TEST_RESULTS.md).
Source strategy digest: [Champion_Orderflow_Strategy.md](Champion_Orderflow_Strategy.md).
Target engine: the existing BTC/USD order-flow paper prototype (see [README.md](README.md)).

## 0. Purpose and honesty statement

This specification converts a **deterministic subset** of the source digest into
engine changes. It does **not** reproduce the MNQ futures strategy, and it does
not claim the source method's performance. Every original requirement is
disposed of as one of three categories:

- **Adapted**: implemented with a clearly labeled BTC/USD proxy,
- **Kept**: existing engine behavior retained without change,
- **Deferred**: not implemented, with a stated blocker.

The core characteristic element being implemented is the digest's confirmation
sequence (`Champion_Orderflow_Strategy.md:147-169`): aggressive sellers absorbed
at a location (footprint imbalance in the lower wick of a bullish bar), followed
by a higher seller retry that fails, followed by a bullish dominance shift.

The adaptation is materially weaker than the source description in three
places, and these deviations are deliberate:

1. "Large lower wick" and "all the volume / POC / negative delta concentrated at
   the bottom" (`:147-153`) become: non-empty lower wick, at least one 4:1
   sell-imbalance row wholly at or below the body low, and negative whole-bar
   delta. Wick-size share, POC-at-low, and volume-share thresholds are not
   implemented.
2. "Immediately pulls back" (`:160`) cannot be verified from closed bars; it
   becomes `confirmation.low < confirmation.open` and
   `confirmation.low < setup.close`. Intrabar ordering is not modeled.
3. Buy-side imbalance is "optionally" confirming in the source (`:178`); here a
   buy-imbalance stack plus a close above it is a **mandatory** dominance gate.

## 1. Disposition of original requirements

| Original requirement (source lines) | Disposition | Adaptation or blocker |
|---|---|---|
| Entry is context + location + confirmation (`:38-55`) | Adapted | Location = existing repeated-POC zone; environment filters deferred |
| 1H/4H structure, value-up/down/balanced (`:63-72`) | Deferred | Requires multi-week warmup and an unspecified pivot policy; separate experiment |
| Gamma regime, walls, flip (`:74-95`) | Deferred | No options/GEX data source exists in this stack |
| Daily/cash-session value area, discount vs premium (`:101-110`) | Deferred | VAH/VAL are not implemented; POC zone remains the location proxy |
| Fibonacci golden pocket 0.705/0.788/0.886 (`:117-130`) | Deferred | Causal swing selection and "clean internal structure" are undefined |
| 5-minute footprint, volume and delta profiles (`:138-145`) | Adapted | Bars become 5 minutes; footprint already exists |
| Absorption at extremes (`:147-153`) | Adapted (weakened) | Deterministic lower-wick sell-imbalance + negative delta proxy; cannot prove passive absorption; concentration thresholds dropped (see §0.1) |
| "Try, fail, try again, fail" second failure (`:155-169`) | Adapted (weakened) | Two closed bars: setup (first failure) + confirmation (higher second failure); "immediately" not verifiable (see §0.2) |
| 400% imbalance flag (`:166`) | Adapted | `imbalance_ratio = 4.0` in `config.toml` |
| Buy-side confirmation (`:178`) | Adapted (strengthened) | Optional in source; mandatory 3-row buy stack in §3.3 (see §0.3) |
| Stop beyond failed sellers (`:180-183`) | Adapted | One row below the setup bar low and zone, whichever is lower |
| Targets = swing points, POC secondary (`:185-190`) | Adapted (inverted) | Engine target = nearest qualifying prior profile node (POC-like), else prior-window high. The source's primary/secondary target ordering is inverted; not claimed as reproduced |
| Trailing into clusters (`:189`) | Deferred | Open positions are monitored from quotes, not closed footprints |
| ~1.5R-2R typical outcome (`:185-191`) | Not a filter | Digest figure is a realized distribution, not an entry threshold; `min_net_reward_risk` stays 1.2 |
| Effort vs. result management (`:198-206`) | Deferred | Open positions are monitored from quotes, not closed footprints |
| 20,000 MNQ contracts per 5-min candle (`:212-217`) | Deferred | Not transferable to venue BTC volume; 20 trades/5-min remains a data-quality floor |
| NY-open session window (`:214-217`, `:262-266`) | Deferred | BTC trades 24/7; session adaptation needs separate validation |
| Stop after two losses / one mistake halts (`:262-266`) | Deferred | Requires durable realized-loss accounting; not implemented in this change |
| MNQ/NQ instrument and contract sizing (`:212-227`, `:331`) | Deferred | Instrument is BTC/USD spot on Alpaca; futures stack is out of scope |
| Level-2 clustered-order targets (`:185-189`) | Deferred | No depth data source |
| Short-side mirror | Deferred | Engine and executor are deliberately long/flat |

## 2. Data contract (unchanged)

- Signal venue: `us-1` (Kraken US via Alpaca). Execution venue: Alpaca US `us`.
- Only signed trades with explicit aggressor side `B`/`S` construct footprints.
- Venues are never mixed. Trades are deduplicated and ordered causally.
- A bar is usable only when fully closed (`build_bars` semantics).
- Missing populated intervals inside the analysis window block the decision.
- All timestamps are UTC. Bars are half-open `[start, end)`.

Known late-data limitations, deliberately out of scope for this change:
the live loop evaluates with a watermark and overlapping REST downloads; late
trade reporting can change reconstructed bars; later downloads can revise
history; the store keeps the normalized trade record rather than full vendor
payloads. The decision journal preserves observations as made. The causality
test in §7 proves that appending future trades cannot change an earlier
decision; it does not prove immutable decision inputs under revision.

## 3. Deterministic rules

Let `P = profile_bars` (288). The analysis window is `P + 2` consecutive closed
bars: `prior = bars[-(P+2):-2]`, `setup = bars[-2]`, `confirmation = bars[-1]`.
If fewer than `P + 2` bars exist, or the last closed bar is not the latest
expected closed bar, or the window has a gap, no decision is made.

Check precedence is fixed and tests depend on it:
missing/stale bar, insufficient history, gap, activity floor, location,
absorption, second failure, dominance shift, target/stop validity, cost-adjusted
reward/risk. The first failing check determines `reason`.

### 3.1 Profile and location (reused, applied to the setup bar)

1. Build the rolling profile and per-bar POC list from `prior` only.
2. Find POC clusters: a center row with at least `minimum_cluster_bars` prior
   bar POCs within `zone_rows`; zone is `[center - zone_rows, center + zone_rows + 1)`
   in row prices.
3. The **setup bar** must touch the selected zone and close above its upper
   edge: `setup.low <= zone_high and setup.high >= zone_low and setup.close > zone_high`.
   Otherwise: `no_poc_zone_reaction`.
4. Candidate selection prefers the cluster with the most bars, then the most
   clustered volume, then the lowest center (deterministic tie-break).

This stands in for the digest's "location" stage. It is a proxy, not a value
area or Fibonacci location.

### 3.2 First failure / absorption proxy (setup bar)

All conditions must hold on `setup`:

1. `setup.close > setup.open` (bullish close).
2. `body_low = min(setup.open, setup.close)`; `body_low > setup.low`
   (a non-empty lower wick).
3. `setup.footprint["delta"] < 0` (aggressive sellers net dominant).
4. At least one sell-imbalance row lies wholly at or below the body low:
   `row_price(row + 1) <= body_low` for some `row` in
   `setup.footprint["sell_imbalance_rows"]`.

Otherwise: `no_bullish_absorption`. Imbalance rows already require the
configured diagonal ratio, minimum row volume, and an observed adjacent row
(`StrategyConfig.imbalance_ratio`, `minimum_level_volume`). This is labeled a
proxy: it detects executed aggression at the low, not resting liquidity.
Condition 2 is mathematically implied by condition 4 whenever a qualifying
imbalance row exists (`row_price(row + 1) > setup.low` for observed rows), and
is kept as an explicit guard.

### 3.3 Second failure / dominance shift (confirmation bar)

All conditions must hold on `confirmation`:

1. Sellers retried: `confirmation.low < confirmation.open` and
   `confirmation.low < setup.close`.
2. Sellers failed higher: `confirmation.low > setup.low`.
   Otherwise: `no_higher_second_failure`.
3. Bullish flip: `confirmation.close > confirmation.open`.
4. Buy dominance: at least one buy-imbalance **stack** exists (a run of
   `stacked_levels` consecutive imbalance rows per `_stacks`) and
   `confirmation.close >= row_price(stack[-1] + 1)` (close above the top row of
   the stack). Otherwise: `no_bullish_dominance_shift`.

### 3.4 Entry, stop, target, costs

- Entry = `confirmation.close` (signal is emitted after that bar closes).
- Stop = `min(setup.low, zone_low) - stop_buffer_rows * row_size`.
- Target = nearest prior profile node above entry (volume >= 10% of profile POC
  and >= both neighboring rows, where an absent neighboring row counts as zero),
  else the maximum prior-window high. This inverts the source's target
  priority; see §1.
- Require `0 < stop < entry < target` and cost-adjusted
  `net_reward_risk >= min_net_reward_risk` using the existing `_costs` model.
- Signal reason: `absorption_second_failure_confirmation`.
- Signal ID hashes config, bar end, entry, stop, and target.

### 3.5 Activity floor

- `setup.trade_count >= minimum_bar_trades`
  and `confirmation.trade_count >= minimum_bar_trades`
  and `sum(prior trade counts) >= profile_bars * minimum_bar_trades`.
- Otherwise: `insufficient_trade_activity`. This is a data-quality floor, not
  the digest's contract-volume filter.

### 3.6 Replay signal parity (bounded)

- Replay analyzes the same window (`P + 2` bars) and enters at the next bar's
  first trade after the confirmation bar closes.
- Stop-first resolution when both stop and target are touched in one bar
  (unchanged, conservative).
- Replay remains single-venue, OHLC-based, fee/slippage-aware, long-only, one
  position, daily-loss limited, holding limited. It does not model event-level
  intrabar order flow, queue position, or cross-venue fills, and it cannot
  validate intrabar management rules.

## 4. Configuration changes

| Setting | Old | New | Rationale |
|---|---|---|---|
| `bar_seconds` | 900 | 300 | Digest uses 5-minute footprints (`:138-145`) |
| `profile_bars` | 96 | 288 | Preserves the 24-hour rolling profile |
| `imbalance_ratio` | 3.0 | 4.0 | Digest's 400% imbalance (`:166`) |
| `max_holding_bars` | 32 | 96 | Preserves the 8-hour hold limit |
| `StrategyConfig.imbalance_ratio` default | 3.0 | 4.0 | Aligns library default with specification |

Unchanged: `row_size`, `stacked_levels`, `minimum_level_volume`,
`minimum_bar_trades`, `zone_rows`, `minimum_cluster_bars`,
`minimum_node_fraction`, `min_net_reward_risk`, fees, slippage, risk caps,
`daily_loss_fraction`, `stop_buffer_rows`. Warmup expands from `P + 1` to `P + 2`
bars in `cli.aligned_start` and the replay observation slice.

Cost-model caveat: BTC proportional fees, basis-point slippage, and the $100
notional cap are not transformed into MNQ commissions, tick values, or margin.
No futures cost equivalence is claimed.

Library-default note: other `StrategyConfig` dataclass defaults
(`bar_seconds=900`, `profile_bars=96`, `max_holding_bars=32`) intentionally
remain at their historical values for direct library callers; all production
runs are driven by `config.toml`, which sets the new values.

## 5. Rejection reason vocabulary

`insufficient_history`, `missing_latest_closed_bar`, `gaps_in_profile`,
`insufficient_trade_activity`, `no_poc_zone_reaction`, `no_bullish_absorption`,
`no_higher_second_failure`, `no_bullish_dominance_shift`,
`no_valid_target_or_stop`, `insufficient_reward_after_costs`,
`incomplete_download_coverage` (live gate).

## 6. Safety invariants (unchanged)

- Paper endpoint only; live URLs and live keys rejected.
- Notional <= $100, risk <= 0.25% per entry, daily loss <= 1%.
- One position at a time; durable order ownership; no unowned positions.
- No signal is a valid outcome; thresholds are never relaxed to force trades.

## 7. Acceptance criteria and required tests

| Criterion | Required test (to add or update) |
|---|---|
| Deterministic two-stage signal | `test_synthetic_setup_generates_deterministic_signal` (rewritten) |
| Input order does not change output | same test (reversed tape equality) |
| Future trades cannot change a past decision | `test_future_trades_cannot_change_past_signal_or_profile` |
| No signal from a setup bar alone | `test_setup_bar_alone_is_insufficient` (new) |
| Absorption requires imbalance in the wick | `test_absorption_imbalance_in_body_rejected` (new) |
| Absorption requires lower wick and bearish delta | `test_absorption_requires_lower_wick_and_delta` (new) |
| Second failure must be higher | `test_second_failure_must_hold_above_setup_low` (new) |
| Confirmation must pull back below open | `test_confirmation_must_pull_back_below_open` (new) |
| Dominance shift requires a buy stack | `test_dominance_shift_requires_buy_stack` (new) |
| Profile excludes setup/confirmation | `test_profile_excludes_setup_and_confirmation` (new) |
| 4:1 ratio boundary | `test_four_to_one_imbalance_boundary` (new) |
| Gaps block decisions | `test_profile_gap_blocks_signal` (updated fixtures) |
| Activity floor blocks decisions | `test_activity_threshold_and_fee_hurdle_block_signal` (updated fixtures) |
| Diagonal comparison fixture | `test_diagonal_not_horizontal_comparison` (pin ratio explicitly) |
| Next-bar entry, costs, stop-first, gap fills, daily limit, holding | existing replay tests (fixtures shifted to the 5-bar setup) |
| Closed bars only | `test_closed_bars_have_no_future_or_partial_trades` |
| Warmup requests `P + 2` bars | `test_aligned_start_requests_two_extra_bars` (new) |

## 8. Explicit non-goals

No gamma, depth, MNQ equivalence, session windows, Fib/value-area location,
loss lockouts, effort-vs-result management, short side, futures cost
equivalence, or immutable decision-input versioning. See the disposition table.
This upgrade is unvalidated; the 24-hour observation run is data collection,
not evidence of an edge.

## 9. Implementation status

- Implementation: complete. The rules in §3 are implemented in
  `tred/strategy.py`, `tred/cli.py`, `config.toml`, `tests/test_strategy.py`,
  `tests/test_integration.py`, and `README.md`.
- Verification: 79 tests pass. Each gate condition was checked by mutation
  (temporary scratch copies only): absorption gate, second-failure gate, buy
  stack, bullish close, setup-close retest, replay window, and warmup window
  removals are all caught by the suite; the explicit lower-wick guard is a
  documented no-op implied by the imbalance-row condition.
- Replay: `data/replay-20261012T014558Z.json` reports 612 closed bars, zero
  signals, and zero trades under the new rules. No signal is a valid outcome.
- The 24-hour observation run is `python3 -m tred run --mode observe --seconds 86400`
  (no orders; data collection only), started 2026-10-12T01:56:41Z, logged to
  `data/observe-24h.log`. Outcomes are recorded in
  [TEST_RESULTS.md](TEST_RESULTS.md).

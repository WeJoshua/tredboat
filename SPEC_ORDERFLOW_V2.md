# Specification Amendment V2: Overlap Absorption, In-Stack Dominance, Target Escalation

Status: implemented and verified on 2026-10-12 (86 tests). This amendment
supersedes the corresponding rules of [SPEC_ORDERFLOW_UPGRADE.md](SPEC_ORDERFLOW_UPGRADE.md)
(which remains as the v1 historical record). The short-side mirror
[SPEC_SHORT_UPGRADE.md](SPEC_SHORT_UPGRADE.md) is implemented for research;
short broker execution remains pending a short-capable venue.
The subsequent discipline/resource changes are documented in
[UPGRADE_STATUS.md](UPGRADE_STATUS.md).

## 1. Why v2 exists

The v1 gates produced **zero executable signals in 10 days** (~1.14M recorded
BTC/USD trades, 2,882 five-minute bars). The funnel over Oct 2-12:

| Stage | Bars reaching it | Outcome |
|---|---|---|
| Location (POC-zone reaction) | ~1,590 | ~1,090 rejected |
| Absorption (v1 strict wick) | ~500 | ~1,350-1,450 rejected |
| Second failure | ~90 | ~20-55 rejected |
| Dominance (above whole stack) | ~50 | ~33-104 rejected |
| Target/cost gate | ~12 | **12/12 negative net RR** (-0.59 to -0.76) |

Economic diagnosis: v1 entries occur after a 5-minute rally already at the top
of the 24h profile; the nearest prior profile node above entry sits only
$10-35 away, so `net_reward_risk` is negative by construction and the cost gate
correctly rejects every setup. That gate must stay; the fix is to give setups a
meaningful target, not to weaken the gate.

## 2. Rule changes (v1 -> v2)

### 2.1 Absorption: sell-imbalance row overlaps the lower wick (v1 -> stricter overlap)

`strategy.py:_sell_absorption`. Condition 4 becomes:

```text
row_price(row) <= body_low   for some sell-imbalance row
```

(v1 required `row_price(row + 1) <= body_low`.) A row whose lower edge is at or
below the body low now qualifies. The lower-wick guard
(`body_low > setup.low`), the bearish-delta requirement, and the 4:1 ratio are
unchanged. This widens absorption from ~500 to ~1,580 surviving bars in the
funnel while keeping "sellers aggressive at the low" semantics.

### 2.2 Dominance: close within the top row of the buy stack (v1 -> in-stack)

`strategy.py:_bullish_dominance_shift`:

```text
confirmation.close >= row_price(stack[-1])   for some 3-row buy stack
```

(v1 required `row_price(stack[-1] + 1)`, i.e., above the whole stack.) The
stack requirement (three consecutive 4:1 imbalance rows) and the bullish close
are unchanged.

### 2.3 Target: escalation node -> prior window high (swing proxy)

`strategy.py:_analyze_bars`:

1. `node` = nearest qualifying prior profile node above entry (unchanged
   qualification: >= 10% of profile POC volume, >= both neighbors).
2. If the node yields `net_reward_risk >= min_net_reward_risk`, it is the
   target (`target_source = "prior_profile_node"`).
3. Otherwise the target escalates to the prior window high
   (`target_source = "prior_high"`), which is the digest's swing-point proxy
   ("primary target: swing points", `Champion_Orderflow_Strategy.md:185-190`).
4. If the escalated target still fails the cost-adjusted RR gate, the setup is
   rejected and `insufficient_reward_after_costs` is reported.

Measured on the v2 candidate set (Oct 2-12, one v2 parameterization): the 12
surviving setups had RR -0.59..-0.76 against the node; against the prior window
high, 3 of 12 reached RR >= 1.2 and 5 >= 0.9.

## 3. Configuration and grid

`config.toml` and defaults are unchanged except where the optimization selects
new parameter values (see [TEST_RESULTS.md](TEST_RESULTS.md)). The optimization
grid widens the reward/risk dimension to `min_net_reward_risk in {1.0, 1.2}`;
values below 1.0 are deliberately excluded because the Oct data showed
sub-1.0 configurations only trade negative-expectancy setups.

## 4. Selection protocol (avoid overfitting)

- Training window: Oct 2 02:20 - Oct 9 02:20 UTC.
- Validation window: Oct 9 02:20 - Oct 12 02:30 UTC (out-of-sample).
- Eligibility: signal_count >= 2, trade_count >= 2,
  max_drawdown_fraction <= 0.03; rank by net P&L, then profit factor, then
  drawdown.
- A configuration is promoted to `config.toml` only if it also performs
  non-negatively out-of-sample.

## 5. Acceptance criteria and tests

Existing v1 tests remain valid (all v2 changes are strict supersets at the
fixture level and are pinned additionally by):

- `test_target_escalates_to_prior_high_when_node_too_close`
- `test_target_without_swing_escalation_rejected`
- `test_profile_node_target_selected_when_qualifying` (node path pinned)
- grid constraint test pinning `min_net_reward_risk in {1.0, 1.2}`

## 6. Documented limitations

- BTC 5-minute tape differs from MNQ futures flow; the pattern remains rare
  (~12 candidates in 10 days under v1; under v2, 20 of 48 parameterizations
  traded a handful of setups in the Oct 2-9 window), so parameter selection is
  sample-limited.
- Target escalation uses the 24h prior window high, not causally confirmed
  higher-timeframe swing points (deferred in v1).
- No gamma, sessions, depth, or MNQ equivalence. Short signals/replays now
  exist; broker execution remains long-only. The subsequent upgrade adds
  two-loss lockouts and a manual mistake-halt request; see UPGRADE_STATUS.md.

## 7. Optimization outcome (2026-10-12)

- Training (Oct 2-9, 48-config grid): 20 configurations traded; **all lost**
  (net P&L -$0.59..-$2.95, profit factor 0.0, zero winners). Minimum-loss
  selection: row_size=12.5, imbalance_ratio=4.0, zone_rows=0,
  minimum_cluster_bars=2, min_net_reward_risk=1.2 (3 signals, 2 trades,
  -$1.19).
- Validation (Oct 9-12, out of sample): **0 signals for every configuration**,
  including the selected one.
- Promotion decision: none. Per §4 a configuration is promoted only with
  non-negative out-of-sample performance; zero OOS activity does not justify
  adopting in-sample losers. `config.toml` therefore keeps the v1 defaults
  (row_size=25, zone_rows=2, minimum_cluster_bars=3, min_net_reward_risk=1.2)
  under the v2 gates.
- Reports: `data/optimization-20261012T025818Z.json` (train),
  `data/optimization-20261012T030312Z.json` (test).

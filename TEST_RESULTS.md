# Paper test results

## Flipped (short) direction: implementation and six-month comparison

Phase 1 of the mirrored strategy was implemented on 2026-10-12 (111 tests
passing): `direction` config field, mirrored analysis and replay, short cost
model, `optimize`/`backtest --direction`, and fail-closed execution guards
(Alpaca crypto is spot; shorts are research-only). See
[SPEC_SHORT_UPGRADE.md](SPEC_SHORT_UPGRADE.md).

Six-month head-to-head (Apr 13 - Oct 12, 2026, identical windows and $100
notional caps):

| Direction | Signals | Trades | Net P&L | Fees | Win rate | Worst month |
|---|---|---|---|---|---|---|
| Long | 37 | 30 | **-$20.44** | $14.99 | ~10% | -$6.02 (May) |
| Short (flip) | 21 | 19 | **-$8.12** | $9.50 | ~21% | -$2.59 (Jun) |

Flipping the logic halves the modeled loss and nearly doubles the win rate,
but the flipped strategy is still non-positive in every month. Reports:
`data/replay-6mo-combined.json` (long) and `data/replay-6mo-short.json`.

Subsequent 6-month short-direction grid selection (train Apr 13 - Aug 11, test
Aug 11 - Oct 12) is recorded in `data/optimize-6mo.json`; see the
`optimize-6mo-short.log` worker output.

## Six-month backtest (April 13 - October 12, 2026)

Data: **11,204,148 additional BTC/USD trades** fetched (Kraken US via Alpaca,
24-hour windows, resume-aware), 12.35M stored in total. Replay run in seven
memory-bounded 30-day chunks (`backtest --start/--end`); each chunk replays
approximately $10,000-equity from flat, so the combined P&L is a sum of
independent chunk outcomes, not a continuous equity curve.

| Month window | Signals | Trades | Net P&L | Win rate |
|---|---|---|---|---|
| Apr 13 - May 13 | 2 | 2 | -$1.90 | 0% |
| May 13 - Jun 12 | 18 | 13 | -$6.02 | 31% |
| Jun 12 - Jul 12 | 7 | 6 | -$4.81 | 17% |
| Jul 12 - Aug 11 | 1 | 1 | -$0.95 | 0% |
| Aug 11 - Sep 10 | 4 | 4 | -$3.99 | 0% |
| Sep 10 - Oct 10 | 5 | 4 | -$2.76 | 0% |
| Oct 10 - Oct 12 | 0 | 0 | $0.00 | n/a |
| **Total** | **37** | **30** | **-$20.44** | **~10%** |

- Modeled fees: $14.99; net expectancy ≈ -$0.68 per executed trade; profit
  factor < 1 in every chunk. Max chunk drawdown <= 0.08%.
- Reason funnel per chunk dominated by `no_poc_zone_reaction` and
  `no_bullish_absorption` (~80% of evaluated bars), with 1-11 setups per month
  reaching the cost gate.
- Six-month reading: the v2 strategy trades rarely (~5 signals/month) and is
  net-negative in every month under the supported configuration. This
  corroborates the 10-day optimization conclusion and motivates the 6-month
  grid selection below.
- Reports: `data/replay-6mo-combined.json` and the seven
  `data/replay-20261012T040*.json` files. To reproduce a single window:
  `python3 -m tred backtest --start 2026-05-13T03:10:00Z --end 2026-06-12T03:10:00Z`.

## Six-month optimization (short direction, in progress)

48-config grid over three memory-bounded windows (train Apr 13 - Aug 11, test
Aug 11 - Oct 12) with `optimize --direction short`, combining per-config P&L.
Selection rule unchanged (v2 §4: promote only with non-negative out-of-sample
performance). Worker log: `data/optimize-6mo-short.log`; result:
`data/optimization-6mo.json`.

## Upgrade verification (5-minute absorption/second-failure rules)

Completed October 12, 2026 (UTC), following
[SPEC_ORDERFLOW_UPGRADE.md](SPEC_ORDERFLOW_UPGRADE.md) and its v2 amendment
[SPEC_ORDERFLOW_V2.md](SPEC_ORDERFLOW_V2.md).

### Local checks

`python3 -m unittest discover -q`: **111 tests passed** (66 before the upgrade).

New coverage: the two-bar absorption/second-failure sequence, lower-wick
sell-imbalance placement, negative-delta requirement, higher second failure,
buy-stack dominance and bullish-close gates, activity floors, target
escalation (profile node to prior high), replay holding limit, the 4:1
imbalance boundary, setup/confirmation exclusion from the prior profile, the
`profile_bars + 2` warmup window, realized paper P&L recording, `pnl` and
`optimize` commands, and the optimization grid/sort logic.

Mutation checks in a scratch copy: removing any gate condition, the replay
window, or the warmup change produces test failures documented in the earlier
audit; v1 fixtures remain green under the v2 superset rules.

### Optimization (10 days, 1,145,424 recorded BTC/USD trades, Oct 2-12)

The v1 funnel showed zero tradeable setups in 10 days (nearest prior node above
entry paid fewer costs than the stop). v2 changed three gates (wick-overlap
absorption, in-stack dominance, target escalation) per
[SPEC_ORDERFLOW_V2.md](SPEC_ORDERFLOW_V2.md); the cost-adjusted reward/risk gate
itself was never relaxed below 1.0.

- Training window Oct 2-9: 20 of 48 grid configurations produced signals;
  **every traded configuration lost** (net P&L range -$0.59 to -$2.95, profit
  factor 0.0, zero winners). Ranked minimum-loss selection:
  `row_size=12.5, imbalance_ratio=4.0, zone_rows=0, minimum_cluster_bars=2,
  min_net_reward_risk=1.2` with 3 signals / 2 trades / -$1.19.
- Validation window Oct 9-12 (out of sample): **0 signals for all 48
  configurations**, including the selected one.
- Report files: `data/optimization-20261012T025818Z.json` (train) and
  `data/optimization-20261012T030312Z.json` (test).
- Decision: no configuration is promoted to `config.toml` (v2 §4 requires
  non-negative out-of-sample performance; zero OOS activity does not justify
  data-mining in-sample losers). The engine keeps the v1 parameter defaults
  with the v2 gates and the 1.2R filter, which is the supported way to minimize
  loss on this evidence: it trades only setups that survive the cost gate and
  binds every entry to $100 notional, 0.25% risk, and a 1% daily loss cap.

### 24-hour paper session (with P&L logging)

- A 10-hour paper session started by the user (PID 2625632, launched locally)
  finished on schedule ~09:59 UTC; credentials are now read from `.env`
  (paper keys, mode 600, git-ignored).
- The 24-hour paper run (`--mode paper --seconds 86400`) is launched
  automatically when that session releases the execution lock; its log is
  `data/observe-24h.log`'s successor `data/paper-24h.log`, PID in
  `data/paper-24h.pid`, launched by `data/paper-launcher.log`.
- Every closed paper trade is journaled (`kind="pnl"`), stored in the durable
  executor state (`closed_trades`), and reported by `python3 -m tred pnl`.
- The run trades only when the cost-adjusted signal gate passes; with the
  optimization evidence, expects at most a handful of setups; zero is a valid
  outcome.

## First verification (pre-upgrade 15-minute rules)

Completed October 12, 2026, Asia/Manila (October 11 UTC). This run used the
superseded 900-second bars, a 96-bar profile, 3:1 imbalances, and the one-bar
POC-reclaim initiation rule. It is retained as history.

- `python3 -m unittest discover -q`: **66 tests passed** at that time.
- Historical replay: **121,500 BTC/USD trades** from Kraken US (`us-1`) through
  Alpaca over 48 hours; 191 complete bars; 0 qualifying signals/trades; reasons
  were 96 warmup bars, 51 without bullish initiation, 43 without a POC-zone
  reclaim, and 1 rejected after costs.
- Authenticated Alpaca paper test: account ACTIVE, paper endpoint only, no
  preexisting BTC position. A requested $10 paper market buy filled, a native
  stop-limit was acknowledged and canceled, and the actual net quantity was
  sold back to flat, verifying execution plumbing only.
- Bounded 60-second paper session: 5 evaluations, 0 strategy trades, clean
  shutdown, process exit status 0.

These results establish working data collection, paper execution, and P&L
attribution, not reduced losses or profitability. The 24-hour paper session is
a bounded forward experiment, not evidence of an edge.

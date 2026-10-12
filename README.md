# Tred: BTC/USD order-flow paper prototype

A Python 3.11+ command-line prototype for recording signed crypto trades, testing
POC-zone location with two-bar absorption/second-failure confirmation, and
executing **Alpaca paper orders**.
It uses the Python standard library; no package installation is needed.

MNQ/NQ signed-tape research is available end to end: `futures-fetch` exports a
verified Databento GLBX trades tape and coverage manifest (own
`DATABENTO_API_KEY`, free trial credit covers it; never logged or stored), and
`futures-compare` runs long, mirrored short and both flipped variants with
whole-contract risk sizing, CME tick values, explicit fees/margin, and the New
York morning window. See [FUTURES_RESEARCH.md](FUTURES_RESEARCH.md) for the
data contract and bounded command usage.

QQQ research on Alpaca (user-selected `tick_prior_price` side downgrade, since
Alpaca equity prints carry no aggressor flag) is implemented in
`equity-fetch` and `equity-compare` — see
[EQUITY_RESEARCH.md](EQUITY_RESEARCH.md). It is blocked on credentials: the
stored `.env` keys currently return HTTP 401 on every Alpaca endpoint. The
Alpaca-sourced BTC tape in `data/tred.sqlite3` remains usable offline; its
6-month long/short replays are negative for both directions.

The implementation is an exploratory interpretation of
[the course transcript](Master%20Order%20Flow%20Trading%20-%20Full%20Transcript.md)
and a deterministic subset of
[the champion strategy digest](Champion_Orderflow_Strategy.md), not a validated
trading edge. It runs a 5-minute, two-bar absorption/second-failure hypothesis
under the v2 rules in [SPEC_ORDERFLOW_V2.md](SPEC_ORDERFLOW_V2.md); the digest's
MNQ, gamma, value-area, session, and depth requirements are deferred.

## Run

From this directory:

```bash
python3 -m unittest discover -v
python3 -m tred fetch --hours 48
python3 -m tred backtest
python3 -m tred run --mode observe --seconds 60
```

The public historical data commands need no account credentials. Historical
downloads are paginated, persisted, deduplicated, and checked for coverage.
The polling recorder intentionally does not attempt subsecond trading.

To check the account, type credentials into hidden prompts:

```bash
python3 -m tred --prompt-credentials status
```

To verify the paper broker connection with a small round trip, then run the
strategy for a bounded period:

```bash
python3 -m tred --prompt-credentials paper-test --smoke-notional 10 --seconds 60
```

`paper-test` **places paper orders**. Its smoke trade is an infrastructure check,
not a strategy signal, and is not included in local replay results. It buys,
places and acknowledges a native stop-limit 2% below the filled entry, confirms
cancellation of that stop, and sells the actual net quantity back to flat.

The first completed verification is recorded in [TEST_RESULTS.md](TEST_RESULTS.md).

For a longer paper observation session without a forced smoke entry:

```bash
python3 -m tred --prompt-credentials run --mode paper --seconds 3600
```

For a long credential-free observation session (no orders):

```bash
python3 -m tred run --mode observe --seconds 86400
```

At the end of a paper session, the runner attempts to cancel its own pending
orders and flatten only its own position. Inspect the shutdown result. An API
failure, ambiguous order status, or below-minimum residual can require recovery:

```bash
python3 -m tred --prompt-credentials status
python3 -m tred --prompt-credentials flatten
```

Do not delete `data/paper-state.json` to bypass a blocked state: it records which
orders and quantity belong to the bot. The bot refuses preexisting BTC positions
or orders that it cannot establish ownership of. It does not reset accounts.

For repeated runs, an optional `.env` may contain the variables shown in
`.env.example`. Use restrictive local permissions and keep credentials out of Git.
The interactive prompt keeps credentials in memory only. No real key is shipped.

## Initial configuration

Edit `config.toml` to run explicit experiments. The initial defaults are:

| Setting | Value |
|---|---|
| Pair / direction | BTC/USD, long or flat |
| Signal data | Kraken US (`us-1`) through Alpaca |
| Execution | Alpaca paper endpoint only |
| Execution quote reference | Alpaca US (`us`) |
| Signal bars / prior profile | 5 minutes / 288 bars (24 hours) |
| Price-row size | $25 |
| Diagonal imbalance | 4:1 at 3 adjacent rows |
| Minimum aggressive volume per qualifying row | 0.001 BTC |
| Minimum signal-bar trades | 20 |
| Required repeated POCs | 3 prior bars within the configured zone |
| Modeled taker fee | 0.25% per side |
| Modeled adverse slippage | 5 basis points per side |
| Minimum reward/risk after costs | 1.2 |
| Maximum entry notional | $100 |
| Maximum planned equity risk | 0.25% per entry |
| Daily equity loss cutoff | 1% from the session's recorded UTC-day baseline |
| Maximum holding period | 96 bars / 8 hours |
| Consecutive strategy-loss cutoff | 2 per UTC day, persistent across restarts |
| Quote age / spread / venue difference limits | 60 seconds / 15 bps / 30 bps |

The executor enforces the $100, 0.25%, and 1% ceilings. Smaller values are allowed.
The daily baseline is established on the bot's first trading check of the UTC
day; it does not reconstruct losses from earlier trading before that baseline.
All timestamps and day boundaries use UTC.

## The setup

1. Construct each footprint from trades with an explicit `B` or `S` taker side.
   Do not infer flow from candle color. Preserve venue separation.
2. Use only prior, consecutive closed bars for the profile and POC clusters.
3. Treat the older of the last two closed bars as the **setup bar** and the
   newest as the **confirmation bar**. The setup bar must touch a POC cluster
   and close above its upper edge.
4. Require absorption on the setup bar: a bullish close, a non-empty lower
   wick, negative delta, and at least one 4:1 sell-imbalance row whose lower
   edge is at or below the body low. This is an executed-flow proxy, not proof
   of passive absorption.
5. Require the confirmation bar to retry lower than its open and the setup
   close yet hold above the setup low, flip bullish, and close inside the top
   row of a stack of diagonal buy imbalances. Missing price rows cannot
   manufacture zero-denominator imbalances.
6. Place the proposed stop below the lower of the zone and setup-bar low, with
   one price-row buffer. The target is the nearest higher prior profile node if
   it satisfies the cost-adjusted reward/risk; otherwise it escalates to the
   prior window high (swing proxy). Reject when neither pays for costs.
7. Reject setups with insufficient reward after modeled fees and slippage.
8. Before paper entry, recheck both venues' fresh quotes, spread, venue price
   difference, signal age, entry drift, and reward/risk at the current ask.

CVD is recorded as a descriptive feature. CVD divergence is **not** an
implemented entry filter. Market-structure regimes, value-area boundaries,
Fibonacci location, gamma exposure, session windows, and depth clusters
from the digest are **not** implemented; see
[SPEC_ORDERFLOW_V2.md](SPEC_ORDERFLOW_V2.md) for the current rules, the
optimization protocol, and the disposition of each requirement.

Both mirrored short signals/replays (`--direction short`) and contrarian
replays (`--flip`) are implemented. **Broker execution remains long-only**:
Alpaca BTC/USD spot cannot open shorts. Selecting a short-capable demo/paper
venue is required before adding its execution adapter; a short research
signal must never be submitted as a spot sale.

Two consecutive strategy losses now block entries for the rest of the UTC
day, in paper execution and both replay directions. A winning trade resets
the streak before a halt; break-even does not. Once halted, the block stays
until the next UTC day. Smoke trades are excluded. Recorded losses from the
current day are imported when an older execution state is first opened.

To record an execution mistake and request a halt:

```bash
python3 -m tred halt --reason "Entered before confirmation"
```

This writes a separate halt request without credentials or broker calls. An
upgraded running executor processes it on its next step, attempts to flatten
its owned position, and blocks new entries for that UTC day. Confirm the
runner's result; the request itself is not an acknowledgement of liquidation.
It does not diagnose mistakes automatically or implement A/B/C grading.

## P&L and optimization commands

- `python3 -m tred pnl` reports realized paper P&L from the durable executor
  state (closed trades, fees, win rate, profit factor). Every closed paper
  trade is also journaled as `kind="pnl"` during a run.
- `python3 -m tred optimize [--start TS] [--end TS]` grid-searches the
  parameter grid on recorded trades (subject to the resource limits below) and
  writes `data/optimization-*.json`. Use `--start`/`--end` for a train/test
  split; see [SPEC_ORDERFLOW_V2.md](SPEC_ORDERFLOW_V2.md) §4.
- `python3 -m tred backtest` reports model P&L with the same signal engine; it
  is not a validated cross-venue backtest.

CLI `backtest` and `optimize` run sequentially at low priority on one CPU,
with at most 512 MiB address space, 30 additional CPU seconds and 60 seconds
wall time. Database reads stop and reject above 100,000 trades rather than
loading the entire history or silently truncating results. Supply **both**
`--start` and `--end` and shorten the window if a limit is reached. No partial
result is published as a completed backtest. These process limits apply to
CLI research, not direct Python library calls or the paper runner.

Signals expire after 90 seconds. The collector uses a 30-second closed-bar
watermark and overlapping REST downloads. This reduces, but cannot eliminate,
the effect of late trade reporting. The decision journal preserves observations
as made; later downloads can change reconstructed historical data.

## Paper order handling

Trading requests are hard-coded to `https://paper-api.alpaca.markets/v2`; live
URLs, custom URLs, and live-key prefixes are rejected. Redirects are disabled.
Only one process can own an execution state at a time.

Each order intent and client order ID is saved before submission. After an
ambiguous submission the bot reconciles by ID instead of repeating the order.
It checks partial fills and net available quantity, including BTC-denominated
fees, before selling or submitting protection.

Alpaca crypto supports simple market, limit, and stop-limit orders, not native
bracket/OCO or stop-market orders. Strategy entries receive a native stop-limit
plus application-managed target, time, and risk exits. Its stop-limit limit is
0.5% below the trigger. A fast fall can leave that order unfilled; the runner's
market-exit fallback also depends on connectivity. Cancellation must be confirmed
before a replacement exit is submitted.

## What the results mean

`backtest` writes a JSON report under `data/`. It uses the same signal engine and
fills entries at the next bar's first trade with modeled fees and slippage. If a
bar reaches both stop and target, it assumes the stop was hit first. It includes
adverse gap fills, one position at a time, holding limits, and daily loss limits.
Initial partial bars are excluded and incomplete download ranges are rejected.
The final unproven/partial bar is excluded; a remaining position is marked closed
at the last complete bar for reporting.

This is an OHLC execution model built from signed trades, not a historical
order-book fill simulator. Reported drawdown uses closed-bar observations and
can miss worse intrabar drawdown. The local replay models **one venue**: it does
not prove that Kraken signals could obtain the same fills on Alpaca.

Alpaca paper fills also omit important live effects such as queue position and
latency slippage. A completed smoke test proves API plumbing, not profitability.
No signal / no trade is a valid test outcome; thresholds are not relaxed merely
to generate activity.

## Local records

- `data/tred.sqlite3`: venue-tagged trades, sampled quotes, completed download
  intervals, and decision/execution journals.
- `data/paper-state.json`: durable order ownership, risk baseline, and recovery
  state; no credentials.
- `data/replay-*.json`: configuration, model assumptions, trade outcomes, and
  summary metrics for each replay.

## References

- [Alpaca crypto market-data fields and venue feeds](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data)
- [Historical signed trades](https://docs.alpaca.markets/us/reference/cryptotrades-1)
- [Crypto trading fees](https://docs.alpaca.markets/us/docs/crypto-fees)
- [Order types and restrictions](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- [Paper simulation limitations](https://docs.alpaca.markets/us/docs/paper-trading)

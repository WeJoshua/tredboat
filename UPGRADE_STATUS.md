# Execution upgrade status — 2026-10-12

The user requested short execution and upgrades following the champion-system
audit. **Short broker execution is not enabled yet.** The existing integration
executes BTC/USD spot on Alpaca, whose documentation explicitly excludes crypto
short selling: https://docs.alpaca.markets/us/docs/crypto-trading.
The user selected **MNQ/NQ futures** after the initial shared upgrade.
Futures research is now ready for the user's own data:
[FUTURES_RESEARCH.md](FUTURES_RESEARCH.md). No broker was selected or
substituted on the user's behalf.

## Futures data path (`futures-fetch`)

- `futures-fetch` exports a verified signed MNQ/NQ tape and coverage manifest
  from the user's own `DATABENTO_API_KEY` (new accounts include free credits),
  using Databento's public HTTP API directly with the standard-library only.
  The `DATABENTO_API_KEY` is used strictly as HTTP Basic auth, is never logged,
  written to disk, or placed in a URL, and `.env` support was added.
- Pre-flight refuses: invalid/continuous contract symbols, windows over seven
  days or zero length, mixed instrument maps, degraded dataset condition dates,
  missing record counts, empty windows, and ranges above the one-million-record
  budget — all before downloading tape bytes.
- The tape CSV is streamed with byte/record caps; the manifest is marked
  `complete` only when downloaded rows exactly equal Databento's reported
  record count. No-trade gaps above `--gap-seconds` become declared exchange
  closures, so daily CME maintenance halts and quiet stretches stay honest
  instead of falsely blocking or bridging the profile. Published pairs never
  overwrite existing files.
- Manifest and closure budgets were raised (`MAX_CLOSURES` 1024, 1 MiB manifest)
  to fit derived closures. `research_limits` gained a `wall_seconds` override;
  `futures-fetch` runs with 300 seconds wall, other research keeps 60.

## Implemented shared changes

- Durable two-consecutive-loss cutoff, default 2, shared by the paper executor
  and both replay directions. UTC-day reset; break-even preserves the streak;
  a pre-halt win resets it; a reached halt remains sticky for that day.
- Current-day historical strategy outcomes migrate from older execution state.
  Closure accounting is idempotent, including recovery, and smoke outcomes do
  not contribute to the strategy streak.
- `halt --reason TEXT` records an explicit execution mistake through a separate
  atomic request file. The upgraded runner attempts to flatten its owned
  position on its next step and rejects further entries that UTC day. The
  command itself does not submit orders or acknowledge a completed exit.
- Five-minute / 288-bar / 96-holding-bar defaults now agree between the library
  and shipped configuration. Existing 15-minute synthetic fixtures explicitly
  declare their timeframe.
- Replay reports serialize the effective `--direction` and `--flip` settings.
- CLI research uses one low-priority CPU, a 512 MiB address-space ceiling,
  30 additional CPU seconds, and a 60-second wall timeout. Existing stricter
  process limits are respected. SQL reads reject above 100,000 trades.
  Both bounds are required when specifying a replay window. Direct library
  calls are outside the CLI process limits.
- Spot short/flip mode rejection occurs before credentials or storage are
  opened. The executor independently rejects non-long signals, including a
  malformed short signal with long price geometry.

These changes are source changes for the next process start. Existing paper
processes were not restarted; no broker orders or historical replays were run.

## Verification

All **170 offline tests passed** after this round (156 prior + 14 new exporter
tests), including a fake-Databento end-to-end export that the real `load_bars`
reader accepts, budget refusals before download, truncated-download refusal,
no-overwrite publication, degraded-condition refusal, decimal/fixed9 price
detection, HTTP error mapping without the key in messages, and CLI wiring with
300-second wall limits. The full test run took 8.4 seconds elapsed on one CPU.
No historical trade database, broker, or actual Databento request was opened.

## QQQ on Alpaca (user-selected path)

Alpaca confirms no futures (2026 lineup added index options, not futures) and
its spot crypto cannot short; its shortable-equity prints (stocks/ETFs) have
no aggressor flag. The user selected QQQ with the explicit `tick_prior_price`
side-classification downgrade.

- `equity-fetch` exports an Alpaca `/v2/stocks/{symbol}/trades` tape (paged,
  streams, byte/record budgets, gap closures, atomic no-overwrite publish,
  `tred.equity-tape.v1` manifest that **requires** the declared side rule and
  its note; `complete` only on exhausted pagination). Default `--feed iex`
  matches paper entitlements; `sip` needs the paid license.
- `equity-compare` replays long, mirrored short and both flipped variants on
  the shared causal engine with whole-share sizing, explicit commissions,
  the same 09:30–11:00 NY window and 11:00 flatten as futures, the shared
  two-loss/daily-loss discipline, and full tick-rule/limitations disclosure
  in the report.
- 16 new offline tests (tape validation, inference-required manifests, budgets,
  pagination guards, no-overwrite, CLI wiring); **186 tests pass total**.

**Results (keys renewed by the user; real QQQ IEX data)**: two weeks
(Sep 28–Oct 02, Oct 05–09) fetched — 78,798 trades, 1,007 five-minute bars —
and compared with the declared defaults. **Zero signals and zero trades in
all four variants of both weeks**; setups die at the POC-zone and absorption
filters. No QQQ trade exists to evaluate, so no expectancy claim is possible.
The Alpaca-sourced BTC/USD tape (12.36M signed trades, ~6 months) remains the
out-of-sample evidence: long −$20.44 over 30 trades, short −$8.12 over 19
trades, both near the fee floor. IEX prints carry sub-cent prices (749.465),
which the equity reader accepts; cent-multiple rows stay in place for the
footprint.

## Six-month scan with losing-trade failure log

`backtest-scan` replays the Alpaca-sourced BTC tape (2026-04-12..10-12, 12.36M
signed trades) in 244 warmup+eval chunks per direction under `scan_limits`:
nice 19, one CPU, 512 MiB address space, cumulative CPU ceiling. It ran ~13
minutes of wall time and did not steal capacity from the 12-core host.
Results: **long 31 trades / 3 wins / 28 losses, net −$21.66 (fees $15.48);
short 20 trades / 5 wins / 15 losses, net −$7.89 (fees $9.99)**. Every loser
is logged with full setup features in `data/losses-6mo-long.jsonl` and
`-short.jsonl`; aggregates in `data/scan-6mo-20261012.json` and the failure
analysis in `data/scan-6mo-20261012-analysis.json`.

Failure themes: two-thirds of losses are full-R stop-outs (long: 18/28 at
mean net R −1.0; short: 11/15) — the entry fades against the prevailing
window flow (all long losers entered with negative setup delta; 24/28 with
negative window CVD). Time-based exits (max_holding / end_of_data) bleed
~0.45R each with no salvage. All long losers targeted prior highs (mean RR
2.46) and never reached them. Fees add ≈$0.50/trade; gross expectancy is near
zero, net expectancy negative. No directional bug in the short mirror — both
sides behave symmetrically.

## Execution-economics experiment (A) and feature separation (B)

`backtest-scan` gained declared experiment knobs: `--execution taker|limit`
(maker entry at the signal level with zero entry fee, market stops, limit
targets — the digest's order style) and `--max-notional` (100..10,000,
research-only; the $100 prototype cap now applies to paper execution only),
plus a `--wins` log alongside `--losses` (full setup features per trade).

Same six months, $2k notional: taker net −$413 (long) / −$160 (short) with
fees ≈ $300/$199 — costs ≈ 70-100% of losses. Limit execution halved the fee
drag and let the R:R gate admit more signals (long 31→43 trades, win rate
10%→23%) but stayed negative: −$246 (long) / −$211 (short). Execution realism
improves the economics but is not the missing edge.

Winner-vs-loser separation (in-sample, hypothesis-forming only): long winners
carry ~2x confirmation `buy_volume` and `delta`, fire when `window_cvd` is
near flat instead of deeply extended (−11 vs −226), and enter with *lower*
`net_reward_risk` (2.8 vs 3.7 — far prior-high targets are losers, matching
the earlier finding). Short winners mirror on delta sign and higher bar
activity. Script: `tools/feature_separation.py`.

## Sample growth (C) and the holdout verdict

`tools/backfill_btc.py` backfilled 2025-11-01..2026-04-12 from Alpaca in
47/47 self-healing chunks with zero holes (clean taker-side prints as far
back as November 2025; October probes reject unknown-side prints, so extension
stops there). The store now holds ~21.7M signed trades (~11.7 months).

The holdout scan (limit, $2k, on the never-before-analyzed window):
**long 95 trades, 20W/75L, net −$639 (fees $432); short 56 trades, 20W/36L,
net −$181 (fees $257)**. Loss structure is identical to the in-sample runs
(stop-outs dominate); the trigger in its current form has no edge out of
sample, and execution was not the binding constraint. The B discriminators
remain the candidates for a D-step filter, to be frozen and validated on a
fresh out-of-sample window rather than tuned on either half of this data.

## Still pending

The user must supply either a `DATABENTO_API_KEY` (free credit) or an exported
signed tape. Then `futures-compare` can answer the market question. The
selected broker adapter still needs signed-position ownership, short sell
entry, buy-to-cover, native protection, partial-fill/restart reconciliation,
and venue-specific sizing/margin/reduce-only behavior. Paper/demo routing must
be explicit. Offline fake-broker tests must precede a credentialed demo check.

The wider champion gaps remain: higher-timeframe/gamma context, value-area and
Fibonacci location, session/participation adaptation, and footprint-driven
management. The new discipline controls do not make the full strategy faithful
to the digest. The short pattern itself remains a symmetric adaptation.

Source-only pre-edit backup: `/tmp/tred-before-short-upgrade-x892vtmj`.

# MNQ/NQ strategy comparison

The futures research path is implemented end to end. The Databento exporter
(`futures-fetch`) builds the required signed tape and coverage manifest from
the user's own `DATABENTO_API_KEY` (free trial credit covers it). No
downloaded MNQ/NQ performance result has been published yet, because no key or
tape file has been supplied in this workspace. The existing BTC results cannot
establish whether switching markets improves the strategy.

## Export a real tape

New Databento accounts include free credits. Put the key in `.env` (or export
`DATABENTO_API_KEY`) and export one dated outright contract window:

```bash
python3 -m tred futures-fetch --symbol MNQZ6 \
  --start 2026-10-05T12:00:00Z --end 2026-10-12T12:00:00Z \
  --output data/futures
```

The exporter calls the official HTTP API directly (no SDK or pandas), asks for
the CSV encoding, and publishes a **verified tape pair**: the signed CSV and a
`tred.futures-tape.v1` manifest marked `complete` only when the downloaded row
count exactly matches Databento's reported record count for the same request.
`complete` asserts provider-count equality; it is not an exchange-level
audit. Pre-flight checks refuse degraded dataset dates, mixed contract maps,
empty windows, and ranges above the one-million-record research budget before
any tape bytes are downloaded. Existing files are never overwritten.

Real MNQ/NQ tapes record no trades during the daily CME maintenance halt and
other quiet stretches. The exporter turns each no-trade gap longer than
`--gap-seconds` (default 300) into a declared exchange closure, so the rolling
profile and open-position checks remain honest across those periods. The
loader still rejects any record inside a declared closure.

## What runs

`futures-compare` runs four fixed variants independently on the same contract
tape and starting equity: long, mirrored short, flipped long signals, and
flipped short signals. It calls the existing POC-zone / absorption /
second-failure signal engine, injecting futures costs into both target
selection and the entry reward/risk gate. It does not grid-search parameters.

| Contract | Dollars per point | Tick size | Dollars per tick |
|---|---:|---:|---:|
| MNQ | 2 | 0.25 | 0.50 |
| NQ | 20 | 0.25 | 5.00 |

These values come from [CME MNQ specifications](https://www.cmegroup.com/markets/equities/nasdaq/micro-e-mini-nasdaq-100.html)
and [CME NQ specifications](https://www.cmegroup.com/markets/equities/nasdaq/e-mini-nasdaq-100.html).
Commission and initial margin must be supplied explicitly for the intended
broker/account. They are not inferred from BTC fees or represented as current
broker quotes.

The model uses whole contracts, one contract maximum by default, 0.25% planned
equity risk per entry, a 1% daily loss cutoff and a two-consecutive-loss halt.
A setup is skipped if the risk or initial margin budget cannot fund one whole
contract. It never rounds quantity up to force a trade. Margin is an entry
collateral check; broker-specific liquidation/variation margin is not modeled.

Entries are restricted to **09:30–11:00 America/New_York** on weekdays;
positions are modeled flat by 11:00. The timezone handles DST. This is the
digest's morning window, not a full exchange holiday calendar. Session risk
resets on the New York calendar day. Setup and confirmation must be in the
morning window. MNQ defaults to **20,000 contracts in each setup/confirmation
bar**. NQ defaults to no contract-volume floor because the MNQ threshold does
not establish an NQ equivalent; supply `--minimum-bar-contracts` explicitly
for a declared NQ experiment. The 20-trade data-quality floor also remains.

Footprint rows default to one CME tick; the prior profile uses 288 populated
five-minute bars. It is a rolling profile, not a cash-session value area. At
least 290 fully covered bars are needed for a first possible signal. Include
warmup data in the export, and state exchange closures explicitly. Gamma,
Fibonacci location, higher-timeframe regimes and dynamic footprint exits
remain outside the current model.

## Input contract

Use a **single dated outright contract** and its own tape, e.g. MNQZ6 or NQZ6.
Do not combine expiries, back-adjust prices, replace MNQ volume with NQ volume,
or substitute an ETF/CFD/OHLCV series. Import fails on mixed contracts, unknown
aggressor side, duplicate IDs, unsorted nanosecond timestamps, fractional
contract volume or off-tick prices. `futures-fetch` enforces the same rules
before publishing; you can also supply your own exported CSV plus a manifest,
for example from Sierra Chart or another source that provides true aggressor
flags. The exporter writes the Databento trades-schema CSV verbatim; the
reader accepts those columns directly.

The canonical CSV columns are:

```text
timestamp,trade_id,symbol,price,size,side
```

`timestamp` is a timezone-qualified RFC3339 event timestamp. `price` is index
points, `size` is whole contracts, and `side` is `B` for aggressive buyer or `S`
for aggressive seller. The reader also accepts Databento **trades-schema CSV**:
`ts_event,ts_recv,instrument_id,sequence,action,price,size,side`, plus either a
`symbol` column or a manifest `instrument_id` mapping. Databento `B` means buy
aggressor and `A` means sell aggressor; `N` is rejected. This follows its
[trade schema](https://databento.com/docs/schemas-and-data-formats/trades).
Select `price_format="fixed9"` for raw integer prices, or `"decimal"` for
pretty prices. Unknown-side records must not be silently removed or inferred
from candle color.

A JSON manifest accompanies the CSV:

```json
{
  "schema": "tred.futures-tape.v1",
  "source": "Name of actual data provider and export job",
  "format": "databento",
  "symbol": "MNQZ6",
  "instrument_id": 123456,
  "price_format": "decimal",
  "start": "2026-10-05T12:00:00Z",
  "end": "2026-10-06T15:00:00Z",
  "complete": false,
  "trades_file": "mnq-trades.csv",
  "closed_intervals": [["2026-10-05T21:00:00Z", "2026-10-05T22:00:00Z"]]
}
```

This is a template, **not downloaded market data**: replace its contract,
instrument ID, dates and closure intervals with the actual export metadata.
`futures-fetch` writes this manifest automatically (format `databento`,
`price_format` fixed9 or decimal as detected, closures derived from real
no-trade gaps); the template remains for hand-assembled tapes from other
sources. Set `complete` to true only after confirming full source coverage.
The reader cannot independently verify the provider's assertion that no ticks
were lost. It excludes initial/final partial bars, rejects records outside
coverage, and allows profile gaps only when entirely covered by declared
exchange closures. Any missing bar while a modeled position is open
invalidates the report.

## Run a bounded comparison

After `futures-fetch` has published a tape pair (or you supplied your own
manifest and CSV), replace the fee and margin placeholders with the intended
account's assumptions:

```bash
python3 -m tred futures-compare \
  --manifest data/mnq-coverage.json \
  --commission-per-side <all-in-USD-fee> \
  --margin-per-contract <initial-margin-USD> \
  --equity 10000
```

Run NQ separately against its own manifest and account assumptions. NQ may
produce no executable trades at this account size even when signals occur;
inspect `whole_contract_risk_or_margin` rather than changing risk to force
entries. `--row-ticks`, `--profile-bars`, `--minimum-bar-contracts`, and
`--slippage-ticks` are explicit experiment settings and are recorded in the
report. Freeze them before evaluating a holdout period.

The importer streams records into footprint rows instead of retaining the
entire raw tape. CLI limits: one CPU, nice 19, at most 512 MiB address space,
30 additional CPU seconds, 256 MiB CSV, one million records, 250,000 footprint
rows, and seven calendar days per input. `futures-compare` runs with a 60-second
wall timeout and `futures-fetch` with 300 seconds for the download. A limit
failure produces no completed report. Existing reports and tapes are never
overwritten. Large comparisons should use separate, predeclared session
windows with warmup, not parallel jobs on this PC.

Reports contain source metadata and a normalized-tape hash, all assumptions,
per-variant signals/trades, net P&L, fees, win rate, profit factor, expectancy,
mean net R, drawdown, exits and rejection counts. Fills are modeled at the
next bar's first trade, with adverse tick slippage and fees on both sides;
when both stop and target are touched, stop wins. Shorts sell first and buy
back, using the correct signed futures P&L.

## How to decide whether futures are better

Use matched BTC/futures evaluation dates, declared risk budgets and realistic
instrument-specific costs. Keep training/parameter selection separate from
held-out sessions. Compare expectancy in net R and drawdown as well as dollar
P&L; the previous $100 BTC notional cap is not comparable to one NQ contract.
Zero signals or a handful of trades is insufficient evidence. The comparison
command deliberately does not declare a winning market from one short window.

## Execution status

This command is research only. It neither calls Alpaca nor submits futures
orders. MNQ/NQ short broker execution still needs the user's paper/demo broker
and data entitlements. [Tradovate's official API](https://api.tradovate.com/)
documents a separate simulation endpoint, but no account or connector has
been selected or activated. A broker adapter must implement signed ownership,
native protection and fill/restart reconciliation before a demo trading run.

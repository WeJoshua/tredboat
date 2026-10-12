# QQQ research on Alpaca — status

The user selected **QQQ on Alpaca with inferred sides** after Alpaca confirmed
no futures support. The path is implemented, offline-tested, and **unblocked**
after renewed paper credentials: two real weeks of QQQ IEX prints were fetched
and compared.

Alpaca equity prints carry timestamp, price, size, conditions and tape but
**no aggressor flag** (its SDK documents `takerSide` as crypto-only). The
`tick_prior_price` downgrade classifies each trade's side from the prior
printed price (IEX also prints sub-cent prices, e.g. 749.465, which the reader
accepts). This is a deliberate, user-selected weakening: identical footprint
shapes can be produced by different order flow. Every manifest and report
states the rule — nothing is silently inferred.

## Real results (2026-09-21 through 2026-10-09)

| Window | IEX records | Bars | Signals (all 4 variants) | Trades |
|---|---:|---:|---:|---:|
| week 1: Oct 05–09 | 37,026 | 482 | 0 | 0 |
| week 2: Sep 28–Oct 02 | 41,772 | 525 | 0 | 0 |

Both windows run with the declared defaults (`profile_bars=288`,
`row_ticks=1`) and the shared two-loss/daily-loss discipline. Setups fail at
the POC-zone and absorption filters (`no_poc_zone_reaction` /
`no_bullish_absorption`, and their short mirrors); the three-filter signal
never fires in ten sessions, so **no QQQ trade exists to evaluate**. This is
not evidence the pattern works on QQQ; it is evidence it did not trigger in
this sample. Combined with the Alpaca-sourced BTC tape (long −$20.44/30
trades, short −$8.12/19 trades over six months), the current evidence shows no
edge in either market during this period.

Fixes to the declared settings (`--profile-bars`, `--row-ticks`) or a longer
matched evaluation window would be new experiments requiring a frozen,
out-of-sample design, not retroactive tuning of the same two weeks.

## Commands

```bash
python3 -m tred equity-fetch --symbol QQQ \
  --start 2026-10-05T13:30:00Z --end 2026-10-09T21:00:00Z \
  --feed iex --output data/equity

python3 -m tred equity-compare \
  --manifest data/equity/qqq-20261005-20261009-coverage.json \
  --commission-fixed 0 --commission-bps 0 --slippage-bps 1 \
  --profile-bars 288 --row-ticks 1 --equity 10000
```

`equity-fetch` streams Alpaca's `/v2/stocks/{symbol}/trades` pages, writes the
canonical CSV and a `tred.equity-tape.v1` manifest, and marks `complete` only
when pagination is exhausted. No-trade gaps over `--gap-seconds` become
declared exchange closures. Windows are at most seven days and one million
records; existing files are never overwritten. `--feed iex` matches paper
account entitlements; `sip` needs the paid data license.

`equity-compare` reuses the exact causal engine (POC zone, absorption,
second-failure confirmation) and runs long, mirrored short and both flipped
variants with whole-share sizing, explicit commission/slippage, the same
09:30–11:00 New York entry window as futures, flattened by 11:00, and the
shared two-loss/daily-loss discipline. `--profile-bars`/`--row-ticks` are
declared experiment settings. The report's `limitations` array records the
tick-rule downgrade and proxy-market caveats.

## Execution status

Equity short/long paper execution at Alpaca (`order_class=bracket` with
native `stop_loss`/`take_profit`, buy-to-cover, ownership reconciliation) is
**not implemented yet** and is gated per the documented rule: fake-broker
offline tests must precede any live demo, and a live executor adds nothing
while the research shows zero tradeable signals. It stays sequenced behind a
positive-expectancy QQQ (or renewed MNQ) result.
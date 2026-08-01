# Order-flow datasets and the second bounded experiment

The candle-only gap experiment is retired. Six predeclared tests, zero
survivors, and the locked confirmation period untouched. The protocol's own
hard stop applies: *if the powered six-test gap experiment has no survivor,
retire candle-only gap research.*

That stop is not a verdict on intraday alpha. It is a verdict on the **inputs**.
Six tests rearranged four prices and a volume per bar. Nothing in that space can
distinguish a million shares bought from a million shares sold, a gap the market
spent four hours negotiating from one that appeared at the opening print, or a
name being liquidated from a sector being repriced. Continuing to reshape OHLCV
would have been searching a space that was already exhausted.

So the next experiment rests on three side channels the bars do not contain.

## What each channel adds, and what it costs

| Channel | New ingestion? | What the candle cannot say |
|---|---|---|
| Premarket price discovery | **No** — the SIP feed already returns 04:00–09:30 bars, and regular-session research discards them | Whether the overnight move was negotiated or simply appeared |
| Signed trade flow | **Yes** — the trades endpoint | Which side crossed the spread |
| Sector-relative context | **No** — candles plus the sector map | Whether a move was idiosyncratic or shared |

Two of the three were already paid for.

## Premarket price discovery

`intraday_premarket.py` → `intraday_premarket_features`.

Per symbol-session: premarket volume, relative volume, return, range,
`premarket_gap` (prior close → last premarket print), `opening_gap` (prior close
→ 09:30 open), and `gap_discovered_premarket`, their ratio — the share of the
eventual gap the premarket had already priced. Above 1 means premarket overshot
and the open pulled back.

Two disciplines carry over from the candle work:

- The relative-volume baseline draws only on **strictly prior** sessions, and is
  withheld entirely below five of them rather than compared to noise.
- Gaps are not measured across a membership hole. A symbol that left and
  rejoined a point-in-time universe has a months-long "overnight" move between
  its last and next row, which is not the quantity any overnight hypothesis is
  about. The ≤5-calendar-day adjacency rule is enforced **once, here**, at the
  point the gap is computed — the factor does not re-derive it from whatever
  candles a given run happens to hold.

```bash
python -m app.cli.intraday_dataset_pipeline premarket --from-universe --universe-key <key> --feed sip
```

## Signed trade flow

`intraday_trade_flow.py` (classification and aggregation) and
`intraday_trade_flow_ingest.py` (bounded, checkpointed fetching) →
`intraday_trade_flow_features`.

Two classifiers:

- **Lee-Ready** — each trade against the prevailing NBBO midpoint, with a tick
  fallback exactly at the midpoint. A quote posted *after* a trade cannot be the
  quote that trade crossed, so the prevailing quote is the latest one strictly
  before it. Accurate; needs quotes, which arrive at roughly ten times the
  volume of trades.
- **The tick rule** — trades only. Affordable across a universe.

The tick rule is the default for bulk ingestion, and that is a real accuracy
concession. `classifier_agreement_report` measures it on a bounded window rather
than asserting it is small:

```bash
python -m app.cli.intraday_dataset_pipeline flow-agreement --symbol AAPL --session 2025-03-03
```

If agreement is poor, the cheap classifier is not a proxy for order flow and the
hypotheses resting on it are measuring something else — worth discovering before
the trials are spent.

**Auction and out-of-sequence prints are never signed.** A call auction has no
side crossing a spread; signing the opening cross would invent imbalance out of
bookkeeping. Those prints still count toward volume and are reported as
`unclassified_share`. Imbalance is taken over *classified* volume only — dividing
by total would pull every bar toward zero in proportion to how many prints
happened to be unsignable.

### Bounded ingestion

Trade data is not candle data at finer resolution; it is three or four orders of
magnitude larger. A single liquid symbol prints more rows in one session than
the entire 237-symbol candle dataset holds for a month. Accordingly:

- Pages are folded into the aggregate and dropped, never accumulated.
- Progress checkpoints per symbol-session, so a restart resumes.
- A run beyond `MAX_SESSIONS_PER_RUN` pending symbol-sessions is **refused**
  rather than started.
- A session whose page ceiling was hit before the close is recorded `failed`,
  not `completed` — a truncated afternoon must not look like a real one.
- Trades are only fetched where a candle exists to align them to.

```bash
python -m app.cli.intraday_dataset_pipeline trade-flow --from-universe --universe-key <key> \
  --start 2025-01-02 --end 2025-01-31 --feed sip
```

## Sector-relative context

`intraday_sector_flow.py`, computed on demand from candles plus the sector map.

Per bar: the symbol's return, its sector's median return **excluding itself**,
the residual, the residual standardized by peer dispersion, and participation
relative to peers. Two rules:

- The peer aggregate excludes the symbol. Including it would let a large move
  drag its own benchmark and shrink the residual it is measured against.
- A sector with fewer than five peers is withheld, not estimated against one
  other name.

```bash
python -m app.cli.intraday_dataset_pipeline sector-flow --dataset-id <id>
```

## The six predeclared tests

Three families × two horizons (1 and 2 bars). Declared together, so a
disappointing family cannot be dropped afterwards, and every one counts toward
the cumulative trial ledger whether or not it is reported.

| Factor | Direction | Forced participant |
|---|---|---|
| `premarket_undiscovered_gap_reversal` | reversal | Auction participants clearing an overnight imbalance against whatever liquidity is present at 09:30 |
| `signed_trade_imbalance_continuation` | continuation | An institution working a parent order to a same-day completion target |
| `sector_relative_forced_flow_reversal` | reversal | A single holder liquidating one position under a mandate while the sector is unmoved |

```bash
python -m app.cli.intraday_factor_audit declare-order-flow --dataset-id <id>
```

### The required event count is not circular

The first experiment's power gate briefly was: it asked how many events would be
needed to detect the effect *the data happened to show*, so a null effect
demanded an impossible sample and every genuine null read `underpowered_null`,
putting the hard stop out of reach.

The fix is to declare the inputs instead of deriving them from results:

```
minimum tradeable net effect   5.0 bps
declared observation dispersion 60.0 bps
hurdle                          t ≥ 3.0
→ required events               2,126
```

Fixed before a single observation is measured, so a null stays a null.

## Gating

Each factor spec declares the channel it depends on
(`requires_premarket`, `requires_trade_flow`, `requires_sector_context`) and is
reported `blocked_missing_<channel>_data` when that channel is absent. An empty
side channel is never reported as a null result.

The sector family additionally **refuses the streaming discovery path**. Batching
symbols to bound memory is safe for per-symbol factors, but this one scores
against a same-instant cross-section of peers, and a batch would silently
redefine that peer group. It must run with the full candle set.

## What has not changed

- Chronological 50/30/20 with an embargo; the locked confirmation period is
  still unconsumed.
- Entry is always the open of the bar after the decision; a horizon that would
  run past the session close drops the event rather than shortening it, because
  a shortened horizon is a different hypothesis from the one declared.
- Gates are never lowered to manufacture a survivor.
- `survivorship_bias_present: true` still stands. The candidate pool is drawn
  from currently-tradable symbols, and the names absent from it are absent
  precisely because they stopped trading. That absence *is* the bias.

# Stage 0 feasibility probe — results and verdict

**Run:** 2026-08-16 · `microstructure_stage0_probe_v1`
**Scope:** 2 symbols × 5 sessions, outside the 2025 research window.
**Consumed:** no trial budget, no declaration, no database write, no data purchase.

## Verdict: NO-GO

The predeclared kill rule fired. Venue rotation accounts for **45.2 %** of gross
order-flow imbalance against a threshold of **30.0 %** declared before the number
existed. The threshold was not moved.

---

## What was measured

Symbols `INTC`, `NVDA`; sessions 2026-06-01 … 2026-06-05; feed SIP; full regular
sessions, every window drained to exhaustion (`all_windows_exhausted: true`).
**23,016,760 quote updates** parsed.

### 1. Power — the effect is easy to detect and far too small to trade

Effect and dispersion both taken from CJP Table 12.1, declared before measurement.

| Measure | Effect | Independent events to detect | vs 5.0 bps min tradeable | vs cost hurdle |
|---|---|---|---|---|
| Regime-incremental | 0.356 bps | 265 | **14.1× short** | **8.9× short** |
| Full regime span | 0.949 bps | 38 | 5.3× short | 3.3× short |

The cost hurdle uses the **measured** pooled median spread (1.584 bps) × 2.0
safety = 3.169 bps. That is a Stage-0 *input* measurement, not an outcome, and
refining it moved the hurdle **down** — in the hypothesis's favour. It still
fails by 3-9×.

Detectability was never the problem. 265 independent observations is nothing.
The problem is that the thing being detected is a fifth of a spread.

#### Power-calculation assumption discrepancy (documented, not corrected)

The `power` command derived required N from a **1.5054 bps** dispersion, taken
from CJP Table 12.1 — the same table the effect came from, so effect and
dispersion describe one distribution. The project separately records a
predeclared **60 bps** dispersion (`DECLARED_OBSERVATION_DISPERSION_BPS`,
`intraday_hypotheses.py`), set for 30-minute-bar order-flow factors.

These are dispersions of **different quantities at different horizons**: a
one-second mid-price change on a $33 stock moves in ±1-tick increments and
simply does not have 60 bps of spread. Neither figure is wrong; they are not
interchangeable. Both are recorded here rather than reconciled, and **no
observed Stage 0 number has been altered**.

Required independent events under each:

| Dispersion | Incremental (0.356 bps) | Full span (0.949 bps) |
|---|---|---|
| 1.5054 bps — CJP table, 1 s (used above) | 265 | 38 |
| 60 bps — project declared, 30 m | 419,446 | 58,981 |

The choice changes the sample-size answer by three orders of magnitude and
changes the **conclusion not at all**, because the conclusion turns on
materiality rather than detectability: the literature effect of 0.356-0.949 bps
sits below KefTrade's 5.0 bps minimum tradeable net effect and below the
measured 3.169 bps cost hurdle under either assumption. A future sub-minute
study would need to declare its own dispersion at its own horizon before
measuring; that declaration does not exist and is not being invented here.

### 2. Venue rotation — the kill rule

| Symbol / session | Quotes | q/s | **Rotation % of gross \|e\|** | Updates rotating | Median spread |
|---|---|---|---|---|---|
| INTC 2026-06-01 | 2,049,061 | 87.6 | 43.80 | 35.2 % | 1.83 bps |
| INTC 2026-06-02 | 1,401,044 | 59.9 | 46.40 | 34.8 % | 1.88 bps |
| INTC 2026-06-03 | 1,822,237 | 77.9 | 48.43 | 36.8 % | 2.63 bps |
| INTC 2026-06-04 | 1,523,272 | 65.1 | 48.22 | 38.4 % | 1.85 bps |
| INTC 2026-06-05 | 2,682,115 | 114.6 | 50.64 | 41.1 % | 1.99 bps |
| NVDA 2026-06-01 | 2,465,399 | 105.4 | 41.08 | 33.7 % | 1.34 bps |
| NVDA 2026-06-02 | 2,281,523 | 97.5 | 36.70 | 31.6 % | 1.31 bps |
| NVDA 2026-06-03 | 2,133,300 | 91.2 | 37.80 | 35.2 % | 0.93 bps |
| NVDA 2026-06-04 | 2,511,478 | 107.3 | 44.86 | 39.1 % | 0.93 bps |
| NVDA 2026-06-05 | 4,147,331 | 177.2 | 47.03 | 39.0 % | 0.97 bps |
| **Pooled** | **23,016,760** | 98.4 | **45.22** | 36.8 % | 1.58 bps |

**Every one of the ten symbol-sessions exceeds the threshold.** Range 36.7-50.6 %.
There is no subset of this sample that passes, and no session-selection rule that
could rescue it.

The measurement is deliberately **conservative**: a side whose price *and* venue
both changed is charged to `price_change`, not to rotation. The true rotation
contamination is therefore at least this large.

#### Rotation is not symmetric noise — it sets the sign

If rotation were random noise it would cancel in the signed sum. It does not.
Removing rotation events **flips the sign of the session's net order-flow
imbalance in 4 of 10 symbol-sessions**:

| Symbol / session | Signed OFI | Rotation part | OFI without rotation | Sign flips |
|---|---|---|---|---|
| INTC 2026-06-01 | 3,734,500 | 1,026,900 | 2,707,600 | no |
| INTC 2026-06-02 | −654,200 | −1,898,400 | 1,244,200 | **YES** |
| INTC 2026-06-03 | 1,580,800 | 1,618,400 | −37,600 | **YES** |
| INTC 2026-06-04 | 1,263,400 | 539,800 | 723,600 | no |
| INTC 2026-06-05 | 1,008,600 | 2,866,200 | −1,857,600 | **YES** |
| NVDA 2026-06-01 | 2,793,000 | 815,700 | 1,977,300 | no |
| NVDA 2026-06-02 | −2,457,300 | −636,400 | −1,820,900 | no |
| NVDA 2026-06-03 | −1,555,400 | 189,600 | −1,745,000 | no |
| NVDA 2026-06-04 | 1,810,100 | 1,304,100 | 506,000 | no |
| NVDA 2026-06-05 | −691,900 | −1,176,500 | 484,600 | **YES** |

In four sessions, whether the day reads as net buying or net selling pressure is
decided by which exchange happened to be posting the NBBO.

#### Why the number is this large

Venue codes are populated on 100 % of SIP quotes. In one arbitrary minute of
INTC, **twelve distinct venues** held the national best bid — IEX 2,676 quotes,
Nasdaq 1,626, Arca 615, and nine others. Each handoff replaces the reported size
with a different venue's queue at the same price, and CKS reads that difference
as liquidity arriving or leaving.

**A caveat, stated because it is real:** rotation is not *pure* bookkeeping. A
venue can lose the NBB precisely because its queue was consumed, so a rotation
sometimes carries a genuine depletion event. But CKS records `q_new − q_old`,
which bears no relationship to the size actually consumed — if a 100-share
venue depletes and a 2,000-share venue takes over at the same price, the formula
records **+1,900** (heavy buying) for an event that was **−100** (liquidity
removed). Wrong sign, wrong magnitude by 20×. Rotation encodes real events
badly rather than encoding nothing, which is worse than noise, not better.

### 3. Nanosecond timestamps — a real defect, and a small one

| | Pooled |
|---|---|
| Rows lost to microsecond truncation | 37,526 of 23,016,760 |
| Microsecond collapse rate | **0.163 %** |
| Rows at an identical nanosecond | **0** |
| Out-of-order rows | 0 |

I expected this to be worse. It is a genuine bug in
`intraday_quote_snapshots` — `UNIQUE(symbol, provider, feed, timestamp)` with
`ON CONFLICT DO UPDATE` silently discards those 37,526 updates, and the survivor
is the *latest* state within the microsecond, which is future information
relative to anything inside it. But at 0.163 % it is a correctness fix, **not** a
reason to reject the data. Alpaca's SIP timestamps are genuinely spread out;
there are zero true nanosecond ties in 23 million updates.

Reporting this plainly matters: one of the three concerns raised in the design
document was overstated, and the probe is what established that.

### 4. Quote rates — the §7 storage estimates were right

Mean 98.4 quotes/second, range 59.9-177.2. That is 1.4-4.1 M per symbol-session,
inside the 0.5-3 M band estimated in the design document. Ten symbol-sessions
took ~2,300 API pages and ran unattended to exhaustion.

### 5. Halt / status coverage — no history exists

| Endpoint | Status |
|---|---|
| `/v2/stocks/{symbol}/auctions` | **200**, real data |
| `/v2/stocks/{symbol}/status` | 404 |
| `/v2/stocks/{symbol}/statuses` | 404 |
| `/v2/stocks/{symbol}/halts` | 404 |
| `/v2/stocks/{symbol}/luld` | 404 |
| `/v2/stocks/{symbol}/imbalances` | 404 |
| `/v1beta1/stocks/{symbol}/status` | 404 |

Trading status and LULD are stream-only; there is no historical record. Halts
would have to be inferred from the quote stream. Auctions **are** retrievable
(INTC opened 2026-06-01 at $109.50 on 1,801,271 shares), which is more than the
design document assumed.

No halt occurred in the sample — the longest gap between consecutive NBBO
updates across all ten sessions was **6.7 seconds**, against a 60-second halt
proxy. So the halt-detection concern is **untested**, not cleared.

Also recorded rather than silently dropped: 445-707 crossed quotes and
14,700-17,100 locked quotes per session. `normalize_stock_quote` currently
rejects the crossed ones row-by-row with no counter.

### 6. Trade classifier agreement — passes

| Symbol | Comparable trades | Lee-Ready vs tick rule |
|---|---|---|
| INTC | 76,349 | **90.2 %** |
| NVDA | 162,974 | **88.0 %** |

The cheap classifier is a fair proxy. This was the one component that could have
failed independently and did not.

---

## What this means

The order book is not the problem. **The view of it that Alpaca sells is.**

Alpaca's NBBO gives one venue's slice of a best price that is usually a tie
among several venues, and it re-picks that venue on every update. Nearly half
of the resulting order-flow signal is the pick changing rather than the market
moving, and in 40 % of sessions the pick decides the sign of the day.

Stack that on the two other blind spots the design document identified and could
not measure here — odd lots excluded from the NBBO by construction, and hidden
orders that CJP measure at 44.6 % of executed AAPL volume — and the visible
queue is a minority of a minority.

Separately and independently, the effect we would be hunting is 0.36-0.95 bps
against a measured 1.58 bps spread. Even with a perfect book it does not clear
the cost hurdle by 3-9×.

Those are two sufficient reasons to stop, and they fail for unrelated causes.
Fixing the data would not fix the economics.

## Verdict

**NO-GO.** Alpaca L1 NBBO cannot support order-book microstructure research.
Continuing would require genuinely better data — venue-resolved L2/L3 (Nasdaq
TotalView-ITCH via Databento, or equivalent) — and the power result says that
even then the economics do not clear KefTrade's declared bar for a
spread-crossing strategy at these horizons.

Per the design document's own hard stop, this retires order-book behaviour as a
**standalone alpha** direction.

## What is worth keeping

Three deliverables outlive the rejection, and none requires further approval to
be useful:

1. **`iter_stock_quote_pages`** — streaming, checkpointed, `Retry-After`-aware quote ingestion with a truthful `exhausted` flag. Removes the 1 M ceiling and the accumulate-in-memory shape in `fetch_stock_quotes`.
2. **Two confirmed defects**, both small and both real: the microsecond collapse in `intraday_quote_snapshots` (0.163 % silent row loss, and a lookahead leak within each collapsed microsecond), and `normalize_stock_quote`'s uncounted row-by-row rejection of crossed quotes.
3. **A measured warning on existing code.** `aggregate_microstructure_bars` (`intraday_execution_costs.py:463`) computes `order_flow_imbalance` and `normalized_order_flow_imbalance` from exactly this feed and writes them to `intraday_microstructure_features`. Those columns are ~45 % venue-routing artefact. Anything that has consumed them should be re-read in that light, and the function should carry the warning.

## Cleanup carried out under this verdict

Authorized as platform-integrity work only. No Stage 1, no alpha testing, no
data purchase, no threshold changed, no historical evidence rewritten.

| # | Action | Where |
|---|---|---|
| 1 | Stage 0 report preserved and the NO-GO conclusion recorded | this file, `reports/stage0_microstructure_probe/` |
| 2 | Quarantined everything whose economic interpretation rests on Alpaca L1 queue size — the `liquidity_shock_reversal_v1` V2 family and the `liquidity_shock_reversal` factor. Both stay registered and readable; both refuse to run with `retired_data_fidelity: Alpaca SIP venue rotation measured at 45.224% of gross OFI vs 30% allowed` | `data_fidelity.py`, `families/v2/base.py`, `families/registry.py`, `intraday_factor_diagnostics.py` |
| 3 | Certified the affected snapshot fields (`order_flow_imbalance`, `normalized_order_flow_imbalance`, `mean_depth`) as `not_approved_for_queue_interpretation`. Additive record; snapshot rows untouched and their immutability triggers intact | migration `079`, `data_fidelity.snapshot_field_certifications` |
| 4 | Nanosecond preservation for future ingestion: `timestamp_ns` on normalized quotes and on `intraday_quote_snapshots`, uniqueness re-keyed to the source instant. Historical rows keep their values; the column is derived from what was already stored | `alpaca.py`, migration `079`, `intraday_execution_costs.persist_quote_snapshots` |
| 5 | Quote rejection accounting: `QuoteNormalizationCounters` reports received / accepted / rejected split by cause, so a halted session can no longer look like a quiet one | `alpaca.py` |
| 6 | Price- and trade-only measurements preserved and explicitly *not* retired — quoted and effective spread, Lee-Ready aggressor classification. Execution-cost research remains unauthorized | `data_fidelity.PRESERVED_QUOTE_PRICE_FIELDS` |
| 7 | Tests added and existing affected tests updated | `test_data_fidelity_cleanup.py` (17), `test_microstructure_probe.py` (18), `test_strategy_engine_v2_families.py`, `test_intraday_factor_diagnostics.py` |

**Scope discipline on the retirement.** The finding concerns quoted *sizes*.
Trade-signed factors (`signed_trade_imbalance_*`) rest on Lee-Ready aggressor
classification, which Stage 0 validated at 90.2% / 88.0% agreement, and the
auction factor rests on midpoint and clearing prices. Neither is retired, and a
test pins that they are not retired by association.

## Limitations

- **The predeclared symbols did not achieve the intended tick-size spread.** INTC was chosen as the large-tick case on the assumption of a low nominal price; it traded near $109 in this window, so both probe symbols are effectively small-tick. The symbols were **not** changed after this was discovered — that would be selection after the fact. It does not affect the rotation measurement, which concerns feed mechanics rather than tick regime, but it means the large-tick regime where Gould-Bonart report the strongest queue-imbalance effect went unprobed.
- Five consecutive sessions in one week; no regime variety, no earnings, no halt, no high-volatility day.
- The 0.36 bps effect is CJP's published ORCL number, not a KefTrade measurement. Measuring our own would require the Stage 1/2 work this verdict declines to authorize.

## Artefacts

`reports/stage0_microstructure_probe/` — `01_power.json`,
`02_quote_stream_probe.json`, `03_halt_coverage.json`,
`04_classifier_agreement.json`, `05_verdict.json`, and ten per-symbol-session
checkpoints under `sessions/`.

Code: `app/services/microstructure_probe.py`,
`app/cli/microstructure_probe.py`, `iter_stock_quote_pages` in
`app/providers/alpaca.py`, `tests/test_microstructure_probe.py` (18 tests).

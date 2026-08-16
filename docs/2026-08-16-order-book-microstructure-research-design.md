# Order-book microstructure: pre-implementation design and GO/NO-GO

**Status:** design only. No code written. No data purchased.
**Date:** 2026-08-16
**Prior direction retired:** candle/VWAP/gap/volume/trade-imbalance/news/sector reshaping.

The headline finding of this audit is a data-fidelity fact that determines
everything downstream, so it goes first:

> **Alpaca's NBBO quote size is the size at the *single* exchange currently
> posting the best price — not the aggregate depth across all venues at that
> price.** Alpaca confirms it publicly: the API returns "exactly one bid and one
> ask which is the 'best' bid and ask across all exchanges" and "doesn't provide
> any 'market depth' quotes." When several venues quote the same NBBO price,
> Alpaca returns whichever arrived most recently.

Every queue-based measurement in the microstructure literature — queue
imbalance, queue depletion, replenishment, absorption, Cont-Kukanov-Stoikov OFI
— is defined on the size of *a queue*. Alpaca's `bs`/`as` is a fragment of one,
and the fragment changes identity whenever `bx`/`ax` rotates to a different
venue at the same price. A venue rotation with zero real liquidity change
produces a large spurious OFI event under the standard formula. KefTrade already
computes OFI this way (`intraday_execution_costs.py:463`), and that contamination
has never been measured.

**Rotation is not an edge case.** Cartea, Jaimungal & Penalva (§3.6, Table 3.8)
reconstruct venue-by-venue quotes for AAPL and report the share of regular
trading hours each venue's best price coincides with the NBBO:

| Venue | % time at NBB | % time at NBO |
|---|---|---|
| NASDAQ | 67.8 | 61.3 |
| ARCA-NYSE | 43.4 | 38.3 |
| EDGX | 34.5 | 41.0 |
| BATS | 18.8 | 15.7 |
| BATS-Y | 4.5 | 0.0 |

Those columns sum far above 100 %, which is the point: **the NBBO is usually a
tie among several venues.** Alpaca resolves the tie by returning "whichever
quote arrived most recently." So the size we receive is one arbitrarily-selected
member of a tied set, re-selected on every update. Under the CKS formula that is
indistinguishable from real queue growth and decay.

That does not kill the direction. It determines what Stage 0 must be.

*(This document now incorporates Cartea, Jaimungal & Penalva,* Algorithmic and
High-Frequency Trading*, CUP 2015 — hereafter **CJP** — whose Chapter 12 is
devoted to order imbalance and whose Chapters 3-4 supply the empirical anchors
used in §5, §6 and §10.)*

---

## 1. What KefTrade currently has that supports this research

The infrastructure is in unusually good shape. The problem is resolution, not
rigour.

### Data acquisition (usable, incomplete)

| Asset | Location | State |
|---|---|---|
| L1 NBBO quote fetch | [alpaca.py:264](../apps/api/app/providers/alpaca.py:264) `fetch_stock_quotes` | Works, feed-tagged, paginated — but **accumulates in memory** and hard-caps at `MAX_STOCK_QUOTE_PAGES(100) × 10_000 = 1M quotes` per call |
| Streaming trade fetch | [alpaca.py:367](../apps/api/app/providers/alpaca.py:367) `iter_stock_trade_pages` | Correct pattern: yields pages, never accumulates, honours `Retry-After`, records `exhausted` truthfully |
| SIP entitlement | `docs/intraday-trade-ready-pipeline.md` | Algo Trader Plus, SIP back to 2016, feed pinned into `source` and the dataset content hash |
| Quote persistence | `intraday_quote_snapshots` (migration 060) | Bid/ask price+size, midpoint, spread_bps, raw payload |
| Auction imbalance table | `intraday_auction_imbalances` (062/063) | Schema exists; **fed only by external file ingest** (`intraday_research_ingest.py`), no Alpaca historical path |

### Microstructure computation (correct math, wrong resolution)

`aggregate_microstructure_bars` ([intraday_execution_costs.py:463](../apps/api/app/services/intraday_execution_costs.py:463))
already implements Cont-Kukanov-Stoikov OFI faithfully:

```python
ofi += (qb1 if pb1 >= pb0 else 0) - (qb0 if pb1 <= pb0 else 0)
ofi -= (qa1 if pa1 <= pa0 else 0) - (qa0 if pa1 >= pa0 else 0)
```

which is exactly `e_n = 1{P^b_n ≥ P^b_{n-1}}·q^b_n − 1{P^b_n ≤ P^b_{n-1}}·q^b_{n-1}
− 1{P^a_n ≤ P^a_{n-1}}·q^a_n + 1{P^a_n ≥ P^a_{n-1}}·q^a_{n-1}`.

It is then **summed into 15m/30m bars and the event stream discarded**. CKS
measure OFI over 10-second-to-1-minute buckets; the documented predictive
content is largely gone by 30 minutes. The right code exists at the wrong
sampling frequency.

`intraday_trade_flow.py` has Lee-Ready with a `_PrevailingQuote` walker (a quote
posted after a trade cannot be the quote it crossed), a tick-rule fallback, an
explicit `classifier_agreement_report`, and — importantly — it refuses to sign
auction and out-of-sequence prints and reports `unclassified_share` rather than
diluting imbalance by them.

### Research protocol (the genuinely valuable asset)

`intraday_alpha_map.py` (2,461 lines) is already the Step-4 layer this brief asks
for. It measures information *before* any strategy exists and contains no
threshold, entry rule, stop, target or P/L:

- `forward_return_ladder` / `attach_forward_returns` — horizon ladder in seconds
- `expanding_normalization` — z-score/percentile within (symbol, time-of-day) using **strictly prior** observations, `None` below 20 priors
- `residualize_cross_section` — splits each feature into market / sector / idiosyncratic, so "eight positions" is distinguishable from "one market bet wearing eight tickers"
- `cross_sectional_dependence` — flags pseudo-diversification above 0.80 `same_sign_share`
- `session_clustered_ic` — clusters by session rather than treating correlated events as independent
- `horizon_cost_feasibility` — oracle preflight; kills a horizon whose top-decile absolute move can't clear cost × safety, *before* any feature is scored
- `cost_hurdle` — `required_gross_bps = round_trip_cost × 2.0`, because break-even is inside the cost model's own error
- `probability_of_backtest_overfitting` (CSCV, 70 partitions) — above 0.5 **no** cell from the grid is authorized
- Benjamini-Hochberg across the whole grid; `monotonicity` ≥ 0.70; five explicit verdicts including the one that matters, `information_below_cost` vs `no_information`
- Frozen-dataset enforcement: reads only `research_dataset_*`, pinned by `dataset_id`, with immutability triggers and a test that pins the property from three sides

Supporting: `intraday_trial_ledger.py` (predeclaration + cumulative trial count,
declaration is single-use), `intraday_research_leakage.py`
(`perturb_future_candles` — perturb the future, and if any output moves, that is
leakage), `intraday_research_power.py` (predeclared 5 bps effect / 60 bps
dispersion / t ≥ 3 → 2,126 events, fixed before observation so a null stays a
null), chronological 50/30/20 with embargo and an **unconsumed locked
confirmation period**, and 128 test files.

### Sub-minute is already anticipated

The alpha map accepts 10s/30s rungs and reports them `below_measurement_grid`
with the exact requirement rather than silently rounding to 60s. The doc states
they "resolve automatically if a sub-minute bar grid is ever ingested." The
horizon ladder was built for this experiment before this experiment was proposed.

---

## 2. What is missing

Ordered by how badly it hurts.

**1. Queue identity — fatal if unaddressed.** Alpaca `bs`/`as` is single-venue.
Queue imbalance, depletion, replenishment and absorption are all defined on a
queue; there is no queue in this feed. Venue rotation at a constant price
injects fake OFI events of arbitrary size. Unquantified today.

**2. No L2 depth.** Alpaca provides no depth-of-book for US equities on any
plan. Multi-level / integrated OFI (Cont-Cucuringu-Zhang), depth-change and
book-shape features are not computable from any KefTrade data source.

**3. No L3 / order-level messages.** Add / cancel / modify / execute are not
available. This means **cancellations, liquidity withdrawal and queue
replenishment are not merely noisy — they are unmeasurable.** From NBBO you see
a net size change and cannot tell a cancel from a fill from a venue rotation.
Two of the fifteen behaviours in the brief have no signal path at all.

**4. Timestamp collapse — silent, and exactly where the signal is.** Alpaca
emits RFC-3339 nanosecond timestamps. `normalize_stock_quote`
([alpaca.py:345](../apps/api/app/providers/alpaca.py:345)) parses into Python
`datetime` (microsecond floor), and `intraday_quote_snapshots` carries
`UNIQUE(symbol, provider, feed, timestamp)` with `ON CONFLICT ... DO UPDATE`.
Two NBBO updates in the same microsecond become one row, last-writer-wins, with
no error and no counter. Sequencing is the entire content of an event-time
feature.

**5. Three independent layers of invisible liquidity.** The visible NBBO size is
not the book, for three separate reasons that compound:

- *Venue fragmentation* — we see one venue's slice of a usually-tied NBBO (Table 3.8 above).
- *Odd lots* — the NBBO is a round-lot construct; sub-100-share interest is excluded, which bites hardest in exactly the high-priced names. (Alpaca documents `bs`/`as` as round lots; confirm against payloads, but the Reg NMS constraint stands regardless.)
- *Hidden orders* — **CJP Table 4.18: for AAPL in 2013, execution against hidden orders occurred in more than 75 % of minutes, and half the time hidden liquidity was 33-56 % of all shares traded that minute** (mean 44.6 %). Their comment is blunt: an agent posting visible offers "found her offers trumped by more aggressive hidden ones relatively often."

Queue imbalance computed from visible NBBO size in a liquid name is therefore
measuring a minority of the actual executable book. This is a *first-order*
limitation on Tier 0, not a rounding error, and it is independent of — and
additive to — the rotation problem.

**6. No streaming quote ingest.** `iter_stock_trade_pages` exists;
`iter_stock_quote_pages` does not. Quotes arrive ~10× trade volume and the
current fetch materialises them in a list with a 1M ceiling — the same OOM shape
the candle work already hit, plus a truncation that looks identical to a quiet
market.

**7. No sub-minute outcome grid.** Finest frozen grid is 1m candles. Horizons
below 60s are correctly refused rather than faked, so today they are simply
unmeasurable.

**8. No event-time research primitive.** Everything in the lab is calendar-bar
shaped. The natural clock for this work is the quote-update / trade sequence.

**9. No historical auction imbalance.** Alpaca has an order-imbalance *stream*
channel; there is no historical endpoint. The table can only be filled going
forward, or from a vendor.

**10. No storage tier for this volume.** Single-node Postgres with B-tree
indexes. Estimated 2×10⁹ quote rows for 8 symbols × 1 year (see §7). That is not
a Postgres table.

**11. Cost model has no queue-position or latency term.**
`intraday_execution_costs.py` calibrates from quoted spreads and matched fills.
It cannot price the thing that decides whether L1 alpha is harvestable: whether
a passive order reaches the front of a queue before the informed flow arrives,
and what happens in the ~100 ms between a KefTrade decision and an Alpaca fill.

---

## 3. Behaviours worth measuring, with definitions

Notation at time `t`: best bid `P^b_t`, ask `P^a_t`, sizes `q^b_t`, `q^a_t`,
mid `M_t = (P^b_t + P^a_t)/2`, spread `s_t = P^a_t − P^b_t`, tick `δ`.
`n` indexes book events in sequence.

### Measurable from L1 NBBO (with the venue caveat)

**Queue / order imbalance (CJP §12.2; Gould-Bonart 2016).** CJP's canonical form
is the signed ratio:
```
ρ_t = (V^b_t − V^a_t) / (V^b_t + V^a_t)     ∈ [−1, +1]
```
`V^b`, `V^a` are quoted LO volumes on the bid and ask. Gould-Bonart's equivalent
`I_t = q^b/(q^b + q^a) ∈ [0,1]` differs only by affine rescaling; fit
`logit P(next mid move up) = α + β·I_t`. They find a strongly significant
relationship across 10 Nasdaq stocks, strongest in large-tick names where
discrete queue dynamics dominate; the information lives over roughly the **next
two mid-price changes** and decays to ~0 after.

Four implementation choices CJP settles, which we should not re-litigate:

1. **Depth to use.** `V` may be taken at-the-touch, over the best *n* levels, or within *n* ticks of the mid. CJP: "Some studies suggest that the best trade-off between predictive power versus model complexity is strongest using only the touch," and they use the touch throughout. **This is the single most favourable fact for Tier 0** — the absence of L2 depth costs less than it appears, *provided* the touch we observe is a real touch. It is the venue-fragment and hidden-order problems, not the missing depth levels, that threaten Tier 0.
2. **Sampling and smoothing.** They sample every millisecond and average over a trailing **100 ms** window. Raw instantaneous imbalance is far noisier; their own comparison of the 100 ms-averaged generator (12.5) against the instantaneous one (12.7) shows materially different rates. Predeclare 100 ms as the primary smoothing and instantaneous as the sensitivity check — do not choose between them after seeing forward returns.
3. **Discretisation.** Five equally spaced regimes with knots at `{−1, −0.6, −0.2, +0.2, +0.6, +1}`: sell-heavy, sell-bias, neutral, buy-bias, buy-heavy. A predeclared fixed binning, not a data-fitted one — worth adopting *precisely because* it was fixed by someone else before we saw our data.
4. **Autocorrelation.** Their ACF of imbalance is significant out to ~2,000 lags ≈ 200 s. This is the concrete justification for the effective-sample-size discipline in §5: adjacent imbalance observations are nearly the same observation.

**Imbalance as a Markov chain (CJP §12.2.1-12.2.2).** Model the regime `Z_t ∈ {1..K}`
as a Markov chain with transition matrix `A`, MLE
```
Â_ij = n_ij / Σ_j n_ij ,    n_ij = #{transitions i → j}
```
and continuous-time generator `B̂ = (1/ΔT)·log Â` (matrix log). Extended to joint
MO arrivals: conditional on regime `k`, buy and sell market orders arrive as
independent Poisson processes with intensities `λ⁺_k`, `λ⁻_k`, estimated by
`λ̂^± = M̂^±_k / Σ τ_k`.

This matters because it is **a queue-reactive model we can actually fit at L1.**
Huang-Lehalle-Rosenbaum proper needs order-level messages; CJP's version needs
only touch imbalance and signed trades, both of which Tier 0 has. Their ORCL
estimates make the economic point directly: the day was sell-heavy overall
(`Σλ⁻ = 1.079/s` vs `Σλ⁺ = 0.444/s`), yet *conditional on a bid-heavy regime*
buy-MO intensity exceeds sell-MO intensity — "order imbalance is indeed a good
predictor of order flow."

**Order flow imbalance (Cont-Kukanov-Stoikov 2014).** Per event:
```
e_n = 1{P^b_n ≥ P^b_{n−1}}·q^b_n − 1{P^b_n ≤ P^b_{n−1}}·q^b_{n−1}
    − 1{P^a_n ≤ P^a_{n−1}}·q^a_n + 1{P^a_n ≥ P^a_{n−1}}·q^a_{n−1}

OFI_k = Σ_{n ∈ (t_{k−1}, t_k]} e_n
```
Impact model:
```
ΔM_k = β·OFI_k + ε_k ,   β ≈ c / (AD_k)^λ ,  λ ≈ 1
```
where `AD_k` is average depth over the bucket. CKS report the relation is
linear, robust to intraday seasonality, and stable across stocks and time
scales; it also implies the observed square-root volume/impact relation, and OFI
explains price moves better and more stably than trade imbalance does. Report
`OFI` and `OFI / AD` separately — never only the normalised form.

**Microprice (Stoikov 2018).** The limit of iterated expected mid-prices given
book state, a martingale by construction:
```
P^micro_t = lim_{i→∞} E[M_{τ_i} | I_t, s_t]
```
expressible as `M_t + g(I_t, s_t)`. Estimated by iterating a
`(imbalance-bucket × spread)` transition matrix to convergence. The tradable
quantity is the **microprice-mid deviation** `(P^micro_t − M_t)/M_t`, in bps.
Stoikov reports forecast accuracy peaking over roughly **3-10 seconds** — which
is the empirical anchor for the horizon ladder in §5.

**Weighted mid (baseline, not a hypothesis).** `M^w_t = (P^b q^a + P^a q^b)/(q^a + q^b)`.
Included only so microprice must beat something.

**Spread state.** `s_t/M_t` in bps, its expanding-window z-score within
(symbol, time-of-day), and `Δs` over the feature window. Spread widening is the
observable half of liquidity withdrawal available at L1.

**Trade aggressor flow (Lee-Ready 1991).** Sign each trade against the
prevailing NBBO midpoint strictly *before* it; tick-rule fallback exactly at the
mid. Already implemented. Signed volume imbalance
`TI = (V^buy − V^sell)/(V^buy + V^sell)` over classified volume only.

**Aggression intensity.** Share of volume executing at or outside the far touch,
and `effective_spread_bps = 2·d·(p_trade − M_prevailing)/M_prevailing` with
`d = ±1` from the classifier. Distinguishes a size-taker paying up from a
patient one.

**Absorption (composite, L1 proxy).** The economically meaningful event:
sustained one-sided aggressive volume that does **not** move the price.
```
A_t = Σ signed aggressive volume over window w   ,   with |ΔM| ≤ δ
```
Report the pair `(A_t, ΔM_t)` and their ratio, not a collapsed score. Large `A`
with `ΔM ≈ 0` is a resting institutional order absorbing flow; the standard
hypothesis is reversal once it lifts. Genuinely measurable at L1 because it only
needs trades + mid.

**Failed pressure / exhaustion.** `A_t` large in one direction followed by
`sign(ΔM_{t+h}) = −sign(A_t)`. Strictly a *result*, so it is a forward-return
measurement, never a feature — it must never enter the panel as a predictor.

**Queue depletion (partially observable).** `q^b_t → 0` with `P^b` unchanged, then
`P^b` steps down. At L1 the *event* is visible; its *cause* (cancels vs fills)
is not, and the venue-rotation problem makes even the size trajectory suspect.
Measure `q^b_t / q^b_{t−w}` and time-to-touch-change, and label the finding
explicitly as cause-agnostic.

### Requires L2

Multi-level OFI, and Cont-Cucuringu-Zhang **integrated OFI**: compute
level-by-level OFI for the top ~5-10 levels and take the first principal
component. They report integrated OFI explains contemporaneous impact
substantially better than best-level OFI alone, and that once levels are
integrated, cross-asset *contemporaneous* impact adds nothing — but **lagged**
cross-asset OFI does improve forecasting, concentrated at short horizons and
decaying rapidly. Depth-change, book slope, and depth-weighted imbalance also
live here.

### Requires L3 / MBO

**Cancellations and liquidity withdrawal.** Cancel rate per unit resting size;
cancel-to-add ratio; the share of a queue's decline attributable to cancels
rather than executions. This is the honest test of whether visible size is real,
and it is the direct check on spoofed depth. **No L1 or L2 proxy exists.**

CJP §4.4 shows how large this hidden dynamic is: **cancellations are 43-48 % of
all exchange messages** across their four assets (AAPL 43.1 %, FARO 48.1 %), and
total messages run ~22 per trade for AAPL and 70-227 per trade for less liquid
names. Roughly half of all book activity is orders being withdrawn, and none of
it is observable in a net top-of-book size change.

**Queue replenishment.** Refill rate after a queue is consumed: the size added
at the touch within `w` after depletion, over the size consumed. Requires adds
to be distinguishable from the net.

**Queue position / time priority, and fill probability.** The variable that
decides whether passive harvesting of L1 alpha is possible at all. CJP Table 4.17
gives the empirical fill curve for AAPL (2013-07-30) — of orders posted at a
given distance from the mid, the share eventually executed:

| Distance from mid | < 2¢ | 2¢ | 3¢ | 4¢ | 5¢ | 6¢ | 10¢ |
|---|---|---|---|---|---|---|---|
| % executed | 78.6 | 64.7 | 55.4 | 44.4 | 34.9 | 27.6 | 8.6 |

Two consequences. First, this is a real, citable prior for the fill-probability
term that KefTrade's cost model currently does not have. Second, it decays fast
enough that a passive strategy's economics are dominated by *where in the queue
you are*, which is precisely the unobservable.

**Queue-reactive model (Huang-Lehalle-Rosenbaum 2015).** The book as a
continuous-time Markov jump process where limit / cancel / market-order
intensities depend on current queue sizes (and on the opposite side's size).
Fit within periods where a reference price is constant. This is the principled
way to ask "is the current book state unusual given its own dynamics" rather
than z-scoring a size. Needs event-typed messages — but see the CJP reduced form
in the L1 section above, which captures the MO-intensity half of it from data we
already have.

---

## 4. Minimum viable historical dataset

Three tiers. The point of Stage 0 is to decide between them with evidence rather
than by assumption.

### Tier 0 — what we can pull today (Alpaca SIP, already paid for)

| | |
|---|---|
| **Content** | L1 NBBO quotes (price, single-venue size, quoting exchange, conditions, tape) + all trades with conditions/exchange/tape, nanosecond source timestamps |
| **Symbols** | 8, predeclared, spanning tick-size regimes — large-tick (spread pinned at 1 tick, where the literature says queue imbalance works best) and small-tick |
| **Window** | 2025-01-02 → 2025-12-31, one full year, all sessions, no symbol or period selection |
| **Answers** | Queue imbalance, best-level OFI, microprice, spread state, aggressor flow, absorption |
| **Cannot answer** | Anything requiring depth, cancels, replenishment or queue position — and cannot separate real queue changes from venue rotation without the Stage-0 diagnostic |

**Tier 0 is sufficient to reject, and insufficient to confirm.** If queue
imbalance and OFI show no incremental predictive information at L1 on a full
year of clean SIP data, buying L3 to look harder is not justified — the
literature's effect is large enough that L1 should show *something*. If it does
show something, Tier 0 cannot tell you whether it is harvestable, and Tier 1
becomes the question.

### Tier 1 — the smallest dataset that can *confirm* (vendor L3)

Nasdaq TotalView-ITCH MBO via Databento (`XNAS.ITCH`), or MBP-10 if MBO proves
unaffordable. Same 8 symbols, **one month**, full order-level messages with
exchange timestamps. Single-venue (Nasdaq) rather than consolidated — which is
a limitation, but a *coherent* one: it is a real book with real queues, unlike a
consolidated top-of-book with no queue at all.

This is the minimum that makes cancellations, replenishment, queue position and
the queue-reactive model measurable at all.

### Tier 2 — not now

Multi-venue L3, full universe, multi-year. Only reachable if Tier 1 produces a
`tradable_candidate` that survives a realistic latency and queue-position model.

**Not needed at any tier:** opening/closing auction imbalance. It is a
different phenomenon on a different clock (a scheduled cross, not continuous
book dynamics) and folding it in would widen the search space for no
hypothesis. Keep the table; do not feed this experiment from it.

---

## 5. Proposed experiment design

Everything below is predeclared. Numbers are fixed before any observation.

### Stage 0 — feasibility probe (must pass before anything else)

Three questions, on **2 symbols × 5 sessions**, deliberately outside the
research window so it consumes no statistical budget:

**0a. Venue-rotation contamination.** Decompose every quote update into
`price_change`, `size_change_same_venue`, `venue_rotation` (`bx`/`ax` changed,
price unchanged). Report the share of total `|e_n|` attributable to rotation.
This is the number that decides whether Alpaca L1 can support OFI at all.

**0b. Timestamp collapse rate.** Share of quote updates lost to microsecond
truncation, per symbol. Also record the observed quote rate per session — this
is the real input to every storage and time estimate in §7, which are currently
assumptions.

**0c. Classifier agreement.** Run the existing `classifier_agreement_report`
(Lee-Ready vs tick rule) on the same window. Already built.

**Predeclared kill rule.** If rotation accounts for **> 30 %** of gross `|e_n|`,
Alpaca L1 OFI is measuring venue routing, not order flow, and Tier 0 is declared
unfit for OFI. Queue imbalance would be re-scoped to price-level features only
(spread, aggressor flow, absorption, microprice on a rotation-cleaned book), or
the direction moves straight to a Tier-1 decision. The threshold is set now,
before the number is known.

### Stage 1 — Order-Book State Engine

`market events → book state → behavioural features → panel`

- **Event-time, not calendar-time.** State updates on each quote or trade.
- **Feature snapshots on a fixed grid**: 1s and 5s, plus event-count clocks (every 50 and 200 book events), so seasonality in event rate does not masquerade as signal.
- **Raw components preserved.** `q^b`, `q^a`, `s`, `OFI`, `AD`, `OFI/AD`, `I`, microprice deviation, signed aggressive volume, absorption pair, per-window counts — all stored separately. No composite score. Explicitly required by the brief and correct: a collapsed score cannot be decomposed after the fact.
- **Storage**: Parquet on disk, one file per (symbol, session), read-only after write, content-hashed into a manifest. Postgres holds manifests and derived grid rows only. Putting 2×10⁹ quote rows into Postgres is the fastest way to end this project.
- **Reuses**: the `iter_stock_trade_pages` streaming/checkpoint pattern, the `_PrevailingQuote` walker, the CKS `e_n` kernel, the frozen-manifest + immutability-trigger pattern.

### Stage 2 — Prediction test (no strategy, no thresholds)

Straight into the alpha map with a sub-second grid.

**Horizons, justified before any outcome is seen:**

| Horizon | Justification |
|---|---|
| next mid change | Gould-Bonart's native unit; queue imbalance is documented to predict exactly this |
| next 2 mid changes | The documented decay point of queue-imbalance information |
| 1 s | **CJP's own conditioning horizon** (§12.2.3 records the mid-price change 1 s after each MO, conditional on imbalance regime). Below plausible retail actionability, so it is an information-content bound, not a tradable claim — but it is the rung with the best external benchmark |
| 5 s | Centre of Stoikov's reported 3-10 s microprice accuracy peak |
| 10 s | Upper end of that peak |
| 30 s | CKS bucket scale; OFI impact is measured here |
| 60 s | Upper bound and the join point with the existing alpha map's 60 s rung |

CJP's imbalance ACF remains significant to ~200 s, so a rung beyond 60 s would be
measuring a signal that has not decorrelated from its own past — another reason
the ladder stops where it does.

Stop at 60 s. Beyond it, the existing alpha map already covers the ladder and
OFI's documented content is dominated by lower-frequency effects. **Horizons are
fixed here and cannot be changed after any result is seen** — that is the
project's rule and the most likely place to break it.

**Measurement, all existing machinery:**
- `horizon_cost_feasibility` oracle preflight kills unaffordable horizons before any feature is scored
- Every `(feature, transform, horizon, slice)` cell predeclared through `declare_trials` — the deflated Sharpe is charged for the grid, not the winner
- `expanding_normalization` per (symbol, time-of-day), strictly prior data
- `residualize_cross_section` so market-wide flow does not read as eight independent signals
- Benjamini-Hochberg across every cell; `monotonicity ≥ 0.70`; PBO via CSCV with the standing rule that **PBO > 0.5 authorizes nothing from the grid**
- Chronological 50/30/20 with embargo; confirmation stays locked

**Incremental information beyond price alone (Step 4's actual question).** Two
nested comparisons per horizon:
1. Baseline: lagged mid returns / tick signs only.
2. Baseline + book features.

Report the increment in out-of-sample R² / rank IC, not the level. A book
feature that only recovers short-horizon return autocorrelation has added
nothing.

**CJP §3.5 makes this baseline unusually easy to specify, and unusually easy to
beat by accident.** For AAPL they find price changes strongly mean-revert — an
ask uptick is followed by a downtick 57 % of the time, a bid uptick by a downtick
63.5 % — and the two-step transitions confirm ~59-60 % reversal. But their
decisive observation is that the *conditional* two-step transition rates are
almost identical to the *unconditional* ones (ask: 59.5 / 59.1 conditional vs
59.3 unconditional). Their conclusion: "even though price changes tend to be
reversed, the direction of the current price change does not carry additional
information about future price changes."

So the honest baseline is the **unconditional** reversal rate, not a fitted
tick-sign model. Any book feature must beat ~59 % directional accuracy on the
next mid change to have contributed anything at all — and a naive classifier
that learns nothing but bid-ask bounce will land right on that number and look
impressive. This single fact should be a hard-coded reference line in every
cell report.

**Effective sample size, stated up front.** Quote updates at millisecond
spacing are massively autocorrelated; 10⁹ observations is not 10⁹ degrees of
freedom. All inference clusters by session (`session_clustered_ic`, already
built) with a stationary block bootstrap over sessions. Every reported cell
carries its effective N alongside its raw N. This is the specific failure the
brief names, and at this data rate it is the easiest one in the world to commit.

### Stage 3 — Economic test

Gate: `E[|ΔP|] > spread + fees + slippage + adverse selection + model uncertainty`.

The existing `cost_hurdle` (round-trip cost × 2.0 safety) is necessary and not
sufficient here, because it prices a *taking* strategy. Two additions:

- **Adverse selection term.** CJP §10.4.2 gives the formalism: the mid follows
  `dS_t = (ν + α_t)dt + σ dW_t`, where `α_t` is a zero-mean-reverting short-term
  alpha that jumps up on buy-MO arrivals and down on sell-MO arrivals,
  `dα_t = −ζ α_t dt + η dW^α_t + ε⁺ dM⁺_t − ε⁻ dM⁻_t`. Their statement of the
  stakes is the cleanest in the literature: a market maker operating at time
  scales where she cannot *see* `α_t` "will not only be sub-optimal, but will
  lose money to better informed traders — traders who are better informed will
  pick-off the LOs posted by the less informed MM."

  Empirically this is the realised-spread decomposition
  `effective spread = realised spread + price impact`, measured at 5 s / 30 s
  post-fill on the actual quote stream. Harvesting an OFI signal passively means
  being filled by exactly the flow the signal says is coming; `α_t` *is* the
  order-book signal, and the adverse-selection cost is what it costs to not have
  it.

  **This inverts the value proposition and is worth stating plainly.** CJP's
  framing means order-book information has two uses: an offensive one (predict
  and trade), which needs the edge to exceed cost; and a defensive one (know when
  *not* to rest an order, or when to pull one), which only needs the edge to
  exceed zero. The defensive use is far more likely to survive Stage 3, and it is
  the same finding that §10 reaches from the other direction.

- **Latency term.** A KefTrade decision reaches Alpaca in ~10¹-10² ms. Every
  cell must be re-measured with the signal read at `t` and entry at `t + Δ` for
  a predeclared latency ladder (50 ms, 250 ms, 1 s). If the edge dies at 250 ms
  it does not exist for this platform, regardless of its t-statistic at `t`.

  CJP §3.3-3.5 supplies the scale. **The median interarrival time of a change in
  either the bid or the ask is 3 ms (8 ms for the bid alone)** for AAPL. At a
  100 ms round trip, on the order of *thirty* book updates occur between decision
  and arrival. Their conclusion is direct: "for latencies greater than 8 ms, if
  you submit an MO, by the time it hits the market, the price may have moved away
  … Latency, therefore, introduces execution risk specially for traders who are
  not colocated." They also note that a broker-intermediated retail path carries
  "substantial delay" relative to a direct feed, and colocation relative to both.

  KefTrade is on the slowest of the three paths described. The latency ladder is
  therefore not a robustness check — it is the primary test.

**Tick-size reality check.** CJP §3.4 makes relative tick size the organising
variable: one cent is 0.2 bps of AAPL at $500 but 2.5 bps of ORCL at $40, and
"one would expect more frequent price movements for AAPL than for ORCL." Their
2013 median quoted spreads, converted to bps, span two orders of magnitude —
AAPL 2.9 bps, MENT 5.5 bps, FARO 29.5 bps, ISNS 419 bps — and for MENT the
one-cent minimum is *binding* in ~50 % of minutes, which is the definition of a
large-tick name.

This directly determines symbol selection in §4: the 8 predeclared symbols must
straddle the regime, because Gould-Bonart's effect is strongest exactly where
the tick binds and the queue matters, while the cost hurdle in bps is *worst*
there. Those two facts point in opposite directions, and the experiment is
partly about which one wins.

### Stage 4 — Strategy

Only against a specific cleared cell, via the existing
`--evidence-basis alpha_map_cleared` gate that validates against
`cleared_cell()` at creation and records the cleared horizon so a mismatched
holding period is visible. If nothing clears, the deliverable is the documented
rejection.

### Hard stop

Predeclared, so it cannot be renegotiated later:

> If Stage 2 on a full year of Tier-0 data produces no cell with
> `information_below_cost` **or better** at any horizon ≤ 60 s — that is, if the
> book contains no measurable forecast at all — the direction is retired and no
> vendor data is bought.
>
> If cells reach `information_below_cost` but none survives the Stage-3 latency
> ladder at 250 ms, **the finding is that order-book alpha exists and is not
> harvestable by KefTrade**, and the direction is retired as a *standalone alpha*
> source. It may then be re-scoped to execution-cost reduction (§10), which is a
> different question with a different bar.

---

## 6. Leakage and selection-bias risks

Ranked by how likely they are to actually happen here.

**1. Quote-trade sequencing (the classic microstructure leak).** A quote posted
*after* a trade cannot be the quote it crossed. `_PrevailingQuote` handles this
for classification with `QUOTE_LAG = 0`, but at event-time resolution
`QUOTE_LAG` should be **predeclared and non-zero** — with SIP reporting latency,
a quote timestamped microseconds before a trade may still have arrived after it.
Test sensitivity at 0 / 1 ms / 5 ms and report all three rather than picking one.

**2. Timestamp collapse turning into lookahead.** If same-microsecond updates
are collapsed last-writer-wins (§2.4), the surviving row is the *latest* state
of that microsecond — which is future information relative to a trade inside it.
This is a live leak in the current schema, not a hypothetical.

**3. Feature/outcome sampled on the same clock.** If a feature window ends at
`t` and the forward return starts at `t`, any shared boundary event contaminates
both. Entry must be the first tradable price *strictly after* the decision
instant — the alpha map already enforces the bar-open rule; the sub-second
version needs the same rule written explicitly.

**4. Search exposure already spent.** The trial ledger is cumulative and this
grid is large: 12 features × 4 transforms × 7 horizons × slices runs to
thousands of cells. Every one is charged. `effective_trials_for_run` must
include the prior programme's count — the candle/gap/news/sector work already
consumed budget, and the deflated Sharpe must reflect that.

**5. Symbol selection.** 8 symbols predeclared **before** any measurement,
chosen on a stated structural criterion (tick-size regime and liquidity band),
never on results. Any symbol added later is a new declaration and counts.

**6. Survivorship.** `survivorship_bias_present: true` still stands — the pool
is drawn from currently-tradable names. Less damaging at second-scale horizons
than at daily, but it does not vanish and should stay flagged.

**7. Horizon shopping.** The single most likely violation. The ladder in §5 is
justified from Stoikov/Gould-Bonart/CKS *before* any outcome; adding a rung
after seeing results is a new declaration and counts.

**8. Regime concentration.** A year of data can be dominated by a few volatile
sessions. `concentration_report` and `effect_size_drift` already exist in
`intraday_research_power.py` and must run.

**9. Venue rotation as fake alpha — the novel one.** If rotation correlates with
genuine flow (venues do gain and lose the NBBO for real reasons), rotation-driven
OFI could show *real* predictive power that is entirely an artefact of the
consolidation mechanism and vanishes on a real book. Stage 0a measures its
magnitude; Stage 2 must additionally report every OFI cell **with and without
rotation events excluded**. If the effect lives only in the rotation component,
that is a finding about the SIP, not about the market.

**10. Existing leakage audit must be extended.** `perturb_future_candles` proves
no output moves when the future is perturbed. It operates on candles. An
equivalent `perturb_future_quotes` is required, or Stage 1 has no leakage test
at all.

**11. Halted-market quotes — a contamination we currently have no filter for.**
This one comes straight from CJP §4.3.1 and is worth quoting because it is a
mistake they made and documented: during the NASDAQ halt of 2013-08-22, messages
kept arriving and being timestamped, orders were cancelled en masse, and the bid
and ask "moved dramatically," producing "huge and also negative artificial
spreads" in their own table. Their warning is explicit — unfiltered data of this
kind "could generate significant distortions" for an algorithm, "especially if
it involves unsupervised/deep learning."

At 30-minute resolution a halt is diluted into a bar. At event resolution a halt
is a dense burst of extreme-imbalance, extreme-spread observations that will
dominate any tail-weighted statistic. Stage 1 must therefore:

- Filter on trading-status and LULD state, not just clock time. Alpaca carries both as stream channels; whether either is retrievable historically is an **open question the Stage 0 probe should answer**, because if it is not, halts must be detected from the quote stream itself (crossed/locked markets, spread blowouts, zero-trade gaps) and excluded by a predeclared rule.
- Exclude, never winsorize. Winsorizing a halt turns it into a plausible-looking extreme observation, which is worse.
- Report excluded time as a coverage statistic, so a symbol that spent a meaningful share of the window halted is visible rather than silently thinned.

`normalize_stock_quote` already rejects crossed and non-positive quotes
([alpaca.py:348](../apps/api/app/providers/alpaca.py:348)), which catches the
worst of it — but it rejects them **silently and row-by-row**, with no counter
and no session-level flag. That is exactly the shape of filtering that hides a
halt instead of reporting one.

---

## 7. Estimated storage, compute and data cost

**These are engineering estimates, not measurements.** Stage 0b replaces them
with real numbers, which is much of why Stage 0 exists.

### Tier 0 (Alpaca SIP, no new spend)

| | Estimate | Basis |
|---|---|---|
| Quote updates / liquid symbol / session | 0.5-3 M | Order-of-magnitude; TSLA is reported to replace its quote roughly every 0.3 ms in active periods, which is the upper tail |
| 8 symbols × 252 sessions | ~1-6 × 10⁹ rows | |
| Raw JSON over the wire | 150-700 GB | ~120 B/row |
| Parquet, columnar + dictionary-encoded | **30-120 GB** | ~20 B/row |
| Same rows in Postgres with indexes | 300 GB-1.5 TB | ~200 B/row — **rejected** |
| Derived 1 s feature grid | 8 × 252 × 23,400 ≈ 47 M rows | Comfortably a Postgres table |
| REST requests at 10 k/page | 10⁵-6 × 10⁵ | `MAX_PAGE_LIMIT = 10000` |
| Wall-clock download | **1-5 days** | Rate limits, pagination, retries; must be checkpointed and resumable or it will never finish |
| Marginal $ | **$0** | Algo Trader Plus already held |

Compute: feature construction is a single linear pass per symbol-session,
embarrassingly parallel, hours not days on existing hardware. The alpha map's
streaming outcome-grid work (the 7.9 GiB OOM fix already in git history) is
directly relevant precedent — the sub-second grid is ~1,400× denser than 1 m
bars and the same streaming discipline is mandatory from line one.

### Tier 1 (Databento, only if Stage 0 + Stage 2 justify it)

Databento prices historical data **per GB of uncompressed binary**, at the same
rate across schemas (MBO, MBP-10, TBBO). New accounts get **$125 in free
credits**, expiring after 6 months, one set per team. There is no minimum spend.

The per-GB rate is not published on the pages fetched and must come from their
calculator — **an explicit action item, not a guess.** What can be said: MBO for
a liquid name runs several GB per symbol-day uncompressed and MBP-10 is
materially smaller, so the Tier-1 scoping question is "how many symbol-days does
$125 buy in MBP-10" — quite possibly the whole Tier-1 probe at zero marginal
cost. Run the calculator before any purchase decision.

### Human cost

Stage 0: ~2-3 days. Stage 1 engine: ~1-2 weeks. Stage 2 wiring into the alpha
map: ~1 week, most of it reuse. This is not a small project, and Stage 0 is
deliberately cheap enough to abandon.

---

## 8. What I would implement first

Exactly one thing, and nothing downstream of it:

**The Stage 0 feasibility probe.** ~300-400 lines, one CLI command, two symbols,
five sessions, outside the research window.

1. **The power calculation — do this first, it is free.** Feed CJP's ~0.36 bps
   incremental effect at 1 s into the existing `required_sessions_for_power`
   machinery and see what event count it demands. Zero lines of ingestion code.
   May end the direction in an afternoon.
2. `iter_stock_quote_pages` — the streaming/checkpointing sibling of
   `iter_stock_trade_pages`. Removes the 1 M ceiling and the accumulate-in-memory
   shape. Needed by everything later regardless of the outcome.
3. **Nanosecond fidelity**: keep the raw `t` string, parse to integer nanoseconds
   alongside the `datetime`, and count collisions instead of overwriting them.
4. **The rotation decomposition (0a)** — the number that decides the direction.
5. **Collapse rate and observed quote rates (0b)** — replaces every estimate in §7.
6. **Halt/status coverage** — determine whether trading-status and LULD history
   are retrievable from Alpaca at all, and if not, what a quote-stream-only halt
   detector would have to look like (§6.11).
7. Run the existing `classifier_agreement_report` (0c) — no new code.

Deliverable: one report, four numbers, measured against the predeclared 30 %
kill threshold and the power result. Nothing is declared, no trial budget is
spent, no strategy is touched.

**Not first, explicitly:** the state engine, the feature set, the sub-second
outcome grid, any alpha-map declaration, any Databento purchase. Building the
engine before knowing whether the input is a real queue is how this becomes
another expensive null.

---

## 9. Reusable KefTrade infrastructure

| Component | Reuse |
|---|---|
| `intraday_alpha_map.py` (2,461 lines) | **Near-total.** Verdict taxonomy, cost hurdle, oracle preflight, PBO/CSCV, BH, monotonicity, expanding normalization, cross-sectional residualization, session-clustered IC. Needs a sub-second grid source and a latency ladder — not a rewrite |
| `iter_stock_trade_pages` | Pattern copied verbatim for quotes: streaming, checkpointed, `Retry-After`-aware, truthful `exhausted` |
| `intraday_trade_flow.py` | Lee-Ready, `_PrevailingQuote`, auction-print exclusion, `unclassified_share`, classifier agreement — used as-is at event resolution |
| CKS `e_n` kernel (`intraday_execution_costs.py:487-494`) | Correct math; extract from the 15m/30m aggregator into an event-time function |
| `intraday_trial_ledger.py` | As-is. Carries the prior programme's cumulative exposure into this grid |
| `intraday_research_power.py` | As-is for power/concentration/drift; the 5 bps minimum effect needs restating for sub-minute horizons **before** measurement |
| `intraday_research_leakage.py` | Pattern reused; needs a `perturb_future_quotes` analogue |
| Frozen-dataset architecture | Manifests, content hashing, immutability triggers, `research_dataset_*` isolation, chronological 50/30/20 + embargo, locked confirmation |
| `intraday_execution_costs.py` | Calibration scaffolding reused; **needs** realised-spread/adverse-selection and latency terms |
| Paper Lab + `evidence_basis` gating | As-is, and only at Stage 4 |
| Test suite (128 files) | Frozen-evidence, session-boundary and streaming-loader tests are the templates for the sub-second equivalents |
| `intraday_quote_snapshots` | **Schema change required** — nanosecond key, collision counter, quoting-exchange columns |
| `aggregate_microstructure_bars` 15m/30m output | Not reusable as a signal. Keep for cost calibration only |

Roughly 70-80 % of the statistical and protocol machinery transfers unchanged.
The new work is concentrated in ingestion fidelity and the event-time state
engine.

---

## 10. GO / NO-GO

### Recommendation: **GO on Stage 0 only. NO-GO on everything downstream until Stage 0 reports.**

**What the literature actually supports.** That order-book state predicts
short-horizon price movement is among the best-replicated findings in
microstructure. Gould-Bonart find queue imbalance significantly predicts the
next mid move across 10 Nasdaq stocks. CKS find a linear, seasonality-robust,
cross-stock-stable OFI/price relation that outperforms trade imbalance.
Cont-Cucuringu-Zhang improve it further by integrating levels. Stoikov's
microprice beats mid and weighted-mid at 3-10 s. **The prediction question is
close to settled, and it is settled in favour.**

**What the same literature says about money.** Kolm, Turiel and Westray — who
did this at the most granular level available, on 115 Nasdaq stocks, with deep
learning — state plainly that predictability does not translate into trading
profits: the horizons are short, the price moves correspondingly small, and
transaction costs, implementation details and delays consume them. Other work
adds that the value of predicting liquidity-consuming flow erodes with latency,
because you cannot cancel and reinsert on each book change.

**CJP Table 12.1 lets us put an actual number on that.** They record the
mid-price change 1 second after each market order, conditional on the imbalance
regime immediately prior, for ORCL (open $33.72, so one tick ≈ 2.97 bps):

| Buy MOs | Δmid = 0 | +1 tick | +2 ticks | −1 tick | E[Δmid] |
|---|---|---|---|---|---|
| Neutral (Z = 0) | 0.77 | 0.21 | — | 0.01 | ≈ 0.20¢ |
| Bid-heavy (Z = +2) | 0.70 | 0.28 | 0.02 | — | ≈ 0.32¢ |

The **incremental** forecast from knowing the imbalance regime — the quantity
this whole experiment exists to measure — is the difference between those rows:
about **0.12 cents, or ≈ 0.36 bps**, at a 1-second horizon, conditional on a
market order having just arrived. ORCL's quoted spread at the time sat at or
near the one-cent tick, ≈ 3 bps.

So the best-documented version of this signal, measured by the authors of the
standard textbook on it, on the stock and horizon they chose to showcase it,
produces an incremental expected move of roughly **one-eighth of the round-trip
spread it would cost to capture by crossing.** That is not a marginal call. And
it is measured under conditions strictly more favourable than ours: a real
single-venue Nasdaq book with true queue sizes, no venue-rotation noise, no
hidden-order blindness, and zero latency.

This is the arithmetic that Step 6 asks for, and it is available *before* we
collect a single row.

**Where KefTrade sits.** Retail routing through Alpaca Paper, ~10¹-10² ms
decision-to-broker latency against a 3 ms median book-update interval, no maker
rebates, no queue-position control, no colocation, and — decisively — **no
queue**, because the only book data available is a single-venue NBBO fragment
that is also blind to ~45 % of executed liquidity in the most liquid names. The
strategies that harvest this alpha in the literature are passive,
queue-position-dependent, and latency-sensitive at the microsecond scale. That
is precisely the capability set KefTrade lacks.

**So the honest expected outcome is a well-documented rejection**, most likely
landing on `information_below_cost` rather than `no_information` — the book
contains real forecast, and it is smaller than the cost of reaching it. CJP's
own ORCL numbers say the ratio is roughly 1:8 under conditions better than ours.
That outcome has genuine value: it is a *different* finding from the candle
programme's, it is decisive, and it closes the direction properly rather than
leaving it as an untested "we should try order flow someday."

**One methodological gain worth taking immediately.** `intraday_research_power.py`
currently uses a *declared* minimum tradeable effect of 5.0 bps with 60.0 bps
dispersion, chosen as a policy input because nothing better existed. For this
experiment we now have a **literature-derived** effect size — ~0.36 bps
incremental at 1 s — and it should be declared as such, with its provenance
recorded, before measurement. Two things follow, and both are findings:

- The required event count at that effect size will be very large. If it exceeds what a year of Tier-0 data can supply, **the experiment is underpowered before it starts**, and that is knowable now, for free, from a spreadsheet rather than from a year of ingestion.
- If we instead keep the 5.0 bps policy minimum — the level at which KefTrade would actually care — then CJP's number already sits an order of magnitude below it, and the honest declaration is that we are testing whether *our* symbols and venue differ from their published result by more than 10×.

Run this calculation as part of Stage 0. It is arithmetic, it costs nothing, and
it can retire the direction before any data is collected.

**Why GO anyway, bounded.** Three reasons:

1. Stage 0 costs 2-3 days and $0, and can retire the direction on its own.
2. The rotation-contamination question is unanswered *today* and KefTrade is
   already computing OFI from this feed. Whatever else happens, that number
   needs to exist.
3. There is a real, reachable payoff that is not standalone alpha: **execution
   cost reduction.** Microprice and queue imbalance are the standard tools for
   deciding when to cross a spread and where to rest an order. KefTrade already
   pays spread on every simulated trade and models cost from quoted spreads with
   no book-state term. Even a total failure at Stage 3 leaves a better cost model
   and better entry timing for whatever the platform eventually trades. That is
   a smaller prize than alpha and it is not nothing.

**NO-GO, explicitly:**
- No Databento purchase before Stage 0 passes and Stage 2 reports on Tier 0.
- No state engine before the rotation number exists.
- No alpha-map declaration — and therefore no trial-budget spend — before Stage 1 is built and tested.
- The locked confirmation period stays locked.

**The one thing that would flip this to NO-GO immediately:** Stage 0a returning
rotation above 30 % of gross `|e_n|`. At that point Alpaca L1 is not a book, the
Tier-0 experiment cannot be run as designed, and the decision becomes "buy
Tier-1 or stop" — with a much weaker prior than we have now, because we would
have learned nothing about whether the effect is present in our symbols.

---

## Sources

- **Cartea, Jaimungal & Penalva — *Algorithmic and High-Frequency Trading*, Cambridge University Press, 2015.** Ch. 12 (order imbalance definition, Markov-chain and MO-intensity models, Table 12.1 conditional 1 s price-change distribution); §3.3-3.6 (interarrival times, latency, tick size, non-Markovian price changes, market fragmentation Table 3.8); §4.3-4.5 (spreads, price impact, messages and cancellations Tables 4.14-4.17, hidden orders Table 4.18); §2.1.3 (measuring liquidity, price impact λ, autocovariance of price changes); §10.4.2 (short-term-alpha and adverse selection).
- [Cont, Kukanov & Stoikov — *The Price Impact of Order Book Events* (arXiv 1011.6402; JFEc 2014)](https://arxiv.org/pdf/1011.6402)
- [Gould & Bonart — *Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book* (arXiv 1512.03492)](https://arxiv.org/abs/1512.03492)
- [Stoikov — *The Micro-Price: A High Frequency Estimator of Future Prices* (SSRN 2970694)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694)
- [Cont, Cucuringu & Zhang — *Cross-Impact of Order Flow Imbalance in Equity Markets* (arXiv 2112.13213; Quantitative Finance 2023)](https://arxiv.org/abs/2112.13213)
- [Kolm, Turiel & Westray — *Deep Order Flow Imbalance: Extracting Alpha at Multiple Horizons from the Limit Order Book* (Mathematical Finance 2023)](https://onlinelibrary.wiley.com/doi/10.1111/mafi.12413)
- [Huang, Lehalle & Rosenbaum — *Simulating and Analyzing Order Book Data: The Queue-Reactive Model* (arXiv 1312.0563)](https://arxiv.org/abs/1312.0563)
- [Alpaca — Real-time Stock Data (channels, nanosecond timestamps, no depth-of-book)](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
- [Alpaca community forum — NBBO size is single-exchange, not aggregated depth](https://forum.alpaca.markets/t/does-alpaca-have-nbbo-quotes-from-all-exchanges/14055)
- [Alpaca — About Market Data API (Basic vs Algo Trader Plus, SIP entitlement)](https://docs.alpaca.markets/us/docs/about-market-data-api)
- [Databento — pricing ($/GB usage-based, $125 new-account credits)](https://databento.com/pricing)
- [Databento — Nasdaq TotalView-ITCH (XNAS.ITCH), MBO/MBP-10](https://databento.com/datasets/XNAS.ITCH)
- [Exegy — UTDF/UQDF: SIP feeds are top-of-book; Level 2 depth is omitted](https://www.exegy.com/utdf-uqdf-basics-nasdaq-sip-feeds/)

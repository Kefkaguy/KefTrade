# Stage 3.6 — News-Triggered L3 Consensus Economic Experiment

Status: FROZEN BEFORE ECONOMIC OUTCOME

Version:
tier1_stage36_news_l3_consensus_v1

Evidence class:
exploratory mechanism development

Prior cumulative effective primary research trials:
530

Fresh primary economic specifications:
1

Effective trials after economic outcome is viewed:
531

Paper trading authorized:
false

Live trading authorized:
false


## Research question

Can a news-triggered state combined with the four already-frozen Nasdaq
Level-3 predictors identify executable five-minute long/short trades
with materially larger net expectancy?

Primary economic target:

mean executable net return >= +5.0 bps

Equivalent economics:

$5,000 trade:
$2.50/trade
$25/day at 10 trades/day
$6,300/year over 252 trading days

$10,000 trade:
$5.00/trade
$50/day at 10 trades/day
$12,600/year over 252 trading days

Stretch target:

mean executable net return >= +8.0 bps

Equivalent economics:

$5,000 trade:
$4.00/trade
$40/day at 10 trades/day
$10,080/year over 252 trading days

$10,000 trade:
$8.00/trade
$80/day at 10 trades/day
$20,160/year over 252 trading days


## Dataset

Nasdaq XNAS.ITCH MBO.

Frozen June 2–30 2025 Stage-1 batch.

Symbols:

AAPL
MSFT
NVDA
TSLA
INTC
CSCO
AMD
CMCSA

Exactly 20 certified Nasdaq trading sessions.

This dataset is already scientifically used, therefore this experiment
is exploratory only.


## News timestamp

Historical event timestamp:

known_at

Audit established:

known_at == updated_at

for all 115,613 inspected news rows.

received_at is forbidden as the historical event timestamp because it
represents later backfill ingestion.


## Story identity

story_id:

COALESCE(content_hash, article_id)


## Quiet-period eligibility

Require at least 60 minutes since the previous same-symbol news story.

The quiet-period calculation includes ALL previous same-symbol news,
including premarket and otherwise non-tradable stories.

No future price outcome participates in this rule.


## Eligible event times

Only exact certified Nasdaq sessions.

News timestamp must fall between:

09:30:00 ET

and

15:54:29 ET


## Observation interval

t0 = known_at

decision time:

td = t0 + 30 seconds


## 30-second price movement

The 0–30 second midpoint move may be stored as a diagnostic.

There is NO minimum shock threshold.

There is NO maximum shock threshold.

Previously inspected shock magnitudes may not be used to define a new
threshold after outcomes are observed.


## Frozen L3 predictors

Use exactly:

200ev|next_2_changes
200ev|next_change
50ev|next_2_changes
50ev|next_change

Use Stage-3.5's exact per-date out-of-sample reconstruction.

Do not refit a new model family.

Do not select one previously better-performing model.

For each cell select the latest finite prediction satisfying:

t0 <= feature_available_ts_recv <= td

A prediction earlier than t0 is stale and forbidden.


## Consensus

All four frozen models must provide non-zero directional predictions.

4 same sign:
TRADE in that direction.

3 versus 1:
TRADE in majority direction.

2 versus 2:
NO TRADE.

Any unavailable prediction:
NO TRADE.

Any zero prediction:
NO TRADE.

Initial news-price direction does NOT determine trade direction.

Continuation/reversal relative to the initial move is diagnostic only.


## Frozen pre-outcome counts

These counts must reproduce exactly before an economic run:

measured events:
259

4_of_4:
147

3_of_4:
21

2_vs_2:
81

incomplete:
10

strong-consensus candidate events:
168

Mismatch:
REFUSE TO RUN.


## Execution

Trade size:
100 shares

Entry:
marketable

Entry request:
td

Entry arrival:
td + 250 milliseconds

Exit:
marketable

Exit request:
td + 5 minutes

Exit arrival:
td + 5 minutes + 250 milliseconds

Therefore arrival-to-arrival holding time:

exactly 5 minutes


## Directional execution

LONG:

entry consumes asks
exit consumes bids

SHORT:

entry consumes bids
exit consumes asks


## Book semantics

Reuse the certified Stage-3 BookReplay and MBO reconstruction.

Book state at instant t may contain only records:

ts_recv <= t

Displayed liquidity only.

Maximum displayed levels:

10

Insufficient liquidity for the complete 100-share leg:
execution failure.

Never invent a worse hypothetical fill.


## Data certification

Raw source must be resolved through existing Stage-1 manifests.

Raw hashes must be verified.

Entry and exit must fall inside actual raw receive-time coverage.

F_BAD_TS_RECV contamination during the required timing interval:
candidate fails closed.

No stale final-book state after EOF may be used.


## Fees

Primary fee schedule:

reuse the frozen Stage-3 PRIMARY_FEE_SCHEDULE_NAME and FEE_SCHEDULES.

Do not introduce a new fee assumption.


## Primary net return

For direction s:

realized_return_bps =
s * (exit_fill - entry_fill) / entry_fill * 10000

primary_net_return_bps =
realized_return_bps - primary_fees_bps


## Primary sample gate

Minimum executable trades:

100

Minimum trading sessions represented:

15

If either condition fails:

verdict =
not_authorized_insufficient_executable_sample


## Statistical inference

Primary inference:

clustered by trading session

Required:

day-clustered t >= 3.0

There is exactly ONE primary Stage-3.6 economic specification.


## Primary success

Require all:

sample gate passed

mean primary net return >= +5.0 bps

day-clustered t >= 3.0

Verdict:

news_l3_5bps_mechanism_supported_exploratory


## Stretch success

Require all:

sample gate passed

mean primary net return >= +8.0 bps

day-clustered t >= 3.0

Report:

stretch_8bps_supported = true


## Failure

If sample gate passes but the 5-bps requirement fails:

verdict =
no_5bps_news_l3_mechanism


## Diagnostics allowed

May report without changing the verdict:

gross midpoint return
fill-to-fill return
execution cost
spread/book cost
fees
levels walked
failure counts
per-day net return
per-symbol net return
4/4 versus 3/4
continuation versus reversal
30-second shock bins


## Forbidden post-outcome adaptation

Do not change after economic outcome is viewed:

symbols
observation interval
holding horizon
consensus threshold
shock threshold
latency
quiet period
trade size
fee assumptions

in order to manufacture a passing result.


## Authorization

A passing exploratory result authorizes ONLY:

an untouched external confirmation experiment.

It does NOT authorize:

paper trading
live trading
broker execution
capital deployment


## Trial ledger

Before economic outcome:

530 effective trials

After Stage-3.6 economic outcome is exposed:

531 effective trials

regardless of pass or fail.

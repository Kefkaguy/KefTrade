# RUG — Random Unbiased Generator

RUG is KefTrade's broad, reproducible candidate generator. It is a generator,
not a judge: every candidate is submitted to the existing campaign workers,
backtester, validation gates, learning engine, elite collection, and archive.
RUG cannot promote a candidate or weaken a validation threshold.

## Search dimensions

RUG varies executable RSI periods and thresholds, EMA fast/slow pairs, entry
structures, ATR stop distances, reward targets, holding periods, UTC entry
windows, volatility thresholds, volume thresholds, assets, and timeframes.
Invalid EMA pairs and duplicate executable configurations are discarded before
jobs are queued.

The random stream is reproducible from `(generator version, seed, batch index)`.
This makes every result auditable while giving later batches a disjoint search
stream.

## Learning loop

Each completed batch runs the existing global research-learning process. The
next batch is created only after that learning snapshot exists:

```text
RUG proposals
→ existing deterministic backtests and validation
→ successes and failure classifications persisted
→ global evidence snapshot refreshed
→ next RUG batch uses the refreshed evidence
```

With evidence available, the default allocation is 60% evidence exploitation,
30% protected random exploration, and 10% challenge tests. Before evidence
exists, it is 90% random exploration and 10% challenge; the engine does not
pretend it has something to exploit.

Challenge candidates sample regions receiving the lowest current evidence
score. They use the same validation gates and exist to retest or falsify the
engine's current beliefs.

## Million-candidate operation

`POST /research/rug/campaigns` accepts a target as large as ten million, but
queues at most 5,000 candidates in one campaign. With `auto_continue=true`,
campaign finalization queues the next bounded batch after learning completes.
This provides backpressure and prevents millions of jobs from being inserted
into PostgreSQL at once.

Example body:

```json
{
  "universe_key": "research_core_ten",
  "target_candidates": 1000000,
  "batch_size": 1000,
  "seed": 20260903,
  "auto_continue": true,
  "asset_limit": 10,
  "timeframes": ["15m", "30m"],
  "dataset_mode": "reproducibility"
}
```

`GET /research/rug/status` reports queued and completed candidates, completed
backtest jobs, collected promoted candidates, rejected candidates incorporated
into learning, failures, and active campaign IDs.

The candidate target is not the number of backtest jobs. Each candidate is
tested for every selected asset/timeframe combination, so one million
candidates over ten assets and two timeframes represents twenty million jobs.

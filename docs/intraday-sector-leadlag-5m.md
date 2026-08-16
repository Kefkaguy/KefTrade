# 5m Sector Peer Lead/Lag Research

Protocol: `intraday_sector_leadlag_5m_v1_peer_excess_spy`

This is a bounded cross-asset falsification study on immutable intraday dataset 86. It asks whether an unusual completed five-minute move in a stock's sector peers, relative to SPY, predicts the target stock's subsequent SPY-relative return over the next 5-15 minutes.

## Frozen predictor

For each eligible target:

1. Sector membership comes from `symbols.sector` and is fingerprinted at declaration time.
2. A sector must contain at least six dataset targets, giving every target at least five leave-one-out peers.
3. Build an equal-weight leave-one-out peer basket over one completed five-minute block.
4. Predictor value = peer basket 5m return - contemporaneous SPY 5m return.
5. Normalize by an expanding history for the same target and same exchange-local five-minute slot, using prior sessions only and at least 20 prior sessions.
6. `z >= +1.5` is `positive_peer_impulse`; `z <= -1.5` is `negative_peer_impulse`.
7. Require a complete 15-minute future timestamp grid for both target and SPY. Preflight checks timestamps only and reads no forward prices.

No target contemporaneous return is used as a predictor except mechanically to remove that target from its sector sum. There is no stock-pair search, sector-specific threshold, alternative z cutoff, or post-outcome predictor tuning.

## Frozen outcomes

There are six fresh cells:

- `positive_peer_impulse`: long target / short SPY at +5m, +10m, +15m.
- `negative_peer_impulse`: short target / long SPY at +5m, +10m, +15m.

Entry is the target and SPY open at the decision timestamp. Exit is the close of the final 1m bar in the horizon. Gross return is equal-notional target return minus SPY return, multiplied by the state direction.

Net return subtracts stressed round-trip target cost plus stressed round-trip SPY cost.

## Multiplicity and promotion

This family inherits 502 already-spent effective trials and adds six fresh cells:

- prior effective trials: 502
- fresh tests: 6
- cumulative effective trials: 508
- two-sided Bonferroni normal threshold: 3.8944416083800593

A cell promotes only if both discovery and validation satisfy:

- gross session/block-bootstrap 95% lower bound >= +5 bps
- net session/block-bootstrap 95% lower bound > 0
- independent-evidence readiness

Validation also requires net day-clustered t >= the cumulative threshold.

Confirmation is not read unless at least one cell promotes. Confirmation is one-shot and forward prices are queried only for the exact promoted state/horizon cells.

## Commands

```bash
python -m app.cli.intraday_sector_leadlag preflight \
  --dataset-id 86
```

```bash
python -m app.cli.intraday_sector_leadlag declare \
  --dataset-id 86 \
  --cost-calibration-id 4 \
  --prior-effective-trials 502 \
  --purpose "Bounded 5m sector peer lead-lag falsification benchmark"
```

```bash
python -m app.cli.intraday_sector_leadlag discover \
  --declaration-id <ID>
```

```bash
python -m app.cli.intraday_sector_leadlag report \
  --run-id <ID>
```

Only if `candidate_cells` is non-empty:

```bash
python -m app.cli.intraday_sector_leadlag confirm \
  --run-id <ID>
```

Do not run confirmation when no candidate cells promote.

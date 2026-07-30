# Intraday research instrument certification

A null result is only evidence about the market once the measurement path has
been shown to work. Before this protocol, a weak factor reading could mean any
of four things and the pipeline could not tell them apart:

1. genuine absence of alpha,
2. insufficient observations,
3. an evidence gate inappropriate for the signal type,
4. a pipeline or timestamp defect.

Phases 0–2 exist to remove 3 and 4 as explanations and to make 2 measurable, so
that only 1 is left when the data really is silent.

## Order of operations

```
certify  ->  declare  ->  discover  ->  confirm
```

Discovery refuses to run without both a certification id and a declaration id.
Neither is a formality: the certification is the proof that the instrument
works, and the declaration is what makes the multiple-testing correction
honest.

```bash
python -m app.cli.intraday_factor_audit certify --dataset-id 77 --timeframe 30m
```

```bash
python -m app.cli.intraday_factor_audit declare --purpose "bounded gap experiment" --timeframe 30m --factors gap_down_acceptance_continuation,gap_down_absorption_reversal
```

```bash
python -m app.cli.intraday_factor_audit discover --dataset-id 77 --certification-id 1 --declaration-id 2 --cost-calibration-id latest
```

```bash
python -m app.cli.intraday_factor_audit ledger --timeframe 30m
```

## Phase 0 — certify the instrument

`intraday_research_controls` and `intraday_research_leakage` produce one stored
verdict in `intraday_research_certifications`, covering three things.

**Controls.** A synthetic market carries a factor of known sign, strength and
holding horizon; the real measurement path must recover it, and must recover
the inverted version with the right sign. The same generator with the effect
set to zero must produce nothing. Both a continuous factor and a
directional-event factor are certified separately, because event conditioning
is a different code path. Placebos — shuffled session dates, flipped
directions, permuted symbols, resampled noise — must every one of them fail.

The symbol permutation is a cyclic rotation rather than a shuffle. Across a
two-name cross-section a shuffle leaves the true pairing intact half the time,
so half the real signal survives and the placebo passes when it should not.

**Leakage.** Every observation carries `signal_bar_timestamp`,
`decision_timestamp`, `entry_bar_timestamp` and `exit_bar_timestamp`. Leakage
is then proven by experiment rather than by reading the code: perturb every bar
from a cut point onward and re-derive the factor. A score that was knowable
before the cut must come back bit-identical, and a target that claims to span
bars after the cut must come back different. The second half matters as much as
the first — a target wired to stale bars produces a factor that cannot predict
anything however real the effect is. A factor that produced no observation on
the dataset is recorded as `not_exercised`, never as a pass.

**Calendar.** Frozen snapshots contain premarket and post-close bars.
`session[0]` is therefore not the opening half hour and `session[-1]` is not the
closing half hour, so on any day carrying an 09:00 or 16:30 bar the old code
measured a premarket open against a post-close print. All session positions now
resolve through `intraday_session_calendar`, which is half-day aware and refuses
to name a closing bar for a session whose bar complement matches neither
calendar shape.

**Published replication.** The first-to-last-half-hour calculation is
reproduced gross of costs using the paper's own close-to-close definitions
(`r1` from the previous close to the 09:30 bar close, `r13` from 15:30 to
16:00), with plain and Newey-West t-statistics. Its purpose is to separate
decay from breakage: the study ran on SPY 1993–2013, so a weak reading on
recent data is a decay finding only if the calculation itself reproduces.

## Phase 1 — factor-type evidence gates

`FactorSpec.factor_type` is one of `continuous`, `directional_event`,
`cross_sectional`, and `factor_evidence_gate` dispatches on it. A universal
positive-rank-IC requirement is the wrong instrument for a directional event,
whose score is a single signed magnitude conditioned on a rare state; its rank
correlation against a one-bar return is noise.

| type | additional requirements |
| --- | --- |
| `continuous` | positive rank IC, stable quarterly rank performance, positive net return |
| `directional_event` | positive event-conditioned net return, positive net bootstrap lower bound, stable subperiods |
| `cross_sectional` | positive cross-sectional IC, executable long-short spread net of both legs, positive net return |

The integrity requirements that apply to any claim — session clustering,
minimum day-clustered t of 3.0, stressed-cost clearance, false-discovery
control, selection adjustment gross and net — apply to all three. Retaining the
t ≥ 3 hurdle is deliberate under repeated factor testing (Harvey, Liu and Zhu).

## Phase 2 — power, regime and stability

`intraday_research_power` attaches to every measured factor: sessions and
observations required for 80% power, the minimum detectable effect, quarterly
and annual results with stability flags, symbol and sector concentration
(carrying its own coverage, since sector data is partial), market-direction and
volatility regimes, discovery-to-validation effect-size drift, and bootstrap
bounds.

`null_result_is_interpretable` is the field that matters. When it is false, the
factor was not measured — it was merely looked at with a sample too small to
resolve the effect being claimed, and the reading is not evidence of absence.

Volatility regimes use a trailing window, so the label attached to a session
never depends on what happened after it. Subperiods with fewer than 20 sessions
are flagged rather than scored: a three-session quarter argues neither way.

## Phase 2 — trial ledger

`intraday_research_trials` is append-only and cumulative. `effective_trials` is
now `max(run size, distinct factor keys ever declared, historical trials + new
trials in this run)` rather than the length of today's argument list. Reusing
the same validation data for a thirteenth idea is a thirteen-trial problem, and
the deflated Sharpe and false-discovery correction are charged accordingly
(White; Bailey and López de Prado).

Declarations are immutable and are consumed through a separate
`intraday_research_trial_declaration_uses` table. Scoring a factor that the
declaration does not name is refused outright. Declared-but-never-run tests
still count: looking at a hypothesis and choosing not to report it is still a
look.

## What is deliberately not here

Phase 3 (expanding the immutable dataset) and phase 4 (the bounded six-test
gap-down experiment) are not part of this work. The bounded experiment should
not be run until the dataset supports it — see the power figures on the
current gap families, which need several hundred sessions of events and hold
a few dozen.

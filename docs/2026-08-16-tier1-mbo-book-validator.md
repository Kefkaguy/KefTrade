# Tier-1 MBO book-reconstruction validator — validation report

**Scope:** book reconstruction and state integrity only.
**Not built:** features, forward returns, prediction tests, Alpha Map cells,
strategies, thresholds. **No further files downloaded.**

## Why this exists before anything else

Stage 0 retired Alpaca L1 because 45.224 % of gross order-flow imbalance turned
out to be venue rotation rather than order flow. Tier 1 replaces that feed with
a real single-venue book. The obvious temptation is to start measuring; the
Stage 0 lesson is that a measurement built on a feed nobody checked is a
measurement of the feed. So the first question is narrower: **can the book be
reconstructed from these events at all, and does the reconstruction stay
internally consistent for a whole session?**

## Event semantics, taken from source

Not inferred. Databento publish two reference implementations — *State
management of resting orders* and *Limit order book construction* — and the
first uses `XNAS.ITCH`, the exact dataset here.

| Action | Effect on the book |
|---|---|
| `A` Add | Insert a new order at its price level |
| `C` Cancel | Remove **some size** from a resting order; remove it at zero |
| `M` Modify | Set the order's **new absolute** price and/or size |
| `R` Clear | Remove every resting order |
| `T` Trade | An aggressing order traded. **No book change.** |
| `F` Fill | A resting order was filled. **No book change.** |
| `N` None | No book change; may still carry flags |

**The `T`/`F` rule is the one that decides whether the book is right.** Databento
state the reason directly: fills are always accompanied by a cancel that does
update the book. Applying `F` as well double-counts every execution and quietly
drains the book. `test_trade_and_fill_do_not_change_the_book` and
`test_the_accompanying_cancel_is_what_reduces_the_book` pin both halves.

Second trap: `C` carries a **delta** to subtract, `M` carries the **new absolute**
size. Swapping them is silent and wrong in both directions.

Priority on `M`: changing price, or *increasing* size, loses queue position;
decreasing size keeps it. All three cases are tested against the resulting
per-level order ordering, not just the aggregate size.

## Two mechanics that decide whether the numbers mean anything

**`F_LAST`.** One venue event normalizes into several records, and the book is
incoherent *between* them. Every book-level check — crossed book above all — is
evaluated only where `F_LAST` is set.
`test_crossed_check_runs_only_at_event_boundaries` runs the same four records
twice, with the rule on and off: **0 violations with it, 1 without.** On a real
session the naive version would bury the genuine findings under thousands of
transient ones.

**Snapshots.** From 2024-02-10 onward Databento open a session with an `R` clear
carrying `F_SNAPSHOT`, then `A` records (also `F_SNAPSHOT`) reinserting each
resting order in priority order. Those are book state, not new orders, so they
are counted separately — a missing snapshot and an empty one are different
facts. They are also exempt from sequence/timestamp monotonicity, because they
carry the snapshot's own generation timestamp.

That exemption produced the one real bug found during development: the first
version skipped *checking* snapshot records but still let them set the baseline,
so the first live record was flagged as a sequence regression against the
snapshot. Caught by
`test_snapshot_records_are_exempt_from_sequence_monotonicity`, fixed by keeping a
separate live-stream baseline.

## What the validator reports

- **BBO** — best bid/ask price, aggregate size, order count per level
- **Depth** — N levels per side, price/size/count
- **Order counts** — resting orders, levels per side, resting size per side, peak resting orders
- **Snapshot accounting** — snapshot records, clears, adds; whether a snapshot was present at all
- **Flag accounting** — `F_LAST`, `F_TOB`, `F_MBP` record counts; how many book states were checked

### State-integrity violations

| Kind | Meaning | Fatal |
|---|---|---|
| `unknown_order_cancel` | Cancel for an order not resting | yes |
| `unknown_order_modify` | Modify for an order not resting | yes |
| `unknown_order_fill` | Fill naming an order not resting | yes |
| `duplicate_order_add` | Add re-using a live order id | yes |
| `cancel_exceeds_resting_size` | Cancel larger than the resting order — would drive size negative | yes |
| `negative_or_undefined_size` | Size is negative or `UNDEF_ORDER_SIZE` | yes |
| `crossed_book` | Best bid > best ask at an event boundary | yes |
| `sequence_regression` | Live-stream sequence went backwards | yes |
| `ts_event_regression` | Live-stream `ts_event` went backwards | yes |
| `modify_changed_side` | Order changed side | yes |
| `invalid_side_for_action` | `side=N` on a book-changing action | yes |
| `undef_price_without_tob` | `UNDEF_PRICE` outside a top-of-book update | yes |
| `unknown_action` | Action outside `ACMRTFN` | yes |
| `snapshot_after_session_start` | Snapshot record after live traffic began | yes |
| `flag_maybe_bad_book` | Publisher set `F_MAYBE_BAD_BOOK` | yes |
| `locked_book` | Best bid == best ask | **no** — legal and common |
| `flag_bad_ts_recv` | Publisher set `F_BAD_TS_RECV` | **no** — receive-clock issue, not a book issue |

Two are deliberately non-fatal. A locked book is a normal Nasdaq state; calling
it a defect would make `clean: false` meaningless. `F_BAD_TS_RECV` is a
timestamping problem on Databento's receive clock, not a statement about book
correctness.

**One deliberate divergence from Databento's reference.** Their implementation
`assert`s on these conditions. This one **records and continues**: an assertion
turns the first anomaly into a crash and hides every later one, which is the
opposite of what a validator is for. Where recovery is defined, it follows the
reference (an unknown `M` is treated as an add); where it is not, it clamps
rather than invents (a cancel exceeding resting size clamps at zero, because a
negative resting size is not a market state).

## Test results

```
34 passed, 1 skipped
```

The skip is the real-file integration check (see below). Coverage:

- **Constants** — all 19 mirrored constants pinned against the installed `databento_dbn`, so a package upgrade that moves a flag value fails the suite rather than silently changing results.
- **Actions** — add/level aggregation, partial and full cancel, modify as absolute size, all three priority cases, clear.
- **T/F/N inertness** and the accompanying-cancel rule.
- **Every violation kind** listed above, each asserted to be both *detected* and *recovered from* without corrupting the book.
- **`F_LAST` boundary rule**, demonstrated by A/B comparison rather than assertion.
- **Snapshot** preamble accounting and monotonicity exemption.
- **Top-of-book** normalization and `UNDEF_PRICE` side-clear.
- **Adapter** — `MboEvent.from_dbn` exercised against genuine `databento_dbn.MBOMsg` objects, so field names and enum-to-string conversion are pinned, not assumed.
- **File reader** — `iter_dbn_events` filters non-MBO records (metadata, symbol mappings) that would otherwise raise mid-replay.

## Status of the real CMCSA file

**Not yet run.** The file `xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst` is on the
VPS; this workstation does not have it, so
`test_real_cmcsa_session_reconstructs_without_fatal_violations` **skips here**
and runs where the data is.

It asserts: >100k records, actions ⊆ `ACMRTFN`, exactly one instrument and one
publisher, sequences and timestamps advancing, zero sequence/timestamp
regressions, zero crossed books, zero oversized cancels, `clean: true`, and a
final book that is not crossed.

To run it:

```bash
KEFTRADE_MBO_TEST_FILE=/path/to/xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst \
  python -m pytest tests/test_mbo_book_validator.py -q
```

And to produce the report itself:

```bash
python -m app.cli.mbo_validate file --path /path/to/xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst --summary-only
```

which writes the full JSON to
`reports/tier1_mbo_validation/<file>.validation.json`.

The CLI refuses before replaying if the local constants disagree with the
installed `databento` package, so a version drift cannot silently produce a
report against different semantics.

## Sample output shape

From a synthetic session with a snapshot preamble, a fill/cancel pair and a
modify:

```json
{
  "replay":  {"records": 8, "by_action": {"A": 4, "C": 1, "F": 1, "M": 1, "R": 1}},
  "snapshot":{"snapshot_records": 3, "snapshot_clears": 1, "snapshot_adds": 2,
              "snapshot_present": true},
  "flags":   {"f_last_records": 6, "book_states_checked": 6},
  "final_book": {
    "best_bid": {"price_display": 34.00, "size": 300, "count": 1},
    "best_ask": {"price_display": 34.01, "size": 250, "count": 1},
    "spread_display": 0.01,
    "resting_orders": 4, "bid_levels": 2, "ask_levels": 2,
    "bid_size": 1100, "ask_size": 400
  },
  "integrity": {"clean": true, "fatal_violation_counts": {},
                "crossed_book_events": 0, "locked_book_events": 0}
}
```

## What was added

| File | Purpose |
|---|---|
| `app/services/mbo_book_validator.py` | Replay core, book, violation vocabulary, report builder |
| `app/cli/mbo_validate.py` | `file` and `constants` commands |
| `tests/test_mbo_book_validator.py` | 34 synthetic tests + 1 real-file integration check |
| `pyproject.toml` | `databento>=0.83.0`, `sortedcontainers>=2.4.0` |

`sortedcontainers` is the same structure Databento's reference book uses; best
bid/ask must be O(1) because they are queried at every `F_LAST`, which on a
liquid session is most records.

## Limitations, stated plainly

- **The real file has not been replayed.** Everything above is synthetic-semantic validation plus a pinned adapter. The integration assertions are written but unexecuted.
- **One symbol, one session, one venue** even once it runs. A clean CMCSA 2025-06-26 says nothing about the other 159 files, and the validator should be run across them before any of them is trusted.
- **Correct-by-construction is not correct-by-comparison.** This checks internal consistency. It does not cross-check the reconstructed BBO against Databento's own MBP-1/TBBO for the same session, which is the stronger test and the obvious next step if you want one.
- **No performance work.** The replay is a straight Python loop; a multi-million-record session will take minutes, not seconds.

## Stopping here

Per instruction: validation only. No features, no forward returns, no
prediction tests, no Alpha Map cells, no strategies, no thresholds, and no
further downloads.

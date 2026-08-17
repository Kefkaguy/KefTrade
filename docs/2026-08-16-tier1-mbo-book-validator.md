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

### Certification is stricter than `clean`

`F_MAYBE_BAD_BOOK` is Databento's marker for an **unrecoverable channel gap**:
the reconstruction may be missing updates it will never receive. Internal
consistency measured *after* such a gap says nothing about what went missing, so
a single occurrence withdraws certification on its own:

```json
"integrity": {
  "clean": false,
  "certified": false,
  "uncertified_reason": "F_MAYBE_BAD_BOOK: Databento reported an unrecoverable
                         channel gap on 1 record(s); the book may be missing
                         updates that will never arrive"
}
```

The replay still finishes — the gap is a verdict, not a crash — so the rest of
the session stays readable. `flags.f_maybe_bad_book_records` carries the count.

`F_BAD_TS_RECV` is counted **split by snapshot versus live**:

```json
"f_bad_ts_recv_records": 3,
"f_bad_ts_recv_snapshot_records": 2,
"f_bad_ts_recv_live_records": 1
```

A snapshot-only occurrence says nothing about the live stream. Live occurrences
are what later latency and timestamp-quality work needs, so they are reported
explicitly and still do not fail certification — a bad receive clock is not a
bad book.

**One deliberate divergence from Databento's reference.** Their implementation
`assert`s on these conditions. This one **records and continues**: an assertion
turns the first anomaly into a crash and hides every later one, which is the
opposite of what a validator is for. Where recovery is defined, it follows the
reference (an unknown `M` is treated as an add); where it is not, it clamps
rather than invents (a cancel exceeding resting size clamps at zero, because a
negative resting size is not a market state).

## Fidelity audit (second pass)

Four changes and two confirmations.

**Sequence ties are permitted — confirmed, not changed.** The check was already
`event.sequence < last_live_sequence`, strictly less-than, so records normalized
from the same TotalView message sharing one sequence pass cleanly. Only true
backward movement counts. This was previously implicit in an operator; it is now
pinned by `test_equal_sequence_numbers_within_one_native_event_are_not_regressions`
(three records at sequence 7 → zero violations) and its negative counterpart at
sequence 7 → 6 → one violation. Same for `ts_event`.

**Violation counts were capped — a real bug the audit found.** Counts were
derived from the retained sample list, which is bounded at 200. A session with
40,000 crossed books would have reported 200 and read as untidy rather than
broken. Counts now come from an uncapped `Counter`; only the sample is bounded,
and `integrity.sample_truncated` says when it is. This mattered directly for the
"any occurrence must be counted" requirement on `F_MAYBE_BAD_BOOK`.

**Every action is reported, including absent ones.** `replay.by_action` now
carries all seven of `A/C/M/R/T/F/N` with explicit zeros, alongside
`by_action_observed` for what actually occurred. Nasdaq TotalView normalizes an
order replace as `C(old order_id)` + `A(new order_id)`, so **few or no `M`
records on the real file would be expected rather than evidence of a parsing
bug** — and a zero has to be visible to support that reading. Generic `M`
support is unchanged and still fully tested;
`test_replace_normalized_as_cancel_plus_add_reconstructs_correctly` covers the
XNAS replace shape end to end without any `M` record at all.

**`F_MAYBE_BAD_BOOK` and `F_BAD_TS_RECV`** are handled as described above.

## Test results

```
68 passed, 1 skipped
```

The skip is the real-file integration check (see below). Coverage:

- **Constants** — all 19 mirrored constants pinned against the installed `databento_dbn`, so a package upgrade that moves a flag value fails the suite rather than silently changing results.
- **Actions** — add/level aggregation, partial and full cancel, modify as absolute size, all three priority cases, clear.
- **T/F/N inertness** and the accompanying-cancel rule.
- **Every violation kind** listed above, each asserted to be both *detected* and *recovered from* without corrupting the book.
- **Certification** — `F_MAYBE_BAD_BOOK` withdraws it; a clean session keeps it; `F_BAD_TS_RECV` does not affect it.
- **Uncapped counts** — 50 violations with a sample limit of 5 reports 50 and retains 5.
- **Sequence ties** — equal sequences within one native event pass; a strictly backward sequence does not.
- **Action coverage** — all seven actions present in the report, absent ones as explicit zeros.
- **`F_LAST` boundary rule**, demonstrated by A/B comparison rather than assertion.
- **Snapshot** preamble accounting and monotonicity exemption.
- **Top-of-book** normalization and `UNDEF_PRICE` side-clear.
- **Adapter** — `MboEvent.from_dbn` exercised against genuine `databento_dbn.MBOMsg` objects, so field names and enum-to-string conversion are pinned, not assumed.
- **File reader** — `iter_dbn_events` filters non-MBO records (metadata, symbol mappings) that would otherwise raise mid-replay.

## All-session fidelity gate

The per-file validator answers "is this session reconstructible". The gate
answers the only question that matters before any measurement:

> Can all 160 frozen Tier-1 XNAS MBO symbol-days be reconstructed from a **known
> starting state** without structural data or book failures?

### Initialization is now explicit

A reconstruction means nothing unless the state it started from is known. Three
fixed modes:

| Mode | Meaning | Certified |
|---|---|---|
| `formal_snapshot` | Databento marked the opening records `F_SNAPSHOT`; the book was rebuilt from published state | yes |
| `known_empty_clear` | The very first record is an `R` clear with no snapshot flag; the book provably started empty | yes |
| `unknown` | Neither — the replay began mid-book against state we never saw | **no** |

`known_empty_clear` is what the real XNAS files do. CMCSA 2025-06-26 opens with
`index=0 sequence=0 action=R side=N order_id=0 flags=8` (`F_BAD_TS_RECV`, not
`F_SNAPSHOT`).

**That clear is not retroactively called a snapshot, and the distinction is
load-bearing.** A snapshot means *we were given the state*; a sequence-0 clear
means *there was no state to give*. Both are certifiable starts, and they are
different guarantees. `snapshot_present` stays `false` for a known-empty file,
and a test asserts it.

The report carries `initialization.mode`, `.certified`, `.first_action`,
`.first_sequence`, `.first_flags` and `.records_before_initialization`. An
`unknown` initialization makes the file uncertified **and** not clean.

An `R` arriving after record 0 is *not* a known-empty start — it may be clearing
state we never saw — so it reads `unknown`. `records_before_initialization`
makes such a near-miss visible rather than something to infer.

### The batch command

```bash
python -m app.cli.mbo_validate --output-dir reports/tier1_mbo_validation \
  batch --directory /path/to/dbn --expected-files 160
```

Discovers `*.dbn.zst`, processes sequentially, reuses the per-file replay core
unchanged, streams each file (nothing is expanded to disk), and drops each book
before opening the next. It writes per-file JSON, one aggregate JSON, and a CSV
matrix — row by row via `csv`, never through pandas, because loading 160 reports
into a dataframe would defeat the streaming.

**A failing file does not stop the walk.** It is recorded with its error, still
occupies a matrix row so the denominator stays honest, and the batch continues.
The complete report is written *before* the process exits non-zero (code 3), so
one run surfaces every failing session instead of one restart per failure.

### The gate

`overall_certified` is a conjunction — every clause must hold:

| Check | Requirement |
|---|---|
| `expected_file_count_met` | exactly 160 files completed |
| `no_unreadable_files` | zero unreadable |
| `all_initializations_certified` | every file has a certified initialization |
| `all_integrity_certified` | every file has `integrity.certified = true` |
| `all_clean` | every file has `clean = true` |
| `no_maybe_bad_book` | zero `F_MAYBE_BAD_BOOK` |
| `no_fatal_violations` | zero fatal structural violations |

159 clean sessions out of 160 is not "99% certified" — it is uncertified with one
session to look at. `F_BAD_TS_RECV` is totalled and reported, and never gates.

Matrix columns: source, symbol, date, records, book_states_checked,
initialization mode and certification, certified, clean,
`f_maybe_bad_book_records`, `f_bad_ts_recv_live_records`, crossed and locked
book events, sequence and ts_event regressions, all six unknown-order/structural
violation counts, all seven `A/C/M/R/T/F/N` action counts, and `read_error`.

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
| `app/services/mbo_batch_validator.py` | Directory walk, matrix, aggregate gate |
| `tests/test_mbo_book_validator.py` | 48 per-file tests + 1 real-file integration check |
| `tests/test_mbo_batch_validator.py` | 20 batch/gate tests |
| `pyproject.toml` | `databento>=0.83.0`, `sortedcontainers>=2.4.0` |

`sortedcontainers` is the same structure Databento's reference book uses; best
bid/ask must be O(1) because they are queried at every `F_LAST`, which on a
liquid session is most records.

## Limitations, stated plainly

- **The 160-file gate has not been run.** The batch command is written and tested against synthetic streams; it has not walked the real directory. Until it does, "all 160 reconstruct" is untested.
- **CMCSA 2025-06-26 alone says nothing about the other 159.** One clean session is one session.
- **Correct-by-construction is not correct-by-comparison.** This checks internal consistency. It does not cross-check the reconstructed BBO against Databento's own MBP-1/TBBO for the same session, which is the stronger test and the obvious next step if you want one.
- **No performance work.** The replay is a straight Python loop; a multi-million-record session will take minutes, not seconds.

## Stopping here

Per instruction: validation only. No features, no forward returns, no
prediction tests, no Alpha Map cells, no strategies, no thresholds, and no
further downloads.

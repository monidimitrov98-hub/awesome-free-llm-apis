# Sprint 13 P1.1: Final out-of-sample holdout protection

Research-engine work only. No strategy was developed, optimized or backtested on real data, and no reserved data was
read or revealed. No dataset, manifest, dataset-registry entry or raw market file changed. Risk, paper, marketdata,
the NinjaTrader bridge and strategy logic are untouched. No order path exists or was added.

This version includes the corrections from the final P1.1 audit (C1, H1, H2, M1, M2, L1–L7, §9).

## 1. Policy (`config/research/HOLDOUT.toml`, Git-tracked, version 2)

| Field | Value |
|---|---|
| Reservation | `mes-mnq-2026` |
| Instruments | MES, MNQ, whether or not the data is marked SYNTHETIC |
| Reserved | from **2026-01-01**, open-ended |
| Embargo | **10 trading dates** before the start: research is blocked from **2025-12-17** |
| Access | `final_oos_only` |

**Embargo counting.** Weekends **and every rule-based US holiday candidate** count as closed. No CME holiday is
officially verified in this project. Treating a candidate as closed can only move the embargo earlier. The dates are:

> 2025-12-31, 12-30, 12-29, 12-26, 12-24, 12-23, 12-22, 12-19, 12-18, 12-17

**Reserved bars.** A bar is reserved when its **Globex trade date** (CME calendar, DST-aware) falls in the blocked
window. Bars outside the exchange schedule are judged conservatively.

**Policy sources.** The canonical file is always required. `FTS_HOLDOUT_EXTRA` can only **add** reservations. A
missing, malformed or empty policy refuses everything.

## 2. Three kinds of access

| Kind | Rule |
|---|---|
| **Research** (training, validation, sweeps, walk-forward, CV, regime fitting, backtests) | Never computes on a reserved bar (§4) |
| **Final OOS evaluation** | Once, through a holdout claim (§3) |
| **Data quality** (`run_data.py`, validation, remediation, conflicts, readiness, the `run_pipeline.py` indicator CLI) | Allowed: no strategy outcome is computed |

## 3. The final evaluation: data-access order

`run_research.py reveal-oos --plan P` → `reveal_oos` → `_reveal_holdout`:

| Step | What happens | What is read |
|---|---|---|
| 1 | Version resolved, code trust checked | Strategy configuration, Git status |
| 2 | Dataset decision | `manifest.json` and the dataset registry **only** (`DatasetStore.load_manifest`: self-hash and registry match; no data file is opened, stat'ed or hashed) |
| 3 | Canonical validation ledger checked; its identity bound in the anchor; earlier reveals checked | Ledger records |
| 4 | `claim_final_oos` | Policy, plan file and registration (canonical plans directory only), dataset manifest. Refused while claim-ledger records are uncommitted. |
| 5 | **PREPARED → CLAIMED** under the cross-process lock | Ledgers only |
| 6 | **CONSUMED** written to the anchor, then to the claim ledger (fsync'ed records, write-through head) | Ledgers only. The policy is re-verified under the same lock. |
| 7 | **The first data read:** `env.market_data(csv, final_access=token)` → `store.load` hashes every file → `bars()` | The bound dataset, after CONSUMED is durable |
| 8 | `run_research` checks the binding against the runtime values; `run_spec` checks the token against the **actual** `MarketData.dataset_identity`; the engine runs | — |
| 9 | Validation-ledger record → run folder → frozen Monte Carlo (once) → **COMPLETED** | Revealed trades only |

**Failures:**

| When | Outcome |
|---|---|
| Before step 6 | ABORTED (`accessed: false`). A test spies on every data-file hash and read and proves that none happened. |
| From step 6 on (data read or not) | **CONSUMED for good.** |
| A crash | The last durable state stands. |

## 4. Research never sees reserved bars

| Input | Rule | Where |
|---|---|---|
| A dataset whose **manifest** reaches the window | Refused before **any** data byte is read or hashed | `ResearchEnv.market_data` → `check_dataset_meta` |
| A dataset before the window | Loaded and verified; checked again for reserved bars before any computation | `market_data_from_dataset(holdout_guard=True)` |
| A CSV file | Read (a data-quality read), then refused **before validation, resampling or any indicator** if it holds a reserved bar | `build_market_data(holdout_guard=True)`, used by `run_research --csv`, `run_backtest.py`, `--replay`, `run_regime_validation.py` |
| Market data handed directly to the engine | Refused **before `spec.prepare`** if it holds a reserved bar | `run_spec` |

A dataset that straddles the boundary, such as a Z25 or H26 contract, can't be used for research. It would first
have to be split into a pre-window dataset with data-quality tooling, which is not part of P1.1.

**Holdout plans** name a research dataset (`use = "research"`), which must end before the window, and an OOS dataset
(`use = "oos"`). Plan checks read manifests only.

## 5. Claim ledger, anchor and Git rollback (`src/research/holdout_ledger.py`)

```
validation_history/holdout_anchor.jsonl (+ .head.json)   git-IGNORED, written FIRST, a full copy of every transition
config/research/holdout_ledger.jsonl    (+ .head.json)   Git-tracked, each record names its anchor record;
                                                         its genesis names the anchor's genesis
```

**Every read** verifies:
- both hash chains and both heads;
- the claim lifecycle;
- that the claim ledger equals the anchor's mirrors, record for record;
- that both ledgers belong together (genesis binding).

**Fail-closed cases:**

| Situation | Result |
|---|---|
| Claim ledger **behind** the anchor (a `git checkout`, `restore`, `stash` or `reset`, or an interrupted append) | Refused |
| Anchor behind, missing, or **recreated** (new genesis) | Refused |
| Edited, removed, reordered or truncated records | Refused |
| Head mismatch | Refused |
| Impossible transitions | Refused |

**Owner fields are committed.** The `claim_prepared` and `claim_consumed` records store an `owner` field with the
process ID (`pid`) and the machine's hostname (`host`), from `owner_info()`. It is an audit trail of which process
and machine made a claim; recovery does not rely on it (it checks the claim lock). The anchor mirrors these records
and the Git-tracked claim ledger keeps them too, so they are committed with every claim. Anyone with access to the
repository, or to any fork or clone of it, can see the hostname. The records are hash-chained, so they cannot be
redacted later without breaking the chain. If a hostname must not be disclosed, do not make claims on that machine.

**No new claim while the claim ledger has uncommitted changes.** `git status` / `git ls-files` must be clean for
both claim-ledger files, so every claim is committed before the next one starts.

**Tests on a real temporary Git repository:**
- after a claim is CONSUMED, each of `git checkout -- .`, `git restore .`, `git stash` and `git reset --hard` rolls
  the tracked file back;
- every holdout operation then fails closed;
- `holdout-repair` replays the anchor;
- the consumed claim is back and a second evaluation stays refused.

**Limits.** The anchor is outside Git, so `git clean -x`, deleting `validation_history/` or a fresh clone removes
it. Every holdout operation then refuses until it is restored from backup. Because the claim ledger names the
anchor's genesis, a recreated anchor is detected. Deleting or rewriting **both** ledgers consistently is not
detectable from the files alone.

**Back up `validation_history/`.**

## 6. Locking, durability and recovery

**Locking** (`src/backtest/filelock.py`):
- OS-level exclusive locks: `msvcrt` byte lock on Windows, `fcntl.flock` on POSIX;
- released by the OS when a process dies;
- re-entrant within a thread, with a normalised path identity;
- nothing is ever written into a lock file;
- order is always the ledger lock, then a claim lock, which is a new id or a non-blocking try during recovery.

**Durability:** record appends are fsync'ed. Heads are replaced with `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`
on Windows, retried on a transient sharing violation, and with `os.replace` plus a directory fsync on POSIX. New
ledger files get a directory fsync.

**Human procedures** (`run_research.py`). None of them can reset a ledger or release a CONSUMED claim:

| Command | Purpose |
|---|---|
| `holdout-status` | Verify both ledgers and list the claims |
| `holdout-recover --claim ID --by --reason` | ABORTED only for a PREPARED/CLAIMED claim whose owner provably no longer holds its claim lock |
| `holdout-repair --by --reason` (M1) | **Extend-only.** A partial last line is moved to a `.partial-*` side file (preserved). A lagging head is moved forward to a fully verified chain. Missing anchor mirrors are replayed into the claim ledger. A head **ahead** of the records, a non-prefix head, or a ledger ahead of its anchor is refused ("restore from backup"). The repair is noted in the anchor. Also moves the canonical validation ledger's head forward the same way. |
| `holdout-complete --claim ID --run FOLDER --by --reason` (M2) | Completes a CONSUMED claim whose owner died **after** its result was recorded. It verifies the run folder, its validation-ledger record and the claim binding, produces the plan-frozen Monte Carlo from the **recorded** trades if it is missing, and writes COMPLETED. A claim without a recorded run stays CONSUMED for good. There is never a second evaluation. |

**Validation ledger** (L6): appends are locked, re-verified inside the lock and flushed to disk.

- **When this machine has the holdout anchor**, it is authoritative: the canonical ledger's genesis is recorded in
  it, and `--init-ledger` after the ledger was deleted is refused ("restore from backup").
- **When no anchor exists on the machine** (a fresh project copy or clone), a first canonical ledger can be created.
  Nothing can tell whether one existed before. Every holdout operation is still refused there, because the claim
  ledger names an anchor that is missing.
- A holdout evaluation binds or checks the ledger's identity in the anchor.
- So erasing the validation ledger's history needs **both** `validation_history/` files deleted, which also disables
  every holdout operation.

## 7. Tokens, Monte Carlo and governance

**Tokens:**
- issued only inside a claim, single-use, and bound to:
  - plan and plan hash;
  - dataset reference, manifest, bars-content and data hashes;
  - strategy, version, version hash and parameter hash;
  - OOS fingerprint;
  - frozen evaluation configuration;
  - policy hash;
  - the exact segment;
- checked against runtime objects in `run_research` (resolved version and parameters, the loaded dataset hash, the
  computed segment) and in `run_spec` (the actual `MarketData.dataset_identity`, root and period) (L2);
- a hand-made token is refused.

**Monte Carlo:** the plan-frozen Monte Carlo runs **once** per claim (L5), only with the CONSUMED access of that run's
claim, or inside `holdout-complete`. Its report hash goes into COMPLETED. Standalone Monte Carlo of a holdout run is
refused by the CLI and the library.

**Governance:**
- OOS_VALIDATED needs the canonical validation ledger and a COMPLETED claim with the same results and manifest.
- SHADOW_APPROVED accepts only the Monte Carlo report recorded in COMPLETED.

## 8. Alternate paths (L4)

| Path | Rule |
|---|---|
| Plans directory | A holdout claim reads its plan from the **canonical** plans directory only; `FTS_PLANS_DIR` elsewhere is refused |
| `--ledger` / `FTS_LEDGER_PATH` | Refused for holdout evaluations |
| Experiment registry (`FTS_EXPERIMENTS_PATH`) | Irrelevant: research on reserved dates is refused before any attempt |
| `FTS_HOLDOUT_EXTRA` | Only adds reservations |

## 9. Remaining limits

- **Paper / shadow replay** (`run_paper.py`, `src/terminal/modes.py`) and the **indicator-pipeline CLI**
  (`run_pipeline.py`) do not use the guard. Paper and shadow were out of scope; the pipeline CLI is a data-processing
  tool. Procedural rule: don't replay 2026 data through the paper runtime.
- **A CSV file is always read** before it can be refused. Its timestamps are needed; this is a data-quality read, and
  no computation follows.
- **This guards against accidental access** through the project's entry points. It is not a defence against
  arbitrary Python code: direct `BacktestEngine` use, private helpers, monkeypatching, code edits or re-dated data.
- **POSIX locking:** the `fcntl` path is implemented but **not executed here** (Windows only). Its test is skipped on
  Windows.
- **Claim records disclose the PID and hostname** in the Git-tracked claim ledger (§5).
- **The anchor is machine-local.** Another machine or a fresh clone refuses every holdout operation until
  `validation_history/` is restored. A first canonical validation ledger can still be created there for non-holdout
  work (§6).

## 10. Tests

`tests/test_sprint13_p1_1_holdout.py`: 27 tests, of which 26 run on Windows and 1 POSIX-only test is skipped.
`tests/conftest.py` redirects the claim ledger, the anchor and their locks to a temporary directory for every test,
so the real ledgers are never touched.

| # | Requirement | Test |
|---|---|---|
| 1 | No reserved bar byte read before durable CONSUMED | `test_the_final_evaluation_reads_no_byte_before_consumed_and_runs_once`: spies on every dataset-file hash and bar read; all happen with the claim already CONSUMED |
| 2 | A crash before access aborts | `test_failures_before_consumed_abort_and_after_consumed_never_release`: ABORTED with **zero** data reads |
| 3 | A crash after possible access never releases | Same test: a crash while reading leaves the claim CONSUMED, and re-reveal and recovery are refused |
| 4 | Git restore, reset and stash can't erase claims | `test_git_rollback_cannot_erase_claim_history` (checkout, restore, stash, reset on a real repository) |
| 5 | Training never reads or processes reserved bars | `test_research_on_a_dataset_reaching_the_window_reads_no_byte`, `test_csv_inputs_with_reserved_bars_are_refused_before_any_computation`, `test_the_engine_refuses_reserved_bars_before_prepare` |
| 6 | No indicator sees reserved bars | `test_allowed_research_never_computes_on_reserved_bars` |
| 7 | Concurrent claims are safe | `test_concurrent_processes_cannot_both_claim_overlapping_dates` (5 races of 4 processes), `test_crash_recovery_releases_only_provably_unaccessed_claims` |
| 8 | Damaged, missing, rolled-back or inconsistent ledgers fail closed | `test_damaged_missing_or_inconsistent_ledgers_fail_closed`, `test_interrupted_writes_are_repaired_extend_only` |
| 9 | Tokens can't be reused or transferred | `test_tokens_are_bound_single_use_and_cannot_be_forged` (also L2, L3, L5) |
| 10 | Monte Carlo and governance bound to one evaluation | The final-evaluation test, `test_a_consumed_claim_completes_only_from_its_recorded_run` (M2) |
| — | L4 and L6 | `test_alternate_ledgers_and_plans_cannot_reach_the_holdout` |
| — | L7 | `test_posix_locks_exclude_other_processes` (POSIX only; skipped here) |

`tests/test_sprint11_research.py::test_every_attempt_is_recorded_including_failures`: its empty period moved from
2030 to 2024-01. The purpose and assertions are unchanged.

"""Final out-of-sample HOLDOUT protection (Sprint 13 P1.1).

``config/research/HOLDOUT.toml`` (Git-tracked, versioned) reserves market data for ONE final out-of-sample
evaluation. For each reserved instrument the BLOCKED WINDOW runs from the embargo start to the reservation end
(open-ended when empty). Three kinds of access are distinguished:

1. Training / validation / development access: REFUSED whenever the period touches the blocked window
   (research runs of every purpose except the OOS reveal, sweeps, walk-forward, purged CV, plans whose
   train / validation segments touch it, standalone Monte Carlo and comparisons of runs that touch it, every
   ``run_backtest.py`` mode, regime-classifier fitting). Refusals happen before an experiment attempt is
   recorded, so they never count in the multiple-testing family. Reserved bars never enter research
   computation: a dataset whose MANIFEST reaches a blocked window is refused before any of its bytes is read
   (:func:`check_dataset_meta`); a CSV file is read (a data-quality read) and refused before validation,
   resampling or any indicator if it holds a reserved bar (:func:`refuse_reserved_bars`, pipeline
   ``holdout_guard``); ``run_spec`` refuses market data holding a reserved bar BEFORE ``spec.prepare``.
2. The preregistered final OOS evaluation: ``reveal_oos`` with a REGISTERED plan (canonical plans directory)
   whose OOS segment lies entirely inside one reservation. It runs through ONE claim
   (:mod:`src.research.holdout_ledger`): PREPARED -> CLAIMED -> CONSUMED -> COMPLETED (or ABORTED). Everything
   before CONSUMED uses metadata only (policy, plan, registry, manifest.json); CONSUMED is durable BEFORE the
   first byte of the dataset is read. The single-use :class:`FinalOOSAccess` is issued only inside the claim and
   is bound to the plan, the dataset identity and hashes, the strategy version and parameters, the policy hash,
   the exact segment and the frozen evaluation configuration; ``run_spec`` checks it against the ACTUAL market
   data (dataset identity carried by ``MarketData``).
3. Data-quality work (ingestion, validation, remediation, conflict analysis, readiness, the indicator
   pipeline CLI): not guarded, because it computes no strategy outcome.

Embargo: the blocked window starts ``embargo_trade_dates`` TRADING dates before the reservation start. Trading
dates are counted conservatively: weekends AND every rule-based US holiday candidate count as closed, whether or
not CME has been verified (src.histdata.calendar.us_holiday_candidates; the calendar holds no verified holidays).
Treating a candidate as closed can only move the embargo start EARLIER, never later. For 2026-01-01 and 10 dates:
Dec 31, 30, 29, 26, 24 (expected early close, still a trading date), 23, 22, 19, 18, 17 -> 2025-12-17.

Policy sources: the canonical file is always loaded and must be valid. ``FTS_HOLDOUT_EXTRA`` may name ONE more
policy file whose reservations are ADDED (it can never remove, replace or narrow a canonical reservation; a
broken extra file fails closed like a broken canonical one). There is no other override.

The rule applies to the reserved roots whatever the SYNTHETIC marker says. :func:`check_simulation` is called by
``src/backtest/runner.run_spec``, the single path from any caller to the backtest engine. Enforcement is
fail-closed: a missing, unreadable or malformed policy, a policy without reservations, or a damaged claim ledger
refuses every guarded operation.

This is a guard against ACCIDENTAL access through the project's entry points (CLI and library calls that go
through ``run_spec``). It is not a security boundary against someone who edits the code, drives
``BacktestEngine`` directly, re-dates the data, or calls private helpers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.backtest import filelock
from src.backtest.manifest import file_sha256
from src.config import PROJECT_ROOT
from src.research import holdout_ledger as HL
from src.research.holdout_ledger import HoldoutError

DEFAULT_FILE = PROJECT_ROOT / "config" / "research" / "HOLDOUT.toml"
EXTRA_ENV = "FTS_HOLDOUT_EXTRA"
ACCESS_MODES = ("final_oos_only",)
_ROOT = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")


def _day(x: Any) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is not None:
        t = t.tz_localize(None)
    return t.normalize()


def _closed_candidates(start: pd.Timestamp) -> list:
    from src.histdata.calendar import us_holiday_candidates

    out = []
    for y in range(start.year - 2, start.year + 1):
        out += list(us_holiday_candidates(y))
    return sorted(out)


@dataclass(frozen=True)
class Reservation:
    id: str
    instruments: tuple[str, ...]
    start: pd.Timestamp
    end: pd.Timestamp | None
    embargo_trade_dates: int
    access: str
    source: str = "canonical"

    @property
    def blocked_start(self) -> pd.Timestamp:
        """First date of the blocked window: ``embargo_trade_dates`` trading dates before ``start``, counting
        weekends and every holiday candidate as closed (conservative)."""
        if self.embargo_trade_dates == 0:
            return self.start
        d = np.busday_offset(self.start.date(), -self.embargo_trade_dates, roll="forward",
                             holidays=_closed_candidates(self.start))
        return pd.Timestamp(d)

    def embargo_dates(self) -> list[str]:
        """The trading dates the embargo covers (most recent first), for audit and display."""
        days = pd.bdate_range(self.blocked_start, self.start - pd.Timedelta(days=1))
        closed = {pd.Timestamp(x) for x in _closed_candidates(self.start)}
        return [str(d.date()) for d in reversed(days) if d not in closed]

    def touches(self, a: Any, b: Any) -> bool:
        a, b = _day(a), _day(b)
        return a <= (self.end if self.end is not None else pd.Timestamp.max.normalize()) and b >= self.blocked_start

    def contains(self, a: Any, b: Any) -> bool:
        """The whole period lies inside the reservation proper (not in the embargo)."""
        a, b = _day(a), _day(b)
        return a >= self.start and (self.end is None or b <= self.end) and b >= a

    def describe(self) -> str:
        end = "open-ended" if self.end is None else str(self.end.date())
        return (f"holdout {self.id}: {', '.join(self.instruments)} from {self.start.date()} ({end}); training and "
                f"validation are also blocked during the {self.embargo_trade_dates}-trading-date embargo from "
                f"{self.blocked_start.date()}")


@dataclass(frozen=True)
class Policy:
    id: str
    version: int
    sha256: str
    path: Path
    reservations: tuple[Reservation, ...]
    sources: tuple[str, ...]

    def reservations_for(self, root: str) -> list[Reservation]:
        r0 = root.split(":")[0]
        return [r for r in self.reservations if r0 in r.instruments]

    def reservation(self, root: str) -> Reservation | None:
        """The first (canonical) reservation of ``root``."""
        rs = self.reservations_for(root)
        return rs[0] if rs else None

    def touching(self, root: str, a: Any, b: Any) -> list[Reservation]:
        return [r for r in self.reservations_for(root) if r.touches(a, b)]

    def identity(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "sha256": self.sha256, "sources": list(self.sources),
                "reservations": [r.id for r in self.reservations]}


def _parse(p: Path, source: str) -> tuple[dict[str, Any], list[Reservation]]:
    if not p.is_file():
        raise HoldoutError(f"holdout policy {p} is missing: research and backtests are refused until it is restored "
                           "(it is Git-tracked)")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise HoldoutError(f"holdout policy {p} is unreadable: {exc}") from exc
    if set(raw) - {"policy", "reservations"}:
        raise HoldoutError(f"holdout policy {p.name}: unknown section(s) {sorted(set(raw) - {'policy', 'reservations'})}")
    pol = raw.get("policy") or {}
    allowed_pol = {"id", "version", "approved_on", "approval"}
    if set(pol) - allowed_pol:
        raise HoldoutError(f"holdout policy {p.name}: unknown [policy] key(s) {sorted(set(pol) - allowed_pol)}")
    ver = pol.get("version")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1 or not str(pol.get("id", "")).strip():
        raise HoldoutError(f"holdout policy {p.name}: [policy] needs an id and an integer version >= 1")
    rows = raw.get("reservations")
    if not isinstance(rows, list) or not rows:
        raise HoldoutError(f"holdout policy {p.name} lists no reservation: refused (fail-closed). Retiring a holdout is "
                           "a code-reviewed decision, not an edit of this file")
    out, seen = [], set()
    allowed = {"id", "instruments", "start", "end", "embargo_trade_dates", "access"}
    for r in rows:
        if not isinstance(r, dict) or set(r) - allowed or {"id", "instruments", "start"} - set(r):
            raise HoldoutError(f"holdout policy {p.name}: malformed reservation {r!r}")
        inst = r["instruments"]
        if not isinstance(inst, list) or not inst or not all(isinstance(i, str) and _ROOT.match(i) for i in inst):
            raise HoldoutError(f"holdout policy {p.name}: reservation {r.get('id')}: instruments must be contract roots")
        if seen & set(inst):
            raise HoldoutError(f"holdout policy {p.name}: {sorted(seen & set(inst))} reserved twice")
        seen |= set(inst)
        try:
            start = _day(str(r["start"]))
            end = _day(str(r["end"])) if str(r.get("end", "")).strip() else None
        except ValueError as exc:
            raise HoldoutError(f"holdout policy {p.name}: reservation {r.get('id')}: invalid date ({exc})") from exc
        if end is not None and end < start:
            raise HoldoutError(f"holdout policy {p.name}: reservation {r.get('id')} ends before it starts")
        emb = r.get("embargo_trade_dates", 0)
        if not isinstance(emb, int) or isinstance(emb, bool) or emb < 0:
            raise HoldoutError(f"holdout policy {p.name}: reservation {r.get('id')}: embargo_trade_dates must be >= 0")
        access = r.get("access", "final_oos_only")
        if access not in ACCESS_MODES:
            raise HoldoutError(f"holdout policy {p.name}: reservation {r.get('id')}: access must be one of {ACCESS_MODES}")
        out.append(Reservation(str(r["id"]), tuple(inst), start, end, emb, access, source))
    return pol, out


def load_policy() -> Policy:
    """The canonical policy plus any ADDED reservations from ``FTS_HOLDOUT_EXTRA``. Any problem raises
    :class:`HoldoutError` (fail-closed)."""
    pol, res = _parse(DEFAULT_FILE, "canonical")
    shas, sources = [file_sha256(DEFAULT_FILE)], [str(DEFAULT_FILE)]
    extra = os.environ.get(EXTRA_ENV, "").strip()
    if extra:
        _, more = _parse(Path(extra), "extra")
        ids = {r.id for r in res}
        if ids & {r.id for r in more}:
            raise HoldoutError(f"{EXTRA_ENV}: reservation ids {sorted(ids & {r.id for r in more})} already exist in the "
                               "canonical policy (an extra policy can only ADD reservations)")
        res = res + more
        shas.append(file_sha256(Path(extra)))
        sources.append(str(Path(extra).resolve()))
    sha = shas[0] if len(shas) == 1 else hashlib.sha256("+".join(shas).encode()).hexdigest()
    return Policy(str(pol["id"]), pol["version"], sha, DEFAULT_FILE, tuple(res), tuple(sources))


# ------------------------------------------------------------------------------------------- research access
def check_research(root: str, start: Any, end: Any, what: str) -> None:
    """Refuse training / validation / development access whose period touches a blocked window."""
    hit = load_policy().touching(root, start, end)
    if hit:
        raise HoldoutError(f"{what} on {root.split(':')[0]} {_day(start).date()}..{_day(end).date()} touches the "
                           f"{hit[0].describe()}. Reserved data is used only once, by the final OOS evaluation of a "
                           "registered plan (run_research.py reveal-oos --plan)")


def check_run_record(instrument: str, period: dict[str, Any], what: str) -> None:
    """Refuse further analysis (standalone Monte Carlo, comparison) of a recorded run touching a blocked window."""
    check_research(instrument, period.get("start"), period.get("end"), what)


def _final_reservation(pol: Policy, root: str, start: Any, end: Any) -> Reservation | None:
    """The one reservation that wholly contains a final OOS segment; None if the segment touches none."""
    hit = pol.touching(root, start, end)
    if not hit:
        return None
    inside = [r for r in hit if r.contains(start, end)]
    if len(inside) != 1 or len(hit) != 1:
        raise HoldoutError(f"the OOS segment {_day(start).date()}..{_day(end).date()} of {root.split(':')[0]} must lie "
                           f"entirely inside exactly one reservation and touch no other ({hit[0].describe()})")
    return inside[0]


def check_plan(instrument: str, train: list[str], validation: list[str], oos: list[str]) -> dict[str, Any] | None:
    """A plan's research segments must end before every blocked window; its OOS segment must lie either entirely
    before the blocked windows (an ordinary plan) or entirely inside one reservation (the final holdout plan)."""
    pol = load_policy()
    for name, seg in (("train", train), ("validation", validation)):
        hit = pol.touching(instrument, seg[0], seg[1])
        if hit:
            raise HoldoutError(f"plan segment {name} {seg[0]}..{seg[1]} of {instrument} touches the "
                               f"{hit[0].describe()}: training and validation must end before the blocked window")
    hit = pol.touching(instrument, oos[0], oos[1])
    if not hit:
        return None
    try:
        r = _final_reservation(pol, instrument, oos[0], oos[1])
    except HoldoutError:
        raise HoldoutError(f"plan OOS segment {oos[0]}..{oos[1]} of {instrument} overlaps the {hit[0].describe()} "
                           f"without lying entirely inside it (from {hit[0].start.date()}): refused") from None
    return {"reservation": r.id, "policy_sha256": pol.sha256}


# ------------------------------------------------------------------------------------------- final OOS claims
@dataclass(frozen=True)
class FinalOOSAccess:
    """Single-use access to one claimed segment. Valid only while registered in this process by
    :func:`claim_final_oos`; a hand-made instance is refused."""
    claim_id: str
    nonce: str
    binding: tuple[tuple[str, Any], ...]

    def get(self, key: str) -> Any:
        return dict(self.binding)[key]


_TOKENS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def eval_config_sha256(monte_carlo: dict[str, Any] | None) -> str:
    return hashlib.sha256(json.dumps({"monte_carlo": monte_carlo}, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _overlap(a0: Any, a1: Any, b0: Any, b1: Any) -> bool:
    return _day(a0) <= _day(b1) and _day(b0) <= _day(a1)


def claim_final_oos(*, app, store, plan: str | None, root: str, start: str, end: str, dataset_ref: str,
                    data_sha256: str, strategy: str, version: str, version_sha256: str, params_sha256: str,
                    fingerprint: str) -> FinalOOSAccess:
    """PREPARED -> CLAIMED for a final OOS evaluation, after verifying everything it binds - from METADATA only.

    Reads: the policy file, the plan file and its registration index, the dataset registry record and the dataset's
    manifest.json (``store.load_manifest``: manifest self-hash and registry match, no data file is opened or
    hashed). Refuses: no registered plan in the canonical plans directory; a segment different from the plan's frozen
    OOS segment; a segment not wholly inside exactly one reservation; a dataset other than the plan's OOS dataset (by
    reference and manifest hash); a strategy version the plan did not freeze; uncommitted claim-ledger records; an
    overlapping non-aborted claim."""
    from src.research.plans import DEFAULT_DIR, load_plan, plans_dir

    pol = load_policy()
    r = _final_reservation(pol, root, start, end)
    if r is None:
        raise HoldoutError(f"{start}..{end} is not reserved data: no holdout claim is needed")
    if not plan:
        raise HoldoutError(f"{start}..{end} is reserved ({r.describe()}): it is revealed only by the final OOS evaluation "
                           "of a REGISTERED plan (--plan), frozen before any result")
    if Path(plans_dir()).resolve() != Path(DEFAULT_DIR).resolve():
        raise HoldoutError("a holdout plan is read from the canonical plans directory only (FTS_PLANS_DIR is refused "
                           "for the final evaluation)")
    p = load_plan(plan, app, store)                       # registered and unmodified, or raises (metadata only)
    key = root
    mine = [d for d in p["datasets"] if d["key"] == key and d["use"] in ("oos", "all")]
    if not mine or mine[0]["ref"] != dataset_ref:
        raise HoldoutError(f"plan {plan} does not freeze dataset {dataset_ref} for the OOS segment of {root}")
    if [str(_day(start).date()), str(_day(end).date())] != p["oos"]:
        raise HoldoutError(f"plan {plan} freezes the OOS segment {p['oos'][0]}..{p['oos'][1]}, not {start}..{end}")
    if f"{strategy}@{version}" not in p["strategies"]:
        raise HoldoutError(f"{strategy}@{version} is not frozen by plan {plan}")
    if key not in p.get("holdout", {}):
        raise HoldoutError(f"plan {plan} was not registered as a holdout plan for {root}")
    ds = store.load_manifest(dataset_ref)                 # manifest + registry only: no data byte is read
    if ds.manifest["manifest_sha256"] != mine[0]["manifest_sha256"] or ds.manifest["manifest_sha256"] != data_sha256:
        raise HoldoutError(f"dataset {dataset_ref} does not match the manifest hash frozen by plan {plan}")
    mc = (p.get("final_evaluation") or {}).get("monte_carlo")
    binding = {"root": root.split(":")[0], "reservation": r.id, "start": str(_day(start).date()),
               "end": str(_day(end).date()), "plan": plan, "plan_sha256": p["sha256"], "dataset_ref": dataset_ref,
               "dataset_manifest_sha256": ds.manifest["manifest_sha256"],
               "bars_content_sha256": ds.manifest["bars_content_sha256"], "data_sha256": data_sha256,
               "strategy": strategy, "version": version, "version_sha256": version_sha256,
               "params_sha256": params_sha256, "fingerprint": fingerprint, "eval_config_sha256": eval_config_sha256(mc),
               "policy_sha256": pol.sha256}
    cid, owner = _claim_segment(binding, mc)
    return _issue_token(cid, binding, owner)


def _claim_segment(binding: dict[str, Any], monte_carlo: dict[str, Any] | None) -> tuple[str, filelock.Held]:
    """The critical section, atomic under the cross-process ledger lock: re-check the policy and that every earlier
    claim record is committed, take the claim's owner lock, record PREPARED, refuse (ABORTED) if any non-aborted
    claim overlaps the segment, else record CLAIMED."""
    why = HL.uncommitted_reason()
    if why:
        raise HoldoutError(f"no new holdout claim: {why}")
    cid = uuid.uuid4().hex
    with HL.locked():
        if load_policy().sha256 != binding["policy_sha256"]:
            raise HoldoutError("the holdout policy changed while the claim was being prepared: refused")
        owner = filelock.acquire(HL.claim_lock_path(cid), timeout=5)
        try:
            HL.append({"type": "claim_prepared", "claim_id": cid, "binding": binding, "monte_carlo": monte_carlo,
                       "owner": HL.owner_info()})
            busy = [c for c in HL.claims().values() if c["claim_id"] != cid and c["state"] != HL.ABORTED
                    and c["binding"]["root"] == binding["root"]
                    and _overlap(binding["start"], binding["end"], c["binding"]["start"], c["binding"]["end"])]
            if busy:
                HL.append({"type": "claim_aborted", "claim_id": cid, "reason": "refused before any access: the segment "
                           f"overlaps claim {busy[0]['claim_id']} ({busy[0]['state']})", "accessed": False})
                raise HoldoutError(f"{binding['start']}..{binding['end']} overlaps holdout claim {busy[0]['claim_id']} "
                                   f"({busy[0]['state']}, {busy[0]['binding']['start']}..{busy[0]['binding']['end']}): "
                                   "the holdout is evaluated once and never re-run")
            HL.append({"type": "claim_claimed", "claim_id": cid})
        except BaseException:
            owner.release()
            raise
    return cid, owner


def _issue_token(cid: str, binding: dict[str, Any], owner: filelock.Held) -> FinalOOSAccess:
    tok = FinalOOSAccess(cid, secrets.token_hex(16), tuple(sorted(binding.items())))
    with _LOCK:
        _TOKENS[tok.nonce] = {"token": tok, "state": "claimed", "owner": owner, "monte_carlo_done": False}
    return tok


def _entry(token: FinalOOSAccess | None) -> dict[str, Any]:
    with _LOCK:
        e = _TOKENS.get(getattr(token, "nonce", None)) if token is not None else None
    if e is None or e["token"] is not token:
        raise HoldoutError("refused: unknown final-OOS access (only a claim issues one; it cannot be forged, copied or "
                           "reused)")
    return e


def _match(token: FinalOOSAccess, actual: dict[str, Any]) -> None:
    b = dict(token.binding)
    for k, v in actual.items():
        want = b.get(k)
        if k in ("start", "end"):
            v = str(_day(v).date())
        if want != v:
            raise HoldoutError(f"refused: the final-OOS access was claimed for {k}={want!r}, not {v!r}")


def verify_binding(token: FinalOOSAccess, **actual: Any) -> None:
    """Refuse unless the access is open (CONSUMED in this process) and every given RUNTIME value equals the binding."""
    e = _entry(token)
    if e["state"] != "consumed":
        raise HoldoutError(f"refused: the final-OOS access is {e['state']}, not consumed (one evaluation only)")
    _match(token, actual)
    if load_policy().sha256 != token.get("policy_sha256"):
        raise HoldoutError("refused: the holdout policy changed since the claim was made")


def consume(token: FinalOOSAccess) -> None:
    """CLAIMED -> CONSUMED, mirrored to the anchor and written durably BEFORE the first byte of the dataset is read.
    The policy is re-verified under the same lock, so a policy change cannot slip between the check and the write."""
    e = _entry(token)
    if e["state"] != "claimed":
        raise HoldoutError(f"refused: the final-OOS access is {e['state']} (one evaluation only)")
    with HL.locked():
        if load_policy().sha256 != token.get("policy_sha256"):
            raise HoldoutError("refused: the holdout policy changed since the claim was made")
        c = HL.claims().get(token.claim_id)
        if c is None or c["state"] != HL.CLAIMED:
            raise HoldoutError(f"refused: holdout claim {token.claim_id} is {None if c is None else c['state']}, "
                               "not CLAIMED")
        HL.append({"type": "claim_consumed", "claim_id": token.claim_id, "owner": HL.owner_info()})
        with _LOCK:
            e["state"] = "consumed"


def check_final_load(token: FinalOOSAccess, *, dataset_ref: str, manifest_sha256: str) -> None:
    """The dataset bytes of a final evaluation may be read only through its CONSUMED access, for the bound dataset."""
    verify_binding(token, dataset_ref=dataset_ref, dataset_manifest_sha256=manifest_sha256)


def check_access(token: FinalOOSAccess, *, root: str, start: Any, end: Any,
                 dataset_identity: dict[str, Any] | None) -> None:
    """run_spec's check of the access against the ACTUAL market data it received (no ledger write)."""
    if not dataset_identity:
        raise HoldoutError("refused: a final OOS evaluation runs on a verified dataset only (no dataset identity)")
    verify_binding(token, root=root.split(":")[0], start=start, end=end, dataset_ref=dataset_identity.get("ref"),
                   dataset_manifest_sha256=dataset_identity.get("manifest_sha256"),
                   bars_content_sha256=dataset_identity.get("bars_content_sha256"))


def mark_monte_carlo(token: FinalOOSAccess) -> None:
    """The frozen final-evaluation Monte Carlo runs at most once per claim."""
    e = _entry(token)
    with _LOCK:
        if e["monte_carlo_done"]:
            raise HoldoutError("refused: the final-evaluation Monte Carlo of this claim was already produced")
        e["monte_carlo_done"] = True


def complete_claim(token: FinalOOSAccess, results: dict[str, Any]) -> dict[str, Any]:
    """CONSUMED -> COMPLETED with the recorded results; releases the claim."""
    e = _entry(token)
    with HL.locked():
        c = HL.claims().get(token.claim_id)
        if c is None or c["state"] != HL.CONSUMED:
            raise HoldoutError(f"holdout claim {token.claim_id} cannot complete from {None if c is None else c['state']}")
        rec = HL.append({"type": "claim_completed", "claim_id": token.claim_id, "results": results})
    _forget(token, e)
    return rec


def abort_unaccessed(token: FinalOOSAccess, reason: str) -> str | None:
    """After a failure: ABORTED only if CONSUMED was never written (the ledgers prove no data byte was read);
    otherwise the claim stays CONSUMED for good. Returns the final state (None if the ledgers cannot be read)."""
    with _LOCK:
        e = _TOKENS.get(token.nonce)
    state = None
    try:
        with HL.locked():
            c = HL.claims().get(token.claim_id)
            state = None if c is None else c["state"]
            if state in (HL.PREPARED, HL.CLAIMED):
                HL.append({"type": "claim_aborted", "claim_id": token.claim_id, "accessed": False,
                           "reason": f"failed before any reserved data was read: {str(reason)[:400]}"})
                state = HL.ABORTED
    finally:
        if e is not None:
            _forget(token, e)
    return state


def _forget(token: FinalOOSAccess, e: dict[str, Any]) -> None:
    with _LOCK:
        _TOKENS.pop(token.nonce, None)
        e["state"] = "spent"
    e["owner"].release()


def _owner_gone(claim_id: str) -> filelock.Held:
    lp = HL.claim_lock_path(claim_id)
    if filelock.held_here(lp):
        raise HoldoutError(f"claim {claim_id} is owned by this process")
    owner = filelock.try_exclusive(lp)
    if owner is None:
        raise HoldoutError(f"claim {claim_id}: its owner still holds the claim lock (alive or uncertain): refused")
    return owner


def recover_claim(claim_id: str, *, by: str, reason: str) -> str:
    """HUMAN recovery of a claim whose owner died. Releases (ABORTED) only a claim still PREPARED or CLAIMED AND
    whose owner provably no longer holds its claim lock. A CONSUMED claim is never released."""
    if not str(by).strip() or not str(reason).strip():
        raise HoldoutError("--by and --reason are required: recovery is a recorded human decision")
    with HL.locked():
        c = HL.claims().get(claim_id)
        if c is None:
            raise HoldoutError(f"no holdout claim {claim_id}")
        if c["state"] in (HL.COMPLETED, HL.ABORTED):
            return c["state"]
        if c["state"] == HL.CONSUMED:
            raise HoldoutError(f"claim {claim_id} is CONSUMED: reserved data may have been read, so it stays consumed "
                               "for good (no recovery can release it; see holdout-complete)")
        owner = _owner_gone(claim_id)
        try:
            HL.append({"type": "claim_aborted", "claim_id": claim_id, "accessed": False, "recovered_by": str(by)[:200],
                       "reason": f"owner process gone before CONSUMED was written: {str(reason)[:400]}"})
        finally:
            owner.release()
    return HL.ABORTED


def complete_consumed_claim(claim_id: str, run_folder: str | Path, *, app, bt, validation_records: list[dict[str, Any]],
                            by: str, reason: str) -> dict[str, Any]:
    """HUMAN completion of a CONSUMED claim whose owner died AFTER the one evaluation was recorded. Never evaluates the
    data again: it verifies the recorded run folder and its validation-ledger record against the claim's binding,
    produces the plan-frozen Monte Carlo from the RECORDED trades if it is missing, and writes COMPLETED.
    A claim without a recorded run (the owner died before the result was written) cannot be completed and stays
    CONSUMED for good."""
    import json as _json

    from src.research.runs import verify_manifest

    if not str(by).strip() or not str(reason).strip():
        raise HoldoutError("--by and --reason are required: completing a claim is a recorded human decision")
    folder = Path(run_folder)
    with HL.locked():
        c = HL.claims().get(claim_id)
        if c is None or c["state"] != HL.CONSUMED:
            raise HoldoutError(f"claim {claim_id} is {None if c is None else c['state']}: only a CONSUMED claim can be "
                               "completed")
        owner = _owner_gone(claim_id)
        try:
            v = verify_manifest(folder)
            if not v["ok"]:
                raise HoldoutError(f"run folder failed verification: {', '.join(v['problems'])}")
            m, b = v["manifest"], c["binding"]
            manifest_sha = _json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["manifest_sha256"]
            hold = (m.get("oos") or {}).get("holdout") or {}
            if m.get("purpose") != "oos" or hold.get("claim_id") != claim_id:
                raise HoldoutError("the run folder is not the final evaluation of this claim")
            if (m["strategy"]["sha256"], m["data"]["sha256"], m["period"]["start"], m["period"]["end"]) != \
                    (b["version_sha256"], b["data_sha256"], b["start"], b["end"]):
                raise HoldoutError("the run folder does not match the claim's binding")
            rec = [r for r in validation_records if r.get("type") == "oos_reveal"
                   and r.get("record_hash") == (m.get("oos") or {}).get("ledger_record_hash")]
            if not rec or (rec[0].get("holdout") or {}).get("claim_id") != claim_id \
                    or rec[0].get("results_fingerprint") != m.get("results_fingerprint"):
                raise HoldoutError("the run is not the one recorded for this claim in the canonical validation ledger")
            mc_cfg = c["history"][0].get("monte_carlo")
            mc_sha, mc_name = None, None
            if mc_cfg:
                from src.research.monte_carlo import run_report

                path = folder / "monte_carlo_final_oos.json"
                if path.is_file():
                    rep = _json.loads(path.read_text(encoding="utf-8"))
                    if (rep.get("final_evaluation") or {}).get("claim_id") != claim_id:
                        raise HoldoutError("the existing final Monte Carlo report belongs to another claim")
                else:
                    path = run_report(folder, app, bt, filename=path.name, final_evaluation={"recovery": _RECOVERY,
                                      "claim": c, "frozen": mc_cfg}, **mc_cfg)
                mc_sha, mc_name = file_sha256(path), path.name
            done = HL.append({"type": "claim_completed", "claim_id": claim_id, "completed_by": str(by)[:200],
                              "reason": str(reason)[:400],
                              "results": {"results_fingerprint": m.get("results_fingerprint"),
                                          "run_manifest_sha256": manifest_sha,
                                          "validation_record_hash": rec[0]["record_hash"],
                                          "monte_carlo_sha256": mc_sha, "monte_carlo_file": mc_name,
                                          "evidence": (m.get("evaluation") or {}).get("evidence")}})
        finally:
            owner.release()
    return done


_RECOVERY = object()          # authorises run_report inside complete_consumed_claim only (never a public value)


def completed_record(claim_id: str) -> dict[str, Any] | None:
    """The COMPLETED record of a claim (verified ledgers), or None."""
    c = HL.claims().get(claim_id)
    if c is None or c["state"] != HL.COMPLETED:
        return None
    return c["history"][-1]


# ------------------------------------------------------------------------------------------- reserved bars
def reserved_mask(root: str, index) -> np.ndarray:
    """True for every bar whose Globex TRADE DATE (CME calendar, DST-aware) falls in a blocked window of ``root``.
    Bars outside the exchange schedule are judged by their UTC date and the following date (conservative)."""
    pol = load_policy()
    rs = pol.reservations_for(root)
    idx = pd.DatetimeIndex(index)
    if not rs or len(idx) == 0:
        return np.zeros(len(idx), dtype=bool)
    from src.histdata.calendar import load_calendar

    utc = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    td = pd.Series(load_calendar().trade_dates(utc).to_numpy())
    fallback = pd.Series(utc.tz_localize(None).normalize() + pd.Timedelta(days=1))
    day = pd.to_datetime(td).fillna(fallback).to_numpy()
    out = np.zeros(len(idx), dtype=bool)
    for r in rs:
        end = np.datetime64("2262-01-01") if r.end is None else np.datetime64(r.end)
        out |= (day >= np.datetime64(r.blocked_start)) & (day <= end)
    return out


def refuse_reserved_bars(root: str, index, what: str) -> None:
    """Refuse (before any computation) if any bar falls in a blocked window. The bars were read only to look at their
    timestamps: a data-quality read, never research."""
    mask = reserved_mask(root, index)
    if mask.any():
        r = load_policy().reservations_for(root)[0]
        first = pd.DatetimeIndex(index)[mask][0]
        raise HoldoutError(f"{what} for {root.split(':')[0]} contains {int(mask.sum())} reserved bar(s) (from {first}) "
                           f"in the {r.describe()}: refused before any indicator, feature or strategy computation. Use "
                           "input that ends before the blocked window (the bars were only inspected, not used)")


def check_dataset_meta(root: str, manifest: dict[str, Any], what: str) -> None:
    """Metadata-only gate for non-final research: a dataset whose manifest reaches into a blocked window is refused
    before ANY of its data bytes are read or hashed (reading it would expose reserved bars)."""
    last = pd.Timestamp(manifest["last_bar_utc"])
    if reserved_mask(root, pd.DatetimeIndex([last])).any():
        r = load_policy().reservations_for(root)[0]
        raise HoldoutError(f"{what}: dataset {manifest.get('dataset_id')}@{manifest.get('version')} runs to {last} and "
                           f"reaches the {r.describe()}. Research reads datasets that end before the blocked window only "
                           "(refused from the manifest; no data byte was read)")


def check_simulation(root: str, first: Any, last: Any, access: FinalOOSAccess | None = None, *,
                     session_first: Any = None, session_last: Any = None) -> dict[str, Any] | None:
    """Gate after slicing, on the dates ACTUALLY simulated. Outside every blocked window: allowed. Inside: only with an
    access opened (CONSUMED) for exactly the segment containing the simulated sessions."""
    pol = load_policy()
    if not pol.touching(root, first, last):
        return None
    if access is None:
        r = pol.touching(root, first, last)[0]
        raise HoldoutError(f"refused: the simulation of {root.split(':')[0]} {_day(first).date()}..{_day(last).date()} "
                           f"touches the {r.describe()}. Only the one-time final OOS evaluation of a registered plan "
                           "may simulate reserved data")
    e = _entry(access)
    if e["state"] != "consumed":
        raise HoldoutError("refused: the final-OOS access was not opened by its claim (CONSUMED) before simulation")
    s0 = first if session_first is None else session_first
    s1 = last if session_last is None else session_last
    if access.get("root") != root.split(":")[0] or not (
            _day(access.get("start")) <= _day(s0) and _day(s1) <= _day(access.get("end"))):
        raise HoldoutError("refused: the simulation is outside the claimed segment (it stays CONSUMED)")
    return {"claim_id": access.claim_id, "reservation": access.get("reservation"), "plan": access.get("plan")}


def simulated_span(frame: pd.DataFrame, *, sessions_only: bool = False) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """(first, last) date of the bars a simulation will run on. By default the conservative union of bar (index)
    dates and session dates; with ``sessions_only`` the session dates (bar dates when there are none)."""
    if frame is None or frame.empty:
        return None
    idx = pd.DatetimeIndex(frame.index)
    lo, hi = _day(idx.min()), _day(idx.max())
    if "session_date" in frame.columns:
        sd = pd.to_datetime(frame["session_date"]).dropna()
        if len(sd):
            if sessions_only:
                return _day(sd.min()), _day(sd.max())
            lo, hi = min(lo, _day(sd.min())), max(hi, _day(sd.max()))
    return lo, hi


__all__ = ["HoldoutError", "Policy", "Reservation", "FinalOOSAccess", "load_policy", "check_research",
           "check_run_record", "check_plan", "claim_final_oos", "verify_binding", "consume", "check_final_load",
           "check_access", "mark_monte_carlo", "complete_claim", "abort_unaccessed", "recover_claim",
           "complete_consumed_claim", "completed_record", "reserved_mask", "refuse_reserved_bars",
           "check_dataset_meta", "check_simulation", "simulated_span", "eval_config_sha256", "EXTRA_ENV",
           "DEFAULT_FILE"]

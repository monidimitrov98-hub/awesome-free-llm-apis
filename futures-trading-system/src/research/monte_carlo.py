"""Monte Carlo robustness of a research run (Sprint 11).

What every result is (``evidence_type``):

* HISTORICAL   -- the single path the backtest actually produced.
* MONTE_CARLO  -- simulated paths built by RESAMPLING / RESEQUENCING the run's own historical trades. They
                  show the spread of outcomes the same trade population could have produced; they add no new
                  information about the market and inherit every bias of the backtest.
* VERIFIED_RULE_CHECK -- a prop firm threshold evaluated on simulated paths, only for rules whose
                  verification is VERIFIED. Unverified, conflicting or unsupported rules are listed as
                  NOT EVALUATED (never assumed). An unverified / non-operational profile is reported as such.

Methods (each documented with its assumption):

* ``trade_iid``     -- trades drawn independently with replacement. Assumes independent trades: it ignores
                       clustering, so its tails are usually TOO THIN.
* ``day_block``     -- whole trading days drawn with replacement (keeps intraday dependence).
* ``moving_block``  -- blocks of L consecutive trading days (keeps short-range serial dependence across
                       days); L = ceil(n_days ** (1/3)) unless given.
* ``shuffle``       -- a permutation of exactly the historical trades (same total P&L; sequence / loss-streak
                       risk only).

Cost scenarios re-price every trade with extra slippage ticks per side and a commission multiplier. They are
approximations (a full re-run, as the research CLI's cost sensitivity does, also changes fills).
Every simulation uses a fixed seed; the manifest hashes the exact input trades and parameters, so the same
inputs always give byte-identical outputs.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

METHODS = ("trade_iid", "day_block", "moving_block", "shuffle")
QUANTILES = (5, 25, 50, 75, 95)


@dataclass(frozen=True)
class CostScenario:
    name: str = "as_backtested"
    extra_slippage_ticks_per_side: float = 0.0
    commission_multiplier: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "extra_slippage_ticks_per_side": self.extra_slippage_ticks_per_side,
                "commission_multiplier": self.commission_multiplier}


DEFAULT_SCENARIOS = (CostScenario(), CostScenario("slippage_+1_tick", 1.0, 1.0),
                     CostScenario("slippage_+2_ticks", 2.0, 1.0), CostScenario("commission_x2", 0.0, 2.0),
                     CostScenario("stress_+2_ticks_commission_x2", 2.0, 2.0))


def trades_sha256(trades: pd.DataFrame) -> str:
    cols = [c for c in ("trade_id", "entry_time", "exit_time", "side", "quantity", "net_pnl", "commission",
                        "exit_session_date", "mae_dollars") if c in trades.columns]
    return hashlib.sha256(trades[cols].to_csv(index=False).encode()).hexdigest()


def repriced(trades: pd.DataFrame, scenario: CostScenario, tick_value: float) -> np.ndarray:
    q = trades["quantity"].astype(float).to_numpy()
    net = trades["net_pnl"].astype(float).to_numpy()
    extra = q * 2 * scenario.extra_slippage_ticks_per_side * tick_value
    extra += trades["commission"].astype(float).to_numpy() * (scenario.commission_multiplier - 1.0)
    return net - extra


def _paths(pnl: np.ndarray, days: np.ndarray, mae: np.ndarray, method: str, n_paths: int, rng: np.random.Generator,
           block: int | None) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Simulated trade sequences: (P&L, MAE, day label) per trade. Trade-level methods keep the historical
    number of trades per day (the k-th simulated trade belongs to the k-th historical trade's day); block
    methods keep whole drawn days together (each drawn day is a new simulated day)."""
    n = len(pnl)
    out = []
    if method in ("trade_iid", "shuffle"):
        for _ in range(n_paths):
            i = rng.integers(0, n, size=n) if method == "trade_iid" else rng.permutation(n)
            out.append((pnl[i], mae[i], days))
        return out
    uniq = list(dict.fromkeys(days.tolist()))
    groups = [np.nonzero(days == d)[0] for d in uniq]
    nd = len(groups)
    if method == "day_block":
        picks = (rng.integers(0, nd, size=nd) for _ in range(n_paths))
    elif method == "moving_block":
        L = min(block or max(1, math.ceil(nd ** (1 / 3))), nd)

        def _draw() -> list[int]:
            chosen: list[int] = []
            while len(chosen) < nd:
                s0 = int(rng.integers(0, nd - L + 1))
                chosen.extend(range(s0, s0 + L))
            return chosen[:nd]
        picks = (_draw() for _ in range(n_paths))
    else:
        raise ValueError(f"unknown Monte Carlo method {method!r}; one of {METHODS}")
    for pick in picks:
        i = np.concatenate([groups[k] for k in pick])
        lab = np.concatenate([np.full(len(groups[k]), j) for j, k in enumerate(pick)]).astype(str)
        out.append((pnl[i], mae[i], lab))
    return out


def _streak(x: np.ndarray) -> int:
    best = cur = 0
    for v in x:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def _max_dd(cum: np.ndarray) -> float:
    if not len(cum):
        return 0.0
    c = np.concatenate([[0.0], cum])
    return float(np.max(np.maximum.accumulate(c) - c))


def _q(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {f"p{q}": None for q in QUANTILES}
    arr = np.asarray(values, dtype=float)
    return {f"p{q}": float(np.percentile(arr, q)) for q in QUANTILES}


def prop_rules(profile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(evaluated, not_evaluated) prop rules for simulation: only VERIFIED max_loss / daily_loss rules with
    the parameters this simulation can apply."""
    if profile is None:
        return [], []
    ev, skipped = [], []
    for r in profile.rules:
        if r.type not in ("max_loss", "daily_loss"):
            continue
        if not r.applicable:
            skipped.append({"rule": r.id, "type": r.type, "why": "not applicable to this product"})
            continue
        if not r.applies_to(profile.stage):
            continue
        if r.verification != "VERIFIED":
            skipped.append({"rule": r.id, "type": r.type, "why": f"rule is {r.verification}: never assumed"})
            continue
        if r.missing_params or r.params.get("amount") is None:
            skipped.append({"rule": r.id, "type": r.type, "why": "parameters incomplete"})
            continue
        entry = {"rule": r.id, "type": r.type, "amount": float(r.params["amount"]),
                 "model": r.params.get("model", "static")}
        if r.type == "max_loss" and r.params.get("lock_at") is not None:
            lv = r.params.get("lock_verification", r.verification)
            if lv == "VERIFIED":
                entry["lock_at"] = float(r.params["lock_at"])
            else:
                entry["lock_note"] = f"lock at {r.params['lock_at']} NOT applied ({lv}): floor keeps trailing (conservative)"
        ev.append(entry)
    return ev, skipped


def _breaches(pnl: np.ndarray, mae: np.ndarray, days: np.ndarray, capital: float, rules: list[dict[str, Any]]
              ) -> dict[str, bool]:
    """Trade-level evaluation (intra-trade low = equity before the trade minus its MAE when known: conservative
    for losses inside a trade; bar-level gaps inside a trade are not represented)."""
    out = {}
    cum = capital + np.concatenate([[0.0], np.cumsum(pnl)])
    lows = cum[:-1] - np.nan_to_num(mae, nan=0.0)
    for r in rules:
        hit = False
        if r["type"] == "max_loss":
            model, amt = r["model"], r["amount"]
            if model == "static":
                floor = np.full(len(pnl), capital - amt)
            elif model == "trailing_intraday":
                floor = np.maximum.accumulate(cum[:-1]) - amt
            else:   # trailing_eod: the floor trails the end-of-day balance (known after each day)
                floor = np.empty(len(pnl))
                hw, cur_day, day_close = capital, None, capital
                for i in range(len(pnl)):
                    if cur_day is not None and days[i] != cur_day:
                        hw = max(hw, day_close)
                    cur_day = days[i]
                    floor[i] = hw - amt
                    day_close = cum[i + 1]
            if "lock_at" in r:
                floor = np.minimum(floor, r["lock_at"])
            hit = bool(np.any(lows <= floor) or np.any(cum[1:] <= floor))
        elif r["type"] == "daily_loss":
            day_pnl: dict[Any, float] = {}
            for i, d in enumerate(days):
                worst = day_pnl.get(d, 0.0) - (0.0 if math.isnan(mae[i]) else mae[i])
                day_pnl[d] = day_pnl.get(d, 0.0) + pnl[i]
                if worst <= -r["amount"] or day_pnl[d] <= -r["amount"]:
                    hit = True
                    break
        out[r["rule"]] = hit
    return out


def simulate(trades: pd.DataFrame, *, capital: float, tick_value: float, methods: tuple[str, ...] = METHODS,
             n_paths: int = 2000, seed: int = 20261125, scenarios: tuple[CostScenario, ...] = DEFAULT_SCENARIOS,
             ruin_loss: float | None = None, drawdown_levels: tuple[float, ...] = (), block: int | None = None,
             profile=None, profile_operational: bool = False, fan_points: int = 50) -> dict[str, Any]:
    """Monte Carlo report. ``ruin_loss``: probability that equity falls to ``capital - ruin_loss`` (defaults
    to the account's own drawdown if given by the caller; otherwise not computed)."""
    if trades is None or trades.empty:
        return {"evidence_type": "MONTE_CARLO", "status": "NO TRADES: nothing to simulate", "paths": 0}
    if n_paths < 1 or n_paths > 200_000:
        raise ValueError("n_paths must be in [1, 200000]")
    bad = [m for m in methods if m not in METHODS]
    if bad:
        raise ValueError(f"unknown Monte Carlo method(s) {bad}; one of {METHODS}")
    t = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    days = pd.to_datetime(t["exit_session_date"]).dt.strftime("%Y-%m-%d").to_numpy()
    mae = t["mae_dollars"].astype(float).to_numpy() if "mae_dollars" in t.columns else np.full(len(t), np.nan)
    rules, skipped = prop_rules(profile)
    hist_pnl = t["net_pnl"].astype(float).to_numpy()
    hist_cum = np.cumsum(hist_pnl)
    result: dict[str, Any] = {
        "historical": {"evidence_type": "HISTORICAL", "trades": len(t), "days": len(set(days)),
                       "final_pnl": float(hist_cum[-1]), "max_drawdown": _max_dd(hist_cum),
                       "max_consecutive_losses": _streak(hist_pnl),
                       "prop_breaches": _breaches(hist_pnl, mae, days, capital, rules) if rules else {}},
        "methods": {}, "scenarios": [s.to_dict() for s in scenarios],
        "prop": {"profile": getattr(profile, "key", None), "profile_operational": bool(profile_operational),
                 "evaluated_rules": rules, "not_evaluated": skipped,
                 "label": ("VERIFIED RULES ONLY" if profile_operational else
                           "RESEARCH ESTIMATE: profile not operational (UNVERIFIED or not activated); only rules "
                           "individually marked VERIFIED are simulated") if profile is not None else "no profile"},
    }
    for mi, method in enumerate(methods):
        per_scenario = {}
        for si, sc in enumerate(scenarios):
            rng = np.random.default_rng([seed, mi, si])
            pnl = repriced(t, sc, tick_value)
            sims = _paths(pnl, days, mae, method, n_paths, rng, block)
            finals, dds, streaks, ruins = [], [], [], 0
            breach_counts = {r["rule"]: 0 for r in rules}
            fan_n = min(fan_points, max(1, len(t)))
            grid = np.linspace(0, len(t), fan_n + 1).astype(int)[1:]
            fan_vals = [[] for _ in range(fan_n)]
            for s, smae, dlab in sims:
                cum = np.cumsum(s)
                finals.append(float(cum[-1]) if len(cum) else 0.0)
                dds.append(_max_dd(cum))
                streaks.append(_streak(s))
                if ruin_loss is not None:
                    lows = np.concatenate([[0.0], cum[:-1]]) - np.nan_to_num(smae, nan=0.0)
                    if np.any(lows <= -ruin_loss) or np.any(cum <= -ruin_loss):
                        ruins += 1
                if rules:
                    for rid, hit in _breaches(s, smae, dlab, capital, rules).items():
                        breach_counts[rid] += int(hit)
                for j, g in enumerate(grid):
                    if len(cum):
                        fan_vals[j].append(float(cum[min(g, len(cum)) - 1]))
            per_scenario[sc.name] = {
                "evidence_type": "MONTE_CARLO", "paths": n_paths,
                "final_pnl": _q(finals), "prob_final_pnl_negative": float(np.mean(np.asarray(finals) < 0)),
                "max_drawdown": _q(dds),
                "prob_drawdown_at_least": {f"{lvl:g}": float(np.mean(np.asarray(dds) >= lvl)) for lvl in drawdown_levels},
                "max_consecutive_losses": _q([float(x) for x in streaks]),
                "risk_of_ruin": (ruins / n_paths) if ruin_loss is not None else None,
                "ruin_loss": ruin_loss,
                "prop_breach_probability": {rid: c / n_paths for rid, c in breach_counts.items()},
                "equity_fan": [{"trade": int(g), **_q(fan_vals[j])} for j, g in enumerate(grid)],
            }
        result["methods"][method] = per_scenario
    result["assumptions"] = {
        "trade_iid": "independent trades (ignores clustering: tails usually too thin)",
        "day_block": "independent trading days (keeps intraday dependence)",
        "moving_block": f"blocks of consecutive days (L = {block or 'ceil(n_days^(1/3))'})",
        "shuffle": "same trades, random order (sequence risk only)",
        "costs": "scenarios re-price trades; a full re-run (CLI cost sensitivity) also changes fills",
        "prop": "trade-level, MAE-based intra-trade lows; gaps inside bars and the firm's exact timing are not modelled",
    }
    result["manifest"] = {"trades_sha256": trades_sha256(t), "seed": seed, "n_paths": n_paths, "methods": list(methods),
                          "block": block, "capital": capital, "tick_value": tick_value, "ruin_loss": ruin_loss,
                          "drawdown_levels": list(drawdown_levels),
                          "profile": getattr(profile, "key", None), "profile_sha256": getattr(profile, "sha256", None)}
    result["manifest"]["sha256"] = hashlib.sha256(json.dumps(result["manifest"], sort_keys=True).encode()).hexdigest()
    result["disclaimer"] = ("Monte Carlo paths resample the backtest's own trades. They describe uncertainty of "
                            "THIS trade sample, not future performance, and inherit every backtest assumption.")
    return result


MC_CONFIG_KEYS = ("methods", "paths", "seed", "ruin_loss", "drawdown_levels", "block")


def check_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate a frozen Monte Carlo configuration (plan ``[final_evaluation.monte_carlo]``); returns it normalised."""
    if not isinstance(cfg, dict) or set(cfg) - set(MC_CONFIG_KEYS) or "seed" not in cfg or "paths" not in cfg:
        raise ValueError(f"monte_carlo needs seed and paths and only the keys {MC_CONFIG_KEYS}")
    out: dict[str, Any] = {}
    methods = cfg.get("methods") or list(METHODS)
    if not isinstance(methods, list) or not methods or any(m not in METHODS for m in methods):
        raise ValueError(f"monte_carlo.methods must be a non-empty subset of {METHODS}")
    out["methods"] = list(methods)
    for k in ("paths", "seed"):
        if not isinstance(cfg[k], int) or isinstance(cfg[k], bool):
            raise ValueError(f"monte_carlo.{k} must be an integer")
    if not 1 <= cfg["paths"] <= 200_000:
        raise ValueError("monte_carlo.paths must be in [1, 200000]")
    out["paths"], out["seed"] = cfg["paths"], cfg["seed"]
    rl = cfg.get("ruin_loss")
    if rl is not None and (not isinstance(rl, (int, float)) or isinstance(rl, bool) or rl <= 0):
        raise ValueError("monte_carlo.ruin_loss must be a positive number")
    out["ruin_loss"] = None if rl is None else float(rl)
    dl = cfg.get("drawdown_levels") or []
    if not isinstance(dl, list) or any(not isinstance(x, (int, float)) or isinstance(x, bool) or x <= 0 for x in dl):
        raise ValueError("monte_carlo.drawdown_levels must be a list of positive numbers")
    out["drawdown_levels"] = [float(x) for x in dl]
    bl = cfg.get("block")
    if bl is not None and (not isinstance(bl, int) or isinstance(bl, bool) or bl < 1):
        raise ValueError("monte_carlo.block must be a positive integer")
    out["block"] = bl
    return out


def run_report(folder, app, bt, *, methods=None, paths: int = 2000, seed: int = 20261125, ruin_loss=None,
               drawdown_levels=(), block=None, filename: str | None = None, final_evaluation=None):
    """Monte Carlo report of a verified research run folder, written into the folder; returns its path.

    Holdout (Sprint 13 P1.1): a run whose period touches a reserved window is refused, EXCEPT as the frozen
    component of its own final OOS evaluation: ``final_evaluation`` must then be the live final-OOS access of that
    run's claim (CONSUMED, issued in this process), and the configuration must be the one frozen by the plan."""
    import json as _json
    from pathlib import Path as _Path

    from src.research.runs import _jsonable, verify_manifest

    folder = _Path(folder)
    v = verify_manifest(folder)
    if not v["ok"]:
        raise ValueError(f"run folder failed verification: {', '.join(v['problems'])}")
    m = v["manifest"]
    from src.research import holdout as H

    if final_evaluation is None:
        H.check_run_record(m["instrument"], m["period"], "Monte Carlo of a run")      # standalone: never on holdout
        fe = None
    else:
        claim = ((m.get("oos") or {}).get("holdout") or {}).get("claim_id")
        if final_evaluation.get("recovery") is H._RECOVERY:       # complete_consumed_claim only (owner gone, CONSUMED)
            c = final_evaluation["claim"]
            cid, cfg_sha = c["claim_id"], c["binding"]["eval_config_sha256"]
            if c["state"] != "CONSUMED" or claim != cid:
                raise H.HoldoutError("the recovery Monte Carlo belongs to its own CONSUMED claim only")
        else:
            tok = final_evaluation.get("token")
            e = H._entry(tok)
            if e["state"] != "consumed" or claim != tok.claim_id:
                raise H.HoldoutError("the final-evaluation Monte Carlo belongs to its own open claim only")
            cid, cfg_sha = tok.claim_id, tok.get("eval_config_sha256")
        frozen = check_config(dict(final_evaluation.get("frozen") or {}))
        asked = check_config({"methods": list(methods) if methods else list(METHODS), "paths": paths, "seed": seed,
                              "ruin_loss": ruin_loss, "drawdown_levels": list(drawdown_levels), "block": block})
        if frozen != asked or H.eval_config_sha256(final_evaluation.get("frozen")) != cfg_sha:
            raise H.HoldoutError("the Monte Carlo configuration is not the one frozen before the reveal")
        if final_evaluation.get("recovery") is not H._RECOVERY:
            H.mark_monte_carlo(tok)                                  # at most once per claim
        fe = {"claim_id": cid, "frozen_config": frozen, "eval_config_sha256": cfg_sha}
    trades = pd.read_csv(folder / "trades.csv")
    root = m["instrument"].split(":")[0]
    spec = app.contracts[root]
    profile, operational = None, False
    if m.get("prop"):
        from src.risk.prop_firms.versions import Registry, operational_state

        pid, _, ver = m["prop"]["profile"].partition("@")
        profile = Registry().get(pid, ver)
        if profile.sha256 != m["prop"]["sha256"]:
            raise ValueError("the run's prop profile file has changed since the run")
        operational = bool(operational_state(profile)["operational"])
    rep = simulate(trades, capital=bt.initial_capital, tick_value=spec.tick_value,
                   methods=tuple(methods) if methods else METHODS, n_paths=paths, seed=seed, ruin_loss=ruin_loss,
                   drawdown_levels=tuple(drawdown_levels), block=block, profile=profile,
                   profile_operational=operational)
    rep["strategy"] = m["strategy"]
    rep["run"] = {"folder": str(folder), "manifest_sha256": _json.loads((folder / "manifest.json").read_text())["manifest_sha256"],
                  "purpose": m["purpose"], "instrument": m["instrument"], "period": m["period"]}
    if fe is not None:
        rep["final_evaluation"] = fe
    out = folder / (filename or f"monte_carlo_seed{seed}_paths{paths}.json")
    out.write_text(_json.dumps(_jsonable(rep), indent=2, default=str), encoding="utf-8")
    return out


__all__ = ["simulate", "CostScenario", "DEFAULT_SCENARIOS", "METHODS", "prop_rules", "trades_sha256", "repriced",
           "run_report", "check_config", "MC_CONFIG_KEYS"]

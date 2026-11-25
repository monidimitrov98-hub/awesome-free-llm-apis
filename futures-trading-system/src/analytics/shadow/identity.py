"""Run identity: which strategy, configuration, code, regime model, costs and session rules produced a
journal. Journaled as ``run_identity`` at every session start and recovery, so every analytics
record can be traced to the exact identity it was produced under (Sprint 8)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from functools import lru_cache
from typing import Any

SESSION_TYPES = ("MOCK", "REPLAY", "SHADOW", "MONITOR", "UNKNOWN")


def _canon(obj: Any) -> str:
    def default(o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return str(o)
    if dataclasses.is_dataclass(obj):
        obj = dataclasses.asdict(obj)
    return json.dumps(obj, sort_keys=True, default=default, separators=(",", ":"))


def sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def code_identity_cached() -> dict[str, Any]:
    """Source fingerprint + git commit (computed once per process: code does not change while running)."""
    try:
        from src.backtest.manifest import code_identity

        ci = code_identity()
        return {"commit": ci["commit"], "dirty": ci["dirty"], "source_sha256": ci["source_sha256"],
                "trusted": ci["trusted"]}
    except Exception as exc:  # noqa: BLE001 -- identity is informative; never blocks trading
        return {"commit": "unknown", "dirty": None, "source_sha256": None, "trusted": False,
                "error": type(exc).__name__}


def run_identity(session) -> dict[str, Any]:
    """Identity of a PaperSession (strategy, config, code, regime, contracts, costs, session rules)."""
    roots = list(session.roots)
    strategies = {r: getattr(session.inner.get(r), "name", type(session.inner.get(r)).__name__) for r in roots}
    rc = session.regime
    gate = next((s for s in session.strategies.values() if hasattr(s, "engine")), None)
    regime = {"enabled": bool(rc is not None and rc.enabled)}
    if rc is not None and rc.enabled:
        regime.update(config_sha256=sha(rc), model_version=getattr(rc, "model_version", None),
                      classifier=getattr(rc, "classifier", None), enforcement=getattr(rc, "enforcement", None),
                      artifacts=dict(getattr(getattr(gate, "engine", None), "artifacts", {}) or {}))
    meta = dict(session.session_meta or {})
    binding = getattr(session, "binding", None)
    if binding is not None:      # Sprint 11: a governance-approved registry version
        strategy = {"names": strategies, "config_sha256": binding.sha256,
                    "version": f"{binding.strategy_id}@{binding.version}", "registry": binding.identity(),
                    "signal_timeframe": binding.signal_timeframe, "context_timeframe": None}
    else:
        strategy_cfg_sha = sha(session.scfg)
        strategy = {"names": strategies, "config_sha256": strategy_cfg_sha,
                    "version": f"{sorted(set(strategies.values()))[0] if strategies else '?'}@{strategy_cfg_sha[:12]}",
                    "signal_timeframe": session.scfg.signal_timeframe, "context_timeframe": session.scfg.context_timeframe}
    return {
        "schema": 1,
        "strategy": strategy,
        "code": code_identity_cached(),
        "regime": regime,
        "contracts": {r: {"tick_size": session.specs[r].tick_size, "point_value": session.specs[r].point_value}
                      for r in roots},
        "session_rules_sha256": sha(session.session),
        "costs": {"sha256": sha({"costs": session.bt.costs, "slippage": session.bt.slippage}),
                  "slippage": dataclasses.asdict(session.bt.slippage),
                  "commission_per_side": {r: session.bt.cost_for(r).commission_per_contract_per_side for r in roots}},
        "account_profile": {"profile": getattr(session.bt.account_rules, "profile", None),
                            "sha256": sha(session.bt.account_rules)},
        "risk_sha256": sha(session.bt.risk),
        "run_kind": session.run_kind,
        "session_type": meta.get("session_type", "UNKNOWN"),
        "data_label": meta.get("data_label"),
        "provider": meta.get("provider"),
        "test_strategy": bool(meta.get("test_strategy")),
        "prop_firm": ({"profile": session.prop.profile.key, "profile_sha256": session.prop.profile.sha256,
                       "ruleset_sha256": session.prop.profile.ruleset_sha256,
                       "operational": bool(session.prop.operational.get("operational"))}
                      if getattr(session, "prop", None) is not None else None),
    }

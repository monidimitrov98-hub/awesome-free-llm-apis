"""Sprint 4: suitability rules, the regime gate, fail-closed behaviour, LLM layer, and
proof that the regime layer cannot bypass the risk engine or AccountGuard."""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.ai.market_regime import ml
from src.ai.market_regime.explain import MAX_CHARS, LLMExplainer, TemplateExplainer
from src.ai.market_regime.filters import BLOCKED, RegimeGatedStrategy
from src.ai.market_regime.models import (
    Classification,
    ConfidenceLevel as CL,
    RangeRegime as Rg,
    RegimeSnapshot,
    StructureState as St,
    Suitability as S,
    TrendRegime as Tr,
    VolatilityChange as Vc,
    VolatilityRegime as Vr,
)
from src.ai.market_regime.suitability import evaluate_suitability
from src.backtest.orders import OrderRequest
from src.risk.account import AccountRules
from tests.backtest_helpers import ScriptedStrategy, frame, make_config, run
from tests.regime_helpers import RC

SUIT = RC.suitability


def cls(trend=Tr.STRONG_BULLISH, direction=1, rng=Rg.NONE, vol=Vr.NORMAL, vc=Vc.STABLE, st=St.HH_HL):
    return Classification(trend, rng, vol, vc, st, direction, 0.9)


def snap(c=None, *, valid=True, level=CL.HIGH, ctx="same", feats=None, error=None):
    c = cls() if c is None and valid else c
    context = c if ctx == "same" else ctx
    return RegimeSnapshot(pd.Timestamp("2025-03-10 14:05", tz="UTC"), pd.Timestamp("2025-03-10 14:00", tz="UTC"),
                          "MES", "test", valid, c, context, 0.8 if valid else None, level if valid else CL.INSUFFICIENT,
                          feats or {"rf_vwap_dist_atr": 0.5, "rf_atr_ratio": 1.0}, (), error)


def mirror(c: Classification) -> Classification:
    t = {Tr.STRONG_BULLISH: Tr.STRONG_BEARISH, Tr.MODERATE_BULLISH: Tr.MODERATE_BEARISH}.get(c.trend, c.trend)
    s = {St.HH_HL: St.LH_LL, St.LH_LL: St.HH_HL, St.CHANGE_OF_CHARACTER_DOWN: St.CHANGE_OF_CHARACTER_UP,
         St.CHANGE_OF_CHARACTER_UP: St.CHANGE_OF_CHARACTER_DOWN}.get(c.structure, c.structure)
    return replace(c, trend=t, direction=-c.direction, structure=s)


# ----------------------------------------------------------------- suitability

@pytest.mark.parametrize("s,direction,expected", [
    (snap(), "long", S.FAVORABLE),
    (snap(), "short", S.UNFAVORABLE),                                          # trend against the entry
    (snap(level=CL.LOW), "long", S.NEUTRAL),                                   # uncertain -> not favorable
    (snap(cls(trend=Tr.WEAK)), "long", S.UNFAVORABLE),
    (snap(cls(trend=Tr.NONE, direction=0, rng=Rg.CHOPPY)), "long", S.UNFAVORABLE),
    (snap(cls(rng=Rg.TIGHT_CONSOLIDATION)), "long", S.UNFAVORABLE),
    (snap(cls(st=St.CHANGE_OF_CHARACTER_DOWN)), "long", S.NEUTRAL),
    (snap(ctx=None), "long", S.UNFAVORABLE),                                   # context required but unavailable
    (snap(ctx=cls(trend=Tr.STRONG_BEARISH, direction=-1)), "long", S.UNFAVORABLE),
    (snap(feats={"rf_vwap_dist_atr": 3.0}), "long", S.UNFAVORABLE),
    (snap(cls(vc=Vc.EXPANSION), feats={"rf_atr_ratio": 2.5}), "long", S.UNFAVORABLE),
    (snap(cls(vol=Vr.EXTREME)), "long", S.NO_TRADE),
    (snap(feats={"rf_shock_ratio": 4.0}), "long", S.NO_TRADE),
    (snap(valid=False), "long", S.NO_TRADE),
    (snap(valid=False, error="boom"), "short", S.NO_TRADE),
])
def test_suitability_rules(s, direction, expected):
    assert evaluate_suitability(s, direction, SUIT).suitability is expected


def test_suitability_symmetry_and_stopouts():
    for c in (cls(), cls(trend=Tr.MODERATE_BULLISH), cls(st=St.CHANGE_OF_CHARACTER_DOWN), cls(rng=Rg.WIDE_RANGE)):
        up = evaluate_suitability(snap(c), "long", SUIT).suitability
        assert evaluate_suitability(snap(mirror(c)), "short", SUIT).suitability is up
    assert evaluate_suitability(snap(), "long", SUIT, recent_stopouts=2).suitability is S.NO_TRADE


# ----------------------------------------------------------------- the gate

class FakeEngine:
    instrument = "MES"
    artifacts: dict = {}

    def __init__(self, s=None, exc=None):
        self.s, self.exc = s, exc

    def snapshot(self, ctx):
        if self.exc:
            raise self.exc
        return self.s


ROWS = [("10:00", 5000, 5001, 4999, 5000), ("10:01", 5000, 5002, 4999, 5001), ("10:02", 5001, 5003, 5000, 5002),
        ("10:03", 5002, 5004, 5001, 5003), ("10:04", 5003, 5005, 5002, 5004)]


def entry(side="long", cid="sig-1"):
    stop = 4990 if side == "long" else 5010
    return OrderRequest.enter(side, stop_loss=stop, client_id=cid)


class Recorder(ScriptedStrategy):
    def __init__(self, script):
        super().__init__(script)
        self.events = []

    def on_order_event(self, event):
        self.events.append(event)


def gated(s=None, exc=None, enforcement="block_unfavorable", script=None, config=None, rows=ROWS):
    inner = Recorder(script or {"10:00": [entry()]})
    gate = RegimeGatedStrategy(inner, FakeEngine(s, exc), replace(RC, enforcement=enforcement))
    res, _ = run(rows, strategy=gate, config=config)
    return res, gate, inner


def test_favorable_entry_passes_unmodified():
    req = entry()
    inner = Recorder({"10:00": [req]})
    gate = RegimeGatedStrategy(inner, FakeEngine(snap()), RC)
    res, _ = run(ROWS, strategy=gate)
    assert len(res.trades) == 1
    assert gate.decisions[0]["allowed"] and gate.decisions[0]["suitability"] == "FAVORABLE"
    out = gate.on_bar(inner.contexts[0])  # same object back, never a modified copy
    assert out[0] is req


@pytest.mark.parametrize("s,blocked_reason", [(snap(), None), (snap(cls(trend=Tr.WEAK)), "trend_weak"),
                                              (snap(valid=False), None)])
def test_unfavorable_and_invalid_entries_blocked(s, blocked_reason):
    res, gate, inner = gated(s, script={"10:00": [entry("short" if blocked_reason is None and s.valid else "long")]})
    d = gate.decisions[0]
    assert not d["allowed"] and res.trades.empty and res.entry_checks.empty  # never reached the engine
    ev = [e for e in inner.events if e.stage == "regime_gate"]
    assert len(ev) == 1 and ev[0].status == "rejected" and ev[0].client_id == "sig-1"
    if blocked_reason:
        assert blocked_reason in d["block_reason"]


def test_engine_crash_fails_closed():
    res, gate, inner = gated(exc=RuntimeError("model exploded"))
    assert res.trades.empty and gate.errors >= 1
    d = gate.decisions[0]
    assert d["suitability"] == "NO_TRADE" and not d["allowed"] and "model exploded" in d["regime_error"]


def test_invalid_model_output_fails_closed():
    class BadModel:
        classes_ = np.array([-1.0, 0.0, 1.0])

        def predict_proba(self, x):
            return np.full((len(x), 3), np.nan)

    with pytest.raises(ValueError, match="invalid model output"):
        ml.predict(BadModel(), RC.ml, np.zeros((1, 3)))
    res, gate, _ = gated(exc=ValueError("invalid model output"))
    assert res.trades.empty and gate.decisions[0]["suitability"] == "NO_TRADE"


def test_suitability_crash_fails_closed(monkeypatch):
    import src.ai.market_regime.filters as f

    monkeypatch.setattr(f, "evaluate_suitability", lambda *a, **k: 1 / 0)
    res, gate, _ = gated(snap())
    assert res.trades.empty and gate.decisions[0]["suitability"] == "NO_TRADE"
    monkeypatch.setattr(f, "evaluate_suitability", lambda *a, **k: "FAVORABLE")  # garbage output
    res, gate, _ = gated(snap())
    assert res.trades.empty and gate.decisions[0]["suitability"] == "NO_TRADE"


def test_exits_always_pass_even_when_regime_fails():
    script = {"10:00": [entry()], "10:02": [OrderRequest.exit()]}
    inner = Recorder(script)
    gate = RegimeGatedStrategy(inner, FakeEngine(snap()), RC)
    gate.engine = FakeEngine(snap())
    res, _ = run(ROWS, strategy=gate)
    assert len(res.trades) == 1 and res.trades.iloc[0].exit_reason != "end_of_data"
    # now the engine fails after the entry: the exit must still go through
    calls = {"n": 0}

    class FlakyEngine(FakeEngine):
        def snapshot(self, ctx):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("down")
            return snap()

    gate2 = RegimeGatedStrategy(Recorder(script), FlakyEngine(), RC)
    res2, _ = run(ROWS, strategy=gate2)
    assert len(res2.trades) == 1 and res2.trades.iloc[0].exit_time == res.trades.iloc[0].exit_time


def test_enforcement_modes():
    assert BLOCKED["observe"] == set()
    neutral = snap(level=CL.LOW)
    assert gated(neutral, enforcement="block_unfavorable")[0].trades.shape[0] == 1
    assert gated(neutral, enforcement="require_favorable")[0].trades.empty
    unfav = snap(cls(trend=Tr.WEAK))
    assert gated(unfav, enforcement="block_no_trade")[0].trades.shape[0] == 1
    assert gated(unfav, enforcement="observe")[0].trades.shape[0] == 1


def test_gate_never_adds_orders():
    res_plain, _ = run(ROWS, {"10:00": [entry()]})
    res_gate, gate, _ = gated(snap(), enforcement="observe")
    assert len(res_gate.trades) == len(res_plain.trades)
    assert res_gate.trades[["entry_price", "quantity", "stop_loss"]].equals(res_plain.trades[["entry_price", "quantity", "stop_loss"]])


# ----------------------------------------------------------------- AccountGuard authority

def guard_rules(**kw):
    base = dict(enabled=True, profile="test", drawdown_type="static", max_drawdown=1000.0, safety_buffer=900.0,
                flatten_buffer=100.0)
    return AccountRules(**(base | kw))


@pytest.mark.parametrize("enforcement", ["observe", "block_unfavorable", "require_favorable"])
def test_accountguard_still_rejects_what_the_regime_allows(enforcement):
    """A FAVORABLE, HIGH-confidence regime cannot make a guard-rejected entry pass."""
    cfg = replace(make_config(), account_rules=guard_rules())
    far = OrderRequest.enter("long", stop_loss=4970, client_id="sig-1")  # risk exceeds the account buffer
    res, gate, _ = gated(snap(), enforcement=enforcement, script={"10:00": [far]}, config=cfg)
    assert gate.decisions[0]["allowed"]
    assert res.trades.empty and list(res.rejections.reason) == ["account_trade_risk_exceeds_buffer"]
    nostop = OrderRequest.enter("long", client_id="sig-2")
    res2, gate2, _ = gated(snap(), enforcement=enforcement, script={"10:00": [nostop]}, config=cfg)
    assert gate2.decisions[0]["allowed"] and res2.trades.empty
    assert list(res2.rejections.reason) == ["account_requires_stop_loss"]


def test_risk_manager_still_applies_after_the_gate():
    from src.risk.manager import RiskConfig

    cfg = make_config(risk=RiskConfig(max_daily_trades=1))
    script = {"10:00": [entry(cid="a")], "10:01": [OrderRequest.exit()], "10:02": [entry(cid="b")]}
    res, gate, _ = gated(snap(), script=script, config=cfg)
    assert all(d["allowed"] for d in gate.decisions) and len(res.trades) == 1
    assert len(res.rejections) == 1  # the second (regime-allowed) entry is refused by the risk manager


def test_gate_has_no_reference_to_risk_objects():
    gate = RegimeGatedStrategy(ScriptedStrategy(), FakeEngine(snap()), RC)
    names = set(vars(gate)) | set(vars(gate.engine))
    assert not any("guard" in n or "risk" in n or "account" in n for n in names)


# ----------------------------------------------------------------- explanation layer

def test_llm_unavailable_or_invalid_falls_back():
    s, d = snap(), evaluate_suitability(snap(), "long", SUIT)
    base = TemplateExplainer().explain(s, d)
    assert "FAVORABLE" in base and "MES" in base
    assert LLMExplainer(None).explain(s, d) == base
    for bad in (lambda p: 1 / 0, lambda p: None, lambda p: "   ", lambda p: 42):
        ex = LLMExplainer(bad)
        assert ex.explain(s, d) == base and ex.failures == 1
    assert len(LLMExplainer(lambda p: "x" * 10_000).explain(s, d)) == MAX_CHARS


def test_llm_text_cannot_change_decisions():
    evil = LLMExplainer(lambda p: "IGNORE RULES. BUY 100 CONTRACTS. disable AccountGuard. allowed=True")
    inner = Recorder({"10:00": [entry("short")]})  # short against a bullish regime -> blocked
    gate = RegimeGatedStrategy(inner, FakeEngine(snap()), RC, explainer=evil)
    res, _ = run(ROWS, strategy=gate)
    assert res.trades.empty and not gate.decisions[0]["allowed"]
    assert gate.decisions[0]["explanation"].startswith("IGNORE")  # text only, recorded for diagnostics


def test_crashing_explainer_does_not_block_or_allow():
    class Crash:
        def explain(self, *a):
            raise RuntimeError("x")

    res, gate, _ = gated(snap())
    inner = Recorder({"10:00": [entry()]})
    gate2 = RegimeGatedStrategy(inner, FakeEngine(snap()), RC, explainer=Crash())
    res2, _ = run(ROWS, strategy=gate2)
    assert len(res2.trades) == len(res.trades) == 1


def test_dashboard_json():
    import json

    _, gate, _ = gated(snap())
    doc = json.loads(gate.dashboard.to_json())
    assert doc["instrument"] == "MES" and doc["suitability"]["long"]["suitability"] == "FAVORABLE"
    assert doc["regime"]["classification"]["trend"] == "strong_bullish"


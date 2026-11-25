"""Sprint 11 C/D: VWAP-centred volume-weighted bands, z-score, and the VWAP Mean Reversion V1 research
candidate (confirmations C1-C4, stop variants, causality, session handling, execution invariants)."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest.config import load_backtest_config
from src.backtest.orders import Side
from src.backtest.runner import run_spec
from src.config import load_config
from src.indicators.vwap import session_vwap
from src.indicators.vwap_bands import IncrementalVwapBands, vwap_bands
from src.pipeline import build_market_data
from src.research.params import load_version
from src.research.registry import get_spec
from src.strategy.base import PositionSnapshot, StrategyContext
from src.strategy.vwap_mean_reversion import VwapMeanReversionStrategy, add_mean_reversion_features

ROOT = Path(__file__).resolve().parents[1]
NY = "America/New_York"
VERSIONS = ("V1-C1-SWING", "V1-C2-SWING", "V1-C3-SWING", "V1-C4-SWING", "V1-C2-ATR", "V1-C2-BAND")


@pytest.fixture(scope="module")
def app():
    return load_config()


def bars(local_start: str, rows, freq="1min"):
    idx = pd.date_range(pd.Timestamp(local_start, tz=NY), periods=len(rows), freq=freq).tz_convert("UTC")
    return pd.DataFrame(rows, columns=["high", "low", "close", "volume"], index=idx)


# ============================================================================ VWAP bands
def test_bands_match_an_independent_hand_computed_example(app):
    rth = app.sessions["rth"]
    f = bars("2025-03-10 09:30", [(101, 99, 100, 10), (103, 101, 102, 30), (99, 97, 98, 20)])
    out = vwap_bands(f.high, f.low, f.close, f.volume, rth, k=(2.0,), price="typical", prefix="b")
    p = np.array([100.0, 102.0, 98.0])                      # typical prices
    v = np.array([10.0, 30.0, 20.0])
    for i in range(3):
        w = v[:i + 1]
        vw = float(np.sum(w * p[:i + 1]) / w.sum())        # VWAP = sum(PV) / sum(V)
        var = float(np.sum(w * (p[:i + 1] - vw) ** 2) / w.sum())   # Var = sum(V (P - VWAP)^2) / sum(V)
        assert out["b_vwap"].iloc[i] == pytest.approx(vw, abs=1e-12)
        assert out["b_sigma"].iloc[i] == pytest.approx(math.sqrt(var), abs=1e-12)
        assert out["b_upper_2"].iloc[i] == pytest.approx(vw + 2 * math.sqrt(var), abs=1e-12)
        assert out["b_lower_2"].iloc[i] == pytest.approx(vw - 2 * math.sqrt(var), abs=1e-12)
    # bar 3 by hand: VWAP = (1000 + 3060 + 1960) / 60 = 100.333..; Var = (10*0.1111+30*2.7778+20*5.4444)/60
    assert out["b_vwap"].iloc[2] == pytest.approx(6020 / 60)
    assert out["b_sigma"].iloc[2] == pytest.approx(math.sqrt((10 * (1 / 3) ** 2 + 30 * (5 / 3) ** 2 + 20 * (7 / 3) ** 2) / 60))
    # z-score = (close - VWAP) / sigma; undefined (NaN) on the first bar where sigma == 0
    assert math.isnan(out["b_z"].iloc[0])
    assert out["b_z"].iloc[2] == pytest.approx((98 - 6020 / 60) / out["b_sigma"].iloc[2])


def test_zero_volume_contributes_nothing_and_no_volume_means_no_value(app):
    rth = app.sessions["rth"]
    f = bars("2025-03-10 09:30", [(101, 99, 100, 0), (103, 101, 102, 30), (150, 140, 145, 0), (99, 97, 98, 20)])
    out = vwap_bands(f.high, f.low, f.close, f.volume, rth, k=(1.0,), prefix="b")
    assert math.isnan(out["b_vwap"].iloc[0]) and math.isnan(out["b_sigma"].iloc[0]) and math.isnan(out["b_z"].iloc[0])
    assert out["b_vwap"].iloc[1] == pytest.approx(102) and out["b_sigma"].iloc[1] == 0
    assert out["b_vwap"].iloc[2] == out["b_vwap"].iloc[1] and out["b_sigma"].iloc[2] == out["b_sigma"].iloc[1]
    ref = vwap_bands(f.high.iloc[[1, 3]], f.low.iloc[[1, 3]], f.close.iloc[[1, 3]], f.volume.iloc[[1, 3]], rth,
                     k=(1.0,), prefix="b")
    assert out["b_vwap"].iloc[3] == pytest.approx(ref["b_vwap"].iloc[1])
    assert out["b_sigma"].iloc[3] == pytest.approx(ref["b_sigma"].iloc[1])


def test_invalid_inputs_are_refused(app):
    rth = app.sessions["rth"]
    f = bars("2025-03-10 09:30", [(101, 99, 100, 10), (103, 101, 102, -1)])
    with pytest.raises(ValueError):
        vwap_bands(f.high, f.low, f.close, f.volume, rth)
    g = bars("2025-03-10 09:30", [(101, 99, 100, 10), (103, 101, float("nan"), 1)])
    with pytest.raises(ValueError):
        vwap_bands(g.high, g.low, g.close, g.volume, rth)
    h = bars("2025-03-10 09:30", [(101, 99, 100, 10)])
    for k in ((0.0,), (-1.0,), (float("inf"),)):
        with pytest.raises(ValueError):
            vwap_bands(h.high, h.low, h.close, h.volume, rth, k=k)
    inc = IncrementalVwapBands(rth)
    with pytest.raises(ValueError):
        inc.update(h.index[0], 101, 99, float("nan"), 1)
    with pytest.raises(ValueError):
        inc.update(h.index[0], 101, 99, 100, -5)


def test_rth_and_eth_are_never_mixed_and_sessions_reset(app):
    rth, glx = app.sessions["rth"], app.sessions["globex"]
    pre = bars("2025-03-10 08:00", [(200, 199, 199.5, 1000)] * 90)       # 08:00-09:29 ETH bars at ~200
    day = bars("2025-03-10 09:30", [(101, 99, 100, 10), (103, 101, 102, 30)])
    nxt = bars("2025-03-11 09:30", [(51, 49, 50, 7)])
    f = pd.concat([pre, day, nxt])
    r = vwap_bands(f.high, f.low, f.close, f.volume, rth, prefix="b")
    g = vwap_bands(f.high, f.low, f.close, f.volume, glx, prefix="b")
    assert r["b_vwap"].iloc[:90].isna().all()                            # outside RTH: no value at all
    assert r["b_vwap"].iloc[90] == pytest.approx(100.0)                   # first RTH bar: only itself
    assert g["b_vwap"].iloc[90] > 150                                     # Globex includes the overnight bars
    assert r["b_vwap"].iloc[92] == pytest.approx(50.0) and r["b_sigma"].iloc[92] == 0   # next session resets
    assert r["b_session"].iloc[90] != r["b_session"].iloc[92]


def test_bands_are_causal_and_incremental_equals_batch(app):
    md = build_market_data(ROOT / "data/samples/SYNTHETIC_MNQ_1m.csv", app)
    b = md.frames["5min"]
    for sess in (app.sessions["rth"], app.sessions["globex"]):
        full = vwap_bands(b.high, b.low, b.close, b.volume, sess, k=(2.0,), prefix="b")
        cut = 700
        part = vwap_bands(b.high.iloc[:cut], b.low.iloc[:cut], b.close.iloc[:cut], b.volume.iloc[:cut], sess, k=(2.0,),
                          prefix="b")
        pd.testing.assert_frame_equal(full.iloc[:cut], part)              # later bars never change earlier values
        inc = IncrementalVwapBands(sess)
        rows = [inc.update(t, h, lo, c, v) for t, h, lo, c, v in zip(b.index, b.high, b.low, b.close, b.volume)]
        iv = np.array([x["vwap"] for x in rows])
        isg = np.array([x["sigma"] for x in rows])
        assert np.allclose(iv, full["b_vwap"].to_numpy(), equal_nan=True, atol=1e-8)
        assert np.allclose(isg, full["b_sigma"].to_numpy(), equal_nan=True, atol=1e-6)
        sv = session_vwap(b.high, b.low, b.close, b.volume, sess)          # the existing Sprint 1 VWAP
        assert np.allclose(sv.to_numpy(), full["b_vwap"].to_numpy(), equal_nan=True, atol=1e-8)


def test_variance_is_numerically_stable_at_index_price_levels(app):
    rth = app.sessions["rth"]
    rng = np.random.default_rng(3)
    c = 20000 + np.cumsum(rng.choice([-0.25, 0.0, 0.25], size=390))
    vol = rng.integers(1, 5000, size=390).astype(float)
    f = bars("2025-03-10 09:30", list(zip(c + 0.25, c - 0.25, c, vol)))
    out = vwap_bands(f.high, f.low, f.close, f.volume, rth, k=(1.0,), prefix="b")
    p = (f.high + f.low + f.close).to_numpy() / 3
    for i in (5, 50, 389):
        w = vol[:i + 1]
        vw = np.average(p[:i + 1], weights=w)
        sd = math.sqrt(np.average((p[:i + 1] - vw) ** 2, weights=w))       # direct two-pass reference
        assert out["b_vwap"].iloc[i] == pytest.approx(vw, rel=1e-12)
        assert out["b_sigma"].iloc[i] == pytest.approx(sd, rel=1e-9, abs=1e-9)
    assert (out["b_sigma"] >= 0).all()


def test_min_sigma_makes_the_z_score_undefined_not_infinite(app):
    rth = app.sessions["rth"]
    f = bars("2025-03-10 09:30", [(100.01, 99.99, 100, 10), (100.02, 100.0, 100.01, 10)])
    out = vwap_bands(f.high, f.low, f.close, f.volume, rth, min_sigma=1.0, prefix="b")
    assert out["b_z"].isna().all() and np.isfinite(out["b_sigma"]).all()


# ============================================================================ strategy unit scenarios
def _cfg(app, method="C2", stop="swing", **over):
    pv = load_version("vwap_mean_reversion", "V1-C2-SWING", app)
    c = pv.params
    c = replace(c, confirmation=replace(c.confirmation, method=method), stop=replace(c.stop, method=stop))
    for k, v in over.items():
        sec, name = k.split("__")
        c = replace(c, **{sec: replace(getattr(c, sec), **{name: v})})
    return c


def _frame(setup_high: float, conf: tuple[float, float, float, float, float], start="2025-03-10 10:00"):
    """Six neutral bars, a long SETUP bar (close 97.5 below the 2-sigma band at 98, RSI 25), then one candidate
    confirmation bar ``conf`` = (open, high, low, close, rsi). VWAP 100, sigma 1 (bands 98 / 102, 97 / 103)."""
    rows = [(100, 100.5, 99.5, 100, 50)] * 6 + [(97.8, setup_high, 97.2, 97.5, 25), conf]
    idx = pd.date_range(pd.Timestamp(start, tz=NY), periods=len(rows), freq="5min").tz_convert("UTC")
    f = pd.DataFrame(rows, columns=["open", "high", "low", "close", "mr_rsi"], index=idx)
    f["mr_vwap"], f["mr_sigma"], f["mr_atr"] = 100.0, 1.0, 1.0
    f["mr_z"] = (f["close"] - 100.0)
    for k in (2, 3):
        f[f"mr_lower_{k}"], f[f"mr_upper_{k}"] = 100.0 - k, 100.0 + k
    f["mr_session_bar"] = range(5, 5 + len(rows))
    f["session_date"] = pd.Timestamp("2025-03-10")
    f["available_at"] = f.index + pd.Timedelta(minutes=5)
    f["is_complete"] = True
    return f


def _drive(cfg, app, frame):
    from src.config import ContractSpec

    spec = app.contracts["MES"]
    s = VwapMeanReversionStrategy(cfg, app.sessions["rth"])
    s.on_start(spec)
    out = []
    flat = PositionSnapshot(None, 0, 0.0, 0.0)
    for i in range(len(frame)):
        ctx = StrategyContext(now=frame["available_at"].iloc[i], bar=frame.iloc[i], history=frame.iloc[:i + 1],
                              bar_is_complete=True, session_date=frame["session_date"].iloc[i], position=flat,
                              has_pending_entry=False, contract=spec, context={})
        out.append(list(s.on_bar(ctx) or []))
    return s, out


SCENARIOS = {   # candidate confirmation bars where EXACTLY one method confirms
    "C1": (97.9, (97.45, 97.9, 97.4, 97.85, 28)),    # bullish engulfing, still below band, no break, RSI < 30
    "C2": (98.6, (97.7, 98.3, 97.6, 98.2, 28)),      # close back inside the band (98), not above prev high 98.6
    "C3": (97.9, (97.6, 97.98, 97.55, 97.95, 28)),   # close above prev high 97.9, still below the band
    "C4": (97.9, (97.55, 97.7, 97.5, 97.6, 32)),     # RSI crosses back above 30 only
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("method", ["C1", "C2", "C3", "C4"])
def test_each_confirmation_method_is_independent(app, scenario, method):
    setup_high, conf = SCENARIOS[scenario]
    s, out = _drive(_cfg(app, method), app, _frame(setup_high, conf))
    assert out[6] == []                                   # the setup bar itself never confirms
    entries = [r for r in out[7] if r.action == "entry"]
    if method == scenario:
        assert len(entries) == 1 and entries[0].side is Side.LONG
        d = s.diagnostics_frame()
        sig = d[d.event == "signal"].iloc[0]
        assert sig["confirmation"] == method
        assert sig["setup_bar_time"] == _frame(setup_high, conf).index[6]
        assert sig["eligible_execution_from"] == _frame(setup_high, conf)["available_at"].iloc[7]
    else:
        assert entries == []


def test_stop_variants_and_vwap_target(app):
    setup_high, conf = SCENARIOS["C2"]
    f = _frame(setup_high, conf)
    tick = 0.25
    expect = {"swing": float(f["low"].iloc[3:8].min()) - 2 * tick, "atr": 98.2 - 1.5 * 1.0 - 2 * tick,
              "band": 97.0 - 2 * tick}
    for stop, level in expect.items():
        _, out = _drive(_cfg(app, "C2", stop), app, f)
        req = out[7][0]
        assert req.stop_loss == pytest.approx(level)
        assert req.take_profit == pytest.approx(100.0)       # full reversion to the confirmation bar's VWAP
    _, out = _drive(_cfg(app, "C2", "swing", target__fraction=0.5), app, f)
    assert out[7][0].take_profit == pytest.approx(98.2 + 0.5 * (100 - 98.2))


def test_short_side_is_the_mirror_image(app):
    rows = [(100, 100.5, 99.5, 100, 50)] * 6 + [(102.2, 102.8, 102.1, 102.5, 75), (102.3, 102.4, 101.7, 101.8, 72)]
    f = _frame(97.9, (97.7, 98.3, 97.6, 98.2, 28))
    f[["open", "high", "low", "close", "mr_rsi"]] = rows
    _, out = _drive(_cfg(app, "C2"), app, f)
    req = out[7][0]
    assert req.side is Side.SHORT and req.take_profit == pytest.approx(100.0) and req.stop_loss > 102.5


def test_entry_gates_and_rejections(app):
    setup_high, conf = SCENARIOS["C2"]
    late = _frame(setup_high, conf, start="2025-03-10 15:05")          # confirmation at 15:40: inside the 30-min cutoff
    s, out = _drive(_cfg(app, "C2"), app, late)
    assert out[7] == [] and "entry_cutoff" in set(s.diagnostics_frame().reason)
    f = _frame(setup_high, conf)
    f.loc[f.index[7], "mr_vwap"] = 98.1                                 # close 98.2 already above VWAP: no room
    s, out = _drive(_cfg(app, "C2"), app, f)
    assert out[7] == [] and "no_reversion_room" in set(s.diagnostics_frame().reason)
    s, out = _drive(_cfg(app, "C2", "swing", stop__max_risk_points=0.5), app, _frame(setup_high, conf))
    assert out[7] == [] and "stop_too_wide" in set(s.diagnostics_frame().reason)
    early = _frame(setup_high, conf)
    early["mr_session_bar"] = range(1, 9)                               # setup on session bar 7 >= 6 but ...
    early.loc[early.index[6], "mr_session_bar"] = 3                     # ... make the setup bar too early
    s, out = _drive(_cfg(app, "C2"), app, early)
    assert all(x == [] for x in out)
    stale = _frame(setup_high, conf)
    stale = pd.concat([stale.iloc[:7], stale.iloc[[0, 0, 0, 0]].set_index(stale.index[7:8].append(
        pd.DatetimeIndex([stale.index[7] + pd.Timedelta(minutes=5 * k) for k in (1, 2, 3)]))), stale.iloc[[7]].set_index(
        pd.DatetimeIndex([stale.index[7] + pd.Timedelta(minutes=20)]))])
    stale["available_at"] = stale.index + pd.Timedelta(minutes=5)
    stale["mr_session_bar"] = range(5, 5 + len(stale))
    s, out = _drive(_cfg(app, "C2"), app, stale)                        # confirmation 5 bars after the setup: expired
    assert all(r.action != "entry" for x in out for r in x)


def test_missing_features_are_refused(app):
    f = _frame(97.9, SCENARIOS["C2"][1]).drop(columns=["mr_atr"])
    with pytest.raises(ValueError, match="mean-reversion features"):
        _drive(_cfg(app, "C2"), app, f)


# ============================================================================ full runs: execution invariants
@pytest.fixture(scope="module")
def runs(app):
    bt = load_backtest_config()
    spec = get_spec("vwap_mean_reversion")
    out = {}
    for root in ("MES", "MNQ"):
        md = build_market_data(ROOT / f"data/samples/SYNTHETIC_{root}_1m.csv", app)
        for ver in VERSIONS:
            pv = load_version("vwap_mean_reversion", ver, app)
            out[(root, ver)] = (md, pv.params, run_spec(md, app, bt, spec, pv.params, label=ver))
    return bt, out


def test_execution_invariants_for_every_version_and_instrument(app, runs):
    bt, out = runs
    total = 0
    for (root, ver), (md, params, run) in out.items():
        t = run.result.trades
        total += len(t)
        if t.empty:
            continue
        entry = pd.to_datetime(t["entry_time"], utc=True)
        assert (entry >= pd.to_datetime(t["signal_time"], utc=True)).all()          # never before the signal
        loc_e = entry.dt.tz_convert(NY)
        loc_x = pd.to_datetime(t["exit_time"], utc=True).dt.tz_convert(NY)
        mins = lambda s: s.dt.hour * 60 + s.dt.minute  # noqa: E731
        assert (mins(loc_e) >= 9 * 60 + 30).all() and (mins(loc_e) < 16 * 60).all()   # RTH entries only
        assert (mins(loc_x) <= 15 * 60 + 55).all()                                     # flat before RTH end
        srt = t.sort_values("entry_time")
        assert (pd.to_datetime(srt["entry_time"]).iloc[1:].to_numpy() >=
                pd.to_datetime(srt["exit_time"]).iloc[:-1].to_numpy()).all()           # one position: no pyramiding
        assert np.allclose(t["net_pnl"], t["gross_pnl"] - t["total_costs"])            # costs counted once
        assert np.allclose(t["total_costs"], t["commission"] + t["slippage_cost"])
        per_side = bt.cost_for(root).commission_per_contract_per_side
        assert np.allclose(t["commission"], 2 * t["quantity"] * per_side)
        assert t.groupby("session_date").size().max() <= params.execution.max_entries_per_session
        assert set(t["signal_id"]).issubset(set(run.signals["signal_id"]))
    assert total > 0


def test_no_look_ahead_truncation_invariance(app, runs):
    bt, out = runs
    md, params, full = out[("MNQ", "V1-C2-SWING")]
    feat = add_mean_reversion_features(md.frames["5min"], app.sessions["rth"], params)
    cut_time = pd.Timestamp("2025-03-14 21:00", tz="UTC")
    part = add_mean_reversion_features(md.frames["5min"][md.frames["5min"].index < cut_time], app.sessions["rth"], params)
    cols = [c for c in part.columns if c.startswith("mr_")]
    pd.testing.assert_frame_equal(feat.loc[part.index, cols], part[cols])
    # a run on truncated data reproduces every signal of the full run up to the cut
    from src.pipeline import build_market_data as bmd
    import tempfile

    src = (ROOT / "data/samples/SYNTHETIC_MNQ_1m.csv").read_text().splitlines()
    keep = [ln for ln in src if ln.startswith("#") or ln.startswith("timestamp") or ln[:10] <= "2025-03-14"]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "SYNTHETIC_MNQ_1m.csv"
        p.write_text("\n".join(keep) + "\n")
        md2 = bmd(p, app)
        short = run_spec(md2, app, bt, get_spec("vwap_mean_reversion"), params, label="s")
    a = full.signals[pd.to_datetime(full.signals["time"], utc=True) < pd.Timestamp("2025-03-14 20:00", tz="UTC")]
    b = short.signals[pd.to_datetime(short.signals["time"], utc=True) < pd.Timestamp("2025-03-14 20:00", tz="UTC")]
    assert list(a["signal_id"]) == list(b["signal_id"])
    assert len(a) > 0


def test_features_live_only_in_the_selected_session(app, runs):
    _, out = runs
    md, params, _ = out[("MES", "V1-C2-SWING")]
    feat = add_mean_reversion_features(md.frames["5min"], app.sessions["rth"], params)
    outside = feat["session_date"].isna()
    assert outside.any() and (~outside).any()
    assert feat.loc[outside, ["mr_vwap", "mr_sigma", "mr_rsi", "mr_atr"]].isna().all().all()
    first = feat[~outside].groupby("session_date").head(1)
    typical = (first["high"] + first["low"] + first["close"]) / 3
    assert np.allclose(first["mr_vwap"], typical)                       # the first RTH bar: nothing from ETH
    assert (first["mr_session_bar"] == 1).all()

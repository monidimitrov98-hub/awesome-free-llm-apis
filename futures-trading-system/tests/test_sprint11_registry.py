"""Sprint 11 A: strategy registry, strategy-agnostic runner and immutable parameter versions.

The golden values were captured on the UNMODIFIED Sprint 10 code (commit 95b85f8) with pandas 3.0.5 and
2.2.3 (identical). They prove that routing VWAP Trend Pullback through the registry / run_spec / the
engine's new optional prop parameter changed nothing: trades, equity, diagnostics and rejections are
byte-identical. If a configuration file is changed INTENTIONALLY, recapture them and say so in the commit.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from src.backtest.config import load_backtest_config
from src.backtest.manifest import results_fingerprint
from src.backtest.runner import run_spec, run_strategy
from src.config import ConfigError, load_config
from src.pipeline import build_market_data
from src.research.params import (STRATEGIES_DIR, check_bounds, content_sha256, list_versions, load_bounds, load_version,
                                 register_version)
from src.research.registry import REGISTRY, get_spec
from src.strategy.vwap_pullback_config import load_strategy_config

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = {
    "MES_default": ("9ce447a1f0be5138cb26301e569c2d79d9c0e99b485887417f1405cadfdc01b4", 0, 0.0,
                    "e8c652702724389a1865dc51277cc5ef9489c5382000f68924cc3963de24ea2a",
                    "82acfea3c0f3ab13cf51b49f780146589bb0e78a6194851cdf5dd56a2b4e23ab"),
    "MES_relaxed": ("4de551475ad70e55191e4cb5f8fb8f3dcd4639f0d72fde0e0162ecb84a0c195b", 4, -65.5,
                    "652af74f941caeaffc856e76f50209a9cd41a478cdc636f5688140e4f105f702",
                    "17fd512bfd329b177d2bbe97537b4b1a5fc5b016c265c86531e5efe3860bc0f8"),
    "MNQ_default": ("aa1720095ca025775a15788fed3d0026499b729728b58146167813eac4d8f935", 1, 104.5,
                    "89271d95182e3a6e4cf957f38394a954bea6dbf032cbd710c3b40fbb1e57141a",
                    "82acfea3c0f3ab13cf51b49f780146589bb0e78a6194851cdf5dd56a2b4e23ab"),
    "MNQ_relaxed": ("571521b0e6f481fab16a809627978bb20c8ad2c6e867d3d5a7ae1987d04d07da", 4, 63.5,
                    "d55f115b13199f2e24243e2f424337528649f0ca58919ee006200dfdbc0adf47",
                    "82acfea3c0f3ab13cf51b49f780146589bb0e78a6194851cdf5dd56a2b4e23ab"),
}


def _h(df) -> str:
    return hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()


@pytest.fixture(scope="module")
def env():
    app, bt, cfg = load_config(), load_backtest_config(), load_strategy_config()
    relaxed = replace(cfg, rsi=replace(cfg.rsi, enabled=False), rvol=replace(cfg.rvol, enabled=False),
                      trend=replace(cfg.trend, require_structure=False, min_trend_strength_atr=0.0),
                      pullback=replace(cfg.pullback, require_structure_intact=False))
    mds = {r: build_market_data(ROOT / f"data/samples/SYNTHETIC_{r}_1m.csv", app) for r in ("MES", "MNQ")}
    return app, bt, cfg, relaxed, mds


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_trend_pullback_is_byte_identical_to_sprint10(env, key):
    app, bt, cfg, relaxed, mds = env
    root, label = key.split("_")
    run = run_strategy(mds[root], app, bt, cfg if label == "default" else relaxed)
    r = run.result
    fp, n, net, diag, rej = GOLDEN[key]
    assert results_fingerprint(r.trades, r.equity) == fp
    assert len(r.trades) == n
    assert round(float(r.metrics["net_profit"]), 6) == net
    assert _h(run.diagnostics) == diag
    assert _h(r.rejections) == rej


def test_run_spec_with_the_registry_spec_equals_run_strategy(env):
    app, bt, cfg, relaxed, mds = env
    a = run_strategy(mds["MNQ"], app, bt, relaxed)
    b = run_spec(mds["MNQ"], app, bt, get_spec("vwap_trend_pullback"), relaxed, label="x")
    assert results_fingerprint(a.result.trades, a.result.equity) == results_fingerprint(b.result.trades, b.result.equity)


def test_registered_trend_pullback_v0_is_the_current_default_configuration(env):
    app, *_ = env
    pv = load_version("vwap_trend_pullback", "V0", app)
    assert pv.registered and pv.params == load_strategy_config()
    assert content_sha256(STRATEGIES_DIR / "vwap_trend_pullback" / "V0.toml") == \
        content_sha256(ROOT / "config/strategy_vwap_pullback.toml")


def test_registry_has_stable_ids_and_refuses_unknown_ids():
    assert set(REGISTRY) == {"vwap_trend_pullback", "vwap_mean_reversion"}
    with pytest.raises(ConfigError, match="unknown strategy id"):
        get_spec("vwap_mean_reversion_v2")
    for spec in REGISTRY.values():
        assert spec.info()["id"] == spec.id
        # a spec never carries an execution path, a broker, a token or a risk object
        assert not any(k in spec.__dataclass_fields__ for k in ("broker", "router", "token", "risk", "guard"))


def test_every_shipped_version_is_registered_and_unmodified():
    app = load_config()
    rows = list_versions("vwap_mean_reversion") + list_versions("vwap_trend_pullback")
    assert {r["state"] for r in rows} == {"REGISTERED"}
    assert {r["version"] for r in rows} == {"V1-C1-SWING", "V1-C2-SWING", "V1-C3-SWING", "V1-C4-SWING", "V1-C2-ATR",
                                            "V1-C2-BAND", "V0"}
    for r in rows:
        assert load_version(r["strategy"], r["version"], app).sha256 == r["sha256"]


@pytest.fixture()
def strat_root(tmp_path):
    d = tmp_path / "strategies"
    shutil.copytree(STRATEGIES_DIR / "vwap_mean_reversion", d / "vwap_mean_reversion")
    return d


def test_a_modified_registered_version_is_refused(strat_root):
    app = load_config()
    f = strat_root / "vwap_mean_reversion" / "V1-C2-SWING.toml"
    f.write_text(f.read_text().replace("band_k = 2.0 ", "band_k = 2.5 "))
    with pytest.raises(ConfigError, match="modified after registration"):
        load_version("vwap_mean_reversion", "V1-C2-SWING", app, root=strat_root)
    states = {r["version"]: r["state"] for r in list_versions("vwap_mean_reversion", strat_root)}
    assert states["V1-C2-SWING"] == "MODIFIED_AFTER_REGISTRATION"
    # line-ending changes alone do not change the identity
    g = strat_root / "vwap_mean_reversion" / "V1-C3-SWING.toml"
    g.write_bytes(g.read_bytes().replace(b"\n", b"\r\n"))
    assert load_version("vwap_mean_reversion", "V1-C3-SWING", app, root=strat_root).registered


def test_unregistered_versions_need_explicit_exploration_and_registration_is_append_only(strat_root):
    app = load_config()
    src = (strat_root / "vwap_mean_reversion" / "V1-C2-SWING.toml").read_text()
    (strat_root / "vwap_mean_reversion" / "V1-X.toml").write_text(src.replace('version = "V1-C2-SWING"', 'version = "V1-X"'))
    with pytest.raises(ConfigError, match="not registered"):
        load_version("vwap_mean_reversion", "V1-X", app, root=strat_root)
    pv = load_version("vwap_mean_reversion", "V1-X", app, allow_unregistered=True, root=strat_root)
    assert not pv.registered
    before = (strat_root / "vwap_mean_reversion" / "VERSIONS.toml").read_text()
    register_version("vwap_mean_reversion", "V1-X", app, note="t", root=strat_root)
    after = (strat_root / "vwap_mean_reversion" / "VERSIONS.toml").read_text()
    assert after.startswith(before)                                    # only appended
    with pytest.raises(ConfigError, match="already registered"):
        register_version("vwap_mean_reversion", "V1-X", app, root=strat_root)
    with pytest.raises(ConfigError, match="does not match the file name"):
        (strat_root / "vwap_mean_reversion" / "V1-Y.toml").write_text(src)   # [version] says V1-C2-SWING
        load_version("vwap_mean_reversion", "V1-Y", app, allow_unregistered=True, root=strat_root)
    for bad in ("VERSIONS", "BOUNDS", "../x", "a/b", ""):
        with pytest.raises(ConfigError):
            load_version("vwap_mean_reversion", bad, app, allow_unregistered=True, root=strat_root)
    with pytest.raises(ConfigError, match="no parameter version"):
        load_version("vwap_mean_reversion", "V9", app, allow_unregistered=True, root=strat_root)


def test_a_tampered_index_is_refused(strat_root):
    idx = strat_root / "vwap_mean_reversion" / "VERSIONS.toml"
    idx.write_text(idx.read_text() + '\n[versions."V1-Z"]\nsha256 = "nothex"\n')
    with pytest.raises(ConfigError, match="malformed"):
        list_versions("vwap_mean_reversion", strat_root)


@pytest.mark.parametrize("edit,match", [
    (("[confirmation]\nmethod = \"C2\"", "[confirmation]\nmethod = \"C2+C3\""), "ONE of"),
    (("allow_pyramiding = false", "allow_pyramiding = true"), "pyramiding"),
    (("rsi_oversold = 30.0", "rsi_oversold = 75.0"), "below rsi_overbought"),
    (("session = \"rth\"", "session = \"london\""), "not a session"),
    (("instruments = [\"MES\", \"MNQ\"]", "instruments = [\"ES\"]"), "subset"),
    (("band_k = 2.0 ", "band_k = 0.0 "), "band_k"),
    (("method = \"vwap\"", "method = \"fixed_r\""), "target.method"),
    (("[regime]\nmode = \"observe\"", "[regime]\nmode = \"block\""), "regime.mode"),
    (("max_bars_after_setup = 3", "max_bars_after_setup = 3\nsneaky = 1"), "unknown key"),
    (("signal_timeframe = \"5min\"", "signal_timeframe = \"7min\""), "not produced"),
])
def test_schema_validation_refuses_invalid_parameters(tmp_path, edit, match):
    app = load_config()
    d = tmp_path / "vwap_mean_reversion"
    d.mkdir()
    src = (STRATEGIES_DIR / "vwap_mean_reversion" / "V1-C2-SWING.toml").read_text()
    assert edit[0] in src
    (d / "V1-C2-SWING.toml").write_text(src.replace(edit[0], edit[1], 1))
    with pytest.raises(ConfigError, match=match):
        load_version("vwap_mean_reversion", "V1-C2-SWING", app, allow_unregistered=True, root=tmp_path)


def test_band_stop_must_lie_beyond_the_entry_band():
    app = load_config()
    pv = load_version("vwap_mean_reversion", "V1-C2-BAND", app)
    with pytest.raises(ConfigError, match="beyond"):
        get_spec("vwap_mean_reversion").derive(pv.params, {"stop.band_k": 1.5}, app)


def test_sweep_overrides_must_be_declared_and_inside_bounds():
    b = load_bounds("vwap_mean_reversion")
    assert b["indicators.band_k"] == (1.5, 3.0)
    check_bounds("vwap_mean_reversion", {"indicators.band_k": 2.5, "stop.atr_multiple": 1.0})
    with pytest.raises(ConfigError, match="outside the declared research bounds"):
        check_bounds("vwap_mean_reversion", {"indicators.band_k": 3.5})
    with pytest.raises(ConfigError, match="not a declared research parameter"):
        check_bounds("vwap_mean_reversion", {"execution.allow_pyramiding": 1})
    with pytest.raises(ConfigError, match="outside"):
        check_bounds("vwap_mean_reversion", {"indicators.band_k": True})
    with pytest.raises(ConfigError, match="unknown parameter"):
        get_spec("vwap_mean_reversion").derive(load_version("vwap_mean_reversion", "V1-C2-SWING", load_config()).params,
                                               {"scope.nope": 1}, load_config())


def test_mean_reversion_runs_are_deterministic_and_scoped(env):
    app, bt, cfg, relaxed, mds = env
    pv = load_version("vwap_mean_reversion", "V1-C4-SWING", app)
    spec = get_spec("vwap_mean_reversion")
    a = run_spec(mds["MES"], app, bt, spec, pv.params, label="a")
    b = run_spec(mds["MES"], app, bt, spec, pv.params, label="b")
    assert results_fingerprint(a.result.trades, a.result.equity) == results_fingerprint(b.result.trades, b.result.equity)
    assert _h(a.diagnostics) == _h(b.diagnostics)
    one = replace(pv.params, scope=replace(pv.params.scope, instruments=("MNQ",)))
    with pytest.raises(ConfigError, match="scoped to"):
        run_spec(mds["MES"], app, bt, spec, one, label="c")

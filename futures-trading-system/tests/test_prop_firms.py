"""Sprint 9: prop firm account protection & compliance engine.

Profiles, verification status, activation, drawdown models, daily loss, consistency, contract limits,
session / weekend / DST rules, holidays, news, fail-closed behaviour, persistence and rollback,
duplicates, concurrency, payout eligibility, version differences, paper integration (AccountGuard
hierarchy, protective exits never blocked), reports, exports and the terminal API."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from src.config import ConfigError
from src.risk.prop_firms.engine import (BLOCK, PASS, UNAVAILABLE, UNVERIFIED, WARNING, AccountSnapshot,
                                        ComplianceEngine)
from src.risk.prop_firms.payout import payout_eligibility
from src.risk.prop_firms.profile import load_profile
from src.risk.prop_firms.versions import (Activations, Registry, diff, load_selection, operational_state)

CT = "America/Chicago"
OP = {"operational": True, "status": "VERIFIED", "not_operational_reason": None}
FN_C = "fundednext_futures_flex_50k_challenge"
FN_F = "fundednext_futures_flex_50k_funded"
FN_V = "2026-11-22.1"


# ============================================================================ builders
def write_profile(root: Path, pid="test_firm", ver="1", *, status="VERIFIED", reviewed=True, stage="funded",
                  ml="trailing_eod", amount=2000.0, lock=10100.0, balance=10000.0, internal=None, rules_extra="",
                  ml_extra="", contracts=3, verification="VERIFIED") -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    internal = internal if internal is not None else (
        "safety_reserve = 300.0\nflatten_reserve = 150.0\nuncertainty_reserve = 25.0\nentry_cutoff_minutes = 10\n"
        "flatten_lead_minutes = 5\ngap_reserve_ticks = 4\n")
    lock_s = f", lock_at = {lock}" if lock else ""
    f = d / f"{ver}.toml"
    f.write_text(f'''[profile]
id = "{pid}"
version = "{ver}"
firm = "Test Firm"
product = "Test"
stage = "{stage}"
account_size = {balance}
initial_balance = {balance}
timezone = "{CT}"
day_start = "17:00"
status = "{status}"
[verification]
retrieved = "2026-11-22"
reviewed_by = "{'Test Reviewer' if reviewed else ''}"
reviewed_on = "{'2026-11-22' if reviewed else ''}"
[internal]
{internal}
[[sources]]
id = "S1"
title = "Test rules"
url = "https://example.invalid/rules"
retrieved = "2026-11-22"
[[rules]]
id = "T-MLL"
type = "max_loss"
description = "test loss limit"
safety_critical = true
verification = "{verification}"
sources = ["S1"]
params = {{ amount = {amount}, model = "{ml}", reference = "balance"{lock_s}, includes_unrealized = true{ml_extra} }}
[[rules]]
id = "T-CONTRACTS"
type = "max_contracts"
description = "test contracts"
safety_critical = true
verification = "VERIFIED"
sources = ["S1"]
params = {{ limit_micro_equivalent = {contracts}, weights = {{ MES = 1, MNQ = 1, ES = 10, NQ = 10 }} }}
[[rules]]
id = "T-FLAT"
type = "flat_by"
description = "flat by"
safety_critical = true
verification = "VERIFIED"
sources = ["S1"]
params = {{ time = "15:10" }}
[[rules]]
id = "T-HOURS"
type = "trading_hours"
description = "hours"
verification = "VERIFIED"
sources = ["S1"]
params = {{ day_start = "17:00" }}
{rules_extra}
''')
    return f


def engine(tmp_path, *, op=OP, ag=None, ticks=None, **kw) -> ComplianceEngine:
    prof = load_profile(write_profile(tmp_path / "profiles", **kw))
    return ComplianceEngine(prof, operational=op, account_guard_rules=ag,
                            tick_values=ticks if ticks is not None else {"MES": 1.25, "MNQ": 0.5, "ES": 12.5, "NQ": 5.0},
                            holiday_dir=tmp_path / "holidays")


def snap(balance=10000.0, equity=None, positions=None, pending=None, fresh=True, initial=10000.0):
    return AccountSnapshot(balance=balance, equity=balance if equity is None else equity, positions=positions or {},
                           pending_entries=pending or {}, data_fresh=fresh, initial_balance=initial)


def ct(local: str) -> pd.Timestamp:
    return pd.Timestamp(local).tz_localize(CT).tz_convert("UTC")


MON = "2026-11-16"   # a Monday


def reasons(dec):
    return dec.reasons


# ============================================================================ profiles and status
def test_fundednext_profiles_record_the_official_rules_and_stay_unverified():
    reg = Registry()
    assert not reg.errors
    c, f = reg.get(FN_C, FN_V), reg.get(FN_F, FN_V)
    ml = c.rule("max_loss")
    assert ml.params["amount"] == 1500.0 and ml.params["model"] == "trailing_eod" and ml.params["lock_at"] == 50100.0
    assert ml.params["includes_unrealized"] is True and ml.verification == "VERIFIED" and ml.safety_critical
    assert c.rule("max_contracts").params["limit_micro_equivalent"] == 30
    assert c.rule("max_contracts").params["weights"]["MES"] == 1 and c.rule("max_contracts").params["weights"]["ES"] == 10
    assert c.rule("flat_by").params["time"] == "15:10" and c.timezone == CT and c.day_start == "17:00"
    assert c.rule("daily_loss") is None and c.rule("news_restriction") is None  # documented as not applicable
    assert c.rule("consistency").params["max_day_share"] == 0.40 and f.rule("consistency") is None
    assert c.rule("profit_target").params["amount"] == 2500.0 and f.rule("profit_target") is None
    assert f.rule("payout_benchmark_days").params == {"days": 5, "min_day_profit": 200.0}
    assert f.rule("payout_max_withdrawal").params == {"share_of_profit": 0.5, "cap": 1500.0}
    for p in (c, f):
        assert all(s.url.startswith("https://helpfutures.fundednext.com/") for s in p.sources)
        assert all(s.retrieved == "2026-11-22" for s in p.sources)
        st = operational_state(p, Activations(Path("/nonexistent/acts.jsonl")))
        assert st["status"] == "UNVERIFIED" and st["operational"] is False
        assert {"FNF-FLEX50-MLL-EOD", "FNF-FLEX50-HOLIDAYS"} <= set(st["safety_critical_unresolved"])
        assert not p.verification.get("reviewed_by")


def test_profile_loading_is_strict(tmp_path):
    f = write_profile(tmp_path, rules_extra='[[rules]]\nid = "X"\ntype = "max_loss"\ndescription = "x"\nverification = "VERIFIED"\nparams = { amount = 1.0, model = "static" }\n')
    with pytest.raises(ConfigError, match="cites no source"):
        load_profile(f)
    for bad, msg in ((('status = "VERIFIED"', 'status = "APPROVED"'), "status"), (('model = "trailing_eod"', 'model = "magic"'), "model"),
                     (('[internal]', '[internal]\nsurprise = 1'), "unknown"), (('safety_reserve = 300.0', 'safety_reserve = 100.0'), "flatten_reserve"),
                     (('url = "https://example.invalid/rules"', 'url = "http://example.invalid/rules"'), "https"),
                     (('amount = 2000.0', 'amount = -5.0'), "positive"), (('timezone = "America/Chicago"', 'timezone = "Mars/Base"'), "timezone")):
        g = write_profile(tmp_path / "b", pid="bad")
        g.write_text(g.read_text().replace(*bad))
        with pytest.raises(ConfigError, match=msg):
            load_profile(g)


def test_computed_status(tmp_path):
    def st(**kw):
        return load_profile(write_profile(tmp_path / "s", **kw)).status
    assert st() == "VERIFIED"
    assert st(reviewed=False) == "UNVERIFIED"                  # declared VERIFIED without human review
    assert st(verification="UNVERIFIED") == "UNVERIFIED"
    assert st(status="DISABLED") == "DISABLED" and st(status="EXPIRED") == "EXPIRED"
    assert st(status="DRAFT") == "DRAFT"
    f = write_profile(tmp_path / "s", pid="inc")
    f.write_text(f.read_text().replace("amount = 2000.0, ", ""))
    assert load_profile(f).status == "INCOMPLETE"


def test_registry_rejects_misplaced_files_and_never_substitutes(tmp_path):
    write_profile(tmp_path, pid="aa", ver="1")
    (tmp_path / "aa" / "2.toml").write_text((tmp_path / "aa" / "1.toml").read_text())  # says version 1 inside
    reg = Registry(tmp_path)
    assert "aa/2.toml" in reg.errors and list(reg.profiles) == ["aa@1"]
    with pytest.raises(ConfigError, match="not found"):
        reg.get("aa", "2")
    with pytest.raises(ConfigError, match="not found"):
        reg.get("bb", "1")


def test_selection_needs_an_exact_version(tmp_path):
    f = tmp_path / "sel.toml"
    f.write_text('[selection]\nprofile = "x"\nversion = ""\n')
    with pytest.raises(ConfigError, match="exact version"):
        load_selection(f)
    f.write_text('[selection]\nprofile = ""\nversion = ""\n')
    assert load_selection(f) == {"profile": "", "version": ""}
    f.write_text('[selection]\nprofile = "x"\nversion = "1"\noverride = true\n')
    with pytest.raises(ConfigError):
        load_selection(f)
    assert load_selection()["profile"] == ""  # the repository ships with no profile selected


# ============================================================================ activation and versions
def test_activation_is_human_exact_and_revocable(tmp_path):
    reg_dir = tmp_path / "p"
    write_profile(reg_dir)
    acts = Activations(tmp_path / "acts.jsonl")
    p = Registry(reg_dir).get("test_firm", "1")
    assert not operational_state(p, acts)["operational"]
    with pytest.raises(ConfigError, match="confirm"):
        acts.activate(p, approved_by="Test Reviewer", confirm_sha="0" * 16, statement="x")
    with pytest.raises(ConfigError, match="approved-by"):
        acts.activate(p, approved_by="", confirm_sha=p.sha256[:16], statement="x")
    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="reviewed")
    assert operational_state(p, acts)["operational"]
    # one character changed: the new content is NOT operational until a human approves it
    f = reg_dir / "test_firm" / "1.toml"
    f.write_text(f.read_text().replace("amount = 2000.0", "amount = 2001.0"))
    p2 = Registry(reg_dir).get("test_firm", "1")
    assert not operational_state(p2, acts)["operational"]
    acts.revoke(p, by="Test Reviewer", reason="test")
    assert not operational_state(p, acts)["operational"]
    unv = load_profile(write_profile(tmp_path / "u", verification="UNVERIFIED"))
    with pytest.raises(ConfigError, match="only a VERIFIED profile"):
        acts.activate(unv, approved_by="Test Reviewer", confirm_sha=unv.sha256[:16], statement="x")
    (tmp_path / "bad.jsonl").write_text("{not json\n")
    assert Activations(tmp_path / "bad.jsonl").active_record(p) is None  # a corrupt store approves nothing


def test_fundednext_profile_cannot_be_activated():
    p = Registry().get(FN_C, FN_V)
    with pytest.raises(ConfigError, match="UNVERIFIED"):
        Activations(Path("/nonexistent/x.jsonl")).activate(p, approved_by="Someone Else", confirm_sha=p.sha256[:16],
                                                           statement="x")


def test_version_diff_flags_unsafe_changes(tmp_path):
    a = load_profile(write_profile(tmp_path / "a"))
    b = load_profile(write_profile(tmp_path / "b", amount=2500.0, contracts=5,
                                   internal="safety_reserve = 200.0\nflatten_reserve = 150.0\nuncertainty_reserve = 25.0\n"
                                            "quantity_policy = \"reduce\"\n"))
    d = diff(a, b)
    txt = " ".join(d["potentially_unsafe"])
    assert "T-MLL: amount" in txt and "looser" in txt and "T-CONTRACTS: limit_micro_equivalent" in txt
    assert "internal safety_reserve reduced" in txt and "quantity_policy changed to reduce" in txt
    assert d["requires_human_approval"] is True
    c = load_profile(write_profile(tmp_path / "c", amount=1500.0))
    assert not any("T-MLL" in x for x in diff(a, c)["potentially_unsafe"])  # stricter
    assert any(ch.get("direction") == "STRICTER" for ch in diff(a, c)["changes"])


# ============================================================================ drawdown models
def test_static_drawdown(tmp_path):
    e = engine(tmp_path, ml="static", lock=None)
    e.observe(ct(f"{MON} 09:00"), snap(10500.0))
    assert e.official_floor() == 8000.0                       # never trails
    assert e.buffers(snap(9000.0))["official_buffer"] == 1000.0


def test_intraday_trailing_raises_on_new_highs_including_open_profit(tmp_path):
    e = engine(tmp_path, ml="trailing_intraday", lock=None, ml_extra=', reference = "equity"'.replace(", reference", ", reference_ignored") if False else "")
    f = write_profile(tmp_path / "p2", pid="intra", ml="trailing_intraday", lock=None)
    f.write_text(f.read_text().replace('reference = "balance"', 'reference = "equity"'))
    e = ComplianceEngine(load_profile(f), operational=OP, tick_values={"MES": 1.25})
    e.observe(ct(f"{MON} 09:00"), snap(10000.0, 10600.0, {"MES": 1}))
    assert e.official_floor() == 8600.0
    e.observe(ct(f"{MON} 09:05"), snap(10000.0, 10100.0, {"MES": 1}))
    assert e.official_floor() == 8600.0                        # never moves down


def test_eod_trailing_only_at_day_change_and_lock(tmp_path):
    e = engine(tmp_path)  # trailing_eod 2000, lock 10100
    e.observe(ct(f"{MON} 09:00"), snap(10000.0))
    e.observe(ct(f"{MON} 14:00"), snap(10900.0, 11500.0, {"MES": 1}))
    assert e.official_floor() == 8000.0                        # intraday highs do not move an EOD floor
    e.observe(ct(f"{MON} 15:30"), snap(11000.0))
    acts = e.observe(ct(f"{MON} 17:01"), snap(11000.0))        # 17:00 CT starts Tuesday's trading date
    assert e.current_day == "2026-11-17" and e.eod_high == 11000.0 and e.official_floor() == 9000.0
    assert any(a.get("kind") == "prop_firm_eod" for a in acts)
    e.observe(ct("2026-11-17 15:30"), snap(12500.0))
    e.observe(ct("2026-11-17 17:30"), snap(12500.0))
    assert e.official_floor() == 10100.0                       # locked at initial + 100
    e.observe(ct("2026-11-18 15:30"), snap(14000.0))
    e.observe(ct("2026-11-18 17:30"), snap(14000.0))
    assert e.official_floor() == 10100.0                       # stays locked


def test_breach_includes_unrealized_losses_and_boundary(tmp_path):
    e = engine(tmp_path)
    acts = e.observe(ct(f"{MON} 09:00"), snap(10000.0, 8000.01, {"MES": 1}))
    assert not e.breached and any(a["action"] == "flatten" for a in acts)  # inside the flatten reserve
    acts = e.observe(ct(f"{MON} 09:01"), snap(10000.0, 8000.0, {"MES": 1}))   # exactly AT the floor
    assert e.breached and any(a.get("kind") == "prop_firm_breach" for a in acts)
    dec = e.evaluate_entry(ct(f"{MON} 09:02"), "MES", 1, snap(9990.0))
    assert "prop_max_loss_breached" in dec.reasons
    g = write_profile(tmp_path / "b", pid="below", ml_extra=', breach_when = "below"')
    e2 = ComplianceEngine(load_profile(g), operational=OP)
    e2.observe(ct(f"{MON} 09:00"), snap(10000.0, 8000.0, {"MES": 1}))
    assert not e2.breached
    e2.observe(ct(f"{MON} 09:01"), snap(10000.0, 7999.99, {"MES": 1}))
    assert e2.breached


def test_zero_buffer_is_zero_not_unavailable(tmp_path):
    e = engine(tmp_path)
    b = e.buffers(snap(8000.0))
    assert b["official_buffer"] == 0.0 and b["estimated_buffer"] == -25.0
    s = e.summary(snap(8000.0))
    assert s["official_buffer"] == 0.0 and s["available_risk_budget"] == -325.0


def test_fees_are_in_the_balance_and_reduce_the_buffer(tmp_path):
    e = engine(tmp_path)
    # balance passed by the runtime = initial + realized gross - commissions
    assert e.buffers(snap(10000.0 - 4.0))["official_buffer"] == 1996.0


def test_entry_thresholds_safety_reserve_trade_risk_and_gap_allowance(tmp_path):
    e = engine(tmp_path)  # floor 8000, uncertainty 25, safety 300, gap 4 ticks
    t = ct(f"{MON} 09:00")
    assert e.evaluate_entry(t, "MES", 1, snap(8325.0)).allowed is False        # est 300 <= 300
    assert e.evaluate_entry(t, "MES", 1, snap(8325.01)).allowed is True
    d = e.evaluate_entry(t, "MES", 1, snap(9000.0), trade_risk=670.0)         # 975 - 670 - 5 = 300 -> block
    assert "prop_trade_risk_exceeds_buffer" in d.reasons
    assert e.evaluate_entry(t, "MES", 1, snap(9000.0), trade_risk=669.0).allowed
    e2 = engine(tmp_path / "x", ticks={})
    assert "prop_tick_value_unknown" in e2.evaluate_entry(t, "MES", 1, snap(9000.0), trade_risk=10.0).reasons


# ============================================================================ daily loss / consistency
DLL = '[[rules]]\nid = "T-DLL"\ntype = "daily_loss"\ndescription = "dll"\nsafety_critical = true\nverification = "VERIFIED"\nsources = ["S1"]\nparams = { amount = 500.0, includes_unrealized = true }\n'


def test_daily_loss_realized_unrealized_and_reset(tmp_path):
    e = engine(tmp_path, rules_extra=DLL)
    e.observe(ct(f"{MON} 08:00"), snap(10000.0))
    t = ct(f"{MON} 10:00")
    assert e.evaluate_entry(t, "MES", 1, snap(9700.0, 9600.0, {"MES": 1})).allowed  # -400, est remaining 75
    d = e.evaluate_entry(t, "MES", 1, snap(9600.0, 9520.0, {"MES": 1}))           # -480 -> 20 - 25 < 0
    assert "prop_daily_loss_limit" in d.reasons
    d = e.evaluate_entry(t, "MES", 1, snap(9700.0), trade_risk=80.0)               # 300 - 25 - 80 = 195 ok
    assert d.allowed
    e.observe(ct(f"{MON} 17:05"), snap(9600.0))                                    # new trading date
    assert e.evaluate_entry(ct("2026-11-17 08:00"), "MES", 1, snap(9600.0)).allowed  # reset at 17:00 CT


def test_consistency_report_boundary_and_adjusted_target(tmp_path):
    extra = ('[[rules]]\nid = "T-CONS"\ntype = "consistency"\ndescription = "c"\nmandatory = false\nseverity = "warning"\n'
             'verification = "VERIFIED"\nsources = ["S1"]\nparams = { max_day_share = 0.4, of = "profit_target" }\n'
             '[[rules]]\nid = "T-TGT"\ntype = "profit_target"\ndescription = "t"\nmandatory = false\nseverity = "info"\n'
             'verification = "VERIFIED"\nsources = ["S1"]\nparams = { amount = 2500.0 }\n')
    e = engine(tmp_path, rules_extra=extra, stage="challenge")
    e.observe(ct(f"{MON} 08:00"), snap(10000.0))
    e.observe(ct(f"{MON} 14:00"), snap(11000.0))                 # +1000 = exactly 40% of 2500
    r = e.consistency_report()
    assert r["daily_threshold"] == 1000.0 and r["best_day"] == 1000.0 and r["adjusted_profit_target"] == 2500.0
    dec = e.evaluate_entry(ct(f"{MON} 14:01"), "MES", 1, snap(11000.0))
    assert dec.allowed and any(x.rule_id == "T-CONS" and x.result == WARNING for x in dec.results)  # warns, never blocks
    e.observe(ct(f"{MON} 14:30"), snap(11500.0))                 # +1500
    assert e.consistency_report()["adjusted_profit_target"] == 3750.0  # 1500 / 0.40 (firm example)


# ============================================================================ contracts
def test_contract_limits_mes_mnq_minis_pending_and_policies(tmp_path):
    e = engine(tmp_path)  # 3 micro-equivalent
    t = ct(f"{MON} 09:00")
    assert e.evaluate_entry(t, "MES", 3, snap()).allowed
    assert "prop_contract_limit" in e.evaluate_entry(t, "MES", 4, snap()).reasons
    assert "prop_contract_limit" in e.evaluate_entry(t, "MNQ", 2, snap(positions={"MES": -2})).reasons  # short counts
    assert "prop_contract_limit" in e.evaluate_entry(t, "MES", 1, snap(pending={"MNQ": 3})).reasons     # pending counts
    assert "prop_contract_limit" in e.evaluate_entry(t, "ES", 1, snap()).reasons                         # 1 mini = 10
    assert "prop_instrument_not_in_contract_table" in e.evaluate_entry(t, "CL", 1, snap()).reasons
    assert "prop_exposure_unknown" in e.evaluate_entry(t, "MES", 1, snap(positions={"CL": 1})).reasons
    r = engine(tmp_path / "r", internal="safety_reserve = 300.0\nflatten_reserve = 150.0\nquantity_policy = \"reduce\"\n")
    d = r.evaluate_entry(t, "MES", 5, snap(positions={"MNQ": 1}))
    assert d.allowed and d.allowed_quantity == 2                # reduced, never increased
    assert not r.evaluate_entry(t, "MES", 1, snap(positions={"MNQ": 3})).allowed
    for q in (0, -1, True, 1.5):
        assert "prop_invalid_quantity" in e.evaluate_entry(t, "MES", q, snap()).reasons


def test_instrument_allowlist(tmp_path):
    extra = '[[rules]]\nid = "T-INS"\ntype = "instrument_allowlist"\ndescription = "i"\nverification = "VERIFIED"\nsources = ["S1"]\nparams = { roots = ["MES"] }\n'
    e = engine(tmp_path, rules_extra=extra)
    t = ct(f"{MON} 09:00")
    assert e.evaluate_entry(t, "MES", 1, snap()).allowed
    assert "prop_instrument_not_allowed" in e.evaluate_entry(t, "MNQ", 1, snap()).reasons


# ============================================================================ session, weekend, DST, holidays
@pytest.mark.parametrize("local,ok", [(f"{MON} 14:59", True), (f"{MON} 15:00", False), (f"{MON} 16:59", False),
                                      (f"{MON} 17:00", True), ("2026-11-20 14:59", True), ("2026-11-20 15:00", False),
                                      ("2026-11-20 18:00", False), ("2026-11-21 12:00", False), ("2026-11-22 16:59", False),
                                      ("2026-11-22 17:00", True)])
def test_entry_window_flat_by_and_weekend(tmp_path, local, ok):
    e = engine(tmp_path)  # flat 15:10, cutoff 10 min -> 15:00, resume 17:00
    assert e.evaluate_entry(ct(local), "MES", 1, snap()).allowed is ok


@pytest.mark.parametrize("utc,date", [("2026-03-06T23:30:00Z", "2026-03-09"),   # Fri 17:30 CST -> Monday
                                      ("2026-03-09T21:59:00Z", "2026-03-09"),   # Mon 16:59 CDT (after DST)
                                      ("2026-03-09T22:00:00Z", "2026-03-10"),   # Mon 17:00 CDT
                                      ("2026-11-02T23:00:00Z", "2026-11-03"),   # Mon 17:00 CST (after DST ends)
                                      ("2026-11-02T22:59:00Z", "2026-11-02")])
def test_trading_date_across_dst(tmp_path, utc, date):
    assert engine(tmp_path).trading_date(pd.Timestamp(utc)) == date


def test_flat_by_requests_protective_flatten_across_dst(tmp_path):
    e = engine(tmp_path)  # flatten lead 5 -> from 15:05 local
    for day in ("2026-03-09", "2026-11-02"):     # first trading days after the DST switches
        e2 = engine(tmp_path / day)
        assert not any(a["action"] == "flatten" for a in e2.observe(ct(f"{day} 15:04"), snap(positions={"MES": 1})))
        assert any(a["reason"] == "prop_firm_flat_by" for a in e2.observe(ct(f"{day} 15:05"), snap(positions={"MES": 1})))
    assert not e.observe(ct(f"{MON} 15:06"), snap())          # flat: nothing to do


HOL = '[[rules]]\nid = "T-HOL"\ntype = "holiday_schedule"\ndescription = "h"\nsafety_critical = true\nverification = "VERIFIED"\nsources = ["S1"]\nparams = { calendar = "testcal" }\n'


def test_holiday_schedule_fails_closed_without_data(tmp_path):
    e = engine(tmp_path, rules_extra=HOL)
    t = ct("2026-11-25 10:00")
    assert "prop_holiday_schedule_unavailable" in e.evaluate_entry(t, "MES", 1, snap()).reasons
    (tmp_path / "holidays").mkdir()
    (tmp_path / "holidays" / "testcal.toml").write_text('covered_until = "2026-12-31"\n[[days]]\ndate = "2026-11-26"\nclose = "closed"\n'
                                                       '[[days]]\ndate = "2026-11-27"\nclose = "12:00"\n')
    e = engine(tmp_path, rules_extra=HOL)
    assert e.evaluate_entry(t, "MES", 1, snap()).allowed
    assert "prop_holiday_closed" in e.evaluate_entry(ct("2026-11-26 10:00"), "MES", 1, snap()).reasons
    assert e.evaluate_entry(ct("2026-11-27 11:59"), "MES", 1, snap()).allowed
    assert "prop_holiday_early_close" in e.evaluate_entry(ct("2026-11-27 12:00"), "MES", 1, snap()).reasons
    assert "prop_holiday_schedule_unavailable" in e.evaluate_entry(ct("2027-01-05 10:00"), "MES", 1, snap()).reasons
    bad = engine(tmp_path / "t", rules_extra=HOL.replace("testcal", "../../etc/passwd"))
    assert "prop_holiday_schedule_unavailable" in bad.evaluate_entry(t, "MES", 1, snap()).reasons


NEWS = '[[rules]]\nid = "T-NEWS"\ntype = "news_restriction"\ndescription = "n"\nsafety_critical = true\nverification = "VERIFIED"\nsources = ["S1"]\nparams = { minutes_before = 2, minutes_after = 2 }\n'


def test_news_restriction_uses_newsguard_and_fails_closed(tmp_path):
    e = engine(tmp_path, rules_extra=NEWS)
    t = ct(f"{MON} 09:00")
    assert "prop_news_state_unavailable" in e.evaluate_entry(t, "MES", 1, snap()).reasons
    assert "prop_news_restricted" in e.evaluate_entry(t, "MES", 1, snap(), news={"available": True, "allowed": False}).reasons
    assert e.evaluate_entry(t, "MES", 1, snap(), news={"available": True, "allowed": True}).allowed


# ============================================================================ fail closed
def test_fail_closed_cases(tmp_path):
    t = ct(f"{MON} 09:00")
    e = engine(tmp_path)
    assert "prop_equity_unavailable" in e.evaluate_entry(t, "MES", 1, snap(10000.0, math.nan, {"MES": 1})).reasons
    assert "prop_market_data_unavailable" in e.evaluate_entry(t, "MES", 1, snap(fresh=False)).reasons
    assert "prop_trade_risk_unknown" in e.evaluate_entry(t, "MES", 1, snap(), scope="fill").reasons
    assert "prop_account_size_mismatch" in e.evaluate_entry(t, "MES", 1, snap(initial=50000.0)).reasons
    off = engine(tmp_path / "o", op={"operational": False, "status": "UNVERIFIED", "not_operational_reason": "x"})
    assert off.evaluate_entry(t, "MES", 1, snap()).reasons[0] == "prop_profile_not_operational"
    unv = engine(tmp_path / "u", verification="UNVERIFIED")
    d = unv.evaluate_entry(t, "MES", 1, snap())
    assert not d.allowed and any(r.result == UNVERIFIED and r.rule_id == "T-MLL" for r in d.results)
    other = engine(tmp_path / "x", rules_extra='[[rules]]\nid = "T-OTHER"\ntype = "other"\ndescription = "o"\nsafety_critical = true\n'
                                              'verification = "UNVERIFIED"\nambiguity = "unknown"\n')
    assert "prop_rule_unverified:T-OTHER" in other.evaluate_entry(t, "MES", 1, snap()).reasons
    nomll = load_profile(write_profile(tmp_path / "n", pid="nomll"))
    nomll = replace(nomll, rules=tuple(r for r in nomll.rules if r.type != "max_loss"))
    e3 = ComplianceEngine(nomll, operational=OP)
    assert "prop_max_loss_rule_missing" in e3.evaluate_entry(t, "MES", 1, snap()).reasons


def test_fundednext_profile_blocks_every_entry_but_still_evaluates():
    from src.config import load_config

    app = load_config()
    p = Registry().get(FN_C, FN_V)
    e = ComplianceEngine(p, operational=operational_state(p, Activations(Path("/nonexistent/a.jsonl"))),
                         tick_values={r: s.tick_value for r, s in app.contracts.items()})
    d = e.evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap(50000.0, initial=50000.0), trade_risk=50.0)
    assert not d.allowed and d.reasons[0] == "prop_profile_not_operational"
    assert "prop_holiday_schedule_unavailable" in d.reasons and "prop_rule_unverified:FNF-FLEX50-MLL-EOD" in d.reasons
    assert d.official_floor == 48500.0 and d.official_buffer == 1500.0 and d.estimated_buffer == 1450.0
    assert ComplianceEngine.classify(d, False) == "UNVERIFIED"  # never SAFE


def test_classification_never_safe_with_unresolved_rules(tmp_path):
    from src.risk.account import AccountRules

    t = ct(f"{MON} 09:00")
    ag = AccountRules(enabled=True, profile="x", drawdown_type="trailing_eod", max_drawdown=2000.0, safety_buffer=400.0,
                      flatten_buffer=150.0)
    e = engine(tmp_path, ag=ag)
    assert ComplianceEngine.classify(e.evaluate_entry(t, "MES", 1, snap()), False) == "SAFE"
    assert ComplianceEngine.classify(engine(tmp_path / "noag").evaluate_entry(t, "MES", 1, snap()), False) == "WARNING"
    assert ComplianceEngine.classify(e.evaluate_entry(t, "MES", 1, snap(8100.0)), False) == "BLOCKED"
    assert ComplianceEngine.classify(e.evaluate_entry(t, "MES", 1, snap(8550.0)), False) == "WARNING"  # est 525 <= 2 x reserve
    assert ComplianceEngine.classify(e.evaluate_entry(t, "MES", 1, snap(10000.0, math.nan, {"MES": 1})), False) == "DATA UNAVAILABLE"
    u = engine(tmp_path / "u", verification="UNVERIFIED")
    assert ComplianceEngine.classify(u.evaluate_entry(t, "MES", 1, snap()), False) == "UNVERIFIED"


# ============================================================================ persistence, duplicates, concurrency
def test_state_roundtrip_and_never_substitutes(tmp_path):
    e = engine(tmp_path)
    e.observe(ct(f"{MON} 09:00"), snap(10000.0))
    e.observe(ct(f"{MON} 15:30"), snap(10800.0))
    e.observe(ct(f"{MON} 17:30"), snap(10800.0))
    e.on_execution("X1", ct(f"{MON} 09:00"))
    st = json.loads(json.dumps(e.state(), default=str))
    r = engine(tmp_path / "same")
    r.restore(st)
    assert r.recovery_ok and r.eod_high == 10800.0 and r.official_floor() == 8800.0 and not r.on_execution("X1", ct(f"{MON} 18:00"))
    other = engine(tmp_path / "other", amount=1500.0)               # different rule set
    other.restore(st)
    assert not other.recovery_ok and "refusing" in other.recovery_problems[0]
    assert "prop_state_untrusted" in other.evaluate_entry(ct(f"{MON} 18:00"), "MES", 1, snap(10800.0)).reasons
    rb = engine(tmp_path / "rb")
    rb.restore({**st, "eod_high": 9000.0})                          # below the initial balance
    assert not rb.recovery_ok
    j = engine(tmp_path / "j")
    j.restore(st)
    j.check_journal_consistency([11200.0], False)                   # journal saw a higher EOD high: rollback
    assert not j.recovery_ok
    k = engine(tmp_path / "k")
    k.restore(st)
    k.check_journal_consistency([], True)                           # breach lost
    assert not k.recovery_ok
    bad = engine(tmp_path / "bad")
    bad.restore({**st, "eod_high": "x"})
    assert not bad.recovery_ok


def test_duplicates_out_of_order_and_bounded_memory(tmp_path):
    from src.risk.prop_firms import engine as eng_mod

    e = engine(tmp_path)
    assert e.on_execution("A", ct(f"{MON} 09:00")) and not e.on_execution("A", ct(f"{MON} 09:00"))
    e.observe(ct(f"{MON} 10:00"), snap(10100.0))
    acts = e.observe(ct(f"{MON} 09:00"), snap(5000.0))       # out of order: days untouched, loss still checked
    assert e.out_of_order == 1 and e.breached and e.current_day == "2026-11-16"
    assert any(a.get("kind") == "prop_firm_breach" for a in acts)
    for i in range(eng_mod.MAX_EXEC_IDS + 10):
        e.on_execution(f"E{i}", ct(f"{MON} 10:00"))
    assert len(e.exec_ids) == eng_mod.MAX_EXEC_IDS
    for i in range(eng_mod.MAX_HISTORY + 20):
        e.evaluate_entry(ct(f"{MON} 10:01"), "MES", 1, snap(10100.0))
    assert len(e.history) == eng_mod.MAX_HISTORY
    t = pd.Timestamp("2027-01-04T15:00:00Z")
    for i in range(eng_mod.MAX_DAYS + 30):
        e.observe(t + pd.Timedelta(days=i), snap(10100.0))
    assert len(e.days) <= eng_mod.MAX_DAYS


def test_concurrent_observe_and_evaluate(tmp_path):
    e = engine(tmp_path)
    errors = []
    base = ct(f"{MON} 08:00")

    def obs():
        try:
            for i in range(300):
                e.observe(base + pd.Timedelta(seconds=i), snap(10000.0 + i))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def ev():
        try:
            for _ in range(300):
                e.evaluate_entry(base + pd.Timedelta(minutes=10), "MES", 1, snap(10000.0))
                e.summary(snap(10000.0))
                e.state()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=f) for f in (obs, ev, ev, obs)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and e.official_floor() == 8000.0


# ============================================================================ payout
def test_payout_eligibility(tmp_path):
    extra = "".join(
        f'[[rules]]\nid = "{rid}"\ntype = "{typ}"\ndescription = "{rid}"\nseverity = "info"\nverification = "{ver}"\nsources = ["S1"]\n'
        f'stages = ["funded"]\nparams = {params}\n'
        for rid, typ, ver, params in (("P-BD", "payout_benchmark_days", "VERIFIED", "{ days = 2, min_day_profit = 200.0 }"),
                                      ("P-MP", "payout_min_profit", "VERIFIED", "{ amount = 500.0 }"),
                                      ("P-MW", "payout_min_withdrawal", "VERIFIED", "{ amount = 250.0 }"),
                                      ("P-MX", "payout_max_withdrawal", "VERIFIED", "{ share_of_profit = 0.5, cap = 1500.0 }")))
    e = engine(tmp_path, rules_extra=extra)
    p = payout_eligibility(e)
    assert p["status"] == "RULES NOT SATISFIED" and "does not request" in p["disclaimer"]
    bal = 10000.0
    for i, gain in enumerate((250.0, 150.0, 300.0)):
        day = pd.Timestamp("2026-11-16") + pd.Timedelta(days=i)
        e.observe(ct(f"{day.date()} 08:00"), snap(bal))
        bal += gain
        e.observe(ct(f"{day.date()} 14:00"), snap(bal))
    p = payout_eligibility(e)
    req = {r["rule"]: r for r in p["requirements"]}
    assert req["P-BD"]["current"] == 2 and req["P-MP"]["current"] == 700.0 and req["P-MX"]["current"] == 350.0
    assert p["status"] == "RULES SATISFIED"
    off = engine(tmp_path / "o", rules_extra=extra, op={"operational": False})
    assert payout_eligibility(off)["status"] in ("MANUAL FIRM CONFIRMATION REQUIRED", "RULES NOT SATISFIED")
    unv = engine(tmp_path / "u", rules_extra=extra.replace('verification = "VERIFIED"\nsources = ["S1"]\nstages = ["funded"]\nparams = { days',
                                                               'verification = "UNVERIFIED"\nsources = ["S1"]\nstages = ["funded"]\nparams = { days'))
    assert payout_eligibility(unv)["status"] == "MANUAL FIRM CONFIRMATION REQUIRED"
    ch = ComplianceEngine(Registry().get(FN_C, FN_V), operational={"operational": False})
    assert payout_eligibility(ch)["status"] == "NOT APPLICABLE"
    fu = ComplianceEngine(Registry().get(FN_F, FN_V), operational={"operational": False})
    assert payout_eligibility(fu)["status"] == "MANUAL FIRM CONFIRMATION REQUIRED"


# ============================================================================ account guard consistency
def test_account_guard_consistency_warning(tmp_path):
    from src.risk.account import AccountRules

    t = ct(f"{MON} 09:00")
    loose = AccountRules(enabled=True, profile="x", drawdown_type="trailing_eod", max_drawdown=3000.0, safety_buffer=400.0,
                         flatten_buffer=150.0)
    d = engine(tmp_path, ag=loose).evaluate_entry(t, "MES", 1, snap())
    assert any(r.rule_id == "engine:account_guard" and r.result == WARNING and "not at least as strict" in r.explanation
               for r in d.results)
    strict = replace(loose, max_drawdown=2000.0)
    d = engine(tmp_path / "s", ag=strict).evaluate_entry(t, "MES", 1, snap())
    assert any(r.rule_id == "engine:account_guard" and r.result == PASS for r in d.results)


# ============================================================================ paper integration
from tests.paper_helpers import T, bt, guard_rules, long, path, session  # noqa: E402


def _active(tmp_path, **kw):
    reg_dir = tmp_path / "profiles"
    write_profile(reg_dir, **kw)
    reg = Registry(reg_dir)
    acts = Activations(tmp_path / "acts.jsonl")
    p = reg.get("test_firm", "1")
    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="test")
    return reg, acts


def _engine_for(tmp_path, reg, acts, btc):
    from tests.paper_helpers import APP
    from src.risk.prop_firms.runtime import build_engine

    return build_engine(bt=btc, specs={r: APP.contracts[r] for r in ("MES", "MNQ")}, profile_id="test_firm", version="1",
                        registry=reg, activations=acts)


def _decisions(s):
    return [e["payload"] for e in s.journal.entries("entry_decision")]


def test_paper_pipeline_order_and_pass(tmp_path):
    reg, acts = _active(tmp_path)
    btc = bt(account_rules=guard_rules())
    evs = path(T("10:00"), 20, lambda i: 5000.0)
    s = session(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5010.0)]}, btc=btc,
                prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    d = _decisions(s)[0]
    stages = [x["stage"] for x in d["stages"]]
    assert d["allowed"] and stages.index("account_guard") < stages.index("prop_firm") < stages.index("risk_manager")
    pf = next(x for x in d["stages"] if x["stage"] == "prop_firm")
    assert pf["profile"] == "test_firm@1" and pf["official_buffer"] == 2000.0
    fa = [e["payload"] for e in s.journal.entries("fill_authorization")][0]
    assert any(x["stage"] == "prop_firm" and x["ok"] for x in fa["stages"])


def test_account_guard_outranks_the_prop_engine(tmp_path):
    reg, acts = _active(tmp_path)
    btc = bt(account_rules=guard_rules(max_drawdown=1000.0, safety_buffer=999.0, flatten_buffer=100.0))
    evs = path(T("10:00"), 20, lambda i: 5000.0)
    s = session(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5010.0)]}, btc=btc,
                prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.account.guard.locked_permanently = True
    s.run()
    d = _decisions(s)[0]
    assert not d["allowed"] and d["reason"].startswith("account_")
    assert not any(x["stage"] == "prop_firm" for x in d["stages"])     # never reached: AccountGuard decided


def test_prop_block_cannot_be_overridden_by_the_strategy(tmp_path):
    reg, acts = _active(tmp_path, contracts=1)
    btc = bt()
    evs = path(T("10:00"), 20, lambda i: 5000.0)
    s = session(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5010.0, quantity=2)]}, btc=btc,
                prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    d = _decisions(s)[0]
    assert not d["allowed"] and d["reason"] == "prop_contract_limit"
    assert not list(s.journal.entries("execution"))


def test_unverified_profile_blocks_all_entries_in_paper(tmp_path):
    reg_dir = tmp_path / "profiles"
    write_profile(reg_dir, verification="UNVERIFIED")
    btc = bt()
    eng = _engine_for(tmp_path, Registry(reg_dir), Activations(tmp_path / "none.jsonl"), btc)
    evs = path(T("10:00"), 20, lambda i: 5000.0)
    s = session(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5010.0)]}, btc=btc, prop_engine=eng)
    s.run()
    assert _decisions(s)[0]["reason"] == "prop_profile_not_operational"


def test_protective_flatten_on_prop_threshold_is_never_blocked(tmp_path):
    # floor 8000 with a 2000 limit; a tiny limit makes one adverse move hit the flatten reserve
    # floor 10000 - 600 = 9400; a 90-point MES drop (-450 + costs) leaves <= 150 of buffer -> protective flatten
    reg, acts = _active(tmp_path, amount=600.0, lock=None,
                        internal="safety_reserve = 300.0\nflatten_reserve = 150.0\nuncertainty_reserve = 0.0\n"
                                 "require_trade_risk_within_buffer = false\n")
    btc = bt()
    evs = path(T("10:00"), 30, lambda i: 5000.0 if i < 40 else 4910.0)
    s = session(tmp_path / "s", evs, {T("10:05"): [long(stop=4800.0, target=5100.0)]}, btc=btc,
                prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    fl = [e["payload"]["reason"] for e in s.journal.entries("flatten_requested")]
    assert "prop_firm_max_loss_protection" in fl
    assert s.account.position_qty("MES") == 0


def test_restart_recovery_keeps_compliance_state_and_refuses_substitution(tmp_path):
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path)
    btc = bt()
    evs = path(T("10:00"), 20, lambda i: 5000.0 if i < 40 else 5008.0)
    s = mk(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5005.0)]}, btc=btc,
           prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    assert list(s.journal.entries("execution"))
    s.journal.close()
    # same profile: restored, trusted
    s2 = mk(tmp_path / "s", [], btc=btc, create=False, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    assert s2.prop.recovery_ok and s2.reconciliation_ok
    s2.journal.close()
    # another rule set under the same id is never adopted: entries blocked
    write_profile(tmp_path / "profiles", amount=1500.0)
    reg2 = Registry(tmp_path / "profiles")
    p2 = reg2.get("test_firm", "1")
    acts.activate(p2, approved_by="Test Reviewer", confirm_sha=p2.sha256[:16], statement="v2")
    s3 = mk(tmp_path / "s", [], btc=btc, create=False, prop_engine=_engine_for(tmp_path, reg2, acts, btc))
    assert not s3.prop.recovery_ok
    s3.journal.close()
    # profile removed from the session: a reconciliation problem (entries blocked), never silently dropped
    s4 = mk(tmp_path / "s", [], btc=btc, create=False)
    assert not s4.reconciliation_ok
    s4.journal.close()


def test_profile_added_to_an_account_with_history_is_untrusted(tmp_path):
    from tests.paper_helpers import session as mk

    reg, acts = _active(tmp_path)
    btc = bt()
    evs = path(T("10:00"), 20, lambda i: 5000.0 if i < 40 else 5008.0)
    s = mk(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5005.0)]}, btc=btc)
    s.run()
    s.journal.close()
    s2 = mk(tmp_path / "s", [], btc=btc, create=False, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    assert not s2.prop.recovery_ok and "history" in s2.prop.recovery_problems[0]
    s2.journal.close()


def test_report_from_journal_and_exports_are_safe(tmp_path):
    from src.analytics.shadow.library import to_csv
    from src.risk.prop_firms.reports import journal_report, rows_for_export

    reg, acts = _active(tmp_path, contracts=1)
    btc = bt()
    evs = path(T("10:00"), 20, lambda i: 5000.0)
    s = session(tmp_path / "s", evs, {T("10:05"): [long(stop=4990.0, target=5010.0, quantity=2, cid="=cmd|calc")]},
                btc=btc, prop_engine=_engine_for(tmp_path, reg, acts, btc))
    s.run()
    s.journal.close()
    rep = journal_report(tmp_path / "s" / "state.db")
    assert rep["counts"]["blocks"] == 1 and rep["block_reasons"] == {"prop_contract_limit": 1}
    assert "test_firm@1" in rep["profiles"]
    txt = to_csv(rows_for_export(rep, "evaluations"), {"report": "x"})
    assert "auth_token" not in json.dumps(rep) and "token" not in txt
    import sqlite3

    db = sqlite3.connect(tmp_path / "s" / "state.db")
    db.execute("UPDATE journal SET payload = replace(payload, 'prop_contract_limit', 'fine') WHERE kind='entry_decision'")
    db.commit()
    db.close()
    from src.paper.journal import JournalError

    with pytest.raises(JournalError):
        journal_report(tmp_path / "s" / "state.db")


# ============================================================================ CLI
def test_cli_list_evaluate_and_refused_activation(capsys):
    import run_prop_firm as cli

    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert FN_C in out and "UNVERIFIED" in out and "not operational" in out
    assert cli.main(["evaluate", FN_C, FN_V, "--balance", "50000", "--equity", "50000", "--root", "MES",
                     "--time", "2026-11-16T15:00:00Z"]) == 2
    assert "prop_profile_not_operational" in capsys.readouterr().out
    assert cli.main(["activate", FN_C, FN_V, "--approved-by", "Some One", "--confirm", "x", "--statement", "s"]) == 1
    assert cli.main(["evaluate", FN_C, FN_V, "--balance", "1", "--equity", "1", "--root", "MES", "--time", "2026-11-16T15:00:00"]) == 1


# ============================================================================ terminal API
TOKEN = "t" * 43
BASE = "http://127.0.0.1:8765"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def prop_terminal(tmp_path_factory):
    import time

    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService
    from tests.e2e.e2e_server import EveryNthBarTestStrategy

    tmp = tmp_path_factory.mktemp("propterm")
    reg_dir = tmp / "profiles"
    write_profile(reg_dir)
    reg, acts = Registry(reg_dir), Activations(tmp / "acts.jsonl")
    p = reg.get("test_firm", "1")
    acts.activate(p, approved_by="Test Reviewer", confirm_sha=p.sha256[:16], statement="t")
    svc = TerminalService(ModeOptions(mode="replay", speed=1e7, replay_start="2025-03-12",
                                      strategy_factory=lambda r: EveryNthBarTestStrategy(), tests_disable_regime_gate=True,
                                      prop_profile="test_firm@1", prop_registry=reg, prop_activations=acts))
    svc.start()
    end = time.monotonic() + 300
    while svc.state not in ("stopped", "failed") and time.monotonic() < end:
        time.sleep(0.2)
    assert svc.state == "stopped", svc.error
    yield svc
    svc.stop()


def test_terminal_prop_api(prop_terminal):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app

    with TestClient(create_app(prop_terminal, token=TOKEN, port=8765), base_url=BASE) as c:
        paths = ("/api/propfirm/status", "/api/propfirm/profiles", "/api/propfirm/profile?key=test_firm@1",
                 "/api/propfirm/compare?a=test_firm@1&b=test_firm@1", "/api/propfirm/history")
        for p in paths:
            assert c.get(p).status_code == 401
            assert c.get(p, headers=AUTH).status_code == 200, p
        st = c.get("/api/propfirm/status", headers=AUTH).json()
        assert st["selected"] and st["status"] in ("SAFE", "WARNING", "BLOCKED", "UNVERIFIED", "DATA UNAVAILABLE")
        assert st["summary"]["profile"] == "test_firm@1" and st["unified"]["hierarchy"][:3] == ["emergency", "account_guard", "prop_firm"]
        # client-provided values are ignored: the backend computes everything
        st2 = c.get("/api/propfirm/status?balance=1&equity=1&official_buffer=99999", headers=AUTH).json()
        assert st2["summary"]["balance"] == st["summary"]["balance"]
        h = c.get("/api/propfirm/history", headers=AUTH).json()
        assert h["counts"]["evaluations"] > 0 and "auth_token" not in json.dumps(h)
        for bad in ("/api/propfirm/profile?key=../../etc", "/api/propfirm/profile?key=a", "/api/propfirm/compare?a=x@1",
                    "/api/propfirm/export/secrets.csv", "/api/propfirm/export/evaluations.xlsx"):
            assert c.get(bad, headers=AUTH).status_code == 422, bad
        assert c.get("/api/propfirm/profile?key=nope@1", headers=AUTH).status_code == 404
        r = c.get("/api/propfirm/export/evaluations.csv", headers=AUTH)
        assert r.status_code == 200 and '# profile: ""test_firm@1""' in r.text and "# timezone" in r.text
        assert not any(line[:1] in "=+-@" for line in r.text.splitlines())
        r = c.get("/api/propfirm/export/rules.json", headers=AUTH)
        assert r.status_code == 200 and json.loads(r.text)["meta"]["ruleset_sha256"]
        for method in ("post", "put", "delete", "patch"):
            assert getattr(c, method)("/api/propfirm/status", headers=AUTH).status_code == 405
        for route in c.app.routes:
            assert (getattr(route, "methods", None) or set()) <= {"GET", "HEAD"}
            assert not any(w in route.path for w in ("activate", "approve", "override", "payout_submit", "withdraw"))
    assert not any(hasattr(prop_terminal, m) for m in ("activate_profile", "set_profile", "override_rule", "submit_payout"))


def test_terminal_without_profile(tmp_path):
    from fastapi.testclient import TestClient

    from src.terminal.api import create_app
    from src.terminal.modes import ModeOptions
    from src.terminal.service import TerminalService

    svc = TerminalService(ModeOptions(mode="mock", mock_rate_hz=10, prop_profile=""))
    with TestClient(create_app(svc, token=TOKEN, port=8765), base_url=BASE) as c:
        import time

        end = time.monotonic() + 20
        while svc.built is None and time.monotonic() < end:
            time.sleep(0.1)
        st = c.get("/api/propfirm/status", headers=AUTH).json()
        assert st["selected"] is False and "AccountGuard" in st["note"]
        assert any(p["key"].startswith(FN_C) for p in c.get("/api/propfirm/profiles", headers=AUTH).json()["profiles"])


# ============================================================================ adversarial audit regressions
def test_out_of_order_events_never_disable_the_loss_check(tmp_path):
    e = engine(tmp_path)
    e.observe(ct(f"{MON} 10:00"), snap(10000.0))
    acts = e.observe(ct(f"{MON} 09:00"), snap(10000.0, 7990.0, {"MES": 1}))   # earlier timestamp, deep loss
    assert e.breached and any(a["action"] == "flatten" for a in acts) and e.out_of_order == 1
    assert e.current_day == "2026-11-16"                                      # day never moved backwards


def test_entry_evaluation_applies_the_new_trading_day_first(tmp_path):
    e = engine(tmp_path)
    e.observe(ct(f"{MON} 09:00"), snap(10000.0))
    e.observe(ct(f"{MON} 15:30"), snap(10900.0))
    # first thing after 17:00: an entry evaluation (no market event yet) -> the floor already trailed
    d = e.evaluate_entry(ct(f"{MON} 17:01"), "MES", 1, snap(10900.0))
    assert e.eod_high == 10900.0 and d.official_floor == 8900.0
    acts = e.observe(ct(f"{MON} 17:02"), snap(10900.0))
    assert [a["kind"] for a in acts if a.get("kind")] == ["prop_firm_eod"]    # journaled exactly once
    assert not [a for a in e.observe(ct(f"{MON} 17:03"), snap(10900.0)) if a.get("kind")]


def test_account_guard_lock_below_the_firm_lock_is_looser(tmp_path):
    from src.risk.account import AccountRules

    ag = AccountRules(enabled=True, profile="x", drawdown_type="trailing_eod", max_drawdown=2000.0, safety_buffer=400.0,
                      flatten_buffer=150.0, floor_lock_offset=0.0)   # locks at 10000 < firm lock 10100
    d = engine(tmp_path, ag=ag).evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap())
    assert any(r.rule_id == "engine:account_guard" and r.result == WARNING for r in d.results)
    d = engine(tmp_path / "b", ag=replace(ag, floor_lock_offset=100.0)).evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap())
    assert any(r.rule_id == "engine:account_guard" and r.result == PASS for r in d.results)


def test_engine_checks_are_not_labelled_as_verified_firm_rules(tmp_path):
    d = engine(tmp_path).evaluate_entry(ct(f"{MON} 09:00"), "MES", 1, snap(initial=1.0))
    eng = [r for r in d.results if r.rule_id.startswith("engine:")]
    assert eng and all(r.verification == "ENGINE" for r in eng)


def test_revocation_takes_effect_immediately_in_a_running_engine(tmp_path):
    from src.risk.prop_firms.runtime import build_engine
    from tests.paper_helpers import APP

    reg, acts = _active(tmp_path)
    e = build_engine(bt=bt(), specs={"MES": APP.contracts["MES"]}, profile_id="test_firm", version="1", registry=reg,
                     activations=acts)
    t = ct(f"{MON} 09:00")
    assert e.evaluate_entry(t, "MES", 1, snap()).allowed
    acts.revoke(reg.get("test_firm", "1"), by="Test Reviewer", reason="rules changed")
    assert e.evaluate_entry(t, "MES", 1, snap()).reasons[0] == "prop_profile_not_operational"
    (tmp_path / "acts.jsonl").write_text("garbage\n")
    assert not e.evaluate_entry(t, "MES", 1, snap()).allowed

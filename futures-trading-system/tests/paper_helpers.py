"""Builders for Sprint 5 paper-trading tests (deterministic; no network)."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from src.ai.market_regime.config import load_regime_config
from src.backtest.config import load_backtest_config
from src.backtest.orders import OrderRequest
from src.config import load_config
from src.paper.config import load_paper_config
from src.paper.events import MarketEvent
from src.paper.feeds import _ListFeed
from src.paper.journal import Journal
from src.paper.runtime import PaperSession
from src.risk.account import AccountRules
from src.risk.news.guard import NewsGuard
from src.strategy.base import Strategy
from src.strategy.vwap_pullback_config import load_strategy_config
from tests.news_helpers import CFG as NEWS_CFG, rec, store

APP = load_config()
BT = load_backtest_config()
SC = load_strategy_config()
RC_OFF = replace(load_regime_config(), enabled=False)
PCFG = replace(load_paper_config(), mode="replay", instruments=("MES",))
NY = "America/New_York"


def T(local: str, day: str = "2025-03-10") -> pd.Timestamp:
    return pd.Timestamp(f"{day} {local}").tz_localize(NY).tz_convert("UTC")


def bt(**kw):
    """Backtest config without account protection unless given (keeps tests focused)."""
    return replace(BT, account_rules=kw.pop("account_rules", AccountRules()), **kw)


def guard_rules(**kw):
    base = dict(enabled=True, profile="test", drawdown_type="static", max_drawdown=1000.0, safety_buffer=200.0,
                flatten_buffer=100.0, require_trade_risk_within_buffer=False)
    return AccountRules(**(base | kw))


class ListFeed(_ListFeed):
    kind = "replay"
    name = "test_list"


class LiveLikeFeed(_ListFeed):
    """Stands in for a live provider in tests of paper mode (no network)."""
    kind = "simulated_realtime"
    name = "test_live_like"


_n = {"i": 0}


def trade(ts, px, size=5.0, root="MES", eid=None):
    _n["i"] += 1
    return MarketEvent(eid or f"t{_n['i']}", root, "trade", ts, px, size, source="test")


def quote(ts, bid, ask, root="MES", bs=10.0, as_=10.0, eid=None):
    _n["i"] += 1
    return MarketEvent(eid or f"q{_n['i']}", root, "quote", ts, None, 0.0, bid, ask, bs, as_, source="test")


def path(start: pd.Timestamp, minutes: int, price_at, root="MES", step_s: int = 15, quotes: bool = True):
    """Trades (and quotes) every ``step_s`` seconds; price_at(i) -> trade price at step i."""
    out = []
    n = minutes * 60 // step_s
    for i in range(n):
        ts = start + pd.Timedelta(seconds=i * step_s)
        px = price_at(i)
        if quotes:
            out.append(quote(ts, px - 0.25, px, root))
        out.append(trade(ts + pd.Timedelta(seconds=1), px, 5.0, root))
    return out


class Script(Strategy):
    """Test strategy: returns given requests at given decision times (ctx.now)."""
    name = "script"

    def __init__(self, script=None):
        self.script = script or {}
        self.events, self.fills, self.contexts = [], [], []

    def on_bar(self, ctx):
        self.contexts.append(ctx)
        return list(self.script.get(ctx.now, []))

    def on_fill(self, fill):
        self.fills.append(fill)

    def on_order_event(self, ev):
        self.events.append(ev)


def allow_news(roots=("MES", "MNQ"), *records, operational=False):
    st = store(*records)
    if operational:
        st.provider_class = {"fixture": "manual"}
    return {r: NewsGuard(NEWS_CFG, st, r, operational=operational) for r in roots}


def session(tmp_path, events, script=None, *, pcfg=PCFG, btc=None, news=None, feed_cls=ListFeed, regime=RC_OFF,
            warmup=None, create=True, strategies=None, **kw):
    strategies = strategies if strategies is not None else {}

    per_root = bool(script) and set(script) <= {"MES", "MNQ"}

    def factory(root):
        s = Script((script or {}).get(root, {}) if per_root else (script or {}))
        strategies[root] = s
        return s
    j = Journal(tmp_path / "state.db", create=create)
    s = PaperSession(pcfg, APP, btc or bt(), SC, regime, news or allow_news(), feed_cls(events), j, warmup=warmup,
                     strategy_factory=factory, sleeper=lambda x: None, **kw)
    s.test_strategies = strategies
    return s


def long(cid="s1", stop=4990.0, target=None, **kw):
    return OrderRequest.enter("long", stop_loss=stop, take_profit=target, client_id=cid, **kw)


__all__ = ["APP", "BT", "SC", "RC_OFF", "PCFG", "T", "bt", "guard_rules", "ListFeed", "LiveLikeFeed", "trade", "quote",
           "path", "Script", "allow_news", "session", "long", "rec"]

"""Paper trading runtime: market events -> validation -> health -> execution -> account/risk
-> bars -> strategy (+ regime gate) -> entry authorization -> paper broker. SIMULATION ONLY.

Data flow per validated event (one SQLite transaction per batch of events):

    feed.poll() --> EventValidator --rejected--> journal "data_rejected"
                         | ok
                         v
                    DataHealth.on_event --> journal "market_event"
                         v
    PaperBroker.on_event (working orders vs this event; entry fills re-authorized) --> executions
                         v
    PaperAccount.apply (positions, PnL, costs) --> AccountGuard / RiskManager / session checks
                         |   (flatten: cancel entries + market exits; exits are never blocked)
                         v
    BarBuilder (completed 1m bars) --> signal boundary? --> build_frames --> StrategyContext
                         v
    strategy.on_bar (wrapped by RegimeGatedStrategy) --> OrderRequests
                         v
    EntryAuthorizer (mode, system, data, request, position, NewsGuard, sizing, session,
                     AccountGuard, RiskManager) --> PaperBroker.submit or rejection record

Recovery: the journal hash chain is verified, the latest snapshot restored (AccountGuard peak,
locks and breach, RiskManager daily state, positions, orders), executions are reconciled against
the account; any mismatch blocks new entries. Working ENTRY orders are cancelled at restart,
protective orders stay working, and new entries stay blocked for the rest of the session in
which the restart happened (strategy state cannot be verified). No price is restored as live.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from typing import Any, Callable

import pandas as pd

from src.backtest.orders import Fill, OrderEvent, OrderRequest, OrderRole, OrderType, Side
from src.config import AppConfig, ConfigError
from src.data.sessions import session_bounds
from src.paper.account import AccountStateError, PaperAccount
from src.paper.authorization import EntryAuthorizer
from src.paper.bars import BarBuilder, build_frames
from src.paper.broker import FillDecision, PaperBroker
from src.paper.config import PaperConfig
from src.paper.events import MarketEvent
from src.paper.feeds import FeedError, MarketDataFeed
from src.paper.journal import Journal, JournalError
from src.paper.orders import Execution, PaperOrder
from src.paper.validation import DataHealth, EventValidator
from src.strategy.base import PositionSnapshot, StrategyContext
from src.strategy.vwap_pullback import VwapTrendPullbackStrategy

log = logging.getLogger("fts.paper")

RUN_KINDS = ("infrastructure_validation", "replay", "simulated_realtime", "paper")
# journal kinds mirrored into the bounded in-memory activity log (read by the terminal GUI; the
# SQLite journal stays the authoritative audit trail)
ACTIVITY_KINDS = frozenset({
    "session_created", "recovered", "entry_decision", "execution", "trade_closed", "flatten_requested",
    "account_guard", "daily_loss_limit", "emergency_stop", "feed_health", "feed_connected", "feed_disconnected",
    "feed_connect_failed", "data_rejected", "data_delayed", "price_level_reset", "system_error", "shutdown",
    "persistence_failure", "persistence_recovered", "protective_stop_restored", "signal_bar_skipped",
    "order_submitted", "order_filled", "order_cancelled", "order_rejected", "order_expired", "session_change"})


class RecoveryError(RuntimeError):
    """Persisted state cannot be recovered; trading must not start on it."""


def run_kind_for(mode: str, feed: MarketDataFeed) -> str:
    if mode == "infrastructure_validation":
        return "infrastructure_validation"
    if mode == "replay":
        if feed.kind != "replay":
            raise ConfigError("replay mode needs a replay feed")
        return "replay"
    if feed.kind == "replay":
        raise ConfigError("paper mode needs a live or simulated real-time feed; use replay mode for replays")
    return "paper" if feed.kind == "live" else "simulated_realtime"


class PaperSession:
    def __init__(self, pcfg: PaperConfig, app: AppConfig, bt, strat_cfg, regime_cfg, news_guards: dict[str, Any],
                 feed: MarketDataFeed, journal: Journal, *, warmup: dict[str, pd.DataFrame] | None = None,
                 clock: Callable[[], pd.Timestamp] | None = None, sleeper: Callable[[float], None] | None = None,
                 news_readiness: dict[str, Any] | None = None,
                 strategy_factory: Callable[[str], Any] | None = None,
                 session_meta: dict[str, Any] | None = None, prop_engine: Any = None, binding: Any = None) -> None:
        """``strategy_factory`` exists for TESTS that need deterministic requests to exercise the
        execution engine; production uses the existing VWAP Trend Pullback strategy, or (Sprint 11) a
        governance-approved registry version passed as ``binding`` (src.research.paper_binding)."""
        self.binding = binding
        self.pcfg, self.app, self.bt, self.scfg, self.regime = pcfg, app, bt, strat_cfg, regime_cfg
        # guards the whole session state: the run loop holds it while processing; read-only views
        # (e.g. the terminal API) hold it while building a consistent snapshot
        self.state_lock = threading.RLock()
        self.activity: collections.deque[dict[str, Any]] = collections.deque(maxlen=3000)
        self._activity_seq = 0
        self.started_at = pd.Timestamp.now(tz="UTC")
        # what kind of run this is (MOCK / REPLAY / SHADOW / MONITOR), data label and provider: journaled
        # with the run identity so analytics never mixes session types
        self.session_meta = dict(session_meta or {})
        self.feed, self.journal = feed, journal
        self.run_kind = run_kind_for(pcfg.mode, feed)
        self.roots = list(pcfg.instruments)
        missing = [r for r in self.roots if r not in news_guards]
        if missing:
            raise ConfigError(f"no NewsGuard for {missing}: entries could not be authorized")
        self.specs = {r: app.contracts[r] for r in self.roots}
        self.session = app.session
        self.clock = clock
        self._flatten_cache: dict[pd.Timestamp, pd.Timestamp] = {}
        self.sleeper = sleeper or time.sleep
        self.validator = EventValidator(self.specs, self.session, max_jump_ticks=pcfg.max_price_jump_ticks,
                                        max_spread_ticks=pcfg.max_spread_ticks, max_future_skew=pcfg.max_future_skew)
        self.health = DataHealth(self.roots, pcfg.stale_after, pcfg.resume_after_events)
        self.account = PaperAccount(bt, self.specs, self.session)
        self.broker = PaperBroker(bt, self.specs, participation=pcfg.limit_participation,
                                  quote_max_age=pcfg.quote_max_age)
        self.news = news_guards
        self.news_readiness = news_readiness
        # Sprint 9: prop firm compliance engine of the EXPLICITLY selected profile (None = not selected)
        self.prop = prop_engine
        self.authorizer = EntryAuthorizer(mode=pcfg.mode, bt=bt, specs=self.specs, account=self.account,
                                          broker=self.broker, news_guards=news_guards, system_state=self.system_state,
                                          flatten_time=self.flatten_time, allow_reversal=pcfg.allow_reversal,
                                          prop_engine=prop_engine, prop_snapshot=self.prop_snapshot)
        self.broker.entry_token_check = self.authorizer.check_token
        self.strategies, self.inner = {}, {}
        for r in self.roots:
            if strategy_factory:
                inner = strategy_factory(r)
            elif binding is not None:
                inner = binding.factory(r)
            else:
                inner = VwapTrendPullbackStrategy(strat_cfg, self.session.timezone)
            strat = inner
            if regime_cfg is not None and regime_cfg.enabled:
                from src.ai.market_regime.filters import RegimeGatedStrategy
                from src.ai.market_regime.regime import RegimeEngine

                strat = RegimeGatedStrategy(inner, RegimeEngine(regime_cfg, r), regime_cfg)
            strat.on_start(self.specs[r])
            self.strategies[r], self.inner[r] = strat, inner
        self.builders = {r: BarBuilder(r) for r in self.roots}
        self._warmup = warmup or {}
        self.seq = 0
        self.accepting_entries = True
        self.reconciliation_ok = True
        self.persistence_ok = True
        self.restart_block_session: pd.Timestamp | None = None
        self.account_session: pd.Timestamp | None = None
        self.last_event_ts: pd.Timestamp | None = None
        self.entry_meta: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.signals_seen = 0
        self._diag_pos = {r: 0 for r in self.roots}
        self._gate_pos = {r: 0 for r in self.roots}
        self._events_since_snapshot = 0
        self._journaled_peaks: tuple[float, float] | None = None
        self.latency = collections.deque(maxlen=2000)       # processing latency (s): processed - received
        self.feed_delay = collections.deque(maxlen=2000)    # data delay (s): received - event time
        self._delayed: dict[str, bool] = {}
        self.last_tick: dict[str, dict[str, Any]] = {}
        self.feed_updates_applied = 0
        self.stopped_reason: str | None = None
        self.clean_shutdown = False
        self._recover_or_init()

    # ------------------------------------------------------------------ time / state helpers
    def now(self) -> pd.Timestamp:
        if self.clock is not None and self.feed.kind != "replay":
            return self.clock()
        return self.last_event_ts if self.last_event_ts is not None else pd.Timestamp(0, tz="UTC")

    def flatten_time(self, session_date: pd.Timestamp) -> pd.Timestamp:
        if session_date not in self._flatten_cache:
            end = session_bounds([session_date], self.session).iloc[0]["end"]
            if self.binding is not None and self.binding.session is not self.session:
                # a strategy trading a narrower session (e.g. RTH) is flat before ITS session ends
                end = min(end, session_bounds([session_date], self.binding.session).iloc[0]["end"])
            self._flatten_cache[session_date] = end - self.bt.execution.flatten_before_session_end
        return self._flatten_cache[session_date]

    def session_of(self, ts: pd.Timestamp) -> pd.Timestamp | None:
        return self.validator.calendar(ts)

    def system_state(self, root: str, ts: pd.Timestamp) -> tuple[bool, str]:
        if not self.accepting_entries:
            return False, "shutting_down"
        if not self.reconciliation_ok:
            return False, "reconciliation_failed"
        if not self.persistence_ok:
            return False, "persistence_unhealthy"
        sd = self.session_of(ts)
        if self.restart_block_session is not None and sd == self.restart_block_session:
            return False, "restart_strategy_state_unverified"
        ok, why = self.health.healthy(root, ts if self.feed.kind == "replay" else self.now())
        if not ok:
            return False, why
        if not math.isfinite(self.account.equity()):
            return False, "account_equity_unknown"
        if self.binding is not None and getattr(self.binding, "recheck", None) is not None:
            ok, why = self._governance_ok()
            if not ok:
                return False, "strategy_not_approved"
        return True, ""

    def _governance_ok(self) -> tuple[bool, str]:
        """Sprint 11: the promotion state of a registry version is re-read at most every 30 s (wall clock); a
        SUSPENDED / REJECTED version, or a promotion store that fails its integrity check, blocks new entries.
        Open positions keep their protective orders (nothing is flattened by this check)."""
        now = time.monotonic()
        cached = getattr(self, "_gov_cache", None)
        if cached is None or now - cached[0] >= 30.0:
            try:
                res = self.binding.recheck()
            except Exception as exc:  # noqa: BLE001 -- unreadable approval state: fail closed
                res = (False, f"governance check failed: {type(exc).__name__}")
            if cached is not None and cached[1][0] and not res[0]:
                log.warning("strategy %s@%s no longer approved: %s -- new entries blocked",
                            self.binding.strategy_id, self.binding.version, res[1])
            self._gov_cache = cached = (now, res)
        return cached[1]

    def status(self) -> dict[str, Any]:
        """Read-only status snapshot for the Sprint 7 GUI (see src/marketdata/status.py)."""
        from src.marketdata.status import build_status

        return build_status(self)

    def _journal(self, kind: str, payload: dict[str, Any], ts: pd.Timestamp | None = None) -> None:
        t = ts or self.now()
        self.journal.append(kind, t, payload)
        if kind in ACTIVITY_KINDS:
            self._activity_seq += 1
            self.activity.append({"seq": self._activity_seq, "kind": kind, "time": t, "payload": payload})

    # ------------------------------------------------------------------ prop firm (Sprint 9)
    def prop_snapshot(self):
        """Backend account values for the compliance engine (never client-provided)."""
        from src.risk.prop_firms.engine import AccountSnapshot

        a = self.account
        pending = {r: sum(o.remaining for o in self.broker.working(r, ("entry",))) for r in self.roots}
        return AccountSnapshot(balance=a.initial_balance + a.net_realized, equity=a.equity(),
                               positions={r: a.position_qty(r) for r in self.roots}, pending_entries=pending,
                               # per-instrument freshness is checked by the authorizer's system stage first
                               data_fresh=bool(self.health.connected), initial_balance=a.initial_balance,
                               known_equity=a.known_equity())

    def _prop_observe(self, ts: pd.Timestamp) -> str | None:
        """Feed the engine; journal its records; return a flatten reason if it requests protection."""
        if self.prop is None:
            return None
        reason = None
        for act in self.prop.observe(ts, self.prop_snapshot()):
            if act["action"] == "journal":
                self._journal(act["kind"], act["payload"], ts)
            elif act["action"] == "flatten":
                reason = reason or act["reason"]
        return reason

    # ------------------------------------------------------------------ recovery
    def state(self) -> dict[str, Any]:
        return {"account": self.account.state(), "broker": self.broker.state(), "validator": self.validator.state(),
                "prop_firm": self.prop.state() if self.prop is not None else None,
                "session": {"run_kind": self.run_kind, "seq": self.seq,
                            "last_event_ts": self.last_event_ts.isoformat() if self.last_event_ts is not None else None,
                            "account_session": str(self.account_session) if self.account_session is not None else None,
                            "clean_shutdown": self.clean_shutdown, "entry_meta": self.entry_meta,
                            "signals_seen": self.signals_seen}}

    def _recover_or_init(self) -> None:
        n = self.journal.verify()
        snap = self.journal.latest_snapshot()
        if n == 0 and snap is None:
            if not getattr(self.journal, "created", False):
                # an existing state file with no journal and no snapshot is never silently treated as a
                # new account: that would reset the balance, drawdown peak, breach and locks
                raise RecoveryError("existing paper state has an empty journal and no snapshot (truncated or "
                                    "tampered); refusing to start a fresh account on it")
            for r in self.roots:
                self.builders[r] = BarBuilder(r, self._warmup.get(r))
            self._journal("session_created", {"run_kind": self.run_kind, "instruments": self.roots,
                                              "initial_balance": self.account.initial_balance,
                                              "news_readiness": self.news_readiness}, pd.Timestamp.now(tz="UTC"))
            self._journal_identity(pd.Timestamp.now(tz="UTC"))
            self._snapshot(pd.Timestamp.now(tz="UTC"))
            self.journal.commit()
            return
        if snap is None:
            raise RecoveryError("journal has entries but no snapshot: state cannot be verified")
        seq, st = snap
        if seq != self.journal.seq:
            raise RecoveryError(f"snapshot (journal seq {seq}) does not match the journal end ({self.journal.seq})")
        try:
            self.account.restore(st["account"])
            self.broker.restore(st["broker"])
            self.validator.restore(st["validator"])
        except (KeyError, ValueError, TypeError, AccountStateError) as exc:
            raise RecoveryError(f"stored state invalid: {exc}") from exc
        self._restore_prop(st.get("prop_firm"))
        s = st["session"]
        if s["run_kind"] != self.run_kind:
            raise RecoveryError(f"stored account belongs to a {s['run_kind']} run, not {self.run_kind}")
        self.seq = int(s["seq"])
        self.last_event_ts = pd.Timestamp(s["last_event_ts"]) if s["last_event_ts"] else None
        self.account_session = pd.Timestamp(s["account_session"]) if s["account_session"] else None
        self.entry_meta, self.signals_seen = dict(s["entry_meta"]), int(s["signals_seen"])
        was_clean = bool(s["clean_shutdown"])
        for r in self.roots:
            hist = self._warmup.get(r)
            stored = self.journal.bars(r)
            if hist is not None and len(stored):
                hist = hist[hist.index < stored.index[0]]
            base = pd.concat([h for h in (hist, stored) if h is not None and len(h)]) if (
                hist is not None and len(hist)) or len(stored) else None
            self.builders[r] = BarBuilder(r, base)
        problems = self._reconcile()
        self.reconciliation_ok = not problems
        self.restart_block_session = self.account_session  # strategy state of that session is not verifiable
        now = self.last_event_ts or pd.Timestamp.now(tz="UTC")
        for o in self.broker.working(roles=("entry",)):
            self.broker.cancel(o.order_id, now, "restart_cancel_pending_entry")
        self._protect_restored_positions(now)
        self._flush_broker_log()
        self.clean_shutdown = False
        self._journal("recovered", {"clean_shutdown_before": was_clean, "reconciliation_problems": problems,
                                    "journal_entries": n, "restart_block_session": str(self.restart_block_session)},
                      pd.Timestamp.now(tz="UTC"))
        self._journal_identity(pd.Timestamp.now(tz="UTC"))
        self._snapshot(now)
        self.journal.commit()

    def _restore_prop(self, pst: dict[str, Any] | None) -> None:
        """Compliance state is never reset or substituted on restart (blocks entries when untrustworthy)."""
        journaled = any(True for _ in self.journal.entries("prop_firm_eod")) or \
            any(True for _ in self.journal.entries("prop_firm_breach"))
        if self.prop is None:
            if pst is not None or journaled:
                self._prop_deselected = True  # a reconciliation problem: entries stay blocked
            return
        if pst is None:
            if journaled or any(True for _ in self.journal.entries("execution")):
                self.prop.recovery_ok = False
                self.prop.recovery_problems.append(
                    "this account has history without compliance state for the selected profile: its high-water "
                    "mark and daily counters cannot be established")
            return
        self.prop.restore(pst)
        highs = [float(e["payload"]["eod_high"]) for e in self.journal.entries("prop_firm_eod")
                 if e["payload"].get("profile") == self.prop.profile.key and e["payload"].get("eod_high") is not None]
        breach = any(True for _ in self.journal.entries("prop_firm_breach"))
        self.prop.check_journal_consistency(highs, breach)

    def _journal_identity(self, ts: pd.Timestamp) -> None:
        """Strategy / config / code / regime / cost identity of this run (Sprint 8 analytics)."""
        from src.analytics.shadow.identity import run_identity

        try:
            ident = run_identity(self)
        except Exception as exc:  # noqa: BLE001 -- identity is informative: it must never stop a session
            log.warning("run identity could not be computed: %s", type(exc).__name__)
            ident = {"schema": 1, "error": type(exc).__name__, "session_type": self.session_meta.get("session_type", "UNKNOWN"),
                     "data_label": self.session_meta.get("data_label"), "provider": self.session_meta.get("provider"),
                     "test_strategy": bool(self.session_meta.get("test_strategy"))}
        self._journal("run_identity", ident, ts)

    def _protect_restored_positions(self, now: pd.Timestamp) -> None:
        """A restored (or interrupted) position must never stay unprotected: every contract needs a
        working stop-loss on the closing side. Missing cover is re-created from the trade record's stop,
        or (no known stop) the position is flattened on the next market event."""
        for r in self.roots:
            q = self.account.position_qty(r)
            if q == 0:
                continue
            covered = sum(o.remaining for o in self.broker.working(r, ("stop_loss",)) if o.is_buy == (q < 0))
            missing = abs(q) - covered
            if missing <= 0:
                continue
            t = self.account.open_trades.get(r)
            if t is not None and t.stop_loss is not None:
                o = PaperOrder(self.broker.new_id("S"), r, "stop_loss", "stop", q < 0, missing, now, self.seq,
                               stop_price=t.stop_loss, session_date=t.session_date, tag="restart_protection")
                self.broker.submit(o, now)
                self._journal("protective_stop_restored", {"root": r, "quantity": missing, "stop": t.stop_loss}, now)
            else:
                self.flatten(r, "restart_unprotected_position", now)

    def _reconcile(self) -> list[str]:
        """Recompute positions and costs from the journal's executions and compare."""
        from src.backtest.position import Position

        pos = {r: Position(point_value=self.specs[r].point_value) for r in self.roots}
        gross = comm = 0.0
        ids = set()
        per_order: dict[str, int] = {}
        for e in self.journal.entries("execution"):
            ex = Execution.from_dict(e["payload"])
            if ex.root not in pos:
                return [f"execution {ex.exec_id} for unknown instrument {ex.root}"]
            gross += pos[ex.root].apply_fill(ex.is_buy, ex.quantity, ex.fill_price)
            comm += ex.commission
            ids.add(ex.exec_id)
            per_order[ex.order_id] = per_order.get(ex.order_id, 0) + ex.quantity
        problems = []
        a = self.account
        for r in self.roots:
            if pos[r].quantity != a.positions[r].quantity or abs(pos[r].avg_price - a.positions[r].avg_price) > 1e-6:
                problems.append(f"{r}: position {a.positions[r].quantity}@{a.positions[r].avg_price} != executions "
                                f"{pos[r].quantity}@{pos[r].avg_price}")
        if abs(gross - a.realized_gross) > 1e-6 or abs(comm - a.commissions) > 1e-6:
            problems.append(f"realized {a.realized_gross}/{a.commissions} != executions {gross}/{comm}")
        if ids != a.applied_exec_ids:
            problems.append("set of booked executions differs from the journal")
        for oid, o in self.broker.orders.items():
            if o.filled_qty != per_order.get(oid, 0):
                problems.append(f"order {oid}: filled {o.filled_qty} but executions show {per_order.get(oid, 0)}")
        for r, t in a.open_trades.items():
            if a.positions[r].quantity == 0:
                problems.append(f"{r}: open trade record without a position")
        for r in self.roots:
            if a.positions[r].quantity != 0 and not self.broker.working(r, ("stop_loss",)):
                problems.append(f"{r}: open position without a working stop-loss")
        if a.guard.enabled and a.guard.peak < a.initial_balance - 1e-9:
            problems.append("drawdown peak below the initial balance")
        if getattr(self, "_prop_deselected", False):
            problems.append("this account has prop firm compliance history but no profile is selected now "
                            "(a profile is never silently dropped or substituted)")
        # protection state must not be lost or reset: compare with what the journal recorded
        for e in self.journal.entries("account_guard"):
            if e["payload"].get("state") == "breach" and not a.guard.breached:
                problems.append("journal records a drawdown breach but the restored guard is not breached")
                break
        hw = -math.inf
        for e in self.journal.entries("account_update"):
            p = e["payload"]
            g = p.get("guard") or {}
            if g.get("breached") and not a.guard.breached:
                problems.append("journal records a drawdown breach but the restored guard is not breached")
            if g.get("locked_permanently") and not a.guard.locked_permanently:
                problems.append("journal records a permanent lock but the restored guard is not locked")
            eq = p.get("equity")
            if isinstance(eq, (int, float)) and math.isfinite(eq):
                hw = max(hw, eq)
        if math.isfinite(hw) and a.high_water < hw - 1e-6:
            problems.append(f"high-water mark {a.high_water} below a journaled equity {hw}")
        problems += self._reconcile_risk_state()
        problems = list(dict.fromkeys(problems))
        return problems

    def _reconcile_risk_state(self) -> list[str]:
        """Replay the journal's executions through a fresh account and compare the protection state
        (RiskManager days, consecutive losses, daily PnL, closed trades) with the restored one; check
        that the emergency stop, daily-loss marks, flatten locks and the trailing peak recorded in the
        journal survived. Detects a lost or edited protection state (bug or recomputed snapshot)."""
        a, problems = self.account, []
        shadow = PaperAccount(self.bt, self.specs, self.session)
        for e in self.journal.entries("execution"):
            ex = Execution.from_dict(e["payload"])
            o = self.broker.orders.get(ex.order_id)
            if o is None:
                problems.append(f"execution {ex.exec_id} for an order that is not in the restored state")
                continue
            sd_ev = self.session_of(ex.ts)
            sd = (o.session_date or sd_ev) if ex.role == "entry" else sd_ev
            try:
                shadow.apply(ex, sd, self.entry_meta.get(ex.order_id))
            except AccountStateError as exc:
                problems.append(f"executions cannot be replayed: {exc}")
                return problems
        days = lambda acc: {str(k.date()): (v.trades, round(v.realized_pnl, 6)) for k, v in acc.risk._days.items()
                            if v.trades or v.realized_pnl}  # noqa: E731
        if days(shadow) != days(a):
            problems.append("RiskManager daily state differs from the journal's executions")
        if shadow.risk._consecutive_losses != a.risk._consecutive_losses:
            problems.append("consecutive-loss count differs from the journal's executions")
        if {k: round(v, 6) for k, v in shadow.daily_net.items()} != {k: round(v, 6) for k, v in a.daily_net.items()}:
            problems.append("daily net PnL differs from the journal's executions")
        if len(shadow.closed_trades) != len(a.closed_trades):
            problems.append("closed-trade count differs from the journal's executions")
        if (list(self.journal.entries("emergency_stop")) or any(
                e["payload"].get("reason") == "emergency_stop" for e in self.journal.entries("flatten_requested"))) \
                and not a.risk.emergency_active:
            problems.append("journal records an emergency stop but the restored RiskManager is not stopped")
        for e in self.journal.entries("daily_loss_limit"):
            if not a.risk.daily_loss_breached(pd.Timestamp(e["payload"]["session_date"])):
                problems.append("journal records a daily-loss breach that the restored RiskManager lost")
        for e in self.journal.entries("account_guard"):
            st = e["payload"].get("state")
            if st == "flatten":
                sd = self.session_of(pd.Timestamp(e["ts"]))
                if sd is not None and sd == self.account_session and not a.guard.is_locked(sd):
                    problems.append("journal records a flatten lock for this session that the restored guard lost")
        for e in self.journal.entries("account_peak"):
            p = e["payload"]
            if a.guard.peak < p["guard_peak"] - 1e-6 or a.high_water < p["high_water"] - 1e-6:
                problems.append("drawdown peak or high-water mark below a value recorded in the journal")
                break
        if a.guard.enabled and a.guard.rules.drawdown_type == "trailing_intraday":
            for kind in ("account_guard", "account_update"):
                for e in self.journal.entries(kind):
                    eq = e["payload"].get("equity")
                    if isinstance(eq, (int, float)) and math.isfinite(eq) and a.guard.peak < eq - 1e-6:
                        problems.append("trailing drawdown peak below a journaled equity")
                        break
        return problems

    def _snapshot(self, ts: pd.Timestamp) -> None:
        self.journal.set_snapshot(ts, self.state())
        self._events_since_snapshot = 0

    # ------------------------------------------------------------------ main loop
    def connect(self, should_stop: Callable[[], bool] | None = None) -> bool:
        for attempt, wait in enumerate([0.0, *self.pcfg.reconnect_backoff][: self.pcfg.reconnect_max_attempts + 1]):
            if should_stop is not None and should_stop():
                return False
            if wait:
                self.sleeper(wait)
            try:
                self.feed.connect()
            except FeedError as exc:
                with self.state_lock:
                    self._journal("feed_connect_failed", {"attempt": attempt, "error": str(exc)[:200]})
                continue
            with self.state_lock:
                self.health.on_connect(self.now())
                self._journal("feed_connected", {"attempt": attempt, "feed": self.feed.name, "kind": self.feed.kind})
            return True
        return False

    def run(self, *, max_batches: int | None = None, idle_sleep: float = 0.5, stop_when_exhausted: bool = True,
            should_stop: Callable[[], bool] | None = None, on_cycle: Callable[["PaperSession"], None] | None = None
            ) -> str:
        """Main loop. ANY unexpected exception (feed, strategy, clock-driven bars, shutdown) ends in
        :meth:`_fatal` -- a journaled, fail-closed safe shutdown -- never in an unhandled crash.
        ``should_stop`` ends the loop with a safe shutdown; ``on_cycle`` is called after every loop
        cycle (e.g. to write the GUI status file) -- it must not raise."""
        try:
            return self._run(max_batches=max_batches, idle_sleep=idle_sleep, stop_when_exhausted=stop_when_exhausted,
                             should_stop=should_stop, on_cycle=on_cycle)
        except Exception as exc:  # noqa: BLE001
            with self.state_lock:
                return self._fatal(exc, None)

    def _run(self, *, max_batches: int | None, idle_sleep: float, stop_when_exhausted: bool,
             should_stop: Callable[[], bool] | None = None, on_cycle: Callable[["PaperSession"], None] | None = None
             ) -> str:
        if not self.connect(should_stop):
            with self.state_lock:
                self.shutdown("stopped" if should_stop is not None and should_stop() else "feed_unavailable")
            return self.stopped_reason or "feed_unavailable"
        batches = 0
        lock = self.state_lock  # held for processing only, never while sleeping or waiting for the feed
        while (max_batches is None or batches < max_batches) and not (should_stop is not None and should_stop()):
            if on_cycle is not None:
                on_cycle(self)
            try:
                batch = self.feed.poll(1000)
            except FeedError as exc:
                with lock:
                    self.health.on_disconnect(self.now(), str(exc))
                    self._journal("feed_disconnected", {"error": str(exc)[:200]})
                    self._commit()
                if should_stop is not None and should_stop():
                    break  # stop requested: shut down safely instead of reconnecting
                if not self.connect(should_stop):
                    with lock:
                        self.shutdown("feed_unavailable")
                    return "feed_unavailable"
                continue
            idle = False
            with lock:
                applied = self._apply_feed_updates()
                if not batch:
                    if applied:
                        self._commit()
                        continue  # more data may be right behind the update: no idle sleep
                    if self.feed.exhausted and stop_when_exhausted:
                        break
                    now = self.now()
                    self.health.check_stale(now)
                    if self.feed.kind != "replay":  # time passing completes bars even without trades
                        for r in self.roots:
                            for minute in self.builders[r].advance(now):
                                self._bar_completed_by_clock(r, minute, now)
                        self._commit()
                    idle = True
                else:
                    for ev in batch:
                        try:
                            self.process_event(ev)
                        except Exception as exc:  # noqa: BLE001 -- any internal failure is fatal and fail-closed
                            return self._fatal(exc, ev)
                    batches += 1
                    self._commit()
            if idle:
                self.sleeper(idle_sleep)
        with lock:
            self.shutdown("end_of_data" if self.feed.exhausted else "stopped")
        return self.stopped_reason or "stopped"

    def _fatal(self, exc: BaseException, ev: MarketEvent | None) -> str:
        """An internal error: entries stop immediately, the error is journaled, state is persisted and
        the session shuts down safely (protective orders stay). It is never silently skipped."""
        self.accepting_entries = False
        self.reconciliation_ok = False
        self._journal("system_error", {"error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                       "event_id": getattr(ev, "event_id", None)})
        try:
            # the failure may have interrupted the event between a fill and its bracket: no position
            # may be left without a protective stop
            self._protect_restored_positions(self.now())
            self._flush_broker_log()
        except Exception as exc2:  # noqa: BLE001
            self._journal("protection_check_failed", {"error": f"{type(exc2).__name__}: {str(exc2)[:300]}"})
        try:
            if self.stopped_reason == "system_error":
                raise RuntimeError("second fatal error")
            self.stopped_reason = "system_error"
            self.shutdown("system_error")
        except Exception as exc2:  # noqa: BLE001
            self._journal("shutdown_failed", {"error": f"{type(exc2).__name__}: {str(exc2)[:300]}"})
            self._commit()
        self.stopped_reason = "system_error"
        return "system_error"

    def _journal_peaks(self) -> None:
        """Record every new drawdown peak / high-water mark in the hash-chained journal (at most once
        per batch), so a restored snapshot can never silently lower them."""
        cur = (self.account.guard.peak, self.account.high_water)
        if cur != self._journaled_peaks:
            self._journal("account_peak", {"guard_peak": cur[0], "high_water": cur[1]})
            self._journaled_peaks = cur

    def _commit(self) -> None:
        self._journal_peaks()
        self._snapshot(self.now())
        try:
            self.journal.commit()
            if not self.persistence_ok:
                self.persistence_ok = True
                self._journal("persistence_recovered", {})
                self._snapshot(self.now())
                self.journal.commit()
        except JournalError as exc:
            self.persistence_ok = False  # new entries blocked until a commit succeeds
            self.journal._pending_snapshot = None
            self.journal.append("persistence_failure", self.now(), {"error": str(exc)[:300]})

    # ------------------------------------------------------------------ per event
    def process_event(self, ev: MarketEvent) -> None:
        clock_now = self.clock() if self.clock is not None and self.feed.kind != "replay" else None
        v = self.validator.check(ev, clock_now)
        if not v.ok:
            ok_ts = isinstance(ev.ts, pd.Timestamp) and not pd.isna(ev.ts) and ev.ts.tzinfo is not None
            self._journal("data_rejected", {"reason": v.reason, **ev.ref()}, ev.ts if ok_ts else None)
            return
        self.seq += 1
        self.last_event_ts = ev.ts if self.last_event_ts is None else max(self.last_event_ts, ev.ts)
        if "price_level_reset" in v.warnings:
            # a confirmed new price level after rejected jumps: data was missing in between, so entries
            # wait for fresh events again (protective orders keep working on the new prices)
            self.health.flag(ev.root, "price_level_reset", ev.ts)
            self._journal("price_level_reset", {**ev.ref(), "note": "jump confirmed by consecutive events"}, ev.ts)
        if ev.received_at is not None:
            self._check_delay(ev)
        self.health.on_event(ev.root, ev.ts)
        prices = {k: getattr(ev, k) for k in ("last", "size", "bid", "ask", "bid_size", "ask_size")
                  if getattr(ev, k) is not None}
        if ev.instrument:
            prices["instrument"] = ev.instrument
        self._journal("market_event", {**ev.ref(), "seq": self.seq, "warnings": v.warnings,
                                       "synthetic": ev.synthetic_from_bars, **prices}, ev.ts)
        sd = v.session_date
        self._session_rollover(sd, ev.ts)
        self._mark(ev)
        execs = self.broker.on_event(ev, self.seq, self.account.position_qty, self._authorize_fill)
        for ex in execs:
            self._book(ex, sd)
        self._flush_broker_log()
        self._risk_checks(ev, sd)
        done = self.builders[ev.root].on_trade(ev) if ev.is_trade else self.builders[ev.root].advance(ev.ts)
        for minute in done:
            self._bar_completed(ev.root, minute, ev)
        self._events_since_snapshot += 1
        t = self.last_tick.setdefault(ev.root, {})
        t["time"] = ev.ts
        if ev.kind == "quote":
            t["bid"], t["ask"], t["quote_time"] = ev.bid, ev.ask, ev.ts
        else:
            t["last"], t["last_time"] = ev.last, ev.ts
        if ev.received_at is not None:
            self.latency.append((self._wall() - ev.received_at).total_seconds())

    def _wall(self) -> pd.Timestamp:
        return self.clock() if self.clock is not None else pd.Timestamp.now(tz="UTC")

    def _check_delay(self, ev: MarketEvent) -> None:
        """Live data: an event received long after its own time is DELAYED data. It is still used
        (marks, protective orders: it is real data) but entries are blocked until data is timely
        again for ``resume_after_events`` events. A negative delay is impossible clock skew."""
        delay = ev.received_at - ev.ts
        self.feed_delay.append(delay.total_seconds())
        late = delay > self.pcfg.max_event_delay or delay < -self.pcfg.max_future_skew
        if late and not self._delayed.get(ev.root):
            self._journal("data_delayed", {"root": ev.root, "delay_seconds": delay.total_seconds(),
                                           "limit_seconds": self.pcfg.max_event_delay.total_seconds()}, ev.ts)
        if late:
            self.health.flag(ev.root, "data_delayed", ev.ts)
        self._delayed[ev.root] = late

    def _apply_feed_updates(self) -> int:
        """Provider-reported data-health changes (subscriptions, provider connection, dropped events,
        book resets) -> DataHealth, journaled. Blocks hold until explicitly cleared; every clearing
        still requires fresh validated events before entries are authorized again."""
        n = 0
        for u in self.feed.health_updates():
            roots = self.roots if u.root is None else [u.root] if u.root in self.roots else []
            for r in roots:
                if u.action == "block":
                    self.health.block(r, u.reason, u.time)
                elif u.action == "unblock":
                    self.health.unblock(r, u.reason, u.time)
                elif u.action == "flag":
                    self.health.flag(r, u.reason, u.time)
            self.feed_updates_applied += 1
            n += 1
            self._journal("feed_health", {"root": u.root, "action": u.action, "reason": u.reason}, u.time)
        return n

    def _mark(self, ev: MarketEvent) -> None:
        pos = self.account.position_qty(ev.root)
        if ev.kind == "quote":
            px = ev.bid if pos > 0 else ev.ask if pos < 0 else (ev.bid + ev.ask) / 2
        else:
            px = ev.last
        self.account.mark(ev.root, px)

    def _session_rollover(self, sd: pd.Timestamp | None, ts: pd.Timestamp) -> None:
        if sd is not None and sd != self.account_session:
            if self.account_session is not None:
                eq = self.account.equity()
                if math.isfinite(eq):
                    self.account.guard.on_session_end(eq, sd)
                self._journal("session_change", {"from": str(self.account_session), "to": str(sd), "equity": eq}, ts)
            self.account_session = sd

    def _authorize_fill(self, o: PaperOrder, base: float, qty: int, ev: MarketEvent) -> FillDecision:
        d, q = self.authorizer.authorize_fill(o, base, qty, ev)
        self._journal("fill_authorization", {"order_id": o.order_id, "root": o.root, "allowed": d.allowed,
                                             "reason": d.reason, "stages": d.stages, "event_id": ev.event_id}, ev.ts)
        if not d.allowed and o.client_id:
            self.strategies[o.root].on_order_event(OrderEvent(o.client_id, _num(o.order_id), "rejected", ev.ts,
                                                              "fill", d.reason))
        return FillDecision(d.allowed, d.reason, q)

    def _book(self, ex: Execution, sd: pd.Timestamp | None) -> None:
        o = self.broker.orders[ex.order_id]
        first_fill = o.role == "entry" and o.filled_qty == ex.quantity
        closed = self.account.apply(ex, sd if o.role != "entry" else (o.session_date or sd),
                                    self.entry_meta.get(ex.order_id))
        self._journal("execution", ex.to_dict(), ex.ts)
        if self.prop is not None:
            self.prop.on_execution(ex.exec_id, ex.ts)
        self._journal("account_update", {"exec_id": ex.exec_id, **{k: v for k, v in self.account.summary().items()
                                                                     if k != "daily_net"}}, ex.ts)
        if closed:
            if o.role == "exit" and o.tag:
                closed["exit_reason"] = o.tag  # why it was closed (session_end, reversal, ...), not the fill rule
            self._journal("trade_closed", closed, ex.ts)
        self.broker.on_execution(ex, self.account.position_qty(ex.root), ex.ts)
        strat = self.strategies[ex.root]
        role = OrderRole(ex.role) if ex.role in OrderRole._value2member_map_ else OrderRole.EXIT
        strat.on_fill(Fill(_num(ex.order_id), role, ex.is_buy, ex.quantity, ex.fill_price, ex.ts, False, ex.commission,
                           ex.slippage_cost, True, ex.reference_price))
        if first_fill and o.client_id:
            strat.on_order_event(OrderEvent(o.client_id, _num(o.order_id), "filled", ex.ts))

    def _flush_broker_log(self) -> None:
        for item in self.broker.log:
            o = self.broker.orders.get(item["order_id"])
            if item["kind"] in ("cancelled", "expired") and o is not None and o.role == "entry" and o.client_id \
                    and o.filled_qty == 0:
                self.strategies[o.root].on_order_event(OrderEvent(o.client_id, _num(o.order_id), "cancelled",
                                                                  pd.Timestamp(item["time"]), "pending", o.reason))
            self._journal("order_" + item["kind"], item, pd.Timestamp(item["time"]))
        self.broker.log.clear()

    # ------------------------------------------------------------------ protection
    def _risk_checks(self, ev: MarketEvent, sd: pd.Timestamp | None) -> None:
        a = self.account
        eq = a.equity()
        a.update_drawdown()
        any_pos = any(a.position_qty(r) != 0 for r in self.roots)
        reason = None
        if math.isfinite(eq) and a.guard.enabled:
            a.guard.observe_intraday(eq, favorable=False)
            state = a.guard.evaluate(eq, ev.ts, sd, position_open=any_pos)
            if state:
                self._journal("account_guard", {"state": state, "equity": eq, "floor": a.guard.floor}, ev.ts)
                reason = "account_protection"
        elif a.guard.enabled and any_pos:
            # Full equity is unknown (an open position has no mark yet, e.g. after a restart). The losses
            # that ARE known must still protect the account: flatten when they alone reach the flatten
            # buffer. No peak is raised and no breach is recorded from partial information.
            known = a.known_equity()
            if a.guard.remaining_buffer(known) <= a.guard.rules.flatten_buffer + 1e-9:
                self._journal("account_guard", {"state": "flatten_on_known_equity", "known_equity": known,
                                                "floor": a.guard.floor, "note": "an open position is unmarked"},
                              ev.ts)
                reason = "account_protection_partial_equity"
        if a.risk.emergency_active:
            reason = reason or "emergency_stop"
        prop_reason = self._prop_observe(ev.ts)
        if prop_reason and any_pos:
            reason = reason or prop_reason
        if sd is not None and a.risk.config.tracks_unrealized_loss and any_pos:
            paid = sum(t.commission for t in a.open_trades.values())  # entry costs already paid
            if math.isfinite(eq):
                was = a.risk.daily_loss_breached(sd)
                if a.risk.check_daily_loss(sd, a.unrealized() - paid):
                    if not was:
                        self._journal("daily_loss_limit", {"session_date": str(sd.date())}, ev.ts)
                    if a.risk.config.daily_loss_action == "flatten":
                        reason = reason or "daily_loss_limit"
            elif a.risk.config.max_daily_loss and a.risk.config.daily_loss_action == "flatten":
                # an open position is unmarked: the KNOWN losses alone still enforce the limit (flatten
                # only; the day is not marked from partial information)
                known_open = a.known_equity() - a.initial_balance - a.net_realized - paid
                if a.risk.daily_pnl(sd, known_open) <= -a.risk.config.max_daily_loss + 1e-9:
                    reason = reason or "daily_loss_limit_partial_equity"
        if reason:
            self.flatten_all(reason, ev.ts)
        # session end: cancel entries and flatten before the session closes
        for r in self.roots:
            osd = None
            open_t = a.open_trades.get(r)
            if open_t is not None:
                osd = open_t.session_date
            if (sd is not None and ev.ts >= self.flatten_time(sd)) or (osd is not None and sd is not None and sd != osd):
                for o in self.broker.working(r, ("entry",)):
                    self.broker.cancel(o.order_id, ev.ts, "session_end")
                if a.position_qty(r) != 0:
                    self.flatten(r, "session_end", ev.ts)
        self._flush_broker_log()

    def flatten(self, root: str, reason: str, ts: pd.Timestamp) -> None:
        """Market exit for one instrument (protective: never authorization-gated)."""
        qty = self.account.position_qty(root)
        if qty == 0 or self.broker.working(root, ("exit",)):
            return
        for o in self.broker.working(root, ("entry", "stop_loss", "take_profit")):
            self.broker.cancel(o.order_id, ts, reason)
        o = PaperOrder(self.broker.new_id("E"), root, "exit", "market", qty < 0, abs(qty), ts, self.seq, tag=reason,
                       session_date=self.session_of(ts))
        self.broker.submit(o, ts)
        self._journal("flatten_requested", {"root": root, "reason": reason, "quantity": abs(qty),
                                            "note": "simulated exit; a real market exit can fill worse"}, ts)

    def flatten_all(self, reason: str, ts: pd.Timestamp) -> None:
        for r in self.roots:
            for o in self.broker.working(r, ("entry",)):
                self.broker.cancel(o.order_id, ts, reason)
            self.flatten(r, reason, ts)

    # ------------------------------------------------------------------ signals
    def _bar_completed(self, root: str, minute: pd.Timestamp, ev: MarketEvent) -> None:
        b = self.builders[root]
        self.journal.add_bar(root, minute, tuple(float(x) for x in b._rows[-1]))
        end = minute + pd.Timedelta(minutes=1)
        tf = pd.Timedelta(self.binding.signal_timeframe if self.binding is not None else self.scfg.signal_timeframe)
        if (end - end.normalize()) % tf != pd.Timedelta(0):
            return
        self._signal(root, end, ev)

    def _bar_completed_by_clock(self, root: str, minute: pd.Timestamp, now: pd.Timestamp) -> None:
        """A minute completed because the clock passed its end (no later trade). The decision uses
        the clock time; any order still fills only on a later real market event."""
        from src.paper.events import MarketEvent as _E

        self._bar_completed(root, minute, _E(f"clock-{now.isoformat()}", root, "clock", now, source="clock"))

    def _signal(self, root: str, boundary: pd.Timestamp, ev: MarketEvent) -> None:
        if self.binding is not None:
            signal, context = self.binding.frames(root, self.builders[root].frame())
        else:
            signal, context = build_frames(self.builders[root].frame(), self.app, self.scfg, self.regime)
        avail = pd.DatetimeIndex(signal["available_at"])
        hits = (avail == boundary).nonzero()[0]
        if not len(hits):
            return
        k = int(hits[0])
        bar = signal.iloc[k]
        sd = bar["session_date"] if pd.notna(bar["session_date"]) else None
        if not bool(bar["is_complete"]):
            self._journal("signal_bar_skipped", {"root": root, "bar_time": str(signal.index[k]),
                                                 "reason": "incomplete_signal_bar"}, ev.ts)
            return
        ctx_frames = {}
        for name, f in context.items():
            a = pd.DatetimeIndex(f["available_at"])
            ctx_frames[name] = f.iloc[: int(a.searchsorted(boundary, side="right"))]
        pos = self.account.positions[root]
        snap = PositionSnapshot(None if pos.is_flat else (Side.LONG if pos.quantity > 0 else Side.SHORT),
                                abs(pos.quantity), pos.avg_price,
                                pos.unrealized_pnl(self.account.marks.get(root, pos.avg_price)) if not pos.is_flat else 0.0)
        ctx = StrategyContext(now=boundary, bar=bar, history=signal.iloc[: k + 1], bar_is_complete=True,
                              session_date=sd, position=snap,
                              has_pending_entry=bool(self.broker.working(root, ("entry",))),
                              contract=self.specs[root], context=ctx_frames)
        strat = self.strategies[root]
        requests = list(strat.on_bar(ctx) or [])
        inner = self.inner[root]
        for row in getattr(inner, "diagnostics", [])[self._diag_pos[root]:]:
            if row.get("decision") == "signal" or row.get("event") == "signal":   # trend / mean-reversion rows
                self.signals_seen += 1
            self._journal("strategy_decision", {"root": root, **{k2: _jsonable(v) for k2, v in row.items()}}, ev.ts)
        self._diag_pos[root] = len(getattr(inner, "diagnostics", []))
        gate_records = []
        if hasattr(strat, "decisions"):
            gate_records = strat.decisions[self._gate_pos[root]:]
            for rec in gate_records:
                self._journal("regime_decision", {"root": root, **{k2: _jsonable(v) for k2, v in rec.items()}}, ev.ts)
            self._gate_pos[root] = len(strat.decisions)
            for rec in gate_records:
                if not rec.get("allowed"):
                    self._record_decision(root, ev.ts, rec.get("signal_id"), rec.get("side"), False,
                                          f"regime_{str(rec.get('suitability')).lower()}",
                                          [{"stage": "regime", "ok": False, "reason": rec.get("block_reason")}], rec)
        regime_by_signal = {rec.get("signal_id"): rec for rec in gate_records}
        entry_seen = False
        for req in requests:
            if req.action == "exit":
                self.flatten(root, req.tag or "strategy_exit", ev.ts)
                continue
            if req.action == "cancel_entry":
                for o in self.broker.working(root, ("entry",)):
                    self.broker.cancel(o.order_id, ev.ts, "cancelled_by_strategy")
                continue
            if req.action != "entry":
                continue
            if entry_seen:
                self._reject_request(root, req, ev.ts, "duplicate_signal", [], None)
                continue
            entry_seen = True
            self._entry(root, req, ev, sd, float(bar["close"]), regime_by_signal.get(req.client_id))
        self._flush_broker_log()

    def _record_decision(self, root, ts, signal_id, side, allowed, reason, stages, regime_rec) -> None:
        rec = {"time": ts.isoformat(), "root": root, "signal_id": signal_id, "side": side, "allowed": allowed,
               "reason": reason, "stages": stages, "run_kind": self.run_kind,
               "regime": None if regime_rec is None else {k: _jsonable(regime_rec.get(k)) for k in (
                   "trend", "suitability", "confidence_level", "volatility", "range")}}
        self.decisions.append(rec)
        self._journal("entry_decision", rec, ts)

    def _reject_request(self, root, req: OrderRequest, ts, reason, stages, regime_rec) -> None:
        self._record_decision(root, ts, req.client_id, req.side.value if req.side else None, False, reason, stages,
                              regime_rec)
        if req.client_id:
            self.strategies[root].on_order_event(OrderEvent(req.client_id, None, "rejected", ts, "authorization",
                                                            reason))

    def _entry(self, root: str, req: OrderRequest, ev: MarketEvent, sd, ref_close: float, regime_rec) -> None:
        d, info = self.authorizer.authorize_signal(req, root, ev.ts, sd, ref_close)
        if not d.allowed:
            self._reject_request(root, req, ev.ts, d.reason, d.stages, regime_rec)
            return
        if info.get("reverse_first"):
            self.flatten(root, "reversal", ev.ts)
        otype = req.order_type.value
        expire = self.flatten_time(sd)
        if req.order_type is OrderType.MARKET:
            expire = min(expire, ev.ts + self.bt.execution.max_fill_delay)  # never beyond the session cutoff
        o = PaperOrder(self.broker.new_id("N"), root, "entry", otype, req.side is Side.LONG, info["quantity"], ev.ts,
                       self.seq, limit_price=info["price"] if otype == "limit" else None,
                       stop_price=info["price"] if otype == "stop" else None, expire_at=expire, client_id=req.client_id,
                       tag=req.tag, stop_loss=info["stop"], take_profit=info["target"],
                       reference_price=info["reference"], max_stop_distance=req.max_stop_distance,
                       min_stop_distance=req.min_stop_distance, min_reward_risk=req.min_reward_risk,
                       risk_budget=info["risk_budget"], session_date=sd, signal_time=ev.ts, auth_token=info["token"])
        self.broker.submit(o, ev.ts)
        self.entry_meta[o.order_id] = {"stop_loss": info["stop"], "signal_id": req.client_id,
                                       "context": {k: _jsonable((regime_rec or {}).get(k)) for k in (
                                           "trend", "suitability", "confidence_level")}}
        self._record_decision(root, ev.ts, req.client_id, req.side.value, True, "", d.stages, regime_rec)
        if req.client_id:
            self.strategies[root].on_order_event(OrderEvent(req.client_id, _num(o.order_id), "accepted", ev.ts,
                                                            "signal"))

    # ------------------------------------------------------------------ shutdown
    def shutdown(self, reason: str) -> None:
        """Safe shutdown: no new entries, pending entries cancelled, positions either kept with their
        protective orders (default) or flattened on the next available market event, state persisted."""
        self.accepting_entries = False
        ts = self.now()
        for o in self.broker.working(roles=("entry",)):
            self.broker.cancel(o.order_id, ts, f"shutdown:{reason}")
        open_pos = {r: self.account.position_qty(r) for r in self.roots if self.account.position_qty(r)}
        if self.pcfg.shutdown_policy == "flatten" and open_pos and self.feed.connected:
            self.flatten_all("shutdown", ts)
            for _ in range(50):  # only real (feed) prices can fill the exits -- never a fabricated price
                try:
                    batch = self.feed.poll(100)
                except FeedError:
                    break
                if not batch:
                    break
                for ev in batch:
                    self.process_event(ev)
                if not any(self.account.position_qty(r) for r in self.roots):
                    break
        self._flush_broker_log()
        remaining = {r: self.account.position_qty(r) for r in self.roots if self.account.position_qty(r)}
        self.stopped_reason = reason
        self.clean_shutdown = True
        self._journal("shutdown", {"reason": reason, "policy": self.pcfg.shutdown_policy,
                                   "open_positions_at_shutdown": remaining,
                                   "working_protective_orders": [o.order_id for o in self.broker.working()]}, ts)
        self._commit()
        try:
            self.feed.disconnect()
        except FeedError:
            pass

    def emergency_stop(self, reason: str) -> None:
        """Operator emergency: block entries and flatten on the next market events."""
        self.account.risk.activate_emergency_stop(reason)
        self._journal("emergency_stop", {"reason": reason}, self.now())
        self.flatten_all("emergency_stop", self.now())
        self._flush_broker_log()
        self._commit()


def _num(order_id: str) -> int:
    digits = "".join(ch for ch in order_id if ch.isdigit())
    return int(digits) if digits else 0


def _jsonable(v: Any) -> Any:
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            return str(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)

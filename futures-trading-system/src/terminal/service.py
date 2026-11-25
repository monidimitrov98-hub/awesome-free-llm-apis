"""TerminalService: runs ONE paper session (mock / replay / shadow) in a background thread and
publishes read-only views of it. The service has no method that places, changes or cancels an
order, or that changes any risk / NewsGuard / strategy / execution setting.

Real-time design (separate channels, never a full snapshot per tick):
    market       <= 4 Hz, only when quotes/health changed
    status       1 Hz (account, risk summary, system state)
    activity     whenever the session's activity log advanced (incremental items)
    notifications deduplicated (same key within a window -> one notification)
    heartbeat    every 5 s
Every message carries the server ``boot_id`` and a global ``seq``; a client that sees a gap or a
new boot id fetches a fresh REST snapshot (no duplicate trades/notifications on reconnect).
"""

from __future__ import annotations

import collections
import logging
import math
import os
import platform
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from src.terminal import APP_NAME, APP_VERSION, views
from src.terminal.modes import Built, ModeOptions, build

log = logging.getLogger("fts.terminal")


class RingLogHandler(logging.Handler):
    """Bounded in-memory log for the diagnostics viewer (no secrets are logged anywhere)."""

    def __init__(self, maxlen: int = 2000) -> None:
        super().__init__()
        self.records: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seq += 1
        self.records.append({"seq": self.seq, "time": pd.Timestamp(record.created, unit="s", tz="UTC").isoformat(),
                             "level": record.levelname, "source": record.name, "message": record.getMessage()[:500]})


@dataclass
class Notification:
    id: int
    time: str
    severity: str        # info | warning | critical | success
    category: str
    title: str
    description: str
    root: str | None


class NotificationCenter:
    """Derives notifications from backend events; the same key within ``window`` seconds is
    suppressed (counted, not repeated). Bounded."""

    def __init__(self, window: float = 60.0, maxlen: int = 500) -> None:
        self.window = window
        self.items: collections.deque[Notification] = collections.deque(maxlen=maxlen)
        self._last: dict[str, float] = {}
        self._id = 0
        self.suppressed = 0

    def add(self, key: str, severity: str, category: str, title: str, description: str, root: str | None = None,
            when: float | None = None) -> Notification | None:
        now = time.monotonic() if when is None else when
        last = self._last.get(key)
        if last is not None and now - last < self.window:
            self.suppressed += 1
            return None
        self._last[key] = now
        if len(self._last) > 2000:
            for k in sorted(self._last, key=self._last.get)[:1000]:
                del self._last[k]
        self._id += 1
        n = Notification(self._id, pd.Timestamp.now(tz="UTC").isoformat(), severity, category, title,
                         description[:300], root)
        self.items.append(n)
        return n


class TerminalService:
    def __init__(self, opts: ModeOptions, *, backtest_dir=None, clock: Callable[[], pd.Timestamp] | None = None,
                 publish_interval: float = 0.25) -> None:
        self.opts = opts
        self.boot_id = secrets.token_hex(6)
        self.started_at = pd.Timestamp.now(tz="UTC")
        self._t0 = time.monotonic()
        self.backtest_dir = backtest_dir
        self.clock = clock
        self.publish_interval = publish_interval
        self.logs = RingLogHandler()
        logging.getLogger("fts").addHandler(self.logs)
        logging.getLogger("fts").setLevel(logging.INFO)
        self.notifications = NotificationCenter()
        self.state = "created"           # created | starting | running | stopped | failed
        self.outcome: str | None = None
        self.error: str | None = None
        self.built: Built | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pub: threading.Thread | None = None
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self._bars_cache: dict = {}
        self._bars_lock = threading.Lock()
        self._proc = None  # psutil.Process, kept so cpu_percent() measures since the previous call
        self._last_market: Any = None
        self._last_activity_seq = 0
        self._prev_health: dict[str, str] = {}
        self._prev_news: dict[str, str] = {}
        self.ws_clients = 0
        self._analytics = None          # Sprint 8 AnalyticsLibrary (lazy; read-only journal analysis)
        self._analytics_error: str | None = None
        self._analytics_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self.analytics_config = None    # tests may point this at another config/analytics.toml

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        # one session per service, ever: a second start() would build a SECOND PaperSession on the same state
        # directory (two writers on one journal). Re-entry is refused, never silently restarted.
        with self._start_lock:
            if self.state != "created":
                log.warning("terminal service already %s: start() ignored (one session per service)", self.state)
                return
            self.state = "starting"
        try:
            self.built = build(self.opts, clock=self.clock)
        except Exception as exc:  # noqa: BLE001 -- shown in the GUI, never silently replaced by another mode
            self.state, self.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
            log.error("mode %s could not start: %s", self.opts.mode, self.error)
            self._start_publisher()
            return
        b = self.built
        if "streamer" in b.extras:
            b.extras["streamer"].start()
        self._thread = threading.Thread(target=self._run_session, name="terminal-session", daemon=True)
        self._thread.start()
        self._start_publisher()
        log.info("terminal started in %s mode (%s data)", b.info.label, b.info.data_label)

    def _start_publisher(self) -> None:
        self._pub = threading.Thread(target=self._publish_loop, name="terminal-publisher", daemon=True)
        self._pub.start()

    def _run_session(self) -> None:
        b = self.built
        self.state = "running"
        try:
            self.outcome = b.session.run(stop_when_exhausted=b.stop_when_exhausted, idle_sleep=b.idle_sleep,
                                         should_stop=self._stop.is_set)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.outcome = "system_error"
        self.state = "failed" if self.outcome in ("system_error", "feed_unavailable") else "stopped"
        log.warning("paper session ended: %s", self.outcome)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        b = self.built
        if b is not None and "streamer" in b.extras:
            b.extras["streamer"].stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._pub is not None:
            self._pub.join(timeout)
        with self._analytics_lock:
            if self._analytics is not None:
                self._analytics.close()  # release read handles before an ephemeral journal is removed
                self._analytics = None
            if getattr(self, "_run_lib", None) is not None:
                self._run_lib.close()
                self._run_lib = None
        if b is not None:
            try:
                b.feed.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                with b.session.state_lock:
                    b.session.journal.close()
            except Exception:  # noqa: BLE001
                pass
            if b.extras.get("ephemeral_state"):  # MOCK / REPLAY runs keep no state after the terminal stops
                import shutil

                shutil.rmtree(b.session.journal.path.parent, ignore_errors=True)
        logging.getLogger("fts").removeHandler(self.logs)

    # ------------------------------------------------------------------ publishing
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _emit(self, topic: str, data: Any) -> None:
        with self._seq_lock:
            self._seq += 1
            msg = {"type": topic, "seq": self._seq, "boot_id": self.boot_id,
                   "ts": pd.Timestamp.now(tz="UTC").isoformat(), "data": data}
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:  # noqa: BLE001 -- a broken listener never stops publishing
                log.exception("listener failed")

    @property
    def seq(self) -> int:
        return self._seq

    def _publish_loop(self) -> None:
        last_status = last_hb = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if self.built is not None:
                    m = self.market()
                    key = (m["instruments"], m["feed"].get("authenticated"), m["validation"])
                    if key != self._last_market:
                        self._last_market = key
                        self._emit("market", m)
                    items = self.activity(self._last_activity_seq)
                    if items:
                        self._last_activity_seq = items[-1]["seq"]
                        self._emit("activity", items)
                        for n in self._derive_notifications(items):
                            self._emit("notification", n)
                    if now - last_status >= 1.0:
                        last_status = now
                        st = self.status()
                        self._emit("status", st)
                        for n in self._state_notifications(m, st):
                            self._emit("notification", n)
                if now - last_hb >= 5.0:
                    last_hb = now
                    self._emit("heartbeat", {"state": self.state, "ws_clients": self.ws_clients})
            except Exception:  # noqa: BLE001
                log.exception("publisher cycle failed")
            self._stop.wait(self.publish_interval)

    # ------------------------------------------------------------------ notifications
    def _note(self, *args, **kw) -> dict | None:
        n = self.notifications.add(*args, **kw)
        return vars(n) if n else None

    def _derive_notifications(self, items: list[dict]) -> list[dict]:
        out = []
        for a in items:
            k, p, root = a["kind"], a["payload"], a.get("root")
            n = None
            if k == "entry_decision":
                if p.get("allowed"):
                    n = self._note(f"sig:{p.get('signal_id')}", "info", "strategy", "Strategy signal authorized",
                                   f"{root} {p.get('side')} -- simulated paper order placed", root)
                else:
                    n = self._note(f"rej:{root}:{p.get('reason')}", "warning", "strategy", "Strategy signal rejected",
                                   f"{root} {p.get('side')}: {p.get('reason')}", root)
            elif k == "execution" and p.get("role") == "entry":
                n = self._note(f"fill:{p.get('exec_id')}", "success", "position", "Paper position opened",
                               a["summary"], root)
            elif k == "trade_closed":
                n = self._note(f"closed:{a['seq']}", "info", "position", "Paper position closed", a["summary"], root)
            elif k == "account_guard":
                n = self._note(f"guard:{p.get('state')}", "critical", "risk", "AccountGuard", a["summary"], root)
            elif k == "daily_loss_limit":
                n = self._note(f"dll:{p.get('session_date')}", "critical", "risk", "Daily loss limit reached",
                               f"session {p.get('session_date')}: entries blocked", root)
            elif k == "feed_disconnected":
                n = self._note("feed:down", "critical", "market_data", "Market data disconnected",
                               str(p.get("error"))[:200], None)
            elif k == "feed_connected":
                n = self._note("feed:up", "success", "market_data", "Market data connected",
                               "fresh validated data is still required before entries", None)
            elif k == "feed_health" and p.get("action") == "block":
                n = self._note(f"block:{root}:{p.get('reason')}", "warning", "market_data", "Data-health block",
                               str(p.get("reason")), root)
            elif k == "system_error":
                n = self._note("syserr", "critical", "system", "Backend error", str(p.get("error"))[:200], None)
            elif k == "flatten_requested":
                n = self._note(f"flat:{root}:{p.get('reason')}", "critical", "risk", "Flatten requested",
                               a["summary"], root)
            if n:
                out.append(n)
        return out

    def _state_notifications(self, m: dict, st: dict) -> list[dict]:
        out = []
        for r, v in m["instruments"].items():
            h = str(v.get("health"))
            prev = self._prev_health.get(r)
            if prev is not None and prev != h:
                if "stale" in h:
                    n = self._note(f"stale:{r}", "warning", "market_data", "Market data stale", f"{r}: {h}", r)
                elif h == "ok" and prev != "ok":
                    n = self._note(f"restored:{r}", "success", "market_data", "Market data healthy", f"{r}: data ok", r)
                else:
                    n = None
                if n:
                    out.append(n)
            self._prev_health[r] = h
        for r, nv in (st.get("news") or {}).items():
            s = str(nv.get("state"))
            if self._prev_news.get(r) not in (None, s) and s == "BLACKOUT_ACTIVE":
                n = self._note(f"news:{r}", "warning", "news", "News blackout active", f"{r}: entries blocked", r)
                if n:
                    out.append(n)
            self._prev_news[r] = s
        rm = st.get("daily_loss_used_pct")
        if rm is not None and rm >= 75:
            n = self._note(f"dlw:{st.get('session_date')}", "warning", "risk", "Daily loss warning",
                           f"{rm:.0f}% of the daily loss limit used", None)
            if n:
                out.append(n)
        return out

    # ------------------------------------------------------------------ read-only views
    def _locked(self, fn: Callable[[Any], Any]) -> Any:
        b = self.built
        if b is None:
            return None
        with b.session.state_lock:
            return fn(b.session)

    def meta(self) -> dict[str, Any]:
        b = self.built
        info = b.info if b else None
        return views.clean({
            "app": APP_NAME, "version": APP_VERSION, "boot_id": self.boot_id, "started_at": self.started_at,
            "state": self.state, "outcome": self.outcome, "error": self.error,
            "mode": info.label if info else self.opts.mode.upper(), "data_label": info.data_label if info else None,
            "description": info.description if info else None,
            "run_kind": b.session.run_kind if b else None,
            "instruments": list(b.contracts) if b else [], "contracts": b.contracts if b else {},
            "timeframes": list(views.TIMEFRAMES), "read_only": True, "order_entry": False,
            "modes": {"MOCK": {"available": True}, "REPLAY": {"available": True},
                      "SHADOW": {"available": True, "note": "needs NinjaTrader + config/marketdata.toml + secret"},
                      "PAPER": {"available": False, "note": "not implemented in the backend (Sprint 8)"}},
        })

    def market(self) -> dict | None:
        b = self.built
        return self._locked(lambda s: views.market(s, b.contracts))

    def status(self) -> dict[str, Any] | None:
        def f(s):
            acc = views.account(s)
            now = s.now()
            sd = s.session_of(now) if now.value > 0 else s.account_session
            sd = sd or s.account_session
            rc = s.account.risk.config
            daily = s.account.risk.daily_pnl(sd, 0.0) if sd is not None else None
            news = {}
            for r, g in s.news.items():
                try:
                    st = g.status(now)
                    news[r] = {"state": st.state.value, "safe": st.safe}
                except Exception as exc:  # noqa: BLE001
                    news[r] = {"state": "ERROR", "safe": False, "error": type(exc).__name__}
            entries = {r: dict(zip(("ok", "reason"), s.system_state(r, now))) for r in s.roots}
            eq = s.account.equity()
            g = s.account.guard
            return views.clean({
                "account": acc, "news": news, "entries": entries,
                "guard": {"enabled": g.enabled, "breached": g.breached, "locked_permanently": g.locked_permanently,
                          "remaining_buffer": (g.remaining_buffer(eq) if g.enabled and math.isfinite(eq) else None),
                          "block": g.entry_block_reason(s.account.equity(), sd) if g.enabled else None},
                "session_date": sd, "daily_loss_used_pct": (max(0.0, -daily) / rc.max_daily_loss * 100
                                                            if rc.max_daily_loss and daily is not None else None),
                "accepting_entries": s.accepting_entries, "mode_authorizes_entries": s.pcfg.mode != "infrastructure_validation",
                "runtime": {"state": self.state, "outcome": self.outcome, "events": s.seq,
                            "stopped_reason": s.stopped_reason},
            })
        return self._locked(f)

    def activity(self, since: int = 0, limit: int = 500) -> list[dict]:
        return self._locked(lambda s: views.activity(s, since, limit=limit)) or []

    def snapshot(self) -> dict[str, Any]:
        """One consistent snapshot: every part is read under a single hold of the session lock."""
        b = self.built
        lock = b.session.state_lock if b is not None else threading.RLock()
        with lock:
            return {"meta": self.meta(), "seq": self._seq, "market": self.market(), "status": self.status(),
                    "activity": self.activity(0, 200), "notifications": [vars(n) for n in self.notifications.items][-100:]}

    def bars(self, root: str, tf: str, limit: int) -> dict | None:
        """Chart data. Only the inputs are copied under the session lock; the indicator computation
        (the expensive part) runs outside it, so chart requests never stall event processing."""
        b = self.built
        if b is None or root not in b.session.roots:
            return None
        with b.session.state_lock:
            inputs = views.bars_inputs(b.session, root, tf)
        return views.bars_from_inputs(b.session, inputs, root, tf, limit, self._bars_cache, self._bars_lock)

    def strategy(self, root: str):
        return self._locked(lambda s: views.strategy(s, root) if root in s.roots else None)

    def analysis(self, root: str):
        return self._locked(lambda s: views.analysis(s, root) if root in s.roots else None)

    def positions(self):
        return self._locked(views.positions)

    def risk(self):
        return self._locked(views.risk)

    def journal(self, page: int, page_size: int, **filters):
        return self._locked(lambda s: views.journal(s, page, page_size, **filters))

    def journal_rows(self, **filters):
        return self._locked(lambda s: views.journal_rows(s, **filters)) or []

    def performance(self, source: str):
        if source == "backtest":
            return views.backtest_performance(self.backtest_dir)
        return self._locked(views.performance)

    # ------------------------------------------------------------------ shadow analytics (Sprint 8, read-only)
    SHADOW_REPORTS = ("sessions", "performance", "costs", "execution", "regimes", "blocked", "reliability", "account",
                      "validation", "session_report")

    def _library(self):
        """The analytics library: configured journals + this terminal's own session journal."""
        from src.analytics.shadow.library import AnalyticsLibrary, load_analytics_config, resolve_journals

        if self._analytics is None:
            cfg = load_analytics_config(self.analytics_config)
            lib = AnalyticsLibrary(resolve_journals(cfg["journals"], cfg["max_journals"]), cfg)
            b = self.built
            if b is not None:
                cur = b.session.journal.path.resolve()
                label = f"this terminal ({b.info.label})"
                src = lib.sources.get(str(cur))
                if src is not None:
                    src.label = src.data.label = label
                else:
                    lib.add(cur, label=label)
            self._analytics = lib
        return self._analytics

    def shadow(self, report: str, filters: dict[str, Any], **kw) -> dict[str, Any]:
        """One Shadow Analytics report. Never touches the trading session (journals are read separately)."""
        from src.analytics.shadow.library import Filters, to_json

        f = Filters(**filters)
        with self._analytics_lock:  # one analysis at a time: bounded CPU, consistent caches
            try:
                lib = self._library()
            except Exception as exc:  # noqa: BLE001 -- configuration problems are shown, never fatal
                return {"unavailable": f"analytics configuration error: {type(exc).__name__}: {str(exc)[:200]}"}
            if report == "compare":
                rep = lib.compare(kw["root"], kw.get("a", "REPLAY"), kw.get("b", "SHADOW"), f)
            elif report == "validation":
                rep = lib.validation()
            elif report == "journals":
                lib.refresh()
                rep = {"journals": lib.journal_status()}
            elif report == "trace":
                rep = lib.trace(kw["run_id"], kw["seq"])
                if rep is None:
                    raise LookupError("no entry decision with this run id and journal sequence")
            else:
                rep = getattr(lib, report)(f)
            import json

            return json.loads(to_json(rep, {"report": report, "filters": f.describe(),
                                            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
                                            "terminal_mode": self.built.info.label if self.built else None}))

    def shadow_export(self, kind: str, fmt: str, filters: dict[str, Any]) -> tuple[str, str]:
        from src.analytics.shadow.library import Filters, export_meta, export_rows, to_csv, to_json

        f = Filters(**filters)
        with self._analytics_lock:
            try:
                lib = self._library()
            except Exception as exc:  # noqa: BLE001
                raise LookupError(f"analytics configuration error: {type(exc).__name__}") from exc
            rows, rep = export_rows(lib, kind, f)
            if rep.get("refused"):
                raise LookupError(rep["refused"])
            meta = export_meta(kind, f, rep)
            body = to_csv(rows, meta) if fmt == "csv" else to_json({"rows": rows}, meta)
            return body, f"shadow_{kind}_{(f.session_type or 'all').lower()}.{fmt}"

    # ------------------------------------------------------------------ operational readiness (Sprint 10)
    def readiness(self) -> dict[str, Any]:
        """One evidence-based verdict for the running simulation: BLOCKED, INCOMPLETE or OPERATIONAL
        (SIMULATION). It is never "SAFE": a simulation that is operational is still only a simulation, and
        any missing or degraded evidence makes the verdict INCOMPLETE."""
        from src.risk.prop_firms.engine import ComplianceEngine

        checks: list[dict[str, Any]] = []

        def add(name, state, detail):
            checks.append({"check": name, "state": state, "detail": detail})

        b = self.built
        if b is None:
            add("session", "BLOCK", self.error or "session not started")
        else:
            def f(s):
                add("session", "BLOCK" if self.state == "failed" else "OK" if self.state == "running" else "WARN",
                    f"{self.state}" + (f" ({self.outcome})" if self.outcome else ""))
                add("persistence", "OK" if s.persistence_ok else "BLOCK",
                    "journal commits succeed" if s.persistence_ok else "journal commits failing: entries blocked")
                add("reconciliation", "OK" if s.reconciliation_ok else "BLOCK",
                    "state reconciled with the journal" if s.reconciliation_ok else "state does not reconcile: entries blocked")
                now = s.now()
                for r in s.roots:
                    ok, why = s.system_state(r, now)
                    add(f"market data {r}", "OK" if ok else "WARN", "healthy" if ok else why)
                g = s.account.guard
                if not g.enabled:
                    add("AccountGuard", "WARN", "disabled: no account-survival floor")
                elif g.breached or g.locked_permanently:
                    add("AccountGuard", "BLOCK", "breached" if g.breached else "locked permanently")
                else:
                    add("AccountGuard", "OK", f"enabled ({g.rules.drawdown_type}, {g.rules.max_drawdown:g})")
                for r, ng in s.news.items():
                    try:
                        st = ng.status(now)
                        add(f"NewsGuard {r}", "OK" if st.safe else "WARN", st.state.value)
                    except Exception as exc:  # noqa: BLE001
                        add(f"NewsGuard {r}", "UNKNOWN", f"state unavailable ({type(exc).__name__}): entries blocked")
                if s.pcfg.mode == "infrastructure_validation":
                    add("run mode", "INFO", "infrastructure validation: entries are never authorized")
                if s.prop is None:
                    add("prop firm profile", "INFO", "none selected (AccountGuard and RiskManager only)")
                else:
                    s.prop.refresh_operational()
                    dec = s.prop.evaluate_entry(s.last_event_ts or now, s.roots[0], 1, s.prop_snapshot(), scope="status",
                                                record=False)
                    cls = ComplianceEngine.classify(dec, s.prop.breached)
                    state = {"SAFE": "OK", "WARNING": "WARN", "BLOCKED": "BLOCK"}.get(cls, "UNKNOWN")
                    if not s.prop.recovery_ok or s.prop.breached:
                        state = "BLOCK"
                    add("prop firm profile", state, f"{s.prop.profile.key}: {cls}"
                        + ("" if s.prop.operational.get("operational") else
                           f" (not operational: {s.prop.operational.get('not_operational_reason')})"))
                return True
            self._locked(f)
            add("data", "INFO", f"{b.info.label} mode, {b.info.data_label} data")
            try:
                rep = self._run_report()
                comp = rep.get("completeness", {})
                add("shadow results", "OK" if comp.get("status") == "COMPLETE" else "WARN",
                    "complete" if comp.get("status") == "COMPLETE" else "; ".join(comp.get("reasons", []))[:500]
                    or comp.get("status", "unknown"))
            except Exception as exc:  # noqa: BLE001 -- unknown evidence is never reported as complete
                add("shadow results", "UNKNOWN", f"analysis unavailable ({type(exc).__name__})")
        states = {c["state"] for c in checks}
        verdict = "BLOCKED" if "BLOCK" in states else "INCOMPLETE" if states & {"WARN", "UNKNOWN"} \
            else "OPERATIONAL (SIMULATION)"
        cont = not any(c["state"] == "BLOCK" and c["check"] in ("session", "persistence", "reconciliation")
                       for c in checks)
        return {"verdict": verdict, "checks": checks, "continue_simulation": cont,
                "continue_reason": "protections and persistence intact" if cont else
                "the simulation is stopped or its state cannot be trusted",
                "note": "Simulation only: no order can reach a broker. OPERATIONAL means every check passed; it is not a "
                        "statement that trading would be safe or profitable.",
                "evaluated_at": pd.Timestamp.now(tz="UTC").isoformat()}

    def _run_report(self) -> dict[str, Any]:
        """Session report of THIS terminal's own journal (incremental library, cached per journal)."""
        from src.analytics.shadow.library import AnalyticsLibrary, Filters, load_analytics_config

        b = self.built
        with self._analytics_lock:
            lib = getattr(self, "_run_lib", None)
            if lib is None:
                lib = self._run_lib = AnalyticsLibrary([], load_analytics_config(self.analytics_config),
                                                       extra={f"this terminal ({b.info.label})": b.session.journal.path})
            return lib.session_report(Filters())

    # ------------------------------------------------------------------ quant research (Sprint 11, read-only)
    def research_overview(self) -> dict[str, Any]:
        from src.config import load_config
        from src.research.views import overview

        return overview(load_config(), root=getattr(self.opts, "research_root", None),
                        governance_path=getattr(self.opts, "governance_path", None))

    def research_run(self, attempt: str) -> dict[str, Any] | None:
        from src.research.views import run_detail

        return run_detail(attempt, root=getattr(self.opts, "research_root", None))

    # ------------------------------------------------------------------ prop firm center (Sprint 9, read-only)
    def _prop_registry(self):
        from src.risk.prop_firms.versions import Registry

        return self.opts.prop_registry or Registry()

    def propfirm_status(self) -> dict[str, Any]:
        """Current compliance state of THIS session's profile (backend values only)."""
        from src.risk.prop_firms.authority import unified_decision
        from src.risk.prop_firms.engine import ComplianceEngine
        from src.risk.prop_firms.payout import payout_eligibility

        def f(s):
            if s.prop is None:
                return {"selected": False, "note": "No prop firm profile is selected for this session: entries are "
                        "governed by AccountGuard and RiskManager only. Select one in config/prop_firm.toml "
                        "(exact id and version) or with --prop-profile."}
            now = s.last_event_ts or pd.Timestamp.now(tz="UTC")
            s.prop.refresh_operational()  # a revocation must never stay hidden behind a cached state
            snap = s.prop_snapshot()
            root = s.roots[0]
            dec = s.prop.evaluate_entry(now, root, 1, snap, scope="status", record=False)
            sd = s.account_session
            eq = s.account.equity()
            ag = s.account.guard.entry_block_reason(eq, sd) if sd is not None else "outside_session"
            rd = s.account.risk.check_entry(when=now, session_date=sd, quantity=1) if sd is not None else None
            ok, why = s.system_state(root, now)
            unified = unified_decision(
                emergency=(ok and not s.account.risk.emergency_active, why or ("emergency_stop" if s.account.risk.emergency_active else "")),
                account_guard=ag, prop_firm=dec.to_dict(), risk_manager=(rd.approved, rd.reason) if rd else None)
            return {"selected": True, "status": ComplianceEngine.classify(dec, s.prop.breached),
                    "summary": s.prop.summary(snap, now), "entry_check": {**dec.to_dict(), "nominal": f"1 x {root} entry now"},
                    "unified": unified, "payout": payout_eligibility(s.prop),
                    "evaluated_at": now.isoformat(), "timezone": s.prop.profile.timezone}
        out = self._locked(f)
        return out if out is not None else {"selected": False, "note": "session not started"}

    def propfirm_profiles(self) -> dict[str, Any]:
        from src.risk.prop_firms.versions import Activations, operational_state

        reg = self._prop_registry()
        acts = self.opts.prop_activations or Activations()
        rows = []
        for key, p in sorted(reg.profiles.items()):
            st = operational_state(p, acts)
            rows.append({"key": key, "id": p.id, "version": p.version, "firm": p.firm, "product": p.product,
                         "stage": p.stage, "account_size": p.account_size, "account_type": p.account_type,
                         "status": st["status"], "operational": st["operational"],
                         "not_operational_reason": st["not_operational_reason"],
                         "unverified_rules": st["unverified_rules"], "incomplete_rules": st["incomplete_rules"],
                         "safety_critical_unresolved": st["safety_critical_unresolved"],
                         "retrieved": p.verification.get("retrieved"), "reviewed_by": bool(p.verification.get("reviewed_by")),
                         "reviewed_on": p.verification.get("reviewed_on") or None, "ruleset_sha256": p.ruleset_sha256[:16]})
        return {"profiles": rows, "invalid_files": sorted(reg.errors)}

    def propfirm_profile(self, key: str) -> dict[str, Any] | None:
        from dataclasses import asdict

        from src.risk.prop_firms.versions import Activations, operational_state

        reg = self._prop_registry()
        p = reg.profiles.get(key)
        if p is None:
            return None
        return {"key": key, "firm": p.firm, "product": p.product, "stage": p.stage, "account_size": p.account_size,
                "account_type": p.account_type, "initial_balance": p.initial_balance, "timezone": p.timezone,
                "day_start": p.day_start, "description": p.description, "verification": p.verification,
                "status": operational_state(p, self.opts.prop_activations or Activations()),
                "internal_limits": {**asdict(p.internal), "note": "our own margins, NOT firm rules"},
                "rules": [asdict(r) for r in p.rules], "sources": [asdict(s) for s in p.sources],
                "profile_sha256": p.sha256, "ruleset_sha256": p.ruleset_sha256,
                "versions": [x.version for x in reg.versions(p.id)]}

    def propfirm_compare(self, a: str, b: str) -> dict[str, Any] | None:
        from src.risk.prop_firms.versions import diff

        reg = self._prop_registry()
        pa, pb = reg.profiles.get(a), reg.profiles.get(b)
        if pa is None or pb is None:
            return None
        return diff(pa, pb)

    def propfirm_history(self) -> dict[str, Any] | None:
        from src.risk.prop_firms.reports import ComplianceJournalReader

        b = self.built
        if b is None:
            return None
        with self._analytics_lock:  # one incremental reader per terminal: only new rows are verified / parsed
            rd = getattr(self, "_prop_reader", None)
            if rd is None or rd.path != b.session.journal.path:
                rd = self._prop_reader = ComplianceJournalReader(b.session.journal.path)
            return rd.refresh().report()

    def system(self) -> dict[str, Any]:
        proc = {}
        try:
            import psutil

            if self._proc is None:
                self._proc = psutil.Process(os.getpid())
                self._proc.cpu_percent(interval=None)  # baseline; the first reading is meaningless
                time.sleep(0.05)
            p = self._proc
            proc = {"cpu_percent": p.cpu_percent(interval=None), "memory_mb": round(p.memory_info().rss / 1e6, 1),
                    "threads": p.num_threads()}
        except Exception:  # noqa: BLE001 -- optional dependency: shown as unavailable
            proc = {"cpu_percent": None, "memory_mb": None, "threads": threading.active_count()}

        def f(s):
            feed = s.feed.status()
            errs = [a for a in s.activity if a["kind"] in ("system_error", "persistence_failure")]
            gate = next((g for g in s.strategies.values() if hasattr(g, "errors")), None)
            return {"runtime": {"run_kind": s.run_kind, "events_processed": s.seq, "accepting_entries": s.accepting_entries,
                                "reconciliation_ok": s.reconciliation_ok, "stopped_reason": s.stopped_reason,
                                "started_at": s.started_at},
                    "journal": {"entries": s.journal.seq, "persistence_ok": s.persistence_ok,
                                "keyed": s.journal.keyed},
                    "strategy": {r: getattr(s.inner[r], "name", "?") for r in s.roots},
                    "regime": {"enabled": gate is not None, "errors": getattr(gate, "errors", None)},
                    "news": {"readiness": (s.news_readiness or {}).get("verdict"), "mode": (s.news_readiness or {}).get("mode")},
                    "feed": feed, "last_error": (errs[-1]["payload"].get("error") if errs else None),
                    "latency": s.status()["latency"]}
        return views.clean({"process": {**proc, "uptime_seconds": round(time.monotonic() - self._t0, 1),
                                        "python": platform.python_version(), "platform": platform.system()},
                            "terminal": {"state": self.state, "outcome": self.outcome, "error": self.error,
                                         "ws_clients": self.ws_clients, "published": self._seq,
                                         "notifications_suppressed": self.notifications.suppressed},
                            "session": self._locked(f)})

    def logs_view(self, level: str | None, q: str | None, limit: int) -> list[dict]:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        minimum = order.get((level or "DEBUG").upper(), 10)
        rows = [r for r in self.logs.records if order.get(r["level"], 0) >= minimum]
        def lvl(a):
            if a["kind"] in ("system_error", "persistence_failure"):
                return "ERROR"
            if a["kind"] == "feed_health":
                return "WARNING" if a["payload"].get("action") in ("block", "flag") else "INFO"
            return "WARNING" if a["kind"] in views.PROTECTION_KINDS or a["kind"] == "data_rejected" else "INFO"
        acts = [{"seq": a["seq"], "time": a["time"], "level": lvl(a),
                 "source": f"session.{a['kind']}", "message": a["summary"]} for a in self.activity(0, 500)]
        rows = sorted(rows + [a for a in acts if order[a["level"]] >= minimum], key=lambda r: str(r["time"]))
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in r["message"].lower() or ql in r["source"].lower()]
        return rows[-limit:]

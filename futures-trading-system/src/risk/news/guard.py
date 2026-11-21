"""NewsGuard: the independent, fail-closed authorization gate for NEW entries.

The backtest engine asks NewsGuard for every new entry, at the signal and again at the
fill, BEFORE the risk manager and the AccountGuard. Strategies, the regime engine and
any LLM never receive a reference to it, so they cannot override it. NewsGuard is never
consulted for exits, stop-losses, take-profits, flattening or AccountGuard actions.

States (highest priority first):

=====================  =====================================================
EMERGENCY_BLACKOUT     an operator emergency window or unexpected official announcement
PROVIDER_UNAVAILABLE   an expected provider's latest refresh failed (or it never succeeded)
CALENDAR_STALE         last successful refresh too old, or the calendar does not cover the next lookahead
CALENDAR_INCOMPLETE    a required event category has no healthy provider covering it
RECOVERY_PENDING       refreshes succeed again but not yet ``recovery_successes`` in a row
DATA_CONFLICT          equally reliable sources disagree about an event in its window
EVENT_UNKNOWN          an event with unknown time/impact covers ``now``
BLACKOUT_ACTIVE        inside a scheduled event's blackout window
NORMAL                 none of the above
DISABLED               the filter is disabled in the configuration
=====================  =====================================================

Every state except NORMAL/DISABLED blocks new entries in ``enforce`` mode; ``observe`` mode
records the same decisions without blocking. Recovery needs successful refreshes recorded
in the refresh log; time passing alone never clears a failure.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pandas as pd

from src.risk.news.blackout import Window, event_windows
from src.risk.news.calendar import CalendarStore
from src.risk.news.config import NewsConfig


class NewsState(StrEnum):
    NORMAL = "NORMAL"
    BLACKOUT_ACTIVE = "BLACKOUT_ACTIVE"
    CALENDAR_STALE = "CALENDAR_STALE"
    CALENDAR_INCOMPLETE = "CALENDAR_INCOMPLETE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DATA_CONFLICT = "DATA_CONFLICT"
    EVENT_UNKNOWN = "EVENT_UNKNOWN"
    EMERGENCY_BLACKOUT = "EMERGENCY_BLACKOUT"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    DISABLED = "DISABLED"


STATE_ORDER = [NewsState.EMERGENCY_BLACKOUT, NewsState.PROVIDER_UNAVAILABLE, NewsState.CALENDAR_STALE,
               NewsState.RECOVERY_PENDING, NewsState.CALENDAR_INCOMPLETE, NewsState.DATA_CONFLICT, NewsState.EVENT_UNKNOWN,
               NewsState.BLACKOUT_ACTIVE, NewsState.NORMAL]
KIND_STATE = {"emergency": NewsState.EMERGENCY_BLACKOUT, "unexpected": NewsState.EMERGENCY_BLACKOUT,
              "conflict": NewsState.DATA_CONFLICT, "unknown_time": NewsState.EVENT_UNKNOWN,
              "unknown_impact": NewsState.EVENT_UNKNOWN, "scheduled": NewsState.BLACKOUT_ACTIVE}


@dataclass
class NewsStatus:
    time: pd.Timestamp
    state: NewsState
    reasons: list[str]
    active: list[Window]
    next_window: Window | None
    calendar_age_minutes: float | None
    provider_health: dict[str, Any]

    @property
    def safe(self) -> bool:
        return self.state in (NewsState.NORMAL, NewsState.DISABLED)


@dataclass
class NewsDecision:
    allowed: bool        # what the engine must do (always True in observe mode)
    would_block: bool    # what enforce mode would do
    status: NewsStatus
    record: dict[str, Any] = field(default_factory=dict)


# a provider-level source "covers" a category only if its own data contains a future event of it
# (or of the release that carries it)
CATEGORY_SOURCES = {"core_cpi": {"core_cpi", "cpi"}, "core_pce": {"core_pce", "pce"},
                    "unemployment_rate": {"unemployment_rate", "nfp"},
                    "fomc_statement": {"fomc_statement", "fomc_decision"},
                    "fed_press_conference": {"fed_press_conference", "fomc_decision"}}
OPERATIONAL_CLASSES = ("verified_live", "manual")


class NewsGuard:
    """``operational=True`` (paper/live use) applies the same coverage rules as the readiness
    report: only verified-live or manual data counts, synthetic data blocks every entry."""

    def __init__(self, cfg: NewsConfig, store: CalendarStore, instrument: str, *, operational: bool = False) -> None:
        self.cfg = cfg
        self.store = store
        self.instrument = instrument
        self.operational = operational
        cfg.regions_for(instrument)  # every traded instrument needs explicit rules (raises otherwise)
        self.decisions: list[dict[str, Any]] = []
        self.flatten_events: list[dict[str, Any]] = []
        self._known = sorted(r.known_at for r in store.records)
        self._cache: tuple[int, list[Window]] | None = None

    # ------------------------------------------------------------------ evaluation
    def windows_at(self, now: pd.Timestamp) -> list[Window]:
        n = bisect.bisect_right(self._known, now) if len(self._known) == len(self.store.records) else -1
        if n < 0:  # store changed since construction (live refresh)
            self._known = sorted(r.known_at for r in self.store.records)
            n = bisect.bisect_right(self._known, now)
        if self._cache is not None and self._cache[0] == n:
            return self._cache[1]
        wins = event_windows(self.store.as_of(now, self.cfg.conflict_policy), self.cfg, self.instrument)
        self._cache = (n, wins)
        return wins

    def _provider_health(self, p: str, refreshes, now: pd.Timestamp) -> tuple[dict[str, Any], list[tuple[NewsState, str]], float | None]:
        attempts = [r for r in refreshes if r.provider == p]
        oks = [r for r in attempts if r.ok]
        fails = [r for r in attempts if not r.ok]
        problems: list[tuple[NewsState, str]] = []
        cls = self.store.provider_class.get(p, "unknown")
        h: dict[str, Any] = {"last_success": None, "last_failure": fails[-1].time.isoformat() if fails else None,
                             "status": "ok", "data_class": cls, "consecutive_failures": 0}
        for r in reversed(attempts):
            if r.ok:
                break
            h["consecutive_failures"] += 1
        if not oks:
            h["status"] = "never_succeeded"
            problems.append((NewsState.PROVIDER_UNAVAILABLE if fails else NewsState.CALENDAR_STALE,
                             f"provider {p}: no successful refresh"))
            return h, problems, None
        last_ok = oks[-1]
        h["last_success"] = last_ok.time.isoformat()
        age = now - last_ok.time
        if cls == "verified_live" and age > self.cfg.refresh_interval:
            h["data_class"] = "cached_live"  # live data, but not refreshed within the refresh interval
        if fails and fails[-1].time > last_ok.time:
            h["status"] = "degraded_cached"  # latest refresh failed; cached data used (use_fresh_cache only)
            if self.cfg.provider_failure_policy == "block_entries" or age > self.cfg.max_age:
                h["status"] = "failing"
                problems.append((NewsState.PROVIDER_UNAVAILABLE,
                                 f"provider {p}: latest refresh failed ({fails[-1].error or 'error'})"))
        elif fails:
            streak = 0
            for r in reversed(attempts):
                if not r.ok:
                    break
                streak += 1
            if streak < self.cfg.recovery_successes:
                h["status"] = "recovering"
                problems.append((NewsState.RECOVERY_PENDING, f"provider {p}: {streak}/{self.cfg.recovery_successes} "
                                 "successful refreshes since the last failure"))
        if age > self.cfg.max_age:
            h["status"] = "stale"
            problems.append((NewsState.CALENDAR_STALE, f"provider {p}: last successful refresh "
                             f"{age.total_seconds() / 60:.0f} min ago (max {self.cfg.max_age.total_seconds() / 60:.0f})"))
        cs, ce = last_ok.coverage_start, last_ok.coverage_end
        h["category_coverage"] = last_ok.category_coverage
        if last_ok.category_coverage is not None:
            pass  # manual calendar: coverage is judged per category (see _category_problem)
        elif cs is None or ce is None or now < cs or now + self.cfg.lookahead > ce:
            h["status"] = "no_coverage"
            problems.append((NewsState.CALENDAR_STALE, f"provider {p}: calendar does not cover "
                             f"{self.cfg.lookahead.total_seconds() / 60:.0f} min ahead"))
        return h, problems, age.total_seconds() / 60

    def _integrity(self, now: pd.Timestamp) -> tuple[list[tuple[NewsState, str]], dict[str, Any], float | None]:
        refreshes = self.store.refreshes_until(now)
        expected = set(self.store.expected_providers or {r.provider for r in refreshes})
        problems: list[tuple[NewsState, str]] = []
        health: dict[str, Any] = {}
        ages = []
        if not expected:
            problems.append((NewsState.CALENDAR_STALE, "no calendar refresh recorded: event risk unknown"))
        healthy = set()
        for p in sorted(expected | set(self.store.provider_coverage)):
            h, probs, age = self._provider_health(p, refreshes, now)
            health[p] = h
            if not probs:
                healthy.add(p)
            if p in expected:
                problems += probs
                if age is not None:
                    ages.append(age)
        if self.operational and self.store.is_synthetic:
            problems.append((NewsState.CALENDAR_INCOMPLETE, "synthetic calendar data is not allowed in operation"))
        for cat in self.cfg.required_categories:
            providers = [p for p, cats in self.store.provider_coverage.items() if cat in cats]
            why = []
            for p in providers:
                if p not in healthy:
                    why.append((NewsState.CALENDAR_INCOMPLETE, f"{p} unhealthy"))
                    continue
                prob = self.coverage_problem(p, cat, now, self.cfg.lookahead, health,
                                             operational=self.operational)
                if prob is None:
                    why = []
                    break
                why.append(prob)
            else:
                if not providers:
                    why.append((NewsState.CALENDAR_INCOMPLETE, "no provider"))
                # stale verification is reported as CALENDAR_STALE; anything else as CALENDAR_INCOMPLETE
                state = NewsState.CALENDAR_STALE if any(s is NewsState.CALENDAR_STALE for s, _ in why) \
                    else NewsState.CALENDAR_INCOMPLETE
                problems.append((state, f"required category {cat!r} not covered: "
                                 + "; ".join(r for _, r in why)))
        return problems, health, (max(ages) if ages else None)

    def coverage_problem(self, p: str, cat: str, now: pd.Timestamp, horizon: pd.Timedelta, health: dict[str, Any],
                         *, operational: bool, strict: bool = False) -> tuple[NewsState, str] | None:
        """THE coverage rule, shared by entry authorization and the readiness report.
        None when provider ``p`` covers ``cat`` from ``now`` for ``horizon``."""
        h = health.get(p, {})
        ok_states = ("ok",) if strict else ("ok", "degraded_cached")
        if h.get("status") not in ok_states:
            return NewsState.CALENDAR_INCOMPLETE, f"{p}: {h.get('status', 'no refresh')}"
        if operational and self.store.provider_class.get(p) not in OPERATIONAL_CLASSES:
            return NewsState.CALENDAR_INCOMPLETE, f"{p}: data class {self.store.provider_class.get(p, 'unknown')} " \
                                                  "is not operational"
        cc = h.get("category_coverage")
        if cc is not None:
            return self._category_problem(cc, cat, now, horizon)
        last = [r for r in self.store.refreshes_until(now) if r.provider == p and r.ok][-1]
        if last.coverage_end is None or last.coverage_end < now + horizon:
            return NewsState.CALENDAR_INCOMPLETE, f"{p}: covers only through {last.coverage_end}"
        if self.store.provider_class.get(p) not in ("verified_live", "unverified_live", "cached_live"):
            return None  # historical/research store: coverage is attested by the calendar file itself
        wanted = CATEGORY_SOURCES.get(cat, {cat})
        for r in self.store.records:
            if r.source == p and r.category in wanted and r.known_at <= now:
                t = r.scheduled_at if r.scheduled_at is not None else (
                    pd.Timestamp(r.event_date).tz_localize("America/New_York").tz_convert("UTC")
                    + pd.Timedelta(days=1) if r.event_date is not None else None)
                if t is not None and t >= now:
                    return None
        return NewsState.CALENDAR_INCOMPLETE, f"{p}: its data lists no upcoming {cat} event"

    def _category_problem(self, cc: dict[str, dict[str, str]] | None, cat: str, now: pd.Timestamp,
                          horizon: pd.Timedelta) -> tuple[NewsState, str] | None:
        """None when a healthy provider's per-category coverage covers ``cat`` for ``horizon`` ahead."""
        if cc is None:
            return None  # provider-level coverage already checked in _provider_health
        entry = cc.get(cat)
        if entry is None:
            return NewsState.CALENDAR_INCOMPLETE, "manual category unresolved"
        if now - pd.Timestamp(entry["verified_at"]) > self.cfg.manual_max_age:
            return NewsState.CALENDAR_STALE, f"manual category last verified {entry['verified_at']}"
        if pd.Timestamp(entry["through"]) < now + horizon:
            return NewsState.CALENDAR_INCOMPLETE, f"manual category verified only through {entry['through']}"
        return None

    def status(self, now: pd.Timestamp, until: pd.Timestamp | None = None) -> NewsStatus:
        """State at ``now``; with ``until`` every window overlapping [now, until) counts as active
        (a fill can happen anywhere inside an execution bar)."""
        if not self.cfg.enabled:
            return NewsStatus(now, NewsState.DISABLED, ["news filter disabled in config"], [], None, None, {})
        wins = self.windows_at(now)
        end = until if until is not None and until > now else None
        if end is not None:
            active = [w for w in wins if w.start < end and w.end > now]
        else:
            active = [w for w in wins if w.contains(now)]
        nxt = next((w for w in wins if w.start > now), None)
        problems, health, age = self._integrity(now)
        candidates = list(problems) + [(KIND_STATE[w.kind], f"{w.kind} window {w.start}..{w.end}") for w in active]
        state = min((s for s, _ in candidates), key=STATE_ORDER.index, default=NewsState.NORMAL)
        reasons = [r for _, r in sorted(candidates, key=lambda c: STATE_ORDER.index(c[0]))]
        return NewsStatus(now, state, reasons, active, nxt, age, health)

    # ------------------------------------------------------------------ decisions
    def check_entry(self, now: pd.Timestamp, *, purpose: str, client_id: str | None = None,
                    side: str | None = None, until: pd.Timestamp | None = None) -> NewsDecision:
        st = self.status(now, until)
        would_block = not st.safe
        allowed = not (would_block and self.cfg.enforcing)
        w = st.active[0] if st.active else None
        rec = {
            "time": now, "instrument": self.instrument, "purpose": purpose, "client_id": client_id, "side": side,
            "decision": "BLOCK" if would_block else "ALLOW", "enforced": self.cfg.enforcing and would_block,
            "mode": self.cfg.mode if self.cfg.enabled else "disabled", "state": st.state.value,
            "reason": "; ".join(st.reasons),
            "event_ids": ";".join(e.event_id for e in w.events) if w else "",
            "event_names": ";".join(e.name for e in w.events) if w else "",
            "event_sources": ";".join(sorted({e.source for e in w.events})) if w else "",
            "scheduled_at": ";".join(str(e.scheduled_at) for e in w.events) if w else "",
            "blackout_start": w.start if w else None, "blackout_end": w.end if w else None,
            "calendar_age_minutes": st.calendar_age_minutes,
            "data_classes": ";".join(sorted({h.get("data_class", "unknown") for h in st.provider_health.values()})),
            "provider_health": json.dumps(st.provider_health, sort_keys=True),
            "next_event": (st.next_window.events[0].name if st.next_window and st.next_window.events else ""),
            "next_window_start": st.next_window.start if st.next_window else None,
            "explanation": explain(st, self.cfg) if self.cfg.explain else "",
        }
        self.decisions.append(rec)
        return NewsDecision(allowed, would_block, st, rec)

    def flatten_window(self, now: pd.Timestamp) -> Window | None:
        """Window that requires flattening an open position (only with pre_event_policy = "flatten")."""
        if not self.cfg.enabled or self.cfg.pre_event_policy != "flatten":
            return None
        for w in self.windows_at(now):
            if w.contains(now) and w.impact in self.cfg.flatten_impacts:
                return w
        return None

    def decisions_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.decisions)


def _fmt(t: pd.Timestamp, cfg: NewsConfig) -> str:
    s = t.tz_convert(cfg.display_timezone).strftime("%Y-%m-%d %H:%M %Z")
    if cfg.local_timezone:
        s += f" ({t.tz_convert(cfg.local_timezone).strftime('%H:%M %Z')})"
    return s


def explain(st: NewsStatus, cfg: NewsConfig) -> str:
    if st.safe:
        nxt = st.next_window
        if nxt is not None and nxt.events:
            return f"ALLOWED: next event {nxt.events[0].name}; entries blocked from {_fmt(nxt.start, cfg)}."
        return "ALLOWED: no relevant event window." if st.state is NewsState.NORMAL else "ALLOWED: news filter disabled."
    w = st.active[0] if st.active else None
    if st.state in (NewsState.BLACKOUT_ACTIVE, NewsState.DATA_CONFLICT, NewsState.EVENT_UNKNOWN) and w and w.events:
        e = w.events[0]
        if e.scheduled_at is not None:
            mins = round((e.scheduled_at - st.time).total_seconds() / 60)
            when = f"in {mins} minutes" if mins > 0 else (f"{-mins} minutes ago" if mins < 0 else "now")
            after = round((w.end - e.scheduled_at).total_seconds() / 60)
            text = (f"BLOCKED: {e.name} release {when}. New entries disabled until {_fmt(w.end, cfg)} "
                    f"({after} minutes after release).")
        else:
            text = f"BLOCKED: {e.name} has no confirmed time; the whole day is blocked."
        if st.state is NewsState.DATA_CONFLICT:
            text += " Sources disagree about this event; every reported time is blocked."
        return text
    if st.state is NewsState.EMERGENCY_BLACKOUT:
        reason = (w.reasons[0] if w and w.reasons else "emergency")
        return f"BLOCKED: emergency blackout ({reason}) until {_fmt(w.end, cfg) if w else 'cleared'}."
    return f"BLOCKED: {st.state.value.replace('_', ' ').lower()} -- {st.reasons[0] if st.reasons else ''}. " \
           "New entries stay disabled until the calendar is refreshed and validated."


def operational_readiness(cfg: NewsConfig, store: CalendarStore, now: pd.Timestamp,
                          instrument: str = "MES") -> dict[str, Any]:
    """Research mode (backtests on historical calendar files: manual or synthetic data allowed and
    labelled) vs OPERATIONAL mode (paper/live trading): every required category must come from a
    healthy VERIFIED-LIVE or MANUAL provider, no synthetic data, and NewsGuard must be enforcing."""
    issues: list[str] = []
    if not cfg.enabled or cfg.mode != "enforce":
        issues.append("news filter must be enabled in enforce mode")
    if store.is_synthetic:
        issues.append("calendar contains SYNTHETIC data")
    for name, pc in cfg.providers.items():
        if pc.enabled and pc.live and not pc.verified:
            issues.append(f"provider {name} is enabled but not verified")
    g = NewsGuard(cfg, store, instrument)
    _, health, _ = g._integrity(now)
    for cat in cfg.required_categories:
        ok = [p for p, cats in store.provider_coverage.items()
              if cat in cats and health.get(p, {}).get("status") == "ok"
              and store.provider_class.get(p) in ("verified_live", "manual")]
        if not ok:
            issues.append(f"required category {cat!r}: no healthy verified-live or manual provider")
    return {"ready": not issues, "issues": issues, "provider_health": health,
            "data_classes": store.data_classes()}

"""Operational calendar readiness: one verdict, never optimistic.

Verdicts, in order of precedence (the first that applies wins; every finding is listed):

=============================  ========================================================
EVENT DATA INVALID             the manual calendar (or the store) is malformed, uses an old
                               format, or contains synthetic data
CONFIGURATION NOT OPERATIONAL  filter disabled / not enforcing, or an unverified live provider enabled
PROVIDER UNAVAILABLE           an expected provider failed, never succeeded or is still recovering
CALENDAR STALE                 data or manual verification older than allowed
CALENDAR INCOMPLETE            a required category is not covered for the operational horizon
OPERATIONAL READY              none of the above
=============================  ========================================================

OPERATIONAL READY additionally requires that EVERY required category is covered, for the
whole operational horizon, by a healthy provider whose data is verified-live or manual;
this is re-checked as an invariant before the verdict is returned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT
from src.risk.news.calendar import CalendarStore
from src.risk.news.config import PROVIDER_REVERIFY, NewsConfig
from src.risk.news.guard import NewsGuard, NewsState
from src.risk.news.manual import SCHEMA, validate_manual

READY = "OPERATIONAL READY"
INVALID = "EVENT DATA INVALID"
CONFIG = "CONFIGURATION NOT OPERATIONAL"
PROVIDER = "PROVIDER UNAVAILABLE"
STALE = "CALENDAR STALE"
INCOMPLETE = "CALENDAR INCOMPLETE"
ORDER = [INVALID, CONFIG, PROVIDER, STALE, INCOMPLETE]
STATE_VERDICT = {NewsState.PROVIDER_UNAVAILABLE: PROVIDER, NewsState.RECOVERY_PENDING: PROVIDER,
                 NewsState.CALENDAR_STALE: STALE, NewsState.CALENDAR_INCOMPLETE: INCOMPLETE}


def load_manual(cfg: NewsConfig) -> tuple[str, Any]:
    """(path, parsed JSON or an Exception) of the enabled manual provider's file."""
    pcs = [p for p in cfg.providers.values() if p.type == "json_file" and p.enabled]
    if not pcs:
        return "", None
    path = Path(pcs[0].path)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        from src.risk.news.event import strict_json_loads

        return str(path), strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return str(path), exc


def readiness_report(cfg: NewsConfig, store: CalendarStore, now: pd.Timestamp, classify, *,
                     manual: tuple[str, Any] | None = None, instrument: str = "MES") -> dict[str, Any]:
    findings: list[tuple[str, str]] = []
    add = lambda verdict, msg: findings.append((verdict, msg))  # noqa: E731
    horizon_end = now + cfg.manual_horizon

    # 1. manual calendar file
    path, doc = manual if manual is not None else load_manual(cfg)
    manual_rep = None
    if path:
        if isinstance(doc, Exception):
            add(INVALID, f"manual calendar {path} unreadable: {type(doc).__name__}: {doc}")
        elif not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            add(INVALID, f"manual calendar {path} must use schema {SCHEMA!r}")
        else:
            manual_rep = validate_manual(doc, cfg, now, classify)
            for e in manual_rep.errors:
                add(INVALID, f"manual calendar: {e}")
    if store.is_synthetic:
        add(INVALID, "calendar store contains SYNTHETIC data")

    # 2. configuration
    if not cfg.enabled or cfg.mode != "enforce":
        add(CONFIG, "news filter must be enabled in enforce mode")
    for name, pc in cfg.providers.items():
        if pc.enabled and pc.live and not pc.verified:
            add(CONFIG, f"provider {name} is enabled but not verified")

    # 3. provider health / staleness / coverage (the SAME rules NewsGuard enforces on every entry,
    #    in operational mode: only verified-live or manual data counts)
    guard = NewsGuard(cfg, store, instrument, operational=True)
    problems, health, _ = guard._integrity(now)
    for state, msg in problems:
        add(STATE_VERDICT.get(state, INCOMPLETE), msg)
    for p, h in health.items():
        if h.get("status") == "degraded_cached":
            add(PROVIDER, f"provider {p}: latest refresh failed; only cached data available")
    for p, when in cfg.provider_evidence.items():
        if now - pd.Timestamp(when) > PROVIDER_REVERIFY:
            add(CONFIG, f"provider {p}: live verification from {when} is older than "
                        f"{PROVIDER_REVERIFY.days} days; re-run scripts/verify_news_providers.py")

    # 4. operational horizon per required category (same rule, strict, longer horizon)
    table: dict[str, Any] = {}
    for cat in cfg.required_categories:
        row = {"covered": False, "by": None, "reasons": []}
        for p, cats in store.provider_coverage.items():
            if cat not in cats:
                continue
            prob = guard.coverage_problem(p, cat, now, cfg.manual_horizon, health, operational=True, strict=True)
            if prob is None:
                row.update(covered=True, by=p)
                break
            row["reasons"].append(prob[1])
        if not row["covered"] and not any(f"category {cat!r} not covered" in m for _, m in findings):
            stale = any("last verified" in r for r in row["reasons"])
            add(STALE if stale else INCOMPLETE, f"required category {cat!r} not covered through "
                f"{horizon_end.date()}: {'; '.join(row['reasons']) or 'no provider covers it'}")
        table[cat] = row

    verdict = next((v for v in ORDER if any(f[0] == v for f in findings)), READY)
    if verdict == READY and not all(r["covered"] for r in table.values()):  # invariant: never optimistic
        verdict = INCOMPLETE
    upcoming = [w.to_dict() for w in guard.windows_at(now) if now <= w.start <= horizon_end]
    return {"verdict": verdict, "ready": verdict == READY, "time": now.isoformat(),
            "operational_horizon_end": horizon_end.isoformat(),
            "findings": [{"verdict": v, "message": m} for v, m in findings], "categories": table,
            "provider_health": health, "manual_calendar": manual_rep.to_dict() if manual_rep else None,
            "upcoming_windows": upcoming[:50], "data_classes": store.data_classes()}

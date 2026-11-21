"""Live calendar refresh: query every enabled provider, validate, store, log health.

Used by ``run_news_calendar.py --refresh`` (schedule it daily or more often). A provider
that fails, returns malformed data or rejected records is logged as a FAILED refresh, so
NewsGuard blocks new entries until a later refresh succeeds (fail closed).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.risk.news.calendar import CalendarStore, RefreshRecord
from src.risk.news.config import NewsConfig
from src.risk.news.providers import Provider, ProviderError, RssProvider
from src.risk.news.unexpected import items_to_events


def expected_providers(cfg: NewsConfig) -> set[str]:
    return {p.name for p in cfg.providers.values() if p.enabled and p.expected}


def refresh(store: CalendarStore, providers: list[Provider], cfg: NewsConfig, now: pd.Timestamp,
            *, force: bool = False, llm_assessments: dict | None = None) -> list[dict[str, Any]]:
    store.expected_providers |= expected_providers(cfg)
    store.priorities.update({p.name: p.pc.priority for p in providers})
    store.provider_coverage.update({p.name: list(p.pc.covers) for p in providers})
    store.provider_class.update({p.name: p.provenance.value for p in providers})
    report = []
    for p in providers:
        if not force and not p.due(now):
            report.append({"provider": p.name, "status": "skipped (rate limit)"})
            continue
        try:
            res = p.fetch(now)
        except ProviderError as exc:
            store.log_refresh(RefreshRecord(now, p.name, False, error=str(exc)[:300]))
            report.append({"provider": p.name, "status": "failed", "error": str(exc)})
            continue
        if res.invalid:
            # malformed records make the whole refresh untrustworthy: keep nothing, fail closed
            store.log_refresh(RefreshRecord(now, p.name, False, error=f"{len(res.invalid)} invalid records: "
                                            f"{res.invalid[0]}"[:300]))
            report.append({"provider": p.name, "status": "failed", "error": res.invalid[:5]})
            continue
        recs = res.records
        if isinstance(p, RssProvider) and cfg.unexpected_enabled and p.name == cfg.unexpected_provider:
            recs = items_to_events(res.items, cfg, p.name, now, llm_assessments)
            res.coverage_start, res.coverage_end = now, now + cfg.lookahead
        added = sum(store.add(r) for r in recs)
        store.log_refresh(RefreshRecord(now, p.name, True, res.coverage_start, res.coverage_end, n_events=added,
                                        category_coverage=res.category_coverage))
        report.append({"provider": p.name, "status": "ok", "new_versions": added})
    return report

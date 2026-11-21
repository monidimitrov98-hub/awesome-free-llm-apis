"""Economic calendar maintenance and NewsGuard status (no trading).

Examples:
    python run_news_calendar.py --refresh                  (query enabled providers; schedule daily or hourly)
    python run_news_calendar.py --status --instrument MES  (current state, active/next windows; --json for a dashboard file)
    python run_news_calendar.py --validate data/news/history.json --start 2025-01-01 --end 2025-03-31

The store is a point-in-time JSON file ([calendar] store_path). A failed or malformed
provider refresh is recorded and keeps new entries blocked until a later refresh succeeds.
Credentials come only from the environment variables named in the config (api_key_env).
Exit codes: 0 ok / calendar safe, 1 configuration or data error, 8 NewsGuard would block entries now.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError
from src.risk.news.calendar import CalendarStore
from src.risk.news.classifier import make_classifier
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH, load_news_config
from src.risk.news.guard import NewsGuard, explain, operational_readiness
from src.risk.news.providers import build_providers
from src.risk.news.service import expected_providers, refresh


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Economic calendar refresh / status / validation.")
    p.add_argument("--config", type=Path, default=DEFAULT_NEWS_CONFIG_PATH)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--refresh", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--validate", type=Path)
    p.add_argument("--instrument", default="MES")
    p.add_argument("--store", type=Path, default=None)
    p.add_argument("--json", type=Path, default=None, help="write the status as dashboard JSON")
    p.add_argument("--start")
    p.add_argument("--end")
    return p.parse_args(argv)


def _store(cfg, path: Path, classify) -> CalendarStore:
    if path.is_file():
        return CalendarStore.load(path, classify)
    return CalendarStore(expected_providers=expected_providers(cfg))


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_news_config(args.config)
        classify = make_classifier(cfg)
        store_path = args.store or (PROJECT_ROOT / cfg.store_path)
        now = pd.Timestamp.now(tz="UTC").floor("s")
        if args.validate:
            st = CalendarStore.load(args.validate, classify)
            start = pd.Timestamp(args.start, tz="UTC") if args.start else min((r.time for r in st.refreshes), default=now)
            end = pd.Timestamp(args.end, tz="UTC") if args.end else now
            rep = st.coverage_report(start, end)
            rep["versions"] = len(st.records)
            rep["unclassified"] = sorted({r.name for r in st.records if r.category == "unclassified"})
            print(json.dumps(rep, indent=2))
            return 0
        if args.refresh:
            st = _store(cfg, store_path, classify)
            report = refresh(st, build_providers(cfg, classify), cfg, now, force=True)
            sha = st.save(store_path)
            for r in report:
                print(f"  {r['provider']}: {r['status']}" + (f" ({r.get('error')})" if r.get("error") else ""))
            print(f"store {store_path} sha256 {sha[:12]}")
            if not report:
                print("no enabled providers: NewsGuard will block entries until a provider is configured")
            return 0
        st = _store(cfg, store_path, classify)
        guard = NewsGuard(cfg, st, args.instrument)
        s = guard.status(now)
        doc = {"time": now.isoformat(), "instrument": args.instrument, "mode": cfg.mode, "state": s.state.value,
               "entries_allowed": s.safe or not cfg.enforcing, "reasons": s.reasons,
               "explanation": explain(s, cfg), "calendar_age_minutes": s.calendar_age_minutes,
               "provider_health": s.provider_health, "active_windows": [w.to_dict() for w in s.active],
               "upcoming_windows": [w.to_dict() for w in guard.windows_at(now) if w.start > now][:10],
               "operational_readiness": operational_readiness(cfg, st, now, args.instrument)}
        print(json.dumps(doc, indent=2))
        if args.json:
            args.json.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return 0 if s.safe else 8
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

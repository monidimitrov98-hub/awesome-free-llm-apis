"""Verify the configured LIVE calendar providers against their real endpoints.

    python scripts/verify_news_providers.py --out config/risk/provider_verification.json

The output is the verification EVIDENCE the config loader checks: an enabled live provider must
match a successful record here (same type, same URLs, verified_on = the date of verified_at).

For every live provider (enabled or not): fetch, parse with the production parser, and report
HTTP outcome, number of events, date range, timezone handling (UTC vs New York times),
categories found, unclassified names, schedule-change markers and coverage of the required
categories. BEA and Census both list the joint "U.S. International Trade in Goods and
Services" release: their times are cross-checked (Census gives no timezone; BEA gives UTC).

Set FTS_HTTP_CONTACT (e.g. your e-mail) before running: BLS blocks automated clients whose
User-Agent has no contact information. Nothing is written to the calendar store.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.risk.news.classifier import make_classifier  # noqa: E402
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH, load_news_config  # noqa: E402
from src.risk.news.providers import ProviderError, RssProvider, build_providers, user_agent  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_NEWS_CONFIG_PATH)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    cfg = load_news_config(args.config)
    classify = make_classifier(cfg)
    now = pd.Timestamp.now(tz="UTC").floor("s")
    report = {"verified_at": now.isoformat(), "user_agent": user_agent(), "providers": {}}
    trade: dict[str, dict[str, str]] = {}
    for p in build_providers(cfg, classify, include_disabled=True):
        if not p.pc.live:
            continue
        entry = {"type": p.pc.type, "urls": list(p.pc.all_urls()), "enabled": p.pc.enabled,
                 "configured_verified": p.pc.verified, "documentation": p.pc.documentation}
        try:
            res = p.fetch(now)
        except ProviderError as exc:
            entry.update(ok=False, error=str(exc))
            report["providers"][p.name] = entry
            print(f"{p.name}: FAILED ({exc})")
            continue
        if isinstance(p, RssProvider):
            entry.update(ok=True, items=len(res.items),
                         newest=[i["published"] + " | " + i["title"][:80] for i in res.items[:5]])
        else:
            recs = res.records
            timed = [r for r in recs if r.scheduled_at is not None]
            future = [r for r in recs if (r.scheduled_at or pd.Timestamp(r.event_date or "1990-01-01", tz="UTC")) >= now]
            cats = collections.Counter(r.category for r in recs)
            entry.update(
                ok=not res.invalid, invalid=res.invalid[:5], events=len(recs), timed=len(timed),
                date_only=len(recs) - len(timed), future_events=len(future),
                first=str(min((r.event_date for r in recs if r.event_date), default=None)),
                last=str(max((r.event_date for r in recs if r.event_date), default=None)),
                coverage_end=str(res.coverage_end),
                ny_times=dict(collections.Counter(r.scheduled_at.tz_convert("America/New_York").strftime("%H:%M")
                                                  for r in timed).most_common(6)),
                statuses=dict(collections.Counter(r.status.value for r in recs)),
                categories=dict(cats.most_common()),
                unclassified=sorted({r.name for r in recs if r.category == "unclassified"})[:15],
                covers_configured=list(p.pc.covers),
                covers_missing=[c for c in p.pc.covers if cats.get(c, 0) == 0 and c not in ("core_pce", "fomc_statement")])
            for r in timed:
                if r.category == "trade_balance" and "international trade in goods" in r.name.lower():
                    trade.setdefault(p.name, {})[str(r.event_date)] = r.scheduled_at.isoformat()
        report["providers"][p.name] = entry
        print(f"{p.name}: {'OK' if entry['ok'] else 'INVALID'} {entry.get('events', entry.get('items'))} items")
    if "bea" in trade and "census" in trade:
        common = sorted(set(trade["bea"]) & set(trade["census"]))
        report["cross_check_trade_release"] = {
            "common_dates": len(common),
            "identical_utc_times": sum(trade["bea"][d] == trade["census"][d] for d in common),
            "differences": {d: [trade["bea"][d], trade["census"][d]] for d in common if trade["bea"][d] != trade["census"][d]}}
    covered = {c for e in report["providers"].values() if e.get("ok") and e.get("enabled")
               for c in e.get("covers_configured", [])}
    report["required_categories_without_verified_live_source"] = [c for c in cfg.required_categories if c not in covered]
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

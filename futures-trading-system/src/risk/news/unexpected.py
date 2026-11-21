"""Unexpected official announcements -> emergency blackouts.

Rule-based detection on items of an official feed (e.g. Federal Reserve monetary-policy
press releases): an item newer than ``max_item_age`` whose title/description contains a
configured keyword becomes an ``unexpected`` event, which blocks new entries from the time
it became known for ``blackout_minutes``.

An optional LLM may classify items, but only through :func:`validate_llm_assessment`:
strict schema, it must cite the URL of a RETRIEVED item (no invented events), and it can
only RAISE severity -- it can never remove a rule-based blackout or allow an entry.
Uncertain or invalid LLM output is treated conservatively (rule result kept, and an
uncertain "maybe" becomes a blackout).
"""

from __future__ import annotations

import hashlib
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import pandas as pd

from src.risk.news.config import NewsConfig
from src.risk.news.event import EventRecord, EventStatus, Impact, Reliability

SEVERITIES = ("none", "low", "high", "uncertain")


def _published(text: str) -> pd.Timestamp | None:
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None or dt.tzinfo is None:
        return None
    return pd.Timestamp(dt).tz_convert("UTC")


def rule_severity(item: Mapping[str, Any], cfg: NewsConfig) -> str:
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    return "high" if any(k in text for k in cfg.unexpected_keywords) else "none"


def validate_llm_assessment(obj: Any, retrieved_links: set[str]) -> dict[str, Any] | None:
    """Return a validated assessment or None (invalid -> ignored, rule result stands)."""
    if not isinstance(obj, dict) or set(obj) != {"source_url", "severity", "summary"}:
        return None
    if obj["source_url"] not in retrieved_links:  # must cite a retrieved official item
        return None
    if obj["severity"] not in SEVERITIES or not isinstance(obj["summary"], str):
        return None
    return {"source_url": obj["source_url"], "severity": obj["severity"], "summary": obj["summary"][:500]}


def combine(rule: str, llm: dict[str, Any] | None) -> str:
    """The LLM can only raise severity; 'uncertain' counts as high."""
    if llm is None:
        return rule
    llm_sev = "high" if llm["severity"] == "uncertain" else llm["severity"]
    return "high" if "high" in (rule, llm_sev) else rule


def items_to_events(items: list[dict[str, Any]], cfg: NewsConfig, source: str, now: pd.Timestamp,
                    llm_assessments: Mapping[str, Any] | None = None) -> list[EventRecord]:
    links = {i.get("link", "") for i in items if i.get("link")}
    out = []
    for it in items:
        pub = _published(it.get("published", ""))
        if pub is None or pub > now or now - pub > cfg.unexpected_max_age:
            continue
        llm = validate_llm_assessment((llm_assessments or {}).get(it.get("link", "")), links)
        if combine(rule_severity(it, cfg), llm) != "high":
            continue
        uid = hashlib.sha256((it.get("link", "") + it.get("title", "")).encode()).hexdigest()[:16]
        out.append(EventRecord(event_id=f"unexpected:{uid}", name=it.get("title", "")[:300] or "official announcement",
                               category="unexpected", region="US", scheduled_at=pub,
                               event_date=pub.tz_convert("America/New_York").date(), timezone="UTC",
                               status=EventStatus.RELEASED, impact=Impact.HIGH, source=source,
                               source_event_id=it.get("link", "")[:200], reliability=Reliability.OFFICIAL,
                               known_at=now))
    return out

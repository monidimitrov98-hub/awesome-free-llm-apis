"""Strict loader for ``config/risk/news_filter.toml``.

Unknown sections/keys are rejected, so this file can never carry account protection,
drawdown, risk-budget or sizing settings. Settings that would let entries through when
data is missing are rejected.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError
from src.risk.news.event import EventDataError, Impact, Reliability, check_timezone, parse_time

DEFAULT_NEWS_CONFIG_PATH = PROJECT_ROOT / "config" / "risk" / "news_filter.toml"
PROVIDER_EVIDENCE_PATH = PROJECT_ROOT / "config" / "risk" / "provider_verification.json"

# ---------------------------------------------------------------- non-negotiable safety floors
# (audit 2026-11-21) The configuration may TIGHTEN the filter but can never remove a critical
# US release, downgrade it below its minimum impact, shrink its window to nothing, drop the US
# region, accept unofficial sources or relax freshness beyond these bounds.
MANDATORY_CATEGORIES = {
    "cpi": Impact.HIGH, "core_cpi": Impact.HIGH, "nfp": Impact.HIGH, "unemployment_rate": Impact.HIGH,
    "pce": Impact.HIGH, "fomc_decision": Impact.HIGH, "fomc_statement": Impact.HIGH,
    "fed_press_conference": Impact.HIGH, "fed_chair_speech": Impact.HIGH,
    "ppi": Impact.MEDIUM, "jobless_claims": Impact.MEDIUM, "ism_manufacturing": Impact.MEDIUM,
    "ism_services": Impact.MEDIUM, "treasury": Impact.MEDIUM, "gdp": Impact.MEDIUM, "retail_sales": Impact.MEDIUM,
}
MIN_WINDOW = pd.Timedelta(minutes=5)          # each side, mandatory categories (overrides included)
MIN_UNKNOWN_WINDOW = pd.Timedelta(minutes=15)  # each side, unclassified relevant events
MAX_CALENDAR_AGE = pd.Timedelta(days=3)
MIN_LOOKAHEAD = pd.Timedelta(minutes=60)
MAX_MANUAL_AGE = pd.Timedelta(days=14)
MIN_MANUAL_HORIZON = pd.Timedelta(days=7)
PROVIDER_REVERIFY = pd.Timedelta(days=90)
OFFICIAL_DOMAINS = ("bls.gov", "dol.gov", "doleta.gov", "federalreserve.gov", "treasury.gov", "ismworld.org",
                    "bea.gov", "census.gov")
_RANK = {Impact.LOW: 0, Impact.UNKNOWN: 1, Impact.MEDIUM: 2, Impact.HIGH: 3}
MODES = ("observe", "enforce")
PROVIDER_TYPES = ("json_file", "ics", "rss", "census_html", "fomc_html")
LIVE_TYPES = ("ics", "rss", "census_html", "fomc_html")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,60}$")


@dataclass(frozen=True)
class EventRule:
    category: str
    patterns: tuple[str, ...]
    impact: Impact
    before: pd.Timedelta
    after: pd.Timedelta
    compiled: tuple[re.Pattern, ...] = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str
    enabled: bool
    reliability: Reliability
    priority: int
    expected: bool
    path: str = ""
    url: str = ""
    api_key_env: str = ""
    api_key_param: str = ""
    urls: tuple[str, ...] = ()
    verified: bool = False
    verified_on: str = ""
    documentation: str = ""
    covers: tuple[str, ...] = ()

    @property
    def live(self) -> bool:
        return self.type in LIVE_TYPES

    def all_urls(self) -> tuple[str, ...]:
        return ((self.url,) if self.url else ()) + self.urls

@dataclass(frozen=True)
class EmergencyWindow:
    start: pd.Timestamp
    end: pd.Timestamp
    reason: str


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool
    mode: str
    display_timezone: str
    local_timezone: str
    max_age: pd.Timedelta
    lookahead: pd.Timedelta
    refresh_interval: pd.Timedelta
    min_request_interval: pd.Timedelta
    request_timeout: float
    max_response_bytes: int
    conflict_policy: str
    unknown_time_policy: str
    backtest_calendar_path: str
    store_path: str
    provider_failure_policy: str
    recovery_successes: int
    pre_event_policy: str
    flatten_impacts: tuple[Impact, ...]
    block_impacts: tuple[Impact, ...]
    unknown_before: pd.Timedelta
    unknown_after: pd.Timedelta
    events: dict[str, EventRule]
    instruments: dict[str, dict[str, Any]]
    emergency: tuple[EmergencyWindow, ...]
    providers: dict[str, ProviderConfig]
    unexpected_enabled: bool
    unexpected_provider: str
    unexpected_keywords: tuple[str, ...]
    unexpected_blackout: pd.Timedelta
    unexpected_max_age: pd.Timedelta
    explain: bool
    required_categories: tuple[str, ...] = ()
    manual_max_age: pd.Timedelta = pd.Timedelta(days=7)
    manual_horizon: pd.Timedelta = pd.Timedelta(days=14)
    official_domains: tuple[str, ...] = ()
    provider_evidence: dict[str, str] = field(default_factory=dict)  # provider -> verified_at of its evidence
    source_path: str = ""

    @property
    def enforcing(self) -> bool:
        return self.enabled and self.mode == "enforce"

    def regions_for(self, root: str) -> tuple[str, ...]:
        inst = self.instruments.get(root.split(":")[0])
        if inst is None:
            raise ConfigError(f"news filter has no [instruments.{root.split(':')[0]}] section; "
                              "every traded instrument needs explicit news rules")
        return tuple(inst["regions"])

    def window(self, root: str, category: str) -> tuple[pd.Timedelta, pd.Timedelta]:
        over = self.instruments.get(root.split(":")[0], {}).get("overrides", {}).get(category)
        if over is not None:
            return over
        rule = self.events.get(category)
        if rule is None:
            return self.unknown_before, self.unknown_after
        return rule.before, rule.after


SECTIONS = {
    "news": {"enabled", "mode", "display_timezone", "local_timezone"},
    "calendar": {"max_age_minutes", "lookahead_minutes", "refresh_interval_minutes", "min_request_interval_seconds",
                 "request_timeout_seconds", "max_response_bytes", "conflict_policy", "unknown_time_policy",
                 "backtest_calendar_path", "store_path"},
    "fail_safe": {"on_integrity_failure", "provider_failure_policy", "recovery_successes"},
    "positions": {"pre_event_policy", "flatten_impacts"},
    "impact": {"block"},
    "unknown_impact": {"before_minutes", "after_minutes"},
    "events": None, "instruments": None, "emergency": {"windows"}, "providers": None,
    "unexpected": {"enabled", "provider", "keywords", "blackout_minutes", "max_item_age_minutes"},
    "diagnostics": {"explain"},
    "coverage": {"required_categories"},
    "manual": {"max_verification_age_days", "operational_horizon_days", "official_domains"},
}


def _num(sec: dict, key: str, where: str, *, minimum: float = 0, integer: bool = False, strict: bool = False):
    v = sec.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ConfigError(f"[{where}] {key} must be a finite number, got {v!r}")
    if integer and not isinstance(v, int):
        raise ConfigError(f"[{where}] {key} must be an integer")
    if v < minimum or (strict and v == minimum):
        raise ConfigError(f"[{where}] {key} must be {'>' if strict else '>='} {minimum}, got {v}")
    return v


def _bool(sec: dict, key: str, where: str) -> bool:
    v = sec.get(key)
    if not isinstance(v, bool):
        raise ConfigError(f"[{where}] {key} must be true or false")
    return v


def _str(sec: dict, key: str, where: str, choices: tuple[str, ...] | None = None, default: str | None = None) -> str:
    v = sec.get(key, default)
    if not isinstance(v, str):
        raise ConfigError(f"[{where}] {key} must be a string")
    if choices is not None and v not in choices:
        raise ConfigError(f"[{where}] {key} must be one of {list(choices)}, got {v!r}")
    return v


def _impacts(values: Any, where: str) -> tuple[Impact, ...]:
    if not isinstance(values, list):
        raise ConfigError(f"{where} must be a list")
    try:
        return tuple(Impact(v) for v in values)
    except ValueError as exc:
        raise ConfigError(f"{where}: unknown impact/severity value ({exc})") from exc


def _minutes(v: Any, where: str) -> pd.Timedelta:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ConfigError(f"{where} must be a finite number of minutes >= 0, got {v!r}")
    return pd.Timedelta(minutes=v)


def load_news_config(path: str | Path | None = None) -> NewsConfig:
    p = Path(path) if path else DEFAULT_NEWS_CONFIG_PATH
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_file():
        raise ConfigError(f"News filter config not found: {p}")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {p}: {exc}") from exc
    for name, sec in raw.items():
        if name not in SECTIONS:
            raise ConfigError(f"Unknown section [{name}] in {p}: the news filter cannot configure "
                              "account protection, drawdown, risk budget or sizing")
        if not isinstance(sec, dict):
            raise ConfigError(f"[{name}] must be a table")
        allowed = SECTIONS[name]
        if allowed is not None and set(sec) - allowed:
            raise ConfigError(f"Unknown key(s) {sorted(set(sec) - allowed)} in [{name}] of {p}")
    for name in ("news", "calendar", "fail_safe", "positions", "impact", "unknown_impact", "events", "instruments"):
        if name not in raw:
            raise ConfigError(f"Missing section [{name}] in {p}")
    news, cal, fs, pos = raw["news"], raw["calendar"], raw["fail_safe"], raw["positions"]
    try:
        disp = check_timezone(_str(news, "display_timezone", "news"))
        local = _str(news, "local_timezone", "news", default="")
        if local:
            check_timezone(local)
    except EventDataError as exc:
        raise ConfigError(f"[news] {exc}") from exc
    if _str(fs, "on_integrity_failure", "fail_safe") != "block_entries":
        raise ConfigError("[fail_safe] on_integrity_failure must be 'block_entries': missing or stale calendar "
                          "data never allows entries")
    recovery = _num(fs, "recovery_successes", "fail_safe", minimum=1, integer=True)

    events: dict[str, EventRule] = {}
    for cat, sec in raw["events"].items():
        if not _NAME.match(cat) or not isinstance(sec, dict):
            raise ConfigError(f"invalid event category [events.{cat}]")
        if set(sec) - {"patterns", "impact", "before_minutes", "after_minutes"}:
            raise ConfigError(f"Unknown key(s) in [events.{cat}]: {sorted(set(sec) - {'patterns', 'impact', 'before_minutes', 'after_minutes'})}")
        pats = sec.get("patterns", [])
        if not isinstance(pats, list) or not all(isinstance(x, str) and x for x in pats):
            raise ConfigError(f"[events.{cat}] patterns must be a list of non-empty strings")
        try:
            compiled = tuple(re.compile(x, re.I) for x in pats)
            impact = Impact(sec.get("impact"))
        except (re.error, ValueError) as exc:
            raise ConfigError(f"[events.{cat}] {exc}") from exc
        events[cat] = EventRule(cat, tuple(pats), impact, _minutes(sec.get("before_minutes"), f"[events.{cat}] before_minutes"),
                                _minutes(sec.get("after_minutes"), f"[events.{cat}] after_minutes"), compiled)
    if "unexpected" not in events:
        raise ConfigError("[events.unexpected] is required")

    instruments: dict[str, dict[str, Any]] = {}
    for root, sec in raw["instruments"].items():
        if not isinstance(sec, dict) or set(sec) - {"regions", "overrides"}:
            raise ConfigError(f"[instruments.{root}] accepts only regions and overrides")
        regions = sec.get("regions")
        if not isinstance(regions, list) or not regions or not all(isinstance(r, str) and r for r in regions):
            raise ConfigError(f"[instruments.{root}] regions must be a non-empty list")
        overrides = {}
        for cat, pair in (sec.get("overrides") or {}).items():
            if cat not in events or not isinstance(pair, list) or len(pair) != 2:
                raise ConfigError(f"[instruments.{root}.overrides] {cat} must be a known category = [before, after]")
            overrides[cat] = (_minutes(pair[0], f"{root} override {cat}"), _minutes(pair[1], f"{root} override {cat}"))
        instruments[root] = {"regions": [r.upper() for r in regions], "overrides": overrides}
    if not instruments:
        raise ConfigError("[instruments] must configure at least one instrument")

    emergency = []
    for i, w in enumerate((raw.get("emergency") or {}).get("windows", [])):
        if not isinstance(w, dict) or set(w) - {"start", "end", "reason"}:
            raise ConfigError(f"[[emergency.windows]] #{i + 1}: needs start, end, reason")
        try:
            s, e = parse_time(w.get("start"), "start"), parse_time(w.get("end"), "end")
        except EventDataError as exc:
            raise ConfigError(f"[[emergency.windows]] #{i + 1}: {exc}") from exc
        if e <= s:
            raise ConfigError(f"[[emergency.windows]] #{i + 1}: end must be after start")
        emergency.append(EmergencyWindow(s, e, str(w.get("reason") or "emergency")[:200]))

    providers: dict[str, ProviderConfig] = {}
    for name, sec in (raw.get("providers") or {}).items():
        keys = {"type", "enabled", "path", "url", "urls", "reliability", "priority", "expected", "api_key_env",
                "api_key_param", "verified", "verified_on", "documentation", "covers"}
        if not _NAME.match(name) or not isinstance(sec, dict) or set(sec) - keys:
            raise ConfigError(f"invalid provider [providers.{name}] (allowed keys {sorted(keys)})")
        if any(k.lower() in ("api_key", "key", "token", "secret", "password") for k in sec):
            raise ConfigError(f"[providers.{name}]: credentials must come from an environment variable (api_key_env)")
        ptype = _str(sec, "type", f"providers.{name}", PROVIDER_TYPES)
        try:
            rel = Reliability(sec.get("reliability"))
        except ValueError as exc:
            raise ConfigError(f"[providers.{name}] reliability: {exc}") from exc
        url, path_ = sec.get("url", ""), sec.get("path", "")
        urls = sec.get("urls", [])
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise ConfigError(f"[providers.{name}] urls must be a list of strings")
        if ptype in LIVE_TYPES:
            if not (url or urls) or not all(isinstance(u, str) and u.startswith("https://") for u in ([url] if url else []) + urls):
                raise ConfigError(f"[providers.{name}] every url must use https://")
        covers = sec.get("covers", [])
        if not isinstance(covers, list) or not all(isinstance(c, str) for c in covers):
            raise ConfigError(f"[providers.{name}] covers must be a list of event categories")
        verified = sec.get("verified", False)
        if not isinstance(verified, bool):
            raise ConfigError(f"[providers.{name}] verified must be true or false")
        enabled = _bool(sec, "enabled", f"providers.{name}")
        if enabled and ptype in LIVE_TYPES and not (verified and sec.get("verified_on") and sec.get("documentation")):
            raise ConfigError(f"[providers.{name}] is a live provider: it can only be enabled after verification "
                              "against its real endpoint (verified = true, verified_on, documentation)")
        if ptype == "json_file" and not (isinstance(path_, str) and path_):
            raise ConfigError(f"[providers.{name}] json_file needs a path")
        env = sec.get("api_key_env", "")
        if env and not _ENV.match(env):
            raise ConfigError(f"[providers.{name}] api_key_env must be an environment variable NAME")
        providers[name] = ProviderConfig(name, ptype, enabled, rel,
                                         int(_num(sec, "priority", f"providers.{name}", minimum=1, integer=True)),
                                         _bool(sec, "expected", f"providers.{name}"), path_, url, env,
                                         str(sec.get("api_key_param", "")), tuple(urls), verified,
                                         str(sec.get("verified_on", "")), str(sec.get("documentation", "")),
                                         tuple(covers))
    for name, pc in providers.items():
        unknown = [c for c in pc.covers if c not in events]
        if unknown:
            raise ConfigError(f"[providers.{name}] covers unknown categories {unknown}")
    required = (raw.get("coverage") or {}).get("required_categories", [])
    if not isinstance(required, list) or any(c not in events for c in required):
        raise ConfigError("[coverage] required_categories must list known event categories")
    man = raw.get("manual") or {}
    domains = man.get("official_domains", [])
    if not isinstance(domains, list) or not domains or not all(
            isinstance(d, str) and re.fullmatch(r"[a-z0-9.-]+\.(gov|org|com)", d) for d in domains):
        raise ConfigError("[manual] official_domains must be a non-empty list of domain names")
    man_age = _num(man, "max_verification_age_days", "manual", minimum=0, strict=True) if man else 7
    man_horizon = _num(man, "operational_horizon_days", "manual", minimum=0, strict=True) if man else 14
    unexpected = raw.get("unexpected") or {"enabled": False}
    u_enabled = _bool(unexpected, "enabled", "unexpected")
    u_provider = unexpected.get("provider", "")
    if u_enabled and u_provider not in providers:
        raise ConfigError("[unexpected] provider must name a configured provider")

    cfg = NewsConfig(
        enabled=_bool(news, "enabled", "news"), mode=_str(news, "mode", "news", MODES), display_timezone=disp,
        local_timezone=local,
        max_age=pd.Timedelta(minutes=_num(cal, "max_age_minutes", "calendar", strict=True)),
        lookahead=pd.Timedelta(minutes=_num(cal, "lookahead_minutes", "calendar", strict=True)),
        refresh_interval=pd.Timedelta(minutes=_num(cal, "refresh_interval_minutes", "calendar", strict=True)),
        min_request_interval=pd.Timedelta(seconds=_num(cal, "min_request_interval_seconds", "calendar", minimum=1)),
        request_timeout=float(_num(cal, "request_timeout_seconds", "calendar", strict=True)),
        max_response_bytes=int(_num(cal, "max_response_bytes", "calendar", minimum=1024, integer=True)),
        conflict_policy=_str(cal, "conflict_policy", "calendar", ("union", "prefer_priority")),
        unknown_time_policy=_str(cal, "unknown_time_policy", "calendar", ("block_day",)),
        backtest_calendar_path=_str(cal, "backtest_calendar_path", "calendar", default=""),
        store_path=_str(cal, "store_path", "calendar", default="data/news/calendar_store.json"),
        provider_failure_policy=_str(fs, "provider_failure_policy", "fail_safe", ("block_entries", "use_fresh_cache")),
        recovery_successes=int(recovery),
        pre_event_policy=_str(pos, "pre_event_policy", "positions", ("keep", "flatten")),
        flatten_impacts=_impacts(pos.get("flatten_impacts", ["high"]), "[positions] flatten_impacts"),
        block_impacts=_validate_block(_impacts(raw["impact"].get("block"), "[impact] block")),
        unknown_before=_minutes(raw["unknown_impact"].get("before_minutes"), "[unknown_impact] before_minutes"),
        unknown_after=_minutes(raw["unknown_impact"].get("after_minutes"), "[unknown_impact] after_minutes"),
        events=events, instruments=instruments, emergency=tuple(emergency), providers=providers,
        unexpected_enabled=u_enabled, unexpected_provider=u_provider,
        unexpected_keywords=tuple(str(k).lower() for k in unexpected.get("keywords", [])),
        unexpected_blackout=_minutes(unexpected.get("blackout_minutes", 30), "[unexpected] blackout_minutes"),
        unexpected_max_age=_minutes(unexpected.get("max_item_age_minutes", 120), "[unexpected] max_item_age_minutes"),
        explain=bool((raw.get("diagnostics") or {}).get("explain", True)), required_categories=tuple(required),
        manual_max_age=pd.Timedelta(days=man_age), manual_horizon=pd.Timedelta(days=man_horizon),
        official_domains=tuple(d.lower() for d in domains),
        source_path=str(p))
    _safety_floors(cfg)
    return replace(cfg, provider_evidence=_check_evidence(cfg))


def _validate_block(impacts: tuple[Impact, ...]) -> tuple[Impact, ...]:
    if Impact.HIGH not in impacts or Impact.UNKNOWN not in impacts:
        raise ConfigError("[impact] block must include 'high' and 'unknown': an unclassified or high-impact "
                          "event can never be treated as safe")
    return impacts


def _safety_floors(c: NewsConfig) -> None:
    missing = [k for k in MANDATORY_CATEGORIES if k not in c.required_categories]
    if missing:
        raise ConfigError(f"[coverage] required_categories cannot drop mandatory categories {missing}")
    for cat, floor in MANDATORY_CATEGORIES.items():
        rule = c.events.get(cat)
        if rule is None:
            raise ConfigError(f"[events.{cat}] is mandatory")
        if _RANK[rule.impact] < _RANK[floor]:
            raise ConfigError(f"[events.{cat}] impact cannot be lower than {floor.value!r}")
        if rule.impact not in c.block_impacts:
            raise ConfigError(f"[impact] block must include {rule.impact.value!r} (used by mandatory {cat!r})")
        if rule.before < MIN_WINDOW or rule.after < MIN_WINDOW:
            raise ConfigError(f"[events.{cat}] before/after must each be >= {MIN_WINDOW.total_seconds() / 60:.0f} min")
    for root, inst in c.instruments.items():
        if "US" not in inst["regions"]:
            raise ConfigError(f"[instruments.{root}] regions must include 'US' (US index futures)")
        for cat, (b, a) in inst["overrides"].items():
            if cat in MANDATORY_CATEGORIES and (b < MIN_WINDOW or a < MIN_WINDOW):
                raise ConfigError(f"[instruments.{root}.overrides] {cat} window below the minimum")
    if c.unknown_before < MIN_UNKNOWN_WINDOW or c.unknown_after < MIN_UNKNOWN_WINDOW:
        raise ConfigError("[unknown_impact] before/after must each be >= 15 min")
    if c.max_age > MAX_CALENDAR_AGE:
        raise ConfigError("[calendar] max_age_minutes cannot exceed 4320 (3 days)")
    if c.lookahead < MIN_LOOKAHEAD:
        raise ConfigError("[calendar] lookahead_minutes must be >= 60")
    if c.manual_max_age > MAX_MANUAL_AGE:
        raise ConfigError("[manual] max_verification_age_days cannot exceed 14")
    if c.manual_horizon < MIN_MANUAL_HORIZON:
        raise ConfigError("[manual] operational_horizon_days must be >= 7")
    bad = [d for d in c.official_domains if d not in OFFICIAL_DOMAINS]
    if bad:
        raise ConfigError(f"[manual] official_domains may only narrow the built-in official list; refused {bad}")


def _check_evidence(c: NewsConfig) -> dict[str, str]:
    """An enabled live provider must match a verification record produced by
    scripts/verify_news_providers.py (same type and URLs, successful, same date as verified_on).
    This stops an ordinary config edit from marking an unverified source as verified. It does NOT
    stop someone who edits the evidence file as well (see docs/news_provider_verification.md)."""
    live = [p for p in c.providers.values() if p.enabled and p.live]
    if not live:
        return {}
    try:
        from src.risk.news.event import strict_json_loads

        doc = strict_json_loads(PROVIDER_EVIDENCE_PATH.read_text(encoding="utf-8"))
        evidence, when = doc["providers"], str(doc["verified_at"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"provider verification evidence {PROVIDER_EVIDENCE_PATH} missing or invalid: {exc}") from exc
    out = {}
    for p in live:
        e = evidence.get(p.name)
        if not isinstance(e, dict) or e.get("ok") is not True or e.get("type") != p.type \
                or list(e.get("urls", [])) != list(p.all_urls()) or when[:10] != p.verified_on:
            raise ConfigError(f"[providers.{p.name}] has no matching successful verification record in "
                              f"{PROVIDER_EVIDENCE_PATH.name} (run scripts/verify_news_providers.py)")
        out[p.name] = when
    return out

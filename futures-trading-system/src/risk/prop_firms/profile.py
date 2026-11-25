"""Prop firm account profiles: data model, strict loading, verification status and identity.

A profile is DATA (a TOML file), never code: the engine has no firm-specific logic. Every rule
carries its source references, verification state, effective date and ambiguity notes.

Profile file layout (``config/prop_firms/profiles/<profile id>/<version>.toml``)::

    [profile]       identity: id, version, firm, product, stage, account size/type, initial balance,
                    timezone, trading-day start, declared status, description
    [verification]  how / when the rules were retrieved, human review fields
    [internal]      OUR OWN stricter margins (safety reserve, flatten reserve, uncertainty reserve,
                    entry cut-off, quantity policy). These are not firm rules and are labelled so.
    [[sources]]     id, title, url, retrieved, effective
    [[rules]]       id, type, description, applicable, mandatory, safety_critical, severity,
                    verification, sources, effective, ambiguity, stages, params

The EFFECTIVE status is computed, never trusted from the file alone:

* DISABLED / EXPIRED declared in the file are kept;
* a mandatory applicable rule with missing parameters makes the profile INCOMPLETE;
* a mandatory applicable rule that is not VERIFIED makes it UNVERIFIED;
* VERIFIED requires the file to declare VERIFIED AND a human review (``reviewed_by`` / ``reviewed_on``);
* otherwise it is DRAFT.

Only an effective VERIFIED profile with a matching human activation record is operational
(see :mod:`src.risk.prop_firms.versions`). Nothing here selects or substitutes a profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.config import ConfigError

PROFILE_STATUSES = ("DRAFT", "INCOMPLETE", "UNVERIFIED", "VERIFIED", "EXPIRED", "DISABLED")
RULE_VERIFICATION = ("VERIFIED", "UNVERIFIED", "CONFLICTING", "NOT_FOUND")
SEVERITIES = ("block", "warning", "info")
STAGES = ("challenge", "funded", "any")
_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,79}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-]{0,39}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Required parameters per rule type (the engine's generic evaluators). A mandatory applicable rule
# without them makes the profile INCOMPLETE.
RULE_TYPES: dict[str, tuple[str, ...]] = {
    "max_loss": ("amount", "model"),                     # static | trailing_intraday | trailing_eod
    "daily_loss": ("amount", "includes_unrealized"),
    "max_contracts": ("limit_micro_equivalent", "weights"),
    "instrument_allowlist": ("roots",),
    "flat_by": ("time",),                                # positions flat by this local time each trading day
    "trading_hours": ("day_start",),                     # no entries between flat_by and the next day start
    "news_restriction": ("minutes_before", "minutes_after"),
    "holiday_schedule": ("calendar",),                   # firm-published holiday / early-close schedule
    "consistency": ("max_day_share", "of"),
    "profit_target": ("amount",),
    "min_trading_days": ("days",),
    "payout_benchmark_days": ("days", "min_day_profit"),
    "payout_min_profit": ("amount",),
    "payout_min_withdrawal": ("amount",),
    "payout_max_withdrawal": ("share_of_profit", "cap"),
    "other": (),
}
MAX_LOSS_MODELS = ("static", "trailing_intraday", "trailing_eod")


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    retrieved: str
    effective: str = ""
    note: str = ""


@dataclass(frozen=True)
class RuleSpec:
    id: str
    type: str
    description: str
    applicable: bool = True
    mandatory: bool = True
    safety_critical: bool = False
    severity: str = "block"
    verification: str = "UNVERIFIED"
    sources: tuple[str, ...] = ()
    effective: str = ""
    ambiguity: str = ""
    stages: tuple[str, ...] = ("any",)
    params: dict[str, Any] = field(default_factory=dict)

    def applies_to(self, stage: str) -> bool:
        return self.applicable and ("any" in self.stages or stage in self.stages)

    @property
    def missing_params(self) -> list[str]:
        return [k for k in RULE_TYPES.get(self.type, ()) if self.params.get(k) in (None, "")]


@dataclass(frozen=True)
class InternalLimits:
    """Our own margins on top of the firm's thresholds (never presented as firm rules)."""

    safety_reserve: float = 0.0          # block new entries when official buffer - uncertainty <= this
    flatten_reserve: float = 0.0         # flatten open positions when official buffer - uncertainty <= this
    uncertainty_reserve: float = 0.0     # $ subtracted from the official buffer (fees / fill / data differences)
    entry_cutoff_minutes: float = 0.0    # no new entries this many minutes before flat_by
    flatten_lead_minutes: float = 0.0    # flatten this many minutes before flat_by
    gap_reserve_ticks: float = 0.0       # per contract, added to trade risk at fill time
    quantity_policy: str = "reject"      # "reject" | "reduce" (reduce only if explicitly configured)
    require_trade_risk_within_buffer: bool = True


@dataclass(frozen=True)
class Profile:
    id: str
    version: str
    firm: str
    product: str
    stage: str
    account_size: float
    account_type: str
    initial_balance: float
    currency: str
    timezone: str
    day_start: str
    declared_status: str
    description: str
    verification: dict[str, Any]
    internal: InternalLimits
    sources: tuple[Source, ...]
    rules: tuple[RuleSpec, ...]
    path: str = ""

    # ------------------------------------------------------------------ identity
    def canonical(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("path", None)
        return d

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, default=str,
                                         separators=(",", ":")).encode()).hexdigest()

    @property
    def ruleset_sha256(self) -> str:
        """Identity of the rule set alone (rules + internal limits), independent of descriptive text."""
        rules = [{"id": r.id, "type": r.type, "applicable": r.applicable, "mandatory": r.mandatory,
                  "safety_critical": r.safety_critical, "severity": r.severity, "verification": r.verification,
                  "stages": list(r.stages), "params": r.params} for r in self.rules]
        return hashlib.sha256(json.dumps({"rules": rules, "internal": asdict(self.internal), "stage": self.stage,
                                          "initial_balance": self.initial_balance, "timezone": self.timezone,
                                          "day_start": self.day_start}, sort_keys=True, default=str).encode()).hexdigest()

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def rule(self, rule_type: str) -> RuleSpec | None:
        """The applicable rule of a type for this profile's stage (None when the profile has none)."""
        for r in self.rules:
            if r.type == rule_type and r.applies_to(self.stage):
                return r
        return None

    def rules_of(self, rule_type: str) -> list[RuleSpec]:
        return [r for r in self.rules if r.type == rule_type and r.applies_to(self.stage)]

    # ------------------------------------------------------------------ status
    def status_report(self) -> dict[str, Any]:
        problems: list[str] = []
        incomplete = [f"{r.id}: missing {r.missing_params}" for r in self.rules
                      if r.applies_to(self.stage) and r.mandatory and r.missing_params]
        unverified = [f"{r.id}: {r.verification}" + (f" ({r.ambiguity})" if r.ambiguity else "")
                      for r in self.rules if r.applies_to(self.stage) and r.mandatory and r.verification != "VERIFIED"]
        reviewed = bool(str(self.verification.get("reviewed_by", "")).strip()) and \
            bool(_DATE.match(str(self.verification.get("reviewed_on", ""))))
        if self.declared_status in ("DISABLED", "EXPIRED"):
            status = self.declared_status
        elif incomplete:
            status = "INCOMPLETE"
        elif unverified:
            status = "UNVERIFIED"
        elif self.declared_status == "VERIFIED" and reviewed:
            status = "VERIFIED"
        elif self.declared_status == "VERIFIED":
            status = "UNVERIFIED"
            problems.append("declared VERIFIED but no human review recorded ([verification] reviewed_by / reviewed_on)")
        else:
            status = "DRAFT" if self.declared_status == "DRAFT" else "UNVERIFIED"
        return {"status": status, "declared_status": self.declared_status, "incomplete_rules": incomplete,
                "unverified_rules": unverified, "human_reviewed": reviewed, "problems": problems,
                "safety_critical_unresolved": [r.id for r in self.rules if r.applies_to(self.stage)
                                               and r.safety_critical and (r.verification != "VERIFIED" or r.missing_params)]}

    @property
    def status(self) -> str:
        return self.status_report()["status"]


# ---------------------------------------------------------------------------------------------- loading
_PROFILE_KEYS = {"id", "version", "firm", "product", "stage", "account_size", "account_type", "initial_balance",
                 "currency", "timezone", "day_start", "status", "description"}
_RULE_KEYS = {"id", "type", "description", "applicable", "mandatory", "safety_critical", "severity", "verification",
              "sources", "effective", "ambiguity", "stages", "params"}
_SOURCE_KEYS = {"id", "title", "url", "retrieved", "effective", "note"}
_VERIFICATION_KEYS = {"retrieved", "method", "reviewed_by", "reviewed_on", "notes"}


def _num(v: Any, name: str, *, positive: bool = False, allow_zero: bool = True) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ConfigError(f"{name} must be a finite number, got {v!r}")
    if v < 0 or (positive and v == 0) or (not allow_zero and v == 0):
        raise ConfigError(f"{name} must be {'positive' if positive else 'non-negative'}, got {v!r}")
    return float(v)


def _validate_params(r: RuleSpec, where: str) -> None:
    p = r.params
    for k, v in p.items():
        if isinstance(v, float) and not math.isfinite(v):
            raise ConfigError(f"{where}: rule {r.id} param {k} must be finite")
    if r.type == "max_loss":
        if p.get("model") is not None and p["model"] not in MAX_LOSS_MODELS:
            raise ConfigError(f"{where}: rule {r.id} model must be one of {MAX_LOSS_MODELS}")
        if p.get("amount") is not None:
            _num(p["amount"], f"{where}: {r.id}.amount", positive=True)
        if p.get("lock_at") is not None:
            _num(p["lock_at"], f"{where}: {r.id}.lock_at", positive=True)
        if p.get("reference", "balance") not in ("balance", "equity"):
            raise ConfigError(f"{where}: rule {r.id} reference must be 'balance' or 'equity'")
        if p.get("breach_when", "at_or_below") not in ("at_or_below", "below"):
            raise ConfigError(f"{where}: rule {r.id} breach_when must be 'at_or_below' or 'below'")
    elif r.type == "max_contracts":
        if p.get("limit_micro_equivalent") is not None:
            _num(p["limit_micro_equivalent"], f"{where}: {r.id}.limit_micro_equivalent", positive=True)
        w = p.get("weights")
        if w is not None:
            if not isinstance(w, dict) or not w:
                raise ConfigError(f"{where}: rule {r.id} weights must be a table root -> micro-equivalent weight")
            for root, wt in w.items():
                _num(wt, f"{where}: {r.id}.weights.{root}", positive=True)
    elif r.type in ("flat_by", "trading_hours"):
        for k in ("time", "day_start"):
            if p.get(k) is not None and not _HHMM.match(str(p[k])):
                raise ConfigError(f"{where}: rule {r.id} {k} must be HH:MM")
    elif r.type == "consistency":
        if p.get("max_day_share") is not None and not 0 < float(p["max_day_share"]) < 1:
            raise ConfigError(f"{where}: rule {r.id} max_day_share must be in (0, 1)")
    elif r.type == "instrument_allowlist":
        if p.get("roots") is not None and (not isinstance(p["roots"], list) or not all(isinstance(x, str) for x in p["roots"])):
            raise ConfigError(f"{where}: rule {r.id} roots must be a list of strings")
    for k in ("amount", "days", "min_day_profit", "cap", "minutes_before", "minutes_after"):
        if p.get(k) is not None:
            _num(p[k], f"{where}: {r.id}.{k}")


def load_profile(path: str | Path) -> Profile:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read prop firm profile {p.name}: {exc}") from exc
    unknown = set(raw) - {"profile", "verification", "internal", "sources", "rules"}
    if unknown:
        raise ConfigError(f"{p.name}: unknown section(s) {sorted(unknown)}")
    head = raw.get("profile") or {}
    if set(head) - _PROFILE_KEYS:
        raise ConfigError(f"{p.name}: unknown [profile] key(s) {sorted(set(head) - _PROFILE_KEYS)}")
    for k in ("id", "version", "firm", "product", "stage", "initial_balance", "timezone"):
        if head.get(k) in (None, ""):
            raise ConfigError(f"{p.name}: [profile] {k} is required")
    if not _ID.match(str(head["id"])) or not _VERSION.match(str(head["version"])):
        raise ConfigError(f"{p.name}: invalid profile id or version")
    if head["stage"] not in ("challenge", "funded"):
        raise ConfigError(f"{p.name}: stage must be 'challenge' or 'funded'")
    status = head.get("status", "DRAFT")
    if status not in PROFILE_STATUSES:
        raise ConfigError(f"{p.name}: status must be one of {PROFILE_STATUSES}")
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(str(head["timezone"]))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{p.name}: unknown timezone {head['timezone']!r}") from exc
    day_start = str(head.get("day_start", "00:00"))
    if not _HHMM.match(day_start):
        raise ConfigError(f"{p.name}: day_start must be HH:MM")
    ver = raw.get("verification") or {}
    if set(ver) - _VERIFICATION_KEYS:
        raise ConfigError(f"{p.name}: unknown [verification] key(s) {sorted(set(ver) - _VERIFICATION_KEYS)}")
    internal_raw = raw.get("internal") or {}
    known_internal = set(InternalLimits.__dataclass_fields__)
    if set(internal_raw) - known_internal:
        raise ConfigError(f"{p.name}: unknown [internal] key(s) {sorted(set(internal_raw) - known_internal)}")
    for k, v in internal_raw.items():
        if k == "quantity_policy":
            if v not in ("reject", "reduce"):
                raise ConfigError(f"{p.name}: internal.quantity_policy must be 'reject' or 'reduce'")
        elif k == "require_trade_risk_within_buffer":
            if not isinstance(v, bool):
                raise ConfigError(f"{p.name}: internal.{k} must be true/false")
        else:
            _num(v, f"{p.name}: internal.{k}")
    internal = InternalLimits(**internal_raw)
    if internal.flatten_reserve > internal.safety_reserve:
        raise ConfigError(f"{p.name}: internal.flatten_reserve must be <= internal.safety_reserve")
    sources = []
    for s in raw.get("sources", []):
        if set(s) - _SOURCE_KEYS:
            raise ConfigError(f"{p.name}: unknown source key(s) {sorted(set(s) - _SOURCE_KEYS)}")
        if not s.get("id") or not str(s.get("url", "")).startswith("https://"):
            raise ConfigError(f"{p.name}: every source needs an id and an https url")
        sources.append(Source(**{k: str(v) for k, v in s.items()}))
    src_ids = {s.id for s in sources}
    if len(src_ids) != len(sources):
        raise ConfigError(f"{p.name}: duplicate source ids")
    rules = []
    for r in raw.get("rules", []):
        if set(r) - _RULE_KEYS:
            raise ConfigError(f"{p.name}: unknown rule key(s) {sorted(set(r) - _RULE_KEYS)} in {r.get('id')}")
        if not r.get("id") or r.get("type") not in RULE_TYPES:
            raise ConfigError(f"{p.name}: rule {r.get('id')!r} has an unknown type {r.get('type')!r}")
        if r.get("severity", "block") not in SEVERITIES:
            raise ConfigError(f"{p.name}: rule {r['id']} severity must be one of {SEVERITIES}")
        if r.get("verification", "UNVERIFIED") not in RULE_VERIFICATION:
            raise ConfigError(f"{p.name}: rule {r['id']} verification must be one of {RULE_VERIFICATION}")
        stages = tuple(r.get("stages", ["any"]))
        if not set(stages) <= set(STAGES):
            raise ConfigError(f"{p.name}: rule {r['id']} stages must be within {STAGES}")
        refs = tuple(r.get("sources", []))
        if set(refs) - src_ids:
            raise ConfigError(f"{p.name}: rule {r['id']} cites unknown source(s) {sorted(set(refs) - src_ids)}")
        spec = RuleSpec(id=str(r["id"]), type=r["type"], description=str(r.get("description", "")),
                        applicable=bool(r.get("applicable", True)), mandatory=bool(r.get("mandatory", True)),
                        safety_critical=bool(r.get("safety_critical", False)), severity=r.get("severity", "block"),
                        verification=r.get("verification", "UNVERIFIED"), sources=refs,
                        effective=str(r.get("effective", "")), ambiguity=str(r.get("ambiguity", "")), stages=stages,
                        params=dict(r.get("params", {})))
        _validate_params(spec, p.name)
        if spec.applicable and spec.verification == "VERIFIED" and not refs:
            raise ConfigError(f"{p.name}: rule {spec.id} is VERIFIED but cites no source")
        rules.append(spec)
    if len({r.id for r in rules}) != len(rules):
        raise ConfigError(f"{p.name}: duplicate rule ids")
    return Profile(id=str(head["id"]), version=str(head["version"]), firm=str(head["firm"]), product=str(head["product"]),
                   stage=head["stage"], account_size=_num(head.get("account_size", head["initial_balance"]),
                                                          f"{p.name}: account_size", positive=True),
                   account_type=str(head.get("account_type", "")),
                   initial_balance=_num(head["initial_balance"], f"{p.name}: initial_balance", positive=True),
                   currency=str(head.get("currency", "USD")), timezone=str(head["timezone"]), day_start=day_start,
                   declared_status=status, description=str(head.get("description", "")), verification=dict(ver),
                   internal=internal, sources=tuple(sources), rules=tuple(rules), path=str(p))

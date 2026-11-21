"""Strict loader for config/paper.toml (unknown keys refused; no risk/account settings here)."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT, ConfigError

DEFAULT_PAPER_CONFIG_PATH = PROJECT_ROOT / "config" / "paper.toml"
MODES = ("infrastructure_validation", "replay", "paper")
SECTIONS = {
    "paper": {"mode", "instruments", "state_dir", "allow_reversal"},
    "data": {"stale_after_seconds", "resume_after_events", "reconnect_max_attempts", "reconnect_backoff_seconds",
             "max_price_jump_ticks", "warmup_sessions", "max_spread_ticks", "max_future_skew_seconds"},
    "execution": {"limit_participation", "quote_max_age_seconds"},
    "persistence": {"snapshot_every_events"},
    "shutdown": {"policy"},
}


@dataclass(frozen=True)
class PaperConfig:
    mode: str
    instruments: tuple[str, ...]
    state_dir: str
    allow_reversal: bool
    stale_after: pd.Timedelta
    resume_after_events: int
    reconnect_max_attempts: int
    reconnect_backoff: tuple[float, ...]
    max_price_jump_ticks: int
    warmup_sessions: int
    limit_participation: float
    snapshot_every_events: int
    shutdown_policy: str
    quote_max_age: pd.Timedelta = pd.Timedelta(seconds=5)
    max_spread_ticks: int = 40
    max_future_skew: pd.Timedelta = pd.Timedelta(seconds=5)


def _pos(v, name, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ConfigError(f"{name} must be a positive number")
    if integer and not isinstance(v, int):
        raise ConfigError(f"{name} must be an integer")
    return v


def load_paper_config(path: str | Path | None = None) -> PaperConfig:
    p = Path(path) if path else DEFAULT_PAPER_CONFIG_PATH
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read paper config {p}: {exc}") from exc
    for name, sec in raw.items():
        if name not in SECTIONS:
            raise ConfigError(f"Unknown section [{name}] in {p}: paper config cannot change risk or account rules")
        if set(sec) - SECTIONS[name]:
            raise ConfigError(f"Unknown key(s) {sorted(set(sec) - SECTIONS[name])} in [{name}]")
    for name in SECTIONS:
        if name not in raw:
            raise ConfigError(f"Missing section [{name}] in {p}")
    pa, d = raw["paper"], raw["data"]
    if pa.get("mode") not in MODES:
        raise ConfigError(f"[paper] mode must be one of {MODES}")
    inst = pa.get("instruments")
    if not isinstance(inst, list) or not inst or not all(i in ("MES", "MNQ") for i in inst):
        raise ConfigError("[paper] instruments must be a non-empty subset of MES, MNQ")
    if not isinstance(pa.get("allow_reversal"), bool):
        raise ConfigError("[paper] allow_reversal must be true or false")
    backoff = d.get("reconnect_backoff_seconds")
    if not isinstance(backoff, list) or not backoff:
        raise ConfigError("[data] reconnect_backoff_seconds must be a non-empty list")
    part = raw["execution"].get("limit_participation")
    if isinstance(part, bool) or not isinstance(part, (int, float)) or not 0 < part <= 1:
        raise ConfigError("[execution] limit_participation must be in (0, 1]")
    pol = raw["shutdown"].get("policy")
    if pol not in ("keep_protected", "flatten"):
        raise ConfigError("[shutdown] policy must be 'keep_protected' or 'flatten'")
    return PaperConfig(
        mode=pa["mode"], instruments=tuple(inst), state_dir=str(pa.get("state_dir", "data/paper")),
        allow_reversal=pa["allow_reversal"],
        stale_after=pd.Timedelta(seconds=_pos(d.get("stale_after_seconds"), "[data] stale_after_seconds")),
        resume_after_events=int(_pos(d.get("resume_after_events"), "[data] resume_after_events", True)),
        reconnect_max_attempts=int(_pos(d.get("reconnect_max_attempts"), "[data] reconnect_max_attempts", True)),
        reconnect_backoff=tuple(float(_pos(x, "[data] reconnect_backoff_seconds")) for x in backoff),
        max_price_jump_ticks=int(_pos(d.get("max_price_jump_ticks"), "[data] max_price_jump_ticks", True)),
        warmup_sessions=int(_pos(d.get("warmup_sessions"), "[data] warmup_sessions", True)),
        limit_participation=float(part),
        snapshot_every_events=int(_pos(raw["persistence"].get("snapshot_every_events"),
                                       "[persistence] snapshot_every_events", True)),
        shutdown_policy=pol,
        quote_max_age=pd.Timedelta(seconds=_pos(raw["execution"].get("quote_max_age_seconds"),
                                                "[execution] quote_max_age_seconds")),
        max_spread_ticks=int(_pos(d.get("max_spread_ticks"), "[data] max_spread_ticks", True)),
        max_future_skew=pd.Timedelta(seconds=_pos(d.get("max_future_skew_seconds"), "[data] max_future_skew_seconds")))

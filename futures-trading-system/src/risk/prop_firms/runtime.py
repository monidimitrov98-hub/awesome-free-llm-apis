"""Build the compliance engine for a session from an EXPLICIT selection (id + version).

No selection -> no engine (existing behaviour). A selection that cannot be loaded is an error: the
session never starts with another profile, an older version, or without the profile it asked for.
"""

from __future__ import annotations

from pathlib import Path

from src.config import ConfigError
from src.risk.prop_firms.engine import ComplianceEngine
from src.risk.prop_firms.versions import Activations, Registry, load_selection, operational_state


def build_engine(*, bt, specs: dict, profile_id: str | None = None, version: str | None = None,
                 selection_path: Path | None = None, registry: Registry | None = None,
                 activations: Activations | None = None) -> ComplianceEngine | None:
    if profile_id is None:
        sel = load_selection(selection_path)
        profile_id, version = sel["profile"], sel["version"]
    if not profile_id:
        return None
    if not version:
        raise ConfigError("a prop firm profile needs an exact version")
    reg = registry or Registry()
    prof = reg.get(profile_id, version)  # raises: never substitutes
    acts = activations or Activations()
    return ComplianceEngine(prof, operational=operational_state(prof, acts),
                            account_guard_rules=getattr(bt, "account_rules", None),
                            tick_values={r: s.tick_value for r, s in specs.items()},
                            operational_check=lambda: operational_state(prof, acts))

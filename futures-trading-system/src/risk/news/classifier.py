"""Event classification: category and impact from configured, case-insensitive patterns.

Classification is a risk PRECAUTION, not a prediction that the event will move the market.
A provider's impact hint can raise the configured impact but never lower it.
"""

from __future__ import annotations

from src.risk.news.config import NewsConfig
from src.risk.news.event import EventDataError, Impact

IMPACT_RANK = {Impact.LOW: 0, Impact.UNKNOWN: 1, Impact.MEDIUM: 2, Impact.HIGH: 3}
# more specific categories are tried first (e.g. "Core CPI" before "CPI")
PRECEDENCE = ("core_cpi", "core_pce", "fomc_statement", "fed_press_conference", "fomc_decision", "fed_chair_speech",
              "unemployment_rate", "jobless_claims")


def _order(cfg: NewsConfig) -> list[str]:
    """Mandatory categories are always tried before any other configured category, so a new
    "alias" category can never capture a mandatory release (and its weaker window)."""
    from src.risk.news.config import MANDATORY_CATEGORIES

    first = [c for c in PRECEDENCE if c in cfg.events]
    mandatory = [c for c in cfg.events if c in MANDATORY_CATEGORIES and c not in first]
    return first + mandatory + [c for c in cfg.events if c not in first and c not in mandatory]


def make_classifier(cfg: NewsConfig):
    order = _order(cfg)

    def classify(name: str, region: str, category: str | None = None, impact: str | None = None):
        hint = None
        if impact is not None:
            try:
                hint = Impact(impact.lower())
            except ValueError as exc:
                raise EventDataError(f"unknown impact {impact!r}") from exc
        if category is not None:
            if category not in cfg.events:
                raise EventDataError(f"unknown category {category!r}")
            cat = category
        else:
            cat = next((c for c in order if any(p.search(name) for p in cfg.events[c].compiled)), "unclassified")
        if cat == "unclassified":
            return cat, hint or Impact.UNKNOWN
        base = cfg.events[cat].impact
        return cat, base if hint is None or IMPACT_RANK[hint] <= IMPACT_RANK[base] else hint

    return classify

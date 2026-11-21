"""Write data/samples/SYNTHETIC_news_calendar.json: a SYNTHETIC point-in-time calendar for
the synthetic sample price files (trade dates 2025-03-10 .. 2025-03-18).

The events are invented for testing (names carry "(SYNTHETIC)", reliability "synthetic").
They are NOT real historical release times and must not be used to evaluate anything.
It exercises: daily refreshes with coverage, a time change published in advance, a
cancellation, an all-day event without a time, and an FOMC decision + press conference.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "samples" / "SYNTHETIC_news_calendar.json"


def ev(eid, name, when, known, **kw):
    return {"event_id": eid, "name": f"{name} (SYNTHETIC)", "scheduled_at": when, "timezone": "America/New_York",
            "known_at": known, "source": "synthetic_fixture", "reliability": "synthetic", "region": "US", **kw}


def main() -> None:
    known = "2025-03-01T12:00:00Z"
    refreshes = [{"time": f"2025-03-{d:02d}T11:00:00Z", "provider": "synthetic_fixture", "ok": True,
                  "coverage_start": f"2025-03-{d:02d}T11:00:00Z", "coverage_end": "2025-03-31T00:00:00Z",
                  "error": "", "n_events": 0} for d in range(1, 20)]
    events = [
        ev("cpi:2025-03-12", "Consumer Price Index", "2025-03-12T08:30:00", known),
        ev("ppi:2025-03-13", "Producer Price Index", "2025-03-13T08:30:00", known),
        ev("jobless_claims:2025-03-13", "Unemployment Insurance Weekly Claims", "2025-03-13T08:30:00", known),
        # time change published on 2025-03-12: never visible before then
        ev("retail_sales:2025-03-17", "Retail Sales", "2025-03-17T08:30:00", known),
        ev("retail_sales:2025-03-17", "Retail Sales", "2025-03-17T10:50:00", "2025-03-12T15:00:00Z", status="changed"),
        # cancelled event: opens no window once the cancellation is known
        ev("gdp:2025-03-14", "Gross Domestic Product", "2025-03-14T08:30:00", known),
        ev("gdp:2025-03-14", "Gross Domestic Product", "2025-03-14T08:30:00", "2025-03-13T20:00:00Z",
           status="cancelled"),
        # an event whose time is not confirmed: the whole day is blocked
        {"event_id": "fed_chair_speech:2025-03-11", "name": "Fed Chair speech (SYNTHETIC)", "date": "2025-03-11",
         "known_at": known, "source": "synthetic_fixture", "reliability": "synthetic", "region": "US"},
        ev("fomc_decision:2025-03-19", "FOMC rate decision", "2025-03-19T14:00:00", known),
        ev("fed_press_conference:2025-03-19", "FOMC press conference", "2025-03-19T14:30:00", known),
        # actual value published only at the release time
        ev("cpi:2025-03-12", "Consumer Price Index", "2025-03-12T08:30:00", "2025-03-12T12:30:00Z",
           status="released", actual="0.2"),
    ]
    doc = {"schema": "news-calendar/1", "synthetic": True,
           "note": "SYNTHETIC calendar for the synthetic sample data; not real release times.",
           "expected_providers": ["synthetic_fixture"], "provider_priority": {"synthetic_fixture": 1},
           "refreshes": refreshes, "events": events}
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

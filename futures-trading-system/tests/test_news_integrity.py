"""Sprint 4.1: the news filter inside the OOS ledger / manifest / replay safeguards, and the
calendar maintenance CLI. CLI runs use an isolated committed copy of the project."""

import json
import re

import pytest

import run_news_calendar
from src.risk.news.config import DEFAULT_NEWS_CONFIG_PATH
from tests.git_project import CSV, IS_OOS, cli, commit_all, copy_project, needs_git

CAL = "data/samples/SYNTHETIC_news_calendar.json"


@needs_git
def test_reveal_replay_and_calendar_change_with_news_filter(tmp_path):
    proj = copy_project(tmp_path)
    first = cli(proj, *IS_OOS, "--news-calendar", CAL, "--reveal-oos", "--init-ledger", "--output-dir", "output/run")
    assert first.returncode == 0, first.stderr
    m = json.loads((proj / "output/run/out_of_sample/r_2/manifest.json").read_text())
    assert "news" in m["config_texts"] and "news_calendar" in m["config_texts"]
    assert m["news"]["mode"] == "enforce" and len(m["news"]["calendar_sha256"]) == 64
    rep = cli(proj, "--replay", "output/run/out_of_sample/r_2/manifest.json")
    assert rep.returncode == 0 and "EXACT" in rep.stdout
    # a different calendar is a different configuration: the revealed OOS cannot be reused silently
    cal = proj / CAL
    cal.write_text(cal.read_text().replace("10:50:00", "10:55:00"), encoding="utf-8")
    commit_all(proj, "edit calendar")
    again = cli(proj, *IS_OOS, "--news-calendar", CAL, "--reveal-oos", "--output-dir", "output/run2")
    assert again.returncode == 4 and "different configuration or code" in again.stderr
    # sealed OOS stays sealed with the news filter too
    sealed = cli(proj, *IS_OOS, "--news-calendar", CAL, "--output-dir", "output/run3")
    assert sealed.returncode == 0 and "SEALED" in sealed.stdout


def _cfg(tmp_path, manual_path):
    text = DEFAULT_NEWS_CONFIG_PATH.read_text().replace('path = "data/news/manual_calendar.json"',
                                                         f'path = "{manual_path.as_posix()}"')
    # no network in unit tests: live providers off; the manual file alone must cover the required categories
    text = text.replace('type = "ics"\nenabled = true', 'type = "ics"\nenabled = false')
    text = text.replace('type = "census_html"\nenabled = true', 'type = "census_html"\nenabled = false')
    text = text.replace('type = "fomc_html"\nenabled = true', 'type = "fomc_html"\nenabled = false')
    from src.risk.news.config import MANDATORY_CATEGORIES

    # required categories cannot be dropped (audit fix): the manual provider claims all of them instead
    allcats = json.dumps(sorted(MANDATORY_CATEGORIES))
    text, n = re.subn(r"(\[providers\.manual\][\s\S]*?covers = )\[[^\]]*\]", lambda m: m.group(1) + allcats, text)
    assert n == 1
    assert text.count("enabled = true") == 2  # [news] and the manual provider only
    p = tmp_path / "news.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_calendar_cli_refresh_status_fail_closed(tmp_path, capsys):
    manual = tmp_path / "manual.json"
    cfg = _cfg(tmp_path, manual)
    store = tmp_path / "store.json"
    assert run_news_calendar.main(["--config", str(cfg), "--status", "--store", str(store)]) == 8  # no data: blocked
    assert run_news_calendar.main(["--config", str(cfg), "--refresh", "--store", str(store)]) == 0
    assert "failed" in capsys.readouterr().out  # manual file missing -> failed refresh recorded
    assert run_news_calendar.main(["--config", str(cfg), "--status", "--store", str(store)]) == 8
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "PROVIDER_UNAVAILABLE" and not out["entries_allowed"]
    manual.write_text(json.dumps({"coverage_start": "2020-01-01T00:00:00Z", "coverage_end": "2099-01-01T00:00:00Z",
                                  "events": []}), encoding="utf-8")
    assert run_news_calendar.main(["--config", str(cfg), "--refresh", "--store", str(store)]) == 0
    capsys.readouterr()
    assert run_news_calendar.main(["--config", str(cfg), "--status", "--store", str(store),
                                   "--json", str(tmp_path / "dash.json")]) == 0
    dash = json.loads((tmp_path / "dash.json").read_text())
    assert dash["state"] == "NORMAL" and dash["provider_health"]["manual"]["status"] == "ok"


def test_calendar_cli_validate_reports_gaps(capsys):
    assert run_news_calendar.main(["--validate", CAL, "--start", "2025-02-01", "--end", "2025-03-20"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["gaps"] and rep["synthetic_calendar"]  # nothing covers February: reported, never assumed quiet

# Adversarial audit — Sprint 4.1 news & economic event protection

- **Commit audited:** `92f9b57`.
- **Method:** I read the code path from calendar refresh to readiness to entry authorization,
  ran probe scripts, and wrote one regression test per finding (`tests/test_news_audit.py`).
- **Result:** every finding below is fixed in the commit that adds this file.

## Findings

| ID | Severity | Where | Problem at 92f9b57 | Fix |
| --- | --- | --- | --- | --- |
| H1 | HIGH | `src/risk/news/manual.py::validate_manual` | A manual category marked `verified` with `verified_through` far ahead but **no events** counted as covered. With every category like that, `--readiness` returned OPERATIONAL READY and NewsGuard allowed entries through CPI/NFP days that were never listed | Release-frequency plausibility: monthly releases must appear every ≤ 40 days (jobless claims ≤ 8, Treasury ≤ 100) across [today, verified_through]; release groups (CPI/Core CPI, Employment Situation) count once. Otherwise the category is not covered |
| H2 | HIGH | `src/risk/news/config.py::load_news_config` | Ordinary config edits removed protection: `required_categories = []` gave READY with an empty calendar; CPI impact `low` (no window); zero windows; instrument regions without `US` (no US events apply); unlimited `max_age` and manual verification age; tiny lookahead and horizon | `_safety_floors`: mandatory categories, minimum impacts in `[impact] block`, minimum windows (overrides included), `US` region, bounds on freshness and horizon, official domains limited to a built-in list |
| H3 | HIGH | `src/risk/news/guard.py::NewsGuard._integrity` | A live provider's coverage claim came only from config `covers`: adding `"cpi"` to BEA's covers made CPI "covered" (READY with an unresolved manual calendar) | `coverage_problem`: a live provider covers a category only if its own data lists an upcoming event of it (or of the release carrying it) |
| M1 | MEDIUM | `src/risk/news/config.py` | Any live provider (for example BLS) could be "verified" by setting `verified = true`, `verified_on` and `documentation` | `_check_evidence`: enabled live providers must match a successful record in `config/risk/provider_verification.json` (type, URLs, date). Readiness also requires evidence < 90 days old |
| M2 | MEDIUM | JSON loading (`providers.JsonFileProvider`, `calendar.CalendarStore.from_json`, `readiness.load_manual`, CLI) | Duplicate keys were silently resolved to the last value (`"status": "unresolved", "status": "verified"` → verified); NaN/Infinity accepted; deep nesting raised an uncaught `RecursionError` | `event.strict_json_loads`: duplicate keys, NaN/Infinity and nesting > 32 refused |
| M3 | MEDIUM | `manual.validate_manual` | Contradictory records accepted (CPI 08:30 and Core CPI 10:00 on the same day) | Error: one release, one time (Fed Chair appearances and Treasury exempt) |
| M4 | MEDIUM | `readiness.readiness_report`, `guard._provider_health` | With `provider_failure_policy = "use_fresh_cache"`, a provider whose latest refresh failed was reported `ok`, and readiness said OPERATIONAL READY | Status `degraded_cached`; readiness requires `ok` and reports PROVIDER UNAVAILABLE |
| M5 | MEDIUM | `src/backtest/engine.py` (entry fill), `guard.status` | The fill re-check used only the execution bar's open time. With 5-minute execution bars, a limit order could fill inside a window starting within the bar | The fill check covers `[bar open, bar open + bar length)` |
| M6 | MEDIUM | `guard.NewsGuard` vs `readiness` | Entry authorization accepted synthetic or unknown-provenance data that readiness rejects (latent: no operational entry path exists yet) | `NewsGuard(operational=True)` applies the readiness rules; readiness uses that guard and the same `coverage_problem` |
| L1 | LOW | `calendar._category_coverage` | A historical store could record a manual verification made after the refresh that used it (look-ahead in backtests) | Refused when loading |
| L2 | LOW | `providers.https_get` | Only per-operation socket timeouts; a server trickling bytes could hold a refresh open indefinitely | Total deadline of 3 × timeout |
| L3 | LOW | `manual.official_url` | `https://www.bls.gov:evil/x` (malformed port) was accepted as an official source | Ports, userinfo and malformed netlocs refused |

## Verified, no change needed

- **Single entry path.** `_submit_entry` is the only place that creates entry orders. It asks
  NewsGuard before sizing, RiskManager and AccountGuard, and the fill path asks again.
  `test_l6_single_entry_creation_site_in_the_engine` keeps it that way.
- **Exits and protection.** Stop-loss, take-profit, strategy exits, AccountGuard flatten and
  session flatten never consult NewsGuard (`tests/test_news_engine.py`).
- **Time and DST.** Ambiguous and nonexistent New York times are refused. Date-only events
  block exactly the New York day (23 h, 25 h and 24 h days tested).
- **Untrusted input.** XML DOCTYPE/ENTITY declarations, non-UTF-8 data, oversized responses,
  cross-host and http redirects are refused (`tests/test_news_hardening.py`).
- **Backtests.** Synthetic calendars are refused with real prices. Point-in-time `known_at`
  prevents look-ahead. The calendar text is part of the manifest and the validation
  fingerprint, so changing it is a new configuration: reveals over the same OOS dates are
  refused (exit 4), and an existing manifest still replays with its embedded calendar.

## Remaining risks (not fixed: outside code control or by design)

- **Evidence is not cryptographic.** Someone who edits both the config and
  `provider_verification.json`, or the code, can still enable an unverified provider. The
  binding stops accidental and ordinary edits only.
- **Manual data is trusted as entered.** A wrongly transcribed date or time, or a false
  `verified_through` that satisfies the frequency floor, is not detectable.
- **Historical `known_at` is trusted.** An author who back-dates the time a schedule change
  became known creates look-ahead in their own backtests.
- **Stale content behind HTTP 200.** A provider serving old content that still lists future
  events is not detected.
- **Research helpers.** `run_strategy(news=None)` and `run_from_csv` run without the news
  filter. No CLI uses `run_from_csv`; `run_backtest.py` always applies the configured filter.
- **Explicit degraded policy.** With `use_fresh_cache`, entry authorization uses cached data
  while a provider fails, but readiness reports PROVIDER UNAVAILABLE.
- **Not tested.** Real network timeouts and TLS behaviour against live servers; only the
  transport logic was tested with stubs.

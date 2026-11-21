# Economic calendar providers: live verification

Verified on **2026-11-21** from the cloud development environment, with network access
restricted to the five official hosts listed below. Machine-readable results are in
`docs/news_provider_verification.json`, produced by:

```
python scripts/verify_news_providers.py --out docs/news_provider_verification.json
```

The script uses the production fetch and parse code and writes nothing to the calendar store.
Run it again before relying on a provider, after any parser change, and periodically.
Agencies change page layouts without notice.

## Summary

| Provider | Endpoint | Result | Status in config |
| --- | --- | --- | --- |
| `bea` | `https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics` | 169 events (2025-01-07 .. 2027-12-22), 55 future | **enabled, verified** |
| `census` | `https://www.census.gov/economic-indicators/calendar-listview-2026.html` and `.../calendar-listview.html` (2027) | 342 events (2026-01-07 .. 2027-12-27), 183 future | **enabled, verified** |
| `fomc` | `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` | 54 meetings 2021-2027 (8 per year), dates only | **enabled, verified** |
| `fed_press` | `https://www.federalreserve.gov/feeds/press_monetary.xml` | RSS 2.0, 15 items, newest 2026-11-18 | verified; **disabled** until `[unexpected]` is enabled |
| `bls` | `https://www.bls.gov/schedule/news_release/bls.ics` | **HTTP 403** ("Access Denied", bot policy) | **disabled, NOT verified** |

The rest of this document is the per-provider evidence.

## BEA: Bureau of Economic Analysis release calendar (iCalendar)

- **Source.** Linked as "online-calendar-subscription.ics" from https://www.bea.gov/news/schedule.
  robots.txt does not disallow it. BEA material is US government work.
- **Format.** iCalendar 2.0 (`text/plain; charset=utf-8`). `DTSTART;VALUE=DATE-TIME:YYYYMMDDTHHMMSSZ`
  is in **UTC**; `X-WR-TIMEZONE:America/New_York`; `DURATION:PT30M`; one VEVENT per release.
  Long summaries are folded across lines (unfolded by the parser).
- **Timestamps.** UTC converts to 08:30 ET for 139 events and 10:00 ET for 29, so the DST
  offsets are correct.
- **Coverage.** GDP (41), Personal Income and Outlays (PCE, 39), International Trade (38),
  and annual and structural statistics (classified `minor_statistics`, low impact).
  Core PCE is part of the Personal Income and Outlays release.
- **Changes and cancellations.** There is no `STATUS:CANCELLED` in the feed. Revised entries carry
  `SEQUENCE > 0` (31 events) and are stored as `changed`. Changes are detected point-in-time by
  comparing refreshes (`known_at` = fetch time). A cancellation would appear as a removed event,
  and a removed event is never treated as cancelled. The last stored version keeps blocking
  (conservative).
- **Update frequency.** HTTP `Last-Modified: Wed, 04 Nov 2026`, `cache-control: s-maxage=60`.
  Every `DTSTAMP` is 2025-09-23, so `DTSTAMP` does not show changes.
- **Also available, not used.** `https://apps.bea.gov/API/signup/release_dates.json` (JSON,
  release name to UTC timestamps, `Last-Modified: 26 Oct 2026`). It could serve as a second
  BEA source for cross-checks.

## Census Bureau: Economic Indicator Release Schedule (HTML list view)

- **Source.** https://www.census.gov/economic-indicators/calendar-listview.html, which shows
  2027 at the time of verification, plus `calendar-listview-2026.html`. robots.txt does not
  disallow them. Census material is US government work.
- **Format.** HTML table `id="calendar"` with columns Indicator, Release Date
  ("January 15, 2027"), Time ("8:30 AM") and Period Covered. It is parsed with the
  standard-library HTML tokenizer. The parser fails closed if the table or its column
  headers change.
- **Timezone.** The page states none. Verified as **US Eastern**: BEA and Census both list the
  joint "U.S. International Trade in Goods and Services" release, and on all **25** common
  dates the Census time interpreted as New York time equals the BEA UTC time.
- **Coverage.** Retail sales, durable goods, housing starts, new home sales, trade, construction
  spending, wholesale trade, factory orders, business formation, and others.
- **Changes and cancellations.** Some rows show "Suspended" instead of a date (4 in 2026). They are
  stored as `postponed` without a date and open no window. A rescheduled date appears as a new
  date at the next refresh.
- **Update frequency.** The page is regenerated continuously (`Last-Modified` equal to the fetch
  time). The next year's page appears late in the year: keep both year URLs configured
  around the turn of the year.
- **Not usable.** `econcards/assets/xml/indicator.xml` is a 2021 snapshot of indicator values,
  not a calendar.

## Federal Reserve: FOMC meeting calendar (HTML)

- **Source.** https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm. There is no
  robots.txt on the host (HTTP 404). Federal Reserve Board website content is public domain
  except where noted (see the "Website Policies" link on the page).
- **Format.** Sections "YYYY FOMC Meetings" with meeting date ranges such as "January 27-28",
  "April 30-May 1" or "September 15-16*" (* = Summary of Economic Projections). 2027 dates
  are **tentative** ("Each meeting date is tentative until confirmed at the meeting immediately
  preceding it").
- **Timestamps.** The page gives **no times**. Each meeting's last day produces three date-only
  events: decision, statement and press conference. They block the **whole New York day**
  unless a timed source (the manual file) refines them for that day. No time is ever assumed.
- **Excluded by the parser.** "(Released <date>)" minutes dates, "(notation vote)" entries,
  "Note: ..." text (for example the 2028 meeting), "Last Update: ...".
- **Changes.** Schedule revisions are announced in press releases (for example "Federal Reserve
  announces revised tentative schedule of FOMC meetings", 2026-11-12, in the monetary RSS feed)
  and appear on the page at the next refresh. `Last-Modified: 18 Nov 2026`.

## Federal Reserve: monetary-policy press releases (RSS)

- **Source.** https://www.federalreserve.gov/feeds/press_monetary.xml, listed on
  https://www.federalreserve.gov/feeds/feeds.htm. The feed `press_all.xml` exists too.
- **Format.** RSS 2.0, UTF-8 with a byte-order mark, `pubDate` in RFC 822 GMT (for example
  "Wed, 28 Oct 2026 18:00:00 GMT" = 14:00 ET for an FOMC statement). No DOCTYPE.
- **Use.** Unexpected official announcements (`[unexpected]`, disabled by default). The keyword
  rules also match **scheduled** releases such as statements and minutes. That is conservative,
  not a precise detector of surprises.

## BLS: Bureau of Labor Statistics release calendar (iCalendar) — NOT VERIFIED

- **Endpoint.** `https://www.bls.gov/schedule/news_release/bls.ics` (documented at
  https://www.bls.gov/help/hlpiCAL.htm).
- **Result.** HTTP 403 "Access Denied": "bot activity that doesn't conform to BLS usage policy
  is prohibited". BLS may block automated clients whose identifying information has no way to
  contact the operator (https://www.bls.gov/bls/pss.htm). The request was **not** retried with
  a disguised browser User-Agent.
- **What it would cover.** CPI and Core CPI, Employment Situation (NFP, unemployment rate), PPI,
  JOLTS and more. These categories are **not covered by any verified live source**.
- **Verification procedure for an environment with internet access.**
  1. `set FTS_HTTP_CONTACT=<your e-mail>` (PowerShell: `$env:FTS_HTTP_CONTACT="..."`). The
     client then sends `User-Agent: futures-research-calendar/1.0 (contact: <e-mail>)`.
  2. `python scripts/verify_news_providers.py --out bls_check.json`
  3. Check that `providers.bls.ok` is true, that the events have `America/New_York` times
     (08:30 for CPI, PPI and NFP), and that `categories` includes cpi, nfp and ppi.
  4. Only then set `verified = true`, `verified_on`, `documentation` and `enabled = true` in
     `[providers.bls]`.

## Coverage gaps (required categories)

| Category | Verified live source | Until then |
| --- | --- | --- |
| CPI / Core CPI | none (BLS unverified) | manual file |
| NFP / unemployment rate | none (BLS unverified) | manual file |
| PPI | none (BLS unverified) | manual file |
| Initial jobless claims | none (DOL weekly, Thursdays; not verified) | manual file |
| ISM manufacturing / services | none (ISM data is licensed) | manual file |
| Fed Chair speeches, testimony | none (no machine-readable official schedule verified) | manual file |
| Treasury refunding | none | manual file |
| FOMC decision / press conference **times** | dates only (FOMC page) | whole day blocked unless the manual file gives times |

NewsGuard blocks new entries (`CALENDAR_INCOMPLETE`) while any category in
`[coverage] required_categories` has no healthy provider. With the default configuration
that includes the **manual** provider. Without a maintained `data/news/manual_calendar.json`,
operational use is blocked by design.

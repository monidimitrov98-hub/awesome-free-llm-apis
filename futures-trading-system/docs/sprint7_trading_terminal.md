# Sprint 7 — FTS Terminal (read-only professional trading terminal)

The FTS Terminal is a local desktop and browser application. It shows what the existing Python
system is doing: market data, strategy decisions, market regime, simulated positions, risk
protection, the paper trade journal, performance and system health.

**It is read-only.** No screen, API endpoint or message can place, change or cancel an order,
or change AccountGuard, NewsGuard, RiskManager, strategy or execution settings. All positions
and trades shown are **paper / simulated**.

## 1. Quick start (beginner)

You need Python 3.12+ (Windows: python.org installer, tick "Add to PATH"). The frontend is
already built (`frontend/dist`), so Node.js is **not** needed just to run the terminal.

```
cd futures-trading-system
pip install -r requirements.txt
python run_terminal.py
```

Your browser opens the terminal in **MOCK** mode. MOCK uses synthetic prices and needs no
NinjaTrader and no internet connection. Press Ctrl+C in the console to stop.

| Goal | Command |
| --- | --- |
| Browser, MOCK (default) | `python run_terminal.py` |
| Historical REPLAY of the repository's SYNTHETIC samples (60x speed) | `python run_terminal.py --mode replay` |
| REPLAY of your own bars, faster | `python run_terminal.py --mode replay --bars MES=path\mes.csv MNQ=path\mnq.csv --speed 300` |
| Live SHADOW (needs the Sprint 6 NinjaTrader bridge set up) | `python run_terminal.py --mode shadow` |
| Desktop window (Windows) | `pip install -r requirements-desktop.txt` then `python run_terminal.py --desktop` |
| Only print the URL | `python run_terminal.py --no-browser` |
| Show a finished backtest next to the session | `python run_terminal.py --backtest-dir output\<run folder>` |

The console prints a URL such as `http://127.0.0.1:8765/#token=...`. The token changes at every
start, and only that URL works. Exit codes: 0 ok, 1 configuration problem, 3 port in use
(already running), 4 desktop wrapper not installed.

## 2. Modes

Only modes the backend actually supports are offered. The mode is chosen at start and **never
switches automatically**: a failed live feed stays visibly failed.

| Mode | Data | Strategy and protections | Fills | Needs |
| --- | --- | --- | --- | --- |
| **MOCK** | SYNTHETIC random walk through the real Sprint 6 bridge protocol | Run in infrastructure-validation mode: entries are **never** authorized | none | nothing |
| **REPLAY** | Historical 1-minute bars (default: repository's SYNTHETIC samples); research NewsGuard with point-in-time calendar | Full: regime gate, NewsGuard, AccountGuard, RiskManager | simulated | the CSV files |
| **SHADOW** | **Live** NinjaTrader 8 level-1 data | Full; operational NewsGuard | simulated | NinjaTrader + Sprint 6 bridge + `config/marketdata.toml` + secret |
| PAPER | — | — | — | not implemented in the backend (shown as unavailable) |

The current mode is always visible:
- the mode badge in the top bar (MOCK amber, REPLAY blue, SHADOW green);
- the data badge (SYNTHETIC / HISTORICAL / LIVE);
- an amber banner for synthetic data;
- `READ-ONLY · NO ORDERS`.

While the manual news calendar is unresolved, the operational NewsGuard blocks every entry in
SHADOW. The Risk Center shows this.

**What works without NinjaTrader:** everything, in MOCK and REPLAY. **What needs NinjaTrader:**
SHADOW only (live data).

## 3. Architecture

```
                 PYTHON CORE (unchanged business logic)
   feed (mock bridge | replay | NinjaTrader bridge) -> EventValidator -> DataHealth -> journal
   -> BarBuilder -> indicators -> regime engine -> strategy -> EntryAuthorizer -> PaperBroker (simulated)
                                   |
               TerminalService (src/terminal/service.py)
     runs the paper session in a background thread; read-only views (src/terminal/views.py)
     are built while holding the session's state_lock; a publisher thread pushes topics
                                   |
               FastAPI app (src/terminal/api.py) on 127.0.0.1 only
               REST snapshots (GET) + one WebSocket (/ws)
                                   |
                     React + TypeScript frontend (frontend/)
                      /                                  \
      Windows desktop (pywebview window)        Browser (same URL)
```

- **The backend is the only source of truth.** VWAP, EMAs, P&L, drawdown, sizing, regime and
  suitability are computed by the existing Python code and only displayed by the GUI. Unknown
  values are sent as `null` and shown as UNAVAILABLE, never estimated.
- **No database access from JavaScript.** The GUI reads only the API. The SQLite journal stays
  internal.
- **Concurrency.** `PaperSession.state_lock` (re-entrant) is held by the run loop while it
  processes a batch, and by every view while it builds a snapshot. Sleeping and waiting for
  the feed happen outside the lock. Chart indicator computation runs outside the lock on
  copied inputs.
- **Desktop and browser** use the same frontend bundle and the same backend.

### Technology choices

| Component | Choice | Why |
| --- | --- | --- |
| API | FastAPI 0.128.0, Starlette 0.50.0, Uvicorn 0.38.0 | Standard, typed request validation, WebSockets built in; versions pinned together |
| Frontend | React 18.3.1, TypeScript 5.6.3, Vite 5.4.11 | Mature and well known; no UI framework: an own small design system keeps the bundle small (≈121 KB gzipped) |
| Charts | TradingView Lightweight Charts 4.2.3 | Apache-2.0 licence, purpose-built financial charts, canvas rendering. The attribution logo stays enabled, as the licence's NOTICE requires |
| Tests | Vitest 2.1.8, Testing Library, jsdom; Playwright (pre-installed) for end-to-end | |
| Desktop | pywebview 5.4 (BSD-3); uses Edge WebView2 on Windows | Far lighter than Electron; reuses the same local server |

## 4. Backend API (read-only, GET only)

Every `/api/*` route except `/api/health` requires `Authorization: Bearer <token>`.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Readiness (public, no data) |
| `GET /api/snapshot` | Initial state: meta, market, status, recent activity, notifications (one consistent read) |
| `GET /api/market` | Per-instrument quotes, health, subscription, freshness, session, entry permission by data; feed; latency |
| `GET /api/status` | Account summary, NewsGuard state, entry permission, AccountGuard summary, runtime |
| `GET /api/bars?root=MES&tf=5min&limit=500` | Bars with backend VWAP / EMA, signal / authorization / simulated-fill markers, stop / target levels |
| `GET /api/strategy?root=MES` | Strategy status, latest evaluation per direction with the decision pipeline, exact rejection codes, authorization stages |
| `GET /api/analysis?root=MES` | Regime engine output (regime, suitability, confidence or `null`, features, history, transitions, performance by regime) |
| `GET /api/positions` | Simulated positions, working simulated orders, recent closed trades |
| `GET /api/risk` | AccountGuard, RiskManager, NewsGuard, entry authorization, protection timeline |
| `GET /api/journal?...` | Closed paper trades: filters `root, direction, outcome, start, end, strategy, regime, exit_reason, q`; `sort, order, page, page_size` |
| `GET /api/journal.csv?...` | The same as CSV (spreadsheet-formula injection neutralised) |
| `GET /api/performance?source=session|backtest` | Metrics and curves of ONE source (never combined) |
| `GET /api/activity?since=N`, `GET /api/notifications?since=N` | Incremental history |
| `GET /api/system`, `GET /api/logs?level=&q=` | Diagnostics (no secrets, no file paths) |

Invalid parameters return 422, an unauthenticated request 401, a wrong Host 400, a foreign
Origin 403, any non-GET method 405, and too many requests 429.

## 5. WebSocket contract (`/ws`)

1. The client connects. The `Origin` must be the terminal's own origin, otherwise the server
   closes with 4403.
2. The client sends `{"type":"auth","token":"..."}` within 5 s, otherwise the server closes
   with 4401.
3. The server sends `{"type":"hello","seq":N,"boot_id":"..."}`. The client then fetches
   `/api/snapshot`.
4. The server streams `{"type", "seq", "boot_id", "ts", "data"}`:

| type | rate | data |
| --- | --- | --- |
| `market` | at most 4 Hz, only when changed | same as `/api/market` |
| `status` | 1 Hz | same as `/api/status` |
| `activity` | when the session's activity log advances | new items only |
| `notification` | when raised (deduplicated per key for 60 s) | one notification |
| `heartbeat` | every 5 s | backend state |
| `resync` | when the client's bounded queue (256) overflows | — |

**Client rules** (`frontend/src/api/live.ts`):
- duplicates (`seq` ≤ last) are ignored;
- a gap, a new `boot_id` or `resync` makes the client fetch a fresh snapshot;
- 15 s without any message (watchdog) means reconnect;
- reconnects back off over 0.5 / 1 / 2 / 4 / 8 / 15 s;
- the only message the server accepts after authentication is `{"type":"ping"}`.

A connected WebSocket says nothing about market data. Feed health is shown separately, and a
degraded feed raises a persistent banner.

## 6. Frontend structure

```
frontend/
  src/main.tsx            token from URL fragment -> sessionStorage; app mount
  src/App.tsx             shell: sidebar (collapsible, warning dots), top bar, banners, status bar, notifications
  src/api/client.ts       GET-only fetch wrapper (Bearer token), CSV download
  src/api/live.ts         WebSocket client (auth, watchdog, back-off, seq / boot-id resync)
  src/state/store.tsx     reducer + provider (snapshot, live merges, dedup, bounded lists)
  src/state/prefs.ts      UI preferences (localStorage, validated, reset to defaults)
  src/components/ui.tsx   design system: Panel, Stat, Badge, Dot, Kv, Table, Seg, Modal, Tip, HBar, useApi
  src/components/Charts.tsx  candlestick chart + series charts (Lightweight Charts)
  src/pages/*.tsx         Dashboard, Charts, Analysis, Strategy, Positions, Risk, Journal, Performance,
                          MarketData, System, Settings
  src/styles/theme.css    tokens (dark default, light option), layout, components
  e2e/terminal.e2e.cjs    Playwright end-to-end checks
```

## 7. Development

```
cd frontend
npm ci                 # exact versions from package-lock.json
npm test               # Vitest unit + component tests
npm run build          # type-check + production build into frontend/dist (served by the backend)
npm run dev            # Vite dev server on 127.0.0.1:5173; start the backend with: python run_terminal.py --dev --no-browser
```

Python tests:
- `python -m pytest tests/test_terminal.py tests/test_terminal_e2e.py -q`
- `test_terminal_e2e.py` drives Chromium through Playwright and is skipped if Node.js,
  Playwright or the built frontend are missing.

## 8. Security

| Threat | Control |
| --- | --- |
| Network exposure | Uvicorn binds 127.0.0.1 only (hard-coded); there is no LAN option in Sprint 7 |
| DNS rebinding | The Host header must be exactly `127.0.0.1:<port>` or `localhost:<port>` |
| Other websites / CSRF | Foreign `Origin` → 403; no CORS headers (except `--dev` for the Vite origin); GET only, no state-changing endpoint exists |
| Unauthorized access | Random per-start token (256 bits), constant-time comparison. It travels in the URL fragment (not sent to the server, not logged) and lives in sessionStorage |
| WebSocket hijacking | Origin check plus first-message authentication; messages are bounded (4 KB); no command channel |
| Injection | Strict typed query parameters (enumerations, patterns, ranges); output is JSON; React escapes text; CSV formula neutralisation |
| Clickjacking / content | CSP `default-src 'self'`, `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `no-store` for API responses |
| Information leaks | Generic 500 messages; API docs disabled (only with `--dev`); no secrets, tokens or filesystem paths in any view (tested) |
| Resource exhaustion | Rate limits (30 req/s per client; heavy endpoints 4/s); bounded queues, histories, notifications and caches |
| Static files | Fixed asset directory, no path traversal (tested) |
| Execution | No order code path: GET-only routes (tested), the service has no order methods (tested), `PaperBroker.TRANSMITS_REAL_ORDERS = False` |

**Assumptions:**
- Processes running as the same Windows user are trusted: they can read the console's token.
- Any future control function (LAN access, settings, actions) needs its own design and
  security review.

## 9. Windows desktop status

The desktop launcher (`--desktop`) does the following:
- starts the server in a background thread and waits for `/api/health`;
- opens a pywebview window on the same URL;
- stops the server when the window closes, so no orphaned backend is left.

**It has not been run on Windows in this repository** (the build environment is Linux). To
validate on Windows:
1. `pip install -r requirements-desktop.txt`.
2. `python run_terminal.py --desktop`: a window opens in MOCK mode.
3. Close the window. Task Manager should show no remaining `python.exe` for the terminal.
4. Run the same command twice: the second instance must exit with "already running" (code 3).
5. `python run_terminal.py` (browser) must still work while the desktop version is closed.

## 10. Troubleshooting

| Symptom | Fix |
| --- | --- |
| "frontend is not built" | `cd frontend && npm ci && npm run build` (Node.js 18+) |
| "is already running" | Another terminal uses the port: close it, or use `--port 8766` |
| Page says unauthorized | Use the full URL printed in the console (the token changes at every start) |
| BACKEND DISCONNECTED banner | The Python process stopped; restart `run_terminal.py` |
| MARKET FEED DEGRADED banner | See Market Data for the reason (stale, not subscribed, provider disconnected, delayed) |
| SHADOW: SESSION FAILED | Read the error: usually the bridge secret or config. See docs/sprint6_live_market_data.md |
| Desktop: missing pywebview | `pip install -r requirements-desktop.txt`, or use the browser |

## 11. Known limitations

- Windows desktop packaging is untested on Windows. NinjaTrader live operation is untested
  (no real installation or licensed feed).
- PAPER mode (a broker simulation account) does not exist; SHADOW is the live-data paper mode.
- MOCK prices and the repository's sample data are synthetic. Replay is not live, and shadow
  results are simulations.
- The Strategy Monitor shows the strategy's own diagnostics and authorization stages. It cannot
  show values the strategy did not record.
- The backtest view reads a finished backtest folder and is never combined with session results.
- Performance on very long sessions: lists are bounded, but chart history is limited to 3,000
  bars per request.
- The news calendar limitation (Sprint 4/5) still blocks entries in SHADOW mode.

## 12. Sprint 8 (shadow trading) integration

- SHADOW mode already runs the full chain on live data. The terminal shows signals, rejection
  reasons, simulated fills and protections as they happen.
- Sprint 8 can add shadow-specific analytics: live versus replay comparison and fill-quality
  statistics. These would be new read-only views in `src/terminal/views.py` and new pages.
- Any action from the GUI (for example an operator emergency stop) needs a separate,
  authenticated, audited control channel with its own security review. It is deliberately not
  present.

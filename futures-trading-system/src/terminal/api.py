"""Local, READ-ONLY application API for the terminal GUI (FastAPI).

Security model (see docs/sprint7_trading_terminal.md):
* binds to 127.0.0.1 only (the launcher refuses anything else);
* Host header must be the loopback host:port (DNS-rebinding protection);
* every /api route except /api/health needs ``Authorization: Bearer <session token>``; the token is
  random per start and reaches the GUI in the URL *fragment* (never sent to the server, never logged);
* WebSocket: Origin must be the terminal's own origin, then a first ``{"type":"auth","token":...}``
  message within 5 s; afterwards the server ignores everything but pings (no command channel);
* GET only; no endpoint changes trading, risk, NewsGuard, strategy or execution state;
* strict query validation (422 on anything unexpected), per-client rate limits, security headers,
  no API docs unless --dev, generic error messages.
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.terminal import APP_NAME, APP_VERSION
from src.terminal.service import TerminalService

log = logging.getLogger("fts.terminal.api")
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
WS_QUEUE = 256
Root = Literal["MES", "MNQ"]
SessionType = Literal["MOCK", "REPLAY", "SHADOW", "MONITOR", "UNKNOWN"]


class RateLimiter:
    """Token bucket per (client, bucket)."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, burst
        self._b: dict[tuple[str, str], tuple[float, float]] = {}

    def allow(self, client: str, bucket: str) -> bool:
        now = time.monotonic()
        tokens, last = self._b.get((client, bucket), (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._b[(client, bucket)] = (tokens, now)
            return False
        self._b[(client, bucket)] = (tokens - 1, now)
        if len(self._b) > 1000:
            self._b.clear()
        return True


class Hub:
    """Fan-out of service messages to WebSocket clients through BOUNDED queues."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:  # called from the publisher thread
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.full():  # slow client: drop its backlog and tell it to re-snapshot (never unbounded)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "resync", "seq": msg["seq"], "boot_id": msg["boot_id"], "data": None})
            else:
                q.put_nowait(msg)


def create_app(service: TerminalService, *, token: str, port: int, dev: bool = False,
               dev_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173"),
               manage_service: bool = True, frontend_dist: Path = FRONTEND_DIST) -> FastAPI:
    hub = Hub()
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}  # exact loopback host:port only (DNS rebinding)
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", *(dev_origins if dev else ())}
    limiter = RateLimiter(rate=30.0, burst=60)
    heavy = RateLimiter(rate=4.0, burst=8)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        service.subscribe(hub.publish)
        if manage_service:
            await asyncio.to_thread(service.start)
        yield
        if manage_service:
            await asyncio.to_thread(service.stop)

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan,
                  docs_url="/api/docs" if dev else None, redoc_url=None, openapi_url="/api/openapi.json" if dev else None)
    app.state.hub = hub

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.headers.get("host", "") not in hosts:
                return JSONResponse({"detail": "invalid host"}, status_code=400)
            if request.method not in ("GET", "HEAD") and not (dev and request.method == "OPTIONS"):
                return JSONResponse({"detail": "read-only API"}, status_code=405)
            origin = request.headers.get("origin")
            if origin is not None and origin not in origins:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            client = request.client.host if request.client else "?"
            if request.url.path.startswith("/api/") and not limiter.allow(client, "api"):
                return JSONResponse({"detail": "rate limited"}, status_code=429)
            try:
                resp = await call_next(request)
            except Exception:  # noqa: BLE001 -- generic message; details only in the local log
                log.exception("request failed: %s", request.url.path)
                return JSONResponse({"detail": "internal error"}, status_code=500)
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Referrer-Policy"] = "no-referrer"
            resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'")
            if dev and origin in origins:
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Access-Control-Allow-Headers"] = "Authorization"
                resp.headers["Vary"] = "Origin"
            return resp

    app.add_middleware(Guard)

    def auth(request: Request) -> None:
        h = request.headers.get("authorization", "")
        if not h.startswith("Bearer ") or not hmac.compare_digest(h[7:], token):
            raise HTTPException(status_code=401, detail="unauthorized")

    def heavy_limit(request: Request) -> None:
        if not heavy.allow(request.client.host if request.client else "?", request.url.path):
            raise HTTPException(status_code=429, detail="rate limited")

    def found(v: Any) -> Any:
        if v is None:
            raise HTTPException(status_code=404, detail="not available")
        return v

    A = [Depends(auth)]

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "app": APP_NAME, "version": APP_VERSION, "state": service.state}

    @app.get("/api/snapshot", dependencies=A)
    def snapshot() -> dict:
        return service.snapshot()

    @app.get("/api/market", dependencies=A)
    def market() -> Any:
        return found(service.market())

    @app.get("/api/status", dependencies=A)
    def status() -> Any:
        return found(service.status())

    @app.get("/api/bars", dependencies=A + [Depends(heavy_limit)])
    def bars(root: Root, tf: Literal["1min", "5min", "15min"] = "5min", limit: int = Query(500, ge=10, le=3000)) -> Any:
        return found(service.bars(root, tf, limit))

    @app.get("/api/strategy", dependencies=A)
    def strategy(root: Root) -> Any:
        return found(service.strategy(root))

    @app.get("/api/analysis", dependencies=A)
    def analysis(root: Root) -> Any:
        return found(service.analysis(root))

    @app.get("/api/positions", dependencies=A)
    def positions() -> Any:
        return found(service.positions())

    @app.get("/api/risk", dependencies=A)
    def risk() -> Any:
        return found(service.risk())

    def journal_filters(root: Root | None = None, direction: Literal["long", "short"] | None = None,
                        outcome: Literal["win", "loss"] | None = None,
                        start: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                        end: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                        strategy: str | None = Query(None, max_length=60, pattern=r"^[\w\-]+$"),
                        regime: str | None = Query(None, max_length=60, pattern=r"^[\w\-]+$"),
                        exit_reason: str | None = Query(None, max_length=60, pattern=r"^[\w\-:]+$"),
                        q: str | None = Query(None, max_length=60),
                        sort: Literal["trade_id", "root", "direction", "entry_time", "exit_time", "net_pnl", "gross_pnl",
                                      "r_multiple", "quantity", "exit_reason", "regime"] = "exit_time",
                        order: Literal["asc", "desc"] = "desc") -> dict:
        return {"root": root, "direction": direction, "outcome": outcome, "start": start, "end": end,
                "strategy": strategy, "regime": regime, "exit_reason": exit_reason, "q": q, "sort": sort, "order": order}

    @app.get("/api/journal", dependencies=A)
    def journal(f: dict = Depends(journal_filters), page: int = Query(1, ge=1, le=100000),
                page_size: int = Query(50, ge=5, le=500)) -> Any:
        return found(service.journal(page, page_size, **f))

    @app.get("/api/journal.csv", dependencies=A + [Depends(heavy_limit)])
    def journal_csv(f: dict = Depends(journal_filters)) -> Response:
        rows = service.journal_rows(**f)
        buf = io.StringIO()
        cols = ["trade_id", "root", "direction", "entry_time", "exit_time", "entry_price", "exit_price", "quantity",
                "gross_pnl", "total_costs", "net_pnl", "r_multiple", "exit_reason", "strategy", "regime"]
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            # neutralise spreadsheet formulas in text cells (CSV injection)
            w.writerow([("'" + str(r.get(c)) if isinstance(r.get(c), str) and str(r.get(c))[:1] in "=+-@" else r.get(c))
                        for c in cols])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="paper_trade_journal.csv"'})

    # ------------------------------------------------------------------ Shadow Analytics (Sprint 8, read-only)
    def shadow_filters(root: Root | None = None,
                       start: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                       end: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                       session_type: SessionType | None = None,
                       strategy_version: str | None = Query(None, max_length=80, pattern=r"^[\w.:@\-]+$"),
                       regime: str | None = Query(None, max_length=60, pattern=r"^[\w\-]+$"),
                       provider: str | None = Query(None, max_length=60, pattern=r"^[\w.:@\- ]+$"),
                       session_id: str | None = Query(None, max_length=80, pattern=r"^[\w.:@\-]+$")) -> dict:
        return {"root": root, "start": start, "end": end, "session_type": session_type,
                "strategy_version": strategy_version, "regime": regime, "provider": provider, "session_id": session_id}

    def shadow_call(fn):
        try:
            out = fn()
        except ValueError as exc:  # filter validation inside the library
            raise HTTPException(status_code=422, detail=str(exc)[:200]) from exc
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:300]) from exc
        except Exception as exc:  # noqa: BLE001 -- generic message; details only in the local log
            log.exception("shadow analytics failed")
            raise HTTPException(status_code=500, detail="analytics failed (see the terminal log)") from exc
        if isinstance(out, dict) and out.get("unavailable"):
            raise HTTPException(status_code=503, detail=out["unavailable"])
        return out

    @app.get("/api/shadow/compare", dependencies=A + [Depends(heavy_limit)])
    def shadow_compare(root: Root, a: SessionType = "REPLAY", b: SessionType = "SHADOW",
                       f: dict = Depends(shadow_filters)) -> Any:
        if a == b:
            raise HTTPException(status_code=422, detail="compare needs two different session types")
        return shadow_call(lambda: service.shadow("compare", f, root=root, a=a, b=b))

    @app.get("/api/shadow/export/{kind}.{fmt}", dependencies=A + [Depends(heavy_limit)])
    def shadow_export(kind: Literal["sessions", "trades", "execution", "reliability", "regimes", "account", "blocked",
                                    "costs"], fmt: Literal["csv", "json"], f: dict = Depends(shadow_filters)) -> Response:
        body, name = shadow_call(lambda: service.shadow_export(kind, fmt, f))
        return Response(body, media_type="text/csv" if fmt == "csv" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/shadow/{report}", dependencies=A + [Depends(heavy_limit)])
    def shadow(report: Literal["sessions", "performance", "costs", "execution", "regimes", "blocked", "reliability",
                               "account", "validation", "journals"], f: dict = Depends(shadow_filters)) -> Any:
        return shadow_call(lambda: service.shadow(report, f))

    # ------------------------------------------------------------------ Prop Firm Center (Sprint 9, read-only)
    def profile_key():
        return Query(..., max_length=130, pattern=r"^[a-z0-9][a-z0-9_\-]{1,79}@[0-9A-Za-z][0-9A-Za-z.\-]{0,39}$")

    def prop_call(fn):
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001 -- generic message; details only in the local log
            log.exception("prop firm view failed")
            raise HTTPException(status_code=500, detail="prop firm view failed (see the terminal log)") from exc
        if out is None:
            raise HTTPException(status_code=404, detail="not available")
        from src.analytics.shadow.library import _jsonable

        return _jsonable(out)

    @app.get("/api/propfirm/status", dependencies=A)
    def propfirm_status() -> Any:
        return prop_call(service.propfirm_status)

    @app.get("/api/propfirm/profiles", dependencies=A)
    def propfirm_profiles() -> Any:
        return prop_call(service.propfirm_profiles)

    @app.get("/api/propfirm/profile", dependencies=A)
    def propfirm_profile(key: str = profile_key()) -> Any:
        return prop_call(lambda: service.propfirm_profile(key))

    @app.get("/api/propfirm/compare", dependencies=A)
    def propfirm_compare(a: str = profile_key(), b: str = profile_key()) -> Any:
        return prop_call(lambda: service.propfirm_compare(a, b))

    @app.get("/api/propfirm/history", dependencies=A + [Depends(heavy_limit)])
    def propfirm_history() -> Any:
        return prop_call(service.propfirm_history)

    @app.get("/api/propfirm/export/{kind}.{fmt}", dependencies=A + [Depends(heavy_limit)])
    def propfirm_export(kind: Literal["evaluations", "drawdown", "breaches", "flattens", "account", "rules"],
                        fmt: Literal["csv", "json"]) -> Response:
        from src.analytics.shadow.library import to_csv, to_json
        from src.risk.prop_firms.reports import rows_for_export

        st = prop_call(service.propfirm_status)
        prof = (st.get("summary") or {}) if st.get("selected") else {}
        meta = {"report": f"prop firm {kind}", "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "timezone": "UTC", "profile": prof.get("profile"), "ruleset_sha256": prof.get("ruleset_sha256"),
                "profile_status": (prof.get("operational") or {}).get("status"),
                "label": "SIMULATED account; rule evaluation is not a firm decision"}
        if kind == "rules":
            if not prof:
                raise HTTPException(status_code=404, detail="no profile selected")
            p = prop_call(lambda: service.propfirm_profile(prof["profile"]))
            rows = [{k: v for k, v in r.items() if k != "params"} | {"params": r.get("params")} for r in p["rules"]]
        else:
            rows = rows_for_export(prop_call(service.propfirm_history), kind)
        body = to_csv(rows, meta) if fmt == "csv" else to_json({"rows": rows}, meta)
        return Response(body, media_type="text/csv" if fmt == "csv" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="prop_firm_{kind}.{fmt}"'})

    @app.get("/api/performance", dependencies=A)
    def performance(source: Literal["session", "backtest"] = "session") -> Any:
        return found(service.performance(source))

    @app.get("/api/activity", dependencies=A)
    def activity(since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> Any:
        return service.activity(since, limit)

    @app.get("/api/notifications", dependencies=A)
    def notifications(since: int = Query(0, ge=0)) -> Any:
        return [vars(n) for n in service.notifications.items if n.id > since]

    @app.get("/api/system", dependencies=A)
    def system() -> Any:
        return service.system()

    @app.get("/api/logs", dependencies=A)
    def logs(level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
             q: str | None = Query(None, max_length=60), limit: int = Query(300, ge=1, le=2000)) -> Any:
        return service.logs_view(level, q, limit)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if websocket.headers.get("origin") not in origins or websocket.headers.get("host", "") not in hosts:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            msg = json.loads(first) if len(first) < 512 else {}
            if not (isinstance(msg, dict) and msg.get("type") == "auth" and isinstance(msg.get("token"), str)
                    and hmac.compare_digest(msg["token"], token)):
                await websocket.close(code=4401)
                return
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            try:
                await websocket.close(code=4401)
            except RuntimeError:
                pass
            return
        q: asyncio.Queue = asyncio.Queue(maxsize=WS_QUEUE)
        hub.clients.add(q)
        service.ws_clients = len(hub.clients)
        await websocket.send_json({"type": "hello", "seq": service.seq, "boot_id": service.boot_id,
                                   "data": {"version": APP_VERSION}})

        async def reader() -> None:  # only pings are understood; anything else is ignored
            while True:
                text = await websocket.receive_text()
                if len(text) <= 256 and text.strip() == '{"type":"ping"}':
                    await q.put({"type": "pong", "seq": service.seq, "boot_id": service.boot_id, "data": None})

        rtask = asyncio.create_task(reader())
        try:
            while True:
                get = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({get, rtask}, return_when=asyncio.FIRST_COMPLETED)
                if rtask in done:
                    get.cancel()
                    break
                await websocket.send_json(get.result())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rtask.cancel()
            hub.clients.discard(q)
            service.ws_clients = len(hub.clients)

    # ------------------------------------------------------------------ static frontend (no secrets inside)
    @app.get("/", include_in_schema=False)
    def index() -> Response:
        p = frontend_dist / "index.html"
        if not p.is_file():
            return JSONResponse({"detail": "frontend not built: run 'npm ci && npm run build' in frontend/"},
                                status_code=503)
        return FileResponse(p)

    @app.get("/assets/{name}", include_in_schema=False)
    def assets(name: str) -> Response:
        if "/" in name or "\\" in name or name.startswith(".") or len(name) > 120:
            raise HTTPException(status_code=404)
        p = (frontend_dist / "assets" / name).resolve()
        if p.parent != (frontend_dist / "assets").resolve() or not p.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(p)

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> Response:
        p = frontend_dist / "favicon.svg"
        if not p.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(p)

    return app

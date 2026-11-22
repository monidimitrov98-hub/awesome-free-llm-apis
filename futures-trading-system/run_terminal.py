"""FTS Terminal (Sprint 7) -- READ-ONLY professional trading terminal. No order can be sent.

    python run_terminal.py                          MOCK mode (synthetic data, no NinjaTrader), opens the browser
    python run_terminal.py --mode replay --speed 120   replay of the repository's SYNTHETIC sample bars
    python run_terminal.py --mode shadow            live NinjaTrader 8 data (needs the Sprint 6 bridge setup)
    python run_terminal.py --desktop                same application in a desktop window (pywebview)
    python run_terminal.py --no-browser             only print the URL

The server binds to 127.0.0.1 only. A random session token is generated at every start and passed to
the GUI in the URL fragment; it is never written to disk or logs (only printed to this console).
Exit codes: 0 ok, 1 configuration error, 3 already running / port busy, 4 desktop wrapper unavailable.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from src.config import ConfigError
from src.terminal import APP_NAME, APP_VERSION
from src.terminal.modes import MODES, ModeOptions, prerequisites


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION} (read-only).")
    p.add_argument("--mode", choices=MODES, default="mock", help="mock (default, synthetic) | replay | shadow (live)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--speed", type=float, default=60.0, help="replay: market seconds per wall second")
    p.add_argument("--bars", nargs="*", default=[], metavar="ROOT=CSV", help="replay data / shadow warm-up history")
    p.add_argument("--replay-start", default=None)
    p.add_argument("--news-calendar", type=Path, default=None, help="replay: historical calendar")
    p.add_argument("--backtest-dir", type=Path, default=None, help="show a finished backtest (output/<run>) separately")
    p.add_argument("--desktop", action="store_true", help="open in a desktop window (pywebview)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--dev", action="store_true", help="development: API docs + Vite dev-server origin allowed")
    p.add_argument("--keep-state", action="store_true",
                   help="mock/replay: keep the run's journal in data/analytics/journals/ for Shadow Analytics "
                        "(by default these runs are ephemeral)")
    return p.parse_args(argv)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:  # noqa: S310 (loopback)
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.1)
    return False


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not 1024 <= a.port <= 65535:
        print("Error: --port must be between 1024 and 65535", file=sys.stderr)
        return 1
    opts = ModeOptions(mode=a.mode, bars={k.upper(): Path(v) for k, v in (x.split("=", 1) for x in a.bars)},
                       replay_start=a.replay_start, speed=a.speed, news_calendar=a.news_calendar)
    if a.keep_state and a.mode in ("mock", "replay"):
        import pandas as pd

        from src.config import PROJECT_ROOT

        opts.state_dir = PROJECT_ROOT / "data" / "analytics" / "journals" / \
            f"{a.mode}-{pd.Timestamp.now(tz='UTC').strftime('%Y%m%dT%H%M%S')}"
    problems = prerequisites(opts)
    if problems:  # never fall back to another mode
        print(f"Error: {a.mode.upper()} mode cannot start: " + "; ".join(problems), file=sys.stderr)
        return 1
    if not port_free(a.port):
        print(f"Error: 127.0.0.1:{a.port} is in use (is {APP_NAME} already running?). Use --port.", file=sys.stderr)
        return 3
    try:
        import uvicorn

        from src.terminal.api import FRONTEND_DIST, create_app
        from src.terminal.service import TerminalService
    except ImportError as exc:
        print(f"Error: missing dependency ({exc.name}). Run: pip install -r requirements.txt", file=sys.stderr)
        return 1
    if not (FRONTEND_DIST / "index.html").is_file():
        print("Error: the frontend is not built. Run: cd frontend && npm ci && npm run build", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(32)
    service = TerminalService(opts, backtest_dir=a.backtest_dir)
    app = create_app(service, token=token, port=a.port, dev=a.dev)
    config = uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning", access_log=False,
                            ws_max_size=4096, timeout_graceful_shutdown=5)
    server = uvicorn.Server(config)
    url = f"http://127.0.0.1:{a.port}/#token={token}"
    print(f"*** {APP_NAME} {APP_VERSION} -- READ-ONLY, no order can be sent ({a.mode.upper()} mode) ***")

    if a.desktop:
        try:
            import webview  # pywebview
        except ImportError:
            print("Error: the desktop window needs pywebview: pip install -r requirements-desktop.txt "
                  "(or run without --desktop and use the browser)", file=sys.stderr)
            return 4
        t = threading.Thread(target=server.run, name="terminal-server", daemon=True)
        t.start()
        if not wait_ready(a.port):
            server.should_exit = True
            t.join(10)
            print("Error: the backend did not become ready", file=sys.stderr)
            return 1
        webview.create_window(f"{APP_NAME} {APP_VERSION} -- {a.mode.upper()}", url, width=1600, height=960,
                              min_size=(1100, 700))
        webview.start()  # blocks until the window is closed
        server.should_exit = True  # closing the window stops the backend: no orphaned process
        t.join(15)
        return 0

    print(f"Open: {url}\n(Ctrl+C stops the terminal and its paper session.)")
    if not a.no_browser:
        threading.Thread(target=lambda: wait_ready(a.port) and webbrowser.open(url), daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

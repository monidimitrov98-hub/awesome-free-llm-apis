"""Sprint 7 end-to-end GUI tests: (mock bridge | synthetic replay) -> market-data backend -> event processing
-> bars -> indicators -> strategy -> read-only API -> WebSocket -> React UI in Chromium (Playwright).
Skipped when Node.js / Playwright / the built frontend are not available."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "frontend" / "e2e" / "terminal.e2e.cjs"


def _playwright_available() -> bool:
    node = shutil.which("node")
    if not node or not (ROOT / "frontend" / "dist" / "index.html").is_file():
        return False
    r = subprocess.run([node, "-e", "require('playwright')"], capture_output=True, cwd="/")
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="node + playwright + built frontend required")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("scenario,speed", [("mock", None), ("mock-fail", None), ("replay", "1200"),
                                            ("replay-nogate", "1500"), ("shadow-analytics", "200000")])
def test_gui_end_to_end(scenario, speed):
    port, token = _port(), secrets.token_urlsafe(24)
    args = [sys.executable, str(ROOT / "tests" / "e2e" / "e2e_server.py"), str(port), token, scenario] + ([speed] if speed else [])
    server = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        end = time.monotonic() + 60
        while time.monotonic() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        r = subprocess.run(["node", str(SCRIPT), str(port), token, scenario], capture_output=True, text=True, timeout=300,
                           cwd="/", env={**os.environ, "NODE_PATH": os.environ.get("NODE_PATH", "")})
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"E2E {scenario}: PASS" in r.stdout
    finally:
        server.terminate()
        try:
            server.wait(15)  # clean shutdown: no orphaned backend process
        except subprocess.TimeoutExpired:
            server.kill()
            pytest.fail("backend did not stop on SIGTERM")

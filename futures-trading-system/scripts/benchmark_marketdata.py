"""Sprint 6 performance measurement with the MOCK bridge over loopback (no NinjaTrader).

    python scripts/benchmark_marketdata.py

The mock bridge runs in a SEPARATE PROCESS (as NinjaTrader would), sending authenticated, MAC'd
level-1 messages over 127.0.0.1. This process runs the backend: authentication/MAC/schema/
timestamp checks, the bounded queue, and the paper runtime (validation, data health, journal,
bar builder, strategy at bar boundaries) in infrastructure_validation mode.

Scenarios:
  flood      the producer sends as fast as it can; the runtime consumes -> sustained throughput
  paced:R    the producer sends R events/s -> end-to-end latency (bridge send -> processed)
Numbers depend on the machine; they are measurements, not guarantees.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import platform
import resource
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.marketdata.mock_bridge import MockBridge  # noqa: E402
from tests.marketdata_helpers import MES, MNQ, Clock, live_session, make_feed  # noqa: E402


def _producer(port: int, secret: bytes, n: int, rate: float | None) -> None:
    b = MockBridge("127.0.0.1", port, secret)
    b.connect()
    b.status("Connected")
    b.subscribe(MES)
    b.subscribe(MNQ)
    t0 = time.perf_counter()
    for i in range(n):
        now = datetime.now(timezone.utc)
        if i % 2:
            b.quote(MES, now, 5999.75, 6000.0)
        else:
            b.trade(MNQ, now, 21000.0, 1.0)
        if rate:
            delay = t0 + (i + 1) / rate - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
    time.sleep(0.5)
    b.close()


def pct(vals, q):
    vals = sorted(vals)
    return round(vals[min(len(vals) - 1, int(q * (len(vals) - 1)))], 3) if vals else None


def scenario(n: int, rate: float | None) -> dict:
    tmp = Path(tempfile.mkdtemp())
    f, secret, clock = make_feed(queue_max_events=max(n + 10, 20_000), clock=Clock(pd.Timestamp.now(tz="UTC")))
    s = live_session(tmp, f, clock, mode="infrastructure_validation")
    proc = mp.get_context("fork").Process(target=_producer, args=(f.server.port, secret, n, rate))
    proc.start()
    f.connect()
    s.health.on_connect(clock())
    e2e, done = [], 0
    cpu0, t_first, t_last = time.process_time(), None, None
    deadline = time.monotonic() + 120
    while done < n and time.monotonic() < deadline:
        s._apply_feed_updates()
        batch = f.poll(1000)
        if not batch:
            time.sleep(0.0005)
            continue
        for ev in batch:
            s.process_event(ev)
            e2e.append((s._wall() - ev.bridge_ts).total_seconds() * 1000)
        s._commit()
        done += len(batch)
        t_first = t_first or time.perf_counter()
        t_last = time.perf_counter()
    cpu = time.process_time() - cpu0
    proc.join(10)
    st = s.status()
    f.disconnect()
    wall = (t_last - t_first) if t_first and t_last else float("nan")
    return {"scenario": "flood" if rate is None else f"paced {int(rate)}/s", "events": done,
            "events_per_second": round(done / wall) if wall == wall and wall > 0 else None,
            "backend_cpu_seconds": round(cpu, 2),
            "end_to_end_ms_p50": pct(e2e, 0.5), "end_to_end_ms_p99": pct(e2e, 0.99), "end_to_end_ms_max": pct(e2e, 1.0),
            "processing_ms_p50": st["latency"]["processing_ms_p50"], "processing_ms_p99": st["latency"]["processing_ms_p99"],
            "queue_overflows": st["feed"]["queue_overflows"], "rejected": st["validation"]["rejected"],
            "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}


def main() -> None:
    print({"python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count(),
           "pandas": pd.__version__})
    print(scenario(30_000, None))
    for rate in (200, 1000, 3000):
        print(scenario(int(rate * 8), rate))


if __name__ == "__main__":
    main()

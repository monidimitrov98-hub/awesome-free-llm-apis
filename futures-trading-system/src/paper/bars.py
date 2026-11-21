"""Completed 1-minute bars from validated trades, and strategy frames from those bars.

* :class:`BarBuilder` -- a minute's bar is COMPLETE only when an event (or clock tick) at or
  after the minute's end has been seen. Minutes without trades produce no bar (a gap), so the
  resampled 5/15-minute bars containing them are ``is_complete = False`` and are never traded.
* :func:`build_frames` -- the backtest's own functions (``resample_ohlcv``, ``add_indicators``,
  the runner's regime feature helper) applied to all completed bars known so far. Indicators
  are causal, so values for a bar equal the backtest's values for the same history. The 1-minute
  frame only carries the columns the regime shock feature needs (ATR), for speed.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pandas as pd

from src.config import AppConfig
from src.data.resample import resample_ohlcv
from src.data.sessions import assign_session_dates
from src.indicators.atr import atr
from src.indicators.compute import add_indicators
from src.paper.events import MarketEvent
from src.strategy.vwap_pullback_config import VwapPullbackConfig

MINUTE = pd.Timedelta(minutes=1)
COLS = ["open", "high", "low", "close", "volume"]


@dataclass
class _Building:
    start: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarBuilder:
    def __init__(self, root: str, history: pd.DataFrame | None = None) -> None:
        self.root = root
        self._rows: list[tuple] = []
        self._index: list[pd.Timestamp] = []
        self._frame_cache: pd.DataFrame | None = None
        if history is not None and len(history):
            h = history[COLS].astype(float)
            self._index = list(h.index)
            self._rows = [tuple(r) for r in h.itertuples(index=False)]
        self._cur: _Building | None = None

    @property
    def last_complete(self) -> pd.Timestamp | None:
        return self._index[-1] if self._index else None

    def _finalize(self) -> list[pd.Timestamp]:
        b = self._cur
        if b is None:
            return []
        if self._index and b.start <= self._index[-1]:
            self._cur = None
            return []  # never rewrite a completed minute
        self._index.append(b.start)
        self._rows.append((b.open, b.high, b.low, b.close, b.volume))
        self._frame_cache = None
        self._cur = None
        return [b.start]

    def advance(self, now: pd.Timestamp) -> list[pd.Timestamp]:
        """Complete the building minute if ``now`` is at/after its end. Returns completed minutes."""
        if self._cur is not None and now >= self._cur.start + MINUTE:
            return self._finalize()
        return []

    def on_trade(self, ev: MarketEvent) -> list[pd.Timestamp]:
        done = self.advance(ev.ts)
        minute = ev.ts.floor("min")
        if self.last_complete is not None and minute <= self.last_complete:
            return done  # trade for an already completed minute (late): not applied
        if self._cur is None:
            self._cur = _Building(minute, ev.last, ev.last, ev.last, ev.last, ev.size)
        else:
            c = self._cur
            c.high, c.low, c.close, c.volume = max(c.high, ev.last), min(c.low, ev.last), ev.last, c.volume + ev.size
        return done

    def frame(self) -> pd.DataFrame:
        if self._frame_cache is None:
            idx = pd.DatetimeIndex(self._index, name="timestamp")
            if idx.tz is None and len(idx):
                idx = idx.tz_localize("UTC")
            self._frame_cache = pd.DataFrame(self._rows, index=idx, columns=COLS, dtype="float64")
        return self._frame_cache

    def tail(self, n: int) -> pd.DataFrame:
        return self.frame().iloc[-n:]


def build_frames(base: pd.DataFrame, app: AppConfig, cfg: VwapPullbackConfig, regime=None
                 ) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """(signal frame, context frames) exactly as the backtest runner builds them."""
    freq = app.data.base_frequency
    frames: dict[str, pd.DataFrame] = {}
    for tf in {cfg.signal_timeframe, cfg.context_timeframe} - {"", None}:
        frames[tf] = add_indicators(resample_ohlcv(base, tf, freq), app.indicators, app.session)
    context = {cfg.context_timeframe: frames[cfg.context_timeframe]} if cfg.context_timeframe else {}
    signal = frames[cfg.signal_timeframe]
    if regime is not None and regime.enabled:
        from src.backtest.runner import _regime_frames

        if regime.fast_timeframe:
            fast = resample_ohlcv(base, regime.fast_timeframe, freq)
            fast["session_date"] = assign_session_dates(fast.index, app.session)
            fast[cfg.atr_col] = atr(fast["high"], fast["low"], fast["close"], app.indicators.atr_period)
            frames[regime.fast_timeframe] = fast
        signal, rctx = _regime_frames(SimpleNamespace(frames=frames), app, cfg, regime)
        context = {**context, **rctx}
    return signal, context


__all__ = ["BarBuilder", "build_frames", "Any"]

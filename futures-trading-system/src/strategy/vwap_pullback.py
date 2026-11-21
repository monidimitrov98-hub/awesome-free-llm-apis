"""VWAP Trend Pullback strategy (research only; no claim of profitability).

Decision flow on every completed signal bar (default 5m), for each enabled
direction. LONG shown; SHORT is the mirror image.

1. Gates: flat with no pending entry; bar complete; inside the strategy's
   session windows; all indicator values present on the bars used; those bars
   contiguous, complete and in the current session.
2. Trend (signal timeframe): close > VWAP, EMA fast > EMA slow,
   (EMA fast - EMA slow) / ATR >= minimum strength, confirmed swing structure
   bullish (last confirmed swing high > previous one AND last swing low > previous one).
3. Context (higher timeframe, default 15m): the last COMPLETED context candle of
   the current session (supplied by the engine, see engine docs) has close > VWAP
   and EMA fast > EMA slow; it must not be stale or incomplete.
4. Pullback: within the last N signal bars some bar's low came into the zone
   [level - penetration x ATR, level + zone x ATR] of EMA fast, EMA slow or VWAP,
   and no bar of that window broke the last confirmed swing low.
5. Not extended: close within max distances (in ATR) of VWAP and EMA fast.
6. Confirmation (current bar): bullish close (close > open), optionally above the
   previous high, and back above EMA fast.
7. RSI recovery: RSI dipped to <= long_pullback_max inside the window, is now
   >= long_recovery_min and <= long_entry_max, and (optionally) rising.
8. RVOL >= minimum (NaN never passes).
9. Stop below the last confirmed swing low (minus buffer) or close - k x ATR,
   rounded with the engine's conservative tick rounding; min/max distance checks.
   Target = close + R x stop distance, rounded the same way.
10. Filters: cooldown after an exit, max filled entries per session, one entry per
    pullback.

Data integrity (not configurable): every bar used for pivots/structure, the stop and
the pullback window must be complete and exactly one signal period apart in time;
pivots whose window touches an incomplete bar or a missing bar are discarded
(``structure_data_invalid`` when that leaves no valid structure).

Pullback identity: the last confirmed swing high (long) / low (short); if none is
confirmed, the bar holding the session's highest high (lowest low) up to the most
recent touch of the zone. Neither depends on the moving lookback window, so one
pullback keeps one id across consecutive bars. A pullback is *reserved* when the
order is submitted, *released* if the engine rejects or cancels it and *consumed*
only when the entry fills.

Order lifecycle: every submitted signal carries a ``signal_id`` (``client_id``); the
engine reports accepted / rejected / filled / cancelled, which is written back into
the diagnostics row (``order_status``) and :meth:`signals_frame`.

All checks are evaluated and recorded (see :meth:`diagnostics_frame`); the first
failing check in this order is the ``rejection_reason``. A passing setup emits one
market entry with a stop-loss/take-profit bracket; the engine executes it no
earlier than its next eligible execution bar and re-validates it at the fill.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

import numpy as np
import pandas as pd

from src.backtest.execution import conservative_order_price
from src.backtest.orders import Fill, OrderEvent, OrderRequest, OrderRole, OrderType, Side
from src.config import ContractSpec
from src.indicators.swings import STRUCTURE_BEARISH, STRUCTURE_BULLISH, market_structure
from src.strategy.base import Strategy, StrategyContext
from src.strategy.vwap_pullback_config import VwapPullbackConfig


class Reason:
    """Rejection codes recorded in the diagnostics (``rejection_reason`` / ``reasons``)."""

    POSITION_OPEN = "position_open"
    ENTRY_PENDING = "entry_pending"
    INCOMPLETE_SIGNAL_BAR = "incomplete_signal_bar"
    OUTSIDE_SESSION = "outside_session"
    OUTSIDE_STRATEGY_WINDOW = "outside_strategy_window"
    MISSING_INDICATOR = "missing_indicator"
    INCOMPLETE_WINDOW = "incomplete_or_gapped_window"
    PRICE_VS_VWAP = "price_vs_vwap"
    EMA_ALIGNMENT = "ema_alignment"
    TREND_TOO_WEAK = "trend_too_weak"
    STRUCTURE = "structure_not_aligned"
    STRUCTURE_DATA_INVALID = "structure_data_invalid"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    CONTEXT_STALE = "context_stale"
    CONTEXT_INCOMPLETE = "context_incomplete"
    CONTEXT_MISSING_INDICATOR = "context_missing_indicator"
    CONTEXT_VWAP = "context_price_vs_vwap"
    CONTEXT_EMA = "context_ema_alignment"
    PULLBACK_NOT_REACHED = "pullback_not_reached"
    PULLBACK_TOO_DEEP = "pullback_too_deep"
    PULLBACK_BROKE_STRUCTURE = "pullback_broke_structure"
    EXTENDED_FROM_VWAP = "extended_from_vwap"
    EXTENDED_FROM_EMA = "extended_from_ema"
    NO_DIRECTIONAL_CLOSE = "no_directional_close"
    NO_CLOSE_BEYOND_PREV_EXTREME = "no_close_beyond_prev_extreme"
    NO_CLOSE_BEYOND_EMA = "no_close_beyond_ema_fast"
    RSI_NO_PULLBACK = "rsi_no_pullback"
    RSI_NOT_RECOVERED = "rsi_not_recovered"
    RSI_OVEREXTENDED = "rsi_overextended"
    RSI_NOT_TURNING = "rsi_not_turning"
    RVOL_UNAVAILABLE = "rvol_unavailable"
    RVOL_TOO_LOW = "rvol_too_low"
    STOP_UNAVAILABLE = "stop_unavailable"
    STOP_WRONG_SIDE = "stop_wrong_side"
    STOP_TOO_TIGHT = "stop_too_tight"
    STOP_TOO_WIDE = "stop_too_wide"
    TARGET_INVALID = "target_invalid"
    COOLDOWN = "cooldown"
    MAX_ENTRIES_PER_SESSION = "max_entries_per_session"
    SAME_PULLBACK = "same_pullback"
    ONE_ENTRY_PER_BAR = "one_entry_per_bar"


DIAGNOSTIC_COLUMNS = [
    "time", "bar_time", "session_date", "instrument", "direction", "decision", "rejection_reason", "reasons",
    "close", "vwap_relation", "ema_relation", "trend_strength_atr", "structure", "context_status",
    "pullback_status", "pullback_level", "extension_status", "candle_status", "rsi", "rsi_status",
    "rvol", "rvol_status", "stop_price", "stop_distance", "target_price", "target_distance", "pullback_id",
    "structure_data", "signal_id", "order_status", "order_id", "order_stage", "order_reason",
]
# Allowed order-lifecycle transitions (anything else is ignored and recorded as a violation).
LIFECYCLE_TRANSITIONS = {
    "submitted": {"accepted", "rejected"},
    "accepted": {"filled", "cancelled", "rejected"},
    "filled": set(), "rejected": set(), "cancelled": set(),  # terminal
}

SIGNAL_COLUMNS = ["signal_id", "direction", "time", "session_date", "pullback_id", "order_id", "status",
                  "accepted_at", "rejected_at", "filled_at", "cancelled_at", "stage", "reason", "detail"]


def _fmt(value: float) -> float:
    return float(value) if value is not None and not pd.isna(value) else np.nan


class VwapTrendPullbackStrategy(Strategy):
    """Rule-based VWAP trend pullback strategy. See module docstring for the rules."""

    def __init__(self, config: VwapPullbackConfig, session_timezone: str) -> None:
        """
        Args:
            config: Strategy configuration.
            session_timezone: Timezone of ``filters.session_windows`` (use the trading
                session's timezone from config/settings.toml).
        """
        self.cfg = config
        self.session_timezone = session_timezone
        self.name = config.name
        self.diagnostics: list[dict[str, Any]] = []
        self._reset()

    # ------------------------------------------------------------------ lifecycle
    def _reset(self) -> None:
        self.diagnostics = []
        self._contract: ContractSpec | None = None
        self._entries_by_session: dict[pd.Timestamp, int] = defaultdict(int)
        self._consumed_pullbacks: set[tuple[str, str]] = set()
        self._reserved_pullbacks: dict[tuple[str, str], str] = {}  # pullback -> signal id
        self._signals: dict[str, dict[str, Any]] = {}
        self._last_exit_time: pd.Timestamp | None = None
        self._last_submitted_session: pd.Timestamp | None = None
        self._counted_entry_orders: set[int] = set()
        self.lifecycle_violations: list[dict[str, Any]] = []
        self._columns_checked = False

    def on_start(self, contract: ContractSpec) -> None:
        self._reset()
        self._contract = contract

    def on_fill(self, fill: Fill) -> None:
        if fill.role is OrderRole.ENTRY:
            # Only one entry can be pending at a time, so this fill belongs to the last submitted signal.
            # Counted once per order id: a duplicate notification cannot count an entry twice.
            if self._last_submitted_session is not None and fill.order_id not in self._counted_entry_orders:
                self._counted_entry_orders.add(fill.order_id)
                self._entries_by_session[self._last_submitted_session] += 1
        else:  # stop-loss, take-profit or any exit closes the position
            self._last_exit_time = fill.bar_time

    def on_order_event(self, event: OrderEvent) -> None:
        """Reconcile a submitted signal with what the engine actually did."""
        sig = self._signals.get(event.client_id)
        if sig is None:
            self.lifecycle_violations.append({"client_id": event.client_id, "event": event.status,
                                              "from": None, "time": event.time, "issue": "unknown signal"})
            return
        if event.status not in LIFECYCLE_TRANSITIONS.get(sig["status"], set()):
            # Duplicate, delayed or impossible transition (e.g. rejected -> filled): ignored.
            self.lifecycle_violations.append({"client_id": event.client_id, "event": event.status,
                                              "from": sig["status"], "time": event.time,
                                              "issue": "invalid transition"})
            return
        sig["status"] = event.status
        sig[f"{event.status}_at"] = event.time
        sig.update(order_id=event.order_id, stage=event.stage, reason=event.reason, detail=event.detail)
        row = self.diagnostics[sig["row"]]
        row.update(order_status=event.status, order_id=event.order_id, order_stage=event.stage,
                   order_reason=event.reason)
        key = sig["pullback_key"]
        if event.status in ("rejected", "cancelled"):
            if self._reserved_pullbacks.get(key) == event.client_id:
                del self._reserved_pullbacks[key]  # not traded: the pullback stays available
        elif event.status == "filled":
            self._reserved_pullbacks.pop(key, None)
            self._consumed_pullbacks.add(key)

    def diagnostics_frame(self) -> pd.DataFrame:
        """All evaluated setups with their statuses, decision, rejection reasons and order outcome."""
        return pd.DataFrame(self.diagnostics, columns=DIAGNOSTIC_COLUMNS)

    def signals_frame(self) -> pd.DataFrame:
        """One row per submitted signal: its lifecycle in the engine (final ``status``)."""
        return pd.DataFrame(list(self._signals.values()), columns=SIGNAL_COLUMNS)

    # ------------------------------------------------------------------ decision
    def on_bar(self, ctx: StrategyContext) -> Sequence[OrderRequest] | None:
        cfg = self.cfg
        if not self._columns_checked:
            missing = [c for c in cfg.required_columns() if c not in ctx.history.columns]
            if missing:
                raise ValueError(f"Signal bars lack columns {missing}; check config/settings.toml [indicators]")
            self._columns_checked = True
        directions = [d for d, on in ((Side.LONG, cfg.enable_long), (Side.SHORT, cfg.enable_short)) if on]
        gate = self._gate(ctx)
        if gate is not None:
            for d in directions:
                self._record(ctx, d, {}, [gate], decision="skip")
            return []

        window, w_fail = self._window(ctx)
        structure = self._structure(ctx)
        requests: list[OrderRequest] = []
        for d in directions:
            info: dict[str, Any] = {}
            fails = [w_fail] if w_fail else []
            if not fails:
                fails = self._evaluate(ctx, d, window, structure, info)
            if not fails and requests:
                fails = [Reason.ONE_ENTRY_PER_BAR]  # the other direction already signalled on this bar
            if fails:
                self._record(ctx, d, info, fails, decision="reject")
                continue
            signal_id = f"{self.name}:{d.value}:{ctx.bar.name.isoformat()}"
            info.update(signal_id=signal_id, order_status="submitted")
            self._record(ctx, d, info, [], decision="signal")
            key = (d.value, info["pullback_id"])
            self._reserved_pullbacks[key] = signal_id
            self._last_submitted_session = ctx.session_date
            self._signals[signal_id] = {
                "signal_id": signal_id, "direction": d.value, "time": ctx.now, "session_date": ctx.session_date,
                "pullback_id": info["pullback_id"], "status": "submitted", "row": len(self.diagnostics) - 1,
                "pullback_key": key,
            }
            requests.append(OrderRequest.enter(
                d, OrderType.MARKET, stop_loss=info["stop_price"], take_profit=info["target_price"],
                tag=f"{self.name}:{d.value}", client_id=signal_id, **self._fill_limits(ctx, info)))
        return requests

    def _fill_limits(self, ctx: StrategyContext, info: dict[str, Any]) -> dict[str, float | None]:
        """Stop-width and reward/risk limits the engine must re-check at the ACTUAL fill."""
        st, tick, atr = self.cfg.stop, ctx.contract.tick_size, info.get("atr", 0.0)
        caps = [v for v in (st.max_distance_atr * atr if st.max_distance_atr and atr else None,
                            st.max_distance_ticks * tick if st.max_distance_ticks else None) if v]
        floor = max(st.min_distance_atr * atr, st.min_distance_ticks * tick)
        return {"max_stop_distance": min(caps) if caps else None,
                "min_stop_distance": floor if floor > 0 else None,
                "min_reward_risk": self.cfg.target.min_actual_r_multiple or None}

    # ------------------------------------------------------------------ gates and inputs
    def _gate(self, ctx: StrategyContext) -> str | None:
        if not ctx.position.is_flat:
            return Reason.POSITION_OPEN
        if ctx.has_pending_entry:
            return Reason.ENTRY_PENDING
        if not ctx.bar_is_complete:
            return Reason.INCOMPLETE_SIGNAL_BAR
        if ctx.session_date is None or pd.isna(ctx.session_date):
            return Reason.OUTSIDE_SESSION
        windows = self.cfg.filters.session_windows
        if windows:
            local = ctx.now.tz_convert(self.session_timezone).time()
            if not any(w.contains(local) for w in windows):
                return Reason.OUTSIDE_STRATEGY_WINDOW
        return None

    def _window(self, ctx: StrategyContext) -> tuple[pd.DataFrame, str | None]:
        """Bars used for pullback/RSI checks (current bar + lookback), validated."""
        cfg = self.cfg
        n = max(cfg.pullback.lookback_bars, 2)
        w = ctx.history.tail(n)
        cols = [cfg.ema_fast_col, cfg.ema_slow_col, cfg.rsi_col, cfg.atr_col, "vwap"]
        if len(w) < n or w[cols].isna().any().any():
            return w, Reason.MISSING_INDICATOR
        # Data integrity (always on): complete bars, exactly one period apart, same session.
        step = ctx.bar["available_at"] - ctx.bar.name
        contiguous = (pd.Series(w.index).diff().dropna() == step).all()
        same_session = (w["session_date"] == ctx.session_date).all()
        if not (contiguous and same_session and bool(w["is_complete"].all())):
            return w, Reason.INCOMPLETE_WINDOW
        return w, None

    def _structure(self, ctx: StrategyContext) -> pd.Series:
        """Confirmed structure of the current session from complete, time-contiguous pivots."""
        s = self.cfg.structure
        hist = ctx.history.tail(s.lookback_bars)
        hist = hist[hist["session_date"] == ctx.session_date]  # structure of the current session only
        if len(hist) < s.swing_left_bars + s.swing_right_bars + 1:
            return pd.Series({"structure": "insufficient_pivots", "last_high": np.nan, "last_low": np.nan,
                              "last_high_time": pd.NaT, "last_low_time": pd.NaT, "rejected_pivots": 0})
        step = ctx.bar["available_at"] - ctx.bar.name
        return market_structure(hist["high"], hist["low"], s.swing_left_bars, s.swing_right_bars,
                                valid=hist["is_complete"].astype(bool), step=step).iloc[-1]

    # ------------------------------------------------------------------ evaluation
    def _evaluate(self, ctx: StrategyContext, d: Side, w: pd.DataFrame, ms: pd.Series,
                  info: dict[str, Any]) -> list[str]:
        cfg, bar = self.cfg, ctx.bar
        sgn = d.sign
        long = d is Side.LONG
        fails: list[str] = []
        close, open_ = float(bar["close"]), float(bar["open"])
        vwap, atr = float(bar["vwap"]), float(bar[cfg.atr_col])
        ema_f, ema_s = float(bar[cfg.ema_fast_col]), float(bar[cfg.ema_slow_col])
        if atr <= 0:
            info["vwap_relation"] = "na"
            return [Reason.MISSING_INDICATOR]
        info["close"], info["atr"] = close, atr
        rejected = int(ms.get("rejected_pivots", 0) or 0)
        info["structure_data"] = f"rejected_pivots={rejected}" if rejected else "ok"

        # --- trend
        info["vwap_relation"] = "above" if close > vwap else "below" if close < vwap else "at"
        info["ema_relation"] = "fast_above" if ema_f > ema_s else "fast_below" if ema_f < ema_s else "equal"
        # Strength is direction-free; the direction is the separate EMA-alignment filter.
        strength = abs(ema_f - ema_s) / atr
        info["trend_strength_atr"] = strength
        info["structure"] = ms["structure"]
        if cfg.trend.require_price_vs_vwap and not (close - vwap) * sgn > 0:
            fails.append(Reason.PRICE_VS_VWAP)
        if cfg.trend.require_ema_alignment and not (ema_f - ema_s) * sgn > 0:
            fails.append(Reason.EMA_ALIGNMENT)
        if cfg.trend.min_trend_strength_atr and strength < cfg.trend.min_trend_strength_atr:
            fails.append(Reason.TREND_TOO_WEAK)
        wanted = STRUCTURE_BULLISH if long else STRUCTURE_BEARISH
        if cfg.trend.require_structure and ms["structure"] != wanted:
            fails.append(Reason.STRUCTURE_DATA_INVALID if rejected else Reason.STRUCTURE)

        # --- higher-timeframe context
        status = self._context_status(ctx, d)
        info["context_status"] = status
        if status not in {"ok", "disabled"}:
            fails.append(status)

        # --- pullback
        pw = w.tail(cfg.pullback.lookback_bars)
        level_cols = {"ema_fast": cfg.ema_fast_col, "ema_slow": cfg.ema_slow_col, "vwap": "vwap"}
        touched, too_deep, last_touch = None, False, None
        for t_bar, b in pw.iterrows():
            extreme = float(b["low"] if long else b["high"])
            b_atr = float(b[cfg.atr_col])
            for name in cfg.pullback.levels:
                level = float(b[level_cols[name]])
                # distance of the extreme beyond the level, measured against the trend
                beyond = (level - extreme) * sgn  # > 0: below the level (long) / above it (short)
                if -cfg.pullback.zone_atr * b_atr <= beyond <= cfg.pullback.max_penetration_atr * b_atr:
                    touched = touched or name
                    last_touch = t_bar
                elif beyond > cfg.pullback.max_penetration_atr * b_atr:
                    too_deep = True
        info["pullback_level"] = touched
        protective = ms["last_low"] if long else ms["last_high"]
        pullback_extreme = float(pw["low"].min() if long else pw["high"].max())
        broke = (cfg.pullback.require_structure_intact and not pd.isna(protective)
                 and (pullback_extreme - float(protective)) * sgn <= 0)
        if touched is None:
            info["pullback_status"] = "too_deep" if too_deep else "not_reached"
            fails.append(Reason.PULLBACK_TOO_DEEP if too_deep else Reason.PULLBACK_NOT_REACHED)
        elif broke:
            info["pullback_status"] = "broke_structure"
            fails.append(Reason.PULLBACK_BROKE_STRUCTURE)
        else:
            info["pullback_status"] = f"touched:{touched}"

        # --- extension
        ext = []
        if cfg.extension.max_vwap_distance_atr and (close - vwap) * sgn / atr > cfg.extension.max_vwap_distance_atr:
            ext.append(Reason.EXTENDED_FROM_VWAP)
        if cfg.extension.max_ema_distance_atr and abs(close - ema_f) / atr > cfg.extension.max_ema_distance_atr:
            ext.append(Reason.EXTENDED_FROM_EMA)
        info["extension_status"] = ",".join(ext) or "ok"
        fails += ext

        # --- confirmation candle
        cc = []
        prev = w.iloc[-2]
        if cfg.confirmation.require_directional_close and not (close - open_) * sgn > 0:
            cc.append(Reason.NO_DIRECTIONAL_CLOSE)
        if cfg.confirmation.require_close_beyond_prev_extreme:
            prev_extreme = float(prev["high"] if long else prev["low"])
            if not (close - prev_extreme) * sgn > 0:
                cc.append(Reason.NO_CLOSE_BEYOND_PREV_EXTREME)
        if cfg.confirmation.require_close_beyond_ema_fast and not (close - ema_f) * sgn > 0:
            cc.append(Reason.NO_CLOSE_BEYOND_EMA)
        info["candle_status"] = ",".join(cc) or "ok"
        fails += cc

        # --- RSI recovery
        r = cfg.rsi
        rsi_now, rsi_prev = float(bar[cfg.rsi_col]), float(prev[cfg.rsi_col])
        info["rsi"] = rsi_now
        rs = []
        if r.enabled:
            win = pw[cfg.rsi_col]
            if long:
                if not win.min() <= r.long_pullback_max:
                    rs.append(Reason.RSI_NO_PULLBACK)
                if not rsi_now >= r.long_recovery_min:
                    rs.append(Reason.RSI_NOT_RECOVERED)
                if rsi_now > r.long_entry_max:
                    rs.append(Reason.RSI_OVEREXTENDED)
                if r.require_turn and not rsi_now > rsi_prev:
                    rs.append(Reason.RSI_NOT_TURNING)
            else:
                if not win.max() >= r.short_pullback_min:
                    rs.append(Reason.RSI_NO_PULLBACK)
                if not rsi_now <= r.short_recovery_max:
                    rs.append(Reason.RSI_NOT_RECOVERED)
                if rsi_now < r.short_entry_min:
                    rs.append(Reason.RSI_OVEREXTENDED)
                if r.require_turn and not rsi_now < rsi_prev:
                    rs.append(Reason.RSI_NOT_TURNING)
        info["rsi_status"] = ",".join(rs) or ("ok" if r.enabled else "disabled")
        fails += rs

        # --- RVOL (NaN never passes)
        rvol = bar["rvol"]
        info["rvol"] = _fmt(rvol)
        if not cfg.rvol.enabled:
            info["rvol_status"] = "disabled"
        elif pd.isna(rvol):
            info["rvol_status"] = "unavailable"
            fails.append(Reason.RVOL_UNAVAILABLE)
        elif float(rvol) < cfg.rvol.min_rvol:
            info["rvol_status"] = "too_low"
            fails.append(Reason.RVOL_TOO_LOW)
        else:
            info["rvol_status"] = "ok"

        # --- stop and target
        fails += self._stop_and_target(ctx, d, close, atr, ms, info)

        # --- trade-management filters
        f = cfg.filters
        info["pullback_id"] = self._pullback_id(ctx, d, ms, last_touch)
        if f.cooldown_minutes and self._last_exit_time is not None and \
                ctx.now < self._last_exit_time + pd.Timedelta(minutes=f.cooldown_minutes):
            fails.append(Reason.COOLDOWN)
        if f.max_entries_per_session and self._entries_by_session[ctx.session_date] >= f.max_entries_per_session:
            fails.append(Reason.MAX_ENTRIES_PER_SESSION)
        key = (d.value, info["pullback_id"])
        if f.one_entry_per_pullback and (key in self._consumed_pullbacks or key in self._reserved_pullbacks):
            fails.append(Reason.SAME_PULLBACK)
        return fails

    @staticmethod
    def _pullback_id(ctx: StrategyContext, d: Side, ms: pd.Series, last_touch: pd.Timestamp | None) -> str:
        """Stable identity of the pullback (see module docstring)."""
        long = d is Side.LONG
        anchor = ms["last_high_time"] if long else ms["last_low_time"]
        if not pd.isna(anchor):
            return str(anchor)
        if last_touch is None:
            return "no_pullback"
        h = ctx.history
        h = h[(h["session_date"] == ctx.session_date) & h["is_complete"].astype(bool) & (h.index <= last_touch)]
        if h.empty:
            return "no_pullback"
        series = h["high"] if long else -h["low"]
        # Same format as the pivot anchor: when this extreme bar is later confirmed as the
        # swing pivot, the id does not change, so the pullback cannot be entered twice.
        return str(series[series == series.max()].index[-1])  # latest bar holding the extreme

    def _context_status(self, ctx: StrategyContext, d: Side) -> str:
        cfg = self.cfg
        tf = cfg.context_timeframe
        if not tf:
            return "disabled"
        if tf not in ctx.context:
            raise ValueError(f"Context timeframe {tf!r} was not supplied to the engine (context_bars)")
        frame = ctx.context[tf]
        if frame.empty:
            return Reason.CONTEXT_UNAVAILABLE
        c = frame.iloc[-1]  # last candle COMPLETED by now (the engine cut the frame)
        step = c["available_at"] - c.name
        if c["available_at"] < ctx.now - step:
            return Reason.CONTEXT_STALE  # the most recent context candle is missing (data gap)
        if pd.isna(c["session_date"]) or c["session_date"] != ctx.session_date:
            return Reason.CONTEXT_UNAVAILABLE  # no completed context candle in this session yet
        if not bool(c["is_complete"]):
            return Reason.CONTEXT_INCOMPLETE
        cols = [cfg.ema_fast_col, cfg.ema_slow_col, "vwap"]
        if c[cols].isna().any():
            return Reason.CONTEXT_MISSING_INDICATOR
        sgn = d.sign
        if cfg.context.require_price_vs_vwap and not (c["close"] - c["vwap"]) * sgn > 0:
            return Reason.CONTEXT_VWAP
        if cfg.context.require_ema_alignment and not (c[cfg.ema_fast_col] - c[cfg.ema_slow_col]) * sgn > 0:
            return Reason.CONTEXT_EMA
        return "ok"

    def _stop_and_target(self, ctx: StrategyContext, d: Side, close: float, atr: float,
                         ms: pd.Series, info: dict[str, Any]) -> list[str]:
        st = self.cfg.stop
        tick = ctx.contract.tick_size
        long = d is Side.LONG
        sgn = d.sign
        if st.method == "swing":
            pivot = ms["last_low"] if long else ms["last_high"]
            if pd.isna(pivot):
                return [Reason.STOP_UNAVAILABLE]
            raw = float(pivot) - sgn * st.buffer_ticks * tick
        else:
            raw = close - sgn * st.atr_multiplier * atr
        # Same conservative rounding the engine applies to protective stops.
        stop = conservative_order_price(raw, tick, OrderType.STOP, is_buy=not long)
        dist = (close - stop) * sgn
        info["stop_price"], info["stop_distance"] = stop, dist
        if dist <= 0:
            return [Reason.STOP_WRONG_SIDE]
        fails = []
        if dist < max(st.min_distance_atr * atr, st.min_distance_ticks * tick) - 1e-9:
            fails.append(Reason.STOP_TOO_TIGHT)
        if (st.max_distance_atr and dist > st.max_distance_atr * atr + 1e-9) or \
                (st.max_distance_ticks and dist > st.max_distance_ticks * tick + 1e-9):
            fails.append(Reason.STOP_TOO_WIDE)
        target = conservative_order_price(close + sgn * self.cfg.target.r_multiple * dist, tick,
                                          OrderType.LIMIT, is_buy=not long)
        tdist = (target - close) * sgn
        info["target_price"], info["target_distance"] = target, tdist
        if tdist <= 0:
            fails.append(Reason.TARGET_INVALID)
        return fails

    # ------------------------------------------------------------------ diagnostics
    def _record(self, ctx: StrategyContext, d: Side, info: dict[str, Any], fails: list[str], decision: str) -> None:
        row = {c: info.get(c, np.nan) for c in DIAGNOSTIC_COLUMNS}
        row.update(
            time=ctx.now, bar_time=ctx.bar.name, session_date=ctx.session_date,
            instrument=ctx.contract.root, direction=d.value, decision=decision,
            rejection_reason=fails[0] if fails else "", reasons=";".join(fails),
        )
        if "close" not in info:
            row["close"] = _fmt(ctx.bar["close"])
        self.diagnostics.append(row)

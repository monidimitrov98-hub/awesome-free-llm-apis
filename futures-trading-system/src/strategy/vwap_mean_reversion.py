"""VWAP Mean Reversion -- V1 RESEARCH CANDIDATE (Sprint 11). Not a confirmed original implementation.

Concept (the user's): VWAP-centred bands, RSI oversold / overbought confirmation, price sufficiently far from
VWAP, entry only after price-action confirmation, target = reversion toward VWAP.

Rules of V1 (LONG; SHORT is the mirror image):

1. Features are computed causally on the SELECTED session only (``scope.session``; RTH and ETH are never
   mixed): VWAP and its volume-weighted sigma (:mod:`src.indicators.vwap_bands`), RSI and ATR (existing
   Wilder implementations) on that session's bars.
2. SETUP on a completed signal bar: ``close <= VWAP - band_k * sigma`` AND ``RSI <= rsi_oversold`` AND
   ``sigma >= min_sigma_ticks * tick`` AND at least ``min_session_bars`` bars of the session have completed.
3. CONFIRMATION on a LATER bar, at most ``max_bars_after_setup`` bars after the setup bar, by exactly ONE
   configured method (C1-C4, see :data:`CONFIRMATION_METHODS`). The setup bar itself never confirms.
4. ENTRY: a market order created at the confirmation bar's ``available_at`` (signal time). The engine fills
   it at the open of the first execution bar that opens at or after that time -- never on the signal bar
   (no same-bar optimistic fill). The close must still be below VWAP (room to revert).
5. TARGET: ``close + fraction * (VWAP - close)`` using the confirmation bar's VWAP (fixed at signal time).
6. STOP (one method): swing low of the last N session bars, ATR multiple, or the band at ``stop.band_k``,
   minus ``buffer_ticks``. Rejected when not below the close, or wider than ``max_risk_points``.
7. One position at a time, no pyramiding; at most ``max_entries_per_session`` entries; a cooldown after
   every exit; no entry within ``entry_cutoff_minutes`` of the session end. Open positions are flattened by
   the engine before the session ends (``config/backtest.toml`` execution.flatten_minutes_before_session_end)
   and optionally after ``max_holding_bars``.

The strategy only proposes requests: sizing, costs, slippage, NewsGuard, RiskManager, AccountGuard and the
prop firm compliance engine are applied by the engine and cannot be bypassed from here.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

from src.backtest.orders import Fill, OrderEvent, OrderRequest, OrderRole, OrderType, Side
from src.config import ContractSpec, SessionConfig
from src.data.sessions import assign_session_dates, session_bounds
from src.indicators.atr import atr
from src.indicators.rsi import rsi
from src.indicators.vwap_bands import vwap_bands
from src.strategy.base import Strategy, StrategyContext
from src.strategy.vwap_mean_reversion_config import CONFIRMATION_METHODS, VwapMeanReversionConfig

DIAGNOSTIC_COLUMNS = ["time", "bar_time", "session_date", "direction", "event", "reason", "close", "vwap", "sigma",
                      "z", "rsi", "band", "stop", "target", "confirmation", "setup_bar_time", "signal_id",
                      "order_status", "eligible_execution_from"]
SIGNAL_COLUMNS = ["signal_id", "direction", "time", "bar_time", "session_date", "setup_bar_time", "confirmation",
                  "entry_reference", "stop", "target", "status", "reason", "order_id"]
REQUIRED_COLUMNS = ("open", "high", "low", "close", "mr_vwap", "mr_sigma", "mr_z", "mr_rsi", "mr_atr",
                    "mr_session_bar", "session_date")
LIFECYCLE = {"submitted": {"accepted", "rejected"}, "accepted": {"filled", "rejected", "cancelled"},
             "filled": set(), "rejected": set(), "cancelled": set()}


def add_mean_reversion_features(frame: pd.DataFrame, session: SessionConfig, cfg: VwapMeanReversionConfig
                                ) -> pd.DataFrame:
    """Causal features for one bar frame (signal timeframe). ``session_date`` is REPLACED by the selected
    session's dates (NaN outside it): the engine then trades, flattens and resets daily limits on that session."""
    out = frame.copy()
    ks = tuple(sorted({cfg.indicators.band_k, cfg.stop.band_k}))
    b = vwap_bands(out["high"], out["low"], out["close"], out["volume"], session, k=ks,
                   price=cfg.indicators.vwap_price, prefix="mr")
    for col in b.columns:
        if col != "mr_session":
            out[col] = b[col]
    out["session_date"] = assign_session_dates(out.index, session)
    in_s = out["session_date"].notna().to_numpy()
    out["mr_session_bar"] = np.where(in_s, out.groupby("session_date", dropna=True).cumcount() + 1, 0)
    src = out[in_s] if cfg.indicators.indicator_scope == "session_bars" else out
    r = rsi(src["close"], cfg.indicators.rsi_period)
    a = atr(src["high"], src["low"], src["close"], cfg.indicators.atr_period)
    out["mr_rsi"] = r.reindex(out.index)
    out["mr_atr"] = a.reindex(out.index)
    if cfg.indicators.indicator_scope == "session_bars":
        out.loc[~in_s, ["mr_rsi", "mr_atr"]] = np.nan
    return out


class VwapMeanReversionStrategy(Strategy):
    name = "vwap_mean_reversion"

    def __init__(self, config: VwapMeanReversionConfig, session: SessionConfig) -> None:
        self.cfg = config
        self.session = session
        self.name = f"vwap_mean_reversion:{config.version}"
        self._reset()

    # ------------------------------------------------------------------ lifecycle
    def _reset(self) -> None:
        self.diagnostics: list[dict[str, Any]] = []
        self._signals: dict[str, dict[str, Any]] = {}
        self._contract: ContractSpec | None = None
        self._idx = 0                      # index of the current signal bar (counts every on_bar call)
        self._session: Any = None
        self._setups: dict[str, dict[str, Any]] = {}
        self._entries: dict[Any, int] = {}
        self._entry_idx: int | None = None
        self._exit_pending_idx = False
        self._last_exit_idx: int | None = None
        self._counted: set[int] = set()
        self._last_signal_session: Any = None
        self._end_cache: dict[Any, pd.Timestamp] = {}
        self._checked = False
        self.lifecycle_violations: list[dict[str, Any]] = []

    def on_start(self, contract: ContractSpec) -> None:
        self._reset()
        self._contract = contract

    def on_fill(self, fill: Fill) -> None:
        if fill.role is OrderRole.ENTRY:
            if fill.order_id not in self._counted:
                self._counted.add(fill.order_id)
                self._entries[self._last_signal_session] = self._entries.get(self._last_signal_session, 0) + 1
                self._entry_idx = self._idx
        else:
            self._exit_pending_idx = True    # resolved to a bar index on the next on_bar
            self._entry_idx = None

    def on_order_event(self, event: OrderEvent) -> None:
        sig = self._signals.get(event.client_id)
        if sig is None or event.status not in LIFECYCLE.get(sig["status"], set()):
            self.lifecycle_violations.append({"client_id": event.client_id, "event": event.status,
                                              "from": sig["status"] if sig else None, "time": event.time})
            return
        sig.update(status=event.status, reason=event.reason, order_id=event.order_id)
        self.diagnostics[sig["row"]]["order_status"] = event.status

    def diagnostics_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.diagnostics, columns=DIAGNOSTIC_COLUMNS)

    def signals_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{k: v for k, v in s.items() if k != "row"} for s in self._signals.values()],
                            columns=SIGNAL_COLUMNS)

    # ------------------------------------------------------------------ helpers
    def _session_end(self, sd: Any) -> pd.Timestamp:
        if sd not in self._end_cache:
            self._end_cache[sd] = session_bounds([pd.Timestamp(sd)], self.session).iloc[0]["end"]
        return self._end_cache[sd]

    def _record(self, ctx: StrategyContext, d: str | None, event: str, reason: str = "", **kw: Any) -> int:
        bar = ctx.bar
        row = {"time": ctx.now, "bar_time": bar.name, "session_date": ctx.session_date, "direction": d,
               "event": event, "reason": reason, "close": float(bar["close"]), "vwap": _f(bar["mr_vwap"]),
               "sigma": _f(bar["mr_sigma"]), "z": _f(bar["mr_z"]), "rsi": _f(bar["mr_rsi"]),
               "band": None, "stop": None, "target": None, "confirmation": self.cfg.confirmation.method,
               "setup_bar_time": None, "signal_id": None, "order_status": None, "eligible_execution_from": None}
        row.update(kw)
        self.diagnostics.append(row)
        return len(self.diagnostics) - 1

    # ------------------------------------------------------------------ decision
    def on_bar(self, ctx: StrategyContext) -> Sequence[OrderRequest] | None:
        cfg, ind = self.cfg, self.cfg.indicators
        self._idx += 1
        if not self._checked:
            missing = [c for c in REQUIRED_COLUMNS if c not in ctx.history.columns]
            if missing:
                raise ValueError(f"signal bars lack mean-reversion features {missing} "
                                 "(use add_mean_reversion_features)")
            self._checked = True
        if self._exit_pending_idx:
            self._last_exit_idx, self._exit_pending_idx = self._idx, False
        sd = ctx.session_date
        if sd is None or pd.isna(sd):
            self._setups.clear()
            return []
        if sd != self._session:
            self._session, self._setups = sd, {}
        if ctx.position.side is not None:          # in a trade: no new setups; optional time exit
            self._setups.clear()
            if cfg.execution.max_holding_bars and self._entry_idx is not None \
                    and self._idx - self._entry_idx >= cfg.execution.max_holding_bars:
                self._record(ctx, ctx.position.side.value, "exit", "max_holding_bars")
                return [OrderRequest.exit(tag="max_holding_bars")]
            return []
        if ctx.has_pending_entry:
            return []
        bar = ctx.bar
        vals = {k: _f(bar[c]) for k, c in (("close", "close"), ("open", "open"), ("vwap", "mr_vwap"),
                                           ("sigma", "mr_sigma"), ("rsi", "mr_rsi"), ("atr", "mr_atr"))}
        tick = self._contract.tick_size if self._contract else 0.25
        ready = (int(bar["mr_session_bar"]) >= ind.min_session_bars and all(
            v is not None for k, v in vals.items() if k != "atr") and vals["sigma"] >= ind.min_sigma_ticks * tick)
        prev = ctx.history.iloc[-2] if len(ctx.history) >= 2 else None
        same_session_prev = prev is not None and prev["session_date"] == sd
        directions = [d for d, on in (("long", cfg.scope.enable_long), ("short", cfg.scope.enable_short)) if on]
        # expire old setups
        for d in list(self._setups):
            if self._idx - self._setups[d]["idx"] > cfg.confirmation.max_bars_after_setup:
                del self._setups[d]
        # gates that apply to entries (setups are still tracked)
        gate = None
        if ctx.now >= self._session_end(sd) - pd.Timedelta(minutes=cfg.execution.entry_cutoff_minutes):
            gate = "entry_cutoff"
        elif self._entries.get(sd, 0) >= cfg.execution.max_entries_per_session:
            gate = "max_entries_per_session"
        elif self._last_exit_idx is not None and self._idx - self._last_exit_idx < cfg.execution.cooldown_bars_after_exit:
            gate = "cooldown_after_exit"
        confirmed: list[tuple[str, dict[str, Any]]] = []
        for d in directions:
            setup = self._setups.get(d)
            if setup is not None and ready and same_session_prev:
                if self._confirms(d, bar, prev, vals):
                    confirmed.append((d, setup))
                    continue
            if ready and self._is_setup(d, bar, vals):
                self._setups[d] = {"idx": self._idx, "bar_time": bar.name}
                self._record(ctx, d, "setup", band=_f(bar[self._band_col(d, ind.band_k)]))
        if not confirmed:
            return []
        if len(confirmed) > 1:
            for d, s in confirmed:
                self._record(ctx, d, "reject", "conflicting_directions", setup_bar_time=s["bar_time"])
            self._setups.clear()
            return []
        d, setup = confirmed[0]
        del self._setups[d]
        if gate is not None:
            self._record(ctx, d, "reject", gate, setup_bar_time=setup["bar_time"])
            return []
        return self._entry(ctx, d, setup, vals, tick)

    def _band_col(self, d: str, k: float) -> str:
        return f"mr_lower_{k:g}" if d == "long" else f"mr_upper_{k:g}"

    def _is_setup(self, d: str, bar: pd.Series, v: dict[str, float]) -> bool:
        ind = self.cfg.indicators
        band = _f(bar[self._band_col(d, ind.band_k)])
        if band is None:
            return False
        if d == "long":
            return v["close"] <= band and v["rsi"] <= ind.rsi_oversold
        return v["close"] >= band and v["rsi"] >= ind.rsi_overbought

    def _confirms(self, d: str, bar: pd.Series, prev: pd.Series, v: dict[str, float]) -> bool:
        m, ind = self.cfg.confirmation.method, self.cfg.indicators
        o, c = v["open"], v["close"]
        po, pc, ph, pl = float(prev["open"]), float(prev["close"]), float(prev["high"]), float(prev["low"])
        long = d == "long"
        if m == "C1":   # engulfing
            if long:
                return pc < po and c > o and c >= po and o <= pc
            return pc > po and c < o and c <= po and o >= pc
        if m == "C2":   # close back inside the entry band
            band = _f(bar[self._band_col(d, ind.band_k)])
            return band is not None and (c > band if long else c < band)
        if m == "C3":   # close beyond the previous bar's extreme in the reversal direction
            return c > ph if long else c < pl
        if m == "C4":   # RSI crosses back out of the extreme zone
            pr = _f(prev["mr_rsi"])
            if pr is None:
                return False
            return (pr <= ind.rsi_oversold < v["rsi"]) if long else (pr >= ind.rsi_overbought > v["rsi"])
        raise ValueError(f"unknown confirmation method {m}")  # validated at load; defensive

    def _entry(self, ctx: StrategyContext, d: str, setup: dict[str, Any], v: dict[str, float],
               tick: float) -> list[OrderRequest]:
        cfg, st = self.cfg, self.cfg.stop
        close, vwap = v["close"], v["vwap"]
        sgn = 1 if d == "long" else -1
        if (vwap - close) * sgn <= 0:
            self._record(ctx, d, "reject", "no_reversion_room", setup_bar_time=setup["bar_time"])
            return []
        if st.method == "swing":
            h = ctx.history[ctx.history["session_date"] == ctx.session_date].tail(st.swing_lookback_bars)
            stop = float(h["low"].min()) if d == "long" else float(h["high"].max())
        elif st.method == "atr":
            if v["atr"] is None:
                self._record(ctx, d, "reject", "atr_unavailable", setup_bar_time=setup["bar_time"])
                return []
            stop = close - sgn * st.atr_multiple * v["atr"]
        else:
            b = _f(ctx.bar[self._band_col(d, st.band_k)])
            if b is None:
                self._record(ctx, d, "reject", "stop_band_unavailable", setup_bar_time=setup["bar_time"])
                return []
            stop = b
        stop -= sgn * st.buffer_ticks * tick
        risk = (close - stop) * sgn
        target = close + cfg.target.fraction * (vwap - close)
        common = dict(setup_bar_time=setup["bar_time"], stop=stop, target=target)
        if risk <= 0:
            self._record(ctx, d, "reject", "stop_not_beyond_entry", **common)
            return []
        if st.max_risk_points and risk > st.max_risk_points:
            self._record(ctx, d, "reject", "stop_too_wide", **common)
            return []
        if cfg.target.min_reward_risk and (target - close) * sgn / risk < cfg.target.min_reward_risk:
            self._record(ctx, d, "reject", "reward_risk_below_min", **common)
            return []
        signal_id = f"{self.name}:{d}:{ctx.bar.name.isoformat()}"
        row = self._record(ctx, d, "signal", signal_id=signal_id, order_status="submitted",
                           eligible_execution_from=ctx.now, **common)
        self._last_signal_session = ctx.session_date
        self._signals[signal_id] = {"signal_id": signal_id, "direction": d, "time": ctx.now, "bar_time": ctx.bar.name,
                                    "session_date": ctx.session_date, "setup_bar_time": setup["bar_time"],
                                    "confirmation": cfg.confirmation.method, "entry_reference": close, "stop": stop,
                                    "target": target, "status": "submitted", "reason": "", "order_id": None, "row": row}
        return [OrderRequest.enter(
            Side.LONG if d == "long" else Side.SHORT, OrderType.MARKET, stop_loss=stop, take_profit=target,
            quantity=cfg.execution.quantity or None, tag=f"{self.name}:{d}", client_id=signal_id,
            max_stop_distance=st.max_risk_points or None, min_reward_risk=cfg.target.min_reward_risk or None)]


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


__all__ = ["VwapMeanReversionStrategy", "add_mean_reversion_features", "CONFIRMATION_METHODS"]

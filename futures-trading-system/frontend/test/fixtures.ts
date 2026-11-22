import type { Snapshot } from "../src/api/types";

export function snapshot(over: Partial<Snapshot["meta"]> = {}): Snapshot {
  return {
    meta: { app: "FTS Terminal", version: "0.7.0", boot_id: "boot1", started_at: "2026-01-01T00:00:00Z", state: "running", outcome: null,
      error: null, mode: "MOCK", data_label: "SYNTHETIC", description: "Synthetic prices", run_kind: "infrastructure_validation",
      instruments: ["MES", "MNQ"], contracts: { MES: "MES 12-26", MNQ: "MNQ 12-26" }, timeframes: ["1min", "5min", "15min"],
      read_only: true, order_entry: false, modes: { MOCK: { available: true }, PAPER: { available: false, note: "not implemented" } }, ...over },
    seq: 1,
    market: {
      time: "2026-01-01T15:00:00Z",
      instruments: {
        MES: { subscription: "subscribed", health: "ok", entries_allowed_by_data: true, last_event_time: "2026-01-01T15:00:00Z",
          freshness_seconds: 0.4, bid: 6000, ask: 6000.25, spread: 0.25, last: 6000.25, quote_time: null, last_time: null,
          contract: "MES 12-26", contract_expiry: "2026-12-18", session_date: "2026-01-01", entries_allowed: false,
          entry_block_reason: "infrastructure_validation" },
        MNQ: { subscription: "subscribed", health: "ok", entries_allowed_by_data: true, last_event_time: "2026-01-01T15:00:00Z",
          freshness_seconds: 0.4, bid: 21000, ask: 21000.25, spread: 0.25, last: 21000, quote_time: null, last_time: null,
          contract: "MNQ 12-26", contract_expiry: "2026-12-18", session_date: "2026-01-01", entries_allowed: true, entry_block_reason: "" },
      },
      feed: { provider: "MOCK bridge (synthetic data)", authenticated: true, provider_connection: "Connected" },
      latency: { processing_ms_p50: 1, processing_ms_p99: 2, data_delay_ms_p50: 1, data_delay_ms_p99: 3 },
      validation: { rejected: {}, events_processed: 100 }, market_session: { session_date: "2026-01-01", in_session: true },
    },
    status: {
      account: { initial_balance: 10000, equity: 10000, known_equity: 10000, equity_complete: true, realized_gross: 0, commissions: 0,
        net_realized: 0, unrealized: 0, slippage: 0, total_costs: 0, high_water: 10000, max_drawdown: 0, current_drawdown: 0,
        session_date: "2026-01-01", session_realized_net: 0, session_pnl: 0, positions: { MES: 0, MNQ: 0 }, trades: 0, wins: 0,
        losses: 0, win_rate: null, profit_factor: null, sample_warning: true },
      news: { MES: { state: "CALENDAR_STALE", safe: false }, MNQ: { state: "CALENDAR_STALE", safe: false } },
      entries: { MES: { ok: true, reason: "" }, MNQ: { ok: true, reason: "" } },
      guard: { enabled: true, breached: false, locked_permanently: false, remaining_buffer: 2000, block: null },
      session_date: "2026-01-01", daily_loss_used_pct: 0, accepting_entries: true, mode_authorizes_entries: false,
      runtime: { state: "running", outcome: null, events: 100, stopped_reason: null },
    },
    activity: [
      { seq: 1, kind: "entry_decision", time: "2026-01-01T15:00:00Z", summary: "MES long signal REJECTED: news_calendar_stale", root: "MES",
        payload: { allowed: false, reason: "news_calendar_stale" } },
      { seq: 2, kind: "feed_health", time: "2026-01-01T15:00:01Z", summary: "Data health block: not_subscribed (MNQ)", root: "MNQ",
        payload: { action: "block" } },
    ],
    notifications: [{ id: 1, time: "2026-01-01T15:00:00Z", severity: "warning", category: "strategy", title: "Strategy signal rejected",
      description: "MES long: news_calendar_stale", root: "MES" }],
  };
}

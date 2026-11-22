// UI preferences only (appearance and display). Stored locally in this browser; validated on load.
// Nothing here can change trading, risk, NewsGuard, strategy or execution settings.

export interface Prefs {
  theme: "dark" | "light";
  density: "compact" | "comfortable";
  sidebarCollapsed: boolean;
  defaultInstrument: "MES" | "MNQ";
  defaultTimeframe: "1min" | "5min" | "15min";
  indicators: { vwap: boolean; emaFast: boolean; emaSlow: boolean; volume: boolean; signals: boolean; fills: boolean; levels: boolean };
  notifications: { enabled: boolean; minSeverity: "info" | "warning" | "critical" };
  timezone: "UTC" | "America/New_York" | "local";
  startPage: string;
}

export const DEFAULT_PREFS: Prefs = {
  theme: "dark", density: "compact", sidebarCollapsed: false, defaultInstrument: "MES", defaultTimeframe: "5min",
  indicators: { vwap: true, emaFast: true, emaSlow: true, volume: true, signals: true, fills: true, levels: true },
  notifications: { enabled: true, minSeverity: "info" }, timezone: "America/New_York", startPage: "dashboard",
};

const KEY = "fts.terminal.prefs.v1";
const PAGES = ["dashboard", "charts", "analysis", "strategy", "positions", "risk", "journal", "performance", "market", "system", "settings"];

function pick<T>(v: unknown, allowed: readonly T[], dflt: T): T { return allowed.includes(v as T) ? (v as T) : dflt; }
function bool(v: unknown, dflt: boolean): boolean { return typeof v === "boolean" ? v : dflt; }

export function validatePrefs(raw: unknown): Prefs {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const ind = (r.indicators && typeof r.indicators === "object" ? r.indicators : {}) as Record<string, unknown>;
  const no = (r.notifications && typeof r.notifications === "object" ? r.notifications : {}) as Record<string, unknown>;
  const d = DEFAULT_PREFS;
  return {
    theme: pick(r.theme, ["dark", "light"] as const, d.theme),
    density: pick(r.density, ["compact", "comfortable"] as const, d.density),
    sidebarCollapsed: bool(r.sidebarCollapsed, d.sidebarCollapsed),
    defaultInstrument: pick(r.defaultInstrument, ["MES", "MNQ"] as const, d.defaultInstrument),
    defaultTimeframe: pick(r.defaultTimeframe, ["1min", "5min", "15min"] as const, d.defaultTimeframe),
    indicators: {
      vwap: bool(ind.vwap, d.indicators.vwap), emaFast: bool(ind.emaFast, d.indicators.emaFast),
      emaSlow: bool(ind.emaSlow, d.indicators.emaSlow), volume: bool(ind.volume, d.indicators.volume),
      signals: bool(ind.signals, d.indicators.signals), fills: bool(ind.fills, d.indicators.fills),
      levels: bool(ind.levels, d.indicators.levels),
    },
    notifications: { enabled: bool(no.enabled, d.notifications.enabled),
      minSeverity: pick(no.minSeverity, ["info", "warning", "critical"] as const, d.notifications.minSeverity) },
    timezone: pick(r.timezone, ["UTC", "America/New_York", "local"] as const, d.timezone),
    startPage: pick(r.startPage, PAGES, d.startPage),
  };
}

export function loadPrefs(): Prefs {
  try { return validatePrefs(JSON.parse(window.localStorage.getItem(KEY) ?? "{}")); } catch { return DEFAULT_PREFS; }
}

export function savePrefs(p: Prefs): void {
  try { window.localStorage.setItem(KEY, JSON.stringify(validatePrefs(p))); } catch { /* storage unavailable */ }
}

export function tzOf(p: Prefs): string {
  return p.timezone === "local" ? Intl.DateTimeFormat().resolvedOptions().timeZone : p.timezone;
}

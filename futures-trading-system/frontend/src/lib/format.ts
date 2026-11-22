// Formatting only: the frontend never computes trading values, it displays backend values.
export const UNAVAILABLE = "UNAVAILABLE";

export function isNum(v: unknown): v is number { return typeof v === "number" && Number.isFinite(v); }

export function price(v: unknown, digits = 2): string { return isNum(v) ? v.toFixed(digits) : UNAVAILABLE; }

export function money(v: unknown, opts: { sign?: boolean } = {}): string {
  if (!isNum(v)) return UNAVAILABLE;
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sign = v < 0 ? "-" : opts.sign && v > 0 ? "+" : "";
  return `${sign}$${s}`;
}

export function pct(v: unknown, digits = 1): string { return isNum(v) ? `${v.toFixed(digits)}%` : UNAVAILABLE; }
export function ratio(v: unknown, digits = 2): string { return isNum(v) ? v.toFixed(digits) : UNAVAILABLE; }
export function int(v: unknown): string { return isNum(v) ? Math.round(v).toLocaleString("en-US") : UNAVAILABLE; }

export function pnlClass(v: unknown): string { return isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "dim"; }

export function time(iso: unknown, withDate = false, tz = "UTC"): string {
  if (typeof iso !== "string" || !iso) return UNAVAILABLE;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return UNAVAILABLE;
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: tz };
  if (withDate) Object.assign(opts, { year: "numeric", month: "2-digit", day: "2-digit" });
  return new Intl.DateTimeFormat("en-CA", opts).format(d).replace(",", "");
}

export function age(seconds: unknown): string {
  if (!isNum(seconds)) return UNAVAILABLE;
  if (seconds < 1) return "<1s";
  if (seconds < 120) return `${seconds.toFixed(0)}s`;
  if (seconds < 7200) return `${(seconds / 60).toFixed(0)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function label(code: unknown): string {
  if (typeof code !== "string" || !code) return "";
  return code.replace(/_/g, " ");
}

export type Tone = "pos" | "neg" | "warn" | "info" | "off";

export function healthTone(h: string | null | undefined): Tone {
  if (!h) return "off";
  if (h === "ok") return "pos";
  if (/disconnect|blocked|not_subscribed|unavailable|error|failed/.test(h)) return "neg";
  return "warn";
}

export function newsTone(state: string | undefined): Tone {
  if (!state) return "off";
  if (state === "NORMAL" || state === "DISABLED") return state === "NORMAL" ? "pos" : "off";
  if (state === "BLACKOUT_ACTIVE" || state === "FLATTEN_REQUIRED") return "neg";
  return "warn";
}

import { useEffect, useRef } from "react";
import {
  createChart, CrosshairMode, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type Time,
  type UTCTimestamp,
} from "lightweight-charts";

export interface BarsPayload {
  root: string; timeframe: string;
  bars: { time: number[]; open: (number | null)[]; high: (number | null)[]; low: (number | null)[]; close: (number | null)[];
    volume: (number | null)[]; vwap: (number | null)[]; ema_fast: (number | null)[]; ema_slow: (number | null)[]; complete: boolean[] };
  forming: { time: number; open: number; high: number; low: number; close: number; volume: number } | null;
  indicator_names: { ema_fast: string; ema_slow: string; vwap: string };
  markers: Marker[];
  levels: { role: string; price: number; order_id: string; quantity: number; side: string }[];
}
export interface Marker { kind: string; time: string | null; bar_time?: string | null; direction?: string | null; reason?: string;
  price?: number | null; side?: string; role?: string; stop?: number | null; target?: number | null; reference?: number | null;
  reward_risk?: number | null; signal_id?: string | null; order_status?: string | null; order_reason?: string | null }

export interface Visible { vwap: boolean; emaFast: boolean; emaSlow: boolean; volume: boolean; signals: boolean; fills: boolean; levels: boolean }

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
const TF_SEC: Record<string, number> = { "1min": 60, "5min": 300, "15min": 900 };

function fmt(tz: string, withDate: boolean) {
  const f = new Intl.DateTimeFormat("en-CA", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false,
    ...(withDate ? { month: "2-digit", day: "2-digit" } : {}) });
  return (t: Time) => f.format(new Date((t as number) * 1000)).replace(",", "");
}

export function toMarkers(markers: Marker[], times: number[], tf: string, show: Visible): SeriesMarker<Time>[] {
  if (!times.length) return [];
  const step = TF_SEC[tf] ?? 300;
  const first = times[0], lastT = times[times.length - 1];
  const out: SeriesMarker<Time>[] = [];
  for (const m of markers) {
    const iso = m.kind === "signal" ? (m.bar_time ?? m.time) : m.time;
    if (!iso) continue;
    const t = Math.floor(new Date(iso).getTime() / 1000 / step) * step;
    if (!Number.isFinite(t) || t < first || t > lastT) continue;
    const long = m.direction === "long" || m.side === "BUY";
    if (m.kind === "signal" && show.signals) {
      out.push({ time: t as UTCTimestamp, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
        color: css("--info") });
    } else if (m.kind === "rejected" && show.signals) {
      out.push({ time: t as UTCTimestamp, position: "aboveBar", shape: "square", color: css("--warn"), size: 0.6 });
    } else if (m.kind === "authorized" && show.signals) {
      out.push({ time: t as UTCTimestamp, position: "aboveBar", shape: "circle", color: css("--info"), size: 0.6 });
    } else if (m.kind === "fill_entry" && show.fills) {
      out.push({ time: t as UTCTimestamp, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
        color: css("--pos"), text: `E ${m.price ?? ""}` });
    } else if (m.kind === "fill_exit" && show.fills) {
      out.push({ time: t as UTCTimestamp, position: "aboveBar", shape: "circle", color: css("--text-2"), text: `X ${m.price ?? ""}` });
    }
  }
  return out.sort((a, b) => (a.time as number) - (b.time as number));
}

export function CandleChart({ data, show, tz, autoScale, live }: { data: BarsPayload | null; show: Visible; tz: string; autoScale: boolean;
  live?: number | null }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const s = useRef<{ candles?: ISeriesApi<"Candlestick">; vol?: ISeriesApi<"Histogram">; vwap?: ISeriesApi<"Line">;
    ef?: ISeriesApi<"Line">; es?: ISeriesApi<"Line">; lines: IPriceLine[] }>({ lines: [] });
  const fitted = useRef<string>("");

  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { color: css("--bg-1") }, textColor: css("--text-2"), fontFamily: css("--mono"), fontSize: 11 },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: css("--line-2") },
      timeScale: { borderColor: css("--line-2"), timeVisible: true, secondsVisible: false },
    });
    chart.current = c;
    s.current = {
      candles: c.addCandlestickSeries({ upColor: css("--pos"), downColor: css("--neg"), borderVisible: false,
        wickUpColor: css("--pos"), wickDownColor: css("--neg") }),
      vol: c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, color: "rgba(100,116,139,.45)" }),
      vwap: c.addLineSeries({ color: "#eab308", lineWidth: 2, priceLineVisible: false, lastValueVisible: false }),
      ef: c.addLineSeries({ color: "#38bdf8", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
      es: c.addLineSeries({ color: "#a78bfa", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
      lines: [],
    };
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    return () => { c.remove(); chart.current = null; };
  }, []);

  useEffect(() => {
    chart.current?.applyOptions({
      localization: { timeFormatter: fmt(tz, true) },
      timeScale: { tickMarkFormatter: fmt(tz, false) },
      rightPriceScale: { autoScale },
    });
  }, [tz, autoScale]);

  useEffect(() => {
    const c = chart.current, ss = s.current;
    if (!c || !ss.candles || !data) return;
    const b = data.bars;
    const candles = [], vol = [], vwap = [], ef = [], es = [];
    for (let i = 0; i < b.time.length; i++) {
      const t = b.time[i] as UTCTimestamp;
      const o = b.open[i], h = b.high[i], l = b.low[i], cl = b.close[i];
      if (o === null || h === null || l === null || cl === null) continue;
      candles.push({ time: t, open: o, high: h, low: l, close: cl });
      vol.push({ time: t, value: b.volume[i] ?? 0, color: cl >= o ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)" });
      if (b.vwap[i] !== null) vwap.push({ time: t, value: b.vwap[i] as number });
      if (b.ema_fast[i] !== null) ef.push({ time: t, value: b.ema_fast[i] as number });
      if (b.ema_slow[i] !== null) es.push({ time: t, value: b.ema_slow[i] as number });
    }
    if (data.forming && data.timeframe === "1min" && (!candles.length || data.forming.time > (candles[candles.length - 1].time as number))) {
      const f = data.forming;
      candles.push({ time: f.time as UTCTimestamp, open: f.open, high: f.high, low: f.low, close: f.close });
    }
    ss.candles.setData(candles);
    ss.vol!.setData(show.volume ? vol : []);
    ss.vwap!.setData(show.vwap ? vwap : []);
    ss.ef!.setData(show.emaFast ? ef : []);
    ss.es!.setData(show.emaSlow ? es : []);
    ss.candles.setMarkers(toMarkers(data.markers, candles.map((x) => x.time as number), data.timeframe, show));
    for (const pl of ss.lines) ss.candles.removePriceLine(pl);
    ss.lines = [];
    if (show.levels) {
      for (const lv of data.levels) {
        const color = lv.role === "stop_loss" ? css("--neg") : lv.role === "take_profit" ? css("--pos") : css("--info");
        ss.lines.push(ss.candles.createPriceLine({ price: lv.price, color, lineWidth: 1, lineStyle: LineStyle.Dashed,
          axisLabelVisible: true, title: `SIM ${lv.role.replace("_", " ").toUpperCase()}` }));
      }
    }
    if (typeof live === "number") {
      ss.lines.push(ss.candles.createPriceLine({ price: live, color: css("--text-3"), lineWidth: 1, lineStyle: LineStyle.Dotted,
        axisLabelVisible: true, title: "last" }));
    }
    const key = `${data.root}/${data.timeframe}`;
    if (fitted.current !== key && candles.length) {
      fitted.current = key;
      c.timeScale().setVisibleLogicalRange({ from: Math.max(0, candles.length - 160), to: candles.length + 4 });
    }
  }, [data, show, live]);

  return <div ref={host} style={{ position: "absolute", inset: 0 }} data-testid="candle-chart" />;
}

export interface LinePoint { time: number; value: number }

export function SeriesChart({ points, kind, tone, tz = "UTC", dateOnly = false }: { points: LinePoint[]; kind: "area" | "line" | "histogram";
  tone: "pos" | "neg" | "info"; tz?: string; dateOnly?: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      layout: { background: { color: css("--bg-1") }, textColor: css("--text-2"), fontFamily: css("--mono"), fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line-2") }, timeScale: { borderColor: css("--line-2"), timeVisible: !dateOnly,
        tickMarkFormatter: dateOnly ? undefined : fmt(tz, true) },
      localization: dateOnly ? undefined : { timeFormatter: fmt(tz, true) },
    });
    const color = css(`--${tone}`);
    const data = dedupe(points).map((p) => ({ time: p.time as UTCTimestamp, value: p.value,
      ...(kind === "histogram" ? { color: p.value >= 0 ? css("--pos") : css("--neg") } : {}) }));
    const series = kind === "area" ? c.addAreaSeries({ lineColor: color, topColor: `${color}55`, bottomColor: `${color}05`, lineWidth: 2 })
      : kind === "histogram" ? c.addHistogramSeries({}) : c.addLineSeries({ color, lineWidth: 2 });
    series.setData(data);
    c.timeScale().fitContent();
    return () => c.remove();
  }, [points, kind, tone, tz, dateOnly]);
  return <div ref={host} style={{ position: "absolute", inset: 0 }} />;
}

/** Lightweight-charts needs strictly increasing times: several trades closing in one second keep the last value. */
export function dedupe(points: LinePoint[]): LinePoint[] {
  const out: LinePoint[] = [];
  for (const p of [...points].sort((a, b) => a.time - b.time)) {
    if (out.length && out[out.length - 1].time === p.time) out[out.length - 1] = p; else out.push(p);
  }
  return out;
}

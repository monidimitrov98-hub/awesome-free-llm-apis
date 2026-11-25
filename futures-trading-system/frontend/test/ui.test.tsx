import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { TerminalContext, type TerminalState } from "../src/state/store";
import { DEFAULT_PREFS, type Prefs } from "../src/state/prefs";
import { API } from "./api-fixtures";
import { snapshot } from "./fixtures";

vi.mock("lightweight-charts", () => {
  const series = () => ({ setData: vi.fn(), setMarkers: vi.fn(), createPriceLine: vi.fn(() => ({})), removePriceLine: vi.fn() });
  const chart = () => ({ addCandlestickSeries: series, addHistogramSeries: series, addLineSeries: series, addAreaSeries: series,
    priceScale: () => ({ applyOptions: vi.fn() }), applyOptions: vi.fn(), remove: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn(), setVisibleLogicalRange: vi.fn() }) });
  return { createChart: vi.fn(chart), CrosshairMode: { Normal: 0 }, LineStyle: { Dashed: 2, Dotted: 1 } };
});

let calls: string[] = [];
let failing: Set<string> = new Set();
beforeEach(() => {
  calls = [];
  failing = new Set();
  window.location.hash = "";
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    const path = url.split("?")[0];
    if (failing.has(path)) return new Response(JSON.stringify({ detail: "internal error" }), { status: 500 });
    return new Response(JSON.stringify(API[path] ?? {}), { status: 200 });
  }));
});

function state(over: Partial<TerminalState> = {}, metaOver = {}): TerminalState {
  const s = snapshot(metaOver);
  return { api: "ok", apiError: null, live: "open", liveInfo: null, lastUpdate: Date.now(), meta: s.meta, market: s.market,
    status: s.status, activity: s.activity, notifications: s.notifications, unread: 1, statusTick: 1, activityTick: 1, ...over };
}

function renderApp(st: TerminalState, prefs: Prefs = DEFAULT_PREFS) {
  let current = prefs;
  const setPrefs = vi.fn((p: Prefs) => { current = p; });
  const utils = render(
    <TerminalContext.Provider value={{ state: st, prefs: current, setPrefs, refresh: vi.fn(), markRead: vi.fn() }}>
      <App />
    </TerminalContext.Provider>);
  return { ...utils, setPrefs };
}

const PAGES = ["Dashboard", "Live Charts", "AI Market Analysis", "Strategy Monitor", "Positions", "Risk Center", "Trade Journal",
  "Performance", "Shadow Analytics", "Prop Firm Center", "Market Data", "System Health", "Settings"];

describe("application shell", () => {
  it("shows the mode unmistakably and labels synthetic data", () => {
    renderApp(state());
    expect(screen.getAllByText("MOCK")[0]).toBeInTheDocument();
    expect(screen.getByText("SYNTHETIC DATA — NOT MARKET PRICES")).toBeInTheDocument();
    expect(screen.getByText("READ-ONLY · NO ORDERS")).toBeInTheDocument();
  });
  it("labels replay and shadow modes distinctly", () => {
    const { unmount } = renderApp(state({}, { mode: "REPLAY", data_label: "HISTORICAL" }));
    expect(screen.getByText("REPLAY")).toBeInTheDocument();
    expect(screen.getByText("HISTORICAL DATA")).toBeInTheDocument();
    unmount();
    renderApp(state({}, { mode: "SHADOW", data_label: "LIVE" }));
    expect(screen.getByText("LIVE DATA")).toBeInTheDocument();
    expect(screen.queryByText("SYNTHETIC DATA — NOT MARKET PRICES")).not.toBeInTheDocument();
  });
  it("every page renders, and no page offers order entry", async () => {
    renderApp(state());
    const nav = screen.getByRole("navigation");
    for (const label of PAGES) {
      await act(async () => { fireEvent.click(within(nav).getByText(label)); });
      await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(label === "Live Charts" ? "Live Charts" : label));
      const buttons = screen.getAllByRole("button").map((b) => b.textContent?.toLowerCase() ?? "");
      expect(buttons.filter((t) => /\b(buy|sell|order|flatten|submit|execute|place)\b/.test(t))).toEqual([]);
    }
  });
  it("shows a persistent warning when the backend is disconnected", () => {
    renderApp(state({ live: "closed", liveInfo: "connection closed" }));
    expect(screen.getByRole("alert", { name: "" })).toBeTruthy();
    expect(screen.getByText(/BACKEND DISCONNECTED/)).toBeInTheDocument();
  });
  it("never hides a degraded feed (API connected but market data unhealthy)", () => {
    const st = state();
    st.market!.instruments.MES.health = "market_data_stale";
    renderApp(st);
    expect(screen.getByText(/MARKET FEED DEGRADED/)).toBeInTheDocument();
    expect(screen.getByText(/MES: market_data_stale/)).toBeInTheDocument();
  });
  it("shows a failed session state", () => {
    renderApp(state({}, { state: "failed", error: "ConfigError: bridge secret file not found" }));
    expect(screen.getByText(/SESSION FAILED/)).toBeInTheDocument();
  });
  it("notification drawer lists backend notifications, filtered by severity preference", () => {
    const { unmount } = renderApp(state());
    fireEvent.click(screen.getByLabelText("Notifications"));
    expect(screen.getByText("Strategy signal rejected")).toBeInTheDocument();
    unmount();
    renderApp(state(), { ...DEFAULT_PREFS, notifications: { enabled: true, minSeverity: "critical" } });
    fireEvent.click(screen.getByLabelText("Notifications"));
    expect(screen.queryByText("Strategy signal rejected")).not.toBeInTheDocument();
  });
  it("sidebar collapses through the preferences", () => {
    const { setPrefs } = renderApp(state());
    fireEvent.click(screen.getByLabelText("Collapse sidebar"));
    expect(setPrefs).toHaveBeenCalledWith(expect.objectContaining({ sidebarCollapsed: true }));
  });
});

describe("pages", () => {
  it("dashboard shows backend values and UNAVAILABLE instead of invented ones", async () => {
    renderApp(state());
    expect(screen.getAllByText("6000.25").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThan(0); // win rate / profit factor with no trades
    expect(screen.getByText(/INSUFFICIENT SAMPLE SIZE/)).toBeInTheDocument();
    expect(screen.getByText(/news_calendar_stale/)).toBeInTheDocument();
  });
  it("risk center shows the incomplete-calendar block and protection timeline", async () => {
    window.location.hash = "/risk";
    renderApp(state());
    await waitFor(() => expect(screen.getByText(/NEWS CALENDAR: CALENDAR STALE/)).toBeInTheDocument());
    expect(screen.getAllByText("BLOCKED").length).toBeGreaterThan(0);
    expect(screen.getByText(/Data health block: not_subscribed/)).toBeInTheDocument();
  });
  it("strategy monitor shows exact rejection codes and authorization stages", async () => {
    window.location.hash = "/strategy";
    renderApp(state());
    await waitFor(() => expect(screen.getAllByText("rsi_not_turning").length).toBeGreaterThan(0));
    expect(screen.getAllByText("news_calendar_stale").length).toBeGreaterThan(0);
  });
  it("trade journal filters are sent to the backend", async () => {
    window.location.hash = "/journal";
    renderApp(state());
    await waitFor(() => expect(screen.getByText("T00001")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Wins & losses"), { target: { value: "win" } });
    await waitFor(() => expect(calls.some((c) => c.startsWith("/api/journal?") && c.includes("outcome=win"))).toBe(true));
  });
  it("errors are shown, never replaced by fake data", async () => {
    failing.add("/api/risk");
    window.location.hash = "/risk";
    renderApp(state());
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Risk state unavailable/));
  });
  it("empty states", async () => {
    window.location.hash = "/positions";
    renderApp(state());
    await waitFor(() => expect(screen.getByText("No open simulated position")).toBeInTheDocument());
    window.location.hash = "/performance";
  });
  it("analysis unavailable is explicit", async () => {
    window.location.hash = "/analysis";
    renderApp(state());
    await waitFor(() => expect(screen.getByText("Analysis unavailable")).toBeInTheDocument());
  });
});

// Sprint 8 Shadow Analytics page: backend values only, session types never combined, hypothetical results
// labelled, no trading controls, errors shown (never replaced by invented data).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { SECTIONS } from "../src/pages/ShadowAnalytics";
import { TerminalContext, type TerminalState } from "../src/state/store";
import { DEFAULT_PREFS } from "../src/state/prefs";
import { API } from "./api-fixtures";
import { snapshot } from "./fixtures";

vi.mock("lightweight-charts", () => ({ createChart: vi.fn(), CrosshairMode: { Normal: 0 }, LineStyle: { Dashed: 2, Dotted: 1 } }));

let calls: string[] = [];
let failing = new Set<string>();
beforeEach(() => {
  calls = [];
  failing = new Set();
  window.location.hash = "/shadow";
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    const path = url.split("?")[0];
    if (failing.has(path)) return new Response(JSON.stringify({ detail: "analytics failed (see the terminal log)" }), { status: 500 });
    return new Response(JSON.stringify(API[path] ?? {}), { status: 200 });
  }));
});

function renderPage() {
  const s = snapshot({});
  const st: TerminalState = { api: "ok", apiError: null, live: "open", liveInfo: null, lastUpdate: Date.now(), meta: s.meta, market: s.market,
    status: s.status, activity: s.activity, notifications: s.notifications, unread: 0, statusTick: 1, activityTick: 1 };
  return render(<TerminalContext.Provider value={{ state: st, prefs: DEFAULT_PREFS, setPrefs: vi.fn(), refresh: vi.fn(), markRead: vi.fn() }}><App /></TerminalContext.Provider>);
}
const section = async (name: string) => {
  const group = screen.getByRole("group", { name: "Section" });
  await act(async () => { fireEvent.click(within(group).getByText(name)); });
};

describe("Shadow Analytics", () => {
  it("lists sessions with their type, provider and strategy identity, and reports excluded journals", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("r1:2025-03-12:MES".split(":")[1])).toBeInTheDocument());
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Shadow Analytics");
    expect(screen.getAllByText("NinjaTrader 8 bridge").length).toBeGreaterThan(0);
    expect(screen.getAllByText("vwap_trend_pullback@abc123def456").length).toBe(2);
    expect(screen.getByRole("alert")).toHaveTextContent(/Excluded journals.*chain broken/);
    expect(screen.getByText(/choose one session type for performance figures/)).toBeInTheDocument();
  });

  it("has all ten sections and never offers a trading control", async () => {
    renderPage();
    expect(SECTIONS.length).toBe(10);
    for (const s of SECTIONS) {
      await section(s.label);
      const buttons = screen.getAllByRole("button").map((b) => b.textContent?.toLowerCase() ?? "");
      expect(buttons.filter((t) => /\b(buy|sell|order|flatten|submit|execute|place|apply|promote)\b/.test(t))).toEqual([]);
    }
  });

  it("refuses to combine session types instead of showing a mixed headline figure", async () => {
    renderPage();
    await section("Performance");
    await waitFor(() => expect(screen.getByText("Choose one session type")).toBeInTheDocument());
    expect(screen.queryByText("Net P&L")).not.toBeInTheDocument();
  });

  it("sends the filters to the backend", async () => {
    renderPage();
    const filters = screen.getByRole("group", { name: "Session type" });
    await act(async () => { fireEvent.click(within(filters).getByText("SHADOW")); });
    fireEvent.change(screen.getByLabelText("MES & MNQ"), { target: { value: "MNQ" } });
    await waitFor(() => expect(calls.some((c) => c.startsWith("/api/shadow/sessions?") && c.includes("session_type=SHADOW") && c.includes("root=MNQ"))).toBe(true));
  });

  it("labels missed-opportunity results as hypothetical and shows exact block reasons", async () => {
    renderPage();
    await section("Blocked Signals");
    await waitFor(() => expect(screen.getByText("HYPOTHETICAL (never traded; not part of any P&L)")).toBeInTheDocument());
    expect(screen.getByText("news_calendar_stale")).toBeInTheDocument();
    expect(screen.getByText("cooldown_active")).toBeInTheDocument();
  });

  it("shows why live and replay are not comparable", async () => {
    renderPage();
    await section("Live vs Replay");
    await waitFor(() => expect(screen.getByText(/strategy_config_sha256 differs/)).toBeInTheDocument());
  });

  it("keeps data-reliability states separate and shows validation warnings", async () => {
    renderPage();
    await section("Data Reliability");
    await waitFor(() => expect(screen.getByText("Bridge connection")).toBeInTheDocument());
    for (const k of ["Provider connected", "Valid events received", "Data fresh", "System operational"]) expect(screen.getByText(k)).toBeInTheDocument();
    await section("Validation");
    await waitFor(() => expect(screen.getByText("journal integrity")).toBeInTheDocument());
    expect(screen.getAllByText(/WARN/).length).toBeGreaterThan(0);
  });

  it("stress rows without a basis are UNAVAILABLE, not invented", async () => {
    renderPage();
    await section("Account Survival");
    await waitFor(() => expect(screen.getByText("no loss basis")).toBeInTheDocument());
  });

  it("shows backend errors", async () => {
    failing.add("/api/shadow/execution");
    renderPage();
    await section("Execution");
    await waitFor(() => expect(screen.getByText(/Execution quality unavailable: analytics failed/)).toBeInTheDocument());
  });
});

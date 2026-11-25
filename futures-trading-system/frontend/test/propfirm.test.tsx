// Sprint 9 Prop Firm Center: backend values only, never a green SAFE for an unverified profile, a zero
// buffer shown as $0.00 (not "Unavailable"), no order / activation / override / payout controls.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { statusTone } from "../src/pages/PropFirm";
import { TerminalContext, type TerminalState } from "../src/state/store";
import { DEFAULT_PREFS } from "../src/state/prefs";
import { API } from "./api-fixtures";
import { snapshot } from "./fixtures";

vi.mock("lightweight-charts", () => ({ createChart: vi.fn(), CrosshairMode: { Normal: 0 }, LineStyle: { Dashed: 2, Dotted: 1 } }));

let calls: string[] = [];
let overrides: Record<string, unknown> = {};
let failing = new Set<string>();
beforeEach(() => {
  calls = []; overrides = {}; failing = new Set();
  window.location.hash = "/propfirm";
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    const path = url.split("?")[0];
    if (failing.has(path)) return new Response(JSON.stringify({ detail: "prop firm view failed (see the terminal log)" }), { status: 500 });
    return new Response(JSON.stringify(overrides[path] ?? API[path] ?? {}), { status: 200 });
  }));
});

function renderPage() {
  const s = snapshot({});
  const st: TerminalState = { api: "ok", apiError: null, live: "open", liveInfo: null, lastUpdate: Date.now(), meta: s.meta, market: s.market,
    status: s.status, activity: s.activity, notifications: s.notifications, unread: 0, statusTick: 1, activityTick: 1 };
  return render(<TerminalContext.Provider value={{ state: st, prefs: DEFAULT_PREFS, setPrefs: vi.fn(), refresh: vi.fn(), markRead: vi.fn() }}><App /></TerminalContext.Provider>);
}
const view = async (name: string) => {
  await act(async () => { fireEvent.click(within(screen.getByRole("group", { name: "Prop firm view" })).getByText(name)); });
};
const forbidden = () => screen.getAllByRole("button").map((b) => b.textContent?.toLowerCase() ?? "")
  .filter((t) => /\b(buy|sell|order|flatten|submit|execute|place|activate|approve|override|payout|withdraw|reset)\b/.test(t));

describe("Prop Firm Center", () => {
  it("never shows SAFE in green for an unverified profile and explains why it is not operational", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("NOT OPERATIONAL: UNVERIFIED")).toBeInTheDocument());
    expect(screen.getAllByText("UNVERIFIED")[0].className).not.toContain("pos");
    expect(screen.queryByText("SAFE")).not.toBeInTheDocument();
    expect(screen.getByText("FNF-FLEX50-MLL-EOD, FNF-FLEX50-HOLIDAYS")).toBeInTheDocument();
    expect(statusTone("SAFE")).toBe("pos");
    for (const s of ["UNVERIFIED", "WARNING", "BLOCKED", "DATA UNAVAILABLE"]) expect(statusTone(s)).not.toBe("pos");
  });

  it("shows a zero official buffer as $0.00, the breach, exact block reasons and the hierarchy", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Official buffer")).toBeInTheDocument());
    const stat = screen.getByText("Official buffer").closest(".stat") as HTMLElement;
    expect(within(stat).getByText("$0.00")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/BREACHED/);
    expect(screen.getAllByText("prop_profile_not_operational").length).toBeGreaterThan(0);
    expect(screen.getByText("Safety hierarchy (highest first)")).toBeInTheDocument();
    expect(screen.getByText("Safety reserve")).toBeInTheDocument();
    expect(screen.getByText("Uncertainty")).toBeInTheDocument();
    expect(forbidden()).toEqual([]);
  });

  it("every view is read-only", async () => {
    renderPage();
    for (const v of ["Rules & sources", "Profiles", "Compare versions", "History", "Account compliance"]) {
      await view(v);
      expect(forbidden()).toEqual([]);
    }
  });

  it("lists sources, verification state and unresolved points", async () => {
    renderPage();
    await view("Rules & sources");
    await waitFor(() => expect(screen.getByText("excerpt cut off")).toBeInTheDocument());
    expect(screen.getAllByText("FundedNext Futures Flex Challenge All Rules").length).toBeGreaterThan(0);
    expect(screen.getByText("NOT REVIEWED")).toBeInTheDocument();
    expect(screen.getByText("Internal limits (our own margins — not firm rules)")).toBeInTheDocument();
  });

  it("flags potentially unsafe rule changes and requires a human", async () => {
    renderPage();
    await view("Compare versions");
    await waitFor(() => expect(screen.getByLabelText("Older version")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Older version"), { target: { value: "test_firm@2" } });
    fireEvent.change(screen.getByLabelText("Newer version"), { target: { value: "test_firm@2" } });
    await waitFor(() => expect(screen.getByText("X-MLL: amount 1500 -> 2000 (looser)")).toBeInTheDocument());
    expect(calls.some((c) => c.startsWith("/api/propfirm/compare?a=test_firm%402"))).toBe(true);
    expect(screen.getByText(/Activation always requires a human/)).toBeInTheDocument();
  });

  it("shows when no profile is selected", async () => {
    overrides["/api/propfirm/status"] = { selected: false, note: "No prop firm profile is selected for this session" };
    renderPage();
    await waitFor(() => expect(screen.getByText("Prop firm engine off")).toBeInTheDocument());
  });

  it("shows backend errors instead of inventing values", async () => {
    failing.add("/api/propfirm/status");
    renderPage();
    await waitFor(() => expect(screen.getByText(/Prop firm status unavailable: prop firm view failed/)).toBeInTheDocument());
  });
});

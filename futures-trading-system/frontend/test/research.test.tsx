// Sprint 11 Quant Research page: read-only, never a green "approved/profitable" signal, failures and
// integrity problems visible, compliance coverage shown, no control to run, reveal, promote or trade.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { evidenceTone, stateTone } from "../src/pages/Research";
import { TerminalContext, type TerminalState } from "../src/state/store";
import { DEFAULT_PREFS } from "../src/state/prefs";
import { API } from "./api-fixtures";
import { snapshot } from "./fixtures";

vi.mock("lightweight-charts", () => ({ createChart: vi.fn(), CrosshairMode: { Normal: 0 }, LineStyle: { Dashed: 2, Dotted: 1 } }));

let calls: string[] = [];
beforeEach(() => {
  calls = [];
  window.location.hash = "/research";
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(url);
    return new Response(JSON.stringify(API[url.split("?")[0]] ?? {}), { status: 200 });
  }));
});

function renderPage() {
  const s = snapshot({});
  const st: TerminalState = { api: "ok", apiError: null, live: "open", liveInfo: null, lastUpdate: Date.now(), meta: s.meta, market: s.market,
    status: s.status, activity: s.activity, notifications: s.notifications, unread: 0, statusTick: 1, activityTick: 1 };
  return render(<TerminalContext.Provider value={{ state: st, prefs: DEFAULT_PREFS, setPrefs: vi.fn(), refresh: vi.fn(), markRead: vi.fn() }}><App /></TerminalContext.Provider>);
}
const view = async (name: string) => {
  await act(async () => { fireEvent.click(within(screen.getByRole("group", { name: "Research view" })).getByText(name)); });
};

describe("Quant Research", () => {
  it("is read-only and states the limitations", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Quant Research"));
    expect(screen.getByText("READ-ONLY · RESEARCH SIMULATION")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/original confirmation and stop rules are not known/)).toBeInTheDocument());
    const buttons = screen.getAllByRole("button").map((b) => b.textContent?.toLowerCase() ?? "");
    expect(buttons.filter((t) => /\b(buy|sell|order|run|reveal|promote|approve|register|activate|execute|submit)\b/.test(t))).toEqual([]);
    expect(calls.every((c) => c.startsWith("/api/"))).toBe(true);
  });

  it("never colours evidence or promotion states green", () => {
    for (const e of ["POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE", "INCONCLUSIVE", "INSUFFICIENT EVIDENCE", "NEGATIVE EXPECTANCY IN THIS SAMPLE"])
      expect(evidenceTone(e)).not.toBe("pos");
    for (const s of ["RESEARCH", "BACKTESTED", "OOS_VALIDATED", "SHADOW_APPROVED", "PAPER_APPROVED", "REJECTED", "SUSPENDED"])
      expect(stateTone(s)).not.toBe("pos");
  });

  it("shows runs with evidence and compliance coverage, and the run detail with integrity and Monte Carlo", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("Research runs (newest first; not ranked)")).toBeInTheDocument());
    expect(screen.getAllByText("INSUFFICIENT EVIDENCE").length).toBeGreaterThan(0);
    expect(screen.getAllByText("INCOMPLETE -- RESEARCH ONLY").length).toBeGreaterThan(0);
    await act(async () => { fireEvent.click(screen.getByText("vwap_mean_reversion@V1-C2-SWING")); });
    await waitFor(() => expect(screen.getByText("MANIFEST VERIFIED")).toBeInTheDocument());
    expect(calls).toContain("/api/research/run?attempt=0123456789abcdef");
    expect(screen.getByText("FNF-FLEX50-HOLIDAYS")).toBeInTheDocument();
    expect(screen.getByText(/UNTRUSTED: working tree has uncommitted changes/)).toBeInTheDocument();
    expect(screen.getByText("Monte Carlo (resampled historical trades; not a forecast)")).toBeInTheDocument();
    expect(screen.getByText(/^33\.3% \[6\.0%, 79\.0%\]$/)).toBeInTheDocument();   // fractions shown as percentages
  });

  it("shows modified versions, store integrity failures and failed experiments", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByRole("group", { name: "Research view" })).toBeInTheDocument());
    await view("Versions & promotion");
    expect(screen.getByText("MODIFIED_AFTER_REGISTRATION")).toBeInTheDocument();
    expect(screen.getByText("INTEGRITY FAILURE")).toBeInTheDocument();
    expect(screen.getAllByText(/there is no live state/).length).toBeGreaterThan(1);
    await view("Experiments");
    expect(screen.getByText(/1 failed or refused/)).toBeInTheDocument();
    expect(screen.getByText("ConfigError: no data")).toBeInTheDocument();
    await view("Out-of-sample ledger");
    expect(screen.getByText("No out-of-sample reveal recorded")).toBeInTheDocument();
  });
});

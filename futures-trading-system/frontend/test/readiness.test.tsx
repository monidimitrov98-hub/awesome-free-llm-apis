// Sprint 10 readiness panel: backend verdict only; never "SAFE"; incomplete evidence is visible.
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Readiness } from "../src/pages/Dashboard";
import { API } from "./api-fixtures";

let body: unknown = API["/api/readiness"];
let status = 200;
beforeEach(() => {
  body = API["/api/readiness"];
  status = 200;
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
});

describe("Operational readiness", () => {
  it("shows the backend verdict and every check, never SAFE", async () => {
    render(<Readiness tick={1} />);
    await waitFor(() => expect(screen.getByText("INCOMPLETE")).toBeInTheDocument());
    expect(screen.getByText(/UNVERIFIED \(not operational/)).toBeInTheDocument();
    expect(screen.getByText("production strategy only: WARN")).toBeInTheDocument();
    expect(screen.queryByText("SAFE")).not.toBeInTheDocument();
  });
  it("a blocked simulation says so", async () => {
    body = { ...(API["/api/readiness"] as object), verdict: "BLOCKED", continue_simulation: false,
      continue_reason: "the simulation is stopped or its state cannot be trusted",
      checks: [{ check: "reconciliation", state: "BLOCK", detail: "state does not reconcile: entries blocked" }] };
    render(<Readiness tick={1} />);
    await waitFor(() => expect(screen.getByText("BLOCKED")).toBeInTheDocument());
    expect(screen.getByText(/Do not rely on this simulation/)).toBeInTheDocument();
  });
  it("an unavailable readiness endpoint is UNKNOWN, not fine", async () => {
    status = 500;
    body = { detail: "prop firm view failed" };
    render(<Readiness tick={1} />);
    await waitFor(() => expect(screen.getByText("UNKNOWN")).toBeInTheDocument());
  });
});

// End-to-end GUI test (Playwright, Chromium): real backend -> REST/WebSocket -> React UI.
// Run by tests/test_terminal_e2e.py:  node frontend/e2e/terminal.e2e.cjs PORT TOKEN SCENARIO
const { chromium } = require("playwright");

const [port, token, scenario] = process.argv.slice(2);
const base = `http://127.0.0.1:${port}`;

async function expectText(page, text, timeout = 60000) {
  await page.getByText(text, { exact: false }).first().waitFor({ timeout });
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  try {
    await page.goto(`${base}/#token=${token}`);
    if (!(await page.evaluate(() => window.location.hash === ""))) throw new Error("token was not removed from the URL");
    if (scenario === "replay") {
      await expectText(page, "REPLAY");
      await expectText(page, "SYNTHETIC DATA — NOT MARKET PRICES");
      // a real backend rejection reason (regime gate / trading window / news) reaches the UI
      await page.evaluate(() => { window.location.hash = "/strategy"; });
      await expectText(page, "Entry authorization decisions");
      await page.getByText(/regime_|outside_trading_window|news_/).first().waitFor({ timeout: 120000 });
    } else if (scenario === "replay-nogate") {
      await page.evaluate(() => { window.location.hash = "/charts"; });
      await expectText(page, "authorized paper order", 120000);     // signal -> authorization shown with the chart
      await page.evaluate(() => { window.location.hash = "/journal"; });
      await expectText(page, "T00001", 120000);                      // simulated trade in the journal
      await page.evaluate(() => { window.location.hash = "/dashboard"; });
      await expectText(page, "SIMULATED fill");                       // activity feed
      await page.evaluate(() => { window.location.hash = "/risk"; });
      await expectText(page, "AccountGuard");
      await expectText(page, "allowed by news");                      // research NewsGuard state visible
    } else if (scenario === "mock") {
      await expectText(page, "MOCK");
      await page.evaluate(() => { window.location.hash = "/market"; });
      await expectText(page, "MOCK bridge (synthetic data)");
      await page.evaluate(() => { window.location.hash = "/risk"; });
      await expectText(page, "NEWS CALENDAR:");                       // incomplete calendar blocks entries, visibly
    } else if (scenario === "shadow-analytics") {
      await page.evaluate(() => { window.location.hash = "/shadow"; });
      await expectText(page, "Shadow Analytics");
      await page.locator("table").getByText("REPLAY").first().waitFor({ timeout: 120000 });  // the run's sessions
      await expectText(page, "e2e_test_strategy@");                    // strategy identity per session
      const section = (name) => page.getByRole("group", { name: "Section" }).getByText(name, { exact: true }).click();
      await section("Performance");
      await expectText(page, "SIMULATED results of recorded sessions", 120000);
      await expectText(page, "INSUFFICIENT SAMPLE SIZE");               // small samples are flagged
      await expectText(page, "RESEARCH SCENARIOS");                     // cost scenarios kept separate
      await section("Blocked Signals");
      await expectText(page, "HYPOTHETICAL (never traded; not part of any P&L)");
      await section("Data Reliability");
      await expectText(page, "Valid events received");
      await section("Validation");
      await expectText(page, "production strategy only");              // the TEST strategy is flagged
      await section("Live vs Replay");
      await expectText(page, "no SHADOW observations for MES");         // nothing compared without shadow data
    } else if (scenario === "prop-firm") {
      await page.evaluate(() => { window.location.hash = "/propfirm"; });
      await expectText(page, "Prop Firm Center");
      await expectText(page, "FundedNext Futures — Futures Flex");
      await expectText(page, "NOT OPERATIONAL: UNVERIFIED");          // never presented as operational
      await expectText(page, "FNF-FLEX50-MLL-EOD");                   // unresolved safety-critical rule listed
      await expectText(page, "Official floor");
      if (await page.getByText("SAFE", { exact: true }).count()) throw new Error("an unverified profile was shown as SAFE");
      const v = (name) => page.getByRole("group", { name: "Prop firm view" }).getByText(name, { exact: true }).click();
      await v("Rules & sources");
      await expectText(page, "helpfutures.fundednext.com");            // official sources shown
      await expectText(page, "NOT REVIEWED");
      await v("History");
      await page.getByText("prop_profile_not_operational").first().waitFor({ timeout: 120000 });  // real blocks
      await page.evaluate(() => { window.location.hash = "/dashboard"; });        // Sprint 10 readiness verdict
      await expectText(page, "Operational readiness");
      await page.getByText(/^(INCOMPLETE|BLOCKED)$/).first().waitFor({ timeout: 60000 });
      if (await page.getByText("OPERATIONAL (SIMULATION)", { exact: true }).count()) throw new Error("unverified profile shown as operational");
    } else if (scenario === "research") {                               // Sprint 11 Quant Research (read-only)
      await page.evaluate(() => { window.location.hash = "/research"; });
      await expectText(page, "Quant Research");
      await expectText(page, "READ-ONLY · RESEARCH SIMULATION");
      await expectText(page, "original confirmation and stop rules are not known");
      await expectText(page, "vwap_mean_reversion@V1-C2-SWING");
      await expectText(page, "INSUFFICIENT EVIDENCE");                 // synthetic data never claims an edge
      await expectText(page, "INCOMPLETE -- RESEARCH ONLY");           // unverified profile: coverage incomplete
      await page.getByText("vwap_mean_reversion@V1-C2-SWING").first().click();
      await expectText(page, "MANIFEST VERIFIED");
      await expectText(page, "Prop firm compliance coverage");
      await expectText(page, "FNF-FLEX50-MLL-EOD");
      const v = (name) => page.getByRole("group", { name: "Research view" }).getByText(name, { exact: true }).click();
      await v("Versions & promotion");
      await expectText(page, "V1-C4-SWING");
      await expectText(page, "there is no live state");
      await v("Experiments");
      await expectText(page, "Every attempt counts in the multiple-testing family");
      await v("Out-of-sample ledger");
      await expectText(page, "No out-of-sample reveal recorded");
    } else if (scenario === "mock-fail") {
      await expectText(page, "MOCK");
      await expectText(page, "MARKET FEED DEGRADED", 60000);          // degraded feed is never hidden
    }
    const buttons = await page.getByRole("button").allTextContents();
    const forbidden = buttons.filter((t) => /\b(buy|sell|order|flatten|submit|execute|apply|promote|activate|approve|override|withdraw)\b/i.test(t));
    if (forbidden.length) throw new Error(`order-like controls found: ${forbidden}`);
    if (errors.length) throw new Error(`page errors: ${errors.join("; ")}`);
    console.log(`E2E ${scenario}: PASS`);
  } catch (e) {
    await page.screenshot({ path: `/tmp/fts-e2e-${scenario}-failure.png` }).catch(() => {});
    console.error(`E2E ${scenario}: FAIL ${e}`);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();

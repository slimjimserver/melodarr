import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "@playwright/test";
import { createFixtureServer } from "../tests/fixture-server.mjs";
import {
  configuration,
  installMilestones,
  runJourney,
} from "./artist-request-benchmark.mjs";
import { NETWORK_PROFILES } from "./network-metrics.mjs";

// Real CDP interception tests, deliberately separate from the fast browser suite.
// The only server involved is the isolated fixture with no acquisition handler.
for (const allowRequest of [false, true]) {
  test(`browser guard prevents unapproved HTTP submissions (allow=${allowRequest})`, async () => {
    const server = createFixtureServer({ benchmark: true });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    let browser;
    try {
      const config = {
        ...configuration({}, true),
        allowRequest,
        url: `http://127.0.0.1:${server.address().port}/`,
        timeoutMs: 10_000,
      };
      browser = await chromium.launch();
      const context = await browser.newContext({
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
        serviceWorkers: "block",
      });
      context.setDefaultTimeout(config.timeoutMs);
      await installMilestones(context, config);
      await context.addInitScript((allow) => {
        const unwanted = () => {
          void fetch("/api/request/release-group", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              mbid: allow ? "arbitrary-id" : "fixture-album",
            }),
          }).catch(() => {});
        };
        if (allow) {
          // Wrong MBID during the armed request click; the configured request
          // from the real UI may proceed, but this extra call must be blocked.
          document.addEventListener(
            "click",
            (event) => {
              if (event.target.closest("button.release-group-request"))
                unwanted();
            },
            true,
          );
        } else {
          window.addEventListener("melodarr-authenticated", unwanted);
        }
      }, allowRequest);
      const page = await context.newPage();
      const cdp = await context.newCDPSession(page);
      await cdp.send("Network.enable");
      await cdp.send("Network.setBlockedURLs", {
        urls: ["*://*/api/discover*"],
      });
      const run = await runJourney(
        page,
        cdp,
        config,
        NETWORK_PROFILES[0],
        "cold",
        1,
      );
      assert.equal(run.status, "failed");
      assert.match(run.error, /blocked an unapproved acquisition request/);
      const state = await (
        await fetch(`${config.url}__benchmark-state`)
      ).json();
      assert.equal(state.requests, allowRequest ? 1 : 0);
      assert.ok(
        run.observedNetwork.inventory.some(
          (row) => row.method === "POST" && row.state === "failed",
        ),
      );
    } finally {
      await browser?.close();
      await new Promise((resolve) => server.close(resolve));
    }
  });
}

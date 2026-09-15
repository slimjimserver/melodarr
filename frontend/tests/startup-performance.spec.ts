import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("/__reset");
});

async function signIn(page: import("@playwright/test").Page) {
  const form = page.locator("#login-form");
  await form.getByLabel("Username").fill("ada");
  await form.getByLabel("Password").fill("fixture-password");
  await form.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("body")).toHaveClass(/authenticated/);
}

test("Library startup defers discovery and full settings payloads", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", request => requests.push(new URL(request.url()).pathname));
  await page.route("**/api/library", route => route.fulfill({ json: {
    artists: [{
      name: "Fixture Artist",
      sortName: "Fixture Artist",
      section: "Music",
      musicbrainzId: "fixture-artist",
    }],
    artistCount: 1,
    releaseGroupCount: 0,
  } }));
  await page.goto("/library");

  await signIn(page);
  await expect(page.locator("#library")).toHaveClass(/active/);
  await expect(page.locator("#library-results")).not.toHaveAttribute("aria-busy", "true");

  expect(requests.filter(path => path === "/static/discovery.js")).toHaveLength(0);
  expect(requests.filter(path => path === "/api/discover")).toHaveLength(0);
  expect(requests.filter(path => path === "/api/settings")).toHaveLength(0);
  expect(requests.filter(path => path === "/api/health")).toHaveLength(1);

  const discovery = page.waitForRequest(request => (
    new URL(request.url()).pathname === "/static/discovery.js"
  ));
  const detail = page.waitForRequest(request => (
    new URL(request.url()).pathname === "/api/music/artist/fixture-artist"
  ));
  await page.getByRole("link", { name: /Fixture Artist/ }).click();
  await Promise.all([discovery, detail]);
  await expect(page.locator("#detail-title")).toHaveText("Fixture Artist");
  expect(requests.filter(path => path === "/static/discovery.js")).toHaveLength(1);
});

test("Settings startup does not initialize discovery", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", request => requests.push(new URL(request.url()).pathname));
  await page.goto("/settings");

  await signIn(page);
  await expect(page.locator("#settings")).toHaveClass(/active/);
  await expect.poll(() => requests.filter(path => path === "/api/settings").length).toBe(1);

  expect(requests.filter(path => path === "/static/discovery.js")).toHaveLength(0);
  expect(requests.filter(path => path === "/api/discover")).toHaveLength(0);
  expect(requests.filter(path => path === "/api/settings")).toHaveLength(1);
  expect(requests.filter(path => path === "/api/settings/notifications")).toHaveLength(0);
  expect(requests.filter(path => path === "/api/health")).toHaveLength(0);

  const notifications = page.waitForRequest(request => (
    new URL(request.url()).pathname === "/api/settings/notifications"
  ));
  await page.getByRole("tab", { name: "Notifications" }).click();
  await notifications;
});

test("overlapping discovery navigations load and initialize the bundle once", async ({ page }) => {
  await page.addInitScript(() => {
    const trackedWindow = window as Window & {
      __discoveryInitializations?: number;
      __discoveryScriptExecutions?: number;
    };
    trackedWindow.__discoveryInitializations = 0;
    trackedWindow.__discoveryScriptExecutions = 0;
    window.addEventListener("melodarr-authenticated", () => {
      trackedWindow.__discoveryInitializations = (
        trackedWindow.__discoveryInitializations || 0
      ) + 1;
    });
  });
  await page.route("**/api/library", route => route.fulfill({ json: {
    artists: [{
      name: "Fixture Artist",
      sortName: "Fixture Artist",
      section: "Music",
      musicbrainzId: "fixture-artist",
    }],
    artistCount: 1,
    releaseGroupCount: 0,
  } }));

  let releaseDiscovery!: () => void;
  const discoveryGate = new Promise<void>(resolve => { releaseDiscovery = resolve; });
  let discoveryRequested!: () => void;
  const firstDiscoveryRequest = new Promise<void>(resolve => {
    discoveryRequested = resolve;
  });
  let discoveryRequests = 0;
  await page.route("**/static/discovery.js", async route => {
    discoveryRequests += 1;
    const response = await route.fetch();
    discoveryRequested();
    await discoveryGate;
    const source = await response.text();
    await route.fulfill({
      response,
      body: `window.__discoveryScriptExecutions += 1;\n${source}`,
    });
  });

  await page.goto("/library");
  await signIn(page);
  await expect(page.getByRole("link", { name: /Fixture Artist/ })).toBeVisible();

  await page.getByRole("link", { name: /Fixture Artist/ }).click();
  await firstDiscoveryRequest;
  await page.locator('header [data-view="discover"]').click();
  releaseDiscovery();

  await expect(page.locator("#detail-title")).toHaveText("Fixture Artist");
  await expect.poll(() => page.evaluate(() => {
    const trackedWindow = window as Window & {
      __discoveryInitializations?: number;
      __discoveryScriptExecutions?: number;
    };
    return {
      initializations: trackedWindow.__discoveryInitializations,
      executions: trackedWindow.__discoveryScriptExecutions,
    };
  })).toEqual({ initializations: 1, executions: 1 });
  expect(discoveryRequests).toBe(1);
});

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request, page }) => {
  await page.route("**/api/settings", route => route.fulfill({ json: { lidarr: {}, plex: {} } }));
  await request.post("/__reset");
  await request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
});

test("library survives direct navigation, refresh, and browser history", async ({ page }) => {
  const discoveryRequests: string[] = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/static/discovery.js") {
      discoveryRequests.push(request.url());
    }
  });
  await page.addInitScript(() => {
    const trackedWindow = window as Window & { __discoveryInitializations?: number };
    trackedWindow.__discoveryInitializations = 0;
    window.addEventListener("melodarr-authenticated", () => {
      trackedWindow.__discoveryInitializations = (
        trackedWindow.__discoveryInitializations || 0
      ) + 1;
    });
  });
  await page.goto("/library");
  await expect(page.locator("#library-copy")).toContainText("0 artists available");
  await expect(page.locator("#library")).toBeVisible();
  await page.reload();
  await expect(page.locator("#library")).toBeVisible();
  await expect(page).toHaveURL(/\/library$/);
  await page.locator('header [data-view="discover"]').click();
  await expect(page.locator("#discover")).toBeVisible();
  await page.goBack();
  await expect(page.locator("#library")).toBeVisible();
  await page.goForward();
  await expect(page.locator("#discover")).toBeVisible();
  expect(discoveryRequests).toHaveLength(1);
  expect(await page.evaluate(() => (
    window as Window & { __discoveryInitializations?: number }
  ).__discoveryInitializations)).toBe(1);
});

test("versioned Plex images load after filtering and refresh", async ({ page }) => {
  await page.route("**/api/library", route => route.fulfill({ json: {
    artists: [{ name: "Fixture Artist", section: "Music", artwork: "/api/artwork/plex-artist/100?v=revision" }],
    artistCount: 1, releaseGroupCount: 0,
  } }));
  await page.route("**/api/artwork/plex-artist/**", route => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("v")).toBe("revision");
    expect(url.searchParams.get("size")).toBe("card");
    return route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="red"/></svg>' });
  });
  await page.goto("/library");
  const image = page.locator(".library-artwork img");
  const loaded = () => image.evaluateAll(images => images.filter(image => (image as HTMLImageElement).naturalWidth > 0).length);
  await expect.poll(loaded).toBe(1);
  await page.locator("#library-search").fill("missing");
  await expect(image).toHaveCount(0);
  await page.locator("#library-search").fill("Fixture");
  await expect.poll(loaded).toBe(1);
  await page.reload();
  await expect.poll(loaded).toBe(1);
});


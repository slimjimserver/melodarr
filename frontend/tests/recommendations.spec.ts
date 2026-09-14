import { expect, test, type Page } from "@playwright/test";
import { resolve } from "node:path";

const sections = [
  ["familiar", "More from artists you love"],
  ["requests", "Because you requested…"],
  ["discovery", "Try something new"],
];

async function fixture(page: Page) {
  const events: { id: string; kind: string; action: string }[] = [];
  const preferences = new Map<string, string>();
  let refreshes = 0;
  let taste = { mode: "balanced", starterArtists: [] as { id: string; name: string }[] };
  await page.route("**/api/discover/preferences", async route => {
    if (route.request().method() === "POST") taste = route.request().postDataJSON();
    await route.fulfill({ json: { ...taste, message: "Taste preferences saved. Your picks are being refreshed." } });
  });
  await page.route("**/api/search?type=artist&*", route => {
    const name = new URL(route.request().url()).searchParams.get("q") || "Artist";
    const index = Number(name.match(/\d+/)?.[0] || 1);
    return route.fulfill({ json: { results: [{ id: `11111111-1111-4111-8111-${String(index).padStart(12, "0")}`, name }] } });
  });
  await page.route("**/api/settings", (route) => route.fulfill({ json: { lidarr: {}, plex: {} } }));
  await page.route("**/api/discover", (route) => route.fulfill({ json: {
    feedVersion: 5, refreshedAt: 1788652800 + refreshes, requestStatus: "ok", tastePreferences: taste,
    sections: sections.map(([id, title]) => ({
      id, title, description: "Albums selected from your requests and listening.",
      items: Array.from({ length: 6 }, (_, i) => ({
        id: `${id}-${i}`, kind: "release-group", name: `${id} album ${i + 1}`,
        artist: `Favorite artist ${i + 1}`, type: "Album", lane: id,
        reason: "Because you requested a favorite album", feedback: preferences.get(`${id}-${i}`),
        listenUrl: "https://www.last.fm/music/Favorite/Album",
      })).filter((item) => preferences.get(item.id) !== "dismiss"),
    })),
    chartArtists: [{ id: "popular", name: "Chart Artist" }],
  } }));
  await page.route("**/api/discover/activity", async (route) => {
    const batch = route.request().postDataJSON().events;
    events.push(...batch);
    for (const event of batch) {
      if (event.action === "undo") preferences.delete(event.id);
      else if (["more", "dismiss"].includes(event.action)) preferences.set(event.id, event.action);
    }
    await route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/discover/refresh", async (route) => {
    refreshes++;
    await route.fulfill({ status: 202, json: { message: "Refreshing your picks." } });
  });
  await page.route("**/api/discover/metrics", (route) => route.fulfill({ json: {
    shown: 6, opened: 2, listeningLinksOpened: 1, requested: 1, played: null,
  } }));
  await page.route("**/api/request/release-group", (route) => route.fulfill({ json: {
    pending: true, message: "Album queued",
  } }));
  return { events, preferences };
}

async function signIn(page: Page, username = "ada") {
  await page.locator("#login-form").getByLabel("Username").fill(username);
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("body")).toHaveClass(/authenticated/);
  await expect(page.locator("[data-personal-recommendation]")).toHaveCount(18);
}

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });

test("homepage explains album picks and separates global charts", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await signIn(page);
  for (const [, title] of sections) await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
  const card = page.locator("[data-personal-recommendation]").first();
  await expect(card.locator(".recommendation-reason")).toHaveText("Because you requested a favorite album");
  await expect(card.getByRole("link", { name: /Listen|Find listening options/ })).toHaveCount(0);
  await expect(card.locator(".recommendation-source")).toHaveCount(0);
  await expect(page.getByText("Chart Artist", { exact: true })).toHaveCount(0);
  await page.getByText("Browse popular music", { exact: true }).click();
  await expect(page.getByText("Chart Artist", { exact: true })).toBeVisible();
  await page.getByText("Your recommendation activity", { exact: true }).click();
  await expect(page.getByText(/Plex listening outcomes are not available yet/)).toBeVisible();
  await page.locator("#recommendations-title").scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(__dirname, "../../.venv-recommendations/homepage-desktop.jpg"), type: "jpeg", quality: 65 });
});

test("feedback persists through reload and dismissal can be undone", async ({ page }) => {
  const { events } = await fixture(page);
  await page.goto("/");
  await signIn(page);
  let card = page.locator('[data-item-id="familiar-0"]');
  await card.getByRole("button", { name: "More like this" }).click();
  await expect(card.getByRole("button", { name: "Undo preference" })).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  card = page.locator('[data-item-id="familiar-0"]');
  await expect(card.getByRole("button", { name: "Undo preference" })).toBeVisible();
  await card.getByRole("button", { name: "Not interested" }).click();
  await expect(card.getByText("familiar album 1 hidden")).toBeVisible();
  await card.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(card.getByRole("button", { name: "Request album" })).toBeVisible();
  expect(events.map((event) => event.action)).toEqual(expect.arrayContaining(["more", "dismiss", "undo"]));
});

test("offscreen carousel cards are not recorded as impressions", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { events } = await fixture(page);
  await page.goto("/");
  await signIn(page);
  await page.locator('[data-item-id="familiar-0"]').scrollIntoViewIfNeeded();
  await expect.poll(() => events.some((event) => event.id === "familiar-0" && event.action === "impression")).toBeTruthy();
  expect(events.some((event) => event.id === "familiar-5" && event.action === "impression")).toBeFalsy();
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
  await page.screenshot({ path: resolve(__dirname, "../../.venv-recommendations/homepage-mobile.jpg"), type: "jpeg", quality: 65 });
});

test("album requests use the existing request flow and refresh queues work", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await signIn(page);
  const card = page.locator('[data-item-id="familiar-0"]');
  const sent = page.waitForRequest("**/api/request/release-group");
  await card.getByRole("button", { name: "Request album" }).click();
  expect((await sent).postDataJSON().mbid).toBe("familiar-0");
  await expect(card.getByRole("button", { name: "Queued", exact: true })).toBeDisabled();
  const refresh = page.waitForRequest("**/api/discover/refresh");
  await page.getByRole("button", { name: "Refresh picks" }).click();
  expect((await refresh).method()).toBe("POST");
  await expect(page.locator("#recommendations-message")).toHaveText("Refreshing your picks.");
});

test("failed feedback keeps the card usable and reports the error", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/discover/activity", (route) => route.fulfill({ status: 503, json: { error: "Please try again" } }));
  await page.goto("/");
  await signIn(page);
  const card = page.locator('[data-item-id="familiar-0"]');
  await card.getByRole("button", { name: "Not interested" }).click();
  await expect(card.getByRole("button", { name: "Not interested" })).toBeEnabled();
  await expect(card.getByRole("button", { name: "Request album" })).toBeVisible();
  await expect(page.getByText("Please try again", { exact: true })).toBeVisible();
});

test("delayed feedback cannot change the next account's cards", async ({ page }) => {
  await fixture(page);
  let release: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/discover/activity", async (route) => {
    if (route.request().postDataJSON().events[0].action === "dismiss") await gate;
    await route.fulfill({ json: { ok: true } }).catch(() => {});
  });
  await page.goto("/");
  await signIn(page);
  const sent = page.waitForRequest((request) => request.url().endsWith("/api/discover/activity") && request.postDataJSON().events[0].action === "dismiss");
  await page.locator('[data-item-id="familiar-0"]').getByRole("button", { name: "Not interested" }).click();
  await sent;
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, "bea");
  release!();
  await expect(page.locator('[data-item-id="familiar-0"]').getByRole("button", { name: "Request album" })).toBeVisible();
  await expect(page.getByText("familiar album 1 hidden")).toHaveCount(0);
});


test("popular albums keep chart ranks, filter recent releases, and request matched albums", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await fixture(page);
  await page.route("**/api/discover", (route) => route.fulfill({ json: {
    feedVersion: 5, refreshedAt: 1788652800, sections: [],
    popularChart: { updated: "Sun, 6 Sep 2026 16:00:00 +0000", status: "ok" },
    popularAlbums: [
      { id: "old-chart", kind: "release-group", name: "An older hit", artist: "Artist", chartRank: 1, recentRelease: false, reason: "#1 on Apple Music’s US Top 100 albums" },
      { id: "new-chart", kind: "release-group", name: "A new hit", artist: "Artist", chartRank: 8, recentRelease: true, reason: "#8 on Apple Music’s US Top 100 albums" },
    ],
  } }));
  await page.goto("/");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Popular albums right now" })).toBeVisible();
  await expect(page.getByText("A new hit", { exact: true })).toBeVisible();
  await expect(page.getByText("An older hit", { exact: true })).toHaveCount(0);
  await expect(page.getByText("#8 on Apple Music’s US Top 100 albums")).toBeVisible();
  await page.getByLabel("Popular album selection").selectOption("all");
  await expect(page.getByText("An older hit", { exact: true })).toBeVisible();
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
  await page.getByRole("heading", { name: "Popular albums right now" }).scrollIntoViewIfNeeded();
  await page.locator(".popular-albums").screenshot({ path: resolve(__dirname, "../../.venv-recommendations/popular-albums-mobile.jpg"), type: "jpeg", quality: 65 });
  const request = page.waitForRequest("**/api/request/release-group");
  await page.locator('[data-item-id="new-chart"]').getByRole("button", { name: "Request album" }).click();
  expect((await request).postDataJSON().mbid).toBe("new-chart");
  await page.locator('[data-item-id="new-chart"]').getByRole("button", { name: "Not interested" }).click();
  await expect(page.getByText("A new hit hidden", { exact: true })).toBeVisible();
  await page.getByLabel("Popular album selection").selectOption("recent");
  await expect(page.getByText("A new hit", { exact: true })).toHaveCount(0);
  await page.getByLabel("Popular album selection").selectOption("all");
  await expect(page.getByText("A new hit", { exact: true })).toHaveCount(0);
});

test.describe("mobile touch and high-density artwork", () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 });

  test("a vertical swipe in the card gap scrolls the page while a horizontal swipe scrolls the row", async ({ page }) => {
    await fixture(page);
    await page.goto("/");
    await signIn(page);
    const card = page.locator('[data-item-id="familiar-0"]');
    await card.evaluate((element) => {
      const reason = element.querySelector(".recommendation-reason")!.getBoundingClientRect();
      const request = element.querySelector(".recommendation-request")!.getBoundingClientRect();
      window.scrollBy({ top: (reason.bottom + request.top) / 2 - window.innerHeight / 2, behavior: "instant" });
    });
    const point = await card.evaluate((element) => {
      const reason = element.querySelector(".recommendation-reason")!.getBoundingClientRect();
      const request = element.querySelector(".recommendation-request")!.getBoundingClientRect();
      return { x: reason.left + reason.width / 2, y: (reason.bottom + request.top) / 2 };
    });
    const client = await page.context().newCDPSession(page);
    const before = await page.evaluate(() => window.scrollY);
    const swipe = async (dx: number, dy: number) => {
      await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: point.x, y: point.y }] });
      for (let step = 1; step <= 12; step++) {
        await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: point.x + dx * step / 12, y: point.y + dy * step / 12 }] });
        // Space the native events like a finger moving across successive frames.
        await page.waitForTimeout(16);
      }
      await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    };
    await swipe(0, -180);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(before + 50);
    // Bring the same gap back into the viewport for a horizontal gesture.
    await card.evaluate((element) => {
      const reason = element.querySelector(".recommendation-reason")!.getBoundingClientRect();
      const request = element.querySelector(".recommendation-request")!.getBoundingClientRect();
      window.scrollBy({ top: (reason.bottom + request.top) / 2 - window.innerHeight / 2, behavior: "instant" });
    });
    const row = card.locator("..");
    const scrollBefore = await row.evaluate((element) => element.scrollLeft);
    await swipe(-80, 0);
    await expect.poll(() => row.evaluate((element) => element.scrollLeft)).toBeGreaterThan(scrollBefore + 30);
  });

  test("cached thumbnail URLs upgrade to large artwork on a retina phone", async ({ page }) => {
    await fixture(page);
    await page.route("**/api/discover", (route) => route.fulfill({ json: {
      feedVersion: 5, refreshedAt: 1788652800,
      sections: [{ id: "discovery", title: "Try something new", items: [{
        id: "retina-album", name: "Retina Album", kind: "release-group", reason: "Similar to Olivia Dean",
        coverArt: "/api/artwork/release-group/retina-album?size=thumb",
      }] }],
    } }));
    await page.route("**/api/artwork/**", (route) => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640"><rect width="640" height="640" fill="#746acb"/></svg>' }));
    await page.goto("/");
    await page.locator("#login-form").getByLabel("Username").fill("ada");
    await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    const art = page.locator('[data-item-id="retina-album"] img.recommendation-art');
    await art.scrollIntoViewIfNeeded();
    await expect(art).toHaveAttribute("src", /size=large/);
    await expect(page.getByRole("link", { name: /Listen|Find listening options/ })).toHaveCount(0);
  });
});


test("Japan selector switches chart ranks, preserves filters, and requests the Japanese match on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await fixture(page);
  const payload = {
    feedVersion: 5, refreshedAt: 1788652800, sections: [], popularChart: { status: "ok" },
    popularCharts: { us: { status: "ok" }, jp: { status: "ok" } },
    popularAlbumsByCountry: {
      us: [{ id: "us-hit", kind: "release-group", name: "US hit", artist: "Artist", recentRelease: true }],
      jp: [
        { id: "jp-old", kind: "release-group", name: "Japanese classic", artist: "Artist", recentRelease: false },
        { id: "jp-new", kind: "release-group", name: "光", artist: "宇多田ヒカル", recentRelease: true,
          chartRank: 12, reason: "#12 on Apple Music’s Japan Top 100 albums" },
      ],
    },
  };
  await page.route("**/api/discover", route => route.fulfill({ json: payload }));
  await page.goto("/");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("US hit", { exact: true })).toBeVisible();
  await page.getByLabel("Chart country").selectOption("jp");
  await expect(page.getByText("US hit", { exact: true })).toHaveCount(0);
  await expect(page.getByText("光", { exact: true })).toBeVisible();
  await expect(page.getByText("#12 on Apple Music’s Japan Top 100 albums")).toBeVisible();
  await expect(page.getByText("Japanese classic", { exact: true })).toHaveCount(0);
  await page.getByLabel("Popular album selection").selectOption("all");
  await expect(page.getByText("Japanese classic", { exact: true })).toBeVisible();
  await page.getByLabel("Chart country").selectOption("us");
  await page.getByLabel("Chart country").selectOption("jp");
  await expect(page.getByLabel("Popular album selection")).toHaveValue("all");
  // Background feed refreshes must not switch the country back to the US.
  await page.evaluate(() => window.dispatchEvent(new Event("melodarr-recommendations-changed")));
  await expect(page.getByLabel("Chart country")).toHaveValue("jp");
  await expect(page.getByText("光", { exact: true })).toBeVisible();
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
  await page.locator(".popular-albums").screenshot({ path: resolve(__dirname, "../../.venv-recommendations/japan-chart-mobile.jpg"), type: "jpeg", quality: 65 });
  const request = page.waitForRequest("**/api/request/release-group");
  await page.locator('[data-item-id="jp-new"]').getByRole("button", { name: "Request album" }).click();
  expect((await request).postDataJSON().mbid).toBe("jp-new");
  await page.locator('[data-item-id="jp-new"]').getByRole("button", { name: "Not interested" }).click();
  await expect(page.getByText("光 hidden", { exact: true })).toBeVisible();
  await page.getByLabel("Chart country").selectOption("us");
  await page.getByLabel("Chart country").selectOption("jp");
  await expect(page.getByText("光", { exact: true })).toHaveCount(0);
});

test("an unavailable Japan chart does not display the US albums", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/discover", route => route.fulfill({ json: {
    feedVersion: 5, refreshedAt: 1788652800, sections: [], popularChart: { status: "ok" },
    popularCharts: { us: { status: "ok" }, jp: { status: "unavailable" } },
    popularAlbumsByCountry: { jp: [], us: [{ id: "us-hit", name: "US hit", kind: "release-group", recentRelease: true }] },
  } }));
  await page.goto("/");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Chart country").selectOption("jp");
  await expect(page.getByText("The album chart couldn’t be loaded.", { exact: false })).toBeVisible();
  await expect(page.getByText("US hit", { exact: true })).toHaveCount(0);
  await page.getByLabel("Chart country").selectOption("us");
  await expect(page.getByText("US hit", { exact: true })).toBeVisible();
});


test("taste controls save the mix and up to five favorite artists and survive reload", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await fixture(page);
  await page.goto("/");
  await signIn(page);
  await page.getByText("Shape Your Recommendations", { exact: true }).click();
  await page.getByRole("radio", { name: "More discovery", exact: true }).check();
  for (let i = 1; i <= 6; i++) {
    await page.getByLabel("Find a favorite artist").fill(`Favorite Artist ${i}`);
    await page.getByRole("button", { name: "Find artists", exact: true }).click();
    await page.locator(".taste-search-results").getByRole("button", { name: `Favorite Artist ${i}`, exact: true }).click();
  }
  await expect(page.getByText("You can choose up to five artists.", { exact: false })).toBeVisible();
  await expect(page.locator(".taste-artists button")).toHaveCount(5);
  const saved = page.waitForRequest("**/api/discover/preferences");
  await page.getByRole("button", { name: "Save taste preferences" }).click();
  const body = (await saved).postDataJSON();
  expect(body.mode).toBe("discovery");
  expect(body.starterArtists).toHaveLength(5);
  await expect(page.getByText("Taste preferences saved.", { exact: false })).toBeVisible();
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
  // Capture the entire expanded panel after checking the normal phone viewport.
  await page.setViewportSize({ width: 320, height: 1600 });
  await page.locator(".taste-preferences").screenshot({ path: resolve(__dirname, "../../.venv-recommendations/taste-controls-mobile.jpg"), type: "jpeg", quality: 65 });
  await page.reload();
  await page.getByText("Shape Your Recommendations", { exact: true }).click();
  await expect(page.getByRole("radio", { name: "More discovery", exact: true })).toBeChecked();
  await expect(page.locator(".taste-artists button")).toHaveCount(5);
  await page.getByRole("button", { name: "Remove Favorite Artist 1 from favorites" }).click();
  await expect(page.locator(".taste-artists button")).toHaveCount(4);
});

test("failed taste saves keep the selections available for retry", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/discover/preferences", route => route.fulfill({ status: 503, json: { error: "Temporarily unavailable" } }));
  await page.goto("/");
  await signIn(page);
  await page.getByText("Shape Your Recommendations", { exact: true }).click();
  await page.getByRole("radio", { name: "More familiar artists", exact: true }).check();
  await page.getByRole("button", { name: "Save taste preferences" }).click();
  await expect(page.getByText("Couldn’t save your taste preferences.", { exact: false })).toBeVisible();
  await expect(page.getByRole("radio", { name: "More familiar artists", exact: true })).toBeChecked();
  await expect(page.getByRole("button", { name: "Save taste preferences" })).toBeEnabled();
});

test("chart polling fills the chart without rebuilding personal cards", async ({ page }) => {
  await fixture(page);
  await page.clock.install();
  let discoverReads = 0;
  await page.route("**/api/discover", route => {
    discoverReads++;
    return route.fulfill({ json: {
      feedVersion: 5, refreshedAt: 1788652800,
      sections: [{ id: "familiar", title: "Personal picks", items: [{ id: "personal", name: "Personal album", artist: "Artist", kind: "release-group" }] }],
      popularChart: { status: "refreshing", pending: true },
      popularCharts: { us: { status: "refreshing", pending: true }, jp: { status: "refreshing", pending: true } },
      popularAlbumsByCountry: { us: [], jp: [] },
    } });
  });
  await page.route("**/api/discover/charts", route => route.fulfill({ json: {
    popularChart: { status: "ok" }, popularCharts: { us: { status: "ok" }, jp: { status: "ok" } },
    popularAlbumsByCountry: { us: [{ id: "chart-new", name: "Fresh chart album", kind: "release-group", recentRelease: true }], jp: [] },
  } }));
  await page.goto("/");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Personal album", { exact: true })).toBeVisible();
  await page.locator('[data-item-id="personal"]').evaluate(element => element.setAttribute("data-preserved", "true"));
  await page.clock.fastForward(15_001);
  await expect(page.getByText("Fresh chart album", { exact: true })).toBeVisible();
  await expect(page.locator('[data-item-id="personal"]')).toHaveAttribute("data-preserved", "true");
  expect(discoverReads).toBe(1);
});

test("charts and optional taste setup are available while the first personal feed is pending", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/discover", route => route.fulfill({ json: {
    pending: true, tastePreferences: { mode: "balanced", starterArtists: [] },
    popularChart: { status: "ok" }, popularCharts: { us: { status: "ok" }, jp: { status: "ok" } },
    popularAlbumsByCountry: { us: [{ id: "chart", name: "Chart before personal", kind: "release-group", recentRelease: true }], jp: [] },
  } }));
  await page.goto("/");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByText("Chart before personal", { exact: true })).toBeVisible();
  await page.getByText("Shape Your Recommendations", { exact: true }).click();
  await expect(page.getByLabel("Find a favorite artist")).toBeVisible();
});

test("request influence is editable only on your own history and persists after reload", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await fixture(page);
  let included = true;
  await page.route("**/api/account/profile?*", route => route.fulfill({ json: {
    user: { id: 1, username: "ada", localUsername: "ada", role: "admin", userType: "local" },
    requests: { artist: [], "release-group": [{ id: 41, mbid: "gift", name: "Gift album", artist_name: "Gift Artist", created_at: 1788652800, use_for_recommendations: included }] },
    pagination: { page: 1, pageSize: 100, total: 1, totalPages: 1 },
  } }));
  await page.route("**/api/discover/request-influence", async route => {
    expect(route.request().postDataJSON().requestId).toBe(41);
    included = route.request().postDataJSON().useForRecommendations;
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto("/");
  await signIn(page);
  await page.goto("/ada/requests");
  const toggle = page.getByLabel("Use for recommendations", { exact: true });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect(page.getByText("Excluded from your taste profile.", { exact: false })).toBeVisible();
  await expect(page.getByText("Gift album", { exact: true })).toBeVisible();
  await page.locator(".history-item").screenshot({ path: resolve(__dirname, "../../.venv-recommendations/request-taste-mobile.jpg"), type: "jpeg", quality: 65 });
  await page.reload();
  await expect(toggle).not.toBeChecked();
  await page.goto("/bea/requests");
  await expect(page.getByText("Gift album", { exact: true })).toBeVisible();
  await expect(toggle).toHaveCount(0);
});


test("mix preview explains row order and limits, supports keyboard selection, and shows unsaved changes", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await signIn(page);
  const panel = page.locator(".taste-preferences");
  await expect(panel.locator("summary")).toContainText("Balanced mix");
  const expectCompactHeader = async () => {
    const spacing = await panel.evaluate(element => {
      const box = element.getBoundingClientRect();
      const summary = element.querySelector("summary")!.getBoundingClientRect();
      const copy = element.querySelector(".taste-summary-copy")!.getBoundingClientRect();
      return { outerTop: summary.top - box.top, top: copy.top - summary.top,
        bottom: summary.bottom - copy.bottom, height: summary.height };
    });
    expect(spacing.outerTop).toBeLessThanOrEqual(1);
    expect(Math.abs(spacing.top - spacing.bottom)).toBeLessThanOrEqual(1);
    expect(spacing.height).toBeLessThan(85);
  };
  await expectCompactHeader();

  await page.getByText("Shape Your Recommendations", { exact: true }).click();
  await expectCompactHeader();
  const save = page.getByRole("button", { name: "Save taste preferences" });
  await expect(save).toBeDisabled();
  const rows = panel.locator(".taste-row-preview li");
  await expect(rows.locator(".taste-row-count")).toHaveText(["6", "6", "6"]);
  const balanced = page.getByRole("radio", { name: "Balanced", exact: true });
  await balanced.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("radio", { name: "More discovery", exact: true })).toBeChecked();
  await expect(rows.first()).toContainText("Try something new");
  await expect(rows.locator(".taste-row-count")).toHaveText(["8", "6", "4"]);
  await expect(panel.locator(".taste-save-state")).toHaveText("Unsaved changes");
  await expect(panel.locator("summary")).toContainText("Balanced mix");
  await page.getByRole("radio", { name: "More familiar artists", exact: true }).check();
  await expect(rows.first()).toContainText("More from artists you love");
  await expect(rows.locator(".taste-row-count")).toHaveText(["8", "6", "4"]);
  await save.click();
  await expect(panel.locator("summary")).toContainText("Familiar first");
  await expect(save).toBeDisabled();
  await expect(panel.locator(".taste-save-state")).toHaveText("All changes saved");
  await panel.screenshot({ path: resolve(__dirname, "../../.venv-recommendations/taste-refined-desktop.jpg"), type: "jpeg", quality: 60 });
  await page.evaluate(() => document.documentElement.removeAttribute("data-theme"));
  await panel.screenshot({ path: resolve(__dirname, "../../.venv-recommendations/taste-refined-light.jpg"), type: "jpeg", quality: 60 });
});


for (const width of [1440, 320]) {
  test(`profile request cards keep Plex actions aligned and remain compact at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1200 });
    await fixture(page);
    await page.route("**/api/account/profile?*", route => route.fulfill({ json: {
      user: { id: 1, username: "ada", localUsername: "ada", role: "admin", userType: "local" },
      requests: {
        artist: ["Myke Towers", "Prince Royce", "Bad Bunny"].map((name, i) => ({
          id: i + 1, mbid: `artist-${i}`, name, created_at: 1789171200, use_for_recommendations: true,
          availableInPlex: true, plexUrl: "https://app.plex.tv/desktop/#!/server/fixture/details?key=artist",
        })),
        "release-group": [{ id: 41, mbid: "album", name: "A very long album title that still leaves room for the Plex button",
          artist_name: "Favorite artist", release_type: "Album", release_date: "2026-09-01", created_at: 1789171200,
          requestStatus: "available", availableInPlex: true, plexUrl: "https://app.plex.tv/desktop/#!/server/fixture/details?key=album",
          use_for_recommendations: false }],
      }, pagination: { page: 1, pageSize: 100, total: 4, totalPages: 1 },
    } }));
    await page.goto("/");
    await signIn(page);
    await page.goto("/ada/requests");
    await expect(page.locator(".history-item")).toHaveCount(4);
    await page.evaluate(() => document.documentElement.removeAttribute("data-theme"));
    const first = page.locator(".history-item").first();
    const geometry = await first.evaluate(card => {
      const box = card.getBoundingClientRect();
      const main = card.querySelector(".history-main")!.getBoundingClientRect();
      const plex = card.querySelector(".history-plex")!.getBoundingClientRect();
      const toggle = card.querySelector(".request-taste-controls")!.getBoundingClientRect();
      return { height: box.height, mainTop: main.top, mainBottom: main.bottom, plexTop: plex.top, plexBottom: plex.bottom, toggleTop: toggle.top };
    });
    expect(geometry.height).toBeLessThan(width === 320 ? 120 : 105);
    expect(geometry.plexTop).toBeGreaterThanOrEqual(geometry.mainTop);
    expect(geometry.plexBottom).toBeLessThanOrEqual(geometry.mainBottom);
    expect(geometry.plexBottom).toBeLessThanOrEqual(geometry.toggleTop);
    await expect(first.getByLabel("Use for recommendations", { exact: true })).toBeChecked();
    await expect(page.locator(".history-item").last().locator(".request-lifecycle")).toHaveText("Available");
    expect(await page.locator(".history-item").last().locator(".history-plex").getAttribute("href")).toContain("app.plex.tv");
    await expect(page.locator("#request-taste-help")).toHaveCount(1);
    const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
    await first.scrollIntoViewIfNeeded();
    await page.screenshot({ path: resolve(__dirname, `../../.venv-recommendations/requests-fixed-${width}.jpg`), type: "jpeg", quality: 65 });
  });
}

import { expect as baseExpect, test, type Page, type Route } from "@playwright/test";

// Real Flask/SQLite responses can queue behind a superseded fixture request.
const expect = baseExpect.configure({ timeout: 10_000 });

type HistoryItem = {
  id: number; kind: string; mbid: string; name: string; artist_name?: string;
  anime_name?: string; anime_slug?: string; song_title?: string; theme_label?: string;
  aliases?: string[]; created_at: number; use_for_recommendations: boolean;
  requestStatus?: string; theme_id?: number; availableInPlex?: boolean; plexUrl?: string;
};

const artist: HistoryItem = { id: 1, kind: "artist", mbid: "fixture-artist", name: "All Time Low", aliases: ["Known Artist Alias"], created_at: 1, use_for_recommendations: true };
const album: HistoryItem = { id: 2, kind: "release-group", mbid: "fixture-album", name: "So Wrong, It's Right", artist_name: "All Time Low",
  anime_name: "Fullmetal Alchemist", anime_slug: "fullmetal_alchemist", song_title: "again", theme_label: "Opening 1", theme_id: 3,
  aliases: ["Known Artist Alias", "Romanized Album Title", "Hagane no Renkinjutsushi"], created_at: 2, use_for_recommendations: true,
  requestStatus: "available", availableInPlex: true, plexUrl: "https://app.plex.tv/album" };

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

async function fixture(page: Page, extra: HistoryItem[] = [], initial: HistoryItem[] = [artist, album]) {
  const items = [...initial.map(item => ({ ...item })), ...extra].sort((a, b) => b.created_at - a.created_at || b.id - a.id);
  const reads: { query: string; page: number; username: string; status?: string }[] = [];
  const updates: unknown[] = [];
  await page.route("**/api/discover", route => route.fulfill({ json: { sections: [] } }));
  await page.route("**/api/account/profile?*", async route => {
    const params = new URL(route.request().url()).searchParams;
    const query = params.get("q") || "";
    const currentPage = Number(params.get("page") || 1);
    const status = params.get("status") || "all";
    reads.push({ query, page: currentPage, username: params.get("username") || "", ...(status !== "all" ? { status } : {}) });
    const response = await page.request.post("/__request-history", { data: {
      items, query, page: params.get("page") ?? "1", status, username: params.get("username"),
    } });
    const result = await response.json();
    if (!route.request().failure()) await route.fulfill({ status: result.status, json: result.body });
  });
  await page.route("**/api/discover/request-influence", async route => {
    const update = route.request().postDataJSON();
    updates.push(update);
    items.find(item => item.id === update.requestId)!.use_for_recommendations = update.useForRecommendations;
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto("/");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("body")).toHaveClass(/authenticated/);
  await page.goto("/ada/requests");
  await expect(page.locator(".request-history-section")).toHaveCount(2);
  return { reads, updates, items };
}

for (const theme of ["midnight", "warm"]) {
  for (const width of [1440, 320]) {
    test(`history search preserves grouped controls in ${theme} at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1200 });
      await page.addInitScript(theme => localStorage.setItem("melodarr-theme", theme), theme);
      const { reads, updates } = await fixture(page);
      const search = page.getByRole("searchbox", { name: "Search request history" });
      const groups = page.locator(".request-history-section");
      const status = page.locator(".request-search-status");
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await expect(page.getByRole("button", { name: "Clear request search" })).toBeHidden();
      const placement = await search.evaluate(element => ({
        inputTop: element.getBoundingClientRect().top,
        helpBottom: document.querySelector("#request-taste-help")!.getBoundingClientRect().bottom,
      }));
      expect(placement.inputTop).toBeGreaterThan(placement.helpBottom);
      reads.length = 0;
      const unexpected: string[] = [];
      page.on("request", request => {
        if (request.resourceType() === "fetch" && !new URL(request.url()).pathname.startsWith("/api/account/profile")) unexpected.push(request.url());
      });
      await page.clock.install({ time: new Date("2026-09-30T12:00:00Z") });
      await page.clock.pauseAt(new Date("2026-09-30T12:01:00Z"));
      await search.fill("ALL time LOW");
      await page.clock.fastForward(249);
      expect(reads).toHaveLength(0);
      await page.clock.fastForward(1);
      await expect(status).toHaveText("2 matching requests.");
      expect(reads).toEqual([{ query: "ALL time LOW", page: 1, username: "ada" }]);
      expect(unexpected).toEqual([]);
      await expect(search).toBeFocused();
      await expect(groups.first().getByRole("heading")).toHaveText("Artists (1)");
      await expect(groups.last().getByRole("heading")).toHaveText("Release groups (1)");
      await expect(groups.first()).toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await groups.first().locator("summary").click();
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await groups.first().locator("summary").focus();
      await page.keyboard.press("Enter");
      await expect(groups.first()).toHaveAttribute("open");
      await expect(groups.last().locator(".history-title")).toHaveAttribute("href", "/albums/fixture-album");
      await expect(groups.last().locator("time")).toHaveAttribute("datetime", "1970-01-01T00:00:02.000Z");
      await expect(groups.last().locator(".request-lifecycle")).toHaveText("Available");
      await expect(groups.last().locator(".history-plex")).toHaveAttribute("href", "https://app.plex.tv/album");
      await expect(groups.last().getByRole("link", { name: /Fullmetal Alchemist/ })).toHaveAttribute("href", "/anime/fullmetal_alchemist#theme-3");
      const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
      await page.screenshot({ path: testInfo.outputPath("history-search.png") });

      await groups.first().getByLabel("Use for recommendations", { exact: true }).uncheck();
      await expect(groups.first().getByText("Excluded from your taste profile.", { exact: false })).toBeVisible();
      await groups.last().getByLabel("Use for recommendations", { exact: true }).uncheck();
      await expect(groups.last().getByText("Excluded from your taste profile.", { exact: false })).toBeVisible();
      expect(updates).toEqual([{ requestId: 1, useForRecommendations: false }, { requestId: 2, useForRecommendations: false }]);
      await page.getByRole("button", { name: "Clear request search" }).click();
      await expect(status).toBeEmpty();
      await expect(search).toHaveValue("");
      await expect(search).toBeFocused();
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await expect(groups.first().getByRole("heading")).toHaveText("Artists");
      await expect(groups.last().getByRole("heading")).toHaveText("Release groups");
      await expect(page).toHaveURL(/\/ada\/requests$/);
      await expect(groups.last().getByLabel("Use for recommendations", { exact: true })).not.toBeChecked();
    });
  }
}

test("history search sends album, anime, song, theme and known alias queries to the local history API", async ({ page }) => {
  const { reads } = await fixture(page);
  const search = page.getByRole("searchbox", { name: "Search request history" });
  for (const query of ["So Wrong Its Right", "Fullmetal Alchemist", "again", "Opening 1", "fullmetal alchemist", "Romanized Album Title", "Hagane no Renkinjutsushi"]) {
    await search.fill(query);
    await search.press("Enter");
    await expect(page.locator(".request-search-status")).toHaveText("1 matching requests.");
    await expect(page.locator(".request-history-section").first()).not.toHaveAttribute("open");
    await expect(page.locator(".request-history-section").last()).toHaveAttribute("open");
    await expect(page.getByRole("link", { name: "So Wrong, It's Right", exact: true })).toBeVisible();
    expect(reads.at(-1)?.query).toBe(query);
  }
  await search.fill("Known Artist Alias");
  await search.press("Enter");
  await expect(page.locator(".request-search-status")).toHaveText("2 matching requests.");
  await search.fill("Nothing matches");
  await search.press("Enter");
  await expect(page.locator(".request-search-status")).toHaveText("No matching requests.");
  await expect(page.locator(".history-item")).toHaveCount(0);
  await expect(page.locator(".request-history-section[open]")).toHaveCount(0);
});

test("search finds off-page requests, paginates filtered totals, restores query on back and resets on clear", async ({ page }) => {
  const extra = Array.from({ length: 205 }, (_, i) => ({ id: 100 + i, mbid: `other-${i}`, kind: "artist", name: "Bulk match", created_at: 100 + i, use_for_recommendations: true }));
  const { reads } = await fixture(page, extra);
  const search = page.getByRole("searchbox", { name: "Search request history" });
  const pagination = page.getByRole("navigation", { name: "Request history pages" });
  await expect(page.getByRole("link", { name: "So Wrong, It's Right", exact: true })).toHaveCount(0);
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await expect(pagination).toContainText("Page 2 of 3");
  await search.fill("All Time Low");
  await search.press("Enter");
  await expect(page.getByRole("link", { name: "So Wrong, It's Right", exact: true })).toBeVisible();
  await expect(pagination).toContainText("Page 1 of 1 · 2 requests");
  expect(reads.at(-1)?.page).toBe(1);
  await search.fill("bulk");
  await search.press("Enter");
  await expect(pagination).toContainText("Page 1 of 3 · 205 requests");
  await expect(page.locator(".request-history-section").first().getByRole("heading")).toHaveText("Artists (205)");
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await expect(pagination).toContainText("Page 2 of 3 · 205 requests");
  await expect(search).toHaveValue("bulk");
  expect(reads.at(-1)).toEqual({ query: "bulk", page: 2, username: "ada" });
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await expect(pagination).toContainText("Page 3 of 3 · 205 requests");
  await expect(page.locator(".history-item")).toHaveCount(5);
  await page.goBack();
  await expect(pagination).toContainText("Page 2 of 3 · 205 requests");
  await expect(search).toHaveValue("bulk");
  await page.getByRole("button", { name: "Clear request search" }).click();
  await expect(pagination).toContainText("Page 1 of 3 · 207 requests");
  await expect(page.locator(".request-history-section").first()).not.toHaveAttribute("open");
  await expect(page.locator(".request-history-section").last()).toHaveAttribute("open");
});

test("search cancels pending debounce when leaving history", async ({ page }) => {
  const { reads } = await fixture(page);
  await page.clock.install({ time: new Date("2026-09-30T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-30T12:01:00Z"));
  await page.getByRole("searchbox", { name: "Search request history" }).fill("All Time Low");
  await page.locator('[data-account-route="profile"]').click();
  await page.clock.fastForward(500);
  expect(reads.filter(read => read.query)).toHaveLength(0);
  await expect(page.locator("#account-title")).toHaveText("Profile");
});

test("search category counts include matches on later pages", async ({ page }) => {
  await fixture(page, Array.from({ length: 101 }, (_, i) => ({
    id: 100 + i, mbid: `artist-${i}`, kind: "artist", name: "All Time Low",
    created_at: 100 + i, use_for_recommendations: true,
  })));
  const search = page.getByRole("searchbox", { name: "Search request history" });
  await search.fill("All Time Low");
  await search.press("Enter");
  const releases = page.locator(".request-history-section").last();
  await expect(releases.getByRole("heading")).toHaveText("Release groups (1)");
  await expect(releases).toHaveAttribute("open");
  await expect(releases.getByText("No matching requests on this page.")).toBeVisible();
  await page.getByRole("navigation", { name: "Request history pages" }).getByRole("button", { name: "Next", exact: true }).click();
  await expect(releases.getByRole("link", { name: "So Wrong, It's Right", exact: true })).toBeVisible();
});

for (const theme of ["midnight", "warm"]) {
  for (const width of [1440, 320]) {
    test(`status filter composes with search and preserves controls in ${theme} at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1200 });
      await page.addInitScript(theme => localStorage.setItem("melodarr-theme", theme), theme);
      const { reads, updates } = await fixture(page, [
        { ...artist, id: 3, mbid: "ready-artist", created_at: 3, availableInPlex: true, plexUrl: "https://app.plex.tv/artist" },
        ...["requested", "queued", "downloading"].map((requestStatus, i) => ({
          id: 4 + i, kind: "release-group", mbid: requestStatus, name: `${requestStatus} album`, artist_name: "All Time Low",
          requestStatus, created_at: 4 + i, use_for_recommendations: true,
        })),
      ]);
      const search = page.getByRole("searchbox", { name: "Search request history" });
      const select = page.getByRole("combobox", { name: "Request status" });
      const groups = page.locator(".request-history-section");
      const message = page.locator(".request-search-status");
      await expect(select).toHaveValue("all");
      expect(await select.locator("option").allTextContents()).toEqual(["All", "Requested", "Available"]);
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      const searchBox = (await search.boundingBox())!;
      const filterBox = (await select.boundingBox())!;
      if (width === 1440) expect(Math.abs(searchBox.y - filterBox.y)).toBeLessThan(5);
      else expect(filterBox.y).toBeGreaterThanOrEqual(searchBox.y + searchBox.height);

      await select.focus();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await expect(select).toHaveValue("requested");
      await expect(message).toHaveText("4 matching requests.");
      await expect(groups.first().getByRole("heading")).toHaveText("Artists (1)");
      await expect(groups.last().getByRole("heading")).toHaveText("Release groups (3)");
      await expect(groups.first()).toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      for (const lifecycle of ["Requested", "Queued", "Downloading"]) {
        await expect(groups.last().locator(".request-lifecycle").filter({ hasText: lifecycle })).toHaveCount(1);
      }
      await expect(page).toHaveURL(/status=requested/);
      await groups.first().locator("summary").click();
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await groups.first().locator("summary").click();
      await groups.first().getByLabel("Use for recommendations", { exact: true }).uncheck();
      expect(updates).toEqual([{ requestId: 1, useForRecommendations: false }]);

      await search.fill("All Time Low");
      await search.press("Enter");
      await expect(message).toHaveText("4 matching requests.");
      await select.selectOption("available");
      await expect(message).toHaveText("2 matching requests.");
      expect(reads.at(-1)).toEqual({ query: "All Time Low", page: 1, username: "ada", status: "available" });
      await expect(groups.last().locator(".history-title")).toHaveAttribute("href", "/albums/fixture-album");
      await expect(groups.last().locator("time")).toHaveAttribute("datetime", "1970-01-01T00:00:02.000Z");
      await expect(groups.last().locator(".history-plex")).toHaveAttribute("href", "https://app.plex.tv/album");
      await groups.last().getByLabel("Use for recommendations", { exact: true }).uncheck();
      expect(updates.at(-1)).toEqual({ requestId: 2, useForRecommendations: false });
      await search.fill("again");
      await search.press("Enter");
      await expect(message).toHaveText("1 matching requests.");
      await expect(select).toHaveValue("available");
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await page.getByRole("button", { name: "Clear request search" }).click();
      await expect(message).toHaveText("2 matching requests.");
      await expect(select).toHaveValue("available");
      await expect(page).toHaveURL(/\/ada\/requests\?status=available$/);
      const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
      expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
      await page.screenshot({ path: testInfo.outputPath("history-status.png") });
      await page.reload();
      await expect(page.getByRole("combobox", { name: "Request status" })).toHaveValue("available");
      await expect(message).toHaveText("2 matching requests.");
      await page.getByRole("combobox", { name: "Request status" }).selectOption("all");
      await expect(message).toBeEmpty();
      await expect(groups.first()).not.toHaveAttribute("open");
      await expect(groups.last()).toHaveAttribute("open");
      await expect(page).toHaveURL(/\/ada\/requests$/);
    });
  }
}

test("status pagination preserves query and restores both from back navigation", async ({ page }) => {
  const { reads } = await fixture(page, Array.from({ length: 205 }, (_, i) => ({
    id: 100 + i, mbid: `bulk-${i}`, kind: "artist", name: "Bulk match", created_at: 100 + i, use_for_recommendations: true,
  })));
  const search = page.getByRole("searchbox", { name: "Search request history" });
  const select = page.getByRole("combobox", { name: "Request status" });
  const pagination = page.getByRole("navigation", { name: "Request history pages" });
  await select.selectOption("requested");
  await search.fill("bulk");
  await search.press("Enter");
  await expect(pagination).toContainText("Page 1 of 3 · 205 requests");
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await expect(pagination).toContainText("Page 2 of 3 · 205 requests");
  await expect(select).toHaveValue("requested");
  await expect(search).toHaveValue("bulk");
  expect(reads.at(-1)).toEqual({ query: "bulk", page: 2, username: "ada", status: "requested" });
  await select.selectOption("available");
  await expect(page.locator(".request-search-status")).toHaveText("No matching requests.");
  expect(reads.at(-1)?.page).toBe(1);
  await expect(search).toHaveValue("bulk");
  await expect(page).not.toHaveURL(/page=2/);
  await page.goBack();
  await expect(select).toHaveValue("requested");
  await expect(search).toHaveValue("bulk");
  await expect(pagination).toContainText("Page 1 of 3 · 205 requests");
  await page.getByRole("button", { name: "Clear request search" }).click();
  await expect(select).toHaveValue("requested");
  await expect(pagination).toContainText("Page 1 of 3 · 206 requests");
  await select.selectOption("available");
  await expect(page.getByRole("link", { name: "So Wrong, It's Right", exact: true })).toBeVisible();
  await expect(pagination).toContainText("Page 1 of 1 · 1 requests");
});

test("status-only empty results identify the selected filter and cancel pending search debounce", async ({ page }) => {
  await fixture(page);
  const select = page.getByRole("combobox", { name: "Request status" });
  const search = page.getByRole("searchbox", { name: "Search request history" });
  const message = page.locator(".request-search-status");
  await page.clock.install({ time: new Date("2026-09-30T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-30T12:01:00Z"));
  await search.fill("nothing");
  await select.selectOption("available");
  await expect(message).toHaveText("No matching requests.");
  await page.clock.fastForward(500);
  await expect(select).toHaveValue("available");
  await expect(message).toHaveText("No matching requests.");
  // Empty histories return the same API shape as real filtered histories.
  await page.route("**/api/account/profile?*", route => route.fulfill({ json: {
    requests: { artist: [], "release-group": [] }, matchCounts: { artist: 0, "release-group": 0 },
    pagination: { page: 1, pageSize: 100, total: 0, totalPages: 0 },
  } }));
  await page.getByRole("button", { name: "Clear request search" }).click();
  await expect(message).toHaveText("No available items found.");
  await expect(page.locator(".request-history-section[open]")).toHaveCount(0);
  await select.selectOption("requested");
  await expect(message).toHaveText("No requested items found.");
});

const historySearch = (page: Page) => page.getByRole("searchbox", { name: "Search request history" });
const historyPages = (page: Page) => page.getByRole("navigation", { name: "Request history pages" });
const historyStatus = (page: Page) => page.getByRole("combobox", { name: "Request status" });
const historyMessage = (page: Page) => page.locator(".request-search-status");
const nextPage = (page: Page) => historyPages(page).getByRole("button", { name: "Next", exact: true });

function bulkHistory(size: number, available = false): HistoryItem[] {
  return Array.from({ length: size }, (_, i) => ({
    id: 100 + i, kind: i % 2 ? "artist" : "release-group", mbid: `boundary-${i}`,
    name: "Bulk match", aliases: ["Bulk Alias"], created_at: 1, use_for_recommendations: true,
    availableInPlex: available, requestStatus: available ? "available" : "requested",
  }));
}

async function replyHistory(page: Page, route: Route, items: HistoryItem[]) {
  const params = new URL(route.request().url()).searchParams;
  const response = await page.request.post("/__request-history", { data: {
    items, query: params.get("q") || "", status: params.get("status") || "all",
    page: params.get("page") ?? "1", username: params.get("username"),
  } });
  const result = await response.json();
  if (!route.request().failure()) await route.fulfill({ status: result.status, json: result.body });
}

for (const status of ["all", "requested", "available"]) {
  for (const size of [0, 1, 100, 101, 205]) {
    test(`boundary ${size} preserves mixed-category partitions with search and ${status}`, async ({ page }) => {
      const { items } = await fixture(page, [], bulkHistory(size, status === "available"));
      await expect(page.locator(".history-item")).toHaveCount(Math.min(size, 100));
      await historyStatus(page).selectOption(status);
      await historySearch(page).fill("Bulk Alias");
      await historySearch(page).press("Enter");
      await expect(historyMessage(page)).toHaveText(size ? `${size} matching requests.` : "No matching requests.");
      const headings = page.locator(".request-history-section h2");
      await expect(headings).toHaveText([`Artists (${Math.floor(size / 2)})`, `Release groups (${Math.ceil(size / 2)})`]);
      const seen: string[] = [];
      const pages = Math.ceil(size / 100);
      for (let current = 1; current <= Math.max(1, pages); current++) {
        await expect(historyPages(page)).toContainText(size ? `Page ${current} of ${pages} · ${size} requests` : "No requests");
        const ids = await page.locator(".history-item").evaluateAll(cards => cards.map(card => (card as HTMLElement).dataset.tasteRequestId!));
        const ordered = items.slice((current - 1) * 100, current * 100);
        expect(new Set(ids)).toEqual(new Set(ordered.map(item => String(item.id))));
        for (const [index, kind] of ["artist", "release-group"].entries()) {
          expect(await page.locator(".request-history-section").nth(index).locator(".history-item").evaluateAll(cards => cards.map(card => (card as HTMLElement).dataset.tasteRequestId))).toEqual(ordered.filter(item => item.kind === kind).map(item => String(item.id)));
        }
        seen.push(...ids);
        if (current < pages) await nextPage(page).click();
      }
      expect(seen.length).toBe(size);
      expect(new Set(seen).size).toBe(size);
      await expect(nextPage(page)).toBeDisabled();
      if (pages <= 1) await expect(historyPages(page).getByRole("button", { name: "Previous" })).toBeDisabled();
      else await expect(historyPages(page).getByRole("button", { name: "Previous" })).toBeEnabled();
    });
  }
}

test("pending filters and debounced searches disable obsolete pagination", async ({ page }) => {
  const { items } = await fixture(page, [], bulkHistory(205, true));
  let pending: Route | undefined;
  await page.route("**/api/account/profile?*", async route => {
    if (new URL(route.request().url()).searchParams.get("status") === "available") pending = route;
    else await route.fallback();
  });
  await historyStatus(page).selectOption("available");
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(nextPage(page)).toBeDisabled();
  // A native click on a disabled button must not change the URL or state.
  await nextPage(page).evaluate(button => (button as HTMLButtonElement).click());
  await expect(page).toHaveURL(/status=available$/);
  await replyHistory(page, pending!, items);
  await expect(historyPages(page)).toContainText("Page 1 of 3");
  await page.unroute("**/api/account/profile?*", undefined);
  // Restore the real response route after removing the delay interceptor.
  await page.route("**/api/account/profile?*", route => replyHistory(page, route, items));
  await page.clock.install();
  await historySearch(page).fill("Bulk Alias");
  await expect(nextPage(page)).toBeDisabled();
  await nextPage(page).evaluate(button => (button as HTMLButtonElement).click());
  await page.clock.fastForward(250);
  await expect(historyMessage(page)).toHaveText("205 matching requests.");
  await expect(page).toHaveURL(/q=Bulk\+Alias&status=available$/);
  await expect(historyPages(page)).toContainText("Page 1 of 3");
});

test("rapid query filter page changes ignore an older delayed response", async ({ page }) => {
  const source = bulkHistory(205, true);
  source.slice(0, 40).forEach(item => { item.availableInPlex = false; item.requestStatus = "requested"; });
  const { items } = await fixture(page, [], source);
  const held: Route[] = [];
  await page.route("**/api/account/profile?*", async route => {
    const params = new URL(route.request().url()).searchParams;
    if (params.get("status") === "available" && params.get("page") === "1") held.push(route);
    else await route.fallback();
  });
  await historySearch(page).fill("Bulk");
  await historyStatus(page).selectOption("available");
  await expect.poll(() => held.length).toBe(1);
  await historySearch(page).fill("Bulk Alias");
  await historySearch(page).press("Enter");
  await expect.poll(() => held.length).toBe(2);
  await expect(nextPage(page)).toBeDisabled();
  await replyHistory(page, held[1], items);
  await expect(historyMessage(page)).toHaveText("165 matching requests.");
  await nextPage(page).click();
  await expect(historyPages(page)).toContainText("Page 2 of 2 · 165 requests");
  await replyHistory(page, held[0], items);
  await expect(page.locator(".history-item")).toHaveCount(65);
  await expect(historySearch(page)).toHaveValue("Bulk Alias");
  await expect(historyStatus(page)).toHaveValue("available");
  await expect(page).toHaveURL(/page=2&q=Bulk\+Alias&status=available$/);
  await expect(historyPages(page)).toContainText("Page 2 of 2 · 165 requests");
});

test("failed searches remove obsolete cards and pagination and allow recovery", async ({ page }) => {
  await fixture(page, [], bulkHistory(205));
  await page.route("**/api/account/profile?*", async route => {
    if (new URL(route.request().url()).searchParams.get("q") === "fails") await route.fulfill({ status: 500, json: { error: "Private upstream URL and internal exception" } });
    else await route.fallback();
  });
  await historySearch(page).fill("fails");
  await historySearch(page).press("Enter");
  await expect(historyMessage(page)).toHaveText("Requests could not be loaded. Please try again.");
  await expect(page.locator(".history-item")).toHaveCount(0);
  await expect(historyPages(page)).toHaveCount(0);
  await expect(historySearch(page)).toHaveValue("fails");
  await expect(page).toHaveURL(/q=fails$/);
  await historySearch(page).fill("Bulk");
  await historySearch(page).press("Enter");
  await expect(historyPages(page)).toContainText("Page 1 of 3 · 205 requests");
});

for (const succeeds of [true, false]) {
  test(`recommendation mutation ${succeeds ? "succeeds" : "fails"} after search and filter rerenders`, async ({ page }) => {
    const { items } = await fixture(page);
    let pending: Route | undefined;
    const posts: unknown[] = [];
    await page.route("**/api/discover/request-influence", route => {
      posts.push(route.request().postDataJSON());
      expect(route.request().headers()["x-csrf-token"]).toBe("csrf-ada");
      pending = route;
    });
    const toggle = page.locator(".request-history-section").last().getByLabel("Use for recommendations", { exact: true });
    await toggle.uncheck();
    await expect(toggle).toBeDisabled();
    await historySearch(page).fill("again");
    await historySearch(page).press("Enter");
    await expect(historyMessage(page)).toHaveText("1 matching requests.");
    await historyStatus(page).selectOption("available");
    await expect(historyMessage(page)).toHaveText("1 matching requests.");
    await expect(toggle).not.toBeChecked();
    await expect(toggle).toBeDisabled();
    if (succeeds) items.find(item => item.id === 2)!.use_for_recommendations = false;
    await pending!.fulfill({ status: succeeds ? 200 : 500, json: succeeds ? { ok: true } : { error: "Private error detail" } });
    await expect(toggle).toBeEnabled();
    await expect(toggle).toBeChecked({ checked: !succeeds });
    await expect(page.getByText(succeeds ? "Excluded from your taste profile." : "Couldn’t save this preference. Please try again.", { exact: false })).toBeVisible();
    expect(posts).toEqual([{ requestId: 2, useForRecommendations: false }]);
    await page.reload();
    await expect(toggle).toBeChecked({ checked: !succeeds });
  });
}

test("pending recommendation save survives page changes without touching another account", async ({ page }) => {
  const { items } = await fixture(page);
  let pending: Route | undefined;
  let posts = 0;
  await page.route("**/api/discover/request-influence", async route => {
    posts++;
    if (posts === 1) pending = route;
    else await route.fallback();
  });
  const toggle = page.locator(".request-history-section").last().getByLabel("Use for recommendations", { exact: true });
  await toggle.uncheck();
  await page.locator('[data-account-route="profile"]').click();
  await page.locator('[data-account-route="requests"]').click();
  await expect(toggle).toBeDisabled();
  await expect(toggle).not.toBeChecked();
  items.find(item => item.id === 2)!.use_for_recommendations = false;
  await pending!.fulfill({ json: { ok: true } });
  await expect(toggle).toBeEnabled();
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  await expect(toggle).toBeEnabled();
  expect(posts).toBe(2);
  await toggle.uncheck();
  await expect(toggle).toBeEnabled();
  // Hold another mutation across logout/login; even a stale 401 cannot sign
  // the new account out or change its independently rendered control.
  await page.route("**/api/discover/request-influence", route => { posts++; pending = route; });
  await toggle.check();
  await expect(toggle).toBeDisabled();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.locator("#login-form").getByLabel("Username").fill("bea");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator("body")).toHaveClass(/authenticated/);
  await page.locator('.header-nav [data-primary-account="requests"]').click();
  await expect(toggle).toBeEnabled();
  await expect(toggle).not.toBeChecked();
  await pending!.fulfill({ status: 401, json: { error: "Not signed in" } });
  await expect(page.locator("body")).toHaveClass(/authenticated/);
  await expect(page).toHaveURL(/\/bea\/requests$/);
  await expect(toggle).not.toBeChecked();
  expect(posts).toBe(4);
});

test("account sidebar href click keyboard and new tab all use the account's default Requests route", async ({ page, context }) => {
  await fixture(page, [], bulkHistory(205));
  await page.goto("/bea/requests?q=Bulk&status=requested&page=2");
  const link = page.locator('[data-account-route="requests"]');
  await expect(link).toHaveAttribute("href", "/bea/requests");
  await historySearch(page).fill("Bulk Alias");
  await historySearch(page).press("Enter");
  await expect(link).toHaveAttribute("href", "/bea/requests");
  const popupEvent = context.waitForEvent("page");
  await link.click({ modifiers: ["ControlOrMeta"] });
  const popup = await popupEvent;
  await popup.waitForURL("**/bea/requests");
  await popup.close();
  await link.click();
  await expect(page).toHaveURL(/\/bea\/requests$/);
  await expect(historySearch(page)).toHaveValue("");
  await historyStatus(page).selectOption("requested");
  await expect(historyMessage(page)).toHaveText("205 matching requests.");
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/bea\/requests$/);
  await expect(historyStatus(page)).toHaveValue("all");
  await expect(page.locator('.header-nav [data-primary-account="requests"]')).toHaveAttribute("href", "/ada/requests");
});

test("whitespace-only URLs use default grouping and retain valid status and pagination", async ({ page }) => {
  await fixture(page, [], bulkHistory(205));
  await page.goto("/ada/requests?q=%20%20&page=2");
  await expect(historyPages(page)).toContainText("Page 2 of 3 · 205 requests");
  await expect(page).toHaveURL(/\/ada\/requests\?page=2$/);
  await expect(historySearch(page)).toHaveValue("");
  await expect(page.locator(".request-history-section h2")).toHaveText(["Artists", "Release groups"]);
  await expect(page.locator(".request-history-section").first()).not.toHaveAttribute("open");
  await expect(page.locator(".request-history-section").last()).toHaveAttribute("open");
  await page.goto("/ada/requests?q=%20%20&status=requested&page=2");
  await expect(historyPages(page)).toContainText("Page 2 of 3 · 205 requests");
  await expect(page).toHaveURL(/status=requested&page=2$/);
  await expect(historyStatus(page)).toHaveValue("requested");
  await expect(page.locator(".request-history-section h2")).toHaveText(["Artists (102)", "Release groups (103)"]);
});

test("out-of-range URLs and shrinking histories recover once to page one", async ({ page }) => {
  const { items, reads } = await fixture(page, [], bulkHistory(101));
  reads.length = 0;
  await page.goto("/ada/requests?page=999&q=Bulk+Alias&status=requested");
  await expect(historyPages(page)).toContainText("Page 1 of 2 · 101 requests");
  await expect(page).toHaveURL(/q=Bulk\+Alias&status=requested$/);
  expect(reads.map(read => read.page)).toEqual([999, 1]);
  await nextPage(page).click();
  await expect(page.locator(".history-item")).toHaveCount(1);
  items.pop();
  reads.length = 0;
  await page.reload();
  await expect(historyPages(page)).toContainText("Page 1 of 1 · 100 requests");
  expect(reads.map(read => read.page)).toEqual([2, 1]);
  await expect(page).toHaveURL(/q=Bulk\+Alias&status=requested$/);
  await expect(page.locator(".history-item")).toHaveCount(100);
});

test("search and filter reductions reset a multi-page view without stale state", async ({ page }) => {
  await fixture(page, [], [...bulkHistory(205), { ...artist, id: 999, name: "Unique Artist", availableInPlex: true }]);
  await nextPage(page).click();
  await expect(historyPages(page)).toContainText("Page 2 of 3");
  await historyStatus(page).selectOption("available");
  await expect(historyPages(page)).toContainText("Page 1 of 1 · 1 requests");
  await historyStatus(page).selectOption("all");
  await expect(historyPages(page)).toContainText("Page 1 of 3");
  await nextPage(page).click();
  await historySearch(page).fill("Unique");
  await historySearch(page).press("Enter");
  await expect(historyPages(page)).toContainText("Page 1 of 1 · 1 requests");
  await expect(historyStatus(page)).toHaveValue("all");
  await expect(page).toHaveURL(/q=Unique$/);
});

for (const malformed of ["+1", "01", "1_0", " 1 ", "1\n", "١", "１", "0", "-1", "1.0", "1e2", "", "92233720368547760"]) {
  test(`direct malformed page ${JSON.stringify(malformed)} uses the fixed invalid state`, async ({ page }) => {
    const { reads } = await fixture(page);
    reads.length = 0;
    await page.goto(`/ada/requests?page=${encodeURIComponent(malformed)}&q=again&status=available`);
    await expect(historyMessage(page)).toHaveText("Page must be a positive integer.");
    await expect(page.locator(".history-item")).toHaveCount(0);
    await expect(historyPages(page)).toHaveCount(0);
    expect(reads).toHaveLength(0);
    await expect(historySearch(page)).toHaveValue("again");
    await expect(historyStatus(page)).toHaveValue("available");
    await historySearch(page).press("Enter");
    await expect(historyMessage(page)).toHaveText("1 matching requests.");
    await expect(page).toHaveURL(/q=again&status=available$/);
  });
}

test("Japanese composition waits for committed input and preserves focus", async ({ page }) => {
  const { reads } = await fixture(page, [], [{ ...artist, name: "日本語", aliases: ["Nihongo"] }]);
  await page.clock.install();
  reads.length = 0;
  const search = historySearch(page);
  await search.focus();
  await search.dispatchEvent("compositionstart");
  await search.fill("に");
  await page.clock.fastForward(1000);
  await search.press("Enter");
  expect(reads).toHaveLength(0);
  await expect(search).toHaveValue("に");
  await expect(search).toBeFocused();
  await search.fill("日本語");
  await search.dispatchEvent("compositionend");
  await page.clock.fastForward(250);
  await expect(historyMessage(page)).toHaveText("1 matching requests.");
  expect(reads).toEqual([{ query: "日本語", username: "ada", page: 1 }]);
  await expect(search).toBeFocused();
  await page.getByRole("button", { name: "Clear request search" }).click();
  await expect(historySearch(page)).toHaveValue("");
});

for (const theme of ["midnight", "warm"]) {
  test(`mobile header accommodates wider wordmark metrics in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 280, height: 900 });
    await page.addInitScript(theme => localStorage.setItem("melodarr-theme", theme), theme);
    await fixture(page);
    // Exercise wider system-font metrics on every OS, including Windows where
    // the default wordmark is narrower than Chromium's Linux fallback font.
    await page.addStyleTag({ content: ".brand > span { letter-spacing: .12em; }" });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(280);
    const brand = await page.locator("header .brand").boundingBox();
    let previousRight = brand!.x + brand!.width;
    for (const selector of ["#theme-toggle", "#account-menu", "#logout"]) {
      const control = page.locator(selector);
      const bounds = await control.boundingBox();
      expect(bounds!.width).toBeGreaterThanOrEqual(44);
      expect(bounds!.height).toBeGreaterThanOrEqual(44);
      expect(bounds!.x).toBeGreaterThanOrEqual(previousRight);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(272);
      await control.focus();
      await expect(control).toBeFocused();
      previousRight = bounds!.x + bounds!.width;
    }
    await expect(page.getByRole("link", { name: "Melodarr home" })).toBeVisible();
    expect((await page.locator("header .brand img").boundingBox())!.width).toBe(36);
  });

  test(`header and Requests controls fit at 280px in ${theme} with a bottom safe area`, async ({ page }) => {
    await page.setViewportSize({ width: 280, height: 900 });
    await page.addInitScript(theme => localStorage.setItem("melodarr-theme", theme), theme);
    await fixture(page);
    await page.evaluate(() => document.documentElement.style.setProperty("--safe-bottom", "34px"));
    await historySearch(page).fill("All Time Low");
    await historySearch(page).press("Enter");
    await expect(historyMessage(page)).toHaveText("2 matching requests.");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(280);
    const logout = await page.locator("#logout").boundingBox();
    expect(logout!.width).toBeGreaterThanOrEqual(44);
    expect(logout!.height).toBeGreaterThanOrEqual(44);
    expect(logout!.x + logout!.width).toBeLessThanOrEqual(280);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await expect.poll(() => page.evaluate(() => document.querySelector(".request-pagination")!.getBoundingClientRect().bottom <= document.querySelector(".tab-bar")!.getBoundingClientRect().top)).toBe(true);
  });
}

test("normalization browser results come from the real isolated Requests API", async ({ page }) => {
  await fixture(page, [], [{ ...artist, name: "Café & O'Neil / A-B_🎵 日本語", aliases: ["Romaji Nihongo"] }]);
  for (const query of ["Café", "Cafe\u0301", "cafe", "日本語", "Romaji Nihongo", "O'Neil", "Café & O'Neil", "A/B", "A-B", "A_B", "  cafe   o neil  ", "cafe%", "🎵cafe"]) {
    await historySearch(page).fill(query);
    await historySearch(page).press("Enter");
    await expect(historyMessage(page)).toHaveText("1 matching requests.");
    await expect(page.locator(".history-item")).toHaveCount(1);
  }
  for (const query of ["%", "_", "///", "&&&", "---", "🎵"]) {
    await historySearch(page).fill(query);
    await historySearch(page).press("Enter");
    await expect(historyMessage(page)).toHaveText("No matching requests.");
    await expect(page.locator(".history-item")).toHaveCount(0);
  }
});

test("native input uses UTF-16 maxlength while direct API queries validate Unicode code points", async ({ page }) => {
  await fixture(page);
  await expect(historySearch(page)).toHaveAttribute("maxlength", "500");
  await historySearch(page).fill("日".repeat(501));
  await expect(historySearch(page)).toHaveValue("日".repeat(500));
  await historySearch(page).fill("🎵".repeat(251));
  expect((await historySearch(page).inputValue()).length).toBe(500);
  expect([...(await historySearch(page).inputValue())].length).toBe(250);
  for (const char of ["a", "日", "🎵"]) {
    await page.goto(`/ada/requests?q=${encodeURIComponent(char.repeat(500))}`);
    await expect(historyMessage(page)).toHaveText("No matching requests.");
    await expect(historySearch(page)).toHaveValue(char.repeat(500));
    await page.goto(`/ada/requests?q=${encodeURIComponent(char.repeat(501))}`);
    await expect(historyMessage(page)).toHaveText("Search must be 500 characters or fewer.");
    await expect(historyPages(page)).toHaveCount(0);
    await expect(historySearch(page)).toHaveValue(char.repeat(501));
  }
});

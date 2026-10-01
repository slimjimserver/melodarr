import { expect, test, type Page } from "@playwright/test";

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

async function fixture(page: Page, extra: HistoryItem[] = []) {
  const items = [{ ...artist }, { ...album }, ...extra].sort((a, b) => b.created_at - a.created_at);
  const reads: { query: string; page: number; username: string; status?: string }[] = [];
  const updates: unknown[] = [];
  await page.route("**/api/discover", route => route.fulfill({ json: { sections: [] } }));
  await page.route("**/api/account/profile?*", route => {
    const params = new URL(route.request().url()).searchParams;
    const query = params.get("q") || "";
    const currentPage = Number(params.get("page") || 1);
    const status = params.get("status") || "all";
    reads.push({ query, page: currentPage, username: params.get("username") || "", ...(status !== "all" ? { status } : {}) });
    const key = (value: string) => value.toLocaleLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
    const matches = items.filter(item => (!query || [item.name, item.artist_name, item.anime_name, item.anime_slug, item.song_title, item.theme_label, ...(item.aliases || [])]
      .some(value => value && key(value).includes(key(query)))) &&
      (status === "all" || (item.kind === "artist" ? item.availableInPlex === true : item.requestStatus === "available") === (status === "available")));
    const visible = matches.slice((currentPage - 1) * 100, currentPage * 100);
    return route.fulfill({ json: {
      requests: { artist: visible.filter(item => item.kind === "artist"), "release-group": visible.filter(item => item.kind === "release-group") },
      matchCounts: { artist: matches.filter(item => item.kind === "artist").length, "release-group": matches.filter(item => item.kind === "release-group").length },
      pagination: { page: currentPage, pageSize: 100, total: matches.length, totalPages: Math.ceil(matches.length / 100) },
    } });
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
  return { reads, updates };
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

import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("/__reset");
});

async function openArtist(page: import("@playwright/test").Page) {
  await page.goto("/artists/fixture-artist");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator(".artist-discography")).toBeVisible();
}

const anime = [{
  slug: "switching-anime", name: "The Fable", performances: [
    { animeSlug: "switching-anime", animeName: "The Fable", themeId: 10, themeType: "OP", themeLabel: "Opening 1", songTitle: "Professionalism" },
    { animeSlug: "switching-anime", animeName: "The Fable", themeId: 11, themeType: "ED", themeLabel: "Ending 2", songTitle: "BEYOND" },
  ],
}];

test("Anime follows Singles with flat performances and an external artist link", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/music/artist/fixture-artist/anime", route => {
    reads++;
    return route.fulfill({ json: { anime, artistLinks: [{ id: 916, slug: "ali", name: "ALI" }] } });
  });
  await openArtist(page);
  await expect(page.getByRole("link", { name: "Open on AnimeThemes", exact: true })).toHaveAttribute("href", "https://animethemes.moe/artist/ali");
  expect(reads).toBe(1);
  await expect(page.locator("#artist-anime")).not.toHaveAttribute("open", "");
  const ids = await page.locator(".discography-release-view > details").evaluateAll(nodes => nodes.map(node => node.id));
  expect(ids.indexOf("artist-anime")).toBe(ids.indexOf("release-type-2") + 1);
  await page.locator(".discography-nav").getByRole("link", { name: "Anime", exact: true }).click();
  await expect(page.locator("#artist-anime summary")).toHaveText("Anime (2)");
  await expect(page.locator("#artist-anime").getByText("Opening 1 · The Fable")).toBeVisible();
  await expect(page.locator("#artist-anime").getByText("Ending 2 · The Fable")).toBeVisible();
  await expect(page.locator("#artist-anime h2")).toHaveCount(0);
  await expect(page.locator("#artist-anime .release-anime-theme-link strong")).toHaveText(["Professionalism", "BEYOND"]);
  await page.locator("#artist-anime .release-anime-theme-link").first().click();
  await expect(page).toHaveURL(/\/anime\/switching-anime#theme-10$/);
});

test("Anime errors can be retried and empty results are explicit", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/music/artist/fixture-artist/anime", route => {
    reads++;
    return reads === 1 ? route.fulfill({ status: 502, json: { error: "unavailable" } })
      : route.fulfill({ json: { anime: [] } });
  });
  await openArtist(page);
  await page.locator("#artist-anime summary").click();
  await page.locator("#artist-anime").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#artist-anime summary")).toHaveText("Anime (0)");
  await expect(page.getByText(/No linked anime appearances yet/)).toBeVisible();
});


test("theme sorting supports chronology, titles, and type on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const item = (name: string, song: string, year: number | null, season: string, type: string, id: number) => ({
    name, year, season, slug: `anime-${id}`, performances: [{
      animeName: name, animeSlug: `anime-${id}`, songTitle: song,
      themeType: type, themeLabel: type === "OP" ? "Opening" : "Ending", themeId: id,
    }],
  });
  await page.route("**/api/music/artist/fixture-artist/anime", route => route.fulfill({ json: { anime: [
    item("Zebra", "Alpha", 2024, "Winter", "OP", 1),
    item("Beta", "Zebra", 2024, "Fall", "ED", 2),
    item("Alpha", "Middle", 2020, "Spring", "OP", 3),
    item("Unknown", "Unknown", null, "", "ED", 4),
  ] } }));
  await openArtist(page);
  await page.locator("#artist-anime summary").click();
  const titles = page.locator("#artist-anime .release-anime-theme-link strong");
  await expect(titles).toHaveText(["Zebra", "Alpha", "Middle", "Unknown"]);
  const sort = page.getByLabel("Sort themes");
  await sort.selectOption("oldest");
  await expect(titles).toHaveText(["Middle", "Alpha", "Zebra", "Unknown"]);
  await sort.selectOption("anime");
  await expect(titles).toHaveText(["Middle", "Zebra", "Unknown", "Alpha"]);
  await sort.selectOption("song");
  await expect(titles).toHaveText(["Alpha", "Middle", "Unknown", "Zebra"]);
  await sort.selectOption("type");
  await expect(titles).toHaveText(["Middle", "Alpha", "Zebra", "Unknown"]);
  await expect(page.locator(".external-link-animethemes")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("anime artist credits link directly to verified artists only", async ({ page }) => {
  await page.route("**/api/anime/switching-anime", route => route.fulfill({ json: {
    id: 1, slug: "switching-anime", name: "Switching Anime", themes: [{
      id: 10, type: "OP", sequence: 1,
      song: { title: "Opening Theme", artists: [
        { id: 916, name: "Fixture Artist" }, { id: 999, name: "Unmapped collaborator" },
      ] },
      mapping: { state: "resolved", artistLinks: { "916": "fixture-artist" }, releaseGroups: [] },
    }],
  } }));
  await page.goto("/anime/switching-anime#theme-10");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  const credits = page.locator("#theme-10 .anime-theme-artists");
  await expect(credits).toHaveText("Fixture Artist, Unmapped collaborator");
  await expect(credits.getByRole("link")).toHaveCount(1);
  await expect(credits.getByRole("link")).toHaveAttribute("href", "/artists/fixture-artist");
  await credits.getByRole("link", { name: "Fixture Artist", exact: true }).click();
  await expect(page).toHaveURL(/\/artists\/fixture-artist$/);
  await expect(page.locator(".artist-discography")).toBeVisible();
});

for (const width of [1280, 700, 390, 320]) {
  test(`discography anime stays beside the date without increasing card height at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/music/artist/fixture-artist", route => route.fulfill({ json: {
      id: "fixture-artist", name: "Fixture Artist", sections: { Single: [
        { id: "with-anime", title: "more than words", type: "Single", date: "2023-09-01", animeNames: ["Jujutsu Kaisen 2nd Season", "Another Anime With a Very Long Name"] },
        { id: "without-anime", title: "more than words", type: "Single", date: "2023-09-01" },
      ] },
    } }));
    await openArtist(page);
    const cards = page.locator("#release-type-2 .artist-card");
    await expect(cards).toHaveCount(2);
    const metadata = cards.first().locator(".release-group-metadata");
    await expect(metadata).toHaveText("2023-09-01 · Jujutsu Kaisen 2nd Season · Another Anime With a Very Long Name");
    await expect(metadata).toHaveCSS("white-space", "nowrap");
    await expect(metadata).toHaveCSS("text-overflow", "ellipsis");
    const heights = await cards.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height));
    expect(heights[0]).toBeCloseTo(heights[1], 2);
    const bounds = await cards.first().evaluate(card => {
      const metadata = card.querySelector(".release-group-metadata")!.getBoundingClientRect();
      const button = card.querySelector(".release-group-request")!.getBoundingClientRect();
      const title = card.querySelector("h2")!.getBoundingClientRect();
      return { metadata: { top: metadata.top, right: metadata.right, left: metadata.left },
        button: { bottom: button.bottom, right: button.right, left: button.left },
        title: { left: title.left, right: title.right } };
    });
    if (width <= 700) {
      expect(bounds.metadata.top).toBeGreaterThanOrEqual(bounds.button.bottom);
      expect(bounds.metadata.right).toBeCloseTo(bounds.button.right, 0);
      expect(bounds.metadata.left).toBeCloseTo(bounds.title.left, 0);
      expect(bounds.title.right).toBeLessThan(bounds.button.left);
    } else {
      expect(bounds.metadata.right).toBeLessThan(bounds.button.left);
    }

    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("matched artist themes support request, search missing, and live availability", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const posts: Record<string, unknown>[] = [];
  await page.route("**/api/music/artist/fixture-artist/anime", route => route.fulfill({ json: { anime: [{
    slug: "switching-anime", name: "Anime", performances: [
      { themeId: 10, songId: 100, animeSlug: "switching-anime", animeName: "Anime", themeLabel: "Opening 1", songTitle: "New song", releaseGroups: [{ id: "new-album" }] },
      { themeId: 11, songId: 101, animeSlug: "switching-anime", animeName: "Anime", themeLabel: "Ending 1", songTitle: "Missing song", releaseGroups: [{ id: "fixture-album", availableInLidarr: true }] },
      { themeId: 12, animeSlug: "switching-anime", animeName: "Anime", themeLabel: "Ending 2", songTitle: "Owned song", releaseGroups: [{ id: "owned-album", fullyAvailableInLidarr: true }] },
      { themeId: 13, animeSlug: "switching-anime", animeName: "Anime", themeLabel: "Ending 3", songTitle: "Unmatched song", releaseGroups: [] },
    ],
  }] } }));
  await page.route("**/api/request/release-group", async route => {
    const body = route.request().postDataJSON();
    posts.push(body);
    await route.fulfill({ json: { message: "Accepted", pending: body.mbid === "new-album", alreadyExists: body.mbid === "fixture-album" } });
  });
  await page.route("**/api/music/artist/fixture-artist/availability?*", route => route.fulfill({ json: {
    availableInLidarr: true, settled: false, releaseGroups: {
      "new-album": { availableInLidarr: true, requestStatus: posts.some(p => p.mbid === "new-album") ? "queued" : "requested" },
      "fixture-album": { availableInLidarr: true, fullyAvailableInLidarr: posts.some(p => p.mbid === "fixture-album") },
    },
  } }));
  await openArtist(page);
  await page.locator("#artist-anime summary").click();
  const newCard = page.locator("#artist-anime .artist-anime-action-card").filter({ hasText: "New song" });
  const missingCard = page.locator("#artist-anime .artist-anime-action-card").filter({ hasText: "Missing song" });
  const ownedCard = page.locator("#artist-anime .artist-anime-action-card").filter({ hasText: "Owned song" });
  await expect(ownedCard.getByRole("button", { name: "Available", exact: true })).toBeDisabled();
  await expect(page.locator("#artist-anime .artist-anime-action-card")).toHaveCount(3);
  await newCard.getByRole("button", { name: "Request", exact: true }).click();
  await expect(newCard.getByRole("button", { name: "Queued", exact: true })).toBeDisabled();
  expect(posts[0]).toMatchObject({ mbid: "new-album", animeSlug: "switching-anime", themeId: "10", songId: "100" });
  await expect(page).toHaveURL(/\/artists\/fixture-artist$/);
  await missingCard.getByRole("button", { name: "Search missing", exact: true }).click();
  await expect(missingCard.getByRole("button", { name: "Available", exact: true })).toBeDisabled();
  await expect(page.locator("#release-type-0 [data-release-group-id='fixture-album'] button")).toHaveText("Available");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("anime release cards update download and availability without a page refresh", async ({ page }) => {
  await page.clock.install();
  let detailReads = 0;
  let statusReads = 0;
  await page.route("**/api/anime/switching-anime", route => {
    detailReads += 1;
    return route.fulfill({ json: {
      id: 1, slug: "switching-anime", name: "Switching Anime", themes: [{
        id: 10, type: "OP", sequence: 1,
        song: { id: 100, title: "Opening", artists: [{ name: "Artist" }] },
        mapping: { state: "resolved", releaseGroups: [{
          id: "live-group", title: "Opening", availableInLidarr: true,
        }] },
      }],
    } });
  });
  await page.route("**/api/music/release-groups/availability?*", route => {
    statusReads += 1;
    return route.fulfill({ json: { releaseGroups: { "live-group": statusReads === 1
      ? { availableInLidarr: true, fullyAvailableInLidarr: false,
          requestStatus: "downloading", downloadStatus: { progress: 42 },
          availableInPlex: false, plexReleases: [] }
      : { availableInLidarr: true, fullyAvailableInLidarr: true,
          requestStatus: "available", downloadStatus: null, availableInPlex: true,
          plexReleases: [{ url: "https://app.plex.tv/owned" }] },
    } } });
  });
  await page.goto("/anime/switching-anime#theme-10");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  const card = page.locator("#theme-10 .anime-release-candidate[data-release-group-id='live-group']");
  await expect(card.getByRole("button", { name: "Search missing" })).toBeVisible();
  const detailReadsBeforePolling = detailReads;

  await page.clock.runFor(5_000);
  await expect(card.getByRole("button", { name: "Downloading 42%" })).toBeDisabled();
  await page.clock.runFor(15_000);
  await expect(card.getByRole("button", { name: "Available" })).toBeDisabled();
  await expect(card.locator(".anime-candidate-plex")).toHaveAttribute("href", "https://app.plex.tv/owned");
  expect(statusReads).toBe(2);
  expect(detailReads).toBe(detailReadsBeforePolling);
});

test("multiple matched releases require a selection when no preferred target exists", async ({ page }) => {
  await page.route("**/api/music/artist/fixture-artist/anime", route => route.fulfill({ json: { anime: [{
    performances: [{ animeSlug: "switching-anime", animeName: "Anime", themeId: 10, songId: 1, themeLabel: "Opening", songTitle: "Choose song", releaseGroups: [
      { id: "first", title: "Burning" }, { id: "second", title: "D o n’t L a u g h I t O f f", availableInLidarr: true },
    ] }],
  }] } }));
  await openArtist(page);
  await page.locator("#artist-anime summary").click();
  await expect(page.locator("#artist-anime").getByRole("button", { name: "Choose release", exact: true })).toBeDisabled();
  await expect(page.getByLabel("Release for Choose song").locator("option")).toHaveText([
    "Choose release…", "Burning", "D o n’t L a u g h I t O f f",
  ]);
  const navigation = page.locator("#artist-anime .artist-anime-action-card > .release-anime-theme-link");
  await expect(navigation).toHaveCSS("border-radius", "0px");
  await expect(navigation).toHaveCSS("overflow", "visible");
  await page.getByLabel("Release for Choose song").selectOption("second");
  await expect(page.locator("#artist-anime").getByRole("button", { name: "Search missing", exact: true })).toBeEnabled();
});

test("each resolved release can be confirmed in Manage mappings", async ({ page }) => {
  const groups = [
    { id: "first-single", title: "First Single", type: "Single" },
    { id: "second-single", title: "Second Single", type: "Single" },
    { id: "album", title: "Album", type: "Album" },
    { id: "ep", title: "EP", type: "EP" },
  ];
  const mapping = { state: "resolved", matchMethod: "recording-search", recordingId: "recording",
    recordingTitle: "Song", releaseGroups: groups };
  await page.route("**/api/anime/switching-anime", route => route.fulfill({ json: {
    id: 1, slug: "switching-anime", name: "Switching Anime", themes: [{
      id: 10, type: "OP", sequence: 1, song: { id: 10, title: "Song", artists: [{ name: "Artist" }] }, mapping,
    }],
  } }));
  let submitted: any;
  await page.route("**/themes/10/mapping", route => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: { mapping: { ...mapping, mappingSource: "local", registryStatus: "confirmed",
      registryProvenance: "manual-confirmation", releaseGroups: [groups[1]] } } });
  });
  await page.goto("/anime/switching-anime#theme-10");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("button", { name: "Confirm this release", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Manage mappings", exact: true }).click();
  await page.getByRole("button", { name: "Show all 4" }).click();
  const confirm = page.getByRole("button", { name: "Confirm this release", exact: true });
  await expect(confirm).toHaveCount(4);
  await expect(page.getByRole("button", { name: "Confirm recommended match", exact: true })).toHaveCount(0);
  await expect(page.locator(".anime-recommended")).toHaveCount(1);
  await confirm.nth(1).click();
  await expect(confirm).toHaveCount(0);
  expect(submitted).toEqual({ confirmAutomatic: true, releaseGroup: "second-single" });
  await expect(page.locator(".anime-release-candidates").getByText("Second Single", { exact: true })).toBeVisible();
});

test("ambiguous release choices always recommend an available candidate without confirming it", async ({ page }) => {
  await page.route("**/api/anime/switching-anime", route => route.fulfill({ json: {
    id: 1, slug: "switching-anime", name: "Switching Anime", themes: [{
      id: 10, type: "OP", sequence: 1, song: { id: 10, title: "Rising Hope", artists: [{ name: "LiSA" }] },
      mapping: { state: "ambiguous", matchMethod: "recording-search", recommendedReleaseGroupId: "removed-candidate",
        releaseGroups: [
          { id: "single", title: "Rising Hope", type: "Single" },
          { id: "ep", title: "LADYBUG", type: "EP" },
          { id: "album", title: "Launcher", type: "Album" },
        ] },
    }],
  } }));
  await page.goto("/anime/switching-anime#theme-10");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("#theme-10 .anime-mapping-status")).toHaveText("Choose a release");
  await expect(page.locator("#theme-10 .anime-recommended")).toHaveCount(1);
  await expect(page.locator("#theme-10 .anime-release-candidates > *").first()).toContainText("Recommended");
  await page.getByRole("button", { name: "Manage mappings", exact: true }).click();
  await expect(page.getByRole("button", { name: "Confirm this release", exact: true })).toHaveCount(3);
});


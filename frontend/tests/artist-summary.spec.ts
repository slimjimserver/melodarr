import { expect, test, type Page } from "@playwright/test";

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });

const group = { id: "fixture-album", title: "Canonical Album", coverArt: "/api/artwork/release-group/fixture-album" };
const tracks = [
  { position: 1, deezer_track_id: 408766392, title: "Sativa", rank: 1, recording_mbid: "recording-one", release_group_mbid: group.id, album: { title: "Provider Album" } },
  { position: 2, deezer_track_id: 4278790372, title: "Nothing Nice to Say", rank: 999, recording_mbid: "recording-two", release_group_mbid: group.id, album: { title: "Provider Album" } },
  { position: 3, deezer_track_id: 3, title: "Unresolved", recording_mbid: "recording-three", release_group_mbid: null, album: { title: "Unmapped Album" } },
];
const bio = { text: "A short artist biography.", sourceUrl: "https://en.wikipedia.org/wiki/Artist" };

async function openArtist(page: Page, canonicalGroup = group) {
  await page.route("**/api/artwork/**", route => route.fulfill({ path: "icons/melodarr-512.png", contentType: "image/png" }));
  // Canonical display metadata wins over provider album context.
  await page.route("**/api/music/artist/fixture-artist", route => route.fulfill({ json: {
    id: "fixture-artist", name: "Fixture Artist", sections: { Album: [{ ...canonicalGroup, type: "Album", date: "2026-01-01" }] },
  } }));
  await page.route("**/api/music/artist/fixture-artist/availability*", route => route.fulfill({ json: { settled: true, releaseGroups: {} } }));
  await page.goto("/artists/fixture-artist");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator(".artist-discography")).toBeVisible();
}

test("summary is lazy and slow supplements leave artist navigation usable", async ({ page }) => {
  let reads = 0;
  let release: (() => void) | undefined;
  await page.route("**/api/music/artist/fixture-artist/summary", async route => {
    reads++;
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ json: { bio, topTracks: [], pending: false } });
  });
  await openArtist(page);
  expect(reads).toBe(0);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await expect(page.locator("#artist-summary-view")).toContainText("Loading summary…");
  await page.locator(".discography-nav").getByRole("link", { name: "Albums", exact: true }).click();
  await expect(page.locator("#release-type-0")).toBeVisible();
  release!();
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await expect(page.locator("#artist-summary-view")).toContainText(bio.text);
  expect(reads).toBe(1);
});

for (const [name, payload] of [
  ["bio only when Deezer fails", { bio, topTracks: [], pending: false }],
  ["top tracks only when Wikipedia fails", { bio: null, topTracks: tracks, releaseGroups: { [group.id]: group }, pending: false }],
  ["both sources", { bio, topTracks: tracks, releaseGroups: { [group.id]: group }, pending: false }],
] as const) {
  test(name, async ({ page }) => {
    await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: payload }));
    await openArtist(page);
    await page.getByRole("button", { name: "Summary", exact: true }).click();
    const view = page.locator("#artist-summary-view");
    await expect(view.locator(".artist-summary-bio")).toHaveCount(payload.bio ? 1 : 0);
    await expect(view.locator(".artist-top-tracks li")).toHaveCount(payload.topTracks.length);
    if (payload.bio) await expect(view.getByRole("link", { name: "Wikipedia", exact: true })).toHaveAttribute("href", bio.sourceUrl);
  });
}

test("ordering and canonical context survive; unresolved rows have no request", async ({ page }) => {
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: { bio: null, topTracks: tracks, releaseGroups: { [group.id]: group }, pending: false } }));
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  const view = page.locator("#artist-summary-view");
  await expect(view.locator(".artist-top-track h2")).toHaveText(["Sativa", "Nothing Nice to Say", "Unresolved"]);
  await expect(view.locator(".artist-top-track .artist-info p")).toHaveText(["Canonical Album", "Canonical Album", "Unmapped Album"]);
  await expect(view.locator("li").last().getByRole("button")).toHaveCount(0);
  await expect(view.locator(".artist-top-track").first().locator("img")).toHaveAttribute("src", /\/api\/artwork\/release-group\/fixture-album/);
});

test("request targets RG and duplicate tracks share pending and canonical state", async ({ page }) => {
  let sent: unknown;
  let release: (() => void) | undefined;
  await page.route("**/api/request/release-group", async route => {
    sent = route.request().postDataJSON();
    await new Promise<void>(resolve => { release = resolve; });
    await route.fulfill({ json: { pending: true, message: "Queued" } });
  });
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: { topTracks: tracks, releaseGroups: { [group.id]: group }, pending: false } }));
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  const buttons = page.locator("#artist-summary-view .release-group-request");
  await buttons.first().click();
  await expect(buttons).toHaveText(["Sending to Lidarr…", "Sending to Lidarr…"]);
  expect(sent).toEqual({ mbid: group.id });
  release!();
  await expect(buttons).toHaveText(["Queued", "Queued"]);
  await expect(buttons.first()).toBeDisabled();
  await expect(buttons.last()).toBeDisabled();
  await page.locator(".discography-nav").getByRole("link", { name: "Albums", exact: true }).click();
  await expect(page.locator("#release-type-0 .release-group-request")).toHaveText("Queued");
});

test("summary fills missing canonical artwork and shares existing Available state", async ({ page }) => {
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
    topTracks: tracks, releaseGroups: { [group.id]: { ...group, fullyAvailableInLidarr: true } }, pending: false,
  } }));
  await openArtist(page, { ...group, coverArt: "" });
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  const view = page.locator("#artist-summary-view");
  await expect(view.locator(".release-group-request")).toHaveText(["Available", "Available"]);
  await expect(view.locator(".release-group-request").first()).toBeDisabled();
  await expect(view.locator(".release-group-request").last()).toBeDisabled();
  await expect(view.locator("img").first()).toHaveAttribute("src", /\/api\/artwork\/release-group\/fixture-album/);
});

test("stale content renders while revalidation completes independently", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/music/artist/fixture-artist/summary", route => {
    reads++;
    return route.fulfill({ json: reads === 1 ? { bio, topTracks: [], pending: true }
      : { bio, topTracks: tracks, releaseGroups: { [group.id]: group }, pending: false } });
  });
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await expect(page.locator(".artist-summary-bio")).toHaveText(bio.text);
  await expect(page.locator("#artist-summary-view li")).toHaveCount(3, { timeout: 10000 });
  expect(reads).toBe(2);
});

test("empty and failed responses degrade quietly and can be retried", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/music/artist/fixture-artist/summary", route => {
    reads++;
    return reads === 1 ? route.fulfill({ status: 502, json: { error: "offline" } })
      : route.fulfill({ json: { bio: null, topTracks: [], pending: false } });
  });
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await page.locator("#artist-summary-view").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#artist-summary-view")).toContainText("No summary available yet.");
  await page.locator(".discography-nav").getByRole("link", { name: "Albums", exact: true }).click();
  await expect(page.locator("#release-type-0 .release-group-request")).toBeEnabled();
});

for (const width of [1280, 700, 390, 320]) {
  test(`summary fits ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
      bio: { ...bio, text: bio.text.repeat(10) }, topTracks: tracks.map(track => ({ ...track, title: track.title.repeat(8) })),
      releaseGroups: { [group.id]: group }, pending: false,
    } }));
    await openArtist(page);
    await page.getByRole("button", { name: "Summary", exact: true }).click();
    await expect(page.locator("#artist-summary-view li")).toHaveCount(3);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.locator("#artist-summary-view .release-group-request").first()).toBeVisible();
  });
}

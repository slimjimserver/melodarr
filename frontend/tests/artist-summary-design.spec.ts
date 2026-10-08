import { expect, test, type Page } from "@playwright/test";

const biography = "The artist combines pop melodies with a distinctive voice and thoughtful songwriting. "
  + "Early performances led to a recording career and a growing audience around the world. "
  + "The debut album introduced a warm sound, followed by records exploring new styles and collaborations. "
  + "Live performances bring these songs to life with an energetic band and intimate acoustic arrangements. "
  + "Later projects reflect a lasting interest in storytelling, creative partnerships, and musical experimentation. "
  + "The artist continues to record and perform, connecting with listeners through songs about everyday life.";
const sourceUrl = "https://en.wikipedia.org/wiki/Artist";
const groups = [
  { id: "fixture-album", title: "A canonical album with a comfortably wrapping subtitle", fullyAvailableInLidarr: true },
  { id: "new-album", title: "A new album" },
  { id: "queued-album", title: "A queued album", requestStatus: "queued" },
  { id: "partial-album", title: "A partially available album", availableInLidarr: true },
].map(group => ({ ...group, coverArt: `/api/artwork/release-group/${group.id}`, type: "Album", date: "2026-01-01" }));
const releaseGroups = Object.fromEntries(groups.map(group => [group.id, group]));
const tracks = [
  ...groups.map((group, index) => ({ position: index + 1, deezer_track_id: index + 1,
    title: index === 0 ? "A long track title (including a featured artist and an extended live version)" : `Track ${index + 1}`,
    release_group_mbid: group.id, album: { title: "Provider album" }, pending: false,
  })),
  { position: 5, deezer_track_id: 5, title: "A pending track with another long title", release_group_mbid: null,
    artist: { name: "Fixture Artist" }, album: { title: "Provider album" }, pending: true,
  },
];

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });

async function openArtist(page: Page) {
  await page.route("**/api/artwork/**", route => route.fulfill({ path: "icons/melodarr-512.png", contentType: "image/png" }));
  await page.route("**/api/music/artist/fixture-artist", route => route.fulfill({ json: {
    id: "fixture-artist", name: "Fixture Artist", sections: { Album: groups },
  } }));
  await page.route("**/api/music/artist/fixture-artist/availability*", route => route.fulfill({ json: { settled: true, releaseGroups } }));
  await page.goto("/artists/fixture-artist");
  await page.locator("#login-form").getByLabel("Username").fill("ada");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator(".artist-discography")).toBeVisible();
}

test("biography uses whole-word ellipsis and keyboard expansion retains the source link", async ({ page }) => {
  const fullText = biography.repeat(3);
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
    bio: { text: fullText, sourceUrl }, topTracks: [], pending: false,
  } }));
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  const card = page.getByRole("region", { name: "Biography", exact: true });
  const paragraph = card.locator(".artist-summary-bio");
  const collapsed = await paragraph.innerText();
  expect(collapsed).toMatch(/\S…$/);
  expect(fullText.startsWith(collapsed.slice(0, -1))).toBe(true);
  expect(fullText[collapsed.length - 1]).toMatch(/[\s,;:.]/);
  const toggle = card.locator(".artist-biography-toggle");
  await expect(toggle).toHaveAccessibleName("Show more");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toHaveAttribute("aria-controls", await paragraph.getAttribute("id") as string);
  await toggle.press("Enter");
  await expect(paragraph).toHaveText(fullText);
  await expect(toggle).toBeFocused();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(card.getByRole("link", { name: "Wikipedia", exact: true })).toHaveAttribute("href", sourceUrl);
  await toggle.press("Space");
  await expect(paragraph).toHaveText(collapsed);
  await expect(toggle).toHaveText("Show more");
  await expect(toggle).toBeFocused();
});

test("the existing capped API extract ends cleanly in the expanded view", async ({ page }) => {
  const rawText = biography.repeat(3).slice(0, 1000);
  const expected = rawText.replace(/\s+\S*$/, "").replace(/[\s,;:–—-]+$/, "") + "…";
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
    bio: { text: rawText, sourceUrl }, topTracks: [], pending: false,
  } }));
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await page.getByRole("button", { name: "Show more", exact: true }).click();
  await expect(page.locator(".artist-summary-bio")).toHaveText(expected);
  await expect(page.getByRole("link", { name: "Wikipedia", exact: true })).toBeVisible();
});

test("short biographies stay complete without a redundant disclosure", async ({ page }) => {
  const text = "A concise biography with a complete final word";
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
    bio: { text, sourceUrl: "javascript:alert(1)" }, topTracks: [], pending: false,
  } }));
  await openArtist(page);
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await expect(page.locator(".artist-summary-bio")).toHaveText(text);
  await expect(page.getByRole("button", { name: "Show more" })).toHaveCount(0);
  await expect(page.locator(".artist-biography-card a")).toHaveCount(0);
});

test("expanded biography survives progressive track updates and tab changes", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
    bio: { text: biography, sourceUrl }, topTracks: ++reads === 1 ? [] : tracks.slice(0, 4),
    releaseGroups, pending: reads === 1,
  } }));
  await page.clock.install({ time: new Date("2026-10-07T12:00:00Z") });
  await openArtist(page);
  await page.clock.pauseAt(new Date("2026-10-07T13:00:00Z"));
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await page.getByRole("button", { name: "Show more", exact: true }).click();
  await expect(page.locator(".artist-summary-bio")).toHaveText(biography);
  await page.clock.runFor(350);
  await expect(page.locator(".artist-top-tracks li")).toHaveCount(4);
  await expect(page.getByRole("button", { name: "Show less", exact: true })).toHaveAttribute("aria-expanded", "true");
  await page.locator(".discography-nav").getByRole("link", { name: "Albums", exact: true }).click();
  await page.getByRole("button", { name: "Summary", exact: true }).click();
  await expect(page.locator(".artist-summary-bio")).toHaveText(biography);
  expect(reads).toBe(2);
});

for (const width of [1280, 700, 390, 320]) {
  for (const theme of ["warm", "midnight"]) {
    test(`shared cards align at ${width}px in ${theme} theme`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(value => localStorage.setItem("melodarr-theme", value), theme);
      await page.route("**/api/music/artist/fixture-artist/summary", route => route.fulfill({ json: {
        bio: { text: biography, sourceUrl }, topTracks: tracks, releaseGroups, pending: false,
      } }));
      await openArtist(page);
      const normal = await page.locator("#release-type-0 .release-card").first().boundingBox();
      await page.getByRole("button", { name: "Summary", exact: true }).click();
      const list = page.getByRole("list", { name: "Top Tracks from Deezer" });
      await expect(list.locator("li")).toHaveCount(5);
      await expect(list.locator("h2")).toHaveText(tracks.map(track => track.title));
      await expect(list.locator(".release-group-request")).toHaveText(["Available", "Request Album", "Queued", "Search missing"]);
      await expect(list.locator(".release-group-request").first()).toBeDisabled();
      await expect(list.locator(".release-group-request").nth(2)).toBeDisabled();
      await expect(list.locator(".artist-top-track-status")).toHaveText("Finding album…");
      const measurements = await list.evaluate(element => {
        return Array.from(element.querySelectorAll(".release-card")).map(card => {
          const bounds = card.getBoundingClientRect();
          const title = card.querySelector("h2")!.getBoundingClientRect();
          const subtitle = card.querySelector(".artist-info p")!.getBoundingClientRect();
          const action = card.querySelector(".release-group-request, .release-card-state")!.getBoundingClientRect();
          const artwork = card.querySelector(".cover-art, .avatar")!.getBoundingClientRect();
          return { x: bounds.x, right: bounds.right, titleX: title.x, subtitleX: subtitle.x, artworkX: artwork.x,
            overlap: Math.min(title.right, action.right) > Math.max(title.left, action.left)
              && Math.min(title.bottom, action.bottom) > Math.max(title.top, action.top),
            actionRight: action.right, actionBottom: action.bottom, cardBottom: bounds.bottom,
          };
        });
      });
      for (const card of measurements) {
        expect(card.x).toBeCloseTo(normal!.x, 1);
        expect(card.right).toBeCloseTo(normal!.x + normal!.width, 1);
        expect(card.titleX).toBeCloseTo(measurements[0].titleX, 1);
        expect(card.subtitleX).toBeCloseTo(card.titleX, 1);
        expect(card.artworkX).toBeCloseTo(measurements[0].artworkX, 1);
        expect(card.overlap).toBe(false);
        expect(card.actionRight).toBeLessThanOrEqual(card.right);
        expect(card.actionBottom).toBeLessThanOrEqual(card.cardBottom);
      }
      expect(await list.evaluate(element => getComputedStyle(element).paddingLeft)).toBe("0px");
      expect(await list.evaluate(element => getComputedStyle(element).listStyleType)).toBe("none");
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.getByRole("button", { name: "Show more", exact: true }).click();
      await expect(page.locator(".artist-summary-bio")).toHaveText(biography);
      await page.screenshot({ path: testInfo.outputPath("summary-expanded.png"), fullPage: true });
      await page.getByRole("button", { name: "Show less", exact: true }).click();
      await page.screenshot({ path: testInfo.outputPath("summary-collapsed.png"), fullPage: true });
    });
  }
}

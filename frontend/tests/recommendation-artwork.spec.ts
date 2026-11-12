import { expect, test, type Locator, type Route } from "@playwright/test";

const variants = ["personal albums", "popular albums", "legacy albums", "legacy artists"] as const;
type Variant = typeof variants[number];

const artworkSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="320"><rect width="640" height="320" fill="#746acb"/></svg>';

function artworkItems(kind = "release-group") {
  return ["real", "missing", "failed"].map((state, index) => ({
    id: `artwork-${state}`,
    kind,
    name: `Artwork ${index + 1}`,
    artist: "Fixture Artist",
    type: kind === "artist" ? "Artist" : "Album",
    reason: "Because you requested a favorite album",
    recentRelease: true,
    coverArt: state === "missing" ? "" : `/api/artwork/${kind}/${state}`,
  }));
}

function feed(variant: Variant) {
  const base = { feedVersion: 5, refreshedAt: 1788652800, sections: [] };
  if (variant === "personal albums") {
    return {
      ...base,
      sections: [{ id: "familiar", title: "More from artists you love", items: artworkItems() }],
    };
  }
  if (variant === "popular albums") {
    return { ...base, popularChart: { status: "ok" }, popularAlbums: artworkItems() };
  }
  return {
    ...base,
    feedVersion: 1,
    ...(variant === "legacy artists" ? { artists: artworkItems("artist") } : { albums: artworkItems() }),
  };
}

async function layout(cards: Locator) {
  return cards.evaluateAll(elements => elements.map(card => {
    const rect = card.getBoundingClientRect();
    const origin = card.closest(".recommendation-carousel")?.getBoundingClientRect().top ?? rect.top;
    const art = card.querySelector(".recommendation-art")!;
    const artwork = art.getBoundingClientRect();
    const style = getComputedStyle(art);
    return {
      width: artwork.width,
      height: artwork.height,
      radius: style.borderRadius,
      overflow: style.overflow,
      artworkTop: artwork.top - origin,
      infoTop: card.querySelector(".recommendation-info")!.getBoundingClientRect().top - origin,
      requestTop: card.querySelector(".recommendation-request")!.getBoundingClientRect().top - origin,
      cardHeight: rect.height,
    };
  }));
}

async function expectAligned(cards: Locator, size: number) {
  const boxes = await layout(cards);
  expect(boxes).toHaveLength(3);
  for (const box of boxes) {
    expect(box.width).toBe(size);
    expect(box.height).toBe(size);
    expect(box).toEqual(boxes[0]);
  }
  return boxes;
}

async function releaseArtwork(routes: Route[]) {
  for (const route of routes) {
    if (new URL(route.request().url()).pathname.endsWith("/failed")) {
      await route.fulfill({ status: 404 });
    } else {
      await route.fulfill({ contentType: "image/svg+xml", body: artworkSvg });
    }
  }
}

async function expectSettled(cards: Locator) {
  const image = cards.nth(0).locator("img").first();
  await expect.poll(() => image.evaluate(node => (node as HTMLImageElement).naturalWidth)).toBe(640);
  await expect(cards.nth(1).locator(".recommendation-fallback")).toHaveCount(1);
  await expect(cards.nth(2).locator(".recommendation-fallback")).toHaveCount(1);
  await expect(cards.nth(2).locator("img")).toHaveCount(0);
  await expect(image).toHaveCSS("object-fit", "cover");
}

test.beforeEach(async ({ request, page }) => {
  await request.post("/__reset");
  await request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
  await page.route("**/api/settings", route => route.fulfill({ json: { lidarr: {}, plex: {} } }));
  await page.route("**/api/discover/activity", route => route.fulfill({ json: { ok: true } }));
  await page.emulateMedia({ reducedMotion: "reduce" });
});

for (const width of [1280, 390]) {
  test.describe(`artwork at ${width}px`, () => {
    test.use({ viewport: { width, height: 900 } });

    for (const variant of variants) {
      test(`${variant} align before and after artwork loads or fails`, async ({ page }) => {
        let discover: Route | undefined;
        const artwork: Route[] = [];
        await page.route("**/api/discover", route => { discover = route; });
        await page.route("**/api/artwork/**", route => { artwork.push(route); });
        await page.goto("/", { waitUntil: "domcontentloaded" });

        const skeleton = page.locator("#recommendation-results .skeleton-art");
        await expect(skeleton).toHaveCount(8);
        const skeletonBox = await skeleton.first().boundingBox();
        expect(skeletonBox!.width).toBe(width > 700 ? 210 : 176);
        expect(skeletonBox!.height).toBe(skeletonBox!.width);
        await discover!.fulfill({ json: feed(variant) });

        const cards = page.locator("#recommendation-results .recommendation-card");
        await expect(cards).toHaveCount(3);
        await cards.last().scrollIntoViewIfNeeded();
        await cards.first().scrollIntoViewIfNeeded();
        await expect.poll(() => artwork.length).toBe(2);
        const personal = variant === "personal albums" || variant === "popular albums";
        const size = personal ? (width > 700 ? 210 : 176) : (width > 700 ? 154 : 132);
        const before = await expectAligned(cards, size);
        if (personal) expect(before[0].width).toBe(skeletonBox!.width);

        await releaseArtwork(artwork);
        await expectSettled(cards);
        const after = await expectAligned(cards, size);
        expect(after).toEqual(before);
        expect(after[0].radius).toBe("12px");
        expect(after[0].overflow).toBe("hidden");
        await expect(cards.locator(".recommendation-open > .recommendation-art")).toHaveCount(3);
        await expect(cards.nth(0).locator(".recommendation-art > img")).toHaveCount(1);
        await expect(cards.nth(1).locator(".recommendation-art > .recommendation-fallback")).toHaveCount(1);
        await expect(cards.nth(2).locator(".recommendation-art > .recommendation-fallback")).toHaveCount(1);
        if (variant === "personal albums") {
          await cards.first().locator("..").screenshot({ path: test.info().outputPath("artwork-alignment.png") });
        }
      });
    }

    test("similar artists keep their compact artwork frame when images fail", async ({ page }) => {
      const artwork: Route[] = [];
      await page.route("**/api/discover", route => route.fulfill({ json: feed("personal albums") }));
      await page.route("**/api/music/artist/fixture-artist/similar?*", route => route.fulfill({ json: {
        artists: artworkItems("artist"), configured: true, hasMore: false, pending: 0,
      } }));
      await page.route("**/api/artwork/artist/**", route => { artwork.push(route); });
      await page.goto("/artists/fixture-artist", { waitUntil: "domcontentloaded" });
      await page.getByRole("button", { name: "Similar artists", exact: true }).click();
      const cards = page.locator(".similar-artists-list .recommendation-card");
      await expect(cards).toHaveCount(3);
      await cards.first().scrollIntoViewIfNeeded();
      await page.mouse.move(0, 0);
      await expect(cards.first().locator(".recommendation-art")).toHaveCSS("transform", "none");
      await expect.poll(() => artwork.length).toBe(2);
      const before = await expectAligned(cards, 64);
      await releaseArtwork(artwork);
      await expectSettled(cards);
      const after = await expectAligned(cards, 64);
      expect(after).toEqual(before);
      expect(after[0].radius).toBe("10px");
      expect(after[0].overflow).toBe("hidden");
    });
  });
}

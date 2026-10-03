import { expect, test, type Page } from "@playwright/test";

const code = "ABCDEFGHJK";
const plexArt = `/api/rooms/${code}/plex-artwork/plex-album-${"1".repeat(64)}`;
const releaseArt = `/api/rooms/${code}/artwork/22222222-2222-4222-8222-222222222222`;
const state = {
  code, status: "active", version: 3, joinPath: `/rooms/${code}`, guestCount: 2,
  nowPlaying: { title: "Jaded", artist: "Track artist", album: "The album", artwork: plexArt, artworkFallback: releaseArt },
  handoff: {}, upNext: { title: "Next song", artist: "Next artist", album: "Next album", artwork: plexArt },
  playbackState: "playing", queueWarning: false, syncError: null,
  queue: [
    { id: "locked", title: "Next song", artist: "Next artist", album: "Next album", requester: null, state: "ready", locked: true, artwork: plexArt, artworkFallback: "", recordingMbid: "recording-1" },
    { id: "pending", title: "Requested song", artist: "Guest artist", album: "Requested album", requester: "jrampersaud123", state: "downloading", locked: false, artwork: releaseArt, artworkFallback: "", recordingMbid: "recording-2" },
    { id: "empty", title: "Uncovered song", artist: "Uncovered artist", album: "", requester: null, state: "requested", locked: false, artwork: "", artworkFallback: "", recordingMbid: null },
  ],
};
type Snapshot = typeof state;
type RoomWindow = Window & { roomSource?: EventSource; retainedRow?: Element; retainedHero?: Element; retainedFocus?: Element };

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

async function openRoom(page: Page, snapshot: Snapshot = state, isHost = true) {
  // A controllable SSE transport exercises the existing room-event handler
  // without reconnecting the fixture to an older snapshot midway through QA.
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget {
      readyState = 1;
      constructor() { super(); (window as RoomWindow).roomSource = this as unknown as EventSource; }
      close() { this.readyState = 2; }
    } as unknown as typeof EventSource;
  });
  await page.route("**/*artwork/**", route => route.fulfill({
    contentType: "image/svg+xml",
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640"><rect width="640" height="640" fill="#8d3523"/><circle cx="320" cy="320" r="190" fill="#e7ac78"/><circle cx="320" cy="320" r="60" fill="#2d2925"/></svg>',
  }));
  if (isHost) {
    await page.request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
    await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: snapshot } }));
    await page.goto("/rooms");
  } else {
    await page.route(`**/api/rooms/${code}/join`, route => route.fulfill({ json: { room: snapshot, guest: { name: "Guest", csrfToken: "guest-csrf" } } }));
    await page.goto(`/rooms/${code}`);
    await page.getByRole("button", { name: "Join Room" }).click();
  }
  await expect(page.getByRole("region", { name: "Now Playing" })).toBeVisible();
}
async function update(page: Page, snapshot: Snapshot) {
  await page.evaluate(value => (window as RoomWindow).roomSource?.dispatchEvent(new MessageEvent("room", { data: JSON.stringify(value) })), snapshot);
}

test("Now Playing has eager square artwork, title, track artist and album without playback controls", async ({ page }) => {
  await openRoom(page);
  const hero = page.getByRole("region", { name: "Now Playing" });
  await expect(hero.getByRole("heading", { name: "Jaded" })).toBeVisible();
  await expect(hero.getByText("Track artist", { exact: true })).toBeVisible();
  await expect(hero.getByText("The album", { exact: true })).toBeVisible();
  await expect(hero.getByRole("img", { name: "Album artwork for The album by Track artist" })).toHaveAttribute("src", `${plexArt}?size=large`);
  await expect(hero.locator("img")).toHaveAttribute("loading", "eager");
  const art = await hero.locator(".room-artwork").boundingBox();
  expect(art!.width).toBeCloseTo(art!.height, 1);
  await expect(page.getByRole("button", { name: /^(play|pause|next|previous|volume)$/i })).toHaveCount(0);
  await expect(page.getByRole("slider")).toHaveCount(0);
  await page.screenshot({ path: "test-results/rooms-player-desktop.png", fullPage: true });
});

test("Plex-adopted rows use cached artwork, title and artist and omit null requesters", async ({ page }) => {
  await openRoom(page);
  const row = page.locator('[data-entry-id="locked"]');
  await expect(row.locator("img")).toHaveAttribute("src", `${plexArt}?size=thumb`);
  await expect(row.locator("img")).toHaveAttribute("loading", "lazy");
  await row.scrollIntoViewIfNeeded();
  await expect(row.locator(".room-artwork")).toHaveClass(/has-artwork/);
  await expect(row.getByText("Next song", { exact: true })).toBeVisible();
  await expect(row.getByText("Next artist", { exact: true })).toBeVisible();
  await expect(row.locator(".room-requester")).toBeHidden();
  await expect(row).not.toContainText(/null|Added from Plex|Autoplay|Host-added/);
  await expect(page.getByRole("heading", { name: "Up Next", exact: true })).toBeVisible();
});

test("pending rows use release-group covers and missing artwork has an accessible placeholder", async ({ page }) => {
  await openRoom(page);
  await expect(page.locator('[data-entry-id="pending"] img')).toHaveAttribute("src", `${releaseArt}?size=thumb`);
  const row = page.locator('[data-entry-id="empty"]');
  await expect(row.getByRole("img", { name: "Album artwork unavailable" })).toBeVisible();
  await expect(row.locator("img")).toBeHidden();
  expect(await row.locator(".room-artwork").evaluate(node => node.clientWidth === node.clientHeight)).toBe(true);
});

test("failed Plex images fall back to release-group artwork then a stable placeholder", async ({ page }) => {
  await openRoom(page);
  await page.route("**/plex-artwork/**", route => route.fulfill({ status: 404 }));
  await update(page, { ...state, version: 4, nowPlaying: { ...state.nowPlaying, artwork: `${plexArt}-new` } });
  await expect(page.locator(".room-now-playing img")).toHaveAttribute("src", `${releaseArt}?size=large`);
  await expect(page.locator(".room-now-playing img")).toBeVisible();
  await page.route("**/artwork/**", route => route.fulfill({ status: 404 }));
  await update(page, { ...state, version: 5, nowPlaying: { ...state.nowPlaying, artwork: `${plexArt}-newer`, artworkFallback: `${releaseArt}?revision=missing` } });
  await expect(page.locator(".room-now-playing").getByRole("img", { name: "Album artwork unavailable" })).toBeVisible();
});

test("locked Up Next is subtly identified and all host controls stay disabled", async ({ page }) => {
  await openRoom(page);
  const row = page.locator('[data-entry-id="locked"]');
  await expect(row.getByText("Up Next · Locked")).toBeVisible();
  for (const name of ["Move Next song up", "Move Next song down", "Remove Next song"]) {
    await expect(row.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await expect(page.getByRole("button", { name: "Move Requested song up" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Remove Requested song" })).toBeEnabled();
});

test("host lifecycle pills retain acquisition detail while guest presentation only shows Requested and Ready", async ({ page }) => {
  await openRoom(page);
  for (const [lifecycle, label] of [["requested", "Requested"], ["downloading", "Downloading"], ["waiting_for_plex", "Waiting for Plex"], ["waiting_for_queue", "Ready · Waiting for queue"], ["ready", "Ready"]]) {
    await update(page, { ...state, version: 4, queue: state.queue.map(entry => entry.id === "pending" ? { ...entry, state: lifecycle } : entry) });
    await expect(page.locator('[data-entry-id="pending"] .request-lifecycle')).toHaveText(label);
  }
  await expect(page.getByText("Requested by jrampersaud123")).toBeVisible();
});

test("guest guards all detailed lifecycle states and receives the same hero and artwork", async ({ page }) => {
  await openRoom(page, state, false);
  for (const lifecycle of ["downloading", "waiting_for_plex", "waiting_for_queue", "ready"]) {
    await update(page, { ...state, version: 4, queue: state.queue.map(entry => entry.id === "pending" ? { ...entry, state: lifecycle, error: "Waiting for Plex import" } : entry) });
    await expect(page.locator('[data-entry-id="pending"] .request-lifecycle')).toHaveText(lifecycle === "ready" ? "Ready" : "Requested");
    await expect(page.locator(".room-queue")).not.toContainText(/Downloading|Waiting for Plex|Waiting for queue/);
  }
  await expect(page.getByRole("button", { name: /Move|Remove|End Room/ })).toHaveCount(0);
  await expect(page.locator(".room-now-playing img")).toHaveAttribute("src", `${plexArt}?size=large`);
});

test("SSE changes Now Playing metadata and artwork and retains unchanged DOM and keyboard focus", async ({ page }) => {
  await openRoom(page);
  await page.getByRole("button", { name: "Remove Requested song" }).focus();
  await page.evaluate(() => {
    (window as RoomWindow).retainedRow = document.querySelector('[data-entry-id="pending"]')!;
    (window as RoomWindow).retainedHero = document.querySelector(".room-now-playing")!;
    (window as RoomWindow).retainedFocus = document.activeElement!;
  });
  await update(page, { ...state, version: 4, nowPlaying: { title: "夜を駆ける", artist: "New track artist", album: "New album", artwork: releaseArt, artworkFallback: "" } });
  await expect(page.locator(".room-now-playing")).toContainText("夜を駆ける");
  await expect(page.locator(".room-now-playing")).toContainText("New track artist");
  await expect(page.locator(".room-now-playing")).toContainText("New album");
  await expect(page.locator(".room-now-playing img")).toHaveAttribute("src", `${releaseArt}?size=large`);
  expect(await page.evaluate(() => (window as RoomWindow).retainedRow === document.querySelector('[data-entry-id="pending"]') && (window as RoomWindow).retainedHero === document.querySelector(".room-now-playing") && (window as RoomWindow).retainedFocus === document.activeElement)).toBe(true);
});

test("the same pending entry switches to cached Plex artwork after materialization and later art-only SSE", async ({ page }) => {
  await openRoom(page);
  const row = page.locator('[data-entry-id="pending"]');
  await expect(row.locator("img")).toHaveAttribute("src", `${releaseArt}?size=thumb`);
  await page.evaluate(() => { (window as RoomWindow).retainedRow = document.querySelector('[data-entry-id="pending"]')!; });
  await update(page, { ...state, version: 4, queue: state.queue.map(entry => entry.id === "pending" ? { ...entry, state: "ready" } : entry) });
  await update(page, { ...state, version: 4, queue: state.queue.map(entry => entry.id === "pending" ? { ...entry, state: "ready", artwork: plexArt, artworkFallback: releaseArt } : entry) });
  await expect(row.locator("img")).toHaveAttribute("src", `${plexArt}?size=thumb`);
  await expect(row.locator(".request-lifecycle")).toHaveText("Ready");
  expect(await page.evaluate(() => (window as RoomWindow).retainedRow === document.querySelector('[data-entry-id="pending"]'))).toBe(true);
});

test("duplicate songs remain independent queue rows through SSE reorder and removal", async ({ page }) => {
  const duplicate = { ...state.queue[1], id: "duplicate", state: "ready", artwork: plexArt };
  const snapshot = { ...state, queue: [...state.queue, duplicate] };
  await openRoom(page, snapshot);
  await expect(page.locator(".room-queue li")).toHaveCount(4);
  await expect(page.locator('[data-entry-id="pending"]')).toContainText("Downloading");
  await expect(page.locator('[data-entry-id="duplicate"]')).toContainText("Ready");
  await update(page, { ...snapshot, version: 4, queue: [snapshot.queue[0], duplicate, snapshot.queue[2]] });
  await expect(page.locator(".room-queue li")).toHaveCount(3);
  await expect(page.locator(".room-queue li").nth(1)).toHaveAttribute("data-entry-id", "duplicate");
});

for (const width of [320, 390]) {
  for (const isHost of [true, false]) {
    test(`${isHost ? "host" : "guest"} player fits ${width}px with Unicode titles and compact controls`, async ({ page }) => {
      await page.setViewportSize({ width, height: 850 });
      const long = "とても長い日本語の曲名🎵".repeat(12);
      await openRoom(page, { ...state, nowPlaying: { ...state.nowPlaying, title: long }, queue: state.queue.map(entry => ({ ...entry, title: long, artist: "長いアーティスト名".repeat(12), requester: entry.requester ? "Requester".repeat(30) : null })) }, isHost);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      const hero = await page.locator(".room-now-playing .room-artwork").boundingBox();
      expect(hero!.width).toBeCloseTo(hero!.height, 1);
      expect(hero!.width).toBeGreaterThan(240);
      const viewportFits = await page.locator(".room-queue li").evaluateAll(rows => rows.every(row => {
        const rect = row.getBoundingClientRect();
        return row.scrollWidth <= row.clientWidth && rect.left >= 0 && rect.right <= window.innerWidth;
      }));
      expect(viewportFits).toBe(true);
      await page.screenshot({ path: `test-results/rooms-player-${isHost ? "host" : "guest"}-${width}.png`, fullPage: true });
    });
  }
}

import { expect, test, type Page } from "@playwright/test";

const room = {
  code: "ABCDEFGHJK", status: "active", version: 3, joinPath: "/rooms/ABCDEFGHJK", guestCount: 1,
  nowPlaying: { title: "Current song", artist: "Artist" }, handoff: { title: "Buffer song", artist: "Artist" },
  upNext: { title: "Buffer song", artist: "Artist" },
  playbackState: "playing", queueWarning: true, syncError: null,
  queue: [{ id: "entry-one", title: "Available song", artist: "Artist", requester: "Guest #1", state: "ready" },
          { id: "entry-two", title: "Missing song", artist: "Artist", requester: "Guest #1", state: "requested" }],
};

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

async function events(page: Page, state: unknown = room) {
  await page.route("**/api/rooms/*/events", route => route.fulfill({
    contentType: "text/event-stream", body: `event: room\ndata: ${JSON.stringify(state)}\n\n`,
  }));
}
async function host(page: Page) {
  await page.request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
  await events(page);
}
async function guest(page: Page) {
  await events(page);
  await page.route("**/api/rooms/ABCDEFGHJK/join", async route => {
    expect(route.request().headers()["x-room-request"]).toBe("1");
    await route.fulfill({ json: { room, guest: { name: "Guest #1", csrfToken: "guest-csrf" } } });
  });
  await page.goto("/rooms/ABCDEFGHJK");
  await page.getByRole("button", { name: "Join Room" }).click();
  await expect(page.getByText("Joined as Guest #1")).toBeVisible();
}

test("host route shows startup instructions and actionable playback failure", async ({ page }) => {
  await host(page);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: null } }));
  await page.route("**/api/rooms", route => route.fulfill({ status: 502, json: { error: "No active Plexamp playback found. Start playing music in Plexamp, make sure there is another song in Up Next, then try again." } }));
  await page.goto("/rooms");
  await expect(page.locator("#rooms")).toHaveClass(/active/);
  await expect(page.getByText(/First start playing music in Plexamp/)).toBeVisible();
  await page.getByRole("button", { name: "Start Room", exact: true }).click();
  await expect(page.getByText(/No active Plexamp playback found/)).toBeVisible();
});

test("host shows Up Next, warns, and submits opaque reorder and removal IDs", async ({ page }) => {
  await host(page);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room } }));
  await page.route("**/api/rooms/ABCDEFGHJK/order", async route => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-ada");
    expect(route.request().postDataJSON()).toEqual({ entryIds: ["entry-two", "entry-one"], version: 3 });
    await route.fulfill({ json: { room: { ...room, version: 4, queue: [...room.queue].reverse() } } });
  });
  await page.route("**/api/rooms/ABCDEFGHJK/entries/entry-one", async route => {
    expect(route.request().method()).toBe("DELETE");
    expect(route.request().postDataJSON()).toEqual({ version: 4 });
    await route.fulfill({ json: { room: { ...room, version: 5, queue: [room.queue[1]] } } });
  });
  await page.goto("/rooms");
  await expect(page.getByText("Buffer song", { exact: true })).toBeVisible();
  await expect(page.locator(".room-warning")).toContainText("almost empty");
  await expect(page.getByLabel("Guest join URL")).toHaveValue("http://127.0.0.1:4173/rooms/ABCDEFGHJK");
  await page.getByRole("button", { name: "Move Missing song up" }).click();
  await expect(page.locator(".room-queue li").first()).toContainText("Missing song");
  await page.getByRole("button", { name: "Remove Available song" }).click();
  await expect(page.locator(".room-queue li")).toHaveCount(1);
});

test("imported queue omits unknown requesters and keeps known guest attribution", async ({ page }) => {
  await host(page);
  const imported = { ...room, handoff: {}, upNext: { title: "Existing next", artist: "Artist" }, queue: [
    { id: "plex-next", title: "Existing next", artist: "Artist", requester: null, state: "ready", locked: true },
    { id: "plex-future", title: "Existing future", artist: "Artist", state: "ready" },
    room.queue[0],
  ] };
  await events(page, imported);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: imported } }));
  await page.goto("/rooms");
  const rows = page.locator(".room-queue li");
  await expect(rows).toHaveCount(3);
  await expect(rows.first()).toContainText("Up Next · Locked");
  await expect(rows.first()).not.toContainText("Requested by");
  await expect(rows.nth(1)).not.toContainText("Requested by");
  await expect(rows.last()).toContainText("Requested by Guest #1");
  await expect(page.getByRole("button", { name: "Remove Existing next" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Remove Existing future" })).toBeEnabled();
  await expect(page.locator("#room-root")).not.toContainText(/Added by|Autoplay|Host-added|undefined|null/);
});

test("external SSE additions removals and reorders update queue and locked controls", async ({ page }) => {
  await page.addInitScript(() => {
    const Original = window.EventSource;
    window.EventSource = class extends Original {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        (window as Window & { roomSource?: EventSource }).roomSource = this;
      }
    };
  });
  await host(page);
  const initial = { ...room, handoff: {}, upNext: room.queue[0], queue: room.queue.map((entry, index) => ({ ...entry, locked: index === 0 })) };
  await events(page, initial);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: initial } }));
  await page.goto("/rooms");
  await expect(page.locator(".room-queue li")).toHaveCount(2);
  const external = { id: "external", title: "New Plex song", artist: "Artist", requester: null, state: "ready", locked: false };
  await page.evaluate(state => (window as Window & { roomSource?: EventSource }).roomSource?.dispatchEvent(
    new MessageEvent("room", { data: JSON.stringify(state) }),
  ), { ...initial, version: 4, queue: [...initial.queue, external] });
  await expect(page.locator(".room-queue li").last()).toContainText("New Plex song");
  await expect(page.locator(".room-queue li").last()).not.toContainText("Requested by");
  await page.evaluate(state => (window as Window & { roomSource?: EventSource }).roomSource?.dispatchEvent(
    new MessageEvent("room", { data: JSON.stringify(state) }),
  ), { ...initial, version: 5, upNext: external, queue: [{ ...external, locked: true }, { ...initial.queue[0], locked: false }] });
  await expect(page.locator(".room-queue li")).toHaveCount(2);
  await expect(page.locator(".room-queue li").first()).toContainText("New Plex song");
  await expect(page.locator(".room-queue li").first()).toContainText("Up Next · Locked");
  await expect(page.getByText("Missing song", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Remove New Plex song" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Remove Available song" })).toBeEnabled();
});

test("guest sees externally synchronized entries with optional requester and no controls", async ({ page }) => {
  const synchronized = { ...room, handoff: {}, upNext: { title: "Plex next", artist: "Artist" }, queue: [
    { id: "plex-next", title: "Plex next", artist: "Artist", state: "ready", locked: true },
    { id: "plex-tail", title: "Plex tail", artist: "Artist", requester: null, state: "ready" },
  ] };
  await events(page, synchronized);
  await page.route("**/api/rooms/ABCDEFGHJK/join", route => route.fulfill({ json: { room: synchronized, guest: { name: "Guest #1", csrfToken: "guest-csrf" } } }));
  await page.goto("/rooms/ABCDEFGHJK");
  await page.getByRole("button", { name: "Join Room" }).click();
  await expect(page.locator(".room-queue li")).toHaveCount(2);
  await expect(page.locator(".room-queue li").first()).toContainText("Up Next · Locked");
  await expect(page.locator(".room-queue")).not.toContainText("Requested by");
  await expect(page.getByRole("button", { name: /Remove|Move/ })).toHaveCount(0);
});

test("pending acquisition labels distinguish Plex indexing from queue placement across SSE updates", async ({ page }) => {
  await page.addInitScript(() => {
    const Original = window.EventSource;
    window.EventSource = class extends Original {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        (window as Window & { roomSource?: EventSource }).roomSource = this;
      }
    };
  });
  await host(page);
  const initial = { ...room, handoff: {}, queue: [
    { ...room.queue[0], locked: true },
    { ...room.queue[1], title: "365", state: "downloading" },
  ] };
  await events(page, initial);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: initial } }));
  await page.goto("/rooms");
  const pending = page.locator('[data-entry-id="entry-two"] .request-lifecycle');
  await expect(pending).toHaveText("Downloading");
  for (const [index, [state, label]] of [
    ["waiting_for_plex", "Waiting for Plex"],
    ["waiting_for_queue", "Ready · Waiting for queue"],
    ["ready", "Ready"],
  ].entries()) {
    await page.evaluate(snapshot => (window as Window & { roomSource?: EventSource }).roomSource?.dispatchEvent(
      new MessageEvent("room", { data: JSON.stringify(snapshot) }),
    ), { ...initial, version: 4 + index, queue: [initial.queue[0], { ...initial.queue[1], state }] });
    await expect(pending).toHaveText(label);
    await expect(page.getByRole("button", { name: "Remove Available song" })).toBeDisabled();
    await expect(page.locator(".room-panel .message.error")).toHaveCount(0);
  }
});

for (const width of [320, 390]) {
  test(`guest can join and request at ${width}px without application navigation`, async ({ page }) => {
    await page.setViewportSize({ width, height: 850 });
    await guest(page);
    await expect(page.locator("header, .tab-bar, #auth, #settings")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Remove|End Room|Move/ })).toHaveCount(0);
    await expect(page.locator(".room-queue li").last()).toContainText("Requested");
    await expect(page.locator(".room-queue")).not.toContainText("Downloading");
    await page.route("**/api/rooms/ABCDEFGHJK/search?*", route => route.fulfill({ json: {
      results: [{ id: "opaque-choice", title: "Search song", artist: "Guest artist", album: "Album", state: "not_requested" }],
    } }));
    await page.route("**/api/rooms/ABCDEFGHJK/entries", async route => {
      expect(route.request().headers()["x-room-csrf"]).toBe("guest-csrf");
      expect(route.request().postDataJSON()).toEqual({ choiceId: "opaque-choice" });
      await route.fulfill({ status: 201, json: { room: { ...room, version: 4, queue: [...room.queue, {
        id: "entry-three", title: "Search song", artist: "Guest artist", requester: "Guest #1", state: "requested",
      }] } } });
    });
    await page.getByLabel("Track or artist").fill("search song");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByRole("button", { name: "Request Search song" }).click();
    await expect(page.locator(".room-queue li").last()).toContainText("Search song");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBeTruthy();
  });
}

test("guest realtime completion and closure remove request controls", async ({ page }) => {
  await page.addInitScript(() => {
    const Original = window.EventSource;
    window.EventSource = class extends Original {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        (window as Window & { roomSource?: EventSource }).roomSource = this;
      }
    };
  });
  await guest(page);
  await page.evaluate(state => (window as Window & { roomSource?: EventSource }).roomSource?.dispatchEvent(
    new MessageEvent("room", { data: JSON.stringify(state) }),
  ), { ...room, version: 4, queue: room.queue.map(entry => ({ ...entry, state: "ready" })) });
  await expect(page.locator(".room-queue li").last()).toContainText("Ready");
  await page.evaluate(state => (window as Window & { roomSource?: EventSource }).roomSource?.dispatchEvent(
    new MessageEvent("room", { data: JSON.stringify(state) }),
  ), { ...room, version: 5, status: "closed" });
  await expect(page.getByText(/This Room has ended/)).toBeVisible();
  await expect(page.getByLabel("Track or artist")).toBeHidden();
});

test("guest join failure is clear and retryable", async ({ page }) => {
  await page.route("**/api/rooms/ABCDEFGHJK/join", route => route.fulfill({ status: 410, json: { error: "This Room has ended." } }));
  await page.goto("/rooms/ABCDEFGHJK");
  await page.getByRole("button", { name: "Join Room" }).click();
  await expect(page.getByText("This Room has ended.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Join Room" })).toBeEnabled();
});

test("an older host mutation cannot replace a later Rooms view", async ({ page }) => {
  await host(page);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room } }));
  let release: () => void = () => {};
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/rooms/ABCDEFGHJK/entries/entry-one", async route => {
    await pending;
    await route.fulfill({ json: { room: { ...room, version: 99, queue: [] } } });
  });
  await page.goto("/rooms");
  const requested = page.waitForRequest("**/api/rooms/ABCDEFGHJK/entries/entry-one");
  await page.getByRole("button", { name: "Remove Available song" }).click();
  await requested;
  await page.locator('.header-nav [data-view="library"]').click();
  await page.locator('.header-nav [data-view="rooms"]').click();
  await expect(page.locator(".room-queue li")).toHaveCount(2);
  const completed = page.waitForResponse("**/api/rooms/ABCDEFGHJK/entries/entry-one");
  release(); await completed;
  await expect(page.locator(".room-queue li")).toHaveCount(2);
});

test("host locks Up Next and can move a future request immediately after it", async ({ page }) => {
  await host(page);
  const lockedRoom = { ...room, upNext: { title: "Available song", artist: "Artist" }, handoff: {}, queue: [
    { ...room.queue[0], locked: true }, room.queue[1],
    { id: "entry-three", title: "Third song", artist: "Artist", requester: "Guest #1", state: "ready" },
  ] };
  await events(page, lockedRoom);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: lockedRoom } }));
  await page.route("**/api/rooms/ABCDEFGHJK/order", async route => {
    expect(route.request().postDataJSON()).toEqual({ entryIds: ["entry-one", "entry-three", "entry-two"], version: 3 });
    await route.fulfill({ json: { room: { ...lockedRoom, version: 4, queue: [lockedRoom.queue[0], lockedRoom.queue[2], lockedRoom.queue[1]] } } });
  });
  await page.goto("/rooms");
  await expect(page.locator(".room-queue li").first()).toContainText("Up Next · Locked");
  for (const name of ["Move Available song up", "Move Available song down", "Remove Available song", "Move Missing song up"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await page.getByRole("button", { name: "Move Third song up" }).click();
  await expect(page.locator(".room-queue li").nth(1)).toContainText("Third song");
  await expect(page.locator(".room-queue li").first()).toContainText("Available song");
  await expect(page.getByRole("button", { name: "Move Third song up" })).toBeDisabled();
});

test("playback event transfers the lock to the new Room Up Next", async ({ page }) => {
  await page.addInitScript(() => {
    const Original = window.EventSource;
    window.EventSource = class extends Original {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        (window as Window & { roomSource?: EventSource }).roomSource = this;
      }
    };
  });
  await host(page);
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room } }));
  await page.goto("/rooms");
  await expect(page.getByRole("button", { name: "Remove Available song" })).toBeEnabled();
  await page.evaluate(state => (window as Window & { roomSource?: EventSource }).roomSource?.dispatchEvent(
    new MessageEvent("room", { data: JSON.stringify(state) }),
  ), { ...room, version: 4, nowPlaying: room.upNext, handoff: {}, upNext: room.queue[0], queue: room.queue.map((entry, index) => ({ ...entry, locked: index === 0 })) });
  await expect(page.locator(".room-queue li").first()).toContainText("Up Next · Locked");
  await expect(page.getByRole("button", { name: "Remove Available song" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Move Available song down" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Remove Missing song" })).toBeEnabled();
});

test("guest sees the locked next Room request without queue controls", async ({ page }) => {
  await events(page);
  const lockedRoom = { ...room, handoff: {}, upNext: room.queue[0], queue: room.queue.map((entry, index) => ({ ...entry, locked: index === 0 })) };
  await events(page, lockedRoom);
  await page.route("**/api/rooms/ABCDEFGHJK/join", route => route.fulfill({ json: { room: lockedRoom, guest: { name: "Guest #1", csrfToken: "guest-csrf" } } }));
  await page.goto("/rooms/ABCDEFGHJK");
  await page.getByRole("button", { name: "Join Room" }).click();
  await expect(page.locator(".room-queue li").first()).toContainText("Up Next · Locked");
  await expect(page.getByRole("button", { name: /Remove|Move/ })).toHaveCount(0);
});

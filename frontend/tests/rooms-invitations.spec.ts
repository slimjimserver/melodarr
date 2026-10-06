import { expect, test, type Page } from "@playwright/test";
import jsQR from "jsqr";

const code = "A7K3", token = "s".repeat(43);
const invitePath = `/rooms/${code}?invite=${token}`;
const secureURL = `http://127.0.0.1:4173${invitePath}`;
const room = {
  code, status: "active", version: 3, joinPath: `/rooms/${code}`, guestCount: 1,
  nowPlaying: { title: "Jaded", artist: "Track artist", album: "Album" },
  upNext: {}, handoff: {}, queue: [], queueWarning: false, syncError: null, playbackState: "playing",
};
type RoomWindow = Window & { roomSource?: EventSource; copiedInvite?: string };

test.beforeEach(async ({ request, page }) => {
  await request.post("/__reset");
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget {
      constructor() { super(); (window as RoomWindow).roomSource = this as unknown as EventSource; }
      close() {}
    } as unknown as typeof EventSource;
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async (text: string) => { (window as RoomWindow).copiedInvite = text; },
    } });
  });
});
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

async function host(page: Page, error: string | null = null) {
  await page.request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
  await page.route("**/api/rooms/active", route => route.fulfill({ json: { room: { ...room, syncError: error } } }));
  await page.route(`**/api/rooms/${code}/invite`, route => route.fulfill({ json: { invitePath } }));
  await page.goto("/rooms");
  await expect(page.getByRole("region", { name: "Now Playing" })).toBeVisible();
}
async function guest(page: Page, fail = false, url = invitePath) {
  await page.route(`**/api/rooms/${code}/join`, async route => {
    expect(route.request().postDataJSON()).toEqual({ name: "", invite: token });
    expect(route.request().url()).not.toContain(token);
    await route.fulfill(fail ? { status: 403, json: { error: "Use a valid invite link from the Room host to join." } }
      : { json: { room, guest: { name: "Guest #1", csrfToken: "guest-csrf" } } });
  });
  await page.goto(url);
  await page.getByRole("button", { name: "Join Room", exact: true }).click();
}
async function decodeQR(page: Page) {
  const pixels = await page.locator(".room-invite-qr").evaluate(async node => {
    const svg = node as SVGSVGElement;
    const side = svg.viewBox.baseVal.width * 6;
    const canvas = document.createElement("canvas"); canvas.width = side; canvas.height = side;
    const context = canvas.getContext("2d")!;
    context.fillStyle = "white"; context.fillRect(0, 0, side, side);
    const image = new Image();
    image.src = `data:image/svg+xml;base64,${btoa(new XMLSerializer().serializeToString(svg))}`;
    await image.decode(); context.drawImage(image, 0, 0, side, side);
    return { side, data: Array.from(context.getImageData(0, 0, side, side).data) };
  });
  return jsQR(new Uint8ClampedArray(pixels.data), pixels.side, pixels.side)?.data;
}

test("Invite locally renders a scannable QR containing the exact secure current-origin URL", async ({ page }) => {
  await host(page);
  const requests: string[] = [];
  page.on("request", request => requests.push(request.url()));
  await page.getByRole("button", { name: "Invite", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Join Room" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(code, { exact: true })).toBeVisible();
  await expect(dialog.getByRole("img", { name: `Scan to join Room ${code}` })).toBeVisible();
  expect(await decodeQR(page)).toBe(secureURL);
  expect(requests.length).toBeGreaterThan(0);
  expect(requests.every(url => new URL(url).origin === new URL(secureURL).origin)).toBe(true);
  await expect(dialog).not.toContainText(token);
  await expect(dialog.getByLabel("Guest join URL")).toBeHidden();
  await page.screenshot({ path: "test-results/rooms-invite-desktop.png" });
});

test("Copy Invite Link copies the full secure URL and shows a subtle confirmation", async ({ page }) => {
  await host(page);
  await page.getByRole("button", { name: "Invite", exact: true }).click();
  await page.getByRole("button", { name: "Copy Invite Link", exact: true }).click();
  expect(await page.evaluate(() => (window as RoomWindow).copiedInvite)).toBe(secureURL);
  await expect(page.getByText("Invite link copied.")).toBeVisible();
  await expect(page.locator(".room-invite-status")).not.toHaveClass(/error|danger/);
});

test("clipboard failure exposes a selectable secure link without displaying a standalone token", async ({ page }) => {
  await host(page);
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error("unavailable"); }; });
  await page.getByRole("button", { name: "Invite", exact: true }).click();
  await page.getByRole("button", { name: "Copy Invite Link", exact: true }).click();
  await expect(page.getByLabel("Guest join URL")).toHaveValue(secureURL);
  await expect(page.getByLabel("Guest join URL")).toBeFocused();
  await expect(page.getByText("Select and copy the invite link.")).toBeVisible();
});

test("invitation dialog supports keyboard dismissal and returns focus to Invite", async ({ page }) => {
  await host(page);
  const invite = page.getByRole("button", { name: "Invite", exact: true });
  await invite.focus(); await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(invite).toBeFocused();
});

test("guest joins with JSON bootstrap token, scrubs the address, and shows success without danger styling", async ({ page }) => {
  await guest(page);
  expect(page.url()).toBe(`http://127.0.0.1:4173/rooms/${code}`);
  await expect(page.getByText("Joined as Guest #1")).toBeVisible();
  await expect(page.locator(".room-message")).toHaveClass(/success/);
  await expect(page.locator(".room-message")).not.toHaveClass(/error|danger/);
  expect(await page.locator(".room-message").evaluate(node => getComputedStyle(node).borderLeftWidth)).toBe("0px");
  expect((await page.locator(".room-message").boundingBox())!.height).toBeLessThan(30);
  await page.evaluate(state => (window as RoomWindow).roomSource?.dispatchEvent(new MessageEvent("room", { data: JSON.stringify(state) })), room);
  await expect(page.getByText("Room updates connected")).toHaveCount(0);
  await expect(page.locator(".room-connection")).toBeHidden();
  await expect(page.getByRole("button", { name: "Invite", exact: true })).toHaveCount(0);
});

test("actual guest authorization errors retain danger styling and retryable join", async ({ page }) => {
  await guest(page, true);
  await expect(page.locator(".room-message")).toHaveClass(/error/);
  await expect(page.locator(".room-message")).toHaveAttribute("role", "alert");
  await expect(page.getByText("Use a valid invite link from the Room host to join.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Join Room", exact: true })).toBeEnabled();
  expect(await page.locator(".room-message").evaluate(node => getComputedStyle(node).borderLeftWidth)).toBe("2px");
});

test("header contains all host actions, preserves their authorization, and removes bottom copy", async ({ page }) => {
  await host(page);
  const heading = page.locator(".room-heading");
  for (const name of ["Invite", "Retry synchronization", "End Room"]) await expect(heading.getByRole("button", { name, exact: true })).toBeVisible();
  await expect(page.locator(".room-actions, .room-note")).toHaveCount(0);
  await expect(page.getByText("Playback stays in the selected Plex player. Queue changes refresh there when playback advances.")).toHaveCount(0);
  await page.route(`**/api/rooms/${code}/sync`, async route => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-ada");
    await route.fulfill({ json: { room } });
  });
  await heading.getByRole("button", { name: "Retry synchronization" }).click();
  await page.route(`**/api/rooms/${code}/end`, async route => {
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-ada");
    await route.fulfill({ json: { room: { ...room, status: "closed", version: 4 } } });
  });
  await heading.getByRole("button", { name: "End Room" }).click();
  await expect(page.getByText("This Room has ended. Plex music playback continues with its current queue.")).toBeVisible();
});

test("sync failures retain error styling and only emphasize the secondary Retry action", async ({ page }) => {
  await host(page, "Queue unavailable.");
  await expect(page.getByText("Queue unavailable.")).toHaveClass(/error/);
  await expect(page.locator(".room-header-actions")).toHaveClass(/has-sync-error/);
  await expect(page.locator(".room-header-actions")).not.toHaveClass(/danger/);
});

test("host-only invite failures remain actionable without opening a broken QR", async ({ page }) => {
  await host(page);
  await page.route(`**/api/rooms/${code}/invite`, route => route.fulfill({ status: 403, json: { error: "Only this Room's host can do that." } }));
  await page.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(page.locator(".room-message")).toHaveClass(/error/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

for (const width of [320, 390]) {
  test(`header actions and centered scannable invitation fit ${width}px mobile screens`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await host(page);
    const controls = page.locator(".room-heading button");
    for (let index = 0; index < await controls.count(); index++) {
      const box = (await controls.nth(index).boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
      await expect(controls.nth(index)).toBeEnabled();
    }
    await page.getByRole("button", { name: "Invite", exact: true }).click();
    const dialog = (await page.getByRole("dialog").boundingBox())!;
    expect(dialog.x).toBeGreaterThanOrEqual(0); expect(dialog.x + dialog.width).toBeLessThanOrEqual(width);
    expect(dialog.x + dialog.width / 2).toBeCloseTo(width / 2, 0);
    const qr = (await page.locator(".room-invite-qr").boundingBox())!;
    expect(qr.width).toBeGreaterThanOrEqual(200); expect(qr.width).toBeLessThanOrEqual(260);
    expect(await decodeQR(page)).toBe(secureURL);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: `test-results/rooms-invite-${width}.png` });
  });
}

import { expect, test, type Page } from "@playwright/test";

const automation = {
  id: "automation:1", source: "automation", kind: "release-group", mbid: "automation-album",
  name: "Popstar", artist_name: "Tinashe", release_type: "Album", release_date: "2026-08-21",
  created_at: 1790949093, availableInPlex: false, requestStatus: "downloading", downloadStatus: { progress: 57 },
  requester: { id: null, username: "Automation API", userType: "automation" },
};
const local = {
  id: 1, source: "user", kind: "artist", mbid: "local-artist", name: "Local Artist",
  created_at: 1790949000, availableInPlex: false,
  requester: { id: 2, username: "Local Listener", localUsername: "local-listener", userType: "local", role: "user" },
};
const plex = {
  id: 2, source: "user", kind: "release-group", mbid: "plex-album", name: "Plex Album",
  created_at: 1790948000, availableInPlex: true, requestStatus: "available", plexUrl: "https://app.plex.tv/album",
  requester: { id: 3, username: "Plex Listener", localUsername: "generated-plex-name", userType: "plex", role: "user", plexUsername: "Plex Listener" },
};

test.beforeEach(async ({ request }) => { await request.post("/__reset"); });

async function adminSession(page: Page) {
  await page.route("**/api/auth/me", route => route.fulfill({ json: { id: 1, username: "ada", role: "admin" } }));
}

for (const width of [1440, 390]) {
  test(`automation requester renders and filters without a user link at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await adminSession(page);
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/admin/requests?*", route => route.fulfill({ json: {
      requests: [automation, local, plex], pagination: { page: 1, pageSize: 100, total: 3, totalPages: 1 },
    } }));
    await page.goto("/settings/requests");

    const rows = page.locator(".admin-request-item");
    await expect(rows).toHaveCount(3);
    const machine = rows.filter({ has: page.getByRole("link", { name: "Popstar", exact: true }) });
    await expect(machine.locator(".admin-request-requester strong")).toHaveText("Automation API");
    await expect(machine.locator(".admin-request-requester a")).toHaveCount(0);
    await expect(machine.locator(".admin-request-requester small")).toContainText("Automation");
    await expect(machine.locator(".user-avatar")).toHaveAttribute("aria-hidden", "true");
    await expect(machine.locator(".request-lifecycle")).toHaveText("Downloading 57%");
    await expect(machine.locator(".history-title")).toHaveAttribute("href", "/albums/automation-album");
    await expect(page.locator("#admin-requests-total")).toHaveText("3");
    await expect(page.locator("#admin-requests-artists")).toHaveText("1");
    await expect(page.locator("#admin-requests-releases")).toHaveText("2");
    await expect(rows.nth(1).locator(".admin-request-requester a")).toHaveAttribute("href", "/local-listener");
    await expect(rows.nth(2).locator(".admin-request-requester a")).toHaveAttribute("href", "/generated-plex-name");
    await expect(rows.nth(2).locator(".admin-request-plex")).toHaveAttribute("href", "https://app.plex.tv/album");

    await page.locator("#admin-requests-search").fill("Automation API");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Popstar");
    await page.locator("#admin-requests-type").selectOption("artist");
    await expect(rows).toHaveCount(0);
    await expect(page.locator(".admin-request-empty")).toHaveText("No requests match the current filters.");
    await page.locator("#admin-requests-search").fill("");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Local Artist");
    await page.locator("#admin-requests-type").selectOption("all");
    await expect(rows).toHaveCount(3);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("admin-automation.png") });
  });
}

test("combined totals and pagination include automation on later pages", async ({ page }) => {
  await adminSession(page);
  const reads: number[] = [];
  const first = Array.from({ length: 100 }, (_, i) => ({ ...local, id: i + 1, mbid: `user-${i}`, name: `User Artist ${i}` }));
  await page.route("**/api/admin/requests?*", route => {
    const pageNumber = Number(new URL(route.request().url()).searchParams.get("page"));
    reads.push(pageNumber);
    return route.fulfill({ json: {
      requests: pageNumber === 1 ? first : [automation],
      pagination: { page: pageNumber, pageSize: 100, total: 101, totalPages: 2 },
    } });
  });
  await page.goto("/settings/requests");
  await expect(page.locator(".admin-request-item")).toHaveCount(100);
  await expect(page.locator("#admin-requests-total")).toHaveText("101");
  await page.locator("#admin-requests-next").click();
  await expect(page.locator(".admin-request-item")).toHaveCount(1);
  await expect(page.locator(".admin-request-requester strong")).toHaveText("Automation API");
  await expect(page.locator("#admin-requests-message")).toHaveText("Showing 101–101 of 101 requests.");
  await expect(page.locator("#admin-requests-page")).toHaveText("Page 2 of 2");
  await expect(page.locator("#admin-requests-next")).toBeDisabled();
  await page.locator("#admin-requests-previous").click();
  await expect(page.locator(".admin-request-item")).toHaveCount(100);
  expect(reads).toEqual([1, 2, 1]);
});

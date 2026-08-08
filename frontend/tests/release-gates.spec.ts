import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("/__reset");
});

async function signIn(page: import("@playwright/test").Page, username: string) {
  const form = page.locator("#login-form");
  await form.getByLabel("Username").fill(username);
  await form.getByLabel("Password").fill("fixture-password");
  await form.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("body")).toHaveClass(/authenticated/);
}

async function openProposalReview(page: import("@playwright/test").Page) {
  const summary = page.locator("#detail-results .anime-mapping-proposals summary");
  await expect(summary).toBeVisible();
  await summary.click();
}

test("account switching refetches anime detail without leaking prior proposals", async ({ page }) => {
  await page.goto("/anime/switching-anime");
  await signIn(page, "ada");
  await expect(page.getByText("Switching Anime")).toBeVisible();
  await page.getByRole("button", { name: "Manage mappings" }).click();
  await openProposalReview(page);
  await expect(page.getByText("Proposal for ada")).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await signIn(page, "bea");
  await page.evaluate(() => {
    window.history.pushState({}, "", "/anime/switching-anime");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await page.getByRole("button", { name: "Manage mappings" }).click();
  await openProposalReview(page);
  await expect(page.getByText("Proposal for bea")).toBeVisible();
  await expect(page.getByText("Proposal for ada")).toHaveCount(0);
});

test("a delayed detail mutation cannot overwrite the next authenticated session", async ({ page }) => {
  await page.goto("/anime/switching-anime");
  await signIn(page, "ada");
  await page.getByRole("button", { name: "Manage mappings" }).click();
  await openProposalReview(page);
  const approval = page.waitForRequest((request) => request.url().endsWith("/mapping-proposals/1/approve"));
  await page.getByRole("button", { name: "Approve" }).click();
  await approval;
  await expect.poll(async () => (
    (await page.request.get("/__fixture-state")).json()
  )).toMatchObject({ delayedProposalPending: true });

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.locator("#login-form")).toBeVisible();
  await signIn(page, "bea");
  await page.evaluate(() => {
    window.history.pushState({}, "", "/anime/switching-anime");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await page.getByRole("button", { name: "Manage mappings" }).click();
  await openProposalReview(page);
  await expect(page.getByText("Proposal for bea")).toBeVisible();

  await expect.poll(async () => (
    (await page.request.get("/__fixture-state")).json()
  )).toMatchObject({ delayedProposalPending: false, delayedProposalAborted: true });
  const release = await page.request.post("/__release-delayed-proposal");
  await expect(release).toBeOK();
  await expect(release.json()).resolves.toEqual({ released: false, aborted: true });
  await expect(page.locator("#status")).toContainText("Signed in as bea");
  await expect(page.getByText("Proposal for bea")).toBeVisible();
  await expect(page.getByText("Proposal for ada")).toHaveCount(0);
  await expect(page.getByText("Stale approval from ada")).toHaveCount(0);
});

test("a delayed detail mutation cannot affect a later view in the same session", async ({ page }) => {
  await page.goto("/anime/switching-anime");
  await signIn(page, "ada");
  await page.getByRole("button", { name: "Manage mappings" }).click();
  await openProposalReview(page);
  const approval = page.waitForRequest((request) => request.url().endsWith("/mapping-proposals/1/approve"));
  await page.getByRole("button", { name: "Approve" }).click();
  await approval;
  await expect.poll(async () => (
    (await page.request.get("/__fixture-state")).json()
  )).toMatchObject({ delayedProposalPending: true });

  await page.locator("header [data-view='discover']").click();
  await expect(page.locator("#discover")).toHaveClass(/active/);
  const release = await page.request.post("/__release-delayed-proposal");
  await expect(release).toBeOK();
  await expect(release.json()).resolves.toEqual({ released: true, aborted: false });
  await expect(page.locator("#discover")).toHaveClass(/active/);
  await expect(page.getByText("Stale approval from ada")).toHaveCount(0);
});

test("an admin alias collision keeps the canonical target separate from the signed-in account", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "ada");
  await page.evaluate(() => {
    window.history.pushState({}, "", "/target-user/settings/general");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.locator("#account-content h2")).toHaveText("General");
  await expect(page.locator("[data-account-self-only]")).toBeHidden();
  const username = page.locator("#account-content input[name='username']");
  await expect(username).toHaveValue("target-user");
  await username.fill("target-user-renamed");
  await page.getByRole("button", { name: "Save general settings" }).click();
  await expect(page.locator("#account-content .form-message")).toContainText("Target account saved");
  await expect(page.getByLabel("Open settings for ada")).toBeVisible();
  await expect(page.locator("#status")).toContainText("Signed in as ada");
});

test("linked-account partial success refreshes the authoritative account state", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "ada");
  await page.getByLabel("Open settings for ada").click();
  await page.getByRole("link", { name: "Linked accounts" }).click();
  await expect(page.locator("#account-content h2")).toHaveText("Linked accounts");
  await page.getByPlaceholder("your-listenbrainz-name").fill("stale-listen");
  await page.getByPlaceholder("your-lastfm-name").fill("stale-last");
  const updates = Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/account/settings?") && response.request().method() === "POST"),
    page.waitForResponse((response) => response.url().includes("/api/account/lastfm?") && response.request().method() === "POST"),
    page.waitForResponse((response) => response.url().includes("/api/account/settings?") && response.request().method() === "GET"),
  ]);
  await page.getByRole("button", { name: "Save linked accounts" }).click();
  await updates;
  const message = page.locator("#account-content #account-plex-message");
  await expect(message).toContainText("ListenBrainz: ListenBrainz saved");
  await expect(message).toContainText("Last.fm: Last.fm was unavailable");
  await expect(page.getByPlaceholder("your-listenbrainz-name")).toHaveValue("authoritative-listen");
  await expect(page.getByPlaceholder("your-lastfm-name")).toHaveValue("authoritative-last");
});

test("SPA navigation moves focus to the main landmark", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "ada");
  await page.getByRole("button", { name: "Your library" }).click();
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(page.locator("#main-content")).toHaveCSS("outline-style", "none");
});

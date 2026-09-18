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
  await page.getByRole("link", { name: "Your library" }).click();
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(page.locator("#main-content")).toHaveCSS("outline-style", "none");
});

test("detail controls expose native new-tab links without losing SPA navigation", async ({ page, context }) => {
  await page.goto("/artists/fixture-artist");
  await signIn(page, "ada");
  await expect(page.locator("#detail-title")).toHaveText("Fixture Artist");

  const albumLink = page.getByRole("link", { name: "Open details for Fixture Album" });
  await expect(albumLink).toHaveAttribute("href", "/albums/fixture-album");
  await expect(albumLink).toHaveJSProperty("tagName", "A");
  const currentUrl = page.url();

  const newPagePromise = context.waitForEvent("page");
  await albumLink.click({ button: "middle" });
  const newPage = await newPagePromise;
  await newPage.waitForURL("**/albums/fixture-album");
  expect(new URL(newPage.url()).pathname).toBe("/albums/fixture-album");
  expect(page.url()).toBe(currentUrl);
  await newPage.close();

  const modifierClickAllowed = await albumLink.evaluate((element) => (
    element.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      button: 0,
      cancelable: true,
      ctrlKey: true,
    }))
  ));
  expect(modifierClickAllowed).toBe(true);

  await albumLink.click();
  await expect(page).toHaveURL(/\/albums\/fixture-album$/);
});

test("release-group editions remain clickable and open their tracklists", async ({ page }) => {
  await page.goto("/albums/fixture-album");
  await signIn(page, "ada");
  await expect(page.locator("#detail-title")).toHaveText("Fixture Album");

  const releaseLink = page.getByRole("link", { name: "Open details for Fixture Album" });
  await expect(releaseLink).toHaveAttribute("href", "/releases/fixture-release");
  await releaseLink.click();

  await expect(page).toHaveURL(/\/releases\/fixture-release$/);
  await expect(page.locator("#detail-results")).toContainText("Fixture Track");
});

test("library page describes artist holdings only", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "ada");
  await page.getByRole("link", { name: "Your library" }).click();

  const summary = page.locator("#library-copy");
  await expect(summary).toHaveText("0 artists available in your Plex music libraries.");
  await expect(summary).not.toContainText("releases");
});

test("artist track search shows every cached release group containing the track", async ({ page }) => {
  await page.route("**/api/music/artist/fixture-artist/tracks?**", async (route) => {
    const query = new URL(route.request().url()).searchParams.get("q");
    expect(query).toBe("Fixture Track");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        candidateCount: 2,
        results: [
          {
            id: "fixture-album",
            title: "Fixture Album",
            type: "Album",
            date: "2026-01-01",
            secondaryTypes: [],
            matchedTracks: ["fixture track"],
          },
          {
            id: "fixture-single",
            title: "Fixture Single",
            type: "Single",
            date: "2025-06-01",
            secondaryTypes: [],
            matchedTracks: ["fixture track"],
          },
        ],
      }),
    });
  });
  await page.goto("/artists/fixture-artist");
  await signIn(page, "ada");

  const search = page.getByLabel("Search releases or tracks");
  await expect(page.locator('[data-release-group-id="fixture-single"]')).toHaveCount(0);
  await search.fill("Fixture Track");

  await expect(page.locator('[data-release-group-id="fixture-album"]')).toBeVisible();
  await expect(page.locator('[data-release-group-id="fixture-single"]')).toBeVisible();
  await expect(page.getByText("2 release groups contain matching tracks.")).toBeVisible();

  await search.clear();
  await expect(page.locator('[data-release-group-id="fixture-single"]')).toHaveCount(0);
  await expect(page.locator('[data-release-group-id="fixture-album"]')).toBeVisible();
});

test("mobile tab bar keeps a compact 48px target above its safe-area padding", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await signIn(page, "ada");
  await page.evaluate(() => document.documentElement.style.setProperty("--safe-bottom", "24px"));

  const metrics = await page.locator(".tab-bar").evaluate((tabBar) => {
    const bounds = tabBar.getBoundingClientRect();
    const styles = getComputedStyle(tabBar);
    const main = document.querySelector("main");
    const toasts = document.querySelector("#toasts");
    return {
      bottom: bounds.bottom,
      height: bounds.height,
      mainPaddingBottom: main ? getComputedStyle(main).paddingBottom : "",
      paddingBottom: styles.paddingBottom,
      toastBottom: toasts ? getComputedStyle(toasts).bottom : "",
    };
  });

  expect(metrics).toEqual({
    bottom: 844,
    height: 72,
    mainPaddingBottom: "108px",
    paddingBottom: "24px",
    toastBottom: "90px",
  });
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`similar artists replace the discography and flow vertically on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/artists/fixture-artist");
    await signIn(page, "ada");
    await expect(page.locator("#detail-title")).toHaveText("Fixture Artist");

    const search = page.getByLabel("Search releases");
    const similarView = page.locator("#similar-artists-view");
    await expect(search).toBeVisible();
    await expect(similarView).toBeHidden();

    if (viewport.name === "mobile") {
      const edges = await page.evaluate(() => {
        const sidebar = document.querySelector(".discography-sidebar");
        const releaseTypes = document.querySelector(".discography-nav");
        const releaseSearch = document.querySelector("#discography-search");
        return {
          sidebarLeft: sidebar?.getBoundingClientRect().left ?? 0,
          releaseTypesLeft: releaseTypes?.getBoundingClientRect().left ?? 0,
          releaseSearchLeft: releaseSearch?.getBoundingClientRect().left ?? 0,
        };
      });
      expect(edges.releaseTypesLeft).toBeCloseTo(edges.releaseSearchLeft, 0);
      expect(edges.releaseTypesLeft - edges.sidebarLeft).toBeGreaterThanOrEqual(7);
    }

    const firstPage = page.waitForRequest((request) => (
      request.url().includes("/api/music/artist/fixture-artist/similar?offset=0&limit=12")
    ));
    await page.getByRole("button", { name: "Similar artists", exact: true }).click();
    await firstPage;
    await expect(search).toBeHidden();
    await expect(similarView).toBeVisible();
    await expect(similarView.locator(".recommendation-card")).toHaveCount(12);
    const list = similarView.getByLabel("Similar artists", { exact: true });
    expect(await list.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    const initialTops = await list.locator(".recommendation-card").evaluateAll((cards) => (
      cards.map((card) => card.getBoundingClientRect().top)
    ));
    expect(initialTops.every((top, index) => (
      index === 0 || top > initialTops[index - 1]
    ))).toBe(true);

    const secondPage = page.waitForRequest((request) => (
      request.url().includes("/api/music/artist/fixture-artist/similar?offset=12&limit=12")
    ));
    await page.getByRole("button", { name: "Show more similar artists" }).click();
    await secondPage;
    await expect(similarView.locator(".recommendation-card")).toHaveCount(18);
    await expect(page.getByRole("link", { name: "Open details for Similar Artist 13" })).toBeFocused();
    const expandedTops = await list.locator(".recommendation-card").evaluateAll((cards) => (
      cards.map((card) => card.getBoundingClientRect().top)
    ));
    expect(expandedTops.every((top, index) => (
      index === 0 || top > expandedTops[index - 1]
    ))).toBe(true);
    expect(await list.evaluate((element) => element.scrollLeft)).toBe(0);

    await page.getByRole("link", { name: "Albums", exact: true }).click();
    await expect(search).toBeVisible();
    await expect(similarView).toBeHidden();
  });
}

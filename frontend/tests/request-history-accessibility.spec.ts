import { expect as baseExpect, test, type Page, type Route } from "@playwright/test";

const expect = baseExpect.configure({ timeout: 10_000 });

const artist = { id: 1, kind: "artist", mbid: "local-artist", name: "Local Artist", created_at: 1, use_for_recommendations: true };
const available = { id: 2, kind: "release-group", mbid: "available-release", name: "Available release", artist_name: "Local Artist",
  anime_name: "Local Anime", anime_slug: "local-anime", theme_label: "Opening 1", theme_id: 3,
  created_at: 3, use_for_recommendations: true, requestStatus: "available", availableInPlex: true, plexUrl: "https://app.plex.tv/album" };
const requested = { ...available, id: 3, mbid: "requested-release", name: "Requested release", created_at: 2,
  requestStatus: "requested", availableInPlex: false };

test.beforeEach(async ({ request }) => {
  await request.post("/__reset");
  await request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
});
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

async function history(page: Page, theme = "midnight", initial = [artist, available, requested]) {
  const items = initial.map(item => ({ ...item }));
  await page.addInitScript(theme => localStorage.setItem("melodarr-theme", theme), theme);
  await page.route("**/api/discover", route => route.fulfill({ json: { sections: [] } }));
  const state = { hold: false, reads: [] as Route[] };
  const respond = async (route: Route) => {
    const params = new URL(route.request().url()).searchParams;
    const response = await page.request.post("/__request-history", { data: {
      items, username: params.get("username"), query: params.get("q") || "", status: params.get("status") || "all", page: params.get("page") || "1",
    } });
    const result = await response.json();
    if (!route.request().failure()) await route.fulfill({ status: result.status, json: result.body });
  };
  await page.route("**/api/account/profile?*", route => {
    if (state.hold) state.reads.push(route);
    else return respond(route);
  });
  await page.goto("/ada/requests");
  await expect(page.locator(".request-history-section")).toHaveCount(2);
  return { state, respond, items };
}

const search = (page: Page) => page.getByRole("searchbox", { name: "Search request history" });
const status = (page: Page) => page.getByRole("combobox", { name: "Request status" });
const card = (page: Page) => page.locator(".history-item").filter({ has: page.getByRole("link", { name: "Available release", exact: true }) });
const checkbox = (page: Page) => card(page).getByLabel("Use for recommendations", { exact: true });

async function queueSearch(page: Page, state: { hold: boolean; reads: Route[] }, query = "Local") {
  const count = state.reads.length;
  state.hold = true;
  await search(page).fill(query);
  await search(page).press("Enter");
  await expect.poll(() => state.reads.length).toBe(count + 1);
  return state.reads.at(-1)!;
}

async function contrast(page: Page, selector: string, placeholder = false) {
  return page.locator(selector).first().evaluate((element, placeholder) => {
    const parse = (color: string) => {
      if (color.startsWith("#")) return color.slice(1).match(/../g)!.map(value => parseInt(value, 16));
      return color.match(/[\d.]+/g)!.map(Number);
    };
    const blend = (foreground: number[], background: number[]) => foreground.slice(0, 3)
      .map((value, index) => value * (foreground[3] ?? 1) + background[index] * (1 - (foreground[3] ?? 1)));
    const luminance = (color: number[]) => color.slice(0, 3).map(value => {
      const normalized = value / 255;
      return normalized <= .04045 ? normalized / 12.92 : ((normalized + .055) / 1.055) ** 2.4;
    }).reduce((total, value, index) => total + value * [.2126, .7152, .0722][index], 0);
    // Composite the actual solid surfaces over the theme's body base. The
    // opaque focus color also has ample clearance over the subtle body gradients.
    let background = parse(getComputedStyle(document.documentElement).getPropertyValue("--background-base").trim());
    const ancestors: Element[] = [];
    for (let node: Element | null = placeholder ? element : element.parentElement; node && node !== document.body; node = node.parentElement) ancestors.push(node);
    for (const ancestor of ancestors.reverse()) background = blend(parse(getComputedStyle(ancestor).backgroundColor), background);
    const style = getComputedStyle(element, placeholder ? "::placeholder" : undefined);
    const foreground = blend(parse(placeholder ? style.color : style.outlineColor), background);
    const [low, high] = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
    return { ratio: (high + .05) / (low + .05), width: style.outlineWidth, style: style.outlineStyle };
  }, placeholder);
}

for (const theme of ["warm", "midnight"]) {
  for (const width of [280, 320, 390]) {
    for (const safeBottom of [0, 34]) {
      test(`keyboard card focus clears navigation in ${theme} at ${width}px with ${safeBottom}px safe area`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await history(page, theme, Array.from({ length: 100 }, (_, i) => ({ ...available, id: 10 + i, mbid: `release-${i}`, name: `Local release ${i}`, created_at: 10 + i })));
        await page.evaluate(value => document.documentElement.style.setProperty("--safe-bottom", `${value}px`), safeBottom);
        await page.locator("#main-content").focus();
        let links = 0; let checkboxes = 0;
        for (let index = 0; index < 100; index++) {
          await page.keyboard.press("Tab");
          const bounds = await page.evaluate(() => {
            const focused = document.activeElement as HTMLElement;
            if (!focused.closest("#account")) return null;
            const rect = focused.getBoundingClientRect();
            return { top: rect.top, bottom: rect.bottom, tag: focused.tagName,
              headerBottom: document.querySelector("header")!.getBoundingClientRect().bottom,
              navTop: document.querySelector(".tab-bar")!.getBoundingClientRect().top };
          });
          if (!bounds) continue;
          expect(bounds.bottom).toBeGreaterThan(bounds.headerBottom);
          expect(bounds.top).toBeLessThan(bounds.navTop);
          if (bounds.tag === "A") links++;
          if (bounds.tag === "INPUT") checkboxes++;
        }
        expect(links).toBeGreaterThan(20);
        expect(checkboxes).toBeGreaterThan(10);
        for (let index = 0; index < 30; index++) {
          await page.keyboard.press("Shift+Tab");
          const clear = await page.evaluate(() => {
            const rect = document.activeElement!.getBoundingClientRect();
            return rect.bottom > document.querySelector("header")!.getBoundingClientRect().bottom
              && rect.top < document.querySelector(".tab-bar")!.getBoundingClientRect().top;
          });
          expect(clear).toBe(true);
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      });
    }
  }

  for (const width of [280, 320, 390, 768, 1440]) {
    test(`focus and search-placeholder contrast in ${theme} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await history(page, theme);
      await page.keyboard.press("Tab");
      for (const selector of [".request-history-section:last-of-type summary", '[data-account-route="requests"]',
        `${width > 700 ? ".header-nav" : ".tab-bar"} [data-primary-account="requests"]`,
        '.request-history-search input', '.request-history-search select', '.history-anime-context', '.request-history-section:last-of-type .history-title']) {
        await page.locator(selector).first().focus();
        await expect(page.locator(selector).first()).toBeFocused();
        const result = await contrast(page, selector);
        expect(result.style).toBe("solid");
        expect(result.width).toBe("3px");
        expect(result.ratio).toBeGreaterThanOrEqual(3);
      }
      expect((await contrast(page, '.request-history-search input', true)).ratio).toBeGreaterThanOrEqual(4.5);
    });
  }

  test(`retained card controls and summaries keep focus in ${theme}`, async ({ page }) => {
    const { state, respond } = await history(page, theme);
    for (const control of [card(page).getByRole("link", { name: "Available release", exact: true }),
      card(page).getByRole("link", { name: "Local Anime · Opening 1" }), checkbox(page),
      page.locator(".request-history-section").last().locator("summary")]) {
      const pending = await queueSearch(page, state);
      await control.focus();
      await respond(pending);
      await expect(page.locator(".request-history-results")).not.toHaveAttribute("aria-busy");
      await expect(control).toBeFocused();
    }
    const pending = await queueSearch(page, state, "");
    const artistLink = page.getByRole("link", { name: "Local Artist", exact: true });
    await artistLink.focus();
    await respond(pending);
    await expect(page.locator(".request-history-results")).not.toHaveAttribute("aria-busy");
    await expect(artistLink).toBeFocused();
    await expect(page.locator(".request-history-section").first()).toHaveAttribute("open");
  });

  for (const trigger of ["search", "status", "error"]) {
    test(`removed focused card returns to ${trigger === "status" ? "status" : "search"} in ${theme} after ${trigger}`, async ({ page }) => {
      const { state, respond } = await history(page, theme);
      state.hold = true;
      if (trigger === "status") await status(page).selectOption("requested");
      else await queueSearch(page, state, "not found");
      await expect.poll(() => state.reads.length).toBe(1);
      await card(page).getByRole("link", { name: "Available release", exact: true }).focus();
      if (trigger === "error") await state.reads[0].fulfill({ status: 500, json: { error: "Private error" } });
      else await respond(state.reads[0]);
      await expect(trigger === "status" ? status(page) : search(page)).toBeFocused();
      await expect(card(page)).toHaveCount(0);
      if (trigger === "error") await expect(page.locator(".request-search-status")).toHaveText("Requests could not be loaded. Please try again.");
    });
  }

  test(`delayed and stale responses do not steal newer focus in ${theme}`, async ({ page }) => {
    const { state, respond } = await history(page, theme);
    const first = await queueSearch(page, state);
    await checkbox(page).focus();
    await status(page).focus();
    await respond(first);
    await expect(status(page)).toBeFocused();
    const older = await queueSearch(page, state, "Local");
    await checkbox(page).focus();
    const newer = await queueSearch(page, state, "Requested release");
    await respond(newer);
    await expect(page.locator(".request-search-status")).toHaveText("1 matching requests.");
    await status(page).focus();
    await respond(older);
    await expect(status(page)).toBeFocused();
    await expect(card(page)).toHaveCount(0);
  });

  for (const succeeds of [true, false]) {
    test(`keyboard recommendation ${succeeds ? "success" : "rollback"} retains focus through a pending rerender in ${theme}`, async ({ page }) => {
      const { state, respond, items } = await history(page, theme);
      let mutation: Route | undefined;
      let posts = 0;
      await page.route("**/api/discover/request-influence", route => {
        posts++; mutation = route;
        expect(route.request().headers()["x-csrf-token"]).toBe("csrf-ada");
        expect(route.request().postDataJSON()).toEqual({ requestId: 2, useForRecommendations: false });
      });
      const toggle = checkbox(page);
      await toggle.focus();
      await page.keyboard.press("Space");
      await expect(toggle).toHaveAttribute("aria-disabled", "true");
      await expect(toggle).toBeFocused();
      await page.keyboard.press("Space");
      await expect(toggle).not.toBeChecked();
      expect(posts).toBe(1);
      const pending = await queueSearch(page, state);
      await toggle.focus();
      await respond(pending);
      await expect(page.locator(".request-history-results")).not.toHaveAttribute("aria-busy");
      await expect(toggle).toBeFocused();
      await expect(toggle).toHaveAttribute("aria-disabled", "true");
      if (succeeds) items.find(item => item.id === 2)!.use_for_recommendations = false;
      await mutation!.fulfill({ status: succeeds ? 200 : 500, json: succeeds ? { ok: true } : { error: "Private error" } });
      await expect(toggle).toBeEnabled();
      await expect(toggle).toBeChecked({ checked: !succeeds });
      await expect(toggle).toBeFocused();
      expect(posts).toBe(1);
    });

    test(`recommendation ${succeeds ? "success" : "failure"} leaves intentional Tab focus untouched in ${theme}`, async ({ page }) => {
      const { state, respond } = await history(page, theme);
      let mutation: Route | undefined;
      await page.route("**/api/discover/request-influence", route => { mutation = route; });
      await checkbox(page).focus();
      await page.keyboard.press("Space");
      await expect(checkbox(page)).toHaveAttribute("aria-disabled", "true");
      await page.keyboard.press("Tab");
      const key = await page.evaluate(() => (document.activeElement as HTMLElement).dataset.requestFocusKey);
      expect(key).toBeTruthy();
      await mutation!.fulfill({ status: succeeds ? 200 : 500, json: succeeds ? { ok: true } : { error: "Private error" } });
      await expect(checkbox(page)).toBeEnabled();
      expect(await page.evaluate(() => (document.activeElement as HTMLElement).dataset.requestFocusKey)).toBe(key);
      const pending = await queueSearch(page, state);
      await status(page).focus();
      await respond(pending);
      await expect(status(page)).toBeFocused();
    });
  }

  test(`pending mutation rerenders leave external focus untouched in ${theme}`, async ({ page }) => {
    const { state, respond } = await history(page, theme);
    let mutation: Route | undefined;
    await page.route("**/api/discover/request-influence", route => { mutation = route; });
    await checkbox(page).focus();
    await page.keyboard.press("Space");
    await expect(checkbox(page)).toHaveAttribute("aria-disabled", "true");
    const pending = await queueSearch(page, state);
    await status(page).focus();
    await respond(pending);
    await expect(page.locator(".request-history-results")).not.toHaveAttribute("aria-busy");
    await expect(status(page)).toBeFocused();
    await mutation!.fulfill({ json: { ok: true } });
    await expect(checkbox(page)).toBeEnabled();
    await expect(status(page)).toBeFocused();
  });

  test(`Back to top is hidden from Tab when inactive and keyboard-operable with reduced motion in ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 900 });
    await history(page, theme);
    const top = page.getByRole("button", { name: "Back to top", includeHidden: true });
    await expect(top).toBeHidden();
    await page.locator(".tab-bar a").last().focus();
    await page.keyboard.press("Tab");
    await expect(top).not.toBeFocused();
    // The existing shared handler is initialized by the lazy discovery bundle.
    const discovery = page.waitForResponse(response => new URL(response.url()).pathname === "/api/discover");
    await page.locator(".tab-bar").getByRole("link", { name: "Discover", exact: true }).click();
    await discovery;
    await page.locator(".tab-bar").getByRole("link", { name: "Requests", exact: true }).click();
    await expect(page.locator(".request-history-section")).toHaveCount(2);
    await page.locator(".request-history-section").first().locator("summary").click();
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await expect(top).toBeVisible();
    await page.locator(".tab-bar a").last().focus();
    await page.keyboard.press("Tab");
    await expect(top).toBeFocused();
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.keyboard.press("Enter");
    expect(await page.evaluate(() => window.scrollY)).toBe(0);
    await expect(top).toBeHidden();
  });
}

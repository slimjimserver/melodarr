import { expect, test, type Page } from "@playwright/test";

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "ignoreErrors" }); });

test.beforeEach(async ({ request, page }) => {
  await request.post("/__reset");
  await request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
  await page.route("**/api/discover", route => route.fulfill({ json: { sections: [] } }));
  await page.route("**/api/account/profile?*", async route => {
    const params = new URL(route.request().url()).searchParams;
    const response = await page.request.post("/__request-history", { data: {
      items: Array.from({ length: 201 }, (_, i) => ({ id: i + 1, kind: "artist", mbid: `nav-${i}`, name: "Reo Another", created_at: i + 1, use_for_recommendations: true, availableInPlex: i % 2 === 0 })),
      username: params.get("username"), query: params.get("q") || "", status: params.get("status") || "all", page: params.get("page") || "1",
    } });
    const result = await response.json();
    if (!route.request().failure()) await route.fulfill({ status: result.status, json: result.body });
  });
});

async function expectRequestsActive(page: Page) {
  for (const bar of [".header-nav", ".tab-bar"]) {
    await expect(page.locator(`${bar} .nav-link.active`)).toHaveCount(1);
    await expect(page.locator(`${bar} [data-primary-account="requests"]`)).toHaveAttribute("aria-current", "page");
  }
}

for (const theme of ["midnight", "warm"]) {
  for (const width of [1440, 900, 768, 701, 390, 320]) {
    test(`primary Requests navigation works in ${theme} at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(theme => localStorage.setItem("melodarr-theme", theme), theme);
      await page.goto("/library");
      await expect(page.locator("#library")).toBeVisible();
      const desktop = page.locator(".header-nav");
      const mobile = page.locator(".tab-bar");
      await expect(desktop.locator("a")).toHaveText(["Discover", "Your library", "Requests", "Rooms", "Settings"]);
      await expect(mobile.locator("a")).toHaveText(["Discover", "Library", "Requests", "Rooms", "Settings"]);
      await expect(desktop.locator('[data-primary-account="requests"]')).toHaveAttribute("href", "/ada/requests");
      const musicIcon = mobile.locator('[data-primary-account="requests"] .tab-icon');
      await expect(musicIcon).toHaveAttribute("aria-hidden", "true");
      await expect(musicIcon.locator("svg")).toHaveCount(1);
      await expect(musicIcon.locator("ellipse")).toHaveCount(2);
      await expect(musicIcon.locator("path")).toHaveAttribute("d", "M9 18V5l11-2v13M9 9l11-2");

      const nav = width > 700 ? desktop : mobile;
      const link = nav.getByRole("link", { name: "Requests", exact: true });
      await page.evaluate(() => { (window as Window & { navigationMarker?: string }).navigationMarker = "same-document"; });
      if (width > 700) {
        await link.focus();
        await expect(link).toBeFocused();
        await page.keyboard.press("Enter");
      } else await link.click();
      await expect(page).toHaveURL(/\/ada\/requests$/);
      await expect(page.locator("#account-title")).toHaveText("Requests");
      await expectRequestsActive(page);
      expect(await page.evaluate(() => (window as Window & { navigationMarker?: string }).navigationMarker)).toBe("same-document");

      if (width <= 700) {
        await page.evaluate(() => document.documentElement.style.setProperty("--safe-bottom", "24px"));
        const metrics = await mobile.evaluate(bar => {
          const bounds = bar.getBoundingClientRect();
          const contentBottom = bounds.bottom - parseFloat(getComputedStyle(bar).paddingBottom);
          return {
            viewport: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
            bottom: bounds.bottom, padding: getComputedStyle(bar).paddingBottom,
            items: [...bar.querySelectorAll("a")].map(item => {
              const rect = item.getBoundingClientRect();
              const range = document.createRange();
              range.selectNodeContents(item);
              const label = range.getBoundingClientRect();
              return { width: rect.width, left: rect.left, right: rect.right, bottom: rect.bottom,
                labelLeft: label.left, labelRight: label.right, contentBottom };
            }),
          };
        });
        expect(metrics.items).toHaveLength(5);
        expect(metrics.scroll).toBeLessThanOrEqual(metrics.viewport);
        expect(metrics.bottom).toBe(900);
        expect(metrics.padding).toBe("24px");
        for (const item of metrics.items) {
          expect(item.width).toBeCloseTo(metrics.viewport / 5, 1);
          expect(item.left).toBeGreaterThanOrEqual(0);
          expect(item.right).toBeLessThanOrEqual(metrics.viewport);
          expect(item.labelLeft).toBeGreaterThanOrEqual(item.left);
          expect(item.labelRight).toBeLessThanOrEqual(item.right);
          expect(item.bottom).toBeLessThanOrEqual(item.contentBottom);
        }
      } else {
        // System font widths differ between Windows and Linux. Verdana also
        // exercises wider tablet labels on Windows, where both fallbacks fit.
        const fonts = ["system-ui, sans-serif", "sans-serif"];
        if (width <= 900) fonts.push("Verdana, sans-serif");
        for (const font of fonts) {
          await page.evaluate(font => { document.body.style.fontFamily = font; }, font);
          const metrics = await desktop.evaluate(nav => {
            const header = nav.parentElement!;
            const bounds = header.getBoundingClientRect();
            const style = getComputedStyle(header);
            return { right: nav.getBoundingClientRect().right,
              scroll: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth,
              contentLeft: bounds.left + parseFloat(style.paddingLeft),
              contentRight: bounds.right - parseFloat(style.paddingRight),
              controls: [...header.querySelectorAll<HTMLElement>(":scope > *")]
                .filter(item => getComputedStyle(item).display !== "none")
                .map(item => ({ name: item.id || item.className, left: item.getBoundingClientRect().left,
                  right: item.getBoundingClientRect().right })),
            };
          });
          expect(metrics.right).toBeLessThanOrEqual(width);
          expect(metrics.scroll, `${font}: ${JSON.stringify(metrics.controls)}`).toBeLessThanOrEqual(metrics.viewport);
          for (const control of metrics.controls) {
            expect(control.left, `${font}: ${control.name}`).toBeGreaterThanOrEqual(metrics.contentLeft - .5);
            expect(control.right, `${font}: ${control.name}`).toBeLessThanOrEqual(metrics.contentRight + .5);
          }
          for (let i = 1; i < metrics.controls.length; i++) {
            expect(metrics.controls[i].left).toBeGreaterThanOrEqual(metrics.controls[i - 1].right);
          }
        }
        await page.evaluate(() => { document.body.style.fontFamily = ""; });
      }
      await page.screenshot({ path: testInfo.outputPath("primary-requests.png") });
      await page.locator('[data-account-route="profile"]').click();
      await expect(page.locator("#account-title")).toHaveText("Profile");
      await expect(nav.getByRole("link", { name: "Requests", exact: true })).not.toHaveAttribute("aria-current", "page");
      await page.locator('[data-account-route="requests"]').click();
      await expect(page).toHaveURL(/\/ada\/requests$/);
      await expectRequestsActive(page);
      for (const [name, path, view] of [["Discover", "/", "discover"], [width > 700 ? "Your library" : "Library", "/library", "library"], ["Settings", "/settings", "settings"]]) {
        await nav.getByRole("link", { name, exact: true }).click();
        await expect(page).toHaveURL(`http://127.0.0.1:4173${path}`);
        await expect(page.locator(`#${view}`)).toBeVisible();
        await expect(nav.getByRole("link", { name, exact: true })).toHaveAttribute("aria-current", "page");
        await expect(nav.getByRole("link", { name: "Requests", exact: true })).not.toHaveAttribute("aria-current", "page");
      }
    });
  }
}

for (const width of [1440, 320]) {
  test(`Requests query state survives reload and browser navigation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const nav = page.locator(width > 700 ? ".header-nav" : ".tab-bar");
    await page.goto("/ada/requests?q=reo&status=available&page=2");
    await expectRequestsActive(page);
    await expect(page.getByRole("searchbox", { name: "Search request history" })).toHaveValue("reo");
    await expect(page.getByRole("combobox", { name: "Request status" })).toHaveValue("available");
    await page.reload();
    await expectRequestsActive(page);
    await nav.getByRole("link", { name: width > 700 ? "Your library" : "Library", exact: true }).click();
    await expect(page).toHaveURL(/\/library$/);
    await page.goBack();
    await expectRequestsActive(page);
    await expect(page).toHaveURL(/q=reo&status=available&page=2/);
    await page.goForward();
    await expect(page.locator("#library")).toBeVisible();
    await nav.getByRole("link", { name: "Requests", exact: true }).click();
    await expect(page).toHaveURL(/\/ada\/requests$/);
    await expect(page.getByRole("searchbox", { name: "Search request history" })).toHaveValue("");
    await expect(page.getByRole("combobox", { name: "Request status" })).toHaveValue("all");
  });

  test(`primary Requests opens the admin's own account while viewing another account at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const nav = page.locator(width > 700 ? ".header-nav" : ".tab-bar");
    await page.goto("/bea/requests?q=another&status=requested&page=2");
    await expectRequestsActive(page);
    await expect(page.locator('[data-account-route="requests"]')).toHaveAttribute("href", /\/bea\/requests/);
    await expect(nav.getByRole("link", { name: "Requests", exact: true })).toHaveAttribute("href", "/ada/requests");
    await page.locator('[data-account-route="profile"]').click();
    await expect(page).toHaveURL(/\/bea$/);
    await nav.getByRole("link", { name: "Requests", exact: true }).click();
    await expect(page).toHaveURL(/\/ada\/requests$/);
    await expect(page.locator('[data-account-route="requests"]')).toHaveAttribute("href", "/ada/requests");
  });
}

test("primary Requests href follows the authenticated identity after switching accounts", async ({ page }) => {
  await page.goto("/ada/requests");
  await expectRequestsActive(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.locator("#login-form").getByLabel("Username").fill("bea");
  await page.locator("#login-form").getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const requests = page.locator(".header-nav").getByRole("link", { name: "Requests", exact: true });
  await expect(requests).toHaveAttribute("href", "/bea/requests");
  await requests.click();
  await expect(page).toHaveURL(/\/bea\/requests$/);
});

test("primary Requests is available to regular users and encodes their username", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await page.route("**/api/auth/me", route => route.fulfill({ json: {
    id: 3, username: "Music Listener", role: "user", csrfToken: "csrf-listener",
  } }));
  await page.goto("/library");
  const mobile = page.locator(".tab-bar");
  const links = mobile.locator("a:visible");
  await expect(links).toHaveText(["Discover", "Library", "Requests", "Rooms"]);
  await expect(mobile.getByRole("link", { name: "Settings", exact: true })).toBeHidden();
  const bounds = await links.evaluateAll(items => items.map(item => {
    const rect = item.getBoundingClientRect();
    return { width: rect.width, left: rect.left, right: rect.right };
  }));
  for (const [index, rect] of bounds.entries()) {
    expect(rect.width).toBeCloseTo(80, 1);
    expect(rect.left).toBeCloseTo(index * 80, 1);
    expect(rect.right).toBeLessThanOrEqual(320);
  }
  const requests = mobile.getByRole("link", { name: "Requests", exact: true });
  await expect(requests).toBeVisible();
  await expect(requests).toHaveAttribute("href", "/Music%20Listener/requests");
  await requests.click();
  await expect(page).toHaveURL(/\/Music%20Listener\/requests$/);
  await expectRequestsActive(page);
  await page.screenshot({ path: testInfo.outputPath("primary-requests-user.png") });
});

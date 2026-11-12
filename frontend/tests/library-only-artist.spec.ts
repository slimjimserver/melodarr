import { expect, test } from "@playwright/test";

const variousArtistsId = "89ad4ac3-39f7-470e-963a-56509c546377";

test("library-only artist refresh and unmatched Plex albums avoid global discography loading", async ({ page, request }) => {
  await request.post("/__reset");
  await request.post("/api/auth/login", { data: { username: "ada", password: "fixture-password" } });
  let refreshes = 0;
  let revalidations = 0;
  const data = {
    id: variousArtistsId, name: "Various Artists", libraryOnly: true,
    metadataSource: "Library", provisional: false, availableInPlex: true,
    sections: { Album: [
      { id: "owned-group", title: "Owned compilation", type: "Album", date: "2020", secondaryTypes: [] },
      { id: "plex:unmapped", title: "Unmatched compilation", type: "Album", secondaryTypes: [],
        localOnly: true, plexUrl: "https://app.plex.tv/desktop/#!/album/unmapped" },
    ] },
  };
  await page.route(new RegExp(`/api/music/artist/${variousArtistsId}(?:[/?]|$)`), route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/refresh")) {
      refreshes++;
      return route.fulfill({ json: data });
    }
    if (path.endsWith("/revalidate") || path.endsWith("/revalidation")) {
      revalidations++;
      return route.fulfill({ json: { status: "library-only", polling: false } });
    }
    if (path.endsWith("/anime")) return route.fulfill({ json: { anime: [] } });
    if (path.endsWith("/availability")) return route.fulfill({ json: { settled: true } });
    return route.fulfill({ json: data });
  });
  await page.goto(`/artists/${variousArtistsId}`);
  await expect(page.locator("#detail-subtitle")).toHaveText("Albums in your libraries");
  await expect(page.getByRole("button", { name: "Add artist to Lidarr" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Refresh discography", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open in Plex", exact: true })).toHaveAttribute(
    "href", "https://app.plex.tv/desktop/#!/album/unmapped",
  );
  await expect(page.locator('a[href*="release-groups/plex"]')).toHaveCount(0);
  await page.getByRole("button", { name: "Refresh library albums", exact: true }).click();
  await expect(page.locator("#detail-message")).toHaveText("Library albums refreshed.");
  expect(refreshes).toBe(1);
  expect(revalidations).toBe(0);
});

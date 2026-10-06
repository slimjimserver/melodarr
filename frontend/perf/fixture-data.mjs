import { createReadStream } from "node:fs";
import { join } from "node:path";

// Add deterministic benchmark HTTP responses to the existing fixture only when
// explicitly enabled. Browser caching/throttling is exercised without routing.
export function createBenchmarkFixture({
  artistDetail,
  releaseGroupDetail,
  send,
  iconsRoot,
}) {
  let requests = 0;
  const artwork = (kind, id) => `/api/artwork/${kind}/${id}`;
  return (request, response, url) => {
    const respond = (body, status = 200) => {
      send(response, status, body);
      return true;
    };
    if (url.pathname === "/__benchmark-state") return respond({ requests });
    if (url.pathname === "/api/auth/me")
      return respond({
        id: 1,
        username: "ada",
        role: "admin",
        csrfToken: "csrf-ada",
      });
    if (url.pathname === "/api/search")
      return respond({
        results: [
          {
            id: "fixture-artist",
            name: "Fixture Artist",
            type: "Group",
            coverArt: artwork("artist", "fixture-artist"),
          },
        ],
      });
    if (url.pathname === "/api/music/artist/fixture-artist") {
      const data = artistDetail();
      data.coverArtLarge = artwork("artist", "fixture-artist");
      data.sections.Album[0].coverArt = artwork(
        "release-group",
        "fixture-album",
      );
      // Below-fold cards exercise the application's own lazy image loading.
      data.sections.Album.push(
        ...Array.from({ length: 30 }, (_, i) => ({
          id: `fixture-lazy-${i}`,
          title: `Later Album ${i}`,
          type: "Album",
          date: "2020",
          secondaryTypes: [],
          coverArt: artwork("release-group", `fixture-lazy-${i}`),
        })),
      );
      return respond(data);
    }
    if (url.pathname === "/api/music/release-group/fixture-album") {
      return respond({
        ...releaseGroupDetail(),
        coverArtLarge: artwork("release-group", "fixture-album"),
      });
    }
    if (url.pathname.endsWith("/anime")) return respond({ anime: [] });
    if (url.pathname.endsWith("/availability"))
      return respond({ settled: true, releaseGroups: {} });
    if (
      url.pathname === "/api/request/release-group" &&
      request.method === "POST"
    ) {
      let body = "";
      request.on("data", (chunk) => {
        body += chunk;
      });
      request.on("end", () => {
        try {
          if (JSON.parse(body).mbid !== "fixture-album")
            return send(response, 400, { error: "Wrong benchmark target" });
          requests += 1;
          send(response, 200, {
            pending: true,
            message: "Fixture request accepted (no acquisition).",
          });
        } catch {
          send(response, 400, { error: "Invalid benchmark request" });
        }
      });
      return true;
    }
    if (url.pathname.startsWith("/api/artwork/")) {
      response.writeHead(200, {
        "content-type": "image/png",
        "cache-control": "public, max-age=3600",
      });
      createReadStream(join(iconsRoot, "melodarr-512.png")).pipe(response);
      return true;
    }
    return false;
  };
}

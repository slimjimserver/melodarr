import { createReadStream, existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";

const root = process.cwd();
const staticRoot = join(root, "static");
const iconsRoot = join(root, "icons");
let signedInAs = "";
let settingsReads = 0;
let delayedProposalResponse;
let delayedProposalAborted = false;

const users = {
  ada: { id: 1, username: "ada", role: "admin", csrfToken: "csrf-ada", plexUsername: "target-user" },
  bea: { id: 2, username: "bea", role: "admin", csrfToken: "csrf-bea" },
};

function send(response, status, body) {
  if (response.destroyed || response.writableEnded) return false;
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(body));
  return true;
}

function animeDetail() {
  const user = users[signedInAs];
  return {
    id: 1,
    slug: "switching-anime",
    name: "Switching Anime",
    format: "TV",
    themes: [{
      id: 10,
      type: "OP",
      sequence: 1,
      song: { title: "Opening Theme", artists: ["Fixture Artist"] },
      proposals: [{
        id: user.id,
        releaseGroupTitle: `Proposal for ${user.username}`,
        username: user.username,
      }],
    }],
  };
}

function artistDetail() {
  return {
    id: "fixture-artist",
    name: "Fixture Artist",
    type: "Group",
    sections: {
      Album: [{
        id: "fixture-album",
        title: "Fixture Album",
        type: "Album",
        date: "2026-01-01",
        secondaryTypes: [],
      }],
    },
  };
}

function releaseGroupDetail() {
  return {
    id: "fixture-album",
    title: "Fixture Album",
    artist: "Fixture Artist",
    artistId: "fixture-artist",
    releases: [{
      id: "fixture-release",
      title: "Fixture Album",
      date: "2026-01-01",
      country: "US",
      format: "CD",
      trackCount: 1,
      status: "Official",
    }],
  };
}

function releaseDetail() {
  return {
    id: "fixture-release",
    title: "Fixture Album",
    artist: "Fixture Artist",
    date: "2026-01-01",
    country: "US",
    tracks: [{ number: 1, title: "Fixture Track", artist: "Fixture Artist" }],
  };
}

const similarArtists = Array.from({ length: 18 }, (_, index) => ({
  id: `similar-${index + 1}`,
  name: `Similar Artist ${index + 1}`,
  type: "Artist",
  rank: index,
  recommendationSource: "Last.fm",
  availableInLidarr: index === 0,
}));

function similarArtistPage(url) {
  const offset = Number(url.searchParams.get("offset") || 0);
  const limit = Number(url.searchParams.get("limit") || 12);
  const artists = similarArtists.slice(offset, offset + limit);
  const nextOffset = offset + limit < similarArtists.length ? offset + limit : null;
  return { artists, configured: true, offset, limit, total: similarArtists.length, nextOffset, hasMore: nextOffset !== null, pending: 0 };
}

function accountSettings(username = signedInAs) {
  settingsReads += 1;
  return {
    username,
    plexConfigured: false,
    plexLinked: false,
    listenbrainzUsername: settingsReads > 1 ? "authoritative-listen" : "before-save",
    lastfmUsername: settingsReads > 1 ? "authoritative-last" : "before-save",
    lastfmConfigured: true,
  };
}

const mimeTypes = {
  ".css": "text/css",
  ".js": "application/javascript",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

function staticPath(url) {
  const requested = decodeURIComponent(url.pathname);
  const rootPath = requested.startsWith("/icons/") ? iconsRoot : staticRoot;
  const relative = requested.startsWith("/icons/")
    ? requested.slice("/icons/".length)
    : requested.slice("/static/".length);
  const path = normalize(join(rootPath, relative));
  return path.startsWith(rootPath) ? path : "";
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url || "/", "http://127.0.0.1:4173");
  if (url.pathname === "/health") return send(response, 200, { ok: true });
  if (url.pathname === "/__reset" && request.method === "POST") {
    signedInAs = "";
    settingsReads = 0;
    delayedProposalResponse = undefined;
    delayedProposalAborted = false;
    return send(response, 200, { ok: true });
  }
  if (url.pathname === "/__fixture-state") {
    return send(response, 200, {
      delayedProposalPending: Boolean(delayedProposalResponse),
      delayedProposalAborted,
    });
  }
  if (url.pathname === "/__release-delayed-proposal" && request.method === "POST") {
    const delayed = delayedProposalResponse;
    delayedProposalResponse = undefined;
    return send(response, 200, {
      released: Boolean(delayed && send(delayed, 500, { error: "Stale approval from ada" })),
      aborted: delayedProposalAborted,
    });
  }
  if (url.pathname === "/api/auth/me") {
    return signedInAs ? send(response, 200, users[signedInAs]) : send(response, 401, { error: "Not signed in" });
  }
  if (url.pathname === "/api/auth/status") return send(response, 200, { firstAccount: false, plexConfigured: false });
  if (url.pathname === "/api/auth/login" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    return request.on("end", () => {
      const username = JSON.parse(body).username;
      signedInAs = username in users ? username : "ada";
      send(response, 200, users[signedInAs]);
    });
  }
  if (url.pathname === "/api/auth/logout" && request.method === "POST") {
    signedInAs = "";
    return send(response, 200, { message: "Signed out" });
  }
  if (url.pathname === "/api/settings") {
    return send(response, 200, {
      lidarr: { configured: false, defaults: {} },
      plex: { configured: false, libraries: [], librarySectionIds: [] },
      lastfm: { configured: false },
      musicbrainz: {
        baseUrl: "https://musicbrainz.org/ws/2",
        userAgent: "Melodarr fixture",
        requestIntervalMs: 1100,
      },
    });
  }
  if (url.pathname === "/api/settings/notifications") {
    return send(response, 200, { enabled: false, email: {}, webPush: {} });
  }
  if (url.pathname === "/api/music/artist/fixture-artist/similar") {
    return send(response, 200, similarArtistPage(url));
  }
  if (url.pathname === "/api/music/artist/fixture-artist") {
    return send(response, 200, artistDetail());
  }
  if (url.pathname === "/api/music/release-group/fixture-album") {
    return send(response, 200, releaseGroupDetail());
  }
  if (url.pathname === "/api/music/release/fixture-release") {
    return send(response, 200, releaseDetail());
  }
  if (url.pathname === "/api/anime/switching-anime") {
    return send(response, 200, animeDetail());
  }
  if (url.pathname === "/api/anime/switching-anime/themes/10/mapping-proposals/1/approve" && request.method === "POST") {
    delayedProposalResponse = response;
    response.once("close", () => {
      if (delayedProposalResponse === response) {
        delayedProposalResponse = undefined;
        delayedProposalAborted = true;
      }
    });
    return;
  }
  if (url.pathname === "/api/account/settings") {
    if (request.method === "POST") return send(response, 200, { message: "ListenBrainz saved" });
    return send(response, 200, accountSettings(url.searchParams.get("username") || signedInAs));
  }
  if (url.pathname === "/api/account/general" && request.method === "POST") {
    return send(response, 200, { username: "target-user-renamed", message: "Target account saved" });
  }
  if (url.pathname === "/api/account/lastfm" && request.method === "POST") {
    return send(response, 500, { error: "Last.fm was unavailable" });
  }
  if (url.pathname === "/api/library") return send(response, 200, { artists: [], artistCount: 0, releaseGroupCount: 0 });
  if (url.pathname.startsWith("/api/")) return send(response, 200, {});

  if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/icons/")) {
    const path = staticPath(url);
    if (path && existsSync(path)) {
      response.writeHead(200, { "content-type": mimeTypes[extname(path)] || "application/octet-stream" });
      createReadStream(path).pipe(response);
      return;
    }
  }
  response.writeHead(200, { "content-type": "text/html" });
  response.end(await readFile(join(staticRoot, "index.html")));
});

server.listen(4173, "127.0.0.1");

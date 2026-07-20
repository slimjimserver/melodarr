# Melodarr

A small, self-hosted music request interface that makes finding artists easier than Lidarr's native search.

Service addresses and tokens are configured in the web UI and stored in `settings.json` (`data/settings.json` with Docker). Disposable API-response entries are stored in a separate SQLite database at `data/cache/metadata.db`. Album artwork thumbnails are cached under `data/cache/artwork` and the general artwork cache is capped at 500 MB. Plex-library artist images are retained outside that eviction cap until a full Plex scan confirms that the artist was removed. Keep the data directory private; back up `melodarr.db`, `settings.json`, and `session-secret.key`, while the reproducible `cache/` directory may be excluded.

## Accounts and access

Melodarr requires an account. An empty installation redirects directly to owner setup, and the first account becomes an administrator. After setup, public registration is disabled: administrators create private, one-time invitation links from **Profile → Invitations** for additional users. Invitations expire after seven days. Administrators can access every section and configure services; invited users can access Discover and submit requests only.

Accounts, password hashes, and hashed invitation tokens are stored in `melodarr.db`; service credentials remain in `settings.json`. Melodarr generates a persistent session-signing secret at `data/session-secret.key`. Sign-in sessions normally last until the browser session ends; **Remember me** extends them to 30 days. For public HTTPS deployments, set `MELODARR_COOKIE_SECURE=true`.

## What it does

- Searches artists and albums through MusicBrainz, with discography and tracklist browsing.
- Recommends artists and albums from each user's linked ListenBrainz listening history, excluding prior requests and artists/albums already represented by connected Lidarr and Plex libraries.
- Builds Last.fm discovery suggestions from a weighted blend of one-month, six-month, and all-time listening, ranking artists by similarity strength and support from multiple favorites.
- Ranks albums from matched artists with a soft release-date boost, while excluding heavily listened albums, prior requests, Lidarr entries, and artists already present in Plex when those services are available.
- Uses personal taste tags to organize personalized album rows targeting ten options each. Sparse rows are backfilled from deeper tag candidates, then MusicBrainz-normalized, library/request-filtered, and recency-re-ranked instead of displaying the raw global tag chart.
- Shows Last.fm's global artist chart separately as **Popular on Last.fm**; entries without MusicBrainz IDs are excluded.
- Sends an artist to Lidarr using the configured root folder, profiles, monitoring, tags, and search defaults.
- Reads artists and album-level releases from selected Plex music libraries (optional).
- Gives administrators a **Settings → Jobs & Cache** dashboard for background-job status, manual job runs, cache sizes, entry counts, and targeted cache flushing.

## Project structure

- `backend/app.py` — minimal development executable and production WSGI entry point.
- `backend/application.py` — Flask application factory, security hook, and Blueprint registration.
- `backend/gunicorn.conf.py` — single-process threaded production-server configuration and worker lifecycle hook.
- `backend/worker.py` — launcher for recommendation refreshes, Lidarr follow-ups and availability scans, and Plex inventory/enrichment work.
- `backend/api_cache.py` — standalone SQLite caching for external JSON API responses and legacy-cache migration.
- `backend/artwork_cache.py` — disk-backed artwork downloading, serving, and eviction.
- `backend/config.py` — application paths, service endpoints, cache limits, and environment configuration.
- `backend/media_urls.py` — internal artist and release-group artwork URLs.
- `backend/recommendations.py` — ListenBrainz/Last.fm recommendation assembly and cache refreshes.
- `backend/routes/` — Flask Blueprints grouped by account, authentication, artwork, discovery, library, music, requests, settings, and frontend pages.
- `backend/security.py` — session lookup, authorization decorators, and CSRF enforcement.
- `backend/services/` — MusicBrainz, Lidarr, Plex, Last.fm, and ListenBrainz client operations.
- `backend/storage.py` — SQLite schema/connection handling, request history, and JSON-backed service settings.
- `backend/workers/` — periodic background-job implementations.
- `frontend/src/` — strict TypeScript source for the browser application.
- `frontend/static/` — committed HTML/CSS plus ignored JavaScript generated for Flask.
- `frontend/tsconfig.json` — TypeScript compiler settings; output is written to `frontend/static/`.
- `frontend/icons/` — service and metadata SVG icons.
- `data/` — persistent Docker runtime data (database, settings, session secret, and artwork cache); it is not committed.

## Configuration notes

- Connect Lidarr first. Use **Test connection** to load its folders, profiles, and tags, then save your request defaults.
- A Lidarr API key entered while editing an existing connection can be left blank to retain the saved key.
- Plex is optional. Test the connection, select the music libraries Melodarr should inspect, and save. Unselected Plex libraries are excluded from **Your library** and recommendation filtering.
- ListenBrainz is optional. Each Melodarr user can add their own public username from the account menu to receive their own collaborative-filtering recommendations.
- Last.fm is optional. Link a Last.fm username and API key in **Linked accounts**; the key remains private to that Melodarr account.
- Live MusicBrainz requests are serialized across web and recommendation work, respect its public rate limit, and retry temporary `429`/`503` responses with bounded backoff.
- MusicBrainz work is scheduled in four levels: full discographies first, other clicks second, hover/focus prefetches third, and background recommendation lookups last. Artist cards and release groups in artist discographies prefetch after a short delay; individual release tracklists remain click-only. Waiting prefetches recheck the cache after yielding so a click does not duplicate upstream work.
- Full artist discographies use the highest MusicBrainz priority across every results page and retry transient timeouts or `429`/`5xx` responses automatically. For multi-user fairness, a discography yields after two consecutive MusicBrainz pages whenever another interactive search is waiting; prefetches and recommendation work remain queued behind both. The browser allows up to two minutes for unusually large discographies and limits lazy artwork downloads to three at a time so covers cannot occupy all web-request threads.
- MusicBrainz metadata is cached for 90 days, while search results remain short-lived. Artist pages include a **Refresh discography** action that force-fetches the artist and every discography page; failed refreshes retain the previous cached metadata.
- Recommendation providers refresh independently: a ListenBrainz or Last.fm outage does not discard results from the other provider. Partial refreshes are cached and retried after five minutes; complete caches refresh every twelve hours.
- Selected Plex music libraries are cached locally, including artists, albums, EPs, singles, other album-level releases, artist artwork, and the Plex/MusicBrainz GUID mappings Plex provides. A recently-added scan merges new records every five minutes, while a full scan replaces the snapshot every twelve hours so removed records and their retained artwork disappear automatically. A separate low-priority enrichment job resolves Plex's MusicBrainz release IDs to release-group IDs and warms the normal 90-day discography cache for every MusicBrainz-linked Plex artist. Interactive MusicBrainz searches and clicks take priority over this background work. Owned release groups and their exact Plex editions are marked in the artist and album views.
- Lidarr album completion statistics are cached by a five-minute library scan. Artist discographies show whether each release group is fully available, incomplete and searchable again, or ready to request; the job can also be run manually from **Settings → Jobs & Cache**.
- Saving or removing a linked ListenBrainz/Last.fm account invalidates that user's old cache and wakes the in-process recommendation worker immediately; Discover polls until the replacement cache is ready.
- New release-group requests are persisted before returning to the browser. Melodarr batches simultaneous albums for the same artist behind one Lidarr artist refresh, then queues every album search automatically; slow refreshes and container restarts do not require the user to request an album again.
- Select the account icon to open your profile (`/<username>`), then use the General, Linked accounts, or Notifications settings pages. Profile includes your recent artist and release-group requests.

## Run with Docker Compose

1. Create a directory for container's appdata. Create ./data folder first before building the container for the first time to make sure permissions are valid.
2. Run `docker compose up -d`.
3. Open `http://localhost:5056` and use **Settings** to connect Lidarr and Plex.

Compose starts one `melodarr` container. Gunicorn runs one web process with
sixteen request-handling threads, and its lifecycle hook starts background loops
for recommendation-cache refreshes and queued Lidarr follow-ups. Keeping a single
web process guarantees the scheduled work is not duplicated.

Inspect it with `docker compose ps` and follow logs with
`docker compose logs -f melodarr`.

For Lidarr running in Docker, use a URL reachable from the Melodarr container (for example `http://lidarr:8686` on the same Docker network).

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
py backend/app.py
```

For local recommendation refreshes, run `py -m backend.worker` in a second
terminal. The Flask development server is intended only for local development;
Docker uses Gunicorn and starts the refresh thread automatically.

Browser JavaScript is generated from TypeScript and intentionally ignored by
Git. After cloning or changing the frontend, install Node.js 22 and pnpm, then
type-check and build it before starting the local Python server:

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm run check
pnpm run build
```

Docker builds the TypeScript sources in a dedicated Node stage, so Node.js is
not included in the final application image. CI also compiles the frontend from
scratch instead of relying on local build artifacts.

## Run the regression tests

The test suite uses Python's built-in `unittest` runner and stores its database,
settings, session secret, and artwork cache in a temporary directory.

```powershell
py -m unittest discover -s tests -v
```

To run it using the application image instead:

```powershell
docker compose run --rm melodarr python -m unittest discover -s tests -v
```

## Continuous integration

The GitHub Actions workflow in `.github/workflows/ci.yml` runs on every push and
pull request, and can also be started manually. It performs three checks:

1. Type-checks and builds the strict TypeScript frontend with Node.js 22.
2. Installs the pinned backend dependencies on Python 3.13 and runs the full
   regression suite.
3. Builds the Docker image and runs the same suite inside that image.

The Docker build context excludes local databases, settings, session secrets,
caches, virtual environments, and other development artifacts through
`.dockerignore`.

## License

Melodarr is licensed under the GNU General Public License v3.0. See `LICENSE`
for the full terms.

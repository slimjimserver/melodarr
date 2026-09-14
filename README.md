<p align="center">
  <img src="frontend/icons/melodarr.svg" alt="Melodarr icon" width="160">
</p>

# Melodarr

## 1. Overview

Melodarr is a self-hosted music discovery and request app for Lidarr. It gives household members a simple interface for finding artists and albums, exploring personalized recommendations, and sending requests to Lidarr, while optional Plex integration prevents suggestions for music that is already in your library.

Melodarr uses MusicBrainz for music metadata and can use ListenBrainz and Last.fm listening history for recommendations. It includes private accounts, administrator-managed invitations, persistent request history, background library scans, and local metadata and artwork caches.

### Personal recommendations

By default, the homepage offers up to six picks per section: **More from artists you love**, **Because you requested…**, and **Try something new**. Requests are strong taste signals, with a per-artist cap so a discography request does not dominate. Optional Last.fm and Plex listening history add context; ListenBrainz contributes its personalized suggestions. Provider ranks are combined, repeated artists are limited, and repeatedly seen suggestions gradually lose priority without treating one ignored card as a dislike.

Missing albums from familiar artists are eligible even when the artist is already in Lidarr. Downloaded albums, current download queues, pending searches, and your previous album requests are filtered out. Catalog presence alone does not mean an album is available. Each card explains its connection to your taste and offers an album request, **More like this**, and **Not interested**, with undo. Cards support both vertical page swipes and horizontal browsing, with larger artwork for high-density displays. Last.fm artist charts remain under **Browse popular music**.

**Shape your recommendations** lets each user choose **More familiar artists**, **Balanced**, or **More discovery**. The preferred section appears first and can show up to eight picks, while the opposite section shows up to four; Balanced keeps six per section. Users can optionally search for and choose up to five favorite artists, even without linked listening history, and edit or remove them later. Favorite artists seed missing-album recommendations and, when the shared Last.fm key is configured, similar-artist discoveries.

On your account’s **Requests** page, turn off **Use for recommendations** for an individual request made for someone else. The request stays in your history and still counts as requested for availability filtering; only its contribution as a taste seed is removed. Other requests, listening history, or explicitly chosen favorites can still contribute the same artist. Preferences persist per account and can only be edited by that account. Changes queue new personal picks, and older in-flight builds are hidden if they predate the saved preferences.

**Popular albums right now** uses Apple Music’s public Top 100 album feeds for the **United States** and **Japan**, selected using **Country**, with no Apple account or API key required. Switch between **New & popular** (original releases from the last 180 days) and **All chart albums**. Original chart positions are retained; owned, pending, dismissed, and already-featured albums are excluded. Exact title and artist-credit matching resolves chart entries to MusicBrainz; ambiguous or unavailable matches are skipped. Chart data is shared and cached separately for each country for six hours, with dated fallback results and short retries during outages. The US and Japan have independent background jobs that check their caches every five minutes. Personal feeds never fetch or resolve these charts: the homepage joins cached chart snapshots to the current library and personal filters. Pending charts update separately without rebuilding the personal cards, and ready charts remain usable while the first personal feed is being prepared. This is Apple Music's album chart, not Billboard's song chart.

Request-based artist discovery needs the administrator's shared Last.fm API key, but does not require a personal Last.fm account. MusicBrainz supplies familiar-artist album catalogs, scanning up to 300 release groups per seed in the background. Requests and feedback queue a refresh; **Refresh picks** also starts a background refresh. Existing caches upgrade automatically after restarting with this version.

**Your recommendation activity** shows private counts over 30 days. An impression means at least half a card was visible for one second, counted once per item per day. Requests within seven days of an impression are counted as associated outcomes, not proof of causation. Verified subsequent Plex plays are shown separately from requests; playback counts are unavailable without matching Plex history. Exposure history is retained for up to 90 days of activity and feedback persists until undone or the account is deleted. Shared library inventory filters availability; other users' requests and feedback do not shape your taste profile.

## 2. Preview


### 📱 Screenshots

| Discover | Artist |
| :---: | :---: |
| <img src="https://raw.githubusercontent.com/slimjimserver/melodarr/main/docs/screenshots/melodarr_discover_page.png" alt="discover" width="100%" /> | <img src="https://raw.githubusercontent.com/slimjimserver/melodarr/main/docs/screenshots/melodarr_artist_page.png" alt="artist" width="100%" /> |
| **Release Group** | **Linked Accounts** |
| <img src="https://raw.githubusercontent.com/slimjimserver/melodarr/main/docs/screenshots/melodarr_release_group_page.png" alt="release-group" width="100%" /> | <img src="https://raw.githubusercontent.com/slimjimserver/melodarr/main/docs/screenshots/melodarr_linked_account_page.png" alt="linked-accounts" width="100%" /> |


## 3. Features

- Search MusicBrainz for artists and albums, then browse discographies, releases, and tracklists.
- Search AnimeThemes for an anime's openings, endings, episode ranges, and related series, then request conservatively matched MusicBrainz releases through Lidarr. Administrators can confirm the recommended automatic recording match or supply a correction; the selected recording, artist, and release-group MBIDs are stored permanently in the local SQLite registry ahead of the disposable API cache.
- Request a complete artist or an individual release group through Lidarr.
- Apply Lidarr root folder, quality profile, metadata profile, monitoring, tag, and automatic-search defaults.
- Discover personalized artists and albums from linked ListenBrainz and Last.fm accounts.
- Filter recommendations and request controls using existing Lidarr entries, previous requests, and selected Plex music libraries.
- Browse the artists and album-level releases already available in Plex, with links back to Plex.
- Track queued Lidarr searches and album availability with automatic background jobs.
- Cache metadata and artwork locally to reduce upstream requests, while revalidating viewed artist discographies in the background.
- Create private user accounts through one-time, seven-day administrator invitations.
- Inspect job status, run maintenance jobs, and flush individual caches from the administrator dashboard.

## 4. Quick start

Melodarr is designed to run with Docker Compose. The included [`docker-compose.yml`](docker-compose.yml) uses the published `slimjimserver/melodarr:latest` image and persists application data in `./data`.

1. Download or copy the docker compose file from the repository.
2. Create the data directory before starting the container:

   ```bash
   mkdir -p data
   ```

   On Linux, give Melodarr's fixed container user ownership of the directory:

   ```bash
   chown -R 1000:1000 data
   ```

   The image runs directly as UID/GID `1000:1000`. It does not start as root or change bind-mount ownership during startup.

3. Start Melodarr:

   ```bash
   docker compose up -d
   ```

4. Open [http://localhost:5056](http://localhost:5056) and create the owner account. The first account is the administrator.
5. Open **Settings**, connect Lidarr, test the connection, and choose the defaults for new requests. Plex is optional.


## 5. Configuration

### Environment variables

No additional environment variables are required for the included Docker Compose setup. It already stores the main database and metadata cache beneath the persistent `/app/data` mount.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MELODARR_DATABASE` | `<project>/melodarr.db` | Main SQLite database containing accounts, invitations, request history, and queued work. The image and Compose set this to `/app/data/melodarr.db`. |
| `MELODARR_CACHE_DATABASE` | `cache/metadata.db` beside the main database | Disposable external API-response cache. The image and Compose set this to `/app/data/cache/metadata.db`. |
| `MELODARR_SETTINGS` | `settings.json` beside the main database | Service configuration and credentials saved through the web UI. |
| `MELODARR_SECRET_KEY_FILE` | `session-secret.key` beside the main database | Persistent generated session-signing key file. |
| `MELODARR_VAPID_PRIVATE_KEY_FILE` | `vapid-private.pem` beside the main database | Persistent stable Web Push/VAPID identity. Back up and restore it with the database. |
| `MELODARR_SECRET_KEY` | Generated and saved to the key file | Explicit session-signing secret. Normally leave unset so Melodarr manages a persistent key in the data volume. |
| `MELODARR_ARTWORK_CACHE` | `<project>/data/cache/artwork` | Directory used for downloaded artist and album artwork. |
| `MELODARR_COOKIE_SECURE` | `false` | Set to `true` when Melodarr is served through HTTPS so session cookies are marked secure. |
| `PORT` | `5056` | Port used only by the local Flask development server. The production Gunicorn container listens on port `5056`. |
| `FLASK_DEBUG` | unset | Set to `1` only when running the local development server. Do not enable it in production. |

### Unraid Community Applications

Use this volume mapping:

```text
Host:      /mnt/user/appdata/melodarr
Container: /app/data
```

Do not map a host directory to `/app`; doing so hides Melodarr's application files. Only `/app/data` should be used for persistent storage.

To use Unraid's native `nobody:users` identity, prepare the directory once from the Unraid terminal:

```bash
mkdir -p /mnt/user/appdata/melodarr
chown -R 99:100 /mnt/user/appdata/melodarr
```

Then add this in the container's **Extra Parameters** field:

```text
--user 99:100
```

Remove any `PUID` or `PGID` variables from an older template; Melodarr no longer uses them. Docker's `--user` override starts Melodarr directly as `99:100`, without a root entrypoint.

For an HTTPS deployment, add this to the service's `environment` block in `docker-compose.yml`:

```yaml
MELODARR_COOKIE_SECURE: "true"
```

### Service configuration

Service credentials are normally configured after signing in.

- **MusicBrainz:** use the hosted WS2 endpoint or set a compatible self-hosted WS2 base URL, user agent, and request interval. Keep the default 1100 ms interval for musicbrainz.org; a local server can usually use 0 ms. The connection can be tested before saving.
- **Lidarr (required for requests):** hostname or IP address, port, SSL choice, API key, and optionally an external browser-facing URL. After testing the connection, choose the root folder, quality and metadata profiles, monitoring behavior, tags, and automatic-search behavior.
- **Plex (optional):** sign in with the Plex account that owns the server, choose one of its advertised connections, and select one or more music libraries to scan. Plex tokens are retrieved through the secure Plex PIN flow and are never pasted into Melodarr.
- **ListenBrainz (optional, per user):** public ListenBrainz username.
- **Last.fm API access (optional, administrator-managed):** the owner or an administrator saves one Last.fm API key for the whole Melodarr instance. The key is never returned by the API or shown again after it is saved.
- **Last.fm listening history (optional, per user):** each user can add their own public Last.fm username to receive recommendations based on their listening history. Individual users do not need Last.fm API keys.

Settings and service credentials are stored in `data/settings.json` when using Docker. Keep the data directory private. For a consistent backup, stop Melodarr cleanly before copying `melodarr.db`, `settings.json`, `session-secret.key`, and `vapid-private.pem`. The VAPID file is the stable Web Push identity; restore it with the database to avoid invalidating existing browser subscriptions. Set `MELODARR_VAPID_PRIVATE_KEY_FILE` only when placing that key in another private persistent location. If the service must remain online, back up `melodarr.db` with SQLite's online backup API or the SQLite shell's `.backup` command; do not make a raw copy of a live database because committed data may still be in its WAL file. The reproducible `cache/` directory can be excluded from backups.

Melodarr is licensed under the [GNU General Public License v3.0](LICENSE).

### Background anime artist matching

The existing worker process automatically discovers verified AnimeThemes/MusicBrainz
artist links, including links saved before this feature was installed. It checks each
linked AnimeThemes artist every 24 hours and saves the performance snapshot to the
application database. Artist pages use that snapshot; a provider outage preserves the
last successful listing. Newly discovered performances are queued without requiring
an anime page visit.

One performance is processed at a time with a five-second pause between jobs.
MusicBrainz lookups use background priority, verified artist IDs, conservative title
and version checks, and recording-to-release expansion (at most 300 releases per
candidate). Official studio singles rank before EPs/albums and special editions.
Automatic results, preferred releases, alternatives, and reverse anime associations
are durable. Successful matches do not expire; manual mappings and rejections take
precedence. Songs shared by multiple performances reuse their saved mapping.

SQLite job leases recover after a worker restart (up to one hour for an interrupted
job). Provider exceptions retry with exponential backoff from five minutes to one
day. Resolver failures retry after one hour, unmatched songs after one day, and
ambiguous candidates after seven days. No downloads or requests are submitted by
this enrichment worker. Deploy the backend and restart the normal worker process;
no separate scheduler or manual backfill command is needed.

Resolved anime song matches with exactly one Single release group and only
Album/EP alternatives are automatically confirmed to that Single. This also
applies when existing saved matches are read by the page or enrichment worker.
The original automatic evidence and alternatives remain stored; the confirmed
mapping supplies one request target. Multiple Singles, unknown release types,
and ambiguous song matches still require a choice. Existing reviewed mappings,
including rejections and manually selected albums, are never overwritten.

When a romaji recording search fails for a verified artist, the anime resolver
also browses that artist's recordings and compares native titles and aliases
using Melodarr's existing Japanese romanizer. Exact title and complete artist-credit
checks still apply, and live/TV-edit versions are excluded. A unique recording is
expanded into its releases and follows the same durable confirmation rules.
The source title, matched native recording title, recording ID, and match method
are retained in automatic-match evidence. Catalog pages are cached for 24 hours
and scanned at background priority in batches of up to 1,000 recordings. Progress
is saved after every page and shared by the artist's songs; larger catalogs resume
on subsequent attempts rather than hitting a permanent cap. Incomplete scans retry
after five minutes and appear as "Catalog scan pending". An incomplete catalog or
multiple matching recordings cannot auto-confirm a song. Startup requeues old
hard-limit failures without altering successful or manually reviewed mappings.
Previously unmatched songs try this fallback when their normal retry is due.

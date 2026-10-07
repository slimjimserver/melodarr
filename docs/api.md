# Melodarr API

Melodarr exposes versioned recording, search, and AnimeThemes resolver endpoints
for trusted external automation. The instance API key authorizes these machine
endpoints; it does not grant access to private accounts or administrator settings.

A machine-readable version of this contract is available in
[`openapi.yaml`](openapi.yaml).

The session/guest Rooms API is documented separately in [`rooms.md`](rooms.md).
Automation API keys do not authorize Room hosting or queue management.

## Artist Summary (browser session)

`GET /api/music/artist/{artistMbid}/summary` is supplemental, session authenticated,
and requested only when the artist's Summary view is selected. The ordinary
artist/discography response never fetches Deezer or Wikipedia. Invalid UUIDs
return `400`; supplemental/cache/provider failures degrade to a `200` response.

The response contains `bio` (plain lead text, `source`, `sourceUrl`, or null),
ordered `topTracks`, canonical `releaseGroups` keyed by MBID, and `pending`.
`sources.bio` and `sources.top_tracks` expose `pending`, `stale`, and `fetchedAt`.
Each track keeps Deezer's original position, public track/contributor/album
details, nullable recording and release-group MBIDs, and resolution provenance.
Release-group display titles/artwork and live request state are joined from
existing canonical metadata and availability; they are never stored as Deezer
metadata. Request buttons use the existing release-group request endpoint.

Snapshots and identity documents reuse `api_cache`, under the `artist-summary`
namespace, visible/clearable through existing maintenance diagnostics. Top
Tracks freshness is exactly **86,400 seconds**; successful Wikipedia biographies
are fresh for **2,592,000 seconds (30 days)**. Stale successes remain stored and
are returned immediately during on-demand revalidation. Failed refreshes retain
those successes and back off for **15 minutes**. Missing Wikipedia sources and
ambiguous identity mappings retry after **7 days**. Successful artist, recording,
and album-context mappings have an effectively permanent **100-year retention**;
snapshot storage uses the same retention to survive expired-row cleanup, while
its explicit `fetched_at` determines the shorter freshness window.

Transport, HTTP, and incomplete-provider-response failures use only a **15-minute**
identity backoff, never the seven-day unresolved mapping cache. Resolver version 5
automatically retries older negative track/album identities (including version 4)
and repairs older unresolved Top Tracks snapshots on the next Summary visit,
bypassing old refresh leases. Successful identities and Wikipedia caches remain
reusable. Identity repairs preserve the original daily track ordering and its
`fetched_at`; no manual cache clear is required.

Two lazy daemon workers share a bounded 32-job queue. Atomic SQLite leases
coalesce refreshes across web processes and recover after 30 minutes if a
process exits. Deezer HTTP requests are sequential with at least 250 ms between
starts, using 3.05-second connect/10-second read timeouts. MusicBrainz resolution
uses its configured mirror, shared caches, and existing background pacing.

Artist identities come exclusively from exact MusicBrainz Deezer/Wikipedia/
Wikidata relationships; no fuzzy artist lookup is used. Wikipedia lead text is
read using the [MediaWiki TextExtracts API](https://www.mediawiki.org/wiki/Extension:TextExtracts),
and Wikidata sitelinks use [wbgetentities](https://www.mediawiki.org/wiki/Wikibase/API).
Recording candidates come from a complete local or configured MusicBrainz ISRC
lookup. Partial local album evidence is never treated as ISRC uniqueness.
Remote ISRC lookups use `/isrc/{ISRC}?inc=artist-credits`. Release collections are
consulted only when contributor/title/duration signals leave multiple candidates:
complete cached collections first, then the existing recording-to-release browse
helper. Unique ISRC and contributor matches need no recording-disambiguation
release lookup. Once a recording is selected, the separate recording → releases
→ release-group flow still supplies album/request context.
Multiple candidates require contributor-set, title/version, duration (±5 seconds),
or album-context evidence; a tied result stays unresolved. Recording search is
allowed only for absent/empty ISRC results and requires exact contributors,
canonical artist credit, title/version, duration, and a unique result. Release
groups are chosen only from releases verified to contain that exact recording;
provider album relationships, album title, artist credit, date, and release
context can break ties. Equal plausible groups remain informational.

Ambiguous ISRC candidates require exact contributor names and canonical artist
credit. Deezer `title_short` supplies the base title when available; recording
version evidence is compared separately. Only `Explicit Version`/`explicit`
receive semantic equivalence; other version wording needs exact normalized
evidence. Narrow remaster wording is mastering context for ISRC-linked recordings,
so it need not appear in the recording title or mix disambiguation. Unlinked
fallback search retains the strict remaster title/version requirement.
One surviving candidate within ±5 seconds resolves as `isrc_title_duration`.
Remaining ties compare each recording's strongest eligible release containing
that exact recording: direct album relationship, title match, canonical artist,
compatible remaster evidence (specified year above unqualified remaster above
missing evidence), then the existing date/context ranks. Explicit conflicting
remaster years reject that release. Cached release collections are reused before
remote browse; missing track identities use bounded release-detail hydration.
One strongest recording resolves as `isrc_album_context`; equal recording scores
remain unresolved regardless of release count or provider order. Release-group
selection runs separately afterward and retains its existing scoring.

Release-group album matching ranks exact Deezer album relationships first, exact
normalized release/group titles next, and controlled remaster equivalence last.
The latter removes only a trailing parenthesis containing `remaster`/`remastered`
and one optional four-digit year, in either order. Release disambiguation can
supply compatible remaster-year evidence (including mono/stereo wording); empty
comments are allowed, while recognized conflicting years reject the fallback.
Other album edition text remains unchanged. Matching
editions collapse by release-group MBID; equally ranked distinct groups remain
unresolved. This fallback is recorded as `recording_album_remaster`.

Within those relationship/title/artist ranks, a valid full Deezer album date
(`YYYY-MM-DD`) ranks exact release-group `first-release-date` agreement first,
exact release-date agreement next, and the existing year-only evidence last.
Original group dates remain useful even when the containing release is a later
remaster. Partial/invalid dates add no exact-date evidence, and primary release
type (Album versus Single) adds no preference. Identical strongest scores across
distinct groups still remain unresolved: year-only evidence cannot break an
exact-date tie. Resolution-method labels are unchanged.

Before committing, smoke-test a real configured MusicBrainz mirror, Wikipedia
access, the Jhené Aiko/Sativa mapping, stale refreshes across application restarts,
and real Lidarr request-state transitions. Automated tests mock external
providers and submit acquisition actions only to the browser fixture.

## Authentication

Melodarr generates an API key automatically on first start. An administrator
can copy or regenerate it under **Settings → Services → Melodarr**.

Send the key in the `X-Api-Key` header:

```http
X-Api-Key: your-api-key
```

API-key requests do not require a browser session, cookie, or CSRF token. The
optional `MELODARR_AUTOMATION_API_KEY` environment variable overrides the
generated key and must contain at least 32 characters. When the override is
active, rotate the key by changing the environment variable and restarting
Melodarr.

Treat the key as a secret. Do not put it in logs, source control, screenshots,
or publicly shared command output. Regenerating it immediately invalidates the
previous key.

Versioned endpoints reuse `login_or_api_key_required` and accept either the
instance key or a signed-in session. A valid key bypasses CSRF only on routes
that explicitly enable API-key authentication. Session-only POSTs still require
the session's `X-CSRF-Token`. Missing/invalid credentials return `401`; a valid
session can still authenticate when no valid key is supplied. For recording
POST, a valid key identifies the automation origin even if a session cookie
is also present. The existing unversioned browser routes retain session/CSRF
authentication.

## Machine recording and search API

| Method | Endpoint | Behavior |
| --- | --- | --- |
| GET | `/api/v1/music/recordings/{recordingMbid}/availability` | Exact indexed Plex copies. |
| GET | `/api/v1/music/recordings/{recordingMbid}/acquisition` | Read-only acquisition target resolution. |
| GET | `/api/v1/music/recordings/{recordingMbid}/request` | Current local/cached recording lifecycle. |
| POST | `/api/v1/music/recordings/{recordingMbid}/request` | Initiate or reuse the global recording acquisition. |
| GET | `/api/v1/search?type=track&q=...` | Existing search results with compact recording state. |

Recording response shapes, UUID validation, lifecycle precedence, and status
codes match the corresponding browser endpoints documented below. Machine
search calls the same search pipeline and also accepts the existing `artist`,
`album`, and `anime` types. Availability, lifecycle, and search state enrichment
use local/cached evidence. Acquisition GET may perform its existing MusicBrainz
resolution but does not initiate work. Recording POST has no required body and
returns `202` for new accepted work, or `200` for an existing acquisition/READY
recording. Resolver/provider errors remain sanitized.

Curl examples (set `MELODARR_API_KEY` securely in your shell; no cookie or CSRF
token is required):

```bash
curl --get 'https://melodarr.example/api/v1/search' \
  -H "X-Api-Key: $MELODARR_API_KEY" \
  --data-urlencode 'type=track' --data-urlencode 'q=Melatonin Tinashe'

curl 'https://melodarr.example/api/v1/music/recordings/89448197-91b7-4f39-80a6-95455ee1eed8/availability' \
  -H "X-Api-Key: $MELODARR_API_KEY"

curl 'https://melodarr.example/api/v1/music/recordings/89448197-91b7-4f39-80a6-95455ee1eed8/acquisition' \
  -H "X-Api-Key: $MELODARR_API_KEY"

curl 'https://melodarr.example/api/v1/music/recordings/89448197-91b7-4f39-80a6-95455ee1eed8/request' \
  -H "X-Api-Key: $MELODARR_API_KEY"

curl -X POST 'https://melodarr.example/api/v1/music/recordings/89448197-91b7-4f39-80a6-95455ee1eed8/request' \
  -H 'Content-Type: application/json' -H "X-Api-Key: $MELODARR_API_KEY"
```

### Requester origin, history, and notifications

One global acquisition remains keyed by recording MBID. Recording requester and
pending-job associations explicitly store `source: user` with a real `user_id`,
or `source: automation` with `user_id: null`. Automation never creates a user or
borrows an administrator account. Partial unique indexes allow at most one
automation association per recording/job; user associations remain unique per
user. The migration preserves valid existing user associations and timestamps.

An initial automation acquisition uses the same locked release-group service,
Lidarr defaults, metadata-refresh/search queue, and worker wakeups as a user
request. Its release-group audit is stored in `automation_request_history`
(`source: automation`, null `user_id`), once per release group. It does not create
private user history or change a user's recommendation history. The recording
association records when automation joined that exact recording acquisition.
Personal request-history endpoints continue to list only that user's history.
The administrator Requests view (`GET /api/admin/requests`) combines user
history and the existing automation audit, including audit rows created before
this view was added, with shared pagination and release-group lifecycle/Plex
badges. Each entry has `source: user` or `source: automation`. Automation entries
show **Automation API**, with requester `id: null`, `userType: automation`, and
no user-profile link. Their entry IDs use `automation:<audit-id>` so they cannot
collide with the existing numeric user-history IDs. This view remains restricted
to signed-in administrators; the automation key does not grant admin access.

Enabled administrator request notifications label the requester **Automation
API** and can reach every opted-in admin. There is no machine recipient for
personal "requested music available" alerts; existing general new-music alerts
continue to follow user preferences. Repeated API POSTs and cross-origin
attachments create no duplicate Lidarr work, private history, release-group
audit, or request notifications. A later UI user attaches to an automation
acquisition, and automation can attach to a user's acquisition. Automation
associations keep pending work alive after user deletion and across restarts.
Already-READY POSTs perform no acquisition/requester/audit writes, as before.

Neither the key nor requester identities are included in recording/search
responses; only the existing lifecycle fields and compact target snapshots are
returned. Keys are not stored in requester/audit rows.

## Resolve AnimeThemes series

```http
POST /api/v1/animethemes/resolve
Content-Type: application/json
X-Api-Key: your-api-key
```

The endpoint accepts a MusicBrainz release-group MBID, one or more MusicBrainz
recording MBIDs, or both.

### Request body

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `releaseGroupId` | UUID string | Conditional | MusicBrainz release-group MBID. |
| `recordingIds` | UUID string array | Conditional | MusicBrainz recording MBIDs. Duplicate values are ignored. |

At least one of `releaseGroupId` or a non-empty `recordingIds` array is
required.

```json
{
  "releaseGroupId": "d65f7448-6d69-48b9-bc11-fb8a0b6f6e5f"
}
```

Both evidence types can be submitted together:

```json
{
  "releaseGroupId": "d65f7448-6d69-48b9-bc11-fb8a0b6f6e5f",
  "recordingIds": [
    "2b31e1c4-561b-305a-a94c-0c47f6378447"
  ]
}
```

### Curl example

```bash
curl -X POST "http://localhost:5056/api/v1/animethemes/resolve" \
  -H "X-Api-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"releaseGroupId":"d65f7448-6d69-48b9-bc11-fb8a0b6f6e5f"}'
```

### Successful response

```json
{
  "series": [
    {
      "animeThemesSeriesId": 157,
      "matchedBy": "releaseGroup",
      "name": "Sword Art Online",
      "slug": "sword_art_online"
    }
  ]
}
```

Each higher-level AnimeThemes series appears at most once. A release-group
match takes priority when the same series also matches a recording.

Unknown MBIDs are valid and return an empty collection:

```json
{
  "series": []
}
```

If AnimeThemes has not assigned an anime to a higher-level series, Melodarr
returns the anime itself as an explicit fallback:

```json
{
  "series": [
    {
      "animeThemesSeriesId": null,
      "animeThemesAnimeId": 1234,
      "matchedBy": "recording",
      "name": "Example Anime",
      "slug": "example_anime",
      "fallback": "anime"
    }
  ]
}
```

## Errors

Errors use a JSON object with an `error` string:

```json
{
  "error": "Sign in is required."
}
```

| Status | Meaning |
| --- | --- |
| `400 Bad Request` | The body is not a JSON object, no identifiers were supplied, or an identifier is malformed. |
| `401 Unauthorized` | The API key is missing or invalid and no authenticated browser session exists. |
| `403 Forbidden` | A browser-session request is missing a valid CSRF token. API-key requests do not require CSRF. |
| `502 Bad Gateway` | An upstream AnimeThemes request required during legacy-data hydration failed. |

## Browser-session access

The same endpoint remains available to Melodarr's browser UI through its
normal signed session. Browser requests must include the session's CSRF token.
External integrations should use `X-Api-Key` instead.

## Plex recording availability

The session API also exposes an exact recording lookup:

```http
GET /api/music/recording/{recordingMbid}/availability
```

This endpoint requires a signed-in Melodarr browser session. The automation
API key does not authorize this unversioned browser route. Use its
`/api/v1/music/recordings/{recordingMbid}/availability` counterpart for key
authentication. GET requests do not require a CSRF token.
Malformed UUIDs return `400`, unauthenticated requests return `401`, and a
temporarily unavailable local index returns `503`. Responses use `Cache-Control: no-store`.

```json
{
  "available": true,
  "recordingMbid": "22222222-2222-2222-2222-222222222222",
  "tracks": [
    {
      "ratingKey": "265485",
      "key": "/library/metadata/265485",
      "plexGuid": "plex://track/example",
      "title": "21 Questions",
      "trackArtist": "50 Cent feat. Nate Dogg",
      "albumArtist": "50 Cent",
      "albumTitle": "Best of 50 Cent",
      "durationMs": 224200,
      "trackNumber": 2,
      "discNumber": 1,
      "year": 2017,
      "librarySectionId": "1",
      "librarySectionTitle": "Music",
      "artistRatingKey": "265482",
      "albumRatingKey": "265483",
      "musicbrainzTrackId": "61e427a8-62f1-4000-b24b-35ecf1b6ce18",
      "musicbrainzRecordingId": "22222222-2222-2222-2222-222222222222",
      "musicbrainzReleaseId": "33333333-3333-3333-3333-333333333333",
      "musicbrainzReleaseGroupId": "44444444-4444-4444-4444-444444444444",
      "isrcs": ["GBUM71029604", "USIR10300005"],
      "mappingSource": "release_track",
      "mappingConfidence": "exact"
    }
  ]
}
```

Unknown or unresolved recordings, and installations without Plex configured,
return `available: false` and an empty `tracks` array. Every indexed Plex copy
is returned in deterministic rating-key order. Credentials are never included.
This lookup reads the local SQLite recording index without contacting Plex or
MusicBrainz or reading the full library snapshot. Availability reflects the
last scan and enrichment; it does not probe the server's current reachability.

### Track indexing and upgrade

Plex snapshot version 7 adds `tracks` and `serverId`. Tracks preserve the fields
above plus the original `guids` collection. Tracks without an exact MusicBrainz
match remain in the inventory with an empty recording ID and mapping fields.

The disposable metadata database's search schema version 8 adds
`track_search_plex_tracks`, `track_search_plex_isrcs`, and
`track_search_release_refs`. The migration preserves existing search rows and
backfills cached inventory and release references without network requests.
Older Plex snapshots receive tracks at the next scheduled full scan; a recent
scan encountering an old snapshot performs this initial full scan once.

Background enrichment loads each unique release with
`recordings+artist-credits+release-groups+isrcs`, reusing the existing MusicBrainz
metadata cache. Old documents lacking ISRC data are upgraded with one release
request. An exact match on `media[].tracks[].id` provides the recording ID,
release-group ID, and all recording ISRCs. MusicBrainz track GUIDs are never
interpreted as recording IDs.

When the parent release is absent or confirmed unusable, fallback enrichment
uses exact `tid:` recording searches in batches of up to 25 IDs. Ambiguous or
truncated results stay unresolved. Results must identify the matching track
for batched attribution. If a server omits these nested IDs, bounded single-ID
searches disambiguate the unattributed tracks. A single-ID search may resolve
one unique recording without nested track metadata. Missing recording ISRC data is fetched through
the normal metadata cache once per unique recording within a batch. A worker
pass handles at most 100 fallback IDs, deferring larger inventories to further
passes with a one-minute pause. All requests use existing background pacing.
Transient release failures leave tracks indexed and do not trigger fallback
requests against a failing service.

Owned release and track mappings run before optional artist-discography
warm-up. Warm-up skips the Various Artists compilation catalogue and reads
at most 10 pages (100 groups per page) for any other artist; remaining pages
load through the normal artist API when requested. Job status includes the
current artist and page. Repeated entries or incorrect offsets stop that
artist's warm-up instead of extending the pass.

Artist-page operations reserve MusicBrainz slots for at most 30 seconds, then
allow background workers to progress while retaining critical request
priority. Cache-miss coalescing is scoped by request priority to avoid waiting
on a paused lower-priority owner. All priorities still share the same cached
response documents.

The artist ID `89ad4ac3-39f7-470e-963a-56509c546377` (Various Artists)
has a library-only discography. Its artist, prefetch, completion, refresh,
and track-search endpoints use current local library snapshots and ignore
previously assembled global MusicBrainz discographies. Plex albums and
Lidarr albums with at least one imported track are included, deduplicated by
release-group ID; unmatched Plex albums remain visible with direct Plex links.
Artist responses set `libraryOnly: true`, `metadataSource: "Library"`, and
`provisional: false`, and use `Cache-Control: no-store`. Refresh rereads local
inventory. Revalidation returns `status: "library-only"` and `polling: false`.
This artist is excluded from full-discography and artist-wide track refreshes;
collection requests for its release groups, releases, or recordings are blocked.
Exact release/track enrichment for owned Plex albums continues normally.
All other artist IDs retain normal MusicBrainz discography behavior.

Recent scans merge tracks and read complete recent-album children, then upsert
only changed track rows and their ISRC relations. Full scans remove deleted
tracks. Recording and ISRC lookups retain multiple Plex copies; ISRCs are
secondary identifiers and are not unique. The internal
`track_search_index.plex_isrc_tracks()` helper supports indexed ISRC lookup.

## Track search recording state

```http
GET /api/search?type=track&q=Song%20Artist
```

This unversioned browser route requires a signed-in Melodarr session; use
`/api/v1/search` for automation API-key access. Existing query validation,
MusicBrainz/local search behavior, ranking,
ordering, and the 25-card limit are unchanged. Plain titles, title plus artist,
ISRCs, and version intent use the existing search interpretation.

Track search still returns **release-group cards**: `id` is the release-group
MBID, `name` is its display title, and `matchedTrack`/`matchedTrackArtist`
describe the matched recording. The additive `recordingMbid` field identifies
that exact MusicBrainz **recording**, never a MusicBrainz track MBID or the card's
release group. Several cards can refer to the same recording. For local cards
with multiple exact recording matches, the first valid recording MBID in
lexical order is used deterministically without changing card ranking.

Each card with a resolved recording MBID also includes `recordingState`:

```json
{
  "id": "22222222-2222-4222-8222-222222222222",
  "name": "Song Single",
  "artist": "Artist",
  "matchedTrack": "Song",
  "matchedTrackArtist": "Artist",
  "recordingMbid": "11111111-1111-4111-8111-111111111111",
  "recordingState": {
    "status": "downloading",
    "available": false,
    "plexCopyCount": 0,
    "downloadStatus": {"progress": 37, "status": "downloading"},
    "target": {
      "releaseGroupMbid": "22222222-2222-4222-8222-222222222222",
      "title": "Song Single",
      "artistName": "Artist",
      "primaryType": "Single"
    },
    "retrying": false
  }
}
```

`status` uses the same lifecycle definition as recording request GET, with
precedence `ready` → `waiting_for_plex` → `downloading` → `queued` → `requested`
→ `not_requested`. `ready` means the exact MusicBrainz recording MBID is present
in Melodarr's indexed Plex library, scoped to the configured server and selected
sections. It overrides stale download state. `available` is true only for
`ready`; `plexCopyCount` counts distinct playable indexed Plex copies. Full
tracks are omitted; fetch recording availability when concrete playback items
are needed.

`waiting_for_plex` means the persisted target release group is fully available
in cached Lidarr state while the exact recording is still absent from Plex.
`downloadStatus` is the same normalized, client-safe cached download projection
as recording request GET (including progress and available safe timing/status
fields), otherwise null. `retrying` is true only for a queued job with a stored
error; raw worker errors are never returned. `target` contains only the already
persisted acquisition snapshot, otherwise null. Historical empty titles/artists
and unknown/null primary types remain as stored.

With no acquisition or Plex copy, state is `not_requested`, `available: false`,
`plexCopyCount: 0`, `downloadStatus: null`, `target: null`, and `retrying: false`.
Cards lacking a resolved recording MBID (including release-group alias-only
fallbacks) remain usable and omit both `recordingMbid` and `recordingState`.

Search enrichment is based on local/cached state and does not initiate an
acquisition. It does not resolve unrequested targets, enrich target metadata
through MusicBrainz, contact Plex/Lidarr live, scan libraries, enqueue work, or
modify recording intent or request history. The original search's MusicBrainz
calls and disposable search-cache behavior are unchanged.

The reusable `recording_requests.recording_states(recording_mbids)` service
deduplicates/normalizes UUIDs and returns a mapping keyed by recording MBID,
without depending on Flask request globals. A 25-recording page uses one
indexed Plex grouped-count query and one indexed acquisition/pending-job join,
plus at most one read each of the cached Lidarr library and download snapshots.
The library snapshot uses existing memoization; pages with no missing active
intent skip both snapshots. Larger callers are chunked in groups of 500.
Recording request GET uses this primitive with `include_tracks=True` to retain
its full Plex copies and adds `plexCopyCount` to its existing response.

## Recording requests and lifecycle

```http
POST /api/music/recording/{recordingMbid}/request
GET  /api/music/recording/{recordingMbid}/request
```

POST means “make this exact MusicBrainz recording playable in Plex.” Both
endpoints require a signed-in Melodarr session; the automation API key does
not authorize them. POST requires the session's `X-CSRF-Token` header and no
request body. GET requires no CSRF token. UUIDs use the same normalization as
recording availability. Lifecycle responses and recording-handler errors use
`Cache-Control: no-store`; authentication and CSRF errors follow the existing
session conventions.

POST checks the indexed Plex recording lookup first. If the exact recording
is already present, it returns `200`, `status: "ready"`, `available: true`,
`alreadyAvailable: true`, and every indexed Plex copy. This path does not
resolve MusicBrainz metadata, touch Lidarr, create intent/requester/history
rows, send notifications, or enqueue work.

For a missing recording, POST reuses an existing acquisition target or asks
the read-only acquisition resolver for its preferred target using the existing
Single → EP → Album → Other → Broadcast → unknown ranking. A new request uses
the same internal release-group operation as `/api/request/release-group`:
Lidarr defaults, already-pending/already-existing handling, metadata refresh,
album search, request history, admin notifications, and worker wakeups are
preserved. Recording intent is persisted only after that operation accepts
the target. New accepted work returns `202`; an existing recording acquisition
returns `200` with `alreadyRequested: true`. If Plex observes the recording
before the new POST finishes, the response can already be `200`/`ready`.

Repeated POSTs preserve the original target and the same user's first request
timestamp. A second authenticated user becomes another requester without
rerunning the resolver or submitting another Lidarr request. The initial
release-group action creates its normal history/notification; attaching to
existing recording intent adds no duplicate history or notification. Targets
are never automatically changed when metadata or ranking rules change.

Example new request (GET returns the same lifecycle fields without the two
POST-only `alreadyAvailable` and `alreadyRequested` flags):

```json
{
  "recordingMbid": "11111111-1111-4111-8111-111111111111",
  "recordingTitle": "Song",
  "status": "queued",
  "available": false,
  "alreadyAvailable": false,
  "alreadyRequested": false,
  "target": {
    "releaseGroupMbid": "22222222-2222-4222-8222-222222222222",
    "title": "Song Single",
    "artistName": "Artist",
    "primaryType": "Single"
  },
  "requestedAt": 1790899200.0,
  "downloadStatus": null,
  "retrying": false,
  "tracks": []
}
```

`recordingTitle`, the target's title/artist/type, and `requestedAt` (Unix seconds
of the initial accepted recording request) are durable snapshots. Unknown
primary types remain null. With no intent, these metadata fields are null.
No requester identities, service credentials, filesystem paths, upstream
response bodies, or raw pending-worker errors are returned.

Lifecycle precedence, highest first:

| Status | Authoritative local condition |
| --- | --- |
| `ready` | The exact recording MBID exists in the selected Plex server/sections' recording index. Wins over all stale Lidarr data. |
| `waiting_for_plex` | The selected release group is fully available in the cached Lidarr library, but the exact recording is absent from Plex. |
| `downloading` | Cached, normalized download information exists for the selected release group. |
| `queued` | A durable metadata-refresh/search follow-up job exists for the selected group. |
| `requested` | Accepted recording intent exists, with none of the stronger conditions. |
| `not_requested` | No intent and no exact indexed Plex recording. |

READY means **the exact requested recording MBID exists in Plex**, never just
that Lidarr imported its release group. `waiting_for_plex` can persist if the
downloaded edition lacks that recording, Plex has not scanned it, or exact
identity enrichment is unresolved. No filename, title, ISRC, or fuzzy match
substitutes for the recording MBID.

When `downloading`, `downloadStatus` is the existing client-safe Lidarr subset:
`progress` (0–100), `status`, `trackedDownloadStatus`, `trackedDownloadState`,
`timeLeft`, and `estimatedCompletionTime`, when known. It is null for all other
states, including `ready` and `waiting_for_plex`. Download snapshots expire
after 30 seconds; stale/expired queue entries do not imply permanent progress.
`retrying: true` identifies a queued follow-up with a retryable worker error.
There is no terminal `failed` state: follow-ups retry with bounded backoff.
There is no `searching` state: the submitted search command ID exists only
until the worker removes the pending job on its next pass.

GET is a local read: one recording-index lookup, a primary-key intent lookup,
and, only for missing recordings with intent, cached Lidarr library/download
snapshots and an indexed pending-job lookup. It makes no live MusicBrainz,
Lidarr, or Plex calls, runs no resolver, starts no scans, rebuilds no indexes,
and writes no lifecycle/requester state. Library JSON is memoized rather than
scanned for each group. Cached state can lag a newly accepted Lidarr operation;
the next worker snapshot supplies the observed state.

### Persistence, concurrency, and completion

The normal startup migration adds `recording_acquisitions` (one immutable
selected target per recording) and `recording_acquisition_requesters` (one
relationship per recording/user, with its first request timestamp). Foreign
keys cascade requester deletion when users or intent are deleted. Indexes
cover recording identity, target release group, and requester user ID. No
lifecycle string is stored. Global intent survives deletion of its last
requester, so existing accepted work is still recognized.

Initiation uses OS file locks in `request-locks/` beside the durable database.
Recording locks coalesce concurrent duplicate POSTs; release-group locks also
coalesce separate recordings/direct album requests choosing the same target
while its job is pending. Locks span threads/processes, release on process
exit, and hold no SQLite writer transaction during upstream calls. Waiting
times out after 45 seconds with a safe `503`; callers may retry. Lock files
must not be removed while Melodarr is running. All web processes must share
the same database and lock directory on a filesystem supporting OS locks.

Synchronous rejection, missing target, configuration failure, or connection
failure leaves no accepted recording intent and allows a later POST retry.
An interruption or database failure after downstream acceptance but before
intent persistence cannot be made atomic with Lidarr: the next POST uses the
existing pending/already-existing release-group behavior to recover. Once
intent is committed, polling never reruns the resolver or retargets it.

Normal Lidarr library scans refresh completion approximately every 2 minutes;
queue snapshots refresh every 7 seconds. The existing Plex worker runs recent
scans every 3 minutes and full scans every 12 hours, plus its existing startup
and manual scans. These scans update the track index and schedule exact
MusicBrainz track-to-recording enrichment where needed. After Plex itself
exposes the imported track and enrichment indexes its exact recording MBID,
the next GET changes `waiting_for_plex` to `ready` and returns every indexed
copy in the same deterministic order and shape as recording availability.
Neither recording endpoint introduces a Plex polling loop or wakes scans.

Errors use the normal `{"error": "..."}` shape: `400` invalid UUID, `401`
unsigned-in caller, `403` missing/invalid POST CSRF token, `404` no valid
acquisition target or target absent from Lidarr, `502` unavailable MusicBrainz
or rejected/unreachable Lidarr, and `503` invalid/missing Lidarr configuration,
local storage trouble, or initiation lock contention. A missing unrequested
recording on GET is a normal `200`/`not_requested` response.

## Recording acquisition targets

```http
GET /api/music/recording/{recordingMbid}/acquisition
```

This unversioned browser endpoint requires a signed-in session; its
`/api/v1/music/recordings/{recordingMbid}/acquisition` counterpart accepts the
automation API key. It does not add anything to Lidarr, start searches/downloads, create
request history, or scan Plex. Responses use `Cache-Control: no-store`.

The result contains `recordingMbid`, `recordingTitle`, `recordingArtistMbids`,
`state`, `available`, `needsAcquisition`, `enumeratedReleaseCount`,
`candidateCount`, `target`, and `alternatives`. `available` is obtained from
the local Plex recording index and `needsAcquisition` is its inverse. A
recording already in Plex still receives an acquisition target for diagnostics.
`target` is the first ranked release group; alternatives contain every other
verified group in deterministic order. Empty results have a null target.

Candidate fields:

| Field | Meaning |
| --- | --- |
| `releaseGroupMbid`, `title` | Exact release-group identity and MusicBrainz title. |
| `artistName`, `artistMbids`, `artistCreditSource` | Credit from the group, or a containing release when group credit is unavailable. |
| `primaryType`, `secondaryTypes` | Actual MusicBrainz types; an unknown primary type remains null. |
| `firstReleaseDate`, `firstReleaseDateSource` | Group date, falling back to the earliest verified containing-release date. Partial dates are retained. |
| `minimumTrackCount` | Smallest known total across all media of a containing release; null when unknown. This is a size hint, not a guaranteed download size. |
| `minimumOfficialTrackCount` | The same hint restricted to Official containing releases. |
| `officialReleaseCount`, `containingReleaseCount`, `releaseStatusCounts` | Counts of unique editions proven to contain the recording. |
| `containingReleases` | Release MBIDs, status, date, track count, and matching MusicBrainz track MBIDs. |
| `containsExactRecording` | Always true for eligible candidates. |
| `artistRelevance` | `same-credit`, `overlapping-credit`, `different-credit`, or `unknown`, based on artist MBIDs, not display names. |
| `ranking`, `rankTuple`, `selectionReasons` | Named lexicographic components, the ordered tuple, and human-readable evidence. Lower tuples win. |
| `availableInLidarr`, `fullyAvailableInLidarr` | Current cached Lidarr tracking/completion, added after ranking. Tracking does not necessarily mean all files are present. |
| `lidarrTrackFileCount`, `lidarrTotalTrackCount` | File counts from the cached Lidarr snapshot. |

### Exact containment and pagination

The resolver looks up the exact recording with `inc=artist-credits`, then
browses `/release?recording={recordingMbid}` with
`inc=recordings+release-groups+artist-credits+media`, `limit=100`, and `offset`.
It advances by the actual number of releases returned until the reported
total is reached. It rejects changing totals, incorrect offsets, repeated
release IDs, empty intermediate pages, and malformed responses. No partial
candidate set is promoted into a resolved result.

Each containing release must have `media[].tracks[].recording.id` equal to
the requested recording MBID. Missing tracklists receive an exact release
lookup through the existing MusicBrainz client. Explicitly different
recordings are excluded even if their title/artist/ISRC matches. Missing group
metadata receives at most one lookup per unique group. Recording lookup
linked-release lists and the compact search index are not used to enumerate
the complete candidate set.

### Ranking

The tuple, in order, is:

```text
(
  nonOfficialOnlyPenalty,
  primaryTypeRank,
  broadPackagingPenalty,
  minimumTrackCount,
  artistRelevanceRank,
  releaseStatusRank,
  secondaryTypePenalty,
  firstReleaseDate,
  releaseGroupMbid
)
```

Known Bootleg/Pseudo-Release/Withdrawn/Cancelled-only groups receive the first
penalty; an unknown status does not imply an unusable release. Otherwise the
primary ordering is Single → EP → Album → Other → Broadcast → unknown.
Within the same primary type, straightforward packaging beats
Compilation/Various Artists packaging, then smaller known containing
releases win (unknown size sorts last). Artist credits prefer equal MBID
sets, then overlap, then unrelated credits, with missing evidence last.
Official status wins the later status tie-break, followed by Promotion,
unknown, Bootleg, Pseudo-Release, Withdrawn, and Cancelled. Fewer secondary
types, earlier dates, and finally the group MBID break remaining ties.
Missing sizes/dates have explicit sort-last sentinels in `ranking`.

Compilations, Live, Remix, Soundtrack, DJ-mix, Mixtape/Street, and Demo groups
are retained when they contain the exact recording. A normal Single beats
an EP/Album regardless of a later date or a larger track count; the exception
for known non-official-only groups lets a clean Official Album beat a
bootleg-only Single. Existing AnimeThemes/discovery preferences are unchanged.

### Caching and failure states

Complete normalized MusicBrainz results use a **seven-day TTL** in
`musicbrainz-metadata:recording-acquisition-v1` within the existing cache
database. Cache identity includes the recording MBID and configured
MusicBrainz base URL. Clearing the MusicBrainz metadata cache also clears
this nested scope; changing its schema version invalidates older results.
An expired normalized document triggers a provider refresh. If that document
was already removed by cache cleanup, the result is rebuilt through the
existing HTTP cache. Fresh raw provider documents can satisfy a cold miss.
Concurrent builders for the same recording/provider are coalesced.

Provider failures and incomplete page sets are not cached as completed
results. Plex availability and cached Lidarr status are read on every
request and are never stored in the normalized result or used to change
recording identity/ranking. Lidarr is read once for all candidates.

| State/status | Meaning |
| --- | --- |
| `resolved` / `200` | A verified target and zero or more alternatives. |
| `no_releases` / `200` | MusicBrainz returned a complete, empty release collection. |
| `no_release_groups` / `200` | Releases exist but none produces an exact, usable group candidate. |
| `musicbrainz_unavailable` / `502` | Provider/network/metadata completeness failure; null target and a fixed public error. |
| `400` | Invalid recording UUID. |
| `401` | Session authentication required. |
| `503` | Local index/cache could not be read; fixed public error. |

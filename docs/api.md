# Melodarr API

Melodarr currently exposes one endpoint for trusted external automation. The
API key is scoped to this endpoint and does not grant access to the rest of the
Melodarr API or administrator interface.

A machine-readable version of this contract is available in
[`openapi.yaml`](openapi.yaml).

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
API key does not authorize it. GET requests do not require a CSRF token.
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

## Recording acquisition targets

```http
GET /api/music/recording/{recordingMbid}/acquisition
```

This read-only endpoint requires a signed-in session, not an automation API
key. It does not add anything to Lidarr, start searches/downloads, create
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

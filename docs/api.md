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

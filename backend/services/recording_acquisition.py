"""Resolve one exact recording to a small, explainable acquisition group.

Only MusicBrainz metadata is cached. Plex and Lidarr state is read separately
on every API request; this service never requests media or writes music indexes.
"""

import json
import re
from collections import Counter
from datetime import date
from uuid import UUID

import requests

if __package__ == "backend.services":
    from ..api_cache import cache_document_lock, get_cache_document, set_cache_document
    from . import musicbrainz
else:
    from api_cache import cache_document_lock, get_cache_document, set_cache_document
    from services import musicbrainz


CACHE_SCHEMA_VERSION = 1
# A nested scope makes the existing MusicBrainz cache clear invalidate this too.
CACHE_NAMESPACE = f"musicbrainz-metadata:recording-acquisition-v{CACHE_SCHEMA_VERSION}"
CACHE_TTL = 7 * 24 * 60 * 60
RELEASE_LOOKUP_INCLUDES = "recordings+release-groups+artist-credits"
_PRIMARY_TYPES = ("Single", "EP", "Album", "Other", "Broadcast")
_PRIMARY_RANK = {value.casefold(): rank for rank, value in enumerate(_PRIMARY_TYPES)}
_BAD_STATUSES = {"bootleg", "pseudo-release", "withdrawn", "cancelled"}
_STATUS_RANK = {
    "official": 0, "promotion": 1, "": 2, "bootleg": 3,
    "pseudo-release": 4, "withdrawn": 5, "cancelled": 6,
}
_RANK_FIELDS = (
    "nonOfficialOnlyPenalty", "primaryTypeRank", "broadPackagingPenalty",
    "minimumTrackCount", "artistRelevanceRank", "releaseStatusRank",
    "secondaryTypePenalty", "firstReleaseDate", "releaseGroupMbid",
)


def _mbid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ""


def _date(value):
    value = str(value or "")
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return ""
    parts = [int(part) for part in value.split("-")]
    try:
        date(*(parts + [1] * (3 - len(parts))))
    except ValueError:
        return ""
    return value


def _credits(entity):
    ids, names = set(), []
    for credit in entity.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        artist_id = _mbid(artist.get("id"))
        if artist_id:
            ids.add(artist_id)
        names.append(str(credit.get("name") or artist.get("name") or "") + str(credit.get("joinphrase") or ""))
    return sorted(ids), "".join(names).strip()


def _matching_tracks(release, recording_id):
    return [
        track for medium in release.get("media") or []
        if isinstance(medium, dict)
        for track in medium.get("tracks") or []
        if isinstance(track, dict)
        and _mbid((track.get("recording") or {}).get("id")) == recording_id
    ]


def _has_track_identities(release):
    media = release.get("media")
    return bool(
        isinstance(media, list) and media
        and all(
            isinstance(medium, dict) and isinstance(medium.get("tracks"), list)
            and all(isinstance(track, dict) and _mbid((track.get("recording") or {}).get("id"))
                    for track in medium["tracks"])
            for medium in media
        )
    )


def _track_count(release):
    media = release.get("media")
    if not isinstance(media, list) or not media:
        return None
    counts = [medium.get("track-count") if isinstance(medium, dict) else None for medium in media]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        return None
    return sum(counts) or None


def candidate_rank(candidate):
    """Return the documented lexicographic acquisition ordering."""
    rank = candidate["ranking"]
    return tuple(rank[field] for field in _RANK_FIELDS)


def rank_candidates(recording_id, recording, releases):
    """Pure aggregation/ranking; titles never establish recording identity."""
    recording_id = str(UUID(str(recording_id)))
    recording_artists = set(_credits(recording)[0]) - {musicbrainz.VARIOUS_ARTISTS_ID}
    grouped = {}
    # Stable iteration also stabilizes representative metadata and evidence rows.
    for release in sorted(releases, key=lambda item: (
        str(item.get("id") or ""), json.dumps(item, sort_keys=True),
    )):
        group = release.get("release-group") or {}
        group_id = _mbid(group.get("id"))
        release_id = _mbid(release.get("id"))
        matches = _matching_tracks(release, recording_id)
        if not group_id or not release_id or not matches:
            continue
        grouped.setdefault(group_id, {})[release_id] = (release, matches)

    candidates = []
    for group_id, editions in grouped.items():
        releases = [item[0] for item in editions.values()]
        # Prefer the most complete group metadata without relying on API order.
        group = min((release["release-group"] for release in releases), key=lambda item: (
            -sum(field in item for field in ("primary-type", "artist-credit", "first-release-date")),
            json.dumps(item, sort_keys=True),
        ))
        artist_ids, artist_name = _credits(group)
        credit_source = "release-group"
        if not artist_ids and not artist_name:
            artist_ids, artist_name = _credits(releases[0])
            credit_source = "containing-release" if artist_ids or artist_name else "unknown"
        group_artists = set(artist_ids) - {musicbrainz.VARIOUS_ARTISTS_ID}
        if not recording_artists or not group_artists:
            relevance, artist_rank = "unknown", 3
        elif group_artists == recording_artists:
            relevance, artist_rank = "same-credit", 0
        elif group_artists.intersection(recording_artists):
            relevance, artist_rank = "overlapping-credit", 1
        else:
            relevance, artist_rank = "different-credit", 2
        secondary = sorted({str(value).strip() for value in group.get("secondary-types") or [] if value}, key=str.casefold)
        broad = int(
            musicbrainz.VARIOUS_ARTISTS_ID in artist_ids
            or any(value.casefold() == "compilation" for value in secondary)
        )
        raw_primary = group.get("primary-type")
        primary = next((value for value in _PRIMARY_TYPES if value.casefold() == str(raw_primary).casefold()), raw_primary or None)
        status_counts = Counter(str(release.get("status") or "Unknown") for release in releases)
        statuses = [str(release.get("status") or "").casefold() for release in releases]
        official_count = sum(status == "official" for status in statuses)
        non_official = int(all(status in _BAD_STATUSES for status in statuses))
        counts = [count for release in releases if (count := _track_count(release)) is not None]
        official_counts = [
            count for release in releases
            if str(release.get("status") or "").casefold() == "official"
            and (count := _track_count(release)) is not None
        ]
        group_date = _date(group.get("first-release-date"))
        dates = [value for release in releases if (value := _date(release.get("date")))]
        first_date = group_date or (min(dates) if dates else "")
        minimum_count = min(counts) if counts else None
        rank = {
            "nonOfficialOnlyPenalty": non_official,
            "primaryTypeRank": _PRIMARY_RANK.get(str(primary).casefold(), len(_PRIMARY_RANK)),
            "broadPackagingPenalty": broad,
            "minimumTrackCount": minimum_count if minimum_count is not None else 2**63 - 1,
            "artistRelevanceRank": artist_rank,
            "releaseStatusRank": min(_STATUS_RANK.get(status, 2) for status in statuses),
            "secondaryTypePenalty": len(secondary),
            "firstReleaseDate": first_date or "9999-12-31",
            "releaseGroupMbid": group_id,
        }
        reasons = ["contains exact recording", f"preferred primary type: {primary or 'unknown'}"]
        reasons.append("official release available" if official_count else "no confirmed official containing release")
        reasons.append(
            f"smallest known containing release: {minimum_count} tracks"
            if minimum_count is not None else "containing-release track count unknown"
        )
        reasons.append(f"artist credit: {relevance}")
        if broad:
            reasons.append("broad compilation or Various Artists packaging ranked lower within primary type")
        if secondary:
            reasons.append("secondary types retained: " + ", ".join(secondary))
        if non_official:
            reasons.append("known non-official-only releases ranked below ordinary acquisition targets")
        candidate = {
            "releaseGroupMbid": group_id, "title": str(group.get("title") or releases[0].get("title") or ""),
            "artistName": artist_name, "artistMbids": artist_ids, "artistCreditSource": credit_source,
            "artistRelevance": relevance, "primaryType": primary, "secondaryTypes": secondary,
            "firstReleaseDate": first_date or None,
            "firstReleaseDateSource": "release-group" if group_date else "containing-release" if dates else "unknown",
            "minimumTrackCount": minimum_count,
            "minimumOfficialTrackCount": min(official_counts) if official_counts else None,
            "officialReleaseCount": official_count, "containingReleaseCount": len(editions),
            "releaseStatusCounts": dict(sorted(status_counts.items())),
            "containsExactRecording": True, "ranking": rank, "selectionReasons": reasons,
            "containingReleases": [{
                "releaseMbid": release_id,
                "status": str(release.get("status") or "Unknown"),
                "date": _date(release.get("date")) or None,
                "trackCount": _track_count(release),
                "matchingTrackMbids": sorted({
                    value for track in matches if (value := _mbid(track.get("id")))
                }),
            } for release_id, (release, matches) in sorted(editions.items())],
        }
        candidate["rankTuple"] = list(candidate_rank(candidate))
        candidates.append(candidate)
    return sorted(candidates, key=candidate_rank)


def _verified_releases(recording_id, releases, *, force_refresh):
    verified, group_metadata = [], {}
    for release in releases:
        if not _matching_tracks(release, recording_id) and not _has_track_identities(release):
            release_id = _mbid(release.get("id"))
            release = musicbrainz.get(
                f"/release/{release_id}", RELEASE_LOOKUP_INCLUDES,
                cache_ttl=CACHE_TTL, force_refresh=force_refresh,
            )
            if not isinstance(release, dict) or _mbid(release.get("id")) != release_id or not _has_track_identities(release):
                raise requests.RequestException("MusicBrainz returned an invalid release tracklist.")
        if not _matching_tracks(release, recording_id):
            continue
        group = release.get("release-group") or {}
        group_id = _mbid(group.get("id"))
        if group_id and any(field not in group for field in ("primary-type", "artist-credit", "first-release-date")):
            if group_id not in group_metadata:
                detail = musicbrainz.get(
                    f"/release-group/{group_id}", "artist-credits",
                    cache_ttl=CACHE_TTL, force_refresh=force_refresh,
                )
                if not isinstance(detail, dict) or _mbid(detail.get("id")) != group_id:
                    raise requests.RequestException("MusicBrainz returned an invalid release group.")
                group_metadata[group_id] = detail
            release = {**release, "release-group": group_metadata[group_id]}
        verified.append(release)
    return verified


def _result(recording_id, state, *, recording=None, releases=(), candidates=()):
    return {
        "schemaVersion": CACHE_SCHEMA_VERSION, "recordingMbid": recording_id, "state": state,
        "recordingTitle": str((recording or {}).get("title") or ""),
        "recordingArtistMbids": _credits(recording or {})[0],
        "enumeratedReleaseCount": len(releases),
        "candidateCount": len(candidates),
        "target": candidates[0] if candidates else None,
        "alternatives": list(candidates[1:]),
    }


def resolve(recording_id):
    """Resolve or reuse complete metadata; provider failures never yield a target."""
    recording_id = str(UUID(str(recording_id)))
    try:
        base_url = musicbrainz.configuration()["baseUrl"]
    except musicbrainz.ConfigurationError:
        return _result(recording_id, "musicbrainz_unavailable")
    document_id = json.dumps({
        "recordingMbid": recording_id, "baseUrl": base_url,
    }, sort_keys=True, separators=(",", ":"))
    with cache_document_lock(CACHE_NAMESPACE, document_id):
        cached = get_cache_document(CACHE_NAMESPACE, document_id)
        if (
            isinstance(cached, dict) and cached.get("schemaVersion") == CACHE_SCHEMA_VERSION
            and cached.get("recordingMbid") == recording_id
            and cached.get("state") in {"resolved", "no_releases", "no_release_groups"}
            and "target" in cached and isinstance(cached.get("alternatives"), list)
        ):
            return cached
        expired = get_cache_document(CACHE_NAMESPACE, document_id, allow_expired=True)
        force_refresh = expired is not None
        try:
            recording = musicbrainz.get(
                f"/recording/{recording_id}", "artist-credits",
                cache_ttl=CACHE_TTL, force_refresh=force_refresh,
            )
            if not isinstance(recording, dict) or _mbid(recording.get("id")) != recording_id:
                raise requests.RequestException("MusicBrainz returned an invalid recording.")
            releases = musicbrainz.browse_releases_by_recording(
                recording_id, cache_ttl=CACHE_TTL, force_refresh=force_refresh,
            )
            verified = _verified_releases(recording_id, releases, force_refresh=force_refresh)
            candidates = rank_candidates(recording_id, recording, verified)
            state = "resolved" if candidates else "no_releases" if not releases else "no_release_groups"
            result = _result(recording_id, state, recording=recording, releases=releases, candidates=candidates)
        except (requests.RequestException, TypeError, ValueError, AttributeError):
            return _result(recording_id, "musicbrainz_unavailable")
        set_cache_document(CACHE_NAMESPACE, document_id, result, CACHE_TTL)
        return result

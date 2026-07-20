"""Recommendation assembly independent of Flask routes and worker startup."""

import logging
import math
from datetime import datetime, timezone

import requests

if __package__:
    from .media_urls import artist_cover_art, release_group_cover_art
    from .services import lastfm, lidarr, listenbrainz, musicbrainz, plex
    from .storage import (
        get_request_history,
        get_service,
        recommendation_users,
        save_recommendation_cache,
    )
else:  # Support the existing `python backend/app.py` entry point.
    from media_urls import artist_cover_art, release_group_cover_art
    from services import lastfm, lidarr, listenbrainz, musicbrainz, plex
    from storage import (
        get_request_history,
        get_service,
        recommendation_users,
        save_recommendation_cache,
    )


logger = logging.getLogger(__name__)


def _musicbrainz_lookup(path, inc=""):
    """Make a cached lookup, treating a confirmed missing entity as empty."""
    try:
        data = musicbrainz.get(path, inc, priority="background")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    return data


def _search_release_group(title, artist):
    """Find an exact release-group match when Last.fm supplies a stale MBID."""
    query = f'releasegroup:"{title.replace(chr(34), "").strip()}"'
    if artist:
        query += f' AND artist:"{artist.replace(chr(34), "").strip()}"'
    results = musicbrainz.search(query, "album", priority="background")

    normalized_title = title.casefold().strip()
    normalized_artist = artist.casefold().strip()
    groups = results.get("release-groups", [])
    for group in groups:
        group_artists = [
            credit.get("name", "").casefold().strip()
            for credit in group.get("artist-credit", [])
        ]
        if (
            group.get("title", "").casefold().strip() == normalized_title
            and (not normalized_artist or normalized_artist in group_artists)
        ):
            return group.get("id", "")
    return groups[0].get("id", "") if groups and groups[0].get("score", 0) >= 95 else ""


def resolve_lastfm_album_mbid(mbid, title, artist):
    """Normalize Last.fm's release, release-group, or stale MBID to a group."""
    if mbid:
        release = _musicbrainz_lookup(f"/release/{mbid}", "release-groups")
        if release:
            release_group = release.get("release-group") or {}
            if release_group.get("id"):
                return release_group["id"]

        release_group = _musicbrainz_lookup(f"/release-group/{mbid}")
        if release_group and release_group.get("id"):
            return release_group["id"]

    return _search_release_group(title, artist) if title and artist else ""


def listenbrainz_recommendations(
    username,
    *,
    excluded_artist_ids=None,
    excluded_artist_names=None,
    excluded_album_ids=None,
    excluded_album_names=None,
):
    """Turn ListenBrainz recording recommendations into browseable MB entities."""
    excluded_artist_ids = set(excluded_artist_ids or ())
    excluded_artist_names = {
        str(name).casefold() for name in (excluded_artist_names or ()) if name
    }
    excluded_album_ids = set(excluded_album_ids or ())
    excluded_album_names = {
        (str(artist).casefold(), str(title).casefold())
        for artist, title in (excluded_album_names or ())
        if artist and title
    }
    recommendations = listenbrainz.recording_recommendations(username)
    scores = {
        item.get("recording_mbid"): item.get("score", 0)
        for item in recommendations
        if item.get("recording_mbid")
    }
    if not scores:
        return [], []

    metadata = listenbrainz.recording_metadata(scores)
    artists, albums = {}, {}
    for recording_mbid, score in scores.items():
        item = metadata.get(recording_mbid, {})
        for artist in item.get("artist", {}).get("artists", []):
            mbid = artist.get("artist_mbid") or artist.get("mbid")
            name = str(artist.get("name") or "").strip()
            if (
                mbid
                and name
                and mbid not in excluded_artist_ids
                and name.casefold() not in excluded_artist_names
                and (mbid not in artists or score > artists[mbid]["score"])
            ):
                artists[mbid] = {
                    "id": mbid,
                    "name": name,
                    "type": artist.get("type", ""),
                    "score": score,
                    "coverArt": artist_cover_art(mbid),
                }
        release = item.get("release", {})
        release_group_mbid = release.get("release_group_mbid")
        album_artist_name = str(release.get("album_artist_name") or "").strip()
        album_name = str(release.get("name") or "").strip()
        if (
            release_group_mbid
            and release_group_mbid not in excluded_album_ids
            and album_artist_name.casefold() not in excluded_artist_names
            and (album_artist_name.casefold(), album_name.casefold())
            not in excluded_album_names
            and album_name
            and (release_group_mbid not in albums or score > albums[release_group_mbid]["score"])
        ):
            albums[release_group_mbid] = {
                "id": release_group_mbid,
                "name": album_name,
                "artist": album_artist_name,
                "date": str(release.get("year", "")),
                "type": release.get("type", "Album"),
                "score": score,
                "coverArt": release_group_cover_art(release_group_mbid),
            }
    return (
        sorted(artists.values(), key=lambda item: item["score"], reverse=True)[:20],
        sorted(albums.values(), key=lambda item: item["score"], reverse=True)[:20],
    )


LASTFM_TASTE_PERIODS = (("1month", 1.0), ("6month", 0.65), ("overall", 0.35))
LASTFM_ALBUM_ARTIST_LIMIT = 10
LASTFM_ALBUMS_PER_ARTIST = 4
LASTFM_PRIMARY_ALBUM_LIMIT = 12
LASTFM_TAG_ROW_LIMIT = 10
LASTFM_TAG_FALLBACK_SCAN_LIMIT = 24


def _integer(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _lastfm_taste_artists(username, api_key, limit=12):
    """Blend recent and long-term listening into weighted seed artists."""
    seeds = {}
    last_error = None
    successful_periods = 0
    for period, period_weight in LASTFM_TASTE_PERIODS:
        try:
            artists = lastfm.get(
                "user.gettopartists",
                username,
                api_key,
                period=period,
                limit=limit,
            ).get("topartists", {}).get("artist", [])
            successful_periods += 1
        except (ValueError, requests.RequestException) as exc:
            last_error = exc
            continue
        maximum_plays = max(
            (_integer(item.get("playcount")) for item in artists),
            default=0,
        )
        for rank, artist in enumerate(artists):
            name = str(artist.get("name", "")).strip()
            mbid = str(artist.get("mbid", "")).strip()
            if not name and not mbid:
                continue
            key = mbid or name.casefold()
            rank_score = 1 / math.sqrt(rank + 1)
            play_score = (
                _integer(artist.get("playcount")) / maximum_plays
                if maximum_plays
                else 0
            )
            entry = seeds.setdefault(key, {
                "id": mbid,
                "name": name,
                "score": 0.0,
            })
            entry["score"] += period_weight * (rank_score + 0.25 * play_score)
    if not successful_periods and last_error:
        raise last_error
    return sorted(seeds.values(), key=lambda item: item["score"], reverse=True)[:limit]


def _lastfm_listened_albums(username, api_key):
    """Return album identities that should be treated as familiar, not discoveries."""
    identities = set()
    for period in ("1month", "6month", "overall"):
        try:
            albums = lastfm.get(
                "user.gettopalbums",
                username,
                api_key,
                period=period,
                limit=50,
            ).get("topalbums", {}).get("album", [])
        except (ValueError, requests.RequestException):
            continue
        for album in albums:
            title = str(album.get("name", "")).strip().casefold()
            artist = str((album.get("artist") or {}).get("name", "")).strip().casefold()
            if title and artist:
                identities.add((artist, title))
    return identities


def _release_recency_score(first_release_date):
    """Softly favor newer releases without excluding older records."""
    try:
        year = int(str(first_release_date)[:4])
    except (TypeError, ValueError):
        return 1.0
    age = max(0, datetime.now(timezone.utc).year - year)
    return max(0.72, 1.24 - age * 0.02)


def _artist_tags(artist, username, api_key):
    identifier = {"mbid": artist["id"]} if artist.get("id") else {
        "artist": artist.get("name", "")
    }
    if not next(iter(identifier.values())):
        return []
    try:
        tags = lastfm.get(
            "artist.gettoptags", username, api_key, **identifier
        ).get("toptags", {}).get("tag", [])
    except (ValueError, requests.RequestException):
        return []
    return [
        str(tag.get("name", "")).strip()
        for tag in tags[:8]
        if str(tag.get("name", "")).strip()
    ]


def lastfm_recommendations(
    username,
    api_key,
    *,
    excluded_artist_ids=None,
    excluded_artist_names=None,
    excluded_album_ids=None,
    excluded_album_names=None,
):
    """Build weighted, novelty-aware suggestions from a user's listening history."""
    excluded_artist_ids = set(excluded_artist_ids or ())
    excluded_artist_names = {
        str(name).casefold() for name in (excluded_artist_names or ()) if name
    }
    excluded_album_ids = set(excluded_album_ids or ())
    excluded_album_names = {
        (str(artist).casefold(), str(title).casefold())
        for artist, title in (excluded_album_names or ())
        if artist and title
    }
    seeds = _lastfm_taste_artists(username, api_key)
    try:
        profile_tags = lastfm_top_tags(username, api_key, limit=10)
    except (ValueError, requests.RequestException):
        profile_tags = []
    taste_tag_weights = {
        str(tag.get("name", "")).strip().casefold(): 1 / math.sqrt(rank + 1)
        for rank, tag in enumerate(profile_tags)
        if str(tag.get("name", "")).strip()
    }
    seed_ids = {seed["id"] for seed in seeds if seed.get("id")}
    seed_names = {seed["name"].casefold() for seed in seeds if seed.get("name")}

    candidates = {}
    similarity_error = None
    successful_similarity_requests = 0
    for seed in seeds:
        identifier = {"mbid": seed["id"]} if seed.get("id") else {
            "artist": seed.get("name", "")
        }
        try:
            similar = lastfm.get(
                "artist.getsimilar", username, api_key, limit=12, **identifier
            ).get("similarartists", {}).get("artist", [])
            successful_similarity_requests += 1
        except (ValueError, requests.RequestException) as exc:
            similarity_error = exc
            continue
        for artist in similar:
            mbid = str(artist.get("mbid", "")).strip()
            name = str(artist.get("name", "")).strip()
            normalized_name = name.casefold()
            if (
                not mbid
                or not name
                or mbid in seed_ids
                or normalized_name in seed_names
                or mbid in excluded_artist_ids
                or normalized_name in excluded_artist_names
            ):
                continue
            try:
                match = float(artist.get("match") or 1.0)
            except (TypeError, ValueError):
                match = 0.5
            entry = candidates.setdefault(mbid, {
                "id": mbid,
                "name": name,
                "score": 0.0,
                "seedNames": set(),
            })
            entry["score"] += seed["score"] * max(0.05, match)
            if seed.get("name"):
                entry["seedNames"].add(seed["name"])
    if seeds and not successful_similarity_requests and similarity_error:
        raise similarity_error

    ranked_candidates = sorted(
        candidates.values(), key=lambda item: item["score"], reverse=True
    )
    artists = []
    for candidate in ranked_candidates[:20]:
        seed_names_text = ", ".join(sorted(candidate["seedNames"])[:2])
        artists.append({
            "id": candidate["id"],
            "name": candidate["name"],
            "type": (
                f"Similar to {seed_names_text}"
                if seed_names_text
                else "Matched to your recent listening"
            ),
            "score": round(candidate["score"], 4),
            "coverArt": artist_cover_art(candidate["id"]),
        })

    listened_albums = _lastfm_listened_albums(username, api_key)
    albums = {}
    for artist_rank, artist in enumerate(artists[:LASTFM_ALBUM_ARTIST_LIMIT]):
        taste_tags = _artist_tags(artist, username, api_key)
        try:
            top_albums = lastfm.get(
                "artist.gettopalbums",
                username,
                api_key,
                mbid=artist["id"],
                limit=LASTFM_ALBUMS_PER_ARTIST,
            ).get("topalbums", {}).get("album", [])
        except (ValueError, requests.RequestException) as exc:
            logger.warning(
                "Skipping Last.fm albums for %s: %s", artist["name"], exc
            )
            continue
        for album_rank, album in enumerate(top_albums):
            name = str(album.get("name", "")).strip()
            artist_name = str(
                (album.get("artist") or {}).get("name", artist["name"])
            ).strip()
            album_key = (artist_name.casefold(), name.casefold())
            if (
                not name
                or album_key in listened_albums
                or album_key in excluded_album_names
            ):
                continue
            try:
                mbid = resolve_lastfm_album_mbid(
                    album.get("mbid", ""), name, artist_name
                )
                if not mbid or mbid in excluded_album_ids:
                    continue
                metadata = _musicbrainz_lookup(f"/release-group/{mbid}") or {}
            except requests.RequestException as exc:
                logger.warning(
                    "Skipping Last.fm album %s by %s after MusicBrainz lookup failed: %s",
                    name,
                    artist_name or "Unknown artist",
                    exc,
                )
                continue
            date = metadata.get("first-release-date", "")
            tag_affinity = sum(
                taste_tag_weights.get(tag.casefold(), 0) for tag in taste_tags
            )
            score = (
                artist["score"]
                * (1 / math.sqrt(album_rank + 1))
                * _release_recency_score(date)
                * (1 + min(0.3, tag_affinity * 0.08))
                * (1 - min(artist_rank, LASTFM_ALBUM_ARTIST_LIMIT - 1) * 0.025)
            )
            item = {
                "id": mbid,
                "name": metadata.get("title") or name,
                "artist": artist_name,
                "type": metadata.get("primary-type") or "Recommended album",
                "date": date,
                "score": round(score, 4),
                "tasteTags": taste_tags,
                "coverArt": release_group_cover_art(mbid),
            }
            if mbid not in albums or score > albums[mbid]["score"]:
                albums[mbid] = item
    return artists, sorted(
        albums.values(), key=lambda item: item["score"], reverse=True
    )[: LASTFM_ALBUM_ARTIST_LIMIT * LASTFM_ALBUMS_PER_ARTIST]


def lastfm_album_mbid(album, username, api_key):
    """Use Last.fm's canonical album lookup when a tag result lacks an MBID."""
    artist = (album.get("artist") or {}).get("name", "")
    title = album.get("name", "")
    if not artist or not title:
        return ""
    try:
        mbid = album.get("mbid", "")
        if not mbid:
            mbid = lastfm.get(
                "album.getinfo", username, api_key, artist=artist, album=title
            ).get("album", {}).get("mbid", "")
        return resolve_lastfm_album_mbid(mbid, title, artist)
    except (ValueError, requests.RequestException):
        return ""


def lastfm_tag_recommendations(
    tag_name,
    username,
    api_key,
    *,
    excluded_album_ids=None,
    excluded_album_names=None,
    excluded_artist_names=None,
    existing_album_ids=None,
    limit=LASTFM_TAG_ROW_LIMIT,
):
    """Backfill a sparse taste row and re-rank it for recency and novelty."""
    excluded_album_ids = set(excluded_album_ids or ())
    excluded_album_names = {
        (str(artist).casefold(), str(title).casefold())
        for artist, title in (excluded_album_names or ())
        if artist and title
    }
    existing_album_ids = set(existing_album_ids or ())
    seen_album_ids = set(existing_album_ids)
    excluded_artist_names = {
        str(name).casefold() for name in (excluded_artist_names or ()) if name
    }
    listened_albums = _lastfm_listened_albums(username, api_key)
    candidates = lastfm.get(
        "tag.gettopalbums",
        username,
        api_key,
        tag=tag_name,
        limit=LASTFM_TAG_FALLBACK_SCAN_LIMIT,
    ).get("albums", {}).get("album", [])
    mapped = []
    for rank, album in enumerate(candidates):
        name = str(album.get("name", "")).strip()
        artist_name = str((album.get("artist") or {}).get("name", "")).strip()
        if (
            not name
            or not artist_name
            or artist_name.casefold() in excluded_artist_names
            or (artist_name.casefold(), name.casefold()) in listened_albums
            or (artist_name.casefold(), name.casefold()) in excluded_album_names
        ):
            continue
        mbid = lastfm_album_mbid(album, username, api_key)
        if (
            not mbid
            or mbid in excluded_album_ids
            or mbid in seen_album_ids
        ):
            continue
        try:
            metadata = _musicbrainz_lookup(f"/release-group/{mbid}") or {}
        except requests.RequestException:
            continue
        date = metadata.get("first-release-date", "")
        recency = _release_recency_score(date)
        mapped.append({
            "id": mbid,
            "name": metadata.get("title") or name,
            "artist": artist_name,
            "type": f"Matched to your {tag_name} taste",
            "date": date,
            "score": round((1 / math.sqrt(rank + 1)) * recency * recency, 4),
            "coverArt": release_group_cover_art(mbid),
            "recommendationSource": f"Last.fm taste · {tag_name}",
        })
        seen_album_ids.add(mbid)
        if len(mapped) >= limit + 4:
            break
    return sorted(mapped, key=lambda item: item["score"], reverse=True)[:limit]


def lastfm_top_tags(username, api_key, limit=10):
    """Return personal tags or infer weighted tags from blended taste seeds."""
    personal_tags = lastfm.get(
        "user.gettoptags", username, api_key, limit=limit
    ).get("toptags", {}).get("tag", [])
    personal_tags = [tag for tag in personal_tags if tag.get("name")]
    if personal_tags:
        return personal_tags[:limit]

    top_artists = _lastfm_taste_artists(username, api_key, limit=8)
    aggregated = {}
    for artist_rank, artist in enumerate(top_artists[:8]):
        identifier = {"mbid": artist["id"]} if artist.get("id") else {"artist": artist.get("name", "")}
        if not next(iter(identifier.values())):
            continue
        try:
            artist_tags = lastfm.get(
                "artist.gettoptags", username, api_key, **identifier
            ).get("toptags", {}).get("tag", [])
        except (ValueError, requests.RequestException):
            continue
        for tag_rank, tag in enumerate(artist_tags[:5]):
            name = str(tag.get("name", "")).strip()
            if not name:
                continue
            key = name.casefold()
            entry = aggregated.setdefault(key, {"name": name, "count": 0})
            entry["count"] += artist["score"] * (5 - tag_rank)
    return sorted(aggregated.values(), key=lambda tag: tag["count"], reverse=True)[:limit]


def _user_value(user, key, default=None):
    try:
        return user[key]
    except (KeyError, IndexError):
        return default


def _recommendation_exclusions(user):
    """Collect already-known artists and albums without making them hard dependencies."""
    excluded = {
        "artist_ids": set(),
        "artist_names": set(),
        "album_ids": set(),
        "album_names": set(),
    }
    user_id = _user_value(user, "id")
    if user_id is None:
        return excluded

    for row in get_request_history(user_id, limit=500):
        if row["kind"] == "artist":
            excluded["artist_ids"].add(row["mbid"])
            excluded["artist_names"].add(row["name"])
        elif row["kind"] == "release-group":
            excluded["album_ids"].add(row["mbid"])

    lidarr_config = get_service("lidarr")
    if lidarr_config:
        try:
            for artist in lidarr.library_artists(lidarr_config):
                mbid = artist.get("foreignArtistId")
                name = artist.get("artistName") or artist.get("name")
                if mbid:
                    excluded["artist_ids"].add(mbid)
                if name:
                    excluded["artist_names"].add(name)
            for album in lidarr.library_albums(lidarr_config):
                mbid = album.get("foreignAlbumId")
                if mbid:
                    excluded["album_ids"].add(mbid)
        except (ValueError, requests.RequestException) as exc:
            logger.warning("Could not load Lidarr exclusions: %s", exc)

    plex_config = get_service("plex")
    if plex_config:
        try:
            inventory = plex.library_snapshot(plex_config)
            for artist in inventory.get("artists", []):
                if artist.get("name"):
                    excluded["artist_names"].add(artist["name"])
                if artist.get("musicbrainzId"):
                    excluded["artist_ids"].add(artist["musicbrainzId"])
            excluded["album_names"].update(
                (release_group["artistName"], release_group["name"])
                for release_group in inventory.get("releaseGroups", [])
                if release_group.get("artistName") and release_group.get("name")
            )
            excluded["album_ids"].update(
                release_group["musicbrainzReleaseGroupId"]
                for release_group in inventory.get("releaseGroups", [])
                if release_group.get("musicbrainzReleaseGroupId")
            )
        except (ValueError, requests.RequestException) as exc:
            logger.warning("Could not load Plex exclusions: %s", exc)
    return excluded


def build_recommendation_cache(user):
    payload = {
        "artists": [],
        "albums": [],
        "chartArtists": [],
        "tagRows": [],
        "providerStatus": {
            "listenbrainz": "disabled",
            "lastfm": "disabled",
        },
    }
    exclusions = _recommendation_exclusions(user)
    if user["listenbrainz_username"]:
        try:
            artists, albums = listenbrainz_recommendations(
                user["listenbrainz_username"],
                excluded_artist_ids=exclusions["artist_ids"],
                excluded_artist_names=exclusions["artist_names"],
                excluded_album_ids=exclusions["album_ids"],
                excluded_album_names=exclusions["album_names"],
            )
            payload["artists"].extend(
                {**item, "recommendationSource": "ListenBrainz"} for item in artists
            )
            payload["albums"].extend(
                {**item, "recommendationSource": "ListenBrainz"} for item in albums
            )
            payload["providerStatus"]["listenbrainz"] = "ok"
        except (ValueError, requests.RequestException) as exc:
            payload["providerStatus"]["listenbrainz"] = "unavailable"
            logger.warning(
                "ListenBrainz recommendations unavailable for %s: %s",
                user["listenbrainz_username"],
                exc,
            )
    if user["lastfm_username"] and user["lastfm_api_key"]:
        username, api_key = user["lastfm_username"], user["lastfm_api_key"]
        lastfm_failed = False
        personalized_albums = []
        try:
            artists, personalized_albums = lastfm_recommendations(
                username,
                api_key,
                excluded_artist_ids=exclusions["artist_ids"],
                excluded_artist_names=exclusions["artist_names"],
                excluded_album_ids=exclusions["album_ids"],
                excluded_album_names=exclusions["album_names"],
            )
            payload["artists"].extend(
                {**item, "recommendationSource": "Last.fm"} for item in artists
            )
            payload["albums"].extend(
                {
                    **{key: value for key, value in item.items() if key != "tasteTags"},
                    "recommendationSource": "Last.fm",
                }
                for item in personalized_albums[:LASTFM_PRIMARY_ALBUM_LIMIT]
            )
        except (ValueError, requests.RequestException) as exc:
            lastfm_failed = True
            logger.warning(
                "Last.fm personalized recommendations unavailable for %s: %s",
                username,
                exc,
            )

        try:
            chart = lastfm.get("chart.gettopartists", username, api_key, limit=20)
            payload["chartArtists"] = [
                {
                    "id": item["mbid"],
                    "name": item["name"],
                    "type": "Popular globally on Last.fm",
                    "coverArt": artist_cover_art(item["mbid"]),
                    "recommendationSource": "Popular on Last.fm",
                }
                for item in chart.get("artists", {}).get("artist", [])
                if item.get("mbid") and item.get("name")
            ]
        except (ValueError, requests.RequestException) as exc:
            lastfm_failed = True
            logger.warning("Last.fm charts unavailable for %s: %s", username, exc)

        try:
            tags = lastfm_top_tags(username, api_key, limit=10)
        except (ValueError, requests.RequestException) as exc:
            tags = []
            lastfm_failed = True
            logger.warning("Last.fm tags unavailable for %s: %s", username, exc)
        main_album_ids = {
            album["id"]
            for album in personalized_albums[:LASTFM_PRIMARY_ALBUM_LIMIT]
        }
        remaining_albums = [
            album for album in personalized_albums if album["id"] not in main_album_ids
        ]
        tag_names = [
            str(tag.get("name", "")).strip()
            for tag in tags[:5]
            if str(tag.get("name", "")).strip()
        ]
        albums_by_tag = {tag_name: [] for tag_name in tag_names}
        tag_rank = {
            tag_name.casefold(): rank for rank, tag_name in enumerate(tag_names)
        }
        for album in remaining_albums:
            matches = [
                tag_name
                for tag_name in albums_by_tag
                if tag_name.casefold() in {
                    candidate_tag.casefold()
                    for candidate_tag in album.get("tasteTags", [])
                }
                and len(albums_by_tag[tag_name]) < LASTFM_TAG_ROW_LIMIT
            ]
            if not matches:
                continue
            selected_tag = min(
                matches,
                key=lambda tag_name: (
                    len(albums_by_tag[tag_name]),
                    tag_rank[tag_name.casefold()],
                ),
            )
            albums_by_tag[selected_tag].append(album)
        for tag_name, tag_albums in albums_by_tag.items():
            if len(tag_albums) < LASTFM_TAG_ROW_LIMIT:
                existing_album_ids = main_album_ids | {
                    album["id"]
                    for albums in albums_by_tag.values()
                    for album in albums
                }
                try:
                    tag_albums.extend(lastfm_tag_recommendations(
                        tag_name,
                        username,
                        api_key,
                        excluded_album_ids=exclusions["album_ids"],
                        excluded_album_names=exclusions["album_names"],
                        excluded_artist_names=exclusions["artist_names"],
                        existing_album_ids=existing_album_ids,
                        limit=LASTFM_TAG_ROW_LIMIT - len(tag_albums),
                    ))
                except (ValueError, requests.RequestException) as exc:
                    lastfm_failed = True
                    logger.warning(
                        "Last.fm taste-row backfill unavailable for %s (%s): %s",
                        username,
                        tag_name,
                        exc,
                    )
            mapped = [
                {
                    **{
                        key: value
                        for key, value in album.items()
                        if key != "tasteTags"
                    },
                    "type": f"Matched to your {tag_name} taste",
                    "recommendationSource": f"Last.fm taste · {tag_name}",
                }
                for album in tag_albums
            ]
            if mapped:
                payload["tagRows"].append({"tag": tag_name, "albums": mapped})
        payload["providerStatus"]["lastfm"] = "partial" if lastfm_failed else "ok"
    return payload


def refresh_recommendation_cache():
    retry_required = False
    for user in recommendation_users():
        try:
            payload = build_recommendation_cache(user)
            save_recommendation_cache(user["id"], payload)
            if any(
                status in {"partial", "unavailable"}
                for status in payload.get("providerStatus", {}).values()
            ):
                retry_required = True
        except (ValueError, requests.RequestException) as exc:
            retry_required = True
            logger.warning("Could not refresh recommendations for %s: %s", user["username"], exc)
    return retry_required

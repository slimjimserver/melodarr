"""Album-first, explainable recommendations built from personal intent and listening."""

import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

if __package__:
    from .recommendation_preferences import preferences_for
    from . import recommendations as engine
    from .recommendation_activity import feedback_for, exposure_counts, prune_exposures
    from .storage import get_request_history, get_lastfm_api_key, get_service, pending_lidarr_search_mbids
    from .services import musicbrainz, lidarr, plex, charts
    from .media_urls import release_group_cover_art
else:
    from recommendation_preferences import preferences_for
    import recommendations as engine
    from recommendation_activity import feedback_for, exposure_counts, prune_exposures
    from storage import get_request_history, get_lastfm_api_key, get_service, pending_lidarr_search_mbids
    from services import musicbrainz, lidarr, plex, charts
    from media_urls import release_group_cover_art

DAY = 86400
FEED_VERSION = 5


def request_seeds(history, feedback=(), now=None):
    """Cap each artist using its strongest request, regardless of discography size."""
    now = time.time() if now is None else now
    artists = {}
    for raw in history:
        row = dict(raw)
        if not row.get("use_for_recommendations", True):
            continue
        name = str(row.get("name") if row.get("kind") == "artist" else row.get("artist_name") or "").strip()
        if not name:
            continue
        weight = 2.5 * 2 ** (-max(0, now - row["created_at"]) / (90 * DAY))
        seed = {"id": row["mbid"] if row["kind"] == "artist" else "", "name": name,
                "score": weight, "origin": "request", "requestedName": row["name"]}
        existing = artists.get(name.casefold())
        if existing and existing.get("id"):
            seed["id"] = existing["id"]
        if not existing or weight > existing["score"]:
            artists[name.casefold()] = seed
        elif seed["id"]:
            existing["id"] = seed["id"]
    for row in feedback:
        if row["action"] != "more":
            continue
        item = json.loads(row["item_json"])
        name = item.get("artist") or item.get("name", "")
        key = name.casefold()
        seed = artists.setdefault(key, {
            "id": item.get("artistId", "") if item["kind"] == "release-group" else item["id"],
            "name": name, "score": 0, "origin": "feedback",
        })
        seed["score"] = max(seed["score"], 2.0)
    return sorted(artists.values(), key=lambda item: item["score"], reverse=True)[:12]


def listening_seeds(user, api_key):
    seeds = []
    config = get_service("plex")
    if engine._user_value(user, "plex_id") and config:
        try:
            seeds.extend(engine.plex_taste_profile(user["id"], config)[0])
        except (ValueError, requests.RequestException):
            pass
    if engine._user_value(user, "lastfm_username") and api_key:
        try:
            seeds.extend(engine._lastfm_taste_artists(user["lastfm_username"], api_key))
        except (ValueError, requests.RequestException):
            pass
    maximum = max((item["score"] for item in seeds), default=1) or 1
    return [{**item, "score": item["score"] / maximum, "origin": "listening"} for item in seeds]


def resolve_seed(seed):
    if seed.get("id"):
        return seed
    # Album request history stores the artist name, not its MBID. Only accept
    # an exact name match; never silently seed an unrelated search result.
    result = musicbrainz.search('artist:"' + seed["name"].replace('"', '') + '"', "artist", priority="background")
    matches = [item for item in result.get("artists", [])
               if item.get("name", "").casefold() == seed["name"].casefold()]
    if len(matches) != 1:
        return seed
    return {**seed, "id": matches[0]["id"]}


def familiar_albums(seeds, exclusions, *, status=None):
    albums = []
    today = datetime.now(timezone.utc).date().isoformat()
    excluded_names = {(a.casefold(), b.casefold()) for a, b in exclusions["album_names"]}
    for original in seeds[:8]:
        try:
            seed = resolve_seed(original)
            if not seed.get("id"):
                continue
            groups = []
            # Bound background catalog work for unusually large discographies.
            for offset in range(0, 300, 100):
                page = musicbrainz.get("/release-group", "", artist=seed["id"],
                                       type="album|ep", limit=100, offset=offset, priority="background")
                batch = page.get("release-groups", [])
                groups.extend(batch)
                if not batch or offset + len(batch) >= page.get("release-group-count", len(groups)):
                    break
            eligible = []
            for group in groups:
                mbid, title = group.get("id"), group.get("title", "")
                date = group.get("first-release-date", "")
                if (not mbid or not title or not date or date > today
                        or mbid in exclusions["album_ids"]
                        or (seed["name"].casefold(), title.casefold()) in excluded_names
                        or group.get("primary-type") not in {"Album", "EP"}
                        or set(group.get("secondary-types", [])) & {"Compilation", "Live", "Remix", "DJ-mix"}):
                    continue
                origin = seed.get("origin")
                reason = (f'You requested {seed.get("requestedName", seed["name"])}'
                          if origin == "request" else f'You asked for more like {seed["name"]}'
                          if origin == "feedback" else f'You chose {seed["name"]} as a favorite'
                          if origin == "starter" else f'You listen to {seed["name"]}')
                eligible.append({
                    "id": mbid, "name": title, "artist": seed["name"], "artistId": seed["id"],
                    "type": group["primary-type"], "date": date,
                    "score": seed["score"] * engine._release_recency_score(date),
                    "reason": reason + "; this album is missing from your library",
                    "recommendationSource": "Your artists", "lane": "familiar",
                    "coverArt": release_group_cover_art(mbid),
                })
            eligible.sort(key=lambda item: (item["score"], item["date"]), reverse=True)
            albums.extend(eligible[:6])
        except (ValueError, requests.RequestException) as exc:
            if status is not None:
                status["failures"] = status.get("failures", 0) + 1
            engine.logger.warning("Familiar album lookup failed (%s)", engine._safe_error_label(exc))
    return albums


def rank_candidates(items, feedback=(), exposures=None):
    """Normalize each provider's ranks, reward agreement and cap repeat exposure."""
    hidden = {(row["kind"], row["mbid"]) for row in feedback if row["action"] == "dismiss"}
    liked = {(row["kind"], row["mbid"]) for row in feedback if row["action"] == "more"}
    exposures = exposures or {}
    sources = defaultdict(list)
    for item in items:
        if item.get("id") and item.get("name"):
            if item.get("providerRanks"):
                for source, rank in item["providerRanks"].items():
                    sources[source].append({**item, "score": 1 / math.sqrt(rank + 1)})
            else:
                sources[item.get("recommendationSource", "Listening")].append(item)
    merged = {}
    for source, candidates in sources.items():
        candidates.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
        source_seen = set()
        for rank, item in enumerate(candidates):
            key = (item.get("kind", "release-group"), item["id"])
            if key in hidden or key in source_seen:
                continue
            source_seen.add(key)
            score = (1 / math.sqrt(rank + 1)) * (1 + min(0.2, max(0, len(item.get("seedNames", [])) - 1) * 0.1))
            if item.get("lane") == "requests":
                score *= 1.3
            if key not in merged:
                merged[key] = {**item, "kind": key[0], "rankScore": score, "sources": [source]}
            else:
                entry = merged[key]
                entry["rankScore"] = max(entry["rankScore"], score) + 0.15
                entry["sources"].append(source)
                if item.get("lane") in {"familiar", "requests"}:
                    entry.update({field: item[field] for field in ("lane", "reason") if field in item})
    for key, item in merged.items():
        item["rankScore"] *= 1 - min(0.3, max(0, exposures.get(key, 0) - 2) * 0.06)
        if key in liked:
            item["rankScore"] *= 1.15
            item["feedback"] = "more"
        item["reason"] = item.get("reason") or (
            "Similar to " + ", ".join(item["seedNames"][:2]) if item.get("seedNames")
            else "Picked from your ListenBrainz recommendations" if "ListenBrainz" in item["sources"]
            else "Matched to your listening history"
        )
        item["rankScore"] = round(item["rankScore"], 5)
    return sorted(merged.values(), key=lambda item: (-item["rankScore"], item["id"]))


def select_sections(ranked, mode="balanced"):
    sections = []
    used = set()
    artist_counts = Counter()
    lanes = [
        ("familiar", "More from artists you love", "Missing albums and recent releases from your personal favorites."),
        ("requests", "Because you requested…", "Follow your recent requests to your next favorite album."),
        ("discovery", "Try something new", "A few discoveries connected to your listening and feedback."),
    ]
    limits = {"familiar": 6, "requests": 6, "discovery": 6}
    if mode == "familiar":
        limits.update(familiar=8, discovery=4)
    elif mode == "discovery":
        limits.update(familiar=4, discovery=8)
        lanes.reverse()
    for lane, title, description in lanes:
        picks = []
        candidates = [item for item in ranked if item.get("lane", "discovery") == lane
                      or (lane == "discovery" and item.get("lane") == "requests"
                          and len(item.get("seedNames", [])) >= 2)]
        # Offer specific albums first. Artists only fill otherwise empty spaces.
        candidates.sort(key=lambda item: item["kind"] == "artist")
        for item in candidates:
            key = (item["kind"], item["id"])
            artist = (item.get("artist") or item["name"]).casefold()
            if key in used or artist_counts[artist] >= 2:
                continue
            used.add(key)
            artist_counts[artist] += 1
            picks.append(item)
            if len(picks) == limits[lane]:
                break
        if picks:
            sections.append({"id": lane, "title": title, "description": description, "items": picks})
    return sections


def build_personal_feed(user, payload, shared_exclusions=None):
    user_id = user["id"]
    prune_exposures(user_id)
    feedback = feedback_for(user_id)
    preferences = preferences_for(user_id)
    history = get_request_history(user_id, limit=500)
    seeds = request_seeds(history, feedback)
    seeded_names = {seed["name"].casefold() for seed in seeds}
    seeds.extend({**artist, "score": 2.2, "origin": "starter"} for artist in preferences["starterArtists"]
                 if artist["name"].casefold() not in seeded_names)
    api_key = get_lastfm_api_key()
    exclusions = engine._recommendation_exclusions(user, shared_exclusions)
    personal_seeds = {item["name"].casefold(): item for item in seeds}
    for seed in listening_seeds(user, api_key):
        personal_seeds.setdefault(seed["name"].casefold(), seed)
    candidates = []
    for kind, key in (("release-group", "albums"), ("artist", "artists")):
        candidates.extend({**item, "kind": kind} for item in payload.get(key, []))
    # Genre chart fallback stays in browse; it is not a personal recommendation.
    for row in payload.get("tagRows", []):
        candidates.extend({**item, "kind": "release-group"} for item in row.get("albums", [])
                          if item.get("seedNames"))
    payload["requestStatus"] = "ok" if seeds else "empty"
    if seeds and api_key:
        try:
            artists, albums = engine.seeded_lastfm_recommendations(
                seeds, api_key, excluded_artist_ids=exclusions["artist_ids"],
                excluded_artist_names=exclusions["artist_names"],
                excluded_album_ids=exclusions["album_ids"], excluded_album_names=exclusions["album_names"],
            )
            requested_names = {seed["name"]: seed.get("requestedName", seed["name"])
                               for seed in seeds if seed.get("origin") == "request"}
            for kind, items in (("artist", artists), ("release-group", albums)):
                for item in items:
                    anchors = [name for name in item.get("seedNames", []) if name in requested_names]
                    reason = ("Because you requested " + ", ".join(requested_names[name] for name in anchors[:2])
                              if anchors else "Similar to " + ", ".join(item.get("seedNames", [])[:2])
                              if item.get("seedNames") else "Connected to your favorite artists")
                    candidates.append({**item, "kind": kind, "lane": "requests" if anchors else "discovery",
                                       "reason": reason, "recommendationSource": "Your requests and feedback"})
        except (ValueError, requests.RequestException) as exc:
            payload["requestStatus"] = "unavailable"
            engine.logger.warning("Request recommendations unavailable (%s)", engine._safe_error_label(exc))
    elif seeds:
        payload["requestStatus"] = "similarity-unconfigured"
    catalog_status = {}
    familiar = familiar_albums(
        sorted(personal_seeds.values(), key=lambda item: item["score"], reverse=True),
        exclusions, status=catalog_status,
    )
    payload["catalogStatus"] = ("partial" if familiar else "unavailable") if catalog_status.get("failures") else "ok"
    candidates.extend({**item, "kind": "release-group"} for item in familiar)
    payload["feedVersion"] = FEED_VERSION
    payload["candidates"] = rank_candidates(candidates, feedback, exposure_counts(user_id))[:100]
    payload["tasteRevision"] = preferences["revision"]
    payload["sections"] = select_sections(payload["candidates"], preferences["mode"])
    # Charts are refreshed independently and joined from cache only by the routes.
    return payload


def current_feed(user_id, payload):
    """Apply feedback and current cached availability without upstream HTTP calls."""
    feedback = feedback_for(user_id)
    hidden = {(row["kind"], row["mbid"]) for row in feedback if row["action"] == "dismiss"}
    liked = {(row["kind"], row["mbid"]) for row in feedback if row["action"] == "more"}
    history = {(row["kind"], row["mbid"]) for row in get_request_history(user_id, limit=500)}
    albums = lidarr.cached_library_availability()
    artists = lidarr.cached_artist_availability()
    downloads = lidarr.cached_download_availability()
    config = get_service("plex")
    plex_index = plex.cached_library_index(config) if config else {}
    plex_albums = plex_index.get("releaseGroupsByMbid", {})
    plex_names = {(item.get("artistName", "").casefold(), item.get("name", "").casefold())
                  for item in plex_index.get("snapshot", {}).get("releaseGroups", [])}
    candidates = [*payload.get("candidates", []), *payload.get("popularCandidates", [])]
    pending = pending_lidarr_search_mbids(item["id"] for item in candidates if item["kind"] == "release-group")
    result = []
    for item in candidates:
        key = (item["kind"], item["id"])
        if key in hidden or key in history:
            continue
        if item["kind"] == "artist" and (item["id"] in artists or item["id"] in plex_index.get("artistsByMbid", {})):
            continue
        status = albums.get(item["id"].casefold(), {}) if item["kind"] == "release-group" else {}
        if item["kind"] == "release-group" and (
            status.get("fullyAvailable") or item["id"].casefold() in pending
            or item["id"].casefold() in downloads
            or item["id"] in plex_albums
            or (item.get("artist", "").casefold(), item["name"].casefold()) in plex_names
        ):
            continue
        result.append({**item, "feedback": "more" if key in liked else None, "availability": "partial" if status.get("trackFileCount") else
                       "missing" if status else "unknown"})
    sections = select_sections([item for item in result if item.get("lane") != "popular"], preferences_for(user_id)["mode"])
    featured = {(item["kind"], item["id"]) for section in sections for item in section["items"]}
    popular = [item for item in result if item.get("lane") == "popular"
               and (item["kind"], item["id"]) not in featured]
    return {**{key: value for key, value in payload.items() if key not in {"candidates", "popularCandidates"}},
            "sections": sections,
            "popularAlbums": [item for item in popular if item.get("chartCountry", "us") == "us"],
            "popularAlbumsByCountry": {
                country: [item for item in popular if item.get("chartCountry", "us") == country]
                for country in charts.CHART_COUNTRIES
            }}

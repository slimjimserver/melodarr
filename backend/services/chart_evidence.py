"""Batched Apple URL evidence and conservative contained-release title matching."""

from urllib.parse import urlparse

import requests

from . import chart_fallback

CHUNK_SIZE = 40
MIN_RELEASE_SCORE = 95


def canonical_apple_resource(item, country):
    apple_id = str(item.get("id") or "")
    source_url = str(item.get("url") or "")
    parsed = urlparse(source_url)
    if (not apple_id.isdecimal() or not 1 <= len(apple_id) <= 20
            or parsed.scheme != "https" or parsed.netloc != "music.apple.com"
            or not parsed.path.startswith(f"/{country}/album/")
            or parsed.path.rstrip("/").split("/")[-1] != apple_id):
        return ""
    return f"https://music.apple.com/{country}/album/{apple_id}"


def _chunks(values):
    for offset in range(0, len(values), CHUNK_SIZE):
        yield values[offset:offset + CHUNK_SIZE]


def _credit_artist_ids(entity):
    return tuple((credit.get("artist") or {}).get("id") for credit in entity.get("artist-credit") or []
                 if isinstance(credit, dict))


def collect_apple_url_evidence(pending, country, lookup_urls, search):
    """Return rank-indexed evidence; never perform one request per Apple row."""
    evidence = {}
    resources = {}
    for rank, item in pending:
        resource = canonical_apple_resource(item, country)
        evidence[rank] = {"canonicalAppleResource": resource, "urlEntityFound": False,
                          "relatedReleaseMbids": [], "parentReleaseGroupMbids": [],
                          "converged": False, "decision": "no_url_entity" if resource else "no_valid_resource"}
        if resource:
            resources.setdefault(resource, []).append(rank)
    if not resources:
        return {rank: {"trace": trace, "group": None} for rank, trace in evidence.items()}

    url_entities = {}
    for chunk in _chunks(sorted(resources)):
        try:
            response = lookup_urls(chunk, priority="background")
        except (ValueError, requests.RequestException) as exc:
            for resource in chunk:
                for rank in resources[resource]:
                    evidence[rank].update({"decision": "lookup_error", "errorType": type(exc).__name__})
            continue
        for entity in response.get("urls") or ([response] if response.get("resource") else []):
            if entity.get("resource") in resources:
                url_entities[entity["resource"]] = entity

    releases_by_rank = {}
    all_release_ids = set()
    for resource, ranks in resources.items():
        entity = url_entities.get(resource)
        if not entity:
            continue
        release_ids = {relation["release"]["id"] for relation in entity.get("relations") or []
                       if relation.get("target-type") == "release"
                       and (relation.get("release") or {}).get("id")}
        for rank in ranks:
            trace = evidence[rank]
            trace["urlEntityFound"] = True
            trace["relatedReleaseMbids"] = sorted(release_ids)
            trace["decision"] = "release_mapping_missing" if release_ids else "no_release_relationship"
            releases_by_rank[rank] = release_ids
        all_release_ids.update(release_ids)

    releases = {}
    for chunk in _chunks(sorted(all_release_ids)):
        query = " OR ".join(f"reid:{mbid}" for mbid in chunk)
        try:
            response = search(query, "release", priority="background", limit=100)
        except (ValueError, requests.RequestException):
            continue
        releases.update({row["id"]: row for row in response.get("releases") or [] if row.get("id") in chunk})

    group_ids = {((release.get("release-group") or {}).get("id")) for release in releases.values()}
    group_ids.discard(None)
    groups = {}
    for chunk in _chunks(sorted(group_ids)):
        query = " OR ".join(f"rgid:{mbid}" for mbid in chunk)
        try:
            response = search(query, "album", priority="background", limit=100)
        except (ValueError, requests.RequestException):
            continue
        groups.update({row["id"]: row for row in response.get("release-groups") or [] if row.get("id") in chunk})

    result = {}
    for rank, trace in evidence.items():
        release_ids = releases_by_rank.get(rank) or set()
        parents = {((releases.get(mbid) or {}).get("release-group") or {}).get("id") for mbid in release_ids}
        parents.discard(None)
        trace["parentReleaseGroupMbids"] = sorted(parents)
        trace["converged"] = bool(release_ids and len(parents) == 1 and all(mbid in releases for mbid in release_ids))
        if release_ids and len(parents) > 1:
            trace["decision"] = "multiple_parent_groups"
        result[rank] = {"trace": trace, "group": groups.get(next(iter(parents))) if trace["converged"] else None,
                        "releases": [releases[mbid] for mbid in sorted(release_ids) if mbid in releases]}
    return result


def select_apple_url(item, evidence):
    """Select only a unique typed parent with corroborating release title/artist."""
    trace = evidence["trace"]
    group = evidence.get("group")
    if not trace["converged"] or group is None:
        return None
    if group.get("primary-type") not in {"Album", "EP"}:
        trace["decision"] = "type_rejected"
        return None
    title = chart_fallback.identity(item.get("name"))
    artist = str(item.get("artistName") or "")
    parent_ids = _credit_artist_ids(group)
    for release in evidence.get("releases") or []:
        if chart_fallback.identity(release.get("title")) != title:
            continue
        points, artist_kind = chart_fallback.artist_identity(release, artist)
        release_ids = _credit_artist_ids(release)
        if points == 25 and release_ids and release_ids == parent_ids:
            trace.update({"decision": "auto_matched", "selectedReleaseMbid": release["id"],
                          "selectedMbid": group["id"], "artistEvidence": artist_kind})
            return group
    trace["decision"] = "title_or_artist_conflict"
    return None


def resolve_release_title(item, search):
    """Require one high-relevance typed group for the complete Apple release title."""
    title = str(item.get("name") or "").strip()
    artist = str(item.get("artistName") or "").strip()
    query = ('release:"' + title.replace('"', '').replace("\\", "")
             + '" AND artist:"' + artist.replace('"', '').replace("\\", "") + '"')
    trace = {"query": query, "candidates": [], "decision": "no_acceptable_candidate"}
    try:
        response = search(query, "album", priority="background", limit=25)
    except (ValueError, requests.RequestException) as exc:
        trace.update({"decision": "lookup_error", "errorType": type(exc).__name__})
        return None, trace
    plausible = []
    for group in response.get("release-groups") or []:
        points, artist_kind = chart_fallback.artist_identity(group, artist)
        try:
            relevance = int(group.get("score") or 0)
        except (TypeError, ValueError):
            relevance = 0
        row = {"id": group.get("id", ""), "title": group.get("title", ""),
               "releaseType": group.get("primary-type") or "", "musicBrainzScore": relevance,
               "artistScore": points, "artistEvidence": artist_kind}
        trace["candidates"].append(row)
        if group.get("id") and relevance >= MIN_RELEASE_SCORE and points == 25:
            plausible.append(group)
    if len(plausible) != 1:
        trace["decision"] = "ambiguous" if len(plausible) > 1 else "no_acceptable_candidate"
        return None, trace
    group = plausible[0]
    if group.get("primary-type") not in {"Album", "EP"}:
        trace["decision"] = "type_rejected"
        return None, trace
    if int(group.get("score") or 0) < 100:
        return None, trace
    trace.update({"decision": "auto_matched", "selectedMbid": group["id"]})
    return group, trace

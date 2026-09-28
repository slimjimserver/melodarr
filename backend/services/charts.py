"""Public album charts, conservatively resolved to requestable release groups."""

import logging
import re
import unicodedata
from datetime import date, timedelta

import requests

if __package__ == "backend.services":
    from ..api_cache import cached_json_get, get_cache_document, set_cache_document
    from ..config import USER_AGENT
    from ..media_urls import release_group_cover_art
    from . import chart_evidence, chart_fallback, musicbrainz
else:
    from api_cache import cached_json_get, get_cache_document, set_cache_document
    from config import USER_AGENT
    from media_urls import release_group_cover_art
    from services import chart_evidence, chart_fallback, musicbrainz

logger = logging.getLogger(__name__)
CHART_URL = "https://rss.marketingtools.apple.com/api/v2/us/music/most-played/100/albums.json"
CHART_COUNTRIES = {"us": "US", "jp": "Japan"}
CHART_TTL = 6 * 60 * 60
RETRY_TTL = 5 * 60
CACHE_NAMESPACE = "album-charts:v1"
DIAGNOSTIC_NAMESPACE = "album-charts-diagnostics:v1"
_EDITION = re.compile(
    r"\s*[\[(](?:deluxe(?: edition| version)?|expanded(?: edition)?|"
    r"(?:\d+(?:st|nd|rd|th) )?anniversary(?: edition)?|remaster(?:ed)?(?: \d{4})?)[\])]\s*$",
    re.IGNORECASE,
)


def _title(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    return _EDITION.sub("", value).strip()


def _identity(value):
    return "".join(character for character in unicodedata.normalize("NFKC", str(value or "")).casefold()
                   if character.isalnum())


def _recent(value, today):
    try:
        # Prefer the original MusicBrainz date: a new deluxe edition of an old
        # album must not turn that album into a new release.
        parts = str(value).split("-")
        released = date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1,
                        int(parts[2]) if len(parts) > 2 else 1)
    except (TypeError, ValueError):
        return False
    return today - timedelta(days=180) <= released <= today


def _counted_search(stats, stage):
    def search(query, search_type, **kwargs):
        if stats is None:
            return musicbrainz.search(query, search_type, **kwargs)
        result = musicbrainz.search(query, search_type, include_cache_status=True, **kwargs)
        response, cached = result if isinstance(result, tuple) else (result, False)
        stats[stage]["logical"] += 1
        stats[stage]["live"] += not cached
        return response
    return search


def _counted_urls(stats):
    def lookup(resources, **kwargs):
        if stats is None:
            return musicbrainz.lookup_urls(resources, **kwargs)
        result = musicbrainz.lookup_urls(resources, include_cache_status=True, **kwargs)
        response, cached = result if isinstance(result, tuple) else (result, False)
        stats["urlLookup"]["logical"] += 1
        stats["urlLookup"]["live"] += not cached
        return response
    return lookup


def _request_stats():
    return {stage: {"logical": 0, "live": 0} for stage in (
        "exactSearch", "urlLookup", "releaseMapping", "groupHydration",
        "releaseTitleSearch", "scoredFallbackSearch")}


def resolve_chart_album(item, rank, today=None, country="us", diagnostic=None, *,
                        exact_only=False, exact_context=None, exact_context_out=None,
                        url_evidence=None, request_stats=None):
    country_name = CHART_COUNTRIES[country]
    title, artist = _title(item.get("name")), str(item.get("artistName") or "").strip()
    if not title or not artist:
        if diagnostic is not None:
            diagnostic["resolution"] = "missing_source_metadata"
        return None
    query = 'releasegroup:"' + title.replace('"', '') + '" AND artist:"' + artist.replace('"', '') + '"'
    if diagnostic is not None:
        diagnostic["matchAttempted"] = True
    response = (_counted_search(request_stats, "exactSearch")(
        query, "album", priority="background") if exact_context is None else
        {"release-groups": exact_context["searchGroups"]})
    matches = {}
    search_groups = response.get("release-groups", [])
    title_matches = credit_matches = invalid_type_matches = 0
    for group in search_groups:
        title_matches += bool(group.get("id") and _identity(_title(group.get("title"))) == _identity(title))
        if group.get("id") and _identity(_title(group.get("title"))) == _identity(title):
            credits = group.get("artist-credit") or []
            names = [credit.get("name") or (credit.get("artist") or {}).get("name", "") for credit in credits]
            credit = "".join(name + str(row.get("joinphrase") or "") for name, row in zip(names, credits))
            if _identity(credit) == _identity(artist):
                credit_matches += 1
                invalid_type_matches += group.get("primary-type") not in {"Album", "EP"}
        if (not group.get("id") or group.get("primary-type") not in {"Album", "EP"}
                or _identity(_title(group.get("title"))) != _identity(title)):
            continue
        credits = group.get("artist-credit") or []
        names = [credit.get("name") or (credit.get("artist") or {}).get("name", "") for credit in credits]
        # Require the complete credit, so a solo artist does not accidentally
        # match a collaboration with an identically titled album.
        credit = "".join(name + str(row.get("joinphrase") or "") for name, row in zip(names, credits))
        if _identity(credit) != _identity(artist):
            continue
        matches[group["id"]] = group
    if diagnostic is not None:
        diagnostic.update({"matchAttempted": True, "searchResults": len(search_groups),
                           "titleMatches": title_matches, "artistCreditMatches": credit_matches,
                           "invalidTypeMatches": invalid_type_matches,
                           "matchingMbids": list(matches),
                           "searchCandidates": [{
                               "id": group.get("id", ""), "title": group.get("title", ""),
                               "type": group.get("primary-type", ""),
                               "artistCredit": "".join(
                                   (credit.get("name") or (credit.get("artist") or {}).get("name", ""))
                                   + str(credit.get("joinphrase") or "")
                                   for credit in group.get("artist-credit") or []),
                           } for group in search_groups]})
    exact_resolution = ("matched" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else
                        "invalid_release_type" if invalid_type_matches else "no_match")
    if diagnostic is not None:
        diagnostic["exactResolution"] = exact_resolution
    if len(matches) == 1:
        group = next(iter(matches.values()))
        method = "exact"
        evidence_stage = "exact_release_group"
    else:
        if exact_context_out is not None:
            exact_context_out.update({"searchGroups": search_groups, "exactResolution": exact_resolution})
        if exact_only:
            return None
        group = None
        evidence_stage = ""
        if url_evidence is not None:
            group = chart_evidence.select_apple_url(item, url_evidence)
            if diagnostic is not None:
                diagnostic["appleUrl"] = url_evidence["trace"]
            if group is not None:
                evidence_stage = "apple_url_release"
        if group is None:
            group, title_trace = chart_evidence.resolve_release_title(
                item, _counted_search(request_stats, "releaseTitleSearch"))
            if diagnostic is not None:
                diagnostic["releaseTitle"] = title_trace
            if group is not None:
                evidence_stage = "release_title"
        if group is None:
            group, fallback_trace, fallback_resolution = chart_fallback.resolve(
                item, search_groups, _counted_search(request_stats, "scoredFallbackSearch"))
            if diagnostic is not None:
                fallback_trace["exactResolution"] = exact_resolution
                diagnostic["fallback"] = fallback_trace
            if group is not None:
                evidence_stage = "scored_fallback"
        if group is None:
            unresolved = ("ambiguous" if exact_resolution == "ambiguous"
                          or title_trace["decision"] == "ambiguous"
                          or fallback_resolution == "ambiguous" else
                          "invalid_release_type" if exact_resolution == "invalid_release_type"
                          or title_trace["decision"] == "type_rejected"
                          or fallback_resolution == "invalid_release_type" else "no_match")
            if diagnostic is not None:
                diagnostic["resolution"] = unresolved
                diagnostic["evidenceStage"] = ("ambiguous" if unresolved == "ambiguous" else
                                               "type_rejected" if unresolved == "invalid_release_type"
                                               else "no_match")
            return None
        method = "fallback"
    if diagnostic is not None:
        diagnostic.update({"resolution": "matched", "matchedMbid": group["id"],
                           "matchMethod": method, "evidenceStage": evidence_stage})
    first_credit = (group.get("artist-credit") or [{}])[0]
    released = group.get("first-release-date") or item.get("releaseDate") or ""
    return {
        "id": group["id"], "kind": "release-group", "name": group["title"],
        "artist": artist, "artistId": (first_credit.get("artist") or {}).get("id", ""),
        "type": group["primary-type"], "date": released,
        "chartRank": rank, "recentRelease": _recent(released, today or date.today()),
        "reason": f"#{rank} on Apple Music’s {country_name} Top 100 albums",
        "recommendationSource": f"Apple Music · {country_name} Top 100", "lane": "popular",
        "chartCountry": country,
        "coverArt": release_group_cover_art(group["id"], size="card"),
    }


def popular_albums(country="us", *, force_refresh=False):
    """Resolve once for all users; retain dated results during upstream outages."""
    if country not in CHART_COUNTRIES:
        raise ValueError("Unsupported chart country")
    namespace = CACHE_NAMESPACE if country == "us" else f"{CACHE_NAMESPACE}:{country}"
    chart_url = CHART_URL.replace("/us/", f"/{country}/")
    source = f"Apple Music · {CHART_COUNTRIES[country]} Top 100"
    cached = get_cache_document(namespace, "current")
    if cached is not None and not force_refresh:
        return cached
    previous = get_cache_document(namespace, "last-success", allow_expired=True)
    try:
        data = cached_json_get(chart_url, namespace="apple-album-chart", ttl=CHART_TTL,
                               headers={"User-Agent": USER_AGENT}, request_timeout=15, cache_response=False)
        feed = data.get("feed") if isinstance(data, dict) else None
        entries = feed.get("results") if isinstance(feed, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError("Album chart is empty or malformed")
        items, seen = [], set()
        diagnostics = []
        pending = []
        stats = _request_stats()
        failures = consecutive_failures = 0
        for rank, entry in enumerate(entries[:100], start=1):
            row = {"rank": rank, "sourceTitle": entry.get("name", "") if isinstance(entry, dict) else "",
                   "sourceArtist": entry.get("artistName", "") if isinstance(entry, dict) else "",
                   "sourceReleaseDate": entry.get("releaseDate", "") if isinstance(entry, dict) else "",
                   "sourceId": entry.get("id", "") if isinstance(entry, dict) else "",
                   "sourceUrl": entry.get("url", "") if isinstance(entry, dict) else "",
                   "resolution": "invalid_source_row" if not isinstance(entry, dict) else "not_attempted"}
            diagnostics.append(row)
            if not isinstance(entry, dict):
                continue
            try:
                context = {}
                item = resolve_chart_album(entry, rank, country=country, diagnostic=row,
                                           exact_only=True, exact_context_out=context,
                                           request_stats=stats)
                consecutive_failures = 0
            except (ValueError, requests.RequestException) as exc:
                row.update({"resolution": "lookup_error", "errorType": type(exc).__name__})
                failures += 1
                consecutive_failures += 1
                # Do not spend minutes repeating calls during an outage.
                if consecutive_failures >= 3:
                    for remaining_rank, remaining in enumerate(entries[rank:100], start=rank + 1):
                        diagnostics.append({"rank": remaining_rank,
                                            "sourceTitle": remaining.get("name", "") if isinstance(remaining, dict) else "",
                                            "sourceArtist": remaining.get("artistName", "") if isinstance(remaining, dict) else "",
                                            "sourceId": remaining.get("id", "") if isinstance(remaining, dict) else "",
                                            "sourceUrl": remaining.get("url", "") if isinstance(remaining, dict) else "",
                                            "resolution": "not_attempted_after_outage"})
                    break
                continue
            if context:
                pending.append((rank, entry, row, context))
            if item and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
                row["recentRelease"] = item.get("recentRelease")
            elif item:
                row.update({"resolution": "duplicate_mbid", "matchedMbid": item["id"],
                            "recentRelease": item.get("recentRelease")})
        if pending:
            def mapped_search(query, search_type, **kwargs):
                stage = "releaseMapping" if search_type == "release" else "groupHydration"
                return _counted_search(stats, stage)(query, search_type, **kwargs)

            evidence = chart_evidence.collect_apple_url_evidence(
                [(rank, entry) for rank, entry, _, _ in pending], country,
                _counted_urls(stats), mapped_search)
            for rank, entry, row, context in pending:
                try:
                    item = resolve_chart_album(
                        entry, rank, country=country, diagnostic=row,
                        exact_context=context, url_evidence=evidence[rank], request_stats=stats)
                except (ValueError, requests.RequestException) as exc:
                    row.update({"resolution": "lookup_error", "errorType": type(exc).__name__})
                    failures += 1
                    continue
                if item and item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)
                    row["recentRelease"] = item.get("recentRelease")
                elif item:
                    row.update({"resolution": "duplicate_mbid", "matchedMbid": item["id"],
                                "recentRelease": item.get("recentRelease")})
        items.sort(key=lambda item: item["chartRank"])
        if failures and not items:
            set_cache_document(f"{DIAGNOSTIC_NAMESPACE}:{country}", "current", {
                "country": country, "updated": str(feed.get("updated") or ""),
                "sourceUrl": chart_url, "sourceRows": len(entries[:100]), "rows": diagnostics,
                "requestCounts": stats,
            }, RETRY_TTL)
            raise ValueError("Chart albums could not be resolved")
        result = {"items": items, "status": "partial" if failures else "ok",
                  "updated": str(feed.get("updated") or ""), "source": source, "country": country,
                  "sourceUrl": chart_url, "stale": False}
        if not failures:
            set_cache_document(namespace, "last-success", result, CHART_TTL)
        set_cache_document(namespace, "current", result, RETRY_TTL if failures else CHART_TTL)
        diagnostic_namespace = f"{DIAGNOSTIC_NAMESPACE}:{country}"
        set_cache_document(diagnostic_namespace, "current", {
            "country": country, "updated": result["updated"], "sourceUrl": chart_url,
            "sourceRows": len(entries[:100]), "rows": diagnostics,
            "requestCounts": stats,
        }, RETRY_TTL if failures else CHART_TTL)
        return result
    except (ValueError, requests.RequestException) as exc:
        logger.warning("Album chart refresh unavailable (%s)", type(exc).__name__)
        result = {**(previous or {"items": [], "updated": "", "source": source, "country": country, "sourceUrl": chart_url}),
                  "status": "unavailable", "stale": bool(previous and previous.get("items"))}
        set_cache_document(namespace, "current", result, RETRY_TTL)
        return result


def cached_chart_diagnostics(country):
    """Read server-side row dispositions for the latest completed chart refresh."""
    if country not in CHART_COUNTRIES:
        raise ValueError("Unsupported chart country")
    return get_cache_document(f"{DIAGNOSTIC_NAMESPACE}:{country}", "current", allow_expired=True)


def cached_popular_albums(country):
    """Read a chart snapshot without fetching or resolving upstream metadata."""
    if country not in CHART_COUNTRIES:
        raise ValueError("Unsupported chart country")
    namespace = CACHE_NAMESPACE if country == "us" else f"{CACHE_NAMESPACE}:{country}"
    fresh = get_cache_document(namespace, "current")
    if fresh is not None:
        return {**fresh, "pending": fresh.get("status") in {"partial", "unavailable"}}
    previous = (get_cache_document(namespace, "current", allow_expired=True)
                or get_cache_document(namespace, "last-success", allow_expired=True))
    return {**(previous or {"items": [], "updated": ""}), "pending": True,
            "status": "refreshing", "stale": bool(previous and previous.get("items")),
            "country": country, "source": f"Apple Music · {CHART_COUNTRIES[country]} Top 100",
            "sourceUrl": CHART_URL.replace("/us/", f"/{country}/")}


def with_cached_charts(payload):
    """Attach shared chart snapshots to a copy of a private personal feed."""
    result = {**payload, "popularCharts": {}, "popularCandidates": []}
    for country in CHART_COUNTRIES:
        chart = cached_popular_albums(country)
        result["popularCharts"][country] = {key: value for key, value in chart.items() if key != "items"}
        result["popularCandidates"].extend({**item, "chartCountry": country} for item in chart["items"])
    result["popularChart"] = result["popularCharts"]["us"]
    return result

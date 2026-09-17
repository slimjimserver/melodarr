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
    from . import musicbrainz
else:
    from api_cache import cached_json_get, get_cache_document, set_cache_document
    from config import USER_AGENT
    from media_urls import release_group_cover_art
    from services import musicbrainz

logger = logging.getLogger(__name__)
CHART_URL = "https://rss.marketingtools.apple.com/api/v2/us/music/most-played/100/albums.json"
CHART_COUNTRIES = {"us": "US", "jp": "Japan"}
CHART_TTL = 6 * 60 * 60
RETRY_TTL = 5 * 60
CACHE_NAMESPACE = "album-charts:v1"
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


def resolve_chart_album(item, rank, today=None, country="us"):
    country_name = CHART_COUNTRIES[country]
    title, artist = _title(item.get("name")), str(item.get("artistName") or "").strip()
    if not title or not artist:
        return None
    query = 'releasegroup:"' + title.replace('"', '') + '" AND artist:"' + artist.replace('"', '') + '"'
    response = musicbrainz.search(query, "album", priority="background")
    matches = {}
    for group in response.get("release-groups", []):
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
    if len(matches) != 1:
        return None
    group = next(iter(matches.values()))
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


def popular_albums(country="us"):
    """Resolve once for all users; retain dated results during upstream outages."""
    if country not in CHART_COUNTRIES:
        raise ValueError("Unsupported chart country")
    namespace = CACHE_NAMESPACE if country == "us" else f"{CACHE_NAMESPACE}:{country}"
    chart_url = CHART_URL.replace("/us/", f"/{country}/")
    source = f"Apple Music · {CHART_COUNTRIES[country]} Top 100"
    cached = get_cache_document(namespace, "current")
    if cached is not None:
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
        failures = consecutive_failures = 0
        for rank, entry in enumerate(entries[:100], start=1):
            if not isinstance(entry, dict):
                continue
            try:
                item = resolve_chart_album(entry, rank, country=country)
                consecutive_failures = 0
            except (ValueError, requests.RequestException):
                failures += 1
                consecutive_failures += 1
                # Do not spend minutes repeating calls during an outage.
                if consecutive_failures >= 3:
                    break
                continue
            if item and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        if failures and not items:
            raise ValueError("Chart albums could not be resolved")
        result = {"items": items, "status": "partial" if failures else "ok",
                  "updated": str(feed.get("updated") or ""), "source": source, "country": country,
                  "sourceUrl": chart_url, "stale": False}
        if not failures:
            set_cache_document(namespace, "last-success", result, CHART_TTL)
        set_cache_document(namespace, "current", result, RETRY_TTL if failures else CHART_TTL)
        return result
    except (ValueError, requests.RequestException) as exc:
        logger.warning("Album chart refresh unavailable (%s)", type(exc).__name__)
        result = {**(previous or {"items": [], "updated": "", "source": source, "country": country, "sourceUrl": chart_url}),
                  "status": "unavailable", "stale": bool(previous and previous.get("items"))}
        set_cache_document(namespace, "current", result, RETRY_TTL)
        return result


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

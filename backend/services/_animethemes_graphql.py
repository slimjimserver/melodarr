"""GraphQL transport and adapters for Melodarr's existing AnimeThemes models.

Queries use variables, and cache identity includes both the query and variables.
Only complete, successful GraphQL responses are allowed into the HTTP cache.
"""

import json

import requests

if __package__ == "backend.services":
    from ..api_cache import cache_key, cached_json_get
    from ..config import USER_AGENT
else:
    from api_cache import cache_key, cached_json_get
    from config import USER_AGENT


URL = "https://graphql.animethemes.moe/"
PAGE_SIZE = 100
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
_IMAGE_FIELDS = "facet: facetLocalized link"
_RESOURCE_FIELDS = "id site: siteLocalized link external_id: externalId"
_SERIES_FIELDS = "id name slug"
_SUMMARY_FIELDS = f"""
    id slug title {{ romaji english native }} year
    season: seasonLocalized media_format: formatLocalized synopsis
    images(first: {PAGE_SIZE}) {{ nodes {{ {_IMAGE_FIELDS} }} }}
"""
_PERFORMANCE_FIELDS = "alias as artist { id slug name { main } }"
_ENTRY_FIELDS = "id episodes notes"
_SONG_FIELDS = f"""
    id title {{ romaji native }}
    performances(first: {PAGE_SIZE}) {{ {_PERFORMANCE_FIELDS} }}
"""
_THEME_FIELDS = f"""
    id type sequence slug song {{ {_SONG_FIELDS} }}
    animethemeentries(first: {PAGE_SIZE}, sort: [ID]) {{ {_ENTRY_FIELDS} }}
"""
_DETAIL_FIELDS = f"""
    {_SUMMARY_FIELDS}
    resources(first: {PAGE_SIZE}) {{ nodes {{ {_RESOURCE_FIELDS} }} {_PAGE_INFO} }}
    series(first: {PAGE_SIZE}) {{ nodes {{ {_SERIES_FIELDS} }} {_PAGE_INFO} }}
    animethemes(first: {PAGE_SIZE}, sort: [ID]) {{ {_THEME_FIELDS} }}
"""
_APPEARANCE_FIELDS = f"id type sequence anime {{ {_SUMMARY_FIELDS} }}"
_ARTIST_SONG_FIELDS = f"""
    id title {{ romaji native }}
    animethemes(first: {PAGE_SIZE}, sort: [ID]) {{ {_APPEARANCE_FIELDS} }}
"""
_ARTIST_PERFORMANCE_FIELDS = f"song {{ {_ARTIST_SONG_FIELDS} }}"
_ARTIST_FIELDS = f"""
    id slug name {{ main }}
    performances(first: {PAGE_SIZE}) {{ {_ARTIST_PERFORMANCE_FIELDS} }}
"""
_SERIES_ANIME_FIELDS = f"""
    {_SUMMARY_FIELDS} animethemes(first: {PAGE_SIZE}, sort: [ID]) {{ id }}
"""
_SERIES_DETAIL_FIELDS = f"""
    {_SERIES_FIELDS}
    anime(first: {PAGE_SIZE}) {{ nodes {{ {_SERIES_ANIME_FIELDS} }} {_PAGE_INFO} }}
"""
_SEARCH_QUERY = f"""
query MelodarrAnimeSearch($query: String!, $limit: Int!) {{
    search(search: $query, first: $limit) {{ anime {{ {_SUMMARY_FIELDS} }} }}
}}
"""


def _params(query, variables):
    return {"query": query, "variables": json.dumps(variables, sort_keys=True)}


def _post_query(url, *, params, headers, timeout):
    # Reuse the HTTP cache's locking, TTLs and refresh behavior for read-only POSTs.
    return requests.post(
        url, json={"query": params["query"], "variables": json.loads(params["variables"])},
        headers=headers, timeout=timeout,
    )


def _validate_response(response):
    try:
        payload = response.json()
    except ValueError as exc:
        raise requests.RequestException("Invalid AnimeThemes GraphQL response.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("errors")
        or not isinstance(payload.get("data"), dict)
    ):
        # Partial data is unsafe for song mappings and must not be cached.
        raise requests.RequestException("AnimeThemes GraphQL request failed.")


def _graphql(query, variables, *, namespace, ttl, force_refresh=False):
    payload = cached_json_get(
        URL, params=_params(query, variables),
        headers={"User-Agent": USER_AGENT}, namespace=namespace, ttl=ttl,
        force_refresh=force_refresh, request_get=_post_query,
        after_response=_validate_response,
    )
    return payload["data"]


def _entity_query(kind, fields, extra_definitions=()):
    if kind in {"anime", "artist", "series"}:
        definition = "$slug: String!"
        selection = f"entity: {kind}(slug: $slug) {{ {fields} }}"
    else:
        definition = "$id: Int!"
        selection = f"entity: {kind}Pagination(id: $id, first: 1) {{ data {{ {fields} }} }}"
    definitions = ", ".join((definition, *extra_definitions))
    return f"query Melodarr{kind.title()}({definitions}) {{ {selection} }}"


def _entity(kind, identifier, fields, *, namespace, ttl, force_refresh=False,
            extra_definitions=(), extra_variables=None):
    variables = {"slug" if kind in {"anime", "artist", "series"} else "id": identifier}
    variables.update(extra_variables or {})
    result = _graphql(
        _entity_query(kind, fields, extra_definitions), variables,
        namespace=namespace, ttl=ttl, force_refresh=force_refresh,
    )["entity"]
    if kind not in {"anime", "artist", "series"}:
        result = next(iter(result["data"]), None)
    return result


def _collect(entity, kind, identifier, field, fields, *, namespace, ttl,
             connection=False, force_refresh=False):
    """Complete cursor connections and page-based lists without truncating them."""
    page = entity[field]
    items = []
    seen_pages = set()
    page_number = 1
    while True:
        batch = page["nodes"] if connection else page
        if not isinstance(batch, list):
            raise requests.RequestException("Invalid AnimeThemes relationship.")
        marker = json.dumps(page, sort_keys=True)
        if marker in seen_pages:
            raise requests.RequestException("AnimeThemes pagination did not advance.")
        seen_pages.add(marker)
        items.extend(batch)
        if connection:
            if not page["pageInfo"]["hasNextPage"]:
                return items
            cursor = page["pageInfo"]["endCursor"]
            if not cursor:
                raise requests.RequestException("Missing AnimeThemes pagination cursor.")
            selection = f"{field}(first: {PAGE_SIZE}, after: $after) {{ nodes {{ {fields} }} {_PAGE_INFO} }}"
            definitions, variables = ("$after: String!",), {"after": cursor}
        else:
            if len(batch) < PAGE_SIZE:
                return items
            page_number += 1
            # Performance pivots do not support a usable ID on all upstream rows.
            sort = "" if field == "performances" else ", sort: [ID]"
            selection = f"{field}(first: {PAGE_SIZE}, page: $page{sort}) {{ {fields} }}"
            definitions, variables = ("$page: Int!",), {"page": page_number}
        parent = _entity(
            kind, identifier, selection, namespace=namespace, ttl=ttl,
            force_refresh=force_refresh, extra_definitions=definitions, extra_variables=variables,
        )
        if parent is None:
            raise requests.RequestException("AnimeThemes entity disappeared during pagination.")
        page = parent[field]


def _summary(anime):
    """Adapt titles and connections to the provider's established internal shape."""
    title = anime.get("title")
    title = title if isinstance(title, dict) else {}
    return {
        **anime,
        "name": title.get("romaji") or title.get("english") or title.get("native"),
        "synonyms": [value for value in title.values() if value],
        "images": (anime.get("images") or {}).get("nodes", []),
    }


def cached_anime(payload):
    """Read both pre-migration and GraphQL cache documents without doing HTTP."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("anime"), dict):
        return payload["anime"]
    data = payload.get("data")
    anime = data.get("entity") if isinstance(data, dict) else None
    return _summary(anime) if isinstance(anime, dict) and anime.get("slug") else None


def anime_cache_key(slug):
    return cache_key("animethemes-detail", URL, _params(
        _entity_query("anime", _DETAIL_FIELDS), {"slug": slug},
    ))


def search(query, limit, *, ttl):
    result = _graphql(
        _SEARCH_QUERY, {"query": query, "limit": limit}, namespace="animethemes-search", ttl=ttl,
    )
    return {"search": {"anime": [
        _summary(anime) for anime in result["search"]["anime"]
        if isinstance(anime, dict) and anime.get("slug")
    ]}}


def detail(slug, *, ttl):
    options = {"namespace": "animethemes-detail", "ttl": ttl}
    anime = _entity("anime", slug, _DETAIL_FIELDS, **options)
    if anime is None:
        return {"anime": None}
    themes = _collect(anime, "anime", slug, "animethemes", _THEME_FIELDS, **options)
    normalized_themes = []
    for theme in themes:
        song = theme.get("song")
        if song:
            performances = _collect(song, "song", song["id"], "performances", _PERFORMANCE_FIELDS, **options)
            song = {
                "id": song["id"],
                "title": song["title"].get("romaji") or song["title"].get("native"),
                "artists": [{
                    **performance["artist"], "name": performance["artist"]["name"]["main"],
                    "artistsong": {"as": performance.get("as") or performance.get("alias")},
                } for performance in performances],
            }
        normalized_themes.append({
            **theme, "song": song,
            "animethemeentries": _collect(
                theme, "animetheme", theme["id"], "animethemeentries", _ENTRY_FIELDS, **options,
            ),
        })
    return {"anime": {
        **_summary(anime), "animethemes": normalized_themes,
        "resources": _collect(anime, "anime", slug, "resources", _RESOURCE_FIELDS, connection=True, **options),
        "series": _collect(anime, "anime", slug, "series", _SERIES_FIELDS, connection=True, **options),
    }}


def series_detail(slug, *, ttl):
    options = {"namespace": "animethemes-series", "ttl": ttl}
    series = _entity("series", slug, _SERIES_DETAIL_FIELDS, **options)
    if series is None:
        return {"series": None}
    anime = _collect(series, "series", slug, "anime", _SERIES_ANIME_FIELDS, connection=True, **options)
    summaries = []
    for item in anime:
        summaries.append({
            **_summary(item),
            "animethemes": _collect(item, "anime", item["slug"], "animethemes", "id", **options),
        })
    return {"series": {**series, "anime": summaries}}


def artist_detail(slug, *, ttl, force_refresh=False):
    options = {"namespace": "animethemes-artist", "ttl": ttl, "force_refresh": force_refresh}
    artist = _entity("artist", slug, _ARTIST_FIELDS, **options)
    if artist is None:
        return {"artist": None}
    performances = _collect(artist, "artist", slug, "performances", _ARTIST_PERFORMANCE_FIELDS, **options)
    songs = {}
    for performance in performances:
        song = performance["song"]
        if song["id"] in songs:
            continue
        themes = _collect(song, "song", song["id"], "animethemes", _APPEARANCE_FIELDS, **options)
        songs[song["id"]] = {
            "id": song["id"], "title": song["title"].get("romaji") or song["title"].get("native"),
            "animethemes": [{**theme, "anime": _summary(theme["anime"])} for theme in themes],
        }
    return {"artist": {**artist, "name": artist["name"]["main"], "songs": list(songs.values())}}

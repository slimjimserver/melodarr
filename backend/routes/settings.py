"""Administrator service configuration and maintenance routes."""

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..api_cache import cache_stats, clear_cache
    from ..artwork_cache import artwork_cache_stats, clear_artwork_cache
    from ..detail_cache import invalidate_all as invalidate_detail_payloads
    from ..responses import api_error
    from ..security import admin_required, login_required
    from ..services import lidarr, plex
    from ..storage import (
        clear_recommendation_cache,
        get_service,
        pending_lidarr_search_stats,
        recommendation_cache_stats,
        save_service,
    )
    from ..workers import lidarr_searches as lidarr_search_worker
    from ..workers import lidarr_library as lidarr_library_worker
    from ..workers import plex as plex_worker
    from ..workers import plex_metadata as plex_metadata_worker
    from ..workers import recommendations as recommendation_worker
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cache_stats, clear_cache
    from artwork_cache import artwork_cache_stats, clear_artwork_cache
    from detail_cache import invalidate_all as invalidate_detail_payloads
    from responses import api_error
    from security import admin_required, login_required
    from services import lidarr, plex
    from storage import (
        clear_recommendation_cache,
        get_service,
        pending_lidarr_search_stats,
        recommendation_cache_stats,
        save_service,
    )
    from workers import lidarr_searches as lidarr_search_worker
    from workers import lidarr_library as lidarr_library_worker
    from workers import plex as plex_worker
    from workers import plex_metadata as plex_metadata_worker
    from workers import recommendations as recommendation_worker


blueprint = Blueprint("settings", __name__)

CACHE_NAMES = {
    "musicbrainz-search": "MusicBrainz Search",
    "musicbrainz-metadata": "MusicBrainz Metadata",
    "musicbrainz-artist-revalidation": "MusicBrainz Artist Revalidation",
    "listenbrainz-metadata": "ListenBrainz Metadata",
    "lastfm": "Last.fm",
    "lidarr-options": "Lidarr Options",
    "lidarr-library": "Lidarr Library Availability",
    "lidarr-artist-metadata": "Lidarr Artist Metadata",
    "plex-library": "Plex Library Inventory",
    "plex-guid": "Plex GUID Mappings",
}


@blueprint.get("/api/health")
@admin_required
def health():
    return jsonify({"lidarr": bool(get_service("lidarr")), "plex": bool(get_service("plex"))})


@blueprint.get("/api/settings")
@admin_required
def settings():
    lidarr_config, plex_config = get_service("lidarr"), get_service("plex")
    return jsonify({
        "lidarr": {
            "configured": bool(lidarr_config),
            "url": lidarr_config.get("url", "") if lidarr_config else "",
            "externalUrl": lidarr_config.get("externalUrl", "") if lidarr_config else "",
            "defaults": lidarr_config.get("defaults", {}) if lidarr_config else {},
        },
        "plex": {
            "configured": bool(plex_config),
            "url": plex_config.get("url", "") if plex_config else "",
            "libraries": plex_config.get("libraries", []) if plex_config else [],
            "librarySectionIds": plex_config.get("librarySectionIds", []) if plex_config else [],
        },
    })


@blueprint.get("/api/settings/maintenance")
@admin_required
def maintenance():
    """Describe runnable background work and disposable cache storage."""
    metadata = cache_stats()
    rows_by_namespace = {
        row["namespace"]: row for row in metadata["namespaces"]
    }
    api_caches = []
    for cache_id, name in CACHE_NAMES.items():
        stats = rows_by_namespace.pop(cache_id, {})
        api_caches.append({
            "id": cache_id,
            "name": name,
            "entries": stats.get("entries", 0),
            "expired": stats.get("expired", 0),
            "valueBytes": stats.get("value_bytes", 0),
            "earliestExpiry": stats.get("earliest_expiry"),
            "latestExpiry": stats.get("latest_expiry"),
        })
    for cache_id, stats in rows_by_namespace.items():
        api_caches.append({
            "id": cache_id,
            "name": cache_id.replace("-", " ").title(),
            "entries": stats["entries"],
            "expired": stats["expired"],
            "valueBytes": stats["value_bytes"],
            "earliestExpiry": stats["earliest_expiry"],
            "latestExpiry": stats["latest_expiry"],
        })

    recommendation_status = recommendation_worker.status()
    lidarr_status = lidarr_search_worker.status()
    lidarr_library_status = lidarr_library_worker.status()
    plex_recent_status = plex_worker.status("recent")
    plex_full_status = plex_worker.status("full")
    plex_metadata_status = plex_metadata_worker.status()
    lidarr_queue = pending_lidarr_search_stats()
    recommendations = recommendation_cache_stats()
    artwork = artwork_cache_stats()
    return jsonify({
        "jobs": [
            {
                "id": "recommendations",
                "name": "Recommendation Refresh",
                "type": "process",
                "schedule": "Every 12 hours",
                **recommendation_status,
            },
            {
                "id": "lidarr-followups",
                "name": "Lidarr Search Follow-Ups",
                "type": "process",
                "schedule": "Every 2 seconds when queued",
                "queued": lidarr_queue["queued"],
                "retrying": lidarr_queue["retrying"],
                **lidarr_status,
            },
            {
                "id": "lidarr-library",
                "name": "Lidarr Library Scan",
                "type": "process",
                "schedule": "Every 4 minutes",
                **lidarr_library_status,
            },
            {
                "id": "plex-recent",
                "name": "Plex Recently Added Scan",
                "type": "process",
                "schedule": "Every 5 minutes",
                **plex_recent_status,
            },
            {
                "id": "plex-full",
                "name": "Plex Full Library Scan",
                "type": "process",
                "schedule": "Every 12 hours",
                **plex_full_status,
            },
            {
                "id": "plex-metadata",
                "name": "Plex MusicBrainz Enrichment",
                "type": "process",
                "schedule": "After Plex library changes",
                **plex_metadata_status,
            },
        ],
        "caches": [
            *api_caches,
            {
                "id": "recommendations",
                "name": "Assembled Recommendations",
                "entries": recommendations["entries"],
                "expired": 0,
                "valueBytes": recommendations["value_bytes"],
                "latestExpiry": None,
            },
            {
                "id": "artwork",
                "name": "Artwork Files",
                "entries": artwork["entries"],
                "expired": artwork["misses"],
                "valueBytes": artwork["valueBytes"],
                "latestExpiry": None,
            },
        ],
        "metadataDatabaseBytes": metadata["databaseBytes"],
    })


@blueprint.post("/api/settings/jobs/<job_id>/run")
@admin_required
def run_job(job_id):
    if job_id == "recommendations":
        recommendation_worker.request_refresh()
        return jsonify({"message": "Recommendation refresh queued."})
    if job_id == "lidarr-followups":
        lidarr_search_worker.request_work()
        return jsonify({"message": "Lidarr follow-up check queued."})
    if job_id == "lidarr-library":
        lidarr_library_worker.request_scan()
        return jsonify({"message": "Lidarr library scan queued."})
    if job_id == "plex-recent":
        plex_worker.request_recent_scan()
        return jsonify({"message": "Plex recently added scan queued."})
    if job_id == "plex-full":
        plex_worker.request_full_scan()
        return jsonify({"message": "Plex full library scan queued."})
    if job_id == "plex-metadata":
        plex_metadata_worker.request_enrichment()
        return jsonify({"message": "Plex MusicBrainz enrichment queued."})
    return api_error("Unknown maintenance job.", 404)


@blueprint.post("/api/settings/cache/<cache_id>/flush")
@admin_required
def flush_cache(cache_id):
    available_namespaces = {
        row["namespace"] for row in cache_stats()["namespaces"]
    }
    if cache_id in {"plex-library", "plex-guid"}:
        clear_cache("plex-library")
        clear_cache("plex-guid")
        invalidate_detail_payloads()
        plex_worker.request_full_scan()
        return jsonify({"message": "Plex library and GUID caches flushed; full scan queued."})
    if cache_id == "lidarr-library":
        clear_cache("lidarr-library")
        invalidate_detail_payloads()
        lidarr_library_worker.request_scan()
        return jsonify({"message": "Lidarr library cache flushed; scan queued."})
    if cache_id in CACHE_NAMES or cache_id in available_namespaces:
        clear_cache(cache_id)
        invalidate_detail_payloads()
        name = CACHE_NAMES.get(cache_id, cache_id.replace("-", " ").title())
        return jsonify({"message": f"{name} cache flushed."})
    if cache_id == "recommendations":
        clear_recommendation_cache()
        recommendation_worker.request_refresh()
        return jsonify({"message": "Recommendation cache flushed; refresh queued."})
    if cache_id == "artwork":
        removed = clear_artwork_cache()
        return jsonify({"message": f"Artwork cache flushed ({removed} files removed)."})
    return api_error("Unknown cache.", 404)


@blueprint.post("/api/settings/lidarr")
@admin_required
def configure_lidarr():
    values = request.get_json(silent=True) or {}
    old = get_service("lidarr") or {}
    config = {
        **lidarr.connection(values, old),
        "externalUrl": str(values.get("externalUrl", "")).strip().rstrip("/"),
        "defaults": {
            "rootFolderPath": values.get("rootFolderPath"),
            "qualityProfileId": values.get("qualityProfileId"),
            "metadataProfileId": values.get("metadataProfileId"),
            "monitor": values.get("monitor", "all"),
            "monitorNewItems": values.get("monitorNewItems", "all"),
            "tags": values.get("tags", []),
            "searchForMissingAlbums": values.get("searchForMissingAlbums", True),
        },
    }
    if not config["url"] or not config["apiKey"]:
        return api_error("Enter both a Lidarr URL and API key.")
    try:
        status = lidarr.system_status(config)
        status.raise_for_status()
        options = lidarr.options(config)
        defaults = config["defaults"]
        if not defaults["rootFolderPath"] and options["rootFolders"]:
            defaults["rootFolderPath"] = options["rootFolders"][0]["path"]
        if not defaults["qualityProfileId"] and options["qualityProfiles"]:
            defaults["qualityProfileId"] = options["qualityProfiles"][0]["id"]
        if not defaults["metadataProfileId"] and options["metadataProfiles"]:
            defaults["metadataProfileId"] = options["metadataProfiles"][0]["id"]
        save_service("lidarr", config)
        clear_cache("lidarr-options")
        invalidate_detail_payloads()
        return jsonify({"message": f"Connected to Lidarr {status.json().get('version', '')}.", "options": options})
    except requests.RequestException:
        return api_error("Could not connect to Lidarr. Check the URL, port, and API key.", 502)


@blueprint.post("/api/settings/lidarr/test")
@admin_required
def test_lidarr():
    values = request.get_json(silent=True) or {}
    config = lidarr.connection(values)
    if not config["url"] or not config["apiKey"]:
        return api_error("Enter a hostname, port, and API key before testing.")
    try:
        response = lidarr.system_status(config)
        response.raise_for_status()
        return jsonify({
            "message": f"Connected to Lidarr {response.json().get('version', '')}.",
            "options": lidarr.options(config),
        })
    except requests.RequestException:
        return api_error("Could not connect to Lidarr. Check the hostname, port, and API key.", 502)


@blueprint.get("/api/lidarr/options")
@login_required
def get_lidarr_options():
    try:
        return jsonify(lidarr.options())
    except (ValueError, requests.RequestException):
        return api_error("Lidarr could not be reached. Recheck its connection in Settings.", 502)


@blueprint.post("/api/settings/plex")
@admin_required
def configure_plex():
    values = request.get_json(silent=True) or {}
    old = get_service("plex") or {}
    config = {
        "url": str(values.get("url", "")).strip().rstrip("/"),
        "token": str(values.get("token", "")).strip() or old.get("token", ""),
    }
    if not config["url"] or not config["token"]:
        return api_error("Enter both a Plex URL and token.")
    try:
        config["machineIdentifier"] = plex.machine_identifier(config)
        libraries = plex.music_sections(config)
        requested_ids = values.get("librarySectionIds")
        if requested_ids is None:
            requested_ids = old.get("librarySectionIds", [item["id"] for item in libraries])
        if not isinstance(requested_ids, list):
            requested_ids = [requested_ids]
        available_ids = {item["id"] for item in libraries}
        selected_ids = [str(value) for value in requested_ids if str(value) in available_ids]
        if not selected_ids:
            return api_error("Select at least one Plex music library.")
        config["libraries"] = libraries
        config["librarySectionIds"] = selected_ids
        save_service("plex", config)
        clear_cache("plex-library")
        clear_cache("plex-guid")
        invalidate_detail_payloads()
        plex_worker.request_full_scan()
        return jsonify({
            "message": "Connected to Plex; full music-library scan queued.",
            "libraries": libraries,
            "librarySectionIds": selected_ids,
        })
    except (ValueError, requests.RequestException):
        return api_error("Could not connect to Plex. Check the URL and token.", 502)


@blueprint.post("/api/settings/plex/test")
@admin_required
def test_plex():
    values = request.get_json(silent=True) or {}
    old = get_service("plex") or {}
    config = {
        "url": str(values.get("url", "")).strip().rstrip("/"),
        "token": str(values.get("token", "")).strip() or old.get("token", ""),
    }
    if not config["url"] or not config["token"]:
        return api_error("Enter a Plex address and token before testing.")
    try:
        machine_identifier = plex.machine_identifier(config)
        libraries = plex.music_sections(config)
        if not libraries:
            return api_error("Plex has no music libraries available for Melodarr.")
        return jsonify({
            "message": "Connected to Plex. Select the music libraries Melodarr should scan.",
            "machineIdentifier": machine_identifier,
            "libraries": libraries,
        })
    except (ValueError, requests.RequestException):
        return api_error("Could not connect to Plex. Check the URL and token.", 502)

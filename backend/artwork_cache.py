"""Disk-backed artwork caching and eviction."""

import os
import time
from hashlib import sha256
from tempfile import NamedTemporaryFile

import requests
from flask import current_app, redirect, send_file

if __package__:
    from .config import (
        ARTWORK_BROWSER_CACHE_TTL,
        ARTWORK_CACHE_DIRECTORY,
        ARTWORK_CACHE_LIMIT_BYTES,
        ARTWORK_MAX_DOWNLOAD_BYTES,
        ARTWORK_MISS_TTL,
    )
else:
    from config import (
        ARTWORK_BROWSER_CACHE_TTL,
        ARTWORK_CACHE_DIRECTORY,
        ARTWORK_CACHE_LIMIT_BYTES,
        ARTWORK_MAX_DOWNLOAD_BYTES,
        ARTWORK_MISS_TTL,
    )


def artwork_cache_file(cache_key):
    for extension in ("jpg", "png", "webp", "gif"):
        path = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.{extension}")
        if os.path.isfile(path):
            return path
    return None


def plex_artist_artwork_key(server_id, rating_key):
    """Return an opaque, filesystem-safe key for a Plex artist thumbnail."""
    identity = f"{server_id}:{rating_key}".encode()
    return f"plex-artist-{sha256(identity).hexdigest()}"


def remove_stale_plex_artist_artwork(valid_keys):
    """Remove permanent Plex thumbnails for artists no longer in the library."""
    valid_keys = set(valid_keys)
    removed = 0
    try:
        with os.scandir(ARTWORK_CACHE_DIRECTORY) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                cache_key = entry.name.rsplit(".", 1)[0]
                if cache_key.startswith("plex-artist-") and cache_key not in valid_keys:
                    os.unlink(entry.path)
                    removed += 1
    except FileNotFoundError:
        pass
    return removed


def trim_artwork_cache():
    """Evict least-recently-served covers until the cache is within its cap."""
    try:
        entries = []
        for filename in os.listdir(ARTWORK_CACHE_DIRECTORY):
            path = os.path.join(ARTWORK_CACHE_DIRECTORY, filename)
            if (
                os.path.isfile(path)
                and not filename.endswith(".miss")
                and not filename.startswith("plex-artist-")
            ):
                entries.append((os.path.getmtime(path), os.path.getsize(path), path))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total <= ARTWORK_CACHE_LIMIT_BYTES:
                break
            os.unlink(path)
            total -= size
    except OSError:
        current_app.logger.warning("Could not trim artwork cache in %s", ARTWORK_CACHE_DIRECTORY)


def artwork_cache_stats():
    """Return counts and sizes for cached covers and negative-cache markers."""
    images = misses = size = 0
    try:
        with os.scandir(ARTWORK_CACHE_DIRECTORY) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.name.endswith(".miss"):
                    misses += 1
                else:
                    images += 1
                size += entry.stat(follow_symlinks=False).st_size
    except FileNotFoundError:
        pass
    return {"entries": images, "misses": misses, "valueBytes": size}


def clear_artwork_cache():
    """Remove only files owned by the artwork cache."""
    removed = 0
    try:
        with os.scandir(ARTWORK_CACHE_DIRECTORY) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    os.unlink(entry.path)
                    removed += 1
    except FileNotFoundError:
        pass
    return removed


def cached_artwork(cache_key, source_url, *, headers=None):
    """Serve a cached image, downloading a safe provider URL on a miss."""
    cached_file = artwork_cache_file(cache_key)
    if cached_file:
        try:
            os.utime(cached_file, None)
        except OSError:
            pass
        return send_file(cached_file, max_age=ARTWORK_BROWSER_CACHE_TTL)

    miss_file = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.miss")
    try:
        if os.path.isfile(miss_file) and time.time() - os.path.getmtime(miss_file) < ARTWORK_MISS_TTL:
            return "", 404
    except OSError:
        pass

    if callable(source_url):
        source_url = source_url()
    if not source_url:
        return "", 404

    temporary_path = None
    try:
        response = requests.get(source_url, headers=headers, stream=True, timeout=20)
        if response.status_code == 404:
            os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
            with open(miss_file, "a", encoding="utf-8"):
                pass
            return "", 404
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        extension = {
            "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"
        }.get(content_type)
        if not extension:
            raise ValueError(f"Artwork provider returned unsupported content type {content_type!r}")

        os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
        final_path = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.{extension}")
        with NamedTemporaryFile("wb", dir=ARTWORK_CACHE_DIRECTORY, delete=False) as file:
            temporary_path = file.name
            downloaded = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                downloaded += len(chunk)
                if downloaded > ARTWORK_MAX_DOWNLOAD_BYTES:
                    raise ValueError("Cover Art Archive image is too large to cache")
                file.write(chunk)
        os.replace(temporary_path, final_path)
        temporary_path = None
        trim_artwork_cache()
        return send_file(final_path, max_age=ARTWORK_BROWSER_CACHE_TTL)
    except (OSError, ValueError, requests.RequestException) as exc:
        current_app.logger.warning("Could not cache artwork %s: %s", cache_key, exc)
        return redirect(source_url, code=302)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)

"""Disk-backed artwork caching, resizing, and eviction."""

import os
import time
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from tempfile import NamedTemporaryFile
from threading import Lock

import requests
from flask import current_app, redirect, send_file
from PIL import Image, UnidentifiedImageError

if __package__:
    from .config import (
        ARTWORK_BROWSER_CACHE_TTL,
        ARTWORK_CACHE_DIRECTORY,
        ARTWORK_CACHE_HIGH_WATER_BYTES,
        ARTWORK_CACHE_LIMIT_BYTES,
        ARTWORK_CACHE_TRIM_INTERVAL,
        ARTWORK_MAX_DOWNLOAD_BYTES,
        ARTWORK_MISS_TTL,
        ARTWORK_SIZES,
        ARTWORK_WEBP_QUALITY,
    )
else:
    from config import (
        ARTWORK_BROWSER_CACHE_TTL,
        ARTWORK_CACHE_DIRECTORY,
        ARTWORK_CACHE_HIGH_WATER_BYTES,
        ARTWORK_CACHE_LIMIT_BYTES,
        ARTWORK_CACHE_TRIM_INTERVAL,
        ARTWORK_MAX_DOWNLOAD_BYTES,
        ARTWORK_MISS_TTL,
        ARTWORK_SIZES,
        ARTWORK_WEBP_QUALITY,
    )


_trim_lock = Lock()
_size_lock = Lock()
_key_locks_lock = Lock()
_key_locks = {}
_last_trim_at = None
_cached_size_bytes = None


@contextmanager
def _artwork_key_lock(cache_key):
    """Serialize cache misses for one image without retaining locks forever."""
    with _key_locks_lock:
        entry = _key_locks.get(cache_key)
        if entry is None:
            entry = {"lock": Lock(), "users": 0}
            _key_locks[cache_key] = entry
        entry["users"] += 1
        lock = entry["lock"]

    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _key_locks_lock:
            entry["users"] -= 1
            if entry["users"] == 0 and _key_locks.get(cache_key) is entry:
                del _key_locks[cache_key]


def _evictable_filename(filename):
    """Return whether a file counts toward the bounded provider-art cache."""
    lowered = filename.lower()
    return (
        not lowered.endswith(".miss")
        and not filename.startswith("plex-artist-")
        and lowered.endswith((".jpg", ".png", ".webp", ".gif"))
    )


def _scan_evictable_entries():
    entries = []
    try:
        with os.scandir(ARTWORK_CACHE_DIRECTORY) as directory:
            for entry in directory:
                if (
                    not entry.is_file(follow_symlinks=False)
                    or not _evictable_filename(entry.name)
                ):
                    continue
                stat = entry.stat(follow_symlinks=False)
                entries.append((stat.st_mtime, stat.st_size, entry.path))
    except FileNotFoundError:
        pass
    return entries


def _estimated_artwork_cache_size():
    """Return a process-local size estimate, scanning only on first use."""
    global _cached_size_bytes
    with _size_lock:
        if _cached_size_bytes is None:
            _cached_size_bytes = sum(size for _, size, _ in _scan_evictable_entries())
        return _cached_size_bytes


def _replace_cache_file(temporary_path, final_path):
    """Atomically publish a cache file and update the size estimate."""
    global _cached_size_bytes
    with _size_lock:
        evictable = _evictable_filename(os.path.basename(final_path))
        previous_size = 0
        if evictable:
            try:
                previous_size = os.path.getsize(final_path)
            except FileNotFoundError:
                pass
        replacement_size = os.path.getsize(temporary_path)
        os.replace(temporary_path, final_path)
        if evictable and _cached_size_bytes is not None:
            _cached_size_bytes = max(
                0, _cached_size_bytes - previous_size + replacement_size
            )


def normalized_size(size):
    """Return a supported variant name, or None for the original image."""
    return size if size in ARTWORK_SIZES else None


def base_cache_key(filename):
    """Return the owning cache key for an original or resized cache file."""
    return filename.rsplit(".", 1)[0].split("@", 1)[0]


def variant_cache_file(cache_key, size):
    return os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}@{size}.webp")


def artwork_cache_file(cache_key, size=None):
    """Return the cached file for a variant, or for the original image."""
    if size:
        path = variant_cache_file(cache_key, size)
        return path if os.path.isfile(path) else None
    for extension in ("jpg", "png", "webp", "gif"):
        path = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.{extension}")
        if os.path.isfile(path):
            return path
    return None


def build_artwork_variant(original_path, cache_key, size):
    """Downscale a cached original into a WebP variant, returning its path.

    Returns None when the original cannot be decoded, so the caller can serve
    the untouched original rather than failing the request.
    """
    edge = ARTWORK_SIZES[size]
    try:
        with Image.open(original_path) as image:
            image = image.convert("RGB")
            # `thumbnail` never upscales, so an already-small source is only
            # re-encoded to WebP rather than stretched.
            image.thumbnail((edge, edge), Image.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, format="WEBP", quality=ARTWORK_WEBP_QUALITY, method=4)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        current_app.logger.warning(
            "Could not resize artwork %s to %s: %s", cache_key, size, exc
        )
        return None

    final_path = variant_cache_file(cache_key, size)
    temporary_path = None
    try:
        os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
        with NamedTemporaryFile("wb", dir=ARTWORK_CACHE_DIRECTORY, delete=False) as file:
            temporary_path = file.name
            file.write(buffer.getvalue())
        _replace_cache_file(temporary_path, final_path)
        temporary_path = None
        return final_path
    except OSError as exc:
        current_app.logger.warning("Could not store artwork variant %s: %s", cache_key, exc)
        return None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


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
                # Resized variants share the owning artist's cache key, so the
                # `@size` suffix has to be removed before the retention check.
                cache_key = base_cache_key(entry.name)
                if cache_key.startswith("plex-artist-") and cache_key not in valid_keys:
                    os.unlink(entry.path)
                    removed += 1
    except FileNotFoundError:
        pass
    return removed


def trim_artwork_cache():
    """Evict least-recently-served covers until the cache is within its cap."""
    global _cached_size_bytes
    try:
        with _size_lock:
            entries = _scan_evictable_entries()
            total = sum(size for _, size, _ in entries)
            for _, size, path in sorted(entries):
                if total <= ARTWORK_CACHE_LIMIT_BYTES:
                    break
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
                total -= size
            _cached_size_bytes = max(0, total)
    except OSError:
        with _size_lock:
            _cached_size_bytes = None
        current_app.logger.warning("Could not trim artwork cache in %s", ARTWORK_CACHE_DIRECTORY)


def maybe_trim_artwork_cache():
    """Trim above the high-water mark, at most once per interval."""
    global _last_trim_at
    if _estimated_artwork_cache_size() <= ARTWORK_CACHE_HIGH_WATER_BYTES:
        return False
    checked_at = time.monotonic()
    if (
        _last_trim_at is not None
        and checked_at - _last_trim_at < ARTWORK_CACHE_TRIM_INTERVAL
    ):
        return False
    if not _trim_lock.acquire(blocking=False):
        return False
    try:
        if _estimated_artwork_cache_size() <= ARTWORK_CACHE_HIGH_WATER_BYTES:
            return False
        checked_at = time.monotonic()
        if (
            _last_trim_at is not None
            and checked_at - _last_trim_at < ARTWORK_CACHE_TRIM_INTERVAL
        ):
            return False
        trim_artwork_cache()
        _last_trim_at = checked_at
        return True
    finally:
        _trim_lock.release()


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
    global _cached_size_bytes
    removed = 0
    try:
        with _size_lock:
            with os.scandir(ARTWORK_CACHE_DIRECTORY) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        os.unlink(entry.path)
                        removed += 1
            _cached_size_bytes = 0
    except FileNotFoundError:
        with _size_lock:
            _cached_size_bytes = 0
    except OSError:
        with _size_lock:
            _cached_size_bytes = None
        raise
    return removed


def _serve(path):
    try:
        os.utime(path, None)
    except OSError:
        pass
    return send_file(path, max_age=ARTWORK_BROWSER_CACHE_TTL)


def _serve_at_size(original_path, cache_key, size):
    """Serve the requested variant, falling back to the original image."""
    if not size:
        return _serve(original_path)
    variant = artwork_cache_file(cache_key, size)
    if not variant:
        variant = build_artwork_variant(original_path, cache_key, size)
    return _serve(variant or original_path)


def cached_artwork(cache_key, source_url, *, headers=None, size=None):
    """Serve a cached image, downloading a safe provider URL on a miss.

    The full-size download is always retained as the regeneration source, so
    adding or changing a variant size never re-requests the upstream provider.
    """
    size = normalized_size(size)
    variant_file = artwork_cache_file(cache_key, size) if size else None
    if variant_file:
        return _serve(variant_file)

    cached_file = artwork_cache_file(cache_key)
    if cached_file and not size:
        return _serve(cached_file)

    with _artwork_key_lock(cache_key):
        # A request ahead of this one may have populated the original, the
        # requested variant, or the negative-cache marker while we waited.
        variant_file = artwork_cache_file(cache_key, size) if size else None
        if variant_file:
            return _serve(variant_file)

        cached_file = artwork_cache_file(cache_key)
        if cached_file:
            return _serve_at_size(cached_file, cache_key, size)

        miss_file = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.miss")
        try:
            if (
                os.path.isfile(miss_file)
                and time.time() - os.path.getmtime(miss_file) < ARTWORK_MISS_TTL
            ):
                return "", 404
        except OSError:
            pass

        resolved_source_url = source_url() if callable(source_url) else source_url
        if not resolved_source_url:
            return "", 404

        temporary_path = None
        try:
            response = requests.get(
                resolved_source_url, headers=headers, stream=True, timeout=20
            )
            if response.status_code == 404:
                os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
                with open(miss_file, "a", encoding="utf-8"):
                    pass
                return "", 404
            response.raise_for_status()
            content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            )
            extension = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
                "image/gif": "gif",
            }.get(content_type)
            if not extension:
                raise ValueError(
                    f"Artwork provider returned unsupported content type {content_type!r}"
                )

            os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
            final_path = os.path.join(
                ARTWORK_CACHE_DIRECTORY, f"{cache_key}.{extension}"
            )
            with NamedTemporaryFile(
                "wb", dir=ARTWORK_CACHE_DIRECTORY, delete=False
            ) as file:
                temporary_path = file.name
                downloaded = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > ARTWORK_MAX_DOWNLOAD_BYTES:
                        raise ValueError("Cover Art Archive image is too large to cache")
                    file.write(chunk)
            _replace_cache_file(temporary_path, final_path)
            temporary_path = None
            served_response = _serve_at_size(final_path, cache_key, size)
            maybe_trim_artwork_cache()
            return served_response
        except (OSError, ValueError, requests.RequestException) as exc:
            current_app.logger.warning(
                "Could not cache artwork %s: %s", cache_key, exc
            )
            return redirect(resolved_source_url, code=302)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

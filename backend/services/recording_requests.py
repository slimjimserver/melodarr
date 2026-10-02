"""Persist acquisition intent; derive lifecycle entirely from local state."""

from uuid import UUID

if __package__ == "backend.services":
    from .. import storage
    from ..request_locks import request_lock
    from . import lidarr, plex, recording_acquisition, release_requests
else:
    import storage
    from request_locks import request_lock
    from services import lidarr, plex, recording_acquisition, release_requests


def _availability(recording_mbid):
    return plex.recording_availability(storage.get_service("plex"), recording_mbid)


def _lifecycle(availability, intent):
    payload = {
        **availability,
        "status": "ready" if availability["available"] else "not_requested",
        "recordingTitle": intent["recording_title"] if intent else None,
        "target": {
            "releaseGroupMbid": intent["release_group_mbid"],
            "title": intent["target_title"],
            "artistName": intent["artist_name"],
            "primaryType": intent["primary_type"],
        } if intent else None,
        "requestedAt": intent["created_at"] if intent else None,
        "downloadStatus": None,
        "retrying": False,
    }
    if availability["available"] or not intent:
        return payload
    target_id = intent["release_group_mbid"]
    album = lidarr.cached_library_availability().get(target_id)
    download = lidarr.cached_download_availability().get(target_id)
    pending = storage.pending_lidarr_search(target_id)
    state, download_status = lidarr.release_group_lifecycle(album, download, bool(pending))
    payload.update(
        status="waiting_for_plex" if state == "available" else state,
        downloadStatus=download_status,
        retrying=bool(pending and pending["last_error"] and state == "queued"),
    )
    return payload


def status(recording_mbid):
    """Only indexed SQL and cached snapshots; no provider calls or mutations."""
    availability = _availability(recording_mbid)
    return _lifecycle(availability, storage.recording_acquisition(recording_mbid))


def _ready(recording_mbid, availability):
    intent = storage.recording_acquisition(recording_mbid)
    return {
        **_lifecycle(availability, intent),
        "alreadyAvailable": True,
        "alreadyRequested": bool(intent),
    }, 200


def request_for_user(recording_mbid, user):
    """Plex first, then reuse accepted intent or initiate its selected target."""
    availability = _availability(recording_mbid)
    if availability["available"]:
        return _ready(recording_mbid, availability)
    with request_lock("recording", recording_mbid):
        # A preceding POST or Plex scan may have completed while we waited.
        availability = _availability(recording_mbid)
        if availability["available"]:
            return _ready(recording_mbid, availability)
        intent = storage.recording_acquisition(recording_mbid)
        already_requested = bool(intent)
        if intent:
            storage.add_recording_acquisition_requester(recording_mbid, user["id"])
        else:
            resolution = recording_acquisition.resolve(recording_mbid)
            if resolution.get("state") == "musicbrainz_unavailable":
                return {"error": "MusicBrainz could not resolve acquisition releases."}, 502
            target = resolution.get("target") if resolution.get("state") == "resolved" else None
            if not target:
                return {"error": "No release group containing this exact recording could be found."}, 404
            # Treat malformed cache/provider data as unusable, never fake work.
            target = {**target, "releaseGroupMbid": str(UUID(target["releaseGroupMbid"]))}
            result = release_requests.request_release_group_for_user(target["releaseGroupMbid"], user)
            if not result.accepted:
                return {"error": result.safe_error}, result.status_code
            storage.save_recording_acquisition(
                recording_mbid, target, resolution.get("recordingTitle") or "", user["id"],
            )
        payload = status(recording_mbid)
        payload.update(alreadyAvailable=False, alreadyRequested=already_requested)
        return payload, 200 if already_requested or payload["available"] else 202

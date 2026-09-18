"""Persistent identity and automation credentials for this Melodarr instance."""

import re
import secrets
from urllib.parse import urlparse

if __package__:
    from .config import APPLICATION_VERSION
    from .storage import get_service, update_service
else:  # Support the existing `python backend/app.py` entry point.
    from config import APPLICATION_VERSION
    from storage import get_service, update_service


DEFAULT_APPLICATION_TITLE = "Melodarr"
MINIMUM_API_KEY_LENGTH = 32


def _title(value):
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 - preserve API validation contract
            "Application title must be text."
        )
    value = re.sub(r"[\r\n]+", " ", value).strip()
    if not value or len(value) > 80 or any(ord(character) < 32 for character in value):
        raise ValueError("Application title must be between 1 and 80 characters.")
    return value


def _url(value):
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004 - preserve API validation contract
            "Application URL must be text."
        )
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or len(value) > 512
    ):
        raise ValueError("Application URL must be an absolute HTTP(S) URL.")
    return value.rstrip("/")


def generate_api_key():
    """Create a URL-safe secret with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def ensure_instance_settings(api_key_override=""):
    """Create durable first-start settings without persisting an env secret."""
    override = str(api_key_override or "").strip()
    legacy_url = str((get_service("notifications") or {}).get("applicationUrl") or "")

    def ensure(current):
        updated = dict(current)
        try:
            updated["applicationTitle"] = _title(
                updated.get("applicationTitle", DEFAULT_APPLICATION_TITLE)
            )
        except ValueError:
            updated["applicationTitle"] = DEFAULT_APPLICATION_TITLE
        try:
            updated["applicationUrl"] = _url(updated.get("applicationUrl", legacy_url))
        except ValueError:
            updated["applicationUrl"] = ""
        stored_key = str(updated.get("apiKey") or "").strip()
        if not override and len(stored_key) < MINIMUM_API_KEY_LENGTH:
            updated["apiKey"] = generate_api_key()
        elif override and not stored_key:
            # The environment value remains outside settings.json. If the
            # override is later removed, the next start generates a key then.
            updated.pop("apiKey", None)
        return updated

    stored = update_service("melodarr", ensure)
    return {
        **stored,
        "apiKey": override or str(stored.get("apiKey") or ""),
        "apiKeyManagedByEnvironment": bool(override),
        "version": APPLICATION_VERSION,
    }


def public_instance_settings(api_key, *, managed_by_environment=False):
    """Return the administrator-visible instance configuration."""
    stored = get_service("melodarr") or {}
    return {
        "applicationTitle": str(
            stored.get("applicationTitle") or DEFAULT_APPLICATION_TITLE
        ),
        "applicationUrl": str(stored.get("applicationUrl") or ""),
        "apiKey": str(api_key or ""),
        "apiKeyManagedByEnvironment": bool(managed_by_environment),
        "version": APPLICATION_VERSION,
    }


def save_instance_settings(values):
    """Validate and persist administrator-editable instance metadata."""
    if not isinstance(values, dict):
        raise ValueError(  # noqa: TRY004 - preserve API validation contract
            "Request body must be a JSON object."
        )
    unknown = set(values).difference({"applicationTitle", "applicationUrl"})
    if unknown:
        raise ValueError("Melodarr settings contain unsupported fields.")
    title = _title(values.get("applicationTitle", ""))
    application_url = _url(values.get("applicationUrl", ""))

    updated = update_service(
        "melodarr",
        lambda current: {
            **current,
            "applicationTitle": title,
            "applicationUrl": application_url,
        },
    )
    # Keep the legacy notification field synchronized so older clients and a
    # later downgrade observe the same canonical URL, including an intentional
    # clear from the Melodarr settings card.
    update_service(
        "notifications",
        lambda current: (
            {**current, "applicationUrl": application_url} if current else current
        ),
    )
    return updated


def rotate_api_key():
    """Replace the stored automation credential and return the new key."""
    key = generate_api_key()
    update_service("melodarr", lambda current: {**current, "apiKey": key})
    return key


def application_identity():
    """Return non-secret instance branding safe for every signed-in user."""
    stored = get_service("melodarr") or {}
    return {
        "applicationTitle": str(
            stored.get("applicationTitle") or DEFAULT_APPLICATION_TITLE
        ),
        "version": APPLICATION_VERSION,
    }

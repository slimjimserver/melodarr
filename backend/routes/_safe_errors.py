"""Keep known validation messages while hiding unexpected exception details."""

from traceback import extract_tb

from flask import current_app


def public_exception_message(exc, allowed_messages, fallback, *, context):
    """Return only a fixed caller-approved message, never exception text."""
    if allowed_messages:
        detail = str(exc)
        for message in allowed_messages:
            if detail == message:
                return message

    frames = extract_tb(exc.__traceback__)
    origin = frames[-1] if frames else None
    current_app.logger.warning(
        "%s failed (%s at %s:%s)",
        context,
        type(exc).__name__,
        origin.name if origin else "unknown",
        origin.lineno if origin else "unknown",
    )
    return fallback

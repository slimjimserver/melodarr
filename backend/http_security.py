"""Security boundaries for outbound HTTP requests."""

import requests


class RedirectRejected(requests.RequestException):
    """Raised when a protected outbound request receives a redirect."""


def request_without_redirects(send, *args, **kwargs):
    """Send one request and fail closed instead of following any 3xx response."""
    kwargs["allow_redirects"] = False
    response = send(*args, **kwargs)
    if 300 <= response.status_code < 400:
        close_response = getattr(response, "close", None)
        if close_response is not None:
            close_response()
        raise RedirectRejected(
            "Protected outbound HTTP requests must not be redirected.",
            response=response,
        )
    return response

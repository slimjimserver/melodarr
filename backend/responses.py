"""Shared HTTP response and request-parsing helpers."""

from flask import jsonify, request


def api_error(message, status=400):
    """Return the consistent JSON error shape used by API routes."""
    return jsonify({"error": message}), status


def request_json_object():
    """Return an object-shaped JSON body, or ``None`` for invalid input."""
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else None

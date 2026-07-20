"""Shared HTTP response helpers."""

from flask import jsonify


def api_error(message, status=400):
    """Return the consistent JSON error shape used by API routes."""
    return jsonify({"error": message}), status

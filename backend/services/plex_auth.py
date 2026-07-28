"""Plex PIN authentication and plex.tv resource discovery."""

import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlencode

import requests


PLEX_TV_URL = "https://plex.tv"
PLEX_AUTH_URL = "https://app.plex.tv/auth/#!"
PLEX_PRODUCT = "Melodarr"
PLEX_VERSION = "0.1"
REQUEST_TIMEOUT = 15


def _headers(client_identifier, token=""):
    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": PLEX_VERSION,
        "X-Plex-Platform": PLEX_PRODUCT,
        "X-Plex-Device": "Web",
        "X-Plex-Device-Name": PLEX_PRODUCT,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def _expires_at(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def create_pin(client_identifier):
    """Create a short-lived Plex PIN and return its browser authorization URL."""
    response = requests.post(
        f"{PLEX_TV_URL}/api/v2/pins",
        params={"strong": "true"},
        headers=_headers(client_identifier),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    pin_id = payload.get("id")
    code = str(payload.get("code", ""))
    if pin_id is None or not code:
        raise ValueError("Plex did not return a valid sign-in PIN.")
    query = urlencode({
        "clientID": client_identifier,
        "code": code,
        "context[device][product]": PLEX_PRODUCT,
        "context[device][version]": PLEX_VERSION,
        "context[device][platform]": PLEX_PRODUCT,
        "context[device][device]": "Web",
        "context[device][deviceName]": PLEX_PRODUCT,
        "context[device][model]": "Plex OAuth",
        "context[device][layout]": "desktop",
    })
    return {
        "id": int(pin_id),
        "code": code,
        "authorizationUrl": f"{PLEX_AUTH_URL}?{query}",
        "expiresAt": _expires_at(payload.get("expiresAt")),
    }


def poll_pin(pin_id, client_identifier):
    """Return the Plex token for an authorized PIN, or an empty string."""
    response = requests.get(
        f"{PLEX_TV_URL}/api/v2/pins/{int(pin_id)}",
        headers=_headers(client_identifier),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    auth_token = response.json().get("authToken")
    return str(auth_token) if auth_token else ""


def get_account(token, client_identifier):
    """Return the stable identity fields for the authenticated Plex account."""
    response = requests.get(
        f"{PLEX_TV_URL}/users/account.json",
        headers=_headers(client_identifier, token),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    account = payload.get("user", payload)
    account_id = account.get("id")
    if account_id is None:
        raise ValueError("Plex did not return an account identifier.")
    return {
        "id": str(account_id),
        "username": str(account.get("username") or account.get("title") or ""),
        "title": str(account.get("title") or account.get("username") or ""),
        "email": str(account.get("email") or "").lower(),
        "thumb": str(account.get("thumb") or ""),
    }


def _boolean(value):
    return value is True or str(value).lower() in {"1", "true", "yes"}


def _connection(value):
    uri = str(value.get("uri") or "").rstrip("/")
    protocol = str(value.get("protocol") or "")
    address = str(value.get("address") or "")
    try:
        port = int(value.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not uri or protocol not in {"http", "https"}:
        return None
    return {
        "uri": uri,
        "protocol": protocol,
        "address": address,
        "port": port,
        "local": _boolean(value.get("local")),
        "secure": protocol == "https",
    }


def _json_resources(payload):
    if isinstance(payload, dict):
        payload = (
            payload.get("MediaContainer", {}).get("Device")
            or payload.get("devices")
            or payload.get("resources")
            or []
        )
    resources = []
    for item in payload if isinstance(payload, list) else []:
        provides = item.get("provides", [])
        if isinstance(provides, str):
            provides = provides.split(",")
        connections = [
            normalized
            for value in item.get("connections", item.get("Connection", []))
            if (normalized := _connection(value))
        ]
        resources.append({
            "name": str(item.get("name") or "Plex Server"),
            "product": str(item.get("product") or ""),
            "clientIdentifier": str(
                item.get("clientIdentifier") or item.get("client_identifier") or ""
            ),
            "provides": [str(value) for value in provides],
            "owned": _boolean(item.get("owned")),
            "accessToken": str(item.get("accessToken") or item.get("access_token") or ""),
            "connections": connections,
        })
    return resources


def _xml_resources(content):
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Plex returned an invalid server list.") from exc
    resources = []
    for item in root.findall(".//Device"):
        provides = str(item.attrib.get("provides", "")).split(",")
        connections = [
            normalized
            for element in item.findall("Connection")
            if (normalized := _connection(element.attrib))
        ]
        resources.append({
            "name": str(item.attrib.get("name") or "Plex Server"),
            "product": str(item.attrib.get("product") or ""),
            "clientIdentifier": str(item.attrib.get("clientIdentifier") or ""),
            "provides": [value for value in provides if value],
            "owned": _boolean(item.attrib.get("owned")),
            "accessToken": str(item.attrib.get("accessToken") or ""),
            "connections": connections,
        })
    return resources


def get_resources(token, client_identifier):
    """Return Plex Media Server resources and their advertised connections."""
    response = requests.get(
        f"{PLEX_TV_URL}/api/resources",
        params={"includeHttps": "1"},
        headers=_headers(client_identifier, token),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    content_type = str(response.headers.get("Content-Type", "")).lower()
    try:
        resources = _json_resources(response.json())
    except (ValueError, TypeError):
        resources = _xml_resources(response.content)
    if "json" not in content_type and not resources and response.content:
        resources = _xml_resources(response.content)

    servers = []
    for resource in resources:
        if "server" not in resource["provides"] or not resource["clientIdentifier"]:
            continue
        seen = set()
        connections = []
        for connection in resource["connections"]:
            if connection["uri"] in seen:
                continue
            seen.add(connection["uri"])
            connections.append(connection)
        if connections:
            resource["connections"] = connections
            servers.append(resource)
    return servers

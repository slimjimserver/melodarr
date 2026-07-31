"""Dependency-light structured-output clients for supported AI providers."""

import json
import os
from ipaddress import ip_address
from urllib.parse import quote, urlsplit, urlunsplit

import requests


CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 45
LOCAL_READ_TIMEOUT_SECONDS = 240
RANKING_OUTPUT_TOKENS = 600
MAX_RESPONSE_BYTES = 256 * 1024
MAX_MODEL_LENGTH = 120
MAX_API_KEY_LENGTH = 4096
SEARCH_PLAN_OUTPUT_TOKENS = 600

PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "defaultModel": "gpt-5.6-sol",
        "requiresApiKey": True,
        "supportsApiKey": True,
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "defaultModel": "claude-sonnet-5",
        "requiresApiKey": True,
        "supportsApiKey": True,
    },
    "gemini": {
        "name": "Google Gemini",
        "defaultModel": "gemini-3.6-flash",
        "requiresApiKey": True,
        "supportsApiKey": True,
    },
    "lmstudio": {
        "name": "LM Studio",
        # Model identifiers depend on what the administrator has downloaded
        # or exposed through LM Studio's just-in-time model loading.
        "defaultModel": "",
        "requiresApiKey": False,
        "supportsApiKey": True,
    },
    "ollama": {
        "name": "Ollama",
        # Ollama model names are installation-specific. Requiring an explicit
        # choice avoids saving a configuration that cannot work locally.
        "defaultModel": "",
        "requiresApiKey": False,
        "supportsApiKey": False,
    },
}

_API_KEY_ENVIRONMENT = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "lmstudio": ("LM_STUDIO_API_KEY",),
}


class AIProviderError(RuntimeError):
    """Base class for errors safe to translate at the API boundary."""


class AIConfigurationError(AIProviderError):
    """The selected provider is not ready to make requests."""


class AIUpstreamError(AIProviderError):
    """A provider could not complete a request."""


class AIResponseError(AIProviderError):
    """A provider returned an incomplete or malformed structured response."""


def provider_catalog():
    """Return public provider metadata without any credentials."""
    return [
        {"id": provider_id, **metadata}
        for provider_id, metadata in PROVIDERS.items()
    ]


def _clean_field(value, name, maximum):
    if not isinstance(value, str):
        raise AIConfigurationError(f"{name} must be text.")
    cleaned = value.strip()
    if len(cleaned) > maximum or any(ord(character) < 32 for character in cleaned):
        raise AIConfigurationError(f"{name} is invalid.")
    return cleaned


def _normalize_local_base_url(value, provider_name, accepted_api_path):
    """Validate a credential-free local inference server origin.

    Local model servers commonly run elsewhere on a private network or on the
    Docker host, so a hostname allowlist would break legitimate installations.
    Restrict the value to a credential-free HTTP(S) origin and never follow
    redirects. A provider's conventional API prefix is accepted and stripped
    so Melodarr cannot accidentally append it twice.
    """
    field_name = f"{provider_name} URL"
    cleaned = _clean_field(value, field_name, 500).rstrip("/")
    parsed = urlsplit(cleaned)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise AIConfigurationError(f"{field_name} has an invalid port.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AIConfigurationError(
            f"{field_name} must be a credential-free HTTP or HTTPS address."
        )
    try:
        address = ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        raise AIConfigurationError(
            f"{field_name} uses an unsafe network address."
        )
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise AIConfigurationError(f"{field_name} has an invalid port.")
    path = parsed.path.rstrip("/")
    if path == accepted_api_path:
        path = ""
    elif path not in {"", "/"}:
        raise AIConfigurationError(f"{field_name} must not include a path.")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def normalize_ollama_base_url(value):
    """Validate an administrator-supplied Ollama origin."""
    return _normalize_local_base_url(value, "Ollama", "/api")


def normalize_lmstudio_base_url(value):
    """Validate an administrator-supplied LM Studio origin."""
    return _normalize_local_base_url(value, "LM Studio", "/v1")


def normalize_saved_settings(values, previous=None):
    """Validate public admin input while retaining an omitted stored key."""
    if not isinstance(values, dict):
        raise AIConfigurationError("Request body must be a JSON object.")
    previous = previous if isinstance(previous, dict) else {}
    provider = _clean_field(values.get("provider", ""), "AI provider", 30).lower()
    if provider not in PROVIDERS:
        raise AIConfigurationError("Choose a supported AI provider.")

    model_value = values.get("model", PROVIDERS[provider]["defaultModel"])
    model = _clean_field(model_value, "AI model", MAX_MODEL_LENGTH)
    if not model:
        model = PROVIDERS[provider]["defaultModel"]
    if not model:
        if provider == "lmstudio":
            message = "Enter the model identifier exposed by LM Studio."
        elif provider == "ollama":
            message = "Enter the name of an installed Ollama model."
        else:
            message = "Enter the AI model to use."
        raise AIConfigurationError(message)

    saved = {"provider": provider, "model": model}
    if provider in {"lmstudio", "ollama"}:
        same_provider = previous.get("provider") == provider
        default_base_url = (
            "http://localhost:1234"
            if provider == "lmstudio"
            else "http://localhost:11434"
        )
        base_url = values.get(
            "baseUrl",
            (previous.get("baseUrl") if same_provider else "")
            or default_base_url,
        )
        normalizer = (
            normalize_lmstudio_base_url
            if provider == "lmstudio"
            else normalize_ollama_base_url
        )
        saved["baseUrl"] = normalizer(base_url)
    if PROVIDERS[provider]["supportsApiKey"]:
        if values.get("clearApiKey") is True:
            api_key = ""
        elif "apiKey" in values and str(values.get("apiKey") or "").strip():
            api_key = _clean_field(
                values["apiKey"],
                "API key",
                MAX_API_KEY_LENGTH,
            )
        elif previous.get("provider") == provider:
            api_key = str(previous.get("apiKey") or "").strip()
        else:
            api_key = ""
        if api_key:
            saved["apiKey"] = api_key
    return saved


def _environment_api_key(provider):
    for name in _API_KEY_ENVIRONMENT.get(provider, ()):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def resolve_settings(saved=None):
    """Resolve stored settings with optional server-side environment secrets."""
    saved = saved if isinstance(saved, dict) else {}
    provider = str(
        saved.get("provider") or os.getenv("MELODARR_AI_PROVIDER") or ""
    ).strip().lower()
    if provider not in PROVIDERS:
        return None
    model = str(
        saved.get("model")
        or os.getenv("MELODARR_AI_MODEL")
        or PROVIDERS[provider]["defaultModel"]
    ).strip()
    if not model or len(model) > MAX_MODEL_LENGTH:
        return None
    settings = {"provider": provider, "model": model}
    if provider in {"lmstudio", "ollama"}:
        is_lmstudio = provider == "lmstudio"
        normalizer = (
            normalize_lmstudio_base_url
            if is_lmstudio
            else normalize_ollama_base_url
        )
        try:
            settings["baseUrl"] = normalizer(
                saved.get("baseUrl")
                or os.getenv(
                    "LM_STUDIO_BASE_URL" if is_lmstudio else "OLLAMA_BASE_URL"
                )
                or (
                    "http://localhost:1234"
                    if is_lmstudio
                    else "http://localhost:11434"
                )
            )
        except AIConfigurationError:
            return None
        if PROVIDERS[provider]["supportsApiKey"]:
            settings["apiKey"] = (
                str(saved.get("apiKey") or "").strip()
                or _environment_api_key(provider)
            )
    else:
        settings["apiKey"] = (
            str(saved.get("apiKey") or "").strip()
            or _environment_api_key(provider)
        )
    return settings


def public_status(saved=None):
    """Describe readiness without returning credentials or internal endpoints."""
    resolved = resolve_settings(saved)
    provider = resolved.get("provider") if resolved else ""
    requires_key = bool(
        provider and PROVIDERS[provider]["requiresApiKey"]
    )
    has_api_key = bool(resolved and resolved.get("apiKey"))
    configured = bool(
        resolved
        and resolved.get("model")
        and (not requires_key or has_api_key)
    )
    return {
        "configured": configured,
        "provider": provider,
        "model": resolved.get("model", "") if resolved else "",
        "hasApiKey": has_api_key,
        "providers": provider_catalog(),
    }


def administrative_status(saved=None):
    """Return redacted configuration fields to administrators only."""
    saved = saved if isinstance(saved, dict) else {}
    resolved = resolve_settings(saved) or {}
    public = public_status(saved)
    provider = str(saved.get("provider") or public["provider"] or "").strip().lower()
    return {
        "configured": public["configured"],
        "provider": provider if provider in PROVIDERS else "",
        "model": str(saved.get("model") or public["model"] or "").strip(),
        "baseUrl": (
            str(saved.get("baseUrl") or resolved.get("baseUrl") or "").strip()
            if provider in {"lmstudio", "ollama"}
            else ""
        ),
        "apiKeyConfigured": public["hasApiKey"],
    }


def recommendation_schema(candidate_ids, maximum_results):
    """Build a compact ordered-ID schema shared by every provider.

    Display reasons are generated from trusted catalog evidence by Melodarr,
    so asking the model to write prose only increases latency and creates text
    that must be discarded. The caller may request more results than retrieval
    found; never invite the model to fill those empty positions with repeats.
    """
    candidate_ids = list(dict.fromkeys(candidate_ids))
    maximum_results = min(maximum_results, len(candidate_ids))
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "recommendations": {
                "type": "array",
                "minItems": 0,
                "maxItems": maximum_results,
                "items": {"type": "string", "enum": candidate_ids},
            },
        },
        "required": ["recommendations"],
    }


def search_plan_schema():
    """Return the provider-neutral contract for bounded query interpretation."""
    short_text = {
        "type": "string",
        "minLength": 1,
        "maxLength": 80,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "entityTypes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "string", "enum": ["artist", "album"]},
            },
            "mustMatchTags": {
                "type": "array",
                "minItems": 0,
                "maxItems": 3,
                "items": short_text,
            },
            "discoveryTags": {
                "type": "array",
                "minItems": 0,
                "maxItems": 4,
                "items": short_text,
            },
            "seedArtists": {
                "type": "array",
                "minItems": 0,
                "maxItems": 3,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                },
            },
            "openEnded": {"type": "boolean"},
        },
        "required": [
            "entityTypes",
            "mustMatchTags",
            "discoveryTags",
            "seedArtists",
            "openEnded",
        ],
    }


def _request_json(
    url,
    *,
    headers,
    payload,
    read_timeout=READ_TIMEOUT_SECONDS,
):
    """POST once with bounded timeouts and sanitized failures.

    A recommendation request may be billable, so ambiguous transport failures
    are not retried automatically. Provider response bodies and credentialed
    URLs are deliberately excluded from every raised error.
    """
    response = None
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=(CONNECT_TIMEOUT_SECONDS, read_timeout),
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect:
            raise AIUpstreamError(
                "The AI provider returned an unexpected redirect."
            )
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                message = "The AI provider rejected its configured credentials."
            elif response.status_code == 429:
                message = "The AI provider is rate limited. Try again shortly."
            else:
                message = "The AI provider could not complete the request."
            raise AIUpstreamError(message)

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = 0
            if declared_size > MAX_RESPONSE_BYTES:
                raise AIResponseError(
                    "The AI provider returned an oversized response."
                )

        body = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AIResponseError(
                    "The AI provider returned an oversized response."
                )
    except requests.Timeout as exc:
        raise AIUpstreamError("The AI provider timed out.") from exc
    except requests.RequestException as exc:
        raise AIUpstreamError("The AI provider could not be reached.") from exc
    finally:
        if response is not None:
            response.close()
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIResponseError("The AI provider returned an invalid response.") from exc
    if not isinstance(value, dict):
        raise AIResponseError("The AI provider returned an invalid response.")
    return value


def _decode_json_text(text):
    if not isinstance(text, str) or not text.strip():
        raise AIResponseError("The AI provider returned an empty response.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIResponseError(
            "The AI provider did not return valid structured recommendations."
        ) from exc
    if not isinstance(value, dict):
        raise AIResponseError(
            "The AI provider did not return structured recommendations."
        )
    return value


def _openai(
    settings,
    system_prompt,
    user_prompt,
    schema,
    *,
    schema_name="music_recommendations",
    max_output_tokens=RANKING_OUTPUT_TOKENS,
):
    payload = {
        "model": settings["model"],
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "store": False,
        "max_output_tokens": max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
    }
    data = _request_json(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {settings['apiKey']}",
            "Content-Type": "application/json",
        },
        payload=payload,
    )
    if data.get("status") == "incomplete":
        raise AIResponseError("OpenAI could not finish the recommendation response.")
    texts = []
    for output in data.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise AIResponseError("OpenAI declined the recommendation request.")
            if content.get("type") == "output_text" and isinstance(
                content.get("text"), str
            ):
                texts.append(content["text"])
    return _decode_json_text("".join(texts))


def _anthropic(
    settings,
    system_prompt,
    user_prompt,
    schema,
    *,
    schema_name="music_recommendations",
    max_output_tokens=RANKING_OUTPUT_TOKENS,
):
    data = _request_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings["apiKey"],
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload={
            "model": settings["model"],
            "max_tokens": max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "output_config": {
                "format": {"type": "json_schema", "schema": schema}
            },
        },
    )
    if data.get("stop_reason") != "end_turn":
        raise AIResponseError("Claude could not finish the recommendation response.")
    texts = [
        block["text"]
        for block in data.get("content") or []
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return _decode_json_text("".join(texts))


def _gemini(
    settings,
    system_prompt,
    user_prompt,
    schema,
    *,
    schema_name="music_recommendations",
    max_output_tokens=RANKING_OUTPUT_TOKENS,
):
    model = quote(settings["model"], safe="")
    data = _request_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={
            "x-goog-api-key": settings["apiKey"],
            "Content-Type": "application/json",
        },
        payload={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]}
            ],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "responseFormat": {
                    "text": {
                        "mimeType": "application/json",
                        "schema": schema,
                    }
                },
            },
        },
    )
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    if candidate.get("finishReason") != "STOP":
        raise AIResponseError("Gemini could not finish the recommendation response.")
    content = candidate.get("content")
    if not isinstance(content, dict):
        raise AIResponseError("Gemini returned an invalid recommendation response.")
    parts = content.get("parts") or []
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return _decode_json_text("".join(texts))


def _ollama(
    settings,
    system_prompt,
    user_prompt,
    schema,
    *,
    schema_name="music_recommendations",
    max_output_tokens=RANKING_OUTPUT_TOKENS,
):
    data = _request_json(
        f"{settings['baseUrl']}/api/chat",
        headers={"Content-Type": "application/json"},
        payload={
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": schema,
            "options": {
                "temperature": 0.2,
                "num_predict": max_output_tokens,
            },
        },
        read_timeout=LOCAL_READ_TIMEOUT_SECONDS,
    )
    if data.get("done") is not True or data.get("done_reason") not in {None, "stop"}:
        raise AIResponseError("Ollama could not finish the recommendation response.")
    message = data.get("message")
    if not isinstance(message, dict):
        raise AIResponseError("Ollama returned an invalid recommendation response.")
    return _decode_json_text(message.get("content"))


def _lmstudio(
    settings,
    system_prompt,
    user_prompt,
    schema,
    *,
    schema_name="music_recommendations",
    max_output_tokens=RANKING_OUTPUT_TOKENS,
):
    headers = {"Content-Type": "application/json"}
    if settings.get("apiKey"):
        headers["Authorization"] = f"Bearer {settings['apiKey']}"
    data = _request_json(
        f"{settings['baseUrl']}/v1/chat/completions",
        headers=headers,
        payload={
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        },
        read_timeout=LOCAL_READ_TIMEOUT_SECONDS,
    )
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    if choice.get("finish_reason") != "stop":
        raise AIResponseError(
            "LM Studio could not finish the recommendation response."
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AIResponseError(
            "LM Studio returned an invalid recommendation response."
        )
    if message.get("refusal"):
        raise AIResponseError("LM Studio declined the recommendation request.")
    return _decode_json_text(message.get("content"))


_GENERATORS = {
    "openai": _openai,
    "anthropic": _anthropic,
    "gemini": _gemini,
    "lmstudio": _lmstudio,
    "ollama": _ollama,
}


def _generate_structured(
    settings,
    *,
    system_prompt,
    user_prompt,
    schema,
    schema_name,
    max_output_tokens,
):
    """Run one validated provider using a caller-supplied JSON schema."""
    if not settings or settings.get("provider") not in PROVIDERS:
        raise AIConfigurationError("AI recommendations are not configured.")
    provider = settings["provider"]
    if PROVIDERS[provider]["requiresApiKey"] and not settings.get("apiKey"):
        raise AIConfigurationError(
            f"{PROVIDERS[provider]['name']} requires an API key."
        )
    return _GENERATORS[provider](
        settings,
        system_prompt,
        user_prompt,
        schema,
        schema_name=schema_name,
        max_output_tokens=max_output_tokens,
    )


def generate_search_plan(settings, *, system_prompt, user_prompt):
    """Interpret a request without allowing the provider to name final results."""
    return _generate_structured(
        settings,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=search_plan_schema(),
        schema_name="music_search_plan",
        max_output_tokens=SEARCH_PLAN_OUTPUT_TOKENS,
    )


def generate_recommendations(
    settings,
    *,
    system_prompt,
    user_prompt,
    candidate_ids,
    maximum_results,
):
    """Request provider-neutral structured recommendations."""
    schema = recommendation_schema(candidate_ids, maximum_results)
    return _generate_structured(
        settings,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=schema,
        schema_name="music_recommendations",
        max_output_tokens=RANKING_OUTPUT_TOKENS,
    )

"""Focused security and provider-contract tests for AI recommendations."""

import json
import math
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

import requests
from werkzeug.security import generate_password_hash


# Resolve Melodarr's module-level storage paths into a disposable directory
# when this file is run on its own.  The repository's existing backend suite
# follows the same pattern.
TEST_DATA = tempfile.TemporaryDirectory(prefix="melodarr-ai-tests-")
os.environ.update({
    "MELODARR_DATABASE": os.path.join(TEST_DATA.name, "melodarr.db"),
    "MELODARR_CACHE_DATABASE": os.path.join(
        TEST_DATA.name,
        "cache",
        "metadata.db",
    ),
    "MELODARR_SETTINGS": os.path.join(TEST_DATA.name, "settings.json"),
    "MELODARR_SECRET_KEY_FILE": os.path.join(
        TEST_DATA.name,
        "session-secret.key",
    ),
    "MELODARR_ARTWORK_CACHE": os.path.join(TEST_DATA.name, "artwork"),
})

from backend import ai_recommendations, listening_profiles
from backend.application import create_app
from backend.services import ai_providers
from backend.storage import (
    db,
    get_listening_profile,
    get_service,
    init_db,
    save_listening_profile,
    write_settings_file,
)


class Response:
    """Small requests.Response stand-in for provider adapter tests."""

    def __init__(
        self,
        payload=None,
        status_code=200,
        *,
        redirect=False,
        content=None,
        headers=None,
    ):
        self._payload = payload
        self.status_code = status_code
        self.is_redirect = redirect
        self.headers = headers or {}
        self._content = (
            content
            if content is not None
            else json.dumps(payload).encode("utf-8")
        )
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def structured_result(candidate_id="artist:artist-1"):
    return {"recommendations": [candidate_id]}


class ProviderContractTests(unittest.TestCase):
    """Keep every HTTP adapter aligned with its official structured API."""

    candidate_ids = ["artist:artist-1", "album:album-1"]

    def generate(self, settings):
        return ai_providers.generate_recommendations(
            settings,
            system_prompt="system rules",
            user_prompt='{"query":"something new"}',
            candidate_ids=self.candidate_ids,
            maximum_results=2,
        )

    @patch("backend.services.ai_providers.requests.post")
    def test_openai_responses_contract_and_heterogeneous_parser(self, post):
        expected = structured_result()
        post.return_value = Response({
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(expected)}
                    ],
                },
            ],
        })

        result = self.generate({
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "apiKey": "openai-secret",
        })

        self.assertEqual(result, expected)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer openai-secret",
        )
        self.assertEqual(
            kwargs["timeout"],
            (
                ai_providers.CONNECT_TIMEOUT_SECONDS,
                ai_providers.READ_TIMEOUT_SECONDS,
            ),
        )
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        body = kwargs["json"]
        self.assertFalse(body["store"])
        self.assertEqual(body["input"][0]["role"], "system")
        self.assertEqual(body["input"][1]["role"], "user")
        output_format = body["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertTrue(output_format["strict"])
        self.assertEqual(
            output_format["schema"],
            ai_providers.recommendation_schema(self.candidate_ids, 2),
        )
        recommendation_item = output_format["schema"]["properties"][
            "recommendations"
        ]["items"]
        self.assertEqual(recommendation_item["type"], "string")
        self.assertEqual(recommendation_item["enum"], self.candidate_ids)

    @patch("backend.services.ai_providers.requests.post")
    def test_anthropic_messages_contract_and_multipart_parser(self, post):
        expected = structured_result()
        encoded = json.dumps(expected)
        post.return_value = Response({
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": encoded[:18]},
                {"type": "text", "text": encoded[18:]},
            ],
        })

        result = self.generate({
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "apiKey": "claude-secret",
        })

        self.assertEqual(result, expected)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], "claude-secret")
        self.assertEqual(
            kwargs["headers"]["anthropic-version"],
            "2023-06-01",
        )
        body = kwargs["json"]
        self.assertEqual(body["system"], "system rules")
        self.assertEqual(body["messages"], [{
            "role": "user",
            "content": '{"query":"something new"}',
        }])
        self.assertEqual(
            body["output_config"]["format"],
            {
                "type": "json_schema",
                "schema": ai_providers.recommendation_schema(
                    self.candidate_ids,
                    2,
                ),
            },
        )
        self.assertNotIn("temperature", body)
        self.assertNotIn("top_p", body)
        self.assertNotIn("top_k", body)

    @patch("backend.services.ai_providers.requests.post")
    def test_gemini_generate_content_contract_and_multipart_parser(self, post):
        expected = structured_result()
        encoded = json.dumps(expected)
        post.return_value = Response({
            "candidates": [{
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"text": encoded[:22]},
                        {"text": encoded[22:]},
                    ]
                },
            }]
        })

        result = self.generate({
            "provider": "gemini",
            "model": "gemini-3.6-flash",
            "apiKey": "gemini-secret",
        })

        self.assertEqual(result, expected)
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.6-flash:generateContent",
        )
        self.assertEqual(
            kwargs["headers"]["x-goog-api-key"],
            "gemini-secret",
        )
        body = kwargs["json"]
        self.assertEqual(
            body["system_instruction"],
            {"parts": [{"text": "system rules"}]},
        )
        self.assertEqual(body["contents"], [{
            "role": "user",
            "parts": [{"text": '{"query":"something new"}'}],
        }])
        response_text = body["generationConfig"]["responseFormat"]["text"]
        self.assertEqual(response_text["mimeType"], "application/json")
        self.assertEqual(
            response_text["schema"],
            ai_providers.recommendation_schema(self.candidate_ids, 2),
        )
        self.assertNotIn("responseMimeType", body["generationConfig"])
        self.assertNotIn("responseSchema", body["generationConfig"])
        self.assertNotIn("responseJsonSchema", body["generationConfig"])
        self.assertNotIn("temperature", body["generationConfig"])
        self.assertNotIn("top_p", body["generationConfig"])
        self.assertNotIn("top_k", body["generationConfig"])

    @patch("backend.services.ai_providers.requests.post")
    def test_ollama_chat_contract_and_structured_parser(self, post):
        expected = structured_result()
        post.return_value = Response({
            "done": True,
            "message": {"content": json.dumps(expected)},
        })

        result = self.generate({
            "provider": "ollama",
            "model": "qwen3:8b",
            "baseUrl": "http://ollama.internal:11434",
        })

        self.assertEqual(result, expected)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://ollama.internal:11434/api/chat")
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/json"})
        self.assertEqual(
            kwargs["timeout"],
            (
                ai_providers.CONNECT_TIMEOUT_SECONDS,
                ai_providers.LOCAL_READ_TIMEOUT_SECONDS,
            ),
        )
        body = kwargs["json"]
        self.assertFalse(body["stream"])
        self.assertEqual(
            body["format"],
            ai_providers.recommendation_schema(self.candidate_ids, 2),
        )
        candidate_id_schema = body["format"]["properties"][
            "recommendations"
        ]["items"]
        self.assertEqual(candidate_id_schema["enum"], self.candidate_ids)
        self.assertEqual(
            [message["role"] for message in body["messages"]],
            ["system", "user"],
        )

    @patch("backend.services.ai_providers.requests.post")
    def test_lmstudio_chat_completions_contract_and_structured_parser(self, post):
        expected = structured_result()
        post.return_value = Response({
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(expected),
                },
            }],
        })

        result = self.generate({
            "provider": "lmstudio",
            "model": "glm-4.7-flash",
            "baseUrl": "http://lmstudio.internal:1234",
            "apiKey": "optional-local-token",
        })

        self.assertEqual(result, expected)
        args, kwargs = post.call_args
        self.assertEqual(
            args[0],
            "http://lmstudio.internal:1234/v1/chat/completions",
        )
        self.assertEqual(kwargs["headers"], {
            "Authorization": "Bearer optional-local-token",
            "Content-Type": "application/json",
        })
        self.assertEqual(
            kwargs["timeout"],
            (
                ai_providers.CONNECT_TIMEOUT_SECONDS,
                ai_providers.LOCAL_READ_TIMEOUT_SECONDS,
            ),
        )
        body = kwargs["json"]
        self.assertFalse(body["stream"])
        self.assertEqual(body["model"], "glm-4.7-flash")
        self.assertEqual(
            body["max_tokens"],
            ai_providers.RANKING_OUTPUT_TOKENS,
        )
        self.assertEqual(
            [message["role"] for message in body["messages"]],
            ["system", "user"],
        )
        response_format = body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(
            response_format["json_schema"]["schema"],
            ai_providers.recommendation_schema(self.candidate_ids, 2),
        )
        candidate_id_schema = response_format["json_schema"]["schema"][
            "properties"
        ]["recommendations"]["items"]
        self.assertEqual(candidate_id_schema["enum"], self.candidate_ids)

    def test_ranking_schema_never_requests_more_items_than_exist(self):
        schema = ai_providers.recommendation_schema(
            ["artist:one", "artist:two", "artist:one"],
            8,
        )

        recommendations = schema["properties"]["recommendations"]
        self.assertEqual(recommendations["maxItems"], 2)
        self.assertEqual(
            recommendations["items"],
            {
                "type": "string",
                "enum": ["artist:one", "artist:two"],
            },
        )

    @patch("backend.services.ai_providers.requests.post")
    def test_lmstudio_api_token_is_optional(self, post):
        expected = structured_result()
        post.return_value = Response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(expected)},
            }],
        })

        result = self.generate({
            "provider": "lmstudio",
            "model": "glm-4.7-flash",
            "baseUrl": "http://localhost:1234",
        })

        self.assertEqual(result, expected)
        self.assertEqual(
            post.call_args.kwargs["headers"],
            {"Content-Type": "application/json"},
        )

    @patch("backend.services.ai_providers.requests.post")
    def test_search_plan_uses_a_small_provider_neutral_schema(self, post):
        expected = {
            "entityTypes": ["artist"],
            "mustMatchTags": ["drill"],
            "discoveryTags": ["UK drill"],
            "seedArtists": [],
            "openEnded": False,
        }
        post.return_value = Response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(expected)},
            }],
        })

        result = ai_providers.generate_search_plan(
            {
                "provider": "lmstudio",
                "model": "glm-4.7-flash",
                "baseUrl": "http://localhost:1234",
            },
            system_prompt="plan rules",
            user_prompt='{"query":"new drill rapper"}',
        )

        self.assertEqual(result, expected)
        body = post.call_args.kwargs["json"]
        self.assertEqual(
            body["max_tokens"],
            ai_providers.SEARCH_PLAN_OUTPUT_TOKENS,
        )
        self.assertEqual(
            body["response_format"]["json_schema"]["name"],
            "music_search_plan",
        )
        self.assertEqual(
            body["response_format"]["json_schema"]["schema"],
            ai_providers.search_plan_schema(),
        )
        schema = ai_providers.search_plan_schema()
        self.assertEqual(
            schema["required"],
            [
                "entityTypes",
                "mustMatchTags",
                "discoveryTags",
                "seedArtists",
                "openEnded",
            ],
        )
        self.assertNotIn("tags", schema["properties"])

    @patch("backend.services.ai_providers.requests.post")
    def test_provider_statuses_that_cannot_be_used_are_rejected(self, post):
        cases = [
            (
                {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "apiKey": "key",
                },
                {
                    "status": "incomplete",
                    "output": [],
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            ),
            (
                {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "apiKey": "key",
                },
                {
                    "status": "completed",
                    "output": [{
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "No"}],
                    }],
                },
            ),
            (
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "apiKey": "key",
                },
                {"stop_reason": "max_tokens", "content": []},
            ),
            (
                {
                    "provider": "gemini",
                    "model": "gemini-3.6-flash",
                    "apiKey": "key",
                },
                {
                    "candidates": [{
                        "finishReason": "SAFETY",
                        "content": {"parts": []},
                    }]
                },
            ),
            (
                {
                    "provider": "lmstudio",
                    "model": "glm-4.7-flash",
                    "baseUrl": "http://localhost:1234",
                },
                {
                    "choices": [{
                        "finish_reason": "length",
                        "message": {"content": "{}"},
                    }],
                },
            ),
            (
                {
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "baseUrl": "http://localhost:11434",
                },
                {"done": False, "message": {}},
            ),
        ]

        for settings, payload in cases:
            with self.subTest(provider=settings["provider"], payload=payload):
                post.return_value = Response(payload)
                with self.assertRaises(ai_providers.AIResponseError):
                    self.generate(settings)

    @patch("backend.services.ai_providers.requests.post")
    def test_transport_failures_are_sanitized_and_not_retried(self, post):
        post.side_effect = requests.Timeout(
            "raw transport detail containing openai-secret"
        )

        with self.assertRaisesRegex(
            ai_providers.AIUpstreamError,
            "^The AI provider timed out\\.$",
        ) as raised:
            self.generate({
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "apiKey": "openai-secret",
            })

        self.assertNotIn("openai-secret", str(raised.exception))
        self.assertEqual(post.call_count, 1)

    @patch("backend.services.ai_providers.requests.post")
    def test_oversized_provider_response_is_bounded_and_closed(self, post):
        response = Response(
            content=b"{" + b" " * ai_providers.MAX_RESPONSE_BYTES + b"}",
        )
        post.return_value = response

        with self.assertRaisesRegex(
            ai_providers.AIResponseError,
            "^The AI provider returned an oversized response\\.$",
        ):
            self.generate({
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "apiKey": "openai-secret",
            })

        self.assertTrue(response.closed)


class ListeningSourceContractTests(unittest.TestCase):
    @patch("backend.services.listenbrainz.requests.get")
    def test_listenbrainz_statistics_are_bounded_and_normalized(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "payload": {
                "artists": [{
                    "artist_mbid": "artist-id",
                    "artist_name": "Artist",
                    "listen_count": 12,
                }]
            }
        }
        get.return_value = response

        result = ai_recommendations.listenbrainz.top_artists(
            "private listener",
            count=1000,
        )

        self.assertEqual(result[0]["artist_name"], "Artist")
        get.assert_called_once()
        self.assertIn(
            "/stats/user/private%20listener/artists",
            get.call_args.args[0],
        )
        self.assertEqual(
            get.call_args.kwargs["params"],
            {"count": 1000, "range": "all_time"},
        )
        self.assertEqual(get.call_args.kwargs["timeout"], 15)
        response.raise_for_status.assert_called_once()

    @patch("backend.services.listenbrainz.requests.get")
    def test_missing_listenbrainz_statistics_are_an_empty_history(self, get):
        get.return_value = Mock(status_code=204)

        result = ai_recommendations.listenbrainz.top_release_groups("listener")

        self.assertEqual(result, [])


class AIConfigurationTests(unittest.TestCase):
    def test_saved_cloud_key_is_retained_replaced_and_explicitly_cleared(self):
        previous = {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "apiKey": "old-secret",
        }

        retained = ai_providers.normalize_saved_settings(
            {"provider": "openai", "model": "gpt-5.6-sol"},
            previous,
        )
        replaced = ai_providers.normalize_saved_settings(
            {
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "apiKey": "new-secret",
            },
            previous,
        )
        cleared = ai_providers.normalize_saved_settings(
            {
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "clearApiKey": True,
            },
            previous,
        )

        self.assertEqual(retained["apiKey"], "old-secret")
        self.assertEqual(replaced["apiKey"], "new-secret")
        self.assertNotIn("apiKey", cleared)
        self.assertFalse(ai_providers.public_status(cleared)["configured"])

    def test_public_status_never_exposes_a_cloud_api_key(self):
        status = ai_providers.public_status({
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "apiKey": "never-return-this",
        })

        self.assertTrue(status["configured"])
        self.assertNotIn("never-return-this", json.dumps(status))
        self.assertNotIn("apiKey", status)

    def test_ollama_has_no_fake_universal_model_default(self):
        catalog = {
            provider["id"]: provider
            for provider in ai_providers.provider_catalog()
        }
        self.assertEqual(catalog["ollama"]["defaultModel"], "")
        with self.assertRaises(ai_providers.AIConfigurationError):
            ai_providers.normalize_saved_settings({
                "provider": "ollama",
                "model": "",
                "baseUrl": "http://localhost:11434",
            })

    def test_lmstudio_requires_an_explicit_model_but_not_an_api_token(self):
        catalog = {
            provider["id"]: provider
            for provider in ai_providers.provider_catalog()
        }
        self.assertEqual(catalog["lmstudio"]["defaultModel"], "")
        self.assertFalse(catalog["lmstudio"]["requiresApiKey"])
        self.assertTrue(catalog["lmstudio"]["supportsApiKey"])
        with self.assertRaises(ai_providers.AIConfigurationError):
            ai_providers.normalize_saved_settings({
                "provider": "lmstudio",
                "model": "",
                "baseUrl": "http://localhost:1234",
            })

        saved = ai_providers.normalize_saved_settings({
            "provider": "lmstudio",
            "model": "glm-4.7-flash",
            "baseUrl": "http://localhost:1234/v1/",
        })
        self.assertEqual(saved, {
            "provider": "lmstudio",
            "model": "glm-4.7-flash",
            "baseUrl": "http://localhost:1234",
        })
        self.assertTrue(ai_providers.public_status(saved)["configured"])

    def test_public_status_never_exposes_the_ollama_network_location(self):
        status = ai_providers.public_status({
            "provider": "ollama",
            "model": "qwen3:8b",
            "baseUrl": "http://private-ollama.internal:11434",
        })

        self.assertTrue(status["configured"])
        self.assertNotIn("private-ollama.internal", json.dumps(status))
        self.assertNotIn("baseUrl", status)

    def test_ollama_url_is_reduced_to_a_credential_free_origin(self):
        self.assertEqual(
            ai_providers.normalize_ollama_base_url(
                "http://ollama.internal:11434/api/"
            ),
            "http://ollama.internal:11434",
        )
        invalid = [
            "ftp://ollama.internal",
            "http://user:password@ollama.internal:11434",
            "http://ollama.internal:11434/models",
            "http://ollama.internal:11434?token=secret",
            "http://ollama.internal:11434/#fragment",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ai_providers.AIConfigurationError):
                    ai_providers.normalize_ollama_base_url(value)

    def test_lmstudio_url_is_reduced_to_a_credential_free_origin(self):
        self.assertEqual(
            ai_providers.normalize_lmstudio_base_url(
                "http://lmstudio.internal:1234/v1/"
            ),
            "http://lmstudio.internal:1234",
        )
        invalid = [
            "ftp://lmstudio.internal",
            "http://user:password@lmstudio.internal:1234",
            "http://lmstudio.internal:1234/api/v0",
            "http://lmstudio.internal:1234?token=secret",
            "http://lmstudio.internal:1234/#fragment",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ai_providers.AIConfigurationError):
                    ai_providers.normalize_lmstudio_base_url(value)


class GroundedRecommendationTests(unittest.TestCase):
    def test_private_history_is_minimized_before_a_prompt_is_built(self):
        history_row = {
            "kind": "release-group",
            "mbid": "private-request-mbid",
            "name": "  Album   Name ",
            "artist_name": "Artist Name",
            "release_type": "Album",
            "release_date": "2025-01-01",
            "created_at": 1_754_000_000,
            "history_key": "plex-history-key",
            "plex_email": "listener@example.com",
            "plex_id": "private-plex-id",
        }
        with (
            patch.object(
                ai_recommendations,
                "get_request_history",
                return_value=[history_row],
            ) as get_history,
            patch.object(
                ai_recommendations,
                "get_service",
                return_value={"token": "plex-token"},
            ),
            patch.object(
                ai_recommendations.recommendation_engine,
                "plex_taste_profile",
                return_value=(
                    [{
                        "name": "Played Artist",
                        "playCount": 12,
                        "ratingKey": "private-rating-key",
                        "lastPlayedAt": 1_754_000_001,
                    }],
                    {"dream pop": 4.0},
                ),
            ),
        ):
            rows, requests_context = ai_recommendations._request_context(41)
            played_artists, tags = ai_recommendations._plex_context(41)

        _system, user_prompt = ai_recommendations._prompts(
            "new music",
            requests_context,
            played_artists,
            tags,
            [{
                "candidateId": "artist:public-musicbrainz-id",
                "kind": "artist",
                "name": "Candidate",
                "artist": "",
                "type": "Group",
                "year": "",
                "source": "ListenBrainz",
            }],
            5,
        )

        get_history.assert_called_once_with(
            41,
            limit=ai_recommendations.MAX_HISTORY_ITEMS,
        )
        self.assertEqual(len(rows), 1)
        prompt = json.loads(user_prompt)
        self.assertEqual(prompt["tasteProfile"]["recentRequests"], [{
            "kind": "release-group",
            "name": "Album Name",
            "artist": "Artist Name",
        }])
        self.assertEqual(prompt["tasteProfile"]["topPlayedArtists"], [{
            "name": "Played Artist",
            "playCount": 12,
        }])
        self.assertEqual(prompt["tasteProfile"]["topTags"], ["dream pop"])
        serialized = json.dumps(prompt)
        for private_value in (
            "private-request-mbid",
            "plex-history-key",
            "listener@example.com",
            "private-plex-id",
            "private-rating-key",
            "plex-token",
            "1754000000",
            "1754000001",
        ):
            self.assertNotIn(private_value, serialized)

    def test_listening_context_merges_connected_sources_for_taste_and_novelty(self):
        user = {
            "id": 41,
            "lastfm_username": "last-user",
            "listenbrainz_username": "listen-user",
        }
        with (
            patch.object(
                ai_recommendations,
                "_plex_context",
                return_value=(
                    [{"name": "Shared Artist", "playCount": 3}],
                    ["electronic"],
                ),
            ),
            patch.object(
                ai_recommendations,
                "_cached_taste_tags",
                return_value=["dream pop"],
            ),
            patch.object(
                ai_recommendations,
                "_lastfm_context",
                return_value=(
                    [{"name": "Shared Artist", "playCount": 12}],
                    ["hip hop"],
                    {"lastfm-id"},
                    {"shared artist"},
                    {("album artist", "heard album")},
                ),
            ),
            patch.object(
                ai_recommendations,
                "_listenbrainz_context",
                return_value=(
                    [{"name": "Listen Artist", "playCount": 7}],
                    {"listen-id"},
                    {"listen artist"},
                    {"heard-release-group"},
                    {("listen artist", "heard release")},
                ),
            ),
        ):
            context = ai_recommendations._listening_context(user)

        self.assertEqual(
            context["artists"],
            [
                {"name": "Shared Artist", "playCount": 12},
                {"name": "Listen Artist", "playCount": 7},
            ],
        )
        self.assertEqual(
            context["tags"],
            ["electronic", "dream pop", "hip hop"],
        )
        self.assertEqual(
            context["heardArtistIds"],
            {"lastfm-id", "listen-id"},
        )
        self.assertEqual(
            context["heardAlbumIds"],
            {"heard-release-group"},
        )

    def test_candidate_pool_is_user_scoped_deduplicated_and_excludes_owned(self):
        owned_artist = "11111111-1111-4111-8111-111111111111"
        new_artist = "22222222-2222-4222-8222-222222222222"
        requested_album = "33333333-3333-4333-8333-333333333333"
        new_album = "44444444-4444-4444-8444-444444444444"
        cached = {
            "artists": [
                {"id": owned_artist, "name": "Already owned"},
                {"id": new_artist, "name": "New Artist"},
                {"id": new_artist, "name": "Duplicate"},
            ],
            "albums": [
                {"id": requested_album, "name": "Already requested"},
                {
                    "id": new_album,
                    "name": "New Album",
                    "artist": "Album Artist",
                },
            ],
            "tagRows": [{
                "albums": [{"id": new_album, "name": "Duplicate Album"}]
            }],
        }
        request_rows = [{"kind": "release-group", "mbid": requested_album}]
        with (
            patch.object(
                ai_recommendations,
                "get_recommendation_cache",
                return_value={
                    "value": json.dumps(cached),
                    "refreshed_at": 1234,
                },
            ) as get_cache,
            patch.object(
                ai_recommendations,
                "_library_exclusions",
                return_value=({owned_artist}, set()),
            ),
        ):
            trusted, prompt_items, refreshed_at = (
                ai_recommendations._candidate_pool(73, request_rows)
            )

        get_cache.assert_called_once_with(73)
        self.assertEqual(
            set(trusted),
            {f"artist:{new_artist}", f"album:{new_album}"},
        )
        self.assertEqual(
            [item["candidateId"] for item in prompt_items],
            [f"artist:{new_artist}", f"album:{new_album}"],
        )
        self.assertEqual(refreshed_at, 1234)

    def test_search_plan_preserves_a_targeted_genre_and_artist_result_type(self):
        plan = ai_recommendations._validated_plan({
            "entityTypes": ["artist", "artist"],
            "mustMatchTags": ["drill", "drill"],
            "discoveryTags": ["UK drill"],
            "seedArtists": [],
            "openEnded": False,
        })

        self.assertEqual(plan["entityTypes"], ["artist"])
        self.assertEqual(plan["tags"], ["drill", "UK drill"])
        self.assertFalse(plan["openEnded"])
        reordered = ai_recommendations._prioritize_explicit_tags(
            {
                **plan,
                "mustMatchTags": ["hip hop"],
                "discoveryTags": ["drill"],
                "tags": ["hip hop", "drill"],
            },
            "Who's a new drill rapper?",
        )
        self.assertEqual(reordered["tags"], ["drill", "hip hop"])
        self.assertEqual(reordered["mustMatchTags"], ["drill"])
        self.assertEqual(reordered["discoveryTags"], ["hip hop"])
        with self.assertRaises(ai_providers.AIResponseError):
            ai_recommendations._validated_plan({
                "entityTypes": ["artist"],
                "mustMatchTags": [],
                "discoveryTags": [],
                "seedArtists": [],
                "openEnded": False,
            })

    def test_model_search_terms_are_escaped_before_musicbrainz_queries(self):
        escaped = ai_recommendations._lucene_phrase(
            'drill" OR artist:*'
        )

        self.assertTrue(escaped.startswith('"'))
        self.assertTrue(escaped.endswith('"'))
        self.assertIn('\\"', escaped)
        self.assertIn("\\:", escaped)
        self.assertIn("\\*", escaped)
        self.assertNotIn('artist:*', escaped)

    @patch.object(ai_recommendations, "get_lastfm_api_key", return_value="")
    @patch.object(
        ai_recommendations,
        "_musicbrainz_recent_artist_candidates",
        return_value=[],
    )
    @patch.object(ai_recommendations.musicbrainz, "search")
    def test_targeted_pool_contains_only_query_retrieved_unheard_artists(
        self,
        musicbrainz_search,
        _recent_candidates,
        _lastfm_key,
    ):
        heard_id = "55555555-5555-4555-8555-555555555555"
        drill_id = "66666666-6666-4666-8666-666666666666"
        generic_id = "77777777-7777-4777-8777-777777777777"
        musicbrainz_search.side_effect = [
            {
                "artists": [
                    {
                        "id": heard_id,
                        "name": "Already Heard",
                        "type": "Person",
                        "score": 100,
                    },
                    {
                        "id": drill_id,
                        "name": "New Drill Artist",
                        "type": "Person",
                        "score": 98,
                    },
                ]
            },
            {
                "artists": [{
                    "id": generic_id,
                    "name": "Generic Rap Artist",
                    "type": "Person",
                    "score": 100,
                }]
            },
        ]
        exclusions = {
            "artistIds": {heard_id},
            "artistNames": {"already heard"},
            "albumIds": set(),
            "albumNames": set(),
        }

        trusted, prompt_items, sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["artist"],
                    "tags": ["drill", "hip hop"],
                    "seedArtists": [],
                    "openEnded": False,
                },
                exclusions,
            )
        )

        self.assertEqual(
            musicbrainz_search.call_args_list,
            [
                call('tag:"drill"', "artist", priority="interactive"),
                call('tag:"hip hop"', "artist", priority="interactive"),
            ],
        )
        self.assertEqual(set(trusted), {f"artist:{drill_id}"})
        self.assertEqual(trusted[f"artist:{drill_id}"]["matchedTags"], ["drill"])
        self.assertEqual(prompt_items[0]["matchedTags"], ["drill"])
        self.assertEqual(sources, ["MusicBrainz tag search"])

    @patch.object(
        ai_recommendations,
        "get_lastfm_api_key",
        return_value="shared-key",
    )
    @patch.object(ai_recommendations.musicbrainz, "get")
    @patch.object(ai_recommendations.musicbrainz, "search")
    @patch.object(ai_recommendations.lastfm, "get_public")
    def test_lastfm_tag_result_requires_musicbrainz_identity_verification(
        self,
        lastfm_get,
        musicbrainz_search,
        musicbrainz_get,
        _lastfm_key,
    ):
        artist_id = "11111111-1111-4111-8111-111111111111"
        musicbrainz_search.return_value = {"artists": []}
        lastfm_get.return_value = {
            "topartists": {
                "artist": [{
                    "mbid": artist_id,
                    "name": "Verified Drill Artist",
                }]
            }
        }
        musicbrainz_get.return_value = {
            "id": artist_id,
            "name": "Verified Drill Artist",
            "type": "Person",
        }
        exclusions = {
            "artistIds": set(),
            "artistNames": set(),
            "albumIds": set(),
            "albumNames": set(),
        }

        trusted, _prompt_items, sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["artist"],
                    "tags": ["drill"],
                    "seedArtists": [],
                    "openEnded": False,
                },
                exclusions,
            )
        )

        lastfm_get.assert_called_once_with(
            "tag.gettopartists",
            "shared-key",
            tag="drill",
            limit=20,
        )
        musicbrainz_get.assert_called_once_with(
            f"/artist/{artist_id}",
            "aliases+tags",
            priority="interactive",
        )
        self.assertEqual(set(trusted), {f"artist:{artist_id}"})
        self.assertEqual(
            trusted[f"artist:{artist_id}"]["matchedTags"],
            ["drill"],
        )
        self.assertEqual(sources, ["Last.fm tag search"])

    @patch.object(
        ai_recommendations,
        "get_lastfm_api_key",
        return_value="shared-key",
    )
    @patch.object(
        ai_recommendations,
        "_musicbrainz_recent_artist_candidates",
        return_value=[],
    )
    @patch.object(ai_recommendations.musicbrainz, "search")
    @patch.object(ai_recommendations.lastfm, "get_public")
    def test_lastfm_artist_without_mbid_is_resolved_by_exact_name(
        self,
        lastfm_get,
        musicbrainz_search,
        _recent_candidates,
        _lastfm_key,
    ):
        resolved_id = "22222222-2222-4222-8222-222222222222"
        musicbrainz_search.side_effect = [
            {"artists": []},
            {
                "artists": [{
                    "id": resolved_id,
                    "name": "Name Resolved Artist",
                    "type": "Person",
                    "score": 100,
                }]
            },
        ]
        lastfm_get.return_value = {
            "topartists": {
                "artist": [{
                    "mbid": "",
                    "name": "Name Resolved Artist",
                }]
            }
        }

        trusted, _prompt_items, _sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["artist"],
                    "tags": ["drill"],
                    "seedArtists": [],
                    "openEnded": False,
                },
                {
                    "artistIds": set(),
                    "artistNames": set(),
                    "albumIds": set(),
                    "albumNames": set(),
                },
            )
        )

        self.assertEqual(set(trusted), {f"artist:{resolved_id}"})
        self.assertEqual(
            musicbrainz_search.call_args_list[1],
            call(
                'artist:"Name Resolved Artist"',
                "artist",
                priority="interactive",
            ),
        )

    def test_hard_constraints_come_from_the_query_not_model_ordering(self):
        plan = ai_recommendations._prioritize_explicit_tags(
            ai_recommendations._validated_plan({
                "entityTypes": ["artist"],
                "mustMatchTags": ["hip hop"],
                "discoveryTags": ["drill", "UK drill"],
                "seedArtists": [],
                "openEnded": False,
            }),
            "Who's a new drill rapper?",
        )

        self.assertEqual(plan["mustMatchTags"], ["drill"])
        self.assertEqual(plan["discoveryTags"], ["hip hop", "UK drill"])
        self.assertEqual(plan["tags"][0], "drill")

    @patch.object(ai_recommendations, "get_lastfm_api_key", return_value="")
    @patch.object(
        ai_recommendations,
        "_musicbrainz_recent_artist_candidates",
        return_value=[],
    )
    @patch.object(ai_recommendations, "_musicbrainz_tag_candidates")
    def test_open_ended_directions_do_not_hard_filter_on_the_first_tag(
        self,
        tag_candidates,
        _recent_candidates,
        _lastfm_key,
    ):
        country_id = "88888888-8888-4888-8888-888888888888"
        electronic_id = "99999999-9999-4999-8999-999999999999"
        tag_candidates.side_effect = [
            [{
                "id": country_id,
                "name": "Country Bridge",
                "score": 90,
                "matchedTags": ["country"],
                "similarTo": [],
                "recommendationSource": "MusicBrainz tag search",
            }],
            [{
                "id": electronic_id,
                "name": "Electronic Bridge",
                "score": 90,
                "matchedTags": ["electronic"],
                "similarTo": [],
                "recommendationSource": "MusicBrainz tag search",
            }],
        ]
        plan = ai_recommendations._prioritize_explicit_tags(
            ai_recommendations._validated_plan({
                "entityTypes": ["artist"],
                "mustMatchTags": ["country"],
                "discoveryTags": ["electronic"],
                "seedArtists": [],
                "openEnded": True,
            }),
            "Take one part of my taste somewhere surprising",
        )

        trusted, prompt_items, _sources = (
            ai_recommendations._query_candidate_pool(
                plan,
                {
                    "artistIds": set(),
                    "artistNames": set(),
                    "albumIds": set(),
                    "albumNames": set(),
                },
            )
        )

        self.assertEqual(plan["mustMatchTags"], [])
        self.assertEqual(
            set(trusted),
            {f"artist:{country_id}", f"artist:{electronic_id}"},
        )
        self.assertEqual(len(prompt_items), 2)

    @patch.object(ai_recommendations.musicbrainz, "search")
    def test_musicbrainz_candidates_require_uuid_identities(self, search):
        verified_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        search.return_value = {
            "artists": [
                {"id": "not-an-mbid", "name": "Malformed", "score": 100},
                {"id": verified_id, "name": "Verified", "score": 99},
            ]
        }

        rows = ai_recommendations._musicbrainz_tag_candidates(
            "drill",
            "artist",
            {
                "artistIds": set(),
                "artistNames": set(),
                "albumIds": set(),
                "albumNames": set(),
            },
        )

        self.assertEqual([row["id"] for row in rows], [verified_id])

    @patch.object(ai_recommendations, "get_lastfm_api_key", return_value="key")
    @patch.object(
        ai_recommendations,
        "_lastfm_artist_candidates",
        return_value=([], 1),
    )
    @patch.object(
        ai_recommendations,
        "_verify_seed_artists",
        return_value=([], 1),
    )
    def test_hallucinated_bridge_seed_cannot_surface_directly(
        self,
        verify_seeds,
        lastfm_candidates,
        _lastfm_key,
    ):
        trusted, prompt_items, _sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["artist"],
                    "mustMatchTags": [],
                    "discoveryTags": [],
                    "tags": [],
                    "seedArtists": ["Definitely Imaginary Artist"],
                    "openEnded": False,
                },
                {
                    "artistIds": set(),
                    "artistNames": set(),
                    "albumIds": set(),
                    "albumNames": set(),
                },
            )
        )

        verify_seeds.assert_called_once_with(["Definitely Imaginary Artist"])
        lastfm_candidates.assert_called_once_with([], [], "key")
        self.assertEqual(trusted, {})
        self.assertEqual(prompt_items, [])

    @patch.object(ai_recommendations, "get_lastfm_api_key", return_value="")
    @patch.object(ai_recommendations, "_musicbrainz_tag_candidates")
    def test_newer_equally_relevant_candidate_leads_server_shortlist(
        self,
        tag_candidates,
        _lastfm_key,
    ):
        older_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        newer_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        tag_candidates.return_value = [
            {
                "id": older_id,
                "name": "Older Album",
                "artist": "Artist",
                "date": "2000-01-01",
                "score": 90,
                "matchedTags": ["drill"],
                "similarTo": [],
                "recommendationSource": "MusicBrainz tag search",
            },
            {
                "id": newer_id,
                "name": "Newer Album",
                "artist": "Artist",
                "date": "2026-01-01",
                "score": 90,
                "matchedTags": ["drill"],
                "similarTo": [],
                "recommendationSource": "MusicBrainz tag search",
            },
        ]

        trusted, prompt_items, _sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["album"],
                    "mustMatchTags": ["drill"],
                    "discoveryTags": [],
                    "tags": ["drill"],
                    "seedArtists": [],
                    "openEnded": False,
                },
                {
                    "artistIds": set(),
                    "artistNames": set(),
                    "albumIds": set(),
                    "albumNames": set(),
                },
            )
        )

        self.assertEqual(next(iter(trusted)), f"album:{newer_id}")
        self.assertEqual(prompt_items[0]["candidateId"], f"album:{newer_id}")
        self.assertGreater(
            trusted[f"album:{newer_id}"]["score"],
            trusted[f"album:{older_id}"]["score"],
        )

    @patch.object(ai_recommendations, "get_lastfm_api_key", return_value="")
    @patch.object(ai_recommendations, "_musicbrainz_tag_candidates")
    def test_server_scored_ranker_shortlist_is_bounded_to_twenty_four(
        self,
        tag_candidates,
        _lastfm_key,
    ):
        tag_candidates.return_value = [
            {
                "id": f"00000000-0000-4000-8000-{index:012d}",
                "name": f"Album {index}",
                "artist": "Artist",
                "date": "2026",
                "score": 100 - index,
                "matchedTags": ["drill"],
                "similarTo": [],
                "recommendationSource": "MusicBrainz tag search",
            }
            for index in range(30)
        ]

        trusted, prompt_items, _sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["album"],
                    "mustMatchTags": ["drill"],
                    "discoveryTags": [],
                    "tags": ["drill"],
                    "seedArtists": [],
                    "openEnded": False,
                },
                {
                    "artistIds": set(),
                    "artistNames": set(),
                    "albumIds": set(),
                    "albumNames": set(),
                },
            )
        )

        self.assertEqual(len(trusted), 24)
        self.assertEqual(len(prompt_items), 24)
        self.assertEqual(
            next(iter(trusted)),
            "album:00000000-0000-4000-8000-000000000000",
        )

    def test_relevance_can_outweigh_recency_and_missing_dates_are_neutral(self):
        strong_old = ai_recommendations._candidate_scores({
            "score": 100,
            "date": "2000",
            "matchedTags": ["drill"],
        }, current_year=2026)
        weak_new = ai_recommendations._candidate_scores({
            "score": 70,
            "date": "2026",
            "matchedTags": ["drill"],
        }, current_year=2026)
        unknown = ai_recommendations._candidate_scores({
            "score": 90,
            "matchedTags": ["drill"],
        }, current_year=2026)

        self.assertGreater(strong_old[3], weak_new[3])
        self.assertEqual(unknown[1], ai_recommendations.UNKNOWN_RECENCY_SCORE)

    @patch.object(ai_recommendations, "get_lastfm_api_key", return_value="")
    @patch.object(
        ai_recommendations,
        "_musicbrainz_recent_artist_candidates",
        side_effect=requests.ConnectionError("recent search unavailable"),
    )
    @patch.object(ai_recommendations, "_musicbrainz_tag_candidates")
    def test_recent_artist_search_is_bounded_and_failure_keeps_relevant_results(
        self,
        tag_candidates,
        recent_candidates,
        _lastfm_key,
    ):
        artist_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        tag_candidates.return_value = [{
            "id": artist_id,
            "name": "Still Available",
            "score": 90,
            "matchedTags": ["one"],
            "similarTo": [],
            "recommendationSource": "MusicBrainz tag search",
        }]

        trusted, _prompt_items, _sources = (
            ai_recommendations._query_candidate_pool(
                {
                    "entityTypes": ["artist"],
                    "mustMatchTags": [],
                    "discoveryTags": ["one", "two", "three", "four"],
                    "tags": ["one", "two", "three", "four"],
                    "seedArtists": [],
                    "openEnded": True,
                },
                {
                    "artistIds": set(),
                    "artistNames": set(),
                    "albumIds": set(),
                    "albumNames": set(),
                },
            )
        )

        self.assertEqual(recent_candidates.call_count, 2)
        self.assertIn(f"artist:{artist_id}", trusted)

    def test_targeted_empty_retrieval_returns_no_filler_and_skips_ranking(self):
        user = {"id": 92, "username": "listener"}
        listening = {
            "artists": [{"name": "Favorite", "playCount": 20}],
            "tags": ["hip hop"],
            "heardArtistIds": set(),
            "heardArtistNames": {"favorite"},
            "heardAlbumIds": set(),
            "heardAlbumNames": set(),
        }
        exclusions = {
            "artistIds": set(),
            "artistNames": {"favorite"},
            "albumIds": set(),
            "albumNames": set(),
        }
        with (
            patch.object(
                ai_recommendations,
                "_request_context",
                return_value=([], []),
            ),
            patch.object(
                listening_profiles,
                "stored_profile_context",
                return_value=listening,
            ),
            patch.object(
                ai_recommendations,
                "_plex_context",
                side_effect=AssertionError("AI request must not fetch Plex history"),
            ) as live_plex,
            patch.object(
                ai_recommendations,
                "_lastfm_context",
                side_effect=AssertionError("AI request must not fetch Last.fm history"),
            ) as live_lastfm,
            patch.object(
                ai_recommendations,
                "_listenbrainz_context",
                side_effect=AssertionError(
                    "AI request must not fetch ListenBrainz history"
                ),
            ) as live_listenbrainz,
            patch.object(
                ai_recommendations,
                "_novelty_exclusions",
                return_value=exclusions,
            ),
            patch.object(
                ai_recommendations,
                "_query_candidate_pool",
                return_value=({}, [], ["MusicBrainz tag search"]),
            ) as query_pool,
            patch.object(ai_recommendations, "_candidate_pool") as cached_pool,
            patch.object(
                ai_providers,
                "resolve_settings",
                return_value={
                    "provider": "lmstudio",
                    "model": "local-model",
                    "baseUrl": "http://localhost:1234",
                },
            ),
            patch.object(
                ai_providers,
                "generate_search_plan",
                return_value={
                    "entityTypes": ["artist"],
                    "mustMatchTags": ["drill"],
                    "discoveryTags": [],
                    "seedArtists": [],
                    "openEnded": False,
                },
            ),
            patch.object(
                ai_providers,
                "generate_recommendations",
            ) as rank,
        ):
            result = ai_recommendations.recommend(
                user,
                query="Who's a new drill rapper I can listen to?",
                limit=5,
                saved_settings={"provider": "lmstudio"},
            )

        query_pool.assert_called_once_with(
            {
                "entityTypes": ["artist"],
                "mustMatchTags": ["drill"],
                "discoveryTags": [],
                "tags": ["drill"],
                "seedArtists": [],
                "openEnded": False,
            },
            exclusions,
        )
        cached_pool.assert_not_called()
        rank.assert_not_called()
        live_plex.assert_not_called()
        live_lastfm.assert_not_called()
        live_listenbrainz.assert_not_called()
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["grounding"]["queryTags"], ["drill"])
        self.assertEqual(result["grounding"]["candidateCount"], 0)

    def test_targeted_candidates_are_personalized_in_a_second_bounded_pass(self):
        user = {"id": 93, "username": "listener"}
        listening = {
            "artists": [{"name": "Favorite Rapper", "playCount": 42}],
            "tags": ["hip hop"],
            "heardArtistIds": set(),
            "heardArtistNames": {"favorite rapper"},
            "heardAlbumIds": set(),
            "heardAlbumNames": set(),
            "promptProfile": {
                "v": 1,
                "a": [["Favorite Rapper", 80, 90, 75, 42]],
                "g": [["hip hop", 100]],
                "st": [],
                "mo": [],
                "d": [50, 20, 30, ["Favorite Rapper"], []],
                "rq": [],
                "nx": [1, 0],
                "neg": [],
                "src": [["lf", "fresh", 0, 92]],
            },
            "profileGeneratedAt": 1234,
            "profileStatus": "ready",
        }
        exclusions = {
            "artistIds": set(),
            "artistNames": {"favorite rapper"},
            "albumIds": set(),
            "albumNames": set(),
        }
        candidate_id = "artist:new-drill"
        trusted = {
            candidate_id: {
                "id": "new-drill",
                "name": "New Drill Artist",
                "kind": "artist",
                "matchedTags": ["drill"],
                "recommendationSource": "MusicBrainz tag search",
            }
        }
        prompt_items = [{
            "candidateId": candidate_id,
            "kind": "artist",
            "name": "New Drill Artist",
            "artist": "",
            "type": "",
            "year": "",
            "source": "MusicBrainz tag search",
            "matchedTags": ["drill"],
            "similarTo": [],
        }]
        settings = {
            "provider": "lmstudio",
            "model": "local-model",
            "baseUrl": "http://localhost:1234",
        }
        plan = {
            "entityTypes": ["artist"],
            "mustMatchTags": ["drill"],
            "discoveryTags": [],
            "seedArtists": [],
            "openEnded": False,
        }
        expected_plan = {**plan, "tags": ["drill"]}
        with (
            patch.object(
                ai_recommendations,
                "_request_context",
                return_value=([], []),
            ),
            patch.object(
                listening_profiles,
                "stored_profile_context",
                return_value=listening,
            ),
            patch.object(
                ai_recommendations,
                "_novelty_exclusions",
                return_value=exclusions,
            ),
            patch.object(
                ai_recommendations,
                "_query_candidate_pool",
                return_value=(
                    trusted,
                    prompt_items,
                    ["MusicBrainz tag search"],
                ),
            ),
            patch.object(
                ai_providers,
                "resolve_settings",
                return_value=settings,
            ),
            patch.object(
                ai_providers,
                "generate_search_plan",
                return_value=plan,
            ),
            patch.object(
                ai_providers,
                "generate_recommendations",
                return_value=structured_result(candidate_id),
            ) as rank,
        ):
            result = ai_recommendations.recommend(
                user,
                query="new drill rapper",
                limit=3,
                saved_settings={"provider": "lmstudio"},
            )

        rank_prompt = json.loads(rank.call_args.kwargs["user_prompt"])
        self.assertEqual(rank_prompt["retrievalPlan"], expected_plan)
        self.assertEqual(rank_prompt["tasteProfile"], listening["promptProfile"])
        self.assertEqual(rank_prompt["requestedCount"], 1)
        self.assertEqual(
            list(rank.call_args.kwargs["candidate_ids"]),
            [candidate_id],
        )
        self.assertEqual(rank.call_args.kwargs["maximum_results"], 1)
        self.assertEqual(result["recommendations"][0]["id"], "new-drill")
        self.assertEqual(
            result["recommendations"][0]["reason"],
            (
                "Matched the verified drill tag through "
                "MusicBrainz tag search."
            ),
        )

    def test_only_server_supplied_candidate_ids_can_reach_the_response(self):
        candidates = {
            "artist:trusted": {
                "id": "trusted",
                "name": "Trusted Artist",
                "kind": "artist",
                "recommendationSource": "ListenBrainz",
            }
        }
        result = ai_recommendations._validated_selections(
            {
                "recommendations": [
                    "artist:invented",
                    "artist:trusted",
                    "artist:trusted",
                ]
            },
            candidates,
            5,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "trusted")
        self.assertEqual(
            result[0]["reason"],
            (
                "Selected from your verified ListenBrainz recommendations "
                "for this request."
            ),
        )
        self.assertNotIn("confidence", result[0])
        self.assertNotIn("artist:invented", json.dumps(result))

    def test_display_reason_uses_only_trusted_similarity_and_provenance(self):
        result = ai_recommendations._validated_selections(
            structured_result("artist:trusted"),
            {
                "artist:trusted": {
                    "id": "trusted",
                    "name": "Trusted Artist",
                    "kind": "artist",
                    "type": "Similar to A Known Favorite",
                    "recommendationSource": "Plex history · Last.fm",
                }
            },
            1,
        )

        self.assertEqual(
            result[0]["reason"],
            (
                "Similar to A Known Favorite; surfaced through "
                "Plex history · Last.fm."
            ),
        )

    def test_display_reason_uses_verified_query_tag_not_model_prose(self):
        result = ai_recommendations._validated_selections(
            {"recommendations": ["artist:trusted"]},
            {
                "artist:trusted": {
                    "id": "trusted",
                    "name": "Trusted Artist",
                    "kind": "artist",
                    "matchedTags": ["drill"],
                    "recommendationSource": "MusicBrainz tag search",
                }
            },
            1,
        )

        self.assertEqual(
            result[0]["reason"],
            (
                "Matched the verified drill tag through "
                "MusicBrainz tag search."
            ),
        )
        self.assertNotIn("biography", result[0]["reason"])

    def test_display_reason_can_cite_verified_recent_release_evidence(self):
        result = ai_recommendations._validated_selections(
            {"recommendations": ["artist:trusted"]},
            {
                "artist:trusted": {
                    "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                    "name": "Trusted Artist",
                    "kind": "artist",
                    "matchedTags": ["drill"],
                    "recentRelease": {
                        "id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                        "title": "Recent Record",
                        "date": "2026-03-01",
                    },
                    "recommendationSource": (
                        "MusicBrainz recent release search"
                    ),
                }
            },
            1,
        )

        self.assertIn('"Recent Record" (2026)', result[0]["reason"])
        self.assertNotIn("model", result[0]["reason"].casefold())

    def test_recommendation_orchestration_uses_only_the_signed_in_user_id(self):
        user = {"id": 91, "username": "private-listener"}
        listening = {
            "artists": [],
            "tags": [],
            "heardArtistIds": set(),
            "heardArtistNames": set(),
            "heardAlbumIds": set(),
            "heardAlbumNames": set(),
            "promptProfile": {
                "v": 1,
                "a": [],
                "g": [],
                "st": [],
                "mo": [],
                "d": [0, 0, 0, [], []],
                "rq": [],
                "nx": [0, 0],
                "neg": [],
                "src": [],
            },
            "profileGeneratedAt": 1234,
            "profileStatus": "ready",
        }
        exclusions = {
            "artistIds": set(),
            "artistNames": set(),
            "albumIds": set(),
            "albumNames": set(),
        }
        trusted = {
            "artist:one": {
                "id": "one",
                "name": "Artist One",
                "kind": "artist",
            }
        }
        with (
            patch.object(
                ai_recommendations,
                "_request_context",
                return_value=([], []),
            ) as request_context,
            patch.object(
                listening_profiles,
                "stored_profile_context",
                return_value=listening,
            ) as listening_context,
            patch.object(
                ai_recommendations,
                "_novelty_exclusions",
                return_value=exclusions,
            ),
            patch.object(
                ai_recommendations,
                "_candidate_pool",
                return_value=(
                    trusted,
                    [{
                        "candidateId": "artist:one",
                        "kind": "artist",
                        "name": "Artist One",
                        "artist": "",
                        "type": "",
                        "year": "",
                        "source": "",
                    }],
                    1234,
                ),
            ) as candidate_pool,
            patch.object(
                ai_providers,
                "resolve_settings",
                return_value={
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "apiKey": "secret",
                },
            ),
            patch.object(
                ai_providers,
                "generate_search_plan",
                return_value={
                    "entityTypes": ["artist"],
                    "tags": [],
                    "seedArtists": [],
                    "openEnded": True,
                },
            ),
            patch.object(
                ai_providers,
                "generate_recommendations",
                return_value=structured_result("artist:one"),
            ),
        ):
            result = ai_recommendations.recommend(
                user,
                query="something different",
                limit=4,
                saved_settings={"provider": "openai"},
            )

        request_context.assert_called_once_with(91)
        listening_context.assert_called_once_with(91)
        candidate_pool.assert_called_once_with(91, [], exclusions)
        self.assertEqual(result["recommendations"][0]["id"], "one")


class ListeningProfileTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with db() as connection:
            connection.execute("DELETE FROM listening_profiles")
            connection.execute("DELETE FROM request_history")
            connection.execute("DELETE FROM users")
            self.user_one = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at, lastfm_username) "
                "VALUES ('profile-one', 'hash', 'user', 1, 'private-lastfm-one')"
            ).lastrowid
            self.user_two = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) "
                "VALUES ('profile-two', 'hash', 'user', 1)"
            ).lastrowid
        write_settings_file({})

    @staticmethod
    def source(name="Favorite Artist"):
        source = listening_profiles._empty_source()
        source.update({
            "artists": [{
                "name": name,
                "mbid": "11111111-1111-1111-1111-111111111111",
                "short": 100,
                "medium": 80,
                "long": 65,
                "count": 42,
            }],
            "genres": [{"name": "drill", "weight": 100}],
            "heardArtistIds": [
                "11111111-1111-1111-1111-111111111111",
            ],
            "heardArtistNames": [name],
        })
        return source

    def test_storage_and_reads_are_strictly_user_scoped(self):
        first = {
            "schemaVersion": listening_profiles.SCHEMA_VERSION,
            "sourceData": {},
            "promptProfile": {"v": 1, "a": [["First Artist", 1, 2, 3, 4]]},
            "novelty": {},
            "affinities": {},
        }
        second = {
            "schemaVersion": listening_profiles.SCHEMA_VERSION,
            "sourceData": {},
            "promptProfile": {"v": 1, "a": [["Second Artist", 1, 2, 3, 4]]},
            "novelty": {},
            "affinities": {},
        }
        save_listening_profile(self.user_one, first, refreshed_at=100)
        save_listening_profile(self.user_two, second, refreshed_at=200)

        self.assertIn("First Artist", get_listening_profile(self.user_one)["value"])
        self.assertNotIn("Second Artist", get_listening_profile(self.user_one)["value"])
        self.assertIn("Second Artist", get_listening_profile(self.user_two)["value"])

    def test_prompt_projection_is_compact_deterministic_and_private(self):
        user = {
            "id": self.user_one,
            "plex_id": "private-plex-id",
            "lastfm_username": "private-lastfm-user",
            "listenbrainz_username": "private-listenbrainz-user",
            "plex_email": "listener@example.com",
        }
        with (
            patch.object(
                listening_profiles,
                "get_service",
                return_value={
                    "url": "http://private-plex",
                    "token": "private-plex-token",
                },
            ),
            patch.object(
                listening_profiles,
                "get_lastfm_api_key",
                return_value="private-lastfm-key",
            ),
            patch.object(
                listening_profiles,
                "_plex_source",
                return_value=self.source("Plex Artist"),
            ),
            patch.object(
                listening_profiles,
                "_lastfm_source",
                return_value=self.source("Last.fm Artist"),
            ),
            patch.object(
                listening_profiles,
                "_listenbrainz_source",
                return_value=self.source("ListenBrainz Artist"),
            ),
        ):
            profile, errors = listening_profiles.build_user_profile(
                user,
                now=1_800_000_000,
            )

        self.assertEqual(errors, [])
        compact = listening_profiles.compact_prompt_json(profile)
        self.assertLessEqual(
            len(compact),
            listening_profiles.MAX_PROMPT_PROFILE_CHARS,
        )
        self.assertLessEqual(
            math.ceil(len(compact) / 3),
            listening_profiles.MAX_PROMPT_PROFILE_APPROX_TOKENS,
        )
        self.assertEqual(
            compact,
            listening_profiles.compact_prompt_json(profile),
        )
        self.assertEqual(profile["novelty"]["negativePreferences"], [])
        self.assertGreater(profile["novelty"]["heardArtistIds"], [])
        for private_value in (
            "private-plex-id",
            "private-lastfm-user",
            "private-listenbrainz-user",
            "listener@example.com",
            "private-plex-token",
            "private-lastfm-key",
            "http://private-plex",
            "1800000000",
        ):
            self.assertNotIn(private_value, compact)

    def test_partial_outage_keeps_the_last_good_source_slice_as_stale(self):
        user = {
            "id": self.user_one,
            "lastfm_username": "private-lastfm-one",
        }
        with (
            patch.object(
                listening_profiles,
                "get_lastfm_api_key",
                return_value="key",
            ),
            patch.object(
                listening_profiles,
                "_lastfm_source",
                return_value=self.source("Durable Favorite"),
            ),
        ):
            listening_profiles.refresh_user_profile(user, now=1_000)

        with (
            patch.object(
                listening_profiles,
                "get_lastfm_api_key",
                return_value="key",
            ),
            patch.object(
                listening_profiles,
                "_lastfm_source",
                side_effect=requests.Timeout("provider offline"),
            ),
        ):
            retry = listening_profiles.refresh_user_profile(user, now=2_000)

        self.assertTrue(retry)
        stored = json.loads(get_listening_profile(self.user_one)["value"])
        self.assertEqual(stored["sources"]["lastfm"]["status"], "stale")
        self.assertEqual(
            stored["sourceData"]["lastfm"]["artists"][0]["name"],
            "Durable Favorite",
        )
        self.assertIn(
            "Durable Favorite",
            json.dumps(stored["promptProfile"]),
        )
        self.assertNotIn("provider offline", json.dumps(stored))

    def test_unexpected_failed_build_does_not_replace_last_good_profile(self):
        original = {
            "schemaVersion": listening_profiles.SCHEMA_VERSION,
            "sourceData": {},
            "promptProfile": {"v": 1, "a": [["Last Good", 1, 1, 1, 1]]},
            "novelty": {},
            "affinities": {},
        }
        save_listening_profile(self.user_one, original, refreshed_at=100)
        row_before = get_listening_profile(self.user_one)
        with patch.object(
            listening_profiles,
            "build_user_profile",
            side_effect=RuntimeError("unexpected"),
        ):
            with self.assertRaises(RuntimeError):
                listening_profiles.refresh_user_profile(
                    {"id": self.user_one},
                    now=200,
                )
        row_after = get_listening_profile(self.user_one)
        self.assertEqual(row_after["value"], row_before["value"])
        self.assertEqual(row_after["refreshed_at"], row_before["refreshed_at"])
        self.assertIn("unexpected", row_after["last_error"])

    def test_first_run_fallback_uses_requests_without_network_history(self):
        with db() as connection:
            connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, artist_name, created_at) "
                "VALUES (?, 'release-group', 'album-id', 'Known Album', "
                "'Known Artist', 10)",
                (self.user_one,),
            )
        with (
            patch.object(
                listening_profiles,
                "_plex_source",
                side_effect=AssertionError("must not call Plex"),
            ),
            patch.object(
                listening_profiles,
                "_lastfm_source",
                side_effect=AssertionError("must not call Last.fm"),
            ),
            patch.object(
                listening_profiles,
                "_listenbrainz_source",
                side_effect=AssertionError("must not call ListenBrainz"),
            ),
        ):
            fallback = listening_profiles.fallback_profile_context(self.user_one)

        self.assertEqual(fallback["profileStatus"], "pending")
        self.assertIn(
            ("known artist", "known album"),
            fallback["heardAlbumNames"],
        )
        self.assertEqual(
            fallback["promptProfile"]["rq"],
            [["r", "Known Album", "Known Artist"]],
        )


class AIRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "ai-test-secret"})
        self.client = self.app.test_client()
        with db() as connection:
            connection.execute("DELETE FROM plex_auth_flows")
            connection.execute("DELETE FROM pending_lidarr_searches")
            connection.execute("DELETE FROM recommendation_cache")
            connection.execute("DELETE FROM listening_profiles")
            connection.execute("DELETE FROM plex_listens")
            connection.execute("DELETE FROM request_history")
            connection.execute("DELETE FROM account_invitations")
            connection.execute("DELETE FROM users")
        write_settings_file({})
        ai_recommendations._active_users.clear()

    def register_admin(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "a-secure-password"},
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def create_and_login_member(self):
        with db() as connection:
            member_id = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "member",
                    generate_password_hash("member-password"),
                    "user",
                    time.time(),
                ),
            ).lastrowid
        client = self.app.test_client()
        response = client.post(
            "/api/auth/login",
            json={"username": "member", "password": "member-password"},
        )
        self.assertEqual(response.status_code, 200)
        return client, member_id, response.get_json()["csrfToken"]

    def test_ai_routes_require_authentication_and_csrf(self):
        self.assertEqual(self.client.get("/api/ai/status").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/ai/recommendations",
                json={"prompt": "new music"},
            ).status_code,
            403,
        )
        admin = self.register_admin()
        self.assertEqual(
            self.client.post(
                "/api/settings/ai",
                json={
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "apiKey": "secret",
                },
            ).status_code,
            403,
        )
        self.assertTrue(admin["csrfToken"])

    def test_admin_configuration_and_status_never_return_the_api_key(self):
        token = self.register_admin()["csrfToken"]
        secret = "sk-private-never-return"
        response = self.client.post(
            "/api/settings/ai",
            headers={"X-CSRF-Token": token},
            json={
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "apiKey": secret,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertEqual(get_service("ai")["apiKey"], secret)
        status = self.client.get("/api/ai/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.get_json()["configured"])
        self.assertNotIn(secret, status.get_data(as_text=True))
        self.assertNotIn("apiKey", status.get_json())
        settings = self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        ai_settings = settings.get_json()["ai"]
        self.assertEqual(ai_settings["provider"], "openai")
        self.assertEqual(ai_settings["model"], "gpt-5.6-sol")
        self.assertTrue(ai_settings["apiKeyConfigured"])
        self.assertNotIn(secret, settings.get_data(as_text=True))
        self.assertNotIn("apiKey", ai_settings)

    def test_only_admin_can_configure_ai(self):
        self.register_admin()
        member_client, _member_id, member_token = self.create_and_login_member()

        response = member_client.post(
            "/api/settings/ai",
            headers={"X-CSRF-Token": member_token},
            json={
                "provider": "ollama",
                "model": "qwen3:8b",
                "baseUrl": "http://localhost:11434",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(get_service("ai"))

    def test_recommendation_route_ignores_caller_supplied_user_identity(self):
        admin = self.register_admin()
        member_client, member_id, member_token = self.create_and_login_member()
        response_payload = {
            "provider": "ollama",
            "model": "qwen3:8b",
            "query": "new music",
            "candidateRefreshedAt": 1234,
            "grounding": {
                "historyItemCount": 0,
                "playedArtistCount": 0,
                "candidateCount": 1,
            },
            "recommendations": [],
        }
        with patch(
            "backend.routes.ai.ai_recommendations.recommend",
            return_value=response_payload,
        ) as recommend:
            response = member_client.post(
                "/api/ai/recommendations",
                headers={"X-CSRF-Token": member_token},
                json={
                    "prompt": "  new   music ",
                    "limit": 5,
                    "userId": admin["id"],
                    "username": "owner",
                },
            )

        self.assertEqual(response.status_code, 200)
        signed_in_user = recommend.call_args.args[0]
        self.assertEqual(signed_in_user["id"], member_id)
        self.assertNotEqual(signed_in_user["id"], admin["id"])
        self.assertEqual(recommend.call_args.kwargs["query"], "new music")
        self.assertEqual(recommend.call_args.kwargs["limit"], 5)

    def test_catalog_retrieval_outage_is_reported_as_upstream_failure(self):
        token = self.register_admin()["csrfToken"]
        with patch(
            "backend.routes.ai.ai_recommendations.recommend",
            side_effect=ai_recommendations.AIRecommendationUnavailable(
                "Music discovery sources could not be reached. Try again shortly."
            ),
        ):
            response = self.client.post(
                "/api/ai/recommendations",
                headers={"X-CSRF-Token": token},
                json={"prompt": "new drill rapper"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "Music discovery sources could not be reached. Try again shortly.",
        )

    def test_user_status_hides_ollama_url_while_admin_settings_redacts_it(self):
        token = self.register_admin()["csrfToken"]
        base_url = "http://private-ollama.internal:11434"
        response = self.client.post(
            "/api/settings/ai",
            headers={"X-CSRF-Token": token},
            json={
                "provider": "ollama",
                "model": "qwen3:8b",
                "baseUrl": base_url,
            },
        )
        self.assertEqual(response.status_code, 200)

        status = self.client.get("/api/ai/status")
        self.assertEqual(status.status_code, 200)
        self.assertNotIn(base_url, status.get_data(as_text=True))
        self.assertNotIn("baseUrl", status.get_json())

        settings = self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        ai_settings = settings.get_json()["ai"]
        self.assertEqual(ai_settings["provider"], "ollama")
        self.assertEqual(ai_settings["model"], "qwen3:8b")
        self.assertEqual(ai_settings["baseUrl"], base_url)
        self.assertFalse(ai_settings["apiKeyConfigured"])
        self.assertNotIn("apiKey", ai_settings)

    def test_lmstudio_url_and_optional_token_are_redacted_by_role(self):
        token = self.register_admin()["csrfToken"]
        base_url = "http://private-lmstudio.internal:1234"
        api_token = "lm-studio-private-token"
        response = self.client.post(
            "/api/settings/ai",
            headers={"X-CSRF-Token": token},
            json={
                "provider": "lmstudio",
                "model": "glm-4.7-flash",
                "baseUrl": f"{base_url}/v1",
                "apiKey": api_token,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(api_token, response.get_data(as_text=True))

        status = self.client.get("/api/ai/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.get_json()["configured"])
        self.assertNotIn(base_url, status.get_data(as_text=True))
        self.assertNotIn("baseUrl", status.get_json())
        self.assertNotIn(api_token, status.get_data(as_text=True))

        settings = self.client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        ai_settings = settings.get_json()["ai"]
        self.assertEqual(ai_settings["provider"], "lmstudio")
        self.assertEqual(ai_settings["model"], "glm-4.7-flash")
        self.assertEqual(ai_settings["baseUrl"], base_url)
        self.assertTrue(ai_settings["apiKeyConfigured"])
        self.assertNotIn(api_token, settings.get_data(as_text=True))
        self.assertNotIn("apiKey", ai_settings)

    def test_ai_routes_reject_non_object_json_and_invalid_prompt_limits(self):
        token = self.register_admin()["csrfToken"]
        headers = {"X-CSRF-Token": token}

        for body in ([], "text", 1, None):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/ai/recommendations",
                    headers=headers,
                    json=body,
                )
                self.assertEqual(response.status_code, 400)

        for body in (
            {"prompt": ""},
            {"prompt": "music", "limit": True},
            {"prompt": "music", "limit": 0},
            {"prompt": "music", "limit": ai_recommendations.MAX_RESULTS + 1},
        ):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/ai/recommendations",
                    headers=headers,
                    json=body,
                )
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()

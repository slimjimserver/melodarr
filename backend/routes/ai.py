"""Authenticated AI recommendation and administrator configuration routes."""

from flask import Blueprint, jsonify

if __package__ == "backend.routes":
    from .. import ai_recommendations
    from ..responses import api_error, request_json_object
    from ..security import admin_required, current_user, login_required
    from ..services import ai_providers
    from ..storage import get_service, save_service
else:  # Support the existing ``python backend/app.py`` entry point.
    import ai_recommendations
    from responses import api_error, request_json_object
    from security import admin_required, current_user, login_required
    from services import ai_providers
    from storage import get_service, save_service


blueprint = Blueprint("ai", __name__)


@blueprint.get("/api/ai/status")
@login_required
def status():
    """Return AI readiness without exposing stored or environment API keys."""
    return jsonify(ai_providers.public_status(get_service("ai")))


@blueprint.post("/api/ai/recommendations")
@login_required
def recommendations():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    user = current_user()
    try:
        query = ai_recommendations.validate_query(values.get("prompt"))
        limit = ai_recommendations.validate_limit(values.get("limit"))
        with ai_recommendations.user_request_slot(user["id"]):
            payload = ai_recommendations.recommend(
                user,
                query=query,
                limit=limit,
                saved_settings=get_service("ai"),
            )
        return jsonify(payload)
    except ai_recommendations.AIRequestInProgress as exc:
        return api_error(str(exc), 409)
    except ai_recommendations.AIRecommendationPending as exc:
        return api_error(str(exc), 409)
    except ai_recommendations.AIRecommendationUnavailable as exc:
        return api_error(str(exc), 502)
    except ai_recommendations.AIRecommendationError as exc:
        return api_error(str(exc))
    except ai_providers.AIConfigurationError as exc:
        return api_error(str(exc), 503)
    except ai_providers.AIUpstreamError as exc:
        return api_error(str(exc), 502)
    except ai_providers.AIResponseError as exc:
        return api_error(str(exc), 502)


@blueprint.post("/api/settings/ai")
@admin_required
def configure():
    """Save server-side AI configuration without echoing credentials."""
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    try:
        saved = ai_providers.normalize_saved_settings(
            values,
            get_service("ai"),
        )
    except ai_providers.AIConfigurationError as exc:
        return api_error(str(exc))
    save_service("ai", saved)
    provider_name = ai_providers.PROVIDERS[saved["provider"]]["name"]
    return jsonify({
        "message": f"{provider_name} settings saved.",
        "ai": ai_providers.administrative_status(saved),
    })

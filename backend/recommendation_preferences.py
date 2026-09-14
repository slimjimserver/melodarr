"""Private, durable controls for recommendation taste inputs."""
import json
from uuid import UUID

if __package__:
    from .storage import db, _wake_recommendations
else:
    from storage import db, _wake_recommendations

MODES = {"familiar", "balanced", "discovery"}


def preferences_for(user_id):
    with db() as connection:
        row = connection.execute("SELECT * FROM recommendation_preferences WHERE user_id=?", (user_id,)).fetchone()
    return {"mode": row["mode"] if row else "balanced",
            "starterArtists": json.loads(row["artists_json"]) if row else [],
            "revision": row["revision"] if row else 0}


def save_preferences(user_id, values):
    if (not isinstance(values, dict) or not isinstance(values.get("mode"), str)
            or values["mode"] not in MODES):
        raise ValueError("Choose familiar, balanced, or discovery recommendations.")
    artists = values.get("starterArtists", [])
    if not isinstance(artists, list) or len(artists) > 5:
        raise ValueError("Choose up to five favorite artists.")
    cleaned, seen = [], set()
    for artist in artists:
        if not isinstance(artist, dict) or not isinstance(artist.get("name"), str):
            raise ValueError("Choose an artist from the search results.")
        try:
            mbid = str(UUID(artist.get("id", "")))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Choose an artist from the search results.") from None
        name = artist["name"].strip()
        if not name or len(name) > 200 or mbid in seen:
            raise ValueError("Choose up to five different artists from the search results.")
        seen.add(mbid)
        cleaned.append({"id": mbid, "name": name})
    with db() as connection:
        connection.execute("""INSERT INTO recommendation_preferences (user_id, mode, artists_json, revision)
            VALUES (?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET
            mode=excluded.mode, artists_json=excluded.artists_json, revision=revision+1""",
            (user_id, values["mode"], json.dumps(cleaned, ensure_ascii=False)))
        connection.execute("DELETE FROM recommendation_cache WHERE user_id=?", (user_id,))
    _wake_recommendations()
    return preferences_for(user_id)


def set_request_influence(user_id, request_id, included):
    if type(request_id) is not int or request_id < 1 or type(included) is not bool:
        raise ValueError("A request ID and a boolean recommendation preference are required.")
    with db() as connection:
        row = connection.execute("SELECT id FROM request_history WHERE id=? AND user_id=?", (request_id, user_id)).fetchone()
        if row is None:
            return False
        connection.execute("UPDATE request_history SET use_for_recommendations=? WHERE id=? AND user_id=?",
                           (int(included), request_id, user_id))
        connection.execute("""INSERT INTO recommendation_preferences (user_id, revision) VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET revision=revision+1""", (user_id,))
        connection.execute("DELETE FROM recommendation_cache WHERE user_id=?", (user_id,))
    _wake_recommendations()
    return True

"""Conservative candidate discovery after the Apple chart's exact MB match fails.

Only an Album/EP with an identical punctuation-insensitive original or recognized
base title, a credible artist identity, MusicBrainz relevance >= 80, a total
score >= 80, and >= 15 points over the next eligible candidate is automatic.
Scores: title 60 (original) or 50 (known edition base); artist 25 (complete
credited/canonical name) or 18 (primary Apple credit); MB relevance 0..10;
Album/EP 5; matching release date 0..5. Dates never penalize an edition of an
older release group. Untyped groups and Singles are never automatic matches.
"""

import re
import unicodedata
from datetime import date

import requests


# An explicit set of observed Apple version labels. Parentheses outside this
# list remain part of the title. The soundtrack label is removed for discovery
# only; the full original title is still used for candidate scoring.
_KNOWN_SUFFIX = re.compile(
    r"\s*(?:\((?:Video Version|spilled|The Til Dawn Edition|"
    r"Original Broadway Cast Recording|Director['’]s Cut|"
    r"Soundtrack from the Netflix Film|deluxe(?: edition| version)?|"
    r"expanded(?: edition)?|(?:\d+(?:st|nd|rd|th) )?anniversary(?: edition)?|"
    r"remaster(?:ed)?(?: \d{4})?)\)|\s+-\s+EP)$",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])")
MIN_MB_SCORE = 80
MIN_TOTAL_SCORE = 80
MIN_MARGIN = 15


def identity(value):
    return "".join(character for character in unicodedata.normalize("NFKC", str(value or "")).casefold()
                   if character.isalnum())


def credited_name(group, *, canonical=False):
    return "".join(
        str(((credit.get("artist") or {}).get("name") if canonical else credit.get("name"))
            or credit.get("name") or (credit.get("artist") or {}).get("name") or "")
        + str(credit.get("joinphrase") or "")
        for credit in group.get("artist-credit") or []
    )


def _primary_apple_artist(value):
    # A list of performers may be credited to one primary MusicBrainz artist.
    # Do not split ordinary comma-containing solo names such as Tyler, the Creator.
    if "," in value and "&" in value:
        return value.split(",", 1)[0].strip()
    return value


def _base_title(value):
    return _KNOWN_SUFFIX.sub("", value).strip()


def artist_identity(group, artist, *, verified_artist_ids=(), allow_primary=False):
    """Return (points, evidence); aliases/MBIDs apply only to one-artist credits."""
    full = identity(artist)
    credited = identity(credited_name(group))
    canonical = identity(credited_name(group, canonical=True))
    if full and full == credited:
        return 25, "credited_name"
    if full and full == canonical:
        return 25, "canonical_name"
    credits = group.get("artist-credit") or []
    if len(credits) == 1 and isinstance(credits[0], dict):
        mb_artist = credits[0].get("artist") or {}
        if mb_artist.get("id") and mb_artist["id"] in verified_artist_ids:
            return 25, "artist_mbid"
        if any(alias.get("type") == "Artist name" and identity(alias.get("name")) == full
               for alias in mb_artist.get("aliases") or []):
            return 25, "artist_alias"
    if allow_primary:
        primary = identity(_primary_apple_artist(artist))
        if primary != full and primary in {credited, canonical}:
            return 18, "primary_apple_credit"
    return 0, "none"


def _query_plan(title, artist):
    base = _base_title(title)
    primary = _primary_apple_artist(artist)
    query_title = _CAMEL_BOUNDARY.sub(" ", base)
    if query_title != title or primary != artist:
        query = ('releasegroup:"' + query_title.replace('"', '').replace("\\", "")
                 + '" AND artist:"' + primary.replace('"', '').replace("\\", "") + '"')
        interpretation = ("recognized_qualifier" if base != title else "original_title")
        if query_title != base:
            interpretation += "+split_compound"
        if primary != artist:
            interpretation += "+primary_artist"
        return [{"query": query, "interpretation": interpretation, "plainSearch": False}]
    return [{"query": f"{title} {artist}", "interpretation": "broader_text",
             "plainSearch": True}]


def _date_bonus(source_date, group_date):
    try:
        source = date.fromisoformat(str(source_date)[:10])
        group = date.fromisoformat(str(group_date)[:10])
    except ValueError:
        return 0
    days = abs((source - group).days)
    return 5 if days <= 30 else 2 if days <= 365 else 0


def score_candidate(group, *, title, artist, source_date="", verified_artist_ids=()):
    original = identity(title)
    base = identity(_base_title(title))
    candidate_title = identity(group.get("title"))
    title_score = 60 if candidate_title == original else 50 if base != original and candidate_title == base else 0
    artist_score, artist_evidence = artist_identity(
        group, artist, verified_artist_ids=verified_artist_ids, allow_primary=True)
    try:
        mb_score = max(0, min(100, int(group.get("score") or 0)))
    except (TypeError, ValueError):
        mb_score = 0
    release_type = group.get("primary-type") or ""
    typed = release_type in {"Album", "EP"}
    date_score = _date_bonus(source_date, group.get("first-release-date"))
    total = title_score + artist_score + mb_score // 10 + (5 if typed else 0) + date_score
    return {
        "id": group.get("id", ""), "title": group.get("title", ""),
        "artistCredit": credited_name(group), "canonicalArtist": credited_name(group, canonical=True),
        "releaseType": release_type, "firstReleaseDate": group.get("first-release-date", ""),
        "musicBrainzScore": mb_score, "titleScore": title_score,
        "artistScore": artist_score, "artistEvidence": artist_evidence,
        "dateScore": date_score, "finalScore": total,
        "eligible": bool(group.get("id") and typed and title_score and artist_score
                         and mb_score >= MIN_MB_SCORE),
    }


def resolve(item, exact_groups, search):
    """Return (selected group or None, detailed trace, unresolved classification)."""
    title = str(item.get("name") or "").strip()
    artist = str(item.get("artistName") or "").strip()
    groups = {group["id"]: group for group in exact_groups if group.get("id")}
    verified_ids = {((group.get("artist-credit") or [{}])[0].get("artist") or {}).get("id")
                    for group in exact_groups
                    if identity(group.get("title")) == identity(title)
                    and identity(credited_name(group)) == identity(artist)
                    and len(group.get("artist-credit") or []) == 1}
    verified_ids.discard(None)
    if len(verified_ids) != 1:
        verified_ids.clear()
    trace = {"sourceTitle": title, "sourceArtist": artist,
             "queries": [], "candidates": [], "candidateMbids": [],
             "bestScore": 0, "runnerUpScore": 0, "decision": "no_acceptable_candidate"}
    for planned in _query_plan(title, artist):
        query_record = dict(planned)
        try:
            response = search(planned["query"], "album", priority="background",
                              plain_search=planned["plainSearch"], limit=25)
        except (ValueError, requests.RequestException) as exc:
            query_record["errorType"] = type(exc).__name__
            trace["queries"].append(query_record)
            continue
        found = response.get("release-groups", [])
        query_record["resultCount"] = len(found)
        trace["queries"].append(query_record)
        groups.update({group["id"]: group for group in found if group.get("id")})

    scored = [score_candidate(group, title=title, artist=artist,
                              source_date=item.get("releaseDate") or "",
                              verified_artist_ids=verified_ids)
              for group in groups.values()]
    scored.sort(key=lambda candidate: (-candidate["finalScore"], candidate["id"]))
    trace["candidates"] = scored
    trace["candidateMbids"] = [candidate["id"] for candidate in scored]
    eligible = [candidate for candidate in scored if candidate["eligible"]]
    trace["bestScore"] = eligible[0]["finalScore"] if eligible else 0
    trace["runnerUpScore"] = eligible[1]["finalScore"] if len(eligible) > 1 else 0
    if eligible and eligible[0]["finalScore"] >= MIN_TOTAL_SCORE:
        if len(eligible) > 1 and eligible[0]["finalScore"] - eligible[1]["finalScore"] < MIN_MARGIN:
            trace["decision"] = "ambiguous"
            return None, trace, "ambiguous"
        trace["decision"] = "auto_matched"
        trace["selectedMbid"] = eligible[0]["id"]
        return groups[eligible[0]["id"]], trace, "matched"
    if any(candidate["titleScore"] and candidate["artistScore"]
           and candidate["releaseType"] not in {"Album", "EP"} for candidate in scored):
        return None, trace, "invalid_release_type"
    return None, trace, "no_match"

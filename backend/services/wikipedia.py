"""Wikipedia lead text from exact MusicBrainz Wikipedia/Wikidata relationships."""

import re
from urllib.parse import quote, unquote, urlsplit

import requests

if __package__ == "backend.services":
    from ..config import USER_AGENT
else:
    from config import USER_AGENT


def _get(url, **params):
    response = requests.get(
        url, params={"format": "json", **params}, headers={"User-Agent": USER_AGENT},
        timeout=(3.05, 10), allow_redirects=False,
    )
    response.raise_for_status()
    value = response.json()
    if 300 <= response.status_code < 400 or not isinstance(value, dict) or "error" in value:
        raise requests.RequestException("Wikipedia returned an API error")
    return value


def _page(url):
    parsed = urlsplit(str(url or ""))
    # Build API destinations from an allowlisted language host, never a supplied URL.
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None
    match = re.fullmatch(r"([a-z][a-z0-9-]{1,14})(?:\.m)?\.wikipedia\.org", parsed.hostname or "")
    if not match or not parsed.path.startswith("/wiki/"):
        return None
    title = unquote(parsed.path[6:]).replace("_", " ")
    if not title or ":" in title or "|" in title:
        return None
    return match[1], title


def bio(relations):
    urls = [str((relation.get("url") or {}).get("resource") or "") for relation in relations or []]
    pages = {page for url in urls if (page := _page(url))}
    english = {page for page in pages if page[0] == "en"}
    if english:
        pages = english
    if not pages:
        items = set()
        for url in urls:
            parsed = urlsplit(url)
            if parsed.hostname in {"wikidata.org", "www.wikidata.org"}:
                match = re.fullmatch(r"/(?:wiki|entity)/(Q[1-9][0-9]*)/?", parsed.path)
                if match:
                    items.add(match[1])
        if len(items) != 1:
            return None
        item = next(iter(items))
        value = _get("https://www.wikidata.org/w/api.php", action="wbgetentities", ids=item, props="sitelinks", sitefilter="enwiki")
        title = (((value.get("entities") or {}).get(item) or {}).get("sitelinks") or {}).get("enwiki", {}).get("title")
        if title:
            pages = {("en", title)}
    if len(pages) != 1:
        return None
    language, title = next(iter(pages))
    value = _get(
        f"https://{language}.wikipedia.org/w/api.php", action="query", prop="extracts|info|pageprops",
        titles=title, redirects=1, exintro=1, explaintext=1, exchars=1000, inprop="url", formatversion=2,
    )
    pages = (value.get("query") or {}).get("pages") or []
    if len(pages) != 1 or "missing" in pages[0] or "disambiguation" in (pages[0].get("pageprops") or {}):
        return None
    page = pages[0]
    text = " ".join(str(page.get("extract") or "").split())
    if not text:
        return None
    return {
        "text": text[:1000], "source": "wikipedia",
        "sourceUrl": f"https://{language}.wikipedia.org/wiki/{quote(str(page.get('title') or title).replace(' ', '_'))}",
    }

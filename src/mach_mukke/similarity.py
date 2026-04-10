import json
import re
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pylast


def normalize_track_key(artist: str, title: str) -> str:
    return f"{normalize_artist(artist)} - {normalize_title(title)}"


def normalize_artist(artist: str) -> str:
    return artist.strip().lower()


def normalize_title(title: str) -> str:
    base = title.strip().lower()
    base = base.replace("&", "and")
    base = base.replace("/", " ")
    base = base.replace("_", " ")
    base = re.sub(r"\s+", " ", base)
    base = re.sub(r"\s*\([^)]*\)", "", base)
    base = re.sub(r"\s*\[[^]]*\]", "", base)
    base = re.sub(r"\s*\{[^}]*\}", "", base)
    base = re.sub(
        r"\b(live|remaster(ed)?|remix|version|edit|mix|acoustic|mono|stereo|radio|demo|deluxe|explicit|clean)\b",
        "",
        base,
    )
    base = re.sub(r"\s+", " ", base)
    return base.strip()


def clean_track_for_lookup(artist: str, title: str) -> tuple[str, str]:
    cleaned_artist = artist.strip()
    cleaned_title = title.strip()
    if cleaned_artist:
        prefix = f"{cleaned_artist} - "
        if cleaned_title.lower().startswith(prefix.lower()):
            cleaned_title = cleaned_title[len(prefix) :].strip()
    cleaned_title = re.sub(r"\s*[\(\[].*?[\)\]]\s*", " ", cleaned_title)
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip()
    return cleaned_artist, cleaned_title


def track_lookup_variants(artist: str, title: str) -> list[tuple[str, str]]:
    base_artist, base_title = clean_track_for_lookup(artist, title)
    variants: list[tuple[str, str]] = [(base_artist, base_title)]

    primary_artist = re.split(
        r"\s*(?:,|&| feat\.?| ft\.?)\s*", base_artist, maxsplit=1
    )[0]
    if primary_artist and primary_artist != base_artist:
        variants.append((primary_artist, base_title))

    stripped_title = re.sub(
        r"\s*[-–—]\s*(feat\.?|ft\.?)\s+.*$", "", base_title, flags=re.IGNORECASE
    ).strip()
    stripped_title = re.sub(
        r"\s*(feat\.?|ft\.?)\s+.*$", "", stripped_title, flags=re.IGNORECASE
    ).strip()
    if stripped_title and stripped_title != base_title:
        variants.append((base_artist, stripped_title))
        if primary_artist and primary_artist != base_artist:
            variants.append((primary_artist, stripped_title))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for variant_artist, variant_title in variants:
        key = normalize_track_key(variant_artist, variant_title)
        if not variant_artist or not variant_title or key in seen:
            continue
        seen.add(key)
        unique.append((variant_artist, variant_title))
    return unique


def _lastfm_api_get_sync(params: dict[str, str], api_key: str) -> dict:
    query = urlencode({**params, "api_key": api_key, "format": "json"})
    url = f"https://ws.audioscrobbler.com/2.0/?{query}"
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _coerce_track(artist: str | None, title: str | None) -> tuple[str, str] | None:
    if not artist or not title:
        return None
    cleaned_artist, cleaned_title = clean_track_for_lookup(artist, title)
    if not cleaned_artist or not cleaned_title:
        return None
    return cleaned_artist, cleaned_title


def _extract_similar_from_api(
    artist: str, title: str, limit: int, api_key: str
) -> list[tuple[str, str]]:
    try:
        payload = _lastfm_api_get_sync(
            {
                "method": "track.getSimilar",
                "artist": artist,
                "track": title,
                "autocorrect": "1",
                "limit": str(limit),
            },
            api_key,
        )
    except (TimeoutError, URLError, OSError, json.JSONDecodeError, ValueError):
        return []

    tracks = payload.get("similartracks", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]

    results: list[tuple[str, str]] = []
    for entry in tracks:
        if not isinstance(entry, dict):
            continue
        artist_data = entry.get("artist")
        artist_name = ""
        if isinstance(artist_data, dict):
            artist_name = str(artist_data.get("name") or "").strip()
        title_name = str(entry.get("name") or "").strip()
        coerced = _coerce_track(artist_name, title_name)
        if coerced:
            results.append(coerced)
    return results


def _extract_similar_from_pylast(
    network: pylast.LastFMNetwork, artist: str, title: str, limit: int
) -> list[tuple[str, str]]:
    try:
        similar_items = network.get_track(artist, title).get_similar(limit=limit)
    except (pylast.WSError, TypeError, ValueError):
        return []

    results: list[tuple[str, str]] = []
    for item in similar_items:
        similar = getattr(item, "item", None)
        if not similar:
            continue
        similar_artist = getattr(getattr(similar, "artist", None), "name", None)
        similar_title = getattr(similar, "title", None)
        coerced = _coerce_track(similar_artist, similar_title)
        if coerced:
            results.append(coerced)
    return results


def _search_tracks_from_api(
    query: str, limit: int, api_key: str
) -> list[tuple[str, str]]:
    try:
        payload = _lastfm_api_get_sync(
            {
                "method": "track.search",
                "track": query,
                "limit": str(limit),
            },
            api_key,
        )
    except (TimeoutError, URLError, OSError, json.JSONDecodeError, ValueError):
        return []

    matches = payload.get("results", {}).get("trackmatches", {}).get("track", [])
    if isinstance(matches, dict):
        matches = [matches]

    results: list[tuple[str, str]] = []
    for entry in matches:
        if not isinstance(entry, dict):
            continue
        artist_name = str(entry.get("artist") or "").strip()
        title_name = str(entry.get("name") or "").strip()
        coerced = _coerce_track(artist_name, title_name)
        if coerced:
            results.append(coerced)
    return results


def _merge_tracks(
    target: list[tuple[str, str]], seen: set[str], tracks: list[tuple[str, str]]
) -> None:
    for artist, title in tracks:
        key = normalize_track_key(artist, title)
        if key in seen:
            continue
        seen.add(key)
        target.append((artist, title))


def fetch_similar_tracks_sync(
    artist: str,
    title: str,
    api_key: str,
    api_secret: str,
    limit: int = 3,
) -> list[tuple[str, str]]:
    network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)
    candidates = track_lookup_variants(artist, title)
    if not candidates:
        return []

    seen: set[str] = set()
    merged: list[tuple[str, str]] = []
    fetch_limit = max(6, limit * 4)

    for candidate_artist, candidate_title in candidates:
        _merge_tracks(
            merged,
            seen,
            _extract_similar_from_pylast(
                network, candidate_artist, candidate_title, fetch_limit
            ),
        )
        _merge_tracks(
            merged,
            seen,
            _extract_similar_from_api(
                candidate_artist, candidate_title, fetch_limit, api_key
            ),
        )
        if len(merged) >= limit:
            return merged[:limit]

    search_queries = [
        f"{artist_name} {title_name}" for artist_name, title_name in candidates
    ]
    search_queries.extend([title_name for _, title_name in candidates])
    for query in search_queries:
        for found_artist, found_title in _search_tracks_from_api(
            query, limit=5, api_key=api_key
        ):
            _merge_tracks(
                merged,
                seen,
                _extract_similar_from_pylast(
                    network, found_artist, found_title, fetch_limit
                ),
            )
            _merge_tracks(
                merged,
                seen,
                _extract_similar_from_api(
                    found_artist, found_title, fetch_limit, api_key
                ),
            )
            if len(merged) >= limit:
                return merged[:limit]

    return merged[:limit]


def fetch_tag_top_tracks_sync(
    tag: str,
    api_key: str,
    api_secret: str,
    limit: int = 10,
) -> list[tuple[str, str]]:
    network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)
    tag_obj = network.get_tag(tag)
    results: list[tuple[str, str]] = []
    try:
        top_items = tag_obj.get_top_tracks(limit=limit)
    except TypeError:
        top_items = tag_obj.get_top_tracks(limit=limit)

    for item in top_items:
        track = item.item
        artist_name = getattr(getattr(track, "artist", None), "name", None)
        title_name = getattr(track, "title", None)
        coerced = _coerce_track(artist_name, title_name)
        if coerced:
            results.append(coerced)
    return results

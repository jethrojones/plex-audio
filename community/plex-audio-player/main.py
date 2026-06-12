import asyncio
import collections
import json
import re
import time
import xml.etree.ElementTree as ET

import requests
from src.agent.capability import MatchingCapability
from src.main import AgentWorker
from src.agent.capability_worker import CapabilityWorker

PLEX_BASE_URL_KEY = "plex_base_url"
PLEX_TOKEN_KEY = "plex_token"
PLEX_ACCOUNT_TOKEN_KEY = "plex_account_token"
PLEX_SERVER_NAME_KEY = "plex_server_name"
PLEX_MACHINE_IDENTIFIER_KEY = "plex_machine_identifier"
PLEX_CLIENT_ID = "openhome-plexaudio-cloud"
PLEX_LINK_STATE_KEY = "plex_audio_account_link"
REQUEST_TIMEOUT = 15
STREAM_CHUNK_SIZE = 64 * 1024
AUDIO_SEARCH_TYPE = "10"
MAX_SEARCH_RESULTS = 30
RESUME_STATE_KEY = "plex_audio_last_audiobook"
RESUME_END_THRESHOLD_MS = 60 * 1000
EXIT_WORDS = {"stop", "exit", "quit", "cancel", "nevermind", "never mind", "done", "bye"}
DEVKIT_DIAGNOSE_TIMEOUT = 25
DEVKIT_SEARCH_TIMEOUT = 45
DEVKIT_PLAY_TIMEOUT = 20
DEVKIT_CONTROL_TIMEOUT = 15
PLAYBACK_LISTEN_WINDOW_SECONDS = 5
MAX_PLAYBACK_SECONDS = 12 * 60 * 60
STOP_WORDS = {"stop", "pause", "quit", "exit", "cancel", "enough", "done"}
STOP_PHRASES = [
    "stop the music",
    "stop playing",
    "stop playback",
    "stop the audiobook",
    "stop plex",
    "pause the music",
    "pause plex",
    "turn it off",
    "shut it off",
    "that s enough",
]

PlexAudioItem = collections.namedtuple(
    "PlexAudioItem",
    ["title", "creator", "collection", "media_type", "part_key", "duration_ms", "rating_key"],
)

PlexAudioClientState = collections.namedtuple(
    "PlexAudioClientState",
    ["base_url", "token", "logger", "url", "get_xml", "parse_tracks", "search_audio", "stream_url_for"],
)

URL_SAFE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

_SANITIZE_PATTERNS = [
    r"\bplay\b",
    r"\bsome\b",
    r"\bfrom\s+my\s*plex\s+library\b",
    r"\bfor\s+my\s*plex\s+library\b",
    r"\bmy\s*plex\s+library\b",
    r"\bfrom plex\b",
    r"\bon plex\b",
    r"\bin plex\b",
    r"\bplex\b",
    r"\blibrary\b",
    r"\bthe audiobook\b",
    r"\ban audiobook\b",
    r"\baudiobook\b",
    r"\baudio book\b",
    r"\bmusic\b",
    r"\bsong\b",
    r"\btrack\b",
    r"\balbum\b",
    r"\bartist\b",
    r"\bplease\b",
]


STOPWORDS = {
    "i", "ll", "im", "ive", "id", "a", "an", "the", "to", "of", "for", "and",
    "or", "me", "my", "we", "you", "it", "is", "on", "in", "at", "from", "some",
    "please", "play", "plex", "music", "song", "track", "album", "artist",
    "library", "put", "open", "home", "openhome", "oh", "hey", "like", "want",
    "hear", "listen", "audiobook", "book",
}


def normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def artist_matches_query(artist_title, user_text):
    """An artist hit requires every meaningful token of the artist's name to
    appear in the user's words — 'James Taylor' must not match 'play taylor swift'."""
    artist_tokens = [t for t in normalize_text(artist_title).split() if t not in STOPWORDS]
    query_tokens = set(normalize_text(user_text).split())
    return bool(artist_tokens) and all(t in query_tokens for t in artist_tokens)


def candidate_artist_phrases(user_text, max_attempts=10):
    """Generate candidate artist-name phrases from a user utterance.

    Tokenizes the normalized text, drops stopwords, then returns contiguous
    n-grams ordered longest-first (the full remaining phrase down to single
    tokens). Single tokens shorter than 3 chars are skipped unless numeric.
    Capped at max_attempts lookup attempts.
    """
    tokens = [t for t in normalize_text(user_text).split() if t and t not in STOPWORDS]
    phrases = []
    seen = set()
    n = len(tokens)
    for size in range(n, 0, -1):
        for start in range(0, n - size + 1):
            gram_tokens = tokens[start:start + size]
            if size == 1:
                token = gram_tokens[0]
                if len(token) < 3 and not token.isdigit():
                    continue
            phrase = " ".join(gram_tokens)
            if phrase and phrase not in seen:
                seen.add(phrase)
                phrases.append(phrase)
                if len(phrases) >= max_attempts:
                    return phrases
    return phrases


def detect_requested_media_type(user_text):
    text = normalize_text(user_text)
    audiobook_terms = {"audiobook", "audio book", "book", "chapter", "novel", "prologue"}
    music_terms = {"music", "song", "album", "artist", "track", "playlist", "band"}
    if any(term in text for term in audiobook_terms):
        return "audiobook"
    if any(term in text for term in music_terms):
        return "music"
    return None


def sanitize_search_query(user_text):
    text = str(user_text or "").strip()
    for pattern in _SANITIZE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    return text or str(user_text or "").strip()


def _meaningful_query(user_text):
    """Strip noise words; returns '' when only generic words remain (e.g. 'play music')."""
    text = str(user_text or "").strip()
    for pattern in _SANITIZE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" .,-")


def infer_media_type(library_title, title, creator, collection, duration_ms):
    haystack = normalize_text(" ".join([library_title, title, creator, collection]))
    if any(term in haystack for term in ["audiobook", "audio book", "books", "novel", "chapter"]):
        return "audiobook"
    if duration_ms and duration_ms >= 30 * 60 * 1000:
        return "audiobook"
    return "music"


def resume_requested(user_text):
    text = normalize_text(user_text)
    resume_phrases = [
        "continue my audiobook",
        "resume my audiobook",
        "continue audiobook",
        "resume audiobook",
        "continue my book",
        "resume my book",
        "pick up where i left off",
        "where i left off",
        "continue plex",
        "resume plex",
    ]
    return any(phrase in text for phrase in resume_phrases)


def score_item(item, user_text, requested_type=None):
    query = normalize_text(sanitize_search_query(user_text))
    item_text = normalize_text(" ".join([item.title, item.creator, item.collection]))
    score = 0
    if requested_type and item.media_type == requested_type:
        score += 100
    if query and query in item_text:
        score += 50
    query_tokens = [t for t in query.split() if t and t not in STOPWORDS]
    title_collection_tokens = set(
        normalize_text(" ".join([item.title, item.collection])).split()
    )
    creator_tokens = set(normalize_text(item.creator).split())
    for token in query_tokens:
        if token in creator_tokens:
            score += 40
        elif token in title_collection_tokens:
            score += 10
    title_text = normalize_text(item.title)
    if title_text and title_text in query:
        score += 15
    return score


def choose_best_item(items, user_text):
    if not items:
        return None
    requested_type = detect_requested_media_type(user_text)
    best = max(items, key=lambda item: score_item(item, user_text, requested_type))
    meaningful = _meaningful_query(user_text)
    # On a meaningful query, refuse to play junk: if even the best match scores
    # below a single token hit, the caller should say it found nothing rather
    # than play an unrelated track. Generic "play music" (empty meaningful
    # query) still returns the max-score item so browse-all playback works.
    if meaningful and score_item(best, user_text, requested_type) < 10:
        return None
    # Token-coverage guard: for a meaningful query, the best item must match
    # strictly more than half of the meaningful non-stopword tokens, otherwise
    # it is an incidental substring hit (e.g. "taylor" matching "James Taylor"
    # when the user said "Play Taylor Swift").
    if meaningful:
        meaningful_tokens = [t for t in normalize_text(meaningful).split() if t and t not in STOPWORDS]
        if meaningful_tokens:
            item_token_set = set(
                normalize_text(
                    " ".join([best.title, best.creator, best.collection])
                ).split()
            )
            matched = sum(1 for t in meaningful_tokens if t in item_token_set)
            if not (matched > len(meaningful_tokens) / 2):
                return None
    return best


def build_music_queue(items, choice):
    """Build the playback queue for a music choice.

    Prefer keeping the same artist: if at least two music items share the
    chosen item's creator, play the choice first then the remaining
    same-artist tracks in their existing (score) order. Otherwise fall back
    to a wrap-around of all items starting at the choice.
    """
    same_artist = [
        it
        for it in items
        if it.media_type == "music" and normalize_text(it.creator) == normalize_text(choice.creator)
    ]
    if len(same_artist) >= 2:
        return [choice] + [it for it in same_artist if it.part_key != choice.part_key]
    try:
        start_idx = next(i for i, it in enumerate(items) if it.part_key == choice.part_key)
    except StopIteration:
        start_idx = 0
    return items[start_idx:] + items[:start_idx]


def _url_quote(value):
    encoded = []
    for char in str(value or ""):
        if char in URL_SAFE_CHARS:
            encoded.append(char)
        elif char == " ":
            encoded.append("+")
        else:
            for byte in char.encode("utf-8"):
                encoded.append("%" + format(byte, "02X"))
    return "".join(encoded)


def _split_url(url):
    base_and_query, separator, fragment = str(url or "").partition("#")
    base, separator, query = base_and_query.partition("?")
    return base, query, fragment


def _parse_query(query_text):
    query = {}
    for pair in str(query_text or "").split("&"):
        if not pair:
            continue
        key, separator, value = pair.partition("=")
        query[key] = value
    return query


def _encode_query(query):
    parts = []
    for key, value in query.items():
        parts.append(_url_quote(key) + "=" + _url_quote(value))
    return "&".join(parts)


def _url_host(url):
    text = str(url or "")
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split(":", 1)[0]


def _url_is_local(url):
    host = _url_host(url)
    return (
        host.startswith("10.")
        or host.startswith("192.168.")
        or host.startswith("172.16.")
        or host.startswith("172.17.")
        or host.startswith("172.18.")
        or host.startswith("172.19.")
        or host.startswith("172.20.")
        or host.startswith("172.21.")
        or host.startswith("172.22.")
        or host.startswith("172.23.")
        or host.startswith("172.24.")
        or host.startswith("172.25.")
        or host.startswith("172.26.")
        or host.startswith("172.27.")
        or host.startswith("172.28.")
        or host.startswith("172.29.")
        or host.startswith("172.30.")
        or host.startswith("172.31.")
        or host in {"localhost", "127.0.0.1"}
    )


def _connection_matches(connection, server_name=None, machine_identifier=None):
    if server_name and str(connection.get("name") or "").lower() != str(server_name).lower():
        return False
    if machine_identifier and str(connection.get("machine_identifier") or "") != str(machine_identifier):
        return False
    return True


def choose_best_plex_connection(connections, preferred_subnets=None, prefer_remote=False):
    candidates = [conn for conn in connections if conn and conn.get("base_url")]
    if not candidates:
        return None
    if prefer_remote:
        # Pick a genuinely remote endpoint: not flagged local and not a private address.
        for conn in candidates:
            if not conn.get("local") and not _url_is_local(conn.get("base_url")):
                return conn
        return None
    prefixes = [str(prefix) for prefix in (preferred_subnets or []) if prefix]
    for prefix in prefixes:
        for conn in candidates:
            if _url_host(conn.get("base_url", "")).startswith(prefix):
                return conn
    for conn in candidates:
        if conn.get("local") or _url_is_local(conn.get("base_url")):
            return conn
    return candidates[0]


def parse_plex_tv_resources(xml_text, server_name=None, machine_identifier=None, preferred_subnets=None, prefer_remote=False):
    if not xml_text:
        return None
    root = ET.fromstring(xml_text)
    connections = []
    for device in root.findall(".//Device"):
        name = device.attrib.get("name") or device.attrib.get("clientIdentifier") or ""
        client_identifier = device.attrib.get("clientIdentifier") or ""
        token = device.attrib.get("accessToken") or ""
        device_data = {"name": name, "machine_identifier": client_identifier}
        if not _connection_matches(device_data, server_name, machine_identifier):
            continue
        for connection in device.findall(".//Connection"):
            uri = connection.attrib.get("uri") or ""
            if not uri:
                continue
            local_flag = str(connection.attrib.get("local") or "").lower() in {"1", "true", "yes"}
            connections.append(
                {
                    "base_url": uri.rstrip("/"),
                    "token": token,
                    "name": name,
                    "machine_identifier": client_identifier,
                    "local": local_flag,
                }
            )
    return choose_best_plex_connection(connections, preferred_subnets, prefer_remote)


def discover_plex_tv_resource(account_token, server_name=None, machine_identifier=None, preferred_subnets=None, logger=None, prefer_remote=False):
    token = str(account_token or "").strip()
    if not token:
        return None
    try:
        url = "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&X-Plex-Token=" + _url_quote(token)
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return parse_plex_tv_resources(response.text, server_name, machine_identifier, preferred_subnets, prefer_remote)
    except Exception as exc:
        if logger:
            logger.warning(f"[PlexAudio] Plex.tv resource discovery failed: {exc}")
    return None


def _plex_url(client, path, params=None):
    raw_path = str(path or "")
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        full_url = raw_path
    else:
        full_url = client.base_url.rstrip("/") + "/" + raw_path.lstrip("/")

    base, existing_query, fragment = _split_url(full_url)
    query = _parse_query(existing_query)
    if params:
        query.update({str(key): str(value) for key, value in params.items() if value is not None})
    if client.token:
        query["X-Plex-Token"] = client.token

    if query:
        url = base + "?" + _encode_query(query)
    else:
        url = base
    if fragment:
        url += "#" + fragment
    return url


def _plex_get_xml(client, path, params=None):
    response = requests.get(client.url(path, params), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _plex_duration_for(track):
    duration = track.attrib.get("duration")
    media = track.find(".//Media")
    if not duration and media is not None:
        duration = media.attrib.get("duration")
    try:
        return int(duration) if duration else 0
    except ValueError:
        return 0


def _plex_parse_tracks(client, xml_text):
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)
    items = []
    for track in root.findall(".//Track"):
        title = track.attrib.get("title") or "Untitled track"
        creator = track.attrib.get("grandparentTitle") or track.attrib.get("parentTitle") or "Unknown artist"
        collection = track.attrib.get("parentTitle") or track.attrib.get("grandparentTitle") or ""
        library_title = track.attrib.get("librarySectionTitle") or ""
        duration_ms = _plex_duration_for(track)
        part = track.find(".//Part")
        part_key = part.attrib.get("key") if part is not None else ""
        if not part_key:
            continue
        media_type = infer_media_type(library_title, title, creator, collection, duration_ms)
        rating_key = track.attrib.get("ratingKey") or track.attrib.get("key") or ""
        items.append(PlexAudioItem(title, creator, collection, media_type, part_key, duration_ms, rating_key))
    return items


def _parse_artist_directories(xml_text):
    """Parse <Directory type="artist"> elements into (rating_key, title) tuples."""
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)
    artists = []
    for directory in root.findall(".//Directory"):
        if directory.attrib.get("type", "") != "artist":
            continue
        rating_key = directory.attrib.get("ratingKey") or ""
        title = directory.attrib.get("title") or ""
        if rating_key and title:
            artists.append((rating_key, title))
    return artists


def _plex_artist_first_candidates(client, user_text, audio_sections):
    """Resolve the query to an artist and return that artist's tracks.

    Returns a list of items on an artist hit, or None when no artist matched
    (so the caller falls back to the generic search path).
    """
    phrases = candidate_artist_phrases(user_text)
    if not phrases:
        return None
    for section_key in audio_sections:
        if not section_key:
            continue
        path = f"/library/sections/{section_key}/all"
        for phrase in phrases:
            try:
                artists = _parse_artist_directories(client.get_xml(path, {"type": "8", "title": phrase}))
            except Exception as exc:
                if client.logger:
                    client.logger.warning(f"[PlexAudio] Artist lookup failed for {phrase!r}: {exc}")
                continue
            # Keep only artists whose every meaningful token appears in user_text.
            accepted = [
                (rating_key, artist_title)
                for rating_key, artist_title in artists
                if artist_matches_query(artist_title, user_text)
            ]
            if not accepted:
                continue
            # Prefer the most specific artist (most non-stopword tokens); tie-break on title length.
            accepted.sort(
                key=lambda rk_t: (
                    len([t for t in normalize_text(rk_t[1]).split() if t not in STOPWORDS]),
                    len(rk_t[1]),
                ),
                reverse=True,
            )
            for rating_key, artist_title in accepted:
                try:
                    tracks = client.parse_tracks(
                        client.get_xml(path, {"type": AUDIO_SEARCH_TYPE, "artist.id": rating_key})
                    )
                except Exception as exc:
                    if client.logger:
                        client.logger.warning(f"[PlexAudio] Artist track lookup failed for {artist_title!r}: {exc}")
                    continue
                if tracks:
                    return tracks
    return None


def _plex_search_audio(client, user_text):
    query = sanitize_search_query(user_text)
    meaningful = _meaningful_query(user_text)
    requested_type = detect_requested_media_type(user_text)
    candidates = []

    audio_sections = []
    try:
        sections_xml = client.get_xml("/library/sections")
        root = ET.fromstring(sections_xml)
        for directory in root.findall(".//Directory"):
            section_type = directory.attrib.get("type", "")
            section_key = directory.attrib.get("key", "")
            if section_type in {"artist", "music"} and section_key:
                audio_sections.append(section_key)
    except Exception as exc:
        if client.logger:
            client.logger.warning(f"[PlexAudio] Section discovery failed: {exc}")

    # Artist-first: resolve the meaningful query to an artist and use their
    # tracks as the candidate pool, skipping the generic search entirely.
    if meaningful:
        artist_tracks = _plex_artist_first_candidates(client, user_text, audio_sections)
        if artist_tracks:
            deduped = []
            seen = set()
            for item in artist_tracks:
                key = item.part_key or "|".join([item.title, item.creator, item.collection])
                if key not in seen:
                    seen.add(key)
                    deduped.append(item)
            deduped.sort(key=lambda item: score_item(item, user_text, requested_type), reverse=True)
            return deduped[:MAX_SEARCH_RESULTS]

    if meaningful:
        try:
            candidates.extend(client.parse_tracks(client.get_xml("/search", {"query": query})))
        except Exception as exc:
            if client.logger:
                client.logger.warning(f"[PlexAudio] Global search failed: {exc}")

    try:
        for section_key in audio_sections:
            path = f"/library/sections/{section_key}/all"
            if meaningful:
                title_matches = client.parse_tracks(client.get_xml(path, {"type": AUDIO_SEARCH_TYPE, "title": query}))
                candidates.extend(title_matches)
                if not title_matches:
                    scanned = client.parse_tracks(client.get_xml(path, {"type": AUDIO_SEARCH_TYPE}))
                    candidates.extend(
                        item for item in scanned if score_item(item, user_text, None) > 0
                    )
            else:
                scanned = client.parse_tracks(client.get_xml(path, {"type": AUDIO_SEARCH_TYPE}))
                candidates.extend(scanned)
    except Exception as exc:
        if client.logger:
            client.logger.warning(f"[PlexAudio] Section search failed: {exc}")

    deduped = []
    seen = set()
    for item in candidates:
        key = item.part_key or "|".join([item.title, item.creator, item.collection])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _plex_stream_url_for(client, item, offset_ms=0):
    params = {"download": "1"}
    try:
        offset_seconds = max(0, int(offset_ms) // 1000)
    except (TypeError, ValueError):
        offset_seconds = 0
    if offset_seconds:
        params["offset"] = str(offset_seconds)
    return client.url(item.part_key, params)


def PlexAudioClient(base_url, token, logger=None):
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    normalized_token = str(token or "").strip()

    client = PlexAudioClientState(
        normalized_base_url,
        normalized_token,
        logger,
        None,
        None,
        None,
        None,
        None,
    )
    client = client._replace(
        url=lambda path, params=None: _plex_url(client, path, params),
        get_xml=lambda path, params=None: _plex_get_xml(client, path, params),
        parse_tracks=lambda xml_text: _plex_parse_tracks(client, xml_text),
        search_audio=lambda user_text: _plex_search_audio(client, user_text),
        stream_url_for=lambda item, offset_ms=0: _plex_stream_url_for(client, item, offset_ms),
    )
    return client


def exit_requested(user_text):
    text = normalize_text(user_text)
    return text in EXIT_WORDS or any(text == normalize_text(word) for word in EXIT_WORDS)


def link_requested(user_text):
    """True when the user is asking to link/connect their Plex account.

    Requires the word "plex" plus an explicit account/auth verb so ordinary
    play requests ("play metallica from plex") never trigger linking."""
    text = normalize_text(user_text)
    if "plex" not in text:
        return False
    link_terms = [
        "link", "sign in", "log in", "login", "connect",
        "authenticate", "authorize", "account",
    ]
    return any(term in text for term in link_terms)


def parse_pin_response(json_dict):
    """Extract (pin_id, code) from a Plex create-pin response, or (None, None)."""
    if not isinstance(json_dict, dict):
        return None, None
    pin_id = json_dict.get("id")
    code = json_dict.get("code")
    if pin_id is None or not code:
        return None, None
    return pin_id, str(code)


def parse_pin_poll(json_dict):
    """Return the authToken from a Plex poll response, or None when unclaimed."""
    if not isinstance(json_dict, dict):
        return None
    token = json_dict.get("authToken")
    if not token:
        return None
    return str(token)


def spell_out_code(code):
    """Render a link code as discrete TTS-friendly characters: 'A. B. C. 7.'."""
    return " ".join(f"{char}." for char in str(code or "").strip())


NEGATION_WORDS = {
    "don", "dont", "do", "not", "won", "wont", "can", "cant", "cannot",
    "never", "no",
}
# Normalizing strips apostrophes, so "don't" becomes "don t". When scanning
# backwards for the word that precedes a command token, skip these clitic
# fragments so "don t stop" is recognized as a negation of "stop".
_CLITIC_FRAGMENTS = {"t", "s"}
SHORT_UTTERANCE_LIMIT = 6


def _command_token_is_negated(tokens, index):
    """True when the meaningful token before tokens[index] is a negation."""
    i = index - 1
    while i >= 0:
        if tokens[i] in _CLITIC_FRAGMENTS:
            i -= 1
            continue
        return tokens[i] in NEGATION_WORDS
    return False


def _has_unnegated_command(tokens, command_words):
    for i, token in enumerate(tokens):
        if token in command_words and not _command_token_is_negated(tokens, i):
            return True
    return False


def _phrase_present_unnegated(tokens, phrase):
    """True when `phrase` appears in `tokens` without a preceding negation."""
    phrase_tokens = phrase.split()
    if not phrase_tokens:
        return False
    span = len(phrase_tokens)
    for start in range(0, len(tokens) - span + 1):
        if tokens[start:start + span] == phrase_tokens and not _command_token_is_negated(tokens, start):
            return True
    return False


def playback_stop_requested(user_text):
    text = normalize_text(user_text)
    if not text:
        return False
    tokens = text.split()
    # Music playing near the mic produces noisy transcriptions ("don't stop believing"),
    # so single stop words only count in short utterances, and never when negated
    # ("please don't stop the music").
    if len(tokens) <= SHORT_UTTERANCE_LIMIT and _has_unnegated_command(tokens, STOP_WORDS):
        return True
    # STOP_PHRASES still match anywhere, but a negation in front of the phrase
    # ("don't stop the music") cancels it.
    return any(_phrase_present_unnegated(tokens, normalize_text(phrase)) for phrase in STOP_PHRASES)


def playback_skip_requested(user_text):
    text = normalize_text(user_text)
    if not text:
        return False
    # Stop always wins — never treat a stop phrase as a skip.
    if playback_stop_requested(user_text):
        return False
    tokens = text.split()
    # Music near the mic produces noisy transcriptions, so only short utterances
    # count as a skip command, and never when negated ("don't skip this one").
    if len(tokens) <= SHORT_UTTERANCE_LIMIT and _has_unnegated_command(tokens, {"next", "skip"}):
        return True
    return False


def playback_new_request(user_text):
    text = normalize_text(user_text)
    if not text:
        return False
    if playback_stop_requested(user_text) or playback_skip_requested(user_text):
        return False
    return bool(re.search(r"\bplay\b", text))


STALE_REPEAT_WINDOW_SECONDS = 5


def is_stale_repeat(heard, last_text, last_at, now):
    """True when `heard` is the same (normalized) utterance we already processed
    within STALE_REPEAT_WINDOW_SECONDS.

    wait_for_complete_transcription has been observed re-returning the same stale
    transcription on consecutive polls; treating such a repeat as silence keeps
    the playback loop from re-firing stop/skip/new_request on one utterance.
    Empty `heard` is never a stale repeat (the empty-result throttle handles it).
    """
    normalized = normalize_text(heard)
    if not normalized:
        return False
    if not last_text or normalize_text(last_text) != normalized:
        return False
    if last_at is None:
        return False
    return (now - last_at) <= STALE_REPEAT_WINDOW_SECONDS


def parse_devkit_payload(output_text):
    text = str(output_text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    return payload
            except ValueError:
                continue
    return None


def items_from_search_payload(payload):
    items = []
    for entry in (payload or {}).get("items") or []:
        part_key = str(entry.get("part_key") or "")
        if not part_key:
            continue
        try:
            duration_ms = int(entry.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        items.append(
            PlexAudioItem(
                str(entry.get("title") or "Untitled track"),
                str(entry.get("creator") or "Unknown artist"),
                str(entry.get("collection") or ""),
                str(entry.get("media_type") or "music"),
                part_key,
                duration_ms,
                str(entry.get("rating_key") or ""),
            )
        )
    return items


def describe_item(item):
    if item.media_type == "audiobook":
        if item.collection and item.collection != item.creator:
            return f"{item.creator}, {item.title}"
        return f"{item.creator}: {item.title}"
    if item.collection:
        return f"{item.title} by {item.creator} from {item.collection}"
    return f"{item.title} by {item.creator}"


def build_resume_state(item, offset_ms=0):
    if not item or item.media_type != "audiobook":
        return None
    return {
        "title": item.title,
        "creator": item.creator,
        "collection": item.collection,
        "media_type": item.media_type,
        "part_key": item.part_key,
        "duration_ms": item.duration_ms,
        "rating_key": item.rating_key,
        "offset_ms": max(0, int(offset_ms or 0)),
    }


def item_from_resume_state(state):
    if not state or state.get("media_type") != "audiobook" or not state.get("part_key"):
        return None
    return PlexAudioItem(
        state.get("title") or "Audiobook",
        state.get("creator") or "Unknown book",
        state.get("collection") or "",
        "audiobook",
        state.get("part_key") or "",
        int(state.get("duration_ms") or 0),
        state.get("rating_key") or "",
    )


def updated_resume_state(state, elapsed_ms):
    if not state:
        return None
    updated = dict(state)
    duration_ms = int(updated.get("duration_ms") or 0)
    offset_ms = max(0, int(updated.get("offset_ms") or 0) + int(elapsed_ms or 0))
    if duration_ms and offset_ms >= max(0, duration_ms - RESUME_END_THRESHOLD_MS):
        offset_ms = 0
    updated["offset_ms"] = offset_ms
    return updated


def plex_error_message(base_url, exc, devkit_mode=False):
    error_text = str(exc or "").lower()
    base_text = str(base_url or "")
    if "401" in error_text or "unauthorized" in error_text:
        return "Plex rejected the token. Check the plex token API key and try again."
    if "no_player" in error_text or "no audio player" in error_text:
        return (
            "Your DevKit has no audio player installed. "
            "Install one on the DevKit with sudo apt install mpv, then try again."
        )
    if "devkit" in error_text:
        return (
            "I could not reach your DevKit to play from Plex. "
            "Check that the DevKit is online and this Ability is synced to it as a Local Ability."
        )
    if "timed out" in error_text or "connecttimeout" in error_text or "connection refused" in error_text:
        if devkit_mode:
            return "Your DevKit cannot reach the Plex server. Check that Plex is running and the plex base url points to its LAN address."
        if "192.168." in base_text or "10." in base_text or "172." in base_text or "localhost" in base_text:
            return (
                "OpenHome cannot reach your Plex server at that local network address. "
                "Use a local DevKit on the same network, or enable Plex Remote Access and set plex base url to a remote Plex URL."
            )
        return "OpenHome cannot reach your Plex server. Check that the plex base url is online and reachable."
    return "Sorry, Plex playback did not work. Check that your Plex server URL is reachable and your token is valid."


class PlexAudioPlayerCapability(MatchingCapability):
    worker: AgentWorker = None
    capability_worker: CapabilityWorker = None

    # Do not change following tag of register capability
    #{{register capability}}

    def call(self, worker: AgentWorker):
        self.worker = worker
        self.capability_worker = CapabilityWorker(self)
        self.worker.session_tasks.create(self.run())

    def _get_optional_api_key(self, key):
        try:
            return self.capability_worker.get_api_keys(key)
        except Exception:
            return ""

    def _get_required_config(self):
        base_url = self._get_optional_api_key(PLEX_BASE_URL_KEY)
        token = self._get_optional_api_key(PLEX_TOKEN_KEY)
        account_token = self._get_optional_api_key(PLEX_ACCOUNT_TOKEN_KEY)
        server_name = self._get_optional_api_key(PLEX_SERVER_NAME_KEY)
        machine_identifier = self._get_optional_api_key(PLEX_MACHINE_IDENTIFIER_KEY)
        missing = []
        if not base_url and not account_token:
            # LAN GDM discovery can still work with no keys when the runtime is on the same network.
            # Keep this non-blocking so local/no-auth Plex setups do not require dummy credentials.
            missing = []
        return base_url, token, account_token, server_name, machine_identifier, missing

    def _preferred_subnets(self):
        subnets = []
        for value in ["10.", "192.168."]:
            subnets.append(value)
        return subnets

    def _resolve_client(self, base_url, token, account_token, server_name, machine_identifier):
        logger = self.worker.editor_logging_handler
        # Fall back to the stored account-link token when config has no token.
        # A Plex account auth token works as both a server token (direct auth)
        # and an account token (plex.tv resource discovery).
        link_token = self._linked_token()
        if not token and link_token:
            token = link_token
        if not account_token and link_token:
            account_token = link_token

        if base_url:
            return PlexAudioClient(base_url, token, logger)

        connection = discover_plex_tv_resource(
            account_token,
            server_name=server_name,
            machine_identifier=machine_identifier,
            preferred_subnets=self._preferred_subnets(),
            logger=logger,
        )
        if connection:
            return PlexAudioClient(connection.get("base_url"), connection.get("token") or token, logger)
        return None

    async def _devkit_call(self, function_name, args, timeout):
        """Run a devkit_functions.py function and return (payload, error_text)."""
        if not hasattr(self.capability_worker, "send_devkit_capability_action"):
            return None, "DevKit actions are not available in this runtime."
        try:
            result = await self.capability_worker.send_devkit_capability_action(
                function_name=function_name,
                args=[str(arg) for arg in args],
                timeout=timeout,
            )
        except Exception as exc:
            return None, f"DevKit call {function_name} failed: {exc}"
        if not isinstance(result, dict):
            return None, f"DevKit call {function_name} returned an unexpected result."
        payload = parse_devkit_payload(result.get("output"))
        if payload is None:
            return None, f"DevKit call {function_name} returned no payload: {result.get('error')}"
        if not payload.get("success"):
            error = payload.get("error") or {}
            return None, f"{error.get('code', 'devkit_error')}: {error.get('message', 'unknown DevKit error')}"
        return payload.get("data") or {}, None

    async def _devkit_diagnose(self, base_url, token):
        """Returns the DevKit's diagnose report, or None when no DevKit responded."""
        logger = self.worker.editor_logging_handler
        for attempt in range(2):
            data, error = await self._devkit_call(
                "plex_diagnose", [base_url, token or ""], DEVKIT_DIAGNOSE_TIMEOUT
            )
            if data is not None:
                logger.info(f"[PlexAudio] DevKit diagnose (attempt {attempt + 1}): {data}")
                return data
            logger.warning(f"[PlexAudio] DevKit diagnose attempt {attempt + 1} failed: {error}")
            if attempt == 0:
                await asyncio.sleep(3)
        return None

    async def _devkit_search(self, client, user_request):
        data, error = await self._devkit_call(
            "plex_search", [client.base_url, client.token or "", user_request], DEVKIT_SEARCH_TIMEOUT
        )
        if data is None:
            raise RuntimeError(f"DevKit search failed: {error}")
        return items_from_search_payload(data)

    async def _listen_during_playback(self):
        """Wait briefly for the user to say something; None on silence."""
        try:
            return await asyncio.wait_for(
                self.capability_worker.wait_for_complete_transcription(),
                timeout=PLAYBACK_LISTEN_WINDOW_SECONDS,
            )
        except asyncio.TimeoutError:
            return None
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Playback listen failed: {exc}")
            await self.worker.session_tasks.sleep(PLAYBACK_LISTEN_WINDOW_SECONDS)
            return None

    async def _stop_devkit_playback(self):
        """Stop mpv on the DevKit, retrying once. Returns the final (data, error).

        plex_stop is the only thing that kills the detached mpv process, so a
        single failed call can orphan music. Retry once after a short pause; the
        DevKit reports was_playing=false when nothing was playing, so a redundant
        call is harmless."""
        data, error = await self._devkit_call("plex_stop", [], DEVKIT_CONTROL_TIMEOUT)
        if data is None:
            await self.worker.session_tasks.sleep(2)
            data, error = await self._devkit_call("plex_stop", [], DEVKIT_CONTROL_TIMEOUT)
        return data, error

    async def _devkit_playback(self, client, item, offset_ms, allow_switch=False):
        """Play on the DevKit and wait for finish or a spoken command.

        Returns (position_ms, action, payload):
          "ended"       — track finished naturally, status failed 3x, or watchdog expired. payload None.
          "stopped"     — user asked to stop; playback halted. payload None.
          "skip"        — user said next/skip (allow_switch only); playback ducked, not stopped. payload None.
          "new_request" — user asked to play something new (allow_switch only); music keeps
                          playing ducked, payload is the raw heard utterance.

        Detection precedence when something is heard: stop > skip > new_request > ignore."""
        data, error = await self._devkit_call(
            "plex_play",
            [
                client.base_url,
                client.token or "",
                item.part_key,
                str(int(offset_ms or 0)),
                str(int(item.duration_ms or 0)),
                item.title,
                item.rating_key or "",
            ],
            DEVKIT_PLAY_TIMEOUT,
        )
        if data is None:
            raise RuntimeError(f"DevKit playback failed: {error}")
        # A successful plex_play means mpv is now running detached on the DevKit;
        # the run() finally must stop it on any exit (including cancellation) so
        # the orphan-mpv incident cannot recur.
        self._devkit_playback_started = True

        started = time.monotonic()
        position_ms = int(offset_ms or 0)
        status_failures = 0
        stop_attempts = 0
        last_heard_text = None
        last_heard_at = None
        while time.monotonic() - started < MAX_PLAYBACK_SECONDS:
            iteration_started = time.monotonic()
            # Platform-driven stop/pause: the runtime sets these events when the
            # user asks to stop or pause during music mode. Same device action
            # here (mpv is killed); callers persist audiobook positions.
            if hasattr(self.worker, "music_mode_stop_event") and self.worker.music_mode_stop_event.is_set():
                await self._devkit_call("plex_duck", ["10"], 5)
                stop_data, _ = await self._stop_devkit_playback()
                if stop_data is not None:
                    position_ms = int(stop_data.get("position_ms") or position_ms)
                    await self.capability_worker.speak("Okay, stopping Plex.")
                    return position_ms, "stopped", None
                stop_attempts += 1
                if stop_attempts >= 3:
                    await self.capability_worker.speak(
                        "I can't reach the device to stop the music. You may need to restart it."
                    )
                    return position_ms, "stopped", None
                await self.capability_worker.speak("I couldn't stop the player. I'll keep trying.")
                continue
            if hasattr(self.worker, "music_mode_pause_event") and self.worker.music_mode_pause_event.is_set():
                await self._devkit_call("plex_duck", ["10"], 5)
                stop_data, _ = await self._stop_devkit_playback()
                if stop_data is not None:
                    position_ms = int(stop_data.get("position_ms") or position_ms)
                    await self.capability_worker.speak("Okay, pausing Plex.")
                    return position_ms, "stopped", None
                stop_attempts += 1
                if stop_attempts >= 3:
                    await self.capability_worker.speak(
                        "I can't reach the device to stop the music. You may need to restart it."
                    )
                    return position_ms, "stopped", None
                await self.capability_worker.speak("I couldn't stop the player. I'll keep trying.")
                continue
            status, status_error = await self._devkit_call("plex_status", [], DEVKIT_CONTROL_TIMEOUT)
            if status is None:
                status_failures += 1
                self.worker.editor_logging_handler.warning(f"[PlexAudio] Status check failed: {status_error}")
                if status_failures >= 3:
                    # The DevKit is unreachable. Don't let the queue advance to the
                    # next track on a dead device — best-effort stop and report lost.
                    await self._stop_devkit_playback()
                    return position_ms, "lost", None
            else:
                status_failures = 0
                position_ms = int(status.get("position_ms") or position_ms)
                if not status.get("playing"):
                    return position_ms, "ended", None
            listen_started = time.monotonic()
            heard = await self._listen_during_playback()
            # A hot listen loop: wait_for_complete_transcription has been observed
            # returning instantly/empty (~1 poll/second), which would make the
            # ability deaf. Throttle so instant-empty results can't spin.
            if not normalize_text(heard) and (time.monotonic() - listen_started) < 1:
                await self.worker.session_tasks.sleep(2)
            # Stale-transcription dedupe: the same non-empty utterance has been
            # observed re-returning on back-to-back polls (instant, non-empty),
            # bypassing the empty-result throttle. Treat a recent repeat as
            # silence so one utterance fires a command at most once.
            now = time.monotonic()
            if is_stale_repeat(heard, last_heard_text, last_heard_at, now):
                heard = None
            elif normalize_text(heard):
                last_heard_text = heard
                last_heard_at = now
            if heard:
                if playback_stop_requested(heard):
                    await self._devkit_call("plex_duck", ["10"], 5)
                    stop_data, _ = await self._stop_devkit_playback()
                    if stop_data is not None:
                        position_ms = int(stop_data.get("position_ms") or position_ms)
                        await self.capability_worker.speak("Okay, stopping Plex.")
                        return position_ms, "stopped", None
                    stop_attempts += 1
                    if stop_attempts >= 3:
                        await self.capability_worker.speak(
                            "I can't reach the device to stop the music. You may need to restart it."
                        )
                        return position_ms, "stopped", None
                    await self.capability_worker.speak("I couldn't stop the player. I'll keep trying.")
                    continue
                if allow_switch and playback_skip_requested(heard):
                    await self._devkit_call("plex_duck", ["10"], 5)
                    await self.capability_worker.speak("Okay, next.")
                    return position_ms, "skip", None
                if allow_switch and playback_new_request(heard):
                    await self._devkit_call("plex_duck", ["20"], 5)
                    return position_ms, "new_request", heard
            # Minimum cycle time: a complete status+listen iteration that took
            # under 2 seconds (e.g. instant status + instant non-empty listen)
            # would hot-loop, so sleep the remainder before the next iteration.
            iteration_elapsed = time.monotonic() - iteration_started
            if iteration_elapsed < 2:
                await self.worker.session_tasks.sleep(2 - iteration_elapsed)
        await self._stop_devkit_playback()
        return position_ms, "ended", None

    async def _get_initial_request(self):
        try:
            msg = await self.capability_worker.wait_for_complete_transcription()
            if msg and sanitize_search_query(msg):
                return msg
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Initial transcription unavailable: {exc}")
        return await self.capability_worker.run_io_loop(
            "What would you like me to play from Plex — music or an audiobook?"
        )

    async def _stream_audio(self, stream_url):
        started_at = time.monotonic()
        if hasattr(self.worker, "music_mode_event"):
            self.worker.music_mode_event.set()
        try:
            await self.capability_worker.send_data_over_websocket("music-mode", {"mode": "on"})
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Music mode signal failed: {exc}")

        try:
            response = requests.get(stream_url, timeout=REQUEST_TIMEOUT, stream=True)
            response.raise_for_status()
            if hasattr(self.capability_worker, "stream_init"):
                await self.capability_worker.stream_init()
                for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if chunk:
                        await self.capability_worker.send_audio_data_in_stream(chunk, chunk_size=STREAM_CHUNK_SIZE)
                await self.capability_worker.stream_end()
            else:
                await self.capability_worker.play_audio(response.content)
            return int((time.monotonic() - started_at) * 1000)
        finally:
            try:
                await self.capability_worker.send_data_over_websocket("music-mode", {"mode": "off"})
            except Exception as exc:
                self.worker.editor_logging_handler.warning(f"[PlexAudio] Music mode cleanup failed: {exc}")
            if hasattr(self.worker, "music_mode_event"):
                self.worker.music_mode_event.clear()

    def _read_resume_state(self):
        try:
            return self.capability_worker.get_single_key(RESUME_STATE_KEY)
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Resume read failed: {exc}")
            return None

    def _write_resume_state(self, state):
        if not state:
            return
        try:
            existing = self.capability_worker.get_single_key(RESUME_STATE_KEY)
            if existing:
                self.capability_worker.update_key(RESUME_STATE_KEY, state)
            else:
                self.capability_worker.create_key(RESUME_STATE_KEY, state)
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Resume save failed: {exc}")

    def _read_link_state(self):
        try:
            return self.capability_worker.get_single_key(PLEX_LINK_STATE_KEY)
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Link read failed: {exc}")
            return None

    def _write_link_state(self, state):
        if not state:
            return
        try:
            existing = self.capability_worker.get_single_key(PLEX_LINK_STATE_KEY)
            if existing:
                self.capability_worker.update_key(PLEX_LINK_STATE_KEY, state)
            else:
                self.capability_worker.create_key(PLEX_LINK_STATE_KEY, state)
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Link save failed: {exc}")

    def _linked_token(self):
        state = self._read_link_state()
        if isinstance(state, dict):
            return str(state.get("token") or "").strip()
        return ""

    def _plex_link_headers(self):
        return {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
            "X-Plex-Product": "OpenHome PlexAudio",
        }

    async def _music_mode_on(self):
        """Signal the platform that immersive music playback is starting."""
        if hasattr(self.worker, "music_mode_event"):
            self.worker.music_mode_event.set()
        try:
            await self.capability_worker.send_data_over_websocket("music-mode", {"mode": "on"})
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Music mode signal failed: {exc}")

    async def _music_mode_off(self):
        """Tear down music mode. Safe to call even if it was never turned on."""
        try:
            await self.capability_worker.send_data_over_websocket("music-mode", {"mode": "off"})
        except Exception as exc:
            self.worker.editor_logging_handler.warning(f"[PlexAudio] Music mode cleanup failed: {exc}")
        if hasattr(self.worker, "music_mode_event"):
            self.worker.music_mode_event.clear()
        if hasattr(self.worker, "music_mode_stop_event"):
            self.worker.music_mode_stop_event.clear()
        if hasattr(self.worker, "music_mode_pause_event"):
            self.worker.music_mode_pause_event.clear()

    async def _link_plex_account(self):
        """Run the Plex PIN device-link flow and store the resulting account token."""
        logger = self.worker.editor_logging_handler
        headers = self._plex_link_headers()
        try:
            # Non-strong PINs yield the short 4-character code that plex.tv/link
            # accepts for manual entry; strong=true returns a long token instead.
            response = requests.post(
                "https://plex.tv/api/v2/pins",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            pin_id, code = parse_pin_response(response.json())
        except Exception as exc:
            logger.warning(f"[PlexAudio] Plex PIN creation failed: {exc}")
            await self.capability_worker.speak(
                "I couldn't start the Plex sign-in right now. Please try again in a moment."
            )
            return

        if not pin_id or not code:
            await self.capability_worker.speak(
                "I couldn't start the Plex sign-in right now. Please try again in a moment."
            )
            return

        spelled = spell_out_code(code)
        await self.capability_worker.speak(
            "To link your Plex account, open plex dot tv slash link on your phone or computer, "
            f"and enter the code: {spelled} ... I'll wait while you enter it."
        )

        poll_url = f"https://plex.tv/api/v2/pins/{pin_id}"
        # Poll every 5 seconds for up to 4 minutes (48 attempts), with one
        # reminder near the 2-minute mark.
        max_attempts = 48
        reminder_at = 24  # ~2 minutes in
        for attempt in range(max_attempts):
            await self.worker.session_tasks.sleep(5)
            if attempt == reminder_at:
                await self.capability_worker.speak(
                    f"Still waiting. Enter this code at plex dot tv slash link: {spelled}"
                )
            try:
                poll = requests.get(poll_url, headers=headers, timeout=REQUEST_TIMEOUT)
                poll.raise_for_status()
                auth_token = parse_pin_poll(poll.json())
            except Exception as exc:
                logger.warning(f"[PlexAudio] Plex PIN poll failed: {exc}")
                continue
            if auth_token:
                self._write_link_state({"token": auth_token, "linked_at": time.time()})
                await self.capability_worker.speak(
                    "Your Plex account is linked. You can now ask me to play music or audiobooks."
                )
                return

        await self.capability_worker.speak(
            "That code expired before it was entered. Say \"link my Plex account\" to try again."
        )

    async def _remote_fallback_client(self, account_token, server_name, machine_identifier, current_client):
        """Try Plex.tv resource discovery for a remote-access endpoint.

        Returns a new PlexAudioClient built from the remote connection, or None
        if no account token is available or discovery finds nothing usable.
        """
        remote_token = account_token or self._linked_token()
        if not remote_token:
            return None
        remote_connection = discover_plex_tv_resource(
            remote_token,
            server_name=server_name,
            machine_identifier=machine_identifier,
            preferred_subnets=None,
            logger=self.worker.editor_logging_handler,
            prefer_remote=True,
        )
        if remote_connection and remote_connection.get("base_url"):
            return PlexAudioClient(
                remote_connection.get("base_url"),
                remote_connection.get("token") or current_client.token,
                self.worker.editor_logging_handler,
            )
        return None

    async def run(self):
        base_url = ""
        devkit_mode = False
        # Reset per-session so a reused capability instance never thinks a prior
        # session's playback is still active.
        self._devkit_playback_started = False
        try:
            base_url, token, account_token, server_name, machine_identifier, missing = self._get_required_config()
            if missing:
                await self.capability_worker.speak(
                    "Plex Audio Player needs setup first. Add a Plex base URL, or add a Plex account token for discovery."
                )
                return

            client = self._resolve_client(base_url, token, account_token, server_name, machine_identifier)
            if not client:
                await self.capability_worker.speak(
                    "I could not find your Plex server. Add plex base url, or add a Plex account token and server name for discovery."
                )
                return
            base_url = client.base_url

            # Preferred path: the DevKit reaches Plex over the LAN and plays locally,
            # so the cloud runtime never needs a route to the Plex server.
            devkit_info = await self._devkit_diagnose(client.base_url, client.token)
            _link_guidance = (
                " If Plex remote access is enabled, say link my Plex account, "
                "and I can stream from outside your network when the local connection is down."
            )
            if devkit_info is None:
                if _url_is_local(client.base_url):
                    # The DevKit is down and the only known Plex address is on the
                    # LAN, which the cloud runtime cannot reach. Before giving up,
                    # try Plex.tv resource discovery for a remote-access endpoint.
                    remote_client = await self._remote_fallback_client(
                        account_token, server_name, machine_identifier, client
                    )
                    if remote_client:
                        client = remote_client
                        base_url = client.base_url
                        devkit_mode = False
                        await self.capability_worker.speak(
                            "Your OpenHome device is not reachable, so I'll stream from Plex remote access instead."
                        )
                    else:
                        await self.capability_worker.speak(
                            "I could not connect to the OpenHome device to reach your Plex server. "
                            "Make sure the device is powered on and the Plex Audio ability is synced to it, then try again."
                            + _link_guidance
                        )
                        return
                else:
                    devkit_mode = False
            elif not devkit_info.get("plex_reachable"):
                # DevKit is up but cannot reach Plex locally. Try remote access
                # before giving up.
                remote_client = await self._remote_fallback_client(
                    account_token, server_name, machine_identifier, client
                )
                if remote_client:
                    client = remote_client
                    base_url = client.base_url
                    devkit_mode = False
                    await self.capability_worker.speak(
                        "Your OpenHome device cannot reach Plex on your local network, "
                        "so I'll stream from Plex remote access instead."
                    )
                else:
                    await self.capability_worker.speak(
                        "Your OpenHome device is online, but it cannot reach the Plex server at the configured address. "
                        "Check that Plex is running and that plex base url is the server's local network address, like its LAN IP and port 32400."
                        + _link_guidance
                    )
                    return
            elif not devkit_info.get("player"):
                await self.capability_worker.speak(
                    "Your OpenHome device can reach Plex, but it has no audio player installed. "
                    "On the device, run sudo apt install mpv, then ask me again."
                )
                return
            else:
                devkit_mode = True

            user_request = await self._get_initial_request()
            # Duck any in-progress music (possibly from an orphaned earlier
            # session) so every spoken response below is audible over it. It's a
            # no-op on the DevKit when nothing is playing.
            await self._devkit_call("plex_duck", ["20"], 5)
            if not user_request or exit_requested(user_request):
                await self.capability_worker.speak("Okay, I will leave Plex closed.")
                return

            if link_requested(user_request):
                await self._link_plex_account()
                return

            # Fresh-session stop kill-switch: a "stop the music" request landing in
            # a brand-new session (no capability attached to the orphaned playback)
            # must still kill mpv on the DevKit, rather than letting the default
            # agent falsely claim it stopped.
            if playback_stop_requested(user_request):
                await self._devkit_call("plex_duck", ["10"], 5)
                stop_data, _ = await self._stop_devkit_playback()
                if stop_data is None:
                    await self.capability_worker.speak(
                        "I couldn't reach the OpenHome device to stop the music. Check that it's powered on."
                    )
                elif stop_data.get("was_playing"):
                    await self.capability_worker.speak("Okay, Plex is stopped.")
                else:
                    await self.capability_worker.speak("Nothing is playing from Plex right now.")
                return

            # STT sometimes finalizes early, leaving only a wake/command artifact
            # (e.g. "Play"). Rather than browse-all and play random tracks, ask
            # once what to play — but only when the request carries no generic
            # intent ("music"/"audiobook"/"something") we could honor as-is.
            normalized_request = normalize_text(user_request)
            if not _meaningful_query(user_request) and not any(
                word in normalized_request
                for word in ["music", "audiobook", "book", "something", "anything"]
            ):
                user_request = await self.capability_worker.run_io_loop(
                    "What would you like me to play from Plex?"
                )
                if not user_request or exit_requested(user_request):
                    await self.capability_worker.speak("Okay, I will leave Plex closed.")
                    return

            # --- Resume audiobook ---
            if resume_requested(user_request):
                resume_state = self._read_resume_state()
                choice = item_from_resume_state(resume_state)
                if not choice:
                    await self.capability_worker.speak(
                        "I do not have an audiobook saved to resume yet. Ask me to play an audiobook from Plex first."
                    )
                    return
                offset_ms = int(resume_state.get("offset_ms") or 0)
                await self.capability_worker.speak(f"Resuming {describe_item(choice)} from Plex.")
                self._write_resume_state(build_resume_state(choice, offset_ms))
                if devkit_mode:
                    await self._music_mode_on()
                    final_position_ms, _, _ = await self._devkit_playback(client, choice, offset_ms)
                    elapsed_ms = max(0, final_position_ms - offset_ms)
                else:
                    elapsed_ms = await self._stream_audio(client.stream_url_for(choice, offset_ms=offset_ms))
                current_state = self._read_resume_state() or build_resume_state(choice, offset_ms)
                self._write_resume_state(updated_resume_state(current_state, elapsed_ms))
                return

            # --- New search ---
            # (Music was already ducked right after the initial request.)
            await self.capability_worker.speak("Searching your Plex audio libraries.")
            if devkit_mode:
                items = await self._devkit_search(client, user_request)
            else:
                items = client.search_audio(user_request)

            choice = choose_best_item(items, user_request)
            if not choice:
                await self.capability_worker.speak(
                    "I could not find matching music or audiobooks in Plex. Try a title, artist, album, or book name."
                )
                await self._devkit_call("plex_duck", ["100"], 5)
                return

            # --- Audiobook: single track with resume state ---
            if choice.media_type == "audiobook":
                await self.capability_worker.speak(f"Playing {describe_item(choice)} from Plex.")
                self._write_resume_state(build_resume_state(choice, 0))
                if devkit_mode:
                    await self._music_mode_on()
                    final_position_ms, _, _ = await self._devkit_playback(client, choice, 0)
                    elapsed_ms = max(0, final_position_ms)
                else:
                    elapsed_ms = await self._stream_audio(client.stream_url_for(choice))
                current_state = self._read_resume_state() or build_resume_state(choice, 0)
                self._write_resume_state(updated_resume_state(current_state, elapsed_ms))
                return

            # --- Music: build a queue from all matching results, play in order ---
            # items is already sorted by relevance score (best first from _devkit_search /
            # search_plex_audio). Prefer the same artist, else wrap to include lower-ranked tracks.
            queue = build_music_queue(items, choice)
            i = 0
            offset = 0
            await self.capability_worker.speak(f"Playing {describe_item(choice)} from Plex.")
            if devkit_mode:
                await self._music_mode_on()
            while i < len(queue):
                if not devkit_mode:
                    await self._stream_audio(client.stream_url_for(queue[i]))
                    return  # cloud path cannot detect track end, play one track only
                position_ms, action, payload = await self._devkit_playback(
                    client, queue[i], offset, allow_switch=True
                )
                if action == "stopped":
                    return
                if action == "lost":
                    await self.capability_worker.speak(
                        "I lost contact with the OpenHome device, so I'm stopping Plex playback."
                    )
                    return
                if action == "skip":
                    i += 1
                    offset = 0
                    continue
                if action == "new_request":
                    new_items = await self._devkit_search(client, payload)
                    new_choice = choose_best_item(new_items, payload)
                    if new_choice:
                        await self.capability_worker.speak(f"Playing {describe_item(new_choice)} from Plex.")
                        queue = build_music_queue(new_items, new_choice)
                        i = 0
                        offset = 0
                        continue
                    await self.capability_worker.speak("I couldn't find that. Continuing the music.")
                    offset = position_ms  # resume current track where it was
                    continue  # same i — plex_play restarts the track at offset, full volume
                # action == "ended"
                i += 1
                offset = 0

            # Queue exhausted. If the last action was a skip, mpv is still playing ducked — stop it.
            await self._stop_devkit_playback()

        except Exception as exc:
            self.worker.editor_logging_handler.error(f"[PlexAudio] Error: {exc}")
            await self.capability_worker.speak(plex_error_message(base_url, exc, devkit_mode))
            # If we ducked in-progress music but never started new playback, restore its volume.
            await self._devkit_call("plex_duck", ["100"], 5)
        finally:
            # Never orphan mpv: if a DevKit playback was started, stop it before
            # tearing down music mode. This runs even on asyncio.CancelledError
            # (which the except above does NOT catch), which is the one path that
            # left mpv playing for minutes in the incident. Guard so finally can
            # never raise while the session is tearing down.
            # _devkit_playback_started is always initialized at the top of run()
            # (the platform sandbox forbids getattr).
            if devkit_mode and self._devkit_playback_started:
                try:
                    await self._stop_devkit_playback()
                except Exception as exc:
                    self.worker.editor_logging_handler.warning(
                        f"[PlexAudio] Teardown stop failed: {exc}"
                    )
            await self._music_mode_off()
            self.capability_worker.resume_normal_flow()

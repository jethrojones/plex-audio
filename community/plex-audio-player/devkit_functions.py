"""DevKit-side Plex client and audio player for the Plex Audio Player Ability.

This file runs on the OpenHome DevKit (a normal Python environment on the
device), not in the standard Ability runtime. It talks to Plex over the LAN
and plays audio through a local player process, so the OpenHome cloud runtime
never needs a route to the Plex server.

Protocol: every registered function prints exactly one JSON object to stdout:
    {"success": bool, "data": {...}, "error": null | {"code": str, "message": str}}
main.py reads that payload from result["output"] of send_devkit_capability_action().

Helper functions for search scoring are duplicated from main.py on purpose:
the two files run in different runtimes and cannot import each other.
"""

import json
import os
import pathlib
import re
import shutil
import signal
import socket as _socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    from devkit_utils.devkit_logging import web_logger as log
except Exception:  # standalone/local test runs without devkit_utils
    import logging

    log = logging.getLogger("plex-audio-devkit")
    log.addHandler(logging.NullHandler())

REQUEST_TIMEOUT = 15
AUDIO_SEARCH_TYPE = "10"
MAX_SEARCH_RESULTS = 30
STATE_FILE = "/home/openhome/.plex_audio_state.json"
MPV_IPC_SOCKET = "/tmp/mpv-plex.sock"
PLAYER_PRIORITY = ["mpv", "ffplay", "cvlc", "mpg123"]


# ---------------------------------------------------------------------------
# Search helpers (duplicated from main.py — split runtimes cannot share code)
# ---------------------------------------------------------------------------

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


def _meaningful_query(user_text):
    """Strip noise words; returns '' when only generic words remain (e.g. 'play music')."""
    text = str(user_text or "").strip()
    for pattern in _SANITIZE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" .,-")


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


def infer_media_type(library_title, title, creator, collection, duration_ms):
    haystack = normalize_text(" ".join([library_title, title, creator, collection]))
    if any(term in haystack for term in ["audiobook", "audio book", "books", "novel", "chapter"]):
        return "audiobook"
    if duration_ms and duration_ms >= 30 * 60 * 1000:
        return "audiobook"
    return "music"


def score_item(item, user_text, requested_type=None):
    query = normalize_text(sanitize_search_query(user_text))
    item_text = normalize_text(" ".join([item["title"], item["creator"], item["collection"]]))
    score = 0
    if requested_type and item["media_type"] == requested_type:
        score += 100
    if query and query in item_text:
        score += 50
    query_tokens = [t for t in query.split() if t and t not in STOPWORDS]
    title_collection_tokens = set(
        normalize_text(" ".join([item["title"], item["collection"]])).split()
    )
    creator_tokens = set(normalize_text(item["creator"]).split())
    for token in query_tokens:
        if token in creator_tokens:
            score += 40
        elif token in title_collection_tokens:
            score += 10
    title_text = normalize_text(item["title"])
    if title_text and title_text in query:
        score += 15
    return score


# ---------------------------------------------------------------------------
# Plex HTTP helpers
# ---------------------------------------------------------------------------

def plex_url(base_url, path, token="", params=None):
    base = str(base_url or "").strip().rstrip("/")
    raw_path = str(path or "")
    if raw_path.startswith("http://") or raw_path.startswith("https://"):
        full_url = raw_path
    else:
        full_url = base + "/" + raw_path.lstrip("/")
    parsed = urllib.parse.urlsplit(full_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    if params:
        query.update({str(key): str(value) for key, value in params.items() if value is not None})
    if token:
        query["X-Plex-Token"] = str(token)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def _http_get_text(url, timeout=REQUEST_TIMEOUT):
    request = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_tracks(xml_text):
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)
    items = []
    for track in root.findall(".//Track"):
        title = track.attrib.get("title") or "Untitled track"
        creator = track.attrib.get("grandparentTitle") or track.attrib.get("parentTitle") or "Unknown artist"
        collection = track.attrib.get("parentTitle") or track.attrib.get("grandparentTitle") or ""
        library_title = track.attrib.get("librarySectionTitle") or ""
        duration = track.attrib.get("duration")
        media = track.find(".//Media")
        if not duration and media is not None:
            duration = media.attrib.get("duration")
        try:
            duration_ms = int(duration) if duration else 0
        except ValueError:
            duration_ms = 0
        part = track.find(".//Part")
        part_key = part.attrib.get("key") if part is not None else ""
        if not part_key:
            continue
        items.append(
            {
                "title": title,
                "creator": creator,
                "collection": collection,
                "media_type": infer_media_type(library_title, title, creator, collection, duration_ms),
                "part_key": part_key,
                "duration_ms": duration_ms,
                "rating_key": track.attrib.get("ratingKey") or track.attrib.get("key") or "",
            }
        )
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


def _artist_first_candidates(base_url, token, user_text, audio_sections, get_text):
    """Try to resolve the query to an artist and return that artist's tracks.

    Returns a list of track dicts on an artist hit, or None when no artist
    matched (so the caller falls back to the generic search path).
    """
    phrases = candidate_artist_phrases(user_text)
    if not phrases:
        return None
    for section_key, section_title, section_type in audio_sections:
        if not section_key:
            continue
        path = "/library/sections/%s/all" % section_key
        for phrase in phrases:
            try:
                artists = _parse_artist_directories(
                    get_text(plex_url(base_url, path, token, {"type": "8", "title": phrase}))
                )
            except Exception as exc:
                log.warning("[PlexAudio] Artist lookup failed for %r: %s", phrase, exc)
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
                    tracks = _parse_tracks(
                        get_text(plex_url(base_url, path, token, {"type": AUDIO_SEARCH_TYPE, "artist.id": rating_key}))
                    )
                except Exception as exc:
                    log.warning("[PlexAudio] Artist track lookup failed for %r: %s", artist_title, exc)
                    continue
                if tracks:
                    log.info("[PlexAudio] Artist-first hit %r -> %d tracks", artist_title, len(tracks))
                    return tracks
    return None


def search_plex_audio(base_url, token, user_text, get_text=_http_get_text):
    query = sanitize_search_query(user_text)
    # When noise words are all that remain (e.g. "play music"), treat as browse-all.
    meaningful = _meaningful_query(user_text)
    requested_type = detect_requested_media_type(user_text)
    candidates = []

    audio_sections = []
    try:
        sections_xml = get_text(plex_url(base_url, "/library/sections", token))
        root = ET.fromstring(sections_xml)
        audio_sections = [
            (d.attrib.get("key", ""), d.attrib.get("title", ""), d.attrib.get("type", ""))
            for d in root.findall(".//Directory")
            if d.attrib.get("type", "") in {"artist", "music"}
        ]
        log.info("[PlexAudio] Audio sections found: %s", audio_sections)
    except Exception as exc:
        log.warning("[PlexAudio] Section discovery failed: %s", exc)

    # Artist-first: resolve the meaningful query to an artist and use their
    # tracks as the candidate pool, skipping the generic search entirely.
    if meaningful:
        artist_tracks = _artist_first_candidates(base_url, token, user_text, audio_sections, get_text)
        if artist_tracks:
            deduped = []
            seen = set()
            for item in artist_tracks:
                key = item["part_key"] or "|".join([item["title"], item["creator"], item["collection"]])
                if key not in seen:
                    seen.add(key)
                    deduped.append(item)
            deduped.sort(key=lambda item: score_item(item, user_text, requested_type), reverse=True)
            return deduped[:MAX_SEARCH_RESULTS]

    if meaningful:
        try:
            candidates.extend(_parse_tracks(get_text(plex_url(base_url, "/search", token, {"query": query}))))
        except Exception as exc:
            log.warning("[PlexAudio] Global search failed: %s", exc)

    try:
        for section_key, section_title, section_type in audio_sections:
            if not section_key:
                continue
            path = "/library/sections/%s/all" % section_key
            if meaningful:
                title_matches = _parse_tracks(
                    get_text(plex_url(base_url, path, token, {"type": AUDIO_SEARCH_TYPE, "title": query}))
                )
                candidates.extend(title_matches)
                if not title_matches:
                    scanned = _parse_tracks(get_text(plex_url(base_url, path, token, {"type": AUDIO_SEARCH_TYPE})))
                    candidates.extend(item for item in scanned if score_item(item, user_text, None) > 0)
            else:
                # Generic request ("play music", "play audiobook") — return all tracks from audio sections.
                scanned = _parse_tracks(get_text(plex_url(base_url, path, token, {"type": AUDIO_SEARCH_TYPE})))
                log.info("[PlexAudio] Browse-all for section %r: %d tracks", section_title, len(scanned))
                candidates.extend(scanned)
    except Exception as exc:
        log.warning("[PlexAudio] Section search failed: %s", exc)

    deduped = []
    seen = set()
    for item in candidates:
        key = item["part_key"] or "|".join([item["title"], item["creator"], item["collection"]])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    deduped.sort(key=lambda item: score_item(item, user_text, requested_type), reverse=True)
    return deduped[:MAX_SEARCH_RESULTS]


# ---------------------------------------------------------------------------
# Player process management
# ---------------------------------------------------------------------------

def detect_player(which=shutil.which):
    for player in PLAYER_PRIORITY:
        if which(player):
            return player
    return None


PULSE_USER_ID = "1000"
PULSE_ENV_EXTRAS = {
    "PULSE_RUNTIME_PATH": "/run/user/%s/pulse" % PULSE_USER_ID,
    "PULSE_SERVER": "unix:/run/user/%s/pulse/native" % PULSE_USER_ID,
    "XDG_RUNTIME_DIR": "/run/user/%s" % PULSE_USER_ID,
}


def build_player_command(player, url, offset_seconds=0):
    offset_seconds = max(0, int(offset_seconds or 0))
    if player == "mpv":
        return ["mpv", "--no-video", "--no-terminal", "--really-quiet", "--ao=pulse",
                "--input-ipc-server=%s" % MPV_IPC_SOCKET,
                "--start=%d" % offset_seconds, url]
    if player == "ffplay":
        command = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        if offset_seconds:
            command.extend(["-ss", str(offset_seconds)])
        command.append(url)
        return command
    if player == "cvlc":
        command = ["cvlc", "--play-and-exit", "--quiet"]
        if offset_seconds:
            command.append("--start-time=%d" % offset_seconds)
        command.append(url)
        return command
    if player == "mpg123":
        # mpg123 has no reliable time-based seek for remote streams; start from 0.
        return ["mpg123", "-q", url]
    return None


def _read_state():
    try:
        return json.loads(pathlib.Path(STATE_FILE).read_text())
    except Exception:
        return None


def _write_state(state):
    try:
        pathlib.Path(STATE_FILE).write_text(json.dumps(state))
    except Exception as exc:
        log.warning("[PlexAudio] Could not write state file: %s", exc)


def _pid_running(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _kill_player(state):
    pid = (state or {}).get("pid")
    if not pid or not _pid_running(pid):
        return False
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return False
    return True


def current_position_ms(state, now=None):
    if not state:
        return 0
    now = time.time() if now is None else now
    offset_ms = int(state.get("offset_ms") or 0)
    started_at = float(state.get("started_at") or now)
    position = offset_ms + max(0, int((now - started_at) * 1000))
    duration_ms = int(state.get("duration_ms") or 0)
    if duration_ms:
        position = min(position, duration_ms)
    return position


def _print_payload(success, data=None, error=None):
    sys.stdout.write(json.dumps({"success": bool(success), "data": data or {}, "error": error}) + "\n")


# ---------------------------------------------------------------------------
# Registered DevKit functions
# ---------------------------------------------------------------------------

def plex_diagnose(base_url="", token=""):
    """Report Plex reachability from the DevKit and available player binaries.

    Always succeeds: a diagnosis is a report, not an error. main.py branches on
    the data fields so it can speak the precise problem to the user.
    """
    data = {
        "player": detect_player(),
        "players_checked": PLAYER_PRIORITY,
        "plex_reachable": False,
        "detail": "",
    }
    if not str(base_url or "").strip():
        data["detail"] = "No Plex base URL provided."
    else:
        try:
            identity_xml = _http_get_text(plex_url(base_url, "/identity", token), timeout=8)
            root = ET.fromstring(identity_xml)
            data["plex_reachable"] = True
            data["machine_identifier"] = root.attrib.get("machineIdentifier", "")
            data["version"] = root.attrib.get("version", "")
        except Exception as exc:
            log.warning("[PlexAudio] Plex unreachable from DevKit: %s", exc)
            data["detail"] = str(exc)
    _print_payload(True, data)


def plex_search(base_url="", token="", user_text=""):
    try:
        items = search_plex_audio(base_url, token, user_text)
        _print_payload(True, {"items": items})
    except Exception as exc:
        log.warning("[PlexAudio] Search failed: %s", exc)
        _print_payload(False, {}, {"code": "search_failed", "message": str(exc)})


PLEX_CLIENT_ID = "openhome-devkit-plexaudio"


def _report_timeline(plex_state, state):
    """Report playback position to Plex so it shows in the dashboard."""
    try:
        base_url = str(state.get("base_url") or "").strip()
        token = str(state.get("token") or "")
        rating_key = str(state.get("rating_key") or "")
        if not base_url or not rating_key:
            return
        position = current_position_ms(state)
        duration = int(state.get("duration_ms") or 0)
        params = {
            "ratingKey": rating_key,
            "key": "/library/metadata/%s" % rating_key,
            "state": plex_state,
            "time": str(position),
            "duration": str(duration),
            "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
            "X-Plex-Product": "OpenHome PlexAudio",
            "X-Plex-Version": "1.0",
            "X-Plex-Platform": "Linux",
        }
        _http_get_text(plex_url(base_url, "/:/timeline", token, params), timeout=5)
    except Exception as exc:
        log.debug("[PlexAudio] Timeline report failed (non-fatal): %s", exc)


def plex_play(base_url="", token="", part_key="", offset_ms="0", duration_ms="0", title="", rating_key=""):
    try:
        _kill_player(_read_state())
        player = detect_player()
        if not player:
            _print_payload(
                False,
                {},
                {"code": "no_player", "message": "No audio player found. Install one with: sudo apt install mpv"},
            )
            return
        try:
            offset = max(0, int(float(offset_ms or 0)))
        except ValueError:
            offset = 0
        url = plex_url(base_url, part_key, token)
        command = build_player_command(player, url, offset // 1000)
        env = os.environ.copy()
        env.update(PULSE_ENV_EXTRAS)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        try:
            duration = max(0, int(float(duration_ms or 0)))
        except ValueError:
            duration = 0
        state = {
            "pid": process.pid,
            "player": player,
            "base_url": base_url,
            "token": token,
            "part_key": part_key,
            "rating_key": rating_key,
            "title": title,
            "offset_ms": offset,
            "duration_ms": duration,
            "started_at": time.time(),
        }
        _write_state(state)
        _report_timeline("playing", state)
        log.info("[PlexAudio] Started %s (pid %s) for %s", player, process.pid, title or part_key)
        _print_payload(True, {"player": player, "pid": process.pid, "offset_ms": offset})
    except Exception as exc:
        log.warning("[PlexAudio] Play failed: %s", exc)
        _print_payload(False, {}, {"code": "play_failed", "message": str(exc)})


def plex_stop():
    state = _read_state()
    position_ms = current_position_ms(state)
    was_playing = _kill_player(state)
    if state:
        state["stopped_at"] = time.time()
        state["pid"] = None
        state["offset_ms"] = position_ms
        _write_state(state)
        _report_timeline("stopped", state)
    _print_payload(True, {"was_playing": was_playing, "position_ms": position_ms})


def plex_status():
    state = _read_state()
    playing = bool(state and _pid_running(state.get("pid")))
    if playing:
        _report_timeline("playing", state)
    _print_payload(
        True,
        {
            "playing": playing,
            "position_ms": current_position_ms(state) if state else 0,
            "title": (state or {}).get("title") or "",
            "part_key": (state or {}).get("part_key") or "",
        },
    )


def plex_duck(volume="20"):
    """Lower or restore mpv's volume via its IPC socket.

    Call with volume="20" to duck before the bot speaks, volume="100" to
    restore after. Uses mpv's JSON IPC protocol — no PulseAudio permission
    issues. Silently succeeds if mpv isn't currently playing.
    """
    try:
        vol = max(0, min(100, int(float(volume or 20))))
        if not os.path.exists(MPV_IPC_SOCKET):
            _print_payload(True, {"volume": None, "message": "no mpv socket"})
            return
        cmd = json.dumps({"command": ["set_property", "volume", vol]}) + "\n"
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(MPV_IPC_SOCKET)
        s.sendall(cmd.encode())
        s.close()
        _print_payload(True, {"volume": vol})
    except Exception as exc:
        log.warning("[PlexAudio] Duck failed: %s", exc)
        _print_payload(False, {}, {"code": "duck_failed", "message": str(exc)})


FUNCTION_REGISTRY = {
    "plex_diagnose": plex_diagnose,
    "plex_search": plex_search,
    "plex_play": plex_play,
    "plex_stop": plex_stop,
    "plex_status": plex_status,
    "plex_duck": plex_duck,
}

if __name__ == "__main__":
    function_name = sys.argv[1]
    FUNCTION_REGISTRY[function_name](*sys.argv[2:])

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
MAX_SEARCH_RESULTS = 12
STATE_FILE = "/home/openhome/.plex_audio_state.json"
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


def normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


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
    query_tokens = query.split()
    item_tokens = item_text.split()
    for token in query_tokens:
        if token and token in item_tokens:
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


def search_plex_audio(base_url, token, user_text, get_text=_http_get_text):
    query = sanitize_search_query(user_text)
    # When noise words are all that remain (e.g. "play music"), treat as browse-all.
    meaningful = _meaningful_query(user_text)
    requested_type = detect_requested_media_type(user_text)
    candidates = []

    if meaningful:
        try:
            candidates.extend(_parse_tracks(get_text(plex_url(base_url, "/search", token, {"query": query}))))
        except Exception as exc:
            log.warning("[PlexAudio] Global search failed: %s", exc)

    try:
        sections_xml = get_text(plex_url(base_url, "/library/sections", token))
        root = ET.fromstring(sections_xml)
        audio_sections = [
            (d.attrib.get("key", ""), d.attrib.get("title", ""), d.attrib.get("type", ""))
            for d in root.findall(".//Directory")
            if d.attrib.get("type", "") in {"artist", "music"}
        ]
        log.info("[PlexAudio] Audio sections found: %s", audio_sections)
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


def plex_play(base_url="", token="", part_key="", offset_ms="0", duration_ms="0", title=""):
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
        url = plex_url(base_url, part_key, token, {"download": "1"})
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
        _write_state(
            {
                "pid": process.pid,
                "player": player,
                "part_key": part_key,
                "title": title,
                "offset_ms": offset,
                "duration_ms": duration,
                "started_at": time.time(),
            }
        )
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
    _print_payload(True, {"was_playing": was_playing, "position_ms": position_ms})


def plex_status():
    state = _read_state()
    playing = bool(state and _pid_running(state.get("pid")))
    _print_payload(
        True,
        {
            "playing": playing,
            "position_ms": current_position_ms(state) if state else 0,
            "title": (state or {}).get("title") or "",
            "part_key": (state or {}).get("part_key") or "",
        },
    )


FUNCTION_REGISTRY = {
    "plex_diagnose": plex_diagnose,
    "plex_search": plex_search,
    "plex_play": plex_play,
    "plex_stop": plex_stop,
    "plex_status": plex_status,
}

if __name__ == "__main__":
    function_name = sys.argv[1]
    FUNCTION_REGISTRY[function_name](*sys.argv[2:])

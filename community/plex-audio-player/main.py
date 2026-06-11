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
REQUEST_TIMEOUT = 15
STREAM_CHUNK_SIZE = 64 * 1024
AUDIO_SEARCH_TYPE = "10"
RESUME_STATE_KEY = "plex_audio_last_audiobook"
RESUME_END_THRESHOLD_MS = 60 * 1000
EXIT_WORDS = {"stop", "exit", "quit", "cancel", "nevermind", "never mind", "done", "bye"}
DEVKIT_DIAGNOSE_TIMEOUT = 25
DEVKIT_SEARCH_TIMEOUT = 45
DEVKIT_PLAY_TIMEOUT = 20
DEVKIT_CONTROL_TIMEOUT = 15
PLAYBACK_LISTEN_WINDOW_SECONDS = 15
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


def normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


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
    query_tokens = query.split()
    item_tokens = item_text.split()
    for token in query_tokens:
        if token and token in item_tokens:
            score += 10
    title_text = normalize_text(item.title)
    if title_text and title_text in query:
        score += 15
    return score


def choose_best_item(items, user_text):
    if not items:
        return None
    requested_type = detect_requested_media_type(user_text)
    return max(items, key=lambda item: score_item(item, user_text, requested_type))


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


def choose_best_plex_connection(connections, preferred_subnets=None):
    candidates = [conn for conn in connections if conn and conn.get("base_url")]
    if not candidates:
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


def parse_plex_tv_resources(xml_text, server_name=None, machine_identifier=None, preferred_subnets=None):
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
    return choose_best_plex_connection(connections, preferred_subnets)


def discover_plex_tv_resource(account_token, server_name=None, machine_identifier=None, preferred_subnets=None, logger=None):
    token = str(account_token or "").strip()
    if not token:
        return None
    try:
        url = "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&X-Plex-Token=" + _url_quote(token)
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return parse_plex_tv_resources(response.text, server_name, machine_identifier, preferred_subnets)
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


def _plex_search_audio(client, user_text):
    query = sanitize_search_query(user_text)
    meaningful = _meaningful_query(user_text)
    requested_type = detect_requested_media_type(user_text)
    candidates = []

    if meaningful:
        try:
            candidates.extend(client.parse_tracks(client.get_xml("/search", {"query": query})))
        except Exception as exc:
            if client.logger:
                client.logger.warning(f"[PlexAudio] Global search failed: {exc}")

    try:
        sections_xml = client.get_xml("/library/sections")
        root = ET.fromstring(sections_xml)
        for directory in root.findall(".//Directory"):
            section_type = directory.attrib.get("type", "")
            section_key = directory.attrib.get("key", "")
            if section_type in {"artist", "music"} and section_key:
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


def playback_stop_requested(user_text):
    text = normalize_text(user_text)
    if not text:
        return False
    tokens = text.split()
    # Music playing near the mic produces noisy transcriptions ("don't stop believing"),
    # so single stop words only count in short utterances.
    if len(tokens) <= 4 and any(token in STOP_WORDS for token in tokens):
        return True
    return any(phrase in text for phrase in STOP_PHRASES)


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
        data, error = await self._devkit_call(
            "plex_diagnose", [base_url, token or ""], DEVKIT_DIAGNOSE_TIMEOUT
        )
        logger = self.worker.editor_logging_handler
        if data is None:
            logger.warning(f"[PlexAudio] DevKit unavailable, using cloud streaming path: {error}")
            return None
        logger.info(f"[PlexAudio] DevKit diagnose: {data}")
        return data

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

    async def _devkit_playback(self, client, item, offset_ms):
        """Play on the DevKit and wait for finish or a stop command. Returns final position in ms."""
        data, error = await self._devkit_call(
            "plex_play",
            [
                client.base_url,
                client.token or "",
                item.part_key,
                str(int(offset_ms or 0)),
                str(int(item.duration_ms or 0)),
                item.title,
            ],
            DEVKIT_PLAY_TIMEOUT,
        )
        if data is None:
            raise RuntimeError(f"DevKit playback failed: {error}")

        started = time.monotonic()
        position_ms = int(offset_ms or 0)
        status_failures = 0
        while time.monotonic() - started < MAX_PLAYBACK_SECONDS:
            status, status_error = await self._devkit_call("plex_status", [], DEVKIT_CONTROL_TIMEOUT)
            if status is None:
                status_failures += 1
                self.worker.editor_logging_handler.warning(f"[PlexAudio] Status check failed: {status_error}")
                if status_failures >= 3:
                    break
            else:
                status_failures = 0
                position_ms = int(status.get("position_ms") or position_ms)
                if not status.get("playing"):
                    return position_ms
            heard = await self._listen_during_playback()
            if heard and playback_stop_requested(heard):
                stop_data, _ = await self._devkit_call("plex_stop", [], DEVKIT_CONTROL_TIMEOUT)
                if stop_data is not None:
                    position_ms = int(stop_data.get("position_ms") or position_ms)
                await self.capability_worker.speak("Okay, stopping Plex.")
                return position_ms
        await self._devkit_call("plex_stop", [], DEVKIT_CONTROL_TIMEOUT)
        return position_ms

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

    async def run(self):
        base_url = ""
        devkit_mode = False
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
            if devkit_info is None:
                devkit_mode = False
            elif not devkit_info.get("plex_reachable"):
                await self.capability_worker.speak(
                    "Your DevKit is online, but it cannot reach the Plex server at the configured address. "
                    "Check that Plex is running and that plex base url is the server's local network address, like its LAN IP and port 32400."
                )
                return
            elif not devkit_info.get("player"):
                await self.capability_worker.speak(
                    "Your DevKit can reach Plex, but it has no audio player installed. "
                    "On the DevKit, run sudo apt install mpv, then ask me again."
                )
                return
            else:
                devkit_mode = True

            user_request = await self._get_initial_request()
            if not user_request or exit_requested(user_request):
                await self.capability_worker.speak("Okay, I will leave Plex closed.")
                return

            resume_state = None
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
            else:
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
                    return
                offset_ms = 0
                await self.capability_worker.speak(f"Playing {describe_item(choice)} from Plex.")

            if choice.media_type == "audiobook":
                self._write_resume_state(build_resume_state(choice, offset_ms))
            if devkit_mode:
                final_position_ms = await self._devkit_playback(client, choice, offset_ms)
                elapsed_ms = max(0, final_position_ms - int(offset_ms or 0))
            else:
                elapsed_ms = await self._stream_audio(client.stream_url_for(choice, offset_ms=offset_ms))
            if choice.media_type == "audiobook":
                current_state = self._read_resume_state() or build_resume_state(choice, offset_ms)
                self._write_resume_state(updated_resume_state(current_state, elapsed_ms))
        except Exception as exc:
            self.worker.editor_logging_handler.error(f"[PlexAudio] Error: {exc}")
            await self.capability_worker.speak(plex_error_message(base_url, exc, devkit_mode))
        finally:
            self.capability_worker.resume_normal_flow()

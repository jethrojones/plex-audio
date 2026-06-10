import collections
import re
import time
import xml.etree.ElementTree as ET

import requests
from src.agent.capability import MatchingCapability
from src.main import AgentWorker
from src.agent.capability_worker import CapabilityWorker

PLEX_BASE_URL_KEY = "plex_base_url"
PLEX_TOKEN_KEY = "plex_token"
REQUEST_TIMEOUT = 15
STREAM_CHUNK_SIZE = 64 * 1024
AUDIO_SEARCH_TYPE = "10"
RESUME_STATE_KEY = "plex_audio_last_audiobook"
RESUME_END_THRESHOLD_MS = 60 * 1000
EXIT_WORDS = {"stop", "exit", "quit", "cancel", "nevermind", "never mind", "done", "bye"}

PlexAudioItem = collections.namedtuple(
    "PlexAudioItem",
    ["title", "creator", "collection", "media_type", "part_key", "duration_ms", "rating_key"],
)

PlexAudioClientState = collections.namedtuple(
    "PlexAudioClientState",
    ["base_url", "token", "logger", "url", "get_xml", "parse_tracks", "search_audio", "stream_url_for"],
)

URL_SAFE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


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
    replacements = [
        r"\bplay\b",
        r"\bfrom plex\b",
        r"\bplex\b",
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
    for pattern in replacements:
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
    for token in query.split():
        if token and token in item_text:
            score += 10
    if item.title and normalize_text(item.title) in query:
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
    query["X-Plex-Token"] = client.token

    url = base + "?" + _encode_query(query)
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
    candidates = []

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
                candidates.extend(client.parse_tracks(client.get_xml(path, {"type": AUDIO_SEARCH_TYPE, "title": query})))
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


class PlexAudioPlayerCapability(MatchingCapability):
    worker: AgentWorker = None
    capability_worker: CapabilityWorker = None

    # Do not change following tag of register capability
    #{{register capability}}

    def call(self, worker: AgentWorker):
        self.worker = worker
        self.capability_worker = CapabilityWorker(self)
        self.worker.session_tasks.create(self.run())

    def _get_required_config(self):
        base_url = self.capability_worker.get_api_keys(PLEX_BASE_URL_KEY)
        token = self.capability_worker.get_api_keys(PLEX_TOKEN_KEY)
        missing = []
        if not base_url:
            missing.append(PLEX_BASE_URL_KEY)
        if not token:
            missing.append(PLEX_TOKEN_KEY)
        return base_url, token, missing

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
        try:
            base_url, token, missing = self._get_required_config()
            if missing:
                await self.capability_worker.speak(
                    "Plex Audio Player needs setup first. Add plex base url and plex token in OpenHome Settings under API Keys."
                )
                return

            client = PlexAudioClient(base_url, token, self.worker.editor_logging_handler)
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
            elapsed_ms = await self._stream_audio(client.stream_url_for(choice, offset_ms=offset_ms))
            if choice.media_type == "audiobook":
                current_state = self._read_resume_state() or build_resume_state(choice, offset_ms)
                self._write_resume_state(updated_resume_state(current_state, elapsed_ms))
        except Exception as exc:
            self.worker.editor_logging_handler.error(f"[PlexAudio] Error: {exc}")
            await self.capability_worker.speak(
                "Sorry, Plex playback did not work. Check that your Plex server URL is reachable and your token is valid."
            )
        finally:
            self.capability_worker.resume_normal_flow()

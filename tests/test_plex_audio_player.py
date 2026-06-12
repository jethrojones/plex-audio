import importlib.util
import json
import sys
import types
from pathlib import Path


def load_ability_module():
    # Stub OpenHome modules so helper functions/classes can be tested locally.
    src = types.ModuleType("src")
    agent = types.ModuleType("src.agent")
    capability = types.ModuleType("src.agent.capability")
    main_mod = types.ModuleType("src.main")
    capability_worker = types.ModuleType("src.agent.capability_worker")

    class MatchingCapability:
        pass

    class AgentWorker:
        pass

    class CapabilityWorker:
        def __init__(self, capability):
            self.capability = capability

    setattr(capability, "MatchingCapability", MatchingCapability)
    setattr(main_mod, "AgentWorker", AgentWorker)
    setattr(capability_worker, "CapabilityWorker", CapabilityWorker)

    sys.modules["src"] = src
    sys.modules["src.agent"] = agent
    sys.modules["src.agent.capability"] = capability
    sys.modules["src.main"] = main_mod
    sys.modules["src.agent.capability_worker"] = capability_worker

    path = Path(__file__).resolve().parents[1] / "community" / "plex-audio-player" / "main.py"
    spec = importlib.util.spec_from_file_location("plex_audio_player_main", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_plex_url_adds_token_and_params():
    mod = load_ability_module()
    client = mod.PlexAudioClient("http://plex.example:32400/", "abc123")

    url = client.url("/library/sections", {"type": "10", "query": "Miles Davis"})

    assert url.startswith("http://plex.example:32400/library/sections?")
    assert "X-Plex-Token=abc123" in url
    assert "type=10" in url
    assert "query=Miles+Davis" in url


def test_plex_url_preserves_existing_query_params():
    mod = load_ability_module()
    client = mod.PlexAudioClient("https://plex.example", "tok")

    url = client.url("/library/parts/99?download=1", {"X-Plex-Client-Identifier": "openhome"})

    assert url.startswith("https://plex.example/library/parts/99?")
    assert "download=1" in url
    assert "X-Plex-Client-Identifier=openhome" in url
    assert "X-Plex-Token=tok" in url


def test_parse_tracks_extracts_music_and_audiobook_metadata():
    mod = load_ability_module()
    client = mod.PlexAudioClient("http://plex", "tok")
    xml = """
    <MediaContainer>
      <Track title="So What" grandparentTitle="Miles Davis" parentTitle="Kind of Blue" librarySectionTitle="Music">
        <Media duration="545000"><Part key="/library/parts/1/file.mp3" /></Media>
      </Track>
      <Track title="Chapter 1" grandparentTitle="The Hobbit" parentTitle="J. R. R. Tolkien" librarySectionTitle="Audiobooks" ratingKey="abc123">
        <Media duration="3600000"><Part key="/library/parts/2/file.m4b" /></Media>
      </Track>
    </MediaContainer>
    """

    items = client.parse_tracks(xml)

    assert len(items) == 2
    assert items[0].title == "So What"
    assert items[0].creator == "Miles Davis"
    assert items[0].collection == "Kind of Blue"
    assert items[0].media_type == "music"
    assert items[0].part_key == "/library/parts/1/file.mp3"
    assert items[1].media_type == "audiobook"
    assert items[1].title == "Chapter 1"
    assert items[1].creator == "The Hobbit"


def test_choose_best_item_prefers_requested_audiobook():
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Song called Dune", "Band", "Album", "music", "/song.mp3", 180000, "song1"),
        mod.PlexAudioItem("Chapter 1", "Dune", "Frank Herbert", "audiobook", "/dune.m4b", 3600000, "book1"),
    ]

    choice = mod.choose_best_item(items, "play the audiobook Dune")

    assert choice.part_key == "/dune.m4b"


def test_choose_best_item_prefers_requested_music():
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Chapter 1", "Blue", "Author", "audiobook", "/book.m4b", 3600000, "book1"),
        mod.PlexAudioItem("Blue in Green", "Miles Davis", "Kind of Blue", "music", "/blue.mp3", 320000, "song1"),
    ]

    choice = mod.choose_best_item(items, "play music blue in green")

    assert choice.part_key == "/blue.mp3"


def test_choose_best_item_returns_none_for_empty_results():
    mod = load_ability_module()

    assert mod.choose_best_item([], "play something") is None


def test_score_item_ignores_titles_that_normalize_to_empty_text():
    mod = load_ability_module()
    item = mod.PlexAudioItem("Гимн России", "Lyube", "Рассея", "music", "/song.mp3", 180000, "song1")

    assert mod.score_item(item, "Miles Davis") == 0


def test_score_item_does_not_treat_partial_words_as_token_matches():
    mod = load_ability_module()
    item = mod.PlexAudioItem("Smiles", "Choir", "Album", "music", "/song.mp3", 180000, "song1")

    assert mod.score_item(item, "Miles Davis") == 0


def test_search_audio_falls_back_to_library_scan_for_artist_matches():
    mod = load_ability_module()
    sections_xml = """
    <MediaContainer>
      <Directory key="6" title="Music" type="artist" />
    </MediaContainer>
    """
    empty_xml = '<MediaContainer size="0" />'
    all_tracks_xml = """
    <MediaContainer>
      <Track title="Blue in Green" grandparentTitle="Miles Davis" parentTitle="Kind of Blue" ratingKey="song1">
        <Media duration="320000"><Part key="/library/parts/blue/file.mp3" /></Media>
      </Track>
    </MediaContainer>
    """
    calls = []

    class FakeClient:
        logger = None

        def get_xml(self, path, params=None):
            calls.append((path, params or {}))
            if path == "/search":
                return empty_xml
            if path == "/library/sections":
                return sections_xml
            if path == "/library/sections/6/all" and (params or {}).get("title") == "Miles Davis":
                return empty_xml
            if path == "/library/sections/6/all" and "title" not in (params or {}):
                return all_tracks_xml
            return empty_xml

        def parse_tracks(self, xml_text):
            return mod._plex_parse_tracks(self, xml_text)

    items = mod._plex_search_audio(FakeClient(), "play music Miles Davis")

    assert [(item.title, item.creator) for item in items] == [("Blue in Green", "Miles Davis")]
    assert ("/library/sections/6/all", {"type": mod.AUDIO_SEARCH_TYPE}) in calls


def test_sanitize_search_query_removes_provider_words():
    mod = load_ability_module()

    assert mod.sanitize_search_query("play the audiobook The Hobbit from Plex") == "The Hobbit"
    assert mod.sanitize_search_query("plex music Miles Davis") == "Miles Davis"
    assert mod.sanitize_search_query("Play some Metallica for myPlex library.") == "Metallica"
    assert mod.sanitize_search_query("play Metallica on plex") == "Metallica"
    assert mod.sanitize_search_query("play metallica in plex") == "metallica"


def test_resume_requested_detects_continue_my_audiobook_phrases():
    mod = load_ability_module()

    assert mod.resume_requested("continue my audiobook")
    assert mod.resume_requested("resume my book")
    assert mod.resume_requested("pick up where I left off in Plex")
    assert not mod.resume_requested("play the audiobook Dune")


def test_stream_url_for_resume_adds_offset_parameter():
    mod = load_ability_module()
    client = mod.PlexAudioClient("https://plex.example", "tok")
    item = mod.PlexAudioItem("Chapter 1", "Dune", "Frank Herbert", "audiobook", "/library/parts/7/file.m4b", 3600000, "rating7")

    url = client.stream_url_for(item, offset_ms=125000)

    assert "offset=125" in url
    assert "X-Plex-Token=tok" in url


def test_build_resume_state_only_stores_audiobooks():
    mod = load_ability_module()
    book = mod.PlexAudioItem("Chapter 1", "Dune", "Frank Herbert", "audiobook", "/dune.m4b", 3600000, "rating7")
    song = mod.PlexAudioItem("So What", "Miles Davis", "Kind of Blue", "music", "/song.mp3", 545000, "song1")

    state = mod.build_resume_state(book, offset_ms=90000)

    assert state["title"] == "Chapter 1"
    assert state["creator"] == "Dune"
    assert state["offset_ms"] == 90000
    assert state["part_key"] == "/dune.m4b"
    assert mod.build_resume_state(song, offset_ms=90000) is None


def test_item_from_resume_state_rehydrates_audiobook():
    mod = load_ability_module()
    state = {
        "title": "Chapter 2",
        "creator": "Dune",
        "collection": "Frank Herbert",
        "media_type": "audiobook",
        "part_key": "/dune2.m4b",
        "duration_ms": 3600000,
        "rating_key": "rating8",
        "offset_ms": 120000,
    }

    item = mod.item_from_resume_state(state)

    assert item.title == "Chapter 2"
    assert item.media_type == "audiobook"
    assert item.part_key == "/dune2.m4b"


def test_update_resume_offset_clamps_near_end_to_zero():
    mod = load_ability_module()
    state = {"duration_ms": 3600000, "offset_ms": 3590000}

    updated = mod.updated_resume_state(state, elapsed_ms=30000)

    assert updated["offset_ms"] == 0


def test_plex_error_message_explains_network_timeout_for_lan_url():
    mod = load_ability_module()
    message = mod.plex_error_message(
        "http://192.168.0.20:32400",
        Exception("Connection to 192.168.0.20 timed out"),
    )

    assert "cannot reach your Plex server" in message
    assert "remote Plex URL" in message


def test_plex_error_message_explains_unauthorized_token():
    mod = load_ability_module()
    message = mod.plex_error_message("https://plex.example", Exception("401 Unauthorized"))

    assert "token" in message.lower()


def test_plex_url_omits_token_when_blank_for_lan_no_auth():
    mod = load_ability_module()
    client = mod.PlexAudioClient("http://plex.local:32400", "")

    url = client.url("/library/sections", {"type": "10"})

    assert url == "http://plex.local:32400/library/sections?type=10"


def test_parse_plex_tv_resources_prefers_matching_local_connection():
    mod = load_ability_module()
    xml = """
    <MediaContainer>
      <Device name="ombee" clientIdentifier="acdc" accessToken="server-token">
        <Connection uri="http://192.168.0.20:32400" local="1" />
        <Connection uri="http://10.0.0.136:32400" local="1" />
        <Connection uri="https://76-121-135-187.example.plex.direct:32400" local="0" />
      </Device>
    </MediaContainer>
    """

    connection = mod.parse_plex_tv_resources(xml, server_name="ombee", preferred_subnets=["10."])

    assert connection["base_url"] == "http://10.0.0.136:32400"
    assert connection["token"] == "server-token"


def test_parse_plex_tv_resources_can_match_machine_identifier():
    mod = load_ability_module()
    xml = """
    <MediaContainer>
      <Device name="Other" clientIdentifier="wrong" accessToken="wrong-token">
        <Connection uri="http://10.0.0.2:32400" local="1" />
      </Device>
      <Device name="ombee" clientIdentifier="acdc74e9" accessToken="right-token">
        <Connection uri="http://10.0.0.136:32400" local="1" />
      </Device>
    </MediaContainer>
    """

    connection = mod.parse_plex_tv_resources(xml, machine_identifier="acdc74e9")

    assert connection["base_url"] == "http://10.0.0.136:32400"
    assert connection["token"] == "right-token"



def test_ability_main_avoids_forbidden_socket_import():
    source = (Path(__file__).resolve().parents[1] / "community" / "plex-audio-player" / "main.py").read_text()

    assert "import socket" not in source
    assert "socket." not in source

def test_choose_best_connection_prefers_matching_preferred_subnet():
    mod = load_ability_module()
    connections = [
        {"base_url": "http://192.168.0.20:32400", "local": True, "token": "tok"},
        {"base_url": "http://10.0.0.136:32400", "local": True, "token": "tok"},
    ]

    chosen = mod.choose_best_plex_connection(connections, preferred_subnets=["10."])

    assert chosen["base_url"] == "http://10.0.0.136:32400"


def load_devkit_module():
    path = Path(__file__).resolve().parents[1] / "community" / "plex-audio-player" / "devkit_functions.py"
    spec = importlib.util.spec_from_file_location("plex_audio_player_devkit", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_playback_stop_requested_detects_short_stop_commands():
    mod = load_ability_module()

    assert mod.playback_stop_requested("stop")
    assert mod.playback_stop_requested("pause it please")
    assert mod.playback_stop_requested("stop the music")
    assert mod.playback_stop_requested("okay that's enough music for now thank you")
    assert not mod.playback_stop_requested("don't stop believing hold on to that feeling streetlight people")
    assert not mod.playback_stop_requested("")


def test_playback_stop_requested_detects_kill_switch_phrases():
    """The fresh-session stop kill-switch relies on these phrases firing True."""
    mod = load_ability_module()

    assert mod.playback_stop_requested("stop the music")
    assert mod.playback_stop_requested("pause")
    assert mod.playback_stop_requested("stop")
    assert mod.playback_stop_requested("stop plex")
    assert mod.playback_stop_requested("turn it off")


def test_is_stale_repeat_same_text_within_window_is_stale():
    mod = load_ability_module()

    # Same normalized utterance arriving 3s after the last one we processed.
    assert mod.is_stale_repeat("Stop the music", "stop the music", 100.0, 103.0) is True


def test_is_stale_repeat_same_text_after_window_is_fresh():
    mod = load_ability_module()

    # Same utterance but the window (5s) has elapsed — treat as a fresh command.
    assert mod.is_stale_repeat("stop the music", "stop the music", 100.0, 106.0) is False


def test_is_stale_repeat_different_text_is_fresh():
    mod = load_ability_module()

    assert mod.is_stale_repeat("next song", "stop the music", 100.0, 101.0) is False


def test_is_stale_repeat_empty_heard_is_never_stale():
    mod = load_ability_module()

    # Empty heard is handled by the empty-result throttle, not the dedupe.
    assert mod.is_stale_repeat("", "stop the music", 100.0, 101.0) is False
    assert mod.is_stale_repeat(None, "stop the music", 100.0, 101.0) is False
    # No prior text recorded yet.
    assert mod.is_stale_repeat("stop", None, None, 101.0) is False


def test_parse_devkit_payload_handles_clean_and_noisy_output():
    mod = load_ability_module()

    clean = mod.parse_devkit_payload('{"success": true, "data": {"playing": false}, "error": null}')
    assert clean["success"] is True

    noisy = mod.parse_devkit_payload('startup notice\n{"success": true, "data": {}, "error": null}\n')
    assert noisy["success"] is True

    assert mod.parse_devkit_payload("") is None
    assert mod.parse_devkit_payload("not json at all") is None


def test_items_from_search_payload_builds_audio_items():
    mod = load_ability_module()
    payload = {
        "items": [
            {
                "title": "So What",
                "creator": "Miles Davis",
                "collection": "Kind of Blue",
                "media_type": "music",
                "part_key": "/library/parts/1/file.mp3",
                "duration_ms": 545000,
                "rating_key": "song1",
            },
            {"title": "No part key", "part_key": ""},
        ]
    }

    items = mod.items_from_search_payload(payload)

    assert len(items) == 1
    assert items[0].title == "So What"
    assert items[0].media_type == "music"
    assert items[0].duration_ms == 545000
    assert mod.items_from_search_payload(None) == []


def test_devkit_plex_url_adds_token_and_download_params():
    dev = load_devkit_module()

    url = dev.plex_url("http://10.0.0.136:32400/", "/library/parts/7/file.m4b", "tok", {"download": "1"})

    assert url.startswith("http://10.0.0.136:32400/library/parts/7/file.m4b?")
    assert "X-Plex-Token=tok" in url
    assert "download=1" in url


def test_devkit_detect_player_uses_priority_order():
    dev = load_devkit_module()

    assert dev.detect_player(which=lambda name: name in {"ffplay", "mpg123"}) == "ffplay"
    assert dev.detect_player(which=lambda name: name == "mpv") == "mpv"
    assert dev.detect_player(which=lambda name: None) is None


def test_devkit_build_player_command_includes_offset_for_mpv_and_ffplay():
    dev = load_devkit_module()

    mpv = dev.build_player_command("mpv", "http://plex/stream", offset_seconds=125)
    ffplay = dev.build_player_command("ffplay", "http://plex/stream", offset_seconds=125)

    assert "--start=125" in mpv
    assert mpv[-1] == "http://plex/stream"
    assert "-ss" in ffplay
    assert ffplay[ffplay.index("-ss") + 1] == "125"


def test_devkit_search_scores_sorts_and_caps_results():
    dev = load_devkit_module()
    sections_xml = '<MediaContainer><Directory key="6" title="Music" type="artist" /></MediaContainer>'
    empty_xml = '<MediaContainer size="0" />'
    all_tracks_xml = """
    <MediaContainer>
      <Track title="Enter Sandman" grandparentTitle="Metallica" parentTitle="Metallica" ratingKey="s1">
        <Media duration="330000"><Part key="/library/parts/1/file.mp3" /></Media>
      </Track>
      <Track title="Unrelated" grandparentTitle="Someone Else" parentTitle="Other" ratingKey="s2">
        <Media duration="200000"><Part key="/library/parts/2/file.mp3" /></Media>
      </Track>
    </MediaContainer>
    """

    def fake_get(url, timeout=None):
        if "/search" in url:
            return empty_xml
        if "/library/sections/6/all" in url and "title=" in url:
            return empty_xml
        if "/library/sections/6/all" in url:
            return all_tracks_xml
        return sections_xml

    items = dev.search_plex_audio("http://10.0.0.136:32400", "tok", "play Metallica on plex", get_text=fake_get)

    assert [item["title"] for item in items] == ["Enter Sandman"]
    assert items[0]["media_type"] == "music"


def test_devkit_diagnose_always_reports_instead_of_erroring(capsys):
    dev = load_devkit_module()
    original_get = dev._http_get_text
    dev._http_get_text = lambda url, timeout=None: (_ for _ in ()).throw(OSError("Connection refused"))
    try:
        dev.plex_diagnose("http://10.0.0.136:32400", "tok")
    finally:
        dev._http_get_text = original_get

    payload = json.loads(capsys.readouterr().out.strip())

    assert payload["success"] is True
    assert payload["data"]["plex_reachable"] is False
    assert "Connection refused" in payload["data"]["detail"]
    assert "player" in payload["data"]


def test_devkit_current_position_ms_accumulates_from_offset():
    dev = load_devkit_module()
    state = {"offset_ms": 5000, "started_at": 1000.0, "duration_ms": 60000}

    assert dev.current_position_ms(state, now=1010.0) == 15000
    assert dev.current_position_ms(state, now=2000.0) == 60000
    assert dev.current_position_ms(None) == 0


def test_meaningful_query_strips_generic_request_to_empty():
    mod = load_ability_module()

    assert mod._meaningful_query("play music") == ""
    assert mod._meaningful_query("play music from Plex") == ""
    assert mod._meaningful_query("play audiobook from my plex library") == ""
    assert mod._meaningful_query("play some music please") == ""
    assert mod._meaningful_query("play Dune audiobook") == "Dune"
    assert mod._meaningful_query("Kind of Blue") == "Kind of Blue"


def test_search_audio_browse_all_for_generic_request():
    """Generic 'play music' request should return all section tracks, not an empty list."""
    mod = load_ability_module()
    sections_xml = '<MediaContainer><Directory key="6" title="Music" type="artist" /></MediaContainer>'
    all_tracks_xml = """
    <MediaContainer>
      <Track title="So What" grandparentTitle="Miles Davis" parentTitle="Kind of Blue" ratingKey="s1">
        <Media duration="545000"><Part key="/library/parts/1/file.mp3" /></Media>
      </Track>
      <Track title="Blue in Green" grandparentTitle="Miles Davis" parentTitle="Kind of Blue" ratingKey="s2">
        <Media duration="320000"><Part key="/library/parts/2/file.mp3" /></Media>
      </Track>
    </MediaContainer>
    """

    class FakeClient:
        logger = None

        def get_xml(self, path, params=None):
            if path == "/library/sections":
                return sections_xml
            return all_tracks_xml

        def parse_tracks(self, xml_text):
            return mod._plex_parse_tracks(self, xml_text)

    items = mod._plex_search_audio(FakeClient(), "play music")

    assert len(items) == 2


def test_devkit_meaningful_query_strips_generic_request_to_empty():
    dev = load_devkit_module()

    assert dev._meaningful_query("play music") == ""
    assert dev._meaningful_query("play audiobook from plex") == ""
    assert dev._meaningful_query("play Dune audiobook") == "Dune"


def test_devkit_search_browse_all_for_generic_request():
    """Generic 'play music' request should return all section tracks without score filtering."""
    dev = load_devkit_module()
    sections_xml = '<MediaContainer><Directory key="6" title="Music" type="artist" /></MediaContainer>'
    all_tracks_xml = """
    <MediaContainer>
      <Track title="So What" grandparentTitle="Miles Davis" parentTitle="Kind of Blue" ratingKey="s1">
        <Media duration="545000"><Part key="/library/parts/1/file.mp3" /></Media>
      </Track>
      <Track title="Unrelated" grandparentTitle="Other Artist" parentTitle="Other Album" ratingKey="s2">
        <Media duration="200000"><Part key="/library/parts/2/file.mp3" /></Media>
      </Track>
    </MediaContainer>
    """

    def fake_get(url, timeout=None):
        if "/library/sections/6/all" in url:
            return all_tracks_xml
        return sections_xml

    items = dev.search_plex_audio("http://10.0.0.136:32400", "tok", "play music", get_text=fake_get)

    assert len(items) == 2


def test_playback_skip_requested_detects_short_next_and_skip_commands():
    mod = load_ability_module()

    assert mod.playback_skip_requested("next")
    assert mod.playback_skip_requested("skip")
    assert mod.playback_skip_requested("next song please")
    assert mod.playback_skip_requested("skip this song")
    assert not mod.playback_skip_requested("stop")
    assert not mod.playback_skip_requested(
        "don't skip a beat in this long noisy transcription of lyrics next"
    )
    assert not mod.playback_skip_requested("")


def test_playback_new_request_detects_play_mid_playback():
    mod = load_ability_module()

    assert mod.playback_new_request("play 3 doors down")
    assert mod.playback_new_request("play metallica from plex")
    assert not mod.playback_new_request("stop playing")  # stop wins
    assert not mod.playback_new_request("next")
    assert not mod.playback_new_request("i love this playlist")  # \bplay\b must not match "playlist"
    assert not mod.playback_new_request("")


def test_choose_best_item_picks_artist_over_garbled_wakeword_title():
    """Garbled wake word 'I'll put home' must not outrank the requested artist."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("I’ll Be Home for Christmas", "Bing Crosby", "Holiday", "music", "/xmas.mp3", 180000, "x1"),
        mod.PlexAudioItem("Enter Sandman", "Metallica", "Metallica", "music", "/sandman.mp3", 330000, "m1"),
    ]

    choice = mod.choose_best_item(items, "I'll put home Play Metallica from Plex.")

    assert choice.part_key == "/sandman.mp3"


def test_choose_best_item_returns_none_for_meaningful_query_with_only_junk():
    """A meaningful query that matches nothing should play nothing, not junk."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Twinkle Twinkle", "Kids Choir", "Nursery", "music", "/a.mp3", 60000, "a"),
        mod.PlexAudioItem("ABC Song", "Kids Choir", "Nursery", "music", "/b.mp3", 60000, "b"),
    ]

    assert mod.choose_best_item(items, "play metallica") is None


def test_choose_best_item_still_plays_for_generic_request():
    """Generic 'play music' (empty meaningful query) still plays a zero-score item."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Twinkle Twinkle", "Kids Choir", "Nursery", "music", "/a.mp3", 60000, "a"),
        mod.PlexAudioItem("ABC Song", "Kids Choir", "Nursery", "music", "/b.mp3", 60000, "b"),
    ]

    choice = mod.choose_best_item(items, "play music")

    assert choice is not None
    assert choice.part_key in {"/a.mp3", "/b.mp3"}


def test_candidate_artist_phrases_drops_stopwords_and_orders_longest_first():
    mod = load_ability_module()

    phrases = mod.candidate_artist_phrases("I'll put home Play Metallica from Plex.")

    assert "metallica" in phrases
    # Only "metallica" survives stopword filtering (i, ll, put, home, play, from, plex).
    assert phrases == ["metallica"]

    # Multi-token artist keeps longest-first ordering with single stopwords gone.
    multi = mod.candidate_artist_phrases("play 3 doors down from plex")
    assert multi[0] == "3 doors down"
    assert multi.index("3 doors down") < multi.index("doors")
    assert "from" not in multi and "plex" not in multi


def test_candidate_artist_phrases_matches_devkit_implementation():
    mod = load_ability_module()
    dev = load_devkit_module()

    text = "I'll put home Play Metallica from Plex."
    assert mod.candidate_artist_phrases(text) == dev.candidate_artist_phrases(text)


def test_playback_stop_and_skip_respect_negation():
    mod = load_ability_module()

    # Negated stop must not stop.
    assert mod.playback_stop_requested("please don't stop the music") is False
    # Plain stop command must stop, even at 6 tokens.
    assert mod.playback_stop_requested("stop the music right now please") is True
    # Negated skip must not skip.
    assert mod.playback_skip_requested("don't skip this one") is False


def test_devkit_score_item_weights_creator_above_title():
    dev = load_devkit_module()
    by_artist = {
        "title": "Some Song",
        "creator": "Metallica",
        "collection": "Album",
        "media_type": "music",
        "part_key": "/a.mp3",
        "duration_ms": 1,
        "rating_key": "a",
    }
    by_title = {
        "title": "Metallica Tribute",
        "creator": "Cover Band",
        "collection": "Album",
        "media_type": "music",
        "part_key": "/b.mp3",
        "duration_ms": 1,
        "rating_key": "b",
    }

    assert dev.score_item(by_artist, "play metallica") > dev.score_item(by_title, "play metallica")


def test_devkit_search_artist_first_returns_only_that_artist():
    """An artist hit should return the artist's tracks, skipping generic search."""
    dev = load_devkit_module()
    sections_xml = '<MediaContainer><Directory key="6" title="Music" type="artist" /></MediaContainer>'
    artist_dir_xml = (
        '<MediaContainer>'
        '<Directory ratingKey="20495" type="artist" title="Metallica" />'
        '</MediaContainer>'
    )
    artist_tracks_xml = """
    <MediaContainer>
      <Track title="Enter Sandman" grandparentTitle="Metallica" parentTitle="Metallica" ratingKey="s1">
        <Media duration="330000"><Part key="/library/parts/1/file.mp3" /></Media>
      </Track>
      <Track title="One" grandparentTitle="Metallica" parentTitle="...And Justice" ratingKey="s2">
        <Media duration="446000"><Part key="/library/parts/2/file.mp3" /></Media>
      </Track>
    </MediaContainer>
    """

    def fake_get(url, timeout=None):
        if "type=8" in url and "title=metallica" in url:
            return artist_dir_xml
        if "artist.id=20495" in url:
            return artist_tracks_xml
        if "/library/sections/6/all" in url:
            return "<MediaContainer />"  # generic path would return nothing
        return sections_xml

    items = dev.search_plex_audio("http://10.0.0.136:32400", "", "play metallica", get_text=fake_get)

    assert {item["creator"] for item in items} == {"Metallica"}
    assert {item["title"] for item in items} == {"Enter Sandman", "One"}


def test_build_music_queue_prefers_same_artist_then_falls_back_to_wraparound():
    mod = load_ability_module()
    choice = mod.PlexAudioItem("Enter Sandman", "Metallica", "Metallica", "music", "/m1.mp3", 330000, "m1")
    other_metallica = mod.PlexAudioItem("One", "Metallica", "...And Justice", "music", "/m2.mp3", 446000, "m2")
    third_metallica = mod.PlexAudioItem("Fade to Black", "Metallica", "Ride the Lightning", "music", "/m3.mp3", 418000, "m3")
    unrelated = mod.PlexAudioItem("So What", "Miles Davis", "Kind of Blue", "music", "/jz.mp3", 545000, "jz")

    # Two or more same-artist items: choice first, then other same-artist tracks only.
    items = [choice, other_metallica, unrelated, third_metallica]
    queue = mod.build_music_queue(items, choice)
    assert [it.part_key for it in queue] == ["/m1.mp3", "/m2.mp3", "/m3.mp3"]

    # Single same-artist match: fall back to wrap-around of all items starting at choice.
    single_items = [unrelated, choice]
    single_queue = mod.build_music_queue(single_items, choice)
    assert [it.part_key for it in single_queue] == ["/m1.mp3", "/jz.mp3"]


# ---------------------------------------------------------------------------
# Fix 1: artist_matches_query — strict artist acceptance
# ---------------------------------------------------------------------------

def test_artist_matches_query_rejects_partial_token_overlap():
    """'James Taylor' must NOT match 'Play Taylor Swift' — 'james' is missing."""
    mod = load_ability_module()
    assert mod.artist_matches_query("James Taylor", "Play Taylor Swift") is False


def test_artist_matches_query_accepts_exact_single_token():
    """'Metallica' — every meaningful token ('metallica') is in the query."""
    mod = load_ability_module()
    assert mod.artist_matches_query("Metallica", "I'll put home Play Metallica from Plex.") is True


def test_artist_matches_query_accepts_stopword_artist_after_stripping():
    """'The Beatles' — stopword 'the' is stripped, leaving ['beatles'] which is in the query."""
    mod = load_ability_module()
    assert mod.artist_matches_query("The Beatles", "play the beatles") is True


def test_artist_matches_query_accepts_multi_token_artist():
    """'3 Doors Down' — tokens ['3','doors','down'] all appear in 'play 3 doors down'."""
    mod = load_ability_module()
    assert mod.artist_matches_query("3 Doors Down", "play 3 doors down") is True


def test_artist_matches_query_parity_main_devkit():
    """main.py and devkit_functions.py implementations must behave identically."""
    mod = load_ability_module()
    dev = load_devkit_module()

    cases = [
        ("James Taylor", "Play Taylor Swift"),
        ("Metallica", "I'll put home Play Metallica from Plex."),
        ("The Beatles", "play the beatles"),
        ("3 Doors Down", "play 3 doors down"),
    ]
    for artist, query in cases:
        assert mod.artist_matches_query(artist, query) == dev.artist_matches_query(artist, query), (
            f"Parity failure for artist_matches_query({artist!r}, {query!r})"
        )


# ---------------------------------------------------------------------------
# Fix 2: choose_best_item token-coverage guard
# ---------------------------------------------------------------------------

def test_choose_best_item_rejects_james_taylor_for_taylor_swift_query():
    """'Play Taylor Swift': 'taylor' matches but 'swift' does not — coverage fails (1 of 2)."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Fire and Rain", "James Taylor", "Sweet Baby James", "music", "/jt.mp3", 200000, "jt1"),
        mod.PlexAudioItem("Carolina in My Mind", "James Taylor", "James Taylor", "music", "/jt2.mp3", 210000, "jt2"),
    ]
    assert mod.choose_best_item(items, "Play Taylor Swift") is None


def test_choose_best_item_accepts_3_doors_down_track():
    """'play 3 doors down': meaningful tokens are {3, doors, down} (3 tokens).
    A track by '3 Doors Down' matches all 3 → 3 > 1.5 → passes coverage."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Kryptonite", "3 Doors Down", "The Better Life", "music", "/3dd.mp3", 220000, "3dd1"),
    ]
    choice = mod.choose_best_item(items, "play 3 doors down")
    assert choice is not None
    assert choice.part_key == "/3dd.mp3"


# ---------------------------------------------------------------------------
# Task 2: Plex account linking (PIN OAuth flow) — pure helpers
# ---------------------------------------------------------------------------

def test_link_requested_detects_account_link_phrases():
    mod = load_ability_module()

    assert mod.link_requested("link my plex account")
    assert mod.link_requested("sign in to plex")
    assert mod.link_requested("log in to plex")
    assert mod.link_requested("connect plex")
    assert mod.link_requested("authorize plex")


def test_link_requested_ignores_ordinary_play_requests():
    mod = load_ability_module()

    assert not mod.link_requested("play metallica from plex")
    assert not mod.link_requested("play music")
    # "link" without "plex" must not fire.
    assert not mod.link_requested("link my spotify account")
    # "plex" alone without an account/auth verb must not fire.
    assert not mod.link_requested("play the audiobook Dune from plex")


def test_parse_pin_response_extracts_id_and_code():
    mod = load_ability_module()

    # Field names observed live at plex.tv/api/v2/pins: integer "id", string "code".
    pin_id, code = mod.parse_pin_response({"id": 1747085891, "code": "z0d0isvxc4glwo1", "authToken": None})

    assert pin_id == 1747085891
    assert code == "z0d0isvxc4glwo1"


def test_parse_pin_response_returns_none_on_missing_fields():
    mod = load_ability_module()

    assert mod.parse_pin_response({}) == (None, None)
    assert mod.parse_pin_response({"id": 5}) == (None, None)
    assert mod.parse_pin_response({"code": "abcd"}) == (None, None)
    assert mod.parse_pin_response(None) == (None, None)


def test_parse_pin_poll_returns_token_when_claimed():
    mod = load_ability_module()

    assert mod.parse_pin_poll({"authToken": "secret-account-token"}) == "secret-account-token"


def test_parse_pin_poll_returns_none_when_authtoken_null():
    mod = load_ability_module()

    # An unclaimed PIN reports authToken: null (confirmed against the live endpoint).
    assert mod.parse_pin_poll({"authToken": None}) is None
    assert mod.parse_pin_poll({}) is None
    assert mod.parse_pin_poll(None) is None


def test_spell_out_code_renders_discrete_characters():
    mod = load_ability_module()

    assert mod.spell_out_code("ABC7") == "A. B. C. 7."
    assert mod.spell_out_code("") == ""


# ---------------------------------------------------------------------------
# Task 3: remote-access fallback — choose_best_plex_connection(prefer_remote=True)
# ---------------------------------------------------------------------------

def test_choose_best_connection_prefer_remote_picks_non_local_candidate():
    mod = load_ability_module()
    connections = [
        {"base_url": "http://192.168.0.20:32400", "local": True, "token": "tok"},
        {"base_url": "http://10.0.0.136:32400", "local": True, "token": "tok"},
        {"base_url": "https://76-121-135-187.example.plex.direct:32400", "local": False, "token": "tok"},
    ]

    chosen = mod.choose_best_plex_connection(connections, prefer_remote=True)

    assert chosen["base_url"] == "https://76-121-135-187.example.plex.direct:32400"


def test_choose_best_connection_prefer_remote_returns_none_when_only_local():
    mod = load_ability_module()
    connections = [
        {"base_url": "http://192.168.0.20:32400", "local": True, "token": "tok"},
        {"base_url": "http://10.0.0.136:32400", "local": True, "token": "tok"},
    ]

    assert mod.choose_best_plex_connection(connections, prefer_remote=True) is None


def test_choose_best_connection_default_behavior_unchanged_when_prefer_remote_false():
    mod = load_ability_module()
    connections = [
        {"base_url": "http://192.168.0.20:32400", "local": True, "token": "tok"},
        {"base_url": "http://10.0.0.136:32400", "local": True, "token": "tok"},
        {"base_url": "https://76-121-135-187.example.plex.direct:32400", "local": False, "token": "tok"},
    ]

    # With prefer_remote False (default), the preferred-subnet rule still wins.
    chosen = mod.choose_best_plex_connection(connections, preferred_subnets=["10."])
    assert chosen["base_url"] == "http://10.0.0.136:32400"
    # And with no preferred subnets, it picks the first local candidate as before.
    chosen_local = mod.choose_best_plex_connection(connections)
    assert chosen_local["base_url"] == "http://192.168.0.20:32400"

import asyncio
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


def test_config_includes_exact_common_plex_trigger():
    config_path = Path(__file__).resolve().parents[1] / "community" / "plex-audio-player" / "config.json"
    config = json.loads(config_path.read_text())

    assert "play music from my Plex" in config["trigger_words"]


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


# Fixtures below mirror the REAL plex.tv /api/v2/resources response shape:
# lowercase <resources>/<resource>/<connections>/<connection> elements.
# (The original fixtures used the legacy v1 <Device>/<Connection> shape, which
# matched the code but not the live API — that's how the parse bug shipped.)
# All tokens, plex.direct hashes, and machine identifiers here are FAKE.

def test_parse_plex_tv_resources_prefers_matching_local_connection():
    mod = load_ability_module()
    xml = """
    <resources>
      <resource name="ombee" clientIdentifier="fake0123456789abcdef0123456789abcdef0123" accessToken="fake-server-token" provides="server" publicAddress="203.0.113.10">
        <connections>
          <connection uri="http://192.168.0.20:32400" address="192.168.0.20" port="32400" protocol="http" local="1" relay="0" />
          <connection uri="http://10.0.0.136:32400" address="10.0.0.136" port="32400" protocol="http" local="1" relay="0" />
          <connection uri="https://203-0-113-10.fakehash1234567890abcdef.plex.direct:32400" address="203.0.113.10" port="32400" protocol="https" local="0" relay="0" />
        </connections>
      </resource>
    </resources>
    """

    connection = mod.parse_plex_tv_resources(xml, server_name="ombee", preferred_subnets=["10."])

    assert connection["base_url"] == "http://10.0.0.136:32400"
    assert connection["token"] == "fake-server-token"


def test_parse_plex_tv_resources_can_match_machine_identifier():
    mod = load_ability_module()
    xml = """
    <resources>
      <resource name="Other" clientIdentifier="fakewrongidentifier" accessToken="fake-wrong-token" provides="server">
        <connections>
          <connection uri="http://10.0.0.2:32400" local="1" relay="0" />
        </connections>
      </resource>
      <resource name="ombee" clientIdentifier="fake0123456789abcdef" accessToken="fake-right-token" provides="server">
        <connections>
          <connection uri="http://10.0.0.136:32400" local="1" relay="0" />
        </connections>
      </resource>
    </resources>
    """

    connection = mod.parse_plex_tv_resources(xml, machine_identifier="fake0123456789abcdef")

    assert connection["base_url"] == "http://10.0.0.136:32400"
    assert connection["token"] == "fake-right-token"


def test_parse_plex_connections_skips_non_server_resources():
    mod = load_ability_module()
    xml = """
    <resources>
      <resource name="Some Player" clientIdentifier="fakeplayerid" accessToken="fake-player-token" provides="player">
        <connections>
          <connection uri="http://10.0.0.50:32500" local="1" relay="0" />
        </connections>
      </resource>
      <resource name="ombee" clientIdentifier="fakeserverid" accessToken="fake-server-token" provides="server">
        <connections>
          <connection uri="http://10.0.0.136:32400" local="1" relay="0" />
        </connections>
      </resource>
    </resources>
    """

    connections = mod._parse_plex_connections(xml)

    assert len(connections) == 1
    assert connections[0]["name"] == "ombee"


def test_parse_remote_plex_connections_returns_relay_before_direct_and_skips_local():
    mod = load_ability_module()
    xml = """
    <resources>
      <resource name="ombee" clientIdentifier="fake0123456789abcdef" accessToken="fake-server-token" provides="server">
        <connections>
          <connection uri="http://10.0.0.136:32400" local="1" relay="0" />
          <connection uri="https://203-0-113-10.fakehash1234567890abcdef.plex.direct:32400" local="0" relay="0" />
          <connection uri="https://198-51-100-20.fakehash1234567890abcdef.plex.direct:8443" local="0" relay="1" />
        </connections>
      </resource>
    </resources>
    """

    remote = mod.parse_remote_plex_connections(xml)

    # Only the two non-local connections, with the relay endpoint first so cloud
    # playback uses the relay path before trying residential-port direct access.
    assert len(remote) == 2
    assert remote[0]["base_url"] == "https://198-51-100-20.fakehash1234567890abcdef.plex.direct:8443"
    assert remote[0]["relay"] is True
    assert remote[1]["base_url"] == "https://203-0-113-10.fakehash1234567890abcdef.plex.direct:32400"
    assert remote[1]["relay"] is False
    # The local connection is excluded.
    assert all(conn["base_url"] != "http://10.0.0.136:32400" for conn in remote)


def test_parse_plex_connections_falls_back_to_legacy_device_format():
    # Legacy v1-style <Device>/<Connection> responses must still parse.
    mod = load_ability_module()
    xml = """
    <MediaContainer>
      <Device name="ombee" clientIdentifier="fake0123456789abcdef" accessToken="fake-server-token" provides="server">
        <Connection uri="http://10.0.0.136:32400" local="1" relay="0" />
        <Connection uri="https://198-51-100-20.fakehash1234567890abcdef.plex.direct:8443" local="0" relay="1" />
      </Device>
    </MediaContainer>
    """

    connections = mod._parse_plex_connections(xml, server_name="ombee")

    assert len(connections) == 2
    assert connections[0]["token"] == "fake-server-token"
    assert connections[0]["machine_identifier"] == "fake0123456789abcdef"
    remote = mod.remote_plex_connections(connections)
    assert len(remote) == 1
    assert remote[0]["relay"] is True


def test_remote_plex_connections_orders_relay_before_direct():
    mod = load_ability_module()
    connections = [
        {"base_url": "https://relay.example.plex.direct:443", "local": False, "relay": True, "token": "tok"},
        {"base_url": "http://192.168.0.20:32400", "local": True, "relay": False, "token": "tok"},
        {"base_url": "https://direct.example.plex.direct:32400", "local": False, "relay": False, "token": "tok"},
    ]

    remote = mod.remote_plex_connections(connections)

    assert [conn["base_url"] for conn in remote] == [
        "https://relay.example.plex.direct:443",
        "https://direct.example.plex.direct:32400",
    ]


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



def test_playback_stop_and_skip_respect_negation():
    mod = load_ability_module()

    # Negated stop must not stop.
    assert mod.playback_stop_requested("please don't stop the music") is False
    # Plain stop command must stop, even at 6 tokens.
    assert mod.playback_stop_requested("stop the music right now please") is True
    # Negated skip must not skip.
    assert mod.playback_skip_requested("don't skip this one") is False



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
# Fix 3: number-word normalization + full-artist-match overrides coverage guard
# ---------------------------------------------------------------------------

def test_normalize_text_maps_number_words_to_digits():
    """Standalone number words become digits, but embedded ones are untouched."""
    mod = load_ability_module()
    assert mod.normalize_text("three doors down") == "3 doors down"
    # "threesome" is one token, not the number word "three" — leave it alone.
    assert mod.normalize_text("threesome") == "threesome"


def test_choose_best_item_metallica_survives_garbled_plex_token():
    """Production STT: 'Plex' garbled to 'Platt'. A full Metallica artist match
    must beat the token-coverage guard ({metallica, platt} would be 1 of 2)."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Enter Sandman", "Metallica", "Metallica", "music", "/sandman.mp3", 330000, "m1"),
        mod.PlexAudioItem("I'll Be Home for Christmas", "Bing Crosby", "Holiday", "music", "/xmas.mp3", 180000, "x1"),
    ]
    choice = mod.choose_best_item(items, "Play music Metallica from Platt.")
    assert choice is not None
    assert choice.part_key == "/sandman.mp3"


def test_choose_best_item_three_doors_down_digit_artist():
    """Production STT: 'three doors down' (word) vs catalog '3 Doors Down' (digit).
    normalize_text maps 'three'->'3', so the full artist match is recognized."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Kryptonite", "3 Doors Down", "The Better Life", "music", "/3dd.mp3", 220000, "3dd1"),
    ]
    choice = mod.choose_best_item(items, "Playing music by three doors down Plex.")
    assert choice is not None
    assert choice.part_key == "/3dd.mp3"


def test_choose_best_item_taylor_swift_regression_still_none():
    """Regression: 'Play Taylor Swift' with only James Taylor items still None —
    artist_matches_query('James Taylor', ...) is False, so guards still apply."""
    mod = load_ability_module()
    items = [
        mod.PlexAudioItem("Fire and Rain", "James Taylor", "Sweet Baby James", "music", "/jt.mp3", 200000, "jt1"),
        mod.PlexAudioItem("Carolina in My Mind", "James Taylor", "James Taylor", "music", "/jt2.mp3", 210000, "jt2"),
    ]
    assert mod.choose_best_item(items, "Play Taylor Swift") is None


def test_candidate_artist_phrases_normalizes_three_to_digit():
    """The artist n-gram sent to Plex's ?title= filter must be '3 doors down'."""
    mod = load_ability_module()
    phrases = mod.candidate_artist_phrases("Playing music by three doors down Plex.")
    assert "3 doors down" in phrases


def test_normalize_text_additional_cases():
    """normalize_text handles number words, punctuation stripping, and unmapped words."""
    mod = load_ability_module()
    # Number-word conversion for all mapped digits.
    assert mod.normalize_text("one two three four five six seven eight nine ten zero") == \
        "1 2 3 4 5 6 7 8 9 10 0"
    # Non-mapped number words pass through unchanged.
    assert mod.normalize_text("eleven twentyone tenth") == "eleven twentyone tenth"
    # Punctuation is stripped; mixed case normalized.
    assert mod.normalize_text("Play music Metallica from Platt.") == "play music metallica from platt"
    # Full phrase with number word in the middle.
    assert mod.normalize_text("Playing music by three doors down Plex.") == \
        "playing music by 3 doors down plex"


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


# ---------------------------------------------------------------------------
# Cloud-primary restructure: mode-selection + cloud queue plumbing
# ---------------------------------------------------------------------------


class _FakeLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeWorker:
    def __init__(self):
        self.editor_logging_handler = _FakeLogger()
        self.session_tasks = _FakeSessionTasks()


class _FakeSessionTasks:
    async def sleep(self, seconds):
        return None

    def create(self, coroutine):
        return asyncio.create_task(coroutine)


def _make_capability(mod):
    """Instantiate the capability with fakes (no MatchingCapability __init__ needed)."""
    cap = mod.PlexAudioPlayerCapability()
    cap.worker = _FakeWorker()
    cap.capability_worker = None
    return cap


class _FakeLinkStorageWorker:
    def __init__(self, keys=None, file_text=None, persist_keys=True, persist_files=True):
        self.keys = dict(keys or {})
        self.file_text = file_text
        self.persist_keys = persist_keys
        self.persist_files = persist_files
        self.created = []
        self.updated = []
        self.file_writes = []
        self.spoken = []
        self.resumed = False

    def get_single_key(self, key):
        return self.keys.get(key)

    def create_key(self, key, value):
        self.created.append((key, value))
        if self.persist_keys:
            self.keys[key] = value

    def update_key(self, key, value):
        self.updated.append((key, value))
        if self.persist_keys:
            self.keys[key] = value

    async def check_if_file_exists(self, filename, in_ability_directory=False):
        return self.file_text is not None

    async def read_file(self, filename, in_ability_directory=False):
        return self.file_text

    async def write_file(self, filename, content, in_ability_directory=False, mode=None):
        self.file_writes.append(
            {
                "filename": filename,
                "content": content,
                "in_ability_directory": in_ability_directory,
                "mode": mode,
            }
        )
        if mode == "w" or self.file_text is None:
            if self.persist_files:
                self.file_text = content
        else:
            if self.persist_files:
                self.file_text += content

    async def speak(self, text):
        self.spoken.append(text)

    def resume_normal_flow(self):
        self.resumed = True


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakeFileOnlyStorageWorker:
    def __init__(self):
        self.file_text = None
        self.file_writes = []

    async def check_if_file_exists(self, filename, in_ability_directory=False):
        return self.file_text is not None

    async def read_file(self, filename, in_ability_directory=False):
        return self.file_text

    async def write_file(self, filename, content, in_ability_directory=False, mode=None):
        self.file_writes.append(
            {
                "filename": filename,
                "content": content,
                "in_ability_directory": in_ability_directory,
                "mode": mode,
            }
        )
        self.file_text = content


def test_write_link_state_uses_key_storage_and_linked_token_reads_it():
    mod = load_ability_module()
    cap = _make_capability(mod)
    worker = _FakeLinkStorageWorker()
    cap.capability_worker = worker

    asyncio.run(cap._write_link_state({"token": "saved-token", "linked_at": 123}))

    assert worker.created == [
        (mod.PLEX_LINK_STATE_KEY, {"token": "saved-token", "linked_at": 123})
    ]
    assert asyncio.run(cap._linked_token()) == "saved-token"


def test_write_link_state_also_writes_file_when_key_is_not_immediately_readable():
    mod = load_ability_module()
    cap = _make_capability(mod)
    worker = _FakeLinkStorageWorker(persist_keys=False)
    cap.capability_worker = worker

    assert asyncio.run(cap._write_link_state({"token": "saved-token", "linked_at": 123})) is True

    assert worker.created == [
        (mod.PLEX_LINK_STATE_KEY, {"token": "saved-token", "linked_at": 123})
    ]
    assert worker.file_writes == [
        {
            "filename": mod.PLEX_LINK_FILE,
            "content": json.dumps({"token": "saved-token", "linked_at": 123}),
            "in_ability_directory": False,
            "mode": "w",
        }
    ]
    assert asyncio.run(cap._linked_token()) == "saved-token"


def test_write_link_state_file_fallback_uses_overwrite_mode():
    mod = load_ability_module()
    cap = _make_capability(mod)
    worker = _FakeFileOnlyStorageWorker()
    cap.capability_worker = worker

    assert asyncio.run(cap._write_link_state({"token": "file-token", "linked_at": 123})) is True

    assert worker.file_writes == [
        {
            "filename": mod.PLEX_LINK_FILE,
            "content": json.dumps({"token": "file-token", "linked_at": 123}),
            "in_ability_directory": False,
            "mode": "w",
        }
    ]
    assert asyncio.run(cap._linked_token()) == "file-token"


def test_linked_token_accepts_existing_valid_file_state_as_fallback():
    mod = load_ability_module()
    cap = _make_capability(mod)
    cap.capability_worker = _FakeLinkStorageWorker(
        file_text=json.dumps({"token": "file-token", "linked_at": 123})
    )

    assert asyncio.run(cap._linked_token()) == "file-token"


def test_linked_token_ignores_corrupt_appended_json_file():
    mod = load_ability_module()
    cap = _make_capability(mod)
    cap.capability_worker = _FakeLinkStorageWorker(
        file_text='{"token": "old-token"}{"token": "new-token"}'
    )

    assert asyncio.run(cap._linked_token()) == ""


def test_link_plex_account_does_not_claim_success_when_saved_token_cannot_be_read_back(monkeypatch):
    mod = load_ability_module()
    cap = _make_capability(mod)
    worker = _FakeLinkStorageWorker(persist_keys=False, persist_files=False)
    cap.capability_worker = worker

    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: _FakeResponse({"id": 7, "code": "ABCD"}))
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: _FakeResponse({"authToken": "new-token"}))

    asyncio.run(cap._link_plex_account())

    spoken = " ".join(worker.spoken).lower()
    assert "your plex account is linked" not in spoken
    assert "could not be saved" in spoken


def test_run_with_saved_link_token_plays_without_starting_link_flow(monkeypatch):
    mod = load_ability_module()
    cap = _make_capability(mod)
    worker = _FakeLinkStorageWorker(keys={mod.PLEX_LINK_STATE_KEY: {"token": "saved-token"}})
    cap.capability_worker = worker

    item = mod.PlexAudioItem("So What", "Miles Davis", "Kind of Blue", "music", "/so-what.mp3", 545000, "rk1")
    calls = {"linked": 0, "streamed": 0}

    class _FakeClient:
        base_url = "https://plex.example:32400"
        token = "saved-token"

        def search_audio(self, user_text):
            return [item]

    async def fail_link():
        calls["linked"] += 1

    async def fake_stream_queue(client, queue, user_request):
        calls["streamed"] += 1
        assert client.token == "saved-token"
        assert [queued.part_key for queued in queue] == ["/so-what.mp3"]
        assert user_request == "play music from my Plex"

    async def fake_initial_request():
        return "play music from my Plex"

    async def fake_music_mode_off():
        return None

    def fake_cloud_reachable_client(base_url, token, account_token, server_name, machine_identifier):
        assert token == "saved-token"
        assert account_token == "saved-token"
        return _FakeClient()

    monkeypatch.setattr(cap, "_get_initial_request", fake_initial_request)
    monkeypatch.setattr(cap, "_link_plex_account", fail_link)
    monkeypatch.setattr(cap, "_cloud_reachable_client", fake_cloud_reachable_client)
    monkeypatch.setattr(cap, "_stream_queue", fake_stream_queue)
    monkeypatch.setattr(cap, "_music_mode_off", fake_music_mode_off)

    asyncio.run(cap.run())

    assert calls == {"linked": 0, "streamed": 1}


def test_new_cloud_methods_exist():
    """The cloud-primary restructure must expose these methods."""
    mod = load_ability_module()
    for name in (
        "_cloud_reachable_client",
        "_probe_cloud_connection",
        "_report_cloud_timeline",
        "_stream_queue",
        "_stream_one_track",
        "_cancel_streamer",
    ):
        assert hasattr(mod.PlexAudioPlayerCapability, name), f"missing {name}"


def test_report_cloud_timeline_builds_expected_request(monkeypatch):
    """Timeline scrobble must hit /:/timeline with the playback state + position."""
    mod = load_ability_module()
    cap = _make_capability(mod)
    client = mod.PlexAudioClient("https://plex.example:32400", "tok")
    item = mod.PlexAudioItem("So What", "Miles Davis", "Kind of Blue", "music", "/p.mp3", 545000, "rk42")

    calls = []

    def fake_get(url, timeout=None):
        calls.append((url, timeout))
        return None

    monkeypatch.setattr(mod.requests, "get", fake_get)
    cap._report_cloud_timeline(client, item, "playing", 12000)

    assert len(calls) == 1
    url, timeout = calls[0]
    assert url.startswith("https://plex.example:32400/:/timeline?")
    assert "ratingKey=rk42" in url
    assert "state=playing" in url
    assert "time=12000" in url
    assert "duration=545000" in url
    assert timeout == 5


def test_report_cloud_timeline_skips_item_without_rating_key(monkeypatch):
    mod = load_ability_module()
    cap = _make_capability(mod)
    client = mod.PlexAudioClient("https://plex.example:32400", "tok")
    item = mod.PlexAudioItem("Unknown", "Artist", "", "music", "/p.mp3", 0, "")

    calls = []
    monkeypatch.setattr(mod.requests, "get", lambda url, timeout=None: calls.append(url))
    cap._report_cloud_timeline(client, item, "stopped", 0)

    assert calls == []


def test_report_cloud_timeline_swallows_request_errors(monkeypatch):
    mod = load_ability_module()
    cap = _make_capability(mod)
    client = mod.PlexAudioClient("https://plex.example:32400", "tok")
    item = mod.PlexAudioItem("So What", "Miles Davis", "Kind of Blue", "music", "/p.mp3", 545000, "rk42")

    def boom(url, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(mod.requests, "get", boom)
    # Must not raise — timeline reporting is strictly best-effort.
    cap._report_cloud_timeline(client, item, "playing", 0)


def test_cloud_reachable_client_probes_non_local_base_url_directly(monkeypatch):
    """A non-local configured base_url is probed as-is; no plex.tv discovery."""
    mod = load_ability_module()
    cap = _make_capability(mod)

    discovery_called = []

    def fake_discover(*a, **k):
        discovery_called.append(True)
        return []

    monkeypatch.setattr(mod, "discover_plex_tv_connections", fake_discover)

    probed = []

    def fake_get(url, timeout=None):
        probed.append(url)

        class _Resp:
            def raise_for_status(self):
                return None

        return _Resp()

    monkeypatch.setattr(mod.requests, "get", fake_get)

    client = cap._cloud_reachable_client(
        "https://plex.example:32400", "tok", "acct", None, None
    )

    assert client is not None
    assert client.base_url == "https://plex.example:32400"
    assert discovery_called == []  # never consulted plex.tv for a remote URL
    assert any("/identity" in u for u in probed)


def test_cloud_reachable_client_uses_remote_discovery_for_local_base_url(monkeypatch):
    """A local base_url forces plex.tv discovery for a remote-access connection."""
    mod = load_ability_module()
    cap = _make_capability(mod)

    monkeypatch.setattr(
        mod,
        "discover_plex_tv_connections",
        lambda *a, **k: [
            {
                "base_url": "https://1-2-3-4.example.plex.direct:32400",
                "token": "remote-tok",
                "local": False,
                "relay": False,
            }
        ],
    )

    def fake_get(url, timeout=None):
        class _Resp:
            def raise_for_status(self):
                return None

        return _Resp()

    monkeypatch.setattr(mod.requests, "get", fake_get)

    client = cap._cloud_reachable_client(
        "http://10.0.0.136:32400", "tok", "acct", None, None
    )

    assert client is not None
    assert client.base_url == "https://1-2-3-4.example.plex.direct:32400"
    assert client.token == "remote-tok"


def test_cloud_reachable_client_prefers_relay_connection_for_cloud_playback(monkeypatch):
    mod = load_ability_module()
    cap = _make_capability(mod)

    monkeypatch.setattr(
        mod,
        "discover_plex_tv_connections",
        lambda *a, **k: [
            {
                "base_url": "https://relay.example.plex.direct:443",
                "token": "relay-tok",
                "local": False,
                "relay": True,
            },
            {
                "base_url": "https://direct.example.plex.direct:32400",
                "token": "direct-tok",
                "local": False,
                "relay": False,
            },
        ],
    )

    probed = []

    def fake_get(url, timeout=None):
        probed.append(url)

        class _Resp:
            def raise_for_status(self):
                return None

        return _Resp()

    monkeypatch.setattr(mod.requests, "get", fake_get)

    client = cap._cloud_reachable_client(
        "http://10.0.0.136:32400", "tok", "acct", None, None
    )

    assert client is not None
    assert client.base_url == "https://relay.example.plex.direct:443"
    assert client.token == "relay-tok"
    assert "direct.example.plex.direct" not in " ".join(probed)


def test_cloud_reachable_client_returns_none_when_probe_fails(monkeypatch):
    """A non-local base_url that fails the /identity probe yields no cloud client."""
    mod = load_ability_module()
    cap = _make_capability(mod)

    def boom(url, timeout=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mod.requests, "get", boom)
    monkeypatch.setattr(mod, "discover_plex_tv_connections", lambda *a, **k: [])

    assert cap._cloud_reachable_client(
        "https://plex.example:32400", "tok", "acct", None, None
    ) is None


# ---------------------------------------------------------------------------
# LED music visualizer (now in devkit_functions.py) — pure helpers
# ---------------------------------------------------------------------------


def test_viz_level_to_lit_count_silence_is_dark():
    dev = load_devkit_module()
    assert dev.level_to_lit_count(0.0, 24) == 0
    assert dev.level_to_lit_count(-0.5, 24) == 0


def test_viz_level_to_lit_count_full_scale_lights_whole_ring():
    dev = load_devkit_module()
    assert dev.level_to_lit_count(1.0, 24) == 24
    assert dev.level_to_lit_count(2.0, 24) == 24


def test_viz_level_to_lit_count_faint_level_lights_at_least_one():
    dev = load_devkit_module()
    # A tiny but non-zero level must light a single pixel, not round to dark.
    assert dev.level_to_lit_count(0.001, 24) == 1


def test_viz_level_to_lit_count_is_monotonic_and_proportional():
    dev = load_devkit_module()
    assert dev.level_to_lit_count(0.5, 24) == 12
    assert dev.level_to_lit_count(0.25, 24) == 6
    counts = [dev.level_to_lit_count(x / 10.0, 24) for x in range(0, 11)]
    assert counts == sorted(counts)


def test_viz_update_envelope_attack_is_faster_than_decay():
    dev = load_devkit_module()
    # Rising from 0 toward 1 closes most of the gap (fast attack).
    risen = dev.update_envelope(0.0, 1.0)
    # Falling from 1 toward 0 closes only a little of the gap (slow decay).
    fell = dev.update_envelope(1.0, 0.0)
    assert risen > 0.5          # attack moved well past halfway
    assert fell > 0.9           # decay barely dropped
    assert (1.0 - fell) < risen  # decay step smaller than attack step


def test_viz_normalize_level_autoscales_between_floor_and_peak():
    dev = load_devkit_module()
    assert dev.normalize_level(0.1, 0.1, 0.5) == 0.0   # at floor -> 0
    assert dev.normalize_level(0.5, 0.1, 0.5) == 1.0   # at peak -> 1
    assert abs(dev.normalize_level(0.3, 0.1, 0.5) - 0.5) < 1e-9
    # Below floor clamps to 0, above peak clamps to 1.
    assert dev.normalize_level(0.0, 0.1, 0.5) == 0.0
    assert dev.normalize_level(0.9, 0.1, 0.5) == 1.0


def test_viz_normalize_level_guards_collapsed_range():
    dev = load_devkit_module()
    # When floor and peak coincide, the min-range guard prevents divide-by-zero.
    result = dev.normalize_level(0.2, 0.2, 0.2)
    assert 0.0 <= result <= 1.0


def test_viz_rms_from_bytes_silence_and_signal():
    dev = load_devkit_module()
    import struct as _struct
    assert dev.rms_from_bytes(b"") == 0.0
    silence = _struct.pack("<8h", 0, 0, 0, 0, 0, 0, 0, 0)
    assert dev.rms_from_bytes(silence) == 0.0
    loud = _struct.pack("<8h", *([16000, -16000] * 4))
    quiet = _struct.pack("<8h", *([1000, -1000] * 4))
    assert dev.rms_from_bytes(loud) > dev.rms_from_bytes(quiet) > 0.0
    assert 0.0 <= dev.rms_from_bytes(loud) <= 1.0


def test_viz_rms_from_bytes_manual_stride_matches_audioop_path():
    dev = load_devkit_module()
    import struct as _struct
    samples = [12000, -8000, 4000, -16000, 9000, -3000, 15000, -11000]
    data = _struct.pack("<8h", *samples)
    # stride=1 uses audioop when present; force the manual loop with stride and
    # confirm it produces a comparable, sane RMS for the same data.
    full = dev.rms_from_bytes(data, sample_stride=1)
    strided = dev.rms_from_bytes(data, sample_stride=2)
    assert 0.0 < full <= 1.0
    assert 0.0 < strided <= 1.0


def test_viz_palette_color_matches_stops_and_interpolates():
    dev = load_devkit_module()
    # Integer positions hit palette stops exactly.
    assert dev.palette_color(0.0) == dev.PALETTE[0]
    assert dev.palette_color(1.0) == dev.PALETTE[1]
    # Wraps around at the end of the palette.
    assert dev.palette_color(float(len(dev.PALETTE))) == dev.PALETTE[0]
    # Half-way interpolates between two stops.
    c0, c1 = dev.PALETTE[0], dev.PALETTE[1]
    mid = dev.palette_color(0.5)
    for channel in range(3):
        assert min(c0[channel], c1[channel]) <= mid[channel] <= max(c0[channel], c1[channel])


def test_viz_peak_dot_index_rides_level():
    dev = load_devkit_module()
    assert dev.peak_dot_index(0.0, 24) == -1     # silence -> no dot
    assert dev.peak_dot_index(1.0, 24) == 23     # full -> last pixel
    assert dev.peak_dot_index(0.5, 24) == 12     # mid-ring
    assert dev.peak_dot_index(2.0, 24) == 23     # clamps to last pixel


def test_viz_registry_contains_all_three_viz_entries():
    """FUNCTION_REGISTRY must expose leds_viz_start, leds_viz_stop, and leds_viz_run."""
    dev = load_devkit_module()
    for name in ("leds_viz_start", "leds_viz_stop", "leds_viz_run"):
        assert name in dev.FUNCTION_REGISTRY, f"missing {name!r} from FUNCTION_REGISTRY"


# ---------------------------------------------------------------------------
# main.py coverage for behaviors previously only tested via devkit copies
# ---------------------------------------------------------------------------


def test_score_item_weights_creator_above_title():
    """main.py score_item: creator match outscores title-only match for same query."""
    mod = load_ability_module()
    by_artist = mod.PlexAudioItem("Some Song", "Metallica", "Album", "music", "/a.mp3", 1, "a")
    by_title = mod.PlexAudioItem("Metallica Tribute", "Cover Band", "Album", "music", "/b.mp3", 1, "b")

    assert mod.score_item(by_artist, "play metallica") > mod.score_item(by_title, "play metallica")


def test_search_audio_artist_first_returns_only_that_artist():
    """Artist-first path in _plex_search_audio: an artist directory hit returns that
    artist's tracks and skips the generic search path entirely."""
    mod = load_ability_module()
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

    class FakeClient:
        logger = None

        def get_xml(self, path, params=None):
            p = params or {}
            if p.get("type") == "8" and "metallica" in str(p.get("title", "")).lower():
                return artist_dir_xml
            if p.get("artist.id") == "20495":
                return artist_tracks_xml
            if path == "/library/sections":
                return sections_xml
            return "<MediaContainer />"

        def parse_tracks(self, xml_text):
            return mod._plex_parse_tracks(self, xml_text)

    items = mod._plex_search_audio(FakeClient(), "play metallica")

    assert {item.creator for item in items} == {"Metallica"}
    assert {item.title for item in items} == {"Enter Sandman", "One"}


def test_search_audio_scores_sorts_and_caps_results():
    """_plex_search_audio: results are scored, sorted, and capped at MAX_SEARCH_RESULTS.
    A zero-score unrelated track does not appear when meaningful query is present."""
    mod = load_ability_module()
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

    class FakeClient:
        logger = None

        def get_xml(self, path, params=None):
            p = params or {}
            if path == "/library/sections":
                return sections_xml
            if path == "/search":
                return empty_xml
            # Artist directory lookup — no match
            if p.get("type") == "8":
                return empty_xml
            # Title lookup — no exact title match
            if "title" in p:
                return empty_xml
            # All-tracks scan
            return all_tracks_xml

        def parse_tracks(self, xml_text):
            return mod._plex_parse_tracks(self, xml_text)

    items = mod._plex_search_audio(FakeClient(), "play Metallica on plex")

    titles = [item.title for item in items]
    assert "Enter Sandman" in titles
    # Zero-score unrelated item filtered out by the meaningful-query path.
    assert "Unrelated" not in titles

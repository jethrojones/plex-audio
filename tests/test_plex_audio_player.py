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

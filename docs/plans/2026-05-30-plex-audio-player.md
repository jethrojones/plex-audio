# Plex Audio Player Implementation Plan

> **For Hermes:** Implement directly with strict TDD for helper behavior; use OpenHome validator after implementation.

**Goal:** Build a first OpenHome community Ability that lets a user ask for music or audiobooks stored in Plex, play matching audio, and resume the last audiobook with "continue my audiobook".

**Architecture:** Create `community/plex-audio-player/` as a standard interactive `main.py` Ability. Keep Plex access in small testable helper functions/classes inside `main.py`, use OpenHome custom API keys for `plex_base_url` and `plex_token`, and stream selected audio via `CapabilityWorker` audio streaming when available, falling back to `play_audio`. Design a provider boundary so Jellyfin/Audiobookshelf/etc. can be added later without rewriting the voice flow.

**Tech Stack:** Python 3.10+, OpenHome SDK (`MatchingCapability`, `CapabilityWorker`), `requests`, stdlib `xml.etree.ElementTree`, `urllib.parse`, pytest for local tests.

---

### Task 1: Create Ability Skeleton

**Objective:** Add the community ability folder with OpenHome-required files.

**Files:**
- Create: `community/plex-audio-player/__init__.py`
- Create: `community/plex-audio-player/main.py`
- Create: `community/plex-audio-player/README.md`

**Steps:**
1. Copy API template structure, but do not hardcode real secrets.
2. Add `#{{register capability}}` exactly inside the capability class.
3. Define required key names: `plex_base_url`, `plex_token`.
4. Add initial README with suggested trigger words and setup.

**Verification:**
- `python validate_ability.py community/plex-audio-player` should reach behavior checks; after full implementation it must pass.

---

### Task 2: Test and Implement Safe Plex URL Construction

**Objective:** Ensure URLs are built safely for local/remote Plex servers and token-bearing endpoints.

**Files:**
- Create: `tests/test_plex_audio_player.py`
- Modify: `community/plex-audio-player/main.py`

**RED:** Write tests for:
- Stripping a trailing slash from `plex_base_url`
- Preserving existing query params
- Adding `X-Plex-Token` query param
- Joining relative Plex paths like `/library/metadata/123`

**GREEN:** Implement `PlexAudioClient.url(path, params=None)`.

**Verification:**
- Run: `pytest tests/test_plex_audio_player.py::test_plex_url_adds_token_and_params -v`

---

### Task 3: Test and Implement Plex XML Parsing

**Objective:** Convert Plex XML responses into normalized audio items.

**Files:**
- Modify: `tests/test_plex_audio_player.py`
- Modify: `community/plex-audio-player/main.py`

**RED:** Add tests that parse XML with `Track` nodes and nested `Media/Part` elements.

**GREEN:** Implement:
- `PlexAudioItem`
- `PlexAudioClient.parse_tracks(xml_text)`
- Type/category inference for music vs audiobook using library title, grandparent title, parent title, and duration.

**Verification:**
- Run parser tests and make sure audiobook/music metadata are captured.

---

### Task 4: Test and Implement Search Ranking

**Objective:** Pick the best audio item from Plex search results based on user request.

**Files:**
- Modify: `tests/test_plex_audio_player.py`
- Modify: `community/plex-audio-player/main.py`

**RED:** Add tests for:
- Music query prefers music library result
- Audiobook query prefers audiobook-like result
- Missing results returns `None`

**GREEN:** Implement:
- `detect_requested_media_type(user_text)`
- `score_item(item, user_text, requested_type)`
- `choose_best_item(items, user_text)`

**Verification:**
- Run search-ranking tests.

---

### Task 5: Implement Audiobook Resume Helpers

**Objective:** Make "continue my audiobook" part of v1, not a future feature.

**Files:**
- Modify: `tests/test_plex_audio_player.py`
- Modify: `community/plex-audio-player/main.py`

**Tests:**
- `resume_requested()` recognizes "continue my audiobook", "resume my book", and "pick up where I left off".
- `stream_url_for(item, offset_ms=...)` adds an offset parameter.
- `build_resume_state()` stores only audiobooks.
- `item_from_resume_state()` rehydrates a stored audiobook.
- `updated_resume_state()` advances offset and resets near the end.

**Verification:**
- Run: `pytest tests/test_plex_audio_player.py -q`

---

### Task 6: Implement Voice Flow

**Objective:** Let the user ask naturally, search Plex, confirm the selected item, then play it.

**Files:**
- Modify: `community/plex-audio-player/main.py`
- Modify: `community/plex-audio-player/README.md`

**Steps:**
1. In `call()`, initialize worker and start `run()` via `session_tasks.create()`.
2. In `run()`, load `plex_base_url` and `plex_token` via `get_api_keys()`.
3. If missing, speak a clear setup message and call `resume_normal_flow()`.
4. Get the first full transcription if available, otherwise ask what they want to hear.
5. Search Plex audio libraries.
6. Speak the chosen title/artist or book/chapter.
7. If the request is "continue" / "resume", load `plex_audio_last_audiobook` from Ability context storage and play from its saved offset.
8. If a new selected item is an audiobook, save it to `plex_audio_last_audiobook` before playback.
9. Stream the media URL with `stream_init`, `send_audio_data_in_stream`, `stream_end`; if streaming fails, fall back to `play_audio`.
10. After audiobook playback returns, update the saved offset with approximate elapsed playback time.
11. Call `resume_normal_flow()` in a `finally` block.

**Verification:**
- Run tests.
- Run OpenHome validator.

---

### Task 7: Marketplace/Contribution Readiness

**Objective:** Document setup clearly enough for any Plex user, while noting future providers.

**Files:**
- Modify: `community/plex-audio-player/README.md`

**Steps:**
1. Include required API keys: `plex_base_url`, `plex_token`.
2. Explain Plex server reachability: the OpenHome runtime must be able to reach the URL; LAN-only URLs work only when the runtime/device is on the same network.
3. Explain how to get a Plex token.
4. Add suggested trigger words.
5. Document v1 audiobook resume behavior and its approximate-offset limitation.
6. Add future-provider note: Jellyfin, Audiobookshelf, Navidrome, Emby.

**Verification:**
- README matches OpenHome contribution format.
- No real credentials included.

---

### Task 8: Validate

**Objective:** Prove the ability follows repo rules.

**Commands:**
```bash
cd /home/jethro/openhome/abilities
pytest tests/test_plex_audio_player.py -q
python validate_ability.py community/plex-audio-player
```

**Expected:**
- All pytest tests pass.
- Validator passes.

---

### Task 9: Manual OpenHome Steps

**Objective:** Install and test in OpenHome dashboard/dev kit.

**Steps:**
1. Zip `community/plex-audio-player/`.
2. Upload at `app.openhome.com` → Abilities → Add Custom Ability.
3. Under Ability Behavior → API Keys, declare and tag required keys:
   - `plex_base_url`
   - `plex_token`
4. Set trigger words: “play from Plex”, “Plex music”, “Plex audiobook”, “play my audiobook”.
5. Test in Live Editor or on DevKit with a reachable Plex URL.

**Acceptance Criteria:**
- User can say “play [artist/song/book] from Plex.”
- User can say “continue my audiobook” and resume the last saved audiobook.
- Ability finds an audio track from Plex and starts playback.
- Ability gives helpful spoken errors for missing config, no match, connection problems, and playback failures.

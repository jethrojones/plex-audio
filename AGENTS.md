# Plex Audio Player — Project Context for Codex

## What This Project Is

An OpenHome Local Ability that lets you play music and audiobooks from a Plex Media Server using voice commands through an OpenHome agent device (Raspberry Pi with Google Voice HAT).

## Session Summary (2026-06-11)

### Problem We Were Solving
The original code assumed the OpenHome ability runtime could reach Plex directly over the network. It can't — the ability's `main.py` runs in the OpenHome cloud, which has no route to the user's LAN Plex server (10.0.0.136:32400, behind double-NAT). The DevKit (Raspberry Pi at 10.0.0.66) is on the same LAN and CAN reach Plex.

### Architecture: Split Runtime (Critical to Understand)
- **`main.py`** — runs in the OpenHome **cloud**. Handles voice conversation flow, calls DevKit functions via `capability_worker.send_devkit_capability_action()`.
- **`devkit_functions.py`** — runs on the **OpenHome DevKit** (Raspberry Pi). Handles all LAN Plex calls and audio playback. The node server executes it as: `sudo python3 devkit_functions.py <function_name> <args...>`. Returns JSON to stdout.
- These two files **cannot import each other** — they run in completely separate environments. Any shared helpers must be duplicated.

### Fixes Made This Session

#### 1. Generic search query ("play music") returning no results
- **Problem:** "play music" → sanitizer strips all words → empty string → Plex title search for "" → score filter removes everything → returns [].
- **Fix:** Added `_meaningful_query()` helper. If the stripped query is empty (pure noise words), skip the Plex title search and browse-all sections instead, returning tracks without a score filter. Implemented in both `main.py` and `devkit_functions.py` (both runtimes need it).

#### 2. `plex_diagnose` erroring instead of reporting
- **Problem:** When Plex was unreachable, `plex_diagnose` returned an error payload → `_devkit_call` returned `None` → silently fell back to cloud mode.
- **Fix:** `plex_diagnose` always returns `success: true` with a report. `main.py` branches on `data["plex_reachable"]` and speaks the precise problem to the user.

#### 3. Audio playing through HDMI instead of the Google Voice HAT speaker
- **Problem:** `plex_play` launches `mpv` via `subprocess.Popen` inside `sudo python3 devkit_functions.py` (runs as root). Root doesn't inherit the `openhome` user's PulseAudio/PipeWire socket. mpv falls back to the first ALSA device, which is HDMI.
- **Fix:** Added `PULSE_ENV_EXTRAS` dict with `PULSE_RUNTIME_PATH`, `PULSE_SERVER`, `XDG_RUNTIME_DIR` pointing to `/run/user/1000/pulse`. Pass as `env=` to `subprocess.Popen`. Added `--ao=pulse` to the mpv command. PipeWire confirmed routing to `alsa_output.platform-soc_sound.stereo-fallback` (Google Voice HAT, card 2: `sndrpigooglevoi`).

#### 4. `plex_status` always returning `playing: false`
- **Problem:** State file `/tmp/plex_audio_player_state.json` was owned by `openhome` user (664 permissions). When `plex_play` ran as root (via `sudo`), it could not overwrite the user-owned file in `/tmp` on this system (AppArmor or security hardening). `_write_state()` failed silently, leaving stale state. `plex_status` read the stale state with `pid: null` → `playing: false`.
- **Fix:** Moved `STATE_FILE` to `/home/openhome/.plex_audio_state.json`. Root can always write to files in the openhome home directory.

#### 5. Trigger words not matching music requests
- **Problem:** The ability's installed trigger words were `["play music from plex", "audiobook", "play audiobook from plex"]`. Phrases like "play Metallica from Plex" didn't match, so the default OpenHome agent handled the call instead.
- **Fix:** Updated trigger words via `PUT https://app.openhome.com/api/capabilities/edit-installed-capability/898453/` with `X-API-Key` header. New set includes: "play from Plex", "Plex music", "play music", "play on Plex", "play from my Plex", "Plex library", "play my audiobook", "Plex audiobook", "audiobook", "continue my audiobook", "resume my book".
- **Status as of session end:** Trigger words updated in cloud, NOT YET TESTED by voice. This is the first thing to verify next session.

### Key Infrastructure Details

#### DevKit (Raspberry Pi)
- IP: `10.0.0.66`
- SSH: `ssh openhome@10.0.0.66` (key auth, password: wealth-punk if key fails)
- Audio: Google Voice HAT = ALSA card 2 `sndrpigooglevoi`, PulseAudio default sink `alsa_output.platform-soc_sound.stereo-fallback`
- Ability files: `/home/openhome/openhome_devkit/local_capabilities/plexaudio/devkit_functions.py`
- Execution copy: `/home/openhome/openhome_devkit/devkit_functions.py`
- State file: `/home/openhome/.plex_audio_state.json`
- Logs: `journalctl -u openhome_devkit_client.service -u openhome_node_server.service -f --no-pager`

#### Plex Server
- LAN IP: `10.0.0.136:32400` (primary) or `192.168.0.20:32400` (wired)
- No token needed if DevKit IP is in Plex's allowed networks
- Double-NAT topology: Xfinity gateway (10.0.0.x) → inner router (10.0.0.133/192.168.0.1) → Plex

#### OpenHome Ability
- Ability ID: `5761`, Installed Capability ID: `898453`
- Agent ID: `590392`
- Cloud version: v5 (as of 2026-06-11)
- Branch: `dev`

#### OpenHome CLI Key Commands
```bash
# Update code (API key only)
npx openhome-cli update "PlexAudio" --zip community/plex-audio-player --json

# Stream logs
npx openhome-cli logs --agent 590392

# Update trigger words directly via API (no JWT needed)
API_KEY=$(node -e "const {getApiKey}=require('~/.npm/_npx/.../openhome-cli/dist/store-4BB7U7QQ.js'); console.log(getApiKey());")
curl -X PUT "https://app.openhome.com/api/capabilities/edit-installed-capability/898453/" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"trigger_words": [...]}'

# DevKit logs + cloud logs simultaneously
ssh openhome@10.0.0.66 "journalctl -u openhome_devkit_client.service -u openhome_node_server.service -f --no-pager -n 0" &
npx openhome-cli logs --agent 590392
```

### Deploying Changes to DevKit
The DevKit syncs from the OpenHome cloud. Workflow:
1. Edit code locally
2. `npx openhome-cli update "PlexAudio" --zip community/plex-audio-player --json`
3. In the OpenHome app: Settings → Sync Abilities (or it auto-syncs)
4. For immediate testing: `scp devkit_functions.py openhome@10.0.0.66:/home/openhome/openhome_devkit/local_capabilities/plexaudio/devkit_functions.py` (and same path without `local_capabilities/plexaudio/`)

### What's Left
1. **Verify music trigger words work** — say "play Metallica from Plex" or "play music from Plex" and confirm the ability fires (not the default "Oh hey!" greeting). If it still doesn't fire, pull live logs (both cloud + DevKit simultaneously) while triggering by voice.
2. **Test full flow** — play a track, confirm audio comes from the OpenHome speaker, confirm "stop" works.
3. **Audiobooks** — confirm audiobook resume/continue logic works with the DevKit path.

# Plex Audio Player

![Community](https://img.shields.io/badge/OpenHome-Community-orange?style=flat-square)
![Author](https://img.shields.io/badge/Author-@jethrojones-lightgrey?style=flat-square)
![Category](https://img.shields.io/badge/Category-Audio-purple?style=flat-square)

## What It Does

Plex Audio Player lets an OpenHome Agent search and play audio-only media from a user's Plex server, including music libraries and audiobook libraries.

It is a **Local Ability**. OpenHome splits every Ability across two runtimes: `main.py` always executes in the standard Ability runtime (OpenHome's cloud), and `devkit_functions.py` executes on the OpenHome DevKit. This Ability puts all Plex network access and audio playback on the DevKit side, so the cloud runtime never needs a route to the Plex server. A Plex server that is only reachable on the home LAN works fine — no Remote Access, port forwarding, or public `plex.direct` URL required.

Version 1 includes audiobook resume support: after an audiobook starts, the Ability stores the last audiobook and an approximate playback position in OpenHome Ability context storage, so users can say "continue my audiobook" or "resume my book" later.

It is designed as the first provider in a broader personal-audio pattern. The code keeps the provider logic separated so similar self-hosted audio platforms such as Jellyfin, Audiobookshelf, Navidrome, and Emby can be added later.

## Suggested Trigger Words

- "play from Plex"
- "Plex music"
- "play my audiobook"
- "Plex audiobook"
- "continue my audiobook"
- "resume my book"
- "play music from my server"

## Setup

1. Package and upload the Ability (see the repo README), set its category to **Local** in the OpenHome dashboard, and sync it to your DevKit from the Live Editor's Local Abilities controls.
2. Make sure the DevKit and the Plex server are on the same network (or on routable subnets).
3. Make sure the DevKit has an audio player binary. OpenHome OS is Debian-based; `mpv` is recommended: `sudo apt install mpv`. The Ability auto-detects `mpv`, `ffplay`, `cvlc`, or `mpg123`, in that order.

**Minimal setup — no token needed:** if your Plex server is allowed to run on your local network without auth (Plex Web App → **Settings → Server → Network → List of IP addresses and networks that are allowed without auth**, e.g. `10.0.0.0/24`), the only key you need is `plex_base_url` with the Plex LAN IP and port, such as `http://10.0.0.136:32400`. No `plex_token` is required.

This Ability can find Plex in two ways:

1. **Manual URL override**: set `plex_base_url` to the Plex server URL as seen **from the DevKit**, such as `http://10.0.0.136:32400`.
2. **Plex.tv resource discovery**: set `plex_account_token` so the Ability can ask Plex.tv for the server's advertised connection URLs. Discovery prefers LAN connection URLs, which the DevKit can reach. OpenHome blocks raw `socket` imports in `main.py`, so multicast LAN discovery is not available.

Recommended OpenHome custom API key values:

- `plex_base_url` — base URL for the user's Plex server, reachable from the DevKit.
- `plex_token` — optional server auth token. This is not required if Plex allows the DevKit subnet under **Settings → Server → Network → List of IP addresses and networks that are allowed without auth**.
- `plex_account_token` — optional Plex account token for Plex.tv resource discovery.
- `plex_server_name` — optional Plex server name to choose when the Plex account has multiple servers.
- `plex_machine_identifier` — optional Plex machine identifier to choose one exact server.

### Important Network Note

This is a Local Ability, so the network requirement depends on which path is active:

- **DevKit path (preferred)**: only the **DevKit** needs to reach `plex_base_url`. A LAN URL like `http://10.0.0.x:32400` is the right choice, and multi-router/double-NAT homes need no port forwarding at all.
- **Cloud fallback (no DevKit connected)**: the standard runtime streams the audio itself, so `plex_base_url` must be reachable from the internet — a verified `plex.direct` Remote Access URL, or a tunnel such as Tailscale Funnel in front of port 32400.
- Do not publish a real Plex token or private home URL in this repo.

Quick reachability checks:

- `http://LAN-IP:32400/identity?X-Plex-Token=TOKEN` should work from any device on the Plex server's network — this is what the DevKit path uses.
- `https://...plex.direct:PORT/identity?X-Plex-Token=TOKEN` should work from outside the network before relying on the cloud fallback path. Plex Remote Access setup (UPnP or manual port forward plus **Manually specify public port**) is documented at https://support.plex.tv/articles/200289506-remote-access/.

### Playback Behavior on the DevKit

- Audio plays from a local player process on the DevKit, not through OpenHome's cloud audio pipeline.
- While playing, the Ability listens in short windows for stop commands ("stop", "pause", "stop the music"). Longer sentences are ignored to avoid false triggers from lyrics the microphone picks up.
- Audiobook resume positions are computed on the DevKit from actual playback time, which makes "continue my audiobook" more accurate than the previous cloud-streaming estimate.

### Getting a Plex Token

Plex's token documentation says authenticated server endpoints use the `X-Plex-Token` URL parameter, for example `http://localhost:32400/?X-Plex-Token=YOURTOKENVALUEHERE`. To find a token, sign in to Plex Web App, browse to a library item, view XML for it, and copy the `X-Plex-Token` value from the URL. Treat it like a password.

Provider URL suggestion for the OpenHome key setup screen:

- `plex_base_url`: `https://support.plex.tv/articles/200289506-remote-access/`
- `plex_token`: `https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/`
- `plex_account_token`: `https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/`

## How It Works

1. The user triggers the Ability and asks for music or an audiobook.
2. The Ability first uses `plex_base_url` if present. Otherwise it tries Plex.tv resource discovery with `plex_account_token`.
3. `main.py` asks the DevKit to run `plex_diagnose`, confirming the DevKit can reach Plex and has an audio player installed. If not, it falls back to cloud streaming.
4. The DevKit searches Plex for audio tracks using Plex's XML API (`plex_search`), filters to `Track` media, and infers music vs audiobook from library metadata and duration.
5. `main.py` picks the best match for the spoken request.
6. For audiobooks, it saves the selected item and resume offset in Ability context storage.
7. If the user asks to continue/resume, it reloads the saved audiobook and starts from the saved offset.
8. The DevKit plays the audio from Plex through a local player process (`plex_play`), while `main.py` polls playback status and listens for stop commands.
9. It always returns control to the Agent with `resume_normal_flow()`.

## Example Conversation

> **User:** "Play the audiobook Dune from Plex."
>
> **AI:** "Searching your Plex audio libraries."
>
> **AI:** "Playing Dune, Chapter 1 from Plex."

> **User:** "Continue my audiobook."
>
> **AI:** "Resuming Dune, Chapter 1 from Plex."

> **User:** "Plex music, Miles Davis Kind of Blue."
>
> **AI:** "Searching your Plex audio libraries."
>
> **AI:** "Playing So What by Miles Davis from Kind of Blue from Plex."

## Current Scope

This first version supports:

- Plex music libraries
- Plex audiobook libraries that appear as audio tracks
- "Continue my audiobook" / "resume my book" using persisted Ability context
- Natural-language search requests
- Music vs audiobook preference based on the request
- Audio streaming from Plex media parts
- Clear setup errors for missing/unreachable Plex discovery configuration

### Resume Behavior

Resume is intentionally audiobook-focused for v1.

- The Ability stores only audiobook resume state, not music tracks.
- The saved state key is `plex_audio_last_audiobook`.
- The offset is approximate because the first OpenHome SDK path streams audio but does not yet expose precise playback-position callbacks.
- If playback reaches the last minute of the item, the saved offset resets to the beginning.

## Future Provider Ideas

The next versions could add:

- Jellyfin audio libraries
- Audiobookshelf audiobook libraries
- Navidrome/Subsonic music libraries
- Emby audio libraries
- Playlist and album queueing
- More precise Plex scrobble/playback-state integration when OpenHome exposes richer playback callbacks

## Developer Notes

Run local checks from the repo root:

```bash
pytest tests/test_plex_audio_player.py -q
python validate_ability.py community/plex-audio-player
```

The Ability intentionally uses OpenHome's custom API key mechanism instead of hardcoded secrets. Trigger words are configured in the OpenHome dashboard, not in code.

Packaging note: keep test stubs and helper-only imports out of `main.py`. The OpenHome editor blocks some stdlib modules/import patterns, including `from types import ...`, `from urllib.parse import ...`, and `import urllib.parse`. This Ability avoids `urllib` entirely and uses a tiny local query-string encoder for Plex URLs; local tests may still use `types.ModuleType` for SDK stubs.

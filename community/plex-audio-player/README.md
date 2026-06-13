# Plex Audio Player

![Community](https://img.shields.io/badge/OpenHome-Community-orange?style=flat-square)
![Author](https://img.shields.io/badge/Author-@jethrojones-lightgrey?style=flat-square)
![Category](https://img.shields.io/badge/Category-Audio-purple?style=flat-square)

## What It Does

Plex Audio Player gives your OpenHome Agent voice control over the music and audiobooks on your own Plex server. You ask for something out loud, and it streams it directly from Plex through the OpenHome cloud.

- **Play by artist or title** — "play Metallica from Plex", "play Kind of Blue".
- **Continuous queues** — when you pick an artist or album, it keeps playing track after track instead of stopping after one song.
- **Next / skip** — say "next" or "skip" to jump to the following track.
- **Mid-song switching** — say "play something else" (e.g. "play the Beatles") while music is playing and it searches and switches without you having to stop first.
- **Stop / pause** — say "stop", "pause", or "stop the music" any time.
- **Audiobook resume** — start an audiobook, come back later, and say "continue my audiobook" to pick up where you left off.

Playback is always cloud streaming: the OpenHome cloud fetches audio directly from your Plex server. This requires either a linked Plex account (recommended) or a manually configured public `plex_base_url`. Your Plex server must have Remote Access enabled, or be reachable at a publicly accessible URL.

## Quick Start: Link Your Plex Account

This is all most people need.

1. **Enable Remote Access on your Plex server.** In Plex: **Settings → Remote Access → Enable Remote Access**. Wait for the indicator to turn **green**. This lets OpenHome's cloud reach your Plex server from outside your home.
2. **Say "link my Plex account."** The Agent reads out a short 4-character code.
3. **Enter the code at [plex.tv/link](https://plex.tv/link)** on your phone or computer, then sign in to Plex if prompted.
4. **Done.** The Agent confirms your account is linked. You can now ask it to play music or audiobooks, and it streams them through the OpenHome cloud directly from your Plex server.

No API keys, no URLs, no device to configure. The link is remembered, so you only do this once.

## LED Visualizer (optional — OpenHome DevKit only)

When the Ability is synced to an OpenHome DevKit, the 24-pixel LED ring on the device automatically pulses in time with the music during playback. The visualizer reads the PulseAudio monitor source as audio streams through the device and drives the ring as a VU-meter bounce.

Zero configuration required — no extra software, no API keys. On a DevKit the visualizer starts and stops automatically with each playback session. Agents without a DevKit simply skip it.

## Voice Commands

| You say | What happens |
| --- | --- |
| "play Metallica from Plex" | Searches your Plex audio and starts a continuous queue. |
| "play Kind of Blue" | Plays the album/track and keeps going through related tracks. |
| "play music" / "play something" | Browses your music libraries and plays. |
| "next" / "skip" | Jumps to the next track in the queue. |
| "play the Beatles" *(while music is playing)* | Switches mid-song: searches and starts the new request. |
| "stop" / "pause" / "stop the music" | Stops playback. |
| "continue my audiobook" / "resume my book" | Resumes your last audiobook from where you left off. |
| "link my Plex account" | Starts the account-linking flow (see Quick Start). |

Tip: while music is playing near the microphone, only short commands are treated as stop/skip, and negations ("don't stop") are ignored, so song lyrics the mic picks up don't trigger anything by accident.

## Optional API Keys (Manual Configuration)

You normally don't need any of these — account linking handles everything. They exist for advanced or manual setups, and are configured via OpenHome's custom API key screen:

- `plex_base_url` — your Plex server's publicly reachable URL, e.g. a remote `plex.direct` URL or any URL the OpenHome cloud can reach. Skips discovery.
- `plex_token` — a Plex server auth token (the `X-Plex-Token` value). Not needed if you use account linking.
- `plex_account_token` — a Plex account token for Plex.tv resource discovery (an alternative to the spoken link flow).
- `plex_server_name` — pick a specific server by name when your account has several.
- `plex_machine_identifier` — pick one exact server by its machine identifier.

To find a token manually: sign in to the Plex Web App, open a library item, choose **Get Info → View XML**, and copy the `X-Plex-Token` value from the URL. Treat it like a password and do not commit it to a repo.

## Troubleshooting

- **"I couldn't find that"** — Check that the artist/title matches what's actually in your Plex library. Try the exact album, artist, or book name. Generic requests like "play music" browse everything.
- **No remote streaming / cloud playback fails** — Re-link your account ("link my Plex account") and confirm Plex **Remote Access** shows **green** in your server settings. Cloud streaming needs the OpenHome cloud to reach your server from the internet.
- **Token rejected** — If you set `plex_token` or `plex_account_token` manually, the value may be stale. Re-copy it, or use the spoken link flow instead.

## How It Works

1. You trigger the Ability and ask for music or an audiobook.
2. The Ability resolves a cloud-reachable Plex connection: a non-local `plex_base_url` is probed as-is; otherwise it discovers a Remote Access endpoint via the linked account token from Plex.tv.
3. It searches your Plex audio libraries using an artist-first strategy: it tries to resolve the query to an exact artist in your library, then falls back to title and full-library search.
4. Matches are scored and the best one is selected (music vs audiobook is inferred from library metadata and track length).
5. Music plays as a **continuous queue** with voice stop, next/skip, and mid-song switching. Audiobooks play as a single long track, and the position is saved so you can resume later.
6. Control returns to the Agent when playback ends or you stop it.

## Developer Notes

Run local checks from the repo root:

```bash
python3 -m pytest tests/ -q
python3 -m py_compile community/plex-audio-player/main.py community/plex-audio-player/devkit_functions.py
python3 validate_ability.py community/plex-audio-player
```

The OpenHome cloud validator forbids some Python patterns (raw `socket`/`urllib` imports, `getattr`, `asyncio.sleep`). The Ability avoids all of them: it uses a tiny local query-string encoder for Plex URLs, `hasattr` guards for optional runtime features, and `worker.session_tasks.sleep()` for delays. Trigger words and API keys are configured in the OpenHome dashboard, not in code.

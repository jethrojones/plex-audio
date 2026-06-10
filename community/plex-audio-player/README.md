# Plex Audio Player

![Community](https://img.shields.io/badge/OpenHome-Community-orange?style=flat-square)
![Author](https://img.shields.io/badge/Author-@jethrojones-lightgrey?style=flat-square)
![Category](https://img.shields.io/badge/Category-Audio-purple?style=flat-square)

## What It Does

Plex Audio Player lets an OpenHome Agent search and play audio-only media from a user's Plex server, including music libraries and audiobook libraries.

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

This Ability requires two OpenHome custom API key values. In the OpenHome dashboard, declare and tag both as required under **Ability Behavior → API Keys**:

- `plex_base_url` — the base URL for the user's Plex server, for example `https://your-plex.example.com` or `http://192.168.1.20:32400`
- `plex_token` — the user's Plex authentication token

### Important Network Note

The OpenHome runtime must be able to reach `plex_base_url`.

- If OpenHome is running in the cloud, use a secure remote Plex URL that is reachable from the internet.
- If OpenHome is running on a local DevKit or device on the same network as Plex, a LAN URL such as `http://192.168.x.x:32400` can work.
- Do not publish a real Plex token or private home URL in this repo.

### Getting a Plex Token

Plex tokens can be found through Plex's official support instructions. In short, sign in to Plex, inspect XML/details for owned media, and copy the `X-Plex-Token` value. Treat it like a password.

Provider URL suggestion for the OpenHome key setup screen:

- `plex_base_url`: `https://support.plex.tv/articles/200288586-installation/`
- `plex_token`: `https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/`

## How It Works

1. The user triggers the Ability and asks for music or an audiobook.
2. The Ability reads `plex_base_url` and `plex_token` with `get_api_keys()`.
3. It searches Plex for audio tracks using Plex's XML API.
4. It filters to `Track` media and infers music vs audiobook from library metadata and duration.
5. It picks the best match for the spoken request.
6. For audiobooks, it saves the selected item and resume offset in Ability context storage.
7. If the user asks to continue/resume, it reloads the saved audiobook and starts from the saved offset.
8. It streams the selected audio through OpenHome audio playback.
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
- Clear setup errors for missing server URL or token

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

Packaging note: keep test stubs and helper-only imports out of `main.py`. The OpenHome editor blocks `from types import ...`, so this Ability uses `namedtuple`-based data containers in `main.py`; local tests may still use `types.ModuleType` for SDK stubs.

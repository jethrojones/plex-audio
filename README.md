# Plex Audio Player for OpenHome

A focused repo for one OpenHome Ability: `plex-audio-player`.

The Ability lets an OpenHome Agent search and play audio-only media from a Plex server, including music and audiobooks. It supports audiobook resume state and Plex connection via either a manual server URL or Plex.tv resource discovery.

It is a **Local Ability**: `main.py` runs in OpenHome's standard Ability runtime (cloud), while `devkit_functions.py` runs on the OpenHome DevKit and talks to Plex over the LAN. This means Plex does not need to be reachable from the internet — no port forwarding or Plex Remote Access required when a DevKit is on the same network as Plex.

## Project layout

```text
community/plex-audio-player/
  main.py               # Standard Ability runtime: voice flow, search choice, resume state
  devkit_functions.py   # Runs on the DevKit: Plex LAN search + local audio playback
  requirements.txt      # DevKit deps (stdlib only — intentionally empty)
  README.md             # Ability setup and usage docs
  __init__.py

tests/
  test_plex_audio_player.py

validate_ability.py
```

## Setup in OpenHome

Upload the packaged Ability ZIP, set the Ability category to **Local**, and sync it to your DevKit from the Live Editor. Then configure API keys as needed:

- `plex_base_url` — Plex server URL **as reachable from the DevKit**, e.g. `http://10.0.0.136:32400`.
- `plex_token` — optional Plex server token. Not needed if Plex allows your local network without auth (Settings → Server → Network → allowed networks) — then the LAN IP and port in `plex_base_url` is the only key required.
- `plex_account_token` — optional token for Plex.tv resource discovery.
- `plex_server_name` — optional selector when the Plex account has multiple servers.
- `plex_machine_identifier` — optional exact Plex server selector.

If `plex_base_url` is present, the Ability uses it first. If not, it tries Plex.tv resource discovery with `plex_account_token` (preferring LAN connection URLs).

If no DevKit is connected (or the DevKit cannot reach Plex), the Ability falls back to streaming through the standard runtime, which then requires a Plex URL reachable from the internet.

## Test

```bash
pytest tests/test_plex_audio_player.py -q
python validate_ability.py community/plex-audio-player
```

## Package

```bash
python - <<'PY'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib

ability = 'plex-audio-player'
src = Path('community') / ability
out = Path('build') / f'{ability}.zip'
out.parent.mkdir(exist_ok=True)

with ZipFile(out, 'w', ZIP_DEFLATED) as z:
    for p in sorted(src.rglob('*')):
        if not p.is_file():
            continue
        if '__pycache__' in p.parts or p.suffix in {'.pyc', '.pyo'}:
            continue
        z.write(p, p.relative_to(src.parent))

print(out)
print(hashlib.sha256(out.read_bytes()).hexdigest())
PY
```

## Notes

OpenHome's live editor blocks some imports that work locally. Keep `main.py` conservative: no `socket`, `urllib`, `types`, raw `open()`, `print()`, `eval()`, or `exec()`.

`devkit_functions.py` is exempt: it runs in the DevKit's normal Python environment, so it may use `urllib`, `subprocess`, `open()`, and `print()` (stdout is the return channel to `main.py`). The search-scoring helpers are duplicated between the two files on purpose — the two runtimes cannot import each other.

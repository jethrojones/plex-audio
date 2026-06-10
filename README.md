# Plex Audio Player for OpenHome

A focused repo for one OpenHome Ability: `plex-audio-player`.

The Ability lets an OpenHome Agent search and play audio-only media from a Plex server, including music and audiobooks. It supports audiobook resume state and Plex connection via either a manual server URL or Plex.tv resource discovery.

## Project layout

```text
community/plex-audio-player/
  main.py       # OpenHome Ability
  README.md    # Ability setup and usage docs
  __init__.py

tests/
  test_plex_audio_player.py

validate_ability.py
```

## Setup in OpenHome

Upload the packaged Ability ZIP, then configure API keys as needed:

- `plex_base_url` — optional manual Plex server URL, e.g. a working `plex.direct` URL or `http://HOST:32400`.
- `plex_token` — optional Plex server token.
- `plex_account_token` — optional token for Plex.tv resource discovery.
- `plex_server_name` — optional selector when the Plex account has multiple servers.
- `plex_machine_identifier` — optional exact Plex server selector.

If `plex_base_url` is present, the Ability uses it first. If not, it tries Plex.tv resource discovery with `plex_account_token`.

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

"""DevKit-side LED ring visualizer for the Plex Audio Player Ability.

This file runs on the OpenHome DevKit (a normal Python environment on the
device), not in the standard Ability runtime. Its only job is the audio-reactive
LED ring: while music streams from the OpenHome cloud, the DevKit's PulseAudio
sink carries the audio, and this visualizer reads that monitor source and pulses
the 24-pixel WS2812B ring in time with the music. There is no Plex networking and
no media playback here — all playback is handled by the cloud runtime (main.py).

Protocol: every registered function prints exactly one JSON object to stdout:
    {"success": bool, "data": {...}, "error": null | {"code": str, "message": str}}
main.py reads that payload from result["output"] of send_devkit_capability_action().
"""

import json
import math
import os
import signal
import struct
import subprocess
import sys
import time

# audioop was removed in Python 3.13; fall back to manual RMS if it's gone.
try:
    import audioop  # type: ignore
except Exception:
    audioop = None

try:
    from devkit_utils.devkit_logging import web_logger as log
except Exception:  # standalone/local test runs without devkit_utils
    import logging

    log = logging.getLogger("plex-audio-devkit")
    log.addHandler(logging.NullHandler())


PULSE_USER_ID = "1000"
PULSE_ENV_EXTRAS = {
    "PULSE_RUNTIME_PATH": "/run/user/%s/pulse" % PULSE_USER_ID,
    "PULSE_SERVER": "unix:/run/user/%s/pulse/native" % PULSE_USER_ID,
    "XDG_RUNTIME_DIR": "/run/user/%s" % PULSE_USER_ID,
}


def _print_payload(success, data=None, error=None):
    sys.stdout.write(json.dumps({"success": bool(success), "data": data or {}, "error": error}) + "\n")


# ---------------------------------------------------------------------------
# LED music visualizer constants and pure helpers
# ---------------------------------------------------------------------------

LED_COUNT = 24
LED_PIN = 12
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_BRIGHTNESS = 180
LED_INVERT = False
LED_CHANNEL = 0

MONITOR_SOURCE = "alsa_output.platform-soc_sound.stereo-fallback.monitor"
SAMPLE_RATE = 22050
CHANNELS = 1
SAMPLE_BYTES = 2  # s16le
FRAME_SAMPLES = 882
CHUNK_BYTES = FRAME_SAMPLES * SAMPLE_BYTES * CHANNELS

FRAME_INTERVAL = 0.03  # ~33 fps render cadence
MAX_PAREC_RESTARTS = 5

ATTACK = 0.6
DECAY = 0.08
FLOOR_RISE = 0.001
FLOOR_FALL = 0.02
PEAK_DECAY = 0.02
MIN_DYNAMIC_RANGE = 0.01
PEAK_DOT_DECAY = 0.6

# Colour palette: cool blue/teal/violet gradient (mirrors led_mode.py)
PALETTE = [
    (40, 90, 255),
    (35, 140, 220),
    (45, 190, 140),
    (70, 80, 210),
    (110, 55, 220),
    (70, 65, 255),
]


def palette_color(t):
    """Interpolated (r, g, b) for a continuous position ``t`` around the palette."""
    i = int(t) % len(PALETTE)
    j = (i + 1) % len(PALETTE)
    f = t - int(t)
    c1 = PALETTE[i]
    c2 = PALETTE[j]
    return (
        int(c1[0] + (c2[0] - c1[0]) * f),
        int(c1[1] + (c2[1] - c1[1]) * f),
        int(c1[2] + (c2[2] - c1[2]) * f),
    )


def viz_scale(rgb, s):
    """Multiply an (r, g, b) tuple by a brightness scalar."""
    r, g, b = rgb
    return (int(r * s), int(g * s), int(b * s))


def rms_from_bytes(chunk, sample_stride=1):
    """RMS amplitude (0.0..1.0) of signed 16-bit little-endian PCM bytes."""
    if not chunk:
        return 0.0
    usable = len(chunk) - (len(chunk) % SAMPLE_BYTES)
    if usable <= 0:
        return 0.0
    if audioop is not None and sample_stride == 1:
        return audioop.rms(chunk[:usable], SAMPLE_BYTES) / 32768.0
    count = usable // SAMPLE_BYTES
    total = 0.0
    n = 0
    for i in range(0, count, sample_stride):
        (sample,) = struct.unpack_from("<h", chunk, i * SAMPLE_BYTES)
        total += sample * sample
        n += 1
    if n == 0:
        return 0.0
    return math.sqrt(total / n) / 32768.0


def update_envelope(prev, level, attack=ATTACK, decay=DECAY):
    """Fast-attack / slow-decay smoothing of the raw level."""
    if level > prev:
        return prev + (level - prev) * attack
    return prev + (level - prev) * decay


def normalize_level(value, floor, peak, min_range=MIN_DYNAMIC_RANGE):
    """Auto-scale ``value`` into 0.0..1.0 against the rolling floor and peak."""
    span = peak - floor
    if span < min_range:
        span = min_range
    norm = (value - floor) / span
    if norm < 0.0:
        return 0.0
    if norm > 1.0:
        return 1.0
    return norm


def level_to_lit_count(level, count=LED_COUNT):
    """How many pixels (0..count) are lit for a normalized 0..1 level."""
    if level <= 0.0:
        return 0
    if level >= 1.0:
        return count
    lit = int(round(level * count))
    if lit < 1:
        lit = 1
    if lit > count:
        lit = count
    return lit


def peak_dot_index(peak_level, count=LED_COUNT):
    """Pixel index the bright peak dot rides for a normalized peak level."""
    if peak_level <= 0.0:
        return -1
    idx = int(round(peak_level * (count - 1)))
    if idx < 0:
        idx = 0
    if idx > count - 1:
        idx = count - 1
    return idx


def _build_parec_command():
    return [
        "parec",
        "--device=%s" % MONITOR_SOURCE,
        "--format=s16le",
        "--rate=%d" % SAMPLE_RATE,
        "--channels=%d" % CHANNELS,
        "--latency-msec=30",
    ]


def _viz_spawn_parec():
    env = os.environ.copy()
    env.update(PULSE_ENV_EXTRAS)
    return subprocess.Popen(
        _build_parec_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _viz_read_exact(stream, nbytes):
    """Read exactly ``nbytes`` from ``stream``; return b'' on EOF/short read."""
    buf = bytearray()
    while len(buf) < nbytes:
        remaining = nbytes - len(buf)
        try:
            piece = stream.read(remaining)
        except Exception:
            return b""
        if not piece:
            return b""
        buf.extend(piece)
    return bytes(buf)


def _viz_kill_previous():
    """Kill any previous LED owner (mirrors led_mode.py.kill_previous())."""
    if not os.path.exists(LED_PID_FILE):
        return
    try:
        with open(LED_PID_FILE, "r") as f:
            old_pid = int(f.read().strip())
    except (ValueError, OSError):
        try:
            os.remove(LED_PID_FILE)
        except OSError:
            pass
        return
    if old_pid == os.getpid():
        return
    try:
        os.kill(old_pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    for _ in range(40):
        try:
            os.kill(old_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(old_pid, signal.SIGKILL)
            time.sleep(0.1)
        except ProcessLookupError:
            pass


def _viz_write_pid():
    with open(LED_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _viz_remove_pid():
    """Remove the pid file only if it's still ours."""
    try:
        with open(LED_PID_FILE, "r") as f:
            if int(f.read().strip()) == os.getpid():
                os.remove(LED_PID_FILE)
    except (ValueError, OSError):
        pass


def _viz_init_strip():
    """Create and initialize the WS2812B strip (lazy rpi_ws281x import)."""
    from rpi_ws281x import Color, PixelStrip, ws  # noqa: PLC0415
    strip_type = ws.WS2811_STRIP_GRB
    strip = PixelStrip(
        LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
        LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL,
        strip_type=strip_type,
    )
    strip.begin()
    from rpi_ws281x import Color  # noqa: PLC0415
    for i in range(LED_COUNT):
        strip.setPixelColor(i, Color(0, 0, 0))
    strip.show()
    time.sleep(0.05)
    return strip


def _viz_clear_strip(strip):
    from rpi_ws281x import Color  # noqa: PLC0415
    for i in range(LED_COUNT):
        strip.setPixelColor(i, Color(0, 0, 0))
    strip.show()


def _viz_render_frame(strip, lit_count, norm_level, peak_idx, palette_pos):
    """Paint one VU-meter frame."""
    from rpi_ws281x import Color  # noqa: PLC0415
    base_rgb = palette_color(palette_pos)
    bar_scale = 0.15 + 0.85 * norm_level
    bar_rgb = viz_scale(base_rgb, bar_scale)
    for i in range(LED_COUNT):
        if i < lit_count:
            strip.setPixelColor(i, Color(*bar_rgb))
        else:
            strip.setPixelColor(i, Color(0, 0, 0))
    if peak_idx >= 0:
        dot_rgb = palette_color(palette_pos + 1.5)
        strip.setPixelColor(peak_idx, Color(*dot_rgb))
    strip.show()


def leds_viz_run(debug=""):
    """Long-running LED visualizer daemon — spawned by leds_viz_start, never via node server.

    Captures PulseAudio monitor with parec, computes per-frame RMS, runs
    fast-attack/slow-decay envelope, and renders a VU-meter bounce on the
    24-pixel WS2812B ring on GPIO 12.
    """
    try:
        from rpi_ws281x import PixelStrip, ws  # noqa: PLC0415
        _ws281x_ok = True
    except Exception as exc:
        log.warning("[viz] rpi_ws281x unavailable: %s", exc)
        _ws281x_ok = False

    if not _ws281x_ok:
        sys.exit(1)

    _viz_kill_previous()
    _viz_write_pid()
    try:
        strip = _viz_init_strip()
    except Exception as exc:
        log.warning("[viz] strip init failed: %s", exc)
        _viz_remove_pid()
        sys.exit(1)

    class _Stopper:
        running = True

        def stop(self, *_args):
            self.running = False

    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper.stop)
    signal.signal(signal.SIGINT, stopper.stop)

    envelope = 0.0
    floor = 0.0
    peak = MIN_DYNAMIC_RANGE
    peak_dot = 0.0
    palette_pos = 0.0
    restarts = 0
    proc = None

    last_debug_t = time.time()
    debug_raw = 0.0
    debug_norm = 0.0
    do_debug = bool(debug)

    try:
        proc = _viz_spawn_parec()
        while stopper.running:
            frame_start = time.time()

            chunk = b""
            if proc is not None and proc.stdout is not None:
                chunk = _viz_read_exact(proc.stdout, CHUNK_BYTES)

            if not chunk:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                restarts += 1
                if restarts > MAX_PAREC_RESTARTS:
                    log.warning("[viz] parec failed %d times, exiting", restarts)
                    break
                time.sleep(0.2)
                try:
                    proc = _viz_spawn_parec()
                except Exception as exc:
                    log.warning("[viz] parec respawn failed: %s", exc)
                    break
                continue

            raw = rms_from_bytes(chunk)
            envelope = update_envelope(envelope, raw)

            if envelope < floor:
                floor += (envelope - floor) * FLOOR_FALL
            else:
                floor += (envelope - floor) * FLOOR_RISE
            if envelope > peak:
                peak = envelope
            else:
                peak += (max(envelope, floor + MIN_DYNAMIC_RANGE) - peak) * PEAK_DECAY

            norm = normalize_level(envelope, floor, peak)
            lit = level_to_lit_count(norm)

            target_dot = peak_dot_index(norm)
            if target_dot > peak_dot:
                peak_dot = float(target_dot)
            else:
                peak_dot = max(0.0, peak_dot - PEAK_DOT_DECAY)
            peak_idx = int(round(peak_dot)) if norm > 0.0 else -1

            palette_pos = (palette_pos + 0.01) % len(PALETTE)
            _viz_render_frame(strip, lit, norm, peak_idx, palette_pos)

            if do_debug:
                debug_raw = raw
                debug_norm = norm
                now = time.time()
                if now - last_debug_t >= 1.0:
                    log.info(
                        "[viz] rms=%.5f envelope=%.5f floor=%.5f peak=%.5f norm=%.3f lit=%d",
                        debug_raw, envelope, floor, peak, debug_norm, lit,
                    )
                    last_debug_t = now

            elapsed = time.time() - frame_start
            if elapsed < FRAME_INTERVAL:
                time.sleep(FRAME_INTERVAL - elapsed)
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            _viz_clear_strip(strip)
        except Exception:
            pass
        _viz_remove_pid()


# ---------------------------------------------------------------------------
# LED music visualizer control
# ---------------------------------------------------------------------------

LED_PID_FILE = "/run/led_controller.pid"
VIZ_ENTRY_POINT = "leds_viz_run"


def _pid_running(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _led_pid_owner():
    """Return the pid in /run/led_controller.pid if it's alive, else None."""
    try:
        with open(LED_PID_FILE, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return None
    if not _pid_running(pid):
        return None
    return pid


def _cmdline_is_viz(pid):
    """True when /proc/<pid>/cmdline shows our visualizer entry point (not led_mode.py)."""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    return VIZ_ENTRY_POINT in cmdline


def leds_viz_start():
    """Start the LED music visualizer, unless one is already running.

    Spawns this file with 'leds_viz_run' as the function argument so the
    visualizer runs inside devkit_functions.py — no separate script needed.
    Never spawns a duplicate: if /run/led_controller.pid already points at a
    live visualizer process, report that pid instead.
    """
    existing = _led_pid_owner()
    if existing and _cmdline_is_viz(existing):
        _print_payload(True, {"running": True, "pid": existing})
        return
    try:
        env = os.environ.copy()
        env.update(PULSE_ENV_EXTRAS)
        process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), VIZ_ENTRY_POINT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        log.info("[PlexAudio] LED visualizer started (pid %s)", process.pid)
        _print_payload(True, {"running": True, "pid": process.pid})
    except Exception as exc:
        log.warning("[PlexAudio] LED visualizer start failed: %s", exc)
        _print_payload(False, {}, {"code": "viz_start_failed", "message": str(exc)})


def leds_viz_stop():
    """Stop our LED visualizer if it owns the strip; never touch led_mode.py.

    SIGTERM lets the visualizer's own handler clear the strip and drop the pid
    file. Reports stopped=False (still success) when no visualizer is running.
    """
    pid = _led_pid_owner()
    if pid and _cmdline_is_viz(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            _print_payload(True, {"stopped": True, "pid": pid})
            return
        except OSError as exc:
            log.warning("[PlexAudio] LED visualizer stop failed: %s", exc)
            _print_payload(False, {}, {"code": "viz_stop_failed", "message": str(exc)})
            return
    _print_payload(True, {"stopped": False})


FUNCTION_REGISTRY = {
    "leds_viz_start": leds_viz_start,
    "leds_viz_stop": leds_viz_stop,
    "leds_viz_run": leds_viz_run,
}

if __name__ == "__main__":
    function_name = sys.argv[1]
    FUNCTION_REGISTRY[function_name](*sys.argv[2:])

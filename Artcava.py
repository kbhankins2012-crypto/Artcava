#!/usr/bin/env python3
"""
artcava - a cava-style terminal visualizer whose bars take their colors
from the album art of whatever is currently playing.

Audio : captured from your default output's monitor via `parec` (PulseAudio / PipeWire)
Art   : read from MPRIS via `playerctl` (Spotify, browsers playing YouTube / YT Music, etc.)

Deps  : sudo apt install python3-numpy python3-pil playerctl pulseaudio-utils

Usage : python3 artcava.py                  # follow whatever is playing
        python3 artcava.py cover.jpg        # force a local image / URL (for testing)
        python3 artcava.py --debug          # show palette swatches + art URL on the bottom line
"""
import colorsys
import io
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import json
import os
import select
import termios
import tty

import numpy as np
from PIL import Image

RATE = 44100
FFT_SIZE = 2048                # longer window = steadier bars (try 1024 for snappier)
N_FFT = 4096
FPS = 60
FREQ_LO, FREQ_HI = 40, 12000
BAR_W, GAP = 2, 1
ATTACK_TIME = 0.028            # seconds; lower = quicker rise, higher = smoother rise
RELEASE_TIME = 0.19            # seconds; higher = smoother, longer bar decay
SPREAD = 0.52                  # neighbor smoothing 0..1; keeps the CAVA-style hills
COLOR_EASE_TIME = 0.22         # seconds for album-art palette transitions
BEAT_FLASH = 0.32              # subtle beat flash; avoids harsh white flicker
BLOCKS = " ▁▂▃▄▅▆▇█"
DEFAULT_PALETTE = [(80, 160, 255), (200, 90, 255), (255, 90, 160)]
CONFIG_DIR = os.path.expanduser("~/.config/artcava")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
COLOR_PRESETS = {
    "Album art": None,
    "Ocean": [(35, 110, 255), (40, 220, 220), (190, 245, 255)],
    "Fire": [(255, 45, 25), (255, 145, 25), (255, 225, 80)],
    "Forest": [(20, 110, 55), (80, 190, 90), (205, 235, 120)],
    "Purple": [(90, 35, 180), (165, 75, 255), (235, 175, 255)],
    "Pink": [(255, 55, 150), (255, 125, 195), (255, 220, 240)],
    "Red": [(130, 0, 0), (230, 25, 35), (255, 115, 105)],
    "Green": [(0, 90, 35), (20, 180, 75), (150, 255, 170)],
    "Blue": [(0, 45, 130), (25, 120, 255), (145, 205, 255)],
    "White": [(130, 145, 165), (210, 220, 235), (255, 255, 255)],
}
SETTINGS = {"color_mode": "Album art", "bar_width": BAR_W, "gap": GAP,
            "sensitivity": 1.0, "release_time": RELEASE_TIME, "beat_flash": BEAT_FLASH}


def load_settings():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for key in SETTINGS:
            if key in saved:
                SETTINGS[key] = saved[key]
    except (OSError, ValueError, TypeError):
        pass
    SETTINGS["color_mode"] = SETTINGS["color_mode"] if SETTINGS["color_mode"] in COLOR_PRESETS else "Album art"
    SETTINGS["bar_width"] = max(1, min(4, int(SETTINGS["bar_width"])))
    SETTINGS["gap"] = max(0, min(4, int(SETTINGS["gap"])))
    SETTINGS["sensitivity"] = max(0.5, min(2.5, float(SETTINGS["sensitivity"])))
    SETTINGS["release_time"] = max(0.06, min(0.5, float(SETTINGS["release_time"])))
    SETTINGS["beat_flash"] = max(0.0, min(1.0, float(SETTINGS["beat_flash"])))


def save_settings():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, indent=2)


def active_palette(album_palette):
    preset = COLOR_PRESETS[SETTINGS["color_mode"]]
    return album_palette if preset is None else preset


def settings_menu():
    """Small btop-inspired keyboard settings panel; called from the visualizer."""
    rows = ["Color theme", "Bar width", "Bar spacing", "Sensitivity", "Bar smoothing", "Beat flash"]
    selected = 0
    color_names = list(COLOR_PRESETS)
    help_text = "↑/↓ select   ←/→ change   Enter change   S/Esc close"
    def value_for(i):
        return [SETTINGS["color_mode"], str(SETTINGS["bar_width"]), str(SETTINGS["gap"]),
                f'{SETTINGS["sensitivity"]:.1f}x', f'{SETTINGS["release_time"]:.2f}s',
                f'{SETTINGS["beat_flash"]:.2f}'][i]
    def change(direction):
        if selected == 0:
            idx = color_names.index(SETTINGS["color_mode"])
            SETTINGS["color_mode"] = color_names[(idx + direction) % len(color_names)]
        elif selected == 1:
            SETTINGS["bar_width"] = max(1, min(4, SETTINGS["bar_width"] + direction))
        elif selected == 2:
            SETTINGS["gap"] = max(0, min(4, SETTINGS["gap"] + direction))
        elif selected == 3:
            SETTINGS["sensitivity"] = round(max(0.5, min(2.5, SETTINGS["sensitivity"] + direction * 0.1)), 1)
        elif selected == 4:
            SETTINGS["release_time"] = round(max(0.06, min(0.5, SETTINGS["release_time"] + direction * 0.02)), 2)
        elif selected == 5:
            SETTINGS["beat_flash"] = round(max(0.0, min(1.0, SETTINGS["beat_flash"] + direction * 0.05)), 2)
    while True:
        sys.stdout.write("\x1b[2J\x1b[H\x1b[1;36m  A R T C A V A   /   S E T T I N G S\x1b[0m\n\n")
        sys.stdout.write("  Navigate with arrow keys. Changes apply immediately.\n\n")
        for i, label in enumerate(rows):
            marker = "\x1b[1;36m ❯ " if i == selected else "   "
            end = "\x1b[0m" if i == selected else ""
            sys.stdout.write(f"{marker}{label:<18} {value_for(i):>12}{end}\n")
        sys.stdout.write("\n  "+help_text+"\n")
        sys.stdout.flush()
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # Read the rest of an arrow-key escape sequence if present.
            if select.select([sys.stdin], [], [], 0.025)[0]:
                seq = sys.stdin.read(1)
                if seq == "[" and select.select([sys.stdin], [], [], 0.025)[0]:
                    arrow = sys.stdin.read(1)
                    if arrow == "A": selected = (selected - 1) % len(rows)
                    elif arrow == "B": selected = (selected + 1) % len(rows)
                    elif arrow == "C": change(1)
                    elif arrow == "D": change(-1)
            else:
                save_settings(); return
        elif ch in "qQ":
            save_settings(); return
        elif ch in "sS":
            save_settings(); return
        elif ch in "\r\n":
            change(1)
        elif ch in "+=":
            change(1)
        elif ch == "-":
            change(-1)
MEDIANCUT = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT")


# ---------------------------------------------------------------- album art --
def load_image(src):
    if src.startswith("file://"):
        src = src[7:]
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "artcava"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return Image.open(io.BytesIO(r.read()))
    return Image.open(src)


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def extract_palette(img, n=4):
    img = img.convert("RGB")
    # crop to the center square: drops black letterbox bars on 16:9 video thumbnails
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    img = img.resize((96, 96))

    q = img.quantize(colors=12, method=MEDIANCUT)
    pal = q.getpalette()
    total = 96 * 96
    cands = []
    for count, idx in q.getcolors():
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        hh, sat, val = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        if val < 0.15:                                   # skip near-black
            continue
        score = (count / total) ** 0.5 * (0.25 + sat) * (0.4 + val)
        # make it readable on a dark terminal, but keep gray/white art neutral
        if sat >= 0.2:
            sat, val = max(sat, 0.45), max(val, 0.75)
        else:
            val = max(val, 0.65)
        vivid = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hh, sat, val))
        cands.append((score, vivid))
    cands.sort(reverse=True)

    chosen = []
    for score, rgb in cands:
        if all(_dist(rgb, c) > 80 for c in chosen):      # keep clearly different colors
            chosen.append(rgb)
        if len(chosen) == n:
            break
    if not chosen:
        return DEFAULT_PALETTE

    # order as a chain of nearest neighbors, starting from the most dominant color,
    # so the gradient doesn't jump across the color wheel
    ordered = [chosen.pop(0)]
    while chosen:
        nxt = min(chosen, key=lambda c: _dist(c, ordered[-1]))
        chosen.remove(nxt)
        ordered.append(nxt)
    if len(ordered) == 1:
        ordered.append(ordered[0])
    return ordered


def gradient(colors, n):
    stops = np.array(colors, dtype=float)
    xs = np.linspace(0, 1, len(stops))
    t = np.linspace(0, 1, n)
    return np.stack([np.interp(t, xs, stops[:, c]) for c in range(3)], axis=1)


class ArtWatcher(threading.Thread):
    def __init__(self, forced=None):
        super().__init__(daemon=True)
        self.palette = DEFAULT_PALETTE
        self.forced = forced
        self.last_url = None
        self.fails = 0
        self.empties = 0

    def current_url(self):
        """Art URL of the player that is actually Playing (not just the first one)."""
        if self.forced:
            return self.forced
        try:
            out = subprocess.run(
                ["playerctl", "-a", "metadata", "--format", "{{status}}|{{mpris:artUrl}}"],
                capture_output=True, text=True, timeout=2)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        paused_url = None
        for line in out.stdout.splitlines():
            status, _, url = line.partition("|")
            if status.strip() == "Playing":
                return url.strip()
            if url.strip() and paused_url is None:
                paused_url = url.strip()
        return paused_url          # nothing playing: keep showing the last paused player's art

    def run(self):
        while True:
            url = self.current_url()
            if url is None:
                pass
            elif url == self.last_url:
                self.fails = 0
            elif url:
                self.empties = 0
                try:
                    self.palette = extract_palette(load_image(url))
                    self.last_url = url
                    self.fails = 0
                except Exception:
                    self.fails += 1
                    if self.fails >= 3:                  # art keeps failing: stop showing old colors
                        self.palette = DEFAULT_PALETTE
            else:
                self.empties += 1
                if self.empties >= 3 and self.last_url != "":   # no art for a while (ad, etc.)
                    self.palette = DEFAULT_PALETTE
                    self.last_url = ""
            time.sleep(0.5)


# ------------------------------------------------------------------- audio --
class AudioReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.proc = subprocess.Popen(
            ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1",
             "--latency-msec=10", "-d", "@DEFAULT_MONITOR@"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.buf = np.zeros(FFT_SIZE, dtype=np.float64)
        self.buf_lock = threading.Lock()

    def snapshot(self):
        with self.buf_lock:
            return self.buf.copy()

    def run(self):
        leftover = b""
        while True:
            data = self.proc.stdout.read(2048)
            if not data:
                break
            data = leftover + data
            if len(data) % 2:
                leftover, data = data[-1:], data[:-1]
            else:
                leftover = b""
            new = np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768
            with self.buf_lock:
                self.buf = np.concatenate((self.buf, new))[-FFT_SIZE:]


class Analyzer:
    def __init__(self, nbars):
        self.nbars = nbars
        self.window = np.hanning(FFT_SIZE)
        self.freqs = np.fft.rfftfreq(N_FFT, 1 / RATE)
        edges = np.geomspace(FREQ_LO, FREQ_HI, nbars + 1)
        self.centers = np.sqrt(edges[:-1] * edges[1:])
        self.slices = []
        for i in range(nbars):
            lo = int(np.searchsorted(self.freqs, edges[i]))
            hi = int(np.searchsorted(self.freqs, edges[i + 1]))
            self.slices.append((lo, hi))
        self.tilt = np.linspace(1.0, 2.5, nbars)
        self.peak = 2.0
        self.vals = np.zeros(nbars)
        self.bass_n = max(1, nbars // 8)
        self.bass_avg = 0.0
        self.pulse = 0.0

    def update(self, samples, dt):
        mag = np.abs(np.fft.rfft(samples * self.window, n=N_FFT))
        raw = np.empty(self.nbars)
        for i, (lo, hi) in enumerate(self.slices):
            if hi - lo >= 2:
                raw[i] = mag[lo:hi].mean()                       # wide bars: average
            else:
                raw[i] = np.interp(self.centers[i], self.freqs, mag)   # narrow (bass) bars: interpolate
        raw *= self.tilt * SETTINGS["sensitivity"]
        self.peak = max(self.peak * 0.5 ** (dt / 4), raw.max(), 2.0)
        v = (raw / self.peak) ** 0.7

        # neighbor smoothing: each bar lifts the bars next to it (cava's "monstercat" look)
        sm = v.copy()
        for d in range(1, 5):
            f = SPREAD ** d
            if d < self.nbars:
                sm[d:] = np.maximum(sm[d:], v[:-d] * f)
                sm[:-d] = np.maximum(sm[:-d], v[d:] * f)
        v = np.clip(sm, 0.0, 1.0)

        # Smooth, frame-rate-independent attack and release.
        # Exponential release avoids the abrupt, uneven drop that can happen
        # when the terminal misses a frame.
        attack = 1.0 - np.exp(-dt / ATTACK_TIME)
        release = 1.0 - np.exp(-dt / SETTINGS["release_time"])
        rising = v > self.vals
        coeff = np.where(rising, attack, release)
        self.vals += (v - self.vals) * coeff

        bass = float(v[:self.bass_n].mean())
        hit = max(0.0, bass - self.bass_avg * 1.15)
        self.bass_avg += (bass - self.bass_avg) * min(1.0, dt * 6)
        self.pulse = max(self.pulse * 0.82 ** (dt * 60), min(1.0, hit * 5))
        return self.vals


# ------------------------------------------------------------------ render --
def render(vals, colors, rows, pulse, status=""):
    flash = colors + (255 - colors) * (pulse * SETTINGS["beat_flash"])
    q = (np.clip(flash, 0, 255).astype(int) >> 3) << 3
    prefixes = [f"\x1b[38;2;{r};{g};{b}m" for r, g, b in q]
    heights = vals * rows * 8
    gap = " " * SETTINGS["gap"]
    lines = []
    for row in range(rows):
        base = (rows - 1 - row) * 8
        parts, last = [], None
        for i, h in enumerate(heights):
            e = int(min(8, max(0, h - base)))
            if prefixes[i] != last:
                parts.append(prefixes[i])
                last = prefixes[i]
            parts.append(BLOCKS[e] * SETTINGS["bar_width"] + gap)
        lines.append("".join(parts) + "\x1b[0m")
    if status:
        lines.append(status + "\x1b[0m\x1b[K")
    return "\x1b[H" + "\n".join(lines)


def status_line(watcher):
    sw = "".join(f"\x1b[48;2;{r};{g};{b}m    " for r, g, b in watcher.palette)
    return f"{sw}\x1b[0m {str(watcher.last_url)[-60:]}"


def main():
    load_settings()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv
    forced = args[0] if args else None
    watcher = ArtWatcher(forced)
    watcher.start()
    try:
        reader = AudioReader()
    except FileNotFoundError:
        sys.exit("parec not found - install pulseaudio-utils (works with PipeWire too).")
    reader.start()

    size = None
    analyzer = None
    old_tty = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())
    current = None
    last = time.perf_counter()
    next_t = last

    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
    try:
        while True:
            now = time.perf_counter()
            dt = min(0.05, max(1e-3, now - last))
            last = now

            # Press S at any time to open the settings panel.
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1)
                if key in "sS":
                    settings_menu()
                    size = None  # force a clean redraw after leaving settings
                    next_t = time.perf_counter()
                    continue
                elif key in "qQ":
                    break
            cols, lines = shutil.get_terminal_size()
            if (cols, lines) != size:
                size = (cols, lines)
                nbars = max(1, (cols + SETTINGS["gap"]) // (SETTINGS["bar_width"] + SETTINGS["gap"]))
                analyzer = Analyzer(nbars)
                current = gradient(active_palette(watcher.palette), nbars)
                sys.stdout.write("\x1b[2J")

            target = gradient(active_palette(watcher.palette), analyzer.nbars)
            color_ease = 1.0 - np.exp(-dt / COLOR_EASE_TIME)
            current += (target - current) * color_ease

            vals = analyzer.update(reader.snapshot(), dt)
            rows = max(1, lines - (2 if debug else 1))
            sys.stdout.write(render(vals, current, rows, analyzer.pulse,
                                    status_line(watcher) if debug else ""))
            sys.stdout.flush()

            next_t += 1 / FPS                    # fixed schedule = even frame spacing
            spare = next_t - time.perf_counter()
            if spare > 0:
                time.sleep(spare)
            else:
                next_t = time.perf_counter()     # fell behind: resync instead of bursting
    except KeyboardInterrupt:
        pass
    finally:
        reader.proc.terminate()
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
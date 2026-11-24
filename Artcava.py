#!/usr/bin/env python3
"""
Artcava - a terminal music visualizer with album-art colors, synced lyrics,
and interactive playback controls.

Audio : captured from your default output's monitor via `parec`
Art   : read from MPRIS via `playerctl`

Deps  : python3-numpy, python3-pil, playerctl, pulseaudio-utils

Keys  : S = settings   Q = quit   L = lyrics on/off   T = song title on/off
        A = previous track   Space = pause/resume   D = next track
        [ ] = nudge lyric timing
        Visualization modes: Bars, Orbit, Starfield, Beat Chaser

Kitty transparency:
    Add these to ~/.config/kitty/kitty.conf:
        allow_remote_control yes
        dynamic_background_opacity yes

    Opacity is changed live using Kitty's remote-control interface.
"""
import bisect
import colorsys
import hashlib
import io
import json
import os
import random
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
import urllib.parse
import urllib.request
from collections import deque

import numpy as np
from PIL import Image

RATE = 44100
FFT_SIZE = 2048
N_FFT = 4096
FPS = 60
FREQ_LO, FREQ_HI = 40, 12000
BAR_W, GAP = 2, 1
ATTACK_TIME = 0.028
RELEASE_TIME = 0.19
SPREAD = 0.52
COLOR_EASE_TIME = 0.22
BEAT_FLASH = 0.32
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

VISUALIZATIONS = ["Bars", "Orbit", "Starfield", "Beat Chaser"]

DEFAULTS = {
    "visualization": "Bars",
    "color_mode": "Album art",
    "bar_width": BAR_W,
    "gap": GAP,
    "sensitivity": 1.0,
    "release_time": RELEASE_TIME,
    "beat_flash": BEAT_FLASH,
    "lyrics": True,
    "lyrics_offset": 0.0,
    "show_title": True,
    "opacity": 1.0,
}

SETTINGS = dict(DEFAULTS)

LIMITS = [
    ("bar_width", 1, 4, int),
    ("gap", 0, 4, int),
    ("sensitivity", 0.5, 2.5, float),
    ("release_time", 0.06, 0.5, float),
    ("beat_flash", 0.0, 1.0, float),
    ("lyrics_offset", -5.0, 5.0, float),
    ("opacity", 0.10, 1.0, float),
]


# -------------------------------------------------------------- settings --
def load_settings():
    """Load saved settings; missing or corrupt values use defaults."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}

    for key in DEFAULTS:
        if key in saved:
            SETTINGS[key] = saved[key]

    if not isinstance(SETTINGS["visualization"], str) or SETTINGS["visualization"] not in VISUALIZATIONS:
        SETTINGS["visualization"] = DEFAULTS["visualization"]

    if not isinstance(SETTINGS["color_mode"], str) or SETTINGS["color_mode"] not in COLOR_PRESETS:
        SETTINGS["color_mode"] = DEFAULTS["color_mode"]

    for flag in ("lyrics", "show_title"):
        if not isinstance(SETTINGS[flag], bool):
            SETTINGS[flag] = DEFAULTS[flag]

    for key, lo, hi, cast in LIMITS:
        try:
            value = cast(SETTINGS[key])
            if value != value:
                raise ValueError
            SETTINGS[key] = max(lo, min(hi, value))
        except (TypeError, ValueError, OverflowError):
            SETTINGS[key] = DEFAULTS[key]


def save_settings():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2)
    except OSError:
        pass


def active_palette(album_palette):
    preset = COLOR_PRESETS[SETTINGS["color_mode"]]
    return album_palette if preset is None else preset


# --------------------------------------------------------------- Kitty --
def kitty_available():
    """Return whether this process appears to be running inside Kitty."""
    return bool(os.environ.get("KITTY_WINDOW_ID")) and shutil.which("kitten") is not None


def kitty_opacity(value):
    """Change Kitty's background opacity through its remote-control interface."""
    if not kitty_available():
        return False

    try:
        result = subprocess.run(
            [
                "kitten", "@", "set-background-opacity",
                "--match", f"id:{os.environ['KITTY_WINDOW_ID']}",
                f"{float(value):.2f}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def kitty_configured_opacity():
    """Read background_opacity from Kitty's main config as a restore value."""
    config = os.path.expanduser("~/.config/kitty/kitty.conf")
    opacity = 1.0

    try:
        with open(config, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                match = re.match(r"^background_opacity\s+([0-9.]+)", line)
                if match:
                    try:
                        opacity = max(0.0, min(1.0, float(match.group(1))))
                    except ValueError:
                        pass
    except OSError:
        pass

    return opacity


def restore_kitty_opacity(original):
    """Restore the opacity read from Kitty's configuration."""
    if kitty_available():
        kitty_opacity(original)


# --------------------------------------------------------------- keyboard --
KEY_RE = re.compile(
    r"\x1b\[<\d+;\d+;\d+[mM]|"
    r"\x1b\[[0-9;]*[A-Za-z~]|"
    r"\x1bO[A-Za-z]|"
    r"\x1b|.",
    re.S,
)

UP, DOWN = ("\x1b[A", "\x1bOA"), ("\x1b[B", "\x1bOB")
RIGHT, LEFT = ("\x1b[C", "\x1bOC"), ("\x1b[D", "\x1bOD")
CONTROLS = "[<<] [||] [>>]"


def player_control(action):
    """Send a playback command to the active media player."""
    try:
        subprocess.run(
            ["playerctl", action],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def mouse_action(key, cols):
    """Return a playback action when a header control is clicked."""
    match = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", key)
    if not match:
        return None

    button, x, y, event = match.groups()
    button, x, y = int(button), int(x), int(y)
    if button != 0 or event != "M" or y != 1:
        return None

    start = cols - len(CONTROLS) + 1
    offset = x - start
    if 0 <= offset <= 3:
        return "previous"
    if 5 <= offset <= 8:
        return "play-pause"
    if 10 <= offset <= 13:
        return "next"
    return None


def read_keys(timeout=None):
    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return []

    data = os.read(fd, 64)
    if not data:
        raise EOFError
    return KEY_RE.findall(data.decode("utf-8", "ignore"))


# --------------------------------------------------------------- menu --
def settings_menu():
    """Settings menu with a persistent visualization-mode selector."""
    rows = [
        "Visualization",
        "Color theme",
        "Bar width",
        "Bar spacing",
        "Sensitivity",
        "Bar smoothing",
        "Beat flash",
        "Lyrics",
        "Lyrics timing",
        "Song title",
        "Opacity",
    ]

    selected = 0
    color_names = list(COLOR_PRESETS)
    help_text = "↑/↓ select   ←/→ change   Enter change   S/Esc close"

    def value_for(i):
        values = [
            SETTINGS["visualization"],
            SETTINGS["color_mode"],
            str(SETTINGS["bar_width"]),
            str(SETTINGS["gap"]),
            f'{SETTINGS["sensitivity"]:.1f}x',
            f'{SETTINGS["release_time"]:.2f}s',
            f'{SETTINGS["beat_flash"]:.2f}',
            "On" if SETTINGS["lyrics"] else "Off",
            f'{SETTINGS["lyrics_offset"]:+.2f}s',
            "On" if SETTINGS["show_title"] else "Off",
            f'{SETTINGS["opacity"]:.2f}',
        ]
        return values[i]

    def change(direction):
        if selected == 0:
            idx = VISUALIZATIONS.index(SETTINGS["visualization"])
            SETTINGS["visualization"] = VISUALIZATIONS[
                (idx + direction) % len(VISUALIZATIONS)
            ]
            return f'Visualization: {SETTINGS["visualization"]}'
        elif selected == 1:
            idx = color_names.index(SETTINGS["color_mode"])
            SETTINGS["color_mode"] = color_names[
                (idx + direction) % len(color_names)
            ]
        elif selected == 2:
            SETTINGS["bar_width"] = max(1, min(4, SETTINGS["bar_width"] + direction))
        elif selected == 3:
            SETTINGS["gap"] = max(0, min(4, SETTINGS["gap"] + direction))
        elif selected == 4:
            SETTINGS["sensitivity"] = round(
                max(0.5, min(2.5, SETTINGS["sensitivity"] + direction * 0.1)), 1
            )
        elif selected == 5:
            SETTINGS["release_time"] = round(
                max(0.06, min(0.5, SETTINGS["release_time"] + direction * 0.02)), 2
            )
        elif selected == 6:
            SETTINGS["beat_flash"] = round(
                max(0.0, min(1.0, SETTINGS["beat_flash"] + direction * 0.05)), 2
            )
        elif selected == 7:
            SETTINGS["lyrics"] = not SETTINGS["lyrics"]
        elif selected == 8:
            SETTINGS["lyrics_offset"] = round(
                max(-5.0, min(5.0, SETTINGS["lyrics_offset"] + direction * 0.25)), 2
            )
        elif selected == 9:
            SETTINGS["show_title"] = not SETTINGS["show_title"]
        elif selected == 10:
            new_value = round(
                max(0.10, min(1.0, SETTINGS["opacity"] + direction * 0.05)), 2
            )
            SETTINGS["opacity"] = new_value
            if not kitty_opacity(new_value):
                return "Kitty opacity unavailable - check Kitty remote-control settings"
            return "Kitty opacity updated"

        return None

    message = ""
    while True:
        out = [
            "\x1b[2J\x1b[H\x1b[1;36m A R T C A V A / S E T T I N G S\x1b[0m\n\n",
            " Navigate with arrow keys. Changes apply immediately.\n\n",
        ]
        for i, label in enumerate(rows):
            marker = "\x1b[1;36m ❯ " if i == selected else "   "
            end = "\x1b[0m" if i == selected else ""
            out.append(f"{marker}{label:<18} {value_for(i):>12}{end}\n")

        if message:
            out.append(f"\n {message}\n")
        out.append("\n " + help_text + "\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        for key in read_keys():
            if key in UP:
                selected = (selected - 1) % len(rows)
                message = ""
            elif key in DOWN:
                selected = (selected + 1) % len(rows)
                message = ""
            elif key in RIGHT or key in ("\r", "\n", "+", "="):
                message = change(1) or ""
                save_settings()
            elif key in LEFT or key == "-":
                message = change(-1) or ""
                save_settings()
            elif key in ("\x1b", "s", "S", "q", "Q"):
                save_settings()
                return


MEDIANCUT = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT")


# -------------------------------------------------------------- album art --
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
        if val < 0.15:
            continue
        score = (count / total) ** 0.5 * (0.25 + sat) * (0.4 + val)
        if sat >= 0.2:
            sat, val = max(sat, 0.45), max(val, 0.75)
        else:
            val = max(val, 0.65)
        vivid = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hh, sat, val))
        cands.append((score, vivid))

    cands.sort(reverse=True)
    chosen = []
    for score, rgb in cands:
        if all(_dist(rgb, c) > 80 for c in chosen):
            chosen.append(rgb)
        if len(chosen) == n:
            break

    if not chosen:
        return DEFAULT_PALETTE

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
        if self.forced:
            return self.forced
        try:
            out = subprocess.run(
                ["playerctl", "-a", "metadata", "--format", "{{status}}|{{mpris:artUrl}}"],
                capture_output=True, text=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        paused_url = None
        for line in out.stdout.splitlines():
            status, _, url = line.partition("|")
            if status.strip() == "Playing":
                return url.strip()
            if url.strip() and paused_url is None:
                paused_url = url.strip()
        return paused_url

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
                    if self.fails >= 3:
                        self.palette = DEFAULT_PALETTE
            else:
                self.empties += 1
                if self.empties >= 3 and self.last_url != "":
                    self.palette = DEFAULT_PALETTE
                    self.last_url = ""
            time.sleep(0.5)


# ---------------------------------------------------------------- audio --
class AudioReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.proc = subprocess.Popen(
            [
                "parec", "--format=s16le", f"--rate={RATE}", "--channels=1",
                "--latency-msec=10", "-d", "@DEFAULT_MONITOR@",
            ],
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
                raw[i] = mag[lo:hi].mean()
            else:
                raw[i] = np.interp(self.centers[i], self.freqs, mag)
        raw *= self.tilt * SETTINGS["sensitivity"]
        self.peak = max(self.peak * 0.5 ** (dt / 4), raw.max(), 2.0)
        v = (raw / self.peak) ** 0.7
        sm = v.copy()
        for d in range(1, 5):
            f = SPREAD ** d
            if d < self.nbars:
                sm[d:] = np.maximum(sm[d:], v[:-d] * f)
                sm[:-d] = np.maximum(sm[:-d], v[d:] * f)
        v = np.clip(sm, 0.0, 1.0)
        attack = 1.0 - np.exp(-dt / ATTACK_TIME)
        release = 1.0 - np.exp(-dt / SETTINGS["release_time"])
        rising = v > self.vals
        coeff = np.where(rising, attack, release)
        self.vals += (v - self.vals) * coeff

        bass = float(v[:self.bass_n].mean())
        hit = max(0.0, bass - self.bass_avg * 1.15)
        self.bass_avg += (bass - self.bass_avg) * min(1.0, dt * 6)
        self.pulse = max(
            self.pulse * 0.82 ** (dt * 60), min(1.0, hit * 5)
        )
        return self.vals


# ---------------------------------------------------------------- lyrics --
LRCLIB = "https://lrclib.net/api"
USER_AGENT = "Artcava (https://github.com/kbhankins2012-crypto/Artcava)"
LYRIC_CACHE = os.path.expanduser("~/.cache/artcava/lyrics")
LYRIC_ROWS = 4
HEADER_ROWS = 2
SEP = "\x1f"
LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
LRC_WORD = re.compile(r"<\d+:\d+(?:\.\d+)?>")
TITLE_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|lyric|video|audio|visuali[sz]er|\bhd\b|4k)[^\)\]]*[\)\]]",
    re.I,
)


def parse_lrc(text):
    out = []
    for raw in text.splitlines():
        tags = LRC_TAG.findall(raw)
        if not tags:
            continue
        line = LRC_WORD.sub("", LRC_TAG.sub("", raw)).strip()
        for minutes, seconds in tags:
            out.append((int(minutes) * 60 + float(seconds), line))
    out.sort(key=lambda item: item[0])
    return out


def http_json(path, params):
    url = f"{LRCLIB}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.load(r)


def fetch_synced(artist, title, album, length):
    artist = re.sub(r"\s*-\s*Topic$", "", artist).strip()
    cands = []

    def add(a, t):
        t = TITLE_NOISE.sub("", t).strip()
        if a and t and (a, t) not in cands:
            cands.append((a, t))

    add(artist, title)
    add(artist.split(",")[0].strip(), title)
    if " - " in title:
        a2, t2 = title.split(" - ", 1)
        add(a2.strip(), t2)
    if not cands:
        return None

    if album and length:
        for a, t in cands:
            try:
                d = http_json("get", {
                    "artist_name": a, "track_name": t,
                    "album_name": album, "duration": round(length),
                })
                if d.get("syncedLyrics"):
                    return d["syncedLyrics"]
            except Exception:
                pass

    for a, t in cands:
        for params in (
            {"track_name": t, "artist_name": a},
            {"q": f"{a} {t}"},
        ):
            try:
                results = http_json("search", params)
            except Exception:
                continue
            ok = [r for r in results if isinstance(r, dict) and r.get("syncedLyrics")]
            if length:
                ok.sort(key=lambda r: abs((r.get("duration") or 0) - length))
                if ok and abs((ok[0].get("duration") or 0) - length) <= 10:
                    return ok[0]["syncedLyrics"]
            elif ok:
                return ok[0]["syncedLyrics"]
    return None


def cache_file(artist, title, album, length):
    key = SEP.join([artist, title, album, str(round(length or 0))])
    return os.path.join(LYRIC_CACHE, hashlib.sha1(key.encode()).hexdigest() + ".lrc")


def load_lyrics(artist, title, album, length):
    path = cache_file(artist, title, album, length)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        pass

    text = fetch_synced(artist, title, album, length)
    if text:
        try:
            os.makedirs(LYRIC_CACHE, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
    return text


class LyricsWatcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.data = ("idle", [], [])
        self.key = None
        self.track = None
        self.playing = False
        self.anchor_pos = 0.0
        self.anchor_t = time.monotonic()

    def position(self):
        if self.playing:
            return self.anchor_pos + (time.monotonic() - self.anchor_t)
        return self.anchor_pos

    @staticmethod
    def now_playing():
        fmt = SEP.join([
            "{{playerName}}", "{{status}}", "{{artist}}", "{{title}}",
            "{{album}}", "{{mpris:length}}",
        ])
        try:
            out = subprocess.run(
                ["playerctl", "-a", "metadata", "--format", fmt],
                capture_output=True, text=True, timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        first = None
        for line in out.stdout.splitlines():
            f = line.split(SEP)
            if len(f) != 6 or not f[3].strip():
                continue
            try:
                length = int(f[5]) / 1e6
            except ValueError:
                length = 0.0
            info = (
                f[0], f[1].strip(), f[2].strip(),
                f[3].strip(), f[4].strip(), length,
            )
            if info[1] == "Playing":
                return info
            first = first or info
        return first

    @staticmethod
    def read_position(player):
        try:
            out = subprocess.run(
                ["playerctl", f"--player={player}", "position"],
                capture_output=True, text=True, timeout=2,
            )
            return float(out.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return None

    def poll(self):
        if not (SETTINGS["lyrics"] or SETTINGS["show_title"]):
            self.key = None
            self.data = ("idle", [], [])
            return

        info = self.now_playing()
        if info is None:
            self.track = None
            return

        player, status, artist, title, album, length = info
        self.track = (title, album, re.sub(r"\s*-\s*Topic$", "", artist).strip())
        if not SETTINGS["lyrics"]:
            self.key = None
            self.data = ("idle", [], [])
            return

        pos = self.read_position(player)
        if pos is not None:
            self.anchor_pos = pos
            self.anchor_t = time.monotonic()
        self.playing = status == "Playing"

        key = (artist, title, album, round(length))
        if key != self.key:
            self.key = key
            self.data = ("loading", [], [])
            text = load_lyrics(artist, title, album, length)
            parsed = parse_lrc(text) if text else []
            if parsed:
                self.data = ("ok", [t for t, _ in parsed], [l for _, l in parsed])
            else:
                self.data = ("none", [], [])

    def run(self):
        while True:
            try:
                self.poll()
            except Exception:
                pass
            time.sleep(0.5)


# -------------------------------------------------------------- text layout --
def char_width(c):
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def text_width(s):
    return sum(char_width(c) for c in s)


def fit(s, width):
    if text_width(s) <= width:
        return s
    out, w = "", 0
    for c in s:
        cw = char_width(c)
        if w + cw > width - 1:
            break
        out += c
        w += cw
    return out + "…"


def header_block(watcher, cols, accent):
    blank = "\x1b[K"
    track = watcher.track
    r, g, b = (int(c) for c in accent)
    bold = f"\x1b[1;38;2;{r};{g};{b}m"
    controls_style = "\x1b[1;38;2;220;220;235m"
    control_width = text_width(CONTROLS)
    available = max(1, cols - control_width - 4)
    metadata = ""

    if track:
        title, album, artist = track
        title = TITLE_NOISE.sub("", title).strip() or title
        album = re.sub(r"\s*-\s*(single|ep)$", "", album, flags=re.I).strip()
        if album.lower() == title.lower():
            album = ""
        rest = [x for x in (artist, album) if x]
        metadata = title
        if rest:
            metadata += " — " + " — ".join(rest)
        metadata = fit(metadata, available)

    left = " " + bold + metadata + "\x1b[0m"
    used = 1 + text_width(metadata)
    padding = max(1, cols - used - control_width)
    line = left + " " * padding + controls_style + CONTROLS + "\x1b[0m\x1b[K"
    return [line, blank]


def lyric_block(watcher, cols, accent):
    state, times, lines = watcher.data
    dim = "\x1b[38;2;110;110;125m"
    r, g, b = (int(c) for c in accent)
    bright = f"\x1b[1;38;2;{r};{g};{b}m"

    def center(text, style):
        if not text:
            return "\x1b[K"
        text = fit(text, max(1, cols - 2))
        return " " * ((cols - text_width(text)) // 2) + style + text + "\x1b[0m\x1b[K"

    if state == "ok":
        i = bisect.bisect_right(times, watcher.position() + SETTINGS["lyrics_offset"]) - 1
        prev = lines[i - 1] if i >= 1 else ""
        cur = lines[i] if i >= 0 else ""
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if i >= 0 and not cur:
            cur = "♪"
        return ["\x1b[K", center(prev, dim), center(cur, bright), center(nxt, dim)]

    msg = {"loading": "Searching for lyrics…", "none": "No synced lyrics found"}.get(state, "")
    return ["\x1b[K", "\x1b[K", center(msg, dim), "\x1b[K"]


# ---------------------------------------------------------- visualizations --
def color_escape(rgb, bold=False):
    r, g, b = np.clip(np.asarray(rgb), 0, 255).astype(int)
    prefix = "1;" if bold else ""
    return f"\x1b[{prefix}38;2;{r};{g};{b}m"


def put_cell(canvas, x, y, text):
    """Safely place a one-cell glyph on a character canvas."""
    h = len(canvas)
    w = len(canvas[0]) if h else 0
    if 0 <= x < w and 0 <= y < h:
        canvas[y][x] = text


def render_orbit(vals, colors, cols, rows, pulse):
    """Radial spectrum: frequency bars extend outward from a central ring."""
    canvas = [[" " for _ in range(cols)] for _ in range(rows)]
    if cols < 12 or rows < 6:
        return ["Terminal too small for Orbit".center(cols)[:cols]] + ["\x1b[K"] * max(0, rows - 1)

    cx, cy = (cols - 1) / 2, (rows - 1) / 2
    max_radius = max(2.0, min(cols * 0.34, rows * 0.38))
    inner = max(1.5, max_radius * 0.30)
    count = len(vals)
    flash_amount = pulse * SETTINGS["beat_flash"]

    # A soft center ring gives the radial bars a clear focal point.
    for k in range(96):
        angle = 2 * np.pi * k / 96
        x = int(round(cx + np.cos(angle) * inner * 2.0))
        y = int(round(cy + np.sin(angle) * inner))
        c = np.array([95, 105, 125], dtype=float) * (0.65 + flash_amount * 0.35)
        put_cell(canvas, x, y, color_escape(c) + "·\x1b[0m")

    for i, value in enumerate(vals):
        angle = (i / count) * 2 * np.pi - np.pi / 2
        length = max(0.0, float(value)) * (max_radius - inner)
        steps = max(1, int(length * 2.5))
        for step in range(steps):
            radius = inner + (step / max(1, steps - 1)) * length
            x = int(round(cx + np.cos(angle) * radius * 2.0))
            y = int(round(cy + np.sin(angle) * radius))
            rgb = colors[i] + (255 - colors[i]) * flash_amount
            glyph = "█" if step == steps - 1 else "▓"
            put_cell(canvas, x, y, color_escape(rgb, bold=True) + glyph + "\x1b[0m")

    center_color = np.mean(colors, axis=0) + (255 - np.mean(colors, axis=0)) * flash_amount
    put_cell(canvas, int(round(cx)), int(round(cy)), color_escape(center_color, True) + "●\x1b[0m")
    return ["".join(row) + "\x1b[K" for row in canvas]


def render_starfield(vals, colors, cols, rows, pulse, elapsed):
    """Album-art-colored stars arranged on a circular orbit, flashing to the beat."""
    canvas = [[" " for _ in range(cols)] for _ in range(rows)]
    if cols < 12 or rows < 6:
        return ["Terminal too small for Starfield".center(cols)[:cols]] + ["\x1b[K"] * max(0, rows - 1)

    cx, cy = (cols - 1) / 2, (rows - 1) / 2
    rx = max(3.0, min(cols * 0.36, rows * 0.72))
    ry = max(2.0, min(rows * 0.34, cols * 0.16))
    count = max(16, min(56, len(vals)))
    beat = pulse * SETTINGS["beat_flash"]

    # Circular dotted orbit behind the stars.
    for k in range(120):
        angle = 2 * np.pi * k / 120
        x = int(round(cx + np.cos(angle) * rx))
        y = int(round(cy + np.sin(angle) * ry))
        dim = np.array([105, 115, 135], dtype=float) * (0.45 + beat * 0.45)
        put_cell(canvas, x, y, color_escape(dim) + "·\x1b[0m")

    # Each star samples a different frequency band and brightens with that band.
    for i in range(count):
        band = int(i * len(vals) / count) % len(vals)
        value = float(vals[band])
        angle = (2 * np.pi * i / count) - np.pi / 2
        wobble = np.sin(elapsed * 2.2 + i * 0.8) * 0.04
        radius_x = rx * (1.0 + wobble + value * 0.08)
        radius_y = ry * (1.0 + wobble + value * 0.08)
        x = int(round(cx + np.cos(angle) * radius_x))
        y = int(round(cy + np.sin(angle) * radius_y))

        twinkle = 0.48 + 0.32 * (0.5 + 0.5 * np.sin(elapsed * (3.0 + value * 5) + i))
        brightness = min(1.0, twinkle + value * 0.28 + beat * 0.55)
        rgb = colors[band] * brightness + (255 - colors[band]) * (beat * 0.32)
        glyph = "★" if value > 0.55 or beat > 0.55 else "✦"
        put_cell(canvas, x, y, color_escape(rgb, bold=True) + glyph + "\x1b[0m")

        # A tiny halo gives the orbiting stars a circular/astral look.
        if value > 0.25:
            hx = int(round(cx + np.cos(angle) * (radius_x - 1.4)))
            hy = int(round(cy + np.sin(angle) * (radius_y - 0.7)))
            halo = colors[band] * min(0.55, 0.18 + value * 0.25 + beat * 0.18)
            put_cell(canvas, hx, hy, color_escape(halo) + "·\x1b[0m")

    # Small pulsing center makes the star ring feel connected to the kick drum.
    center_brightness = 0.35 + beat * 0.65
    center_rgb = np.mean(colors, axis=0) * center_brightness + 255 * beat * 0.25
    put_cell(canvas, int(round(cx)), int(round(cy)), color_escape(center_rgb, True) + "✧\x1b[0m")
    return ["".join(row) + "\x1b[K" for row in canvas]


class BeatChaser:
    """Autonomous maze runner that collects music notes on detected beat hits."""
    MAZE = (
        "###############",
        "#.....#.......#",
        "#.###.#.#####.#",
        "#...#.#.....#.#",
        "###.#.###.#.#.#",
        "#...#.....#...#",
        "#.#######.###.#",
        "#.............#",
        "###############",
    )

    def __init__(self):
        self.maze = [list(row) for row in self.MAZE]
        self.height = len(self.maze)
        self.width = len(self.maze[0])
        self.player = (1, 1)
        self.notes = {(5, 1), (9, 1), (13, 1), (2, 3), (7, 3), (11, 3),
                      (3, 5), (7, 5), (13, 5), (4, 7), (9, 7), (13, 7)}
        self.score = 0
        self.last_beat = 0.0
        self.beat_flash = 0.0
        self.steps = 0

    def open_cells(self):
        return [
            (x, y)
            for y in range(1, self.height - 1)
            for x in range(1, self.width - 1)
            if self.maze[y][x] != "#" and (x, y) != self.player and (x, y) not in self.notes
        ]

    def path_to_nearest_note(self):
        """Breadth-first search to the closest remaining note."""
        if not self.notes:
            return []
        queue = deque([(self.player, [])])
        visited = {self.player}
        while queue:
            pos, path = queue.popleft()
            if pos in self.notes:
                return path
            x, y = pos
            for nxt in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                nx, ny = nxt
                if (0 <= ny < self.height and 0 <= nx < self.width
                        and self.maze[ny][nx] != "#" and nxt not in visited):
                    visited.add(nxt)
                    queue.append((nxt, path + [nxt]))
        return []

    def update(self, pulse, elapsed):
        # The FFT bass transient acts as the game's beat clock.
        if pulse > 0.48 and elapsed - self.last_beat > 0.20:
            self.last_beat = elapsed
            self.beat_flash = 1.0
            path = self.path_to_nearest_note()
            if path:
                self.player = path[0]
                self.steps += 1
                if self.player in self.notes:
                    self.notes.remove(self.player)
                    self.score += 1
            # Keep the game alive after the first group of notes is collected.
            if len(self.notes) < 5:
                candidates = self.open_cells()
                if candidates:
                    self.notes.add(random.choice(candidates))
        self.beat_flash = max(0.0, self.beat_flash - 0.055)

    def render(self, cols, rows, colors):
        canvas = [[" " for _ in range(cols)] for _ in range(rows)]
        maze_x = max(0, (cols - self.width) // 2)
        maze_y = max(0, (rows - self.height - 2) // 2 + 1)
        main_color = np.mean(colors, axis=0)
        wall_color = main_color * (0.62 + self.beat_flash * 0.38)
        note_color = np.array([255, 255, 255]) * (0.68 + self.beat_flash * 0.32)
        for y, row in enumerate(self.maze):
            for x, cell in enumerate(row):
                sx, sy = maze_x + x, maze_y + y
                if not (0 <= sx < cols and 0 <= sy < rows):
                    continue
                if cell == "#":
                    glyph = "█"
                    style = color_escape(wall_color, True)
                elif (x, y) in self.notes:
                    glyph = "♪"
                    style = color_escape(note_color, True)
                else:
                    glyph = " "
                    style = ""
                canvas[sy][sx] = style + glyph + ("\x1b[0m" if style else "")

        px, py = self.player
        sx, sy = maze_x + px, maze_y + py
        if 0 <= sx < cols and 0 <= sy < rows:
            player_color = np.array([255, 255, 255]) * (0.72 + self.beat_flash * 0.28)
            canvas[sy][sx] = color_escape(player_color, True) + "◉\x1b[0m"

        title = f"BEAT CHASER   NOTES {self.score}   MOVES {self.steps}"
        title_y = min(rows - 1, max(0, maze_y - 1))
        title_x = max(0, (cols - len(title)) // 2)
        for i, char in enumerate(title[:max(0, cols - title_x)]):
            if 0 <= title_y < rows:
                canvas[title_y][title_x + i] = color_escape(main_color, True) + char + "\x1b[0m"

        if rows < self.height + 3 or cols < self.width:
            return ["Terminal too small for Beat Chaser".center(cols)[:cols]] + ["\x1b[K"] * max(0, rows - 1)
        return ["".join(line) + "\x1b[K" for line in canvas]


def render_bars(vals, colors, rows, pulse):
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
        lines.append("".join(parts) + "\x1b[0m\x1b[K")
    return lines


def render(vals, colors, rows, pulse, status="", extra=None, header=None,
           mode="Bars", cols=80, elapsed=0.0, game=None):
    """Compose the selected visualization with the shared header and lyrics."""
    lines = list(header) if header else []
    if mode == "Orbit":
        art = render_orbit(vals, colors, cols, rows, pulse)
    elif mode == "Starfield":
        art = render_starfield(vals, colors, cols, rows, pulse, elapsed)
    elif mode == "Beat Chaser" and game is not None:
        game.update(pulse, elapsed)
        art = game.render(cols, rows, colors)
    else:
        art = render_bars(vals, colors, rows, pulse)
    lines.extend(art)
    if extra:
        lines.extend(extra)
    if status:
        lines.append(status + "\x1b[0m\x1b[K")
    return "\x1b[H" + "\n".join(lines) + "\x1b[J"


def status_line(watcher):
    sw = "".join(f"\x1b[48;2;{r};{g};{b}m    " for r, g, b in watcher.palette)
    return f"{sw}\x1b[0m {str(watcher.last_url)[-60:]}"


# ------------------------------------------------------------------- main --
def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit(
            "Artcava needs a real terminal (not an IDE console). "
            "Open a terminal and run: python3 Artcava.py"
        )

    load_settings()
    original_opacity = kitty_configured_opacity()
    in_kitty = kitty_available()

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv
    forced = args[0] if args else None

    watcher = ArtWatcher(forced)
    watcher.start()
    lyrics = LyricsWatcher()
    lyrics.start()

    try:
        reader = AudioReader()
    except FileNotFoundError:
        sys.exit("parec not found - install pulseaudio-utils (works with PipeWire too).")
    except OSError as exc:
        sys.exit(f"Could not start audio capture: {exc}")
    reader.start()

    game = BeatChaser()
    size = None
    analyzer = None
    old_tty = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())

    current = None
    last = time.perf_counter()
    next_t = last
    start_time = time.monotonic()
    quit_requested = False

    if in_kitty:
        kitty_opacity(SETTINGS["opacity"])

    sys.stdout.write(
        "\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[2J"
    )
    sys.stdout.flush()

    try:
        while not quit_requested:
            now = time.perf_counter()
            dt = min(0.05, max(1e-3, now - last))
            last = now

            for key in read_keys(0):
                cols, _ = shutil.get_terminal_size()
                action = mouse_action(key, cols)
                if action:
                    player_control(action)
                    continue
                if key in ("a", "A"):
                    player_control("previous")
                    continue
                if key in ("d", "D"):
                    player_control("next")
                    continue
                if key == " ":
                    player_control("play-pause")
                    continue
                if key in ("s", "S"):
                    settings_menu()
                    size = None
                    next_t = last = time.perf_counter()
                    break
                if key in ("q", "Q"):
                    quit_requested = True
                    break
                if key in ("l", "L"):
                    SETTINGS["lyrics"] = not SETTINGS["lyrics"]
                    save_settings()
                    sys.stdout.write("\x1b[2J")
                elif key in ("t", "T"):
                    SETTINGS["show_title"] = not SETTINGS["show_title"]
                    save_settings()
                    sys.stdout.write("\x1b[2J")
                elif key in ("[", "]"):
                    step = -0.25 if key == "[" else 0.25
                    SETTINGS["lyrics_offset"] = round(
                        max(-5.0, min(5.0, SETTINGS["lyrics_offset"] + step)), 2
                    )
                    save_settings()

            if quit_requested:
                break

            cols, lines = shutil.get_terminal_size()
            if (cols, lines) != size:
                size = (cols, lines)
                nbars = max(
                    1,
                    (cols + SETTINGS["gap"]) // (SETTINGS["bar_width"] + SETTINGS["gap"]),
                )
                analyzer = Analyzer(nbars)
                current = gradient(active_palette(watcher.palette), nbars)
                sys.stdout.write("\x1b[2J")

            target = gradient(active_palette(watcher.palette), analyzer.nbars)
            color_ease = 1.0 - np.exp(-dt / COLOR_EASE_TIME)
            current += (target - current) * color_ease
            vals = analyzer.update(reader.snapshot(), dt)

            roomy = lines >= 12
            show_lyrics = SETTINGS["lyrics"] and roomy
            show_title = SETTINGS["show_title"] and roomy
            rows = max(
                1,
                lines - 1
                - (LYRIC_ROWS if show_lyrics else 0)
                - (HEADER_ROWS if show_title else 0)
                - (1 if debug else 0),
            )

            mid = current[len(current) // 2]
            accent = mid + (255 - mid) * 0.35
            header = header_block(lyrics, cols, accent) if show_title else None
            extra = lyric_block(lyrics, cols, accent) if show_lyrics else None
            elapsed = time.monotonic() - start_time

            sys.stdout.write(
                render(
                    vals, current, rows, analyzer.pulse,
                    status_line(watcher) if debug else "",
                    extra, header,
                    mode=SETTINGS["visualization"],
                    cols=cols, elapsed=elapsed, game=game,
                )
            )
            sys.stdout.flush()

            next_t += 1 / FPS
            spare = next_t - time.perf_counter()
            if spare > 0:
                time.sleep(spare)
            else:
                next_t = time.perf_counter()

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        try:
            reader.proc.terminate()
        except Exception:
            pass
        if in_kitty:
            restore_kitty_opacity(original_opacity)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        sys.stdout.write(
            "\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()

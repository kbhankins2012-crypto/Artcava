#!/usr/bin/env python3
"""
Artcava - a terminal music visualizer with album-art colors, synced lyrics,
and interactive playback controls.

Audio : captured from your default output's monitor via `parec`
Art   : read from MPRIS via `playerctl`

Deps  : python3-numpy, python3-pil, playerctl, pulseaudio-utils
        (Audio DSP additionally needs pactl/parec/paplay from
         pulseaudio-utils or PipeWire's pipewire-pulse; no extra Python deps)

Keys  : S = settings   Q = quit   L = lyrics on/off   T = song title on/off
        A = previous track   Space = pause/resume   D = next track
        [ ] = nudge lyric timing
        Mouse drag = move decorations (configure slots in Settings)
        Water reflection = Settings > Reflection (Bars visualization)
        Click or drag the progress bar = jump to any point in the song
        Lyric animation and lyric font = Settings
        Audio DSP = Settings > Audio (master volume, bass, mid, treble, 5-band EQ).
                    All audio controls live only in Settings; the main
                    visualizer screen is unchanged.

Audio DSP (Settings > Audio):
    When enabled, Artcava creates a temporary virtual sink named
    "artcava_dsp" (PulseAudio module-null-sink / PipeWire equivalent),
    moves existing playback streams to it, makes it the default sink so
    new streams also route through it, and runs a processing pipeline:

        parec (artcava_dsp.monitor) -> Artcava --dsp-worker -> paplay (original sink)

    The worker applies master volume, bass/mid/treble and a 5-band EQ
    in the frequency domain (numpy FFT, stereo, soft limiter to prevent
    clipping) and plays the result to your original output device.
    The visualizer then taps the original sink's monitor, so it shows
    the *processed* audio. On disable or exit, streams are moved back,
    the original default sink is restored, and the virtual sink is
    unloaded. No permanent system configuration is changed.
    If pactl/parec/paplay are missing or the virtual sink cannot be
    created, DSP stays off and the visualizer keeps working normally.

Kitty transparency:
    Add these to ~/.config/kitty/kitty.conf:
        allow_remote_control yes
        dynamic_background_opacity yes

    Opacity is changed live using Kitty's remote-control interface.
"""
import base64
import bisect
import colorsys
import hashlib
import io
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import tempfile
import threading
import time
import tty
import unicodedata
import urllib.parse
import urllib.request
import webbrowser

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

# Audio DSP constants. The DSP worker runs at 48 kHz stereo regardless of
# the visualizer's 44.1 kHz mono tap; PulseAudio/PipeWire resamples as needed.
AUDIO_DSP_RATE = 48000
AUDIO_DSP_CHANNELS = 2
AUDIO_DSP_FFT_SIZE = 4096
AUDIO_DSP_HOP = 2048
AUDIO_DSP_SINK_NAME = "artcava_dsp"
AUDIO_EQ_BANDS = [
    ("audio_eq_60", 60.0, "EQ 60 Hz"),
    ("audio_eq_230", 230.0, "EQ 230 Hz"),
    ("audio_eq_910", 910.0, "EQ 910 Hz"),
    ("audio_eq_3640", 3640.0, "EQ 3.6 kHz"),
    ("audio_eq_14000", 14000.0, "EQ 14 kHz"),
]

VISUALIZATIONS = ("Bars", "Mirror")
LYRIC_ANIMATIONS = ("Off", "Fade", "Slide", "Karaoke", "Lively")
LYRIC_ANIMATION_HELP = {
    "Off": "Lyric lines change instantly.",
    "Fade": "Lines cross-fade smoothly when the song moves to the next one.",
    "Slide": "Fade, plus the new line glides in from the side.",
    "Karaoke": "Fade, plus the current line fills in as it is sung.",
    "Lively": "Everything: fade, slide, karaoke fill, a glow on the beat and a gentle shimmer.",
}
LYRIC_FONTS = ("Normal", "Bold", "Italic", "Script", "Gothic", "Double-struck",
               "Monospace", "Sans bold", "Small caps", "Wide")


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
    # Soft periwinkle, lavender, blush pink, and misty aqua.
    "Dreamy": [(120, 105, 220), (175, 150, 245), (245, 175, 220), (145, 210, 235)],
}

DEFAULTS = {
    "fps": FPS,
    "color_mode": "Album art",
    "visualization": "Bars",
    "bar_width": BAR_W,
    "gap": GAP,
    "sensitivity": 1.0,
    "release_time": RELEASE_TIME,
    "beat_flash": BEAT_FLASH,
    "lyrics": True,
    # User-selected global sync adjustment. Persist across tracks and launches.
    "lyrics_offset": 0.0,
    "show_title": True,
    "opacity": 1.0,
    # Water reflection under the Bars visualization (off unless enabled).
    "reflection": False,
    "reflection_strength": 0.55,
    "reflection_length": 0.50,
    "water_distortion": 0.35,
    # How the synced lyrics are animated and styled.
    "lyric_animation": "Lively",
    "lyric_font": "Normal",
    # Audio DSP (Settings > Audio). Off by default so existing users are
    # not surprised by routing changes after updating. Values persist in
    # settings.json; missing keys (old settings files) get these defaults.
    "audio_enabled": False,
    "audio_preset": "Custom",
    "audio_presets": {},
    "audio_volume": 100,
    "audio_bass": 0.0,
    "audio_mid": 0.0,
    "audio_treble": 0.0,
    "audio_eq_60": 0.0,
    "audio_eq_230": 0.0,
    "audio_eq_910": 0.0,
    "audio_eq_3640": 0.0,
    "audio_eq_14000": 0.0,
}

DEFAULT_DECORATIONS = [
    {"path": "", "x": 2, "y": 5, "width": 12, "height": 8},
    {"path": "", "x": 18, "y": 5, "width": 12, "height": 8},
    {"path": "", "x": 34, "y": 5, "width": 12, "height": 8},
    {"path": "", "x": 50, "y": 5, "width": 12, "height": 8},
    {"path": "", "x": 66, "y": 5, "width": 12, "height": 8},
]

SETTINGS = dict(DEFAULTS)
SETTINGS["audio_presets"] = {}
SETTINGS["decorations"] = [dict(item) for item in DEFAULT_DECORATIONS]

LIMITS = [
    ("fps", 1, 120, int),
    ("bar_width", 1, 4, int),
    ("gap", 0, 4, int),
    ("sensitivity", 0.5, 2.5, float),
    ("release_time", 0.06, 0.5, float),
    ("beat_flash", 0.0, 1.0, float),
    ("lyrics_offset", -5.0, 5.0, float),
    ("opacity", 0.10, 1.0, float),
    ("reflection_strength", 0.10, 1.0, float),
    ("reflection_length", 0.20, 1.0, float),
    ("water_distortion", 0.0, 1.0, float),
    ("audio_volume", 0, 150, int),
    ("audio_bass", 0.0, 12.0, float),
    ("audio_mid", -12.0, 12.0, float),
    ("audio_treble", -12.0, 12.0, float),
    ("audio_eq_60", -12.0, 12.0, float),
    ("audio_eq_230", -12.0, 12.0, float),
    ("audio_eq_910", -12.0, 12.0, float),
    ("audio_eq_3640", -12.0, 12.0, float),
    ("audio_eq_14000", -12.0, 12.0, float),
]


# Presets intentionally do not include the processing on/off switch: selecting
# a sound must never reroute audio or unexpectedly enable processing.
AUDIO_PRESET_KEYS = ("audio_volume", "audio_bass", "audio_mid", "audio_treble",
                     "audio_eq_60", "audio_eq_230", "audio_eq_910",
                     "audio_eq_3640", "audio_eq_14000")
AUDIO_PRESET_MAX = 50
AUDIO_PRESET_NAME_MAX = 24


def normalize_preset(values):
    """Validate a saved sound using the same limits as the Audio sliders."""
    if not isinstance(values, dict):
        return None
    result = {}
    for key, lo, hi, cast in LIMITS:
        if key not in AUDIO_PRESET_KEYS:
            continue
        try:
            value = cast(values.get(key, DEFAULTS[key]))
            if not math.isfinite(value):
                raise ValueError
            result[key] = max(lo, min(hi, value))
        except (TypeError, ValueError, OverflowError):
            result[key] = DEFAULTS[key]
    return result


def valid_preset_name(name):
    return (isinstance(name, str) and name == name.strip() and 0 < len(name) <= AUDIO_PRESET_NAME_MAX
            and all(ch.isprintable() for ch in name)
            and name.casefold() not in ("flat", "custom"))


def current_audio_values():
    return {key: SETTINGS[key] for key in AUDIO_PRESET_KEYS}


def preset_values(name):
    if name == "Flat":
        return {key: DEFAULTS[key] for key in AUDIO_PRESET_KEYS}
    return SETTINGS["audio_presets"].get(name)


def preset_label():
    name = SETTINGS.get("audio_preset", "Custom")
    values = preset_values(name)
    return name + (" *" if values is not None and values != current_audio_values() else "")


def apply_audio_preset(name):
    values = preset_values(name)
    if values is None:
        return False
    SETTINGS.update(values)
    SETTINGS["audio_preset"] = name
    save_settings()
    return True


def cycle_audio_preset(direction):
    names = ["Flat"] + list(SETTINGS["audio_presets"])
    current = SETTINGS.get("audio_preset", "Custom")
    index = names.index(current) if current in names else (-1 if direction > 0 else 0)
    name = names[(index + direction) % len(names)]
    apply_audio_preset(name)
    return name


def save_audio_preset(name, overwrite=False):
    if not valid_preset_name(name):
        return False, "Choose a name (1-24 characters); Flat and Custom are reserved."
    presets = SETTINGS["audio_presets"]
    existing = next((n for n in presets if n.casefold() == name.casefold()), None)
    if existing and not overwrite:
        return False, "Name already exists. Choose another or overwrite the selected preset."
    if not existing and len(presets) >= AUDIO_PRESET_MAX:
        return False, "Preset limit reached; delete one first."
    name = existing or name
    presets[name] = current_audio_values()
    SETTINGS["audio_preset"] = name
    save_settings()
    return True, f"Saved preset: {name}"


def delete_audio_preset(name):
    if name not in SETTINGS["audio_presets"]:
        return False
    del SETTINGS["audio_presets"][name]
    if SETTINGS["audio_preset"] == name:
        SETTINGS["audio_preset"] = "Custom"
    save_settings()
    return True


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
        SETTINGS[key] = saved.get(key, DEFAULTS[key])

    if not isinstance(SETTINGS["color_mode"], str) or SETTINGS["color_mode"] not in COLOR_PRESETS:
        SETTINGS["color_mode"] = DEFAULTS["color_mode"]

    # Older settings files, including files with removed experimental modes,
    # safely fall back to the classic Bars visualization.
    if SETTINGS.get("visualization") not in VISUALIZATIONS:
        SETTINGS["visualization"] = "Bars"

    if SETTINGS.get("lyric_animation") not in LYRIC_ANIMATIONS:
        SETTINGS["lyric_animation"] = DEFAULTS["lyric_animation"]
    if SETTINGS.get("lyric_font") not in LYRIC_FONTS:
        SETTINGS["lyric_font"] = DEFAULTS["lyric_font"]

    for flag in ("lyrics", "show_title", "reflection", "audio_enabled"):
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

    raw_presets = SETTINGS.get("audio_presets")
    presets = {}
    seen_names = set()
    if isinstance(raw_presets, dict):
        for name, raw in raw_presets.items():
            if len(presets) >= AUDIO_PRESET_MAX:
                break
            if not valid_preset_name(name) or name.casefold() in seen_names:
                continue
            normalized_preset = normalize_preset(raw)
            if normalized_preset is not None:
                presets[name] = normalized_preset
                seen_names.add(name.casefold())
    SETTINGS["audio_presets"] = presets
    if SETTINGS.get("audio_preset") not in ("Flat", "Custom", *presets):
        SETTINGS["audio_preset"] = "Custom"

    # Decoration paths and dimensions are loaded independently, so a damaged
    # slot in settings.json cannot stop the rest of Artcava from launching.
    raw_decorations = saved.get("decorations", DEFAULT_DECORATIONS)
    if not isinstance(raw_decorations, list):
        raw_decorations = []
    normalized = []
    for index, original in enumerate(DEFAULT_DECORATIONS):
        item = dict(original)
        try:
            saved_item = raw_decorations[index]
            if isinstance(saved_item, dict):
                item["path"] = str(saved_item.get("path", ""))[:2048]
                item["x"] = max(1, min(500, int(saved_item.get("x", item["x"]))))
                item["y"] = max(1, min(300, int(saved_item.get("y", item["y"]))))
                item["width"] = max(2, min(80, int(saved_item.get("width", item["width"]))))
                item["height"] = max(2, min(50, int(saved_item.get("height", item["height"]))))
        except (IndexError, TypeError, ValueError, OverflowError):
            pass
        normalized.append(item)
    SETTINGS["decorations"] = normalized


def save_settings():
    # Atomic replacement prevents the DSP process from reading a partial JSON file.
    temp_path = None
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CONFIG_DIR,
                                         prefix=".settings-", delete=False) as f:
            temp_path = f.name
            json.dump(SETTINGS, f, indent=2)
        os.replace(temp_path, CONFIG_FILE)
        temp_path = None
    except OSError:
        pass
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
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


# ------------------------------------------------------------- decorations --
def choose_decoration_file():
    """Open a Linux file picker and return an image path, or an empty string."""
    file_filter = "Images and GIFs | *.png *.jpg *.jpeg *.webp *.bmp *.gif"
    commands = [
        ["zenity", "--file-selection", "--title=Choose an Artcava decoration",
         "--file-filter=" + file_filter],
        ["kdialog", "--getopenfilename", os.path.expanduser("~"),
         "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"],
    ]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and result.stdout.strip():
                return os.path.abspath(os.path.expanduser(result.stdout.strip()))
            return ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return ""


class DecorationRenderer:
    """Display five still images or GIFs using Kitty's graphics protocol."""

    def __init__(self):
        self.enabled = kitty_available()
        # Process-specific IDs reduce the chance of colliding with another app
        # that is also using Kitty graphics in the same terminal window.
        base = (os.getpid() % 1000000) * 10 + 100
        self.image_ids = [base + i for i in range(5)]
        self.placement_ids = [base + 10 + i for i in range(5)]
        self.images = [None] * 5
        self.durations = [[] for _ in range(5)]
        self.total_durations = [100] * 5
        self.started = [time.monotonic()] * 5
        self.uploaded = [None] * 5
        self.dragging = None
        self.drag_offset = (0, 0)
        self.drag_dirty = False
        self.reload()

    @staticmethod
    def _send(control, payload=""):
        sys.stdout.write("\x1b_G" + control + (";" + payload if payload else "") + "\x1b\\")

    def reload_slot(self, index):
        old = self.images[index]
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self.images[index] = None
        self.durations[index] = []
        self.total_durations[index] = 100
        self.uploaded[index] = None
        self.started[index] = time.monotonic()
        path = SETTINGS["decorations"][index].get("path", "")
        if not path or not os.path.isfile(path):
            return
        try:
            image = Image.open(path)
            durations = []
            frame_count = getattr(image, "n_frames", 1)
            for frame_index in range(frame_count):
                image.seek(frame_index)
                durations.append(max(20, int(image.info.get("duration", 100) or 100)))
            self.images[index] = image
            self.durations[index] = durations or [100]
            self.total_durations[index] = sum(self.durations[index])
        except Exception:
            try:
                image.close()
            except Exception:
                pass
            self.images[index] = None

    def reload(self):
        for index in range(5):
            self.reload_slot(index)

    def _frame_index(self, index, now):
        durations = self.durations[index]
        if len(durations) <= 1:
            return 0
        elapsed = int((now - self.started[index]) * 1000) % self.total_durations[index]
        for frame_index, duration in enumerate(durations):
            if elapsed < duration:
                return frame_index
            elapsed -= duration
        return 0

    def _upload(self, index, frame):
        """Transmit PNG bytes in protocol-sized chunks without placing them."""
        image_id = self.image_ids[index]
        # Replacing the pixels for a known ID requires deleting its old data.
        self._send(f"a=d,d=I,i={image_id},q=2")
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        chunks = [encoded[offset:offset + 4096] for offset in range(0, len(encoded), 4096)]
        for chunk_index, chunk in enumerate(chunks):
            more = int(chunk_index < len(chunks) - 1)
            if chunk_index == 0:
                self._send(
                    f"a=t,f=100,t=d,i={image_id},q=2,m={more}", chunk
                )
            else:
                self._send(f"m={more},q=2", chunk)

    def mouse(self, key, cols, lines):
        """Handle clicks and held-button motion over a decoration."""
        match = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", key)
        if not match:
            return False
        button, x, y, event = match.groups()
        button, x, y = int(button), int(x), int(y)

        if event == "m":
            was_dragging = self.dragging is not None
            self.dragging = None
            if self.drag_dirty:
                save_settings()
                self.drag_dirty = False
            return was_dragging

        if button == 0 and event == "M":
            for index, item in enumerate(SETTINGS["decorations"]):
                if self.images[index] is None:
                    continue
                if (item["x"] <= x < item["x"] + item["width"] and
                        item["y"] <= y < item["y"] + item["height"]):
                    self.dragging = index
                    self.drag_offset = (x - item["x"], y - item["y"])
                    self.drag_dirty = False
                    return True
            return False

        # SGR button 32 is motion while the left mouse button is held.
        if button == 32 and self.dragging is not None:
            item = SETTINGS["decorations"][self.dragging]
            new_x = max(1, min(max(1, cols - item["width"] + 1), x - self.drag_offset[0]))
            new_y = max(1, min(max(1, lines - item["height"] + 1), y - self.drag_offset[1]))
            if (new_x, new_y) != (item["x"], item["y"]):
                item["x"], item["y"] = new_x, new_y
                self.drag_dirty = True
            return True
        return False

    def draw(self, cols, lines):
        if not self.enabled:
            return
        now = time.monotonic()
        sys.stdout.write("\x1b7")  # Save cursor while placing graphics.
        for index, item in enumerate(SETTINGS["decorations"]):
            image = self.images[index]
            if image is None:
                continue
            width = min(item["width"], cols - item["x"] + 1)
            height = min(item["height"], lines - item["y"] + 1)
            if width < 2 or height < 2:
                continue
            try:
                frame_index = self._frame_index(index, now)
                key = (frame_index, width, height)
                if self.uploaded[index] != key:
                    image.seek(frame_index)
                    resampling = getattr(Image, "Resampling", Image).LANCZOS
                    frame = image.convert("RGBA").resize(
                        (width * 9, height * 18), resampling
                    )
                    self._upload(index, frame)
                    self.uploaded[index] = key

                item_x, item_y = item["x"], item["y"]
                sys.stdout.write(f"\x1b[{item_y};{item_x}H")
                self._send(
                    f"a=p,i={self.image_ids[index]},p={self.placement_ids[index]},"
                    f"c={width},r={height},q=2"
                )
            except Exception:
                continue
        sys.stdout.write("\x1b8")  # Restore cursor position.

    def invalidate(self):
        """Force image data to be retransmitted after a full-screen clear."""
        self.uploaded = [None] * 5

    def clear(self):
        if self.enabled:
            for image_id in self.image_ids:
                self._send(f"a=d,d=I,i={image_id},q=2")


def decoration_menu(slot_index, renderer):
    """Edit one decoration's image and dimensions."""
    selected = 0
    message = ""
    rows = ["Choose / replace image", "Width", "Height", "Remove image", "Back"]
    while True:
        item = SETTINGS["decorations"][slot_index]
        filename = os.path.basename(item["path"]) if item["path"] else "Not set"
        if len(filename) > 30:
            filename = filename[:27] + "..."
        values = [filename, f"{item['width']} cells", f"{item['height']} cells", "Press Enter", "Press Enter"]
        out = [
            "\x1b[2J\x1b[H\x1b[1;36m A R T C A V A / DECORATION " + str(slot_index + 1) + "\x1b[0m\n\n",
            " Choose an image or GIF, then drag it on the main screen to move it.\n\n",
        ]
        for row_index, label in enumerate(rows):
            marker = "\x1b[1;36m ❯ " if row_index == selected else "   "
            end = "\x1b[0m" if row_index == selected else ""
            out.append(f"{marker}{label:<24} {values[row_index]:>30}{end}\n")
        if message:
            out.append("\n " + message + "\n")
        if not renderer.enabled:
            out.append("\n Images display only in Kitty terminal.\n")
        out.append("\n ↑/↓ select   ←/→ resize   Enter choose   Esc back\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        for key in read_keys():
            if key in UP:
                selected = (selected - 1) % len(rows)
                message = ""
            elif key in DOWN:
                selected = (selected + 1) % len(rows)
                message = ""
            elif selected in (1, 2) and (key in RIGHT or key in LEFT or key in ("+", "-")):
                direction = -1 if key in LEFT or key == "-" else 1
                dimension = "width" if selected == 1 else "height"
                limit = (2, 80) if dimension == "width" else (2, 50)
                item[dimension] = max(limit[0], min(limit[1], item[dimension] + direction))
                save_settings()
                message = f"Decoration {dimension} updated."
            elif key in ("\r", "\n", "+", "="):
                if selected == 0:
                    path = choose_decoration_file()
                    if path:
                        try:
                            with Image.open(path) as test_image:
                                test_image.verify()
                            item["path"] = path
                            renderer.reload_slot(slot_index)
                            save_settings()
                            message = "Image loaded. Drag it on the main screen to position it."
                        except Exception as exc:
                            message = f"Couldn't load that image: {exc}"
                    else:
                        message = "File picker cancelled or unavailable. Install zenity or kdialog."
                elif selected == 1:
                    item["width"] = min(80, item["width"] + 1)
                    save_settings()
                elif selected == 2:
                    item["height"] = min(50, item["height"] + 1)
                    save_settings()
                elif selected == 3:
                    item["path"] = ""
                    renderer.reload_slot(slot_index)
                    save_settings()
                    message = "Decoration removed."
                else:
                    save_settings()
                    return
            elif key in ("\x1b", "s", "S", "q", "Q"):
                save_settings()
                return


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


_pending = ""
PARTIAL = re.compile(r"\x1b\[[0-9;<]*$")


def read_keys(timeout=None):
    """Read key and mouse input, keeping any half-received escape sequence for the next call."""
    global _pending
    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return []

    data = os.read(fd, 4096)
    if not data:
        raise EOFError
    text = _pending + data.decode("utf-8", "ignore")
    _pending = ""
    if text.endswith("\x1b") and select.select([fd], [], [], 0.02)[0]:
        # Ends on a lone ESC: either the Esc key or the first byte of a mouse report.
        # Give the rest of a report a moment to arrive; a real Esc press waits ~20 ms.
        more = os.read(fd, 4096)
        text += more.decode("utf-8", "ignore")
    match = PARTIAL.search(text)
    if match:                                # report was cut off: keep the rest
        _pending, text = text[match.start():], text[:match.start()]
    return KEY_RE.findall(text)


# --------------------------------------------------------------- updates --
UPDATE_RAW_URL = (
    "https://raw.githubusercontent.com/"
    "kbhankins2012-crypto/Artcava/main/Artcava.py"
)
UPDATE_PAGE_URL = (
    "https://github.com/kbhankins2012-crypto/Artcava/blob/main/README.md"
)
UPDATE_USER_AGENT = "Artcava-Updater/1.0"


class UpdateChecker(threading.Thread):
    """Compare this installed script with Artcava.py on GitHub's main branch."""

    def __init__(self, local_path):
        super().__init__(daemon=True)
        self.local_path = os.path.realpath(local_path)
        self.status = "checking"
        self.available = False
        self.remote_hash = ""
        self.error = ""
        self._state_lock = threading.Lock()
        self._check_lock = threading.Lock()

    def snapshot(self):
        with self._state_lock:
            return self.status, self.available, self.remote_hash, self.error

    def check_now(self):
        """Fetch a fresh GitHub copy and verify mismatches before notifying."""
        with self._check_lock:
            try:
                def fetch_remote_source():
                    # A unique query string helps avoid stale CDN/proxy cache entries.
                    fresh_url = f"{UPDATE_RAW_URL}?artcava_check={time.time_ns()}"
                    request = urllib.request.Request(
                        fresh_url,
                        headers={
                            "User-Agent": UPDATE_USER_AGENT,
                            "Cache-Control": "no-cache, no-store, max-age=0",
                            "Pragma": "no-cache",
                        },
                    )
                    with urllib.request.urlopen(request, timeout=6) as response:
                        source = response.read(2 * 1024 * 1024 + 1)

                    if len(source) > 2 * 1024 * 1024:
                        raise ValueError(
                            "The GitHub source file is unexpectedly large"
                        )
                    return source

                remote_source = fetch_remote_source()

                with open(self.local_path, "rb") as source_file:
                    local_source = source_file.read()

                local_hash = hashlib.sha256(local_source).hexdigest()
                remote_hash = hashlib.sha256(remote_source).hexdigest()
                available = remote_hash != local_hash

                # If a mismatch appears, fetch once more before showing a notice.
                # The second fresh response is the one used for the final decision.
                if available:
                    time.sleep(0.3)
                    verified_source = fetch_remote_source()
                    remote_hash = hashlib.sha256(verified_source).hexdigest()
                    available = remote_hash != local_hash

                with self._state_lock:
                    self.remote_hash = remote_hash
                    self.available = available
                    self.status = "available" if available else "up_to_date"
                    self.error = ""
            except Exception as exc:
                with self._state_lock:
                    self.available = False
                    self.status = "error"
                    self.error = str(exc)

            return self.snapshot()[0]

    def run(self):
        self.check_now()


def copy_to_clipboard(text):
    """Copy text using whichever common Linux clipboard tool is installed."""
    commands = (
        # Kitty's clipboard kitten works without a separate clipboard utility.
        ["kitten", "clipboard"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    )
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def open_update_page():
    """Copy the GitHub source link and open it in the default browser."""
    copied = copy_to_clipboard(UPDATE_PAGE_URL)
    try:
        opened = webbrowser.open(UPDATE_PAGE_URL, new=2)
    except Exception:
        opened = False

    if copied and opened:
        return "GitHub opened and the link was copied to your clipboard."
    if copied:
        return "Link copied. Open it in your browser to view the update."
    if opened:
        return "GitHub opened. Clipboard tools (wl-copy, xclip, or xsel) were not found."
    return "Could not open or copy automatically. Link: " + UPDATE_PAGE_URL


def update_notice_layout(cols, lines):
    """Return the terminal position and text for the small update banner."""
    title = "Artcava update available"
    actions = "[U] Update now (copy + open)   [X] Update later"
    width = max(len(title), len(actions)) + 4
    if cols < width + 2 or lines < 6:
        return None
    row = lines - 3
    col = cols - width + 1
    top = "┌" + "─" * (width - 2) + "┐"
    middle = "│ " + actions.ljust(width - 4) + " │"
    bottom = "└" + "─" * (width - 2) + "┘"
    return row, col, width, actions, (top, middle, bottom)


def update_notice_overlay(cols, lines):
    """Draw a right-aligned, three-line update notice in the terminal corner."""
    layout = update_notice_layout(cols, lines)
    if not layout:
        return ""
    row, col, _width, _actions, panel = layout
    style = "\x1b[1;38;2;105;190;255m"
    body = "\x1b[38;2;225;230;240m"
    bg = "\x1b[48;2;24;27;38m"
    reset = "\x1b[0m"
    return "".join(
        f"\x1b[{row + i};{col}H{bg}{style if i != 1 else body}{line}{reset}"
        for i, line in enumerate(panel)
    )


def update_notice_mouse_action(key, cols, lines):
    """Recognize clicks on Update now or Update later in the update banner."""
    match = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", key)
    if not match:
        return None
    button, x, y, event = match.groups()
    if int(button) != 0 or event != "M":
        return None

    layout = update_notice_layout(cols, lines)
    if not layout:
        return None
    row, col, _width, actions, _panel = layout
    if int(y) != row + 1:
        return None

    inner_x = int(x) - (col + 2)
    later_at = actions.index("[X]")
    if 0 <= inner_x < later_at:
        return "update"
    if later_at <= inner_x < len(actions):
        return "later"
    return None


# --------------------------------------------------------------- menu --
# How the settings screen is drawn. Pure drawing code: what each row does lives in settings_menu().
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RESET = "\x1b[0m"
_TEXT, _BRIGHT, _MUTED, _GREEN, _TRACK = (200, 205, 218), (236, 239, 248), (112, 116, 135), (110, 225, 150), (70, 74, 92)

# Rows are grouped into sections; each entry is the index of the first row in that section.
# Audio rows live only here in Settings; the main visualizer screen is unchanged.
SETTINGS_SECTIONS = {0: "LOOK", 7: "LYRICS & TITLE", 12: "WINDOW", 13: "AUDIO",
                     24: "WATER REFLECTION", 28: "DECORATIONS", 33: "UPDATES"}
SETTINGS_HELP = [
    "Bar colors. 'Album art' follows the cover; the others are fixed palettes.",           # 0  Color theme
    "Bars rise from the bottom. Mirror grows outward from the middle.",                    # 1  Visualization
    "How many columns wide each bar is.",                                                  # 2  Bar width
    "Empty columns between the bars.",                                                     # 3  Bar spacing
    "How strongly the bars react to the music.",                                           # 4  Sensitivity
    "How slowly the bars fall back down. Higher is smoother.",                             # 5  Bar smoothing
    "How much the bars brighten on a beat.",                                               # 6  Beat flash
    "Show synced lyrics under the bars (looked up on LRCLIB).",                            # 7  Lyrics
    "Show lyrics earlier (+) or later (-). The [ and ] keys do the same.",                 # 8  Lyrics timing
    "How lyric lines move: fade, slide, karaoke fill, or all of it.",                      # 9  Lyric animation
    "Restyles the lyric text. Some styles need a font with math symbols.",                 # 10 Lyric font
    "Show the song title, progress bar and playback buttons at the top.",                  # 11 Song title
    "Window transparency. Kitty only, with remote control enabled.",                       # 12 Opacity
    "Route audio through Artcava DSP (virtual sink). Off = normal audio.",                 # 13 Audio processing
    "Left/Right applies a preset. Enter opens save, overwrite and delete options.",       # 14 Audio presets
    "Master output volume. 0-150%. Applies to the processed audio only.",                  # 14 Master volume
    "Boost low frequencies. 0 to +12 dB.",                                                 # 15 Bass boost
    "Adjust middle frequencies around 1 kHz. -12 to +12 dB.",                              # 16 Midrange
    "Adjust high frequencies. -12 to +12 dB.",                                             # 17 Treble
    "Equalizer band at 60 Hz. -12 to +12 dB.",                                             # 18 EQ 60 Hz
    "Equalizer band at 230 Hz. -12 to +12 dB.",                                            # 19 EQ 230 Hz
    "Equalizer band at 910 Hz. -12 to +12 dB.",                                            # 20 EQ 910 Hz
    "Equalizer band at 3.6 kHz. -12 to +12 dB.",                                           # 21 EQ 3.6 kHz
    "Equalizer band at 14 kHz. -12 to +12 dB.",                                            # 22 EQ 14 kHz
    "Mirror the bars in water underneath them (Bars visualization only).",                 # 23 Reflection
    "How visible the reflection is.",                                                      # 24 Reflection strength
    "How far the reflection reaches below the bars.",                                      # 25 Reflection length
    "How much the reflection ripples. 0% is a still mirror.",                              # 26 Water distortion
] + ["Show an image or GIF on screen, then drag it where you want it. Kitty only."] * 5 + [  # 27-31 Decorations
    "Compare this copy of Artcava with the latest version on GitHub.",                     # 32 Check for updates
]


def _vis_len(text):
    """Width of a string on screen, ignoring color codes."""
    return text_width(_ANSI_RE.sub("", text))


def _bg(color):
    return f"\x1b[48;2;{color[0]};{color[1]};{color[2]}m"


def _clip(text, width):
    """Trim a styled string to `width` columns; color codes are kept and not counted."""
    out, used = [], 0
    for part in re.split(r"(\x1b\[[0-9;]*[A-Za-z])", text):
        if not part:
            continue
        if part.startswith("\x1b["):
            out.append(part)
            continue
        for ch in part:
            w = char_width(ch)
            if used + w > width:
                return "".join(out) + _RESET
            out.append(ch)
            used += w
    return "".join(out)


def _wrap(text, width, max_lines):
    """Break text into lines of at most `width` columns (color codes allowed)."""
    lines, current = [], ""
    for word in text.split(" "):
        trial = word if not current else current + " " + word
        if _vis_len(trial) <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:max_lines]


def _slider(fraction, cells, accent):
    """A small slider: filled part in the accent color, a bright knob, a dim track."""
    pos = int(round(max(0.0, min(1.0, fraction)) * (cells - 1)))
    return (_fg(accent) + "━" * pos + _fg(_WHITE) + "●"
            + _fg(_TRACK) + "─" * (cells - 1 - pos))


def settings_widget(i, accent, palette):
    """The value column for settings row i, as colored text (foreground colors only)."""
    s = SETTINGS

    # The preset was inserted at row 14; preserve all legacy row mappings.
    if i == 14:
        return f"{_fg(_MUTED)}◂ {_fg(_BRIGHT)}{preset_label()} {_fg(_MUTED)}▸"
    if i > 14:
        i -= 1

    def choice(text):
        return f"{_fg(_MUTED)}◂ {_fg(_BRIGHT)}{text} {_fg(_MUTED)}▸"

    def pill(on):
        return f"{_fg(_GREEN)}● On " if on else f"{_fg(_MUTED)}○ Off"

    def meter(value, lo, hi, text, width=6):
        return f"{_slider((value - lo) / (hi - lo), 8, accent)} {_fg(_BRIGHT)}{text:>{width}}"

    if i == 0:
        mode = s["color_mode"]
        album = palette if palette is not None and len(palette) else DEFAULT_PALETTE
        colors = album if COLOR_PRESETS[mode] is None else COLOR_PRESETS[mode]
        swatch = "".join(_fg(tuple(int(v) for v in c)) + "██" for c in colors[:4])
        return f"{_fg(_MUTED)}◂ {swatch} {_fg(_BRIGHT)}{mode} {_fg(_MUTED)}▸"
    if i == 1:
        return choice(s["visualization"])
    if i == 2:
        return choice(s["bar_width"])
    if i == 3:
        return choice(s["gap"])
    if i == 4:
        return meter(s["sensitivity"], 0.5, 2.5, f'{s["sensitivity"]:.1f}x')
    if i == 5:
        return meter(s["release_time"], 0.06, 0.5, f'{s["release_time"]:.2f}s')
    if i == 6:
        return meter(s["beat_flash"], 0.0, 1.0, f'{s["beat_flash"]:.2f}')
    if i == 7:
        return pill(s["lyrics"])
    if i == 8:
        return meter(s["lyrics_offset"], -5.0, 5.0, f'{s["lyrics_offset"]:+.2f}s')
    if i == 9:
        return choice(s["lyric_animation"])
    if i == 10:
        return choice(s["lyric_font"])
    if i == 11:
        return pill(s["show_title"])
    if i == 12:
        return meter(s["opacity"], 0.10, 1.0, f'{s["opacity"]:.2f}')
    if i == 13:
        return pill(s["audio_enabled"])
    if i == 14:
        return meter(s["audio_volume"], 0, 150, f'{s["audio_volume"]}%')
    if i == 15:
        return meter(s["audio_bass"], 0.0, 12.0, f'{s["audio_bass"]:+.1f} dB')
    if i == 16:
        return meter(s["audio_mid"], -12.0, 12.0, f'{s["audio_mid"]:+.1f} dB')
    if i == 17:
        return meter(s["audio_treble"], -12.0, 12.0, f'{s["audio_treble"]:+.1f} dB')
    if i == 18:
        return meter(s["audio_eq_60"], -12.0, 12.0, f'{s["audio_eq_60"]:+.1f} dB')
    if i == 19:
        return meter(s["audio_eq_230"], -12.0, 12.0, f'{s["audio_eq_230"]:+.1f} dB')
    if i == 20:
        return meter(s["audio_eq_910"], -12.0, 12.0, f'{s["audio_eq_910"]:+.1f} dB')
    if i == 21:
        return meter(s["audio_eq_3640"], -12.0, 12.0, f'{s["audio_eq_3640"]:+.1f} dB')
    if i == 22:
        return meter(s["audio_eq_14000"], -12.0, 12.0, f'{s["audio_eq_14000"]:+.1f} dB')
    if i == 23:
        return pill(s["reflection"])
    if i == 24:
        return meter(s["reflection_strength"], 0.10, 1.0, f'{s["reflection_strength"] * 100:.0f}%')
    if i == 25:
        return meter(s["reflection_length"], 0.20, 1.0, f'{s["reflection_length"] * 100:.0f}%')
    if i == 26:
        return meter(s["water_distortion"], 0.0, 1.0, f'{s["water_distortion"] * 100:.0f}%')
    if 27 <= i <= 31:
        path = s["decorations"][i - 27]["path"]
        name = os.path.basename(path)[:14] if path else "Empty"
        return f"{_fg(_BRIGHT if path else _MUTED)}{name} {_fg(_MUTED)}▸"
    return f"{_fg(_BRIGHT)}Check now {_fg(_MUTED)}▸"


def settings_screen(rows, selected, message, update_prompt, accent, palette, cols, lines):
    """The whole settings screen as one string: a centered, bordered panel with sections."""
    accent = tuple(int(c) for c in (accent if accent is not None else (105, 190, 255)))     # may be a numpy array
    panel = min(cols, max(12, min(cols - 2, 72)))               # never wider than the window
    inner = panel - 2
    margin = " " * max(0, (cols - panel) // 2)
    edge = _fg(_mix(accent, (0, 0, 0), 0.45))
    select_bg = _bg(_mix((16, 18, 26), accent, 0.32))

    def boxed(content, bg=""):
        body = _clip(content, inner)
        pad = " " * max(0, inner - _vis_len(body))
        return f"{margin}{edge}│{_RESET}{bg}{body}{pad}{_RESET}{edge}│{_RESET}"

    # What to show: section headings and rows, scrolled so the selected row stays in view.
    display = []
    for i in range(len(rows)):
        if i in SETTINGS_SECTIONS:
            display.append(("section", SETTINGS_SECTIONS[i]))
        display.append(("row", i))
    pos = display.index(("row", selected))
    visible = max(5, min(len(display), lines - 9))
    first = max(0, min(pos - visible // 2, len(display) - visible))
    if first > 0 and display[first][0] == "row" and display[first - 1][0] == "section" and pos < first - 1 + visible:
        first -= 1                                              # keep a section's heading with its first row
    shown = display[first:first + visible]

    out = []
    title = " A R T C A V A  ·  S E T T I N G S "
    top = _clip(title, max(0, inner - 2))
    out.append(f"{margin}{edge}╭─{_RESET}\x1b[1m{_fg(accent)}{top}{_RESET}{edge}"
               f"{'─' * max(0, inner - 1 - _vis_len(top))}╮{_RESET}")

    for kind, value in shown:
        if kind == "section":
            fill = "─" * max(0, inner - 3 - len(value))
            out.append(boxed(f" \x1b[1m{_fg(accent)}{value}\x1b[22m {edge}{fill}"))
            continue
        widget = settings_widget(value, accent, palette)
        label_w = max(3, inner - 5 - _vis_len(widget))
        label = fit(rows[value], label_w)
        label += " " * (label_w - text_width(label))
        if value == selected:
            line = (f" {_fg(accent)}\x1b[1m❯\x1b[22m {_fg(_BRIGHT)}\x1b[1m{label}\x1b[22m {widget} ")
            out.append(boxed(line, select_bg))
        else:
            out.append(boxed(f"   {_fg(_TEXT)}{label} {widget} "))

    divider = f"{margin}{edge}├{'─' * inner}┤{_RESET}"
    out.append(divider)
    notes = []
    if update_prompt:
        notes.append(f" {_fg((255, 214, 120))}[U] Update now (copy link + open GitHub)   [X] Update later")
    shade = _fg(_BRIGHT) if message else _fg(_MUTED)
    text = message or (SETTINGS_HELP[selected] if selected < len(SETTINGS_HELP) else "")
    notes += [" " + shade + line for line in _wrap(text, inner - 2, 3 - len(notes))]
    for note in (notes + [""] * 3)[:3]:
        out.append(boxed(note))
    out.append(divider)

    more_above, more_below = first > 0, first + visible < len(display)
    counter = f"{'▲' if more_above else ' '}{'▼' if more_below else ' '} {selected + 1}/{len(rows)} "
    hints = " ↑↓ move   ←→ change   Enter open   Esc close"
    if _vis_len(hints) + len(counter) + 1 > inner:
        hints = " ↑↓  ←→  Enter  Esc"
    gap = " " * max(1, inner - _vis_len(hints) - len(counter))
    out.append(boxed(f"{_fg(_MUTED)}{hints}{gap}{_fg(_MUTED)}{counter}"))
    out.append(f"{margin}{edge}╰{'─' * inner}╯{_RESET}")

    spare = lines - len(out)
    pad_top = "\n" * (1 if spare > 2 else 0)
    return "\x1b[2J\x1b[H" + pad_top + "\n".join(out)



def audio_preset_name_prompt():
    """Enter a printable preset name without leaving the terminal UI."""
    name = ""
    while True:
        cols, _lines = shutil.get_terminal_size()
        width = max(10, cols - 4)
        sys.stdout.write("\x1b[2J\x1b[H\x1b[1;36m A R T C A V A / SAVE AUDIO PRESET\x1b[0m\n\n")
        sys.stdout.write(" " + fit("Enter a name for the current audio settings:", width) + "\n\n")
        sys.stdout.write("  " + fit(name + "_", width) + "\n\n")
        sys.stdout.write(" Enter save   Backspace erase   Esc cancel\n")
        sys.stdout.flush()
        for key in read_keys():
            if key == "\x1b":
                return None
            if key in ("\r", "\n"):
                return name.strip()
            if key in ("\x7f", "\b"):
                name = name[:-1]
            elif len(key) == 1 and key.isprintable() and len(name) < AUDIO_PRESET_NAME_MAX:
                name += key


def audio_presets_menu():
    """Choose, save, overwrite, or delete audio presets in Settings only."""
    rows = ("Choose preset", "Save current settings as new", "Overwrite selected preset",
            "Delete selected preset", "Back")
    selected = 0
    message = "Left/Right switches and applies a preset. * means edited since saving."
    confirm_delete = False
    while True:
        cols, _lines = shutil.get_terminal_size()
        width = max(10, cols - 4)
        sys.stdout.write("\x1b[2J\x1b[H\x1b[1;36m A R T C A V A / AUDIO PRESETS\x1b[0m\n\n")
        for i, label in enumerate(rows):
            marker = "\x1b[1;36m ❯ " if i == selected else "   "
            value = "  ◂ " + preset_label() + " ▸" if i == 0 else ""
            sys.stdout.write(marker + fit(label + value, width) + "\x1b[0m\n")
        sys.stdout.write("\n " + fit(message, width) + "\n")
        sys.stdout.write("\n ↑/↓ select   ←/→ choose   Enter open   Esc back\n")
        sys.stdout.flush()
        for key in read_keys():
            if confirm_delete:
                confirm_delete = False
                if key in ("y", "Y"):
                    name = SETTINGS["audio_preset"]
                    if delete_audio_preset(name):
                        message = f"Deleted {name}; current audio settings are unchanged."
                    else:
                        message = "Flat and Custom cannot be deleted."
                else:
                    message = "Delete cancelled."
                continue
            if key in ("\x1b",):
                return "Audio preset settings closed."
            if key in UP:
                selected = (selected - 1) % len(rows)
                message = ""
            elif key in DOWN:
                selected = (selected + 1) % len(rows)
                message = ""
            elif selected == 0 and (key in LEFT or key in RIGHT):
                name = cycle_audio_preset(-1 if key in LEFT else 1)
                message = f"Applied {name}. Processing on/off is unchanged."
            elif key in ("\r", "\n"):
                if selected == 0:
                    message = "Use Left/Right to choose and apply a preset."
                elif selected == 1:
                    name = audio_preset_name_prompt()
                    if name is None:
                        message = "Save cancelled."
                    else:
                        _ok, message = save_audio_preset(name)
                elif selected == 2:
                    name = SETTINGS["audio_preset"]
                    if name in SETTINGS["audio_presets"]:
                        _ok, message = save_audio_preset(name, overwrite=True)
                    else:
                        message = "Choose a saved preset first. Flat cannot be overwritten."
                elif selected == 3:
                    name = SETTINGS["audio_preset"]
                    if name in SETTINGS["audio_presets"]:
                        message = f"Delete {name}? Press Y to confirm; any other key cancels."
                        confirm_delete = True
                    else:
                        message = "Choose a saved preset first. Flat cannot be deleted."
                else:
                    return "Audio preset settings closed."

def settings_menu(update_checker=None, update_ui_state=None, decoration_renderer=None, accent=None, palette=None, audio_dsp=None):
    """Display and edit Artcava's settings, including five decoration slots."""
    rows = [
        "Color theme", "Visualization", "Bar width", "Bar spacing",
        "Sensitivity", "Bar smoothing", "Beat flash", "Lyrics",
        "Lyrics timing", "Lyric animation", "Lyric font", "Song title", "Opacity",
        "Audio processing", "Audio presets", "Master volume", "Bass boost", "Midrange", "Treble",
        "EQ 60 Hz", "EQ 230 Hz", "EQ 910 Hz", "EQ 3.6 kHz", "EQ 14 kHz",
        "Reflection", "Reflection strength", "Reflection length", "Water distortion",
        "Decoration slot 1", "Decoration slot 2", "Decoration slot 3",
        "Decoration slot 4", "Decoration slot 5", "Check for updates",
    ]
    selected = 0
    color_names = list(COLOR_PRESETS)
    update_prompt = False

    def change(direction):
        if selected == 14:
            return f"Applied preset: {cycle_audio_preset(direction)}"
        row = selected if selected < 14 else selected - 1
        if row == 0:
            idx = color_names.index(SETTINGS["color_mode"])
            SETTINGS["color_mode"] = color_names[(idx + direction) % len(color_names)]
        elif row == 1:
            idx = VISUALIZATIONS.index(SETTINGS["visualization"])
            SETTINGS["visualization"] = VISUALIZATIONS[(idx + direction) % len(VISUALIZATIONS)]
        elif row == 2:
            SETTINGS["bar_width"] = max(1, min(4, SETTINGS["bar_width"] + direction))
        elif row == 3:
            SETTINGS["gap"] = max(0, min(4, SETTINGS["gap"] + direction))
        elif row == 4:
            SETTINGS["sensitivity"] = round(max(0.5, min(2.5, SETTINGS["sensitivity"] + direction * 0.1)), 1)
        elif row == 5:
            SETTINGS["release_time"] = round(max(0.06, min(0.5, SETTINGS["release_time"] + direction * 0.02)), 2)
        elif row == 6:
            SETTINGS["beat_flash"] = round(max(0.0, min(1.0, SETTINGS["beat_flash"] + direction * 0.05)), 2)
        elif row == 7:
            SETTINGS["lyrics"] = not SETTINGS["lyrics"]
        elif row == 8:
            SETTINGS["lyrics_offset"] = round(max(-5.0, min(5.0, SETTINGS["lyrics_offset"] + direction * 0.25)), 2)
        elif row == 9:
            idx = LYRIC_ANIMATIONS.index(SETTINGS["lyric_animation"])
            SETTINGS["lyric_animation"] = LYRIC_ANIMATIONS[(idx + direction) % len(LYRIC_ANIMATIONS)]
            return LYRIC_ANIMATION_HELP[SETTINGS["lyric_animation"]]
        elif row == 10:
            idx = LYRIC_FONTS.index(SETTINGS["lyric_font"])
            SETTINGS["lyric_font"] = LYRIC_FONTS[(idx + direction) % len(LYRIC_FONTS)]
            return "Preview: " + font_preview(SETTINGS["lyric_font"])
        elif row == 11:
            SETTINGS["show_title"] = not SETTINGS["show_title"]
        elif row == 12:
            new_value = round(max(0.10, min(1.0, SETTINGS["opacity"] + direction * 0.05)), 2)
            SETTINGS["opacity"] = new_value
            if not kitty_opacity(new_value):
                return "Kitty opacity unavailable - check Kitty remote-control settings"
            return "Kitty opacity updated"
        elif row == 13:
            SETTINGS["audio_enabled"] = not SETTINGS["audio_enabled"]
            if audio_dsp is not None:
                if SETTINGS["audio_enabled"]:
                    if audio_dsp.start():
                        return "Audio processing enabled (virtual sink ArtcavaDSP)"
                    SETTINGS["audio_enabled"] = False
                    return f"Audio DSP failed: {audio_dsp.last_error or 'unknown error'}"
                audio_dsp.stop()
                return "Audio processing disabled - original routing restored"
            return None
        elif row == 14:
            SETTINGS["audio_volume"] = max(0, min(150, SETTINGS["audio_volume"] + direction * 5))
        elif row == 15:
            SETTINGS["audio_bass"] = round(max(0.0, min(12.0, SETTINGS["audio_bass"] + direction * 0.5)), 1)
        elif row == 16:
            SETTINGS["audio_mid"] = round(max(-12.0, min(12.0, SETTINGS["audio_mid"] + direction * 0.5)), 1)
        elif row == 17:
            SETTINGS["audio_treble"] = round(max(-12.0, min(12.0, SETTINGS["audio_treble"] + direction * 0.5)), 1)
        elif row in (18, 19, 20, 21, 22):
            eq_keys = ["audio_eq_60", "audio_eq_230", "audio_eq_910", "audio_eq_3640", "audio_eq_14000"]
            eq_key = eq_keys[row - 18]
            SETTINGS[eq_key] = round(max(-12.0, min(12.0, SETTINGS[eq_key] + direction * 0.5)), 1)
        elif row == 23:
            SETTINGS["reflection"] = not SETTINGS["reflection"]
        elif row == 24:
            SETTINGS["reflection_strength"] = round(max(0.1, min(1.0, SETTINGS["reflection_strength"] + direction * 0.05)), 2)
        elif row == 25:
            SETTINGS["reflection_length"] = round(max(0.2, min(1.0, SETTINGS["reflection_length"] + direction * 0.05)), 2)
        elif row == 26:
            SETTINGS["water_distortion"] = round(max(0.0, min(1.0, SETTINGS["water_distortion"] + direction * 0.05)), 2)
        if 23 <= row <= 26 and SETTINGS["visualization"] != "Bars":
            return "The reflection only appears with the Bars visualization."
        if 14 <= row <= 22 and SETTINGS.get("audio_enabled") and audio_dsp is not None and audio_dsp.is_active():
            return "Audio DSP updated live"
        return None

    message = ""
    while True:
        cols, lines = shutil.get_terminal_size()
        sys.stdout.write(settings_screen(rows, selected, message, update_prompt, accent, palette, cols, lines))
        sys.stdout.flush()

        for key in read_keys():
            if update_prompt and key in ("u", "U"):
                message = open_update_page()
                update_prompt = False
                if update_ui_state is not None:
                    update_ui_state["dismissed"] = True
                continue
            if update_prompt and key in ("x", "X"):
                message = "Update postponed. You can check again later."
                update_prompt = False
                if update_ui_state is not None:
                    update_ui_state["dismissed"] = True
                continue

            if key in UP:
                selected = (selected - 1) % len(rows)
                message = ""
            elif key in DOWN:
                selected = (selected + 1) % len(rows)
                message = ""
            elif selected == 14 and key in ("\r", "\n"):
                message = audio_presets_menu()
            elif 28 <= selected <= 32 and key in ("\r", "\n"):
                if decoration_renderer is None:
                    message = "Decoration renderer is unavailable."
                else:
                    decoration_menu(selected - 28, decoration_renderer)
                    message = f"Decoration slot {selected - 27} settings closed."
            elif selected == len(rows) - 1 and key in ("\r", "\n"):
                if update_checker is None:
                    message = "Update checker is unavailable."
                else:
                    state = update_checker.check_now()
                    if state == "available":
                        update_prompt = True
                        message = "A newer Artcava.py is available on GitHub."
                    elif state == "up_to_date":
                        message = "Artcava is up to date!"
                    else:
                        message = "Couldn't check for updates. Check your internet connection."
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
    def __init__(self, monitor="@DEFAULT_MONITOR@"):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.proc = subprocess.Popen(
            [
                "parec", "--format=s16le", f"--rate={RATE}", "--channels=1",
                "--latency-msec=10", "-d", monitor,
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


# ------------------------------------------------------------ audio DSP --
# Genuine audible processing via a temporary virtual sink.
#
# Routing when enabled:
#   playback apps -> artcava_dsp (virtual sink, default) -> parec monitor
#   -> DSP worker (this file, --dsp-worker) -> paplay -> original sink -> speakers
#   visualizer taps original sink monitor, so it sees processed audio.
#
# When disabled / on exit the reverse happens: pipeline stopped, streams
# moved back to the original sink, original default restored, virtual sink
# unloaded. Nothing is written to permanent PipeWire/PulseAudio config.
#
# The worker itself is pure numpy (already a dependency): FFT EQ with
# Hann-window overlap-add (4096 FFT / 2048 hop at 48 kHz), stereo-linked
# gains, and a soft-knee limiter so +12 dB boosts and 150% volume cannot
# hard-clip. It polls settings.json so Settings > Audio changes apply live
# without restarting the pipeline or the visualizer.

def compute_eq_gains(params, rate=AUDIO_DSP_RATE, fft_size=AUDIO_DSP_FFT_SIZE):
    """Linear FFT gains for one block, from audio_* settings.

    Bass is a lowshelf (full below 200 Hz, gone by 800 Hz), treble a
    highshelf (in from 2 kHz, full above 8 kHz), midrange and each EQ
    band are Gaussian peaks in log-frequency. DC is left at 0 dB.
    Master volume (0-150%) is applied as a flat linear gain.
    """
    freqs = np.fft.rfftfreq(fft_size, 1.0 / rate)
    safe = np.maximum(freqs, 1.0)
    logf = np.log2(safe)
    db = np.zeros_like(freqs, dtype=np.float64)

    bass_db = float(params.get("audio_bass", 0.0) or 0.0)
    if bass_db:
        lo, hi = math.log2(200.0), math.log2(800.0)
        t = np.clip((logf - lo) / (hi - lo), 0.0, 1.0)
        db += bass_db * (0.5 + 0.5 * np.cos(np.pi * t))

    treble_db = float(params.get("audio_treble", 0.0) or 0.0)
    if treble_db:
        lo, hi = math.log2(2000.0), math.log2(8000.0)
        t = np.clip((logf - lo) / (hi - lo), 0.0, 1.0)
        db += treble_db * (0.5 - 0.5 * np.cos(np.pi * t))

    mid_db = float(params.get("audio_mid", 0.0) or 0.0)
    if mid_db:
        db += mid_db * np.exp(-0.5 * ((logf - math.log2(1000.0)) / 0.7) ** 2)

    for key, center, _label in AUDIO_EQ_BANDS:
        gain_db = float(params.get(key, 0.0) or 0.0)
        if gain_db:
            db += gain_db * np.exp(-0.5 * ((logf - math.log2(center)) / 0.6) ** 2)

    db[0] = 0.0
    linear = 10.0 ** (db / 20.0)
    volume = float(params.get("audio_volume", 100) or 0)
    linear *= max(0.0, volume / 100.0)
    return linear.astype(np.float64)


def soft_clip(x, threshold=0.85, ceiling=0.98):
    """Soft-knee limiter: linear below threshold, asymptotically ceiling above.

    Prevents hard clipping/distortion when EQ boosts and volume are
    combined, while leaving normal-level material untouched.
    """
    ax = np.abs(x)
    denom = ceiling - threshold
    return np.where(
        ax <= threshold,
        x,
        np.sign(x) * (threshold + denom * (1.0 - np.exp(-np.maximum(ax - threshold, 0.0) / denom))),
    )


class DSPProcessor:
    """Streaming stereo FFT EQ with overlap-add. Pure numpy, no scipy needed."""

    def __init__(self, params=None, rate=AUDIO_DSP_RATE,
                 fft_size=AUDIO_DSP_FFT_SIZE, hop=AUDIO_DSP_HOP):
        self.rate = rate
        self.fft_size = fft_size
        self.hop = hop
        self.params = dict(params or {})
        self.gains = compute_eq_gains(self.params, rate, fft_size)
        # Hann analysis window; 50% overlap-add of Hann sums to 1.0, so a
        # flat EQ reconstructs the input at unity gain (verified by tests).
        self.window = np.hanning(fft_size).astype(np.float64)
        self.ola = np.zeros((fft_size, AUDIO_DSP_CHANNELS), dtype=np.float64)
        self.input_buffer = np.zeros((0, AUDIO_DSP_CHANNELS), dtype=np.float64)
        # Frame counters so flush() can drain exactly the input length
        # (no infinite padding loop, no extra tail beyond the input).
        self.total_in = 0
        self.total_out = 0

    def update_params(self, params):
        new_gains = compute_eq_gains(params, self.rate, self.fft_size)
        if not np.allclose(new_gains, self.gains):
            self.gains = new_gains
            self.params = dict(params)
            return True
        return False

    def _process_one_block(self):
        """Process one FFT block from input_buffer; return one hop of output."""
        block = self.input_buffer[:self.fft_size] * self.window[:, None]
        spectrum = np.fft.rfft(block, axis=0)
        spectrum *= self.gains[:, None]
        filtered = np.fft.irfft(spectrum, n=self.fft_size, axis=0)
        self.ola += filtered
        out = self.ola[:self.hop].copy()
        # Shift OLA left by one hop; zero the tail for the next block.
        self.ola = np.concatenate(
            (self.ola[self.hop:], np.zeros((self.hop, AUDIO_DSP_CHANNELS))), axis=0
        )
        self.input_buffer = self.input_buffer[self.hop:]
        self.total_out += self.hop
        return out

    def process(self, samples):
        """Feed (n, 2) float samples; return newly available output (may be empty)."""
        if samples is not None and len(samples):
            samples = np.asarray(samples, dtype=np.float64)
            if samples.ndim == 1:
                samples = samples.reshape(-1, AUDIO_DSP_CHANNELS)
            self.total_in += len(samples)
            self.input_buffer = np.concatenate((self.input_buffer, samples), axis=0)
        out_chunks = []
        while len(self.input_buffer) >= self.fft_size:
            out_chunks.append(self._process_one_block())
        if not out_chunks:
            return np.zeros((0, AUDIO_DSP_CHANNELS), dtype=np.float64)
        return soft_clip(np.concatenate(out_chunks, axis=0))

    def flush(self):
        """Drain remaining buffered audio; total output == total input. Call at EOF."""
        remaining = self.total_in - self.total_out
        if remaining <= 0:
            return np.zeros((0, AUDIO_DSP_CHANNELS), dtype=np.float64)
        out_chunks = []
        # Pad the final partial block once, then process until the input
        # length has been produced. Padded zeros beyond the input are
        # trimmed below, so this always terminates.
        if len(self.input_buffer) < self.fft_size:
            pad = self.fft_size - len(self.input_buffer)
            self.input_buffer = np.concatenate(
                (self.input_buffer, np.zeros((pad, AUDIO_DSP_CHANNELS))), axis=0
            )
        while self.total_out < self.total_in:
            if len(self.input_buffer) < self.fft_size:
                pad = self.fft_size - len(self.input_buffer)
                self.input_buffer = np.concatenate(
                    (self.input_buffer, np.zeros((pad, AUDIO_DSP_CHANNELS))), axis=0
                )
            out_chunks.append(self._process_one_block())
        if not out_chunks:
            return np.zeros((0, AUDIO_DSP_CHANNELS), dtype=np.float64)
        out = soft_clip(np.concatenate(out_chunks, axis=0))
        # Trim any output produced from zero-padding beyond the input length.
        excess = len(out) - remaining
        if excess > 0:
            out = out[:remaining]
        return out


def read_dsp_params():
    """Read audio_* values from settings.json (worker polls this for live updates)."""
    params = {k: DEFAULTS[k] for k in DEFAULTS if k.startswith("audio_")}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key in params:
                if key in saved:
                    params[key] = saved[key]
    except (OSError, ValueError):
        pass
    for key, lo, hi, cast in LIMITS:
        if not key.startswith("audio_"):
            continue
        try:
            value = cast(params[key])
            if not math.isfinite(value):
                raise ValueError
            params[key] = max(lo, min(hi, value))
        except (TypeError, ValueError, OverflowError):
            params[key] = DEFAULTS[key]
    return params


def dsp_worker_main():
    """Stdin/stdout s16le stereo worker: parec monitor in, processed PCM out."""
    rate = AUDIO_DSP_RATE
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--rate" and i + 1 < len(argv):
            try:
                rate = int(argv[i + 1])
            except ValueError:
                pass
    processor = DSPProcessor(params=read_dsp_params(), rate=rate)
    try:
        last_mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        last_mtime = 0.0
    hop_bytes = AUDIO_DSP_HOP * AUDIO_DSP_CHANNELS * 2
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    try:
        while True:
            # Live-update EQ/volume from settings.json without a restart.
            try:
                mtime = os.path.getmtime(CONFIG_FILE)
            except OSError:
                mtime = 0.0
            if mtime != last_mtime:
                last_mtime = mtime
                processor.update_params(read_dsp_params())
            data = stdin.read(hop_bytes)
            if not data:
                break
            # Drop a truncated final stereo frame, not just a single byte.
            data = data[:len(data) - len(data) % (AUDIO_DSP_CHANNELS * 2)]
            if not data:
                break
            samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
            samples = samples.reshape(-1, AUDIO_DSP_CHANNELS)
            out = processor.process(samples)
            if len(out):
                pcm = np.clip(out, -1.0, 1.0)
                stdout.write((pcm * 32767.0).astype("<i2").tobytes())
                stdout.flush()
        tail = processor.flush()
        if len(tail):
            pcm = np.clip(tail, -1.0, 1.0)
            stdout.write((pcm * 32767.0).astype("<i2").tobytes())
            stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        try:
            stdout.flush()
        except Exception:
            pass


def _run_pactl(args, timeout=3):
    """Run pactl; return (returncode, stdout, stderr). Never raises."""
    try:
        result = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return 127, "", "pactl not found"
    except subprocess.TimeoutExpired:
        return 124, "", "pactl timeout"
    except OSError as exc:
        return 1, "", str(exc)


def _pactl_default_sink():
    rc, out, _err = _run_pactl(["get-default-sink"])
    if rc == 0 and out:
        return out.splitlines()[0].strip()
    rc, out, _err = _run_pactl(["info"])
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("Default Sink:"):
                return line.split(":", 1)[1].strip()
    return None


def _pactl_sink_exists(name):
    rc, out, _err = _run_pactl(["list", "short", "sinks"])
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip() == name:
            return True
    return False


def _pactl_source_exists(name):
    rc, out, _err = _run_pactl(["list", "short", "sources"])
    if rc != 0:
        return False
    return any(len(parts) >= 2 and parts[1].strip() == name
               for parts in (line.split("\t") for line in out.splitlines()))


def _pactl_sinks():
    rc, out, _err = _run_pactl(["list", "short", "sinks"])
    if rc != 0:
        return None
    sinks = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            sinks[parts[1].strip()] = parts[0].strip()
    return sinks


def _pactl_sink_inputs():
    """Return (stream id, sink index), or None on server failure."""
    rc, out, _err = _run_pactl(["list", "short", "sink-inputs"])
    if rc != 0:
        return None
    inputs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            inputs.append((parts[0].strip(), parts[1].strip()))
    return inputs


class AudioDSPManager:
    """Owns the virtual-sink routing and the parec->worker->paplay pipeline.

    start() is idempotent; stop() always attempts to restore the original
    sink/default even if individual steps fail. All failures are reported
    via last_error and leave the visualizer running without DSP.
    """

    def __init__(self):
        self.active = False
        self.original_sink = None
        self.module_index = None
        self.parec_proc = None
        self.worker_proc = None
        self.paplay_proc = None
        self.proc_logs = {}
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.last_error = ""
        self.status = "off"

    def is_active(self):
        return self.active

    def expected_monitor(self):
        """Monitor the visualizer should tap: processed output when DSP is on."""
        if self.active and self.original_sink:
            return f"{self.original_sink}.monitor"
        return "@DEFAULT_MONITOR@"

    def _start_pipeline(self, original_sink):
        parec_cmd = ["parec", f"--device={AUDIO_DSP_SINK_NAME}.monitor",
                     "--format=s16le", f"--rate={AUDIO_DSP_RATE}",
                     f"--channels={AUDIO_DSP_CHANNELS}", "--latency-msec=20"]
        worker_cmd = [sys.executable, os.path.realpath(__file__), "--dsp-worker",
                      "--rate", str(AUDIO_DSP_RATE)]
        paplay_cmd = ["paplay", "--raw", f"--device={original_sink}",
                      "--format=s16le", f"--rate={AUDIO_DSP_RATE}",
                      f"--channels={AUDIO_DSP_CHANNELS}", "--latency-msec=20"]
        self.parec_proc = self.worker_proc = self.paplay_proc = None
        self.proc_logs = {}
        try:
            # Files avoid deadlocking a child on a full stderr pipe. Keep each
            # process's error separately so cascading failures are diagnosable.
            self.proc_logs = {name: tempfile.TemporaryFile(mode="w+b")
                              for name in ("parec", "DSP worker", "paplay")}
            parec = self.parec_proc = subprocess.Popen(parec_cmd, stdout=subprocess.PIPE,
                                     stderr=self.proc_logs["parec"], bufsize=0)
            worker = self.worker_proc = subprocess.Popen(worker_cmd, stdin=parec.stdout,
                                      stdout=subprocess.PIPE,
                                      stderr=self.proc_logs["DSP worker"], bufsize=0)
            try:
                parec.stdout.close()
            except Exception:
                pass
            paplay = self.paplay_proc = subprocess.Popen(paplay_cmd, stdin=worker.stdout,
                                      stdout=subprocess.DEVNULL,
                                      stderr=self.proc_logs["paplay"], bufsize=0)
            try:
                worker.stdout.close()
            except Exception:
                pass
        except OSError as exc:
            self.last_error = f"could not start DSP pipeline: {exc}"
            self._stop_pipeline()
            return False
        time.sleep(0.5)
        failed = [(name, proc.poll()) for proc, name in
                  ((parec, "parec"), (worker, "DSP worker"), (paplay, "paplay"))
                  if proc.poll() is not None]
        if failed:
            self.last_error = self._pipeline_error("DSP pipeline failed", failed)
            self._stop_pipeline()
            return False
        self.parec_proc, self.worker_proc, self.paplay_proc = parec, worker, paplay
        return True

    def _pipeline_error(self, summary, failed):
        details = []
        for name, code in failed:
            text = ""
            log = self.proc_logs.get(name)
            if log is not None:
                try:
                    log.flush()
                    log.seek(0, os.SEEK_END)
                    log.seek(max(0, log.tell() - 2048))
                    text = log.read().decode("utf-8", "replace").strip().replace("\n", "; ")
                except (OSError, ValueError):
                    pass
            details.append(f"{name} (exit {code})" + (f": {text}" if text else ""))
        return summary + ": " + " | ".join(details)

    def _stop_pipeline(self):
        for proc in (self.paplay_proc, self.worker_proc, self.parec_proc):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        for proc in (self.paplay_proc, self.worker_proc, self.parec_proc):
            if proc is None:
                continue
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.parec_proc = self.worker_proc = self.paplay_proc = None
        for log in self.proc_logs.values():
            try:
                log.close()
            except OSError:
                pass
        self.proc_logs = {}

    def _monitor_loop(self, stop_event):
        while not stop_event.wait(1.0):
            failed = None
            ended = [(name, proc.poll()) for proc, name in
                     ((self.parec_proc, "parec"), (self.worker_proc, "DSP worker"),
                      (self.paplay_proc, "paplay")) if proc is not None and proc.poll() is not None]
            if ended:
                failed = self._pipeline_error("DSP pipeline stopped unexpectedly", ended)
            if not failed and self.original_sink and not _pactl_sink_exists(self.original_sink):
                failed = "Original audio output disappeared or audio service stopped"
            if failed:
                with self.lock:
                    if stop_event is not self.stop_event or stop_event.is_set():
                        return
                    self.last_error = failed
                    self.status = "error"
                    self.stop()
                return

    def start(self):
        with self.lock:
            if self.active:
                return True
            self.last_error = ""
            self.status = "starting"
            for tool in ("pactl", "parec", "paplay"):
                if shutil.which(tool) is None:
                    self.last_error = (
                        f"{tool} not found - install pulseaudio-utils "
                        "(or PipeWire's pipewire-pulse)"
                    )
                    self.status = "error"
                    return False
            original = _pactl_default_sink()
            if not original:
                self.last_error = "could not determine default sink (is PipeWire/PulseAudio running?)"
                self.status = "error"
                return False
            if original == AUDIO_DSP_SINK_NAME:
                self.last_error = "default sink is already ArtcavaDSP (stale state?)"
                self.status = "error"
                return False
            sinks = _pactl_sinks()
            inputs = _pactl_sink_inputs()
            if sinks is None or inputs is None or original not in sinks:
                self.last_error = "could not enumerate sinks or playback streams"
                self.status = "error"
                return False
            if AUDIO_DSP_SINK_NAME in sinks:
                self.last_error = "ArtcavaDSP sink already exists; refusing to reuse it"
                self.status = "error"
                return False
            self.original_sink = original
            original_index = sinks[original]

            rc, out, err = _run_pactl([
                "load-module", "module-null-sink",
                f"sink_name={AUDIO_DSP_SINK_NAME}",
                "sink_properties=device.description=ArtcavaDSP",
                f"rate={AUDIO_DSP_RATE}", f"channels={AUDIO_DSP_CHANNELS}",
            ])
            if rc != 0:
                self.last_error = f"could not create virtual sink: {err or out or 'pactl failed'}"
                self.status = "error"
                self.original_sink = None
                return False
            self.module_index = out.splitlines()[0].strip() if out else None
            if not self.module_index or not self.module_index.isdigit():
                self.last_error = "audio server did not return a module ID; check for a stale ArtcavaDSP sink"
                self.status = "error"
                self.original_sink = None
                return False

            # Some servers publish the sink and its monitor asynchronously.
            # Do not start parec until both are visible.
            for _ in range(20):
                if (_pactl_sink_exists(AUDIO_DSP_SINK_NAME) and
                        _pactl_source_exists(f"{AUDIO_DSP_SINK_NAME}.monitor")):
                    break
                time.sleep(0.1)
            else:
                self.last_error = "virtual sink or monitor source did not appear after creation"
                self._unload_virtual_sink()
                self.status = "error"
                self.original_sink = None
                return False

            rc, _out, err = _run_pactl(["set-default-sink", AUDIO_DSP_SINK_NAME])
            if rc != 0:
                self.last_error = f"could not set default sink: {err or 'pactl failed'}"
                self._unload_virtual_sink()
                self.status = "error"
                self.original_sink = None
                return False

            # Move streams that existed before DSP started. paplay has not
            # started yet, so there is no DSP output stream to exclude and
            # no feedback loop is possible at this point.
            for input_id, sink_index in inputs:
                if sink_index == original_index:
                    _run_pactl(["move-sink-input", input_id, AUDIO_DSP_SINK_NAME], timeout=2)

            if not self._start_pipeline(original):
                # Roll back routing so the user still hears normal audio.
                self._restore_streams(original)
                if _pactl_default_sink() == AUDIO_DSP_SINK_NAME:
                    _run_pactl(["set-default-sink", original], timeout=2)
                self._unload_virtual_sink()
                self.status = "error"
                self.original_sink = None
                return False

            self.active = True
            self.status = "active"
            self.stop_event = threading.Event()
            self.monitor_thread = threading.Thread(target=self._monitor_loop,
                                                   args=(self.stop_event,), daemon=True)
            self.monitor_thread.start()
            return True

    def _restore_streams(self, target):
        """Move only streams still on our virtual sink, never unrelated streams."""
        sinks = _pactl_sinks()
        inputs = _pactl_sink_inputs()
        if sinks is None or inputs is None:
            return
        virtual_index = sinks.get(AUDIO_DSP_SINK_NAME)
        if virtual_index is not None:
            for input_id, sink_index in inputs:
                if sink_index == virtual_index:
                    _run_pactl(["move-sink-input", input_id, target], timeout=2)

    def _unload_virtual_sink(self):
        if self.module_index:
            _run_pactl(["unload-module", str(self.module_index)], timeout=2)
            self.module_index = None

    def stop(self):
        with self.lock:
            was_active = self.active or self.module_index is not None
            self.stop_event.set()
            # Never join while holding this lock: the monitor may be waiting
            # for it to perform an emergency rollback.
            self.monitor_thread = None
            self._stop_pipeline()
            original = self.original_sink
            if was_active and original:
                sinks = _pactl_sinks()
                target = original if sinks and original in sinks else None
                if target is None and sinks:
                    target = next((name for name in sinks if name != AUDIO_DSP_SINK_NAME), None)
                if target:
                    self._restore_streams(target)
                    # Do not override a default selected by the user while active.
                    if _pactl_default_sink() == AUDIO_DSP_SINK_NAME:
                        _run_pactl(["set-default-sink", target], timeout=2)
                self._unload_virtual_sink()
            self.active = False
            if self.status != "error":
                self.status = "off"
            self.original_sink = None


# ---------------------------------------------------------------- lyrics --
LRCLIB = "https://lrclib.net/api"
USER_AGENT = "Artcava (https://github.com/kbhankins2012-crypto/Artcava)"
LYRIC_CACHE = os.path.expanduser("~/.cache/artcava/lyrics")
LYRIC_ROWS = 4
HEADER_ROWS = 2
PROGRESS_MAX_WIDTH = 100         # widest the progress bar gets (wider = finer seeking)
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
        self.player = None            # name of the player that is playing (seek target)
        self.seek_preview = None      # seconds shown while the progress bar is being dragged
        self.hold_until = 0.0         # ignore polled positions until the player catches up
        self.seek_failed = False

    def position(self):
        if self.seek_preview is not None:          # dragging: show where the bar is being moved
            return self.seek_preview
        if self.playing:
            return self.anchor_pos + (time.monotonic() - self.anchor_t)
        return self.anchor_pos

    def seek(self, seconds):
        """Jump to `seconds` in the current song; playerctl runs in the background."""
        track = self.track
        length = float(track[3] or 0.0) if track else 0.0
        if length > 0:
            seconds = min(seconds, max(0.0, length - 1.0))      # don't skip past the end
        seconds = max(0.0, seconds)
        now = time.monotonic()
        self.anchor_pos, self.anchor_t = seconds, now           # show the jump immediately
        self.hold_until = now + 1.2                             # ignore a stale position while the player catches up
        command = ["playerctl"]
        if self.player:
            command.append(f"--player={self.player}")
        command += ["position", f"{seconds:.2f}"]

        def run():
            try:
                result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=3)
                failed = result.returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                failed = True
            if failed:
                self.seek_failed = True
                self.hold_until = 0.0                           # go back to showing the real position

        threading.Thread(target=run, daemon=True).start()

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
        self.player = player
        self.track = (
            title,
            album,
            re.sub(r"\s*-\s*Topic$", "", artist).strip(),
            length,
        )

        # Keep playback position updated even when synced lyrics are disabled,
        # because the same clock drives the on-screen song progress bar.
        pos = self.read_position(player)
        if pos is not None and time.monotonic() >= self.hold_until:
            self.anchor_pos = max(0.0, pos)
            self.anchor_t = time.monotonic()
        self.playing = status == "Playing"

        if not SETTINGS["lyrics"]:
            self.key = None
            self.data = ("idle", [], [])
            return

        key = (artist, title, album, round(length))
        if key != self.key:
            # IMPORTANT: lyrics_offset is a user preference, not per-track
            # state. Never reset or modify it when the song changes. The value
            # is loaded from settings.json and changes only through the user's
            # Lyrics timing controls; save_settings() persists those changes.
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


def format_time(seconds):
    """Format a playback time as M:SS, or H:MM:SS for long tracks."""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        seconds = 0

    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_layout(cols, elapsed_text, total_text):
    """Where the progress bar sits on its row: (left_padding, bar_width, first_bar_column).

    Used both to draw the bar and to turn a mouse position into a time, so the two
    can never disagree. The bar grows with the window (up to PROGRESS_MAX_WIDTH).
    """
    fixed_width = text_width(elapsed_text) + text_width(total_text) + 6
    bar_width = max(6, min(PROGRESS_MAX_WIDTH, int(cols * 0.6), cols - fixed_width - 2))
    visible_width = text_width(elapsed_text) + 2 + bar_width + 2 + text_width(total_text)
    left_padding = max(0, (cols - visible_width) // 2)
    first_column = left_padding + text_width(elapsed_text) + 2 + 1      # 1-based, as the mouse reports
    return left_padding, bar_width, first_column


def progress_line(watcher, cols, accent):
    """Draw a centered, live song progress bar with elapsed and total time."""
    track = watcher.track
    if not track:
        return "\x1b[K"

    duration = max(0.0, float(track[3] or 0.0))
    position = max(0.0, watcher.position())
    if duration > 0:
        position = min(position, duration)
        fraction = min(1.0, position / duration)
        total_text = format_time(duration)
    else:
        fraction = 0.0
        total_text = "--:--"

    elapsed_text = format_time(position)
    left_padding, bar_width, _first_column = progress_layout(cols, elapsed_text, total_text)
    marker = min(bar_width - 1, int(round(fraction * (bar_width - 1))))
    filled = "━" * marker
    remaining = "─" * (bar_width - marker - 1)

    r, g, b = (int(c) for c in accent)
    active = f"\x1b[1;38;2;{r};{g};{b}m"
    dim = "\x1b[38;2;105;105;120m"
    marker_style = "\x1b[1;38;2;255;255;255m"

    content = (
        dim + elapsed_text + "  "
        + active + filled
        + marker_style + ("●" if duration > 0 else "○")
        + dim + remaining + "  " + total_text
        + "\x1b[0m"
    )
    return " " * left_padding + content + "\x1b[K"


def header_block(watcher, cols, accent):
    track = watcher.track
    r, g, b = (int(c) for c in accent)
    bold = f"\x1b[1;38;2;{r};{g};{b}m"
    controls_style = "\x1b[1;38;2;220;220;235m"
    control_width = text_width(CONTROLS)
    available = max(1, cols - control_width - 4)
    metadata = ""

    if track:
        title, album, artist = track[:3]
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
    return [line, progress_line(watcher, cols, accent)]


# -------------------------------------------------------------- lyric styling --
def _math_map(style, digit_style=None):
    """Map A-Z, a-z (and optionally digits) to a Unicode "Mathematical Alphanumeric" alphabet."""
    table = {}
    for i in range(26):
        for base, kind in ((65, "CAPITAL"), (97, "SMALL")):
            letter = chr(base + i)
            for name in (f"MATHEMATICAL {style} {kind} {letter.upper()}", f"{style} {kind} {letter.upper()}"):
                try:
                    table[ord(letter)] = ord(unicodedata.lookup(name))
                    break
                except KeyError:
                    continue
    if digit_style:
        words = ("ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE")
        for digit, word in enumerate(words):
            try:
                table[48 + digit] = ord(unicodedata.lookup(f"MATHEMATICAL {digit_style} DIGIT {word}"))
            except KeyError:
                pass
    return table


_SMALL_CAPS = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_FONT_TABLES = {
    "Script": str.maketrans(_math_map("BOLD SCRIPT")),
    "Gothic": str.maketrans(_math_map("BOLD FRAKTUR")),
    "Double-struck": str.maketrans(_math_map("DOUBLE-STRUCK", "DOUBLE-STRUCK")),
    "Monospace": str.maketrans(_math_map("MONOSPACE", "MONOSPACE")),
    "Sans bold": str.maketrans(_math_map("SANS-SERIF BOLD", "SANS-SERIF BOLD")),
    "Small caps": str.maketrans(_SMALL_CAPS),
    "Wide": str.maketrans({**{c: c + 0xFEE0 for c in range(0x21, 0x7F)}, 0x20: 0x3000}),
}


def apply_font(text, font):
    """Restyle text with a Unicode alphabet. Same number of characters; other characters are untouched.

    Normal, Bold and Italic have no table: Bold and Italic use the terminal's own bold/italic.
    """
    table = _FONT_TABLES.get(font)
    return text.translate(table) if table else text


def font_preview(font):
    """A short sample line in the given font, for the settings menu."""
    sample = "The quick brown fox jumps over 123"
    if font == "Bold":
        return "\x1b[1m" + sample + "\x1b[0m"
    if font == "Italic":
        return "\x1b[3m" + sample + "\x1b[0m"
    text = apply_font(sample, font)
    if font in _FONT_TABLES and font not in ("Small caps", "Wide"):
        text += "   (empty boxes? your terminal font lacks these symbols - pick Normal)"
    return text


LYRIC_TRANSITION = 0.45          # seconds for a line change to finish fading / sliding
LYRIC_SLIDE_COLUMNS = 10         # how far the new line slides in from the side
_LYRIC_DIM = (110, 110, 125)
_WHITE = (255, 255, 255)


def _mix(a, b, t):
    """Blend color a toward color b by t (0..1)."""
    return tuple(int(max(0, min(255, round(x + (y - x) * t)))) for x, y in zip(a, b))


def _fg(color, bold=False):
    return f"\x1b[{'1;' if bold else ''}38;2;{color[0]};{color[1]};{color[2]}m"


def lyric_block(watcher, cols, accent, pulse=0.0):
    """LYRIC_ROWS terminal lines: a spacer, then the previous, current and next lyric.

    Looks are controlled by SETTINGS["lyric_animation"] and SETTINGS["lyric_font"]. All of the
    animation is worked out from the song position (no stored state), so it stays correct when
    you seek, and with animation Off and font Normal the plain layout is unchanged.
    `pulse` (0..1, from the beat detector) makes the Lively mode glow on the beat.
    """
    state, times, lines = watcher.data
    mode = SETTINGS["lyric_animation"]
    font = SETTINGS["lyric_font"]
    attr = {"Bold": "\x1b[1m", "Italic": "\x1b[3m"}.get(font, "")     # the terminal's own bold / italic
    accent = tuple(int(c) for c in accent)

    def place(text, shift=0):
        """Styled, trimmed text and its left padding (centered, plus an optional sideways shift)."""
        text = fit(apply_font(text, font), max(1, cols - 2))
        width = text_width(text)
        return text, max(0, min((cols - width) // 2 + shift, cols - width))

    def plain(text, color, bold=False, shift=0):
        if not text:
            return "\x1b[K"
        text, pad = place(text, shift)
        return " " * pad + attr + _fg(color, bold) + text + "\x1b[0m\x1b[K"

    def sweep(text, age, gap, color, shift):
        """The current line with a karaoke-style fill that follows the singing."""
        text, pad = place(text, shift)
        sing = min(max(gap - 0.15, 0.3), max(1.2, 0.085 * len(text)))   # time to "sing" the line
        done = int(max(0.0, min(1.0, age / sing)) * len(text))
        sung, head, rest = text[:done], text[done:done + 1], text[done + 1:]
        unsung = _mix(_LYRIC_DIM, color, 0.40)
        return (" " * pad + attr + _fg(color, True) + sung
                + _fg(_mix(color, _WHITE, 0.65), True) + head
                + _fg(unsung) + rest + "\x1b[0m\x1b[K")

    if state == "ok":
        pos = watcher.position() + SETTINGS["lyrics_offset"]
        i = bisect.bisect_right(times, pos) - 1
        prev = lines[i - 1] if i >= 1 else ""
        cur = lines[i] if i >= 0 else ""
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        is_note = i >= 0 and not cur
        if is_note:
            cur = "♪"

        if mode == "Off":
            return ["\x1b[K", plain(prev, _LYRIC_DIM), plain(cur, accent, True), plain(nxt, _LYRIC_DIM)]

        age = pos - times[i] if i >= 0 else 1e9                      # seconds since this line began
        progress = max(0.0, min(1.0, age / LYRIC_TRANSITION))
        ease = 1.0 - (1.0 - progress) ** 3                           # fast start, soft landing
        color = accent
        if mode == "Lively":
            breath = 1.0 + 0.05 * float(np.sin(6.2832 * 0.4 * time.monotonic()))
            color = _mix(accent, _WHITE, min(1.0, max(0.0, pulse)) * 0.35)   # glow on the beat
            color = tuple(min(255, int(c * breath)) for c in color)          # slow shimmer
        shift = int(round((1.0 - ease) * LYRIC_SLIDE_COLUMNS)) if mode in ("Slide", "Lively") else 0

        prev_color = _mix(accent, _LYRIC_DIM, ease)                  # the old line fades down...
        cur_color = _mix(_LYRIC_DIM, color, ease)                    # ...the new one lights up...
        next_color = _mix((0, 0, 0), _LYRIC_DIM, ease)               # ...and the next fades in
        if mode in ("Karaoke", "Lively") and i >= 0 and not is_note:
            gap = times[i + 1] - times[i] if i + 1 < len(times) else 5.0
            cur_line = sweep(cur, age, gap, cur_color, shift)
        else:
            cur_line = plain(cur, cur_color, True, shift)
        return ["\x1b[K", plain(prev, prev_color), cur_line, plain(nxt, next_color)]

    msg = {"loading": "Searching for lyrics…", "none": "No synced lyrics found"}.get(state, "")
    return ["\x1b[K", "\x1b[K", plain(msg, _LYRIC_DIM), "\x1b[K"]


class SeekBar:
    """Click or drag the progress bar (second header row) to jump within the song.

    Dragging previews the new time (the bar, the clock and the lyrics follow the
    mouse); letting go sends the seek to the player that is playing.
    """
    PROGRESS_ROW = 2

    def __init__(self):
        self.dragging = False
        self.bar = (1, 2, 0.0)       # (first column, width, song length), frozen at the start of a drag
        self.last_event = 0.0
        self.message = ""

    def _target(self, x):
        first, width, duration = self.bar
        fraction = (x - first) / max(1, width - 1)
        return max(0.0, min(1.0, fraction)) * duration

    def cancel(self, watcher, commit=False):
        """End a drag; with commit=True, jump to where it was released."""
        if not self.dragging:
            return
        self.dragging = False
        target = watcher.seek_preview
        watcher.seek_preview = None
        if commit and target is not None:
            watcher.seek(target)

    def tick(self, watcher):
        """Drop a drag whose button release never arrived (for example after a focus change)."""
        if self.dragging and time.monotonic() - self.last_event > 5.0:
            self.cancel(watcher)

    def mouse(self, key, cols, watcher, header_visible):
        """Handle one mouse report; return True when it belonged to the progress bar."""
        match = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", key)
        if not match:
            return False
        button, x, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        event = match.group(4)

        if event == "m":                                     # button released
            if self.dragging and button == 0:
                watcher.seek_preview = self._target(x)       # where the button was actually let go
                self.cancel(watcher, commit=True)
                return True
            return False

        if button == 0:                                      # left button pressed
            track = watcher.track
            if not header_visible or y != self.PROGRESS_ROW or not track:
                return False
            duration = float(track[3] or 0.0)
            elapsed_text = format_time(watcher.position())
            total_text = format_time(duration) if duration > 0 else "--:--"
            _pad, width, first = progress_layout(cols, elapsed_text, total_text)
            if not (first - 1 <= x <= first + width):        # a cell of slack on each side
                return False
            if duration <= 0:
                self.message = "This player doesn't report the song length, so seeking isn't available."
                return True
            self.dragging = True
            self.bar = (first, width, duration)
            self.last_event = time.monotonic()
            watcher.seek_preview = self._target(x)
            return True

        if button == 32 and self.dragging:                   # mouse moved with the button held
            self.last_event = time.monotonic()
            watcher.seek_preview = self._target(x)
            return True
        return False


# --------------------------------------------------------------- render --
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


def render_mirror(vals, colors, rows, pulse):
    """Render a mirrored spectrum: each frequency bar extends both ways from center."""
    flash = colors + (255 - colors) * (pulse * SETTINGS["beat_flash"])
    q = (np.clip(flash, 0, 255).astype(int) >> 3) << 3
    prefixes = [f"\x1b[38;2;{r};{g};{b}m" for r, g, b in q]

    width = SETTINGS["bar_width"]
    gap = " " * SETTINGS["gap"]
    center_top = (rows - 1) // 2
    center_bottom = rows // 2
    max_distance = max(center_top, rows - 1 - center_bottom)
    max_extent = max_distance + 1

    output = []
    for row in range(rows):
        distance = min(abs(row - center_top), abs(row - center_bottom))
        parts = []
        for i, value in enumerate(vals):
            height = int(np.ceil(float(np.clip(value, 0.0, 1.0)) * max_extent))
            if distance < height:
                parts.append(prefixes[i] + "█" * width + "\x1b[0m" + gap)
            else:
                parts.append(" " * width + gap)
        output.append("".join(parts) + "\x1b[K")

    return output


# ------------------------------------------------------------ reflection --
# Water reflection for the Bars visualization. The bars themselves are still drawn
# by render_bars() exactly as before; this only adds a faded, gently rippling mirror
# image in the rows beneath them. It is computed with a handful of numpy operations
# per frame and draws each row as a few runs of identical cells, so it stays cheap.
REFLECTION_MIN_ROWS = 8          # shorter bar areas skip the reflection entirely
REFLECTION_CHARS = " ▔▀█"         # how much of a reflected cell is filled, from the top
_reflection_layout_cache = {"key": None, "layout": None}
_color_prefix_cache = {}


def reflection_split(rows):
    """Split the bar area into (bar_rows, reflection_rows), or None if it is too short."""
    if rows < REFLECTION_MIN_ROWS:
        return None
    length = SETTINGS["reflection_length"]
    reflection_rows = int(round(rows * length / (1.0 + length)))
    reflection_rows = max(2, min(rows // 2, reflection_rows))
    return rows - reflection_rows, reflection_rows


def _reflection_layout(nbars, width, gap):
    """Which bar each terminal column belongs to (-1 for gaps); cached per layout."""
    key = (nbars, width, gap)
    cache = _reflection_layout_cache
    if cache["key"] != key:
        stride = width + gap
        cols = np.arange(nbars * stride - gap)
        cache["layout"] = np.where(cols % stride < width, cols // stride, -1)
        cache["key"] = key
    return cache["layout"]


def _color_prefix(code):
    prefix = _color_prefix_cache.get(code)
    if prefix is None:
        r, g, b = ((code >> 10) & 31) << 3, ((code >> 5) & 31) << 3, (code & 31) << 3
        prefix = f"\x1b[38;2;{r};{g};{b}m"
        if len(_color_prefix_cache) < 40000:
            _color_prefix_cache[code] = prefix
    return prefix


def render_reflection(vals, colors, bar_rows, reflection_rows, pulse):
    """Return `reflection_rows` lines: a faded, rippling mirror image of the bars."""
    strength = SETTINGS["reflection_strength"]
    distortion = SETTINGS["water_distortion"]
    layout = _reflection_layout(len(vals), SETTINGS["bar_width"], SETTINGS["gap"])
    ncols = layout.shape[0]

    k = np.arange(reflection_rows)
    depth = (k + 0.5) / reflection_rows              # 0 at the water line, 1 at the far end
    fade = strength * (1.0 - depth) ** 1.5           # brightest at the surface, fading with depth

    # Ripple: each reflected row slides sideways by a small, slowly moving amount that
    # grows with depth, so it is calm at the surface and wobblier further down.
    if distortion > 0.0:
        t = time.monotonic()
        amplitude = distortion * (0.35 + 1.65 * depth)
        wave = (0.7 * np.sin(6.2832 * (0.23 * k - 0.85 * t))
                + 0.3 * np.sin(6.2832 * (0.61 * k + 0.50 * t + 0.3)))
        shift = np.rint(amplitude * wave).astype(np.int64)
    else:
        shift = np.zeros(reflection_rows, dtype=np.int64)

    source = np.clip(np.arange(ncols)[None, :] - shift[:, None], 0, ncols - 1)
    bar_index = layout[source]                       # (rows, columns)
    solid = bar_index >= 0
    safe = np.where(solid, bar_index, 0)

    # Same height scale as the real bars, so the reflection mirrors them exactly.
    heights = np.clip(vals, 0.0, 1.0) * bar_rows
    cover = np.clip(heights[safe] - k[:, None], 0.0, 1.0) * solid
    level = ((cover >= 0.0625).astype(np.int64) + (cover >= 0.3125) + (cover >= 0.75))
    level[fade < 0.03] = 0                           # too faint to be worth drawing

    # Same colors as the bars (album art, presets, beat flash), dimmed by the fade.
    flash = colors + (255 - colors) * (pulse * SETTINGS["beat_flash"])
    rgb = np.clip(flash, 0, 255)[safe] * fade[:, None, None]
    q = rgb.astype(np.int64) >> 3
    code = (q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2]
    key = np.where(level > 0, code * 4 + level, 0)

    lines = []
    for r in range(reflection_rows):
        row = key[r]
        if not row.any():
            lines.append("\x1b[K")
            continue
        cuts = (np.flatnonzero(row[1:] != row[:-1]) + 1).tolist()
        parts = []
        start = 0
        for end in cuts + [ncols]:
            value = int(row[start])
            if value == 0:
                parts.append(" " * (end - start))
            else:
                color_code, lvl = divmod(value, 4)
                parts.append(_color_prefix(color_code) + REFLECTION_CHARS[lvl] * (end - start))
            start = end
        lines.append("".join(parts) + "\x1b[0m\x1b[K")
    return lines


def render(vals, colors, rows, pulse, status="", extra=None, header=None):
    """Compose the selected spectrum style with the shared header and lyrics."""
    lines = list(header) if header else []
    if SETTINGS["visualization"] == "Mirror":
        lines.extend(render_mirror(vals, colors, rows, pulse))
    else:
        split = reflection_split(rows) if SETTINGS["reflection"] else None
        if split:
            bar_rows, reflection_rows = split
            lines.extend(render_bars(vals, colors, bar_rows, pulse))
            try:
                lines.extend(render_reflection(vals, colors, bar_rows, reflection_rows, pulse))
            except Exception:    # a cosmetic effect must never take the visualizer down
                lines.extend(["\x1b[K"] * reflection_rows)
        else:
            lines.extend(render_bars(vals, colors, rows, pulse))
    if extra:
        lines.extend(extra)
    if status:
        lines.append(status + "\x1b[0m\x1b[K")
    return "\x1b[H" + "\n".join(lines)



def status_line(watcher):
    sw = "".join(f"\x1b[48;2;{r};{g};{b}m    " for r, g, b in watcher.palette)
    return f"{sw}\x1b[0m {str(watcher.last_url)[-60:]}"


# ------------------------------------------------------------------- main --
def main():
    # DSP worker mode: stdin/stdout PCM, no terminal required. Checked before
    # the tty guard so the pipeline can run headless.
    if "--dsp-worker" in sys.argv:
        dsp_worker_main()
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit(
            "Artcava needs a real terminal (not an IDE console). "
            "Open a terminal and run: python3 Artcava.py"
        )

    load_settings()
    decoration_renderer = DecorationRenderer()
    original_opacity = kitty_configured_opacity()
    in_kitty = kitty_available()

    # Audio DSP: start before the visualizer so the first frames already
    # tap processed audio. Failure here never blocks the visualizer.
    audio_dsp = AudioDSPManager()
    if SETTINGS.get("audio_enabled"):
        if not audio_dsp.start():
            SETTINGS["audio_enabled"] = False
            save_settings()
            print(f"Artcava: Audio DSP disabled: {audio_dsp.last_error}", file=sys.stderr)

    # Check GitHub in the background. This never blocks startup or playback.
    update_checker = UpdateChecker(os.path.realpath(__file__))
    update_checker.start()
    update_ui_state = {"dismissed": False}
    seekbar = SeekBar()

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv
    forced = args[0] if args else None

    watcher = ArtWatcher(forced)
    watcher.start()
    lyrics = LyricsWatcher()
    lyrics.start()

    try:
        reader = AudioReader(monitor=audio_dsp.expected_monitor())
    except FileNotFoundError:
        audio_dsp.stop()
        sys.exit("parec not found - install pulseaudio-utils (works with PipeWire too).")
    except OSError as exc:
        audio_dsp.stop()
        sys.exit(f"Could not start audio capture: {exc}")
    reader.start()

    size = None
    analyzer = None
    old_tty = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())

    current = None
    accent = (105, 190, 255)          # lighter shade of the album color, updated every frame
    last = time.perf_counter()
    next_t = last
    quit_requested = False
    action_message = ""
    action_message_until = 0.0

    if in_kitty:
        kitty_opacity(SETTINGS["opacity"])

    sys.stdout.write(
        "\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[2J"
    )
    sys.stdout.flush()

    try:
        while not quit_requested:
            now = time.perf_counter()
            dt = min(0.05, max(1e-3, now - last))
            last = now

            for key in read_keys(0):
                cols, lines = shutil.get_terminal_size()

                notice_action = None
                if not update_ui_state["dismissed"]:
                    notice_action = update_notice_mouse_action(key, cols, lines)

                if notice_action == "update" or (
                    notice_action is None and key in ("u", "U")
                    and update_checker.snapshot()[1] and not update_ui_state["dismissed"]
                ):
                    action_message = open_update_page()
                    action_message_until = time.monotonic() + 5.0
                    update_ui_state["dismissed"] = True
                    continue

                if notice_action == "later" or (
                    notice_action is None and key in ("x", "X")
                    and update_checker.snapshot()[1] and not update_ui_state["dismissed"]
                ):
                    update_ui_state["dismissed"] = True
                    action_message = "Update reminder dismissed. Check again in Settings."
                    action_message_until = time.monotonic() + 4.0
                    continue

                if decoration_renderer.mouse(key, cols, lines):
                    continue

                if seekbar.mouse(key, cols, lyrics, SETTINGS["show_title"] and lines >= 12):
                    continue

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
                    seekbar.cancel(lyrics)
                    settings_menu(update_checker, update_ui_state, decoration_renderer,
                                  accent=accent, palette=watcher.palette, audio_dsp=audio_dsp)
                    # DSP may have been toggled in Settings: re-tap the correct
                    # monitor so the visualizer follows processed/unprocessed audio.
                    expected = audio_dsp.expected_monitor()
                    if reader.monitor != expected:
                        try:
                            reader.proc.terminate()
                        except Exception:
                            pass
                        try:
                            reader = AudioReader(monitor=expected)
                            reader.start()
                        except OSError:
                            pass
                    decoration_renderer.invalidate()
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
                    decoration_renderer.invalidate()
                elif key in ("t", "T"):
                    SETTINGS["show_title"] = not SETTINGS["show_title"]
                    save_settings()
                    sys.stdout.write("\x1b[2J")
                    decoration_renderer.invalidate()
                elif key in ("[", "]"):
                    step = -0.25 if key == "[" else 0.25
                    SETTINGS["lyrics_offset"] = round(
                        max(-5.0, min(5.0, SETTINGS["lyrics_offset"] + step)), 2
                    )
                    save_settings()

            if quit_requested:
                break

            if audio_dsp.status == "error" and not audio_dsp.is_active() and SETTINGS.get("audio_enabled"):
                SETTINGS["audio_enabled"] = False
                save_settings()
                action_message = "Audio processing stopped: " + audio_dsp.last_error
                action_message_until = time.monotonic() + 6.0
                # Return the analyzer to normal routing after automatic rollback.
                if reader.monitor != audio_dsp.expected_monitor():
                    try:
                        reader.proc.terminate()
                        reader = AudioReader(monitor=audio_dsp.expected_monitor())
                        reader.start()
                    except OSError:
                        pass

            seekbar.tick(lyrics)
            if lyrics.seek_failed:
                lyrics.seek_failed = False
                seekbar.message = "This player doesn't support seeking."
            if seekbar.message:
                action_message = seekbar.message
                action_message_until = time.monotonic() + 4.0
                seekbar.message = ""

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
                decoration_renderer.invalidate()

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
            extra = lyric_block(lyrics, cols, accent, analyzer.pulse) if show_lyrics else None

            frame = render(
                vals, current, rows, analyzer.pulse,
                status_line(watcher) if debug else "",
                extra, header,
            )

            if action_message and time.monotonic() < action_message_until:
                # A short-lived feedback line appears near the bottom after a click.
                feedback = fit(action_message, max(1, cols - 2))
                frame += f"\x1b[{max(1, lines - 1)};2H\x1b[38;2;170;210;255m{feedback}\x1b[0m"
            elif update_checker.snapshot()[1] and not update_ui_state["dismissed"]:
                frame += update_notice_overlay(cols, lines)

            sys.stdout.write(frame)
            decoration_renderer.draw(cols, lines)
            sys.stdout.flush()

            next_t += 1 / SETTINGS["fps"]
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
        # Restore original audio routing before leaving the alternate screen.
        try:
            audio_dsp.stop()
        except Exception:
            pass
        save_settings()
        decoration_renderer.clear()
        if in_kitty:
            restore_kitty_opacity(original_opacity)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        sys.stdout.write(
            "\x1b[?1006l\x1b[?1002l\x1b[?1000l\x1b[?25h\x1b[?1049l"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()

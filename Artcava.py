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

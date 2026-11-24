## Requirements

### Python requirements
- **Python 3** – Runs Artcava.
- **NumPy** – Handles audio frequency analysis and FFT processing.
- **Pillow** – Processes album artwork and extracts colors.

### System dependencies
- **playerctl** – Reads currently playing music metadata and controls playback through MPRIS.
- **parec** – Captures system audio through PulseAudio or PipeWire's PulseAudio compatibility layer.
- **Kitty** – Required for terminal opacity features and Kitty-specific integrations.

### Installation

#### 1. Debian, Linux Mint, Ubuntu
```bash
sudo apt install python3 python3-numpy python3-pil playerctl pulseaudio-utils
```

#### 2. Arch Linux, Manjaro, EndeavourOS
```bash
sudo pacman -S python python-numpy python-pillow playerctl libpulse
```

#### 3. Fedora
```bash
sudo dnf install python3 python3-numpy python3-pillow playerctl pulseaudio-utils
```

#### 4. openSUSE
```bash
sudo zypper install python3 python3-numpy python3-Pillow playerctl pulseaudio-utils
```

### Optional dependencies
- **wl-clipboard** – Clipboard support on Wayland.
- **xclip** or **xsel** – Clipboard support on X11.

# Artcava (art-KAH-vuh)

**A Linux terminal audio visualizer that dynamically matches its colors to your currently playing album artwork.**

Artcava is a Python-based terminal audio visualizer inspired by CAVA. It captures system audio, analyzes frequencies in real time, and extracts colors from your currently playing music's album artwork to create a visualizer that matches your music.

Designed for the **Kitty terminal**.

## Features

- **Real-time audio visualization** — Displays audio frequencies as animated bars.
- **Dynamic album artwork colors** — Extracts colors from your currently playing album artwork.
- **FFT audio analysis** — Uses NumPy to analyze audio frequencies.
- **Smooth animations** — Adjustable release smoothing and sensitivity.
- **Color presets** — Choose from multiple built-in color themes.
- **Settings menu** — Customize your visualizer directly from the terminal.
- **Beat flash** — Optional visual effects that respond to music.
- **Saved settings** — Your preferences are saved automatically.
- **True-color support** — Uses ANSI true color for terminal visuals.

## Quick Install

**Requirements:**
- Linux
- Kitty terminal installed
- An MPRIS-compatible music player

Copy and paste this command into your terminal:

```bash
bash -c 'command -v kitty >/dev/null 2>&1 || { echo "Error: Kitty must be installed before continuing."; exit 1; }; . /etc/os-release; case "$ID" in debian|ubuntu|linuxmint|pop|elementary|zorin) sudo apt update && sudo apt install -y git python3 python3-numpy python3-pil playerctl pulseaudio-utils;; arch|manjaro|endeavouros|omarchy) sudo pacman -Syu --needed --noconfirm git python python-numpy python-pillow playerctl libpulse;; fedora) sudo dnf install -y git python3 python3-numpy python3-pillow playerctl pulseaudio-utils;; opensuse*) sudo zypper --non-interactive install git python3 python3-numpy python3-Pillow playerctl pulseaudio-utils;; *) echo "Unsupported distribution: $ID"; exit 1;; esac && mkdir -p "$HOME/.local/share/artcava" "$HOME/.local/share/applications" && if [ -d "$HOME/.local/share/artcava/.git" ]; then git -C "$HOME/.local/share/artcava" pull --ff-only; else git clone https://github.com/kbhankins2012-crypto/Artcava.git "$HOME/.local/share/artcava"; fi && chmod +x "$HOME/.local/share/artcava/Artcava.py" && printf "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Artcava\nComment=Terminal audio visualizer colored by album artwork\nExec=kitty --hold python3 \"%s\"\nIcon=audio-x-generic\nTerminal=false\nCategories=AudioVideo;Audio;\n" "$HOME/.local/share/artcava/Artcava.py" > "$HOME/.local/share/applications/artcava.desktop" && echo "Artcava installed successfully!"'
```

The installer checks for Kitty, installs the required packages for supported distributions, downloads Artcava, and creates a desktop application launcher.

## Manual Installation

Prefer installing everything yourself?

See [INSTALL.md](INSTALL.md) for manual installation instructions.

## Supported Distributions

The installer currently includes package-installation commands for:

- Debian
- Arch Linux
- Fedora
- Linux Mint
- Ubuntu
- openSUSE
- Manjaro
- EndeavourOS
- Omarchy

Other Linux distributions may work if their dependencies are available.

*Not every distribution has been tested.*

## Requirements

- Linux
- Python 3
- Kitty terminal
- NumPy
- Pillow
- playerctl
- PulseAudio utilities (`parec`)
- An MPRIS-compatible music player

## Running Artcava

After installation, you can launch Artcava from your desktop application menu.

To run it manually:

```bash
kitty --hold python3 Artcava.py
```

## Controls

| Key | Action |
|---|---|
| S | Open or close settings |
| Up / Down | Navigate settings |
| Left / Right | Adjust settings |
| Enter | Change or select an option |
| Esc | Close settings |
| Q | Quit Artcava |

## Color Presets

Artcava includes several color presets:

- Album Art
- Ocean
- Fire
- Forest
- Purple
- Pink
- Red
- Green
- Blue
- White

The Album Art preset automatically extracts colors from your currently playing album artwork.

## Configuration

Your settings are automatically saved here:

```text
~/.config/artcava/settings.json
```

You can customize your visualizer without manually editing configuration files.

## Dependencies

Artcava uses:

- **NumPy** — Audio frequency analysis
- **Pillow** — Image processing and color extraction
- **playerctl** — Music player information and album artwork
- **parec** — System audio capture

## Contributing

Contributions, suggestions, and bug reports are welcome!

Feel free to open an issue or submit a pull request on GitHub.

## License

Artcava is licensed under the MIT License. See [LICENSE](LICENSE) for details.

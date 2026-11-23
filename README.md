# Artcava
A terminal-based audio visualizer that dynamically matches its colors to your currently playing album artwork.
# Artcava

**A Linux terminal audio visualizer that dynamically matches its colors to your currently playing album artwork.**

Artcava is a Python-based audio visualizer inspired by CAVA. It captures system audio, analyzes its frequencies, and uses colors extracted from album artwork to create a dynamic terminal visualization.

Designed to run inside the Kitty terminal.
## Quick Install

**Before you begin:** Kitty terminal must already be installed on your Linux system.

Copy and paste the following command into your Kitty terminal:

```bash
bash -c 'command -v kitty >/dev/null 2>&1 || { echo "Error: Kitty must be installed before continuing."; exit 1; }; . /etc/os-release; case "$ID" in debian|ubuntu|linuxmint|pop|elementary|zorin) sudo apt update && sudo apt install -y git python3 python3-numpy python3-pil playerctl pulseaudio-utils;; arch|manjaro|endeavouros|omarchy) sudo pacman -Syu --needed --noconfirm git python python-numpy python-pillow playerctl libpulse;; fedora) sudo dnf install -y git python3 python3-numpy python3-pillow playerctl pulseaudio-utils;; opensuse*) sudo zypper --non-interactive install git python3 python3-numpy python3-Pillow playerctl pulseaudio-utils;; *) echo "Unsupported distribution: $ID"; exit 1;; esac && mkdir -p "$HOME/.local/share/artcava" "$HOME/.local/share/applications" && if [ -d "$HOME/.local/share/artcava/.git" ]; then git -C "$HOME/.local/share/artcava" pull --ff-only; else git clone https://github.com/kbhankins2012-crypto/Artcava.git "$HOME/.local/share/artcava"; fi && chmod +x "$HOME/.local/share/artcava/Artcava.py" && printf "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Artcava\nComment=Terminal audio visualizer colored by album artwork\nExec=kitty --hold python3 \"%s\"\nIcon=audio-x-generic\nTerminal=false\nCategories=AudioVideo;Audio;\n" "$HOME/.local/share/artcava/Artcava.py" > "$HOME/.local/share/applications/artcava.desktop" && echo "Artcava installed successfully!"'
```

For manual installation, including installing Kitty, see [INSTALL.md](INSTALL.md).
## Features

- Real-time audio visualization
- Dynamic album artwork color extraction
- FFT-based audio analysis
- Smooth bar animations
- Multiple color presets
- Customizable visualization settings
- Beat flash effects
- Settings menu accessible while the visualizer is running
- Settings automatically saved between sessions
- True-color ANSI terminal rendering

## Screenshots

Screenshots coming soon!

## Supported Platforms

Artcava is designed for Linux distributions that support its required dependencies.

Expected compatibility includes:

- Debian
- Arch Linux
- Fedora
- Linux Mint
- Ubuntu
- openSUSE
- Manjaro
- EndeavourOS
- Other compatible Linux distributions

Not every distribution has been individually tested.

## Requirements

- Linux
- Python 3
- Kitty terminal
- NumPy
- Pillow
- playerctl
- PulseAudio utilities (`parec`)
- An MPRIS-compatible music player

## Installation

See **[INSTALL.md](INSTALL.md)** for installation instructions.

The installation guide includes distribution-specific commands for installing dependencies and setting up Artcava.

## Running Artcava

After installing the dependencies and downloading the repository, run:

```bash
kitty --hold python3 Artcava.py
```

## Controls

| Key | Action |
|---|---|
| S | Open settings |
| Q | Quit |
| Up / Down | Navigate settings |
| Left / Right | Adjust settings |
| Enter | Change setting |
| Esc | Close settings |
| S | Close settings |

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

## Configuration

Artcava automatically saves settings to:

```text
~/.config/artcava/settings.json
```

Your settings are preserved between sessions.

## Dependencies

Python dependencies are listed in `requirements.txt`.

System dependencies, including Kitty and audio utilities, must be installed separately. See `INSTALL.md`.

## Contributing

Contributions, suggestions, and bug reports are welcome!

Feel free to fork the repository, make improvements, and submit a pull request.

## License

Artcava is distributed under the MIT License. See [LICENSE](LICENSE) for details.

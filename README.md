# Artcava
A terminal-based audio visualizer that dynamically matches its colors to your currently playing album artwork.
# Artcava

**A Linux terminal audio visualizer that dynamically matches its colors to your currently playing album artwork.**

Artcava is a Python-based audio visualizer inspired by CAVA. It captures system audio, analyzes its frequencies, and uses colors extracted from album artwork to create a dynamic terminal visualization.

Designed to run inside the Kitty terminal.

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

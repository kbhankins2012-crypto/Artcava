
# Artcava (art-KAH-vuh)

**A Linux terminal audio visualizer that dynamically matches its colors to your currently playing album artwork.**

Artcava is a Python-based terminal audio visualizer inspired by CAVA. It captures system audio, analyzes frequencies in real time, and extracts colors from your currently playing music's album artwork to create a visualizer that matches your music.

Designed for the **Kitty terminal**.

## Preview

<img width="1904" height="1003" alt="Screenshot From 2026-11-23 14-04-17" src="https://github.com/user-attachments/assets/0c04c57f-3c6d-4886-aadf-4cd545364146" />

**Disclaimer:** Not every album cover is gonna turn into colors perfectly. Some covers might look a little different in the visualizer, and that's completely normal. Artcava does its best to match the colors, but the results won't always be exact.


## Features

- **Real-time audio visualization:** Displays live audio frequencies in a CAVA-style bar visualizer.
- **Dynamic album-art colors:** Automatically extracts colors from the currently playing song's album artwork and applies them to the visualizer.
- **Synced lyrics:** Displays synchronized lyrics for the currently playing song using LRCLIB.
- **Adjustable lyric timing:** Fine-tune lyric synchronization using `[` and `]`.
- **Lyrics caching:** Saves fetched lyrics locally so they don't need to be downloaded again.
- **Custom color presets:** Choose from multiple built-in color themes or use album-art colors.
- **Beat flash:** Bars subtly react to bass and beats.
- **Customizable settings:** Adjust bar width, spacing, sensitivity, smoothing, colors, and lyrics.
- **Persistent configuration:** Your settings are saved automatically.
- **True-color terminal support:** Uses RGB colors for a more vibrant terminal experience.

### Additional Controls

| Key | Action |
|---|---|
| `L` | Toggle lyrics on/off |
| `[` | Shift lyrics timing backward |
| `]` | Shift lyrics timing forward |

## Kitty Transparency Setup

Artcava's opacity control uses Kitty's remote-control feature. To enable it, add two settings to your Kitty configuration before using the opacity control.

**1. Open a terminal and create or open Kitty's configuration file:**

```bash
mkdir -p ~/.config/kitty
nano ~/.config/kitty/kitty.conf
```

**2. Add these two lines to the file:**

```conf
allow_remote_control yes
dynamic_background_opacity yes
```

**3. Save the configuration:**

- Press `Ctrl + O` to save.
- Press `Enter` to confirm.
- Press `Ctrl + X` to exit.

Restart Kitty after saving the file. The opacity control will then be able to adjust Kitty's background opacity while Artcava is running. Artcava's other features can still be used without enabling this setting.


### Lyrics

Artcava retrieves synchronized lyrics from [LRCLIB](https://lrclib.net).

Lyrics are cached locally in `~/.cache/artcava/lyrics/` so previously fetched lyrics don't need to be downloaded again.

Lyrics can be enabled or disabled in the settings menu, and their timing can be adjusted whenever needed.

### Updating

Already have Artcava installed? Simply run the Quick Install command again to download the latest version.

Your saved settings are preserved when updating.

## Quick Install

Copy and paste this command into your terminal:

```bash
bash -c '
set -e
D="$HOME/.local/share/artcava"
. /etc/os-release
case " $ID $ID_LIKE " in
  *" debian "*|*" ubuntu "*)
    sudo apt update && sudo apt install -y git kitty python3 python3-numpy python3-pil playerctl pulseaudio-utils ;;
  *" arch "*)
    sudo pacman -Syu --needed --noconfirm git kitty python python-numpy python-pillow playerctl libpulse ;;
  *" fedora "*|*" rhel "*)
    sudo dnf install -y git kitty python3 python3-numpy python3-pillow playerctl pulseaudio-utils ;;
  *" suse "*|*" opensuse "*)
    sudo zypper --non-interactive install git kitty python3 python3-numpy python3-Pillow playerctl pulseaudio-utils ;;
  *)
    echo "Unsupported distribution: $ID"; exit 1 ;;
esac
if [ -d "$D/.git" ]; then
  git -C "$D" fetch --depth 1 origin main && git -C "$D" reset --hard origin/main
else
  rm -rf "$D" && git clone --depth 1 https://github.com/kbhankins2012-crypto/Artcava.git "$D"
fi
chmod +x "$D/Artcava.py"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sf "$D/Artcava.py" "$HOME/.local/bin/artcava"
printf "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Artcava\nComment=Terminal audio visualizer colored by album artwork\nExec=kitty --hold python3 \"%s\"\nIcon=audio-x-generic\nTerminal=false\nCategories=AudioVideo;Audio;\n" "$D/Artcava.py" > "$HOME/.local/share/applications/artcava.desktop"
echo "Artcava is installed and up to date! Open it from your app menu, or type: artcava"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: log out and back in (or add ~/.local/bin to your PATH) to use the artcava command." ;; esac
'
```

The installer automatically detects your Linux distribution, installs the required dependencies (including Kitty), downloads or updates Artcava, creates a terminal command, and adds a desktop application launcher.

## Manual Installation

Prefer installing everything yourself?

See [Installing Artcava](https://github.com/kbhankins2012-crypto/Artcava/blob/main/Installing%20Artcava.md) for manual installation instructions.

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

After installation, you can launch Artcava from your desktop application menu or run:

```bash
artcava
```

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

Artcava is licensed under the MIT License. See [LICENSE](https://github.com/kbhankins2012-crypto/Artcava/blob/main/LICENSE) for details.

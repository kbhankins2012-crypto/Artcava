# Artcava — Installation Guide

Artcava is a Linux terminal audio visualizer built with Python. It uses album artwork to dynamically determine the colors of its visualizer bars.

Artcava is designed to run inside the Kitty terminal.

---

## 1. Requirements

Before installing Artcava, make sure you have:

- Linux
- Python 3
- Kitty terminal
- NumPy
- Pillow
- playerctl
- PulseAudio utilities (provides `parec`)
- An MPRIS-compatible music player

---

## 2. Debian

Open a terminal and run:

```bash
sudo apt update
sudo apt install kitty python3 python3-numpy python3-pil playerctl pulseaudio-utils
```

---

## 3. Arch Linux

Open a terminal and run:

```bash
sudo pacman -Syu
sudo pacman -S kitty python python-numpy python-pillow playerctl libpulse
```

---

## 4. Fedora

Open a terminal and run:

```bash
sudo dnf install kitty python3 python3-numpy python3-pillow playerctl pulseaudio-utils
```

---

## 5. Linux Mint

Open a terminal and run:

```bash
sudo apt update
sudo apt install kitty python3 python3-numpy python3-pil playerctl pulseaudio-utils
```

---

## 6. openSUSE

Open a terminal and run:

```bash
sudo zypper refresh
sudo zypper install kitty python3 python3-numpy python3-Pillow playerctl pulseaudio-utils
```

---

## 7. Manjaro

Open a terminal and run:

```bash
sudo pacman -Syu
sudo pacman -S kitty python python-numpy python-pillow playerctl libpulse
```

---

## 8. EndeavourOS

Open a terminal and run:

```bash
sudo pacman -Syu
sudo pacman -S kitty python python-numpy python-pillow playerctl libpulse
```

---

## 9. Downloading Artcava

Clone the repository:

```bash
git clone https://github.com/YOUR-USERNAME/Artcava.git
```

Enter the project directory:

```bash
cd Artcava
```

Alternatively, download the repository as a ZIP and extract it.

---

## 10. Making Artcava Executable

Run:

```bash
chmod +x Artcava.py
```

---

## 11. Launching Artcava

Run Artcava inside Kitty:

```bash
kitty --hold python3 Artcava.py
```

Artcava should now launch.

---

## 12. Creating an Application Menu Launcher

To launch Artcava from your desktop application menu, create a desktop entry:

```bash
mkdir -p ~/.local/share/applications
nano ~/.local/share/applications/artcava.desktop
```

Paste this:

```ini
[Desktop Entry]
Version=1.0
Type=Application
Name=Artcava
Comment=Terminal audio visualizer colored by album artwork
Exec=kitty --hold python3 /FULL/PATH/TO/Artcava/Artcava.py
Icon=audio-x-generic
Terminal=false
Categories=AudioVideo;Audio;
```

Replace `/FULL/PATH/TO/Artcava/Artcava.py` with the actual location of your Python file.

Save and exit.

Artcava should now appear in your desktop application menu.

---

## 13. Troubleshooting

**Kitty is not installed**

Make sure Kitty is installed using your distribution's package manager.

**Missing Python modules**

Make sure NumPy and Pillow are installed.

**Album artwork is not appearing**

Make sure:
- playerctl is installed.
- Your music player supports MPRIS.
- The music player is currently playing something.

**Audio capture is not working**

Make sure PulseAudio utilities are installed and that `parec` is available.

---

## 14. Compatibility

Artcava is intended to work across Linux distributions that provide its required dependencies.

Package names and installation commands may differ between distributions. Not every distribution or desktop environment has been individually tested.

If you encounter an issue, please consider opening a GitHub issue with your distribution name and error message.

---

## License

Artcava is open source and distributed under the MIT License.

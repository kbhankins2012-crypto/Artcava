## Requirements

### Python dependencies
- **Python 3**
- **NumPy** – Audio frequency analysis and FFT processing.
- **Pillow** – Album artwork processing and color extraction.

### System dependencies
- **playerctl** – Reads currently playing music information and controls playback through MPRIS.
- **pulseaudio-utils** – Provides `parec`, which Artcava uses to capture system audio.

### Optional dependencies
- **Kitty** – Enables live terminal background opacity control and Kitty clipboard integration.
- **wl-clipboard**, **xclip**, or **xsel** – Alternative clipboard utilities for copying update links outside Kitty.

### Debian installation

```bash
sudo apt install python3-numpy python3-pil playerctl pulseaudio-utils
```

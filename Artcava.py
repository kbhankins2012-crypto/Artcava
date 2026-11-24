                sm[d:] = np.maximum(sm[d:], v[:-d] * f)
                sm[:-d] = np.maximum(sm[:-d], v[d:] * f)
        v = np.clip(sm, 0.0, 1.0)

        # Smooth, frame-rate-independent attack and release.
        # Exponential release avoids the abrupt, uneven drop that can happen
        # when the terminal misses a frame.
        attack = 1.0 - np.exp(-dt / ATTACK_TIME)
        release = 1.0 - np.exp(-dt / SETTINGS["release_time"])
        rising = v > self.vals
        coeff = np.where(rising, attack, release)
        self.vals += (v - self.vals) * coeff

        bass = float(v[:self.bass_n].mean())
        hit = max(0.0, bass - self.bass_avg * 1.15)
        self.bass_avg += (bass - self.bass_avg) * min(1.0, dt * 6)
        self.pulse = max(self.pulse * 0.82 ** (dt * 60), min(1.0, hit * 5))
        return self.vals


# ------------------------------------------------------------------ render --
def render(vals, colors, rows, pulse, status=""):
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
        lines.append("".join(parts) + "\x1b[0m")
    if status:
        lines.append(status + "\x1b[0m\x1b[K")
    return "\x1b[H" + "\n".join(lines)


def status_line(watcher):
    sw = "".join(f"\x1b[48;2;{r};{g};{b}m    " for r, g, b in watcher.palette)
    return f"{sw}\x1b[0m {str(watcher.last_url)[-60:]}"


def main():
    load_settings()
    original_kitty_opacity = kitty_configured_opacity() if kitty_available() else None
    apply_opacity()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    debug = "--debug" in sys.argv
    forced = args[0] if args else None
    watcher = ArtWatcher(forced)
    watcher.start()
    try:
        reader = AudioReader()
    except FileNotFoundError:
        sys.exit("parec not found - install pulseaudio-utils (works with PipeWire too).")
    reader.start()

    size = None
    analyzer = None
    old_tty = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())
    current = None
    last = time.perf_counter()
    next_t = last

    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
    try:
        while True:
            now = time.perf_counter()
            dt = min(0.05, max(1e-3, now - last))
            last = now

            # Press S at any time to open the settings panel.
            if select.select([sys.stdin], [], [], 0)[0]:
                key = sys.stdin.read(1)
                if key in "sS":
                    settings_menu()
                    size = None  # force a clean redraw after leaving settings
                    next_t = time.perf_counter()
                    continue
                elif key in "qQ":
                    break
            cols, lines = shutil.get_terminal_size()
            if (cols, lines) != size:
                size = (cols, lines)
                nbars = max(1, (cols + SETTINGS["gap"]) // (SETTINGS["bar_width"] + SETTINGS["gap"]))
                analyzer = Analyzer(nbars)
                current = gradient(active_palette(watcher.palette), nbars)
                sys.stdout.write("\x1b[2J")

            target = gradient(active_palette(watcher.palette), analyzer.nbars)
            color_ease = 1.0 - np.exp(-dt / COLOR_EASE_TIME)
            current += (target - current) * color_ease

            vals = analyzer.update(reader.snapshot(), dt)
            rows = max(1, lines - (2 if debug else 1))
            sys.stdout.write(render(vals, current, rows, analyzer.pulse,
                                    status_line(watcher) if debug and SETTINGS["opacity"] >= 1.0 else ""))
            sys.stdout.flush()

            next_t += 1 / FPS                    # fixed schedule = even frame spacing
            spare = next_t - time.perf_counter()
            if spare > 0:
                time.sleep(spare)
            else:
                next_t = time.perf_counter()     # fell behind: resync instead of bursting
    except KeyboardInterrupt:
        pass
    finally:
        reader.proc.terminate()
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        if original_kitty_opacity is not None:
            kitty_opacity(original_kitty_opacity)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

"""Colour tokens for PESForge.

Every colour the editor draws comes from here. A palette names ten base colours (the four
shipped ones are the palettes explored in docs/mockups); the rest is derived, and the derived
tokens are blended until they clear the contrast they promise — quiet text is the dim body
colour pushed toward the background *only as far as* 4.5:1, and the ability bar's low fill
keeps 3:1 against the panel so a 45 does not read as an empty slot.

`tokens(name)` returns a flat dict; editor.py binds each key to a C_* module constant, so the
UI code keeps naming colours instead of carrying literals.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Base colours only. Everything else is computed, which is what keeps a new palette honest.
BASE = {
    "amber": {
        "bg": "#120e0a", "panel": "#1b1510", "panel2": "#251c14", "fg": "#f4ead9",
        "dim": "#ab927a", "accent": "#f0a63f", "green": "#7fd08b", "magenta": "#dd6f9f",
        "warn": "#e05d5d", "line": "#35281c",
    },
    "crimson": {
        "bg": "#110b0d", "panel": "#1b1115", "panel2": "#24161b", "fg": "#f4e8ec",
        "dim": "#b08a96", "accent": "#f0566c", "green": "#5ee0a0", "magenta": "#c26ae0",
        "warn": "#e8b04b", "line": "#35212a",
    },
    "ice": {
        "bg": "#080d13", "panel": "#0f1723", "panel2": "#162130", "fg": "#e6eefb",
        "dim": "#8399b3", "accent": "#58c8f0", "green": "#5ee0a0", "magenta": "#e070c0",
        "warn": "#e8b04b", "line": "#1f2f42",
    },
    "pitch": {
        "bg": "#0a100d", "panel": "#111a15", "panel2": "#17241c", "fg": "#e8f3ec",
        "dim": "#8fa998", "accent": "#63d99b", "green": "#a8e063", "magenta": "#e070b0",
        "warn": "#e8c05b", "line": "#21332a",
    },
}

# Settings offers these in this order; the first is the default.
CHOICES = (("amber", "Amber"), ("crimson", "Crimson"), ("ice", "Ice"), ("pitch", "Pitch"))
DEFAULT = CHOICES[0][0]

# tokens editor.py binds: contrast floor each one is blended to hold, against `bg` unless
# the tuple names another token as the ground.
FLOORS = {
    "hint": 4.5,          # shortcut hints, secondary captions
    "bar_low": 3.0,       # a low ability fill must still read as a fill (on panel2)
    "ghost": 3.0,         # the pre-edit tick on an ability bar
}


def choice(cfg=None):
    """The palette name in a config dict, falling back to the default for junk."""
    name = (cfg or {}).get("theme") if isinstance(cfg, dict) else cfg
    return name if name in BASE else DEFAULT


def _rgb(text):
    text = text.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in rgb)


def mix(a, b, t):
    """Blend `a` toward `b` by t (0..1) in sRGB."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def _linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color):
    r, g, b = _rgb(color)
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def toward(fg, ground, ratio):
    """`fg` pushed toward `ground` until their contrast is exactly `ratio` (or unchanged)."""
    if contrast(fg, ground) <= ratio:
        return fg
    lo, hi = 0.0, 1.0
    for _ in range(24):                    # contrast falls monotonically as fg approaches ground
        mid = (lo + hi) / 2
        if contrast(mix(fg, ground, mid), ground) > ratio:
            lo = mid
        else:
            hi = mid
    return mix(fg, ground, hi)


def luminance_side(fg, ground):
    """+1 when fg is lighter than ground, -1 when darker: which way 'brighter' lies."""
    return 1 if luminance(fg) >= luminance(ground) else -1


def away(fg, ground, ratio):
    """`fg` pushed away from `ground` until contrast clears `ratio` (or unchanged)."""
    if contrast(fg, ground) >= ratio:
        return fg
    target = WHITE if luminance_side(fg, ground) > 0 else BLACK
    out = fg
    for step in range(1, 101):
        out = mix(fg, target, step / 100.0)
        if contrast(out, ground) >= ratio:
            return out
    return out


def tokens(name=DEFAULT):
    """Flat token dict for one palette. Keys are the C_* names, lowercased."""
    base = BASE.get(name) or BASE[DEFAULT]
    bg, panel, panel2 = base["bg"], base["panel"], base["panel2"]
    accent, fg, dim = base["accent"], base["fg"], base["dim"]
    return {
        "bg": bg,
        "panel": panel,
        "panel2": panel2,
        "line": base["line"],
        "line_soft": toward(base["line"], bg, 1.6),      # a hairline that is still a line
        "hover": mix(panel2, fg, 0.07),
        "sel": mix(panel2, accent, 0.22),
        "ink": mix(bg, accent, 0.10),                    # text on an accent fill
        "fg": fg,
        "dim": dim,
        "hint": toward(dim, bg, FLOORS["hint"]),
        "accent": accent,
        "accent_hi": mix(accent, fg, 0.22),
        "accent_lo": mix(accent, bg, 0.32),
        "green": base["green"],
        "magenta": base["magenta"],
        "warn": base["warn"],
        "dirty": mix(panel2, accent, 0.18),              # field background for an edited value
        "bar": mix(panel2, fg, 0.10),                    # ability bar track
        "bar_low": away(mix(accent, bg, 0.45), panel2, FLOORS["bar_low"]),
        "ghost": away(mix(fg, bg, 0.55), bg, FLOORS["ghost"]),
        "focus": accent,
    }


TOKEN_NAMES = tuple(tokens().keys())

# the two poles `away()` blends toward; not palette colours, so kept apart from BASE
WHITE, BLACK = "#ffffff", "#000000"

if __name__ == "__main__":                             # a quick contrast report
    for name, label in CHOICES:
        t = tokens(name)
        print(f"{label:8s} fg/bg {contrast(t['fg'], t['bg']):5.2f}  "
              f"dim/bg {contrast(t['dim'], t['bg']):5.2f}  "
              f"hint/bg {contrast(t['hint'], t['bg']):5.2f}  "
              f"accent/bg {contrast(t['accent'], t['bg']):5.2f}  "
              f"bar_low/panel2 {contrast(t['bar_low'], t['panel2']):5.2f}  "
              f"ghost/bg {contrast(t['ghost'], t['bg']):5.2f}")

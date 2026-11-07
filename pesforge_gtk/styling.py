"""The stylesheet: `style.css` rendered from the theme tokens, and the light/dark choice.

The CSS lives in `style.css` next to this module (not in a Python string) and is read with
`importlib.resources`, so it resolves from the source tree and from inside the zipapp alike.
`$name` placeholders are theme tokens; `$tint('name', alpha)` renders an `rgba()` from one
(the sheet is CSS, so it has no colour maths of its own).
"""
import os
import re
from importlib import resources
from string import Template

import gi

from gi.repository import Gio, GLib, Gtk

import settings
import theme

TOK = theme.tokens("amber")     # the live token dict; install_css() updates it in place, so
                                # every module that imports TOK keeps reading the current one
_TINT = re.compile(r"\$tint\('([a-z0-9_]+)', ([0-9.]+)\)")


def tint(color, alpha):
    """An rgba() from a token's #rrggbb — the CSS has no colour maths of its own."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def css():
    """The stylesheet with the current tokens substituted in."""
    text = resources.files(__package__).joinpath("style.css").read_text(encoding="utf-8")
    text = _TINT.sub(lambda m: tint(TOK[m.group(1)], float(m.group(2))), text)
    return Template(text).substitute(TOK)


def system_theme():
    """'light' or 'dark' as the desktop asks for it.

    PESFORGE_THEME wins (tests, and a manual override); then the desktop's own answer — the GTK
    theme's name, the `color-scheme` GTK syncs from the freedesktop portal, then the portal
    directly — and dark when nothing answers. Reading only `gtk-application-prefer-dark-theme` was
    wrong: that property stays False even under Adwaita-dark, so a dark desktop was handed the
    light palette. The light palette paints dark ink, and the theme's dark value boxes then ate it
    whole: every spinbutton looked empty.
    """
    want = (os.environ.get("PESFORGE_THEME") or "").strip().lower()
    if not want:                                 # then a mode pinned in the settings menu
        want = str(settings.load().get("mode", "")).strip().lower()
    if want in ("light", "dark"):
        return want
    name = ""
    try:
        st = Gtk.Settings.get_default()
        if st is not None:
            name = str(st.get_property("gtk-theme-name") or "").lower()
            if name.endswith("dark") or st.get_property("gtk-application-prefer-dark-theme"):
                return "dark"
    except Exception:
        pass
    try:
        scheme = Gio.Settings.new("org.gnome.desktop.interface").get_string("color-scheme")
        if scheme in ("prefer-dark", "prefer-light"):
            return "dark" if scheme == "prefer-dark" else "light"
    except Exception:
        pass
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                            "org.freedesktop.portal.Settings", "Read",
                            GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                            None, Gio.DBusCallFlags.NONE, 1000, None)
        return {1: "dark", 2: "light"}.get(res.unpack()[0], "dark")
    except Exception:
        return "dark"


_PROVIDER = None


def install_css(display, mode=None):
    """(Re)load the stylesheet for the desktop's light/dark choice; safe to call again.

    One provider is kept and its data replaced, so following a theme change live cannot stack
    providers on the display.
    """
    global _PROVIDER
    TOK.update(theme.tokens(theme.choice(settings.load()), mode or system_theme()))
    if _PROVIDER is None:
        _PROVIDER = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, _PROVIDER,
                                                  # above the user's gtk.css (USER, 800): a themed
                                                  # desktop paints our fields with its own palette,
                                                  # and that is where mixed light/dark controls came
                                                  # from. The palette still follows the desktop's
                                                  # light/dark answer; its colours are ours.
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
    try:
        _PROVIDER.load_from_data(css().encode("utf-8"))
    except TypeError:                       # older bindings want (data, length)
        _PROVIDER.load_from_data(css(), -1)

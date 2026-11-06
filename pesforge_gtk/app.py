"""Entry: argument and environment handling, and the `Gtk.Application` itself.

`--check` reports what the app would open without a window. A normal start waits for the
user; `--db <cpk|folder>` opens straight into a database, `PESFORGE_GTK_SIZE=WxH` picks the
window size for a check, and `PESFORGE_GTK_SELFTEST=<mode>` schedules the self-check — the
only case in which `pesforge_gtk.selftest` is imported.
"""
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .importing import (  # noqa: E402
    EDITABLE, GAME, VINTAGES, db_arg, default_database, game_dir, vintage_database,
)
from .styling import install_css  # noqa: E402
from .window import PesForgeGtk  # noqa: E402


def check():
    """`--check`: report what the app would open, without opening a window."""
    print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
          f"PyGObject {'.'.join(str(v) for v in gi.version_info)}")
    missing = 0
    for vintage in EDITABLE:
        spec = VINTAGES[vintage]
        game = game_dir(vintage)
        source = vintage_database(vintage)
        copies = spec["module"].database_cpks(game) if game else []
        where = game or "not found (set " + spec["env"] + ")"
        print(f"{spec['label']:<9} install: {where}")
        print(f"          table  : {source or 'none found'}")
        if len(copies) > 1:
            print(f"          copies : {len(copies)} CPKs hold this release's tables "
                  f"({', '.join(os.path.basename(p) for p in copies[:4])}…)")
        missing += 1 if not source else 0
    return 1 if missing == len(EDITABLE) else 0


def main():
    if "--check" in sys.argv[1:]:
        return check()
    selftest = None
    if os.environ.get("PESFORGE_GTK_SELFTEST"):
        from . import selftest as _selftest   # a normal start never imports the checks
        selftest = _selftest
    # Gtk.Application is single-instance: a check run must not take over an app the user has open,
    # so it registers an id of its own.
    app_id = "dev.pesforge.gtk4"
    if selftest is not None:
        app_id += f".selftest{os.getpid()}"
    app = Gtk.Application(application_id=app_id)

    def on_activate(_app):
        display = Gdk.Display.get_default()
        if display is None:
            print("editor_gtk: no display to draw on (is DISPLAY/WAYLAND_DISPLAY set?)", flush=True)
            return
        install_css(display)
        win = PesForgeGtk(_app)                      # empty: nothing is read until it is asked
        # follow the desktop's light/dark switch while the app is open
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            def on_scheme_change(*_a):
                install_css(display)
                win.queue_draw()                     # the ability bars read the tokens when drawn
            gtk_settings.connect("notify::gtk-application-prefer-dark-theme", on_scheme_change)
        size = os.environ.get("PESFORGE_GTK_SIZE", "")   # WxH: lets a test pick the window size
        if "x" in size:
            try:
                win.set_default_size(*(int(n) for n in size.split("x", 1)))
            except ValueError:
                pass
        win.present()
        mode = os.environ.get("PESFORGE_GTK_SELFTEST", "").lower()
        # --db, and the checks that need data, open one; a normal start waits for the user
        want = db_arg() or (default_database(GAME) if mode in ("1", "2", "write", "fit") else "")
        if want:
            win.load_database(want)
        if mode:
            GLib.timeout_add(1200, win.selftest)     # after the layout has settled

    app.connect("activate", on_activate)      # without this the app shows nothing and exits
    status = app.run([sys.argv[0]])
    if status:
        return status
    return 1 if (selftest is not None and selftest.SELFTEST_FAILURES) else 0

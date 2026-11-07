"""The import dialog: drop or choose a CPK, see what it holds, then load it.

Version tabs are deliberately absent: detection reads the release out of the tables, so a
manual pick could only disagree with the file.
"""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .importing import NOT_READ, describe_pick  # noqa: E402


class ImportHub(Gtk.Window):
    """Drop or choose a CPK, see what it holds, then load it.

    Version tabs are deliberately absent: detection reads the release out of the tables,
    so a manual pick could only disagree with the file.
    """

    def __init__(self, app):
        super().__init__(title="Import database", transient_for=app, modal=True)
        self.app = app
        self.path = ""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for edge in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{edge}")(16)
        box.set_size_request(460, -1)
        self.set_child(box)

        zone = Gtk.Label(label="Drop a CPK, a PES 5 option file or a game folder here",
                         justify=Gtk.Justification.CENTER)
        zone.add_css_class("hint")
        zone.set_size_request(-1, 84)
        frame = Gtk.Frame()
        frame.set_child(zone)
        frame.add_controller(self._drop_target())
        box.append(frame)

        pick = Gtk.Box(spacing=8)
        choose = Gtk.Button(label="Choose file…")
        choose.connect("clicked", lambda _b: app.choose_database(self.show_path, folder=False))
        pick.append(choose)
        folder = Gtk.Button(label="Choose folder…")
        folder.set_tooltip_text("pick the game's own folder (or its Data/ or download/) and the "
                                "database inside it is found for you")
        folder.connect("clicked", lambda _b: app.choose_database(self.show_path, folder=True))
        pick.append(folder)
        box.append(pick)

        self.summary = Gtk.Label(label="nothing chosen yet", xalign=0, justify=Gtk.Justification.LEFT)
        self.summary.set_wrap(True)
        self.summary.set_max_width_chars(72)   # a wrapping label is still as wide as its text
        box.append(self.summary)
        note = Gtk.Label(label=NOT_READ, xalign=0, wrap=True)
        note.set_max_width_chars(72)
        note.add_css_class("hint")
        box.append(note)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self.load_btn = Gtk.Button(label="Load")
        self.load_btn.set_sensitive(False)
        self.load_btn.connect("clicked", lambda _b: self._load())
        buttons.append(cancel)
        buttons.append(self.load_btn)
        box.append(buttons)
        if app.source:                  # reopening: show what is loaded now
            self.show_path(app.source)

    def _drop_target(self):
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        return target

    def _on_drop(self, _target, value, _x, _y):
        files = value.get_files()
        path = files[0].get_path() if files else None
        if not path:
            return False
        self.show_path(path)
        return True

    def show_path(self, path):
        """A pick — file or folder — say what it holds, and what should have been picked.

        load_database() resolves the same way by itself, so the hub has to as well or it would
        be the less useful of the two.
        """
        self.path = path
        details, can_load, target = describe_pick(path)
        self.path = target or path
        self.load_btn.set_label(f"Load {os.path.basename(target)}"
                                if can_load and target and target != path else "Load")
        self.summary.set_text("\n".join(details))
        self.load_btn.set_sensitive(can_load)
        self.load_btn.set_tooltip_text("" if can_load else
                                       "this build edits PES 2021 / SP Football Life, PES 2019, "
                                       "PES 2015 and PES 5 databases")

    def _load(self):
        if self.path:
            self.app.load_database(self.path)
        self.close()

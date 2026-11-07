"""The window shell: header, footer, pages, import/unload, and the class assembly.

PesForgeGtk is assembled here from the mixins — the list, the editor panes, the batch tray,
the editing plumbing, the write flow and the layout policy — so the class has exactly one
definition site and each responsibility lives in its own module.

`do_size_allocate` must be defined on the class itself: PyGObject installs a `do_*` override
only when it is on the class, and a mixin copy is silently never called (see layout.py).
"""
import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

import settings  # noqa: E402
import theme  # noqa: E402

from .batchbar import BatchBarMixin  # noqa: E402
from .editing import EditingMixin  # noqa: E402
from .formpane import FormPaneMixin  # noqa: E402
from .hub import ImportHub  # noqa: E402
from .importing import (  # noqa: E402
    VINTAGES, find_database, import_start_folder, install_of, probe_database,
)
from .layout import LayoutMixin  # noqa: E402
from .listpane import ListPaneMixin, Row  # noqa: E402
from .reference import bulk_fields  # noqa: E402
from .saving import SavingMixin  # noqa: E402
from .styling import install_css  # noqa: E402
from .widgets import a11y, dim_label  # noqa: E402




class PesForgeGtk(BatchBarMixin, EditingMixin, FormPaneMixin, LayoutMixin, ListPaneMixin,
                  SavingMixin, Gtk.ApplicationWindow):
    def do_size_allocate(self, width, height, baseline):
        """Track the real window size: GTK4 has no size signal (the policy is in layout.py).

        This vfunc has to live on the class itself: PyGObject only installs a `do_*` override
        defined here, not one inherited from a mixin (see layout.py).
        """
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        self._size_panes(width, height)

    def __init__(self, app, table=None, table_source=None, teams=None, assign=None):
        """Starts with no database: nothing is read until the user imports one."""
        super().__init__(application=app, title="PESForge")
        self.set_default_size(1504, 1030)       # below ~1030 the panes stack and scroll
        self.table = table
        self.teams = teams
        self.assign = assign
        self.clubs = {}
        self.index = None
        self.source = table_source                       # full path: this is what Save writes
        self.source_name = os.path.basename(str(table_source)) if table_source else ""
        self.game = install_of(table_source) if table_source else None
        self.binding = False                    # guards the checkbox <-> selection loop
        self.provenance = ""                    # the file being edited, for the footer line
        self.last_folder = ""                   # where the last pick came from, for the chooser
        self.vintage = "pes2019"                # replaced by whatever is imported
        self.mod = VINTAGES["pes2019"]["module"]
        self.rows = []
        self.dirty = set()
        self.originals = {}                     # (index, key) -> the value the game holds
        self.undo_stack = []                    # steps: [(index, key, old, new), ...]
        self.redo_stack = []
        self.loading = True                     # until the first record is loaded
        self.packs = self.mod.database_cpks(self.game) if self.game else []
        self.assign_by_pid = {}
        if assign is not None:
            for i in range(len(assign)):
                self.assign_by_pid.setdefault(assign.player(i), []).append(i)
        self.connect("close-request", self._on_close_request)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        root.append(self._header())
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self._empty_state(), "empty")
        root.append(self.stack)
        root.append(self._batch_bar())
        root.append(self._footer(table_source))
        self._shortcuts()

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._matches, None)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorted = Gtk.SortListModel(model=self.filtered)
        # The selection is the *current row* (a click, the arrows); the bulk pick lives in
        # `checked` so a filter or a sort can never re-point a tick at another player.
        self.selection = Gtk.SingleSelection(model=self.sorted)
        self.selection.set_autoselect(False)
        self.selection.connect("selection-changed", self._on_select)
        self.checked = set()                     # table indexes the user ticked
        if self.table is not None:               # a caller handed us a database (tests)
            self._build_panes()
            self.column_view.set_model(self.selection)
            self.load_rows()
            self.populate()

    # ---- header
    def _settings_button(self):
        """The settings menu: the palette, the light/dark mode, and how many backups to keep.

        Only settings this build honours are here — each is read back when it is used, so
        writing the config and reloading the stylesheet is all it takes to apply one.
        """
        cfg = settings.load()
        popover = Gtk.Popover()
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        for m, side in ((10, "start"), (10, "end"), (10, "top"), (10, "bottom")):
            (grid.set_margin_start(m) if side == "start" else
             grid.set_margin_end(m) if side == "end" else
             grid.set_margin_top(m) if side == "top" else grid.set_margin_bottom(m))
        grid.attach(dim_label("Theme", 76, 12), 0, 0, 1, 1)
        mode = Gtk.DropDown.new_from_strings(("Follow desktop", "Dark", "Light"))
        mode.set_selected({"dark": 1, "light": 2}.get(str(cfg.get("mode", "")).lower(), 0))
        mode.set_hexpand(True)
        a11y(mode, "theme mode: follow the desktop, dark, or light")
        mode.connect("notify::selected", self._on_mode_changed)
        grid.attach(mode, 1, 0, 1, 1)
        grid.attach(dim_label("Palette", 76, 12), 0, 1, 1, 1)
        names = [name for name, _label in theme.CHOICES]
        palette = Gtk.DropDown.new_from_strings([label for _name, label in theme.CHOICES])
        palette.set_selected(names.index(theme.choice(cfg)))
        palette.set_hexpand(True)
        a11y(palette, "colour palette")
        palette.connect("notify::selected",
                        lambda dd, _p: self._set_config(theme=names[dd.get_selected()]))
        grid.attach(palette, 1, 1, 1, 1)
        grid.attach(dim_label("Backups", 76, 12), 0, 2, 1, 1)
        keep = Gtk.SpinButton.new_with_range(0, 200, 1)
        keep.set_value(int(cfg.get("max_backups", 50)))
        keep.set_tooltip_text("backups kept before the oldest is pruned; 0 keeps everything")
        keep.set_hexpand(False)
        keep.set_halign(Gtk.Align.START)
        a11y(keep, "backups to keep; 0 keeps everything")
        keep.connect("value-changed", lambda sb: self._set_config(max_backups=int(sb.get_value())))
        grid.attach(keep, 1, 2, 1, 1)
        popover.set_child(grid)
        button = Gtk.MenuButton(label="Settings")
        button.set_popover(popover)
        button.set_tooltip_text("palette, theme mode, backups")
        self.settings_button = button
        return button

    def _set_config(self, **changes):
        """Write settings, then reload the stylesheet — that is what applies them."""
        cfg = settings.load()
        cfg.update(changes)
        settings.save(cfg)
        install_css(self.get_display())
        self._status("settings saved · " + ", ".join(f"{k}={v}" for k, v in changes.items()))

    def _on_mode_changed(self, dd, _pspec):
        self._set_config(mode=("", "dark", "light")[dd.get_selected()])

    def _header(self):
        bar = Gtk.Box(spacing=10)
        bar.set_margin_top(10)
        bar.set_margin_bottom(2)
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        brand = Gtk.Label(label="PESForge")
        brand.add_css_class("brand")
        bar.append(brand)
        self.import_btn = Gtk.Button(label="Import database…")
        self.import_btn.set_tooltip_text("open a database CPK (nothing is loaded until you do)")
        self.import_btn.connect("clicked", lambda _b: self.import_database())
        bar.append(self.import_btn)
        self.unload_btn = Gtk.Button(label="Unload")
        self.unload_btn.set_tooltip_text("drop the loaded database and go back to the start")
        self.unload_btn.add_css_class("quiet")
        self.unload_btn.connect("clicked", lambda _b: self.unload_database())
        bar.append(self.unload_btn)
        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text("Search players, clubs, IDs…")
        a11y(self.search, "search players by name, shirt name, club or id")
        self.search.connect("search-changed", lambda _e: self._refilter())
        self.search.connect("activate", lambda _e: self._open_first_match())
        self.search.set_sensitive(False)                         # nothing loaded to search yet
        bar.append(self.search)
        bar.append(self._settings_button())
        return bar

    # ---- empty state: shown until a database is imported
    def _empty_state(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label(label="No database loaded")
        title.add_css_class("hero")
        box.append(title)
        hint = Gtk.Label(label="Import the file that carries the player tables — for example\n"
                               "PES 2019's Data/dt10_x64.cpk, PES 2015's Data/dt10.cpk, an "
                               "SP Football Life download/data_s*.cpk, or a classic option "
                               "file (KONAMI-WIN32PES5OPT / KONAMI-WIN32PES6OPT in Documents)",
                         justify=Gtk.Justification.CENTER)
        hint.add_css_class("hint")
        hint.set_wrap(True)                     # a long hint must not hold the window open
        hint.set_max_width_chars(60)
        box.append(hint)
        button = Gtk.Button(label="Import database…")
        button.add_css_class("accent")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda _b: self.import_database())
        box.append(button)
        version = Gtk.Label(label="reads PES 2021 / SP Football Life, PES 2019 and PES 2015 CPKs "
                                  "and the classic PES 5 / PES 6 option files · "
                                  "edits 124-, 180-, 188- and 312-byte player records")
        version.add_css_class("hint")
        version.set_wrap(True)
        version.set_max_width_chars(60)
        box.append(version)
        return box

    def _build_panes(self):
        """Build the list and the two editor panes into one page.

        One structure, and `_size_panes` only turns it: the list beside a stacked form+BIO, or —
        when the window is too narrow for the 586px the editor needs — the list as a strip on top
        with the editor using the whole width. Deciding that at build time meant deciding it from
        a size the window did not have yet (it opens at the remembered size and the compositor
        tiles it afterwards), which is how a 738px window ended up with a sliced-up form.
        """
        self.narrow = True                        # the abilities' compact grid: 3 columns, no bars
        self.roomy = getattr(self, "roomy", False)   # the BIO grid, when the window is wide enough
        self._paned = None                        # a fresh page re-applies its arrangement
        panes = Gtk.Box(spacing=8)
        panes.set_margin_start(8)
        panes.set_margin_end(8)
        panes.set_margin_bottom(6)
        panes.set_vexpand(True)
        self._panes = panes
        self._list_pane = self._list()
        self._form_pane = self._form()
        self._bio_pane = self._bio()
        self._editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._editor.set_hexpand(True)
        self._editor.append(self._form_pane)
        self._editor.append(self._bio_pane)
        panes.append(self._list_pane)
        panes.append(self._editor)
        if hasattr(self, "bulk_field"):            # the tray's fields differ per release
            self.bulk_field.set_model(Gtk.StringList.new(list(bulk_fields(self.mod))))
            self._update_bulk_range()
        old = self.stack.get_child_by_name("data")
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(panes, "data")
        self.stack.set_visible_child_name("data")

    def _footer(self, source):
        bar = Gtk.Box(spacing=8)
        bar.add_css_class("footer")
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        bar.set_margin_top(4)
        bar.set_margin_bottom(10)
        revert = Gtk.Button(label="Revert")
        revert.set_tooltip_text("drop the pending edits and re-read the database")
        revert.connect("clicked", lambda _b: self._reload())
        revert.set_sensitive(False)
        self.revert_btn = revert
        bar.append(revert)
        self.dirty_badge = Gtk.Label()
        self.dirty_badge.add_css_class("pill")
        self.dirty_badge.add_css_class("dirty")
        self.dirty_badge.set_visible(False)
        bar.append(self.dirty_badge)
        self.status = Gtk.Label(label=(f"{VINTAGES[self.vintage]['label']} · "
                                       f"{os.path.basename(str(source))}") if source
                                else "no database loaded · Import database… to pick one", xalign=0)
        self.status.add_css_class("hint")
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)   # a long path must not widen the window
        bar.append(self.status)
        self.save_btn = Gtk.Button(label="Save to game")
        self.save_btn.set_tooltip_text("write every pending edit into the database (Ctrl+S)")
        self.save_btn.add_css_class("accent")
        self.save_btn.connect("clicked", lambda _b: self._save())
        self.save_btn.set_sensitive(False)
        bar.append(self.save_btn)
        return bar

    def _update_badge(self):
        """The pending-edit badge: how many edits are waiting for Save."""
        if not hasattr(self, "dirty_badge"):
            return
        n = len(self.dirty)
        self.dirty_badge.set_text(f"{n} edited" if n else "")
        self.dirty_badge.set_visible(bool(n))
        self.dirty_badge.set_tooltip_text(f"{n} edit(s) not yet written — Save to game writes "
                                          f"them" if n else None)

    def _status(self, text):
        """Write the footer line. Everything it reports names the database it is about."""
        self.status.set_text(f"{self.provenance} · {text}" if self.provenance else text)

    def _shortcuts(self):
        """Ctrl+S / Ctrl+Z / Ctrl+Y / Ctrl+F / Ctrl+B / Escape: save, undo, redo, find,
        batch-apply, clear. (GTK 4 dropped Alt-underlines, so the keyboard route to everything
        is these accelerators plus Tab, which the focus-ring rules in style.css make visible.)"""
        ctrl = Gtk.ShortcutController()
        for spec, fn in (("<Control>s", lambda *_: (self._save(), True)[1]),
                         ("<Control>z", self._undo),
                         ("<Control>y", self._redo),
                         ("<Control><Shift>z", self._redo),
                         ("<Control>f", lambda *_: (self.search.grab_focus(), True)[1]),
                         ("<Control>b", lambda *_: (self._bulk_apply(), True)[1]),
                         ("Escape", lambda *_: (self.search.set_text(""), True)[1])):
            ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(spec),
                                               Gtk.CallbackAction.new(fn)))
        self.add_controller(ctrl)

    # ---- import / unload
    def selftest(self):
        """`PESFORGE_GTK_SELFTEST=...`: run the asked-for check (imported only when asked)."""
        from . import selftest
        return selftest.run(self)

    def unload_database(self, ask=True):
        """Drop the loaded database and go back to the empty window.

        With edits pending this asks first — discarding them is a real choice, not a status line.
        """
        if self.table is None:
            self._status("nothing loaded")
            return
        if self.dirty:
            if not ask:
                return
            dialog = Gtk.AlertDialog()
            dialog.set_modal(True)
            dialog.set_message(f"Discard {len(self.dirty)} unsaved edit(s) and unload?")
            dialog.set_detail("They are not written to the database until you press "
                              "Save to game.")
            dialog.set_buttons(["Keep editing", "Discard and unload"])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)

            def _done(d, result):
                try:
                    if d.choose_finish(result) == 1:
                        self.unload_database(ask=False)
                except GLib.Error:
                    pass
            dialog.choose(self, None, _done)
            return
        self.table = self.teams = self.assign = None
        self.source = self.source_name = ""
        self.game = None
        self.packs = []
        self.clubs, self.assign_by_pid = {}, {}
        self.rows, self.dirty = [], set()
        self.originals.clear()
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.originals.clear()
        self.index = None
        self.provenance = ""
        self.loading = True
        self.store.remove_all()
        self.stack.set_visible_child_name("empty")
        self.save_btn.set_sensitive(False)
        self.revert_btn.set_sensitive(False)
        self.search.set_sensitive(False)
        self.search.set_text("")
        self.status.set_text("no database loaded · Import database… to pick one")
        self._update_bulk()

    def import_database(self):
        """The import hub: drop or choose a CPK (or a game folder), then load it."""
        self.hub = ImportHub(self)
        self.hub.present()

    def choose_database(self, callback, folder=False):
        """A chooser that starts where you last picked: a CPK, or a folder to find one in."""
        chooser = Gtk.FileChooserNative(
            title="Import the folder holding the database CPK" if folder
            else "Import a database CPK",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN)
        if not folder:
            filters = Gtk.FileFilter()
            filters.set_name("PES databases (*.cpk, KONAMI-WIN32PES5OPT, KONAMI-WIN32PES6OPT)")
            filters.add_pattern("*.cpk")
            filters.add_pattern("KONAMI-WIN32PES5OPT")   # the classic era's single files
            filters.add_pattern("KONAMI-WIN32PES6OPT")
            chooser.add_filter(filters)
        chooser.set_current_folder(Gio.File.new_for_path(import_start_folder(self.last_folder)))

        def chosen(c, response):
            file = c.get_file() if response == Gtk.ResponseType.ACCEPT else None
            c.destroy()
            if file is not None and file.get_path():
                callback(file.get_path())

        chooser.connect("response", chosen)
        chooser.show()

    def load_database(self, path):
        """Probe a pick — CPK or folder — and load what it holds, or say why this build will not."""
        self.last_folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        name = os.path.basename(os.path.abspath(path))
        swapped = ""
        vintage, note = ("", "") if os.path.isdir(path) else probe_database(path)
        target = ""
        if vintage not in VINTAGES:                  # a folder pick, or a CPK with no tables
            # look where the database for this pick actually lives: the folder itself, then the
            # folder of the picked file, then the install the file sits in (SP Football Life
            # keeps its database in download/, one shelf up from Data/)
            for where in ((path,) if os.path.isdir(path) else
                          (os.path.dirname(os.path.abspath(path)), install_of(path))):
                target, vintage, note = find_database(where)
                if target and vintage in VINTAGES:
                    if not os.path.isdir(path):
                        swapped = name
                    path, name = target, os.path.basename(target)
                    break
        if vintage not in VINTAGES:
            where = f"{name} ({swapped} had no player table)" if swapped else name
            if target and os.path.basename(target) != name:
                where += f" → {os.path.basename(target)}"
            tail = ("" if vintage in ("pes2021", "no player table")
                    else " · this build edits PES 2021 / SP Football Life, PES 2019, PES 2015 "
                         "and the classic PES 5 / PES 6 option files.")
            self._status(f"{where}: {vintage} — {note}{tail}")
            return False
        mod = VINTAGES[vintage]["module"]
        try:                              # read everything before swapping: a failure keeps
            db = mod.Cpk(path)            # the database that is already loaded
            table = db.read_table("Player")
            teams = mod.TeamTable(*db.read_raw("Team"))
            assign = mod.AssignmentTable(*db.read_raw("PlayerAssignment"))
        except (KeyError, OSError, ValueError) as exc:
            why = f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}"
            self._status(f"{name}: cannot load ({why})")
            self._alert("Could not load the database", f"{name}\n{why}")
            return False
        self.vintage = vintage
        self.mod = mod
        self.table, self.teams, self.assign = table, teams, assign
        self._ovr_cache = {}                  # ratings belong to the database that was open
        self.source = path
        self.source_name = name
        self.game = install_of(path)
        self.packs = self.mod.database_cpks(self.game)
        self.clubs = {self.teams.id(i): self.teams.name(i) for i in range(len(self.teams))}
        self.assign_by_pid = {}
        for i in range(len(self.assign)):
            self.assign_by_pid.setdefault(self.assign.player(i), []).append(i)
        self.dirty.clear()
        self.originals.clear()
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.originals.clear()
        self.index = None
        self.rows = []
        self.store.remove_all()
        self._build_panes()
        self.column_view.set_model(self.selection)
        self.load_rows()
        self.populate()
        self.save_btn.set_sensitive(True)
        self.revert_btn.set_sensitive(True)
        self.search.set_sensitive(True)
        self.search.grab_focus()          # typing should land in the search, not on the floor
        self.provenance = name + (f" ({swapped} had no player table)" if swapped else "")
        self._status(f"{VINTAGES[vintage]['label']} · {len(self.rows):,} players · {note}"
                     + (" · EDIT00000000 is present and overrides this file in game"
                        if getattr(mod, "EDIT_SAVE", True) and settings.edit_save_files() else ""))
        return True


    BULK_OPS = (("Set to", "set"), ("Add", "add"), ("Subtract", "sub"))

    BIO_LABELS = ("Height", "Weight", "Age", "Nationality", "Form", "Weak Foot Usage",
                  "Weak Foot Accuracy", "Injury Resistance", "Reputation")

    BIO_RANGES = {"Height": (150, 215), "Weight": (55, 110), "Age": (15, 50),
                  "Nationality": (0, 511), "Form": (1, 8), "Weak Foot Usage": (1, 4),
                  "Weak Foot Accuracy": (1, 4), "Injury Resistance": (1, 4),
                  "Reputation": (1, 8)}

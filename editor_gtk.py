"""PESForge in GTK4 — the editor's window, on GTK4 + libadwaita.

Run:  ./bin/pesforge-gtk4              (or: python3 editor_gtk.py; GTK4 via gi, no other dependency)

The window starts empty: nothing is read until a database CPK is imported, and the hub shows
what a pick holds before the window changes (§ ImportHub). One panel edits every release this
build reads — PES 2019's 188-byte records and PES 2015's 180-byte ones — because everything the
screen shows derives from that release's map module (`pes2019.py` / `pes2015.py`): ability order,
bio rows, skills, positions, playing styles.

What is real here: the list is a virtualized GtkColumnView over the table the game itself loads,
search filters 16k rows through a GtkFilterListModel, the ability bars are Cairo on a
GtkDrawingArea, the palette is `theme.tokens(...)` turned into CSS, and Save patches every copy
of the table (with region backups, then reads each one back). Undo/redo and the photo fetch exist
in the sibling Tk editor, which is not part of this build.
"""
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pes2015  # noqa: E402
import pes2019  # noqa: E402
import pes2021  # noqa: E402
import pesdb  # noqa: E402
import settings  # noqa: E402
import theme  # noqa: E402

SELFTEST_FAILURES = []            # filled by --selftest runs, turned into an exit code by main()

# The pick/probe layer (VINTAGES, the release probes, folder resolution) moved into the
# package; the names are re-exported so `editor_gtk.VINTAGES` and friends keep working for
# the tests, the launcher and anything importing this file.
from pesforge_gtk.importing import (  # noqa: E402,F401
    EDITABLE, GAME, NOT_READ, PROBE_LIMIT, VINTAGE_NAMES, VINTAGES,
    cpk_files, db_arg, default_database, describe_database, describe_pick, find_database,
    game_dir, import_start_folder, install_of, pack_order, probe_candidates, probe_database,
    rank_cpk, vintage_database,
)
from pesforge_gtk.styling import (  # noqa: E402,F401  (re-exported: the tests use these)
    TOK, css, install_css, system_theme, tint,
)
from pesforge_gtk.reference import (  # noqa: E402,F401
    ABILITY_GROUPS, COUNTRIES, OVR_ALIASES, STYLE_NAMES_FALLBACK,
    ability_group, ability_order, bulk_fields, country_names, field_range,
    grouped_ability_order, overall_rating, tier_of,
)
from pesforge_gtk.widgets import (  # noqa: E402,F401
    AbilityBar, a11y, dim_label, ellipsizing_dropdown, select_all_on_focus,
    set_widget_value, slim_spin, spin_steps, step_widget, widget_value,
)
from pesforge_gtk.hub import ImportHub  # noqa: E402,F401
from pesforge_gtk.listpane import (  # noqa: E402,F401
    LIST_MAX, LIST_MIN_H, ListPaneMixin, Row,
)


WIDE_ENOUGH = 1000        # the window width at which the roomy grids fit beside the list


def widgets(root):
    """Every widget under `root`, depth first (no window-manager or GTK-internal traversal)."""
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        out.append(w)
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return out


class PesForgeGtk(ListPaneMixin, Gtk.ApplicationWindow):
    def __init__(self, app, table=None, table_source=None, teams=None, assign=None):
        """Starts with no database: nothing is read until the user imports one."""
        super().__init__(application=app, title="PESForge GTK4")
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
        hint = Gtk.Label(label="Import the CPK that carries the player tables — for example\n"
                               "PES 2019's Data/dt10_x64.cpk, PES 2015's Data/dt10.cpk or "
                               "an SP Football Life download/data_s*.cpk",
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
        version = Gtk.Label(label="reads PES 2021 / SP Football Life, PES 2019 and PES 2015 CPKs · "
                                  "edits 180-, 188- and 312-byte player records")
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
        self._pane_split = None
        if hasattr(self, "bulk_field"):            # the tray's fields differ per release
            self.bulk_field.set_model(Gtk.StringList.new(list(bulk_fields(self.mod))))
            self._update_bulk_range()
        old = self.stack.get_child_by_name("data")
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(panes, "data")
        self.stack.set_visible_child_name("data")
        self._col_room = None                    # fresh columns: re-apply the width policy

    def do_size_allocate(self, width, height, baseline):
        """Track the real window size: GTK4 has no size signal, and the arrangement needs both."""
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        self._size_panes(width, height)

    def _rebuild_for_mode(self):
        """Rebuild the grids for the other layout — once, when the window's size is settled.

        A rebuild is a fresh page: the store, the filter and the form all have to be refilled,
        or the list comes back empty.
        """
        was = self.index
        self._build_panes()
        if getattr(self, "rows", None):
            self.populate()
            self._refilter()
            if was is not None:
                self._load(was)
        return False

    def _size_panes(self, width, height=0):
        """Point the page: the list beside the editor, or as a strip above it.

        The editor's own content decides, not a width I guessed: the form overflows (and GTK
        slices its columns) whenever the space left beside the list is smaller than what the
        fields need, and that need changes with how the labels happen to wrap. So: side by side
        while it fits, strip-on-top the moment it does not, with a 40px band so the two states
        cannot flip back and forth.
        """
        if width <= 1 or not hasattr(self, "_editor"):
            return False
        total = width - 16                          # the two 8px margins
        list_w = max(260, min(LIST_MAX, int(total * 0.32)))
        # The six columns are 452px together and a ColumnView squeezes instead of scrolling: at a
        # 320px pane ID and NAME collapsed to nothing (the "blank columns" the user reported)
        # because the policy below was data nobody read. Hide what does not fit, and never hand the
        # pane less width than the columns that are left.
        pane_w = total if (width < 910 or 0 < height < 950) else list_w
        cols_w = 36 + 158 + 56                      # check, NAME, STATE: shown at any width
        for col, need in self._col_min:
            col.set_visible(pane_w >= need)
            if pane_w >= need:
                cols_w += int(col.get_fixed_width())
        list_w = max(list_w, cols_w)
        space = total - list_w - 8                  # what the editor would get beside the list
        try:
            editor_min = self._editor.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        except Exception:                           # not measurable before the first frame
            editor_min = 586
        was = getattr(self, "_paned", None) or ("h", 0)
        if was[0] == "h":
            # what the editor needs when it *has* the side-by-side width — the only honest number
            # to judge the way back with. Measured while stacked it is inflated (the form is as
            # wide as the window there, so its labels stop wrapping), and the pane then never
            # returns: the split layout would be a one-way door.
            self._editor_min_h = max(getattr(self, "_editor_min_h", 0), editor_min)
        else:
            editor_min = getattr(self, "_editor_min_h", editor_min)
        stacked = (width < 910 or 0 < height < 950
                   or space < editor_min + 16
                   or (was[0] == "h" and self._form_overflows()))
        want = ("v", -1) if stacked else ("h", list_w)
        if was != want:
            self._paned = want
            self._panes.set_orientation(
                Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
            self._editor.set_vexpand(True)
            if stacked:
                self._list_pane.set_size_request(-1, LIST_MIN_H)
            else:
                self._list_pane.set_size_request(list_w, -1)
        return False

    def _form_overflows(self):
        """True when the form pane is actually showing a horizontal scrollbar this frame."""
        scroller = self._form_pane.get_first_child()
        if scroller is None:
            return False
        ha = scroller.get_hadjustment()
        return ha.get_upper() > ha.get_page_size() + 0.5


    def _form(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("pane")
        box.set_hexpand(True)
        box.set_margin_top(0)
        # Rows are as tall as a spinbutton whatever the window does, so a narrow window gets more
        # columns instead of more rows and drops the value bars: that is what makes the whole
        # panel fit the window the user actually has. `self.narrow` is set in _build_panes.
        narrow = getattr(self, "narrow", False)
        scroller = Gtk.ScrolledWindow()      # narrow windows both panes share a column and scroll
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(False)
        scroller.set_min_content_width(0)    # the pane's width is _size_panes' call, not its rows'
        scroller.set_vexpand(True)
        box.append(scroller)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m, side in ((8, "start"), (8, "end"), (6, "top"), (8, "bottom")):
            inner.set_margin_start(m) if side == "start" else (
                inner.set_margin_end(m) if side == "end" else
                inner.set_margin_top(m) if side == "top" else inner.set_margin_bottom(m))
        scroller.set_child(inner)

        inner.append(self._section("PLAYER", ""))
        self.hero = Gtk.Label(label="no player", xalign=0)
        self.hero.add_css_class("hero")
        inner.append(self.hero)
        self.meta = Gtk.Label(label="", xalign=0)
        self.meta.add_css_class("meta")
        self.meta.set_ellipsize(Pango.EllipsizeMode.END)
        inner.append(self.meta)

        pills = Gtk.Box(spacing=6)
        self.pills = {}
        for key in ("OVR", "Height", "Weight", "Age", "Foot"):
            p = Gtk.Label(label=f"{key} —")
            p.add_css_class("pill")
            pills.append(p)
            self.pills[key] = p
        inner.append(pills)

        self.name_entries = {}
        self.name_slots = {}
        slots = getattr(self.mod, "NAME_SLOTS",
                       (("Name", "full"), ("Shirt Name", "shirt"), ("Japanese Name", "japanese")))
        nrow = Gtk.Grid(column_spacing=10, row_spacing=0) if narrow else None
        for i, (key, off) in enumerate(slots):
            lab = dim_label(key, 72 if narrow else 96, 11 if narrow else 15)
            e = Gtk.Entry()
            e.set_hexpand(True)
            e.set_tooltip_text(f"up to {self._name_limit()} bytes")
            a11y(e, f"{key}, up to {self._name_limit()} bytes")
            e.connect("changed", self._on_name_changed, off)
            self.name_slots[key] = off
            self.name_entries[key] = e
            if nrow is not None:              # short window: pair the name fields up
                nrow.attach(lab, (i % 2) * 2, i // 2, 1, 1)
                nrow.attach(e, (i % 2) * 2 + 1, i // 2, 1, 1)
            else:
                row = Gtk.Box(spacing=6)
                row.append(lab)
                row.append(e)
                inner.append(row)
        if nrow is not None:
            inner.append(nrow)

        inner.append(self._section("ABILITIES",
                                   " · ".join(name for name, _labels in ABILITY_GROUPS)))
        self.abilities = {}
        cols = 3 if narrow else 2
        grid = Gtk.Grid(column_spacing=6, row_spacing=0)
        order = grouped_ability_order(self.mod)
        for n, label in enumerate(order):
            cell = Gtk.Box(spacing=6)
            cell.append(dim_label(label, 114, 20))
            spin = Gtk.SpinButton.new_with_range(40, 99, 1)
            spin.set_width_chars(4)          # without this the entry reserves ~112px
            spin.set_size_request(72, 28)    # one box size for every value control in the grid
            lo, hi = spin.get_range()        # the range belongs on the field, not floating above
            spin.set_tooltip_text(f"{label} · {ability_group(label)} · {int(lo)} to {int(hi)}")
            a11y(spin, f"{label}, {ability_group(label)}, {int(lo)} to {int(hi)}")
            select_all_on_focus(spin)
            spin_steps(spin)
            if narrow:
                slim_spin(spin)
            spin.connect("value-changed", self._on_ability_changed, label)
            cell.append(spin)
            if not narrow:                   # the wide layout has room for the value bars
                bar = AbilityBar(42)
                cell.append(bar)
                self.abilities[label] = (spin, bar)
            else:
                self.abilities[label] = (spin, None)
            grid.attach(cell, n % cols, n // cols, 1, 1)
        pos_cell = Gtk.Box(spacing=6)
        pos_cell.append(dim_label("Position", 114, 20))
        self.position = ellipsizing_dropdown(self.mod.POSITION_NAMES, 5)   # "DMF" fits, its popup keeps the long form
        self.position.set_size_request(72, 28)                      # same box as the value fields
        self.position.set_hexpand(False)                            # or it outgrows its column
        self.position.connect("notify::selected", self._on_position_changed)
        pos_cell.append(self.position)
        grid.attach(pos_cell, len(order) % cols, len(order) // cols, 1, 1)
        band = Gtk.Box(spacing=12)
        band.append(grid)
        if getattr(self, "roomy", False):
            band.append(self._profile())
        inner.append(band)
        return box

    BULK_OPS = (("Set to", "set"), ("Add", "add"), ("Subtract", "sub"))
    # The bio rows both releases share, with the ranges the panel has always used; the map
    # module decides which of them exist (PES 2015 has no Reputation).
    BIO_LABELS = ("Height", "Weight", "Age", "Nationality", "Form", "Weak Foot Usage",
                  "Weak Foot Accuracy", "Injury Resistance", "Reputation")
    BIO_RANGES = {"Height": (150, 215), "Weight": (55, 110), "Age": (15, 50),
                  "Nationality": (0, 511), "Form": (1, 8), "Weak Foot Usage": (1, 4),
                  "Weak Foot Accuracy": (1, 4), "Injury Resistance": (1, 4),
                  "Reputation": (1, 8)}

    def _profile(self):
        """The abilities at a glance: one bar per group, drawn right of the grid.

        The grid already holds every number; this shows the shape of them — four rows, each the
        mean of its group. Reuses AbilityBar, so the colours match the values elsewhere.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.set_valign(Gtk.Align.START)
        head = Gtk.Label(label="PROFILE", xalign=0)
        head.add_css_class("dim")
        box.append(head)
        self.profile = {}
        for name, _labels in ABILITY_GROUPS:
            row = Gtk.Box(spacing=6)
            tag = Gtk.Label(label=name[:3].upper(), xalign=0)
            tag.add_css_class("dim")
            tag.set_size_request(32, -1)
            bar = AbilityBar(64)
            bar.set_hexpand(True)
            bar.set_tooltip_text(f"{name} average")
            a11y(bar, f"{name} average")
            value = Gtk.Label(label="—", xalign=1)
            value.add_css_class("dim")
            value.set_size_request(20, -1)
            row.append(tag)
            row.append(bar)
            row.append(value)
            box.append(row)
            self.profile[name] = (bar, value)
        return box

    def _bio(self):
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.add_css_class("pane")
        pane.set_vexpand(True)
        box = Gtk.ScrolledWindow()          # the safety net, like the app's own scroll panes
        box.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box.set_propagate_natural_width(False)
        box.set_min_content_width(0)        # the pane's width is _size_panes' call, not its rows'
        box.set_vexpand(True)
        pane.append(box)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_top(6)
        inner.set_margin_bottom(6)
        inner.set_margin_start(8)
        inner.set_margin_end(8)
        box.set_child(inner)
        inner.append(self._section("BIO", ""))

        self.bio = {}
        roomy = self.roomy                        # three narrow columns, or two roomy ones
        pairs = []

        # PES 2015 stores the same style ids as 2019 (0 None, 17 Defensive Goalkeeper) but its
        # map carries no names, so it borrows the shared list
        names = getattr(self.mod, "STYLE_NAMES", None) or STYLE_NAMES_FALLBACK
        if names:
            self.style = ellipsizing_dropdown(names)       # roomy: no truncation, the box is the longest style
            self.style.connect("notify::selected", self._on_style_changed)
        else:                                 # a release without names: its id in a spinbox
            lo, hi = field_range(self.mod, "Playing Style")
            self.style = Gtk.SpinButton.new_with_range(lo, hi, 1)
            self.style.connect("value-changed", self._on_bio_changed, "Playing Style")
        self.foot = ellipsizing_dropdown(("Right", "Left"), 6)
        self.foot.connect("notify::selected", self._on_foot_changed)

        # Every field goes into ONE grid: the style and foot dropdowns used to sit on their own
        # line below it, so their labels started at a different x than the rows above and the
        # boxes looked like they were floating. The grid sizes each value column to its widest
        # member, so the boxes line up and a country name is never cut short.
        fields = [(label, self.BIO_RANGES[label]) for label in self.BIO_LABELS
                  if label in self.mod.FIELD_BY_LABEL]
        # style and foot follow the last row's dropdown, as in the game's own panel
        fields += [("Playing Style", None), ("Foot", None)]
        for label, span in fields:
            key = label
            if label == "Playing Style":
                widget = self.style
            elif label == "Foot":
                widget = self.foot
            elif key == "Nationality":
                # a code, not a number: offer the country itself. The list is the whole code
                # range, so the selected index *is* the stored value — nothing to drift.
                widget = ellipsizing_dropdown([COUNTRIES.get(c, f"#{c}") for c in range(512)],
                                              12)                        # 7 cut "Portugal" in half
                if hasattr(widget, "set_enable_search"):
                    widget.set_enable_search(True)
            else:
                lo, hi = span
                widget = Gtk.SpinButton.new_with_range(lo, hi, 1)
                widget.set_width_chars(4)
                if not roomy:
                    slim_spin(widget)
            if isinstance(widget, Gtk.DropDown):
                # notify::selected hands over the pspec too; the handler wants (widget, key)
                widget.connect("notify::selected",
                               lambda dd, _p, k=key: self._on_bio_changed(dd, k))
            else:
                widget.connect("value-changed", self._on_bio_changed, key)
            if span is not None:
                widget.set_tooltip_text(f"{label} · {span[0]} to {span[1]}")
                a11y(widget, f"{label}, {span[0]} to {span[1]}")
            if isinstance(widget, Gtk.SpinButton):
                select_all_on_focus(widget)
                spin_steps(widget)
            self.bio[key] = widget
            pairs.append((label, widget))

        grid = Gtk.Grid(column_spacing=6, row_spacing=1)
        ncols = 2 if roomy else 3
        for i, (text, widget) in enumerate(pairs):
            lab = dim_label(text, 128 if roomy else 114, 20)
            widget.set_hexpand(False)
            widget.set_halign(Gtk.Align.FILL)   # one width per column: no box floats
            if widget.get_tooltip_text() is None:
                widget.set_tooltip_text(text)
            grid.attach(lab, (i % ncols) * 2, i // ncols, 1, 1)
            grid.attach(widget, (i % ncols) * 2 + 1, i // ncols, 1, 1)
        inner.append(grid)

        self.chip_filter = Gtk.SearchEntry()
        self.chip_filter.add_css_class("chipfilter")
        self.chip_filter.set_placeholder_text("filter skills and styles…")
        a11y(self.chip_filter, "filter the skill and playing-style chips by name")
        self.chip_filter.set_tooltip_text("show only the chips whose name matches")
        self.chip_filter.connect("search-changed", lambda _e: self._filter_chips())
        inner.append(self._section("SKILLS & COM STYLES", self.chip_filter))
        # A grid, not a FlowBox: the flow reports six widest-in-a-row as its minimum width, which
        # no amount of font shrinking fixed, and a grid's columns are the pane's to divide.
        cols = 6 if getattr(self, "narrow", False) else 4
        grid2 = Gtk.Grid(column_spacing=2, row_spacing=2)
        self.chips = {}
        # the modules key their skill/style flags as "Skill: X" / "Style: X"; the chip shows
        # the bare name, the table lookup uses the label
        for n, label in enumerate([f"Style: {x}" for x in self.mod.COM_STYLES]
                                  + [f"Skill: {x}" for x in self.mod.SKILLS]):
            chip = Gtk.ToggleButton(label=label.split(": ", 1)[1])
            chip.add_css_class("chip")
            chip.set_hexpand(True)
            text = chip.get_child()
            if text is not None:              # long names elide instead of widening the column
                text.set_ellipsize(Pango.EllipsizeMode.END)
            chip.set_tooltip_text(label.split(": ", 1)[1])
            a11y(chip, f"toggle {label.split(': ', 1)[1]}")
            self._chip_text(chip, False, chip.get_tooltip_text())
            chip.connect("toggled", self._on_skill_toggled, label)
            grid2.attach(chip, n % cols, n // cols, 1, 1)
            self.chips[label] = chip
        inner.append(grid2)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        inner.append(spacer)
        return pane

    def _section(self, text, hint):
        """A section band: its title, and a hint that may be a string or a widget."""
        box = Gtk.Box(spacing=8)
        box.add_css_class("section-row")
        lab = Gtk.Label(label=f"▼ {text}", xalign=0)
        lab.add_css_class("section")
        box.append(lab)
        if isinstance(hint, Gtk.Widget):
            hint.set_hexpand(True)
            hint.set_halign(Gtk.Align.END)
            box.append(hint)
        elif hint:
            h = Gtk.Label(label=hint, xalign=1)
            h.add_css_class("hint")
            h.set_hexpand(True)
            box.append(h)
        return box

    def _batch_bar(self):
        """The mockup's tray: one operation across every ticked player, staged in memory."""
        self.batch = Gtk.Box(spacing=8)
        self.batch.add_css_class("batch")
        self.batch.set_margin_start(14)
        self.batch.set_margin_end(14)
        self.batch.set_margin_top(2)
        self.batch_label = Gtk.Label(label="", xalign=0)
        self.batch_label.add_css_class("hint")
        self.batch_label.set_ellipsize(Pango.EllipsizeMode.END)  # never widens the tray
        self.batch.append(self.batch_label)
        self.bulk_field = Gtk.DropDown.new_from_strings(bulk_fields(self.mod))
        self.bulk_field.set_tooltip_text("field to change")
        self.bulk_field.set_size_request(186, -1)
        self.bulk_field.connect("notify::selected", self._update_bulk_range)
        self.batch.append(self.bulk_field)
        self.bulk_op = Gtk.DropDown.new_from_strings([label for label, _ in self.BULK_OPS])
        self.bulk_op.set_selected(1)
        self.batch.append(self.bulk_op)
        self.bulk_amount = Gtk.SpinButton.new_with_range(-99, 99, 1)
        spin_steps(self.bulk_amount)
        self.bulk_amount.set_value(1)
        self.batch.append(self.bulk_amount)
        apply_btn = Gtk.Button(label="Apply to selected")
        apply_btn.add_css_class("accent")
        apply_btn.connect("clicked", lambda _b: self._bulk_apply())
        self.batch.append(apply_btn)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.batch.append(spacer)
        clear = Gtk.Button(label="Clear selection")
        clear.connect("clicked", lambda _b: self.selection.unselect_all())
        self.batch.append(clear)
        self.batch.set_visible(False)
        self._bulk_widgets = (self.bulk_field, self.bulk_op, self.bulk_amount, apply_btn)
        return self.batch

    def _range(self, label):
        """(low, high) for a field: the form's own range where one is declared, else the bits."""
        lo, hi = field_range(self.mod, label)
        if label in self.BIO_RANGES:
            lo, hi = max(lo, self.BIO_RANGES[label][0]), min(hi, self.BIO_RANGES[label][1])
        return lo, hi

    def _update_bulk_range(self, *_a):
        """The amount follows the field: "Set to" reaches the field's whole range.

        The spin was ±99, so "Set to Height 200" (or Nationality 511) was unreachable.
        """
        if not hasattr(self, "bulk_amount"):
            return
        label = bulk_fields(self.mod)[self.bulk_field.get_selected()]
        lo, hi = self._range(label)
        self.bulk_amount.set_range(min(lo, -99), max(hi, 99))

    def _update_bulk(self):
        self._update_badge()
        n = len(self.selected_indexes())
        self.batch.set_visible(bool(n))
        if n:
            self.batch_label.set_text(f"{n} selected · {len(self.dirty)} pending edit(s)")

    def _bulk_apply(self):
        """Set, add or subtract one field across every ticked player, clamped to its range."""
        indexes = self.selected_indexes()
        if not indexes:
            return
        label = bulk_fields(self.mod)[self.bulk_field.get_selected()]
        op = self.BULK_OPS[self.bulk_op.get_selected()][1]
        amount = int(self.bulk_amount.get_value())
        lo, hi = self._range(label)
        changes = []
        for i in indexes:
            old = self.table.get(i, label)
            new = amount if op == "set" else (old + amount if op == "add" else old - amount)
            new = max(lo, min(hi, new))
            if new == old:
                continue                        # clamped back to what it already was
            changes.append((i, label, old, new))
            self.table.set(i, label, new)
            self._mark_dirty(i, label, refresh=False)
        self._refresh_cells(indexes)
        self._push_step(changes)
        if self.index in indexes:
            self._load(self.index)              # the form shows the current player: reload it
        done = f"set {label} to {max(lo, min(hi, amount))}" if op == "set" \
            else f"{op} {amount:+d} {label}"
        self._status(f"{done} on {len(indexes)} player(s) · "
                     f"{len(self.dirty)} pending edit(s) · Save writes it")
        self._update_bulk()


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
        """Ctrl+S / Ctrl+Z / Ctrl+Y / Ctrl+F / Escape: save, undo, redo, find, clear."""
        ctrl = Gtk.ShortcutController()
        for spec, fn in (("<Control>s", lambda *_: (self._save(), True)[1]),
                         ("<Control>z", self._undo),
                         ("<Control>y", self._redo),
                         ("<Control><Shift>z", self._redo),
                         ("<Control>f", lambda *_: (self.search.grab_focus(), True)[1]),
                         ("Escape", lambda *_: (self.search.set_text(""), True)[1])):
            ctrl.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(spec),
                                               Gtk.CallbackAction.new(fn)))
        self.add_controller(ctrl)

    def _alert(self, message, detail=""):
        """A modal the user cannot miss: a traceback in a signal handler is not an error report."""
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(message)
        if detail:
            dialog.set_detail(detail[:400])
        dialog.set_buttons(["OK"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def _done(d, result):
            try:
                d.choose_finish(result)
            except GLib.Error:                      # dismissed
                pass
        dialog.choose(self, None, _done)

    def _on_close_request(self, *_a, ask=True):
        """Hold the window open while edits are pending: memory is their only copy."""
        if not self.dirty:
            return False
        if ask:
            dialog = Gtk.AlertDialog()
            dialog.set_modal(True)
            dialog.set_message(f"Discard {len(self.dirty)} unsaved edit(s)?")
            dialog.set_detail("They are not written to the database until you press "
                              "Save to game.")
            dialog.set_buttons(["Keep editing", "Discard"])
            dialog.set_cancel_button(0)
            dialog.set_default_button(0)

            def _done(d, result):
                try:
                    if d.choose_finish(result) == 1:
                        self.destroy()
                except GLib.Error:
                    pass
            dialog.choose(self, None, _done)
        return True

    # ---- import / unload
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
            filters.set_name("CRI CPK database")
            filters.add_pattern("*.cpk")
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
                    else " · this build edits PES 2019, PES 2015 and SP Football Life records.")
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
                        if settings.edit_save_files() else ""))
        return True


    def _selftest_theme(self):
        """`PESFORGE_GTK_SELFTEST=theme`: which palette is in force, and does it hold contrast."""
        ratio = theme.contrast(TOK["fg"], TOK["bg"])
        print(f"SELFTEST THEME {system_theme()} bg={TOK['bg']} fg={TOK['fg']} "
              f"accent={TOK['accent']} contrast={ratio:.2f}", flush=True)
        if ratio < 4.5:
            return self._selftest_fail(f"fg/bg contrast {ratio:.2f} is below 4.5")
        self.close()
        return False

    def _selftest_fit(self):
        """`PESFORGE_GTK_SELFTEST=fit`: neither editor pane may need a scrollbar at this size.

        The user's ask is the whole editor on screen at once — no scrolling, no resizing. The
        panes are inside scroll areas as a safety net, so the test is whether their content still
        exceeds the page the scroller shows.
        """
        sizes = []
        for label, scroller in (("form", self._form_pane), ("bio", self._bio_pane)):
            inner = scroller.get_first_child()          # the pane's own scroll area
            if inner is None:
                return self._selftest_fail(f"{label} pane has no scroll area")
            if inner.get_width() <= 1 or inner.get_height() <= 1:
                # a zero-sized pane has nothing to overflow, so the check below would "pass"
                return self._selftest_fail(f"{label} pane is not laid out yet")
            va, ha = inner.get_vadjustment(), inner.get_hadjustment()
            over = (int(ha.get_upper()), int(va.get_upper()))
            sizes.append(f"{label}={inner.get_width()}x{inner.get_height()}"
                         f" (needs {over[0]}x{over[1]})")
            if va.get_upper() > va.get_page_size() + 0.5 or ha.get_upper() > ha.get_page_size() + 0.5:
                return self._selftest_fail(f"{label} pane scrolls: " + ", ".join(sizes))
        print(f"SELFTEST FIT OK window={self.get_width()}x{self.get_height()} "
              + " ".join(sizes), flush=True)
        self.close()
        return False

    def selftest(self):
        """`PESFORGE_GTK_SELFTEST=1`: prove the screen built against real data, then quit.

        Guards the two failures a traceback never shows: an empty detail pane beside a selected
        row, and a list model that does not match the table behind it. `=empty` checks the other
        half instead: nothing is read until it is asked for.
        """
        mode = os.environ.get("PESFORGE_GTK_SELFTEST", "").lower()
        if mode == "empty":
            return self._selftest_empty()
        if mode == "cycle":
            return self._selftest_cycle()
        if mode == "select":
            return self._selftest_select()
        if mode == "hub":
            return self._selftest_hub()
        if mode == "write2015":
            return self._selftest_write2015()
        if mode == "write2021":
            return self._selftest_write2021()
        if mode == "fit":
            return self._selftest_fit()
        if mode == "theme":
            return self._selftest_theme()
        if not self.rows or len(self.store) != len(self.rows):
            return self._selftest_fail(f"list holds {len(self.store)} rows, table has {len(self.rows)}")
        if self.index is None or not self.name_entries["Name"].get_text().strip():
            return self._selftest_fail("no player loaded into the form")
        total = self.filtered.get_n_items()
        self.search.set_text(self.rows[self.index].name[:5])
        self._refilter()                        # applied directly: the signal is async
        narrowed = self.filtered.get_n_items()
        if not 0 < narrowed < total:
            return self._selftest_fail(f"search for {self.rows[self.index].name!r} matched {narrowed} of {total}")
        self.search.set_text("zzzznomatch")
        self._refilter()
        if self.filtered.get_n_items():
            return self._selftest_fail("a query nothing can match still listed players")
        if "no player matches" not in self.count.get_text():
            return self._selftest_fail(f"an empty result read as: {self.count.get_text()!r}")
        self.search.set_text("")
        self._refilter()
        if self.filtered.get_n_items() != total:
            return self._selftest_fail("clearing the search did not restore the list")
        if os.environ.get("PESFORGE_GTK_SELFTEST", "").lower() in ("write", "2"):
            return self._selftest_write()
        nations = self._selftest_clubs()
        if nations is False:                    # the club check already reported what was wrong
            return False
        if self._selftest_chrome() is False:
            return False
        print(f"SELFTEST OK players={total} selected={self.index} "
              f"abilities={len(self.abilities)} chips={len(self.chips)} "
              f"nations={nations} source={self.source_name}", flush=True)
        self.close()
        return False

    def _selftest_select(self):
        """Clicking a player shows that player: GTK's selection-changed reports the *previous*
        position, which used to leave the form one click behind."""
        try:
            if self.table is None:
                self.load_database(default_database(os.environ.get("PES2019_DIR", "")))
            if self.table is None:
                return self._selftest_fail(f"no database: {self.status.get_text()}")

            def shown():
                return self.index          # the table row the form is holding

            checks = []
            self.show_row(7)
            checks.append(("a click shows the row it hit", shown(), 7))
            self.selection.set_selected(3)            # GTK reports this one as pos=0
            checks.append(("a selection change shows that row", shown(), 3))
            for pos in (1, 2, 4):                     # ticks are the user's alone
                self._set_checked(self.sorted.get_item(pos).index, True)
            checks.append(("ticking does not move the form", shown(), 3))
            self.show_row(4)                          # a click among ticked rows
            checks.append(("a click among ticked rows still shows it", shown(), 4))
            checks.append(("the ticks are still counted", len(self.selected_indexes()), 3))
            for label, got, want in checks:
                if got != want:
                    return self._selftest_fail(f"{label}: got {got}, want {want}")
                print(f"SELECT {label}: {got}", flush=True)
            print("SELFTEST SELECT OK", flush=True)
        except Exception as exc:
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")
        self.close()
        return False

    def _selftest_hub(self):
        """The hub says what a pick holds, and offers Load only when it can open it."""
        try:
            checks = []
            pack = default_database(os.environ.get("PES2019_DIR", ""))
            if pack:
                details, can_load = describe_database(pack)
                checks.append(("a PES 2019 pack is loadable", can_load, True))
                checks.append(("its players are counted", any("players" in d for d in details), True))
            db15 = vintage_database("pes2015")
            if db15 and os.path.isfile(db15):
                details, can_load = describe_database(db15)
                checks.append(("a PES 2015 database is loadable", can_load, True))
                checks.append(("and counted", any("players" in d for d in details), True))
                details, can_load, target = describe_pick(os.path.dirname(db15))
                checks.append(("its Data/ folder resolves to dt10.cpk",
                               os.path.basename(target), "dt10.cpk"))
                checks.append(("and that folder pick is loadable", can_load, True))
            spfl = os.path.join(os.environ.get("SPFL_DIR", ""), "download", "data_s2627.cpk")
            if not os.path.isfile(spfl):
                spfl = vintage_database("pes2021")
            if os.path.isfile(spfl):
                details, can_load = describe_database(spfl)
                checks.append(("SP Football Life is loadable", can_load, True))
                checks.append(("and is named", any("PES 2021" in d for d in details), True))
                details, can_load, target = describe_pick(install_of(spfl))
                checks.append(("its install folder finds download/data_s2627.cpk",
                               os.path.basename(target), "data_s2627.cpk"))
                checks.append(("which this build edits", can_load, True))
                checks.append(("and loading that folder works",
                               self.load_database(install_of(spfl)), True))
                checks.append(("the footer names the file it found",
                               os.path.basename(spfl) in self.status.get_text(), True))
                checks.append(("and counts its players",
                               f"{len(self.table):,} players" in self.status.get_text(), True))
                checks.append(("loaded as the 2021 vintage", self.vintage, "pes2021"))
                self.unload_database()
                details, can_load, target = describe_pick(os.path.join(install_of(spfl), "Data"))
                checks.append(("picking its Data/ folder still finds download/data_s2627.cpk",
                               os.path.basename(target), "data_s2627.cpk"))
            if not checks:
                return self._selftest_fail("no install to describe")
            self.hub = ImportHub(self)
            shown = pack or spfl
            self.hub.show_path(shown)
            # Load tracks whether *this* pick can be opened — SP Football Life included, which is
            # why the old "a PES 2019 pack exists" expectation no longer holds.
            checks.append(("the hub takes a loadable pick", self.hub.load_btn.get_sensitive(),
                           bool(shown) and describe_pick(shown)[1]))
            if pack:
                # a folder pick: the install resolves to the pack the game itself loads
                self.hub.show_path(install_of(pack))
                checks.append(("the hub takes a folder and finds the pack",
                               os.path.basename(self.hub.path), os.path.basename(pack)))
                checks.append(("and offers to load it", self.hub.load_btn.get_sensitive(), True))
                self.hub.show_path(os.path.dirname(pack))
                checks.append(("the folder holding that pack resolves to it",
                               os.path.basename(self.hub.path), os.path.basename(pack)))
            empty = os.path.join(os.environ.get("PES2019_DIR", ""), "Data", "dt00_4K_x64.cpk")
            if os.path.isfile(empty):
                self.hub.show_path(empty)
                checks.append(("a pick with no table offers the database of its folder",
                               os.path.basename(self.hub.path), "dt10_x64.cpk"))
                checks.append(("and it is loadable", self.hub.load_btn.get_sensitive(), True))
                self.hub.show_path(os.path.dirname(empty))
                checks.append(("picking that Data/ folder resolves past the dt00 stub",
                               os.path.basename(self.hub.path), "dt10_x64.cpk"))
            missing = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-no-database-here")
            os.makedirs(missing, exist_ok=True)
            self.hub.show_path(missing)
            checks.append(("a folder with no database offers nothing",
                           (self.hub.load_btn.get_sensitive(), "no player table" in
                            self.hub.summary.get_text()), (False, True)))
            self.hub.close()
            for label, got, want in checks:
                if got != want:
                    return self._selftest_fail(f"{label}: got {got}, want {want}")
                print(f"HUB {label}: {got}", flush=True)
            print("SELFTEST HUB OK", flush=True)
        except Exception as exc:
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")
        self.close()
        return False

    def _selftest_write2015(self):
        """Load a copy of the PES 2015 database, edit it, save, and read the edits back.

        The copy lives in a scratch folder shaped like an install, so the save writes that
        and never the game — the released file's hash is checked before it finishes.
        """
        import hashlib
        import shutil
        try:
            src = vintage_database("pes2015")
            if not src or not os.path.isfile(src):
                return self._selftest_fail(f"no PES 2015 database (set PES2015_DIR): {src!r}")
            before = hashlib.sha256(open(src, "rb").read()).hexdigest()
            root = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-2015-check")
            copy = os.path.join(root, "Data", "dt10.cpk")
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            shutil.copyfile(src, copy)
            self.load_database(copy)
            if self.table is None:
                return self._selftest_fail(f"could not load the copy: {self.status.get_text()}")
            if self.vintage != "pes2015" or len(self.rows) < 1000:
                return self._selftest_fail(f"loaded {self.vintage} with {len(self.rows)} rows")
            i = 3
            pid = self.table.pid(i)
            label = "Speed" if "Speed" in self.mod.FIELD_BY_LABEL else ability_order(self.mod)[0]
            was = self.table.get(i, label)
            want = min(99, was + 3)
            self.table.set(i, label, want)
            self._mark_dirty(i, label)
            self.table.set_name(i, shirt="SELFTEST")
            self._mark_dirty(i, "name:shirt")
            self._save()
            if "saved" not in self.status.get_text():
                return self._selftest_fail(f"save said {self.status.get_text()!r}")
            back = self.mod.Cpk(copy).read_table("Player")
            hits = back.find(str(pid))
            if not hits:
                return self._selftest_fail(f"player {pid} vanished from the copy")
            j = hits[0]
            if back.get(j, label) != want or back.shirt(j) != "SELFTEST":
                return self._selftest_fail(f"read back {back.get(j, label)!r}/{back.shirt(j)!r}, "
                                           f"want {want}/'SELFTEST'")
            if hashlib.sha256(open(src, "rb").read()).hexdigest() != before:
                return self._selftest_fail("the game's own database was modified")
            print(f"SELFTEST WRITE2015 OK players={len(self.rows)} pid={pid} "
                  f"{label}={was}->{want} shirt=SELFTEST", flush=True)
        except Exception as exc:
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")
        self.close()
        return False

    def _selftest_write2021(self):
        """Load a copy of the SP Football Life / PES 2021 database, edit it, save, read it back.

        The copy lives in a scratch folder shaped like an install (`download/data_s2627.cpk`), so
        the save writes that and never the game — the released file's hash is checked at the end.
        """
        import hashlib
        import shutil
        try:
            src = vintage_database("pes2021")
            if not src or not os.path.isfile(src):
                return self._selftest_fail(f"no 2021 database (set SPFL_DIR): {src!r}")
            before = hashlib.sha256(open(src, "rb").read()).hexdigest()
            root = os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-2021-check")
            copy = os.path.join(root, "download", os.path.basename(src))
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            shutil.copyfile(src, copy)
            self.load_database(copy)
            if self.table is None:
                return self._selftest_fail(f"could not load the copy: {self.status.get_text()}")
            if self.vintage != "pes2021" or len(self.rows) < 10000:
                return self._selftest_fail(f"loaded {self.vintage} with {len(self.rows)} rows")
            i = 3
            pid = self.table.pid(i)
            label = "Speed"
            was = self.table.get(i, label)
            want = min(99, was + 3)
            self.table.set(i, label, want)
            self._mark_dirty(i, label)
            skill = "Skill: " + pes2021.SKILLS[0]
            self.table.set(i, skill, 1)
            self._mark_dirty(i, skill)
            self.table.set_name(i, shirt="SELFTEST")
            self._mark_dirty(i, "name:shirt")
            self._save()
            if "saved" not in self.status.get_text():
                return self._selftest_fail(f"save said {self.status.get_text()!r}")
            back = self.mod.Cpk(copy).read_table("Player")
            hits = back.find(str(pid))
            if not hits:
                return self._selftest_fail(f"player {pid} vanished from the copy")
            j = hits[0]
            if (back.get(j, label), back.get(j, skill), back.shirt(j)) != (want, 1, "SELFTEST"):
                return self._selftest_fail(
                    f"read back {back.get(j, label)!r}/{back.get(j, skill)!r}/{back.shirt(j)!r}, "
                    f"want {want}/1/'SELFTEST'")
            if hashlib.sha256(open(src, "rb").read()).hexdigest() != before:
                return self._selftest_fail("the game's own database was modified")
            print(f"SELFTEST WRITE2021 OK players={len(self.rows)} pid={pid} "
                  f"{label}={was}->{want} {skill}=1 shirt=SELFTEST", flush=True)
        except Exception as exc:
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")
        self.close()
        return False

    def _selftest_cycle(self):
        """Import PES 2019, unload, import SP Football Life, unload, import PES 2015."""
        try:
            spfl = settings.database_path(os.environ.get("SPFL_DIR", "")) \
                if os.environ.get("SPFL_DIR") else ""
            legs = (("pes2019", vintage_database("pes2019"), "loaded"),
                    ("spfl", spfl, "loaded"),
                    ("pes2015", vintage_database("pes2015"), "loaded"))
            for n, (name, path, expect) in enumerate(legs, 1):
                if not path or not os.path.isfile(path):
                    print(f"CYCLE step={n} name={name} result=skipped (no install)", flush=True)
                    continue
                self.load_database(path)
                loaded = self.table is not None
                if loaded and len(self.rows) < 1000:
                    return self._selftest_fail(f"{name}: loaded {len(self.rows)} rows")
                result = "loaded" if loaded else "refused"
                print(f"CYCLE step={n} name={name} result={result} file={os.path.basename(path)} "
                      f"status={self.status.get_text()[:90]!r}", flush=True)
                if result != expect:
                    return self._selftest_fail(f"{name}: {result}, expected {expect}")
                want_name = {"spfl": "PES 2021", "pes2015": "PES 2015"}.get(name)
                if want_name and want_name not in self.status.get_text():
                    return self._selftest_fail(f"{name}: the status does not name it: "
                                               f"{self.status.get_text()!r}")
                if name in ("spfl", "pes2021") and self.vintage != "pes2021":
                    return self._selftest_fail(f"{name}: loaded as {self.vintage}")
                self.unload_database()
                if self.table is not None or self.rows or self.store.get_n_items():
                    return self._selftest_fail(f"{name}: unload left the database loaded")
                if self.stack.get_visible_child_name() != "empty":
                    return self._selftest_fail(f"{name}: unload did not return to the empty page")
                if self.save_btn.get_sensitive() or self.revert_btn.get_sensitive():
                    return self._selftest_fail(f"{name}: controls stayed live after unload")
            print("SELFTEST CYCLE OK", flush=True)
        except Exception as exc:
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")
        self.close()
        return False

    def _selftest_empty(self):
        """The window must start with nothing loaded, and import a database on demand."""
        try:
            if self.table is not None or self.rows or self.store.get_n_items():
                return self._selftest_fail("a database was loaded without being asked")
            if self.stack.get_visible_child_name() != "empty":
                return self._selftest_fail(f"visible page is "
                                           f"{self.stack.get_visible_child_name()!r}, want 'empty'")
            if self.save_btn.get_sensitive() or self.revert_btn.get_sensitive():
                return self._selftest_fail("Save/Revert are live with no database")
            if self.search.get_sensitive():
                return self._selftest_fail("the search box is live with no database")
            want = default_database(GAME)
            if not want or not self.load_database(want):
                return self._selftest_fail(f"importing {want or 'nothing'} failed: "
                                           f"{self.status.get_text()!r}")
            if self.stack.get_visible_child_name() != "data":
                return self._selftest_fail("the panes did not come up after the import")
            if len(self.rows) < 10000 or self.store.get_n_items() != len(self.rows):
                return self._selftest_fail(f"{len(self.rows)} rows, "
                                           f"{self.store.get_n_items()} in the model")
            if not (self.save_btn.get_sensitive() and self.revert_btn.get_sensitive()):
                return self._selftest_fail("Save/Revert stayed disabled after the import")
            print(f"SELFTEST EMPTY OK rows={len(self.rows)} source={self.source_name}", flush=True)
        except Exception as exc:
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")
        self.close()
        return False

    def _selftest_write(self):
        """Bulk apply, commit and restore against whatever PES2019_DIR points at.

        Only ever run with that variable aimed at a copy: it does write the database.
        """
        try:
            return self._selftest_write_run()
        except Exception as exc:                     # a broken check must not hang the app
            return self._selftest_fail(f"{type(exc).__name__}: {exc}")

    def _selftest_clubs(self):
        """No list row shows a national team where its club belongs.

        The country assignment row often comes first, so "first row wins" labelled 1,263 of the
        PES 2019 players with a nation instead of their club. Returns how many rows were in that
        position, or False when one is still wrong (the failure is reported here).
        """
        checked = 0
        for i in range(len(self.table)):
            rows = self.assign_by_pid.get(self.table.pid(i)) or []
            if not rows or self.assign.team(rows[0]) > pesdb.NATIONAL_MAX:
                continue                       # a club row already comes first
            checked += 1
            want = next((self.clubs.get(self.assign.team(r), "") for r in rows
                         if self.assign.team(r) > pesdb.NATIONAL_MAX), "")
            if self.rows[i].club != want:
                return self._selftest_fail(f"row {i} ({self.table.name(i)}): shows club "
                                           f"{self.rows[i].club!r}, want {want!r}")
        return checked

    def _selftest_chrome(self):
        """The window's plain buttons are exactly the ones that act.

        The spike carried inert Undo/Redo/Discard/Save-to-game buttons in the header, a footer
        "Apply to player" that never could be clicked and a decorative Players/Clubs/PES 2015 tab
        row; a button that does nothing reads as broken the moment the app is a daily driver.
        """
        want = sorted({"Import database…", "Unload", "Revert", "Save to game",
                       "Apply to selected", "Clear selection"})
        got = sorted({w.get_label() for w in widgets(self)
                      if type(w) is Gtk.Button and w.get_label()})
        if got != want:
            return self._selftest_fail(f"window buttons are {got}, want {want}")
        print(f"CHROME OK buttons={len(got)}", flush=True)
        return True

    def _selftest_write_run(self):
        import hashlib
        path = self.source
        before = hashlib.sha256(open(path, "rb").read()).hexdigest()
        backup_dir = settings.backups_dir()
        pre = set(self.mod.backup_list(backup_dir))
        picks = [0, 1, 2]
        for pos in picks:
            self._set_checked(self.sorted.get_item(pos).index, True)
        label = "Speed"
        self.bulk_field.set_selected(bulk_fields(self.mod).index(label))
        self.bulk_op.set_selected(1)                                  # Add
        self.bulk_amount.set_value(5)
        old = {i: self.table.get(i, label) for i in picks}
        print(f"WRITE stage=selected picks={picks}", flush=True)
        self._bulk_apply()
        print(f"WRITE stage=applied dirty={len(self.dirty)}", flush=True)
        for i, was in old.items():
            want = min(99, was + 5)
            if self.table.get(i, label) != want:
                return self._selftest_fail(f"row {i}: {label} is {self.table.get(i, label)}, want {want}")
        if len(self.dirty) != 3:
            return self._selftest_fail(f"staged {len(self.dirty)} edits, want 3")
        if not self.batch_label.get_text().startswith("3 selected"):
            return self._selftest_fail(f"tray reads {self.batch_label.get_text()!r}")
        if not all(self.rows[i].edited for i in picks):
            return self._selftest_fail("a changed row is not flagged in the list")
        self._undo()
        if any(self.table.get(i, label) != was for i, was in old.items()):
            return self._selftest_fail("undo did not restore every row of the bulk step")
        self._redo()
        if any(self.table.get(i, label) != min(99, was + 5) for i, was in old.items()):
            return self._selftest_fail("redo did not reapply the bulk step")
        print("WRITE stage=undo/redo ok", flush=True)
        if self._on_close_request(ask=False) is not True:
            return self._selftest_fail("a close with pending edits was not held back")
        self._save()
        print(f"WRITE stage=saved status={self.status.get_text()!r}", flush=True)
        if "saved" not in self.status.get_text():
            return self._selftest_fail(f"save said {self.status.get_text()!r}")
        for i in picks:
            # the STATE cell is repainted by swapping the row object, so a saved row must be a
            # fresh object marked clean — otherwise the list keeps saying "edited"
            if self.rows[i].edited or self.store.get_item(i) is not self.rows[i]:
                return self._selftest_fail(f"row {i} still reads as edited after the save")
        back = self.mod.Cpk(path).read_table("Player")
        for i, was in old.items():
            if not self._reads_back(back, back.pid(i), {label: min(99, was + 5)}):
                return self._selftest_fail(f"player {back.pid(i)}: {label} missing after save")
        fresh = [n for n in self.mod.backup_list(backup_dir) if n not in pre]
        if not fresh:
            return self._selftest_fail("the save took no backup")
        for name in fresh:
            self.mod.restore_backup(name)
        if hashlib.sha256(open(path, "rb").read()).hexdigest() != before:
            return self._selftest_fail("restoring the backups did not return the original bytes")
        os.chmod(path, 0o444)                 # a write that cannot happen must say so
        self.table.set(picks[0], label, min(99, old[picks[0]] + 7))
        self._mark_dirty(picks[0], label)
        self._save()
        if "could not write" not in self.status.get_text():
            return self._selftest_fail(f"a read-only CPK was not reported: "
                                       f"{self.status.get_text()!r}")
        if not self.dirty:
            return self._selftest_fail("a failed save dropped the pending edits")
        os.chmod(path, 0o644)
        self.dirty.clear()
        print("WRITE stage=read-only reported", flush=True)
        print(f"SELFTEST WRITE OK players={len(self.rows)} edited={len(picks)} "
              f"backups={len(fresh)} restored=1 state=clean", flush=True)
        self.close()
        return False

    def _selftest_fail(self, why):
        SELFTEST_FAILURES.append(why)
        print(f"SELFTEST FAIL {why}", flush=True)
        self.dirty.clear()            # a check must not sit on the close guard
        self.close()
        return False


    # ---- form <-> record
    def _load(self, index):
        if index is None:
            return
        self.index = index
        t = self.table
        self.loading = True
        self.hero.set_text(t.name(index))
        self.meta.set_text(self._meta_text(index))
        self._paint_ovr(index)
        self._paint_profile(index)
        for key in ("Height", "Weight", "Age"):
            self.pills[key].set_text(f"{key} {t.get(index, key)}")
        self.pills["Foot"].set_text("Foot " + ("Left" if t.get(index, "Foot") else "Right"))
        for key, getter in (("Name", t.name), ("Shirt Name", t.shirt), ("Japanese Name", t.japanese)):
            entry = self.name_entries.get(key)     # a release without that slot has no row
            if entry is not None:
                entry.set_text(getter(index))
        for label, (spin, bar) in self.abilities.items():
            value = t.get(index, label)
            spin.set_value(value)
            if bar is not None:               # the narrow layout drops the bars
                bar.set_value(value)
        self.position.set_selected(t.get(index, "Position"))
        for key, spin in self.bio.items():
            set_widget_value(spin, t.get(index, key))
        style = t.get(index, "Playing Style")
        if hasattr(self.style, "set_selected"):          # a named list, wherever it came from
            names = getattr(self.mod, "STYLE_NAMES", None) or STYLE_NAMES_FALLBACK
            style = min(style, len(names) - 1)
            self.style.set_selected(style)
            self.style.set_tooltip_text(names[style] if style < len(names) else "")
        else:                                            # 2015 numbers them
            self.style.set_value(style)
        self.foot.set_selected(t.get(index, "Foot"))
        for label, chip in self.chips.items():
            chip.set_active(bool(t.get(index, label)))
            self._chip_text(chip, bool(t.get(index, label)))
        for label, (spin, _bar) in self.abilities.items():
            self._decorate(spin, label, rating=True)
        for key, widget in self.bio.items():
            self._decorate(widget, key)
        for display, entry in self.name_entries.items():
            self._decorate(entry, "name:" + self.name_slots.get(display, display))
        self.loading = False
        self._status(f"{len(self.rows):,} players · showing {t.name(index)} "
                     f"(id {t.pid(index)})"
                     + (f" · {len(self.dirty)} edited" if self.dirty else ""))

    # ---- commit
    def _edited_values(self):
        """pid -> {field: value}, the form's pending edits keyed by what the game matches on."""
        edits = {}
        for i, key in sorted(self.dirty):
            pid = self.table.pid(i)
            value = (self.table.name(i) if key == "name:full" else
                     self.table.shirt(i) if key == "name:shirt" else
                     self.table.japanese(i) if key == "name:japanese" else
                     self.table.get(i, key))
            edits.setdefault(pid, {})[key] = value
        return edits

    @staticmethod
    def _reads_back(table, pid, fields):
        """True when a patched table really carries the edited values for that player."""
        for i in table.find(str(pid)):
            if all((table.name(i) if k == "name:full" else
                    table.shirt(i) if k == "name:shirt" else
                    table.japanese(i) if k == "name:japanese" else
                    table.get(i, k)) == v for k, v in fields.items()):
                return True
        return False

    def _save(self):
        """Write every pending edit into each copy of the table, backing each region up first."""
        if not self.dirty:
            self._status("nothing to save · edit a field first")
            return
        if not self.game:
            self._status(f"no install to write to (set {VINTAGES[self.vintage]['env']})")
            return
        edits = self._edited_values()
        backup = settings.backups_dir()
        written, failed = [], []
        others = self.mod.database_cpks(self.game) if self.game else []
        # Read every copy and build what would be written first: a copy that cannot be read
        # fails before anything has been written.
        jobs = []
        for path in [self.source] + [p for p in others if p != self.source]:
            name = os.path.basename(path)
            try:
                db = self.mod.Cpk(path)
                table = db.read_table("Player")
            except KeyError:
                continue                      # a pack without a player table
            except (OSError, ValueError) as exc:
                failed.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            hit = 0
            for pid, fields in edits.items():
                for i in table.find(str(pid)):
                    hit += 1
                    for key, value in fields.items():
                        if key.startswith("name:"):
                            table.set_name(i, **{key.split(":", 1)[1]: value})
                        else:
                            table.set(i, key, value)
            if hit:
                jobs.append((path, db, table, hit))
        # Write. A failure is reported in the status line and a dialog — a traceback in a signal
        # handler is a console nobody watches.
        for path, db, table, hit in jobs:
            name = os.path.basename(path)
            if not os.access(path, os.W_OK):  # no backup is worth spending on a doomed write
                failed.append(f"{name}: not writable")
                continue
            try:
                db.patch("Player", table.to_payload(), backup_dir=backup)
                back = self.mod.Cpk(path).read_table("Player")    # saved is not written
            except (OSError, KeyError, ValueError) as exc:
                failed.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            wrong = [pid for pid, fields in edits.items() if not self._reads_back(back, pid, fields)]
            if wrong:
                failed.append(f"{name}: {len(wrong)} player(s) did not read back")
            else:
                written.append(f"{name} ({hit})")
        if not jobs:
            if failed:
                self._status("could not write: " + "; ".join(failed))
                self._alert("Could not write the database", "; ".join(failed))
            else:
                self._status("no database copy carries these players — nothing written")
            return
        exes = VINTAGES[self.vintage]["exe"]
        exes = exes if isinstance(exes, tuple) else (exes,)
        running = any(subprocess.run(["pgrep", "-f", exe],
                                     capture_output=True).returncode == 0 for exe in exes)
        head = "game is running, it reloads the table when you restart it · " if running else ""
        if failed:
            kind = "partial save: " if written else "could not write: "
            self._status(f"{head}{kind}" + "; ".join(failed))
            self._alert("Save did not finish", f"{head}{kind}" + "; ".join(failed))
            return
        saved = sorted({i for i, _label in self.dirty})
        self.dirty.clear()
        self.originals.clear()                     # the game holds these now: no "was …" left
        self._refresh_cells(saved, edited=False)   # the STATE cell only repaints on a re-bind
        if self.index is not None:
            self._load(self.index)                 # and the form's fields drop their markers
        pruned = settings.prune_backups(backup, settings.load()["max_backups"])
        self._status(f"{head}saved {len(edits)} player(s) into " + ", ".join(written)
                     + f" · backups in {backup}" + (f" ({pruned} old pruned)" if pruned else "")
                     + (" · EDIT00000000 is present and overrides this file in game"
                        if settings.edit_save_files() else ""))
        self._update_bulk()

    def _reload(self):
        """Drop the pending edits and re-read the database from disk."""
        try:
            self.table = self.mod.Cpk(self.source).read_table("Player")
        except (KeyError, OSError, ValueError) as exc:
            self._status(f"reload failed: {exc}")
            return
        self.dirty.clear()
        self.originals.clear()
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.originals.clear()
        self.index = None
        self.rows = []
        self.store.remove_all()
        self.load_rows()
        self.populate()
        self._status("pending edits dropped, re-read from disk")

    def _push_step(self, changes):
        """One user action as one undo step — a bulk edit reverses in one go.

        This is where a field's pristine value is remembered, once, from the step itself: it is
        what the "was …" tooltip shows and what lets an edit that lands back on it stop being an
        edit. Reading the value after the write would remember the new one.
        """
        if not changes:
            return
        for index, key, old, _new in changes:
            self.originals.setdefault((index, key), old)
        self.undo_stack.append(changes)
        del self.undo_stack[:-100]
        self.redo_stack.clear()

    def _value(self, index, key):
        """The stored value of an edit key: a field label, or `name:slot`."""
        if key == "name:full":
            return self.table.name(index)
        if key == "name:shirt":
            return self.table.shirt(index)
        if key == "name:japanese":
            return self.table.japanese(index)
        return self.table.get(index, key)

    def _set_value(self, index, key, value):
        if key.startswith("name:"):
            self.table.set_name(index, **{key.split(":", 1)[1]: value})
        else:
            self.table.set(index, key, value)

    def _undo(self, *_a):
        """Ctrl+Z: reverse the last edit step."""
        if not self.undo_stack:
            self._status("nothing to undo")
            return True
        self._apply_step(self.undo_stack.pop(), back=True)
        return True

    def _redo(self, *_a):
        """Ctrl+Y / Ctrl+Shift+Z: reapply the step Ctrl+Z took back."""
        if not self.redo_stack:
            self._status("nothing to redo")
            return True
        self._apply_step(self.redo_stack.pop(), back=False)
        return True

    def _apply_step(self, step, back):
        """Move every change in a step to its other side and repaint the rows it touched."""
        if back:
            self.redo_stack.append(step)
        else:
            self.undo_stack.append(step)
        for index, key, old, new in step:
            self._set_value(index, key, old if back else new)
            self._mark_dirty(index, key, refresh=False)
        self._refresh_cells(sorted({index for index, _k, _o, _n in step}))
        if self.index is not None and any(index == self.index for index, _k, _o, _n in step):
            self._load(self.index)
        self._status(f"{'undone' if back else 'redone'} · {len(self.dirty)} pending edit(s) "
                     f"· Save writes it")

    def _mark_dirty(self, index, label, refresh=True):
        """Record a pending edit and repaint its row: a mutated Row paints nothing by itself.

        A field that comes back to the value the game holds (an undo, a retyped number) stops
        counting as an edit.
        """
        original = self.originals.get((index, label))
        if original is not None and original == self._value(index, label):
            self.dirty.discard((index, label))
        else:
            self.dirty.add((index, label))
        self._ovr_cache.pop(index, None)          # the rating is built from these values
        if index == self.index:
            self._paint_ovr(index)
            self._paint_profile(index)
        if refresh:
            self._refresh_cells([index])          # the STATE cell only repaints on a re-bind
        else:
            self.rows[index].edited = True        # the bulk path re-splices them in one go
        self._status(f"{len(self.rows):,} players · {len(self.dirty)} pending edit(s) "
                     f"· Save writes it")
        if hasattr(self, "batch"):
            self._update_bulk()

    def _decorate(self, widget, key, rating=False):
        """A field's state: rating tint, the modified marker, the original in the tooltip."""
        base = getattr(widget, "_base_tooltip", None) or widget.get_tooltip_text() or key
        widget._base_tooltip = base
        if rating:
            for n in range(4):
                widget.remove_css_class(f"tier{n}")
            widget.add_css_class(f"tier{tier_of(widget_value(widget))}")
        original = self.originals.get((self.index, key))
        value = self._value(self.index, key) if self.index is not None else None
        if original is not None and original != value:
            widget.add_css_class("edited")
            widget.set_tooltip_text(f"{base} · was {original}")
        else:
            widget.remove_css_class("edited")
            widget.set_tooltip_text(base)

    def _on_ability_changed(self, spin, label):
        if self.loading or self.index is None:
            return
        value = int(spin.get_value())
        old = self.table.get(self.index, label)
        if old == value:
            return
        self.table.set(self.index, label, value)
        self._push_step([(self.index, label, old, value)])
        self._decorate(spin, label, rating=True)
        bar = self.abilities[label][1]
        if bar is not None:
            bar.set_value(value)
        self._mark_dirty(self.index, label)

    def _paint_ovr(self, index):
        rating = self.ovr(index)
        self.pills["OVR"].set_text(f"OVR {rating}")
        for n in range(4):
            self.pills["OVR"].remove_css_class(f"tier{n}")
        self.pills["OVR"].add_css_class(f"tier{tier_of(rating)}")

    def _paint_profile(self, index):
        """One bar per ability group: the mean of the abilities this release actually has."""
        if not getattr(self, "profile", None):
            return
        t = self.table
        for name, labels in ABILITY_GROUPS:
            have = [t.get(index, lab) for lab in labels if lab in self.mod.FIELD_BY_LABEL]
            value = round(sum(have) / len(have)) if have else 0
            bar, text = self.profile[name]
            bar.set_value(value)
            text.set_text(str(value) if value else "—")

    def ovr(self, index):
        """The player's reference rating, cached per record (see `overall_rating`)."""
        cache = getattr(self, "_ovr_cache", None)
        if cache is None:
            cache = self._ovr_cache = {}
        if index not in cache:
            cache[index] = overall_rating(self.table, index, self.mod)
        return cache[index]

    def _meta_text(self, index):
        """The line under the name: id, club, country, how many packs were searched."""
        pid = self.table.pid(index)
        rows = self.club_rows(pid)
        club = self.clubs.get(self.assign.team(rows[0]), "free agent") if rows else "free agent"
        number = self.assign.number(rows[0]) if rows else 0
        return (f"id {pid} · {club} (#{number}) · "
                f"{COUNTRIES.get(self.table.get(index, 'Nationality'), '')} · "
                f"{len(self.packs)} packs searched")

    def _on_bio_changed(self, spin, key):
        if self.loading or self.index is None:
            return
        value = widget_value(spin)
        old = self.table.get(self.index, key)
        if old == value:
            return
        self.table.set(self.index, key, value)
        self._push_step([(self.index, key, old, value)])
        self._decorate(spin, key)
        if key in ("Height", "Weight", "Age"):
            self.pills[key].set_text(f"{key} {int(spin.get_value())}")
        if key == "Nationality":               # the country name lives in the meta line
            self.meta.set_text(self._meta_text(self.index))
        self._mark_dirty(self.index, key)

    def _on_position_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        old = self.table.get(self.index, "Position")
        if old == dd.get_selected():
            return
        self.table.set(self.index, "Position", dd.get_selected())
        self._push_step([(self.index, "Position", old, dd.get_selected())])
        row = self.rows[self.index]
        row.pos = self.mod.POSITION_NAMES[dd.get_selected()]
        self._mark_dirty(self.index, "Position")

    def _on_style_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        old = self.table.get(self.index, "Playing Style")
        if old == dd.get_selected():
            return
        self.table.set(self.index, "Playing Style", dd.get_selected())
        self._push_step([(self.index, "Playing Style", old, dd.get_selected())])
        self._mark_dirty(self.index, "Playing Style")

    def _on_foot_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        old = self.table.get(self.index, "Foot")
        if old == dd.get_selected():
            return
        self.table.set(self.index, "Foot", dd.get_selected())
        self._push_step([(self.index, "Foot", old, dd.get_selected())])
        self.pills["Foot"].set_text("Foot " + ("Left" if dd.get_selected() else "Right"))
        self._mark_dirty(self.index, "Foot")

    def _chip_text(self, chip, active, name=None):
        """A chosen chip carries a tick; colour alone must not be the only difference."""
        text = chip.get_child()
        if text is None:
            return
        if name is None:
            name = getattr(chip, "_chip_name", None) or chip.get_tooltip_text() or ""
        chip._chip_name = name
        text.set_text(f"✓ {name}" if active else name)

    def _filter_chips(self):
        """Narrow the chip grid to the names that match the filter box."""
        query = self.chip_filter.get_text().strip().lower()
        for recorded, chip in self.chips.items():
            chip.set_visible(not query or query in recorded.lower())

    def _on_skill_toggled(self, chip, label):
        if self.loading or self.index is None:
            return
        value = 1 if chip.get_active() else 0
        old = self.table.get(self.index, label)
        if old == value:
            return
        self.table.set(self.index, label, value)
        self._push_step([(self.index, label, old, value)])
        self._chip_text(chip, bool(value))
        self._mark_dirty(self.index, label)

    def _name_limit(self):
        """The game's name slot in UTF-8 bytes — what it will actually keep."""
        return int(getattr(self.mod, "NAME_SLOT",
                           getattr(self.mod, "NAME_LEN",
                                   getattr(pesdb, "NAME_SLOT", 61)))) - 1

    def _on_name_changed(self, entry, slot):
        if self.loading or self.index is None:
            return
        text = entry.get_text()
        limit = self._name_limit()
        if len(text.encode("utf-8")) > limit:
            # the game stores a fixed slot and cuts the rest: say so instead of losing it quietly
            entry.add_css_class("invalid")
            entry.set_tooltip_text(f"too long — the game stores {limit} bytes and cuts the rest")
        else:
            entry.remove_css_class("invalid")
            entry.set_tooltip_text(f"up to {limit} bytes")
        old = self._value(self.index, "name:" + slot)
        if old == text:
            return
        self.table.set_name(self.index, **{slot: text})
        self._push_step([(self.index, "name:" + slot, old, text)])
        self._decorate(entry, "name:" + slot)
        row = self.rows[self.index]
        row.name = self.table.name(self.index)
        self._mark_dirty(self.index, f"name:{slot}")


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
    # Gtk.Application is single-instance: a check run must not take over an app the user has open,
    # so it registers an id of its own.
    app_id = "dev.pesforge.gtk4"
    if os.environ.get("PESFORGE_GTK_SELFTEST"):
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
            GLib.timeout_add(700, win.selftest)      # after the first frame

    app.connect("activate", on_activate)      # without this the app shows nothing and exits
    status = app.run([sys.argv[0]])
    return status or (1 if SELFTEST_FAILURES else 0)


if __name__ == "__main__":
    raise SystemExit(main())

"""PESForge in GTK4 — a working spike of the main screen, not a mockup.

Run:  python3 editor_gtk.py            (needs GTK4 via gi; no other dependency)

What is real here: the player list is the PES 2019 table the game actually reads
(`download/dt80_200E_x64.cpk`, or Data/dt10_x64.cpk when that pack is absent), read through
`pes2019.py`; every value in the form comes from the selected record; editing writes back into
that in-memory table; the search filters 16k rows through GtkFilterListModel; the ability bars
are Cairo on a GtkDrawingArea; the palette is `theme.tokens("amber")` turned into CSS.

What is deliberately not here (it is IO, and the Tk app already has it): saving a CPK, undo /
redo, the photo fetch, the PES 2021 and PES 2015 tabs. Save/Undo/Redo are inert on purpose —
judging the toolkit does not need a write path pointed at a real install.
"""
import json
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pes2015  # noqa: E402
import pes2019  # noqa: E402
import pesdb  # noqa: E402
import settings  # noqa: E402
import theme  # noqa: E402

SELFTEST_FAILURES = []            # filled by --selftest runs, turned into an exit code by main()
GAME = os.environ.get("PES2019_DIR") or pes2019.find_game_dir()
TOK = theme.tokens("amber")


def install_of(path):
    """The install a database CPK lives in, for finding the other copies a save must write."""
    folder = os.path.dirname(os.path.abspath(path))              # .../download or .../Data
    return os.path.dirname(folder) if os.path.basename(folder) in ("download", "Data") else folder


def probe_database(path):
    """(vintage, note) for a database CPK, judged by the shape of its Player table.

    Each vintage has its own container quirks, so the readers are tried in turn and the
    first that yields a whole table wins. The record size is the release's signature:
    180 bytes PES 2015, 188 PES 2019, 312 PES 2020/2021. Detection has to read the
    table: filenames moved between releases and a PES 2015 pack also ships a Player.bin.
    """
    attempts = (("pes2019", pes2019.REC, lambda q: pes2019.Cpk2019(q).read_raw("Player")[0]),
                ("pes2015", pes2015.REC, lambda q: pes2015.Cpk2015(q).read_raw("Player")[0]),
                ("pes2021", pesdb.REC, lambda q: pesdb.table_rows(pesdb.table_blob(q, "Player"))))
    missing, reasons = 0, []
    for vintage, rec, read in attempts:
        try:
            payload = read(path)
        except KeyError:                     # the container opened, it has no Player entry
            missing += 1
            continue
        except Exception as exc:
            reasons.append(f"{vintage} reader: {str(exc).splitlines()[0][:70]}")
            continue
        if payload and len(payload) % rec == 0:
            return vintage, f"{len(payload):,} bytes of player data, {len(payload) // rec:,} players"
        reasons.append(f"{vintage} reader returned {len(payload):,} bytes, not whole records")
    if missing == len(attempts):
        folder = os.path.dirname(os.path.abspath(path))
        try:
            others = [f for f in sorted(os.listdir(folder))
                      if f.endswith(".cpk") and ("dt10" in f or "dt80" in f)]
        except OSError:
            others = []
        note = "this CPK carries no Player table"
        if others:
            note += f"; in that folder try {', '.join(others[:3])}"
        return "no player table", note
    return "unreadable", reasons[0] if reasons else "no reader could open it"


def sibling_database(path, prefer=("dt10_x64.cpk",)):
    """A CPK in the same folder that does hold the Player table, for a pick that does not.

    PES 2019's `Data/` ships 40+ CPKs and only the dt00_x64/dt10_x64 pair carries the
    tables, so a pick from the right folder is often one entry away from a working
    database. Ranked: the base table, a mounted dt80 pack, then anything else.
    """
    folder = os.path.dirname(os.path.abspath(path))
    try:
        names = [f for f in os.listdir(folder) if f.endswith(".cpk")]
    except OSError:
        return ""
    game = install_of(path) if os.path.basename(folder) == "download" else ""
    packs = pes2019.mounted_packs(game) if game else []
    order = {name: i for i, name in enumerate(packs)}      # the last mounted pack wins

    def rank(name):
        if name in prefer:
            return (0, 0)                  # the base table the game reads first
        if name in order:
            return (1, len(order) - order[name])   # later in DpFileList overrides earlier
        return (2, 0) if name.startswith("dt80") else (3, 0)

    for name in sorted(names, key=lambda n: (rank(n), n)):
        if name == os.path.basename(path):
            continue
        try:
            if pes2019.Cpk2019(os.path.join(folder, name)).read_raw("Player")[0]:
                return os.path.join(folder, name)
        except Exception:
            continue
    return ""


def db_arg():
    """`--db <cpk>`: open straight into a database instead of the empty start."""
    args = sys.argv[1:]
    if "--db" in args and args.index("--db") + 1 < len(args):
        return args[args.index("--db") + 1]
    return ""


def default_database(game_dir):
    """The CPK the game itself loads from an install: the mounted pack, else the base table."""
    packs = pes2019.database_cpks(game_dir) if game_dir else []
    return next((p for p in reversed(packs) if p.endswith("dt80_200E_x64.cpk")),
                packs[0] if packs else "")


def field_range(label):
    """(low, high) for a numeric field: abilities stop at 99 even if their bits allow more."""
    _bit, width, add = pes2019.FIELD_BY_LABEL[label]
    hi = add + (1 << width) - 1
    if label in PesForgeGtk.ABILITY_ORDER:
        hi = min(hi, 99)
    return add, hi


def country_names():
    """code -> country name, from the table the PES 2021 side already ships."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "countries.json")
    try:
        with open(path, encoding="utf-8") as f:
            return {int(code): name for code, name in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


COUNTRIES = country_names()


# ---------------------------------------------------------------- CSS from the app's tokens
def css():
    t = TOK
    return f"""
    window {{ background-color: {t['bg']}; color: {t['fg']}; }}
    label {{ color: {t['fg']}; }}
    button, togglebutton, entry, spinbutton, dropdown, combobox, windowcontrols button {{
        background-image: none; box-shadow: none; }}
    scrolledwindow, viewport {{ background-color: transparent; }}
    columnview, columnview > listview, listview {{ background-color: {t['panel']}; }}
    columnview > listview > row {{ background-color: {t['panel']}; color: {t['fg']}; }}
    columnview > listview > row:hover {{ background-color: {t['hover']}; }}
    columnview > listview > row:selected {{ background-color: {t['sel']}; color: {t['fg']}; }}
    columnview > header > button {{ background-color: {t['panel2']}; background-image: none;
        border: none; border-radius: 0; color: {t['dim']}; font-size: 11px; font-weight: bold;
        padding: 3px 8px; min-height: 20px; }}
    columnview > header > button label {{ font-size: 11px; font-weight: bold; }}
    .dim {{ color: {t['dim']}; }}
    .hint {{ color: {t['hint']}; }}
    .brand {{ color: {t['accent']}; font-weight: bold; font-size: 17px; }}
    .section {{ color: {t['accent']}; font-weight: bold; font-size: 12px; }}
    .hero {{ font-size: 21px; font-weight: bold; }}
    .meta {{ color: {t['hint']}; font-size: 11px; }}
    .stat {{ font-size: 11px; font-weight: bold; color: {t['dim']}; }}

    .pane {{ background-color: {t['panel']}; border-radius: 6px; }}
    .pane-header {{ background-color: {t['panel2']}; }}
    .row {{ padding: 1px 8px; }}
    .row:selected {{ background-color: {t['sel']}; }}
    .row:hover {{ background-color: {t['hover']}; }}
    .head {{ background-color: {t['panel2']}; color: {t['dim']}; font-size: 11px;
             font-weight: bold; padding: 4px 8px; }}
    columnview {{ background-color: {t['panel']}; }}
    columnview > listview > row {{ padding: 0; }}
    columnview > listview > row:selected {{ background-color: {t['sel']}; }}

    entry, spinbutton, dropdown, combobox {{ background-color: {t['bg']}; color: {t['fg']};
        border: 1px solid {t['line']}; border-radius: 4px; min-height: 24px; padding: 0 6px; }}
    entry:focus, spinbutton:focus {{ border-color: {t['accent']}; outline: none; }}
    entry.search {{ background-color: {t['panel']}; border-radius: 5px; }}
    button {{ background-color: {t['panel2']}; color: {t['dim']}; border: 1px solid {t['line']};
        border-radius: 5px; padding: 4px 10px; min-height: 26px; font-size: 12px; }}
    button:hover {{ background-color: {t['hover']}; color: {t['fg']}; }}
    button.accent {{ background-color: {t['accent']}; color: {t['ink']}; font-weight: bold;
        border: none; padding: 4px 14px; }}
    button.accent:hover {{ background-color: {t['accent_hi']}; }}
    button.tab {{ background-color: transparent; border: none; color: {t['dim']};
        padding: 2px 8px; min-height: 22px; }}
    button.tab.active {{ background-color: {t['sel']}; color: {t['accent']}; }}
    button.tab.off {{ color: {t['bar']}; font-style: italic; }}
    .batch {{ background-color: {t['panel']}; border-top: 1px solid {t['line']};
        padding: 6px 4px; }}
    .batch spinbutton {{ min-width: 74px; }}
    columnview label.cell {{ color: {t['fg']}; }}

    .pill {{ background-color: {t['panel2']}; border: 1px solid {t['line']};
        border-radius: 999px; padding: 1px 10px; font-size: 11px; color: {t['fg']}; }}
    .chip {{ background-color: {t['panel2']}; border: 1px solid {t['line']};
        border-radius: 999px; padding: 1px 9px; font-size: 11px; color: {t['dim']};
        min-height: 20px; }}
    .chip:checked {{ background-color: {t['accent']}; color: {t['ink']};
        border-color: {t['accent']}; font-weight: bold; }}
    .footer {{ background-color: {t['bg']}; }}
    spinbutton text {{ background: none; color: {t['fg']}; font-size: 12px; }}
    spinbutton entry {{ border: none; background: none; min-height: 18px; }}
    spinbutton button {{ border: none; background-color: {t['panel']}; color: {t['dim']};
        min-height: 16px; padding: 0 2px; border-radius: 3px; }}
    .abar-track {{ border-radius: 3px; }}
    """


def install_css(display):
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(css().encode("utf-8"))
    except TypeError:                       # older bindings want (data, length)
        provider.load_from_data(css(), -1)
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


# ---------------------------------------------------------------- the row model
class Row(GObject.Object):
    """One list row: the values the view shows, plus the index into the player table."""

    __gtype_name__ = "PlayerRow"

    index = GObject.Property(type=int, default=0)
    pid = GObject.Property(type=int, default=0)
    name = GObject.Property(type=str, default="")
    club = GObject.Property(type=str, default="")
    pos = GObject.Property(type=str, default="")
    spd = GObject.Property(type=int, default=0)
    bc = GObject.Property(type=int, default=0)
    fin = GObject.Property(type=int, default=0)
    sta = GObject.Property(type=int, default=0)
    edited = GObject.Property(type=bool, default=False)

    @property
    def state(self):
        return "edited" if self.edited else ""


class AbilityBar(Gtk.DrawingArea):
    """The ability bar: a track and a fill, Cairo-drawn, repainted when the value changes."""

    def __init__(self, width=104):
        super().__init__()
        self.value = 40
        self.set_content_width(width)
        self.set_content_height(7)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_value(self, value):
        self.value = value
        self.queue_draw()

    @staticmethod
    def _round_rect(cr, x, y, w, h, r):
        r = min(r, h / 2, max(w, 0.1) / 2)
        cr.new_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 0)
        cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
        cr.arc(x + r, y + h - r, r, 1.5708, 3.14159)
        cr.arc(x + r, y + r, r, 3.14159, 4.71239)
        cr.close_path()

    def _draw(self, _area, cr, width, height):
        frac = max(0.0, min(1.0, (self.value - 40) / 59.0))
        track = Gdk.RGBA()
        Gdk.RGBA.parse(track, TOK["bar"])
        cr.set_source_rgba(track.red, track.green, track.blue, track.alpha)
        self._round_rect(cr, 0, 0, width, height, height / 2)
        cr.fill()
        if frac <= 0:
            return
        spec = TOK["accent"] if self.value >= 80 else (
            TOK["accent_lo"] if self.value >= 65 else TOK["bar_low"])
        paint = Gdk.RGBA()
        Gdk.RGBA.parse(paint, spec)
        cr.set_source_rgba(paint.red, paint.green, paint.blue, paint.alpha)
        self._round_rect(cr, 0, 0, max(width * frac, height), height, height / 2)
        cr.fill()


# ---------------------------------------------------------------- the window
class PesForgeGtk(Gtk.ApplicationWindow):
    def __init__(self, app, table=None, table_source=None, teams=None, assign=None):
        """Starts with no database: nothing is read until the user imports one."""
        super().__init__(application=app, title="PESForge GTK4")
        self.set_default_size(1504, 946)
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
        self.rows = []
        self.dirty = set()
        self.loading = True                     # until the first record is loaded
        self.packs = pes2019.database_cpks(self.game) if self.game else []
        self.assign_by_pid = {}
        if assign is not None:
            for i in range(len(assign)):
                self.assign_by_pid.setdefault(assign.player(i), []).append(i)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        root.append(self._header())
        root.append(self._tabs())
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self._empty_state(), "empty")
        root.append(self.stack)
        root.append(self._batch_bar())
        root.append(self._footer(table_source))

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._matches, None)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorted = Gtk.SortListModel(model=self.filtered)
        self.selection = Gtk.MultiSelection(model=self.sorted)
        self.selection.connect("selection-changed", self._on_select)
        if self.table is not None:               # a caller handed us a database (tests)
            self._build_panes()
            self.column_view.set_model(self.selection)
            self.load_rows()
            self.populate()

    # ---- header
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
        self.unload_btn.connect("clicked", lambda _b: self.unload_database())
        bar.append(self.unload_btn)
        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text("Search players, clubs, IDs…")
        self.search.connect("search-changed", lambda _e: self._refilter())
        self.search.set_sensitive(False)                         # nothing loaded to search yet
        bar.append(self.search)
        for label, cls in (("Undo", None), ("Redo", None), ("Discard", None), ("Save to game", "accent")):
            b = Gtk.Button(label=label)
            b.set_sensitive(False) if label in ("Undo", "Redo") else None
            b.set_tooltip_text("not wired in the spike") if label != "Discard" else None
            if cls:
                b.add_css_class(cls)
            if label == "Discard":
                b.connect("clicked", lambda _b: self._load(self.index))
            bar.append(b)
        return bar

    # ---- tab row
    def _tabs(self):
        """Players is the only view this build has; the rest say so instead of doing nothing."""
        bar = Gtk.Box(spacing=2)
        bar.set_margin_start(14)
        bar.set_margin_bottom(4)
        for label, here in (("Players", True), ("Clubs", False), ("PES 2015", False)):
            b = Gtk.Button(label=label)
            b.add_css_class("tab")
            b.add_css_class("active" if here else "off")
            if not here:
                b.set_tooltip_text("only in the Tk build (editor.py)")
            b.connect("clicked", self._on_tab, label, here)
            bar.append(b)
        return bar

    def _on_tab(self, _button, label, here):
        if here:
            self._status(f"Players · {len(self.rows):,} players")
        else:
            self._status(f"{label}: not in the GTK4 build yet, the Tk editor has it")

    # ---- empty state: shown until a database is imported
    def _empty_state(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        title = Gtk.Label(label="No database loaded")
        title.add_css_class("hero")
        box.append(title)
        hint = Gtk.Label(label="Import the CPK the game reads:\n"
                               "Data/dt10_x64.cpk, or a mounted download/dt80_*_x64.cpk",
                         justify=Gtk.Justification.CENTER)
        hint.add_css_class("hint")
        box.append(hint)
        button = Gtk.Button(label="Import database…")
        button.add_css_class("accent")
        button.set_halign(Gtk.Align.CENTER)
        button.connect("clicked", lambda _b: self.import_database())
        box.append(button)
        version = Gtk.Label(label=f"PES 2019 map · {len(self.BULK_FIELDS)} batch fields · "
                                  f"record size {pes2019.REC} bytes")
        version.add_css_class("hint")
        box.append(version)
        return box

    def _build_panes(self):
        """Build the list / form / bio panes and make them the visible page."""
        panes = Gtk.Box(spacing=12)
        panes.set_margin_start(12)
        panes.set_margin_end(12)
        panes.set_margin_bottom(8)
        panes.set_vexpand(True)
        panes.append(self._list())
        panes.append(self._form())
        panes.append(self._bio())
        old = self.stack.get_child_by_name("data")
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(panes, "data")
        self.stack.set_visible_child_name("data")

    def _list(self):
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.add_css_class("pane")
        pane.set_size_request(468, -1)
        pane.set_overflow(Gtk.Overflow.HIDDEN)
        self.column_view = Gtk.ColumnView()
        self.column_view.set_show_column_separators(False)
        self.column_view.set_show_row_separators(False)
        self.column_view.add_css_class("rows")
        check_factory = Gtk.SignalListItemFactory()
        check_factory.connect("setup", self._check_setup)
        check_factory.connect("bind", self._check_bind)
        check_col = Gtk.ColumnViewColumn(title="✓", factory=check_factory)
        check_col.set_fixed_width(36)
        self.column_view.append_column(check_col)
        for title, width, attr, align, sortable in (
                ("ID", 48, "pid", 1.0, True), ("NAME", 158, "name", 0.0, True),
                ("CLUB", 106, "club", 0.0, True), ("POS", 36, "pos", 0.5, False),
                ("SPD", 40, "spd", 1.0, True), ("BC", 34, "bc", 1.0, True),
                ("FIN", 36, "fin", 1.0, True), ("STA", 36, "sta", 1.0, True)):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._cell_setup)
            factory.connect("bind", self._cell_bind, attr)
            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_fixed_width(width)
            col.set_resizable(True)
            if sortable:
                sorter = Gtk.CustomSorter.new(self._compare, attr)
                col.set_sorter(sorter)
            self.column_view.append_column(col)
        state_factory = Gtk.SignalListItemFactory()
        state_factory.connect("setup", self._cell_setup)
        state_factory.connect("bind", self._cell_bind, "state")
        state_col = Gtk.ColumnViewColumn(title="STATE", factory=state_factory)
        state_col.set_fixed_width(56)
        self.column_view.append_column(state_col)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.column_view)
        scroller.set_vexpand(True)
        pane.append(scroller)
        self.count = Gtk.Label(label="", xalign=0)
        self.count.add_css_class("hint")
        self.count.set_margin_start(8)
        self.count.set_margin_bottom(6)
        pane.append(self.count)
        return pane

    def _cell_setup(self, _f, item):
        label = Gtk.Label(xalign=0)
        label.add_css_class("cell")
        click = Gtk.GestureClick()
        click.connect("released", self._row_clicked, item)
        label.add_controller(click)
        item.set_child(label)

    def _cell_bind(self, _f, item, attr):
        row = item.get_item()
        label = item.get_child()
        value = getattr(row, attr)
        label.set_text(f"{value:,}" if attr == "pid" else str(value))
        label.set_xalign(1.0 if attr in ("pid", "spd", "bc", "fin", "sta") else 0.0)
        label.set_width_chars(0)

    def _check_setup(self, _f, item):
        check = Gtk.CheckButton()
        check.set_halign(Gtk.Align.CENTER)
        click = Gtk.GestureClick()
        click.connect("released", self._row_clicked, item)
        check.add_controller(click)
        item.set_child(check)

    def _check_bind(self, _f, item):
        check = item.get_child()
        self.binding = True
        check.set_active(self.selection.is_selected(item.get_position()))
        self.binding = False
        check.connect("toggled", self._on_check, item)

    def _on_check(self, check, item):
        if self.binding:
            return                                  # set_active() above, not a click
        pos = item.get_position()
        if check.get_active():
            self.selection.select_item(pos, False)
        elif self.selection.is_selected(pos):
            self.selection.unselect_item(pos)

    def selected_indexes(self):
        """Record indexes of every ticked row, in table order.

        shortcut: asks the model per position (Gtk.Bitset has no indexed getter here),
        which is fine at 16k rows; move to the bitset iterator if the list grows.
        """
        out = []
        for pos in range(self.selection.get_n_items()):
            if self.selection.is_selected(pos):
                row = self.sorted.get_item(pos)
                if row is not None:
                    out.append(row.index)
        return out

    def _compare(self, a, b, attr):
        av, bv = getattr(a, attr), getattr(b, attr)
        return (av > bv) - (av < bv)

    def _form(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("pane")
        box.set_hexpand(True)
        box.set_margin_top(0)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m, side in ((12, "start"), (12, "end"), (10, "top"), (12, "bottom")):
            inner.set_margin_start(m) if side == "start" else (
                inner.set_margin_end(m) if side == "end" else
                inner.set_margin_top(m) if side == "top" else inner.set_margin_bottom(m))
        box.append(inner)

        inner.append(self._section("PLAYER", ""))
        self.hero = Gtk.Label(label="no player", xalign=0)
        self.hero.add_css_class("hero")
        inner.append(self.hero)
        self.meta = Gtk.Label(label="", xalign=0)
        self.meta.add_css_class("meta")
        inner.append(self.meta)

        pills = Gtk.Box(spacing=6)
        self.pills = {}
        for key in ("Height", "Weight", "Age", "Foot"):
            p = Gtk.Label(label=f"{key} —")
            p.add_css_class("pill")
            pills.append(p)
            self.pills[key] = p
        inner.append(pills)

        self.name_entries = {}
        for key, off in (("Name", "full"), ("Shirt Name", "shirt"), ("Japanese Name", "japanese")):
            row = Gtk.Box(spacing=8)
            lab = Gtk.Label(label=key, xalign=1)
            lab.add_css_class("dim")
            lab.set_size_request(120, -1)
            row.append(lab)
            e = Gtk.Entry()
            e.set_hexpand(True)
            e.connect("changed", self._on_name_changed, off)
            row.append(e)
            self.name_entries[key] = e
            inner.append(row)

        inner.append(self._section("ABILITIES", "40 - 99"))
        self.abilities = {}
        grid = Gtk.Grid(column_spacing=18, row_spacing=1)
        for n, label in enumerate(self.ABILITY_ORDER):
            col = 0 if n % 2 == 0 else 1
            r = n // 2
            cell = Gtk.Box(spacing=8)
            lab = Gtk.Label(label=label, xalign=1)
            lab.add_css_class("dim")
            lab.set_size_request(136, -1)
            cell.append(lab)
            spin = Gtk.SpinButton.new_with_range(40, 99, 1)
            spin.set_size_request(84, -1)
            spin.connect("value-changed", self._on_ability_changed, label)
            cell.append(spin)
            bar = AbilityBar()
            cell.append(bar)
            self.abilities[label] = (spin, bar)
            grid.attach(cell, col, r, 1, 1)
        pos_cell = Gtk.Box(spacing=8)
        pos_lab = Gtk.Label(label="Position", xalign=1)
        pos_lab.add_css_class("dim")
        pos_lab.set_size_request(136, -1)
        pos_cell.append(pos_lab)
        self.position = Gtk.DropDown.new_from_strings(pes2019.POSITION_NAMES)
        self.position.connect("notify::selected", self._on_position_changed)
        pos_cell.append(self.position)
        grid.attach(pos_cell, 1, (len(self.ABILITY_ORDER) + 1) // 2, 1, 1)
        inner.append(grid)
        return box

    BULK_OPS = (("Set to", "set"), ("Add", "add"), ("Subtract", "sub"))
    BULK_FIELDS = ("Attacking Prowess", "Ball Control", "Dribbling", "Low Pass", "Lofted Pass",
                   "Finishing", "Header", "Place Kicking", "Swerve", "Speed", "Explosive Power",
                   "Kicking Power", "Jump", "Physical Contact", "Unwavering Balance", "Stamina",
                   "Defensive Prowess", "Ball Winning", "Goalkeeping", "GK Catch", "Clearing",
                   "Reflexes", "Coverage", "Age", "Height", "Weight", "Form",
                   "Weak Foot Usage", "Weak Foot Accuracy", "Injury Resistance", "Reputation")

    ABILITY_ORDER = (
        "Attacking Prowess", "Ball Control", "Dribbling", "Low Pass", "Lofted Pass", "Finishing",
        "Header", "Place Kicking", "Swerve", "Speed", "Explosive Power", "Kicking Power", "Jump",
        "Physical Contact", "Unwavering Balance", "Stamina", "Defensive Prowess", "Ball Winning",
        "Goalkeeping", "GK Catch", "Clearing", "Reflexes", "Coverage")

    def _bio(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.add_css_class("pane")
        outer.set_size_request(406, -1)
        outer.set_vexpand(True)
        box = Gtk.ScrolledWindow()          # the safety net, like the app's own scroll panes
        box.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.set_vexpand(True)
        outer.append(box)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        box.set_child(inner)
        inner.append(self._section("BIO", ""))

        self.bio = {}
        rows = (("Height", 150, 215, "Height"), ("Weight", 55, 110, "Weight"),
                ("Age", 15, 50, "Age"), ("Nationality", 0, 511, "Nationality"),
                ("Form", 1, 8, "Form"), ("Weak Foot Usage", 1, 4, "Weak Foot Usage"),
                ("Weak Foot Accuracy", 1, 4, "Weak Foot Accuracy"),
                ("Injury Resistance", 1, 4, "Injury Resistance"),
                ("Reputation", 1, 8, "Reputation"))
        grid = Gtk.Grid(column_spacing=8, row_spacing=2)
        for n, (label, lo, hi, key) in enumerate(rows):
            lab = Gtk.Label(label=label, xalign=1)
            lab.add_css_class("dim")
            lab.set_size_request(122, -1)
            spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
            spin.set_hexpand(True)
            spin.connect("value-changed", self._on_bio_changed, key)
            self.bio[key] = spin
            grid.attach(lab, 0, n, 1, 1)
            if key == "Nationality":
                box2 = Gtk.Box(spacing=8)
                box2.append(spin)
                self.country = Gtk.Label(label="", xalign=0)
                self.country.add_css_class("dim")
                box2.append(self.country)
                grid.attach(box2, 1, n, 1, 1)
            else:
                grid.attach(spin, 1, n, 1, 1)
        inner.append(grid)

        style_row = Gtk.Box(spacing=8)
        lab = Gtk.Label(label="Playing Style", xalign=1)
        lab.add_css_class("dim")
        lab.set_size_request(122, -1)
        style_row.append(lab)
        self.style = Gtk.DropDown.new_from_strings(pes2019.STYLE_NAMES)
        self.style.set_hexpand(True)
        self.style.connect("notify::selected", self._on_style_changed)
        style_row.append(self.style)
        inner.append(style_row)

        foot_row = Gtk.Box(spacing=8)
        flab = Gtk.Label(label="Foot", xalign=1)
        flab.add_css_class("dim")
        flab.set_size_request(122, -1)
        foot_row.append(flab)
        self.foot = Gtk.DropDown.new_from_strings(("Right", "Left"))
        self.foot.connect("notify::selected", self._on_foot_changed)
        foot_row.append(self.foot)
        inner.append(foot_row)

        inner.append(self._section("SKILLS & COM STYLES", "click to toggle"))
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(3)
        flow.set_min_children_per_line(3)
        flow.set_homogeneous(False)       # natural widths: equal columns clipped labels
        flow.set_row_spacing(4)
        flow.set_column_spacing(4)
        self.chips = {}
        # the modules key their skill/style flags as "Skill: X" / "Style: X"; the chip shows
        # the bare name, the table lookup uses the label
        for label in ([f"Style: {n}" for n in pes2019.COM_STYLES]
                      + [f"Skill: {n}" for n in pes2019.SKILLS]):
            chip = Gtk.ToggleButton(label=label.split(": ", 1)[1])
            chip.add_css_class("chip")
            chip.connect("toggled", self._on_skill_toggled, label)
            child = Gtk.FlowBoxChild()
            child.set_child(chip)
            flow.append(child)
            self.chips[label] = chip
        inner.append(flow)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        inner.append(spacer)
        return outer

    def _section(self, text, hint):
        box = Gtk.Box(spacing=8)
        lab = Gtk.Label(label=f"▼ {text}", xalign=0)
        lab.add_css_class("section")
        box.append(lab)
        if hint:
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
        self.batch.append(self.batch_label)
        self.bulk_field = Gtk.DropDown.new_from_strings(self.BULK_FIELDS)
        self.bulk_field.set_tooltip_text("field to change")
        self.bulk_field.set_size_request(186, -1)
        self.batch.append(self.bulk_field)
        self.bulk_op = Gtk.DropDown.new_from_strings([label for label, _ in self.BULK_OPS])
        self.bulk_op.set_selected(1)
        self.batch.append(self.bulk_op)
        self.bulk_amount = Gtk.SpinButton.new_with_range(-99, 99, 1)
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

    def _update_bulk(self):
        n = len(self.selected_indexes())
        self.batch.set_visible(bool(n))
        if n:
            self.batch_label.set_text(f"{n} selected · {len(self.dirty)} pending edit(s)")

    def _bulk_apply(self):
        """Set, add or subtract one field across every ticked player, clamped to its range."""
        indexes = self.selected_indexes()
        if not indexes:
            return
        label = self.BULK_FIELDS[self.bulk_field.get_selected()]
        op = self.BULK_OPS[self.bulk_op.get_selected()][1]
        amount = int(self.bulk_amount.get_value())
        lo, hi = field_range(label)
        for i in indexes:
            old = self.table.get(i, label)
            new = amount if op == "set" else (old + amount if op == "add" else old - amount)
            self.table.set(i, label, max(lo, min(hi, new)))
            self._mark_dirty(i, label)
            self.rows[i].edited = True
        self._refresh_cells(indexes)
        if self.index in indexes:
            self._load(self.index)              # the form shows the current player: reload it
        done = f"set {label} to {max(lo, min(hi, amount))}" if op == "set" \
            else f"{op} {amount:+d} {label}"
        self._status(f"{done} on {len(indexes)} player(s) · "
                     f"{len(self.dirty)} pending edit(s) · Save writes it")
        self._update_bulk()

    def _refresh_cells(self, indexes):
        """Re-bind the edited rows: a GtkColumnView binds an item once, so writing a property
        repaints nothing. Swapping the row object in the store is what refreshes the cell."""
        seen, batch = set(), []
        for i in sorted(indexes):
            if i in seen:
                continue
            seen.add(i)
            old = self.rows[i]
            t = self.table
            batch.append((i, Row(index=i, pid=old.pid, name=t.name(i), club=old.club,
                                 pos=pes2019.POSITION_NAMES[t.get(i, "Position")],
                                 spd=t.get(i, "Speed"), bc=t.get(i, "Ball Control"),
                                 fin=t.get(i, "Finishing"), sta=t.get(i, "Stamina"),
                                 edited=True)))
        for i, row in batch:
            self.rows[i] = row
            self.store.splice(i, 1, [row])      # replace in place: the view re-binds that cell
        self._update_count()

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
        self.status = Gtk.Label(label=("PES 2019 · " + os.path.basename(str(source))) if source
                                else "no database loaded · Import database… to pick one", xalign=0)
        self.status.add_css_class("hint")
        self.status.set_hexpand(True)
        bar.append(self.status)
        apply_btn = Gtk.Button(label="Apply to player")
        apply_btn.set_sensitive(False)
        apply_btn.set_tooltip_text("the form writes as you edit it; Save commits")
        bar.append(apply_btn)
        self.save_btn = Gtk.Button(label="Save to game")
        self.save_btn.add_css_class("accent")
        self.save_btn.connect("clicked", lambda _b: self._save())
        self.save_btn.set_sensitive(False)
        bar.append(self.save_btn)
        return bar

    def _status(self, text):
        """Write the footer line. Everything it reports names the database it is about."""
        self.status.set_text(f"{self.provenance} · {text}" if self.provenance else text)

    # ---- import / unload
    def unload_database(self):
        """Drop the loaded database and go back to the empty window (the Tk build's Unmount)."""
        if self.table is None:
            self._status("nothing loaded")
            return
        if self.dirty:
            self._status(f"{len(self.dirty)} unsaved edit(s) — save or Revert before unloading")
            return
        self.table = self.teams = self.assign = None
        self.source = self.source_name = ""
        self.game = None
        self.packs = []
        self.clubs, self.assign_by_pid = {}, {}
        self.rows, self.dirty = [], set()
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
        """Pick the CPK the game reads; what it is decides whether this build can open it."""
        chooser = Gtk.FileChooserNative(title="Import a database CPK", transient_for=self,
                                        action=Gtk.FileChooserAction.OPEN)
        filters = Gtk.FileFilter()
        filters.set_name("CRI CPK database")
        filters.add_pattern("*.cpk")
        chooser.add_filter(filters)
        start = (self.game and (os.path.join(self.game, "download")
                                if os.path.isdir(os.path.join(self.game, "download"))
                                else os.path.join(self.game, "Data"))) or GAME or None
        if start and os.path.isdir(start):
            chooser.set_current_folder(Gio.File.new_for_path(start))
        chooser.connect("response", self._on_import_response)
        chooser.show()

    def _on_import_response(self, chooser, response):
        path = chooser.get_file().get_path() if response == Gtk.ResponseType.ACCEPT else None
        chooser.destroy()
        if path:
            self.load_database(path)

    def load_database(self, path):
        """Probe a CPK and load it, or say precisely why this build will not."""
        vintage, note = probe_database(path)
        name = os.path.basename(path)
        swapped = ""
        if vintage == "no player table":
            better = sibling_database(path)
            if better:                       # the folder has the tables, this entry does not
                swapped, path = name, better
                name = os.path.basename(path)
                vintage, note = probe_database(path)
        if vintage != "pes2019":
            self._status(f"{name}: {vintage} — {note}. This build edits PES 2019 "
                         f"({pes2019.REC}-byte records); the Tk editor has the rest.")
            return False
        db = pes2019.Cpk2019(path)
        self.table = db.read_table("Player")
        self.teams = pes2019.TeamTable(*db.read_raw("Team"))
        self.assign = pes2019.AssignmentTable(*db.read_raw("PlayerAssignment"))
        self.source = path
        self.source_name = name
        self.game = install_of(path)
        self.packs = pes2019.database_cpks(self.game)
        self.clubs = {self.teams.id(i): self.teams.name(i) for i in range(len(self.teams))}
        self.assign_by_pid = {}
        for i in range(len(self.assign)):
            self.assign_by_pid.setdefault(self.assign.player(i), []).append(i)
        self.dirty.clear()
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
        self.provenance = name + (f" ({swapped} had no player table)" if swapped else "")
        self._status(f"PES 2019 · {len(self.rows):,} players · {note}")
        return True

    # ---- data
    def load_rows(self):
        t = self.table
        for i in range(len(t)):
            pid = t.pid(i)
            club = ""
            rows = self.assign_by_pid.get(pid)
            if rows:
                club = self.clubs.get(self.assign.team(rows[0]), "")
            self.rows.append(Row(index=i, pid=pid, name=t.name(i), club=club,
                                 pos=pes2019.POSITION_NAMES[t.get(i, "Position")],
                                 spd=t.get(i, "Speed"), bc=t.get(i, "Ball Control"),
                                 fin=t.get(i, "Finishing"), sta=t.get(i, "Stamina")))

    def populate(self):
        """Fill the list model — run after load_rows(), so the store matches the table."""
        for r in self.rows:
            self.store.append(r)
        self._update_count()
        GLib.idle_add(self._select_first)

    def _matches(self, item, _data):
        q = self.search.get_text().strip().lower() if hasattr(self, "search") else ""
        if not q:
            return True
        if q.isdigit():
            return str(item.pid).startswith(q)
        return q in item.name.lower() or q in item.club.lower()

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_count()
        GLib.idle_add(self._select_first)

    def _update_count(self):
        shown = self.filtered.get_n_items()
        total = self.store.get_n_items()
        query = self.search.get_text().strip()
        if not shown and total and query:
            self.count.set_text(f'no player matches "{query}" — clear the search to see all {total:,}')
        else:
            self.count.set_text(f"{shown:,} of {total:,} players — type to narrow")

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
        print(f"SELFTEST OK players={total} selected={self.index} "
              f"abilities={len(self.abilities)} chips={len(self.chips)} "
              f"source={self.source_name}", flush=True)
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
            self.selection.select_item(3, False)      # GTK reports this one as pos=0
            checks.append(("a selection change shows that row", shown(), 3))
            self.selection.select_item(1, False)      # False = add, leaving the rest ticked
            self.selection.select_item(2, False)
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

    def _selftest_cycle(self):
        """Import PES 2019, unload, import SP Football Life, unload, import PES 2015."""
        try:
            spfl = settings.database_path(os.environ.get("SPFL_DIR", "")) \
                if os.environ.get("SPFL_DIR") else ""
            legs = (("pes2019", default_database(os.environ.get("PES2019_DIR", "")), "loaded"),
                    ("spfl", spfl, "refused"),
                    ("pes2015", os.path.join(os.environ.get("PES2015_DIR", ""), "Data", "dt10.cpk"),
                     "refused"))
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
                if not loaded and name in ("spfl", "pes2015") and name.replace("spfl", "pes2021") not in self.status.get_text():
                    return self._selftest_fail(f"{name}: refused without naming the vintage: "
                                               f"{self.status.get_text()!r}")
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

    def _selftest_write_run(self):
        import hashlib
        path = self.source
        before = hashlib.sha256(open(path, "rb").read()).hexdigest()
        backup_dir = settings.backups_dir()
        pre = set(pes2019.backup_list(backup_dir))
        picks = [0, 1, 2]
        for pos in picks:
            self.selection.select_item(pos, False)
        label = "Speed"
        self.bulk_field.set_selected(self.BULK_FIELDS.index(label))
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
        self._save()
        print(f"WRITE stage=saved status={self.status.get_text()!r}", flush=True)
        if "saved" not in self.status.get_text():
            return self._selftest_fail(f"save said {self.status.get_text()!r}")
        back = pes2019.Cpk2019(path).read_table("Player")
        for i, was in old.items():
            if not self._reads_back(back, back.pid(i), {label: min(99, was + 5)}):
                return self._selftest_fail(f"player {back.pid(i)}: {label} missing after save")
        fresh = [n for n in pes2019.backup_list(backup_dir) if n not in pre]
        if not fresh:
            return self._selftest_fail("the save took no backup")
        for name in fresh:
            pes2019.restore_backup(name)
        if hashlib.sha256(open(path, "rb").read()).hexdigest() != before:
            return self._selftest_fail("restoring the backups did not return the original bytes")
        print(f"SELFTEST WRITE OK players={len(self.rows)} edited={len(picks)} "
              f"backups={len(fresh)} restored=1", flush=True)
        self.close()
        return False

    def _selftest_fail(self, why):
        SELFTEST_FAILURES.append(why)
        print(f"SELFTEST FAIL {why}", flush=True)
        self.close()
        return False

    def _select_first(self):
        """Load row 0 into the form: nothing is selected up front, and a selection that does not
        change emits no signal, so the form would otherwise sit empty."""
        if self.selection.get_n_items() and not self.selection.is_selected(0):
            self.selection.select_item(0, False)
        if self.selection.get_n_items() and self.index is None:
            row = self.sorted.get_item(0)
            if row is not None:
                self._load(row.index)
        self._update_bulk()
        return False

    def _on_select(self, _sel, _pos, _n):
        """A single-row selection (arrows, a plain click, a tick) drives the form.

        `_pos` is *not* the row that was clicked — GTK reports the previous position — which
        is why the form used to lag a click behind. With several rows ticked the form stays on
        the current player; a click inside any row is handled by that row's own gesture.
        """
        indexes = self.selected_indexes()
        if len(indexes) == 1 and indexes[0] != self.index:
            self._load(indexes[0])
        self._update_bulk()

    def _row_clicked(self, _gesture, _n_press, _x, _y, item):
        """Show the row that was clicked, whatever the selection was doing."""
        self.show_row(item.get_position())

    def show_row(self, pos):
        """Show the player at view position `pos` (a click, or a check being ticked)."""
        row = self.sorted.get_item(pos) if pos != Gtk.INVALID_LIST_POSITION else None
        if row is not None and row.index != self.index:
            self._load(row.index)

    # ---- form <-> record
    def _load(self, index):
        if index is None:
            return
        self.index = index
        t = self.table
        self.loading = True
        self.hero.set_text(t.name(index))
        pid = t.pid(index)
        rows = self.assign_by_pid.get(pid)
        club = self.clubs.get(self.assign.team(rows[0]), "free agent") if rows else "free agent"
        number = self.assign.number(rows[0]) if rows else 0
        self.meta.set_text(f"id {pid} · {club} (#{number}) · {len(self.packs)} packs searched")
        for key in ("Height", "Weight", "Age"):
            self.pills[key].set_text(f"{key} {t.get(index, key)}")
        self.pills["Foot"].set_text("Foot " + ("Left" if t.get(index, "Foot") else "Right"))
        self.name_entries["Name"].set_text(t.name(index))
        self.name_entries["Shirt Name"].set_text(t.shirt(index))
        self.name_entries["Japanese Name"].set_text(t.japanese(index))
        for label, (spin, bar) in self.abilities.items():
            value = t.get(index, label)
            spin.set_value(value)
            bar.set_value(value)
        self.position.set_selected(t.get(index, "Position"))
        for key, spin in self.bio.items():
            spin.set_value(t.get(index, key))
        self.country.set_text(COUNTRIES.get(t.get(index, "Nationality"), ""))
        self.style.set_selected(min(t.get(index, "Playing Style"), len(pes2019.STYLE_NAMES) - 1))
        self.foot.set_selected(t.get(index, "Foot"))
        for label, chip in self.chips.items():
            chip.set_active(bool(t.get(index, label)))
        self.loading = False
        self._status(f"{len(self.rows):,} players · showing {t.name(index)} (id {pid})"
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
        if not GAME:
            self._status("no install to write to (set PES2019_DIR)")
            return
        edits = self._edited_values()
        backup = settings.backups_dir()
        written, failed = [], []
        others = pes2019.database_cpks(self.game) if self.game else []
        for path in [self.source] + [p for p in others if p != self.source]:
            try:
                db = pes2019.Cpk2019(path)
                table = db.read_table("Player")
            except (KeyError, OSError, ValueError):
                continue                      # a pack without a player table
            hit = 0
            for pid, fields in edits.items():
                for i in table.find(str(pid)):
                    hit += 1
                    for key, value in fields.items():
                        if key.startswith("name:"):
                            table.set_name(i, **{key.split(":", 1)[1]: value})
                        else:
                            table.set(i, key, value)
            if not hit:
                continue                      # this copy does not carry the player
            db.patch("Player", table.to_payload(), backup_dir=backup)
            back = pes2019.Cpk2019(path).read_table("Player")     # saved is not written
            wrong = [pid for pid, fields in edits.items() if not self._reads_back(back, pid, fields)]
            name = os.path.basename(path)
            if wrong:
                failed.append(f"{name}: {len(wrong)} player(s) did not read back")
            else:
                written.append(f"{name} ({hit})")
        running = subprocess.run(["pgrep", "-f", "PES2019.exe"], capture_output=True).returncode == 0
        head = "game is running, it reloads the table when you restart it · " if running else ""
        if failed:
            self._status(f"{head}partial save: " + "; ".join(failed))
            return
        if not written:
            self._status("no database copy carries these players — nothing written")
            return
        self.dirty.clear()
        for r in self.rows:
            r.edited = False
        self._status(f"{head}saved {len(edits)} player(s) into " + ", ".join(written)
                     + f" · backups in {backup}")
        self._update_bulk()

    def _reload(self):
        """Drop the pending edits and re-read the database from disk."""
        try:
            self.table = pes2019.Cpk2019(self.source).read_table("Player")
        except (KeyError, OSError, ValueError) as exc:
            self._status(f"reload failed: {exc}")
            return
        self.dirty.clear()
        self.index = None
        self.rows = []
        self.store.remove_all()
        self.load_rows()
        self.populate()
        self._status("pending edits dropped, re-read from disk")

    def _mark_dirty(self, index, label):
        self.dirty.add((index, label))
        self.rows[index].edited = True
        self._status(f"{len(self.rows):,} players · {len(self.dirty)} pending edit(s) "
                     f"· Save writes it")
        if hasattr(self, "batch"):
            self._update_bulk()

    def _on_ability_changed(self, spin, label):
        if self.loading or self.index is None:
            return
        value = int(spin.get_value())
        self.table.set(self.index, label, value)
        self.abilities[label][1].set_value(value)
        self._mark_dirty(self.index, label)

    def _on_bio_changed(self, spin, key):
        if self.loading or self.index is None:
            return
        self.table.set(self.index, key, int(spin.get_value()))
        if key in ("Height", "Weight", "Age"):
            self.pills[key].set_text(f"{key} {int(spin.get_value())}")
        if key == "Nationality":
            self.country.set_text(COUNTRIES.get(int(spin.get_value()), ""))
        self._mark_dirty(self.index, key)

    def _on_position_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        self.table.set(self.index, "Position", dd.get_selected())
        row = self.rows[self.index]
        row.pos = pes2019.POSITION_NAMES[dd.get_selected()]
        self._mark_dirty(self.index, "Position")

    def _on_style_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        self.table.set(self.index, "Playing Style", dd.get_selected())
        self._mark_dirty(self.index, "Playing Style")

    def _on_foot_changed(self, dd, _p):
        if self.loading or self.index is None:
            return
        self.table.set(self.index, "Foot", dd.get_selected())
        self.pills["Foot"].set_text("Foot " + ("Left" if dd.get_selected() else "Right"))
        self._mark_dirty(self.index, "Foot")

    def _on_skill_toggled(self, chip, label):
        if self.loading or self.index is None:
            return
        self.table.set(self.index, label, 1 if chip.get_active() else 0)
        self._mark_dirty(self.index, label)

    def _on_name_changed(self, entry, slot):
        if self.loading or self.index is None:
            return
        text = entry.get_text()
        self.table.set_name(self.index, **{slot: text})
        row = self.rows[self.index]
        row.name = self.table.name(self.index)
        self._mark_dirty(self.index, f"name:{slot}")


def check():
    """`--check`: report what the app would open, without opening a window."""
    print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
          f"PyGObject {'.'.join(str(v) for v in gi.version_info)}")
    if not GAME:
        print("PES 2019 install: not found (set PES2019_DIR)")
        return 1
    packs = pes2019.database_cpks(GAME)
    source = next((p for p in reversed(packs) if p.endswith("dt80_200E_x64.cpk")), packs[0] if packs else "")
    print(f"PES 2019 install: {GAME}")
    print(f"player table    : {source or 'none found'}")
    return 0 if source else 1


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
        win.present()
        mode = os.environ.get("PESFORGE_GTK_SELFTEST", "").lower()
        # --db, and the checks that need data, open one; a normal start waits for the user
        want = db_arg() or (default_database(GAME) if mode in ("1", "2", "write") else "")
        if want:
            win.load_database(want)
        if mode:
            GLib.timeout_add(700, win.selftest)      # after the first frame

    app.connect("activate", on_activate)      # without this the app shows nothing and exits
    status = app.run([sys.argv[0]])
    return status or (1 if SELFTEST_FAILURES else 0)


if __name__ == "__main__":
    raise SystemExit(main())

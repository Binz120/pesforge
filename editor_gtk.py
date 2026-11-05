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
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pes2019  # noqa: E402
import theme  # noqa: E402

SELFTEST_FAILURES = []            # filled by --selftest runs, turned into an exit code by main()
GAME = os.environ.get("PES2019_DIR") or pes2019.find_game_dir()
TOK = theme.tokens("amber")


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
    def __init__(self, app, table, table_source, teams, assign):
        super().__init__(application=app, title="PESForge — GTK4 spike")
        self.set_default_size(1504, 946)
        self.table = table
        self.teams = teams
        self.assign = assign
        self.clubs = {}
        self.index = None
        self.rows = []
        self.dirty = set()
        self.loading = True                     # until the first record is loaded
        self.packs = pes2019.database_cpks(GAME) if GAME else []
        self.assign_by_pid = {}
        for i in range(len(assign)):
            self.assign_by_pid.setdefault(assign.player(i), []).append(i)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        root.append(self._header())
        root.append(self._tabs())
        root.append(self._body())
        root.append(self._footer(table_source))

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self._matches, None)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorted = Gtk.SortListModel(model=self.filtered)
        self.selection = Gtk.SingleSelection(model=self.sorted)
        self.selection.set_autoselect(True)
        self.selection.connect("selection-changed", self._on_select)
        self.column_view.set_model(self.selection)

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
        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text("Search players, clubs, IDs…")
        self.search.connect("search-changed", lambda _e: self._refilter())
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
        bar = Gtk.Box(spacing=2)
        bar.set_margin_start(14)
        bar.set_margin_bottom(4)
        for label, active in (("Players  16,762", False), ("Clubs  529", False),
                              ("PES 2015", False), ("PES 2019  16,762", True)):
            b = Gtk.Button(label=label)
            b.add_css_class("tab")
            if active:
                b.add_css_class("active")
            bar.append(b)
        return bar

    # ---- list | form | bio
    def _body(self):
        body = Gtk.Box(spacing=12)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_bottom(8)
        body.set_vexpand(True)
        body.append(self._list())
        body.append(self._form())
        body.append(self._bio())
        return body

    def _list(self):
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.add_css_class("pane")
        pane.set_size_request(468, -1)
        pane.set_overflow(Gtk.Overflow.HIDDEN)
        self.column_view = Gtk.ColumnView()
        self.column_view.set_show_column_separators(False)
        self.column_view.set_show_row_separators(False)
        self.column_view.add_css_class("rows")
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
        item.set_child(label)

    def _cell_bind(self, _f, item, attr):
        row = item.get_item()
        label = item.get_child()
        value = getattr(row, attr)
        label.set_text(f"{value:,}" if attr == "pid" else str(value))
        label.set_xalign(1.0 if attr in ("pid", "spd", "bc", "fin", "sta") else 0.0)
        label.set_width_chars(0)

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

    def _footer(self, source):
        bar = Gtk.Box(spacing=8)
        bar.add_css_class("footer")
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        bar.set_margin_top(4)
        bar.set_margin_bottom(10)
        revert = Gtk.Button(label="Revert")
        revert.connect("clicked", lambda _b: self._load(self.index))
        bar.append(revert)
        self.status = Gtk.Label(label=f"PES 2019 · loading {source} …", xalign=0)
        self.status.add_css_class("hint")
        self.status.set_hexpand(True)
        bar.append(self.status)
        apply_btn = Gtk.Button(label="Apply to player")
        apply_btn.set_sensitive(False)
        bar.append(apply_btn)
        save = Gtk.Button(label="Save to game")
        save.add_css_class("accent")
        save.set_sensitive(False)
        bar.append(save)
        return bar

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
        self.count.set_text(f"{shown:,} of {total:,} players — type to narrow")

    def selftest(self):
        """`PESFORGE_GTK_SELFTEST=1`: prove the screen built against real data, then quit.

        Guards the two failures a traceback never shows: an empty detail pane beside a selected
        row, and a list model that does not match the table behind it.
        """
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
        self.search.set_text("")
        self._refilter()
        if self.filtered.get_n_items() != total:
            return self._selftest_fail("clearing the search did not restore the list")
        print(f"SELFTEST OK players={total} selected={self.index} "
              f"abilities={len(self.abilities)} chips={len(self.chips)}", flush=True)
        self.close()
        return False

    def _selftest_fail(self, why):
        SELFTEST_FAILURES.append(why)
        print(f"SELFTEST FAIL {why}", flush=True)
        self.close()
        return False

    def _select_first(self):
        """Load the selected row — autoselect already picks row 0, and re-selecting it emits
        no selection-changed, so the form would otherwise sit empty."""
        pos = self.selection.get_selected()
        if pos == Gtk.INVALID_LIST_POSITION and self.selection.get_n_items():
            self.selection.set_selected(0)
            pos = self.selection.get_selected()
        row = self.selection.get_selected_item() if pos != Gtk.INVALID_LIST_POSITION else None
        if row is not None and self.index is None:
            self._load(row.index)
        return False

    def _on_select(self, _sel, _pos, _n):
        print("DBG on_select pos=", _pos, flush=True)
        row = self.selection.get_selected_item()
        if row is not None:
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
        self.status.set_text(
            f"PES 2019 · {len(self.rows):,} players · showing {t.name(index)} (id {pid})"
            + (f" · {len(self.dirty)} edited" if self.dirty else ""))

    def _mark_dirty(self, index, label):
        self.dirty.add((index, label))
        self.status.set_text(f"PES 2019 · {len(self.rows):,} players · "
                             f"{len(self.dirty)} unsaved field edit(s) — this spike writes nothing")

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
    app = Gtk.Application(application_id="dev.pesforge.gtkspike")

    def on_activate(_app):
        if not GAME:
            win = Gtk.ApplicationWindow(application=_app, title="PESForge — no install found")
            win.set_child(Gtk.Label(label="No PES 2019 install found (set PES2019_DIR)."))
            win.present()
            return
        packs = pes2019.database_cpks(GAME)
        source = next((p for p in reversed(packs) if p.endswith("dt80_200E_x64.cpk")), packs[0])
        db = pes2019.Cpk2019(source)
        table = db.read_table("Player")
        teams = pes2019.TeamTable(*db.read_raw("Team"))
        assign = pes2019.AssignmentTable(*db.read_raw("PlayerAssignment"))
        install_css(Gdk.Display.get_default())
        win = PesForgeGtk(_app, table, os.path.basename(source), teams, assign)
        win.clubs = {teams.id(i): teams.name(i) for i in range(len(teams))}
        win.load_rows()
        win.populate()
        win.present()
        if os.environ.get("PESFORGE_GTK_SELFTEST"):
            GLib.timeout_add(700, win.selftest)      # after the first frame

    app.connect("activate", on_activate)      # without this the app shows nothing and exits
    status = app.run([sys.argv[0]])
    return status or (1 if SELFTEST_FAILURES else 0)


if __name__ == "__main__":
    raise SystemExit(main())

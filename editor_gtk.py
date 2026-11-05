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
in the Tk build (`editor.py`), which this fork keeps.
"""
import json
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pes2015  # noqa: E402
import pes2019  # noqa: E402
import pes2021  # noqa: E402
import pesdb  # noqa: E402
import settings  # noqa: E402
import theme  # noqa: E402

SELFTEST_FAILURES = []            # filled by --selftest runs, turned into an exit code by main()
GAME = os.environ.get("PES2019_DIR") or pes2019.find_game_dir()
TOK = theme.tokens("amber")

# One entry per release this build can edit. Everything the panel shows derives from the
# vintage's map module — the widgets are the same, only the field lists and offsets differ.
VINTAGES = {
    "pes2021": {"label": "SP Football Life / PES 2021", "module": pes2021, "env": "SPFL_DIR",
                "cfg_key": "game_dir", "exe": ("FL_2027.exe", "PES2021.exe"),
                "game_file": os.path.join("download", "data_s2627.cpk")},
    "pes2019": {"label": "PES 2019", "module": pes2019, "env": "PES2019_DIR",
                "cfg_key": "pes2019_dir", "exe": ("PES2019.exe",),
                "game_file": os.path.join("Data", "dt10_x64.cpk")},
    "pes2015": {"label": "PES 2015", "module": pes2015, "env": "PES2015_DIR",
                "cfg_key": "pes2015_dir", "exe": ("PES2015.exe",),
                "game_file": os.path.join("Data", "dt10.cpk")},
}
EDITABLE = tuple(VINTAGES)


def game_dir(vintage):
    """One release's install: its environment variable, the remembered folder, then a search."""
    spec = VINTAGES[vintage]
    found = os.environ.get(spec["env"]) or settings.load().get(spec["cfg_key"], "")
    return found if found and os.path.isdir(found) else (spec["module"].find_game_dir() or "")


def vintage_database(vintage):
    """The CPK the game itself loads for a release: the file it names, else what it ships."""
    game = game_dir(vintage)
    if not game:
        return ""
    named = os.path.join(game, VINTAGES[vintage]["game_file"])
    if os.path.isfile(named):
        return named
    packs = VINTAGES[vintage]["module"].database_cpks(game)
    return packs[0] if packs else named


def dim_label(text, width, chars):
    """A fixed-width right-aligned field label.

    `max_width_chars` matters as much as the size request: a Label's *natural* width is its whole
    text, so without the cap a long label ("Defensive Awareness") pushes its value box right and
    every row's box lands somewhere else — the column looks ragged even though it is a grid.
    """
    lab = Gtk.Label(label=text, xalign=1)
    lab.add_css_class("dim")
    lab.set_size_request(width, -1)
    lab.set_max_width_chars(chars)
    lab.set_ellipsize(Pango.EllipsizeMode.END)
    lab.set_tooltip_text(text)
    return lab


def slim_spin(spin):
    """Hide a spinbutton's steppers (they cost ~60px of width each, and typing/scrolling works).

    The compact layouts have three fields per row and no room for the buttons; the batch tray
    keeps its own steppers, and the wide layout keeps every one of them.
    """
    child = spin.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            child.set_visible(False)
        child = child.get_next_sibling()


def ellipsizing_dropdown(items, chars=12):
    """A DropDown whose shown value elides.

    Its natural width is otherwise the longest item in the list ("Extra Frontman"), which makes
    that cell wider than every value box beside it and the whole column ragged.
    """
    dd = Gtk.DropDown.new_from_strings(items)
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        lab = Gtk.Label(xalign=0)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        lab.set_max_width_chars(chars)
        item.set_child(lab)

    def bind(_f, item):
        entry = item.get_item()
        item.get_child().set_text(entry.get_string() if entry is not None else "")

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    dd.set_factory(factory)
    return dd


def ability_order(mod):
    """The abilities in the order the map lists them (23 for PES 2019, 19 for PES 2015)."""
    return tuple(mod.ABILITIES)


def bulk_fields(mod):
    """The tray's fields: every ability, then the bio numbers the release actually carries."""
    bios = ("Age", "Height", "Weight", "Form", "Weak Foot Usage", "Weak Foot Accuracy",
            "Injury Resistance", "Reputation")
    return ability_order(mod) + tuple(n for n in bios if n in mod.FIELD_BY_LABEL)


def field_range(mod, label):
    """(low, high) for a numeric field: abilities stop at 99 even if their bits allow more."""
    _bit, width, add = mod.FIELD_BY_LABEL[label]
    hi = add + (1 << width) - 1
    if label in ability_order(mod):
        hi = min(hi, 99)
    return add, hi


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


def cpk_files(folder):
    """Every `*.cpk` worth probing for a folder pick, from the folder and its neighbours.

    A picked folder is usually one of three things: the install itself, its `Data/`, or its
    `download/`. PES 2019 and PES 2015 keep their tables in `Data/`, SP Football Life keeps its
    real database in `download/` — so the walk takes the folder, those two subfolders, and the
    same pair one level down (`<install>/Data`, `<install>/download`) so picking the install root
    works as well. Nothing deeper: the point is one pick, not a disk scan.
    """
    root = os.path.abspath(folder)
    dirs = [root, os.path.join(root, "Data"), os.path.join(root, "download")]
    try:
        children = sorted(os.listdir(root))
    except OSError:
        children = []
    for child in children:
        for sub in ("Data", "download"):
            dirs.append(os.path.join(root, child, sub))
    out = []
    for one in dirs:
        try:
            names = sorted(os.listdir(one))
        except OSError:
            continue
        for name in names:
            path = os.path.join(one, name)
            if name.lower().endswith(".cpk") and path not in out:
                out.append(path)
    return out


def rank_cpk(path, packs=()):
    """Which CPK to open first: the install's own database, else the pack the game loads."""
    name = os.path.basename(path)
    if name.startswith("data_s"):         # SP Football Life's real database
        return (0, 0)
    if name in packs:                     # DpFileList order: the later entries override earlier
        return (1, len(packs) - list(packs).index(name))
    if name in ("dt10_x64.cpk", "dt10.cpk"):
        return (2, 0)
    if name.startswith("dt80"):           # a data pack with no DpFileList to order it
        return (3, 0)
    return (4, 0)                         # dt00 audio/4K stubs, smkdb, anything else


def pack_order(files):
    """The DpFileList order of the install these candidates live in, when there is one."""
    for path in files:
        game = install_of(path)
        packs = pes2019.mounted_packs(game) if game else []
        if packs:
            return packs
    return ()


def probe_candidates(files, packs=()):
    """(found, vintage, note) over a ranked CPK candidate list — the first release this build
    can open, or empties.

    The walk is capped: a game folder holds 40+ CPKs and the ranking puts the plausible ones
    first, so opening all of them to say "no" would just make the user wait.
    """
    tried = 0
    for path in sorted(files, key=lambda p: rank_cpk(p, packs)):
        if tried >= PROBE_LIMIT:
            break
        tried += 1
        vintage, note = probe_database(path)
        if vintage in VINTAGES:
            return path, vintage, note
    return "", "", ""


def find_database(folder):
    """(path, vintage, note) for a folder: the database inside it, best candidate first.

    Probing decides, never the filename. SP Football Life's `Data/dt10_x64.cpk` is an 86 KB stub
    with no player table, so naming the file a folder "should" hold points at nothing and the
    Load button stays dead. A folder with no database of its own is then answered by the install
    around it (its `Data/` holds 47 CPKs, its `download/` holds the real one).
    """
    files = cpk_files(folder)
    found, vintage, note = probe_candidates(files, pack_order(files))
    install = install_of(files[0]) if files else ""
    if install and os.path.abspath(install) == os.path.abspath(folder):
        install = ""                      # this *is* the install: nothing further to try
    if not found and install:
        ifiles = cpk_files(install)
        found, vintage, note = probe_candidates(ifiles, pack_order(ifiles))
    if found:
        return found, vintage, note
    looked = ", ".join(sorted({os.path.dirname(p) or p for p in files})) or os.path.abspath(folder)
    note = f"no CPK with a player table in {looked}"
    if install:
        note += f", nor in {install}"
    return "", "no player table", note


def describe_pick(path):
    """(details, can_load, target) for a file or folder pick: what it holds, then what it points at.

    A pick that cannot be opened is answered with the file that should have been picked — the
    folder's own database, or the install's (`Data/` next to SP Football Life's `download/`).
    """
    if os.path.isdir(path):
        found, vintage, note = find_database(path)
        details = [f"folder: {os.path.abspath(path)}"]
        if found:
            details += [f"found: {os.path.relpath(found, os.path.abspath(path))}",
                        f"detected: {VINTAGE_NAMES.get(vintage, vintage)} — {note}"]
        else:
            details.append(f"detected: {VINTAGE_NAMES.get(vintage, vintage)} — {note}")
        return details, vintage in VINTAGES, found
    details, can_load = describe_database(path)
    if can_load:
        return details, True, path
    # no table in the pick: answer with the file that should have been picked — the folder's own
    # database, or the install's (find_database looks at both)
    folder = os.path.dirname(os.path.abspath(path))
    found, vintage, note = find_database(folder)
    if found:
        return ([f"picked: {os.path.basename(path)} — no player table",
                 f"found: {os.path.relpath(found, folder)}",
                 f"detected: {VINTAGE_NAMES.get(vintage, vintage)} — {note}"],
                vintage in VINTAGES, found)
    return details, False, ""


VINTAGE_NAMES = {"pes2019": "PES 2019", "pes2015": "PES 2015",
                 "pes2021": "SP Football Life / PES 2021"}
PROBE_LIMIT = 12          # CPKs to open before calling a folder empty: the ranking puts the
                          # real candidates first, and a game folder holds 40+ entries
NOT_READ = ("Reads CRI CPK databases and edits PES 2021 / SP Football Life (312-byte), PES 2019 "
            "(188-byte) and PES 2015 (180-byte) records. EDIT0000 option files, .ted/.ced "
            "containers and spreadsheets are not read at all.")


def describe_database(path):
    """(details, can_load) for a pick: what it holds, then whether Load will do anything.

    The release is read off the tables, so there is nothing here to choose — the point of
    the dialog is to show the detection before the main window changes under you.
    """
    vintage, note = probe_database(path)
    details = [f"file: {os.path.basename(path)}",
               f"detected: {VINTAGE_NAMES.get(vintage, vintage)} — {note}"]
    can_load = vintage in VINTAGES
    if can_load:
        mod = VINTAGES[vintage]["module"]
        try:
            teams = mod.TeamTable(*mod.Cpk(path).read_raw("Team"))
            details.append(f"contents: {len(teams):,} clubs, {mod.REC}-byte player records")
        except Exception:
            pass
    return details, can_load


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


def import_start_folder(last=""):
    """Where the file chooser opens: where you last picked, else your games, else home.

    Deliberately not a release's install — that made every import start in PES 2019's
    folder and forced a walk out of it to reach SP Football Life or PES 2015.
    """
    if last and os.path.isdir(last):
        return last
    games = os.path.expanduser("~/Games")
    return games if os.path.isdir(games) else os.path.expanduser("~")


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
    .brand {{ color: {t['accent']}; font-weight: bold; font-size: 15px; }}
    .section {{ color: {t['accent']}; font-weight: bold; font-size: 11px; }}
    .hero {{ font-size: 17px; font-weight: bold; }}
    .meta {{ color: {t['hint']}; font-size: 10px; }}
    .stat {{ font-size: 10px; font-weight: bold; color: {t['dim']}; }}

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
        border: 1px solid {t['line']}; border-radius: 4px; min-height: 20px; padding: 0 5px; }}
    entry:focus, spinbutton:focus {{ border-color: {t['accent']}; outline: none; }}
    entry.search {{ background-color: {t['panel']}; border-radius: 5px; }}
    spinbutton, spinbutton entry, spinbutton button {{ min-width: 0; }}
    button {{ background-color: {t['panel2']}; color: {t['dim']}; border: 1px solid {t['line']};
        border-radius: 5px; padding: 2px 8px; min-height: 22px; font-size: 12px; }}
    button:hover {{ background-color: {t['hover']}; color: {t['fg']}; }}
    button.accent {{ background-color: {t['accent']}; color: {t['ink']}; font-weight: bold;
        border: none; padding: 2px 10px; }}
    button.accent:hover {{ background-color: {t['accent_hi']}; }}
    button.accent:disabled {{ background-color: {t['panel2']}; color: {t['hint']}; }}
    .batch {{ background-color: {t['panel']}; border-top: 1px solid {t['line']};
        padding: 3px 4px; }}
    .batch spinbutton {{ min-width: 66px; }}
    columnview label.cell {{ color: {t['fg']}; }}

    .pill {{ background-color: {t['panel2']}; border: 1px solid {t['line']};
        border-radius: 999px; padding: 0 8px; font-size: 10px; color: {t['fg']}; }}
    .chip {{ background-color: {t['panel2']}; border: 1px solid {t['line']};
        border-radius: 999px; padding: 0 3px; font-size: 8px; color: {t['dim']};
        min-height: 15px; min-width: 0; }}
    .chip:checked {{ background-color: {t['accent']}; color: {t['ink']};
        border-color: {t['accent']}; font-weight: bold; }}
    .footer {{ background-color: {t['bg']}; }}
    /* dropdown and menu popups are separate widgets: without this they come up in Adwaita's
       light colours, a white box with black text on top of a dark app */
    popover, popover.background, popover > arrow, popover > contents {{
        background-color: {t['panel2']}; color: {t['fg']}; border: 1px solid {t['line']};
        box-shadow: none; }}
    popover listview, popover listview > row, popover row, popover cell {{
        background-color: {t['panel2']}; color: {t['fg']}; }}
    popover listview > row:hover, popover row:hover {{ background-color: {t['hover']}; }}
    popover listview > row:selected, popover row:selected {{
        background-color: {t['sel']}; color: {t['fg']}; }}
    popover label, popover cell label {{ color: {t['fg']}; }}
    popover scrollbar {{ background-color: {t['panel']}; }}
    menu, menuitem, popover.menu, popover.menu > contents {{
        background-color: {t['panel2']}; color: {t['fg']}; }}
    menuitem:hover, popover.menu modelbutton:hover {{ background-color: {t['hover']}; }}
    spinbutton text {{ background: none; color: {t['fg']}; font-size: 11px; }}
    spinbutton entry {{ border: none; background: none; min-height: 16px; }}
    spinbutton button {{ border: none; background-color: {t['panel']}; color: {t['dim']};
        min-height: 7px; min-width: 10px; padding: 0; border-radius: 3px; }}
    .abar-track {{ border-radius: 3px; }}
    """


def system_theme():
    """'light' or 'dark' as the desktop asks for it.

    PESFORGE_THEME wins (tests, and a manual override); then GTK's own preference, which modern
    GTK syncs from the freedesktop portal; then the portal directly; and the app's dark palette
    when nothing answers. The app used to paint itself dark whatever the desktop said, which is
    why a light session showed a dark window with a white dropdown in it.
    """
    want = (os.environ.get("PESFORGE_THEME") or "").strip().lower()
    if want in ("light", "dark"):
        return want
    try:
        st = Gtk.Settings.get_default()
        if st is not None:
            return "dark" if st.get_property("gtk-application-prefer-dark-theme") else "light"
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
    global TOK, _PROVIDER
    TOK = theme.tokens(theme.choice(settings.load()), mode or system_theme())
    if _PROVIDER is None:
        _PROVIDER = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, _PROVIDER,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    try:
        _PROVIDER.load_from_data(css().encode("utf-8"))
    except TypeError:                       # older bindings want (data, length)
        _PROVIDER.load_from_data(css(), -1)


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

        zone = Gtk.Label(label="Drop a CPK or a game folder here", justify=Gtk.Justification.CENTER)
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
                                       "this build edits PES 2019 and PES 2015 tables")

    def _load(self):
        if self.path:
            self.app.load_database(self.path)
        self.close()


class PesForgeGtk(Gtk.ApplicationWindow):
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
        self.narrow = True                        # the compact grids: 3 columns, no value bars
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

    def _size_panes(self, width, height=0):
        """Point the page: side by side, or list-on-top when the editor cannot fit beside it.

        The editor needs ~586px of width and ~950px of height for all of its fields; below either,
        the list gives way (it is a strip above, and the editor scrolls) — the columns still line
        up, which is what "aligned" means here.
        """
        if width <= 1 or not hasattr(self, "_list_pane"):
            return False
        stacked = width < 910 or 0 < height < 950
        total = width - 16                          # the two 8px margins
        # Side by side, the editor's grids need ~630px: the list takes the rest, capped at 590
        # (ten columns want ~586) and floored so its rows still make sense.
        room = max(260, total - 648)
        list_w = max(260, min(590, int(total * 0.32), room))
        want = ("v", -1) if stacked else ("h", list_w)
        if getattr(self, "_paned", None) != want:
            self._paned = want
            self._panes.set_orientation(
                Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
            self._list_pane.set_size_request(-1, 130) if stacked else \
                self._list_pane.set_size_request(list_w, -1)
            self._editor.set_vexpand(True)
        # Columns appear as the pane can pay for them: a stacked strip has the whole window,
        # side by side the list shows its stats, then club and position — never squeezed stubs.
        room = 10 ** 6 if stacked else list_w
        if getattr(self, "_col_room", None) != room:
            self._col_room = room
            for col, need in getattr(self, "_col_min", ()):
                col.set_visible(room >= need)
            # the identity columns take the widths the pane can actually give them,
            # so a 4-digit id is never elided to "7,8…"
            for col, wide, tight in ((getattr(self, "_check_col", None), 36, 34),
                                     (getattr(self, "_cols", {}).get("ID"), 48, 56),
                                     (getattr(self, "_cols", {}).get("STATE"), 56, 50)):
                if col is not None:
                    col.set_fixed_width(tight if room < 440 else wide)
        return False

    def _list(self):
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.add_css_class("pane")
        pane.set_vexpand(True)
        pane.set_overflow(Gtk.Overflow.HIDDEN)
        self.column_view = Gtk.ColumnView()
        self._col_min = []                       # (column, pane width it needs to be shown at)
        self._cols = {}                          # title -> column, for the width policy
        self.column_view.set_show_column_separators(False)
        self.column_view.set_show_row_separators(False)
        self.column_view.add_css_class("rows")
        check_factory = Gtk.SignalListItemFactory()
        check_factory.connect("setup", self._check_setup)
        check_factory.connect("bind", self._check_bind)
        check_col = Gtk.ColumnViewColumn(title="✓", factory=check_factory)
        check_col.set_fixed_width(36)
        self.column_view.append_column(check_col)
        self._check_col = check_col
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
            if title == "NAME":
                col.set_expand(True)             # the name takes the slack the pane has
            if title in ("CLUB", "POS"):
                self._col_min.append((col, 586))     # ten columns want ~586px
            elif title in ("SPD", "BC", "FIN", "STA"):
                self._col_min.append((col, 440))     # the stats fit without club and position
            self._cols[title] = col
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
        # The three panes together want ~1970px, and a tiled window hands the app far less: each
        # pane scrolls on its own instead of squeezing the others' content out of view.
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(False)
        scroller.set_min_content_width(0)        # the pane's width is _size_panes' call, not the
        scroller.set_min_content_height(0)       # content's: otherwise one wide row sets it up
        scroller.set_child(self.column_view)
        scroller.set_vexpand(True)
        pane.append(scroller)
        self.count = Gtk.Label(label="", xalign=0)
        self.count.add_css_class("hint")
        self.count.set_ellipsize(Pango.EllipsizeMode.END)   # its text was the pane's minimum
        self.count.set_max_width_chars(30)                  # ...and its natural, too
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
        text = f"{value:,}" if attr == "pid" else str(value)
        label.set_text(text)
        # ten columns want more than the pane holds, so the view squeezes them: elide instead of
        # chopping the word, and keep the full value on hover
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_tooltip_text(text or None)
        label.set_xalign(1.0 if attr in ("pid", "spd", "bc", "fin", "sta") else 0.0)
        label.set_width_chars(0)

    def _check_setup(self, _f, item):
        check = Gtk.CheckButton()
        check.set_halign(Gtk.Align.CENTER)
        click = Gtk.GestureClick()
        click.connect("released", self._row_clicked, item)
        check.add_controller(click)
        # connected once per cell: connecting in bind() adds a handler per re-bind, so one
        # toggle would run the body as many times as the row has been shown
        check.connect("toggled", self._on_check, item)
        item.set_child(check)

    def _check_bind(self, _f, item):
        check = item.get_child()
        row = item.get_item()
        self.binding = True
        check.set_active(row is not None and row.index in self.checked)
        self.binding = False

    def _on_check(self, check, item):
        if self.binding:
            return                                  # set_active() above, not a click
        row = item.get_item()
        if row is not None:
            self._set_checked(row.index, check.get_active())

    def _set_checked(self, index, on):
        """Tick one table row into the bulk selection — ticks are the user's alone."""
        if on:
            self.checked.add(index)
        else:
            self.checked.discard(index)
        if 0 <= index < len(self.rows):
            # a cell paints on a re-bind only, so splice this row and keep its edited flag
            self._refresh_cells([index], edited=self.rows[index].edited)
        self._update_bulk()

    def selected_indexes(self):
        """Table indexes of every ticked row, in table order — the bulk pick, nothing else."""
        return sorted(self.checked)

    def _compare(self, a, b, attr):
        av, bv = getattr(a, attr), getattr(b, attr)
        return (av > bv) - (av < bv)

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
        for key in ("Height", "Weight", "Age", "Foot"):
            p = Gtk.Label(label=f"{key} —")
            p.add_css_class("pill")
            pills.append(p)
            self.pills[key] = p
        inner.append(pills)

        self.name_entries = {}
        slots = getattr(self.mod, "NAME_SLOTS",
                       (("Name", "full"), ("Shirt Name", "shirt"), ("Japanese Name", "japanese")))
        nrow = Gtk.Grid(column_spacing=10, row_spacing=0) if narrow else None
        for i, (key, off) in enumerate(slots):
            lab = dim_label(key, 72 if narrow else 96, 11 if narrow else 15)
            e = Gtk.Entry()
            e.set_hexpand(True)
            e.connect("changed", self._on_name_changed, off)
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

        inner.append(self._section("ABILITIES", "40 - 99"))
        self.abilities = {}
        cols = 3 if narrow else 2
        grid = Gtk.Grid(column_spacing=6, row_spacing=0)
        order = ability_order(self.mod)
        for n, label in enumerate(order):
            cell = Gtk.Box(spacing=6)
            cell.append(dim_label(label, 108, 19))
            spin = Gtk.SpinButton.new_with_range(40, 99, 1)
            spin.set_width_chars(4)          # without this the entry reserves ~112px
            spin.set_size_request(72, 28)    # one box size for every value control in the grid
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
        pos_cell.append(dim_label("Position", 108, 19))
        self.position = Gtk.DropDown.new_from_strings(self.mod.POSITION_NAMES)
        self.position.set_size_request(72, 28)                      # same box as the value fields
        self.position.set_hexpand(False)                            # or it outgrows its column
        self.position.connect("notify::selected", self._on_position_changed)
        pos_cell.append(self.position)
        grid.attach(pos_cell, len(order) % cols, len(order) // cols, 1, 1)
        inner.append(grid)
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
        rows = tuple((label,) + self.BIO_RANGES[label] + (label,)
                     for label in self.BIO_LABELS if label in self.mod.FIELD_BY_LABEL)
        # two fields per line: nine rows in one column is what made this pane tall enough to scroll
        pairs = []
        val_w = 90 if getattr(self, "narrow", False) else 128    # one box size for spins and dropdowns
        for label, lo, hi, key in rows:
            spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
            spin.set_width_chars(4)
            spin.set_size_request(val_w, 28)
            if getattr(self, "narrow", False):
                slim_spin(spin)
            spin.connect("value-changed", self._on_bio_changed, key)
            self.bio[key] = spin
            pairs.append((label, spin))   # nationality is a code; the country shows in the meta

        names = getattr(self.mod, "STYLE_NAMES", None)
        if names:
            self.style = ellipsizing_dropdown(names, 12)
            self.style.connect("notify::selected", self._on_style_changed)
        else:                                 # PES 2015 numbers its playing styles
            lo, hi = field_range(self.mod, "Playing Style")
            self.style = Gtk.SpinButton.new_with_range(lo, hi, 1)
            self.style.connect("value-changed", self._on_bio_changed, "Playing Style")
        dropdowns = [("Playing Style", self.style)]
        self.foot = ellipsizing_dropdown(("Right", "Left"), 8)
        self.foot.connect("notify::selected", self._on_foot_changed)
        dropdowns.append(("Foot", self.foot))

        grid = Gtk.Grid(column_spacing=6, row_spacing=1)
        ncols = 3 if getattr(self, "narrow", False) else 2   # three short rows beat six tall ones
        for i, (text, widget) in enumerate(pairs):
            lab = dim_label(text, 84 if ncols == 3 else 92, 14)
            widget.set_size_request(val_w, 28)   # one box size, so the columns line up
            widget.set_hexpand(False)
            widget.set_tooltip_text(widget.get_tooltip_text() or text)
            grid.attach(lab, (i % ncols) * 2, i // ncols, 1, 1)
            grid.attach(widget, (i % ncols) * 2 + 1, i // ncols, 1, 1)
        inner.append(grid)
        # outside the grid: a dropdown's own minimum is wider than the number boxes beside it.
        # Both share one line — stacked, they were the 17px that made the pane scroll.
        line = Gtk.Box(spacing=6)
        for text, widget in dropdowns:
            line.append(dim_label(text, 84, 14))
            widget.set_hexpand(True)
            line.append(widget)
        inner.append(line)

        inner.append(self._section("SKILLS & COM STYLES", "click to toggle"))
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
            chip.connect("toggled", self._on_skill_toggled, label)
            grid2.attach(chip, n % cols, n // cols, 1, 1)
            self.chips[label] = chip
        inner.append(grid2)
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        inner.append(spacer)
        return pane

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

    def _refresh_cells(self, indexes, edited=True):
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
                                 pos=self.position_name(t.get(i, "Position")),
                                 spd=t.get(i, "Speed"), bc=t.get(i, "Ball Control"),
                                 fin=t.get(i, "Finishing"), sta=t.get(i, "Stamina"),
                                 edited=edited)))
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

    def _status(self, text):
        """Write the footer line. Everything it reports names the database it is about."""
        self.status.set_text(f"{self.provenance} · {text}" if self.provenance else text)

    def _shortcuts(self):
        """Ctrl+S / Ctrl+Z / Ctrl+Y / Ctrl+F / Escape — the Tk build's keyboard set."""
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
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
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
        self.source = path
        self.source_name = name
        self.game = install_of(path)
        self.packs = self.mod.database_cpks(self.game)
        self.clubs = {self.teams.id(i): self.teams.name(i) for i in range(len(self.teams))}
        self.assign_by_pid = {}
        for i in range(len(self.assign)):
            self.assign_by_pid.setdefault(self.assign.player(i), []).append(i)
        self.dirty.clear()
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
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
        self._status(f"{VINTAGES[vintage]['label']} · {len(self.rows):,} players · {note}"
                     + (" · EDIT00000000 is present and overrides this file in game"
                        if settings.edit_save_files() else ""))
        return True

    # ---- data
    def load_rows(self):
        t = self.table
        for i in range(len(t)):
            pid = t.pid(i)
            rows = self.club_rows(pid)
            club = self.clubs.get(self.assign.team(rows[0]), "") if rows else ""
            self.rows.append(Row(index=i, pid=pid, name=t.name(i), club=club,
                                 pos=self.position_name(t.get(i, "Position")),
                                 spd=t.get(i, "Speed"), bc=t.get(i, "Ball Control"),
                                 fin=t.get(i, "Finishing"), sta=t.get(i, "Stamina")))

    def position_name(self, value):
        """The release's name for a stored position id; a stray value shows as itself."""
        names = self.mod.POSITION_NAMES
        return names[value] if 0 <= value < len(names) else str(value)

    def club_rows(self, pid):
        """The player's club assignment rows: national-team rows sit below `NATIONAL_MAX`.

        The country row often comes first, and "first row wins" then shows a nation where the
        club belongs (D. RANDOLPH read as IRELAND instead of TEESSIDE) — the same skew the
        PES 2021 editor fixes by skipping team ids under 100.
        """
        return [r for r in self.assign_by_pid.get(pid, ())
                if self.assign.team(r) > pesdb.NATIONAL_MAX]

    def populate(self):
        """Fill the list model — run after load_rows(), so the store matches the table.

        One `splice` for all rows: 26k `append()` calls were 5.3s of a 6.5s import, and the view
        only binds what is on screen anyway.
        """
        self.store.splice(0, self.store.get_n_items(), self.rows)
        self._update_count()
        self._load_first()

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
        self._load_first()

    def _update_count(self):
        shown = self.filtered.get_n_items()
        total = self.store.get_n_items()
        query = self.search.get_text().strip()
        if not shown and total and query:
            text = f'no player matches "{query}" — clear the search to see all {total:,}'
        else:
            text = f"{shown:,} of {total:,} players — type to narrow"
        self.count.set_text(text)
        self.count.set_tooltip_text(text)

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

    def _load_first(self):
        """Load row 0 into the form: the detail pane must not sit empty.

        It is *not* selected — the form follows the cursor, the bulk pick follows the ticks,
        and a ticked row must mean the user ticked it.
        """
        if self.index is None and self.sorted.get_n_items():
            row = self.sorted.get_item(0)
            if row is not None:
                self._load(row.index)
        self._update_bulk()
        return False

    def _on_select(self, sel, _pos, _n):
        """The current row drives the form: arrows and plain clicks move it.

        `_pos` is *not* the row that was clicked — GTK reports the previous position — so the
        row comes from the model instead. Ticks never touch this selection.
        """
        row = sel.get_selected_item()
        if row is not None and row.index != self.index:
            self._load(row.index)
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
        self.meta.set_text(self._meta_text(index))
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
            spin.set_value(t.get(index, key))
        style = t.get(index, "Playing Style")
        if hasattr(self.style, "set_selected"):          # 2019 names its styles
            self.style.set_selected(min(style, len(self.mod.STYLE_NAMES) - 1))
        else:                                            # 2015 numbers them
            self.style.set_value(style)
        self.foot.set_selected(t.get(index, "Foot"))
        for label, chip in self.chips.items():
            chip.set_active(bool(t.get(index, label)))
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
        self._refresh_cells(saved, edited=False)   # the STATE cell only repaints on a re-bind
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
        self.checked.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.index = None
        self.rows = []
        self.store.remove_all()
        self.load_rows()
        self.populate()
        self._status("pending edits dropped, re-read from disk")

    def _push_step(self, changes):
        """One user action as one undo step — a bulk edit reverses in one go, like the Tk build."""
        if not changes:
            return
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
        """Record a pending edit and repaint its row: a mutated Row paints nothing by itself."""
        self.dirty.add((index, label))
        if refresh:
            self._refresh_cells([index])          # the STATE cell only repaints on a re-bind
        else:
            self.rows[index].edited = True        # the bulk path re-splices them in one go
        self._status(f"{len(self.rows):,} players · {len(self.dirty)} pending edit(s) "
                     f"· Save writes it")
        if hasattr(self, "batch"):
            self._update_bulk()

    def _on_ability_changed(self, spin, label):
        if self.loading or self.index is None:
            return
        value = int(spin.get_value())
        old = self.table.get(self.index, label)
        if old == value:
            return
        self.table.set(self.index, label, value)
        self._push_step([(self.index, label, old, value)])
        bar = self.abilities[label][1]
        if bar is not None:
            bar.set_value(value)
        self._mark_dirty(self.index, label)

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
        value = int(spin.get_value())
        old = self.table.get(self.index, key)
        if old == value:
            return
        self.table.set(self.index, key, value)
        self._push_step([(self.index, key, old, value)])
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

    def _on_skill_toggled(self, chip, label):
        if self.loading or self.index is None:
            return
        value = 1 if chip.get_active() else 0
        old = self.table.get(self.index, label)
        if old == value:
            return
        self.table.set(self.index, label, value)
        self._push_step([(self.index, label, old, value)])
        self._mark_dirty(self.index, label)

    def _on_name_changed(self, entry, slot):
        if self.loading or self.index is None:
            return
        text = entry.get_text()
        old = self._value(self.index, "name:" + slot)
        if old == text:
            return
        self.table.set_name(self.index, **{slot: text})
        self._push_step([(self.index, "name:" + slot, old, text)])
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

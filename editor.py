#!/usr/bin/env python3
"""SP Football Life 2027 — player editor.

Reads the game's own player database out of download/data_s*.cpk, lets you change
attributes / positions / skills / identity, and writes the result as a Sider live-CPK
override so the game picks it up on the next launch.
"""
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
import tkinter.font as tkfont
import unicodedata
from tkinter import ttk, messagebox, filedialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pesdb
import settings

STATE_DIR = settings.state_dir()
VANILLA = os.path.join(STATE_DIR, "Player.vanilla.bin")
PORTRAIT_DIR = os.path.join(STATE_DIR, "portraits")
# pesmaster keeps one 128x128 face per PES player id, ~17 KB; fetched on first view, never bundled
PORTRAIT_URL = "https://www.pesmaster.com/pes-2021/graphics/players/player_{pid}.png"

# Team.bin name slots the game actually shows. The 24-byte "database" name at +788 is empty in
# all 745 FL27 rows, so the tab does not offer it and a save must not blank it.
CLUB_NAME_SLOTS = (("English", 368, 70), ("Short", 882, 10))
PHOTO_SIDE = 128        # pesmaster portraits are 128×128; the slot must not be smaller


def _icon_path():
    """The window/app icon; in a one-file build it is unpacked next to the binary."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "pesforge.png")


def bar_value(x, width):
    """Click position on an ability bar -> 40..99 (left edge 40, right edge 99)."""
    frac = max(0.0, min(1.0, x / max(1, width - 1)))
    return 40 + round(59 * frac)


def group_average(get, labels):
    """Mean of the abilities in one radar group; `get(label)` returns the int value."""
    return sum(get(label) for label in labels) / len(labels)


def search_summary(shown, total, limit=800):
    """Status text for the player list, honest about the 800-row cap."""
    if total > limit:
        return f"first {limit:,} of {total:,} matches (refine the search)"
    return f"{shown:,} shown"

# ability groups for the radar under the list; every one of the 25 abilities is in exactly one
RADAR_SHORT = {"Attack": "Att", "Control": "Ctl", "Passing": "Pas", "Defence": "Def",
               "Athletic": "Ath", "Keeping": "GK"}

# Abilities whose database names are wider than their grid cell; the database label stays the
# key for reads and writes, only the shown text is shortened to keep the row intact.
ATTR_SHORT = {
    "Offensive Awareness": "Off Aware.",
    "Defensive Awareness": "Def Aware.",
    "Tight Possession": "Tight Poss.",
    "Place Kicking": "Place Kick",
    "Kicking Power": "Kick Power",
    "Physical Contact": "Phys. Cont.",
}

RADAR_GROUPS = (
    ("Attack", ("Offensive Awareness", "Finishing", "Heading", "Place Kicking")),
    ("Control", ("Ball Control", "Dribbling", "Tight Possession")),
    ("Passing", ("Low Pass", "Lofted Pass", "Curl")),
    ("Defence", ("Defensive Awareness", "Ball Winning", "Aggression")),
    ("Athletic", ("Speed", "Acceleration", "Kicking Power", "Jump", "Physical Contact",
                  "Balance", "Stamina")),
    ("Keeping", ("GK Awareness", "GK Catching", "GK Clearing", "GK Reflexes", "GK Reach")),
)

C_BG = "#120e0a"
C_PANEL = "#1b1510"
C_PANEL2 = "#251c14"
C_LINE = "#35281c"      # grid lines, separators
C_HOVER = "#2e2318"     # hover/active background
C_SEL = "#3a2a16"       # selected row / listbox selection
C_INK = "#1a1004"       # text on the accent colour
C_FG = "#f4ead9"
C_DIM = "#ab927a"
C_ACCENT = "#f0a63f"
C_ACCENT_HI = "#f7c169"    # hover of accent-coloured buttons
C_ACCENT_LO = "#a86f24"    # radar spoke for a group averaging under 65
C_LINE_SOFT = "#6b5128"    # dim spoke on the radar's outer ring
C_GREEN = "#7fd08b"
C_MAGENTA = "#dd6f9f"
C_WARN = "#e05d5d"


class Pill(tk.Canvas):
    """A chip drawn as a rounded "island" — Tk cannot round a widget's corners.

    A canvas rather than a Label: the text is one item and the background is a smoothed
    polygon whose corner radius is half the height, which is the pill shape the mockup
    uses for every chip family (`.chip{border-radius:999px}`).
    """

    def __init__(self, parent, text, font, click=None):
        super().__init__(parent, bg=C_BG, bd=0, highlightthickness=0, cursor="hand2")
        self._text = text
        self._font = font
        self._click = click
        self._fill, self._fg, self._line = C_PANEL2, C_DIM, C_LINE
        self._redraw()
        if click is not None:
            self.bind("<Button-1>", self._fire)

    def _fire(self, _event=None):
        if self._click is not None:
            self._click()

    def get_text(self):
        return self._text

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self._redraw()

    def set_state(self, fill=None, fg=None, line=None):
        """Repaint the chip; None keeps the current colour."""
        if fill is not None:
            self._fill = fill
        if fg is not None:
            self._fg = fg
        if line is not None:
            self._line = line
        self._redraw()

    def _redraw(self):
        w = self._font.measure(self._text) + 18
        h = self._font.metrics("linespace") + 6
        r = min(h / 2, w / 2)
        self.configure(width=w, height=h)
        self.delete("all")
        # points marching along both semicircular ends: Tk's smoothing only approximates a
        # curve between control points, so two corner points round a pill into a plain box
        pts = []
        for deg in (270, 248, 225, 202, 180, 158, 135, 112, 90):      # left end, top to bottom
            a = math.radians(deg)
            pts += [r + r * math.cos(a), r + r * math.sin(a)]
        for deg in (90, 68, 45, 22, 0, -22, -45, -68, -90):           # right end, bottom to top
            a = math.radians(deg)
            pts += [w - r + r * math.cos(a), r + r * math.sin(a)]
        self.create_polygon(pts, fill=self._fill, outline=self._line, width=1, smooth=True)
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=self._fg, font=self._font)


# Nicknames the database does not spell out. Typing the alias lists the clubs whose name
# contains one of its targets; an alias also matches on a prefix, so "man" finds both
# Manchester clubs and "spur" finds Tottenham.
CLUB_ALIASES = {
    "man u": ["manchester united"], "man utd": ["manchester united"],
    "utd": ["manchester united"], "red devils": ["manchester united"],
    "man city": ["manchester city"],
    "spurs": ["tottenham"], "gunners": ["arsenal"], "toon": ["newcastle"],
    "barca": ["fc barcelona"], "atleti": ["atletico madrid"], "real": ["real madrid"],
    "juve": ["juventus"], "psg": ["paris saint"], "bvb": ["dortmund"],
    "wolves": ["wolverhampton"], "forest": ["nottingham"], "naples": ["napoli"],

}


def _pick_font():
    """First widely-available sans-serif this system reports; DejaVu Sans otherwise.

    A Tk built without Xft lists no real families (or only "fixed"), and any family name it
    cannot resolve falls back to its own default, so naming a common font is the safe answer.
    """
    from tkinter import font as tkfont
    families = set(tkfont.families())
    if len(families) > 3:
        for name in ("DejaVu Sans", "Noto Sans", "Liberation Sans", "Cantarell", "Ubuntu",
                     "Segoe UI", "Arial", "Helvetica"):
            if name in families:
                return name
    return "DejaVu Sans"


def norm(text):
    """Lowercase and strip accents: 'München' -> 'munchen', 'Atlético' -> 'atletico'."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()

PLAY_STYLES = [
    "Goal Poacher", "Dummy Runner", "Fox in the Box", "Target Man", "Deep-Lying Forward",
    "Creative Playmaker", "Prolific Winger", "Roaming Flank", "Cross Specialist",
    "Box-to-Box", "The Destroyer", "Orchestrator", "Anchor Man", "Defensive Full-back",
    "Offensive Full-back", "Full-back Finisher", "Build Up", "Offensive Goalkeeper",
    "Defensive Goalkeeper", "None 19", "None 20", "None 21", "None 22", "None 23",
    "None 24", "None 25", "None 26", "None 27", "None 28", "None 29", "None 30", "None 31",
]


def ensure_vanilla(cpk_path):
    """Cache the pristine Player.bin; refetch when the CPK is newer (game update or new folder)."""
    if not os.path.exists(VANILLA) or os.path.getmtime(VANILLA) < os.path.getmtime(cpk_path):
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(VANILLA, "wb") as f:
            f.write(pesdb.table_blob(cpk_path, "Player"))


def pristine_cpk(cpk_path):
    """Snapshot of the shipped CPK for Restore vanilla; taken once, before the first patch.

    If the CPK has already been patched, the oldest backup in the backup folder is the
    shipped file, so that wins over the current (patched) CPK.
    """
    import glob
    import hashlib
    import shutil
    key = hashlib.sha1(os.path.abspath(cpk_path).encode()).hexdigest()[:8]
    path = os.path.join(STATE_DIR, "data.pristine.%s.cpk" % key)
    if not os.path.exists(path):
        os.makedirs(STATE_DIR, exist_ok=True)
        older = sorted(glob.glob(os.path.join(
            settings.backups_dir(settings.load(), STATE_DIR),
            os.path.splitext(os.path.basename(cpk_path))[0] + ".*.cpk")))
        shutil.copy2(older[0] if older else cpk_path, path)
    return path


def load_table_blob(cpk_path, name):
    return pesdb.table_blob(cpk_path, name)


def load_tables(cpk_path):
    """Every table the editor touches, plus the pid/team indexes it needs.

    Player comes from the cached vanilla copy (the editable one); the other tables are read
    from the CPK each time, so a save of one never hides a change made to another.
    """
    ensure_vanilla(cpk_path)
    tables = {
        "Player": pesdb.PlayerTable(open(VANILLA, "rb").read()),
        "Team": pesdb.TeamTable(load_table_blob(cpk_path, "Team")),
        "PlayerAssignment": pesdb.AssignmentTable(load_table_blob(cpk_path, "PlayerAssignment")),
        "Coach": pesdb.CoachTable(load_table_blob(cpk_path, "Coach")),
        "PlayerAppearance": pesdb.AppearanceTable(load_table_blob(cpk_path, "PlayerAppearance")),
    }
    return tables


class Editor(tk.Tk):
    # fields bulk edit may touch: every ability plus the plain numeric identity values
    NUMERIC_FIELDS = [l for l, *_ in pesdb.ATTRS] + [l for l, *_ in pesdb.CAREER] + \
                     ["Age", "Height (cm)", "Weight (kg)", "Form", "Weak Foot Usage",
                      "Weak Foot Accuracy", "Injury Resistance"]

    def __init__(self):
        super().__init__()
        # Tk scales fonts from the server DPI but not one pixel of our own sizes, so at 144 dpi
        # the table rows overlapped and the panes were too narrow for their own text.
        self.ui = max(1.0, round(self.winfo_fpixels("1i") / 96.0, 2))
        try:
            self.iconphoto(True, tk.PhotoImage(master=self, file=_icon_path()))
        except (tk.TclError, OSError):
            pass                       # no bundled icon: Tk's default shows, nothing breaks
        self.title(f"{settings.APP_NAME} — SP Football Life 2027")
        self.configure(bg=C_BG)
        win_w = min(int(1460 * self.ui), self.winfo_screenwidth())
        win_h = min(int(920 * self.ui), self.winfo_screenheight())
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(min(int(1180 * self.ui), win_w), min(int(760 * self.ui), win_h))

        self.cfg = settings.load()
        self.FONT = _pick_font()
        self._chip_font = tkfont.Font(root=self, family=self.FONT, size=8)   # pill chips measure
        configured = self.cfg["game_dir"]
        self.game_dir = settings.game_dir(self.cfg)      # searches the disk if that folder is gone
        self.auto_found = configured != self.game_dir
        self.backups_dir = settings.backups_dir(self.cfg, STATE_DIR)
        self.cpk_path = settings.database_path(self.game_dir)
        if not os.path.exists(self.cpk_path):
            # First run on a new machine: ask where the game is instead of refusing to start.
            self.withdraw()
            messagebox.showinfo(
                "Locate your game",
                "The editor could not find SP Football Life 2027 automatically.\n\n"
                "Pick the game folder — the one containing the download folder with "
                "data_s*.cpk in it.",
                parent=self)
            while not os.path.exists(self.cpk_path):
                folder = filedialog.askdirectory(
                    title="Select the SP Football Life / PES 2021 folder", parent=self)
                if not folder:
                    self.destroy()
                    raise SystemExit(1)
                folder = os.path.expanduser(folder)
                if not settings.has_database(folder):
                    messagebox.showerror(
                        "Not a game folder",
                        f"No download/data_s*.cpk inside:\n{folder}",
                        parent=self)
                    continue
                self.cfg["game_dir"] = folder
                settings.save(self.cfg)
                self.game_dir, self.auto_found = folder, True
                self.cpk_path = settings.database_path(folder)
            self.deiconify()
        self.pristine = pristine_cpk(self.cpk_path)
        self.edit_saves = settings.edit_save_files()
        self.tables = load_tables(self.cpk_path)
        self.portrait_pid = None
        self._portrait_queue = queue.Queue()
        self._portrait_busy = set()
        self._pump_running = False
        self._portrait_missing = set()
        self._net_failed = 0.0
        self._radar_vals = None
        self._radar_target = None
        self._radar_job = None
        self._radar_poly = None
        self._radar_axes = None
        self.index = None          # record index shown
        self.reindex()
        self.dirty = False
        self.vars = {}
        self._undo = []           # [(undo_callable, redo_callable), ...]
        self._redo = []
        self._edited_pids = set()  # players touched this session, for the "changed only" view
        self._build_style()
        self._build_ui()
        self._apply_cursors()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.populate("")
        where = " · database found automatically" if self.auto_found else ""
        shadow = " · in-game EDIT save overrides the database on load" if self.edit_saves else ""
        self.status(f"{len(self.table.records):,} players loaded from {self.game_dir}{where}{shadow}")
        self._park_if_asked()

    # ------------------------------------------------------------------ tables
    def reindex(self):
        """Rebuild the pid/team lookups from the loaded tables."""
        self.table = self.tables["Player"]
        self.team_table = self.tables["Team"]
        self.assign = self.tables["PlayerAssignment"]
        self.coach_table = self.tables["Coach"]
        self.appearance_table = self.tables["PlayerAppearance"]
        self.coaches = {self.coach_table.cid(i): self.coach_table.name(i)
                        for i in range(len(self.coach_table.records))}
        self.stadium_names = pesdb.stadium_names(pesdb.read_table(self.cpk_path, "Stadium"))
        self.team_index = {self.team_table.tid(i): i for i in range(len(self.team_table.records))}
        self.clubs = {self.team_table.tid(i): self.team_table.name(i)
                      for i in range(len(self.team_table.records))}
        self.assign_by_pid = {}
        for j in range(len(self.assign.records)):
            self.assign_by_pid.setdefault(self.assign.pid(j), []).append(j)
        self.player_team = {}
        for pid, rows in self.assign_by_pid.items():
            clubs = [self.assign.team(j) for j in rows if self.assign.team(j) > pesdb.NATIONAL_MAX]
            if clubs:
                self.player_team[pid] = clubs[0]
        self.dirty_tables = set()
        self.mode = "players"                      # "clubs" while a club search is on screen
        if not hasattr(self, "sort_col"):
            self.sort_col, self.sort_desc = None, False
        self.club_counts = {}
        for tid in self.player_team.values():
            self.club_counts[tid] = self.club_counts.get(tid, 0) + 1

    # ---------------------------------------------------------------- style
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=C_BG, foreground=C_FG, fieldbackground=C_PANEL2,
                     bordercolor=C_PANEL2, lightcolor=C_PANEL2, darkcolor=C_PANEL2,
                     troughcolor=C_PANEL, focuscolor=C_ACCENT)
        st.configure("Treeview", background=C_PANEL, fieldbackground=C_PANEL,
                     foreground=C_FG, rowheight=int(24 * self.ui), borderwidth=0)
        st.configure("Treeview.Heading", background=C_PANEL2, foreground=C_ACCENT,
                     relief="flat", font=(self.FONT, 9, "bold"))
        st.map("Treeview", background=[("selected", C_SEL)],
               foreground=[("selected", C_FG)])
        st.configure("TEntry", fieldbackground=C_PANEL2, foreground=C_FG, insertcolor=C_FG,
                     padding=(2, 1))
        st.configure("TCombobox", fieldbackground=C_PANEL2, background=C_PANEL2, foreground=C_FG,
                     selectbackground=C_PANEL2, selectforeground=C_FG, arrowcolor=C_ACCENT,
                     insertcolor=C_FG, padding=(2, 1))
        st.map("TCombobox",
               fieldbackground=[("readonly", C_PANEL2), ("focus", C_PANEL2)],
               foreground=[("readonly", C_FG)],
               selectbackground=[("readonly", C_PANEL2)],
               selectforeground=[("readonly", C_FG)],
               background=[("active", C_PANEL2)])
        self.option_add("*TCombobox*Listbox.background", C_PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", C_FG)
        self.option_add("*TCombobox*Listbox.selectBackground", C_SEL)
        self.option_add("*TCombobox*Listbox.selectForeground", C_FG)
        self.option_add("*Spinbox.background", C_PANEL2)
        self.option_add("*Spinbox.foreground", C_FG)
        self.option_add("*Spinbox.insertBackground", C_FG)
        self.option_add("*Spinbox.buttonBackground", C_PANEL)
        self.option_add("*Spinbox.activeBackground", C_HOVER)
        self.option_add("*Spinbox.highlightThickness", 0)
        self.option_add("*Spinbox.relief", "flat")
        self.option_add("*Spinbox.bd", 0)
        st.configure("TSpinbox", fieldbackground=C_PANEL2, foreground=C_FG, arrowcolor=C_ACCENT,
                     padding=(2, 1))
        st.configure("TCheckbutton", background=C_BG, foreground=C_FG, padding=(0, 0))
        st.map("TCheckbutton", background=[("active", C_BG)])
        # ttk button colours have to go through map(): on the clam theme a plain configure()
        # background is not painted, which is how these buttons ended up as bare text
        st.configure("Save.TButton", font=(self.FONT, 10, "bold"), padding=(18, 8), borderwidth=0)
        st.map("Save.TButton", background=[("", C_ACCENT), ("active", C_ACCENT_HI)],
               foreground=[("", C_INK)])
        st.configure("Ghost.TButton", padding=(12, 6), borderwidth=0)
        st.map("Ghost.TButton", background=[("", C_PANEL2), ("active", C_HOVER)],
               foreground=[("", C_FG)])

    # ---------------------------------------------------------------- ui
    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self._build_body_one_page(body)

        foot = tk.Frame(self, bg=C_PANEL)
        foot.pack(fill="x", side="bottom")
        self.statusbar = tk.Label(foot, text="", bg=C_PANEL, fg=C_DIM, anchor="w",
                                  font=(self.FONT, 9), padx=12, pady=6)
        self.statusbar.pack(side="left", fill="x", expand=True)
        tk.Label(foot, text="Ctrl+Z undo · Ctrl+S save", bg=C_PANEL, fg=C_LINE_SOFT,
                 font=(self.FONT, 8)).pack(side="right", padx=(10, 12))
        self.lbl_dirty = tk.Label(foot, text="", bg=C_PANEL, fg=C_MAGENTA,
                                  font=(self.FONT, 10, "bold"))
        self.lbl_dirty.pack(side="right", padx=(10, 0))

        self.bind("<Control-comma>", lambda e: self.open_settings())
        self.bind("<Control-s>", self.on_save_key)
        self.bind("<Control-z>", self.on_undo_key)
        self.bind("<Control-y>", self.on_redo_key)
        self.bind("<Control-f>", self.on_search_key)
        self.bind("<Escape>", self.on_clear_key)

        # XWayland/Hyprland sometimes swallows the Expose that should follow a resize,
        # so widgets stay blank until a mouse-over forces a repaint; re-issue one after
        # the resize settles. Debounced: a drag fires many <Configure>s.
        self._resize_repaint_job = None
        self.bind("<Configure>", self._on_root_resize)

    def _on_root_resize(self, _=None):
        if self._resize_repaint_job is not None:
            self.after_cancel(self._resize_repaint_job)
        self._resize_repaint_job = self.after(120, self._force_repaint)

    def _force_repaint(self):
        self._resize_repaint_job = None
        self.event_generate("<Expose>", x=0, y=0,
                            width=self.winfo_width(), height=self.winfo_height())

    # ---------------------------------------------------------------- chrome
    def _build_header(self):
        header = tk.Frame(self, bg=C_BG)
        header.pack(fill="x", padx=16, pady=(10, 8))
        self._header = header
        top = tk.Frame(header, bg=C_BG)
        top.pack(fill="x")
        self._header_top = top
        self._title = tk.Label(top, text=settings.APP_NAME.upper(), bg=C_BG, fg=C_ACCENT,
                               font=(self.FONT, 15, "bold"))
        self._subtitle = tk.Label(top, text="SP Football Life 2027", bg=C_BG, fg=C_DIM,
                                  font=(self.FONT, 10))
        # the actions are packed first so they own the header's right edge: pack() shrinks the
        # last-packed child when the header narrows, and that must never be a button
        self._build_actions(top)
        self._title.pack(side="left")
        self._subtitle.pack(side="left", padx=10)

        # search: a dim hint label lies over the entry while it is empty and unfocused; the
        # hint is not entry text, so the search variable itself stays clean
        self.q = tk.StringVar()
        box = tk.Frame(top, bg=C_PANEL2, highlightthickness=1, highlightbackground=C_LINE)
        box.pack(side="left", fill="x", expand=True, padx=(16, 12))
        self.search_entry = ent = tk.Entry(box, textvariable=self.q, bg=C_PANEL2, fg=C_FG,
                                           insertbackground=C_FG, relief="flat", bd=0)
        ent.pack(side="left", fill="x", expand=True, padx=10, pady=5)
        ent.bind("<Return>", lambda e: self.open_first(self.q.get()))
        ent.bind("<FocusIn>", self._search_focus_in)
        ent.bind("<FocusOut>", self._search_focus_out)
        tk.Label(box, text="Ctrl+F", bg=C_PANEL2, fg=C_LINE_SOFT,
                 font=(self.FONT, 8)).pack(side="right", padx=(0, 8))
        self._search_focused = False
        self._search_hint = tk.Label(box, text="Search players, clubs, IDs…", bg=C_PANEL2,
                                     fg=C_DIM, cursor="xterm")
        self._search_hint.bind("<Button-1>", lambda e: ent.focus_set())
        self.q.trace_add("write", lambda *a: (self._debounced(), self._show_search_hint()))
        self._after = None
        self._show_search_hint()

    def _search_focus_in(self, *_):
        """The hint is driven by the entry's own focus events: `focus_get()` is None for Tk
        under XWayland, so the hint used to reappear over the caret and read as a glitch."""
        self._search_focused = True
        self._search_hint.place_forget()

    def _search_focus_out(self, *_):
        self._search_focused = False
        self._show_search_hint()

    def _show_search_hint(self, *_):
        if not self.q.get() and not self._search_focused:
            self._search_hint.place(in_=self.search_entry, x=2, rely=0.5, anchor="w")
        else:
            self._search_hint.place_forget()

    def _later(self, callback):
        """Run a menu command on the next event-loop pass.

        A tk.Menu keeps a grab until the click that selected the entry has been fully
        processed, and a dialog raised inside that grab can end up unable to take focus or
        to be clicked at all — so every menu command hops past the grab.
        """
        def run(_event=None):
            self.after_idle(callback)
        return run

    def _make_menu_button(self, parent, text="≡  Menu"):
        """The header's menu button, entries included so both live in one place."""
        btn = tk.Menubutton(parent, text=text, bg=C_PANEL2, fg=C_FG,
                            activebackground=C_HOVER, activeforeground=C_FG,
                            bd=0, padx=12, pady=3, cursor="hand2")
        menu = tk.Menu(btn, tearoff=0, bg=C_PANEL2, fg=C_FG,
                       activebackground=C_ACCENT, activeforeground=C_INK, bd=0)
        menu.add_command(label="Settings…", accelerator="Ctrl+,",
                         command=self._later(self.open_settings))
        menu.add_command(label="Open backups folder",
                         command=self._later(self.open_backups))
        menu.add_separator()
        menu.add_command(label="Reload database", command=self._later(self.reload))
        menu.add_command(label="Restore vanilla", command=self._later(self.restore_vanilla))
        menu.add_separator()
        menu.add_command(label="Export roster CSV…", command=self._later(self._export_dialog))
        menu.add_command(label="Import roster CSV…", command=self._later(self._import_dialog))
        menu.add_separator()
        menu.add_command(label="Bulk edit selected…", command=self._later(self.bulk_dialog))
        menu.add_command(label="Compare selected players…",
                         command=self._later(self.compare_dialog))
        menu.add_separator()
        menu.add_command(label="Coaches…", command=self._later(self.open_coaches))
        menu.add_command(label="Appearance of the selected player…",
                         command=self._later(self.open_appearance))
        menu.add_separator()
        menu.add_command(label="About", command=self._later(self.about))
        btn.configure(menu=menu)
        self.menu_btn = btn
        return btn

    def _build_actions(self, parent):
        """Save / undo / revert / menu, pinned to the header's right edge.

        The rows are re-split whenever the header narrows past what the buttons fit, because a
        tiling compositor hands out less than the requested width and pack() answers by
        shrinking the last-packed widget — the bar — to nothing while still calling it mapped.

        The buttons are rebuilt inside their row frame whenever the split changes: a widget
        packed `-in` a frame that is not its parent gets a geometry but is never painted, and
        that is exactly how the bar used to end up invisible.
        """
        bar = tk.Frame(parent, bg=C_BG)
        bar.pack(side="right")
        self._button_bar = bar
        self._bar_rows = []
        self._bar_buttons = []
        self._bar_groups = None
        defs = [("Save to game", "Save.TButton", self.save),
                ("Undo", "Ghost.TButton", self.undo),
                ("Revert", "Ghost.TButton", self.on_select_refresh),
                ("≡  Menu", "Ghost.TButton", None)]

        def build_rows(groups):
            for frame in self._bar_rows:
                frame.destroy()
            self._bar_rows = []
            self._bar_buttons = []
            for n, group in enumerate(groups):
                frame = tk.Frame(bar, bg=C_BG)
                frame.pack(fill="x", pady=(0, 0) if n == 0 else (4, 0))
                for text, style, cmd in group:
                    if cmd is None:                        # the menu button
                        button = self._make_menu_button(frame, text)
                    else:
                        button = ttk.Button(frame, text=text, style=style, command=cmd)
                    button.pack(side="left", padx=(6, 0))
                    self._bar_buttons.append(button)
                self._bar_rows.append(frame)

        def layout_bar(_=None):
            """Rows are re-split to whatever fits, so no button is ever clipped away."""
            space = self._header.winfo_width()
            if space <= 1:
                space = int(700 * self.ui)                 # the header is not mapped yet
            # the bar shares the row with the brand: split for the width the bar gets once the
            # brand has drawn, and never below one button's worth
            brand = self._title.winfo_reqwidth() + self._subtitle.winfo_reqwidth()
            space = max(int(165 * self.ui), space - brand - int(16 * self.ui))
            widths = [b.winfo_reqwidth() + 6 for b in self._bar_buttons] or \
                     [int((len(text) + 3) * 8 * self.ui) for text, _s, _c in defs]
            groups, row, used = [], [], 0
            for spec, width in zip(defs, widths):
                if row and used + width > space:
                    groups.append(tuple(row))
                    row, used = [], 0
                row.append(spec)
                used += width
            if row:
                groups.append(tuple(row))
            if self._bar_groups == groups and self._bar_buttons:
                return
            self._bar_groups = groups
            build_rows(groups)

        self._header.bind("<Configure>", layout_bar, add="+")
        layout_bar()
        return bar

    # ---------------------------------------------------------------- bodies
    def _build_body_one_page(self, body):
        """Two form columns beside the list; they stack into one when the window is narrow."""
        self._one_page_body = body
        self._one_page_mode = None
        self._one_page_job = None
        body.bind("<Configure>", self._maybe_fold_one_page)
        self._layout_one_page(body, wide=True)

    def _maybe_fold_one_page(self, evt):
        """Rebuild the body when the window crosses a size the columns still fit at.

        The rebuild is where the compact grids pick their column counts, so a window that is
        too small for the wide layout gets the stacked one instead of clipped fields.
        """
        wide = evt.width >= 1120 * self.ui
        mode = (wide, *self._detail_columns(evt.width, wide).values())
        if mode == self._one_page_mode:
            return
        self._one_page_mode = mode
        if self._one_page_job is not None:
            self.after_cancel(self._one_page_job)
        self._one_page_job = self.after(120, self._finish_fold)

    def _finish_fold(self):
        self._one_page_job = None
        mode = self._one_page_mode
        self._layout_one_page(self._one_page_body, wide=mode is None or mode[0])

    def _detail_columns(self, body_w, wide):
        """How many cells per row each form grid fits, for a body `body_w` px wide.

        The numbers are the measured width of one cell (label + widget) at ui 1.0 on Tk 8.6,
        the wider of the two Tk builds this app runs on; the layout asks for the pane widths
        first (`_grid_cols`) so the two always agree.
        """
        list_w = getattr(self, "_list_w", 0) or int(236 * self.ui)
        avail = max(320, int(body_w or 0) - list_w - 28)
        if wide:
            return self._grid_cols(int(avail * 0.55), avail - int(avail * 0.55))
        return self._grid_cols(avail, avail)

    def _grid_cols(self, mid_w, right_w):
        """Cells per row for each grid that fits the two form panes."""
        def fit(pane, most, cell):
            return max(1, min(most, int(pane / (cell * self.ui))))
        return {
            "details": fit(mid_w, 3, 272),
            "contract": fit(mid_w, 3, 260),
            "positions": fit(mid_w, 5, 128),
            "abilities": fit(mid_w, 3, 165),
            "skills": fit(right_w, 4, 140),
        }

    def _layout_one_page(self, body, wide):
        for child in body.winfo_children():
            child.destroy()
        for c in range(3):                     # the wide layout's column weights survived the fold
            body.columnconfigure(c, weight=0, minsize=0)
        left = tk.Frame(body, bg=C_BG)
        self._build_list_pane(left)
        # the list keeps the width its own columns ask for; the form panes get what is left
        self.update_idletasks()
        self._list_w = max(int(236 * self.ui), left.winfo_reqwidth())
        cols = self._detail_columns(body.winfo_width(), wide)
        if wide:
            body_w = body.winfo_width() or int(1460 * self.ui)
            avail = max(360, body_w - self._list_w - 28)
            mid_w = max(int(300 * self.ui), int(avail * 0.55) - 14)
            right_w = max(int(300 * self.ui), avail - mid_w - 28)
            body.columnconfigure(0, minsize=self._list_w)
            body.columnconfigure(1, minsize=mid_w, weight=55)
            body.columnconfigure(2, minsize=right_w, weight=45)
            body.rowconfigure(0, weight=1)
            left.grid(row=0, column=0, sticky="nsew")
            mid = tk.Frame(body, bg=C_BG)
            mid.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
            right = tk.Frame(body, bg=C_BG)
            right.grid(row=0, column=2, sticky="nsew", padx=(14, 0))
            center = self._scroll_pane(mid)
            self._build_player_card(center)
            self._build_attributes(center, with_radar=False, cols=cols["abilities"])
            self._build_position_grid(center, cols["positions"])
            self._build_details(center, cols["details"])
            self._build_career(center, cols["contract"])
            inner_right = self._scroll_pane(right)
            self._build_radar_pane(inner_right)
            self._build_skills(inner_right, cols=cols["skills"])
            self._build_club(inner_right)
        else:
            body.columnconfigure(0, minsize=int(232 * self.ui))
            body.columnconfigure(1, weight=1)
            body.rowconfigure(0, weight=1)
            left.grid(row=0, column=0, sticky="nsew")
            detail = tk.Frame(body, bg=C_BG)
            detail.grid(row=0, column=1, sticky="nsew", padx=(14, 0))
            col = self._scroll_pane(detail)
            self._build_player_card(col)
            self._build_attributes(col, cols=cols["abilities"])
            self._build_position_grid(col, cols["positions"])
            self._build_details(col, cols["details"])
            self._build_career(col, cols["contract"])
            self._build_skills(col, cols=cols["skills"])
            self._build_club(col)
        if getattr(self, "statusbar", None) is not None:    # a relayout, not the first build
            self.populate(self.q.get())
            if self.index is not None:
                self.load_form()

    # ---------------------------------------------------------------- left pane
    def _build_list_pane(self, left):
        filt = tk.Frame(left, bg=C_BG)
        filt.pack(fill="x", pady=(0, 8))
        tk.Label(filt, text="Club", bg=C_BG, fg=C_DIM, width=7, anchor="w").pack(side="left")
        self.club_filter = tk.StringVar(value="")   # empty = every club
        self._club_names = sorted({n for t, n in self.clubs.items() if n and t >= 100})
        self.cb_club = ttk.Combobox(filt, textvariable=self.club_filter, width=30,
                                    values=self._club_names)
        self.cb_club.pack(side="left", fill="x", expand=True, padx=8)
        self.cb_club.bind("<<ComboboxSelected>>", lambda e: self.apply_club_filter())
        self.cb_club.bind("<Return>", lambda e: self.apply_club_filter())
        self.cb_club.bind("<KeyRelease>", self._filter_club_values)
        self._club_after = None

        filt_nat = tk.Frame(left, bg=C_BG)
        filt_nat.pack(fill="x", pady=(0, 8))
        tk.Label(filt_nat, text="Country", bg=C_BG, fg=C_DIM, width=7,
                 anchor="w").pack(side="left")
        self.country_filter = tk.StringVar(value="")      # empty = every nationality
        self._country_names = sorted(set(pesdb.COUNTRIES.values()))
        self.cb_country = ttk.Combobox(filt_nat, textvariable=self.country_filter, width=30,
                                       values=self._country_names)
        self.cb_country.pack(side="left", fill="x", expand=True, padx=8)
        self.cb_country.bind("<<ComboboxSelected>>", lambda e: self.apply_country_filter())
        self.cb_country.bind("<Return>", lambda e: self.apply_country_filter())
        self.cb_country.bind("<KeyRelease>", self._filter_country_values)
        self._country_after = None

        filt_pos = tk.Frame(left, bg=C_BG)
        filt_pos.pack(fill="x", pady=(0, 8))
        tk.Label(filt_pos, text="Position", bg=C_BG, fg=C_DIM, width=7,
                 anchor="w").pack(side="left")
        self.position_filter = tk.StringVar(value="")      # empty = every position
        self.cb_pos = ttk.Combobox(filt_pos, textvariable=self.position_filter, width=30,
                                   values=[""] + pesdb.POS_NAMES, state="readonly")
        self.cb_pos.pack(side="left", fill="x", expand=True, padx=8)
        self.cb_pos.bind("<<ComboboxSelected>>", lambda e: self.populate(self.q.get()))

        self.changed_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Changed only", variable=self.changed_only,
                        style="TCheckbutton",
                        command=lambda: self.populate(self.q.get())).pack(anchor="w", pady=(0, 4))

        self.free_agents_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Free agents only", variable=self.free_agents_only,
                        style="TCheckbutton",
                        command=lambda: self.populate(self.q.get())).pack(anchor="w", pady=(0, 4))

        cols = ("id", "name", "club", "pos", "ovr", "age")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c, t, w, anchor in (("id", "ID", 42, "e"), ("name", "Name", 100, "w"),
                                ("club", "Club", 80, "w"), ("pos", "Pos", 30, "center"),
                                ("ovr", "OVR", 30, "center"), ("age", "Age", 26, "center")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=int(w * self.ui), anchor=anchor, stretch=(c == "name"))
        # no scrollbar: the wheel, arrows and PageUp/PageDown move the list, and the status
        # line already says how many rows there are
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Return>", lambda e: self.on_select())

    def _apply_cursors(self):
        """Give every widget an explicit X cursor.

        Tk does not set one for ttk widgets, and under XWayland/Hyprland a window without an
        explicit cursor can end up with no pointer at all — the mouse position is then invisible
        over the whole app."""
        try:
            self.configure(cursor="left_ptr")
        except tk.TclError:
            self.configure(cursor="arrow")
        for w in self._walk(self):
            try:
                if isinstance(w, (ttk.Entry, ttk.Spinbox, tk.Entry, tk.Spinbox)):
                    w.configure(cursor="xterm")
                    w.bind("<FocusOut>", self._clear_selection, add="+")
                elif isinstance(w, ttk.Treeview):
                    w.configure(cursor="arrow")
                elif isinstance(w, tk.Canvas) and w.cget("cursor") == "sb_h_double_arrow":
                    continue                      # the ability bars are drag handles
                elif isinstance(w, (ttk.Button, tk.Canvas, ttk.Scrollbar)):
                    w.configure(cursor="hand2")
                else:
                    w.configure(cursor="left_ptr")
            except tk.TclError:
                continue

    def _clear_selection(self, evt=None):
        """Tk 9 leaves a spinbox's value selected after focus leaves it; that blue block looks
        like the field is active when it is not."""
        w = evt.widget if evt is not None else self.focus_get()
        if w is None:
            return
        try:
            w.tk.call(str(w), "selection", "clear")
        except tk.TclError:
            pass

    def _walk(self, w):
        for c in w.winfo_children():
            yield c
            yield from self._walk(c)

    def _scroll_pane(self, parent):
        """Canvas-backed form column; the scrollbar only appears when the content really
        overflows, so a page that fits shows no scroll chrome at all.

        The compact layout is sized to fit a 1500x950 window, so in normal use the bar never
        shows; it is the safety net for a window squeezed smaller than the design.
        """
        canvas = tk.Canvas(parent, bg=C_BG, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C_BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        shown = {"bar": False}

        def sync(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            overflow = inner.winfo_reqheight() > canvas.winfo_height() + 1
            if overflow != shown["bar"]:
                shown["bar"] = overflow
                if overflow:
                    sb.pack(side="right", fill="y")
                else:
                    sb.pack_forget()
            # a wheel tick while the page was taller leaves the view scrolled past short content,
            # and nothing resets it because the scrollbar cannot scroll a fitting page back up
            if not overflow:
                canvas.yview_moveto(0)

        def on_resize(e):
            canvas.itemconfigure(win, width=e.width)
            sync()

        def on_map(e):
            sync()
            canvas.yview_moveto(0)   # a pane always opens at the top of its form

        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>", on_resize)
        canvas.bind("<Map>", on_map)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        # bind_all reaches every column's canvas, so one wheel tick over the list scrolled the
        # hidden columns too and left their content pushed down out of view
        canvas.bind_all("<Button-4>", lambda e: canvas.winfo_ismapped() and canvas.yview_scroll(-2, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.winfo_ismapped() and canvas.yview_scroll(2, "units"))
        return inner

    def _wrap(self, label, minimum=160):
        """Keep a long line inside its pane: Tk labels clip instead of wrapping on their own."""
        def fit(evt, lab=label):
            lab.configure(wraplength=max(minimum, evt.width - 10))
        label.master.bind("<Configure>", fit, add="+")
        return label

    def _section(self, parent, text):
        f = tk.LabelFrame(parent, text=" " + text.upper() + " ", bg=C_BG, fg=C_ACCENT,
                          bd=0, font=(self.FONT, 9, "bold"), padx=7, pady=4)
        f.pack(fill="x", pady=(0, 6))
        return f

    # ---- identity
    def _build_player_card(self, parent):
        """The player card: photo, name, meta line, value chips and the big OVR."""
        head = self._section(parent, "Player")
        top = tk.Frame(head, bg=C_BG)
        top.pack(fill="x", pady=(0, 2))
        side = int(PHOTO_SIDE * self.ui)
        slot = tk.Frame(top, width=side, height=side, bg=C_PANEL2)
        slot.pack(side="left", padx=(0, 10))
        slot.pack_propagate(False)
        self.photo = tk.Label(slot, bg=C_PANEL2, bd=0, text="no photo", fg=C_DIM,
                              font=(self.FONT, 8))
        self.photo.pack(fill="both", expand=True)
        self.photo_img = None

        ovr_box = tk.Frame(top, bg=C_BG)
        ovr_box.pack(side="right", padx=(10, 0))
        self.ovr_big = tk.Label(ovr_box, text="—", bg=C_BG, fg=C_GREEN,
                                font=(self.FONT, 26, "bold"))
        self.ovr_big.pack()
        tk.Label(ovr_box, text="OVR", bg=C_BG, fg=C_DIM, font=(self.FONT, 8)).pack()

        col = tk.Frame(top, bg=C_BG)
        col.pack(side="left", fill="both", expand=True)
        self.lbl_hero = tk.Label(col, text="Select a player", bg=C_BG, fg=C_FG,
                                 font=(self.FONT, 14, "bold"), anchor="w")
        self.lbl_hero.pack(fill="x", pady=(0, 3))
        self.lbl_sub = tk.Label(col, text="Type a name or ID above, then press Enter",
                                bg=C_BG, fg=C_DIM, anchor="w", justify="left",
                                font=(self.FONT, 9))
        self._wrap(self.lbl_sub)
        self.lbl_sub.pack(fill="x", pady=(0, 5))

        chips = tk.Frame(col, bg=C_BG)
        chips.pack(fill="x", pady=(0, 5))
        self._card_chips = {}
        for name in ("Style", "Height", "Weight", "Age", "Form"):
            chip = Pill(chips, "—", self._chip_font)
            chip.pack(side="left", padx=(0, 6))
            self._card_chips[name] = chip

        grid = tk.Frame(col, bg=C_BG)
        grid.pack(fill="x")
        for i, (label, key) in enumerate((("Full name", "name"), ("Shirt name", "shirt"))):
            tk.Label(grid, text=label, bg=C_BG, fg=C_DIM, anchor="w").grid(
                row=i, column=0, sticky="w", pady=1)
            v = tk.StringVar()
            self.vars[key] = v
            e = ttk.Entry(grid, textvariable=v, width=40)
            e.grid(row=i, column=1, sticky="we", padx=6, pady=1)
            e.bind("<FocusOut>", lambda ev, k=key: self.apply_name(k))
        grid.columnconfigure(1, weight=1)

    def _build_position_grid(self, parent, cols=5):
        """Position ratings as pill chips; a click cycles none -> can play -> natural."""
        sec = self._section(parent, "Position ratings  (click to cycle: none · can play · natural)")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(fill="x")
        self._pos_chips = {}
        for n, (label, bit) in enumerate(pesdb.POSITIONS):
            r, c = divmod(n, cols)
            v = tk.StringVar()
            self.vars["Pos " + label] = v
            chip = Pill(grid, label, self._chip_font,
                        lambda l=label: self.cycle_position(l))
            chip.grid(row=r, column=c, sticky="w", padx=(0, 8), pady=1)
            self._pos_chips[label] = chip

    def cycle_position(self, label):
        """Click-cycle a position rating chip; applies like the old combobox did."""
        if self.index is None:
            return
        field = "Pos " + label
        val = (["none", "can play", "natural"].index(self.vars[field].get()) + 1) % 3
        self.vars[field].set(["none", "can play", "natural"][val])
        self.apply_position(label)
        self._style_position_chip(label)

    def _style_position_chip(self, label):
        chip = getattr(self, "_pos_chips", {}).get(label)
        if chip is None:
            return
        val = ["none", "can play", "natural"].index(self.vars["Pos " + label].get())
        chip.set_text(label if val == 0 else f"{label} {val}")
        if val == 2:
            chip.set_state(fill=C_ACCENT, fg=C_INK, line=C_ACCENT)
        elif val == 1:
            chip.set_state(fill=C_PANEL2, fg=C_ACCENT, line=C_ACCENT)
        else:
            chip.set_state(fill=C_PANEL2, fg=C_DIM, line=C_LINE)

    def _build_details(self, parent, cols):
        get = {f[0]: f for f in pesdb.PHYSICAL}
        details = self._section(parent, "Details")
        self._field_grid(details, [get["Age"], get["Height (cm)"], get["Weight (kg)"],
                                   get["Nationality"], get["Form"], get["Weak Foot Usage"],
                                   get["Weak Foot Accuracy"], get["Injury Resistance"],
                                   get["Stronger Foot"], get["Registered Position"],
                                   get["Playing Style"]], cols)

    def _build_career(self, parent, cols):
        rest = self._section(parent, "Contract · motion · celebration  (dates YYYYMMDD)")
        dates = [f for f in pesdb.CAREER if f[0] in ("Contract Until", "Loan Until")]
        others = [f for f in pesdb.CAREER if f[0] not in ("Contract Until", "Loan Until")]
        # one grid, so the date row shares its columns with every row below it
        self._field_grid(rest, dates + others + list(pesdb.MOTIONS), cols)

    def _field_grid(self, parent, fields, cols):
        """Label + widget grid for the form sections (Details and the career block).

        Every row is `[label][widget]`; the label column has a fixed width and a uniform
        share of the spare width, so all rows share one pitch and the widgets line up down
        each column. Career fields use one 9-char box size instead of per-field widths.
        """
        F = (self.FONT, 8)
        career = {f[0] for f in pesdb.CAREER + pesdb.MOTIONS}
        grid = tk.Frame(parent, bg=C_BG)
        grid.pack(fill="x")
        for n, spec in enumerate(fields):
            label = spec[0]
            r, c = divmod(n, cols)
            tk.Label(grid, text=label, bg=C_BG, fg=C_DIM, anchor="w", width=19, font=F).grid(
                row=r, column=c * 2, sticky="w", pady=1, padx=(0, 4))
            key = label
            v = tk.StringVar()
            self.vars[key] = v
            if label == "Stronger Foot":
                w = ttk.Combobox(grid, textvariable=v, state="readonly", width=7,
                                 values=("Right", "Left"), font=F)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_foot())
            elif label == "Registered Position":
                w = ttk.Combobox(grid, textvariable=v, state="readonly", width=12,
                                 values=pesdb.POS_NAMES, font=F)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_field("Registered Position"))
            elif label == "Playing Style":
                w = ttk.Combobox(grid, textvariable=v, state="readonly", width=20,
                                 values=PLAY_STYLES, font=F)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_style())
            elif label in ("Youth Club", "Owner Club"):
                # a club id: the box shows the club name and takes one by name or id
                w = ttk.Combobox(grid, textvariable=v, width=20, font=F,
                                 values=self._club_ref_values())
                w.bind("<<ComboboxSelected>>", lambda e, l=label: self.apply_club_ref(l))
                w.bind("<Return>", lambda e, l=label: self.apply_club_ref(l))
                w.bind("<KeyRelease>",
                       lambda e, cb=w: self._filter_club_ref_values(e, cb))
            else:
                _, _, width, add = spec
                lo = 0 if add == 0 and label != "Age" else add
                if label == "Age":
                    lo, hi = 15, 78
                elif label == "Height (cm)":
                    lo, hi = 100, 227
                elif label == "Weight (kg)":
                    lo, hi = 30, 157
                elif label == "Nationality":
                    lo, hi = 0, 511
                elif label in ("Contract Until", "Loan Until"):
                    lo, hi = 0, 99999999                 # YYYYMMDD
                elif label == "Market Value":
                    lo, hi = 0, (1 << 24) - 1
                elif label == "National Caps":
                    lo, hi = 0, 255
                else:
                    lo, hi = add, add + (1 << width) - 1      # motion slots, celebrations
                w = tk.Spinbox(grid, textvariable=v, from_=lo, to=hi,
                               width=9 if label in career else 5, font=F,
                               command=lambda k=key: self.apply_field(k))
                w.bind("<FocusOut>", lambda e, k=key: self.apply_field(k))
                w.bind("<Return>", lambda e, k=key: self.apply_field(k))
            w.grid(row=r, column=c * 2 + 1, sticky="w", pady=1, padx=(0, 8))
        # no column weights: label and widget sit tight together, so the pitch stays even;
        # spare width stays at the right edge instead of opening gaps mid-form

    # ---- attributes
    def _build_attributes(self, parent, with_radar=True, cols=2):
        F = (self.FONT, 8)
        sec = self._section(parent, "Abilities  (40 – 99)")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(fill="x")
        for n, (label, bit, width, add) in enumerate(pesdb.ATTRS):
            r, c = divmod(n, cols)
            tk.Label(grid, text=ATTR_SHORT.get(label, label), bg=C_BG, fg=C_DIM,
                     anchor="w", width=12, font=F).grid(row=r, column=c * 3, sticky="w", pady=1)
            v = tk.StringVar()
            self.vars[label] = v
            sp = tk.Spinbox(grid, textvariable=v, from_=40, to=99, width=3, font=F,
                            command=lambda k=label: self.apply_field(k))
            sp.bind("<FocusOut>", lambda e, k=label: self.apply_field(k))
            sp.bind("<Return>", lambda e, k=label: self.apply_field(k))
            sp.grid(row=r, column=c * 3 + 1, sticky="w", padx=(0, 3))
            bar = tk.Canvas(grid, width=30, height=11, bg=C_PANEL2, highlightthickness=0,
                            cursor="sb_h_double_arrow")
            bar.grid(row=r, column=c * 3 + 2, sticky="w", padx=(0, 10))
            bar.bind("<Button-1>", lambda e, l=label: self.drag_bar(l, e))
            bar.bind("<B1-Motion>", lambda e, l=label: self.drag_bar(l, e))
            self.vars[label + "::bar"] = bar

        if with_radar:
            self._build_radar_pane(parent)

    def _build_radar_pane(self, parent):
        prof = self._section(parent, "Ability profile  (group averages)")
        self.radar = tk.Canvas(prof, height=int(180 * self.ui), bg=C_BG, highlightthickness=0)
        self.radar.pack(fill="x")
        self.radar.bind("<Configure>", lambda e: self.draw_radar())

    # ---- skills
    def _build_skills(self, parent, cols=2):
        """Skills as pill chips; a click toggles, styled like the position chips."""
        sec = self._section(parent, "Skills")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(fill="x")
        self._skill_chips = {}
        for n, (label, bit) in enumerate(pesdb.SKILLS):
            r, c = divmod(n, cols)
            v = tk.IntVar()
            self.vars["Skill " + label] = v
            chip = Pill(grid, label, self._chip_font,
                        lambda l=label: self.toggle_skill(l))
            chip.grid(row=r, column=c, sticky="w", padx=(0, 8), pady=1)
            self._skill_chips[label] = chip

    def toggle_skill(self, label):
        """Click-toggle a skill chip; applies like the checkbox it replaced."""
        if self.index is None:
            return
        v = self.vars["Skill " + label]
        v.set(0 if v.get() else 1)
        self.apply_skill(label)
        self._style_skill_chip(label)

    def _style_skill_chip(self, label):
        chip = getattr(self, "_skill_chips", {}).get(label)
        if chip is None:
            return
        on = bool(self.vars["Skill " + label].get())
        chip.set_state(fg=C_ACCENT if on else C_DIM,
                       line=C_ACCENT if on else C_LINE)

    def ovr(self, i):
        """Reference rating for one player record (see pesdb.OVR_MODEL)."""
        return pesdb.overall(lambda label: self.table.get(i, label),
                             self.table.get(i, "Registered Position"))

    # ---- club / contract
    def _build_club(self, parent):
        F = (self.FONT, 8)
        sec = self._section(parent, "Current club")
        self.lbl_club = tk.Label(sec, text="—", bg=C_BG, fg=C_FG, anchor="w",
                                 font=(self.FONT, 10, "bold"))
        self.lbl_club.pack(fill="x", pady=(0, 3))

        row = tk.Frame(sec, bg=C_BG)
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text="Move to club", bg=C_BG, fg=C_DIM, width=12, anchor="w",
                 font=F).pack(side="left")
        self.club_target = tk.StringVar()
        self.cb_target = ttk.Combobox(row, textvariable=self.club_target, width=26, font=F,
                                      values=sorted({n for n in self.clubs.values() if n}))
        self.cb_target.pack(side="left", fill="x", expand=True, padx=6)
        self.cb_target.bind("<KeyRelease>", self._filter_target_values)
        self.cb_target.bind("<Return>", lambda e: self.apply_transfer())
        ttk.Button(row, text="Transfer", style="Ghost.TButton",
                   command=self.apply_transfer).pack(side="left")

        link = tk.Frame(sec, bg=C_BG)
        link.pack(fill="x", pady=(0, 2))
        tk.Label(link, text="Coach", bg=C_BG, fg=C_DIM, width=12, anchor="w",
                 font=F).pack(side="left")
        self.coach_var = tk.StringVar()
        self.cb_coach = ttk.Combobox(link, textvariable=self.coach_var, width=26, font=F,
                                     values=self._coach_choices())
        self.cb_coach.pack(side="left", fill="x", expand=True, padx=6)
        self.cb_coach.bind("<Return>", lambda e: self.apply_club_coach())
        ttk.Button(link, text="Set", style="Ghost.TButton",
                   command=self.apply_club_coach).pack(side="left")

        field = tk.Frame(sec, bg=C_BG)
        field.pack(fill="x", pady=(0, 2))
        tk.Label(field, text="Stadium", bg=C_BG, fg=C_DIM, width=12, anchor="w",
                 font=F).pack(side="left")
        self.stadium_var = tk.StringVar()
        self.cb_stadium = ttk.Combobox(field, textvariable=self.stadium_var, width=26, font=F,
                                       values=[f"{sid}  {name}"
                                               for sid, name in sorted(self.stadium_names.items())])
        self.cb_stadium.pack(side="left", fill="x", expand=True, padx=6)
        self.cb_stadium.bind("<Return>", lambda e: self.apply_stadium())
        ttk.Button(field, text="Set", style="Ghost.TButton",
                   command=self.apply_stadium).pack(side="left")

        number = tk.Frame(sec, bg=C_BG)
        number.pack(fill="x", pady=(0, 2))
        tk.Label(number, text="Squad no.", bg=C_BG, fg=C_DIM, width=12, anchor="w",
                 font=F).pack(side="left")
        self.shirt_var = tk.StringVar()
        self.shirt_spin = tk.Spinbox(number, textvariable=self.shirt_var, from_=1, to=99, width=5,
                                      font=F, command=self.apply_shirt_number)
        self.shirt_spin.pack(side="left", padx=6)
        self.shirt_spin.bind("<Return>", lambda e: self.apply_shirt_number())
        self.shirt_spin.bind("<FocusOut>", lambda e: self.apply_shirt_number())
        tk.Label(number, text="the number this player wears in this club", bg=C_BG, fg=C_DIM,
                 font=(self.FONT, 8)).pack(side="left", padx=6)

        sec2 = self._section(parent, "Club name in the database")
        self.team_vars = {}
        for label in ("English", "Database", "Short"):
            r = tk.Frame(sec2, bg=C_BG)
            r.pack(fill="x")
            tk.Label(r, text=label, bg=C_BG, fg=C_DIM, width=12, anchor="w",
                     font=F).pack(side="left")
            if label == "Database":
                # which data_s*.cpk the loads and saves go to, not a name slot
                self.db_var = tk.StringVar(value=self._short_path(self.cpk_path))
                self.cb_db = ttk.Combobox(r, textvariable=self.db_var, width=28, font=F,
                                          state="readonly", postcommand=self._refresh_databases)
                self.cb_db.pack(side="left", fill="x", expand=True, padx=6)
                self.cb_db.bind("<<ComboboxSelected>>",
                                lambda e: self.switch_database(self.db_var.get()))
                self._refresh_databases()
                continue
            v = tk.StringVar()
            self.team_vars[label] = v
            ttk.Entry(r, textvariable=v, width=28, font=F).pack(side="left", fill="x",
                                                                   expand=True, padx=6)
            if label == "Short":                    # the button rides the last row, saving one
                ttk.Button(r, text="Apply club name", style="Ghost.TButton",
                           command=self.apply_team_name).pack(side="left")
        note = "Transfers and club names are written to Team.bin + PlayerAssignment.bin on save."
        if self.edit_saves:
            note += ("\nAn in-game edit save overrides this database: "
                     + ", ".join(os.path.basename(p) for p in self.edit_saves))
        note_lbl = tk.Label(parent, text=note, bg=C_BG, fg=C_WARN if self.edit_saves else C_DIM,
                            anchor="w", justify="left", font=(self.FONT, 8))
        note_lbl.pack(fill="x", pady=(0, 2))
        self._wrap(note_lbl)

    # ---- career club-id fields (Youth Club / Owner Club)
    def _club_ref_values(self):
        """Selector values for the career club-id fields: `id  name`, by id.

        Clubs only — team ids below 100 are national teams, and a club field must never
        offer a country (the list used to start with `1  Ireland`).
        """
        return [self._club_ref_text(t) for t in sorted(self.clubs) if t >= 100]

    def _club_ref_text(self, cid):
        """How a club-id field reads: the id with its club name when one is known."""
        if not cid:
            return "0  —"
        return f"{cid}  {self.clubs[cid]}" if cid in self.clubs else str(cid)

    def _filter_club_ref_values(self, evt, combo):
        """Type-ahead in a career club-id box: ids and club names both match."""
        if evt.keysym in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = combo.get().strip()
        values = self._club_ref_values()
        if q:
            values = [s for s in values
                      if s.split("  ", 1)[0].startswith(q)
                      or self.club_matches(q, s.split("  ", 1)[-1])][:60]
        combo["values"] = values

    def apply_club_ref(self, label):
        """Apply a career club-id field: the leading id, else an exact club name."""
        if self.index is None:
            return
        raw = self.vars[label].get().strip()
        cid = self._leading_id(raw)
        if cid is None and raw:
            cid = next((t for t, n in self.clubs.items() if t >= 100 and norm(n) == norm(raw)),
                       None)
        if cid is None:
            self.status(f"pick a club from the list for {label}")
            return
        self.vars[label].set(str(cid))
        self.apply_field(label)
        self.vars[label].set(self._club_ref_text(cid))
        self.status(f"{label}: {self._club_ref_text(cid)}")

    def _coach_choices(self):
        return [f"{cid}  {name}" for cid, name in
                sorted(self.coaches.items(), key=lambda kv: kv[1].lower())]

    @staticmethod
    def _leading_id(text):
        head = text.strip().split(" ")[0]
        return int(head) if head.isdigit() else None

    def apply_club_coach(self):
        idx = getattr(self, "team_index_current", None)
        cid = self._leading_id(self.coach_var.get())
        if idx is None or cid is None or cid not in self.coaches:
            self.status("pick a coach from the list")
            return
        if self.team_table.coach_id(idx) == cid:
            return
        self.team_table.set_coach(idx, cid)
        self._mark_table_dirty("Team")
        self.status(f"{self.team_table.name(idx)} coached by {self.coaches[cid]} — save to write it")

    def apply_stadium(self):
        idx = getattr(self, "team_index_current", None)
        sid = self._leading_id(self.stadium_var.get())
        if idx is None or sid is None or sid not in self.stadium_names:
            self.status("pick a stadium from the list")
            return
        if self.team_table.stadium_id(idx) == sid:
            return
        self.team_table.set_stadium(idx, sid)
        self._mark_table_dirty("Team")
        self.status(f"{self.team_table.name(idx)} → {self.stadium_names[sid]} — save to write it")

    def apply_shirt_number(self):
        rows = getattr(self, "shirt_rows", [])
        if not rows:
            self.status("no club row for this player — nothing to number")
            return
        try:
            number = max(1, min(99, int(self.shirt_var.get())))
        except ValueError:
            return
        if {self.assign.shirt_number(j) for j in rows} == {number}:
            return
        for j in rows:
            self.assign.set_shirt_number(j, number)
        self.shirt_var.set(str(number))
        self._mark_table_dirty("PlayerAssignment")
        self.status(f"squad number {number} written to {len(rows)} club row(s) — save to apply")

    def open_coaches(self):
        win = getattr(self, "_coach_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_set()
            return
        self._coach_win = CoachWindow(self)

    def open_appearance(self):
        if self.index is None:
            self.status("select a player first — the appearance record follows the selection")
            return
        win = getattr(self, "_app_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._app_win = AppearanceWindow(self)

    def load_club(self, i, pid):
        rows = self.assign_by_pid.get(pid, [])
        club_rows = [j for j in rows if self.assign.team(j) > pesdb.NATIONAL_MAX]
        self.assign_rows_current = club_rows
        team_id = self.assign.team(club_rows[0]) if club_rows else None
        name = self.clubs.get(team_id, "—") if team_id else "(no club row)"
        self.lbl_club.configure(
            text=f"{name}   ·   team id {team_id if team_id else '—'}   ·   "
                 f"{len(club_rows)} club row(s), {len(rows)} total")
        self.club_target.set("")
        idx = self.team_index.get(team_id)
        for label, offset, length in CLUB_NAME_SLOTS:
            self.team_vars[label].set("" if idx is None else self.team_table.slot(idx, offset, length))
        self.db_var.set(self._short_path(self.cpk_path))
        self.team_index_current = idx
        self.shirt_rows = [j for j in club_rows if self.assign.team(j) == team_id] if team_id else []
        if idx is None:
            self.coach_var.set("")
            self.stadium_var.set("")
            self.shirt_var.set("—")
            return
        cid = self.team_table.coach_id(idx)
        self.coach_var.set(f"{cid}  {self.coaches.get(cid, '—')}")
        sid = self.team_table.stadium_id(idx)
        self.stadium_var.set(f"{sid}  {self.stadium_names.get(sid, '—')}")
        self.shirt_var.set(str(self.assign.shirt_number(self.shirt_rows[0]))
                           if self.shirt_rows else "—")

    def _refresh_databases(self):
        """Fill the Database dropdown: every data_s*.cpk on disk, current one first."""
        paths = settings.find_databases()
        if self.cpk_path not in paths:
            paths.insert(0, self.cpk_path)
        self.cb_db["values"] = [self._short_path(p) for p in paths]

    @staticmethod
    def _short_path(path):
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home + os.sep) else path

    def switch_database(self, path):
        """Point the editor (loads and saves) at another install's data CPK."""
        path = os.path.expanduser(path or "")
        if not path or os.path.realpath(path) == os.path.realpath(self.cpk_path):
            return
        if self.dirty_tables:
            self.db_var.set(self._short_path(self.cpk_path))
            self.status("unsaved changes — save or revert before switching database")
            return
        game_dir = os.path.dirname(os.path.dirname(path))     # <game>/download/data_sXXXX.cpk
        if not settings.has_database(game_dir):
            self.db_var.set(self._short_path(self.cpk_path))
            self.status(f"no data_s*.cpk in {os.path.join(game_dir, 'download')}")
            return
        self.cfg["game_dir"] = game_dir
        settings.save(self.cfg)
        self.game_dir = settings.game_dir(self.cfg)
        self.cpk_path = settings.database_path(self.game_dir)
        self.backups_dir = settings.backups_dir(self.cfg, STATE_DIR)
        self.pristine = pristine_cpk(self.cpk_path)
        self.edit_saves = settings.edit_save_files()
        self.tables = load_tables(self.cpk_path)
        self.reindex()
        self._club_names = sorted({n for t, n in self.clubs.items() if n and t >= 100})
        self.cb_club["values"] = self._club_names
        self.club_filter.set("")
        self.country_filter.set("")
        self._edited_pids.clear()
        self.index = None
        self.team_index_current = None      # the old index belongs to the old Team.bin
        for v in self.team_vars.values():
            v.set("")
        self.lbl_club.configure(text="—")
        self.populate(self.q.get())
        self.db_var.set(self._short_path(self.cpk_path))
        self.status(f"database → {self.cpk_path} · {len(self.table.records):,} players")

    def _filter_target_values(self, evt):
        """Type-ahead in the Move to club box: names, accents and nicknames all match."""
        if evt.keysym in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = self.club_target.get().strip()
        self.cb_target["values"] = ([n for n in self._club_names if self.club_matches(q, n)][:60]
                                    if q else self._club_names)

    def apply_transfer(self):
        if self.index is None:
            return
        want = self.club_target.get().strip()
        if not want:
            self.status("type a club name in the Move to club box first")
            return
        exact = {t for t, n in self.clubs.items() if t >= 100 and norm(n) == norm(want)}
        ids = sorted(exact) or sorted({t for t, n in self.clubs.items()
                                       if t >= 100 and self.club_matches(want, n)})
        if not ids:
            self.status(f"no club matches {want!r} — try part of the name, or a nickname")
            return
        if len(ids) > 1:
            names = ", ".join(self.clubs[t] for t in ids[:4])
            more = f" (+{len(ids) - 4} more)" if len(ids) > 4 else ""
            self.status(f"{len(ids)} clubs match {want!r}: {names}{more} — type more of the name")
            return
        team_id = ids[0]
        rows = getattr(self, "assign_rows_current", [])
        previous = self.assign.team(rows[0]) if rows else None
        if rows:
            self.assign.set_team(rows[0], team_id)
            note = f"row {rows[0]}"
        else:                              # player had no club row: add one
            rec = bytearray(pesdb.ASSIGN_REC)
            struct.pack_into("<I", rec, 4, self.table.pid(self.index))
            struct.pack_into("<I", rec, 8, team_id)
            self.assign.records.append(rec)
            rows = [len(self.assign.records) - 1]
            self.assign_by_pid.setdefault(self.table.pid(self.index), []).append(rows[0])
            note = f"new row {rows[0]}"
        self.player_team[self.table.pid(self.index)] = team_id
        self._edited_pids.add(self.table.pid(self.index))
        if previous in self.club_counts and previous != team_id:
            self.club_counts[previous] -= 1
            self.club_counts[team_id] = self.club_counts.get(team_id, 0) + 1
        elif previous is None:
            self.club_counts[team_id] = self.club_counts.get(team_id, 0) + 1
        self.assign_rows_current = rows
        self._refresh_row()
        self._mark_table_dirty("PlayerAssignment")
        self.load_club(self.index, self.table.pid(self.index))
        self.populate(self.q.get())
        self.status(f"moved to {self.clubs.get(team_id, team_id)} ({note}) — save to write it")

    def apply_team_name(self):
        idx = getattr(self, "team_index_current", None)
        if idx is None:
            self.status("no club row selected — nothing to rename")
            return
        changed = False
        for label, offset, length in CLUB_NAME_SLOTS:
            value = self.team_vars[label].get()
            if value == self.team_table.slot(idx, offset, length):
                continue
            self.team_table.set_slot(idx, offset, length, value)
            changed = True
        if not changed:
            return
        team_id = self.team_table.tid(idx)
        self.clubs[team_id] = self.team_table.name(idx)
        self._club_names = sorted({n for t, n in self.clubs.items() if n and t >= 100})
        self.cb_club["values"] = self._club_names
        self._mark_table_dirty("Team")
        self.populate(self.q.get())
        self.status(f"renamed team {team_id} — save to write it")

    # ---------------------------------------------------------------- data
    def _debounced(self):
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(220, lambda: self.populate(self.q.get()))

    def clear_search(self):
        if not self.q.get():
            return
        self.q.set("")
        self._debounced()

    def on_save_key(self, _evt=None):
        self.save()
        return "break"

    def on_undo_key(self, _evt=None):
        self.undo()
        return "break"

    def on_redo_key(self, _evt=None):
        self.redo()
        return "break"

    def on_search_key(self, _evt=None):
        self.search_entry.focus_set()
        return "break"

    def on_clear_key(self, _evt=None):
        self.clear_search()
        return "break"

    def open_first(self, query):
        """Enter in the search box: filter, then open the first match."""
        if self._after:                 # a pending debounce would wipe the selection again
            self.after_cancel(self._after)
            self._after = None
        self.populate(query, select_first=True)

    def _filter_club_values(self, evt):
        """Type-ahead: narrow the Club dropdown to names containing what was typed."""
        if evt.keysym in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(self.club_filter.get().strip())
        self.cb_club["values"] = ([n for n in self._club_names if q in norm(n)][:80]
                                  if q else self._club_names)
        # Rebuilding the value list is cheap; repopulating the tree is not, so debounce it.
        if self._club_after:
            self.after_cancel(self._club_after)
        self._club_after = self.after(250, self.apply_club_filter)

    def apply_club_filter(self):
        if self._club_after:
            self.after_cancel(self._club_after)
            self._club_after = None
        self.populate(self.q.get())

    def _filter_country_values(self, evt):
        """Country type-ahead: narrow the dropdown, then apply the filter after a pause.

        Only re-listing the values is instant, repopulating 26k rows is not, so the apply is
        debounced exactly like the club box. Browsing 214 countries with the mouse (and losing
        the pick when the dropdown closed) stops being the only way to reach one.
        """
        if evt.keysym in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(self.country_filter.get().strip())
        self.cb_country["values"] = ([n for n in self._country_names if q in norm(n)][:80]
                                     if q else self._country_names)
        if self._country_after:
            self.after_cancel(self._country_after)
        self._country_after = self.after(250, self.populate, self.q.get())

    def apply_country_filter(self):
        """Accept a typed country: the exact name, else the single remaining suggestion."""
        if self._country_after:
            self.after_cancel(self._country_after)
            self._country_after = None
        raw = self.country_filter.get().strip()
        if raw:
            picks = [n for n in self._country_names if norm(n) == norm(raw)]
            if not picks:
                picks = [n for n in self._country_names if norm(raw) in norm(n)]
            if len(picks) == 1:
                self.country_filter.set(picks[0])
        self.populate(self.q.get())

    @staticmethod
    def alias_targets(query):
        """Club-name fragments the query stands for as a nickname, e.g. 'spur' -> tottenham."""
        q = norm(query)
        return [t for alias, targets in CLUB_ALIASES.items() if q and alias.startswith(q)
                for t in targets]

    @classmethod
    def club_matches(cls, query, name):
        """Club-name search: substring, accented or not, or a nickname from CLUB_ALIASES."""
        n, q = norm(name), norm(query)
        if not q:
            return False
        # words, not raw substrings: "real" must not match Montréal
        hit = q in n if " " in q else any(w.startswith(q) for w in n.split())
        return hit or any(t in n for t in cls.alias_targets(query))

    def _club_teams(self):
        """Team ids for the Club box: an exact name wins, otherwise every match."""
        want = self.club_filter.get().strip()
        if not want:
            return None
        exact = {t for t, n in self.clubs.items() if t >= 100 and norm(n) == norm(want)}
        return exact or {t for t, n in self.clubs.items()
                         if t >= 100 and self.club_matches(want, n)}

    def populate(self, query, select_first=False):
        q = query.strip().lower()
        if q and not q.isdigit() and any(self.club_matches(q, n)
                                         for t, n in self.clubs.items() if t >= 100):
            self.mode = "clubs"
            return self._populate_clubs(q, select_first)
        self.mode = "players"
        self._player_headings()
        teams = self._club_teams()
        nation = self.country_filter.get().strip().lower()
        rows = []
        for i in range(len(self.table.records)):
            name = self.table.name(i)
            pid = self.table.pid(i)
            club = self.clubs.get(self.player_team.get(pid, -1), "")
            if teams is not None and self.player_team.get(pid) not in teams:
                continue
            if nation and nation not in pesdb.country_name(self.table.get(i, "Nationality")).lower():
                continue
            if self.changed_only.get() and pid not in self._edited_pids:
                continue
            pos_i = self.table.get(i, "Registered Position")
            pos_name = pesdb.POS_NAMES[pos_i] if pos_i < len(pesdb.POS_NAMES) else "?"
            if self.position_filter.get() and pos_name != self.position_filter.get():
                continue
            if self.free_agents_only.get() and self.player_team.get(pid) is not None:
                continue
            if q:
                if q.isdigit():
                    if str(pid) != q and not str(pid).startswith(q):
                        continue
                elif (q not in name.lower() and q not in self.table.shirt(i).lower()
                      and not self.club_matches(q, club)):
                    continue
            rows.append((i, pid, name, club, pos_name,
                         pos_i, self.table.get(i, "Age"), club.lower()))
        key = {"id": 1, "name": 2, "club": 7, "pos": 5, "ovr": None, "age": 6}.get(self.sort_col)
        if self.sort_col == "ovr":
            rows.sort(key=lambda r: self.ovr(r[0]), reverse=self.sort_desc)
        elif self.sort_col == "club":                     # free agents stay at the bottom
            named = [r for r in rows if r[3]]
            named.sort(key=lambda r: r[7], reverse=self.sort_desc)
            rows = named + [r for r in rows if not r[3]]
        elif key is not None:
            rows.sort(key=lambda r: r[key], reverse=self.sort_desc)
        limit = 800
        shown = min(len(rows), limit)
        self.tree.delete(*self.tree.get_children())
        for r in rows[:shown]:
            i, pid, name, club, pos, _pos_i, age, _cl = r
            self.tree.insert("", "end", iid=str(i),
                             values=(pid, name, club, pos, self.ovr(i), age))
        self._select_first(select_first, shown)
        if not rows:
            if teams is not None and not teams:
                self.status(f"no club matches {self.club_filter.get().strip()!r}")
            elif teams:
                self.status(f"no match in {self.club_filter.get().strip()} — clear the Club box "
                            "or widen the search")
            else:
                self.status("no match — try a shorter name, a shirt name, a club, or an ID")
            return
        hint = "" if self.tree.selection() else (
            f" · Enter opens the first of {shown:,}" if shown > 1 else "")
        scope = f" · club: {self.club_filter.get().strip()}" if teams else ""
        if nation:
            scope += f" · country: {self.country_filter.get().strip()}"
        if self.position_filter.get():
            scope += f" · position: {self.position_filter.get()}"
        if self.free_agents_only.get():
            scope += " · free agents"
        if self.changed_only.get():
            scope += " · changed only"
        self.status(search_summary(shown, len(rows))
                    + hint + scope + f" · {len(self.table.records):,} players in table")

    # ---- clubs view
    def _populate_clubs(self, q, select_first=False):
        """Search results as clubs: one row per matching club, click to open its squad."""
        self._club_headings()
        targets = self.alias_targets(q)          # nickname hits first, then plain substrings
        rows = sorted(((tid, name) for tid, name in self.clubs.items()
                       if tid >= 100 and self.club_matches(q, name)),
                      key=lambda r: (not any(t in norm(r[1]) for t in targets), norm(r[1])))
        self.tree.delete(*self.tree.get_children())
        for tid, name in rows:
            self.tree.insert("", "end", iid=f"club:{tid}",
                             values=(tid, name, f"{self.club_counts.get(tid, 0)} players", "", "", ""))
        shown = len(rows)
        if not shown:
            self.status(f"no club matches {q!r}")
            return
        if select_first:
            return self.open_club(rows[0][0])
        self.status(f"{shown} club{'s' if shown != 1 else ''} match {q!r} — click one to open its squad")

    def open_club(self, tid):
        name = self.clubs.get(tid, "")
        self.mode = "players"
        self.club_filter.set(name)
        self.q.set("")
        self.populate("")
        self.status(f"{self.clubs.get(tid, tid)} — {self.club_counts.get(tid, 0)} players")

    def _player_headings(self):
        for c, t in (("id", "ID"), ("name", "Name"), ("club", "Club"),
                     ("pos", "Pos"), ("ovr", "OVR"), ("age", "Age")):
            if c == self.sort_col:
                t += " \u25bc" if self.sort_desc else " \u25b2"
            self.tree.heading(c, text=t, command=lambda c=c: self.sort_by(c))

    def _club_headings(self):
        for c, t in (("id", "Team ID"), ("name", "Club"), ("club", "Squad"),
                     ("pos", ""), ("ovr", ""), ("age", "")):
            self.tree.heading(c, text=t, command="")

    def sort_by(self, col):
        if self.mode == "clubs":
            return
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, col in ("ovr", "age", "id")
        self.populate(self.q.get())

    def _select_first(self, select_first, shown):
        rows = self.tree.get_children()
        if rows and (select_first or shown == 1):
            self.tree.selection_set(rows[0])
            self.tree.see(rows[0])
            return True
        return False

    def on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        if sel[0].startswith("club:"):
            return self.open_club(int(sel[0][5:]))
        self.index = int(sel[0])
        self.load_form()

    def on_select_refresh(self):
        if self.index is not None:
            # reload every table from disk, then this record
            ensure_vanilla(self.cpk_path)
            self.tables = load_tables(self.cpk_path)
            self.reindex()
            self._edited_pids.clear()
            self.load_form()
            self.dirty = False
            self.lbl_dirty.configure(text="")
            self.status("reverted unsaved changes to the loaded tables")

    def load_portrait(self, pid):
        """Show the player's face. One 17 KB fetch per player ever, cached under STATE_DIR."""
        self.portrait_pid = pid
        if not self.cfg.get("photos", True):
            self._show_portrait(None)
            return
        path = os.path.join(PORTRAIT_DIR, f"{pid}.png")
        if os.path.exists(path):
            self._show_portrait(path)
            return
        self._show_portrait(None)
        if pid in self._portrait_missing or pid in self._portrait_busy:
            return
        if time.time() - self._net_failed < 60:     # went offline recently: don't retry on every selection
            return
        self._portrait_busy.add(pid)
        if not self._pump_running:               # idle: the pump only exists while work does
            self._pump_running = True
            self._portrait_job = self.after(150, self._pump_portraits)

        def fetch():
            try:
                os.makedirs(PORTRAIT_DIR, exist_ok=True)
                req = urllib.request.Request(PORTRAIT_URL.format(pid=pid),
                                             headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = r.read()
                if data[:8] != b"\x89PNG\r\n\x1a\n":
                    self._portrait_missing.add(pid)
                    return
                tmp = f"{path}.{os.getpid()}.part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except urllib.error.HTTPError:
                self._portrait_missing.add(pid)      # no pesmaster entry for this id
                return
            except (urllib.error.URLError, TimeoutError, OSError):
                self._net_failed = time.time()       # offline: placeholder stays, retry later
                return
            finally:
                self._portrait_busy.discard(pid)
            self._portrait_queue.put((pid, path))

        threading.Thread(target=fetch, daemon=True).start()

    def _pump_portraits(self):
        """Main-thread drain of finished fetches; Tk calls from a worker thread are not safe."""
        while True:
            try:
                pid, path = self._portrait_queue.get_nowait()
            except queue.Empty:
                break
            if pid == self.portrait_pid:
                self._show_portrait(path)
        if self._portrait_busy or not self._portrait_queue.empty():
            self._portrait_job = self.after(150, self._pump_portraits)
        else:
            self._pump_running = False
            self._portrait_job = None

    def destroy(self):
        for attr in ("_portrait_job", "_radar_job", "_after", "_club_after",
                     "_resize_repaint_job", "_one_page_job"):
            job = getattr(self, attr, None)
            if job:
                setattr(self, attr, None)
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            super().destroy()
        except tk.TclError:
            pass          # Tk 9 raises when a child already removed its own command

    def _show_portrait(self, path):
        if not path:
            self.photo_img = None
            self.photo.configure(image="", text="no photo")
            return
        try:
            img = tk.PhotoImage(master=self, file=path)     # never the default root
        except tk.TclError:
            return
        # Tk clips an image that is bigger than its label; halve it instead of cropping the face.
        side = int(PHOTO_SIDE * self.ui)
        if img.width() > side or img.height() > side:
            img = img.subsample(2)
        self.photo_img = img          # keep a reference or Tk drops the image
        self.photo.configure(image=img, text="")

    def _refresh_row(self):
        """Rewrite the selected row from the live table (OVR/Age/Name change after an edit)."""
        i = self.index
        if i is None or not self.tree.exists(str(i)):
            return
        t = self.table
        pid = t.pid(i)
        pos_i = t.get(i, "Registered Position")
        self.tree.item(str(i), values=(
            pid, t.name(i), self.clubs.get(self.player_team.get(pid, -1), ""),
            pesdb.POS_NAMES[pos_i] if pos_i < len(pesdb.POS_NAMES) else "?",
            self.ovr(i), t.get(i, "Age")))

    def load_form(self):
        i = self.index
        t = self.table
        pid = t.pid(i)
        club = self.clubs.get(self.player_team.get(pid, -1), "—")
        natural = "/".join(nm for nm in pesdb.POS_NAMES if t.get(i, "Pos " + nm) >= 1) or "—"
        foot = "left foot" if t.get(i, "Stronger Foot") else "right foot"
        self.lbl_hero.configure(text=t.name(i) or "(no name)")
        self.lbl_sub.configure(text=f"ID {pid}   ·   {club}   ·   "
                                   f"{pesdb.country_name(t.get(i, 'Nationality')) or '—'}   ·   "
                                   f"{natural}   ·   {foot}")
        self.ovr_big.configure(text=str(self.ovr(i)))
        style_i = t.get(i, "Playing Style")
        for name, text in (("Style", PLAY_STYLES[style_i] if style_i < len(PLAY_STYLES)
                            else str(style_i)),
                           ("Height", f"{t.get(i, 'Height (cm)')} cm"),
                           ("Weight", f"{t.get(i, 'Weight (kg)')} kg"),
                           ("Age", f"age {t.get(i, 'Age')}"),
                           ("Form", f"form {t.get(i, 'Form')}")):
            self._card_chips[name].set_text(text)
        self._card_chips["Style"].set_state(fg=C_ACCENT, line=C_ACCENT)
        self.load_club(i, pid)
        self.load_portrait(pid)
        self.tree.set(str(i), "name", t.name(i)) if self.tree.exists(str(i)) else None

        self.vars["name"].set(t.name(i))
        self.vars["shirt"].set(t.shirt(i))

        for label, bit, width, add in pesdb.PHYSICAL:
            v = self.vars[label]
            val = t.get(i, label)
            if label == "Registered Position":
                v.set(pesdb.POS_NAMES[val] if val < len(pesdb.POS_NAMES) else str(val))
            elif label == "Playing Style":
                v.set(PLAY_STYLES[val] if val < len(PLAY_STYLES) else str(val))
            elif label == "Stronger Foot":
                v.set("Left" if val else "Right")
            else:
                v.set(str(val))
        for label, bit, width, add in pesdb.MOTIONS:
            self.vars[label].set(str(t.get(i, label)))
        for label, *_ in pesdb.CAREER:
            if label in ("Youth Club", "Owner Club"):
                self.vars[label].set(self._club_ref_text(t.get(i, label)))
            else:
                self.vars[label].set(str(t.get(i, label)))
        for label, bit in pesdb.POSITIONS:
            self.vars["Pos " + label].set(["none", "can play", "natural"][t.get(i, "Pos " + label)])
            self._style_position_chip(label)
        for label, bit, width, add in pesdb.ATTRS:
            val = t.get(i, label)
            self.vars[label].set(str(val))
            self._draw_bar(self.vars[label + "::bar"], val)
        for label, bit in pesdb.SKILLS:
            self.vars["Skill " + label].set(t.get(i, "Skill " + label))
            self._style_skill_chip(label)
        self._clear_selection()
        self.draw_radar(i)

    # ---------------------------------------------------------------- edits
    def _mark_dirty(self):
        self.dirty = True
        self.lbl_dirty.configure(text="● unsaved")

    def _mark_table_dirty(self, name):
        self.dirty_tables.add(name)
        self._mark_dirty()

    # ------------------------------------------------------------- undo / redo
    def _push_undo(self, undo, redo):
        """Record one reversible edit; history is bounded so a session cannot grow forever."""
        self._undo.append((undo, redo))
        del self._undo[:-200]

    def _set_player_field(self, index, label, value):
        """The single write path for a player value: table, form, bar, radar, row."""
        self.table.set(index, label, value)
        self._edited_pids.add(self.table.pid(index))
        if self.index == index:
            if label in ("Youth Club", "Owner Club"):
                self.vars[label].set(self._club_ref_text(value))
            elif label.startswith("Pos "):
                self.vars[label].set(["none", "can play", "natural"][value])
            else:
                self.vars[label].set(str(value))
            if label.startswith("Pos "):
                self._style_position_chip(label[4:])
            elif label.startswith("Skill "):
                self._style_skill_chip(label[6:])
            bar = self.vars.get(label + "::bar")
            if bar is not None:
                self._draw_bar(bar, value)
                self.draw_radar()
        self._refresh_row()
        self._mark_table_dirty("Player")

    def undo(self):
        if not self._undo:
            self.status("nothing to undo")
            return
        undo, redo = self._undo.pop()
        undo()
        self._redo.append((undo, redo))
        self.status("undone")

    def redo(self):
        if not self._redo:
            self.status("nothing to redo")
            return
        undo, redo = self._redo.pop()
        redo()
        self._undo.append((undo, redo))
        self.status("redone")

    def draw_radar(self, i=None):
        """Spider chart of the six ability groups; the shape eases to each new player/value."""
        c = getattr(self, "radar", None)
        if c is None:
            return
        i = self.index if i is None else i
        w, h = c.winfo_width(), int(c.cget("height"))
        if w < 80:
            return
        if self._radar_job is not None:
            self.radar.after_cancel(self._radar_job)
            self._radar_job = None
        c.delete("all")
        self._radar_poly = None
        cx, cy = w / 2, h / 2 + 4
        r = min(w, h) / 2 - 44
        angles = [-math.pi / 2 + 2 * math.pi * k / len(RADAR_GROUPS)
                  for k in range(len(RADAR_GROUPS))]
        target = []
        texts = []
        for (name, labels), a in zip(RADAR_GROUPS, angles):
            if i is None:
                texts.append(name)
                target.append(None)
                continue
            avg = group_average(lambda label: self.table.get(i, label), labels)
            shown = RADAR_SHORT.get(name, name) if c.winfo_width() < 390 else name
            texts.append(f"{shown} {round(avg)}")
            target.append(max(0.0, min(1.0, (avg - 40) / 59)))
        for frac in (0.25, 0.5, 0.75, 1.0):
            c.create_polygon([v for a in angles
                              for v in (cx + r * frac * math.cos(a), cy + r * frac * math.sin(a))],
                             outline=C_LINE, fill="")
        for a, text in zip(angles, texts):
            c.create_line(cx, cy, cx + r * math.cos(a), cy + r * math.sin(a), fill=C_LINE)
            anchor = "w" if math.cos(a) > 0.3 else ("e" if math.cos(a) < -0.3 else "center")
            c.create_text(cx + (r + 8) * math.cos(a), cy + (r + 8) * math.sin(a),
                          text=text, fill=C_DIM, font=(self.FONT, 8), anchor=anchor)
        self._radar_axes = (cx, cy, r, angles)
        if i is None or any(t is None for t in target):
            self._radar_vals = self._radar_target = None
            return
        if self._radar_vals is None or not len(self._radar_vals) == len(target):
            self._radar_vals = [0.0] * len(target)      # a new player grows out of the centre
        self._radar_target = target
        self._radar_poly = c.create_polygon(self._radar_points(self._radar_vals),
                                            fill=C_SEL, outline=C_ACCENT, width=2)
        if any(abs(v - t) > 0.004 for v, t in zip(self._radar_vals, target)):
            self._radar_job = c.after(16, self._radar_tick)

    def _radar_points(self, fracs):
        cx, cy, r, angles = self._radar_axes
        return [v for f, a in zip(fracs, angles)
                for v in (cx + r * f * math.cos(a), cy + r * f * math.sin(a))]

    def _radar_tick(self):
        self._radar_job = None
        vals, target = self._radar_vals, self._radar_target
        if vals is None or target is None or self._radar_poly is None:
            return
        moving = False
        for k, t in enumerate(target):
            d = t - vals[k]
            if abs(d) > 0.004:
                vals[k] += d * 0.28          # ease out, ~250 ms to settle
                moving = True
            else:
                vals[k] = t
        self.radar.coords(self._radar_poly, *self._radar_points(vals))
        if moving:
            self._radar_job = self.radar.after(16, self._radar_tick)

    def drag_bar(self, label, evt):
        """Click or drag an ability bar to set it: left edge 40, right edge 99."""
        bar = self.vars[label + "::bar"]
        val = bar_value(evt.x, int(bar.cget("width")))
        if str(val) != self.vars[label].get():
            self.vars[label].set(str(val))
            self.apply_field(label)

    def _draw_bar(self, bar, val):
        """Redraw one ability bar; the fill spans the canvas, not a fixed 90 px."""
        bar.delete("all")
        frac = max(0.0, min(1.0, (val - 40) / 59))
        bar.create_rectangle(0, 0, int(bar.cget("width")) * frac, int(bar.cget("height")),
                             fill=C_ACCENT if val >= 80 else
                             (C_ACCENT_LO if val >= 65 else C_LINE_SOFT), width=0)

    @staticmethod
    def _field_range(label):
        """(low, high) for a numeric field: abilities stop at 99 even though their bit field allows more."""
        _bit, width, add = pesdb.FIELD_BY_LABEL[label]
        hi = add + (1 << width) - 1
        if label in {l for l, *_ in pesdb.ATTRS}:
            hi = min(hi, 99)
        return add, hi

    def apply_field(self, label):
        if self.index is None:
            return
        raw = self.vars[label].get().strip()
        try:
            val = int(float(raw))
        except ValueError:
            return
        lo, hi = self._field_range(label)
        val = max(lo, min(hi, val))
        old = self.table.get(self.index, label)
        if old == val:
            self.vars[label].set(str(val))       # normalise the text, write nothing
            return
        i = self.index
        self._set_player_field(i, label, val)
        self._push_undo(lambda: self._set_player_field(i, label, old),
                        lambda: self._set_player_field(i, label, val))

    def apply_style(self):
        if self.index is None:
            return
        name = self.vars["Playing Style"].get().strip()
        if name not in PLAY_STYLES:
            return                       # an unknown style index: leave the record alone
        val = PLAY_STYLES.index(name)
        old = self.table.get(self.index, "Playing Style")
        if old == val:
            return
        i = self.index
        self._set_player_field(i, "Playing Style", val)
        self._push_undo(lambda: self._set_player_field(i, "Playing Style", old),
                        lambda: self._set_player_field(i, "Playing Style", val))

    def apply_foot(self):
        if self.index is None:
            return
        foot = 1 if self.vars["Stronger Foot"].get() == "Left" else 0
        old = self.table.get(self.index, "Stronger Foot")
        if old == foot:
            return
        i = self.index
        self._set_player_field(i, "Stronger Foot", foot)
        self._push_undo(lambda: self._set_player_field(i, "Stronger Foot", old),
                        lambda: self._set_player_field(i, "Stronger Foot", foot))

    def apply_position(self, label):
        if self.index is None:
            return
        field = "Pos " + label
        val = ["none", "can play", "natural"].index(self.vars[field].get())
        old = self.table.get(self.index, field)
        if old == val:
            return
        i = self.index
        self._set_player_field(i, field, val)
        self._push_undo(lambda: self._set_player_field(i, field, old),
                        lambda: self._set_player_field(i, field, val))

    def apply_skill(self, label):
        if self.index is None:
            return
        field = "Skill " + label
        val = int(self.vars[field].get())
        old = self.table.get(self.index, field)
        if old == val:
            return
        i = self.index
        self._set_player_field(i, field, val)
        self._push_undo(lambda: self._set_player_field(i, field, old),
                        lambda: self._set_player_field(i, field, val))

    def apply_name(self, key):
        if self.index is None:
            return
        cur = self.table.name(self.index) if key == "name" else self.table.shirt(self.index)
        val = self.vars[key].get()
        if val == cur:
            return
        i = self.index

        def write(text):
            if key == "name":
                self.table.set_name(i, full=text, shirt=self.table.shirt(i))
            else:
                self.table.set_name(i, full=self.table.name(i), shirt=text)
            self.vars[key].set(text)
            self._refresh_row()
            self._mark_table_dirty("Player")
            self._edited_pids.add(self.table.pid(i))

        write(val)
        self._push_undo(lambda: write(cur), lambda: write(val))

    def bulk_apply(self, label, op, amount):
        """Set or shift one numeric field on every selected player, as one undo step."""
        items = [int(i) for i in self.tree.selection()]
        if not items or label not in self.NUMERIC_FIELDS:
            self.status("select players and a numeric field first")
            return 0
        lo, hi = self._field_range(label)
        olds = {i: self.table.get(i, label) for i in items}
        for i, old in olds.items():
            new = amount if op == "set" else old + amount
            self._set_player_field(i, label, max(lo, min(hi, new)))
        self._push_undo(
            lambda: [self._set_player_field(i, label, v) for i, v in olds.items()],
            lambda: [self._set_player_field(i, label,
                                            max(lo, min(hi, amount if op == "set" else olds[i] + amount)))
                     for i in olds])
        self.status(f"{op} {amount:+d} {label} on {len(items)} players — save to write it")
        return len(items)

    def bulk_dialog(self):
        if not self.tree.selection():
            self.status("select one or more players first")
            return
        dlg = tk.Toplevel(self)
        dlg.title("Bulk edit")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        body = tk.Frame(dlg, bg=C_BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        field = tk.StringVar(value="Speed")
        op = tk.StringVar(value="add")
        amount = tk.StringVar(value="1")
        tk.Label(body, text="Field", bg=C_BG, fg=C_DIM).grid(row=0, column=0, sticky="w")
        ttk.Combobox(body, textvariable=field, values=self.NUMERIC_FIELDS, width=22,
                     state="readonly").grid(row=0, column=1, sticky="we", padx=8, pady=3)
        tk.Label(body, text="How", bg=C_BG, fg=C_DIM).grid(row=1, column=0, sticky="w")
        ttk.Combobox(body, textvariable=op, values=("add", "set"), width=8,
                     state="readonly").grid(row=1, column=1, sticky="w", padx=8, pady=3)
        tk.Label(body, text="Value", bg=C_BG, fg=C_DIM).grid(row=2, column=0, sticky="w")
        tk.Spinbox(body, textvariable=amount, from_=-99, to=99, width=6).grid(
            row=2, column=1, sticky="w", padx=8, pady=3)
        ttk.Button(body, text="Apply", style="Save.TButton",
                   command=lambda: (self.bulk_apply(field.get(), op.get(),
                                                    int(float(amount.get() or 0))),
                                    dlg.destroy())).grid(row=3, column=0, columnspan=2,
                                                         sticky="e", pady=(10, 0))
        self._apply_cursors()

    def compare_rows(self, a, b):
        """[(ability, value in a, value in b), ...] in the order the Attributes tab shows."""
        return [(label, self.table.get(a, label), self.table.get(b, label))
                for label, _bit, _w, _add in pesdb.ATTRS]

    def compare_dialog(self):
        sel = [int(i) for i in self.tree.selection()]
        if len(sel) != 2:
            self.status("select exactly two players (ctrl-click), then Compare")
            return None
        a, b = sel
        dlg = tk.Toplevel(self)
        dlg.title(f"{self.table.name(a)}  vs  {self.table.name(b)}")
        dlg.configure(bg=C_BG)
        head = tk.Frame(dlg, bg=C_BG, padx=16, pady=12)
        head.pack(fill="both", expand=True)
        for col, text, fg, font, width in (
                (0, "Ability", C_DIM, (self.FONT, 9), 21),
                (1, self.table.name(a), C_ACCENT, (self.FONT, 11, "bold"), 18),
                (2, self.table.name(b), C_ACCENT, (self.FONT, 11, "bold"), 18),
                (3, "±", C_DIM, (self.FONT, 9), 5)):
            tk.Label(head, text=text, bg=C_BG, fg=fg, font=font, width=width, anchor="w").grid(
                row=0, column=col, sticky="w", padx=6, pady=(0, 8))
        for n, (label, va, vb) in enumerate(self.compare_rows(a, b)):
            diff = va - vb
            colour = C_GREEN if diff > 0 else (C_MAGENTA if diff < 0 else C_DIM)
            tk.Label(head, text=label, bg=C_BG, fg=C_DIM, anchor="w", width=21).grid(
                row=n + 1, column=0, sticky="w", padx=6)
            tk.Label(head, text=va, bg=C_BG, fg=C_FG, width=5, anchor="e").grid(
                row=n + 1, column=1, sticky="e", padx=6)
            tk.Label(head, text=vb, bg=C_BG, fg=C_FG, width=5, anchor="e").grid(
                row=n + 1, column=2, sticky="e", padx=6)
            tk.Label(head, text=f"{diff:+d}" if diff else "=", bg=C_BG, fg=colour,
                     width=5, anchor="e").grid(row=n + 1, column=3, sticky="e", padx=6)
        return dlg

    # ---------------------------------------------------------------- io
    def export_roster(self, path):
        import cli                                     # the CLI owns the CSV format; keep it in one place
        _path, written = cli.export_csv(self.table, path)     # returns (path, rows)
        self.status(f"exported {written:,} players → {path}")
        return written

    def import_roster(self, path):
        import cli
        try:
            touched = cli.import_csv(self.table, path, quiet=True)   # returns rows touched
        except SystemExit as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return 0
        self._mark_table_dirty("Player")
        if self.index is not None:          # no player on screen: nothing to redraw
            self.load_form()
        self.status(f"imported {touched:,} rows — save to write it · "
                    "\"Changed only\" stays empty after an import")
        return touched

    def _export_dialog(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", initialfile="roster.csv",
            filetypes=[("CSV", "*.csv")], title="Export roster CSV")
        if path:
            self.export_roster(path)

    def _import_dialog(self):
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("CSV", "*.csv")], title="Import roster CSV")
        if not path:
            return
        if self.dirty_tables and not messagebox.askokcancel(
                "Unsaved changes",
                "You have unsaved edits. Importing a roster CSV replaces every value the file "
                "carries, and an import cannot be undone.\n\nImport anyway?",
                parent=self, default="cancel"):
            return
        self.import_roster(path)

    def write_override(self):
        """Patch every changed table into the CPK. Returns [(name, bytes, rows re-read)]."""
        written = []
        slots = os.path.join(STATE_DIR, "slots.json")
        kinds = {"Player": pesdb.PlayerTable, "Team": pesdb.TeamTable,
                 "PlayerAssignment": pesdb.AssignmentTable, "Coach": pesdb.CoachTable,
                 "PlayerAppearance": pesdb.AppearanceTable}
        for name in ("Player", "Team", "PlayerAssignment", "Coach", "PlayerAppearance"):
            if name not in self.dirty_tables:
                continue
            data = self.tables[name].to_file()
            pesdb.patch_table(self.cpk_path, name, data, self.backups_dir, slots)
            check = kinds[name](pesdb.table_blob(self.cpk_path, name))
            written.append((name, len(data), len(check.records)))
        settings.prune_backups(self.backups_dir, self.cfg["max_backups"])
        return written

    def game_running(self):
        """PES keeps the CPK open while it runs; writing then can corrupt it."""
        try:
            out = subprocess.run(["pgrep", "-x", "FL_2027.exe"], capture_output=True)
        except OSError:
            return False
        return out.returncode == 0

    def save(self):
        if not self.dirty_tables:
            self.status("nothing to save")
            return
        if self.game_running() and not messagebox.askokcancel(
                "The game is running",
                "SP Football Life 2027 is open. Writing the database while it runs can be "
                "ignored by the game or, worse, leave the CPK half-written.\n\n"
                "Close the game and save again — or continue at your own risk.",
                parent=self, default="cancel"):
            return
        what = ", ".join(sorted(n + ".bin" for n in self.dirty_tables))
        if self.cfg["confirm_save"] and not messagebox.askokcancel(
                "Save to game",
                f"Write {what} into {os.path.basename(self.cpk_path)}?\n\n"
                "The CPK is backed up first. Changes load next time the game starts.",
                parent=self):
            return
        try:
            written = self.write_override()
        except PermissionError:
            messagebox.showerror(
                "Cannot write the database",
                f"{self.cpk_path}\n\n"
                "The game folder is not writable by this user. install the game somewhere "
                "you own (a Steam library under your home is fine) or fix the folder "
                "permissions, then save again.", parent=self)
            return
        except OSError as exc:
            messagebox.showerror("Cannot write the database", str(exc), parent=self)
            return
        ok = all(re_read == len(self.tables[name].records) for name, _, re_read in written)
        self.dirty = False
        self.dirty_tables.clear()
        self.lbl_dirty.configure(text="")
        summary = " · ".join(f"{name} {size:,} B → {n:,} rows" for name, size, n in written)
        self.status(f"wrote {summary} ({'ok' if ok else 'MISMATCH'})"
                    " · applies next time the game starts")

    def _park_if_asked(self):
        """PESFORGE_PARK=<workspace> (set by the launcher): open there without pulling the view over."""
        ws = os.environ.get("PESFORGE_PARK")
        if ws:
            self.after(60, lambda: self._park_window(ws))

    def _park_window(self, ws, tries=20):
        env = self._hypr_env()
        addr = self._hypr_address(env) if env else None   # unmapped windows are not clients yet
        if env and addr:
            subprocess.run(["hyprctl", "dispatch",
                            f'hl.dsp.window.move({{window="address:{addr}", workspace="{ws}", follow=false}})'],
                           capture_output=True, timeout=5, env=env)
        elif tries:
            self.after(60, lambda: self._park_window(ws, tries - 1))

    @staticmethod
    def _hypr_env():
        """Environment that lets hyprctl find the compositor, or None when this is not Hyprland."""
        env = dict(os.environ)
        if "HYPRLAND_INSTANCE_SIGNATURE" not in env:
            base = os.path.join(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "hypr")
            try:
                env["HYPRLAND_INSTANCE_SIGNATURE"] = sorted(os.listdir(base))[0]
            except (OSError, IndexError):
                return None
        return env

    @staticmethod
    def _hypr_address(env):
        """Our own Hyprland window address, or None when hyprctl cannot answer."""
        try:
            out = subprocess.run(["hyprctl", "-j", "clients"], capture_output=True,
                                 text=True, timeout=5, env=env).stdout
            for client in json.loads(out):
                if client.get("pid") == os.getpid():
                    return client["address"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        return None

    def on_close(self):
        """The window's close button: ask before throwing away unwritten changes."""
        if self.dirty_tables and not messagebox.askokcancel(
                "Unsaved changes",
                "You have changes that were not written to the game file.\n\nClose anyway?",
                parent=self, default="cancel"):
            return
        self.destroy()

    def reload(self, note=None):
        ensure_vanilla(self.cpk_path)
        self.tables = load_tables(self.cpk_path)
        self.reindex()
        self._edited_pids.clear()
        self.populate(self.q.get())
        self.dirty = False
        self.lbl_dirty.configure(text="")
        self.status(note or "reloaded the current database (override if present, else the CPK copy)")

    def restore_vanilla(self):
        """Put the shipped CPK back (snapshot taken before the first patch)."""
        os.makedirs(self.backups_dir, exist_ok=True)
        shutil.copy2(self.cpk_path, os.path.join(
            self.backups_dir,
            f"{os.path.splitext(os.path.basename(self.cpk_path))[0]}.before-restore-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.cpk"))
        shutil.copy2(self.pristine, self.cpk_path)
        os.remove(VANILLA) if os.path.exists(VANILLA) else None
        self.reload("restored the shipped CPK — the game is back on the original database")

    # ---------------------------------------------------------------- settings
    def _pick_dir(self, var):
        start = os.path.expanduser(var.get().strip()) or self.game_dir
        chosen = filedialog.askdirectory(initialdir=start, parent=self, mustexist=True)
        if chosen:
            var.set(chosen)

    def open_backups(self):
        os.makedirs(self.backups_dir, exist_ok=True)
        import subprocess
        subprocess.Popen(["xdg-open", self.backups_dir])

    def about(self):
        messagebox.showinfo("About",
                            f"{settings.APP_NAME}\n\n"
                            f"Game folder:  {self.game_dir}\n"
                            f"Database:     {self.cpk_path}\n"
                            f"Backups:      {self.backups_dir}\n"
                            f"Settings:     {settings.CONFIG_PATH}\n\n"
                            f"{len(self.table.records):,} players in the loaded table.",
                            parent=self)

    def open_settings(self):
        dlg = tk.Toplevel(self, bg=C_BG)
        dlg.title("Settings")
        dlg.transient(self)
        dlg.resizable(False, False)

        game = tk.StringVar(value=self.game_dir)
        backups = tk.StringVar(value=self.cfg["backups_dir"])
        keep = tk.StringVar(value=str(self.cfg["max_backups"]))
        confirm = tk.BooleanVar(value=self.cfg["confirm_save"])
        photos = tk.BooleanVar(value=self.cfg.get("photos", True))

        body = tk.Frame(dlg, bg=C_BG, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="SETTINGS", bg=C_BG, fg=C_ACCENT,
                 font=(self.FONT, 13, "bold")).grid(row=0, column=0, columnspan=3,
                                                       sticky="w", pady=(0, 12))

        def row(r, label, var):
            tk.Label(body, text=label, bg=C_BG, fg=C_DIM, anchor="w").grid(
                row=r, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=var, width=56).grid(
                row=r, column=1, sticky="we", padx=8, pady=4)
            ttk.Button(body, text="Browse…", style="Ghost.TButton",
                       command=lambda: self._pick_dir(var)).grid(row=r, column=2, sticky="w")

        row(1, "Game folder", game)
        row(2, "Backup folder", backups)
        tk.Label(body, text="blank = <state>/backups", bg=C_BG, fg=C_DIM,
                 font=(self.FONT, 8)).grid(row=3, column=1, sticky="w", padx=8)

        tk.Label(body, text="Keep backups", bg=C_BG, fg=C_DIM, anchor="w").grid(
            row=4, column=0, sticky="w", pady=4)
        tk.Spinbox(body, textvariable=keep, from_=0, to=999, width=5).grid(
            row=4, column=1, sticky="w", padx=8, pady=4)
        tk.Label(body, text="0 keeps every backup", bg=C_BG, fg=C_DIM,
                 font=(self.FONT, 8)).grid(row=4, column=2, sticky="w")

        ttk.Checkbutton(body, text="Ask before writing the game file", variable=confirm,
                        style="TCheckbutton").grid(row=5, column=0, columnspan=2,
                                                   sticky="w", pady=(10, 0))
        ttk.Checkbutton(body, text="Load player photos from pesmaster (online)",
                        variable=photos, style="TCheckbutton").grid(row=6, column=0, columnspan=2,
                                                                    sticky="w", pady=(6, 0))

        err = tk.Label(body, text="", bg=C_BG, fg=C_MAGENTA, anchor="w",
                       font=(self.FONT, 9))
        err.grid(row=7, column=0, columnspan=3, sticky="we", pady=(8, 0))

        def commit():
            gd = os.path.expanduser(game.get().strip())
            pid = self.table.pid(self.index) if self.index is not None else None
            cpk = settings.database_path(gd)
            if not os.path.exists(cpk):
                err.configure(text=f"no download/data_s*.cpk in {gd}")
                return
            try:
                max_backups = max(0, int(float(keep.get())))
            except ValueError:
                err.configure(text="Keep backups must be a number")
                return
            self.cfg.update(game_dir=gd, backups_dir=backups.get().strip(),
                            max_backups=max_backups, confirm_save=bool(confirm.get()),
                            photos=bool(photos.get()))
            settings.save(self.cfg)
            self.game_dir = settings.game_dir(self.cfg)
            self.backups_dir = settings.backups_dir(self.cfg, STATE_DIR)
            self.cpk_path = settings.database_path(self.game_dir)
            dlg.destroy()
            self.pristine = pristine_cpk(self.cpk_path)
            self.edit_saves = settings.edit_save_files()
            self.tables = load_tables(self.cpk_path)
            self.reindex()
            self._club_names = sorted({n for t, n in self.clubs.items() if n and t >= 100})
            self.cb_club["values"] = self._club_names
            self.club_filter.set("")
            self.country_filter.set("")
            self.index = None
            self.populate(self.q.get())
            if pid is not None:              # a just-enabled photo setting shows at once
                self.load_portrait(pid)
            kept = settings.prune_backups(self.backups_dir, self.cfg["max_backups"])
            note = f"settings saved → {settings.CONFIG_PATH}"
            if os.environ.get("SPFL_DIR"):
                note += " · SPFL_DIR is set and still wins over the game folder"
            if kept:
                note += f" · pruned {kept} old backup(s)"
            self.status(f"{note} · {len(self.table.records):,} players")

        buttons = tk.Frame(body, bg=C_BG)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", style="Save.TButton",
                   command=commit).pack(side="right", padx=8)
        dlg.bind("<Return>", lambda e: commit())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self._apply_cursors()
        dlg.focus_set()

    def status(self, msg):
        self.statusbar.configure(text=msg)


class CoachWindow(tk.Toplevel):
    """Every coach in Coach.bin: name, Japanese name, nationality, adaptability, club."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.index = None
        self.title("Coaches — Coach.bin")
        self.configure(bg=C_BG)
        ui = app.ui
        self.geometry(f"{int(860 * ui)}x{int(680 * ui)}")
        self.transient(app)

        top = tk.Frame(self, bg=C_BG)
        top.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(top, text="Search", bg=C_BG, fg=C_DIM).pack(side="left")
        self.q = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.q)
        ent.pack(side="left", fill="x", expand=True, padx=8)
        ent.bind("<KeyRelease>", lambda _e: self.fill())

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=12)
        self.tree = ttk.Treeview(body, columns=("id", "name", "country", "club", "adapt"),
                                 show="headings", selectmode="browse")
        for col, width, text in (("id", 64, "ID"), ("name", 230, "Name"),
                                 ("country", 130, "Nationality"), ("club", 190, "Club"),
                                 ("adapt", 60, "Adapt")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=int(width * ui), anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        bar.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.bind("<<TreeviewSelect>>", self.pick)

        # club = the team whose record names this coach (Team.bin +0)
        self.club_of = {}
        for i in range(len(app.team_table.records)):
            self.club_of.setdefault(app.team_table.coach_id(i), app.team_table.name(i))

        form = tk.Frame(self, bg=C_BG)
        form.pack(fill="x", padx=12, pady=(12, 0))
        self.vars = {}
        for n, label in enumerate(("Name", "Japanese name", "Nationality", "Adaptability")):
            tk.Label(form, text=label, bg=C_BG, fg=C_DIM, anchor="w", width=14).grid(
                row=n // 2, column=(n % 2) * 2, sticky="w", pady=3)
            v = tk.StringVar()
            self.vars[label] = v
            if label == "Adaptability":
                w = tk.Spinbox(form, textvariable=v, from_=40, to=99, width=8)
            elif label == "Nationality":
                w = tk.Spinbox(form, textvariable=v, from_=0, to=511, width=8)
            else:
                w = ttk.Entry(form, textvariable=v, width=30)
            w.grid(row=n // 2, column=(n % 2) * 2 + 1, sticky="w", padx=(0, 18), pady=3)
        self.note = tk.Label(self, text="", bg=C_BG, fg=C_ACCENT, anchor="w",
                             font=(app.FONT, 9))
        self.note.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Button(self, text="Apply to coach", style="Save.TButton",
                   command=self.apply).pack(anchor="e", padx=12, pady=12)

        self.fill()

    def fill(self):
        needle = self.q.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        t = self.app.coach_table
        for i in range(len(t.records)):
            name = t.name(i)
            club = self.club_of.get(t.cid(i), "")
            if needle and needle not in name.lower() and needle not in club.lower():
                continue
            self.tree.insert("", "end", iid=str(i), values=(
                t.cid(i), name, pesdb.country_name(t.country(i)) or t.country(i),
                club, t.adaptability(i)))

    def pick(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        t = self.app.coach_table
        self.index = int(sel[0])
        self.vars["Name"].set(t.name(self.index))
        self.vars["Japanese name"].set(t.japanese(self.index))
        self.vars["Nationality"].set(str(t.country(self.index)))
        self.vars["Adaptability"].set(str(t.adaptability(self.index)))

    def apply(self):
        if self.index is None:
            self.note.configure(text="pick a coach in the list first")
            return
        t = self.app.coach_table
        try:
            country = max(0, min(511, int(self.vars["Nationality"].get())))
            adapt = max(40, min(99, int(self.vars["Adaptability"].get())))
        except ValueError:
            self.note.configure(text="nationality and adaptability are numbers")
            return
        t.set_name(self.index, self.vars["Name"].get())
        t.set_japanese(self.index, self.vars["Japanese name"].get())
        t.set_country(self.index, country)
        t.set_adaptability(self.index, adapt)
        self.app._mark_table_dirty("Coach")
        values = list(self.tree.item(str(self.index), "values"))
        values[1] = t.name(self.index)
        values[2] = pesdb.country_name(t.country(self.index)) or t.country(self.index)
        values[4] = t.adaptability(self.index)
        self.tree.item(str(self.index), values=values)
        self.note.configure(text=f"coach {t.cid(self.index)} saved — Save to game writes Coach.bin")


class AppearanceWindow(tk.Toplevel):
    """The selected player's appearance record: PlayerAppearance.bin, one 60-byte row each."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.index = app.index
        self.title(f"Appearance — {app.table.name(self.index)}")
        self.configure(bg=C_BG)
        ui = app.ui
        self.geometry(f"{int(600 * ui)}x{int(760 * ui)}")
        self.transient(app)

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        self.tree = ttk.Treeview(body, columns=("value",), selectmode="browse")
        self.tree.heading("#0", text="Field")
        self.tree.heading("value", text="Value")
        self.tree.column("#0", width=int(300 * ui))
        self.tree.column("value", width=int(70 * ui), anchor="e")
        self.tree.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        bar.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.bind("<<TreeviewSelect>>", self.pick)

        form = tk.Frame(self, bg=C_BG)
        form.pack(fill="x", padx=12)
        tk.Label(form, text="Value", bg=C_BG, fg=C_DIM).pack(side="left")
        self.value = tk.StringVar()
        self.spin = tk.Spinbox(form, textvariable=self.value, width=8, command=self.apply)
        self.spin.pack(side="left", padx=8)
        self.spin.bind("<Return>", lambda _e: self.apply())
        ttk.Button(form, text="Apply to player", style="Save.TButton",
                   command=self.apply).pack(side="left")
        self.note = tk.Label(self, text="", bg=C_BG, fg=C_ACCENT, anchor="w",
                             font=(app.FONT, 9))
        self.note.pack(fill="x", padx=12, pady=(6, 12))
        self.fill()

    def fill(self):
        t = self.app.appearance_table
        for group, labels in pesdb.APPEARANCE_GROUPS:
            gid = self.tree.insert("", "end", text=group, values=("",), open=False)
            for label in labels:
                self.tree.insert(gid, "end", iid=label, text=label,
                                 values=(t.get(self.index, label),))

    def pick(self, _e=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in pesdb.APPEARANCE_BY_LABEL:
            return
        _bit, width, add = pesdb.APPEARANCE_BY_LABEL[sel[0]]
        self.spin.configure(from_=add, to=add + (1 << width) - 1)
        self.value.set(str(self.app.appearance_table.get(self.index, sel[0])))

    def apply(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in pesdb.APPEARANCE_BY_LABEL:
            self.note.configure(text="pick a field in the list first")
            return
        label = sel[0]
        _bit, width, add = pesdb.APPEARANCE_BY_LABEL[label]
        try:
            value = max(add, min(add + (1 << width) - 1, int(self.value.get())))
        except ValueError:
            self.note.configure(text="a whole number, please")
            return
        self.app.appearance_table.set(self.index, label, value)
        self.app._mark_table_dirty("PlayerAppearance")
        self.tree.item(label, values=(value,))
        self.note.configure(text=f"{label} = {value} — Save to game writes PlayerAppearance.bin")


def check():
    """Print what this machine has, so a broken install can be diagnosed without a GUI."""
    import platform
    print("editor      :", settings.VERSION)
    print("python      :", sys.version.split()[0], platform.machine())
    print("platform    :", platform.system(), platform.release())
    print("session     :", "wayland" if os.environ.get("WAYLAND_DISPLAY") else
          ("x11" if os.environ.get("DISPLAY") else "none (Tk needs a display)"))
    try:
        import tkinter
        print("tkinter     :", "ok (Tk %s)" % tkinter.TkVersion)
    except Exception as exc:                    # noqa: BLE001 - any import/runtime failure
        print("tkinter     : MISSING —", exc)
        print("              install python3-tk (Debian/Ubuntu), python3-tkinter (Fedora),")
        print("              tk (Arch), python3-tk (openSUSE)")
    cfg = settings.load()
    folder = settings.game_dir(cfg)
    print("config      :", settings.CONFIG_PATH)
    print("game folder :", folder or "(not found)")
    if folder and settings.has_database(folder):
        cpk = settings.database_path(folder)
        print("database    :", cpk, "%.1f MB" % (os.path.getsize(cpk) / 1e6))
    else:
        print("database    : NOT FOUND — set the game folder in Settings")
    print("countries   :", f"{len(pesdb.COUNTRIES)} names" if pesdb.COUNTRIES
          else "MISSING data/countries.json — nationality will be blank")
    saves = settings.edit_save_files()
    print("edit saves  :", ", ".join(saves) if saves else "none (database edits are live)")
    return 0 if folder and settings.has_database(folder) else 1


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check())
    if len(sys.argv) > 1:                      # headless mode: no window, no game
        import cli
        raise SystemExit(cli.main(sys.argv[1:]))
    app = Editor()
    app.mainloop()

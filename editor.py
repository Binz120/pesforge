#!/usr/bin/env python3
"""SP Football Life 2027 — player editor.

Reads the game's own player database out of download/data_s*.cpk, lets you change
attributes / positions / skills / identity, and patches the changed tables back into that CPK
(after backing it up) so the game picks them up on its next launch.
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
import theme

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

# ---- palette ---------------------------------------------------------------
# Every colour the window draws comes from theme.py; a palette is ten base colours and the
# rest is derived there (contrast included). These names are the only colour references the
# UI code uses — the palette switch in Settings picks which set they resolve to at launch.
THEME = theme.choice(settings.load())
_PAL = theme.tokens(THEME)
C_BG = _PAL["bg"]
C_PANEL = _PAL["panel"]
C_PANEL2 = _PAL["panel2"]
C_LINE = _PAL["line"]            # grid lines, separators
C_HOVER = _PAL["hover"]          # hover/active background
C_SEL = _PAL["sel"]              # selected row / listbox selection
C_INK = _PAL["ink"]              # text on the accent colour
C_FG = _PAL["fg"]
C_DIM = _PAL["dim"]              # secondary text
C_HINT = _PAL["hint"]            # quietest text still legible (shortcut hints)
C_ACCENT = _PAL["accent"]
C_ACCENT_HI = _PAL["accent_hi"]  # hover of accent-coloured buttons
C_ACCENT_LO = _PAL["accent_lo"]  # mid ability bar fill
C_GREEN = _PAL["green"]
C_MAGENTA = _PAL["magenta"]
C_WARN = _PAL["warn"]
C_DIRTY = _PAL["dirty"]          # field background while an edit is pending
C_BAR = _PAL["bar"]              # ability bar track
C_BAR_LOW = _PAL["bar_low"]      # ability fill under 65 — still 3:1 on the panel
C_GHOST = _PAL["ghost"]          # the tick showing the value before the edit


class Pill(tk.Canvas):
    """A chip drawn as a rounded "island" — Tk cannot round a widget's corners.

    A canvas rather than a Label: the text is one item and the background is a smoothed
    polygon whose corner radius is half the height, which is the pill shape the mockup
    uses for every chip family (`.chip{border-radius:999px}`).
    """

    def __init__(self, parent, text, font, click=None, click_back=None):
        # highlightthickness gives the chip a real focus ring when it is reached by Tab: the
        # chips are the position and skill editors, so they must be operable without a mouse.
        super().__init__(parent, bg=C_BG, bd=0, highlightthickness=1,
                         highlightbackground=C_BG, highlightcolor=C_ACCENT,
                         takefocus=1 if click is not None else 0, cursor="hand2")
        self._text = text
        self._font = font
        self._click = click
        self._click_back = click_back
        self._hover = False
        self._fill, self._fg = C_PANEL2, C_DIM
        self._line = theme.mix(C_LINE, C_FG, 0.22)   # a border, not a suggestion
        self._redraw()
        if click is not None:
            self.bind("<Button-1>", self._fire)
            self.bind("<Return>", self._fire)
            self.bind("<space>", self._fire)
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)
            if click_back is not None:
                self.bind("<Button-3>", self._fire_back)

    def _enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _leave(self, _event=None):
        self._hover = False
        self._redraw()

    def _fire(self, _event=None):
        if self._click is not None:
            self._click()

    def _fire_back(self, _event=None):
        """Right-click: the same chip the other way round (natural -> can play -> none)."""
        if self._click_back is not None:
            self._click_back()

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
        """Draw the pill as an exact capsule: two half-discs, a bar, and a stroke on the edge.

        This used to be one smoothed polygon. Tk's smoothing is a B-spline that cuts the
        corners of its control points, so the rounded ends came out flattened and the fill
        never reached the outline — the chips read as thin empty outlines instead of solid
        bubbles. Two filled discs plus a rectangle give a fill that touches the border, and
        the border is drawn separately as two arcs and two lines so it stays 1 px everywhere.
        """
        w = self._font.measure(self._text) + 12
        h = self._font.metrics("linespace") + 6
        fill = theme.mix(self._fill, C_FG, 0.07) if self._hover else self._fill
        r = h / 2
        self.configure(width=w, height=h)
        self.delete("all")
        left = (0.5, 0.5, h - 0.5, h - 0.5)
        right = (w - h + 0.5, 0.5, w - 0.5, h - 0.5)
        # fill: the two caps and the bar between them, no outline of their own
        self.create_oval(*left, fill=fill, outline="")
        self.create_oval(*right, fill=fill, outline="")
        self.create_rectangle(r - 1, 0, w - r + 1, h, fill=fill, outline="")
        # border: left half-circle, right half-circle, top and bottom edges
        self.create_arc(*left, start=90, extent=180, style="arc", outline=self._line, width=1)
        self.create_arc(*right, start=270, extent=180, style="arc", outline=self._line, width=1)
        mid = h / 2
        self.create_line(mid, 0.5, w - mid, 0.5, fill=self._line, width=1)
        self.create_line(mid, h - 0.5, w - mid, h - 0.5, fill=self._line, width=1)
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=self._fg, font=self._font)


class CheckPill(Pill):
    """A checkbox drawn as a chip: filled with a check when on, an outline when off.

    ttk's clam indicator is a dark square on a dark background — in the reference captures the
    two filters read as empty boxes whether they were ticked or not. The chip grammar the rest
    of the window already uses (filled = on) carries the state in shape as well as colour.
    """

    def __init__(self, parent, text, font, variable, command=None):
        self._variable = variable
        self._command = command
        self._base = text
        super().__init__(parent, text=text, font=font, click=self._toggle)
        variable.trace_add("write", lambda *a: self._paint())
        self._paint()

    def _toggle(self):
        self._variable.set(0 if self._variable.get() else 1)
        if self._command is not None:
            self._command()

    def _paint(self):
        on = bool(self._variable.get())
        self.set_text(("\u2713 " if on else "") + self._base)
        if on:
            self.set_state(fill=C_ACCENT, fg=C_INK, line=C_ACCENT)
        else:
            self.set_state(fill=C_PANEL2, fg=C_DIM, line=C_LINE)


class Section(tk.Frame):
    """A collapsible form section: chevron, title, optional hint, change count, hairline rule.

    Tk's LabelFrame cannot make its own border label clickable, and its label was also what
    clipped a long section title inside a narrow pane. Drawing the header as a row fixes both:
    the whole title line folds the body, and the parenthetical explanations live beside the
    title in quiet type instead of inside it.
    """

    def __init__(self, parent, title, hint=None, collapsed=False, fonts=None, on_toggle=None):
        super().__init__(parent, bg=C_BG)
        self.section_key = None
        self.folded = bool(collapsed)
        self.on_toggle = on_toggle
        sec_font, tiny = fonts or (None, None)
        head = tk.Frame(self, bg=C_BG, cursor="hand2")
        head.pack(fill="x")
        self.chevron = tk.Label(head, text="\u25be", bg=C_BG, fg=C_ACCENT,
                                font=sec_font, width=1)
        self.chevron.pack(side="left")
        self.title = tk.Label(head, text=" " + title.upper() + " ", bg=C_BG, fg=C_ACCENT,
                              font=sec_font)
        self.title.pack(side="left")
        self.hint = tk.Label(head, text=hint or "", bg=C_BG, fg=C_HINT, font=tiny)
        self.hint.pack(side="left", padx=(2, 6))
        self.count_label = tk.Label(head, text="", bg=C_BG, fg=C_MAGENTA, font=tiny)
        self.count_label.pack(side="right", padx=(6, 0))
        self.rule = tk.Frame(head, bg=C_LINE, height=1)
        self.rule.pack(side="left", fill="x", expand=True, padx=(8, 6))
        self.body = tk.Frame(self, bg=C_BG)
        if not self.folded:
            self.body.pack(fill="x")
        for w in (head, self.chevron, self.title, self.hint):
            w.bind("<Button-1>", lambda _e: self.toggle())
        self.chevron.configure(text="\u25b8" if self.folded else "\u25be")

    def toggle(self):
        self.set_folded(not self.folded)
        if self.on_toggle is not None:
            self.on_toggle(self)

    def set_folded(self, folded):
        self.folded = bool(folded)
        if self.folded:
            self.body.pack_forget()
            self.chevron.configure(text="\u25b8")
        else:
            self.body.pack(fill="x")
            self.chevron.configure(text="\u25be")


class SlimScroll(tk.Canvas):
    """A 6px scroll indicator that overlays its list instead of taking a column.

    The list used to have no scrollbar at all ("the wheel and PageDown are enough"), which is
    true until you want to drag to the top of 800 rows. A ttk.Scrollbar costs 15px of width
    that the form panes cannot spare, so this draws a thumb over the tree's right edge and
    disappears when the list fits.
    """

    def __init__(self, parent, command, width=7):
        super().__init__(parent, width=width, bg=C_BG, highlightthickness=0, bd=0,
                         cursor="sb_v_double_arrow")
        self._command = command
        self._first, self._last = 0.0, 1.0
        self._drag = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        need = not (self._first <= 0.0 and self._last >= 1.0)
        if need and not self.winfo_manager():
            self.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        elif not need and self.winfo_manager():
            self.place_forget()
        if need:
            self._draw()

    def _draw(self):
        self.delete("all")
        h = self.winfo_height()
        if not h:
            return
        top = int(self._first * h)
        bottom = max(top + 18, int(self._last * h))
        w = int(self.cget("width"))
        self.create_rectangle(w - 5, top + 1, w - 1, bottom - 1,
                              fill=C_HINT, width=0)

    def _frac(self, y):
        h = max(1, self.winfo_height())
        span = max(0.02, self._last - self._first)
        return max(0.0, min(1.0, y / h - span / 2))

    def _press(self, evt):
        self._drag = self._frac(evt.y)
        self._command("moveto", self._drag)

    def _motion(self, evt):
        if self._drag is None:
            return
        self._command("moveto", self._frac(evt.y))


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
        self.cfg = settings.load()
        win_w = min(int(1460 * self.ui), self.winfo_screenwidth())
        win_h = min(int(920 * self.ui), self.winfo_screenheight())
        # the size the window was left at last time, within what this screen allows
        try:
            last_w, last_h = (int(v) for v in str(self.cfg.get("window") or "").split("x"))
            win_w = max(min(int(1180 * self.ui), win_w), min(last_w, self.winfo_screenwidth()))
            win_h = max(min(int(760 * self.ui), win_h), min(last_h, self.winfo_screenheight()))
        except ValueError:
            pass
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(min(int(1180 * self.ui), win_w), min(int(760 * self.ui), win_h))

        self.FONT = _pick_font()
        self._chip_font = tkfont.Font(root=self, family=self.FONT, size=8)   # pill chips measure
        self._label_font = tkfont.Font(root=self, family=self.FONT, size=9)  # filter chips
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
        self.view_mode = tk.StringVar(value="players")   # the list's Players/Clubs tab
        self.reindex()
        self.dirty = False
        self.vars = {}
        self._undo = []           # [(undo_callable, redo_callable), ...]
        self._redo = []
        self._edited_pids = set()  # players touched this session, for the "changed only" view
        self._changes = {}         # (table, record id, field) -> pending edit, the change ledger
        self._field_widgets = {}   # (table, record id, field) -> the widget that shows it
        self._field_section = {}   # field name -> section key, for the per-section counts
        self._section_counts = {}  # section key -> the label that carries its changed count
        self._ovr_cache = {}       # record index -> OVR, invalidated when an ability changes
        self._build_style()
        self._build_ui()
        self._apply_cursors()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Map>", self._on_root_map, add="+")
        self.populate("")
        where = " · database found automatically" if self.auto_found else ""
        shadow = " · in-game EDIT save overrides the database on load" if self.edit_saves else ""
        self.status(f"{len(self.table.records):,} players loaded from {self.game_dir}{where}{shadow}")
        self.after(150, self._update_career_note)     # one pass over the table, off the open path
        self._park_if_asked()

    def _career_empties(self):
        """Career fields that hold nothing in any record of the loaded table.

        The shipped data leaves OwnerClub zero for all 26,395 players (the schema puts it at
        byte 4; it is not a decoding bug), and Contract/Loan/MarketValue are blank for all but
        a few dozen. Showing "0 —" made those look broken; the form says which they are instead.
        """
        cached = getattr(self, "_career_empty_cache", None)
        if cached is not None:
            return cached
        recs = self.table.records
        empty = []
        for label, bit, width, _add in pesdb.CAREER:
            mask = ((1 << width) - 1) << bit
            if not any(int.from_bytes(rec, "little") & mask for rec in recs):
                empty.append(label)
        self._career_empty_cache = empty
        return empty

    def _update_career_note(self):
        note = getattr(self, "career_note", None)
        if note is None or not note.winfo_exists():
            return
        empty = self._career_empties()
        if not empty:
            note.configure(text="")
            return
        names = ", ".join(empty)
        text = (f"{names} {'is' if len(empty) == 1 else 'are'} blank in all "
                f"{len(self.table.records):,} records of this database \u2014 the shipped "
                "data, not a read error.")
        if "Owner Club" in empty:
            text += (" The game keeps per-player ownership in its EDIT save, which PESForge "
                     "cannot read yet.")
        text += " Type a club name or id to set one."
        note.configure(text=text)

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
        self.index_by_pid = {self.table.pid(i): i for i in range(len(self.table.records))}
        self._career_empty_cache = None
        self._country_ids = {name: cid for cid, name in pesdb.COUNTRIES.items() if name}
        self._country_names = sorted(set(pesdb.COUNTRIES.values()))
        self._ovr_cache = {}

    # ---------------------------------------------------------------- style
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        # One type scale for the whole window: labels, values, section headings, quiet text.
        # Every widget names one of these instead of carrying its own size, which is what kept
        # the form at 8pt everywhere while the card shouted at 26.
        self.F_LABEL = (self.FONT, 9)
        self.F_VALUE = (self.FONT, 10)
        self.F_SECTION = (self.FONT, 10, "bold")
        self.F_SMALL = (self.FONT, 9)
        self.F_TINY = (self.FONT, 8)
        st.configure(".", background=C_BG, foreground=C_FG, fieldbackground=C_PANEL2,
                     bordercolor=C_PANEL2, lightcolor=C_PANEL2, darkcolor=C_PANEL2,
                     troughcolor=C_PANEL, focuscolor=C_ACCENT)
        st.configure("Treeview", background=C_PANEL, fieldbackground=C_PANEL,
                     foreground=C_FG, rowheight=int(25 * self.ui), borderwidth=0,
                     font=self.F_LABEL)
        st.configure("Treeview.Heading", background=C_PANEL2, foreground=C_DIM,
                     relief="flat", font=(self.FONT, 8, "bold"))
        st.map("Treeview", background=[("selected", C_SEL)],
               foreground=[("selected", C_FG)])
        st.configure("TEntry", fieldbackground=C_PANEL2, foreground=C_FG, insertcolor=C_FG,
                     padding=(3, 2))
        st.configure("TCombobox", fieldbackground=C_PANEL2, background=C_PANEL2, foreground=C_FG,
                     selectbackground=C_PANEL2, selectforeground=C_FG, arrowcolor=C_ACCENT,
                     insertcolor=C_FG, padding=(3, 2))
        st.map("TCombobox",
               fieldbackground=[("readonly", C_PANEL2), ("focus", C_PANEL2)],
               foreground=[("readonly", C_FG)],
               selectbackground=[("readonly", C_PANEL2)],
               selectforeground=[("readonly", C_FG)],
               background=[("active", C_PANEL2)])
        # A pending edit tints the field itself (C_DIRTY) instead of only the footer dot; the
        # Dirty.* siblings are the same widgets with that background.
        st.configure("Dirty.TEntry", fieldbackground=C_DIRTY, foreground=C_FG)
        st.configure("Dirty.TCombobox", fieldbackground=C_DIRTY, foreground=C_FG,
                     selectbackground=C_DIRTY, selectforeground=C_FG, arrowcolor=C_ACCENT)
        st.map("Dirty.TCombobox", fieldbackground=[("readonly", C_DIRTY), ("focus", C_DIRTY)],
               foreground=[("readonly", C_FG)])
        self.option_add("*TCombobox*Listbox.background", C_PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", C_FG)
        self.option_add("*TCombobox*Listbox.selectBackground", C_SEL)
        self.option_add("*TCombobox*Listbox.selectForeground", C_FG)
        self.option_add("*Spinbox.background", C_PANEL2)
        self.option_add("*Spinbox.foreground", C_FG)
        self.option_add("*Spinbox.insertBackground", C_FG)
        self.option_add("*Spinbox.buttonBackground", C_PANEL2)
        self.option_add("*Spinbox.activeBackground", C_HOVER)
        self.option_add("*Spinbox.highlightThickness", 1)
        self.option_add("*Spinbox.highlightBackground", C_PANEL2)
        self.option_add("*Spinbox.highlightColor", C_ACCENT)
        self.option_add("*Spinbox.relief", "flat")
        self.option_add("*Spinbox.bd", 0)
        st.configure("TSpinbox", fieldbackground=C_PANEL2, foreground=C_FG, arrowcolor=C_ACCENT,
                     padding=(3, 2))
        st.configure("TCheckbutton", background=C_BG, foreground=C_FG, padding=(0, 0))
        st.map("TCheckbutton", background=[("active", C_BG)])
        # ttk button colours have to go through map(): on the clam theme a plain configure()
        # background is not painted, which is how these buttons ended up as bare text
        st.configure("Save.TButton", font=(self.FONT, 10, "bold"), padding=(18, 8), borderwidth=0)
        st.map("Save.TButton", background=[("", C_ACCENT), ("active", C_ACCENT_HI)],
               foreground=[("", C_INK)])
        # -4 chars is the smallest minimum clam allows without clipping a label: the
        # secondary buttons size to their text instead of the theme's 11-char floor
        st.configure("Ghost.TButton", font=(self.FONT, 9), padding=(10, 5),
                     width=-4, borderwidth=0)
        st.map("Ghost.TButton", background=[("", C_PANEL2), ("active", C_HOVER),
                                            ("disabled", C_PANEL2)],
               foreground=[("", C_FG), ("disabled", C_HINT)])

    # ---------------------------------------------------------------- ui
    def _build_ui(self):
        self._build_header()
        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        # The list pane is built once and re-gridded, never rebuilt: relaying it out used to
        # re-scan all 26,395 players and recreate the tree on every fold, which is what made a
        # resize blank the window for most of a second.
        self._left = tk.Frame(body, bg=C_BG)
        self._build_list_pane(self._left)
        self.update_idletasks()
        self._list_w = max(int(236 * self.ui), self._left.winfo_reqwidth())
        self._build_body_one_page(body)

        foot = tk.Frame(self, bg=C_PANEL)
        foot.pack(fill="x", side="bottom")
        self.statusbar = tk.Label(foot, text="", bg=C_PANEL, fg=C_DIM, anchor="w",
                                  font=(self.FONT, 9), padx=12, pady=6)
        self.statusbar.pack(side="left", fill="x", expand=True)
        self._hint_label = tk.Label(foot, text="Ctrl+Z undo · Ctrl+Y redo · Ctrl+S save",
                                    bg=C_PANEL, fg=C_HINT, font=(self.FONT, 8))
        self._hint_label.pack(side="right", padx=(10, 12))
        self.lbl_dirty = tk.Label(foot, text="", bg=C_PANEL, fg=C_MAGENTA,
                                  font=(self.FONT, 10, "bold"))
        self.lbl_dirty.pack(side="right", padx=(10, 0))
        # The change count is the always-visible half of the change ledger: the drawer behind it
        # lists every pending edit, so "what have I touched?" never needs a save first.
        self.lbl_changes = tk.Label(foot, text="", bg=C_PANEL, fg=C_ACCENT,
                                    font=(self.FONT, 9, "bold"), cursor="hand2")
        self.lbl_changes.pack(side="right", padx=(10, 0))
        self.lbl_changes.bind("<Button-1>", lambda e: self.open_changes())

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

    def _on_root_resize(self, evt=None):
        """Only the window's own Configure matters.

        A binding on the root is also on every child's bindtags (a child's bindtags carry its
        toplevel), so without the `evt.widget is self` check this ran once per widget in the
        window — hundreds of times per relayout, each one cancelling and rescheduling a
        repaint, which is what made a resize take most of a second.
        """
        if evt is not None and evt.widget is not self:
            return
        if self._resize_repaint_job is not None:
            self.after_cancel(self._resize_repaint_job)
        self._resize_repaint_job = self.after(120, self._force_repaint)

    def _on_root_map(self, evt=None):
        if evt is not None and evt.widget is not self:
            return
        self.after(80, self._force_repaint)

    def _force_repaint(self):
        """Redraw the window after a resize or a rebuilt layout.

        XWayland under Hyprland sometimes never sends the Expose that follows a resize, so the
        panes stay blank (or keep the previous layout's pixels) until a mouse-over damages them.
        A generated Expose on the window and on the scroll canvases is the only reliable way to
        ask Tk to paint them; doing it after the rebuild matters — new widgets mapped while the
        compositor is settled never get their own event, and that is the stale/overlapping
        screen this used to show when a window was dragged across the fold width.
        """
        self._resize_repaint_job = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        targets = [self]
        for extra in ("_header", "_header_top", "_statusbar"):
            widget = getattr(self, extra, None)
            try:
                if widget is None or not widget.winfo_exists():
                    continue
                targets.append(widget)
                # a container's Expose repaints its own background, not its children: the
                # action buttons live in rows inside the header and were left unpainted, so
                # walk the header and expose every leaf as well
                if extra == "_header":
                    targets.extend(self._walk(widget))
            except tk.TclError:
                continue
        for pane in getattr(self, "_pane_canvases", []):
            try:
                if pane.winfo_exists() and pane.winfo_ismapped():
                    targets.append(pane)
            except tk.TclError:
                continue
        for widget in targets:
            try:
                widget.event_generate("<Expose>", x=0, y=0,
                                      width=max(1, widget.winfo_width()),
                                      height=max(1, widget.winfo_height()))
            except tk.TclError:
                pass

    # ---------------------------------------------------------------- chrome
    def _build_header(self):
        header = tk.Frame(self, bg=C_BG)
        header.pack(fill="x", padx=12, pady=(10, 8))
        self._header = header
        top = tk.Frame(header, bg=C_BG)
        top.pack(fill="x")
        self._header_top = top
        self._title = tk.Label(top, text=settings.APP_NAME, bg=C_BG, fg=C_ACCENT,
                               font=(self.FONT, 16, "bold"))
        # the actions are packed first so they own the header's right edge: pack() shrinks the
        # last-packed child when the header narrows, and that must never be a button
        self._build_actions(top)
        self._title.pack(side="left")

        # search: a dim hint label lies over the entry while it is empty and unfocused; the
        # hint is not entry text, so the search variable itself stays clean. The box expands
        # into the header's slack (the mockup's ~70% wide field) but with a small requested
        # width, because pack has to be able to shrink it before it touches a button: a fixed
        # width plus a spacer pushed the action bar off the right edge of a narrow window.
        self.q = tk.StringVar()
        self._search_box = box = tk.Frame(top, bg=C_PANEL2, highlightthickness=1,
                                          highlightbackground=C_LINE)
        box.pack(side="left", fill="x", expand=True, padx=(16, 12))
        self.search_entry = ent = tk.Entry(box, textvariable=self.q, bg=C_PANEL2, fg=C_FG,
                                           insertbackground=C_FG, relief="flat", bd=0,
                                           # the frame draws the border; an Entry's own
                                           # highlight would draw a second rectangle inside it
                                           highlightthickness=0,
                                           width=int(26 * self.ui))
        ent.pack(side="left", fill="x", expand=True, padx=10, pady=5)
        ent.bind("<Return>", lambda e: self.open_first(self.q.get()))
        ent.bind("<FocusIn>", self._search_focus_in)
        ent.bind("<FocusOut>", self._search_focus_out)
        tk.Label(box, text="Ctrl+F", bg=C_PANEL2, fg=C_HINT,
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
        try:
            self._search_box.configure(highlightbackground=C_ACCENT, highlightcolor=C_ACCENT)
        except (AttributeError, tk.TclError):
            pass

    def _search_focus_out(self, *_):
        self._search_focused = False
        try:
            self._search_box.configure(highlightbackground=C_LINE)
        except (AttributeError, tk.TclError):
            pass
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
        menu.add_command(label="Save to game", accelerator="Ctrl+S",
                         command=self._later(self.save))
        menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self._later(self.undo))
        menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self._later(self.redo))
        menu.add_command(label="Discard all unsaved changes…",
                         command=self._later(self.discard_changes))
        menu.add_separator()
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
                ("Redo", "Ghost.TButton", self.redo),
                ("Discard", "Ghost.TButton", self.discard_changes),
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

        def layout_bar(evt=None):
            """Rows are re-split to whatever fits, so no button is ever clipped away.

            The width comes from the event: during a resize `winfo_width()` can still report
            the previous size, which is how the menu button ended up on a fifth row.
            """
            space = evt.width if evt is not None and evt.width > 1 else self._header.winfo_width()
            if space <= 1:
                space = int(700 * self.ui)                 # the header is not mapped yet
            # the bar shares the row with the brand: split for the width the bar gets once the
            # brand has drawn, and never below one button's worth
            brand = self._title.winfo_reqwidth()
            space = max(int(196 * self.ui), space - brand - int(16 * self.ui))
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
            if getattr(self, "statusbar", None) is not None:
                self.after_idle(self._force_repaint)

        self._header.bind("<Configure>", layout_bar, add="+")
        layout_bar()
        return bar

    # ---------------------------------------------------------------- bodies
    def _build_body_one_page(self, body):
        """Two form columns beside the list; they stack into one when the window is narrow."""
        self._one_page_body = body
        self._one_page_mode = None
        self._one_page_wide = True
        self._one_page_job = None
        body.bind("<Configure>", self._maybe_fold_one_page)
        self._layout_one_page(body, wide=True)
        # the first <Configure> arrives before the window has its real width, so the fold is
        # decided again once the window is mapped; otherwise a wide window could open stacked
        self._fold_job = self.after(150, self._recheck_fold)

    def _recheck_fold(self):
        self._fold_job = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        body = getattr(self, "_one_page_body", None)
        if body is None or not body.winfo_exists():
            return
        width = body.winfo_width()
        if width > 100:
            self._maybe_fold_one_page(type("E", (), {"width": width})())

    def _maybe_fold_one_page(self, evt):
        """Rebuild the body when the window crosses a size the columns still fit at.

        Three shapes: two form columns beside the list (wide), the form stacked beside the
        list (folded), and - below the width a side-by-side pair can share - the list above
        the form with a draggable splitter. A tiling window manager can hand the app a window
        narrower than its own minimum, and clipping would leave neither pane usable.
        """
        width = evt.width
        if width < 200:
            return
        wide = width >= 1120 * self.ui
        compact = width < int(900 * self.ui)
        mode = ("wide" if wide else ("compact" if compact else "folded"),
                *self._detail_columns(width, wide).values())
        if mode == self._one_page_mode:
            return
        if self._one_page_job is not None:
            self.after_cancel(self._one_page_job)
        self._one_page_job = self.after(120, self._finish_fold)

    def _finish_fold(self):
        self._one_page_job = None
        if getattr(self, "_closing", False) or not self.winfo_exists():
            return
        body = self._one_page_body
        try:
            if not body.winfo_exists():
                return
        except tk.TclError:
            return
        width = body.winfo_width()
        if width < 200:                      # still not sized: leave the layout alone
            return
        wide = width >= 1120 * self.ui
        compact = width < int(900 * self.ui)
        self._layout_one_page(body, wide=wide, compact=compact)

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
            "details": fit(mid_w, 3, 262),
            "contract": fit(mid_w, 3, 250),
            "positions": fit(mid_w, 5, 128),
            "abilities": fit(mid_w, 3, 150),
            "skills": fit(right_w, 4, 118),
        }

    def _layout_one_page(self, body, wide, width=None, height=None, compact=False):
        """Build (or rebuild) the body. `width`/`height` plan a layout for a size the window
        may not currently have — the tests use that to check the design size on a desktop
        whose window manager tiles the window to its own size."""
        self._one_page_wide = wide
        for child in body.winfo_children():
            if child is not self._left:        # the list survives a relayout
                child.destroy()
        for c in range(3):                     # the wide layout's weights survived the fold
            body.columnconfigure(c, weight=0, minsize=0)
        for r in range(3):                     # and compact's row weights survive a fold back
            body.rowconfigure(r, weight=0, minsize=0)
        left = self._left
        # The list keeps the width its own columns ask for; the form panes get what is left.
        # The filters are laid out first: compact mode puts the three rows side by side, which
        # makes the list request the whole window, and measuring before that would hand the
        # form panes a tiny share of a wide window.
        self._layout_list_filters(compact=compact)
        self.update_idletasks()
        self._list_w = max(int(236 * self.ui), left.winfo_reqwidth())
        body_w = width or body.winfo_width()
        body_h = height or body.winfo_height()
        cols = self._detail_columns(body_w, wide)
        self._plan = {"width": body_w, "height": body_h, "wide": wide, "compact": compact}
        if compact:
            # one column: the list on top, a splitter, the form below. The filter rows go
            # side by side so they do not eat the small pane.
            body.columnconfigure(0, weight=1)
            body.rowconfigure(2, weight=1)
            split = getattr(self, "_compact_list_h", 0) or int(body_h * 0.42)
            split = max(int(150 * self.ui), min(split, max(int(200 * self.ui),
                                                          body_h - int(200 * self.ui))))
            self._compact_list_h = split
            body.rowconfigure(0, minsize=split, weight=0)
            body.grid_rowconfigure(1, minsize=int(7 * self.ui), weight=0)
            left.grid(row=0, column=0, sticky="nsew")
            sash = tk.Frame(body, bg=C_PANEL, height=int(7 * self.ui),
                            cursor="sb_v_double_arrow")
            sash.grid(row=1, column=0, sticky="ew")
            tk.Frame(sash, bg=C_ACCENT, height=2).place(relx=0.5, rely=0.5, relwidth=0.12,
                                                        anchor="center")
            sash.bind("<Button-1>", self._sash_press)
            sash.bind("<B1-Motion>", self._sash_drag)
            self._sash = sash
            detail = tk.Frame(body, bg=C_BG)
            detail.grid(row=2, column=0, sticky="nsew")
            col = self._scroll_pane(detail)
            self._build_player_card(col)
            self._build_attributes(col, cols=cols["abilities"])
            self._build_position_grid(col, cols=cols["positions"])
            self._build_details(col, cols=cols["details"])
            self._build_career(col, cols=cols["contract"])
            self._build_skills(col, cols=cols["skills"])
            self._build_club(col)
        elif wide:
            # the right column (radar, 48 skill chips, club) and the middle one (card, 25
            # ability rows, positions, details) are both measured; 45/55 with 8px gutters is
            # the split the design size fits both at
            avail = max(360, body_w - self._list_w - 16)
            right_w = max(int(300 * self.ui), int(avail * 0.45))
            mid_w = max(int(300 * self.ui), avail - right_w - 8)
            self._plan.update(mid=mid_w, right=right_w)
            body.columnconfigure(0, minsize=self._list_w)
            body.columnconfigure(1, minsize=mid_w, weight=55)
            body.columnconfigure(2, minsize=right_w, weight=45)
            body.rowconfigure(0, weight=1)
            left.grid(row=0, column=0, sticky="nsew")
            mid = tk.Frame(body, bg=C_BG)
            mid.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
            right = tk.Frame(body, bg=C_BG)
            right.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
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
            detail.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
            col = self._scroll_pane(detail)
            self._build_player_card(col)
            self._build_attributes(col, cols=cols["abilities"])
            self._build_position_grid(col, cols["positions"])
            self._build_details(col, cols["details"])
            self._build_career(col, cols["contract"])
            self._build_skills(col, cols=cols["skills"])
            self._build_club(col)
        self._one_page_mode = ("wide" if wide else ("compact" if compact else "folded"),
                               *self._detail_columns(body_w, wide).values())
        if getattr(self, "statusbar", None) is not None:    # a relayout, not the first build
            if self.index is not None:
                self.load_form()
            # the new widgets were mapped after the resize settled: without this the compositor
            # can keep showing the old layout's pixels over them
            self.after_idle(self._force_repaint)
            self.after(80, self._force_repaint)

    def _layout_list_filters(self, compact):
        """Stack the three filter rows, or put them side by side in a short pane."""
        rows = getattr(self, "_filter_rows", [])
        if not rows or getattr(self, "_filter_box", None) is None:
            return
        for row, label, combo in rows:
            row.pack_forget()
            label.pack_forget()
            combo.pack_forget()
        for row, label, combo in rows:
            if compact:
                # the labels stay: three unlabelled dropdowns side by side say nothing about
                # which one filters clubs and which one filters countries
                row.pack(side="left", fill="x", expand=True, padx=(0, 6))
                label.configure(width=0)          # natural width, not the 7-char gutter
                label.pack(side="left", padx=(0, 4))
                combo.pack(side="left", fill="x", expand=True)
            else:
                row.pack(fill="x", pady=(0, 8))
                label.configure(width=7)
                label.pack(side="left")
                combo.pack(side="left", fill="x", expand=True, padx=8)

    def _sash_press(self, evt):
        self._sash_origin = (evt.y_root, getattr(self, "_compact_list_h", 0))

    def _sash_drag(self, evt):
        """Drag the splitter: the list keeps a usable height, the form keeps the rest."""
        origin = getattr(self, "_sash_origin", None)
        if origin is None or not getattr(self, "_one_page_body", None):
            return
        body = self._one_page_body
        try:
            if not body.winfo_exists():
                return
        except tk.TclError:
            return
        y0, height0 = origin
        limit = max(int(140 * self.ui), body.winfo_height() - int(180 * self.ui))
        self._compact_list_h = max(int(120 * self.ui),
                                   min(limit, height0 + (evt.y_root - y0)))
        body.rowconfigure(0, minsize=self._compact_list_h)

    # ---------------------------------------------------------------- left pane
    def _build_list_pane(self, left):
        self._filter_box = tk.Frame(left, bg=C_BG)
        self._filter_box.pack(fill="x")
        self._filter_rows = []          # (row frame, label, combobox) for the compact layout
        filt = tk.Frame(self._filter_box, bg=C_BG)
        filt.pack(fill="x", pady=(0, 8))
        club_label = tk.Label(filt, text="Club", bg=C_BG, fg=C_DIM, width=7, anchor="w")
        club_label.pack(side="left")
        self.club_filter = tk.StringVar(value="")   # empty = every club
        self._club_names = sorted({n for t, n in self.clubs.items() if n and t >= 100})
        self.cb_club = ttk.Combobox(filt, textvariable=self.club_filter, width=30,
                                    values=self._club_names)
        self.cb_club.pack(side="left", fill="x", expand=True, padx=8)
        self._filter_rows.append((filt, club_label, self.cb_club))
        self.cb_club.bind("<<ComboboxSelected>>", lambda e: self.apply_club_filter())
        self.cb_club.bind("<Return>", lambda e: self.apply_club_filter())
        self.cb_club.bind("<KeyRelease>", self._filter_club_values)
        self._club_after = None

        filt_nat = tk.Frame(self._filter_box, bg=C_BG)
        filt_nat.pack(fill="x", pady=(0, 8))
        nat_label = tk.Label(filt_nat, text="Country", bg=C_BG, fg=C_DIM, width=7,
                             anchor="w")
        nat_label.pack(side="left")
        self.country_filter = tk.StringVar(value="")      # empty = every nationality
        self._country_names = sorted(set(pesdb.COUNTRIES.values()))
        self.cb_country = ttk.Combobox(filt_nat, textvariable=self.country_filter, width=30,
                                       values=self._country_names)
        self.cb_country.pack(side="left", fill="x", expand=True, padx=8)
        self.cb_country.bind("<<ComboboxSelected>>", lambda e: self.apply_country_filter())
        self.cb_country.bind("<Return>", lambda e: self.apply_country_filter())
        self.cb_country.bind("<KeyRelease>", self._filter_country_values)
        self._country_after = None
        self._filter_rows.append((filt_nat, nat_label, self.cb_country))

        filt_pos = tk.Frame(self._filter_box, bg=C_BG)
        filt_pos.pack(fill="x", pady=(0, 8))
        pos_label = tk.Label(filt_pos, text="Position", bg=C_BG, fg=C_DIM, width=7,
                             anchor="w")
        pos_label.pack(side="left")
        self.position_filter = tk.StringVar(value="")      # empty = every position
        self.cb_pos = ttk.Combobox(filt_pos, textvariable=self.position_filter, width=30,
                                   values=[""] + pesdb.POS_NAMES, state="readonly")
        self.cb_pos.pack(side="left", fill="x", expand=True, padx=8)
        self.cb_pos.bind("<<ComboboxSelected>>", lambda e: self.populate(self.q.get()))
        self._filter_rows.append((filt_pos, pos_label, self.cb_pos))

        self.changed_only = tk.BooleanVar(value=False)
        self.pill_changed = CheckPill(left, "Changed only", self._label_font, self.changed_only,
                                      command=lambda: self.populate(self.q.get()))
        self.pill_changed.pack(anchor="w", pady=(0, 3))

        self.free_agents_only = tk.BooleanVar(value=False)
        self.pill_free = CheckPill(left, "Free agents only", self._label_font, self.free_agents_only,
                                   command=lambda: self.populate(self.q.get()))
        self.pill_free.pack(anchor="w", pady=(0, 3))

        # The two result kinds are tabs rather than a hidden mode switch: a club search keeps
        # its players on the Players tab and its clubs on the Clubs tab, with counts.
        tabs = tk.Frame(left, bg=C_BG)
        tabs.pack(fill="x", pady=(2, 4))
        self.tab_players = tk.Label(tabs, text="Players", bg=C_PANEL2, fg=C_DIM, padx=8, pady=3,
                                    cursor="hand2", font=self.F_LABEL)
        self.tab_players.pack(side="left", padx=(0, 4))
        self.tab_players.bind("<Button-1>", lambda e: self.set_view_mode("players"))
        self.tab_clubs = tk.Label(tabs, text="Clubs", bg=C_PANEL2, fg=C_DIM, padx=8, pady=3,
                                  cursor="hand2", font=self.F_LABEL)
        self.tab_clubs.pack(side="left")
        self.tab_clubs.bind("<Button-1>", lambda e: self.set_view_mode("clubs"))

        cols = ("id", "name", "club", "pos", "ovr", "age")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c, t, w, anchor in (("id", "ID", 40, "e"), ("name", "Name", 84, "w"),
                                ("club", "Club", 70, "w"), ("pos", "Pos", 30, "center"),
                                ("ovr", "OVR", 30, "center"), ("age", "Age", 26, "center")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=int(w * self.ui), anchor=anchor, stretch=(c == "name"))
        # no scrollbar: the wheel, arrows and PageUp/PageDown move the list, and the status
        # line already says how many rows there are
        self.tree.pack(side="left", fill="both", expand=True)
        # the indicator overlays the tree's right edge, so a list that fits costs no width
        self.list_scroll = SlimScroll(left, self.tree.yview)
        self.tree.configure(yscrollcommand=self.list_scroll.set)
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
                elif isinstance(w, tk.Canvas) and str(w.cget("cursor")).startswith("sb_"):
                    continue                      # the ability bars and the list thumb are drag handles
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
        self._pane_canvases = [c for c in getattr(self, "_pane_canvases", [])
                               if c.winfo_exists()] + [canvas]
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

    def _section(self, parent, text, key=None, hint=None):
        """A collapsible form section; returns the body frame callers pack their grid into.

        The label used to be the LabelFrame's border text — wide parenthetical titles clipped in
        a narrow pane and nothing could be folded away, so a 130-field form was one scroll. The
        title now carries a chevron, the hint moved beside it in quiet type, and the body (the
        grid) can fold. `key` names the section for the folded-state memory and the change count.
        """
        collapsed = key is not None and key in (self.cfg.get("collapsed") or [])
        sec = Section(parent, text, hint=hint, collapsed=collapsed,
                      fonts=(self.F_SECTION, self.F_TINY),
                      on_toggle=lambda s: self._remember_section(key, s))
        sec.pack(fill="x", pady=(0, 3))
        sec.section_key = key
        if key is not None:
            self._section_counts[key] = sec.count_label
        return sec.body

    def _remember_section(self, key, sec):
        """Persist which sections are folded, so a reopened window looks like it was left."""
        if key is None:
            return
        folded = [k for k in (self.cfg.get("collapsed") or []) if k != key]
        if sec.folded:
            folded.append(key)
        self.cfg["collapsed"] = folded
        try:
            settings.save(self.cfg)
        except OSError:
            pass

    # ---- identity
    def _build_player_card(self, parent):
        """The player card: photo, name, meta line, value chips, OVR and the two name slots."""
        head = self._section(parent, "Player", key="player")
        top = tk.Frame(head, bg=C_BG)
        top.pack(fill="x", pady=(0, 2))
        side = int(PHOTO_SIDE * self.ui)
        self._photo_slot = slot = tk.Frame(top, width=side, height=side, bg=C_PANEL2)
        slot.pack_propagate(False)
        self.photo = tk.Label(slot, bg=C_PANEL2, bd=0, text="no photo", fg=C_DIM,
                              font=self.F_TINY)
        self.photo.pack(fill="both", expand=True)
        self.photo_img = None
        self._photo_side = side
        self._sync_photo_slot()
        # a 128px photo cannot shrink, so on a pane too narrow to hold it the card drops the
        # photo rather than squashing the face into a 116px slot
        top.bind("<Configure>", lambda e: self._fit_photo_slot(e.width), add="+")

        ovr_box = tk.Frame(top, bg=C_BG)
        ovr_box.pack(side="right", padx=(10, 0))
        self.ovr_big = tk.Label(ovr_box, text="\u2014", bg=C_BG, fg=C_GREEN,
                                font=(self.FONT, 30, "bold"))
        self.ovr_big.pack()
        tk.Label(ovr_box, text="OVR", bg=C_BG, fg=C_DIM, font=self.F_TINY).pack()

        col = tk.Frame(top, bg=C_BG)
        col.pack(side="left", fill="both", expand=True)
        self.lbl_hero = tk.Label(col, text="Select a player", bg=C_BG, fg=C_FG,
                                 font=(self.FONT, 16, "bold"), anchor="w")
        self.lbl_hero.pack(fill="x", pady=(0, 2))
        self.lbl_sub = tk.Label(col, text="Type a name or ID above, then press Enter",
                                bg=C_BG, fg=C_DIM, anchor="w", justify="left",
                                font=self.F_SMALL)
        self._wrap(self.lbl_sub)
        self.lbl_sub.pack(fill="x", pady=(0, 4))

        chips = tk.Frame(col, bg=C_BG)
        chips.pack(fill="x", pady=(0, 5))
        self._card_chips = {}
        for name in ("Style", "Height", "Weight", "Age", "Form"):
            chip = Pill(chips, "\u2014", self._chip_font)
            chip.pack(side="left", padx=(0, 6))
            self._card_chips[name] = chip

        grid = tk.Frame(col, bg=C_BG)
        grid.pack(fill="x")
        self._name_counters = {}
        for i, (label, key) in enumerate((("Full name", "name"), ("Shirt name", "shirt"))):
            tk.Label(grid, text=label, bg=C_BG, fg=C_DIM, anchor="w",
                     font=self.F_LABEL).grid(row=i, column=0, sticky="w", pady=1)
            v = tk.StringVar()
            self.vars[key] = v
            e = ttk.Entry(grid, textvariable=v, width=34, font=self.F_VALUE)
            e.grid(row=i, column=1, sticky="we", padx=6, pady=1)
            e.bind("<FocusOut>", lambda ev, k=key: self.apply_name(k))
            counter = tk.Label(grid, text="", bg=C_BG, fg=C_HINT, font=self.F_TINY, width=6,
                               anchor="e")
            counter.grid(row=i, column=2, sticky="e")
            v.trace_add("write", lambda *a, k=key: self._update_name_counter(k))
            self._name_counters[key] = counter
            self._field_widgets["Full name" if key == "name" else "Shirt name"] = e
            self._field_section["Full name" if key == "name" else "Shirt name"] = "player"
        grid.columnconfigure(1, weight=1)

    def _name_field(self, key):
        return "Full name" if key == "name" else "Shirt name"

    def _update_name_counter(self, key):
        """Names are capped at 60 bytes in the record; say so instead of silently truncating."""
        lab = getattr(self, "_name_counters", {}).get(key)
        if lab is None:
            return
        text = self.vars[key].get()
        size = len(text.encode("utf-8"))
        lab.configure(text=f"{size}/60", fg=C_WARN if size > 60 else C_HINT)

    def _sync_photo_slot(self):
        """A 128px empty box is not worth the space when photos are switched off."""
        slot = getattr(self, "_photo_slot", None)
        if slot is None:
            return
        show = bool(self.cfg.get("photos", True))
        self._fit_photo_slot(slot.master.winfo_width() or int(600 * self.ui), show=show)

    def _fit_photo_slot(self, row_width, show=None):
        """Show the photo only when the card row can hold it uncropped."""
        slot = getattr(self, "_photo_slot", None)
        if slot is None:
            return
        if show is None:
            show = bool(self.cfg.get("photos", True))
        need = int(self._photo_side + 10 + 210 * self.ui)   # photo + gap + name/OVR minimum
        want = show and row_width >= need
        if want == bool(slot.winfo_manager()):
            return
        if want:
            siblings = [w for w in slot.master.winfo_children() if w is not slot]
            if siblings:
                slot.pack(side="left", padx=(0, 10), before=siblings[0])
            else:
                slot.pack(side="left", padx=(0, 10))
        else:
            slot.pack_forget()

    def _build_position_grid(self, parent, cols=5):
        """Position ratings as pill chips; a click cycles none -> can play -> natural."""
        sec = self._section(parent, "Position ratings", key="positions",
                            hint="click cycles none \u00b7 can play \u00b7 natural \u00b7 "
                                 "right-click reverses")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(fill="x")
        self._pos_chips = {}
        for n, (label, bit) in enumerate(pesdb.POSITIONS):
            r, c = divmod(n, cols)
            v = tk.StringVar()
            self.vars["Pos " + label] = v
            chip = Pill(grid, label, self._chip_font,
                        lambda l=label: self.cycle_position(l),
                        lambda l=label: self.cycle_position(l, back=True))
            chip.grid(row=r, column=c, sticky="w", padx=(0, 8), pady=1)
            self._pos_chips[label] = chip
            self._field_section["Pos " + label] = "positions"

    def cycle_position(self, label, back=False):
        """Click-cycle a position rating chip; right-click walks the other way."""
        if self.index is None:
            return
        field = "Pos " + label
        step = -1 if back else 1
        val = (["none", "can play", "natural"].index(self.vars[field].get()) + step) % 3
        self.vars[field].set(["none", "can play", "natural"][val])
        self.apply_position(label)
        self._style_position_chip(label)

    def _style_position_chip(self, label):
        chip = getattr(self, "_pos_chips", {}).get(label)
        if chip is None:
            return
        val = ["none", "can play", "natural"].index(self.vars["Pos " + label].get())
        # the glyph carries the state as well as the colour: "CB" none, "CB \u00b7" can play,
        # "CB \u2713" natural — so the three ratings survive a colour-blind palette
        chip.set_text({0: label, 1: label + " \u00b7", 2: label + " \u2713"}[val])
        if val == 2:
            chip.set_state(fill=C_ACCENT, fg=C_INK, line=C_ACCENT)
        elif val == 1:
            chip.set_state(fill=C_PANEL2, fg=C_ACCENT, line=C_ACCENT)
        else:
            chip.set_state(fill=C_PANEL2, fg=C_DIM, line=C_LINE)

    def _build_details(self, parent, cols):
        get = {f[0]: f for f in pesdb.PHYSICAL}
        details = self._section(parent, "Details", key="details")
        self._field_grid(details, [get["Age"], get["Height (cm)"], get["Weight (kg)"],
                                   get["Nationality"], get["Form"], get["Weak Foot Usage"],
                                   get["Weak Foot Accuracy"], get["Injury Resistance"],
                                   get["Stronger Foot"], get["Registered Position"],
                                   get["Playing Style"]], cols, section="details")

    def _build_career(self, parent, cols):
        # folded by default: twenty controls that are touched once a save, not once a player
        rest = self._section(parent, "Contract \u00b7 motion \u00b7 celebration", key="career",
                             hint="dates YYYYMMDD")
        dates = [f for f in pesdb.CAREER if f[0] in ("Contract Until", "Loan Until")]
        others = [f for f in pesdb.CAREER if f[0] not in ("Contract Until", "Loan Until")]
        # the "this one is blank" line sits above the grid: the block is at the bottom of the
        # pane, so a note below it would open off-screen
        self.career_note = tk.Label(rest, text="", bg=C_BG, fg=C_HINT, anchor="w",
                                    justify="left", font=self.F_TINY)
        self.career_note.pack(fill="x", pady=(0, 3))
        self._wrap(self.career_note)
        # a relayout rebuilds this section; the note's text has to be put back on the new label
        self.after_idle(self._update_career_note)
        # one grid, so the date row shares its columns with every row below it
        self._field_grid(rest, dates + others + list(pesdb.MOTIONS), cols, section="career")

    def _country_id(self, name):
        return (getattr(self, "_country_ids", {}) or {}).get(name)

    def _filter_nationality_values(self, evt, combo):
        if evt.keysym in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(combo.get().strip())
        names = self._country_names
        combo["values"] = ([n for n in names if q in norm(n)][:80] if q else names)

    def _field_grid(self, parent, fields, cols, section=None):
        """Label + widget grid for the form sections (Details and the career block).

        Every row is `[label][widget]`; the label column has a fixed width and a uniform
        share of the spare width, so all rows share one pitch and the widgets line up down
        each column. Career fields use one 9-char box size instead of per-field widths.
        """
        F = self.F_LABEL
        career = {f[0] for f in pesdb.CAREER + pesdb.MOTIONS}
        grid = tk.Frame(parent, bg=C_BG)
        grid.pack(fill="x")
        for n, spec in enumerate(fields):
            label = spec[0]
            r, c = divmod(n, cols)
            tk.Label(grid, text=label, bg=C_BG, fg=C_DIM, anchor="w", width=16, font=F).grid(
                row=r, column=c * 2, sticky="w", pady=1, padx=(0, 4))
            key = label
            v = tk.StringVar()
            self.vars[key] = v
            if label == "Stronger Foot":
                w = ttk.Combobox(grid, textvariable=v, state="readonly", width=7,
                                 values=("Right", "Left"), font=self.F_VALUE)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_foot())
            elif label == "Registered Position":
                w = ttk.Combobox(grid, textvariable=v, state="readonly", width=10,
                                 values=pesdb.POS_NAMES, font=self.F_VALUE)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_field("Registered Position"))
            elif label == "Playing Style":
                w = ttk.Combobox(grid, textvariable=v, state="readonly", width=17,
                                 values=PLAY_STYLES, font=self.F_VALUE)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_style())
            elif label == "Nationality":
                # a name, not a 0-511 id: the database already ships every country's name
                w = ttk.Combobox(grid, textvariable=v, width=14, font=self.F_VALUE,
                                 values=self._country_names)
                w.bind("<<ComboboxSelected>>", lambda e: self.apply_field("Nationality"))
                w.bind("<Return>", lambda e: self.apply_field("Nationality"))
                w.bind("<KeyRelease>", lambda e, cb=w: self._filter_nationality_values(e, cb))
            elif label in ("Youth Club", "Owner Club"):
                # a club id: the box shows the club name and takes one by name or id
                w = ttk.Combobox(grid, textvariable=v, width=17, font=self.F_VALUE,
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
                elif label in ("Contract Until", "Loan Until"):
                    lo, hi = 0, 99999999                 # YYYYMMDD
                elif label == "Market Value":
                    lo, hi = 0, (1 << 24) - 1
                elif label == "National Caps":
                    lo, hi = 0, 255
                else:
                    lo, hi = add, add + (1 << width) - 1      # motion slots, celebrations
                w = tk.Spinbox(grid, textvariable=v, from_=lo, to=hi,
                               width=9 if label in career else 5, font=self.F_VALUE,
                               command=lambda k=key: self.apply_field(k))
                w.bind("<FocusOut>", lambda e, k=key: self.apply_field(k))
                w.bind("<Return>", lambda e, k=key: self.apply_field(k))
            w.grid(row=r, column=c * 2 + 1, sticky="w", pady=1, padx=(0, 8))
            self._field_widgets[label] = w
            if section is not None:
                self._field_section[label] = section
        # no column weights: label and widget sit tight together, so the pitch stays even;
        # spare width stays at the right edge instead of opening gaps mid-form

    # ---- attributes
    def _build_attributes(self, parent, with_radar=True, cols=2):
        """Abilities: label, number, and a bar wide enough to aim at.

        The bar used to be a 30x11 canvas — 59 values across 29 pixels, so one pixel was two
        ability points and a mis-click moved a player's speed. It now spans the row, the
        number stays exact, and a ghost tick marks where the player started.
        """
        sec = self._section(parent, "Abilities", key="abilities", hint="40 \u2013 99")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(fill="x")
        for n, (label, bit, width, add) in enumerate(pesdb.ATTRS):
            r, c = divmod(n, cols)
            tk.Label(grid, text=ATTR_SHORT.get(label, label), bg=C_BG, fg=C_DIM,
                     anchor="w", width=11, font=self.F_LABEL).grid(
                row=r, column=c * 3, sticky="w", pady=1)
            v = tk.StringVar()
            self.vars[label] = v
            sp = tk.Spinbox(grid, textvariable=v, from_=40, to=99, width=3, font=self.F_VALUE,
                            command=lambda k=label: self.apply_field(k))
            sp.bind("<FocusOut>", lambda e, k=label: self.apply_field(k))
            sp.bind("<Return>", lambda e, k=label: self.apply_field(k))
            sp.grid(row=r, column=c * 3 + 1, sticky="w", padx=(0, 6))
            bar = tk.Canvas(grid, width=44, height=int(18 * self.ui), bg=C_BAR,
                            highlightthickness=1, highlightbackground=C_LINE,
                            cursor="sb_h_double_arrow")
            bar.grid(row=r, column=c * 3 + 2, sticky="we", padx=(0, 6), pady=1)
            bar.bind("<Button-1>", lambda e, l=label: self.drag_bar(l, e))
            bar.bind("<B1-Motion>", lambda e, l=label: self.drag_bar(l, e))
            bar.bind("<Button-3>", lambda e, l=label: self.reset_field(l))
            self.vars[label + "::bar"] = bar
            self._field_widgets[label] = sp
            self._field_section[label] = "abilities"
        for c in range(cols):
            grid.columnconfigure(c * 3 + 2, weight=1)

        if with_radar:
            self._build_radar_pane(parent)

    def _build_radar_pane(self, parent):
        prof = self._section(parent, "Ability profile", key="profile", hint="group averages")
        self.radar = tk.Canvas(prof, height=int(205 * self.ui), bg=C_BG, highlightthickness=0)
        self.radar.pack(fill="x")
        self.radar.bind("<Configure>", lambda e: self.draw_radar())

    # ---- skills
    def _build_skills(self, parent, cols=2):
        """Skills as pill chips with a filter and a count: 48 names is too many to scan."""
        sec = self._section(parent, "Skills", key="skills", hint="click to toggle")
        row = tk.Frame(sec, bg=C_BG)
        row.pack(fill="x")
        tk.Label(row, text="Filter", bg=C_BG, fg=C_DIM, font=self.F_LABEL).pack(side="left")
        self.skill_filter = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.skill_filter, width=14, font=self.F_LABEL)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", self._filter_skills)
        self._skill_count = tk.Label(row, text="", bg=C_BG, fg=C_HINT, font=self.F_TINY)
        self._skill_count.pack(side="right")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(fill="x", pady=(3, 0))
        self._skill_grid = grid
        self._skill_cols = cols
        self._skill_chips = {}
        for n, (label, bit) in enumerate(pesdb.SKILLS):
            r, c = divmod(n, cols)
            v = tk.IntVar()
            self.vars["Skill " + label] = v
            chip = Pill(grid, label, self._chip_font,
                        lambda l=label: self.toggle_skill(l))
            chip.grid(row=r, column=c, sticky="w", padx=(0, 8), pady=1)
            chip.bind("<Button-3>", lambda e, l=label: self.toggle_skill(l))
            self._skill_chips[label] = chip
            self._field_section["Skill " + label] = "skills"
        self._update_skill_count()

    def _filter_skills(self, evt=None):
        if evt is not None and getattr(evt, "keysym", "") in (
                "Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(self.skill_filter.get().strip())
        for chip in self._skill_chips.values():
            chip.grid_remove()
        shown = 0
        for label, _bit in pesdb.SKILLS:
            if q and q not in norm(label):
                continue
            r, c = divmod(shown, self._skill_cols)
            self._skill_chips[label].grid(row=r, column=c, sticky="w", padx=(0, 4), pady=1)
            shown += 1
        self._shown_skills = shown
        self._update_skill_count()

    def _update_skill_count(self):
        lab = getattr(self, "_skill_count", None)
        if lab is None:
            return
        chosen = sum(1 for label, _b in pesdb.SKILLS if self.vars["Skill " + label].get())
        shown = getattr(self, "_shown_skills", len(pesdb.SKILLS))
        text = f"{chosen} / {len(pesdb.SKILLS)} chosen"
        if shown != len(pesdb.SKILLS):
            text += f" \u00b7 {shown} shown"
        lab.configure(text=text)

    def toggle_skill(self, label, back=None):
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
        # filled means on: a shape difference, not just an accent outline
        if on:
            chip.set_state(fill=C_ACCENT, fg=C_INK, line=C_ACCENT)
        else:
            chip.set_state(fill=C_PANEL2, fg=C_DIM, line=C_LINE)
        self._update_skill_count()

    @staticmethod
    def _ovr_colour(rating):
        """A rating has a tier at a glance: 90+ gold, 80+ green, 70+ plain, below that dim."""
        if rating >= 90:
            return C_ACCENT
        if rating >= 80:
            return C_GREEN
        if rating >= 70:
            return C_FG
        return C_DIM

    def ovr(self, i):
        """Reference rating for one player record (see pesdb.OVR_MODEL), cached per record."""
        cache = self._ovr_cache
        if i in cache:
            return cache[i]
        rating = pesdb.overall(lambda label: self.table.get(i, label),
                               self.table.get(i, "Registered Position"))
        cache[i] = rating
        return rating

    # ---- club / contract
    def _build_club(self, parent):
        """The club block. Everything below the club line edits Team.bin / PlayerAssignment.bin,
        not the player — the badge says so, because the form around it edits a player."""
        F = self.F_LABEL
        sec = self._section(parent, "Current club", key="club", hint="edits the club record")
        self.lbl_club = tk.Label(sec, text="\u2014", bg=C_BG, fg=C_FG, anchor="w",
                                 font=(self.FONT, 11, "bold"))
        self.lbl_club.pack(fill="x")
        self.lbl_club_scope = tk.Label(sec, text="", bg=C_BG, fg=C_HINT, anchor="w",
                                       font=self.F_TINY, justify="left")
        self.lbl_club_scope.pack(fill="x", pady=(0, 3))
        self._wrap(self.lbl_club_scope, 120)

        row = tk.Frame(sec, bg=C_BG)
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text="Move to club", bg=C_BG, fg=C_DIM, width=11, anchor="w",
                 font=F).pack(side="left")
        self.club_target = tk.StringVar()
        self.cb_target = ttk.Combobox(row, textvariable=self.club_target, width=18, font=F,
                                      values=sorted({n for n in self.clubs.values() if n}))
        self.cb_target.pack(side="left", fill="x", expand=True, padx=6)
        self.cb_target.bind("<KeyRelease>", self._filter_target_values)
        self.cb_target.bind("<Return>", lambda e: self.apply_transfer())
        ttk.Button(row, text="Transfer", style="Ghost.TButton",
                   command=self.apply_transfer).pack(side="left")

        link = tk.Frame(sec, bg=C_BG)
        link.pack(fill="x", pady=(0, 2))
        tk.Label(link, text="Coach", bg=C_BG, fg=C_DIM, width=11, anchor="w",
                 font=F).pack(side="left")
        self.coach_var = tk.StringVar()
        self.cb_coach = ttk.Combobox(link, textvariable=self.coach_var, width=18, font=F,
                                     values=self._coach_choices())
        self.cb_coach.pack(side="left", fill="x", expand=True, padx=6)
        self.cb_coach.bind("<KeyRelease>", self._filter_coach_values)
        self.cb_coach.bind("<Return>", lambda e: self.apply_club_coach())
        ttk.Button(link, text="Set", style="Ghost.TButton",
                   command=self.apply_club_coach).pack(side="left")

        field = tk.Frame(sec, bg=C_BG)
        field.pack(fill="x", pady=(0, 2))
        tk.Label(field, text="Stadium", bg=C_BG, fg=C_DIM, width=11, anchor="w",
                 font=F).pack(side="left")
        self.stadium_var = tk.StringVar()
        self.cb_stadium = ttk.Combobox(field, textvariable=self.stadium_var, width=18, font=F,
                                       values=[f"{sid}  {name}"
                                               for sid, name in sorted(self.stadium_names.items())])
        self.cb_stadium.pack(side="left", fill="x", expand=True, padx=6)
        self.cb_stadium.bind("<KeyRelease>", self._filter_stadium_values)
        self.cb_stadium.bind("<Return>", lambda e: self.apply_stadium())
        ttk.Button(field, text="Set", style="Ghost.TButton",
                   command=self.apply_stadium).pack(side="left")

        number = tk.Frame(sec, bg=C_BG)
        number.pack(fill="x", pady=(0, 2))
        tk.Label(number, text="Squad no.", bg=C_BG, fg=C_DIM, width=11, anchor="w",
                 font=F).pack(side="left")
        self.shirt_var = tk.StringVar()
        self.shirt_spin = tk.Spinbox(number, textvariable=self.shirt_var, from_=1, to=99,
                                      width=4, font=self.F_VALUE,
                                      command=self.apply_shirt_number)
        self.shirt_spin.pack(side="left", padx=6)
        self.shirt_spin.bind("<Return>", lambda e: self.apply_shirt_number())
        self.shirt_spin.bind("<FocusOut>", lambda e: self.apply_shirt_number())
        tk.Label(number, text="worn in this club", bg=C_BG, fg=C_HINT,
                 font=self.F_TINY).pack(side="left", padx=4)

        sec2 = self._section(parent, "Club name in the database", key="clubname",
                             hint="Team.bin")
        self.team_vars = {}
        for label in ("English", "Database", "Short"):
            r = tk.Frame(sec2, bg=C_BG)
            r.pack(fill="x")
            tk.Label(r, text=label, bg=C_BG, fg=C_DIM, width=11, anchor="w",
                     font=F).pack(side="left")
            if label == "Database":
                # which data_s*.cpk the loads and saves go to, not a name slot
                self.db_var = tk.StringVar(value=self._short_path(self.cpk_path))
                self.cb_db = ttk.Combobox(r, textvariable=self.db_var, width=24, font=F,
                                          state="readonly", postcommand=self._refresh_databases)
                self.cb_db.pack(side="left", fill="x", expand=True, padx=6)
                self.cb_db.bind("<<ComboboxSelected>>",
                                lambda e: self.switch_database(self.db_var.get()))
                # the disk is only walked when the dropdown opens or the database changes
                self.cb_db["values"] = getattr(self, "_db_paths", None) or [
                    self._short_path(self.cpk_path)]
                continue
            v = tk.StringVar()
            self.team_vars[label] = v
            ent = ttk.Entry(r, textvariable=v, width=24, font=F)
            ent.pack(side="left", fill="x", expand=True, padx=6)
            self._field_widgets["Club name " + label] = ent
            self._field_section["Club name " + label] = "clubname"
            if label == "Short":                    # the button rides the last row, saving one
                ttk.Button(r, text="Apply", style="Ghost.TButton",
                           command=self.apply_team_name).pack(side="left")
        self._field_section["Club name"] = "clubname"
        note = "Transfers and club names are written to Team.bin + PlayerAssignment.bin on save."
        if self.edit_saves:
            note += ("\nAn in-game edit save overrides this database: "
                     + ", ".join(os.path.basename(p) for p in self.edit_saves))
        note_lbl = tk.Label(parent, text=note, bg=C_BG, fg=C_WARN if self.edit_saves else C_HINT,
                            anchor="w", justify="left", font=self.F_TINY)
        note_lbl.pack(fill="x", pady=(0, 2))
        self._wrap(note_lbl)

    def _filter_coach_values(self, evt):
        """Type-ahead for the coach box: 980 names is a scrolling exercise otherwise."""
        if getattr(evt, "keysym", "") in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(self.coach_var.get().strip())
        values = self._coach_choices()
        self.cb_coach["values"] = [v for v in values if q in norm(v)] if q else values

    def _filter_stadium_values(self, evt):
        if getattr(evt, "keysym", "") in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(self.stadium_var.get().strip())
        values = [f"{sid}  {name}" for sid, name in sorted(self.stadium_names.items())]
        self.cb_stadium["values"] = [v for v in values if q in norm(v)] if q else values

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
            return "—"
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
        old_cid = self.team_table.coach_id(idx)
        if old_cid == cid:
            return
        self.team_table.set_coach(idx, cid)
        team_id = self.team_table.tid(idx)
        self._note_change(("Team", team_id, "Coach"), self.clubs.get(team_id, f"team {team_id}"),
                          "Coach", self.coaches.get(old_cid, old_cid), self.coaches.get(cid, cid),
                          lambda i=idx, v=old_cid: self._set_team_coach(i, v))
        self._mark_table_dirty("Team")
        self.status(f"{self.team_table.name(idx)} coached by {self.coaches[cid]} — save to write it",
                    "ok")

    def _set_team_coach(self, idx, cid):
        self.team_table.set_coach(idx, cid)
        self._mark_table_dirty("Team")
        if getattr(self, "team_index_current", None) == idx:
            self.coach_var.set(f"{cid}  {self.coaches.get(cid, '\u2014')}")

    def apply_stadium(self):
        idx = getattr(self, "team_index_current", None)
        sid = self._leading_id(self.stadium_var.get())
        if idx is None or sid is None or sid not in self.stadium_names:
            self.status("pick a stadium from the list")
            return
        old_sid = self.team_table.stadium_id(idx)
        if old_sid == sid:
            return
        self.team_table.set_stadium(idx, sid)
        team_id = self.team_table.tid(idx)
        self._note_change(("Team", team_id, "Stadium"), self.clubs.get(team_id, f"team {team_id}"),
                          "Stadium", self.stadium_names.get(old_sid, old_sid),
                          self.stadium_names.get(sid, sid),
                          lambda i=idx, v=old_sid: self._set_team_stadium(i, v))
        self._mark_table_dirty("Team")
        self.status(f"{self.team_table.name(idx)} → {self.stadium_names[sid]} — save to write it",
                    "ok")

    def _set_team_stadium(self, idx, sid):
        self.team_table.set_stadium(idx, sid)
        self._mark_table_dirty("Team")
        if getattr(self, "team_index_current", None) == idx:
            self.stadium_var.set(f"{sid}  {self.stadium_names.get(sid, '\u2014')}")

    def apply_shirt_number(self):
        rows = getattr(self, "shirt_rows", [])
        if not rows:
            self.status("no club row for this player — nothing to number")
            return
        try:
            number = max(1, min(99, int(self.shirt_var.get())))
        except ValueError:
            return
        old_numbers = {self.assign.shirt_number(j) for j in rows}
        if old_numbers == {number}:
            return
        old = sorted(old_numbers)[0] if len(old_numbers) == 1 else "mixed"
        for j in rows:
            self.assign.set_shirt_number(j, number)
        self.shirt_var.set(str(number))
        pid = self.table.pid(self.index) if self.index is not None else None
        if pid is not None:
            self._note_change(("PlayerAssignment", pid, "Squad no."),
                              self.table.name(self.index), "Squad no.", old, number,
                              lambda r=list(rows), v=old: self._set_shirt_rows(r, v))
        self._mark_table_dirty("PlayerAssignment")
        self.status(f"squad number {number} written to {len(rows)} club row(s) — save to apply",
                    "ok")

    def _set_shirt_rows(self, rows, number):
        if not isinstance(number, int):
            return
        for j in rows:
            self.assign.set_shirt_number(j, number)
        self._mark_table_dirty("PlayerAssignment")
        if self.index is not None:
            self.shirt_var.set(str(number))

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
        name = self.clubs.get(team_id, "\u2014") if team_id else "(no club row)"
        self.lbl_club.configure(text=name)
        if team_id:
            squad = self.club_counts.get(team_id, 0)
            scope = (f"Coach, stadium and squad number belong to this club and every one of "
                     f"its {squad} players, not to the player above.  team id {team_id} · "
                     f"{len(club_rows)} club row(s), {len(rows)} total")
        else:
            scope = "This player has no club row — a transfer creates one."
        self.lbl_club_scope.configure(text=scope)
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
        self._db_paths = [self._short_path(p) for p in paths]
        self.cb_db["values"] = self._db_paths

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
        pid = self.table.pid(self.index)
        self._note_change(("PlayerAssignment", pid, "Club"), self.table.name(self.index),
                          "Club", self.clubs.get(previous, previous) if previous else "(none)",
                          self.clubs.get(team_id, team_id),
                          lambda j=rows[0], t=previous: self._set_assignment_team(j, t))
        self._refresh_row()
        self._mark_table_dirty("PlayerAssignment")
        self.load_club(self.index, pid)
        self.populate(self.q.get())
        self.status(f"moved to {self.clubs.get(team_id, team_id)} ({note}) — save to write it",
                    "ok")

    def _set_assignment_team(self, row, team_id):
        if team_id is None:
            return
        self.assign.set_team(row, team_id)
        self._mark_table_dirty("PlayerAssignment")
        if self.index is not None:
            pid = self.table.pid(self.index)
            self.player_team[pid] = team_id
            self._refresh_row()
            self.load_club(self.index, pid)

    def apply_team_name(self):
        idx = getattr(self, "team_index_current", None)
        if idx is None:
            self.status("no club row selected — nothing to rename")
            return
        changed = False
        team_id = self.team_table.tid(idx)
        for label, offset, length in CLUB_NAME_SLOTS:
            value = self.team_vars[label].get()
            old = self.team_table.slot(idx, offset, length)
            if value == old:
                continue
            self.team_table.set_slot(idx, offset, length, value)
            self._note_change(("Team", team_id, "Club name " + label),
                              self.clubs.get(team_id, f"team {team_id}"), label + " name",
                              old, value,
                              lambda i=idx, o=offset, l=length, v=old:
                              self._set_team_slot(i, o, l, v))
            changed = True
        if not changed:
            return
        self.clubs[team_id] = self.team_table.name(idx)
        self._club_names = sorted({n for t, n in self.clubs.items() if n and t >= 100})
        self.cb_club["values"] = self._club_names
        self._mark_table_dirty("Team")
        self.populate(self.q.get())
        self.status(f"renamed team {team_id} — save to write it", "ok")

    def _set_team_slot(self, idx, offset, length, value):
        self.team_table.set_slot(idx, offset, length, value)
        team_id = self.team_table.tid(idx)
        self.clubs[team_id] = self.team_table.name(idx)
        self._mark_table_dirty("Team")
        if getattr(self, "team_index_current", None) == idx:
            for label, off, ln in CLUB_NAME_SLOTS:
                if off == offset:
                    self.team_vars[label].set(value)
        self.populate(self.q.get())

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
        """Fill the list. Both result kinds are counted, and the Players/Clubs tabs say how many.

        A club name used to hijack the whole list: typing "arsenal" replaced every player row
        with club rows and there was no way to see the players again without retyping. Now the
        players that match the club stay listed, the club rows are one click away, and the
        counts on the tabs make the choice visible.
        """
        q = query.strip().lower()
        club_rows = []
        if q and not q.isdigit():
            targets = self.alias_targets(q)
            club_rows = sorted(((tid, name) for tid, name in self.clubs.items()
                                if tid >= 100 and self.club_matches(q, name)),
                               key=lambda r: (not any(t in norm(r[1]) for t in targets), norm(r[1])))
        rows = self._match_players(q)
        self._mode_counts = {"players": len(rows), "clubs": len(club_rows)}
        if self.view_mode.get() == "clubs" and not club_rows:
            self.view_mode.set("players")
        if not rows and club_rows:
            self.view_mode.set("clubs")          # nothing to show as players: use the clubs
        self._update_mode_tabs()
        if self.view_mode.get() == "clubs":
            return self._render_clubs(club_rows, select_first)
        return self._render_players(rows, select_first)

    def _match_players(self, q):
        self.mode = "players"
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
        return rows

    def set_view_mode(self, mode):
        self.view_mode.set(mode)
        self.populate(self.q.get())

    def _update_mode_tabs(self):
        counts = getattr(self, "_mode_counts", {"players": 0, "clubs": 0})
        for mode, lab in (("players", getattr(self, "tab_players", None)),
                          ("clubs", getattr(self, "tab_clubs", None))):
            if lab is None:
                continue
            active = self.view_mode.get() == mode
            lab.configure(text=f"{mode.title()}  {counts.get(mode, 0):,}",
                          fg=C_ACCENT if active else C_DIM,
                          bg=C_SEL if active else C_PANEL2)

    def _render_players(self, rows, select_first=False):
        self.mode = "players"
        self._player_headings()
        teams = self._club_teams()
        nation = self.country_filter.get().strip().lower()
        limit = 800
        shown = min(len(rows), limit)
        self.tree.delete(*self.tree.get_children())
        for r in rows[:shown]:
            i, pid, name, club, pos, _pos_i, age, _cl = r
            self.tree.insert("", "end", iid=str(i),
                             values=(pid, name, club, pos, self.ovr(i), age))
        # keep the player the form is showing selected when the filter did not remove them;
        # otherwise say so rather than silently editing a record that is no longer listed
        self._open_player_hidden = False
        if self.index is not None and self.tree.exists(str(self.index)):
            self.tree.selection_set(str(self.index))
            self.tree.see(str(self.index))
        else:
            self._open_player_hidden = self.index is not None
            self._select_first(select_first, shown)
        if not rows:
            if teams is not None and not teams:
                self.status(f"no club matches {self.club_filter.get().strip()!r}", "warn")
            elif teams:
                self.status(f"no match in {self.club_filter.get().strip()} — clear the Club box "
                            "or widen the search", "warn")
            else:
                self.status("no match — try a shorter name, a shirt name, a club, or an ID", "warn")
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
        other = self._mode_counts.get("clubs", 0)
        also = f" · {other:,} club{'s' if other != 1 else ''} match — see the Clubs tab" if other else ""
        if getattr(self, "_open_player_hidden", False):
            also += " · the open player is not in this filter"
        self.status(search_summary(shown, len(rows))
                    + hint + scope + also + f" · {len(self.table.records):,} players in table")

    # ---- clubs view
    def _render_clubs(self, rows, select_first=False):
        """Search results as clubs: one row per matching club, click to open its squad."""
        self.mode = "clubs"
        self._club_headings()
        self.tree.delete(*self.tree.get_children())
        for tid, name in rows:
            self.tree.insert("", "end", iid=f"club:{tid}",
                             values=(tid, name, f"{self.club_counts.get(tid, 0)} players", "", "", ""))
        shown = len(rows)
        if not shown:
            self.status("no club matches — try part of the name, or a nickname", "warn")
            return
        if select_first:
            return self.open_club(rows[0][0])
        self.status(f"{shown} club{'s' if shown != 1 else ''} match — click one to open its squad"
                    " · the Players tab has the players whose club matches")

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

    def discard_changes(self, ask=True):
        """Throw away every pending edit and reload the tables from disk.

        The header button used to be called "Revert", which read like "undo my last edit" while
        it actually reloaded every table — a whole session of work could disappear on one click.
        """
        if not self.dirty_tables:
            self.status("nothing to discard — there are no unsaved changes")
            return
        tables = ", ".join(sorted(n + ".bin" for n in self.dirty_tables))
        if ask and not messagebox.askokcancel(
                "Discard unsaved changes",
                f"{len(self._changes)} pending change(s) in {tables} will be thrown away.\n\n"
                "The tables are reloaded from disk; nothing is written or deleted there.",
                parent=self, default="cancel"):
            return
        self.reload(f"discarded unsaved changes in {tables} — reloaded from disk")

    def on_select_refresh(self):
        """Reload the tables and this record (the old "Revert" entry point)."""
        self.discard_changes(ask=False)

    def show_player(self, pid):
        """Open one player by id: clear a filter that hides them and select the row."""
        index = self.index_by_pid.get(pid)
        if index is None:
            self.status(f"no player with id {pid} in this database", "warn")
            return
        for name in ("club_filter", "country_filter", "position_filter"):
            var = getattr(self, name, None)
            if var is not None and var.get():
                var.set("")
        self.changed_only.set(False)
        self.free_agents_only.set(False)
        self.q.set("")
        self.populate("")
        if self.tree.exists(str(index)):
            self.tree.selection_set(str(index))
            self.tree.see(str(index))
        self.index = index
        self.load_form()

    def show_club(self, tid):
        """Open a club's squad in the list and its record in the details pane."""
        self.q.set("")
        self.open_club(tid)

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
        self._closing = True
        for attr in ("_portrait_job", "_radar_job", "_after", "_club_after",
                     "_resize_repaint_job", "_one_page_job", "_fold_job"):
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

    def _refresh_card(self, i=None):
        """The card's derived values: hero, meta line, OVR, value chips.

        Every edit path must land here — the chips used to be painted once from load_form,
        so editing Height left a stale `185 cm` chip above the new value in the field.
        """
        i = self.index if i is None else i
        t = self.table
        pid = t.pid(i)
        club = self.clubs.get(self.player_team.get(pid, -1), "—")
        natural = "/".join(nm for nm in pesdb.POS_NAMES if t.get(i, "Pos " + nm) >= 1) or "—"
        foot = "left foot" if t.get(i, "Stronger Foot") else "right foot"
        self.lbl_hero.configure(text=t.name(i) or "(no name)")
        self.lbl_sub.configure(text=f"ID {pid}   ·   {club}   ·   "
                                   f"{pesdb.country_name(t.get(i, 'Nationality')) or '—'}   ·   "
                                   f"{natural}   ·   {foot}")
        rating = self.ovr(i)
        self.ovr_big.configure(text=str(rating), fg=self._ovr_colour(rating))
        style_i = t.get(i, "Playing Style")
        for name, text in (("Style", PLAY_STYLES[style_i] if style_i < len(PLAY_STYLES)
                            else str(style_i)),
                           ("Height", f"{t.get(i, 'Height (cm)')} cm"),
                           ("Weight", f"{t.get(i, 'Weight (kg)')} kg"),
                           ("Age", f"age {t.get(i, 'Age')}"),
                           ("Form", f"form {t.get(i, 'Form')}")):
            self._card_chips[name].set_text(text)
            self._card_chips[name].set_state(fg=C_FG)     # a value is brighter than the empty dash
        self._card_chips["Style"].set_state(fg=C_ACCENT, line=C_ACCENT)

    def load_form(self):
        i = self.index
        t = self.table
        pid = t.pid(i)
        self._refresh_card(i)
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
            elif label == "Nationality":
                v.set(pesdb.country_name(val) or str(val))
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
            entry = self._changes.get(("Player", pid, label))
            self._draw_bar(self.vars[label + "::bar"], val,
                           base=entry["base"] if entry is not None else None)
        for label, bit in pesdb.SKILLS:
            self.vars["Skill " + label].set(t.get(i, "Skill " + label))
            self._style_skill_chip(label)
        self._clear_selection()
        self._update_skill_count()
        for key, counter in getattr(self, "_name_counters", {}).items():
            self._update_name_counter(key)
        self._refresh_field_markers()
        self.draw_radar(i)

    # ---------------------------------------------------------------- edits
    def _mark_dirty(self):
        self.dirty = True
        self.lbl_dirty.configure(text="● unsaved")

    def _mark_table_dirty(self, name):
        self.dirty_tables.add(name)
        self._mark_dirty()
        self._sync_history_buttons()

    # ------------------------------------------------------------ change ledger
    # Every pending edit lands here, keyed by (table, record id, field), so the UI can answer
    # "what have I changed?" without diffing 26k records. `base` is the value before this
    # session's first edit, which is what a revert restores and what the ability bar's ghost
    # tick marks; an edit that returns to `base` removes its entry.
    def _note_change(self, key, who, label, old, new, revert):
        table = key[0]
        entry = self._changes.get(key)
        base = entry["base"] if entry is not None else old
        if new == base:
            self._changes.pop(key, None)
        elif old != new or entry is not None:
            self._changes[key] = {"key": key, "table": table, "who": who, "label": label,
                                  "base": base, "new": new, "old": old, "revert": revert}
        self._update_changes_badge()
        self._mark_field_changed(key)

    def _drop_changes(self):
        """Forget every pending edit (after a save, or after discarding them)."""
        self._changes.clear()
        for widget in getattr(self, "_field_widgets", {}).values():
            self._tint_field(widget, False)
        self._update_changes_badge()

    def _update_changes_badge(self):
        lbl = getattr(self, "lbl_changes", None)
        n = len(self._changes)
        if lbl is not None:
            lbl.configure(text="" if not n else f"{n} change{'s' if n != 1 else ''}")
        # a section header carries its own count, so a collapsed block still says it has edits
        field_section = getattr(self, "_field_section", {})
        for sec, lab in getattr(self, "_section_counts", {}).items():
            count = sum(1 for e in self._changes.values()
                        if field_section.get(e["key"][2]) == sec)
            lab.configure(text=f"{count} changed" if count else "")

    def _tint_field(self, widget, dirty):
        try:
            if isinstance(widget, ttk.Entry):
                widget.configure(style="Dirty.TEntry" if dirty else "TEntry")
            elif isinstance(widget, ttk.Combobox):
                widget.configure(style="Dirty.TCombobox" if dirty else "TCombobox")
            else:
                widget.configure(bg=C_DIRTY if dirty else C_PANEL2)
        except tk.TclError:
            pass

    def _mark_field_changed(self, key, force_clean=False):
        """Tint the field a pending edit belongs to — only while its player is on screen."""
        table, ident, field = key
        widget = getattr(self, "_field_widgets", {}).get(field)
        if widget is None or self.index is None:
            return
        if table != "Player" or ident != self.table.pid(self.index):
            return
        self._tint_field(widget, not force_clean and key in self._changes)

    def _refresh_field_markers(self):
        """After loading a player, show exactly which of their fields have pending edits."""
        for field in getattr(self, "_field_widgets", {}):
            pid = self.table.pid(self.index) if self.index is not None else None
            self._mark_field_changed(("Player", pid, field))

    def _reconcile_dirty(self):
        """A save or a revert leaves only the tables the ledger still has edits for."""
        self.dirty_tables &= {e["table"] for e in self._changes.values()}
        if not self.dirty_tables:
            self.dirty = False
            self.lbl_dirty.configure(text="")
        self._sync_history_buttons()

    def _sync_history_buttons(self):
        for btn in getattr(self, "_bar_buttons", []):
            try:
                text = btn.cget("text")
            except tk.TclError:
                continue
            if text == "Undo":
                btn.state(["!disabled"] if self._undo else ["disabled"])
            elif text == "Redo":
                btn.state(["!disabled"] if self._redo else ["disabled"])

    def open_changes(self):
        """The change drawer: every pending edit, with a jump and a revert per row."""
        if not self._changes:
            self.status("nothing changed yet — edits show up here as you make them")
            return
        win = getattr(self, "_changes_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        win = self._changes_win = tk.Toplevel(self)
        win.title(f"Changes — {len(self._changes)} pending")
        win.configure(bg=C_BG)
        win.transient(self)
        win.geometry(f"{int(760 * self.ui)}x{int(440 * self.ui)}")

        head = tk.Frame(win, bg=C_BG)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text="UNSAVED CHANGES", bg=C_BG, fg=C_ACCENT,
                 font=(self.FONT, 11, "bold")).pack(side="left")
        tk.Label(head, text="double-click a row to open the record", bg=C_BG, fg=C_HINT,
                 font=(self.FONT, 8)).pack(side="left", padx=10)

        body = tk.Frame(win, bg=C_BG)
        body.pack(fill="both", expand=True, padx=14)
        cols = ("table", "who", "field", "was", "now")
        tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor in (("table", "Table", 120, "w"),
                                         ("who", "Record", 220, "w"),
                                         ("field", "Field", 160, "w"),
                                         ("was", "Was", 60, "e"), ("now", "Now", 60, "e")):
            tree.heading(col, text=text)
            tree.column(col, width=int(width * self.ui), anchor=anchor,
                        stretch=(col in ("who", "field")))
        tree.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        bar.pack(side="left", fill="y")
        tree.configure(yscrollcommand=bar.set)
        self._changes_tree = tree
        for key, e in sorted(self._changes.items(),
                             key=lambda kv: (kv[1]["table"], str(kv[1]["who"]))):
            tree.insert("", "end", iid=str(len(tree.get_children())), values=(
                e["table"], e["who"], e["label"],
                "-" if e["base"] is None else e["base"],
                "-" if e["new"] is None else e["new"]))
        self._changes_order = [k for k, _ in sorted(self._changes.items(),
                                                   key=lambda kv: (kv[1]["table"],
                                                                   str(kv[1]["who"])))]

        def chosen():
            return [self._changes_order[int(i)] for i in tree.selection()
                    if int(i) < len(self._changes_order)]

        def revert_selected():
            keys = chosen()
            skipped = 0
            for key in keys:
                entry = self._changes.get(key)
                if entry is None:
                    continue
                if entry.get("revert") is None:
                    skipped += 1
                    continue
                entry["revert"]()
                self._mark_field_changed(key, force_clean=True)
            self._reconcile_dirty()
            done = len(keys) - skipped
            self.status(f"reverted {done} change{'s' if done != 1 else ''}"
                        + (f" · {skipped} import row(s) only revert with Discard all" if skipped else "")
                        if keys else "select a row to revert", "ok")
            win.destroy()
            if self.index is not None:
                self.load_form()

        def go_to():
            keys = chosen()
            if not keys:
                return
            table, ident, _field = keys[0]
            win.destroy()
            if table in ("Player", "PlayerAssignment"):
                self.show_player(ident)
            elif table == "Team":
                self.show_club(ident)
            elif table == "Coach":
                self.open_coaches()

        tree.bind("<Double-1>", lambda e: go_to())
        buttons = tk.Frame(win, bg=C_BG)
        buttons.pack(fill="x", padx=14, pady=12)
        ttk.Button(buttons, text="Revert selected", style="Ghost.TButton",
                   command=revert_selected).pack(side="left")
        ttk.Button(buttons, text="Revert all", style="Ghost.TButton",
                   command=lambda: (self.discard_changes(ask=False), win.destroy())
                   ).pack(side="left", padx=8)
        ttk.Button(buttons, text="Save now", style="Save.TButton",
                   command=self.save).pack(side="right")
        ttk.Button(buttons, text="Close", style="Ghost.TButton",
                   command=win.destroy).pack(side="right", padx=8)
        self._apply_cursors()

    # ------------------------------------------------------------- undo / redo
    def _push_undo(self, undo, redo):
        """Record one reversible edit; history is bounded so a session cannot grow forever."""
        self._undo.append((undo, redo))
        del self._undo[:-200]
        self._redo.clear()             # a new edit forks the history: redo would be a lie
        self._sync_history_buttons()

    def _set_player_field(self, index, label, value):
        """The single write path for a player value: table, form, bar, radar, row, ledger."""
        old = self.table.get(index, label)
        pid = self.table.pid(index)
        self.table.set(index, label, value)
        self._ovr_cache.pop(index, None)          # an ability moved: its OVR is stale
        if label in ("Owner Club", "Youth Club"):  # the "which fields are blank" answer moved
            self._career_empty_cache = None
            self._update_career_note()
        self._edited_pids.add(pid)
        if old != value:
            self._note_change(("Player", pid, label), self.table.name(index) or f"#{pid}",
                              label, old, value,
                              lambda i=index, l=label, v=old: self._set_player_field(i, l, v))
        if self.index == index:
            if label in ("Youth Club", "Owner Club"):
                self.vars[label].set(self._club_ref_text(value))
            elif label == "Nationality":
                self.vars[label].set(pesdb.country_name(value) or str(value))
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
            self._refresh_card(index)
        self._refresh_row()
        self._mark_table_dirty("Player")

    def undo(self):
        if not self._undo:
            self.status("nothing to undo")
            return
        undo, redo = self._undo.pop()
        undo()
        self._redo.append((undo, redo))
        self._sync_history_buttons()
        self._reconcile_dirty()
        self.status("undone", "ok")

    def redo(self):
        if not self._redo:
            self.status("nothing to redo")
            return
        undo, redo = self._redo.pop()
        redo()
        self._undo.append((undo, redo))
        self._sync_history_buttons()
        self._reconcile_dirty()
        self.status("redone", "ok")

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
        width = bar.winfo_width() or int(bar.cget("width")) or 80
        val = bar_value(evt.x, width)
        if str(val) != self.vars[label].get():
            self.vars[label].set(str(val))
            self.apply_field(label)

    def _draw_bar(self, bar, val, base=None):
        """Redraw one ability bar: track, tiered fill, and a ghost tick at the value it started with."""
        bar.delete("all")
        width = bar.winfo_width() or int(bar.cget("width")) or 80
        height = int(bar.cget("height"))
        bar.create_rectangle(0, 0, width, height, fill=C_BAR, width=0)
        frac = max(0.0, min(1.0, (val - 40) / 59))
        bar.create_rectangle(0, 0, max(1, int(width * frac)), height,
                             fill=C_ACCENT if val >= 80 else
                             (C_ACCENT_LO if val >= 65 else C_BAR_LOW), width=0)
        if base is not None and base != val:
            x = max(1, min(width - 2, int(width * max(0.0, min(1.0, (base - 40) / 59)))))
            bar.create_line(x, 0, x, height, fill=C_GHOST, width=2)

    def reset_field(self, label):
        """Right-click an ability bar: put that one field back to its pre-edit value."""
        if self.index is None:
            return
        entry = self._changes.get(("Player", self.table.pid(self.index), label))
        if entry is None:
            self.status(f"{label} has not been changed", "warn")
            return
        old = self.vars[label].get()
        self.vars[label].set(str(entry["base"]))
        self.apply_field(label)
        self.status(f"{label} back to {entry['base']} (was {old})", "ok")

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
        if label == "Nationality" and not raw.isdigit():
            cid = self._country_id(raw)
            if cid is None:
                self.status(f"no country called {raw!r} — pick one from the list", "warn")
                return
            raw = str(cid)
        try:
            val = int(float(raw))
        except ValueError:
            self.status(f"{label} needs a number", "warn")
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

        field = self._name_field(key)

        def write(text):
            old = self.table.name(i) if key == "name" else self.table.shirt(i)
            if key == "name":
                self.table.set_name(i, full=text, shirt=self.table.shirt(i))
            else:
                self.table.set_name(i, full=self.table.name(i), shirt=text)
            self.vars[key].set(text)
            self._update_name_counter(key)
            self._refresh_row()
            if self.index == i:
                self._refresh_card(i)
            self._mark_table_dirty("Player")
            self._edited_pids.add(self.table.pid(i))
            self._note_change(("Player", self.table.pid(i), field),
                              self.table.name(i) or f"#{self.table.pid(i)}", field,
                              old, text, lambda t=old: write(t))

        write(val)
        self._push_undo(lambda: write(cur), lambda: write(val))

    def _selected_players(self):
        """Record indexes selected in the list, ignoring club rows (which have no record)."""
        out = []
        for item in self.tree.selection():
            if str(item).startswith("club:"):
                continue
            try:
                out.append(int(item))
            except ValueError:
                continue
        return out

    def bulk_apply(self, label, op, amount):
        """Set or shift one numeric field on every selected player, as one undo step."""
        items = self._selected_players()
        if not items or label not in self.NUMERIC_FIELDS:
            self.status("select players and a numeric field first", "warn")
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
        what = f"set {label} to {max(lo, min(hi, amount))}" if op == "set" \
            else f"{op} {amount:+d} {label}"
        self.status(f"{what} on {len(items)} player(s) — save to write it", "ok")
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
        sel = self._selected_players()
        if len(sel) != 2:
            self.status("select exactly two players (ctrl-click), then Compare", "warn")
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
        self._ovr_cache.clear()
        seq = getattr(self, "_import_seq", 0) + 1
        self._import_seq = seq
        self._note_change(("Player", f"import{seq}", "Roster CSV"), f"{touched:,} rows",
                          "Roster import", "\u2014", "\u2014", None)
        if self.index is not None:          # no player on screen: nothing to redraw
            self.load_form()
        self.status(f"imported {touched:,} rows — save to write it · "
                    "\"Changed only\" stays empty after an import", "warn")
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

    def _duplicate_squad_numbers(self):
        """Squad numbers shared inside one club — the mistake a bulk shirt edit makes."""
        if "PlayerAssignment" not in self.dirty_tables:
            return []
        seen = {}
        for j in range(len(self.assign.records)):
            team = self.assign.team(j)
            if team <= pesdb.NATIONAL_MAX:
                continue
            seen.setdefault((team, self.assign.shirt_number(j)), []).append(self.assign.pid(j))
        out = []
        for (team, number), pids in sorted(seen.items()):
            if len(pids) < 2:
                continue
            names = [self.table.name(self.index_by_pid[p]) for p in pids
                     if p in self.index_by_pid]
            more = "" if len(pids) <= 3 else f" +{len(pids) - 3} more"
            out.append(f"{self.clubs.get(team, team)}: {len(pids)} players share number "
                       f"{number} — {', '.join(names[:3])}{more}")
        return out[:5]

    def _preflight_summary(self):
        """What the write will contain, and what is wrong with doing it now."""
        counts = {}
        for entry in self._changes.values():
            counts[entry["table"]] = counts.get(entry["table"], 0) + 1
        warnings = []
        if self.game_running():
            warnings.append("The game is running. Writing now can be ignored by it or leave "
                            "the CPK half-written — close it first.")
        if self.edit_saves:
            warnings.append("An in-game EDIT save is present and overrides this database on "
                            "load: " + ", ".join(os.path.basename(p) for p in self.edit_saves))
        warnings.extend(self._duplicate_squad_numbers())
        return counts, warnings

    def _preflight(self):
        """Review sheet before writing. Returns True to go ahead.

        The old flow was a yes/no messagebox naming the .bin files; it could not show how many
        edits were waiting, where the backup would land, or the two things that actually bite
        (the game running, and an in-game EDIT save shadowing the database).
        """
        counts, warnings = self._preflight_summary()

        dlg = tk.Toplevel(self)
        dlg.title("Write to the game database")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.resizable(False, False)
        body = tk.Frame(dlg, bg=C_BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="WRITE TO THE GAME", bg=C_BG, fg=C_ACCENT,
                 font=(self.FONT, 12, "bold")).pack(anchor="w")
        tk.Label(body, text=os.path.basename(self.cpk_path) + "   ·   " + self._short_path(
            self.cpk_path), bg=C_BG, fg=C_DIM, font=self.F_SMALL).pack(anchor="w", pady=(2, 10))

        for name in ("Player", "Team", "PlayerAssignment", "Coach", "PlayerAppearance"):
            if name not in self.dirty_tables:
                continue
            row = tk.Frame(body, bg=C_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{name}.bin", bg=C_BG, fg=C_FG, width=20, anchor="w",
                     font=self.F_SMALL).pack(side="left")
            tk.Label(row, text=f"{counts.get(name, 0)} change(s)", bg=C_BG, fg=C_ACCENT,
                     width=14, anchor="w", font=self.F_SMALL).pack(side="left")
            tk.Label(row, text=f"{len(self.tables[name].records):,} rows now", bg=C_BG,
                     fg=C_DIM, font=self.F_SMALL).pack(side="left")

        tk.Label(body, text=f"Backup: {self._short_path(self.backups_dir)}", bg=C_BG, fg=C_HINT,
                 font=self.F_TINY).pack(anchor="w", pady=(10, 0))
        if warnings:
            tk.Label(body, text="BEFORE YOU WRITE", bg=C_BG, fg=C_WARN,
                     font=(self.FONT, 9, "bold")).pack(anchor="w", pady=(10, 2))
            for text in warnings:
                tk.Label(body, text="• " + text, bg=C_BG, fg=C_WARN, justify="left",
                         wraplength=int(460 * self.ui), font=self.F_SMALL).pack(anchor="w")
        choice = {"go": False}

        def go():
            choice["go"] = True
            dlg.destroy()

        buttons = tk.Frame(body, bg=C_BG)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Write to game", style="Save.TButton",
                   command=go).pack(side="right")
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="right", padx=8)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Return>", lambda e: go())
        self._apply_cursors()
        dlg.grab_set()
        self.wait_window(dlg)
        return choice["go"]

    def save(self):
        if not self.dirty_tables:
            self.status("nothing to save — no edits are pending")
            return
        if self.cfg["confirm_save"] and not self._preflight():
            self.status("save cancelled — nothing was written", "warn")
            return
        if not self.cfg["confirm_save"] and (self.game_running() or self.edit_saves) \
                and not self._preflight():
            self.status("save cancelled — nothing was written", "warn")
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
        self._drop_changes()
        summary = " · ".join(f"{name} {size:,} B → {n:,} rows" for name, size, n in written)
        self.status(f"wrote {summary} ({'ok' if ok else 'MISMATCH'})"
                    " · applies next time the game starts", "ok" if ok else "error")

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
        self._remember_geometry()
        self.destroy()

    def _remember_geometry(self):
        """Remember the window size, so a session reopens at the size it was left."""
        if not self.winfo_exists():
            return
        size = f"{self.winfo_width()}x{self.winfo_height()}"
        if size == str(self.cfg.get("window")):
            return
        self.cfg["window"] = size
        try:
            settings.save(self.cfg)
        except OSError:
            pass

    def reload(self, note=None):
        ensure_vanilla(self.cpk_path)
        self.tables = load_tables(self.cpk_path)
        self.reindex()
        self._edited_pids.clear()
        self._drop_changes()
        self._undo.clear()
        self._redo.clear()
        self.dirty_tables.clear()
        self.populate(self.q.get())
        self.dirty = False
        self.lbl_dirty.configure(text="")
        self._sync_history_buttons()
        self.status(note or "reloaded the current database (override if present, else the CPK copy)",
                    "ok")

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
        chosen_theme = tk.StringVar(value=dict(theme.CHOICES).get(THEME, "Amber"))

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

        tk.Label(body, text="Theme", bg=C_BG, fg=C_DIM, anchor="w").grid(
            row=7, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(body, textvariable=chosen_theme, state="readonly", width=14,
                     values=[label for _key, label in theme.CHOICES],
                     font=self.F_LABEL).grid(row=7, column=1, sticky="w", padx=8, pady=(10, 0))
        tk.Label(body, text="applies next launch", bg=C_BG, fg=C_HINT,
                 font=self.F_TINY).grid(row=7, column=2, sticky="w")

        err = tk.Label(body, text="", bg=C_BG, fg=C_MAGENTA, anchor="w",
                       font=(self.FONT, 9))
        err.grid(row=8, column=0, columnspan=3, sticky="we", pady=(8, 0))

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
            picked = next((key for key, label in theme.CHOICES
                           if label == chosen_theme.get()), THEME)
            self.cfg.update(game_dir=gd, backups_dir=backups.get().strip(),
                            max_backups=max_backups, confirm_save=bool(confirm.get()),
                            photos=bool(photos.get()), theme=picked)
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
            self._sync_photo_slot()
            if pid is not None:              # a just-enabled photo setting shows at once
                self.load_portrait(pid)
            kept = settings.prune_backups(self.backups_dir, self.cfg["max_backups"])
            note = f"settings saved → {settings.CONFIG_PATH}"
            if os.environ.get("SPFL_DIR"):
                note += " · SPFL_DIR is set and still wins over the game folder"
            if kept:
                note += f" · pruned {kept} old backup(s)"
            if picked != THEME:
                note += f" · theme {dict(theme.CHOICES)[picked]} applies next launch"
            self.status(f"{note} · {len(self.table.records):,} players", "ok")

        buttons = tk.Frame(body, bg=C_BG)
        buttons.grid(row=9, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", style="Save.TButton",
                   command=commit).pack(side="right", padx=8)
        dlg.bind("<Return>", lambda e: commit())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self._apply_cursors()
        dlg.focus_set()

    def status(self, msg, level="info"):
        """Footer message with a severity: info is quiet, ok/warn/error colour the line.

        The old single colour meant "no club matches …" (a real dead end) looked exactly like
        "reloaded the database" (routine), which is the kind of thing people stop reading.
        """
        colours = {"info": C_DIM, "ok": C_GREEN, "warn": C_WARN, "error": C_MAGENTA}
        self.statusbar.configure(text=msg, fg=colours.get(level, C_DIM))


class CoachWindow(tk.Toplevel):
    """Every coach in Coach.bin: name, Japanese name, nationality, adaptability, club.

    Changes go through the app's change ledger like every other edit, and the nationality is a
    country name from the same list the player form uses — it used to be a 0-511 spinbox, the
    last place in the app where you had to know a database id.
    """

    FIELDS = ("Name", "Japanese name", "Nationality", "Adaptability")

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.index = None
        self._country_ids = app._country_ids
        self._country_names = app._country_names
        self.title("Coaches \u2014 Coach.bin")
        self.configure(bg=C_BG)
        ui = app.ui
        self.geometry(f"{int(880 * ui)}x{int(700 * ui)}")
        self.minsize(int(520 * ui), int(420 * ui))
        self.transient(app)

        top = tk.Frame(self, bg=C_BG)
        top.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(top, text="Search", bg=C_BG, fg=C_DIM, font=app.F_LABEL).pack(side="left")
        self.q = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.q, font=app.F_VALUE)
        ent.pack(side="left", fill="x", expand=True, padx=8)
        ent.bind("<KeyRelease>", lambda _e: self.fill())
        self.count = tk.Label(top, text="", bg=C_BG, fg=C_HINT, font=app.F_TINY)
        self.count.pack(side="left")

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=14)
        self.tree = ttk.Treeview(body, columns=("id", "name", "country", "club", "adapt"),
                                 show="headings", selectmode="browse")
        for col, width, text, anchor in (("id", 64, "ID", "e"), ("name", 230, "Name", "w"),
                                         ("country", 130, "Nationality", "w"),
                                         ("club", 190, "Club", "w"),
                                         ("adapt", 60, "Adapt", "e")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=int(width * ui), anchor=anchor,
                             stretch=(col in ("name", "club")))
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
        form.pack(fill="x", padx=14, pady=(14, 0))
        self.vars = {}
        for n, label in enumerate(self.FIELDS):
            tk.Label(form, text=label, bg=C_BG, fg=C_DIM, anchor="w", width=14,
                     font=app.F_LABEL).grid(row=n // 2, column=(n % 2) * 2, sticky="w", pady=3)
            v = tk.StringVar()
            self.vars[label] = v
            if label == "Adaptability":
                w = tk.Spinbox(form, textvariable=v, from_=40, to=99, width=6,
                               font=app.F_VALUE)
            elif label == "Nationality":
                w = ttk.Combobox(form, textvariable=v, values=self._country_names, width=20,
                                 font=app.F_VALUE)
                w.bind("<KeyRelease>", self._filter_countries)
            else:
                w = ttk.Entry(form, textvariable=v, width=30, font=app.F_VALUE)
            w.grid(row=n // 2, column=(n % 2) * 2 + 1, sticky="w", padx=(0, 18), pady=3)
            self._field = w if label == "Name" else getattr(self, "_field", w)
        self.note = tk.Label(self, text="", bg=C_BG, fg=C_ACCENT, anchor="w",
                             font=app.F_LABEL)
        self.note.pack(fill="x", padx=14, pady=(8, 0))
        ttk.Button(self, text="Apply to coach", style="Save.TButton",
                   command=self.apply).pack(anchor="e", padx=14, pady=12)

        self.fill()

    def _filter_countries(self, evt):
        if getattr(evt, "keysym", "") in ("Return", "KP_Enter", "Up", "Down", "Escape", "Tab"):
            return
        q = norm(self.vars["Nationality"].get().strip())
        boxes = [w for w in self.winfo_children() for w in w.winfo_children()
                 if isinstance(w, ttk.Combobox)]
        for box in boxes:
            box["values"] = ([n for n in self._country_names if q in norm(n)][:80] if q
                             else self._country_names)

    def fill(self):
        needle = self.q.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        t = self.app.coach_table
        shown = 0
        for i in range(len(t.records)):
            name = t.name(i)
            club = self.club_of.get(t.cid(i), "")
            if needle and needle not in name.lower() and needle not in club.lower():
                continue
            self.tree.insert("", "end", iid=str(i), values=(
                t.cid(i), name, pesdb.country_name(t.country(i)) or t.country(i),
                club, t.adaptability(i)))
            shown += 1
        self.count.configure(text=f"{shown:,} of {len(t.records):,} coaches")

    def pick(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        t = self.app.coach_table
        self.index = int(sel[0])
        self.vars["Name"].set(t.name(self.index))
        self.vars["Japanese name"].set(t.japanese(self.index))
        self.vars["Nationality"].set(pesdb.country_name(t.country(self.index)) or "")
        self.vars["Adaptability"].set(str(t.adaptability(self.index)))

    def write(self, field, value):
        """Write one field of the selected coach and refresh the row (used by revert too)."""
        t = self.app.coach_table
        if self.index is None:
            return
        if field == "Name":
            t.set_name(self.index, value)
        elif field == "Japanese name":
            t.set_japanese(self.index, value)
        elif field == "Nationality":
            t.set_country(self.index, int(value))
        elif field == "Adaptability":
            t.set_adaptability(self.index, int(value))
        self.app._mark_table_dirty("Coach")
        if self.tree.exists(str(self.index)):
            values = list(self.tree.item(str(self.index), "values"))
            values[1] = t.name(self.index)
            values[2] = pesdb.country_name(t.country(self.index)) or t.country(self.index)
            values[4] = t.adaptability(self.index)
            self.tree.item(str(self.index), values=values)

    def apply(self):
        if self.index is None:
            self.note.configure(text="pick a coach in the list first")
            return
        t = self.app.coach_table
        raw_country = self.vars["Nationality"].get().strip()
        country = self._country_ids.get(raw_country)
        if country is None and raw_country.isdigit():
            country = max(0, min(511, int(raw_country)))
        if country is None:
            self.note.configure(text=f"no country called {raw_country!r} — pick one from the list")
            return
        try:
            adapt = max(40, min(99, int(self.vars["Adaptability"].get())))
        except ValueError:
            self.note.configure(text="adaptability is a number (40-99)")
            return
        wanted = {"Name": self.vars["Name"].get(),
                  "Japanese name": self.vars["Japanese name"].get(),
                  "Nationality": country,
                  "Adaptability": adapt}
        current = {"Name": t.name(self.index),
                   "Japanese name": t.japanese(self.index),
                   "Nationality": t.country(self.index),
                   "Adaptability": t.adaptability(self.index)}
        cid = t.cid(self.index)
        changed = 0
        for field, value in wanted.items():
            old = current[field]
            if old == value:
                continue
            self.write(field, value)
            show = (lambda v, f=field: pesdb.country_name(v) or v) if field == "Nationality" \
                else (lambda v: v)
            self.app._note_change(("Coach", cid, field), t.name(self.index) or f"coach {cid}",
                                  field, show(old), show(value),
                                  lambda f=field, v=old: self.write(f, v))
            changed += 1
        self.note.configure(
            text=(f"{changed} field(s) changed on coach {cid} — Save to game writes Coach.bin"
                  if changed else "nothing to apply — the coach is unchanged"))


class AppearanceWindow(tk.Toplevel):
    """The selected player's appearance record: PlayerAppearance.bin, one 60-byte row each.

    The list is still grouped by head/eyes/hair/kit (and still addressable by field name), but
    a field is now edited with a slider that knows its own range, and the row is marked once it
    has a pending change — 117 raw numbers under one spinbox was the least usable screen left.
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.index = app.index
        self._fields = {}
        self.title(f"Appearance \u2014 {app.table.name(self.index)}")
        self.configure(bg=C_BG)
        ui = app.ui
        self.geometry(f"{int(720 * ui)}x{int(760 * ui)}")
        self.minsize(int(520 * ui), int(420 * ui))
        self.transient(app)

        head = tk.Frame(self, bg=C_BG)
        head.pack(fill="x", padx=14, pady=(14, 4))
        tk.Label(head, text="APPEARANCE", bg=C_BG, fg=C_ACCENT,
                 font=(app.FONT, 12, "bold")).pack(side="left")
        tk.Label(head, text=f"{app.table.name(self.index)}  \u00b7  id {app.table.pid(self.index)}",
                 bg=C_BG, fg=C_DIM, font=app.F_LABEL).pack(side="left", padx=10)
        tk.Label(head, text="filter", bg=C_BG, fg=C_HINT,
                 font=app.F_TINY).pack(side="left", padx=(14, 4))
        self.filter = tk.StringVar()
        ent = ttk.Entry(head, textvariable=self.filter, width=16, font=app.F_LABEL)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda _e: self.filter_rows())

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=14, pady=6)
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
        form.pack(fill="x", padx=14, pady=(6, 0))
        self.field_label = tk.Label(form, text="pick a field", bg=C_BG, fg=C_FG, anchor="w",
                                    font=(app.FONT, 11, "bold"))
        self.field_label.pack(fill="x")
        self.range_label = tk.Label(form, text="", bg=C_BG, fg=C_HINT, anchor="w",
                                    font=app.F_TINY)
        self.range_label.pack(fill="x")
        slide_row = tk.Frame(form, bg=C_BG)
        slide_row.pack(fill="x", pady=(4, 2))
        self.scale = tk.Scale(slide_row, from_=0, to=1, orient="horizontal", showvalue=False,
                              bg=C_BG, fg=C_DIM, troughcolor=C_PANEL2, bd=0, relief="flat",
                              highlightthickness=0, activebackground=C_ACCENT,
                              command=self._slide, length=int(420 * ui))
        self.scale.pack(side="left", fill="x", expand=True)
        self.value = tk.StringVar()
        self.spin = tk.Spinbox(slide_row, textvariable=self.value, width=6,
                               font=app.F_VALUE, command=self.apply)
        self.spin.pack(side="left", padx=8)
        self.spin.bind("<Return>", lambda _e: self.apply())
        ttk.Button(slide_row, text="Apply", style="Ghost.TButton",
                   command=self.apply).pack(side="left")
        ttk.Button(slide_row, text="Reset field", style="Ghost.TButton",
                   command=self.reset).pack(side="left", padx=6)

        self.note = tk.Label(self, text="", bg=C_BG, fg=C_ACCENT, anchor="w",
                             font=app.F_LABEL)
        self.note.pack(fill="x", padx=14, pady=(6, 4))
        tk.Label(self, text="Appearance rows follow Player.bin order, one row per player.",
                 bg=C_BG, fg=C_HINT, font=app.F_TINY, anchor="w").pack(fill="x", padx=14)
        ttk.Button(self, text="Close", style="Ghost.TButton",
                   command=self.destroy).pack(anchor="e", padx=14, pady=10)
        self.fill()

    # ---- rows
    def fill(self):
        t = self.app.appearance_table
        for n, (group, labels) in enumerate(pesdb.APPEARANCE_GROUPS):
            gid = self.tree.insert("", "end", text=group, values=("",), open=(n == 0))
            for label in labels:
                value = t.get(self.index, label)
                self.tree.insert(gid, "end", iid=label, text=label, values=(value,))
                self._fields[label] = gid
        self._mark_changed()

    def filter_rows(self):
        q = norm(self.filter.get().strip())
        for group, labels in pesdb.APPEARANCE_GROUPS:
            shown = [l for l in labels if not q or q in norm(l)]
            for label in labels:
                parent = self.tree.parent(label)
                if label in shown:
                    self.tree.move(label, parent, "end")
                else:
                    self.tree.detach(label)

    def _mark_changed(self):
        """A pending change tints the row, and the group stays open so it is findable."""
        changed = {e["key"][2] for e in self.app._changes.values()
                   if e["table"] == "PlayerAppearance" and e["key"][1] == self.index}
        for label, group in self._fields.items():
            tags = ("changed",) if label in changed else ()
            self.tree.item(label, tags=tags)
        self.tree.tag_configure("changed", foreground=C_ACCENT)
        for group in {g for g in self._fields.values()}:
            self.tree.item(group, open=True)

    # ---- editing
    def pick(self, _e=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in pesdb.APPEARANCE_BY_LABEL:
            return
        label = sel[0]
        _bit, width, add = pesdb.APPEARANCE_BY_LABEL[label]
        lo, hi = add, add + (1 << width) - 1
        self.scale.configure(from_=lo, to=hi)
        value = self.app.appearance_table.get(self.index, label)
        self.value.set(str(value))
        self.scale.set(value)
        self.field_label.configure(text=label)
        self.range_label.configure(text=f"range {lo} \u2013 {hi}"
                                        + ("  \u00b7  changed this session"
                                           if self._is_changed(label) else ""))

    def _is_changed(self, label):
        return ("PlayerAppearance", self.index, label) in self.app._changes

    def _slide(self, raw):
        """The slider feeds the value box; the write still goes through apply()."""
        try:
            self.value.set(str(int(float(raw))))
        except ValueError:
            return

    def apply(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in pesdb.APPEARANCE_BY_LABEL:
            self.note.configure(text="pick a field in the list first")
            return
        label = sel[0]
        _bit, width, add = pesdb.APPEARANCE_BY_LABEL[label]
        lo, hi = add, add + (1 << width) - 1
        try:
            value = max(lo, min(hi, int(float(self.value.get()))))
        except ValueError:
            self.note.configure(text="a whole number, please")
            return
        table = self.app.appearance_table
        old = table.get(self.index, label)
        if old == value:
            self.note.configure(text=f"{label} is already {value}")
            return
        table.set(self.index, label, value)
        self.app._mark_table_dirty("PlayerAppearance")
        self.app._note_change(("PlayerAppearance", self.index, label),
                              self.app.table.name(self.index), label, old, value,
                              lambda v=old, l=label: self._write(l, v))
        self.tree.item(label, values=(value,))
        self.value.set(str(value))
        self.scale.set(value)
        self._mark_changed()
        self.range_label.configure(text=f"range {lo} \u2013 {hi}  \u00b7  changed this session")
        self.note.configure(text=f"{label} = {value} — Save to game writes PlayerAppearance.bin")

    def _write(self, label, value):
        self.app.appearance_table.set(self.index, label, value)
        self.app._mark_table_dirty("PlayerAppearance")
        if self.tree.exists(label):
            self.tree.item(label, values=(value,))
        if self.winfo_exists():
            self._mark_changed()

    def reset(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in pesdb.APPEARANCE_BY_LABEL:
            return
        label = sel[0]
        entry = self.app._changes.get(("PlayerAppearance", self.index, label))
        if entry is None:
            self.note.configure(text=f"{label} has not been changed")
            return
        self._write(label, entry["base"])
        self.value.set(str(self.app.appearance_table.get(self.index, label)))
        self.scale.set(self.app.appearance_table.get(self.index, label))
        self._mark_changed()
        self.note.configure(text=f"{label} back to {entry['base']}")


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

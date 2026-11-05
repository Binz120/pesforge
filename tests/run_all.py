#!/usr/bin/env python3
"""Assert-based tests for the PESForge editor. Run: python3 tests/run_all.py

No pytest on this box: every check is a plain assert and the exit code is the verdict.
Each CLI test gets its own sandbox (SPFL_DIR / SPFL_STATE) so nothing touches the real
game directory.
"""
import csv
import contextlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pesdb
import pesdb_maps as maps
import pes2015
import pes2019
import settings
import settings

# Any machine: SPFL_GAME wins, else whatever the editor would auto-detect.
GAME = os.environ.get("SPFL_GAME") or settings.game_dir()
CPK = settings.database_path(GAME) if GAME else ""
PLAYER = "Player.bin"
PA = "PlayerAssignment.bin"


@contextlib.contextmanager
def gui():
    """A throwaway Editor with its own config; skips when there is no display/database."""
    if not os.environ.get("DISPLAY"):
        raise FileNotFoundError("no DISPLAY")
    import tkinter as tk
    import editor
    box = tempfile.mkdtemp(prefix="pesforge-gui-")
    saved = settings.CONFIG_PATH
    settings.CONFIG_PATH = os.path.join(box, "config.json")
    app = editor.Editor()
    app.attributes("-alpha", 0.0)      # tests must never flash a window on the real desktop
    app.update()
    try:
        yield app
    finally:
        try:
            app.destroy()
        except tk.TclError:
            pass
        settings.CONFIG_PATH = saved


def sandbox():
    """A private copy of the CPK: edits patch it in place, so a symlink would hit the real game."""
    box = tempfile.mkdtemp(prefix="pesforge-test-")
    os.makedirs(os.path.join(box, "game", "download"))
    shutil.copy2(CPK, os.path.join(box, "game", "download", "data_s2627.cpk"))
    os.makedirs(os.path.join(box, "state"))
    return box


def box_cpk(box):
    return os.path.join(box, "game", "download", "data_s2627.cpk")


def cli(box, *args, expect=0):
    env = dict(os.environ, SPFL_DIR=os.path.join(box, "game"),
               SPFL_STATE=os.path.join(box, "state"),
               SPFL_CONFIG=os.path.join(box, "state", "config.json"))
    p = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py")] + list(args),
                       capture_output=True, text=True, env=env)
    assert p.returncode == expect, f"{args} -> rc {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p.stdout + p.stderr


def override(box, name):
    return box_cpk(box)


def scan_rows(raw, field, team=None):
    """Direct row scan: values of `field` (1=pid, 2=team), optionally filtered by team."""
    out = []
    for i in range(0, len(raw) - 15, 16):
        row = struct.unpack_from("<IIII", raw, i)
        if team is None or row[2] == team:
            out.append(row[field])
    return out


# ---------------------------------------------------------------- 1. tables
def test_tables():
    assert len(pesdb.read_table(CPK, "Player")) == 26395 * 312
    assert len(pesdb.read_table(CPK, "Team")) == 745 * 1532
    assert len(pesdb.read_table(CPK, "PlayerAssignment")) == 21240 * 16
    # the lead block survives a re-pack byte for byte
    blob = pesdb.table_blob(CPK, "Player")
    t = pesdb.PlayerTable(blob)
    assert t.to_file()[:2048] == blob[:2048]
    assert len(pesdb.PlayerTable(t.to_file()).records) == 26395


# ---------------------------------------------------------------- 2. squads
def test_squads():
    raw = pesdb.read_table(CPK, "PlayerAssignment")
    squads = pesdb.squads(raw)
    assert sum(len(v) for v in squads.values()) == 21240

    single = min(squads, key=lambda t: (len(squads[t]), t))
    assert len(squads[single]) > 0
    for team in (101, single):
        assert squads[team] == scan_rows(raw, 1, team=team), team

    box = sandbox()
    out = cli(box, "--squad", "Arsenal")
    assert "team id 101" in out
    assert f"{len(scan_rows(raw, 1, team=101))} players" in out
    # every listed id resolves back to the same club through the first-row map
    teams = pesdb.player_teams(raw)
    for pid in squads[101]:
        assert teams[pid] == 101, pid


def test_free_agent_still_findable():
    """Review focus 1: a player with no assignment row is findable and exports a blank club."""
    raw = pesdb.read_table(CPK, "PlayerAssignment")
    assigned = set(scan_rows(raw, 1))
    player = pesdb.PlayerTable(pesdb.table_blob(CPK, "Player"))
    free = [i for i in range(len(player.records)) if player.pid(i) not in assigned]
    assert free, "no free agent in the shipped table"
    i = free[0]
    box = sandbox()
    out = cli(box, "--find", str(player.pid(i)))
    assert str(player.pid(i)) in out
    path = os.path.join(box, "ass.csv")
    cli(box, "--export-assignments", path)
    assert str(player.pid(i)) not in {r for r in open(path).read().splitlines()}


# ---------------------------------------------------------------- 3. export
def test_export_assignments():
    box = sandbox()
    path = os.path.join(box, "ass.csv")
    out = cli(box, "--export-assignments", path)
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    assert len(rows) == 21240, len(rows)
    assert list(rows[0]) == ["id", "name", "shirt", "team_id", "club", "league_id"]
    raw = pesdb.read_table(CPK, "PlayerAssignment")
    expected = [(r[1], r[2]) for r in (struct.unpack_from("<IIII", raw, i)
                                       for i in range(0, len(raw), 16)) if r[1]]
    assert [(int(r["id"]), int(r["team_id"])) for r in rows] == expected
    assert "row(s) reference" not in out


def test_export_reports_orphans():
    """Review focus 5: a PA row whose id is not in Player.bin is reported, never reconciled."""
    box = sandbox()
    blob = pesdb.table_blob(CPK, "PlayerAssignment")
    rows = bytearray(pesdb.table_rows(blob))
    struct.pack_into("<I", rows, 4, 999999)          # pid
    struct.pack_into("<I", rows, 8, 12345)           # team id absent from Team.bin
    pesdb.patch_table(box_cpk(box), "PlayerAssignment",
                      blob[:2048] + pesdb.wesys_pack(bytes(rows)))
    out = cli(box, "--export-assignments", os.path.join(box, "ass.csv"))
    assert "1 row(s) reference player ids absent" in out
    rec = [r for r in csv.DictReader(open(os.path.join(box, "ass.csv")))
           if r["id"] == "999999"]
    assert len(rec) == 1 and rec[0]["name"] == "" and rec[0]["club"] == ""
    assert rec[0]["team_id"] == "12345"


# ---------------------------------------------------------------- 4. transfer
def test_transfer():
    box = sandbox()
    raw = pesdb.read_table(CPK, "PlayerAssignment")
    pid = 101520                                    # David Raya: Spain row + Arsenal row
    assert len(pesdb.assignment_rows(raw, pid)) == 2
    out = cli(box, "--transfer", "David Raya", "--to", "Chelsea FC", "--save")
    assert "Arsenal FC -> Chelsea FC" in out
    assert "1 changed" in out
    assert "2 club assignment rows" not in out      # only one club row; the country row is skipped

    after = pesdb.table_rows(pesdb.table_blob(box_cpk(box), "PlayerAssignment"))
    blob = pesdb.table_blob(box_cpk(box), "PlayerAssignment")
    assert len(after) == len(raw)
    assert blob[:2048] == pesdb.table_blob(CPK, "PlayerAssignment")[:2048]
    diffs = [i for i in range(0, len(after), 16) if after[i:i + 16] != raw[i:i + 16]]
    assert len(diffs) == 1, len(diffs)
    assert struct.unpack_from("<II", after, diffs[0] + 4) == (pid, 102)
    # the national-team row is untouched
    assert struct.unpack_from("<I", after, pesdb.assignment_rows(raw, pid)[0] + 8)[0] == 7


def test_transfer_duplicate_rows():
    """Review focus 2: with 2+ club rows the first is edited, and the run says so."""
    box = sandbox()
    blob = pesdb.table_blob(CPK, "PlayerAssignment")
    rows = bytearray(pesdb.table_rows(blob))
    pid = 999901
    for k, team in ((0, 101), (1, 103)):
        off = 4 + k * 16
        struct.pack_into("<I", rows, off, pid)
        struct.pack_into("<I", rows, off + 4, team)
    # give it a Player.bin record so the transfer can find the name
    player = pesdb.PlayerTable(pesdb.table_blob(CPK, "Player"))
    rec = bytearray(pesdb.REC)
    struct.pack_into("<I", rec, 8, pid)
    player.records.append(rec)
    player.set_name(len(player.records) - 1, full="Dup Test", shirt="DUP")
    pesdb.patch_table(box_cpk(box), "PlayerAssignment",
                      blob[:2048] + pesdb.wesys_pack(bytes(rows)))
    pesdb.patch_table(box_cpk(box), "Player", player.to_file())

    out = cli(box, "--transfer", "Dup Test", "--to", "Chelsea FC", "--save")
    assert "2 club assignment rows" in out and "edited the first" in out
    after = pesdb.table_rows(pesdb.table_blob(box_cpk(box), "PlayerAssignment"))
    touched = [struct.unpack_from("<II", after, i + 4) for i in range(0, len(after), 16)
               if struct.unpack_from("<I", after, i + 4)[0] == pid]
    assert touched == [(pid, 102), (pid, 103)], touched


# ---------------------------------------------------------------- 5. blank tables
def test_blank_tables():
    box = sandbox()
    out_dir = os.path.join(box, "blank")
    cli(box, "--clean-tables", out_dir)
    for name in ("Player", "Team", "PlayerAssignment", "Coach"):
        path = os.path.join(out_dir, name + ".bin")
        blob = open(path, "rb").read()
        assert pesdb.table_rows(blob) == b"", name
        assert blob[:2048] == pesdb.table_blob(CPK, name)[:2048], name
        assert len(blob) > 2048


# ---------------------------------------------------------------- 6. append import
def test_append_import():
    box = sandbox()
    pesdb.patch_table(box_cpk(box), "Player", pesdb.blank_table(pesdb.table_blob(CPK, "Player")))
    path = os.path.join(box, "rows.csv")
    long_name = "Long Name " * 7
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "shirt", "Speed", "Age"])
        w.writerow([900001, "Test Player One", "ONE", 99, 20])
        w.writerow([900002, "Test Player Two", "TWO", 88, 25])
        w.writerow([900003, long_name, "THREE", 77, 30])

    # without --append the ids are refused by name and nothing is written
    out = cli(box, "--import", path, "--save")
    assert "skip id 900001: not in table" in out
    assert "0 players changed" in out
    assert len(pesdb.PlayerTable(pesdb.table_blob(box_cpk(box), "Player")).records) == 0

    out = cli(box, "--import", path, "--append", "--save")
    assert "3 players changed" in out
    t = pesdb.PlayerTable(pesdb.table_blob(box_cpk(box), "Player"))
    assert len(t.records) == 3
    assert [(t.pid(i), t.get(i, "Speed"), t.get(i, "Age")) for i in range(3)] == \
           [(900001, 99, 20), (900002, 88, 25), (900003, 77, 30)]
    # Review focus 4: a 61+ byte name is cut at the slot, never spilling into the next one
    assert len(t.name(2).encode()) == 60
    assert t.name(2).startswith("Long Name")
    assert t.shirt(2) == "THREE"


# ---------------------------------------------------------------- 7. copy
def test_copy_tsv():
    box = sandbox()
    out = cli(box, "--copy", "Lionel Messi").strip()
    cells = out.split("\t")
    labels = list(pesdb.FIELD_BY_LABEL)
    assert len(cells) == len(labels), (len(cells), len(labels))
    player = pesdb.PlayerTable(pesdb.table_blob(CPK, "Player"))
    i = next(k for k in range(len(player.records)) if player.name(k) == "Lionel Messi")
    import cli as cli_mod
    for label, cell in zip(labels, cells):
        assert cli_mod.canonical(label) == label
        assert int(cell) == player.get(i, label), label


# ---------------------------------------------------------------- 8. vendor maps
def test_vendor_maps():
    """Tables lifted from the PESDatabase client: names, ids, and eFootball -> PES2021."""
    assert len(maps.abilities("PES2021")) == 25
    assert set(maps.abilities("PES2021")) == {label for label, *_ in pesdb.ATTRS}
    assert len(maps.abilities("EFootball")) == 26
    assert len(maps.PLAYING_STYLES) == 25 and len(set(maps.PLAYING_STYLES)) == 25

    conv = maps.convert_stats({"Offensive Awareness": 90, "Set Piece Taking": 70,
                               "Tackling": 45, "GK Parrying": 60}, "EFootball", "PES2021")
    assert conv["stats"] == {"OffensiveAwareness": 90, "PlaceKicking": 70, "BallWinning": 45}
    assert conv["unmapped"] == ["GK Parrying"]
    back = maps.convert_stats({"PlaceKicking": 70}, "PES2021", "EFootball")
    assert back["stats"] == {"SetPieceTaking": 70} and back["unmapped"] == []

    assert maps.skill_id("Scissors Feint") == 1
    assert maps.skill_id("Chip shot control") == 12        # vendor spells it "Chip Shot Control"
    assert maps.skill_id("Mazing Run") is None             # in our table, not in the vendor list


# ---------------------------------------------------------------- 9. settings
def test_settings():
    box = sandbox()
    # the config file alone (no SPFL_DIR) points the CLI at a game folder
    with open(os.path.join(box, "state", "config.json"), "w") as f:
        json.dump({"game_dir": os.path.join(box, "game"), "max_backups": 2}, f)
    env = dict(os.environ, SPFL_STATE=os.path.join(box, "state"),
               SPFL_CONFIG=os.path.join(box, "state", "config.json"))
    env.pop("SPFL_DIR", None)
    p = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "--squad", "Arsenal"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "team id 101" in p.stdout, p.stdout[:200]

    # max_backups prunes the oldest, newest survive
    bdir = os.path.join(box, "backups")
    os.makedirs(bdir)
    for n in range(5):
        path = os.path.join(bdir, f"Player.{n}.bin")
        open(path, "wb").close()
        os.utime(path, (1_700_000_000 + n, 1_700_000_000 + n))
    assert settings.prune_backups(bdir, 2) == 3
    assert sorted(os.listdir(bdir)) == ["Player.3.bin", "Player.4.bin"]
    assert settings.prune_backups(bdir, 0) == 0      # 0 keeps everything



def test_appearance_table():
    app = pesdb.AppearanceTable(pesdb.table_blob(CPK, "PlayerAppearance"))
    assert len(app.records) == 26395
    i = {app.pid(k): k for k in range(len(app.records))}[7511]
    # Messi's record, checked against the reference client's own decode
    assert (app.get(i, "Skin Colour"), app.get(i, "Head Length"), app.get(i, "Head Width")) == (1, 2, 3)
    assert (app.get(i, "Hair Red"), app.get(i, "Hair Green"), app.get(i, "Hair Blue")) == (5, 4, 3)
    # every field must survive a write + re-read at its maximum
    for label, (bit, width, add) in pesdb.APPEARANCE_BY_LABEL.items():
        app.set(i, label, add + (1 << width) - 1)
    again = pesdb.AppearanceTable(app.to_file())
    for label, (bit, width, add) in pesdb.APPEARANCE_BY_LABEL.items():
        assert again.get(i, label) == add + (1 << width) - 1, label
    assert len(again.records) == len(app.records)
    # the player id field is untouched by the appearance bits
    assert again.pid(i) == 7511


def test_team_and_assignment_tables():
    box = sandbox()
    team = pesdb.TeamTable(pesdb.table_blob(CPK, "Team"))
    assert len(team.records) == 745
    idx = {team.tid(k): k for k in range(len(team.records))}[101]
    assert team.name(idx) == "Arsenal FC"
    team.set_slot(idx, 368, 70, "Arsenal TEST")
    assert "Arsenal TEST" in [team.name(k) for k in range(len(team.records))]

    asg = pesdb.AssignmentTable(pesdb.table_blob(CPK, "PlayerAssignment"))
    rows = [k for k in range(len(asg.records)) if asg.pid(k) == 7511 and asg.team(k) > 99]
    asg.set_team(rows[0], 101)
    assert asg.team(rows[0]) == 101

    pesdb.patch_table(box_cpk(box), "Team", team.to_file())
    pesdb.patch_table(box_cpk(box), "PlayerAssignment", asg.to_file())
    team2 = pesdb.TeamTable(pesdb.table_blob(box_cpk(box), "Team"))
    asg2 = pesdb.AssignmentTable(pesdb.table_blob(box_cpk(box), "PlayerAssignment"))
    assert team2.name({team2.tid(k): k for k in range(len(team2.records))}[101]) == "Arsenal TEST"
    assert len(team2.records) == 745 and len(asg2.records) == 21240
    assert asg2.team(rows[0]) == 101


def test_overall_matches_reference():
    t = pesdb.PlayerTable(pesdb.table_blob(CPK, "Player"))
    i = next(k for k in range(len(t.records)) if t.pid(k) == 7511)
    get = lambda label: t.get(i, label)
    assert pesdb.overall(get, t.get(i, "Registered Position")) == 85   # reference client: 85
    assert 40 <= pesdb.overall(get, 99) <= 99                          # no model for that slot


def test_database_autodiscovery():
    """A fresh install with no config finds the game folder and remembers it."""
    box = sandbox()
    games = os.path.join(box, "games")
    cpk = os.path.join(games, "SP Football Life 2027", "download", "data_s2627.cpk")
    os.makedirs(os.path.dirname(cpk))
    open(cpk, "wb").close()
    assert settings.find_databases([games]) == [cpk]
    assert settings.has_database(os.path.dirname(os.path.dirname(cpk)))
    assert not settings.has_database(os.path.join(box, "nowhere"))

    saved = settings.CONFIG_PATH, settings.SEARCH_ROOTS, os.environ.pop("SPFL_DIR", None)
    try:
        settings.CONFIG_PATH = os.path.join(box, "state", "config.json")
        settings.SEARCH_ROOTS = (games,)
        cfg = {"game_dir": os.path.join(box, "uninstalled"), "backups_dir": "",
               "max_backups": 50, "confirm_save": True}
        assert settings.game_dir(cfg) == os.path.dirname(os.path.dirname(cpk))
        assert json.load(open(settings.CONFIG_PATH))["game_dir"] == settings.game_dir(cfg)
        os.environ["SPFL_DIR"] = os.path.join(box, "game")        # env still wins
        assert settings.game_dir(cfg) == os.path.join(box, "game")
    finally:
        settings.CONFIG_PATH, settings.SEARCH_ROOTS, env = saved
        os.environ.pop("SPFL_DIR", None)
        if env:
            os.environ["SPFL_DIR"] = env


def test_gui_helpers():
    import editor
    # drag_bar mapping: left edge 40, right edge 99, clamps outside
    assert [editor.bar_value(x, 64) for x in (0, 31, 32, 63, 200)] == [40, 69, 70, 99, 99]
    assert editor.bar_value(0, 1) == 40
    # radar group average
    assert editor.group_average(lambda label: {"a": 80, "b": 90}[label], ("a", "b")) == 85.0
    # search status line
    assert editor.search_summary(800, 800) == "800 shown"
    assert editor.search_summary(800, 3210) == "first 800 of 3,210 matches (refine the search)"


def test_close_guard():
    with gui() as app:
        import editor
        assert hasattr(app, "on_close"), "no close handler yet"
        app.dirty_tables.add("Player")
        asked = []
        real = editor.messagebox.askokcancel
        try:
            editor.messagebox.askokcancel = lambda *a, **k: (asked.append(1), False)[1]
            app.on_close()
            assert app.winfo_exists(), "window closed while edits were unsaved"
            assert asked, "close did not ask"
            editor.messagebox.askokcancel = lambda *a, **k: True
            app.on_close()
            try:
                alive = bool(app.winfo_exists())
            except Exception:
                alive = False
            assert not alive, "window survived an approved close"
        finally:
            editor.messagebox.askokcancel = real


def test_row_refresh_after_edit():
    with gui() as app:
        i = 0
        app.index = i
        app.load_form()
        app.update()
        assert app.tree.exists(str(i)), "row 0 not in the first 800 rows"
        app.vars["Age"].set("44")
        app.apply_field("Age")
        app.update()
        values = app.tree.item(str(i), "values")
        assert values[5] == "44", f"age column stale: {values}"
        assert values[4] == str(app.ovr(i)), f"OVR column stale: {values[4]} vs {app.ovr(i)}"


def test_keyboard_shortcuts():
    with gui() as app:
        assert app.bind("<Control-s>"), "Ctrl+S is not bound"
        calls = []
        app.save = lambda: calls.append("save")
        app.on_save_key()
        assert calls == ["save"], "Ctrl+S did not save"
        app.q.set("messi")
        app.on_clear_key()
        app.update()
        assert app.q.get() == "", "Escape did not clear the search"


def test_button_bar_fits():
    """Save/Revert/Reload stay on screen at every width: the header wraps instead of squeezing.

    A tiling compositor hands out less than the requested width, and pack() answers by shrinking
    the last-packed widget — the button bar — to nothing while still reporting it as mapped, so
    the check is a real width, not `winfo_ismapped`.
    """
    with gui() as app:
        bar = getattr(app, "_button_bar", None)
        assert bar is not None, "button bar not exposed for layout checks"
        header = app._header
        app.lbl_dirty.configure(text="● unsaved")
        saved = header.pack_info()
        header.pack_configure(fill="none", side="top", anchor="nw")
        header.pack_propagate(False)          # an explicit width only counts with propagate off
        try:
            for width in (1500, 1180, 940, 780, 620, 460, 360, 240, 200):
                header.configure(width=width, height=int(120 * app.ui))
                app.update()
                app.update_idletasks()
                for b in app._bar_buttons:
                    name = b.cget("text")
                    left = b.winfo_rootx() - header.winfo_rootx()
                    right = left + b.winfo_width()
                    assert b.winfo_ismapped(), f"{name} hidden at {width}px"
                    assert b.winfo_width() > 40, f"{name} squeezed to {b.winfo_width()}px at {width}px"
                    assert left >= -1 and right <= header.winfo_width() + 1, \
                        f"{name} outside the header at {width}px: {left}..{right} of {header.winfo_width()}"
                    # a button packed -in a frame it does not belong to still reports a
                    # geometry, so ask which widget is really under the button's own centre
                    under = app.winfo_containing(b.winfo_rootx() + b.winfo_width() // 2,
                                                 b.winfo_rooty() + b.winfo_height() // 2)
                    assert under is b, f"{name} is not painted (found {under}) at {width}px"
        finally:
            header.configure(width=0)
            header.pack_propagate(True)
            header.pack_configure(**saved)
        app.update()
        right = max(b.winfo_rootx() + b.winfo_width() for b in app._bar_buttons)
        assert right <= bar.winfo_rootx() + bar.winfo_width() + 1, \
            f"last button ends at {right}, bar ends at {bar.winfo_rootx() + bar.winfo_width()}"
        for b in app._bar_buttons:
            assert b.winfo_width() > 40, b.cget("text")
        # the bar row must sit inside the header it was moved into, not below its visible edge
        assert bar.winfo_rooty() + bar.winfo_height() <= header.winfo_rooty() + header.winfo_height() + 1, \
            "button row is outside the header"


def test_compact_layout_fits_without_scrolling():
    """Both form columns fit the size the app asks for (1460x920 at ui 1.0): nothing scrolls.

    A tiling window manager gives a mapped window its own size, so the layout is planned at
    the design width and height (`_layout_one_page` takes both) and the content's requested
    size is compared with the pane it was planned into. When the window does honour the size,
    the mapped geometry is checked too — a `ttk.Scrollbar` may only be off screen then.
    """
    if not os.environ.get("DISPLAY"):
        raise FileNotFoundError("no DISPLAY")
    import tkinter as tk
    from tkinter import ttk
    import editor as editor_mod
    box = tempfile.mkdtemp(prefix="pesforge-fit-")
    saved = settings.CONFIG_PATH
    settings.CONFIG_PATH = os.path.join(box, "config.json")
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"game_dir": GAME, "photos": False}, f)
    app = editor_mod.Editor()
    app.attributes("-alpha", 0.0)
    app.update()
    try:
        app.q.set("messi")
        app.populate("messi", select_first=True)
        for _ in range(8):
            app.update()
            app.update_idletasks()
        body = app._one_page_body
        # the window chrome at the design size: one header row, the footer, the 8px padding
        design_w = int(1460 * app.ui) - 2 * int(12 * app.ui)
        design_h = int(920 * app.ui) - int(90 * app.ui)
        # cancel a queued fold first: the window on a tiling compositor is narrower than the
        # planned layout, and a 120 ms rebuild would replace the plan before it is measured
        if app._one_page_job is not None:
            app.after_cancel(app._one_page_job)
            app._one_page_job = None
        app._layout_one_page(body, wide=True, width=design_w, height=design_h)
        for _ in range(8):
            app.update_idletasks()
        panes = []
        for frame in sorted(body.grid_slaves(row=0), key=lambda w: int(w.grid_info()["column"])):
            # the form pane canvases hold their content frame; a scroll indicator holds nothing
            canvases = [c for c in frame.winfo_children()
                        if isinstance(c, tk.Canvas) and c.winfo_children()]
            panes.extend(canvases)
        assert len(panes) == 2, f"expected two form panes, found {len(panes)}"
        plan = app._plan
        for idx, canvas in enumerate(panes):
            inner = canvas.winfo_children()[0]
            planned = plan["mid"] if idx == 0 else plan["right"]
            assert inner.winfo_reqheight() <= plan["height"] + 1, (
                f"column {idx} content {inner.winfo_reqheight()}px does not fit the planned "
                f"{plan['height']}px pane (plan {plan}, panes {len(panes)}, "
                f"heights {[p.winfo_children()[0].winfo_reqheight() for p in panes]})")
            for section in inner.winfo_children():
                assert section.winfo_reqwidth() <= planned + 1, (
                    f"section {section.winfo_class()} at column {idx} needs "
                    f"{section.winfo_reqwidth()}px inside a {planned}px pane")
        # the same check on real geometry, when the window manager lets the window be the size
        # the app asked for (Xvfb, a floating window); a tiled window is sized by the WM
        body_w = body.winfo_width()
        if abs(body_w - design_w) <= 4:
            for name in app._walk(app):
                if isinstance(name, ttk.Scrollbar):
                    assert not name.winfo_ismapped(), "a scrollbar is showing although nothing scrolls"
    finally:
        app.destroy()
        settings.CONFIG_PATH = saved


def test_photo_toggle_default():
    assert "photos" in settings.DEFAULTS, "DEFAULTS has no photos key"
    assert settings.DEFAULTS["photos"] is True
    box = tempfile.mkdtemp(prefix="pesforge-cfg-")
    path = settings.CONFIG_PATH
    try:
        settings.CONFIG_PATH = os.path.join(box, "config.json")
        cfg = settings.load()
        cfg["photos"] = False
        settings.save(cfg)
        assert settings.load()["photos"] is False
    finally:
        settings.CONFIG_PATH = path


def test_no_false_dirty():
    """Tabbing through a field fires FocusOut -> apply_*; an unchanged value must not dirty."""
    with gui() as app:
        app.index = 0
        app.load_form()
        app.update()
        for label in ("Offensive Awareness", "Age"):
            app.vars[label].set(str(app.table.get(0, label)))
            app.apply_field(label)
            assert not app.dirty_tables, f"{label} marked dirty without a change: {app.dirty_tables}"
        app.apply_style()
        app.apply_foot()
        app.apply_position("CF")
        assert not app.dirty_tables, f"no-op appliers dirtied the player: {app.dirty_tables}"


def test_settings_migration():
    """First run as PESForge takes the old spfl27-editor config and state with it."""
    box = tempfile.mkdtemp(prefix="pesforge-mig-")
    old_cfg = os.path.join(box, "config", "spfl27-editor")
    old_state = os.path.join(box, "data", "spfl27-editor")
    os.makedirs(old_cfg)
    os.makedirs(os.path.join(old_state, "portraits"))
    with open(os.path.join(old_cfg, "config.json"), "w") as f:
        f.write('{"game_dir": "/games/x"}')
    with open(os.path.join(old_state, "portraits", "7511.png"), "w") as f:
        f.write("x")
    env = dict(os.environ, HOME=box, XDG_CONFIG_HOME=os.path.join(box, "config"),
               XDG_DATA_HOME=os.path.join(box, "data"))
    env.pop("SPFL_CONFIG", None)
    env.pop("SPFL_STATE", None)
    out = subprocess.run([sys.executable, "-c",
                          "import settings; print(settings.CONFIG_PATH); print(settings.state_dir())"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out[0].endswith("/config/pesforge/config.json"), out
    assert out[1].endswith("/data/pesforge"), out
    with open(out[0]) as f:
        assert json.load(f)["game_dir"] == "/games/x"
    assert os.path.exists(os.path.join(out[1], "portraits", "7511.png")), out[1]


def test_country_names():
    assert pesdb.country_name(144) == "Argentina"
    assert pesdb.country_name(204) == "England"
    assert pesdb.country_name(99999) == ""
    assert len(pesdb.COUNTRIES) >= 200


def test_country_filter():
    with gui() as app:
        app.country_filter.set("Wales")
        app.populate("")
        app.update()
        rows = app.tree.get_children()
        assert 0 < len(rows) < 800, f"wales matched {len(rows)}"
        for item in rows:
            assert pesdb.country_name(app.table.get(int(item), "Nationality")) == "Wales", item
        welsh = len(rows)
        app.country_filter.set("")
        app.populate("")
        app.update()
        assert len(app.tree.get_children()) == 800 > welsh, "clearing the filter did not widen"
        app.country_filter.set("England")
        app.club_filter.set("Portsmouth FC")
        app.populate("")
        app.update()
        rows = app.tree.get_children()
        assert rows, "England + Portsmouth FC matched nothing"
        for item in rows:
            i = int(item)
            assert pesdb.country_name(app.table.get(i, "Nationality")) == "England"
            assert app.clubs.get(app.player_team.get(app.table.pid(i), -1)) == "Portsmouth FC"


def test_undo_redo():
    with gui() as app:
        app.index = 0
        app.load_form()
        app.update()
        start = app.table.get(0, "Speed")
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        assert app.table.get(0, "Speed") == 99
        app.undo()
        app.update()
        assert app.table.get(0, "Speed") == start, "undo did not restore the value"
        assert app.vars["Speed"].get() == str(start), "the form kept the undone value"
        app.redo()
        app.update()
        assert app.table.get(0, "Speed") == 99, "redo did not reapply"
        app.undo(); app.undo()          # empty stack must not raise
        assert app.table.get(0, "Speed") == start


def test_bulk_edit():
    with gui() as app:
        assert str(app.tree.cget("selectmode")) == "extended", "tree must allow multi-select"
        app.tree.selection_set(("0", "1", "2"))
        before = [app.table.get(i, "Aggression") for i in (0, 1, 2)]
        app.bulk_apply("Aggression", "add", 5)
        after = [app.table.get(i, "Aggression") for i in (0, 1, 2)]
        assert all(min(99, b + 5) == a for b, a in zip(before, after)), (before, after)
        app.undo()
        assert [app.table.get(i, "Aggression") for i in (0, 1, 2)] == before


def test_compare_players():
    with gui() as app:
        app.tree.selection_set(("0", "1"))
        dlg = app.compare_dialog()
        assert dlg is not None, "compare needs exactly two selected players"
        rows = app.compare_rows(0, 1)
        assert len(rows) == len(pesdb.ATTRS), "one row per ability"
        name_a, val_a, val_b = rows[0]
        assert val_a == app.table.get(0, name_a) and val_b == app.table.get(1, name_a)
        dlg.destroy()


def test_changed_only_view():
    with gui() as app:
        app.index = 0
        app.load_form()
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        app.changed_only.set(True)
        app.populate("")
        app.update()
        shown = {int(i) for i in app.tree.get_children()}
        assert shown == {0}, shown
        app.changed_only.set(False)
        app.populate("")
        app.update()
        assert len(app.tree.get_children()) == 800


def test_position_and_free_agent_filters():
    with gui() as app:
        app.position_filter.set("GK")
        app.populate("")
        app.update()
        rows = app.tree.get_children()
        assert rows, "no goalkeepers"
        for item in rows:
            assert pesdb.POS_NAMES[app.table.get(int(item), "Registered Position")] == "GK"
        app.position_filter.set("")
        app.free_agents_only.set(True)
        app.populate("")
        app.update()
        rows = app.tree.get_children()
        assert rows and all(app.player_team.get(app.table.pid(int(i))) is None for i in rows)


def test_csv_round_trip():
    with gui() as app:
        path = os.path.join(tempfile.mkdtemp(prefix="pesforge-csv-"), "roster.csv")
        n = app.export_roster(path)
        assert n > 10000 and os.path.getsize(path) > 100000, (n, path)
        app.index = 0
        app.load_form()
        old = app.table.get(0, "Speed")
        with open(path, encoding="utf-8") as f:
            rows = f.read().splitlines()
        header = rows[0].split(",")
        speed_col = header.index("Speed")
        line = rows[1].split(",")
        line[speed_col] = "99"
        rows[1] = ",".join(line)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        touched = app.import_roster(path)
        assert touched >= 1
        assert app.table.get(0, "Speed") == 99
        app.index = None                    # importing with nothing selected must not raise
        assert app.import_roster(path) >= 1


def test_portrait_fits_slot():
    """A portrait is never clipped or stretched: the slot fits the image, oversized ones are halved."""
    import editor
    with gui() as app:
        side = int(editor.PHOTO_SIDE * app.ui)   # the slot is DPI-scaled: 128 at 96 dpi, 256 at 192
        path = os.path.join(tempfile.mkdtemp(), "portrait.ppm")
        with open(path, "wb") as f:              # Tk reads binary P6 only
            f.write(b"P6\n%d %d\n255\n" % (side * 2, side * 2)
                    + bytes([200, 120, 90]) * (side * side * 4))
        app._show_portrait(path)
        assert (app.photo_img.width(), app.photo_img.height()) == (side, side), \
            (app.photo_img.width(), app.photo_img.height())
        app.update()
        # a photo is shown at its full size or not at all: never squashed into a narrow slot
        if app.photo.winfo_ismapped():
            assert app.photo.winfo_width() >= side and app.photo.winfo_height() >= side, \
                (app.photo.winfo_width(), app.photo.winfo_height())
        shutil.rmtree(os.path.dirname(path))

def test_colour_tokens():
    """Colours live in theme.py only, and every palette clears the contrast it promises."""
    import theme
    text = open(os.path.join(ROOT, "editor.py"), encoding="utf-8").read()
    stray = [f"{n}: {ln.strip()}" for n, ln in enumerate(text.splitlines(), 1)
             if re.search(r'"#[0-9a-fA-F]{6}"', ln)]
    assert not stray, f"colours outside the palette module: {stray}"
    for name, _label in theme.CHOICES:
        t = theme.tokens(name)
        assert theme.contrast(t["fg"], t["bg"]) >= 12, name
        assert theme.contrast(t["dim"], t["bg"]) >= 4.5, name
        assert theme.contrast(t["hint"], t["bg"]) >= 4.4, name
        assert theme.contrast(t["accent"], t["bg"]) >= 4.5, name
        assert theme.contrast(t["bar_low"], t["panel2"]) >= 3.0, name
        assert theme.contrast(t["ghost"], t["bg"]) >= 3.0, name
        assert theme.contrast(t["ink"], t["accent"]) >= 4.5, name
    assert theme.choice({"theme": "nope"}) == theme.DEFAULT
    assert theme.choice({"theme": "ice"}) == "ice"
    # theme.py may only carry hexes as base palette entries
    for n, ln in enumerate(open(os.path.join(ROOT, "theme.py"), encoding="utf-8"), 1):
        if re.search(r'"#[0-9a-fA-F]{6}"', ln):
            assert re.match(r'(\s+"[a-z0-9_]+": "#[0-9a-fA-F]{6}"'
                            r'|[A-Z]+(, [A-Z]+)? = "#[0-9a-fA-F]{6}")',
                            ln), f"theme.py:{n} {ln!r}"


def test_field_clamp():
    """A typed or bulk value must stop at 99 for abilities, not at the bit field's 103."""
    with gui() as app:
        app.index = 0
        app.load_form()
        app.vars["Speed"].set("103")
        app.apply_field("Speed")
        assert app.table.get(0, "Speed") == 99, app.table.get(0, "Speed")
        app.vars["Age"].set("90")
        app.apply_field("Age")
        assert app.table.get(0, "Age") == 78, app.table.get(0, "Age")   # 15 + 2**6 - 1
        app.tree.selection_set(("0",))
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        app.bulk_apply("Speed", "add", 40)
        assert app.table.get(0, "Speed") == 99, app.table.get(0, "Speed")


def test_career_fields():
    """The career block of the vendor PES 2021 schema: decoded, writable, and sane in the data."""
    t = pesdb.PlayerTable(pesdb.table_blob(CPK, "Player"))
    for label, bit, width, add in pesdb.CAREER:
        assert pesdb.FIELD_BY_LABEL[label] == (bit, width, add)
    i = next(k for k in range(len(t.records)) if t.pid(k) == 7511)
    assert t.get(i, "Contract Until") == pesdb.bit_get(t.records[i], 96, 27)
    t.set(i, "Contract Until", 20270630)
    t.set(i, "Market Value", 1234567)
    t.set(i, "National Caps", 198)
    t.set(i, "Youth Club", 101)
    t.set(i, "Owner Club", 102)
    t.set(i, "Legend", 1)
    again = pesdb.PlayerTable(t.to_file())
    assert (again.get(i, "Contract Until"), again.get(i, "Market Value"),
            again.get(i, "National Caps"), again.get(i, "Youth Club"),
            again.get(i, "Owner Club"), again.get(i, "Legend")) == \
        (20270630, 1234567, 198, 101, 102, 1)
    assert again.pid(i) == 7511 and again.name(i) == t.name(i)   # neighbours untouched
    # the shipped database keeps contract dates in YYYYMMDD form; caps fit their byte
    dates = {t.get(k, "Contract Until") for k in range(0, 4000)}
    dates.discard(0)
    for d in dates:
        assert 20000000 <= d <= 20999999, d
        assert d % 100 <= 31, d
    assert max(t.get(k, "National Caps") for k in range(len(t.records))) <= 255


def test_display_addends():
    """Form, the weak foot, injury and reputation/attitude sliders read 1-based, like the game."""
    t = pesdb.PlayerTable(pesdb.table_blob(CPK, "Player"))
    for label in ("Form", "Weak Foot Usage", "Weak Foot Accuracy", "Injury Resistance",
                  "Reputation", "Playing Attitude"):
        assert pesdb.FIELD_BY_LABEL[label][2] == 1, label
    bit = pesdb.FIELD_BY_LABEL["Form"][0]
    pesdb.bit_set(t.records[0], bit, 3, 0, 0)
    assert t.get(0, "Form") == 1                       # stored 0 is the lowest the game shows
    t.set(0, "Form", 8)
    assert pesdb.bit_get(t.records[0], bit, 3) == 7 and t.get(0, "Form") == 8
    t.set(0, "Form", 1)
    assert pesdb.bit_get(t.records[0], bit, 3) == 0
    assert pesdb.bit_get(t.records[0], 451, 3) <= 7    # reputation raw fits its 3 bits


def test_coach_table():
    """Coach.bin: 100-byte records, 980 coaches; name, Japanese name, country, adaptability."""
    co = pesdb.CoachTable(pesdb.table_blob(CPK, "Coach"))
    assert len(co.records) == 980 and len(pesdb.read_table(CPK, "Coach")) == 980 * 100
    ids = [co.cid(i) for i in range(len(co.records))]
    assert len(set(ids)) == len(ids), "coach ids repeat"
    i = next(k for k in range(len(co.records)) if co.name(k) == "Mikel Arteta")
    assert co.japanese(i) and 40 <= co.adaptability(i) <= 99
    co.set_name(i, "Test Arteta")
    co.set_japanese(i, "テスト監督")
    co.set_country(i, 42)
    co.set_adaptability(i, 90)
    again = pesdb.CoachTable(co.to_file())
    assert (again.name(i), again.japanese(i), again.country(i), again.adaptability(i)) == \
        ("Test Arteta", "テスト監督", 42, 90)
    assert again.cid(i) == co.cid(i) and len(again.records) == 980


def test_team_coach_and_stadium_links():
    """Team.bin +0 is the coach id and +60 the stadium id; every link resolves."""
    team = pesdb.TeamTable(pesdb.table_blob(CPK, "Team"))
    coaches = pesdb.CoachTable(pesdb.table_blob(CPK, "Coach"))
    coach_ids = {coaches.cid(k) for k in range(len(coaches.records))}
    stadiums = pesdb.stadium_names(pesdb.read_table(CPK, "Stadium"))
    assert len(stadiums) == 37 and stadiums[2] == "Spotify Camp Nou"
    for i in range(len(team.records)):
        assert team.coach_id(i) in coach_ids or team.coach_id(i) == 0, team.name(i)
        assert team.stadium_id(i) in stadiums or team.stadium_id(i) == 0, team.name(i)
    idx = {team.tid(i): i for i in range(len(team.records))}[108]          # FC Barcelona
    assert team.name(idx) == "FC Barcelona"
    cid = team.coach_id(idx)
    assert coaches.name(next(k for k in range(len(coaches.records)) if coaches.cid(k) == cid))
    assert stadiums[team.stadium_id(idx)] == "Spotify Camp Nou"
    team.set_coach(idx, 2)
    team.set_stadium(idx, 5)
    again = pesdb.TeamTable(team.to_file())
    assert (again.coach_id(idx), again.stadium_id(idx), again.name(idx)) == \
        (2, 5, "FC Barcelona")


def test_squad_numbers():
    """PlayerAssignment +12 is the number the game shows, stored one below it."""
    asg = pesdb.AssignmentTable(pesdb.table_blob(CPK, "PlayerAssignment"))
    rows = [j for j in range(len(asg.records)) if asg.pid(j) == 7511 and asg.team(j) > 99]
    assert len(rows) == 1
    assert asg.shirt_number(rows[0]) == 10                  # Messi at Inter Miami
    asg.set_shirt_number(rows[0], 30)
    again = pesdb.AssignmentTable(asg.to_file())
    assert again.shirt_number(rows[0]) == 30 and again.pid(rows[0]) == 7511
    asg.set_shirt_number(rows[0], 0)
    assert asg.shirt_number(rows[0]) == 1                   # clamped into 1..99
    asg.set_shirt_number(rows[0], 200)
    assert asg.shirt_number(rows[0]) == 99


def test_cli_coach_stadium_shirt():
    """--coaches, --club-coach, --stadium and --shirt write through to the CPK."""
    box = sandbox()
    out = cli(box, "--coaches", "arteta")
    assert "Mikel Arteta" in out and "Arsenal FC" in out, out
    out = cli(box, "--club-coach", "Arsenal FC", "Hansi Flick", "--save")
    assert "-> 102080" in out and "re-read coach id 102080" in out, out
    out = cli(box, "--club-coach", "Arsenal FC", "Hansi Flick")
    assert "nothing changed" in out, out
    out = cli(box, "--stadium", "Arsenal FC", "5", "--save")
    assert "Wembley" in out and "re-read stadium id 5" in out, out
    out = cli(box, "--shirt", "Lionel Messi", "30", "--save")
    assert "shirt [10] -> 30" in out and "re-read shirt 30" in out, out
    team = pesdb.TeamTable(pesdb.table_blob(box_cpk(box), "Team"))
    idx = {team.tid(i): i for i in range(len(team.records))}[101]
    assert (team.stadium_id(idx), team.coach_id(idx)) == (5, 102080)
    asg = pesdb.AssignmentTable(pesdb.table_blob(box_cpk(box), "PlayerAssignment"))
    assert [asg.shirt_number(j) for j in range(len(asg.records))
            if asg.pid(j) == 7511 and asg.team(j) > 99] == [30]
    out = cli(box, "--export", os.path.join(box, "roster.csv"))
    header = open(os.path.join(box, "roster.csv"), encoding="utf-8").readline()
    for label in ("Contract Until", "Market Value", "National Caps", "Ballon d'Or"):
        assert label in header, label


def test_new_tables_patch_into_cpk():
    """Coach.bin and PlayerAppearance.bin patch into the CPK exactly like the older tables."""
    box = sandbox()
    co = pesdb.CoachTable(pesdb.table_blob(box_cpk(box), "Coach"))
    i = next(k for k in range(len(co.records)) if co.name(k) == "Hansi Flick")
    co.set_adaptability(i, 88)
    pesdb.patch_table(box_cpk(box), "Coach", co.to_file())
    again = pesdb.CoachTable(pesdb.table_blob(box_cpk(box), "Coach"))
    assert again.adaptability(i) == 88 and len(again.records) == 980

    app = pesdb.AppearanceTable(pesdb.table_blob(box_cpk(box), "PlayerAppearance"))
    app.set(276, "Skin Colour", 5)
    pesdb.patch_table(box_cpk(box), "PlayerAppearance", app.to_file())
    again = pesdb.AppearanceTable(pesdb.table_blob(box_cpk(box), "PlayerAppearance"))
    assert again.get(276, "Skin Colour") == 5 and len(again.records) == 26395
    assert again.pid(276) == 7511


def test_blank_career_row_is_hidden():
    """A career field the database leaves empty everywhere collapses; the value stays reachable."""
    with gui() as app:
        app.update()
        assert "Owner Club" in app.vars, "the value must stay settable even when its row is hidden"
        assert app._field_widgets["Owner Club"].winfo_manager() == "", "the dead row should be hidden"
        assert app._field_widgets["Youth Club"].winfo_manager() == "grid", "a populated row stays"
        assert app._field_labels["Owner Club"].winfo_manager() == "", "its label too"
        # and the rows after it move up: the grid stays dense, no hole where the row was
        g = app._career_grid
        cells = []
        for label in g["labels"]:
            info = app._field_widgets[label].grid_info()
            if info:
                cells.append((int(info["row"]), int(info["column"]) // 2))
        assert sorted(cells) == [divmod(i, g["cols"]) for i in range(len(cells))], \
            f"career rows leave a hole: {sorted(cells)}"
        assert len(cells) == len(g["labels"]) - 1, "Owner Club is the only hidden one"
        note = app.career_note.cget("text")
        assert "Owner Club" in note and "hidden" in note, note
        assert "Type a club name" not in note, "nothing to type into if the row is hidden"


def test_theme_switch_repaints_bars():
    """A palette switch rebuilds the form: the bars must come back drawn, in the new palette."""
    import editor
    import theme
    with gui() as app:
        app.q.set("gerrard")
        app.populate("gerrard", select_first=True)
        for _ in range(8):
            app.update()
            app.update_idletasks()
        try:
            app._apply_theme("ice")
            for _ in range(10):
                app.update()
                app.update_idletasks()
            bar = app.vars["Speed::bar"]
            ids = bar.find_all()
            assert len(ids) == 2, f"track + fill expected, got {ids}"
            track = bar.coords(ids[0])
            assert track[2] >= bar.winfo_width() - 1, \
                f"bar drawn {track[2]}px wide in a {bar.winfo_width()}px canvas: {track}"
            assert bar.itemcget(ids[0], "fill").lower() == theme.tokens("ice")["bar"].lower()
            fill = bar.itemcget(ids[1], "fill").lower()
            assert fill in (editor.C_ACCENT.lower(), editor.C_ACCENT_LO.lower(),
                            editor.C_BAR_LOW.lower()), fill
        finally:
            app._apply_theme(theme.DEFAULT)          # leave the process on the default palette
            app.update()


def test_career_and_appearance_ui():
    """The new career fields, coaches window and appearance window work on a live Editor."""
    with gui() as app:
        app.index = 0
        app.load_form()
        for label in ("Contract Until", "Loan Until", "Market Value", "National Caps",
                      "Reputation", "Playing Attitude", "Legend", "Ballon d'Or"):
            assert label in app.vars, label
        app.vars["Contract Until"].set("20270630")
        app.apply_field("Contract Until")
        assert app.table.get(0, "Contract Until") == 20270630
        app.vars["Form"].set("8")
        app.apply_field("Form")
        assert app.table.get(0, "Form") == 8
        app.vars["Form"].set("0")                     # clamped to the lowest the game shows
        app.apply_field("Form")
        assert app.table.get(0, "Form") == 1

        app.open_coaches()
        win = app._coach_win
        win.update()
        assert len(win.tree.get_children()) == 980
        win.q.set("arteta")
        win.fill()
        assert len(win.tree.get_children()) == 1
        row = win.tree.get_children()[0]
        win.tree.selection_set(row)
        win.pick()
        assert win.vars["Name"].get() == "Mikel Arteta"
        win.vars["Adaptability"].set("91")
        win.apply()
        assert app.coach_table.adaptability(int(row)) == 91
        assert "Coach" in app.dirty_tables
        win.destroy()

        app.open_appearance()
        aware = app._app_win
        aware.update()
        assert aware.tree.exists("Skin Colour")
        aware.tree.selection_set("Skin Colour")
        aware.pick()
        aware.value.set("4")
        aware.apply()
        assert app.appearance_table.get(0, "Skin Colour") == 4
        assert "PlayerAppearance" in app.dirty_tables
        aware.destroy()

        app.index = 0
        app.load_form()
        assert app.shirt_var.get() == "—" or app.shirt_var.get().isdigit()
        app.status("")                                 # the club links must not have thrown


def test_form_columns_and_club_refs():
    """One career grid with even columns; club-id fields read and take club names."""
    if not os.environ.get("DISPLAY"):
        raise FileNotFoundError("no DISPLAY")
    import tkinter as tk
    import editor as editor_mod
    box = tempfile.mkdtemp(prefix="pesforge-cols-")
    saved = settings.CONFIG_PATH
    settings.CONFIG_PATH = os.path.join(box, "config.json")
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"game_dir": GAME, "photos": False}, f)
    app = editor_mod.Editor()
    app.attributes("-alpha", 0.0)
    app.update()
    try:
        app.q.set("messi")
        app.populate("messi", select_first=True)
        for _ in range(6):
            app.update()
            app.update_idletasks()

        # the career block is one grid, and every grid column shares a single left edge
        secs = []

        def walk(w):
            for c in w.winfo_children():
                if getattr(c, "section_key", None) == "career":
                    secs.append(c)
                walk(c)

        walk(app)
        assert secs, "no career section"
        grids = [g for s in secs for g in s.body.winfo_children() if g.grid_slaves()]
        assert len(grids) == 1, f"career block split across {len(grids)} grids"
        cols = {}
        for w in grids[0].grid_slaves():
            cols.setdefault(int(w.grid_info()["column"]), []).append(w)
        for col, ws in cols.items():
            xs = {w.winfo_rootx() for w in ws}
            assert len(xs) == 1, f"grid column {col} staggered: {sorted(xs)}"

        # club-id fields show the club name and take one by name or id
        app.index = 0
        app.load_form()
        name = app.clubs.get(app.table.get(0, "Youth Club"), "")
        if name:
            assert name in app.vars["Youth Club"].get()
        app.vars["Youth Club"].set("127  Bayern München")
        app.apply_club_ref("Youth Club")
        assert app.table.get(0, "Youth Club") == 127
        assert "Bayern" in app.vars["Youth Club"].get()
        app.vars["Youth Club"].set("Manchester United")
        app.apply_club_ref("Youth Club")
        assert app.table.get(0, "Youth Club") == 100
        app.vars["Youth Club"].set("not a club at all")
        app.apply_club_ref("Youth Club")
        assert app.table.get(0, "Youth Club") == 100      # nothing written, no crash
    finally:
        try:
            app.destroy()
        except tk.TclError:
            pass
        settings.CONFIG_PATH = saved


def test_header_chips_and_country_filter():
    """Header search/actions, the card chips and the country type-ahead all work."""
    with gui() as app:
        # search and the action buttons live in the header, not in the list pane
        app.update_idletasks()          # the header may still be re-wrapping from the map
        for w in (app.search_entry, app._button_bar):
            assert str(w).startswith(str(app._header)), "header widget is not in the header"
        # A tiling compositor can hand the test window a tile shorter than the header itself;
        # the geometry check only means something when the header got its requested height.
        if app._header.winfo_height() >= app._header.winfo_reqheight() - 1:
            for w in (app.search_entry, app._button_bar):
                r_top = w.winfo_rooty()
                assert app._header.winfo_rooty() <= r_top, "widget sits above the header"
                assert r_top + w.winfo_height() <= \
                    app._header.winfo_rooty() + app._header.winfo_height() + 1, \
                    f"widget falls below the header: {w} {r_top}+{w.winfo_height()} vs " \
                    f"{app._header.winfo_rooty()}+{app._header.winfo_height()}"
        names = [b.cget("text") for b in app._bar_buttons]
        for want in ("Save to game", "Undo", "Redo", "Discard", "Menu"):
            assert any(want in n for n in names), f"{want!r} missing from {names}"

        app.index = 0
        app.load_form()
        app.update_idletasks()

        # the card shows the rating and the value chips (pills; read through the Pill API)
        import editor as editor_mod
        assert app.ovr_big.cget("text").isdigit(), app.ovr_big.cget("text")
        assert app._card_chips["Style"].get_text() not in ("", "—")
        assert app._card_chips["Height"].get_text().endswith("cm")
        assert isinstance(app._skill_chips["Trickster"], editor_mod.Pill)

        # the search hint yields to the caret: hidden while focused, back when idle and empty
        app.q.set("")
        app._search_focus_in()
        app.update_idletasks()
        assert not app._search_hint.place_info(), "hint shows over the focused box"
        app._search_focus_out()
        app.update_idletasks()
        assert app._search_hint.place_info(), "hint missing while the box is empty and idle"

        # an edit repaints the card chips and the meta line, not only the form field
        age = int(app.vars["Age"].get())
        app.vars["Age"].set(str(age + 1))
        app.apply_field("Age")
        assert app._card_chips["Age"].get_text() == f"age {age + 1}", \
            app._card_chips["Age"].get_text()
        app.undo()
        app.update_idletasks()
        assert app._card_chips["Age"].get_text() == f"age {age}", \
            app._card_chips["Age"].get_text()
        before_meta = app.lbl_sub.cget("text")
        app.cycle_position("GK")                # none -> can play: the meta list gains GK
        assert app.lbl_sub.cget("text") != before_meta
        assert "GK" in app.lbl_sub.cget("text")
        app.cycle_position("GK")
        app.cycle_position("GK")
        assert app.lbl_sub.cget("text") == before_meta

        # a position chip cycles none -> can play -> natural and writes to the table
        before = app.vars["Pos GK"].get()
        app.cycle_position("GK")
        after = app.vars["Pos GK"].get()
        assert after != before, "position chip did not cycle"
        assert app.table.get(0, "Pos GK") == ["none", "can play", "natural"].index(after)
        app.cycle_position("GK")
        app.cycle_position("GK")                       # back to the stored value
        assert app.vars["Pos GK"].get() == before

        # a skill chip toggles and writes to the table
        v = app.vars["Skill Trickster"]
        was = v.get()
        app.toggle_skill("Trickster")
        assert v.get() != was
        assert app.table.get(0, "Skill Trickster") == v.get()
        app.toggle_skill("Trickster")
        assert v.get() == was

        # club pickers offer clubs only — team ids under 100 are national teams
        vals = app._club_ref_values()
        assert vals, "club list empty"
        assert all(int(s.split("  ", 1)[0]) >= 100 for s in vals), vals[:4]
        assert "Ireland" not in app._club_names, "a national team is offered as a club"
        app.vars["Owner Club"].set("Ireland")
        app.apply_club_ref("Owner Club")
        assert app.table.get(0, "Owner Club") == 0, "a national team was written as a club"

        # the country box narrows as you type and Enter accepts the single fit
        full = len(app._country_names)
        app.country_filter.set("Engl")
        app._filter_country_values(type("E", (), {"keysym": "l"})())
        values = [str(x) for x in app.tk.splitlist(app.cb_country.cget("values"))]
        assert "England" in values, values[:8]
        assert len(values) < full, "dropdown was not narrowed"
        app.apply_country_filter()
        assert app.country_filter.get() == "England"
        app.country_filter.set("")
        app.apply_country_filter()                     # leave the filter empty again


def test_theme_module():
    import theme
    assert theme.choice({}) == theme.DEFAULT
    assert dict(theme.CHOICES)["ice"] == "Ice"
    assert set(theme.tokens("crimson")) == set(theme.TOKEN_NAMES)
    assert "theme" in settings.DEFAULTS and settings.DEFAULTS["theme"] in theme.BASE
    light = theme.toward(theme.BASE["amber"]["dim"], theme.BASE["amber"]["bg"], 4.5)
    assert theme.contrast(light, theme.BASE["amber"]["bg"]) <= 4.6


def test_change_ledger():
    """Every edit is listed in the ledger, and the field it belongs to is tinted."""
    import editor as editor_mod
    with gui() as app:
        app.index = 0
        app.load_form()
        app.update()
        start = app.table.get(0, "Speed")
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        key = ("Player", app.table.pid(0), "Speed")
        assert key in app._changes, "the edit is not in the change ledger"
        entry = app._changes[key]
        assert entry["base"] == start and entry["new"] == 99, entry
        assert "change" in app.lbl_changes.cget("text")
        assert str(app._field_widgets["Speed"].cget("background")) == editor_mod.C_DIRTY
        # undo returns the field and drops the ledger row
        app.undo()
        assert key not in app._changes, "undo left the change in the ledger"
        assert app.lbl_changes.cget("text") == ""
        assert str(app._field_widgets["Speed"].cget("background")) == editor_mod.C_PANEL2
        # an edit that returns to its start value is not a change
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        app.vars["Speed"].set(str(start))
        app.apply_field("Speed")
        assert key not in app._changes
        # revert one entry directly (what the drawer's button calls)
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        app._changes[key]["revert"]()
        app._reconcile_dirty()
        assert app.table.get(0, "Speed") == start
        assert key not in app._changes and "Player" not in app.dirty_tables


def test_sections_fold_and_remember():
    """The career block starts folded and the choice survives in the config."""
    import editor as editor_mod
    with gui() as app:
        def walk(w, out):
            for c in w.winfo_children():
                if isinstance(c, editor_mod.Section):
                    out[c.section_key] = c
                walk(c, out)
        secs = {}
        walk(app, secs)
        career = secs.get("career")
        assert career is not None, "no career section"
        assert career.folded and not career.body.winfo_manager(), "career should start folded"
        career.toggle()
        app.update()
        assert career.body.winfo_manager(), "expanding did not show the body"
        assert "career" not in (settings.load()["collapsed"] or [])
        career.toggle()
        app.update()
        assert not career.body.winfo_manager()
        assert "career" in (settings.load()["collapsed"] or [])


def test_list_tabs_and_club_rows():
    """Clubs are a tab, not a mode that hides the players; club rows are not players."""
    with gui() as app:
        app.q.set("arsenal")
        app.populate("arsenal")
        app.update()
        assert app.view_mode.get() == "players", "a club search must keep the player rows"
        assert app._mode_counts["clubs"] >= 1, app._mode_counts
        assert "Clubs" in app.tab_clubs.cget("text")
        players = app.tree.get_children()
        assert players and not any(p.startswith("club:") for p in players)
        app.set_view_mode("clubs")
        app.update()
        clubs = app.tree.get_children()
        assert clubs and all(c.startswith("club:") for c in clubs), clubs[:3]
        # bulk edit and compare must not try to parse a club id as a player record
        app.tree.selection_set(clubs[0])
        assert app.bulk_apply("Speed", "add", 1) == 0
        assert app.compare_dialog() is None
        app.set_view_mode("players")
        app.q.set("")
        app.populate("")
        assert len(app.tree.get_children()) == 800


def test_country_picker_and_name_counter():
    with gui() as app:
        app.index = 0
        app.load_form()
        app.vars["Nationality"].set("Argentina")
        app.apply_field("Nationality")
        assert app.table.get(0, "Nationality") == 144
        assert app.vars["Nationality"].get() == "Argentina", "the box lost the country name"
        # the counter follows the var's trace, so no update() — a pending fold rebuild would
        # reload the form from the table and reset the box
        app.vars["name"].set("X" * 61)
        assert app._name_counters["name"].cget("text") == "61/60"
        app.load_form()


def test_ovr_cache_and_skill_filter():
    import pesdb as db
    with gui() as app:
        app.index = 0
        app.load_form()
        before = app.ovr(0)
        for label, _bit, _w, _add in db.ATTRS:
            app.vars[label].set("99")
            app.apply_field(label)
        assert app.ovr(0) > before, "the OVR cache was not invalidated by an ability edit"
        shown = sum(1 for c in app._skill_chips.values() if c.winfo_manager())
        app.skill_filter.set("turn")
        app._filter_skills()
        now = sum(1 for c in app._skill_chips.values() if c.winfo_manager())
        assert 0 < now < shown, (shown, now)
        app.skill_filter.set("")
        app._filter_skills()


def test_preflight_summary():
    with gui() as app:
        app.index = 0
        app.load_form()
        app.vars["Speed"].set("99")
        app.apply_field("Speed")
        counts, warnings = app._preflight_summary()
        assert counts.get("Player") == 1, counts
        assert any("EDIT" in w for w in warnings) == bool(app.edit_saves)
        team = app.player_team.get(app.table.pid(0))
        rows = [j for j in range(len(app.assign.records)) if app.assign.team(j) == team][:2]
        if team is not None and len(rows) == 2:
            for j in rows:
                app.assign.set_shirt_number(j, 7)
            app._mark_table_dirty("PlayerAssignment")
            dups = app._duplicate_squad_numbers()
            assert any("number 7" in d for d in dups), dups


def test_window_size_is_remembered():
    with gui() as app:
        app._remember_geometry()
        size = settings.load()["window"]
        assert "x" in size and all(part.isdigit() for part in size.split("x")), size


def test_blank_career_fields_are_explained():
    """A field the shipped data leaves empty says so instead of reading "0 —"."""
    with gui() as app:
        app.index = 0
        app.load_form()
        app.update()
        empty = app._career_empties()
        assert "Owner Club" in empty, f"OwnerClub is blank in the shipped data: {empty}"
        assert app._club_ref_text(0) == "—", app._club_ref_text(0)
        app._update_career_note()
        note = app.career_note.cget("text")
        assert "Owner Club" in note and "blank" in note, note
        # the note is rebuilt with the section: a relayout must not leave it empty again
        app._layout_one_page(app._one_page_body, wide=False, width=800, height=830)
        for _ in range(4):
            app.update()
        assert "Owner Club" in app.career_note.cget("text"), "the note was lost on a relayout"
        # the field is still editable, and the note follows the table
        app.vars["Owner Club"].set("Manchester United")
        app.apply_club_ref("Owner Club")
        assert app.table.get(0, "Owner Club") == 100
        assert "Manchester" in app.vars["Owner Club"].get()
        assert "blank" not in app.career_note.cget("text"), app.career_note.cget("text")


def test_popup_windows_have_cursors():
    """A popup window with no cursor loses the pointer entirely under XWayland."""
    with gui() as app:
        menu = app.nametowidget(app.menu_btn.cget("menu"))
        assert str(menu.cget("cursor")), "the menu popup has no cursor"
        app.open_coaches()
        win = app._coach_win
        win.update()
        assert str(win.cget("cursor")), "the coach window has no cursor"
        assert str(win.tree.cget("cursor")), "the coach tree has no cursor"
        win.destroy()
        app.index = 0
        app.load_form()
        app.open_appearance()
        aware = app._app_win
        aware.update()
        assert str(aware.cget("cursor")), "the appearance window has no cursor"
        assert str(aware.tree.cget("cursor")), "the appearance tree has no cursor"
        aware.destroy()
        app.open_pes2015()
        pes = app._pes2015_win
        pes.update()
        assert str(pes.cget("cursor")), "the PES 2015 window has no cursor"
        assert str(pes.tree.cget("cursor")), "the PES 2015 list has no cursor"
        pes.destroy()


# ---------------------------------------------------------------- PES 2015
def pes2015_install():
    """The PES 2015 install, or a skip when this machine has none (set PES2015_DIR)."""
    folder = os.environ.get("PES2015_DIR") or pes2015.find_game_dir()
    if not folder or not os.path.isfile(os.path.join(folder, "Data", "dt10.cpk")):
        raise FileNotFoundError(2, "no PES 2015 install (set PES2015_DIR)", "dt10.cpk")
    return folder


def pes2015_box():
    """A private copy of dt10.cpk — edits patch it in place, so a symlink would hit the game."""
    box = tempfile.mkdtemp(prefix="pesforge-2015-")
    os.makedirs(os.path.join(box, "game", "Data"))
    os.makedirs(os.path.join(box, "game", "download"))
    shutil.copy2(os.path.join(pes2015_install(), "Data", "dt10.cpk"),
                 os.path.join(box, "game", "Data", "dt10.cpk"))
    return box


def cli2015(box, *args, expect=0):
    env = dict(os.environ, PES2015_DIR=os.path.join(box, "game"),
               SPFL_STATE=os.path.join(box, "state"),
               SPFL_CONFIG=os.path.join(box, "state", "config.json"))
    p = subprocess.run([sys.executable, os.path.join(ROOT, "cli2015.py")] + list(args),
                       capture_output=True, text=True, env=env)
    assert p.returncode == expect, f"{args} -> rc {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p.stdout + p.stderr


def pes2015_diffs(a, b, span=1 << 16):
    """Byte ranges where two buffers differ; the tail is one range, chunks keep it fast."""
    common = min(len(a), len(b))
    ranges = []
    for start in range(0, common, span):
        end = min(start + span, common)
        if a[start:end] == b[start:end]:
            continue
        i = start
        while i < end:
            if a[i:i + 1] != b[i:i + 1]:
                j = i
                while j < end and a[j:j + 1] != b[j:j + 1]:
                    j += 1
                ranges.append((i, j))
                i = j
            else:
                i += 1
    if len(a) != len(b):
        ranges.append((common, max(len(a), len(b))))
    return ranges


def test_pes2015_reads_the_shipped_table():
    db = pes2015.Cpk2015(os.path.join(pes2015_install(), "Data", "dt10.cpk"))
    table = db.read_table("Player")
    assert len(table) == 10091, len(table)
    assert len(db.read_table("Player1")) == 1100
    for i in range(len(table)):      # every shipped row carries an id and all three names
        assert table.pid(i) and table.name(i) and table.shirt(i) and table.japanese(i), i
    me = table.find("7511")[0]
    assert table.name(me).upper() == "L. MESSI", table.name(me)
    assert pes2015.POSITION_NAMES[table.get(me, "Position")] == "SS"


def test_pes2015_default_targets_include_the_mounted_pack():
    folder = pes2015_install()
    paths = [os.path.basename(p) for p in pes2015.database_cpks(folder)]
    assert paths and paths[0] == "dt10.cpk", paths
    packs = pes2015.mounted_packs(folder)
    assert packs, "DpFileList.bin names no data pack"
    for pack in packs:
        assert pack in paths, (pack, paths)


def test_pes2015_cli_dry_run_then_save():
    box = pes2015_box()
    cpk_path = os.path.join(box, "game", "Data", "dt10.cpk")
    before = open(cpk_path, "rb").read()
    out = cli2015(box, "--set", "7511", "Speed=41")
    assert "Speed" in out and "dry run" in out, out
    assert open(cpk_path, "rb").read() == before, "a dry run wrote to the CPK"
    out = cli2015(box, "--set", "7511", "Speed=41", "--save")
    assert "all edits present" in out, out
    shown = cli2015(box, "--show", "7511")
    assert re.search(r"Speed\s+41\n", shown), shown
    out = cli2015(box, "--set", "7511", "Speed=41")
    assert "nothing changed" in out, out


def test_pes2015_write_confines_its_changes():
    """A save may touch only the header/TOC tables and the appended payload."""
    box = pes2015_box()
    cpk_path = os.path.join(box, "game", "Data", "dt10.cpk")
    before = open(cpk_path, "rb").read()
    db = pes2015.Cpk2015(cpk_path)
    table = db.read_table("Player")
    rows_before = [bytes(r) for r in table.records]
    me = table.find("7511")[0]
    table.set(me, "Speed", 42)
    table.set_name(me, shirt="MESSI-TEST")
    backup = os.path.join(box, "backup")
    where, appended = db.patch("Player", table.to_payload(), backup_dir=backup)
    assert appended and where >= len(before), (where, len(before))
    after = open(cpk_path, "rb").read()
    allowed = [(0x10, 0x10 + db.header.size), (db.toc_off, db.toc_off + db.toc.size)]
    for lo, hi in pes2015_diffs(before, after):
        assert any(lo >= a and hi <= b for a, b in allowed) or lo >= len(before), \
            f"unexpected change at {lo:#x}..{hi:#x}"
    fresh = pes2015.Cpk2015(cpk_path).read_table("Player")
    assert len(fresh) == len(rows_before)
    assert fresh.get(me, "Speed") == 42
    assert fresh.shirt(me) == "MESSI-TEST"
    for i in range(len(fresh)):
        if i != me:
            assert bytes(fresh.records[i]) == rows_before[i], f"row {i} moved"
    folder = os.path.join(backup, "pes2015")
    base = os.path.join(folder, [f for f in os.listdir(folder) if f.endswith(".json")][0][:-5])
    pes2015.restore_backup(base)
    assert open(cpk_path, "rb").read() == before, "restore did not return the original bytes"


def test_pes2015_patch_in_place_keeps_the_file_size():
    box = pes2015_box()
    cpk_path = os.path.join(box, "game", "Data", "dt10.cpk")
    before = open(cpk_path, "rb").read()
    db = pes2015.Cpk2015(cpk_path)
    table = db.read_table("Player1")
    small = pes2015.PlayerTable(b"".join(bytes(r) for r in table.records[:1]), table.prefix)
    assert pes2015.wesys_records(small.to_payload()) == b"".join(
        bytes(r) for r in small.records)
    off = db.entry("Player1")[1][1]
    where, appended = db.patch("Player1", small.to_payload())
    assert not appended, "a one-row payload must fit the slot"
    assert where == off + pes2015.LEAD, (where, off)
    assert len(open(cpk_path, "rb").read()) == len(before), "an in-place patch grew the CPK"
    again = pes2015.Cpk2015(cpk_path)
    found = again.entry("Player1")
    assert found and found[1][1] == off
    assert len(again.read_table("Player1")) == 1


def test_pes2015_name_slots_never_split_a_codepoint():
    assert pes2015.encode_name("A" * 60) == b"A" * 45 + b"\x00"
    cut = pes2015.encode_name("A" * 44 + "名")
    assert len(cut) == 46 and cut.endswith(b"\x00\x00"), cut
    cut.decode("utf-8")              # no half codepoint left behind
    assert pes2015.encode_name("") == b"\x00" * 46


# ---------------------------------------------------------------- importing a database
def test_pes2015_unmount_returns_to_the_detected_install():
    """The 2015 tab's Unmount forgets the imported folder and falls back to the install."""
    folder = pes2015.find_game_dir()
    if not folder or not os.path.isfile(os.path.join(folder, "Data", "dt10.cpk")):
        raise FileNotFoundError("no PES 2015 install")
    saved_env = os.environ.get("PES2015_DIR")
    os.environ["PES2015_DIR"] = folder
    try:
        with gui() as app:
            app.cfg["pes2015_dir"] = folder
            settings.save(app.cfg)
            app.set_view_mode("pes2015")
            app.update()
            panel = app._pes2015_panel
            assert panel.dir == folder, panel.dir
            panel.unmount_dir()
            app.update()
            assert not settings.load().get("pes2015_dir")
            assert panel.dir == folder, "the environment still points at the install"
            assert panel.copies, "tables must reload from the fallback install"
            panel.unmount_dir()                      # nothing imported: say so, change nothing
            assert "nothing imported" in panel.note.cget("text")
    finally:
        if saved_env is None:
            os.environ.pop("PES2015_DIR", None)
        else:
            os.environ["PES2015_DIR"] = saved_env


def test_import_database_switches_the_editor():
    """Import… takes any data CPK file: it becomes the database the editor reads and writes."""
    box = sandbox()
    source = box_cpk(box)                            # <box>/game/download/data_s2627.cpk
    imported = os.path.join(box, "elsewhere", "custom_db.cpk")
    os.makedirs(os.path.dirname(imported), exist_ok=True)
    shutil.copy2(source, imported)                   # a copy outside any game-folder shape
    import editor
    saved_state, saved_vanilla = editor.STATE_DIR, editor.VANILLA
    editor.STATE_DIR = os.path.join(box, "state")    # keep the test out of the real state dir
    editor.VANILLA = os.path.join(editor.STATE_DIR, "Player.vanilla.bin")
    saved_dir = os.environ.get("SPFL_DIR")
    os.environ["SPFL_DIR"] = os.path.join(box, "game")
    try:
        with gui() as app:
            assert app.IMPORT_ENTRY in list(app.cb_db["values"]), "no Import entry in the dropdown"
            before = app.cpk_path
            app.switch_database(imported)
            assert app.cpk_path == imported, app.cpk_path
            assert settings.load().get("database") == imported
            assert len(app.table.records) == 26395
            assert settings.imported_database({"database": imported}) == imported
            app.switch_database(before)              # back to the install: folder mode again
            assert app.cpk_path == before, app.cpk_path
            assert not settings.load().get("database")
            app.switch_database(imported)            # and now the button's own way back
            assert app.cpk_path == imported
            app.unmount_database()
            assert app.cpk_path == before, app.cpk_path
            assert not settings.load().get("database")
            assert len(app.table.records) == 26395
            app.unmount_database()                   # nothing imported: it must say so
            assert app.cpk_path == before
            assert "nothing imported" in app.statusbar.cget("text"), \
                app.statusbar.cget("text")
    finally:
        editor.STATE_DIR, editor.VANILLA = saved_state, saved_vanilla
        if saved_dir is None:
            os.environ.pop("SPFL_DIR", None)
        else:
            os.environ["SPFL_DIR"] = saved_dir


def test_import_database_validates_foreign_files():
    """A file that is not a PES 2021 database is refused, not loaded."""
    box = sandbox()
    import editor
    editor.validate_database(box_cpk(box))           # the shipped database passes
    junk = os.path.join(box, "junk.cpk")
    with open(junk, "wb") as f:
        f.write(b"not a cpk at all" * 64)
    try:
        editor.validate_database(junk)
    except ValueError:
        pass
    else:
        raise AssertionError("a non-CPK file passed database validation")
    for module, folder, pack, vintage in ((pes2015, pes2015.find_game_dir(), "dt10.cpk", "PES 2015"),
                                          (pes2019, pes2019.find_game_dir(), "dt10_x64.cpk", "PES 2019")):
        path = os.path.join(folder, "Data", pack) if folder else ""
        if not path or not os.path.isfile(path):
            continue
        try:
            editor.validate_database(path)
        except ValueError as exc:
            assert vintage in str(exc), exc                  # the refusal names the vintage
            assert f"{vintage} database" in str(exc), exc    # and points at the tab
        else:
            raise AssertionError(f"a {vintage} CPK passed as a PES 2021 database")


def test_pes2015_window_edits_a_sandbox():
    """The PES 2015 window: loads a sandbox install, edits every copy of a player, saves, reverts."""
    box = pes2015_box()
    import editor
    saved_state, saved_vanilla = editor.STATE_DIR, editor.VANILLA
    editor.STATE_DIR = os.path.join(box, "state")
    editor.VANILLA = os.path.join(editor.STATE_DIR, "Player.vanilla.bin")
    saved_dir = os.environ.get("PES2015_DIR")
    os.environ["PES2015_DIR"] = os.path.join(box, "game")
    try:
        with gui() as app:
            app.cfg["confirm_save"] = False          # no dialogs in a test
            menu = app.nametowidget(app.menu_btn.cget("menu"))
            import tkinter as tk
            labels = []
            for n in range(menu.index("end") + 1):
                try:
                    labels.append(menu.entrycget(n, "label"))
                except tk.TclError:
                    pass                             # separators carry no -label
            assert "PES 2015 database…" in labels, labels
            app.open_pes2015()
            win = app._pes2015_win
            win.update()
            assert win.dir == os.path.join(box, "game"), win.dir
            assert len(win.copies) > 12000, len(win.copies)
            multi = next(pid for pid, copies in win.copies.items() if len(copies) > 1)
            win.tree.selection_set(str(multi))
            win.pick()
            win.stat_vars["Speed"].set("41")
            win.name_vars["Shirt Name"].set("TESTSHIRT")
            win.bio_vars["Height"].set("170")
            win.foot_var.set("Right")
            win.flag_vars[("Skill", "Scissors Feint")].set(True)
            win.apply()
            assert multi in win.dirty, "apply did not mark the player"
            win.save()
            assert not win.dirty, "save left edits pending"
            for path, stem, _t, _i in win.copies[multi]:     # every copy landed on disk
                fresh = editor.pes2015.Cpk2015(path).read_table(stem)
                row = next(k for k in range(len(fresh)) if fresh.pid(k) == multi)
                assert fresh.get(row, "Speed") == 41, (stem, fresh.get(row, "Speed"))
                assert fresh.shirt(row) == "TESTSHIRT", (stem, fresh.shirt(row))
                assert fresh.get(row, "Height") == 170, (stem, fresh.get(row, "Height"))
                assert fresh.get(row, "Skill: Scissors Feint") == 1, stem
                assert fresh.get(row, "Foot") == 0, stem
            kept = editor.pes2015.Cpk2015(os.path.join(box, "game", "Data", "dt10.cpk"))
            assert len(kept.read_table("Player")) == 10091
            win.tree.selection_set(str(multi))               # Revert drops an unsaved edit
            win.pick()
            win.stat_vars["Speed"].set("42")
            win.bio_vars["Height"].set("171")
            win.apply()
            assert multi in win.dirty
            win.revert()
            assert not win.dirty, "revert left edits pending"
            _p, _s, table, i = win.display[multi]
            assert table.get(i, "Speed") == 41, "revert did not reload from disk"
            assert table.get(i, "Height") == 170, "revert did not reload the bio fields"
            win.tree.selection_set("4522")                   # the form populates from disk
            win.pick()
            assert win.bio_vars["Height"].get() == "185", win.bio_vars["Height"].get()
            assert win.foot_var.get() == "Right", win.foot_var.get()
            assert win._club_text(4522) == "REAL MADRID", win._club_text(4522)
            assert len(win.flag_vars) == len(editor.pes2015.SKILLS) + len(editor.pes2015.COM_STYLES)
            assert "club" in win.tree["columns"]
            # the same panel is the main window's "PES 2015" tab: it imports the database and
            # swaps the PES 2021 body out while active
            app.set_view_mode("pes2015")
            app.update()
            panel = app._pes2015_panel
            assert panel.dir == os.path.join(box, "game"), panel.dir
            assert len(panel.copies) > 12000, len(panel.copies)
            assert not app._one_page_body.winfo_manager(), "the PES 2021 body stayed visible"
            assert panel.winfo_ismapped(), "the PES 2015 panel is not mapped in its host"
            assert "PES 2015" in app.tab_pes2015.cget("text"), app.tab_pes2015.cget("text")
            assert str(panel.tree.cget("cursor")), "the PES 2015 tab list has no cursor"
            for lab in (app.tab_players, app.tab_clubs, app.tab_pes2015):
                assert lab.winfo_ismapped(), "the tab strip is not visible in PES 2015 mode"
            panel.q.set("messi")
            panel.fill()
            assert "7511" in panel.tree.get_children(), "the search did not list Messi"
            panel.tree.selection_set("7511")
            panel.pick()
            assert panel.name_vars["Name"].get().upper() == "L. MESSI", \
                panel.name_vars["Name"].get()
            assert panel.bio_vars["Height"].get() == "169", panel.bio_vars["Height"].get()
            app.set_view_mode("players")
            app.update()
            assert app._one_page_body.winfo_manager() == "pack", "the body did not come back"
            win.destroy()
    finally:
        editor.STATE_DIR, editor.VANILLA = saved_state, saved_vanilla
        if saved_dir is None:
            os.environ.pop("PES2015_DIR", None)
        else:
            os.environ["PES2015_DIR"] = saved_dir


def test_playing_style_enum():
    """The playing-style ids are PES 2021's binary enum, not the eFootball-era order."""
    import editor
    for idx, name in ((1, "Goal Poacher"), (3, "Fox in the Box"), (4, "Prolific Winger"),
                      (5, "Classic No. 10"), (6, "Hole Player"), (7, "Box-to-Box"),
                      (11, "Offensive Full-back"), (13, "Target Man"),
                      (14, "Creative Playmaker"), (15, "Build Up"),
                      (17, "Defensive Goalkeeper"), (18, "Roaming Flank"),
                      (20, "Orchestrator"), (21, "Full-back Finisher")):
        assert editor.PLAY_STYLES[idx] == name, (idx, editor.PLAY_STYLES[idx])


def test_pes2015_form_is_compact():
    """The PES 2015 form must fit a modest window without scrolling: ~400px tall at ui=1."""
    pes2015_install()                                   # skips cleanly when there is none
    import editor
    with gui() as app:
        app.set_view_mode("pes2015")
        app.update()
        panel = app._pes2015_panel
        assert panel.winfo_reqheight() <= int(560 * app.ui), panel.winfo_reqheight()


def test_pes2015_teams_coaches_squads_roundtrip():
    """Team and coach renames plus a squad-number move land on disk, verify, and restore."""
    box = pes2015_box()
    cpk_path = os.path.join(box, "game", "Data", "dt10.cpk")
    original = open(cpk_path, "rb").read()

    out = cli2015(box, "--teams", "barcelona")
    assert "FC BARCELONA" in out, out
    out = cli2015(box, "--set-team", "108", "Name=FC TEST", "Short=TEST")
    assert "dry run" in out and "FC BARCELONA" in out, out
    assert open(cpk_path, "rb").read() == original, "a dry run wrote to the CPK"
    out = cli2015(box, "--set-team", "108", "Name=FC TEST", "Short=TEST", "--save")
    assert "all edits present" in out, out
    assert "FC TEST" in cli2015(box, "--teams", "108")

    out = cli2015(box, "--set-coach", "mazzarri", "Name=V. COACH", "--save")
    assert "all edits present" in out, out
    assert "V. COACH" in cli2015(box, "--coaches", "18")

    out = cli2015(box, "--set-squad", "L. Messi", "Team=108", "Number=77", "--save")
    assert "all edits present" in out, out
    shown = cli2015(box, "--squads", "L. Messi")
    assert re.search(r"108\s+FC TEST\s+number 77", shown), shown

    state = os.path.join(box, "state", "backups", "pes2015")
    backups = [f[:-5] for f in os.listdir(state) if f.endswith(".json")]
    backups.sort(key=lambda n: os.path.getmtime(os.path.join(state, n + ".json")), reverse=True)
    assert len(backups) >= 3, backups
    for name in backups:
        cli2015(box, "--restore", name)
    assert open(cpk_path, "rb").read() == original, "restore did not return the original bytes"



# ---------------------------------------------------------------- PES 2019
def pes2019_install():
    """The PES 2019 install, or a skip when this machine has none (set PES2019_DIR)."""
    folder = os.environ.get("PES2019_DIR") or pes2019.find_game_dir()
    if not folder or not os.path.isfile(os.path.join(folder, "Data", "dt10_x64.cpk")):
        raise FileNotFoundError(2, "no PES 2019 install (set PES2019_DIR)", "dt10_x64.cpk")
    return folder


def pes2019_box():
    """A private copy of dt10_x64.cpk — a patch writes in place, so a link would hit the game."""
    box = tempfile.mkdtemp(prefix="pesforge-2019-")
    os.makedirs(os.path.join(box, "game", "Data"))
    os.makedirs(os.path.join(box, "game", "download"))
    shutil.copy2(os.path.join(pes2019_install(), "Data", "dt10_x64.cpk"),
                 os.path.join(box, "game", "Data", "dt10_x64.cpk"))
    return box


def test_pes2019_reads_the_shipped_table():
    """The map against the real bytes: ids, names, bio and the pesdb.net card for Messi."""
    db = pes2019.Cpk2019(os.path.join(pes2019_install(), "Data", "dt10_x64.cpk"))
    table = db.read_table("Player")
    assert len(table) == 13805, len(table)
    found = table.find("7511")
    assert found, "L. Messi is not in the shipped table"
    i = found[0]
    assert (table.name(i), table.shirt(i)) == ("L. MESSI", "MESSI"), table.name(i)
    assert (table.get(i, "Height"), table.get(i, "Weight"), table.get(i, "Age")) == (170, 72, 31)
    assert table.get(i, "Foot") == 1, "Messi is left-footed"
    assert table.get(i, "Nationality") == 144, "country code 144 is Argentina"
    assert pes2019.STYLE_NAMES[table.get(i, "Playing Style")] == "Creative Playmaker"
    assert pes2019.POSITION_NAMES[table.get(i, "Position")] == "SS"
    card = {"Attacking Prowess": 95, "Ball Control": 97, "Dribbling": 96, "Low Pass": 88,
            "Lofted Pass": 86, "Finishing": 95, "Place Kicking": 90, "Swerve": 89,
            "Speed": 86, "Explosive Power": 92, "Unwavering Balance": 95,
            "Physical Contact": 66, "Jump": 67, "Stamina": 76, "Ball Winning": 48,
            "Goalkeeping": 40, "GK Catch": 40, "Reflexes": 40}
    got = {k: table.get(i, k) for k in card}
    assert got == card, {k: (got[k], card[k]) for k in card if got[k] != card[k]}
    # the goalie copy of the same map: Neuer's two skills are the two pesdb lists
    neu = table.find("33185")[0]
    assert (table.get(neu, "Height"), table.get(neu, "Weight")) == (193, 92)
    skills = [label for label, _bit in pes2019.FLAG_FIELDS.items() if table.get(neu, label)]
    assert sorted(skills) == ["Skill: GK Long Throw", "Skill: GK Low Punt"], skills


def test_pes2019_teams_coaches_and_squads_line_up():
    """Every foreign key resolves: assignment -> team, team -> coach, and the real squad."""
    db = pes2019.Cpk2019(os.path.join(pes2019_install(), "Data", "dt10_x64.cpk"))
    teams = pes2019.TeamTable(*db.read_raw("Team"))
    coaches = pes2019.CoachTable(*db.read_raw("Coach"))
    assign = pes2019.AssignmentTable(*db.read_raw("PlayerAssignment"))
    stadiums = pes2019.StadiumTable(*db.read_raw("Stadium"))
    assert (len(teams), len(coaches), len(assign), len(stadiums)) == (529, 529, 14368, 44)
    barca = teams.by_id()[108]
    assert teams.name(barca) == "BARCELONA"
    assert coaches.name(coaches.by_id()[teams.coach_id(barca)]) == "E. VALVERDE"
    assert stadiums.name(stadiums.find("2")[0]) == "Camp Nou"
    rows = assign.find(7511)
    assert sorted((teams.name(teams.by_id()[assign.team(r)]), assign.number(r))
                  for r in rows) == [("ARGENTINA", 10), ("BARCELONA", 10)], rows
    known = set(teams.by_id())
    unknown = {assign.team(r) for r in range(len(assign))} - known
    assert not unknown, f"{len(unknown)} squad rows point at no team: {sorted(unknown)[:5]}"


def test_pes2019_default_targets_include_the_mounted_packs():
    """The writer's default list: the database pack first, then what DpFileList mounts."""
    folder = pes2019_install()
    paths = [os.path.basename(p) for p in pes2019.database_cpks(folder)]
    assert paths[0] == "dt10_x64.cpk", paths
    packs = pes2019.mounted_packs(folder)
    assert packs, "DpFileList.bin names no packs"
    assert paths[1:] == packs, paths
    live = [p for p in pes2019.database_cpks(folder) if p.endswith("dt80_200E_x64.cpk")]
    if live:                      # the live-update pack carries a superset of the base table
        assert len(pes2019.Cpk2019(live[0]).read_table("Player")) == 16762


def test_pes2019_patch_round_trip_and_restore():
    """One ability and one name change; every other row and field stays byte-identical."""
    box = pes2019_box()
    path = os.path.join(box, "game", "Data", "dt10_x64.cpk")
    original = open(path, "rb").read()
    db = pes2019.Cpk2019(path)
    table = db.read_table("Player")
    i = table.find("7511")[0]
    before = bytes(table.records[i])
    table.set(i, "Ball Control", 80)
    table.set_name(i, shirt="MESSI-X")
    db.patch("Player", table.to_payload(), backup_dir=os.path.join(box, "backups"))
    back = pes2019.Cpk2019(path).read_table("Player")
    assert len(back) == len(table), "the row count changed"
    assert back.get(i, "Ball Control") == 80 and back.shirt(i) == "MESSI-X"
    # Ball Control is a 6-bit field at bit 134 and the shirt name ends at +102: a few bytes
    moved = [k for k in range(pes2019.REC) if before[k] != back.records[i][k]]
    assert len(moved) <= 6, f"{len(moved)} bytes moved: {moved}"
    for j in range(len(back)):
        if j != i:
            assert bytes(back.records[j]) == bytes(table.records[j]), f"row {j} moved"
    backups = pes2019.backup_list(os.path.join(box, "backups"))
    assert backups, "no backup was taken"
    for name in backups:
        pes2019.restore_backup(name)
    assert open(path, "rb").read() == original, "restore did not return the original bytes"
    shutil.rmtree(box, ignore_errors=True)


def test_pes2019_tab_swaps_the_body_and_shows_players():
    """The tab drives the shared panel: body swap, search, select, and back again."""
    folder = pes2019_install()
    with gui() as app:
        app.cfg["pes2019_dir"] = folder
        app.set_view_mode("pes2019")
        app.update()
        panel = app._pes2019_panel
        assert panel.winfo_ismapped(), "the PES 2019 panel is not on screen"
        assert not app._one_page_body.winfo_manager(), "the PES 2021 body is still packed"
        assert panel.copies, "the panel read no player copies"
        panel.q.set("messi")
        panel.fill()
        rows = panel.tree.get_children()
        assert rows, "the panel list came up empty"
        panel.tree.selection_set(rows[0])
        panel.pick()
        assert panel.pid == 7511, panel.pid
        assert panel.name_vars["Name"].get() == "L. MESSI", panel.name_vars["Name"].get()
        app.set_view_mode("players")
        app.update()
        assert not panel.winfo_ismapped(), "the panel stayed on screen"
        assert app._one_page_body.winfo_manager(), "the PES 2021 body did not come back"
        app.set_view_mode("pes2019")
        app.update()
        assert panel.winfo_ismapped(), "the panel did not come back"



def gtk4_python():
    """An interpreter that can import GTK4, picked the way bin/pesforge-gtk4 picks one.

    The interpreter running the suite is not necessarily the one with system PyGObject: a venv
    without system site packages has no `gi` at all.
    """
    for candidate in (os.environ.get("PESFORGE_GTK4_PYTHON"), sys.executable,
                      "/usr/bin/python3", "python3"):
        if not candidate:
            continue
        path = candidate if os.sep in candidate else shutil.which(candidate)
        if not path or not os.path.isfile(path):
            continue
        probe = subprocess.run([path, "-c", 'import gi; gi.require_version("Gtk", "4.0");'
                                             ' from gi.repository import Gtk'], capture_output=True)
        if probe.returncode == 0:
            return path
    raise RuntimeError("no interpreter with GTK4 (PyGObject) for the GTK4 app")


def test_gtk4_app_builds_and_searches():
    """The GTK4 screen builds on the installed PES 2019 table and its search narrows the list.

    Drives the app through its own self-check (`PESFORGE_GTK_SELFTEST=1`): a window that opens is
    not the same as a list holding the real rows with a record loaded into the form.
    """
    game = pes2019_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the GTK4 app")
    env = dict(os.environ, PES2019_DIR=game, PESFORGE_GTK_SELFTEST="1")
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")],
                         capture_output=True, text=True, env=env, timeout=600)
    text = out.stdout + out.stderr
    assert "SELFTEST OK" in text, text[-600:]
    assert out.returncode == 0, f"the app exited {out.returncode}: {text[-400:]}"
    found = re.search(r"SELFTEST OK players=(\d+) selected=(\d+) abilities=(\d+) chips=(\d+)", text)
    assert found, text[-300:]
    players, selected, abilities, chips = (int(g) for g in found.groups())
    assert players > 10000, f"only {players} players listed"
    assert selected < players, f"row {selected} selected of {players}"
    assert abilities == 23, f"{abilities} ability rows"
    expected = len(pes2019.SKILLS) + len(pes2019.COM_STYLES)
    assert chips == expected, f"{chips} skill chips, the map holds {expected}"


def test_gtk4_bulk_matrix_stages_and_commits():
    """Tick rows, batch-edit one field across them, commit, restore — on a sandbox copy.

    The app runs the whole cycle itself (`PESFORGE_GTK_SELFTEST=write`); the assertions are
    that the staging is right, the file on disk really changed, and the backup put it back.
    """
    box = pes2019_box()                        # a copy of dt10_x64.cpk, never the install
    game = os.path.join(box, "game")
    cpk = os.path.join(game, "Data", "dt10_x64.cpk")
    before = open(cpk, "rb").read()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the GTK4 app")
    env = dict(os.environ, PES2019_DIR=game, SPFL_STATE=os.path.join(box, "state"),
               PESFORGE_GTK_SELFTEST="write")
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=600)
    text = out.stdout + out.stderr
    assert "SELFTEST WRITE OK" in text, text[-600:]
    found = re.search(r"SELFTEST WRITE OK players=(\d+) edited=(\d+) backups=(\d+) restored=(\d+)", text)
    assert found, text[-300:]
    players, edited, backups, restored = (int(g) for g in found.groups())
    assert players > 10000, f"only {players} players listed"
    assert edited == 3, f"{edited} players edited"
    assert backups >= 1 and restored == 1, (backups, restored)
    assert open(cpk, "rb").read() == before, "the sandbox was not restored byte for byte"
    shutil.rmtree(box, ignore_errors=True)


def test_gtk4_starts_empty_and_imports_on_demand():
    """The GTK4 window reads nothing until a database is imported, then fills itself."""
    game = pes2019_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the GTK4 app")
    env = dict(os.environ, PES2019_DIR=game, PESFORGE_GTK_SELFTEST="empty")
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=600)
    text = out.stdout + out.stderr
    assert "SELFTEST EMPTY OK" in text, text[-600:]
    found = re.search(r"SELFTEST EMPTY OK rows=(\d+) source=(\S+)", text)
    assert found, text[-300:]
    assert int(found.group(1)) > 10000, found.group(1)


def test_gtk4_import_probe_identifies_the_release():
    """The importer reads the release out of the table, and explains a file it cannot use."""
    game = pes2019_install()
    py = gtk4_python()
    junk = f"/tmp/pesforge-notacpk-{os.getpid()}.txt"
    script = (
        "import sys; sys.path.insert(0, %r); import editor_gtk as g\n"
        "print('good', g.probe_database(%r))\n"
        "open(%r, 'w').write('not a cpk')\n"
        "print('junk', g.probe_database(%r))\n"
    ) % (ROOT, os.path.join(game, "Data", "dt10_x64.cpk"), junk, junk)
    # a pick that lands on a CPK without the tables must find the one that has them
    empty = os.path.join(game, "Data", "dt00_4K_x64.cpk")
    script += ("print('swap', g.sibling_database(%r).split('/')[-1])\n"
               "print('nada', repr(g.sibling_database(%r)))\n") % (empty, junk)
    out = subprocess.run([py, "-c", script], capture_output=True, text=True, timeout=600,
                         env=dict(os.environ, GDK_BACKEND="x11"))
    text = out.stdout + out.stderr
    assert "('pes2019'" in text, text[-400:]
    assert "13,805" in text, text[-400:]
    assert "'unreadable'" in text, text[-400:]
    assert "nada ''" in text, text[-400:]
    if os.path.isfile(empty):
        assert "swap dt10_x64.cpk" in text, text[-400:]
    try:
        os.unlink(junk)
    except OSError:
        pass


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except FileNotFoundError as exc:
            print(f"skip {name}: no game database ({exc.filename})")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except RuntimeError as exc:                  # a skip for a missing optional runtime
            print(f"skip {name}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

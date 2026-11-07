#!/usr/bin/env python3
"""Assert-based tests for the PESForge editor. Run: python3 tests/run_all.py

No pytest on this box: every check is a plain assert and the exit code is the verdict.
Each CLI test gets its own sandbox (SPFL_DIR / SPFL_STATE) so nothing touches the real
game directory.
"""
import csv
import contextlib
import gi
import importlib
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
import pes2021
import pes5
import pes6
import settings

# Any machine: SPFL_GAME wins, else whatever the editor would auto-detect.
GAME = os.environ.get("SPFL_GAME") or settings.game_dir()
CPK = settings.database_path(GAME) if GAME else ""
PLAYER = "Player.bin"
PA = "PlayerAssignment.bin"


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


def test_theme_module():
    import theme
    assert theme.choice({}) == theme.DEFAULT
    assert dict(theme.CHOICES)["ice"] == "Ice"
    assert set(theme.tokens("crimson")) == set(theme.TOKEN_NAMES)
    assert "theme" in settings.DEFAULTS and settings.DEFAULTS["theme"] in theme.BASE
    light = theme.toward(theme.BASE["amber"]["dim"], theme.BASE["amber"]["bg"], 4.5)
    assert theme.contrast(light, theme.BASE["amber"]["bg"]) <= 4.6


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


def pes2019_mounted_database(game):
    """The CPK the GTK app opens by default: the mounted dt80 pack, else the base table."""
    packs = pes2019.database_cpks(game)
    return next((p for p in reversed(packs) if p.endswith("dt80_200E_x64.cpk")),
                packs[0] if packs else "")


def pes2021_database():
    """This machine's SP Football Life database (SPFL_DIR wins), or raise for a skip."""
    folder = os.environ.get("SPFL_DIR") or settings.game_dir()
    path = settings.database_path(folder) if folder else ""
    if not os.path.isfile(path):
        raise FileNotFoundError(2, "no SP Football Life database", "data_s2627.cpk")
    return path


def test_pesdb_name_never_splits_a_codepoint():
    """The 312-byte name slot is cut at a codepoint boundary, exactly like 2015/2019."""
    box = sandbox()
    table = pes2021.Cpk(box_cpk(box)).read_table("Player")
    name = "\u6f22" * 19 + "ab" + "\u6f22" * 10          # 89 bytes: a naive cut splits char 21
    table.set_name(3, full=name)
    got = table.name(3)
    assert "\ufffd" not in got, repr(got)
    assert len(got.encode("utf-8")) <= pesdb.NAME_SLOT - 1, got
    assert pesdb.encode_name(name).split(b"\x00")[0].decode("utf-8") == got, got


def test_pes2021_reads_the_shipped_database():
    """The 2021 adapter over the real FL27 bytes, in the vocabulary the panel asks for."""
    db = pes2021.Cpk(pes2021_database())
    table = db.read_table("Player")
    teams = db.read_table("Team")
    assign = db.read_table("PlayerAssignment")
    assert (len(table), len(teams), len(assign)) == (26395, 745, 21240), \
        (len(table), len(teams), len(assign))
    i = table.find("7511")[0]
    assert table.name(i) == "Lionel Messi", table.name(i)
    assert (table.get(i, "Height"), table.get(i, "Age")) == (170, 39)
    assert table.get(i, "Foot") == 1, "Messi is left-footed"
    assert table.get(i, "Stronger Foot") == 1, "pesdb's own label reads the same field"
    assert pes2021.STYLE_NAMES[table.get(i, "Playing Style")] == "Classic No. 10"
    assert pes2021.POSITION_NAMES[table.get(i, "Position")] == "SS"
    assert (len(pes2021.ABILITIES), len(pes2021.SKILLS)) == (25, 48)
    assert len(pes2021.STYLE_NAMES) == 32, "the 5-bit style field, 22 of them named"
    assert pes2021.STYLE_NAMES[21] == "Full-back Finisher"
    assert table.japanese(i) == "", "a 312-byte record has no Japanese name slot"
    assert "Height" in pes2021.FIELD_BY_LABEL and "Skill: Scissors Feint" in pes2021.FIELD_BY_LABEL
    # the panel's own accessors land on the right rows
    assert teams.id(0) == 1 and teams.name(0).lower() == "ireland"
    rows = assign.find(7511)
    assert len(rows) == 2 and all(assign.player(r) == 7511 for r in rows), rows
    assert assign.number(rows[0]) >= 1
    # a set/read round trip on a copy in memory: no file is touched here
    was = table.get(i, "Speed")
    table.set(i, "Speed", 99)
    assert table.get(i, "Speed") == 99
    table.set(i, "Speed", was)
    assert table.get(i, "Speed") == was
    table.set(i, "Speed", 250)                        # clamped, never wrapped around
    assert table.get(i, "Speed") == 99, table.get(i, "Speed")


def test_gtk4_theme_follows_the_system():
    """The app paints light or dark as the desktop asks, with readable contrast either way."""
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    seen = {}
    for mode in ("light", "dark"):
        env = dict(os.environ, PESFORGE_THEME=mode, PESFORGE_GTK_SELFTEST="theme")
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                             text=True, env=env, timeout=300)
        text = out.stdout + out.stderr
        assert "SELFTEST THEME" in text, text[-500:]
        line = [ln for ln in text.splitlines() if ln.startswith("SELFTEST THEME")][0]
        parts = dict(p.split("=") for p in line.split() if "=" in p)
        assert line.split()[2] == mode, line
        assert float(parts["contrast"]) >= 4.5, line
        seen[mode] = parts["bg"]
    assert seen["light"] != seen["dark"], seen          # the two modes are actually different


def test_gtk4_compact_layout_fits_without_scrolling():
    """The whole editor is on screen at the tiled size and at the default: no pane scrolls.

    The panes sit in scroll areas as a safety net, so the check is that their content still fits
    the page the scroller shows — this is what the user asked for (no scrolling, no resizing).
    """
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    # 941 is the size the app is used at; 917 is one tile narrower on this desktop and the app
    # keeps the split layout there too (the form's grid is 584px wide, not 607), so the promise
    # holds at both — see test_gtk4_layout_is_a_function_of_size for the way back
    for size in ("941x1030", "917x1030", "1504x1030"):
        env = dict(os.environ, PESFORGE_GTK_SELFTEST="fit", PESFORGE_GTK_SIZE=size, SPFL_DIR=game)
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                             capture_output=True, text=True, env=env, timeout=600)
        text = out.stdout + out.stderr
        assert "SELFTEST FIT OK" in text, text[-700:]
    assert "scrolls" not in text, text[-400:]


def test_gtk4_layout_is_a_function_of_size():
    """A window that has grown arranges itself the same way as one born that size (Phase 4).

    The sweep resizes the live window up across the split layout's boundary and then re-asks the
    policy at each size on the way back down. It fails on: a size deciding differently the second
    time, a list narrower than the columns it shows, a squeezed list column (which is what a
    blank column looks like), or an editor pane scrolling while the split layout is in use.
    """
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="sweep", PESFORGE_GTK_SIZE="700x1030",
               SPFL_DIR=game)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                         capture_output=True, text=True, env=env, timeout=600)
    text = out.stdout + out.stderr
    assert "SELFTEST SWEEP OK" in text, text[-900:]
    assert "layout sweep" not in text, text[-900:]


def test_gtk4_every_control_has_an_accessible_name():
    """Nothing interactive is announced as its role alone, and no column renders blank (Phase 5).

    Runs the a11y walk at two widths: the compact one shows four list columns, the wide one all
    six, so the rendered-row check (one cell per visible column, none of them zero-width) is
    exercised for both the hidden and the shown columns.
    """
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    for size, columns in (("941x1030", 4), ("1504x1030", 6)):
        env = dict(os.environ, PESFORGE_GTK_SELFTEST="a11y", PESFORGE_GTK_SIZE=size,
                   SPFL_DIR=game)
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                             capture_output=True, text=True, env=env, timeout=600)
        text = out.stdout + out.stderr
        assert "SELFTEST A11Y OK" in text, text[-900:]
        line = [ln for ln in text.splitlines() if ln.startswith("SELFTEST A11Y OK")][0]
        parts = dict(p.split("=") for p in line.split() if "=" in p)
        assert int(parts["columns"]) == columns, line
        assert int(parts["cells"]) == columns, line
        assert int(parts["named"]) > 50, line
        assert "no accessible name" not in text, text[-900:]


def test_gtk4_follows_the_desktop_text_scale():
    """Text scaling moves the app's own text, without clipping anything (Phase 5).

    GTK resolves a stylesheet's `px` font sizes against the screen, not against the desktop's
    text-scaling factor — measured: the app's player-name line stayed 22px tall at every scale,
    while an unstyled label doubled. So the app's font sizes are in `em`, and the fixed pixel
    widths (column widths, field-label widths, the list pane's thresholds) are multiplied by the
    factor when they are built. This runs the app at four scales and asserts the name line grows
    with each, and that no widget is drawn outside its parent or on top of a sibling.
    """
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    heights = []
    for scale in ("1.0", "1.25", "1.5", "2.0"):
        env = dict(os.environ, PESFORGE_GTK_SELFTEST="scale", PESFORGE_GTK_SCALE=scale,
                   PESFORGE_GTK_SIZE="941x1030", SPFL_DIR=game)
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                             capture_output=True, text=True, env=env, timeout=600)
        text = out.stdout + out.stderr
        assert "SELFTEST SCALE OK" in text, text[-900:]
        line = [ln for ln in text.splitlines() if ln.startswith("SELFTEST SCALE OK")][0]
        parts = dict(p.split("=") for p in line.split() if "=" in p)
        assert parts["scale"] == scale, line
        heights.append(int(parts["hero_h"]))
    assert heights == sorted(set(heights)), heights      # strictly grows with the scale
    assert heights[-1] >= heights[0] * 1.8, heights      # and by roughly the factor (2.0 at most)


def test_gtk4_ability_boxes_share_one_column():
    """The abilities grid is straight: one label width, one value-box column (the user's report).

    In a Box per cell, a label wider than its `set_size_request` takes its own natural width and
    shifts the value box beside it, so the boxes line up per row instead of per column — what
    "misaligned" looked like on a real screenshot (40px apart, in the rows whose neighbours had
    long names like "Short Pass Accuracy"). The labels are measured and drawn at one step now;
    this fails if a second label width or a second box x appears, at both shapes, Position
    included.
    """
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    for size, expect_cells in (("941x1030", 26), ("1504x1030", 26)):
        env = dict(os.environ, PESFORGE_GTK_SELFTEST="align", PESFORGE_GTK_SIZE=size,
                   SPFL_DIR=game)
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                             capture_output=True, text=True, env=env, timeout=600)
        text = out.stdout + out.stderr
        assert "SELFTEST ALIGN OK" in text, text[-900:]
        line = [ln for ln in text.splitlines() if ln.startswith("SELFTEST ALIGN OK")][0]
        parts = dict(p.split("=") for p in line.split() if "=" in p)
        assert int(parts["cells"]) == expect_cells, line
        assert int(parts["box_x"]) > int(parts["label_w"]), line


def test_gtk4_resizes_under_a_window_manager():
    """A real shrink and grow under a window manager: the same size decides the same layout.

    `PESFORGE_GTK_SELFTEST=sweep` can only *ask* the policy what a smaller size would do, because
    a window with no window manager will not honour a shrink. `xfwm4` does: this drives a real
    resize against the live app with `xdotool` — 1400 → 900 → 1400 → 917 → 941 wide, at 1030 tall
    — and reads the geometry the app prints (`=watch`). It fails when a size seen twice decided
    differently, when an editor pane scrolls at a size where the split layout is in use, or when
    917 (a tiled window on this desktop) is not split. Skips, not fails, where xfwm4, xdotool or
    Xvfb are missing.
    """
    import shutil as _shutil
    import time
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    xfwm4 = _shutil.which("xfwm4")
    xdotool = _shutil.which("xdotool")
    xvfb = _shutil.which("Xvfb") or os.path.expanduser("~/.local/opt/xvfb/usr/bin/Xvfb")
    if not (xfwm4 and xdotool and os.path.exists(xvfb)):
        raise RuntimeError("no window manager (xfwm4), xdotool or Xvfb: cannot drive a resize")
    display = ":100"
    log = os.path.join(tempfile.mkdtemp(prefix="pesforge-wm-"), "watch.log")
    xvfb_p = subprocess.Popen([xvfb, display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"])
    env = dict(os.environ, DISPLAY=display)
    env.pop("WAYLAND_DISPLAY", None)
    wm = None
    app = None
    try:
        for _ in range(40):                      # wait for the X server to answer
            if subprocess.run([xdotool, "getdisplaygeometry"], env=env,
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.25)
        wm = subprocess.Popen([xfwm4, "--compositor=off"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        app_env = dict(env, GDK_BACKEND="x11", PESFORGE_GTK_SELFTEST="watch",
                       PESFORGE_GTK_SIZE="1400x1030", SPFL_DIR=game)
        app = subprocess.Popen([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                               stdout=open(log, "w"), stderr=subprocess.STDOUT, env=app_env)
        window = ""
        for _ in range(80):                      # wait for the window, then its first line
            found = subprocess.run([xdotool, "search", "--pid", str(app.pid)], env=env,
                                   capture_output=True, text=True)
            window = (found.stdout.split() or [""])[-1]
            if window and "WATCH" in open(log).read():
                break
            time.sleep(0.5)
        assert window, "the app never mapped a window under the window manager"
        steps = [(1400, 1030), (900, 1030), (1400, 1030), (917, 1030), (941, 1030)]
        seen = {}
        for width, height in steps:
            assert subprocess.run([xdotool, "windowsize", window, str(width), str(height)],
                                  env=env, capture_output=True).returncode == 0
            time.sleep(1.6 if width >= 917 else 1.2)     # the layout settles, it is not instant
            lines = [ln for ln in open(log) if ln.startswith("WATCH")]
            match = [ln for ln in lines if ln.split()[1].split("x")[0].isdigit()
                     and abs(int(ln.split()[1].split("x")[0]) - width) <= 2
                     and abs(int(ln.split()[1].split("x")[1]) - height) <= 2]
            assert match, f"no geometry line for {width}x{height}: {lines[-3:]}"
            line = match[-1]
            seen[(width, height)] = (line, seen.get((width, height), (line,))[0])
            print(f"wm resize {width}x{height}: {line.strip()}")
        for (width, height), (now_line, first_line) in seen.items():
            if now_line is not first_line:               # a size the driver visited twice
                for key in ("paned=", "cols="):
                    was = [w for w in first_line.split() if w.startswith(key)][0]
                    got = [w for w in now_line.split() if w.startswith(key)][0]
                    assert was == got, f"{width}x{height}: {key}{got} on the way back, {was} before"
        for width, height in ((1400, 1030), (917, 1030), (941, 1030)):
            line = seen[(width, height)][0]
            assert "paned=h" in line, f"{width}: the split layout is not in use: {line.strip()}"
            for pane in ("form=", "bio="):
                cell = [w for w in line.split() if w.startswith(pane)][0]
                assert "+h" not in cell and "+v" not in cell, \
                    f"{width}: {pane} scrolls while split: {cell}"
    finally:
        for proc in (app, wm, xvfb_p):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(os.path.dirname(log), ignore_errors=True)


def test_gtk4_edits_and_saves_pes2021():
    """SP Football Life is editable: load the install's database copy, edit, save, read it back.

    The check copies the database into a scratch folder shaped like an install
    (`download/data_s2627.cpk`), edits an ability, a skill and the shirt name, saves, and reads
    all three back off disk — the app hashes the game's own file and asserts it is untouched.
    """
    game = os.path.dirname(os.path.dirname(pes2021_database()))
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="write2021", SPFL_DIR=game)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST WRITE2021 OK" in text, text[-700:]
    assert "Speed=" in text and "shirt=SELFTEST" in text, text[-700:]
    found = re.search(r"SELFTEST WRITE2021 OK players=(\d+) pid=(\d+)", text)
    assert found and int(found.group(1)) == 26395, text[-400:]
    shutil.rmtree(os.path.join(os.environ.get("TMPDIR") or "/tmp", "pesforge-2021-check"),
                  ignore_errors=True)


def national_first_count(cpk_path):
    """Players whose first assignment row is a national team.

    The count the CLUB column has to get right: those rows used to read IRELAND where the
    player's club belongs. Computed here from the CPK, independently of the app.
    """
    db = pes2019.Cpk2019(cpk_path)
    players = db.read_table("Player")
    assign = pes2019.AssignmentTable(*db.read_raw("PlayerAssignment"))
    teams_of = {}
    for i in range(len(assign)):
        teams_of.setdefault(assign.player(i), []).append(assign.team(i))
    return sum(1 for i in range(len(players))
               if teams_of.get(players.pid(i))
               and teams_of[players.pid(i)][0] <= pesdb.NATIONAL_MAX)


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
    raise RuntimeError("no interpreter with GTK4 (PyGObject) for the app")


def test_gtk4_app_builds_and_searches():
    """The app's screen builds on the installed PES 2019 table and its search narrows the list.

    Drives the app through its own self-check (`PESFORGE_GTK_SELFTEST=1`): a window that opens is
    not the same as a list holding the real rows with a record loaded into the form. The same run
    proves the two data/UI fixes: no CLUB cell shows a national team where the club belongs, and
    the window carries no inert button (the spike's Undo/Redo/Discard/Save-to-game header row).
    """
    game = pes2019_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
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
    assert "CHROME OK buttons=6" in text, text[-600:]
    found = re.search(r"nations=(\d+)", text)
    assert found, text[-300:]
    wanted = national_first_count(pes2019_mounted_database(game))
    assert int(found.group(1)) == wanted and wanted > 0, (
        f"the app repaired {found.group(1)} national-first club rows, the CPK says {wanted}")


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
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PES2019_DIR=game, SPFL_STATE=os.path.join(box, "state"),
               PESFORGE_GTK_SELFTEST="write")
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=600)
    text = out.stdout + out.stderr
    assert "SELFTEST WRITE OK" in text, text[-600:]
    found = re.search(r"SELFTEST WRITE OK players=(\d+) edited=(\d+) backups=(\d+) restored=(\d+)", text)
    assert found, text[-300:]
    assert "state=clean" in text, "the saved rows still read as edited in the list"
    players, edited, backups, restored = (int(g) for g in found.groups())
    assert players > 10000, f"only {players} players listed"
    assert edited == 3, f"{edited} players edited"
    assert backups >= 1 and restored == 1, (backups, restored)
    assert open(cpk, "rb").read() == before, "the sandbox was not restored byte for byte"
    shutil.rmtree(box, ignore_errors=True)


def test_gtk4_starts_empty_and_imports_on_demand():
    """The app reads nothing until a database is imported, then fills itself."""
    game = pes2019_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PES2019_DIR=game, PESFORGE_GTK_SELFTEST="empty")
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=600)
    text = out.stdout + out.stderr
    assert "SELFTEST EMPTY OK" in text, text[-600:]
    found = re.search(r"SELFTEST EMPTY OK rows=(\d+) source=(\S+)", text)
    assert found, text[-300:]
    assert int(found.group(1)) > 10000, found.group(1)
    # a pick from the game's own Data/ that carries no tables resolves to one that does
    empty_pick = os.path.join(game, "Data", "dt00_4K_x64.cpk")
    if os.path.isfile(empty_pick):
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", empty_pick],
                            capture_output=True, text=True, timeout=600,
                            env=dict(env, PESFORGE_GTK_SELFTEST="1"))
        picked = out.stdout + out.stderr
        assert "source=dt10_x64.cpk" in picked, picked[-400:]
    # and a folder pick: the install itself resolves to the pack the game loads
    pack = pes2019_mounted_database(game)
    if pack and os.path.isfile(pack):
        out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py"), "--db", game],
                             capture_output=True, text=True, timeout=900,
                             env=dict(env, PESFORGE_GTK_SELFTEST="1"))
        picked = out.stdout + out.stderr
        assert "SELFTEST OK" in picked, picked[-400:]
        assert f"source={os.path.basename(pack)}" in picked, picked[-400:]


def test_gtk4_import_probe_identifies_the_release():
    """The importer reads the release out of the table, and explains a file it cannot use.

    A pick also resolves: the folder the file sits in, then the install around it. SP Football
    Life's `Data/dt10_x64.cpk` is an 86 KB stub with no player table, so a filename is never
    enough — the resolver probes the candidates and takes the one that really carries a table.
    """
    game = pes2019_install()
    py = gtk4_python()
    junk = f"/tmp/pesforge-notacpk-{os.getpid()}.txt"
    pack = pes2019_mounted_database(game)
    script = (
        "import sys; sys.path.insert(0, %r); import editor_gtk as g\n"
        "print('good', g.probe_database(%r))\n"
        "open(%r, 'w').write('not a cpk')\n"
        "print('junk', g.probe_database(%r))\n"
        "print('junk-dir', g.find_database(%r)[0] == '')\n"
    ) % (ROOT, os.path.join(game, "Data", "dt10_x64.cpk"), junk, junk, os.path.dirname(junk))
    # a pick that lands on a CPK without the tables must find the one that has them
    empty = os.path.join(game, "Data", "dt00_4K_x64.cpk")
    if os.path.isfile(empty):
        script += ("print('file-pick', g.describe_pick(%r)[2].split('/')[-1])\n"
                   "print('data-folder', g.find_database(%r)[0].split('/')[-1])\n"
                   ) % (empty, os.path.dirname(empty))
    if pack and os.path.isfile(pack):
        script += ("print('install', g.find_database(%r)[0].split('/')[-1])\n"
                   "print('install-vintage', g.find_database(%r)[1])\n"
                   "print('install-ok', g.describe_pick(%r)[1])\n"
                   ) % (game, game, game)
    # SP Football Life's Data/ holds no database at all: the answer is one shelf up in download/
    spfl = settings.game_dir() or ""
    if spfl and os.path.isfile(os.path.join(spfl, "download", "data_s2627.cpk")):
        script += ("print('spfl-root', g.find_database(%r)[0].split('/')[-1])\n"
                   "print('spfl-vintage', g.find_database(%r)[1])\n"
                   "print('spfl-loadable', g.describe_pick(%r)[1])\n"
                   "print('spfl-data-pick', g.describe_pick(%r)[2].split('/')[-1])\n"
                   ) % (spfl, spfl, spfl, os.path.join(spfl, "Data"))
    pes15 = os.environ.get("PES2015_DIR") or ""
    if pes15 and os.path.isfile(os.path.join(pes15, "Data", "dt10.cpk")):
        script += ("print('pes2015-root', g.find_database(%r)[0].split('/')[-1])\n"
                   "print('pes2015-vintage', g.find_database(%r)[1])\n"
                   ) % (pes15, pes15)
    out = subprocess.run([py, "-c", script], capture_output=True, text=True, timeout=900,
                         env=dict(os.environ, GDK_BACKEND="x11"))
    text = out.stdout + out.stderr
    assert "('pes2019'" in text, text[-400:]
    assert "13,805" in text, text[-400:]
    assert "'unreadable'" in text, text[-400:]
    assert "junk-dir True" in text, text[-400:]
    if os.path.isfile(empty):
        assert "file-pick dt10_x64.cpk" in text, text[-400:]
        assert "data-folder dt10_x64.cpk" in text, text[-400:]
    if pack and os.path.isfile(pack):
        assert f"install {os.path.basename(pack)}" in text, text[-500:]
        assert "install-vintage pes2019" in text, text[-500:]
        assert "install-ok True" in text, text[-500:]
    if spfl and os.path.isfile(os.path.join(spfl, "download", "data_s2627.cpk")):
        assert "spfl-root data_s2627.cpk" in text, text[-500:]
        assert "spfl-vintage pes2021" in text, text[-500:]
        assert "spfl-loadable True" in text, text[-500:]
        assert "spfl-data-pick data_s2627.cpk" in text, text[-500:]
    if pes15 and os.path.isfile(os.path.join(pes15, "Data", "dt10.cpk")):
        assert "pes2015-root dt10.cpk" in text, text[-500:]
        assert "pes2015-vintage pes2015" in text, text[-500:]
    try:
        os.unlink(junk)
    except OSError:
        pass


def test_gtk4_import_cycle_across_installs():
    """Import PES 2019, unload, import SP Football Life, unload, import PES 2015.

    The sequence as driven by hand, asserted: this build loads its own vintage, identifies
    the others by record size instead of mis-reading them, and unloads cleanly each time.
    """
    game = pes2019_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="cycle", PES2019_DIR=game,
               SPFL_DIR=settings.game_dir() or "")
    try:
        env["PES2015_DIR"] = pes2015_install()
    except FileNotFoundError:
        env["PES2015_DIR"] = ""
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST CYCLE OK" in text, text[-700:]
    assert "step=1 name=pes2019 result=loaded" in text, text[-700:]
    assert "step=1 name=pes2019 result=skipped" not in text, text[-700:]
    if env["SPFL_DIR"]:
        assert "step=2 name=spfl result=loaded" in text, text[-700:]
        assert "PES 2021" in text, text[-700:]
    if env["PES2015_DIR"]:
        assert "step=3 name=pes2015 result=loaded" in text, text[-700:]
        assert "PES 2015 · 10,091 players" in text, text[-700:]


def test_gtk4_clicking_a_player_shows_that_player():
    """The form follows the row that was clicked, not the previous selection.

    GtkSelectionModel::selection-changed hands over the position that was current *before*
    the change, so reading it left the form one click behind — reported as "not loading some
    players when selected". The click itself now drives the form.
    """
    game = pes2019_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="select", PES2019_DIR=game)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST SELECT OK" in text, text[-600:]
    assert "a click shows the row it hit: 7" in text, text[-600:]


def test_gtk4_import_hub_describes_a_pick():
    """The hub names what a pick holds and marks whether this build can open it."""
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="hub", PES2019_DIR=pes2019_install(),
               SPFL_DIR=settings.game_dir() or "")
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST HUB OK" in text, text[-600:]
    assert "a PES 2019 pack is loadable: True" in text, text[-600:]
    assert "a PES 2015 database is loadable: True" in text, text[-600:]
    if os.path.isfile(os.path.join(settings.game_dir() or "", "download", "data_s2627.cpk")):
        assert "SP Football Life is loadable: True" in text, text[-600:]


def test_gtk4_edits_and_saves_pes2015():
    """PES 2015 is editable: load the install, change a player, save into a copy, read it back.

    The check copies Data/dt10.cpk into a scratch folder shaped like an install first, and
    asserts the game's own database is byte-identical when it finishes.
    """
    game = pes2015_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="write2015", PES2015_DIR=game)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST WRITE2015 OK" in text, text[-700:]
    assert "Speed=" in text and "shirt=SELFTEST" in text, text[-700:]


# ------------------------------------------------------------------ PES 5
def pes5_install():
    """The PES 5 install, or a skip when this machine has none (set PES5_DIR)."""
    folder = os.environ.get("PES5_DIR") or pes5.find_game_dir()
    if not folder or not pes5.is_pes5_dir(folder):
        raise FileNotFoundError(2, "no PES 5 install (set PES5_DIR)", "pes5.exe")
    return folder


def pes5_option():
    """The option file to decode: the install's shipped copy (a template), else the live one."""
    try:
        shipped = os.path.join(pes5_install(), "misc", "Option File", pes5.OPT_NAME)
        if os.path.isfile(shipped):
            return shipped
    except FileNotFoundError:
        pass
    live = pes5.live_option_file()
    if not live:
        raise FileNotFoundError(2, "no PES 5 option file (set PES5_DIR)", pes5.OPT_NAME)
    return live


def test_pes5_reads_the_shipped_option_file():
    """Zidane and Buffon decode exactly, and the squads invert back into club rosters."""
    path = pes5_option()
    db = pes5.Cpk(path)
    assert db.bad_sums == [], db.bad_sums
    table = db.read_table("Player")
    assert len(table) == pes5.PLAYERS + pes5.CREATED, len(table)
    zid = table.find("Zidane")[0]
    assert zid == 205 and table.shirt(zid) == "Z I D A N E", (zid, table.shirt(zid))
    assert (table.get(zid, "Age"), table.get(zid, "Height"), table.get(zid, "Weight")) == \
        (33, 185, 78)
    assert pes5.NATION_NAMES[table.get(zid, "Nationality")] == "France"
    assert pes5.POSITION_NAMES[table.get(zid, "Position")] == "AM"
    assert table.get(zid, "Technique") == 99 and table.get(zid, "Dribble Accuracy") == 96
    assert table.get(zid, "Skill: Playmaking") == 1 and table.get(zid, "Skill: Dribbling") == 0
    gk = table.find("Buffon")[0]
    assert gk == 300 and table.get(gk, "GK Ability") == 97
    assert table.get(gk, "Skill: 1-on-1 Stopper") == 1
    teams = pes5.TeamTable(*db.read_raw("Team"))
    assert len(teams) == 138 and teams.name(0) == "Arsenal FC", len(teams)
    assign = pes5.AssignmentTable(*db.read_raw("PlayerAssignment"))
    arsenal = [(assign.number(r), table.name(assign.player(r)))
               for r in range(len(assign)) if assign.team(r) == teams.id(0)]
    assert arsenal[0] == (1, "Lehmann"), arsenal[:3]
    assert table.find("205") == [205] and table.find("32768") == [pes5.PLAYERS]


def test_pes5_skills_are_the_games_own_names():
    """The classic panel shows the abilities the games themselves name, in their order.

    The PESFan editor's table (Stats.java) abridged them — "Anti-Dribble" and "1-T Pass" are
    in neither game — and mapped byte 20 bit 7 as "GK Kick", which sits on 59.3% of the live
    roster (measured 2026-11-07) and on no edit menu. The names below are the games' own
    strings (dat/e_text.afs, byte-identical in PES 5 and PES 6).
    """
    assert pes5.SKILLS == (
        "Dribbling", "Tactical Dribble", "Positioning", "Reaction", "Playmaking", "Passing",
        "Scoring", "1-1 Scoring", "Post Player", "Lines", "Middle Shooting", "Side", "Centre",
        "Penalties", "1-Touch Pass", "Outside", "Marking", "Sliding Tackle", "Covering",
        "D-Line Control", "Penalty Stopper", "1-on-1 Stopper", "Long Throw")
    assert "Skill: GK Kick" not in pes5.FIELD_BY_LABEL


def test_pes5_fixture_round_trip_and_detection():
    """The codec on a synthetic option file: no game data needed, so CI gates on it.

    The raw signature the shipped files carry, a no-edit save that is byte identical, an
    edit confined to its own record, checksums that still pass after a write, and detection
    reading the bytes rather than the file's name.
    """
    box = tempfile.mkdtemp(prefix="pesforge-pes5-fixture-")
    try:
        path = os.path.join(box, "KONAMI-WIN32PES5OPT")
        fixtures.build_option_file(path)
        raw = open(path, "rb").read()
        assert len(raw) == pes5.FILESIZE, len(raw)
        assert raw[pes5.SIG_OFF:pes5.SIG_OFF + 4] == b"\xe7\xb0\x05\xfa", \
            raw[pes5.SIG_OFF:pes5.SIG_OFF + 4].hex()
        db = pes5.Cpk(path)
        assert db.bad_sums == []
        table = db.read_table("Player")
        assert len(table) == pes5.PLAYERS + pes5.CREATED
        assert table.name(1) == "PLAYER 0001" and table.shirt(1) == "PA"
        assert 40 <= table.get(1, "Attack") <= 99
        again = os.path.join(box, "again.bin")
        pes5.save(db.data, again)                     # no edits: byte for byte
        assert open(again, "rb").read() == raw
        orig = bytes(db.data)                         # the plaintext before the edit
        was = table.get(1, "Attack")
        table.set(1, "Attack", was + 1 if was < 99 else was - 1)
        db.patch("Player", table.to_payload(), backup_dir=os.path.join(box, "backups"))
        back = pes5.Cpk(path)
        assert back.bad_sums == [], back.bad_sums
        assert back.read_table("Player").get(1, "Attack") != was
        lo = pes5.BLOCK[4] + pes5.REC                       # record 1's own bytes
        hi = pes5.BLOCK[4] + 2 * pes5.REC
        checksum = (pes5.BLOCK[4] - 8, pes5.BLOCK[4] - 4)   # block 4's recomputed sum
        ranges = pes2015_diffs(orig, back.data)
        assert any(lo <= r[0] and r[1] <= hi for r in ranges), ranges
        assert all((lo <= r[0] and r[1] <= hi) or (checksum[0] <= r[0] and r[1] <= checksum[1])
                   for r in ranges), ranges
        names = pes5.backup_list(os.path.join(box, "backups"))
        assert names and os.path.basename(names[0]).startswith(pes5.OPT_NAME)
        pes5.restore_backup(names[0])
        assert open(path, "rb").read() == raw
        from pesforge_gtk import importing as imp       # no gi: the data layer, headless
        vintage, note = imp.probe_database(path)
        assert vintage == "pes5" and "5,184 players" in note, (vintage, note)
        renamed = os.path.join(box, "definitely-a-pes2019-pack.cpk")
        shutil.copy2(path, renamed)
        assert imp.probe_database(renamed)[0] == "pes5"
    finally:
        shutil.rmtree(box, ignore_errors=True)


def test_pes5_probe_and_resolution():
    """A folder pick resolves to the file the game reads, and junk is refused, not mis-read."""
    pes5_option()                                   # skip when this machine has none
    from pesforge_gtk import importing as imp
    live = imp.vintage_database("pes5")
    assert os.path.isfile(live), live
    assert os.path.basename(live) == pes5.OPT_NAME
    install = pes5_install()
    found, vintage, _note = imp.find_database(install)
    assert vintage == "pes5" and os.path.isfile(found), (found, vintage)
    details, can_load, target = imp.describe_pick(install)
    assert can_load and target and os.path.isfile(target), details
    junk = os.path.join(tempfile.gettempdir(), f"pesforge-not-an-option-{os.getpid()}.bin")
    with open(junk, "wb") as fh:
        fh.write(b"not an option file")
    try:
        assert imp.probe_database(junk)[0] != "pes5", imp.probe_database(junk)
    finally:
        os.unlink(junk)


def test_gtk4_edits_and_saves_pes5():
    """PES 5 is editable: load the option file, change a player, save into a copy, read it back.

    The check copies KONAMI-WIN32PES5OPT into a scratch folder first, and asserts the game's
    own file is byte-identical when it finishes.
    """
    pes5_option()                                   # skip when this machine has none
    game = pes5_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="write5", PES5_DIR=game)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST WRITE5 OK" in text, text[-700:]
    assert "Speed=" in text and "shirt=SELFTEST" in text, text[-700:]



# ------------------------------------------------------------------ PES 6
def pes6_install():
    """The PES 6 install, or a skip when this machine has none (set PES6_DIR)."""
    folder = os.environ.get("PES6_DIR") or pes6.find_game_dir()
    if not folder or not pes6.is_pes6_dir(folder):
        raise FileNotFoundError(2, "no PES 6 install (set PES6_DIR)", "pes6.exe")
    return folder


def pes6_option():
    """The option file to decode: the install's shipped copy (a template), else the live one."""
    try:
        shipped = os.path.join(pes6_install(), "misc", "Option File", pes6.OPT_NAME)
        if os.path.isfile(shipped):
            return shipped
    except FileNotFoundError:
        pass
    live = pes6.live_option_file()
    if not live:
        raise FileNotFoundError(2, "no PES 6 option file (set PES6_DIR)", pes6.OPT_NAME)
    return live


def test_pes6_reads_the_shipped_option_file():
    """Messi and Buffon decode exactly, and the squads invert back into club rosters."""
    path = pes6_option()
    db = pes6.Cpk(path)
    assert db.bad_sums == [], db.bad_sums
    table = db.read_table("Player")
    assert len(table) == pes6.PLAYERS + pes6.CREATED, len(table)
    messi = table.find("Messi")[0]
    assert messi == 1033 and (table.get(messi, "Age"), table.get(messi, "Height")) == (21, 170)
    assert pes6.NATION_NAMES[table.get(messi, "Nationality")] == "Argentina"
    assert pes6.POSITION_NAMES[table.get(messi, "Position")] == "SS"
    assert table.get(messi, "Attack") == 95 and table.get(messi, "Dribble Accuracy") == 95
    assert table.get(messi, "Skill: Dribbling") == 1
    gk = table.find("Buffon")[0]
    assert gk == 300 and table.get(gk, "GK Ability") == 94
    assert table.get(gk, "Skill: 1-on-1 Stopper") == 1
    teams = pes6.TeamTable(*db.read_raw("Team"))
    assert len(teams) == 140 and teams.name(0) == "Arsenal FC", len(teams)
    assign = pes6.AssignmentTable(*db.read_raw("PlayerAssignment"))
    united = {assign.number(r): table.name(assign.player(r))
              for r in range(len(assign)) if assign.team(r) == 73 + 10}
    assert (united.get(7), united.get(10)) == ("C. Ronaldo", "Rooney"), sorted(united.items())[:6]
    assert table.find("1033") == [1033] and table.find("32768") == [pes6.PLAYERS]


def test_pes6_skills_are_the_games_own_names():
    """PES 6's specials are the same games' own list as PES 5's, on its own bits."""
    assert pes6.SKILLS == pes5.SKILLS
    assert "Skill: GK Kick" not in pes6.FIELD_BY_LABEL


def test_pes6_fixture_round_trip_and_detection():
    """The codec on a synthetic option file: no game data needed, so CI gates on it.

    The raw signature the shipped files carry, a no-edit save that is byte identical, an
    edit confined to its own record, checksums that still pass after a write, and detection
    reading the bytes rather than the file's name.
    """
    box = tempfile.mkdtemp(prefix="pesforge-pes6-fixture-")
    try:
        path = os.path.join(box, "KONAMI-WIN32PES6OPT")
        fixtures.build_option_file6(path)
        raw = open(path, "rb").read()
        assert len(raw) == pes6.FILESIZE, len(raw)
        assert raw[pes6.SIG_OFF:pes6.SIG_OFF + 4] == b"\xc3\xb0\x05\xfa", raw[pes6.SIG_OFF:pes6.SIG_OFF + 4].hex()
        db = pes6.Cpk(path)
        assert db.bad_sums == []
        table = db.read_table("Player")
        assert len(table) == pes6.PLAYERS + pes6.CREATED
        assert table.name(1) == "PLAYER 0001" and table.shirt(1) == "PA"
        assert 40 <= table.get(1, "Attack") <= 99
        again = os.path.join(box, "again.bin")
        pes6.save(db.data, again)                     # no edits: byte for byte
        assert open(again, "rb").read() == raw
        orig = bytes(db.data)                         # the plaintext before the edit
        was = table.get(1, "Attack")
        table.set(1, "Attack", was + 1 if was < 99 else was - 1)
        db.patch("Player", table.to_payload(), backup_dir=os.path.join(box, "backups"))
        back = pes6.Cpk(path)
        assert back.bad_sums == [], back.bad_sums
        assert back.read_table("Player").get(1, "Attack") != was
        lo = pes6.BLOCK[4] + pes6.REC                       # record 1's own bytes
        hi = pes6.BLOCK[4] + 2 * pes6.REC
        checksum = (pes6.BLOCK[4] - 8, pes6.BLOCK[4] - 4)   # block 4's recomputed sum
        ranges = pes2015_diffs(orig, back.data)
        assert any(lo <= r[0] and r[1] <= hi for r in ranges), ranges
        assert all((lo <= r[0] and r[1] <= hi) or (checksum[0] <= r[0] and r[1] <= checksum[1])
                   for r in ranges), ranges
        names = pes6.backup_list(os.path.join(box, "backups"))
        assert names and os.path.basename(names[0]).startswith(pes6.OPT_NAME)
        pes6.restore_backup(names[0])
        assert open(path, "rb").read() == raw
        from pesforge_gtk import importing as imp       # no gi: the data layer, headless
        vintage, note = imp.probe_database(path)
        assert vintage == "pes6" and "5,184 players" in note, (vintage, note)
        renamed = os.path.join(box, "definitely-a-pes2019-pack.cpk")
        shutil.copy2(path, renamed)
        assert imp.probe_database(renamed)[0] == "pes6"
    finally:
        shutil.rmtree(box, ignore_errors=True)


def test_pes6_probe_and_resolution():
    """A folder pick resolves to the file the game reads, and junk is refused, not mis-read."""
    pes6_option()                                   # skip when this machine has none
    from pesforge_gtk import importing as imp
    live = imp.vintage_database("pes6")
    assert os.path.isfile(live), live
    assert os.path.basename(live) == pes6.OPT_NAME
    install = pes6_install()
    found, vintage, _note = imp.find_database(install)
    assert vintage == "pes6" and os.path.isfile(found), (found, vintage)
    details, can_load, target = imp.describe_pick(install)
    assert can_load and target and os.path.isfile(target), details
    junk = os.path.join(tempfile.gettempdir(), f"pesforge-not-an-option6-{os.getpid()}.bin")
    with open(junk, "wb") as fh:
        fh.write(b"not an option file")
    try:
        assert imp.probe_database(junk)[0] != "pes6", imp.probe_database(junk)
    finally:
        os.unlink(junk)


def test_gtk4_edits_and_saves_pes6():
    """PES 6 is editable: load the option file, change a player, save into a copy, read it back.

    The check copies KONAMI-WIN32PES6OPT into a scratch folder first, and asserts the game's
    own file is byte-identical when it finishes.
    """
    pes6_option()                                   # skip when this machine has none
    game = pes6_install()
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="write6", PES6_DIR=game)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=900)
    text = out.stdout + out.stderr
    assert "SELFTEST WRITE6 OK" in text, text[-700:]
    assert "Speed=" in text and "shirt=SELFTEST" in text, text[-700:]


def test_system_theme_matches_the_desktop():
    """The app paints as the desktop actually declares itself, not as a False-by-default flag says.

    `gtk-application-prefer-dark-theme` stays False even under Adwaita-dark, so reading only that
    flag sent a dark desktop the light palette: dark ink into the theme's dark value boxes, where
    every spinbutton then looked empty. The palette must agree with what the desktop declares.
    """
    py = gtk4_python()
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("no display for the app")
    scheme = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                            capture_output=True, text=True).stdout.strip().strip("'")
    if scheme not in ("prefer-dark", "prefer-light"):
        return                                     # nothing declared: the app's own default stands
    sys.path.insert(0, ROOT)
    import settings as _settings
    pinned = str(_settings.load().get("mode", "")).strip().lower()
    env = dict(os.environ, PESFORGE_GTK_SELFTEST="theme")
    env.pop("PESFORGE_THEME", None)
    out = subprocess.run([py, os.path.join(ROOT, "editor_gtk.py")], capture_output=True,
                         text=True, env=env, timeout=300)
    text = out.stdout + out.stderr
    assert "SELFTEST THEME" in text, text[-500:]
    line = [ln for ln in text.splitlines() if ln.startswith("SELFTEST THEME")][0]
    mode = line.split()[2]
    # a mode the user pinned in the app's own settings outranks what the desktop declares
    want = pinned if pinned in ("light", "dark") else ("dark" if scheme == "prefer-dark" else "light")
    assert mode == want, f"desktop declares {scheme}, the app painted {mode}: {line}"


def test_gtk4_value_fields_clamp_and_step():
    """A value field steps by one (five with Shift) and never leaves its own range.

    The stepping lives in `step_widget`; the wheel and arrow controllers call it, so this pins the
    arithmetic and the clamp without a keyboard.
    """
    mod = importlib.import_module("editor_gtk")
    real = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not real:
        raise RuntimeError("no display for a GTK widget")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk  # noqa:  the widget needs the binding loaded
    Gtk.init()
    spin = Gtk.SpinButton.new_with_range(40, 99, 1)
    spin.set_value(97)
    mod.step_widget(spin, 1)
    assert spin.get_value() == 98, spin.get_value()
    mod.step_widget(spin, 5)
    assert spin.get_value() == 99, f"the range must clamp: {spin.get_value()}"
    mod.step_widget(spin, -5)
    assert spin.get_value() == 94, spin.get_value()
    for _ in range(60):
        mod.step_widget(spin, -1)
    assert spin.get_value() == 40, f"the floor must hold: {spin.get_value()}"
    bio = Gtk.SpinButton.new_with_range(150, 215, 1)
    bio.set_value(214)
    mod.step_widget(bio, 5)
    assert bio.get_value() == 215, bio.get_value()

# ------------------------------------------------------- synthetic fixtures (no game data)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures                                              # noqa: E402


@contextlib.contextmanager
def fixture_install(vintage, players=320, seed=7):
    """One release's synthetic install in a scratch dir, with the state dir redirected."""
    box = tempfile.mkdtemp(prefix=f"pesforge-fixture-{vintage}-")
    saved = os.environ.get("SPFL_STATE")
    os.environ["SPFL_STATE"] = os.path.join(box, "state")
    os.makedirs(os.environ["SPFL_STATE"], exist_ok=True)
    try:
        yield fixtures.build_install(box, vintage, players, seed), box
    finally:
        if saved is None:
            os.environ.pop("SPFL_STATE", None)
        else:
            os.environ["SPFL_STATE"] = saved
        shutil.rmtree(box, ignore_errors=True)


def test_fixture_containers_are_valid():
    """Every fixture is a well-formed container: independent reader, alignment, no overlap."""
    for vintage in ("pes2015", "pes2019", "pes2021"):
        with fixture_install(vintage) as (path, _box):
            regions = fixtures.validate_cpk(path, vintage)
            names = {name for _s, _e, name in regions}
            assert names == {"Player.bin", "Team.bin", "PlayerAssignment.bin"}, names


def test_fixture_build_is_deterministic():
    """The same seed builds the same bytes, so a fixture failure is reproducible."""
    with fixture_install("pes2019") as (path, box):
        again = fixtures.build_cpk("pes2019", os.path.join(box, "again.cpk"))
        with open(path, "rb") as a, open(again, "rb") as b:
            assert a.read() == b.read(), "the fixture generator is not deterministic"


def test_fixture_no_edit_save_returns_the_original_bytes():
    """Load then save with no edits: byte-identical, because the re-pack is deterministic.

    The payloads are written by the same writers the app saves with, so the decoded payload
    and the container are both identical — the stronger form of the round trip.
    """
    for vintage in ("pes2015", "pes2019", "pes2021"):
        with fixture_install(vintage) as (path, box):
            mod = fixtures.MODS[vintage]
            with open(path, "rb") as f:
                before = f.read()
            db = mod.Cpk(path)
            table = db.read_table("Player")
            db.patch("Player", table.to_payload(), backup_dir=os.path.join(box, "backups"))
            with open(path, "rb") as f:
                assert f.read() == before, f"{vintage}: a no-edit save moved bytes"
            records, _prefix = fixtures.player_records(path, vintage)
            assert records == [bytes(r) for r in table.records], f"{vintage}: rows differ"
            backups = mod.backup_list(os.path.join(box, "backups"))
            assert backups, f"{vintage}: the save took no backup"
            for name in backups:
                mod.restore_backup(name)
            with open(path, "rb") as f:
                assert f.read() == before, f"{vintage}: restore did not return the bytes"


def test_fixture_single_field_edit_moves_only_its_record_and_span():
    """One field on one record: the record moves, and only the bytes its map covers."""
    for vintage in ("pes2015", "pes2019", "pes2021"):
        with fixture_install(vintage) as (path, box):
            mod = fixtures.MODS[vintage]
            db = mod.Cpk(path)
            table = db.read_table("Player")
            before, _prefix = fixtures.player_records(path, vintage)
            i, label = 7, "Speed"
            was = table.get(i, label)
            table.set(i, label, was + 1)
            db.patch("Player", table.to_payload(), backup_dir=os.path.join(box, "backups"))
            after, _prefix = fixtures.player_records(path, vintage)
            assert len(after) == len(before), f"{vintage}: the row count changed"
            moved = [k for k in range(len(before)) if before[k] != after[k]]
            assert moved == [i], f"{vintage}: records that moved: {moved}"
            bit, width, _add = mod.FIELD_BY_LABEL[label]
            lo, hi = bit // 8, (bit + width + 7) // 8
            span = [k for k in range(len(before[i])) if before[i][k] != after[i][k]]
            assert span and all(lo <= k < hi for k in span), (vintage, span, (lo, hi))
            assert mod.Cpk(path).read_table("Player").get(i, label) == was + 1


def test_fixture_batch_edit_moves_only_those_records():
    """A batch write over N rows changes those N records and nothing else."""
    picks = [3, 9, 40, 41, 300]
    for vintage in ("pes2015", "pes2019", "pes2021"):
        with fixture_install(vintage) as (path, box):
            mod = fixtures.MODS[vintage]
            db = mod.Cpk(path)
            table = db.read_table("Player")
            before, _prefix = fixtures.player_records(path, vintage)
            old = {i: table.get(i, "Speed") for i in picks}
            for i in picks:
                table.set(i, "Speed", min(99, old[i] + 2))
            db.patch("Player", table.to_payload(), backup_dir=os.path.join(box, "backups"))
            after, _prefix = fixtures.player_records(path, vintage)
            moved = [k for k in range(len(before)) if before[k] != after[k]]
            assert moved == picks, f"{vintage}: moved {moved}, wanted {picks}"
            back = mod.Cpk(path).read_table("Player")
            for i in picks:
                assert back.get(i, "Speed") == min(99, old[i] + 2), (vintage, i)


def test_fixture_release_detection_reads_bytes_not_names():
    """Three releases, a mislabeled file and a table-less stub all identify by their bytes."""
    py = gtk4_python()
    box = tempfile.mkdtemp(prefix="pesforge-fixture-probe-")
    try:
        for vintage in ("pes2015", "pes2019", "pes2021"):
            fixtures.build_install(os.path.join(box, vintage), vintage)
        fixtures.build_mislabeled(os.path.join(box, "mislabeled"))   # 2015 bytes, 2019 name
        fixtures.build_stub(os.path.join(box, "stub"))
        script = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
            "import editor_gtk as g\n"
            "for name in ('pes2015', 'pes2019', 'pes2021', 'mislabeled'):\n"
            "    path, vintage, _note = g.find_database(%r + '/' + name)\n"
            "    print('probe', name, vintage, bool(path))\n"
            "print('stub', g.probe_database(%r)[0])\n"
            "print('stub-folder', g.find_database(%r)[1])\n"
        ) % (ROOT, os.path.join(ROOT, "tests"), box,
             os.path.join(box, "stub", "Data", "dt00_4K_x64.cpk"), os.path.join(box, "stub"))
        out = subprocess.run([py, "-c", script], capture_output=True, text=True, timeout=300,
                             env=dict(os.environ, GDK_BACKEND="x11"))
        text = out.stdout + out.stderr
        for line in ("probe pes2015 pes2015 True", "probe pes2019 pes2019 True",
                     "probe pes2021 pes2021 True", "probe mislabeled pes2015 True",
                     "stub no player table", "stub-folder no player table"):
            assert line in text, f"{line!r} missing from:\n{text[-600:]}"
    finally:
        shutil.rmtree(box, ignore_errors=True)


def test_data_layer_imports_stay_toolkit_free():
    """The data layer must not import gi (it has to run headless, e.g. in CI)."""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import cpk, pesdb, pesdb_maps, pes5, pes6, pes2015, pes2019, pes2021, settings, theme\n"
        "bad = [m for m in ('gi', 'gi.repository') if m in sys.modules]\n"
        "print('toolkit-free', not bad, bad)\n"
    ) % ROOT
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         timeout=120)
    text = out.stdout + out.stderr
    assert "toolkit-free True" in text, text[-500:]


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    # PESFORGE_TEST_FILTER=gtk4,fixture narrows a local run to the named tests; CI runs all
    want = os.environ.get("PESFORGE_TEST_FILTER", "").strip()
    if want:
        parts = [p for p in want.split(",") if p]
        tests = [(n, f) for n, f in tests if any(p in n for p in parts)]
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

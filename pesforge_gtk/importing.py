"""Identify a pick — a CPK or a folder — and the release behind it.

Nothing here trusts a filename: the release is read out of the tables themselves (the
record size of the `Player` table is the signature), and a pick that carries no table is
answered with the file that does.
"""
import os

import pes2015
import pes2019
import pes2021
import pes5
import pesdb
import settings

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
    # The classic era keeps its whole database in one file outside the install (see pes5.py);
    # there is no installed path to name, so game_file is empty and the module resolves it.
    "pes5": {"label": "PES 5", "module": pes5, "env": "PES5_DIR",
             "cfg_key": "pes5_dir", "exe": ("pes5.exe",)},
}
EDITABLE = tuple(VINTAGES)
VINTAGE_NAMES = {"pes2019": "PES 2019", "pes2015": "PES 2015", "pes5": "PES 5",
                 "pes2021": "SP Football Life / PES 2021"}
PROBE_LIMIT = 12          # CPKs to open before calling a folder empty: the ranking puts the
                          # real candidates first, and a game folder holds 40+ entries
NOT_READ = ("Reads CRI CPK databases — PES 2021 / SP Football Life, PES 2019 and PES 2015 "
            "player records — and the classic PES 5 option file, KONAMI-WIN32PES5OPT "
            "(124-byte records). PES save files (EDIT00000000), .ted/.ced containers and "
            "spreadsheets are not read at all.")

GAME = os.environ.get("PES2019_DIR") or pes2019.find_game_dir()


def game_dir(vintage):
    """One release's install: its environment variable, the remembered folder, then a search."""
    spec = VINTAGES[vintage]
    found = os.environ.get(spec["env"]) or settings.load().get(spec["cfg_key"], "")
    return found if found and os.path.isdir(found) else (spec["module"].find_game_dir() or "")


def vintage_database(vintage):
    """The database the game itself loads for a release: the file it names, else what it ships."""
    game = game_dir(vintage)
    if not game:
        return ""
    spec = VINTAGES[vintage]
    named = os.path.join(game, spec["game_file"]) if spec.get("game_file") else ""
    if named and os.path.isfile(named):
        return named
    packs = spec["module"].database_cpks(game)
    return packs[0] if packs else named


def install_of(path):
    """The install a database CPK lives in, for finding the other copies a save must write."""
    folder = os.path.dirname(os.path.abspath(path))              # .../download or .../Data
    return os.path.dirname(folder) if os.path.basename(folder) in ("download", "Data") else folder


def probe_database(path):
    """(vintage, note) for a database file, judged by the shape of its Player table.

    Each vintage has its own container quirks, so the readers are tried in turn and the
    first that yields a whole table wins. The record size is the release's signature:
    124 bytes PES 5's option file, 180 PES 2015, 188 PES 2019, 312 PES 2020/2021. Detection
    has to read the table: filenames moved between releases and a PES 2015 pack also ships
    a Player.bin. The PES 5 reader checks size and signature first, so a CPK costs it one
    stat call.
    """
    attempts = (("pes5", pes5.REC, lambda q: pes5.Cpk(q).read_raw("Player")[0]),
                ("pes2019", pes2019.REC, lambda q: pes2019.Cpk2019(q).read_raw("Player")[0]),
                ("pes2015", pes2015.REC, lambda q: pes2015.Cpk2015(q).read_raw("Player")[0]),
                ("pes2021", pesdb.REC, lambda q: pesdb.table_rows(pesdb.table_blob(q, "Player"))))
    missing, reasons = 0, []
    for vintage, rec, read in attempts:
        try:
            payload = read(path)
        except pes5.NotAnOptionFile:         # a CPK is no option file: nothing here to find
            missing += 1
            continue
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
    around it (its `Data/` holds 47 CPKs, its `download/` holds the real one). A PES 5 install
    has no CPK at all: its answer is the option file the game reads, which the module resolves
    (usually outside the install, under Documents).
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
    option = pes5.option_file(folder)
    if option:
        vintage, note = probe_database(option)
        if vintage in VINTAGES:
            return option, vintage, note
    looked = ", ".join(sorted({os.path.dirname(p) or p for p in files})) or os.path.abspath(folder)
    note = f"no CPK with a player table in {looked}"
    if install:
        note += f", nor in {install}"
    return "", "no player table", note


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
    import sys
    args = sys.argv[1:]
    if "--db" in args and args.index("--db") + 1 < len(args):
        return args[args.index("--db") + 1]
    return ""


def default_database(game_dir):
    """The CPK the game itself loads from an install: the mounted pack, else the base table."""
    packs = pes2019.database_cpks(game_dir) if game_dir else []
    return next((p for p in reversed(packs) if p.endswith("dt80_200E_x64.cpk")),
                packs[0] if packs else "")

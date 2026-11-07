"""SP Football Life / PES 2021 database: pesdb's 312-byte map in the vintage panel's shape.

`pesdb.py` holds the map and the CPK/WESYS mechanics — it is the PES 2021 editor the app and
`cli.py` drive. This module is only the interface `editor_gtk.py` asks of a release, the way
`pes2015.py` and `pes2019.py` already provide it, so one panel edits all three vintages:

    Cpk / PlayerTable / TeamTable / AssignmentTable, REC, ABILITIES, POSITION_NAMES, SKILLS,
    COM_STYLES, STYLE_NAMES, FIELD_BY_LABEL, NAME_SLOTS, database_cpks, find_game_dir,
    backup_list / restore_backup

The panel's vocabulary is the older modules', so three things are aliases here rather than a
second map: it asks for "Height" / "Weight" / "Foot" / "Position" where pesdb files those as
"Height (cm)" / "Weight (kg)" / "Stronger Foot" / "Registered Position"; chips are labelled
"Skill: X" / "Style: X" by both other modules, so the skill bits are re-keyed with the colon;
and this record has no Japanese name slot, so the panel leaves that row out (NAME_SLOTS).

Writes go through `pesdb.patch_table` — in place while the re-packed table fits its slot, else
appended at the end of the CPK with that entry repointed — and the CPK is copied first. The
database pack is 3.3 MB, so a whole-file backup is cheaper than deriving regions, and the sidecar
next to it is what `restore_backup` needs to put it back.
"""
import glob
import json
import os
import shutil
import struct
import time

import pes2019  # only for mounted_packs/DpFileList: the container format is the same
import pesdb
import settings

REC = pesdb.REC                                    # 312-byte player records

ABILITIES = tuple(label for label, _bit, _w, _add in pesdb.ATTRS)
POSITION_NAMES = tuple(pesdb.POS_NAMES)
SKILLS = tuple(label for label, _bit in pesdb.SKILLS)
# The record encodes no separate COM-style block: the forty-eight skill bits are the chips.
COM_STYLES = ()

# PES 2021's own style enum, the order the game lists it in: the 22 named styles, then the
# unnamed remainder of the 5-bit field (PLAY_STYLES carries the same 32, and
# tests/run_all.py pins the two lists to each other).
STYLE_NAMES = ("None", "Goal Poacher", "Dummy Runner", "Fox in the Box", "Prolific Winger",
               "Classic No. 10", "Hole Player", "Box-to-Box", "Anchor Man", "The Destroyer",
               "Extra Frontman", "Offensive Full-back", "Defensive Full-back", "Target Man",
               "Creative Playmaker", "Build Up", "Offensive Goalkeeper",
               "Defensive Goalkeeper", "Roaming Flank", "Cross Specialist", "Orchestrator",
               "Full-back Finisher",
               "None 22", "None 23", "None 24", "None 25", "None 26", "None 27", "None 28",
               "None 29", "None 30", "None 31")

# The player rows both vintages' panels show; the record has no third (Japanese) slot.
NAME_SLOTS = (("Name", "full"), ("Shirt Name", "shirt"))

# label -> (bit, width, add) in the panel's vocabulary, over pesdb's own map.
FIELD_BY_LABEL = dict(pesdb.FIELD_BY_LABEL)
FIELD_BY_LABEL.update({
    "Height": pesdb.FIELD_BY_LABEL["Height (cm)"],
    "Weight": pesdb.FIELD_BY_LABEL["Weight (kg)"],
    "Foot": pesdb.FIELD_BY_LABEL["Stronger Foot"],
    "Position": pesdb.FIELD_BY_LABEL["Registered Position"],
})
for _label, _bit in pesdb.SKILLS:
    FIELD_BY_LABEL["Skill: " + _label] = (_bit, 1, 0)

ALIASES = {"Height": "Height (cm)", "Weight": "Weight (kg)", "Foot": "Stronger Foot",
           "Position": "Registered Position"}
SKILL_PREFIX = "Skill: "


def pesdb_label(label):
    """A panel label -> the same field's name inside pesdb (unchanged when it already matches)."""
    if label.startswith(SKILL_PREFIX):
        return "Skill " + label[len(SKILL_PREFIX):]
    return ALIASES.get(label, label)


def field_range(label):
    """(low, high) for a numeric field: abilities stop at 99 however wide their bits are."""
    _bit, width, add = FIELD_BY_LABEL[label]
    hi = add + (1 << width) - 1
    if label in ABILITIES:
        hi = min(hi, 99)
    return add, hi


class PlayerTable(pesdb.PlayerTable):
    """Player.bin's 312-byte records, with the panel's own method names.

    `prefix` is ignored: the panel splats `(blob, prefix)` into a table class, and pesdb's
    classes read both the lead block and the WESYS prefix out of the blob itself.
    """

    def __init__(self, raw_file, prefix=None):
        super().__init__(raw_file)

    def __len__(self):
        return len(self.records)

    def get(self, i, label):
        return super().get(i, pesdb_label(label))

    def japanese(self, i):
        """This record has no Japanese name slot (the panel hides that row for this release)."""
        return ""

    def set(self, i, label, value):
        lo, hi = field_range(label)
        super().set(i, pesdb_label(label), max(lo, min(hi, int(value))))

    def set_name(self, i, full=None, shirt=None, japanese=None):
        super().set_name(i, full=full, shirt=shirt)      # japanese: nothing to write

    def find(self, needle):
        """Row indexes for a player id (digits) or a name / shirt-name substring."""
        needle = needle.strip()
        if needle.isdigit():
            pid = int(needle)
            return [i for i in range(len(self.records)) if self.pid(i) == pid]
        low = needle.lower()
        return [i for i in range(len(self.records))
                if low in self.name(i).lower() or low in self.shirt(i).lower()]

    def to_payload(self):
        """The whole table file (lead block + WESYS rows), as patch_table/patch() expect."""
        return self.to_file()


class TeamTable(pesdb.TeamTable):
    """Team.bin rows, with the id accessor the panel expects (`id`, not `tid`)."""

    def __init__(self, raw_file, prefix=None):
        super().__init__(raw_file)

    def __len__(self):
        return len(self.records)

    def id(self, i):
        return self.tid(i)


class AssignmentTable(pesdb.AssignmentTable):
    """PlayerAssignment.bin rows, with the panel's accessor names."""

    def __init__(self, raw_file, prefix=None):
        super().__init__(raw_file)

    def __len__(self):
        return len(self.records)

    def player(self, i):
        return self.pid(i)

    def number(self, i):
        return self.shirt_number(i)

    def find(self, pid):
        pid = int(pid)
        return [i for i in range(len(self.records)) if self.pid(i) == pid]


TABLES = {"Player": PlayerTable, "Team": TeamTable, "PlayerAssignment": AssignmentTable}


class Cpk:
    """One database CPK: the reader/writer shape the panel asks a release for."""

    def __init__(self, path):
        self.path = path

    def read_raw(self, stem):
        """(the table's whole file blob, its WESYS prefix) — what the panel splats into a class.

        Unlike the older vintages' readers the rows never travel alone: pesdb's table classes
        keep the file's lead block and re-pack the rows into it, so they take the whole blob.
        """
        blob = pesdb.table_blob(self.path, stem)
        return blob, pesdb.table_prefix(blob)

    def read_table(self, stem):
        """A table object for one stem; KeyError when this CPK has no such entry."""
        try:
            cls = TABLES[stem]
        except KeyError:
            raise KeyError(f"{stem} is not a table this build reads") from None
        return cls(*self.read_raw(stem))

    def patch(self, stem, payload, backup_dir=None):
        """Write one table back, backing the CPK up first."""
        if backup_dir:
            backup_file(self.path, backup_dir)
        return pesdb.patch_table(self.path, stem, payload,
                                 slots_path=os.path.join(settings.state_dir(), "slots.json"))


def backup_file(cpk_path, backup_dir):
    """Whole-file backup + a sidecar naming the source; returns the backup's base path."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(backup_dir, "%s.%s" % (os.path.basename(cpk_path)[:-4], stamp))
    n = 1
    while os.path.exists(base + ".cpk"):
        n += 1
        base = os.path.join(backup_dir, "%s.%s-%d" % (os.path.basename(cpk_path)[:-4], stamp, n))
    shutil.copy2(cpk_path, base + ".cpk")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"kind": "pes2021", "cpk": os.path.abspath(cpk_path), "stamp": stamp,
                   "file_size": os.path.getsize(cpk_path)}, f, indent=2)
    return base


def backup_list(backup_dir):
    """This vintage's backups (newest first) under a backups directory."""
    out = os.path.expanduser(backup_dir)
    if not os.path.isdir(out):
        return []
    bases = []
    for name in os.listdir(out):
        if not name.endswith(".json"):
            continue
        path = os.path.join(out, name[:-5])
        try:
            with open(path + ".json", encoding="utf-8") as f:
                if json.load(f).get("kind") != "pes2021":
                    continue
        except (OSError, ValueError):
            continue
        if os.path.isfile(path + ".cpk"):
            bases.append(path)
    bases.sort(key=lambda p: os.path.getmtime(p + ".json"), reverse=True)
    return bases


def restore_backup(base):
    """Put one whole-file backup back over the CPK it came from."""
    base = base[:-5] if base.endswith(".json") else base
    with open(base + ".json", encoding="utf-8") as f:
        meta = json.load(f)
    shutil.copy2(base + ".cpk", meta["cpk"])
    return meta


# ---------------------------------------------------------------- install discovery
GAME_DIRS = (
    "~/Games/SP Football Life*",
    "~/.wine/drive_c/Program Files*/SP Football Life*",
    "~/.wine/drive_c/Program Files*/*/SP Football Life*",
    "~/.local/share/wineprefixes/*/drive_c/Program Files*/SP Football Life*",
    "~/.wine/drive_c/Program Files (x86)/Pro Evolution Soccer 2021",
    "~/.steam/steam/steamapps/common/eFootball PES 2021*",
    "~/.local/share/Steam/steamapps/common/eFootball PES 2021*",
)


def is_2021_dir(path):
    """True for an install that carries a PES 2021 database pack."""
    return bool(path) and bool(database_cpks(path))


def find_game_dir():
    """An installed PES 2021 / SP Football Life folder (SPFL_DIR wins), or ''."""
    env = os.environ.get("SPFL_DIR")
    candidates = ([env] if env else []) + [os.path.expanduser(p) for p in GAME_DIRS]
    for candidate in candidates:
        for path in glob.glob(candidate):
            if is_2021_dir(path):
                return path
    return ""


def database_cpks(game_dir):
    """The CPKs holding this install's database, the one the game reads first.

    SP Football Life ships `download/data_s26XX.cpk` — the file `settings.database_path` picks
    and the one `cli.py` writes. The stock `Data/dt10_x64.cpk` and the tiny `SMK_*`/`01_smkdb_*`
    stubs beside it carry no player table, so a save skips them by failing to read the table.
    """
    download = os.path.join(os.path.expanduser(game_dir or ""), "download")
    try:
        names = [n for n in os.listdir(download)
                 if n.startswith("data_s") and n.endswith(".cpk")]
    except OSError:
        names = []
    names.sort(key=lambda n: (n != "data_s2627.cpk", n))
    return [os.path.join(download, n) for n in names]


def open_players(paths):
    """[(cpk_path, "Player", PlayerTable)] for every database in the given CPKs."""
    out = []
    for path in paths:
        try:
            out.append((path, "Player", Cpk(path).read_table("Player")))
        except (KeyError, OSError, ValueError):
            continue
    return out

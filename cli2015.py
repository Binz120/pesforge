#!/usr/bin/env python3
"""Headless PES 2015 side of PESForge: read and edit the player tables in dt10.cpk / dt80 packs.

    python3 cli2015.py --find "messi"
    python3 cli2015.py --show 7511
    python3 cli2015.py --set "L. Messi" Speed=90 Position=SS
    python3 cli2015.py --set 7511 "Ball Control=99" --save
    python3 cli2015.py --export players.csv
    python3 cli2015.py --tables
    python3 cli2015.py --backups
    python3 cli2015.py --restore last

Without --save every command is a dry run. An edit lands in every copy of the player — the
base Player.bin and each numbered update slot, in dt10.cpk and in the mounted data pack —
so the game shows it whichever table it reads. Close the game before saving.
"""
import csv
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pes2015
import settings

NAME_FIELDS = ("Name", "Shirt Name", "Japanese Name")
ATTR_FIELDS = tuple(pes2015.FIELD_BY_LABEL)


def canonical(label):
    """User spelling -> a field label (case/space insensitive)."""
    norm = {k.lower().replace(" ", "").replace("-", ""): k for k in ATTR_FIELDS}
    key = label.lower().replace(" ", "").replace("-", "")
    for name in NAME_FIELDS:
        norm[name.lower().replace(" ", "")] = name
    if key in norm:
        return norm[key]
    raise SystemExit(f"unknown field {label!r} — try --fields")


def parse_value(label, raw):
    """A --set value: numbers for everything, a position name or number for Position."""
    raw = raw.strip()
    if label == "Position":
        up = raw.upper()
        if up in pes2015.POSITION_NAMES:
            return pes2015.POSITION_NAMES.index(up)
        if raw.isdigit():
            return int(raw)
        raise SystemExit(f"position {raw!r} is not one of {'/'.join(pes2015.POSITION_NAMES)} "
                         f"or 0..12")
    if not raw.lstrip("-").isdigit():
        raise SystemExit(f"{label} needs a number, got {raw!r}")
    return int(raw)


def field_range(label):
    bit, width, add = pes2015.FIELD_BY_LABEL[label]
    return add, add + (1 << width) - 1


def open_dbs(game, cpk=None):
    paths = [cpk] if cpk else pes2015.database_cpks(game)
    return [(path, pes2015.Cpk2015(path)) for path in paths]


def gather(dbs):
    """[(cpk path, db, stem, PlayerTable)] — every readable Player*.bin, base tables first."""
    out = []
    for path, db in dbs:
        for stem in pes2015.PLAYER_STEMS:
            if db.entry(stem):
                out.append((path, db, stem, db.read_table(stem)))
    return out


def describe(table, i):
    pos = table.get(i, "Position")
    pos = pes2015.POSITION_NAMES[pos] if pos < len(pes2015.POSITION_NAMES) else "?"
    return (f"{table.pid(i):>7}  {table.name(i):<26} {pos:<4} "
            f"SPD {table.get(i, 'Speed'):>2}  BC {table.get(i, 'Ball Control'):>2}  "
            f"FIN {table.get(i, 'Finishing'):>2}  STA {table.get(i, 'Stamina'):>2}")


def gk(table, i):
    return (f"{table.pid(i):>7}  {table.name(i):<26} "
            f"GK {table.get(i, 'Goalkeeping'):>2}  SAV {table.get(i, 'Saving'):>2}")


def game_running():
    """True when PES2015.exe is in the process list (wine presents it under that name)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if "PES2015" in exe:
            return True
    return False


def table_rows(db, stem):
    """Row count from the payload header alone — no decompression."""
    _i, (_fn, off, _size, _x) = db.entry(stem)
    with open(db.path, "rb") as f:
        f.seek(off + pes2015.LEAD)
        head = f.read(16)
    return struct.unpack_from("<II", head, 8)[1] // pes2015.REC


def targets(game, cpk):
    """Human-readable list of the CPKs an edit would reach."""
    paths = [cpk] if cpk else pes2015.database_cpks(game)
    labels = []
    for path in paths:
        if os.path.basename(path) == "dt10.cpk":
            labels.append(f"{os.path.basename(path)} (base)")
        else:
            labels.append(f"{os.path.basename(path)} (mounted data pack)")
    return labels


def resolve_hits(tables, needle):
    """[(path, db, stem, table, [rows])] with exact name matches preferred over substrings."""
    out = []
    for path, db, stem, table in tables:
        rows = table.find(needle)
        exact = [i for i in rows if table.name(i).lower() == needle.strip().lower()
                 or table.shirt(i).lower() == needle.strip().lower()]
        if exact:
            rows = exact
        if rows:
            out.append((path, db, stem, table, rows))
    return out


def main(argv):
    save = "--save" in argv
    argv = [a for a in argv if a != "--save"]
    game = os.environ.get("PES2015_DIR") or pes2015.find_game_dir()
    cpk = None
    if "--game" in argv:
        k = argv.index("--game")
        game = argv[k + 1]
        del argv[k:k + 2]
    if "--cpk" in argv:
        k = argv.index("--cpk")
        cpk = argv[k + 1]
        del argv[k:k + 2]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not game or not os.path.isfile(os.path.join(game, "Data", "dt10.cpk")):
        raise SystemExit("no PES 2015 install found — set PES2015_DIR or pass --game DIR")

    if argv[0] == "--fields":
        for label in ATTR_FIELDS:
            lo, hi = field_range(label)
            print(f"{label:<24} {lo}..{hi}")
        print(f"{'Position':<24} 0..12 ({'/'.join(pes2015.POSITION_NAMES)})")
        print("names: Name, Shirt Name, Japanese Name (46 bytes max)")
        return 0

    dbs = open_dbs(game, cpk)

    if argv[0] == "--tables":
        print("editing target(s): " + ", ".join(targets(game, cpk)))
        for path, db in dbs:
            print(f"{os.path.basename(path)}:")
            for stem in pes2015.PLAYER_STEMS:
                if db.entry(stem):
                    print(f"  {stem + '.bin':<12} {table_rows(db, stem):>7,} rows")
        return 0

    tables = gather(dbs)

    if argv[0] == "--find":
        needle = " ".join(argv[1:])
        n = 0
        for path, db, stem, table, rows in resolve_hits(tables, needle):
            print(f"{os.path.basename(path)}  {stem}.bin")
            for i in rows[:20]:
                print("  " + describe(table, i))
            n += len(rows)
        print(f"{n} match(es)")
        return 0 if n else 2

    if argv[0] == "--show":
        needle = " ".join(argv[1:])
        hits = resolve_hits(tables, needle)
        if not hits:
            raise SystemExit("no match")
        seen = 0
        for path, db, stem, table, rows in hits:
            for i in rows:
                if table.get(i, "Position") == 0:
                    print(f"{os.path.basename(path)}  {stem}.bin")
                    print("  " + gk(table, i))
                else:
                    print(f"{os.path.basename(path)}  {stem}.bin")
                    print("  " + describe(table, i))
                for label in ATTR_FIELDS:
                    value = table.get(i, label)
                    if label == "Position":
                        value = (pes2015.POSITION_NAMES[value]
                                 if value < len(pes2015.POSITION_NAMES) else value)
                    print(f"    {label:<26} {value}")
                print(f"    {'Japanese Name':<26} {table.japanese(i)}")
                print(f"    {'Shirt Name':<26} {table.shirt(i)}")
                seen += 1
                if seen >= 4:
                    return 0
        return 0

    if argv[0] == "--set":
        needle = argv[1]
        if not argv[2:]:
            raise SystemExit('usage: --set "player" Field=Value ... [--save]')
        assignments = []
        for pair in argv[2:]:
            if "=" not in pair:
                raise SystemExit(f"expected field=value, got {pair!r}")
            label, raw = pair.split("=", 1)
            label = canonical(label)
            assignments.append((label, raw if label in NAME_FIELDS
                                else parse_value(label, raw)))
        hits = resolve_hits(tables, needle)
        if not hits:
            raise SystemExit(f"no player matching {needle!r}")
        pids = {table.pid(i) for _p, _d, _s, table, rows in hits for i in rows}
        if len(pids) != 1:
            print(f"{len(pids)} players match {needle!r} — be more specific:")
            for path, db, stem, table, rows in hits:
                for i in rows[:10]:
                    print("  " + describe(table, i))
            return 2
        pid = pids.pop()
        touched = []
        for path, db, stem, table, rows in hits:
            changes = []
            for i in rows:
                for label, value in assignments:
                    if label in NAME_FIELDS:
                        current = {"Name": table.name(i), "Shirt Name": table.shirt(i),
                                   "Japanese Name": table.japanese(i)}[label]
                        if value != current:
                            table.set_name(i, **{{"Name": "full", "Shirt Name": "shirt",
                                                  "Japanese Name": "japanese"}[label]: value})
                            changes.append((label, i, current, value))
                    else:
                        lo, hi = field_range(label)
                        value = max(lo, min(hi, value))
                        current = table.get(i, label)
                        if value != current:
                            table.set(i, label, value)
                            changes.append((label, i, current, value))
            if changes:
                touched.append((path, db, stem, table, changes))
        if not touched:
            print("nothing changed")
            return 0
        first_table, first_rows = hits[0][3], hits[0][4]
        print(f"{first_table.name(first_rows[0])} (id {pid})")
        for path, db, stem, table, changes in touched:
            print(f"  {os.path.basename(path)}  {stem}.bin")
            for label, i, old, new in changes:
                shown_old = pes2015.POSITION_NAMES[old] if label == "Position" else old
                shown_new = pes2015.POSITION_NAMES[new] if label == "Position" else new
                print(f"    {label:<26} {shown_old} -> {shown_new}")
        if not save:
            print("(dry run — add --save to write)")
            return 0
        if game_running():
            print("warning: PES2015.exe is running — the game keeps showing the old data "
                  "until it restarts, and a save while it reads can corrupt a table")
        backup_dir = settings.backups_dir()
        wrote = []
        for path, db, stem, table, changes in touched:
            where, appended = db.patch(stem, table.to_payload(), backup_dir=backup_dir)
            mode = "appended" if appended else "patched in place"
            print(f"  wrote {stem}.bin -> {os.path.basename(path)} ({mode} at {where:#x})")
            wrote.append((path, stem, changes))
        for path, stem, changes in wrote:
            fresh = pes2015.Cpk2015(path).read_table(stem)
            rows = {fresh.pid(i): i for i in range(len(fresh))}
            i = rows.get(pid)
            ok = i is not None and all(
                (fresh.name(i) if label == "Name" else
                 fresh.shirt(i) if label == "Shirt Name" else
                 fresh.japanese(i) if label == "Japanese Name" else
                 fresh.get(i, label)) == new for label, _row, _old, new in changes)
            print(f"  re-read {os.path.basename(path)}:{stem}.bin — "
                  f"{'all edits present' if ok else 'MISMATCH'}")
        return 0

    if argv[0] == "--export":
        path = argv[1]
        labels = [label for label in ATTR_FIELDS]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["cpk", "table", "row", "id", "name", "shirt", "japanese", "position"]
                       + labels)
            n = 0
            for cpk_path, db, stem, table in tables:
                for i in range(len(table)):
                    pos = table.get(i, "Position")
                    w.writerow([os.path.basename(cpk_path), stem, i, table.pid(i),
                                table.name(i), table.shirt(i), table.japanese(i),
                                pes2015.POSITION_NAMES[pos] if pos < len(pes2015.POSITION_NAMES)
                                else pos]
                               + [table.get(i, label) for label in labels])
                    n += 1
        print(f"exported {n:,} rows -> {path}")
        return 0

    if argv[0] == "--backups":
        backups = pes2015.backup_list(settings.backups_dir())
        for base in backups[:40]:
            with open(base + ".json", encoding="utf-8") as f:
                import json
                meta = json.load(f)
            print(f"{os.path.basename(base)}  {os.path.basename(meta['cpk'])}"
                  f":{meta['table']}  {meta['stamp']}")
        print(f"{len(backups)} backup(s) in {settings.backups_dir()}/pes2015")
        return 0

    if argv[0] == "--restore":
        backups = pes2015.backup_list(settings.backups_dir())
        if not backups:
            raise SystemExit("no backups to restore")
        if len(argv) < 2:
            raise SystemExit("usage: --restore <backup name|last>  (see --backups)")
        name = argv[1]
        if name == "last":
            chosen = backups[0]
        else:
            matches = [b for b in backups if os.path.basename(b).startswith(name)]
            if len(matches) != 1:
                raise SystemExit(f"{len(matches)} backups match {name!r} — see --backups")
            chosen = matches[0]
        meta = pes2015.restore_backup(chosen)
        print(f"restored {os.path.basename(meta['cpk'])}:{meta['table']} from "
              f"{os.path.basename(chosen)}")
        return 0

    raise SystemExit(f"unknown command {argv[0]!r} — see --help")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

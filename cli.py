"""Headless side of PESForge.

Everything here works on files only: nothing is read from or written to the game process,
so the game can be closed the whole time. Edits land in the Sider live-CPK override and are
picked up the next time the game starts.

    pesforge --find "messi"
    pesforge --show 7511
    pesforge --set "Lionel Messi" Speed=99 Finishing=95 Age=30
    pesforge --shirt "Lionel Messi" 10
    pesforge --coaches "arteta"
    pesforge --club-coach "Arsenal FC" "Mikel Arteta"
    pesforge --stadium "Arsenal FC" 15
    pesforge --export roster.csv
    pesforge --import edits.csv
    (add --save to actually write; without it every command is a dry run)
"""
import csv
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pesdb

import settings

CFG = settings.load()
GAME_DIR = settings.game_dir(CFG)
DATA_CPK = settings.database_path(GAME_DIR)
STATE_DIR = settings.state_dir()
BACKUP_DIR = settings.backups_dir(CFG, STATE_DIR)
VANILLA = os.path.join(STATE_DIR, "Player.vanilla.bin")

BOOL_FIELDS = {label: bit for label, bit in pesdb.SKILLS}
BOOL_FIELDS.update({"Pos " + l: b for l, b in pesdb.POSITIONS})


def load():
    """The editable player table, read from the CPK (edits are patched back into it).

    A live-CPK override must not be used: Sider serves pesdb tables from livecpk in a form the
    game cannot read, which shows every player as unregistered.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    if (not os.path.exists(VANILLA)
            or os.path.getmtime(VANILLA) < os.path.getmtime(DATA_CPK)):
        with open(VANILLA, "wb") as f:
            f.write(pesdb.table_blob(DATA_CPK, "Player"))
    return pesdb.PlayerTable(open(VANILLA, "rb").read())


def load_table(name, rec):
    """Editable non-player table, read from the CPK."""
    return pesdb.LeadTable(pesdb.table_blob(DATA_CPK, name), rec)


def load_clubs():
    return pesdb.club_names(pesdb.read_table(DATA_CPK, "Team"))


def resolve_club(clubs, needle):
    """One team id for a club name: exact match wins, else a unique substring."""
    want = needle.strip().lower()
    hits = [t for t, n in clubs.items() if n.lower() == want] or \
           [t for t, n in clubs.items() if want in n.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"no club matching {needle!r}")
    print(f"{len(hits)} clubs match {needle!r}: " + ", ".join(sorted(clubs[t] for t in hits)[:20]))
    raise SystemExit(2)


def player_index(table):
    return {table.pid(i): i for i in range(len(table.records))}


def canonical(label):
    """Map user spelling to a field label (case/space insensitive)."""
    norm = {k.lower().replace(" ", "").replace("-", ""): k for k in pesdb.FIELD_BY_LABEL}
    key = label.lower().replace(" ", "").replace("-", "")
    if key in norm:
        return norm[key]
    raise KeyError(f"unknown field {label!r} — try --fields")


def find(table, needle, exact=False):
    needle_l = needle.strip().lower()
    if needle_l.isdigit():
        pid = int(needle_l)
        return [i for i in range(len(table.records)) if table.pid(i) == pid]
    hits = []
    for i in range(len(table.records)):
        hay = (table.name(i) + " " + table.shirt(i)).lower()
        if (hay == needle_l) if exact else (needle_l in hay):
            hits.append(i)
    return hits


def describe(table, i):
    t = table
    pos = pesdb.POS_NAMES[t.get(i, "Registered Position")] \
        if t.get(i, "Registered Position") < len(pesdb.POS_NAMES) else "?"
    return (f"{t.pid(i):>6}  {t.name(i):<28} {pos:<3} age {t.get(i, 'Age'):>2}  "
            f"SPD {t.get(i, 'Speed'):>2}  FIN {t.get(i, 'Finishing'):>2}  "
            f"BC {t.get(i, 'Ball Control'):>2}")


def parse_assignment(pairs):
    out = []
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"expected field=value, got {p!r}")
        k, v = p.split("=", 1)
        out.append((canonical(k), v))
    return out


def apply(table, index, assignments, quiet=False):
    changed = []
    for label, raw in assignments:
        bit, width, add = pesdb.FIELD_BY_LABEL[label]
        cur = table.get(index, label)
        if label in BOOL_FIELDS and not raw.strip().lstrip("-").isdigit():
            val = 0 if raw.strip().lower() in ("0", "off", "no", "false", "no") else 1
        elif raw.strip().lstrip("-").isdigit():
            val = int(raw)
        else:
            val = 1 if raw.strip().lower() in ("yes", "on", "true", "y") else 0
        lo, hi = add, add + (1 << width) - 1
        val = max(lo, min(hi, val))
        if val != cur:
            table.set(index, label, val)
            changed.append((label, cur, val))
    return changed


def write(table, backup_dir=None, filename="Player.bin"):
    """Patch the table back into the CPK. Returns (cpk path, table bytes written)."""
    data = table.to_file()
    pesdb.patch_table(DATA_CPK, filename[:-4], data, backup_dir or BACKUP_DIR,
                      os.path.join(STATE_DIR, "slots.json"))
    return DATA_CPK, len(data)


def prune_backups():
    """Keep the newest N backups (Settings > Keep backups); 0 keeps everything."""
    n = settings.prune_backups(BACKUP_DIR, CFG["max_backups"])
    if n:
        print(f"  pruned {n} old backup(s) in {BACKUP_DIR}")


def export_csv(table, path):
    labels = [l for l, *_ in pesdb.ATTRS] + [l for l, *_ in pesdb.PHYSICAL] \
             + [l for l, *_ in pesdb.CAREER] \
             + ["Pos " + l for l, _ in pesdb.POSITIONS] + ["Skill " + l for l, _ in pesdb.SKILLS]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "shirt"] + labels)
        for i in range(len(table.records)):
            w.writerow([table.pid(i), table.name(i), table.shirt(i)]
                       + [table.get(i, l) for l in labels])
    return path, len(table.records)


def import_csv(table, path, quiet=False, append=False):
    labels = set(pesdb.FIELD_BY_LABEL)
    by_id = {}
    for i in range(len(table.records)):
        by_id[table.pid(i)] = i
    touched = 0
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        unknown = [c for c in reader.fieldnames or [] if c not in labels
                   and c not in ("id", "name", "shirt")]
        if unknown:
            raise SystemExit("unknown columns: " + ", ".join(unknown))
        for row in reader:
            pid = int(row.get("id") or 0)
            if pid not in by_id:
                if not append:
                    if not quiet:
                        print(f"  skip id {pid}: not in table (add --append to create it)")
                    continue
                rec = bytearray(pesdb.REC)
                struct.pack_into("<I", rec, 8, pid)
                table.records.append(rec)
                by_id[pid] = len(table.records) - 1
            i = by_id[pid]
            n = 0
            for col, raw in row.items():
                if col in ("id", "") or raw is None or raw == "":
                    continue
                if col in ("name", "shirt"):
                    continue
                n += len(apply(table, i, [(col, raw)], quiet=True))
            if row.get("name"):
                table.set_name(i, full=row["name"], shirt=table.shirt(i))
                n += 1
            if row.get("shirt"):
                table.set_name(i, full=table.name(i), shirt=row["shirt"])
                n += 1
            if n:
                touched += 1
    return touched


def export_assignments(table, clubs, pa, path):
    """One row per PlayerAssignment row: id, name, shirt, team_id, club, league_id."""
    by_pid = player_index(table)
    orphans = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "shirt", "team_id", "club", "league_id"])
        for row in pa.records:
            pid, team = struct.unpack_from("<II", row, 4)
            if not pid:
                continue
            i = by_pid.get(pid)
            if i is None:
                orphans += 1
            w.writerow([pid, table.name(i) if i is not None else "",
                        table.shirt(i) if i is not None else "",
                        team, clubs.get(team, ""), ""])
    return len(pa.records), orphans


def transfer(table, clubs, pa, player, club):
    """Move a player's first club assignment row (national-team rows are left alone)."""
    hits = find(table, player)
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} matches for player {player!r} — be more specific")
    i = hits[0]
    pid = table.pid(i)
    team = resolve_club(clubs, club)
    rows = pesdb.assignment_rows(pa.raw, pid)
    if not rows:
        raise SystemExit(f"{table.name(i)} (id {pid}) has no assignment row to move")
    club_rows = [r for r in rows
                 if struct.unpack_from("<I", pa.raw, r + 8)[0] > pesdb.NATIONAL_MAX]
    if not club_rows:
        raise SystemExit(f"{table.name(i)} (id {pid}) has no club assignment row")
    rec = pa.records[club_rows[0] // 16]
    old = struct.unpack_from("<I", rec, 8)[0]
    struct.pack_into("<I", rec, 8, team)
    return i, pid, old, team, club_rows


def load_coaches():
    return pesdb.CoachTable(pesdb.table_blob(DATA_CPK, "Coach"))


def load_teams():
    return pesdb.TeamTable(pesdb.table_blob(DATA_CPK, "Team"))


def coach_choices(coaches):
    return sorted({(coaches.cid(i), coaches.name(i)) for i in range(len(coaches.records))},
                  key=lambda kv: kv[1].lower())


def resolve_coach(coaches, needle):
    """One coach id for a name: exact match wins, else a unique substring."""
    want = needle.strip().lower()
    names = coach_choices(coaches)
    hits = [cid for cid, n in names if n.lower() == want] or \
           [cid for cid, n in names if want in n.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"no coach matching {needle!r}")
    print(f"{len(hits)} coaches match {needle!r}: " + ", ".join(str(c) for c in hits[:10]))
    raise SystemExit(2)


def copy_tsv(table, i):
    """One line of values in --fields order, tab-separated."""
    return "\t".join(str(table.get(i, label)) for label in pesdb.FIELD_BY_LABEL)


def main(argv):
    save = "--save" in argv
    append = "--append" in argv
    argv = [a for a in argv if a not in ("--save", "--append")]
    table = load()

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    if argv[0] == "--fields":
        for label in pesdb.FIELD_BY_LABEL:
            bit, width, add = pesdb.FIELD_BY_LABEL[label]
            print(f"{label:<24} {add}..{add + (1 << width) - 1}")
        return 0

    if argv[0] == "--find":
        hits = find(table, " ".join(argv[1:]))
        for i in hits[:40]:
            print(describe(table, i))
        print(f"{len(hits)} match(es)")
        return 0

    if argv[0] == "--show":
        hits = find(table, " ".join(argv[1:]))
        if not hits:
            raise SystemExit("no match")
        i = hits[0]
        print(describe(table, i))
        for label in pesdb.FIELD_BY_LABEL:
            print(f"  {label:<24} {table.get(i, label)}")
        return 0

    if argv[0] == "--set":
        name = argv[1]
        assignments = parse_assignment(argv[2:])
        hits = find(table, name)
        if len(hits) != 1:
            print(f"{len(hits)} matches for {name!r} — be more specific:")
            for i in hits[:20]:
                print("  " + describe(table, i))
            return 2
        i = hits[0]
        print("before: " + describe(table, i))
        changed = apply(table, i, assignments)
        for label, old, new in changed:
            print(f"  {label:<24} {old} -> {new}")
        print("after:  " + describe(table, i))
        if not changed:
            print("nothing changed")
            return 0
        if save:
            target, size = write(table, BACKUP_DIR)
            check = pesdb.PlayerTable(pesdb.table_blob(DATA_CPK, "Player"))
            ok = all(check.get(i, label) == new for label, _old, new in changed)
            print(f"wrote {size:,} B -> {target}")
            print(f"re-read: {len(check.records):,} players, "
                  f"{'all edits present' if ok else 'MISMATCH'}")
            prune_backups()
        else:
            print("(dry run — add --save to write)")
        return 0

    if argv[0] == "--export":
        path, n = export_csv(table, argv[1])
        print(f"exported {n:,} players -> {path}")
        return 0

    if argv[0] == "--import":
        touched = import_csv(table, argv[1], append=append)
        print(f"{touched:,} players changed from {argv[1]}")
        if save and touched:
            target, size = write(table, BACKUP_DIR)
            check = pesdb.PlayerTable(pesdb.table_blob(DATA_CPK, "Player"))
            print(f"wrote {size:,} B -> {target} · re-read {len(check.records):,} players")
            prune_backups()
        elif touched:
            print("(dry run — add --save to write)")
        return 0

    if argv[0] == "--squad":
        clubs = load_clubs()
        team = resolve_club(clubs, " ".join(argv[1:]))
        pids = pesdb.squads(load_table("PlayerAssignment", 16).raw).get(team, [])
        by_pid = player_index(table)
        print(f"{clubs[team]}  ·  team id {team}  ·  {len(pids)} players")
        for pid in pids:
            i = by_pid.get(pid)
            print("  " + describe(table, i) if i is not None
                  else f"  {pid:>6}  (no Player.bin record)")
        return 0

    if argv[0] == "--export-assignments":
        pa = load_table("PlayerAssignment", 16)
        rows, orphans = export_assignments(table, load_clubs(), pa, argv[1])
        print(f"exported {rows:,} assignment rows -> {argv[1]}")
        if orphans:
            print(f"  {orphans:,} row(s) reference player ids absent from Player.bin")
        return 0

    if argv[0] == "--transfer":
        args = argv[1:]
        if "--to" not in args:
            raise SystemExit('usage: --transfer "player" --to "club" [--save]')
        k = args.index("--to")
        player, club = " ".join(args[:k]), " ".join(args[k + 1:])
        pa = load_table("PlayerAssignment", 16)
        clubs = load_clubs()
        before = [bytes(r) for r in pa.records]
        i, pid, old, team, rows = transfer(table, clubs, pa, player, club)
        print(f"{table.name(i)} (id {pid}): {clubs.get(old, old)} -> {clubs.get(team, team)}")
        if len(rows) > 1:
            print(f"  {len(rows)} club assignment rows for this id; edited the first "
                  f"(record {rows[0] // 16})")
        if save:
            target, size = write(pa, BACKUP_DIR, "PlayerAssignment.bin")
            check = pesdb.LeadTable(pesdb.table_blob(DATA_CPK, "PlayerAssignment"), 16)
            changed = sum(1 for a, b in zip(before, check.records) if a != b)
            print(f"wrote {size:,} B -> {target}")
            print(f"re-read: {len(check.records):,} rows, {changed} changed")
            prune_backups()
        else:
            print("(dry run — add --save to write)")
        return 0

    if argv[0] == "--coaches":
        coaches = load_coaches()
        teams = load_teams()
        needle = " ".join(argv[1:]).strip().lower()
        names = coach_choices(coaches)
        rows = [kv for kv in names if not needle or needle in kv[1].lower()]
        clubs = {}
        for i in range(len(teams.records)):
            clubs.setdefault(teams.coach_id(i), teams.name(i))
        for cid, name in rows[:80]:
            print(f"{cid:>8}  {name:<28} {clubs.get(cid, '')}")
        print(f"{len(rows):,} coach(es)" + (f" of {len(names):,}" if needle else ""))
        return 0

    if argv[0] == "--club-coach":
        if len(argv) < 3:
            raise SystemExit('usage: --club-coach "club" "coach" [--save]')
        teams, coaches = load_teams(), load_coaches()
        clubs = {teams.tid(i): teams.name(i) for i in range(len(teams.records))}
        tid = resolve_club(clubs, argv[1])
        cid = resolve_coach(coaches, argv[2])
        idx = {teams.tid(i): i for i in range(len(teams.records))}[tid]
        by_cid = {coaches.cid(k): k for k in range(len(coaches.records))}
        old = teams.coach_id(idx)
        print(f"{clubs[tid]}: coach {old} -> {cid} ({coaches.name(by_cid[cid])})")
        if old == cid:
            print("nothing changed")
            return 0
        teams.set_coach(idx, cid)
        if save:
            target, size = write(teams, BACKUP_DIR, "Team.bin")
            again = load_teams()
            print(f"wrote {size:,} B -> {target} · re-read coach id {again.coach_id(idx)}")
            prune_backups()
        else:
            print("(dry run — add --save to write)")
        return 0

    if argv[0] == "--stadium":
        if len(argv) < 3:
            raise SystemExit('usage: --stadium "club" <stadium id> [--save]')
        teams = load_teams()
        clubs = {teams.tid(i): teams.name(i) for i in range(len(teams.records))}
        tid = resolve_club(clubs, argv[1])
        sid = int(argv[2])
        names = pesdb.stadium_names(pesdb.read_table(DATA_CPK, "Stadium"))
        if sid not in names:
            raise SystemExit(f"no stadium with id {sid} — try --stadiums")
        idx = {teams.tid(i): i for i in range(len(teams.records))}[tid]
        old = teams.stadium_id(idx)
        print(f"{clubs[tid]}: stadium {old} -> {sid} ({names[sid]})")
        teams.set_stadium(idx, sid)
        if save:
            target, size = write(teams, BACKUP_DIR, "Team.bin")
            again = load_teams()
            print(f"wrote {size:,} B -> {target} · re-read stadium id {again.stadium_id(idx)}")
            prune_backups()
        else:
            print("(dry run — add --save to write)")
        return 0

    if argv[0] == "--shirt":
        if len(argv) < 3:
            raise SystemExit('usage: --shirt "player" <number> [--save]')
        number = max(1, min(99, int(argv[-1])))
        player = " ".join(argv[1:-1])
        hits = find(table, player)
        if len(hits) != 1:
            raise SystemExit(f"{len(hits)} matches for {player!r} — be more specific")
        pid = table.pid(hits[0])
        pa = pesdb.AssignmentTable(pesdb.table_blob(DATA_CPK, "PlayerAssignment"))
        rows = [j for j in range(len(pa.records))
                if pa.pid(j) == pid and pa.team(j) > pesdb.NATIONAL_MAX]
        if not rows:
            raise SystemExit(f"{table.name(hits[0])} (id {pid}) has no club assignment row")
        before = [pa.shirt_number(j) for j in rows]
        for j in rows:
            pa.set_shirt_number(j, number)
        print(f"{table.name(hits[0])} (id {pid}): shirt {before} -> {number} in {len(rows)} club row(s)")
        if save:
            target, size = write(pa, BACKUP_DIR, "PlayerAssignment.bin")
            again = pesdb.AssignmentTable(pesdb.table_blob(DATA_CPK, "PlayerAssignment"))
            print(f"wrote {size:,} B -> {target} · re-read shirt {again.shirt_number(rows[0])}")
            prune_backups()
        else:
            print("(dry run — add --save to write)")
        return 0

    if argv[0] == "--stadiums":
        names = pesdb.stadium_names(pesdb.read_table(DATA_CPK, "Stadium"))
        for sid, name in sorted(names.items()):
            print(f"{sid:>4}  {name}")
        print(f"{len(names)} stadium(s)")
        return 0

    if argv[0] == "--clean-tables":
        out = argv[1]
        os.makedirs(out, exist_ok=True)
        for name in ("Player", "Team", "PlayerAssignment", "Coach"):
            data = pesdb.blank_table(pesdb.table_blob(DATA_CPK, name))
            with open(os.path.join(out, name + ".bin"), "wb") as f:
                f.write(data)
            print(f"  {name + '.bin':24} {len(pesdb.table_rows(data)):,} row bytes")
        print(f"blank tables -> {out}")
        return 0

    if argv[0] == "--copy":
        hits = find(table, " ".join(argv[1:]))
        if len(hits) != 1:
            raise SystemExit(f"{len(hits)} matches — be more specific")
        print(copy_tsv(table, hits[0]))
        return 0

    raise SystemExit(f"unknown command {argv[0]!r} — see --help")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

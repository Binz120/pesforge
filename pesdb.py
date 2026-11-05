"""PES 2021 / SP Football Life pesdb access: WESYS containers, CPK extraction, player records.

Player records are 312 bytes, little-endian BIT fields counted from bit 0 of the record.
Field map decoded by the FL community (all 97 fields agree with in-game values):
see docs/research/player-record.md of Matozanato/FootballLife-new-leagues.
"""
import json
import os
import struct
import zlib

import cpk

REC = 312
LEAD = 2048               # every pesdb table file starts with this many bytes of lead block
NATIONAL_MAX = 99         # shortcut: FL27 gives national teams ids 1-99 and clubs 100+;
                          # revisit if a patched database renumbers them and transfers land wrong
NAME_OFF = 0x44
NAME_SLOT = 0x3D          # 61 bytes per name slot: full, shirt, screen, printed

# (label, bit, width, add)
ATTRS = [
    ("Offensive Awareness", 370, 6, 40),
    ("Ball Control", 281, 6, 40),
    ("Dribbling", 352, 6, 40),
    ("Tight Possession", 416, 6, 40),
    ("Low Pass", 263, 6, 40),
    ("Lofted Pass", 402, 6, 40),
    ("Finishing", 396, 6, 40),
    ("Place Kicking", 250, 6, 40),
    ("Curl", 332, 6, 40),
    ("Heading", 288, 6, 40),
    ("Defensive Awareness", 275, 6, 40),
    ("Ball Winning", 312, 6, 40),
    ("Aggression", 384, 6, 40),
    ("GK Awareness", 326, 6, 40),
    ("GK Catching", 364, 6, 40),
    ("GK Clearing", 269, 6, 40),
    ("GK Reflexes", 320, 6, 40),
    ("GK Reach", 300, 6, 40),
    ("Speed", 306, 6, 40),
    ("Acceleration", 344, 6, 40),
    ("Kicking Power", 358, 6, 40),
    ("Jump", 294, 6, 40),
    ("Physical Contact", 390, 6, 40),
    ("Balance", 376, 6, 40),
    ("Stamina", 338, 6, 40),
]

PHYSICAL = [
    ("Age", 408, 6, 15),
    ("Height (cm)", 216, 7, 100),
    ("Weight (kg)", 256, 7, 30),
    ("Nationality", 233, 9, 0),
    ("Playing Style", 155, 5, 0),
    ("Registered Position", 434, 4, 0),
    # stored one below the number the game shows (form 1-8, weak foot 1-4, injury 1-4)
    ("Form", 438, 3, 1),
    ("Weak Foot Usage", 454, 2, 1),
    ("Weak Foot Accuracy", 462, 2, 1),
    ("Injury Resistance", 458, 2, 1),
    ("Stronger Foot", 514, 1, 0),
]

# (label, bit, width, add) — career and registration data, same 312-byte record as the abilities.
# Offsets, widths and addends are the PES 2021 player schema, checked against the shipped
# database.
CAREER = [
    ("Youth Club", 0, 32, 0),
    ("Owner Club", 32, 32, 0),
    ("Contract Until", 96, 27, 0),        # YYYYMMDD, e.g. 20260630
    ("Loan Until", 128, 27, 0),           # YYYYMMDD
    ("Market Value", 160, 24, 0),
    ("National Caps", 184, 8, 0),
    ("Reputation", 451, 3, 1),
    ("Playing Attitude", 382, 2, 1),
    ("Legend", 508, 1, 0),
    ("Hand", 509, 1, 0),
    ("Ballon d'Or", 498, 1, 0),
]

# (label, bit) — value 0 none, 1 can play, 2 natural
POSITIONS = [
    ("GK", 350), ("CB", 468), ("LB", 318), ("RB", 474), ("DMF", 414), ("CMF", 456),
    ("LMF", 466), ("RMF", 460), ("AMF", 464), ("LWF", 472), ("RWF", 476),
    ("SS", 478), ("CF", 470),
]

SKILLS = [
    ("Scissors Feint", 519), ("Double Touch", 523), ("Flip Flap", 485), ("Marseille Turn", 497),
    ("Sombrero", 482), ("Cross Over Turn", 287), ("Cut Behind & Turn", 528),
    ("Scotch Move", 525), ("Step On Skill control", 500), ("Heel Trick", 505),
    ("Chip shot control", 506), ("Mazing Run", 531), ("Incisive Run", 510),
    ("Speeding Bullet", 526), ("Long Range Drive", 527),
    ("Heading", 495), ("Long Range Shooting", 518), ("Acrobatic Finishing", 524),
    ("First-time Shot", 511), ("Dipping Shot", 494), ("Rising Shots", 499),
    ("Knuckle Shot", 513), ("Rabona", 515), ("No Look Pass", 512),
    ("One-touch Pass", 507), ("Through Passing", 487), ("Weighted Pass", 484),
    ("Pinpoint Crossing", 483), ("Low Lofted Pass", 488), ("Outside Curler", 493),
    ("Early Cross", 447), ("Long Ball Expert", 529), ("Long Throw", 521),
    ("GK Long Throw", 522), ("GK Low Punt", 490), ("GK High Punt", 496),
    ("GK Penalty Saver", 502), ("Penalty Specialist", 501),
    ("Man Marking", 504), ("Interception", 503), ("Acrobatic Clear", 530),
    ("Track Back", 517), ("Fighting Spirit", 486), ("Captaincy", 492),
    ("Super-Sub", 516), ("Long Ranger", 520), ("Gamesmanship", 491), ("Trickster", 489),
]

# (label, bit, width, add) — celebrations and running/dribbling motion, PES 2021 layout
MOTIONS = [
    ("Celebration 1", 242, 8, 0),
    ("Dribbling Hunching", 441, 3, 1),
    ("Dribbling Arm Move", 426, 4, 1),
    ("Running Hunching", 448, 3, 1),
    ("Running Arm Move", 430, 4, 1),
    ("Corner Kick", 422, 4, 1),
    ("Free Kick", 123, 5, 1),
    ("Penalty Kick", 444, 3, 1),
    ("Dribble Motion", 480, 2, 1),
]

POS_NAMES = ["GK", "CB", "LB", "RB", "DMF", "CMF", "LMF", "RMF", "AMF", "LWF", "RWF", "SS", "CF"]

# field lookups
FIELD_BY_LABEL = {}
for _l, _b, _w, _a in ATTRS + PHYSICAL + MOTIONS + CAREER:
    FIELD_BY_LABEL[_l] = (_b, _w, _a)
for _l, _b in POSITIONS:
    FIELD_BY_LABEL["Pos " + _l] = (_b, 2, 0)
for _l, _b in SKILLS:
    FIELD_BY_LABEL["Skill " + _l] = (_b, 1, 0)


# ---------------------------------------------------------------- WESYS
COUNTRIES = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "countries.json"),
              encoding="utf-8") as _f:
        COUNTRIES = {int(k): v for k, v in json.load(_f).items()}
except (OSError, ValueError):
    COUNTRIES = {}


def country_name(nid):
    """Nationality id stored in a player record -> English country name ('' if unknown)."""
    return COUNTRIES.get(nid, "")


def encode_name(text, slot=NAME_SLOT):
    """UTF-8 bytes for one fixed-width name slot: never split mid-codepoint, zero-padded."""
    b = str(text).encode("utf-8")[:slot - 1]
    while b:
        try:
            b.decode("utf-8")
            break
        except UnicodeDecodeError:
            b = b[:-1]
    return b + b"\x00" * (slot - len(b))


def is_wesys(buf):
    return len(buf) >= 16 and buf[3:8] == b"WESYS"


def wesys_pack(raw, prefix=b"\x00\x01\x01", level=9):
    """WESYS block, level 9 only.

    The old sweep over every zlib level cost 1.9s per save on an 8MB table and level 9 was always
    the winner (measured on the shipped tables); a payload that outgrows its slot still takes
    `patch_table`'s append path, so nothing depends on the last few bytes.
    """
    raw = bytes(raw)
    comp = zlib.compress(raw, level)
    return prefix + b"WESYS" + struct.pack("<II", len(comp), len(raw)) + comp


# ---------------------------------------------------------------- CPK access
def _find_block(data, offset, span=8192):
    """The shipped CPKs put a few files after an understated size; real start is WESYS-aligned."""
    if is_wesys(data[offset:offset + 16]):
        return offset
    end = min(len(data), offset + span)
    i = data.find(b"WESYS", offset, end)
    return (i - 3) if i > 0 else offset


def _wesys_starts(blob):
    out, pos = [], blob.find(b"WESYS")
    while pos > 0:
        out.append(pos - 3)
        pos = blob.find(b"WESYS", pos + 5)
    return out


def table_blob(cpk_path, name):
    """The whole table file from the CPK: LEAD-byte lead block, then the WESYS block."""
    c = cpk.Cpk(cpk_path)
    data = c.d
    for row in c.entries():
        fn = row.get("FileName", "")
        stem = fn[:-4] if fn.endswith(".bin") else fn
        if stem != name:
            continue
        block = _find_block(data, row["FileOffset"])
        # _find_block lands on the WESYS block; when it skipped a lead block, back up over it
        start = block - LEAD if block > row["FileOffset"] else block
        return data[start:block + row["FileSize"] + 4096]
    raise KeyError(f"{name} not found in {cpk_path}")


def _table_block(blob):
    """(start, decompressed rows) of the table's real WESYS block.

    Picks the largest complete stream: Player.bin leads with a small PlayerWeekno block,
    and compressed bytes can contain a literal "WESYS" before the real one.
    """
    starts = [s for s in _wesys_starts(blob) if s >= LEAD]
    if not starts:
        raise ValueError("no WESYS block in table file")
    best = None
    for start in starts:
        csz, usz = struct.unpack("<II", blob[start + 8:start + 16])
        d = zlib.decompressobj()
        out = d.decompress(blob[start + 16:start + 16 + csz])
        if d.eof and len(out) == usz and (best is None or len(out) > len(best[1])):
            best = (start, out)
    if best is None:
        raise ValueError("no complete WESYS block")
    return best


def table_rows(blob):
    """Decompressed rows of a table file blob, eof checked."""
    return _table_block(blob)[1]


def table_prefix(blob):
    """The 3 bytes in front of the table's real WESYS magic (the re-pack prefix)."""
    start = _table_block(blob)[0]
    return blob[start:start + 3]


def read_table(cpk_path, name):
    """Decompressed records of one pesdb table, eof checked."""
    return table_rows(table_blob(cpk_path, name))


def blank_table(blob):
    """The same table file with every row dropped: lead block kept, WESYS block empty."""
    start = _table_block(blob)[0]
    return blob[:start] + wesys_pack(b"", blob[start:start + 3])


# ---------------------------------------------------------------- records
def bit_get(rec, bit, width, add=0):
    return ((int.from_bytes(rec, "little") >> bit) & ((1 << width) - 1)) + add


def bit_set(rec, bit, width, add, value):
    mask = (1 << width) - 1
    v = int.from_bytes(rec, "little")
    v = (v & ~(mask << bit)) | (((int(value) - add) & mask) << bit)
    rec[:] = v.to_bytes(len(rec), "little")


class LeadTable:
    """A pesdb table file: LEAD-byte lead block + one WESYS block of fixed-size rows.

    The lead block is kept byte-for-byte; only the rows are re-packed.
    """

    def __init__(self, raw_file, rec):
        self.file = bytes(raw_file)
        self.rec = rec
        self.start, self.raw = _table_block(self.file)
        self.prefix = self.file[self.start:self.start + 3]
        if len(self.raw) % rec:
            raise ValueError(f"table is not a multiple of {rec}: {len(self.raw)}")
        self.records = [bytearray(self.raw[i:i + rec]) for i in range(0, len(self.raw), rec)]

    def to_file(self):
        raw = b"".join(bytes(r) for r in self.records)
        return self.file[:self.start] + wesys_pack(raw, self.prefix)

    def clear(self):
        """Drop every row, keeping the lead block."""
        self.records = []


class PlayerTable(LeadTable):
    """Player.bin: [2048-byte block: PlayerWeekno][WESYS block: 312-byte player records]."""

    def __init__(self, raw_file):
        super().__init__(raw_file, REC)

    # -- fields
    def get(self, i, label):
        bit, width, add = FIELD_BY_LABEL[label]
        return bit_get(self.records[i], bit, width, add)

    def set(self, i, label, value):
        bit, width, add = FIELD_BY_LABEL[label]
        bit_set(self.records[i], bit, width, add, value)

    # -- identity
    def pid(self, i):
        return struct.unpack_from("<I", self.records[i], 8)[0]

    def slot(self, i, n):
        s = self.records[i][NAME_OFF + n * NAME_SLOT:NAME_OFF + (n + 1) * NAME_SLOT]
        return s.split(b"\0")[0].decode("utf-8", "replace")

    def name(self, i):
        return self.slot(i, 0)

    def shirt(self, i):
        return self.slot(i, 1)

    def set_name(self, i, full=None, shirt=None):
        rec = self.records[i]
        for n, val in ((0, full), (1, shirt), (2, shirt), (3, full)):
            if val is None:
                continue
            rec[NAME_OFF + n * NAME_SLOT:NAME_OFF + (n + 1) * NAME_SLOT] = encode_name(val, NAME_SLOT)

    # to_file() comes from LeadTable: lead block + re-packed WESYS rows.


def table_slot(cpk_path, name):
    """(offset, length) of a table's real bytes inside the CPK.

    The TOC understates some files, so the length comes from the WESYS block's own compressed
    size (block start + 16 + csz), not from FileSize.
    """
    c = cpk.Cpk(cpk_path)
    for row in c.entries():
        fn = row.get("FileName", "")
        stem = fn[:-4] if fn.endswith(".bin") else fn
        if stem != name:
            continue
        off = row["FileOffset"]
        # limit the scan to this entry plus slack: the next table starts after it
        tail = c.d[off:off + row["FileSize"] + 4096]
        best = None
        for start in _wesys_starts(tail):
            csz, usz = struct.unpack_from("<II", tail, start + 8)
            d = zlib.decompressobj()
            out = d.decompress(tail[start + 16:start + 16 + csz])
            if d.eof and len(out) == usz and (best is None or len(out) > best[0]):
                best = (len(out), start, csz)
        if best is None:
            raise ValueError(f"{name}: no complete WESYS block in the CPK")
        # The TOC's FileSize is the floor: deriving the slot from the current csz alone would
        # shrink it after a blank-table write and block the next, larger save.
        return off, max(best[1] + 16 + best[2], row["FileSize"])
    raise KeyError(f"{name} not found in {cpk_path}")


def patch_table(cpk_path, name, blob, backup_dir=None, slots_path=None):
    """Write a table back into the CPK.

    Normally the table's own slot is overwritten, which leaves every TOC offset valid. When the
    new blob is larger than the slot (recompression adds a few bytes), it is appended at the end
    of the CPK and this entry is repointed instead, so no other file moves.
    Returns (offset, length) of where the table now sits.
    """
    import json
    import shutil
    import time

    c = cpk.Cpk(cpk_path)
    index = entry = None
    for i, row in enumerate(c.entries()):
        fn = row.get("FileName", "")
        if (fn[:-4] if fn.endswith(".bin") else fn) == name:
            index, entry = i, row
            break
    if index is None:
        raise KeyError(f"{name} not found in {cpk_path}")

    off, length = table_slot(cpk_path, name)
    # The slot never shrinks: remember the first (shipped) capacity, or a blank-table write
    # would make the next, larger save fail.
    if slots_path:
        try:
            slots = json.load(open(slots_path))
        except (OSError, ValueError):
            slots = {}
        key = "%s|%s" % (os.path.abspath(cpk_path), name)
        length = max(length, slots.get(key, 0))
        if slots.get(key) != length:
            slots[key] = length
            os.makedirs(os.path.dirname(slots_path), exist_ok=True)
            json.dump(slots, open(slots_path, "w"), indent=2)

    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(cpk_path, os.path.join(
            backup_dir, "%s.%s.cpk" % (os.path.basename(cpk_path)[:-4],
                                       time.strftime("%Y%m%d-%H%M%S"))))

    if len(blob) <= length:
        with open(cpk_path, "r+b") as f:
            f.seek(off)
            f.write(blob + bytes(length - len(blob)))
        return off, length

    new_off = (len(c.d) + 0x7FF) & ~0x7FF        # 2 KiB aligned, past everything else
    c.toc.set_row_int(index, "FileOffset", new_off)
    c.toc.set_row_int(index, "FileSize", len(blob))
    c.toc.set_row_int(index, "ExtractSize", len(blob))   # the 2019/2015 writers keep it in step
    with open(cpk_path, "r+b") as f:
        f.seek(0)
        f.write(c.d)
        f.seek(new_off)
        f.write(blob)
        f.truncate()
    return new_off, len(blob)


def squads(pa_raw):
    """PlayerAssignment.bin rows -> {team_id: [player_id, ...]}."""
    out = {}
    for i in range(0, len(pa_raw) - 15, 16):
        pid, team = struct.unpack_from("<II", pa_raw, i + 4)
        if pid and team:
            out.setdefault(team, []).append(pid)
    return out


def assignment_rows(pa_raw, pid):
    """Offsets of every row for `pid` — loans and duplicates have more than one."""
    return [i for i in range(0, len(pa_raw) - 15, 16)
            if struct.unpack_from("<I", pa_raw, i + 4)[0] == pid]


def club_names(team_raw):
    """Team.bin: 1532-byte rows, team id at +0x08, name at +0x170."""
    names = {}
    for i in range(0, len(team_raw) - 1531, 1532):
        tid = struct.unpack_from("<I", team_raw, i + 8)[0]
        name = team_raw[i + 0x170:i + 0x170 + 96].split(b"\0")[0].decode("utf-8", "replace")
        names.setdefault(tid, name)
    return names


def stadium_names(stadium_raw):
    """Stadium.bin: 392-byte rows, id u16 at +4, English name at +189, Japanese at +8."""
    names = {}
    for i in range(0, len(stadium_raw) - STADIUM_REC + 1, STADIUM_REC):
        rec = stadium_raw[i:i + STADIUM_REC]
        sid = struct.unpack_from("<H", rec, 4)[0]
        eng = rec[189:370].split(b"\0")[0].decode("utf-8", "replace").strip()
        jap = rec[8:180].split(b"\0")[0].decode("utf-8", "replace").strip()
        names.setdefault(sid, eng or jap)
    return names


def player_teams(pa_raw):
    """PlayerAssignment.bin: 16-byte rows -> {player_id: club team_id}.

    National-team rows (ids <= NATIONAL_MAX) are skipped: a player with a country row first
    would otherwise show "Spain" where the club belongs.
    """
    out = {}
    for i in range(0, len(pa_raw) - 15, 16):
        _row, pid, team, _no = struct.unpack_from("<IIII", pa_raw, i)
        if pid and team > NATIONAL_MAX:
            out.setdefault(pid, team)
    return out


# ---------------------------------------------------------------- appearance, teams, overall
APPEARANCE = [
    ('Skin Colour', 296, 3, 0),
    ('Head Length', 76, 4, -7),
    ('Head Width', 80, 4, -7),
    ('Head Depth', 84, 4, -7),
    ('Face Height', 348, 4, -7),
    ('Face Size', 344, 4, -7),
    ('Upper Eyelid Type', 315, 3, 1),
    ('Bottom Eyelid Type', 320, 3, 1),
    ('Eye Height', 224, 4, -7),
    ('Horizontal Eye Position', 220, 4, -7),
    ('Iris Colour', 448, 4, 1),
    ('Pupil Size', 172, 4, -7),
    ('Upper Eyelid Height Inner', 200, 4, -7),
    ('Upper Eyelid Width Inner', 196, 4, -7),
    ('Upper Eyelid Height Outer', 208, 4, -7),
    ('Upper Eyelid Width Outer', 204, 4, -7),
    ('Inner Eye Height', 184, 4, -7),
    ('Inner Eye Position', 180, 4, -7),
    ('Eye Corner Height', 192, 4, -7),
    ('Outer Eye Position', 188, 4, -7),
    ('Bottom Eyelid Height', 216, 4, -7),
    ('Eye Depth', 176, 4, -7),
    ('Forehead', 304, 3, 1),
    ('Eyebrow Type', 336, 3, 1),
    ('Eyebrow Thickness', 414, 2, 0),
    ('Eyebrow Style', 398, 2, 0),
    ('Eyebrow Density', 406, 2, 0),
    ('Eyebrow Red', 392, 6, 0),
    ('Eyebrow Green', 400, 5, 0),
    ('Eyebrow Blue', 408, 5, 0),
    ('Inner Eyebrow Height', 152, 4, -7),
    ('Brow Width', 148, 4, -7),
    ('Outer Eyebrow Height', 164, 4, -7),
    ('Temple Width', 160, 4, -7),
    ('Eyebrow Depth', 168, 4, -7),
    ('Nose Type', 352, 3, 1),
    ('Laughter Lines', 312, 3, 1),
    ('Nose Height', 228, 4, -7),
    ('Nostril Width', 236, 4, -7),
    ('Nose Width', 248, 4, -7),
    ('Nose Tip Depth', 232, 4, -7),
    ('Nose Depth', 244, 4, -7),
    ('Upper Lip Type', 360, 3, 1),
    ('Lower Lip Type', 363, 3, 1),
    ('Mouth Position', 256, 4, -7),
    ('Lip Size', 264, 4, -7),
    ('Lip Width', 276, 4, -7),
    ('Mouth Corner Height', 272, 4, -7),
    ('Mouth Depth', 260, 4, -7),
    ('Facial Hair Type', 307, 5, 0),
    ('Facial Hair Red', 428, 6, 0),
    ('Facial Hair Green', 434, 5, 0),
    ('Facial Hair Blue', 440, 5, 0),
    ('Facial Hair Thickness', 446, 2, 0),
    ('Cheek Type', 299, 3, 1),
    ('Neck Line Type', 341, 2, 1),
    ('Cheekbones', 128, 4, -7),
    ('Chin Height', 288, 4, -7),
    ('Chin Width', 284, 4, -7),
    ('Jaw Height', 120, 4, -7),
    ('Jawline', 116, 4, -7),
    ('Chin Depth', 292, 4, -7),
    ('Ear Length', 324, 4, -7),
    ('Ear Width', 328, 4, -7),
    ('Ear Angle', 332, 4, -7),
    ('Overall Style', 384, 3, 0),
    ('Overall Length', 416, 3, 0),
    ('Overall Wave Level', 419, 3, 0),
    ('Overall Hair Variation', 452, 5, 0),
    ('Front Style', 464, 2, 0),
    ('Front Parted', 467, 3, 0),
    ('Front Hairline', 473, 2, 0),
    ('Front Forehead Width', 476, 2, 0),
    ('Side Back Style', 470, 3, 0),
    ('Side Back Cropped', 388, 3, 0),
    ('Hair Red', 366, 6, 0),
    ('Hair Green', 372, 5, 0),
    ('Hair Blue', 378, 5, 0),
    ('Hair Accessory', 424, 1, 0),
    ('Hair Accessory Colour', 425, 2, 1),
    ('Hair Colour', 460, 3, 0),
    ('Neck Length', 32, 4, -7),
    ('Neck Size', 36, 4, -7),
    ('Shoulder Height', 40, 4, -7),
    ('Shoulder Width', 44, 4, -7),
    ('Chest Measurement', 48, 4, -7),
    ('Waist Size', 52, 4, -7),
    ('Arm Size', 56, 4, -7),
    ('Thigh Size', 64, 4, -7),
    ('Calf Size', 68, 4, -7),
    ('Leg Length', 72, 4, -7),
    ('Arm Length', 60, 4, -7),
    ('Wrist Taping', 94, 2, 0),
    ('Wrist Tape Colour1', 88, 2, 0),
    ('Wrist Tape Colour2', 91, 2, 0),
    ('Ankle Taping', 111, 1, 0),
    ('Player Gloves', 112, 1, 0),
    ('Player Gloves Colour', 113, 3, 1),
    ('Undershorts', 108, 2, 0),
    ('Sleeves', 102, 2, 0),
    ('Shirttail', 110, 1, 0),
    ('Sock Length', 106, 2, 0),
    ('Long Sleeved Inners', 104, 2, 0),
    ('Value Ap1', 96, 1, 0),
    ('Value Ap2', 124, 4, 0),
    ('Value Ap3', 132, 4, 0),
    ('Value Ap4', 136, 4, 0),
    ('Value Ap5', 140, 4, 0),
    ('Value Ap6', 144, 4, 0),
    ('Value Ap7', 156, 4, 0),
    ('Value Ap8', 212, 4, 0),
    ('Value Ap9', 240, 4, 0),
    ('Value Ap10', 252, 4, 0),
    ('Value Ap11', 268, 4, 0),
    ('Value Ap12', 280, 4, 0),
    ('Value Ap15', 358, 1, 0),
    ('Value Ap17', 422, 2, 0),
]

APPEARANCE_BY_LABEL = {f[0]: f[1:] for f in APPEARANCE}

APPEARANCE_GROUPS = [
    ('Head & face', ['Skin Colour', 'Head Length', 'Head Width', 'Head Depth', 'Face Height', 'Face Size', 'Forehead', 'Temple Width', 'Laughter Lines', 'Cheek Type', 'Cheekbones', 'Chin Height', 'Chin Width', 'Jaw Height', 'Jawline', 'Chin Depth', 'Ear Length', 'Ear Width', 'Ear Angle']),
    ('Eyes', ['Upper Eyelid Type', 'Bottom Eyelid Type', 'Eye Height', 'Horizontal Eye Position', 'Iris Colour', 'Pupil Size', 'Upper Eyelid Height Inner', 'Upper Eyelid Width Inner', 'Upper Eyelid Height Outer', 'Upper Eyelid Width Outer', 'Inner Eye Height', 'Inner Eye Position', 'Eye Corner Height', 'Outer Eye Position', 'Bottom Eyelid Height', 'Eye Depth']),
    ('Eyebrows', ['Eyebrow Type', 'Eyebrow Thickness', 'Eyebrow Style', 'Eyebrow Density', 'Eyebrow Red', 'Eyebrow Green', 'Eyebrow Blue', 'Inner Eyebrow Height', 'Brow Width', 'Outer Eyebrow Height', 'Eyebrow Depth']),
    ('Nose', ['Nose Type', 'Nose Height', 'Nostril Width', 'Nose Width', 'Nose Tip Depth', 'Nose Depth']),
    ('Mouth', ['Upper Lip Type', 'Lower Lip Type', 'Mouth Position', 'Lip Size', 'Lip Width', 'Mouth Corner Height', 'Mouth Depth']),
    ('Facial hair', ['Facial Hair Type', 'Facial Hair Red', 'Facial Hair Green', 'Facial Hair Blue', 'Facial Hair Thickness']),
    ('Hair', ['Overall Style', 'Overall Length', 'Overall Wave Level', 'Overall Hair Variation', 'Front Style', 'Front Parted', 'Front Hairline', 'Front Forehead Width', 'Side Back Style', 'Side Back Cropped', 'Hair Red', 'Hair Green', 'Hair Blue', 'Hair Accessory', 'Hair Accessory Colour', 'Hair Colour']),
    ('Physique', ['Neck Line Type', 'Neck Length', 'Neck Size', 'Shoulder Height', 'Shoulder Width', 'Chest Measurement', 'Waist Size', 'Arm Size', 'Thigh Size', 'Calf Size', 'Leg Length', 'Arm Length']),
    ('Kit', ['Wrist Taping', 'Wrist Tape Colour1', 'Wrist Tape Colour2', 'Ankle Taping', 'Player Gloves', 'Player Gloves Colour', 'Undershorts', 'Sleeves', 'Shirttail', 'Sock Length', 'Long Sleeved Inners']),
    ('Other', ['Value Ap1', 'Value Ap2', 'Value Ap3', 'Value Ap4', 'Value Ap5', 'Value Ap6', 'Value Ap7', 'Value Ap8', 'Value Ap9', 'Value Ap10', 'Value Ap11', 'Value Ap12', 'Value Ap15', 'Value Ap17']),
]

TEAM_REC = 1532
ASSIGN_REC = 16
COACH_REC = 100
STADIUM_REC = 392


class AppearanceTable(LeadTable):
    """PlayerAppearance.bin: 60-byte records, player id u32 at +0, bit fields after."""

    def __init__(self, raw_file):
        super().__init__(raw_file, 60)

    def pid(self, i):
        return struct.unpack_from("<I", self.records[i], 0)[0]

    def get(self, i, label):
        bit, width, add = APPEARANCE_BY_LABEL[label]
        return bit_get(self.records[i], bit, width, add)

    def set(self, i, label, value):
        bit, width, add = APPEARANCE_BY_LABEL[label]
        bit_set(self.records[i], bit, width, add, value)


class TeamTable(LeadTable):
    """Team.bin: 1532-byte records, id u32 at +8, name slots at fixed offsets."""

    def __init__(self, raw_file):
        super().__init__(raw_file, TEAM_REC)

    def tid(self, i):
        return struct.unpack_from("<I", self.records[i], 8)[0]

    def slot(self, i, offset, length):
        raw = self.records[i][offset:offset + length].split(b"\0")[0]
        return raw.decode("utf-8", "replace")

    def name(self, i):
        return self.slot(i, 368, 70)

    def set_slot(self, i, offset, length, value):
        self.records[i][offset:offset + length] = encode_name(value, length)

    # the club's coach link and its home stadium live at the top of the team record
    def coach_id(self, i):
        return struct.unpack_from("<I", self.records[i], 0)[0]

    def set_coach(self, i, cid):
        struct.pack_into("<I", self.records[i], 0, int(cid))

    def stadium_id(self, i):
        return struct.unpack_from("<H", self.records[i], 60)[0]

    def set_stadium(self, i, sid):
        struct.pack_into("<H", self.records[i], 60, int(sid) & 0xFFFF)


class CoachTable(LeadTable):
    """Coach.bin: 100-byte records, id u32 at +0, name slot at +54, Japanese at +8."""

    def __init__(self, raw_file):
        super().__init__(raw_file, COACH_REC)

    def cid(self, i):
        return struct.unpack_from("<I", self.records[i], 0)[0]

    def slot(self, i, offset):
        return self.records[i][offset:offset + 45].split(b"\0")[0].decode("utf-8", "replace")

    def name(self, i):
        return self.slot(i, 54)

    def japanese(self, i):
        return self.slot(i, 8)

    def set_slot(self, i, offset, value):
        self.records[i][offset:offset + 45] = encode_name(value, 45)

    def set_name(self, i, value):
        self.set_slot(i, 54, value)

    def set_japanese(self, i, value):
        self.set_slot(i, 8, value)

    def country(self, i):
        return bit_get(self.records[i], 32, 9)

    def set_country(self, i, value):
        bit_set(self.records[i], 32, 9, 0, value)

    def adaptability(self, i):
        return bit_get(self.records[i], 41, 7, 40)

    def set_adaptability(self, i, value):
        bit_set(self.records[i], 41, 7, 40, value)


class AssignmentTable(LeadTable):
    """PlayerAssignment.bin: 16-byte rows, playerId u32 at +4, teamId u32 at +8."""

    def __init__(self, raw_file):
        super().__init__(raw_file, ASSIGN_REC)

    def pid(self, i):
        return struct.unpack_from("<I", self.records[i], 4)[0]

    def team(self, i):
        return struct.unpack_from("<I", self.records[i], 8)[0]

    def set_team(self, i, team_id):
        struct.pack_into("<I", self.records[i], 8, team_id)

    def shirt_number(self, i):
        """Squad number of this row; PES 20+ stores it one below the number shown in game."""
        return self.records[i][12] + 1

    def set_shirt_number(self, i, number):
        self.records[i][12] = max(1, min(99, int(number))) - 1


# ---------------------------------------------------------------- overall
# Position -> [(label, weight)] for the PES 2021 rating model.
OVR_MODEL = {
    0: [("GK Awareness", .24), ("GK Reflexes", .22), ("GK Reach", .22),
        ("GK Catching", .14), ("GK Clearing", .14), ("Jump", .04)],
    1: [("Defensive Awareness", .35), ("Ball Winning", .30), ("Physical Contact", .18),
        ("Header", .08), ("Jump", .05), ("Aggression", .04)],
    2: [("Speed", .20), ("Acceleration", .18), ("Stamina", .15), ("Defensive Awareness", .18),
        ("Ball Winning", .15), ("Low Pass", .08), ("Ball Control", .06)],
    4: [("Low Pass", .25), ("Lofted Pass", .20), ("Ball Control", .15), ("Tight Possession", .10),
        ("Defensive Awareness", .10), ("Ball Winning", .10), ("Stamina", .10)],
    5: [("Low Pass", .22), ("Lofted Pass", .18), ("Ball Control", .15), ("Tight Possession", .10),
        ("Offensive Awareness", .15), ("Stamina", .10), ("Ball Winning", .10)],
    6: [("Speed", .20), ("Acceleration", .18), ("Dribbling", .16), ("Ball Control", .14),
        ("Low Pass", .14), ("Stamina", .18)],
    8: [("Offensive Awareness", .20), ("Ball Control", .20), ("Dribbling", .15), ("Low Pass", .15),
        ("Finishing", .15), ("Acceleration", .15)],
    9: [("Speed", .22), ("Acceleration", .20), ("Dribbling", .18), ("Ball Control", .15),
        ("Finishing", .15), ("Offensive Awareness", .10)],
    11: [("Offensive Awareness", .22), ("Finishing", .20), ("Dribbling", .15), ("Ball Control", .15),
         ("Speed", .14), ("Acceleration", .14)],
    12: [("Offensive Awareness", .22), ("Finishing", .22), ("Ball Control", .12),
         ("Physical Contact", .10), ("Speed", .10), ("Header", .10), ("Jump", .10),
         ("Kicking Power", .04)],
}
OVR_MODEL[3] = OVR_MODEL[2]        # LB/RB
OVR_MODEL[7] = OVR_MODEL[6]        # LMF/RMF
OVR_MODEL[10] = OVR_MODEL[9]       # RWF


def overall(get, position):
    """Rating of a PES 2021 record, from the per-position model below.

    Weighted average of the abilities that matter for the running position, rounded
    half-to-even.
    """
    def value(label):
        v = get({"Header": "Heading"}.get(label, label))
        return 0 if v is None else v

    weights = OVR_MODEL.get(int(position))
    if not weights:
        names = ("Offensive Awareness", "Defensive Awareness", "Finishing",
                 "Speed", "Physical Contact", "Stamina")
        return int(round(sum(value(n) for n in names) / 6.0))
    return int(round(sum(value(label) * weight for label, weight in weights)))

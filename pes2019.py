"""PES 2019 database: read and write the tables inside dt10_x64.cpk / dt80_*_x64.cpk.

Player record (188 bytes, little-endian bit fields counted from bit 0):
    id      u32 at +4
    names   +48 Japanese | +94 shirt | +140 full, 45-byte UTF-8 slots
    bio     nationality 9 bits at 73; second nationality 9 bits at 64; height 8 bits at 82
            (+100 cm); weight 7 bits at 104 (+30 kg); age 5 bits at 256 (+15); playing style
            5 bits at 123; form 3 bits at 270 (+1); weak foot usage 2 bits at 285 (+1);
            weak foot accuracy 2 bits at 294 (+1); injury resistance 2 bits at 290 (+1);
            reputation 3 bits at 282 (+1); playing attitude 2 bits at 222 (+1);
            strong foot 1 bit at 351 (0 right, 1 left)
    primary position   4 bits at 266 (0 GK, 1 CB, ... 12 CF)
    playability        13 two-bit fields (see POSITION_FLAGS): 0 cannot play, 1 can, 2 natural
    abilities  23 six-bit fields, value = raw + 40 (see ABILITIES — the record's own order)
    flags      one bit each in bytes 40..45 — the game's COM styles, skills and the
               legend / Ballon d'Or markers (see COM_STYLES, SKILLS, MARKERS)

Team.bin rows are 1468 bytes: u32 coach id @0, u32 id @8 (the id PlayerAssignment rows
carry), u16 stadium id @60, name slots — Japanese @24 (70 B) and the full name @234 (70 B)
are the two filled on every row (the localized slots @94/@374/@584/@758/@1178/@1398 exist
only on the 81 national-team rows).

Coach.bin rows are 100 bytes: u32 id @0, Japanese name @8 (45 B), name @54 (45 B),
country 9 bits at bit 32, adaptability 7 bits at bit 41 (+40).

PlayerAssignment.bin rows are 16 bytes: u32 row id @0, u32 player id @4, u32 team id @8,
byte @12 the shirt number stored one below the number the game shows, byte @13 a squad slot
(4 x the row's index inside its squad), byte @14 flags. Team ids below 100 are national
teams, 100 and up clubs; a player usually has two rows (club + country).

Stadium.bin rows are 392 bytes: u16 id @4, Japanese name @8, English name @189, id string @370.

The map was recovered from the Ejogc editor's own PES 2019 schema (PES2020Editor V0.12.3,
`SchemaPes2019`: 188-byte player, 1468-byte team, 100-byte coach, 16-byte assignment,
60-byte appearance, 392-byte stadium, 149 player fields as [byteOffset, shift, width,
addend]) and every field below was then checked against this install's shipped tables:
the record size divides 13,805 rows exactly, ids are strictly ascending at +4, and
pesdb.net's PES 2019 cards match the decode field for field — Messi 170/72/31, left foot,
Creative Playmaker, 20 abilities to the point; Neuer 193/92/32, style 16, both his skills;
Buffon 191/94/40 with all 29 fields exact. Ronaldo's card sits a few points higher because
pesdb carries a later data update than this disc (his record here reads Place Kicking 75
against the site's 88) — the map is the same on both.

The tables live under `common/etc/pesdb/` in the CPK, unchanged from PES 2015's layout:

    file payload = 3-byte prefix + 'WESYS' + u32 compressed + u32 uncompressed + zlib,
    at FileOffset + 0x800, and its length (16 + compressed) equals the TOC's FileSize and
    ExtractSize — verified on every one of these tables. The 0x800 in front holds the
    file's lead block (Player ships a 2040-byte one) and stays untouched.

One quirk this release shares with a data pack: the same tables ship twice inside
`Data/` — `dt00_x64.cpk` (the asset pack) and `dt10_x64.cpk` (the database pack) — and in
this install those copies are byte-identical. dt10 is the one to edit; the packs listed in
`download/DpFileList.bin` (dt80_*_x64.cpk) are mounted after it, so they win, and
`dt80_200E_x64.cpk` carries a *larger* player table (16,762 rows) than dt10's 13,805.
"""
import glob
import json
import os
import struct
import time
import zlib

import cpk

REC = 188
LEAD = 0x800               # a file's payload starts this far after its TOC FileOffset
ID_OFF = 4
NAME_OFFS = {"japanese": 48, "shirt": 94, "full": 140}
NAME_LEN = 45
PREFIX = b"\x04\x10\x81"   # the 3-byte prefix every Player*.bin payload carries

POSITION_NAMES = ("GK", "CB", "LB", "RB", "DMF", "CMF", "LMF", "RMF",
                  "AMF", "LWF", "RWF", "SS", "CF")

# Registered position, 4 bits. Checked against L. Messi (11 SS), C. Ronaldo (12 CF),
# M. Neuer (0 GK), G. Buffon (0 GK).
PRIMARY_POSITION = 266
# Playability per position, 2 bits: 0 cannot play, 1 can play, 2 natural. Checked on Messi
# (GK..DMF 0, CMF/LMF/RMF/LWF 1, AMF/RWF/SS/CF 2, registered SS).
POSITION_FLAGS = {
    "GK": 190, "CB": 302, "LB": 158, "RB": 310, "DMF": 254, "CMF": 288, "LMF": 298,
    "RMF": 292, "AMF": 296, "LWF": 308, "RWF": 312, "SS": 316, "CF": 304,
}

# label -> bit offset (width 6, value = raw + 40). The record's own order; the vendor's
# PES 2019 property names are in the comment.
ABILITIES = {
    "Attacking Prowess": 224,     # OffensiveAwareness
    "Ball Control": 134,          # BallControl
    "Dribbling": 204,             # Dribbling
    "Low Pass": 111,              # LowPass
    "Lofted Pass": 248,           # LoftedPass
    "Finishing": 242,             # Finishing
    "Header": 140,                # Heading
    "Place Kicking": 90,          # PlaceKicking
    "Swerve": 184,                # Curl
    "Speed": 160,                 # Speed
    "Explosive Power": 198,       # Acceleration
    "Kicking Power": 210,         # KickingPower
    "Jump": 146,                  # Jump
    "Physical Contact": 236,      # PhysicalContact
    "Unwavering Balance": 230,    # Balance
    "Stamina": 192,               # Stamina
    "Defensive Prowess": 128,     # DefensiveAwareness
    "Ball Winning": 166,          # BallWinning
    "Goalkeeping": 178,           # GKAwareness
    "GK Catch": 216,              # GKCatching
    "Clearing": 117,              # GKClearing
    "Reflexes": 172,              # GKReflexes
    "Coverage": 152,              # GKReach
}
ABILITY_ADD = 40
ABILITY_WIDTH = 6

# label -> (bit, width, add). "Position" is the registered position, 0..12.
FIELD_BY_LABEL = {label: (bit, ABILITY_WIDTH, ABILITY_ADD) for label, bit in ABILITIES.items()}
FIELD_BY_LABEL["Position"] = (PRIMARY_POSITION, 4, 0)
for _pos, _bit in POSITION_FLAGS.items():
    FIELD_BY_LABEL[f"Play {_pos}"] = (_bit, 2, 0)

# Bio block: recovered from the vendor schema and matched field-for-field against pesdb.net
# cards (L. Messi, C. Ronaldo, M. Neuer, G. Buffon — height, weight, age, foot, style, the
# add-1 detail fields and both nationalities). (bit, width, add).
BIO_FIELDS = {
    "Nationality": (73, 9, 0),          # the vendor's country code; data/countries.json
    "Nationality 2": (64, 9, 0),        # second nationality, 0 = none
    "Height": (82, 8, 100),             # cm
    "Weight": (104, 7, 30),             # kg
    "Age": (256, 5, 15),                # years, as of the shipped database
    "Playing Style": (123, 5, 0),       # PES 2019 style id (see STYLE_NAMES)
    "Form": (270, 3, 1),                # 1..8
    "Weak Foot Usage": (285, 2, 1),     # 1..4
    "Weak Foot Accuracy": (294, 2, 1),  # 1..4
    "Injury Resistance": (290, 2, 1),   # 1..4
    "Reputation": (282, 3, 1),          # 1..8
    "Playing Attitude": (222, 2, 1),    # 1..4
    "Foot": (351, 1, 0),                # 0 right, 1 left
    "Corner Kicks": (273, 3, 1),        # 1..8, who takes them
    "Free Kicks": (262, 4, 1),          # 1..16
    "Penalty Kick": (306, 2, 1),        # 1..4
}
FIELD_BY_LABEL.update(BIO_FIELDS)

# PES 2019's own style enum, in the order the game lists it. Neuer reads 16 (Offensive
# Goalkeeper), Buffon 17 (Defensive Goalkeeper), Ronaldo 4 (Prolific Winger), Messi 14
# (Creative Playmaker) — all four confirmed against pesdb.net's PES 2019 cards.
STYLE_NAMES = ("None", "Goal Poacher", "Dummy Runner", "Fox in the Box", "Prolific Winger",
               "Classic No. 10", "Hole Player", "Box-to-Box", "Anchor Man", "The Destroyer",
               "Extra Frontman", "Offensive Full-back", "Defensive Full-back", "Target Man",
               "Creative Playmaker", "Build Up", "Offensive Goalkeeper",
               "Defensive Goalkeeper", "Roaming Flank", "Cross Specialist", "Orchestrator",
               "Full-back Finisher")

# label -> bit. The seven COM playing styles come first in the block, then the player skills.
COM_STYLES = {
    "Trickster": 327,
    "Mazing Run": 368,
    "Speeding Bullet": 362,
    "Incisive Run": 347,
    "Long Ball Expert": 366,
    "Early Cross": 320,
    "Long Ranger": 356,
}
SKILLS = {
    "Scissors Feint": 355,
    "Double Touch": 359,
    "Flip Flap": 324,
    "Marseille Turn": 335,
    "Sombrero": 321,
    "Cross Over Turn": 287,
    "Cut Behind & Turn": 365,
    "Scotch Move": 361,
    "Step On Skill Control": 363,
    "Heading": 333,
    "Long Range Drive": 364,
    "Chip Shot Control": 343,
    "Knuckle Shot": 350,
    "Dipping Shots": 332,
    "Rising Shots": 337,
    "Acrobatic Finishing": 360,
    "Heel Trick": 342,
    "First-time Shot": 348,
    "One-touch Pass": 344,
    "Weighted Pass": 323,
    "Pinpoint Crossing": 322,
    "Outside Curler": 331,
    "Rabona": 352,
    "No Look Pass": 349,
    "Low Lofted Pass": 326,
    "GK Low Punt": 328,
    "GK High Punt": 334,
    "Long Throw": 357,
    "GK Long Throw": 358,
    "Penalty Specialist": 338,
    "GK Penalty Saver": 339,
    "Gamesmanship": 329,
    "Man Marking": 341,
    "Track Back": 354,
    "Interception": 340,
    "Acrobatic Clear": 367,
    "Captaincy": 330,
    "Super-sub": 353,
    "Fighting Spirit": 325,
}
# The remaining bits of the same block: myClub markers, not editable skills.
MARKERS = {"Legend": 345, "Hand": 346, "Ballon d'Or": 336}   # WinnerGoldenBall
FLAG_FIELDS = dict({f"Style: {name}": (bit, 1, 0) for name, bit in COM_STYLES.items()},
                   **{f"Skill: {name}": (bit, 1, 0) for name, bit in SKILLS.items()},
                   **{name: (bit, 1, 0) for name, bit in MARKERS.items()})
FIELD_BY_LABEL.update(FLAG_FIELDS)

PLAYER_STEMS = ("Player", "Player1", "Player2", "Player3", "Player4", "Player5", "Player6")
TEAM_STEMS = ("Team", "Team1", "Team2", "Team3", "Team4", "Team5", "Team6")
COACH_STEMS = ("Coach", "Coach1", "Coach2", "Coach3", "Coach4", "Coach5", "Coach6")
ASSIGN_STEMS = ("PlayerAssignment", "PlayerAssignment1", "PlayerAssignment2",
                "PlayerAssignment3", "PlayerAssignment4", "PlayerAssignment5",
                "PlayerAssignment6")
STADIUM_STEMS = ("Stadium",)


# ---------------------------------------------------------------- record bits
def bit_get(rec, bit, width, add=0):
    return ((int.from_bytes(rec, "little") >> bit) & ((1 << width) - 1)) + add


def bit_set(rec, bit, width, add, value):
    mask = (1 << width) - 1
    v = int.from_bytes(rec, "little")
    v = (v & ~(mask << bit)) | (((int(value) - add) & mask) << bit)
    rec[:] = v.to_bytes(len(rec), "little")


def encode_name(text, slot=NAME_LEN):
    """A name for one fixed-width slot: UTF-8, never split mid-codepoint, zero-padded."""
    b = text.encode("utf-8")
    if len(b) > slot - 1:
        b = b[:slot - 1]
        while True:                              # drop a codepoint the cut left incomplete
            try:
                b.decode("utf-8")
                break
            except UnicodeDecodeError:
                b = b[:-1]
    return b + b"\x00" * (slot - len(b))


# ---------------------------------------------------------------- WESYS payloads
def wesys_payload(records, prefix=PREFIX):
    """Records -> a file payload (prefix + WESYS + zlib), the form stored in the CPK."""
    raw = bytes(records)
    comp = zlib.compressobj(9, zlib.DEFLATED, 15, 9)
    packed = comp.compress(raw) + comp.flush()
    return prefix + b"WESYS" + struct.pack("<II", len(packed), len(raw)) + packed


def wesys_records(payload):
    """A file payload -> its decompressed bytes (both sizes checked, eof enforced)."""
    if payload[3:8] != b"WESYS":
        raise ValueError(f"no WESYS block in the payload ({payload[:8]!r})")
    csz, usz = struct.unpack_from("<II", payload, 8)
    d = zlib.decompressobj()
    out = d.decompress(payload[16:16 + csz]) + d.flush()
    if not d.eof or len(out) != usz:
        raise ValueError(f"short WESYS block: {len(out):,} of {usz:,} bytes")
    return out


# ---------------------------------------------------------------- tables
class PlayerTable:
    """One Player*.bin's records, editable and repackable back into its payload."""

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) % REC:
            raise ValueError(f"player table is not a multiple of {REC}: {len(raw)} bytes")
        self.prefix = prefix
        self.records = [bytearray(raw[i:i + REC]) for i in range(0, len(raw), REC)]

    def __len__(self):
        return len(self.records)

    # identity
    def pid(self, i):
        return struct.unpack_from("<I", self.records[i], ID_OFF)[0]

    def _slot(self, i, name):
        off = NAME_OFFS[name]
        return self.records[i][off:off + NAME_LEN].split(b"\x00")[0].decode("utf-8", "replace")

    def name(self, i):
        return self._slot(i, "full")

    def shirt(self, i):
        return self._slot(i, "shirt")

    def japanese(self, i):
        return self._slot(i, "japanese")

    def set_name(self, i, full=None, shirt=None, japanese=None):
        rec = self.records[i]
        for name, value in (("full", full), ("shirt", shirt), ("japanese", japanese)):
            if value is None:
                continue
            off = NAME_OFFS[name]
            rec[off:off + NAME_LEN] = encode_name(value)

    # fields
    def get(self, i, label):
        bit, width, add = FIELD_BY_LABEL[label]
        return bit_get(self.records[i], bit, width, add)

    def set(self, i, label, value):
        bit, width, add = FIELD_BY_LABEL[label]
        lo, hi = add, add + (1 << width) - 1
        bit_set(self.records[i], bit, width, add, max(lo, min(hi, int(value))))

    def find(self, needle):
        """Row indexes for a player id (digits) or a name / shirt-name substring."""
        needle = needle.strip()
        if needle.isdigit():
            pid = int(needle)
            return [i for i in range(len(self.records)) if self.pid(i) == pid]
        low = needle.lower()
        return [i for i in range(len(self.records))
                if low in self.name(i).lower() or low in self.shirt(i).lower()]

    def skills(self):
        """Label -> bit for everything in the skill / COM-style / marker block."""
        return FLAG_FIELDS

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- the CPK
class Cpk2019:
    """A PES 2019 CPK: reads any table, patches file payloads and the TOC."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, "rb") as f:
                head = f.read(0x2000)
        except OSError as exc:
            raise FileNotFoundError(getattr(exc, "errno", None) or 2, str(exc), path) from exc
        if head[:4] != b"CPK ":
            raise ValueError(f"{path}: not a CPK")
        self.header = cpk.Utf(head, 0x10)
        self.header_row = next(self.header.rows())
        self.toc_off = 0x10 + (self.header_row.get("TocOffset") or 0)
        size = (self.header_row.get("TocSize")
                or (self.header_row.get("ContentOffset") or 0) - self.toc_off)
        with open(path, "rb") as f:
            f.seek(self.toc_off)
            toc_buf = f.read(size)
        self.toc = cpk.Utf(toc_buf, 0)
        self.entries = [(r.get("FileName") or "", r.get("FileOffset") or 0,
                         r.get("FileSize") or 0, r.get("ExtractSize") or 0)
                        for r in self.toc.rows()]

    def __str__(self):
        return f"{os.path.basename(self.path)} ({len(self.entries):,} files)"

    def entry(self, stem):
        """(index, (name, offset, size, extract size)) of one file by stem, or None."""
        for i, (fn, off, size, xsize) in enumerate(self.entries):
            base = fn[:-4] if fn.endswith(".bin") else fn
            if base == stem and size:
                return i, (fn, off, size, xsize)
        return None

    def stems(self):
        """Every .bin stem in the archive that holds data."""
        return [fn[:-4] for fn, _o, size, _x in self.entries if size and fn.endswith(".bin")]

    def read_payload(self, off, size):
        """The stored payload of one entry (its WESYS wrapper, not decompressed)."""
        with open(self.path, "rb") as f:
            f.seek(off + LEAD)
            payload = f.read(size)
        if payload[3:8] != b"WESYS":
            raise ValueError(f"{self.path}: no WESYS payload at {off + LEAD:#x}")
        csz = struct.unpack_from("<II", payload, 8)[0]
        if 16 + csz != size:
            raise ValueError(f"{self.path}: payload at {off + LEAD:#x} is {16 + csz} bytes, "
                             f"the TOC says {size}")
        return payload

    def read_raw(self, stem):
        """(decompressed bytes, 3-byte prefix) of any WESYS table in the CPK."""
        found = self.entry(stem)
        if found is None:
            raise KeyError(f"{stem} not in {self.path}")
        _i, (_fn, off, size, _x) = found
        payload = self.read_payload(off, size)
        return wesys_records(payload), payload[:3]

    def read_table(self, stem):
        """PlayerTable for a Player*.bin stem, decompressed and eof-checked."""
        return PlayerTable(*self.read_raw(stem))

    def next_payload(self, off):
        """Start of the next file payload after this entry, or the end of the CPK."""
        starts = [o + LEAD for _fn, o, size, _x in self.entries if size and o > off]
        return min(starts) if starts else os.path.getsize(self.path)

    def patch(self, stem, payload, backup_dir=None, append=False):
        """Write a new payload for one file entry; returns (payload offset, appended?).

        In place when it fits the entry's slot (up to the next payload's start), else appended
        at the end of the CPK with the entry repointed — the shape the shipped files use.
        The caller's Cpk2019 is stale afterwards except for another patch() on the same object
        (the TOC it holds accumulates the changes); re-open to read the new state.
        """
        found = self.entry(stem)
        if found is None:
            raise KeyError(f"{stem} not in {self.path}")
        index, (_fn, off, size, _x) = found
        p_off = off + LEAD
        limit = self.next_payload(off)
        fits = not append and p_off + len(payload) <= limit
        # In place, the write can reach past the old payload's tail into the slot's slack;
        # back up everything that will be written so a restore undoes it exactly.
        span = max(size, len(payload)) if fits else size
        backup_regions(self.path, [(0x10, self.header.size),
                                   (self.toc_off, self.toc.size),
                                   (p_off, span)], backup_dir, stem)
        if fits:
            where = p_off
        else:
            end = os.path.getsize(self.path)
            where = (end + LEAD - 1) & ~(LEAD - 1)          # a fresh 2048-aligned payload
            self.toc.set_row_int(index, "FileOffset", where - LEAD)
        self._write(where, payload)
        self.toc.set_row_int(index, "FileSize", len(payload))
        self.toc.set_row_int(index, "ExtractSize", len(payload))
        if not fits:
            self._update_content_size()
        self._flush(self.toc, self.toc_off)
        return where, not fits

    def _write(self, off, data):
        with open(self.path, "r+b") as f:
            f.seek(off)
            f.write(data)

    def _flush(self, table, where):
        """Write a patched @UTF table back in its stored form (re-obfuscated when needed)."""
        self._write(where, table.cipher())

    def _update_content_size(self):
        """ContentSize tracks the packed content area; keep it right after an append."""
        content_off = self.header_row.get("ContentOffset") or 0
        if not content_off:
            return
        aligned = (os.path.getsize(self.path) - content_off + LEAD - 1) & ~(LEAD - 1)
        if self.header_row.get("ContentSize") == aligned:
            return
        self.header.set_row_int(0, "ContentSize", aligned)
        self._flush(self.header, 0x10)


Cpk = Cpk2019            # the name the vintage-agnostic panel/CLI code asks for


# ---------------------------------------------------------------- Team.bin (1468-byte rows)
TEAM_REC = 1468
TEAM_COACH_ID = 0         # u32: the coach id Coach.bin carries
TEAM_ID = 8               # u32: the id PlayerAssignment rows carry
TEAM_STADIUM_ID = 60      # u16: the stadium id, 0 = none
TEAM_NAME_SLOTS = {"japanese": 24, "full": 234, "short": 1398}
TEAM_NAME_LEN = 70


class TeamTable:
    """Team.bin rows: one team per 1468 bytes.

    Checked against the shipped tables: id 1 IRELAND, 108 BARCELONA, 100 MAN RED (the
    unlicensed Manchester United), and Barcelona's u32@0 resolves in Coach.bin to
    E. VALVERDE. Only @24 and @234 are filled on every row; the localized slots carry
    data on the 81 national-team rows alone, so set_names() writes those two and leaves
    the rest as shipped.
    """

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) % TEAM_REC:
            raise ValueError(f"team table is not a multiple of {TEAM_REC}: {len(raw)} bytes")
        self.prefix = prefix
        self.records = [bytearray(raw[i:i + TEAM_REC]) for i in range(0, len(raw), TEAM_REC)]

    def __len__(self):
        return len(self.records)

    def id(self, i):
        return struct.unpack_from("<I", self.records[i], TEAM_ID)[0]

    def coach_id(self, i):
        return struct.unpack_from("<I", self.records[i], TEAM_COACH_ID)[0]

    def stadium_id(self, i):
        return struct.unpack_from("<H", self.records[i], TEAM_STADIUM_ID)[0]

    def set_coach_id(self, i, coach_id):
        struct.pack_into("<I", self.records[i], TEAM_COACH_ID, int(coach_id))

    def set_stadium_id(self, i, stadium_id):
        struct.pack_into("<H", self.records[i], TEAM_STADIUM_ID, int(stadium_id))

    def _slot(self, i, off):
        return self.records[i][off:off + TEAM_NAME_LEN].split(b"\x00")[0].decode("utf-8", "replace")

    def name(self, i):
        return self._slot(i, TEAM_NAME_SLOTS["full"])

    def japanese(self, i):
        return self._slot(i, TEAM_NAME_SLOTS["japanese"])

    def short(self, i):
        return self._slot(i, TEAM_NAME_SLOTS["short"])

    def set_names(self, i, full=None, japanese=None, short=None):
        rec = self.records[i]
        for key, value in (("full", full), ("japanese", japanese), ("short", short)):
            if value is None:
                continue
            off = TEAM_NAME_SLOTS[key]
            rec[off:off + TEAM_NAME_LEN] = encode_name(value, TEAM_NAME_LEN)

    def find(self, needle):
        """Row indexes whose id (digits) or name matches."""
        needle = needle.strip()
        if needle.isdigit():
            tid = int(needle)
            return [i for i in range(len(self.records)) if self.id(i) == tid]
        low = needle.lower()
        return [i for i in range(len(self.records)) if low in self.name(i).lower()]

    def by_id(self):
        return {self.id(i): i for i in range(len(self.records))}

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- Coach.bin (100-byte rows)
COACH_REC = 100
COACH_ID = 0
COACH_NAME_OFF = 54
COACH_JAPANESE_OFF = 8
COACH_NAME_LEN = 45
COACH_COUNTRY = (32, 9, 0)          # bit, width, add
COACH_ADAPTABILITY = (41, 7, 40)


class CoachTable:
    """Coach.bin rows: 100 bytes — id, Japanese name, name, country, adaptability."""

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) % COACH_REC:
            raise ValueError(f"coach table is not a multiple of {COACH_REC}: {len(raw)} bytes")
        self.prefix = prefix
        self.records = [bytearray(raw[i:i + COACH_REC]) for i in range(0, len(raw), COACH_REC)]

    def __len__(self):
        return len(self.records)

    def id(self, i):
        return struct.unpack_from("<I", self.records[i], COACH_ID)[0]

    def _slot(self, i, off):
        return self.records[i][off:off + COACH_NAME_LEN].split(b"\x00")[0].decode("utf-8", "replace")

    def name(self, i):
        return self._slot(i, COACH_NAME_OFF)

    def japanese(self, i):
        return self._slot(i, COACH_JAPANESE_OFF)

    def country(self, i):
        return bit_get(self.records[i], *COACH_COUNTRY)

    def adaptability(self, i):
        return bit_get(self.records[i], *COACH_ADAPTABILITY)

    def set_names(self, i, name=None, japanese=None):
        rec = self.records[i]
        for off, value in ((COACH_NAME_OFF, name), (COACH_JAPANESE_OFF, japanese)):
            if value is None:
                continue
            rec[off:off + COACH_NAME_LEN] = encode_name(value, COACH_NAME_LEN)

    def find(self, needle):
        """Row indexes whose id (digits) or name matches."""
        needle = needle.strip()
        if needle.isdigit():
            cid = int(needle)
            return [i for i in range(len(self.records)) if self.id(i) == cid]
        low = needle.lower()
        return [i for i in range(len(self.records)) if low in self.name(i).lower()]

    def by_id(self):
        return {self.id(i): i for i in range(len(self.records))}

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ------------------------------------------------- PlayerAssignment.bin (16-byte rows)
ASSIGN_REC = 16
ASSIGN_PLAYER = 4            # u32: the player id
ASSIGN_TEAM = 8              # u32: the team id (below 100 national, 100+ clubs)
ASSIGN_NUMBER = 12           # byte: the shirt number, stored one below the shown one


class AssignmentTable:
    """PlayerAssignment.bin rows: player -> club, plus the shirt number.

    The number is stored one below what the game shows (Messi's rows carry 9 for the 10 he
    wore at Barcelona and for Argentina, C. Ronaldo's 6 for the 7 at MD WHITE), the same
    convention the PES 2021 tables use.
    """

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) % ASSIGN_REC:
            raise ValueError(f"assignment table is not a multiple of {ASSIGN_REC}: {len(raw)} bytes")
        self.prefix = prefix
        self.records = [bytearray(raw[i:i + ASSIGN_REC]) for i in range(0, len(raw), ASSIGN_REC)]

    def __len__(self):
        return len(self.records)

    def player(self, i):
        return struct.unpack_from("<I", self.records[i], ASSIGN_PLAYER)[0]

    def team(self, i):
        return struct.unpack_from("<I", self.records[i], ASSIGN_TEAM)[0]

    def number(self, i):
        """The shirt number the game shows (the stored byte plus one)."""
        return self.records[i][ASSIGN_NUMBER] + 1

    def set_number(self, i, value):
        self.records[i][ASSIGN_NUMBER] = max(0, min(255, int(value) - 1))

    def set_team(self, i, team_id):
        struct.pack_into("<I", self.records[i], ASSIGN_TEAM, int(team_id))

    def find(self, pid):
        """Row indexes for a player id."""
        pid = int(pid)
        return [i for i in range(len(self.records)) if self.player(i) == pid]

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- Stadium.bin (392-byte rows)
STADIUM_REC = 392
STADIUM_ID = 4
STADIUM_JAPANESE_OFF = 8
STADIUM_NAME_OFF = 189
STADIUM_IDSTRING_OFF = 370


class StadiumTable:
    """Stadium.bin rows: 392 bytes — id, Japanese name, English name, id string."""

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) % STADIUM_REC:
            raise ValueError(f"stadium table is not a multiple of {STADIUM_REC}: {len(raw)} bytes")
        self.prefix = prefix
        self.records = [bytearray(raw[i:i + STADIUM_REC]) for i in range(0, len(raw), STADIUM_REC)]

    def __len__(self):
        return len(self.records)

    def id(self, i):
        return struct.unpack_from("<H", self.records[i], STADIUM_ID)[0]

    def _slot(self, i, off, length):
        return self.records[i][off:off + length].split(b"\x00")[0].decode("utf-8", "replace")

    def name(self, i):
        return self._slot(i, STADIUM_NAME_OFF, 80)

    def japanese(self, i):
        return self._slot(i, STADIUM_JAPANESE_OFF, 80)

    def id_string(self, i):
        return self._slot(i, STADIUM_IDSTRING_OFF, 20)

    def find(self, needle):
        """Row indexes whose id (digits) or names match."""
        needle = needle.strip()
        if needle.isdigit():
            sid = int(needle)
            return [i for i in range(len(self.records)) if self.id(i) == sid]
        low = needle.lower()
        return [i for i in range(len(self.records))
                if low in self.name(i).lower() or low in self.japanese(i).lower()
                or low in self.id_string(i).lower()]

    def names(self):
        """The English name of every stadium, in row order."""
        return [self.name(i) for i in range(len(self.records))]

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- backups
def backup_regions(cpk_path, regions, backup_dir, tag):
    """Save the byte ranges a patch will touch (payload, TOC, header) plus the file size.

    Restore with restore_backup(): it rewrites the saved ranges and truncates the file back
    to its old size, so an in-place edit and an appended payload both undo exactly.
    """
    if not backup_dir:
        return None
    out = os.path.join(os.path.expanduser(backup_dir), "pes2019")
    os.makedirs(out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{os.path.basename(cpk_path)[:-4]}.{tag}.{stamp}"
    n = 1
    while os.path.exists(os.path.join(out, name + ".json")):
        n += 1                                     # same-second saves must not clobber
        name = f"{os.path.basename(cpk_path)[:-4]}.{tag}.{stamp}-{n}"
    blob = bytearray()
    index = []
    with open(cpk_path, "rb") as f:
        for off, length in regions:
            f.seek(off)
            data = f.read(length)
            index.append({"off": off, "len": len(data), "at": len(blob)})
            blob += data
    with open(os.path.join(out, name + ".bin"), "wb") as f:
        f.write(blob)
    with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as f:
        json.dump({"cpk": os.path.abspath(cpk_path), "table": tag, "stamp": stamp,
                   "file_size": os.path.getsize(cpk_path), "regions": index}, f, indent=2)
    return os.path.join(out, name)


def backup_list(backup_dir):
    """Backup base names (newest first) under a backups directory."""
    out = os.path.join(os.path.expanduser(backup_dir), "pes2019")
    if not os.path.isdir(out):
        return []
    names = [f[:-5] for f in os.listdir(out) if f.endswith(".json")]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(out, n + ".json")), reverse=True)
    return [os.path.join(out, n) for n in names]


def restore_backup(base):
    """Put one backup's ranges back, and truncate the file to its recorded size."""
    base = base[:-5] if base.endswith(".json") else base
    with open(base + ".json", encoding="utf-8") as f:
        meta = json.load(f)
    with open(base + ".bin", "rb") as f:
        blob = f.read()
    path = meta["cpk"]
    with open(path, "r+b") as f:
        for r in meta["regions"]:
            f.seek(r["off"])
            f.write(blob[r["at"]:r["at"] + r["len"]])
        if os.path.getsize(path) > meta["file_size"]:
            f.truncate(meta["file_size"])
    return meta


# ---------------------------------------------------------------- install discovery
GAME_DIRS = (
    "~/Games/Pro Evolution Soccer 2019",
    "~/.wine/drive_c/Program Files (x86)/Pro Evolution Soccer 2019",
    "~/.wine/drive_c/Program Files/Pro Evolution Soccer 2019",
    "~/.local/share/wineprefixes/*/drive_c/Program Files*/Pro Evolution Soccer 2019",
    "~/.steam/steam/steamapps/common/Pro Evolution Soccer 2019",
    "~/.local/share/Steam/steamapps/common/Pro Evolution Soccer 2019",
)


def is_2019_dir(path):
    """True for a PES 2019 install folder (it has the 64-bit database pack)."""
    return bool(path) and os.path.isfile(os.path.join(path, "Data", "dt10_x64.cpk"))


def find_game_dir():
    """An installed PES 2019 folder (PES2019_DIR wins), or "" when there is none."""
    env = os.environ.get("PES2019_DIR")
    candidates = ([env] if env else []) + [os.path.expanduser(p) for p in GAME_DIRS]
    for candidate in candidates:
        for path in glob.glob(candidate):
            if is_2019_dir(path):
                return path
    return ""


def mounted_packs(game_dir):
    """The data pack CPKs DpFileList.bin names — those are the ones the game mounts.

    DpFileList.bin is a small binary list; the printable .cpk names are all this reader
    needs, and they are checked against disk.
    """
    path = os.path.join(game_dir, "download", "DpFileList.bin")
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return []
    names, run = [], b""
    for b in blob + b"\x00":
        if 32 <= b < 127:
            run += bytes([b])
        else:
            if run.lower().endswith(b".cpk"):
                names.append(run.decode())
            run = b""
    out = []
    for name in names:
        if name not in out and os.path.isfile(os.path.join(game_dir, "download", name)):
            out.append(name)
    return out


def database_cpks(game_dir):
    """The CPKs that hold the tables, the one the game loads first first.

    `Data/dt00_x64.cpk` also carries these tables (byte-identical copies in this install)
    but the database pack is dt10, so that is what a writer touches; the packs listed in
    DpFileList.bin are mounted after it and win over it.
    """
    out = []
    base = os.path.join(game_dir, "Data", "dt10_x64.cpk")
    if os.path.isfile(base):
        out.append(base)
    for name in mounted_packs(game_dir):
        out.append(os.path.join(game_dir, "download", name))
    return out


def open_players(paths):
    """[(cpk_path, stem, PlayerTable)] for every Player*.bin across the given CPKs."""
    out = []
    for path in paths:
        db = Cpk2019(path)
        for stem in PLAYER_STEMS:
            if db.entry(stem):
                out.append((path, stem, db.read_table(stem)))
    return out

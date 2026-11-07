"""PES 2015 database: read and write the tables inside dt10.cpk / dt80_*.cpk.

Player record (180 bytes, little-endian bit fields counted from bit 0):
    id      u32 at +0
    names   +0x28 Japanese | +0x56 shirt | +0x84 full, 46-byte UTF-8 slots
    bio     nationality 8 bits at 32; height 7 bits at 50 (+132 cm); weight 7 bits at 57
            (+30 kg); age 6 bits at 192 (+15); playing style 5 bits at 198; form 3 bits at
            211 (+1); weak foot usage 2 bits at 233; injury resistance 2 bits at 237;
            weak foot accuracy 2 bits at 241; strong foot 1 bit at 279 (0 right, 1 left)
    primary position   4 bits at 0xcf (0 GK, 1 CB, ... 12 CF)
    abilities  19 six-bit fields, value = raw + 40
    flags      one bit each at 264..291 — the game's skill / COM-style block (see SKILLS,
               COM_STYLES): e.g. Marseille Turn 271, Mazing Run 291, Trickster 266

Team.bin rows are 700 bytes: u16 alt id @0, kind @2, roster id @8 (PlayerAssignment's team
ids; 108 Barcelona, 1 Ireland, 100 Manchester United); name slots — Japanese @24, 3-letter
code @48, English full name @234 and @304, short name @654.

Coach.bin rows are 108 bytes: u32 id @0, Japanese name @16, name @62.

PlayerAssignment.bin: 4-byte lead, 16-byte rows ([player id][team id][number+flags]
[counter]), 12-byte trailer. Team ids below 100 are national teams, 100+ clubs.

The player map was recovered from the game's own tables: abilities checked against pesdb.net
cards (Goalkeeping/Saving match exactly; outfield values sit a few points under the online
ratings, which is the shipped data pack vs the live site), the bio/flag fields matched ~200
pesdb cards bit-for-bit (values verified on L. Messi, C. Ronaldo, M. Neuer, G. Buffon).

CPK layout in these releases (2015-2017 vintage; no "TOC " magic, all @UTF tables XOR
obfuscated with CRI's LCG stream, see cpk.decrypt_utf):

    CPK header + @UTF table at 0x10
    TOC @UTF at 0x10 + header['TocOffset']
    each file: payload at FileOffset + 0x800 (the pack's 2048-byte alignment),
    payload = 3-byte prefix + 'WESYS' + u32 compressed + u32 uncompressed + zlib,
    and its length (16 + compressed) equals the TOC's FileSize and ExtractSize.

Writing re-packs a table's WESYS payload and patches it in place while it fits the entry's
slot (up to the next payload's start); a payload that outgrows the slot is appended at the
end of the CPK and the entry is repointed (FileOffset = new payload - 0x800, the shape the
shipped files use). Either way the TOC — and the header when ContentSize moves — is
re-obfuscated and written back, so the game reads the same format it shipped.
"""
import glob
import json
import os
import struct
import time
import zlib

import cpk

REC = 180
LEAD = 0x800               # a file's payload starts this far after its TOC FileOffset
ID_OFF = 0
NAME_OFFS = {"japanese": 0x28, "shirt": 0x56, "full": 0x84}
NAME_LEN = 46
PREFIX = b"\x00\x10\x81"   # the 3-byte prefix every Player*.bin payload carries

# primary position: 4 bits at 0xcf. Checked against C. Ronaldo (9 LWF), L. Messi (11 SS),
# M. Neuer (0 GK), S. Ramos (1 CB) and G. Buffon (0 GK).
PRIMARY_POSITION = 0xCF
POSITION_NAMES = ("GK", "CB", "LB", "RB", "DMF", "CMF", "LMF", "RMF",
                  "AMF", "LWF", "RWF", "SS", "CF")

# label -> bit offset (width 6, value = raw + 40)
ABILITIES = {
    "Place Kicking": 0x47,
    "Low Pass": 0x4D,
    "Defensive Prowess": 0x53,
    "Ball Control": 0x59,
    "Header": 0x60,
    "Jump": 0x66,
    "Speed": 0x6C,
    "Ball Winning": 0x72,
    "Saving": 0x78,
    "Goalkeeping": 0x80,
    "Swerve": 0x86,
    "Stamina": 0x8C,
    "Explosive Power": 0x92,
    "Dribbling": 0x98,
    "Kicking Power": 0xA0,
    "Attacking Prowess": 0xA6,
    "Body Balance": 0xAC,
    "Finishing": 0xB2,
    "Lofted Pass": 0xB8,
}
ABILITY_ADD = 40
ABILITY_WIDTH = 6

# label -> (bit, width, add). "Position" is the primary position, 0..12.
FIELD_BY_LABEL = {label: (bit, ABILITY_WIDTH, ABILITY_ADD) for label, bit in ABILITIES.items()}
FIELD_BY_LABEL["Position"] = (PRIMARY_POSITION, 4, 0)

# Bio + characteristics: recovered 2026-11-04 by cross-checking ~200 pesdb.net cards against
# these tables (height/weight/age/foot/weak foot/form/injury/style and the flag block all
# matched every card exactly or within a couple of shipped-data drifts). (bit, width, add).
BIO_FIELDS = {
    "Nationality": (32, 8, 0),          # country code (Country.bin's numbering)
    "Height": (50, 7, 132),             # cm
    "Weight": (57, 7, 30),              # kg
    "Age": (192, 6, 15),                # years, as of the shipped database
    "Playing Style": (198, 5, 0),       # style id (1..17+; Neuer 17 Offensive Goalkeeper)
    "Form": (211, 3, 1),                # 1..8
    "Weak Foot Usage": (233, 2, 1),     # 1..4
    "Injury Resistance": (237, 2, 1),   # 1..4
    "Weak Foot Accuracy": (241, 2, 1),  # 1..4
    "Foot": (279, 1, 0),                # 0 right, 1 left
}

# The 28-bit flag block at 264..291, in the game's own internal order (both families share it),
# plus "Early Cross" on the spare bit at 95. Labels come from the same cross-check; bit 275 did
# not appear on any of the ~200 cards.
SKILLS = {
    "Flip Flap": 264,
    "Fighting Spirit": 265,
    "Low Punt Trajectory": 267,
    "Captaincy": 268,
    "Outside Curler": 269,
    "Acrobatic Finishing": 270,
    "Marseille Turn": 271,
    "Man Marking": 272,
    "One-touch Pass": 273,
    "First-time Shot": 277,
    "Knuckle Shot": 278,
    "Super-sub": 280,
    "Scissors Feint": 282,
    "Long Throw": 283,
    "Weighted Pass": 284,
    "GK Long Throw": 285,
    "Scotch Move": 286,
    "Long Range Drive": 288,
    "Cut Behind & Turn": 289,
    "Track Back": 281,
}
COM_STYLES = {
    "Trickster": 266,
    "Long Ranger": 274,
    "Incisive Run": 276,
    "Speeding Bullet": 287,
    "Long Ball Expert": 290,
    "Mazing Run": 291,
    "Early Cross": 95,              # the gap bit inside the ability block; 3-card match
}
FLAG_FIELDS = dict({f"Skill: {name}": (bit, 1, 0) for name, bit in SKILLS.items()},
                   **{f"Style: {name}": (bit, 1, 0) for name, bit in COM_STYLES.items()})

FIELD_BY_LABEL.update(BIO_FIELDS)
FIELD_BY_LABEL.update(FLAG_FIELDS)

# the base table plus the numbered update slots (dt10 ships Player1/4/5, dt80 more)
PLAYER_STEMS = ("Player", "Player1", "Player2", "Player3", "Player4", "Player5", "Player6")


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
    """Records -> a file payload (prefix + WESYS + zlib), the form stored in the CPK.

    Level 9/memLevel 9 is the best standard zlib offers; the shipped streams are a few
    percent smaller than that (CRI's own compressor), so a re-packed table usually outgrows
    its slot and gets appended — see Cpk2015.patch.
    """
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

    def skills(self, i):
        """The set flags of the skill / COM-style block, by label."""
        return [label for label in FLAG_FIELDS if self.get(i, label)]

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- the CPK
class Cpk2015:
    """A PES 2015-vintage CPK: reads any table, patches file payloads and the TOC."""

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
        The caller's Cpk2015 is stale afterwards except for another patch() on the same object
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


# Cpk is the vintage-agnostic name the shared panel/CLI asks for.
Cpk = Cpk2015


# ---------------------------------------------------------------- Team.bin (700-byte rows)
TEAM_REC = 700
TEAM_ALT_ID = 0            # u16: the id the shipped edit data uses
TEAM_KIND = 2              # u16: 2 special, 4 club, 5 Master League, 6 national team, 7 Japan
TEAM_ID = 8                # u16: the roster id PlayerAssignment rows carry
TEAM_NAME_SLOTS = {"japanese": 24, "code": 48, "full": 234, "full2": 304, "short": 654}
TEAM_STEMS = ("Team", "Team1", "Team2", "Team3", "Team4", "Team5", "Team6")


class TeamTable:
    """Team.bin rows: one team per 700 bytes.

    Field map recovered from the shipped tables (roster ids cross-checked against
    PlayerAssignment rosters — 108 Barcelona, 50 Argentina, 1 Ireland, 100 Manchester
    United): u16 id@8 is the id PlayerAssignment rows carry, kind@2 says what the row is.
    Names: Japanese @24, 3-letter code @48, the English full name @234 and @304 (both
    carry it on every row that has one) and the short name @654 ('BARCELONA',
    'REAL_MADRID'). set_names() writes those five slots; the eleven other language slots
    the national teams carry stay as shipped.
    """

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) % TEAM_REC:
            raise ValueError(f"team table is not a multiple of {TEAM_REC}: {len(raw)} bytes")
        self.prefix = prefix
        self.records = [bytearray(raw[i:i + TEAM_REC]) for i in range(0, len(raw), TEAM_REC)]

    def __len__(self):
        return len(self.records)

    def id(self, i):
        return struct.unpack_from("<H", self.records[i], TEAM_ID)[0]

    def alt_id(self, i):
        return struct.unpack_from("<H", self.records[i], TEAM_ALT_ID)[0]

    def kind(self, i):
        return struct.unpack_from("<H", self.records[i], TEAM_KIND)[0]

    def _slot(self, i, name):
        off = TEAM_NAME_SLOTS[name]
        return self.records[i][off:off + NAME_LEN].split(b"\x00")[0].decode("utf-8", "replace")

    def name(self, i):
        return self._slot(i, "full") or self._slot(i, "full2")

    def short(self, i):
        return self._slot(i, "short")

    def japanese(self, i):
        return self._slot(i, "japanese")

    def code(self, i):
        return self._slot(i, "code")

    def set_names(self, i, full=None, short=None, japanese=None, code=None):
        """Write the name slots; 'full' covers both English slots (@234 and @304)."""
        rec = self.records[i]
        if full is not None:
            for off in (TEAM_NAME_SLOTS["full"], TEAM_NAME_SLOTS["full2"]):
                rec[off:off + NAME_LEN] = encode_name(full)
        for name, value in (("short", short), ("japanese", japanese), ("code", code)):
            if value is None:
                continue
            off = TEAM_NAME_SLOTS[name]
            rec[off:off + NAME_LEN] = encode_name(value)

    def find(self, needle):
        """Row indexes for a roster id (digits; alt-id fallback), or a name substring."""
        needle = needle.strip()
        low = needle.lower()
        if needle.isdigit():
            want = int(needle)
            rows = [i for i in range(len(self.records)) if self.id(i) == want]
            if rows:
                return rows                             # alt ids live in another namespace
            return [i for i in range(len(self.records)) if self.alt_id(i) == want]
        return [i for i in range(len(self.records))
                if low in self.name(i).lower() or low in self.short(i).lower()]

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- Coach.bin (108-byte rows)
COACH_REC = 108
COACH_ID = 0               # u32
COACH_JP = 16              # 46-byte name slot
COACH_NAME = 62            # 46-byte name slot
COACH_STEMS = ("Coach", "Coach1", "Coach2", "Coach3", "Coach4", "Coach5", "Coach6")


class CoachTable:
    """Coach.bin rows: u32 id@0, 12 unknown bytes, Japanese name @16, name @62."""

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
        return self.records[i][off:off + NAME_LEN].split(b"\x00")[0].decode("utf-8", "replace")

    def name(self, i):
        return self._slot(i, COACH_NAME)

    def japanese(self, i):
        return self._slot(i, COACH_JP)

    def set_names(self, i, name=None, japanese=None):
        rec = self.records[i]
        for off, value in ((COACH_NAME, name), (COACH_JP, japanese)):
            if value is not None:
                rec[off:off + NAME_LEN] = encode_name(value)

    def find(self, needle):
        """Row indexes for a coach id (digits) or a name substring."""
        needle = needle.strip()
        if needle.isdigit():
            cid = int(needle)
            return [i for i in range(len(self.records)) if self.id(i) == cid]
        low = needle.lower()
        return [i for i in range(len(self.records)) if low in self.name(i).lower()]

    def to_payload(self):
        return wesys_payload(b"".join(bytes(r) for r in self.records), self.prefix)


# ---------------------------------------------------------------- PlayerAssignment.bin
ASSIGN_LEAD = 4            # leading bytes before the first row
ASSIGN_REC = 16
ASSIGN_TRAIL = 12          # trailing bytes after the last row
ASSIGN_NUMBER_MASK = 0x7F  # the squad number lives in the low 7 bits of the number word
ASSIGN_STEMS = ("PlayerAssignment", "PlayerAssignment1", "PlayerAssignment2",
                "PlayerAssignment3", "PlayerAssignment4", "PlayerAssignment5",
                "PlayerAssignment6")


class AssignmentTable:
    """PlayerAssignment.bin: 4-byte lead + 16-byte rows + 12-byte trailer.

    A row is [player id u32][team id u32][number byte + flags][counter]. Team ids below
    100 are national teams, 100 and up clubs (verified against the rosters: 108
    Barcelona, 50 Argentina, 1 Ireland, 100 Manchester United); the number word's low
    seven bits are the squad number (Messi 10, Buffon 1, Pirlo 21), and a player usually
    has two rows — club and country. set_number() touches only the number bits.
    """

    def __init__(self, raw, prefix=PREFIX):
        if len(raw) < ASSIGN_LEAD + ASSIGN_TRAIL \
                or (len(raw) - ASSIGN_LEAD - ASSIGN_TRAIL) % ASSIGN_REC:
            raise ValueError(f"assignment table has an odd shape: {len(raw)} bytes")
        self.prefix = prefix
        self.lead = raw[:ASSIGN_LEAD]
        self.trailer = raw[len(raw) - ASSIGN_TRAIL:]
        body = raw[ASSIGN_LEAD:len(raw) - ASSIGN_TRAIL]
        self.records = [bytearray(body[i:i + ASSIGN_REC])
                        for i in range(0, len(body), ASSIGN_REC)]

    def __len__(self):
        return len(self.records)

    def player(self, i):
        return struct.unpack_from("<I", self.records[i], 0)[0]

    def team(self, i):
        return struct.unpack_from("<I", self.records[i], 4)[0]

    def number(self, i):
        return self.records[i][8] & ASSIGN_NUMBER_MASK

    def set_number(self, i, value):
        if not 0 <= int(value) <= ASSIGN_NUMBER_MASK:
            raise ValueError(f"squad number out of range: {value}")
        rec = self.records[i]
        rec[8] = (rec[8] & ~ASSIGN_NUMBER_MASK) | int(value)

    def set_team(self, i, team_id):
        struct.pack_into("<I", self.records[i], 4, int(team_id))

    def find(self, pid):
        """Row indexes for one player id."""
        return [i for i in range(len(self.records)) if self.player(i) == int(pid)]

    def to_payload(self):
        body = b"".join(bytes(r) for r in self.records)
        return wesys_payload(self.lead + body + self.trailer, self.prefix)


# ---------------------------------------------------------------- backups
def backup_regions(cpk_path, regions, backup_dir, tag):
    """Save the byte ranges a patch will touch (payload, TOC, header) plus the file size.

    Restore with restore_backup(): it rewrites the saved ranges and truncates the file back
    to its old size, so an in-place edit and an appended payload both undo exactly.
    """
    if not backup_dir:
        return None
    out = os.path.join(os.path.expanduser(backup_dir), "pes2015")
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
    out = os.path.join(os.path.expanduser(backup_dir), "pes2015")
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
    "~/Games/Pro Evolution Soccer 2015",
    "~/.wine/drive_c/Program Files (x86)/Pro Evolution Soccer 2015",
    "~/.wine/drive_c/Program Files/Pro Evolution Soccer 2015",
    "~/.local/share/wineprefixes/*/drive_c/Program Files*/Pro Evolution Soccer 2015",
    "~/.steam/steam/steamapps/common/Pro Evolution Soccer 2015",
    "~/.local/share/Steam/steamapps/common/Pro Evolution Soccer 2015",
)


def find_game_dir():
    """An installed PES 2015 folder (PES2015_DIR wins), or "" when there is none."""
    env = os.environ.get("PES2015_DIR")
    candidates = ([env] if env else []) + [os.path.expanduser(p) for p in GAME_DIRS]
    for candidate in candidates:
        for path in glob.glob(candidate):
            if os.path.isfile(os.path.join(path, "Data", "dt10.cpk")):
                return path
    return ""


def mounted_packs(game_dir):
    """The data pack CPKs DpFileList.bin names — those are the ones the game mounts.

    DpFileList.bin is a small binary list (100-byte entries after a 16-byte header); the
    printable .cpk names are all this reader needs, and they are checked against disk.
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
    """The CPKs that hold the player tables, base first: Data/dt10.cpk + the mounted packs."""
    out = []
    base = os.path.join(game_dir, "Data", "dt10.cpk")
    if os.path.isfile(base):
        out.append(base)
    for name in mounted_packs(game_dir):
        out.append(os.path.join(game_dir, "download", name))
    return out


def open_players(paths):
    """[(cpk_path, stem, PlayerTable)] for every Player*.bin across the given CPKs."""
    out = []
    for path in paths:
        db = Cpk2015(path)
        for stem in PLAYER_STEMS:
            if db.entry(stem):
                out.append((path, stem, db.read_table(stem)))
    return out

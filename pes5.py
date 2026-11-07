"""PES 5 (classic era) database: read and write the KONAMI-WIN32PES5OPT option file.

There is no CPK and no WESYS here — the whole database is one 1,250,304-byte file the game
reads at boot. This module gives it the same face the CPK releases present to the panel
(`pes2015.py` / `pes2019.py` / `pes2021.py`): `Cpk` with `read_raw` / `read_table` / `patch`,
a player table of fixed-size records, a team table and an assignment table.

    players 124-byte records: 5,000 market players at 36,872 + i*124, then the 184 created
            slots at 14,044 + k*124 (the game calls them 32768+k). A record has no id field —
            the index *is* the identity squads carry.
    names   the full name is UTF-16LE (15 characters) at +0, the shirt name a 16-byte
            single-byte slot at +32, the call name u16 at +48.
    stats   every stat is a little-endian u16 word at `rec[47+off]`, read as
            `(word >> shift) & mask` — i.e. a bit field over the record, which is why
            FIELD_BY_LABEL below can hold plain (bit, width, add) triples like every other
            vintage's map. Offsets are the PESFan Editor's own field table (Stats.java).
    clubs   138 x 140 bytes at 803,608; a club's name is a 49-byte slot at +0.
    squads  block 5: 32 slots per club (u16 player id at 667458 + c*64, number byte at
            659414 + c*32, stored one below the shown one, 255 = blank). The file keeps
            squads team-side, so assignment_blob() inverts them for the panel.

Crypto (all int math mod 2**32), derived from PESFan Editor 5.08 and byte-verified on three
live option files:
    convert()            data[i] ^= KEYPC[i % 256]                    (whole file)
    decrypt()/encrypt()  blocks 1..9 only, per 4-byte LE word, j cycling 0..366 over KEY:
                             dec: w = ((w - KEY[j]) + 1815549477) ^ 0x6C371625
                             enc: w = KEY[j] + ((w ^ 0x6C371625) - 1815549477)
    checksum             u32 LE at block[i]-8 == sum of block[i]'s u32 words, in the
                         post-convert, pre-decrypt domain. Save order: encrypt, sums, convert.

The game loads the file at boot, so close it before saving. On this box PES 5 Original Season
keeps it in ~/Documents/P5OSOF/... (the exe embeds a \\P5OSOF\\ redirect); plain PES 5 uses
~/Documents/KONAMI/... — find_game_dir()/option_file() resolve both.
"""
import json
import os
import shutil
import struct
import time

REC = 124
PLAYERS = 5000            # market players, at 36,872 + i*124
CREATED = 184             # user-created slots, at 14,044 + k*124, ids 32768+k
NAME_LEN = 16             # the full name's 15 characters + terminator, for the panel's limit
NATIONAL_MAX = 63         # teams 0..56 nations, 57..63 classics, 64..201 clubs
FILESIZE = 1250304
OPT_NAME = "KONAMI-WIN32PES5OPT"
SIG_OFF = 5132            # raw bytes `e7 b0 05 fa` — how a PC option file is told apart


class NotAnOptionFile(ValueError):
    """A file this format never held: wrong size, or no option-file signature.

    Kept distinct from a container error so the importer's probe can count it as "no Player
    entry for this release" — a CPK simply has no PES 5 table to find, which is not the same
    as corruption (the panel's own rule: report a missing entry as missing).
    """


EDIT_SAVE = False         # no EDIT0000 save shadows this one: the option file *is* the save

KEYPC = [
    115, 96, 225, 198, 31, 60, 173, 66, 11, 88, 185, 254, 55, 180, 5, 250,
    163, 80, 145, 54, 79, 44, 93, 178, 59, 72, 105, 110, 103, 164, 181, 106,
    211, 64, 65, 166, 127, 28, 13, 34, 107, 56, 25, 222, 151, 148, 101, 218,
    3, 48, 241, 22, 175, 12, 189, 146, 155, 40, 201, 78, 199, 132, 21, 74,
    51, 32, 161, 134, 223, 252, 109, 2, 203, 24, 121, 190, 247, 116, 197, 186,
    99, 16, 81, 246, 15, 236, 29, 114, 251, 8, 41, 46, 39, 100, 117, 42,
    147, 0, 1, 102, 63, 220, 205, 226, 43, 248, 217, 158, 87, 84, 37, 154,
    195, 240, 177, 214, 111, 204, 125, 82, 91, 232, 137, 14, 135, 68, 213, 10,
    243, 224, 97, 70, 159, 188, 45, 194, 139, 216, 57, 126, 183, 52, 133, 122,
    35, 208, 17, 182, 207, 172, 221, 50, 187, 200, 233, 238, 231, 36, 53, 234,
    83, 192, 193, 38, 255, 156, 141, 162, 235, 184, 153, 94, 23, 20, 229, 90,
    131, 176, 113, 150, 47, 140, 61, 18, 27, 168, 73, 206, 71, 4, 149, 202,
    179, 160, 33, 6, 95, 124, 237, 130, 75, 152, 249, 62, 119, 244, 69, 58,
    227, 144, 209, 118, 143, 108, 157, 242, 123, 136, 169, 174, 167, 228, 245, 170,
    19, 128, 129, 230, 191, 92, 77, 98, 171, 120, 89, 30, 215, 212, 165, 26,
    67, 112, 49, 86, 239, 76, 253, 210, 219, 104, 9, 142, 7, 196, 85, 138]

KEY = [
    1815549542, 1815549585, 1815549585, 1815549509, 1815549566, 1815549588, 1815549594, 1815549591, 1815549509, 1815549560, 1815549588, 1815549576, 1815549576, 1815549578, 1815549591, 1815549509,
    1815549548, 1815549574, 1815549586, 1815549578, 1815549509, 1815549542, 1815549591, 1815549578, 1815549509, 1815549543, 1815549578, 1815549585, 1815549588, 1815549587, 1815549580, 1815549509,
    1815549561, 1815549588, 1815549509, 1815549564, 1815549546, 1815549523, 1815549350, 1815549541, 1815549362, 1815549418, 1815549364, 1815549358, 1815549351, 1815549425, 1815549370, 1815549403,
    1815549351, 1815549426, 1815549351, 1815549389, 1815549371, 1815549462, 1815549366, 1815549390, 1815549351, 1815549421, 1815549380, 1815549394, 1815549372, 1815549363, 1815549351, 1815549418,
    1815549350, 1815549543, 1815549351, 1815549420, 1815549351, 1815549385, 1815549351, 1815549447, 1815549351, 1815549452, 1815549352, 1815549546, 1815549352, 1815549544, 1815549352, 1815549544,
    1815549352, 1815549361, 1815549351, 1815549425, 1815549352, 1815549579, 1815549350, 1815549568, 1815549352, 1815549571, 1815549351, 1815549461, 1815549358, 1815549473, 1815549366, 1815549383,
    1815549351, 1815549402, 1815549351, 1815549417, 1815549352, 1815549550, 1815549350, 1815549568, 1815549352, 1815549555, 1815549352, 1815549563, 1815549352, 1815549356, 1815549352, 1815549368,
    1815549351, 1815549418, 1815549364, 1815549385, 1815549369, 1815549353, 1815549351, 1815549402, 1815549351, 1815549417, 1815549351, 1815549383, 1815549351, 1815549454, 1815549369, 1815549598,
    1815549351, 1815549391, 1815549360, 1815549364, 1815549351, 1815549454, 1815549351, 1815549452, 1815549351, 1815549402, 1815549351, 1815549383, 1815549350, 1815549542, 1815549360, 1815549445,
    1815549366, 1815549552, 1815549351, 1815549391, 1815549372, 1815549362, 1815549351, 1815549441, 1815549351, 1815549421, 1815549351, 1815549383, 1815549361, 1815549415, 1815549365, 1815549585,
    1815549351, 1815549447, 1815549350, 1815549542, 1815549352, 1815549355, 1815549350, 1815549568, 1815549352, 1815549562, 1815549350, 1815549568, 1815549359, 1815549433, 1815549351, 1815549425,
    1815549352, 1815549600, 1815549352, 1815549358, 1815549352, 1815549368, 1815549352, 1815549578, 1815549352, 1815549543, 1815549352, 1815549542, 1815549351, 1815549418, 1815549352, 1815549579,
    1815549350, 1815549568, 1815549352, 1815549571, 1815549351, 1815549425, 1815549351, 1815549447, 1815549351, 1815549453, 1815549363, 1815549451, 1815549351, 1815549453, 1815549351, 1815549461,
    1815549351, 1815549404, 1815549351, 1815549454, 1815549351, 1815549425, 1815549351, 1815549426, 1815549352, 1815549558, 1815549350, 1815549568, 1815549352, 1815549349, 1815549351, 1815549422,
    1815549366, 1815549427, 1815549351, 1815549404, 1815549351, 1815549454, 1815549364, 1815549459, 1815549369, 1815549554, 1815549351, 1815549461, 1815549359, 1815549401, 1815549351, 1815549403,
    1815549351, 1815549454, 1815549351, 1815549402, 1815549350, 1815549542, 1815549351, 1815549398, 1815549351, 1815549412, 1815549351, 1815549452, 1815549351, 1815549419, 1815549351, 1815549402,
    1815549351, 1815549417, 1815549351, 1815549445, 1815549359, 1815549461, 1815549351, 1815549402, 1815549351, 1815549383, 1815549361, 1815549413, 1815549351, 1815549453, 1815549351, 1815549411,
    1815549351, 1815549396, 1815549351, 1815549420, 1815549350, 1815549542, 1815549364, 1815549385, 1815549369, 1815549353, 1815549363, 1815549587, 1815549351, 1815549444, 1815549351, 1815549452,
    1815549351, 1815549455, 1815549351, 1815549454, 1815549351, 1815549419, 1815549351, 1815549398, 1815549351, 1815549412, 1815549351, 1815549452, 1815549351, 1815549419, 1815549351, 1815549402,
    1815549351, 1815549417, 1815549351, 1815549445, 1815549366, 1815549599, 1815549367, 1815549453, 1815549351, 1815549402, 1815549351, 1815549417, 1815549351, 1815549421, 1815549351, 1815549383,
    1815549363, 1815549371, 1815549351, 1815549422, 1815549351, 1815549421, 1815549351, 1815549454, 1815549351, 1815549425, 1815549351, 1815549418, 1815549352, 1815549580, 1815549352, 1815549358,
    1815549352, 1815549594, 1815549352, 1815549360, 1815549371, 1815549581, 1815549363, 1815549603, 1815549351, 1815549425, 1815549351, 1815549410, 1815549351, 1815549444, 1815549351, 1815549422,
    1815549357, 1815549416, 1815549362, 1815549355, 1815549358, 1815549408, 1815549351, 1815549402, 1815549351, 1815549441, 1815549351, 1815549404, 1815549350, 1815549543, 1815549477,]

BLOCK = [12, 5144, 9508, 14044, 36872, 657712, 803608, 822940, 969192, 1228568]
BLOBSIZE = [4948, 1232, 4520, 22816, 620000, 145880, 19320, 146240, 259364, 21016]
PLAYER_BYTES = BLOBSIZE[4]
CREATED_BYTES = BLOBSIZE[3]


# ---------------------------------------------------------------- crypto
def convert(buf):
    """The whole-file XOR (its own inverse)."""
    for i in range(len(buf)):
        buf[i] ^= KEYPC[i & 255]


def _cipher(buf, enc):
    """The per-word block cipher over blocks 1..9 (encrypt or decrypt)."""
    for b in range(1, 10):
        j = 0
        for k in range(BLOCK[b], BLOCK[b] + BLOBSIZE[b], 4):
            w = struct.unpack_from("<I", buf, k)[0]
            if enc:
                w = (KEY[j] + ((w ^ 0x6C371625) - 1815549477)) & 0xFFFFFFFF
            else:
                w = (((w - KEY[j]) + 1815549477) & 0xFFFFFFFF) ^ 0x6C371625
            struct.pack_into("<I", buf, k, w)
            j = (j + 1) % 367


def _sums(buf):
    """Every block's summed u32 words, as stored at block-start - 8."""
    out = []
    for b in range(10):
        s = 0
        for k in range(BLOCK[b], BLOCK[b] + BLOBSIZE[b], 4):
            s = (s + struct.unpack_from("<I", buf, k)[0]) & 0xFFFFFFFF
        out.append(s)
    return out


def load(path):
    """(plaintext byte array, [(block, stored, computed), ...] for bad checksums).

    The checksums are read in the post-convert, pre-decrypt domain, exactly where the game
    writes them — a file whose sums do not add up still loads (a sloppy third-party editor
    may have left it that way), and the caller can say so.
    """
    buf = bytearray(open(path, "rb").read())
    if len(buf) != FILESIZE:
        raise NotAnOptionFile(f"{path}: {len(buf):,} bytes, not a PC PES 5 option file "
                              f"({FILESIZE:,})")
    if buf[SIG_OFF:SIG_OFF + 4] != b"\xe7\xb0\x05\xfa":
        raise NotAnOptionFile(f"{path}: no PES 5 option-file signature at {SIG_OFF:#x}")
    convert(buf)
    bad = []
    for i, s in enumerate(_sums(buf)):
        stored = struct.unpack_from("<I", buf, BLOCK[i] - 8)[0]
        if stored != s:
            bad.append((i, hex(stored), hex(s)))
    _cipher(buf, enc=False)
    return buf, bad


def save(data, path):
    """Encode a plaintext buffer and write it: encrypt, checksums, convert."""
    buf = bytearray(data)
    _cipher(buf, enc=True)
    for i, s in enumerate(_sums(buf)):
        struct.pack_into("<I", buf, BLOCK[i] - 8, s)
    convert(buf)
    with open(path, "wb") as f:
        f.write(buf)


# ---------------------------------------------------------------- field map
def _bits(off, shift, mask, add=0):
    """A stat's (bit, width, add) over the record: words live at rec[47+off]."""
    return ((47 + off) * 8 + shift, mask.bit_length(), add)


# (label, off, shift, mask) — the PESFan Editor's own ability table (Stats.java), whose names
# Ability99Panel shows. 26 fields, raw 0..127 (the game tops out at 99).
_STATS = (
    ("Attack", 7, 0, 127), ("Defence", 7, 7, 127), ("Balance", 9, 0, 127),
    ("Stamina", 9, 7, 127), ("Speed", 10, 6, 127), ("Acceleration", 11, 5, 127),
    ("Response", 13, 0, 127), ("Agility", 13, 7, 127), ("Dribble Accuracy", 14, 6, 127),
    ("Dribble Speed", 15, 5, 127), ("Short Pass Accuracy", 17, 0, 127),
    ("Short Pass Speed", 17, 7, 127), ("Long Pass Accuracy", 18, 6, 127),
    ("Long Pass Speed", 19, 5, 127), ("Shot Accuracy", 21, 0, 127),
    ("Shot Power", 21, 7, 127), ("Shot Technique", 22, 6, 127), ("Free Kick", 23, 5, 127),
    ("Curling", 25, 0, 127), ("Heading", 25, 7, 127), ("Jump", 26, 6, 127),
    ("Technique", 29, 0, 127), ("Aggression", 29, 7, 127), ("Mentality", 30, 6, 127),
    ("GK Ability", 31, 5, 127), ("Team Work", 33, 0, 127),
)
# (label, off, shift, mask, add) — bio fields, the same addressing. Height +148 cm, age +15
# years, the 1..8 fields (condition, consistency, weak foot) stored one below the shown value.
_BIO = (
    ("Height", 41, 0, 63, 148), ("Weight", 41, 6, 127, 0), ("Age", 62, 5, 31, 15),
    ("Nationality", 63, 2, 127, 0), ("Condition", 34, 2, 7, 1), ("Consistency", 33, 7, 7, 1),
    ("Weak Foot Accuracy", 34, 5, 7, 1), ("Weak Foot Frequency", 35, 0, 7, 1),
    ("Injury Tolerance", 35, 3, 3, 0), ("Position", 6, 4, 15, 0), ("Foot", 5, 0, 1, 0),
    # the panel's one style row edits the dribble style (the editor's own type 5: shown +1)
    ("Playing Style", 6, 0, 3, 1),
)
# (label, off, shift) — one bit each, in the edit menu's own order and under the games' own
# names: the strings the installs carry in dat/e_text.afs (PES 5 and PES 6 byte-identical),
# not the PESFan editor's abbreviations — "Anti-Dribble" and "1-T Pass" are in neither game.
_SPECIALS = (
    ("Dribbling", 24, 4), ("Tactical Dribble", 24, 5), ("Positioning", 24, 6), ("Reaction", 24, 7),
    ("Playmaking", 28, 4), ("Passing", 28, 5), ("Scoring", 28, 6), ("1-1 Scoring", 28, 7),
    ("Post Player", 32, 4), ("Lines", 32, 5), ("Middle Shooting", 32, 6), ("Side", 32, 7),
    ("Centre", 35, 5), ("Penalties", 35, 6), ("1-Touch Pass", 35, 7), ("Outside", 36, 0),
    ("Marking", 36, 1), ("Sliding Tackle", 36, 2), ("Covering", 36, 3), ("D-Line Control", 36, 4),
    ("Penalty Stopper", 36, 5), ("1-on-1 Stopper", 36, 6), ("Long Throw", 36, 7),
    ("GK Kick", 20, 7),
)

ABILITIES = tuple(label for label, _o, _s, _m in _STATS)
SKILLS = tuple(label for label, _o, _s in _SPECIALS)
COM_STYLES = ()            # the classic era has one flag block: the specials are the chips
STYLE_NAMES = None         # no playing-style list: the style row is the numeric dribble style

POSITION_NAMES = ("GK", "?", "CWP", "CBT", "SB", "DM", "WB", "CM", "SM", "AM", "WG", "SS", "CF")
NATION_NAMES = (
    "Austria", "Belgium", "Bulgaria", "Croatia", "Czech Republic", "Denmark", "England",
    "Finland", "France", "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia",
    "Netherlands", "Northern Ireland", "Norway", "Poland", "Portugal", "Romania", "Russia",
    "Scotland", "Serbia and Montenegro", "Slovakia", "Slovenia", "Spain", "Sweden",
    "Switzerland", "Turkey", "Ukraine", "Wales", "Cameroon", "Cote d'Ivoire", "Morocco",
    "Nigeria", "Senegal", "South Africa", "Tunisia", "Costa Rica", "Mexico", "USA", "Argentina",
    "Brazil", "Chile", "Colombia", "Ecuador", "Paraguay", "Peru", "Uruguay", "Venezuela",
    "China", "Iran", "Japan", "Saudi Arabia", "South Korea", "Australia", "Albania", "Armenia",
    "Belarus", "Bosnia and Herzegovina", "Cyprus", "Georgia", "Estonia", "Faroe Islands",
    "Iceland", "Israel", "Lithuania", "Luxembourg", "Macedonia", "Moldova", "Algeria", "Angola",
    "Burkina Faso", "Cape Verde", "Congo", "DR Congo", "Egypt", "Equatorial Guinea", "Gabon",
    "Gambia", "Ghana", "Guinea", "Guinea-Bissau", "Liberia", "Libya", "Mali", "Mauritius",
    "Mozambique", "Namibia", "Sierra Leone", "Togo", "Zambia", "Zimbabwe", "Canada", "Grenada",
    "Guadeloupe", "Guatemala", "Honduras", "Jamaica", "Martinique", "Netherlands Antilles",
    "Panama", "Trinidad and Tobago", "Bolivia", "Guyana", "Uzbekistan", "New Zealand",
    "Free Nationality")

# The panel's bio grid, in the game's own order; its ranges are the fields' real ones.
BIO_LABELS = ("Age", "Height", "Weight", "Nationality", "Consistency", "Condition",
              "Weak Foot Accuracy", "Weak Foot Frequency", "Injury Tolerance")
BIO_RANGES = {"Height": (148, 211), "Weight": (40, 127), "Age": (15, 46),
              "Nationality": (0, len(NATION_NAMES) - 1), "Consistency": (1, 8),
              "Condition": (1, 8), "Weak Foot Accuracy": (1, 8), "Weak Foot Frequency": (1, 8),
              "Injury Tolerance": (0, 2)}
# A stored code the number alone cannot explain: offered as the game's own letters.
CHOICE_FIELDS = {"Injury Tolerance": ("C", "B", "A")}

# The panel's own row labels: the record has the two name slots, no Japanese one.
NAME_SLOTS = (("Name", "full"), ("Shirt Name", "shirt"))

# PES 5's registered positions, mapped onto the reference-rating model's own position ids
# (0 GK, 1 CB, 2/3 LB/RB, 4 DMF, 5 CMF, 6/7 LMF/RMF, 8 AMF, 9/10 LWF/RWF, 11 SS, 12 CF).
OVR_POSITION = {0: 0, 2: 1, 3: 1, 4: 2, 5: 4, 6: 2, 7: 5, 8: 6, 9: 8, 10: 9, 11: 11, 12: 12}

FIELD_BY_LABEL = {}
for _label, _off, _shift, _mask in _STATS:
    FIELD_BY_LABEL[_label] = _bits(_off, _shift, _mask)
for _label, _off, _shift, _mask, _add in _BIO:
    FIELD_BY_LABEL[_label] = _bits(_off, _shift, _mask, _add)
for _label, _off, _shift in _SPECIALS:
    FIELD_BY_LABEL["Skill: " + _label] = _bits(_off, _shift, 1)


# ---------------------------------------------------------------- bit fields
def bit_get(rec, bit, width, add=0):
    return ((int.from_bytes(rec, "little") >> bit) & ((1 << width) - 1)) + add


def bit_set(rec, bit, width, add, value):
    mask = (1 << width) - 1
    v = int.from_bytes(rec, "little")
    v = (v & ~(mask << bit)) | (((int(value) - add) & mask) << bit)
    rec[:] = v.to_bytes(len(rec), "little")


def encode_name(text):
    """The full name's slot: UTF-16LE, 15 characters at most, zero-padded (Player.setName)."""
    b = text.encode("utf-16-le")[:30]
    return b + b"\x00" * (32 - len(b))


def encode_shirt(text):
    """The shirt-name slot (TransferPanel.setShirtName): uppercase A-Z . _ space, 15 bytes."""
    out = bytearray(16)
    for i, ch in enumerate(text.upper()[:15]):
        out[i] = ord(ch) if 65 <= ord(ch) <= 90 or ch in "._ " else 32
    return bytes(out)


# ---------------------------------------------------------------- players
class PlayerTable:
    """The option file's player records: 5,000 market players, then the 184 created slots.

    A record carries no id — the index *is* the identity the squads and the game use (created
    slots answer to 32768+k), so pid() is index arithmetic, not a read.
    """

    def __init__(self, raw, prefix=None):
        if len(raw) != (PLAYERS + CREATED) * REC:
            raise ValueError(f"player table is not {PLAYERS + CREATED} x {REC}: {len(raw)} bytes")
        self.records = [bytearray(raw[i:i + REC]) for i in range(0, len(raw), REC)]

    def __len__(self):
        return len(self.records)

    def pid(self, i):
        return i if i < PLAYERS else 32768 + i - PLAYERS

    def _utf16(self, i, off):
        rec = self.records[i]
        end = 30
        for n in range(0, 30, 2):
            if rec[off + n] == 0 and rec[off + n + 1] == 0:
                end = n
                break
        return bytes(rec[off:off + end]).decode("utf-16-le", "replace")

    def name(self, i):
        return self._utf16(i, 0)

    def shirt(self, i):
        """The shirt name: a 16-byte single-byte slot at +32 (uppercase, NUL-terminated)."""
        return bytes(self.records[i][32:48]).split(b"\x00")[0].decode("latin-1")

    def japanese(self, i):
        """No such slot in this era; the panel leaves the row out (NAME_SLOTS)."""
        return ""

    def get(self, i, label):
        bit, width, add = FIELD_BY_LABEL[label]
        return bit_get(self.records[i], bit, width, add)

    def set(self, i, label, value):
        bit, width, add = FIELD_BY_LABEL[label]
        hi = add + (1 << width) - 1
        if label in ABILITIES:
            hi = min(hi, 99)          # the bits allow 127; the game's own scale stops at 99
        bit_set(self.records[i], bit, width, add, max(add, min(hi, int(value))))

    def set_name(self, i, full=None, shirt=None, japanese=None):
        """Write a name slot, with the edited-marks the PESFan editor sets (Player.setName).

        The call-name word becomes 0xCDCD on a rename (the game then uses no commentary name),
        and the record's own name/shirt edited bits are raised — the same bytes a name edited
        in-game ends up with here.
        """
        rec = self.records[i]
        if full is not None:
            rec[0:32] = encode_name(full)
            rec[48:50] = b"\xcd\xcd"
            bit_set(rec, 400, 1, 0, 1)
            bit_set(rec, 402, 1, 0, 1)
        if shirt is not None:
            rec[32:48] = encode_shirt(shirt)
            bit_set(rec, 401, 1, 0, 1)

    def find(self, needle):
        """Row indexes for a player id (digits), or a name / shirt-name substring."""
        needle = needle.strip()
        if needle.isdigit():
            pid = int(needle)
            if pid < PLAYERS:
                return [pid]
            if 32768 <= pid < 32768 + CREATED:
                return [PLAYERS + pid - 32768]
            return []
        low = needle.lower()
        return [i for i in range(len(self.records))
                if low in self.name(i).lower() or low in self.shirt(i).lower()]

    def to_payload(self):
        """The two regions a save writes back, in the order read_raw() hands them out."""
        return b"".join(bytes(r) for r in self.records)


# ---------------------------------------------------------------- clubs / squads
CLUB_BLOCK = BLOCK[6]
CLUB_REC = 140
CLUBS = 138
TEAM_REC = 72              # the synthetic team table Cpk.read_raw("Team") builds
SQUAD_BLOCK = BLOCK[5]     # club slots: 667458 + c*64, numbers: 659414 + c*32


def club_name(data, c):
    """A club's name: a 49-byte slot, UTF-8 with a cp1252 fallback for legacy bytes."""
    raw = bytes(data[CLUB_BLOCK + c * CLUB_REC: CLUB_BLOCK + c * CLUB_REC + 49]).split(b"\x00")[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def squad(data, team):
    """A club's (player ids, shirt numbers) in storage-slot order; number None when blank.

    Teams are numbered 0..56 nations, 57..63 classics, 64..201 clubs (the canonical squad
    index); only the clubs are read here.
    """
    c = team - 64
    slots = SQUAD_BLOCK + 9746 + c * 64          # 667458: club slots, 64 bytes each
    nums = SQUAD_BLOCK + 1702 + c * 32           # 659414: club numbers, u8 each
    ids, numbers = [], []
    for s in range(32):
        pid = data[slots + s * 2] | (data[slots + s * 2 + 1] << 8)
        n = data[nums + s]
        ids.append(pid)
        numbers.append(None if n == 255 else n + 1)
    return ids, numbers


def team_blob(data):
    """The synthetic table the panel's TeamTable reads: club id + name, nothing else."""
    out = bytearray()
    for c in range(CLUBS):
        name = club_name(data, c).encode("utf-8")[:TEAM_REC - 2]
        out += struct.pack("<H", 64 + c) + name + b"\x00" * (TEAM_REC - 2 - len(name))
    return bytes(out)


def assignment_blob(data):
    """One 6-byte row per club squad member: [player id][team id][shirt number][0].

    The file keeps squads team-side — a list of player slots per team — so the panel's
    player-to-club question has to be answered by inverting them here. Only clubs are
    emitted: a national-team row would shadow the club row in the panel's own "first row
    above NATIONAL_MAX wins" reading of this table.
    """
    out = bytearray()
    for team in range(64, 64 + CLUBS):
        ids, numbers = squad(data, team)
        for pid, number in zip(ids, numbers):
            if pid in (0, 0xFFFF) or number is None:     # empty slot / no number assigned
                continue
            out += struct.pack("<HHB", pid, team, min(number, 255)) + b"\x00"
    return bytes(out)


class TeamTable:
    """The 138 clubs, read from the synthetic blob Cpk.read_raw("Team") builds.

    National-team names live in the exe, not in the file, so they are not here — the panel
    only needs the clubs (a player's club is what it shows).
    """

    def __init__(self, raw, prefix=None):
        if len(raw) % TEAM_REC:
            raise ValueError(f"team table is not a multiple of {TEAM_REC}: {len(raw)} bytes")
        self.records = [bytearray(raw[i:i + TEAM_REC]) for i in range(0, len(raw), TEAM_REC)]

    def __len__(self):
        return len(self.records)

    def id(self, i):
        return struct.unpack_from("<H", self.records[i], 0)[0]

    def name(self, i):
        return self.records[i][2:].split(b"\x00")[0].decode("utf-8", "replace")


class AssignmentTable:
    """The inverted squads: one row per club player. Read-only — a save writes players."""

    ASSIGN_REC = 6

    def __init__(self, raw, prefix=None):
        if len(raw) % self.ASSIGN_REC:
            raise ValueError(f"assignment table is not a multiple of {self.ASSIGN_REC}: "
                             f"{len(raw)} bytes")
        self.records = [bytearray(raw[i:i + self.ASSIGN_REC])
                        for i in range(0, len(raw), self.ASSIGN_REC)]

    def __len__(self):
        return len(self.records)

    def player(self, i):
        return struct.unpack_from("<H", self.records[i], 0)[0]

    def team(self, i):
        return struct.unpack_from("<H", self.records[i], 2)[0]

    def number(self, i):
        return self.records[i][4]


# ---------------------------------------------------------------- backups
def backup_file(path, backup_dir, tag):
    """Keep the whole option file beside its stamp — it is 1.2 MB, regions buy nothing."""
    if not backup_dir:
        return None
    out = os.path.join(os.path.expanduser(backup_dir), "pes5")
    os.makedirs(out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{OPT_NAME}.{tag}.{stamp}"
    n = 1
    while os.path.exists(os.path.join(out, name + ".json")):
        n += 1                                     # same-second saves must not clobber
        name = f"{OPT_NAME}.{tag}.{stamp}-{n}"
    shutil.copyfile(path, os.path.join(out, name + ".bin"))
    with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as f:
        json.dump({"cpk": os.path.abspath(path), "table": tag, "stamp": stamp,
                   "file_size": os.path.getsize(path)}, f, indent=2)
    return os.path.join(out, name)


def backup_list(backup_dir):
    """Backup base names (newest first) under a backups directory."""
    out = os.path.join(os.path.expanduser(backup_dir), "pes5")
    if not os.path.isdir(out):
        return []
    names = [f[:-5] for f in os.listdir(out) if f.endswith(".json")]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(out, n + ".json")), reverse=True)
    return [os.path.join(out, n) for n in names]


def restore_backup(base):
    """Put a whole-file backup back, byte for byte."""
    base = base[:-5] if base.endswith(".json") else base
    with open(base + ".json", encoding="utf-8") as f:
        meta = json.load(f)
    shutil.copyfile(base + ".bin", meta["cpk"])
    return meta


# ---------------------------------------------------------------- the file
class Cpk:
    """A PES 5 option file, in the shape the panel asks of a release's database.

    The name is the panel's, not the format's: there is no container here — the file *is*
    the database. patch() rewrites it whole (re-encrypted, checksums recomputed) after
    backing it up, so a save is one file and one backup.
    """

    def __init__(self, path):
        self.path = path
        size = os.path.getsize(path)               # a missing file: FileNotFoundError, as usual
        if size != FILESIZE:
            raise NotAnOptionFile(f"{path}: {size:,} bytes, not a PC PES 5 option file "
                                  f"({FILESIZE:,})")
        self.data, self.bad_sums = load(path)

    def __str__(self):
        return f"{os.path.basename(self.path)} (PES 5 option file)"

    def read_raw(self, stem):
        """(records, prefix) of a table the panel asks for — the three this file carries."""
        if stem == "Player":
            return (bytes(self.data[BLOCK[4]:BLOCK[4] + PLAYER_BYTES])
                    + bytes(self.data[BLOCK[3]:BLOCK[3] + CREATED_BYTES]), None)
        if stem == "Team":
            return team_blob(self.data), None
        if stem == "PlayerAssignment":
            return assignment_blob(self.data), None
        raise KeyError(f"{stem} not in {self.path}")

    def read_table(self, stem):
        table = PlayerTable(*self.read_raw(stem))
        return table

    def patch(self, stem, payload, backup_dir=None):
        """Write the player regions back into the file — the only edit path (players)."""
        if stem != "Player":
            raise KeyError(f"a PES 5 option file edits players only: {stem}")
        want = PLAYER_BYTES + CREATED_BYTES
        if len(payload) != want:
            raise ValueError(f"player payload is {len(payload):,} bytes, expected {want:,}")
        backup_file(self.path, backup_dir, "Player")
        self.data[BLOCK[4]:BLOCK[4] + PLAYER_BYTES] = payload[:PLAYER_BYTES]
        self.data[BLOCK[3]:BLOCK[3] + CREATED_BYTES] = payload[PLAYER_BYTES:]
        save(self.data, self.path)


# ---------------------------------------------------------------- install discovery
GAME_DIRS = (
    "~/Games/PES 5 Original Season",
    "~/Games/Pro Evolution Soccer 5",
    "~/Games/*",
    "~/.wine/drive_c/Program Files*/Pro Evolution Soccer 5",
    "~/.local/share/wineprefixes/*/drive_c/Program Files*/Pro Evolution Soccer 5",
    "~/.steam/steam/steamapps/common/Pro Evolution Soccer 5",
    "~/.local/share/Steam/steamapps/common/Pro Evolution Soccer 5",
)
LIVE_FILES = (
    "~/Documents/P5OSOF/Pro Evolution Soccer 5/save/folder1/" + OPT_NAME,
    "~/Documents/KONAMI/Pro Evolution Soccer 5/save/folder1/" + OPT_NAME,
)


def is_pes5_dir(path):
    """True for a PES 5 install folder: it carries the game's exe (either release's spelling)."""
    if not path:
        return False
    try:
        names = {n.lower() for n in os.listdir(path)}
    except OSError:
        return False
    return "pes5.exe" in names


def find_game_dir():
    """An installed PES 5 folder (PES5_DIR wins), or "" when there is none."""
    import glob
    env = os.environ.get("PES5_DIR")
    for pattern in ([env] if env else []) + [os.path.expanduser(p) for p in GAME_DIRS]:
        for path in glob.glob(pattern) if ("*" in pattern or "?" in pattern) else [pattern]:
            if is_pes5_dir(path):
                return path
    return ""


def live_option_file():
    """The option file the game reads at boot: the exe redirects into Documents.

    Original Season redirects to `\\P5OSOF\\`; the plain release uses the KONAMI folder.
    Both are checked — only what exists is returned.
    """
    for pattern in LIVE_FILES:
        path = os.path.expanduser(pattern)
        if os.path.isfile(path):
            return path
    return ""


def option_file(folder):
    """The option file a pick resolves to: an install's live file, else the folder's own copy.

    A PES 5 install does not keep its database inside itself (the game writes it under
    Documents), so an install pick must answer with the live file; any other folder answers
    with the option file it holds, wherever the release put it (`misc/Option File/`, the
    `save/folder1/` layout, or loose in the folder).
    """
    folder = os.path.abspath(folder)
    here = [p for p in (os.path.join(folder, OPT_NAME),
                        os.path.join(folder, "save", "folder1", OPT_NAME),
                        os.path.join(folder, "misc", "Option File", OPT_NAME))
            if os.path.isfile(p)]
    if is_pes5_dir(folder) or is_pes5_dir(os.path.dirname(folder)):
        return live_option_file() or (here[0] if here else "")
    return here[0] if here else ""


def database_cpks(folder):
    """The copies a save must write for `folder`: the option file the game reads there."""
    found = option_file(folder)
    return [found] if found else []

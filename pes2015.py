"""PES 2015 database: read and write the player tables inside dt10.cpk / dt80_*.cpk.

Player record (180 bytes, little-endian bit fields counted from bit 0):
    id      u32 at +0
    names   +0x28 Japanese | +0x56 shirt | +0x84 full, 46-byte UTF-8 slots
    primary position   4 bits at 0xcf (0 GK, 1 CB, ... 12 CF)
    abilities  19 six-bit fields, value = raw + 40

The map was recovered from the game's own tables and checked against pesdb.net cards
(Goalkeeping/Saving match exactly; outfield values sit a few points under the online
ratings, which is the shipped data pack vs the live site).

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

    def read_table(self, stem):
        """PlayerTable for a Player*.bin stem, decompressed and eof-checked."""
        found = self.entry(stem)
        if found is None:
            raise KeyError(f"{stem} not in {self.path}")
        _i, (_fn, off, size, _x) = found
        payload = self.read_payload(off, size)
        return PlayerTable(wesys_records(payload), payload[:3])

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
        backup_regions(self.path, [(0x10, self.header.size),
                                   (self.toc_off, self.toc.size),
                                   (p_off, size)], backup_dir, stem)
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

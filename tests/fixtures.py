"""Seeded synthetic CPK fixtures for the three releases — no game data required.

Every fixture is built from the modules' own record maps through their own table classes:
records are filled with deterministic pseudo-random values (random.Random(seed)), wrapped
into a WESYS payload by the release's own writer, and stored in a minimal but faithful CRI
CPK container (header @UTF, `TOC ` block, TOC @UTF, 2 KiB-aligned payloads behind 2048-byte
leads) assembled here. `build_option_file` does the same for the classic era, whose database
is a single KONAMI-WIN32PES5OPT written through `pes5.save()`.

What the fixtures are for:

* round-trip tests that need no game install (load, save with no edits, edit one field,
  batch-edit) and can therefore gate CI;
* the shapes a real install cannot be asked to provide on demand: a CPK with no player
  table, a file whose name claims one release while its bytes are another, a folder pick.

`validate_cpk` re-reads a written container with an independent parser (no `cpk.py` code):
it checks the TOC itself, payload alignment, overlapping regions and every WESYS header.
"""
import os
import random
import struct
import sys
import zlib

# run standalone and from tests/run_all.py alike: the repo root holds the modules
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pes2015
import pes2019
import pes2021
import pes5
import pesdb

LEAD = 0x800                 # 2048: the lead block in front of every payload
TOC_OFF = 0x400              # where the `TOC ` block sits in a fixture
PAYLOAD_OFF = 0x1000         # where the first payload region starts
MAGIC_ENC = b"\x1f\x9e\xf3\xf5"      # CRI's obfuscated-@UTF marker (PES 2015/2019 TOCs)


def xor_stream(buf, m=0x0000655F, t=0x00004115):
    """CRI's UTF obfuscation, implemented here (a second implementation: the app's is
    `cpk.decrypt_utf`). The cipher is its own inverse and restarts at each table."""
    out = bytearray(len(buf))
    for i, byte in enumerate(buf):
        out[i] = byte ^ (m & 0xFF)
        m = (m * t) & 0xFFFFFFFF
    return bytes(out)

MODS = {"pes2015": pes2015, "pes2019": pes2019, "pes2021": pes2021}
RECS = {"pes2015": pes2015.REC, "pes2019": pes2019.REC, "pes2021": pesdb.REC}
# the file each release's tables live in, under an install root
GAME_FILE = {"pes2015": ("Data", "dt10.cpk"),
             "pes2019": ("Data", "dt10_x64.cpk"),
             "pes2021": ("download", "data_s2627.cpk")}
# the WESYS prefix each release's payloads carry
WESYS_PREFIX = {"pes2015": pes2015.PREFIX, "pes2019": pes2019.PREFIX,
                "pes2021": b"\x00\x01\x01"}


# ---------------------------------------------------------------- @UTF writer
def utf_table(columns, rows, name="table"):
    """Build one @UTF table the way `cpk.Utf` reads it.

    columns: (name, storage, type) — storage 0x50 per-row, 0x30 constant; type 0x0A string,
    0x04 u32, 0x06 u64. The three body offsets are relative to base+8, the per-row string
    values to the string table.
    """
    strings = bytearray()

    def intern(text):
        off = len(strings)
        strings.extend(text.encode("utf-8") + b"\x00")
        return off

    name_off = intern(name)
    defs = bytearray()
    for cname, storage, ctype, const in columns:
        defs.append(storage | ctype)
        defs += struct.pack(">I", intern(cname))
        if storage == 0x30:                      # constant: the value travels in the column
            if ctype == 0x0A:
                defs += struct.pack(">I", intern(const))
            elif ctype == 0x04:
                defs += struct.pack(">I", int(const))
            elif ctype in (0x06,):
                defs += struct.pack(">Q", int(const))
            elif ctype != 0x10:                  # 0x10 = a zero column, no payload
                raise ValueError(f"unhandled constant type {ctype:#x}")
    body_rows = bytearray()
    for row in rows:
        for cname, storage, ctype, _const in columns:
            if storage != 0x50:
                continue
            value = row[cname]
            if ctype == 0x0A:
                body_rows += struct.pack(">I", intern(value))
            elif ctype == 0x04:
                body_rows += struct.pack(">I", int(value))
            elif ctype == 0x06:
                body_rows += struct.pack(">Q", int(value))
            else:
                raise ValueError(f"unhandled per-row type {ctype:#x}")
    rows_off = 24 + len(defs)
    row_width = len(body_rows) // len(rows) if rows else 0
    str_off = rows_off + len(body_rows)
    data_off = str_off + len(strings)
    body = (struct.pack(">IIIIHHI", rows_off, str_off, data_off, name_off,
                        len(columns), row_width, len(rows)) + bytes(defs) + bytes(body_rows)
            + bytes(strings))
    return b"@UTF" + struct.pack(">I", 8 + len(body)) + body


def _cpk(files, content_off=PAYLOAD_OFF, encrypt_toc=False):
    """Assemble a CPK from [(FileName, payload bytes, whole_file)] entries.

    whole_file follows the PES 2021/DLC convention: FileOffset is the start of a region that
    begins with a 2048-byte lead block and FileSize covers lead + payload. Otherwise (the
    PES 2015/2019 convention) the payload alone sits at FileOffset + 2048 and FileSize is
    its own length — `read_payload` re-checks `16 + csz == FileSize` on those.

    encrypt_toc stores the TOC the way PES 2015/2019 do: the whole @UTF table obfuscated,
    starting at TocOffset with no `TOC ` magic in front.
    """
    offset = content_off
    entries = []
    total = content_off
    for filename, payload, whole in files:
        span = len(payload) if whole else LEAD + len(payload)
        entries.append((filename, offset, len(payload)))
        total = offset + span
        offset = (total + LEAD - 1) & ~(LEAD - 1)         # 2 KiB between regions
    toc = utf_table(
        [("DirName", 0x30, 0x0A, "common/etc/pesdb/"),
         ("FileName", 0x50, 0x0A, None), ("FileSize", 0x50, 0x04, None),
         ("ExtractSize", 0x50, 0x04, None), ("FileOffset", 0x50, 0x06, None),
         ("ID", 0x50, 0x04, None), ("UserString", 0x30, 0x0A, "")],
        [{"FileName": fn, "FileSize": size, "ExtractSize": size, "FileOffset": off, "ID": 0}
         for fn, off, size in entries],
        name="cpk_toc")
    stored = xor_stream(toc) if encrypt_toc else toc
    header = utf_table(
        [("ContentOffset", 0x50, 0x06, None), ("ContentSize", 0x50, 0x06, None),
         ("TocOffset", 0x50, 0x04, None), ("TocSize", 0x50, 0x04, None)],
        [{"ContentOffset": content_off, "ContentSize": total - content_off,
          "TocOffset": TOC_OFF - 0x10, "TocSize": len(stored)}], name="cpk_header")
    out = bytearray(b"CPK " + b"\xff" * 4 + struct.pack(">Q", TOC_OFF - 0x10))
    out += header
    # the plain form carries a `TOC ` block whose magic sits 16 bytes before the @UTF; the
    # obfuscated form starts the block itself at TocOffset
    out += bytes((TOC_OFF if encrypt_toc else TOC_OFF - 0x10) - len(out))
    if encrypt_toc:
        out += stored
    else:
        out += b"TOC " + b"\xff" + struct.pack(">I", len(toc)) + b"\x00" * 7 + toc
    out += bytes(content_off - len(out))                 # pad up to the payload area
    for _filename, payload, whole in files:
        out += bytes((LEAD - len(out) % LEAD) % LEAD)    # every region starts 2 KiB aligned
        out += payload if whole else bytes(LEAD) + payload
    assert len(out) == total, (len(out), total)
    return bytes(out)


# ---------------------------------------------------------------- record builders
def _fill_table(table, count, rng, mod, vintage):
    """Set every field of every record from the module's own map, in range."""
    for i in range(count):
        for label, (bit, width, add) in mod.FIELD_BY_LABEL.items():
            value = add + rng.randrange(1 << min(width, 10))
            table.set(i, label, value)
    return table


def players(vintage, count=320, seed=7):
    """The Player table's payload for one release, deterministic per seed."""
    mod = MODS[vintage]
    rng = random.Random(seed)
    if vintage == "pes2021":
        raw = bytes(count * pesdb.REC)
        blob = bytes(LEAD) + pesdb.wesys_pack(raw, WESYS_PREFIX[vintage])
        table = pesdb.PlayerTable(blob)
        for i in range(count):
            struct.pack_into("<I", table.records[i], 8, 100000 + i)
        _fill_table(table, count, rng, pesdb, vintage)
        for i in range(count):
            table.set_name(i, full=f"PLAYER {i:03d}", shirt=f"P{i:03d}")
        return table.to_file()
    table = mod.PlayerTable(bytes(count * mod.REC))
    for i in range(count):
        struct.pack_into("<I", table.records[i], mod.ID_OFF, 100000 + i)
    _fill_table(table, count, rng, mod, vintage)
    for i in range(count):
        table.set_name(i, full=f"PLAYER {i:03d}", shirt=f"P{i:03d}",
                       japanese=f"JP{i:03d}")
    return table.to_payload()


def teams(vintage, seed=7, first_club=100, clubs=24, nations=20):
    """A Team table: `nations` rows with ids 1..n, then `clubs` rows with ids 100.."""
    mod = MODS[vintage]
    rng = random.Random(seed + 1)
    count = nations + clubs
    if vintage == "pes2021":
        raw = bytes(count * pesdb.TEAM_REC)
        blob = bytes(LEAD) + pesdb.wesys_pack(raw, WESYS_PREFIX[vintage])
        table = pesdb.TeamTable(blob)
        for i in range(count):
            tid = i + 1 if i < nations else first_club + (i - nations)
            struct.pack_into("<I", table.records[i], 8, tid)
            table.set_slot(i, 368, 70, f"TEAM {tid}")
        rng.randrange(1)                                   # keep the seed honest
        return table.to_file()
    table = mod.TeamTable(bytes(count * mod.TEAM_REC))
    for i in range(count):
        tid = i + 1 if i < nations else first_club + (i - nations)
        fmt = "<H" if vintage == "pes2015" else "<I"
        struct.pack_into(fmt, table.records[i], mod.TEAM_ID, tid)
        table.set_names(i, full=f"TEAM {tid}", short=f"T{tid}")
    rng.randrange(1)
    return table.to_payload()


def assignments(vintage, count=320, seed=7, first_club=100, clubs=24, nations=20):
    """Assignment rows: every third player's *national* row comes first (the club skew)."""
    mod = MODS[vintage]
    rows = []
    for i in range(count):
        pid = 100000 + i
        club = first_club + (i % clubs)
        nation = 1 + (i % nations)
        if i % 3 == 0:
            rows.append((pid, nation, 10 + (i % 20)))
        rows.append((pid, club, 1 + (i % 30)))
    if vintage == "pes2021":
        raw = b"".join(struct.pack("<II", 0, pid) + struct.pack("<I", team)
                       + bytes([max(0, min(98, number - 1)), 0, 0, 0])
                       for pid, team, number in rows)
        blob = bytes(LEAD) + pesdb.wesys_pack(raw, WESYS_PREFIX[vintage])
        return pesdb.AssignmentTable(blob).to_file()
    if vintage == "pes2019":
        raw = b"".join(struct.pack("<II", 0, pid) + struct.pack("<I", team)
                       + bytes([max(0, min(254, number - 1)), 0, 0, 0])
                       for pid, team, number in rows)
        table = pes2019.AssignmentTable(raw)
        return table.to_payload()
    raw = (bytes(pes2015.ASSIGN_LEAD)
           + b"".join(struct.pack("<II", pid, team) + bytes([number & 0x7F, 0, 0, 0])
                      + b"\x00" * 4 for pid, team, number in rows)
           + bytes(pes2015.ASSIGN_TRAIL))
    return pes2015.AssignmentTable(raw).to_payload()


def build_cpk(vintage, path, players_n=320, seed=7):
    """Write one release's fixture CPK (Player + Team + PlayerAssignment) to `path`."""
    whole = vintage == "pes2021"
    files = [("Team.bin", teams(vintage, seed), whole),
             ("PlayerAssignment.bin", assignments(vintage, players_n, seed), whole),
             # Player last: pesdb.table_blob reads a window past the entry, and the largest
             # WESYS block in it must be the player table's
             ("Player.bin", players(vintage, players_n, seed), whole)]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_cpk(files, encrypt_toc=not whole))
    return path


def build_install(root, vintage, players_n=320, seed=7):
    """A folder shaped like one release's install; returns the database CPK's path."""
    folder, name = GAME_FILE[vintage]
    path = os.path.join(root, folder, name)
    return build_cpk(vintage, path, players_n, seed)


def build_stub(root):
    """A CPK with entries but no player table (the `dt00`/`SMK`-stub case)."""
    path = os.path.join(root, "Data", "dt00_4K_x64.cpk")
    files = [("Asset.bin", pes2019.wesys_payload(bytes(64)), False)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_cpk(files))
    return path


def build_mislabeled(root):
    """PES 2015 bytes under the PES 2019 filename (detection must read the bytes)."""
    path = os.path.join(root, "Data", "dt10_x64.cpk")
    return build_cpk("pes2015", path)


# ------------------------------------------------------- PES 5 option file
def build_option_file(path, seed=7, players_n=48, clubs_n=8):
    """A synthetic PES 5 option file: the real layout and crypto, seeded records inside.

    Written through `pes5.save()`, so the raw signature at 0x140C and the ten block
    checksums are the module's own. Players 1..players_n are named and filled from the
    record map; the first clubs_n clubs get a name and a four-player squad each.
    """
    rng = random.Random(seed)
    data = bytearray(pes5.FILESIZE)
    for off, size in zip(pes5.BLOCK, pes5.BLOBSIZE):
        struct.pack_into("<III", data, off - 12, size, 0, 0xD00)   # size, checksum, marker
    blob = bytes((pes5.PLAYERS + pes5.CREATED) * pes5.REC)
    table = pes5.PlayerTable(blob)
    for i in range(1, players_n + 1):
        # the shirt slot's own alphabet is A-Z . _ and space (the whole shipped file uses
        # exactly those), so the fixture's shirt names stay inside it
        table.set_name(i, full=f"PLAYER {i:04d}", shirt=f"P{chr(65 + (i - 1) % 26)}")
        for label in pes5.ABILITIES:
            table.set(i, label, 40 + rng.randrange(60))
        table.set(i, "Position", rng.randrange(len(pes5.POSITION_NAMES)))
        table.set(i, "Nationality", rng.randrange(len(pes5.NATION_NAMES)))
        table.set(i, "Height", 160 + rng.randrange(40))
        table.set(i, "Age", 18 + rng.randrange(20))
        for skill in pes5.SKILLS:
            table.set(i, "Skill: " + skill, rng.randrange(2))
    payload = table.to_payload()
    data[pes5.BLOCK[4]:pes5.BLOCK[4] + pes5.PLAYER_BYTES] = payload[:pes5.PLAYER_BYTES]
    data[pes5.BLOCK[3]:pes5.BLOCK[3] + pes5.CREATED_BYTES] = payload[pes5.PLAYER_BYTES:]
    for c in range(clubs_n):
        name = f"CLUB {c:03d}".encode("utf-8")
        base = pes5.CLUB_BLOCK + c * pes5.CLUB_REC
        data[base:base + len(name)] = name
        for s in range(4):
            pid = 1 + (c * 4 + s) % players_n
            struct.pack_into("<H", data, pes5.SQUAD_BLOCK + 9746 + c * 64 + s * 2, pid)
            data[pes5.SQUAD_BLOCK + 1702 + c * 32 + s] = s        # stored one below 1..4
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    pes5.save(data, path)
    return path


# ---------------------------------------------------------------- independent reader
def read_utf(buf, base):
    """A second, independent @UTF parser — deliberately not `cpk.Utf`.

    Handles the obfuscated form too (PES 2015/2019 TOCs): the marker in the file decrypts
    to '@UTF' at offset 0.
    """
    if buf[base:base + 4] == MAGIC_ENC:
        stored_size = struct.unpack_from(">I", xor_stream(buf[base:base + 8]), 4)[0]
        buf = xor_stream(buf[base:base + stored_size])
        base = 0
    assert buf[base:base + 4] == b"@UTF", f"no @UTF at {base:#x}: {buf[base:base+4]!r}"
    size = struct.unpack_from(">I", buf, base + 4)[0]
    rows_off, str_off, data_off, _name_off, ncols, row_width, nrows = \
        struct.unpack_from(">IIIIHHI", buf, base + 8)
    body = base + 8
    strings, cur = {}, body + str_off
    end = base + size
    while cur < end:
        nul = buf.index(b"\x00", cur)
        strings[cur - body - str_off] = buf[cur:nul].decode("utf-8", "replace")
        cur = nul + 1
    cols, p = [], body + 24
    for _ in range(ncols):
        flag = buf[p]
        name = strings[struct.unpack_from(">I", buf, p + 1)[0]]
        p += 5
        storage, ctype = flag & 0xF0, flag & 0x0F
        const = None
        if storage == 0x30:
            if ctype == 0x0A:
                const = strings[struct.unpack_from(">I", buf, p)[0]]
                p += 4
            elif ctype == 0x04:
                const = struct.unpack_from(">I", buf, p)[0]
                p += 4
            elif ctype == 0x06:
                const = struct.unpack_from(">Q", buf, p)[0]
                p += 8
        cols.append((name, storage, ctype, const))
    rows = []
    p = body + rows_off
    for _ in range(nrows):
        row = {}
        for name, storage, ctype, const in cols:
            if storage == 0x30:
                row[name] = const
            elif storage == 0x10:
                row[name] = None
            elif ctype == 0x0A:
                row[name] = strings[struct.unpack_from(">I", buf, p)[0]]
                p += 4
            elif ctype == 0x04:
                row[name] = struct.unpack_from(">I", buf, p)[0]
                p += 4
            elif ctype == 0x06:
                row[name] = struct.unpack_from(">Q", buf, p)[0]
                p += 8
            else:
                raise AssertionError(f"unhandled column {name}: type {ctype:#x}")
        rows.append((row, row_width))
    return rows


def _entry_payload(buf, row, whole):
    """(stored WESYS payload, region start, region end) for one TOC row.

    whole: the 2021 convention — the entry's FileSize covers the 2048-byte lead block plus
    the payload, and FileOffset is the region start. Otherwise the payload alone sits at
    FileOffset + 2048 and FileSize is its own length.
    """
    start, size = row["FileOffset"], row["FileSize"]
    if whole:
        return buf[start + LEAD:start + size], start, start + size
    return buf[start + LEAD:start + LEAD + size], start, start + LEAD + size


def validate_cpk(path, vintage):
    """Re-read a written fixture container with the independent parser.

    Checks: the header and TOC parse; every file region starts 2 KiB-aligned and stays inside
    the file; no two regions overlap; each payload is a complete WESYS block whose sizes agree
    with the TOC; the player table's rows are the release's record size.
    """
    with open(path, "rb") as f:
        buf = f.read()
    assert buf[:4] == b"CPK ", "not a CPK"
    header = read_utf(buf, 0x10)
    assert len(header) == 1
    hrow, _w = header[0]
    toc_off = 0x10 + hrow["TocOffset"]
    whole = vintage == "pes2021"
    if whole:
        assert buf[toc_off - 0x10:toc_off - 0x0B] == b"TOC \xff", "missing TOC header"
    else:                                    # the older vintages store the TOC obfuscated
        assert buf[toc_off:toc_off + 4] == MAGIC_ENC, \
            f"PES 2015/2019 TOC must be obfuscated: {buf[toc_off:toc_off + 4]!r}"
    toc = read_utf(buf, toc_off)
    regions = []
    for row, _width in toc:
        name = row["FileName"]
        payload, start, end = _entry_payload(buf, row, whole)
        assert start % LEAD == 0, f"{name}: region at {start:#x} is not 2 KiB aligned"
        assert payload and end <= len(buf), f"{name}: region {start:#x}+{end - start} is out of file"
        assert payload[3:8] == b"WESYS", f"{name}: no WESYS at {start:#x}"
        csz, usz = struct.unpack_from("<II", payload, 8)
        # an in-place PES 2021 write keeps the TOC's FileSize as the slot's floor and pads the
        # remainder with zeros, so the stream may end short of the region
        assert 16 + csz <= len(payload), f"{name}: {16 + csz} bytes of stream, region says {len(payload)}"
        assert not any(payload[16 + csz:]), f"{name}: non-zero slack after the WESYS stream"
        d = zlib.decompressobj()
        out = d.decompress(payload[16:])
        assert d.eof and len(out) == usz, f"{name}: short WESYS block"
        if name == "Player.bin":
            assert usz % RECS[vintage] == 0, f"Player.bin: {usz} is not whole records"
        regions.append((start, end, name))
    regions.sort()
    for (a_start, a_end, a_name), (b_start, _b_end, b_name) in zip(regions, regions[1:]):
        assert a_end <= b_start, f"{a_name} and {b_name} overlap: {a_end:#x} > {b_start:#x}"
    return regions


def player_records(path, vintage):
    """(record bytes, prefix) decoded straight from a fixture CPK, no app code."""
    with open(path, "rb") as f:
        buf = f.read()
    header = read_utf(buf, 0x10)
    hrow, _w = header[0]
    toc = read_utf(buf, 0x10 + hrow["TocOffset"])
    whole = vintage == "pes2021"
    for row, _width in toc:
        if row["FileName"] != "Player.bin":
            continue
        payload, _start, _end = _entry_payload(buf, row, whole)
        csz, usz = struct.unpack_from("<II", payload, 8)
        out = zlib.decompress(payload[16:16 + csz])
        assert len(out) == usz
        rec = RECS[vintage]
        return [out[i:i + rec] for i in range(0, len(out), rec)], payload[:3]
    raise AssertionError("no Player.bin in the fixture")


if __name__ == "__main__":                    # a quick self-check: build and re-read all three
    import tempfile
    for _v in ("pes2015", "pes2019", "pes2021"):
        _box = tempfile.mkdtemp(prefix="pesforge-fixture-")
        _path = build_install(_box, _v)
        validate_cpk(_path, _v)
        _rows, _prefix = player_records(_path, _v)
        print(f"{_v}: {_path} ({os.path.getsize(_path):,} bytes, "
              f"{len(_rows)} players, prefix {_prefix!r}) ok")
    _box = tempfile.mkdtemp(prefix="pesforge-fixture-")
    _path = build_option_file(os.path.join(_box, "KONAMI-WIN32PES5OPT"))
    _db = pes5.Cpk(_path)
    print(f"pes5: {_path} ({os.path.getsize(_path):,} bytes, "
          f"{len(_db.read_table('Player'))} player slots, checksums "
          f"{'ok' if not _db.bad_sums else _db.bad_sums}) ok")

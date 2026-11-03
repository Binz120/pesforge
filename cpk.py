"""CRI CPK reader/writer for PES / Football Life archives (header + TOC UTF tables)."""
import struct

MAGIC_ENC = b'\x1f\x9e\xf3\xf5'

TYPE_FMT = {0x0: '>B', 0x1: '>b', 0x2: '>H', 0x3: '>h', 0x4: '>I',
            0x5: '>i', 0x6: '>Q', 0x7: '>q', 0x8: '>f', 0x9: '>d'}
TYPE_SIZE = {0x0: 1, 0x1: 1, 0x2: 2, 0x3: 2, 0x4: 4, 0x5: 4,
             0x6: 8, 0x7: 8, 0x8: 4, 0x9: 8}

ST_PERROW, ST_CONST, ST_ZERO = 0x50, 0x30, 0x10
T_STRING, T_DATA = 0x0A, 0x0B


class Utf:
    def __init__(self, data, base=0):
        self.d = data
        self.base = base
        if data[base:base + 4] != b'@UTF':
            raise ValueError(f'no @UTF at {base:#x}: {data[base:base+4]!r}')
        self.size = struct.unpack_from('>I', data, base + 4)[0]
        (self.rows_off, self.str_off, self.data_off,
         self.name_off, self.ncols, self.row_width,
         self.nrows) = struct.unpack_from('>IIIIHHI', data, base + 8)

        # ALL body offsets are relative to base + 8 (after the magic + size header)
        self.body = body = base + 8
        self.str_base = sbase = body + self.str_off
        self.strings = {}
        end = base + self.size
        cur = sbase
        while cur < end:
            try:
                nul = data.index(b'\x00', cur)
            except ValueError:
                break
            self.strings[cur - sbase] = data[cur:nul].decode('utf-8', 'replace')
            cur = nul + 1

        # columns
        self.cols = []
        p = body + 24
        for _ in range(self.ncols):
            flag = data[p]
            name_off = struct.unpack_from('>I', data, p + 1)[0]
            p += 5
            storage, ctype = flag & 0xF0, flag & 0x0F
            name = self.string(name_off)
            const = None
            if storage == ST_CONST:
                if ctype in (T_STRING,):
                    const = struct.unpack_from('>I', data, p)[0]
                    p += 4
                elif ctype == T_DATA:
                    const = struct.unpack_from('>II', data, p)
                    p += 8
                else:
                    const = struct.unpack_from(TYPE_FMT[ctype], data, p)[0]
                    p += TYPE_SIZE[ctype]
            self.cols.append({'name': name, 'storage': storage, 'type': ctype,
                              'const': const})

    def string(self, off):
        return self.strings.get(off, '')

    def row_cells(self, index):
        """{column: (file offset, type)} for the per-row values of row `index`."""
        p = self.base + 8 + self.rows_off + index * self.row_width
        cells = {}
        for col in self.cols:
            if col["storage"] != ST_PERROW:
                continue
            cells[col["name"]] = (p, col["type"])
            p += 4 if col["type"] == T_STRING else (8 if col["type"] == T_DATA
                                                    else TYPE_SIZE[col["type"]])
        return cells

    def set_row_int(self, index, name, value):
        """Overwrite an unsigned int cell in place (same width, so the layout is untouched)."""
        p, ctype = self.row_cells(index)[name]
        fmt = {0x4: ">I", 0x6: ">Q"}.get(ctype)
        if fmt is None:
            raise ValueError(f"{name} is not an unsigned int column")
        struct.pack_into(fmt, self.d, p, value)

    def rows(self):
        p = self.base + 8 + self.rows_off
        for _ in range(self.nrows):
            row = {}
            for col in self.cols:
                t, st = col['type'], col['storage']
                if st == ST_CONST:
                    v = col['const']
                    row[col['name']] = (self.string(v) if t == T_STRING else v)
                    continue
                if st == ST_ZERO:
                    row[col['name']] = None
                    continue
                if t == T_STRING:
                    off = struct.unpack_from('>I', self.d, p)[0]
                    p += 4
                    row[col['name']] = self.string(off)
                elif t == T_DATA:
                    off, size = struct.unpack_from('>II', self.d, p)
                    p += 8
                    s = self.base + 8 + self.data_off + off
                    row[col['name']] = self.d[s:s + size]
                else:
                    row[col['name']] = struct.unpack_from(TYPE_FMT[t], self.d, p)[0]
                    p += TYPE_SIZE[t]
            yield row


class Cpk:
    """Reads a CPK: header table at 0x10, TOC (TOC 0xFF header + @UTF) located via header."""

    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self.d = bytearray(f.read())
        if self.d[:4] != b'CPK ':
            raise ValueError('not a CPK')
        self.header = Utf(self.d, 0x10)
        # TOC offset is not in the header table; find the "@UTF" that follows a "TOC " magic.
        self.toc_off = self._find_toc()
        if self.toc_off is None:
            raise ValueError('TOC not found')
        self.toc = Utf(self.d, self.toc_off)

    def _find_toc(self):
        pos = 0
        while True:
            i = self.d.find(b'TOC ', pos)
            if i < 0:
                return None
            if self.d[i + 0x10:i + 0x14] == b'@UTF':
                return i + 0x10
            pos = i + 4

    def entries(self):
        for r in self.toc.rows():
            yield r

    def listing(self):
        out = []
        for r in self.entries():
            out.append((r.get('DirName', ''), r.get('FileName', ''),
                        r.get('FileSize') or 0, r.get('ExtractSize') or 0,
                        r.get('FileOffset') or 0))
        return out

    def read_file(self, row):
        off = row['FileOffset']
        size = row['FileSize']
        xsize = row['ExtractSize']
        raw = self.d[off:off + size]
        if size == xsize or raw[:8] != b'CRILAYLA':
            return raw
        return crilayla_decompress(raw)


def crilayla_decompress(data, out_size=None):
    """CRILAYLA: 0x10 header, then reversed-deflate-ish. Reference: CRI layla spec."""
    import zlib  # not used; kept for clarity of fallback
    assert data[:8] == b'CRILAYLA'
    out_size = out_size or struct.unpack_from('<I', data, 8)[0]
    comp_size = struct.unpack_from('<I', data, 12)[0]
    body = data[16:16 + comp_size + 256]
    # reconstruct: original data is stored backwards
    out = bytearray()
    src = body[::-1]
    pos = 0
    while len(out) < out_size:
        flags = src[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= out_size:
                break
            if flags & (1 << bit):
                # back reference
                b1, b2 = src[pos], src[pos + 1]
                pos += 2
                dist = (b1 | ((b2 & 0xF) << 8)) + 1
                length = (b2 >> 4) + 3
                start = len(out) - dist
                for k in range(length):
                    out.append(out[start + k])
            else:
                out.append(src[pos])
                pos += 1
    return bytes(out[:out_size])


if __name__ == '__main__':
    import sys
    c = Cpk(sys.argv[1])
    print(f'{len(c.listing())} entries, toc at {c.toc_off:#x}')
    for d, f, size, xsize, off in c.listing()[:60]:
        print(f'{size:>12,}  {xsize:>12,}  {off:#x}  {d}{f}')

"""Minimal pure-Python reader for Inno Setup installers (no Qt, no external tools, nothing is executed).

Why: the vendor's driver ships inside an Inno Setup .exe (Thermal Master-Setup_x.y.z.exe), which neither
zipfile nor py7zr can open, and we must not *run* the installer just to get at the driver. This parses the
setup headers (format as documented by the innoextract project, zlib licence), lists the packed files and
extracts selected ones.

Supported: Inno Setup 6.x Unicode, embedded data, stored/zlib/LZMA1/LZMA2 chunks, no encryption, no
types/components/tasks. Anything else raises InnoError (callers should turn that into a user message).
"""
import hashlib
import lzma
import mmap
import ntpath
import os
import struct
import zlib

LOADER_MAGIC = b'rDlPtS\xcd\xe6\xd7{\x0b*'
CHUNK_MAGIC = b'zlb\x1a'
DATA_ENTRY_SIZE = 74
N_HEADER_STRINGS = 34
MAX_FILE_SIZE = 512 * 1024 * 1024

STORED, ZLIB, BZIP2, LZMA1, LZMA2 = range(5)


class InnoError(Exception):
    pass


class _Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def take(self, n):
        if n < 0 or self.p + n > len(self.d):
            raise InnoError('truncated or corrupt Inno Setup header')
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self):
        return self.take(1)[0]

    def u32(self):
        return struct.unpack('<I', self.take(4))[0]

    def i32(self):
        return struct.unpack('<i', self.take(4))[0]

    def u64(self):
        return struct.unpack('<Q', self.take(8))[0]

    def blob(self):
        return self.take(self.u32())

    def string(self):
        return self.blob().decode('utf-16-le', errors='replace')


def _lzma_filters(first):
    """first = the stream's property bytes (5 for LZMA1, 1 for LZMA2)."""
    if len(first) == 5:
        p = first[0]
        if p > 9 * 5 * 5:
            raise InnoError('bad LZMA properties')
        dict_size = max(struct.unpack('<I', first[1:])[0], 4096)
        return [{'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': p % 9, 'lp': (p // 9) % 5, 'pb': p // 45}]
    prop = first[0]
    if prop > 40:
        raise InnoError('bad LZMA2 properties')
    dict_size = 0xFFFFFFFF if prop == 40 else (2 | (prop & 1)) << (prop // 2 + 11)
    return [{'id': lzma.FILTER_LZMA2, 'dict_size': min(dict_size, 1 << 30)}]


def _read_block(f, pos):
    """One CRC-protected header block (each 4 KiB piece is preceded by a CRC32). Returns (data, end_pos)."""
    f.seek(pos)
    hdr = f.read(9)
    if len(hdr) != 9:
        raise InnoError('truncated block header')
    crc, size, compressed = struct.unpack('<IIB', hdr)
    if zlib.crc32(hdr[4:]) != crc:
        raise InnoError('block header CRC mismatch')
    raw = f.read(size)
    if len(raw) != size:
        raise InnoError('truncated block')
    out = bytearray()
    for i in range(0, size, 4100):
        c = raw[i:i + 4100]
        if len(c) < 5 or zlib.crc32(c[4:]) != struct.unpack('<I', c[:4])[0]:
            raise InnoError('block CRC mismatch')
        out += c[4:]
    if compressed:
        try:
            out = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=_lzma_filters(bytes(out[:5]))).decompress(bytes(out[5:]))
        except lzma.LZMAError as e:
            raise InnoError(f'LZMA error in header: {e}')
    return bytes(out), pos + 9 + size


class FileEntry:
    def __init__(self, source, dest, location):
        self.source = source
        self.dest = dest            # e.g. '{app}\\drivers\\inf\\P2Pro.inf'
        self.location = location    # index into InnoSetup.data

    def __repr__(self):
        return f'FileEntry({self.dest!r})'


class DataEntry:
    def __init__(self):
        self.chunk_offset = self.chunk_size = self.file_offset = self.file_size = 0
        self.sha1 = b''
        self.method = STORED
        self.x86_filter = False


class InnoSetup:
    def __init__(self, path):
        self.path = path
        self.files = []
        self.data = []
        self.compression = STORED
        with open(path, 'rb') as f:
            self._load(f)

    # -- parsing ---------------------------------------------------------------------------------
    def _load(self, f):
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            i = mm.find(LOADER_MAGIC)
            if i < 0:
                raise InnoError('not an Inno Setup installer (loader table not found)')
            tbl = bytes(mm[i:i + 48])
        if len(tbl) < 48:
            raise InnoError('truncated Inno loader table')
        _revision, _size, _exe_off, _exe_size, _exe_crc, header_off, data_off, crc = struct.unpack('<8I', tbl[12:44])
        if zlib.crc32(tbl[:40]) != crc:
            raise InnoError('Inno loader table CRC mismatch')
        f.seek(header_off)
        text = f.read(64).split(b'\0')[0].decode('latin-1')
        try:
            if not (text.startswith('Inno Setup Setup Data (') and text.endswith('(u)')):
                raise ValueError
            self.version = tuple(int(x) for x in text.split('(')[1].split(')')[0].split('.'))
            if self.version[0] != 6:
                raise ValueError
        except ValueError:
            raise InnoError(f'unsupported Inno Setup version: {text!r}')
        self.version_text = text
        self.data_offset = data_off
        blk1, pos = _read_block(f, header_off + 64)
        blk2, _ = _read_block(f, pos)
        counts = self._parse_block1(blk1)
        self._parse_block2(blk2, counts['data_entries'])
        for fe in self.files:
            if fe.location >= len(self.data):
                raise InnoError('corrupt Inno Setup header (file location index)')

    def _parse_block1(self, b):
        r = _Reader(b)
        for _ in range(N_HEADER_STRINGS):
            r.blob()
        names = ('languages', 'messages', 'permissions', 'types', 'components', 'tasks', 'directories', 'files',
                 'data_entries', 'icons', 'ini', 'registry', 'delete', 'uninstall_delete', 'run', 'uninstall_run')
        c = {n: r.u32() for n in names}
        if any(v > 100000 for v in c.values()):
            raise InnoError('corrupt Inno Setup header (counts)')
        r.take(20)                      # windows version range
        r.take(8)                       # back colors
        r.take(1 + 8)                   # wizard style + resize percent
        r.take(1)                       # image alpha format
        r.take(20 + 8)                  # password sha1 + salt
        r.take(8 + 4)                   # extra disk space, slices per disk
        r.take(3)                       # uninstall log mode, dir exists warning, privileges required
        r.take(1)                       # privileges override
        r.take(2)                       # show language dialog, language detection
        method = r.u8()                 # compression
        if method > LZMA2:
            raise InnoError(f'unsupported compression method {method}')
        self.compression = method
        r.take(2 + 2 + 8)               # architectures, disable dir/group page, uninstall display size
        r.take(6)                       # option flags (48 bits)
        if c['types'] or c['components']:
            raise InnoError('installer uses types/components (not supported)')
        for _ in range(c['languages']):
            for _ in range(10):
                r.blob()
            r.take(4 + 16 + 1)
        for _ in range(c['messages']):
            r.blob(), r.blob(), r.take(4)
        for _ in range(c['permissions']):
            r.blob()
        for _ in range(c['tasks']):
            for _ in range(6):
                r.blob()
            r.take(4 + 1 + 20 + 1)
        for _ in range(c['directories']):
            for _ in range(5):
                r.blob()
            r.take(4 + 20 + 2 + 1)
        for _ in range(c['files']):
            src = r.string()
            dest = r.string()
            for _ in range(8):
                r.blob()
            r.take(20)                  # windows version range
            loc = r.u32()
            r.take(4 + 8 + 2 + 4 + 1)   # attributes, external size, permission, flags, type
            if loc != 0xFFFFFFFF and dest:   # 0xFFFFFFFF: entry without payload (e.g. the uninstaller)
                self.files.append(FileEntry(src, dest, loc))
        return c

    def _parse_block2(self, b, count):
        if len(b) != count * DATA_ENTRY_SIZE:
            raise InnoError('corrupt Inno Setup header (data entry table size)')
        r = _Reader(b)
        for _ in range(count):
            e = DataEntry()
            first_slice, last_slice = r.u32(), r.u32()
            if first_slice or last_slice:
                raise InnoError('multi-slice installers are not supported')
            e.chunk_offset = r.u32()
            e.file_offset = r.u64()
            e.file_size = r.u64()
            e.chunk_size = r.u64()
            e.sha1 = r.take(20)
            r.take(8 + 8)               # timestamp, file version
            flags = struct.unpack('<H', r.take(2))[0]
            if flags & (1 << 6):
                raise InnoError('encrypted installers are not supported')
            e.x86_filter = bool(flags & (1 << 4))
            e.method = self.compression if flags & (1 << 7) else STORED
            self.data.append(e)

    # -- extraction ------------------------------------------------------------------------------
    def extract(self, entries, outdir):
        """Extract the given FileEntry objects below outdir (same relative layout, '{app}\\' etc. stripped).
        Returns {entry: written_path}. Raises InnoError on any corruption (checksums are verified)."""
        outdir = os.path.abspath(outdir)
        plan = {}
        for fe in entries:
            rel = safe_relpath(fe.dest)
            plan.setdefault(self._chunk_key(fe), []).append((fe, rel))
        written = {}
        with open(self.path, 'rb') as f:
            for key, items in plan.items():
                items.sort(key=lambda it: self.data[it[0].location].file_offset)
                stream = _ChunkStream(f, self.data_offset + key[0], key[1], key[2])
                pos = 0
                for fe, rel in items:
                    d = self.data[fe.location]
                    if d.file_offset < pos:      # same payload listed twice
                        stream = _ChunkStream(f, self.data_offset + key[0], key[1], key[2])
                        pos = 0
                    if d.file_size > MAX_FILE_SIZE:
                        raise InnoError('file too large')
                    stream.skip(d.file_offset - pos)
                    content = stream.read(d.file_size)
                    pos = d.file_offset + d.file_size
                    if d.x86_filter:
                        content = _decode_x86(content)
                    if hashlib.sha1(content).digest() != d.sha1:
                        raise InnoError(f'checksum mismatch for {fe.dest}')
                    target = os.path.join(outdir, *rel)
                    if os.path.commonpath([outdir, os.path.abspath(target)]) != outdir:
                        raise InnoError(f'unsafe path in installer: {fe.dest}')
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, 'wb') as out:
                        out.write(content)
                    written[fe] = target
        return written

    def _chunk_key(self, fe):
        d = self.data[fe.location]
        return (d.chunk_offset, d.chunk_size, d.method)


def safe_relpath(dest):
    """'{app}\\drivers\\inf\\P2Pro.inf' -> ['drivers', 'inf', 'P2Pro.inf']; rejects traversal and absolute paths."""
    s = dest
    if s.startswith('{'):
        end = s.find('}')
        if end > 0:
            s = s[end + 1:]
    s = s.replace('/', '\\')
    if ntpath.splitdrive(s)[0] or s.startswith('\\\\'):
        raise InnoError(f'unsafe path in installer: {dest}')
    parts = [p for p in s.split('\\') if p not in ('', '.')]
    if not parts or any(p == '..' or ':' in p or '{' in p for p in parts):
        raise InnoError(f'unsafe path in installer: {dest}')
    return parts


class _ChunkStream:
    """Sequential reader over one (solid) compressed chunk."""
    BLOCK = 1 << 18

    def __init__(self, f, start, size, method):
        self.f = f
        self.method = method
        self.remaining = size
        f.seek(start)
        if f.read(4) != CHUNK_MAGIC:
            raise InnoError('bad chunk magic')
        self.buf = bytearray()
        self.eof = False
        if method == ZLIB:
            self.d = zlib.decompressobj()
        elif method in (LZMA1, LZMA2):
            first = self._raw(5 if method == LZMA1 else 1)
            self.d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=_lzma_filters(first))
        elif method == STORED:
            self.d = None
        else:
            raise InnoError('unsupported compression method')

    def _raw(self, n):
        n = min(n, self.remaining)
        data = self.f.read(n)
        self.remaining -= len(data)
        return data

    def _more(self):
        try:
            if self.d is None:
                out = self._raw(self.BLOCK)
                if not out:
                    self.eof = True
            elif self.method == ZLIB:
                blk = self.d.unconsumed_tail or self._raw(self.BLOCK)
                if not blk:
                    self.eof = True
                    return
                out = self.d.decompress(blk, 1 << 20)
            else:
                if self.d.eof:
                    self.eof = True
                    return
                if self.d.needs_input:
                    blk = self._raw(self.BLOCK)
                    if not blk:
                        self.eof = True
                        return
                    out = self.d.decompress(blk, 1 << 20)
                else:
                    out = self.d.decompress(b'', 1 << 20)
        except (lzma.LZMAError, zlib.error) as e:
            raise InnoError(f'decompression error: {e}')
        self.buf += out

    def read(self, n):
        while len(self.buf) < n and not self.eof:
            self._more()
        if len(self.buf) < n:
            raise InnoError('unexpected end of compressed data')
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def skip(self, n):
        while n > 0:
            if not self.buf and not self.eof:
                self._more()
            if not self.buf:
                raise InnoError('unexpected end of compressed data')
            k = min(n, len(self.buf))
            del self.buf[:k]
            n -= k


def _decode_x86(data):
    """Inno Setup 5.3.9+ 'call instruction optimized' decoder: undo the CALL/JMP relative->absolute rewrite."""
    out = bytearray(data)
    n = len(out)
    block = 0x10000
    i = 0
    while i < n:
        j1 = out.find(b'\xe8', i)
        j2 = out.find(b'\xe9', i)
        cands = [j for j in (j1, j2) if j >= 0]
        if not cands:
            break
        j = min(cands)
        # j = opcode position; Inno's 'offset' (bytes consumed so far) is j + 1 after reading the opcode
        if block - (j % block) < 5 or j + 5 > n:
            i = j + 1
            continue
        if out[j + 4] in (0, 0xff):
            addr = (j + 5) & 0xffffff
            rel = (out[j + 1] | out[j + 2] << 8 | out[j + 3] << 16)
            rel = (rel - addr) & 0xffffffff
            out[j + 1] = rel & 0xff
            out[j + 2] = (rel >> 8) & 0xff
            out[j + 3] = (rel >> 16) & 0xff
            if rel & 0x800000:
                out[j + 4] = (~out[j + 4]) & 0xff
        i = j + 5
    return bytes(out)

#!/usr/bin/env python3
"""Convert a CodeBreaker (.cbs) PS2 save into a .psu that PCSX2 can import.

Usage:
    python3 cbs2psu.py input.cbs [output.psu]

CBS format (per ps2dev/mymc's ps2save.py):
    0x00  'CFU\0'
    0x04  u32  unknown
    0x08  u32  header length (payload starts here)
    0x0C  u32  decompressed size
    0x10  u32  compressed size
    0x14  char[32]  directory name, e.g. BASLUS-21351-Melz
    ...   timestamps, mode, then the display title
    hlen  payload: RC4 (fixed S-box, no key schedule) then zlib

The decompressed payload is a flat sequence of 64-byte entry headers, each
followed immediately by that file's data.

PSU is the format PCSX2's memory card editor imports: 512-byte entries for
the directory, '.', '..', then one per file, with each file's data padded
out to a 1024-byte boundary.
"""

import struct
import sys
import zlib
from pathlib import Path

CBS_MAGIC = b"CFU\0"

# Initial RC4 permutation state used by CodeBreaker. Note this replaces the
# usual key schedule outright -- the S-box below IS the key.
CBS_RC4S = [
    0x5F, 0x1F, 0x85, 0x6F, 0x31, 0xAA, 0x3B, 0x18, 0x21, 0xB9, 0xCE, 0x1C,
    0x07, 0x4C, 0x9C, 0xB4, 0x81, 0xB8, 0xEF, 0x98, 0x59, 0xAE, 0xF9, 0x26,
    0xE3, 0x80, 0xA3, 0x29, 0x2D, 0x73, 0x51, 0x62, 0x7C, 0x64, 0x46, 0xF4,
    0x34, 0x1A, 0xF6, 0xE1, 0xBA, 0x3A, 0x0D, 0x82, 0x79, 0x0A, 0x5C, 0x16,
    0x71, 0x49, 0x8E, 0xAC, 0x8C, 0x9F, 0x35, 0x19, 0x45, 0x94, 0x3F, 0x56,
    0x0C, 0x91, 0x00, 0x0B, 0xD7, 0xB0, 0xDD, 0x39, 0x66, 0xA1, 0x76, 0x52,
    0x13, 0x57, 0xF3, 0xBB, 0x4E, 0xE5, 0xDC, 0xF0, 0x65, 0x84, 0xB2, 0xD6,
    0xDF, 0x15, 0x3C, 0x63, 0x1D, 0x89, 0x14, 0xBD, 0xD2, 0x36, 0xFE, 0xB1,
    0xCA, 0x8B, 0xA4, 0xC6, 0x9E, 0x67, 0x47, 0x37, 0x42, 0x6D, 0x6A, 0x03,
    0x92, 0x70, 0x05, 0x7D, 0x96, 0x2F, 0x40, 0x90, 0xC4, 0xF1, 0x3E, 0x3D,
    0x01, 0xF7, 0x68, 0x1E, 0xC3, 0xFC, 0x72, 0xB5, 0x54, 0xCF, 0xE7, 0x41,
    0xE4, 0x4D, 0x83, 0x55, 0x12, 0x22, 0x09, 0x78, 0xFA, 0xDE, 0xA7, 0x06,
    0x08, 0x23, 0xBF, 0x0F, 0xCC, 0xC1, 0x97, 0x61, 0xC5, 0x4A, 0xE6, 0xA0,
    0x11, 0xC2, 0xEA, 0x74, 0x02, 0x87, 0xD5, 0xD1, 0x9D, 0xB7, 0x7E, 0x38,
    0x60, 0x53, 0x95, 0x8D, 0x25, 0x77, 0x10, 0x5E, 0x9B, 0x7F, 0xD8, 0x6E,
    0xDA, 0xA2, 0x2E, 0x20, 0x4F, 0xCD, 0x8F, 0xCB, 0xBE, 0x5A, 0xE0, 0xED,
    0x2C, 0x9A, 0xD4, 0xE2, 0xAF, 0xD0, 0xA9, 0xE8, 0xAD, 0x7A, 0xBC, 0xA8,
    0xF2, 0xEE, 0xEB, 0xF5, 0xA6, 0x99, 0x28, 0x24, 0x6C, 0x2B, 0x75, 0x5D,
    0xF8, 0xD3, 0x86, 0x17, 0xFB, 0xC0, 0x7B, 0xB3, 0x58, 0xDB, 0xC7, 0x4B,
    0xFF, 0x04, 0x50, 0xE9, 0x88, 0x69, 0xC9, 0x2A, 0xAB, 0xFD, 0x5B, 0x1B,
    0x8A, 0xD9, 0xEC, 0x27, 0x44, 0x0E, 0x33, 0xC8, 0x6B, 0x93, 0x32, 0x48,
    0xB6, 0x30, 0x43, 0xA5,
]

DIR_MODE = 0x8427
FILE_MODE = 0x8497


def rc4_crypt(sbox, data):
    """RC4 with a pre-seeded permutation state (no key schedule)."""
    s = list(sbox)
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def read_cbs(path):
    """Return (dirname, title, created, modified, [(entry_header, data), ...])."""
    data = Path(path).read_bytes()
    if data[:4] != CBS_MAGIC:
        raise ValueError(f"{path}: not a CodeBreaker save (bad magic)")

    _, hlen = struct.unpack("<LL", data[4:12])
    if hlen < 92 + 32:
        raise ValueError(f"{path}: header too short ({hlen})")

    dlen, flen = struct.unpack("<LL", data[12:20])
    dirname = data[20:52].split(b"\0")[0].decode("ascii", "replace")
    created = data[52:60]
    modified = data[60:68]
    title = data[92:hlen].split(b"\0")[0].decode("ascii", "replace")

    # flen is sometimes the whole file's length and sometimes just the body's,
    # so trust whichever actually matches what's left after the header.
    body = data[hlen:hlen + flen]
    if len(body) != flen:
        body = data[hlen:]

    body = rc4_crypt(CBS_RC4S, body)
    body = zlib.decompressobj().decompress(body, dlen)
    if len(body) != dlen:
        raise ValueError(f"{path}: got {len(body)} bytes, header claims {dlen}")

    files = []
    off = 0
    while off < len(body):
        if len(body) - off < 64:
            raise ValueError(f"{path}: truncated entry header at {off}")
        header = struct.unpack("<8s8sLHHLL32s", body[off:off + 64])
        size = header[2]
        chunk = body[off + 64:off + 64 + size]
        if len(chunk) != size:
            raise ValueError(f"{path}: truncated file data at {off}")
        files.append((header, chunk))
        off += 64 + size

    return dirname, title, created, modified, files


def write_psu(out_path, dirname, created, modified, files):
    def entry(mode, size, created_ts, modified_ts, name):
        buf = bytearray(512)
        struct.pack_into("<H", buf, 0, mode)
        struct.pack_into("<L", buf, 4, size)
        buf[8:16] = created_ts
        buf[24:32] = modified_ts
        raw = name.encode("ascii", "replace")[:31]
        buf[64:64 + len(raw)] = raw
        return bytes(buf)

    psu = bytearray()
    # The directory entry's "size" counts its children, including . and ..
    psu += entry(DIR_MODE, len(files) + 2, created, modified, dirname)
    psu += entry(DIR_MODE, 0, created, modified, ".")
    psu += entry(DIR_MODE, 0, created, modified, "..")

    for header, chunk in files:
        f_created, f_modified, size, mode, _, _, _, name = header
        nm = name.split(b"\0")[0].decode("ascii", "replace")
        psu += entry(mode or FILE_MODE, size, f_created, f_modified, nm)
        psu += chunk
        psu += b"\0" * ((-len(chunk)) % 1024)

    Path(out_path).write_bytes(psu)
    return len(psu)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".psu")

    dirname, title, created, modified, files = read_cbs(src)
    print(f"{src.name}")
    print(f"  directory : {dirname}")
    print(f"  title     : {title}")
    print(f"  files     : {len(files)}")
    for header, chunk in files:
        nm = header[7].split(b"\0")[0].decode("ascii", "replace")
        print(f"      {nm:<24} {len(chunk):>8} bytes")

    total = write_psu(dst, dirname, created, modified, files)
    print(f"  -> {dst}  ({total} bytes)")


if __name__ == "__main__":
    main()

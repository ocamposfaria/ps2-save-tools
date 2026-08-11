#!/usr/bin/env python3
"""Insert a .psu save into an existing PS2 memory card image (.ps2).

Usage:
    python3 psu2mc.py card.ps2 save.psu

Writes into the card in place, so back it up first. Existing saves are left
alone; the new save is added alongside them as another root directory.

Card layout (8MB, with ECC):
    Each physical page is 512 bytes of data + 16 bytes spare, and the first
    12 spare bytes are four 3-byte Hamming codes, one per 128-byte chunk.
    A cluster is 2 pages = 1024 bytes. Cluster N of the data area lives at
    page (alloc_offset + N) * 2.

    The FAT is indirect: superblock ifc_list -> indirect FAT clusters ->
    FAT clusters -> one u32 entry per data cluster. An entry has bit 31 set
    when allocated; the low 31 bits are the next cluster in the chain, or
    0x7FFFFFFF to end it.

Directory entries are 512 bytes each, two per cluster. A directory's own
length counts its children including '.' and '..'.
"""

import struct
import sys
from pathlib import Path

PAGE = 512
SPARE = 16
RAW_PAGE = PAGE + SPARE
PAGES_PER_CLUSTER = 2
CLUSTER = PAGE * PAGES_PER_CLUSTER

FAT_ALLOCATED = 0x80000000
FAT_CHAIN_END = 0x7FFFFFFF
FAT_MASK = 0x7FFFFFFF

DF_READ = 0x0001
DF_WRITE = 0x0002
DF_EXECUTE = 0x0004
DF_EXISTS = 0x8000
DF_FILE = 0x0010
DF_DIR = 0x0020

# Copied from what the PS2 itself writes, verified against a working save on a
# real card: 0x8427 for directories (including '.' and '..'), 0x8497 for files.
MODE_DIR = 0x8427
MODE_FILE = 0x8497


# ----------------------------------------------------------------- ECC
def _parityb(a):
    a ^= a >> 1
    a ^= a >> 2
    a ^= a >> 4
    return a & 1


_PARITY = [_parityb(b) for b in range(256)]
_CPMASK = []
for _b in range(256):
    _m = 0
    for _i, _mask in enumerate([0x55, 0x33, 0x0F, 0x00, 0xAA, 0xCC, 0xF0]):
        _m |= _PARITY[_b & _mask] << _i
    _CPMASK.append(_m)


def ecc_calculate(chunk):
    """Hamming code for one 128-byte chunk."""
    column_parity = 0x77
    line_parity_0 = 0x7F
    line_parity_1 = 0x7F
    for i, b in enumerate(chunk):
        column_parity ^= _CPMASK[b]
        if _PARITY[b]:
            line_parity_0 ^= ~i & 0xFF
            line_parity_1 ^= i
    return bytes([column_parity & 0xFF, line_parity_0 & 0x7F, line_parity_1 & 0xFF])


def page_spare(page):
    """Build the 16-byte spare area for a 512-byte page."""
    ecc = b"".join(ecc_calculate(page[i * 128:(i + 1) * 128]) for i in range(4))
    return ecc + b"\0" * 4


# ----------------------------------------------------------------- card
class MemoryCard:
    def __init__(self, path):
        self.path = Path(path)
        self.data = bytearray(self.path.read_bytes())
        if self.data[:28] != b"Sony PS2 Memory Card Format ":
            raise ValueError(f"{path}: not a PS2 memory card image")

        (page_len, pages_per_cluster, _pages_per_block) = struct.unpack(
            "<HHH", self.data[0x28:0x2E])
        if page_len != PAGE or pages_per_cluster != PAGES_PER_CLUSTER:
            raise ValueError(f"{path}: unexpected geometry {page_len}/{pages_per_cluster}")

        self.clusters_per_card, = struct.unpack("<I", self.data[0x30:0x34])
        self.alloc_offset, self.alloc_end, self.rootdir = struct.unpack(
            "<III", self.data[0x34:0x40])
        self.ifc_list = list(struct.unpack("<32i", self.data[0x50:0xD0]))

        if len(self.data) < self.clusters_per_card * PAGES_PER_CLUSTER * RAW_PAGE:
            raise ValueError(f"{path}: image is shorter than its superblock claims")

    # -- raw page/cluster access ---------------------------------------
    def read_page(self, n):
        off = n * RAW_PAGE
        return bytes(self.data[off:off + PAGE])

    def write_page(self, n, page):
        if len(page) != PAGE:
            raise ValueError("page must be 512 bytes")
        off = n * RAW_PAGE
        self.data[off:off + PAGE] = page
        self.data[off + PAGE:off + RAW_PAGE] = page_spare(page)

    def cluster_page(self, cluster):
        return (self.alloc_offset + cluster) * PAGES_PER_CLUSTER

    def read_cluster(self, cluster):
        base = self.cluster_page(cluster)
        return self.read_page(base) + self.read_page(base + 1)

    def write_cluster(self, cluster, data):
        if len(data) != CLUSTER:
            raise ValueError("cluster must be 1024 bytes")
        base = self.cluster_page(cluster)
        self.write_page(base, data[:PAGE])
        self.write_page(base + 1, data[PAGE:])

    # FAT and indirect-FAT clusters are addressed from the start of the card,
    # not offset by alloc_offset the way data clusters are.
    def read_phys_cluster(self, cluster):
        base = cluster * PAGES_PER_CLUSTER
        return self.read_page(base) + self.read_page(base + 1)

    def write_phys_cluster(self, cluster, data):
        if len(data) != CLUSTER:
            raise ValueError("cluster must be 1024 bytes")
        base = cluster * PAGES_PER_CLUSTER
        self.write_page(base, data[:PAGE])
        self.write_page(base + 1, data[PAGE:])

    # -- FAT ------------------------------------------------------------
    def _fat_slot(self, cluster):
        """Locate the (fat_cluster, entry index) holding a data cluster's FAT entry."""
        entries_per_cluster = CLUSTER // 4
        fat_index = cluster // entries_per_cluster
        fat_offset = cluster % entries_per_cluster

        indirect_index = fat_index // entries_per_cluster
        indirect_offset = fat_index % entries_per_cluster

        ifc = self.ifc_list[indirect_index]
        if ifc < 0:
            raise ValueError(f"cluster {cluster}: no indirect FAT cluster")

        indirect = self.read_phys_cluster(ifc)
        fat_cluster, = struct.unpack_from("<I", indirect, indirect_offset * 4)
        return fat_cluster, fat_offset

    def get_fat(self, cluster):
        fat_cluster, offset = self._fat_slot(cluster)
        buf = self.read_phys_cluster(fat_cluster)
        value, = struct.unpack_from("<I", buf, offset * 4)
        return value

    def set_fat(self, cluster, value):
        fat_cluster, offset = self._fat_slot(cluster)
        buf = bytearray(self.read_phys_cluster(fat_cluster))
        struct.pack_into("<I", buf, offset * 4, value)
        self.write_phys_cluster(fat_cluster, bytes(buf))

    def free_clusters(self):
        total = self.alloc_end
        return [c for c in range(total) if not (self.get_fat(c) & FAT_ALLOCATED)]

    def allocate_chain(self, count, pool):
        """Claim `count` clusters from `pool` and link them into a chain."""
        if len(pool) < count:
            raise ValueError(f"card full: need {count} clusters, {len(pool)} free")
        chain = [pool.pop(0) for _ in range(count)]
        for i, c in enumerate(chain):
            nxt = chain[i + 1] if i + 1 < len(chain) else FAT_CHAIN_END
            self.set_fat(c, FAT_ALLOCATED | (nxt & FAT_MASK))
        return chain

    # -- directory entries ----------------------------------------------
    # Entries are 512 bytes, so two per cluster. A directory's clusters are
    # not necessarily contiguous -- always walk its FAT chain.
    def chain_of(self, first_cluster):
        chain = [first_cluster]
        c = first_cluster
        while True:
            nxt = self.get_fat(c) & FAT_MASK
            if nxt == FAT_CHAIN_END:
                return chain
            chain.append(nxt)
            c = nxt

    def read_dirent(self, chain, index):
        cluster = chain[index // 2]
        return self.read_page(self.cluster_page(cluster) + (index % 2))

    def write_dirent_at(self, chain, index, entry):
        cluster = chain[index // 2]
        self.write_page(self.cluster_page(cluster) + (index % 2), entry)

    def dir_entries(self, first_cluster, length):
        """Read `length` entries. The count lives in the parent's entry, not
        in this directory's own '.', so callers must supply it."""
        chain = self.chain_of(first_cluster)
        return [self.read_dirent(chain, i) for i in range(length)]

    def root_entries(self):
        # The root has no parent, so its '.' does carry the real length.
        chain = self.chain_of(self.rootdir)
        length, = struct.unpack("<I", self.read_dirent(chain, 0)[4:8])
        return [self.read_dirent(chain, i) for i in range(length)]

    def save(self, out_path=None):
        Path(out_path or self.path).write_bytes(bytes(self.data))


def make_dirent(mode, length, cluster, name, created=b"\0" * 8, modified=b"\0" * 8,
                dir_entry=0):
    """Build a 512-byte directory entry.

    Layout is mymc's "<HHL8sLL8sL28x448s": mode, unused, length, created,
    cluster, dir_entry, modified, attr, name.

    Note the '.' entry inside a directory does NOT repeat the parent's values:
    it carries length=0, cluster=0, and puts its own index within the parent
    in dir_entry. Only the entry in the parent holds the real length/cluster.
    """
    e = bytearray(512)
    struct.pack_into("<H", e, 0x00, mode)
    struct.pack_into("<I", e, 0x04, length)
    e[0x08:0x10] = created
    struct.pack_into("<I", e, 0x10, cluster)
    struct.pack_into("<I", e, 0x14, dir_entry)
    e[0x18:0x20] = modified
    raw = name.encode("ascii", "replace")[:31]
    e[0x40:0x40 + len(raw)] = raw
    return bytes(e)


def parse_psu(path):
    """Return (dirname, created, modified, [(name, created, modified, data)])."""
    d = Path(path).read_bytes()
    off = 0

    def take_entry():
        nonlocal off
        e = d[off:off + 512]
        off += 512
        mode, = struct.unpack("<H", e[0:2])
        size, = struct.unpack("<I", e[4:8])
        created = e[8:16]
        modified = e[24:32]
        name = e[64:96].split(b"\0")[0].decode("ascii", "replace")
        return mode, size, created, modified, name

    mode, count, created, modified, dirname = take_entry()
    if not (mode & DF_DIR):
        raise ValueError(f"{path}: first entry is not a directory")
    take_entry()  # .
    take_entry()  # ..

    files = []
    for _ in range(count - 2):
        fmode, size, fcreated, fmodified, name = take_entry()
        data = d[off:off + size]
        off += (size + 1023) // 1024 * 1024
        files.append((name, fcreated, fmodified, data))
    return dirname, created, modified, files


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__.strip())

    card_path, psu_path = sys.argv[1], sys.argv[2]
    dirname, created, modified, files = parse_psu(psu_path)

    mc = MemoryCard(card_path)
    print(f"card: {card_path}")
    print(f"  {mc.alloc_end} clusters, rootdir at {mc.rootdir}")

    existing = mc.root_entries()
    names = [e[0x40:0x60].split(b"\0")[0].decode("ascii", "replace") for e in existing]
    print(f"  existing: {[n for n in names if n not in ('.', '..')]}")
    if dirname in names:
        sys.exit(f"  '{dirname}' is already on this card -- refusing to overwrite")

    pool = mc.free_clusters()
    print(f"  free clusters: {len(pool)}")

    # The save's directory holds '.', '..' and one entry per file, 2 per cluster.
    n_entries = len(files) + 2
    dir_clusters_needed = (n_entries + 1) // 2
    dir_chain = mc.allocate_chain(dir_clusters_needed, pool)

    print(f"  writing '{dirname}' ({len(files)} files)")

    # Index this save will occupy in the root directory -- '.' has to record it.
    root_index = len(existing)

    # '.' and '..' come first, then the files. '.' points back at the parent
    # slot rather than repeating this directory's own length/cluster.
    entries = [
        make_dirent(MODE_DIR, 0, 0, ".", created, modified, dir_entry=root_index),
        make_dirent(MODE_DIR, 0, 0, "..", created, modified),
    ]
    for name, fcreated, fmodified, data in files:
        n_clusters = max(1, (len(data) + CLUSTER - 1) // CLUSTER)
        chain = mc.allocate_chain(n_clusters, pool)
        for i, c in enumerate(chain):
            chunk = data[i * CLUSTER:(i + 1) * CLUSTER]
            mc.write_cluster(c, chunk.ljust(CLUSTER, b"\0"))
        entries.append(
            make_dirent(MODE_FILE, len(data), chain[0], name, fcreated, fmodified))
        print(f"      {name:<24} {len(data):>8} bytes -> cluster {chain[0]}")

    for i, e in enumerate(entries):
        mc.write_dirent_at(dir_chain, i, e)

    # Append the save to the root directory and grow its length.
    root_len = len(existing)
    root_cluster_index = root_len // 2
    root_chain = mc.chain_of(mc.rootdir)
    if root_cluster_index >= len(root_chain):
        extra = mc.allocate_chain(1, pool)[0]
        mc.set_fat(root_chain[-1], FAT_ALLOCATED | extra)
        mc.set_fat(extra, FAT_ALLOCATED | FAT_CHAIN_END)
        root_chain.append(extra)

    mc.write_dirent_at(
        root_chain, root_len,
        make_dirent(MODE_DIR, n_entries, dir_chain[0], dirname, created, modified))

    root_self = bytearray(mc.read_dirent(root_chain, 0))
    struct.pack_into("<I", root_self, 4, root_len + 1)
    mc.write_dirent_at(root_chain, 0, bytes(root_self))

    mc.save()
    print(f"  done -- root now has {root_len + 1} entries")


if __name__ == "__main__":
    main()

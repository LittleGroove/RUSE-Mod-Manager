"""
Edata (.dat) container reader/writer for RUSE game archives.

A .dat file is Eugen's proprietary virtual filesystem ('edata') — conceptually
similar to a ZIP without compression.  It holds a flat list of files (with
virtual directory paths) packed sequentially in a data region.

RUSE uses two edata versions:
  v1  — older format (4-byte offsets/sizes, 1 padding byte before name)
  v2  — newer format (8-byte offsets/sizes, 16-byte MD5 checksum per file)

This module detects the version automatically and handles both.

Public API
----------
  open_dat(path)             → EdataFile  (read the archive from disk)
  EdataFile.list()           → list of virtual paths inside the archive
  EdataFile.get(path)        → bytes for one internal file
  EdataFile.replace(path, data)  → update one file and repack on disk
  EdataFile.extract_all(dir) → dump all files to a directory
"""

import hashlib
import os
import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EDATA_MAGIC = b"edat"

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class EdataEntry:
    """Metadata for one file inside an edata archive."""
    path: str          # virtual path inside the archive (e.g. "pc/ndf/patchable/gfx/everything.ndfbin")
    offset: int        # byte offset inside the data region
    size: int          # file size in bytes
    checksum: bytes    # MD5 (v2) or empty (v1)
    # position of the offset/size fields in the archive for in-place update
    _offset_field_pos: int = 0
    _size_field_pos: int = 0


@dataclass
class EdataHeader:
    version: int       # 1 or 2
    dict_offset: int   # byte offset of the file dictionary
    dict_length: int   # byte length of the file dictionary
    file_offset: int   # byte offset where file data begins
    file_length: int   # byte length of the file data region (v1) / 0 (v2 uses total)
    sector_size: int   # v2 only; 0 for v1


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _read_null_str(data: bytes, pos: int) -> Tuple[str, int]:
    """Read a null-terminated string from bytes.  Returns (string, next_pos)."""
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


# ── Parser ────────────────────────────────────────────────────────────────────

def _parse_header_v1(data: bytes) -> EdataHeader:
    """
    Both v1 and v2 share the same EdataHeader struct (from EdataHeader.cs):
      0x00  magic "edat"         (4 bytes)
      0x04  version              (4 bytes) == 1
      0x08  Checksum_V1          (16 bytes) — MD5 of dictionary
      0x18  Skip_1               (1 byte)  — padding
      0x19  DictOffset           (4 bytes) — always 0x40D (1037) for v1
      0x1D  DictLength           (4 bytes)
      0x21  FileOffset           (4 bytes) — where file data begins
      0x25  FileLength           (4 bytes) — total bytes of file data
      0x29  Unknown_1            (4 bytes) — always 0
      0x2D  Padding/SectorSize   (4 bytes) — 8192 for v2; 0 for v1
    """
    if len(data) < 0x29:
        raise ValueError("File too short for edata v1 header")
    dict_offset = struct.unpack_from("<I", data, 0x19)[0]
    dict_length = struct.unpack_from("<I", data, 0x1D)[0]
    file_offset = struct.unpack_from("<I", data, 0x21)[0]
    file_length = struct.unpack_from("<I", data, 0x25)[0]
    return EdataHeader(version=1, dict_offset=dict_offset, dict_length=dict_length,
                       file_offset=file_offset, file_length=file_length, sector_size=0)


def _parse_header_v2(data: bytes) -> EdataHeader:
    """
    v2 header (1024 bytes total):
      0x00  "edat" (4)
      0x04  version = 2  (uint32)
      0x08  padding (17 bytes)
      0x19  files_dict_offset  uint32
      0x1D  files_dict_size    uint32
      0x21  files_data_offset  uint32
      0x25  files_data_size    uint32
      0x29  padding (4)
      0x2D  sectorSize         uint32
      0x31  dict_checksum (16 bytes)
      0x41  padding (972 bytes)   → total = 0x400 = 1024
    """
    if len(data) < 0x41:
        raise ValueError("File too short for edata v2 header")
    version = struct.unpack_from("<I", data, 4)[0]
    dict_offset = struct.unpack_from("<I", data, 0x19)[0]
    dict_size   = struct.unpack_from("<I", data, 0x1D)[0]
    data_offset = struct.unpack_from("<I", data, 0x21)[0]
    data_size   = struct.unpack_from("<I", data, 0x25)[0]
    sector_size = struct.unpack_from("<I", data, 0x2D)[0]
    return EdataHeader(version=2, dict_offset=dict_offset, dict_length=dict_size,
                       file_offset=data_offset, file_length=data_size, sector_size=sector_size)


def _parse_dict_v2(data: bytes, hdr: EdataHeader) -> List[EdataEntry]:
    """
    v2 dictionary entry format (from C# ReadEdatV2Dictionary):
      4   fileGroupId  — 0 = file entry; >0 = directory entry
      if fileGroupId == 0 (file):
        4   fileEntrySize
        8   offset (int64)
        8   size   (int64)
        16  checksum (MD5)
        N+1 name (null-terminated)
        [1 skip if name.length % 2 == 0]
      if fileGroupId > 0 (directory):
        4   fileEntrySize (0 = inherit parent)
        N+1 name
        [1 skip if name.length % 2 == 0]
    """
    pos = hdr.dict_offset
    end = min(hdr.dict_offset + hdr.dict_length, len(data))   # never walk past the actual buffer
    entries = []
    dir_stack: List[str] = []
    ending_stack: List[int] = []

    while pos < end:
        fg = struct.unpack_from("<i", data, pos)[0]
        pos += 4

        if fg == 0:  # file entry
            fe_size = struct.unpack_from("<i", data, pos)[0]; pos += 4
            offset  = struct.unpack_from("<q", data, pos)[0]; pos += 8
            size    = struct.unpack_from("<q", data, pos)[0]; pos += 8
            checksum = data[pos:pos+16]; pos += 16
            name, pos = _read_null_str(data, pos)
            if len(name) % 2 == 0:
                pos += 1  # skip padding byte

            full_path = "".join(dir_stack) + name

            entry = EdataEntry(path=full_path, offset=offset, size=size, checksum=checksum)
            entries.append(entry)

            # Pop directories whose end we've reached
            while ending_stack and pos == ending_stack[-1]:
                dir_stack.pop()
                ending_stack.pop()

        elif fg > 0:  # directory entry
            fe_size = struct.unpack_from("<i", data, pos)[0]; pos += 4
            name, pos = _read_null_str(data, pos)
            if len(name) % 2 == 0:
                pos += 1

            if fe_size != 0:
                ending_stack.append(fe_size + pos - 8)
            elif ending_stack:
                ending_stack.append(ending_stack[-1])

            dir_stack.append(name)

    return entries


def _parse_dict_v1(data: bytes, hdr: EdataHeader) -> List[EdataEntry]:
    """
    v1 dictionary (from C# ReadEdatV1Dictionary):
      4   fileGroupId — 0 = file; >0 = directory
      if file:
        4   fileEntrySize
        4   offset (uint32)
        4   size   (uint32)
        1   skip byte (instead of checksum)
        N+1 name (null-terminated)
        [1 skip if (name.length+1) % 2 == 0]
      if directory:
        4   fileEntrySize
        N+1 name
        [1 skip if (name.length+1) % 2 == 1]
    """
    pos = hdr.dict_offset
    end = min(hdr.dict_offset + hdr.dict_length, len(data))   # never walk past the actual buffer
    entries = []
    dir_stack: List[str] = []
    ending_stack: List[int] = []

    while pos < end:
        fg = struct.unpack_from("<i", data, pos)[0]
        pos += 4

        if fg == 0:
            fe_size = struct.unpack_from("<i", data, pos)[0]; pos += 4
            offset  = struct.unpack_from("<I", data, pos)[0]; pos += 4
            size    = struct.unpack_from("<I", data, pos)[0]; pos += 4
            pos += 1  # skip 1 byte (no checksum in v1)
            name, pos = _read_null_str(data, pos)
            if (len(name) + 1) % 2 == 0:
                pos += 1

            full_path = "".join(dir_stack) + name
            entry = EdataEntry(path=full_path, offset=offset, size=size, checksum=b"")
            entries.append(entry)

            while ending_stack and pos == ending_stack[-1]:
                dir_stack.pop()
                ending_stack.pop()

        elif fg > 0:
            fg_start = pos - 4  # pos already advanced past the 4-byte fg field
            fe_size = struct.unpack_from("<i", data, pos)[0]; pos += 4
            name, pos = _read_null_str(data, pos)
            if (len(name) + 1) % 2 == 1:
                pos += 1

            if fe_size != 0:
                ending_stack.append(fg_start + fe_size)
            elif ending_stack:
                ending_stack.append(ending_stack[-1])

            dir_stack.append(name)

    return entries


# ── Full-archive rebuild helpers ──────────────────────────────────────────────

def _build_path_tree(file_pairs):
    """
    Build a directory tree from (path, bytes) pairs sorted by path.
    Returns root node: {'files': [path, ...], 'dirs': OrderedDict(name -> node)}
    """
    from collections import OrderedDict
    root = {'files': [], 'dirs': OrderedDict()}
    for path, _ in sorted(file_pairs, key=lambda x: x[0].replace("/", "\\").lower()):
        parts = path.replace("/", "\\").split("\\")
        node = root
        for part in parts[:-1]:
            if part not in node['dirs']:
                node['dirs'][part] = {'files': [], 'dirs': OrderedDict()}
            node = node['dirs'][part]
        node['files'].append(path)
    return root


def _dfs_file_order(tree):
    """Return file paths in DFS traversal order (matches dict serialization order)."""
    result = []
    for sub in tree['dirs'].values():
        result.extend(_dfs_file_order(sub))
    result.extend(tree['files'])
    return result


def _write_v1_file_entry(buf: bytearray, path: str, file_offsets: dict, file_sizes: dict):
    name = path.replace("/", "\\").split("\\")[-1]
    name_b = name.encode("utf-8") + b"\x00"
    pad = 1 if (len(name) + 1) % 2 == 0 else 0
    entry_size = 4 + 4 + 4 + 4 + 1 + len(name_b) + pad
    buf.extend(struct.pack("<i", 0))             # fg = 0 (file)
    buf.extend(struct.pack("<i", entry_size))    # fe_size (informational)
    buf.extend(struct.pack("<I", file_offsets[path]))
    buf.extend(struct.pack("<I", file_sizes[path]))
    buf.extend(b"\x00")                          # skip byte
    buf.extend(name_b)
    if pad:
        buf.extend(b"\x00")


def _write_v1_dir_entry(buf: bytearray, dir_name: str, node: dict,
                        file_offsets: dict, file_sizes: dict):
    stored = dir_name + "\\"   # directory names include trailing backslash
    name_b = stored.encode("utf-8") + b"\x00"
    pad = 1 if (len(stored) + 1) % 2 == 1 else 0

    fe_size_off = len(buf)   # position of fg field — used to patch fe_size later
    buf.extend(struct.pack("<i", 1))   # fg = 1 (directory)
    buf.extend(struct.pack("<i", 0))   # fe_size placeholder
    buf.extend(name_b)
    if pad:
        buf.extend(b"\x00")

    for sub_name, sub_node in node['dirs'].items():
        _write_v1_dir_entry(buf, sub_name, sub_node, file_offsets, file_sizes)
    for p in node['files']:
        _write_v1_file_entry(buf, p, file_offsets, file_sizes)

    # fe_size = bytes from start of fg field through all children
    fe_size = len(buf) - fe_size_off
    struct.pack_into("<i", buf, fe_size_off + 4, fe_size)


def _serialize_v1_dict(tree: dict, file_offsets: dict, file_sizes: dict) -> bytes:
    """Serialize a path tree to v1 edata dictionary bytes."""
    buf = bytearray()
    for dir_name, node in tree['dirs'].items():
        _write_v1_dir_entry(buf, dir_name, node, file_offsets, file_sizes)
    for p in tree['files']:
        _write_v1_file_entry(buf, p, file_offsets, file_sizes)
    return bytes(buf)


# ── Public API ────────────────────────────────────────────────────────────────

class EdataFile:
    """
    Represents an open RUSE .dat archive.

    Usage:
        edat = EdataFile.open("ZZ_GladPatchableWin.dat")
        data = edat.get("pc/ndf/patchable/gfx/everything.ndfbin")
        edat.replace("pc/ndf/patchable/gfx/everything.ndfbin", modified_bytes)
    """

    def __init__(self, path: str):
        self.path = path
        self._data: bytes = b""
        self._header: EdataHeader = None
        self._entries: List[EdataEntry] = []
        self._entry_map: Dict[str, EdataEntry] = {}

    @classmethod
    def open(cls, path: str) -> "EdataFile":
        obj = cls(path)
        with open(path, "rb") as f:
            obj._data = f.read()
        obj._parse()
        return obj

    def _parse(self):
        data = self._data
        if not data.startswith(EDATA_MAGIC):
            raise ValueError(f"Not an edata file: {self.path}")
        version = struct.unpack_from("<I", data, 4)[0]
        try:
            if version == 1:
                self._header = _parse_header_v1(data)
                self._entries = _parse_dict_v1(data, self._header)
            elif version == 2:
                self._header = _parse_header_v2(data)
                self._entries = _parse_dict_v2(data, self._header)
            else:
                raise ValueError(f"Unsupported edata version {version} in {self.path}")
        except struct.error as e:
            # A truncated/oversized header or directory record would unpack past the buffer — surface a
            # clean ValueError (callers already handle it) instead of a raw struct.error.
            raise ValueError(f"corrupt edata dictionary in {self.path}: {e}")
        # Build a case-insensitive lookup map (paths use backslash or forward slash interchangeably)
        self._entry_map = {}
        for e in self._entries:
            key = e.path.replace("\\", "/").lower()
            self._entry_map[key] = e

    def list(self) -> List[str]:
        """Return all virtual paths inside the archive."""
        return [e.path for e in self._entries]

    def get(self, virtual_path: str) -> Optional[bytes]:
        """Extract the raw bytes of one file from the archive."""
        key = virtual_path.replace("\\", "/").lower()
        entry = self._entry_map.get(key)
        if entry is None:
            return None
        start = self._header.file_offset + entry.offset
        return self._data[start:start + entry.size]

    def replace(self, virtual_path: str, new_data: bytes) -> None:
        """
        Replace one file inside the archive and write the result back to disk.
        The archive is fully rebuilt (offsets are updated, dictionary is rewritten).
        """
        key = virtual_path.replace("\\", "/").lower()
        if key not in self._entry_map:
            raise KeyError(f"File {virtual_path!r} not found in {self.path}")
        target = self._entry_map[key]
        if self._header.version == 1:
            new_path = self._rebuild_v1(target, new_data)
        else:
            new_path = self._rebuild_v2(target, new_data)
        # Atomic replace
        backup = self.path + ".bak"
        shutil.copy2(self.path, backup)
        shutil.move(new_path, self.path)
        os.remove(backup)
        # Reload so the in-memory state is consistent
        with open(self.path, "rb") as f:
            self._data = f.read()
        self._parse()

    def add(self, virtual_path: str, new_data: bytes) -> None:
        """
        Add a new file to the archive and write the result back to disk.
        Raises KeyError if the path already exists (use replace() instead).
        Only supported for v1 archives.
        """
        key = virtual_path.replace("\\", "/").lower()
        if key in self._entry_map:
            raise KeyError(
                f"File {virtual_path!r} already exists in {self.path}. Use replace() instead."
            )
        if self._header.version != 1:
            raise NotImplementedError(
                f"add() is only implemented for v1 archives (this is v{self._header.version})"
            )
        new_path = self._append_files_v1({virtual_path: new_data})
        backup = self.path + ".bak"
        shutil.copy2(self.path, backup)
        shutil.move(new_path, self.path)
        os.remove(backup)
        with open(self.path, "rb") as f:
            self._data = f.read()
        self._parse()

    def extract_all(self, out_dir: str) -> None:
        """Dump all internal files to out_dir, preserving the virtual path structure.

        Archive entry paths are UNTRUSTED (a shared mod could craft them): any entry that would
        resolve outside out_dir — via '..' or an absolute/drive path — is skipped, so a malicious
        archive can never write files elsewhere on disk."""
        out_root = Path(out_dir).resolve()
        for entry in self._entries:
            rel = entry.path.replace("\\", "/").lstrip("/")
            dest = (out_root / rel).resolve()
            if dest != out_root and out_root not in dest.parents:
                continue                       # refuse a path-traversal entry
            dest.parent.mkdir(parents=True, exist_ok=True)
            raw = self.get(entry.path)
            if raw is not None:
                dest.write_bytes(raw)

    # ── Rebuild helpers ───────────────────────────────────────────────────────

    def _multi_replace_v1(self, to_replace: dict) -> str:
        """Replace multiple existing files in a v1 archive while preserving the
        original dict structure (same directory count, same dict_len, same file_offset).

        to_replace: {virtual_path_key (lower, backslash): new_bytes}
        Returns temp file path.
        """
        data = self._data
        hdr = self._header
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
        tmp_path = tmp.name
        try:
            # Write header + dict unchanged — preserves directory hierarchy exactly
            tmp.write(data[:hdr.file_offset])

            # Rewrite file content in original entry order, applying replacements
            new_offsets: Dict[str, Tuple[int, int]] = {}
            cur_pos = 0
            for entry in self._entries:
                key = entry.path.replace("/", "\\").lower()
                raw = to_replace.get(
                    key,
                    data[hdr.file_offset + entry.offset:hdr.file_offset + entry.offset + entry.size],
                )
                new_offsets[entry.path] = (cur_pos, len(raw))
                tmp.write(raw)
                cur_pos += len(raw)

            tmp.flush()
        finally:
            tmp.close()

        # Patch the dict copy in the temp file with new per-file offsets/sizes
        with open(tmp_path, "r+b") as f:
            pos = hdr.dict_offset
            end = hdr.dict_offset + hdr.dict_length
            entry_idx = 0

            while pos < end:
                f.seek(pos)
                fg = struct.unpack("<i", f.read(4))[0]

                if fg == 0:
                    f.seek(pos + 8)  # skip fg(4) + fe_size(4)
                    offset_pos = f.tell()
                    f.seek(4, 1)     # skip old offset
                    size_pos = f.tell()
                    f.seek(4, 1)     # skip old size
                    f.seek(1, 1)     # skip padding byte

                    name_bytes = bytearray()
                    while True:
                        b = f.read(1)
                        if b == b"\x00":
                            break
                        name_bytes.extend(b)
                    name = name_bytes.decode("utf-8", errors="replace")
                    if (len(name) + 1) % 2 == 0:
                        f.seek(1, 1)

                    end_of_entry = f.tell()
                    cur_entry = self._entries[entry_idx]
                    new_off, new_sz = new_offsets[cur_entry.path]

                    f.seek(offset_pos)
                    f.write(struct.pack("<I", new_off))
                    f.seek(size_pos)
                    f.write(struct.pack("<I", new_sz))

                    pos = end_of_entry
                    entry_idx += 1

                elif fg > 0:
                    f.seek(pos + 8)  # skip fg(4) + fe_size(4)
                    name_bytes = bytearray()
                    while True:
                        b = f.read(1)
                        if b == b"\x00":
                            break
                        name_bytes.extend(b)
                    name = name_bytes.decode("utf-8", errors="replace")
                    if (len(name) + 1) % 2 == 1:
                        f.seek(1, 1)
                    pos = f.tell()
                else:
                    break

            # Update FileLength at 0x25
            f.seek(0x25)
            f.write(struct.pack("<I", cur_pos))

            # Recompute dict checksum at 0x08 (offsets/sizes changed)
            f.seek(hdr.dict_offset)
            dict_data = f.read(hdr.dict_length)
            f.seek(8)
            f.write(_md5(dict_data))

        return tmp_path

    def _batch_update_v1(self, to_replace: dict, to_add: dict) -> str:
        """Rebuild a v1 archive: replace existing files and/or add new files.

        to_replace: {virtual_path_key (lower, backslash): new_bytes}
        to_add:     {virtual_path (as-is): new_bytes}
        Returns temp file path.

        Both branches preserve the original dict's (radix-tree) structure exactly — the
        from-scratch rebuild produced a game-incompatible structure (see _append_files_v1).
        Replaces patch offsets/sizes in place; adds are prepended as root-level entries.
        """
        if not to_add:
            return self._multi_replace_v1(to_replace)
        if not to_replace:
            return self._append_files_v1(to_add)
        # Apply replaces (structure-preserving) first, then append new files onto that.
        rp = self._multi_replace_v1(to_replace)
        replaced = EdataFile.open(rp)
        out = replaced._append_files_v1(to_add)
        os.remove(rp)
        return out

    def batch_update(self, to_replace: dict, to_add: dict) -> None:
        """Replace existing files and add new files in one efficient rebuild.

        to_replace: {virtual_path: new_bytes}  — must already exist in archive
        to_add:     {virtual_path: new_bytes}  — must NOT already exist
        Only supported for v1 archives.
        """
        if self._header.version != 1:
            raise NotImplementedError(f"batch_update() only supported for v1 archives (got v{self._header.version})")
        replace_keys = {p.replace("/", "\\").lower(): b for p, b in to_replace.items()}
        new_path = self._batch_update_v1(replace_keys, to_add)
        backup = self.path + ".bak"
        shutil.copy2(self.path, backup)
        shutil.move(new_path, self.path)
        os.remove(backup)
        with open(self.path, "rb") as f:
            self._data = f.read()
        self._parse()

    def _append_files_v1(self, to_add: dict) -> str:
        """Add new files to a v1 archive WITHOUT rebuilding the directory tree.

        The on-disk dict is a char-prefix-compressed radix tree; rebuilding it from
        scratch produces a different (game-incompatible) structure.  Instead we keep the
        original dict bytes verbatim and PREPEND each new file as a ROOT-LEVEL entry whose
        name is its full virtual path (legal — a dict name is just the residual after its
        parent prefix, and file names may contain backslashes).  Prepending (not appending)
        guarantees root level: the dict's last directory scope stays open through the end,
        so appended entries would nest under it, but at the dict's start no scope is open.
        Directory fe_sizes are relative spans, so shifting the original entries later is
        harmless.  New file data is appended after the existing data region, so every
        existing file's relative offset is unchanged.  Returns temp file path.

        to_add: {virtual_path: bytes}  (paths must not already exist)
        """
        data = self._data
        hdr = self._header
        orig_dict = data[hdr.dict_offset: hdr.dict_offset + hdr.dict_length]
        orig_data = data[hdr.file_offset: hdr.file_offset + hdr.file_length]

        new_entries = bytearray()
        new_data = bytearray()
        cur = len(orig_data)            # new files' offsets are relative to file_offset
        for vpath, content in to_add.items():
            name = vpath.replace("/", "\\")
            name_b = name.encode("utf-8") + b"\x00"
            pad = 1 if (len(name) + 1) % 2 == 0 else 0
            entry_size = 4 + 4 + 4 + 4 + 1 + len(name_b) + pad
            new_entries += struct.pack("<i", 0)             # fg = 0 (file)
            new_entries += struct.pack("<i", entry_size)    # fileEntrySize
            new_entries += struct.pack("<I", cur)           # offset (rel to file_offset)
            new_entries += struct.pack("<I", len(content))  # size
            new_entries += b"\x00"                          # skip byte (no checksum in v1)
            new_entries += name_b
            if pad:
                new_entries += b"\x00"
            new_data += content
            cur += len(content)

        new_dict = bytes(new_entries) + orig_dict      # prepend -> entries are at root level
        new_file_offset = hdr.dict_offset + len(new_dict)
        new_data_region = orig_data + bytes(new_data)

        header_region = bytearray(data[:hdr.dict_offset])      # header + reserved gap, verbatim
        struct.pack_into("<I", header_region, 0x1D, len(new_dict))         # DictLength
        struct.pack_into("<I", header_region, 0x21, new_file_offset)       # FileOffset
        struct.pack_into("<I", header_region, 0x25, len(new_data_region))  # FileLength
        header_region[8:24] = _md5(new_dict)                               # dict MD5

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
        tmp_path = tmp.name
        try:
            tmp.write(bytes(header_region))
            tmp.write(new_dict)
            tmp.write(new_data_region)
        finally:
            tmp.close()
        return tmp_path

    def _add_v1(self, virtual_path: str, new_content: bytes) -> str:
        """Rebuild a v1 archive with one new file added.  Returns temp file path."""
        data = self._data
        hdr = self._header

        # Collect all existing files as (path, raw_bytes) pairs, plus the new file
        all_pairs = [
            (e.path, data[hdr.file_offset + e.offset:hdr.file_offset + e.offset + e.size])
            for e in self._entries
        ]
        all_pairs.append((virtual_path, new_content))

        # Build a sorted directory tree and determine DFS file traversal order
        tree = _build_path_tree(all_pairs)
        ordered_paths = _dfs_file_order(tree)

        # Compute file offsets (relative to start of file-data region) in DFS order
        path_to_bytes = {p: b for p, b in all_pairs}
        file_offsets: Dict[str, int] = {}
        file_sizes:   Dict[str, int] = {}
        cur = 0
        for p in ordered_paths:
            file_offsets[p] = cur
            file_sizes[p]   = len(path_to_bytes[p])
            cur += file_sizes[p]

        # Serialize the new dictionary
        dict_bytes = _serialize_v1_dict(tree, file_offsets, file_sizes)

        # Rebuild header region: copy original bytes up to dict_offset, patch fields
        header_region = bytearray(data[:hdr.dict_offset])
        new_file_offset = hdr.dict_offset + len(dict_bytes)
        struct.pack_into("<I", header_region, 0x1D, len(dict_bytes))  # DictLength
        struct.pack_into("<I", header_region, 0x21, new_file_offset)  # FileOffset
        struct.pack_into("<I", header_region, 0x25, cur)              # FileLength
        header_region[8:24] = _md5(dict_bytes)                        # dict MD5 checksum

        # Write to temp file: header + dict + file data
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
        tmp_path = tmp.name
        try:
            tmp.write(bytes(header_region))
            tmp.write(dict_bytes)
            for p in ordered_paths:
                tmp.write(path_to_bytes[p])
        finally:
            tmp.close()

        return tmp_path

    def _rebuild_v1(self, target: EdataEntry, new_content: bytes) -> str:
        """Rebuild a v1 archive with target replaced, write to temp file."""
        data = self._data
        hdr = self._header
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
        tmp_path = tmp.name
        try:
            # Write everything up to file_offset unchanged (header + dict)
            tmp.write(data[:hdr.file_offset])

            # Rewrite file content, updating offsets
            new_offsets: Dict[str, Tuple[int, int]] = {}
            cur_pos = 0
            for entry in self._entries:
                raw = new_content if entry is target else data[hdr.file_offset + entry.offset:hdr.file_offset + entry.offset + entry.size]
                new_offsets[entry.path] = (cur_pos, len(raw))
                tmp.write(raw)
                cur_pos += len(raw)

            # Go back and patch the dictionary with new offsets/sizes
            tmp.flush()
        finally:
            tmp.close()

        # Patch dictionary in the temp file
        with open(tmp_path, "r+b") as f:
            pos = hdr.dict_offset
            end = hdr.dict_offset + hdr.dict_length
            entry_idx = 0

            while pos < end:
                f.seek(pos)
                fg = struct.unpack("<i", f.read(4))[0]

                if fg == 0:
                    fe_size_pos = pos + 4
                    f.seek(fe_size_pos + 4)  # skip fileEntrySize

                    offset_pos = f.tell()
                    f.seek(4, 1)  # skip old offset
                    size_pos = f.tell()
                    f.seek(4, 1)  # skip old size
                    f.seek(1, 1)  # skip padding byte

                    # Read name to advance pos
                    name_bytes = bytearray()
                    while True:
                        b = f.read(1)
                        if b == b"\x00":
                            break
                        name_bytes.extend(b)
                    name = name_bytes.decode("utf-8", errors="replace")
                    if (len(name) + 1) % 2 == 0:
                        f.seek(1, 1)

                    end_of_entry = f.tell()  # save before seeking back to write
                    cur_entry = self._entries[entry_idx]
                    new_off, new_sz = new_offsets[cur_entry.path]

                    f.seek(offset_pos)
                    f.write(struct.pack("<I", new_off))
                    f.seek(size_pos)
                    f.write(struct.pack("<I", new_sz))

                    pos = end_of_entry
                    entry_idx += 1

                elif fg > 0:
                    f.seek(pos + 4)
                    f.seek(4, 1)  # skip fileEntrySize
                    name_bytes = bytearray()
                    while True:
                        b = f.read(1)
                        if b == b"\x00":
                            break
                        name_bytes.extend(b)
                    name = name_bytes.decode("utf-8", errors="replace")
                    if (len(name) + 1) % 2 == 1:
                        f.seek(1, 1)
                    pos = f.tell()
                else:
                    break

            # Update FileLength at 0x25
            f.seek(0x25)
            f.write(struct.pack("<I", cur_pos))

            # Update dict checksum at 0x08
            f.seek(hdr.dict_offset)
            dict_data = f.read(hdr.dict_length)
            checksum = _md5(dict_data)
            f.seek(8)
            f.write(checksum)

        return tmp_path

    def _rebuild_v2(self, target: EdataEntry, new_content: bytes) -> str:
        """Rebuild a v2 archive with target replaced, write to temp file."""
        data = self._data
        hdr = self._header
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".dat")
        tmp_path = tmp.name
        try:
            # Copy header (file_offset bytes)
            tmp.write(data[:hdr.file_offset])

            new_offsets: Dict[str, Tuple[int, int, bytes]] = {}
            cur_pos = 0
            reserve = b"\x00" * 200  # v2 uses a 200-byte reserve between files

            for entry in self._entries:
                raw = new_content if entry is target else data[hdr.file_offset + entry.offset:hdr.file_offset + entry.offset + entry.size]
                checksum = _md5(raw) if entry is target else entry.checksum
                new_offsets[entry.path] = (cur_pos, len(raw), checksum)
                tmp.write(raw)
                tmp.write(reserve)
                cur_pos += len(raw) + len(reserve)

            tmp.flush()
        finally:
            tmp.close()

        # Patch dictionary
        with open(tmp_path, "r+b") as f:
            pos = hdr.dict_offset
            end = hdr.dict_offset + hdr.dict_length
            entry_idx = 0

            while pos < end:
                f.seek(pos)
                fg = struct.unpack("<i", f.read(4))[0]

                if fg == 0:
                    f.seek(pos + 8)  # skip fg + fileEntrySize
                    offset_pos = f.tell()
                    f.seek(8, 1)  # skip old offset (int64)
                    size_pos = f.tell()
                    f.seek(8, 1)  # skip old size (int64)
                    checksum_pos = f.tell()
                    f.seek(16, 1)  # skip old checksum

                    name_bytes = bytearray()
                    while True:
                        b = f.read(1)
                        if b == b"\x00":
                            break
                        name_bytes.extend(b)
                    name = name_bytes.decode("utf-8", errors="replace")
                    if len(name) % 2 == 0:
                        f.seek(1, 1)

                    end_of_entry = f.tell()  # save before seeking back to write
                    cur_entry = self._entries[entry_idx]
                    new_off, new_sz, new_cs = new_offsets[cur_entry.path]

                    f.seek(offset_pos)
                    f.write(struct.pack("<q", new_off))
                    f.seek(size_pos)
                    f.write(struct.pack("<q", new_sz))
                    f.seek(checksum_pos)
                    f.write(new_cs)

                    pos = end_of_entry
                    entry_idx += 1

                elif fg > 0:
                    f.seek(pos + 8)
                    name_bytes = bytearray()
                    while True:
                        b = f.read(1)
                        if b == b"\x00":
                            break
                        name_bytes.extend(b)
                    name = name_bytes.decode("utf-8", errors="replace")
                    if len(name) % 2 == 0:
                        f.seek(1, 1)
                    pos = f.tell()
                else:
                    break

            # Update total files_data_size at 0x25
            f.seek(0, 2)  # end of file
            file_end = f.tell()
            new_data_size = file_end - hdr.file_offset
            f.seek(0x25)
            f.write(struct.pack("<I", new_data_size))

            # Recompute dict checksum at 0x31
            f.seek(hdr.dict_offset)
            dict_data = f.read(hdr.dict_length)
            checksum = _md5(dict_data)
            f.seek(0x31)
            f.write(checksum)

        return tmp_path


def open_dat(path: str) -> EdataFile:
    """Convenience function — open a .dat archive and return an EdataFile."""
    return EdataFile.open(path)

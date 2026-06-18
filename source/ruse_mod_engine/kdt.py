"""
RUSE KD-tree (KDT) reader/writer for TStreamedMeshKdTree NDF instances.

A .kdt file is an NDF binary (EUG0/CNDF wrapper) containing a single
TStreamedMeshKdTree instance.  The Storage Blob property encodes the entire
KD-tree in a multi-stream zlib format:

  blob[0:4]            comp1_size  — compressed byte count of d1
  blob[4:8]            decomp1_size — nominally decompressed size (may be off by ~30 B)
  blob[8:8+comp1_size] d1 compressed (standard zlib, NOT Z_FULL_FLUSH)
  blob[8+comp1_size:]  subtrees_raw — chunks 1-4 (runtime-loaded; preserve verbatim)

d1 (decompressed chunk 0) layout:
  [4]  vtx_count  (uint32)
  [4]  quant_scale = 0x7FFF (uint32; divisor for uint16→world-unit conversion)
  [vtx_count × 6]  vertex buffer: vtx_count × (u16 qx, u16 qy, u16 qz)
      world_x = bbox_min.x + (qx / 32767.0) * (bbox_max.x - bbox_min.x)
      world_y = bbox_min.y + (qy / 32767.0) * (bbox_max.y - bbox_min.y)
      NOTE: qz encodes KD-tree depth/structure, NOT physical height — always preserve
  [rest]  conn stream (0x80 XX encoded tree data) + 0xDD runtime placeholders

All OffsetOf* NDF properties are byte offsets into d1.
The 0xDD-initialized sections (IndexBufferIndexes onward) are filled at game
load time from subtrees_raw (chunks 1-4); they are NOT useful static data.
"""
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ndfbin


# ── Helpers ───────────────────────────────────────────────────────────────────

def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]

def _pack_u32(v: int) -> bytes:
    return struct.pack("<I", v)

def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

# Z_FULL_FLUSH stored-block sync marker that zlib uses as a flush point
_ZFULL_FLUSH_MARKER = b"\x00\x00\xff\xff"

def _decompress_d1(data: bytes) -> bytes:
    """Decompress d1, handling both standard zlib and Z_FULL_FLUSH variants.

    Most KDTs use standard zlib.  A subset (e.g. challenge_2 maps) append a
    Z_FULL_FLUSH stored-block (00 00 FF FF) followed by 4 bytes of metadata
    that are NOT part of the deflate stream.  The metadata causes a standard
    decompress to fail with "invalid block type".

    Fix: find the last 00-00-FF-FF sync marker and decompress up to that point.
    """
    try:
        return zlib.decompressobj(0).decompress(data)
    except zlib.error:
        pass
    # Find last Z_FULL_FLUSH sync marker and decompress only the valid prefix
    pos = data.rfind(_ZFULL_FLUSH_MARKER)
    if pos >= 0:
        try:
            return zlib.decompressobj(0).decompress(data[:pos + 4])
        except zlib.error:
            pass
    raise zlib.error(f"Cannot decompress d1 ({len(data)} bytes): no valid zlib stream found")


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class KdTree:
    """Parsed KD-tree.

    Public fields:
      bbox_min, bbox_max  — spatial bounds in RUSE world units (x, y, z)
      tri_count           — total triangles
      subtree_count       — always 1 for production KDTs
      vertices            — list of (world_x, world_y) decoded positions

    Internal fields (prefixed _) are needed for round-trip serialisation.
    Modify `vertices` then call `kdt.write(kdt_bytes)` to rebuild.
    For a full rebuild from zone polygons use `kdt.build()`.
    """

    # Spatial bounds
    bbox_min: Tuple[float, float, float]
    bbox_max: Tuple[float, float, float]

    # Geometry summary
    tri_count: int
    subtree_count: int

    # Decoded vertex world positions — list of (x, y).  Z is preserved via _vtx_raw.
    vertices: List[Tuple[float, float]]

    # Raw vertex buffer bytes (vtx_count × 6; uint16 × 3 per vertex)
    _vtx_raw: bytes

    # d1 bytes after the vertex buffer (conn stream + 0xDD sections) — preserved verbatim
    _d1_tail: bytes

    # Blob suffix after compressed d1 (chunks 1–4, loaded at runtime) — preserved verbatim
    _subtrees_raw: bytes

    # NDF binary for writing back to file
    _ndf: object  # NdfBinary

    # OffsetOf* property values (byte offsets into d1) — for reference
    offsets: Dict[str, int] = field(default_factory=dict)

    @property
    def vtx_count(self) -> int:
        return len(self.vertices)


# ── Read ──────────────────────────────────────────────────────────────────────

def read(kdt_bytes: bytes) -> KdTree:
    """Parse NDF kdt bytes → KdTree.

    kdt_bytes: raw file bytes as returned by edata.get() / dat.get()
    """
    ndf = ndfbin.read(kdt_bytes)
    inst = ndf.instances[0]

    props: Dict[str, ndfbin.NdfValue] = {}
    for pv in inst.props:
        name = ndf.properties[pv.prop_index].name
        props[name] = pv.value

    bbox_min = tuple(props["BoundingBoxMin"].raw)
    bbox_max = tuple(props["BoundingBoxMax"].raw)
    tri_count = int(props["TriangleCount"].raw)
    sub_count = int(props["SubtreeCount"].raw)

    offsets = {
        "VertexBufferIndexes":          int(props["OffsetOfVertexBufferIndexes"].raw),
        "IndexBuffer":                  int(props["OffsetOfIndexBuffer"].raw),
        "IndexBufferIndexes":           int(props["OffsetOfIndexBufferIndexes"].raw),
        "TriangleIndexLists":           int(props["OffsetOfTriangleIndexLists"].raw),
        "TriangleIndexBufferIndexes":   int(props["OffsetOfTriangleIndexBufferIndexes"].raw),
        "MainNode":                     int(props["OffsetOfMainNode"].raw),
        "CompressedSubtreeIndexBuffer": int(props["OffsetOfCompressedSubtreeIndexBuffer"].raw),
        "CompressedSubtrees":           int(props["OffsetOfCompressedSubtrees"].raw),
    }

    blob = bytes(props["Storage"].raw)
    comp1_size = _u32(blob, 0)
    subtrees_raw = blob[8 + comp1_size:]

    # Decompress d1; ignore the preamble's decomp1_size — it may be off by a few bytes.
    # Some KDTs have a Z_FULL_FLUSH terminator + metadata bytes at the end of the stream.
    d1 = _decompress_d1(blob[8:8 + comp1_size])

    vtx_count  = _u32(d1, 0)
    vtx_end    = 8 + vtx_count * 6
    vtx_raw    = d1[8:vtx_end]
    d1_tail    = d1[vtx_end:]

    # Decode world XY (Z encodes tree structure; always preserve via _vtx_raw)
    bx_r = bbox_max[0] - bbox_min[0]
    by_r = bbox_max[1] - bbox_min[1]
    vertices: List[Tuple[float, float]] = []
    for i in range(vtx_count):
        base = i * 6
        qx = struct.unpack_from("<H", vtx_raw, base)[0]
        qy = struct.unpack_from("<H", vtx_raw, base + 2)[0]
        wx = bbox_min[0] + (qx / 32767.0) * bx_r
        wy = bbox_min[1] + (qy / 32767.0) * by_r
        vertices.append((wx, wy))

    return KdTree(
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        tri_count=tri_count,
        subtree_count=sub_count,
        vertices=vertices,
        _vtx_raw=vtx_raw,
        _d1_tail=d1_tail,
        _subtrees_raw=subtrees_raw,
        _ndf=ndf,
        offsets=offsets,
    )


# ── Write ─────────────────────────────────────────────────────────────────────

def write(kdt: KdTree) -> bytes:
    """Rebuild NDF kdt bytes from a KdTree (vertex-edit round-trip).

    Re-quantizes `kdt.vertices` into uint16 XY, preserves qz from `_vtx_raw`,
    recompresses d1, rebuilds the Storage blob, and returns new NDF bytes.

    Only the Storage property is changed.  All other NDF properties (OffsetOf*,
    BoundingBox, TriangleCount, etc.) are written back unchanged.
    Call this after modifying `kdt.vertices` in-place.
    """
    bx_r = kdt.bbox_max[0] - kdt.bbox_min[0]
    by_r = kdt.bbox_max[1] - kdt.bbox_min[1]
    vtx_count = kdt.vtx_count

    new_vtx_raw = bytearray(vtx_count * 6)
    for i, (wx, wy) in enumerate(kdt.vertices):
        qx = _clamp(int(round(((wx - kdt.bbox_min[0]) / bx_r) * 32767.0)), 0, 32767)
        qy = _clamp(int(round(((wy - kdt.bbox_min[1]) / by_r) * 32767.0)), 0, 32767)
        base = i * 6
        struct.pack_into("<HH", new_vtx_raw, base, qx, qy)
        # Preserve original qz (encodes tree depth — must not change)
        new_vtx_raw[base + 4:base + 6] = kdt._vtx_raw[base + 4:base + 6]

    d1 = (
        _pack_u32(vtx_count)
        + _pack_u32(0x7FFF)
        + bytes(new_vtx_raw)
        + kdt._d1_tail
    )

    d1_comp = zlib.compress(d1, 9)

    blob = (
        _pack_u32(len(d1_comp))
        + _pack_u32(len(d1))
        + d1_comp
        + kdt._subtrees_raw
    )

    # Update Storage property in NDF (mutation in place)
    ndf = kdt._ndf
    inst = ndf.instances[0]
    for pv in inst.props:
        if ndf.properties[pv.prop_index].name == "Storage":
            pv.value.raw = bytearray(blob)
            break

    return ndfbin.write(ndf)


# ── CS node-opcode + TIL codecs (RE'd from RUSE.exe FUN_140aa2e50 / leaf handlers) ──
# Both proven byte-identical round-trip on beta/chess/blitz. See docs/map_editor/ruse_exe_re.md.

# A runtime KD node is 8 bytes (tag u32 + split u32). The CS region stores them as a
# compact opcode stream (DFS pre-order). These codecs convert between the opcode bytes
# and a flat node list. Node tuple forms:
#   ("int", axis, abs_split, two_children: bool, flag5: int)   internal node
#   ("leaf", count, variant: int)                              leaf (count triangles)

def decode_opcodes(stream: bytes) -> Tuple[list, bytes]:
    """Decode a CS opcode stream → (nodes, trailing_pad).

    nodes: list of ("int", axis, abs_split, two, flag5) | ("leaf", count, variant)
    Splits are delta-encoded per axis in the stream; abs_split is the accumulated value.
    """
    nodes: list = []
    split = [0, 0, 0]
    p, end = 0, len(stream)
    while end > 0 and stream[end - 1] == 0x7E:  # strip 7e terminator/pad
        end -= 1
    while p < end:
        op = stream[p]; p += 1
        if op >= 0x80:                                  # leaf
            ind = op & 0x1F
            if ind == 0x1F:
                cnt = stream[p] + 1; p += 1
            else:
                cnt = ind + 1
            nodes.append(("leaf", cnt, (op >> 5) & 3))
        else:                                           # internal
            axis = (op >> 3) & 3
            two = bool(op & 0x40)
            flag5 = (op >> 5) & 1
            if op & 4:                                  # 2-byte delta (big-endian)
                mag = (stream[p] << 8) | stream[p + 1]; p += 2
            else:                                       # 1-byte delta (+ op bit0 as 9th bit)
                mag = ((op & 1) << 8) | stream[p]; p += 1
            delta = -mag if (op & 2) else mag
            if axis < 3:
                split[axis] = (split[axis] + delta) & 0xFFFF
            nodes.append(("int", axis, split[axis] if axis < 3 else 0, two, flag5))
    return nodes, stream[end:]


def encode_opcodes(nodes: list, tail: bytes = b"") -> bytes:
    """Encode a flat node list → CS opcode stream (inverse of decode_opcodes).

    Uses minimal-width split deltas (1-byte when |delta| ≤ 0x1ff), matching the
    original encoder exactly.
    """
    out = bytearray()
    split = [0, 0, 0]
    for nd in nodes:
        if nd[0] == "leaf":
            _, cnt, variant = nd
            ind = cnt - 1
            if ind < 0x1F:
                out.append(0x80 | (variant << 5) | ind)
            else:
                out.append(0x80 | (variant << 5) | 0x1F)
                out.append((cnt - 1) & 0xFF)
        else:
            _, axis, abs_split, two, flag5 = nd
            # delta accumulates mod 0x10000 at decode; pick the minimal-magnitude
            # signed delta that reaches abs_split from the running value.
            if axis < 3:
                raw = (abs_split - split[axis]) & 0xFFFF
                delta = raw - 0x10000 if raw >= 0x8000 else raw
            else:
                delta = 0
            mag = abs(delta); sign = 1 if delta < 0 else 0
            op = (axis << 3) | (0x40 if two else 0) | (flag5 << 5) | (sign << 1)
            if mag <= 0x1FF:
                op |= (mag >> 8) & 1
                out.append(op); out.append(mag & 0xFF)
            else:
                op |= 4
                out.append(op); out.append((mag >> 8) & 0xFF); out.append(mag & 0xFF)
            if axis < 3:
                split[axis] = abs_split & 0xFFFF
    return bytes(out) + tail


def leaf_triangle_counts(nodes: list) -> list:
    """Triangle counts per leaf, in stream/DFS order (used to walk the TIL)."""
    return [nd[1] for nd in nodes if nd[0] == "leaf"]


def decode_til(til: bytes, counts: list) -> Tuple[list, int]:
    """Decode TIL → list of per-leaf triangle-index lists; returns (lists, bytes_consumed).

    Each leaf list has `counts[i]` entries, delta-varint encoded:
      byte < 0x80 → value += (byte - 0x40);  byte >= 0x80 → 4-byte big-endian absolute.
    """
    lists, p = [], 0
    for cnt in counts:
        prev, vals = 0, []
        for _ in range(cnt):
            b = til[p]
            if b < 0x80:
                prev = prev + (b - 0x40); p += 1
            else:
                prev = ((b & 0x7F) << 24) | (til[p + 1] << 16) | (til[p + 2] << 8) | til[p + 3]
                p += 4
            vals.append(prev)
        lists.append(vals)
    return lists, p


def encode_til(lists: list) -> bytes:
    """Encode per-leaf triangle-index lists → TIL (inverse of decode_til)."""
    out = bytearray()
    for vals in lists:
        prev = 0
        for v in vals:
            d = v - prev
            if -0x40 <= d <= 0x3F:
                out.append((d + 0x40) & 0xFF)
            else:
                out.append(0x80 | ((v >> 24) & 0x7F))
                out.append((v >> 16) & 0xFF)
                out.append((v >> 8) & 0xFF)
                out.append(v & 0xFF)
            prev = v
    return bytes(out)


# ── Build helpers ─────────────────────────────────────────────────────────────

def _tri_centroid_axis(t: tuple, axis: int) -> float:
    _, v0, v1, v2 = t
    return (v0[axis] + v1[axis] + v2[axis]) / 3.0


def _build_kdt_nodes(
    tris: list,
    bbox_min: tuple,
    bbox_max: tuple,
    max_depth: int,
) -> list:
    """Build heap-indexed KD-tree from zone triangles.

    Returns a list of N entries where entry[i] = [zone_id, ...] for node i.
    Internal nodes (depth < max_depth) receive zone_ids of spanning triangles.
    Leaf nodes (depth == max_depth) receive zone_ids of all remaining triangles.
    """
    nodes: dict = {}

    def recurse(node_idx, tri_set, depth, xmin, xmax, ymin, ymax):
        nodes.setdefault(node_idx, [])
        if not tri_set:
            return

        if depth >= max_depth:
            nodes[node_idx] = [z for (z, v0, v1, v2) in tri_set if z != 0]
            return

        axis = depth % 2  # 0 = X, 1 = Y
        sorted_tris = sorted(tri_set, key=lambda t: _tri_centroid_axis(t, axis))
        split = _tri_centroid_axis(sorted_tris[len(sorted_tris) // 2], axis)

        spanning, left_tris, right_tris = [], [], []
        for t in tri_set:
            z, v0, v1, v2 = t
            coords = [v0[axis], v1[axis], v2[axis]]
            cmin, cmax = min(coords), max(coords)
            if cmin < split < cmax:
                spanning.append(t)
            elif cmax <= split:
                left_tris.append(t)
            else:
                right_tris.append(t)

        nodes[node_idx] = [z for (z, v0, v1, v2) in spanning if z != 0]

        li, ri = 2 * node_idx + 1, 2 * node_idx + 2
        if axis == 0:
            recurse(li, left_tris,  depth + 1, xmin,  split, ymin,  ymax)
            recurse(ri, right_tris, depth + 1, split, xmax,  ymin,  ymax)
        else:
            recurse(li, left_tris,  depth + 1, xmin,  xmax,  ymin,  split)
            recurse(ri, right_tris, depth + 1, xmin,  xmax,  split, ymax)

    recurse(0, tris, 0, bbox_min[0], bbox_max[0], bbox_min[1], bbox_max[1])

    if not nodes:
        return [[]]
    N = max(nodes.keys()) + 1
    return [nodes.get(i, []) for i in range(N)]


def _encode_conn(nodes: list) -> bytes:
    """Encode conn stream: per node, (0x80 zone_id)* 0x00."""
    buf = bytearray()
    for zone_ids in nodes:
        for z in zone_ids:
            buf += bytes([0x80, z & 0xFF])
        buf += b'\x00'
    return bytes(buf)


def _find_raw_chunks(buf: bytes) -> list:
    """Return each zlib chunk in buf as raw bytes (8-byte header + compressed data)."""
    raw_chunks = []
    i = 0
    while True:
        pos = buf.find(b'\x78\xda', i)
        if pos < 0:
            break
        if pos >= 8:
            comp  = struct.unpack_from('<I', buf, pos - 8)[0]
            decomp = struct.unpack_from('<I', buf, pos - 4)[0]
            if 4 <= comp <= 200_000 and 4 <= decomp <= 2_000_000 and pos + comp <= len(buf):
                raw_chunks.append(buf[pos - 8 : pos + comp])
        i = pos + 1
    return raw_chunks


# ── FULL round-trip codec (load/edit/write every portion; byte-identical) ───────
# Storage blob layout: a sequence of zlib chunks, each = 8-byte [comp:u32][decomp:u32]
# header + zlib stream (length comp-4, compressobj(level)+Z_SYNC_FLUSH) + a raw inter-chunk
# gap (TIBI / MainNode / CSIB live raw in the gaps). chunk0=d1 (verts+conn+0xDD), chunk1=VBI,
# chunk2=geometry/zone records, chunk3=TIL, chunk4=CS opcodes.
_COMP_LEVELS = (7, 9, 6, 8, 5, 4, 3, 2, 1)


def _detect_comp_level(content: bytes, target_zlib: bytes):
    """Level whose compressobj(level)+Z_SYNC_FLUSH reproduces target_zlib byte-identically."""
    for lv in _COMP_LEVELS:
        co = zlib.compressobj(lv, zlib.DEFLATED, zlib.MAX_WBITS)
        if co.compress(content) + co.flush(zlib.Z_SYNC_FLUSH) == target_zlib:
            return lv
    return None


def _compress_chunk(content: bytes, level) -> bytes:
    co = zlib.compressobj(level if level else 7, zlib.DEFLATED, zlib.MAX_WBITS)
    return co.compress(content) + co.flush(zlib.Z_SYNC_FLUSH)


def _parse_blob(blob: bytes):
    """blob -> (lead_bytes, [chunk dicts]).  chunk = {content, level, gap, _zlib}."""
    headers = []
    j = 0
    while True:
        z = blob.find(b'\x78\xda', j)
        if z < 0:
            break
        if z >= 8:
            comp, decomp = struct.unpack_from('<II', blob, z - 8)
            zlib_len = comp - 4
            if 4 <= comp <= 32_000_000 and 4 <= decomp <= 64_000_000 and z + zlib_len <= len(blob):
                try:
                    content = zlib.decompressobj(0).decompress(blob[z:z + zlib_len])
                except zlib.error:
                    content = None
                if content is not None and len(content) == decomp:
                    headers.append((z, zlib_len, content))
                    j = z + zlib_len
                    continue
        j = z + 1
    chunks = []
    for k, (z, zlib_len, content) in enumerate(headers):
        zend = z + zlib_len
        nxt = (headers[k + 1][0] - 8) if k + 1 < len(headers) else len(blob)
        zb = blob[z:zend]
        chunks.append({"content": content, "level": _detect_comp_level(content, zb),
                       "gap": blob[zend:nxt], "_zlib": zb})
    lead = blob[:headers[0][0] - 8] if headers else blob
    return lead, chunks


def _reassemble_blob(lead: bytes, chunks: list) -> bytes:
    out = bytearray(lead)
    for c in chunks:
        zb = c["_zlib"] if (c.get("_zlib") is not None and c["level"] is None) \
            else _compress_chunk(c["content"], c["level"])
        out += struct.pack("<II", len(zb) + 4, len(c["content"]))
        out += zb + c["gap"]
    return bytes(out)


def _chunk_spans(blob: bytes) -> list:
    """Byte spans of each zlib chunk in the blob -> list of (hdr_off, data_off, data_end).
    hdr = the 8-byte [comp][decomp] header; data = the zlib stream; gaps live between data_end and
    the next hdr. Same chunk-detection as _parse_blob."""
    spans, j = [], 0
    while True:
        z = blob.find(b'\x78\xda', j)
        if z < 0:
            break
        if z >= 8:
            comp, decomp = struct.unpack_from('<II', blob, z - 8)
            zlib_len = comp - 4
            if 4 <= comp <= 32_000_000 and 4 <= decomp <= 64_000_000 and z + zlib_len <= len(blob):
                try:
                    content = zlib.decompressobj(0).decompress(blob[z:z + zlib_len])
                except zlib.error:
                    content = None
                if content is not None and len(content) == decomp:
                    spans.append((z - 8, z, z + zlib_len))
                    j = z + zlib_len
                    continue
        j = z + 1
    return spans


def _remap_offsets(orig_blob: bytes, new_blob: bytes, offsets: dict):
    """Map each OffsetOf* value (a byte offset into orig_blob, pointing at a chunk header or a position
    inside an inter-chunk gap) to its corresponding byte offset in new_blob. Returns {name: new_off} or
    None if the chunk counts differ (can't map). Anchors: an offset equal to a chunk header tracks that
    header; an offset inside gap i (chunk i's data_end .. chunk i+1's header) keeps its sub-offset from
    chunk i's data_end."""
    L0, L1 = _chunk_spans(orig_blob), _chunk_spans(new_blob)
    if not L0 or len(L0) != len(L1):
        return None
    hdr0 = [c[0] for c in L0]; end0 = [c[2] for c in L0]
    hdr1 = [c[0] for c in L1]; end1 = [c[2] for c in L1]
    n = len(L0)

    def remap(v):
        for i in range(n):                       # exact chunk header?
            if v == hdr0[i]:
                return hdr1[i]
        for i in range(n):                       # inside gap after chunk i?
            nxt = hdr0[i + 1] if i + 1 < n else len(orig_blob)
            if end0[i] <= v < nxt:
                return end1[i] + (v - end0[i])
        return v                                 # before first chunk / unrecognised -> leave as-is

    return {name: remap(v) for name, v in offsets.items()}


def decode_conn(conn: bytes):
    """conn (heap-node -> zone/ref list) stream -> (nodes, consumed).
    Each ref = (0x80|hi, lo) -> value (hi<<8)|lo; each node ends with 0x00.
    Stops at the first <0x80 non-zero byte (a trailing u32 IndexBuffer = conn_tail) or
    truncation; `consumed` is how many bytes were node entries. A final unterminated node
    is captured."""
    nodes, cur, p = [], [], 0
    while p < len(conn):
        b = conn[p]
        if b == 0x00:
            nodes.append(cur); cur = []; p += 1
        elif b & 0x80:
            if p + 1 >= len(conn):
                break
            cur.append(((b & 0x7F) << 8) | conn[p + 1]); p += 2
        else:
            break
    if cur:
        nodes.append(cur)
    return nodes, p


def encode_conn_faithful(nodes: list, terminated: bool) -> bytes:
    """Re-encode conn node entries byte-faithfully: ref -> (0x80|hi, lo); 0x00 after every
    node except the last when the original did not end in 0x00 (`terminated` False)."""
    buf = bytearray()
    for i, refs in enumerate(nodes):
        for v in refs:
            buf += bytes([0x80 | ((v >> 8) & 0x7F), v & 0xFF])
        if i < len(nodes) - 1 or terminated:
            buf += b"\x00"
    return bytes(buf)


# ── chunk2 = IndexBuffer (delta + 8-deep move-to-front nibble codec) ─────────────
# Reverse-engineered from RUSE.exe FUN_140ab45d0 (decode) — the per-index reader the
# game runs `count` times.  Each value is a vertex index, coded as a 4-bit op:
#   nibble 0..7   : move-to-front reference to one of the last 8 distinct values
#   nibble 8..14  : value = last_value + (nibble-8)        (small positive delta 0..6)
#   nibble 15     : escape — read 1 byte B (low-first nibble pair):
#                     B < 0x80  -> delta = B - 0x40        (8-bit, range -64..63)
#                     B >= 0x80 -> read 2nd byte B2; delta = ((B&0x7f)<<8 | B2) - 0x4000
# History (h[0..7]) is seeded 0..7; h[0] always equals the last emitted value.
# Nibbles pack low-first; an escape byte at cursor c reads (N[c]<<4|N[c+1]) if c odd
# else (N[c]|N[c+1]<<4).  The encoder mirrors this exactly (bit7 = "more bytes" marker,
# so the 8-bit escape only covers delta -64..63 even though the decoder accepts B==0x80).
INDEXBUF_SEED = (0, 1, 2, 3, 4, 5, 6, 7)


def _ib_to_nibbles(buf: bytes) -> list:
    N = []
    for b in buf:
        N.append(b & 0xF); N.append(b >> 4)
    return N


def _ib_from_nibbles(N: list) -> bytes:
    if len(N) & 1:
        N = N + [0]
    return bytes(((N[i] & 0xF) | ((N[i + 1] & 0xF) << 4)) for i in range(0, len(N), 2))


def decode_indexbuf(buf: bytes, count: Optional[int] = None, seed=INDEXBUF_SEED):
    """Decode the chunk2 IndexBuffer stream -> (values, nibbles_consumed).
    count=None decodes greedily until the buffer is exhausted (a trailing zero
    nibble may pad to a byte boundary)."""
    N = _ib_to_nibbles(buf)
    ntot = len(N)
    cur = 0
    h = list(seed)
    L = 0
    out = []
    while True:
        if count is not None:
            if len(out) >= count:
                break
        elif cur >= ntot:
            break
        c0 = cur
        n = N[c0]
        if n == 0xF:
            if c0 + 3 > ntot:
                break
            cc = c0 + 1
            b0 = (N[cc] << 4 | N[cc + 1]) if (cc & 1) else (N[cc] | N[cc + 1] << 4)
            if b0 < 0x81:
                cur = c0 + 3
                d = b0 - 0x40
            else:
                if c0 + 5 > ntot:
                    break
                cc2 = c0 + 3
                b1 = (N[cc2] << 4 | N[cc2 + 1]) if (cc2 & 1) else (N[cc2] | N[cc2 + 1] << 4)
                cur = c0 + 5
                d = ((b0 & 0x7F) << 8 | b1) - 0x4000
            v = (L + d) & 0xFFFFFFFF
            h = [v] + h[:7]
        elif n >= 8:
            cur = c0 + 1
            v = (L + (n & 7)) & 0xFFFFFFFF
            h = [v] + h[:7]
        else:
            cur = c0 + 1
            v = h[n]
            if n != 0:
                h = [v] + h[:n] + h[n + 1:]
        L = v
        out.append(v)
    return out, cur


def encode_indexbuf(values: list, seed=INDEXBUF_SEED) -> bytes:
    """Re-encode IndexBuffer values byte-faithfully (inverse of decode_indexbuf).
    Greedy lowest-cost code; verified byte-identical on all 98 Storage KDTs."""
    N = []
    h = list(seed)
    L = 0

    def wr_byte(B):
        c = len(N)
        if c & 1:
            N.append((B >> 4) & 0xF); N.append(B & 0xF)
        else:
            N.append(B & 0xF); N.append((B >> 4) & 0xF)

    for V in values:
        hit = False
        for k in range(8):
            if h[k] == V:
                N.append(k)
                if k != 0:
                    h = [V] + h[:k] + h[k + 1:]
                hit = True
                break
        if hit:
            L = V
            continue
        d = V - L
        if 0 <= d <= 6:
            N.append(8 + d)
            h = [V] + h[:7]
        elif -64 <= d <= 63:
            N.append(0xF); wr_byte((d + 0x40) & 0xFF)
            h = [V] + h[:7]
        else:
            v15 = (d + 0x4000) & 0x7FFF
            N.append(0xF); wr_byte(0x80 | (v15 >> 8)); wr_byte(v15 & 0xFF)
            h = [V] + h[:7]
        L = V
    return _ib_from_nibbles(N)


@dataclass
class KdtFull:
    """Fully-decomposed KDT for byte-identical load/edit/write.

    Each chunk's ORIGINAL content is preserved; write_full re-encodes a chunk from its
    decoded value view ONLY when that view is marked dirty (see mark_dirty / set_vert).
    So read_full -> write_full is byte-identical on every map, while edited sections are
    rebuilt from values.

    Decoded value views (edit, then mark_dirty(name)):
      verts ('verts')   — [(qx,qy,qz)] uint16 quantized (world via world_xy / bbox)
      conn  ('conn')    — [per-node zone-id list] (mechanics: node->zones); None if undecoded
      til   ('til')     — [per-leaf triangle-index list]; None if undecoded
      cs_nodes ('cs')   — CS opcode node list (KD-tree); cs_tail = trailing pad
      indexbuf ('chunk2') — [vertex-index list] (chunk2 IndexBuffer, delta+MTF coded);
                            None if undecoded. Edit + mark_dirty('chunk2') to re-encode.
    Raw-preserved sections: vbi_raw ('vbi'), chunk2_raw, dd_raw ('dd'), conn_raw.
    """
    ndf: object
    bbox_min: Tuple[float, float, float]
    bbox_max: Tuple[float, float, float]
    quant: int
    verts: List[Tuple[int, int, int]]
    conn: Optional[List[list]]
    conn_raw: bytes
    conn_tail: bytes
    conn_terminated: bool
    vbi_raw: bytes
    chunk2_raw: bytes
    til: Optional[List[list]]
    cs_nodes: Optional[list]
    cs_tail: bytes
    dd_raw: bytes
    lead: bytes
    levels: list
    gaps: list
    _orig: list                        # original decompressed content per chunk
    _zlibs: list                       # original zlib bytes per chunk
    mainnode: Optional[list] = None    # 7x (tag u32, val f32): [0:6]=bbox clip planes, [6]=root
    _mn_gap: int = -1                  # which gap holds MainNode
    _mn_off: int = -1                  # MainNode offset within that gap
    indexbuf: Optional[list] = None    # chunk2 IndexBuffer vertex-index list (delta+MTF)
    dirty: set = field(default_factory=set)

    def mark_dirty(self, name: str):
        self.dirty.add(name)

    def set_vert(self, i, qx, qy, qz=None):
        old = self.verts[i]
        self.verts[i] = (qx & 0xFFFF, qy & 0xFFFF, (old[2] if qz is None else qz) & 0xFFFF)
        self.dirty.add("verts")

    def world_xy(self, i):
        bx = self.bbox_max[0] - self.bbox_min[0]
        by = self.bbox_max[1] - self.bbox_min[1]
        qx, qy, _ = self.verts[i]
        return (self.bbox_min[0] + qx / 32767.0 * bx,
                self.bbox_min[1] + qy / 32767.0 * by)


def read_full(kdt_bytes: bytes) -> KdtFull:
    """Parse a .kdt fully into editable value views + raw-preserved sections.
    Value decoding is best-effort: sections it can't decode stay raw (still round-trip)."""
    ndf = ndfbin.read(kdt_bytes)
    inst = ndf.instances[0]
    props = {ndf.properties[pv.prop_index].name: pv for pv in inst.props}
    if "Storage" not in props:
        # LoadableDataChunk variant (e.g. m07 kursk area KDTs) — no Storage blob / chunk2.
        # These round-trip byte-identically via ndfbin.write(ndfbin.read(...)) directly.
        raise ValueError("KDT has no Storage blob (LoadableDataChunk variant); "
                         "round-trip it via ndfbin instead of read_full/write_full")
    bbox_min = tuple(props["BoundingBoxMin"].value.raw)
    bbox_max = tuple(props["BoundingBoxMax"].value.raw)
    blob = bytes(props["Storage"].value.raw)
    lead, chunks = _parse_blob(blob)
    contents = [c["content"] for c in chunks]
    levels = [c["level"] for c in chunks]
    gaps = [c["gap"] for c in chunks]
    zlibs = [c["_zlib"] for c in chunks]

    verts, conn, conn_raw, conn_tail, vbi_raw, chunk2_raw, til, cs_nodes, cs_tail, dd_raw, quant = \
        [], None, b"", b"", b"", b"", None, None, b"", b"", 0x7FFF
    conn_terminated = True
    if len(chunks) == 5:
        c0, c1, c2, c3, c4 = contents
        vbi_raw, chunk2_raw = c1, c2
        try:
            vtx_count = _u32(c0, 0); quant = _u32(c0, 4)
            vend = 8 + vtx_count * 6
            verts = [struct.unpack_from("<HHH", c0, 8 + i * 6) for i in range(vtx_count)]
            dd_start = next((i for i in range(vend, len(c0)) if c0[i] == 0xDD), len(c0))
            conn_raw = c0[vend:dd_start]
            dd_raw = c0[dd_start:]
            try:
                conn, consumed = decode_conn(conn_raw)
                conn_tail = conn_raw[consumed:]
                conn_terminated = consumed > 0 and conn_raw[consumed - 1] == 0x00
            except Exception:
                conn = None
        except Exception:
            verts = []
        try:
            cs_nodes, cs_tail = decode_opcodes(c4)
            til, _u = decode_til(c3, leaf_triangle_counts(cs_nodes))
        except Exception:
            cs_nodes, til = None, None
        try:
            indexbuf, _consumed = decode_indexbuf(c2)
        except Exception:
            indexbuf = None
    else:
        indexbuf = None

    # MainNode (56B = 7x (tag u32, val f32)) lives raw in a gap; entry[6] tag has bit 0x50000000.
    mainnode, mn_gap, mn_off = None, -1, -1
    for gi, g in enumerate(gaps):
        for off in range(0, len(g) - 55):
            if (struct.unpack_from("<I", g, off + 48)[0] & 0x50000000) == 0x50000000:
                mainnode = [struct.unpack_from("<If", g, off + k * 8) for k in range(7)]
                mn_gap, mn_off = gi, off
                break
        if mainnode is not None:
            break

    return KdtFull(
        ndf=ndf, bbox_min=bbox_min, bbox_max=bbox_max, quant=quant,
        verts=verts, conn=conn, conn_raw=conn_raw, conn_tail=conn_tail,
        conn_terminated=conn_terminated, vbi_raw=vbi_raw, chunk2_raw=chunk2_raw,
        til=til, cs_nodes=cs_nodes, cs_tail=cs_tail, dd_raw=dd_raw, lead=lead,
        levels=levels, gaps=gaps, _orig=contents, _zlibs=zlibs,
        mainnode=mainnode, _mn_gap=mn_gap, _mn_off=mn_off, indexbuf=indexbuf,
    )


def decode_mesh(kdt_bytes: bytes):
    """Decode the KDT's triangle mesh OFFLINE for any map (CRACKED 2026-05-24; validated vs Frida).
    The 85/etc verts in d1 are NOT absolute — they're RESIDUALS + back-ref instructions decoded by the
    VtxBufIdx predictor (RE'd from FUN_14018c020/FUN_14027c310):
        d1[8 : 8+N*6] = residual[i] (3x u16);  d1[8+N*6:] = ref instructions
        ref[0]=ref[1]=0; ref[i]=i-delta  (instr byte 0x00->delta 1; else (b0,b1) b0!=0 -> ((b0&0x7f)<<8)|b1)
        out[0]=residual[0]&0x7fff;  out[i]=(out[ref[i]]+residual[i])&0x7fff   (per x,y,z)
    World = bbox_min + q/32767*(bbox_max-bbox_min). Triangles via the IndexBuffer (== runtime, verified).
    Returns {"verts":[(x,y)], "tris":[(a,b,c)], "bbox_min", "bbox_max"} or None."""
    try:
        ndf = ndfbin.read(kdt_bytes)
        props = {ndf.properties[pv.prop_index].name: pv.value for pv in ndf.instances[0].props}
        bmin = tuple(props["BoundingBoxMin"].raw); bmax = tuple(props["BoundingBoxMax"].raw)
        blob = bytes(props["Storage"].raw)
        comp1 = _u32(blob, 0)
        d1 = _decompress_d1(blob[8:8 + comp1])
        n = _u32(d1, 0)
        if n == 0 or 8 + n * 6 > len(d1):
            return None
        res = [struct.unpack_from("<HHH", d1, 8 + i * 6) for i in range(n)]
        instr = d1[8 + n * 6:]
        ref = [0, 0]; p = 0
        for i in range(2, n):
            if p >= len(instr):
                return None
            b0 = instr[p]
            if b0 == 0:
                dlt = 1; p += 1
            else:
                if p + 1 >= len(instr):
                    return None
                dlt = ((b0 & 0x7f) << 8) | instr[p + 1]; p += 2
            ref.append(i - dlt)
        q = []
        for i in range(n):
            if i == 0:
                v = [res[0][c] & 0x7fff for c in range(3)]
            else:
                r = ref[i]
                if not (0 <= r < len(q)):
                    return None
                v = [(q[r][c] + res[i][c]) & 0x7fff for c in range(3)]
            q.append(v)
        bx = bmax[0] - bmin[0]; by = bmax[1] - bmin[1]
        verts = [(bmin[0] + v[0] / 32767.0 * bx, bmin[1] + v[1] / 32767.0 * by) for v in q]
        ib = read_full(kdt_bytes).indexbuf or []
        tris = [(ib[i], ib[i + 1], ib[i + 2]) for i in range(0, len(ib) - 2, 3)
                if max(ib[i], ib[i + 1], ib[i + 2]) < n]
        return {"verts": verts, "tris": tris, "bbox_min": bmin, "bbox_max": bmax}
    except Exception:
        return None


def _kdt_refs(d1: bytes, n: int):
    instr = d1[8 + n * 6:]; ref = [0, 0]; p = 0
    for i in range(2, n):
        if p >= len(instr):
            return None
        b0 = instr[p]
        if b0 == 0:
            dlt = 1; p += 1
        else:
            if p + 1 >= len(instr):
                return None
            dlt = ((b0 & 0x7f) << 8) | instr[p + 1]; p += 2
        ref.append(i - dlt)
    return ref


def encode_mesh(kdt_bytes: bytes, verts_world) -> bytes:
    """Inverse of decode_mesh: write (possibly edited) verts back into a .kdt. verts_world=[(x,y)] (N).
    Re-quantizes x,y (15-bit, /32767), preserves each vertex's z, recomputes the predictor residuals
    (residual[i]=(q[i]-q[ref[i]])&0x7fff; residual[0]=q[0]) keeping the ORIGINAL ref instructions, then
    rebuilds d1 + recompresses. Byte-identical when verts are unchanged (returns the input bytes).
    NOTE: this moves vertices only; it does NOT rebuild the KD-tree partition / MainNode, so large moves
    that push triangles outside their tree cells may break in-game capture queries (small reshapes OK)."""
    ndf = ndfbin.read(kdt_bytes)
    props = {ndf.properties[pv.prop_index].name: pv.value for pv in ndf.instances[0].props}
    bmin = tuple(props["BoundingBoxMin"].raw); bmax = tuple(props["BoundingBoxMax"].raw)
    blob = bytes(props["Storage"].raw)
    # Use the multi-chunk CONTAINER model (same as read_full): chunk0 = d1 (verts), chunks 1-4 the
    # subtree/IndexBuffer/TIL/CS sections. The naive "[comp][raw][d1][rest]" split is off by 4 bytes
    # (stored comp = zlib_len + 4) and silently drops chunk1's length header on re-encode, collapsing
    # the chunk list so the IndexBuffer is lost. Reassembling via _reassemble_blob keeps all 5 chunks.
    lead, chunks = _parse_blob(blob)
    if not chunks:
        raise ValueError("encode_mesh: could not parse Storage chunks")
    d1 = chunks[0]["content"]
    n = _u32(d1, 0)
    if len(verts_world) != n:
        raise ValueError("encode_mesh: expected %d verts, got %d" % (n, len(verts_world)))
    ref = _kdt_refs(d1, n)
    if ref is None:
        raise ValueError("encode_mesh: could not parse ref instructions")
    orig_res = [list(struct.unpack_from("<HHH", d1, 8 + i * 6)) for i in range(n)]
    oq = []
    for i in range(n):
        if i == 0:
            oq.append([orig_res[0][c] & 0x7fff for c in range(3)])
        else:
            pv = oq[ref[i]]; oq.append([(pv[c] + orig_res[i][c]) & 0x7fff for c in range(3)])
    bxr = bmax[0] - bmin[0]; byr = bmax[1] - bmin[1]
    nq = []
    for i, (wx, wy) in enumerate(verts_world):
        qx = _clamp(int(round((wx - bmin[0]) / bxr * 32767.0)), 0, 32767)
        qy = _clamp(int(round((wy - bmin[1]) / byr * 32767.0)), 0, 32767)
        nq.append([qx, qy, oq[i][2]])                     # z preserved
    vtx = bytearray(n * 6)
    for i in range(n):
        if i == 0:
            r = [nq[0][c] & 0x7fff for c in range(3)]
        else:
            r = [(nq[i][c] - nq[ref[i]][c]) & 0x7fff for c in range(3)]
        struct.pack_into("<HHH", vtx, i * 6, *r)
    new_d1 = d1[:8] + bytes(vtx) + d1[8 + n * 6:]
    if new_d1 == d1:
        return kdt_bytes                                  # unchanged -> byte-identical
    chunks[0]["content"] = new_d1
    chunks[0]["_zlib"] = None                             # force recompress of the edited chunk
    new_blob = _reassemble_blob(lead, chunks)
    # A large vert move recompresses chunk0 to a different size, shifting every later chunk/gap in the
    # blob; the OffsetOf* NDF properties are blob byte-offsets, so remap them or the loader crashes.
    pbn = {ndf.properties[pv.prop_index].name: pv for pv in ndf.instances[0].props}
    offs = {nm: int(pbn[nm].value.raw) for nm in pbn if nm.startswith("OffsetOf")}
    remapped = _remap_offsets(blob, new_blob, offs) if offs else None
    if remapped:
        for nm, val in remapped.items():
            pbn[nm].value.raw = val
    if "Storage" in pbn:
        pbn["Storage"].value.raw = bytearray(new_blob)
    return ndfbin.write(ndf)


def build_kdt_mesh_tree(tris, verts_world, bbox_min, bbox_max, max_leaf=6, max_depth=32):
    """Build a fresh ray-tracing KD-tree over the triangles (XY). Returns (cs_nodes, til_lists).
    Standard recursive median-split: choose the longer cell axis, split at the median of triangle
    centroids, and send every triangle OVERLAPPING a child cell to that child (a straddling tri goes to
    BOTH). Stop when a cell has <= max_leaf tris or a split makes no progress. DFS pre-order; per §4 the
    adjacent child (emitted first) is the BELOW/low half, the far child (second) the ABOVE/high half;
    leaf cells therefore overlap every triangle in their TIL (the ray-query correctness invariant)."""
    def axis_minmax(t, a):
        return (min(verts_world[t[0]][a], verts_world[t[1]][a], verts_world[t[2]][a]),
                max(verts_world[t[0]][a], verts_world[t[1]][a], verts_world[t[2]][a]))
    tb = [(axis_minmax(tris[t], 0), axis_minmax(tris[t], 1)) for t in range(len(tris))]  # ((minx,maxx),(miny,maxy))

    def to_int(world, a):
        rng = bbox_max[a] - bbox_min[a]
        return 0 if rng == 0 else _clamp(int(round((world - bbox_min[a]) / rng * 32767.0)), 0, 32767)

    cs_nodes = []; til = []
    def emit_leaf(ids):
        ids = sorted(ids)
        cs_nodes.append(("leaf", max(len(ids), 1), 0))
        til.append(ids if ids else [0])

    def rec(ids, lo, hi, depth):
        if len(ids) <= max_leaf or depth >= max_depth:
            emit_leaf(ids); return
        ax = 0 if (hi[0] - lo[0]) >= (hi[1] - lo[1]) else 1
        cents = sorted(((tb[t][ax][0] + tb[t][ax][1]) * 0.5) for t in ids)
        split = cents[len(cents) // 2]
        eps = (hi[ax] - lo[ax]) * 1e-4
        split = min(max(split, lo[ax] + eps), hi[ax] - eps)
        below = [t for t in ids if tb[t][ax][0] < split]   # overlaps [lo, split]
        above = [t for t in ids if tb[t][ax][1] > split]   # overlaps [split, hi]
        # no progress (a child holds all tris, or a side is empty) -> leaf
        if not below or not above or len(below) >= len(ids) or len(above) >= len(ids):
            # try the other axis once before giving up
            ax2 = 1 - ax
            cents2 = sorted(((tb[t][ax2][0] + tb[t][ax2][1]) * 0.5) for t in ids)
            sp2 = cents2[len(cents2) // 2]
            eps2 = (hi[ax2] - lo[ax2]) * 1e-4
            sp2 = min(max(sp2, lo[ax2] + eps2), hi[ax2] - eps2)
            b2 = [t for t in ids if tb[t][ax2][0] < sp2]
            a2 = [t for t in ids if tb[t][ax2][1] > sp2]
            if b2 and a2 and len(b2) < len(ids) and len(a2) < len(ids):
                ax, split, below, above = ax2, sp2, b2, a2
            else:
                emit_leaf(ids); return
        cs_nodes.append(("int", ax, to_int(split, ax), True, 0))   # two-child split node
        lo2 = list(lo); hi2 = list(hi); hi2[ax] = split; rec(below, lo2, hi2, depth + 1)   # adjacent = below
        lo3 = list(lo); hi3 = list(hi); lo3[ax] = split; rec(above, lo3, hi3, depth + 1)   # far = above
    rec(list(range(len(tris))), [bbox_min[0], bbox_min[1]], [bbox_max[0], bbox_max[1]], 0)
    return cs_nodes, til


def rebuild_kdt_tree(kdt_bytes: bytes, verts_world):
    """v2 FULL rebuild of the KDT for the 'KDT follows sectors' edit (docs/map_editor/ruse_exe_re.md).
    Moves the verts (encode_mesh) AND rebuilds the ray-tracing KD-tree from scratch over the new triangle
    positions (build_kdt_mesh_tree) — required because a stale tree's splits/leaf cells no longer match
    the moved triangles and crash the in-game capture query. Keeps the IndexBuffer (triangles) + VBI; only
    the CS opcode tree (chunk4), the per-leaf TIL (chunk3) and the MainNode root split are regenerated, via
    the proven encoders (write_full). Returns (new_kdt_bytes, ok). ok=False when there is no tree to rebuild
    (area KDTs) -> falls back to verts-only encode_mesh."""
    moved = encode_mesh(kdt_bytes, verts_world)
    kf = read_full(moved)
    if not kf.cs_nodes or not kf.til:
        return moved, False
    n = len(verts_world)
    ib = kf.indexbuf or []
    tris = [(ib[i], ib[i + 1], ib[i + 2]) for i in range(0, len(ib) - 2, 3)
            if max(ib[i], ib[i + 1], ib[i + 2]) < n]
    bmin, bmax = kf.bbox_min, kf.bbox_max
    cs_nodes, til = build_kdt_mesh_tree(tris, verts_world, bmin, bmax)
    kf.cs_nodes = cs_nodes; kf.til = til
    kf.mark_dirty("cs"); kf.mark_dirty("til")
    if kf.mainnode is not None and cs_nodes and cs_nodes[0][0] == "int":
        mn = list(kf.mainnode); t6, _ = mn[6]
        ax = cs_nodes[0][1]
        mn[6] = (t6, bmin[ax] + (cs_nodes[0][2] / 32767.0) * (bmax[ax] - bmin[ax]))
        kf.mainnode = mn; kf.mark_dirty("mainnode")
    return write_full(kf), True


def write_full(kf: KdtFull) -> bytes:
    """Re-encode to .kdt bytes. Clean chunks emit original content (byte-identical);
    dirty value views are rebuilt from values."""
    contents = list(kf._orig)
    if contents and (kf.dirty & {"verts", "conn", "dd"}):
        conn_bytes = (encode_conn_faithful(kf.conn, kf.conn_terminated) + kf.conn_tail
                      if (kf.conn is not None and "conn" in kf.dirty) else kf.conn_raw)
        contents[0] = (_pack_u32(len(kf.verts)) + _pack_u32(kf.quant)
                       + b"".join(struct.pack("<HHH", *v) for v in kf.verts)
                       + conn_bytes + kf.dd_raw)
    if len(contents) == 5:
        if "vbi" in kf.dirty:
            contents[1] = kf.vbi_raw
        if "chunk2" in kf.dirty:
            contents[2] = (encode_indexbuf(kf.indexbuf) if kf.indexbuf is not None
                           else kf.chunk2_raw)
        if "til" in kf.dirty and kf.til is not None:
            contents[3] = encode_til(kf.til)
        if "cs" in kf.dirty and kf.cs_nodes is not None:
            contents[4] = encode_opcodes(kf.cs_nodes, kf.cs_tail)

    gaps = list(kf.gaps)
    if "mainnode" in kf.dirty and kf.mainnode is not None and kf._mn_gap >= 0:
        g = gaps[kf._mn_gap]; o = kf._mn_off
        mn = b"".join(struct.pack("<If", t, v) for t, v in kf.mainnode)
        gaps[kf._mn_gap] = g[:o] + mn + g[o + 56:]
    chunks = []
    for content, orig, level, gap, zb in zip(contents, kf._orig, kf.levels, gaps, kf._zlibs):
        chunks.append({"content": content, "level": level, "gap": gap,
                       "_zlib": zb if content == orig else None})
    blob = _reassemble_blob(kf.lead, chunks)
    # The OffsetOf* NDF properties are byte offsets INTO the blob (they point at chunk headers and
    # raw gap sections). Rebuilding/recompressing chunks shifts those positions, so remap every
    # OffsetOf* from its structural anchor (chunk header / gap + sub-offset) to its new blob position;
    # otherwise the game's loader reads a section from the wrong byte and crashes on load.
    props_by_name = {kf.ndf.properties[pv.prop_index].name: pv for pv in kf.ndf.instances[0].props}
    orig_blob = bytes(props_by_name["Storage"].value.raw) if "Storage" in props_by_name else None
    if orig_blob:
        offs = {n: int(props_by_name[n].value.raw) for n in props_by_name if n.startswith("OffsetOf")}
        remapped = _remap_offsets(orig_blob, blob, offs) if offs else None
        if remapped:
            for n, val in remapped.items():
                props_by_name[n].value.raw = val
    if "Storage" in props_by_name:
        props_by_name["Storage"].value.raw = bytearray(blob)
    return ndfbin.write(kf.ndf)


def transform_kdt(kdt_bytes: bytes, dx: float = 0.0, dy: float = 0.0,
                  scale: float = 1.0, center: Optional[Tuple[float, float]] = None) -> bytes:
    """Rigidly translate/scale the KDT's world zone layout (X/Y), editing ONLY coordinate
    values — the NDF BoundingBox + MainNode X/Y planes & root split.  Every structural section
    (verts/conn/IndexBuffer/TIL/CS) stays byte-identical, because the quantized geometry rides
    the bbox: world = bmin + q/32767*(bmax-bmin).  Proven safe in-game (bbox-shift test).

    Maps world (x,y) -> (cx + scale*(x-cx) + dx, cy + scale*(y-cy) + dy); center defaults to the
    bbox centre.  Z is untouched (zones keep their heights).  Returns new .kdt bytes.
    """
    kf = read_full(kdt_bytes)
    bmin, bmax = list(kf.bbox_min), list(kf.bbox_max)
    cx, cy = center if center is not None else ((bmin[0] + bmax[0]) / 2.0,
                                                (bmin[1] + bmax[1]) / 2.0)

    def tx(x): return cx + scale * (x - cx) + dx
    def ty(y): return cy + scale * (y - cy) + dy

    new_min = (tx(bmin[0]), ty(bmin[1]), bmin[2])
    new_max = (tx(bmax[0]), ty(bmax[1]), bmax[2])
    for pv in kf.ndf.instances[0].props:
        nm = kf.ndf.properties[pv.prop_index].name
        if nm == "BoundingBoxMin":
            pv.value.raw = type(pv.value.raw)(new_min) if isinstance(pv.value.raw, tuple) else list(new_min)
        elif nm == "BoundingBoxMax":
            pv.value.raw = type(pv.value.raw)(new_max) if isinstance(pv.value.raw, tuple) else list(new_max)

    if kf.mainnode is not None:
        mn = list(kf.mainnode)
        # [0]=maxX [1]=minX [2]=maxY [3]=minY [4]=maxZ [5]=minZ [6]=root split
        mn[0] = (mn[0][0], tx(mn[0][1])); mn[1] = (mn[1][0], tx(mn[1][1]))
        mn[2] = (mn[2][0], ty(mn[2][1])); mn[3] = (mn[3][0], ty(mn[3][1]))
        ax = mn[6][0] & 3
        if ax == 0:
            mn[6] = (mn[6][0], tx(mn[6][1]))
        elif ax == 1:
            mn[6] = (mn[6][0], ty(mn[6][1]))
        kf.mainnode = mn
        kf.mark_dirty("mainnode")
    return write_full(kf)


# ── Build ─────────────────────────────────────────────────────────────────────

def build(zones: list, bbox_min: tuple, bbox_max: tuple,
          template_kdt_bytes: bytes) -> KdTree:
    """Build a new KdTree from zone polygon definitions.

    zones: list of dicts, each with:
        "id"    (int)           — KDT zone ID (scenario a0_zone_idx + 2)
        "verts" (list of (x,y)) — polygon vertices in RUSE world units
        "faces" (list of (i0,i1,i2)) — triangle indices into verts
        OR
        "pts"   (list of (x,y)) — polygon vertices (fan-triangulated internally)

    bbox_min, bbox_max: (x, y, z) tuples for the full map bounding box.

    template_kdt_bytes: NDF bytes of the existing KDT for THIS MAP.  Its chunks
        2-4 (pre-computed tree indices) are copied verbatim into the new KDT and
        drive runtime zone detection until a full chunk-format decoder is written.

    Returns a KdTree ready to be written with write().
    """
    import math

    # ── Parse template ─────────────────────────────────────────────────────────
    tmpl     = read(template_kdt_bytes)
    tmpl_ndf = ndfbin.read(template_kdt_bytes)   # fresh copy for mutation
    tmpl_inst = tmpl_ndf.instances[0]
    tmpl_pv   = {}
    for pv in tmpl_inst.props:
        tmpl_pv[tmpl_ndf.properties[pv.prop_index].name] = pv

    tmpl_blob  = bytes(tmpl_pv['Storage'].value.raw)
    tmpl_c1    = _u32(tmpl_blob, 0)
    tmpl_d1    = _decompress_d1(tmpl_blob[8:8 + tmpl_c1])
    tmpl_vtx   = _u32(tmpl_d1, 0)
    tmpl_ve    = 8 + tmpl_vtx * 6
    tmpl_dd    = next((i for i in range(tmpl_ve, len(tmpl_d1)) if tmpl_d1[i] == 0xDD), len(tmpl_d1))
    T          = tmpl.offsets
    T_tri      = tmpl.tri_count

    # Template section sizes (used for proportional scaling)
    T_TIL    = T['TriangleIndexBufferIndexes'] - T['TriangleIndexLists']
    T_TIBI   = T['MainNode'] - T['TriangleIndexBufferIndexes']
    T_CS     = len(tmpl_d1) - T['CompressedSubtrees']

    # ── Collect triangles ──────────────────────────────────────────────────────
    tris: List[tuple] = []
    for zone in zones:
        zid = int(zone['id'])
        if 'faces' in zone and 'verts' in zone:
            verts = [(float(v[0]), float(v[1])) for v in zone['verts']]
            for i0, i1, i2 in zone['faces']:
                tris.append((zid, verts[i0], verts[i1], verts[i2]))
        elif 'pts' in zone:
            pts = [(float(p[0]), float(p[1])) for p in zone['pts']]
            for i in range(1, len(pts) - 1):
                tris.append((zid, pts[0], pts[i], pts[i + 1]))
        else:
            raise ValueError(f"Zone {zid}: need 'pts' or ('verts' + 'faces')")

    if not tris:
        raise ValueError('No triangles generated from zone definitions')
    tri_count = len(tris)

    # ── Build KD-tree ──────────────────────────────────────────────────────────
    max_depth = max(1, math.floor(math.log2(max(2, tri_count))))
    nodes = _build_kdt_nodes(tris, bbox_min, bbox_max, max_depth)
    N     = len(nodes)
    N_half = N // 2

    # ── Collect unique vertices ────────────────────────────────────────────────
    vtx_map:  Dict[tuple, int] = {}
    vtx_list: List[tuple]      = []

    def _get_vtx(xy: tuple) -> int:
        key = (round(xy[0] * 10) / 10.0, round(xy[1] * 10) / 10.0)
        if key not in vtx_map:
            vtx_map[key] = len(vtx_list)
            vtx_list.append(key)
        return vtx_map[key]

    for _, v0, v1, v2 in tris:
        _get_vtx(v0); _get_vtx(v1); _get_vtx(v2)

    vtx_count = len(vtx_list)

    # ── Quantize vertex buffer (qz=0; Z axis unused for zone detection) ────────
    bx_r = bbox_max[0] - bbox_min[0]
    by_r = bbox_max[1] - bbox_min[1]
    if bx_r == 0.0 or by_r == 0.0:
        raise ValueError('bbox X and Y ranges must be non-zero')

    vtx_raw = bytearray(vtx_count * 6)
    for i, (wx, wy) in enumerate(vtx_list):
        qx = _clamp(int(round(((wx - bbox_min[0]) / bx_r) * 32767.0)), 0, 32767)
        qy = _clamp(int(round(((wy - bbox_min[1]) / by_r) * 32767.0)), 0, 32767)
        struct.pack_into('<HHH', vtx_raw, i * 6, qx, qy, 0)

    vtx_end = 8 + vtx_count * 6

    # ── Encode conn stream ─────────────────────────────────────────────────────
    conn    = _encode_conn(nodes)
    conn_sz = len(conn)
    dd_start = vtx_end + conn_sz

    # ── Compute OffsetOf* values ───────────────────────────────────────────────
    # oVBI: points into conn stream at first zone_id byte of node N_half's entry.
    # For all observed maps: internal_conn_sz + 1 (to skip the 0x80 prefix byte).
    internal_sz  = sum(1 + 2 * len(nodes[i]) for i in range(N_half))
    n_half_zones = nodes[N_half] if N_half < N else []
    oVBI = vtx_end + internal_sz + (1 if n_half_zones else 0)
    oIB  = oVBI + (2 * len(n_half_zones) if n_half_zones else 1)

    # Remaining sections sit in the 0xDD region immediately after conn stream.
    oIBI  = dd_start        # IBI = 4B constant
    oTIL  = oIBI + 4
    scale = max(vtx_count / max(1, tmpl_vtx), tri_count / max(1, T_tri))
    new_TIL  = max(64, round(T_TIL  * scale * 2))   # generous factor for safety
    new_TIBI = max(4,  T_TIBI)
    new_CS   = max(64, round(T_CS   * scale * 2))
    oTIBI = oTIL  + new_TIL
    oMN   = oTIBI + new_TIBI
    oCSIB = oMN   + 56      # MN = 56B constant
    oCS   = oCSIB + 4       # CSIB = 4B constant
    d1_end = max(oCS + new_CS, oVBI + vtx_count * 4 + 64)

    # ── Build d1 ───────────────────────────────────────────────────────────────
    dd_size = d1_end - dd_start
    d1 = (
        _pack_u32(vtx_count)
        + _pack_u32(0x7FFF)
        + bytes(vtx_raw)
        + conn
        + b'\xDD' * dd_size
    )

    # ── Build new chunk[1]: vtx_count × 4B of sentinel 0x7FFDFFF5 ─────────────
    sentinel    = bytes([0xF5, 0xFF, 0xFD, 0x7F])
    c1_data     = sentinel * vtx_count
    c1_comp     = zlib.compress(c1_data, 9)
    chunk1_raw  = _pack_u32(len(c1_comp)) + _pack_u32(len(c1_data)) + c1_comp

    # ── Copy template chunks 2-4 verbatim ─────────────────────────────────────
    # Scan the full NDF file bytes: chunk[0]=d1, chunk[1]=VBI, chunks[2-4]=tree indices.
    # subtrees_raw alone cannot be scanned — chunk[1]'s header straddles the
    # d1_comp/subtrees_raw boundary, and chunk[4] may extend into NDF trailing bytes.
    tmpl_raw_chunks = _find_raw_chunks(template_kdt_bytes)
    if len(tmpl_raw_chunks) < 5:
        raise ValueError(
            f'Template NDF has only {len(tmpl_raw_chunks)} zlib chunks; expected 5')
    new_subtrees_raw = chunk1_raw + b''.join(tmpl_raw_chunks[2:5])

    # ── Compress d1 and build Storage blob ────────────────────────────────────
    d1_comp = zlib.compress(d1, 9)
    blob    = (
        _pack_u32(len(d1_comp))
        + _pack_u32(len(d1))
        + d1_comp
        + new_subtrees_raw
    )

    # ── Update NDF properties in the fresh copy ────────────────────────────────
    updates = {
        'BoundingBoxMin':                       tuple(bbox_min),
        'BoundingBoxMax':                       tuple(bbox_max),
        'TriangleCount':                        tri_count,
        'SubtreeCount':                         1,
        'OffsetOfVertexBufferIndexes':          oVBI,
        'OffsetOfIndexBuffer':                  oIB,
        'OffsetOfIndexBufferIndexes':           oIBI,
        'OffsetOfTriangleIndexLists':           oTIL,
        'OffsetOfTriangleIndexBufferIndexes':   oTIBI,
        'OffsetOfMainNode':                     oMN,
        'OffsetOfCompressedSubtreeIndexBuffer': oCSIB,
        'OffsetOfCompressedSubtrees':           oCS,
        'Storage':                              bytearray(blob),
    }
    for pv in tmpl_inst.props:
        name = tmpl_ndf.properties[pv.prop_index].name
        if name in updates:
            pv.value.raw = updates[name]

    # ── Return KdTree object ───────────────────────────────────────────────────
    d1_tail = conn + b'\xDD' * dd_size

    return KdTree(
        bbox_min       = tuple(bbox_min),
        bbox_max       = tuple(bbox_max),
        tri_count      = tri_count,
        subtree_count  = 1,
        vertices       = list(vtx_list),
        _vtx_raw       = bytes(vtx_raw),
        _d1_tail       = d1_tail,
        _subtrees_raw  = new_subtrees_raw,
        _ndf           = tmpl_ndf,
        offsets        = {
            'VertexBufferIndexes':          oVBI,
            'IndexBuffer':                  oIB,
            'IndexBufferIndexes':           oIBI,
            'TriangleIndexLists':           oTIL,
            'TriangleIndexBufferIndexes':   oTIBI,
            'MainNode':                     oMN,
            'CompressedSubtreeIndexBuffer': oCSIB,
            'CompressedSubtrees':           oCS,
        },
    )

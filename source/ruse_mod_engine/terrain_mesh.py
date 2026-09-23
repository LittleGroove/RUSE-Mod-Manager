"""RUSE terrain MESH decode -- ``highdef.tms`` / ``lowdef.tms`` (the ``TMSG`` container).

This is the geometry half of the terrain: the actual triangle mesh the game renders, with per-vertex
positions and normals.  It is a different file from ``highdef.tmst_pc`` / ``.tmst_chunk_pc``, which
carry the terrain *texture* (see ``terrain_codec``).

Container
---------
Three related formats share one header validator, selected by magic: ``TMSG`` (this mesh),
``TMST`` (the tile index) and ``TRMP`` (not present in shipped maps).  All require version 3, the
platform tag ``"PC\\0\\0"``, and the magic repeated as the final 4 bytes of the file::

    file header, 16 bytes
      +0x00 u32 magic   +0x04 u32 version = 3   +0x08 char[4] "PC\\0\\0"   +0x0C u32 total size

    descriptor, 856 bytes, at file offset 16
      +0x00 u32 gridW   +0x04 u32 gridH   +0x08 u32 8
      +0x0C f32 cell world size X         +0x10 f32 cell world size Y
      +0x14/+0x18  (offset, length) CELL TABLE     -- gridW*gridH records of 48 bytes
      +0x1C/+0x20  (offset, length) PER-CELL BLOB  -- 1280 or 2048 bytes per cell
      +0x24/+0x28  (offset, length) GEOMETRY POOL  -- the vertex and index blocks
      +0x2C        NUL-terminated vertex type string

Offsets are from the start of the FILE and the three sections chain end-to-end from 872 (16 + 856).
Cells are row-major with X fastest.  Every shipped map uses the vertex type
``TVertex__PositionIn4w_4w__NormalIn01_4ubn`` -- stride 12, two elements.

    cell record, 48 bytes
      +0x00 u8  flags   bit0 -> IB0 present, bit1 -> IB1 present, bit2 -> blob is 2048 not 1280
      +0x04 u32 VB  offset   +0x08 u32 VB  byte size   +0x0C u32 VB  VERTEX COUNT
      +0x10 u32 IB0 offset   +0x14 u32 IB0 byte size   +0x18 u32 IB0 INDEX COUNT
      +0x1C u32 IB1 offset   +0x20 u32 IB1 byte size   +0x24 u32 IB1 INDEX COUNT
      +0x28 u32 offset into the per-cell blob section

Index buffers
-------------
``u32 uncompressed byte size`` then a deflate stream.  The streams are Z_SYNC_FLUSH terminated (they
end ``00 00 ff ff``, no final block and no adler32), so ``zlib.decompress`` reports a truncated
stream and any trailing byte gives "invalid stored block lengths".  Feed a ``decompressobj``
*exactly* the block's bytes and then ``flush()``.

The u16 that come out are NOT vertex numbers -- they are signed deltas against the previous index,
which is why values like 0xfffe appear in a buffer addressing only a few hundred vertices.  A
cumulative sum recovers a plain triangle list.

Vertex buffers -- the ``VBUF`` container
----------------------------------------
::

    +0x00 "VBUF"   +0x04 u16 header length (8)   +0x06 u8 ?
    +0x07 u16 vertex stride   +0x09 u16 element count   +0x0B u8 flag (2 -> predictor present)
    then, if the flag is set: u32 length + a compressed sub-stream (the shared PREDICTOR)
    then per element: a header, then u32 length + a compressed sub-stream
    every sub-stream is padded to a 4-byte boundary

    element header
      +0x00 u32 ?      +0x04 u16 header length (the header spans [4, 4+len))
      +0x06 u8  data type   +0x07 u8 codec   +0x08 u8 mode (0 direct, 2 predicted)
      +0x09 u16 format      +0x0B u16 BYTE OFFSET of this element inside the vertex
      +0x0D u16 sub-record count, then that many 24-byte sub-records
    data types: 0 float1, 1 float2, 2 float3, 3 float4, 4 ubyte4, 6 word2, 8 word4

Every sub-stream is ``+0x00 u8 version(1) | +0x01 u8 header size(0x14) | +0x02 u8 method |
+0x03 u8 shift base | +0x04 u32 size | +0x08 u32 literal count | +0x0C u32 match count |
+0x10 u16 literal base | +0x12 u16 token base``, then the control words at +0x14.  The two base
fields are scaled by ``shift = base + 2``.  Bit 7 of the method byte means STORED.

There are two decompressor families and the method byte picks between them: methods below 16 are
BYTE granular and the size field counts bytes; method 16 and above are U16 granular and the size
field counts 16-bit units, so the output is twice as long.  Otherwise the two are the same LZ.
Shipped ``.tms`` files only ever use 8 and 16.

The LZ itself reads a stream of 32-bit control words; each bit is one operation, consumed from the
low end.  A 0 bit starts a literal run whose length is the number of consecutive 0 bits (capped so
the run never exceeds 16 bytes), copied from the literal region.  A 1 bit is a back-reference read
from the token region, in one of four widths::

    (t & 3) == 3, bit2 clear : 2 bytes  len = (t>>3)&0x00f  + 4   dist = (t>>7 )&0x01ff + 1
    (t & 3) == 3, bit2 set   : 3 bytes  len = (t>>3)&0x0ff  + 4   dist = (t>>11)&0x1fff + 1
    (t & 3) != 3, bit2 clear : 2 bytes  len = (t&3) + 1           dist = (t>>3)&0x1fff  + 1
    (t & 3) != 3, bit2 set   : 1 byte   len = (t&3) + 1           dist = (t>>3)&0x001f  + 1

Lengths and distances are in UNITS (bytes or u16 per the method).  Both copy paths deliberately
overrun, so the input is padded before decoding.

Predicted elements (mode 2)
---------------------------
The shared predictor sub-stream decodes to one back-reference per vertex -- the earlier vertex this
one is predicted from.  Vertices 0 and 1 reference vertex 0; from vertex 2 on, a leading 0 byte
means "the previous vertex", otherwise two bytes give a 15-bit distance ``((b0 & 0x7f) << 8) | b1``
subtracted from the current index.

Each element is then a per-component delta against its predicted vertex, wrapped to the component
width::

    out[i][k] = (src[i][k] + out[parent[i]][k]) & mask

with ``mask`` 0xff for ubyte4.  word4 carries its own mask as a u16 at the head of its stream (its
data starts 4 bytes in), and afterwards each component is scaled up by ``1 << (16 - floor(log2(
mask + 1)))`` to fill the 16-bit range.

What the decoded terrain vertex means
-------------------------------------
The word4 element at vertex offset 0 is the position, and it is GLOBAL to the map rather than local
to the cell:

    component 0   map X, quantised so the whole map spans 0..32768 whatever its aspect ratio
    component 1   map Y, likewise
    component 2   ELEVATION, 0..32767
    component 3   not identified

so cell (cx, cy) covers ``[cx * 32768/gridW, (cx+1) * 32768/gridW]`` horizontally.  Adjacent cells
therefore restate their shared edge, and they agree: across all 64 shipped meshes, 142023 boundary
vertices decoded from completely independent per-cell streams match, 141942 of them exactly (see
``tools/test_scripts/probe_tms_watertight.py``).  Multiply by ``cellWorldSize * grid / 32768`` to
get world units on each axis; the elevation's world scale is not in this file.

Component 3 is genuinely per-vertex on most cells (241 of 380 sampled carry more than one value)
but correlates only weakly with elevation (r ~ 0.25) and is flat on cells where elevation is not,
so it is NOT a second/morph height.  Its meaning is still open -- do not read it as elevation.

The ubyte4 element at vertex offset 8 is the normal, each component 0..255 mapping to -1..1, so
flat ground reads (128, 128, 255).  Normals are computed per cell, so a shared edge vertex can
round differently on each side.

Decoded here without numpy so the module stays importable everywhere; callers that want arrays can
wrap the returned buffers.
"""
import struct
import zlib

CELL_REC = 48
DESCRIPTOR = 0x358
SECTION_START = 872          # 16-byte file header + 856-byte descriptor

# element data types
T_FLOAT1, T_FLOAT2, T_FLOAT3, T_FLOAT4, T_UBYTE4, T_WORD2, T_WORD4 = 0, 1, 2, 3, 4, 6, 8


def inflate_block(buf):
    """Inflate a Z_SYNC_FLUSH-terminated deflate stream. `buf` must be EXACTLY the block."""
    o = zlib.decompressobj()
    return o.decompress(buf) + o.flush()


def lz_decompress(src):
    """Decode one VBUF sub-stream. Returns the decoded bytes.

    `src` is the whole sub-stream including its 0x14-byte header.
    """
    if len(src) < 0x14:
        raise ValueError("sub-stream too short: %d bytes" % len(src))
    method = src[2] & 0x3F
    unit = 2 if method >= 16 else 1
    n_units = struct.unpack_from("<I", src, 4)[0]
    out_bytes = n_units * unit
    if src[2] & 0x80:                                    # STORED: payload is a plain copy
        return bytes(src[8:8 + out_bytes])
    shift = (src[3] + 2) & 0x1F
    lit_base = struct.unpack_from("<H", src, 0x10)[0] << shift
    tok_base = struct.unpack_from("<H", src, 0x12)[0] << shift
    src = bytes(src) + b"\0" * 0x80                      # both copy paths overrun
    out = bytearray(out_bytes + 64)
    cap = 0x10000 if unit == 1 else 0x100                # literal run caps at 16 bytes
    lit = 0                                              # literal cursor, in units
    tok = tok_base                                       # token cursor, in bytes
    op = 0                                               # output cursor, in units
    cp = 0x14
    while op < n_units:
        ctrl = struct.unpack_from("<I", src, cp)[0]
        cp += 4
        bits = 32
        while True:
            if not (ctrl & 1):
                v = ctrl | cap
                n = 0
                while not (v & 1):
                    v >>= 1
                    n += 1
                if bits <= n:
                    n = bits
                s = lit_base + lit * unit
                out[op * unit:(op + n) * unit] = src[s:s + n * unit]
                op += n
                lit += n
                bits -= n
                used = n
            else:
                t = struct.unpack_from("<I", src, tok)[0]
                if (t & 3) == 3:
                    if not (t & 4):
                        ln = ((t >> 3) & 0x0F) + 4
                        dist = ((t >> 7) & 0x01FF) + 1
                        tok += 2
                    else:
                        ln = ((t >> 3) & 0xFF) + 4
                        dist = ((t >> 11) & 0x1FFF) + 1
                        tok += 3
                else:
                    ln = (t & 3) + 1
                    if not (t & 4):
                        dist = ((t >> 3) & 0x1FFF) + 1
                        tok += 2
                    else:
                        dist = ((t >> 3) & 0x1F) + 1
                        tok += 1
                if dist > op:
                    raise ValueError("back-reference %d before start of output at %d" % (dist, op))
                a, d = op * unit, dist * unit
                if d >= ln * unit:                       # no overlap: one slice
                    out[a:a + ln * unit] = out[a - d:a - d + ln * unit]
                else:
                    for i in range(ln * unit):
                        out[a + i] = out[a + i - d]
                op += ln
                bits -= 1
                used = 1
            ctrl >>= (used & 0x1F)
            if bits == 0 or op >= n_units:
                break
    return bytes(out[:out_bytes])


def predictor_parents(stream, count):
    """Decode the shared predictor sub-stream into one parent index per vertex."""
    parents = [0, 0]
    p = 0
    for i in range(2, count):
        b = stream[p]
        if b == 0:
            p += 1
            parents.append(i - 1)
        else:
            parents.append(i - (((b & 0x7F) << 8) | stream[p + 1]))
            p += 2
    return parents[:count] if count >= 2 else parents[:max(count, 0)]


def _undelta_ubyte4(src, parents, count):
    """ubyte4: 4 bytes per vertex, each component a delta against the predicted vertex."""
    out = bytearray(count * 4)
    for i in range(count):
        a, s, b = i * 4, i * 4, parents[i] * 4
        out[a] = (src[s] + out[b]) & 0xFF
        out[a + 1] = (src[s + 1] + out[b + 1]) & 0xFF
        out[a + 2] = (src[s + 2] + out[b + 2]) & 0xFF
        out[a + 3] = (src[s + 3] + out[b + 3]) & 0xFF
    return out


def _undelta_word4(src, parents, count):
    """word4: a u16 mask, then 4 u16 per vertex; rescaled to fill 16 bits afterwards."""
    mask = struct.unpack_from("<H", src, 0)[0]
    vals = list(struct.unpack_from("<%dH" % (count * 4), src, 4))
    out = [0] * (count * 4)
    for i in range(count):
        a, b = i * 4, parents[i] * 4
        out[a] = (vals[a] + out[b]) & mask
        out[a + 1] = (vals[a + 1] + out[b + 1]) & mask
        out[a + 2] = (vals[a + 2] + out[b + 2]) & mask
        out[a + 3] = (vals[a + 3] + out[b + 3]) & mask
    bitlen = (mask + 1).bit_length() - 1
    scale = 1 << ((16 - bitlen) & 0x1F)
    if scale != 1:
        out = [(v * scale) & 0xFFFF for v in out]
    return out, mask, scale


class TerrainMesh(object):
    """A decoded ``TMSG`` terrain mesh."""

    def __init__(self, blob):
        self.d = blob
        magic, ver, plat, size = struct.unpack_from("<4sI4sI", blob, 0)
        if magic != b"TMSG" or ver != 3 or plat[:2] != b"PC":
            raise ValueError("not a v3 PC TMSG: %r ver=%d plat=%r" % (magic, ver, plat))
        self.total = size
        self.footer_ok = blob[-4:] == b"TMSG"
        h = blob[16:16 + DESCRIPTOR]
        u = lambda o: struct.unpack_from("<I", h, o)[0]
        f = lambda o: struct.unpack_from("<f", h, o)[0]
        self.grid_w, self.grid_h = u(0x00), u(0x04)
        self.cell_size = (f(0x0C), f(0x10))
        self.sec_cells = (u(0x14), u(0x18))
        self.sec_blob = (u(0x1C), u(0x20))
        self.sec_geom = (u(0x24), u(0x28))
        self.vertex_type = h[0x2C:].split(b"\x00", 1)[0].decode("latin-1")
        self.geom = blob[self.sec_geom[0]:self.sec_geom[0] + self.sec_geom[1]]
        self.blob = blob[self.sec_blob[0]:self.sec_blob[0] + self.sec_blob[1]]
        self.cells = self._cells()

    def _cells(self):
        off, ln = self.sec_cells
        out = []
        for i in range(ln // CELL_REC):
            r = self.d[off + i * CELL_REC:off + (i + 1) * CELL_REC]
            flags = r[0]
            vo, vs, vc, i0o, i0s, i0c, i1o, i1s, i1c, bo = struct.unpack_from("<10I", r, 4)
            out.append(dict(index=i, x=i % self.grid_w, y=i // self.grid_w, flags=flags,
                            vb=(vo, vs, vc), ib0=(i0o, i0s, i0c), ib1=(i1o, i1s, i1c),
                            blob_off=bo, blob_size=2048 if (flags >> 2) & 1 else 1280))
        return out

    def cell_blob(self, cell):
        o, n = cell["blob_off"], cell["blob_size"]
        return self.blob[o:o + n]

    def index_codes(self, cell, which=0):
        """The cell's index buffer as raw u16 codes, straight out of the deflate stream."""
        o, sz, cnt = cell["ib0"] if which == 0 else cell["ib1"]
        if sz == 0:
            return []
        want = struct.unpack_from("<I", self.geom, o)[0]
        raw = inflate_block(self.geom[o + 4:o + sz])
        if len(raw) != want or want != cnt * 2:
            raise ValueError("cell %d ib%d: %d bytes, prefix %d, count*2 %d"
                             % (cell["index"], which, len(raw), want, cnt * 2))
        return list(struct.unpack("<%dH" % cnt, raw))

    def indices(self, cell, which=0):
        """The cell's triangle-list indices.

        The stored u16 are not vertex numbers -- they are SIGNED DELTAS against the previous
        index, which is why raw values like 0xfffe show up in buffers of a few hundred vertices.
        Running them as a cumulative sum lands every index inside [0, vertexCount).
        """
        nv = cell["vb"][2]
        out = []
        cur = 0
        for code in self.index_codes(cell, which):
            cur += code - 0x10000 if code > 0x7FFF else code
            if not 0 <= cur < nv:
                raise ValueError("cell %d ib%d: index %d outside [0,%d)"
                                 % (cell["index"], which, cur, nv))
            out.append(cur)
        return out

    def triangles(self, cell, which=0):
        """The cell's triangles as (i0, i1, i2) vertex-index triples."""
        idx = self.indices(cell, which)
        return [tuple(idx[i:i + 3]) for i in range(0, len(idx) - 2, 3)]

    def vb_header(self, cell):
        o, sz, cnt = cell["vb"]
        b = self.geom[o:o + sz]
        if b[:4] != b"VBUF":
            raise ValueError("cell %d: VB magic %r" % (cell["index"], b[:4]))
        return dict(magic=b[:4], header_len=struct.unpack_from("<H", b, 4)[0], unk6=b[6],
                    stride=struct.unpack_from("<H", b, 7)[0],
                    elements=struct.unpack_from("<H", b, 9)[0],
                    flag=b[11], vertex_count=cnt, block_size=sz)

    def vb_walk(self, cell):
        """Split a cell's VBUF into (predictor_substream, [(element_header, sub_stream), ...])."""
        o, sz, _vc = cell["vb"]
        b = self.geom[o:o + sz]
        if b[:4] != b"VBUF":
            raise ValueError("cell %d: VB magic %r" % (cell["index"], b[:4]))
        p = 4 + struct.unpack_from("<H", b, 4)[0]
        n_elements = struct.unpack_from("<H", b, 9)[0]
        pred, els = None, []

        def take():
            nonlocal p
            ln = struct.unpack_from("<I", b, p)[0]
            p += 4
            s = b[p:p + ln]
            p += (ln + 3) // 4 * 4
            return s

        if b[11]:
            pred = take()
        for _ in range(n_elements):
            hlen = struct.unpack_from("<H", b, p + 4)[0]
            hdr = b[p:p + 4 + hlen]
            p = p + 4 + hlen
            els.append((hdr, take()))
        return pred, els

    @staticmethod
    def element_header(hdr):
        typ, codec, mode = hdr[6], hdr[7], hdr[8]
        fmt, voff, nsub = struct.unpack_from("<HHH", hdr, 9)
        return dict(type=typ, codec=codec, mode=mode, format=fmt, offset=voff, sub_count=nsub)

    def vertices(self, cell):
        """Decode a cell's vertex buffer.

        Returns a dict with the raw per-element component lists, keyed by the element's byte
        offset inside the vertex, plus the metadata needed to interpret them.
        """
        count = cell["vb"][2]
        pred, els = self.vb_walk(cell)
        if count == 0:
            return dict(count=0, stride=self.vb_header(cell)["stride"], elements={})
        parents = predictor_parents(lz_decompress(pred), count) if pred else [0] * count
        out = {}
        for hdr, stream in els:
            e = self.element_header(hdr)
            raw = lz_decompress(stream)
            if e["mode"] != 2:
                out[e["offset"]] = dict(e, data=raw, decoded=False)
                continue
            if e["type"] == T_UBYTE4:
                want = count * 4
                if len(raw) != want:
                    raise ValueError("cell %d ubyte4: %d bytes, expected %d"
                                     % (cell["index"], len(raw), want))
                out[e["offset"]] = dict(e, data=_undelta_ubyte4(raw, parents, count),
                                        components=4, decoded=True)
            elif e["type"] == T_WORD4:
                want = count * 8 + 4
                if len(raw) != want:
                    raise ValueError("cell %d word4: %d bytes, expected %d"
                                     % (cell["index"], len(raw), want))
                vals, mask, scale = _undelta_word4(raw, parents, count)
                out[e["offset"]] = dict(e, data=vals, components=4, mask=mask, scale=scale,
                                        decoded=True)
            else:
                out[e["offset"]] = dict(e, data=raw, decoded=False)
        return dict(count=count, stride=self.vb_header(cell)["stride"], elements=out,
                    parents=parents)

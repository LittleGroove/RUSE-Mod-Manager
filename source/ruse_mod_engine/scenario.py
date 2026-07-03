"""
Read/write RUSE .scenario files (public version format).

Public version (190852) differences from compat wgrd_cons_parsers/scenario.py:
  - Offset 40: uint32 zone_count directly (no outer AREA wrapper)
  - area7: count(u32) + count*u32 (variable-length; compat had Const(0,u32))
  - GDP0 section absent in simple multiplayer scenarios

Usage:
    from ruse_mod_engine import scenario
    scen = scenario.read(data_bytes)
    ndf  = scen["ndf_data"]          # extract NDF binary for editing
    scen["ndf_data"] = new_ndf_bytes  # replace NDF
    out  = scenario.write(scen)      # serialise back with correct MD5
"""
import hashlib, io, struct


# ---------------------------------------------------------------------------
# Internal binary reader
# ---------------------------------------------------------------------------

class _Reader:
    def __init__(self, data: bytes):
        self.buf = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        r = self.buf[self.pos:self.pos + n]
        self.pos += n
        return r

    def u16(self) -> int:
        return struct.unpack_from("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack_from("<I", self.read(4))[0]

    def f32(self) -> float:
        return struct.unpack_from("<f", self.read(4))[0]

    def expect(self, b: bytes):
        got = self.read(len(b))
        if got != b:
            raise ValueError("Expected %r got %r at pos %d" % (b, got, self.pos - len(b)))

    def align4(self):
        r = self.pos % 4
        if r:
            self.pos += (4 - r)


# ---------------------------------------------------------------------------
# Zone parse
# ---------------------------------------------------------------------------

def _parse_zone(r: _Reader) -> dict:
    zone = {}

    # area0: name
    r.expect(b'AREA')
    zone["a0_x0"]       = r.u32()
    zone["a0_zone_idx"] = r.u32()
    name_len            = r.u32()
    zone["name"]        = r.read(name_len).decode("utf-8", errors="replace")
    r.align4()

    # area1: marker position
    r.expect(b'AREA')
    zone["pos"] = (r.f32(), r.f32(), r.f32())

    # area2: subparts
    r.expect(b'AREA')
    n = r.u32()
    zone["subparts"] = [(r.u32(), r.u32(), r.u32(), r.u32()) for _ in range(n)]

    # area3: border faces (triIdx, triCnt, vtxIdx, vtxCnt)
    r.expect(b'AREA')
    zone["border_faces"] = (r.u32(), r.u32(), r.u32(), r.u32())

    # area4: border vertex range (offset, count)
    r.expect(b'AREA')
    zone["border_vtx"] = (r.u32(), r.u32())

    # area5: vertex buffer (vtx_count * 5 floats)
    r.expect(b'AREA')
    vtx_count  = r.u32()
    face_count = r.u32()
    zone["vertices"]   = [(r.f32(), r.f32(), r.f32(), r.f32(), r.f32()) for _ in range(vtx_count)]
    zone["face_count"] = face_count

    # area6: face index buffer (face_count * 3 uint32)
    r.expect(b'AREA')
    zone["faces"] = [(r.u32(), r.u32(), r.u32()) for _ in range(face_count)]

    # area7: variable-length index list (count + count*u32)
    r.expect(b'AREA')
    n = r.u32()
    zone["area7"] = [r.u32() for _ in range(n)]

    # area8: bare magic
    r.expect(b'AREA')

    # end0
    r.expect(b'END0')

    return zone


# ---------------------------------------------------------------------------
# Zone serialise
# ---------------------------------------------------------------------------

def _write_zone(out: io.BytesIO, zone: dict):
    w = out.write
    pk = struct.pack

    # area0: name
    name_bytes = zone["name"].encode("utf-8")
    name_len   = len(name_bytes)
    pad        = (4 - (name_len % 4)) % 4
    w(b'AREA')
    w(pk('<I', zone["a0_x0"]))
    w(pk('<I', zone["a0_zone_idx"]))
    w(pk('<I', name_len))
    w(name_bytes)
    w(b'\x00' * pad)

    # area1: position
    w(b'AREA')
    for v in zone["pos"]:
        w(pk('<f', v))

    # area2: subparts
    w(b'AREA')
    w(pk('<I', len(zone["subparts"])))
    for sp in zone["subparts"]:
        for v in sp:
            w(pk('<I', v))

    # area3: border faces
    w(b'AREA')
    for v in zone["border_faces"]:
        w(pk('<I', v))

    # area4: border vertex range
    w(b'AREA')
    for v in zone["border_vtx"]:
        w(pk('<I', v))

    # area5: vertex buffer
    vtxs  = zone["vertices"]
    faces = zone["faces"]
    w(b'AREA')
    w(pk('<I', len(vtxs)))
    w(pk('<I', len(faces)))
    for vtx in vtxs:
        for v in vtx:
            w(pk('<f', v))

    # area6: face index buffer
    w(b'AREA')
    for face in faces:
        for v in face:
            w(pk('<I', v))

    # area7: variable-length index list
    a7 = zone["area7"]
    w(b'AREA')
    w(pk('<I', len(a7)))
    for v in a7:
        w(pk('<I', v))

    # area8: bare magic
    w(b'AREA')

    # end0
    w(b'END0')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read(data: bytes) -> dict:
    """Parse a .scenario binary.

    Returns dict:
        header   — {"unk_hdr": bytes(16), "unk0a": int, "unk1": int, "unk2": int}
        zones    — list of zone dicts (see _parse_zone)
        ndf_data — raw NDF bytes (EUG0+CNDF), ready for ndfbin.read()
    """
    r = _Reader(data)

    r.expect(b'SCENARIO\r\n')
    unk_hdr = r.read(16)
    unk0a   = r.u16()
    unk1    = r.u32()
    unk2    = r.u32()
    _ndf_off = r.u32()   # stored offset; recalculated on write

    zone_count = r.u32()
    zones = [_parse_zone(r) for _ in range(zone_count)]

    ndf_size = r.u32()
    ndf_data = r.read(ndf_size)

    return {
        "header":   {"unk_hdr": unk_hdr, "unk0a": unk0a, "unk1": unk1, "unk2": unk2},
        "zones":    zones,
        "ndf_data": ndf_data,
    }


def write(scenario: dict) -> bytes:
    """Serialise a scenario dict to bytes with recomputed MD5 checksum.

    The MD5 at bytes 10-25 covers bytes 0-9 + bytes 28-EOF (same as ScenarioClose.py).
    unk0a (bytes 26-27) is preserved from the parsed header.
    """
    h   = scenario["header"]
    buf = io.BytesIO()
    w   = buf.write
    pk  = struct.pack

    # Fixed header up to ndf_off placeholder
    w(b'SCENARIO\r\n')
    w(b'\x00' * 16)                   # MD5 placeholder — filled below
    w(pk('<H', h["unk0a"]))
    w(pk('<I', h["unk1"]))
    w(pk('<I', h["unk2"]))
    ndf_off_pos = buf.tell()
    w(b'\x00' * 4)                    # ndf_off placeholder — filled below

    # Zone data
    w(pk('<I', len(scenario["zones"])))
    for zone in scenario["zones"]:
        _write_zone(buf, zone)

    # NDF section
    ndf_start = buf.tell()
    ndf_data  = scenario["ndf_data"]
    w(pk('<I', len(ndf_data)))
    w(ndf_data)

    # Patch ndf_off (offset from byte 40 to start of NDF section)
    ndf_off = ndf_start - 40
    cur = buf.tell()
    buf.seek(ndf_off_pos)
    w(pk('<I', ndf_off))
    buf.seek(cur)

    data = bytearray(buf.getvalue())

    # Recompute MD5: covers bytes 0-9 ("SCENARIO\r\n") + bytes 28-EOF
    # (identical to ScenarioClose.py splicing logic)
    md5_input = bytes(data[:10]) + bytes(data[28:])
    md5       = hashlib.md5(md5_input).digest()
    data[10:26] = md5

    return bytes(data)

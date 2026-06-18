"""Edit the forest/concealment SDB quadtree in mapinfo.win (buffer4) — the PRE-GENERATED spatial DB the
game point-queries for forest cover (GetIsEnForet) and other AI-terrain layers. Concealment = layer bit
0x08. RE'd from parser FUN_1405613c0 / query FUN_140a6b7c0 / validator FUN_140a71f90 (the StateDB md5).

Buffer4 SDB layout:
  'SDB\r\n'(5) + md5(16) + u32 ver(=2) + u32 payloadlen + payload(payloadlen bytes) + trailing
  payload = 28-byte header (u32 u32 f32 bboxX f32 bboxY f32 cell u32 u32) + u32 quadtree entries.
  md5 = md5(b'SDB\r\n' + ver(4) + payloadlen(4) + payload)   [no salt]   -- VALIDATED by the game.
  entry V: (V&1)->LEAF (4 quadrant occupancy bytes); else INTERNAL, 4-child block at (V-0x1c)>>2.
  quadtree over [0,0]..[bboxX,bboxY], entry0=root; quadrant order BL,BR,TL,TR.

mapinfo.win: 'INFOIA\r\n'(8) + md5(16) + u32 u32 + bbox(16) = 48-byte header, then 4 length-prefixed
  buffers (buf3=SDB), then a trailing section (preserved verbatim).
  md5 = md5(b'INFOIA\r\n' + b'Eugen Systems' + file[24:])   [salted]   -- VALIDATED by the game.

decode->edit->build->encode round-trips BYTE-IDENTICAL on every sample map (full per-cell byte preserved,
DFS serialization, uniform-quadrant leaf merge)."""
import struct
import hashlib

FOREST_BIT = 0x08          # concealment (en_foret)
BLOCKED_BIT = 0x04         # ground-impassable / water (unit no-go)
_PAYLOAD_OFF = 29          # payload starts here (after 5+16+4+4 prefix)
_HEADER28 = 28             # 28-byte header at start of payload; entries follow at 57


def is_sdb(buf) -> bool:
    return bool(buf) and len(buf) >= 57 and buf[:5] == b"SDB\r\n"


def decode_grid(sdb: bytes):
    """SDB buffer -> {'grid': bytearray(R*R) of per-cell layer bytes, 'R', 'bboxX', 'bboxY', 'cell',
    'orig': sdb}. grid[y*R + x] is the full occupancy byte (all layers + flags) of finest cell (x,y);
    y increases with world Y (row-major, quadrant order BL,BR,TL,TR)."""
    payloadlen = struct.unpack_from("<I", sdb, 25)[0]
    bboxX = struct.unpack_from("<f", sdb, 37)[0]
    bboxY = struct.unpack_from("<f", sdb, 41)[0]
    cell = struct.unpack_from("<f", sdb, 45)[0]
    n = (payloadlen - 0x1C) >> 2
    ents = [struct.unpack_from("<I", sdb, 57 + 4 * i)[0] for i in range(n)]

    md = [0]
    def depth(i, d):
        if not (0 <= i < n) or d > 22:
            return
        V = ents[i]
        if V & 1:
            md[0] = max(md[0], d + 1)
        else:
            ci = (V - 0x1C) >> 2
            for q in range(4):
                depth(ci + q, d + 1)
    depth(0, 0)
    L = md[0]
    R = 1 << L
    grid = bytearray(R * R)

    def fill(i, cx, cy, csz):
        if not (0 <= i < n):
            return
        V = ents[i]; h = csz >> 1
        qp = ((cx, cy), (cx + h, cy), (cx, cy + h), (cx + h, cy + h))
        if V & 1:
            for q in range(4):
                byte = (V >> (q * 8)) & 0xFF
                qx, qy = qp[q]
                for yy in range(qy, qy + h):
                    row = yy * R
                    for xx in range(qx, qx + h):
                        grid[row + xx] = byte
        else:
            ci = (V - 0x1C) >> 2
            for q in range(4):
                qx, qy = qp[q]
                fill(ci + q, qx, qy, h)
    fill(0, 0, 0, R)
    return {"grid": grid, "R": R, "bboxX": bboxX, "bboxY": bboxY, "cell": cell, "orig": sdb}


def build_ents(grid, R):
    """Region quadtree over grid -> u32 entry list (DFS layout, leaf-merge of uniform quadrants).
    Matches the game's exact serialization (byte-identical round-trip on sample maps)."""
    def uniform(cx, cy, sz):
        v = grid[cy * R + cx]
        for yy in range(cy, cy + sz):
            row = yy * R
            for xx in range(cx, cx + sz):
                if grid[row + xx] != v:
                    return None
        return v
    ents = [None]
    def fill(idx, cx, cy, sz):
        h = sz >> 1
        qp = ((cx, cy), (cx + h, cy), (cx, cy + h), (cx + h, cy + h))
        if sz == 2:
            b = [grid[qy * R + qx] for (qx, qy) in qp]
        else:
            us = [uniform(qx, qy, h) for (qx, qy) in qp]
            if all(u is not None for u in us):
                b = us
            else:
                block = len(ents)
                ents[idx] = (block << 2) + 0x1C
                ents.extend([None, None, None, None])
                for q, (qx, qy) in enumerate(qp):
                    fill(block + q, qx, qy, h)
                return
        # bit0 of V is the LEAF FLAG (= bit0 of quadrant-0's byte). Force it so a painted cell whose
        # byte has bit0=0 doesn't make the node decode as internal. Eugen's leaves already set it, so
        # this stays byte-identical on a no-op edit.
        ents[idx] = (b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)) | 1
    fill(0, 0, 0, R)
    return ents


def encode(orig_sdb: bytes, ents) -> bytes:
    """Rebuild an SDB buffer from entries: preserve the 28-byte header + trailing, recompute payloadlen
    and the (validated) md5."""
    header28 = orig_sdb[_PAYLOAD_OFF:_PAYLOAD_OFF + _HEADER28]
    payload = header28 + b"".join(struct.pack("<I", e) for e in ents)
    payloadlen = len(payload)
    ver = orig_sdb[21:25]
    md5 = hashlib.md5(b"SDB\r\n" + ver + struct.pack("<I", payloadlen) + payload).digest()
    orig_pl = struct.unpack_from("<I", orig_sdb, 25)[0]
    trailing = orig_sdb[_PAYLOAD_OFF + orig_pl:]
    return b"SDB\r\n" + md5 + ver + struct.pack("<I", payloadlen) + payload + trailing


def encode_from_grid(orig_sdb: bytes, grid, R) -> bytes:
    return encode(orig_sdb, build_ents(grid, R))


# ── mapinfo.win repack ──────────────────────────────────────────────────────────
def split_mapinfo(win: bytes):
    """-> (header48, [buf0,buf1,buf2,buf3], trailing) or None."""
    if not win or win[:8] != b"INFOIA\r\n" or len(win) < 56:
        return None
    bufs, p = [], 48
    for _ in range(4):
        ln = struct.unpack_from("<I", win, p)[0]; p += 4
        if p + ln > len(win):
            return None
        bufs.append(win[p:p + ln]); p += ln
    return win[:48], bufs, win[p:]


def replace_buffer4(win: bytes, new_sdb: bytes) -> bytes:
    """Replace mapinfo.win buffer4 (the SDB) with new_sdb, recompute the (salted) outer md5."""
    parts = split_mapinfo(win)
    if not parts:
        raise ValueError("not a mapinfo.win")
    header48, bufs, trailing = parts
    bufs = list(bufs); bufs[3] = new_sdb
    body = b"".join(struct.pack("<I", len(b)) + b for b in bufs) + trailing
    out = bytearray(header48 + body)
    md5 = hashlib.md5(b"INFOIA\r\n" + b"Eugen Systems" + bytes(out[24:])).digest()
    out[8:24] = md5
    return bytes(out)


# ── high-level edit helpers ─────────────────────────────────────────────────────
def grid_to_cells(grid, R, bboxX, bboxY, bit=FOREST_BIT):
    """Merge finest cells with `bit` set into horizontal-run world rects (x0,y0,x1,y1) for overlay
    drawing. Far fewer rects than per-cell."""
    cw = bboxX / R; ch = bboxY / R
    cells = []
    for yy in range(R):
        row = yy * R; x = 0; y0 = yy * ch; y1 = (yy + 1) * ch
        while x < R:
            if grid[row + x] & bit:
                x0 = x
                while x < R and (grid[row + x] & bit):
                    x += 1
                cells.append((x0 * cw, y0, x * cw, y1))
            else:
                x += 1
    return cells


# ── per-layer bitmask helpers (for surgical, composable SDB rmod patches) ─────────
# Data layers are the cell-byte bits EXCEPT bit0 (0x01 = the quadtree leaf flag, structural).
DATA_LAYER_BITS = (0x02, FOREST_BIT, BLOCKED_BIT, 0x10, 0x20, 0x40, 0x80)  # 0x04, 0x08 + the rest


def pack_layer_mask(grid, R, bit) -> bytes:
    """Bitmask of cells where `bit` is set: LSB-first, cell index i → byte i//8, bit i%8."""
    n = R * R
    out = bytearray((n + 7) >> 3)
    for i in range(n):
        if grid[i] & bit:
            out[i >> 3] |= (1 << (i & 7))
    return bytes(out)


def apply_layer_mask(grid, R, bit, mask) -> int:
    """Set `bit` where mask has a 1 and clear it where 0, for every cell — leaving all OTHER layer
    bits untouched.  Returns the number of cells changed."""
    n = R * R
    changed = 0
    for i in range(n):
        on = (mask[i >> 3] >> (i & 7)) & 1
        nb = (grid[i] | bit) if on else (grid[i] & ~bit)
        if nb != grid[i]:
            grid[i] = nb
            changed += 1
    return changed


def world_to_cell(x, y, R, bboxX, bboxY):
    cx = int(x / bboxX * R); cy = int(y / bboxY * R)
    return max(0, min(R - 1, cx)), max(0, min(R - 1, cy))


def paint_circle(grid, R, bboxX, bboxY, wx, wy, world_radius, bit=FOREST_BIT, erase=False):
    """Set (or clear) `bit` for every finest cell whose center is within world_radius of (wx,wy).
    Returns the number of cells changed."""
    cxr = max(1, int(world_radius / bboxX * R))
    cyr = max(1, int(world_radius / bboxY * R))
    ccx, ccy = world_to_cell(wx, wy, R, bboxX, bboxY)
    cellw = bboxX / R; cellh = bboxY / R
    changed = 0
    for yy in range(max(0, ccy - cyr), min(R, ccy + cyr + 1)):
        wyy = (yy + 0.5) * cellh
        for xx in range(max(0, ccx - cxr), min(R, ccx + cxr + 1)):
            wxx = (xx + 0.5) * cellw
            if (wxx - wx) ** 2 + (wyy - wy) ** 2 > world_radius * world_radius:
                continue
            i = yy * R + xx; b = grid[i]
            nb = (b & ~bit) if erase else (b | bit)
            if nb != b:
                grid[i] = nb; changed += 1
    return changed

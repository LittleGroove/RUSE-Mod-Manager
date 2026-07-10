"""Edit the forest/concealment SDB quadtree in mapinfo.win (buffer4) — the PRE-GENERATED spatial DB the
game point-queries for forest cover (GetIsEnForet) and other AI-terrain layers. Concealment = layer bit
0x08. Reverse-engineered from the SDB parse/query/validate format (the StateDB md5).

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
    # Guard against a crafted mapinfo whose quadtree nests to the depth cap: L can reach 23, so R*R would
    # be (1<<23)² ≈ 70 TB → an un-catchable MemoryError / machine-hang.  A real terrain grid is tiny by
    # comparison (well under 1<<13 = 8192 per side); anything larger is malformed, so refuse it (the caller
    # falls back to a raw file patch).  This is a hard safety cap, not a format limit.
    if L > 13:
        raise ValueError(f"SDB grid depth {L} implausible (grid {1 << L}×{1 << L}) — refusing to allocate")
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


# ═════════════════════════════════════════════════════════════════════════════════
# Unified SDB codec — handles BOTH mapinfo buffer4 AND the terrain dat's output.sdb.
#
# Both files are the SAME quadtree encoding (confirmed vs 32 maps, issue #9):
#   container = 'SDB\r\n'(5) + md5(16) + u32 ver + u32 body_size, then a body.
#   body = prefix (28-byte sub-header + u32 root-pointer, =0x20 bytes) + a flat array of
#          16-byte NODES (4× u32 children) starting at the root pointer + optional trailing.
#   child V: (V&1) -> LEAF carrying 4 sub-cell occupancy bytes; else BRANCH, V = byte
#            offset (within body) of the child node. The root node is the first node.
#   The node array starts at body 0x20 for BOTH files (the legacy buffer4 "0x1C entry model"
#   just folds a single root-pointer entry into the prefix; output.sdb's "storage_flag" at
#   0x1C IS that same root pointer = 0x20). node_start is read from the root pointer.
#   md5 = md5('SDB\r\n' + ver + body_size + body[:body_size])  [matched 32/32 — ProLution's
#   body-only md5 is WRONG and would write game-rejected files].
#
# Edits are IN PLACE (change leaf values / subdivide a leaf into a child node appended at
# the end), preserving the original node order/offsets -> BYTE-IDENTICAL on a no-op for
# both files. A merge-rebuild does NOT round-trip output.sdb (the game does not maximally
# merge), which is why this is edit-in-place rather than rebuild-from-grid.
#
# NB (runtime audit, issue #9): the game's SDB parser is called from ONE site — the mapinfo.win
# loader (buffer4). The terrain dat's output.sdb is NEVER parsed (build artifact). And the runtime
# query builder is only ever called with mask 0x08 (forest/conceal) and 0x04 (blocked/clear-path) —
# the other bits are never queried, so only these two layers do anything in-game.
# ═════════════════════════════════════════════════════════════════════════════════
_NODE = 16

# The ONLY two runtime-meaningful SDB layers (confirmed query masks). bit 0x08 = forest cover
# (GetIsEnForet); bit 0x04 = blocked / clear-path. Other bits exist in the data but are never queried.
LAYERS_BUFFER4 = (("Forest / concealment (0x08)", 0x08), ("Blocked / clear-path (0x04)", 0x04))


def _tree_valid(nodes, ns, body_size) -> bool:
    cnt = len(nodes)
    for node in nodes:
        for V in node:
            if V & 1:
                continue
            if V < ns or V >= body_size or (V - ns) % _NODE:
                return False
    seen = set(); stack = [ns]
    while stack:
        off = stack.pop()
        if off in seen:
            continue
        idx = (off - ns) // _NODE
        if not (0 <= idx < cnt):
            return False
        seen.add(off)
        for V in nodes[idx]:
            if not (V & 1):
                stack.append(V)
    return len(seen) == cnt


def parse(data: bytes, node_start=None):
    """Parse any SDB (buffer4 or output.sdb) -> dict {ver, prefix, node_start, nodes, tail}.
    `nodes` is a list of [c0,c1,c2,c3] in file order; serialize(parse(x)) == x byte-for-byte.
    `node_start` (the byte offset of the root/first node within the body) is read from the root
    pointer at body 0x1C when None — that is 0x20 for every real map; 0x20/0x1C are tried as
    fallbacks. Pass it explicitly only for exotic files."""
    if not is_sdb(data):
        raise ValueError("not an SDB")
    ver = struct.unpack_from("<I", data, 21)[0]
    body_size = struct.unpack_from("<I", data, 25)[0]
    body = data[29:]
    if body_size > len(body) or body_size < 0x20:
        raise ValueError("SDB body too small / body_size exceeds data")
    if node_start is not None:
        cands = [node_start]
    else:
        root_ptr = struct.unpack_from("<I", body, 0x1C)[0]
        cands = [root_ptr, 0x20, 0x1C]
    seen = set()
    for ns in cands:
        if ns in seen or ns < 0x1C or body_size < ns or (body_size - ns) % _NODE:
            continue
        seen.add(ns)
        cnt = (body_size - ns) // _NODE
        nodes = [list(struct.unpack_from("<IIII", body, ns + i * _NODE)) for i in range(cnt)]
        if _tree_valid(nodes, ns, body_size):
            return {"ver": ver, "prefix": body[:ns], "node_start": ns, "nodes": nodes,
                    "tail": body[body_size:]}
    raise ValueError("SDB node array did not validate (tried %s)" % [hex(c) for c in cands])


def serialize(p) -> bytes:
    """Serialize a parsed SDB back to bytes with the correct (game-validated) md5."""
    node_bytes = b"".join(struct.pack("<IIII", *(c & 0xFFFFFFFF for c in n)) for n in p["nodes"])
    payload = p["prefix"] + node_bytes
    body_size = len(payload)
    md5 = hashlib.md5(b"SDB\r\n" + struct.pack("<I", p["ver"]) + struct.pack("<I", body_size) + payload).digest()
    return b"SDB\r\n" + md5 + struct.pack("<I", p["ver"]) + struct.pack("<I", body_size) + payload + p["tail"]




def tree_grid_R(p) -> int:
    # Finest grid = 1<<(deepest LEAF depth + 1): the +1 resolves each leaf's 2x2 sub-cells. (Counting
    # only branch depth under-samples by one level and drops real sub-cell detail — matches legacy
    # decode_grid's R exactly when leaf depth is used.)
    nodes = p["nodes"]; ns = p["node_start"]
    def depth(off, d):
        idx = (off - ns) // _NODE
        # Depth cap + index bounds: a malformed/cyclic node graph could otherwise recurse forever or index
        # past the node list.  A real terrain quadtree is shallow, so d>20 (or a bad pointer) = malformed.
        if d > 20 or not (0 <= idx < len(nodes)):
            return d
        m = d
        for V in nodes[idx]:
            if V & 1:
                m = max(m, d + 1)
            else:
                m = max(m, depth(V, d + 1))
        return m
    L = depth(ns, 0) + 1
    # Hard safety cap: R = 1<<L, and to_grid allocates R*R.  1<<14 = 16384 per side is already far beyond
    # any real map; anything larger is a crafted/corrupt tree, so refuse (caller ships raw).
    if L > 14:
        raise ValueError(f"SDB tree depth {L} implausible (grid {1 << L}×{1 << L}) — refusing to allocate")
    return 1 << L


def to_grid(p):
    """Decode the quadtree -> (bytearray grid of R*R per-cell occupancy bytes, R). grid[y*R+x] is
    the finest-cell byte; quadrant order is 0=(x,y) 1=(x+h,y) 2=(x,y+h) 3=(x+h,y+h) (image space).
    Uses numpy slice-fill when available (much faster at native resolution); pure-Python fallback."""
    nodes = p["nodes"]; ns = p["node_start"]; R = tree_grid_R(p)
    try:
        import numpy as np
        g = np.zeros((R, R), np.uint8); use_np = True
    except Exception:
        g = bytearray(R * R); use_np = False
    stack = [(ns, 0, 0, R)]
    # Bound the walk: a valid tree visits each node once, so a cyclic / over-visiting (malformed) tree
    # trips this and stops with a partial grid instead of looping forever (an un-catchable hang).
    max_pops = len(nodes) * 4 + 16
    pops = 0
    while stack:
        pops += 1
        if pops > max_pops:
            break
        off, x0, y0, sz = stack.pop()
        idx = (off - ns) // _NODE
        # A branch can't subdivide below a single cell, and a pointer must land inside the node list;
        # either would be malformed — skip it rather than descend into a runaway/OOB read.
        if sz < 2 or not (0 <= idx < len(nodes)):
            continue
        node = nodes[idx]; h = sz >> 1; hh = h >> 1
        quad = ((x0, y0), (x0 + h, y0), (x0, y0 + h), (x0 + h, y0 + h))
        for q in range(4):
            V = node[q]; qx, qy = quad[q]
            if V & 1:
                sub = ((qx, qy), (qx + hh, qy), (qx, qy + hh), (qx + hh, qy + hh))
                fz = max(hh, 1)
                for s in range(4):
                    b = (V >> (s * 8)) & 0xFF
                    sx, sy = sub[s]
                    if use_np:
                        g[sy:sy + fz, sx:sx + fz] = b
                    else:
                        for yy in range(sy, min(sy + fz, R)):
                            row = yy * R
                            for xx in range(sx, min(sx + fz, R)):
                                g[row + xx] = b
            else:
                stack.append((V, qx, qy, h))
    return (bytearray(g.tobytes()), R) if use_np else (g, R)


def apply_layer_masks_inplace(p, layer_specs) -> None:
    """FAST rmod apply: set each layer to a target state by EDITING THE TREE IN PLACE, instead of
    decode→apply→rebuild. `layer_specs` = [(bit, mask_bytes, R_mask)] where mask_bytes is an LSB-first
    bitmap (the rmod's stored target for that bit) at resolution R_mask. We decode the tree's grid once,
    diff each layer's target against the current state, and paint only the changed cells (paint_tree).
    Requires numpy (raise → caller falls back to the legacy decode_grid/encode_from_grid path)."""
    import numpy as np
    grid, R = to_grid(p)
    cur = np.frombuffer(bytes(grid), np.uint8).reshape(R, R)
    for bit, mask_bytes, R_mask in layer_specs:
        bits = np.unpackbits(np.frombuffer(mask_bytes, np.uint8), bitorder="little")
        need = R_mask * R_mask
        if bits.size < need:
            bits = np.concatenate([bits, np.zeros(need - bits.size, np.uint8)])
        target = bits[:need].reshape(R_mask, R_mask).astype(bool)
        if R_mask != R:                                  # legacy masks are at 2× native; downsample
            if R_mask > R and R_mask % R == 0:
                target = target[:: R_mask // R, :: R_mask // R]
            else:
                from PIL import Image
                target = np.asarray(Image.fromarray(target.view(np.uint8) * 255)
                                    .resize((R, R), Image.NEAREST)) > 127
        have = (cur & bit) > 0
        set_cells = target & ~have
        clr_cells = (~target) & have
        if set_cells.any():
            paint_tree(p, set_cells, R, bit, True)
        if clr_cells.any():
            paint_tree(p, clr_cells, R, bit, False)


def paint_tree(p, mask, R, bit, value: bool) -> None:
    """Edit-in-place: set (value=True) or clear `bit` on every cell where `mask` (a flat length-R*R
    truthy sequence, or numpy 2-D RxR array) is set. Subdivides leaves only where the mask edge
    crosses them; leaves all untouched nodes byte-identical. Requires numpy (map-editor dependency)."""
    import numpy as np
    m = np.asarray(mask)
    if m.ndim == 1:
        m = m.reshape(R, R)
    m = m.astype(bool)
    sat = np.zeros((R + 1, R + 1), np.int64)
    sat[1:, 1:] = np.cumsum(np.cumsum(m.astype(np.int64), 0), 1)
    def count(x, y, s):
        x2, y2 = min(x + s, R), min(y + s, R); x, y = max(x, 0), max(y, 0)
        if x2 <= x or y2 <= y:
            return 0
        return int(sat[y2, x2] - sat[y, x2] - sat[y2, x] + sat[y, x])
    if value:
        setb = lambda b: ((b | bit) | 1) & 0xFF
    else:
        setb = lambda b: ((b & ~bit) | 1) & 0xFF
    def split(V):
        return [((((V >> (q * 8)) & 0xFF) * 0x01010101) | 0x01010101) & 0xFFFFFFFF for q in range(4)]
    nodes = p["nodes"]; ns = p["node_start"]
    def rec(off, x0, y0, sz):
        node = nodes[(off - ns) // _NODE]; h = sz >> 1
        quad = ((x0, y0), (x0 + h, y0), (x0, y0 + h), (x0 + h, y0 + h))
        for q in range(4):
            V = node[q]; qx, qy = quad[q]
            if count(qx, qy, h) == 0:
                continue
            if V & 1:
                hh = h >> 1
                sb = [(V >> (s * 8)) & 0xFF for s in range(4)]
                if hh < 1:
                    node[q] = (setb(sb[0]) | setb(sb[1]) << 8 | setb(sb[2]) << 16 | setb(sb[3]) << 24)
                    continue
                sub = ((qx, qy), (qx + hh, qy), (qx, qy + hh), (qx + hh, qy + hh)); need = False
                for s in range(4):
                    c = count(sub[s][0], sub[s][1], hh)
                    if c == 0:
                        continue
                    if c == hh * hh:
                        sb[s] = setb(sb[s])
                    else:
                        need = True; break
                if not need:
                    node[q] = (sb[0] | sb[1] << 8 | sb[2] << 16 | sb[3] << 24) | 1
                else:
                    noff = ns + len(nodes) * _NODE
                    nodes.append(split(V)); node[q] = noff
                    rec(noff, qx, qy, h)
            else:
                rec(V, qx, qy, h)
    rec(ns, 0, 0, R)

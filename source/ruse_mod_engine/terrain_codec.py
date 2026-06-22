"""RUSE high-def terrain decode — ``highdef.tmst_pc`` index + ``highdef.tmst_chunk_pc`` chunks.

The ``.tmst_chunk_pc`` is a homebrew JPEG-like lossy RGB texture codec (Eugen ~2010): per 128×128
tile = a TGU1/zlib chunk holding two YCbCr DXT1 endpoints (EP0/EP1) + a selector bank, each stored as
Adaptive-Rice/Exp-Golomb-coded 4×4-DCT coefficients.  Decode = un-Rice → un-zigzag → dequant → IDCT
→ DXT1-endpoint interpolate → BT.601 YCbCr→RGB.

Reverse-engineered and released CC0 by ProLution (RUSE Modding Database); validated against the real
game files in Issue #8 (the decode's roads/town/water match the baked minimap).  See
memory/project_terrain_tmst_codec.md.  This is the decode half, adapted to take BYTES (not file
paths) so the editor can feed it data extracted from a DataMap dat via edata.

Known limitation: the DXT1 selector scale is a RUNTIME value not stored in the file, so colour is
auto-scaled (washed-out/bright) — structurally accurate, colour approximate.

Requires numpy + Pillow.  Callers should guard on availability.
"""
import struct
import zlib

import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

# ── constants (from Ruse.exe, per ProLution) ────────────────────────────────
ZIGZAG_ORDER = [0, 1, 4, 8, 5, 2, 3, 6, 9, 12, 13, 10, 7, 11, 14, 15]
QUANT_TABLE = np.array([18, 14, 18, 49, 12, 16, 37, 78, 24, 57, 104, 121,
                        51, 69, 103, 100], dtype=np.float64).reshape(4, 4)
IDCT_BASIS = np.array([20, 20, 20, 20, 26, 11, -11, -26, 20, -20, -20, 20,
                       11, -26, 26, -11], dtype=np.float64).reshape(4, 4)
IDCT_BT = IDCT_BASIS.T
NORM_FACTOR = 7200.0
BIAS_Y, BIAS_CBCR = 16.0, 128.0
SCALE_Y = 1.164382815361023
COEFF_CB_G, COEFF_CB_B = 0.3917617201805115, 2.0172343254089355
COEFF_CR_R, COEFF_CR_G = 1.5999336242675781, 0.8129687309265137
TILE_SIZE, BLOCKS_PER_TILE, BLOCK_COLS = 128, 1024, 32


class _BitReader:
    """Bits LSB-first within little-endian uint32 words (matches Ruse.exe BitStream)."""
    __slots__ = ("words", "pos")

    def __init__(self, data, offset, nbytes):
        nwords = nbytes // 4
        self.words = struct.unpack_from("<%dI" % nwords, data, offset) if nwords else ()
        self.pos = 0

    def read_unary(self):
        count = 0
        while True:
            wi, bi = self.pos >> 5, self.pos & 31
            if wi >= len(self.words):
                return count
            w = self.words[wi] >> bi
            if w == 0:
                count += 32 - bi
                self.pos += 32 - bi
                continue
            t = (w & (-w)).bit_length() - 1
            count += t
            self.pos += t + 1
            return count

    def read_bits(self, n):
        if n == 0:
            return 0
        wi, bi = self.pos >> 5, self.pos & 31
        if wi >= len(self.words):
            self.pos += n
            return 0
        val = self.words[wi] >> bi
        if 32 - bi < n and wi + 1 < len(self.words):
            val |= self.words[wi + 1] << (32 - bi)
        val &= (1 << n) - 1
        self.pos += n
        return val


def _zigzag_decode(val):
    return (val >> 1) ^ -(val & 1)


def _rice_decode_value(reader, flags):
    k = flags - 8 if flags >= 8 else flags
    if flags >= 8:                       # Rice
        q = reader.read_unary()
        r = reader.read_bits(k) if k else 0
        val = (q << k) | r
    else:                                # Exp-Golomb
        m = reader.read_unary()
        rem = reader.read_bits(m + k) if m + k else 0
        val = (1 << (m + k)) + rem - (1 << k)
    return _zigzag_decode(val)


def _decode_macro_bank(body, off, n_blocks=BLOCKS_PER_TILE):
    """Decode one 128×128 channel → (pixels 128×128, raw 4×4 blocks, new offset)."""
    descs = []
    for _ in range(17):
        d = struct.unpack_from("<I", body, off)[0]
        off += 4
        descs.append((d >> 28, d & 0x0FFFFFFF))
    subs = []
    for fl, ct in descs:
        db = ((ct + 31) // 32) * 4
        subs.append((fl, ct, off, db))
        off += db

    fl0, ct0, o0, d0 = subs[0]
    r = _BitReader(body, o0, d0) if ct0 and d0 else None
    raw_counts = [_rice_decode_value(r, fl0) for _ in range(n_blocks)] if r else [0] * n_blocks
    nz_counts, running = [], 0
    for v in raw_counts:
        running += v
        nz_counts.append(max(0, running))

    sb_vals = {}
    for j in range(1, 17):
        n_need = sum(1 for c in nz_counts if c >= j)
        fj, cj, oj, dj = subs[j]
        if n_need > 0 and cj > 0 and dj > 0:
            r = _BitReader(body, oj, dj)
            sb_vals[j] = [_rice_decode_value(r, fj) for _ in range(n_need)]
        else:
            sb_vals[j] = []

    coeffs = np.zeros((n_blocks, 4, 4), dtype=np.float64)
    si = {j: 0 for j in range(1, 17)}
    dc_pred = 0
    for b in range(n_blocks):
        nc = nz_counts[b]
        flat = np.zeros(16)
        for j in range(1, min(nc + 1, 17)):
            if si[j] < len(sb_vals[j]):
                flat[ZIGZAG_ORDER[j - 1]] = sb_vals[j][si[j]]
                si[j] += 1
        flat[0] += dc_pred
        dc_pred = flat[0]
        coeffs[b] = flat.reshape(4, 4) * QUANT_TABLE

    blocks = np.einsum("ij,bjk,kl->bil", IDCT_BT, coeffs, IDCT_BASIS) / NORM_FACTOR
    pixels = np.zeros((TILE_SIZE, TILE_SIZE))
    for b in range(n_blocks):
        bx, by = (b % BLOCK_COLS) * 4, (b // BLOCK_COLS) * 4
        pixels[by:by + 4, bx:bx + 4] = blocks[b]
    return pixels, blocks, off


def _ycbcr_to_rgb(y, cb, cr):
    yf = (y - BIAS_Y) * SCALE_Y
    r = yf + COEFF_CR_R * (cr - BIAS_CBCR)
    g = yf - COEFF_CR_G * (cr - BIAS_CBCR) - COEFF_CB_G * (cb - BIAS_CBCR)
    b = yf + COEFF_CB_B * (cb - BIAS_CBCR)
    return np.clip(np.stack([r, g, b], axis=-1) + 0.5, 0, 255).astype(np.uint8)


def decode_tile(body, use_index=True):
    """A decompressed TGU1 body → 128×128×3 uint8 RGB ndarray."""
    off = 4
    channels = []
    for _ in range(6):
        pixels, _, off = _decode_macro_bank(body, off)
        channels.append(pixels)

    if use_index:
        aux_count = struct.unpack_from("<I", body, off)[0]
        off += 4 + aux_count * 4
        _, idx_blocks, off = _decode_macro_bank(body, off)
        all_vals = idx_blocks.flatten()
        vmin, vmax = all_vals.min(), all_vals.max()
        idx_norm = ((idx_blocks - vmin) / (vmax - vmin) * 3.0) if vmax > vmin \
            else np.full_like(idx_blocks, 1.5)
        selectors = np.clip(np.round(idx_norm), 0, 3).astype(np.float64)
        sel_grid = np.zeros((TILE_SIZE, TILE_SIZE))
        for b in range(BLOCKS_PER_TILE):
            bx, by = (b % BLOCK_COLS) * 4, (b // BLOCK_COLS) * 4
            sel_grid[by:by + 4, bx:bx + 4] = selectors[b]
        w0 = np.where(sel_grid == 0, 1.0,
             np.where(sel_grid == 1, 2.0 / 3,
             np.where(sel_grid == 2, 1.0 / 3, 0.0)))
        w1 = 1.0 - w0
        y = channels[0] * w0 + channels[3] * w1
        cb = channels[1] * w0 + channels[4] * w1
        cr = channels[2] * w0 + channels[5] * w1
    else:
        y = (channels[0] + channels[3]) / 2
        cb = (channels[1] + channels[4]) / 2
        cr = (channels[2] + channels[5]) / 2
    return _ycbcr_to_rgb(y, cb, cr)


# ── index (.tmst_pc) + chunk (.tmst_chunk_pc), from BYTES ────────────────────

def parse_tile_index(data):
    """`.tmst_pc` bytes → (grid_w, grid_h, [(offset, size)]).  Raises on bad magic."""
    if data[:4] != b"TMST":
        raise ValueError("not a TMST index (magic=%r)" % data[:4])
    grid_w = struct.unpack_from("<I", data, 0x14)[0]
    grid_h = struct.unpack_from("<I", data, 0x18)[0]
    n = (len(data) - 76) // 8
    records = [struct.unpack_from("<II", data, 76 + i * 8) for i in range(n)]
    return grid_w, grid_h, records


def find_tgu1_positions(data):
    """All TGU1 chunk start offsets in a `.tmst_chunk_pc`."""
    positions, pos = [], 0
    while True:
        idx = data.find(b"TGU1", pos)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 4
    return positions


# ── ENCODE (write-back) ─────────────────────────────────────────────────────
# Ported from ProLution's reference encoder.  NOT wired into the editor UI — it's here so a future
# terrain-modding feature can re-encode a 128×128 RGB tile into a valid .tmst_chunk_pc chunk.  The
# only lossy step is DCT quantisation (PSNR ~30-40 dB on natural terrain).  See
# memory/project_terrain_tmst_codec.md.
_IDCT_INV = np.linalg.inv(IDCT_BASIS.astype(np.float64))
_FWD_LEFT, _FWD_RIGHT = _IDCT_INV.T, _IDCT_INV


class _BitWriter:
    __slots__ = ("words", "pos")

    def __init__(self):
        self.words, self.pos = [], 0

    def write_bits(self, val, n):
        if n == 0:
            return
        val &= (1 << n) - 1
        while n > 0:
            wi, bi = self.pos >> 5, self.pos & 31
            while wi >= len(self.words):
                self.words.append(0)
            take = min(32 - bi, n)
            self.words[wi] |= (val & ((1 << take) - 1)) << bi
            val >>= take
            n -= take
            self.pos += take

    def write_unary(self, count):
        self.pos += count                # count zero bits = just advance (buffer is zero-filled)
        while (self.pos >> 5) >= len(self.words):
            self.words.append(0)
        self.write_bits(1, 1)

    def to_bytes(self):
        return struct.pack("<%dI" % len(self.words), *self.words)


def _zigzag_encode(val):
    return (val << 1) ^ (val >> 31)


def _rice_encode_value(writer, signed_val, flags):
    val = _zigzag_encode(signed_val)
    k = flags - 8 if flags >= 8 else flags
    if flags >= 8:                       # Rice
        writer.write_unary(val >> k)
        if k:
            writer.write_bits(val & ((1 << k) - 1), k)
    else:                                # Exp-Golomb
        adjusted = val + (1 << k)
        m = max(0, adjusted.bit_length() - 1 - k)
        writer.write_unary(m)
        if m + k > 0:
            writer.write_bits(adjusted - (1 << (m + k)), m + k)


def _choose_rice_k(values):
    if not values:
        return 8
    mags = [abs(_zigzag_encode(v)) for v in values]
    mean = sum(mags) / len(mags) if mags else 0
    if mean < 1:
        return 8
    return max(0, min(7, int(np.log2(mean)))) + 8


def _rgb_to_ycbcr(rgb):
    r = rgb[:, :, 0].astype(np.float64)
    g = rgb[:, :, 1].astype(np.float64)
    b = rgb[:, :, 2].astype(np.float64)
    y = 0.257 * r + 0.504 * g + 0.098 * b + BIAS_Y
    cb = -0.148 * r - 0.291 * g + 0.439 * b + BIAS_CBCR
    cr = 0.439 * r - 0.368 * g - 0.071 * b + BIAS_CBCR
    return y, cb, cr


def _encode_macro_bank(pixels_128):
    blocks = np.zeros((BLOCKS_PER_TILE, 4, 4), dtype=np.float64)
    for b in range(BLOCKS_PER_TILE):
        bx, by = (b % BLOCK_COLS) * 4, (b // BLOCK_COLS) * 4
        blocks[b] = pixels_128[by:by + 4, bx:bx + 4]
    coeffs = np.einsum("ij,bjk,kl->bil", _FWD_LEFT, blocks, _FWD_RIGHT) * NORM_FACTOR
    int_coeffs = np.round(coeffs / QUANT_TABLE).astype(np.int32)

    dc_pred, prev_nz = 0, 0
    nz_counts, count_deltas = [], []
    coeff_lists = {j: [] for j in range(1, 17)}
    for b in range(BLOCKS_PER_TILE):
        flat = int_coeffs[b].flatten()
        dc = int(flat[0])
        flat[0] = dc - dc_pred
        dc_pred = dc
        nz = 0
        for j in range(1, 17):
            if int(flat[ZIGZAG_ORDER[j - 1]]) != 0 or j <= nz:
                nz = j
        nz_counts.append(nz)
        count_deltas.append(nz - prev_nz)
        prev_nz = nz
        for j in range(1, nz + 1):
            coeff_lists[j].append(int(flat[ZIGZAG_ORDER[j - 1]]))

    flags0 = _choose_rice_k(count_deltas)
    bw0 = _BitWriter()
    for v in count_deltas:
        _rice_encode_value(bw0, v, flags0)
    parts = [struct.pack("<I", (flags0 << 28) | (bw0.pos & 0x0FFFFFFF))]
    datas = [bw0.to_bytes()]
    for j in range(1, 17):
        vals = coeff_lists[j]
        fj = _choose_rice_k(vals) if vals else 8
        bw = _BitWriter()
        for v in vals:
            _rice_encode_value(bw, v, fj)
        parts.append(struct.pack("<I", (fj << 28) | (bw.pos & 0x0FFFFFFF)))
        datas.append(bw.to_bytes())
    return b"".join(parts) + b"".join(datas)


def encode_tile(rgb):
    """Encode a 128×128 RGB uint8 array → decompressed TGU1 body bytes (EP0==EP1, selector 0)."""
    rgb = np.asarray(rgb, dtype=np.uint8)
    y, cb, cr = _rgb_to_ycbcr(rgb)
    parts = [struct.pack("<I", 0)]
    for ch in (y, cb, cr, y, cb, cr):
        parts.append(_encode_macro_bank(ch))
    parts.append(struct.pack("<I", 0))                                   # aux header
    parts.append(_encode_macro_bank(np.zeros((TILE_SIZE, TILE_SIZE))))   # index bank = 0
    return b"".join(parts)


def make_tgu1_chunk(body_bytes):
    """Wrap an uncompressed tile body in zlib + a TGU1 header + pre-header → a .tmst_chunk_pc chunk."""
    comp = zlib.compress(body_bytes, 6)
    tgu1 = bytearray(36)
    tgu1[0:4] = b"TGU1"
    struct.pack_into("<I", tgu1, 4, 3)
    tgu1[8:12] = b"PC\x00\x00"
    struct.pack_into("<I", tgu1, 12, TILE_SIZE)
    struct.pack_into("<I", tgu1, 16, TILE_SIZE)
    struct.pack_into("<I", tgu1, 20, 16384)
    struct.pack_into("<I", tgu1, 24, 1)
    struct.pack_into("<I", tgu1, 32, len(comp))
    pre = bytearray(40)
    total = 40 + 36 + len(comp)
    struct.pack_into("<I", pre, 0, total)
    struct.pack_into("<I", pre, 36, total)
    return bytes(pre) + bytes(tgu1) + comp


def best_lod(grid_w, grid_h, budget=300):
    """Highest-detail LOD whose tile count stays within `budget` (decode is pure-Python ~0.15 s/tile,
    so this caps the worst-case first-decode time).  LOD0=16N, LOD1=4N, LOD2=N tiles (N=grid_w·grid_h)."""
    n = grid_w * grid_h
    if 16 * n <= budget:
        return 0
    if 4 * n <= budget:
        return 1
    return 2


def enhance(img):
    """Display polish for a decoded terrain image (issue #8): DESTRIPE the horizontal tile-seam
    banding (subtract each row's deviation from a vertically median-smoothed row profile), then
    autocontrast + a saturation bump — the raw decode is washed-out because a runtime colour-scale
    value isn't stored in the file."""
    a = np.asarray(img.convert("RGB")).astype(np.float64)
    H = a.shape[0]
    rm = a.mean(axis=1)                                  # [H,3] per-row mean
    k, _pad = 9, 4
    rp = np.pad(rm, ((_pad, _pad), (0, 0)), mode="edge")
    rm_s = np.median(np.stack([rp[i:i + H] for i in range(k)], 0), axis=0)
    a = np.clip(a - (rm - rm_s)[:, None, :], 0, 255)
    out = Image.fromarray((a + 0.5).astype(np.uint8))
    out = ImageOps.autocontrast(out, cutoff=1)
    out = ImageEnhance.Color(out).enhance(1.25)
    return out


def compose(hd_img, base_img, detail_gain=1.3, blur=6, saturation=1.15):
    """Best-quality terrain image (issue #8): inject the high-def tmst's DETAIL onto a clean BASE
    (the baked minimap), so the result has the base's correct colour/brightness — NO per-tile decode
    banding — plus the tmst's fine detail.  ``base_img`` is resized to ``hd_img``.

    Why: the raw tmst decode is washed-out + horizontally banded (a runtime colour-scale value isn't
    in the file).  The banding is mostly low-frequency, so we keep only the tmst's HIGH-frequency
    detail (after a destripe pass) and lay it over the minimap's clean low-frequency colour.  Falls
    back to ``enhance()`` if no base is available."""
    if base_img is None:
        return enhance(hd_img)
    hd = hd_img.convert("RGB")
    W, H = hd.size
    base = base_img.convert("RGB").resize((W, H), Image.BILINEAR)
    a = np.asarray(hd).astype(np.float64)
    rm = a.mean(axis=1)                                   # destripe horizontal seams first
    p = 7
    rp = np.pad(rm, ((p, p), (0, 0)), mode="edge")
    rms = np.median(np.stack([rp[i:i + H] for i in range(2 * p + 1)], 0), axis=0)
    ds = np.clip(a - (rm - rms)[:, None, :], 0, 255)
    low = np.asarray(Image.fromarray(ds.astype(np.uint8))
                     .filter(ImageFilter.GaussianBlur(blur))).astype(np.float64)
    detail = ds - low                                    # tmst high-frequency detail only
    out = np.clip(np.asarray(base).astype(np.float64) + detail * detail_gain, 0, 255).astype(np.uint8)
    res = Image.fromarray(out)
    return ImageEnhance.Color(res).enhance(saturation) if saturation != 1.0 else res


def decode_terrain(tmst_bytes, chunk_bytes, lod=1, use_index=True, progress=None):
    """Decode + stitch the full terrain at ``lod`` (0=highest … 3=thumbnail) → PIL RGB Image.

    Records are a LOD pyramid: rec0=LOD3, then LOD2=N, LOD1=4N, LOD0=16N (N=grid_w·grid_h).
    ``progress(done, total)`` is called periodically if given.  Returns None if the data is empty.
    """
    grid_w, grid_h, records = parse_tile_index(tmst_bytes)
    positions = find_tgu1_positions(chunk_bytes)
    if not positions or not records:
        return None
    pos_to_idx = {p: i for i, p in enumerate(positions)}
    record_info = [(pos_to_idx.get(a + 40), b) for a, b in records]

    n_groups = grid_w * grid_h
    lod_config = {
        0: (grid_w * 4, grid_h * 4, 1 + n_groups + n_groups * 4, 16, 4),
        1: (grid_w * 2, grid_h * 2, 1 + n_groups, 4, 2),
        2: (grid_w, grid_h, 1, 1, 1),
        3: (1, 1, 0, 1, 1),
    }
    tiles_w, tiles_h, rec_start, tiles_per_group, inner_w = lod_config[lod]
    canvas = np.zeros((tiles_h * TILE_SIZE, tiles_w * TILE_SIZE, 3), dtype=np.uint8)
    n_tiles = tiles_w * tiles_h

    for tile_idx in range(n_tiles):
        ri = rec_start + tile_idx
        if ri >= len(record_info) or record_info[ri][0] is None:
            continue
        group, within = tile_idx // tiles_per_group, tile_idx % tiles_per_group
        gx, gy = group % grid_w, group // grid_w
        tx, ty = within % inner_w, within // inner_w
        x, y = gx * inner_w + tx, gy * inner_w + ty
        chunk_idx, rec_size = record_info[ri]
        cp = positions[chunk_idx]
        comp = chunk_bytes[cp + 36: cp + 36 + (rec_size - 76)]
        try:
            body = zlib.decompressobj().decompress(comp)
            rgb = decode_tile(body, use_index=use_index)
        except Exception:
            continue
        canvas[y * TILE_SIZE:(y + 1) * TILE_SIZE, x * TILE_SIZE:(x + 1) * TILE_SIZE] = rgb
        if progress and tile_idx % 16 == 0:
            progress(tile_idx, n_tiles)
    if progress:
        progress(n_tiles, n_tiles)
    return Image.fromarray(canvas)

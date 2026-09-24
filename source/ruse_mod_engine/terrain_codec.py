"""RUSE high-def terrain decode — ``highdef.tmst_pc`` index + ``highdef.tmst_chunk_pc`` chunks.

The ``.tmst_chunk_pc`` is a homebrew JPEG-like lossy RGB texture codec (Eugen ~2010) wrapped in the
same container as a ``.tgv`` texture.  Each index record is::

    record +0x00  u32 x2      (1, 1)
    record +0x08  u32 w, h    TRUE texture size — 512x512 for every LOD0/1/2 chunk in every ship map
    record +0x18  u16 x2      mip count, format-string length
    record +0x1C  char[4]     "DXT1"
    record +0x20  u32 x2      mip offset (always 0x28) + mip size
    record +0x28  "TGU1"      the codec block:
        TGU1 +0x04  u32       version (5)
        TGU1 +0x08  u32 w, h  ENDPOINT plane size = texture/4 (128x128)
        TGU1 +0x18  u32       selector block count (= ep_w*ep_h, 16384; a few tiles ship 16357..16383)
        TGU1 +0x1C  u32       flag 256 (the .tgv textures use 257)
        TGU1 +0x20  u32       DECOMPRESSED body size, then the zlib stream

So a chunk is literally a 512x512 DXT1 surface: six ``ep_w x ep_h`` endpoint planes (EP0 Y/Cb/Cr then
EP1 Y/Cb/Cr — one DXT1 endpoint pair per 4x4 output block) followed by a selector bank of ep_w*ep_h
blocks x 4x4 = ONE 2-bit selector per output pixel.  Every bank is Adaptive-Rice/Exp-Golomb-coded
4x4-DCT coefficients: un-Rice -> un-zigzag -> dequant -> IDCT.

Reverse-engineered and released CC0 by ProLution (RUSE Modding Database); validated against the real
game files in Issue #8 (the decode's roads/town/water match the baked minimap).  Issue #19 (also
ProLution) established the native 512 px chunk size, and re-deriving the container above showed the
selector bank had been read at 1/16 its real length — see ``decode_tile`` for what that cost and how
it decodes now.  This is the decode half, adapted to take BYTES (not file paths) so the editor can
feed it data extracted from a DataMap dat via edata.

Requires numpy + Pillow.  Callers should guard on availability.
"""
import os
import struct
import time
import zlib
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

# ── parallel decode tuning ───────────────────────────────────────────────────
# The per-tile decode is ~95% pure-Python entropy decoding (GIL-bound), so THREADS don't help — but
# independent processes do (measured ~4.7× at 8 workers on a 12-core box). We leave at least one core
# for the UI/OS and cap the pool so we don't spawn a numpy interpreter per core and exhaust RAM on
# many-core machines; below _PARALLEL_MIN_TILES the spawn cost isn't worth it so we stay sequential.
_MAX_DECODE_WORKERS = 8
_PARALLEL_MIN_TILES = 96     # below this the pool spawn cost isn't worth it (LOD2/small maps stay serial)

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

# ── chunk geometry (issue #19; read from the headers, these are only the near-universal defaults) ──
CHUNK_PX = 512               # native texture size of every LOD0/LOD1/LOD2 chunk in every shipped map
ENDPOINT_PX = 128            # = CHUNK_PX // 4: one DXT1 endpoint pair per 4x4 output block
RENDER_PX = (64, 128, 256, 512)   # per-chunk render resolutions the decoder can produce

# Mapping from the decoded selector bank onto DXT1 selector space (0..3).  The bank is NOT stored the
# way the colour banks are: no DC prediction, and no per-4x4-block transpose.  What comes out is a
# signed field which maps affinely onto the selectors.  Fitted by regression against a ground truth
# the LOD pyramid provides for free — a LOD2 tile and the 16 LOD0 tiles of the same grid cell cover
# the same ground, and those 16 tiles carry 4x4 x 128x128 = 512x512 ENDPOINT pixels, i.e. a
# selector-free 512x512 reference image aligned pixel-for-pixel with the LOD2 tile's output.  Over 12
# cells on 3 maps: A = 0.2303 +/- 0.0093, B = 1.962 +/- 0.031 (tools/test_scripts/probe_tmst_*).
SELECTOR_A, SELECTOR_B = 0.230, 1.96


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


def _bank_subbands(body, off):
    """The 17 sub-band descriptors of a macro bank → ([(flags, bits, data_off, data_bytes)], end_off).

    Every sub-band's byte range comes from the descriptor table, so a caller that decodes only some of
    them still knows where the bank ends and stays aligned with the stream."""
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
    return subs, off


def _nz_counts(body, subs, n_blocks):
    """Sub-band 0 = per-block deltas of the nonzero-coefficient count → the running counts."""
    fl0, ct0, o0, d0 = subs[0]
    r = _BitReader(body, o0, d0) if ct0 and d0 else None
    raw_counts = [_rice_decode_value(r, fl0) for _ in range(n_blocks)] if r else [0] * n_blocks
    nz_counts, running = [], 0
    for v in raw_counts:
        running += v
        nz_counts.append(max(0, running))
    return nz_counts


def _decode_macro_bank(body, off, n_blocks=BLOCKS_PER_TILE, block_cols=BLOCK_COLS,
                       transpose=True, dc_predict=True):
    """Decode one channel bank → (pixel plane, raw 4×4 blocks, new offset).

    ``block_cols`` is how many 4×4 blocks make a row of the plane, so the plane comes out
    ``(n_blocks // block_cols) * 4`` high by ``block_cols * 4`` wide.  ``transpose`` and
    ``dc_predict`` are switchable because the SELECTOR bank uses neither while the colour banks use
    both (issue #19)."""
    subs, off = _bank_subbands(body, off)

    nz_counts = _nz_counts(body, subs, n_blocks)

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
        if dc_predict:
            flat[0] += dc_pred
            dc_pred = flat[0]
        coeffs[b] = flat.reshape(4, 4) * QUANT_TABLE

    blocks = np.einsum("ij,bjk,kl->bil", IDCT_BT, coeffs, IDCT_BASIS) / NORM_FACTOR
    # Each 4×4 block comes out TRANSPOSED relative to a row-major surface (the game's DCT indexes the
    # block column-major). Without this swap every 4×4 block is diagonally flipped — which reads as a
    # herringbone "rotated detail" texture and a ~2× discontinuity spike at every 4px block seam
    # (measured block-edge/interior step 1.96 → 1.07 on Cotentin once transposed: blocks become
    # seamless and roads/hedgerows flow correctly). ProLution's reference codec had the same bug; it
    # was invisible at LOD2 overviews and only surfaced at the higher LODs the detail dropdown enables
    # (issue #15). It applies to the COLOUR banks only — the selector bank is stored untransposed, and
    # forcing this swap on it costs r 0.981 -> 0.933 against ground truth (issue #19), so callers pass
    # transpose=False for it.
    if transpose:
        blocks = blocks.transpose(0, 2, 1)
    return _blocks_to_plane(blocks, block_cols), blocks, off


def _blocks_to_plane(blocks, block_cols):
    """[n,4,4] 4×4 blocks in raster order → the (rows*4, block_cols*4) plane they tile.

    A short final row (a few shipped tiles carry 16357..16383 selector blocks instead of 16384) is
    zero-padded rather than dropped, so the plane always has whole rows."""
    n = blocks.shape[0]
    rows = (n + block_cols - 1) // block_cols
    if n < rows * block_cols:
        pad = np.zeros((rows * block_cols, 4, 4), dtype=blocks.dtype)
        pad[:n] = blocks
        blocks = pad
    return blocks.reshape(rows, block_cols, 4, 4).transpose(0, 2, 1, 3) \
                 .reshape(rows * 4, block_cols * 4)


def _decode_bank_dc(body, off, n_blocks):
    """Only the DC coefficient of each block of a bank → (dc [n_blocks], offset after the bank).

    This is exact, not an approximation: rows 1..3 of ``IDCT_BASIS`` each sum to zero, so the MEAN of a
    decoded 4×4 block is exactly its dequantised DC coefficient.  A render at the endpoint resolution
    (one pixel per 4×4 block) therefore needs sub-bands 0 and 1 only, and sub-bands 2..16 are skipped
    entirely — which is what makes the low-resolution steps of the ladder genuinely cheaper rather
    than just smaller (issue #19)."""
    subs, end = _bank_subbands(body, off)
    nz_counts = _nz_counts(body, subs, n_blocks)
    f1, c1, o1, d1 = subs[1]
    n_need = sum(1 for c in nz_counts if c >= 1)
    vals = []
    if n_need > 0 and c1 > 0 and d1 > 0:
        r = _BitReader(body, o1, d1)
        vals = [_rice_decode_value(r, f1) for _ in range(n_need)]
    dc = np.zeros(n_blocks)
    k = 0
    for b in range(n_blocks):
        if nz_counts[b] >= 1 and k < len(vals):
            dc[b] = vals[k]
            k += 1
    return dc, end


def _ycbcr_to_rgb(y, cb, cr):
    yf = (y - BIAS_Y) * SCALE_Y
    r = yf + COEFF_CR_R * (cr - BIAS_CBCR)
    g = yf - COEFF_CR_G * (cr - BIAS_CBCR) - COEFF_CB_G * (cb - BIAS_CBCR)
    b = yf + COEFF_CB_B * (cb - BIAS_CBCR)
    return np.clip(np.stack([r, g, b], axis=-1) + 0.5, 0, 255).astype(np.uint8)


def _scale_planes(planes, out_w, out_h):
    """Box-reduce (or, as a fallback, pixel-replicate) YCbCr planes to out_w × out_h.

    Reducing the float planes rather than the finished RGB avoids a round-trip through uint8, and the
    factors are always integral here (512→256/128/64, 128→64)."""
    h, w = planes[0].shape
    if (w, h) == (out_w, out_h):
        return planes
    if w >= out_w and h >= out_h and w % out_w == 0 and h % out_h == 0:
        kx, ky = w // out_w, h // out_h
        return [p.reshape(out_h, ky, out_w, kx).mean(axis=(1, 3)) for p in planes]
    ry, rx = max(1, -(-out_h // h)), max(1, -(-out_w // w))
    return [np.repeat(np.repeat(p, ry, 0), rx, 1)[:out_h, :out_w] for p in planes]


def decode_tile(body, use_index=True, tile_px=TILE_SIZE, ep_w=ENDPOINT_PX, ep_h=ENDPOINT_PX,
                n_sel=None):
    """A decompressed TGU1 body → ``tile_px`` wide uint8 RGB ndarray.

    ``ep_w``/``ep_h``/``n_sel`` come from the chunk's TGU1 header (see ``chunk_geometry``); the
    defaults are what every LOD0/LOD1/LOD2 chunk in every shipped map carries.  The chunk's NATIVE
    size is ``ep_w * 4`` — 512 px — and ``tile_px`` selects what we render out of it:

      * ``tile_px > ep_w`` (256, 512): decode the whole selector bank and blend per output pixel.
        This is the real 512 px surface the game holds; 256 is its exact 2× box reduction.
      * ``tile_px <= ep_w`` (64, 128): the selector's per-block MEAN is enough, and that mean is
        exactly the bank's DC coefficient, so sub-bands 2..16 are never read.  128 px is therefore
        the exact box reduction of the 512 px render (bar selector clamping), not a crop of it.

    The selector bank used to be read as 1024 blocks at a 128-wide stride — 1/16 of its real length,
    laid out over the wrong ground.  Scored against the LOD0-endpoint ground truth described at
    SELECTOR_A, that render (r 0.932/0.875) came out WORSE than ignoring the selector altogether
    (0.946/0.905); reading the bank properly gives 0.982/0.966, and restores the contrast that used to
    be blamed on a missing runtime colour scale (sd 42.1 vs the reference's 42.0)."""
    n_ep_blocks = (ep_w // 4) * (ep_h // 4)
    off = 4
    channels = []
    for _ in range(6):
        pixels, _, off = _decode_macro_bank(body, off, n_ep_blocks, ep_w // 4)
        channels.append(pixels)

    out_w = max(1, int(tile_px))
    out_h = max(1, int(round(tile_px * ep_h / float(ep_w))))

    if not use_index:
        planes = [(channels[0] + channels[3]) / 2, (channels[1] + channels[4]) / 2,
                  (channels[2] + channels[5]) / 2]
        return _ycbcr_to_rgb(*_scale_planes(planes, out_w, out_h))

    aux_count = struct.unpack_from("<I", body, off)[0]
    off += 4 + aux_count * 4
    if n_sel is None:
        n_sel = ep_w * ep_h

    if out_w > ep_w:                                   # full per-pixel selector detail
        _, sel_blocks, off = _decode_macro_bank(body, off, n_sel, ep_w,
                                                transpose=False, dc_predict=False)
        sel = np.clip(np.round(SELECTOR_A * _blocks_to_plane(sel_blocks, ep_w) + SELECTOR_B), 0, 3)
        w0 = 1.0 - sel[:ep_h * 4, :ep_w * 4] / 3.0     # sel 0..3 → EP0 weight 1, 2/3, 1/3, 0
        up = lambda p: np.repeat(np.repeat(p, 4, axis=0), 4, axis=1)
        planes = [up(channels[i]) * w0 + up(channels[i + 3]) * (1.0 - w0) for i in range(3)]
    else:                                              # block-mean selector = the bank's DC alone
        dc, off = _decode_bank_dc(body, off, n_sel)
        flat = np.zeros(ep_w * ep_h)
        flat[:min(n_sel, flat.size)] = dc[:flat.size]
        w0 = 1.0 - np.clip(SELECTOR_A * flat.reshape(ep_h, ep_w) + SELECTOR_B, 0.0, 3.0) / 3.0
        planes = [channels[i] * w0 + channels[i + 3] * (1.0 - w0) for i in range(3)]

    return _ycbcr_to_rgb(*_scale_planes(planes, out_w, out_h))


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


def chunk_geometry(chunk_bytes, tgu1_pos):
    """The TGU1 header at ``tgu1_pos`` → (ep_w, ep_h, n_selector_blocks, native_px).

    Read rather than assumed: a handful of shipped tiles carry 16357..16383 selector blocks instead of
    the usual 16384, and the LOD3 overview record is not square (256×128 … 1024×512).  Falls back to
    the universal defaults if the header looks wrong, so a malformed chunk degrades to a normal decode
    attempt instead of raising."""
    try:
        ep_w, ep_h = struct.unpack_from("<II", chunk_bytes, tgu1_pos + 8)
        n_sel = struct.unpack_from("<I", chunk_bytes, tgu1_pos + 24)[0]
        if not (4 <= ep_w <= 4096 and 4 <= ep_h <= 4096 and 0 < n_sel <= ep_w * ep_h):
            raise ValueError
    except Exception:
        ep_w, ep_h, n_sel = ENDPOINT_PX, ENDPOINT_PX, ENDPOINT_PX * ENDPOINT_PX
    return ep_w, ep_h, n_sel, ep_w * 4


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
        blocks[b] = pixels_128[by:by + 4, bx:bx + 4].T   # mirror the decoder's per-block transpose
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
    """Encode a 128×128 RGB uint8 array → decompressed TGU1 body bytes (EP0==EP1, selector 0).

    NOTE (issue #19): this writes a 1024-block selector bank, i.e. a 128 px surface — NOT the 512 px
    layout the game actually ships (16384 selector blocks, one per output pixel), and it omits the
    outer .tgv-style record envelope entirely.  It was never game-valid and still isn't; decoding its
    output needs ``decode_tile(..., n_sel=1024)``.  Left as the starting point for a future terrain
    write-back, which will need the real container before it can round-trip."""
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
    """Highest-detail LOD whose tile count stays within `budget`, capping worst-case first-decode time.
    LOD0=16N, LOD1=4N, LOD2=N tiles (N=grid_w·grid_h).

    Measured per-tile decode (issue #19): ~0.095 s at 64/128 px per chunk, ~0.219 s at 256/512 px —
    so a budget in tiles is only half the story once ``tile_px`` is also a lever."""
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


def compose(hd_img, base_img, detail_gain=1.3, blur=6, saturation=1.15, destripe=True):
    """Best-quality terrain image (issue #8): inject the high-def tmst's DETAIL onto a clean BASE
    (the baked minimap), so the result has the base's correct colour/brightness — NO per-tile decode
    banding — plus the tmst's fine detail.  ``base_img`` is resized to ``hd_img``.

    Why: the raw tmst decode is washed-out + horizontally banded (a runtime colour-scale value isn't
    in the file).  The banding is mostly low-frequency, so we keep only the tmst's HIGH-frequency
    detail (after a destripe pass) and lay it over the minimap's clean low-frequency colour.  Falls
    back to ``enhance()`` if no base is available.

    ``destripe`` subtracts each row's deviation from a vertically median-smoothed row profile.  It is
    there for the banding the OLD truncated selector read baked into every tile; that cause is fixed
    (issue #19).  It is also a WHOLE-IMAGE operation, so the tiled path (``terrain_tiles``) turns it
    off — a row median taken over whatever happens to be on screen would shift as the user pans."""
    if base_img is None:
        return enhance(hd_img)
    hd = hd_img.convert("RGB")
    W, H = hd.size
    base = base_img.convert("RGB").resize((W, H), Image.BILINEAR)
    a = np.asarray(hd).astype(np.float64)
    if destripe:
        rm = a.mean(axis=1)                               # destripe horizontal seams first
        p = 7
        rp = np.pad(rm, ((p, p), (0, 0)), mode="edge")
        rms = np.median(np.stack([rp[i:i + H] for i in range(2 * p + 1)], 0), axis=0)
        ds = np.clip(a - (rm - rms)[:, None, :], 0, 255)
    else:
        ds = a
    low = np.asarray(Image.fromarray(ds.astype(np.uint8))
                     .filter(ImageFilter.GaussianBlur(blur))).astype(np.float64)
    detail = ds - low                                    # tmst high-frequency detail only
    out = np.clip(np.asarray(base).astype(np.float64) + detail * detail_gain, 0, 255).astype(np.uint8)
    res = Image.fromarray(out)
    return ImageEnhance.Color(res).enhance(saturation) if saturation != 1.0 else res


def decode_worker_count(max_workers=None):
    """How many worker processes to use: leave a core for the UI/OS and cap the pool so a many-core
    machine doesn't spin up a numpy interpreter per core and run itself out of RAM. `max_workers`
    overrides the auto value (still floored at 1)."""
    if max_workers is not None:
        return max(1, int(max_workers))
    cpu = os.cpu_count() or 2
    return max(1, min(cpu - 1, _MAX_DECODE_WORKERS))


# Set per worker process via the pool initializer (cheaper than shipping the flags on every task).
_WORKER_USE_INDEX = True
_WORKER_TILE_PX = TILE_SIZE


def start_parent_watchdog():
    """Make THIS worker process exit as soon as its parent does.  Pool initializer (or part of one).

    Without it a decode worker outlives the app forever.  Measured: kill the Mod Manager mid-decode
    and all 8 workers were still alive 230 s later at roughly 550 MB each, and an aborted run left
    them running for ~20 minutes (~4.4 GB) until they were killed by hand.  A pool worker blocks in
    ``call_queue.get()`` and nothing in it ever checks whether the parent is still there, so a
    parent that dies without a clean ``shutdown()`` -- crash, taskkill /F, End Task -- strands every
    one of them.

    They are also invisible as leaks: in a frozen build ``multiprocessing`` re-launches OUR OWN exe
    for each worker, so each stranded worker shows up in Task Manager as another
    ``RUSE_ModManager_vX.Y.Z.exe`` with no window -- indistinguishable from the app itself.

    ``os._exit`` on purpose: the parent is gone, there is no result to hand back and no cleanup worth
    running.  Polls rather than touching ``parent._sentinel`` so it cannot break on a private detail.
    Silent no-op when there is no parent process (the in-process fallback path)."""
    try:
        import multiprocessing
        import threading
        parent = multiprocessing.parent_process()
    except Exception:
        return
    if parent is None:
        return                       # running in-process, not as a pool worker -- nothing to watch

    def _watch():
        while True:
            try:
                if not parent.is_alive():
                    os._exit(0)
            except Exception:
                return               # can't tell any more; leave the worker alone rather than guess
            time.sleep(2.0)

    try:
        threading.Thread(target=_watch, daemon=True).start()
    except Exception:
        pass


def _worker_init(use_index, tile_px=TILE_SIZE):
    global _WORKER_USE_INDEX, _WORKER_TILE_PX
    _WORKER_USE_INDEX, _WORKER_TILE_PX = use_index, tile_px
    start_parent_watchdog()


def _decode_tile_job(task):
    """Worker side: (x, y, compressed_body, ep_w, ep_h, n_sel) → (x, y, rgb-or-None). Mirrors the
    sequential per-tile try/except so one bad tile returns None instead of killing the whole batch."""
    x, y, comp, ep_w, ep_h, n_sel = task
    try:
        body = zlib.decompressobj().decompress(comp)
        return x, y, decode_tile(body, _WORKER_USE_INDEX, _WORKER_TILE_PX, ep_w, ep_h, n_sel)
    except Exception:
        return x, y, None


def decode_terrain(tmst_bytes, chunk_bytes, lod=1, use_index=True, progress=None, max_workers=None,
                   tile_px=TILE_SIZE):
    """Decode + stitch the full terrain at ``lod`` (0=highest … 3=thumbnail) → PIL RGB Image.

    Records are a LOD pyramid: rec0=LOD3, then LOD2=N, LOD1=4N, LOD0=16N (N=grid_w·grid_h).
    ``lod`` and ``tile_px`` are the two independent detail levers: the tier says how many chunks cover
    the map, ``tile_px`` how much of each chunk's native 512 px we render, so the finished image is
    ``tiles_w * tile_px`` across.  ``progress(done, total)`` is called periodically if given (it may
    raise to ABORT — the abort propagates out and the worker pool is torn down).  Returns None if the
    data is empty.

    Big maps (≥ _PARALLEL_MIN_TILES tiles) are decoded across a process pool — the per-tile work is
    GIL-bound pure-Python entropy decoding, so processes (not threads) are what parallelise it. If the
    pool can't start or breaks mid-run (e.g. a locked-down/frozen environment) it falls back to the
    sequential path, so the result is identical and worst-case as fast as before.
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
    tile_px = max(1, int(tile_px))
    canvas = np.zeros((tiles_h * tile_px, tiles_w * tile_px, 3), dtype=np.uint8)
    n_tiles = tiles_w * tiles_h

    # tile work-list: (dst_x, dst_y, compressed_body, ep_w, ep_h, n_sel) for every present tile
    tasks = []
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
        ep_w, ep_h, n_sel, _native = chunk_geometry(chunk_bytes, cp)
        tasks.append((x, y, chunk_bytes[cp + 36: cp + 36 + (rec_size - 76)], ep_w, ep_h, n_sel))

    def _place(x, y, rgb):
        if rgb is None:
            return
        h, w = rgb.shape[:2]
        y0, x0 = y * tile_px, x * tile_px
        h, w = min(h, canvas.shape[0] - y0), min(w, canvas.shape[1] - x0)
        if h > 0 and w > 0:
            canvas[y0:y0 + h, x0:x0 + w] = rgb[:h, :w]

    workers = decode_worker_count(max_workers)
    if workers > 1 and len(tasks) >= _PARALLEL_MIN_TILES:
        ex = None
        try:
            ex = ProcessPoolExecutor(max_workers=workers, initializer=_worker_init,
                                     initargs=(use_index, tile_px))
        except Exception:
            ex = None                                   # can't spawn → sequential fallback below
        if ex is not None:
            aborted = []
            ok = True
            try:
                done = 0
                for x, y, rgb in ex.map(_decode_tile_job, tasks, chunksize=8):
                    _place(x, y, rgb)
                    done += 1
                    if progress and done % 16 == 0:
                        try:
                            progress(done, n_tiles)
                        except BaseException as ab:      # caller asked to abort — don't fall back
                            aborted.append(ab)
                            break
            except Exception:
                ok = False                               # pool broke mid-run → sequential fallback
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
            if aborted:
                raise aborted[0]
            if ok:
                if progress:
                    progress(n_tiles, n_tiles)
                return Image.fromarray(canvas)
            # else: fall through and redo sequentially (overwrites any partial canvas)

    for i, (x, y, comp, ep_w, ep_h, n_sel) in enumerate(tasks):
        try:
            rgb = decode_tile(zlib.decompressobj().decompress(comp), use_index,
                              tile_px, ep_w, ep_h, n_sel)
        except Exception:
            continue
        _place(x, y, rgb)
        if progress and i % 16 == 0:
            progress(i, n_tiles)
    if progress:
        progress(n_tiles, n_tiles)
    return Image.fromarray(canvas)

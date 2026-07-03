"""RUSE ``.tgv_pc`` texture decode (e.g. ``div_map.tgv_pc`` = the full-map terrain albedo, DXT5_LIN).

Header → ZIPO/zlib mip blocks (Z_FULL_FLUSH — use ``decompressobj``) → DXT1/DXT5/uncompressed block
decode → PIL image.  Decode-only.  Used by the map editor as an alternative, seam-free terrain source
(issue #8): unlike the tiled ``tmst`` high-def, the albedo is a single clean texture.

Requires numpy + Pillow.
"""
import struct
import zlib

import numpy as np
from PIL import Image


def _parse(data):
    """Parse a .tgv_pc → (width, height, format, [mip dicts]).  None on bad data."""
    if len(data) < 32:
        return None
    _v1, _v2, w, h, _w2, _h2 = struct.unpack_from("<6I", data, 0)
    mip_count, fmt_len = struct.unpack_from("<2H", data, 24)
    if not (1 <= mip_count <= 16):
        return None
    fmt = data[28:28 + fmt_len].decode("ascii", "replace")
    ts = 28 + fmt_len
    offs = struct.unpack_from("<%dI" % mip_count, data, ts)
    sizes = struct.unpack_from("<%dI" % mip_count, data, ts + mip_count * 4)
    mips = []
    for i in range(mip_count):
        off = offs[i]
        if off + 8 > len(data) or data[off:off + 4] != b"ZIPO":
            continue
        decomp = struct.unpack_from("<I", data, off + 4)[0]
        try:
            raw = zlib.decompressobj().decompress(data[off + 8: off + sizes[i]])
        except Exception:
            continue
        if len(raw) == decomp:
            mips.append({"level": i, "w": max(1, w >> i), "h": max(1, h >> i), "data": raw})
    return w, h, fmt, mips


def _unpack565(c):
    r = ((c >> 11) & 31).astype(np.uint16)
    g = ((c >> 5) & 63).astype(np.uint16)
    b = (c & 31).astype(np.uint16)
    return (np.stack([(r * 527 + 23) >> 6, (g * 259 + 33) >> 6, (b * 527 + 23) >> 6], -1)
            ).astype(np.uint8)


def _decode_dxt_color(data, w, h, stride, color_off):
    """Vectorised DXT1-style colour decode (also the colour half of DXT5).  ``stride`` = bytes per
    4×4 block (8 for DXT1, 16 for DXT5), ``color_off`` = byte offset of the colour block within it."""
    bw, bh = (w + 3) // 4, (h + 3) // 4
    nb = bw * bh
    blk = np.frombuffer(data, dtype=np.uint8, count=nb * stride).reshape(nb, stride)
    c0 = blk[:, color_off].astype(np.uint16) | (blk[:, color_off + 1].astype(np.uint16) << 8)
    c1 = blk[:, color_off + 2].astype(np.uint16) | (blk[:, color_off + 3].astype(np.uint16) << 8)
    bits = (blk[:, color_off + 4].astype(np.uint32) | (blk[:, color_off + 5].astype(np.uint32) << 8)
            | (blk[:, color_off + 6].astype(np.uint32) << 16) | (blk[:, color_off + 7].astype(np.uint32) << 24))
    e0, e1 = _unpack565(c0), _unpack565(c1)                      # [nb,3]
    pal = np.zeros((nb, 4, 3), np.uint8)
    pal[:, 0], pal[:, 1] = e0, e1
    f0, f1 = e0.astype(np.uint16), e1.astype(np.uint16)
    pal[:, 2] = ((2 * f0 + f1) // 3).astype(np.uint8)
    pal[:, 3] = ((f0 + 2 * f1) // 3).astype(np.uint8)
    idx = np.stack([(bits >> (2 * p)) & 3 for p in range(16)], -1)   # [nb,16]
    px = np.take_along_axis(pal, idx[:, :, None].repeat(3, 2).astype(np.intp), 1)  # [nb,16,3]
    px = px.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)
    return px[:h, :w]


def decode_tgv(data):
    """Decode a ``.tgv_pc`` to a full-resolution PIL RGB Image (mip 0).  None if unsupported/empty."""
    parsed = _parse(data)
    if not parsed:
        return None
    w, h, fmt, mips = parsed
    m = next((mm for mm in mips if mm["level"] == 0), max(mips, key=lambda x: len(x["data"])) if mips else None)
    if m is None:
        return None
    f = fmt.upper()
    mw, mh, raw = m["w"], m["h"], m["data"]
    if f.startswith("DXT1"):
        rgb = _decode_dxt_color(raw, mw, mh, 8, 0)
    elif f.startswith("DXT5") or f.startswith("DXT3"):
        rgb = _decode_dxt_color(raw, mw, mh, 16, 8)             # colour is the 2nd 8 bytes
    elif f.startswith("A8R8G8B8") or f.startswith("R8G8B8A8") or f.startswith("X8R8G8B8"):
        arr = np.frombuffer(raw, np.uint8, count=mw * mh * 4).reshape(mh, mw, 4)
        rgb = arr[:, :, 2::-1] if f.startswith("A8R8G8B8") or f.startswith("X8R8G8B8") else arr[:, :, :3]
    else:
        return None
    return Image.fromarray(np.ascontiguousarray(rgb), "RGB")

"""Tile-backed lazy terrain for the map editor — full 512 px detail without a giant image.

The game already ships the terrain as a zoomable tile pyramid (see ``terrain_codec``): four LOD tiers,
512x512 px per chunk, with a byte-offset index.  The editor used to flatten a whole tier into one
image, which is why full detail looked impossible — Cotentin at 512 px per chunk is 24576x16384, i.e.
1.6 GB as RGBA.  Nothing needs that: ``map_editor._build_composite`` only ever crops the visible
viewport and resizes THAT, so a viewport at 1:1 is ~21 MB of tiles.

``TerrainTiles`` therefore presents the same tiny surface the editor already used — ``.size`` and a
crop — while decoding only the chunks a given view actually touches:

  * **Coordinate space.**  ``.size`` is the LOD0 grid at ``BASE_PX`` (128) px per chunk, so it matches
    what the old "Full detail" setting produced and the overlay rasters keep their scale.  Chunks are
    still DECODED at their native 512, so zooming past 1:1 keeps resolving real detail rather than
    interpolating — that is the 4x issue #19 is about.
  * **Tier by zoom.**  A tier-L chunk covers ``BASE_PX * 2**L`` base px, so it yields ``4 / 2**L``
    output px per base px.  The coarsest tier that still meets the requested scale is used, which is
    what keeps a zoomed-out view to a handful of chunks.
  * **Never blocks.**  A crop composes from whatever is decoded; anything missing falls back to the
    baked minimap upscaled, and is queued.  Since an upscaled minimap carries almost no
    high-frequency detail, the detail-over-base composition degrades to plain minimap colour there —
    so the picture simply sharpens in place as chunks land, with no special-casing.
  * **Lossless cache.**  Decoded chunks are cached to disk as PNG — the raw decode, exactly as the
    codec produced it, so it stays usable for editing later.  Re-reading a cached chunk is ~45x
    cheaper than re-decoding it (6 ms vs 276 ms).

Requires numpy + Pillow.  Callers should guard on availability.
"""
import os
import threading
import zlib
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image, ImageEnhance

from . import terrain_codec as tc

BASE_PX = 128                 # base-space px per LOD0 chunk (keeps .size == the old "Full detail")
NATIVE_PX = tc.CHUNK_PX       # 512 — what a chunk actually decodes to
CACHE_REV = "r4"              # bump when terrain_codec output changes, so stale PNGs are bypassed
_LRU_TILES = 64               # ~50 MB of decoded RGB at 512x512
_LRU_SCALED = 512             # display-sized chunks; tiny (a 64px chunk is 12 KB)
_LRU_CANVAS = 6               # fully assembled+composed viewports
_MAX_QUEUE = 512
_MIN_TILE_PX = 32


def _compose_fast(hd, base, detail_gain=1.3, saturation=1.15, shrink=8):
    """``terrain_codec.compose``'s detail-over-base, but fast enough to run per frame.

    Same idea — keep the decoded chunk's HIGH-frequency detail and lay it over the minimap's clean
    colour — with two changes that matter only for speed: the low-pass is a downscale/upscale pair
    instead of ``GaussianBlur(6)`` (visually equivalent at this radius, and O(1) per pixel), and the
    arithmetic is float32.  Measured on a 1536x1024 viewport: 210 ms -> ~15 ms, which is the
    difference between a pan that stutters and one that doesn't."""
    w, h = hd.size
    a = np.asarray(hd, dtype=np.float32)
    small = hd.resize((max(1, w // shrink), max(1, h // shrink)), Image.BILINEAR)
    low = np.asarray(small.resize((w, h), Image.BILINEAR), dtype=np.float32)
    # In-place from here on. On a 2250x1424 viewport this array is ~9.6M floats and every extra
    # temporary is another 38 MB written and read back: the allocations, not the arithmetic, are
    # what made this ~150 ms a frame at mid zoom. Same maths, a third of the memory traffic.
    out = a
    out -= low                      # high-frequency detail (a - low)
    out *= detail_gain
    out += np.asarray(base, dtype=np.float32)
    if saturation != 1.0:
        # Fold ImageEnhance.Color in rather than paying a second full-image pass: it blends the
        # image with its own greyscale, using the same ITU-R 601-1 luma weights PIL's "L" convert
        # uses. Done here it costs one extra reduction instead of a convert + blend + reconvert.
        grey = (out[..., 0] * (299 / 1000.0) + out[..., 1] * (587 / 1000.0)
                + out[..., 2] * (114 / 1000.0))[..., None]
        out -= grey
        out *= saturation
        out += grey
    np.clip(out, 0, 255, out=out)
    return Image.fromarray(out.astype(np.uint8))


def _decode_task(task):
    """(tx, ty, compressed_body, ep_w, ep_h, n_sel) -> (tx, ty, rgb-or-None).

    Deliberately NOT ``terrain_codec._decode_tile_job``: that one reads the target size from a
    process-global the pool initializer sets, so calling it in-process (the no-pool fallback) would
    silently decode at the module default of 128 px instead of native.  Everything is in the task
    here, so the pooled and in-thread paths produce identical tiles."""
    tx, ty, comp, ep_w, ep_h, n_sel = task
    try:
        body = zlib.decompressobj().decompress(comp)
        return tx, ty, tc.decode_tile(body, True, NATIVE_PX, ep_w, ep_h, n_sel)
    except Exception:
        return tx, ty, None


class TerrainTiles:
    """Lazy tiled view over a map's ``.tmst_pc`` / ``.tmst_chunk_pc`` pair.

    ``on_ready()`` is called (from a worker thread) when newly decoded chunks have landed, so the
    caller can schedule a redraw.  Call ``close()`` when done."""

    def __init__(self, tmst_bytes, chunk_bytes, base_img=None, cache_dir=None,
                 finest_lod=0, on_ready=None, max_workers=None):
        self.grid_w, self.grid_h, self._records = tc.parse_tile_index(tmst_bytes)
        self._chunk = chunk_bytes
        self._positions = tc.find_tgu1_positions(chunk_bytes)
        self._pos_to_idx = {p: i for i, p in enumerate(self._positions)}
        self.base_img = base_img.convert("RGB") if base_img is not None else None
        self.cache_dir = cache_dir
        self.finest_lod = max(0, min(2, int(finest_lod)))
        self._on_ready = on_ready

        self.size = (self.grid_w * 4 * BASE_PX, self.grid_h * 4 * BASE_PX)

        self._lru = OrderedDict()          # (lod,tx,ty) -> native 512 RGB
        self._scaled = OrderedDict()       # (lod,tx,ty,tile_px) -> that chunk at display size
        self._canvas_cache = OrderedDict()  # assembled+composed viewports
        self._gen = 0                      # bumps when a chunk lands, invalidating the two caches
        self._lock = threading.Lock()
        self._queue = []
        self._inflight = set()
        self._wake = threading.Event()
        self._closed = False
        self._workers = tc.decode_worker_count(max_workers)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ── geometry ────────────────────────────────────────────────────────────
    def _tier(self, scale):
        """Coarsest LOD whose native render still meets `scale` output px per base px."""
        if scale <= 0:
            return 2
        lod = 2
        while lod > 0 and (NATIVE_PX / float(BASE_PX * (1 << lod))) < scale:
            lod -= 1
        return max(self.finest_lod, min(2, lod))

    def _tiles_across(self, lod):
        return self.grid_w * (1 << (2 - lod)), self.grid_h * (1 << (2 - lod))

    def _record_index(self, lod, tx, ty):
        n = self.grid_w * self.grid_h
        rec_start = {0: 1 + n + 4 * n, 1: 1 + n, 2: 1}[lod]
        inner = 1 << (2 - lod)
        gx, gy = tx // inner, ty // inner
        within = (ty % inner) * inner + (tx % inner)
        return rec_start + (gy * self.grid_w + gx) * (inner * inner) + within

    # ── tile access ─────────────────────────────────────────────────────────
    def _cache_path(self, lod, tx, ty):
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, "%s_L%d" % (CACHE_REV, lod), "%d_%d.png" % (ty, tx))

    def _get_cached(self, key):
        with self._lock:
            img = self._lru.get(key)
            if img is not None:
                self._lru.move_to_end(key)
                return img
        path = self._cache_path(*key)
        if path and os.path.isfile(path):
            try:
                img = Image.open(path)
                img.load()
                img = img.convert("RGB")
                self._store(key, img)
                return img
            except Exception:
                pass
        return None

    def _store(self, key, img):
        with self._lock:
            self._lru[key] = img
            self._lru.move_to_end(key)
            while len(self._lru) > _LRU_TILES:
                self._lru.popitem(last=False)
            self._gen += 1                       # a new chunk invalidates the scaled/canvas caches

    def _scaled_tile(self, key, tile_px):
        """A chunk at DISPLAY size, cached.

        This is what keeps pan/zoom interactive. Re-reading the native chunks every frame is what made
        `_build_composite` cost ~630 ms: fitting Cotentin needs 96 LOD2 chunks but the native LRU holds
        64, so it thrashed and re-decoded ~96 PNGs per frame. Display-sized chunks are tiny (a 64 px
        chunk is 12 KB), so hundreds fit and a repeat view is nearly free."""
        sk = key + (tile_px,)
        with self._lock:
            img = self._scaled.get(sk)
            if img is not None:
                self._scaled.move_to_end(sk)
                return img
        native = self._get_cached(key)
        if native is None:
            return None
        img = (native if native.size == (tile_px, tile_px)
               else native.resize((tile_px, tile_px),
                                  Image.BOX if tile_px < native.size[0] else Image.BICUBIC))
        with self._lock:
            self._scaled[sk] = img
            self._scaled.move_to_end(sk)
            while len(self._scaled) > _LRU_SCALED:
                self._scaled.popitem(last=False)
        return img

    def _task_for(self, lod, tx, ty):
        """(tx, ty, compressed_body, ep_w, ep_h, n_sel) for the worker, or None if absent."""
        ri = self._record_index(lod, tx, ty)
        if ri >= len(self._records):
            return None
        off, size = self._records[ri]
        ci = self._pos_to_idx.get(off + 40)
        if ci is None or size <= 76:
            return None
        cp = self._positions[ci]
        ep_w, ep_h, n_sel, _native = tc.chunk_geometry(self._chunk, cp)
        return (tx, ty, self._chunk[cp + 36: cp + 36 + (size - 76)], ep_w, ep_h, n_sel)

    def _request(self, keys):
        if self._closed or not keys:
            return
        with self._lock:
            for k in keys:
                if k in self._inflight or k in self._lru:
                    continue
                if len(self._queue) >= _MAX_QUEUE:
                    break
                self._inflight.add(k)
                self._queue.append(k)
        self._wake.set()

    # ── background decode ───────────────────────────────────────────────────
    def _run(self):
        pool = None
        try:
            while not self._closed:
                self._wake.wait(0.25)
                self._wake.clear()
                while not self._closed:
                    with self._lock:
                        batch, self._queue = self._queue[:16], self._queue[16:]
                    if not batch:
                        break
                    tasks = []
                    for k in batch:
                        t = self._task_for(*k)
                        if t is None:
                            with self._lock:
                                self._inflight.discard(k)
                        else:
                            tasks.append((k, t))
                    if not tasks:
                        continue
                    if pool is None and self._workers > 1:
                        try:
                            pool = ProcessPoolExecutor(max_workers=self._workers)
                        except Exception:
                            pool = False            # can't spawn — decode in this thread instead
                    got = 0
                    try:
                        if pool:
                            results = list(pool.map(_decode_task, [t for _, t in tasks]))
                        else:
                            results = [_decode_task(t) for _, t in tasks]
                    except Exception:
                        pool = False                # pool broke mid-run — finish in this thread
                        results = [_decode_task(t) for _, t in tasks]
                    for (k, _t), res in zip(tasks, results):
                        rgb = res[2] if res else None
                        with self._lock:
                            self._inflight.discard(k)
                        if rgb is None:
                            continue
                        img = Image.fromarray(np.ascontiguousarray(rgb))
                        self._store(k, img)
                        self._write_cache(k, img)
                        got += 1
                    if got and self._on_ready:
                        try:
                            self._on_ready()
                        except Exception:
                            pass
        finally:
            if pool:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass

    def _write_cache(self, key, img):
        path = self._cache_path(*key)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            img.save(tmp, format="PNG", compress_level=6)   # lossless: the raw decode, as produced
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    # ── the two things the editor needs ─────────────────────────────────────
    def crop_resized(self, box, out_size, resample=None):
        """`box` in base-space px, `out_size` the wanted output size → RGBA Image, composed for display.

        Returns immediately: chunks not yet decoded show the baked minimap and are queued."""
        W, H = self.size
        x0, y0, x1, y1 = box
        x0 = max(0.0, min(float(W), float(x0)))
        y0 = max(0.0, min(float(H), float(y0)))
        x1 = max(x0 + 1.0, min(float(W), float(x1)))
        y1 = max(y0 + 1.0, min(float(H), float(y1)))
        ow, oh = max(1, int(out_size[0])), max(1, int(out_size[1]))

        scale = ow / max(1e-6, x1 - x0)                   # output px per base px
        lod = self._tier(scale)
        span = BASE_PX * (1 << lod)                       # base px covered by one chunk at this tier
        tw, th = self._tiles_across(lod)
        tx0 = max(0, int(x0 // span)); tx1 = min(tw - 1, int((x1 - 1e-6) // span))
        ty0 = max(0, int(y0 // span)); ty1 = min(th - 1, int((y1 - 1e-6) // span))
        nx, ny = tx1 - tx0 + 1, ty1 - ty0 + 1
        # Build the canvas at the resolution the OUTPUT needs, not at native — a chunk covers `span`
        # base px, so it only needs `scale * span` output px.  Without this, a fit-to-window frame
        # assembled the whole map at 512 px per chunk and then threw ~97% of it away (measured 5.4 s
        # on Cotentin vs well under a second here).
        #
        # Rounded UP to a power of two (never below what the view asks for, so no detail is lost —
        # the final resize does the fractional part).  That quantisation is what lets the scaled-chunk
        # and canvas caches survive a zoom burst: without it every wheel notch is a fresh tile_px and
        # therefore a total cache miss.
        want = max(_MIN_TILE_PX, min(NATIVE_PX, scale * span))
        tile_px = min(NATIVE_PX, 1 << (int(want - 1).bit_length()))
        cw, ch = nx * tile_px, ny * tile_px

        region = (tx0 * span, ty0 * span, (tx1 + 1) * span, (ty1 + 1) * span)

        ckey = (lod, tx0, ty0, tx1, ty1, tile_px, self._gen)
        with self._lock:
            cached = self._canvas_cache.get(ckey)
            if cached is not None:
                self._canvas_cache.move_to_end(ckey)
        if cached is not None:
            return self._finish(cached, (x0, y0, x1, y1), region, tile_px, span, (ow, oh), resample)

        # fallback layer: the baked minimap over exactly this tile-aligned region
        if self.base_img is not None:
            bw, bh = self.base_img.size
            src = (region[0] / W * bw, region[1] / H * bh, region[2] / W * bw, region[3] / H * bh)
            canvas = self.base_img.resize((cw, ch), Image.BICUBIC, box=src)
        else:
            canvas = Image.new("RGB", (cw, ch), (60, 70, 55))
        base_layer = canvas.copy()

        missing = []
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                key = (lod, tx, ty)
                img = self._scaled_tile(key, tile_px)
                if img is None:
                    missing.append(key)
                    continue
                canvas.paste(img, ((tx - tx0) * tile_px, (ty - ty0) * tile_px))
        self._request(missing)

        # lay the decoded detail over the minimap's colour. Not terrain_codec.compose: its destripe
        # is a whole-image row median (unstable across pans, and its cause — the truncated selector
        # read — is fixed, issue #19) and its GaussianBlur costs ~210 ms a frame. See _compose_fast.
        composed = _compose_fast(canvas, base_layer)
        with self._lock:
            self._canvas_cache[ckey] = composed
            self._canvas_cache.move_to_end(ckey)
            while len(self._canvas_cache) > _LRU_CANVAS:
                self._canvas_cache.popitem(last=False)
        return self._finish(composed, (x0, y0, x1, y1), region, tile_px, span, (ow, oh), resample)

    def _finish(self, composed, box, region, tile_px, span, out_size, resample):
        """Crop the assembled canvas to the exact view and scale it to the output size."""
        x0, y0, x1, y1 = box
        ow, oh = out_size
        cscale = tile_px / float(span)                    # base px -> canvas px
        crop = ((x0 - region[0]) * cscale, (y0 - region[1]) * cscale,
                (x1 - region[0]) * cscale, (y1 - region[1]) * cscale)
        if resample is None:
            resample = Image.NEAREST if (x1 - x0) * cscale <= ow else Image.BILINEAR
        return composed.resize((ow, oh), resample, box=crop).convert("RGBA")

    def prefetch(self, box, out_size):
        """Queue the chunks a view would need without composing it (used to warm the cache)."""
        try:
            self.crop_resized(box, out_size)
        except Exception:
            pass

    def stats(self):
        with self._lock:
            return {"cached": len(self._lru), "queued": len(self._queue),
                    "inflight": len(self._inflight)}

    def close(self):
        self._closed = True
        self._wake.set()

    def __bool__(self):
        return True

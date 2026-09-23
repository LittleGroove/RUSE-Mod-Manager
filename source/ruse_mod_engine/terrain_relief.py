"""Shaded-relief raster built from the decoded terrain MESH (``terrain_mesh``).

The map view already shows the game's terrain COLOUR at full detail (``terrain_tiles``).  What it
could not show was SHAPE: the colour texture is painted, so a hill and a flat field with the same
ground cover look identical.  The mesh carries the actual landform, so this module turns it into a
relief layer that can be laid over the colour -- the same trick a topographic map uses.

Two things make this cheap and accurate:

  * **The shading uses the mesh's own per-vertex normals.**  Nothing is inferred from a heightfield,
    so there is no gradient estimation and no resampling error -- the light is applied to exactly
    the surface orientation the game renders with.  Flat ground is (128, 128, 255), i.e. straight up.
  * **Triangles are filled flat, one polygon call each.**  Terrain triangles are small on screen, so
    per-triangle shading is visually smooth, and it keeps the whole map to a single pass of C-level
    PIL polygon fills instead of a per-pixel Python loop.  Cotentin's high-def mesh (228k triangles)
    rasterises in ~1.3 s at 1024 px.

Coordinates come straight from the decoded position: components 0 and 1 are the map's X and Y
quantised to 0..32768 regardless of the map's aspect ratio, so scaling them by the target width and
height puts the relief exactly on top of the baked minimap and the decoded terrain tiles (verified
against the shipped minimap: coastline, bays and sector lines coincide).

Real terrain normals are close to vertical over most of a map, so an ``exaggeration`` factor scales
the horizontal part of each normal before lighting.  That is the usual cartographic cheat: it makes
gentle relief legible without moving anything.

Requires numpy + Pillow.  Callers should guard on availability.
"""
import math

import numpy as np
from PIL import Image, ImageDraw

from .terrain_mesh import TerrainMesh

FULL = 32768                  # the position quantisation range on each axis
DEFAULT_LIGHT = (-0.6, -0.6, 0.55)     # from the north-west, the cartographic convention
# Tuned by compositing over the shipped minimap across the exaggeration/strength grid: at 2.0/0.35
# the landform reads clearly while the terrain's own colour survives.  Pushing either higher starts
# blowing the sea cliffs out to white and swamping the ground cover.
DEFAULT_EXAGGERATION = 2.0
DEFAULT_STRENGTH = 0.45
HIGHLIGHT_DAMP = 0.6          # lit slopes are toned down; shadow carries the form more legibly
LONG_SIDE = 2048              # default raster size; matches the editor's other overlay rasters


def _normalise(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def raster_size(mesh, long_side=LONG_SIDE):
    """Pixel size for a relief of this map, preserving its world aspect ratio."""
    w = mesh.grid_w * mesh.cell_size[0]
    h = mesh.grid_h * mesh.cell_size[1]
    if w <= 0 or h <= 0:
        w = h = 1.0
    if w >= h:
        return int(long_side), max(1, int(round(long_side * h / w)))
    return max(1, int(round(long_side * w / h))), int(long_side)


def shade_image(mesh, size=None, exaggeration=DEFAULT_EXAGGERATION, light=DEFAULT_LIGHT,
                long_side=LONG_SIDE):
    """Render the mesh to an 8-bit shaded-relief image (mid-grey = flat ground).

    Returns a PIL "L" image.  ``size`` overrides the automatic aspect-correct size.
    """
    w, h = size or raster_size(mesh, long_side)
    lx, ly, lz = _normalise(light)
    flat = max(lz, 1e-6)                      # what perfectly level ground returns
    img = Image.new("L", (w, h), int(round(255 * flat)))
    draw = ImageDraw.Draw(img)
    sx = (w - 1) / float(FULL)
    sy = (h - 1) / float(FULL)
    for cell in mesh.cells:
        v = mesh.vertices(cell)
        if v["count"] == 0:
            continue
        pos = v["elements"].get(0)
        nrm = v["elements"].get(8)
        if pos is None or nrm is None or not pos.get("decoded") or not nrm.get("decoded"):
            continue
        P = np.asarray(pos["data"], dtype=np.float64).reshape(-1, 4)
        N = np.asarray(nrm["data"], dtype=np.float64).reshape(-1, 4)
        xs = P[:, 0] * sx
        ys = P[:, 1] * sy
        # 0..255 maps to -1..1; exaggerate the horizontal part, then renormalise
        nx = (N[:, 0] / 127.5 - 1.0) * exaggeration
        ny = (N[:, 1] / 127.5 - 1.0) * exaggeration
        nz = np.maximum(N[:, 2] / 127.5 - 1.0, 1e-6)
        inv = 1.0 / np.sqrt(nx * nx + ny * ny + nz * nz)
        shade = np.clip((nx * lx + ny * ly + nz * lz) * inv, 0.0, 1.0) * 255.0
        shade = shade.astype(np.int32)
        # ib1 is a coarser LOD of the same ground -- drawing it too would just overpaint ib0
        try:
            tris = mesh.triangles(cell, 0)
        except ValueError:
            continue
        for a, b, c in tris:
            draw.polygon([(xs[a], ys[a]), (xs[b], ys[b]), (xs[c], ys[c])],
                         fill=int((shade[a] + shade[b] + shade[c]) // 3))
    return img


def to_overlay(shade, strength=DEFAULT_STRENGTH, highlight_damp=HIGHLIGHT_DAMP):
    """Turn a shaded-relief image into an RGBA layer to composite over the terrain colour.

    Shadows go down as black, highlights up as white, both with alpha proportional to how far the
    slope departs from flat -- so level ground stays fully transparent and the terrain's own colour
    is untouched there.  The neutral point is the image's MEDIAN rather than the analytic flat-ground
    value, so a map that is mostly gentle and one that is mostly steep both end up centred.
    """
    a = np.asarray(shade, dtype=np.float32) / 255.0
    flat = float(np.median(a))
    d = a - flat
    scale = max(flat, 1.0 - flat, 1e-6)
    d = np.clip(d / scale, -1.0, 1.0)
    lit = d >= 0
    alpha = np.abs(d) * float(strength)
    alpha = np.where(lit, alpha * float(highlight_damp), alpha)
    out = np.zeros(a.shape + (4,), dtype=np.uint8)
    out[..., :3] = np.where(lit[..., None], np.uint8(255), np.uint8(0))
    out[..., 3] = (np.clip(alpha, 0.0, 1.0) * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def build(tms_blob, exaggeration=DEFAULT_EXAGGERATION, strength=DEFAULT_STRENGTH,
          long_side=LONG_SIDE, light=DEFAULT_LIGHT):
    """Convenience: raw ``.tms`` bytes -> an RGBA relief overlay for the whole map."""
    mesh = TerrainMesh(tms_blob)
    return to_overlay(shade_image(mesh, exaggeration=exaggeration, light=light,
                                  long_side=long_side), strength=strength)

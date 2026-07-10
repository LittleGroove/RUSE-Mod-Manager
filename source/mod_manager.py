"""
R.U.S.E. COMPAT Mod Manager
============================
Unified mod manager with four tabs:
  Mod Manager  — deploy / restore mods to the live game
  Convert      — convert old-style mod folders to .rmod
  Create       — build new .rmod files with a dat explorer
  Settings     — configure game root and folder paths

Copyright (C) 2025 the RUSE Mod Manager authors.

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version. This program is distributed in the hope that it
will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License (the LICENSE file) for details.
"""

import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, font, ttk

# When running as a PyInstaller --onefile exe, __file__ points to a temp
# extraction dir; sys.executable is always the real exe location.
if getattr(sys, "frozen", False):
    _LAUNCH_DIR = Path(sys.executable).parent.resolve()
else:
    _LAUNCH_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_LAUNCH_DIR))

# build.py bundles the shipped mods INTO the onefile exe; PyInstaller extracts them under
# sys._MEIPASS/bundled_mods/{compat,public}/ at runtime.  These are authoritative (always applied,
# hide any external mod of the same name + major version) to keep multiplayer consistent.  Running
# from source there is no bundle → None → mods load normally from the external mods/ folder.
_BUNDLE_ROOT = getattr(sys, "_MEIPASS", None)
_BUNDLED_MODS_DIR = (Path(_BUNDLE_ROOT) / "bundled_mods") if _BUNDLE_ROOT else None
# Auto-deployed 'unofficial patch' rmods baked into the exe (predeploy/<branch>/*.rmod).  They apply
# before everything else on every Deploy, never appear in the manager list, and aren't toggleable.
# In dev (no _MEIPASS) we read straight from the repo's predeploy/ folder.
_PREDEPLOY_DIR = (Path(_BUNDLE_ROOT) / "predeploy") if _BUNDLE_ROOT else (_LAUNCH_DIR / "predeploy")

import i18n
from i18n import t          # every user-facing string is t("English") — see i18n.py
import ui_util              # pixel-accurate, language-aware widget sizing (see ui_util.py)
from ruse_mod_engine import applier as applier_mod
from ruse_mod_engine import mod_project as mp_project_mod
from ruse_mod_engine import path_map as path_map_mod
from ruse_mod_engine.converter import (
    scan_mod_folder, run_conversion, update_rmod,
    name_to_id as _name_to_id,
    sanitize_filename as _sanitize_filename,
)

_SETTINGS_FILE  = _LAUNCH_DIR / "settings.json"
# Bumped whenever the apply/deploy engine changes how it generates dats WITHOUT the rmod bytes
# changing (e.g. an applier bug fix). It's folded into the deploy cache key so those fixes
# auto-invalidate stale cached deployments instead of silently re-serving the old, broken dats.
# 2 = surgical import/export ordinals now kept dense + instance TransRefs remapped (see
#     project_surgical_import_density: fixes the Fortress/Overlord import-table OOB crash).
# 3 = import removals + dense renumber deferred to AFTER instance patches (phase B), so a
#     re-pointed instance is seen before its old import is dropped (no dangling refs).
_ENGINE_DEPLOY_VERSION = 3

_MGR_STATE_FILE = _LAUNCH_DIR / ".manager_state.json"
# Serialises every read-modify-write of _MGR_STATE_FILE.  Worker threads (_do_deploy / _do_restore →
# _mgr_set_deployed_dats) and the main thread (_save_mgr_state, the 15 s poll) all rewrite this file;
# without the lock two overlapping RMWs can lose an update or tear the JSON, corrupting the deploy
# tracker that the NEXT deploy relies on to clean up leftovers.
_MGR_STATE_LOCK = threading.Lock()


def _atomic_write_json(path: Path, obj) -> None:
    """Write ``obj`` as JSON to ``path`` atomically: dump to a sibling ``.tmp`` then ``os.replace`` it
    into place.  A crash/full-disk mid-write leaves the old file intact (or the ``.tmp``), never a
    truncated ``path`` — which every reader would silently treat as ``{}`` and thereby wipe the user's
    whole per-build mod configuration.  Callers must already hold ``_MGR_STATE_LOCK``."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_PROFILES_DIR   = _LAUNCH_DIR / "profile"
_PROFILE_AUTO   = "Auto"   # 'Set Backed-Up Profile' default: apply the newest applicable backup
_OG_PROFILE_PREFIX = "v3591"   # the OG compat build; its lvl1/lvl100 career presets (profile/v3591-lvl*)
                               # work on ANY build — an older profile upgrades forward on next launch

# Far-right per-rmod "cache this prefix" toggle shown in the mod list when "Per-mod cache points" is on.
# Both markers are the SAME length (11 chars) so _mgr_lb_click can locate the click region by measuring
# the text before the trailing marker; rows are left-justified to _CACHE_MARK_COL so it sits to the right.
_CACHE_MARK_ON  = "  [✓ cache]"
_CACHE_MARK_OFF = "  [  cache]"
_CACHE_MARK_COL = 46

from ruse_mod_engine import steam as _steam_mod
from ruse_mod_engine import dic as _dic_mod
from ruse_mod_engine import game_versions as _gv_mod
from ruse_mod_engine import migrate as _migrate_mod
from ruse_mod_engine import mod_format as _mod_format


def _find_steam_profile_dirs() -> list:
    """Return all Steam/userdata/*/21970/{local,remote} dirs that exist."""
    return _steam_mod.find_steam_profile_dirs()


def _detect_mod_folder_version(mod_folder: str):
    """Return 'compat', 'public', or None based on .dat paths found in mod_folder."""
    mod_root = Path(mod_folder)
    for dat in mod_root.rglob("*.dat"):
        parts = dat.relative_to(mod_root).parts
        if any(p in ("99", "1360") for p in parts):
            return "compat"
        if "190852" in parts:
            return "public"
    return None


# A mod version is "x.x.x" — three dot-separated numbers, each one or more digits (e.g. 1.0.0,
# 12.4.30).  Anything else (blank, "v1.0", "1.0.0-beta", free text) normalizes to the 1.0.0 default.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _normalize_version(s: str) -> str:
    s = (s or "").strip()
    return s if _VERSION_RE.fullmatch(s) else "1.0.0"


def _major_of(version: str) -> str:
    """MAJOR version number from a version string, for the _V# / -v# suffixes.  Tolerates 'v2',
    '2.1.0', '  3 ' → '2','2','3'; falls back to '1' when nothing usable is present."""
    head = (version or "").strip().lstrip("vV").split(".")[0]
    digits = "".join(c for c in head if c.isdigit())
    return digits or "1"


# ── Window icon (base64 PNG, written by make_icon.py) ────────────────────────
_ICON_B64: str = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAZjElEQVR4nNV7CZhVxbXuX1V7OvPpc3puaKBBUASViFfQ6xhRjOMXLqLGRM1VTOKUeH0v5kVDjLnve7nv+oya9xL1xiFqfIrxJhoThxiNRAERJ2QQsIEe6eHM4x6q6n21TzeCQZv5y1vf131299m7qtaqtf61aq21gUNIUoK8fd+x+m6/A4j6HoeYtEM0D3n11VMYIX/xgNXuigfbT08m9G8NZvlLFOKUoKY9Ri7r/KNiXwno2MWrPUKUTA7BwnCQ6amnwC66CFxdr3zk8CRQuV5KcltdhNFMgcM0CAyNoFjmz4G4Vx5/eV/q08/9fykAKUHxQ4DcDrFwIdh/PX/CbZLjukiYJXNFAdeTXNcIYxTcdkEjQUKqtkxL4J6s6907/+qetDKJpUtBD6YgyIEeUNny6vuO1WZfs9pVf7/3+MSzKKM3BSxyZibP4brSAyEsFgIp20ChDNTHgHIFnDLCIkEC25UpInHlMV/Z8pw/psIG4i9W/l0L4Kmd1Hb5g5PmhUL4FyFwFiVAsSJczokWDdXmXLEWePiPHB9uBX70dYZ5swEuIItV6RmM6LpGQAhelELedcxXtr6onjkY+EAOFOMLF0Kohb18b2syEjGuI8Bt4SBluZIQnAOmAVoXBl57H3j4eY43P3AhmQZd4xAucMJMA5edBRw/ncDxIKsOEAsRQilBqcKfIwcJH8j+qrv/a2RH3nt80nmS4CFTJ8l8ScDzJNc0X93RO0zwzDKB/3jOhmMztHRMhhGIgXMPxVQXsgODADNwxvEMt32V+mZRqoA73sHFB7Kvz739KTsnlHxHSJyldtvxhCsl0eJhEGXjD78ALH3NRmoIiDUlUdc8EWakGcJzQCiDlAL5wU0opvtQyleQTOpYeCrDP51M0BCvCYIdJHwg++vWpKw8FAqw86RUgCakEEDABGEMWLUeePD3Hla8ayPc3Ij61okwQg0KA+E5ZeQGOyG4h0h9OwKRBrjVAkrpbqT6t8Ate0gkDNx6OcO84wBxkPCB7Ktb++4FE28VnrzeNGmyVJVqE4WhgwUtIJUj+NGvPPxphQdimmgZ14ZgchIYM+HaJVTygygXhiC4W9tCyhCKNyMYbYIeiKKc7UNpuBO5dBqCAyceRHwg++rWghY5M+27NXBCwEIBYCgL/OZ14Mk/20hnJBLNTUiMmw6mB/2drhaGkR/eBs4dUMJ88KgtQUIID0wzEYjUI5psB6EayvntKAxtPaj4QMYKX087TYWvn+/WGAVeWgX8+BEX6ZSDUMOouteDUoZKMYVSth9OJe9PSSj1d37X2ZQVC19QZjCOULzVF4YSzMHEB7LbXV8CqlT9EzuvjunWlq9xoAcCSDS1IVzfAaqZcKtFVApDPvNSeP6ujknKiJXeS8AIRBGua0Ug2rjf+DCqwWMKQEoQtYbdha+7ujXgmWVyxK1paJo0EaG6cdCtCLhTQTHTh2phCJ5ng/qM11R9z6i2LClcX2j7hw+BK4+/fIOPD2MKQC4BVZ/LJ02aHbLIHaEddi49gLB4ZMSt/VHi6dc9DA+Kndxak6++dimDUnY77HIGlGkA2Y267zHtPz5UbJGWktynO8klSgtGN3i3AliyBPT22yFWPjz5SEL4GgnpuZ6yr5pgVqyV8qHnOVa868KMm2iaMB1WtHnErZV8gKsW074OKtuXcq84H13L3zzjr1gKshM+yFF8KAxuQsHHhzKSSeMTfIiBlB3pRoJML5b5GofrX9wemZVeuHCp2K0A5Ihk/vrbaRGWqX6oUbQ6vHZoWb5O4uHnBd5cw0F0Ey3jx8OKt0EzguCu7at7pTgM7jqglKoJiFDj7VWY4QtLSAmmFrLLNyPfU0KkEJyqO82gwoc2BKONPriWs70Y7hvBhzoNt15OcdJRRAoJSYlMeRw20fQj53x1c35nLdA+vQytwIkE6gWIFrIgX39f4sa7PbgOR7K1vpxom+lJaiikQrWQRiHTo9SdaJoOyyCmJ6jucQmNCkdI2DvrACFSTUyEID7AjhKjknJBQiogsAwBl4uSukc9q54xGNFAiFV1CGGUSsZQsUtZz6kUiGI+GG1GpPEwaGYExeGPUS7k2U0/lcEbL6Vk8bkUJZs2AKK8O7Frn7EdrpCAZQCrNgjhVjQ696SJ6374Nftmu7rRdN0SYaYJp1yA6zlSN0LCKdluqeLyl94LnLx6E/vm/OO8Oxed7r01kGUxCiJUZCg86XJIV9do8JN9JzJk8sqPHtVv5ZL3nDu7+uSUFloiARpkklLuSbecd6p/3ahPe2O9+XWd8shP/tm7oSoMixCP2JV1CIYH/DOFGYoR7oW4dB1v0W3OA6UKWjQGyblUKuXujQDIqFIGTSlVNBerq998yvSVge2VxgcZM6lUSycRCUm4FNKWMPNCiDWLLynf/T8fFhvGRb38lI7IDTND+qmlCndjYaYPDNjPpQbKvx0/OfwAAF+VFdZ3bS3e8r2Li7eeN4c53cXwxaD0JEloGyHEkELkKQLrLzzL/c32nvK3lr7BZk9qZw2RhHE3IaYGhBTUEIBJQqqEe8J2cvIbAVMOEEJadrL33dqjhjFIaYJaqOc5ZddmTBIWU8DuD0sAyvxdVAbfEgkZ0z7oxKyZHflvNEVErlxFMhgmEc8j0DWGZ/9SOOnF11LVx38aDzoudBUPuZ7ELfcMXXTaLP3emTPHf7+uTj+2UhXgCkTUAhiL6zpp96R+VlnH3QuPKz1QLAfmhuKkTtMUONZAUo3DGEGxKiNf/V7PZa6bBCFjxx10zDt26IRCdkgupPC4rH16UpbKnHuehONKkcm5XqJOn/zH5fb1GzuV65KeuheQrvp0PUlsDo2q9XIp1Y8KYnIFzz38sMip4RA7NpPzeNUWwnWlLFcFdzwpCkWuolE5mJXX/pcHMv/Y0kBL/vyitg4upIxFGOrrNCTiOjzBAlzUNnesTDPdYwHsLAq14QQUEuSq73WuenVlricWZtRxQUMWlaZBj/z50+kJoQBzR1zB6DNqQj9AHflbRcWEe9KJRLTxjCqUFzIeZfTlN7N9F3xj45tSSGqZVFOMtDQa2kddznHDGW6ozahFucqUJPn1c6meux/qXXf/453vDQ9WC56QkREBHLS0uFQMDAx7qZBFMxoj45Rr0HSimSY1PtjsJA1DmeQeDOR7AsoIram0UuWhtEc+3lZZEbLo1FSON6rkKYHgmTyPDAx6erJZhRm1I4Qysfuf6Fu7ZlOA0eS0mckEbfR6uxM18ycHry4gpDoUcR40idohf/GuJ2Wh5MlCBfqeRAFqiZSBVWwv7XlC6QXL5j154RmJ1rakNn7Vu+lLrvx+72Km08ayTaxSSXDDVJLaAW6+HBiDnHlk4rW6KUdusEhu0ku9fV/ybxrDBDTsJy25ru2Y8a1WuFDmCAWZpmZ7/6NKdnwTlerQNFYsJP0EimZt6yqvsmfFFlomDdiOENEww/zTEhcPp+2Ge7/fcvf2fq96zx9i97RSnjrxKJnpKRJN12vcMyZxzcWtR5482xicPv3VNehJPUb+ED1VEhYa8QLkgAtASihQxKJz6tsV85xLdA865f94cqjztRX5oRsW1WWqtqBm4PNhRkU7ugYrmymupcL5iQfzhwGT0UqVC9claGm0vhgNaUfwavXWxgbn2mVr6eyuYWppISU6lVQAKCFYMD8x3rLo11au1u0f3p3977WE5dhE91UAo0LoG3K469YO3srXN9TrdP4Joee/dm60s1IVxh4NRFD9oCs27/nX3VXvvpf7174B26uLaZRSiFzR45rBWqVhPTBxPJ39k8tKz+YKMHfHnKFTfyPe31iZAwJ2sLyATwoAlQAWf7/zrWWr8r2hIEU4qGm3LG6ZftkFDR3besoV31PsAWlUiO5BSl9/nzWcOCX7iyd+0/eVFe8W1lgmZYwSWqlwEQoyRg3zR7c9XJ1VF6HlnRgjCg2f/EO6+64Hezc88buutcSz03vgAGpzYz+9QP+Qmw6YyGsaaXMczoWg2oed7gWvvJpa9uzJrdWxBlHqW6yA3nIl7ztvnjW7q7/1tP/2bb24bkNh6cp3chtnzYwsUGd7FRs0N+rm1kFx8Zbe6jNTp5k+fzUvAPyfx3rXre80bZY8/PBxzdpx2Nxr7okIKPaTTJ0gaFE+CjUqOqNEkg82i3qTUffTk6n02S5EANsWZaqZ/8B047pEnXF5Xcy4tlAll/77z7v+067yjZZJiYoMTYNIy2ItK9ZUWnSdeiP8+crQ2sj4tCNb2ZQZR00M1R82gRIl2rFJ218BjLq/0euRPCe1hQ+Szg53pXZKAKWyIIrUv/10h5RwPKnOz9l0zvVKZS4NA+SDj+ymZR+6R8QjdNiTmKryeoQQqgqq24e9CGMQo8cbNRpjRt2iE3MPzD121f3HH47sEZfyf/EkO19jfmJUO3T9AdLnVfHmECIH/egGUOVvnDQ7nDi8wzy5aktT1Q80jchyhcvBYddzPTnocennAkpliVOOjwSfuKvj7KpLpgmVLBWgSgsGUp4XtVSo5JugL3SNEdyzZOI/WDqZYZoUUbOyJJ3j3YyqU/Tn5+HoXjKnIgsV+6sjplSfI9urIliuYvKRGF+EAghETfmHdNZ1o2GmOY4UHeOt4InHRifajlCmIJNxRp79U6Y3XxKFtiTesG25JRHXiXq+rUk3zzgh/gWXI8Q5ZF1MI0Mp13vrg1KmvVW3XQ6x8zpMnVIhSYQLErnprqEL3aJ9lMIOLj6fR7qnvBNJWK7KAtGIrjXWG6QxadCmeoPojBDOZTReZ7BE3NDjDSaJR6kDGfvS6nVeziuVb9i41c6r5KBhUN9VRSMaUYfpXz493Hv7z3pWt7Umx7/1kdnYbBWuX7+53O1xMMtkUM0DsbCmniEfbiwXb/zx1ndKRa971rRI1tBoeOd1JOt09YOGhAHKmKXgZk/40vaIe796Ixta5mQ2da8K3f/g0uwJKiOrQuH+oYozoYH/btnyodSflhemN9QZ7htvpXvLXuSwK/+N3HPjOeSWbKZv8dZheWF9vTXJMIi1fcjhy98rpFatcbJNzfF4mTad9L+fw2HpovjejNahxb/5PV8QiugzQkEayOQ41m6sZF95szBc9ozIKXPGB8/8ivlW7+ryfb9cmj1RrWPnoIqC03feG+oneqJBrW+/BSDVmNLDYCF4zBmXnfHP6z9cg74tn4C7FmqccdEPSHpztw1RcilgA0akLZqMxDZ188k3/1I+O6Wt7bVixd7S3ZvPlTIVDWBmrKEh0TbJnFJ2A5NcT2B7Wrb+4nnrkbaEttyx3Q8HU24qk/OaYTsWNC3aNm5CIsCDR2zo45VpxxeKwxlB0tv9U/JO+XYCRMchmWifDNrfoPLmKpO4XwKAj+sCJceM59PG1WUeRnJCAIRQv6pLmYGMy6ZEExWYzQ0wTRPFUhmlYgnNTXFOqM7e3dx/ejBgoa65AR1HxJHNZjA8nEbBMfyCiWWZSt1lqVQh24atuZYVnwuriiCpoLGhA1YghJ7ePiiG9HA8glBicVSvwmOD0PVRFpSWamicMMu/TvWlQMAP3GGIQEgiqqqbgbiCU4WwmWwGN33nJixadBE/7fTT5NVXXaWde+458uz5870FC76sT506jTlO1dne309ffOlldu89P5UvvPCCO2XyZPnc739vrFr1NrVtG1dffTUWLFhA5s07g197zTfJueeeK84680xx1vz52tFHz6L5bMo2TNN78qmnQ3feeSdefunF6uSODv7+B2sC//fJp2goGJBCchAiUK2UVZJVOefRwuPnkranAqgNRrSaQNWlOrmqnAig6TqbNb3j/amHTZleLhdFR0v0nfPPP/+Eby2+oiufGc4/8utnDuvt3pqtVivR++78wfIzz7/0iMsuWWSsXP5XQQhLKl9iWaacNX3Smskdk6eXSgXRmjBWX3LxJSd8+4Zv9vZ9/EHaijaZDQ31kkBO+vm/37bs5HlfnvGt669zfv2rB/IyFJoAlQD3IwzqZ5L3lCltLwTwiRxGrwiB53FsWL/W/tJ5F8ZK5TK6tm3j046YERoeHKikt3dts+Lj5mzr6dWnTZnQ6bmuHks0GX2DmaZorK5oEO8jh8sE55yMjlEeGWPq4dMD6fSw3b91w8cXX3XTnFAoZrzzzirOmCZvWfI/prRMPLzliccfG+CVTA9B6wQ//Nij2G+f4wBZCwQkHwn3VFGSy1A4hI/Wrxs+8eQvtgwODsCxbdHT3e21t4/3onXJsGYYWvv4dnR+vFWzTAO59IDb2NjIi8USK+fSVZUVDIbC+Gj92toYQwNwq47s7urm9fX1bkNzW/j1vyzTFi640JkyqT1VLBa8d99emeqYML4yONBfhh6bUKs2K4gYBWd5gAVA/ESNDzKGFYVwHT+EVYc9zgX6e7vKmzZuqGxe+85HjFJn/bo19pNPLjW+ffMtTTfeeKP8859fkX9d9krKsoL6tTf/oH3evDPZo796sCAhNco0yjlHf2/3jjEohb2l82P3V48+Zi7+xg0tFy1aJDZt7jQ2r3u3ExDVF5//z8IvfnGf9p2bvxsPxxMRz7H9NYbjbSPJRhUBqjPJHrCGUZmNlItWPDolKj23C4TE4iHIu5YKcv8zHBNnzIARafWRv5Dq8psdXKcqg+EoqeYHNygzdir5wVC8sbWUHRqQRuzoxmR8iEgv3bNtC6xwLFDX0NoeNfnqVK5SP9zXVQwmW8ZpeiBpWiaq2cENkhLLKe8YY7s04rPq6yLpgEmHevv6iVstFZPNk6bmhrvW6dHWuUGDDOaGujZHmo44PpxoY1Yo6WuoVx5A5/trcOUFDN+9lMpMUTVzyRzR9PYxS2MjpKtoWwUWARVTqcTHx2sQifciVD8J8eapcGPNKOcHSSHTC0nNqaoEbhrhdpcLYsaaWxjTWKZQbRHcSUQbJ6iql5kvFJFK2ccwRr1Y80RTKpWSEpVyBTDCU9XEZizU7nK5Y4x82W1I5+yYHohzM1wfqDgezFjLcWojXEQam6aeXmdFElRVorlTRrpvHdJ9g9AtiuMOJ1DlcqUJQmK3Tdra7v4JIE2kKBUrJHnZGRQTGgn58SMCqb4h5LMZJFsyCCXGI9aoWt2iKGV6qe13f0gwTVfmwYQQ0DQGoofM2hFCSE3TVA3RVHz6p4jaUcKv5Y/8rqXaNP/TH0PFB5oWNGqNE9JnhmqmFgjXI1TXCqZbumsXURrejP7uXsCz8Y9Ha7j8HIq50wlyJcBgUh1E1RlSfqYAiFISv7dmVqnZefcLhLivhIO0oVDm7he/QPSjJjM8/bqFpa9xbN+6CaF0D8KJVkQbD4MVSqBSGEYp2we7nN2lL0Dt1OgUNX53WcMnVqru/eRyl3v8p7wawFnhBKL1E6AZIV/dK9luDGxbDztrY84sHVeeY2LOkbUHs0UIXZNcgmiUsC/NvfCjgmoBIKTW/fK3DRIjtqFaSlwjdTsh8pqASROFspSGBnXCY8M54I5HBf60kgPcQbypEZGGiX6FVkV1+VSXLwy+T50h+Jy+oTq/U8QM1fkCtgsDyGzfitxACvWNFP90soYrziaIBIFsQU0oua4TLRFlKFXlS6WqvG3uli1v+8Pe/hkC+DTt3AcohESuJKWqGBsayMp1Eo+9CLyxxvHrg7FEAqH6DgTjrXBVvT4/4HeK1HqDRs1P7nVniKaZsCINfq8QMwK1XqFMDwa2bIVherjqPBNfPomgrR6+uu9o4wlTFIo8RRh+9m/Pbrtj6VLwT3eHfK4Adm4sGu0ElRJnqSKkw6Ubtoimylov+91hHOm0Az2oIdkyyccH1Suk+gFV84TfHeabuV9JxVg02lAVirf4DZS6FYbwbBSHO5Ee6IVbqWDuTANXnMNw6tFApgjYDvwSfCxEabEsuATuIMT62Wh/0M6NXzsT2dde4EJZqhQ0V2ah+gOffl36+JBKuQhFAzvwQZnBZ+HDritRIKfWJ31g9ZkPJ1WwBac0jOG+rSgNDSKRVN1hOs48zu8uR74EyZj0wgGqq+MvpXixVMKdc7++5WU17KuvnqKddtpf1KF5t5Ine9sN/sID4xJxXbueADdYJjkw+CAl1GGGMcMHOCtS7wuKu2Wke9YhvX0AiTqCRaebWHAyRvsD1WNc18GUnZer8iXBxf/apXX2mtXeWD3EZE8EsPs+4daklPp+4kMtrKZMRzDS4PcD6mYInNsop7agv6cX0rZxxhwNP/iahmRMolwFHNWdSlUlmhDbFimqkXt/8rutP/btfMlIS+9OSH/ABKBIAcnq+/+2U3yv8aEwhMJwl7/TscYOv+FKuTWnNOSre3H7IObMMvH1czUcdwSg6owVG1KF/ZEg9Uvtn+4D3Jf3CAj2kQ4EPvjHacFBNQN2YfsOt5ZsABaeauKK+YpZ359LQqRnaFRXQEd33wnqp4cO+RsjT+0HPoST7WBMg1PJob/zYxgmH3FrFG31che3Fg3Rg/IuEdlfAewrPryp8EFX/QRMtYjghKP0Md1aoeD8bN71fakD+TYZwQGkPcUHpvDhbeAHD3LMmAhccTbbEb6O5dYO9PuEBAeBxsIHwSUPjpiFsvGg6UdxfrZFha91UYbKPrq1v6s3R58aAx9MHYILsNGXKHcbvu6lW9tbIjgE9Gl8gNQfCgfZeaooajsSdRGGTIELQuQdQODe/XFrf7ckP/Xm+PuPdZy9/OH2x994eOLiTb+d/LR6oXr0OxW+HqrNwaEmJYjPat9RAjrUr9D/P1zBFmoLDN4EAAAAAElFTkSuQmCC"  # generated — do not edit by hand

# ── RUSE Military Theme ───────────────────────────────────────────────────────
# Dark navy command-room steel, scratched metallic silver text, military gold
import theme                # single source of truth for the palette; local _R_* names kept unchanged
_R_BG        = theme.BG
_R_BG_PANEL  = theme.PANEL
_R_BG_WIDGET = theme.WIDGET
_R_BORDER    = theme.BORDER
_R_GOLD      = theme.GOLD
_R_GOLD_BRT  = theme.GOLD_BRT
_R_RED       = theme.RED
_R_GREEN     = theme.GREEN
_R_TEXT      = theme.TEXT
_R_TEXT_DIM  = theme.DIM
_R_SEL_BG    = theme.SEL_BG
_R_SEL_FG    = theme.SEL_FG
_R_BTN       = theme.BTN
_R_BTN_ACT   = theme.BTN_ACT

# Log tag colours — SEMANTIC by state so the user can read progress at a glance:
#   step = IN PROGRESS (cyan), ok = DONE (green, reserved for real completion),
#   warn = warning (yellow), err = failure (red), head = section title, info = neutral detail.
_DARK_BG  = _R_BG_WIDGET
_COL_INFO = "#ccd8e8"   # neutral detail
_COL_STEP = "#49b7cc"   # cyan — an operation IN PROGRESS / a progress milestone (never means "done")
_COL_WARN = "#e6c62a"   # yellow — warning
_COL_ERR  = "#cc3030"   # red — failure
_COL_OK   = "#46b04a"   # green — DONE (only for actual completion messages)
_COL_HEAD = _R_GOLD_BRT   # gold — section title

# Fonts
_F_MAIN = theme.F
_F_BOLD = theme.FB
_F_HEAD = theme.FHEAD
_F_LOG  = theme.F

# Strips the leading "N. " from a shared-load-order line, leaving "Name" or "Name | vVersion".  We then
# split the version off the RIGHT (rsplit on " | v") so a mod NAME that itself contains " | " survives.
_LO_NUM = re.compile(r'^\d+\.\s+(.+)$')


def _is_dir_safe(p: Path) -> bool:
    """Path.is_dir() that returns False instead of raising when the path sits on an
    unreachable device — e.g. a removed/ejected drive (WinError 433) or a dropped
    network share.  Steam keeps recording an install's location after its drive is
    gone, so any probe of the game root must tolerate the device simply not being
    there and treat it as 'not found' rather than crashing."""
    try:
        return p.is_dir()
    except OSError:
        return False


def _exists_safe(p: Path) -> bool:
    """Path.exists() that returns False on an unreachable device.  See _is_dir_safe."""
    try:
        return p.exists()
    except OSError:
        return False


def _detect_game_version(data_path: Path) -> str:
    """Return 'public' if this is the Steam re-release (PC/190852/), else 'compat'."""
    if _is_dir_safe(data_path / "PC" / "190852"):
        return "public"
    return "compat"

# ── Shared helpers ────────────────────────────────────────────────────────────

# The Mod Editor's log windows are "mirrors": they show ALL activity — every per-tab _log() message
# plus all Python-logging output (including Pillow's DEBUG flood) — and are never cleared.  The per-tab
# logs (Mod Manager, Convert) stay scoped to their own messages.  Widgets register here once built.
_LOG_MIRRORS = []


def _register_log_mirror(widget):
    if widget not in _LOG_MIRRORS:
        _LOG_MIRRORS.append(widget)


def _append_raw(widget, msg, tag="info"):
    widget.configure(state="normal")
    widget.insert(tk.END, msg + "\n", tag)
    widget.see(tk.END)
    widget.configure(state="disabled")


class _TextHandler(logging.Handler):
    """Routes Python-logging output (including Pillow's DEBUG lines) to the Mod Editor's "all logs"
    mirror windows.  Deliberately NOT pointed at any per-tab log, so the Mod Manager / Convert logs
    only ever show their own messages."""
    def __init__(self, root):
        super().__init__()
        self._root = root

    def emit(self, record):
        msg = self.format(record)
        level = record.levelname
        self._root.after(0, self._append, msg, level)

    def _append(self, msg, level):
        tag = {"WARNING": "warn", "ERROR": "err"}.get(level, "info")
        for w in list(_LOG_MIRRORS):
            try:
                _append_raw(w, msg, tag)
            except tk.TclError:
                pass


class _ThemedScrolledText(tk.Text):
    """tk.Text with a ttk.Scrollbar so it picks up the TScrollbar theme style.

    Drop-in replacement for scrolledtext.ScrolledText: pack/grid/place calls
    are forwarded to the internal container frame so callers see normal geometry
    behaviour, while all Text widget methods work directly on the instance.
    """
    def __init__(self, master=None, **kw):
        self.frame = ttk.Frame(master)
        _vbar = ttk.Scrollbar(self.frame, orient="vertical")
        _vbar.pack(side="right", fill="y")
        kw["yscrollcommand"] = _vbar.set
        tk.Text.__init__(self, self.frame, **kw)
        self.pack(side="left", fill="both", expand=True)
        _vbar.configure(command=self.yview)
        # Forward geometry management to the container frame
        for _m in ("pack", "pack_configure", "pack_forget", "pack_info",
                   "grid", "grid_configure", "grid_forget", "grid_remove",
                   "grid_info", "place", "place_configure", "place_forget",
                   "place_info"):
            setattr(self, _m, getattr(self.frame, _m))
        # destroy must NOT be forwarded directly: frame.destroy() cascades to
        # this Text widget's destroy(), which would call frame.destroy() again.
        # Guard against re-entrancy so external callers still get full cleanup.
        _guard = [False]
        def _guarded_destroy():
            if _guard[0]:
                return
            _guard[0] = True
            self.frame.destroy()
        self.destroy = _guarded_destroy


def _make_log(parent, height=12) -> "_ThemedScrolledText":
    w = _ThemedScrolledText(parent, state="disabled", font=_F_LOG,
                                  wrap="none", height=height)
    w.tag_configure("info", foreground=_COL_INFO)
    w.tag_configure("step", foreground=_COL_STEP)   # in-progress / milestone (cyan)
    w.tag_configure("warn", foreground=_COL_WARN)
    w.tag_configure("err",  foreground=_COL_ERR)
    w.tag_configure("ok",   foreground=_COL_OK)
    w.tag_configure("head", foreground=_COL_HEAD, font=_F_BOLD)
    w.configure(background=_DARK_BG, foreground=_COL_INFO,
                insertbackground=_R_GOLD)
    return w


def _log(widget, msg, tag="info"):
    # Tkinter is single-threaded: a widget may only be mutated on the main (interpreter) thread.
    # Worker threads (deploy / backup / restore / convert) log freely, so marshal to the main thread
    # HERE — one safe choke point — instead of wrapping every call site in ``self._ui(…)``.
    # after(0) callbacks run FIFO, so message order is preserved.  This is what makes the deploy/backup
    # logging thread-safe; before it, cross-thread widget.insert() corrupted Tcl state and crashed the
    # app mid/near deploy.
    if threading.current_thread() is not threading.main_thread():
        try:
            widget.after(0, _log, widget, msg, tag)
        except (RuntimeError, tk.TclError):
            pass   # app shutting down or widget already destroyed — drop the line
        return
    try:
        _append_raw(widget, msg, tag)
    except tk.TclError:
        return
    # Mirror every per-tab message into the Mod Editor's "all logs" windows — but never back into the
    # same widget (a mirror logging to itself).
    for m in _LOG_MIRRORS:
        if m is not widget:
            try:
                _append_raw(m, msg, tag)
            except tk.TclError:
                pass


def _log_clear(widget):
    widget.configure(state="normal")
    widget.delete("1.0", tk.END)
    widget.configure(state="disabled")


# ── Main application ──────────────────────────────────────────────────────────

class ModManagerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()   # stay hidden until startup finishes; deiconify() at the end of __init__
        self.minsize(960, 680)
        self.resizable(True, True)
        self.configure(background=_R_BG)

        self._set_window_icon()
        self._apply_titlebar_theme()
        self._settings = self._load_settings()
        # Load the UI language catalog before any widgets are built (stable keys with English
        # fallback, so this is safe even if lang/ is missing).  Changing the language prompts a restart.
        i18n.load(self._settings.get("default_language", "us"))
        self.title(t("mgr.r_u_s_e_mod"))   # after i18n.load so it localizes
        if getattr(self, "_lang_autodetected", False):
            self._save_settings()   # record the OS-detected language so it persists from now on
        # In-exe auto-update.  Split in two so the interactive part can't hang startup:
        #   * housekeeping (sweep the old exe / heal shortcuts) is non-interactive → run it now.
        #   * the version check SHOWS A MODAL Yes/No prompt, so it must wait until the main window is
        #     actually on screen.  Shown against the still-withdrawn window (as it used to be) the prompt
        #     had no taskbar button and could open buried + unclickable, wedging its wait_window loop —
        #     the "hangs when there's an update, won't open" bug.  It's deferred to after deiconify()
        #     via self.after() at the end of __init__, so it runs in the live event loop over a real,
        #     foreground parent.  Skipped silently for dev runs, offline users, and up-to-date builds.
        import auto_update
        auto_update.run_startup_housekeeping()
        if not self._settings.get("game_root"):
            self._auto_detect_game_root()
        self._selected_mod_build = None   # default: follow the INSTALLED build; must precede _bootstrap_folders
        # Baseline for the 15-s version-check poll: the last installed build it acted on.  Kept SEPARATE
        # from _mgr_current_ver (which tracks the VIEWED build, and may be a user-selected non-installed
        # one) so the poll only refreshes on a real installed-build change — not when the user is just
        # viewing another build's mods.
        self._last_installed_ver = self._version_subname()
        self._bootstrap_folders()
        self._apply_theme()
        self._mgr_running  = False
        self._conv_running = False
        self._mgr_mod_vars: list = []     # [(BooleanVar, path), ...] — ALL mods, always
        self._mgr_current_ver: str = ""   # BUILD-ID key (v<buildid>) of the currently loaded mod list
        # _selected_mod_build (set above, before _bootstrap_folders): build id whose mod library is being
        # viewed/used.  None = follow the installed game.  Lets the user pick a different build's mods
        # (mods/v<id>/) and deploy them onto the installed build via the version maps.  Deploy target /
        # cache / backups always stay keyed to the INSTALLED build (_game_build_id).
        self._show_compat_var = tk.BooleanVar(value=False)
        self._scanned_compat: list = []   # Path list from last scan of mods/compat/
        self._scanned_public: list = []   # Path list from last scan of mods/public/

        # Shipped (bundled) mods baked into the exe by build.py — authoritative for multiplayer:
        # always applied, can't be disabled, and they HIDE any external mod with the same internal
        # name + major version.  Empty when running from source (no bundle) → mods load normally.
        self._bundled_order: list = []    # ordered bundled rmod path strings (display/apply order)
        self._bundled_paths: set = set()  # those paths, for fast membership
        self._bundled_keys: set = set()   # {(branch, name_lower, major)} a bundled mod covers
        self._bundled_meta: dict = {}     # bundled rmod path -> {size, mtime} (build-time, manifest.json)
        # Auto-deployed 'unofficial patch' rmods baked into the exe (predeploy/<branch>/), branch-keyed
        # and prepended to every Deploy.  Invisible in the list; aren't toggleable.  See _scan_predeploy.
        self._predeploy_order: dict = {}   # build id -> ordered predeploy rmod paths
        self._predeploy_meta: dict = {}   # predeploy rmod path -> {size, mtime} (build-time stamps)
        self._mgr_cache_flags: dict = {}  # rmod path -> BooleanVar: cache the prefix ending at this mod
        self._mgr_state_loading = False   # True while restoring from disk — suppresses the auto-save
                                          # that otherwise fires from every rebuild/redraw (see below)

        # Mod Editor project state (None until a mod project is created/loaded)
        self._project = None

        self._scan_bundled()
        self._scan_predeploy()
        self._build_ui()
        self._load_mgr_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._auto_detect_job = self.after(15000, self._auto_detect_poll)
        # Centre the MAIN window on screen before showing it.  Tk otherwise leaves an unplaced window at
        # the WM's default (often a monitor corner) — and since every popup centres over THIS window, a
        # corner-placed main window makes all the dialogs look corner-placed too.  Move-only (+x+y keeps
        # the natural/content size); a third down reads as centred.
        try:
            self.update_idletasks()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            x = (self.winfo_screenwidth() - w) // 2
            y = (self.winfo_screenheight() - h) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass
        self.deiconify()   # UI is fully built — show the window (paired with withdraw() at top of __init__)
        # Now that the window is mapped and about to enter mainloop, run the in-exe update check (see the
        # split note near the top of __init__).  Deferred so its modal Yes/No prompt has a real, visible,
        # foreground parent — never the withdrawn window that used to let the prompt open buried and hang.
        # A short delay lets the window paint first; the check is a silent no-op unless a newer release
        # exists (dev runs / offline / up-to-date all return immediately).
        self.after(200, lambda: auto_update.check_for_update(self))

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _bootstrap_folders(self):
        """Create required folders next to the exe on first run.  A failure here (read-only drive,
        permissions) leaves the app without its working folders, so surface it instead of swallowing it
        — otherwise the first deploy/backup fails later with a confusing secondary error."""
        try:
            mods = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
            mods.mkdir(parents=True, exist_ok=True)
            # mods are keyed by build id now (mods/v<buildid>/, created lazily by _mods_dir);
            # the legacy compat/public folders are no longer created.
            if self._game_build_id():
                self._mods_dir().mkdir(parents=True, exist_ok=True)
            if self._settings.get("working_dir"):
                self._backup_dir().mkdir(parents=True, exist_ok=True)
                self._mod_out_dir().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # Always log the real traceback first: this runs before _build_ui, and the friendly dialog
            # below can MASK a plain bug (e.g. an AttributeError) as a "folder" error — leaving a trail
            # makes such early-init bugs diagnosable instead of a mystery hang.
            logging.getLogger(__name__).exception("bootstrap_folders failed")
            # The log widget doesn't exist yet (called before _build_ui), so surface via a dialog.
            try:
                ui_util.warning(
                    self, t("mgr.couldn_t_create_working_folders"),
                    t("mgr.mod_manager_couldn_t_create", err=str(e)))
            except Exception:
                pass

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        # Point the centralized themed dialogs (ui_util.info/error/warning/confirm/show_text) at this
        # app's palette, so every popup matches the dark-navy + gold theme instead of the OS default.
        ui_util.configure_dialogs(
            panel_bg=_R_BG_PANEL, widget_bg=_R_BG_WIDGET, border=_R_BORDER,
            text=_R_TEXT, dim=_R_TEXT_DIM, gold=_R_GOLD, gold_brt=_R_GOLD_BRT,
            btn=_R_BTN, btn_act=_R_BTN_ACT, sel_bg=_R_SEL_BG, sel_fg=_R_SEL_FG,
            font=_F_MAIN, font_bold=_F_BOLD, font_head=_F_HEAD)

        # Base frames
        s.configure("TFrame",
                     background=_R_BG_PANEL)
        s.configure("TLabelFrame",
                     background=_R_BG_PANEL, bordercolor=_R_BORDER,
                     relief="groove")
        s.configure("TLabelFrame.Label",
                     background=_R_BG_PANEL, foreground=_R_GOLD,
                     font=_F_BOLD)

        # Labels
        s.configure("TLabel",
                     background=_R_BG_PANEL, foreground=_R_TEXT,
                     font=_F_MAIN)

        # Buttons
        s.configure("TButton",
                     background=_R_BTN, foreground=_R_TEXT,
                     font=_F_BOLD, bordercolor=_R_BORDER,
                     relief="flat", padding=(8, 3))
        s.map("TButton",
              background=[("active", _R_BTN_ACT), ("pressed", _R_SEL_BG),
                          ("disabled", _R_BG_PANEL)],
              foreground=[("active", _R_GOLD_BRT),
                          ("disabled", _R_TEXT_DIM)])

        # Entries
        s.configure("TEntry",
                     fieldbackground=_R_BG_WIDGET, foreground=_R_TEXT,
                     font=_F_MAIN, insertcolor=_R_GOLD,
                     bordercolor=_R_BORDER, selectbackground=_R_SEL_BG,
                     selectforeground=_R_SEL_FG)

        # Combobox
        s.configure("TCombobox",
                     fieldbackground=_R_BG_WIDGET, foreground=_R_TEXT,
                     font=_F_MAIN, background=_R_BTN,
                     arrowcolor=_R_GOLD, bordercolor=_R_BORDER,
                     selectbackground=_R_SEL_BG, selectforeground=_R_SEL_FG)
        s.map("TCombobox",
              fieldbackground=[("readonly", _R_BG_WIDGET)],
              selectbackground=[("readonly", _R_SEL_BG)])

        # Checkbutton
        s.configure("TCheckbutton",
                     background=_R_BG_PANEL, foreground=_R_TEXT,
                     font=_F_MAIN, focuscolor=_R_GOLD)
        s.map("TCheckbutton",
              background=[("active", _R_BG_PANEL)],
              foreground=[("active", _R_GOLD)])

        # Notebook
        s.configure("TNotebook",
                     background=_R_BG, bordercolor=_R_BORDER,
                     tabmargins=[2, 4, 0, 0])
        s.configure("TNotebook.Tab",
                     background=_R_BTN, foreground=_R_TEXT_DIM,
                     font=_F_BOLD, padding=(12, 5),
                     bordercolor=_R_BORDER)
        s.map("TNotebook.Tab",
              background=[("selected", _R_BG_PANEL)],
              foreground=[("selected", _R_GOLD)])

        # Scrollbars
        s.configure("TScrollbar",
                     background=_R_BG_PANEL, troughcolor=_R_BG_WIDGET,
                     arrowcolor=_R_GOLD, bordercolor=_R_BORDER,
                     relief="flat")
        s.map("TScrollbar",
              background=[("active", _R_BTN_ACT)])

        # Treeview
        s.configure("Treeview",
                     background=_R_BG_WIDGET, foreground=_R_TEXT,
                     fieldbackground=_R_BG_WIDGET, font=_F_MAIN,
                     bordercolor=_R_BORDER, rowheight=20)
        s.configure("Treeview.Heading",
                     background=_R_BTN, foreground=_R_GOLD,
                     font=_F_BOLD, bordercolor=_R_BORDER, relief="flat")
        s.map("Treeview",
              background=[("selected", _R_SEL_BG)],
              foreground=[("selected", _R_SEL_FG)])
        s.map("Treeview.Heading",
              background=[("active", _R_BTN_ACT)])

        # PanedWindow sash
        s.configure("TPanedwindow", background=_R_BG)
        s.configure("Sash", sashthickness=5, sashpad=2,
                     background=_R_BORDER)

        # Separators
        s.configure("TSeparator", background=_R_BORDER)

        # LabelFrame alternate casing (tkinter is inconsistent across platforms)
        s.configure("TLabelframe",       background=_R_BG_PANEL,
                    bordercolor=_R_BORDER, relief="groove")
        s.configure("TLabelframe.Label", background=_R_BG_PANEL,
                    foreground=_R_GOLD, font=_F_BOLD)

        # Catch-all: force all native tk widgets (Listbox, Text, Frame, etc.)
        # that aren't individually configured to use the RUSE palette.
        self.tk_setPalette(
            background=_R_BG_PANEL,
            foreground=_R_TEXT,
            activeBackground=_R_BTN_ACT,
            activeForeground=_R_GOLD_BRT,
            selectBackground=_R_SEL_BG,
            selectForeground=_R_SEL_FG,
            insertBackground=_R_GOLD,
            highlightBackground=_R_BORDER,
            highlightColor=_R_GOLD,
            disabledForeground=_R_TEXT_DIM,
        )

    # ── Window chrome ─────────────────────────────────────────────────────────

    def _set_window_icon(self):
        if _ICON_B64:
            try:
                photo = tk.PhotoImage(data=_ICON_B64)
                self.iconphoto(True, photo)
                self._icon_photo = photo   # keep reference so GC doesn't drop it
            except Exception:
                pass

    def _apply_titlebar_theme(self):
        """Colour the OS title bar via Windows DWM.

        Win11 21H2+: sets exact caption + border colours from the app palette.
        Win10:       falls back to system dark-mode (dark grey bar).
        Fails silently on non-Windows or old builds.
        """
        def _colorref(hex_str: str) -> int:
            h = hex_str.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return (b << 16) | (g << 8) | r   # COLORREF = 0x00BBGGRR

        try:
            dwm  = ctypes.windll.dwmapi
            hwnd = self.winfo_id()

            # Win11: custom caption background + text + border
            for attr, value in (
                (35, ctypes.c_uint32(_colorref(_R_BG))),        # DWMWA_CAPTION_COLOR
                (36, ctypes.c_uint32(_colorref(_R_GOLD_BRT))),  # DWMWA_TEXT_COLOR
                (34, ctypes.c_uint32(_colorref(_R_GOLD))),      # DWMWA_BORDER_COLOR
            ):
                try:
                    dwm.DwmSetWindowAttribute(hwnd, attr,
                                              ctypes.byref(value),
                                              ctypes.sizeof(value))
                except Exception:
                    pass

            # Win10 fallback: dark-mode title bar (dark grey)
            dark = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20,
                                      ctypes.byref(dark),
                                      ctypes.sizeof(dark))
        except Exception:
            pass

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_settings(self) -> dict:
        d = {
            "game_root":   "",
        }
        saved = {}
        if _SETTINGS_FILE.exists():
            try:
                with open(_SETTINGS_FILE, encoding="utf-8") as f:
                    saved = json.load(f) or {}
            except Exception:
                saved = {}
        # A corrupt / hand-edited settings.json can be valid JSON but the WRONG SHAPE — a list,
        # string or number instead of an object.  json.load() happily returns those, and then
        # dict.update() blows up, which the outer startup guard turns into "the app won't open".
        # Treat any non-object as "no saved settings" so a bad file degrades to first-run defaults.
        if not isinstance(saved, dict):
            saved = {}
        d.update(saved)
        # Likewise coerce individual values whose type later feeds a Path()/string API: a numeric
        # game_root (e.g. someone typed a bare number) would crash _game_version's Path(game_root).
        if not isinstance(d.get("game_root"), str):
            d["game_root"] = ""
        # First launch (no language ever chosen): follow the OS UI language, falling back to English
        # for anything we don't ship.  The resolved choice is then persisted, and any later change in
        # Settings overrides it.  `_lang_autodetected` tells __init__ to save it once.
        self._lang_autodetected = "default_language" not in saved
        if self._lang_autodetected:
            d["default_language"] = i18n.detect_os_language()
        # A language-change restart hands the chosen language straight to the relaunched process via
        # this env var (scoped to that child only).  It wins over the file so the new instance uses
        # the right language immediately, even if the settings.json write isn't visible to the read
        # yet (e.g. on synced/networked storage).  It's then persisted by the save below / on close.
        env_lang = os.environ.get("RUSE_MM_LANG")
        if env_lang in i18n.LANGS:
            d["default_language"] = env_lang
            self._lang_autodetected = True   # persist the handed-off choice into settings.json
        # Working dir is ALWAYS where the app/exe lives, and the mods folder is ALWAYS <working dir>/mods.
        # These are derived (never user-set), so any saved value is ignored — a moved/copied install always
        # resolves to the right place and there's nothing to misconfigure.
        d["working_dir"] = str(_LAUNCH_DIR)
        d["mods_folder"] = str(_LAUNCH_DIR / "mods")
        return d

    def _save_settings(self):
        try:
            # working_dir and mods_folder are DERIVED from the exe's location on every load (see
            # _load_settings) and are never user-set, so we don't persist them — that keeps
            # machine-specific absolute paths out of settings.json.  game_root is the one real
            # setting; it can live on a different drive (e.g. the Steam library), so it stays absolute.
            to_save = {k: v for k, v in self._settings.items()
                       if k not in ("working_dir", "mods_folder")}
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2)
                f.flush()
                os.fsync(f.fileno())   # durable before a restart relaunches and re-reads this file
        except Exception as e:
            ui_util.error(self, t("mgr.save_error"), str(e))

    def _auto_detect_game_root(self):
        """Set game_root from Steam on first launch if not already configured."""
        try:
            dirs = _steam_mod.find_ruse_game_dirs()
            auto = dirs.get("public") or dirs.get("compat")
            if auto:
                self._settings["game_root"] = str(auto)
                self._save_settings()
        except Exception:
            pass

    def _game_data(self) -> Path:
        return Path(self._settings["game_root"]) / "Data"

    def _cv_game_data(self, mod_folder: str, build: str = "") -> Path:
        """Clean reference game-data dir to diff a mod against. When a target BUILD is given
        (Convert tab picker), use that build's build-id backup (output/backups/v<build>/Data)
        so the diff baseline matches the version the user is making the mod for. Otherwise fall
        back to branch detection. Prefer the clean BACKUP over the live game so a deployed/dirty
        install can't corrupt the diff. (The app NEVER reads the dev source folder — only the
        user's backups and the live Steam game_root.)"""
        base = Path(self._settings.get("working_dir", str(_LAUNCH_DIR))) / "output" / "backups"
        if build:
            bd = base / f"v{build}" / "Data"
            if (bd / "PC").is_dir():
                return bd
        ver = _detect_mod_folder_version(mod_folder)
        for cand in (base / self._version_subname() / "Data", base / ver / "Data"):  # build-id then legacy
            if (cand / "PC").is_dir():
                return cand
        return self._game_data()

    def _clean_root_for_branch(self, branch: str):
        """The clean game ROOT (holds Data/ — and, from a backup, Maps/) for `branch`, or None.
        Mirrors _cv_game_data but keyed by branch and returns the ROOT (apply_mod + the converter both
        take a game-root reference): clean BACKUP → the live game (only if it matches the branch).
        Used by Update .rmods to reconstruct each rmod against clean originals."""
        bd = Path(self._settings.get("working_dir", str(_LAUNCH_DIR))) / "output" / "backups" / branch
        if (bd / "Data" / "PC").is_dir():
            return bd
        gr = self._settings.get("game_root", "").strip()
        if gr and self._game_version() == branch and _is_dir_safe(Path(gr) / "Data"):
            return Path(gr)
        return None

    def _cv_dest_dir(self, mod_folder: str, build: str = "") -> Path:
        """Where a converted mod is written. With a target BUILD (Convert tab picker) → that
        build's folder mods/v<build>/; otherwise the detected-build folder for the current
        install (falling back to the legacy compat/public folder when no build id)."""
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        if build:
            return base / f"v{build}"
        bid = self._game_build_id()
        if bid:
            return base / f"v{bid}"
        return base   # no build id → mods root (strictly build-id; no compat/public fallback)

    def _game_version(self) -> str:
        """FORMAT indicator: 'compat' (OG, Data/PC/99) vs 'public' (remaster, 190852).
        Used for dat-path resolution and the OG-vs-remaster UI split."""
        gr = self._settings.get("game_root", "")
        if not gr:
            return "compat"
        return _detect_game_version(Path(gr) / "Data")

    def _game_build_id(self) -> str:
        """The Steam BUILD ID of the installed game (issue #14 key), or '' if unknown."""
        gr = self._settings.get("game_root", "")
        if not gr:
            return ""
        try:
            return _gv_mod.detect_build_id(gr) or ""
        except Exception:
            return ""

    def _effective_mod_build(self) -> str:
        """Which build's MOD LIBRARY to view/use: the user-selected build if set, else the installed
        game's build.  Drives the mod folder (_mods_dir) and the game_version gate (_rmod_matches_build).
        NOT the deploy target — that stays the installed build (_game_build_id)."""
        # getattr default: _mods_dir runs from _bootstrap_folders early in __init__, before this attr is
        # assigned — without the default that AttributeError got swallowed into a blocking error dialog.
        return getattr(self, "_selected_mod_build", None) or self._game_build_id()

    def _available_mod_builds(self) -> list:
        """Build ids that have a mod library, newest-first (build id descending ~= release order).
        A build qualifies if it has a mods/v<id>/ folder OR bundled mods, plus the installed build."""
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        found = set()
        try:
            for d in base.glob("v*"):
                if d.is_dir() and d.name[1:].isdigit():
                    found.add(d.name[1:])
        except Exception:
            pass
        try:
            found.update(str(b) for b in _gv_mod.known_builds())   # registry builds
        except Exception:
            pass
        if _BUNDLED_MODS_DIR:   # builds whose mods are baked into the exe (bundled_mods/v<id>/)
            try:
                for d in _BUNDLED_MODS_DIR.glob("v*"):
                    if d.is_dir() and d.name[1:].isdigit():
                        found.add(d.name[1:])
            except Exception:
                pass
        inst = self._game_build_id()
        if inst:
            found.add(inst)
        return sorted((b for b in found if str(b).isdigit()), key=lambda x: int(x), reverse=True)

    def _build_has_rmods(self, bid: str) -> bool:
        """True if a build has mods in EITHER its mods/v<id>/ folder OR the exe's baked-in
        bundled_mods/v<id>/ (so a build whose only mods are shipped still counts)."""
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        try:
            for d in (base / f"v{bid}",
                      (_BUNDLED_MODS_DIR / f"v{bid}") if _BUNDLED_MODS_DIR else None):
                if d and d.is_dir() and any(d.glob("*.rmod")):
                    return True
        except Exception:
            pass
        return False

    def _default_mod_build(self):
        """Which build to VIEW by default so the list isn't empty on launch: the installed build if it has
        mods, else the newest OTHER build that does (its mods deploy onto the installed build via the maps).
        None = follow installed.  Computed WITHOUT _mods_dir/_effective (avoids reading _selected_mod_build
        while we're deciding it)."""
        inst = self._game_build_id()
        if not inst or self._build_has_rmods(inst):
            return None
        for b in self._available_mod_builds():
            if b != inst and self._build_has_rmods(b):
                return b
        return None

    def _maybe_suggest_mod_build(self):
        """If the user is viewing the installed build and it has NO mods, but another build does, log a
        hint that they can pick that build in the selector to use its mods here."""
        if not hasattr(self, "_mgr_log") or self._selected_mod_build is not None:
            return
        alt = self._default_mod_build()   # non-None only when installed has no mods but another does
        if alt:
            _log(self._mgr_log,
                 t("mgr.no_mods_game_version_v", inst=self._game_build_id(), alt=alt), "warn")

    def _version_key(self) -> str:
        """Folder key for build-id-scoped storage (backups, mods): the build id if
        detected, else the legacy format name so things still work without a manifest."""
        return self._game_build_id() or self._game_version()

    def _version_subname(self) -> str:
        k = self._version_key()
        return f"v{k}" if k.isdigit() else k

    def _branch_label(self) -> str:
        """Friendly label for the UI: 'compat-2 (v23661872)' when known, else the key."""
        bid = self._game_build_id()
        if bid:
            try:
                return _gv_mod.display_name(bid)
            except Exception:
                pass
        return self._game_version()

    def _backup_root(self) -> Path:
        return Path(self._settings["working_dir"]) / "output" / "backups"

    def _subname_for(self, key: str) -> str:
        """Folder name for a version key: 'v<buildid>' for a numeric build id, else the key as-is."""
        return f"v{key}" if str(key).isdigit() else str(key)

    def _backup_dir_for(self, key: str) -> Path:
        """Backup folder for an ARBITRARY version key (build id or legacy format name)."""
        return self._backup_root() / self._subname_for(key)

    def _backup_dir(self) -> Path:
        # backups are keyed by BUILD ID (output/backups/v<buildid>/). Legacy branch-named
        # backups are left untouched — the app NEVER auto-deletes or moves a user backup.
        return self._backup_dir_for(self._version_key())

    def _backed_up_versions(self) -> list:
        """Versions the user has a clean backup for: [(key, label, path), ...] for each
        output/backups/<sub>/ that actually holds .dat files.  `key` is the build id (digits) or a
        legacy format name; `label` is the friendly display.  Drives the project-creation version
        picker — you can author a mod for any backed-up version, not just the installed one."""
        out = []
        try:
            subs = sorted([d for d in self._backup_root().iterdir() if d.is_dir()],
                          key=lambda d: d.name.lower())
        except Exception:
            subs = []
        for d in subs:
            try:
                if not any(d.rglob("*.dat")):
                    continue
            except Exception:
                continue
            name = d.name
            key = name[1:] if (name.startswith("v") and name[1:].isdigit()) else name
            try:
                label = _gv_mod.display_name(key) if key.isdigit() else key
            except Exception:
                label = name
            out.append((key, label, d))
        # Newest-first: build-id backups by descending id (matches the Mod Manager build selector), then
        # any legacy format-name backups alphabetically.  So a picker's default (first entry, used when the
        # installed build isn't backed up) lands on the newest version, not the alphabetically-first.
        out.sort(key=lambda e: (0, -int(e[0])) if str(e[0]).isdigit() else (1, str(e[0]).lower()))
        return out

    def _build_installable(self, build_id) -> bool:
        """True when `build_id` is a build a Steam branch CURRENTLY serves — i.e. the user could switch
        to that branch and get exactly this build, then back it up.  A superseded build (its branch has
        since moved to a newer build) or one absent from the shipped registry is NOT installable as that
        exact build; its clean files can only come from someone else's backup.  Drives the two variants
        of the 'backup required' guidance when opening a project."""
        try:
            br = _gv_mod.branch_for_build(str(build_id))
            return bool(br) and str(_gv_mod.build_for_branch(br)) == str(build_id)
        except Exception:
            return False

    def _read_project_version(self, folder) -> str:
        """The build id / version a project targets, read from its project.json (or '' if unknown)."""
        try:
            meta = json.loads((Path(folder) / "project.json").read_text(encoding="utf-8"))
            return str(meta.get("version", "")) if isinstance(meta, dict) else ""
        except Exception:
            return ""

    def _has_backup(self) -> bool:
        """True when a clean backup for the current branch exists (at least one .dat).  Editing
        REQUIRES this: the editors read pristine files from the backup, never the live (possibly
        already-modded) game install."""
        bd = self._backup_dir()
        try:
            return bd.exists() and any(bd.rglob("*.dat"))
        except OSError:
            return False

    def _require_backup(self) -> bool:
        """Gate for opening/creating a mod project.  Shows a guiding message and returns False when
        no backup exists yet, so the caller can abort."""
        if self._has_backup():
            return True
        ui_util.warning(
            self,
            t("mgr.backup_required"),
            t("mgr.need_clean_backup_game_files"))
        return False

    def _mod_out_dir(self) -> Path:
        return Path(self._settings["working_dir"]) / "output" / "mod_output_files"

    def _deploy_cache_key(self, active: list) -> str:
        """The deployment's cache key.  For each active rmod, in patch order (first patched first), build
        a stamp ``name:size:mtime``; conjoin them ALL into one string, then hash that once (so the key is
        maximally unique to this exact ordered set + the bytes/times of every mod in it).  Computed at
        deploy time — we don't know the selection or its order until then.

        size+mtime auto-invalidate the cache when any deployed rmod changes.  For a shipped/bundled mod
        we use the size+mtime captured at BUILD (self._bundled_meta) — its basename is stable but its
        _MEIPASS mtime is reset every launch; external mods use their live stat()."""
        parts = [f"engine:{_ENGINE_DEPLOY_VERSION}"]
        for p in active:
            pth = Path(p)
            # Both SAFE-bundled and predeploy rmods live under _MEIPASS with reset mtimes, so they share
            # the same "stable build-time stamps" treatment via their respective manifests.
            meta = self._bundled_meta.get(str(pth)) or self._predeploy_meta.get(str(pth))
            if meta is not None:                       # shipped mod — stable build-time stamps
                size, mtime = meta.get("size"), meta.get("mtime")
            else:
                try:
                    st = pth.stat()
                    size, mtime = st.st_size, st.st_mtime_ns
                except OSError:
                    # Can't read this mod's bytes right now (transiently locked?) — return None so this
                    # deploy neither REUSES nor WRITES a cache.  Folding a "?" placeholder into the key
                    # would let two locked same-name mods collide and defeat the size+mtime invalidation.
                    return None
            parts.append(f"{pth.name}:{size}:{mtime}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _deploy_cache_dir(self, active: list):
        """Folder holding the generated dats for one deployment, keyed by BUILD ID (like the mods
        folder): output/cached_deployments/v<buildid>/<hash>/.  Build-id keying (not the format
        branch) keeps caches distinct between two builds of the same format (e.g. compat-2 vs
        compat-3 vs public, all 'public' format), so a cache hit can't reuse one build's dats on
        another.  Returns None when the cache key can't be computed (a mod's bytes weren't readable) —
        callers then skip caching for this deploy."""
        key = self._deploy_cache_key(active)
        if key is None:
            return None
        return (Path(self._settings["working_dir"]) / "output" / "cached_deployments"
                / self._version_subname() / key)

    @staticmethod
    def _write_deploy_cache(cache_dir: Path, src_root: Path) -> int:
        """Snapshot the generated dats under src_root into cache_dir ATOMICALLY: build a sibling ``.tmp``
        tree, then rename it into place only once every file is copied.  An interrupted write leaves the
        ``.tmp`` (harmless, cleaned next time), NEVER a half-populated ``cache_dir`` — so the longest-prefix
        probe can't reuse a partially-written cache and silently drop mods' edits.  Returns the file count,
        or -1 on failure."""
        tmp = cache_dir.parent / (cache_dir.name + ".tmp")
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True, exist_ok=True)
            n = 0
            for src in src_root.rglob("*"):
                if src.is_file():
                    dest = tmp / src.relative_to(src_root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    n += 1
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            os.replace(tmp, cache_dir)                  # atomic publish — only a complete cache appears
            return n
        except Exception:
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)                  # don't leave a partial temp behind
            except Exception:
                pass
            return -1

    def _mgr_saved_flag(self, key: str, default: bool) -> bool:
        """Read a top-level boolean from the saved manager state (.manager_state.json), or `default`."""
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    return bool(json.load(f).get(key, default))
            except Exception:
                pass
        return default

    def _mod_cache_var(self, path) -> "tk.BooleanVar":
        """The per-rmod 'cache the prefix ending at this mod' BooleanVar (lazily created, default off)."""
        key = str(path)
        v = self._mgr_cache_flags.get(key)
        if v is None:
            v = tk.BooleanVar(value=False)
            self._mgr_cache_flags[key] = v
        return v

    def _saved_entries_for_current_build(self, st: dict) -> list:
        """The saved mod entries for the CURRENT build id from a loaded state dict.  After migration the
        per-build list lives under ``builds[ver]``; a pre-migration state file still has the legacy
        unified ``mods`` list.  (Reading ``mods`` directly is wrong once ``_save_mgr_state`` has run — it
        migrates to ``builds`` and drops ``mods`` — which silently lost bundled enable/cache choices.)"""
        cur = self._mgr_current_ver or self._version_subname()
        builds = st.get("builds")
        if isinstance(builds, dict):
            return builds.get(cur, [])
        return st.get("mods", [])

    def _bundled_saved_cache(self) -> dict:
        """{identity-string: cache-flag} for bundled mods from saved state — keyed by stable identity
        (their _MEIPASS path isn't stable across launches), mirroring _bundled_saved_enabled."""
        out = {}
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    st = json.load(f)
                for e in self._saved_entries_for_current_build(st):
                    bid = e.get("bundled_id")
                    if bid:
                        out[bid] = bool(e.get("cache", False))
            except Exception:
                pass
        return out

    def _mgr_on_per_mod_cache_toggle(self):
        """Show/hide the per-rmod cache markers in the list and persist the choice."""
        self._save_mgr_state()
        if hasattr(self, "_mgr_lb"):
            self._mgr_rebuild()

    def _mgr_saved_deployed_dats(self) -> list:
        """Dat rel-paths (forward-slash) the LAST deploy overlaid onto the game, persisted so the NEXT
        deploy can also restore them to clean — even dats the new mod list no longer touches (otherwise
        a dat modified by a previous deploy stays dirty)."""
        # Read under the lock: a worker's _mgr_set_deployed_dats / _save_mgr_state could otherwise be
        # mid-write and this read would see a torn file and wrongly return [] (skipping leftover cleanup).
        with _MGR_STATE_LOCK:
            if _MGR_STATE_FILE.exists():
                try:
                    with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                        return list(json.load(f).get("deployed_dats", []))
                except Exception:
                    pass
        return []

    def _mgr_set_deployed_dats(self, rels):
        """Persist the set of dat rel-paths currently overlaid onto the game (forward-slash form).
        Read-modify-write so the rest of the manager state is preserved.  Called from worker threads —
        the module lock keeps it from racing _save_mgr_state on the main thread."""
        with _MGR_STATE_LOCK:
            existing = {}
            if _MGR_STATE_FILE.exists():
                try:
                    with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}
            existing["deployed_dats"] = sorted(set(rels))
            try:
                _atomic_write_json(_MGR_STATE_FILE, existing)
            except Exception:
                # This tracker drives leftover-dat cleanup on the NEXT deploy — a silent write failure
                # would leave modded dats dirty in the install while the user is told it deployed.
                logging.exception("Failed to persist deployed_dats to %s", _MGR_STATE_FILE)

    def _conv_out_dir(self) -> Path:
        return Path(self._settings["working_dir"]) / "output" / "converter_output"

    def _mods_dir(self) -> Path:
        """The mods subfolder for the DETECTED BUILD ID: mods/v<buildid>/. STRICTLY build-id —
        no compat/public fallback. The legacy mods/compat and mods/public folders are left
        untouched on purpose: upgrading users grab their old rmods from there and drop them
        into a build-id folder (the manager then stamps that build id onto them, see
        _stamp_legacy_rmods). When no build id is detected, returns the mods ROOT (no rmods
        live there → empty list)."""
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        bid = self._effective_mod_build()
        if not bid:
            return base
        dest = base / f"v{bid}"
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return dest

    def _rmod_ext(self) -> str:
        """Return the correct rmod file extension for the current game mode."""
        return ".compat.rmod" if self._game_version() == "compat" else ".rmod"

    def _rmod_filter(self) -> list:
        """Return the file dialog filter list for the current game mode."""
        if self._game_version() == "compat":
            return [(t("mgr.r_u_s_e_compat"), "*.compat.rmod"), (t("common.all_files"), "*.*")]
        return [(t("mgr.r_u_s_e_mod_2"), "*.rmod"), (t("common.all_files"), "*.*")]

    def _get_mod_files(self) -> list:
        """Return the filtered list of mod paths to show, from the scan caches.

        Compat mode: only .compat.rmod files.
        Public mode: only .rmod files, plus .compat.rmod files when toggle is on.
        """
        if self._game_version() == "compat":
            return list(self._scanned_compat)
        # Public mode — show public mods, add compat ones if toggle is on
        files = list(self._scanned_public)
        if self._show_compat_var.get():
            existing = {p for p in files}
            extras = [p for p in self._scanned_compat if p not in existing]
            files = sorted(files + extras, key=lambda p: p.name.lower())
        return files

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=6, pady=6)

        tabs = [
            ("  Mod Manager  ", self._build_manager_tab),
            ("  Convert  ",     self._build_convert_tab),
            ("  Mod Editor  ",  self._build_mod_editor_tab),
            ("  Settings  ",    self._build_settings_tab),
        ]
        self._ed_tab = None
        for label, builder in tabs:
            frame = ttk.Frame(self._nb)
            self._nb.add(frame, text=t(label))
            builder(frame)
            if "Mod Editor" in label:
                self._ed_tab = frame

        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._update_mod_editor_tab()   # editor is public-only — hide its tab in compat mode

    def _is_compat_game(self) -> bool:
        """True only when a game root is configured AND it's the compat (non-190852) build."""
        gr = self._settings.get("game_root", "").strip()
        return bool(gr) and self._game_version() == "compat"

    def _update_mod_editor_tab(self):
        """Show/hide the Mod Editor tab.  The editor edits the public PC/190852 files only, so it has
        no use in compat mode — hide its tab whenever the manager detects a compat install (startup,
        Detect Game Version, or a Settings game-root change all route through here)."""
        tab = getattr(self, "_ed_tab", None)
        if tab is None or getattr(self, "_nb", None) is None:
            return
        try:
            self._nb.tab(tab, state=("hidden" if self._is_compat_game() else "normal"))
        except Exception:
            pass

    def _on_tab_changed(self, _=None):
        idx = self._nb.index("current")
        if idx == 1:
            self._reset_convert_tab()          # also re-lists the backed-up versions (Make mod for version)
        elif idx == 2:
            self._ed_refresh_target_versions()  # re-list the backed-up versions in the Game Version picker

    # =========================================================================
    # MOD MANAGER TAB
    # =========================================================================

    def _build_manager_tab(self, p):
        pad = {"padx": 6, "pady": 3}

        # ── Setup checklist ───────────────────────────────────────────────────
        sf = ttk.LabelFrame(p, text=t("mgr.setup_checklist_complete_steps_1"))
        sf.pack(fill="x", **pad)
        sf.columnconfigure(1, weight=1)

        ttk.Label(sf, text=t("mgr.step_1"), font=_F_BOLD, foreground=_R_GOLD
                  ).grid(row=0, column=0, padx=(8, 4), pady=(6, 2), sticky="w")
        self._mgr_s1_lbl = tk.Label(sf, text=t("mgr.checking"),
                                    font=_F_LOG, background=_R_BG_PANEL,
                                    foreground=_R_GOLD, anchor="w")
        self._mgr_s1_lbl.grid(row=0, column=1, sticky="ew", padx=4, pady=(6, 2))
        ttk.Button(sf, text=t("mgr.open_settings"),
                   command=lambda: self._nb.select(3)
                   ).grid(row=0, column=2, padx=6, pady=(6, 2))

        ttk.Label(sf, text=t("mgr.step_2"), font=_F_BOLD, foreground=_R_GOLD
                  ).grid(row=1, column=0, padx=(8, 4), pady=(2, 6), sticky="w")
        self._mgr_s2_lbl = tk.Label(sf, text=t("mgr.checking"),
                                    font=_F_LOG, background=_R_BG_PANEL,
                                    foreground=_R_GOLD, anchor="w")
        self._mgr_s2_lbl.grid(row=1, column=1, sticky="ew", padx=4, pady=(2, 6))
        bbf = ttk.Frame(sf)
        bbf.grid(row=1, column=2, padx=6, pady=(2, 6))
        self._mgr_backup_btn = ttk.Button(bbf, text=t("mgr.create_backup"),
                                          command=self._mgr_create_backup,
                                          state="disabled")
        self._mgr_backup_btn.pack(side="left", padx=2)
        self._mgr_restore_btn = ttk.Button(bbf, text=t("mgr.restore_clean"),
                                           command=self._mgr_restore_clean,
                                           state="disabled")
        self._mgr_restore_btn.pack(side="left", padx=2)

        # ── Main body: vertical paned (top = mods+desc, bottom = log) ─────────
        # NOTE: this is packed LAST (see bottom of this method) so it is the widget
        # that shrinks when the window is too short — the action bar and footer below
        # are packed side=BOTTOM first and reserve their space, staying always visible.
        vpw = ttk.PanedWindow(p, orient=tk.VERTICAL)

        # Top pane — horizontal: mod list (left) | description (right)
        hpw = ttk.PanedWindow(vpw, orient=tk.HORIZONTAL)
        vpw.add(hpw, weight=3)

        # ── Mod list ──────────────────────────────────────────────────────────
        mf = ttk.LabelFrame(hpw, text=t("mgr.mods_active"))
        hpw.add(mf, weight=2)

        tb = ttk.Frame(mf)
        tb.pack(fill="x", padx=4, pady=(4, 0))
        # Wrapping toolbar: in a narrow window the buttons flow onto a second row instead of the
        # right-hand ones (Share/Import Order) getting clipped off the edge (issue #5.3).
        # Import Order / Share Order are pinned RIGHT (they act on the whole order, not a selection);
        # the rest stay left-aligned.
        tb_btns = [
            ttk.Button(tb, text=t("mgr.scan_mods_folder"), command=self._mgr_scan),
            ttk.Button(tb, text=t("mgr.add_rmod"), command=self._mgr_add),
            ttk.Button(tb, text=t("mgr.remove_selected"), command=self._mgr_remove),
            ttk.Button(tb, text=t("mgr.clear_all"), command=self._mgr_clear),
            ttk.Button(tb, text=t("mgr.browse_rmods"), command=self._mgr_browse_mods),
        ]
        tb_btns_right = [
            ttk.Button(tb, text=t("mgr.import_order"), command=self._mgr_import_order),
            ttk.Button(tb, text=t("mgr.share_order"), command=self._mgr_share_order),
        ]
        ui_util.flow(tb, tb_btns, right=tb_btns_right)

        ttk.Label(mf,
                  text=t("mgr.top_loads_first_bottom_overrides"),
                  font=_F_LOG, foreground=_R_GOLD,
                  ).pack(anchor="w", padx=6, pady=(4, 2))

        lf = ttk.Frame(mf)
        lf.pack(fill="both", expand=True, padx=4, pady=(0, 2))

        ob = ttk.Frame(lf)
        ob.pack(side="right", fill="y", padx=(2, 2), pady=2)
        ttk.Label(ob, text=t("mgr.earlier"), font=_F_LOG,
                  foreground=_R_TEXT_DIM).pack(pady=(2, 0))
        ttk.Button(ob, text="⇈", width=3, command=self._mgr_top).pack(fill="x")
        ttk.Button(ob, text="▲", width=3, command=self._mgr_up).pack(
            fill="x", pady=(2, 0))
        ttk.Button(ob, text="▼", width=3, command=self._mgr_down).pack(
            fill="x", pady=(2, 0))
        ttk.Button(ob, text="⇊", width=3, command=self._mgr_bottom).pack(
            fill="x", pady=(2, 0))
        ttk.Label(ob, text=t("mgr.later"), font=_F_LOG,
                  foreground=_R_TEXT_DIM).pack()
        # Build selector: which build's mod library to view/use.  Replaces the old COMPAT toggle — instead
        # of mixing formats, you pick a build; deploying onto a different installed build uses the version
        # maps (remap + stale flags).  Newest-first (release order).
        ttk.Label(ob, text=t("mgr.mods"), font=_F_LOG,
                  foreground=_R_TEXT_DIM).pack(pady=(8, 0))
        self._mod_build_var = tk.StringVar()
        self._mod_build_cb = ttk.Combobox(ob, textvariable=self._mod_build_var,
                                          state="readonly", width=11, font=_F_LOG)
        self._mod_build_cb.bind("<<ComboboxSelected>>", self._on_mod_build_select)

        self._mgr_lb = tk.Listbox(lf, selectmode="extended",
                                  activestyle="none",
                                  background=_R_BG_WIDGET, foreground=_R_TEXT,
                                  selectbackground=_R_SEL_BG, selectforeground=_R_SEL_FG,
                                  font=("Courier New", 10), relief="flat",
                                  highlightthickness=1, highlightcolor=_R_BORDER,
                                  highlightbackground=_R_BORDER)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self._mgr_lb.yview)
        self._mgr_lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._mgr_lb.pack(side="left", fill="both", expand=True)
        self._mgr_lb.bind("<ButtonRelease-1>", self._mgr_lb_click)
        self._mgr_lb.bind("<space>", self._mgr_toggle)
        self._mgr_lb.bind("<<ListboxSelect>>", self._mgr_on_select)

        # Enable / disable bar — wraps in a narrow window so 'Update .rmod' isn't clipped (issue #5.3).
        # 'Update .rmod' re-derives the selected EXTERNAL rmod into the current format; it's disabled
        # unless exactly one external (non-bundled) mod is selected (see _mgr_update_btn_state).
        eb = ttk.Frame(mf)
        eb.pack(fill="x", padx=4, pady=(2, 6))
        self._mgr_update_btn = ttk.Button(eb, text=t("mgr.update_rmod"),
                                          command=self._mgr_update_rmod, state="disabled")
        # 'Share Mod' opens GitHub's upload page for the selected external rmod, pointed at the exact
        # source/example_mods/v<buildid>/ folder build.py pulls community mods from — same single-
        # external-selection gate as 'Update .rmod' (see _mgr_update_btn_state).
        self._mgr_share_btn = ttk.Button(eb, text=t("mgr.share_mod_2"),
                                         command=self._mgr_share_mod, state="disabled")
        eb_btns = [
            ttk.Button(eb, text=t("mgr.enable_selected"), command=self._mgr_enable_selected),
            ttk.Button(eb, text=t("mgr.disable_selected"), command=self._mgr_disable_selected),
            ttk.Button(eb, text=t("mgr.all_off"), command=self._mgr_disable_all),
            self._mgr_update_btn,
            self._mgr_share_btn,
        ]
        ui_util.flow(eb, eb_btns)

        # ── Selected mod detail ───────────────────────────────────────────────
        df = ttk.LabelFrame(hpw, text=t("mgr.selected_mod"))
        hpw.add(df, weight=1)
        self._mgr_detail_meta = tk.StringVar(value=t("mgr.select_mod_see_details"))
        ttk.Label(df, textvariable=self._mgr_detail_meta,
                  justify="left", font=_F_LOG).pack(padx=8, pady=(6, 2), anchor="w")
        self._mgr_detail_desc = _ThemedScrolledText(
            df, state="disabled", font=_F_MAIN,
            wrap="word", relief="flat",
            background=_R_BG_WIDGET, foreground=_R_TEXT,
            insertbackground=_R_GOLD)
        self._mgr_detail_desc.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        # ── Log ───────────────────────────────────────────────────────────────
        lf2 = ttk.LabelFrame(vpw, text=t("mgr.log"))
        vpw.add(lf2, weight=1)
        self._mgr_log = _make_log(lf2)
        self._mgr_log.pack(fill="both", expand=True, padx=4, pady=4)
        # NOTE: this log shows ONLY Mod Manager activity.  Python-logging output (incl. Pillow's DEBUG)
        # and the cross-tab "all logs" view live in the Mod Editor's mirror windows — see
        # _build_mod_editor_tab, which attaches the logging handler to those.

        # ── Actions bar ───────────────────────────────────────────────────────
        af = ttk.Frame(p)
        self._mgr_dry = tk.BooleanVar(value=False)
        cb_dry = ttk.Checkbutton(af, text=t("mgr.dry_run_no_files_written"),
                                 variable=self._mgr_dry)
        cb_dry.pack(side="left", padx=4)
        # Master switch (default ON): when off, never cache or reuse — always apply patches fresh.
        self._mgr_cache_enabled = tk.BooleanVar(value=self._mgr_saved_flag("cache_enabled", True))
        cb_cache = ttk.Checkbutton(af, text=t("mgr.cache_enabled"), variable=self._mgr_cache_enabled,
                                   command=self._save_mgr_state)
        cb_cache.pack(side="left", padx=4)
        # When ON (default), each rmod row shows a far-right "cache" toggle; only the prefixes ending at a
        # ticked mod are cached (controls disk).  When OFF, only the full deployment result is cached.
        self._mgr_per_mod_cache = tk.BooleanVar(value=self._mgr_saved_flag("per_mod_cache", True))
        cb_pmc = ttk.Checkbutton(af, text=t("mgr.per_mod_cache_points"), variable=self._mgr_per_mod_cache,
                                 command=self._mgr_on_per_mod_cache_toggle)
        cb_pmc.pack(side="left", padx=4)
        # When OFF (default), reuse cached generated dats instead of re-applying patches; when ON, always
        # rebuild the dats (bypasses cache reuse) and re-cache them.
        self._mgr_regen = tk.BooleanVar(value=False)
        cb_regen = ttk.Checkbutton(af, text=t("mgr.always_regenerate_dat_files"),
                                   variable=self._mgr_regen)
        cb_regen.pack(side="left", padx=4)
        ttk.Button(af, text=t("mgr.clear_log"),
                   command=lambda: _log_clear(self._mgr_log)).pack(
            side="right", padx=4)
        # Right cluster packs right→left, so packing Launch then Deploy renders them Deploy | Launch.
        self._mgr_launch_btn = ttk.Button(af, text=t("mgr.launch_r_u_s_e"),
                                          command=self._mgr_launch_game)
        self._mgr_launch_btn.pack(side="right", padx=4)
        self._mgr_deploy_btn = ttk.Button(af, text=t("mgr.deploy_mods"),
                                          command=self._mgr_deploy)
        self._mgr_deploy_btn.pack(side="right", padx=4)

        # Responsive: when the bar is too narrow to fit everything at full size, swap the
        # checkbox + Launch labels to short forms so the Deploy button is never pushed off.
        # (full label, short label) per widget; the relayout handler picks based on width.
        self._mgr_af = af
        self._mgr_af_compact = False
        self._mgr_af_full_req = 0
        self._mgr_af_labels = [
            (cb_dry,                t("mgr.dry_run_no_files_written"), t("mgr.dry_run")),
            (cb_cache,              t("mgr.cache_enabled"),              t("mgr.cache")),
            (cb_pmc,                t("mgr.per_mod_cache_points"),       t("mgr.per_mod")),
            (cb_regen,              t("mgr.always_regenerate_dat_files"), t("mgr.regen")),
            (self._mgr_launch_btn,  t("mgr.launch_r_u_s_e"),         t("mgr.launch")),
        ]
        af.bind("<Configure>", self._mgr_af_relayout)

        self._mgr_foot = tk.StringVar(value=t("mgr.ready"))
        foot_lbl = ttk.Label(p, textvariable=self._mgr_foot, anchor="w")

        # ── Pack order (the part that keeps the bottom buttons visible) ─────────
        # Tk's pack clips the LAST-packed widgets first when the window is too short.
        # So pack the footer and action bar to the BOTTOM *before* the paned window,
        # then pack the paned window last with expand=True.  When the window is
        # shortened the paned window (mods list + log) shrinks while the Deploy /
        # Launch buttons and footer stay pinned and fully visible.
        foot_lbl.pack(side=tk.BOTTOM, fill="x", padx=6, pady=(0, 4))
        af.pack(side=tk.BOTTOM, fill="x", padx=6, pady=(2, 3))
        vpw.pack(side=tk.TOP, fill="both", expand=True, padx=6, pady=3)

        self.after(150, self._mgr_refresh_status)

    def _mgr_af_relayout(self, event):
        """Toggle the action bar between full and short labels based on its width, so the
        Deploy button is never squeezed off when the window is made skinny.  The full-size
        requirement is measured once (while still in full mode); we compare the live width
        against it with a little hysteresis to avoid flapping at the boundary."""
        af = getattr(self, "_mgr_af", None)
        if af is None:
            return
        # Record the full-layout width requirement once, while labels are still full.
        if not self._mgr_af_compact and self._mgr_af_full_req == 0:
            req = af.winfo_reqwidth()
            if req > 1:
                self._mgr_af_full_req = req
        full_req = self._mgr_af_full_req
        if full_req == 0:
            return
        want_compact = self._mgr_af_compact
        if event.width < full_req:
            want_compact = True
        elif event.width > full_req + 24:   # hysteresis band
            want_compact = False
        if want_compact == self._mgr_af_compact:
            return
        self._mgr_af_compact = want_compact
        for widget, full, short in self._mgr_af_labels:
            try:
                widget.configure(text=(short if want_compact else full))
            except Exception:
                pass

    def _mgr_refresh_status(self):
        game_root = self._settings.get("game_root", "").strip()
        # A configured path can become unreachable between launches — e.g. the drive it
        # lives on was removed.  Treat that as "not ready": surface it so the user re-sets
        # the path, and gate the buttons that would otherwise fail against a dead device.
        game_ready = bool(game_root) and _is_dir_safe(Path(game_root) / "Data")
        if game_ready:
            ver = self._game_version()
            ver_label = "R.U.S.E." if ver == "public" else "R.U.S.E. COMPAT"
            s1_text     = t("mgr.done_ver_label_game_root", ver_label=ver_label, game_root=game_root)
            s1_text_set = s1_text
            s1_color    = _COL_OK
        elif game_root:
            s1_text = s1_text_set = t(
                "mgr.game_root_unreachable_game_root",
                game_root=game_root)
            s1_color = _COL_ERR
        else:
            s1_text     = t("mgr.incomplete_click_open_settings_set")
            s1_text_set = t("mgr.incomplete_browse_r_u_s")
            s1_color    = _COL_ERR
        self._mgr_s1_lbl.configure(text=s1_text, foreground=s1_color)
        if hasattr(self, "_set_s1_lbl"):
            self._set_s1_lbl.configure(text=s1_text_set, foreground=s1_color)
        if not self._mgr_running:
            backup_state = "normal" if game_ready else "disabled"
            if hasattr(self, "_mgr_launch_btn"):
                self._mgr_launch_btn.configure(state=backup_state)
            if hasattr(self, "_mgr_backup_btn"):
                self._mgr_backup_btn.configure(state=backup_state)
            if hasattr(self, "_set_backup_btn"):
                self._set_backup_btn.configure(state=backup_state)
            if hasattr(self, "_prof_backup_btn"):
                self._prof_backup_btn.configure(state=backup_state)

        bd = self._backup_dir()
        # Show the friendly BRANCH name + build id (issue #14) so the user can confirm
        # they're backing up / deploying for the branch they intended.
        if self._game_build_id():
            ver_label = f"{self._branch_label()} "
        else:
            ver_label = "R.U.S.E. COMPAT " if self._game_version() == "compat" else ""
        n = sum(1 for _ in bd.rglob("*.dat")) if bd.exists() else 0
        if not game_root:
            s2_text  = t("mgr.incomplete_set_game_root_directory")
            s2_color = _COL_ERR
        elif n > 0:
            s2_text  = t("mgr.done_ver_label_backup_ready",
                         ver_label=ver_label, n=n)
            s2_color = _COL_OK
        else:
            s2_text  = t("mgr.incomplete_no_ver_label_backup", ver_label=ver_label)
            s2_color = _COL_ERR
        self._mgr_s2_lbl.configure(text=s2_text, foreground=s2_color)
        if hasattr(self, "_set_s2_lbl"):
            self._set_s2_lbl.configure(text=s2_text, foreground=s2_color)
        restore_state = "normal" if (n > 0 and not self._mgr_running) else "disabled"
        if hasattr(self, "_mgr_restore_btn"):
            self._mgr_restore_btn.configure(state=restore_state)
        if hasattr(self, "_set_restore_btn"):
            self._set_restore_btn.configure(state=restore_state)

        if hasattr(self, "_mod_build_cb"):
            self._mod_build_cb.pack(fill="x", pady=(0, 2))
            self._refresh_mod_build_cb()
        self._cv_refresh_labels()

    def _cv_refresh_labels(self):
        """Update Convert/Create tab button labels and hints to reflect current game mode."""
        ext = self._rmod_ext()
        if hasattr(self, "_cv_btn"):
            self._cv_btn.configure(text=t("mgr.convert_ext", ext=ext))
        if hasattr(self, "_cr_load_btn"):
            self._cr_load_btn.configure(text=t("mgr.load_ext_edit", ext=ext))
        if hasattr(self, "_cr_save_btn"):
            self._cr_save_btn.configure(text=t("mgr.save_as_ext", ext=ext))

    def _mgr_label(self, var, path) -> str:
        chk = "☑" if var.get() else "☐"
        # [SAFE] marks a bundled (baked-into-the-exe) mod: its bytes are trusted/canonical and it
        # overrides any external mod of the same name+major.  It's a normal toggleable mod otherwise.
        prefix = ("[SAFE]  " if self._is_bundled(path) else "") + \
                 ("[COMPAT]  " if str(path).lower().endswith(".compat.rmod") else "")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            name = d.get("name", Path(path).name)
            ver  = d.get("version", "")
            base = f"{chk}  {prefix}{name}" + (f"  v{ver}" if ver else "")
        except Exception:
            base = f"{chk}  {prefix}{Path(path).name}"
        # "Per-mod cache points" mode → append a far-right cache toggle (fixed 11-char marker, padded so
        # it sits to the right).  Clicking it (handled in _mgr_lb_click via text measurement) marks the
        # prefix ending at this mod to be cached.
        if getattr(self, "_mgr_per_mod_cache", None) is not None and self._mgr_per_mod_cache.get():
            mark = _CACHE_MARK_ON if self._mod_cache_var(path).get() else _CACHE_MARK_OFF
            base = base.ljust(_CACHE_MARK_COL) + mark
        return base

    def _refresh_mod_build_cb(self):
        """Populate the build selector with available mod-library builds (newest-first) and show the
        current effective build.  Labels are the raw build id 'v<id>' (friendly branch names aren't
        reliable across builds), with '(installed)' marking the installed one."""
        if not hasattr(self, "_mod_build_cb"):
            return
        builds = self._available_mod_builds()
        inst = self._game_build_id()
        self._mod_build_choices = {}   # label -> build id
        labels = []
        for b in builds:
            label = f"v{b}" + (t("mgr.installed") if b == inst else "")
            self._mod_build_choices[label] = b
            labels.append(label)
        self._mod_build_cb.configure(values=labels)
        cur = self._effective_mod_build()
        for label, b in self._mod_build_choices.items():
            if b == cur:
                self._mod_build_var.set(label)
                break

    def _on_mod_build_select(self, _evt=None):
        """User picked a build in the selector: repoint the whole mod list to that build's library and
        RELOAD it (re-scan mods/v<id>/ + rebuild the list for that build), not just redraw.  Selecting
        the installed build clears the override.  Deploy still targets the installed build."""
        label = self._mod_build_var.get()
        picked = getattr(self, "_mod_build_choices", {}).get(label)
        if not picked:
            return
        self._selected_mod_build = None if picked == self._game_build_id() else picked
        self._scan_bundled()                               # re-scan bundled_mods/v<selected>/ (baked-in mods)
        self._mgr_scan_both()                              # re-scan the now-repointed mods/v<selected>/
        self._mgr_load_mode(self._effective_mod_build())   # rebuild the list for the selected build

    def _mgr_add_path(self, path: str):
        key = self._norm_path(path)
        if any(self._norm_path(ex) == key for _, ex in self._mgr_mod_vars):
            return
        # A SAFE (shipped) mod with the same name+major takes precedence — don't add a shadowed copy.
        if self._bundled_keys and self._rmod_identity(path) in self._bundled_keys:
            ui_util.info(
                self,
                t("mgr.shipped_safe_mod_takes_precedence"),
                t("mgr.safe_mod_same_name_major"))
            return
        var = tk.BooleanVar(value=False)
        self._mgr_mod_vars.append((var, path))
        is_compat_file = path.lower().endswith(".compat.rmod")
        ver = self._game_version()
        shown = ((ver == "compat" and is_compat_file) or
                 (ver == "public" and (self._show_compat_var.get() or not is_compat_file)))
        if shown:
            self._mgr_lb.insert(tk.END, self._mgr_label(var, path))
        self._save_mgr_state()   # newly-added mod must survive a reload

    def _visible_mv_indices(self) -> list:
        """Map listbox positions to mod_vars indices based on current mode and compat toggle."""
        ver = self._game_version()
        if ver == "compat":
            # Compat mode: show .compat.rmod only
            return [i for i, (_, p) in enumerate(self._mgr_mod_vars)
                    if str(p).lower().endswith(".compat.rmod")]
        # Public mode: show .rmod always; also show .compat.rmod if toggle is on
        if self._show_compat_var.get():
            return list(range(len(self._mgr_mod_vars)))
        return [i for i, (_, p) in enumerate(self._mgr_mod_vars)
                if not str(p).lower().endswith(".compat.rmod")]

    def _mgr_redraw_list(self):
        """Rebuild listbox text in-place, preserving scroll position and selection."""
        sel   = list(self._mgr_lb.curselection())
        yview = self._mgr_lb.yview()
        self._mgr_lb.delete(0, tk.END)
        for mv_idx in self._visible_mv_indices():
            var, path = self._mgr_mod_vars[mv_idx]
            self._mgr_lb.insert(tk.END, self._mgr_label(var, path))
        for s in sel:
            if 0 <= s < self._mgr_lb.size():
                self._mgr_lb.selection_set(s)
        self._mgr_lb.yview_moveto(yview[0])
        self._mgr_update_btn_state()
        if not self._mgr_state_loading:   # keep disk in sync with enable/disable changes
            self._save_mgr_state()

    def _mgr_refresh_item(self, lb_idx: int):
        visible = self._visible_mv_indices()
        if not (0 <= lb_idx < len(visible)):
            return
        mv_idx = visible[lb_idx]
        yview  = self._mgr_lb.yview()
        var, path = self._mgr_mod_vars[mv_idx]
        self._mgr_lb.delete(lb_idx)
        self._mgr_lb.insert(lb_idx, self._mgr_label(var, path))
        self._mgr_lb.selection_set(lb_idx)
        self._mgr_lb.yview_moveto(yview[0])
        self._mgr_update_btn_state()   # selection just changed → keep Update/Share enablement honest

    def _mgr_lb_click(self, event):
        lb_idx  = self._mgr_lb.nearest(event.y)
        visible = self._visible_mv_indices()
        if not (0 <= lb_idx < len(visible)):
            return
        mv_idx = visible[lb_idx]
        bb = self._mgr_lb.bbox(lb_idx)
        if not bb:
            return
        # `nearest()` clamps to the last row, so a click in the empty area BELOW the list would otherwise
        # toggle the last mod's enabled/cache state. Only act if the click is inside the row's y-span.
        if not (bb[1] <= event.y <= bb[1] + bb[3]):
            return
        f = font.Font(font=self._mgr_lb.cget("font"))
        # Far-right "cache" marker (only present in Per-mod cache mode): the trailing fixed-length
        # marker — toggle it if the click lands past where it starts (measured from the actual text).
        if self._mgr_per_mod_cache.get():
            row = self._mgr_lb.get(lb_idx)
            if len(row) >= len(_CACHE_MARK_ON) and event.x >= bb[0] + f.measure(row[:-len(_CACHE_MARK_ON)]):
                cv = self._mod_cache_var(self._mgr_mod_vars[mv_idx][1])
                cv.set(not cv.get())
                self._mgr_refresh_item(lb_idx)
                self._save_mgr_state()
                return
        # Left "☑/☐" region toggles enabled.
        if event.x <= bb[0] + f.measure("☑  "):
            var, _ = self._mgr_mod_vars[mv_idx]
            var.set(not var.get())
            self._mgr_refresh_item(lb_idx)
            self._save_mgr_state()

    def _mgr_toggle(self, _=None):
        visible = self._visible_mv_indices()
        for lb_idx in self._mgr_lb.curselection():
            if lb_idx < len(visible):
                var, _ = self._mgr_mod_vars[visible[lb_idx]]
                var.set(not var.get())
        self._mgr_redraw_list()

    def _mgr_enable_selected(self):
        visible = self._visible_mv_indices()
        for lb_idx in self._mgr_lb.curselection():
            if lb_idx < len(visible):
                self._mgr_mod_vars[visible[lb_idx]][0].set(True)
        self._mgr_redraw_list()

    def _mgr_disable_selected(self):
        visible = self._visible_mv_indices()
        for lb_idx in self._mgr_lb.curselection():
            if lb_idx < len(visible):
                self._mgr_mod_vars[visible[lb_idx]][0].set(False)
        self._mgr_redraw_list()

    def _mgr_enable_all(self):
        # Only the VISIBLE rows — never flip a mod hidden by the compat toggle or the current mode.
        # Enabling a hidden compat mod would let it deploy against a public game (Deploy reads raw
        # v.get()); _mgr_redraw_list (unlike _mgr_rebuild) does not re-hide it.
        for mv_idx in self._visible_mv_indices():
            self._mgr_mod_vars[mv_idx][0].set(True)
        self._mgr_redraw_list()

    def _mgr_disable_all(self):
        # Only the VISIBLE rows — a hidden mod's enabled flag is preserved on disk by _save_mgr_state's
        # prior_enabled path, so "All Off" must not silently clear (or leave) hidden mods.
        for mv_idx in self._visible_mv_indices():
            self._mgr_mod_vars[mv_idx][0].set(False)
        self._mgr_redraw_list()

    def _mgr_update_btn_state(self):
        """Enable 'Update .rmod' and 'Share Mod' only when exactly one EXTERNAL (non-bundled) mod is
        selected and no other long-running task is in progress.  Called on selection change and after
        list redraws."""
        btns = [b for b in (getattr(self, "_mgr_update_btn", None),
                            getattr(self, "_mgr_share_btn", None)) if b is not None]
        if not btns:
            return
        def _set(state):
            for b in btns:
                b.configure(state=state)
        if getattr(self, "_conv_running", False) or getattr(self, "_mgr_running", False):
            _set("disabled"); return
        sel = self._mgr_lb.curselection()
        if len(sel) != 1:
            _set("disabled"); return
        visible = self._visible_mv_indices()
        if sel[0] >= len(visible):
            _set("disabled"); return
        _, path = self._mgr_mod_vars[visible[sel[0]]]
        _set("disabled" if self._is_bundled(path) else "normal")

    def _mgr_update_rmod(self):
        """Re-derive the SELECTED external rmod into the current format (paths, schema, surgical patches)
        without needing the original mod folder — see converter.update_rmod.  Bundled mods are blocked
        upfront because they're baked into the exe and can't be updated externally."""
        if getattr(self, "_conv_running", False) or getattr(self, "_mgr_running", False):
            return
        sel = self._mgr_lb.curselection()
        if len(sel) != 1:
            return
        visible = self._visible_mv_indices()
        if sel[0] >= len(visible):
            return
        _, path = self._mgr_mod_vars[visible[sel[0]]]
        if self._is_bundled(path):
            ui_util.warning(
                self,
                t("mgr.update_rmod"),
                t("mgr.shipped_bundled_mod_s_baked"))
            return
        name = Path(path).name
        branch = "compat" if name.lower().endswith(".compat.rmod") else "public"
        root = self._clean_root_for_branch(branch)
        if root is None:
            ui_util.error(
                self,
                t("mgr.update_rmod"),
                t("mgr.no_clean_branch_originals_available",
                  branch=branch))
            return
        self._conv_running = True
        self._mgr_update_btn.configure(state="disabled")
        _log(self._mgr_log, t("mgr.updating_name_current_format", name=name), "head")

        def _work():
            def wf(m): self._ui(lambda m=m: _log(self._mgr_log, f"  {m}", "warn"))
            err = None
            try:
                status = update_rmod(path, str(root), branch,
                                     log_fn=(lambda *_a: None), warn_fn=wf)
            except Exception as e:
                status, err = "failed", str(e)

            def _done():
                self._conv_running = False
                self._mgr_update_btn_state()
                if err:
                    _log(self._mgr_log, t("mgr.error_e_3", e=err), "err")
                tag = {"updated": "ok", "unchanged": "info",
                       "skipped": "warn", "failed": "err"}.get(status, "info")
                label = {"updated": t("mgr.updated"), "unchanged": t("mgr.unchanged"),
                         "skipped": t("mgr.skipped"), "failed": t("mgr.failed_2")}.get(status, status)
                _log(self._mgr_log, t("mgr.name_label", name=name, label=label), tag)
            self._ui(_done)
        threading.Thread(target=_work, daemon=True).start()

    def _mgr_share_mod(self):
        """Share the SELECTED external rmod with the community by opening GitHub's 'upload files'
        page pointed at the exact source/example_mods/v<buildid>/ folder build.py pulls community
        mods from.  A browser can't be handed the file for the user (browser security), so we also
        reveal the .rmod in Explorer, highlighted, ready to drag onto the page.  GitHub does the
        rest: a signed-in user without push access is auto-forked and gets a 'Propose changes' pull
        request, which the PR template then fills in.  No git, no tokens, no folder-picking."""
        if getattr(self, "_conv_running", False) or getattr(self, "_mgr_running", False):
            return
        sel = self._mgr_lb.curselection()
        if len(sel) != 1:
            return
        visible = self._visible_mv_indices()
        if sel[0] >= len(visible):
            return
        _, path = self._mgr_mod_vars[visible[sel[0]]]
        name = Path(path).name
        if self._is_bundled(path):
            ui_util.info(
                self,
                t("mgr.share_mod"),
                t("mgr.name_already_part_built_pack", name=name))
            return

        # The repo folder mirrors the local mods/v<buildid>/ layout, so the file's OWN parent folder
        # is the target build folder.  Fall back to the currently loaded version if it isn't a
        # v<buildid> folder (e.g. a mod imported from some other location).
        sub = Path(path).parent.name
        if not re.fullmatch(r"v\d+", sub):
            sub = self._mgr_current_ver or self._version_subname()
        if not re.fullmatch(r"v\d+", sub or ""):
            ui_util.warning(
                self,
                t("mgr.share_mod"),
                t("mgr.couldn_t_tell_which_game"))
            return
        try:
            label = _gv_mod.display_name(sub[1:])
        except Exception:
            label = sub

        url = (f"https://github.com/LittleGroove/RUSE-Mod-Manager/upload/main/"
               f"source/example_mods/{sub}")
        if not ui_util.confirm(
                self,
                t("mgr.share_mod"),
                t("mgr.share_name_community_opens_github", name=name, label=label)):
            return

        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            ui_util.error(
                self,
                t("mgr.share_mod"),
                t("mgr.couldn_t_open_web_browser", url=url))
            return
        # Reveal the .rmod in Explorer, highlighted, so it's ready to drag onto the upload page.
        try:
            if sys.platform == "win32":
                subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"')
        except Exception:
            pass
        _log(self._mgr_log,
             t("mgr.sharing_name_opened_github_s", name=name, label=label), "info")

    def _mgr_on_select(self, _=None):
        self._mgr_update_btn_state()
        sel = self._mgr_lb.curselection()
        if not sel:
            self._mgr_detail_meta.set(t("mgr.select_mod_see_details"))
            self._mgr_set_desc("")
            return
        visible = self._visible_mv_indices()
        lb_idx  = sel[0]
        if lb_idx >= len(visible):
            return
        _, path = self._mgr_mod_vars[visible[lb_idx]]
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            line1 = "   |   ".join(
                f"{label}: {d[k]}"
                for k, label in [("name", t("mgr.name_2")), ("author", t("common.author"))]
                if k in d
            )
            n = sum(len(pg.get("changes", [])) for pg in d.get("patches", []))
            ver   = t("mgr.version_version", version=d['version']) if "version" in d else ""
            line2 = "   |   ".join(filter(None, [ver, t("mgr.ndf_changes_n", n=n)]))
            self._mgr_detail_meta.set(f"{line1}\n{line2}" if line1 else line2)
            self._mgr_set_desc(d.get("description", ""))
        except Exception:
            self._mgr_detail_meta.set(t("mgr.file_path", path=path))
            self._mgr_set_desc("")

    def _mgr_set_desc(self, text: str):
        self._mgr_detail_desc.configure(state="normal")
        self._mgr_detail_desc.delete("1.0", tk.END)
        if text:
            self._mgr_detail_desc.insert("1.0", text)
        self._mgr_detail_desc.configure(state="disabled")

    def _mgr_rebuild(self, sel=-1):
        visible = self._visible_mv_indices()
        visible_set = set(visible)
        for i, (var, _) in enumerate(self._mgr_mod_vars):
            if i not in visible_set:
                var.set(False)
        self._mgr_lb.delete(0, tk.END)
        for mv_idx in visible:
            var, path = self._mgr_mod_vars[mv_idx]
            self._mgr_lb.insert(tk.END, self._mgr_label(var, path))
        if 0 <= sel < self._mgr_lb.size():
            self._mgr_lb.selection_set(sel)
            self._mgr_lb.see(sel)
        # Persist immediately so the on-disk order/enabled-state always matches what's shown.  Without
        # this, changes (reorder, enable/disable, imported load order) only survived if the app closed
        # cleanly — any reload from disk (game-version switch, restart) reverted to the stale order, and
        # an imported order appeared to "fall back" to the mods' old positions.  Skipped while loading.
        if not self._mgr_state_loading:
            self._save_mgr_state()

    # ── Share / Import load order ─────────────────────────────────────────────

    def _mgr_share_order(self):
        game_ver = self._game_version()
        mode_label = "R.U.S.E. COMPAT" if game_ver == "compat" else "R.U.S.E."
        visible = self._visible_mv_indices()
        active = [(var, path) for idx in visible
                  for var, path in [self._mgr_mod_vars[idx]] if var.get()]
        if not active:
            ui_util.info(self, t("mgr.nothing_share"),
                                t("mgr.no_mods_are_currently_enabled"))
            return
        lines = [f"=== {mode_label} Load Order ==="]
        for i, (_, path) in enumerate(active, 1):
            is_compat_mod = path.lower().endswith(".compat.rmod")
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                name = d.get("name", Path(path).stem)
                ver  = d.get("version", "")
            except Exception:
                name = Path(path).stem
                ver  = ""
            prefix = "[COMPAT] " if is_compat_mod else ""
            lines.append(f"{i}. {prefix}{name}" + (f" | v{ver}" if ver else ""))
        lines.append("=== End Load Order ===")
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._mgr_foot.set(
            t("mgr.load_order_n_active_mod",
              n=len(active)))
        self.after(5000, lambda: self._mgr_foot.set(t("mgr.ready")))
        ui_util.show_text(
            self, t("mgr.load_order_copied"),
            t("mgr.load_order_n_active_mod_2", n=len(active)),
            text)

    def _mgr_import_order(self):
        dlg = ui_util.themed_toplevel(
            self, t("mgr.import_load_order_from_friend"),
            resizable=True, min_size=(540, 500))

        ttk.Label(dlg,
                  text=t("mgr.paste_friend_s_shared_load"),
                  foreground=_R_TEXT, justify="left", font=_F_LOG,
                  ).pack(padx=10, pady=(10, 4), anchor="w")

        txt_frame = ttk.LabelFrame(dlg, text=t("mgr.paste_load_order_here"))
        txt_frame.pack(fill="both", expand=True, padx=10, pady=4)
        txt = _ThemedScrolledText(
            txt_frame, height=7, font=_F_LOG,
            background=_R_BG_WIDGET, foreground=_R_TEXT,
            insertbackground=_R_GOLD, relief="flat")
        txt.pack(fill="both", expand=True, padx=4, pady=4)

        # Pre-fill from clipboard if it looks like a load order
        try:
            clip = self.clipboard_get()
            if "Load Order" in clip:
                txt.insert("1.0", clip)
        except Exception:
            pass

        bf = ttk.Frame(dlg)
        bf.pack(fill="x", padx=10, pady=4)

        res_frame = ttk.LabelFrame(dlg, text=t("mgr.results"))
        res_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        res_log = _make_log(res_frame, height=7)
        res_log.pack(fill="both", expand=True, padx=4, pady=4)

        def do_paste():
            try:
                txt.delete("1.0", tk.END)
                txt.insert("1.0", self.clipboard_get())
            except Exception:
                pass

        ttk.Button(bf, text=t("mgr.paste_from_clipboard"),
                   command=do_paste).pack(side="left", padx=4)
        ttk.Button(bf, text=t("mgr.check_apply"),
                   command=lambda: self._mgr_do_import(
                       txt.get("1.0", tk.END).strip(), res_log)
                   ).pack(side="left", padx=4)
        ttk.Button(bf, text=t("common.close"),
                   command=dlg.destroy).pack(side="right", padx=4)

    def _mgr_parse_load_order(self, text: str) -> tuple:
        """Parse shared load order text.

        Returns (entries, detected_mode) where:
          entries       = [(name, version, is_compat), ...]
          detected_mode = 'compat' | 'public' | None  (None = header not recognised)
        """
        in_block = False
        detected_mode = None
        entries = []
        for line in text.splitlines():
            stripped = line.strip()
            if "Load Order" in stripped and "R.U.S.E." in stripped:
                in_block = True
                detected_mode = "compat" if "COMPAT" in stripped else "public"
                continue
            if "End Load Order" in stripped:
                break
            if not in_block or not stripped:
                continue
            m = _LO_NUM.match(stripped)
            if m:
                body = m.group(1).strip()
                # Version is the LAST " | v…" on the line; a mod name may itself contain " | ".
                if " | v" in body:
                    raw_name, ver = body.rsplit(" | v", 1)
                    raw_name, ver = raw_name.strip(), ver.strip()
                else:
                    raw_name, ver = body, ""
                is_compat = raw_name.startswith("[COMPAT] ")
                name = raw_name[len("[COMPAT] "):] if is_compat else raw_name
                entries.append((name, ver, is_compat))
        return entries, detected_mode

    def _mgr_scan_mods_meta(self) -> list:
        """Return [(name, version, path, is_compat), ...] for all known mods."""
        results = []
        for _, path in self._mgr_mod_vars:
            is_compat = path.lower().endswith(".compat.rmod")
            try:
                with open(path, encoding="utf-8") as fh:
                    d = json.load(fh)
                results.append((d.get("name", Path(path).stem), d.get("version", ""), path, is_compat))
            except Exception:
                results.append((Path(path).stem, "", path, is_compat))
        return results

    def _mgr_do_import(self, text: str, log_widget):
        """Parse, verify, and apply a shared load order."""
        _log_clear(log_widget)
        entries, detected_mode = self._mgr_parse_load_order(text)
        if not entries:
            _log(log_widget, t("mgr.no_valid_load_order_entries"), "err")
            return

        current_ver = self._game_version()
        if detected_mode and detected_mode != current_ver:
            _mode_label = {"compat": "R.U.S.E. COMPAT", "public": "R.U.S.E."}
            want = _mode_label.get(detected_mode, detected_mode)
            have = _mode_label.get(current_ver, current_ver)
            ui_util.warning(
                self,
                t("mgr.wrong_game_version"),
                t("mgr.load_order_was_made_want", want=want, have=have))
            return

        available = self._mgr_scan_mods_meta()
        by_name: dict = {}
        for name, ver, path, is_compat in available:
            key = (name.strip().lower(), is_compat)
            by_name.setdefault(key, []).append((ver, path))

        matched_paths = []   # paths of mods that appear in the shared order (will be ON)
        all_ok = True

        for name, req_ver, is_compat in entries:
            key = (name.strip().lower(), is_compat)
            candidates = by_name.get(key, [])
            tag = "[COMPAT] " if is_compat else ""
            if not candidates:
                _log(log_widget,
                     t("mgr.missing_tag_name", tag=tag, name=name)
                     + (t("mgr.need_v_req_ver", req_ver=req_ver) if req_ver else ""),
                     "err")
                all_ok = False
                continue

            exact = [(v, p) for v, p in candidates if v == req_ver] if req_ver else candidates
            if exact:
                ver, path = exact[0]
                _log(log_widget,
                     t("mgr.ok_tag_name", tag=tag, name=name)
                     + (t("mgr.v_ver", ver=ver) if ver else ""), "ok")
                matched_paths.append(path)
            else:
                ver, path = candidates[0]
                _log(log_widget,
                     t("mgr.version_tag_name_have_v",
                       tag=tag, name=name, ver=ver, req_ver=req_ver), "warn")
                all_ok = False
                matched_paths.append(path)

        # A friend's order might list the same mod twice — keep the FIRST occurrence and drop repeats
        # (and say so), so the applied count is honest and the list isn't doubled before _ensure_bundled
        # silently de-dups it.
        seen_mp, deduped = set(), []
        for p in matched_paths:
            if p in seen_mp:
                _log(log_widget, t("mgr.skip_stem_listed_more_than", stem=Path(p).stem), "warn")
                continue
            seen_mp.add(p)
            deduped.append(p)
        matched_paths = deduped

        if not matched_paths:
            _log(log_widget, t("mgr.none_mods_order_were_found"), "err")
            return

        # Everything not in the shared order goes AFTER the imported mods and is switched OFF — INCLUDING
        # shipped (SAFE) mods, so they behave exactly like external ones: an imported order disables the
        # shipped mods it omits, and the mods it lists (shipped or external) become active and are ranked
        # at the TOP in import order.  Previously shipped mods were skipped here and re-injected always-on
        # above the imported mods, so an import could neither disable nor outrank them.
        matched_set = set(matched_paths)
        extra = [(var, path) for var, path in self._mgr_mod_vars
                 if path not in matched_set]

        self._mgr_mod_vars.clear()

        for path in matched_paths:
            self._mgr_mod_vars.append((tk.BooleanVar(value=True), path))

        for _, path in extra:
            self._mgr_mod_vars.append((tk.BooleanVar(value=False), path))
            _log(log_widget,
                 t("mgr.off_stem_not_shared_order", stem=Path(path).stem), "info")

        self._ensure_bundled_in_list()   # dedup + re-inject any shipped mod somehow missing (invariant)

        # Legacy .compat.rmod visibility is superseded by the build selector (pick that build's library).
        if (self._game_version() == "public"
                and not self._show_compat_var.get()
                and any(p.lower().endswith(".compat.rmod") for p in matched_paths)):
            self._show_compat_var.set(True)

        self._mgr_rebuild()

        status = t("mgr.all_mods_matched") if all_ok else t("mgr.some_mods_missing_version_mismatched")
        _log(log_widget,
             t("mgr.applied_n_mod_s_enabled",
               n=len(matched_paths), status=status), "head")

    # ── Bundled (shipped) mods — baked into the exe, authoritative for multiplayer ──────────────

    @staticmethod
    def _version_major(ver) -> str:
        """Major version number from a version string ('2.3.1' -> '2', 'v4' -> '4', '' -> '1').
        Mirrors the Convert tab's _V#/-v# major, so a mod's identity tracks its major version."""
        s = str(ver).strip().lstrip("vV")
        head = s.split(".")[0]
        digits = "".join(c for c in head if c.isdigit())
        return digits or "1"

    def _rmod_identity(self, path) -> tuple:
        """A mod's identity = (branch, internal-name lowercased, MAJOR version), read from the rmod's
        own name/version fields (NOT the filename).  Two mods with the same identity are 'the same mod';
        changing the NAME or the MAJOR version makes it a distinct mod.  branch = compat for *.compat.rmod."""
        p = str(path)
        branch = "compat" if p.lower().endswith(".compat.rmod") else "public"
        name, ver = "", ""
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            name = str(d.get("name", "")).strip().lower()
            ver = d.get("version", "")
        except Exception:
            pass
        return (branch, name, self._version_major(ver))

    def _scan_bundled(self):
        """Discover the shipped rmods baked into the exe (build.py → _MEIPASS/bundled_mods/).  Each is
        authoritative: always applied, can't be disabled, and hides any external mod with the same
        identity (branch+name+major).  No bundle (running from source) → nothing shipped, normal mods."""
        self._bundled_order = []
        self._bundled_paths = set()
        self._bundled_keys = set()
        self._bundled_meta = {}
        if not _BUNDLED_MODS_DIR or not _BUNDLED_MODS_DIR.is_dir():
            return
        # Build-time deploy-cache stamps (size + mtime per shipped rmod) baked by build.py.  The
        # deployment cache key conjoins each rmod's name:size:mtime; a bundled mod's _MEIPASS mtime is
        # reset every launch, so we use these fixed build-time stamps instead.  Keyed by (sub, filename).
        stamp_by_file = {}
        man = _BUNDLED_MODS_DIR / "manifest.json"
        if man.is_file():
            try:
                for m in json.loads(man.read_text(encoding="utf-8")).get("mods", []):
                    stamp_by_file[(m.get("sub"), m.get("file"))] = (m.get("size"), m.get("mtime"))
            except Exception:
                stamp_by_file = {}
        # Bundled mods are baked per BUILD-ID folder (bundled_mods/v<buildid>/); load the set for the
        # EFFECTIVE (viewed) build — so picking a build in the selector lists ITS bundled mods, just like
        # its folder mods. (.compat.rmod also ends in .rmod, so one glob covers both formats.)
        bid = self._effective_mod_build()
        files = []
        if bid:
            bdir = _BUNDLED_MODS_DIR / f"v{bid}"
            if bdir.is_dir():
                files = sorted(bdir.glob("*.rmod"))
        for f in files:
            ident = self._rmod_identity(f)
            if not ident[1]:           # no readable internal name — can't key it, skip
                continue
            sp = str(f)
            self._bundled_order.append(sp)
            self._bundled_paths.add(sp)
            self._bundled_keys.add(ident)
            stamp = stamp_by_file.get((f.parent.name, f.name))
            if stamp is not None:
                self._bundled_meta[sp] = {"size": stamp[0], "mtime": stamp[1]}

    def _is_bundled(self, path) -> bool:
        return str(path) in self._bundled_paths

    def _scan_predeploy(self):
        """Discover the AUTO-DEPLOY (predeploy) rmods baked into the exe (build.py → _MEIPASS/predeploy/)
        — in dev, straight from <repo>/predeploy/.  These apply on every Deploy BEFORE the user's mods
        and never appear in the manager list (so the user can't reorder, disable, or even see them).
        Per BUILD-ID folder: predeploy/v<buildid>/*.rmod (the installed build's set). A sibling
        manifest.json (baked at build time) carries each rmod's stable size+mtime — the deploy cache
        key keys off those instead of the per-launch _MEIPASS timestamp (same as the SAFE bundled mods)."""
        self._predeploy_order = {}
        self._predeploy_meta = {}
        bid = self._game_build_id()
        if not _PREDEPLOY_DIR or not _PREDEPLOY_DIR.is_dir() or not bid:
            return
        # Build-time stamps (frozen exe only; dev uses live stat).
        stamp_by_file = {}
        man = _PREDEPLOY_DIR / "manifest.json"
        if man.is_file():
            try:
                for m in json.loads(man.read_text(encoding="utf-8")).get("mods", []):
                    stamp_by_file[(m.get("sub"), m.get("file"))] = (m.get("size"), m.get("mtime"))
            except Exception:
                stamp_by_file = {}
        sub = f"v{bid}"
        d = _PREDEPLOY_DIR / sub
        if d.is_dir():
            order = self._predeploy_order[bid] = []
            for f in sorted(d.glob("*.rmod")):     # .compat.rmod also matches; one folder per build
                sp = str(f)
                order.append(sp)
                stamp = stamp_by_file.get((sub, f.name))
                if stamp is not None and stamp[0] is not None:
                    self._predeploy_meta[sp] = {"size": stamp[0], "mtime": stamp[1]}

    def _bundled_saved_enabled(self) -> dict:
        """{identity-string: enabled} for bundled mods from the saved manager state.  Bundled mods are
        keyed by stable IDENTITY (branch::name::major), not their unstable _MEIPASS path, so a user's
        enable/disable choice for a SAFE mod survives restarts."""
        out = {}
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    st = json.load(f)
                for e in self._saved_entries_for_current_build(st):
                    bid = e.get("bundled_id")
                    if bid:
                        out[bid] = bool(e.get("enabled", True))
            except Exception:
                pass
        return out

    @staticmethod
    def _norm_path(p) -> str:
        """Canonical key for comparing mod paths (case/slash-insensitive) so the same file saved with
        a different slash direction or case can't produce a duplicate list row."""
        try:
            return os.path.normcase(os.path.abspath(str(p)))
        except Exception:
            return str(p).lower()

    def _rel_mod_path(self, p) -> str:
        """Serialize an external mod path RELATIVE to the mods folder for .manager_state.json.  The
        working dir and mods folder are always derived from the exe's location (never user-set), so a
        relative path means a moved/copied install still finds its mods and no machine-specific
        absolute path is ever written.  Falls back to the absolute path only if `p` somehow isn't
        under the mods folder (shouldn't happen — _mgr_add copies every mod into it)."""
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        try:
            rel = os.path.relpath(os.path.abspath(str(p)), os.path.abspath(str(base)))
            if not rel.startswith(".."):       # under the mods folder → store it relative
                return rel
        except Exception:                      # e.g. a different drive — keep it absolute
            pass
        return str(p)

    def _abs_mod_path(self, rel) -> str:
        """Resolve a saved mod path from .manager_state.json back to an absolute path under the mods
        folder.  An already-absolute value (legacy state, or the fallback above) is returned
        unchanged; an empty value stays empty so callers can skip it."""
        if not rel:
            return ""
        rp = Path(rel)
        if rp.is_absolute():
            return str(rp)
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        return str(base / rp)

    def _ensure_bundled_in_list(self):
        """RECONCILE the mod list after any (re)build — called from every list-building path so the
        invariant holds no matter how an entry got in:
          • DROP a duplicate path (keep the first) — a stale saved-state path in a different
            slash/case form must not double a mod (the 'artifacts on first load' the user hit);
          • DROP an external entry a shipped (bundled) mod shadows (same branch+name+major) — the
            WHOLE entry leaves the list, not just its label, so it can't be enabled;
          • INJECT any missing bundled (SAFE) mod at the top (default DISABLED; enabled-state
            remembered by identity, since the _MEIPASS path isn't stable).
        With no bundle (running from source) only the de-dup runs, so normal loading is unaffected."""
        seen, kept = set(), []
        for v, p in self._mgr_mod_vars:
            key = self._norm_path(p)
            if key in seen:
                continue
            if (self._bundled_keys and not self._is_bundled(p)
                    and self._rmod_identity(p) in self._bundled_keys):
                continue
            seen.add(key)
            kept.append((v, p))
        self._mgr_mod_vars = kept
        if not self._bundled_order:
            return
        have = {self._norm_path(p) for _, p in self._mgr_mod_vars}
        saved = self._bundled_saved_enabled()
        saved_cache = self._bundled_saved_cache()
        inject = []
        for bp in self._bundled_order:
            if self._norm_path(bp) in have:
                continue
            ident = "::".join(self._rmod_identity(bp))
            inject.append((tk.BooleanVar(value=saved.get(ident, False)), bp))
            self._mgr_cache_flags[str(bp)] = tk.BooleanVar(value=saved_cache.get(ident, False))
        if inject:
            self._mgr_mod_vars[:0] = inject

    def _stamp_build_on_rmods(self, folder: Path) -> None:
        """ASK whether to record this build id on any rmod in the build-id folder whose version
        doesn't match (legacy branch-name version, blank, or a different build) — placing it here
        signals intent, but the version edit is PERMANENT, so the user confirms first. Only the
        'game_version' field is touched, only on Yes, and only for files that differ. A declined
        file isn't re-asked this session. Called from the user-initiated 'Scan Mods Folder' only,
        never the automatic startup/refresh scans (so it can't nag on launch)."""
        bid = self._game_build_id()
        if not bid or not folder or not folder.is_dir():
            return
        declined = getattr(self, "_stamp_declined", None)
        if declined is None:
            declined = self._stamp_declined = set()
        pending = []
        for f in folder.glob("*.rmod"):
            if str(f) in declined:
                continue
            try:
                obj = json.loads(f.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if str(obj.get("game_version", "")) == bid:
                continue                                   # already this build
            pending.append((f, obj))
        if not pending:
            return
        label = self._branch_label()
        shown = "\n".join("  - {n}  (now '{v}')".format(
            n=f.name, v=(o.get("game_version") or "unset")) for f, o in pending[:12])
        more = "" if len(pending) <= 12 else t("mgr.n_more", n=len(pending) - 12)
        ok = ui_util.confirm(
            self,
            t("mgr.tag_mods_label", label=label),
            t("mgr.n_mod_s_folder_are",
              n=len(pending), shown=shown, more=more, label=label))
        if not ok:
            for f, _ in pending:
                declined.add(str(f))                       # don't re-ask this session
            return
        n = 0
        for f, obj in pending:
            obj["game_version"] = bid
            try:
                f.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
                n += 1
            except Exception:
                logging.exception("Couldn't stamp game_version into %s", f)
        if hasattr(self, "_mgr_log"):
            _log(self._mgr_log, t("mgr.tagged_n_mod_s_as", n=n, label=label), "head")

    def _rmod_matches_build(self, path) -> bool:
        """True if the rmod at ``path`` is tagged (``game_version``) for the current build id — or no
        build id is detected, so we can't gate.  The SINGLE source of the game_version gate, shared by the
        folder scan and by state-restore, so a saved mod tagged for a different build can't slip back into
        the list.  Gates on the EFFECTIVE (viewed) build so selecting a build shows that build's mods."""
        bid = self._effective_mod_build()
        if not bid:
            return True
        try:
            return str(json.loads(Path(path).read_text(encoding="utf-8-sig")).get("game_version", "")) == bid
        except Exception:
            return False

    def _mgr_scan_both(self, prompt_stamp: bool = False):
        """Scan the mods folder for the DETECTED BUILD ID (mods/v<buildid>/) and update the
        file caches, split by format (.compat.rmod = OG, .rmod = remaster). ONLY rmods whose
        recorded game_version MATCHES this build are loaded into the list — a mod tagged for a
        different/older version (or untagged) is ignored until the user runs the manual 'Scan
        Mods Folder', which offers to tag it for this build. So the list always reflects mods
        actually meant for the installed version. External files a shipped (bundled) mod hides
        (same branch + internal name + major) are dropped."""
        mdir = self._mods_dir()
        if prompt_stamp:                                   # only the deliberate 'Scan Mods Folder'
            self._stamp_build_on_rmods(mdir)
        _is_for_build = self._rmod_matches_build         # single source of the game_version gate

        comp = sorted(f for f in (mdir.glob("*.compat.rmod") if mdir.exists() else [])
                      if _is_for_build(f))
        pub = sorted(
            f for f in (mdir.glob("*.rmod") if mdir.exists() else [])
            if not f.name.lower().endswith(".compat.rmod") and _is_for_build(f)
        )
        if self._bundled_keys:
            comp = [f for f in comp if self._rmod_identity(f) not in self._bundled_keys]
            pub = [f for f in pub if self._rmod_identity(f) not in self._bundled_keys]
        self._scanned_compat = comp
        self._scanned_public = pub

    def _mgr_browse_mods(self):
        """Reveal the BASE mods folder in the OS file explorer (not a build-id subfolder — just the
        root, so the user can browse every rmod file across all builds)."""
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        try:
            base.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(base))               # Windows: open in Explorer
            else:
                ui_util.info(self, t("mgr.folder"), str(base))
        except Exception as e:
            ui_util.error(self, t("mgr.open_failed"),
                                 t("mgr.could_not_open_path_e", path=base, e=e))

    def _mgr_scan(self):
        """Scan both mod dirs, refresh caches, append any newly found mods at the bottom.
        This is the user-initiated scan, so it offers to tag legacy rmods for this build."""
        self._mgr_scan_both(prompt_stamp=True)
        before   = len(self._mgr_mod_vars)
        existing = {self._norm_path(p) for _, p in self._mgr_mod_vars}   # case/slash-insensitive dedupe
        ver = self._game_version()
        scan_files = (self._scanned_compat if ver == "compat"
                      else list(self._scanned_public) + list(self._scanned_compat))
        for f in scan_files:
            path = str(f)
            if self._norm_path(path) not in existing:
                self._mgr_mod_vars.append((tk.BooleanVar(value=False), path))
                existing.add(self._norm_path(path))
        self._ensure_bundled_in_list()
        added = len(self._mgr_mod_vars) - before
        if added:
            self._mgr_rebuild()
        n_c = len(self._scanned_compat)
        n_p = len(self._scanned_public)
        _log(self._mgr_log,
             f"Scanned — {n_c} compat, {n_p} public mod(s) found.  "
             f"{added} new mod(s) added.", "head")

    def _mgr_add(self):
        mods_dir = self._mods_dir()
        ext = self._rmod_ext()
        for p in filedialog.askopenfilenames(
                parent=self,
                title=t("mgr.select_ext_files", ext=ext),
                filetypes=self._rmod_filter()):
            src = Path(p)
            dest = mods_dir / src.name
            if src.resolve() != dest.resolve():
                try:
                    mods_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dest))
                except Exception as e:
                    ui_util.error(self, t("mgr.copy_failed"),
                        t("mgr.could_not_copy_name_mods", name=src.name, e=e))
                    continue
            self._mgr_add_path(str(dest))

    def _mgr_remove(self):
        visible  = self._visible_mv_indices()
        to_remove = {visible[lb_idx] for lb_idx in self._mgr_lb.curselection()
                     if lb_idx < len(visible)}
        self._mgr_mod_vars = [(v, p) for i, (v, p) in enumerate(self._mgr_mod_vars)
                              if i not in to_remove]
        self._mgr_rebuild()

    def _mgr_clear(self):
        self._mgr_mod_vars.clear()
        self._ensure_bundled_in_list()   # shipped mods can't be cleared away — re-inject them
        self._mgr_rebuild()              # rebuilds the listbox and persists the cleared state

    def _mgr_up(self):
        sel = self._mgr_lb.curselection()
        if len(sel) != 1 or sel[0] == 0: return
        lb_idx  = sel[0]
        visible = self._visible_mv_indices()
        mv_i, mv_j = visible[lb_idx], visible[lb_idx - 1]
        self._mgr_mod_vars[mv_i], self._mgr_mod_vars[mv_j] = \
            self._mgr_mod_vars[mv_j], self._mgr_mod_vars[mv_i]
        self._mgr_rebuild(lb_idx - 1)

    def _mgr_down(self):
        sel = self._mgr_lb.curselection()
        if len(sel) != 1: return
        lb_idx  = sel[0]
        visible = self._visible_mv_indices()
        if lb_idx >= len(visible) - 1: return
        mv_i, mv_j = visible[lb_idx], visible[lb_idx + 1]
        self._mgr_mod_vars[mv_i], self._mgr_mod_vars[mv_j] = \
            self._mgr_mod_vars[mv_j], self._mgr_mod_vars[mv_i]
        self._mgr_rebuild(lb_idx + 1)

    def _mgr_top(self):
        sel = self._mgr_lb.curselection()
        if len(sel) != 1 or sel[0] == 0: return
        lb_idx  = sel[0]
        visible = self._visible_mv_indices()
        mv_idx  = visible[lb_idx]
        item = self._mgr_mod_vars.pop(mv_idx)
        self._mgr_mod_vars.insert(visible[0], item)
        self._mgr_rebuild(0)

    def _mgr_bottom(self):
        sel = self._mgr_lb.curselection()
        if len(sel) != 1: return
        lb_idx  = sel[0]
        visible = self._visible_mv_indices()
        last_lb = len(visible) - 1
        if lb_idx == last_lb: return
        mv_idx  = visible[lb_idx]
        mv_last = visible[last_lb]
        item = self._mgr_mod_vars.pop(mv_idx)
        # mv_idx < mv_last always; after pop, mv_last shifts to mv_last-1,
        # so inserting at mv_last places the item right after the new last visible entry
        self._mgr_mod_vars.insert(mv_last, item)
        self._mgr_rebuild(last_lb)

    # ── Backup / Restore ──────────────────────────────────────────────────────

    def _mgr_create_backup(self):
        if self._mgr_running:   # a deploy/backup/restore is already touching the game files
            return
        if not self._settings["game_root"]:
            ui_util.error(self, t("mgr.no_game_root"), t("mgr.set_game_root_settings_first"))
            return
        game_root = Path(self._settings["game_root"])
        if not _is_dir_safe(game_root / "Data"):
            ui_util.error(self, t("mgr.not_found"), t("mgr.data_folder_not_found_path", path=game_root / 'Data'))
            return
        bd = self._backup_dir()
        has_files = bd.exists() and any(bd.rglob("*.dat"))
        if has_files:
            if not ui_util.confirm(self, t("mgr.overwrite"),
                                       t("mgr.backup_already_exists_bd_overwrite", bd=bd)):
                return
            # NOTE: do NOT delete the existing backup here.  _do_backup builds the new copy in a sibling
            # .tmp and only swaps it in atomically (rmtree(bd) + os.replace) AFTER the whole copy succeeds,
            # so the old backup must stay intact until then — deleting it up front means a failed/interrupted
            # copy (disk full, locked file, kill) would leave the user with NO backup at all.
        self._mgr_set_busy(True)
        try:
            self._mgr_show_backup_warn()
            threading.Thread(target=self._do_backup,
                             args=(game_root, bd), daemon=True).start()
        except Exception:
            self._mgr_set_busy(False)   # worker never started — don't leave the UI stuck-busy
            raise

    def _do_backup(self, game_root: Path, bd: Path):
        # Mirror the game ROOT into the backup: Data/PC/<sub>/<dat> and Maps/PC/<terrain dats>.  The Maps
        # folder (per-terrain world/minimap dats) is large, so this can take a while.
        # Build the backup in a sibling ``.tmp`` folder and rename it into place only when the WHOLE copy
        # finishes.  An interrupted backup (crash, disk full, kill) then leaves the .tmp — never a partial
        # `bd`.  This matters because a partial backup passes the "has a .dat" check everywhere: deploy
        # would read clean source from an incomplete backup, and Restore Clean would leave the un-backed-up
        # dats still modded — the opposite of what the button promises.
        tmp = bd.with_name(bd.name + ".tmp")
        try:
            _log(self._mgr_log, t("mgr.creating_backup_game_root_bd", game_root=game_root, bd=bd), "head")
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True, exist_ok=True)
            count = 0
            for top in ("Data", "Maps"):
                src_root = game_root / top
                if not src_root.is_dir():
                    continue
                for src in src_root.rglob("*"):
                    if src.is_file():
                        rel  = src.relative_to(game_root)     # e.g. Data\PC\190852\X.dat, Maps\PC\Y.dat
                        dest = tmp / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # Show each file BEFORE copying it, so the user sees activity — especially the
                        # pause on a big .dat.  "step" = in-progress (cyan).  Footer mirrors the current
                        # file for a smooth live indicator.
                        _log(self._mgr_log, t("mgr.backing_up_rel", rel=rel), "step")
                        self._ui(lambda r=rel: self._mgr_foot.set(t("mgr.backing_up_r", r=r)))
                        shutil.copy2(src, dest)
                        count += 1
            if bd.exists():
                shutil.rmtree(bd)
            os.replace(tmp, bd)                               # atomic publish — partial backups never appear
            _log(self._mgr_log, t("mgr.backup_complete_count_files", count=count), "ok")   # DONE = green
        except Exception as e:
            _log(self._mgr_log, t("mgr.backup_error_e", e=e), "err")
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)                        # don't leave a partial temp behind
            except Exception:
                pass
        finally:
            self._ui(self._mgr_refresh_status)
            self._ui(lambda: self._mgr_set_busy(False))

    def _mgr_restore_clean(self):
        if self._mgr_running:   # a deploy/backup/restore is already touching the game files
            return
        bd = self._backup_dir()
        if not bd.exists():
            ui_util.error(self, t("mgr.no_backup"), t("mgr.no_backup_found_create_one"))
            return
        if not self._settings["game_root"]:
            ui_util.error(self, t("mgr.no_game_root"), t("mgr.set_game_root_settings_first"))
            return
        if not ui_util.confirm(self, t("mgr.restore_clean"),
                                   t("mgr.copy_original_backup_files_back")):
            return
        self._mgr_set_busy(True)
        try:
            threading.Thread(target=self._do_restore,
                             args=(bd, Path(self._settings["game_root"])), daemon=True).start()
        except Exception:
            self._mgr_set_busy(False)   # worker never started — don't leave the UI stuck-busy
            raise

    def _do_restore(self, bd: Path, game_root: Path):
        # The backup mirrors the game root (Data/… and Maps/…), so copy those subtrees back over the
        # install.  Only Data/ and Maps/ are restored — stray files at the backup root (e.g. deploy-time
        # timestamped .bak copies) are deliberately ignored.
        try:
            _log(self._mgr_log, t("mgr.restoring_clean_game_files"), "head")
            count = 0
            for top in ("Data", "Maps"):
                sroot = bd / top
                if not sroot.is_dir():
                    continue
                for src in sroot.rglob("*"):
                    if src.is_file():
                        rel  = src.relative_to(bd)
                        dest = game_root / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        _log(self._mgr_log, t("mgr.restoring_rel", rel=rel), "step")  # in-progress (cyan)
                        self._ui(lambda r=rel: self._mgr_foot.set(t("mgr.restoring_r", r=r)))
                        shutil.copy2(src, dest)
                        count += 1
            mod_out = self._mod_out_dir()
            if mod_out.exists():
                shutil.rmtree(mod_out)
            # Reclaim deploy-cache disk here too.  Caches are fully regenerable on the next deploy, and
            # Restore Clean is the natural point to clear them — otherwise old cached_deployments/v<bid>/
            # trees accumulate forever across game updates.
            cache_root = Path(self._settings["working_dir"]) / "output" / "cached_deployments"
            if cache_root.exists():
                shutil.rmtree(cache_root, ignore_errors=True)
            self._mgr_set_deployed_dats([])   # game is fully clean now — nothing left deployed to track
            _log(self._mgr_log, t("mgr.restored_count_files_game_clean", count=count), "ok")   # DONE = green
            self._ui(lambda: self._mgr_foot.set(t("mgr.game_restored_clean_state")))
        except Exception as e:
            _log(self._mgr_log, t("mgr.restore_error_e", e=e), "err")
        finally:
            self._ui(lambda: self._mgr_set_busy(False))

    # ── Deploy ────────────────────────────────────────────────────────────────

    def _mgr_deploy(self):
        if self._mgr_running:
            return
        # A Convert-tab job (make / update / migrate an .rmod) may be REWRITING a mod file that this deploy
        # would read — deploying now could pick up a half-written .rmod.  Block until it finishes.
        if getattr(self, "_conv_running", False):
            ui_util.info(self, t("mgr.please_wait"),
                         t("mgr.conversion_still_running_let_finish"))
            return
        if not self._settings["game_root"]:
            ui_util.error(self, t("mgr.no_game_root"), t("mgr.set_game_root_settings_first"))
            return
        bd = self._backup_dir()
        if not bd.exists() or not any(bd.rglob("*.dat")):
            ui_util.error(self, t("mgr.no_backup"),
                                 t("mgr.no_game_file_backup_found"))
            return
        active = [p for v, p in self._mgr_mod_vars if v.get()]
        # Auto-deployed 'unofficial patch' rmods baked into the exe (predeploy/<branch>/) — always
        # applied FIRST so the user's mods (and any SAFE-bundled mods) layer on top of them.  Invisible
        # in the list; matched to the currently-detected game branch.  See _scan_predeploy.
        predeploy = list(self._predeploy_order.get(self._game_build_id(), []))
        active = predeploy + active
        if not active:
            ui_util.info(self, t("mgr.no_active_mods"),
                                t("mgr.check_least_one_mod_deploy"))
            return
        dry = self._mgr_dry.get()
        # Read every Tk variable the deploy needs HERE, on the main thread — the worker thread must not
        # touch the Tk interpreter (a cross-thread .get() is as unsafe as a cross-thread widget insert).
        # Mirrors how `dry` is already snapshotted and passed in as an argument.
        cache_on = bool(self._mgr_cache_enabled.get())
        per_mod  = bool(self._mgr_per_mod_cache.get())
        regen    = bool(self._mgr_regen.get())
        per_mod_flags = {str(p): bool(self._mod_cache_var(p).get()) for p in active}
        self._mgr_set_busy(True)
        try:
            self._mgr_foot.set(t("mgr.deploying_mods"))
            threading.Thread(
                target=self._do_deploy,
                args=(bd, active, dry, cache_on, per_mod, regen, per_mod_flags),
                daemon=True).start()
        except Exception:
            self._mgr_set_busy(False)   # worker never started — don't leave the UI stuck-busy
            raise

    def _mgr_launch_game(self):
        """Launch RUSE.exe from the configured game root."""
        if self._mgr_running:
            return
        game_root = self._settings.get("game_root", "").strip()
        if not game_root:
            ui_util.error(self, t("mgr.no_game_root"), t("mgr.set_game_root_settings_first"))
            return
        exe = Path(game_root) / "RUSE.exe"
        if not _exists_safe(exe):
            ui_util.error(
                self,
                t("mgr.ruse_exe_not_found"),
                t("mgr.could_not_find_ruse_exe", game_root=game_root))
            return
        try:
            subprocess.Popen([str(exe)], cwd=game_root)
            _log(self._mgr_log, t("mgr.launched_r_u_s_e_2", exe=exe), "ok")
            self._mgr_foot.set(t("mgr.launched_r_u_s_e"))
        except Exception as ex:
            _log(self._mgr_log, t("mgr.failed_launch_r_u_s", ex=ex), "err")
            ui_util.error(self, t("mgr.launch_failed"), t("mgr.could_not_launch_r_u", ex=ex))

    def _do_deploy(self, bd: Path, active: list, dry: bool,
                   cache_on: bool, per_mod: bool, regen: bool, per_mod_flags: dict):
        try:
            # rmod dat paths are GAME-ROOT-relative now (Data/PC/<sub>/<dat>, Maps/PC/<dat>), and the
            # backup `bd` mirrors the game root — so the applier reads clean dats from `bd`, and we
            # restore/overlay relative to the live game root.
            game_root = Path(self._settings["game_root"])
            mod_out  = self._mod_out_dir()
            game_ver = self._game_version()
            prefix   = "[DRY RUN] " if dry else ""
            ver_label = "R.U.S.E." if game_ver == "public" else "R.U.S.E. COMPAT"

            # ── Deployment cache (incremental / longest-prefix model) ───────────────────────────────
            # Master switch "Cache enabled" gates everything; "Always regenerate" bypasses REUSE (rebuild
            # fresh) but still writes the cache.  Mods apply in ORDER, so the cached result of the first K
            # mods is a valid BASE for applying the rest: probe the cache for the longest matching PREFIX
            # of the active list (full set → drop last → next-last → … → first); the longest hit becomes
            # the base and only the un-applied tail mods are layered on top (any dat the prefix never
            # touched starts from the clean backup).  No hit → apply all from the backup.
            #   WHAT GETS WRITTEN (to bound disk): "Per-mod cache points" OFF → only the full result;
            #   ON → only the prefixes ending at an rmod the user ticked.  Dry runs never read/write.
            # cache_on / per_mod / regen were read from their Tk vars on the MAIN thread in _mgr_deploy
            # and passed in — never read a Tk var from this worker thread.
            prefix_base = None      # cache dir whose dats already have mods[0:k] applied
            tail = list(active)     # mods still to apply on top of the base
            if cache_on and (not dry) and (not regen):
                for k in range(len(active), 0, -1):
                    cdir = self._deploy_cache_dir(active[:k])
                    if cdir is not None and cdir.is_dir() and any(cdir.rglob("*.dat")):
                        prefix_base, tail = cdir, active[k:]
                        break
            reused = len(active) - len(tail)
            exact  = (prefix_base is not None and not tail)

            _log(self._mgr_log,
                 t("mgr.prefix_deploying_n_mod_s",
                   prefix=prefix, n=len(active), ver_label=ver_label, bd=bd, mod_out=mod_out), "head")
            if prefix_base is not None:
                _log(self._mgr_log,
                     (t("mgr.cache_hit_all_reused_mod",
                        reused=reused, name=prefix_base.name[:12])
                      if exact else
                      t("mgr.cache_hit_reused_n_mod",
                        reused=reused, n=len(active), name=prefix_base.name[:12], tail=len(tail))), "step")

            all_results = []
            apply_raised = False                # any mod whose apply_mod() RAISED (partial/corrupt output)
            if exact:
                gen_root = prefix_base          # whole set already cached — overlay it directly
            else:
                gen_root = mod_out
                if not dry:
                    if mod_out.exists():
                        shutil.rmtree(mod_out)
                    mod_out.mkdir(parents=True, exist_ok=True)
                    if prefix_base is not None:
                        # Seed the work area with the cached prefix's dats; the applier layers the tail
                        # mods straight onto them (and pulls any dat the prefix didn't touch from bd).
                        _log(self._mgr_log, t("mgr.reusing_cached_base_copying_prepared"), "step")
                        for src in prefix_base.rglob("*"):
                            if src.is_file():
                                dest = mod_out / src.relative_to(prefix_base)
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(src, dest)
                # Per-dat progress: the applier calls this just before it opens each .dat — the slow step
                # for large data files.  Cyan "step" tells the user WHY a mod can pause for a while.
                def _prep(dr):
                    _log(self._mgr_log, t("mgr.preparing_dat_large_data_files", dat=dr), "step")
                # Shared touched-set across the freshly-applied mods → cross-mod conflict detection
                # (which later mod overwrote which earlier one).  Covers the `tail`; a reused cached prefix
                # isn't re-applied this run, so prefix↔tail conflicts aren't detected until a full deploy.
                deploy_state: dict = {}
                for i, mod_path in enumerate(tail):
                    _log(self._mgr_log, t("mgr.name", name=Path(mod_path).name), "head")
                    ok = False
                    try:
                        result = applier_mod.apply_mod(
                            mod_path      = mod_path,
                            game_data_dir = str(bd),
                            backup        = False,
                            dry_run       = dry,
                            output_dir    = str(mod_out) if not dry else None,
                            game_version  = game_ver,
                            progress      = _prep,      # live "preparing <dat>…" feedback during the slow part
                            deploy_state  = deploy_state,   # cross-mod conflict detection
                            source_build  = self._effective_mod_build(),   # mods' build (may be selected)
                            target_build  = self._game_build_id(),         # deploy onto the INSTALLED build
                        )
                        all_results.append(result)
                        for rec in result.change_log:
                            _log(self._mgr_log,
                                 f"  {rec.table}[{rec.instance_id}].{rec.prop}: "
                                 f"{rec.old_val} → {rec.new_val}", "info")   # detail (neutral, not "done")
                        for w in result.warnings:
                            _log(self._mgr_log, t("mgr.warn_w", w=w), "warn")
                        for e in result.errors:
                            _log(self._mgr_log, t("mgr.error_e", e=e), "err")
                        for rf in result.requires_repair:
                            _log(self._mgr_log, t("mgr.needs_repair_d", d=str(rf).strip()), "warn")
                        if result.requires_repair:
                            _log(self._mgr_log,
                                 t("mgr.n_change_s_could_not", n=len(result.requires_repair)), "warn")
                        if result.conflicts:
                            _log(self._mgr_log,
                                 t("mgr.n_edit_s_overwrite_earlier",
                                   n=len(result.conflicts)), "warn")
                            for cf in result.conflicts[:12]:
                                _log(self._mgr_log, t("mgr.d", d=str(cf)), "warn")
                            if len(result.conflicts) > 12:
                                _log(self._mgr_log,
                                     t("mgr.n_more_2", n=len(result.conflicts) - 12), "warn")
                        mrep = getattr(result, "map_report", None) or {}
                        if mrep.get("map"):
                            _log(self._mgr_log,
                                 t("mgr.built_v_deployed_v_b",
                                   a=mrep.get("from"), b=mrep.get("to"), n=mrep.get("remapped", 0)), "step")
                            if mrep.get("stale"):
                                _log(self._mgr_log,
                                     t("mgr.n_edit_s_target_values",
                                       n=len(mrep["stale"]), a=mrep.get("from")), "warn")
                                for pg, ch in mrep["stale"][:12]:
                                    nm = pg.ndf.replace("\\", "/").split("/")[-1]
                                    _log(self._mgr_log, t("mgr.d_tbl_m",
                                         d=nm, tbl=ch.table, m=ch.match), "warn")
                        _log(self._mgr_log, f"  {result.summary()}", "info")
                        ok = True
                    except Exception as ex:
                        _log(self._mgr_log, t("mgr.error_ex", ex=ex), "err")
                        apply_raised = True         # its output dat may be half-written — must not overlay

                    # Cache the cumulative result for the prefix ending at this mod (active[:reused+i+1]),
                    # so it never has to be re-applied.  Per-mod mode → only when THIS mod is ticked for
                    # caching; otherwise → only the full result (the last mod).
                    gpos = reused + i           # this mod's index in the full active list
                    plen = gpos + 1
                    want_cache = (per_mod_flags.get(str(active[gpos]), False) if per_mod
                                  else i == len(tail) - 1)
                    pc = self._deploy_cache_dir(active[:plen]) if (
                        cache_on and want_cache and ok and not dry) else None
                    if pc is not None and any(mod_out.rglob("*.dat")):
                        n = self._write_deploy_cache(pc, mod_out)
                        if n >= 0:
                            _log(self._mgr_log,
                                 t("mgr.cached_plen_n_mod_prefix",
                                   plen=plen, n=len(active), name=pc.name[:12]), "step")
                        else:
                            _log(self._mgr_log, t("mgr.warn_could_not_cache_prefix", plen=plen), "warn")

            # A mod that RAISED during apply may have left a half-written dat in mod_out.  Overlaying that
            # would ship a corrupt dat AND (because `touched` is scanned from the rmod JSON regardless of
            # apply success) could orphan a leftover dat the tracker then forgets.  Abort BEFORE touching
            # the live install: leave the game exactly as the previous deploy left it (tracker intact),
            # and tell the user which state we're in so they can fix/disable the failing mod and retry.
            if not dry and apply_raised:
                _log(self._mgr_log,
                     t("mgr.deploy_aborted_mod_failed_apply"), "err")
                self._ui(lambda: self._mgr_foot.set(
                    t("mgr.deploy_aborted_mod_failed_game")))
                return

            if not dry:
                # Every dat any patch type touches must be reset to clean before the overlay.  (Read
                # straight from the rmods, so this works whether the dats were generated or cached.)
                touched: set = set()
                for mod_path in active:
                    try:
                        with open(mod_path, encoding="utf-8") as f:
                            rmod = json.load(f)
                        for sect in ("patches", "file_patches", "loc_patches",
                                     "sdb_patches", "scenario_patches"):
                            for g in rmod.get(sect, []):
                                dat = applier_mod.resolve_dat_for_version(g["dat"], game_ver)
                                touched.add(dat.replace("/", os.sep))
                    except Exception:
                        # A mod we can't parse here is missing from `touched`, which skews leftover-dat
                        # restore — record which one rather than silently skewing the set.
                        logging.exception("Couldn't scan %s for touched dats", mod_path)

                # Only restore the LEFTOVERS — dats a PREVIOUS deploy modified that this mod list does
                # NOT touch.  The dats it DOES touch are rebuilt from the clean backup and fully
                # overwritten by the overlay below, so cleaning them first would be redundant work.
                prev = {r.replace("/", os.sep) for r in self._mgr_saved_deployed_dats()}
                restore_targets = sorted(prev - touched)
                _log(self._mgr_log,
                     t("mgr.restoring_n_leftover_file_s", n=len(restore_targets)), "head")
                restored = 0
                for dat_rel in restore_targets:
                    src = bd / dat_rel
                    dest = game_root / dat_rel
                    if src.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        restored += 1
                        _log(self._mgr_log, t("mgr.restored_dat_rel", dat_rel=dat_rel), "step")
                    else:
                        # a previous-deploy dat with no clean backup (e.g. branch changed) — can't revert
                        _log(self._mgr_log, t("mgr.warn_no_clean_backup_restore", dat_rel=dat_rel), "warn")
                _log(self._mgr_log, t("mgr.restored_restored_leftover_file_s", restored=restored), "step")

                _log(self._mgr_log, t("mgr.overlaying_modded_files_onto_game"), "head")
                overlay = 0
                overlaid: set = set()       # the dats now non-clean in the game (persist for next deploy)
                for src in gen_root.rglob("*"):
                    if src.is_file():
                        rel  = src.relative_to(gen_root)
                        dest = game_root / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        _log(self._mgr_log, t("mgr.installing_rel", rel=rel), "step")
                        self._ui(lambda r=rel: self._mgr_foot.set(t("mgr.installing_r", r=r)))
                        shutil.copy2(src, dest)
                        overlay += 1
                        overlaid.add(str(rel).replace(os.sep, "/"))
                _log(self._mgr_log, t("mgr.deployed_overlay_modded_file_s", overlay=overlay), "step")
                # Record exactly what's now overlaid so the next deploy can clean it even if its mod
                # list doesn't touch these dats.
                self._mgr_set_deployed_dats(overlaid)

            if exact:
                summary = t("mgr.done_deployed_n_cached_file",
                            n=sum(1 for f in gen_root.rglob('*') if f.is_file()))
                done_tag = "ok"
            else:
                total_ch = sum(r.changes_applied for r in all_results)
                total_w  = sum(len(r.warnings)   for r in all_results)
                total_e  = sum(len(r.errors)     for r in all_results)
                summary  = (t("mgr.done_total_ch_change_s",
                              total_ch=total_ch, total_w=total_w, total_e=total_e)
                            + (t("mgr.reused_n_mod_s_reused",
                                 reused=reused, n=len(active)) if reused else ""))
                # Completion colour reflects the outcome: green = clean, yellow = finished but some
                # changes warned / couldn't apply (a hard failure aborts earlier, in red).
                done_tag = "ok" if (total_w == 0 and total_e == 0) else "warn"
            _log(self._mgr_log, f"\n{summary}", done_tag)
            self._ui(lambda: self._mgr_foot.set(summary))
        except Exception as ex:
            _log(self._mgr_log, t("mgr.deploy_error_ex", ex=ex), "err")
            self._ui(lambda: self._mgr_foot.set(t("mgr.deploy_failed_see_log")))
        finally:
            self._ui(lambda: self._mgr_set_busy(False))

    def _mgr_set_busy(self, busy: bool):
        self._mgr_running = busy
        state = "disabled" if busy else "normal"
        self._mgr_deploy_btn.configure(state=state)
        if hasattr(self, "_mgr_launch_btn"):
            if busy:
                self._mgr_launch_btn.configure(state="disabled")
            else:
                gr = self._settings.get("game_root", "").strip()
                self._mgr_launch_btn.configure(state="normal" if gr else "disabled")
        if hasattr(self, "_mgr_backup_btn"):
            if busy:
                self._mgr_backup_btn.configure(state="disabled")
            else:
                gr = self._settings.get("game_root", "").strip()
                self._mgr_backup_btn.configure(state="normal" if gr else "disabled")
        if hasattr(self, "_set_backup_btn"):
            if busy:
                self._set_backup_btn.configure(state="disabled")
            else:
                gr = self._settings.get("game_root", "").strip()
                self._set_backup_btn.configure(state="normal" if gr else "disabled")
        if hasattr(self, "_prof_backup_btn"):
            if busy:
                self._prof_backup_btn.configure(state="disabled")
            else:
                gr = self._settings.get("game_root", "").strip()
                self._prof_backup_btn.configure(state="normal" if gr else "disabled")
        if busy:
            restore_state = "disabled"
        else:
            bd = self._backup_dir()
            has_backup = bd.exists() and any(bd.rglob("*.dat"))
            restore_state = "normal" if has_backup else "disabled"
        if hasattr(self, "_mgr_restore_btn"):
            self._mgr_restore_btn.configure(state=restore_state)
        if hasattr(self, "_set_restore_btn"):
            self._set_restore_btn.configure(state=restore_state)
        # Mod Editor hub buttons (present only once a project is open): Restore Clean mirrors the shared
        # restore gate; Deploy is disabled while any game-file op runs so it can't race the restore.
        if hasattr(self, "_ed_restore_btn"):
            self._ed_restore_btn.configure(state=restore_state)
        if hasattr(self, "_ed_deploy_btn"):
            self._ed_deploy_btn.configure(state=("disabled" if busy else "normal"))

    def _mgr_show_backup_warn(self):
        ver_label = " [R.U.S.E. COMPAT]" if self._game_version() == "compat" else ""
        text = t("mgr.backup_progress_ver_label", ver_label=ver_label)
        self._mgr_s2_lbl.configure(text=text, foreground=_COL_WARN)
        if hasattr(self, "_set_s2_lbl"):
            self._set_s2_lbl.configure(text=text, foreground=_COL_WARN)

    # ── Manager state persistence ─────────────────────────────────────────────

    def _load_saved_view(self):
        """The build the user was last VIEWING in the selector, restored only if it's still valid: chosen
        against the SAME installed build and still has mods.  Otherwise None (follow the installed build)."""
        if not _MGR_STATE_FILE.exists():
            return None
        try:
            with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return None
        if not isinstance(st, dict):   # valid JSON but wrong shape (list/str/number) → no saved view
            return None
        sel = st.get("selected_mod_build")
        same_ctx = str(st.get("selected_for_build") or "") == str(self._game_build_id() or "")
        if sel and str(sel).isdigit() and same_ctx and self._build_has_rmods(str(sel)):
            return str(sel)
        return None

    def _load_mgr_state(self):
        # Restore the last-viewed build so the user returns exactly where they left off (validated).
        self._selected_mod_build = self._load_saved_view()
        self._scan_bundled()   # re-scan bundled for the restored view (installed's set was loaded in __init__)
        # Always warm the caches so the toggle works immediately.  Load the EFFECTIVE build's list (the
        # selected build, which may differ from the installed one) so scan folder and state key agree.
        self._mgr_scan_both()
        eff = self._effective_mod_build()
        self._mgr_load_mode(f"v{eff}" if str(eff).isdigit() else self._version_subname())
        self._refresh_mod_build_cb()   # populate the build selector on first load (not only on refresh)
        self._maybe_suggest_mod_build()   # if installed build has no mods, hint at one that does

    def _save_mgr_state(self):
        """Save the CURRENT build's mod list (active set + ORDER) under its build-id key, so every game
        build keeps its OWN independent list.  Cache toggles + the deploy tracker stay global; the mod
        list is per build (`builds: {v<buildid>: [...]}`)."""
        existing = {}
        with _MGR_STATE_LOCK:
            if _MGR_STATE_FILE.exists():
                try:
                    with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass
        if not isinstance(existing, dict):   # wrong-shape JSON on disk → don't let .get() crash the save
            existing = {}
        builds = existing.get("builds")
        if not isinstance(builds, dict):
            builds = {}
        cur = self._mgr_current_ver or self._version_subname()
        # A mod hidden by the current branch / compat-toggle is force-disabled in memory by
        # _mgr_rebuild (so it can't deploy in the wrong mode) — but the user never chose to disable it
        # and can't even click it while hidden.  Persisting that transient False would silently lose
        # their enable choice, so for hidden mods we keep the enabled flag already on disk (this
        # build's prior list, or the legacy unified list pre-migration); the in-memory flag is only
        # authoritative for mods currently visible in the list.
        prior_enabled = {}
        for e in (builds.get(cur) or existing.get("mods") or []):
            pkey = e.get("bundled_id") or e.get("path")
            if pkey:
                prior_enabled[pkey] = bool(e.get("enabled", False))
        visible = set(self._visible_mv_indices())

        # Bundled (SAFE) mods are persisted by stable IDENTITY (not their unstable _MEIPASS path) so
        # the user's enable/disable choice survives restarts; external mods are persisted by path.
        mods_entries = []
        for i, (v, p) in enumerate(self._mgr_mod_vars):
            cache = bool(self._mod_cache_var(p).get())
            key = ("::".join(self._rmod_identity(p)) if self._is_bundled(p)
                   else self._rel_mod_path(p))
            enabled = v.get() if i in visible else prior_enabled.get(key, v.get())
            if self._is_bundled(p):
                mods_entries.append({"bundled_id": key, "enabled": enabled, "cache": cache})
            else:
                mods_entries.append({"path": key, "enabled": enabled, "cache": cache})
        builds[cur] = mods_entries
        existing["builds"] = builds
        existing.pop("mods", None)   # migrated to per-build; drop the legacy unified list
        if hasattr(self, "_mgr_cache_enabled"):
            existing["cache_enabled"] = bool(self._mgr_cache_enabled.get())
        if hasattr(self, "_mgr_per_mod_cache"):
            existing["per_mod_cache"] = bool(self._mgr_per_mod_cache.get())
        # Persist the build selector so the user returns to the build they were VIEWING (with the installed
        # build it was chosen against, so we don't restore it into a different game after a branch switch).
        if self._selected_mod_build:
            existing["selected_mod_build"] = self._selected_mod_build
            existing["selected_for_build"] = self._game_build_id()
        else:
            existing.pop("selected_mod_build", None)
            existing.pop("selected_for_build", None)
        with _MGR_STATE_LOCK:
            # A worker thread (deploy/restore → _mgr_set_deployed_dats) may have rewritten
            # deployed_dats since we read `existing` above.  Re-read it under the lock and merge it in
            # so our stale copy can't clobber that update.
            if _MGR_STATE_FILE.exists():
                try:
                    with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                        latest = json.load(f).get("deployed_dats")
                    if latest is not None:
                        existing["deployed_dats"] = latest
                except Exception:
                    logging.exception("Couldn't re-read deployed_dats under lock; state may be stale")
            try:
                _atomic_write_json(_MGR_STATE_FILE, existing)   # never leave a truncated state file
            except Exception:
                # Silent failure here loses the user's whole mod configuration for this build.
                logging.exception("Failed to write manager state to %s", _MGR_STATE_FILE)

    def _mgr_load_mode(self, ver: str):
        """Clear the mod list, restore saved state, then append any newly found mods. `ver` is the
        BUILD-ID key (v<buildid>) of the current install — tracked so a later build switch is detected."""
        self._mgr_current_ver = ver
        self._mgr_state_loading = True   # suppress the per-rebuild auto-save while we restore
        try:
            self._mgr_load_mode_inner(ver)
        finally:
            self._mgr_state_loading = False

    def _mgr_load_mode_inner(self, ver: str):
        self._mgr_lb.delete(0, tk.END)
        self._mgr_mod_vars.clear()
        self._mgr_cache_flags.clear()
        state = {}
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                pass
        if not isinstance(state, dict):   # wrong-shape JSON (list/str/number) → start from empty state
            state = {}
        # Per-build mod list: `builds[ver]` is THIS build's own list (active set + order).  Migrate a
        # legacy UNIFIED "mods" list losslessly — take this build's slice: bundled entries (they
        # resolve per-build by identity below) + external entries whose path lives under this build's
        # mods/v<buildid>/ folder.  The first save rewrites everything under "builds".
        builds = state.get("builds")
        if isinstance(builds, dict):
            entries = builds.get(ver, [])
        else:
            legacy = state.get("mods") or state.get(ver) or []
            entries = [e for e in legacy
                       if e.get("bundled_id") is not None
                       or str(e.get("path", "")).replace("\\", "/").split("/", 1)[0] == ver]
        # Saved shipped (SAFE) mods are keyed by stable IDENTITY (their _MEIPASS path changes every
        # launch), so map identity → the CURRENT bundled path.  Restoring them HERE, in their saved
        # list position, is what makes a shipped mod hold its place and enabled-state across restarts —
        # exactly like an external mod — instead of always being re-injected at the top below.
        bundled_by_ident = {"::".join(self._rmod_identity(bp)): bp for bp in self._bundled_order}
        for entry in entries:
            bid = entry.get("bundled_id")
            if bid is not None:
                bp = bundled_by_ident.get(bid)
                if not bp:
                    continue   # a mod that used to ship with the app but no longer does — drop it
                var = tk.BooleanVar(value=bool(entry.get("enabled", False)))
                self._mgr_mod_vars.append((var, bp))
                self._mgr_cache_flags[str(bp)] = tk.BooleanVar(value=bool(entry.get("cache", False)))
                continue
            p = self._abs_mod_path(entry.get("path", ""))   # relative-to-mods-folder → absolute
            if not p or not Path(p).exists():
                continue
            # An external file hidden by a shipped mod (same name+major) is not restored.
            if self._is_bundled(p) or self._rmod_identity(p) in self._bundled_keys:
                continue
            # Apply the SAME game_version gate as the folder scan: a saved+enabled mod whose tag no longer
            # matches this build (user retagged it, or it entered the list during a no-build-id window)
            # must not be resurrected into the list and deployed against the wrong build.
            if not self._rmod_matches_build(p):
                continue
            var = tk.BooleanVar(value=bool(entry.get("enabled", False)))
            self._mgr_mod_vars.append((var, p))
            self._mgr_cache_flags[str(p)] = tk.BooleanVar(value=bool(entry.get("cache", False)))
        # Append any newly discovered mods not in saved state.  `ver` is the build-id mode key now,
        # so the compat-vs-remaster split keys off the FORMAT (_game_version()), not `ver`.  Dedupe by
        # _norm_path (case/slash-insensitive) so a saved path in a different form can't double a row.
        existing = {self._norm_path(p) for _, p in self._mgr_mod_vars}
        scan_files = (self._scanned_compat if self._game_version() == "compat"
                      else list(self._scanned_public) + list(self._scanned_compat))
        for f in scan_files:
            path = str(f)
            if self._norm_path(path) not in existing:
                self._mgr_mod_vars.append((tk.BooleanVar(value=False), path))
                existing.add(self._norm_path(path))
        self._ensure_bundled_in_list()   # shipped (SAFE) mods always PRESENT on top; default disabled,
                                         # user-enabled (enable/cache state remembered by identity)
        self._mgr_rebuild()

    # =========================================================================
    # CONVERT TAB
    # =========================================================================

    def _build_convert_tab(self, p):
        pad  = {"padx": 6, "pady": 3}
        padg = {"padx": 6, "pady": 3}

        # ── Section ① — build an .rmod FROM a mod folder ──────────────────────
        # Big visual break: users kept reading the mod-folder→rmod tool as part of the rmod→all-versions
        # tool below.  A bold numbered banner + the ② separator later make them two distinct tools.
        ttk.Label(p, text=t("mgr.make_rmod_from_mod_folder"),
                  foreground=_R_GOLD_BRT, font=_F_HEAD).pack(anchor="w", padx=8, pady=(8, 0))
        ttk.Label(p, text=t("mgr.turn_folder_modded_game_files"),
                  foreground=_R_TEXT_DIM).pack(anchor="w", padx=8, pady=(0, 4))

        # ── Directories ───────────────────────────────────────────────────────
        df = ttk.LabelFrame(p, text=t("mgr.directories"))
        df.pack(fill="x", **pad)
        df.columnconfigure(1, weight=1)

        ttk.Label(df, text=t("mgr.mod_folder")).grid(row=0, column=0, sticky="e", **padg)
        self._cv_mod = tk.StringVar()
        ttk.Entry(df, textvariable=self._cv_mod).grid(
            row=0, column=1, sticky="ew", **padg)
        ttk.Button(df, text=t("mgr.browse"),
                   command=self._cv_browse_mod).grid(row=0, column=2, **padg)
        # Dynamic mode line: the mod's branch is auto-detected from its version folder (99/1360 =
        # COMPAT → .compat.rmod; 190852 = public → .rmod), found anywhere in the game-root-relative
        # layout (Data\PC\<ver>\… core dats, Maps\PC\… terrain dats — older flat PC\ layouts too).  It
        # states the type AND where it'll be saved (mods/compat or mods/public) — output is auto-routed.
        self._cv_mode = tk.StringVar()
        ttk.Label(df, textvariable=self._cv_mode,
                  foreground=_R_TEXT_DIM, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        # Which game VERSION (build) is this mod for? Picks the clean backup to diff against
        # and where the rmod lands. Defaults to the detected installed build.
        ttk.Label(df, text=t("mgr.make_mod_version")).grid(row=2, column=0, sticky="e", **padg)
        self._cv_build = tk.StringVar()
        self._cv_build_cb = ttk.Combobox(df, textvariable=self._cv_build, state="readonly", width=26)
        self._cv_build_cb.grid(row=2, column=1, sticky="w", **padg)
        # Make mod for version: EXACTLY the versions the user has a clean backup for — those backups
        # are the pristine dats the convert diffs the mod against.  Same box behaviour as the Mod
        # Editor's Game Version picker (both go through ui_util.populate_version_combo).
        self._cv_refresh_target_versions()

        # ── Main body: vertical paned ─────────────────────────────────────────
        vpw = ttk.PanedWindow(p, orient=tk.VERTICAL)
        vpw.pack(fill="both", expand=True, padx=6, pady=3)

        # Top pane — horizontal: mod info+changes (left) | description (right)
        hpw = ttk.PanedWindow(vpw, orient=tk.HORIZONTAL)
        vpw.add(hpw, weight=2)

        # ── Left: Mod Info form + Detected Changes listbox ────────────────────
        left = ttk.LabelFrame(hpw, text=t("mgr.mod_info"))
        hpw.add(left, weight=1)
        left.columnconfigure(1, weight=1)
        left.rowconfigure(4, weight=1)

        # Name + the auto-appended "_V#" file-name suffix (driven by the major version below).
        ttk.Label(left, text=t("common.name")).grid(row=0, column=0, sticky="e", **padg)
        self._cv_name = tk.StringVar()
        self._cv_name.trace_add("write", lambda *_: self._cv_update_preview())
        ne = ttk.Entry(left, textvariable=self._cv_name)
        ne.grid(row=0, column=1, columnspan=2, sticky="ew", **padg)
        self._cv_name_suffix = tk.StringVar()
        ttk.Label(left, textvariable=self._cv_name_suffix, foreground=_R_GOLD,
                  font=_F_BOLD).grid(row=0, column=3, sticky="w", padx=(0, 8))

        # No ID field: the id is derived from the Name — exactly like converting an open Mod Editor
        # project — so both convert paths behave identically.  _start_rmod_convert appends the -v# suffix.

        # Version — its MAJOR number becomes the _V# suffix on the rmod file name (and the -v# id suffix).
        ttk.Label(left, text=t("mgr.version")).grid(row=1, column=0, sticky="e", **padg)
        self._cv_ver = tk.StringVar(value="1.0.0")
        self._cv_ver.trace_add("write", lambda *_: self._cv_refresh_suffix())
        cv_ver_ent = ttk.Entry(left, textvariable=self._cv_ver, width=8)
        cv_ver_ent.grid(row=1, column=1, sticky="w", **padg)
        # On leaving the field, snap a non-conforming entry (blank, "v1.0", free text) back to x.x.x.
        cv_ver_ent.bind("<FocusOut>",
                        lambda *_: self._cv_ver.set(_normalize_version(self._cv_ver.get())))
        ttk.Label(left, text=t("mgr.major_auto_added_as_v"),
                  foreground=_R_TEXT_DIM).grid(row=1, column=2, columnspan=2, sticky="w", padx=4)

        ttk.Label(left, text=t("mgr.author")).grid(row=2, column=0, sticky="e", **padg)
        self._cv_author = tk.StringVar()
        ttk.Entry(left, textvariable=self._cv_author).grid(
            row=2, column=1, columnspan=3, sticky="ew", **padg)

        self._cv_preview = tk.StringVar()
        ttk.Label(left, textvariable=self._cv_preview,
                  foreground=_R_TEXT_DIM).grid(
            row=3, column=1, columnspan=3, sticky="w", padx=6, pady=(0, 2))

        det = ttk.LabelFrame(left, text=t("mgr.detected_dat_files"))
        det.grid(row=4, column=0, columnspan=4, sticky="nsew", padx=6, pady=(2, 6))
        det.columnconfigure(0, weight=1)
        det.rowconfigure(1, weight=1)

        sb2 = ttk.Frame(det)
        sb2.pack(fill="x", pady=(4, 0), padx=4)
        ttk.Button(sb2, text=t("mgr.scan_changes"),
                   command=self._cv_scan).pack(side="left", padx=2)
        self._cv_btn = ttk.Button(sb2, text=t("mgr.convert"), command=self._cv_convert)
        self._cv_btn.pack(side="left", padx=2)
        self._cv_scan_status = tk.StringVar()
        ttk.Label(sb2, textvariable=self._cv_scan_status,
                  foreground=_R_TEXT_DIM).pack(side="left", padx=8)

        lf = ttk.Frame(det)
        lf.pack(fill="both", expand=True)
        self._cv_lb = tk.Listbox(lf, selectmode="browse",
                                 activestyle="none",
                                 background=_R_BG_WIDGET, foreground=_R_TEXT,
                                 selectbackground=_R_SEL_BG, selectforeground=_R_SEL_FG,
                                 font=_F_MAIN, relief="flat",
                                 highlightthickness=1, highlightcolor=_R_BORDER,
                                 highlightbackground=_R_BORDER)
        cvs = ttk.Scrollbar(lf, orient="vertical", command=self._cv_lb.yview)
        self._cv_lb.configure(yscrollcommand=cvs.set)
        cvs.pack(side="right", fill="y")
        self._cv_lb.pack(side="left", fill="both", expand=True)

        # ── Right: Description ────────────────────────────────────────────────
        right = ttk.LabelFrame(hpw, text=t("mgr.description"))
        hpw.add(right, weight=1)
        self._cv_desc = _ThemedScrolledText(
            right, font=_F_MAIN, wrap="word", relief="flat",
            background=_R_BG_WIDGET, foreground=_R_TEXT,
            insertbackground=_R_GOLD)
        self._cv_desc.pack(fill="both", expand=True, padx=6, pady=6)

        # ── Bottom pane: compat→public + log ─────────────────────────────────
        bot = ttk.Frame(vpw)
        vpw.add(bot, weight=2)

        # The Convert button now lives in the "Detected .dat Files" toolbar (next to Scan for Changes);
        # keep the mode label + its text in sync with the selected mod folder's branch.
        self._cv_mod.trace_add("write", lambda *_: self._cv_refresh_mode())
        self._cv_refresh_mode()
        self.after(160, self._cv_refresh_labels)

        # ── Section ② — convert an EXISTING .rmod to every game version ───────
        ttk.Separator(bot, orient="horizontal").pack(fill="x", padx=6, pady=(10, 2))
        ttk.Label(bot, text=t("mgr.convert_existing_rmod_every_game"),
                  foreground=_R_GOLD_BRT, font=_F_HEAD).pack(anchor="w", padx=8, pady=(0, 4))
        # Title now lives in the banner above; keep the border to group the fields.
        tf = ttk.LabelFrame(bot, text="")
        tf.pack(fill="x", **pad)
        tf.columnconfigure(1, weight=1)

        ttk.Label(tf, text=t("mgr.source_version")).grid(row=0, column=0, sticky="e", **padg)
        self._mig_ver = tk.StringVar()
        self._mig_ver_cb = ttk.Combobox(tf, textvariable=self._mig_ver, state="readonly", width=26)
        self._mig_ver_cb.grid(row=0, column=1, sticky="w", **padg)
        self._mig_ver_cb.bind("<<ComboboxSelected>>", lambda *_: self._mig_refresh_rmods())

        ttk.Label(tf, text=t("mgr.rmod")).grid(row=1, column=0, sticky="e", **padg)
        self._mig_src = tk.StringVar()
        self._mig_rmod_cb = ttk.Combobox(tf, textvariable=self._mig_src, state="readonly")
        self._mig_rmod_cb.grid(row=1, column=1, sticky="ew", **padg)
        ttk.Button(tf, text=t("mgr.browse"), command=self._mig_browse).grid(row=1, column=2, **padg)
        ttk.Label(tf, text=t("mgr.converts_selected_rmod_every_other"),
                  foreground=_R_TEXT_DIM, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))
        af2 = ttk.Frame(tf)
        af2.grid(row=3, column=0, columnspan=3, sticky="ew", **padg)
        self._tr_btn = ttk.Button(af2, text=t("mgr.convert_all_versions"),
                                  command=self._mig_convert_all)
        self._tr_btn.pack(side="right", padx=4)
        self._tr_status = tk.StringVar()
        ttk.Label(af2, textvariable=self._tr_status,
                  foreground=_R_TEXT_DIM).pack(side="left", padx=4)
        # Populate with every build that has a mod library (default = installed) + list its rmods.
        self._mig_refresh_versions()

        # Pin the footer to the bottom of this pane FIRST (side=bottom, packed before the
        # log) so Tk clips the log — not the status line — when the pane gets short.
        self._cv_foot = tk.StringVar(value=t("mgr.ready"))
        ttk.Label(bot, textvariable=self._cv_foot, anchor="w").pack(
            side=tk.BOTTOM, fill="x", padx=6, pady=(0, 4))

        lf2 = ttk.LabelFrame(bot, text=t("mgr.log"))
        lf2.pack(side=tk.TOP, fill="both", expand=True, **pad)
        self._cv_log = _make_log(lf2)
        self._cv_log.pack(fill="both", expand=True, padx=4, pady=4)

        self._cv_refresh_suffix()   # seed the _V# / -v# hints from the default version

    def _reset_convert_tab(self):
        self._cv_mod.set("")
        self._cv_name.set("")
        self._cv_ver.set("1.0.0")
        self._cv_author.set("")
        self._cv_desc.delete("1.0", tk.END)
        self._cv_preview.set("")
        self._cv_lb.delete(0, tk.END)
        self._cv_scan_status.set("")
        self._mig_src.set("")
        self._tr_status.set("")
        # Refresh both version pickers so backups / mod-library builds created since this tab was last
        # shown appear: 'Make mod for version' (backed-up versions) and Section ② 'Source version'
        # (every build with a mod library).
        self._cv_refresh_target_versions()
        self._mig_refresh_versions()
        _log_clear(self._cv_log)
        self._cv_foot.set(t("mgr.ready"))


    def _cv_browse_mod(self):
        d = filedialog.askdirectory(
            parent=self,
            title=t("mgr.select_mod_root_mirrors_game_2"))
        if not d:
            return
        self._cv_mod.set(d)
        name = Path(d).name
        if not self._cv_name.get():
            self._cv_name.set(name)
        # If the folder carries a well-formed description.txt, prefill author / version / description
        # from it; otherwise (missing / corrupt / not in format) just keep the folder name above.
        meta = mp_project_mod.read_folder_description(d)
        if meta:
            if not self._cv_author.get().strip():
                self._cv_author.set(meta["author"])
            if meta["version"] and self._cv_ver.get().strip() in ("", "1.0.0"):
                self._cv_ver.set(_normalize_version(meta["version"]))
            if meta["description"] and not self._cv_desc.get("1.0", tk.END).strip():
                self._cv_desc.delete("1.0", tk.END)
                self._cv_desc.insert("1.0", meta["description"])

    def _cv_refresh_mode(self):
        """Reflect the SELECTED MOD FOLDER's branch (auto-detected from its 99/1360/190852 sub-folder)
        in the Convert button + the mode label, so the user sees exactly what they're about to make.
        This is MOD-detected, not game-detected — convert works for either branch regardless of which
        game is installed (it diffs against the matching bundled originals)."""
        mod_folder = self._cv_mod.get().strip()
        ver = _detect_mod_folder_version(mod_folder) if mod_folder else None
        btn = getattr(self, "_cv_btn", None)
        if ver == "compat":
            if btn: btn.configure(text=t("mgr.convert_compat_rmod"))
            self._cv_mode.set(t("mgr.detected_r_u_s_e_2", dest=self._cv_dest_dir(mod_folder)))
        elif ver == "public":
            if btn: btn.configure(text=t("mgr.convert_rmod_3"))
            self._cv_mode.set(t("mgr.detected_r_u_s_e", dest=self._cv_dest_dir(mod_folder)))
        elif mod_folder:
            if btn: btn.configure(text=t("mgr.convert"))
            self._cv_mode.set(t("mgr.no_99_1360_190852_version"))
        else:
            if btn: btn.configure(text=t("mgr.convert"))
            self._cv_mode.set(t("mgr.select_mod_root_mirrors_game"))
        self._cv_update_preview()

    def _cv_major(self) -> str:
        """The MAJOR version number from the Version field, used for the _V# / -v# suffixes.
        Tolerates 'v2', '2.1.0', '  3 ' etc.; falls back to '1' when nothing usable is typed."""
        return _major_of(self._cv_ver.get())

    def _cv_refresh_suffix(self):
        """Keep the gold _V# (file-name) hint next to the Name box in sync with the major version, and
        re-render the file-name preview.  The suffix is appended to the rmod FILE NAME (and the id gets a
        matching -v# inside _start_rmod_convert) — the rmod's own `name` field stays exactly as typed."""
        mj = self._cv_major()
        if hasattr(self, "_cv_name_suffix"):
            self._cv_name_suffix.set(f"_V{mj}")
        self._cv_update_preview()

    def _cv_update_preview(self):
        name = self._cv_name.get().strip()
        if name:
            mod_folder = self._cv_mod.get().strip()
            mod_ver = _detect_mod_folder_version(mod_folder) if mod_folder else None
            ext = ".compat.rmod" if (mod_ver or self._game_version()) == "compat" else ".rmod"
            base = re.sub(r"_V\d+$", "", _sanitize_filename(name))
            self._cv_preview.set(f"→ {base}_V{self._cv_major()}{ext}")
        else:
            self._cv_preview.set("")

    def _cv_scan(self):
        mod_folder = self._cv_mod.get().strip()
        if not mod_folder:
            ui_util.error(self, t("mgr.missing"), t("mgr.set_mod_folder_first")); return
        self._cv_lb.delete(0, tk.END)
        self._cv_scan_status.set(t("mgr.scanning"))
        game_data = str(self._cv_game_data(mod_folder))
        # Run the folder walk + stat() off the UI thread — a large or slow (network) mod folder would
        # otherwise freeze the whole window — and never crash if a file vanishes mid-scan.
        def _work():
            rows, err = [], None
            try:
                for mod_dat, _, rmod_rel in scan_mod_folder(mod_folder, game_data):
                    try:
                        kb = mod_dat.stat().st_size // 1024
                    except OSError:
                        kb = 0
                    rows.append((rmod_rel, kb))
            except Exception as e:               # noqa: BLE001 — report any scan failure, don't crash
                err = e
            self._ui(lambda: self._cv_scan_done(rows, err))
        threading.Thread(target=_work, daemon=True).start()

    def _cv_scan_done(self, rows, err):
        if err is not None:
            self._cv_scan_status.set(t("mgr.scan_failed_see_error"))
            ui_util.error(self, t("mgr.scan_failed"), str(err))
            return
        if not rows:
            self._cv_scan_status.set(t("mgr.no_matching_dat_files_found"))
            return
        for rmod_rel, kb in rows:
            self._cv_lb.insert(tk.END, t("mgr.rmod_rel_kb_kb", rmod_rel=rmod_rel, kb=kb))
        self._cv_scan_status.set(t("mgr.n_dat_file_s_found", n=len(rows)))

    def _start_rmod_convert(self, *, mod_folder, name, mod_id, version, author, description,
                            log, on_done, target_build=""):
        """The one rmod-conversion path, shared by the Convert tab AND the Mod Editor so both behave
        IDENTICALLY: standardized versioned naming (a "_V#" suffix on the rmod FILE NAME and a "-v#"
        suffix on the ID — the `name` field stays clean; existing suffixes are stripped so re-converting
        doesn't stack), branch detected from the mod folder (→ .compat.rmod / .rmod and routed to
        mods/compat or mods/public), diffed against the matching clean originals (_cv_game_data), and run
        on a worker thread.  Streams into the `log` widget; `on_done(ok, out_rmod, err)` runs on the UI
        thread.  Returns the output path once started, or None (after an error dialog) if it can't start
        (missing name, or no reference originals to diff against)."""
        if not name:
            ui_util.error(self, t("mgr.missing"), t("mgr.enter_mod_name"))
            return None
        if not mod_id:
            mod_id = _name_to_id(name)
        major  = _major_of(version)
        mod_id = re.sub(r"-v\d+$", "", mod_id) + f"-v{major}"
        # target_build (Convert tab picker) decides which version's clean backup to diff against
        # and where the rmod lands; without it, fall back to detecting the mod folder's branch.
        if target_build:
            mod_ver = "compat" if (_gv_mod.dataver_for_build(target_build) == "99") else "public"
        else:
            mod_ver = _detect_mod_folder_version(mod_folder) or self._game_version()
        ext = ".compat.rmod" if mod_ver == "compat" else ".rmod"
        game_data = Path(self._cv_game_data(mod_folder, build=target_build))
        if not _exists_safe(game_data / "PC"):
            where = _gv_mod.display_name(target_build) if target_build else (
                'R.U.S.E. COMPAT' if mod_ver == 'compat' else 'R.U.S.E.')
            ui_util.error(
                self,
                t("mgr.missing_backup_reference_files"),
                t("mgr.couldn_t_find_clean_game",
                  game=where, game_data=game_data))
            return None
        dest_dir = self._cv_dest_dir(mod_folder, build=target_build)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Stamp the rmod with the SAME build id that decides its destination folder
        # (_cv_dest_dir uses `target_build or _game_build_id()`), so the manager — which
        # only lists a mod when its game_version matches the installed build — always
        # recognises what we just wrote instead of nagging to "update the version".
        stamp_build = target_build or self._game_build_id()
        file_base = re.sub(r"_V\d+$", "", _sanitize_filename(name))
        out_rmod  = str(dest_dir / f"{file_base}_V{major}{ext}")
        version   = _normalize_version(version)
        game_data = str(game_data)

        def _work():
            err, ok = None, False
            def lf(m): self._ui(lambda m=m: _log(log, m, "info"))
            def wf(m): self._ui(lambda m=m: _log(log, t("mgr.warn") + m, "warn"))
            try:
                ok = run_conversion(
                    mod_folder=str(mod_folder), game_data_dir=game_data, output_rmod=out_rmod,
                    name=name, mod_id=mod_id, version=version,
                    author=author, description=description, log_fn=lf, warn_fn=wf,
                    game_version=stamp_build)
            except Exception as e:
                err = str(e)
            self._ui(lambda: on_done(ok, out_rmod, err))

        self._ui(lambda: _log(log, t("mgr.converting_name", name=name), "head"))
        threading.Thread(target=_work, daemon=True).start()
        return out_rmod

    def _cv_convert(self):
        if self._conv_running:
            return
        # Don't start a conversion while a deploy/backup/restore is mid-write: converting diffs against the
        # build's clean backup, which a backup job may be rewriting.  Mirrors the Mod Manager tab's
        # update-rmod / share guards (and the deploy→conversion guard the other way).
        if getattr(self, "_mgr_running", False):
            ui_util.info(self, t("mgr.please_wait"), t("mgr.mgr_op_running_let_finish"))
            return
        mod_folder  = self._cv_mod.get().strip()
        name        = self._cv_name.get().strip()
        version     = _normalize_version(self._cv_ver.get())
        self._cv_ver.set(version)   # reflect the normalized value back into the field
        author      = self._cv_author.get().strip()
        description = self._cv_desc.get("1.0", tk.END).strip()
        if not mod_folder:
            ui_util.error(self, t("mgr.missing"), t("mgr.set_mod_folder")); return
        if not name:
            ui_util.error(self, t("mgr.missing"), t("mgr.enter_mod_name")); return
        # Id is derived from the name (no ID field) — identical to converting an open Mod Editor project.
        mod_id = _name_to_id(name)

        def _done(ok, out_rmod, err):
            self._conv_running = False
            self._cv_btn.configure(state="normal")
            if err:
                _log(self._cv_log, t("mgr.error_err", err=err), "err")
                self._cv_foot.set(t("mgr.error_see_log"))
            elif ok:
                _log(self._cv_log, t("mgr.done_out_rmod", out_rmod=out_rmod), "ok")
                self._cv_foot.set(t("mgr.written_name", name=Path(out_rmod).name))
            else:
                _log(self._cv_log, t("mgr.conversion_failed_see_warnings"), "err")
                self._cv_foot.set(t("mgr.conversion_failed"))

        self._conv_running = True
        self._cv_btn.configure(state="disabled")
        self._cv_foot.set(t("mgr.converting"))
        try:
            started = self._start_rmod_convert(mod_folder=mod_folder, name=name, mod_id=mod_id,
                                               version=version, author=author, description=description,
                                               log=self._cv_log, on_done=_done,
                                               target_build=self._cv_target_build())
        except Exception:
            # A SYNCHRONOUS failure before the worker starts (e.g. the output dir can't be created) must
            # not leave the tool stuck — restore the button/flag, then re-raise for the global error dialog.
            self._conv_running = False
            self._cv_btn.configure(state="normal")
            self._cv_foot.set(t("mgr.error_see_log"))
            raise
        if started is None:
            self._conv_running = False
            self._cv_btn.configure(state="normal")
            self._cv_foot.set(t("mgr.ready"))

    # ── build/version pickers + migrate-to-all-versions (Convert tab) ───────────
    def _cv_refresh_target_versions(self):
        """Populate the 'Make mod for version' picker with EXACTLY the backed-up versions (the clean
        dats a convert diffs the mod against), defaulting to the installed build.  Same box behaviour
        as the Mod Editor's Game Version picker — both go through ui_util.populate_version_combo."""
        if not hasattr(self, "_cv_build_cb"):
            return
        self._cv_build_map_backup = ui_util.populate_version_combo(
            self._cv_build_cb, self._cv_build,
            self._backed_up_versions(), default_key=self._version_key())

    def _mig_refresh_versions(self):
        """Populate the Section ② 'Source version' picker with every build that has a mod library — the
        same set as the Mod Manager tab's build selector (_available_mod_builds) — but branch-labeled via
        display_name ('branch (vBUILD)', or 'vBUILD' when a build has no branch), since there's room here.
        Defaults to the installed build, then lists that version's rmods."""
        if not hasattr(self, "_mig_ver_cb"):
            return
        vers = [(b, _gv_mod.display_name(b), None) for b in self._available_mod_builds()]
        self._mig_ver_map = ui_util.populate_version_combo(
            self._mig_ver_cb, self._mig_ver, vers, default_key=self._game_build_id())
        self._mig_refresh_rmods()

    def _cv_target_build(self) -> str:
        """Build id chosen in 'Make mod for version' (else the detected install).  Resolved against
        the backed-up-versions map the picker was populated from."""
        m = getattr(self, "_cv_build_map_backup", None) or {}
        return m.get(self._cv_build.get(), self._game_build_id())

    def _mig_src_build(self) -> str:
        """Build id chosen in the Section ② 'Source version' picker (else '')."""
        m = getattr(self, "_mig_ver_map", None) or {}
        return m.get(self._mig_ver.get(), "")

    def _mig_refresh_rmods(self):
        """List the rmods in the selected source version's folder (mods/v<build>/)."""
        if not hasattr(self, "_mig_rmod_cb"):
            return
        build = self._mig_src_build()
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        d = (base / f"v{build}") if build else base
        names = sorted(p.name for p in d.glob("*.rmod")) if d.is_dir() else []
        self._mig_rmod_cb["values"] = names
        if names and self._mig_src.get() not in names:
            self._mig_src.set(names[0])
        elif not names:
            self._mig_src.set("")

    def _mig_browse(self):
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        p = filedialog.askopenfilename(
            parent=self,
            title=t("mgr.select_rmod_convert_all_versions"), initialdir=str(base),
            filetypes=[(t("mgr.mod_files"), "*.rmod"), (t("common.all_files"), "*.*")])
        if p:
            self._mig_src.set(p)

    def _mig_resolve_rmod_path(self):
        """Absolute path of the selected source rmod (a dropdown name in the version folder,
        or a browsed absolute path)."""
        sel = self._mig_src.get().strip()
        if not sel:
            return None
        p = Path(sel)
        if p.is_file():
            return p
        build = self._mig_src_build()
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        cand = (base / f"v{build}" / sel) if build else (base / sel)
        return cand if cand.is_file() else None

    def _mig_convert_all(self):
        """Convert the selected rmod to EVERY other mapped game version (forward + backward),
        writing each into its mods/v<build>/ folder. Uses only shipped maps (no snapshots)."""
        if self._conv_running:
            return
        if getattr(self, "_mgr_running", False):   # see _cv_convert — don't write mods while a deploy/backup/restore runs
            ui_util.info(self, t("mgr.please_wait"), t("mgr.mgr_op_running_let_finish"))
            return
        src_path  = self._mig_resolve_rmod_path()
        src_build = self._mig_src_build()
        if not src_path or not src_build:
            ui_util.error(self, t("mgr.missing"), t("mgr.pick_source_version_rmod_first"))
            return
        targets = [e for e in _migrate_mod.registry_timeline(include_og=True)
                   if e["buildid"] != src_build]
        # The OG<->remaster path translation only goes OG->remaster (forward). A remaster
        # source therefore can't convert backward to the OG (data-version 99) build; drop it.
        src_dv = _gv_mod.dataver_for_build(src_build) or "190852"
        if src_dv != "99":
            targets = [e for e in targets
                       if (_gv_mod.dataver_for_build(e["buildid"]) or "190852") != "99"]
        if not targets:
            ui_util.info(self, t("mgr.nothing_do"), t("mgr.no_other_mapped_versions_convert"))
            return
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        self._conv_running = True
        self._tr_btn.configure(state="disabled")
        self._tr_status.set(t("mgr.converting"))
        _log(self._cv_log, t("mgr.migrating_name_n_version_s",
                             name=src_path.name, n=len(targets)), "head")

        def _work():
            ok = skipped = 0
            # Validate the source rmod ONCE up front: if it can't be read (corrupt/truncated/foreign file),
            # fail fast with a single clear message instead of the same parse error repeated per target.
            try:
                _mod_format.load(str(src_path))
            except Exception as ex:
                def _fail(ex=ex):
                    self._conv_running = False
                    self._tr_btn.configure(state="normal")
                    self._tr_status.set(t("mgr.error_see_log"))
                    _log(self._cv_log, t("mgr.error_err", err=str(ex)), "err")
                self._ui(_fail)
                return
            for e in targets:
                label = _gv_mod.display_name(e["buildid"])
                try:
                    mod = _mod_format.load(str(src_path))   # fresh mutable copy per target (convert_rmod mutates)
                    rep = _migrate_mod.convert_rmod(mod, src_build, e["buildid"], None)  # shipped maps
                    if rep.get("error"):
                        self._ui(lambda l=label, r=rep: _log(
                            self._cv_log, t("mgr.l_skipped_err", l=l, err=r["error"]), "warn"))
                        skipped += 1
                        continue
                    out_dir = base / f"v{e['buildid']}"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    _mod_format.save(mod, str(out_dir / src_path.name))
                    self._ui(lambda l=label, r=rep: _log(
                        self._cv_log, t("mgr.l_ok_reindexed_ri_dropped",
                                        l=l, ri=r["reindexed"], d=r["dropped"]),
                        "ok" if not r["dropped"] else "warn"))
                    ok += 1
                except Exception as ex:
                    self._ui(lambda l=label, ex=ex: _log(
                        self._cv_log, t("mgr.l_error_err", l=l, err=str(ex)), "err"))
                    skipped += 1

            def _fin():
                self._conv_running = False
                self._tr_btn.configure(state="normal")
                self._tr_status.set(t("mgr.done_ok_converted_sk_skipped", ok=ok, sk=skipped))
                _log(self._cv_log, t("mgr.migration_complete_ok_converted_sk",
                                     ok=ok, sk=skipped), "head")
                # Nothing converted → almost always no version maps exist FROM this source build (e.g. a
                # build the app has no mappings for).  Surface it in a popup, not just the log.
                if ok == 0:
                    ui_util.warning(self, t("mgr.no_version_maps_title"),
                                    t("mgr.no_version_maps_body",
                                      src=_gv_mod.display_name(src_build)))
            self._ui(_fin)
        threading.Thread(target=_work, daemon=True).start()

    # =========================================================================
    # CREATE TAB
    # =========================================================================

    def _build_mod_editor_tab(self, p):
        """Project-based Mod Editor: a project-selection screen / project hub, and the modding windows
        (Units, Map, AI, Economy, Raw) shown as NESTED in-tab views that take over the main window — one
        at a time — with a shared Back button.  The hub keeps its own Close Project button."""
        self._ed_outer = ttk.Frame(p)
        self._ed_outer.pack(fill="both", expand=True)
        # Top nav bar — shown ONLY while inside a nested editor view: Back + the view's title.
        self._ed_navbar = ttk.Frame(self._ed_outer)
        ttk.Button(self._ed_navbar, text=t("mgr.back"), command=self._ed_back).pack(side="left", padx=6, pady=4)
        self._ed_nav_title = ttk.Label(self._ed_navbar, text="", font=_F_HEAD, foreground=_R_GOLD_BRT)
        self._ed_nav_title.pack(side="left", padx=8)
        # Content area — shows the selection screen, the hub, or the current nested editor view.
        self._ed_content = ttk.Frame(self._ed_outer)
        self._ed_content.pack(fill="both", expand=True)
        self._ed_view_stack = []        # [(frame, title, cleanup_or_None)] — the nested editor views
        self._ed_proj_paths = []
        self._build_ed_select(self._ed_content)
        self._build_ed_hub(self._ed_content)
        self._ed_show_select()

        # Python-logging output (incl. Pillow's DEBUG) → the Mod Editor "all logs" mirrors only; the
        # per-tab logs stay scoped to their own messages.  Tag PIL lines with the editor that caused
        # them (Raw vs Map) so the source is obvious — see pil_log.py.
        handler = _TextHandler(self)
        handler.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.DEBUG)
        import pil_log
        pil_log.install(handler)

    # ── Nested editor views (one window at a time, Back to step out) ─────────────
    def _ed_open_view(self, frame, title, cleanup=None):
        """Show an editor as a nested in-tab view taking over the main window: hide whatever's currently
        shown (the hub, or the view beneath this one) and reveal `frame` with the Back bar."""
        if self._ed_view_stack:
            self._ed_view_stack[-1][0].pack_forget()
        else:
            self._ed_hub.pack_forget()
            self._ed_navbar.pack(side="top", fill="x", before=self._ed_content)
        self._ed_view_stack.append((frame, title, cleanup))
        frame.pack(fill="both", expand=True)
        self._ed_nav_title.configure(text=title)

    def _ed_back(self):
        """Pop the top nested view (running its cleanup) and reveal the one beneath it, or the hub."""
        if not self._ed_view_stack:
            return
        frame, _title, cleanup = self._ed_view_stack.pop()
        frame.pack_forget()
        if cleanup:
            try:
                cleanup()
            except Exception:
                pass
        frame.destroy()
        if self._ed_view_stack:
            top = self._ed_view_stack[-1]
            top[0].pack(fill="both", expand=True)
            self._ed_nav_title.configure(text=top[1])
        else:
            self._ed_navbar.pack_forget()
            self._ed_hub.pack(fill="both", expand=True)
        self._ed_update_status()

    def _ed_clear_views(self):
        """Tear down any open nested views (used when leaving for the hub/selection screen)."""
        while getattr(self, "_ed_view_stack", None):
            frame, _t, cleanup = self._ed_view_stack.pop()
            frame.pack_forget()
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass
            frame.destroy()
        if hasattr(self, "_ed_navbar"):
            self._ed_navbar.pack_forget()

    def _editor_mods_dir(self) -> Path:
        """Default location for new mod folders: output/editor_mods next to the app."""
        d = _LAUNCH_DIR / "output" / "editor_mods"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    # ── Project-selection screen ──────────────────────────────────────────────

    def _build_ed_select(self, parent):
        self._ed_select = ttk.Frame(parent)

        ttk.Label(self._ed_select, text=t("mgr.mod_editor"), font=_F_HEAD,
                  foreground=_R_GOLD_BRT).pack(anchor="w", padx=10, pady=(10, 0))
        ttk.Label(self._ed_select,
                  text=t("mgr.create_new_mod_project_load"),
                  foreground=_R_TEXT_DIM, wraplength=820, justify="left"
                  ).pack(anchor="w", padx=10, pady=(0, 8))

        # Body: project picker (left half) | the Mod Editor "all logs" window (right half).
        body = ttk.PanedWindow(self._ed_select, orient=tk.HORIZONTAL)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        left = ttk.Frame(body)
        body.add(left, weight=1)

        nf = ttk.LabelFrame(left, text=t("mgr.create_new_mod"))
        nf.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Label(nf, text=t("mgr.mod_name_2")).grid(row=0, column=0, sticky="e", padx=6, pady=6)
        self._ed_new_name = tk.StringVar()
        ent = ttk.Entry(nf, textvariable=self._ed_new_name, width=40)
        ent.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        ent.bind("<Return>", lambda *_: self._ed_create_project())
        ttk.Button(nf, text=t("mgr.create_project"), command=self._ed_create_project
                   ).grid(row=0, column=2, padx=6, pady=6)
        # Target game version: defaults to the auto-detected installed build, but you can pick any
        # version you have a clean backup for (so you can mod compat-2 while your game is on public).
        ttk.Label(nf, text=t("mgr.game_version")).grid(row=1, column=0, sticky="e", padx=6, pady=(0, 6))
        self._ed_target_ver = tk.StringVar()
        self._ed_target_map = {}
        self._ed_target_cb = ttk.Combobox(nf, textvariable=self._ed_target_ver, state="readonly", width=38)
        self._ed_target_cb.grid(row=1, column=1, sticky="w", padx=6, pady=(0, 6))
        self._ed_refresh_target_versions()
        nf.columnconfigure(1, weight=1)

        lf = ttk.LabelFrame(left, text=t("mgr.load_existing_mod"))
        lf.pack(fill="both", expand=True, padx=4, pady=6)
        # Action buttons sit at the top-left, beside the "Load Existing Mod" title.
        tb = ttk.Frame(lf)
        tb.pack(fill="x", padx=6, pady=(2, 0))
        btns = ttk.Frame(tb)
        btns.pack(side="left")
        ttk.Button(btns, text=t("mgr.load_selected"), command=self._ed_load_selected).pack(side="left", padx=2)
        ttk.Button(btns, text=t("mgr.refresh"), command=self._ed_refresh_project_list).pack(side="left", padx=2)
        ttk.Button(btns, text=t("mgr.browse_folder"), command=self._ed_browse_project).pack(side="left", padx=2)

        lwrap = tk.Frame(lf, background=_R_BG_PANEL)
        lwrap.pack(fill="both", expand=True, padx=6, pady=6)
        self._ed_proj_lb = tk.Listbox(lwrap, height=8, activestyle="none",
                                      background=_R_BG_WIDGET, foreground=_R_TEXT,
                                      selectbackground=_R_SEL_BG, selectforeground=_R_GOLD_BRT,
                                      font=_F_MAIN, exportselection=False)
        self._ed_proj_lb.pack(side="left", fill="both", expand=True)
        psb = ttk.Scrollbar(lwrap, orient="vertical", command=self._ed_proj_lb.yview)
        self._ed_proj_lb.configure(yscrollcommand=psb.set)
        psb.pack(side="left", fill="y")
        self._ed_proj_lb.bind("<Double-Button-1>", lambda *_: self._ed_load_selected())

        self._ed_dir_lbl = ttk.Label(lf, text="", foreground=_R_TEXT_DIM)
        self._ed_dir_lbl.pack(fill="x", padx=6, pady=(0, 6), anchor="w")

        # Right half: the Mod Editor "all logs" mirror — every tab's activity plus all Python-logging
        # output (incl. Pillow's DEBUG).  Never cleared for the life of the program.
        logf = ttk.LabelFrame(body, text=t("mgr.log_all_activity_never_cleared"))
        body.add(logf, weight=1)
        self._ed_master_log = _make_log(logf, height=20)
        self._ed_master_log.pack(fill="both", expand=True, padx=4, pady=4)
        _register_log_mirror(self._ed_master_log)

    def _ed_refresh_project_list(self):
        self._ed_proj_lb.delete(0, tk.END)
        self._ed_proj_paths = []
        base = self._editor_mods_dir()
        self._ed_dir_lbl.configure(text=str(base))
        try:
            subs = sorted([d for d in base.iterdir() if d.is_dir()],
                          key=lambda d: d.name.lower())
        except Exception:
            subs = []
        for d in subs:
            is_proj = mp_project_mod.ModProject.is_project_folder(d)
            self._ed_proj_lb.insert(tk.END, d.name + ("" if is_proj else t("mgr.no_project_json")))
            self._ed_proj_paths.append(d)
        if not self._ed_proj_paths:
            self._ed_proj_lb.insert(tk.END, t("mgr.no_mods_yet_create_one"))
            self._ed_proj_paths.append(None)
        self._ed_refresh_target_versions()

    def _ed_refresh_target_versions(self):
        """Populate the create-project version picker with every backed-up version, defaulting to the
        auto-detected installed build (the long-standing automatic behaviour)."""
        if not hasattr(self, "_ed_target_cb"):
            return
        # Shared behaviour lives in ui_util so this box and the Convert tab's 'Make mod for version'
        # box list the backed-up versions the exact same way.
        self._ed_target_map = ui_util.populate_version_combo(
            self._ed_target_cb, self._ed_target_ver,
            self._backed_up_versions(), default_key=self._version_key())

    def _ed_create_project(self):
        name = self._ed_new_name.get().strip()
        if not name:
            ui_util.info(self, t("mgr.mod_name"), t("mgr.enter_name_new_mod"))
            return
        # The chosen version comes from the picker (a backed-up version); fall back to the detected
        # build.  If nothing is backed up at all, guide the user to create a backup first.
        key = self._ed_target_map.get(self._ed_target_ver.get()) if getattr(self, "_ed_target_map", None) else None
        if not key:
            self._require_backup()
            return
        bdir = self._backup_dir_for(key)
        try:
            proj = mp_project_mod.ModProject.create(
                self._editor_mods_dir(), name, key,
                self._settings.get("game_root", ""), str(bdir))
        except FileExistsError:
            ui_util.error(self, t("mgr.already_exists"),
                                 t("mgr.mod_folder_name_already_exists"))
            self._ed_refresh_project_list()
            return
        except Exception as e:
            ui_util.error(self, t("common.create_failed"), str(e))
            return
        self._project = proj
        self._ed_new_name.set("")
        self._ed_show_hub()

    def _ed_load_selected(self):
        sel = self._ed_proj_lb.curselection()
        if not sel:
            ui_util.info(self, t("mgr.load"), t("mgr.select_mod_folder_load"))
            return
        folder = self._ed_proj_paths[sel[0]]
        if folder is not None:
            self._ed_open_folder(folder)

    def _ed_browse_project(self):
        """Reveal the mod-projects folder in the OS file explorer (no picking — just open it, so the
        user can see/manage their project folders)."""
        base = self._editor_mods_dir()
        try:
            base.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(base))               # Windows: open in Explorer
            else:
                ui_util.info(self, t("mgr.folder"), str(base))
        except Exception as e:
            ui_util.error(self, t("mgr.open_failed"), t("mgr.could_not_open_path_e", path=base, e=e))

    def _prompt_branch_build_id(self, current=""):
        """Ask which game version a project should target; return its build id (or None if cancelled /
        nothing to offer).  Shown when a project's saved version isn't one the editor recognises (a
        legacy branch NAME, or a retired build id) so it must be reassigned to a live build.

        The picker lists EXACTLY the versions the user has a clean backup for — the same set and
        behaviour as the Mod Editor / Convert version pickers (shared ui_util.populate_version_combo) —
        because a project can only be worked on against a backup it actually has.  Restricted to
        backups whose key IS a build id: the return value relabels the project via set_build_id, so a
        legacy format-name backup folder ('compat'/'public') is not a valid target here."""
        vers = [(k, l, p) for (k, l, p) in self._backed_up_versions() if str(k).isdigit()]
        if not vers:
            return None
        win = ui_util.themed_toplevel(self, t("mgr.which_game_version"))
        tk.Label(win, text=t("mgr.mod_project_was_saved_v", v=current),
                 justify="left").pack(padx=14, pady=(12, 6))
        var = tk.StringVar()
        cb = ttk.Combobox(win, textvariable=var, state="readonly", width=28)
        cb.pack(padx=14, pady=4)
        # default to the currently-detected build when it's among the backed-up versions
        mapping = ui_util.populate_version_combo(cb, var, vers, default_key=self._version_key())
        chosen = {"id": None}

        def ok():
            chosen["id"] = mapping.get(var.get())
            win.destroy()
        ttk.Button(win, text=t("common.ok"), command=ok).pack(pady=(6, 12))
        win.wait_window()
        return chosen["id"]

    def _ed_open_folder(self, folder):
        # Read pristine files from the backup matching the PROJECT's target version (so a project made
        # for another version still loads correctly).  A project can ONLY be opened against a backup of
        # the exact build it targets — that backup holds the original dats every edit is made against.
        ver = self._read_project_version(folder)
        if str(ver).isdigit():
            # The project targets a specific build id.  It can ONLY be opened against a backup of THAT
            # exact build.  If there's no such backup, refuse — and tailor the guidance to whether the
            # user can still get that version: a build a Steam branch currently serves is installable
            # (install it + Create Backup); a superseded/retired build isn't (they need someone else's
            # backup of that exact build id).
            bdir = self._backup_dir_for(ver)
            if not (bdir.exists() and any(bdir.rglob("*.dat"))):
                if self._build_installable(ver):
                    # Name the BRANCH too — that's the label the user picks in Steam's betas dropdown;
                    # they won't recognise a build id there.
                    msg = t("mgr.project_needs_version_backup",
                            ver=_gv_mod.display_name(ver),
                            branch=_gv_mod.branch_for_build(ver) or str(ver))
                else:
                    msg = t("mgr.project_needs_manual_backup",
                            ver=_gv_mod.display_name(ver), build=str(ver))
                ui_util.warning(self, t("mgr.backup_required"), msg)
                return
        else:
            # Legacy branch-NAME project (pre-build-id, no specific build id): fall back to the
            # current-build backup gate; the migration prompt below assigns it a live build id.
            key = self._version_key()
            bdir = self._backup_dir_for(key)
            if not (bdir.exists() and any(bdir.rglob("*.dat"))):
                if not self._require_backup():
                    return
                bdir = self._backup_dir()
        try:
            proj = mp_project_mod.ModProject.load(
                folder, self._settings.get("game_root", ""), str(bdir))
        except Exception as e:
            ui_util.error(self, t("common.load_failed"), str(e))
            return
        # Migrate legacy / stale projects to a live build id -> ask which build, store it.
        # Two cases need this: (a) 'version' is a branch NAME (pre-build-id projects), and
        # (b) 'version' is a build id that's no longer in the registry (a RETIRED build, e.g.
        # the old public 23762668 after a game update) — otherwise its converted rmods would be
        # stamped for a build the manager no longer knows and would nag / not list. Only RELABEL
        # when the chosen build shares the project's data-version (format); an OG<->remaster jump
        # must be CONVERTED, not relabeled, or the project's Data/PC/<sub> files wouldn't be found.
        _stale_build = str(proj.version).isdigit() and str(proj.version) not in _gv_mod.known_builds()
        if proj.needs_version_migration() or _stale_build:
            bid = self._prompt_branch_build_id(current=str(proj.version))
            if bid:
                old_sub = proj._sub()
                new_sub = _gv_mod.dataver_for_build(bid) or old_sub
                if new_sub == old_sub:
                    proj.set_build_id(bid)
                else:
                    ui_util.warning(
                        self,
                        t("mgr.different_game_format"),
                        t("mgr.project_s_files_are_data",
                          a=old_sub, b=_gv_mod.display_name(bid), c=new_sub))
        self._project = proj
        self._ed_show_hub()

    # ── Project hub ─────────────────────────────────────────────────────────────

    def _build_ed_hub(self, parent):
        self._ed_hub = ttk.Frame(parent)

        head = ttk.Frame(self._ed_hub)
        head.pack(fill="x", padx=8, pady=(8, 2))
        self._ed_proj_lbl = ttk.Label(head, text="", font=_F_HEAD, foreground=_R_GOLD_BRT)
        self._ed_proj_lbl.pack(side="left")
        self._ed_status_lbl = ttk.Label(head, text="", foreground=_R_TEXT_DIM)
        self._ed_status_lbl.pack(side="right")
        self._ed_path_lbl = ttk.Label(self._ed_hub, text="", foreground=_R_TEXT_DIM)
        self._ed_path_lbl.pack(anchor="w", padx=8)

        act = ttk.LabelFrame(self._ed_hub, text=t("mgr.mod_windows"))
        act.pack(fill="x", padx=8, pady=6)
        # Wrap the editor-open buttons (in a plain inner frame) so none (e.g. Raw / Asset Editor) is
        # clipped when the window is narrow (issue #5.3).  The help text is on its OWN line below, so
        # it never affects the button positions.
        act_row = ttk.Frame(act)
        act_row.pack(fill="x", padx=4, pady=(2, 0))
        # "Add .dat files…" sits at the RIGHT of the editor-window button row — it imports existing
        # .dat files into the project (see _ed_add_dats).  Packed FIRST so it reserves its right-edge
        # spot; the flow of window buttons then fills the remaining width and wraps within it.
        ttk.Button(act_row, text=t("mgr.add_dat_files"), command=self._ed_add_dats
                   ).pack(side="right", padx=4, pady=6)
        act_bar = ttk.Frame(act_row)
        act_bar.pack(side="left", fill="x", expand=True)
        act_btns = [
            ttk.Button(act_bar, text=t("common.units_buildings"), command=self._ed_open_units),
            ttk.Button(act_bar, text=t("mgr.map_editor"), command=self._open_map_editor),
            ttk.Button(act_bar, text=t("mgr.ai"), command=self._ed_open_ai),
            ttk.Button(act_bar, text=t("mgr.economy"), command=self._ed_open_economy),
            ttk.Button(act_bar, text=t("mgr.raw_asset_editor"), command=self._ed_open_tools),
        ]
        ui_util.flow(act_bar, act_btns, pady=6)
        ttk.Label(act, text=t("mgr.each_window_has_its_own"),
                  foreground=_R_TEXT_DIM).pack(anchor="w", padx=8, pady=(2, 4))

        proj = ttk.LabelFrame(self._ed_hub, text=t("mgr.project"))
        proj.pack(fill="x", padx=8, pady=(0, 6))
        self._ed_deploy_btn = ttk.Button(proj, text=t("mgr.deploy_game"), command=self._ed_deploy)
        self._ed_deploy_btn.pack(side="left", padx=4, pady=6)
        # Restore Clean here is the SAME operation as the Mod Manager / Settings buttons (shared
        # _mgr_restore_clean): revert the whole install from the clean backup.  This is why deploy no
        # longer writes per-file .bak copies — the full backup + this button already cover reverting.
        self._ed_restore_btn = ttk.Button(proj, text=t("mgr.restore_clean"),
                                          command=self._mgr_restore_clean)
        self._ed_restore_btn.pack(side="left", padx=4, pady=6)
        ttk.Button(proj, text=t("mgr.convert_rmod"), command=self._ed_convert_to_rmod
                   ).pack(side="left", padx=4, pady=6)
        # Pack Close Project (right) BEFORE the help text so it keeps its spot in a narrow window —
        # the long explanatory label clips instead of pushing Close Project off-screen (issue #5.3).
        ttk.Button(proj, text=t("mgr.close_project"), command=self._ed_close_project
                   ).pack(side="right", padx=4, pady=6)
        ttk.Label(proj, text=t("mgr.deploy_write_dats_into_game"),
                  foreground=_R_TEXT_DIM).pack(side="left", padx=10)

        # ── Bottom: Mod Details (left) + Log (right) — fill the remaining hub space ───
        bottom = ttk.Frame(self._ed_hub)
        bottom.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        det = ttk.LabelFrame(bottom, text=t("mgr.mod_details_description_txt"))
        det.pack(side="left", fill="both", expand=True, padx=(0, 4))
        det.columnconfigure(1, weight=1)
        det.rowconfigure(2, weight=1)          # description row stretches (matches the Mod Manager box)

        ttk.Label(det, text=t("mgr.author")).grid(row=0, column=0, sticky="e", padx=6, pady=(6, 3))
        self._ed_meta_author = tk.StringVar()
        ttk.Entry(det, textvariable=self._ed_meta_author).grid(
            row=0, column=1, sticky="ew", padx=6, pady=(6, 3))

        ttk.Label(det, text=t("mgr.version")).grid(row=1, column=0, sticky="e", padx=6, pady=3)
        verrow = ttk.Frame(det)
        verrow.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        self._ed_meta_ver = tk.StringVar()
        ed_ver_ent = ttk.Entry(verrow, textvariable=self._ed_meta_ver, width=14)
        ed_ver_ent.pack(side="left")
        # Empty or non-x.x.x input snaps to the 1.0.0 default when the field loses focus.
        ed_ver_ent.bind("<FocusOut>", lambda *_: self._ed_normalize_version())
        # Game version the project was BUILT AGAINST (read-only) — reuses the create-picker's
        # "Game version:" label so, right beside the mod's own version, the user can always see which
        # game build this project targets instead of guessing.
        ttk.Label(verrow, text=t("mgr.game_version"),
                  foreground=_R_TEXT_DIM).pack(side="left", padx=(18, 4))
        self._ed_meta_gamever = tk.StringVar()
        ttk.Label(verrow, textvariable=self._ed_meta_gamever,
                  foreground=_R_GOLD, font=_F_BOLD).pack(side="left")

        ttk.Label(det, text=t("mgr.description_2")).grid(row=2, column=0, sticky="ne", padx=6, pady=3)
        self._ed_meta_desc = _ThemedScrolledText(
            det, font=_F_MAIN, wrap="word", relief="flat",
            background=_R_BG_WIDGET, foreground=_R_TEXT, insertbackground=_R_GOLD,
            highlightthickness=1, highlightcolor=_R_BORDER, highlightbackground=_R_BORDER)
        self._ed_meta_desc.grid(row=2, column=1, sticky="nsew", padx=6, pady=3)

        br = ttk.Frame(det)
        br.grid(row=3, column=1, sticky="ew", padx=6, pady=(0, 4))
        self._ed_meta_save_btn = ttk.Button(br, text=t("mgr.save_details"),
                                            command=self._ed_save_description, state="disabled")
        self._ed_meta_save_btn.pack(side="left")
        self._ed_meta_status = ttk.Label(br, text="", foreground=_R_TEXT_DIM)
        self._ed_meta_status.pack(side="left", padx=8)
        ttk.Label(det, text=t("mgr.first_line_author_last_line"),
                  foreground=_R_TEXT_DIM, wraplength=380, justify="left").grid(
            row=4, column=1, sticky="w", padx=6, pady=(0, 6))

        logf = ttk.LabelFrame(bottom, text=t("mgr.log_all_activity_never_cleared"))
        logf.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self._ed_log = _make_log(logf, height=14)
        self._ed_log.pack(fill="both", expand=True, padx=4, pady=4)
        _register_log_mirror(self._ed_log)   # mirror: shows ALL activity, like the selection-screen log

        # Enable Save Details whenever a field differs from what's saved to description.txt.
        self._ed_meta_saved = ("", "", "")
        self._ed_meta_author.trace_add("write", lambda *_: self._ed_meta_check_dirty())
        self._ed_meta_ver.trace_add("write", lambda *_: self._ed_meta_check_dirty())
        self._ed_meta_desc.bind("<<Modified>>", self._ed_meta_on_text_modified)

    def _ed_show_select(self):
        self._ed_clear_views()
        if hasattr(self, "_ed_hub"):
            self._ed_hub.pack_forget()
        self._ed_refresh_project_list()
        self._ed_select.pack(fill="both", expand=True)

    def _ed_show_hub(self):
        self._ed_clear_views()
        self._ed_sync_project_paths()
        self._ed_select.pack_forget()
        self._ed_proj_lbl.configure(text=t("mgr.mod_name_3", name=self._project.name))
        self._ed_path_lbl.configure(text=str(self._project.folder))
        self._ed_load_description()
        self._ed_update_status()
        self._mgr_set_busy(self._mgr_running)   # set Deploy / Restore Clean button states (backup gate)
        self._ed_hub.pack(fill="both", expand=True)

    # ── Mod Details (description.txt) ─────────────────────────────────────────────

    def _ed_meta_current(self) -> tuple:
        """The (author, description, version) currently shown in the Mod Details fields."""
        return (self._ed_meta_author.get().strip(),
                self._ed_meta_desc.get("1.0", tk.END).strip(),
                self._ed_meta_ver.get().strip())

    def _ed_meta_dirty(self) -> bool:
        return self._ed_meta_current() != self._ed_meta_saved

    def _ed_meta_check_dirty(self):
        """Enable Save Details (and show the unsaved marker) when a field differs from what's saved."""
        if not getattr(self, "_ed_meta_save_btn", None):
            return
        dirty = self._ed_meta_dirty()
        self._ed_meta_save_btn.configure(state=("normal" if dirty else "disabled"))
        self._ed_meta_status.configure(text=(t("mgr.unsaved") if dirty else ""), foreground=_R_GOLD)

    def _ed_meta_on_text_modified(self, _event=None):
        # The Text <<Modified>> flag latches; clear it so later edits fire again, then re-check.
        if self._ed_meta_desc.edit_modified():
            self._ed_meta_desc.edit_modified(False)
            self._ed_meta_check_dirty()

    def _ed_normalize_version(self):
        """Snap the version field to x.x.x (1.0.0 when blank or not in that format)."""
        self._ed_meta_ver.set(_normalize_version(self._ed_meta_ver.get()))

    def _ed_load_description(self):
        """Load the project's description.txt into the Mod Details fields and reset dirty state."""
        if not self._project:
            return
        meta = self._project.read_description()
        self._ed_meta_author.set(meta["author"])
        # Blank / malformed stored versions show as the 1.0.0 default.
        self._ed_meta_ver.set(_normalize_version(meta["version"]))
        # Game version the project targets (read-only): friendly "branch (vBUILD)" when known, else the
        # raw build id / legacy branch name.  Set on every hub load so a relabel/migration is reflected.
        pv = str(self._project.version)
        if hasattr(self, "_ed_meta_gamever"):
            self._ed_meta_gamever.set(_gv_mod.display_name(pv) if pv.isdigit() else pv)
        self._ed_meta_desc.delete("1.0", tk.END)
        self._ed_meta_desc.insert("1.0", meta["description"])
        self._ed_meta_desc.edit_modified(False)
        self._ed_meta_saved = self._ed_meta_current()
        self._ed_meta_check_dirty()

    def _ed_save_description(self):
        if not self._project:
            return
        self._ed_normalize_version()   # never persist a non-x.x.x version
        author, description, version = self._ed_meta_current()
        try:
            self._project.write_description(author, description, version)
        except Exception as e:
            ui_util.error(self, t("mgr.save_details"), t("mgr.could_not_write_description_txt", e=e))
            return
        self._ed_meta_saved = (author, description, version)
        self._ed_meta_check_dirty()
        self._ed_meta_status.configure(text=t("mgr.saved"), foreground=_R_GREEN)
        self.after(2000, self._ed_meta_check_dirty)

    def _backup_dir_for_project(self, proj) -> Path:
        """Backup dir matching the PROJECT's target version (a project may target a different version
        than the installed game — that's the whole point of the version picker).  Build-id version →
        that build's backup; legacy/unknown → the current game build's backup."""
        ver = str(getattr(proj, "version", "") or "")
        key = ver if ver.isdigit() else self._version_key()
        bdir = self._backup_dir_for(key)
        if bdir.exists() and any(bdir.rglob("*.dat")):
            return bdir
        return self._backup_dir()

    def _ed_sync_project_paths(self):
        """Keep the project's game_root/backup_dir current with Settings.  backup_dir tracks the
        PROJECT's target version (NOT necessarily the installed game's build), so editing a project
        made for another version reads that version's clean files."""
        if self._project:
            self._project.game_root = self._settings.get("game_root", "")
            self._project.backup_dir = str(self._backup_dir_for_project(self._project))

    def _ed_update_status(self):
        if not self._project:
            return
        n = self._project.dirty_count()
        self._ed_status_lbl.configure(
            text=(t("common.n_unsaved_change_set_s", n=n) if n else t("common.all_changes_saved")),
            foreground=(_R_GOLD if n else _R_GREEN))

    def _ed_open_units(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import units_editor
            view = units_editor.UnitsEditorWindow(
                self._ed_content, self._project, on_change=self._ed_update_status,
                default_lang=self._settings.get("default_language", "us"))
        except Exception as e:
            ui_util.error(self, t("common.units_editor"), t("mgr.failed_open_units_editor_e", e=e))
            return
        self._ed_open_view(view, t("mgr.units_buildings"))

    def _ed_open_ai(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import ai_editor
            view = ai_editor.AIEditorWindow(self._ed_content, self._project,
                                            on_change=self._ed_update_status)
        except Exception as e:
            ui_util.error(self, t("common.ai_editor"), t("mgr.failed_open_ai_editor_e", e=e))
            return
        self._ed_open_view(view, t("common.ai"))

    def _ed_open_economy(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import economy_editor
            view = economy_editor.EconomyEditorWindow(self._ed_content, self._project,
                                                      on_change=self._ed_update_status)
        except Exception as e:
            ui_util.error(self, t("common.economy_editor"), t("mgr.failed_open_economy_editor_e", e=e))
            return
        self._ed_open_view(view, t("common.economy"))

    def _ed_open_tools(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import tools_editor
        except Exception as e:
            ui_util.error(self, t("mgr.raw_asset_editor_2"), t("mgr.could_not_load_tools_module", e=e))
            return
        try:
            view = tools_editor.ToolsEditorWindow(
                self._ed_content, self._project, on_change=self._ed_update_status,
                open_nested=self._ed_open_nested_tools)
            self._ed_open_view(view, t("mgr.raw_asset_editor_2"), cleanup=view.cleanup)
        except Exception as e:
            ui_util.error(self, t("mgr.raw_asset_editor_2"), t("mgr.failed_open_raw_asset_editor", e=e))

    def _ed_open_nested_tools(self, store, on_applied):
        """Open an embedded .dat (from the Raw editor's 'Open as nested .dat') as ANOTHER nested view on
        the same stack, so Back walks back out through each nested archive to the hub."""
        try:
            import tools_editor
            view = tools_editor.ToolsEditorWindow(
                self._ed_content, on_change=on_applied, store=store,
                open_nested=self._ed_open_nested_tools)
        except Exception as e:
            ui_util.error(self, t("mgr.raw_asset_editor_2"), t("mgr.failed_open_nested_archive_e", e=e))
            return
        self._ed_open_view(view, t("mgr.raw_asset_editor_name", name=store.name), cleanup=view.cleanup)

    def _ed_add_dats(self):
        """Import existing .dat files into the current project.  The user already has built .dat files
        (e.g. from another tool) and wants them in a project.  Each picked file is routed by FILENAME:
        the six core dats have fixed unique names → Data/PC/<sub>/; anything else is a per-map terrain
        dat → Maps/PC/.  (Names are unique, so no folder navigation is needed.)  Files are copied in,
        then the project is RELOADED so the editors use them.  The Data/PC sub-folder is decided by the
        project's target version — a backup for that version is what the editors read clean files from."""
        if not self._project:
            return
        paths = filedialog.askopenfilenames(
            parent=self,
            title=t("mgr.add_dat_files_project"),
            initialdir=self._settings.get("game_root", "") or str(self._editor_mods_dir()),
            filetypes=[(t("mgr.game_data_files"), "*.dat"), (t("common.all_files"), "*.*")])
        if not paths:
            return
        core_by_name = {fn.lower(): dk for dk, fn in mp_project_mod.DAT_FILES.items()}
        plan = []                                          # (src, dest, rel-in-project)
        for sp in paths:
            sp = Path(sp)
            dk = core_by_name.get(sp.name.lower())
            key = dk if dk else (mp_project_mod._TERRAIN_PREFIX + sp.name)
            dest = self._project.project_dat_path(key)
            rel = str(dest.relative_to(self._project.folder)).replace(os.sep, "/")
            plan.append((sp, dest, rel))
        listing = "\n".join(t("mgr.name_rel", name=sp.name, rel=rel) for sp, _, rel in plan)
        msg = t("mgr.copy_these_dat_file_s",
                ver=self._project.branch_label, listing=listing)
        overwrite = [rel for _, d, rel in plan if d.is_file()]
        if overwrite:
            msg += t("mgr.replaces_file_s_already_project") + "\n".join("  • " + r for r in overwrite)
        if not ui_util.confirm(self, t("mgr.add_dat_files_2"), msg):
            return
        copied = 0
        for sp, dest, rel in plan:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(sp), str(dest))
                copied += 1
                _log(self._ed_log, t("mgr.added_dat_rel", rel=rel), "ok")
            except Exception as e:
                ui_util.error(self, t("mgr.add_dat_files_2"), t("mgr.could_not_copy_name_e", name=sp.name, e=e))
                return
        # Reload the project from disk so the newly-copied dats are what the editor has loaded.
        folder = self._project.folder
        try:
            self._project = mp_project_mod.ModProject.load(
                folder, self._settings.get("game_root", ""), str(self._project.backup_dir))
        except Exception as e:
            ui_util.error(self, t("common.load_failed"), str(e))
            return
        self._ed_show_hub()
        ui_util.info(self, t("mgr.add_dat_files_2"),
                            t("mgr.added_n_dat_file_s", n=copied))

    def _ed_deploy(self):
        if not self._project:
            return
        if self._mgr_running:   # a deploy/backup/restore (any tab) is already touching the game files
            ui_util.info(self, t("mgr.please_wait"), t("mgr.op_running_try_again"))
            return
        self._ed_sync_project_paths()
        if not self._settings.get("game_root", ""):
            ui_util.info(self, t("mgr.no_game_root"), t("mgr.set_game_root_directory_settings"))
            return
        # Require a clean backup of the INSTALLED game before deploying: deploy no longer writes per-file
        # .bak copies, so 'Restore Clean' (which reverts from this backup) is the only way back.  Without
        # it, a deploy would leave the install modified with no in-app revert.  Matches the Mod Manager
        # deploy's backup gate.
        bd_install = self._backup_dir()
        if not (bd_install.exists() and any(bd_install.rglob("*.dat"))):
            ui_util.error(self, t("mgr.no_backup"), t("mgr.no_game_file_backup_found"))
            return
        if self._project.is_dirty():
            ui_util.info(
                self,
                t("mgr.unsaved_changes"),
                t("mgr.have_changes_aren_t_saved"))
            return
        dats = self._project.saved_dats()
        if not dats:
            ui_util.info(self, t("mgr.deploy"),
                                t("mgr.nothing_deploy_yet_make_save"))
            return
        # Warn if the installed game is a DIFFERENT version than this project targets: deploying writes
        # the project's version files into a game set to another version, which may not load correctly.
        target = str(self._project.version)
        installed = self._game_build_id()
        if target.isdigit() and installed and installed != target:
            try:
                target_lbl = _gv_mod.display_name(target)
            except Exception:
                target_lbl = target
            if not ui_util.confirm(
                    self,
                    t("mgr.wrong_game_version_2"),
                    t("mgr.mod_project_targets_target_but",
                      target=target_lbl, installed=self._branch_label())):
                return
        if not ui_util.confirm(
                self,
                t("mgr.deploy_game_2"),
                t("mgr.deploy_confirm_intro")
                + t("mgr.will_overwrite_live_game_file")
                + "\n".join("  • " + d.name for d in dats)
                + t("mgr.deploy_overwrite_proceed")):
            return
        # Leftovers are reset to the INSTALLED game's OWN clean copy, so their clean source is the
        # INSTALLED build's backup — NOT this project's backup (a project may target a different version
        # than the installed game; reverting from the project's backup would write a DIFFERENT version's
        # clean dat over the install).  This mirrors the Mod Manager's Deploy leftover-restore exactly.
        install_bd = self._backup_dir()
        game_root = Path(self._settings["game_root"])
        _log(self._ed_log, t("mgr.deploying_mod_name_game", name=self._project.name), "head")
        try:
            # Shared deploy tracker: first revert LEFTOVERS — dats a PREVIOUS deploy (this project, the
            # Mod Manager, or another project) modified that this mod won't overwrite — so they don't
            # stay dirty.  The dats this mod deploys get overwritten below, so they need no pre-clean.
            proj_rels = {r.replace("/", os.sep) for r in self._project.deployed_dat_rels()}
            prev = {r.replace("/", os.sep) for r in self._mgr_saved_deployed_dats()}
            reverted = 0
            for rel in sorted(prev - proj_rels):
                src = install_bd / rel
                if src.is_file():
                    dest = game_root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    reverted += 1
                    _log(self._ed_log, t("mgr.reverted_leftover_rel", rel=rel), "info")
                else:
                    # No clean backup of the installed game for this leftover — can't revert it; log it
                    # (don't silently skip), matching the Mod Manager's leftover-restore warning.
                    _log(self._ed_log, t("mgr.warn_no_clean_backup_restore", dat_rel=rel), "warn")
            copied = self._project.deploy()
            for c in copied:
                _log(self._ed_log, t("mgr.deployed_c", c=c), "ok")
            # Record what's now overlaid so the next deploy (here or in the Mod Manager) can clean it.
            self._mgr_set_deployed_dats(r.replace(os.sep, "/") for r in proj_rels)
        except Exception as e:
            _log(self._ed_log, t("mgr.error_e_2", e=e), "err")
            ui_util.error(self, t("mgr.deploy"), str(e))
            return
        _log(self._ed_log, t("mgr.done_deployed_n_file_s",
                             n=len(copied), reverted=reverted), "head")
        msg = t("mgr.deployed_2") + "\n".join(copied)
        if reverted:
            msg += t("mgr.reverted_reverted_leftover_file_s", reverted=reverted)
        ui_util.info(self, t("mgr.deployed"), msg)

    def _ed_convert_to_rmod(self):
        if not self._project or getattr(self, "_ed_converting", False):
            return
        # Don't diff against the build's clean backup while a deploy/backup/restore is mid-write — same
        # guard the Convert tab's Convert / Convert-all-versions buttons enforce.
        if getattr(self, "_mgr_running", False):
            ui_util.info(self, t("mgr.please_wait"), t("mgr.mgr_op_running_let_finish"))
            return
        self._ed_sync_project_paths()
        if self._project.is_dirty():
            ui_util.info(self, t("mgr.unsaved_changes"),
                                t("mgr.save_changes_editor_windows_first"))
            return
        dats = self._project.saved_dats()
        if not dats:
            ui_util.info(self, t("mgr.convert_rmod_2"),
                                t("mgr.nothing_convert_yet_make_save"))
            return
        # Offer to flush unsaved Mod Details so the rmod carries the author/version/description shown.
        if self._ed_meta_dirty() and ui_util.confirm(
                self,
                t("mgr.unsaved_details"),
                t("mgr.have_unsaved_mod_details_author")):
            self._ed_save_description()
        meta = self._project.read_description()

        def _done(ok, out_rmod, err):
            self._ed_converting = False
            self._ed_update_status()
            if err:
                _log(self._ed_log, t("mgr.error_err_2", err=err), "err")
                ui_util.error(self, t("mgr.convert_rmod_2"), err)
            elif ok:
                _log(self._ed_log, t("mgr.done_wrote_out_rmod", out_rmod=out_rmod), "ok")
                ui_util.info(self, t("mgr.convert_rmod_2"),
                                    t("mgr.exported_update_mod_only_changes",
                                      out_rmod=out_rmod))
            else:
                _log(self._ed_log, t("mgr.no_changes_found_convert"), "warn")
                ui_util.warning(self, t("mgr.convert_rmod_2"), t("mgr.no_changes_were_found_convert"))

        # Use the SAME conversion path as the Convert tab: versioned _V#/-v# naming, branch detection,
        # shipped to the mods folder, diffed against the clean originals.
        self._ed_converting = True
        self._ed_status_lbl.configure(text=t("mgr.converting_rmod_diffing_please_wait"),
                                      foreground=_R_GOLD)
        # Target the build the PROJECT was created for so the diff baseline, destination
        # folder and stamped game_version all agree (a project targeting compat-2 while the
        # user runs public still converts against compat-2's clean files). Falls back to the
        # installed build only if the project still holds a legacy branch name (not a build id).
        proj_build = str(self._project.version) if str(self._project.version).isdigit() else ""
        if self._start_rmod_convert(
                mod_folder=str(self._project.folder),
                name=self._project.name,
                mod_id=_name_to_id(self._project.name),
                version=meta["version"],
                author=meta["author"],
                description=meta["description"] or "Created with the RUSE Mod Editor",
                log=self._ed_log, on_done=_done, target_build=proj_build) is None:
            self._ed_converting = False
            self._ed_update_status()

    def _ed_close_project(self):
        if self._project and self._project.is_dirty():
            if not ui_util.confirm(
                    self,
                    t("mgr.unsaved_changes"),
                    t("mgr.there_are_changes_not_yet")):
                return
        self._project = None
        self._ed_show_select()

    def _open_map_editor(self):
        """Open the map editor as a nested in-tab view, bound to the current mod project so all edits
        save into the mod folder (like the other editor windows), never the live game."""
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import map_editor
            view = map_editor.MapEditorWindow(self._ed_content, self._project,
                                              on_change=self._ed_update_status)
        except Exception as e:
            ui_util.error(self, t("common.map_editor"), t("mgr.failed_open_map_editor_e", e=e))
            return
        self._ed_open_view(view, t("common.map_editor"))

    # =========================================================================
    # SETTINGS TAB
    # =========================================================================

    def _scrollable_body(self, parent):
        """Wrap a tab body in a vertically-scrollable canvas so tall, fixed-height content (e.g. the
        Settings sections) is never clipped when the window is short.  Returns the inner frame;
        pack/grid your sections into *that* instead of into `parent`.  Shared scroll behavior
        (issue #12): content that fits is pinned to the top (no phantom over-scroll), and the wheel
        scrolls whatever the pointer is over (an inner list/notes box scrolls itself)."""
        return ui_util.make_scrollable(parent, bg=_R_BG_PANEL)

    def _build_settings_tab(self, p):
        # Make the whole tab scrollable: sections below pack into this inner frame, so
        # nothing (Accessibility, Output Folder Structure, …) is lost on short windows.
        p = self._scrollable_body(p)
        pad = {"padx": 8, "pady": 6}

        ttk.Label(p, text=t("mgr.configure_paths_r_u_s"),
                  foreground=_R_TEXT_DIM).pack(anchor="w", padx=10, pady=(10, 4))

        pf = ttk.LabelFrame(p, text=t("mgr.paths"))
        pf.pack(fill="x", **pad)
        pf.columnconfigure(1, weight=1)

        # (label, settings key, editable?, hint).  Only the Game Root is user-set; the working dir and
        # mods folder are AUTO-derived from the app's location, so they're shown read-only and their
        # button just opens the folder in the file explorer (it doesn't change the path).
        defs = [
            (t("mgr.game_root_directory"), "game_root", True,
             t("mgr.root_folder_r_u_s")),
            (t("mgr.working_directory"),   "working_dir", False,
             t("mgr.where_app_lives_output_state")),
            (t("mgr.mods_folder"),         "mods_folder", False,
             t("mgr.where_rmod_files_live_auto")),
        ]
        # Row layout: status at 0, game_root at 1-2, working_dir at 4-5, mods_folder at 6-7
        _entry_rows = [1, 4, 6]
        self._set_vars: dict = {}
        self._set_save_job = None
        for ri, (label, key, editable, hint) in enumerate(defs):
            er = _entry_rows[ri]
            ttk.Label(pf, text=label).grid(
                row=er, column=0, sticky="ne", **pad)
            var = tk.StringVar(value=self._settings.get(key, ""))
            self._set_vars[key] = var
            if editable:
                var.trace_add("write", lambda *_: self._set_schedule_save())
                ttk.Entry(pf, textvariable=var).grid(row=er, column=1, sticky="ew", **pad)
                ttk.Button(pf, text=t("mgr.browse"), command=self._set_browse_game_root).grid(
                    row=er, column=2, padx=4, pady=6)
            else:
                # read-only: visible & copyable but not editable; the button opens it in Explorer.
                ttk.Entry(pf, textvariable=var, state="readonly").grid(
                    row=er, column=1, sticky="ew", **pad)
                ttk.Button(pf, text=t("mgr.open"), command=lambda k=key: self._set_open_folder(k)).grid(
                    row=er, column=2, padx=4, pady=6)
            ttk.Label(pf, text=hint, foreground=_R_TEXT_DIM, wraplength=580,
                      justify="left").grid(
                row=er+1, column=1, sticky="w", padx=8, pady=(0, 4))

        self._set_s1_lbl = tk.Label(pf, text="", font=_F_LOG,
                                    background=_R_BG_PANEL, anchor="w")
        self._set_s1_lbl.grid(row=0, column=1, columnspan=2,
                               sticky="ew", padx=8, pady=(16, 4))

        sf = ttk.Frame(p)
        sf.pack(fill="x", **pad)
        self._set_status = tk.StringVar()
        ttk.Label(sf, textvariable=self._set_status,
                  foreground=_COL_OK, font=_F_LOG).pack(side="left", padx=2)

        bf = ttk.LabelFrame(p, text=t("mgr.game_file_backup"))
        bf.pack(fill="x", **pad)

        bfh = ttk.Frame(bf)
        bfh.pack(fill="x")

        # Left — game file backup
        bfl = ttk.Frame(bfh)
        bfl.pack(side="left", fill="both", expand=True)
        ttk.Label(bfl,
                  text=t("mgr.back_up_original_game_files"),
                  foreground=_R_TEXT_DIM, font=_F_LOG, justify="left",
                  ).pack(anchor="w", padx=8, pady=(6, 2))
        btn_row = ttk.Frame(bfl)
        btn_row.pack(anchor="w", padx=8, pady=(2, 4))
        self._set_backup_btn = ttk.Button(btn_row, text=t("mgr.create_backup"),
                                          command=self._mgr_create_backup,
                                          state="disabled")
        self._set_backup_btn.pack(side="left", padx=(0, 4))
        self._set_restore_btn = ttk.Button(btn_row, text=t("mgr.restore_clean"),
                                           command=self._mgr_restore_clean,
                                           state="disabled")
        self._set_restore_btn.pack(side="left", padx=(0, 4))
        ttk.Button(btn_row, text=t("mgr.detect_game_version"),
                   command=self._set_detect_game).pack(side="left")
        self._set_s2_lbl = tk.Label(bfl, text="", font=_F_LOG,
                                    background=_R_BG_PANEL, anchor="w")
        self._set_s2_lbl.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Separator(bfh, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)

        # Right — profile management
        bfr = ttk.Frame(bfh)
        bfr.pack(side="left", fill="y", padx=4, pady=4)
        ttk.Label(bfr, text=t("mgr.profile"), foreground=_R_TEXT_DIM,
                  font=_F_BOLD).pack(anchor="w", pady=(2, 4))
        self._prof_lvl1_btn = ttk.Button(
            bfr, text=t("mgr.set_lvl_1_profile"), command=self._profile_set_lvl1)
        self._prof_lvl1_btn.pack(fill="x", pady=2)
        self._prof_lvl100_btn = ttk.Button(
            bfr, text=t("mgr.set_lvl_100_profile"), command=self._profile_set_lvl100)
        self._prof_lvl100_btn.pack(fill="x", pady=2)
        self._prof_backup_btn = ttk.Button(bfr, text=t("mgr.back_up_current_profile"),
                                            command=self._profile_backup_current,
                                            state="disabled")
        self._prof_backup_btn.pack(fill="x", pady=(6, 2))
        self._prof_set_backup_btn = ttk.Button(bfr, text=t("mgr.set_backed_up_profile"),
                                               command=self._profile_set_backed_up,
                                               state="disabled")
        self._prof_set_backup_btn.pack(fill="x", pady=(2, 2))
        # Which backed-up profile the button applies: "Auto" (newest applicable, the default) or a
        # specific populated version the user picks. Values are refreshed in _profile_refresh_ui.
        self._prof_apply_choice = tk.StringVar(value=_PROFILE_AUTO)
        self._prof_apply_cb = ttk.Combobox(bfr, textvariable=self._prof_apply_choice,
                                           state="readonly", values=[_PROFILE_AUTO])
        self._prof_apply_cb.pack(fill="x", pady=(0, 2))

        info = ttk.LabelFrame(p, text=t("mgr.output_folder_structure"))
        info.pack(fill="x", **pad)
        ttk.Label(info,
                  text=t("mgr.output_backups_original_game_files"),
                  justify="left",
                  font=_F_LOG).pack(padx=8, pady=6, anchor="w")

        # ── Accessibility (at the very bottom) ──────────────────────────────────
        acc = ttk.LabelFrame(p, text=t("mgr.accessibility"))
        acc.pack(fill="x", **pad)
        arow = ttk.Frame(acc)
        arow.pack(fill="x", padx=8, pady=6)
        ttk.Label(arow, text=t("mgr.default_language")).pack(side="left")
        cur_code = self._settings.get("default_language", "us")
        self._lang_var = tk.StringVar(value=_dic_mod.lang_label(cur_code))
        self._lang_cb = ttk.Combobox(arow, textvariable=self._lang_var, state="readonly", width=22,
                                     values=[name for _c, name in _dic_mod.LANGUAGES])
        self._lang_cb.pack(side="left", padx=8)
        ui_util.fit_combobox(self._lang_cb)   # fit names like "Chinese (Simplified)" (issue #5.1)
        self._lang_cb.bind("<<ComboboxSelected>>", self._on_default_language)
        # A mouse-wheel over a readonly Combobox cycles its value (Tk default) — here that would fire
        # a language-change confirm on every scroll of the Settings tab.  Swallow the wheel so scrolling
        # never silently changes the language.
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._lang_cb.bind(_seq, lambda _e: "break")
        # Right side: create Windows shortcuts to this app's .exe.
        sc = ttk.Frame(arow)
        sc.pack(side="right")
        ttk.Button(sc, text=t("mgr.add_start_menu_shortcut"),
                   command=self._add_start_menu_shortcut).pack(side="left", padx=2)
        ttk.Button(sc, text=t("mgr.add_desktop_shortcut"),
                   command=self._add_desktop_shortcut).pack(side="left", padx=2)
        ttk.Label(acc, text=t("mgr.localization_language_used_default_when"),
                  foreground=_R_TEXT_DIM, font=_F_LOG, justify="left", wraplength=580
                  ).pack(anchor="w", padx=8, pady=(0, 6))

        # ── About & Legal (non-affiliation, no-warranty, privacy) ───────────────
        about = ttk.LabelFrame(p, text=t("mgr.about_legal"))
        about.pack(fill="x", **pad)
        ver = self._app_version()
        ttk.Label(about, text=(t("mgr.ruse_mod_manager") + (f"  v{ver}" if ver else "")),
                  font=_F_BOLD).pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Label(
            about,
            text=t("mgr.unofficial_fan_tool_ruse_mod"),
            foreground=_R_TEXT_DIM, font=_F_LOG, justify="left", wraplength=580,
        ).pack(anchor="w", padx=8, pady=(0, 4))
        abtn = ttk.Frame(about)
        abtn.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(abtn, text=t("mgr.project_page_source"),
                   command=lambda: self._open_url(
                       "https://github.com/LittleGroove/RUSE-Mod-Manager")).pack(side="left", padx=(0, 4))
        ttk.Button(abtn, text=t("mgr.license_gpl_3_0"),
                   command=lambda: self._open_url(
                       "https://github.com/LittleGroove/RUSE-Mod-Manager/blob/main/LICENSE")
                   ).pack(side="left")

        self._profile_refresh_ui()

    def _app_version(self) -> str:
        """The app's version string for display: the embedded build version when packaged, else the
        source-tree build_config.json. '' if neither is available (never fatal)."""
        try:
            import auto_update
            ver = auto_update.current_version()
            if ver:
                return str(ver)
        except Exception:
            pass
        try:
            cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_config.json")
            with open(cfg, encoding="utf-8") as f:
                v = json.load(f)
            return f"{v.get('major', 1)}.{v.get('minor', 0)}.{v.get('patch', 0)}"
        except Exception:
            return ""

    def _open_url(self, url: str):
        """Open a link in the user's browser; show a themed error (with the URL) if it can't."""
        try:
            if webbrowser.open(url):
                return
        except Exception:
            pass
        ui_util.error(self, t("mgr.couldn_t_open_browser"),
                      t("mgr.please_open_link_yourself_url", url=url))

    def _on_default_language(self, _=None):
        code = _dic_mod.LANG_CODE.get(self._lang_var.get(), "us")
        prev = self._settings.get("default_language", "us")
        if code == prev:
            return
        # Re-entrancy guard: the confirm below runs a nested event loop (wait_window).  Without this,
        # another <<ComboboxSelected>> firing while it's open would stack a second modal dialog — and a
        # later "Yes" calling quit() from an inner loop while an outer dialog is still open hangs the app
        # (mainloop can't return until every nested loop does).
        if getattr(self, "_lang_switch_pending", False):
            return
        # The picked language is a real, persisted choice: it's what the app opens in from now on,
        # whether or not the user restarts.  The popup only asks WHETHER TO RESTART NOW to apply it to
        # the already-built UI immediately (the interface can't re-language itself in place).
        self._settings["default_language"] = code
        self._save_settings()
        self._lang_switch_pending = True
        i18n.set_language(code)                 # preview the confirm in the newly-selected language
        try:
            restart_now = ui_util.confirm(
                self,
                t("mgr.restart_change_language"),
                t("mgr.interface_language_changes_when_mod"))
        finally:
            self._lang_switch_pending = False
        if restart_now:
            self._restart_app()
        else:
            # Keep the selection (dropdown + settings stay on it, applies next launch).  Only return the
            # RUNNING session to its built language so it stays visually consistent until a restart.
            i18n.set_language(prev)

    def _restart_app(self):
        """Relaunch the app and close this one.

        On a frozen onefile build, ANYTHING we spawn as our own child runs inside the PyInstaller
        bootloader's Windows Job object — which makes it pin our ``_MEIxxxx`` temp dir ("Failed to
        remove temporary directory" on exit) and/or get killed when we exit.  So we do NOT launch the
        new instance ourselves.  We ask the already-running shell (``explorer.exe``) to open the exe:
        the new instance becomes EXPLORER's child, OUTSIDE our job — verified that explorer, not us,
        is its parent.  We therefore keep no child that pins our temp dir (clean removal), the
        relaunch reliably outlives us, and we exit through the clean window-close path.  The chosen
        language is read by the new instance from the just-(fsync'd) settings.json — durable and
        visible before we hand off — so it always applies."""
        import subprocess
        # Persist FIRST (durably) so the relaunched instance reads the new language/state.
        try:
            self._save_settings()
            self._save_mgr_state()
        except Exception:
            pass
        env = dict(os.environ)
        env["RUSE_MM_LANG"] = self._settings.get("default_language", "us")
        try:
            if getattr(sys, "frozen", False) and sys.platform == "win32":
                exe = str(Path(sys.executable).resolve())
                # Hand the launch to the running shell so the new instance is EXPLORER's child,
                # outside our job — nothing we own pins our _MEI temp dir, and it outlives our exit.
                subprocess.Popen(["explorer.exe", exe], close_fds=True)
            else:
                args = ([sys.executable] if getattr(sys, "frozen", False)
                        else [sys.executable, str(Path(__file__).resolve())])
                subprocess.Popen(args, cwd=str(_LAUNCH_DIR), close_fds=True, env=env)
        except Exception as e:
            ui_util.error(self, t("mgr.restart_failed"), str(e))
            return
        self.quit()

    # ── Shortcuts ─────────────────────────────────────────────────────────────
    def _add_desktop_shortcut(self):
        self._create_app_shortcut("Desktop")

    def _add_start_menu_shortcut(self):
        self._create_app_shortcut("Programs")   # WScript.Shell name for Start Menu\Programs

    def _create_app_shortcut(self, where: str):
        """Create a Windows .lnk to this app in the user's Desktop or Start-Menu Programs folder.
        ``where`` is a WScript.Shell SpecialFolders name ("Desktop" or "Programs").  Targets the
        packaged .exe when frozen; in a dev run it points pythonw at this script."""
        import subprocess, tempfile
        if getattr(sys, "frozen", False):
            target, args, icon = sys.executable, "", sys.executable
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            target = str(pyw if pyw.is_file() else sys.executable)
            args = f'"{Path(__file__).resolve()}"'
            icon = ""
        name = "R.U.S.E. Mod Manager"

        def psq(s):                              # single-quoted PowerShell literal
            return "'" + str(s).replace("'", "''") + "'"

        lines = [
            "$ws = New-Object -ComObject WScript.Shell",
            f"$dir = $ws.SpecialFolders.Item({psq(where)})",
            "if (-not $dir) { Write-Error 'Target folder not found'; exit 1 }",
            f"$lnk = $ws.CreateShortcut((Join-Path $dir {psq(name + '.lnk')}))",
            f"$lnk.TargetPath = {psq(target)}",
            f"$lnk.Arguments = {psq(args)}",
            f"$lnk.WorkingDirectory = {psq(str(_LAUNCH_DIR))}",
            f"$lnk.Description = {psq(name)}",
        ]
        if icon:
            lines.append(f"$lnk.IconLocation = {psq(icon)}")
        lines += ["$lnk.Save()", "Write-Output (Join-Path $dir " + psq(name + '.lnk') + ")"]

        ps1 = None
        try:
            fd, ps1 = tempfile.mkstemp(suffix=".ps1")
            os.close(fd)
            with open(ps1, "w", encoding="utf-8-sig") as f:   # BOM → PS 5.1 reads it as UTF-8
                f.write("\n".join(lines))
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if res.returncode == 0:
                made = (res.stdout or "").strip().splitlines()
                path = made[-1] if made else ""
                _log(self._mgr_log, t("mgr.created_shortcut_path_2", path=path), "ok")
                ui_util.info(self, t("mgr.shortcut_created"),
                                    t("mgr.created_shortcut_path", path=path) if path else t("mgr.shortcut_created_2"))
            else:
                err = (res.stderr or res.stdout or "Unknown error").strip()
                _log(self._mgr_log, t("mgr.shortcut_failed_err", err=err), "err")
                ui_util.error(self, t("mgr.shortcut_failed"), err)
        except Exception as e:
            ui_util.error(self, t("mgr.shortcut_failed"), str(e))
        finally:
            if ps1 and os.path.exists(ps1):
                try:
                    os.remove(ps1)
                except OSError:
                    pass

    def _set_browse_game_root(self):
        d = filedialog.askdirectory(
            parent=self,
            title=t("mgr.select_r_u_s_e"))
        if d: self._set_vars["game_root"].set(d)

    def _set_open_folder(self, key: str):
        """Open the auto-derived working/mods folder in the OS file explorer (view-only — these paths
        are not user-editable)."""
        raw = self._settings.get(key) or self._set_vars.get(key, tk.StringVar()).get()
        if not raw:
            return
        path = Path(raw)
        try:
            path.mkdir(parents=True, exist_ok=True)   # the mods folder may not exist until first use
        except Exception:
            pass
        if not path.is_dir():
            ui_util.error(self, t("mgr.not_found"), t("mgr.folder_does_not_exist_path", path=path))
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))               # Windows: open in Explorer
            else:
                ui_util.info(self, t("mgr.folder"), str(path))
        except Exception as e:
            ui_util.error(self, t("mgr.open_failed"), t("mgr.could_not_open_path_e", path=path, e=e))

    def _set_detect_game(self, silent: bool = False):
        """Detect R.U.S.E. installation via Steam and set game_root.

        silent=True: only updates when game_root is empty or the path no longer exists,
                     and silently prefers compat over public when both are found.
        silent=False (button): always runs, asks user if both versions are found.
        """
        try:
            dirs = _steam_mod.find_ruse_game_dirs()
        except Exception as e:
            if not silent:
                ui_util.error(self, t("mgr.detection_failed"), t("mgr.could_not_query_steam_e", e=e))
            return

        compat = dirs.get("compat")
        public = dirs.get("public")

        if not compat and not public:
            if not silent:
                ui_util.info(
                    self,
                    t("mgr.not_found"),
                    t("mgr.could_not_find_r_u"))
            return

        # In silent mode, only update when game_root is absent or gone
        if silent:
            current = self._settings.get("game_root", "").strip()
            if current and _is_dir_safe(Path(current)):
                return
            chosen = str(public or compat)
        elif compat and public:
            use_compat = ui_util.confirm(
                self,
                t("mgr.two_versions_found"),
                t("mgr.both_versions_r_u_s", compat=compat, public=public))
            chosen = str(compat) if use_compat else str(public)
        else:
            chosen = str(public or compat)

        # Update through the settings var so the trace fires the full save + mode-switch pipeline
        if hasattr(self, "_set_vars") and "game_root" in self._set_vars:
            self._set_vars["game_root"].set(chosen)
        else:
            self._settings["game_root"] = chosen
            self._save_settings()

    def _auto_detect_poll(self):
        """Every 15 s: silently fix a missing/gone game_root, AND auto-apply a BUILD switch.

        If the user changes the R.U.S.E. branch in Steam (same install path, new buildid in the
        appmanifest), the detected version key changes — so re-key the mod list / backups / labels to
        the new build automatically, no "Detect Game Version" press needed."""
        try:
            # Don't rescan/rebuild the UI while a deploy/backup/restore is writing game files — a
            # version-change rebuild mid-operation would race it.  Skip this tick; the next one catches up.
            if not self._mgr_running:
                self._set_detect_game(silent=True)
                gr = self._settings.get("game_root", "").strip()
                if gr and _is_dir_safe(Path(gr)):
                    new_ver = self._version_subname()
                    # Compare against the INSTALLED-build baseline, not _mgr_current_ver — the latter tracks
                    # the VIEWED build, which may be a user-selected non-installed one (viewing that must NOT
                    # trip a "game changed" refresh).
                    if self._last_installed_ver and new_ver != self._last_installed_ver:
                        # Guard a TRANSIENT build-id detection failure: when the Steam manifest is briefly
                        # unreadable, _version_subname() falls back to the format name (compat/public).
                        # Re-keying state to that non-build key would blank the list — and risk deploying
                        # only bundled mods — until detection recovers. If we already hold a real numeric
                        # build id, ignore a drop to a non-numeric key and wait for the next good tick.
                        cur_numeric = bool(re.match(r"^v\d+$", self._last_installed_ver))
                        new_numeric = bool(re.match(r"^v\d+$", new_ver))
                        if cur_numeric and not new_numeric:
                            pass                       # transient detection glitch — keep current build
                        else:
                            self._last_installed_ver = new_ver   # only advance on a real change we act on
                            _log(self._mgr_log,
                                 t("mgr.game_version_changed_label_refreshing",
                                   label=self._branch_label()), "head")
                            self._apply_version_change(new_ver)
        except Exception:
            pass
        self._auto_detect_job = self.after(15000, self._auto_detect_poll)

    def _set_schedule_save(self):
        if self._set_save_job:
            self.after_cancel(self._set_save_job)
        self._set_save_job = self.after(600, self._set_do_save)

    def _set_do_save(self):
        self._set_save_job = None
        old_game_root = self._settings.get("game_root", "")
        old_ver = self._mgr_current_ver or self._version_subname()
        for key, var in self._set_vars.items():
            self._settings[key] = var.get().strip()
        self._save_settings()
        self._set_status.set(t("mgr.settings_saved"))
        self.after(2000, lambda: self._set_status.set(""))
        self._mgr_refresh_status()
        self._profile_refresh_ui()
        self._update_mod_editor_tab()   # public-only editor — hide its tab if now compat
        new_game_root = self._settings.get("game_root", "")
        new_ver = self._version_subname()   # BUILD-ID key — detects build changes within one format too
        if new_game_root and new_game_root != old_game_root:
            self._set_auto_backup()
        if new_game_root and new_ver != old_ver:
            self._apply_version_change(new_ver)

    def _apply_version_change(self, new_ver: str):
        """Re-key everything to the game's current version/build: stash the outgoing build's mod list,
        re-scan and restore the NEW build's list, and refresh the version-dependent UI.  Shared by the
        Settings save path (_set_do_save) and the auto-detect poll (a Steam branch switch).  `new_ver`
        is the build-id key (e.g. 'v23762668')."""
        # Build (or format) changed.  Save the OUTGOING build's list FIRST, while the old bundled context
        # is still loaded (so bundled mods serialize by their old identity correctly)…
        self._save_mgr_state()
        # …then RE-SCAN the bundled + predeploy rmods for the NEW build id.  They are baked per build id;
        # without this a Steam branch switch would keep the OLD build's shipped mods (wrong-build data →
        # MP-integrity break / CTD, and they bypass the game_version gate) and silently miss the new
        # build's predeploy patches.
        # Clear any viewed-build override FIRST so the per-build scans below key off the NEW installed
        # build (they read _effective_mod_build), not a stale selection.
        self._selected_mod_build = None          # installed build changed → follow it
        self._last_installed_ver = new_ver       # keep the poll baseline in sync (also for the Settings path)
        self._scan_bundled()
        self._scan_predeploy()
        self._show_compat_var.set(False)
        if hasattr(self, "_mod_build_cb"):
            self._refresh_mod_build_cb()
        self._mgr_scan_both()
        self._mgr_load_mode(new_ver)
        self._cv_refresh_labels()
        self._mgr_refresh_status()
        self._maybe_suggest_mod_build()          # new installed build has no mods yet → hint at one that does
        self._profile_refresh_ui()
        self._update_mod_editor_tab()

    def _set_auto_backup(self):
        """Start a backup automatically when game root is newly set, if none exists yet."""
        # Fires from a Settings var trace, which can happen WHILE a deploy/backup/restore is running.
        # Without this guard it would start a second worker; whichever finishes first flips
        # ``_mgr_running`` off and re-enables Deploy over the still-running op — letting two operations
        # write the same game files at once.  Bail if the manager is already busy.
        if self._mgr_running:
            return
        gr = self._settings.get("game_root", "")
        if not gr:
            return
        game_root = Path(gr)
        if not _is_dir_safe(game_root / "Data"):
            return
        bd = self._backup_dir()
        if bd.exists() and any(bd.rglob("*.dat")):
            return  # existing backup for this version — don't overwrite silently
        ver_label = "R.U.S.E." if self._game_version() == "public" else "R.U.S.E. COMPAT"
        _log(self._mgr_log,
             t("mgr.game_root_configured_ver_label",
               ver_label=ver_label),
             "head")
        self._mgr_set_busy(True)
        try:
            self._mgr_show_backup_warn()
            threading.Thread(target=self._do_backup,
                             args=(game_root, bd), daemon=True).start()
        except Exception:
            self._mgr_set_busy(False)   # worker never started — don't leave the UI stuck-busy
            raise

    # ── Profile management ────────────────────────────────────────────────────

    def _profile_refresh_ui(self):
        """The lvl1/lvl100 presets are OG-compat career profiles, but older profiles upgrade forward —
        so the buttons work on ANY installed build (enabled whenever a game root is set).  The per-build
        profile restore is enabled when a backup exists for the detected build."""
        gr = self._settings.get("game_root", "").strip()
        state = "normal" if gr else "disabled"
        self._prof_lvl1_btn.configure(state=state)
        self._prof_lvl100_btn.configure(state=state)
        if hasattr(self, "_prof_set_backup_btn"):
            applicable = self._applicable_profile_backups()
            can_set = gr and bool(applicable)
            self._prof_set_backup_btn.configure(
                state="normal" if can_set else "disabled")
            if hasattr(self, "_prof_apply_cb"):
                # "Auto" + each populated applicable version (current build + older), newest-first.
                values = [_PROFILE_AUTO] + [lab for _, lab, _ in applicable]
                self._prof_apply_cb.configure(values=values)
                if self._prof_apply_choice.get() not in values:
                    self._prof_apply_choice.set(_PROFILE_AUTO)
                self._prof_apply_cb.configure(state="readonly" if can_set else "disabled")

    def _applicable_profile_backups(self) -> list:
        """Backed-up profiles that can be applied to the CURRENT build: the current build's and any
        OLDER build's (by release order). An older profile upgrades forward fine (the game rebuilds it
        and strips the legacy Ubisoft-account data on load); a NEWER build's profile may not work on an
        older game, so newer ones are excluded. Returns [(build_id, label, Path), ...] newest-first."""
        cur = self._game_build_id()
        tl = _migrate_mod.registry_timeline(include_og=True)   # oldest -> newest
        order = [e["buildid"] for e in tl]
        if cur in order:
            applicable = set(order[:order.index(cur) + 1])     # current + everything older
        else:
            applicable = {cur} if cur else set()
        out = []
        for e in tl:                                           # oldest -> newest
            if e["buildid"] not in applicable:
                continue
            p = _PROFILES_DIR / f"v{e['buildid']}" / "PROFILE.ruse"
            if p.is_file():
                out.append((e["buildid"], _gv_mod.display_name(e["buildid"]), p))
        out.reverse()                                          # newest-first (current build on top)
        return out

    def _profile_set_lvl(self, level: int):
        # The presets ARE the OG compat build's career profiles (profile/v3591-lvl<level>/).  Older
        # profiles upgrade forward (the game rebuilds them on launch), so they apply to ANY build.
        src = _PROFILES_DIR / f"{_OG_PROFILE_PREFIX}-lvl{level}" / "PROFILE.ruse"
        if not src.is_file():
            ui_util.error(self, t("mgr.profile_not_found"),
                                 t("mgr.preset_profile_not_found_src", src=src))
            return
        dirs = _find_steam_profile_dirs()
        if not dirs:
            ui_util.error(self, t("mgr.steam_not_found"),
                                 t("mgr.no_steam_r_u_s_2"))
            return
        copied, failed = [], []
        for d in dirs:
            try:
                shutil.copy2(src, d / "PROFILE.ruse")
                copied.append(str(d))
            except Exception as e:
                failed.append(f"{d}: {e}")
        msg = t("mgr.lvl_level_profile_deployed_copied", level=level) + \
              "\n".join(f"  {c}" for c in copied)
        if self._game_version() != "compat":
            msg += t("mgr.og_compat_preset_game_rebuilds",
                     cur=self._branch_label())
        if failed:
            msg += t("mgr.failed") + "\n".join(f"  {f}" for f in failed)
        ui_util.info(self, t("mgr.profile_set"), msg)

    def _profile_set_lvl1(self):
        self._profile_set_lvl(1)

    def _profile_set_lvl100(self):
        self._profile_set_lvl(100)

    def _profile_backup_current(self):
        dirs = _find_steam_profile_dirs()
        if not dirs:
            ui_util.error(self, t("mgr.steam_not_found"),
                                 t("mgr.no_steam_r_u_s"))
            return
        # Pick the most recently modified PROFILE.ruse across all Steam dirs
        src = None
        for d in dirs:
            p = d / "PROFILE.ruse"
            if p.is_file():
                if src is None or p.stat().st_mtime > src.stat().st_mtime:
                    src = p
        if src is None:
            ui_util.error(self, t("mgr.no_profile"),
                                 t("mgr.profile_ruse_not_found_any"))
            return
        ver = self._version_subname()          # per BUILD: profile/v<buildid>/ — archivable per version
        label = self._branch_label()
        backup_dir = _PROFILES_DIR / ver
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / "PROFILE.ruse"
        shutil.copy2(src, dest)
        ui_util.info(self, t("mgr.backup_complete"),
                            t("mgr.profile_backed_up_label_from", label=label, src=src, dest=dest))
        self._profile_refresh_ui()

    def _profile_set_backed_up(self):
        """Apply a backed-up profile to the current build. The dropdown chooses which: "Auto" (default)
        = the newest applicable backup — the current build's (applied silently) or, failing that, the
        most-recent OLDER one (with a confirm, since it's upgraded forward). Picking a specific version
        applies it directly. Newer builds' profiles are never offered."""
        options = self._applicable_profile_backups()      # newest-first (current build on top if present)
        if not options:
            ui_util.error(
                self,
                t("mgr.no_backup"),
                t("mgr.no_applicable_backed_up_profile",
                  label=self._branch_label()))
            return
        choice = self._prof_apply_choice.get() if hasattr(self, "_prof_apply_choice") else _PROFILE_AUTO
        if choice == _PROFILE_AUTO:
            build, label, src = options[0]                # newest applicable
            if build != self._game_build_id():
                # AUTO fell back to an OLDER profile — confirm the forward-upgrade
                if not ui_util.confirm(
                    self,
                    t("mgr.apply_older_profile"),
                    t("mgr.no_profile_backed_up_cur",
                      cur=self._branch_label(), label=label)):
                    return
        else:
            picked = next((o for o in options if o[1] == choice), None)
            if picked is None:
                ui_util.error(self, t("mgr.no_backup"),
                                     t("mgr.profile_version_no_longer_available"))
                return
            build, label, src = picked                    # explicit user pick — no confirm
        dirs = _find_steam_profile_dirs()
        if not dirs:
            ui_util.error(self, t("mgr.steam_not_found"),
                                 t("mgr.no_steam_r_u_s"))
            return
        copied, failed = [], []
        for d in dirs:
            try:
                shutil.copy2(src, d / "PROFILE.ruse")
                copied.append(str(d))
            except Exception as e:
                failed.append(f"{d}: {e}")
        note = "" if build == self._game_build_id() else t(
            "mgr.older_profile_game_rebuilds_cur",
            cur=self._branch_label())
        msg = (t("mgr.label_profile_deployed_copied", label=label)
               + "\n".join(f"  {c}" for c in copied) + note)
        if failed:
            msg += t("mgr.failed") + "\n".join(f"  {f}" for f in failed)
        ui_util.info(self, t("mgr.profile_set"), msg)

    # =========================================================================
    # Errors
    # =========================================================================

    def report_callback_exception(self, exc, val, tb):
        """Tk calls this for any exception raised inside an event callback (button handlers, ``after``
        jobs, nested ``wait_window`` loops).  The default just prints — and in a --noconsole frozen
        build stderr is None, so errors vanish and a broken callback can silently spin a nested loop
        with the main window still hidden ("running, no window").  Surface it as a dialog so it can't
        hide, and always dump the traceback for dev runs.  We do NOT tear the app down (a single bad
        callback shouldn't kill a working session), but the error is now visible."""
        import traceback
        try:
            sys.stderr.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass
        try:
            ui_util.error(self, t("mgr.something_went_wrong"),
                          t("mgr.unexpected_error_occurred_err", err=str(val)))
        except Exception:
            pass

    # =========================================================================
    # Close
    # =========================================================================

    def _ui(self, func):
        """Marshal `func` onto the UI thread from a worker, swallowing the RuntimeError/TclError that
        after() raises if the window is already tearing down (e.g. force-closed mid-operation).  Mirrors
        the guard the module-level _log() already uses for its cross-thread widget writes."""
        try:
            self.after(0, func)
        except (RuntimeError, tk.TclError):
            pass

    def _on_close(self):
        # If a modal popup is open (e.g. the language-change confirm), tear it down FIRST.  Its
        # ui_util.confirm runs a nested wait_window loop; self.quit() below can't return through that
        # loop while the popup still exists, so the X button would appear to "hang".  Destroying the
        # grabbed popup lets its loop unwind (it resolves to its cancel/No value) and the close proceed.
        g = self.grab_current()
        if g is not None and g is not self:
            try:
                g.grab_release()
            except Exception:
                pass
            try:
                g.destroy()
            except Exception:
                pass
        # A deploy/backup/restore/convert worker may be mid-write to the game files or a .rmod.  Closing
        # now kills the daemon thread mid-operation (a partial write / stale tracker) — make it deliberate.
        if getattr(self, "_mgr_running", False) or getattr(self, "_conv_running", False):
            if not ui_util.confirm(
                    self,
                    t("mgr.still_working"),
                    t("mgr.backup_deploy_restore_conversion_still")):
                return
        # Warn about mod-editor changes that were never saved into the mod
        if getattr(self, "_project", None) and self._project.is_dirty():
            if not ui_util.confirm(
                    self,
                    t("mgr.unsaved_mod_changes"),
                    t("mgr.mod_project_has_changes_weren")):
                return
        # Flush any pending debounced settings save before exit
        if getattr(self, "_set_save_job", None):
            self.after_cancel(self._set_save_job)
            self._set_save_job = None
            for key, var in self._set_vars.items():
                self._settings[key] = var.get().strip()
        # Cancel the recurring 15 s game-version poll so it can't fire against tearing-down widgets
        # (matters if a nested modal loop runs during shutdown).
        if getattr(self, "_auto_detect_job", None):
            try:
                self.after_cancel(self._auto_detect_job)
            except Exception:
                pass
            self._auto_detect_job = None
        self._save_settings()
        self._save_mgr_state()
        self.quit()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # MUST be the very first thing: the terrain decoder spawns a worker-process pool, and in a frozen
    # (PyInstaller) build each spawned worker re-launches THIS executable. freeze_support() detects that
    # re-launch and runs the worker instead of starting a second copy of the GUI — without it a big-map
    # decode would fork-bomb the app open. No-op when running from source. (terrain perf, issue #15)
    import multiprocessing
    multiprocessing.freeze_support()
    # Single-instance guard.  MUST come after freeze_support() (so terrain-decode worker processes,
    # which re-launch this exe, have already short-circuited and never reach here) but before any
    # window is built.  If another live instance already holds the lock we bow out silently — this is
    # what stops the "I launched once but got two windows" reports.  Fail-open on any error.
    try:
        import single_instance
        if not single_instance.acquire():
            sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        pass
    # Give Windows an explicit AppUserModelID so the taskbar treats us as our own app and uses the
    # window icon (set via iconphoto) for the taskbar button. Without this, a frozen build shows a
    # blank white taskbar icon because Windows groups us under the generic host-process identity.
    # Must run before ANY window (incl. the banlist tk.Tk below) is created. No-op off Windows.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RuseModManager.FieldOperations")
    except Exception:
        pass
    # Banlist gate: bundled exe only.  Refuses to load if any Steam account that
    # has signed in on this machine is listed in the baked-in banlist.txt.
    if getattr(sys, "frozen", False):
        import banlist
        _ban = banlist.check()
        if _ban is not None:
            _name, _reason = _ban
            _r = tk.Tk(); _r.withdraw()
            ui_util.error(
                _r,
                "Banned",
                f"{_name} has been banned!\nReason: {_reason}",
            )
            _r.destroy()
            sys.exit(1)
    # Guard the whole startup.  __init__ withdraws the window and only deiconifies at the very end, so
    # ANY unhandled error in between would otherwise leave a live-but-invisible process ("running in
    # Task Manager, no window").  Surface it as a dialog and exit non-zero instead of vanishing.
    try:
        app = ModManagerApp()
    except SystemExit:
        raise
    except Exception as _startup_err:
        import traceback
        _tb = traceback.format_exc()
        try:
            _r = tk.Tk(); _r.withdraw()
            ui_util.error(_r, t("mgr.startup_failed"),
                          t("mgr.mod_manager_couldn_t_finish", err=str(_startup_err)))
            _r.destroy()
        except Exception:
            pass
        try:
            sys.stderr.write(_tb)
        except Exception:
            pass
        sys.exit(1)
    app.mainloop()

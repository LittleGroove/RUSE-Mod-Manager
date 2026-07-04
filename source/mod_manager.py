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
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

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
_R_BG        = "#08101c"   # deep navy black — window bg
_R_BG_PANEL  = "#0e1a2a"   # navy blue — frame / panel bg
_R_BG_WIDGET = "#060d18"   # near-black navy — listbox / entry / log bg
_R_BORDER    = "#243a5c"   # steel blue border
_R_GOLD      = "#c8a020"   # military gold — primary accent
_R_GOLD_BRT  = "#e0c030"   # bright gold — headings / selected text
_R_RED       = "#b03020"   # danger red
_R_GREEN     = "#3a8030"   # success / OK green (status only)
_R_TEXT      = "#ccd8e8"   # metallic scratched silver-white — body text
_R_TEXT_DIM  = "#3e5878"   # muted steel blue — hint / secondary text
_R_SEL_BG    = "#1a3060"   # selection background
_R_SEL_FG    = "#e0c030"   # selection foreground
_R_BTN       = "#122030"   # button face
_R_BTN_ACT   = "#1e3250"   # button active / hover

# Log tag colors
_DARK_BG  = _R_BG_WIDGET
_COL_INFO = "#ccd8e8"
_COL_WARN = _R_GOLD
_COL_ERR  = "#cc3030"
_COL_OK   = "#4a9a38"
_COL_HEAD = _R_GOLD_BRT

# Fonts
_F_MAIN = ("Courier New", 9)
_F_BOLD = ("Courier New", 9,  "bold")
_F_HEAD = ("Courier New", 10, "bold")
_F_LOG  = ("Courier New", 9)

# Matches a numbered line of a shared load order:  1. Mod Name | v1.2.0
_LO_LINE = re.compile(r'^\d+\.\s+(.+?)(?:\s*\|\s*v?(.+))?$')


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
    w.tag_configure("warn", foreground=_COL_WARN)
    w.tag_configure("err",  foreground=_COL_ERR)
    w.tag_configure("ok",   foreground=_COL_OK)
    w.tag_configure("head", foreground=_COL_HEAD, font=_F_BOLD)
    w.configure(background=_DARK_BG, foreground=_COL_INFO,
                insertbackground=_R_GOLD)
    return w


def _log(widget, msg, tag="info"):
    _append_raw(widget, msg, tag)
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
        # Load the UI language catalog before any widgets are built (English-as-key with fallback, so
        # this is safe even if lang.json is missing).  Changing the language prompts a restart.
        i18n.load(self._settings.get("default_language", "us"))
        self.title(t("R.U.S.E. MOD MANAGER — Field Operations"))   # after i18n.load so it localizes
        if getattr(self, "_lang_autodetected", False):
            self._save_settings()   # record the OS-detected language so it persists from now on
        # In-exe auto-update.  If a newer release exists on GitHub, the user gets a Yes/No prompt:
        # Yes -> downloads and relaunches; No -> closes the app.  Skipped silently for dev runs,
        # offline users, and up-to-date builds.  Runs BEFORE the main UI is built so the user
        # doesn't see a half-built window flash when declining.
        import auto_update
        auto_update.run_startup_check(self)
        if not self._settings.get("game_root"):
            self._auto_detect_game_root()
        self._bootstrap_folders()
        self._apply_theme()
        self._mgr_running  = False
        self._conv_running = False
        self._mgr_mod_vars: list = []     # [(BooleanVar, path), ...] — ALL mods, always
        self._mgr_current_ver: str = ""   # BUILD-ID key (v<buildid>) of the currently loaded mod list
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
        self.after(15000, self._auto_detect_poll)
        self.deiconify()   # UI is fully built — show the window (paired with withdraw() at top of __init__)

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _bootstrap_folders(self):
        """Create required folders next to the exe on first run."""
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
        d.update(saved)
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
            ui_util.error(self, t("Save Error"), str(e))

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
        return out

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
            t("Backup required"),
            t("You need a clean backup of your game files before editing a mod.\n\n"
              "The editors load original files from this backup — never from your live game "
              "install, which may already contain mods.\n\n"
              "Go to the Mod Manager tab, set your Game Root if needed, then click "
              "“Create Backup”. Once that's done, you can open or create a mod project."))
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
                    size, mtime = "?", "?"
            parts.append(f"{pth.name}:{size}:{mtime}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _deploy_cache_dir(self, active: list) -> Path:
        """Folder holding the generated dats for one deployment, keyed by BUILD ID (like the mods
        folder): output/cached_deployments/v<buildid>/<hash>/.  Build-id keying (not the format
        branch) keeps caches distinct between two builds of the same format (e.g. compat-2 vs
        compat-3 vs public, all 'public' format), so a cache hit can't reuse one build's dats on
        another."""
        return (Path(self._settings["working_dir"]) / "output" / "cached_deployments"
                / self._version_subname() / self._deploy_cache_key(active))

    @staticmethod
    def _write_deploy_cache(cache_dir: Path, src_root: Path) -> int:
        """Snapshot the generated dats under src_root into cache_dir (replacing any prior contents).
        Returns the number of files cached, or -1 on failure."""
        try:
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            n = 0
            for src in src_root.rglob("*"):
                if src.is_file():
                    dest = cache_dir / src.relative_to(src_root)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    n += 1
            return n
        except Exception:
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

    def _bundled_saved_cache(self) -> dict:
        """{identity-string: cache-flag} for bundled mods from saved state — keyed by stable identity
        (their _MEIPASS path isn't stable across launches), mirroring _bundled_saved_enabled."""
        out = {}
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    st = json.load(f)
                for e in st.get("mods", []):
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
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    return list(json.load(f).get("deployed_dats", []))
            except Exception:
                pass
        return []

    def _mgr_set_deployed_dats(self, rels):
        """Persist the set of dat rel-paths currently overlaid onto the game (forward-slash form).
        Read-modify-write so the rest of the manager state is preserved."""
        existing = {}
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
        existing["deployed_dats"] = sorted(set(rels))
        try:
            with open(_MGR_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass

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
        bid = self._game_build_id()
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
            return [(t("R.U.S.E. COMPAT Mod files"), "*.compat.rmod"), (t("All files"), "*.*")]
        return [(t("R.U.S.E. Mod files"), "*.rmod"), (t("All files"), "*.*")]

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
            self._reset_convert_tab()

    # =========================================================================
    # MOD MANAGER TAB
    # =========================================================================

    def _build_manager_tab(self, p):
        pad = {"padx": 6, "pady": 3}

        # ── Setup checklist ───────────────────────────────────────────────────
        sf = ttk.LabelFrame(p, text=t("Setup Checklist  —  complete steps 1 and 2 before deploying mods"))
        sf.pack(fill="x", **pad)
        sf.columnconfigure(1, weight=1)

        ttk.Label(sf, text=t("Step 1"), font=_F_BOLD, foreground=_R_GOLD
                  ).grid(row=0, column=0, padx=(8, 4), pady=(6, 2), sticky="w")
        self._mgr_s1_lbl = tk.Label(sf, text=t("Checking…"),
                                    font=_F_LOG, background=_R_BG_PANEL,
                                    foreground=_R_GOLD, anchor="w")
        self._mgr_s1_lbl.grid(row=0, column=1, sticky="ew", padx=4, pady=(6, 2))
        ttk.Button(sf, text=t("Open Settings"),
                   command=lambda: self._nb.select(3)
                   ).grid(row=0, column=2, padx=6, pady=(6, 2))

        ttk.Label(sf, text=t("Step 2"), font=_F_BOLD, foreground=_R_GOLD
                  ).grid(row=1, column=0, padx=(8, 4), pady=(2, 6), sticky="w")
        self._mgr_s2_lbl = tk.Label(sf, text=t("Checking…"),
                                    font=_F_LOG, background=_R_BG_PANEL,
                                    foreground=_R_GOLD, anchor="w")
        self._mgr_s2_lbl.grid(row=1, column=1, sticky="ew", padx=4, pady=(2, 6))
        bbf = ttk.Frame(sf)
        bbf.grid(row=1, column=2, padx=6, pady=(2, 6))
        self._mgr_backup_btn = ttk.Button(bbf, text=t("Create Backup"),
                                          command=self._mgr_create_backup,
                                          state="disabled")
        self._mgr_backup_btn.pack(side="left", padx=2)
        self._mgr_restore_btn = ttk.Button(bbf, text=t("Restore Clean"),
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
        mf = ttk.LabelFrame(hpw, text=t("Mods  (☑ = active)"))
        hpw.add(mf, weight=2)

        tb = ttk.Frame(mf)
        tb.pack(fill="x", padx=4, pady=(4, 0))
        # Wrapping toolbar: in a narrow window the buttons flow onto a second row instead of the
        # right-hand ones (Share/Import Order) getting clipped off the edge (issue #5.3).
        # Import Order / Share Order are pinned RIGHT (they act on the whole order, not a selection);
        # the rest stay left-aligned.
        tb_btns = [
            ttk.Button(tb, text=t("Scan Mods Folder"), command=self._mgr_scan),
            ttk.Button(tb, text=t("Add .rmod…"), command=self._mgr_add),
            ttk.Button(tb, text=t("Remove Selected"), command=self._mgr_remove),
            ttk.Button(tb, text=t("Clear All"), command=self._mgr_clear),
            ttk.Button(tb, text=t("Browse rmods"), command=self._mgr_browse_mods),
        ]
        tb_btns_right = [
            ttk.Button(tb, text=t("Import Order…"), command=self._mgr_import_order),
            ttk.Button(tb, text=t("Share Order"), command=self._mgr_share_order),
        ]
        ui_util.flow(tb, tb_btns, right=tb_btns_right)

        ttk.Label(mf,
                  text=t("TOP loads first  —  BOTTOM overrides.  Use ▲ ▼ to reorder."),
                  font=_F_LOG, foreground=_R_GOLD,
                  ).pack(anchor="w", padx=6, pady=(4, 2))

        lf = ttk.Frame(mf)
        lf.pack(fill="both", expand=True, padx=4, pady=(0, 2))

        ob = ttk.Frame(lf)
        ob.pack(side="right", fill="y", padx=(2, 2), pady=2)
        ttk.Label(ob, text=t("earlier"), font=_F_LOG,
                  foreground=_R_TEXT_DIM).pack(pady=(2, 0))
        ttk.Button(ob, text="⇈", width=3, command=self._mgr_top).pack(fill="x")
        ttk.Button(ob, text="▲", width=3, command=self._mgr_up).pack(
            fill="x", pady=(2, 0))
        ttk.Button(ob, text="▼", width=3, command=self._mgr_down).pack(
            fill="x", pady=(2, 0))
        ttk.Button(ob, text="⇊", width=3, command=self._mgr_bottom).pack(
            fill="x", pady=(2, 0))
        ttk.Label(ob, text=t("later"), font=_F_LOG,
                  foreground=_R_TEXT_DIM).pack()
        self._compat_toggle_btn = tk.Button(
            ob, text=t("COMPAT  ○"), width=9,
            background=_R_BTN, foreground=_R_TEXT_DIM,
            activebackground=_R_BTN_ACT, activeforeground=_R_TEXT,
            relief="flat", font=_F_LOG,
            command=self._mgr_toggle_compat)

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
        self._mgr_update_btn = ttk.Button(eb, text=t("Update .rmod"),
                                          command=self._mgr_update_rmod, state="disabled")
        # 'Share Mod' opens GitHub's upload page for the selected external rmod, pointed at the exact
        # source/example_mods/v<buildid>/ folder build.py pulls community mods from — same single-
        # external-selection gate as 'Update .rmod' (see _mgr_update_btn_state).
        self._mgr_share_btn = ttk.Button(eb, text=t("📤  Share Mod"),
                                         command=self._mgr_share_mod, state="disabled")
        eb_btns = [
            ttk.Button(eb, text=t("☑  Enable Selected"), command=self._mgr_enable_selected),
            ttk.Button(eb, text=t("☐  Disable Selected"), command=self._mgr_disable_selected),
            ttk.Button(eb, text=t("All Off"), command=self._mgr_disable_all),
            self._mgr_update_btn,
            self._mgr_share_btn,
        ]
        ui_util.flow(eb, eb_btns)

        # ── Selected mod detail ───────────────────────────────────────────────
        df = ttk.LabelFrame(hpw, text=t("Selected Mod"))
        hpw.add(df, weight=1)
        self._mgr_detail_meta = tk.StringVar(value=t("Select a mod to see details."))
        ttk.Label(df, textvariable=self._mgr_detail_meta,
                  justify="left", font=_F_LOG).pack(padx=8, pady=(6, 2), anchor="w")
        self._mgr_detail_desc = _ThemedScrolledText(
            df, state="disabled", font=_F_MAIN,
            wrap="word", relief="flat",
            background=_R_BG_WIDGET, foreground=_R_TEXT,
            insertbackground=_R_GOLD)
        self._mgr_detail_desc.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        # ── Log ───────────────────────────────────────────────────────────────
        lf2 = ttk.LabelFrame(vpw, text=t("Log"))
        vpw.add(lf2, weight=1)
        self._mgr_log = _make_log(lf2)
        self._mgr_log.pack(fill="both", expand=True, padx=4, pady=4)
        # NOTE: this log shows ONLY Mod Manager activity.  Python-logging output (incl. Pillow's DEBUG)
        # and the cross-tab "all logs" view live in the Mod Editor's mirror windows — see
        # _build_mod_editor_tab, which attaches the logging handler to those.

        # ── Actions bar ───────────────────────────────────────────────────────
        af = ttk.Frame(p)
        self._mgr_dry = tk.BooleanVar(value=False)
        cb_dry = ttk.Checkbutton(af, text=t("Dry run (no files written)"),
                                 variable=self._mgr_dry)
        cb_dry.pack(side="left", padx=4)
        # Master switch (default ON): when off, never cache or reuse — always apply patches fresh.
        self._mgr_cache_enabled = tk.BooleanVar(value=self._mgr_saved_flag("cache_enabled", True))
        cb_cache = ttk.Checkbutton(af, text=t("Cache enabled"), variable=self._mgr_cache_enabled,
                                   command=self._save_mgr_state)
        cb_cache.pack(side="left", padx=4)
        # When ON (default), each rmod row shows a far-right "cache" toggle; only the prefixes ending at a
        # ticked mod are cached (controls disk).  When OFF, only the full deployment result is cached.
        self._mgr_per_mod_cache = tk.BooleanVar(value=self._mgr_saved_flag("per_mod_cache", True))
        cb_pmc = ttk.Checkbutton(af, text=t("Per-mod cache points"), variable=self._mgr_per_mod_cache,
                                 command=self._mgr_on_per_mod_cache_toggle)
        cb_pmc.pack(side="left", padx=4)
        # When OFF (default), reuse cached generated dats instead of re-applying patches; when ON, always
        # rebuild the dats (bypasses cache reuse) and re-cache them.
        self._mgr_regen = tk.BooleanVar(value=False)
        cb_regen = ttk.Checkbutton(af, text=t("Always regenerate dat files"),
                                   variable=self._mgr_regen)
        cb_regen.pack(side="left", padx=4)
        ttk.Button(af, text=t("Clear Log"),
                   command=lambda: _log_clear(self._mgr_log)).pack(
            side="right", padx=4)
        # Right cluster packs right→left, so packing Launch then Deploy renders them Deploy | Launch.
        self._mgr_launch_btn = ttk.Button(af, text=t("🎮  Launch R.U.S.E."),
                                          command=self._mgr_launch_game)
        self._mgr_launch_btn.pack(side="right", padx=4)
        self._mgr_deploy_btn = ttk.Button(af, text=t("▶  Deploy Mods"),
                                          command=self._mgr_deploy)
        self._mgr_deploy_btn.pack(side="right", padx=4)

        # Responsive: when the bar is too narrow to fit everything at full size, swap the
        # checkbox + Launch labels to short forms so the Deploy button is never pushed off.
        # (full label, short label) per widget; the relayout handler picks based on width.
        self._mgr_af = af
        self._mgr_af_compact = False
        self._mgr_af_full_req = 0
        self._mgr_af_labels = [
            (cb_dry,                t("Dry run (no files written)"), t("Dry run")),
            (cb_cache,              t("Cache enabled"),              t("Cache")),
            (cb_pmc,                t("Per-mod cache points"),       t("Per-mod")),
            (cb_regen,              t("Always regenerate dat files"), t("Regen")),
            (self._mgr_launch_btn,  t("🎮  Launch R.U.S.E."),         t("🎮  Launch")),
        ]
        af.bind("<Configure>", self._mgr_af_relayout)

        self._mgr_foot = tk.StringVar(value=t("Ready."))
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
            s1_text     = t("Done  —  {ver_label}  |  {game_root}", ver_label=ver_label, game_root=game_root)
            s1_text_set = s1_text
            s1_color    = _COL_OK
        elif game_root:
            s1_text = s1_text_set = t(
                "Game Root unreachable  —  {game_root}\nThe folder can't be found "
                "(its drive may have been removed).  Re-set the Game Root Directory in Settings.",
                game_root=game_root)
            s1_color = _COL_ERR
        else:
            s1_text     = t("Incomplete  —  Click 'Open Settings', set the Game Root Directory"
                            " to your R.U.S.E. install folder, then come back.")
            s1_text_set = t("Incomplete  —  Browse to your R.U.S.E. install folder"
                            " using the Game Root Directory field below.")
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
            s2_text  = t("Incomplete  —  Set the Game Root Directory first.")
            s2_color = _COL_ERR
        elif n > 0:
            s2_text  = t("Done  —  {ver_label}backup ready ({n} .dat files).  You can now deploy mods.",
                         ver_label=ver_label, n=n)
            s2_color = _COL_OK
        else:
            s2_text  = t("Incomplete  —  No {ver_label}backup found."
                         "  Click 'Create Backup' before deploying.", ver_label=ver_label)
            s2_color = _COL_ERR
        self._mgr_s2_lbl.configure(text=s2_text, foreground=s2_color)
        if hasattr(self, "_set_s2_lbl"):
            self._set_s2_lbl.configure(text=s2_text, foreground=s2_color)
        restore_state = "normal" if (n > 0 and not self._mgr_running) else "disabled"
        if hasattr(self, "_mgr_restore_btn"):
            self._mgr_restore_btn.configure(state=restore_state)
        if hasattr(self, "_set_restore_btn"):
            self._set_restore_btn.configure(state=restore_state)

        if hasattr(self, "_compat_toggle_btn"):
            if self._game_version() == "public":
                self._compat_toggle_btn.pack(fill="x", pady=(8, 0))
                self._mgr_update_compat_btn()
            else:
                self._compat_toggle_btn.pack_forget()
        self._cv_refresh_labels()

    def _cv_refresh_labels(self):
        """Update Convert/Create tab button labels and hints to reflect current game mode."""
        ext = self._rmod_ext()
        if hasattr(self, "_cv_btn"):
            self._cv_btn.configure(text=t("▶  Convert to {ext}", ext=ext))
        if hasattr(self, "_cr_load_btn"):
            self._cr_load_btn.configure(text=t("Load {ext} to Edit", ext=ext))
        if hasattr(self, "_cr_save_btn"):
            self._cr_save_btn.configure(text=t("Save as {ext}", ext=ext))

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

    def _mgr_toggle_compat(self):
        self._show_compat_var.set(not self._show_compat_var.get())
        self._mgr_update_compat_btn()
        if self._game_version() == "public":
            self._mgr_rebuild()

    def _mgr_update_compat_btn(self):
        if not hasattr(self, "_compat_toggle_btn"):
            return
        if self._show_compat_var.get():
            self._compat_toggle_btn.configure(
                text=t("COMPAT  ●"),
                foreground=_R_GOLD,
                background=_R_BTN_ACT)
        else:
            self._compat_toggle_btn.configure(
                text=t("COMPAT  ○"),
                foreground=_R_TEXT_DIM,
                background=_R_BTN)

    def _mgr_add_path(self, path: str):
        key = self._norm_path(path)
        if any(self._norm_path(ex) == key for _, ex in self._mgr_mod_vars):
            return
        # A SAFE (shipped) mod with the same name+major takes precedence — don't add a shadowed copy.
        if self._bundled_keys and self._rmod_identity(path) in self._bundled_keys:
            ui_util.info(
                self,
                t("Shipped (SAFE) mod takes precedence"),
                t("A SAFE mod with the same name and major version ships with the app and overrides this "
                  "one, so it wasn't added.\n\nChange its name or major version to add it as a separate mod."))
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

    def _mgr_lb_click(self, event):
        lb_idx  = self._mgr_lb.nearest(event.y)
        visible = self._visible_mv_indices()
        if not (0 <= lb_idx < len(visible)):
            return
        mv_idx = visible[lb_idx]
        bb = self._mgr_lb.bbox(lb_idx)
        if not bb:
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
        for var, _ in self._mgr_mod_vars:
            var.set(True)
        self._mgr_redraw_list()

    def _mgr_disable_all(self):
        for var, _ in self._mgr_mod_vars:
            var.set(False)
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
                t("Update .rmod"),
                t("This is a shipped (bundled) mod — it's baked into the exe and can't be updated externally."))
            return
        name = Path(path).name
        branch = "compat" if name.lower().endswith(".compat.rmod") else "public"
        root = self._clean_root_for_branch(branch)
        if root is None:
            ui_util.error(
                self,
                t("Update .rmod"),
                t("No clean {branch} originals available — create a {branch} backup (or set a matching Game Root) first.",
                  branch=branch))
            return
        self._conv_running = True
        self._mgr_update_btn.configure(state="disabled")
        _log(self._mgr_log, t("Updating {name} to the current format…", name=name), "head")

        def _work():
            def wf(m): self.after(0, lambda m=m: _log(self._mgr_log, f"  {m}", "warn"))
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
                    _log(self._mgr_log, t("ERROR: {e}", e=err), "err")
                tag = {"updated": "ok", "unchanged": "info",
                       "skipped": "warn", "failed": "err"}.get(status, "info")
                label = {"updated": t("updated"), "unchanged": t("unchanged"),
                         "skipped": t("skipped"), "failed": t("failed")}.get(status, status)
                _log(self._mgr_log, t("{name}: {label}", name=name, label=label), tag)
            self.after(0, _done)
        threading.Thread(target=_work, daemon=True).start()

    def _mgr_share_mod(self):
        """Share the SELECTED external rmod with the community by opening GitHub's 'upload files'
        page pointed at the exact source/example_mods/v<buildid>/ folder build.py pulls community
        mods from.  A browser can't be handed the file for the user (browser security), so we also
        reveal the .rmod in Explorer, highlighted, ready to drag onto the page.  GitHub does the
        rest: a signed-in user without push access is auto-forked and gets a 'Propose changes' pull
        request, which the PR template then fills in.  No git, no tokens, no folder-picking."""
        import webbrowser
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
                t("Share Mod"),
                t("“{name}” is already part of the built-in pack — no need to share it.", name=name))
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
                t("Share Mod"),
                t("Couldn't tell which game version this mod is for, so it can't be shared "
                  "automatically.\n\nMake sure the game is detected in Settings, then try again."))
            return
        try:
            label = _gv_mod.display_name(sub[1:])
        except Exception:
            label = sub

        url = (f"https://github.com/LittleGroove/RUSE-Mod-Manager/upload/main/"
               f"source/example_mods/{sub}")
        if not ui_util.confirm(
                self,
                t("Share Mod"),
                t("Share “{name}” with the community?\n\n"
                  "This opens GitHub in your web browser, on the upload page for the {label} folder. "
                  "Drag the mod file onto that page, then click “Propose changes” — GitHub creates the "
                  "request for you.\n\n"
                  "You'll need a free GitHub account and to be signed in. We'll also open the mod's "
                  "folder so it's ready to drag in.", name=name, label=label)):
            return

        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            ui_util.error(
                self,
                t("Share Mod"),
                t("Couldn't open your web browser. You can upload the mod here:\n{url}", url=url))
            return
        # Reveal the .rmod in Explorer, highlighted, so it's ready to drag onto the upload page.
        try:
            if sys.platform == "win32":
                subprocess.Popen(f'explorer.exe /select,"{os.path.normpath(path)}"')
        except Exception:
            pass
        _log(self._mgr_log,
             t("Sharing {name}: opened GitHub's upload page for the {label} folder. Drag the file in, "
               "then click “Propose changes”.", name=name, label=label), "info")

    def _mgr_on_select(self, _=None):
        self._mgr_update_btn_state()
        sel = self._mgr_lb.curselection()
        if not sel:
            self._mgr_detail_meta.set(t("Select a mod to see details."))
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
                for k, label in [("name", t("Name")), ("author", t("Author"))]
                if k in d
            )
            n = sum(len(pg.get("changes", [])) for pg in d.get("patches", []))
            ver   = t("Version: {version}", version=d['version']) if "version" in d else ""
            line2 = "   |   ".join(filter(None, [ver, t("NDF changes: {n}", n=n)]))
            self._mgr_detail_meta.set(f"{line1}\n{line2}" if line1 else line2)
            self._mgr_set_desc(d.get("description", ""))
        except Exception:
            self._mgr_detail_meta.set(t("File: {path}", path=path))
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
            ui_util.info(self, t("Nothing to Share"),
                                t("No mods are currently enabled.\n"
                                  "Enable at least one mod before sharing."))
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
            t("Load order ({n} active mod(s)) copied to clipboard — paste it to your friends!",
              n=len(active)))
        self.after(5000, lambda: self._mgr_foot.set(t("Ready.")))
        ui_util.show_text(
            self, t("Load Order Copied"),
            t("Your load order ({n} active mod(s)) has been copied to the clipboard.\n"
              "Paste it to a friend so they can match it. Here's what was copied:", n=len(active)),
            text)

    def _mgr_import_order(self):
        dlg = ui_util.themed_toplevel(
            self, t("Import Load Order from Friend"),
            resizable=True, min_size=(540, 500))

        ttk.Label(dlg,
                  text=t("Paste a friend's shared load order below, then click 'Check & Apply'.\n"
                         "The mod manager will verify you have all mods at the correct versions\n"
                         "and rearrange your list to match."),
                  foreground=_R_TEXT, justify="left", font=_F_LOG,
                  ).pack(padx=10, pady=(10, 4), anchor="w")

        txt_frame = ttk.LabelFrame(dlg, text=t("Paste Load Order Here"))
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

        res_frame = ttk.LabelFrame(dlg, text=t("Results"))
        res_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        res_log = _make_log(res_frame, height=7)
        res_log.pack(fill="both", expand=True, padx=4, pady=4)

        def do_paste():
            try:
                txt.delete("1.0", tk.END)
                txt.insert("1.0", self.clipboard_get())
            except Exception:
                pass

        ttk.Button(bf, text=t("Paste from Clipboard"),
                   command=do_paste).pack(side="left", padx=4)
        ttk.Button(bf, text=t("Check & Apply"),
                   command=lambda: self._mgr_do_import(
                       txt.get("1.0", tk.END).strip(), res_log)
                   ).pack(side="left", padx=4)
        ttk.Button(bf, text=t("Close"),
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
            m = _LO_LINE.match(stripped)
            if m:
                raw_name = m.group(1).strip()
                is_compat = raw_name.startswith("[COMPAT] ")
                name = raw_name[len("[COMPAT] "):] if is_compat else raw_name
                entries.append((name, (m.group(2) or "").strip(), is_compat))
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
            _log(log_widget, t("No valid load order entries found in the pasted text."), "err")
            return

        current_ver = self._game_version()
        if detected_mode and detected_mode != current_ver:
            _mode_label = {"compat": "R.U.S.E. COMPAT", "public": "R.U.S.E."}
            want = _mode_label.get(detected_mode, detected_mode)
            have = _mode_label.get(current_ver, current_ver)
            ui_util.warning(
                self,
                t("Wrong Game Version"),
                t("This load order was made for {want},\n"
                  "but your game is currently set to {have}.\n\n"
                  "Switch to {want} in Settings → Detect Game Version,\n"
                  "then try importing again.", want=want, have=have))
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
                     t("MISSING  {tag}{name}", tag=tag, name=name)
                     + (t("  (need v{req_ver})", req_ver=req_ver) if req_ver else ""),
                     "err")
                all_ok = False
                continue

            exact = [(v, p) for v, p in candidates if v == req_ver] if req_ver else candidates
            if exact:
                ver, path = exact[0]
                _log(log_widget,
                     t("OK       {tag}{name}", tag=tag, name=name)
                     + (t("  v{ver}", ver=ver) if ver else ""), "ok")
                matched_paths.append(path)
            else:
                ver, path = candidates[0]
                _log(log_widget,
                     t("VERSION  {tag}{name}  — you have v{ver}, need v{req_ver}",
                       tag=tag, name=name, ver=ver, req_ver=req_ver), "warn")
                all_ok = False
                matched_paths.append(path)

        if not matched_paths:
            _log(log_widget, t("\nNone of the mods in the order were found."), "err")
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
                 t("OFF      {stem}  (not in shared order)", stem=Path(path).stem), "info")

        self._ensure_bundled_in_list()   # dedup + re-inject any shipped mod somehow missing (invariant)

        # In public mode: if the order contains compat mods, auto-enable the
        # compat toggle so they become visible and aren't auto-disabled by rebuild.
        if (self._game_version() == "public"
                and not self._show_compat_var.get()
                and any(p.lower().endswith(".compat.rmod") for p in matched_paths)):
            self._show_compat_var.set(True)
            self._mgr_update_compat_btn()
            _log(log_widget, t("COMPAT toggle enabled — compat mods are now visible."), "info")

        self._mgr_rebuild()

        status = t("All mods matched.") if all_ok else t("Some mods missing or version mismatched — see above.")
        _log(log_widget,
             t("\nApplied: {n} mod(s) enabled in order.  {status}",
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
        # Bundled mods are baked per BUILD-ID folder (bundled_mods/v<buildid>/); load the set for
        # the installed build. (.compat.rmod also ends in .rmod, so one glob covers both formats.)
        bid = self._game_build_id()
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
                for e in st.get("mods", []):
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
        more = "" if len(pending) <= 12 else t("\n  …and {n} more", n=len(pending) - 12)
        ok = ui_util.confirm(
            self,
            t("Tag mods for {label}?", label=label),
            t("{n} mod(s) in this folder are tagged for a different version:\n\n{shown}{more}\n\n"
              "You placed them in the {label} folder, so they can be recorded as built for it. "
              "This PERMANENTLY updates each mod's version field. Tag them now?",
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
                pass
        if hasattr(self, "_mgr_log"):
            _log(self._mgr_log, t("Tagged {n} mod(s) as built for {label}.", n=n, label=label), "head")

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
        bid = self._game_build_id()

        def _is_for_build(f) -> bool:
            # No build id detected -> can't gate; show what's there. Otherwise require an exact
            # game_version match (stamped to this build), so mismatched/untagged mods are hidden.
            if not bid:
                return True
            try:
                return str(json.loads(f.read_text(encoding="utf-8-sig")).get("game_version", "")) == bid
            except Exception:
                return False

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
                ui_util.info(self, t("Folder"), str(base))
        except Exception as e:
            ui_util.error(self, t("Open Failed"),
                                 t("Could not open:\n{path}\n\n{e}", path=base, e=e))

    def _mgr_scan(self):
        """Scan both mod dirs, refresh caches, append any newly found mods at the bottom.
        This is the user-initiated scan, so it offers to tag legacy rmods for this build."""
        self._mgr_scan_both(prompt_stamp=True)
        before   = len(self._mgr_mod_vars)
        existing = {p for _, p in self._mgr_mod_vars}
        ver = self._game_version()
        scan_files = (self._scanned_compat if ver == "compat"
                      else list(self._scanned_public) + list(self._scanned_compat))
        for f in scan_files:
            path = str(f)
            if path not in existing:
                self._mgr_mod_vars.append((tk.BooleanVar(value=False), path))
                existing.add(path)
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
                title=f"Select {ext} files",
                filetypes=self._rmod_filter()):
            src = Path(p)
            dest = mods_dir / src.name
            if src.resolve() != dest.resolve():
                try:
                    mods_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dest))
                except Exception as e:
                    ui_util.error(self, t("Copy Failed"),
                        t("Could not copy {name} to mods folder:\n{e}", name=src.name, e=e))
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
        if not self._settings["game_root"]:
            ui_util.error(self, t("No Game Root"), t("Set Game Root in Settings first."))
            return
        game_root = Path(self._settings["game_root"])
        if not _is_dir_safe(game_root / "Data"):
            ui_util.error(self, t("Not Found"), t("Data folder not found:\n{path}", path=game_root / 'Data'))
            return
        bd = self._backup_dir()
        has_files = bd.exists() and any(bd.rglob("*.dat"))
        if has_files:
            if not ui_util.confirm(self, t("Overwrite?"),
                                       t("Backup already exists:\n{bd}\n\nOverwrite?", bd=bd)):
                return
            shutil.rmtree(bd)
        self._mgr_set_busy(True)
        self._mgr_show_backup_warn()
        threading.Thread(target=self._do_backup,
                         args=(game_root, bd), daemon=True).start()

    def _do_backup(self, game_root: Path, bd: Path):
        # Mirror the game ROOT into the backup: Data/PC/<sub>/<dat> and Maps/PC/<terrain dats>.  The Maps
        # folder (per-terrain world/minimap dats) is large, so this can take a while.
        try:
            _log(self._mgr_log, t("Creating backup: {game_root} → {bd}", game_root=game_root, bd=bd), "head")
            bd.mkdir(parents=True, exist_ok=True)
            count = 0
            for top in ("Data", "Maps"):
                src_root = game_root / top
                if not src_root.is_dir():
                    continue
                for src in src_root.rglob("*"):
                    if src.is_file():
                        rel  = src.relative_to(game_root)     # e.g. Data\PC\190852\X.dat, Maps\PC\Y.dat
                        dest = bd / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        count += 1
                        _log(self._mgr_log, f"  {rel}", "info")
            _log(self._mgr_log, t("Backup complete: {count} files.", count=count), "ok")
        except Exception as e:
            _log(self._mgr_log, t("Backup error: {e}", e=e), "err")
        finally:
            self.after(0, self._mgr_refresh_status)
            self.after(0, lambda: self._mgr_set_busy(False))

    def _mgr_restore_clean(self):
        bd = self._backup_dir()
        if not bd.exists():
            ui_util.error(self, t("No Backup"), t("No backup found. Create one first."))
            return
        if not self._settings["game_root"]:
            ui_util.error(self, t("No Game Root"), t("Set Game Root in Settings first."))
            return
        if not ui_util.confirm(self, t("Restore Clean"),
                                   t("Copy original backup files back into the game (Data and Maps),\n"
                                     "removing any deployed mods.\n\nProceed?")):
            return
        self._mgr_set_busy(True)
        threading.Thread(target=self._do_restore,
                         args=(bd, Path(self._settings["game_root"])), daemon=True).start()

    def _do_restore(self, bd: Path, game_root: Path):
        # The backup mirrors the game root (Data/… and Maps/…), so copy those subtrees back over the
        # install.  Only Data/ and Maps/ are restored — stray files at the backup root (e.g. deploy-time
        # timestamped .bak copies) are deliberately ignored.
        try:
            _log(self._mgr_log, t("Restoring clean game files…"), "head")
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
                        shutil.copy2(src, dest)
                        count += 1
            mod_out = self._mod_out_dir()
            if mod_out.exists():
                shutil.rmtree(mod_out)
            self._mgr_set_deployed_dats([])   # game is fully clean now — nothing left deployed to track
            _log(self._mgr_log, t("Restored {count} files — game is clean.", count=count), "ok")
            self.after(0, lambda: self._mgr_foot.set(t("Game restored to clean state.")))
        except Exception as e:
            _log(self._mgr_log, t("Restore error: {e}", e=e), "err")
        finally:
            self.after(0, lambda: self._mgr_set_busy(False))

    # ── Deploy ────────────────────────────────────────────────────────────────

    def _mgr_deploy(self):
        if self._mgr_running:
            return
        if not self._settings["game_root"]:
            ui_util.error(self, t("No Game Root"), t("Set Game Root in Settings first."))
            return
        bd = self._backup_dir()
        if not bd.exists() or not any(bd.rglob("*.dat")):
            ui_util.error(self, t("No Backup"),
                                 t("No game file backup found.\n\n"
                                   "Click 'Create Backup' (Step 2 in the setup checklist)\n"
                                   "before deploying mods."))
            return
        active = [p for v, p in self._mgr_mod_vars if v.get()]
        # Auto-deployed 'unofficial patch' rmods baked into the exe (predeploy/<branch>/) — always
        # applied FIRST so the user's mods (and any SAFE-bundled mods) layer on top of them.  Invisible
        # in the list; matched to the currently-detected game branch.  See _scan_predeploy.
        predeploy = list(self._predeploy_order.get(self._game_build_id(), []))
        active = predeploy + active
        if not active:
            ui_util.info(self, t("No Active Mods"),
                                t("Check at least one mod to deploy."))
            return
        dry = self._mgr_dry.get()
        self._mgr_set_busy(True)
        self._mgr_foot.set(t("Deploying mods…"))
        threading.Thread(target=self._do_deploy,
                         args=(bd, active, dry), daemon=True).start()

    def _mgr_launch_game(self):
        """Launch RUSE.exe from the configured game root."""
        if self._mgr_running:
            return
        game_root = self._settings.get("game_root", "").strip()
        if not game_root:
            ui_util.error(self, t("No Game Root"), t("Set Game Root in Settings first."))
            return
        exe = Path(game_root) / "RUSE.exe"
        if not _exists_safe(exe):
            ui_util.error(
                self,
                t("RUSE.exe Not Found"),
                t("Could not find RUSE.exe in the game root:\n\n{game_root}\n\n"
                  "Check that the Game Root Directory in Settings points to your "
                  "R.U.S.E. install folder.", game_root=game_root))
            return
        try:
            subprocess.Popen([str(exe)], cwd=game_root)
            _log(self._mgr_log, t("Launched R.U.S.E.  ({exe})", exe=exe), "ok")
            self._mgr_foot.set(t("Launched R.U.S.E."))
        except Exception as ex:
            _log(self._mgr_log, t("Failed to launch R.U.S.E.: {ex}", ex=ex), "err")
            ui_util.error(self, t("Launch Failed"), t("Could not launch R.U.S.E.:\n\n{ex}", ex=ex))

    def _do_deploy(self, bd: Path, active: list, dry: bool):
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
            cache_on = bool(self._mgr_cache_enabled.get())
            per_mod  = bool(self._mgr_per_mod_cache.get())
            regen    = bool(self._mgr_regen.get())
            prefix_base = None      # cache dir whose dats already have mods[0:k] applied
            tail = list(active)     # mods still to apply on top of the base
            if cache_on and (not dry) and (not regen):
                for k in range(len(active), 0, -1):
                    cdir = self._deploy_cache_dir(active[:k])
                    if cdir.is_dir() and any(cdir.rglob("*.dat")):
                        prefix_base, tail = cdir, active[k:]
                        break
            reused = len(active) - len(tail)
            exact  = (prefix_base is not None and not tail)

            _log(self._mgr_log,
                 t("{prefix}Deploying {n} mod(s)  [{ver_label}]  "
                   "source={bd}  output={mod_out}",
                   prefix=prefix, n=len(active), ver_label=ver_label, bd=bd, mod_out=mod_out), "head")
            if prefix_base is not None:
                _log(self._mgr_log,
                     (t("Cache hit: all {reused} mod(s) reused from {name}… — no patching.",
                        reused=reused, name=prefix_base.name[:12])
                      if exact else
                      t("Cache hit: {reused}/{n} mod(s) reused from {name}… — "
                        "applying the remaining {tail} on top.",
                        reused=reused, n=len(active), name=prefix_base.name[:12], tail=len(tail))), "ok")

            all_results = []
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
                        for src in prefix_base.rglob("*"):
                            if src.is_file():
                                dest = mod_out / src.relative_to(prefix_base)
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(src, dest)
                for i, mod_path in enumerate(tail):
                    _log(self._mgr_log, t("\n── {name} ──", name=Path(mod_path).name), "head")
                    ok = False
                    try:
                        result = applier_mod.apply_mod(
                            mod_path      = mod_path,
                            game_data_dir = str(bd),
                            backup        = False,
                            dry_run       = dry,
                            output_dir    = str(mod_out) if not dry else None,
                            game_version  = game_ver,
                        )
                        all_results.append(result)
                        for rec in result.change_log:
                            _log(self._mgr_log,
                                 f"  {rec.table}[{rec.instance_id}].{rec.prop}: "
                                 f"{rec.old_val} → {rec.new_val}", "ok")
                        for w in result.warnings:
                            _log(self._mgr_log, t("  WARN  {w}", w=w), "warn")
                        for e in result.errors:
                            _log(self._mgr_log, t("  ERROR {e}", e=e), "err")
                        _log(self._mgr_log, f"  {result.summary()}", "info")
                        ok = True
                    except Exception as ex:
                        _log(self._mgr_log, t("  ERROR: {ex}", ex=ex), "err")

                    # Cache the cumulative result for the prefix ending at this mod (active[:reused+i+1]),
                    # so it never has to be re-applied.  Per-mod mode → only when THIS mod is ticked for
                    # caching; otherwise → only the full result (the last mod).
                    gpos = reused + i           # this mod's index in the full active list
                    want_cache = (self._mod_cache_var(active[gpos]).get() if per_mod
                                  else i == len(tail) - 1)
                    if cache_on and want_cache and ok and not dry and any(mod_out.rglob("*.dat")):
                        plen = gpos + 1
                        pc = self._deploy_cache_dir(active[:plen])
                        n = self._write_deploy_cache(pc, mod_out)
                        if n >= 0:
                            _log(self._mgr_log,
                                 t("  cached {plen}/{n}-mod prefix → {name}…",
                                   plen=plen, n=len(active), name=pc.name[:12]), "info")
                        else:
                            _log(self._mgr_log, t("  WARN could not cache prefix {plen}", plen=plen), "warn")

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
                        pass

                # Only restore the LEFTOVERS — dats a PREVIOUS deploy modified that this mod list does
                # NOT touch.  The dats it DOES touch are rebuilt from the clean backup and fully
                # overwritten by the overlay below, so cleaning them first would be redundant work.
                prev = {r.replace("/", os.sep) for r in self._mgr_saved_deployed_dats()}
                restore_targets = sorted(prev - touched)
                _log(self._mgr_log,
                     t("\nRestoring {n} leftover file(s) from a previous deploy to "
                       "clean state…", n=len(restore_targets)), "head")
                restored = 0
                for dat_rel in restore_targets:
                    src = bd / dat_rel
                    dest = game_root / dat_rel
                    if src.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        restored += 1
                        _log(self._mgr_log, t("  restored: {dat_rel}", dat_rel=dat_rel), "info")
                    else:
                        # a previous-deploy dat with no clean backup (e.g. branch changed) — can't revert
                        _log(self._mgr_log, t("  WARN no clean backup to restore: {dat_rel}", dat_rel=dat_rel), "warn")
                _log(self._mgr_log, t("Restored {restored} file(s).", restored=restored), "ok")

                _log(self._mgr_log, t("Overlaying modded files…"), "head")
                overlay = 0
                overlaid: set = set()       # the dats now non-clean in the game (persist for next deploy)
                for src in gen_root.rglob("*"):
                    if src.is_file():
                        rel  = src.relative_to(gen_root)
                        dest = game_root / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest)
                        overlay += 1
                        overlaid.add(str(rel).replace(os.sep, "/"))
                _log(self._mgr_log, t("Deployed {overlay} modded file(s) to game.", overlay=overlay), "ok")
                # Record exactly what's now overlaid so the next deploy can clean it even if its mod
                # list doesn't touch these dats.
                self._mgr_set_deployed_dats(overlaid)

            if exact:
                summary = t("Done — deployed {n} cached file(s) (no patching)",
                            n=sum(1 for f in gen_root.rglob('*') if f.is_file()))
            else:
                total_ch = sum(r.changes_applied for r in all_results)
                total_w  = sum(len(r.warnings)   for r in all_results)
                total_e  = sum(len(r.errors)     for r in all_results)
                summary  = (t("Done — {total_ch} change(s), {total_w} warning(s), {total_e} error(s)",
                              total_ch=total_ch, total_w=total_w, total_e=total_e)
                            + (t("  ({reused} of {n} mod(s) reused from cache)",
                                 reused=reused, n=len(active)) if reused else ""))
            _log(self._mgr_log, f"\n{summary}", "head")
            self.after(0, lambda: self._mgr_foot.set(summary))
        except Exception as ex:
            _log(self._mgr_log, t("\nDeploy error: {ex}", ex=ex), "err")
            self.after(0, lambda: self._mgr_foot.set(t("Deploy failed — see log.")))
        finally:
            self.after(0, lambda: self._mgr_set_busy(False))

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

    def _mgr_show_backup_warn(self):
        ver_label = " [R.U.S.E. COMPAT]" if self._game_version() == "compat" else ""
        text = t("Backup in progress{ver_label}…", ver_label=ver_label)
        self._mgr_s2_lbl.configure(text=text, foreground=_COL_WARN)
        if hasattr(self, "_set_s2_lbl"):
            self._set_s2_lbl.configure(text=text, foreground=_COL_WARN)

    # ── Manager state persistence ─────────────────────────────────────────────

    def _load_mgr_state(self):
        # Always warm the caches so the toggle works immediately
        self._mgr_scan_both()
        self._mgr_load_mode(self._version_subname())   # mode key = build id (not format)

    def _save_mgr_state(self):
        """Save the CURRENT build's mod list (active set + ORDER) under its build-id key, so every game
        build keeps its OWN independent list.  Cache toggles + the deploy tracker stay global; the mod
        list is per build (`builds: {v<buildid>: [...]}`)."""
        existing = {}
        if _MGR_STATE_FILE.exists():
            try:
                with open(_MGR_STATE_FILE, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
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
        try:
            with open(_MGR_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass

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
            var = tk.BooleanVar(value=bool(entry.get("enabled", False)))
            self._mgr_mod_vars.append((var, p))
            self._mgr_cache_flags[str(p)] = tk.BooleanVar(value=bool(entry.get("cache", False)))
        # Append any newly discovered mods not in saved state.  `ver` is the build-id mode key now,
        # so the compat-vs-remaster split keys off the FORMAT (_game_version()), not `ver`.
        existing = {p for _, p in self._mgr_mod_vars}
        scan_files = (self._scanned_compat if self._game_version() == "compat"
                      else list(self._scanned_public) + list(self._scanned_compat))
        for f in scan_files:
            path = str(f)
            if path not in existing:
                self._mgr_mod_vars.append((tk.BooleanVar(value=False), path))
        self._ensure_bundled_in_list()   # shipped mods always present, on top, enabled
        self._mgr_rebuild()

    # =========================================================================
    # CONVERT TAB
    # =========================================================================

    def _build_convert_tab(self, p):
        pad  = {"padx": 6, "pady": 3}
        padg = {"padx": 6, "pady": 3}

        # ── Directories ───────────────────────────────────────────────────────
        df = ttk.LabelFrame(p, text=t("Directories"))
        df.pack(fill="x", **pad)
        df.columnconfigure(1, weight=1)

        ttk.Label(df, text=t("Mod Folder:")).grid(row=0, column=0, sticky="e", **padg)
        self._cv_mod = tk.StringVar()
        ttk.Entry(df, textvariable=self._cv_mod).grid(
            row=0, column=1, sticky="ew", **padg)
        ttk.Button(df, text=t("Browse…"),
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
        ttk.Label(df, text=t("Make mod for version:")).grid(row=2, column=0, sticky="e", **padg)
        self._cv_build = tk.StringVar()
        self._cv_build_cb = ttk.Combobox(df, textvariable=self._cv_build, state="readonly",
                                         values=self._cv_build_choices(), width=26)
        self._cv_build_cb.grid(row=2, column=1, sticky="w", **padg)
        _det_bid = self._game_build_id()
        if _det_bid:
            self._cv_build.set(_gv_mod.display_name(_det_bid))
        elif self._cv_build_cb["values"]:
            self._cv_build.set(self._cv_build_cb["values"][-1])

        # ── Main body: vertical paned ─────────────────────────────────────────
        vpw = ttk.PanedWindow(p, orient=tk.VERTICAL)
        vpw.pack(fill="both", expand=True, padx=6, pady=3)

        # Top pane — horizontal: mod info+changes (left) | description (right)
        hpw = ttk.PanedWindow(vpw, orient=tk.HORIZONTAL)
        vpw.add(hpw, weight=2)

        # ── Left: Mod Info form + Detected Changes listbox ────────────────────
        left = ttk.LabelFrame(hpw, text=t("Mod Info"))
        hpw.add(left, weight=1)
        left.columnconfigure(1, weight=1)
        left.rowconfigure(5, weight=1)

        # Name + the auto-appended "_V#" file-name suffix (driven by the major version below).
        ttk.Label(left, text=t("Name:")).grid(row=0, column=0, sticky="e", **padg)
        self._cv_name = tk.StringVar()
        self._cv_name.trace_add("write", lambda *_: self._cv_update_preview())
        ne = ttk.Entry(left, textvariable=self._cv_name)
        ne.grid(row=0, column=1, columnspan=2, sticky="ew", **padg)
        ne.bind("<FocusOut>", self._cv_auto_id)
        self._cv_name_suffix = tk.StringVar()
        ttk.Label(left, textvariable=self._cv_name_suffix, foreground=_R_GOLD,
                  font=_F_BOLD).grid(row=0, column=3, sticky="w", padx=(0, 8))

        # ID + the auto-appended "-v#" id suffix.
        ttk.Label(left, text=t("ID:")).grid(row=1, column=0, sticky="e", **padg)
        self._cv_id = tk.StringVar()
        ttk.Entry(left, textvariable=self._cv_id).grid(
            row=1, column=1, columnspan=2, sticky="ew", **padg)
        self._cv_id_suffix = tk.StringVar()
        ttk.Label(left, textvariable=self._cv_id_suffix, foreground=_R_GOLD,
                  font=_F_BOLD).grid(row=1, column=3, sticky="w", padx=(0, 8))

        # Version — its MAJOR number becomes the _V# / -v# suffix above.
        ttk.Label(left, text=t("Version:")).grid(row=2, column=0, sticky="e", **padg)
        self._cv_ver = tk.StringVar(value="1.0.0")
        self._cv_ver.trace_add("write", lambda *_: self._cv_refresh_suffix())
        cv_ver_ent = ttk.Entry(left, textvariable=self._cv_ver, width=8)
        cv_ver_ent.grid(row=2, column=1, sticky="w", **padg)
        # On leaving the field, snap a non-conforming entry (blank, "v1.0", free text) back to x.x.x.
        cv_ver_ent.bind("<FocusOut>",
                        lambda *_: self._cv_ver.set(_normalize_version(self._cv_ver.get())))
        ttk.Label(left, text=t("major # → auto-added as _V# (file) / -v# (id)"),
                  foreground=_R_TEXT_DIM).grid(row=2, column=2, columnspan=2, sticky="w", padx=4)

        ttk.Label(left, text=t("Author:")).grid(row=3, column=0, sticky="e", **padg)
        self._cv_author = tk.StringVar()
        ttk.Entry(left, textvariable=self._cv_author).grid(
            row=3, column=1, columnspan=3, sticky="ew", **padg)

        self._cv_preview = tk.StringVar()
        ttk.Label(left, textvariable=self._cv_preview,
                  foreground=_R_TEXT_DIM).grid(
            row=4, column=1, columnspan=3, sticky="w", padx=6, pady=(0, 2))

        det = ttk.LabelFrame(left, text=t("Detected .dat Files"))
        det.grid(row=5, column=0, columnspan=4, sticky="nsew", padx=6, pady=(2, 6))
        det.columnconfigure(0, weight=1)
        det.rowconfigure(1, weight=1)

        sb2 = ttk.Frame(det)
        sb2.pack(fill="x", pady=(4, 0), padx=4)
        ttk.Button(sb2, text=t("Scan for Changes"),
                   command=self._cv_scan).pack(side="left", padx=2)
        self._cv_btn = ttk.Button(sb2, text=t("▶  Convert"), command=self._cv_convert)
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
        right = ttk.LabelFrame(hpw, text=t("Description"))
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

        # ── Migrate an existing rmod to EVERY game version (forward + backward) ──────
        tf = ttk.LabelFrame(bot, text=t("Migrate a mod to every game version"))
        tf.pack(fill="x", **pad)
        tf.columnconfigure(1, weight=1)

        ttk.Label(tf, text=t("Source version:")).grid(row=0, column=0, sticky="e", **padg)
        self._mig_ver = tk.StringVar()
        self._mig_ver_cb = ttk.Combobox(tf, textvariable=self._mig_ver, state="readonly",
                                        values=self._cv_build_choices(), width=26)
        self._mig_ver_cb.grid(row=0, column=1, sticky="w", **padg)
        self._mig_ver_cb.bind("<<ComboboxSelected>>", lambda *_: self._mig_refresh_rmods())

        ttk.Label(tf, text=t("rmod:")).grid(row=1, column=0, sticky="e", **padg)
        self._mig_src = tk.StringVar()
        self._mig_rmod_cb = ttk.Combobox(tf, textvariable=self._mig_src, state="readonly")
        self._mig_rmod_cb.grid(row=1, column=1, sticky="ew", **padg)
        ttk.Button(tf, text=t("Browse…"), command=self._mig_browse).grid(row=1, column=2, **padg)
        ttk.Label(tf, text=t("  Converts the selected rmod to every OTHER game version that has a shipped "
                             "mapping (forward + backward), writing each into its mods/v<build>/ folder."),
                  foreground=_R_TEXT_DIM, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))
        af2 = ttk.Frame(tf)
        af2.grid(row=3, column=0, columnspan=3, sticky="ew", **padg)
        self._tr_btn = ttk.Button(af2, text=t("▶  Convert to all versions"),
                                  command=self._mig_convert_all)
        self._tr_btn.pack(side="right", padx=4)
        self._tr_status = tk.StringVar()
        ttk.Label(af2, textvariable=self._tr_status,
                  foreground=_R_TEXT_DIM).pack(side="left", padx=4)
        # default the source version to the detected install + list its mods
        if _det_bid:
            self._mig_ver.set(_gv_mod.display_name(_det_bid))
        elif self._mig_ver_cb["values"]:
            self._mig_ver.set(self._mig_ver_cb["values"][-1])
        self.after(200, self._mig_refresh_rmods)

        # Pin the footer to the bottom of this pane FIRST (side=bottom, packed before the
        # log) so Tk clips the log — not the status line — when the pane gets short.
        self._cv_foot = tk.StringVar(value=t("Ready."))
        ttk.Label(bot, textvariable=self._cv_foot, anchor="w").pack(
            side=tk.BOTTOM, fill="x", padx=6, pady=(0, 4))

        lf2 = ttk.LabelFrame(bot, text=t("Log"))
        lf2.pack(side=tk.TOP, fill="both", expand=True, **pad)
        self._cv_log = _make_log(lf2)
        self._cv_log.pack(fill="both", expand=True, padx=4, pady=4)

        self._cv_refresh_suffix()   # seed the _V# / -v# hints from the default version

    def _reset_convert_tab(self):
        self._cv_mod.set("")
        self._cv_name.set("")
        self._cv_id.set("")
        self._cv_ver.set("1.0.0")
        self._cv_author.set("")
        self._cv_desc.delete("1.0", tk.END)
        self._cv_preview.set("")
        self._cv_lb.delete(0, tk.END)
        self._cv_scan_status.set("")
        self._mig_src.set("")
        self._tr_status.set("")
        _log_clear(self._cv_log)
        self._cv_foot.set(t("Ready."))


    def _cv_browse_mod(self):
        d = filedialog.askdirectory(
            title=t("Select mod ROOT (mirrors the game: Data\\PC\\<ver>\\…, Maps\\PC\\…)"))
        if not d:
            return
        self._cv_mod.set(d)
        name = Path(d).name
        if not self._cv_name.get():
            self._cv_name.set(name)
        if not self._cv_id.get():
            self._cv_id.set(_name_to_id(name))
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
            if btn: btn.configure(text=t("▶  Convert to .compat.rmod"))
            self._cv_mode.set(t("Detected:  R.U.S.E. COMPAT mod  →  .compat.rmod   ·   saved to  "
                                "{dest}", dest=self._cv_dest_dir(mod_folder)))
        elif ver == "public":
            if btn: btn.configure(text=t("▶  Convert to .rmod"))
            self._cv_mode.set(t("Detected:  R.U.S.E. (public) mod  →  .rmod   ·   saved to  "
                                "{dest}", dest=self._cv_dest_dir(mod_folder)))
        elif mod_folder:
            if btn: btn.configure(text=t("▶  Convert"))
            self._cv_mode.set(t("⚠  No 99/, 1360/ or 190852/ version folder found — pick the mod's ROOT. "
                                "It should mirror the game root, e.g. "
                                "mod\\Data\\PC\\190852\\ZZ_GladPatchableWin.dat (and mod\\Maps\\PC\\… for "
                                "terrain).  Older flat PC\\ or loose layouts still convert."))
        else:
            if btn: btn.configure(text=t("▶  Convert"))
            self._cv_mode.set(t("Select a mod ROOT (mirrors the game root: Data\\PC\\<ver>\\…, Maps\\PC\\… "
                                "for terrain).  Branch is auto-detected from the version folder "
                                "(99 or 1360 = COMPAT → .compat.rmod;  190852 = public → .rmod)."))
        self._cv_update_preview()

    def _cv_major(self) -> str:
        """The MAJOR version number from the Version field, used for the _V# / -v# suffixes.
        Tolerates 'v2', '2.1.0', '  3 ' etc.; falls back to '1' when nothing usable is typed."""
        return _major_of(self._cv_ver.get())

    def _cv_refresh_suffix(self):
        """Keep the gold _V# (file-name) and -v# (id) hints next to the Name/ID boxes in sync with
        the major version, and re-render the file-name preview.  The suffixes are appended to the
        rmod FILE NAME and the ID only — the rmod's own `name` field stays exactly as typed."""
        mj = self._cv_major()
        if hasattr(self, "_cv_name_suffix"):
            self._cv_name_suffix.set(f"_V{mj}")
            self._cv_id_suffix.set(f"-v{mj}")
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

    def _cv_auto_id(self, _=None):
        name = self._cv_name.get().strip()
        if name and not self._cv_id.get():
            self._cv_id.set(_name_to_id(name))

    def _cv_scan(self):
        mod_folder = self._cv_mod.get().strip()
        if not mod_folder:
            ui_util.error(self, t("Missing"), t("Set the Mod Folder first.")); return
        self._cv_lb.delete(0, tk.END)
        pairs = scan_mod_folder(mod_folder, str(self._cv_game_data(mod_folder)))
        if not pairs:
            self._cv_scan_status.set(t("No matching .dat files found."))
            return
        for mod_dat, _, rmod_rel in pairs:
            self._cv_lb.insert(
                tk.END, t("{rmod_rel}  ({kb} KB)", rmod_rel=rmod_rel, kb=mod_dat.stat().st_size // 1024))
        self._cv_scan_status.set(t("{n} .dat file(s) found", n=len(pairs)))

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
            ui_util.error(self, t("Missing"), t("Enter a mod Name."))
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
                t("Missing backup / reference files"),
                t("Couldn't find clean {game} game files to diff this mod against "
                  "(looked in {game_data}).\n\nCreate a backup for that version on the Mod Manager "
                  "tab first (from a clean, unmodded game), or set the matching Game Root.",
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
            def lf(m): self.after(0, lambda m=m: _log(log, m, "info"))
            def wf(m): self.after(0, lambda m=m: _log(log, t("WARN  ") + m, "warn"))
            try:
                ok = run_conversion(
                    mod_folder=str(mod_folder), game_data_dir=game_data, output_rmod=out_rmod,
                    name=name, mod_id=mod_id, version=version,
                    author=author, description=description, log_fn=lf, warn_fn=wf,
                    game_version=stamp_build)
            except Exception as e:
                err = str(e)
            self.after(0, lambda: on_done(ok, out_rmod, err))

        self.after(0, lambda: _log(log, t("Converting: {name}", name=name), "head"))
        threading.Thread(target=_work, daemon=True).start()
        return out_rmod

    def _cv_convert(self):
        if self._conv_running:
            return
        mod_folder  = self._cv_mod.get().strip()
        name        = self._cv_name.get().strip()
        version     = _normalize_version(self._cv_ver.get())
        self._cv_ver.set(version)   # reflect the normalized value back into the field
        author      = self._cv_author.get().strip()
        description = self._cv_desc.get("1.0", tk.END).strip()
        if not mod_folder:
            ui_util.error(self, t("Missing"), t("Set the Mod Folder.")); return
        if not name:
            ui_util.error(self, t("Missing"), t("Enter a mod Name.")); return
        mod_id = self._cv_id.get().strip() or _name_to_id(name)
        self._cv_id.set(mod_id)     # keep the clean (un-suffixed) id in the box; the hint shows -v#

        def _done(ok, out_rmod, err):
            self._conv_running = False
            self._cv_btn.configure(state="normal")
            if err:
                _log(self._cv_log, t("\nError: {err}", err=err), "err")
                self._cv_foot.set(t("Error — see log."))
            elif ok:
                _log(self._cv_log, t("\nDone — {out_rmod}", out_rmod=out_rmod), "ok")
                self._cv_foot.set(t("Written: {name}", name=Path(out_rmod).name))
            else:
                _log(self._cv_log, t("\nConversion failed — see warnings."), "err")
                self._cv_foot.set(t("Conversion failed."))

        self._conv_running = True
        self._cv_btn.configure(state="disabled")
        self._cv_foot.set(t("Converting…"))
        if self._start_rmod_convert(mod_folder=mod_folder, name=name, mod_id=mod_id, version=version,
                                    author=author, description=description,
                                    log=self._cv_log, on_done=_done,
                                    target_build=self._cv_target_build()) is None:
            self._conv_running = False
            self._cv_btn.configure(state="normal")
            self._cv_foot.set(t("Ready."))

    # ── build/version pickers + migrate-to-all-versions (Convert tab) ───────────
    def _cv_build_choices(self) -> list:
        """Branch-labeled display strings for every mapped build (incl OG), newest last."""
        return [_gv_mod.display_name(e["buildid"])
                for e in _migrate_mod.registry_timeline(include_og=True)]

    def _cv_build_map(self) -> dict:
        """display string -> build id, for the version pickers."""
        return {_gv_mod.display_name(e["buildid"]): e["buildid"]
                for e in _migrate_mod.registry_timeline(include_og=True)}

    def _cv_target_build(self) -> str:
        """Build id chosen in 'Make mod for version' (else the detected install)."""
        return self._cv_build_map().get(self._cv_build.get(), self._game_build_id())

    def _mig_src_build(self) -> str:
        return self._cv_build_map().get(self._mig_ver.get(), "")

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
            title=t("Select an rmod to convert to all versions"), initialdir=str(base),
            filetypes=[(t("Mod files"), "*.rmod"), (t("All files"), "*.*")])
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
        src_path  = self._mig_resolve_rmod_path()
        src_build = self._mig_src_build()
        if not src_path or not src_build:
            ui_util.error(self, t("Missing"), t("Pick a source version and rmod first."))
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
            ui_util.info(self, t("Nothing to do"), t("No other mapped versions to convert to."))
            return
        base = Path(self._settings.get("mods_folder", str(_LAUNCH_DIR / "mods")))
        self._conv_running = True
        self._tr_btn.configure(state="disabled")
        self._tr_status.set(t("Converting…"))
        _log(self._cv_log, t("Migrating {name} to {n} version(s)…",
                             name=src_path.name, n=len(targets)), "head")

        def _work():
            ok = skipped = 0
            for e in targets:
                label = _gv_mod.display_name(e["buildid"])
                try:
                    mod = _mod_format.load(str(src_path))
                    rep = _migrate_mod.convert_rmod(mod, src_build, e["buildid"], None)  # shipped maps
                    if rep.get("error"):
                        self.after(0, lambda l=label, r=rep: _log(
                            self._cv_log, t("  {l}: skipped — {err}", l=l, err=r["error"]), "warn"))
                        skipped += 1
                        continue
                    out_dir = base / f"v{e['buildid']}"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    _mod_format.save(mod, str(out_dir / src_path.name))
                    self.after(0, lambda l=label, r=rep: _log(
                        self._cv_log, t("  {l}: ok (reindexed {ri}, dropped {d})",
                                        l=l, ri=r["reindexed"], d=r["dropped"]),
                        "ok" if not r["dropped"] else "warn"))
                    ok += 1
                except Exception as ex:
                    self.after(0, lambda l=label, ex=ex: _log(
                        self._cv_log, t("  {l}: error — {err}", l=l, err=str(ex)), "err"))
                    skipped += 1

            def _fin():
                self._conv_running = False
                self._tr_btn.configure(state="normal")
                self._tr_status.set(t("Done — {ok} converted, {sk} skipped.", ok=ok, sk=skipped))
                _log(self._cv_log, t("Migration complete: {ok} converted, {sk} skipped.",
                                     ok=ok, sk=skipped), "head")
            self.after(0, _fin)
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
        ttk.Button(self._ed_navbar, text=t("←  Back"), command=self._ed_back).pack(side="left", padx=6, pady=4)
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

        ttk.Label(self._ed_select, text=t("MOD EDITOR"), font=_F_HEAD,
                  foreground=_R_GOLD_BRT).pack(anchor="w", padx=10, pady=(10, 0))
        ttk.Label(self._ed_select,
                  text=t("Create a new mod project or load an existing one. A project keeps every "
                         "edit (units, buildings, maps…) together; files are grabbed from your clean "
                         "backup into the mod folder when you first save changes to them."),
                  foreground=_R_TEXT_DIM, wraplength=820, justify="left"
                  ).pack(anchor="w", padx=10, pady=(0, 8))

        # Body: project picker (left half) | the Mod Editor "all logs" window (right half).
        body = ttk.PanedWindow(self._ed_select, orient=tk.HORIZONTAL)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        left = ttk.Frame(body)
        body.add(left, weight=1)

        nf = ttk.LabelFrame(left, text=t("Create New Mod"))
        nf.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Label(nf, text=t("Mod name:")).grid(row=0, column=0, sticky="e", padx=6, pady=6)
        self._ed_new_name = tk.StringVar()
        ent = ttk.Entry(nf, textvariable=self._ed_new_name, width=40)
        ent.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        ent.bind("<Return>", lambda *_: self._ed_create_project())
        ttk.Button(nf, text=t("Create Project"), command=self._ed_create_project
                   ).grid(row=0, column=2, padx=6, pady=6)
        # Target game version: defaults to the auto-detected installed build, but you can pick any
        # version you have a clean backup for (so you can mod compat-2 while your game is on public).
        ttk.Label(nf, text=t("Game version:")).grid(row=1, column=0, sticky="e", padx=6, pady=(0, 6))
        self._ed_target_ver = tk.StringVar()
        self._ed_target_map = {}
        self._ed_target_cb = ttk.Combobox(nf, textvariable=self._ed_target_ver, state="readonly", width=38)
        self._ed_target_cb.grid(row=1, column=1, sticky="w", padx=6, pady=(0, 6))
        self._ed_refresh_target_versions()
        nf.columnconfigure(1, weight=1)

        lf = ttk.LabelFrame(left, text=t("Load Existing Mod"))
        lf.pack(fill="both", expand=True, padx=4, pady=6)
        # Action buttons sit at the top-left, beside the "Load Existing Mod" title.
        tb = ttk.Frame(lf)
        tb.pack(fill="x", padx=6, pady=(2, 0))
        btns = ttk.Frame(tb)
        btns.pack(side="left")
        ttk.Button(btns, text=t("Load Selected"), command=self._ed_load_selected).pack(side="left", padx=2)
        ttk.Button(btns, text=t("Refresh"), command=self._ed_refresh_project_list).pack(side="left", padx=2)
        ttk.Button(btns, text=t("Browse Folder…"), command=self._ed_browse_project).pack(side="left", padx=2)

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
        logf = ttk.LabelFrame(body, text=t("Log  ·  all activity (never cleared)"))
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
            self._ed_proj_lb.insert(tk.END, d.name + ("" if is_proj else t("   (no project.json)")))
            self._ed_proj_paths.append(d)
        if not self._ed_proj_paths:
            self._ed_proj_lb.insert(tk.END, t("(no mods yet — create one above)"))
            self._ed_proj_paths.append(None)
        self._ed_refresh_target_versions()

    def _ed_refresh_target_versions(self):
        """Populate the create-project version picker with every backed-up version, defaulting to the
        auto-detected installed build (the long-standing automatic behaviour)."""
        if not hasattr(self, "_ed_target_cb"):
            return
        vers = self._backed_up_versions()
        self._ed_target_map = {lbl: key for key, lbl, _ in vers}
        labels = list(self._ed_target_map)
        self._ed_target_cb.configure(values=labels)
        cur = self._version_key()
        default = next((lbl for key, lbl, _ in vers if key == cur), labels[0] if labels else "")
        self._ed_target_ver.set(default)

    def _ed_create_project(self):
        name = self._ed_new_name.get().strip()
        if not name:
            ui_util.info(self, t("Mod name"), t("Enter a name for the new mod."))
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
            ui_util.error(self, t("Already exists"),
                                 t("A mod folder with that name already exists. Pick another name, "
                                   "or load it from the list below."))
            self._ed_refresh_project_list()
            return
        except Exception as e:
            ui_util.error(self, t("Create failed"), str(e))
            return
        self._project = proj
        self._ed_new_name.set("")
        self._ed_show_hub()

    def _ed_load_selected(self):
        sel = self._ed_proj_lb.curselection()
        if not sel:
            ui_util.info(self, t("Load"), t("Select a mod folder to load."))
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
                ui_util.info(self, t("Folder"), str(base))
        except Exception as e:
            ui_util.error(self, t("Open Failed"), t("Could not open:\n{path}\n\n{e}", path=base, e=e))

    def _prompt_branch_build_id(self, current=""):
        """Ask which branch (game version) a project targets; return its build id.
        Used to migrate legacy projects whose 'version' is still a branch NAME, or whose
        stored build id has since been retired (no longer in the registry)."""
        builds = _gv_mod.known_builds()                      # build_id -> {branch, dataver}
        if not builds:
            return None
        # order by branch label; show "branch (vBUILD)"
        items = sorted(builds.items(), key=lambda kv: kv[1].get("branch", kv[0]))
        labels = [_gv_mod.display_name(b) for b, _ in items]
        ids = [b for b, _ in items]
        win = ui_util.themed_toplevel(self, t("Which game version?"))
        tk.Label(win, text=t("This mod project was saved for '{v}', which isn't a current "
                             "game build.\nWhich game version (branch) is it built for?", v=current),
                 justify="left").pack(padx=14, pady=(12, 6))
        var = tk.StringVar(value=labels[0])
        # default to the currently-detected build if present
        det = self._game_build_id()
        if det in ids:
            var.set(labels[ids.index(det)])
        ttk.Combobox(win, textvariable=var, values=labels, state="readonly",
                     width=28).pack(padx=14, pady=4)
        chosen = {"id": None}

        def ok():
            chosen["id"] = ids[labels.index(var.get())] if var.get() in labels else None
            win.destroy()
        ttk.Button(win, text=t("OK"), command=ok).pack(pady=(6, 12))
        win.wait_window()
        return chosen["id"]

    def _ed_open_folder(self, folder):
        # Read pristine files from the backup matching the PROJECT's target version (so a project made
        # for another version still loads correctly).  Fall back to the current-build backup gate for
        # legacy/unknown versions.
        ver = self._read_project_version(folder)
        key = ver if str(ver).isdigit() else self._version_key()
        bdir = self._backup_dir_for(key)
        if not (bdir.exists() and any(bdir.rglob("*.dat"))):
            if not self._require_backup():
                return
            bdir = self._backup_dir()
        try:
            proj = mp_project_mod.ModProject.load(
                folder, self._settings.get("game_root", ""), str(bdir))
        except Exception as e:
            ui_util.error(self, t("Load failed"), str(e))
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
                        t("Different game format"),
                        t("This project's files are data version {a}, but {b} is {c}. "
                          "Left unchanged — convert the mod instead of relabeling it.",
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

        act = ttk.LabelFrame(self._ed_hub, text=t("Mod Windows"))
        act.pack(fill="x", padx=8, pady=6)
        # Wrap the editor-open buttons (in a plain inner frame) so none (e.g. Raw / Asset Editor) is
        # clipped when the window is narrow (issue #5.3).  The help text is on its OWN line below, so
        # it never affects the button positions.
        act_row = ttk.Frame(act)
        act_row.pack(fill="x", padx=4, pady=(2, 0))
        # "Add .dat files…" sits at the RIGHT of the editor-window button row — it imports existing
        # .dat files into the project (see _ed_add_dats).  Packed FIRST so it reserves its right-edge
        # spot; the flow of window buttons then fills the remaining width and wraps within it.
        ttk.Button(act_row, text=t("  Add .dat files…  "), command=self._ed_add_dats
                   ).pack(side="right", padx=4, pady=6)
        act_bar = ttk.Frame(act_row)
        act_bar.pack(side="left", fill="x", expand=True)
        act_btns = [
            ttk.Button(act_bar, text=t("  Units & Buildings  "), command=self._ed_open_units),
            ttk.Button(act_bar, text=t("  Map Editor  "), command=self._open_map_editor),
            ttk.Button(act_bar, text=t("  AI  "), command=self._ed_open_ai),
            ttk.Button(act_bar, text=t("  Economy  "), command=self._ed_open_economy),
            ttk.Button(act_bar, text=t("  Raw / Asset Editor  "), command=self._ed_open_tools),
        ]
        ui_util.flow(act_bar, act_btns, pady=6)
        ttk.Label(act, text=t("Each window has its own Save button — it saves every accumulated "
                              "change to the mod's .dat."),
                  foreground=_R_TEXT_DIM).pack(anchor="w", padx=8, pady=(2, 4))

        proj = ttk.LabelFrame(self._ed_hub, text=t("Project"))
        proj.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(proj, text=t("  Deploy to Game  "), command=self._ed_deploy
                   ).pack(side="left", padx=4, pady=6)
        ttk.Button(proj, text=t("  Convert to rmod  "), command=self._ed_convert_to_rmod
                   ).pack(side="left", padx=4, pady=6)
        # Pack Close Project (right) BEFORE the help text so it keeps its spot in a narrow window —
        # the long explanatory label clips instead of pushing Close Project off-screen (issue #5.3).
        ttk.Button(proj, text=t("  Close Project  "), command=self._ed_close_project
                   ).pack(side="right", padx=4, pady=6)
        ttk.Label(proj, text=t("Deploy = write the dats into the game. Convert to rmod = export an "
                               "update mod (only your changes) to share."),
                  foreground=_R_TEXT_DIM).pack(side="left", padx=10)

        # ── Bottom: Mod Details (left) + Log (right) — fill the remaining hub space ───
        bottom = ttk.Frame(self._ed_hub)
        bottom.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        det = ttk.LabelFrame(bottom, text=t("Mod Details (description.txt)"))
        det.pack(side="left", fill="both", expand=True, padx=(0, 4))
        det.columnconfigure(1, weight=1)
        det.rowconfigure(2, weight=1)          # description row stretches (matches the Mod Manager box)

        ttk.Label(det, text=t("Author:")).grid(row=0, column=0, sticky="e", padx=6, pady=(6, 3))
        self._ed_meta_author = tk.StringVar()
        ttk.Entry(det, textvariable=self._ed_meta_author).grid(
            row=0, column=1, sticky="ew", padx=6, pady=(6, 3))

        ttk.Label(det, text=t("Version:")).grid(row=1, column=0, sticky="e", padx=6, pady=3)
        self._ed_meta_ver = tk.StringVar()
        ed_ver_ent = ttk.Entry(det, textvariable=self._ed_meta_ver, width=14)
        ed_ver_ent.grid(row=1, column=1, sticky="w", padx=6, pady=3)
        # Empty or non-x.x.x input snaps to the 1.0.0 default when the field loses focus.
        ed_ver_ent.bind("<FocusOut>", lambda *_: self._ed_normalize_version())

        ttk.Label(det, text=t("Description:")).grid(row=2, column=0, sticky="ne", padx=6, pady=3)
        self._ed_meta_desc = _ThemedScrolledText(
            det, font=_F_MAIN, wrap="word", relief="flat",
            background=_R_BG_WIDGET, foreground=_R_TEXT, insertbackground=_R_GOLD,
            highlightthickness=1, highlightcolor=_R_BORDER, highlightbackground=_R_BORDER)
        self._ed_meta_desc.grid(row=2, column=1, sticky="nsew", padx=6, pady=3)

        br = ttk.Frame(det)
        br.grid(row=3, column=1, sticky="ew", padx=6, pady=(0, 4))
        self._ed_meta_save_btn = ttk.Button(br, text=t("Save Details"),
                                            command=self._ed_save_description, state="disabled")
        self._ed_meta_save_btn.pack(side="left")
        self._ed_meta_status = ttk.Label(br, text="", foreground=_R_TEXT_DIM)
        self._ed_meta_status.pack(side="left", padx=8)
        ttk.Label(det, text=t("First line = author, last line = version, the rest = description. Used "
                              "when you convert to an rmod."),
                  foreground=_R_TEXT_DIM, wraplength=380, justify="left").grid(
            row=4, column=1, sticky="w", padx=6, pady=(0, 6))

        logf = ttk.LabelFrame(bottom, text=t("Log  ·  all activity (never cleared)"))
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
        self._ed_proj_lbl.configure(text=t("Mod: {name}", name=self._project.name))
        self._ed_path_lbl.configure(text=str(self._project.folder))
        self._ed_load_description()
        self._ed_update_status()
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
        self._ed_meta_status.configure(text=(t("● unsaved") if dirty else ""), foreground=_R_GOLD)

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
            ui_util.error(self, t("Save Details"), t("Could not write description.txt:\n{e}", e=e))
            return
        self._ed_meta_saved = (author, description, version)
        self._ed_meta_check_dirty()
        self._ed_meta_status.configure(text=t("✓ saved"), foreground=_R_GREEN)
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
            text=(t("● {n} unsaved change-set(s)", n=n) if n else t("✓ all changes saved")),
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
            ui_util.error(self, t("Units Editor"), t("Failed to open the units editor:\n{e}", e=e))
            return
        self._ed_open_view(view, t("Units & Buildings"))

    def _ed_open_ai(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import ai_editor
            view = ai_editor.AIEditorWindow(self._ed_content, self._project,
                                            on_change=self._ed_update_status)
        except Exception as e:
            ui_util.error(self, t("AI Editor"), t("Failed to open the AI editor:\n{e}", e=e))
            return
        self._ed_open_view(view, t("AI"))

    def _ed_open_economy(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import economy_editor
            view = economy_editor.EconomyEditorWindow(self._ed_content, self._project,
                                                      on_change=self._ed_update_status)
        except Exception as e:
            ui_util.error(self, t("Economy Editor"), t("Failed to open the economy editor:\n{e}", e=e))
            return
        self._ed_open_view(view, t("Economy"))

    def _ed_open_tools(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        try:
            import tools_editor
        except Exception as e:
            ui_util.error(self, t("Raw / Asset Editor"), t("Could not load the tools module:\n{e}", e=e))
            return
        try:
            view = tools_editor.ToolsEditorWindow(
                self._ed_content, self._project, on_change=self._ed_update_status,
                open_nested=self._ed_open_nested_tools)
            self._ed_open_view(view, t("Raw / Asset Editor"), cleanup=view.cleanup)
        except Exception as e:
            ui_util.error(self, t("Raw / Asset Editor"), t("Failed to open the Raw / Asset Editor:\n{e}", e=e))

    def _ed_open_nested_tools(self, store, on_applied):
        """Open an embedded .dat (from the Raw editor's 'Open as nested .dat') as ANOTHER nested view on
        the same stack, so Back walks back out through each nested archive to the hub."""
        try:
            import tools_editor
            view = tools_editor.ToolsEditorWindow(
                self._ed_content, on_change=on_applied, store=store,
                open_nested=self._ed_open_nested_tools)
        except Exception as e:
            ui_util.error(self, t("Raw / Asset Editor"), t("Failed to open the nested archive:\n{e}", e=e))
            return
        self._ed_open_view(view, t("Raw / Asset Editor — {name}", name=store.name), cleanup=view.cleanup)

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
            title=t("Add .dat files to the project"),
            initialdir=self._settings.get("game_root", "") or str(self._editor_mods_dir()),
            filetypes=[(t("Game data files"), "*.dat"), (t("All files"), "*.*")])
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
        listing = "\n".join(t("  • {name}  →  {rel}", name=sp.name, rel=rel) for sp, _, rel in plan)
        msg = t("Copy these .dat file(s) into the project (for {ver})?\n\n{listing}\n\n"
                "Each goes where the game expects it; the project reloads so the editors use them.",
                ver=self._project.branch_label, listing=listing)
        overwrite = [rel for _, d, rel in plan if d.is_file()]
        if overwrite:
            msg += t("\n\nThis REPLACES file(s) already in the project:\n") + "\n".join("  • " + r for r in overwrite)
        if not ui_util.confirm(self, t("Add .dat files"), msg):
            return
        copied = 0
        for sp, dest, rel in plan:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(sp), str(dest))
                copied += 1
                _log(self._ed_log, t("  added .dat: {rel}", rel=rel), "ok")
            except Exception as e:
                ui_util.error(self, t("Add .dat files"), t("Could not copy {name}:\n{e}", name=sp.name, e=e))
                return
        # Reload the project from disk so the newly-copied dats are what the editor has loaded.
        folder = self._project.folder
        try:
            self._project = mp_project_mod.ModProject.load(
                folder, self._settings.get("game_root", ""), str(self._project.backup_dir))
        except Exception as e:
            ui_util.error(self, t("Load failed"), str(e))
            return
        self._ed_show_hub()
        ui_util.info(self, t("Add .dat files"),
                            t("Added {n} .dat file(s) to the project.", n=copied))

    def _ed_deploy(self):
        if not self._project:
            return
        self._ed_sync_project_paths()
        if not self._settings.get("game_root", ""):
            ui_util.info(self, t("No Game Root"), t("Set the Game Root Directory in Settings first."))
            return
        if self._project.is_dirty():
            ui_util.info(
                self,
                t("Unsaved changes"),
                t("You have changes that aren't saved to the mod yet. Save them in their editor "
                  "window (each window has a Save button), then deploy."))
            return
        dats = self._project.saved_dats()
        if not dats:
            ui_util.info(self, t("Deploy"),
                                t("Nothing to deploy yet — make and save a change in an editor "
                                  "window first to build the mod's .dat."))
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
                    t("Wrong game version?"),
                    t("This mod project targets {target}, but your game is currently set to {installed}.\n\n"
                      "Deploying writes {target} files into a {installed} game, which may not load "
                      "correctly. Switch your game to {target} in Steam to test it.\n\nDeploy anyway?",
                      target=target_lbl, installed=self._branch_label())):
                return
        if not ui_util.confirm(
                self,
                t("Deploy to game?"),
                t("This will OVERWRITE the live game file(s):\n\n")
                + "\n".join("  • " + d.name for d in dats)
                + t("\n\nA timestamped backup of each original is saved under output/backups, and any "
                    "files left modified by a previous deploy that this mod doesn't touch are reverted to "
                    "clean.\n\nProceed?")):
            return
        # Use the backup matching THIS project's version (set at load/create), so leftover-revert and
        # clean reads come from the right version's originals — not necessarily the installed build's.
        bd = Path(self._project.backup_dir) if getattr(self._project, "backup_dir", "") else self._backup_dir()
        game_root = Path(self._settings["game_root"])
        _log(self._ed_log, t("\nDeploying mod '{name}' to the game…", name=self._project.name), "head")
        try:
            # Shared deploy tracker: first revert LEFTOVERS — dats a PREVIOUS deploy (this project, the
            # Mod Manager, or another project) modified that this mod won't overwrite — so they don't
            # stay dirty.  The dats this mod deploys get overwritten below, so they need no pre-clean.
            proj_rels = {r.replace("/", os.sep) for r in self._project.deployed_dat_rels()}
            prev = {r.replace("/", os.sep) for r in self._mgr_saved_deployed_dats()}
            reverted = 0
            for rel in sorted(prev - proj_rels):
                src = bd / rel
                if src.is_file():
                    dest = game_root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    reverted += 1
                    _log(self._ed_log, t("  reverted leftover: {rel}", rel=rel), "info")
            copied, backups = self._project.deploy(bd)
            for c in copied:
                _log(self._ed_log, t("  deployed: {c}", c=c), "ok")
            # Record what's now overlaid so the next deploy (here or in the Mod Manager) can clean it.
            self._mgr_set_deployed_dats(r.replace(os.sep, "/") for r in proj_rels)
        except Exception as e:
            _log(self._ed_log, t("  ERROR: {e}", e=e), "err")
            ui_util.error(self, t("Deploy"), str(e))
            return
        _log(self._ed_log, t("Done — deployed {n} file(s), reverted {reverted} leftover(s).",
                             n=len(copied), reverted=reverted), "head")
        msg = t("Deployed to:\n\n") + "\n".join(copied)
        if reverted:
            msg += t("\n\nReverted {reverted} leftover file(s) from a previous deploy to clean.", reverted=reverted)
        if backups:
            msg += t("\n\nBackups saved:\n") + "\n".join(backups)
        ui_util.info(self, t("Deployed"), msg)

    def _ed_convert_to_rmod(self):
        if not self._project or getattr(self, "_ed_converting", False):
            return
        self._ed_sync_project_paths()
        if self._project.is_dirty():
            ui_util.info(self, t("Unsaved changes"),
                                t("Save your changes in the editor windows first, then convert."))
            return
        dats = self._project.saved_dats()
        if not dats:
            ui_util.info(self, t("Convert to rmod"),
                                t("Nothing to convert yet — make and save a change first."))
            return
        # Offer to flush unsaved Mod Details so the rmod carries the author/version/description shown.
        if self._ed_meta_dirty() and ui_util.confirm(
                self,
                t("Unsaved details"),
                t("You have unsaved Mod Details (author / version / description). Save them to "
                  "description.txt before converting, so they go into the rmod?")):
            self._ed_save_description()
        meta = self._project.read_description()

        def _done(ok, out_rmod, err):
            self._ed_converting = False
            self._ed_update_status()
            if err:
                _log(self._ed_log, t("Error: {err}", err=err), "err")
                ui_util.error(self, t("Convert to rmod"), err)
            elif ok:
                _log(self._ed_log, t("Done — wrote {out_rmod}", out_rmod=out_rmod), "ok")
                ui_util.info(self, t("Convert to rmod"),
                                    t("Exported update mod (only your changes) to the mods folder:\n\n{out_rmod}",
                                      out_rmod=out_rmod))
            else:
                _log(self._ed_log, t("No changes found to convert."), "warn")
                ui_util.warning(self, t("Convert to rmod"), t("No changes were found to convert."))

        # Use the SAME conversion path as the Convert tab: versioned _V#/-v# naming, branch detection,
        # shipped to the mods folder, diffed against the clean originals.
        self._ed_converting = True
        self._ed_status_lbl.configure(text=t("Converting to rmod… (diffing, please wait)"),
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
                    t("Unsaved changes"),
                    t("There are changes not yet saved to the mod (save them in an editor window "
                      "to keep them). Close the project and discard those unsaved changes?")):
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
            ui_util.error(self, t("Map Editor"), t("Failed to open the map editor:\n{e}", e=e))
            return
        self._ed_open_view(view, t("Map Editor"))

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

        ttk.Label(p, text=t("Configure paths for the R.U.S.E. COMPAT Mod Manager."),
                  foreground=_R_TEXT_DIM).pack(anchor="w", padx=10, pady=(10, 4))

        pf = ttk.LabelFrame(p, text=t("Paths"))
        pf.pack(fill="x", **pad)
        pf.columnconfigure(1, weight=1)

        # (label, settings key, editable?, hint).  Only the Game Root is user-set; the working dir and
        # mods folder are AUTO-derived from the app's location, so they're shown read-only and their
        # button just opens the folder in the file explorer (it doesn't change the path).
        defs = [
            (t("Game Root Directory:"), "game_root", True,
             t("Root folder of your R.U.S.E. COMPAT installation (contains Ruse.exe and Data/).")),
            (t("Working Directory:"),   "working_dir", False,
             t("Where the app lives — output and state files are stored here. Auto-set to the exe's folder; "
               "click Open to view it in your file explorer.")),
            (t("Mods Folder:"),         "mods_folder", False,
             t("Where your .rmod files live. Auto-set to <working dir>\\mods; click Open to view it.")),
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
                ttk.Button(pf, text=t("Browse…"), command=self._set_browse_game_root).grid(
                    row=er, column=2, padx=4, pady=6)
            else:
                # read-only: visible & copyable but not editable; the button opens it in Explorer.
                ttk.Entry(pf, textvariable=var, state="readonly").grid(
                    row=er, column=1, sticky="ew", **pad)
                ttk.Button(pf, text=t("Open…"), command=lambda k=key: self._set_open_folder(k)).grid(
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

        bf = ttk.LabelFrame(p, text=t("Game File Backup"))
        bf.pack(fill="x", **pad)

        bfh = ttk.Frame(bf)
        bfh.pack(fill="x")

        # Left — game file backup
        bfl = ttk.Frame(bfh)
        bfl.pack(side="left", fill="both", expand=True)
        ttk.Label(bfl,
                  text=t("Back up your original game files before deploying any mods.\n"
                         "Set Game Root Directory above first, then click the button below."),
                  foreground=_R_TEXT_DIM, font=_F_LOG, justify="left",
                  ).pack(anchor="w", padx=8, pady=(6, 2))
        btn_row = ttk.Frame(bfl)
        btn_row.pack(anchor="w", padx=8, pady=(2, 4))
        self._set_backup_btn = ttk.Button(btn_row, text=t("Create Backup"),
                                          command=self._mgr_create_backup,
                                          state="disabled")
        self._set_backup_btn.pack(side="left", padx=(0, 4))
        self._set_restore_btn = ttk.Button(btn_row, text=t("Restore Clean"),
                                           command=self._mgr_restore_clean,
                                           state="disabled")
        self._set_restore_btn.pack(side="left", padx=(0, 4))
        ttk.Button(btn_row, text=t("Detect Game Version"),
                   command=self._set_detect_game).pack(side="left")
        self._set_s2_lbl = tk.Label(bfl, text="", font=_F_LOG,
                                    background=_R_BG_PANEL, anchor="w")
        self._set_s2_lbl.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Separator(bfh, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)

        # Right — profile management
        bfr = ttk.Frame(bfh)
        bfr.pack(side="left", fill="y", padx=4, pady=4)
        ttk.Label(bfr, text=t("Profile"), foreground=_R_TEXT_DIM,
                  font=_F_BOLD).pack(anchor="w", pady=(2, 4))
        self._prof_lvl1_btn = ttk.Button(
            bfr, text=t("Set lvl 1 Profile"), command=self._profile_set_lvl1)
        self._prof_lvl1_btn.pack(fill="x", pady=2)
        self._prof_lvl100_btn = ttk.Button(
            bfr, text=t("Set lvl 100 Profile"), command=self._profile_set_lvl100)
        self._prof_lvl100_btn.pack(fill="x", pady=2)
        self._prof_backup_btn = ttk.Button(bfr, text=t("Back Up Current Profile"),
                                            command=self._profile_backup_current,
                                            state="disabled")
        self._prof_backup_btn.pack(fill="x", pady=(6, 2))
        self._prof_set_backup_btn = ttk.Button(bfr, text=t("Set Backed-Up Profile"),
                                               command=self._profile_set_backed_up,
                                               state="disabled")
        self._prof_set_backup_btn.pack(fill="x", pady=(2, 2))
        # Which backed-up profile the button applies: "Auto" (newest applicable, the default) or a
        # specific populated version the user picks. Values are refreshed in _profile_refresh_ui.
        self._prof_apply_choice = tk.StringVar(value=_PROFILE_AUTO)
        self._prof_apply_cb = ttk.Combobox(bfr, textvariable=self._prof_apply_choice,
                                           state="readonly", values=[_PROFILE_AUTO])
        self._prof_apply_cb.pack(fill="x", pady=(0, 2))

        info = ttk.LabelFrame(p, text=t("Output Folder Structure"))
        info.pack(fill="x", **pad)
        ttk.Label(info,
                  text=t("output/backups/          ← original game files "
                         "(created by 'Create Backup')\n"
                         "output/mod_output_files/ ← patched .dat files "
                         "(generated on Deploy)\n"
                         "mods/                    ← converted .rmod files "
                         "(output of the Convert tab)"),
                  justify="left",
                  font=_F_LOG).pack(padx=8, pady=6, anchor="w")

        # ── Accessibility (at the very bottom) ──────────────────────────────────
        acc = ttk.LabelFrame(p, text=t("Accessibility"))
        acc.pack(fill="x", **pad)
        arow = ttk.Frame(acc)
        arow.pack(fill="x", padx=8, pady=6)
        ttk.Label(arow, text=t("Default language:")).pack(side="left")
        cur_code = self._settings.get("default_language", "us")
        self._lang_var = tk.StringVar(value=_dic_mod.lang_label(cur_code))
        self._lang_cb = ttk.Combobox(arow, textvariable=self._lang_var, state="readonly", width=22,
                                     values=[name for _c, name in _dic_mod.LANGUAGES])
        self._lang_cb.pack(side="left", padx=8)
        ui_util.fit_combobox(self._lang_cb)   # fit names like "Chinese (Simplified)" (issue #5.1)
        self._lang_cb.bind("<<ComboboxSelected>>", self._on_default_language)
        # Right side: create Windows shortcuts to this app's .exe.
        sc = ttk.Frame(arow)
        sc.pack(side="right")
        ttk.Button(sc, text=t("Add Start Menu Shortcut"),
                   command=self._add_start_menu_shortcut).pack(side="left", padx=2)
        ttk.Button(sc, text=t("Add Desktop Shortcut"),
                   command=self._add_desktop_shortcut).pack(side="left", padx=2)
        ttk.Label(acc, text=t("The localization language used by default when editing in-game text "
                              "(e.g. unit names). You can still pick another language per-edit in the "
                              "editors. English is the game's primary language."),
                  foreground=_R_TEXT_DIM, font=_F_LOG, justify="left", wraplength=580
                  ).pack(anchor="w", padx=8, pady=(0, 6))

        self._profile_refresh_ui()

    def _on_default_language(self, _=None):
        code = _dic_mod.LANG_CODE.get(self._lang_var.get(), "us")
        prev = self._settings.get("default_language", "us")
        self._settings["default_language"] = code
        self._save_settings()
        i18n.set_language(code)
        if code != prev:
            # The whole UI is built once in the chosen language, so a restart applies it everywhere.
            if ui_util.confirm(
                    self,
                    t("Restart to change language"),
                    t("The interface language changes when the Mod Manager restarts.\n\n"
                      "Restart now?")):
                self._restart_app()

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
            ui_util.error(self, t("Restart failed"), str(e))
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
                _log(self._mgr_log, t("Created shortcut: {path}", path=path), "ok")
                ui_util.info(self, t("Shortcut created"),
                                    t("Created shortcut:\n{path}", path=path) if path else t("Shortcut created."))
            else:
                err = (res.stderr or res.stdout or "Unknown error").strip()
                _log(self._mgr_log, t("Shortcut failed: {err}", err=err), "err")
                ui_util.error(self, t("Shortcut failed"), err)
        except Exception as e:
            ui_util.error(self, t("Shortcut failed"), str(e))
        finally:
            if ps1 and os.path.exists(ps1):
                try:
                    os.remove(ps1)
                except OSError:
                    pass

    def _set_browse_game_root(self):
        d = filedialog.askdirectory(
            title=t("Select R.U.S.E. COMPAT game root (contains Ruse.exe)"))
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
            ui_util.error(self, t("Not Found"), t("Folder does not exist:\n{path}", path=path))
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))               # Windows: open in Explorer
            else:
                ui_util.info(self, t("Folder"), str(path))
        except Exception as e:
            ui_util.error(self, t("Open Failed"), t("Could not open:\n{path}\n\n{e}", path=path, e=e))

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
                ui_util.error(self, t("Detection Failed"), t("Could not query Steam:\n{e}", e=e))
            return

        compat = dirs.get("compat")
        public = dirs.get("public")

        if not compat and not public:
            if not silent:
                ui_util.info(
                    self,
                    t("Not Found"),
                    t("Could not find R.U.S.E. or R.U.S.E. COMPAT in any Steam library.\n\n"
                      "Make sure the game is installed and Steam has been run at least once."))
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
                t("Two Versions Found"),
                t("Both versions of R.U.S.E. were found:\n\n"
                  "  R.U.S.E. COMPAT:  {compat}\n"
                  "  R.U.S.E.:         {public}\n\n"
                  "Use R.U.S.E. COMPAT?\n(No = use R.U.S.E.)", compat=compat, public=public))
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
            self._set_detect_game(silent=True)
            gr = self._settings.get("game_root", "").strip()
            if gr and _is_dir_safe(Path(gr)):
                new_ver = self._version_subname()
                if self._mgr_current_ver and new_ver != self._mgr_current_ver:
                    _log(self._mgr_log,
                         t("Game version changed to {label} — refreshing the mod list.",
                           label=self._branch_label()), "head")
                    self._apply_version_change(new_ver)
        except Exception:
            pass
        self.after(15000, self._auto_detect_poll)

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
        self._set_status.set(t("Settings saved."))
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
        # Build (or format) changed — save old list, then re-scan + restore the new build's list.
        self._save_mgr_state()
        self._show_compat_var.set(False)
        self._mgr_update_compat_btn()
        self._mgr_scan_both()
        self._mgr_load_mode(new_ver)
        self._cv_refresh_labels()
        self._mgr_refresh_status()
        self._profile_refresh_ui()
        self._update_mod_editor_tab()

    def _set_auto_backup(self):
        """Start a backup automatically when game root is newly set, if none exists yet."""
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
             t("Game root configured [{ver_label}] — automatically backing up game files...",
               ver_label=ver_label),
             "head")
        self._mgr_set_busy(True)
        self._mgr_show_backup_warn()
        threading.Thread(target=self._do_backup,
                         args=(game_root, bd), daemon=True).start()

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
            ui_util.error(self, t("Profile Not Found"),
                                 t("Preset profile not found:\n{src}", src=src))
            return
        dirs = _find_steam_profile_dirs()
        if not dirs:
            ui_util.error(self, t("Steam Not Found"),
                                 t("No Steam R.U.S.E. profile directories found.\n\n"
                                   "Expected: C:\\Program Files (x86)\\Steam\\"
                                   "userdata\\<id>\\21970\\{local,remote}"))
            return
        copied, failed = [], []
        for d in dirs:
            try:
                shutil.copy2(src, d / "PROFILE.ruse")
                copied.append(str(d))
            except Exception as e:
                failed.append(f"{d}: {e}")
        msg = t("lvl {level} profile deployed.\n\nCopied to:\n", level=level) + \
              "\n".join(f"  {c}" for c in copied)
        if self._game_version() != "compat":
            msg += t("\n\n(An OG-compat preset — the game rebuilds it for {cur} on next launch.)",
                     cur=self._branch_label())
        if failed:
            msg += t("\n\nFailed:\n") + "\n".join(f"  {f}" for f in failed)
        ui_util.info(self, t("Profile Set"), msg)

    def _profile_set_lvl1(self):
        self._profile_set_lvl(1)

    def _profile_set_lvl100(self):
        self._profile_set_lvl(100)

    def _profile_backup_current(self):
        dirs = _find_steam_profile_dirs()
        if not dirs:
            ui_util.error(self, t("Steam Not Found"),
                                 t("No Steam R.U.S.E. profile directories found."))
            return
        # Pick the most recently modified PROFILE.ruse across all Steam dirs
        src = None
        for d in dirs:
            p = d / "PROFILE.ruse"
            if p.is_file():
                if src is None or p.stat().st_mtime > src.stat().st_mtime:
                    src = p
        if src is None:
            ui_util.error(self, t("No Profile"),
                                 t("PROFILE.ruse not found in any Steam directory."))
            return
        ver = self._version_subname()          # per BUILD: profile/v<buildid>/ — archivable per version
        label = self._branch_label()
        backup_dir = _PROFILES_DIR / ver
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / "PROFILE.ruse"
        shutil.copy2(src, dest)
        ui_util.info(self, t("Backup Complete"),
                            t("Profile backed up for {label}.\n\n"
                              "From: {src}\n"
                              "To:   {dest}", label=label, src=src, dest=dest))
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
                t("No Backup"),
                t("No applicable backed-up profile found for {label} or any older version.",
                  label=self._branch_label()))
            return
        choice = self._prof_apply_choice.get() if hasattr(self, "_prof_apply_choice") else _PROFILE_AUTO
        if choice == _PROFILE_AUTO:
            build, label, src = options[0]                # newest applicable
            if build != self._game_build_id():
                # AUTO fell back to an OLDER profile — confirm the forward-upgrade
                if not ui_util.confirm(
                    self,
                    t("Apply older profile?"),
                    t("No profile is backed up for {cur}.\n\nApply the most recent older one, {label}? "
                      "The game rebuilds it for {cur} on next launch.",
                      cur=self._branch_label(), label=label)):
                    return
        else:
            picked = next((o for o in options if o[1] == choice), None)
            if picked is None:
                ui_util.error(self, t("No Backup"),
                                     t("That profile version is no longer available."))
                return
            build, label, src = picked                    # explicit user pick — no confirm
        dirs = _find_steam_profile_dirs()
        if not dirs:
            ui_util.error(self, t("Steam Not Found"),
                                 t("No Steam R.U.S.E. profile directories found."))
            return
        copied, failed = [], []
        for d in dirs:
            try:
                shutil.copy2(src, d / "PROFILE.ruse")
                copied.append(str(d))
            except Exception as e:
                failed.append(f"{d}: {e}")
        note = "" if build == self._game_build_id() else t(
            "\n\n(An older profile — the game rebuilds it for {cur} on next launch.)",
            cur=self._branch_label())
        msg = (t("{label} profile deployed.\n\nCopied to:\n", label=label)
               + "\n".join(f"  {c}" for c in copied) + note)
        if failed:
            msg += t("\n\nFailed:\n") + "\n".join(f"  {f}" for f in failed)
        ui_util.info(self, t("Profile Set"), msg)

    # =========================================================================
    # Close
    # =========================================================================

    def _on_close(self):
        # Warn about mod-editor changes that were never saved into the mod
        if getattr(self, "_project", None) and self._project.is_dirty():
            if not ui_util.confirm(
                    self,
                    t("Unsaved mod changes"),
                    t("Your mod project has changes that weren't saved to the mod's .dat "
                      "(use an editor window's Save button to keep them).\n\nExit anyway?")):
                return
        # Flush any pending debounced settings save before exit
        if getattr(self, "_set_save_job", None):
            self.after_cancel(self._set_save_job)
            self._set_save_job = None
            for key, var in self._set_vars.items():
                self._settings[key] = var.get().strip()
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
    app = ModManagerApp()
    app.mainloop()

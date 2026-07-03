# Third-Party Notices

RUSE Mod Manager is licensed under the **GNU General Public License v3.0** (see `LICENSE`). It bundles
and uses the third-party software below; every component's license is GPL-3.0-compatible, and each
license travels with its component. This file summarizes the notices and points to the full texts.

## CPython 2.5.1 — bundled interpreter (PSF License)

The packaged application and the source tree include the **CPython 2.5.1** interpreter at
`ruse_mod_engine/python251/`, bundled because R.U.S.E embeds Python 2.5.1 — recompiling the game's
mission scripts (`.xyz`) requires that exact interpreter to produce engine-loadable bytecode.

> Copyright © 2001-2007 Python Software Foundation; All Rights Reserved
> Copyright © 2000 BeOpen.com; Copyright © 1995-2001 CNRI; Copyright © 1991-1995 Stichting Mathematisch Centrum

Distributed under the **Python Software Foundation License Agreement** (OSI-approved, GPL-compatible).
Full text: **`ruse_mod_engine/python251/LICENSE.txt`** (bundled beside the interpreter, in source and in
the exe's `_MEIPASS`).

## uncompyle6 and xdis — .xyz decompiler (GPL)

The application uses **uncompyle6** and **xdis** (both by Rocky Bernstein) to decompile the game's
compiled `.xyz` mission scripts to editable Python source. Both are licensed under the **GNU GPL**
(uncompyle6: GPL-3.0; xdis: GPL, or-later) — compatible with, and covered by, this project's GPL-3.0.

- Corresponding source (they are used unmodified): uncompyle6 → https://github.com/rocky/python-uncompyle6 ,
  xdis → https://github.com/rocky/python-xdis (both also on PyPI). Their `spark_parser` dependency is MIT.
- Because the whole of RUSE Mod Manager is GPL-3.0, bundling these GPL libraries in the packaged exe is
  compliant; the complete corresponding source of this work is this public repository.

## Permissively-licensed libraries

The packaged `.exe` (built with PyInstaller) also embeds general-purpose libraries under permissive,
GPL-compatible licenses — notably **NumPy** (BSD-3-Clause), **Pillow** (HPND/PIL), and MIT-licensed
helpers (six, openpyxl, spark_parser, colorama). See each project's upstream license for the full text.

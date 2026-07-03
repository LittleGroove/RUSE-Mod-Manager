# Bundled Python 2.5.1 — the game's own interpreter (script emit)

RUSE embeds **Python 2.5.1** (confirmed from the embedded interpreter). Its bytecode opcodes + marshal format are
exactly what the engine loads, so compiling mission scripts (`effetmap.xyz`) with this interpreter produces
**engine-loadable** code — proven in-game. This is the GOLDEN PATH for all script edits (see
`docs/operation_editor/05-editing-a-script.md`); in-place constant/byte patching is deprecated (unreliable).

## What lives here
- `compile_worker.py` — committed. Reads source on stdin, writes the raw marshalled code object on stdout.
- The interpreter binaries — **NOT committed** (see `.gitignore`). Install Python 2.5.1 here so `python.exe`
  exists: download `python-2.5.1.msi` (python.org/ftp/python/2.5.1/), admin-extract via `msiexec /a <msi> /qn
  TARGETDIR=<dir>`, then copy into this folder: `python.exe`, `pythonw.exe`, `python25.dll`, `msvcr71.dll`
  (the VC7.1 runtime 2.5.1 was built against), `Lib/`, `DLLs/`, **and `LICENSE.txt`** (the PSF license — see
  Redistribution below).
- `ruse_compile251.exe` — a renamed copy of `python.exe` used by `script_logic._launch_exe()` to dodge a
  parent debugger's pydevd auto-attach.

## Used by
`ruse_mod_engine/script_logic.py` → `recompile_source_to_xyz()` runs `python.exe compile_worker.py`. The
`docs/operation_editor/` corpus documents the format + the objective/condition/reward taxonomy.

## Redistribution / license
Python 2.5.1 is under the **PSF License**, which permits redistribution of the interpreter (binaries included)
provided the PSF copyright + license notice ship with it. Drop the interpreter's `LICENSE.txt` into this folder
(it's produced by the admin-extract, at the MSI's install root); `build.py::_python251_datas()` then bundles it
into the exe along with the rest of the interpreter.

## Packaging
`build.py::_python251_datas()` bundles this whole folder into the exe at `ruse_mod_engine/python251/` (matching
its dev-tree path, so `script_logic._py251_interpreter()` finds `python.exe` under `_MEIPASS`). The build logs
whether the interpreter was bundled; if it's absent on the build machine, `.xyz` recompile is simply unavailable
in that exe (decompile/preview still work).

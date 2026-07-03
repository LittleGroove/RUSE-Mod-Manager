# -*- coding: utf-8 -*-
# Python 2.5.1 COMPILE WORKER for RUSE .xyz mission scripts. SHIPPED with the modding engine.
# Run by ruse_mod_engine/script_logic.py under ruse_mod_engine/python251/python.exe.
# Contract: read Python SOURCE from stdin, compile it with the GAME'S OWN Python (2.5.1), write the
# raw marshalled code object to stdout (binary). The caller wraps that marshal in the XYZ0 container.
# argv[1] (optional) = co_filename to compile under (preserves the original script's co_filename).
#
# 2.5.1 is the version the game embeds, so its bytecode opcodes + marshal format are engine-loadable
# (proven in-game). This is the GOLDEN emit path; do NOT run this file under Python 3.
import sys
import marshal


def main():
    if sys.platform == "win32":
        import os
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    co_filename = sys.argv[1] if len(sys.argv) > 1 else "effetmap.py"
    source = sys.stdin.read()
    code = compile(source, co_filename, "exec")
    sys.stdout.write(marshal.dumps(code))


if __name__ == "__main__":
    main()

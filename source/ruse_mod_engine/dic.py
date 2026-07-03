"""RUSE .dic localization tables ('TRA' format).

Layout:  b"TRA\\0" + u32 count + count*[8-byte key][u32 byte-offset][u32 char-len] + UTF-16-LE blob.
Records are sorted ascending by key interpreted as a little-endian u64 (binary-search lookup).
String byte-offsets are absolute (into the file); some entries share an offset (dedup).

MP map names live in genlocalisation\\ww2\\localisation\\{dev,translations\\<lang>}\\flash_txt.dic,
keyed by TMultiMapInfo.Description (an 8-byte LocHash). Map-name keys = <u16 LE> + b"\\x3a\\xe5\\x05\\x00\\x00";
MP01(Blitz)=0x5042 .. MP30=0x5101.

`add_entry` preserves the original string blob verbatim (shifts the absolute offsets by the one
inserted record) so existing entries stay byte-exact — only the new key/string are added.
"""
import struct

MAGIC = b"TRA\x00"
MAP_KEY_SUFFIX = b"\x3a\xe5\x05\x00\x00\x00"   # bytes [2:8] shared by all MP map-name keys (+ 2-byte u16)

# RUSE localization languages = the folder codes under genlocalisation\ww2\localisation\translations\<code>\
# (+ \dev\). Ordered with English (us) first = the default. (code, friendly name).
LANGUAGES = [
    ("us", "English"),
    ("fr", "French"),
    ("ger", "German"),
    ("ita", "Italian"),
    ("spa", "Spanish"),
    ("pol", "Polish"),
    ("cz", "Czech"),
    ("ru", "Russian"),
    ("jpn", "Japanese"),
    ("sc", "Chinese (Simplified)"),
    ("dev", "Dev / default"),
]
LANG_NAME = dict(LANGUAGES)                     # code -> friendly name
LANG_CODE = {v: k for k, v in LANGUAGES}        # friendly name -> code


def lang_label(code: str) -> str:
    """Friendly label for a language code (falls back to the code itself)."""
    return LANG_NAME.get(code, code)


def map_name_key(u16: int) -> bytes:
    """8-byte Description/.dic key for an MP map-name slot (u16 past 0x5101 is unused)."""
    return struct.pack("<H", u16) + MAP_KEY_SUFFIX


def _records(b: bytes):
    # Accept any TRA version byte (shipped game .dic is "TRA\x01"; the 4th byte is a version, not part
    # of an exact magic). Writers preserve b[:4] verbatim so the version round-trips.
    if b[:3] != b"TRA":
        raise ValueError("not a TRA .dic")
    count = struct.unpack_from("<I", b, 4)[0]
    recs = []
    for i in range(count):
        o = 8 + i * 16
        off, ln = struct.unpack_from("<II", b, o + 8)
        recs.append((b[o:o + 8], off, ln))
    return count, recs, 8 + count * 16


def read(b: bytes):
    """[(key_bytes, string)] in file order."""
    _, recs, _ = _records(b)
    return [(k, b[o:o + ln * 2].decode("utf-16-le", "replace")) for k, o, ln in recs]


def keys(b: bytes) -> set:
    _, recs, _ = _records(b)
    return {k for k, _, _ in recs}


def reemit(b: bytes) -> bytes:
    """Re-serialize verbatim (used to verify the format is understood — must equal input)."""
    count, recs, blob_start = _records(b)
    out = bytearray(b[:4] + struct.pack("<I", count))
    for k, o, ln in recs:
        out += k + struct.pack("<II", o, ln)
    out += b[blob_start:]
    return bytes(out)


def add_entry(b: bytes, new_key: bytes, name: str) -> bytes:
    """Insert (new_key -> name) into a TRA .dic, keeping records sorted by LE-u64 key. Existing
    string bytes are preserved exactly (their absolute offsets shift by the one added record;
    the new UTF-16-LE string is appended after the blob). Raises if new_key already exists."""
    if len(new_key) != 8:
        raise ValueError("key must be 8 bytes")
    count, recs, blob_start = _records(b)
    if any(k == new_key for k, _, _ in recs):
        raise ValueError("key already present: %s" % new_key.hex())
    blob = b[blob_start:]
    enc = name.encode("utf-16-le")
    new_blob_start = 8 + (count + 1) * 16          # blob shifts by +16 (one record)
    new_off = new_blob_start + len(blob)           # new string appended after existing blob
    nk = int.from_bytes(new_key, "little")

    out = bytearray(b[:4] + struct.pack("<I", count + 1))
    placed = False
    for k, o, ln in recs:
        if not placed and int.from_bytes(k, "little") > nk:
            out += new_key + struct.pack("<II", new_off, len(name)); placed = True
        out += k + struct.pack("<II", o + 16, ln)
    if not placed:
        out += new_key + struct.pack("<II", new_off, len(name))
    out += blob + enc
    return bytes(out)


def set_entry(b: bytes, key: bytes, name: str) -> bytes:
    """Change an EXISTING key's string. The record count is unchanged, so every other entry's
    absolute offset stays valid; the new UTF-16-LE string is appended after the current blob and the
    edited record is repointed to it (the old bytes are left orphaned — harmless). Returns the new
    bytes. Raises KeyError if the key isn't present. No-op-safe if the name is unchanged."""
    if len(key) != 8:
        raise ValueError("key must be 8 bytes")
    count, recs, blob_start = _records(b)
    if not any(k == key for k, _, _ in recs):
        raise KeyError("key not present: %s" % key.hex())
    blob = b[blob_start:]
    new_off = blob_start + len(blob)               # append point (count unchanged → blob_start same)
    out = bytearray(b[:4] + struct.pack("<I", count))
    for k, o, ln in recs:
        if k == key:
            out += k + struct.pack("<II", new_off, len(name))
        else:
            out += k + struct.pack("<II", o, ln)   # untouched: offset still valid (blob prefix kept)
    out += blob + name.encode("utf-16-le")
    return bytes(out)


def get_entry(b: bytes, key: bytes):
    """Return the string for key, or None if absent."""
    _, recs, _ = _records(b)
    for k, o, ln in recs:
        if k == key:
            return b[o:o + ln * 2].decode("utf-16-le", "replace")
    return None


def free_map_key(*dics) -> bytes:
    """A map-name key (suffix-shaped) not used in any of the given .dic byte blobs."""
    used = set()
    for b in dics:
        used |= keys(b)
    u16 = 0x5102
    while map_name_key(u16) in used:
        u16 += 1
    return map_name_key(u16)


def baseunite_paths(project) -> dict:
    """Map a project's baseunite.dic entries by language code -> archive entry path. Same logic the
    units editor uses (units_editor.py:401-421); pulled here so cloning code doesn't reimplement it."""
    import re
    out = {}
    try:
        paths = project.entry_paths("loc", "baseunite.dic")
    except Exception:
        return out
    for p in paths:
        pl = p.replace("/", "\\").lower()
        m = re.search(r"translations\\([^\\]+)\\baseunite", pl)
        code = m.group(1) if m else ("dev" if "\\dev\\" in pl else None)
        if code:
            out[code] = p
    return out


def add_baseunite_entries(project, key: bytes, name: str) -> int:
    """Write `name` under `key` into every language's baseunite.dic on `project`. Marks the loc dat
    dirty so save_all() persists it. Skips languages where the key is already present (idempotent).
    Returns the number of dics actually mutated."""
    n = 0
    for _lang, path in baseunite_paths(project).items():
        try:
            blob = project.get_raw("loc", path)
        except Exception:
            continue
        if key in keys(blob):
            continue
        project.set_raw("loc", path, add_entry(blob, key, name))
        n += 1
    return n

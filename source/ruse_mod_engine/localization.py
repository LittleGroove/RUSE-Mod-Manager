"""Localization index over a project's .dic tables — the layer under the Raw editor's LocHash manager.

A LocHash (8-byte value on properties like NameInMenuToken, TMultiMapInfo.Description, an operation
objective's Text) is an OPAQUE KEY into the game's per-language text tables (.dic, "TRA" format —
see dic.py).  At runtime the game reads the hash, then looks up dic[hash] for the current language to
get the words to show.  Editing the hash bytes is almost never what a modder wants; editing the .dic
ENTRY the hash points at (keeping the hash) is.

This module builds a cross-file index from a set of (path, blob) .dic sources so the UI can:
  - resolve a LocHash to its string in every language it exists in,
  - search all strings (to re-point a property at an existing entry),
  - and compute the .dic edits (per language) for changing text / adding a new entry.

It is pure: the UI supplies (path, blob) pairs read from the store and applies the returned blobs
back via the store.  Kept UI-free so it's unit-testable.
"""
import hashlib
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from . import dic as dic_mod

# Language code from a .dic archive path: ...\translations\<code>\file.dic, or \dev\ for the default.
_LANG_RE = re.compile(r"translations[\\/]([^\\/]+)[\\/]", re.IGNORECASE)


def lang_from_path(path: str) -> str:
    """Language code for a .dic path (us/fr/ger/.../dev), or 'dev' if none is recognisable."""
    m = _LANG_RE.search(path)
    if m:
        return m.group(1).lower()
    pl = path.replace("/", "\\").lower()
    if "\\dev\\" in pl:
        return "dev"
    return "dev"


def mint_key(name: str) -> bytes:
    """Deterministic 8-byte LocHash for a NEW string (truncated MD5 of the text) — the same scheme
    clone.py uses when minting a cloned descriptor's NameInMenuToken.  Two different strings collide
    only on an MD5-64 collision (astronomically unlikely); an identical string maps to a stable key."""
    return hashlib.md5(name.encode("utf-8")).digest()[:8]


class LocEntry:
    __slots__ = ("dat_key", "path", "lang", "key", "string")

    def __init__(self, dat_key, path, lang, key, string):
        self.dat_key = dat_key
        self.path = path
        self.lang = lang
        self.key = key          # 8 raw bytes
        self.string = string

    @property
    def key_hex(self) -> str:
        return self.key.hex()


class LocIndex:
    """In-memory index over a set of .dic sources.

    `sources` is an iterable of (dat_key, path, blob).  Blobs that don't parse as TRA .dic are
    skipped (they're just other files).  Nothing is mutated here — edit helpers return NEW blobs the
    caller writes back through the store.
    """

    def __init__(self, sources):
        self.entries: List[LocEntry] = []
        self._by_key: Dict[bytes, List[LocEntry]] = defaultdict(list)
        self._blob: Dict[Tuple[str, str], bytes] = {}     # (dat_key, path) -> blob
        for dat_key, path, blob in sources:
            try:
                recs = dic_mod.read(blob)
            except Exception:
                continue
            self._blob[(dat_key, path)] = blob
            lang = lang_from_path(path)
            for k, s in recs:
                e = LocEntry(dat_key, path, lang, k, s)
                self.entries.append(e)
                self._by_key[k].append(e)

    # ── lookups ──────────────────────────────────────────────────────────────────────────────────
    def resolve(self, key: bytes) -> List[LocEntry]:
        """Every entry (across languages/files) whose key == `key`."""
        return list(self._by_key.get(bytes(key), ()))

    def langs_for(self, key: bytes) -> Dict[str, LocEntry]:
        """lang code -> entry for `key` (first file wins if a language has it twice)."""
        out: Dict[str, LocEntry] = {}
        for e in self.resolve(key):
            out.setdefault(e.lang, e)
        return out

    def string_for(self, key: bytes, prefer=("us", "dev")) -> Optional[str]:
        """Best single display string for `key` — English/dev first, else any language."""
        langs = self.langs_for(key)
        for p in prefer:
            if p in langs:
                return langs[p].string
        return next(iter(langs.values())).string if langs else None

    def has_key(self, key: bytes) -> bool:
        return bytes(key) in self._by_key

    def dic_paths(self) -> List[Tuple[str, str]]:
        """All (dat_key, path) .dic sources indexed."""
        return list(self._blob.keys())

    def blob(self, dat_key: str, path: str) -> Optional[bytes]:
        return self._blob.get((dat_key, path))

    def available_langs(self) -> List[str]:
        """Language codes present across all entries, ordered by dic.LANGUAGES then any extras."""
        present = {e.lang for e in self.entries}
        order = [c for c, _ in dic_mod.LANGUAGES]
        return [c for c in order if c in present] + sorted(present - set(order))

    def string_in(self, key: bytes, lang: str, fallback=("dev", "us")) -> Optional[str]:
        """String for `key` in `lang`, falling back through `fallback` then any language. None if the
        key isn't present at all."""
        langs = self.langs_for(key)
        if lang in langs:
            return langs[lang].string
        for f in fallback:
            if f in langs:
                return langs[f].string
        return next(iter(langs.values())).string if langs else None

    def search(self, text: str, limit: int = 500) -> List[LocEntry]:
        """Entries whose string OR key-hex contains `text` (case-insensitive).  Deduplicated by
        (key, string) so the same translated line across files isn't listed many times."""
        q = (text or "").lower().strip()
        seen = set()
        out: List[LocEntry] = []
        for e in self.entries:
            if q and q not in e.string.lower() and q not in e.key_hex:
                continue
            sig = (e.key, e.string)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(e)
            if len(out) >= limit:
                break
        return out

    # ── edits (return NEW blobs; caller writes them back) ──────────────────────────────────────────
    def set_text(self, key: bytes, lang_to_text: Dict[str, str]) -> Dict[Tuple[str, str], bytes]:
        """For each (lang -> new text), rewrite that language's .dic entry for `key`.  Returns
        {(dat_key, path): new_blob} for every file actually changed (unchanged text is skipped).
        Only languages where the key already exists are touched."""
        out: Dict[Tuple[str, str], bytes] = {}
        langs = self.langs_for(key)
        for lang, text in lang_to_text.items():
            e = langs.get(lang)
            if e is None or e.string == text:
                continue
            blob = self._blob.get((e.dat_key, e.path))
            if blob is None:
                continue
            out[(e.dat_key, e.path)] = dic_mod.set_entry(blob, key, text)
        return out

    def add_entry(self, target_paths: List[Tuple[str, str]], key: bytes,
                  text: str) -> Dict[Tuple[str, str], bytes]:
        """Add (key -> text) to each of the given (dat_key, path) .dic files (skipping any that
        already have the key).  Returns {(dat_key, path): new_blob} for the files changed."""
        out: Dict[Tuple[str, str], bytes] = {}
        for dat_key, path in target_paths:
            blob = self._blob.get((dat_key, path))
            if blob is None:
                continue
            try:
                if key in dic_mod.keys(blob):
                    continue
                out[(dat_key, path)] = dic_mod.add_entry(blob, key, text)
            except Exception:
                continue
        return out

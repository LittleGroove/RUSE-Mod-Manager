"""Single source of truth for the app's visual theme.

The "Field Operations" military palette — dark navy command-room steel, scratched silver-white text,
military gold — plus the Courier terminal fonts.  Every window, tab and editor imports its colours and
fonts from HERE so the whole app reads as one system.

Before this module the palette was copy-pasted into ~14 files, and had drifted into TWO shades: the
mod-manager + main editors used the values below, while the operation editor and scenario tabs had
their own slightly different navy/gold and used Segoe UI — so those windows visibly didn't match, and a
single colour tweak meant editing 14 files.  Change a value here and it lands everywhere.

ui_util's themed pop-up dialogs are pointed at these same values by ``mod_manager._apply_theme`` (which
forwards them to ``ui_util.configure_dialogs``), so dialogs match their parent windows too.

Naming is deliberately neutral (``PANEL`` not ``_R_BG_PANEL``): each module binds its own local names
to these, e.g. ``_R_BG_PANEL = theme.PANEL`` or ``_PANEL = theme.PANEL``, so nothing in the call sites
had to change during the migration.
"""

# ── Colours ───────────────────────────────────────────────────────────────────
BG        = "#08101c"   # deep navy black — window background
PANEL     = "#0e1a2a"   # navy blue — frame / panel background
WIDGET    = "#060d18"   # near-black navy — listbox / entry / log background
BORDER    = "#243a5c"   # steel blue border
GOLD      = "#c8a020"   # military gold — primary accent
GOLD_BRT  = "#e0c030"   # bright gold — headings / selected text
RED       = "#b03020"   # danger red
GREEN     = "#3a8030"   # success / OK green (status only)
TEXT      = "#ccd8e8"   # scratched silver-white — body text
DIM       = "#3e5878"   # muted steel blue — hint / secondary text
SEL_BG    = "#1a3060"   # selection background
SEL_FG    = "#e0c030"   # selection foreground
BTN       = "#122030"   # button face
BTN_ACT   = "#1e3250"   # button active / hover

# ── Fonts ─────────────────────────────────────────────────────────────────────
F      = ("Courier New", 9)            # body
FB     = ("Courier New", 9, "bold")    # emphasis
FHEAD  = ("Courier New", 10, "bold")   # headings
FS     = ("Courier New", 8)            # small / secondary

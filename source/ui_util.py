"""Shared Tk UI helpers — pixel-accurate, language-aware widget sizing.

Tk's ``width`` option on Entry/Combobox/Listbox is counted in "average characters" — specifically
the pixel width of "0" in the widget's font.  A fixed character count therefore UNDER-sizes for
languages whose glyphs are wider than Latin digits (Cyrillic, and especially CJK), which is why a
box sized "wide enough" in English truncates in Russian or Japanese.

These helpers measure the ACTUAL pixel width of the text in the widget's real font and convert back
to the character units Tk wants, so a box fits its content in every language.  Use ``fit_combobox``
for dropdowns and ``chars_for`` when you need the raw width for any character-width widget.
"""
import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


def notes_section(parent, save, *, panel_bg, widget_bg, text_fg, dim_fg, gold,
                  font, font_bold, initial="", label="Notes", hint=None, height=4):
    """Build a 'Notes' section (gold header + optional hint + multiline Text) into ``parent`` for
    issue #5.6.  Pre-filled with ``initial``; AUTO-SAVES via ``save(text)`` when the box loses focus
    or is destroyed (switching selection, changing tab, closing) — so a note is never lost and needs
    no extra click.  Returns the Text widget."""
    frame = tk.Frame(parent, background=panel_bg)
    frame.pack(fill="x", padx=2, pady=(10, 6))
    tk.Label(frame, text=label, anchor="w", background=panel_bg, foreground=gold,
             font=font_bold).pack(fill="x", padx=2, pady=(0, 2))
    if hint:
        tk.Label(frame, text=hint, anchor="w", justify="left", background=panel_bg,
                 foreground=dim_fg, font=font, wraplength=520).pack(fill="x", padx=2, pady=(0, 2))
    box = tk.Frame(frame, background=panel_bg)
    box.pack(fill="x", padx=2)
    txt = tk.Text(box, height=height, wrap="word", background=widget_bg, foreground=text_fg,
                  insertbackground=gold, font=font, relief="flat", highlightthickness=1,
                  highlightcolor="#243a5c", highlightbackground="#243a5c")
    sb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    if initial:
        txt.insert("1.0", initial)

    # Keep a captured copy of the text so we can save on <Destroy> WITHOUT touching the widget (it's
    # mid-teardown then and .get() raises).  Refresh the copy on every keystroke / focus-out.
    state = {"text": initial}

    def _capture(_=None):
        try:
            state["text"] = txt.get("1.0", "end-1c")
        except Exception:
            pass

    def _save(_=None):
        try:
            save(state["text"])
        except Exception:
            pass
    txt.bind("<KeyRelease>", _capture)
    txt.bind("<FocusOut>", lambda e: (_capture(), _save()))   # click away / switch / tab
    txt.bind("<Destroy>", _save)                              # panel rebuild / editor close
    return txt


def widget_font(widget, style_name=None, fallback="TkDefaultFont"):
    """The :class:`tkfont.Font` a widget actually renders with.  Tries the widget's own ``-font``,
    then the ttk style's font (``style_name``), then a fallback — so it works for plain tk widgets
    and themed ttk widgets alike."""
    f = ""
    try:
        f = widget.cget("font")
    except Exception:
        f = ""
    if not f and style_name:
        try:
            f = ttk.Style().lookup(style_name, "font")
        except Exception:
            f = ""
    f = f or fallback
    try:
        return tkfont.nametofont(f)
    except Exception:
        try:
            return tkfont.Font(font=f)
        except Exception:
            return tkfont.nametofont("TkDefaultFont")


def chars_for(widget, *texts, pad=3, style_name=None, minimum=0, maximum=80):
    """The Tk ``width`` (in average-character units) needed to show the WIDEST of ``texts`` in the
    widget's font — pixel-accurate, so it's right for any language/script.  Bounded by
    ``minimum``/``maximum``.  ``pad`` leaves a little breathing room (and covers the dropdown arrow
    on a combobox)."""
    fnt = widget_font(widget, style_name)
    unit = fnt.measure("0") or 7
    widest = max((fnt.measure(str(s)) for s in texts if s is not None), default=0)
    n = math.ceil(widest / unit) + pad
    return max(min(n, maximum), minimum)


def shade(hex_color, delta):
    """Lighten (``delta`` > 0) or darken (< 0) a ``#rrggbb`` colour by ``delta`` per channel,
    clamped to 0..255.  Used to derive a subtle zebra-stripe shade from a panel background."""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    clamp = lambda c: max(0, min(255, c + delta))
    return "#%02x%02x%02x" % (clamp(r), clamp(g), clamp(b))


def row_bg(index, base, delta=10):
    """Zebra-stripe background for row ``index``: ``base`` on even rows, a ``delta``-shaded variant on
    odd ones — so a user can follow a single row across to the value box (issue #5.2)."""
    return base if index % 2 == 0 else shade(base, delta)


def apply_row_bg(frame, bg):
    """Paint a row ``frame`` and its plain-tk children one ``bg`` so the whole row reads as a single
    stripe.  ttk widgets (Entry/Combobox) have no ``-background`` and are skipped — they keep their
    own field colour, which reads fine as the 'cell' sitting on the stripe."""
    for w in (frame, *frame.winfo_children()):
        try:
            w.configure(background=bg)
        except Exception:
            pass


def flow(container, widgets, gap=4, pady=2):
    """Lay ``widgets`` (already children of ``container``) left-to-right, wrapping to new rows when
    they don't fit ``container``'s width — so nothing is ever clipped off the edge in a small window
    (issue #5.3).  ``container`` should be ``pack(fill="x")`` and hold ONLY these widgets.

    Uses absolute ``place`` (NOT grid): grid shares column widths across rows, so a wide item on a
    wrapped row would inflate that column on the first row and shove the other items off-screen.
    Place positions each widget independently and we set the container's height to fit the rows."""
    def relayout(event=None):
        avail = event.width if event is not None else container.winfo_width()
        if avail <= 1:
            return
        x = y = rowh = 0
        for w in widgets:
            ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x > 0 and x + ww > avail:           # doesn't fit → wrap to next row
                x = 0
                y += rowh + pady
                rowh = 0
            w.place(x=x, y=y)
            x += ww + gap
            rowh = max(rowh, wh)
        container.configure(height=y + rowh)       # reserve room for all (wrapped) rows
    container.bind("<Configure>", relayout)
    container.after(60, relayout)


def equalize_panes(paned):
    """Set a ``ttk.PanedWindow``'s sashes so its panes START at EQUAL widths (1/n each).  Applied
    once, after the widget has a real size, so the user can still drag the sashes afterwards.  Pair
    with equal ``weight`` on each pane so a window resize keeps them proportional."""
    def apply(_tries=0):
        try:
            n = len(paned.panes())
            w = paned.winfo_width()
            if n >= 2 and w > 1:
                for i in range(1, n):
                    paned.sashpos(i - 1, round(w * i / n))
            elif _tries < 20:
                paned.after(50, lambda: apply(_tries + 1))   # not realized yet — retry briefly
        except Exception:
            pass
    paned.after(50, apply)


def with_scrollbars(holder, widget, hbar=True, vbar=True):
    """Lay ``widget`` (a Treeview/Listbox/Text/Canvas already created as a child of ``holder``) into
    ``holder`` with horizontal and/or vertical scrollbars, via grid — so wide content (long virtual
    paths, instance names, value strings) can be scrolled into view instead of being clipped off the
    right edge (issue #5.4).  ``holder`` must contain ONLY this widget + its scrollbars."""
    widget.grid(row=0, column=0, sticky="nsew")
    if vbar:
        vb = ttk.Scrollbar(holder, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=vb.set)
        vb.grid(row=0, column=1, sticky="ns")
    if hbar:
        hb = ttk.Scrollbar(holder, orient="horizontal", command=widget.xview)
        widget.configure(xscrollcommand=hb.set)
        hb.grid(row=1, column=0, sticky="ew")
    holder.rowconfigure(0, weight=1)
    holder.columnconfigure(0, weight=1)


def fit_tree_column(tree, col, texts, pad=24, minimum=80, maximum=1400, header=""):
    """Widen a ``ttk.Treeview`` column to fit the longest of ``texts`` (pixel-measured in the row
    font) and STOP it stretching, so the horizontal scrollbar can reveal the full content (issue
    #5.4).  Pass the column ``header`` text so a wide header isn't itself clipped."""
    fnt = widget_font(tree, "Treeview")
    widest = max((fnt.measure(str(s)) for s in texts), default=0)
    if header:
        widest = max(widest, fnt.measure(str(header)))
    tree.column(col, width=max(min(widest + pad, maximum), minimum), stretch=False)


def stripe_treeview(tree, base, delta=10):
    """Set up zebra-stripe row tags on a ``ttk.Treeview`` (issue #5.2).  Call ``retag_treeview`` after
    (re)populating it to apply them.  Pass the Treeview's row background as ``base``."""
    try:
        tree.tag_configure("evenrow", background=base)
        tree.tag_configure("oddrow", background=shade(base, delta))
    except Exception:
        pass


def retag_treeview(tree):
    """Re-apply alternating even/odd row tags to every current row, PRESERVING any other tags a row
    carries (e.g. a 'modified' highlight)."""
    try:
        for i, iid in enumerate(tree.get_children("")):
            tags = [tg for tg in tree.item(iid, "tags") if tg not in ("evenrow", "oddrow")]
            tags.append("evenrow" if i % 2 == 0 else "oddrow")
            tree.item(iid, tags=tags)
    except Exception:
        pass


def fit_combobox(combo, values=None, pad=3, minimum=12, maximum=60):
    """Size a ``ttk.Combobox`` so its longest value (or each of ``values``) is fully readable in the
    box and its drop-down list, in whatever language is active.  Safe no-op on any Tk error."""
    try:
        if values is None:
            values = combo.tk.splitlist(combo.cget("values"))
        combo.configure(width=chars_for(combo, *values, pad=pad, style_name="TCombobox",
                                        minimum=minimum, maximum=maximum))
    except Exception:
        pass

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

try:                                     # dialogs want translated button labels ("OK", "Yes", …)
    from i18n import t as _t
except Exception:                        # i18n not importable (isolated tests) → identity fallback
    def _t(key, /, **fmt):
        return key.format(**fmt) if fmt else key


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


# ── Vertical scrolling — one wrapper + correct mouse-wheel routing (issue #12) ────────────────────
# Two bugs this fixes everywhere:
#   1. content that FITS the viewport must NOT scroll (it used to drift up/down — "hurts the brain").
#   2. the wheel must scroll the widget actually UNDER THE POINTER — an inner Text / Listbox / Treeview
#      (notes box, a dropdown's list, any sub-list) scrolls ITSELF, never the outer scroll-canvas.
_WHEEL_BOUND = set()    # Tcl interpreters that already have the global <MouseWheel> router
_WHEEL_ANCHOR = [None]  # a live root widget for winfo_containing — bind_all sometimes delivers
                        # event.widget as an unresolved PATH STRING (no winfo_containing)


def _yview_overflows(w):
    """True if a yview-capable widget can actually scroll (its content overflows the viewport)."""
    try:
        first, last = w.yview()
    except (tk.TclError, ValueError, TypeError):
        return False
    return (first, last) != (0.0, 1.0)


def _canvas_overflows(c):
    """True if a scroll-canvas's content is taller than its visible height."""
    try:
        bb = c.bbox("all")
    except tk.TclError:
        return False
    return bool(bb) and (bb[3] - bb[1]) > c.winfo_height()


def _dismiss_overlay_if_outside(anchor, pointer_widget):
    """If a dropdown/popup currently holds the grab and the pointer is NOT over it, dismiss it.

    Overlays (ttk Combobox popdowns, tk Menus) are separate toplevels anchored to SCREEN coords, so
    when the background scrolls they'd float disconnected from the widget that opened them.  Closing
    them on a background scroll is the standard, less-confusing behaviour (issue #12 follow-up).

    Queried at the TCL level — a Combobox popdown is a window Tkinter can't resolve to a widget
    (`grab_current()` raises KeyError), so we work with window-path strings throughout."""
    try:
        gname = anchor.tk.eval("grab current").strip()   # plain window-path string ("" if none)
    except tk.TclError:
        return
    if not gname:
        return
    # Pointer inside the popup itself? leave it — it scrolls its own list.
    pw = str(pointer_widget) if pointer_widget is not None else ""
    if pw and (pw == gname or pw.startswith(gname + ".")):
        return
    try:
        if gname.endswith(".popdown"):                       # ttk Combobox dropdown
            anchor.tk.call("ttk::combobox::Unpost", gname[:-len(".popdown")])
            return
        anchor.tk.call("grab", "release", gname)             # generic popup / menu
        cls = str(anchor.tk.call("winfo", "class", gname))
        if cls == "Menu":
            anchor.tk.call(gname, "unpost")
        elif cls in ("Toplevel", "Menubutton"):
            anchor.tk.call("wm", "withdraw", gname)
    except tk.TclError:
        try:
            anchor.tk.call("grab", "release", gname)
        except tk.TclError:
            pass


def _global_wheel(event):
    """Route a wheel turn to the nearest scrollable ancestor of the widget UNDER THE POINTER.  A
    target that overflows scrolls and consumes the event; one that FITS is skipped so the event
    falls through to an outer scrollable; if nothing overflows, nothing moves (locked).  Bound once
    per interpreter on the 'all' tag, so it covers every scrollable in the app uniformly."""
    if not event.delta:
        return
    # bind_all may hand us event.widget as a raw path STRING (no winfo_containing) — fall back to a
    # stored live root widget for the geometry query (winfo_containing works from ANY widget).
    anchor = event.widget if hasattr(event.widget, "winfo_containing") else _WHEEL_ANCHOR[0]
    if anchor is None:
        return
    try:
        w = anchor.winfo_containing(event.x_root, event.y_root)
    except (KeyError, tk.TclError, AttributeError):
        return
    # An open dropdown/menu would float out of place if the background scrolled under it — close it
    # (unless the pointer is over the dropdown itself, in which case it scrolls its own list).  Never
    # let a dismiss hiccup break scrolling.
    try:
        _dismiss_overlay_if_outside(anchor, w)
    except (tk.TclError, KeyError, AttributeError):
        pass
    step = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
    while w is not None:
        if getattr(w, "_uiutil_vscroll", False):
            if _canvas_overflows(w):
                w.yview_scroll(step, "units")
                return "break"
        elif isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)):
            if _yview_overflows(w):
                w.yview_scroll(step, "units")
                return "break"
        w = getattr(w, "master", None)


def make_scrollable(parent, bg=None):
    """A vertically-scrollable canvas wrapping an inner frame.  Pack/grid your content into the
    RETURNED inner frame (NOT ``parent``).  Content that fits the window stays pinned to the top, and
    the mouse wheel scrolls whatever the pointer is over (see _global_wheel).  ``bg`` (when given)
    colours the canvas + a tk.Frame inner to match a panel; otherwise the inner is a themed ttk.Frame.
    The inner stashes ``inner._canvas`` so callers can preserve scroll position across a re-render."""
    cbg = {"background": bg} if bg else {}
    canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, **cbg)
    canvas._uiutil_vscroll = True
    vbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    vbar.pack(side="right", fill="y")
    inner = tk.Frame(canvas, **cbg) if bg else ttk.Frame(canvas)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _sync(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        if not _canvas_overflows(canvas):
            canvas.yview_moveto(0)        # content fits → pin to the top (no phantom over-scroll)
    inner.bind("<Configure>", _sync)
    # Stretch inner to the canvas width so fill="x" content renders full-width, and re-check fit.
    canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win, width=e.width), _sync()))

    interp = parent.tk      # bind_all lives on the per-interpreter 'all' tag — register once
    try:
        _WHEEL_ANCHOR[0] = parent._root()    # stable widget for winfo_containing in the router
    except Exception:
        _WHEEL_ANCHOR[0] = parent.winfo_toplevel()
    if interp not in _WHEEL_BOUND:
        parent.winfo_toplevel().bind_all("<MouseWheel>", _global_wheel, add="+")
        _WHEEL_BOUND.add(interp)

    inner._canvas = canvas
    return inner


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
    """Paint a row ``frame`` and ALL its descendant plain-tk widgets one ``bg`` so the whole row reads
    as a single stripe (recurses, so nested sub-frames like the #6 conversion box stripe too).  ttk
    widgets (Entry/Combobox) have no ``-background`` and are skipped — they keep their own field
    colour, which reads fine as the 'cell' sitting on the stripe."""
    def _paint(w):
        try:
            w.configure(background=bg)
        except Exception:
            pass
        for c in w.winfo_children():
            _paint(c)
    _paint(frame)


def flow(container, widgets, gap=4, pady=2, right=None):
    """Lay ``widgets`` (already children of ``container``) left-to-right, wrapping to new rows when
    they don't fit ``container``'s width — so nothing is ever clipped off the edge in a small window
    (issue #5.3).  ``container`` should be ``pack(fill="x")`` and hold ONLY these widgets.

    ``right`` (optional) is a second list of widgets pinned to the RIGHT edge of the last row — they
    stay right-aligned as the window resizes.  If they'd overlap the left-flowed widgets in a narrow
    window they drop to their own new (still right-aligned) row instead of being clipped (issue #5.3).

    Uses absolute ``place`` (NOT grid): grid shares column widths across rows, so a wide item on a
    wrapped row would inflate that column on the first row and shove the other items off-screen.
    Place positions each widget independently and we set the container's height to fit the rows."""
    right = right or []
    all_widgets = list(widgets) + list(right)

    # Reserve the natural SINGLE-ROW size synchronously (placed children contribute NOTHING to the
    # container's requested size, and the wrap-layout below only runs on a deferred <Configure>).
    # Without this, a parent window measuring its content — e.g. themed_toplevel's min-size guardrail
    # — sees a zero-height button bar and lets the user shrink the window until these buttons are
    # clipped off (they look like they vanished).  Setting a width floor also means the window can't
    # be narrowed below where the buttons fit on one row, so they never need to wrap out of view.
    if all_widgets:
        try:
            nat_w = sum(w.winfo_reqwidth() for w in all_widgets) + gap * (len(all_widgets) - 1)
            one_row_h = max(w.winfo_reqheight() for w in all_widgets)
            container.configure(width=nat_w, height=one_row_h)
        except Exception:
            pass

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
        if right:
            total_r = sum(w.winfo_reqwidth() for w in right) + gap * (len(right) - 1)
            if x > 0 and x + total_r > avail:      # won't fit beside left widgets → own new row
                x = 0
                y += rowh + pady
                rowh = 0
            rx = avail
            for w in reversed(right):              # place right-to-left from the edge
                ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
                rx -= ww
                w.place(x=max(rx, 0), y=y)
                rx -= gap
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


# ── Hover tooltips + truncated value cells (clipping fix for the editor field rows) ───────────────
# Field rows show read-only value previews (current / default) in fixed-width cells.  A value wider
# than the cell used to be hard-cut and lost; now it's truncated with an ellipsis and the FULL value
# is revealed on hover.  So nothing is ever silently clipped — the row stays aligned and the data is
# one mouse-over away.

def truncate(text, width):
    """``text`` shortened to at most ``width`` characters, with a trailing ellipsis when it was cut.
    ``width`` <= 0 returns the text unchanged (no truncation)."""
    s = str(text)
    if width <= 0 or len(s) <= width:
        return s
    return s[:max(1, width - 1)] + "…"


def tooltip(widget, text, *, bg="#0e1a2a", fg="#ccd8e8", border="#243a5c",
            font=("Courier New", 9), delay=400, wraplength=560):
    """Attach a hover tooltip to ``widget``.  ``text`` may be a string OR a no-arg callable returning
    the current string (re-evaluated on every hover, so it tracks live edits).  An empty/blank result
    shows no tooltip.  Safe across widget teardown — the popup is cancelled and destroyed on leave,
    click, and <Destroy>."""
    state = {"tip": None, "after": None}

    def _resolve():
        try:
            return str(text() if callable(text) else text)
        except Exception:
            return ""

    def _show():
        state["after"] = None
        msg = _resolve().strip()
        if not msg or state["tip"] is not None:
            return
        try:
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry("+%d+%d" % (x, y))
            tk.Label(tip, text=msg, background=bg, foreground=fg, font=font, justify="left",
                     wraplength=wraplength, relief="solid", borderwidth=1,
                     highlightbackground=border, highlightthickness=1, padx=6, pady=3).pack()
            state["tip"] = tip
        except tk.TclError:
            state["tip"] = None

    def _schedule(_=None):
        _unschedule()
        try:
            state["after"] = widget.after(delay, _show)
        except tk.TclError:
            pass

    def _unschedule():
        if state["after"] is not None:
            try:
                widget.after_cancel(state["after"])
            except tk.TclError:
                pass
            state["after"] = None

    def _hide(_=None):
        _unschedule()
        if state["tip"] is not None:
            try:
                state["tip"].destroy()
            except tk.TclError:
                pass
            state["tip"] = None

    widget.bind("<Enter>", _schedule, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<ButtonPress>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")


def value_cell(parent, full_text, width, *, anchor="e", show_when_empty=True, **label_kw):
    """A read-only value Label truncated to ``width`` chars (ellipsis when cut) with a hover tooltip
    carrying the FULL value — the standard way to render a field row's current/default preview so a
    wide value is never lost off the edge.  ``label_kw`` are passed to the ``tk.Label`` (background,
    foreground, font...).  Returns the Label (not yet grid/pack-ed); the caller places it.  When
    ``full_text`` is empty and ``show_when_empty`` is False, returns ``None``."""
    full = "" if full_text is None else str(full_text)
    if not full and not show_when_empty:
        return None
    lab = tk.Label(parent, text=truncate(full, width), width=width, anchor=anchor, **label_kw)
    if len(full) > width > 0:
        tooltip(lab, full)
    return lab


# A deliberate user PICK — a mouse click or keyboard navigation. We bind the load to THESE, not to
# the <<ListboxSelect>>/<<TreeviewSelect>> virtual events: those fire for ANY selection change
# (programmatic, focus, and scroll-adjacent shifts), so binding the load to them let a mouse-wheel
# turn over the list quietly change which item was loaded. Binding to real input keeps "select" a
# click/keyboard action — spinning the wheel scrolls the view and never loads a different item.
_PICK_EVENTS = ("<ButtonRelease-1>", "<KeyRelease-Up>", "<KeyRelease-Down>",
                "<KeyRelease-Prior>", "<KeyRelease-Next>", "<KeyRelease-Home>",
                "<KeyRelease-End>", "<KeyRelease-Return>")


def debounce_load(widget, handler, *, on_peek=None, delay=45, events=_PICK_EVENTS):
    """Make a heavy selection-load on a Listbox/Treeview robust, responsive, and CLICK-driven.

    Triggered only by a real pick — a left-click or keyboard navigation (`events`), NOT the
    <<ListboxSelect>> virtual event — so scrolling the list with the mouse wheel never loads a
    different item; selection is a deliberate click/keyboard action. By the time these fire the
    widget's selection is already updated, so reading it gives the clicked row. Two things happen:

      • on_peek() — runs IMMEDIATELY on the pick (keep it cheap: set a 'Loading …' header). Gives
        instant visible confirmation that the click registered, before the slow load.
      • handler() — DEBOUNCED by `delay` ms and re-scheduled on each pick, so it runs only ONCE, for
        the item the selection SETTLES on (a click-drag or held arrow loads only the final row).
        handler reads the widget's own selection, so what loads always matches what's highlighted.

    Both are called with no args (they query the widget for its current selection), so the same
    handler can still be invoked directly (e.g. by tests) for a synchronous load. The picked selection
    is also PINNED: if anything drifts it during the debounce/load window (e.g. a stray wheel turn
    while the panel is still rendering), it is snapped back so the loaded item == the clicked one."""
    state = {"after": None, "sel": None}

    def _get():
        try:
            return ("lb", tuple(widget.curselection()))      # Listbox
        except (AttributeError, tk.TclError):
            pass
        try:
            return ("tv", tuple(widget.selection()))         # Treeview
        except (AttributeError, tk.TclError):
            return (None, ())

    def _set(snap):
        kind, sel = snap
        if not sel:
            return
        try:
            if kind == "lb":
                widget.selection_clear(0, "end")
                for i in sel:
                    widget.selection_set(i)
                widget.activate(sel[0])
            elif kind == "tv":
                widget.selection_set(sel)
        except tk.TclError:
            pass

    def _evt(_e=None):
        snap = _get()
        if not snap[1]:
            return                       # click landed on no row (e.g. empty area) — ignore
        state["sel"] = snap
        if on_peek is not None:
            try:
                on_peek()
            except Exception:
                pass
        if state["after"] is not None:
            try:
                widget.after_cancel(state["after"])
            except tk.TclError:
                pass
        state["after"] = widget.after(delay, _fire)

    def _fire():
        state["after"] = None
        # Pin to the PICKED row: if a stray scroll (or anything) drifted the selection while the panel
        # was loading, snap it back so what loads matches what was clicked.
        if state["sel"] is not None and _get() != state["sel"]:
            _set(state["sel"])
        try:
            handler()
        except tk.TclError:
            pass

    for ev in events:
        widget.bind(ev, _evt, add="+")


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


# ═════════════════════════════════════════════════════════════════════════════
# Themed message dialogs — ONE place for every popup
# ═════════════════════════════════════════════════════════════════════════════
# tkinter.messagebox draws NATIVE OS dialogs (grey Windows boxes) that ignore the app's dark-navy +
# gold theme, and popups are hard-coded in hundreds of places.  These helpers render the same kinds
# of prompts (info / error / warning / yes-no confirm / "here's the text I copied") in the app theme
# from a SINGLE place, so every popup looks and behaves the same.  Drop-in shapes for messagebox:
#     ui_util.info(parent, title, msg)          ← messagebox.showinfo
#     ui_util.error(parent, title, msg)         ← messagebox.showerror
#     ui_util.warning(parent, title, msg)       ← messagebox.showwarning
#     ui_util.confirm(parent, title, msg)->bool ← messagebox.askyesno
#     ui_util.show_text(parent, title, msg, body)  info + a scrollable read-only text block
# Call configure_dialogs(**palette) ONCE at startup so the colours/fonts match the app; until then
# a sensible dark default is used.

_DIALOG_THEME = {
    "panel_bg": "#0e1a2a", "widget_bg": "#060d18", "border": "#243a5c",
    "text": "#ccd8e8", "dim": "#3e5878", "gold": "#c8a020", "gold_brt": "#e0c030",
    "btn": "#122030", "btn_act": "#1e3250", "sel_bg": "#1a3060", "sel_fg": "#e0c030",
    "danger": "#e0554a",
    "font": ("Courier New", 9), "font_bold": ("Courier New", 9, "bold"),
    "font_head": ("Courier New", 10, "bold"),
}

# Per-kind accent glyph + colour key (into _DIALOG_THEME).
_DIALOG_KINDS = {
    "info":    ("ℹ", "gold"),      # ℹ
    "warning": ("⚠", "gold_brt"),  # ⚠
    "error":   ("✖", "danger"),    # ✖
    "confirm": ("?",      "gold_brt"),
}


def configure_dialogs(**palette):
    """Set the colours/fonts the themed dialogs use.  Call ONCE at app startup with the app palette so
    every popup matches the theme.  Only the keys you pass are applied; ``None`` values are ignored."""
    _DIALOG_THEME.update({k: v for k, v in palette.items() if v is not None})


def center_over(win, parent=None):
    """Move ``win`` so it is CENTRED over ``parent`` (its top-level window) — or on screen if there's
    no usable parent.  Only repositions; ``win``'s current size is kept.  This is the fix for popups
    opening in the monitor's top-left corner: call it once the window's content exists.  Safe no-op on
    any Tk error (e.g. the window was already destroyed)."""
    try:
        win.update_idletasks()
        w = win.winfo_width()
        h = win.winfo_height()
        if w <= 1 or h <= 1:                           # not realized yet → fall back to requested
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        top = parent.winfo_toplevel() if parent is not None else None
        if top is not None and top.winfo_ismapped() and top.winfo_width() > 1:
            x = top.winfo_rootx() + (top.winfo_width() - w) // 2
            y = top.winfo_rooty() + (top.winfo_height() - h) // 3    # a third down looks centred
        else:                                          # no parent → centre on the screen
            x = (win.winfo_screenwidth() - w) // 2
            y = (win.winfo_screenheight() - h) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")      # '+X+Y' with no WxH ⇒ move only, keep size
    except Exception:
        pass


def themed_toplevel(parent, title, *, size=None, min_size=None, resizable=False,
                    modal=True, on_escape=False):
    """Create the standard themed popup window and return it — the ONE base for every small dialog /
    edit window, so they all share the app look and always open CENTRED OVER THE APP WINDOW instead of
    the monitor's top-left corner.  Fill the returned Toplevel with your own content and buttons.

    Handles for you: theme background, title, ``transient`` (stays with the app window), optional modal
    ``grab``, fixed or content sizing, and centring (deferred until the content is built, with the
    window hidden until then so it doesn't flash at the top-left first).

    Args:
        size:      ``(w, h)`` fixed size; omit to size the window to its content.
        min_size:  ``(w, h)`` minimum size (``minsize``).
        resizable: allow the user to resize (default ``False``).
        modal:     ``grab_set`` so the popup blocks the parent until closed (default ``True``).
        on_escape: also bind Escape and the window ✕ to destroy the window (default ``False`` so a
                   dialog's existing close/cancel behaviour is preserved untouched)."""
    th = _DIALOG_THEME
    top = parent.winfo_toplevel() if parent is not None else None
    win = tk.Toplevel(top) if top is not None else tk.Toplevel()
    win.withdraw()                                     # hide until centred (no top-left flash)
    win.title(title)
    win.configure(background=th["panel_bg"])
    win.resizable(bool(resizable), bool(resizable))
    if size:
        win.geometry(f"{int(size[0])}x{int(size[1])}")
    if min_size:
        win.minsize(int(min_size[0]), int(min_size[1]))
    if top is not None:
        win.transient(top)                             # keep the popup attached to the app window
    if on_escape:
        win.bind("<Escape>", lambda _e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", win.destroy)

    # Guardrail: floor the window's MINIMUM size at what its content actually needs, so the user can
    # never shrink it small enough to clip/hide buttons or controls (they'd think a feature vanished
    # when they only made the window too small).  Resizing can then only ADD space.  Grow-only and
    # re-applied after deferred layout (e.g. ui_util.flow button bars settle on a timer) so late-sized
    # content is still fully accounted for.
    floor = {"w": 0, "h": 0}
    def _apply_minsize():
        try:
            win.update_idletasks()
            fw = max(win.winfo_reqwidth(), int(min_size[0]) if min_size else 0, floor["w"])
            fh = max(win.winfo_reqheight(), int(min_size[1]) if min_size else 0, floor["h"])
            fw = min(fw, win.winfo_screenwidth() - 40)     # never bigger than the screen (stay usable)
            fh = min(fh, win.winfo_screenheight() - 80)
            if fw > floor["w"] or fh > floor["h"]:         # grow-only; never shrink the floor
                floor["w"], floor["h"] = fw, fh
                win.minsize(fw, fh)                        # also grows a too-small window to fit content
                return True
        except Exception:
            pass
        return False

    def _reveal():                                     # after the caller has built the content
        _apply_minsize()
        center_over(win, top)                          # centre at the final (content-fitting) size
        win.deiconify()
        if modal:
            try:
                win.grab_set()
            except Exception:
                pass

        def _settle():                                 # deferred layouts (flow bars) have run by now
            if _apply_minsize():                       # if the floor grew, re-centre for the new size
                center_over(win, top)
        win.after(150, _settle)
    win.after_idle(_reveal)
    return win


def _dialog_button(parent, text, command, *, primary=False):
    th = _DIALOG_THEME
    return tk.Button(
        parent, text=text, command=command, cursor="hand2",
        background=(th["gold"] if primary else th["btn"]),
        foreground=(th["widget_bg"] if primary else th["text"]),
        activebackground=(th["gold_brt"] if primary else th["btn_act"]),
        activeforeground=(th["widget_bg"] if primary else th["gold_brt"]),
        relief="flat", borderwidth=0, highlightthickness=0,
        font=th["font_bold"], padx=16, pady=4, width=max(8, len(text) + 2))


def _run_dialog(parent, kind, title, message, buttons, *, default_index=-1, cancel_value=None,
                build_extra=None, min_width=360):
    """Build a modal, themed Toplevel and block until the user picks a button.

    ``buttons`` is a list of ``(label, value, primary)``, laid out right-aligned in list order (so the
    LAST entry sits rightmost — put the primary/affirmative action last).  Returns the chosen value.
    ``default_index`` is the button that Enter triggers and that gets focus (default: the last one).
    Closing via the window's ✕ or Escape returns ``cancel_value`` (defaults to the last button's
    value — the safe choice for a single-OK dialog).  Optional ``build_extra(content_frame)`` adds
    widgets (e.g. a text block) between the message and the buttons."""
    th = _DIALOG_THEME
    if cancel_value is None:
        cancel_value = buttons[-1][1]
    result = {"value": cancel_value}
    top = parent.winfo_toplevel() if parent is not None else None

    win = tk.Toplevel(top) if top is not None else tk.Toplevel()
    win.title(title)
    win.configure(background=th["panel_bg"])
    win.resizable(False, False)
    if top is not None:
        win.transient(top)

    def _close(value):
        result["value"] = value
        try:
            win.grab_release()
        except Exception:
            pass
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", lambda: _close(cancel_value))
    win.bind("<Escape>", lambda _e: _close(cancel_value))

    body = tk.Frame(win, background=th["panel_bg"])
    body.pack(fill="both", expand=True, padx=16, pady=14)

    # Header row: accent glyph + message.
    glyph, colour_key = _DIALOG_KINDS.get(kind, _DIALOG_KINDS["info"])
    head = tk.Frame(body, background=th["panel_bg"])
    head.pack(fill="x")
    tk.Label(head, text=glyph, background=th["panel_bg"], foreground=th[colour_key],
             font=("Courier New", 22, "bold")).pack(side="left", anchor="n", padx=(0, 12))
    tk.Label(head, text=message, background=th["panel_bg"], foreground=th["text"],
             font=th["font"], justify="left", wraplength=460, anchor="w").pack(
        side="left", fill="x", expand=True)

    if build_extra is not None:
        build_extra(body)

    # Button bar: right-aligned, list order left→right (last entry sits rightmost).
    bar = tk.Frame(body, background=th["panel_bg"])
    bar.pack(fill="x", pady=(14, 0))
    default_index %= len(buttons)                      # -1 → last button
    focus_btn = None
    for i, (label, value, primary) in reversed(list(enumerate(buttons))):
        b = _dialog_button(bar, label, (lambda v=value: _close(v)), primary=primary)
        b.pack(side="right", padx=(8, 0))
        if i == default_index:
            focus_btn = b
            win.bind("<Return>", lambda _e, v=value: _close(v))

    win.update_idletasks()
    w = max(min_width, win.winfo_reqwidth())
    win.geometry(f"{w}x{win.winfo_reqheight()}")       # enforce the min width; keep natural height
    center_over(win, top)                              # then centre over the app window

    win.grab_set()
    # Force the dialog to the front.  Critical when the parent window is still WITHDRAWN (e.g. the
    # startup auto-update prompt, shown before the main window is mapped): a transient of a hidden
    # window has no taskbar button, so if it opened behind another app the user would have no way to
    # reach it and the app would appear hung ("running, no window").  lift + a brief topmost pulse
    # guarantees it surfaces; we drop topmost again so it doesn't stay pinned over everything.
    try:
        win.lift()
        win.attributes("-topmost", True)
        win.focus_force()
        win.after(300, lambda: win.winfo_exists() and win.attributes("-topmost", False))
    except Exception:
        pass
    if focus_btn is not None:
        focus_btn.focus_set()
    win.wait_window()
    return result["value"]


def info(parent, title, message):
    """Themed replacement for ``messagebox.showinfo``.  One OK button."""
    return _run_dialog(parent, "info", title, message, [(_t("OK"), True, True)])


def warning(parent, title, message):
    """Themed replacement for ``messagebox.showwarning``.  One OK button."""
    return _run_dialog(parent, "warning", title, message, [(_t("OK"), True, True)])


def error(parent, title, message):
    """Themed replacement for ``messagebox.showerror``.  One OK button."""
    return _run_dialog(parent, "error", title, message, [(_t("OK"), True, True)])


def confirm(parent, title, message, *, yes=None, no=None, danger=False):
    """Themed replacement for ``messagebox.askyesno`` — returns ``True`` for yes, ``False`` for no.
    ``yes``/``no`` override the button labels; ``danger=True`` styles it as an error (destructive)."""
    kind = "error" if danger else "confirm"
    return _run_dialog(parent, kind, title, message,
                       [(no or _t("No"), False, False), (yes or _t("Yes"), True, True)],
                       default_index=1, cancel_value=False)


def show_text(parent, title, message, body, *, ok_label=None, height=16, width=72):
    """Info dialog that ALSO shows a block of monospace text (read-only, scrollable, pre-selected) —
    e.g. the load order that was just copied to the clipboard.  One OK button."""
    th = _DIALOG_THEME

    def build(content):
        holder = tk.Frame(content, background=th["panel_bg"])
        holder.pack(fill="both", expand=True, pady=(12, 0))
        txt = tk.Text(holder, height=height, width=width, wrap="none",
                      background=th["widget_bg"], foreground=th["text"],
                      selectbackground=th["sel_bg"], selectforeground=th["sel_fg"],
                      insertbackground=th["gold"], relief="flat",
                      highlightthickness=1, highlightcolor=th["border"],
                      highlightbackground=th["border"], font=th["font"])
        sb = ttk.Scrollbar(holder, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", body)
        txt.configure(state="disabled")                # read-only, but still selectable/copyable

    return _run_dialog(parent, "info", title, message,
                       [(ok_label or _t("OK"), True, True)], build_extra=build, min_width=440)

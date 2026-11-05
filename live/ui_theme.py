"""B37.3.9.10 · UI theme: readable Korean system fonts + rounded panels.

Font policy
-----------
The application prefers the user-selected installed fonts:
* body: Pyeojin Gothic Medium (fallback Pyeojin Gothic)
* title/emphasis: Paperlogy 6 SemiBold (fallback Paperlogy 7 Bold)

Font binaries are not required for startup. If those families are not installed,
Windows falls back to Malgun Gothic. Rounded panel/theme behavior is unchanged.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "assets" / "fonts"
BODY_FAMILY = "Pyeojin Gothic Medium"
TITLE_FAMILY = "Paperlogy 6 SemiBold"
BODY_FALLBACKS = ("Pyeojin Gothic", "Malgun Gothic", "Segoe UI")
TITLE_FALLBACKS = ("페이퍼로지 6 SemiBold", "Paperlogy 7 Bold", "Paperlogy 5 Medium", "Paperlogy", "Malgun Gothic", "Segoe UI")
FALLBACK_BODY = "Malgun Gothic" if sys.platform == "win32" else "Segoe UI"

# Palette taken from the approved layout boards.
PAGE_BG = "#13161b"
PANEL_BG = "#1c2128"
PANEL_BORDER = "#2b313a"
CARD_BG = "#242a32"
CARD_BORDER = "#323943"
TEXT = "#e8eaed"
MUTED = "#9aa3ad"
ORANGE = "#f0a04b"
RED = "#f07167"
BLUE = "#7fb8e6"
GREEN = "#79c99e"
HILITE_BG = "#3a2a16"
HILITE_BORDER = "#d9963d"

# B37.4.34 semantic state tokens.  Components should consume these names
# instead of inventing new shades.  Color expresses state; marker shape
# expresses role (first/additional/collapse/recovered).
STATE_SUCCESS = GREEN
STATE_FAILURE = RED
STATE_WARNING = ORANGE
STATE_CANDIDATE = "#8f98a3"
STATE_UNKNOWN = MUTED
STATE_INFO = BLUE
STATE_INACTIVE = "#6f7883"
RESULT_KILL = BLUE
RESULT_WIPE = TEXT
RESULT_EARLY_WIPE = STATE_INACTIVE

# --------------------------------------------------------------------------
# B37.4 · Design system: fixed landscape design canvas + uniform scale.
#   The approved layout boards were drawn on a 1440 × 900 canvas.  The app
#   window is that canvas multiplied by SCALE (fit to the monitor work area,
#   e.g. QHD 2560 × 1392 → 1.54).  All component metrics are written in design
#   px and converted with px(); fonts are point sizes scaled by `tk scaling`.
# --------------------------------------------------------------------------
DESIGN_W = 1440
DESIGN_H = 900
SIDEBAR_W = 288          # design px (board 1: 278/1389 of the canvas)
SCALE = 1.0

# spacing / radius tokens (design px)
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
RADIUS = {"panel": 10, "card": 8, "chip": 6, "button": 9}
# type ramp (points before scaling)
TYPE = {"display": 21, "title": 12, "h3": 11, "body": 10, "caption": 9, "micro": 8}


def fit_scale(work_w: int, work_h: int, *, lo: float = 1.0, hi: float = 2.0) -> float:
    """Uniform scale that fits the 1440×900 canvas into the work area."""
    try:
        s = min(float(work_w) / DESIGN_W, float(work_h) / DESIGN_H)
    except Exception:
        s = 1.0
    s = max(lo, min(hi, s))
    return round(s - 0.004, 2) if s > lo else lo


def set_scale(value: float) -> None:
    global SCALE
    SCALE = max(0.5, float(value or 1.0))


def px(n):
    """Design px → screen px (ints, tuples and None pass through)."""
    if n is None:
        return None
    if isinstance(n, (tuple, list)):
        return type(n)(px(x) for x in n)
    try:
        return int(round(float(n) * SCALE))
    except Exception:
        return n


_registered = False


_fonts_loaded = False
BUNDLED_FONT_FILES = ("PyeojinGothic-Medium.ttf", "Paperlogy-6SemiBold.ttf")


def _bundled_font_dirs() -> list[Path]:
    dirs = [FONT_DIR]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        dirs.append(Path(frozen) / "assets" / "fonts")
    return dirs


# RC4.4: names under which the same fonts appear when a user installed them.
# (family aliases, weight token): a different weight of the same family
# (e.g. Paperlogy 4 Regular) is a different face and does not count.
_FONT_ALIASES = {
    "PyeojinGothic-Medium.ttf": (("pyeojin", "펴진고딕", "펴진 고딕"), "medium"),
    "Paperlogy-6SemiBold.ttf": (("paperlogy", "페이퍼로지"), "semibold"),
}
_FONTS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"


def _installed_font_entries() -> list[tuple[str, str]]:
    """(registry value name, file) of fonts installed per-user or machine-wide."""
    out: list[tuple[str, str]] = []
    try:
        import winreg
    except ImportError:
        return out
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _FONTS_KEY) as key:
                i = 0
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    out.append((str(name), str(value)))
                    i += 1
        except OSError:
            continue
    return out


def _installed_copy(filename: str, entries) -> bool:
    """True when the same font (by file name or family alias) is installed."""
    stem = filename.lower()
    aliases, weight = _FONT_ALIASES.get(filename, ((), ""))
    for name, value in entries:
        n = name.lower().replace("-", " ")
        v = str(value).replace("/", "\\").rsplit("\\", 1)[-1].lower()
        if v == stem:
            return True
        if any(a in n for a in aliases) and weight and weight in n.replace(" ", ""):
            return True
    return False


def register_bundled_fonts() -> bool:
    """Load the two bundled OFL fonts privately for THIS process only.

    Called at import time, before Tk creates any font.  FR_PRIVATE means no
    system font folder or registry is touched and nothing remains after exit.

    RC4.4: if the user has the same font INSTALLED in Windows, the private copy
    is skipped.  Registering one family twice (installed + private) made GDI
    measure and draw Hangul with different faces: clipped last syllables
    ("체크" -> "체"), overlapping glyphs and wide gaps in titles.

    Returns True when both fonts are available to this process (always True
    off Windows, where nothing needs registering).
    """
    global _registered, _fonts_loaded
    if _registered:
        return _fonts_loaded or sys.platform != "win32"
    _registered = True
    if sys.platform != "win32":
        return True  # nothing to register; Tk uses installed fonts
    try:
        entries = _installed_font_entries()
    except Exception:
        entries = []
    try:
        import ctypes
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        add_font = gdi32.AddFontResourceExW
        add_font.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        add_font.restype = ctypes.c_int
        available = 0
        for filename in BUNDLED_FONT_FILES:
            if _installed_copy(filename, entries):
                available += 1          # already installed: never register twice
                continue
            for folder in _bundled_font_dirs():
                fp = folder / filename
                if fp.is_file():
                    available += int(add_font(str(fp), 0x10, None) > 0)  # FR_PRIVATE
                    break
        _fonts_loaded = available == len(BUNDLED_FONT_FILES)
    except (OSError, AttributeError, ValueError):
        _fonts_loaded = False
    return _fonts_loaded

def resolve_families(root: tk.Misc) -> tuple[str, str]:
    """Return (body, title) using the preferred installed Korean families."""
    try:
        from tkinter import font as tkfont
        actual = list(tkfont.families(root))
        by_lower = {f.lower(): f for f in actual}
    except Exception:
        by_lower = {}

    def pick(primary: str, fallbacks: tuple[str, ...]) -> str:
        for name in (primary,) + tuple(fallbacks):
            hit = by_lower.get(name.lower())
            if hit:
                return hit
        return FALLBACK_BODY

    # Korean Windows may list a localized family name (e.g. "페이퍼로지 6 SemiBold")
    # in tk font families.  GDI still accepts the English family name, so when
    # the bundled files were registered we use the canonical names directly.
    if _fonts_loaded:
        return BODY_FAMILY, TITLE_FAMILY
    body = pick(BODY_FAMILY, BODY_FALLBACKS)
    title = pick(TITLE_FAMILY, TITLE_FALLBACKS)
    return body, title

def _round_rect(canvas: tk.Canvas, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return canvas.create_polygon(pts, smooth=True, splinesteps=12, **kw)


class RoundBox(tk.Frame):
    """Rounded container. Put children in ``.inner``.

    The rounded shape is drawn on a background Canvas placed behind ``inner``;
    ``inner`` is packed normally so height follows its content.
    """

    def __init__(self, master, *, fill=CARD_BG, outline=CARD_BORDER, outer=None, radius=10,
                 padx=12, pady=10, border=1, cursor=None):
        if outer is None:
            try:
                outer = master.cget("bg")
            except Exception:
                outer = PAGE_BG
        super().__init__(master, bg=outer, bd=0, highlightthickness=0, cursor=cursor or "")
        radius, padx, pady = px(radius), px(padx), px(pady)
        self._outer, self._fill, self._outline = outer, fill, outline
        self._radius, self._border = radius, border
        self._canvas = tk.Canvas(self, bg=outer, highlightthickness=0, bd=0, cursor=cursor or "")
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.inner = tk.Frame(self, bg=fill, cursor=cursor or "")
        self.inner.pack(fill="both", expand=True, padx=padx, pady=pady)
        self._shape = None
        self.bind("<Configure>", self._redraw, add="+")

    def configure(self, cnf=None, **kw):  # noqa: D401
        recolor = False
        if "bg" in kw or "background" in kw:
            self._fill = kw.pop("bg", kw.pop("background", self._fill))
            recolor = True
        if "highlightbackground" in kw:
            self._outline = kw.pop("highlightbackground")
            recolor = True
        kw.pop("highlightthickness", None)
        if recolor:
            try:
                self.inner.configure(bg=self._fill)
            except Exception:
                pass
            self._redraw()
        if cnf or kw:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def cget(self, key):
        if key in {"bg", "background"}:
            return self._fill
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def bind_all_children(self, sequence, func):
        for w in [self, self._canvas, self.inner] + list(self.inner.winfo_children()):
            w.bind(sequence, func, add="+")

    def _redraw(self, _e=None):
        c = self._canvas
        w = max(4, self.winfo_width())
        h = max(4, self.winfo_height())
        if self._shape is not None:
            c.delete(self._shape)
        b = self._border
        self._shape = _round_rect(c, b, b, w - b - 1, h - b - 1, self._radius,
                                  fill=self._fill, outline=self._outline, width=b)


class RoundPanel(RoundBox):
    """Section panel: title (left, bold) + subtitle (right, muted) + body."""

    def __init__(self, master, title="", subtitle="", *, title_font=None, body_font=None, **kw):
        kw.setdefault("fill", PANEL_BG)
        kw.setdefault("outline", PANEL_BORDER)
        kw.setdefault("padx", 16)
        kw.setdefault("pady", 14)
        super().__init__(master, **kw)
        head = tk.Frame(self.inner, bg=self._fill)
        head.pack(fill="x", pady=(0, px(10)))
        self.head = head
        self.title_var = tk.StringVar(value=title)
        self.subtitle_var = tk.StringVar(value=subtitle)
        self.title_label = tk.Label(head, textvariable=self.title_var, bg=self._fill, fg=TEXT,
                                    font=title_font or (TITLE_FAMILY, 12), anchor="w")
        self.title_label.pack(side="left")
        self.subtitle_label = tk.Label(head, textvariable=self.subtitle_var, bg=self._fill, fg=MUTED,
                                       font=body_font or (BODY_FAMILY, 9), anchor="e")
        self.subtitle_label.pack(side="right")
        self.body = tk.Frame(self.inner, bg=self._fill)
        self.body.pack(fill="both", expand=True)

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self.title_var.set(kw.pop("text"))
        if "subtitle" in kw:
            self.subtitle_var.set(kw.pop("subtitle"))
        return super().configure(cnf, **kw)

    config = configure

    def cget(self, key):
        if key == "text":
            return self.title_var.get()
        return super().cget(key)


class RoundLabelFrame(tk.Frame):
    """Drop-in for ttk.LabelFrame: the returned widget is the panel *body*;
    geometry calls are forwarded to the rounded outer panel."""

    def __init__(self, master, text="", padding=None, title_font=None, body_font=None, **_kw):
        self._outer = RoundPanel(master, text, "", title_font=title_font, body_font=body_font)
        super().__init__(self._outer.body, bg=PANEL_BG)
        tk.Frame.pack(self, fill="both", expand=True)

    # geometry → outer
    def pack(self, cnf=None, **kw):
        return self._outer.pack(cnf or {}, **kw)
    pack_configure = pack

    def pack_forget(self):
        return self._outer.pack_forget()

    def grid(self, cnf=None, **kw):
        return self._outer.grid(cnf or {}, **kw)
    grid_configure = grid

    def grid_remove(self):
        return self._outer.grid_remove()

    def grid_forget(self):
        return self._outer.grid_forget()

    def place(self, cnf=None, **kw):
        return self._outer.place(cnf or {}, **kw)

    def winfo_manager(self):
        return self._outer.winfo_manager()

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._outer.configure(text=kw.pop("text"))
        kw.pop("padding", None)
        if cnf or kw:
            return super().configure(cnf, **kw)
        return None
    config = configure

    def cget(self, key):
        if key == "text":
            return self._outer.cget("text")
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


PANEL_TYPES = (RoundBox, RoundLabelFrame)


def fix_panel_children(root: tk.Misc) -> None:
    """ttk children inside rounded panels get panel-coloured styles."""
    from tkinter import ttk

    def inside_panel(w):
        p = w.master
        while p is not None:
            if isinstance(p, PANEL_TYPES):
                return True
            if isinstance(p, tk.Tk):
                return False
            p = p.master
        return False

    def walk(w):
        for c in w.winfo_children():
            try:
                if isinstance(c, ttk.Frame) and not isinstance(c, ttk.LabelFrame) and inside_panel(c):
                    if str(c.cget("style")) in {"", "TFrame", "App.TFrame"}:
                        c.configure(style="Card.TFrame")
                elif isinstance(c, ttk.Label) and inside_panel(c):
                    st = str(c.cget("style"))
                    mapping = {"": "Card.TLabel", "TLabel": "Card.TLabel", "Muted.TLabel": "Muted.Card.TLabel",
                               "Header.TLabel": "CardTitle.TLabel", "Title.TLabel": "Title.Card.TLabel"}
                    if st in mapping:
                        c.configure(style=mapping[st])
                elif isinstance(c, ttk.Checkbutton) and inside_panel(c):
                    c.configure(style="Card.TCheckbutton")
                elif isinstance(c, ttk.Radiobutton) and inside_panel(c):
                    c.configure(style="Card.TRadiobutton")
                elif isinstance(c, ttk.Separator) and inside_panel(c):
                    c.configure(style="Card.TSeparator")
            except Exception:
                pass
            walk(c)

    walk(root)


class RoundButton(RoundBox):
    """Rounded button (board look). Supports configure(text=, state=, command=)."""

    STYLES = {
        "accent": {"fill": "#f0a04b", "outline": "#f0a04b", "fg": "#151515", "hover": "#f6b468"},
        "default": {"fill": "#1c2128", "outline": "#323943", "fg": "#e8eaed", "hover": "#252b33"},
    }

    def __init__(self, master, text="", command=None, kind="default", font=None, state="normal", outer=None, pady=8):
        st = self.STYLES.get(kind, self.STYLES["default"])
        self._st = st
        super().__init__(master, fill=st["fill"], outline=st["outline"], outer=outer, radius=9, padx=10, pady=pady, cursor="hand2")
        self._command = command
        self._state = state
        self.label = tk.Label(self.inner, text=text, bg=st["fill"], fg=st["fg"], font=font or (BODY_FAMILY, 10), cursor="hand2")
        self.label.pack(fill="x")
        for w in (self, self._canvas, self.inner, self.label):
            w.bind("<Button-1>", self._click, add="+")
            w.bind("<Enter>", lambda _e: self._hover(True), add="+")
            w.bind("<Leave>", lambda _e: self._hover(False), add="+")
        self._paint()

    def _paint(self, hover=False):
        st = self._st
        disabled = self._state == "disabled"
        fill = st["hover"] if (hover and not disabled) else st["fill"]
        if disabled:
            fill = "#1c2128" if st is self.STYLES["default"] else "#7a5a35"
        RoundBox.configure(self, bg=fill, highlightbackground=st["outline"] if not disabled else "#2b313a")
        if disabled:
            fg = "#5d6570" if st is self.STYLES["default"] else "#c9a983"
            if st is not self.STYLES["default"]:
                fill = "#4a3824"
                RoundBox.configure(self, bg=fill, highlightbackground="#5a4429")
        else:
            fg = st["fg"]
        self.label.configure(bg=fill, fg=fg)

    def _hover(self, on):
        self._paint(hover=on)

    def _click(self, _e=None):
        if self._state != "disabled" and callable(self._command):
            self._command()

    def invoke(self):
        self._click()

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self.label.configure(text=kw.pop("text"))
        if "command" in kw:
            self._command = kw.pop("command")
        if "state" in kw:
            self._state = str(kw.pop("state"))
            self._paint()
        kw.pop("style", None)
        if cnf or kw:
            return RoundBox.configure(self, cnf, **kw)
        return None

    config = configure

    def cget(self, key):
        if key == "text":
            return self.label.cget("text")
        if key == "state":
            return self._state
        return RoundBox.cget(self, key)

    def instate(self, spec):
        want_disabled = "disabled" in spec
        neg = "!disabled" in spec
        if neg:
            return self._state != "disabled"
        return (self._state == "disabled") == want_disabled



class ScrollPage(tk.Frame):
    """Vertically scrollable page body for the fixed landscape window.

    The widget itself is the *content* frame (children go here); the scroll
    container (``_outer``) is what the notebook manages.
    """

    def __init__(self, master, bg=PAGE_BG):
        from tkinter import ttk
        self._outer = tk.Frame(master, bg=bg)
        self._canvas = tk.Canvas(self._outer, bg=bg, highlightthickness=0, bd=0, yscrollincrement=px(24))
        self._bar = ttk.Scrollbar(self._outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_scroll)
        # GOLD SCROLL GUARD HF6: never hide the only manual navigation fallback.
        # Pack the bar first so it keeps its width during narrow-window relayout.
        self._bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        super().__init__(self._canvas, bg=bg)
        self._outer._inner = self
        self._win = self._canvas.create_window((0, 0), window=self, anchor="nw")
        self.bind("<Configure>", self._sync, add="+")
        self._canvas.bind("<Configure>", self._sync_width, add="+")
        self._extent_sync_pending = False
        # Dynamic Try cards change the requested inner height without necessarily
        # resizing the fixed-height Canvas window.  The inner <Configure> alone
        # therefore does NOT reliably update the scrollregion.  Watch child
        # geometry changes, then reconcile once after Tk finishes the layout.
        self._canvas.bind_all("<Configure>", self._guard_child_configure, add="+")
        self._canvas.bind_all("<Map>", self._guard_child_configure, add="+")
        # B37.4.4: install a permanent application-level wheel dispatcher for
        # every ScrollPage.  There are four ScrollPages in the notebook.  The
        # older enter/leave + bind_all/unbind_all design let one hidden page or
        # a child-widget transition steal/remove the global binding, which is
        # why the scrollbar thumb worked while the mouse wheel could stay dead.
        # Each dispatcher now acts only when the pointer is actually inside its
        # own page; hidden pages return immediately.  add="+" prevents pages
        # from overwriting one another's global handler.
        self._canvas.bind_all("<MouseWheel>", self._dispatch_wheel, add="+")
        self._canvas.bind_all("<Button-4>", self._dispatch_wheel, add="+")
        self._canvas.bind_all("<Button-5>", self._dispatch_wheel, add="+")

    def _on_scroll(self, lo, hi):
        # An initial (0,1) report is *not* proof that later dynamic cards fit.
        # In particular, refreshing a Try can temporarily report a short body.
        # Keep the vertical bar permanently available as a manual fallback.
        self._bar.set(lo, hi)
        if not self._bar.winfo_manager():
            self._bar.pack(side="right", fill="y")

    def _guard_child_configure(self, event):
        # Listen only to this page, not unrelated Tk windows or sidebars.
        widget = getattr(event, "widget", None)
        while widget is not None:
            if widget is self or widget is self._outer:
                self._schedule_extent_sync()
                break
            widget = getattr(widget, "master", None)

    def _schedule_extent_sync(self):
        if getattr(self, "_extent_sync_pending", False):
            return
        self._extent_sync_pending = True
        try:
            self._canvas.after_idle(self._sync_region_after_layout)
        except (RuntimeError, tk.TclError):
            self._extent_sync_pending = False

    def _sync(self, _e=None):
        self._schedule_extent_sync()

    def _sync_width(self, e):
        try:
            self._canvas.itemconfigure(self._win, width=e.width)
        except (RuntimeError, tk.TclError):
            return
        self._schedule_extent_sync()

    def _sync_region_after_layout(self):
        self._extent_sync_pending = False
        try:
            if not self._canvas.winfo_exists():
                return
            # reqheight can grow while the Canvas window remains fixed-height.
            # Expand that window before computing its scroll extent.
            h = max(int(self._canvas.winfo_height()), int(self.winfo_reqheight()))
            old_h = float(self._canvas.itemcget(self._win, "height") or 0)
            if abs(old_h - h) > 1:
                self._canvas.itemconfigure(self._win, height=h)
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except (AttributeError, RuntimeError, tk.TclError, ValueError):
            pass

    def _pointer_inside(self):
        try:
            w = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
            outer = self._outer
            while w is not None:
                if w is outer:
                    return True
                w = getattr(w, "master", None)
        except Exception:
            pass
        return False

    @staticmethod
    def _widget_has_own_vertical_wheel(widget):
        """Keep native wheel behavior for real text/tree/list widgets."""
        try:
            cls = str(widget.winfo_class())
        except Exception:
            return False
        return cls in {"Text", "Treeview", "Listbox"}

    def _dispatch_wheel(self, e):
        # Global handlers are installed for all notebook pages.  Only the page
        # physically under the pointer is allowed to consume this event.
        if not self._pointer_inside():
            return None
        if self._widget_has_own_vertical_wheel(getattr(e, "widget", None)):
            return None

        if getattr(e, "num", None) == 4:
            units = -3
        elif getattr(e, "num", None) == 5:
            units = 3
        else:
            try:
                delta = float(getattr(e, "delta", 0) or 0)
            except (TypeError, ValueError):
                delta = 0.0
            if not delta:
                return None
            # Some Windows precision mice/touchpads report deltas smaller than
            # 120.  int(delta / 120) therefore produced 0 and made the wheel
            # look completely dead.  Any non-zero delta must move at least one
            # step; ordinary ±120 wheels still move the historical 3 units.
            steps = max(1, int(round(abs(delta) / 120.0)))
            units = (-3 if delta > 0 else 3) * steps
        self._canvas.yview_scroll(units, "units")
        return "break"

    # Compatibility shim for older callers/tests.  Wheel ownership is no longer
    # toggled by enter/leave; the permanent dispatcher decides from the pointer.
    def _bind_wheel(self, _on):
        return None

    def _wheel(self, e):
        return self._dispatch_wheel(e)



def install_vertical_wheel_target(container, scrollable=None, *, units_per_step=3, preserve_native=True):
    """Route application mouse-wheel events to one custom vertical scroller.

    Tk sends ``<MouseWheel>`` to the child under the pointer.  A Canvas that
    contains a Frame therefore does *not* receive wheel events while the pointer
    is over labels/cards inside that embedded Frame.  This helper installs an
    additive application-level dispatcher and consumes the event only when the
    pointer is currently inside ``container``.

    ``scrollable`` defaults to ``container`` and must expose ``yview_scroll``.
    The callback is returned so callers may retain it for diagnostics/tests.
    """
    target = scrollable or container

    def pointer_inside():
        try:
            w = container.winfo_containing(container.winfo_pointerx(), container.winfo_pointery())
            while w is not None:
                if w is container:
                    return True
                w = getattr(w, "master", None)
        except Exception:
            pass
        return False

    def dispatch(event):
        if not pointer_inside():
            return None
        if preserve_native:
            try:
                if str(event.widget.winfo_class()) in {"Text", "Treeview", "Listbox"}:
                    return None
            except Exception:
                pass

        if getattr(event, "num", None) == 4:
            units = -int(units_per_step)
        elif getattr(event, "num", None) == 5:
            units = int(units_per_step)
        else:
            try:
                delta = float(getattr(event, "delta", 0) or 0)
            except (TypeError, ValueError):
                delta = 0.0
            if not delta:
                return None
            steps = max(1, int(round(abs(delta) / 120.0)))
            units = (-int(units_per_step) if delta > 0 else int(units_per_step)) * steps
        try:
            target.yview_scroll(units, "units")
        except Exception:
            return None
        return "break"

    container.bind_all("<MouseWheel>", dispatch, add="+")
    container.bind_all("<Button-4>", dispatch, add="+")
    container.bind_all("<Button-5>", dispatch, add="+")
    return dispatch


def install_scroll_notebook(nb) -> None:
    """Let a ttk.Notebook accept ScrollPage contents transparently."""
    orig_add, orig_select, orig_forget = nb.add, nb.select, nb.forget
    orig_tab, orig_index, orig_hide = nb.tab, nb.index, nb.hide

    def outer(w):
        return getattr(w, "_outer", w)

    nb.add = lambda child, **kw: orig_add(outer(child), **kw)
    nb.forget = lambda child: orig_forget(outer(child))
    nb.hide = lambda child: orig_hide(outer(child))
    nb.tab = lambda child, option=None, **kw: orig_tab(outer(child) if not isinstance(child, (int, str)) else child, option, **kw)
    nb.index = lambda child: orig_index(outer(child) if not isinstance(child, (int, str)) else child)

    def select(tab_id=None):
        if tab_id is None:
            cur = orig_select()
            try:
                w = nb.nametowidget(cur)
                inner = getattr(w, "_inner", None)
                return str(inner) if inner is not None else cur
            except Exception:
                return cur
        return orig_select(outer(tab_id) if not isinstance(tab_id, (int, str)) else tab_id)

    nb.select = select

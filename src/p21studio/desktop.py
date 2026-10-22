"""Minimal desktop app (tkinter): type a label, format it, queue it, print it.

Two pages: Print (editor, preview, queue) and History. Printing runs on one
background thread; the window only talks to it through queues, because tkinter
must only be touched from the main thread.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

from . import api, config, fonts
from .label import canvas_size, new_label, normalize

MEDIA = "14x40"
MARGIN = 4
HISTORY_MAX = 50
SIZES = ["Auto", "12", "16", "20", "24", "28", "32", "40", "48", "56", "64", "80", "96"]
ICON_FONT = Path(__file__).parent / "assets" / "lucide.ttf"

# Lucide icon font code points (lucide-static 0.460.0, ISC licence; see assets/LUCIDE-LICENSE).
ICONS = {
    "bold": 0xE061, "italic": 0xE100, "underline": 0xE19A, "printer": 0xE145,
    "list-plus": 0xE23F, "list-start": 0xE2E1, "x": 0xE1B2, "history": 0xE1F5,
    "repeat": 0xE14A, "sun": 0xE17C, "moon": 0xE122, "sun-moon": 0xE2B2,
    "bluetooth": 0xE060, "tag": 0xE183, "search": 0xE155,
}

# Palette: "Minimalism & Swiss" navy + green (ui-ux-pro search), with Will's house
# rules applied: muted low-contrast dark mode and fixed status colours.
PALETTES = {
    "light": {
        "bg": "#F8FAFC", "surface": "#FFFFFF", "fg": "#0F172A", "muted": "#475569",
        "border": "#E4E7EB", "hover": "#EEF1F5", "primary": "#1E3A5F", "accent": "#059669",
        "toggle": "#DCE6F2", "paper": "#FFFFFF", "ink": "#0F172A",
        "success": "#2F855A", "error": "#B5523B", "warning": "#B7791F",
    },
    "dark": {
        "bg": "#1C2128", "surface": "#242A33", "fg": "#D5DAE1", "muted": "#9AA4B2",
        "border": "#343C47", "hover": "#2D343F", "primary": "#8FB3DA", "accent": "#3BAF8A",
        "toggle": "#33445A", "paper": "#E6E9ED", "ink": "#1C2128",
        "success": "#4FB3A9", "error": "#E8836B", "warning": "#D9A441",
    },
}


# --- pure helpers (tested without a window) ---------------------------------
def on_color(hex_color: str) -> str:
    """Dark or light text for a background, picked by luminance (WCAG formula)."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return "#0F172A" if (lum + 0.05) / 0.05 > 1.05 / (lum + 0.05) else "#FFFFFF"


def build_label(job: dict) -> dict:
    """One full-size text element on a 14x40 label, from a job dict."""
    label = new_label(MEDIA)
    w, h = canvas_size(label)
    size = job.get("size", "auto")
    label["elements"] = [{
        "type": "text", "x": MARGIN, "y": MARGIN // 2, "w": w - 2 * MARGIN, "h": h - MARGIN,
        "text": job.get("text", ""), "font": job.get("font") or "auto",
        "size": "auto" if size in (None, "", "auto", "Auto") else int(size),
        "bold": bool(job.get("bold")), "italic": bool(job.get("italic")), "underline": bool(job.get("underline")),
    }]
    return normalize(label)


def history_path() -> Path:
    return config.config_dir() / "history.json"


def load_history(path: Path | None = None) -> list[dict]:
    try:
        data = json.loads((path or history_path()).read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict) and "text" in d][:HISTORY_MAX]
    except (OSError, ValueError, TypeError):
        return []


def add_history(job: dict, path: Path | None = None) -> list[dict]:
    """Prepend a printed job (newest first), keep the last HISTORY_MAX, save."""
    path = path or history_path()
    entry = {k: job.get(k) for k in ("text", "font", "size", "bold", "italic", "underline", "copies")}
    entry["printed_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    items = [entry] + load_history(path)
    items = items[:HISTORY_MAX]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


LOW_BATTERY_PERCENT = 20


def battery_is_low(percent) -> bool:
    """True when the reported battery is at or below the low threshold; unknown is not low."""
    return percent is not None and percent <= LOW_BATTERY_PERCENT


def friendly_error(e: BaseException) -> str:
    """Short plain message for the status line; technical text goes in Details."""
    from .printer import PrinterNotReady
    from .transports import TransportError

    if isinstance(e, api.NoDeviceError):
        return "Printer not found. Turn it on and close the phone app."
    if isinstance(e, PrinterNotReady):
        return {0x01: "Lid is open.", 0x04: "No labels loaded.", 0x24: "Out of labels.", 0x30: "Battery low."}.get(e.code, "Printer not ready.")
    if isinstance(e, TransportError):
        return "Lost connection to the printer."
    if isinstance(e, api.RenderErrors):
        return "Label could not be drawn."
    return "Print failed."


def system_theme() -> str:
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=2).stdout
            return "dark" if "Dark" in out else "light"
    except Exception:
        pass
    return "light"


# --- printing worker ----------------------------------------------------------
class Worker(threading.Thread):
    """Runs printer jobs one at a time. Requests in, results out (both queues)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.inbox: queue.Queue = queue.Queue()
        self.outbox: queue.Queue = queue.Queue()

    def run(self) -> None:
        while True:
            kind, payload = self.inbox.get()
            try:
                if kind == "info":
                    try:
                        self.outbox.put(("info", api.printer_info(), None))
                    except Exception as e:
                        self.outbox.put(("info_error", e, traceback.format_exc()))
                elif kind == "print":
                    for job in payload:
                        self.outbox.put(("printing", job, None))
                        res = api.print_labels(build_label(job), copies=job["copies"])
                        if not res["ok"]:
                            raise RuntimeError(res.get("error") or f"printed {res['printed']} of {res['labels']}")
                        if not str(res.get("device", "")).startswith(("mock", "file")):
                            config.set_value("device", res["device"])  # remember a printer that worked
                        self.outbox.put(("printed", job, None))
                    self.outbox.put(("done", None, None))
            except Exception as e:
                self.outbox.put(("error", e, traceback.format_exc()))


# --- small widgets --------------------------------------------------------------
class Tooltip:
    """Shows on hover and on keyboard focus, so it is not hover-only."""

    def __init__(self, widget: tk.Widget, text: str, app: "App") -> None:
        self.widget, self.text, self.app, self.tip, self.after_id = widget, text, app, None, None
        for seq, fn in (("<Enter>", self._schedule), ("<FocusIn>", self._show), ("<Leave>", self._hide), ("<FocusOut>", self._hide), ("<ButtonPress>", self._hide)):
            widget.bind(seq, fn, add="+")

    def _schedule(self, _e=None) -> None:
        self.after_id = self.widget.after(450, self._show)

    def _show(self, _e=None) -> None:
        if self.tip or not self.widget.winfo_viewable():
            return
        p = self.app.pal
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=self.text, bg=p["fg"], fg=p["bg"], font=self.app.f_small, padx=8, pady=4).pack()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2 - self.tip.winfo_reqwidth() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip.wm_geometry(f"+{max(0, x)}+{y}")

    def _hide(self, _e=None) -> None:
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class IconButton(tk.Label):
    """Icon button: mouse, Space/Enter, focus ring, tooltip, optional toggle state."""

    def __init__(self, parent, app: "App", icon: str, tip: str, command, kind: str = "plain", toggle: tk.BooleanVar | None = None, text: str = "") -> None:
        super().__init__(parent, takefocus=1, cursor="hand2", compound="left", text=text, padx=10 if text else 0)
        self.app, self.icon, self.command, self.kind, self.toggle, self.hover = app, icon, command, kind, toggle, False
        Tooltip(self, tip, app)
        self.bind("<Button-1>", lambda e: self.invoke())
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        if toggle is not None:
            toggle.trace_add("write", lambda *_: self.paint())
        app.themed(self.paint)

    def invoke(self) -> None:
        if str(self["state"]) != "disabled":
            self.command()

    def _set_hover(self, on: bool) -> None:
        self.hover = on
        self.paint()

    def paint(self, *_):
        p = self.app.pal
        if self.kind == "primary":
            bg = p["accent"]
            fg = on_color(bg)
        else:
            on = self.toggle is not None and self.toggle.get()
            bg = p["toggle"] if on else (p["hover"] if self.hover else p["surface"])
            fg = p["primary"] if on else p["fg"]
        size = self.app.icon_px
        self.configure(image=self.app.icon(self.icon, fg), bg=bg, fg=fg, font=self.app.f_body,
                       highlightthickness=2, highlightbackground=bg, highlightcolor=p["primary"],
                       width=0 if self.text_set else size + 16, height=size + 16)

    @property
    def text_set(self) -> bool:
        return bool(self.cget("text"))


# --- the app --------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = config.get("desktop", {}) or {}
        self.theme_choice = self.cfg.get("theme", "system")  # system / light / dark
        self.pal = PALETTES[self._resolved_theme()]
        self._paint_fns: list = []
        self._icons: dict = {}
        self.queue_jobs: list[dict] = []
        self.history = load_history()
        self.removed: tuple[int, dict] | None = None
        self.busy = False
        self.preview_after = None
        self.worker = Worker()
        self.worker.start()

        scale = root.winfo_fpixels("1i") / 96
        self.icon_px = round(20 * scale)
        base = tkfont.nametofont("TkDefaultFont").actual()
        self.f_body = tkfont.Font(family=base["family"], size=max(10, abs(base["size"])))
        self.f_small = tkfont.Font(family=base["family"], size=max(9, abs(base["size"]) - 1))
        self.f_head = tkfont.Font(family=base["family"], size=max(10, abs(base["size"])), weight="bold")
        mono = "Consolas" if sys.platform == "win32" else "Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono"
        self.f_mono = tkfont.Font(family=mono, size=max(9, abs(base["size"]) - 1))

        self.text_var = tk.StringVar()
        self.bold = tk.BooleanVar(value=self.cfg.get("bold", True))
        self.italic = tk.BooleanVar(value=False)
        self.underline = tk.BooleanVar(value=False)
        self.font_var = tk.StringVar(value=self.cfg.get("font", "Auto"))
        self.size_var = tk.StringVar(value="Auto")
        self.copies_var = tk.StringVar(value="1")
        self.filter_var = tk.StringVar()

        root.title("Label Printer")
        root.minsize(460, 560)
        root.geometry("520x680")
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._build()
        self.apply_theme()
        self._bind_keys()
        root.after(100, self._poll)
        root.after(300, lambda: self.worker.inbox.put(("info", None)))
        self.set_status("Looking for printer…", "muted")

    # theme ---------------------------------------------------------------
    def _resolved_theme(self) -> str:
        return system_theme() if self.theme_choice == "system" else self.theme_choice

    def themed(self, fn) -> None:
        self._paint_fns.append(fn)

    def icon(self, name: str, color: str) -> ImageTk.PhotoImage:
        key = (name, color, self.icon_px)
        if key not in self._icons:
            px = self.icon_px
            big = px * 4  # draw large, then shrink: smoother strokes
            img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            font = ImageFont.truetype(str(ICON_FONT), big)
            ImageDraw.Draw(img).text((big // 2, big // 2), chr(ICONS[name]), font=font, fill=color, anchor="mm")
            self._icons[key] = ImageTk.PhotoImage(img.resize((px, px), Image.Resampling.LANCZOS))
        return self._icons[key]

    def _paint_printer_lbl(self) -> None:
        self.printer_lbl.configure(fg=self.pal["warning" if self.batt_low else "muted"])

    def apply_theme(self) -> None:
        self.pal = p = PALETTES[self._resolved_theme()]
        s = self.style
        s.configure(".", background=p["bg"], foreground=p["fg"], font=self.f_body, bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["surface"], troughcolor=p["bg"], focuscolor=p["primary"])
        for w in ("TCombobox", "TSpinbox", "TEntry"):
            s.configure(w, fieldbackground=p["surface"], foreground=p["fg"], background=p["surface"],
                        arrowcolor=p["muted"], insertcolor=p["fg"], padding=6, selectbackground=p["toggle"], selectforeground=p["fg"])
            s.map(w, fieldbackground=[("readonly", p["surface"])], bordercolor=[("focus", p["primary"])],
                  foreground=[("readonly", p["fg"])], selectbackground=[("readonly", p["surface"])], selectforeground=[("readonly", p["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", p["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["toggle"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["fg"])
        self.root.configure(bg=p["bg"])
        for fn in self._paint_fns:
            fn()
        self._dark_titlebar(self._resolved_theme() == "dark")
        self.refresh_preview()
        self.render_queue()
        self.render_history()

    def _dark_titlebar(self, dark: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def cycle_theme(self) -> None:
        order = ["system", "light", "dark"]
        self.theme_choice = order[(order.index(self.theme_choice) + 1) % 3]
        self.save_cfg()
        self.apply_theme()

    def save_cfg(self) -> None:
        self.cfg.update(theme=self.theme_choice, font=self.font_var.get(), bold=self.bold.get())
        config.set_value("desktop", self.cfg)

    # layout --------------------------------------------------------------
    def frame(self, parent, role="bg", **kw) -> tk.Frame:
        f = tk.Frame(parent, **kw)
        self.themed(lambda: f.configure(bg=self.pal[role]))
        return f

    def label(self, parent, text="", role="fg", bg="bg", font=None, **kw) -> tk.Label:
        lab = tk.Label(parent, text=text, anchor="w", **kw)
        self.themed(lambda: lab.configure(bg=self.pal[bg], fg=self.pal[role], font=font or self.f_body))
        return lab

    def card(self, parent) -> tk.Frame:
        f = tk.Frame(parent, highlightthickness=1, bd=0)
        self.themed(lambda: f.configure(bg=self.pal["surface"], highlightbackground=self.pal["border"], highlightcolor=self.pal["border"]))
        return f

    def _build(self) -> None:
        root = self.root
        # Top bar: printer state + theme. Bottom bar: tabs. Pages in between.
        top = self.frame(root)
        top.pack(fill="x", padx=16, pady=(12, 4))
        self.bt_btn = IconButton(top, self, "bluetooth", "Find printer", self.find_printer)
        self.bt_btn.pack(side="left")
        self.printer_lbl = self.label(top, "", role="muted", font=self.f_mono)
        self.printer_lbl.pack(side="left", padx=8)
        self.batt_low = False
        self.themed(self._paint_printer_lbl)  # registered after label(), so it wins on repaint
        self.theme_btn = IconButton(top, self, "sun-moon", "Theme: system / light / dark", self.cycle_theme)
        self.theme_btn.pack(side="right")

        self.tabbar = self.frame(root, role="surface")
        self.tabbar.pack(side="bottom", fill="x")
        self.toast = self.frame(root, role="fg")  # packed above the tab bar when shown
        self.status_row = self.frame(root)
        self.status_row.pack(side="bottom", fill="x", padx=16, pady=(0, 6))
        self._status_role = "muted"
        self.status_lbl = tk.Label(self.status_row, anchor="w")
        self.themed(lambda: self.status_lbl.configure(bg=self.pal["bg"], fg=self.pal[self._status_role], font=self.f_body))
        self.status_lbl.pack(side="left")
        self.details_btn = tk.Label(self.status_row, text="Details", cursor="hand2", takefocus=1)
        self.themed(lambda: self.details_btn.configure(bg=self.pal["bg"], fg=self.pal["primary"], font=self.f_small))
        self.details_btn.bind("<Button-1>", lambda e: self.toggle_details())
        self.details_btn.bind("<Return>", lambda e: self.toggle_details())
        self.details_txt = tk.Text(root, height=6, wrap="word", bd=0, padx=8, pady=6)
        self.themed(lambda: self.details_txt.configure(bg=self.pal["surface"], fg=self.pal["muted"], font=self.f_mono))
        self.details = ""

        self.pages = self.frame(root)
        self.pages.pack(fill="both", expand=True)
        self.page_print = self.frame(self.pages)
        self.page_hist = self.frame(self.pages)
        self._build_print(self.page_print)
        self._build_history(self.page_hist)

        self.tab_btns = {}
        for name, icon, tip in (("print", "tag", "Print  (Ctrl+1)"), ("history", "history", "History  (Ctrl+2)")):
            b = IconButton(self.tabbar, self, icon, tip, lambda n=name: self.show_page(n), text=name.capitalize())
            b.toggle = tk.BooleanVar(value=False)
            b.toggle.trace_add("write", lambda *_, b=b: b.paint())
            b.pack(side="left", expand=True, fill="x", pady=6, padx=6)
            self.tab_btns[name] = b
        self.show_page("print")

    def _build_print(self, page: tk.Frame) -> None:
        pad = {"padx": 16}
        ed = self.card(page)
        ed.pack(fill="x", pady=(4, 8), **pad)
        self.text = tk.Text(ed, height=3, wrap="word", bd=0, padx=10, pady=8, undo=True)
        self.themed(lambda: self.text.configure(bg=self.pal["surface"], fg=self.pal["fg"], insertbackground=self.pal["fg"],
                                                selectbackground=self.pal["toggle"], selectforeground=self.pal["fg"], font=self.f_body))
        self.text.pack(fill="x")
        self.text.bind("<<Modified>>", self._on_text)
        self.text.focus_set()

        fmt = self.frame(page)
        fmt.pack(fill="x", **pad)
        IconButton(fmt, self, "bold", "Bold  (Ctrl+B)", lambda: self._flip(self.bold), toggle=self.bold).pack(side="left")
        IconButton(fmt, self, "italic", "Italic  (Ctrl+I)", lambda: self._flip(self.italic), toggle=self.italic).pack(side="left", padx=4)
        IconButton(fmt, self, "underline", "Underline  (Ctrl+U)", lambda: self._flip(self.underline), toggle=self.underline).pack(side="left")
        size = ttk.Combobox(fmt, textvariable=self.size_var, values=SIZES, width=5)
        size.pack(side="right")
        Tooltip(size, "Font size in dots (Auto fits the label)", self)
        try:
            fams = ["Auto"] + fonts.families()
        except Exception:
            fams = ["Auto"]
        font = ttk.Combobox(fmt, textvariable=self.font_var, values=fams, width=18)
        font.pack(side="right", padx=8)
        Tooltip(font, "Font", self)
        for v in (self.bold, self.italic, self.underline, self.font_var, self.size_var):
            v.trace_add("write", lambda *_: self.schedule_preview())

        self.preview_box = self.frame(page)
        self.preview_box.pack(fill="x", pady=12, **pad)
        self.preview_lbl = tk.Label(self.preview_box, bd=0)
        self.themed(lambda: self.preview_lbl.configure(bg=self.pal["bg"]))
        self.preview_lbl.pack()
        self.warn_lbl = self.label(self.preview_box, "", role="warning", font=self.f_small)
        self.warn_lbl.pack()

        act = self.frame(page)
        act.pack(fill="x", **pad)
        self.label(act, "Qty", role="muted").pack(side="left")
        spin = ttk.Spinbox(act, from_=1, to=99, textvariable=self.copies_var, width=4, font=self.f_mono)
        spin.pack(side="left", padx=(6, 0))
        Tooltip(spin, "Copies of this label (1–99)", self)
        self.print_btn = IconButton(act, self, "printer", "Print now  (Ctrl+Enter)", self.print_now, kind="primary", text="Print")
        self.print_btn.pack(side="right")
        IconButton(act, self, "list-plus", "Add to queue  (Ctrl+Shift+Enter)", self.add_to_queue).pack(side="right", padx=8)

        qhead = self.frame(page)
        qhead.pack(fill="x", pady=(18, 4), **pad)
        self.qtitle = self.label(qhead, "Queue", role="muted", font=self.f_head)
        self.qtitle.pack(side="left")
        self.printq_btn = IconButton(qhead, self, "list-start", "Print queue  (Ctrl+P)", self.print_queue)
        self.printq_btn.pack(side="right")
        self.qlist = self.frame(page)
        self.qlist.pack(fill="both", expand=True, **pad)

    def _build_history(self, page: tk.Frame) -> None:
        head = self.frame(page)
        head.pack(fill="x", padx=16, pady=(4, 8))
        self.label(head, "History", role="muted", font=self.f_head).pack(side="left")
        search = ttk.Entry(head, textvariable=self.filter_var, width=24)
        search.pack(side="right")
        icon = tk.Label(head)
        self.themed(lambda: icon.configure(image=self.icon("search", self.pal["muted"]), bg=self.pal["bg"]))
        icon.pack(side="right", padx=(0, 6))
        Tooltip(search, "Search history  (Ctrl+F)", self)
        self.search_entry = search
        self.filter_var.trace_add("write", lambda *_: self.render_history())
        self.hlist = self.frame(page)
        self.hlist.pack(fill="both", expand=True, padx=16)

    # pages ---------------------------------------------------------------
    def show_page(self, name: str) -> None:
        for n, b in self.tab_btns.items():
            b.toggle.set(n == name)
        self.page_print.pack_forget()
        self.page_hist.pack_forget()
        (self.page_print if name == "print" else self.page_hist).pack(fill="both", expand=True)
        if name == "print":
            self.text.focus_set()

    def _bind_keys(self) -> None:
        r = self.root
        keys = {
            "<Control-Return>": self.print_now,
            "<Control-Shift-Return>": self.add_to_queue,
            "<Control-b>": lambda: self._flip(self.bold),
            "<Control-i>": lambda: self._flip(self.italic),
            "<Control-u>": lambda: self._flip(self.underline),
        }
        for seq, fn in keys.items():
            # The text box sees keys first (Ctrl+I would type a tab), so bind there too and stop it.
            handler = lambda e, fn=fn: (fn(), "break")[1]
            self.text.bind(seq, handler)
            r.bind_all(seq, handler)
        self.text.bind("<Control-z>", self._ctrl_z)
        r.bind_all("<Control-z>", self._ctrl_z)
        r.bind_all("<Control-p>", lambda e: self.print_queue())
        r.bind_all("<Control-Key-1>", lambda e: self.show_page("print"))
        r.bind_all("<Control-Key-2>", lambda e: self.show_page("history"))
        r.bind_all("<Control-f>", lambda e: (self.show_page("history"), self.search_entry.focus_set()))

    def _flip(self, var: tk.BooleanVar) -> None:
        var.set(not var.get())
        self.save_cfg()

    # editor + preview ----------------------------------------------------
    def _on_text(self, _e=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.schedule_preview()

    def current_job(self) -> dict:
        try:
            copies = max(1, min(99, int(self.copies_var.get())))
        except ValueError:
            copies = 1
        font = self.font_var.get().strip()
        return {
            "text": self.text.get("1.0", "end-1c"), "font": "auto" if font in ("", "Auto") else font,
            "size": self.size_var.get().strip() or "Auto", "bold": self.bold.get(), "italic": self.italic.get(),
            "underline": self.underline.get(), "copies": copies,
        }

    def load_job(self, job: dict) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", job.get("text", ""))
        self.font_var.set("Auto" if job.get("font") in (None, "auto") else job["font"])
        self.size_var.set(str(job.get("size") or "Auto"))
        self.bold.set(bool(job.get("bold")))
        self.italic.set(bool(job.get("italic")))
        self.underline.set(bool(job.get("underline")))
        self.copies_var.set(str(job.get("copies") or 1))
        self.show_page("print")

    def schedule_preview(self) -> None:
        if self.preview_after:
            self.root.after_cancel(self.preview_after)
        self.preview_after = self.root.after(150, self.refresh_preview)

    def refresh_preview(self) -> None:
        self.preview_after = None
        if not hasattr(self, "preview_lbl"):
            return
        p = self.pal
        job = self.current_job()
        try:
            res = api.render_label(build_label(job))
            scale = 2 if self.root.winfo_fpixels("1i") / 96 >= 1.25 else 1
            img = res.preview(scale=max(1, scale), ink=p["ink"], paper=p["paper"])
            w, h = img.size
            framed = Image.new("RGB", (w + 2, h + 2), p["border"])
            framed.paste(img, (1, 1))
            self._preview_img = ImageTk.PhotoImage(framed)
            self.preview_lbl.configure(image=self._preview_img)
            warn = next((x for x in res.warnings if "font" not in x.lower() or job["font"] != "auto"), "")
            self.warn_lbl.configure(text="Text too long for the label." if "fit" in warn.lower() else "")
        except Exception as e:
            self.warn_lbl.configure(text=f"Preview failed: {e}")

    # queue ---------------------------------------------------------------
    def add_to_queue(self) -> None:
        job = self.current_job()
        if not job["text"].strip():
            self.set_status("Type some text first.", "warning")
            return
        self.queue_jobs.append(job)
        self.render_queue()
        self.set_status("Added to queue.", "muted")

    def remove_from_queue(self, i: int) -> None:
        self.removed = (i, self.queue_jobs.pop(i))
        self.render_queue()
        self.show_toast("Removed from queue.", "Undo", self.undo_remove)

    def undo_remove(self) -> None:
        if self.removed:
            i, job = self.removed
            self.queue_jobs.insert(min(i, len(self.queue_jobs)), job)
            self.removed = None
            self.render_queue()
        self.hide_toast()

    def _ctrl_z(self, _e=None):
        if self.removed and self.toast.winfo_ismapped():
            self.undo_remove()
            return "break"
        return None

    def _row(self, parent, job: dict, extra: str, icon: str, tip: str, command, on_click=None) -> None:
        row = self.card(parent)
        row.pack(fill="x", pady=3)
        first = (job["text"].strip().splitlines() or [""])[0]
        text = self.label(row, first[:40] + ("…" if len(first) > 40 or "\n" in job["text"].strip() else ""), bg="surface")
        text.pack(side="left", padx=10, pady=8, fill="x", expand=True)
        meta = self.label(row, extra, role="muted", bg="surface", font=self.f_mono)
        meta.pack(side="left", padx=6)
        btn = IconButton(row, self, icon, tip, command)
        btn.pack(side="right", padx=4, pady=2)
        if on_click:
            for w in (row, text, meta):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e: on_click())

    def render_queue(self) -> None:
        if not hasattr(self, "qlist"):
            return
        for w in self.qlist.winfo_children():
            w.destroy()
        self._paint_fns = [f for f in self._paint_fns if _alive(f)]
        start = len(self._paint_fns)
        if not self.queue_jobs:
            self.label(self.qlist, "Queue is empty. Add a label above.", role="muted").pack(anchor="w", pady=6)
        for i, job in enumerate(self.queue_jobs):
            self._row(self.qlist, job, f"×{job['copies']}", "x", "Remove from queue", lambda i=i: self.remove_from_queue(i))
        self._paint_new(start)
        n = sum(j["copies"] for j in self.queue_jobs)
        self.qtitle.configure(text=f"Queue · {n} label{'s' if n != 1 else ''}" if n else "Queue")

    # history -------------------------------------------------------------
    def render_history(self) -> None:
        if not hasattr(self, "hlist"):
            return
        for w in self.hlist.winfo_children():
            w.destroy()
        self._paint_fns = [f for f in self._paint_fns if _alive(f)]
        start = len(self._paint_fns)
        q = self.filter_var.get().strip().lower()
        items = [h for h in self.history if q in h.get("text", "").lower()]
        if not self.history:
            self.label(self.hlist, "No labels printed yet.", role="muted").pack(anchor="w", pady=6)
        elif not items:
            self.label(self.hlist, "No matches.", role="muted").pack(anchor="w", pady=6)
        for h in items:
            when = (h.get("printed_at") or "")[:16].replace("T", " ")
            self._row(self.hlist, h, f"×{h.get('copies') or 1}  {when}", "repeat", "Print again",
                      lambda h=h: self.start_print([{**h}]), on_click=lambda h=h: self.load_job(h))
        self._paint_new(start)

    def _paint_new(self, start: int) -> None:
        for f in self._paint_fns[start:]:
            f()  # rows are rebuilt after the theme was applied, so paint them now

    # printing ------------------------------------------------------------
    def print_now(self) -> None:
        job = self.current_job()
        if not job["text"].strip():
            self.set_status("Type some text first.", "warning")
            return
        self.start_print([job])

    def print_queue(self) -> None:
        if not self.queue_jobs:
            self.set_status("Queue is empty.", "warning")
            return
        jobs, self.queue_jobs = self.queue_jobs, []
        self.render_queue()
        self.start_print(jobs)

    def start_print(self, jobs: list[dict]) -> None:
        if self.busy:
            self.queue_jobs.extend(jobs)  # never drop work: park it in the queue
            self.render_queue()
            self.set_status("Printer busy. Added to queue.", "muted")
            return
        self.busy = True
        self.pending = list(jobs)
        self.set_status("Connecting…", "muted")
        self.worker.inbox.put(("print", jobs))

    def find_printer(self) -> None:
        config.set_value("device", None)  # forget a saved printer and scan again
        self.set_status("Looking for printer…", "muted")
        self.worker.inbox.put(("info", None))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload, details = self.worker.outbox.get_nowait()
                self._handle(kind, payload, details)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, kind: str, payload, details) -> None:
        if kind == "info":
            batt = (payload.get("battery") or {})
            name = "P21" if not str(payload.get("device", "")).startswith("mock") else "Mock printer"
            pct = f" · {batt['percent']}%" if batt.get("percent") is not None else ""
            self.printer_lbl.configure(text=f"{name}{pct}")
            self.batt_low = battery_is_low(batt.get("percent"))
            self._paint_printer_lbl()
            self.set_status("Ready.", "success")
        elif kind == "printing":
            self.set_status(f"Printing “{payload['text'].strip()[:24]}” ×{payload['copies']}…", "muted")
        elif kind == "printed":
            self.history = add_history(payload)
            if payload in self.pending:
                self.pending.remove(payload)
            self.render_history()
        elif kind == "done":
            self.busy = False
            self.set_status("Printed.", "success")
            self.worker.inbox.put(("info", None))  # battery drops while printing; the single worker keeps this after the print
        elif kind == "info_error":
            self.printer_lbl.configure(text="No printer")
            self.set_status(friendly_error(payload), "error", details=f"{type(payload).__name__}: {payload}\n\n{details}")
        elif kind == "error":
            self.busy = False
            if getattr(self, "pending", None):
                # put unprinted jobs back at the front of the queue, so nothing is lost
                self.queue_jobs[:0] = [j for j in self.pending if j not in self.queue_jobs]
                self.pending = []
                self.render_queue()
            if isinstance(payload, api.NoDeviceError):
                self.printer_lbl.configure(text="No printer")
            self.set_status(friendly_error(payload), "error", details=f"{type(payload).__name__}: {payload}\n\n{details}")

    # status, details, toast ---------------------------------------------
    def set_status(self, text: str, role: str = "muted", details: str = "") -> None:
        self._status_role = role
        self.status_lbl.configure(text=text, fg=self.pal[role])
        self.details = details
        if details:
            self.details_btn.pack(side="left", padx=8)
        else:
            self.details_btn.pack_forget()
            self.details_txt.pack_forget()

    def toggle_details(self) -> None:
        if self.details_txt.winfo_ismapped():
            self.details_txt.pack_forget()
        else:
            self.details_txt.delete("1.0", "end")
            self.details_txt.insert("1.0", self.details)
            self.details_txt.pack(side="bottom", fill="x", padx=16, pady=(0, 6), before=self.status_row)

    def show_toast(self, text: str, action: str, command) -> None:
        for w in self.toast.winfo_children():
            w.destroy()
        p = self.pal
        self.toast.configure(bg=p["fg"])
        tk.Label(self.toast, text=text, bg=p["fg"], fg=p["bg"], font=self.f_body).pack(side="left", padx=12, pady=8)
        b = tk.Label(self.toast, text=f"{action}  (Ctrl+Z)", bg=p["fg"], fg=p["toggle"], font=self.f_head, cursor="hand2")
        b.pack(side="right", padx=12)
        b.bind("<Button-1>", lambda e: command())
        self.toast.pack(side="bottom", fill="x", padx=16, pady=(0, 6), before=self.status_row)
        if getattr(self, "_toast_after", None):
            self.root.after_cancel(self._toast_after)
        self._toast_after = self.root.after(6000, self.hide_toast)

    def hide_toast(self) -> None:
        self.toast.pack_forget()
        self.removed = None


def _alive(fn) -> bool:
    """Drop theme callbacks whose widget was destroyed (queue/history rows are rebuilt)."""
    try:
        fn()
        return True
    except tk.TclError:
        return False


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

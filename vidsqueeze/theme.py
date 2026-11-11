"""Темы оформления: минималистичный «лофт» — бетон, графит, медь. Светлая, тёмная и чисто чёрная (OLED)."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

FONT = "Segoe UI"

THEMES: dict[str, dict] = {
    "light": dict(
        title="Светлая", dark=False,
        bg="#EEEBE5", panel="#F8F6F2", field="#FFFFFF", border="#D6D0C5", fg="#26231F", muted="#7B746A",
        accent="#B4532A", accent_fg="#FFFFFF", select="#E8DACB", trough="#DDD7CB", ok="#2E7D4F", err="#B3261E",
    ),
    "graphite": dict(
        title="Графит", dark=True,
        bg="#1D1D1C", panel="#262625", field="#1A1A19", border="#3A3935", fg="#E9E5DD", muted="#928C82",
        accent="#D98B5F", accent_fg="#1B1511", select="#3C342D", trough="#34332F", ok="#7DC293", err="#E87D7D",
    ),
    "oled": dict(
        title="OLED", dark=True,
        bg="#000000", panel="#080808", field="#000000", border="#262626", fg="#E6E2DA", muted="#857F76",
        accent="#E39A6F", accent_fg="#000000", select="#241B15", trough="#1A1A1A", ok="#7DC293", err="#F08080",
    ),
}


def _shift(color: str, amount: float) -> str:
    """Осветлить (amount > 0) или затемнить (amount < 0) цвет #RRGGBB."""
    c = color.lstrip("#")
    rgb = [int(c[i:i + 2], 16) for i in (0, 2, 4)]
    if amount >= 0:
        rgb = [round(v + (255 - v) * amount) for v in rgb]
    else:
        rgb = [round(v * (1 + amount)) for v in rgb]
    return "#" + "".join(f"{max(0, min(255, v)):02X}" for v in rgb)


def titlebar(window: tk.Misc, palette: dict) -> None:
    """Тёмный (или в цвет окна) заголовок окна в Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.windll.dwmapi

        def put(attr: int, value: int) -> None:
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hex_color: str) -> int:
            c = hex_color.lstrip("#")
            return int(c[4:6] + c[2:4] + c[0:2], 16)   # COLORREF = 0x00BBGGRR

        put(20, 1 if palette["dark"] else 0)           # DWMWA_USE_IMMERSIVE_DARK_MODE
        put(35, colorref(palette["bg"]))               # DWMWA_CAPTION_COLOR (Windows 11)
        put(36, colorref(palette["fg"]))               # DWMWA_TEXT_COLOR
        put(34, colorref(palette["border"]))           # DWMWA_BORDER_COLOR
    except Exception:
        pass


def apply(root: tk.Tk, key: str) -> dict:
    """Применяет тему ко всем ttk-виджетам и возвращает палитру (для обычных tk-виджетов)."""
    p = THEMES[key]
    style = ttk.Style(root)
    style.theme_use("clam")
    bg, panel, field, border = p["bg"], p["panel"], p["field"], p["border"]
    fg, muted, accent, select = p["fg"], p["muted"], p["accent"], p["select"]
    hover = _shift(panel, 0.08 if p["dark"] else -0.05)

    root.configure(bg=bg)
    root.option_add("*Font", f"{{{FONT}}} 10")
    root.option_add("*TCombobox*Listbox.background", field)
    root.option_add("*TCombobox*Listbox.foreground", fg)
    root.option_add("*TCombobox*Listbox.selectBackground", accent)
    root.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])
    root.option_add("*TCombobox*Listbox.font", f"{{{FONT}}} 10")

    style.configure(".", background=bg, foreground=fg, fieldbackground=field, bordercolor=border,
                    lightcolor=bg, darkcolor=bg, troughcolor=p["trough"], focuscolor=bg,
                    selectbackground=accent, selectforeground=p["accent_fg"], font=(FONT, 10))

    for pref, back in (("", bg), ("Card.", panel)):
        style.configure(f"{pref}TFrame", background=back)
        style.configure(f"{pref}TLabel", background=back, foreground=fg)
        style.configure(f"{pref}Muted.TLabel", background=back, foreground=muted, font=(FONT, 9))
        style.configure(f"{pref}Section.TLabel", background=back, foreground=accent, font=(FONT, 9, "bold"))
        style.configure(f"{pref}TCheckbutton", background=back, foreground=fg, indicatorbackground=field,
                        indicatorforeground=p["accent_fg"], indicatormargin=(0, 0, 6, 0), padding=(0, 3))
        style.map(f"{pref}TCheckbutton", background=[("active", back)],
                  indicatorbackground=[("selected", accent), ("active", select)],
                  foreground=[("disabled", muted)], bordercolor=[("selected", accent)])
        style.configure(f"{pref}TRadiobutton", background=back, foreground=fg, indicatorbackground=field,
                        indicatorforeground=p["accent_fg"], indicatormargin=(0, 0, 6, 0), padding=(0, 3))
        style.map(f"{pref}TRadiobutton", background=[("active", back)],
                  indicatorbackground=[("selected", accent), ("active", select)],
                  foreground=[("disabled", muted)])
        style.configure(f"{pref}Horizontal.TScale", background=back, troughcolor=p["trough"], bordercolor=back)

    style.configure("TButton", background=panel, foreground=fg, bordercolor=border, relief="flat",
                    padding=(14, 7), borderwidth=1, focuscolor=panel)
    style.map("TButton", background=[("disabled", bg), ("pressed", select), ("active", hover)],
              foreground=[("disabled", muted)], bordercolor=[("active", accent)])
    style.configure("Accent.TButton", background=accent, foreground=p["accent_fg"], bordercolor=accent,
                    font=(FONT, 12, "bold"), padding=(26, 11))
    style.map("Accent.TButton", background=[("disabled", p["trough"]), ("pressed", _shift(accent, -0.15)),
                                            ("active", _shift(accent, 0.12))],
              foreground=[("disabled", muted)], bordercolor=[("disabled", p["trough"])])
    style.configure("Seg.Toolbutton", background=bg, foreground=muted, bordercolor=border, relief="flat",
                    padding=(12, 4), font=(FONT, 9))
    style.map("Seg.Toolbutton", background=[("selected", accent), ("active", hover)],
              foreground=[("selected", p["accent_fg"])], bordercolor=[("selected", accent)])

    for cls in ("TCombobox", "TSpinbox", "TEntry"):
        style.configure(cls, fieldbackground=field, background=panel, foreground=fg, bordercolor=border,
                        arrowcolor=muted, insertcolor=fg, padding=(6, 4), arrowsize=14)
        style.map(cls, fieldbackground=[("disabled", bg), ("readonly", field)], bordercolor=[("focus", accent)],
                  foreground=[("disabled", muted)], arrowcolor=[("active", accent)],
                  background=[("active", panel), ("readonly", panel)],
                  selectbackground=[("!focus", field)], selectforeground=[("!focus", fg)])

    style.configure("Treeview", background=field, fieldbackground=field, foreground=fg, bordercolor=border,
                    rowheight=32, borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", select)], foreground=[("selected", fg)])
    style.configure("Treeview.Heading", background=bg, foreground=muted, relief="flat", borderwidth=0,
                    padding=(8, 8), font=(FONT, 9, "bold"))
    style.map("Treeview.Heading", background=[("active", bg)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    style.configure("Horizontal.TProgressbar", troughcolor=p["trough"], background=accent, bordercolor=p["trough"],
                    lightcolor=accent, darkcolor=accent, thickness=8)
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=border, troughcolor=bg, bordercolor=bg,
                        arrowcolor=muted, relief="flat", arrowsize=12)
        style.map(f"{orient}.TScrollbar", background=[("active", muted)])
    titlebar(root, p)
    return p

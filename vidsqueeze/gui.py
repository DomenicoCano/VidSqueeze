"""Графическое окно VidSqueeze (tkinter). Оформление — vidsqueeze/theme.py."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import theme
from .cli import human
from .codecs import CODECS, VENDORS
from .compare import Candidate, compare_file
from .compress import VIDEO_EXTS, Options, compress_file, find_videos
from .estimate import estimate_file
from .ffmpeg import Cancelled, FFmpegError, ffmpeg_path, set_low_priority
from .hardware import cpu_threads, detect_gpu_encoders, threads_for_percent

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _Base = TkinterDnD.Tk
    HAVE_DND = True
except Exception:  # перетаскивание — приятный бонус, не обязательное условие
    _Base = tk.Tk
    HAVE_DND = False

APP_NAME = "VidSqueeze"
PRESETS = {
    "Максимальное качество": {"vp9": 34, "h265": 22, "av1": 34},
    "Баланс (рекомендуется)": {"vp9": 42, "h265": 30, "av1": 42},
    "Минимальный размер": {"vp9": 50, "h265": 36, "av1": 50},
}
CUSTOM = "Свой вариант"
DEFAULT_PRESET = "Баланс (рекомендуется)"
AUDIO_CHOICES = {"Авто (убрать, если тишина)": "auto", "Оставить": "keep", "Убрать": "drop"}
FPS_CHOICES = {"Не больше 30 (рекомендуется)": 30.0, "Не больше 60": 60.0, "Без ограничения": 0.0}
CODEC_TITLES = {"vp9": "VP9  ·  .webm", "h265": "H.265  ·  .mp4", "av1": "AV1  ·  .webm"}
CODEC_NOTES = {
    "vp9": "Лучший выбор для сайтов и браузеров.",
    "h265": "Воспроизводится почти везде: телефоны, ТВ, плееры.",
    "av1": "Самый компактный, но кодируется очень медленно.",
}
CRF_MAX = {"vp9": 63, "h265": 51, "av1": 63}
GPU_NONE = "Не использовать (процессор)"
GPU_WAIT = "Поиск видеокарты…"
DEFAULT_THEME = "graphite"
CPU_STEP = 5


def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / "vidsqueeze" / "settings.json"


def open_in_explorer(path: Path) -> None:
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


def _last_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return lines[-1] if lines else repr(exc)


class App(_Base):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — сжатие видео")
        self.minsize(1020, 660)
        self.geometry("1200x800")

        self.items: dict[str, Path] = {}
        self.actual: dict[str, str] = {}          # iid -> фактический размер после сжатия
        self.est: dict[tuple, int] = {}           # кэш оценок размера
        self.est_err: dict[str, str] = {}
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_output: Path | None = None
        self.gpu_found: dict[str, list[str]] = {}
        self.listeners: list = []                 # окна, которым нужно знать о смене темы
        self._busy = False
        self._loading = True
        self._applying_preset = False
        self._est_after = None
        self._est_gen = 0
        self._est_active = False
        self._est_cancel = threading.Event()
        self._est_thread: threading.Thread | None = None
        self._cards: list[tuple[tk.Frame, str]] = []   # (рамка, роль: card | line)

        saved = self._read_settings()
        self.v_theme = tk.StringVar(value=saved.get("theme") if saved.get("theme") in theme.THEMES else DEFAULT_THEME)
        self.pal = theme.apply(self, self.v_theme.get())
        self._build_vars()
        self._build_ui()
        self._apply_settings(saved)
        self._recolor()
        self._loading = False
        for var in (*self.v_use.values(), *self.v_crf.values(), self.v_audio, self.v_fps, self.v_fast,
                    self.v_ten, self.v_gpu):
            var.trace_add("write", self._settings_changed)
        self.v_lowprio.trace_add("write", lambda *_: set_low_priority(self.v_lowprio.get()))
        set_low_priority(self.v_lowprio.get())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        threading.Thread(target=lambda: self.events.put(("gpu", detect_gpu_encoders())), daemon=True).start()

    # ------------------------------------------------------------------ переменные
    def _build_vars(self) -> None:
        self.v_preset = tk.StringVar(value=DEFAULT_PRESET)
        self.v_use = {k: tk.BooleanVar(value=k in ("vp9", "h265")) for k in CODECS}
        self.v_crf = {k: tk.IntVar(value=PRESETS[DEFAULT_PRESET][k]) for k in CODECS}
        self.v_audio = tk.StringVar(value=next(iter(AUDIO_CHOICES)))
        self.v_fps = tk.StringVar(value=next(iter(FPS_CHOICES)))
        self.v_ten = tk.BooleanVar(value=False)
        self.v_fast = tk.BooleanVar(value=False)
        self.v_cpu = tk.IntVar(value=75)
        self.v_lowprio = tk.BooleanVar(value=True)
        self.v_gpu = tk.StringVar(value=GPU_WAIT)
        self.v_auto_est = tk.BooleanVar(value=True)
        self.v_out_mode = tk.StringVar(value="same")
        self.v_out_dir = tk.StringVar(value="")
        self.v_status = tk.StringVar(value="Добавьте видео, чтобы начать")
        self.v_summary = tk.StringVar(value="")
        self.v_cpu_text = tk.StringVar()
        self.v_gpu_note = tk.StringVar(value="Проверяю, какие кодеры видеокарты доступны…")
        for k in CODECS:
            self.v_crf[k].trace_add("write", self._crf_changed)

    # ------------------------------------------------------------------ интерфейс
    def _card(self, parent: tk.Misc, title: str) -> ttk.Frame:
        """Карточка: тонкая рамка, заголовок-метка капсом, внутри — рабочая область."""
        outer = tk.Frame(parent, bg=self.pal["panel"], highlightthickness=1, highlightbackground=self.pal["border"])
        self._cards.append((outer, "card"))
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12, 16, 14))
        inner.pack(fill="both", expand=True)
        inner.columnconfigure(0, weight=1)
        ttk.Label(inner, text=title.upper(), style="Card.Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        outer.inner = inner  # type: ignore[attr-defined]
        return outer  # type: ignore[return-value]

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # --- шапка
        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 8))
        head.columnconfigure(1, weight=1)
        self.l_title = tk.Label(head, text="VidSqueeze", font=(theme.FONT, 22, "bold"), anchor="w")
        self.l_title.grid(row=0, column=0, sticky="w")
        self.l_sub = tk.Label(head, text="   сжатие видео без лишнего шума", font=(theme.FONT, 10), anchor="w")
        self.l_sub.grid(row=0, column=1, sticky="sw", pady=(0, 4))
        seg = ttk.Frame(head)
        seg.grid(row=0, column=2, sticky="e")
        for key, pal in theme.THEMES.items():
            ttk.Radiobutton(seg, text=pal["title"], value=key, variable=self.v_theme, style="Seg.Toolbutton",
                            command=self._theme_changed).pack(side="left")

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 18))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        self._build_left(left)
        self._build_right(body)

    def _build_left(self, left: ttk.Frame) -> None:
        ttk.Label(left, text="ВИДЕО", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))

        frame = tk.Frame(left, bg=self.pal["border"], highlightthickness=1, highlightbackground=self.pal["border"])
        self._cards.append((frame, "line"))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("size", "est", "status"), show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Файл", anchor="w")
        self.tree.heading("size", text="Размер", anchor="e")
        self.tree.heading("est", text="После сжатия", anchor="w")
        self.tree.heading("status", text="Статус", anchor="w")
        self.tree.column("#0", width=200, anchor="w", stretch=True)
        self.tree.column("size", width=84, anchor="e", stretch=False)
        self.tree.column("est", width=340, anchor="w", stretch=True)
        self.tree.column("status", width=100, anchor="w", stretch=False)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        hint = "Перетащите видео сюда\nили нажмите «Добавить видео»" if HAVE_DND else "Нажмите «Добавить видео»"
        self.empty = tk.Label(self.tree, text=hint, font=(theme.FONT, 12), justify="center")
        self.empty.place(relx=0.5, rely=0.5, anchor="center")
        if HAVE_DND:
            for w in (self.tree, self.empty):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        btns = ttk.Frame(left)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.b_add = ttk.Button(btns, text="Добавить видео", command=self._add_files)
        self.b_folder = ttk.Button(btns, text="Добавить папку", command=self._add_folder)
        self.b_remove = ttk.Button(btns, text="Убрать", command=self._remove_selected)
        self.b_clear = ttk.Button(btns, text="Очистить", command=self._clear)
        self.b_compare = ttk.Button(btns, text="Сравнить качество…", command=self._open_compare)
        for b in (self.b_add, self.b_folder, self.b_remove, self.b_clear):
            b.pack(side="left", padx=(0, 8))
        self.b_compare.pack(side="right")

        self.l_summary = tk.Label(left, textvariable=self.v_summary, anchor="w", justify="left",
                                  font=(theme.FONT, 10), wraplength=700)
        self.l_summary.grid(row=3, column=0, sticky="ew", pady=(10, 10))

        save = self._card(left, "Куда сохранить")
        save.grid(row=4, column=0, sticky="ew")
        s = save.inner  # type: ignore[attr-defined]
        s.columnconfigure(2, weight=1)
        ttk.Radiobutton(s, text="Рядом с исходным файлом", variable=self.v_out_mode, value="same",
                        style="Card.TRadiobutton", command=self._sync_out).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(s, text="В папку:", variable=self.v_out_mode, value="custom",
                        style="Card.TRadiobutton", command=self._sync_out).grid(row=1, column=1, sticky="w", padx=(18, 8))
        self.e_out = ttk.Entry(s, textvariable=self.v_out_dir)
        self.e_out.grid(row=1, column=2, sticky="ew", padx=(0, 8))
        self.b_out = ttk.Button(s, text="Обзор…", command=self._pick_out)
        self.b_out.grid(row=1, column=3)

        act = ttk.Frame(left)
        act.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        act.columnconfigure(1, weight=1)
        self.b_start = ttk.Button(act, text="Сжать", style="Accent.TButton", command=self._start)
        self.b_start.grid(row=0, column=0, rowspan=2, padx=(0, 16))
        self.bar = ttk.Progressbar(act, maximum=1000)
        self.bar.grid(row=0, column=1, sticky="sew", pady=(0, 6))
        self.l_status = tk.Label(act, textvariable=self.v_status, anchor="w", font=(theme.FONT, 10))
        self.l_status.grid(row=1, column=1, sticky="nw")
        self.b_open = ttk.Button(act, text="Открыть папку", command=self._open_output, state="disabled")
        self.b_open.grid(row=0, column=2, rowspan=2, padx=(16, 0))

        self.log = tk.Text(left, height=3, state="disabled", relief="flat", font=("Consolas", 9), padx=8, pady=6,
                           highlightthickness=1)
        self.log.grid(row=6, column=0, sticky="ew", pady=(12, 0))

    def _build_right(self, body: ttk.Frame) -> None:
        wrap = ttk.Frame(body)
        wrap.grid(row=0, column=1, sticky="ns")
        ttk.Label(wrap, text="НАСТРОЙКИ", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.canvas = tk.Canvas(wrap, width=392, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        wrap.rowconfigure(1, weight=1)
        self.canvas.grid(row=1, column=0, sticky="ns")
        vsb.grid(row=1, column=1, sticky="ns", padx=(4, 0))
        col = ttk.Frame(self.canvas)
        win = self.canvas.create_window(0, 0, anchor="nw", window=col, width=392)
        col.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))
        col.columnconfigure(0, weight=1)
        self._right_col = col

        # --- качество
        q = self._card(col, "Качество")
        q.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        qi = q.inner  # type: ignore[attr-defined]
        qi.columnconfigure(1, weight=1)
        self.cb_preset = ttk.Combobox(qi, textvariable=self.v_preset, state="readonly", values=[*PRESETS, CUSTOM])
        self.cb_preset.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.cb_preset.bind("<<ComboboxSelected>>", self._preset_selected)
        r = 2
        for key in CODECS:
            ttk.Checkbutton(qi, text=CODEC_TITLES[key], variable=self.v_use[key], style="Card.TCheckbutton"
                            ).grid(row=r, column=0, columnspan=2, sticky="w")
            ttk.Label(qi, text="CRF", style="Card.Muted.TLabel").grid(row=r, column=1, sticky="e", padx=(0, 6))
            ttk.Spinbox(qi, from_=10, to=CRF_MAX[key], width=4, textvariable=self.v_crf[key]
                        ).grid(row=r, column=2, sticky="e")
            ttk.Label(qi, text=CODEC_NOTES[key], style="Card.Muted.TLabel").grid(row=r + 1, column=0, columnspan=3,
                                                                                   sticky="w", padx=(24, 0), pady=(0, 4))
            r += 2
        ttk.Label(qi, style="Card.Muted.TLabel", wraplength=340, justify="left",
                  text="CRF — число качества: меньше — лучше картинка, но больше файл. "
                       "Если не уверены, оставьте «Баланс» или нажмите «Сравнить качество»."
                  ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # --- скорость
        sp = self._card(col, "Скорость")
        sp.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        si = sp.inner  # type: ignore[attr-defined]
        si.columnconfigure(0, weight=1)
        head = ttk.Frame(si, style="Card.TFrame")
        head.grid(row=1, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="Нагрузка на процессор", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, textvariable=self.v_cpu_text, style="Card.Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.scale = ttk.Scale(si, from_=10, to=100, orient="horizontal", style="Card.Horizontal.TScale",
                               command=self._cpu_moved)
        self.scale.grid(row=2, column=0, sticky="ew", pady=(6, 2))
        ttk.Label(si, style="Card.Muted.TLabel", wraplength=340, justify="left",
                  text="Сколько потоков процессора занять. Больше — быстрее, но компьютер заметно загружен. "
                       "У разных кодеков прирост разный."
                  ).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(si, text="Низкий приоритет (компьютер не тормозит)", variable=self.v_lowprio,
                        style="Card.TCheckbutton").grid(row=4, column=0, sticky="w", pady=(6, 10))
        ttk.Label(si, text="Видеокарта", style="Card.TLabel").grid(row=5, column=0, sticky="w")
        self.cb_gpu = ttk.Combobox(si, textvariable=self.v_gpu, state="disabled", values=[GPU_WAIT])
        self.cb_gpu.grid(row=6, column=0, sticky="ew", pady=(4, 4))
        self.cb_gpu.bind("<<ComboboxSelected>>", lambda e: self._update_gpu_note())
        self.l_gpu_note = ttk.Label(si, textvariable=self.v_gpu_note, style="Card.Muted.TLabel", wraplength=340,
                                    justify="left")
        self.l_gpu_note.grid(row=7, column=0, sticky="w")
        ttk.Checkbutton(si, text="Быстрый режим (быстрее, файл чуть больше)", variable=self.v_fast,
                        style="Card.TCheckbutton").grid(row=8, column=0, sticky="w", pady=(10, 0))

        # --- звук и кадры
        av = self._card(col, "Звук и кадры")
        av.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ai = av.inner  # type: ignore[attr-defined]
        ai.columnconfigure(1, weight=1)
        ttk.Label(ai, text="Звук", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(ai, textvariable=self.v_audio, values=list(AUDIO_CHOICES), state="readonly", width=26
                     ).grid(row=1, column=1, sticky="ew", padx=(12, 0))
        ttk.Label(ai, text="Частота кадров", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(ai, textvariable=self.v_fps, values=list(FPS_CHOICES), state="readonly", width=26
                     ).grid(row=2, column=1, sticky="ew", padx=(12, 0))
        ttk.Checkbutton(ai, text="10-бит цвет (для HDR и плавных градиентов)", variable=self.v_ten,
                        style="Card.TCheckbutton").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- оценка размера
        es = self._card(col, "Оценка размера")
        es.grid(row=3, column=0, sticky="ew")
        ei = es.inner  # type: ignore[attr-defined]
        ttk.Checkbutton(ei, text="Считать размер автоматически", variable=self.v_auto_est, style="Card.TCheckbutton",
                        command=lambda: self._schedule_estimate(force=self.v_auto_est.get())
                        ).grid(row=1, column=0, sticky="w")
        self.b_estimate = ttk.Button(ei, text="Оценить сейчас", command=lambda: self._start_estimate(force=True))
        self.b_estimate.grid(row=1, column=1, sticky="e")
        ei.columnconfigure(0, weight=1)
        ttk.Label(ei, style="Card.Muted.TLabel", wraplength=340, justify="left",
                  text="Программа кодирует три коротких фрагмента вашими настройками и пересчитывает на всё видео. "
                       "Точность обычно ±5–20%."
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._sync_out()
        self._refresh_empty()

    def _wheel(self, event) -> None:
        if self.canvas.bbox("all") and self.canvas.bbox("all")[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ------------------------------------------------------------------ тема
    def _theme_changed(self) -> None:
        self.pal = theme.apply(self, self.v_theme.get())
        self._recolor()
        for fn in list(self.listeners):
            fn()

    def _recolor(self) -> None:
        p = self.pal
        for f, role in self._cards:
            f.configure(highlightbackground=p["border"], bg=p["panel"] if role == "card" else p["border"])
        for w in (self.l_title,):
            w.configure(bg=p["bg"], fg=p["fg"])
        for w in (self.l_sub,):
            w.configure(bg=p["bg"], fg=p["muted"])
        self.l_summary.configure(bg=p["bg"], fg=p["fg"])
        self.l_status.configure(bg=p["bg"], fg=p["muted"])
        self.empty.configure(bg=p["field"], fg=p["muted"])
        self.canvas.configure(bg=p["bg"])
        self.log.configure(bg=p["panel"], fg=p["muted"], highlightbackground=p["border"], highlightcolor=p["border"],
                           insertbackground=p["fg"])
        self.tree.tag_configure("ok", foreground=p["ok"])
        self.tree.tag_configure("err", foreground=p["err"])

    # ------------------------------------------------------------------ список файлов
    def _refresh_empty(self) -> None:
        if self.items:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")

    def _add_paths(self, paths: list[Path]) -> None:
        found = find_videos(paths, recursive=True)
        known = {p.resolve() for p in self.items.values()}
        added = 0
        for f in found:
            if f.resolve() in known:
                continue
            try:
                size = human(f.stat().st_size)
            except OSError:
                continue
            iid = self.tree.insert("", "end", text=f.name, values=(size, "", "Ожидает"))
            self.items[iid] = f
            added += 1
        if paths and all(p.is_file() for p in paths) and len(paths) - len(found) > 0:
            self._log(f"Пропущено файлов (не видео или уже сжатые): {len(paths) - len(found)}")
        if added:
            self.v_status.set(f"Видео в списке: {len(self.items)}")
            self._schedule_estimate()
        self._refresh_empty()
        self._refresh_estimates()

    def _on_drop(self, event) -> None:
        if not self._busy:
            self._add_paths([Path(p) for p in self.tk.splitlist(event.data)])

    def _add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        names = filedialog.askopenfilenames(title="Выберите видео", filetypes=[("Видео", exts), ("Все файлы", "*.*")])
        if names:
            self._add_paths([Path(n) for n in names])

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(title="Папка с видео")
        if d:
            self._add_paths([Path(d)])

    def _remove_selected(self) -> None:
        if self._busy:
            return
        for iid in self.tree.selection():
            self.items.pop(iid, None)
            self.actual.pop(iid, None)
            self.tree.delete(iid)
        self._refresh_empty()
        self._refresh_estimates()

    def _clear(self) -> None:
        if self._busy:
            return
        self._stop_estimate()
        self.items.clear()
        self.actual.clear()
        self.tree.delete(*self.tree.get_children())
        self._refresh_empty()
        self.v_status.set("Добавьте видео, чтобы начать")
        self.bar["value"] = 0
        self._refresh_estimates()

    # ------------------------------------------------------------------ настройки
    def _preset_selected(self, _e=None) -> None:
        name = self.v_preset.get()
        if name in PRESETS:
            self._applying_preset = True
            for k, v in PRESETS[name].items():
                self.v_crf[k].set(v)
            self._applying_preset = False
            self._schedule_estimate()

    def _crf_changed(self, *_a) -> None:
        if self._applying_preset:
            return
        try:
            cur = {k: self.v_crf[k].get() for k in CODECS}
        except tk.TclError:
            return
        for name, vals in PRESETS.items():
            if vals == cur:
                self.v_preset.set(name)
                return
        self.v_preset.set(CUSTOM)

    def _settings_changed(self, *_a) -> None:
        self._schedule_estimate()

    def _cpu_moved(self, value: str) -> None:
        pct = max(10, min(100, round(float(value) / CPU_STEP) * CPU_STEP))
        self.v_cpu.set(pct)
        total = cpu_threads()
        n = threads_for_percent(pct) or total
        self.v_cpu_text.set(f"{pct}%  ·  {n} из {total} потоков")

    def _set_cpu(self, pct: int) -> None:
        self.scale.set(pct)
        self._cpu_moved(str(pct))

    def _gpu_vendor(self) -> str | None:
        label = self.v_gpu.get()
        for vendor, text in VENDORS.items():
            if text == label and vendor in self.gpu_found:
                return vendor
        return None

    def _update_gpu_note(self) -> None:
        vendor = self._gpu_vendor()
        if not self.gpu_found:
            self.v_gpu_note.set("Видеокарта с поддержкой кодирования не найдена — всё считает процессор.")
            return
        if vendor is None:
            self.v_gpu_note.set("Процессор даёт лучшее качество при том же размере файла, но работает медленнее. "
                                "Видеокарта доступна — можно включить ради скорости.")
            return
        on_gpu = ", ".join(CODECS[k].label for k in self.gpu_found[vendor])
        off_gpu = [CODECS[k].label for k in CODECS if k not in self.gpu_found[vendor]]
        self.v_gpu_note.set(
            f"На видеокарте будет считаться: {on_gpu}; остальное ({', '.join(off_gpu)}) — на процессоре.\n"
            "Видеокарта работает в разы быстрее, но при том же CRF файл получается заметно больше, "
            "а при равном размере картинка хуже: возможны блочные «квадратики», особенно в тёмных сценах "
            "и на градиентах. Для максимального качества при малом размере выбирайте процессор.")

    def _sync_out(self) -> None:
        state = "normal" if self.v_out_mode.get() == "custom" else "disabled"
        self.e_out.configure(state=state)
        self.b_out.configure(state=state)

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="Папка для результата")
        if d:
            self.v_out_dir.set(d)

    def _collect_options(self, quiet: bool = False) -> Options | None:
        def warn(msg: str) -> None:
            if not quiet:
                messagebox.showwarning(APP_NAME, msg)

        codecs = tuple(k for k in CODECS if self.v_use[k].get())
        if not codecs:
            warn("Выберите хотя бы один формат (VP9, H.265 или AV1).")
            return None
        crf = {}
        for k in codecs:
            try:
                v = int(self.v_crf[k].get())
            except (tk.TclError, ValueError):
                warn(f"Некорректное значение CRF для {CODECS[k].label}.")
                return None
            crf[k] = max(0, min(CRF_MAX[k], v))
        out_dir = None
        if self.v_out_mode.get() == "custom":
            if not self.v_out_dir.get().strip():
                warn("Укажите папку для результата.")
                return None
            out_dir = Path(self.v_out_dir.get().strip())
        vendor = self._gpu_vendor()
        return Options(
            codecs=codecs, crf=crf, audio=AUDIO_CHOICES[self.v_audio.get()],
            max_fps=FPS_CHOICES[self.v_fps.get()], ten_bit=self.v_ten.get(),
            fast=self.v_fast.get(), out_dir=out_dir,
            threads=threads_for_percent(self.v_cpu.get()),
            hw=vendor, hw_codecs=tuple(self.gpu_found.get(vendor, ())) if vendor else (),
        )

    # ------------------------------------------------------------------ оценка размера
    def _est_key(self, path: Path, opts: Options, key: str) -> tuple | None:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        hw = opts.hw if key in opts.hw_codecs else None
        return (str(path), mtime, key, opts.crf.get(key), hw, opts.fast, opts.ten_bit, opts.max_fps, opts.audio)

    def _schedule_estimate(self, force: bool = False) -> None:
        if self._loading:
            return
        if self._est_after:
            self.after_cancel(self._est_after)
        self._est_after = self.after(900, lambda: self._start_estimate(force=force))
        self._refresh_estimates()

    def _stop_estimate(self) -> None:
        self._est_cancel.set()
        self._est_active = False

    def _start_estimate(self, force: bool = False) -> None:
        self._est_after = None
        self._stop_estimate()
        if self._busy or not self.items:
            self._refresh_estimates()
            return
        if not (force or self.v_auto_est.get()):
            self._refresh_estimates()
            return
        opts = self._collect_options(quiet=True)
        if opts is None:
            self._refresh_estimates()
            return
        todo = []
        for iid, path in self.items.items():
            if iid in self.actual:
                continue
            missing = tuple(k for k in opts.codecs if self._est_key(path, opts, k) not in self.est)
            if missing:
                todo.append((iid, path, missing))
        if not todo:
            self._refresh_estimates()
            return
        self._est_gen += 1
        gen = self._est_gen
        self._est_cancel = cancel = threading.Event()
        self._est_active = True
        self.est_err.clear()
        self._est_thread = threading.Thread(target=self._est_worker, args=(gen, todo, opts, cancel), daemon=True)
        self._est_thread.start()
        self._refresh_estimates()

    def _est_worker(self, gen: int, todo: list, opts: Options, cancel: threading.Event) -> None:
        q = self.events
        for iid, path, codecs in todo:
            if cancel.is_set():
                return
            q.put(("est_start", gen, iid))
            try:
                res = estimate_file(path, opts, codecs, cancel)
            except Cancelled:
                return
            except Exception as e:  # noqa: BLE001
                q.put(("est_err", gen, iid, _last_line(e)))
                continue
            q.put(("est", gen, iid, path, opts, res))
        q.put(("est_done", gen))

    def _refresh_estimates(self) -> None:
        opts = self._collect_options(quiet=True)
        total_src = 0
        sums = {k: 0 for k in (opts.codecs if opts else ())}
        known_files = 0
        for iid, path in self.items.items():
            try:
                total_src += path.stat().st_size
            except OSError:
                pass
            if iid in self.actual:
                self.tree.set(iid, "est", self.actual[iid])
                continue
            if opts is None:
                self.tree.set(iid, "est", "")
                continue
            parts, complete = [], True
            for k in opts.codecs:
                v = self.est.get(self._est_key(path, opts, k))
                if v is None:
                    complete = False
                else:
                    parts.append(f"{CODECS[k].label} ≈ {human(v)}")
                    sums[k] += v
            if complete:
                known_files += 1
            if parts:
                text = "  ·  ".join(parts)
            elif iid in self.est_err:
                text = "не удалось оценить"
            elif self._est_active:
                text = "считаю…"
            else:
                text = "—"
            self.tree.set(iid, "est", text)
        if not self.items or opts is None:
            self.v_summary.set("")
        elif known_files == len([i for i in self.items if i not in self.actual]) and known_files:
            cmp = "   ·   ".join(f"{CODECS[k].label} ≈ {human(v)}" for k, v in sums.items())
            self.v_summary.set(f"Исходники {human(total_src)}  →  после сжатия: {cmp}")
        elif self._est_active:
            self.v_summary.set("Считаю, сколько места займёт результат…")
        else:
            self.v_summary.set("Размер после сжатия — по кнопке «Оценить сейчас» (автоматический расчёт выключен)."
                               if not self.v_auto_est.get() else "")

    # ------------------------------------------------------------------ запуск сжатия
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.b_add, self.b_folder, self.b_remove, self.b_clear, self.b_compare, self.b_estimate):
            b.configure(state=state)
        self.b_start.configure(text="Остановить" if busy else "Сжать")

    def _start(self) -> None:
        if self._busy:
            self.cancel.set()
            self.v_status.set("Останавливаю…")
            return
        if not self.items:
            messagebox.showinfo(APP_NAME, "Сначала добавьте видео: перетащите файлы в окно или нажмите «Добавить видео».")
            return
        opts = self._collect_options()
        if opts is None:
            return
        self._stop_estimate()
        for iid in self.items:
            self.tree.item(iid, tags=())
            self.tree.set(iid, "status", "Ожидает")
        self.actual.clear()
        self.cancel = threading.Event()
        self.bar["value"] = 0
        self.b_open.configure(state="disabled")
        self._set_busy(True)
        self.v_status.set("Начинаю…")
        files = list(self.items.items())
        self.worker = threading.Thread(target=self._work, args=(files, opts, self.cancel), daemon=True)
        self.worker.start()

    def _work(self, files, opts: Options, cancel: threading.Event) -> None:
        q = self.events
        n = len(files)
        tot_in = tot_out = done = failed = 0
        cancelled = False
        for i, (iid, path) in enumerate(files):
            if cancel.is_set():
                cancelled = True
                break
            q.put(("start", iid, i, n, path.name))

            def cb(key, frac, i=i, iid=iid):
                idx = opts.codecs.index(key)
                ff = (idx + frac) / len(opts.codecs)
                q.put(("progress", iid, (i + ff) / n, ff, CODECS[key].label))

            try:
                results = compress_file(path, opts, on_progress=cb, log=lambda m: q.put(("log", m)), cancel=cancel)
            except Cancelled:
                q.put(("status", iid, "Отменено", ""))
                cancelled = True
                break
            except Exception as e:  # noqa: BLE001 — показываем любую ошибку пользователю
                q.put(("status", iid, "Ошибка", "err"))
                q.put(("log", f"{path.name}: {_last_line(e)}"))
                failed += 1
                continue
            src = path.stat().st_size
            parts = []
            for r in results:
                pct = (1 - r.size / src) * 100 if src else 0
                parts.append(f"{CODECS[r.codec].label} {human(r.size)} ({'−' if pct >= 0 else '+'}{abs(pct):.0f}%)")
            q.put(("done", iid, "  ·  ".join(parts)))
            q.put(("output", results[0].path))
            tot_in += src
            tot_out += min(r.size for r in results)
            done += 1
        q.put(("finished", cancelled, done, failed, tot_in, tot_out))

    # ------------------------------------------------------------------ события из потоков
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "start":
            _, iid, i, n, name = ev
            self.tree.set(iid, "status", "Анализ…")
            self.tree.see(iid)
            self.v_status.set(f"Файл {i + 1} из {n}: {name}")
        elif kind == "progress":
            _, iid, overall, ff, label = ev
            self.bar["value"] = overall * 1000
            self.tree.set(iid, "status", f"{label}: {ff * 100:.0f}%")
        elif kind == "status":
            _, iid, text, tag = ev
            self.tree.set(iid, "status", text)
            self.tree.item(iid, tags=(tag,) if tag else ())
        elif kind == "done":
            _, iid, text = ev
            self.actual[iid] = text
            self.tree.set(iid, "est", text)
            self.tree.set(iid, "status", "Готово")
            self.tree.item(iid, tags=("ok",))
        elif kind == "output":
            self.last_output = ev[1]
        elif kind == "log":
            self._log(ev[1])
        elif kind == "finished":
            _, cancelled, done, failed, tin, tout = ev
            self._set_busy(False)
            if cancelled:
                self.v_status.set("Остановлено")
            else:
                self.bar["value"] = 1000
                if done and tin:
                    self.v_status.set(f"Готово: {human(tin)} → {human(tout)} (экономия {(1 - tout / tin) * 100:.0f}%)"
                                      + (f", ошибок: {failed}" if failed else ""))
                else:
                    self.v_status.set("Не удалось сжать ни одного файла" if failed else "Готово")
            if self.last_output:
                self.b_open.configure(state="normal")
            self._schedule_estimate()
        elif kind == "gpu":
            self._gpu_detected(ev[1])
        elif kind == "est_start":
            pass
        elif kind == "est":
            _, gen, iid, path, opts, res = ev
            for k, v in res.items():
                key = self._est_key(path, opts, k)
                if key:
                    self.est[key] = v
            self._refresh_estimates()
        elif kind == "est_err":
            _, gen, iid, msg = ev
            self.est_err[iid] = msg
            self._log(f"Оценка размера не удалась: {msg}")
            self._refresh_estimates()
        elif kind == "est_done":
            if ev[1] == self._est_gen:
                self._est_active = False
                self._refresh_estimates()

    def _gpu_detected(self, found: dict[str, list[str]]) -> None:
        self.gpu_found = found
        if found:
            self.cb_gpu.configure(values=[GPU_NONE, *(VENDORS[v] for v in found)], state="readonly")
            want = getattr(self, "_saved_gpu", None)
            self.v_gpu.set(VENDORS[want] if want in found else GPU_NONE)
        else:
            self.cb_gpu.configure(values=["Не найдена"], state="disabled")
            self.v_gpu.set("Не найдена")
        self._update_gpu_note()

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        if self.last_output:
            open_in_explorer(self.last_output)

    # ------------------------------------------------------------------ сравнение
    def _open_compare(self) -> None:
        sel = self.tree.selection()
        if not sel:
            if len(self.items) == 1:
                sel = tuple(self.items)
            else:
                messagebox.showinfo(APP_NAME, "Выберите в списке одно видео, на котором сравнить качество.")
                return
        CompareWindow(self, self.items[sel[0]])

    def apply_choice(self, codec: str, crf: int) -> None:
        for k in CODECS:
            self.v_use[k].set(k == codec)
        self.v_crf[codec].set(crf)

    # ------------------------------------------------------------------ сохранение настроек
    def _read_settings(self) -> dict:
        try:
            return json.loads(settings_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _apply_settings(self, d: dict) -> None:
        self._saved_gpu = d.get("gpu")
        try:
            self._applying_preset = True
            for k in CODECS:
                self.v_use[k].set(bool(d.get("use", {}).get(k, self.v_use[k].get())))
                self.v_crf[k].set(int(d.get("crf", {}).get(k, self.v_crf[k].get())))
            self._applying_preset = False
            self.v_preset.set(d.get("preset", self.v_preset.get()))
            for var, key, choices in ((self.v_audio, "audio", AUDIO_CHOICES), (self.v_fps, "fps", FPS_CHOICES)):
                if d.get(key) in choices:
                    var.set(d[key])
            self.v_ten.set(bool(d.get("ten", False)))
            self.v_fast.set(bool(d.get("fast", False)))
            self.v_lowprio.set(bool(d.get("lowprio", True)))
            self.v_auto_est.set(bool(d.get("auto_est", True)))
            self.v_out_mode.set(d.get("out_mode", "same"))
            self.v_out_dir.set(d.get("out_dir", ""))
            self._sync_out()
            self._set_cpu(int(d.get("cpu", 75)))
        except Exception:
            self._applying_preset = False
            self._set_cpu(75)

    def _save_settings(self) -> None:
        try:
            d = {
                "theme": self.v_theme.get(),
                "use": {k: v.get() for k, v in self.v_use.items()},
                "crf": {k: int(v.get()) for k, v in self.v_crf.items()},
                "preset": self.v_preset.get(), "audio": self.v_audio.get(), "fps": self.v_fps.get(),
                "ten": self.v_ten.get(), "fast": self.v_fast.get(), "cpu": self.v_cpu.get(),
                "lowprio": self.v_lowprio.get(), "auto_est": self.v_auto_est.get(),
                "gpu": self._gpu_vendor() if self.gpu_found else getattr(self, "_saved_gpu", None),
                "out_mode": self.v_out_mode.get(), "out_dir": self.v_out_dir.get(),
            }
            p = settings_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._busy and not messagebox.askyesno(APP_NAME, "Идёт сжатие. Остановить и выйти?"):
            return
        self.cancel.set()
        self._est_cancel.set()
        self._save_settings()
        for t in (self.worker, self._est_thread):
            if t and t.is_alive():
                t.join(timeout=3)
        self.destroy()


class CompareWindow(tk.Toplevel):
    """Окно «Сравнить качество»: кодирует короткие фрагменты и показывает таблицу."""

    TARGETS = {"Почти без потерь (SSIM 0.98)": 0.98, "Хорошее (SSIM 0.97)": 0.97, "Компактное (SSIM 0.95)": 0.95}

    def __init__(self, app: App, src: Path) -> None:
        super().__init__(app)
        self.app, self.src = app, src
        self.title(f"Сравнение качества — {src.name}")
        self.geometry("680x520")
        self.minsize(600, 440)
        self.transient(app)
        self.cancel = threading.Event()
        self.events: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self._rec: Candidate | None = None
        self.retheme()
        app.listeners.append(self.retheme)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        ttk.Label(self, wraplength=620, justify="left", style="Muted.TLabel",
                  text="Три коротких фрагмента (по 1 с) сжимаются разными способами. Видно, какой вариант даёт "
                       "самый маленький файл при нужном качестве. Форматы, CRF и видеокарта берутся из главного окна."
                  ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 8))
        top = ttk.Frame(self)
        top.grid(row=1, column=0, sticky="ew", padx=20, pady=4)
        ttk.Label(top, text="Нужное качество").pack(side="left")
        self.v_target = tk.StringVar(value="Хорошее (SSIM 0.97)")
        ttk.Combobox(top, textvariable=self.v_target, values=list(self.TARGETS), state="readonly", width=30
                     ).pack(side="left", padx=12)
        self.b_run = ttk.Button(top, text="Запустить", command=self._run)
        self.b_run.pack(side="left")

        box = tk.Frame(self, highlightthickness=1, highlightbackground=app.pal["border"])
        box.grid(row=3, column=0, sticky="nsew", padx=20, pady=8)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(box, columns=("fmt", "crf", "size", "ssim", "note"), show="headings", height=8)
        for c, t, w, a in (("fmt", "Формат", 90, "w"), ("crf", "CRF", 56, "e"), ("size", "Размер (оценка)", 130, "e"),
                           ("ssim", "SSIM", 80, "e"), ("note", "", 150, "w")):
            self.tree.heading(c, text=t, anchor=a)
            self.tree.column(c, width=w, anchor=a)
        self.tree.tag_configure("rec", background=app.pal["select"], foreground=app.pal["ok"])
        self.tree.grid(row=0, column=0, sticky="nsew")

        self.v_status = tk.StringVar(value=f"Исходник: {human(src.stat().st_size)}")
        ttk.Label(self, textvariable=self.v_status, wraplength=620, style="Muted.TLabel"
                  ).grid(row=4, column=0, sticky="w", padx=20)
        self.bar = ttk.Progressbar(self, mode="indeterminate")
        self.bar.grid(row=5, column=0, sticky="ew", padx=20, pady=8)
        bot = ttk.Frame(self)
        bot.grid(row=6, column=0, sticky="e", padx=20, pady=(2, 18))
        self.b_apply = ttk.Button(bot, text="Применить рекомендацию", command=self._apply, state="disabled")
        self.b_apply.pack(side="left", padx=8)
        ttk.Button(bot, text="Закрыть", command=self._close).pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(150, self._poll)

    def retheme(self) -> None:
        if self.winfo_exists():
            self.configure(bg=self.app.pal["bg"])
            theme.titlebar(self, self.app.pal)

    def _candidates(self, opts: Options) -> list[tuple[str, int]]:
        codecs = list(opts.codecs)
        offsets = {1: (-8, -4, 0, 4, 8), 2: (-6, 0, 6)}.get(len(codecs), (-5, 5))
        out = []
        for k in codecs:
            base = opts.crf[k]
            out += [(k, max(0, min(CRF_MAX[k], base + o))) for o in offsets]
        return out

    def _run(self) -> None:
        opts = self.app._collect_options(quiet=True)
        if opts is None:
            self.v_status.set("Выберите в главном окне хотя бы один формат.")
            return
        self.tree.delete(*self.tree.get_children())
        self.b_run.configure(state="disabled")
        self.b_apply.configure(state="disabled")
        self.cancel = threading.Event()
        cands = self._candidates(opts)
        target = self.TARGETS[self.v_target.get()]
        self.v_status.set("Готовлю эталон…")
        self.bar.start(12)
        q = self.events

        def work() -> None:
            try:
                items, _dur = compare_file(self.src, cands, opts, target, log=lambda m: q.put(("log", m)),
                                           cancel=self.cancel)
                q.put(("done", items))
            except Cancelled:
                q.put(("stopped",))
            except Exception as e:  # noqa: BLE001
                q.put(("error", _last_line(e)))

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "log":
                    self.v_status.set(ev[1])
                elif ev[0] == "done":
                    self._show(ev[1])
                elif ev[0] == "error":
                    self._finish(f"Ошибка: {ev[1]}")
                elif ev[0] == "stopped":
                    self._finish("Остановлено")
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _finish(self, text: str) -> None:
        self.bar.stop()
        self.bar["value"] = 0
        self.v_status.set(text)
        self.b_run.configure(state="normal")

    def _show(self, items: list[Candidate]) -> None:
        for c in sorted(items, key=lambda c: c.estimated):
            note = "рекомендация" if c.recommended else ("оптимальный" if c.pareto else "")
            self.tree.insert("", "end", values=(CODECS[c.codec].label, c.crf, human(c.estimated), f"{c.ssim:.4f}", note),
                             tags=("rec",) if c.recommended else ())
        rec = next((c for c in items if c.recommended), None)
        self._finish(f"Исходник: {human(self.src.stat().st_size)}. Размеры — только видео, без звука."
                     + (f" Рекомендую: {CODECS[rec.codec].label}, CRF {rec.crf}." if rec else ""))
        self._rec = rec
        if rec:
            self.b_apply.configure(state="normal")

    def _apply(self) -> None:
        if self._rec:
            self.app.apply_choice(self._rec.codec, self._rec.crf)
            self._close()

    def _close(self) -> None:
        self.cancel.set()
        if self.retheme in self.app.listeners:
            self.app.listeners.remove(self.retheme)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.destroy()


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        ffmpeg_path()
    except FFmpegError as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, str(e))
        return 1
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

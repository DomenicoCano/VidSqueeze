"""Сжатие одного файла."""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .codecs import CODECS, video_args
from .ffmpeg import MediaInfo, ffmpeg_path, probe, run, run_capture

SILENCE_DBFS = -50.0
OUTPUT_NAME = re.compile(r"-(?:%s)(?:-\d+)?$" % "|".join(CODECS))   # clip-vp9, clip-h265-1
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp"}


@dataclass
class Options:
    codecs: tuple[str, ...] = ("vp9", "h265")
    crf: dict[str, int] = field(default_factory=dict)   # переопределения CRF по кодекам
    audio: str = "auto"                                  # auto | keep | drop
    max_fps: float = 30.0                                # 0 = не ограничивать
    ten_bit: bool = False
    fast: bool = False
    out_dir: Optional[Path] = None
    threads: int = 0                                     # 0 = все потоки процессора
    hw: Optional[str] = None                             # производитель видеокарты: nvidia | intel | amd
    hw_codecs: tuple[str, ...] = ()                      # какие кодеки видеокарта реально умеет


@dataclass
class Result:
    codec: str
    path: Path
    size: int
    command: list[str]


def target_fps(src_fps: float, max_fps: float) -> Optional[float]:
    """None — оставить как есть. Иначе целая доля исходной частоты (60 -> 30, 50 -> 25, 120 -> 30),
    а если доля получается заметно ниже лимита (31 -> 15.5, 40 -> 20) — сам лимит.

    Запас 5% нужен для телефонных видео с переменной частотой: они показывают 30.02 или 30.3 fps,
    и без запаса такие ролики резались бы до 15 fps."""
    if max_fps <= 0 or src_fps <= max_fps * 1.05:
        return None
    n = math.ceil(src_fps / max_fps - 1e-6)
    share = src_fps / n
    return round(share, 3) if share >= max_fps * 0.8 else float(max_fps)


def video_filter(info: MediaInfo, max_fps: float) -> Optional[str]:
    parts = []
    fps = target_fps(info.fps, max_fps)
    if fps:
        parts.append(f"fps={fps}")
    if info.width % 2 or info.height % 2:   # кодекам нужны чётные размеры
        parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return ",".join(parts) or None


_peak_cache: dict[tuple, float] = {}


def audio_peak_db(path: Path, stream: int) -> float:
    """Пиковый уровень дорожки в dBFS. Кэшируется: анализ декодирует весь звук, а оценка размера
    вызывает его при каждом изменении настроек."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, stream)
    if key not in _peak_cache:
        _peak_cache[key] = _measure_peak(path, stream)
    return _peak_cache[key]


def _measure_peak(path: Path, stream: int) -> float:
    err = run_capture(["-i", str(path), "-map", f"0:a:{stream}", "-vn",
                       "-af", "astats=metadata=0", "-f", "null", "-"])
    overall = err.split("Overall", 1)[-1]
    m = re.search(r"Peak level dB:\s*(-?[\d.]+|-inf)", overall)
    if not m or m[1] == "-inf":
        return float("-inf")
    return float(m[1])


def pick_audio_stream(info: MediaInfo, mode: str) -> Optional[int]:
    """Номер звуковой дорожки, которую сохранить, или None — без звука.

    auto: самая громкая дорожка, если она не тише −50 dBFS. Важно для записей OBS и игр, где звук
    разложен по нескольким дорожкам и первая бывает пустой."""
    if mode == "drop" or info.audio_streams == 0:
        return None
    if mode == "keep":
        return 0
    peaks = [audio_peak_db(info.path, i) for i in range(info.audio_streams)]
    best = max(range(len(peaks)), key=lambda i: peaks[i])
    return best if peaks[best] > SILENCE_DBFS else None


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def codec_args(opts: Options, key: str, crf: Optional[int] = None, hdr: bool = False) -> list[str]:
    """Аргументы видеокодера с учётом всех настроек (CRF, потоки, видеокарта, 10-бит).
    HDR-исходник всегда кодируется в 10 бит: в 8 битах на градиентах HLG/HDR10 появляются полосы."""
    if crf is None:
        crf = opts.crf.get(key, CODECS[key].default_crf)
    hw = opts.hw if key in opts.hw_codecs else None
    return video_args(key, crf, opts.fast, opts.ten_bit or hdr, opts.threads, hw)


def build_command(info: MediaInfo, codec_key: str, out: Path, opts: Options, audio: Optional[int]) -> list[str]:
    codec = CODECS[codec_key]
    args = ["-n", "-i", str(info.path), "-map", "0:v:0"]
    vf = video_filter(info, opts.max_fps)
    if vf:
        args += ["-vf", vf]
    args += codec_args(opts, codec_key, hdr=info.hdr)
    if audio is not None:
        args += ["-map", f"0:a:{audio}", "-c:a", codec.audio_codec]
    else:
        args += ["-an"]
    args += [str(out)]
    return args


def compress_file(
    src: Path,
    opts: Options,
    on_progress: Optional[Callable[[str, float], None]] = None,
    log: Callable[[str], None] = print,
    cancel: Optional[threading.Event] = None,
) -> list[Result]:
    info = probe(src)
    audio = pick_audio_stream(info, opts.audio)
    if opts.audio == "auto" and info.audio_streams:
        if audio is None:
            log("  звук тихий (≤ −50 dBFS) — удаляю")
        elif audio > 0:
            log(f"  звук: беру дорожку {audio + 1} из {info.audio_streams} — в остальных тише")
    if info.hdr and not opts.ten_bit:
        log("  HDR-видео — кодирую в 10 бит, чтобы не было полос на градиентах")
    out_dir = opts.out_dir or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for key in opts.codecs:
        codec = CODECS[key]
        out = unique_path(out_dir / f"{src.stem}-{key}{codec.ext}")
        args = build_command(info, key, out, opts, audio)
        try:
            run(args, info.duration, (lambda f, k=key: on_progress(k, f)) if on_progress else None, cancel)
        except Exception:
            out.unlink(missing_ok=True)  # не оставляем недописанный файл
            raise
        results.append(Result(key, out, out.stat().st_size, [ffmpeg_path(), *args]))
    return results


def find_videos(inputs: list[Path], recursive: bool) -> list[Path]:
    """Видео из файлов и папок. В папках пропускаются наши же результаты (`-vp9`, `-h265-1` …),
    чтобы повторный запуск не пережимал их; явно выбранный файл берётся всегда."""
    files: list[tuple[Path, bool]] = []
    for p in inputs:
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files += [(f, True) for f in sorted(it) if f.is_file()]
        elif p.is_file():
            files.append((p, False))
    seen, out = set(), []
    for f, scanned in files:
        if f.suffix.lower() not in VIDEO_EXTS or (scanned and OUTPUT_NAME.search(f.stem)):
            continue
        if f.resolve() not in seen:
            seen.add(f.resolve())
            out.append(f)
    return out

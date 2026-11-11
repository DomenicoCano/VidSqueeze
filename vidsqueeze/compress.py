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
OUTPUT_SUFFIXES = tuple(f"-{k}" for k in CODECS)
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
    """None — оставить как есть. Иначе кратное делителю исходной частоты (60 -> 30, 50 -> 25)."""
    if max_fps <= 0 or src_fps <= max_fps + 0.01:
        return None
    n = math.ceil(src_fps / max_fps - 1e-6)
    return round(src_fps / n, 3)


def video_filter(info: MediaInfo, max_fps: float) -> Optional[str]:
    parts = []
    fps = target_fps(info.fps, max_fps)
    if fps:
        parts.append(f"fps={fps}")
    if info.width % 2 or info.height % 2:   # кодекам нужны чётные размеры
        parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return ",".join(parts) or None


def audio_peak_db(path: Path, stream: int) -> float:
    err = run_capture(["-i", str(path), "-map", f"0:a:{stream}", "-vn",
                       "-af", "astats=metadata=0", "-f", "null", "-"])
    overall = err.split("Overall", 1)[-1]
    m = re.search(r"Peak level dB:\s*(-?[\d.]+|-inf)", overall)
    if not m or m[1] == "-inf":
        return float("-inf")
    return float(m[1])


def audio_is_silent(info: MediaInfo) -> bool:
    if info.audio_streams == 0:
        return True
    peaks = [audio_peak_db(info.path, i) for i in range(info.audio_streams)]
    return max(peaks) <= SILENCE_DBFS


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def codec_args(opts: Options, key: str, crf: Optional[int] = None) -> list[str]:
    """Аргументы видеокодера с учётом всех настроек (CRF, потоки, видеокарта, 10-бит)."""
    if crf is None:
        crf = opts.crf.get(key, CODECS[key].default_crf)
    hw = opts.hw if key in opts.hw_codecs else None
    return video_args(key, crf, opts.fast, opts.ten_bit, opts.threads, hw)


def build_command(info: MediaInfo, codec_key: str, out: Path, opts: Options, keep_audio: bool) -> list[str]:
    codec = CODECS[codec_key]
    args = ["-n", "-i", str(info.path), "-map", "0:v:0"]
    vf = video_filter(info, opts.max_fps)
    if vf:
        args += ["-vf", vf]
    args += codec_args(opts, codec_key)
    if keep_audio:
        args += ["-map", "0:a:0", "-c:a", codec.audio_codec]
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
    if opts.audio == "drop":
        keep_audio = False
    elif opts.audio == "keep":
        keep_audio = info.audio_streams > 0
    else:
        keep_audio = not audio_is_silent(info)
        if info.audio_streams and not keep_audio:
            log("  звук тихий (≤ −50 dBFS) — удаляю")
    out_dir = opts.out_dir or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for key in opts.codecs:
        codec = CODECS[key]
        out = unique_path(out_dir / f"{src.stem}-{key}{codec.ext}")
        args = build_command(info, key, out, opts, keep_audio)
        try:
            run(args, info.duration, (lambda f, k=key: on_progress(k, f)) if on_progress else None, cancel)
        except Exception:
            out.unlink(missing_ok=True)  # не оставляем недописанный файл
            raise
        results.append(Result(key, out, out.stat().st_size, [ffmpeg_path(), *args]))
    return results


def find_videos(inputs: list[Path], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for p in inputs:
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files += sorted(f for f in it if f.is_file())
        elif p.is_file():
            files.append(p)
    seen, out = set(), []
    for f in files:
        if f.suffix.lower() not in VIDEO_EXTS or f.stem.endswith(OUTPUT_SUFFIXES):
            continue
        if f.resolve() not in seen:
            seen.add(f.resolve())
            out.append(f)
    return out

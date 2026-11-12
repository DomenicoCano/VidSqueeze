"""Поиск FFmpeg, разбор информации о файле и запуск процессов."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if sys.platform == "win32" else 0
_low_priority = False


def set_low_priority(enabled: bool) -> None:
    """Запускать ffmpeg с пониженным приоритетом, чтобы компьютер не тормозил."""
    global _low_priority
    _low_priority = enabled


def _flags() -> int:
    return _NOWINDOW | (_BELOW_NORMAL if _low_priority else 0)


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Системный ffmpeg, если есть; иначе встроенный из imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise FFmpegError(
            "FFmpeg не найден. Установите его или выполните: pip install imageio-ffmpeg"
        ) from exc


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    out = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-encoders"],
        capture_output=True, text=True, errors="replace", creationflags=_flags(),
    ).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(("V", "A")) and len(parts[0]) == 6:
            names.add(parts[1])
    return frozenset(names)


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    pix_fmt: str = ""
    audio_streams: int = 0
    size: int = 0
    hdr: bool = False          # HLG / HDR10 (PQ): кодировать в 10 бит, иначе на градиентах полосы
    audio_codecs: list[str] = field(default_factory=list)


_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+[^:]*:\s*Video:\s*([\w\-]+)(.*)")
_AUDIO = re.compile(r"Stream #\d+:\d+[^:]*:\s*Audio:\s*([\w\-]+)")
_DIMS = re.compile(r"[\s,](\d{2,6})x(\d{2,6})(?=[\s,\[])")
_FPS = re.compile(r"([\d.]+)\s*fps")
_TBR = re.compile(r"([\d.]+)\s*tbr")
_PIX = re.compile(r",\s*(yuv\w+|rgb\w*|gbr\w*|nv\d+\w*|gray\w*|pal8|bgr\w*)")


def probe(path: Path) -> MediaInfo:
    """Параметры файла (ffprobe не нужен: разбираем вывод `ffmpeg -i`)."""
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, errors="replace", creationflags=_flags(),
    )
    text = proc.stderr
    info = MediaInfo(path=path, size=path.stat().st_size)
    m = _DUR.search(text)
    if m:
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    for line in text.splitlines():
        v = _VIDEO.search(line)
        if v and not info.vcodec:
            info.vcodec = v[1]
            rest = v[2]
            info.hdr = bool(re.search(r"smpte2084|arib-std-b67", rest))
            # убираем скобки, чтобы "(High)" и "[SAR 1:1 DAR 16:9]" не мешали
            clean = re.sub(r"\([^)]*\)", "", rest)
            d = _DIMS.search(clean + " ")
            if d:
                info.width, info.height = int(d[1]), int(d[2])
            p = _PIX.search(clean)
            if p:
                info.pix_fmt = p[1]
            f = _FPS.search(clean) or _TBR.search(clean)
            if f:
                info.fps = float(f[1])
            continue
        a = _AUDIO.search(line)
        if a:
            info.audio_streams += 1
            info.audio_codecs.append(a[1])
    if not info.vcodec:
        raise FFmpegError(f"В файле нет видеопотока или он не читается: {path}")
    return info


class Cancelled(Exception):
    """Пользователь остановил обработку."""


def run(
    args: list[str],
    duration: float = 0.0,
    on_progress: Optional[Callable[[float], None]] = None,
    cancel: Optional["threading.Event"] = None,
) -> str:
    """Запускает ffmpeg; возвращает объединённый вывод. Ошибка -> FFmpegError."""
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", creationflags=_flags(),
    )
    log: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled()
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                t = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            if on_progress and duration > 0:
                on_progress(min(t / duration, 1.0))
        elif "=" in line and line.split("=", 1)[0] in {
            "frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time", "out_time_ms",
            "dup_frames", "drop_frames", "speed", "progress",
        }:
            continue
        elif line:
            log.append(line)
    code = proc.wait()
    if code != 0:
        raise FFmpegError("ffmpeg завершился с ошибкой:\n" + "\n".join(log[-15:]))
    return "\n".join(log)


def run_capture(args: list[str]) -> str:
    """Запуск для анализа (astats, ssim): возвращает stderr."""
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", *args],
        capture_output=True, text=True, errors="replace", creationflags=_flags(),
    )
    if proc.returncode != 0:
        raise FFmpegError("ffmpeg завершился с ошибкой:\n" + proc.stderr[-1500:])
    return proc.stderr

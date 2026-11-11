"""Определение того, какие аппаратные кодеры реально работают на этом компьютере."""
from __future__ import annotations

import os

from .codecs import HW_ENCODERS, VENDORS, video_args
from .ffmpeg import FFmpegError, available_encoders, run_capture


def cpu_threads() -> int:
    return os.cpu_count() or 4


def threads_for_percent(percent: int) -> int:
    """Доля потоков процессора. 0 = «все» (100%)."""
    if percent >= 100:
        return 0
    return max(1, round(cpu_threads() * percent / 100))


def _works(key: str, vendor: str) -> bool:
    """Пробное кодирование нескольких кадров: единственный надёжный способ проверить, что кодер доступен."""
    if HW_ENCODERS[(key, vendor)] not in available_encoders():
        return False
    try:
        run_capture(["-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10", "-frames:v", "8",
                     *video_args(key, 30, fast=True, hw=vendor), "-f", "null", "-"])
        return True
    except FFmpegError:
        return False


def detect_gpu_encoders() -> dict[str, list[str]]:
    """{'nvidia': ['h265'], ...} — только то, что реально заработало."""
    found: dict[str, list[str]] = {}
    for vendor in VENDORS:
        keys = [k for (k, v) in HW_ENCODERS if v == vendor and _works(k, vendor)]
        if keys:
            found[vendor] = keys
    return found

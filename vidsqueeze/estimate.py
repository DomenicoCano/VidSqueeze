"""Предварительная оценка размера результата (до запуска сжатия)."""
from __future__ import annotations

import math
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .codecs import CODECS
from .compare import reference_args
from .compress import Options, codec_args, pick_audio_stream, video_filter
from .ffmpeg import Cancelled, probe, run

# Битрейт звука по умолчанию у кодеров ffmpeg для стерео (используется только для оценки).
AUDIO_BPS = {"libopus": 96_000, "aac": 128_000}


def estimate_file(
    src: Path,
    opts: Options,
    codecs: tuple[str, ...],
    cancel: Optional[threading.Event] = None,
) -> dict[str, int]:
    """Оценка итогового размера файла (видео + звук) для каждого кодека, в байтах.

    Кодирует три 1-секундных фрагмента реальными настройками и пересчитывает на всю длительность."""
    info = probe(src)
    duration = max(info.duration, 0.1)
    audio = pick_audio_stream(info, opts.audio) is not None
    ref_args, ref_dur = reference_args(src, duration, video_filter(info, opts.max_fps))
    result: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="vidsqueeze-est-", ignore_cleanup_errors=True) as tmp:
        ref = Path(tmp) / "reference.mkv"
        run([*ref_args, "-an", "-c:v", "ffv1", str(ref)], cancel=cancel)
        for key in codecs:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            out = Path(tmp) / f"est-{key}{CODECS[key].ext}"
            run(["-i", str(ref), *codec_args(opts, key, hdr=info.hdr), "-an", str(out)], cancel=cancel)
            video_bytes = math.ceil(out.stat().st_size * duration / ref_dur)
            audio_bytes = int(duration * AUDIO_BPS[CODECS[key].audio_codec] / 8) if audio else 0
            result[key] = video_bytes + audio_bytes
            out.unlink(missing_ok=True)
    return result

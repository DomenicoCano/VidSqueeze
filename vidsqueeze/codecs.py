"""Профили кодеков (значения по умолчанию взяты из pixel-point/skills, compress-video)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ffmpeg import available_encoders


@dataclass(frozen=True)
class Codec:
    key: str
    label: str
    ext: str
    default_crf: int
    audio_codec: str


CODECS = {
    "vp9": Codec("vp9", "VP9", ".webm", 42, "libopus"),
    "h265": Codec("h265", "H.265", ".mp4", 30, "aac"),
    "av1": Codec("av1", "AV1", ".webm", 42, "libopus"),
}

DEFAULT_CODECS = ("vp9", "h265")

# Кодеры видеокарт. VP9 на видеокартах практически не кодируется — он всегда считается процессором.
VENDORS = {"nvidia": "NVIDIA (NVENC)", "intel": "Intel (Quick Sync)", "amd": "AMD (AMF)"}
HW_ENCODERS = {
    ("h265", "nvidia"): "hevc_nvenc", ("h265", "intel"): "hevc_qsv", ("h265", "amd"): "hevc_amf",
    ("av1", "nvidia"): "av1_nvenc", ("av1", "intel"): "av1_qsv", ("av1", "amd"): "av1_amf",
}


def _hw_args(key: str, vendor: str, crf: int, fast: bool, ten_bit: bool) -> list[str]:
    enc = HW_ENCODERS[(key, vendor)]
    args = ["-c:v", enc]
    if vendor == "nvidia":
        args += ["-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-preset", "p5" if fast else "p7",
                 "-tune", "hq", "-spatial-aq", "1"]
        if ten_bit and key == "h265":
            args += ["-profile:v", "main10"]
        args += ["-pix_fmt", "p010le" if ten_bit else "yuv420p"]
    elif vendor == "intel":
        args += ["-global_quality", str(crf), "-preset", "medium" if fast else "veryslow"]
        if ten_bit:  # без -pix_fmt ffmpeg сам выберет nv12
            args += ["-pix_fmt", "p010le"]
    else:  # amd
        args += ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-quality", "speed" if fast else "quality"]
        if ten_bit:
            args += ["-pix_fmt", "p010le"]
    if key == "h265":
        args += ["-tag:v", "hvc1", "-movflags", "+faststart"]
    return args


def video_args(
    key: str,
    crf: int,
    fast: bool = False,
    ten_bit: bool = False,
    threads: int = 0,
    hw: Optional[str] = None,
) -> list[str]:
    """Аргументы видеокодера. hw — производитель видеокарты (или None: считает процессор).
    threads=0 — все потоки."""
    if hw and (key, hw) in HW_ENCODERS:
        return _hw_args(key, hw, crf, fast, ten_bit)

    pix_fmt = "yuv420p10le" if ten_bit else "yuv420p"
    thr = ["-threads", str(threads)] if threads > 0 else []
    if key == "vp9":
        deadline = ["-deadline", "good", "-cpu-used", "4"] if fast else ["-deadline", "best"]
        return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf), "-row-mt", "1", "-tile-columns", "2",
                *deadline, *thr, "-pix_fmt", pix_fmt]
    if key == "h265":
        preset = "medium" if fast else "veryslow"
        # у libx265 «-threads» задаёт число параллельно кодируемых кадров и нагрузку не ограничивает
        # (проверено: с -threads 2 x265 всё равно занимает ~7 ядер); размер пула задаёт только pools
        pools = ["-x265-params", f"pools={threads}"] if threads > 0 else []
        return ["-c:v", "libx265", "-crf", str(crf), "-preset", preset, "-tag:v", "hvc1",
                "-movflags", "+faststart", *pools, "-pix_fmt", pix_fmt]
    if key == "av1":
        if "libsvtav1" in available_encoders():
            return ["-c:v", "libsvtav1", "-b:v", "0", "-crf", str(crf),
                    "-preset", "8" if fast else "6", *thr, "-pix_fmt", pix_fmt]
        if "libaom-av1" in available_encoders():
            return ["-c:v", "libaom-av1", "-b:v", "0", "-crf", str(crf), "-row-mt", "1",
                    "-cpu-used", "8" if fast else "6", *thr, "-pix_fmt", pix_fmt]
        raise ValueError("В этой сборке FFmpeg нет AV1-кодера (libsvtav1 / libaom-av1).")
    raise ValueError(f"Неизвестный кодек: {key}")

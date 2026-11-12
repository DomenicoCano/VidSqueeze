"""Сравнение кодеков и CRF на коротких фрагментах (SSIM + оценка итогового размера)."""
from __future__ import annotations

import math
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .codecs import CODECS
from .compress import Options, codec_args, video_filter
from .ffmpeg import Cancelled, probe, run, run_capture

CLIP_SECONDS = 1.0
CLIP_POINTS = (0.25, 0.50, 0.75)
DEFAULT_CANDIDATES = [("vp9", 36), ("vp9", 42), ("vp9", 48), ("h265", 24), ("h265", 30), ("h265", 36)]


@dataclass
class Candidate:
    codec: str
    crf: int
    preview_bytes: int = 0
    ssim: float = 0.0
    estimated: int = 0
    pareto: bool = False
    recommended: bool = False


def parse_candidates(specs: list[str]) -> list[tuple[str, int]]:
    """'vp9:36,42,48' 'h265:30' -> [('vp9',36),('vp9',42),('vp9',48),('h265',30)]"""
    out = []
    for spec in specs:
        name, _, crfs = spec.partition(":")
        name = name.lower().replace(".", "").replace("hevc", "h265")
        if name not in CODECS or not crfs:
            raise ValueError(f"Неверный кандидат '{spec}'. Формат: vp9:36,42,48")
        out += [(name, int(c)) for c in crfs.split(",")]
    if not 2 <= len(out) <= 8:
        raise ValueError("Нужно от 2 до 8 пар кодек/CRF.")
    return out


def mark_pareto(items: list[Candidate]) -> None:
    best = -1.0
    for c in sorted(items, key=lambda c: (c.estimated, -c.ssim)):
        if c.ssim > best:
            c.pareto = True
            best = c.ssim


def recommend(items: list[Candidate], target_ssim: float) -> Optional[Candidate]:
    front = [c for c in items if c.pareto]
    ok = [c for c in front if c.ssim >= target_ssim]
    pick = min(ok, key=lambda c: c.estimated) if ok else max(front, key=lambda c: c.ssim, default=None)
    if pick:
        pick.recommended = True
    return pick


def reference_args(src: Path, duration: float, vf: Optional[str]) -> tuple[list[str], float]:
    """Аргументы ffmpeg (входы + фильтр) для эталона из трёх 1-секундных фрагментов и его длительность.

    Каждый фрагмент — отдельный вход с `-ss` перед `-i`: ffmpeg прыгает к ближайшему ключевому кадру
    и не декодирует всё до нужного места. С фильтром trim эталон для двухчасового фильма собирался
    больше минуты, так как декодировалось всё видео от начала до 75%."""
    if duration <= CLIP_SECONDS * len(CLIP_POINTS):
        chain = "[0:v]" + (vf + "," if vf else "") + "setpts=PTS-STARTPTS[out]"
        return ["-i", str(src), "-filter_complex", chain, "-map", "[out]"], duration
    args, chains, labels = [], [], ""
    for i, p in enumerate(CLIP_POINTS):
        start = max(0.0, min(duration * p, duration - CLIP_SECONDS))
        args += ["-ss", f"{start:.3f}", "-t", f"{CLIP_SECONDS}", "-i", str(src)]
        chains.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
        labels += f"[v{i}]"
    tail = f"{labels}concat=n={len(CLIP_POINTS)}:v=1:a=0" + (f",{vf}" if vf else "") + "[out]"
    return args + ["-filter_complex", ";".join(chains + [tail]), "-map", "[out]"], CLIP_SECONDS * len(CLIP_POINTS)


def compare_file(
    src: Path,
    candidates: list[tuple[str, int]],
    opts: Options,
    target_ssim: float = 0.97,
    log: Callable[[str], None] = print,
    cancel: Optional[threading.Event] = None,
) -> tuple[list[Candidate], float]:
    info = probe(src)
    ref_args, ref_dur = reference_args(src, info.duration, video_filter(info, opts.max_fps))
    items = [Candidate(c, crf) for c, crf in candidates]
    with tempfile.TemporaryDirectory(prefix="vidsqueeze-", ignore_cleanup_errors=True) as tmp:
        tmp_dir = Path(tmp)
        ref = tmp_dir / "reference.mkv"
        log("Готовлю эталон (FFV1, без потерь)…")
        run([*ref_args, "-an", "-c:v", "ffv1", str(ref)], cancel=cancel)
        for n, cand in enumerate(items, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            log(f"[{n}/{len(items)}] {CODECS[cand.codec].label} CRF {cand.crf}…")
            out = tmp_dir / f"c{n}{CODECS[cand.codec].ext}"
            run(["-i", str(ref), *codec_args(opts, cand.codec, cand.crf, hdr=info.hdr), "-an", str(out)], cancel=cancel)
            cand.preview_bytes = out.stat().st_size
            err = run_capture(["-i", str(out), "-i", str(ref), "-lavfi", "ssim", "-f", "null", "-"])
            m = re.search(r"SSIM.*?All:([\d.]+)", err, re.S)
            cand.ssim = float(m[1]) if m else 0.0
            cand.estimated = math.ceil(cand.preview_bytes * info.duration / ref_dur)
            out.unlink(missing_ok=True)
    mark_pareto(items)
    recommend(items, target_ssim)
    return items, info.duration

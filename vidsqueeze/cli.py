"""Командная строка: vidsqueeze compress | compare | info."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .codecs import CODECS, DEFAULT_CODECS
from .compare import DEFAULT_CANDIDATES, compare_file, parse_candidates
from .compress import Options, compress_file, find_videos
from .ffmpeg import FFmpegError, ffmpeg_path, probe


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def progress_printer(label: str):
    def cb(key: str, frac: float) -> None:
        sys.stdout.write(f"\r  {CODECS[key].label:6} {frac * 100:5.1f}%")
        sys.stdout.flush()
    return cb


def hw_options(a: argparse.Namespace) -> dict:
    """Потоки процессора и видеокарта из аргументов командной строки."""
    from .hardware import detect_gpu_encoders, threads_for_percent
    out: dict = {"threads": threads_for_percent(a.cpu) if a.cpu else 0}
    if a.gpu:
        keys = detect_gpu_encoders().get(a.gpu)
        if keys:
            out.update(hw=a.gpu, hw_codecs=tuple(keys))
        else:
            print(f"Видеокарта {a.gpu} недоступна — считаю процессором.")
    return out


def cmd_compress(a: argparse.Namespace) -> int:
    files = find_videos([Path(p) for p in a.inputs], a.recursive)
    if not files:
        print("Видеофайлы не найдены.")
        return 1
    crf = {}
    for key in CODECS:
        v = getattr(a, f"crf_{key}")
        if v is not None:
            crf[key] = v
    opts = Options(
        codecs=tuple(a.codec or DEFAULT_CODECS), crf=crf, audio=a.audio, max_fps=a.max_fps,
        ten_bit=a.ten_bit, fast=a.fast, out_dir=Path(a.output) if a.output else None,
        **hw_options(a),
    )
    failed = 0
    total_in = total_out = 0
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f}")
        try:
            results = compress_file(f, opts, on_progress=progress_printer(f.name))
        except (FFmpegError, ValueError, OSError) as e:
            print(f"\n  ОШИБКА: {e}")
            failed += 1
            continue
        src_size = f.stat().st_size
        sys.stdout.write("\r" + " " * 30 + "\r")
        for r in results:
            saved = (1 - r.size / src_size) * 100 if src_size else 0
            note = f"экономия {saved:.0f}%" if saved >= 0 else f"БОЛЬШЕ исходника на {-saved:.0f}%"
            print(f"  {CODECS[r.codec].label:6} {human(r.size):>10}  ({note})  {r.path}")
            if a.show_commands:
                print("    " + " ".join(f'"{x}"' if " " in x else x for x in r.command))
        total_in += src_size
        total_out += min(r.size for r in results)
    if len(files) > 1 and total_in:
        print(f"\nИтого: {human(total_in)} -> {human(total_out)} (лучший кодек на файл, "
              f"экономия {(1 - total_out / total_in) * 100:.0f}%)")
    if failed:
        print(f"С ошибкой: {failed} из {len(files)}")
    return 1 if failed else 0


def cmd_compare(a: argparse.Namespace) -> int:
    src = Path(a.file)
    if not src.is_file():
        print(f"Файл не найден: {src}")
        return 1
    cands = parse_candidates(a.candidates) if a.candidates else DEFAULT_CANDIDATES
    items, duration = compare_file(src, cands, Options(max_fps=a.max_fps, fast=a.fast, **hw_options(a)), a.target_ssim)
    print(f"\nИсходник: {human(src.stat().st_size)}, {duration:.1f} с. "
          f"Оценка размера только видео, без звука.\n")
    print(f"{'Формат':8}{'CRF':>5}{'Оценка размера':>17}{'SSIM':>9}   ")
    for c in sorted(items, key=lambda c: c.estimated):
        mark = "<- рекомендация" if c.recommended else ("(парето)" if c.pareto else "")
        print(f"{CODECS[c.codec].label:8}{c.crf:>5}{human(c.estimated):>17}{c.ssim:>9.4f}   {mark}")
    rec = next((c for c in items if c.recommended), None)
    if rec:
        print(f"\nРекомендую: {CODECS[rec.codec].label} CRF {rec.crf} "
              f"(цель SSIM ≥ {a.target_ssim}). Применить: "
              f"vidsqueeze compress \"{src}\" -c {rec.codec} --crf-{rec.codec} {rec.crf}")
    return 0


def cmd_info(a: argparse.Namespace) -> int:
    i = probe(Path(a.file))
    print(f"FFmpeg: {ffmpeg_path()}")
    print(f"{i.path.name}: {i.vcodec} {i.width}x{i.height} {i.fps} fps {i.pix_fmt}, "
          f"{i.duration:.1f} с, аудиопотоков: {i.audio_streams}, {human(i.size)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vidsqueeze", description="Сжатие видео через FFmpeg (VP9 / H.265 / AV1).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress", help="сжать файлы и/или папки")
    c.add_argument("inputs", nargs="+", help="файлы или папки")
    c.add_argument("-c", "--codec", action="append", choices=list(CODECS),
                   help="кодек (можно несколько раз). По умолчанию: vp9 и h265")
    for key, codec in CODECS.items():
        c.add_argument(f"--crf-{key}", type=int, metavar="N", help=f"CRF для {codec.label} (по умолчанию {codec.default_crf})")
    c.add_argument("--audio", choices=["auto", "keep", "drop"], default="auto",
                   help="auto: убрать, если звук отсутствует или тише −50 dBFS")
    c.add_argument("--max-fps", type=float, default=30.0, help="ограничить частоту кадров (0 — не ограничивать)")
    c.add_argument("--ten-bit", action="store_true", help="10-бит вместо 8-бит")
    c.add_argument("--fast", action="store_true", help="быстрее, но файлы чуть больше")
    c.add_argument("-r", "--recursive", action="store_true", help="заходить в подпапки")
    c.add_argument("-o", "--output", help="папка для результата (по умолчанию рядом с исходником)")
    c.add_argument("--cpu", type=int, metavar="%", help="доля потоков процессора, 10-100 (по умолчанию все)")
    c.add_argument("--gpu", choices=["nvidia", "intel", "amd"], help="кодировать H.265/AV1 на видеокарте (быстрее, но качество при том же размере хуже)")
    c.add_argument("--show-commands", action="store_true", help="показать команды ffmpeg")
    c.set_defaults(func=cmd_compress)

    m = sub.add_parser("compare", help="сравнить кодеки/CRF по качеству и размеру")
    m.add_argument("file")
    m.add_argument("--candidates", nargs="+", metavar="КОДЕК:CRF,...",
                   help="например: vp9:36,42,48 h265:24,30,36 (2–8 пар)")
    m.add_argument("--target-ssim", type=float, default=0.97, help="порог качества для рекомендации")
    m.add_argument("--max-fps", type=float, default=30.0)
    m.add_argument("--fast", action="store_true")
    m.add_argument("--cpu", type=int, metavar="%")
    m.add_argument("--gpu", choices=["nvidia", "intel", "amd"])
    m.set_defaults(func=cmd_compare)

    i = sub.add_parser("info", help="показать параметры файла и путь к FFmpeg")
    i.add_argument("file")
    i.set_defaults(func=cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FFmpegError, ValueError) as e:
        print(f"Ошибка: {e}")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130

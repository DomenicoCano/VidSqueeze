# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

VidSqueeze (пакет `vidsqueeze`) — Windows-программа для сжатия видео через FFmpeg (VP9 / H.265 / AV1) с окном на tkinter, пакетной обработкой, предварительной оценкой размера, сравнением качества (SSIM), настройкой нагрузки CPU и кодированием на видеокарте. Рецепты кодеков, CRF, логика звука и сравнения перенесены из скилла `compress-video` репозитория pixel-point/skills (MIT). Интерфейс и сообщения — на русском; пользователь общается по-русски. Основной сценарий — для самого пользователя и друга, поэтому приоритет: «положил видео, нажал кнопку».

## Команды

Окружение: `.venv` на Python 3.10.0 (`.\.venv\Scripts\python`). Тестов и линтера в проекте нет.

```powershell
.\.venv\Scripts\python -m vidsqueeze.gui            # окно (то же, что VidSqueeze.pyw)
.\.venv\Scripts\python -m vidsqueeze compress f.mp4  # CLI: compress | compare | info
.\.venv\Scripts\python -m PyInstaller --noconfirm --clean --windowed --name VidSqueeze `
  --icon "$PWDidsqueezessets\icon.ico" --add-data "$PWDidsqueezessets;vidsqueezessets" `
  --collect-all imageio_ffmpeg --collect-all tkinterdnd2 --distpath dist --workpath build --specpath build VidSqueeze.pyw
```

Проверка вручную: сгенерировать тестовое видео встроенным ffmpeg (`testsrc2` через `-f lavfi`) и прогнать `compress`/`compare` с `--fast`; GUI проверялся скриптом, который создаёт `App`, вызывает `_add_paths`/`_start` и опрашивает состояние через `after`.

### Грабли окружения

- **pip в этом Python не ходит в интернет** (ошибка SSL в 3.10.0), а curl работает. Зависимости ставились так: скачать wheel через `curl.exe` (URL из `https://pypi.org/pypi/<pkg>/json`), затем `pip install --no-deps <файл.whl>`. Имя файла должно оставаться настоящим именем wheel.
- Системного `ffmpeg`/`ffprobe` нет. Используется бинарник из `imageio-ffmpeg` (ffmpeg 9.x): есть libx265, libvpx-vp9, libaom-av1, libopus, aac, фильтры ssim/astats; **нет** libsvtav1 и **нет ffprobe**.
- Запускайте скрипты проверки с `python -I` (изолированный режим); при печати кириллицы в консоль cp1251 делайте `sys.stdout.reconfigure(encoding="utf-8")`.
- У ttk-виджетов `widget["state"]` возвращает не `str` — сравнивайте через `str(...)`. Шрифты у `ttk.Label` в теме vista не применяются — для крупного текста используйте `tk.Label`.

## Архитектура

Ядро не зависит от интерфейса; CLI и GUI — два тонких слоя над одними и теми же функциями.

- [vidsqueeze/ffmpeg.py](vidsqueeze/ffmpeg.py) — поиск ffmpeg (системный, иначе `imageio_ffmpeg`), `probe()` (разбор stderr `ffmpeg -i`, т.к. ffprobe нет), `run()` (запуск с `-progress pipe:1`, колбэк прогресса, отмена через `threading.Event` → `Cancelled`), `set_low_priority()` (глобальный флаг BELOW_NORMAL для всех запусков ffmpeg), `run_capture()` для astats/ssim.
- [vidsqueeze/codecs.py](vidsqueeze/codecs.py) — профили кодеков и `video_args(key, crf, fast, ten_bit, threads, hw)`. AV1 использует libsvtav1, если он есть, иначе libaom-av1 (во встроенной сборке — libaom, медленно). `hw` = nvidia/intel/amd переключает на hevc_/av1_ nvenc/qsv/amf (VP9 всегда на CPU); `threads` > 0 добавляет `-threads` (0 = все).
- [vidsqueeze/hardware.py](vidsqueeze/hardware.py) — `detect_gpu_encoders()` проверяет кодеры видеокарт пробным кодированием 8 кадров (по списку `-encoders` судить нельзя: в сборке есть всё, а работает только то, что есть в железе), `threads_for_percent()`. На машине автора: RTX 3070 → только h265 (AV1-кодера в Ampere нет), Intel QSV нет.
- [vidsqueeze/estimate.py](vidsqueeze/estimate.py) — оценка итогового размера до сжатия: три 1-секундных фрагмента реальными настройками → пересчёт на длительность + оценка звука (opus 96 кбит/с, aac 128). Реальная погрешность на тестах 3–11%.
- [vidsqueeze/compress.py](vidsqueeze/compress.py) — `Options` (в т.ч. `threads`, `hw`, `hw_codecs`), `codec_args()` (единая точка сборки аргументов кодера для сжатия, сравнения и оценки), `compress_file()`, `find_videos()`. Фильтры (`video_filter`): ограничение fps делителем исходной частоты (60→30, 50→25) и округление нечётных размеров. Звук в режиме auto удаляется, если пик по `astats` ≤ −50 dBFS. Выходы называются `<имя>-vp9.webm` / `-h265.mp4` / `-av1.webm`, существующие файлы не перезаписываются (`-n` + `unique_path`); `find_videos` пропускает файлы с такими суффиксами, чтобы не пережимать результат. При ошибке/отмене недописанный файл удаляется.
- [vidsqueeze/theme.py](vidsqueeze/theme.py) — три темы (`light` «Светлая», `graphite` «Графит», `oled` чисто чёрная) на ttk-теме `clam` с ручной настройкой всех виджетов, цвет заголовка окна через DWM.
- [vidsqueeze/compare.py](vidsqueeze/compare.py) — `compare_file(src, candidates, opts, target_ssim, ...)`: три 1-секундных фрагмента (25/50/75%) → эталон FFV1 → кандидаты (2–8 пар кодек/CRF) → SSIM → оценка размера `preview_bytes * duration / ref_duration` (только видео) → фронт Парето → рекомендация (минимальный размер при SSIM ≥ порога).
- [vidsqueeze/cli.py](vidsqueeze/cli.py) — argparse-подкоманды `compress`, `compare`, `info`; здесь же `human()` (формат размеров), его импортирует GUI.
- [vidsqueeze/gui.py](vidsqueeze/gui.py) — окно `App` и диалог `CompareWindow`. Тяжёлая работа (сжатие, оценка размера, определение GPU) идёт в daemon-потоках, которые кладут события-кортежи (`start`/`progress`/`status`/`done`/`output`/`log`/`finished`/`gpu`/`est`/`est_err`/`est_done`) в `queue.Queue`; главный поток разбирает их в `_poll()` через `after`. Tk-виджеты из потоков не трогать. Перетаскивание файлов — через `tkinterdnd2` (необязательная зависимость, есть запасной вариант без неё). Настройки хранятся в `%APPDATA%\vidsqueeze\settings.json`. Пресеты качества (`PRESETS`) задают CRF по кодекам; ручное изменение CRF переключает пресет на «Свой вариант». Оценки размера кэшируются по ключу (файл, mtime, кодек, CRF, GPU, fast, 10-бит, fps, звук), поэтому устаревших оценок не бывает; изменение любой настройки запускает пересчёт с задержкой 0,9 с (`_schedule_estimate`), старый расчёт отменяется. Во время сжатия оценка не идёт.
- [VidSqueeze.pyw](VidSqueeze.pyw) — точка входа без консоли и для PyInstaller.
- [vidsqueeze/assets/](vidsqueeze/assets/) — значок программы (`icon.ico` 16–256 px, `icon.png`). Пути в `--icon`/`--add-data` абсолютные, потому что `--specpath build` меняет базу относительных путей. Окно ставит значок через `iconbitmap(default=...)`, `main()` задаёт AppUserModelID, чтобы на панели задач не было значка Python.
- Релизы: `vX.Y.Z` на GitHub с архивом `VidSqueeze-win64.zip` (внутри папка `VidSqueeze`); README ссылается на `releases/latest/download/VidSqueeze-win64.zip`, поэтому имя архива менять нельзя. Версия — в `vidsqueeze/__init__.py` и `pyproject.toml`. CLI `gh` не установлен.

## Дизайн GUI

Минималистичный «лофт»: бетонно-тёплые нейтральные цвета, медный акцент, плоские кнопки, тонкие рамки, заголовки секций капсом. Две колонки: слева список видео (колонки «Размер» / «После сжатия» / «Статус»), итог оценки, «Куда сохранить», кнопка «Сжать», прогресс, журнал; справа прокручиваемые карточки настроек (Качество, Скорость, Звук и кадры, Оценка размера). Переключатель темы — сегментированная кнопка в шапке; выбор сохраняется. Новые настройки обязаны попадать в карточки без потери существующих («без потери в настройках и подробной информации» — требование пользователя). Цвета берите из `self.pal`, а не хардкодьте; новые обычные `tk`-виджеты перекрашивайте в `App._recolor()`, окна верхнего уровня регистрируйте в `app.listeners`. Включается DPI-awareness в `gui.main()`. Оформление проверяйте скриншотом: запустить окно с `SetProcessDpiAwareness(1)` и снять `CopyFromScreen` по `winfo_rootx/rooty` (PowerShell тоже должен быть DPI-aware).

## Видеокарта: что известно

Аппаратные кодеры быстрее, но при том же CRF дают заметно больший файл (на тесте RTX 3070: ~650 КБ против ~390 КБ у x265), а по отзывам форумов при равном размере уступают x265 в качестве; утверждения про «квадратики» в источниках не подтверждены — в UI формулировка осторожная. Параметры качества различаются (`-cq` у NVENC, `-global_quality` у QSV, `-qp_i/-qp_p` у AMF); для av1_qsv `-global_quality`, по отзывам, ненадёжен — работоспособность проверяет пробное кодирование. Для QSV нельзя передавать `-pix_fmt yuv420p` (кодеру нужен nv12/p010le).

## Дальше по плану

Иконка, установщик, подпись `.exe`; перенос «аудита» и «сканирования сайта» из исходного скилла пока не делался. «Без потери качества» в UI означает визуально почти неотличимо (высокий SSIM), а не lossless.

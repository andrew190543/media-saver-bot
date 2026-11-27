# Design Doc (v7): Устранение логических ошибок и повышение надёжности Media Saver Bot

**Статус:** Final Candidate (v7 — устранены все замечания Goldfish v6)  
**Автор:** Elephant Session  
**Область:** [bot.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/bot.py), [config.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/config.py), [handlers/media.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/handlers/media.py), [services/downloader.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/services/downloader.py), [services/database.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/services/database.py), [middlewares/throttling.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/middlewares/throttling.py), [keyboards/inline.py](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/keyboards/inline.py), [requirements.txt](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/requirements.txt), [Dockerfile](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/Dockerfile), [docker-compose.yml](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/docker-compose.yml), [.env.example](file:///c:/Users/VELoz/Desktop/opencode/media-saver-bot/.env.example)

---

## 1. Контекст, цели и не-цели

### 1.1 Контекст
`Media Saver Bot` — Telegram-бот на `aiogram 3`, принимающий ссылки (YouTube, Instagram, Pinterest, TikTok, X/Twitter, SoundCloud) и доставляющий видео, аудио (MP3), обложки/пины и галереи через `yt-dlp`, `gallery-dl`, `ffmpeg`/`ffprobe` и SQLite (`aiosqlite`).

Аудит кода выявил критические дефекты логики, безопасности и эксплуатации:
1. При `USE_LOCAL_BOT_API=True` бот направляет трафик к `LOCAL_BOT_API_URL` через `TELEGRAM_PROXY_URL` и использует дефолтный 60-секундный таймаут `AiohttpSession`.
2. `get_proxy_for_url` использует подстрочный поиск (`x.com` ложно совпадает с `dropbox.com`, `yandex.com`, `netflix.com`).
3. `extract_metadata` глотает любые ошибки и молча падает в `is_gallery=True`, показывая фейковое меню галереи.
4. `handle_link_message` удаляет сообщение пользователя и не ловит `TelegramForbiddenError`.
5. `action="cover"` («Обложка / Пин») ресайзит картинку до `320x320`.
6. Логика размеров и FFmpeg: хардкод `48 МБ` при Local Bot API, оценка 4K-формата скрывает `720p`, `max(duration, 10)` при `duration==0` даёт файлы 500+ МБ, порог `250_000` bps даёт >120 МБ на длинных видео уже *после* кодирования, `scale='min(1280,iw)':-2` падает на нечётной ширине, нет `-pix_fmt yuv420p` и `-map`, сохраняется `.webm`, Pass 1 пишет `-f mp4 os.devnull`, игнорируются `returncode`.
7. Таймауты `asyncio.wait_for` оставляют живые потоки и процессы в удаляемой `temp_dir`.
8. Галереи: одиночный `.mp4` идёт в `answer_photo`, `.webp` не конвертируется, фото >10 МБ или с `ratio > 20:1` падают, >10 элементов обрезаются, галереи не кэшируются, не учитываются суммарный 50 МБ лимит `sendMediaGroup`, 1-элементные чанки и `FloodWait`.
9. Отсутствуют: защита от SSRF, семафор на метаданные и per-user квоты, single-flight и callback-троттлинг, `PySocks` для `gallery-dl`, изоляция `:ro` `cookies.txt`, защита `msg.delete()` на старых сообщениях (>48ч), SQLite WAL с rollback, маскировка `BOT_TOKEN`/паролей прокси и graceful shutdown при рестарте Watchtower.

### 1.2 Цели
- Устранить все перечисленные баги и гонки при единой кросс-платформенной архитектуре (Docker Linux/VPS + локальный запуск на Windows).
- Сделать поведение детерминированным, защищённым от SSRF/DoS и готовым к однопроходной реализации с юнит-тестами.

### 1.3 Не-цели
- Работа в группах, супергруппах и каналах.
- Ограничение доступа по `ALLOWED_USER_IDS`.
- Скачивание мульти-видео плейлистов и профилей/досок целиком.
- Автоматическая нарезка длинных видео на части.

---

## 2. Утверждённые требования

- **R1 (Docker + Windows и разделение прокси):** Штатная работа в Docker Compose (Linux/VPS) и локально на Windows (`python bot.py`). `TELEGRAM_PROXY_URL` используется **только** для `https://api.telegram.org`. При `USE_LOCAL_BOT_API=True` подключение к `LOCAL_BOT_API_URL` идёт с `proxy=None`.
- **R2 (Приватные чаты, открыто всем):** Обработка сообщений и callback-кнопок только в личных чатах (`ChatType.PRIVATE`), открыто всем пользователям с защитой квотами.
- **R3 (Неудаление сообщений пользователя):** Бот **никогда** не удаляет исходное сообщение пользователя со ссылкой.
- **R4 (Валидация, отказ на ошибки/плейлисты/профили, галереи >10):**
  - Невалидные, внутренние (SSRF) и неподдерживаемые ссылки отклоняются сразу на этапе анализа без ложного fallback в галерею.
  - Мульти-видео плейлисты, каналы и профили/доски отклоняются явно; ссылка на конкретное видео с `&list=...` обрабатывает это одно видео.
  - Галереи >10 элементов отправляются последовательными чанками (≤10 файлов и в пределах лимита веса POST-запроса, при `len(chunk) == 1` — одиночным `sendPhoto`/`sendVideo`), до `MAX_GALLERY_ITEMS = 30`.
- **R5 (Размеры и сжатие):**
  - Если размер видео ≤ активного лимита (`48 МБ` для Cloud или `1950 МБ` для Local), видео **не сжимается** (включая `1080p` / `Лучшее` / `720p` и случай, когда при выборе «Сжать» скачанный `720p` уже ≤ лимита).
  - Если скачанный файл > активного лимита — сжимается до `active_limit_mb - 1`. Если видео физически не влезает при минимально приемлемом качестве — отказ происходит **до** запуска FFmpeg.
- **R6 (Полноразмерная обложка / Пин):** Кнопка «🖼 Скачать обложку / Пин» отдаёт изображение в полном исходном разрешении (приводя к JPEG и уменьшая лишь при нарушении лимитов `sendPhoto`), а миниатюра `320x320` используется только как `thumbnail` для видео/аудио.
- **R7 (Учёт `downloads_count`):** `downloads_count` инкрементируется при каждой успешной не-кэшированной доставке для всех действий (`best`, `720p`, `480p`, `compress`, `mp3`, `gallery`, `cover`).

---

## 3. Critical User Journeys (CUJ)

* **CUJ 1: Видео (1080p/720p) в пределах активного лимита** — сообщение пользователя сохраняется; сжатие не вызывается; `ffprobe` определяет точные `width`/`height`/`duration`; видео отправляется с 320px миниатюрой, кэшируется под `f"{api_scope}:{action}"` (`sent.video or sent.animation`), инкрементируется `downloads_count`.
* **CUJ 2: Аудио-ссылка (SoundCloud)** — `is_audio_only=True`; пользователю показываются только `🎵 Аудио (MP3)` и (при наличии) `🖼 Скачать обложку / Пин`.
* **CUJ 3: Крупное видео (> лимита) и отказ на сверхдлинные ролики** — `720p` доступен, если влезает в лимит; динамическое предупреждение с `{limit_mb} МБ`; при необходимости сжатия `ffprobe` проверяет длительность, при `video_bps < 100_000` отказ до старта FFmpeg, иначе 2-pass сжатие в `.mp4`; если скачанный `720p` при `compress` уже ≤ лимита — отправляется без сжатия.
* **CUJ 4: Невалидный URL, SSRF, плейлист или профиль/доска** — сообщение пользователя остаётся, статус «Анализирую...» сразу меняется на санитизированную ошибку, в `url_map` запись не создаётся.
* **CUJ 5: Галереи >10 элементов, смешанное медиа и обложки** — `gallery-dl` валидирует ссылку через `-J`, скачивает до 30 элементов; `.webp` (включая animated через `Pillow` + `ffconcat`) и негабаритные фото нормализуются (инфографика `ratio > 20:1` уходит как `document` отдельно от фото/видео); альбомы делятся по ≤10 элементов и ≤45 МБ (Cloud) / ≤1900 МБ (Local) (чанк из 1 элемента уходит как `answer_video`/`answer_photo`, допуская одиночное сжатое видео до 47 МБ) с `TelegramRetryAfter`; `cover` отдаёт полный размер с автообновлением протухшего `thumbnail_url`.
* **CUJ 6: Двойные клики, смена Local API, старые сообщения (>48ч) и рестарт Watchtower** — повторный клик отклоняется всплывающим уведомлением; параллельный запрос с другого сообщения ждёт `InflightJob` с динамическим таймаутом до `download_semaphore` (перехватывая лидерство только если лидер завершился без кэша и без `error_msg`, и не плодя дублей поверх живого лидера); кэш изолирован по `api_scope`; ошибка `msg.delete()` на >48ч не затирает результат; при `SIGTERM` активные задачи корректно отменяются и уведомляют пользователя **до** закрытия `bot.session` и `db`.

---

## 4. Детальный дизайн по модулям

### 4.1 `config.py` — Настройки, прокси, SSRF и санитизация

1. **Кросс-платформенные и новые параметры `Settings`:**
   - `TEMP_DIR: str = Field(default_factory=lambda: str(Path(tempfile.gettempdir()) / "media_saver_bot"))`
   - `TELEGRAM_PROXY_URL: str = ""`
   - `TELEGRAM_REQUEST_TIMEOUT: float = 300.0` (и `LOCAL_TELEGRAM_REQUEST_TIMEOUT: float = 1800.0` при `USE_LOCAL_BOT_API=True`)
   - `METADATA_TIMEOUT: int = 35`
   - `LOCAL_DOWNLOAD_TIMEOUT: int = 1800` (для скачивания до 1950 МБ через Local Bot API)
   - `COMPRESS_TIMEOUT: int = 300` (и `LOCAL_COMPRESS_TIMEOUT: int = 1800`)
   - `SHUTDOWN_TIMEOUT: float = 15.0` (строго меньше `WATCHTOWER_TIMEOUT: 30s` и `stop_grace_period: 25s`)
   - `MAX_CONCURRENT_METADATA: int = 4`
   - `MAX_ACTIVE_JOBS_PER_USER: int = 2`
   - `MAX_RAW_DOWNLOAD_MB: int = 2500`
   - `CALLBACK_RATE_LIMIT_SECONDS: float = 1.5`
   - `MAX_GALLERY_ITEMS: int = 30`
   - `ALLOW_GENERIC_EXTRACTOR: bool = False`
   - `@property api_scope -> str`: `"local" if self.USE_LOCAL_BOT_API else "cloud"`
   - `@property effective_max_filesize_mb -> int`: `self.LOCAL_MAX_FILESIZE_MB if self.USE_LOCAL_BOT_API else self.MAX_FILESIZE_MB`
   - `@property effective_download_timeout -> int`: `self.LOCAL_DOWNLOAD_TIMEOUT if self.USE_LOCAL_BOT_API else self.DOWNLOAD_TIMEOUT`

2. **Прокси-маршрутизация и нормализация схемы:**
   - `get_proxy_for_url(url)`: парсит `hostname = (urlparse(url).hostname or "").lower().rstrip(".")`, матчит `hostname == domain or hostname.endswith(f".{domain}")` (`*` поддерживается только при явном указании).
   - `normalize_aiohttp_proxy(proxy_url: str | None) -> str | None`: заменяет `socks5h://` -> `socks5://` и `socks4a://` -> `socks4://` (для `bot.py` и `downloader.py`).

3. **SSRF-валидатор (`async def validate_external_url(url: str, uses_proxy: bool) -> str`):**
   - Очищает пробелы, извлекает `hostname = (parsed.hostname or "").lower().rstrip(".")` **до** проверок доменов/single-label.
   - Запрещает `\`, управляющие символы, `userinfo` (`user:pass@`), IPv6 Zone ID (`%`), схемы кроме `http`/`https`, невалидные порты.
   - Запрещает локальные домены и туннели (`localhost`, `*.localhost`, `*.local`, `*.internal`, `*.localdomain`, `localtest.me`, `*.nip.io`, `*.sslip.io`, `*.xip.io`) и любые single-label хосты без точки (`telegram-bot-api`, `redis`).
   - Отвергает нестандартные IPv4-литералы (`0177.0.0.1`, `0x7f.0.0.1`, `127.1`, `2130706433`).
   - `_is_public_ip(ip_obj)`: разворачивает `ip_obj.ipv4_mapped` (обязательно для Python 3.11!), отвергает NAT64 `64:ff9b::/96` и `64:ff9b:1::/48`, 6to4 `2002::/16`, Teredo `2001::/32`, требует `ip_obj.is_global and not (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified)`.
   - Для IP-литерала проверяет `_is_public_ip` всегда (включая `uses_proxy=True`).
   - Для домена при `not uses_proxy` выполняет `await asyncio.to_thread(socket.getaddrinfo, hostname, None)` и проверяет все IP через `_is_public_ip`.

4. **Санитизация секретов (`sanitize_secrets(text: str) -> str`):**
   - Маскирует `user:pass@` в любых URL и заменяет `settings.BOT_TOKEN` + паттерн `bot\d+:[A-Za-z0-9_-]+` на `bot<REDACTED>`.

---

### 4.2 `bot.py`, `middlewares/throttling.py`, Docker и окружение

1. **Сессия Telegram в `bot.py`:**
   ```python
   if settings.USE_LOCAL_BOT_API:
       session = AiohttpSession(
           api=TelegramAPIServer.from_base(settings.LOCAL_BOT_API_URL, is_local=True),
           proxy=None,
           timeout=settings.LOCAL_TELEGRAM_REQUEST_TIMEOUT,
       )
   else:
       session = AiohttpSession(
           api=TelegramAPIServer.from_base("https://api.telegram.org"),
           proxy=normalize_aiohttp_proxy(settings.TELEGRAM_PROXY_URL),
           timeout=settings.TELEGRAM_REQUEST_TIMEOUT,
       )
   ```
2. **Startup cleanup и безопасный Shutdown:**
   - На старте очищаются зависшие `dl_*` и `meta_*` директории в `TEMP_DIR` (и для мета-анализа также используется префикс `dl_` или `meta_`).
   - Весь участок работы бота после `await db.init()` — включая `await bot.delete_webhook(drop_pending_updates=True)` и `await dp.start_polling(bot, close_bot_session=False)` — оборачивается в общий `try ... finally` (чтобы ошибка на старте тоже закрывала `bot.session` и `db`).
   - Учёт активных задач: в начале `handle_link_message` и `handle_download_callback` текущая задача `asyncio.current_task()` регистрируется в `active_handler_tasks: set[asyncio.Task]` (с `discard` в `finally`).
   - В `finally:` в `bot.py`:
     1) отменить оставшиеся задачи из `active_handler_tasks`, выполнить `await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=settings.SHUTDOWN_TIMEOUT)` внутри **`try ... except asyncio.TimeoutError: pass`**;
     2) гарантированно выполнить `await bot.session.close()` и `await db.close()`.
3. **Роутинг и `ThrottlingMiddleware`:**
   - На корневом `router` вешаются фильтры приватного чата:
     `router.message.filter(F.chat.type == ChatType.PRIVATE)` и `router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)`.
   - Для ссылок заводится дочерний `link_router` (включаемый в `router`) с фильтром **`HasUrlFilter`** (извлекающим первый URL из `message.entities`/`message.caption_entities` (`url` / `text_link`) и `message.text`/`message.caption`, и передающим `{"extracted_url": url}` в хендлер).
   - `MessageThrottlingMiddleware` регистрируется строго как **inner** middleware на `link_router.message.middleware(...)` (выполняется **после** `HasUrlFilter` и `ChatType.PRIVATE`, а не как `outer_middleware`), поэтому `/start` и обычный текст без ссылок не расходуют `RATE_LIMIT_SECONDS`.
   - `CallbackThrottlingMiddleware` регистрируется как inner middleware `router.callback_query.middleware(...)`. Оба middleware оборачивают ответы в `try/except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError)`. При `rate_limit <= 0` кэш не используется.
4. **Инфраструктура:**
   - `requirements.txt`: `yt-dlp[default]>=2025.11.12`, **`gallery-dl>=1.30.0`**, **`PySocks>=1.7.1`** (явный пакет для SOCKS5 в `requests` внутри `gallery-dl`, т.к. extra `gallery-dl[socks]` не существует!), `Pillow>=10.4.0`.
   - `requirements-dev.txt`: `pytest>=8.0.0`, `pytest-asyncio>=0.24.0`.
   - `Dockerfile`:
     - Установить `curl`, `unzip`, `ca-certificates`, `ffmpeg`, `gosu`.
     - Установить `deno` мульти-архитектурно (`TARGETARCH` `amd64`/`arm64`) в `/usr/local/bin/deno` (`deno --version`).
     - Создать непривилегированного пользователя с домашним каталогом (`useradd -m appuser`, чтобы `$HOME/.cache/deno` был доступен на запись), добавить `docker-entrypoint.sh` с нормализацией CRLF (`sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh`, `chmod +x`), который делает `chown -R appuser:appuser /app/data /tmp/media_saver_bot` и запускает `exec gosu appuser "$@"`.
   - `docker-compose.yml`:
     - Добавить `stop_grace_period: 25s` для сервиса `bot` (достаточно для `SHUTDOWN_TIMEOUT=15s`).
     - Порт `telegram-bot-api` привязать к `"127.0.0.1:8081:8081"`; в комментарии уточнить, что `TELEGRAM_PROXY=http://...` у `aiogram/telegram-bot-api` проксирует исходящие HTTP/webhook-запросы, а не MTProto-трафик в РФ.
   - `.env.example`: разделить `Windows local` и `Docker`, закомментировав Docker-специфичные `TEMP_DIR=/tmp/media_saver_bot` и `host.docker.internal`, чтобы копия `.env.example -> .env` работала на Windows из коробки.

---

### 4.3 `services/database.py` — SQLite WAL, транзакции и кэш

1. **Единое соединение с общей блокировкой и rollback:**
   - `init()` открывает `self._conn = await aiosqlite.connect(self.db_path, timeout=15.0)`, ставит `self._conn.row_factory = aiosqlite.Row`, выполняет `PRAGMA busy_timeout=15000;`, `PRAGMA synchronous=NORMAL;` и `PRAGMA journal_mode=WAL;` (warning при не-WAL).
   - `self._db_lock = asyncio.Lock()` оборачивает **все** операции чтения и записи; запись всегда с `try: ... await commit() except BaseException: await rollback(); raise`.
2. **Upsert счётчика и обновление `thumbnail`:**
   - `increment_download(user_id, username=None)`: `INSERT INTO users (user_id, username, downloads_count) VALUES (?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET downloads_count = downloads_count + 1`.
   - `update_url_thumbnail(url_hash, thumbnail)` для обновления протухших CDN-ссылок.
3. **Схема `file_cache`:**
   - `format_key = f"{settings.api_scope}:{action}"` (`cloud:...` / `local:...`).
   - Для одиночного элемента: `media_type` = `"video" | "animation" | "audio" | "photo" | "document"`, `file_id` = строка Telegram.
   - Для мульти-элементной галереи: `media_type` = `"gallery"`, `file_id` = JSON:
     `{"items": [{"file_id": "...", "type": "photo"|"video"|"animation"|"document"}], "truncated": bool, "total": int}`.
   - `delete_cached_file(url_hash, format_key)` для инвалидации битого кэша.

---

### 4.4 `services/downloader.py` — Subprocess, `yt-dlp`, `gallery-dl`, `Pillow` и FFmpeg

#### 4.4.1 Модели данных
- `MediaInfo`: `url`, `title`, `uploader`, `duration: int = 0`, `thumbnail_url: str | None = None`, `is_gallery: bool = False`, `is_audio_only: bool = False`, `size_best: int = 0`, `size_720p: int = 0`, `size_480p: int = 0`, `size_mp3: int = 0`.
- `MediaItemProbe`: `duration: int`, `width: int`, `height: int`, `has_audio: bool`, `vcodec: str`, `acodec: str`, `pix_fmt: str`.
- `DownloadedMedia`: `files: list[Path]`, `media_type: str` (`"video" | "audio" | "photo" | "document" | "gallery"`), `title: str`, `uploader: str`, `duration: int = 0`, `width: int = 0`, `height: int = 0`, `thumbnail_path: Path | None = None`, `item_types: dict[Path, str]` (`"photo"|"video"|"document"`), `item_meta: dict[Path, tuple[int, int, int]]`, `truncated: bool = False`, `total_items: int = 0`, `skipped_count: int = 0`.
- `DownloadUserError(Exception)` — безопасное пользовательское сообщение.

#### 4.4.2 Куки, подпроцессы и JS-рантайм
- **`_prepare_work_cookies(work_dir: Path) -> str | None`:** проверяет префикс 64 КБ (со снятием UTF-8 BOM `\ufeff`) на наличие валидной 7-колоночной Netscape-строки; при успехе копирует **весь** файл целиком без BOM и без `:ro`-атрибутов в `work_dir / "cookies.txt"`.
- **`_run_subprocess(cmd, timeout, max_stdout_bytes, max_stderr_bytes=262_144)`:**
  - `env` с `PYTHONIOENCODING="utf-8"` и `PYTHONUTF8="1"`.
  - Запуск: на POSIX **обязательно** передавать `start_new_session=True` (чтобы дочерний процесс имел собственный PGID `== proc.pid`); на Windows — `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`.
  - Потоковое чтение `stdout`/`stderr` чанками по 64 КБ с жёстким лимитом; регистрация в `active_subprocesses`; при `TimeoutError`/`CancelledError`/превышении буфера: если `proc.returncode is None`, POSIX `os.killpg(proc.pid, signal.SIGKILL)` с fallback на `proc.kill()` при `ProcessLookupError` / если процесс ещё жив; Windows `taskkill /F /T /PID {proc.pid}` (5с) затем `proc.kill()`; далее `await asyncio.wait_for(proc.wait(), timeout=5.0)` и отмена reader-задач в `finally`.
- **`_ytdlp_js_args()`:** добавляет `["--js-runtimes", f"deno:{deno_path}"]` при наличии `deno` или `node:{node_path}` при наличии `node`.

#### 4.4.3 Мета-анализ без слепого fallback (`extract_metadata`)
1. Валидация URL через `await validate_external_url(url, uses_proxy=bool(settings.get_proxy_for_url(url)))` (заодно блокирует `gallery-dl`-префиксы `r:...`).
2. `yt-dlp`: `sys.executable -m yt_dlp --ignore-config --dump-single-json --no-download --no-playlist --flat-playlist --playlist-items 1:5 --encoding utf-8 --geo-bypass --socket-timeout 25` (+ `--ies default,-generic` при `not ALLOW_GENERIC_EXTRACTOR`, `--proxy`, `--cookies`, `_ytdlp_js_args()`, `--`, `url`), `max_stdout_bytes = 25 * 1024 * 1024`.
3. **Разбор `yt-dlp`:**
   - При `returncode != 0`: сетевые/таймаут/429/`Private video`/`Sign in`/`Video unavailable` -> `DownloadUserError`. Отсутствие видеоформатов или неподдерживаемая ссылка от соцсети/фотоплатформы (`instagram`, `pinterest`, `twitter`, `x`, `reddit`, `imgur`, `tiktok` и др. известных экстракторов `gallery-dl`) -> **шаг 4 (`gallery-dl -J`)**.
   - При JSON:
     - `is_live` / `live_status == "is_live"` -> `DownloadUserError("Прямые трансляции не поддерживаются.")`.
     - `_type in ("playlist", "multi_video")`:
       - `entries` пуст -> `DownloadUserError`.
       - Профили/каналы/ленты соцсетей (`TikTokUser`, `InstagramUser`, `TwitterUser`, `YoutubeTab`, `SoundcloudSet` и т.п. плейлист- или профиль-экстракторы) -> **немедленно** `DownloadUserError("Плейлисты и профили не поддерживаются. Отправьте ссылку на конкретное видео или пост.")`.
       - Карусельный экстрактор конкретного поста (`Instagram`, `Twitter`, `TikTok`, `Reddit`, `Pinterest`, когда URL — не профиль): `len(entries) > 1` -> **шаг 4**; `len(entries) == 1` -> распаковать `entries[0]` (если `_type in ("url", "url_transparent")`, 1 повторный запрос по `entries[0]["url"]` с `validate_external_url(..., uses_proxy=bool(active_proxy))`, наследуя `active_proxy = settings.get_proxy_for_url(url)`).
       - Иначе (`YoutubeTab`, `SoundcloudSet` и прочие списки, независимо от `len(entries)`) -> `DownloadUserError("Плейлисты и каналы не поддерживаются. Отправьте ссылку на конкретное видео.")`.
     - Одиночное изображение (`ext in ("jpg", "jpeg", "png", "webp")`) -> **обязательно** передать на **шаг 4 (`gallery-dl -J`)** (чтобы все фото-посты проходили через единую валидацию категорий и URL без обхода!).
     - Разбор `formats` (`video_only`, `audio_only`, `combined` без двойного суммирования!): отсутствие видеоформатов и не-видео `ext` -> `is_audio_only = True`; расчёт `size_best`, `size_720p`, `size_480p`, `size_mp3` по приоритету реальных селекторов (H.264+AAC / MP4 / fallback).
4. **Шаг 4: Явная валидация через `gallery-dl`:**
   - Отсечение профилей/досок по URL-паттернам (`instagram.com/<user>/`, `pinterest.com/<user>/<board>/`, `x.com/<user>` без `/status/`).
   - Запуск `sys.executable -m gallery_dl --config-ignore -J --range 1-31` (+ `--proxy`, `--cookies`, `--`, `url`; без `--encoding`).
   - Проверка битовой маски `returncode` (`gallery-dl >= 1.30.0`): `& 16` -> cookies, `& 8` -> anti-bot challenge, `!= 0` -> `DownloadUserError`.
   - Парсинг **всех** событий массива `DataJob` (`[2, meta_dict]` и `[3, asset_url, meta_dict]`):
     - **Строгая проверка категории во всех событиях:** если хотя бы в одном `[2, meta_dict]` или `[3, url, meta_dict]` встречается `category in ("directlink", "generic", "")` или `subcategory in ("user", "board", "avatar", "following", "saved", "feed", "subreddit", "search", "media", "replies", "reels", "tagged", "created", "section")` -> `DownloadUserError("Ссылки этого типа не поддерживаются.")` (блокирует протаскивание `directlink` через `[6, ...]` из Reddit/постов, профили, ленты и поиск).
     - Валидация каждого `[3, asset_url]` и `[6, url]` через `validate_external_url(item_url, uses_proxy=bool(active_proxy))`, где `active_proxy = settings.get_proxy_for_url(url)` унаследован от исходного URL поста (т.к. CDN-домены `pbs.twimg.com`, `scontent-*.cdninstagram.com`, `i.pinimg.com` отсутствуют в `PROXY_DOMAINS`, но запросы к ним идут через тот же прокси).
     - 0 валидных `[3, ...]` -> `DownloadUserError`.
     - Извлечение `title`, `uploader`, `thumbnail_url`.

#### 4.4.4 Скачивание видео/аудио (`download_ytdlp`) и `ffprobe`
- Полная команда:
  `sys.executable -m yt_dlp --ignore-config --no-simulate --no-quiet --no-progress --no-playlist --playlist-items 1 --print after_move:filepath --max-filesize {MAX_RAW_DOWNLOAD_MB}M --encoding utf-8 --geo-bypass --socket-timeout 25 -o {work_dir / "media_%(id)s.%(ext)s"} -f {format_selector}` (+ `--merge-output-format mp4` для видео или `-x --audio-format mp3 --audio-quality 192K --add-metadata` для `mp3`; `--ies default,-generic` при `not ALLOW_GENERIC_EXTRACTOR`, `--proxy`, `--cookies`, `_ytdlp_js_args()`, `--`, `url`).
  *(Пояснение: при `--no-quiet` поток `screen` в `yt-dlp` пишет в `stdout`, поэтому сообщение о `max-filesize` попадает в `stdout`; проверять `max-filesize` / `File is larger` нужно по объединению `stdout + "\n" + stderr`, а итоговый путь из `--print after_move:filepath` — перебором строк `stdout` снизу вверх, выбирая существующий `Path(line).is_file()` внутри `work_dir`!)*
- Таймаут скачивания в `download_ytdlp` и `download_gallery`: `settings.effective_download_timeout`.
- Селекторы форматов:
  - `mp3`: `bestaudio/best`;
  - `720p`: `bestvideo[height<=720][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]`;
  - `480p`: аналогично с `height<=480`;
  - `best`: `bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best`;
  - `compress`: при `effective_max_filesize_mb <= 50` пытается сначала взять `720p` (или вертикальное видео с `width<=720`), с **обязательным fallback** на `best`-селектор (`{720p_or_width720_selector}/{best_selector}`), чтобы вертикальные Reels/TikTok `720x1280` / `1080x1920` и single-stream `1080p` никогда не падали с `Requested format is not available`! При расчёте `can_720p`/`can_480p` проверяется наличие форматов с подходящей высотой (или меньшей стороной `min(width, height)` для вертикальных роликов).
- Если файл не найден и `stdout + "\n" + stderr` содержит `max-filesize` / `File is larger` -> `DownloadUserError("Видео превышает максимально допустимый размер загрузки.")`.
- `probe_media(path) -> MediaItemProbe`: точные `duration`, `width`, `height` (с учётом rotation), `has_audio`, `vcodec`, `acodec`, `pix_fmt`.
- Для `mp3`: при `not has_audio` -> `DownloadUserError("В этом медиа отсутствует аудиодорожка.")`.
- **Нормализация видео в пределах лимита (`<= effective_max_filesize_bytes`):**
  Если контейнер != `.mp4` или `vcodec != "h264"` или `pix_fmt != "yuv420p"` или `(has_audio and acodec not in ("aac", "mp3", "mp4a"))`:
  - Целевой новый файл: `normalized_{input_path.stem}.mp4` (никогда не перезаписывает входной файл in-place!).
  - Если `vcodec == "h264"` и `pix_fmt == "yuv420p"` и `(not has_audio or acodec in ("aac", "mp3", "mp4a"))`: stream-copy remux `-protocol_whitelist file,pipe,crypto -i ... -map 0:v:0 -map 0:a:0? -c copy -movflags +faststart`.
  - Иначе: однопроходная перекодировка CRF:
    `-protocol_whitelist file,pipe,crypto -i ... -map 0:v:0 -map 0:a:0? -sn -dn -vf "scale='trunc(iw/2)*2':-2" -c:v libx264 -preset faster -crf 23 -pix_fmt yuv420p` (`-c:a aac -b:a 128k` или `-an`) `-movflags +faststart`, таймаут `COMPRESS_TIMEOUT` / `LOCAL_COMPRESS_TIMEOUT`.
  - Удалить исходник, проверить размер: если стал > лимита -> `compress_video_to_limit`.

#### 4.4.5 Изображения, animated WebP, обложки и галереи
- **`_fetch_image_bytes(url, source_url, max_bytes=20*1024*1024)`:**
  `active_proxy = settings.get_proxy_for_url(source_url)`; `allow_redirects=False`, цикл до 5 редиректов с `urllib.parse.urljoin(current_url, location)` и `await validate_external_url(next_url, uses_proxy=bool(active_proxy))` (наследует `uses_proxy` от `source_url`, т.к. CDN редиректы идут через тот же прокси); `cookie_jar=aiohttp.DummyCookieJar()`; `ProxyConnector.from_url(normalize_aiohttp_proxy(active_proxy), rdns=True)`; чтение чанками `<= max_bytes`.
- **Единый нормализатор изображений (`Pillow` + `ffmpeg` для animated WebP):**
  - `Image.MAX_IMAGE_PIXELS = 40_000_000`.
  - Animated WebP (`img.format == "WEBP"` и `is_animated` и `n_frames > 1`):
    - Для галереи: извлекает кадры в RGB с композитингом альфа-канала на белый/чёрный фон, пишет в `ffconcat` **строго без UTF-8 BOM** (`encoding="utf-8"`, используя **относительные имена файлов**, `duration >= 20` мс и дублируя последний кадр без `duration`), кодирует с тихой аудиодорожкой (`-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 -shortest`, чтобы Telegram сохранял результат как `video`, а не конвертировал в `animation` внутри альбома): `-protocol_whitelist file,pipe,crypto,lavfi -f concat -safe 0 -i ... -f lavfi -i anullsrc=... -vf "scale='trunc(iw/2)*2':-2" -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 64k -shortest -movflags +faststart` в `.mp4`.
    - Для `cover`: первый кадр в RGB JPEG.
  - Статические изображения: `CMYK`/`P`/`RGBA` -> `RGB`; `ratio > 20:1` -> `"document"` (если размер превышает `settings.effective_max_filesize_bytes`, файл пропускается или сжимается до лимита); иначе `w + h <= 10000`, `max(w, h) <= 4096` (или `320` для миниатюр), JPEG шагом `92 -> 85 -> 75 -> 65` до `<= 9.5 МБ` (`<= 200 КБ` для миниатюр).
- **`download_cover`:** оригинал в полном качестве; при 403/404/410 обновляет `thumbnail_url` через мета-запрос (сохраняя в `url_map.thumbnail` через `db.update_url_thumbnail`) и повторяет.
- **`download_gallery`:**
  - Запуск: `sys.executable -m gallery_dl --config-ignore --no-mtime -d str(gallery_dir) -o "extractor.directory=[]" -o "extractor.filename={num:?//0>4}_{filename}.{extension}" --range 1-{MAX_GALLERY_ITEMS + 1} --no-part` (+ `--proxy`, `--cookies`, `--`, `url`), таймаут `settings.effective_download_timeout`.
    *(Пояснение: `--no-mtime` предотвращает подстановку HTTP `Last-Modified` в `mtime` файлов, гарантируя точный порядок скачивания при сортировке `(p.stat().st_mtime_ns, p.name)`, а `0>4` явно задаёт заполнение нулями).*
  - Валидация путей `is_relative_to`, суммарный вес `<= MAX_RAW_DOWNLOAD_MB`, нормализация фото/видео, заполнение `item_types` и `item_meta`.

#### 4.4.6 Сжатие FFmpeg (`compress_video_to_limit`)
- `probe_media(input_path)`; при `duration <= 0` -> `DownloadUserError`.
- `target_mb = max(1, settings.effective_max_filesize_mb - 1)`; 6% запас на контейнер; адаптивное аудио; при `video_bps < 100_000` -> `DownloadUserError` **до** старта FFmpeg.
- `max_w`: `1920` при `target_mb > 100` (Local Bot API), иначе `1280` / `854` / `640`.
- Выход: `compressed_{input_path.stem}.mp4`. `-protocol_whitelist file,pipe,crypto` строго перед `-i`, `-map 0:v:0 -map 0:a:0? -sn -dn`, `-vf scale='trunc(min({max_w},iw)/2)*2':-2 -pix_fmt yuv420p`, Pass 1 `-an -f null -`, Pass 2 `-c:a aac`/`-an` + `-movflags +faststart`, проверка `returncode == 0` + финальный `probe_media`.

---

### 4.5 `keyboards/inline.py` и `handlers/media.py` — UI, Single-Flight, кэш и ошибки

1. **Клавиатура (`build_media_keyboard`) и `truncate_html_caption`:**
   - Параметры: `(url_hash, limit_mb, is_gallery, is_audio_only, has_thumbnail, can_best, can_720p, can_480p)`.
   - Для `is_gallery`: `🖼 Скачать все фото / Галерею` + `🖼 Скачать обложку / Пин` (при `has_thumbnail`).
   - Для `is_audio_only`: `🎵 Аудио (MP3)` + `🖼 Скачать обложку / Пин` (при `has_thumbnail`), без видеокнопок.
   - Для видео: `Лучшее` (если `can_best`), `720p` (если `can_720p`), `480p (Эконом)` и `🗜 Сжать видео до {limit_mb} МБ` (если `not can_best or not can_720p`, причём `480p` только при `can_480p`), `🎵 Аудио (MP3)`, `🖼 Скачать обложку / Пин` (при `has_thumbnail`), динамический `adjust`.
   - `truncate_html_caption`: обрезает сырые `title` и `uploader` **до** `html.escape`, гарантируя длину `< 1024` без разрыва тегов и `&...;` для всех подписей (`sendPhoto`, `sendVideo`, `sendAudio`, `sendMediaGroup`).
2. **Входящие ссылки (`handle_link_message`):**
   - Подвешен на `link_router.message(HasUrlFilter())`; принимает `extracted_url: str` напрямую от фильтра (поддерживаются `text`, `caption`, `url` и `text_link` сущности — без отсечения сообщений без `message.text`).
   - Регистрирует `asyncio.current_task()` в `active_handler_tasks` (с `discard` в `finally`), использует `metadata_semaphore` + `user_active_counts` (`try/finally`).
   - **Без удаления сообщения пользователя** (R3).
   - Полная обёртка ошибок; при `CancelledError` — отмена подпроцессов, очистка `temp_dir` и попытка обновить статус через `await asyncio.wait_for(safe_update_status(status_msg, ..., request_timeout=2.0), timeout=2.5)` (в `aiogram 3` `request_timeout` передаётся параметром метода Bot API, т.к. `bot.request_timeout()` удалён!), затем `raise`; превью-фото через `_fetch_image_bytes` + `FSInputFile`.
3. **Обработка кнопок (`handle_download_callback`) — порядок и Single-Flight:**
   - Регистрирует `asyncio.current_task()` в `active_handler_tasks` (с `discard` в `finally`).
   - Вход (строго без промежуточных `await` при захвате): (1) проверка `isinstance(callback.message, Message)` -> `safe_callback_answer(..., show_alert=True)` при `InaccessibleMessage`; (2) проверка `action in ALLOWED_ACTIONS`; (3) проверка `msg_key in in_flight_messages` -> всплывающий отказ; (4) проверка `user_active_counts.get(user_id, 0) >= MAX_ACTIVE_JOBS_PER_USER`; (5) добавление `msg_key` в `in_flight_messages` и инкремент `user_active_counts` внутри `try/finally` (`discard` и `pop(..., None)` при `0`), затем `safe_callback_answer(callback)` и снятие кнопок `safe_update_status`.
   - **Single-Flight `InflightJob` (строго ДО `download_semaphore`):**
     - Расчёт таймаута ожидания по активному режиму:
       `wait_timeout = (settings.LOCAL_COMPRESS_TIMEOUT * 2 + settings.effective_download_timeout + settings.LOCAL_TELEGRAM_REQUEST_TIMEOUT + 60) if settings.USE_LOCAL_BOT_API else (settings.COMPRESS_TIMEOUT * 2 + settings.effective_download_timeout + settings.TELEGRAM_REQUEST_TIMEOUT + 60)`.
     - Цикл ожидания (макс. 2 итерации, т.е. до 1 перехвата лидерства):
       - Если `job := inflight_jobs.get(job_key)` существует:
         - `try: await asyncio.wait_for(job.event.wait(), timeout=wait_timeout) except asyncio.TimeoutError: raise DownloadUserError("⏱ Превышено время ожидания загрузки. Попробуйте позже.")` *(не запускает дубликат поверх живого лидера!)*.
         - Проверить `cached = await db.get_cached_file(...)`: если найден — доставить из кэша и завершить.
         - Иначе если `job.error_msg` — выбросить `DownloadUserError(job.error_msg)`.
         - Иначе (`cached is None and job.error_msg is None`, например лидер упал на личной доставке в чужой чат или по `CancelledError`) — на 1-й итерации сделать `continue` (где синхронно без промежуточного `await` проверяется `inflight_jobs.get(job_key)`), а если и после 2-й итерации нет кэша и `error_msg` -> явно выбросить `DownloadUserError("Не удалось завершить загрузку. Попробуйте ещё раз.")` (сообщение никогда не зависнет!).
   - **Отправка и кэширование (`send_with_retry`, макс. 2 ретрая на `TelegramRetryAfter <= 60s`):**
     - **Чанкинг галереи (свежей и из кэша):**
       - Элементы `"document"` и `"animation"` отделяются от `"photo"`/`"video"`, т.к. `sendMediaGroup` запрещает смешивать `InputMediaDocument` и не поддерживает `InputMediaAnimation` (`"document"` отправляется отдельно через `answer_document` / document-group ≤10; `"animation"` — через `answer_animation`).
       - `"photo"`/`"video"` делятся чанками `<= 10` элементов и `<= 45 МБ` (Cloud) / `<= 1900 МБ` (Local). **Если в чанке оказался ровно 1 элемент** (например, одиночное 47 МБ видео после отсечения) — отправляется через `answer_video` / `answer_photo`, а НЕ через `sendMediaGroup` (Telegram требует 2..10 элементов в `sendMediaGroup`!).
       - При кэшировании одиночного видео или элемента `sendMediaGroup` проверяется `sent.video` vs `sent.animation`: если Telegram вернул `sent.animation`, сохраняется `media_type="animation"` и `sent.animation.file_id`, а при cache-hit вызывается `answer_animation` (никогда `answer_video`, т.к. Telegram вернёт `type of file mismatch`!).
     - Инвалидация битого `file_id`, безопасный `safe_delete_message`, а при `CancelledError` — убийство подпроцессов, очистка `temp_dir` и попытка обновить статус через `await asyncio.wait_for(safe_update_status(msg, ..., request_timeout=2.0), timeout=2.5)` (передача `request_timeout=2.0` в `edit_text`/`edit_caption`, без несуществующего `bot.request_timeout()`), затем `raise`.

---

## 5. План тестирования и верификации

1. **Юнит-тесты (`tests/` на `pytest` + `pytest-asyncio`):**
   - `test_config_and_ssrf.py`: `get_proxy_for_url`, `validate_external_url` (`.rstrip(".")`, `::ffff:127.0.0.1`, `100.64.0.1`, `0177.0.0.1`, `127.1`, `r:` префикс, `uses_proxy`), `normalize_aiohttp_proxy`, `sanitize_secrets`.
   - `test_metadata_and_gallery.py`: `is_audio_only`, `FormatSizeEstimate`, плейлисты, `--no-quiet --no-progress` + `--max-filesize`, `gallery-dl -J` (`--no-mtime`, запрет `category == "directlink"`, `PySocks` зависимость), валидация извлечённых URL.
   - `test_ffmpeg_and_images.py`: `compress_video_to_limit` (`compressed_*.mp4`, `-protocol_whitelist` перед `-i`, `-map`, `< 100k`, R5, `max_w=1920`), CRF-нормализация (`normalized_*.mp4`, `-vf scale='trunc(iw/2)*2':-2`), remux с проверкой `acodec`, animated WebP `ffconcat` (относительные пути, `duration >= 20ms`, последний кадр), `ratio > 20:1` -> `"document"`.
   - `test_concurrency_and_handlers.py`: `InflightJob` (динамический `wait_timeout`, гонка перехвата лидерства, таймаут поверх живого лидера), `in_flight_messages`, `user_active_counts`, `HasUrlFilter` до `MessageThrottlingMiddleware`, `sendMediaGroup` с `len(chunk) == 1` через `answer_video`, разделение `"document"`/`"animation"`, кэширование `sent.animation` + `answer_animation`, `CancelledError` до закрытия `bot.session`/`db`.
   - `test_database.py`: WAL/busy_timeout, `_db_lock`, `rollback()` на `BaseException`, `increment_download` upsert, кэш галерей с `"document"`, `"animation"` и `"audio"`.

---

## 6. Открытые компромиссы для утверждения пользователем

1. **`downloads_count` при отдаче из кэша:** Предлагается считать все *реальные* скачивания (`best`, `720p`, `480p`, `compress`, `mp3`, `gallery`, `cover`) и не считать мгновенную отдачу из кэша (или учитывать и кэш-хиты — переключение в 1 строку).
2. **`deno` + `yt-dlp[default]` + `Pillow` + `gallery-dl` + `PySocks` + `gosu` в `Dockerfile`:** Увеличит образ на ~55–70 МБ, но обеспечит работу YouTube EJS, SOCKS5 в `gallery-dl`, animated WebP на FFmpeg 5.1 и безопасные права непривилегированного `appuser`.
3. **Запрет `ALLOW_GENERIC_EXTRACTOR=False` и `directlink`:** Надёжно блокирует SSRF через произвольные HTML/картинки; при необходимости новые домены добавляются в `PROXY_DOMAINS` / разрешённые экстракторы.

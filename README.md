# Media Saver Bot

Универсальный Telegram-бот для сохранения медиа по ссылкам.

## Описание

Бот принимает ссылку на видео/контент из источников — **YouTube, Instagram, Pinterest, TikTok, X (Twitter), SoundCloud** — и:

- анализирует ссылку через `yt-dlp` / `gallery-dl`;
- отправляет превью/обложку с кнопками форматов;
- скачивает видео в выбранном качестве (720p / 480p / оптимальное / MP3 / сжатие FFmpeg / галерея / обложка);
- кэширует готовые файлы по ссылке, чтобы повторные запросы отдавали мгновенно.

## Что обновлено

- Исправлен формат длительности: теперь отображается в корректном виде `HH:MM:SS` (например, `01:05:09`).
- Исправлена логика проверки лимита размера файла при использовании Local Telegram Bot API — файлы до ~1950 МБ не пережимаются, трафик к Local API идёт без `TELEGRAM_PROXY_URL`, увеличен таймаут загрузки.
- Добавлена защита от SSRF, точное сопоставление доменов прокси, изоляция `cookies.txt` (для `:ro` Docker-монтирования), single-flight защита от двойных кликов и callback-троттлинг.
- Бот работает только в личных чатах, не удаляет сообщения пользователя, отклоняет невалидные ссылки/плейлисты/профили без ложного fallback в галерею, отдаёт обложки/пины в полном качестве, а галереи >10 файлов отправляет альбомами по ≤10 элементов (с нормализацией `.webp`, animated WebP и панорам `ratio > 20:1`).
- Минимальный Docker-образ с `USER 10001:10001`, `deno` (YouTube EJS для `yt-dlp`), `PySocks` (для `gallery-dl` через SOCKS5), `Pillow` и интеграцией sidecar `xray` (`ghcr.io/xtls/xray-core:latest`) в `docker-compose.yml`.

## Развёртывание на Linux CasaOS (Docker + Xray + Watchtower)

Бот работает по схеме **long polling** и не имеет веб-интерфейса / Mini App / webhook, поэтому **туннель `devtunnel` и публикация портов (`ports:`) ему не нужны**.

1. **Подготовка каталогов и прав на CasaOS** (используется уже существующий `/DATA/AppData/xray/config.json`):

   ```bash
   sudo mkdir -p /DATA/AppData/media-saver-bot/data
   sudo chown -R 10001:10001 /DATA/AppData/media-saver-bot/data
   cd /DATA/AppData/media-saver-bot
   touch cookies.txt && chmod 644 cookies.txt
   ```

2. **Скопируйте `docker-compose.yml` и `.env.example` в `/DATA/AppData/media-saver-bot/`:**

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

   В `.env` обязательно заполните `BOT_TOKEN`. Для sidecar-контейнера `xray` (`network_mode: "service:xray"`) оставьте:

   ```dotenv
   PROXY_ENABLED=true
   PROXY_URL=http://127.0.0.1:10809
   TELEGRAM_PROXY_URL=http://127.0.0.1:10809
   ```

3. **Убедитесь, что пакет в GHCR публичный** (`GitHub → Settings → Packages → media-saver-bot → Change visibility → Public`), чтобы Watchtower на сервере мог обновлять образ без токена.

4. **Перед запуском на сервере остановите тестовый бот на Windows** (иначе возникнет конфликт `BOT_TOKEN` / `TelegramConflictError`):
   - если запускали локальный Docker: `docker compose -f docker-compose.local.yml down`
   - если запускали `python bot.py`: закройте процесс в терминале.

5. **Запустите стек на CasaOS:**

   ```bash
   cd /DATA/AppData/media-saver-bot
   sudo docker compose pull bot xray
   sudo docker compose up -d
   sudo docker compose logs -f bot
   ```

   - Сервис `media-saver-xray` поднимает локальный HTTP-прокси `127.0.0.1:10809` из `/DATA/AppData/xray/config.json` (не затрагивая другие проекты) и помечен `com.centurylinklabs.watchtower.enable: "false"`, чтобы Watchtower не рвал сетевой стек `service:xray`.
   - Сервис `media_saver_bot` работает под `USER 10001:10001` с `cap_drop: [ALL]` и `no-new-privileges:true`, а Watchtower (уже запущенный на сервере) автоматически обновляет его каждые 300 секунд по метке `com.centurylinklabs.watchtower.enable: "true"`.
   - Если на чистом сервере Watchtower ещё не запущен, можно поднять его с профилем `docker compose --profile watchtower up -d`.

6. **Обновление ключей VPN в Xray:**
   Когда ключи в `/DATA/AppData/xray/config.json` полностью устареют, обновите файл из Happ и перезапустите только `xray` (или оба сервиса, если требуется):

   ```bash
   sudo docker compose -f /DATA/AppData/media-saver-bot/docker-compose.yml restart xray
   ```

## Локальный запуск и разработка (Windows)

1. **Требования:**
   - Python 3.11+ или Docker Desktop;
   - `ffmpeg`, `ffprobe`, `yt-dlp`, `gallery-dl`, `deno` (или `node`, рекомендуется для YouTube);
   - файл `cookies.txt` (Netscape format) для Instagram/TikTok и т.п. (необязательно).

2. **Запуск:**
   - локально: `pip install -r requirements.txt && python bot.py` (в `.env` укажите локальные адреса `127.0.0.1`, например `PROXY_URL=http://127.0.0.1:10809`, `TELEGRAM_PROXY_URL=socks5://127.0.0.1:1443` или пусто);
   - тесты: `pip install -r requirements-dev.txt && pytest`;
   - локальный Docker: создайте пустой `cookies.txt` (`type nul > cookies.txt`), в `.env` для прокси на хосте Windows укажите `host.docker.internal` и выполните:
     `docker compose -f docker-compose.local.yml up -d --build`
     (для остановки: `docker compose -f docker-compose.local.yml down`).

## Полная автоматизация: Windows → GitHub Actions → CasaOS

При push в `main` workflow `.github/workflows/docker.yml` собирает образ и публикует:

- `ghcr.io/andrew190543/media-saver-bot:latest`
- `ghcr.io/andrew190543/media-saver-bot:sha-XXXXXXX`

Важно:

- `.env`, `cookies.txt` и `data/` исключены из образа через `.dockerignore` — токен и cookies не попадают в GHCR.
- Зависимости (`yt-dlp`, `gallery-dl`, `ffmpeg`, `deno`) ставятся при сборке образа. Чтобы обновить их без изменений кода, нажмите **Run workflow** в GitHub Actions.

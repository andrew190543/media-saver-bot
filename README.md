# Media Saver Bot

Универсальный Telegram-бот для сохранения медиа по ссылкам.

## Описание

Бот принимает ссылку на видео/контент из источников — **YouTube, Instagram, Pinterest, TikTok, X (Twitter), SoundCloud** — и:

- анализирует ссылку через `yt-dlp`;
- отправляет превью/обложку с кнопками форматов;
- скачивает видео в выбранном качестве (720p / 480p / оптимальное / MP3 / сжатие FFmpeg / галерея / обложка);
- кэширует готовые файлы по ссылке, чтобы повторные запросы отдавали мгновенно.

## Что обновлено

- Исправлен формат длительности: теперь отображается в корректном виде `HH:MM:SS` (например, `01:05:09`).
- Исправлена логика проверки лимита размера файла при использовании Local Telegram Bot API — файлы до ~1950 МБ не пережимаются, трафик к Local API идёт без `TELEGRAM_PROXY_URL`, увеличен таймаут загрузки.
- Добавлена защита от SSRF, точное сопоставление доменов прокси, изоляция `cookies.txt` (для `:ro` Docker-монтирования), single-flight защита от двойных кликов и callback-троттлинг.
- Бот работает только в личных чатах, не удаляет сообщения пользователя, отклоняет невалидные ссылки/плейлисты/профили без ложного fallback в галерею, отдаёт обложки/пины в полном качестве, а галереи >10 файлов отправляет альбомами по ≤10 элементов (с нормализацией `.webp`, animated WebP и панорам `ratio > 20:1`).
- В `Dockerfile` добавлены `deno` (YouTube EJS для `yt-dlp`), `PySocks` (для `gallery-dl` через SOCKS5), `Pillow`, `gosu` и непривилегированный запуск `appuser`.

## Как пользоваться

1. **Требования:**
   - Python 3.11+ или Docker;
   - `ffmpeg`, `ffprobe`, `yt-dlp`, `gallery-dl`, `deno` (или `node`, рекомендуется для YouTube);
   - файл `cookies.txt` (Netscape format) для Instagram/TikTok и т.п. (необязательно);
   - локальные прокси:
     - для сайтов — V2Ray из вашего набора **zapret** (пример: `socks5://127.0.0.1:1080`);
     - для Telegram — **TgWsProxy** (`C:\Users\VELoz\Downloads\TgWsProxy_windows.exe`, `socks5://127.0.0.1:1443`).

2. **Настройка:**
   - Скопируйте `.env.example` в `.env` и укажите:
     - `BOT_TOKEN` — токен от @BotFather;
     - `PROXY_ENABLED=true`, `PROXY_URL=socks5://127.0.0.1:1080` (или `host.docker.internal` для Docker);
     - `TELEGRAM_PROXY_URL=socks5://127.0.0.1:1443` (оставьте пустым при `USE_LOCAL_BOT_API=true` или без TgWsProxy);
     - `MAX_FILESIZE_MB`, `USE_LOCAL_BOT_API`, `LOCAL_MAX_FILESIZE_MB` и другие параметры по необходимости.

3. **Запуск:**
   - локально: `pip install -r requirements.txt && python bot.py` (в `.env` прокси должны быть `127.0.0.1`);
   - тесты: `pip install -r requirements-dev.txt && pytest`;
   - Docker из GHCR: `docker compose pull bot && docker compose up -d`;
   - Docker локальная сборка: замените `image` на `build: .` в `docker-compose.yml` и выполните `docker compose up -d --build`.

4. **Использование:**
   - Отправьте боту в личные сообщения ссылку на видео, аудио или пост/галерею (мульти-видео плейлисты и профили не поддерживаются).
   - Бот ответит превью и предложит выбрать формат: «Видео: Лучшее», «Видео: 720p», «Видео: 480p», «Аудио (MP3)», «Сжать видео», «Галерея», «Обложка / Пин».

## Полная автоматизация: GitHub Actions + GHCR + Watchtower

При push в `main` workflow `.github/workflows/docker.yml` собирает образ и публикует:

- `ghcr.io/andrew190543/media-saver-bot:latest`
- `ghcr.io/andrew190543/media-saver-bot:sha-XXXXXXX`

Watchtower опрашивает реестр каждые 5 минут и перезапускает только контейнер с меткой
`com.centurylinklabs.watchtower.enable=true` (то есть `bot`).

**Первоначальная настройка на сервере:**

1. Скопируйте `docker-compose.yml` и `.env.example` на сервер, переименуйте `.env.example` в `.env`
   и заполните `BOT_TOKEN`, прокси и при необходимости `USE_LOCAL_BOT_API=true`.
2. Создайте файл `cookies.txt` (можно пустой, если cookies не нужны): на Windows — `type nul > cookies.txt`.
3. После первого успешного Actions-запуска выполните `docker compose pull bot && docker compose up -d`.
4. Если пакет GHCR приватный: откройте `Settings → Packages → <package> → Change visibility` (Public)
   либо добавьте в `.env` `GHCR_USER` и `GHCR_TOKEN` (PAT с `read:packages`) и раскомментируйте
   `REPO_USER` / `REPO_PASS` в `docker-compose.yml`.

Важно:

- `.env`, `cookies.txt` и `data/` исключены из образа через `.dockerignore` — токен и cookies не попадают в GHCR.
- Зависимости (`yt-dlp`, `gallery-dl`, `ffmpeg`) ставятся при сборке образа. Чтобы обновить их без изменений кода,
  нажмите **Run workflow** в Actions.
- Локальные прокси V2Ray/TgWsProxy должны быть доступны с хоста Docker; `host.docker.internal`
  пробрасывается в `docker-compose.yml`. При запуске без Docker используйте `127.0.0.1`.

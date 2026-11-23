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
- Исправлена логика проверки лимита размера файла при использовании Local Telegram Bot API — файлы до ~1950 МБ теперь корректно проходит проверку.
- Добавлена настройка прокси для Telegram (`TELEGRAM_PROXY_URL`) — трафик бота к Telegram API идёт через локальный **TgWsProxy** (`socks5://127.0.0.1:1443`).
- Включён прокси для скачивания сайтов через локальный прокси (**zapret / V2Ray**, `socks5://127.0.0.1:1080`).

## Как пользоваться

1. **Требования:**
   - Python 3.11+ или Docker;
   - `ffmpeg`, `yt-dlp`, `gallery-dl`;
   - файл `cookies.txt` (Netscape format) для Instagram/TikTok и т.п. (необязательно);
   - локальные прокси:
     - для сайтов — V2Ray из вашего набора **zapret** (пример: `socks5://127.0.0.1:1080`);
     - для Telegram — **TgWsProxy** (`C:\Users\VELoz\Downloads\TgWsProxy_windows.exe`, `socks5://127.0.0.1:1443`).

2. **Настройка:**
   - Скопируйте `.env.example` в `.env` и укажите:
     - `BOT_TOKEN` — токен от @BotFather;
     - `PROXY_ENABLED=true`, `PROXY_URL=socks5://127.0.0.1:1080`;
     - `TELEGRAM_PROXY_URL=socks5://127.0.0.1:1443`;
     - `MAX_FILESIZE_MB`, `USE_LOCAL_BOT_API`, `LOCAL_MAX_FILESIZE_MB` и другие параметры по необходимости.

3. **Запуск:**
   - локально: `pip install -r requirements.txt && python bot.py` (в `.env` прокси должны быть `127.0.0.1`);
   - Docker из GHCR: `docker compose pull bot && docker compose up -d`;
   - Docker локальная сборка: замените `image` на `build: .` в `docker-compose.yml` и выполните `docker compose up -d --build`.

4. **Использование:**
   - Отправьте боту ссылку на видео/плейлист.
   - Бот ответит превью и предложит выбрать формат: «Видео: Лучшее», «Видео: 720p», «Видео: 480p», «Аудио (MP3)», «Сжать видео до 48 МБ», «Галерея», «Обложка».

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

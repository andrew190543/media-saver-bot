from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

ALLOWED_ACTIONS = frozenset({"best", "720p", "480p", "compress", "mp3", "cover", "gallery"})


class MediaCallback(CallbackData, prefix="dl"):
    url_hash: str
    action: str


def build_media_keyboard(
    url_hash: str,
    limit_mb: int,
    is_gallery: bool = False,
    is_audio_only: bool = False,
    has_thumbnail: bool = True,
    can_best: bool = True,
    can_720p: bool = True,
    can_480p: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if is_gallery:
        builder.button(
            text="🖼 Скачать все фото / Галерею",
            callback_data=MediaCallback(url_hash=url_hash, action="gallery"),
        )
        if has_thumbnail:
            builder.button(
                text="🖼 Скачать обложку / Пин",
                callback_data=MediaCallback(url_hash=url_hash, action="cover"),
            )
        builder.adjust(1)
        return builder.as_markup()

    if is_audio_only:
        builder.button(
            text="🎵 Аудио (MP3)",
            callback_data=MediaCallback(url_hash=url_hash, action="mp3"),
        )
        if has_thumbnail:
            builder.button(
                text="🖼 Скачать обложку / Пин",
                callback_data=MediaCallback(url_hash=url_hash, action="cover"),
            )
        builder.adjust(1)
        return builder.as_markup()

    video_count = 0
    if can_best:
        builder.button(
            text="🎬 Видео: Лучшее",
            callback_data=MediaCallback(url_hash=url_hash, action="best"),
        )
        video_count += 1
    if can_720p:
        builder.button(
            text="🎬 Видео: 720p",
            callback_data=MediaCallback(url_hash=url_hash, action="720p"),
        )
        video_count += 1

    if not can_best or not can_720p:
        if can_480p:
            builder.button(
                text="🎬 Видео: 480p (Эконом)",
                callback_data=MediaCallback(url_hash=url_hash, action="480p"),
            )
            video_count += 1
        builder.button(
            text=f"🗜 Сжать видео до {limit_mb} МБ (FFmpeg)",
            callback_data=MediaCallback(url_hash=url_hash, action="compress"),
        )
        video_count += 1

    builder.button(
        text="🎵 Аудио (MP3)",
        callback_data=MediaCallback(url_hash=url_hash, action="mp3"),
    )

    row_layout = [2] * (video_count // 2)
    if video_count % 2:
        row_layout.append(1)
    row_layout.append(1)

    if has_thumbnail:
        builder.button(
            text="🖼 Скачать обложку / Пин",
            callback_data=MediaCallback(url_hash=url_hash, action="cover"),
        )
        row_layout.append(1)

    builder.adjust(*row_layout)
    return builder.as_markup()

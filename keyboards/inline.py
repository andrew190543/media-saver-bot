from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class MediaCallback(CallbackData, prefix="dl"):
    url_hash: str
    action: str


def build_media_keyboard(
    url_hash: str,
    is_oversized: bool = False,
    is_gallery: bool = False,
    has_thumbnail: bool = True,
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

    if not is_oversized:
        builder.button(
            text="🎬 Видео: Лучшее",
            callback_data=MediaCallback(url_hash=url_hash, action="best"),
        )
        builder.button(
            text="🎬 Видео: 720p",
            callback_data=MediaCallback(url_hash=url_hash, action="720p"),
        )
    else:
        builder.button(
            text="🎬 Видео: 480p (Эконом)",
            callback_data=MediaCallback(url_hash=url_hash, action="480p"),
        )
        builder.button(
            text="🗜 Сжать видео до 48 МБ (FFmpeg)",
            callback_data=MediaCallback(url_hash=url_hash, action="compress"),
        )

    builder.button(
        text="🎵 Аудио (MP3)",
        callback_data=MediaCallback(url_hash=url_hash, action="mp3"),
    )

    if has_thumbnail:
        builder.button(
            text="🖼 Скачать обложку / Пин",
            callback_data=MediaCallback(url_hash=url_hash, action="cover"),
        )

    builder.adjust(2, 1, 1)
    return builder.as_markup()

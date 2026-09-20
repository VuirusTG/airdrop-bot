from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def review_keyboard(
    project_id: int,
    previous_id: int | None = None,
    next_id: int | None = None,
    position: int | None = None,
    total: int | None = None,
    can_undo: bool = False,
) -> InlineKeyboardMarkup:
    counter = f"📋 {position}/{total}" if position and total else "📋 Review queue"
    rows = [
        [
            InlineKeyboardButton(
                text="◀️ Previous",
                callback_data=f"review_prev:{project_id}",
            ),
            InlineKeyboardButton(
                text=counter,
                callback_data=f"review_info:{project_id}",
            ),
            InlineKeyboardButton(
                text="Next ▶️",
                callback_data=f"review_next:{project_id}",
            ),
        ],
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{project_id}"),
            InlineKeyboardButton(text="🔁 Rework", callback_data=f"rework:{project_id}"),
            InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete:{project_id}"),
        ],
        [
            InlineKeyboardButton(
                text="🎨 Настроить фото",
                callback_data=f"studio_open:{project_id}",
            ),
            InlineKeyboardButton(
                text="🔄 Быстрый цвет",
                callback_data=f"regen_image:{project_id}",
            ),
        ],
    ]
    if can_undo:
        rows.append([
            InlineKeyboardButton(
                text="↩️ Отменить правку (Undo)",
                callback_data=f"undo_edit:{project_id}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_preview_keyboard(project_id: int, pending_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👁 Предпросмотр карточки",
                    callback_data=f"preview_edit:{project_id}:{pending_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Применить",
                    callback_data=f"apply_edit:{project_id}:{pending_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"cancel_edit:{project_id}:{pending_id}",
                ),
            ],
        ]
    )


def edit_confirm_keyboard(project_id: int, pending_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Применить",
                    callback_data=f"apply_edit:{project_id}:{pending_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"cancel_edit:{project_id}:{pending_id}",
                ),
            ],
        ]
    )


def photo_studio_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Сгенерировать арт ИИ",
                    callback_data=f"studio_regen_ai:{project_id}",
                ),
                InlineKeyboardButton(
                    text="🎭 Пресет стиля",
                    callback_data=f"studio_styles:{project_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌈 Сменить цвет",
                    callback_data=f"studio_colors:{project_id}",
                ),
                InlineKeyboardButton(
                    text="📝 Текст на карточке",
                    callback_data=f"studio_steps:{project_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📎 Загрузить своё фото",
                    callback_data=f"studio_upload:{project_id}",
                ),
                InlineKeyboardButton(
                    text="◀️ Назад к посту",
                    callback_data=f"studio_back:{project_id}",
                ),
            ],
        ]
    )


def studio_colors_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Неон Лайм", callback_data=f"set_color:{project_id}:lime"),
                InlineKeyboardButton(text="🔵 Кибер Циан", callback_data=f"set_color:{project_id}:cyan"),
            ],
            [
                InlineKeyboardButton(text="🟣 Фиолетовый", callback_data=f"set_color:{project_id}:violet"),
                InlineKeyboardButton(text="🟡 Золотой", callback_data=f"set_color:{project_id}:gold"),
            ],
            [
                InlineKeyboardButton(text="🔴 Красный", callback_data=f"set_color:{project_id}:red"),
                InlineKeyboardButton(text="🟠 Оранжевый", callback_data=f"set_color:{project_id}:orange"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад в Студию", callback_data=f"studio_open:{project_id}"),
            ],
        ]
    )


def studio_styles_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🥷 Kunoichi (Девушка)", callback_data=f"set_style:{project_id}:kunoichi"),
                InlineKeyboardButton(text="⚔️ Shinobi (Самурай)", callback_data=f"set_style:{project_id}:shinobi"),
            ],
            [
                InlineKeyboardButton(text="🏙 Cyber City (Город)", callback_data=f"set_style:{project_id}:cybercity"),
                InlineKeyboardButton(text="🌌 Quantum Portal", callback_data=f"set_style:{project_id}:portal"),
            ],
            [
                InlineKeyboardButton(text="💻 Matrix Terminal", callback_data=f"set_style:{project_id}:matrix"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад в Студию", callback_data=f"studio_open:{project_id}"),
            ],
        ]
    )


def upload_choice_keyboard(project_id: int, upload_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🖼 Сделать фоном карточки",
                    callback_data=f"apply_upload_bg:{project_id}:{upload_token}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📸 Заменить карточку целиком",
                    callback_data=f"apply_upload_full:{project_id}:{upload_token}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"studio_open:{project_id}",
                ),
            ],
        ]
    )


def open_in_x_keyboard(text: str) -> InlineKeyboardMarkup:
    intent_url = f"https://twitter.com/intent/tweet?text={quote(text, safe='')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🐦 Опубликовать в X / Twitter", url=intent_url)],
        ]
    )


def archive_keyboard(has_deleted: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if has_deleted:
        buttons.append([
            InlineKeyboardButton(text="🗑 Очистить архив", callback_data="archive_clear_deleted")
        ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="archive_refresh"),
        InlineKeyboardButton(text="📋 К очереди проверки", callback_data="archive_to_review"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


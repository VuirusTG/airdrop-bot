from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def review_keyboard(
    project_id: int,
    previous_id: int | None = None,
    next_id: int | None = None,
    position: int | None = None,
    total: int | None = None,
) -> InlineKeyboardMarkup:
    counter = f"📋 {position}/{total}" if position and total else "📋 Review queue"
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
                    text="🎨 Regenerate image",
                    callback_data=f"regen_image:{project_id}",
                )
            ],
        ]
    )


def open_in_x_keyboard(text: str) -> InlineKeyboardMarkup:
    intent_url = f"https://twitter.com/intent/tweet?text={quote(text, safe='')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open in X", url=intent_url)],
        ]
    )

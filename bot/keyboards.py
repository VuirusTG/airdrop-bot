from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def review_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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

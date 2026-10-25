"""Telegram UI Handlers for Content Series & X Threads Review and Navigation."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.keyboards import (
    series_card_keyboard,
    series_creation_keyboard,
    series_preview_keyboard,
)
from config import settings
from db.database import get_session
from db.models import ContentSeries, Project, SeriesPost, SeriesStatus
from publishing.series_publisher import SeriesPublisher
from services.editor_service import EditPlan
from services.series_editor import SeriesEditor
from services.series_engine import SeriesEngine
from services.series_models import (
    SeriesData,
    SeriesFormat,
    series_db_to_data,
    sync_data_to_series_db,
)
from services.series_version_manager import SeriesVersionManager

logger = logging.getLogger(__name__)

router = Router()

awaiting_series_edit: dict[int, dict[str, Any]] = {}
awaiting_series_add: dict[int, dict[str, Any]] = {}
last_active_series: dict[int, int] = {}


async def _is_admin_message(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    if not user_id:
        return False
    if settings.ADMIN_USER_ID and user_id != settings.ADMIN_USER_ID:
        try:
            await message.answer(f"⛔ Доступ запрещён. Ваш ID: {user_id}")
        except Exception:
            pass
        return False
    return True


async def _is_admin_callback(callback: CallbackQuery) -> bool:
    user_id = callback.from_user.id if callback.from_user else 0
    if not user_id:
        return False
    if settings.ADMIN_USER_ID and user_id != settings.ADMIN_USER_ID:
        try:
            await callback.answer(f"Нет доступа. Ваш ID: {user_id}", show_alert=True)
        except Exception:
            pass
        return False
    return True


async def _load_series(session: AsyncSession, series_id: int) -> ContentSeries | None:
    res = await session.execute(
        select(ContentSeries)
        .options(selectinload(ContentSeries.posts))
        .where(ContentSeries.id == series_id)
    )
    return res.scalar_one_or_none()


async def _send_or_edit_series_card(
    target: CallbackQuery | Message,
    series_id: int,
    position: int = 1,
) -> None:
    """Render and display the single-card series navigation view."""
    async with get_session() as session:
        series = await _load_series(session, series_id)
        if not series or not series.posts:
            if isinstance(target, CallbackQuery) or hasattr(target, "data"):
                await target.answer("Серия не найдена или пуста.", show_alert=True)
            else:
                await target.answer("Серия не найдена или пуста.")
            return

        can_undo = await SeriesVersionManager.can_undo(session, series_id)
        can_redo = await SeriesVersionManager.can_redo(session, series_id)
        series_data = series_db_to_data(series)

    position = max(1, min(position, series_data.total_posts))
    text = series_data.format_single_post_view(position)
    keyboard = series_card_keyboard(
        series_id=series_id,
        position=position,
        total=series_data.total_posts,
        can_undo=can_undo,
        can_redo=can_redo,
    )

    user_id = target.from_user.id if target.from_user else 0
    if user_id:
        last_active_series[user_id] = series_id

    is_callback = isinstance(target, CallbackQuery) or (
        hasattr(target, "data") and getattr(target, "data", None) is not None and not hasattr(target, "chat")
    )

    if is_callback:
        if getattr(target, "message", None):
            try:
                await target.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await target.message.answer(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------
# CALLBACKS: SERIES INITIATION
# ---------------------------------------------------------

@router.callback_query(F.data.startswith("series_init:"))
async def on_series_init(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    project_id = int(parts[1])
    format_type = parts[2] if len(parts) > 2 else SeriesFormat.X_THREAD

    title_fmt = "X/Twitter тред" if format_type == SeriesFormat.X_THREAD else "серию постов Telegram"
    kb = series_creation_keyboard(project_id, format_type)
    msg_text = (
        f"🧵 <b>Создание серии: {title_fmt}</b>\n\n"
        f"Выберите желаемое количество постов:\n"
        f"• <b>3 поста</b> — короткий фокус (Hook → Key Points → CTA)\n"
        f"• <b>5 постов</b> — стандартный разбор airdrop проекта\n"
        f"• <b>7 постов</b> — подробный гайд со всеми шагами и активностями"
    )
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("series_cancel_init:"))
async def on_series_cancel_init(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    from bot.handlers.admin_review import _replace_review_message
    await _replace_review_message(callback, project_id)


@router.callback_query(F.data.startswith("series_gen:"))
async def on_series_generate(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    project_id = int(parts[1])
    format_type = parts[2]
    total_posts = int(parts[3])

    await callback.answer("⏳ Генерирую серию с ИИ (планирование + написание)...", show_alert=False)
    if callback.message:
        await callback.message.edit_text("⏳ <i>ИИ разрабатывает логическую структуру и пишет серию постов...</i>", parse_mode="HTML")

    async with get_session() as session:
        proj_res = await session.execute(
            select(Project).options(selectinload(Project.drafts)).where(Project.id == project_id)
        )
        project = proj_res.scalar_one_or_none()
        draft = project.latest_draft() if project else None

        series_data = await SeriesEngine.generate_series(
            project=project,
            draft=draft,
            format_type=format_type,
            desired_posts_count=total_posts,
        )

        # Persist to database
        db_series = ContentSeries(
            project_id=project.id if project else None,
            source_draft_id=draft.id if draft else None,
            format=series_data.format,
            title=series_data.title,
            topic=series_data.topic,
            total_posts=series_data.total_posts,
            plan_json=series_data.plan.to_json() if series_data.plan else None,
            status=SeriesStatus.DRAFT,
            current_version=1,
        )
        session.add(db_series)
        await session.flush()

        for post_data in series_data.posts:
            db_post = SeriesPost(
                series_id=db_series.id,
                position=post_data.position,
                purpose=post_data.purpose,
                content=post_data.content,
                media_required=post_data.media_required,
                media_type=post_data.media_type,
            )
            session.add(db_post)
        await session.flush()

        # Save initial version snapshot
        series_data.id = db_series.id
        await SeriesVersionManager.save_snapshot(
            session=session,
            series_id=db_series.id,
            action="Начальная генерация серии",
            user_command=f"generate format={format_type} posts={total_posts}",
            series_data=series_data,
        )
        await session.commit()
        series_id = db_series.id

    await _send_or_edit_series_card(callback, series_id=series_id, position=1)


# ---------------------------------------------------------
# CALLBACKS: CARD NAVIGATION & PREVIEW
# ---------------------------------------------------------

@router.callback_query(F.data.startswith("series_nav:"))
async def on_series_nav(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2])
    await _send_or_edit_series_card(callback, series_id=series_id, position=position)


@router.callback_query(F.data == "series_edge:first")
async def on_series_edge_first(callback: CallbackQuery):
    await callback.answer("Это первый пост в серии.")


@router.callback_query(F.data == "series_edge:last")
async def on_series_edge_last(callback: CallbackQuery):
    await callback.answer("Это последний пост в серии.")


@router.callback_query(F.data.startswith("series_info:"))
async def on_series_info(callback: CallbackQuery):
    parts = callback.data.split(":")
    position = parts[2] if len(parts) > 2 else "1"
    await callback.answer(f"Позиция {position}")


@router.callback_query(F.data.startswith("series_full_preview:"))
async def on_series_full_preview(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    return_pos = int(parts[2]) if len(parts) > 2 else 1

    async with get_session() as session:
        series = await _load_series(session, series_id)
        if not series:
            await callback.answer("Серия не найдена.", show_alert=True)
            return
        series_data = series_db_to_data(series)

    preview_text = series_data.format_full_preview()
    keyboard = series_preview_keyboard(series_id=series_id, return_position=return_pos)

    if callback.message:
        try:
            await callback.message.edit_text(preview_text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await callback.message.answer(preview_text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()


# ---------------------------------------------------------
# CALLBACKS: POST EDITING, REGENERATION, DELETE & ADD
# ---------------------------------------------------------

@router.callback_query(F.data.startswith("series_edit_post:"))
async def on_series_edit_post_button(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2])

    user_id = callback.from_user.id if callback.from_user else 0
    awaiting_series_edit[user_id] = {"series_id": series_id, "position": position}
    last_active_series[user_id] = series_id

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"✏️ <b>Редактирование поста #{position}:</b>\n\n"
            f"Отправьте сообщением в чат:\n"
            f"1. <b>Новый текст поста</b> (заменит текущий текст целиком)\n"
            f"ИЛИ\n"
            f"2. <b>Инструкцию</b> (например: «Сделай короче», «Добавь ссылку на гайд», «Убери упоминание цены»).\n\n"
            f"<i>Для отмены отправьте /cancel</i>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("series_regen_post:"))
async def on_series_regen_post(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2])

    await callback.answer("🔄 Перегенерирую пост...")

    async with get_session() as session:
        series = await _load_series(session, series_id)
        if not series:
            return
        series_data = series_db_to_data(series)

        plan = EditPlan(
            scope="post",
            position=position,
            target="post",
            operation="regenerate",
            explanation=f"Перегенерация поста #{position}",
        )
        updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        if not ok:
            await callback.answer(f"Ошибка: {err}", show_alert=True)
            return

        sync_data_to_series_db(updated_data, series, session)
        await SeriesVersionManager.save_snapshot(
            session=session,
            series_id=series_id,
            action=f"Перегенерация поста #{position}",
            user_command="regenerate",
            series_data=updated_data,
        )
        await session.commit()

    await _send_or_edit_series_card(callback, series_id=series_id, position=position)


@router.callback_query(F.data.startswith("series_del_post:"))
async def on_series_del_post(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2])

    async with get_session() as session:
        series = await _load_series(session, series_id)
        if not series:
            return
        series_data = series_db_to_data(series)

        plan = EditPlan(
            scope="post",
            position=position,
            target="post",
            operation="remove",
            explanation=f"Удаление поста #{position}",
        )
        updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        if not ok:
            await callback.answer(f"Ошибка: {err}", show_alert=True)
            return

        sync_data_to_series_db(updated_data, series, session)
        await SeriesVersionManager.save_snapshot(
            session=session,
            series_id=series_id,
            action=f"Удаление поста #{position}",
            user_command=f"delete post {position}",
            series_data=updated_data,
        )
        await session.commit()
        new_total = updated_data.total_posts

    next_pos = min(position, new_total)
    await callback.answer(f"Пост #{position} удалён.")
    await _send_or_edit_series_card(callback, series_id=series_id, position=next_pos)


@router.callback_query(F.data.startswith("series_add_post:"))
async def on_series_add_post_button(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2])

    user_id = callback.from_user.id if callback.from_user else 0
    # Add after current position
    insert_pos = position + 1
    awaiting_series_add[user_id] = {"series_id": series_id, "position": insert_pos}
    last_active_series[user_id] = series_id

    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"➕ <b>Добавление нового поста после #{position}:</b>\n\n"
            f"Отправьте текст нового поста в чат:\n"
            f"<i>Для отмены отправьте /cancel</i>",
            parse_mode="HTML",
        )


# ---------------------------------------------------------
# CALLBACKS: UNDO / REDO / SAVE / PUBLISH / CLOSE
# ---------------------------------------------------------

@router.callback_query(F.data.startswith("series_undo:"))
async def on_series_undo(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2]) if len(parts) > 2 else 1

    async with get_session() as session:
        restored, msg = await SeriesVersionManager.undo(session, series_id)
        if not restored:
            await callback.answer(msg or "Нет предыдущих версий для Undo.", show_alert=True)
            return
        await session.commit()

    await callback.answer("↩️ Отмена выполнена (Undo).")
    await _send_or_edit_series_card(callback, series_id=series_id, position=position)


@router.callback_query(F.data.startswith("series_redo:"))
async def on_series_redo(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    series_id = int(parts[1])
    position = int(parts[2]) if len(parts) > 2 else 1

    async with get_session() as session:
        restored, msg = await SeriesVersionManager.redo(session, series_id)
        if not restored:
            await callback.answer(msg or "Нет версий для Redo.", show_alert=True)
            return
        await session.commit()

    await callback.answer("🔁 Повтор выполнен (Redo).")
    await _send_or_edit_series_card(callback, series_id=series_id, position=position)


@router.callback_query(F.data.startswith("series_save_draft:"))
async def on_series_save_draft(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    series_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        series = await _load_series(session, series_id)
        if series:
            series.status = SeriesStatus.DRAFT
            await session.commit()
    await callback.answer("💾 Серия сохранена как черновик.", show_alert=True)


@router.callback_query(F.data.startswith("series_close:"))
async def on_series_close(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    await callback.answer("Панель серии закрыта.")
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass


@router.callback_query(F.data.startswith("series_publish:"))
async def on_series_publish(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    series_id = int(callback.data.split(":")[1])
    await callback.answer("🚀 Запуск публикации серии...", show_alert=False)

    if callback.message:
        await callback.message.answer("🚀 <i>Публикация постов серии запущена...</i>", parse_mode="HTML")

    actual_bot = getattr(callback, "bot", None)
    if not actual_bot:
        try:
            from bot.main import bot as global_bot
            actual_bot = global_bot
        except Exception:
            actual_bot = None
    res = await SeriesPublisher.publish_series(series_id, bot=actual_bot)

    if res.success:
        report = (
            f"🎉 <b>Серия успешно опубликована!</b>\n\n"
            f"• Всего постов: {res.total_posts}\n"
            f"• Опубликовано: {res.published_count}\n"
            f"• Формат: <code>{res.format.upper()}</code>\n"
        )
        if res.first_x_post_id:
            report += f"\n🔗 <a href='https://x.com/i/web/status/{res.first_x_post_id}'>Ссылка на первый твит треда</a>"
    else:
        report = (
            f"⚠️ <b>Публикация завершилась с ошибками:</b>\n\n"
            f"• Опубликовано постов: {res.published_count}/{res.total_posts}\n"
            f"• Ошибка: <code>{html.escape(res.error_message or 'Неизвестная ошибка')}</code>\n\n"
            f"<i>Вы можете нажать «Опубликовать всю серию» снова для продолжения с места сбоя.</i>"
        )

    if callback.message:
        await callback.message.answer(report, parse_mode="HTML", disable_web_page_preview=False)


# ---------------------------------------------------------
# COMMANDS: /series, /threads
# ---------------------------------------------------------

@router.message(Command("series"))
@router.message(Command("threads"))
async def on_series_command(message: Message):
    if not await _is_admin_message(message):
        return
    async with get_session() as session:
        res = await session.execute(
            select(ContentSeries).order_by(ContentSeries.id.desc()).limit(10)
        )
        all_series = res.scalars().all()

    if not all_series:
        await message.answer(
            "ℹ️ <b>Нет сохранённых серий или тредов.</b>\n\n"
            "Вы можете создать тред или серию прямо из очереди проверки (/review), "
            "нажав кнопки <b>«🧵 Создать тред»</b> или <b>«📚 Создать серию»</b>.",
            parse_mode="HTML",
        )
        return

    lines = ["📚 <b>Список последних серий и тредов:</b>\n"]
    for s in all_series:
        fmt_icon = "🧵" if s.format == SeriesFormat.X_THREAD else "📚"
        status_icon = "✅" if s.status == SeriesStatus.PUBLISHED else ("⏳" if s.status == SeriesStatus.DRAFT else "⚠️")
        lines.append(f"{fmt_icon} #{s.id} — <b>{html.escape(s.title or s.topic)}</b> ({s.total_posts} постов) [{status_icon} {s.status}]")

    lines.append("\n<i>Чтобы открыть серию, отправьте команду:</i> <code>/series_open &lt;ID&gt;</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("series_open"))
async def on_series_open_cmd(message: Message):
    if not await _is_admin_message(message):
        return
    parts = message.text.split() if message.text else []
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Укажите ID серии, например: <code>/series_open 1</code>", parse_mode="HTML")
        return
    series_id = int(parts[1])
    await _send_or_edit_series_card(message, series_id=series_id, position=1)


# ---------------------------------------------------------
# TEXT INPUT & NATURAL LANGUAGE EDITING ROUTER
# ---------------------------------------------------------

@router.message(F.text, ~F.text.startswith("/"))
async def on_series_text_input(message: Message):
    """Handles text replies when awaiting post edit/add, or natural language thread editing commands."""
    user_id = message.from_user.id if message.from_user else 0
    if not user_id or not await _is_admin_message(message):
        return

    # 1. Check if user is in awaiting_series_edit
    if user_id in awaiting_series_edit:
        ctx = awaiting_series_edit.pop(user_id)
        series_id = ctx["series_id"]
        position = ctx["position"]
        instruction = message.text.strip()

        async with get_session() as session:
            series = await _load_series(session, series_id)
            if not series:
                await message.answer("Серия не найдена.")
                return
            series_data = series_db_to_data(series)

            # Check if instruction is a full text replacement or modifier instruction
            is_full_text = len(instruction) > 40 and not any(w in instruction.lower() for w in ["сделай", "убери", "замени", "добавь", "короче"])
            op = "replace" if is_full_text else "rewrite"
            plan = EditPlan(
                scope="post",
                position=position,
                target="content",
                operation=op,
                new_value=instruction,
                explanation=f"Редактирование поста #{position}: {instruction[:40]}",
            )
            updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
            if not ok:
                await message.answer(f"⚠️ Ошибка: {err}")
                return

            sync_data_to_series_db(updated_data, series, session)
            await SeriesVersionManager.save_snapshot(
                session=session,
                series_id=series_id,
                action=f"Правка поста #{position}",
                user_command=instruction,
                series_data=updated_data,
            )
            await session.commit()

        await message.answer(f"✅ Пост #{position} обновлён!")
        await _send_or_edit_series_card(message, series_id=series_id, position=position)
        return

    # 2. Check if user is in awaiting_series_add
    if user_id in awaiting_series_add:
        ctx = awaiting_series_add.pop(user_id)
        series_id = ctx["series_id"]
        position = ctx["position"]
        new_text = message.text.strip()

        async with get_session() as session:
            series = await _load_series(session, series_id)
            if not series:
                await message.answer("Серия не найдена.")
                return
            series_data = series_db_to_data(series)

            plan = EditPlan(
                scope="post",
                position=position,
                target="post",
                operation="add",
                new_value=new_text,
                explanation=f"Добавление поста #{position}",
            )
            updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
            if not ok:
                await message.answer(f"⚠️ Ошибка: {err}")
                return

            sync_data_to_series_db(updated_data, series, session)
            await SeriesVersionManager.save_snapshot(
                session=session,
                series_id=series_id,
                action=f"Добавление поста #{position}",
                user_command=new_text[:40],
                series_data=updated_data,
            )
            await session.commit()

        await message.answer(f"✅ Новый пост добавлен на позицию #{position}!")
        await _send_or_edit_series_card(message, series_id=series_id, position=position)
        return

    # 3. Check for natural language commands targeting active series
    cmd_lower = message.text.strip().lower()
    has_series_keywords = any(kw in cmd_lower for kw in ["пост", "твит", "тред", "сери", "undo", "redo", "отмени", "верни"])
    if has_series_keywords and user_id in last_active_series:
        series_id = last_active_series[user_id]
        async with get_session() as session:
            series = await _load_series(session, series_id)
            if not series:
                return

            # Direct Undo / Redo in chat
            if cmd_lower in ("undo", "отмени", "отменить"):
                restored, msg = await SeriesVersionManager.undo(session, series_id)
                if restored:
                    await session.commit()
                    await message.answer("↩️ Undo выполнено!")
                    await _send_or_edit_series_card(message, series_id=series_id, position=1)
                else:
                    await message.answer(msg or "ℹ️ Нет версий для Undo.")
                return

            if cmd_lower in ("redo", "повтори", "повторить"):
                restored, msg = await SeriesVersionManager.redo(session, series_id)
                if restored:
                    await session.commit()
                    await message.answer("🔁 Redo выполнено!")
                    await _send_or_edit_series_card(message, series_id=series_id, position=1)
                else:
                    await message.answer(msg or "ℹ️ Нет версий для Redo.")
                return

            series_data = series_db_to_data(series)
            plan = await SeriesEditor.create_series_edit_plan(
                command=message.text,
                series=series_data,
            )

            updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
            if not ok:
                await message.answer(f"⚠️ Не удалось применить команду: {err}")
                return

            sync_data_to_series_db(updated_data, series, session)
            await SeriesVersionManager.save_snapshot(
                session=session,
                series_id=series_id,
                action=plan.explanation,
                user_command=message.text,
                series_data=updated_data,
            )
            await session.commit()

        target_pos = plan.position or 1
        await message.answer(f"✅ <b>Команда применена:</b> {html.escape(plan.explanation)}", parse_mode="HTML")
        await _send_or_edit_series_card(message, series_id=series_id, position=target_pos)
        return

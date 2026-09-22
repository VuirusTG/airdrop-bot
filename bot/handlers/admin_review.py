import asyncio
from dataclasses import asdict
import html
import json
import logging
import re
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from bot.keyboards import (
    archive_keyboard,
    edit_confirm_keyboard,
    edit_preview_keyboard,
    open_in_x_keyboard,
    photo_studio_keyboard,
    review_keyboard,
    studio_colors_keyboard,
    studio_styles_keyboard,
    upload_choice_keyboard,
)
from config import settings
from db.database import get_session
from db.models import Draft, DraftSnapshot, Project, ProjectStatus, PublishedPost
from ingestion.scheduler import source_scanner
from publishing.dispatcher import publish_project
from services.ai_rework import rework_draft
from services.artwork_generator import STYLE_PRESETS, generate_artwork
from services.draft_content import DraftContent, draft_to_content, sync_content_to_draft
from services.editor_service import EditorService, EditPlan
from services.image_rework import (
    build_ai_art_prompt,
    classify_rework_intent,
    detect_preset,
    detect_theme_color,
    requests_image_rework,
)
from services.llm_draft import DraftResult
from services.media import ensure_draft_image, telegram_photo
from services.project_image import discover_project_image
from services.project_link import discover_project_link
from services.social_card import THEME_COLORS, generate_social_card, render_social_card_from_content
from services.task_validator import validate_tasks
from services.version_manager import VersionManager
from services.health import collect_system_health

logger = logging.getLogger(__name__)

router = Router()
awaiting_feedback: dict[int, bool] = {}
awaiting_upload: dict[int, int] = {}
awaiting_steps: dict[int, int] = {}
uploaded_tokens: dict[str, str] = {}
pending_edits: dict[str, dict] = {}
_manual_scan_task: asyncio.Task | None = None


async def _is_admin_message(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    if not user_id:
        return False
    if settings.ADMIN_USER_ID and user_id != settings.ADMIN_USER_ID:
        logger.warning(
            "Rejected message from unauthorized user %s (ADMIN_USER_ID is %s)",
            user_id,
            settings.ADMIN_USER_ID,
        )
        try:
            await message.answer(
                f"⛔ <b>Доступ запрещён</b>\n\n"
                f"Ваш Telegram ID: <code>{user_id}</code>\n"
                f"ADMIN_USER_ID в боте: <code>{settings.ADMIN_USER_ID}</code>\n\n"
                f"Чтобы управлять ботом, укажите этот ID в переменной <code>ADMIN_USER_ID</code> на Render.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return False
    return True


async def _is_admin_callback(callback: CallbackQuery) -> bool:
    user_id = callback.from_user.id if callback.from_user else 0
    if not user_id:
        return False
    if settings.ADMIN_USER_ID and user_id != settings.ADMIN_USER_ID:
        logger.warning(
            "Rejected callback from unauthorized user %s (ADMIN_USER_ID is %s)",
            user_id,
            settings.ADMIN_USER_ID,
        )
        try:
            await callback.answer(f"Нет доступа. Ваш ID: {user_id}", show_alert=True)
        except Exception:
            pass
        return False
    return True



async def _load_project(session, project_id: int) -> Project | None:
    result = await session.execute(
        select(Project).options(selectinload(Project.drafts)).where(Project.id == project_id)
    )
    return result.scalar_one_or_none()


async def _review_queue(session) -> list[Project]:
    result = await session.execute(
        select(Project)
        .options(selectinload(Project.drafts))
        .where(Project.status == ProjectStatus.PENDING_REVIEW)
        .order_by(Project.id)
    )
    return list(result.scalars().all())


def _queue_meta(queue: list[Project], project_id: int) -> tuple[int, int, int | None, int | None]:
    ids = [project.id for project in queue]
    try:
        index = ids.index(project_id)
    except ValueError:
        return 1, max(len(queue), 1), None, None
    previous_id = ids[index - 1] if index > 0 else None
    next_id = ids[index + 1] if index + 1 < len(ids) else None
    return index + 1, len(ids), previous_id, next_id


def _review_caption(project: Project, draft: Draft, position: int, total: int) -> str:
    score = f"{project.legitimacy_score:.1f}/10" if project.legitimacy_score is not None else "n/a"
    header = f"🔎 REVIEW {position}/{total}  •  #{project.id}  •  {score}"

    source = draft.source_url or project.source_url or "Источник не указан"
    project_url = draft.project_url or project.project_url or "⚠️ Не найдена — публикация заблокирована"

    # Twitter draft formatted strictly <= 280 chars for free Twitter accounts
    tw_section = ""
    tw_text = ""
    if draft.twitter_text:
        tw_text = draft.twitter_text.strip()
        if len(tw_text) > 280:
            tw_text = tw_text[:279].rsplit(" ", 1)[0] + "…"
        tw_section = f"\n\n2. Черновик для твиттера\n\n{tw_text}"

    tg_header = "1. Черновик для телеграмм канала"
    tg_body = draft.rendered_text()

    meta_section = (
        f"{header}\n\n"
        f"🔒 Источник (виден только администратору):\n{source}\n\n"
        f"Ссылка на проект (попадет в публичные посты):\n{project_url}"
    )

    full_text = f"{meta_section}\n\n{tg_header}\n\n{tg_body}{tw_section}"
    if len(full_text) <= 1024:
        return full_text

    # Priority 1: Keep BOTH Telegram & Twitter by making metadata compact
    compact_meta = f"{header}\n🔗 {project_url}\n🔒 {source}"
    compact_with_tw = f"{compact_meta}\n\n{tg_header}\n\n{tg_body}{tw_section}"
    if len(compact_with_tw) <= 1024:
        return compact_with_tw

    # Priority 2: Minimal headers so both Telegram & Twitter fit
    if tw_text:
        minimal_with_tw = f"{header}\n🔗 {project_url}\n\n1. Telegram:\n{tg_body}\n\n2. Twitter (X):\n{tw_text}"
        if len(minimal_with_tw) <= 1024:
            return minimal_with_tw

    # Priority 3: If Telegram body alone is long, prioritize full Telegram post + compact meta
    compact_tg_only = f"{compact_meta}\n\n{tg_header}\n\n{tg_body}"
    if len(compact_tg_only) <= 1024:
        return compact_tg_only

    # Priority 4: Minimal header + Telegram post
    minimal_tg = f"{header}\n\n{tg_body}"
    if len(minimal_tg) <= 1024:
        return minimal_tg

    # Priority 5: Full Telegram body directly
    if len(tg_body) <= 1024:
        return tg_body

    return tg_body[:1020].rsplit("\n", 1)[0]



async def _replace_review_message(
    callback: CallbackQuery,
    project_id: int,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Keep one stable review card, including media/text type changes."""
    if not callback.message:
        return
    async with get_session() as session:
        queue = await _review_queue(session)
        project = next((item for item in queue if item.id == project_id), None)
        if not project or not project.latest_draft():
            return
        draft = project.latest_draft()
        if draft.image_path:
            await ensure_draft_image(project, draft)
            await session.commit()

        position, total, previous_id, next_id = _queue_meta(queue, project.id)
        if keyboard is None:
            can_undo = await VersionManager.can_undo(session, draft.id)
            keyboard = review_keyboard(project.id, previous_id, next_id, position, total, can_undo=can_undo)

        photo_obj = telegram_photo(draft.image_path) if draft.image_path else None
        current_has_photo = bool(callback.message.photo)
        desired_has_photo = photo_obj is not None
        caption = _review_caption(project, draft, position, total)

        sent = callback.message
        try:
            if desired_has_photo and current_has_photo:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=photo_obj,
                        caption=caption,
                    ),
                    reply_markup=keyboard,
                )
            elif not desired_has_photo and not current_has_photo:
                await callback.message.edit_text(
                    caption,
                    reply_markup=keyboard,
                )
            else:
                # Type changed between text and photo -> send new, delete old
                if desired_has_photo:
                    sent = await callback.bot.send_photo(
                        chat_id=callback.message.chat.id,
                        photo=photo_obj,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                else:
                    sent = await callback.bot.send_message(
                        chat_id=callback.message.chat.id,
                        text=caption,
                        reply_markup=keyboard,
                    )
                try:
                    await callback.message.delete()
                except Exception:
                    pass
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("Could not edit review message (%s); replacing: %s", project_id, exc)
            if desired_has_photo:
                sent = await callback.bot.send_photo(
                    chat_id=callback.message.chat.id,
                    photo=photo_obj,
                    caption=caption,
                    reply_markup=keyboard,
                )
            else:
                sent = await callback.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=caption,
                    reply_markup=keyboard,
                )
            try:
                await callback.message.delete()
            except Exception:
                pass

        project.review_chat_id = sent.chat.id
        project.review_message_id = sent.message_id
        await session.commit()


async def _show_review_project(callback: CallbackQuery, project_id: int) -> bool:
    """Replace the current review card with another pending project."""
    if not callback.message:
        return False
    async with get_session() as session:
        queue = await _review_queue(session)
        project = next((item for item in queue if item.id == project_id), None)
        if not project or not project.latest_draft():
            return False
    try:
        await _replace_review_message(callback, project_id)
    except Exception as exc:
        logger.exception("Could not show review project #%s: %s", project_id, exc)
        return False
    return True


async def _open_review_queue(message: Message) -> None:
    async with get_session() as session:
        queue = await _review_queue(session)
        if not queue:
            await message.answer("📭 Очередь черновиков пуста.")
            return
        project = queue[0]
        draft = project.latest_draft()
        if not draft:
            await message.answer("В очереди найден проект без черновика.")
            return
        if draft.image_path:
            try:
                await ensure_draft_image(project, draft)
                await session.commit()
            except Exception as exc:
                logger.warning("ensure_draft_image failed for project #%s: %s", project.id, exc)

        position, total, previous_id, next_id = _queue_meta(queue, project.id)
        can_undo = await VersionManager.can_undo(session, draft.id)
        keyboard = review_keyboard(project.id, previous_id, next_id, position, total, can_undo=can_undo)
        caption = _review_caption(project, draft, position, total)
        sent = None
        photo_input = telegram_photo(draft.image_path) if draft.image_path else None
        if photo_input:
            try:
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=photo_input,
                    caption=caption,
                    reply_markup=keyboard,
                )
            except Exception as exc:
                logger.warning("Failed to send review photo for project #%s (%s): %s; falling back to text", project.id, draft.image_path, exc)
                sent = await message.answer(caption, reply_markup=keyboard)
        else:
            sent = await message.answer(caption, reply_markup=keyboard)

        project.review_chat_id = sent.chat.id
        project.review_message_id = sent.message_id
        await session.commit()


@router.message(Command("start"))
@router.message(Command("help"))
async def on_start_help(message: Message):
    if not await _is_admin_message(message):
        return
    text = (
        "🤖 <b>Airdrop Review Bot</b>\n\n"
        "Доступные команды:\n"
        "• /review — открыть очередь черновиков на модерацию\n"
        "• /archive — открыть архив опубликованных и удалённых проектов\n"
        "• /scan_now — запустить немедленное сканирование источников\n"
        "• /status — проверить статус подключений и AI-провайдеров\n\n"
        "В очереди черновиков вы можете:\n"
        "• <b>✅ Approve</b> — опубликовать пост в Telegram и Twitter/X\n"
        "• <b>🔁 Rework</b> — отправить правки (по тексту или внешнему виду)\n"
        "• <b>🗑 Delete</b> — удалить проект из очереди\n"
        "• <b>🎨 Regenerate image</b> — перегенерировать social card"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("review"))
async def on_review(message: Message):
    if not await _is_admin_message(message):
        return
    try:
        await _open_review_queue(message)
    except Exception as exc:
        logger.exception("Error in on_review: %s", exc)
        await message.answer(f"⚠️ Ошибка при открытии очереди: {exc}")


@router.callback_query(F.data.startswith("review_info:"))
async def on_review_info(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    async with get_session() as session:
        queue = await _review_queue(session)
        project_id = int(callback.data.split(":")[1])
        position, total, _, _ = _queue_meta(queue, project_id)
    await callback.answer(f"Черновик {position} из {total} в очереди.")


@router.callback_query(F.data.startswith("review_prev:"))
async def on_review_prev(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    current_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        queue = await _review_queue(session)
        position, _, previous_id, _ = _queue_meta(queue, current_id)
    if not previous_id:
        await callback.answer("Это первый черновик.")
        return
    if await _show_review_project(callback, previous_id):
        await callback.answer()
    else:
        await callback.answer("Черновик больше недоступен.", show_alert=True)


@router.callback_query(F.data.startswith("review_next:"))
async def on_review_next(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    current_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        queue = await _review_queue(session)
        _, _, _, next_id = _queue_meta(queue, current_id)
    if not next_id:
        await callback.answer("Это последний черновик.")
        return
    if await _show_review_project(callback, next_id):
        await callback.answer()
    else:
        await callback.answer("Черновик больше недоступен.", show_alert=True)


async def _show_next_or_empty(callback: CallbackQuery, excluded_id: int) -> None:
    if not callback.message:
        return
    async with get_session() as session:
        queue = [project for project in await _review_queue(session) if project.id != excluded_id]
        project = queue[0] if queue else None
        draft = project.latest_draft() if project else None

    if project and draft:
        try:
            await _replace_review_message(callback, project.id)
        except Exception:
            await callback.message.answer("Не удалось открыть следующий черновик. Используйте /review.")
        return

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption="📭 Очередь черновиков пуста.", reply_markup=None)
        else:
            await callback.message.edit_text("📭 Очередь черновиков пуста.", reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return

    project_id = int(callback.data.split(":")[1])
    telegram_success = False
    twitter_text: str | None = None
    image_path: str | None = None

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            await callback.answer("Проект или черновик не найден.", show_alert=True)
            return
        if project.status == ProjectStatus.PUBLISHED:
            await callback.answer("Этот проект уже опубликован.", show_alert=True)
            return
        if project.status == ProjectStatus.APPROVED:
            await callback.answer("Публикация уже выполняется.", show_alert=True)
            return
        if not project.latest_draft().project_url:
            await callback.answer(
                "Ссылка на проект не найдена. Публикация заблокирована, чтобы не отправить неверный URL.",
                show_alert=True,
            )
            return

        latest_draft = project.latest_draft()
        if latest_draft and (latest_draft.image_path or settings.ENABLE_SOCIAL_CARD_GENERATION):
            try:
                await ensure_draft_image(project, latest_draft)
                await session.commit()
            except Exception as exc:
                logger.warning("ensure_draft_image failed on approve for project #%s: %s", project.id, exc)
        twitter_text = latest_draft.twitter_text if latest_draft else None
        image_path = latest_draft.image_path if latest_draft else None

        project.status = ProjectStatus.APPROVED
        await session.commit()
        results = await publish_project(callback.bot, project, latest_draft)
        telegram_result = next(result for result in results if result.platform == "telegram")
        telegram_success = telegram_result.success
        project.status = ProjectStatus.PUBLISHED if telegram_success else ProjectStatus.APPROVED
        await session.commit()

        if not telegram_success and callback.message:
            lines = ["Результат публикации:"]
            for result in results:
                marker = "✅" if result.success else "❌"
                detail = result.url or result.error or "готово"
                lines.append(f"{marker} {result.platform}: {detail}")
            x_result = next(result for result in results if result.platform == "x")
            fallback_keyboard = (
                open_in_x_keyboard(latest_draft.twitter_text)
                if not x_result.success and latest_draft.twitter_text
                else None
            )
            await callback.message.answer("\n".join(lines), reply_markup=fallback_keyboard)

    if telegram_success:
        if twitter_text and callback.message:
            tw_text = twitter_text.strip()
            if len(tw_text) > 280:
                tw_text = tw_text[:279].rsplit(" ", 1)[0] + "…"
            tw_keyboard = open_in_x_keyboard(tw_text)
            tw_caption = (
                "🐦 <b>Пост опубликован в Telegram!</b>\n\n"
                "Черновик для публикации в X / Twitter:\n\n"
                f"{tw_text}\n\n"
                "👆 Сохраните фото выше и нажмите кнопку ниже, чтобы открыть Twitter с готовым текстом."
            )
            curr_img = latest_draft.image_path if latest_draft else image_path
            photo_to_send = telegram_photo(curr_img) if curr_img else None
            if photo_to_send:
                try:
                    await callback.bot.send_photo(
                        chat_id=callback.message.chat.id,
                        photo=photo_to_send,
                        caption=tw_caption,
                        parse_mode="HTML",
                        reply_markup=tw_keyboard,
                    )
                except Exception as exc:
                    logger.warning("Could not send Twitter photo handoff: %s", exc)
                    await callback.message.answer(
                        f"🐦 Черновик для X / Twitter:\n\n{tw_text}",
                        reply_markup=tw_keyboard,
                    )
            else:
                await callback.message.answer(
                    f"🐦 Черновик для X / Twitter:\n\n{tw_text}",
                    reply_markup=tw_keyboard,
                )

        await _show_next_or_empty(callback, project_id)
        await callback.answer("Опубликовано в Telegram.")
    else:
        await callback.answer("Telegram не опубликовал пост. Смотрите ошибку выше.", show_alert=True)


@router.callback_query(F.data.startswith("delete:"))
async def on_delete(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project:
            await callback.answer("Проект не найден.", show_alert=True)
            return
        project.status = ProjectStatus.DELETED
        await session.commit()
    await _show_next_or_empty(callback, project_id)
    await callback.answer("Удалено.")


@router.callback_query(F.data.startswith("rework:"))
async def on_rework(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    awaiting_feedback[project_id] = True
    if callback.message:
        await callback.message.reply(
            "Ответьте на это сообщение и напишите, что изменить в черновике (по тексту или по картинке):\n"
            f"(project #{project_id})"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("regen_image:"))
async def on_regenerate_image(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.answer("Генерирую новую картинку...")

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            if callback.message:
                await callback.message.answer("Проект или черновик не найден.")
            return
        previous_draft = project.latest_draft()
        new_version = previous_draft.version + 1
        official_image = await discover_project_image(project.source_url)
        variant_themes = ["", "синий", "фиолетовый", "красный", "золотой"]
        theme = variant_themes[new_version % len(variant_themes)]
        regeneration_prompt = theme or previous_draft.image_prompt or ""
        loop_time = int(asyncio.get_event_loop().time())
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=official_image.url if official_image else None,
            image_prompt=regeneration_prompt,
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}-{loop_time}",
            potential_reward=previous_draft.potential_reward,
        )
        if not social_card:
            if callback.message:
                await callback.message.answer(
                    "Не удалось создать новую картинку. Предыдущая версия сохранена.",
                    reply_markup=review_keyboard(project.id),
                )
            return

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=social_card.path,
            image_source=social_card.source,
            image_prompt=regeneration_prompt,
            source_url=previous_draft.source_url or project.source_url,
            project_url=previous_draft.project_url or project.project_url,
            rework_feedback="Regenerate image button",
        )
        project.drafts.append(new_draft)
        await session.commit()
        if callback.message:
            try:
                await _replace_review_message(callback, project.id)
            except Exception as exc:
                logger.exception("Could not replace review card after image regeneration: %s", exc)
                await callback.message.answer("Изображение создано, но карточку не удалось обновить. Используйте /review.")


@router.callback_query(F.data.startswith("studio_open:"))
async def on_studio_open(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=photo_studio_keyboard(project_id)
        )
    await callback.answer("🎨 Студия карточки")


@router.callback_query(F.data.startswith("studio_back:"))
async def on_studio_back(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    await _replace_review_message(callback, project_id)
    await callback.answer()


@router.callback_query(F.data.startswith("studio_colors:"))
async def on_studio_colors(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=studio_colors_keyboard(project_id)
        )
    await callback.answer("Выберите цвет")


@router.callback_query(F.data.startswith("set_color:"))
async def on_set_color(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    _, pid_str, color_name = callback.data.split(":")
    project_id = int(pid_str)
    await callback.answer(f"Применяю цвет: {color_name}...")

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            return
        previous_draft = project.latest_draft()
        new_version = previous_draft.version + 1
        loop_time = int(asyncio.get_event_loop().time())
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=None,
            image_prompt=previous_draft.image_prompt,
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}-{loop_time}",
            potential_reward=previous_draft.potential_reward,
            theme_color=color_name,
        )
        if not social_card:
            await callback.answer("Ошибка смены цвета", show_alert=True)
            return

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=social_card.path,
            image_source=social_card.source,
            image_prompt=f"color:{color_name}",
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=f"Set color {color_name}",
        )
        project.drafts.append(new_draft)
        await session.commit()

    await _replace_review_message(callback, project_id, keyboard=studio_colors_keyboard(project_id))


@router.callback_query(F.data.startswith("studio_styles:"))
async def on_studio_styles(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=studio_styles_keyboard(project_id)
        )
    await callback.answer("Выберите стиль фона")


@router.callback_query(F.data.startswith("set_style:"))
async def on_set_style(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    _, pid_str, preset_name = callback.data.split(":")
    project_id = int(pid_str)
    await callback.answer(f"Генерирую стиль {preset_name}...")

    preset_info = STYLE_PRESETS.get(preset_name, {})
    preset_theme = preset_info.get("theme_color", "lime")
    seed = int(asyncio.get_event_loop().time()) % 100000
    art_path, provider = await generate_artwork(preset=preset_name, seed=seed)

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            return
        previous_draft = project.latest_draft()
        new_version = previous_draft.version + 1
        loop_time = int(asyncio.get_event_loop().time())
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=None,
            image_prompt=f"preset:{preset_name}",
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}-{loop_time}",
            potential_reward=previous_draft.potential_reward,
            custom_artwork_path=art_path,
            theme_color=preset_theme,
        )
        if not social_card:
            await callback.answer("Не удалось применить стиль", show_alert=True)
            return

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=social_card.path,
            image_source=f"preset_{preset_name}_{provider}",
            image_prompt=f"preset:{preset_name}",
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=f"Preset {preset_name}",
        )
        project.drafts.append(new_draft)
        await session.commit()

    await _replace_review_message(callback, project_id, keyboard=studio_styles_keyboard(project_id))


@router.callback_query(F.data.startswith("studio_regen_ai:"))
async def on_studio_regen_ai(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.answer("🎨 ИИ генерирует новый фоновый арт...")

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            return
        previous_draft = project.latest_draft()

        art_prompt = f"Futuristic cyberpunk crypto artwork for {project.name}, blockchain ecosystem"
        seed = int(asyncio.get_event_loop().time() * 10) % 999999
        art_path, provider = await generate_artwork(prompt=art_prompt, seed=seed)

        new_version = previous_draft.version + 1
        loop_time = int(asyncio.get_event_loop().time())
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=None,
            image_prompt=art_prompt,
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}-{loop_time}",
            potential_reward=previous_draft.potential_reward,
            custom_artwork_path=art_path,
        )
        if not social_card:
            await callback.answer("Ошибка генерации ИИ", show_alert=True)
            return

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=social_card.path,
            image_source=f"ai_{provider}",
            image_prompt=art_prompt,
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback="AI Art Regeneration",
        )
        project.drafts.append(new_draft)
        await session.commit()

    await _replace_review_message(callback, project_id, keyboard=photo_studio_keyboard(project_id))


@router.callback_query(F.data.startswith("studio_upload:"))
async def on_studio_upload(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    awaiting_upload[callback.from_user.id] = project_id
    if callback.message:
        await callback.message.reply(
            f"📎 <b>Загрузка своего фото для проекта #{project_id}</b>\n\n"
            "Отправьте фотографию или баннер прямо в этот чат (можно в ответ на это сообщение).\n\n"
            "После отправки появится выбор: сделать фото фоном карточки (с наложением текста) или заменить карточку целиком.",
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("studio_steps:"))
async def on_studio_steps(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    awaiting_steps[callback.from_user.id] = project_id
    if callback.message:
        await callback.message.reply(
            f"📝 <b>Редактирование шагов на карточке (project #{project_id})</b>\n\n"
            "Отправьте в ответ 3 шага для карточки (каждый с новой строки):\n\n"
            "<code>1. Visit official testnet bridge\n2. Swap tokens on DEX\n3. Mint verified badge</code>\n\n"
            "<i>(Рекомендуется использовать короткие ёмкие фразы на английском)</i>",
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("apply_upload_bg:"))
async def on_apply_upload_bg(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    _, pid_str, token = callback.data.split(":")
    project_id = int(pid_str)
    img_path = uploaded_tokens.get(token)
    if not img_path or not Path(img_path).is_file():
        await callback.answer("Фото устарело или не найдено. Отправьте заново.", show_alert=True)
        return

    await callback.answer("Применяю как фон карточки...")
    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            return
        previous_draft = project.latest_draft()
        new_version = previous_draft.version + 1
        loop_time = int(asyncio.get_event_loop().time())
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=None,
            image_prompt="user_custom_background",
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}-{loop_time}",
            potential_reward=previous_draft.potential_reward,
            custom_artwork_path=img_path,
        )
        if not social_card:
            await callback.answer("Ошибка генерации карточки из фото", show_alert=True)
            return

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=social_card.path,
            image_source="user_upload_card",
            image_prompt="user_custom_background",
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback="User uploaded custom card background",
        )
        project.drafts.append(new_draft)
        await session.commit()

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await _replace_review_message(callback, project_id)


@router.callback_query(F.data.startswith("apply_upload_full:"))
async def on_apply_upload_full(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    _, pid_str, token = callback.data.split(":")
    project_id = int(pid_str)
    img_path = uploaded_tokens.get(token)
    if not img_path or not Path(img_path).is_file():
        await callback.answer("Фото устарело или не найдено. Отправьте заново.", show_alert=True)
        return

    await callback.answer("Карточка заменена на ваше фото!")
    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            return
        previous_draft = project.latest_draft()
        new_version = previous_draft.version + 1

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=img_path,
            image_source="user_upload_direct",
            image_prompt="user_upload_direct",
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback="User uploaded direct image replacement",
        )
        project.drafts.append(new_draft)
        await session.commit()

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await _replace_review_message(callback, project_id)


@router.message(F.photo)
async def on_photo_message(message: Message):
    if not await _is_admin_message(message):
        return
    user_id = message.from_user.id if message.from_user else 0
    project_id = awaiting_upload.pop(user_id, None)

    if not project_id and message.reply_to_message:
        txt = message.reply_to_message.text or message.reply_to_message.caption or ""
        if "(project #" in txt:
            try:
                project_id = int(txt.split("(project #")[1].split(")")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "#" in txt:
            m = re.search(r"#(\d+)", txt)
            if m:
                project_id = int(m.group(1))

    if not project_id:
        async with get_session() as session:
            queue = await _review_queue(session)
            if queue:
                project_id = queue[0].id

    if not project_id:
        await message.answer("Не удалось определить проект для этой фотографии. Нажмите «Загрузить своё фото» в карточке.")
        return

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    if not file.file_path:
        await message.answer("Не удалось скачать фотографию из Telegram.")
        return

    import secrets
    token = secrets.token_hex(8)
    upload_dir = Path("images/user_uploads").resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest_path = upload_dir / f"upload_{project_id}_{token}.jpg"

    await message.bot.download_file(file.file_path, destination=dest_path)
    uploaded_tokens[token] = str(dest_path)

    await message.answer(
        f"📸 <b>Фото получено для проекта #{project_id}!</b>\n\n"
        "Как вы хотите его использовать?",
        parse_mode="HTML",
        reply_markup=upload_choice_keyboard(project_id, token),
    )


async def _execute_and_apply_plan(
    target_msg: Message,
    session,
    project: Project,
    draft: Draft,
    content: DraftContent,
    plan: EditPlan,
    user_command: str,
) -> None:
    updated_content, is_valid, err = await EditorService.apply_edit_plan(plan, content)
    if not is_valid:
        await target_msg.answer(
            f"❌ <b>Не удалось применить изменения:</b>\n{html.escape(err or '')}\n\nЧерновик оставлен без изменений.",
            parse_mode="HTML",
        )
        return

    # Handle image operations according to level
    color = detect_theme_color(user_command) or (updated_content.artwork.theme_color if updated_content.artwork else None)
    if color:
        updated_content.artwork.theme_color = color

    if plan.image_operation in ("generate_artwork", "new_artwork"):
        preset = detect_preset(user_command)
        art_prompt = build_ai_art_prompt(user_command, project.name)
        seed = int(asyncio.get_event_loop().time()) % 100000
        try:
            art_path, art_provider = await generate_artwork(
                prompt=art_prompt,
                preset=preset,
                seed=seed,
            )
            updated_content.artwork.path = art_path
            updated_content.artwork.source = art_provider
            updated_content.artwork.custom_artwork_path = art_path
        except Exception as exc:
            logger.warning("Failed to generate custom artwork: %s", exc)

        card = await render_social_card_from_content(updated_content)
        if card:
            updated_content.artwork.path = card.path
            updated_content.artwork.source = card.source
    elif plan.image_operation in ("rerender_text", "local_edit", "restyle") or color:
        card = await render_social_card_from_content(updated_content)
        if card:
            updated_content.artwork.path = card.path
            updated_content.artwork.source = card.source

    # Save changes to draft
    sync_content_to_draft(updated_content, draft)
    draft.version = draft.version + 1
    draft.rework_feedback = user_command
    plan_dict = asdict(plan)
    draft.edit_plan_json = json.dumps(plan_dict, ensure_ascii=False)

    # Save snapshot
    await VersionManager.save_snapshot(
        session,
        draft_id=draft.id,
        project_id=project.id,
        action=plan.explanation,
        user_command=user_command,
        edit_plan_json=draft.edit_plan_json,
        content=updated_content,
    )
    await session.commit()

    if draft.image_path:
        try:
            await ensure_draft_image(project, draft)
            await session.commit()
        except Exception:
            pass

    queue = await _review_queue(session)
    position, total, previous_id, next_id = _queue_meta(queue, project.id)
    can_undo = await VersionManager.can_undo(session, draft.id)
    keyboard = review_keyboard(project.id, previous_id, next_id, position, total, can_undo=can_undo)
    caption = _review_caption(project, draft, position, total)

    if project.review_message_id and project.review_chat_id:
        try:
            await target_msg.bot.delete_message(
                chat_id=project.review_chat_id,
                message_id=project.review_message_id,
            )
        except Exception:
            pass

    if target_msg.reply_to_message:
        try:
            await target_msg.reply_to_message.delete()
        except Exception:
            pass

    photo_to_send = telegram_photo(draft.image_path) if draft.image_path else None
    sent = None
    caption = _review_caption(project, draft, position, total)
    twitter_in_caption = bool(draft.twitter_text and draft.twitter_text.strip() in caption)

    if photo_to_send:
        try:
            sent = await target_msg.answer_photo(
                photo=photo_to_send,
                caption=caption,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.warning("Could not send updated photo card: %s; falling back to text", exc)
            sent = await target_msg.answer(caption, reply_markup=keyboard)
    else:
        sent = await target_msg.answer(caption, reply_markup=keyboard)

    project.review_chat_id = sent.chat.id
    project.review_message_id = sent.message_id
    await session.commit()

    if draft.twitter_text and not twitter_in_caption and photo_to_send:
        try:
            await target_msg.answer(
                f"🐦 <b>2. Черновик для Twitter (X):</b>\n\n{draft.twitter_text.strip()}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await target_msg.answer(
        f"✅ <b>Изменения применены:</b> {html.escape(plan.explanation)}\n"
        f"Сохранена версия v{draft.version}. При необходимости вы можете нажать «↩️ Отменить правку (Undo)».",
        parse_mode="HTML",
    )


@router.message(F.text, ~F.text.startswith("/"))
async def on_feedback_reply(message: Message):
    if not await _is_admin_message(message):
        return
    prompt_text = ""
    project_id = None
    if message.reply_to_message:
        prompt_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if "(project #" in prompt_text:
            try:
                project_id = int(prompt_text.split("(project #")[1].split(")")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "•  #" in prompt_text:
            try:
                project_id = int(prompt_text.split("•  #")[1].split("  •")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "#" in prompt_text:
            match = re.search(r"#(\d+)", prompt_text)
            if match:
                project_id = int(match.group(1))

    # If message is not an explicit reply, resolve project from awaiting_feedback or active queue
    if not project_id:
        if awaiting_feedback:
            project_id = next(iter(awaiting_feedback.keys()))
        else:
            async with get_session() as session:
                queue = await _review_queue(session)
                if queue:
                    project_id = queue[0].id

    if not project_id:
        await message.answer("ℹ️ Нет активного черновика для редактирования. Вызовите /review для просмотра очереди.")
        return

    awaiting_feedback.pop(project_id, None)
    feedback_text = message.text.strip()

    # Check if this reply is for card steps editing
    if "Редактирование шагов на карточке" in prompt_text:
        awaiting_steps.pop(message.from_user.id if message.from_user else 0, None)
        steps_lines = [line.strip() for line in feedback_text.splitlines() if line.strip()]
        if not steps_lines:
            await message.answer("Шаги не распознаны. Отправьте от 1 до 5 шагов, каждый с новой строки.")
            return

        async with get_session() as session:
            project = await _load_project(session, project_id)
            if not project or not project.latest_draft():
                return
            previous_draft = project.latest_draft()
            content = draft_to_content(previous_draft, project)

            is_valid, err, sanitized = validate_tasks(steps_lines[:5])
            if not is_valid:
                await message.answer(f"❌ Ошибка в шагах: {err}\nПопробуйте ещё раз.")
                return

            if not await VersionManager.can_undo(session, previous_draft.id):
                await VersionManager.save_snapshot(
                    session,
                    draft_id=previous_draft.id,
                    project_id=project.id,
                    action="Исходный черновик",
                    user_command=None,
                    edit_plan_json=None,
                    content=content,
                )

            content.tasks = sanitized
            card = await render_social_card_from_content(content)
            if card:
                content.artwork.path = card.path
                content.artwork.source = card.source

            new_version = previous_draft.version + 1
            new_draft = Draft(
                project_id=project.id,
                version=new_version,
                title=content.title,
                summary=content.description,
                instructions="\n".join(f"{i}. {t}" for i, t in enumerate(content.tasks, 1)),
                potential_reward=content.potential_reward,
                risk_note=previous_draft.risk_note,
                twitter_text=previous_draft.twitter_text,
                image_path=content.artwork.path,
                image_source=content.artwork.source,
                image_prompt=previous_draft.image_prompt,
                source_url=project.source_url,
                project_url=project.project_url,
                rework_feedback=f"Custom card steps: {'; '.join(sanitized)}",
                content_json=content.to_json(),
            )
            project.drafts.append(new_draft)
            await session.commit()

            await VersionManager.save_snapshot(
                session,
                draft_id=new_draft.id,
                project_id=project.id,
                action="Ручное обновление шагов",
                user_command=feedback_text,
                edit_plan_json=None,
                content=content,
            )

            queue = await _review_queue(session)
            position, total, previous_id, next_id = _queue_meta(queue, project.id)
            can_undo = await VersionManager.can_undo(session, new_draft.id)
            keyboard = review_keyboard(project.id, previous_id, next_id, position, total, can_undo=can_undo)
            caption = _review_caption(project, new_draft, position, total)

            if project.review_message_id and project.review_chat_id:
                try:
                    await message.bot.delete_message(
                        chat_id=project.review_chat_id,
                        message_id=project.review_message_id,
                    )
                except Exception:
                    pass

            photo_input = telegram_photo(new_draft.image_path)
            if photo_input:
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=photo_input,
                    caption=caption,
                    reply_markup=keyboard,
                )
                project.review_chat_id = sent.chat.id
                project.review_message_id = sent.message_id
                await session.commit()
            return

    # Standard Natural Language Edit via Editor V2
    try:
        async with get_session() as session:
            project = await _load_project(session, project_id)
            if not project or not project.latest_draft():
                await message.answer("Проект или черновик не найден.")
                return
            draft = project.latest_draft()
            if not project.project_url:
                project.project_url = await discover_project_link(
                    project.source_url, project.raw_data or "", project.name
                )

            content = draft_to_content(draft, project)

            has_snaps = await session.execute(
                select(DraftSnapshot.id).where(DraftSnapshot.draft_id == draft.id).limit(1)
            )
            if not has_snaps.scalar_one_or_none():
                await VersionManager.save_snapshot(
                    session,
                    draft_id=draft.id,
                    project_id=project.id,
                    action="Исходный черновик",
                    user_command=None,
                    edit_plan_json=None,
                    content=content,
                )

            plan = await EditorService.create_edit_plan(feedback_text, content)
            logger.info("Editor V2 plan for project #%s: %s", project.id, plan)

            if plan.requires_confirmation and plan.operation in ("remove", "delete") and plan.confidence < 0.7:
                pending_id = uuid.uuid4().hex[:8]
                pending_edits[pending_id] = {
                    "project_id": project.id,
                    "draft_id": draft.id,
                    "plan": plan,
                    "content": content,
                    "user_command": feedback_text,
                }

                def _format_diff_val(val: Any) -> str:
                    if isinstance(val, list):
                        return "\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(val, 1))
                    return str(val or "—")

                diff_text = (
                    f"📋 <b>Предложен план изменений (требует подтверждения):</b>\n\n"
                    f"• <b>Объект:</b> <code>{html.escape(plan.target)}</code> ({plan.operation})\n"
                    f"• <b>Было:</b> {html.escape(_format_diff_val(plan.old_value))}\n"
                    f"• <b>Станет:</b> {html.escape(_format_diff_val(plan.new_value))}\n"
                    f"• <b>Пояснение:</b> {html.escape(plan.explanation)}\n"
                    f"• <b>Операция с карточкой:</b> <code>{plan.image_operation}</code>\n\n"
                    f"Подтвердите применение или посмотрите предпросмотр:"
                )
                await message.reply(
                    diff_text,
                    parse_mode="HTML",
                    reply_markup=edit_preview_keyboard(project.id, pending_id),
                )
                return

            await _execute_and_apply_plan(message, session, project, draft, content, plan, feedback_text)
    except Exception as exc:
        logger.exception("Error applying feedback edit: %s", exc)
        await message.answer(f"⚠️ Ошибка при обработке команды: {html.escape(str(exc))}")


@router.callback_query(F.data.startswith("preview_edit:"))
async def on_preview_edit(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    project_id = int(parts[1])
    pending_id = parts[2]

    pending = pending_edits.get(pending_id)
    if not pending:
        await callback.answer("Предпросмотр устарел или уже применён.", show_alert=True)
        return

    plan: EditPlan = pending["plan"]
    content: DraftContent = pending["content"]
    await callback.answer("Формирую предпросмотр...")

    simulated_content, is_valid, err = await EditorService.apply_edit_plan(plan, content)
    if not is_valid:
        await callback.answer(f"Ошибка: {err}", show_alert=True)
        return

    card = await render_social_card_from_content(simulated_content)
    post_preview = simulated_content.render_telegram_post()
    text = (
        f"👁 <b>ПРЕДПРОСМОТР КАРТОЧКИ И ПОСТА:</b>\n\n"
        f"<b>План:</b> {html.escape(plan.explanation)}\n\n"
        f"<b>Текст поста:</b>\n{html.escape(post_preview[:600])}"
    )
    kb = edit_confirm_keyboard(project_id, pending_id)
    photo_input = telegram_photo(card.path) if card else None
    if photo_input and callback.message:
        await callback.bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=photo_input,
            caption=text[:1024],
            parse_mode="HTML",
            reply_markup=kb,
        )
    elif callback.message:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("apply_edit:"))
async def on_apply_edit(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    project_id = int(parts[1])
    pending_id = parts[2]

    pending = pending_edits.pop(pending_id, None)
    if not pending:
        await callback.answer("Действие уже выполнено или устарело.", show_alert=True)
        return

    await callback.answer("Применяю изменения...")
    plan: EditPlan = pending["plan"]
    user_command: str = pending["user_command"]

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            await callback.answer("Проект не найден.", show_alert=True)
            return
        draft = project.latest_draft()
        content = draft_to_content(draft, project)
        if callback.message:
            await _execute_and_apply_plan(callback.message, session, project, draft, content, plan, user_command)


@router.callback_query(F.data.startswith("cancel_edit:"))
async def on_cancel_edit(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    parts = callback.data.split(":")
    pending_id = parts[2] if len(parts) > 2 else ""
    pending_edits.pop(pending_id, None)
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.answer("Изменения отменены.")


@router.callback_query(F.data.startswith("undo_edit:"))
async def on_undo_edit(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.answer("Откатываю к предыдущей версии...")

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            await callback.answer("Проект не найден.", show_alert=True)
            return
        draft = project.latest_draft()

        restored_content, status_msg = await VersionManager.undo(session, draft.id)
        if not restored_content:
            await callback.answer(status_msg or "Невозможно отменить.", show_alert=True)
            return

        if draft.image_path:
            try:
                await ensure_draft_image(project, draft)
                await session.commit()
            except Exception:
                pass

        queue = await _review_queue(session)
        position, total, previous_id, next_id = _queue_meta(queue, project.id)
        can_undo = await VersionManager.can_undo(session, draft.id)
        keyboard = review_keyboard(project.id, previous_id, next_id, position, total, can_undo=can_undo)
        await _replace_review_message(callback, project.id, keyboard=keyboard)
        await callback.answer(status_msg, show_alert=True)



@router.message(Command("scan_now"))
async def on_scan_now(message: Message):
    global _manual_scan_task
    if not await _is_admin_message(message):
        return

    if _manual_scan_task and not _manual_scan_task.done():
        await message.answer("⏳ Сканирование уже выполняется. Повторный запуск не нужен.")
        return

    progress = await message.answer(
        "🔎 Сканирование запущено. Я пришлю итог в это же сообщение, когда оно завершится."
    )

    async def _run() -> None:
        global _manual_scan_task
        try:
            summary = await source_scanner.scan_once()
            text = (
                "✅ Сканирование завершено.\n\n"
                f"Найдено сигналов: {summary['collected']}\n"
                f"Отправлено на проверку: {summary['sent_for_review']}\n"
                f"Отфильтровано: {summary['filtered']}\n"
                f"Дубликаты/уже обработаны: {summary['duplicates']}\n"
                f"Ошибки: {summary['errors']}\n"
                f"Обработано через Groq: {summary['groq']}\n"
                f"Обработано локальным режимом без AI: {summary['fallback']}"
            )
            await progress.edit_text(text)
        except Exception as exc:
            logger.exception("Manual /scan_now failed: %s", exc)
            try:
                await progress.edit_text(f"❌ Сканирование завершилось ошибкой: {str(exc)[:800]}")
            except Exception:
                pass
        finally:
            _manual_scan_task = None

    _manual_scan_task = asyncio.create_task(_run())


@router.message(Command("status"))
@router.message(Command("channel_status"))
async def on_system_status(message: Message):
    if not await _is_admin_message(message):
        return
    progress = await message.answer("Проверяю источники и подключения...")
    health = await collect_system_health(message.bot)

    lines = [
        "Статус системы",
        "",
        f"1. Источники: {health.working_sources}/{len(health.sources)} работают",
    ]
    for source in health.sources:
        marker = "✅" if source.working else "❌"
        lines.append(f"{marker} {source.name}: {source.detail}")

    lines.extend(
        [
            "",
            f"2. Telegram: {'✅' if health.telegram.working else '❌'} {health.telegram.detail}",
            f"3. X/Twitter: {'✅' if health.x.working else '❌'} {health.x.detail}",
            f"4. Groq (основной AI): {'✅' if health.groq.working else '❌'} {health.groq.detail}",
            f"5. Gemini (резерв): {'✅' if health.gemini.working else '❌'} {health.gemini.detail}",
            f"6. Cloudflare Images: {'✅' if health.cloudflare.working else '❌'} {health.cloudflare.detail}",
            "",
            "7. Рекомендации:",
        ]
    )
    lines.extend(f"• {recommendation}" for recommendation in health.recommendations)
    await progress.edit_text("\n".join(lines))


async def _render_archive_view() -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        published_count = (
            await session.scalar(
                select(func.count(Project.id)).where(Project.status == ProjectStatus.PUBLISHED)
            )
            or 0
        )
        deleted_count = (
            await session.scalar(
                select(func.count(Project.id)).where(Project.status == ProjectStatus.DELETED)
            )
            or 0
        )
        pending_count = (
            await session.scalar(
                select(func.count(Project.id)).where(Project.status == ProjectStatus.PENDING_REVIEW)
            )
            or 0
        )

        recent_query = await session.execute(
            select(Project)
            .where(Project.status.in_([ProjectStatus.PUBLISHED, ProjectStatus.DELETED]))
            .order_by(Project.id.desc())
            .limit(8)
        )
        recent_projects = list(recent_query.scalars().all())

    lines = [
        "📦 <b>Архив проектов</b>",
        "",
        "📊 <b>Статистика:</b>",
        f"• ✅ Опубликовано: {published_count}",
        f"• 🗑 Удалено / Отклонено: {deleted_count}",
        f"• ⏳ В очереди проверки: {pending_count}",
    ]

    if recent_projects:
        lines.extend(["", "🕒 <b>Последние записи в архиве:</b>"])
        for p in recent_projects:
            marker = "✅" if p.status == ProjectStatus.PUBLISHED else "🗑"
            status_name = "Опубликован" if p.status == ProjectStatus.PUBLISHED else "Удален"
            p_name = html.escape(p.name or "Без названия")
            p_cat = html.escape(p.category or "opportunity")
            chain_info = f", {html.escape(p.chain)}" if p.chain else ""
            lines.append(f"{marker} #{p.id} <b>{p_name}</b> ({p_cat}{chain_info}) — {status_name}")
    else:
        lines.extend(["", "В архиве пока нет записей."])

    if deleted_count > 0:
        lines.extend(
            [
                "",
                "ℹ️ Нажмите <b>«🗑 Очистить архив»</b>, чтобы удалить отклонённые записи и освободить место в базе данных.",
            ]
        )

    markup = archive_keyboard(has_deleted=(deleted_count > 0))
    return "\n".join(lines), markup


@router.message(Command("archive"))
async def on_archive(message: Message):
    if not await _is_admin_message(message):
        return
    try:
        text, markup = await _render_archive_view()
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Error rendering archive: %s", exc)
        try:
            text, markup = await _render_archive_view()
            clean_text = re.sub(r"<[^>]+>", "", text)
            await message.answer(clean_text, reply_markup=markup)
        except Exception as inner_exc:
            await message.answer(f"⚠️ Ошибка при открытии архива: {inner_exc}")


@router.callback_query(F.data == "archive_refresh")
async def on_archive_refresh(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    text, markup = await _render_archive_view()
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
    await callback.answer("Архив обновлен.")


@router.callback_query(F.data == "archive_clear_deleted")
async def on_archive_clear_deleted(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return

    cleared_count = 0
    async with get_session() as session:
        del_ids_result = await session.execute(
            select(Project.id).where(Project.status == ProjectStatus.DELETED)
        )
        del_ids = list(del_ids_result.scalars().all())
        cleared_count = len(del_ids)
        if cleared_count > 0:
            await session.execute(
                delete(PublishedPost).where(PublishedPost.project_id.in_(del_ids))
            )
            await session.execute(delete(Draft).where(Draft.project_id.in_(del_ids)))
            await session.execute(delete(Project).where(Project.id.in_(del_ids)))
            await session.commit()

    text, markup = await _render_archive_view()
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
    await callback.answer(
        f"🗑 Архив очищен! Удалено записей: {cleared_count}", show_alert=True
    )


@router.callback_query(F.data == "archive_to_review")
async def on_archive_to_review(callback: CallbackQuery):
    if not await _is_admin_callback(callback):
        return
    if callback.message:
        await _open_review_queue(callback.message)
    await callback.answer()


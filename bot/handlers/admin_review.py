import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from bot.keyboards import archive_keyboard, open_in_x_keyboard, review_keyboard
from config import settings
from db.database import get_session
from db.models import Draft, Project, ProjectStatus, PublishedPost
from ingestion.scheduler import source_scanner
from publishing.dispatcher import publish_project
from services.ai_rework import rework_draft
from services.image_rework import requests_image_rework
from services.llm_draft import DraftResult
from services.media import ensure_draft_image, telegram_photo
from services.project_image import discover_project_image
from services.project_link import discover_project_link
from services.social_card import generate_social_card
from services.health import collect_system_health

logger = logging.getLogger(__name__)

router = Router()
awaiting_feedback: dict[int, bool] = {}
_manual_scan_task: asyncio.Task | None = None


def _is_admin_message(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == settings.ADMIN_USER_ID)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return callback.from_user.id == settings.ADMIN_USER_ID


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
    if draft.twitter_text:
        tw = draft.twitter_text.strip()
        if len(tw) > 280:
            tw = tw[:279].rsplit(" ", 1)[0] + "…"
        tw_section = f"\n\n2. Черновик для твиттера\n\n{tw}"

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

    # If exceeding 1024 characters (Telegram limit for photo captions),
    # smartly shorten the telegram draft body to fit while preserving links and twitter draft
    overhead = len(meta_section) + len(f"\n\n{tg_header}\n\n") + len(tw_section)
    avail_tg = 1024 - overhead - 5
    if avail_tg > 80:
        short_body = tg_body[:avail_tg].rsplit(" ", 1)[0] + "…"
        res = f"{meta_section}\n\n{tg_header}\n\n{short_body}{tw_section}"
        if len(res) <= 1024:
            return res

    return full_text[:1020].rsplit(" ", 1)[0] + "…"



async def _replace_review_message(callback: CallbackQuery, project_id: int) -> None:
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
        keyboard = review_keyboard(project.id, previous_id, next_id, position, total)

        current_has_photo = bool(callback.message.photo)
        desired_has_photo = bool(draft.image_path)
        caption = _review_caption(project, draft, position, total)

        sent = callback.message
        try:
            if desired_has_photo and current_has_photo:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=telegram_photo(draft.image_path),
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
                        photo=telegram_photo(draft.image_path),
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
                    photo=telegram_photo(draft.image_path),
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
            await ensure_draft_image(project, draft)
            await session.commit()

        position, total, previous_id, next_id = _queue_meta(queue, project.id)
        keyboard = review_keyboard(project.id, previous_id, next_id, position, total)
        caption = _review_caption(project, draft, position, total)
        if draft.image_path:
            sent = await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=telegram_photo(draft.image_path),
                caption=caption,
                reply_markup=keyboard,
            )
        else:
            sent = await message.answer(caption, reply_markup=keyboard)
        project.review_chat_id = message.chat.id
        project.review_message_id = sent.message_id
        await session.commit()


@router.message(Command("review"))
async def on_review(message: Message):
    if not _is_admin_message(message):
        return
    await _open_review_queue(message)


@router.callback_query(F.data.startswith("review_info:"))
async def on_review_info(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    async with get_session() as session:
        queue = await _review_queue(session)
        project_id = int(callback.data.split(":")[1])
        position, total, _, _ = _queue_meta(queue, project_id)
    await callback.answer(f"Черновик {position} из {total} в очереди.")


@router.callback_query(F.data.startswith("review_prev:"))
async def on_review_prev(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
        twitter_text = latest_draft.twitter_text
        image_path = latest_draft.image_path

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
            if len(tw_caption) > 1024:
                tw_caption = tw_text[:1020].rsplit(" ", 1)[0] + "…"
            if image_path:
                try:
                    await callback.bot.send_photo(
                        chat_id=callback.message.chat.id,
                        photo=telegram_photo(image_path),
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
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    project_id = int(callback.data.split(":")[1])
    awaiting_feedback[project_id] = True
    if callback.message:
        await callback.message.reply(
            "Ответьте на это сообщение и напишите, что изменить в черновике.\n"
            f"(project #{project_id})"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("regen_image:"))
async def on_regenerate_image(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
        regeneration_prompt = previous_draft.image_prompt or (
            f"{project.category} opportunity, red and teal cinematic environment"
        )
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=official_image.url if official_image else None,
            image_prompt=regeneration_prompt,
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}",
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


@router.message(F.reply_to_message, F.text)
async def on_feedback_reply(message: Message):
    if not _is_admin_message(message):
        return
    prompt_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if "(project #" not in prompt_text:
        return
    project_id = int(prompt_text.split("(project #")[1].rstrip(")"))
    if project_id not in awaiting_feedback:
        return

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            await message.answer("Проект или черновик не найден.")
            return
        previous_draft = project.latest_draft()
        if not project.project_url:
            project.project_url = await discover_project_link(
                project.source_url, project.raw_data or "", project.name
            )
        previous = DraftResult(
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_prompt=previous_draft.image_prompt,
        )
        try:
            new_result, rework_provider = await rework_draft(
                project.name,
                project.raw_data,
                project.chain,
                project.source_url,
                project.project_url,
                previous,
                message.text,
            )
        except Exception as exc:
            await session.rollback()
            await message.answer(
                "Groq и резервный Gemini сейчас недоступны, поэтому переработка не выполнена. "
                "Текущий черновик сохранён без изменений и ожидает дальнейших действий.\n\n"
                f"Причина: {str(exc)[:500]}",
                reply_markup=review_keyboard(project.id),
            )
            return

        del awaiting_feedback[project_id]
        image_requested = requests_image_rework(message.text)
        social_card = None
        if image_requested:
            official_image = await discover_project_image(project.source_url)
            social_card = await generate_social_card(
                name=project.name,
                category=project.category,
                chain=project.chain,
                instructions=new_result.instructions,
                official_image_url=official_image.url if official_image else None,
                image_prompt=new_result.image_prompt,
                project_url=project.project_url,
                generation_key=f"project-{project.id}-v{previous_draft.version + 1}",
                potential_reward=new_result.potential_reward,
            )
        new_draft = Draft(
            project_id=project.id,
            version=previous_draft.version + 1,
            title=new_result.title,
            summary=new_result.summary,
            instructions=new_result.instructions,
            potential_reward=new_result.potential_reward,
            risk_note=new_result.risk_note,
            twitter_text=new_result.twitter_text,
            image_path=social_card.path if social_card else previous_draft.image_path,
            image_source=social_card.source if social_card else previous_draft.image_source,
            image_prompt=(
                new_result.image_prompt if image_requested and social_card else previous_draft.image_prompt
            ),
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=message.text,
        )
        project.drafts.append(new_draft)
        await session.commit()

        if new_draft.image_path:
            await ensure_draft_image(project, new_draft)
            await session.commit()

        queue = await _review_queue(session)
        position, total, previous_id, next_id = _queue_meta(queue, project.id)
        keyboard = review_keyboard(project.id, previous_id, next_id, position, total)
        caption = _review_caption(project, new_draft, position, total)

        if project.review_message_id and project.review_chat_id:
            try:
                await message.bot.delete_message(
                    chat_id=project.review_chat_id,
                    message_id=project.review_message_id,
                )
            except Exception:
                pass

        if message.reply_to_message:
            try:
                await message.reply_to_message.delete()
            except Exception:
                pass

        if new_draft.image_path:
            sent = await message.answer_photo(
                photo=telegram_photo(new_draft.image_path),
                caption=caption,
                reply_markup=keyboard,
            )
        else:
            sent = await message.answer(caption, reply_markup=keyboard)

        project.review_chat_id = sent.chat.id
        project.review_message_id = sent.message_id
        await session.commit()



@router.message(Command("scan_now"))
async def on_scan_now(message: Message):
    global _manual_scan_task
    if not _is_admin_message(message):
        await message.answer("Этот бот доступен только администратору.")
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
    if not _is_admin_message(message):
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
            .order_by(Project.updated_at.desc(), Project.id.desc())
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
            p_name = html.escape(p.name)
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
    if not _is_admin_message(message):
        return
    text, markup = await _render_archive_view()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "archive_refresh")
async def on_archive_refresh(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
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
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    if callback.message:
        await _open_review_queue(callback.message)
    await callback.answer()


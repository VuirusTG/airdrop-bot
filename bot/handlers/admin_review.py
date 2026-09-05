"""Admin review commands and callback handlers."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.keyboards import open_in_x_keyboard, review_keyboard
from config import settings
from db.database import get_session
from db.models import Draft, Project, ProjectStatus
from ingestion.scheduler import source_scanner
from publishing.dispatcher import publish_project
from services.ai_rework import rework_draft
from services.image_rework import requests_image_rework
from services.llm_draft import DraftResult
from services.media import telegram_photo
from services.project_image import discover_project_image
from services.project_link import discover_project_link
from services.social_card import generate_social_card
from services.health import collect_system_health

router = Router()
awaiting_feedback: dict[int, bool] = {}


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
    lines = [
        f"🔎 REVIEW {position}/{total}  •  #{project.id}  •  {score}",
        f"🚀 {draft.title}",
        "",
        draft.summary.strip(),
        "",
        "📝 What to do:",
        draft.instructions.strip(),
    ]
    if draft.potential_reward:
        lines += ["", f"💰 {draft.potential_reward.strip()}"]
    if draft.risk_note:
        lines += ["", f"⚠️ {draft.risk_note.strip()}"]
    if draft.project_url:
        lines += ["", f"🔗 {draft.project_url}"]
    text = "\n".join(lines).strip()
    return text if len(text) <= 1024 else text[:1019].rsplit(" ", 1)[0] + "…"


async def _show_review_project(callback: CallbackQuery, project_id: int) -> bool:
    """Replace the single review message with another pending project."""
    if not callback.message:
        return False
    async with get_session() as session:
        queue = await _review_queue(session)
        project = next((item for item in queue if item.id == project_id), None)
        if not project or not project.latest_draft():
            return False
        position, total, previous_id, next_id = _queue_meta(queue, project_id)
        draft = project.latest_draft()
        keyboard = review_keyboard(project.id, previous_id, next_id, position, total)
        caption = _review_caption(project, draft, position, total)

        try:
            if draft.image_path:
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=telegram_photo(draft.image_path),
                        caption=caption,
                    ),
                    reply_markup=keyboard,
                )
            else:
                await callback.message.edit_text(caption, reply_markup=keyboard)
        except Exception:
            try:
                await callback.message.edit_caption(caption=caption, reply_markup=keyboard)
            except Exception:
                await callback.message.edit_text(caption, reply_markup=keyboard)

        project.review_chat_id = callback.message.chat.id
        project.review_message_id = callback.message.message_id
        await session.commit()
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
        if queue:
            project = queue[0]
            draft = project.latest_draft()
            if not draft:
                return
            position, total, previous_id, next_id = _queue_meta(queue, project.id)
            keyboard = review_keyboard(project.id, previous_id, next_id, position, total)
            caption = _review_caption(project, draft, position, total)
            try:
                if draft.image_path:
                    await callback.message.edit_media(
                        media=InputMediaPhoto(media=telegram_photo(draft.image_path), caption=caption),
                        reply_markup=keyboard,
                    )
                else:
                    await callback.message.edit_text(caption, reply_markup=keyboard)
            except Exception:
                try:
                    await callback.message.edit_caption(caption=caption, reply_markup=keyboard)
                except Exception:
                    await callback.message.edit_text(caption, reply_markup=keyboard)
            project.review_chat_id = callback.message.chat.id
            project.review_message_id = callback.message.message_id
            await session.commit()
            return

    try:
        await callback.message.edit_caption(caption="📭 Очередь черновиков пуста.", reply_markup=None)
    except Exception:
        await callback.message.edit_text("📭 Очередь черновиков пуста.", reply_markup=None)


@router.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    project_id = int(callback.data.split(":")[1])
    telegram_success = False
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

        project.status = ProjectStatus.APPROVED
        await session.commit()
        results = await publish_project(callback.bot, project, project.latest_draft())
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
                open_in_x_keyboard(project.latest_draft().twitter_text)
                if not x_result.success and project.latest_draft().twitter_text
                else None
            )
            await callback.message.answer("\n".join(lines), reply_markup=fallback_keyboard)

    if telegram_success:
        await _show_next_or_empty(callback, project_id)
        await callback.answer("Опубликовано в Telegram.")
    else:
        await callback.answer("Telegram не опубликовал пост. Смотрите ошибку ниже.", show_alert=True)


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
        queue = await _review_queue(session)
        position, total, previous_id, next_id = _queue_meta(queue, project.id)

        if callback.message:
            await callback.message.edit_media(
                media=InputMediaPhoto(
                    media=telegram_photo(new_draft.image_path),
                    caption=_review_caption(project, new_draft, position, total),
                ),
                reply_markup=review_keyboard(project.id, previous_id, next_id, position, total),
            )


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
        await message.answer(
            f"Переработано через {rework_provider}, версия {new_draft.version}\n"
            f"Изображение: {'создано заново' if image_requested and social_card else 'сохранено без изменений'}\n\n"
            f"{new_draft.rendered_review_text()}",
            reply_markup=review_keyboard(project.id),
        )


@router.message(Command("scan_now"))
async def on_scan_now(message: Message):
    if not _is_admin_message(message):
        await message.answer("Этот бот доступен только администратору.")
        return
    await message.answer("Сканирую источники. Облачная AI-проверка может занять несколько минут...")
    summary = await source_scanner.scan_once()
    await message.answer(
        "Сканирование завершено.\n"
        f"Найдено сигналов: {summary['collected']}\n"
        f"Отправлено на проверку: {summary['sent_for_review']}\n"
        f"Отфильтровано: {summary['filtered']}\n"
        f"Дубликаты/уже обработаны: {summary['duplicates']}\n"
        f"Ошибки: {summary['errors']}\n"
        f"Обработано через Groq: {summary['groq']}\n"
        f"Обработано локальным режимом без AI: {summary['fallback']}"
    )


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

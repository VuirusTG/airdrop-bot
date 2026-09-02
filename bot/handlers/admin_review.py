"""Admin review commands and callback handlers."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
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


@router.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    project_id = int(callback.data.split(":")[1])
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
        project.status = (
            ProjectStatus.PUBLISHED if telegram_result.success else ProjectStatus.APPROVED
        )
        await session.commit()

        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
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
            result_text = "\n".join(lines)
            fallback_image = project.latest_draft().image_path
            if fallback_keyboard and fallback_image:
                try:
                    await callback.bot.send_photo(
                        chat_id=callback.message.chat.id,
                        photo=telegram_photo(fallback_image),
                        caption=(
                            result_text
                            + "\n\nФото для ручной публикации в X. Откройте X кнопкой ниже, "
                            "затем прикрепите это изображение."
                        )[:1024],
                        reply_markup=fallback_keyboard,
                    )
                except Exception:
                    await callback.message.answer(result_text, reply_markup=fallback_keyboard)
            else:
                await callback.message.answer(result_text, reply_markup=fallback_keyboard)

    if telegram_result.success:
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
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(f"Проект #{project.id} удален и архивирован.")
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

        if callback.message:
            provider = (
                "Cloudflare Workers AI"
                if social_card.source == "generated_social_card_cloudflare"
                else "локальный резервный генератор"
            )
            await callback.bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=telegram_photo(new_draft.image_path),
                caption=f"Новая картинка для версии {new_version}, источник: {provider}",
            )
            await callback.message.answer(
                f"Создана версия {new_version}. Текст сохранён без изменений.",
                reply_markup=review_keyboard(project.id),
            )


@router.message(F.reply_to_message, F.text)
async def on_feedback_reply(message: Message):
    if not _is_admin_message(message):
        return
    prompt_text = message.reply_to_message.text or ""
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
                reply_markup=review_keyboard(project_id),
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
                new_result.image_prompt
                if image_requested and social_card
                else previous_draft.image_prompt
            ),
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=message.text,
        )
        project.drafts.append(new_draft)
        await session.commit()
        if image_requested and new_draft.image_path:
            try:
                await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=telegram_photo(new_draft.image_path),
                    caption=f"Новая social card для версии {new_draft.version}",
                )
            except Exception:
                pass
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

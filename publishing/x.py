"""Publish prepared text through the official X API using OAuth 1.0a user context."""
import asyncio
import base64
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image
from requests_oauthlib import OAuth1

from config import settings

CREATE_POST_URL = "https://api.x.com/2/tweets"
CURRENT_USER_URL = "https://api.x.com/2/users/me"
MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class XPublishError(RuntimeError):
    pass


def _credentials_ready() -> bool:
    return all(
        (
            settings.X_API_KEY,
            settings.X_API_SECRET,
            settings.X_ACCESS_TOKEN,
            settings.X_ACCESS_TOKEN_SECRET,
        )
    )


def _auth() -> OAuth1:
    return OAuth1(
        settings.X_API_KEY,
        settings.X_API_SECRET,
        settings.X_ACCESS_TOKEN,
        settings.X_ACCESS_TOKEN_SECRET,
    )


def _friendly_error(status_code: int, detail: str) -> str:
    if status_code == 401:
        return "X отклонил авторизацию: проверьте API key и access tokens."
    if status_code == 403:
        return "X запретил публикацию: приложению нужны права Read and Write."
    if status_code == 402:
        return "На балансе X API недостаточно кредитов для публикации."
    if status_code == 429:
        return "X временно ограничил частоту запросов. Опубликуйте вручную или повторите позже."
    return f"X API вернул ошибку {status_code}: {detail[:300]}"


def _load_image(image_path: str) -> tuple[bytes, str]:
    try:
        if image_path.startswith(("https://", "http://")):
            with requests.get(image_path, timeout=20, stream=True) as response:
                response.raise_for_status()
                declared_size = int(response.headers.get("content-length", "0") or 0)
                if declared_size > MAX_IMAGE_BYTES:
                    raise XPublishError("Изображение для X превышает лимит 5 МБ.")
                chunks: list[bytes] = []
                downloaded = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_IMAGE_BYTES:
                        raise XPublishError("Изображение для X превышает лимит 5 МБ.")
                    chunks.append(chunk)
                content = b"".join(chunks)
        else:
            content = Path(image_path).read_bytes()
    except XPublishError:
        raise
    except (OSError, requests.RequestException) as exc:
        raise XPublishError(f"Не удалось получить изображение для X: {exc}") from exc

    if not content:
        raise XPublishError("Изображение для X пустое.")
    if len(content) > MAX_IMAGE_BYTES:
        raise XPublishError("Изображение для X превышает лимит 5 МБ.")

    try:
        with Image.open(BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            image.verify()
    except Exception as exc:
        raise XPublishError("Файл проекта не является поддерживаемым изображением.") from exc

    media_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }.get(image_format)
    if not media_type:
        raise XPublishError(f"X не поддерживает формат изображения {image_format or 'unknown'}.")
    return content, media_type


def _upload_image(image_path: str) -> str:
    content, media_type = _load_image(image_path)
    payload = {
        "media": base64.b64encode(content).decode("ascii"),
        "media_category": "tweet_image",
        "media_type": media_type,
        "shared": False,
    }
    try:
        response = requests.post(MEDIA_UPLOAD_URL, json=payload, auth=_auth(), timeout=30)
    except requests.RequestException as exc:
        raise XPublishError(f"Не удалось загрузить изображение в X: {exc}") from exc

    if response.status_code not in (200, 201):
        raise XPublishError(
            "X не загрузил изображение: "
            + _friendly_error(response.status_code, response.text)
        )
    data = response.json().get("data", {})
    media_id = str(data.get("id") or data.get("media_id_string") or "")
    if not media_id:
        raise XPublishError("X загрузил изображение, но не вернул media_id.")
    return media_id


def _publish_sync(text: str, image_path: str | None = None) -> tuple[str, str]:
    payload: dict = {"text": text}
    if image_path:
        payload["media"] = {"media_ids": [_upload_image(image_path)]}
    try:
        response = requests.post(
            CREATE_POST_URL,
            json=payload,
            auth=_auth(),
            timeout=20,
        )
    except requests.RequestException as exc:
        raise XPublishError(f"Не удалось подключиться к X API: {exc}") from exc

    if response.status_code != 201:
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("title") or str(payload)
        except ValueError:
            detail = response.text
        raise XPublishError(_friendly_error(response.status_code, detail))

    payload = response.json().get("data", {})
    post_id = str(payload.get("id") or "")
    if not post_id:
        raise XPublishError("X API подтвердил запрос, но не вернул ID публикации.")
    return post_id, f"https://x.com/i/web/status/{post_id}"


async def publish_to_x(text: str, image_path: str | None = None) -> tuple[str, str]:
    if not settings.X_AUTO_PUBLISH:
        raise XPublishError("Автопубликация в X отключена настройкой X_AUTO_PUBLISH.")
    if not _credentials_ready():
        raise XPublishError(
            "Не заполнены X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN и X_ACCESS_TOKEN_SECRET."
        )
    if not text.strip():
        raise XPublishError("Черновик для X пуст.")
    if len(text) > 280:
        raise XPublishError(f"Черновик для X длиннее 280 символов ({len(text)}).")
    return await asyncio.to_thread(_publish_sync, text, image_path)


def _check_connection_sync() -> tuple[bool, str]:
    try:
        response = requests.get(CURRENT_USER_URL, auth=_auth(), timeout=15)
    except requests.RequestException as exc:
        return False, f"Не удалось подключиться к X API: {exc}"
    if response.status_code != 200:
        return False, _friendly_error(response.status_code, response.text)
    data = response.json().get("data", {})
    username = data.get("username")
    return True, f"подключен аккаунт @{username}" if username else "OAuth подключен"


async def check_x_connection() -> tuple[bool, str]:
    if not settings.X_AUTO_PUBLISH:
        return False, "автопубликация отключена через X_AUTO_PUBLISH"
    if not _credentials_ready():
        missing = [
            name
            for name, value in (
                ("X_API_KEY", settings.X_API_KEY),
                ("X_API_SECRET", settings.X_API_SECRET),
                ("X_ACCESS_TOKEN", settings.X_ACCESS_TOKEN),
                ("X_ACCESS_TOKEN_SECRET", settings.X_ACCESS_TOKEN_SECRET),
            )
            if not value
        ]
        return False, "не заполнены: " + ", ".join(missing)
    return await asyncio.to_thread(_check_connection_sync)

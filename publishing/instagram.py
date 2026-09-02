"""
Instagram publishing via the Graph API (content-publish endpoint). Meta does
not charge per call. For a single account (yours), you can skip full Meta App
Review by keeping your app in Development Mode and adding your own Instagram
account as a Test User in the Meta Developer dashboard — see README.

IMPORTANT: Instagram's API requires an image (no text-only feed posts), and
the image must be reachable at a public URL. Image generation + hosting is
Stage 3 — until that lands, this function will raise a clear error rather
than silently failing.
"""
import httpx

from config import settings

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


class InstagramNotReady(Exception):
    pass


async def publish_to_instagram(caption: str, image_url: str | None) -> dict:
    if not settings.INSTAGRAM_ACCESS_TOKEN or not settings.INSTAGRAM_BUSINESS_ACCOUNT_ID:
        raise InstagramNotReady(
            "Instagram credentials not set in .env — see README 'Instagram setup'."
        )
    if not image_url:
        raise InstagramNotReady(
            "Instagram requires an image and none was provided yet "
            "(image generation/hosting lands in Stage 3)."
        )

    async with httpx.AsyncClient() as http:
        # Step 1: create a media container
        container_resp = await http.post(
            f"{GRAPH_API_BASE}/{settings.INSTAGRAM_BUSINESS_ACCOUNT_ID}/media",
            data={
                "image_url": image_url,
                "caption": caption,
                "access_token": settings.INSTAGRAM_ACCESS_TOKEN,
            },
        )
        container_resp.raise_for_status()
        container_id = container_resp.json()["id"]

        # Step 2: publish the container
        publish_resp = await http.post(
            f"{GRAPH_API_BASE}/{settings.INSTAGRAM_BUSINESS_ACCOUNT_ID}/media_publish",
            data={
                "creation_id": container_id,
                "access_token": settings.INSTAGRAM_ACCESS_TOKEN,
            },
        )
        publish_resp.raise_for_status()
        return publish_resp.json()  # contains the published media id

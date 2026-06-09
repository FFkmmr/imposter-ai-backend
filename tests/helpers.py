"""Shared constants and helper functions used across test modules."""
import uuid
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

API_KEY = "test-api-key"
ADMIN_KEY = "test-admin-key"


def api_headers(device_id: str | None = None) -> dict:
    h = {"X-Api-Key": API_KEY}
    if device_id:
        h["X-Device-Id"] = device_id
    return h


def admin_headers() -> dict:
    return {"X-Admin-Api-Key": ADMIN_KEY}


async def create_user(client: AsyncClient, device_id: str | None = None) -> dict:
    did = device_id or str(uuid.uuid4())
    resp = await client.post("/v1/users", json={"device_id": did})
    assert resp.status_code in (200, 201)
    return resp.json()


async def create_category(db_session: AsyncSession, *, slug="test-cat", is_premium=False):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
    from models import Category
    cat = Category(
        slug=slug,
        name={"en": slug.title(), "ru": slug.title()},
        description={"en": "desc", "ru": "описание"},
        is_premium=is_premium,
        is_active=True,
        sort_order=0,
    )
    db_session.add(cat)
    await db_session.flush()
    return cat


async def create_word_pack(db_session: AsyncSession, category_id, *, locale="en", difficulty="easy"):
    from models import WordPack
    wp = WordPack(
        category_id=category_id,
        locale=locale,
        civilian_word="Apple",
        impostor_word="Pear",
        difficulty=difficulty,
        tags=[],
    )
    db_session.add(wp)
    await db_session.flush()
    return wp

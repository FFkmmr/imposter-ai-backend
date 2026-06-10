"""Shared constants and helper functions used across test modules."""
import uuid
from datetime import datetime, timedelta, timezone
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


async def create_premium_user(
    db_session: AsyncSession,
    *,
    device_id: str | None = None,
    tokens: int = 10,
    is_premium: bool = True,
    expires_at: datetime | None = None,
    expires_in_days: int | None = 30,
):
    """Создаёт User напрямую в db_session (она же инжектится в эндпоинт через
    dependency override), чтобы детерминированно задать premium-статус и баланс
    токенов для ADR-003 тестов.

    - premium-активный по умолчанию (is_premium=True, expires через 30 дней);
    - передай expires_in_days=None + expires_at=None → premium без даты истечения;
    - передай is_premium=False → не-premium;
    - передай expires_at=<прошлое> → expired premium.

    Возвращает (User, device_id_str).
    """
    from models import User

    did = uuid.UUID(device_id) if device_id else uuid.uuid4()

    if expires_at is None and expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

    user = User(
        device_id=did,
        tokens=tokens,
        is_premium=is_premium,
        premium_expires_at=expires_at,
    )
    db_session.add(user)
    await db_session.flush()
    return user, str(did)


async def get_user_tokens(db_session: AsyncSession, device_id: str) -> int:
    """Перечитывает баланс токенов пользователя из БД (для проверки списания)."""
    from sqlalchemy import select
    from models import User

    db_session.expire_all()
    result = await db_session.execute(
        select(User.tokens).where(User.device_id == uuid.UUID(device_id))
    )
    return result.scalar_one()


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

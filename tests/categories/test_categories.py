"""
tests/categories — Category listing and word retrieval tests.

Covers:
  GET /v1/categories
    - Returns list of active categories
    - Inactive categories not included
    - Locale query param (en/ru fallback)
    - Requires X-Api-Key

  GET /v1/categories/premium
    - Returns only premium categories
    - Includes preview_words (up to 3)
    - Free-only categories not returned

  GET /v1/categories/{id}/words
    - Returns words for valid category
    - 404 for unknown category
    - Premium category blocked for non-premium user (403)
    - Premium category accessible for premium user
    - Locale fallback to "en" when requested locale missing
    - count param respected
    - Rate limit headers present
    - Rate limit exceeded → 429
    - Missing X-Device-Id → 422
    - Invalid X-Device-Id → 400
"""
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from helpers import api_headers, create_category, create_word_pack, create_user
from models import User


class TestListCategories:
    async def test_returns_200(self, client: AsyncClient, db_session: AsyncSession):
        await create_category(db_session, slug="animals")
        resp = await client.get("/v1/categories", headers=api_headers())
        assert resp.status_code == 200

    async def test_response_has_categories_key(self, client: AsyncClient, db_session: AsyncSession):
        await create_category(db_session, slug="food")
        resp = await client.get("/v1/categories", headers=api_headers())
        assert "categories" in resp.json()

    async def test_active_categories_returned(self, client: AsyncClient, db_session: AsyncSession):
        await create_category(db_session, slug="sports")
        resp = await client.get("/v1/categories", headers=api_headers())
        slugs = [c["slug"] for c in resp.json()["categories"]]
        assert "sports" in slugs

    async def test_inactive_category_excluded(self, client: AsyncClient, db_session: AsyncSession):
        from models import Category
        cat = Category(
            slug="inactive-cat",
            name={"en": "Inactive"},
            description={},
            is_active=False,
            sort_order=99,
        )
        db_session.add(cat)
        await db_session.flush()
        resp = await client.get("/v1/categories", headers=api_headers())
        slugs = [c["slug"] for c in resp.json()["categories"]]
        assert "inactive-cat" not in slugs

    async def test_locale_name_returned(self, client: AsyncClient, db_session: AsyncSession):
        from models import Category
        cat = Category(
            slug="locale-test",
            name={"en": "Animals", "ru": "Животные"},
            description={"en": "desc"},
            is_active=True,
            sort_order=0,
        )
        db_session.add(cat)
        await db_session.flush()

        resp_ru = await client.get("/v1/categories?locale=ru", headers=api_headers())
        names = {c["slug"]: c["name"] for c in resp_ru.json()["categories"]}
        assert names.get("locale-test") == "Животные"

    async def test_locale_fallback_to_en(self, client: AsyncClient, db_session: AsyncSession):
        from models import Category
        cat = Category(
            slug="en-only",
            name={"en": "English Only"},
            description={},
            is_active=True,
            sort_order=0,
        )
        db_session.add(cat)
        await db_session.flush()

        resp = await client.get("/v1/categories?locale=fr", headers=api_headers())
        names = {c["slug"]: c["name"] for c in resp.json()["categories"]}
        assert names.get("en-only") == "English Only"

    async def test_requires_api_key(self, client: AsyncClient):
        resp = await client.get("/v1/categories")
        assert resp.status_code == 422


class TestListPremiumCategories:
    async def test_returns_only_premium(self, client: AsyncClient, db_session: AsyncSession):
        await create_category(db_session, slug="free-cat", is_premium=False)
        prem = await create_category(db_session, slug="prem-cat", is_premium=True)
        await create_word_pack(db_session, prem.id)

        resp = await client.get("/v1/categories/premium", headers=api_headers())
        slugs = [c["slug"] for c in resp.json()["categories"]]
        assert "prem-cat" in slugs
        assert "free-cat" not in slugs

    async def test_preview_words_included(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="prem-preview", is_premium=True)
        await create_word_pack(db_session, cat.id)
        await create_word_pack(db_session, cat.id)

        resp = await client.get("/v1/categories/premium", headers=api_headers())
        cats = {c["slug"]: c for c in resp.json()["categories"]}
        assert "prem-preview" in cats
        assert isinstance(cats["prem-preview"]["preview_words"], list)
        assert len(cats["prem-preview"]["preview_words"]) <= 3

    async def test_preview_words_max_3(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="prem-many", is_premium=True)
        for i in range(5):
            from models import WordPack
            wp = WordPack(
                category_id=cat.id,
                locale="en",
                civilian_word=f"Word{i}",
                difficulty="easy",
                tags=[],
            )
            db_session.add(wp)
        await db_session.flush()

        resp = await client.get("/v1/categories/premium", headers=api_headers())
        cats = {c["slug"]: c for c in resp.json()["categories"]}
        assert len(cats["prem-many"]["preview_words"]) <= 3


class TestGetWords:
    async def test_returns_words_for_free_category(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="words-free")
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        resp = await client.get(
            f"/v1/categories/{cat.id}/words",
            headers=api_headers(did),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "words" in body
        assert len(body["words"]) >= 1

    async def test_word_entry_shape(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="shape-test")
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        word = resp.json()["words"][0]
        assert "id" in word
        assert "civilian_word" in word
        assert "difficulty" in word

    async def test_unknown_category_returns_404(self, client: AsyncClient):
        did = str(uuid.uuid4())
        resp = await client.get(
            f"/v1/categories/{uuid.uuid4()}/words",
            headers=api_headers(did),
        )
        assert resp.status_code == 404

    async def test_premium_category_blocked_for_free_user(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="prem-blocked", is_premium=True)
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())
        # create user but NOT premium
        await client.post("/v1/users", json={"device_id": did})

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        assert resp.status_code == 403

    async def test_premium_category_accessible_for_premium_user(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        cat = await create_category(db_session, slug="prem-ok", is_premium=True)
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())
        # create user then make premium directly in DB
        await client.post("/v1/users", json={"device_id": did})
        from sqlalchemy import select
        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        user.is_premium = True
        await db_session.flush()

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        assert resp.status_code == 200

    async def test_locale_fallback_to_en(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="locale-words")
        await create_word_pack(db_session, cat.id, locale="en")  # only en exists
        did = str(uuid.uuid4())

        resp = await client.get(
            f"/v1/categories/{cat.id}/words?locale=ru",
            headers=api_headers(did),
        )
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"

    async def test_count_param_limits_words(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="count-test")
        for _ in range(5):
            await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        resp = await client.get(
            f"/v1/categories/{cat.id}/words?count=2",
            headers=api_headers(did),
        )
        assert len(resp.json()["words"]) <= 2

    async def test_rate_limit_headers_present(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="rl-headers")
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers
        assert "x-ratelimit-reset" in resp.headers

    async def test_rate_limit_exceeded_returns_429(self, client: AsyncClient, db_session: AsyncSession, fake_redis):
        cat = await create_category(db_session, slug="rl-exceed")
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        # fill the rate limit bucket manually
        import time
        now = time.time()
        key = f"rate:words:{did}"
        for i in range(101):
            await fake_redis.zadd(key, {str(now + i): now + i})

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        assert resp.status_code == 429

    async def test_missing_device_id_returns_422(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="no-device")
        resp = await client.get(
            f"/v1/categories/{cat.id}/words",
            headers={"X-Api-Key": "test-api-key"},
        )
        assert resp.status_code == 422

    async def test_invalid_device_id_returns_400(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="bad-device")
        resp = await client.get(
            f"/v1/categories/{cat.id}/words",
            headers={"X-Api-Key": "test-api-key", "X-Device-Id": "not-a-uuid"},
        )
        assert resp.status_code == 400

    async def test_mode_returned_in_response(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="mode-test")
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        resp = await client.get(
            f"/v1/categories/{cat.id}/words?mode=party",
            headers=api_headers(did),
        )
        assert resp.json()["mode"] == "party"

    async def test_expired_premium_blocked(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="prem-expired", is_premium=True)
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())
        await client.post("/v1/users", json={"device_id": did})

        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        user.is_premium = True
        user.premium_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await db_session.flush()

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        assert resp.status_code == 403

    async def test_active_premium_not_blocked(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="prem-future", is_premium=True)
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())
        await client.post("/v1/users", json={"device_id": did})

        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        user.is_premium = True
        user.premium_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        await db_session.flush()

        resp = await client.get(f"/v1/categories/{cat.id}/words", headers=api_headers(did))
        assert resp.status_code == 200

    async def test_cache_control_header_on_list(self, client: AsyncClient, db_session: AsyncSession):
        await create_category(db_session, slug="cache-list")
        resp = await client.get("/v1/categories", headers=api_headers())
        assert resp.headers.get("cache-control") == "max-age=1800"

    async def test_cache_control_header_on_premium_list(self, client: AsyncClient, db_session: AsyncSession):
        cat = await create_category(db_session, slug="cache-prem", is_premium=True)
        await create_word_pack(db_session, cat.id)
        resp = await client.get("/v1/categories/premium", headers=api_headers())
        assert resp.headers.get("cache-control") == "max-age=1800"

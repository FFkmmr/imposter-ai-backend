"""
tests/ai — AI theme generation tests (ADR-003: premium-only + token-spend).

All OpenAI calls are mocked — no real API calls are made.

Контракт POST /v1/ai/generate-theme переписан под ADR-003:
  - premium-only: не-premium / expired / отсутствующий User → 200,
    fallback_used=true, fallback_reason="premium_required", tokens_remaining=null,
    баланс/AI не вызывается;
  - списание AI_THEME_TOKEN_COST РОВНО при фактической выдаче AI-слова
    (свежая LLM-генерация ИЛИ pool-cache hit); история пишется только ПОСЛЕ списания;
  - на ЛЮБОЙ fallback токены НЕ списываются (premium_required, insufficient_tokens,
    rate_limit_exceeded, moderation_rejected, ai_unavailable);
  - premium с tokens < cost → insufficient_tokens, баланс не в минусе;
  - гонка: два параллельных запроса с tokens=cost → один получает слово+списание,
    второй insufficient_tokens, баланс не в минусе (conditional UPDATE rowcount);
  - best-effort: сбой Redis sadd / _log пост-commit → всё равно 200 + слово, токен списан;
  - эндпоинт нигде не отдаёт 429.

  POST /v1/ai/generate-words  (admin) — токеномикой НЕ затронут (регресс).
"""
import uuid
import json
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from helpers import (
    api_headers, admin_headers, create_category, create_word_pack,
    create_premium_user, get_user_tokens,
)


DEVICE = str(uuid.uuid4())

# ── helpers ────────────────────────────────────────────────────────────────

def _llm_response(words: list[dict]) -> MagicMock:
    """Build a fake OpenAI ChatCompletion response."""
    msg = MagicMock()
    msg.content = json.dumps(words)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _moderation_safe() -> MagicMock:
    result = MagicMock()
    result.flagged = False
    mod = MagicMock()
    mod.results = [result]
    return mod


def _moderation_unsafe() -> MagicMock:
    result = MagicMock()
    result.flagged = True
    mod = MagicMock()
    mod.results = [result]
    return mod


async def _ensure_fallback_pool(db_session):
    """Гарантирует наличие хотя бы одного WordPack для curated-fallback."""
    cat = await create_category(db_session, slug=f"ai-fb-{uuid.uuid4().hex[:8]}")
    await create_word_pack(db_session, cat.id)


# ═══════════════════════════════════════════════════════════════════════════
# Premium-gating (Шаг 3) — не-premium → premium_required, без списания, без AI
# ═══════════════════════════════════════════════════════════════════════════
class TestPremiumGating:
    async def test_missing_user_returns_premium_required(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Отсутствующий в БД User → premium_required, tokens_remaining=null, AI не вызывается."""
        await _ensure_fallback_pool(db_session)
        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "premium_required"
        assert body["tokens_remaining"] is None
        llm.assert_not_awaited()

    async def test_non_premium_user_returns_premium_required(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """User существует, но is_premium=False → premium_required, баланс не трогается."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(
            db_session, tokens=100, is_premium=False, expires_in_days=None
        )
        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_reason"] == "premium_required"
        assert body["tokens_remaining"] is None
        llm.assert_not_awaited()
        assert await get_user_tokens(db_session, did) == 100  # не списано

    async def test_expired_premium_returns_premium_required(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """is_premium=True, но premium_expires_at в прошлом → premium_required."""
        from datetime import datetime, timedelta, timezone
        await _ensure_fallback_pool(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        _, did = await create_premium_user(
            db_session, tokens=100, is_premium=True, expires_at=past
        )
        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        assert resp.json()["fallback_reason"] == "premium_required"
        assert resp.json()["tokens_remaining"] is None
        llm.assert_not_awaited()
        assert await get_user_tokens(db_session, did) == 100


# ═══════════════════════════════════════════════════════════════════════════
# Успешная выдача + списание токенов (Шаги 7-10)
# ═══════════════════════════════════════════════════════════════════════════
class TestSuccessfulSpend:
    async def test_premium_fresh_llm_spends_exactly_cost(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Premium, баланс>cost, свежая LLM → fallback_used=false, списано РОВНО cost,
        tokens_remaining = баланс-cost."""
        _, did = await create_premium_user(db_session, tokens=5)  # cost=1 default
        words = [{"civilian_word": "Mars", "impostor_word": "Moon", "difficulty": "medium"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is False
        assert body["fallback_reason"] is None
        assert body["civilian_word"] == "Mars"
        assert body["tokens_remaining"] == 4  # 5 - 1
        assert await get_user_tokens(db_session, did) == 4

    async def test_party_mode_includes_impostor_word_on_success(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        _, did = await create_premium_user(db_session, tokens=5)
        words = [{"civilian_word": "Cat", "impostor_word": "Kitten", "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "pets", "locale": "en", "mode": "party"},
            )
        body = resp.json()
        assert body["impostor_word"] == "Kitten"
        assert body["fallback_used"] is False
        assert body["tokens_remaining"] == 4

    async def test_standard_mode_excludes_impostor_word_on_success(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        _, did = await create_premium_user(db_session, tokens=5)
        words = [{"civilian_word": "Cat", "impostor_word": "Kitten", "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "pets", "locale": "en", "mode": "standard"},
            )
        assert resp.json()["impostor_word"] is None

    async def test_custom_cost_spends_configured_amount(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """AI_THEME_TOKEN_COST=3 → списывается ровно 3."""
        _, did = await create_premium_user(db_session, tokens=10)
        words = [{"civilian_word": "Mars", "impostor_word": None, "difficulty": "easy"}]

        from config import get_settings
        settings = get_settings()
        with (
            patch.object(settings, "ai_theme_token_cost", 3),
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        assert resp.json()["tokens_remaining"] == 7  # 10 - 3
        assert await get_user_tokens(db_session, did) == 7

    async def test_pool_cache_hit_also_spends_and_writes_history_after(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        """Pool-cache hit тоже списывает токен; история пишется ТОЛЬКО после списания."""
        _, did = await create_premium_user(db_session, tokens=5)
        topic, locale = "cached-topic", "en"
        cached = {"civilian_word": "CachedWord", "impostor_word": None, "difficulty": "easy"}
        pool_key = f"topic_pool:{topic}:{locale}"
        history_key = f"device_history:{did}:{topic}:{locale}"
        await fake_redis.sadd(pool_key, json.dumps(cached))

        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": topic, "locale": locale, "mode": "standard"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["civilian_word"] == "CachedWord"
        assert body["fallback_used"] is False
        assert body["tokens_remaining"] == 4
        llm.assert_not_awaited()  # LLM не вызван — pool hit
        assert await get_user_tokens(db_session, did) == 4
        # история записана ПОСЛЕ успешного списания
        hist = await fake_redis.smembers(history_key)
        assert json.dumps(cached) in hist

    async def test_rate_limit_headers_present_on_success(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        _, did = await create_premium_user(db_session, tokens=5)
        words = [{"civilian_word": "Sun", "impostor_word": None, "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "weather", "locale": "en", "mode": "standard"},
            )
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers
        assert "x-ratelimit-reset" in resp.headers


# ═══════════════════════════════════════════════════════════════════════════
# Недостаток баланса (Шаг 6) — insufficient_tokens, без списания
# ═══════════════════════════════════════════════════════════════════════════
class TestInsufficientTokens:
    async def test_zero_balance_returns_insufficient_tokens(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Premium с tokens=0 < cost → insufficient_tokens, tokens_remaining=0, без AI."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=0)
        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "insufficient_tokens"
        assert body["tokens_remaining"] == 0
        llm.assert_not_awaited()
        assert await get_user_tokens(db_session, did) == 0  # не ушёл в минус

    async def test_balance_below_custom_cost_returns_insufficient(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """tokens=2 < cost=3 → insufficient_tokens, tokens_remaining=2, без списания."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=2)
        from config import get_settings
        settings = get_settings()
        llm = AsyncMock()
        with (
            patch.object(settings, "ai_theme_token_cost", 3),
            patch("routers.ai._generate_with_llm", new=llm),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )
        body = resp.json()
        assert body["fallback_reason"] == "insufficient_tokens"
        assert body["tokens_remaining"] == 2
        llm.assert_not_awaited()
        assert await get_user_tokens(db_session, did) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Прочие fallback'и для premium — токены НЕ списываются
# ═══════════════════════════════════════════════════════════════════════════
class TestNonSpendingFallbacks:
    async def test_rate_limit_exceeded_no_spend(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        """Premium, rate-limit превышен → rate_limit_exceeded, 200 (не 429),
        tokens_remaining=баланс, без списания."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=5)

        import time
        now = time.time()
        key = f"rate:ai_theme:{did}"
        for i in range(60):  # premium limit 50/24h → переполняем
            await fake_redis.zadd(key, {str(now + i): now + i})

        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "food", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200  # никогда не 429
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "rate_limit_exceeded"
        assert body["tokens_remaining"] == 5
        llm.assert_not_awaited()
        assert await get_user_tokens(db_session, did) == 5

    async def test_blacklist_moderation_rejected_no_spend(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Premium, тема в blacklist → moderation_rejected, без списания."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=5)
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(did),
            json={"topic": "sex drugs", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_reason"] == "moderation_rejected"
        assert body["tokens_remaining"] == 5
        assert await get_user_tokens(db_session, did) == 5

    async def test_llm_all_unsafe_moderation_rejected_no_spend(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Premium, LLM вернул только unsafe-слова → moderation_rejected, без списания."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=5)
        words = [{"civilian_word": "BadWord", "impostor_word": None, "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=False)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "food", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        assert resp.json()["fallback_reason"] == "moderation_rejected"
        assert resp.json()["tokens_remaining"] == 5
        assert await get_user_tokens(db_session, did) == 5

    async def test_llm_timeout_ai_unavailable_no_spend(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Premium, LLM timeout → ai_unavailable, без списания."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=5)

        async def _timeout(*a, **kw):
            raise asyncio.TimeoutError()

        with patch("routers.ai._generate_with_llm", new=_timeout):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "nature", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        assert resp.json()["fallback_reason"] == "ai_unavailable"
        assert resp.json()["tokens_remaining"] == 5
        assert await get_user_tokens(db_session, did) == 5

    async def test_llm_exception_ai_unavailable_no_spend(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Premium, LLM кидает исключение → ai_unavailable, без списания."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=5)

        async def _crash(*a, **kw):
            raise RuntimeError("boom")

        with patch("routers.ai._generate_with_llm", new=_crash):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": "animals", "locale": "en", "mode": "standard"},
            )
        assert resp.status_code == 200
        assert resp.json()["fallback_reason"] == "ai_unavailable"
        assert resp.json()["tokens_remaining"] == 5
        assert await get_user_tokens(db_session, did) == 5


# ═══════════════════════════════════════════════════════════════════════════
# Атомарность / гонка (Шаг 8, conditional decrement)
# ═══════════════════════════════════════════════════════════════════════════
class TestRaceAndAtomicity:
    async def test_concurrent_conditional_decrement_only_one_succeeds(self):
        """Ядро ADR-003 §5: два конкурентных conditional decrement на User с tokens=cost
        → ровно один UPDATE даёт rowcount=1 (выдаст слово), второй rowcount=0
        (insufficient_tokens); баланс не уходит в минус.

        Проверяем атомарность на уровне строки User напрямую через две независимые
        сессии на общем engine — это тот самый механизм, которым эндпоинт гарантирует
        отсутствие двойного списания при гонке параллельных запросов одного device_id.
        """
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "api"))
        from conftest import _SessionLocal
        from sqlalchemy import select, update
        from models import User

        cost = 1
        uid = uuid.uuid4()
        did = uuid.uuid4()
        async with _SessionLocal() as setup:
            setup.add(User(id=uid, device_id=did, tokens=cost, is_premium=True,
                           premium_expires_at=None))
            await setup.commit()

        async def _decrement() -> int:
            """Возвращает rowcount conditional decrement в отдельной сессии."""
            async with _SessionLocal() as s:
                stmt = (
                    update(User)
                    .where(User.id == uid, User.tokens >= cost)
                    .values(tokens=User.tokens - cost)
                )
                res = await s.execute(stmt)
                await s.commit()
                return res.rowcount

        rc1, rc2 = await asyncio.gather(_decrement(), _decrement())

        # Ровно один decrement затронул строку (rowcount=1), второй — нет (rowcount=0).
        assert sorted([rc1, rc2]) == [0, 1], f"rowcounts={rc1},{rc2}"

        # Баланс не ушёл в минус — ровно 0 после единственного списания.
        async with _SessionLocal() as check:
            bal = (await check.execute(
                select(User.tokens).where(User.id == uid))).scalar_one()
        assert bal == 0

    async def test_sequential_exhaustion_second_gets_insufficient(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        """Полный HTTP-стек: premium с tokens=1, cost=1. Первый запрос выдаёт слово
        и списывает (баланс→0); второй (баланс<cost) → insufficient_tokens, без
        второго списания, баланс не в минусе. Эквивалент исхода гонки."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=1)
        topic, locale = "exhaust", "en"
        cached = {"civilian_word": "OnlyWord", "impostor_word": None, "difficulty": "easy"}
        await fake_redis.sadd(f"topic_pool:{topic}:{locale}", json.dumps(cached))

        llm = AsyncMock()
        with patch("routers.ai._generate_with_llm", new=llm):
            first = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": topic, "locale": locale, "mode": "standard"},
            )
            second = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": topic, "locale": locale, "mode": "standard"},
            )

        b1, b2 = first.json(), second.json()
        assert b1["fallback_used"] is False
        assert b1["tokens_remaining"] == 0
        assert b2["fallback_used"] is True
        assert b2["fallback_reason"] == "insufficient_tokens"
        assert b2["tokens_remaining"] == 0
        llm.assert_not_awaited()
        assert await get_user_tokens(db_session, did) == 0  # не в минусе


# ═══════════════════════════════════════════════════════════════════════════
# Best-effort пост-commit (Шаг 9-10) — сбой Redis/лог не ломает ответ
# ═══════════════════════════════════════════════════════════════════════════
class TestBestEffortPostCommit:
    async def test_redis_history_failure_still_returns_word_token_spent(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        """Сбой r.sadd(history) пост-commit → эндпоинт всё равно 200 + слово, токен списан."""
        _, did = await create_premium_user(db_session, tokens=5)
        topic, locale = "be-redis", "en"
        cached = {"civilian_word": "PaidWord", "impostor_word": None, "difficulty": "easy"}
        await fake_redis.sadd(f"topic_pool:{topic}:{locale}", json.dumps(cached))

        orig_sadd = fake_redis.sadd
        calls = {"n": 0}

        async def _flaky_sadd(key, *vals):
            # первый sadd (pool read уже прошёл) — это history sadd → падает
            if key.startswith("device_history:"):
                raise RuntimeError("redis down")
            return await orig_sadd(key, *vals)

        with patch.object(fake_redis, "sadd", side_effect=_flaky_sadd):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": topic, "locale": locale, "mode": "standard"},
            )
        assert resp.status_code == 200  # не 500
        body = resp.json()
        assert body["fallback_used"] is False
        assert body["civilian_word"] == "PaidWord"
        assert body["tokens_remaining"] == 4
        assert await get_user_tokens(db_session, did) == 4  # токен списан

    async def test_audit_log_failure_still_returns_word_token_spent(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        """Сбой _log пост-commit → эндпоинт всё равно 200 + слово, токен списан."""
        _, did = await create_premium_user(db_session, tokens=5)
        topic, locale = "be-log", "en"
        cached = {"civilian_word": "LoggedWord", "impostor_word": None, "difficulty": "easy"}
        await fake_redis.sadd(f"topic_pool:{topic}:{locale}", json.dumps(cached))

        async def _boom_log(*a, **kw):
            raise RuntimeError("db log unavailable")

        with patch("routers.ai._log", side_effect=_boom_log):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(did),
                json={"topic": topic, "locale": locale, "mode": "standard"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is False
        assert body["civilian_word"] == "LoggedWord"
        assert body["tokens_remaining"] == 4
        assert await get_user_tokens(db_session, did) == 4


# ═══════════════════════════════════════════════════════════════════════════
# Валидация / авторизация (Шаги 1-2) — до premium-gating
# ═══════════════════════════════════════════════════════════════════════════
class TestValidationAndAuth:
    async def test_empty_topic_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(DEVICE),
            json={"topic": "   ", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 400

    async def test_special_chars_only_topic_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(DEVICE),
            json={"topic": "!!!###$$$", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 400

    async def test_invalid_device_id_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers={"X-Api-Key": "test-api-key", "X-Device-Id": "bad-uuid"},
            json={"topic": "space", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 400

    async def test_missing_api_key_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers={"X-Device-Id": str(uuid.uuid4())},
            json={"topic": "space", "locale": "en"},
        )
        assert resp.status_code == 422

    async def test_wrong_api_key_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers={"X-Api-Key": "bad", "X-Device-Id": str(uuid.uuid4())},
            json={"topic": "space", "locale": "en"},
        )
        assert resp.status_code == 401

    async def test_never_returns_429(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        """Регресс ADR-003: эндпоинт нигде не отдаёт 429 (rate-limit → 200 fallback)."""
        await _ensure_fallback_pool(db_session)
        _, did = await create_premium_user(db_session, tokens=5)
        import time
        now = time.time()
        key = f"rate:ai_theme:{did}"
        for i in range(100):
            await fake_redis.zadd(key, {str(now + i): now + i})
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(did),
            json={"topic": "food", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code != 429
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# generate-words (admin) — регресс: токеномикой НЕ затронут (ADR-003 §8)
# ═══════════════════════════════════════════════════════════════════════════
class TestGenerateWords:
    async def test_requires_admin_key(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-words",
            headers={"X-Api-Key": "test-api-key"},
            json={"topic": "animals", "locale": "en", "count": 10},
        )
        assert resp.status_code == 422

    async def test_wrong_admin_key_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-words",
            headers={"X-Admin-Api-Key": "wrong"},
            json={"topic": "animals", "locale": "en", "count": 10},
        )
        assert resp.status_code == 401

    async def test_returns_words_list_no_token_spend(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Admin-эндпоинт не списывает токены даже у существующего premium-юзера."""
        _, did = await create_premium_user(db_session, tokens=5)
        words = [
            {"civilian_word": f"Word{i}", "impostor_word": None, "difficulty": "easy"}
            for i in range(10)
        ]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-words",
                headers=admin_headers(),
                json={"topic": "animals", "locale": "en", "count": 10},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "words" in body
        assert body["total"] == 10
        # баланс не затронут admin-эндпоинтом
        assert await get_user_tokens(db_session, did) == 5

    async def test_empty_topic_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-words",
            headers=admin_headers(),
            json={"topic": "  ", "locale": "en", "count": 10},
        )
        assert resp.status_code == 400

    async def test_llm_unavailable_returns_503(self, client: AsyncClient):
        async def _fail(*a, **kw):
            raise RuntimeError("LLM down")

        with patch("routers.ai._generate_with_llm", new=_fail):
            resp = await client.post(
                "/v1/ai/generate-words",
                headers=admin_headers(),
                json={"topic": "animals", "locale": "en", "count": 10},
            )
        assert resp.status_code == 503

    async def test_unsafe_words_filtered(self, client: AsyncClient):
        words = [
            {"civilian_word": "Safe", "impostor_word": None, "difficulty": "easy"},
            {"civilian_word": "Unsafe", "impostor_word": None, "difficulty": "easy"},
        ]
        safe_flags = [True, False]
        call_count = {"n": 0}

        async def _mod(text):
            idx = call_count["n"] % len(safe_flags)
            call_count["n"] += 1
            return safe_flags[idx]

        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=_mod),
        ):
            resp = await client.post(
                "/v1/ai/generate-words",
                headers=admin_headers(),
                json={"topic": "mix", "locale": "en", "count": 10},
            )
        body = resp.json()
        assert body["unsafe_filtered"] >= 1

"""
tests/ai — AI theme generation tests.

All OpenAI calls are mocked — no real API calls are made.

Covers:
  POST /v1/ai/generate-theme
    - Blacklisted topic → fallback (moderation_rejected), no LLM call
    - Empty topic → 400
    - LLM success path → civilian_word returned, fallback_used=False
    - LLM timeout → fallback (ai_unavailable)
    - LLM exception → fallback (ai_unavailable)
    - Moderation rejects all words → fallback (moderation_rejected)
    - Redis pool cache hit → word served from pool, LLM not called
    - Rate limit exceeded → fallback (rate_limit_exceeded), 200 not 429
    - Rate limit headers present on success
    - Invalid device_id → 400
    - Missing X-Api-Key → 422
    - Wrong X-Api-Key → 401
    - mode=party includes impostor_word
    - mode=standard excludes impostor_word

  POST /v1/ai/generate-words  (admin)
    - Requires X-Admin-Api-Key
    - Returns words list with total
    - Empty topic → 400
    - LLM unavailable → 503
"""
import uuid
import json
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from helpers import api_headers, admin_headers, create_category, create_word_pack


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


# ── generate-theme ─────────────────────────────────────────────────────────

class TestGenerateTheme:
    async def test_blacklisted_topic_returns_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        cat = await create_category(db_session, slug="ai-blacklist")
        await create_word_pack(db_session, cat.id)

        resp = await client.post(
            "/v1/ai/generate-theme",
            headers={**api_headers(DEVICE)},
            json={"topic": "sex drugs", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "moderation_rejected"

    async def test_empty_topic_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(DEVICE),
            json={"topic": "   ", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 400

    async def test_special_chars_only_topic_returns_400(self, client: AsyncClient):
        # after _sanitize("!!!###") → "" → should be rejected with 400
        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(DEVICE),
            json={"topic": "!!!###$$$", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 400

    async def test_llm_success_no_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        words = [{"civilian_word": "Mars", "impostor_word": "Moon", "difficulty": "medium"}]
        llm_resp = _llm_response(words)
        mod_resp = _moderation_safe()

        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "space", "locale": "en", "mode": "standard"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is False
        assert body["civilian_word"] == "Mars"

    async def test_llm_timeout_uses_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        cat = await create_category(db_session, slug="ai-timeout")
        await create_word_pack(db_session, cat.id)

        async def _timeout(*a, **kw):
            raise asyncio.TimeoutError()

        with patch("routers.ai._generate_with_llm", new=_timeout):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "nature", "locale": "en", "mode": "standard"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "ai_unavailable"

    async def test_llm_exception_uses_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        cat = await create_category(db_session, slug="ai-exc")
        await create_word_pack(db_session, cat.id)

        async def _crash(*a, **kw):
            raise RuntimeError("boom")

        with patch("routers.ai._generate_with_llm", new=_crash):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "animals", "locale": "en", "mode": "standard"},
            )

        assert resp.status_code == 200
        assert resp.json()["fallback_reason"] == "ai_unavailable"

    async def test_moderation_rejects_all_uses_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        cat = await create_category(db_session, slug="ai-mod-reject")
        await create_word_pack(db_session, cat.id)

        words = [{"civilian_word": "BadWord", "impostor_word": None, "difficulty": "easy"}]

        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=False)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "food", "locale": "en", "mode": "standard"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "moderation_rejected"

    async def test_redis_pool_cache_hit_skips_llm(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        topic = "cached-topic"
        locale = "en"
        cached = {"civilian_word": "CachedWord", "impostor_word": None, "difficulty": "easy"}
        pool_key = f"topic_pool:{topic}:{locale}"
        await fake_redis.sadd(pool_key, json.dumps(cached))

        llm_called = False

        async def _llm_should_not_be_called(*a, **kw):
            nonlocal llm_called
            llm_called = True
            return []

        with patch("routers.ai._generate_with_llm", new=_llm_should_not_be_called):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": topic, "locale": locale, "mode": "standard"},
            )

        assert resp.status_code == 200
        assert resp.json()["civilian_word"] == "CachedWord"
        assert not llm_called

    async def test_rate_limit_exceeded_returns_200_with_fallback(
        self, client: AsyncClient, db_session: AsyncSession, fake_redis
    ):
        cat = await create_category(db_session, slug="ai-rl")
        await create_word_pack(db_session, cat.id)
        did = str(uuid.uuid4())

        import time
        now = time.time()
        key = f"rate:ai_theme:{did}"
        for i in range(6):
            await fake_redis.zadd(key, {str(now + i): now + i})

        resp = await client.post(
            "/v1/ai/generate-theme",
            headers=api_headers(did),
            json={"topic": "food", "locale": "en", "mode": "standard"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fallback_used"] is True
        assert body["fallback_reason"] == "rate_limit_exceeded"

    async def test_rate_limit_headers_on_success(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        words = [{"civilian_word": "Sun", "impostor_word": None, "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "weather", "locale": "en", "mode": "standard"},
            )
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers
        assert "x-ratelimit-reset" in resp.headers

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

    async def test_party_mode_includes_impostor_word(
        self, client: AsyncClient
    ):
        words = [{"civilian_word": "Cat", "impostor_word": "Kitten", "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "pets", "locale": "en", "mode": "party"},
            )
        assert resp.json()["impostor_word"] == "Kitten"

    async def test_standard_mode_excludes_impostor_word(
        self, client: AsyncClient
    ):
        words = [{"civilian_word": "Cat", "impostor_word": "Kitten", "difficulty": "easy"}]
        with (
            patch("routers.ai._generate_with_llm", new=AsyncMock(return_value=words)),
            patch("routers.ai._check_moderation", new=AsyncMock(return_value=True)),
        ):
            resp = await client.post(
                "/v1/ai/generate-theme",
                headers=api_headers(str(uuid.uuid4())),
                json={"topic": "pets", "locale": "en", "mode": "standard"},
            )
        assert resp.json()["impostor_word"] is None


# ── generate-words (admin) ─────────────────────────────────────────────────

class TestGenerateWords:
    async def test_requires_admin_key(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-words",
            headers={"X-Api-Key": "test-api-key"},  # client key, not admin
            json={"topic": "animals", "locale": "en", "count": 10},
        )
        assert resp.status_code == 422  # missing header → 422

    async def test_wrong_admin_key_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/v1/ai/generate-words",
            headers={"X-Admin-Api-Key": "wrong"},
            json={"topic": "animals", "locale": "en", "count": 10},
        )
        assert resp.status_code == 401

    async def test_returns_words_list(self, client: AsyncClient):
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

"""
tests/config — Remote config and localizations tests.

Covers:
  GET /v1/config
    - Returns 200 with default config structure
    - Has required top-level keys (features, content, app)
    - DB overrides merge into defaults
    - updated_at field present
    - Requires X-Api-Key

  GET /v1/localizations
    - Returns strings for locale=en
    - Returns strings for locale=ru
    - Unknown locale falls back to en
    - key filter returns only requested keys
    - Missing locale param → 422
    - Requires X-Api-Key
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from helpers import api_headers


class TestGetConfig:
    async def test_returns_200(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert resp.status_code == 200

    async def test_has_features_key(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert "features" in resp.json()

    async def test_has_content_key(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert "content" in resp.json()

    async def test_has_app_key(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert "app" in resp.json()

    async def test_has_updated_at(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert "updated_at" in resp.json()

    async def test_default_ai_theme_enabled(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert resp.json()["features"]["ai_theme"]["enabled"] is True

    async def test_db_override_merges(self, client: AsyncClient, db_session: AsyncSession):
        from models import FeatureConfig
        fc = FeatureConfig(key="custom_flag", value={"enabled": False})
        db_session.add(fc)
        await db_session.flush()

        resp = await client.get("/v1/config", headers=api_headers())
        assert resp.json()["custom_flag"] == {"enabled": False}

    async def test_requires_api_key(self, client: AsyncClient):
        resp = await client.get("/v1/config")
        assert resp.status_code == 422

    async def test_wrong_api_key_returns_401(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers={"X-Api-Key": "bad"})
        assert resp.status_code == 401


class TestGetLocalizations:
    async def test_en_locale_returns_strings(self, client: AsyncClient):
        resp = await client.get("/v1/localizations?locale=en", headers=api_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["locale"] == "en"
        assert "strings" in body
        assert len(body["strings"]) > 0

    async def test_ru_locale_returns_strings(self, client: AsyncClient):
        resp = await client.get("/v1/localizations?locale=ru", headers=api_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["locale"] == "ru"
        assert "paywall.title" in body["strings"]

    async def test_unknown_locale_falls_back_to_en(self, client: AsyncClient):
        resp = await client.get("/v1/localizations?locale=de", headers=api_headers())
        assert resp.status_code == 200
        # falls back to en strings
        assert len(resp.json()["strings"]) > 0

    async def test_key_filter_returns_subset(self, client: AsyncClient):
        resp = await client.get(
            "/v1/localizations?locale=en&keys=paywall.title",
            headers=api_headers(),
        )
        strings = resp.json()["strings"]
        assert "paywall.title" in strings
        assert "paywall.subtitle" not in strings

    async def test_multiple_key_filter(self, client: AsyncClient):
        resp = await client.get(
            "/v1/localizations?locale=en&keys=paywall.title,paywall.subtitle",
            headers=api_headers(),
        )
        strings = resp.json()["strings"]
        assert "paywall.title" in strings
        assert "paywall.subtitle" in strings

    async def test_missing_locale_returns_422(self, client: AsyncClient):
        resp = await client.get("/v1/localizations", headers=api_headers())
        assert resp.status_code == 422

    async def test_requires_api_key(self, client: AsyncClient):
        resp = await client.get("/v1/localizations?locale=en")
        assert resp.status_code == 422

    async def test_ru_paywall_title_is_russian(self, client: AsyncClient):
        resp = await client.get("/v1/localizations?locale=ru", headers=api_headers())
        title = resp.json()["strings"]["paywall.title"]
        # should contain at least one Cyrillic character
        assert any("Ѐ" <= c <= "ӿ" for c in title)

    async def test_cache_control_header(self, client: AsyncClient):
        resp = await client.get("/v1/localizations?locale=en", headers=api_headers())
        assert resp.headers.get("cache-control") == "max-age=3600"


class TestConfigCacheControl:
    async def test_cache_control_header(self, client: AsyncClient):
        resp = await client.get("/v1/config", headers=api_headers())
        assert resp.headers.get("cache-control") == "max-age=900"

"""
tests/auth — API key authentication guard tests.

Covers:
  - Missing X-Api-Key → 422 (FastAPI validation)
  - Wrong X-Api-Key → 401
  - Correct X-Api-Key → request proceeds (not 401)
  - Missing X-Admin-Api-Key on admin endpoint → 422
  - Wrong X-Admin-Api-Key → 401
  - Correct X-Admin-Api-Key → request proceeds
"""
import uuid
import pytest
from httpx import AsyncClient
from helpers import API_KEY, ADMIN_KEY


# ── client-facing key (X-Api-Key) ─────────────────────────────────────────

class TestApiKeyGuard:
    async def test_missing_key_returns_422(self, client: AsyncClient):
        resp = await client.get("/v1/categories")
        assert resp.status_code == 422

    async def test_wrong_key_returns_401(self, client: AsyncClient):
        resp = await client.get("/v1/categories", headers={"X-Api-Key": "wrong"})
        assert resp.status_code == 401

    async def test_correct_key_is_accepted(self, client: AsyncClient):
        resp = await client.get("/v1/categories", headers={"X-Api-Key": API_KEY})
        assert resp.status_code != 401

    async def test_401_body_contains_detail(self, client: AsyncClient):
        resp = await client.get("/v1/categories", headers={"X-Api-Key": "bad"})
        assert "detail" in resp.json()

    async def test_empty_string_key_rejected(self, client: AsyncClient):
        resp = await client.get("/v1/categories", headers={"X-Api-Key": ""})
        assert resp.status_code == 401


# ── admin key (X-Admin-Api-Key) ────────────────────────────────────────────

class TestAdminApiKeyGuard:
    async def test_missing_admin_key_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/v1/admin/users/tokens",
            json={"device_id": str(uuid.uuid4()), "tokens_amount": 10},
        )
        assert resp.status_code == 422

    async def test_wrong_admin_key_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers={"X-Admin-Api-Key": "wrong"},
            json={"device_id": str(uuid.uuid4()), "tokens_amount": 10},
        )
        assert resp.status_code == 401

    async def test_correct_admin_key_accepted(self, client: AsyncClient):
        # User doesn't exist so it's 404 but NOT 401 — key was accepted.
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers={"X-Admin-Api-Key": ADMIN_KEY},
            json={"device_id": str(uuid.uuid4()), "tokens_amount": 10},
        )
        assert resp.status_code != 401

    async def test_client_key_rejected_on_admin_endpoint(self, client: AsyncClient):
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers={"X-Admin-Api-Key": API_KEY},  # wrong key type
            json={"device_id": str(uuid.uuid4()), "tokens_amount": 10},
        )
        assert resp.status_code == 401

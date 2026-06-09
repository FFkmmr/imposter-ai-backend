"""
tests/admin — Admin token management tests.

Covers:
  POST /v1/admin/users/tokens
    - Requires X-Admin-Api-Key (missing → 422, wrong → 401)
    - Known user → 200, tokens incremented
    - Multiple calls accumulate tokens correctly
    - Unknown device_id → 404
    - tokens_amount < 1 → 422 (Field ge=1 constraint)
    - Missing tokens_amount → 422
    - Invalid device_id format → 422
    - Response shape contains expected fields
"""
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from helpers import admin_headers, create_user


class TestAddTokens:
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

    async def test_known_user_returns_200(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)

        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": 10},
        )
        assert resp.status_code == 200

    async def test_tokens_incremented(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)

        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": 42},
        )
        assert resp.json()["tokens"] == 42

    async def test_multiple_calls_accumulate(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)

        await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": 10},
        )
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": 5},
        )
        assert resp.json()["tokens"] == 15

    async def test_unknown_device_returns_404(self, client: AsyncClient):
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": str(uuid.uuid4()), "tokens_amount": 10},
        )
        assert resp.status_code == 404

    async def test_zero_tokens_amount_returns_422(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)

        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": 0},
        )
        assert resp.status_code == 422

    async def test_negative_tokens_amount_returns_422(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)

        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": -5},
        )
        assert resp.status_code == 422

    async def test_missing_tokens_amount_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 422

    async def test_invalid_device_id_format_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": "not-a-uuid", "tokens_amount": 10},
        )
        assert resp.status_code == 422

    async def test_response_shape(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)

        resp = await client.post(
            "/v1/admin/users/tokens",
            headers=admin_headers(),
            json={"device_id": did, "tokens_amount": 1},
        )
        body = resp.json()
        assert "id" in body
        assert "device_id" in body
        assert "tokens" in body
        assert "is_premium" in body
        assert "created_at" in body

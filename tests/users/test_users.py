"""
tests/users — User registration and profile tests.

Covers:
  POST /v1/users
    - New device_id → 201, returns access_token + profile
    - Same device_id again → 200 (idempotent), same user id
    - Invalid device_id (not UUID) → 422
    - Missing device_id → 422

  GET /v1/users/me
    - Valid device → 200, profile matches
    - Unknown device → 404
    - Missing X-Device-Id → 422
    - Invalid X-Device-Id format → 400
    - Missing X-Api-Key → 422
    - Wrong X-Api-Key → 401
"""
import uuid
import pytest
from httpx import AsyncClient
from helpers import api_headers, create_user


class TestCreateUser:
    async def test_new_user_returns_201(self, client: AsyncClient):
        resp = await client.post("/v1/users", json={"device_id": str(uuid.uuid4())})
        assert resp.status_code == 201

    async def test_new_user_response_shape(self, client: AsyncClient):
        resp = await client.post("/v1/users", json={"device_id": str(uuid.uuid4())})
        body = resp.json()
        assert "id" in body
        assert "device_id" in body
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["tokens"] == 0
        assert body["is_premium"] is False

    async def test_existing_user_returns_200(self, client: AsyncClient):
        did = str(uuid.uuid4())
        r1 = await client.post("/v1/users", json={"device_id": did})
        r2 = await client.post("/v1/users", json={"device_id": did})
        assert r1.status_code == 201
        assert r2.status_code == 200

    async def test_existing_user_returns_same_id(self, client: AsyncClient):
        did = str(uuid.uuid4())
        r1 = await client.post("/v1/users", json={"device_id": did})
        r2 = await client.post("/v1/users", json={"device_id": did})
        assert r1.json()["id"] == r2.json()["id"]

    async def test_each_call_returns_access_token(self, client: AsyncClient):
        did = str(uuid.uuid4())
        r1 = await client.post("/v1/users", json={"device_id": did})
        r2 = await client.post("/v1/users", json={"device_id": did})
        assert r1.json()["access_token"]
        assert r2.json()["access_token"]

    async def test_invalid_device_id_returns_422(self, client: AsyncClient):
        resp = await client.post("/v1/users", json={"device_id": "not-a-uuid"})
        assert resp.status_code == 422

    async def test_missing_device_id_returns_422(self, client: AsyncClient):
        resp = await client.post("/v1/users", json={})
        assert resp.status_code == 422

    async def test_different_devices_get_different_ids(self, client: AsyncClient):
        r1 = await client.post("/v1/users", json={"device_id": str(uuid.uuid4())})
        r2 = await client.post("/v1/users", json={"device_id": str(uuid.uuid4())})
        assert r1.json()["id"] != r2.json()["id"]


class TestGetProfile:
    async def test_known_user_returns_200(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)
        resp = await client.get("/v1/users/me", headers=api_headers(did))
        assert resp.status_code == 200

    async def test_profile_fields(self, client: AsyncClient):
        did = str(uuid.uuid4())
        await create_user(client, did)
        resp = await client.get("/v1/users/me", headers=api_headers(did))
        body = resp.json()
        assert body["device_id"] == did
        assert "id" in body
        assert "tokens" in body
        assert "is_premium" in body
        assert "created_at" in body

    async def test_unknown_device_returns_404(self, client: AsyncClient):
        did = str(uuid.uuid4())
        resp = await client.get("/v1/users/me", headers=api_headers(did))
        assert resp.status_code == 404

    async def test_missing_device_id_header_returns_422(self, client: AsyncClient):
        resp = await client.get("/v1/users/me", headers={"X-Api-Key": "test-api-key"})
        assert resp.status_code == 422

    async def test_invalid_device_id_format_returns_400(self, client: AsyncClient):
        resp = await client.get(
            "/v1/users/me",
            headers={"X-Api-Key": "test-api-key", "X-Device-Id": "not-a-uuid"},
        )
        assert resp.status_code == 400

    async def test_missing_api_key_returns_422(self, client: AsyncClient):
        did = str(uuid.uuid4())
        resp = await client.get("/v1/users/me", headers={"X-Device-Id": did})
        assert resp.status_code == 422

    async def test_wrong_api_key_returns_401(self, client: AsyncClient):
        did = str(uuid.uuid4())
        resp = await client.get(
            "/v1/users/me",
            headers={"X-Api-Key": "wrong", "X-Device-Id": did},
        )
        assert resp.status_code == 401

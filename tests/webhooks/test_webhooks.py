"""
tests/webhooks — Adapty subscription webhook tests.

Covers:
  POST /v1/webhooks/adapty
    - Valid payload → 200 {"status": "ok"}
    - Missing Adapty-Signature when secret configured → 401
    - Invalid Adapty-Signature → 401
    - Valid Adapty-Signature → 200
    - No secret configured → signature check skipped → 200
    - Invalid JSON → 400
    - subscription_renewed → user.is_premium=True, premium_expires_at set
    - subscription_initial_purchase → user.is_premium=True
    - subscription_expired → user.is_premium=False, premium_expires_at cleared
    - subscription_cancelled → user.is_premium=False
    - Unknown event_type → 200 (no-op, idempotent)
    - customer_user_id=null → 200 (skipped gracefully)
    - Extra unknown fields tolerated (extra="allow")
    - Does NOT require X-Api-Key
"""
import hashlib
import hmac
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from helpers import create_user
from models import User


VALID_PAYLOAD = {
    "event_type": "subscription_renewed",
    "profile_id": "prof-123",
    "customer_user_id": None,  # will be set per test with real device_id
    "paid_access_level": {
        "id": "premium",
        "is_active": True,
        "expires_at": "2099-01-01T00:00:00Z",
    },
}


def _make_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestAdaptyWebhookSignature:
    async def test_no_secret_configured_accepts_without_signature(self, client: AsyncClient):
        """When adapty_webhook_secret is empty, signature check is skipped."""
        with patch("routers.webhooks.get_settings") as mock_settings:
            mock_settings.return_value.adapty_webhook_secret = ""
            resp = await client.post("/v1/webhooks/adapty", json=VALID_PAYLOAD)
        assert resp.status_code == 200

    async def test_secret_configured_missing_signature_returns_401(self, client: AsyncClient):
        with patch("routers.webhooks.get_settings") as mock_settings:
            mock_settings.return_value.adapty_webhook_secret = "mysecret"
            resp = await client.post("/v1/webhooks/adapty", json=VALID_PAYLOAD)
        assert resp.status_code == 401

    async def test_secret_configured_wrong_signature_returns_401(self, client: AsyncClient):
        with patch("routers.webhooks.get_settings") as mock_settings:
            mock_settings.return_value.adapty_webhook_secret = "mysecret"
            resp = await client.post(
                "/v1/webhooks/adapty",
                headers={"Adapty-Signature": "sha256=deadbeef"},
                json=VALID_PAYLOAD,
            )
        assert resp.status_code == 401

    async def test_secret_configured_valid_signature_returns_200(self, client: AsyncClient):
        import json as _json
        secret = "mysecret"
        body = _json.dumps(VALID_PAYLOAD).encode()
        sig = _make_signature(body, secret)
        with patch("routers.webhooks.get_settings") as mock_settings:
            mock_settings.return_value.adapty_webhook_secret = secret
            resp = await client.post(
                "/v1/webhooks/adapty",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Adapty-Signature": sig,
                },
            )
        assert resp.status_code == 200


class TestAdaptyWebhookPayload:
    async def test_response_body_is_ok(self, client: AsyncClient):
        resp = await client.post("/v1/webhooks/adapty", json=VALID_PAYLOAD)
        assert resp.json() == {"status": "ok"}

    async def test_invalid_json_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/v1/webhooks/adapty",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_does_not_require_api_key(self, client: AsyncClient):
        resp = await client.post("/v1/webhooks/adapty", json=VALID_PAYLOAD)
        assert resp.status_code not in (401, 422)

    async def test_extra_fields_tolerated(self, client: AsyncClient):
        payload = {
            **VALID_PAYLOAD,
            "unknown_future_field": "some_value",
            "another_extra": 42,
        }
        resp = await client.post("/v1/webhooks/adapty", json=payload)
        assert resp.status_code == 200

    async def test_null_customer_user_id_accepted(self, client: AsyncClient):
        payload = {**VALID_PAYLOAD, "customer_user_id": None}
        resp = await client.post("/v1/webhooks/adapty", json=payload)
        assert resp.status_code == 200

    async def test_minimal_payload_accepted(self, client: AsyncClient):
        resp = await client.post(
            "/v1/webhooks/adapty",
            json={"event_type": "subscription_renewed", "profile_id": "p1"},
        )
        assert resp.status_code == 200

    async def test_unknown_event_type_accepted(self, client: AsyncClient):
        payload = {**VALID_PAYLOAD, "event_type": "some_future_event"}
        resp = await client.post("/v1/webhooks/adapty", json=payload)
        assert resp.status_code == 200


class TestAdaptyWebhookPremiumActivation:
    async def test_subscription_renewed_sets_premium(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        payload = {
            "event_type": "subscription_renewed",
            "profile_id": "prof-1",
            "customer_user_id": did,
            "paid_access_level": {
                "id": "premium",
                "is_active": True,
                "expires_at": "2099-06-01T00:00:00Z",
            },
        }
        resp = await client.post("/v1/webhooks/adapty", json=payload)
        assert resp.status_code == 200

        db_session.expire_all()
        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        assert user.is_premium is True
        assert user.premium_expires_at is not None

    async def test_subscription_initial_purchase_sets_premium(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        payload = {
            "event_type": "subscription_initial_purchase",
            "profile_id": "prof-2",
            "customer_user_id": did,
            "paid_access_level": {"id": "premium", "is_active": True, "expires_at": None},
        }
        resp = await client.post("/v1/webhooks/adapty", json=payload)
        assert resp.status_code == 200

        db_session.expire_all()
        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        assert user.is_premium is True

    async def test_subscription_expired_clears_premium(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        # activate first
        await client.post("/v1/webhooks/adapty", json={
            "event_type": "subscription_initial_purchase",
            "profile_id": "prof-3",
            "customer_user_id": did,
            "paid_access_level": {"id": "premium", "is_active": True, "expires_at": None},
        })

        resp = await client.post("/v1/webhooks/adapty", json={
            "event_type": "subscription_expired",
            "profile_id": "prof-3",
            "customer_user_id": did,
        })
        assert resp.status_code == 200

        db_session.expire_all()
        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        assert user.is_premium is False
        assert user.premium_expires_at is None

    async def test_subscription_cancelled_clears_premium(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        await client.post("/v1/webhooks/adapty", json={
            "event_type": "subscription_initial_purchase",
            "profile_id": "prof-4",
            "customer_user_id": did,
            "paid_access_level": {"id": "premium", "is_active": True, "expires_at": None},
        })

        resp = await client.post("/v1/webhooks/adapty", json={
            "event_type": "subscription_cancelled",
            "profile_id": "prof-4",
            "customer_user_id": did,
        })
        assert resp.status_code == 200

        db_session.expire_all()
        result = await db_session.execute(
            select(User).where(User.device_id == uuid.UUID(did))
        )
        user = result.scalar_one()
        assert user.is_premium is False

    async def test_unknown_customer_user_id_is_ignored(self, client: AsyncClient):
        """Non-existent user → webhook still returns 200, no crash."""
        resp = await client.post("/v1/webhooks/adapty", json={
            "event_type": "subscription_renewed",
            "profile_id": "prof-x",
            "customer_user_id": str(uuid.uuid4()),
            "paid_access_level": {"id": "premium", "is_active": True, "expires_at": None},
        })
        assert resp.status_code == 200

"""
tests/webhooks — Adapty subscription webhook tests (NEW contract, ADR-002).

Endpoint: POST /v1/billing/adapty/webhook
Auth:     Authorization: Bearer <ADAPTY_WEBHOOK_SECRET> (static secret, constant-time, NOT HMAC)
Economy:  token grants per vendor_product_id tier (weekly/yearly/fallback)
Idempotency: ProcessedWebhookEvent ledger keyed by event_id

The old HMAC webhook (POST /v1/webhooks/adapty, Adapty-Signature) is removed —
this file is fully rewritten for the new contract.

Covered (per backend_spec.md §4 and §770):
  Auth:
    - missing Authorization → 401
    - wrong token → 401
    - wrong scheme (Basic) → 401
    - empty ADAPTY_WEBHOOK_SECRET → 500
  Tolerance (authorized but unprocessable → 200 ignored):
    - empty_body, invalid_json, not_an_object, missing_event_id,
      unknown event_type, missing_customer_user_id,
      non-UUID customer_user_id, user_not_found
  Applied:
    - subscription_started (weekly)  → is_premium=True, premium_expires_at set,
      tokens += weekly grant
    - subscription_renewed           → tokens granted
    - subscription_cancelled         → is_premium=False, tokens UNCHANGED
    - subscription_expired           → is_premium=False, tokens UNCHANGED
  Tiers:
    - weekly product → weekly grant
    - yearly product → yearly grant
    - unknown product → fallback grant
  Idempotency:
    - repeat same event_id → 200 duplicate, balance unchanged, ledger not doubled
  Defensive parsing:
    - customer_user_id in profile.customer_user_id and in user_id
    - vendor_product_id in different locations
    - profile not a dict / event_properties missing → no 5xx
"""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpers import create_user
from models import ProcessedWebhookEvent, User

WEBHOOK_URL = "/v1/billing/adapty/webhook"

# Deterministic tier config used by all tests.
SECRET = "test-adapty-secret-bearer-9f3a1c"
PRODUCT_WEEKLY = "sub_weekly_test"
PRODUCT_YEARLY = "sub_yearly_test"
TOKENS_WEEKLY = 100
TOKENS_YEARLY = 1500
TOKENS_FALLBACK = 50


class _FakeSettings:
    """Minimal stand-in for config.Settings used by the webhook router.

    The router reads only these attributes; we mock get_settings() so the
    product_id → grant mapping and the bearer secret are fully deterministic
    and independent of process env."""

    adapty_webhook_secret = SECRET
    subscription_product_weekly = PRODUCT_WEEKLY
    subscription_product_yearly = PRODUCT_YEARLY
    subscription_tokens_weekly = TOKENS_WEEKLY
    subscription_tokens_yearly = TOKENS_YEARLY
    subscription_tokens_grant = TOKENS_FALLBACK


@contextmanager
def override_settings(**overrides):
    """Patch routers.webhooks.get_settings to return a deterministic Settings.

    The router calls get_settings() directly (not via Depends), so patching the
    symbol in the router module is the correct seam. Defaults to _FakeSettings;
    pass overrides (e.g. adapty_webhook_secret="") to vary individual fields."""
    fake = _FakeSettings()
    for k, v in overrides.items():
        setattr(fake, k, v)
    with patch("routers.webhooks.get_settings", return_value=fake):
        yield fake


def auth_headers(token: str = SECRET) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _get_user(db: AsyncSession, device_id: str) -> User:
    db.expire_all()
    result = await db.execute(select(User).where(User.device_id == uuid.UUID(device_id)))
    return result.scalar_one()


def started_payload(device_id: str, *, event_id: str, product_id: str,
                    expires_at: str | None = "2099-06-01T00:00:00Z") -> dict:
    props: dict = {"vendor_product_id": product_id}
    if expires_at is not None:
        props["expires_at"] = expires_at
    return {
        "event_id": event_id,
        "event_type": "subscription_started",
        "customer_user_id": device_id,
        "event_properties": props,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Authorization
# ─────────────────────────────────────────────────────────────────────────────
class TestAuth:
    async def test_missing_authorization_returns_401(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(WEBHOOK_URL, json={"event_id": "e1"})
        assert resp.status_code == 401

    async def test_wrong_token_returns_401(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers("not-the-secret"),
                json={"event_id": "e1"},
            )
        assert resp.status_code == 401

    async def test_wrong_scheme_returns_401(self, client: AsyncClient):
        """Basic scheme (not Bearer) → 401 even with correct secret value."""
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers={"Authorization": f"Basic {SECRET}"},
                json={"event_id": "e1"},
            )
        assert resp.status_code == 401

    async def test_bearer_no_token_returns_401(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers={"Authorization": "Bearer"},
                json={"event_id": "e1"},
            )
        assert resp.status_code == 401

    async def test_empty_secret_returns_500(self, client: AsyncClient):
        """ADAPTY_WEBHOOK_SECRET not configured → 500, even with a Bearer header."""
        with override_settings(adapty_webhook_secret=""):
            resp = await client.post(
                WEBHOOK_URL, headers={"Authorization": "Bearer whatever"},
                json={"event_id": "e1"},
            )
        assert resp.status_code == 500

    async def test_does_not_require_api_key(self, client: AsyncClient):
        """Webhook path is excluded from X-Api-Key protection — auth is bearer only."""
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={"event_id": "e1", "event_type": "subscription_started"},
            )
        # No X-Api-Key sent, yet not rejected for missing api key.
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Tolerance → 200 ignored
# ─────────────────────────────────────────────────────────────────────────────
class TestTolerance:
    async def test_empty_body_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), content=b"")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "empty_body"}

    async def test_invalid_json_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                content=b"this is not json",
                # send a content-type so server attempts to parse body
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "invalid_json"}

    async def test_json_array_not_an_object_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), json=[1, 2, 3])
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "not_an_object"}

    async def test_json_number_not_an_object_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), json=42)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "not_an_object"}

    async def test_missing_event_id_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={"event_type": "subscription_started"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "missing_event_id"}

    async def test_unknown_event_type_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={"event_id": "e-unk", "event_type": "subscription_paused"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ignored"
        assert body["event_type"] == "subscription_paused"

    async def test_missing_customer_user_id_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={"event_id": "e-nocid", "event_type": "subscription_started"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "missing_customer_user_id"}

    async def test_non_uuid_customer_user_id_ignored(self, client: AsyncClient):
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "e-baduuid",
                    "event_type": "subscription_started",
                    "customer_user_id": "not-a-uuid",
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "invalid_customer_user_id"}

    async def test_user_not_found_ignored(self, client: AsyncClient):
        """Well-formed UUID but no such user → 200 ignored, no crash."""
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "e-nouser",
                    "event_type": "subscription_started",
                    "customer_user_id": str(uuid.uuid4()),
                    "event_properties": {"vendor_product_id": PRODUCT_WEEKLY},
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "user_not_found"}


# ─────────────────────────────────────────────────────────────────────────────
# Applied — premium activation / deactivation
# ─────────────────────────────────────────────────────────────────────────────
class TestApplied:
    async def test_started_weekly_sets_premium_grants_tokens(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        created = await create_user(client, did)
        assert created["tokens"] == 0
        assert created["is_premium"] is False

        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(did, event_id="evt-w1", product_id=PRODUCT_WEEKLY),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "applied"
        assert body["event_type"] == "subscription_started"
        assert body["tokens_granted"] == TOKENS_WEEKLY
        assert body["is_premium"] is True

        user = await _get_user(db_session, did)
        assert user.is_premium is True
        assert user.tokens == TOKENS_WEEKLY
        assert user.premium_expires_at is not None
        # expires_at parsed from payload (2099-06-01)
        assert user.premium_expires_at.year == 2099

    async def test_renewed_grants_tokens(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        payload = started_payload(did, event_id="evt-r1", product_id=PRODUCT_YEARLY)
        payload["event_type"] = "subscription_renewed"
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["tokens_granted"] == TOKENS_YEARLY

        user = await _get_user(db_session, did)
        assert user.is_premium is True
        assert user.tokens == TOKENS_YEARLY

    async def test_cancelled_clears_premium_keeps_tokens(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        # First activate (weekly grant).
        with override_settings():
            await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(did, event_id="evt-c-act", product_id=PRODUCT_WEEKLY),
            )
        user = await _get_user(db_session, did)
        tokens_before = user.tokens
        assert tokens_before == TOKENS_WEEKLY

        # Now cancel.
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-c-cancel",
                    "event_type": "subscription_cancelled",
                    "customer_user_id": did,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "applied"
        assert body["is_premium"] is False
        assert body["tokens_granted"] == 0

        user = await _get_user(db_session, did)
        assert user.is_premium is False
        # Tokens NOT touched on cancel (contract §4).
        assert user.tokens == tokens_before

    async def test_expired_clears_premium_keeps_tokens(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)

        with override_settings():
            await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(did, event_id="evt-e-act", product_id=PRODUCT_YEARLY),
            )
        user = await _get_user(db_session, did)
        tokens_before = user.tokens
        assert tokens_before == TOKENS_YEARLY

        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-e-expire",
                    "event_type": "subscription_expired",
                    "customer_user_id": did,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["is_premium"] is False

        user = await _get_user(db_session, did)
        assert user.is_premium is False
        assert user.tokens == tokens_before


# ─────────────────────────────────────────────────────────────────────────────
# Tier resolution
# ─────────────────────────────────────────────────────────────────────────────
class TestTiers:
    @pytest.mark.parametrize(
        "product_id,expected_grant",
        [
            (PRODUCT_WEEKLY, TOKENS_WEEKLY),
            (PRODUCT_YEARLY, TOKENS_YEARLY),
            ("some_unknown_sku", TOKENS_FALLBACK),
        ],
    )
    async def test_tier_grant(
        self, client: AsyncClient, db_session: AsyncSession,
        product_id, expected_grant,
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(
                    did, event_id=f"evt-tier-{product_id}", product_id=product_id
                ),
            )
        assert resp.status_code == 200
        assert resp.json()["tokens_granted"] == expected_grant

        user = await _get_user(db_session, did)
        assert user.tokens == expected_grant

    async def test_missing_product_id_uses_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """No vendor_product_id anywhere → fallback grant (not a crash)."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-noprod",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "event_properties": {},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["tokens_granted"] == TOKENS_FALLBACK
        user = await _get_user(db_session, did)
        assert user.tokens == TOKENS_FALLBACK


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────
class TestIdempotency:
    async def test_duplicate_event_id_does_not_double_grant(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)
        payload = started_payload(did, event_id="evt-dup", product_id=PRODUCT_WEEKLY)

        with override_settings():
            first = await client.post(WEBHOOK_URL, headers=auth_headers(), json=payload)
        assert first.status_code == 200
        assert first.json()["status"] == "applied"

        user = await _get_user(db_session, did)
        assert user.tokens == TOKENS_WEEKLY

        # Replay the very same event_id.
        with override_settings():
            second = await client.post(WEBHOOK_URL, headers=auth_headers(), json=payload)
        assert second.status_code == 200
        assert second.json() == {"status": "duplicate"}

        # Balance unchanged.
        user = await _get_user(db_session, did)
        assert user.tokens == TOKENS_WEEKLY

        # Ledger has exactly one row for this event_id.
        db_session.expire_all()
        rows = (
            await db_session.execute(
                select(ProcessedWebhookEvent).where(
                    ProcessedWebhookEvent.event_id == "evt-dup"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].tokens_granted == TOKENS_WEEKLY

    async def test_event_id_from_id_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """event_id may arrive as payload.id (fallback) — still idempotent."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        payload = {
            "id": "evt-via-id",
            "event_type": "subscription_started",
            "customer_user_id": did,
            "event_properties": {"vendor_product_id": PRODUCT_WEEKLY},
        }
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"

        with override_settings():
            again = await client.post(WEBHOOK_URL, headers=auth_headers(), json=payload)
        assert again.json() == {"status": "duplicate"}


# ─────────────────────────────────────────────────────────────────────────────
# Defensive parsing
# ─────────────────────────────────────────────────────────────────────────────
class TestDefensiveParsing:
    async def test_customer_user_id_in_profile(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-profcid",
                    "event_type": "subscription_started",
                    "profile": {"customer_user_id": did},
                    "event_properties": {"vendor_product_id": PRODUCT_WEEKLY},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        user = await _get_user(db_session, did)
        assert user.is_premium is True

    async def test_customer_user_id_in_user_id_field(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-userid",
                    "event_type": "subscription_started",
                    "user_id": did,
                    "event_properties": {"vendor_product_id": PRODUCT_YEARLY},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        user = await _get_user(db_session, did)
        assert user.tokens == TOKENS_YEARLY

    async def test_vendor_product_id_top_level(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """vendor_product_id at payload top level (not under event_properties)."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-toplvlprod",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "vendor_product_id": PRODUCT_WEEKLY,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["tokens_granted"] == TOKENS_WEEKLY

    async def test_product_id_alias_under_event_properties(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """`product_id` (alias for vendor_product_id) under event_properties."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-prodalias",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "event_properties": {"product_id": PRODUCT_YEARLY},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["tokens_granted"] == TOKENS_YEARLY

    async def test_profile_not_dict_does_not_crash(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """profile is a string (malformed) → no 5xx; falls back to top-level cid."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-profstr",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "profile": "not-a-dict",
                    "event_properties": {"vendor_product_id": PRODUCT_WEEKLY},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"

    async def test_event_properties_missing_does_not_crash(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """No event_properties at all → fallback grant, no 5xx."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-noprops",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["tokens_granted"] == TOKENS_FALLBACK

    async def test_event_properties_not_dict_does_not_crash(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """event_properties is a list → defensively coerced, no 5xx, fallback grant."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-propslist",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "event_properties": ["weird"],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["tokens_granted"] == TOKENS_FALLBACK

    async def test_unparsable_expires_at_does_not_crash(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Malformed expires_at string → no 5xx; premium set, expires_at left None."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-badexp",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "event_properties": {
                        "vendor_product_id": PRODUCT_WEEKLY,
                        "expires_at": "31/12/2099 not-iso",
                    },
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        user = await _get_user(db_session, did)
        assert user.is_premium is True
        # Unparsable → premium_expires_at stays None (defensive, lines 94-96).
        assert user.premium_expires_at is None

    async def test_expires_at_from_profile_fallback(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """expires_at absent in event_properties but present in profile → used."""
        did = str(uuid.uuid4())
        await create_user(client, did)
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "evt-expprofile",
                    "event_type": "subscription_started",
                    "customer_user_id": did,
                    "profile": {"expires_at": "2099-12-31T23:59:59Z"},
                    "event_properties": {"vendor_product_id": PRODUCT_WEEKLY},
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        user = await _get_user(db_session, did)
        assert user.premium_expires_at is not None
        assert user.premium_expires_at.year == 2099


# ─────────────────────────────────────────────────────────────────────────────
# Q-BILL-2 — premium_expires_at preservation on renew/start без expires_at
# ─────────────────────────────────────────────────────────────────────────────
class TestQBill2ExpiresPreservation:
    """ADR: activation-событие БЕЗ expires_at сохраняет прежний premium_expires_at
    (не обнуляет), С expires_at — обновляет; deactivation не трогает expires_at.
    Иначе renewed без expires_at сломал бы проверку истечения в premium-gating."""

    async def test_renewed_without_expires_at_keeps_previous(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """started (expires 2099) → renewed БЕЗ expires_at → дата НЕ обнулена, осталась 2099."""
        did = str(uuid.uuid4())
        await create_user(client, did)

        # 1) started с expires_at 2099 → дата установлена.
        with override_settings():
            await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(
                    did, event_id="qb2-start", product_id=PRODUCT_WEEKLY,
                    expires_at="2099-06-01T00:00:00Z",
                ),
            )
        user = await _get_user(db_session, did)
        assert user.premium_expires_at is not None
        assert user.premium_expires_at.year == 2099

        # 2) renewed БЕЗ expires_at → дата должна сохраниться (не None).
        renewed = started_payload(
            did, event_id="qb2-renew", product_id=PRODUCT_WEEKLY, expires_at=None
        )
        renewed["event_type"] = "subscription_renewed"
        assert "expires_at" not in renewed["event_properties"]
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), json=renewed)
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"

        user = await _get_user(db_session, did)
        assert user.is_premium is True
        # Q-BILL-2 ядро: дата НЕ обнулена.
        assert user.premium_expires_at is not None
        assert user.premium_expires_at.year == 2099

    async def test_started_without_expires_at_keeps_previous(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """started (expires 2099) → второй started БЕЗ expires_at → дата сохранена."""
        did = str(uuid.uuid4())
        await create_user(client, did)

        with override_settings():
            await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(
                    did, event_id="qb2-s1", product_id=PRODUCT_WEEKLY,
                    expires_at="2099-06-01T00:00:00Z",
                ),
            )
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(
                    did, event_id="qb2-s2", product_id=PRODUCT_WEEKLY, expires_at=None
                ),
            )
        assert resp.status_code == 200
        user = await _get_user(db_session, did)
        assert user.premium_expires_at is not None
        assert user.premium_expires_at.year == 2099

    async def test_renewed_with_expires_at_updates_date(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """started (2099) → renewed С новым expires_at (2100) → дата обновлена на 2100."""
        did = str(uuid.uuid4())
        await create_user(client, did)

        with override_settings():
            await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(
                    did, event_id="qb2-u-start", product_id=PRODUCT_WEEKLY,
                    expires_at="2099-06-01T00:00:00Z",
                ),
            )
        renewed = started_payload(
            did, event_id="qb2-u-renew", product_id=PRODUCT_WEEKLY,
            expires_at="2100-01-15T00:00:00Z",
        )
        renewed["event_type"] = "subscription_renewed"
        with override_settings():
            resp = await client.post(WEBHOOK_URL, headers=auth_headers(), json=renewed)
        assert resp.status_code == 200
        user = await _get_user(db_session, did)
        assert user.premium_expires_at is not None
        assert user.premium_expires_at.year == 2100

    async def test_deactivation_does_not_touch_expires_at(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """started (2099) → cancelled → premium_expires_at НЕ обнулён (контракт §4),
        is_premium=False."""
        did = str(uuid.uuid4())
        await create_user(client, did)

        with override_settings():
            await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json=started_payload(
                    did, event_id="qb2-d-start", product_id=PRODUCT_WEEKLY,
                    expires_at="2099-06-01T00:00:00Z",
                ),
            )
        with override_settings():
            resp = await client.post(
                WEBHOOK_URL, headers=auth_headers(),
                json={
                    "event_id": "qb2-d-cancel",
                    "event_type": "subscription_cancelled",
                    "customer_user_id": did,
                },
            )
        assert resp.status_code == 200
        assert resp.json()["is_premium"] is False
        user = await _get_user(db_session, did)
        assert user.is_premium is False
        # expires_at не трогается при деактивации.
        assert user.premium_expires_at is not None
        assert user.premium_expires_at.year == 2099

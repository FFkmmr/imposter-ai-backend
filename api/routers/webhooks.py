"""Adapty webhook — bearer-авторизация + токеномика (вариант Б, ADR-002).

Финальный путь: POST /v1/billing/adapty/webhook (prefix "/v1" в api/main.py +
prefix "/billing" здесь + маршрут "/adapty/webhook").

Авторизация — статический bearer-секрет (ADAPTY_WEBHOOK_SECRET), constant-time
сравнение. Не HMAC. Любой авторизованный, но непригодный к обработке payload →
HTTP 200 {"status":"ignored",...}, чтобы Adapty не ретраил бесконечно. 5xx — только
при реальном внутреннем сбое (например, БД недоступна).
"""
import hmac
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from database import get_db
from models import ProcessedWebhookEvent, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Webhooks"])

# Канонические поддерживаемые события (ADR-002). lowercase.
PREMIUM_ACTIVATION_EVENTS = {"subscription_started", "subscription_renewed"}
PREMIUM_DEACTIVATION_EVENTS = {"subscription_cancelled", "subscription_expired"}
SUPPORTED_EVENTS = PREMIUM_ACTIVATION_EVENTS | PREMIUM_DEACTIVATION_EVENTS


def _verify_bearer(authorization: str | None, secret: str) -> None:
    """Проверка bearer-токена. Пустой секрет → 500. Неверный/отсутствующий → 401."""
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="ADAPTY_WEBHOOK_SECRET is not configured on the server",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    if not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _as_dict(value: Any) -> dict:
    """Возвращает value, если это dict, иначе пустой dict (дефенсивно)."""
    return value if isinstance(value, dict) else {}


def _first_nonempty(*values: Any) -> Any:
    """Первое непустое (truthy) значение или None."""
    for value in values:
        if value:
            return value
    return None


def _extract_customer_user_id(payload: dict) -> Any:
    profile = _as_dict(payload.get("profile"))
    return _first_nonempty(
        payload.get("customer_user_id"),
        profile.get("customer_user_id"),
        payload.get("user_id"),
    )


def _extract_vendor_product_id(payload: dict) -> Any:
    props = _as_dict(payload.get("event_properties"))
    return _first_nonempty(
        props.get("vendor_product_id"),
        props.get("product_id"),
        payload.get("vendor_product_id"),
        payload.get("product_id"),
    )


def _extract_expires_at(payload: dict) -> datetime | None:
    props = _as_dict(payload.get("event_properties"))
    profile = _as_dict(payload.get("profile"))
    raw = _first_nonempty(props.get("expires_at"), profile.get("expires_at"))
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Adapty webhook: unparsable expires_at=%r", raw)
        return None


def _resolve_token_grant(vendor_product_id: Any, settings: Settings) -> int:
    """Грант токенов по тиру vendor_product_id. Неизвестный → fallback."""
    if vendor_product_id and vendor_product_id == settings.subscription_product_weekly:
        return settings.subscription_tokens_weekly
    if vendor_product_id and vendor_product_id == settings.subscription_product_yearly:
        return settings.subscription_tokens_yearly
    return settings.subscription_tokens_grant


@router.post("/adapty/webhook")
async def adapty_webhook(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    _verify_bearer(authorization, settings.adapty_webhook_secret)

    raw_body = await request.body()
    if not raw_body or not raw_body.strip():
        return {"status": "ignored", "reason": "empty_body"}

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "ignored", "reason": "invalid_json"}

    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "not_an_object"}

    event_id = _first_nonempty(payload.get("event_id"), payload.get("id"))
    if not event_id:
        return {"status": "ignored", "reason": "missing_event_id"}
    event_id = str(event_id)

    event_type = str(payload.get("event_type") or "").lower()
    if event_type not in SUPPORTED_EVENTS:
        logger.info("Adapty webhook: unsupported event_type=%s event_id=%s", event_type, event_id)
        return {"status": "ignored", "event_type": event_type}

    customer_user_id = _extract_customer_user_id(payload)
    if not customer_user_id:
        return {"status": "ignored", "reason": "missing_customer_user_id"}
    customer_user_id = str(customer_user_id)

    # customer_user_id == User.device_id (UUID). Не парсится → ignored (не 5xx).
    try:
        device_uuid = uuid.UUID(customer_user_id)
    except (ValueError, AttributeError):
        logger.info("Adapty webhook: customer_user_id not a UUID=%s", customer_user_id)
        return {"status": "ignored", "reason": "invalid_customer_user_id"}

    # Идемпотентность: уже обработанное событие → duplicate без начисления.
    existing = await db.get(ProcessedWebhookEvent, event_id)
    if existing is not None:
        logger.info("Adapty webhook: duplicate event_id=%s", event_id)
        return {"status": "duplicate"}

    result = await db.execute(select(User).where(User.device_id == device_uuid))
    user = result.scalar_one_or_none()
    if user is None:
        logger.info("Adapty webhook: user not found for device_id=%s", customer_user_id)
        return {"status": "ignored", "reason": "user_not_found"}

    expires_at = _extract_expires_at(payload)
    tokens_granted = 0
    is_premium: bool

    if event_type in PREMIUM_ACTIVATION_EVENTS:
        is_premium = True
        user.is_premium = True
        # Q-BILL-2: обновляем premium_expires_at ТОЛЬКО если expires_at реально
        # распарсен. Если отсутствует/None — сохраняем прежнее значение (не обнуляем),
        # иначе renewed-событие без expires_at сломало бы проверку истечения в gating.
        if expires_at is not None:
            user.premium_expires_at = expires_at
        vendor_product_id = _extract_vendor_product_id(payload)
        tokens_granted = _resolve_token_grant(vendor_product_id, settings)
        user.tokens += tokens_granted
    else:  # PREMIUM_DEACTIVATION_EVENTS
        is_premium = False
        user.is_premium = False
        # Токены и premium_expires_at не трогаем (по контракту §4).

    # Начисление + запись в ledger — одна атомарная транзакция.
    db.add(
        ProcessedWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            customer_user_id=customer_user_id,
            tokens_granted=tokens_granted,
        )
    )

    try:
        await db.commit()
    except IntegrityError:
        # Гонка: параллельный дубль успел вставить ledger-запись первым.
        await db.rollback()
        logger.info("Adapty webhook: race duplicate event_id=%s", event_id)
        return {"status": "duplicate"}

    logger.info(
        "Adapty webhook applied | event=%s | event_id=%s | tokens=%s | is_premium=%s",
        event_type,
        event_id,
        tokens_granted,
        is_premium,
    )
    return {
        "status": "applied",
        "event_type": event_type,
        "tokens_granted": tokens_granted,
        "is_premium": is_premium,
    }

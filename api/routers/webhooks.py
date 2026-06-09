import hashlib
import hmac
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db
from models import User
from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

ACTIVATION_EVENTS = {
    "subscription_initial_purchase",
    "subscription_renewed",
    "subscription_activated",
}

EXPIRY_EVENTS = {
    "subscription_expired",
    "subscription_cancelled",
    "subscription_refunded",
}


class AdaptyWebhookPayload(BaseModel):
    event_type: str
    profile_id: str
    customer_user_id: str | None = None
    paid_access_level: dict | None = None

    class Config:
        extra = "allow"


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@router.post("/adapty")
async def adapty_webhook(
    request: Request,
    adapty_signature: str | None = Header(default=None, alias="Adapty-Signature"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()

    raw_body = await request.body()

    if settings.adapty_webhook_secret:
        if not adapty_signature:
            raise HTTPException(status_code=401, detail="Missing Adapty-Signature header")
        if not _verify_signature(raw_body, adapty_signature, settings.adapty_webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid Adapty-Signature")

    try:
        import json
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = body.get("event_type")
    customer_user_id = body.get("customer_user_id")
    paid_access_level = body.get("paid_access_level")

    logger.info(
        "Adapty webhook received | event=%s | customer_user_id=%s",
        event_type,
        customer_user_id,
    )

    if not customer_user_id:
        return {"status": "ok"}

    try:
        device_uuid = uuid.UUID(customer_user_id)
    except (ValueError, AttributeError):
        logger.warning("Adapty webhook: invalid customer_user_id=%s", customer_user_id)
        return {"status": "ok"}

    if event_type in ACTIVATION_EVENTS:
        expires_at: datetime | None = None
        if paid_access_level and paid_access_level.get("expires_at"):
            try:
                expires_at = datetime.fromisoformat(
                    paid_access_level["expires_at"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                expires_at = None

        result = await db.execute(select(User).where(User.device_id == device_uuid))
        user = result.scalar_one_or_none()
        if user:
            user.is_premium = True
            user.premium_expires_at = expires_at
            await db.commit()

    elif event_type in EXPIRY_EVENTS:
        result = await db.execute(select(User).where(User.device_id == device_uuid))
        user = result.scalar_one_or_none()
        if user:
            user.is_premium = False
            user.premium_expires_at = None
            await db.commit()

    return {"status": "ok"}

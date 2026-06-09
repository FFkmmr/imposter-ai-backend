import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from database import get_db
from models import User
from auth import require_admin_api_key

router = APIRouter(prefix="/admin", tags=["Admin"])


class AddTokensRequest(BaseModel):
    device_id: uuid.UUID
    tokens_amount: int = Field(ge=1)


@router.post("/users/tokens", dependencies=[Depends(require_admin_api_key)])
async def add_tokens(
    body: AddTokensRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.device_id == body.device_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.tokens += body.tokens_amount
    await db.commit()
    await db.refresh(user)

    return {
        "id": user.id,
        "device_id": user.device_id,
        "tokens": user.tokens,
        "is_premium": user.is_premium,
        "premium_expires_at": user.premium_expires_at.isoformat() if user.premium_expires_at else None,
        "created_at": user.created_at.isoformat(),
    }

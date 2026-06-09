import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database import get_db
from models import User
from jwt_utils import create_access_token
from auth import require_api_key

router = APIRouter(prefix="/users", tags=["User"])


class CreateUserRequest(BaseModel):
    device_id: uuid.UUID


class UserProfileOut(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    tokens: int
    is_premium: bool
    premium_expires_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class UserWithTokenOut(UserProfileOut):
    access_token: str
    token_type: str = "bearer"


async def _get_device_id(x_device_id: str = Header(alias="X-Device-Id")) -> uuid.UUID:
    try:
        return uuid.UUID(x_device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Device-Id must be a valid UUID")


@router.post("", response_model=UserWithTokenOut)
async def create_or_get_user(
    body: CreateUserRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.device_id == body.device_id))
    user = result.scalar_one_or_none()

    if user:
        response.status_code = status.HTTP_200_OK
        token = create_access_token(user.id, user.device_id)
        profile = UserProfileOut.model_validate(user, from_attributes=True)
        return UserWithTokenOut(**profile.model_dump(), access_token=token)

    user = User(device_id=body.device_id)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    response.status_code = status.HTTP_201_CREATED
    token = create_access_token(user.id, user.device_id)
    profile = UserProfileOut.model_validate(user, from_attributes=True)
    return UserWithTokenOut(**profile.model_dump(), access_token=token)


@router.get("/me", response_model=UserProfileOut, dependencies=[Depends(require_api_key)])
async def get_profile(
    device_id: uuid.UUID = Depends(_get_device_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.device_id == device_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

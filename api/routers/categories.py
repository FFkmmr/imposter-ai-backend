import uuid
import random
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database import get_db
from models import Category, WordPack, User
from auth import require_api_key
from rate_limit import check_rate_limit, get_words_limit_key

router = APIRouter(prefix="/categories", tags=["Categories"])


async def _get_device_id(x_device_id: str = Header(alias="X-Device-Id")) -> uuid.UUID:
    try:
        return uuid.UUID(x_device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Device-Id must be a valid UUID")


class CategoryOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str
    is_premium: bool
    is_active: bool
    cover_image_url: str | None
    sort_order: int

    class Config:
        from_attributes = True


class CategoryPremiumOut(CategoryOut):
    preview_words: list[str] = []


class WordEntryOut(BaseModel):
    id: uuid.UUID
    civilian_word: str
    impostor_word: str | None
    difficulty: str
    tags: list[str] = []

    class Config:
        from_attributes = True


def _localize_category(cat: Category, locale: str) -> CategoryOut:
    name = cat.name.get(locale) or cat.name.get("en") or next(iter(cat.name.values()), "")
    desc_raw = cat.description or {}
    desc = desc_raw.get(locale) or desc_raw.get("en") or ""
    return CategoryOut(
        id=cat.id,
        slug=cat.slug,
        name=name,
        description=desc,
        is_premium=cat.is_premium,
        is_active=cat.is_active,
        cover_image_url=cat.cover_image_url,
        sort_order=cat.sort_order,
    )


@router.get("", dependencies=[Depends(require_api_key)])
async def list_categories(
    response: Response,
    locale: str = Query(default="en"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Category).where(Category.is_active == True).order_by(Category.sort_order)
    )
    cats = result.scalars().all()
    response.headers["Cache-Control"] = "max-age=1800"
    return {"categories": [_localize_category(c, locale) for c in cats]}


@router.get("/premium", dependencies=[Depends(require_api_key)])
async def list_premium_categories(
    response: Response,
    locale: str = Query(default="en"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Category).where(Category.is_active == True, Category.is_premium == True).order_by(Category.sort_order)
    )
    cats = result.scalars().all()

    out = []
    for cat in cats:
        base = _localize_category(cat, locale)
        packs_result = await db.execute(
            select(WordPack).where(WordPack.category_id == cat.id, WordPack.locale == locale).limit(3)
        )
        packs = packs_result.scalars().all()
        if not packs:
            packs_result = await db.execute(
                select(WordPack).where(WordPack.category_id == cat.id, WordPack.locale == "en").limit(3)
            )
            packs = packs_result.scalars().all()
        preview = [p.civilian_word for p in packs]
        out.append(CategoryPremiumOut(**base.model_dump(), preview_words=preview))

    response.headers["Cache-Control"] = "max-age=1800"
    return {"categories": out}


@router.get("/{category_id}/words", dependencies=[Depends(require_api_key)])
async def get_words(
    category_id: uuid.UUID,
    response: Response,
    device_id: uuid.UUID = Depends(_get_device_id),
    locale: str = Query(default="en"),
    mode: str = Query(default="standard"),
    count: int = Query(default=1, ge=1, le=5),
    db: AsyncSession = Depends(get_db),
):
    cat_result = await db.execute(select(Category).where(Category.id == category_id))
    cat = cat_result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    if cat.is_premium:
        user_result = await db.execute(select(User).where(User.device_id == device_id))
        user = user_result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        premium_active = (
            user is not None
            and user.is_premium
            and (user.premium_expires_at is None or user.premium_expires_at > now)
        )
        if not premium_active:
            raise HTTPException(status_code=403, detail="Premium subscription required")

    key, limit = await get_words_limit_key(device_id)
    allowed, rl_limit, remaining, reset_ts = await check_rate_limit(key, limit)

    response.headers["X-RateLimit-Limit"] = str(rl_limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset_ts)

    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    packs_result = await db.execute(
        select(WordPack).where(WordPack.category_id == category_id, WordPack.locale == locale)
    )
    packs = packs_result.scalars().all()

    if not packs:
        packs_result = await db.execute(
            select(WordPack).where(WordPack.category_id == category_id, WordPack.locale == "en")
        )
        packs = packs_result.scalars().all()
        locale = "en"

    selected = random.sample(packs, min(count, len(packs))) if packs else []
    words = [WordEntryOut.model_validate(p) for p in selected]

    return {"category_id": category_id, "locale": locale, "mode": mode, "words": words}

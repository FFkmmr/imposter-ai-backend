from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db
from models import FeatureConfig
from auth import require_api_key

router = APIRouter(tags=["Config"])

DEFAULT_CONFIG = {
    "features": {
        "ai_theme": {"enabled": True, "free_daily_limit": 5, "premium_daily_limit": 50},
        "premium_categories": {"enabled": True},
        "party_mode_extra_roles": {"enabled": True, "is_premium": False},
        "import_custom_words": {"enabled": True},
    },
    "content": {
        "default_free_categories": ["animals", "food", "movies"],
        "featured_category_slug": "animals",
    },
    "app": {
        "min_supported_version": "1.0.0",
        "force_update_version": None,
        "maintenance_mode": False,
    },
}

DEFAULT_LOCALIZATIONS = {
    "en": {
        "paywall.title": "Unlock Everything",
        "paywall.subtitle": "AI themes, premium categories and more",
        "onboarding.step1.title": "Assign Roles",
    },
    "ru": {
        "paywall.title": "Открой всё",
        "paywall.subtitle": "AI-темы, премиум-категории и многое другое",
        "onboarding.step1.title": "Раздаём роли",
    },
    "es": {
        "paywall.title": "Desbloquéalo todo",
        "paywall.subtitle": "Temas con IA, categorías premium y mucho más",
        "onboarding.step1.title": "Repartimos los roles",
    },
    "pt": {
        "paywall.title": "Desbloqueie tudo",
        "paywall.subtitle": "Temas com IA, categorias premium e muito mais",
        "onboarding.step1.title": "Distribuímos os papéis",
    },
    "fr": {
        "paywall.title": "Tout débloquer",
        "paywall.subtitle": "Thèmes IA, catégories premium et bien plus",
        "onboarding.step1.title": "On distribue les rôles",
    },
    "de": {
        "paywall.title": "Alles freischalten",
        "paywall.subtitle": "KI-Themen, Premium-Kategorien und vieles mehr",
        "onboarding.step1.title": "Wir verteilen die Rollen",
    },
}


@router.get("/config", dependencies=[Depends(require_api_key)])
async def get_config(response: Response, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    result = await db.execute(select(FeatureConfig))
    rows = result.scalars().all()

    config = dict(DEFAULT_CONFIG)
    for row in rows:
        config[row.key] = row.value

    config["updated_at"] = datetime.now(timezone.utc).isoformat()
    response.headers["Cache-Control"] = "max-age=900"
    return config


@router.get("/localizations", dependencies=[Depends(require_api_key)])
async def get_localizations(
    response: Response,
    locale: str = Query(...),
    keys: str | None = Query(default=None),
):
    strings = DEFAULT_LOCALIZATIONS.get(locale) or DEFAULT_LOCALIZATIONS.get("en") or {}

    if keys:
        key_list = [k.strip() for k in keys.split(",")]
        strings = {k: v for k, v in strings.items() if k in key_list}

    response.headers["Cache-Control"] = "max-age=3600"
    return {"locale": locale, "strings": strings}

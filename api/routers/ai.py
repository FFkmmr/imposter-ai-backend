import uuid
import re
import random
import asyncio
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from database import get_db
from models import User, WordPack, Category, AITopicRequestLog
from auth import require_api_key, require_admin_api_key
from rate_limit import check_rate_limit, get_ai_theme_limit_key
from redis_client import get_redis
from config import get_settings

router = APIRouter(prefix="/ai", tags=["AI"])

BLACKLIST_PATTERNS = [
    r"\b(sex|porn|kill|murder|rape|nazi|terror|bomb|drug|cocaine|fentanyl)\b",
]
_blacklist_re = [re.compile(p, re.IGNORECASE) for p in BLACKLIST_PATTERNS]


def _sanitize(topic: str) -> str:
    return re.sub(r"[^\w\s-]", "", topic.strip())[:80]


def _is_blocked(topic: str) -> bool:
    return any(r.search(topic) for r in _blacklist_re)


async def _get_device_id(x_device_id: str = Header(alias="X-Device-Id")) -> uuid.UUID:
    try:
        return uuid.UUID(x_device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Device-Id must be a valid UUID")


async def _get_fallback_word(db: AsyncSession, locale: str, mode: str) -> dict:
    result = await db.execute(select(WordPack).where(WordPack.locale == locale).limit(50))
    packs = result.scalars().all()
    if not packs:
        result = await db.execute(select(WordPack).where(WordPack.locale == "en").limit(50))
        packs = result.scalars().all()
    if not packs:
        return {"civilian_word": "Unknown", "impostor_word": None}
    pick = random.choice(packs)
    return {
        "civilian_word": pick.civilian_word,
        "impostor_word": pick.impostor_word if mode == "party" else None,
    }


async def _generate_with_llm(topic: str, locale: str, mode: str, count: int = 1) -> list[dict]:
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    party_instruction = (
        "For each entry, provide: civilian_word (what regular players see) and "
        "impostor_word (a similar but different word the undercover player gets). "
        "Both words should be in the same language."
        if mode == "party"
        else "For each entry, provide only: civilian_word. Set impostor_word to null."
    )

    prompt = (
        f"Generate {count} party-game word{'s' if count > 1 else ''} for the topic: '{topic}'.\n"
        f"Language: {locale}.\n"
        f"{party_instruction}\n"
        f"Rules: words must be safe, family-friendly, widely known. No NSFW, hate, or illegal content.\n"
        f"Respond ONLY with a JSON array of objects with keys: civilian_word, impostor_word, difficulty (easy/medium/hard).\n"
        f"Example: [{{'civilian_word': 'Mars', 'impostor_word': 'Moon', 'difficulty': 'medium'}}]"
    )

    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=400,
        ),
        timeout=5.0,
    )

    import json
    text = response.choices[0].message.content.strip()
    text = re.sub(r"^```json\s*|^```\s*|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


async def _check_moderation(text: str) -> bool:
    """Returns True if content is safe."""
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    result = await client.moderations.create(input=text)
    return not result.results[0].flagged


class GenerateThemeRequest(BaseModel):
    topic: str = Field(max_length=80)
    locale: str = "en"
    mode: str = "standard"


@router.post("/generate-theme", dependencies=[Depends(require_api_key)])
async def generate_theme(
    body: GenerateThemeRequest,
    response: Response,
    device_id: uuid.UUID = Depends(_get_device_id),
    db: AsyncSession = Depends(get_db),
):
    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="topic cannot be empty")

    sanitized = _sanitize(body.topic)
    if not sanitized:
        raise HTTPException(status_code=400, detail="topic cannot be empty after sanitization")

    user_result = await db.execute(select(User).where(User.device_id == device_id))
    user = user_result.scalar_one_or_none()
    is_premium = user.is_premium if user else False

    key, limit = await get_ai_theme_limit_key(device_id, is_premium)
    allowed, rl_limit, remaining, reset_ts = await check_rate_limit(key, limit)

    response.headers["X-RateLimit-Limit"] = str(rl_limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset_ts)

    if not allowed:
        fallback = await _get_fallback_word(db, body.locale, body.mode)
        await _log(db, device_id, body.locale, sanitized, body.mode, False, True, "rate_limit_exceeded")
        return {
            "locale": body.locale,
            "topic": sanitized,
            **fallback,
            "difficulty": "easy",
            "is_safe": True,
            "fallback_used": True,
            "fallback_reason": "rate_limit_exceeded",
        }

    if _is_blocked(sanitized):
        fallback = await _get_fallback_word(db, body.locale, body.mode)
        await _log(db, device_id, body.locale, sanitized, body.mode, True, True, "moderation_rejected")
        return {
            "locale": body.locale,
            "topic": sanitized,
            **fallback,
            "difficulty": "easy",
            "is_safe": True,
            "fallback_used": True,
            "fallback_reason": "moderation_rejected",
        }

    r = get_redis()
    pool_key = f"topic_pool:{sanitized}:{body.locale}"
    history_key = f"device_history:{device_id}:{sanitized}:{body.locale}"

    pool_words_raw = await r.smembers(pool_key)
    used_words = await r.smembers(history_key)
    available = [w for w in pool_words_raw if w not in used_words]

    if available:
        import json
        pick_raw = random.choice(available)
        pick = json.loads(pick_raw)
        await r.sadd(history_key, pick_raw)
        await r.expire(history_key, 30 * 86400)
        await _log(db, device_id, body.locale, sanitized, body.mode, False, False, None)
        return {
            "locale": body.locale,
            "topic": sanitized,
            "civilian_word": pick["civilian_word"],
            "impostor_word": pick.get("impostor_word") if body.mode == "party" else None,
            "difficulty": pick.get("difficulty", "medium"),
            "is_safe": True,
            "fallback_used": False,
            "fallback_reason": None,
        }

    try:
        words = await _generate_with_llm(sanitized, body.locale, body.mode, count=5)
    except (asyncio.TimeoutError, Exception):
        fallback = await _get_fallback_word(db, body.locale, body.mode)
        await _log(db, device_id, body.locale, sanitized, body.mode, False, True, "ai_unavailable")
        return {
            "locale": body.locale,
            "topic": sanitized,
            **fallback,
            "difficulty": "easy",
            "is_safe": True,
            "fallback_used": True,
            "fallback_reason": "ai_unavailable",
        }

    import json
    safe_words = []
    for w in words:
        try:
            is_safe = await _check_moderation(w.get("civilian_word", ""))
        except Exception:
            is_safe = True
        if is_safe:
            safe_words.append(w)

    if not safe_words:
        fallback = await _get_fallback_word(db, body.locale, body.mode)
        await _log(db, device_id, body.locale, sanitized, body.mode, True, True, "moderation_rejected")
        return {
            "locale": body.locale,
            "topic": sanitized,
            **fallback,
            "difficulty": "easy",
            "is_safe": True,
            "fallback_used": True,
            "fallback_reason": "moderation_rejected",
        }

    for w in safe_words:
        await r.sadd(pool_key, json.dumps(w))

    pick = random.choice(safe_words)
    pick_raw = json.dumps(pick)
    await r.sadd(history_key, pick_raw)
    await r.expire(history_key, 30 * 86400)

    await _log(db, device_id, body.locale, sanitized, body.mode, False, False, None)

    return {
        "locale": body.locale,
        "topic": sanitized,
        "civilian_word": pick["civilian_word"],
        "impostor_word": pick.get("impostor_word") if body.mode == "party" else None,
        "difficulty": pick.get("difficulty", "medium"),
        "is_safe": True,
        "fallback_used": False,
        "fallback_reason": None,
    }


async def _log(
    db: AsyncSession,
    device_id: uuid.UUID,
    locale: str,
    sanitized_prompt: str,
    mode: str,
    was_rejected: bool,
    fallback_used: bool,
    fallback_reason: str | None,
):
    log = AITopicRequestLog(
        device_id=device_id,
        locale=locale,
        sanitized_prompt=sanitized_prompt,
        mode=mode,
        was_rejected=was_rejected,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )
    db.add(log)
    await db.commit()


class GenerateWordsRequest(BaseModel):
    topic: str
    locale: str = "en"
    count: int = Field(default=20, ge=10, le=30)
    mode: str = "standard"
    difficulty: str | None = None


@router.post("/generate-words", dependencies=[Depends(require_admin_api_key)])
async def generate_words(body: GenerateWordsRequest, db: AsyncSession = Depends(get_db)):
    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="topic cannot be empty")

    sanitized = _sanitize(body.topic)

    try:
        words = await _generate_with_llm(sanitized, body.locale, body.mode, count=body.count)
    except (asyncio.TimeoutError, Exception):
        await _log(db, uuid.UUID(int=0), body.locale, sanitized, body.mode, False, True, "ai_unavailable")
        raise HTTPException(status_code=503, detail="LLM unavailable")

    import json
    safe_words = []
    unsafe_count = 0
    for w in words:
        try:
            is_safe = await _check_moderation(w.get("civilian_word", ""))
        except Exception:
            is_safe = True
        if is_safe:
            safe_words.append({**w, "is_safe": True})
        else:
            unsafe_count += 1

    if len(safe_words) < 10:
        try:
            extra = await _generate_with_llm(sanitized, body.locale, body.mode, count=body.count - len(safe_words))
            for w in extra:
                try:
                    is_safe = await _check_moderation(w.get("civilian_word", ""))
                except Exception:
                    is_safe = True
                if is_safe:
                    safe_words.append({**w, "is_safe": True})
                else:
                    unsafe_count += 1
        except Exception:
            pass

    was_rejected = unsafe_count > 0 and len(safe_words) == 0
    await _log(db, uuid.UUID(int=0), body.locale, sanitized, body.mode, was_rejected, False, None)

    return {
        "locale": body.locale,
        "topic": body.topic,
        "words": safe_words,
        "total": len(safe_words),
        "unsafe_filtered": unsafe_count,
    }

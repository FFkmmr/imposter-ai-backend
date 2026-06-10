import uuid
import re
import json
import random
import asyncio
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from database import get_db
from models import User, WordPack, Category, AITopicRequestLog
from auth import require_api_key, require_admin_api_key
from rate_limit import check_rate_limit, get_ai_theme_limit_key
from redis_client import get_redis
from config import get_settings

logger = logging.getLogger(__name__)

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

    # max_tokens масштабируем от count: каждый объект {civilian_word, impostor_word,
    # difficulty} ~40-60 токенов + накладные на массив/обёртку. Пол 400 (для count=1..5),
    # потолок 4000 (count=30 → ~1800, с запасом). Не ломает generate-theme (count=5 → 400).
    max_tokens = min(4000, max(400, count * 60))

    # Таймаут тоже зависит от count: base 5s + 0.5s/объект, потолок 25s.
    # generate-theme (count=5) → 5s (быстро). count=30 → 20s.
    timeout = min(25.0, 5.0 + count * 0.5)

    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=max_tokens,
        ),
        timeout=timeout,
    )

    text = response.choices[0].message.content.strip()
    text = re.sub(r"^```json\s*|^```\s*|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(
            "AI LLM: failed to parse JSON response | count=%s | max_tokens=%s | "
            "raw_response (truncated 500)=%r",
            count, max_tokens, text[:500],
            exc_info=True,
        )
        raise


async def _check_moderation(text: str) -> bool:
    """Returns True if content is safe."""
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    result = await client.moderations.create(input=text)
    return not result.results[0].flagged


def _is_premium_active(user: User | None) -> bool:
    """Premium = is_premium AND (premium_expires_at is null OR > now).
    Та же логика, что в GET /categories/{id}/words."""
    now = datetime.now(timezone.utc)
    return (
        user is not None
        and user.is_premium
        and (user.premium_expires_at is None or user.premium_expires_at > now)
    )


async def _fallback_response(
    db: AsyncSession,
    device_id: uuid.UUID,
    locale: str,
    sanitized: str,
    mode: str,
    reason: str,
    tokens_remaining: int | None,
    was_rejected: bool = False,
) -> dict:
    """Собирает curated-fallback ответ (HTTP 200) и пишет лог. Токены не трогает."""
    fallback = await _get_fallback_word(db, locale, mode)
    await _log(db, device_id, locale, sanitized, mode, was_rejected, True, reason)
    return {
        "locale": locale,
        "topic": sanitized,
        **fallback,
        "difficulty": "easy",
        "is_safe": True,
        "fallback_used": True,
        "fallback_reason": reason,
        "tokens_remaining": tokens_remaining,
    }


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
    # --- Шаг 1: Валидация topic ---
    if not body.topic.strip():
        raise HTTPException(status_code=400, detail="topic cannot be empty")

    sanitized = _sanitize(body.topic)
    if not sanitized:
        raise HTTPException(status_code=400, detail="topic cannot be empty after sanitization")

    # --- Шаг 2: Авторизация — X-Api-Key (dependency) + X-Device-Id UUID (_get_device_id) ---

    settings = get_settings()
    cost = settings.ai_theme_token_cost

    user_result = await db.execute(select(User).where(User.device_id == device_id))
    user = user_result.scalar_one_or_none()

    # --- Шаг 3: Premium-gating ---
    # Не premium (включая отсутствующего в БД) → premium_required, tokens_remaining=null,
    # токены не трогаем. AI не вызывается.
    if not _is_premium_active(user):
        return await _fallback_response(
            db, device_id, body.locale, sanitized, body.mode,
            reason="premium_required", tokens_remaining=None,
        )

    # С этого момента user гарантированно не None и premium-активен.
    # tokens_remaining для всех premium-путей — int (текущий баланс), кроме успеха (после списания).
    balance = user.tokens

    # --- Шаг 4: Rate-limit (анти-абуз потолок premium 50/24h) ---
    key, limit = await get_ai_theme_limit_key(device_id, True)
    allowed, rl_limit, remaining, reset_ts = await check_rate_limit(key, limit)

    response.headers["X-RateLimit-Limit"] = str(rl_limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(reset_ts)

    if not allowed:
        return await _fallback_response(
            db, device_id, body.locale, sanitized, body.mode,
            reason="rate_limit_exceeded", tokens_remaining=balance,
        )

    # --- Шаг 5: Blacklist / модерация темы ---
    if _is_blocked(sanitized):
        return await _fallback_response(
            db, device_id, body.locale, sanitized, body.mode,
            reason="moderation_rejected", tokens_remaining=balance, was_rejected=True,
        )

    # --- Шаг 6: Проверка баланса (early check; финальная гарантия — conditional decrement) ---
    if balance < cost:
        return await _fallback_response(
            db, device_id, body.locale, sanitized, body.mode,
            reason="insufficient_tokens", tokens_remaining=balance,
        )

    # --- Шаг 7: Подбор AI-слова-кандидата (pool-cache hit ИЛИ свежая LLM-генерация) ---
    # ВАЖНО: на этом шаге НЕ делаем sadd в device_history. Фиксация — только после списания (шаг 9).
    r = get_redis()
    pool_key = f"topic_pool:{sanitized}:{body.locale}"
    history_key = f"device_history:{device_id}:{sanitized}:{body.locale}"

    pool_words_raw = await r.smembers(pool_key)
    used_words = await r.smembers(history_key)
    available = [w for w in pool_words_raw if w not in used_words]

    if available:
        # pool-cache hit
        pick_raw = random.choice(available)
        pick = json.loads(pick_raw)
    else:
        # свежая LLM-генерация
        try:
            words = await _generate_with_llm(sanitized, body.locale, body.mode, count=5)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning(
                "AI theme: LLM generation failed, using fallback | device_id=%s | topic=%s | "
                "error=%s: %s",
                device_id, sanitized, type(exc).__name__, exc,
                exc_info=True,
            )
            return await _fallback_response(
                db, device_id, body.locale, sanitized, body.mode,
                reason="ai_unavailable", tokens_remaining=balance,
            )

        safe_words = []
        for w in words:
            try:
                is_safe = await _check_moderation(w.get("civilian_word", ""))
            except Exception:
                is_safe = True
            if is_safe:
                safe_words.append(w)

        if not safe_words:
            return await _fallback_response(
                db, device_id, body.locale, sanitized, body.mode,
                reason="moderation_rejected", tokens_remaining=balance, was_rejected=True,
            )

        for w in safe_words:
            await r.sadd(pool_key, json.dumps(w))

        pick = random.choice(safe_words)
        pick_raw = json.dumps(pick)

    # --- Шаг 8: Атомарное conditional decrement (ПЕРЕД фиксацией выдачи) ---
    # UPDATE users SET tokens = tokens - :cost WHERE id = :id AND tokens >= :cost.
    # rowcount=0 → гонка/баланс ушёл параллельным запросом → insufficient_tokens, история НЕ меняется.
    decrement = (
        update(User)
        .where(User.id == user.id, User.tokens >= cost)
        .values(tokens=User.tokens - cost)
    )
    result = await db.execute(decrement)
    if result.rowcount == 0:
        await db.rollback()
        # Текущий баланс перечитываем (он мог уйти параллельным запросом).
        refreshed = await db.execute(select(User.tokens).where(User.id == user.id))
        current_balance = refreshed.scalar_one_or_none() or 0
        return await _fallback_response(
            db, device_id, body.locale, sanitized, body.mode,
            reason="insufficient_tokens", tokens_remaining=current_balance,
        )

    new_balance = balance - cost
    await db.commit()

    # --- Шаг 9: Фиксация выдачи ТОЛЬКО при rowcount=1 (best-effort) ---
    # Токен УЖЕ списан и AI-слово сгенерировано → клиент ОБЯЗАН получить слово.
    # Запись в device_history (sadd/expire) и аудит-лог — best-effort: их сбой
    # (Redis/БД-лог недоступны) НЕ должен ронять ответ в HTTP 500 и терять оплаченное слово.
    try:
        await r.sadd(history_key, pick_raw)
        await r.expire(history_key, 30 * 86400)
    except Exception:
        logger.warning(
            "AI theme: device_history update failed (best-effort) | device_id=%s | topic=%s",
            device_id, sanitized, exc_info=True,
        )

    # --- Шаг 10: Лог (best-effort) + ответ с tokens_remaining ---
    try:
        await _log(db, device_id, body.locale, sanitized, body.mode, False, False, None)
    except Exception:
        logger.warning(
            "AI theme: audit log failed (best-effort) | device_id=%s | topic=%s",
            device_id, sanitized, exc_info=True,
        )

    logger.info(
        "AI theme issued | device_id=%s | topic=%s | cost=%s | tokens_remaining=%s",
        device_id, sanitized, cost, new_balance,
    )

    return {
        "locale": body.locale,
        "topic": sanitized,
        "civilian_word": pick["civilian_word"],
        "impostor_word": pick.get("impostor_word") if body.mode == "party" else None,
        "difficulty": pick.get("difficulty", "medium"),
        "is_safe": True,
        "fallback_used": False,
        "fallback_reason": None,
        "tokens_remaining": new_balance,
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
    except (asyncio.TimeoutError, Exception) as exc:
        logger.error(
            "AI words: LLM generation failed | topic=%s | locale=%s | count=%s | error=%s: %s",
            sanitized, body.locale, body.count, type(exc).__name__, exc,
            exc_info=True,
        )
        await _log(db, uuid.UUID(int=0), body.locale, sanitized, body.mode, False, True, "ai_unavailable")
        raise HTTPException(status_code=503, detail="LLM unavailable")

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

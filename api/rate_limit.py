import time
import uuid
from redis_client import get_redis

LIMITS = {
    "ai_theme_free": 5,
    "ai_theme_premium": 50,
    "words_free": 100,
}
WINDOW = 86400  # 24h rolling


async def check_rate_limit(key: str, limit: int) -> tuple[bool, int, int, int]:
    """
    Returns (is_allowed, limit, remaining, reset_ts).
    Uses a simple sliding window with a sorted set in Redis.
    """
    r = get_redis()
    now = time.time()
    window_start = now - WINDOW

    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.zrange(key, 0, 0, withscores=True)
    pipe.expire(key, WINDOW)
    results = await pipe.execute()

    count = results[2]
    oldest = results[3]

    if count > limit:
        reset_ts = int(oldest[0][1] + WINDOW) if oldest else int(now + WINDOW)
        return False, limit, 0, reset_ts

    remaining = max(0, limit - count)
    reset_ts = int(oldest[0][1] + WINDOW) if oldest else int(now + WINDOW)
    return True, limit, remaining, reset_ts


async def get_ai_theme_limit_key(device_id: uuid.UUID, is_premium: bool) -> tuple[str, int]:
    key = f"rate:ai_theme:{device_id}"
    limit = LIMITS["ai_theme_premium"] if is_premium else LIMITS["ai_theme_free"]
    return key, limit


async def get_words_limit_key(device_id: uuid.UUID) -> tuple[str, int]:
    key = f"rate:words:{device_id}"
    return key, LIMITS["words_free"]

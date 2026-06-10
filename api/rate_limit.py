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
    if limit == 0:  # unlimited
        return True, 0, 0, 0

    r = get_redis()
    now = time.time()
    window_start = now - WINDOW

    # First check current count without adding
    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zcard(key)
    pipe.zrange(key, 0, 0, withscores=True)
    results = await pipe.execute()

    count = results[1]
    oldest = results[2]

    if count >= limit:
        reset_ts = int(oldest[0][1] + WINDOW) if oldest else int(now + WINDOW)
        return False, limit, 0, reset_ts

    # Only consume a slot if allowed
    pipe2 = r.pipeline()
    pipe2.zadd(key, {str(now): now})
    pipe2.expire(key, WINDOW)
    await pipe2.execute()

    remaining = max(0, limit - count - 1)
    reset_ts = int(oldest[0][1] + WINDOW) if oldest else int(now + WINDOW)
    return True, limit, remaining, reset_ts


async def get_ai_theme_limit_key(device_id: uuid.UUID, is_premium: bool) -> tuple[str, int]:
    key = f"rate:ai_theme:{device_id}"
    limit = LIMITS["ai_theme_premium"] if is_premium else LIMITS["ai_theme_free"]
    return key, limit


async def get_words_limit_key(device_id: uuid.UUID, is_premium: bool = False) -> tuple[str, int]:
    key = f"rate:words:{device_id}"
    limit = 0 if is_premium else LIMITS["words_free"]  # 0 = unlimited
    return key, limit

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from jose import jwt
from config import get_settings


def _load_key(path: str) -> str:
    return Path(path).read_text()


def create_access_token(user_id: uuid.UUID, device_id: uuid.UUID) -> str:
    settings = get_settings()
    private_key = _load_key(settings.jwt_private_key_path)
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_expire_days)
    payload = {
        "sub": str(user_id),
        "device_id": str(device_id),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)

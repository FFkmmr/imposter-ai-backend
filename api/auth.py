from fastapi import Header, HTTPException, status
from config import get_settings


def require_api_key(x_api_key: str = Header(alias="X-Api-Key")) -> str:
    settings = get_settings()
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-Api-Key")
    return x_api_key


def require_admin_api_key(x_admin_api_key: str = Header(alias="X-Admin-Api-Key")) -> str:
    settings = get_settings()
    if x_admin_api_key != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-Admin-Api-Key")
    return x_admin_api_key

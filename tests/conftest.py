"""
Shared fixtures for all tests.

Strategy: use an in-memory SQLite engine via aiosqlite for DB, a fakeredis
instance for Redis, and stub out JWT key loading so no real key files are needed.
Every test gets a fresh DB (rolled back) and a real AsyncClient against the ASGI app.
"""
import sys
import os
# Make tests/ directory importable so test modules can do `from helpers import ...`
sys.path.insert(0, os.path.dirname(__file__))

import uuid
import json
import asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

# ── env vars must be set before importing app modules ──────────────────────
import os
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("JWT_PRIVATE_KEY_PATH", "/tmp/priv.pem")
os.environ.setdefault("JWT_PUBLIC_KEY_PATH", "/tmp/pub.pem")
os.environ.setdefault("REDIS_HOST", "localhost")

# ── patch lru_cache so Settings re-reads env vars each import ──────────────
from config import get_settings
get_settings.cache_clear()

from database import Base
import models  # noqa: F401 — register all ORM models

# ── RSA key pair (generated once per session) ──────────────────────────────
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _private_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = _private_key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


# ── SQLite async engine (shared across tests, tables created once) ─────────
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_tables():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Each test gets a session that is rolled back after the test."""
    async with _engine.begin() as conn:
        async with _SessionLocal(bind=conn) as session:
            yield session
            await conn.rollback()


# ── fakeredis ──────────────────────────────────────────────────────────────
@pytest.fixture
def fake_redis():
    import fakeredis.aioredis as fakeredis
    r = fakeredis.FakeRedis(decode_responses=True)
    return r


# ── ASGI client factory ────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def client(db_session, fake_redis) -> AsyncGenerator[AsyncClient, None]:
    """
    Full ASGI client with:
    - SQLite DB session injected via dependency override
    - fakeredis injected via redis_client.get_redis patch
    - JWT key loading patched to use in-memory RSA keys
    """
    from main import app
    from database import get_db

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    with (
        patch("redis_client.get_redis", return_value=fake_redis),
        patch("rate_limit.get_redis", return_value=fake_redis),
        patch("routers.ai.get_redis", return_value=fake_redis),
        patch("jwt_utils._load_key", side_effect=lambda path: (
            _PRIVATE_PEM if "priv" in path else _PUBLIC_PEM
        )),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


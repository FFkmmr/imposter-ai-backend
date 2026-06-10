"""
tests/content — Расширенный контент-контракт MVP (13 категорий, 6 локалей).

Эти тесты загружают РЕАЛЬНЫЙ docs/seed_words.json в тестовую БД (через ORM,
т.к. seed.py использует Postgres jsonb-касты, несовместимые с SQLite) и проверяют
живой контракт локализации и premium-гейтинга на полном контенте.

Покрывает (Part A, шаг 4):
  GET /v1/categories?locale=es|pt|fr|de — локализованные name + непустой description (не fallback en)
  GET /v1/categories — у всех 13 категорий непустой локализованный description
  GET /v1/categories/premium — ровно 3 premium с непустыми preview_words
  GET /v1/categories/{premium_id}/words — без подписки 403; с is_premium 200
  GET /v1/categories/{id}/words?locale=fr|de — слова именно этой локали (не fallback en)
  mode=party — impostor_word != null
  GET /v1/localizations?locale=es|pt|fr|de — все ключи переведены; неизвестная локаль → fallback en
  Seed idempotency — повторная загрузка не дублирует
"""
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from helpers import api_headers
from models import Category, WordPack, User

SEED_FILE = Path(__file__).resolve().parents[2] / "docs" / "seed_words.json"
EXPECTED_PREMIUM = {"famous_brands", "world_landmarks", "mythology"}
LOCALES = ["en", "ru", "es", "pt", "fr", "de"]


def _load_seed_data() -> dict:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


async def _seed_db(db: AsyncSession, data: dict) -> None:
    """Idempotent ORM-based seed mirroring api/seed.py logic for SQLite tests."""
    locales = data.get("meta", {}).get("locales") or LOCALES
    for idx, cat_data in enumerate(data["categories"]):
        slug = cat_data["slug"]
        existing = (
            await db.execute(select(Category).where(Category.slug == slug))
        ).scalar_one_or_none()
        if existing:
            cat = existing
        else:
            cat = Category(
                slug=slug,
                name=cat_data["name"],
                description=cat_data.get("description", {}),
                is_premium=cat_data.get("is_premium", False),
                is_active=True,
                sort_order=idx,
            )
            db.add(cat)
            await db.flush()

        word_count = (
            await db.execute(
                select(func.count()).select_from(WordPack).where(WordPack.category_id == cat.id)
            )
        ).scalar()
        if word_count and word_count > 0:
            continue

        for word in cat_data["words"]:
            for locale in locales:
                civilian = word["civilian_word"].get(locale) or word["civilian_word"].get("en")
                impostor_raw = word.get("impostor_word")
                impostor = (
                    (impostor_raw.get(locale) or impostor_raw.get("en"))
                    if impostor_raw
                    else None
                )
                db.add(
                    WordPack(
                        category_id=cat.id,
                        locale=locale,
                        civilian_word=civilian,
                        impostor_word=impostor,
                        difficulty=word.get("difficulty", "medium"),
                        tags=[],
                    )
                )
        await db.flush()


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession):
    """Loads the full real seed content into the test DB."""
    data = _load_seed_data()
    await _seed_db(db_session, data)
    await db_session.flush()
    return data


# ── GET /v1/categories — локализация на es/pt/fr/de ─────────────────────────

class TestCategoriesLocalization:
    @pytest.mark.parametrize("locale", ["es", "pt", "fr", "de"])
    async def test_localized_name_and_description_not_en_fallback(
        self, client: AsyncClient, seeded, locale
    ):
        resp = await client.get(f"/v1/categories?locale={locale}", headers=api_headers())
        assert resp.status_code == 200
        cats = resp.json()["categories"]
        assert len(cats) == 13

        by_slug = {c["slug"]: c for c in cats}
        en_data = {c["slug"]: c for c in seeded["categories"]}

        for slug, cat in by_slug.items():
            expected_name = en_data[slug]["name"][locale]
            expected_desc = en_data[slug]["description"][locale]
            assert cat["name"] == expected_name, f"{slug}: name not localized to {locale}"
            assert cat["description"].strip(), f"{slug}: empty description for {locale}"
            assert cat["description"] == expected_desc, f"{slug}: desc not localized to {locale}"
            # Guard against silent en-fallback: localized value must differ from en
            # when the seed actually provides a distinct translation.
            if en_data[slug]["description"]["en"] != expected_desc:
                assert cat["description"] != en_data[slug]["description"]["en"]

    async def test_all_13_categories_have_nonempty_localized_description(
        self, client: AsyncClient, seeded
    ):
        for locale in LOCALES:
            resp = await client.get(f"/v1/categories?locale={locale}", headers=api_headers())
            assert resp.status_code == 200
            cats = resp.json()["categories"]
            assert len(cats) == 13, f"expected 13 categories, got {len(cats)}"
            for c in cats:
                assert c["description"].strip(), (
                    f"category '{c['slug']}' has empty description for locale '{locale}'"
                )


# ── GET /v1/categories/premium ──────────────────────────────────────────────

class TestPremiumCategories:
    async def test_exactly_three_premium_with_preview_words(
        self, client: AsyncClient, seeded
    ):
        resp = await client.get("/v1/categories/premium", headers=api_headers())
        assert resp.status_code == 200
        cats = resp.json()["categories"]
        slugs = {c["slug"] for c in cats}
        assert slugs == EXPECTED_PREMIUM, f"premium slugs mismatch: {slugs}"
        assert len(cats) == 3
        for c in cats:
            assert isinstance(c["preview_words"], list)
            assert len(c["preview_words"]) >= 1, f"{c['slug']} has empty preview_words"
            assert len(c["preview_words"]) <= 3
            assert all(w.strip() for w in c["preview_words"])

    @pytest.mark.parametrize("locale", ["es", "fr", "de"])
    async def test_premium_preview_localized(self, client: AsyncClient, seeded, locale):
        resp = await client.get(
            f"/v1/categories/premium?locale={locale}", headers=api_headers()
        )
        assert resp.status_code == 200
        en_data = {c["slug"]: c for c in seeded["categories"] if c.get("is_premium")}
        for c in resp.json()["categories"]:
            assert c["name"] == en_data[c["slug"]]["name"][locale]


# ── GET /v1/categories/{premium_id}/words — premium gating ──────────────────

class TestPremiumWordsGating:
    async def _premium_cat_id(self, db_session: AsyncSession) -> uuid.UUID:
        cat = (
            await db_session.execute(
                select(Category).where(Category.slug == "famous_brands")
            )
        ).scalar_one()
        return cat.id

    async def test_premium_words_without_subscription_403(
        self, client: AsyncClient, seeded, db_session: AsyncSession
    ):
        cat_id = await self._premium_cat_id(db_session)
        did = str(uuid.uuid4())
        await client.post("/v1/users", json={"device_id": did})  # non-premium user
        resp = await client.get(
            f"/v1/categories/{cat_id}/words", headers=api_headers(did)
        )
        assert resp.status_code == 403

    async def test_premium_words_with_subscription_200(
        self, client: AsyncClient, seeded, db_session: AsyncSession
    ):
        cat_id = await self._premium_cat_id(db_session)
        did = str(uuid.uuid4())
        await client.post("/v1/users", json={"device_id": did})
        user = (
            await db_session.execute(
                select(User).where(User.device_id == uuid.UUID(did))
            )
        ).scalar_one()
        user.is_premium = True
        await db_session.flush()

        resp = await client.get(
            f"/v1/categories/{cat_id}/words", headers=api_headers(did)
        )
        assert resp.status_code == 200
        assert len(resp.json()["words"]) >= 1


# ── GET /v1/categories/{id}/words?locale=fr|de — слова именно этой локали ────

class TestWordsLocale:
    @pytest.mark.parametrize("locale", ["fr", "de"])
    async def test_words_returned_in_requested_locale(
        self, client: AsyncClient, seeded, db_session: AsyncSession, locale
    ):
        # animals: known distinct translations (Éléphant / Elefant vs Elephant)
        cat = (
            await db_session.execute(select(Category).where(Category.slug == "animals"))
        ).scalar_one()
        did = str(uuid.uuid4())

        resp = await client.get(
            f"/v1/categories/{cat.id}/words?locale={locale}&count=5",
            headers=api_headers(did),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["locale"] == locale, "server fell back to en instead of requested locale"

        # Build the set of valid civilian words for this locale from seed data,
        # and the set of en words. Returned words must be the locale variant.
        animals = next(c for c in seeded["categories"] if c["slug"] == "animals")
        locale_words = {w["civilian_word"].get(locale) for w in animals["words"]}
        returned = {w["civilian_word"] for w in body["words"]}
        assert returned <= locale_words, (
            f"returned words {returned} not all in {locale} set"
        )

    async def test_party_mode_impostor_word_not_null(
        self, client: AsyncClient, seeded, db_session: AsyncSession
    ):
        cat = (
            await db_session.execute(select(Category).where(Category.slug == "animals"))
        ).scalar_one()
        did = str(uuid.uuid4())
        resp = await client.get(
            f"/v1/categories/{cat.id}/words?mode=party&count=5",
            headers=api_headers(did),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "party"
        assert len(body["words"]) >= 1
        for w in body["words"]:
            assert w["impostor_word"] is not None, "party mode must have impostor_word"


# ── GET /v1/localizations ───────────────────────────────────────────────────

class TestLocalizations:
    EXPECTED_KEYS = {"paywall.title", "paywall.subtitle", "onboarding.step1.title"}

    @pytest.mark.parametrize("locale", ["es", "pt", "fr", "de"])
    async def test_all_keys_translated(self, client: AsyncClient, locale):
        resp = await client.get(
            f"/v1/localizations?locale={locale}", headers=api_headers()
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["locale"] == locale
        strings = body["strings"]
        assert self.EXPECTED_KEYS <= set(strings.keys()), (
            f"missing keys for {locale}: {self.EXPECTED_KEYS - set(strings.keys())}"
        )
        for k, v in strings.items():
            assert v.strip(), f"empty value for key '{k}' in locale '{locale}'"

    async def test_localized_value_differs_from_en(self, client: AsyncClient):
        en = (await client.get("/v1/localizations?locale=en", headers=api_headers())).json()[
            "strings"
        ]
        de = (await client.get("/v1/localizations?locale=de", headers=api_headers())).json()[
            "strings"
        ]
        assert de["paywall.title"] != en["paywall.title"], "de not actually localized"

    async def test_unknown_locale_falls_back_to_en(self, client: AsyncClient):
        en = (await client.get("/v1/localizations?locale=en", headers=api_headers())).json()[
            "strings"
        ]
        resp = await client.get("/v1/localizations?locale=zz", headers=api_headers())
        assert resp.status_code == 200
        body = resp.json()
        # locale echoed back as requested, but strings are en fallback content
        assert body["strings"] == en


# ── Seed idempotency ────────────────────────────────────────────────────────

class TestSeedIdempotency:
    async def test_reseed_does_not_duplicate(
        self, client: AsyncClient, seeded, db_session: AsyncSession
    ):
        data = _load_seed_data()
        cats_before = (
            await db_session.execute(select(func.count()).select_from(Category))
        ).scalar()
        words_before = (
            await db_session.execute(select(func.count()).select_from(WordPack))
        ).scalar()

        # run the seed routine a second time
        await _seed_db(db_session, data)
        await db_session.flush()

        cats_after = (
            await db_session.execute(select(func.count()).select_from(Category))
        ).scalar()
        words_after = (
            await db_session.execute(select(func.count()).select_from(WordPack))
        ).scalar()

        assert cats_after == cats_before == 13
        assert words_after == words_before
        # 13 categories × 20 words × 6 locales = 1560
        assert words_after == 13 * 20 * len(LOCALES)

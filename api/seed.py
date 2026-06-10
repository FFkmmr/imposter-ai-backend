"""
Idempotent seed script. Run once at container startup.
Loads seed_words.json into categories + word_packs tables.
"""
import asyncio
import json
import uuid
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, text
from config import get_settings

SEED_FILE = Path(__file__).parent / "seed_words.json"


async def seed():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    locales = data.get("meta", {}).get("locales") or ["en", "ru"]

    async with Session() as db:
        for idx, cat_data in enumerate(data["categories"]):
            slug = cat_data["slug"]

            result = await db.execute(
                text("SELECT id FROM categories WHERE slug = :slug"),
                {"slug": slug},
            )
            existing = result.fetchone()

            if existing:
                cat_id = existing[0]
                print(f"  skip category '{slug}' (already exists)")
            else:
                cat_id = uuid.uuid4()
                await db.execute(
                    text(
                        "INSERT INTO categories (id, slug, name, description, is_premium, is_active, sort_order) "
                        "VALUES (:id, :slug, cast(:name as jsonb), cast(:desc as jsonb), :premium, true, :order)"
                    ),
                    {
                        "id": str(cat_id),
                        "slug": slug,
                        "name": json.dumps(cat_data["name"]),
                        "desc": json.dumps(cat_data.get("description", {})),
                        "premium": cat_data.get("is_premium", False),
                        "order": idx,
                    },
                )
                print(f"  + category '{slug}'")

            result = await db.execute(
                text("SELECT COUNT(*) FROM word_packs WHERE category_id = :cid"),
                {"cid": str(cat_id)},
            )
            count = result.scalar()
            if count and count > 0:
                print(f"    skip words for '{slug}' ({count} already exist)")
                continue

            for word in cat_data["words"]:
                for locale in locales:
                    civilian = word["civilian_word"].get(locale) or word["civilian_word"].get("en")
                    impostor_raw = word.get("impostor_word")
                    impostor = impostor_raw.get(locale) or impostor_raw.get("en") if impostor_raw else None

                    await db.execute(
                        text(
                            "INSERT INTO word_packs (id, category_id, locale, civilian_word, impostor_word, difficulty, tags) "
                            "VALUES (:id, :cid, :locale, :cw, :iw, :diff, cast(:tags as jsonb))"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "cid": str(cat_id),
                            "locale": locale,
                            "cw": civilian,
                            "iw": impostor,
                            "diff": word.get("difficulty", "medium"),
                            "tags": "[]",
                        },
                    )

            print(f"    + {len(cat_data['words'])} words × {len(locales)} locales for '{slug}'")

        await db.commit()

    await engine.dispose()
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())

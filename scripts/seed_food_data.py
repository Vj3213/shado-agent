"""Seed the food knowledge base into Postgres. Idempotent — safe to re-run."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.food_data import DISHES, FOOD_DATA, SEASONS  # noqa: E402
from storage import PostgresRepository  # noqa: E402
from agent.app.config import load_config  # noqa: E402


def seed(repo: PostgresRepository) -> tuple[int, int, int]:
    with repo._connect() as conn, conn.cursor() as cur:
        n_ing, n_dish, n_map = 0, 0, 0
        for category, entries in FOOD_DATA.items():
            for name, common_names in entries:
                cur.execute(
                    "INSERT INTO ingredient (name, category, common_names, season) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category, "
                    "common_names = EXCLUDED.common_names, season = EXCLUDED.season",
                    (name, category, common_names, SEASONS.get(name, "all")),
                )
                n_ing += 1
        for dish, (meal_types, ingredients) in DISHES.items():
            cur.execute(
                "INSERT INTO dish (name, meal_types) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET meal_types = EXCLUDED.meal_types",
                (dish, meal_types),
            )
            n_dish += 1
            for ing in ingredients:
                cur.execute(
                    "INSERT INTO dish_ingredient (dish_id, ingredient_id) "
                    "SELECT d.id, i.id FROM dish d, ingredient i "
                    "WHERE d.name = %s AND i.name = %s "
                    "ON CONFLICT (dish_id, ingredient_id) DO NOTHING",
                    (dish, ing),
                )
                n_map += 1
    return n_ing, n_dish, n_map


if __name__ == "__main__":
    repo = PostgresRepository(load_config().database_url)
    repo.init_schema()
    ingredients, dishes, mappings = seed(repo)
    print(f"Seeded: {ingredients} ingredients, {dishes} dishes, {mappings} mappings")

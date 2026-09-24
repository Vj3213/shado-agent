"""One-shot DB bootstrap: applies storage/schema.sql, then seeds the food KB."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import PostgresRepository  # noqa: E402
from agent.app.config import load_config  # noqa: E402

if __name__ == "__main__":
    config = load_config()
    repo = PostgresRepository(config.database_url)
    repo.init_schema()
    print(f"Schema applied to {config.database_url.rsplit('/', 1)[-1]}")

    from scripts.seed_food_data import seed  # noqa: E402

    ingredients, dishes, mappings = seed(repo)
    print(f"Seeded: {ingredients} ingredients, {dishes} dishes, {mappings} mappings")


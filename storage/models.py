from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

MealType = Literal["breakfast", "lunch", "dinner", "snack"]
Direction = Literal["in", "out"]


@dataclass(frozen=True)
class MessageRecord:
    group_id: str
    sender: str
    text: str
    direction: Direction
    created_at: datetime
    source: str = "agent"  # 'operator' = written by the human; 'agent' = generated


@dataclass(frozen=True)
class MealEntry:
    day: date
    item: str
    meal_type: MealType


@dataclass(frozen=True)
class BotLoopState:
    group_id: str
    peer_sender: str
    consecutive_turns: int
    last_reset_at: datetime | None


@dataclass(frozen=True)
class IngredientRow:
    id: int
    name: str
    common_names: str


@dataclass(frozen=True)
class DishRow:
    name: str
    meal_types: str


@dataclass(frozen=True)
class SuggestionRow:
    dish: str
    status: str
    created_at: datetime

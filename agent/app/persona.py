"""Persona and prompt assembly for the Gemini call — DB-grounded (RAG-lite)."""

from __future__ import annotations

from datetime import date, datetime

from storage.models import DishRow, MealEntry, MessageRecord, SuggestionRow

SYSTEM_PROMPT = """\
You are a warm, friendly human member of an Indian family WhatsApp group. You chat \
casually (light Hinglish is natural: "khana", "yaar", "kya banau" etc.), and you love \
helping everyone decide what to eat. You can also just chat normally about anything \
else going on in the family — greetings, small talk, plans — like a person would.

How you work (output JSON fields):
- "is_food_related": true if the conversation is about food, meals, hunger, cooking, \
or the food suggestions being discussed. Small talk and family chat -> false.
- "reply": what you say. 1-3 sentences, plain text, no markdown, at most one emoji. \
Vary phrasing every time; never use templated or repeated sentences. Never start \
replies the same way; match the energy of the message.
- "meals": every food the members clearly said they ate or will eat, with meal type \
(breakfast/lunch/dinner/snack). Empty list if none.
- "suggested_dishes": the exact dish names you suggest in this reply (from the \
SUGGESTION POOL below when possible). Empty list if you suggested nothing.
- "selected_dish": if the member clearly picked or confirmed one of the dishes from \
YOUR SUGGESTED RECENTLY (e.g. "haan wo banau", "paratha khaya tha"), put that exact \
name here, else null.
- "react": MOST messages deserve no reaction at all (null). React only when the \
moment genuinely calls for it — a real joke gets 😂, genuine good news gets 👍, a \
sad update gets ❤️. A couple of reactions a day feels human; reacting often feels \
like a bot. Choose ONE emoji ONLY from: 👍 ❤️ 😂 😮 😢 🙏 — else null. Reacting and \
replying together is fine when the moment truly calls for both.

Your food habits:
- CURRENT TIME below tells you the real date and time. For anything time-related \
("abhi kya time hai", chai/dinner timing) use ONLY that — never guess or make up \
a time. Suggest what fits the time of day: breakfast-type things (poha, upma, \
paratha) only in the morning; chai + light snacks in the late afternoon; proper \
dinner dishes in the evening; khichdi/soup when it's late night.
- Suggest 2-3 dishes at a time from the SUGGESTION POOL when it fits. The pool is \
already filtered against what was eaten recently and what you already suggested — \
so do NOT re-suggest those.
- TODAY'S MEAL LOG tells you what they ate today: never suggest repeats of it.
- YOU SUGGESTED RECENTLY shows your past suggestions with status. "pending" means \
you suggested it and they never confirmed — you may casually follow up ("Aloo \
paratha banaya tha?") instead of re-suggesting. "selected" means they picked it.
- If they ask for MORE options or reject suggestions ("kuch aur", "ye nahi"), give \
genuinely different ideas than everything already mentioned in the chat.
- You may politely disagree with the other meal-suggestion app in the group, like a \
normal person would ("wo bhaari ho jayega dinner me, poha khao").
- If the conversation is NOT food-related, just chat normally as a family member — \
no meal suggestions, no food pushing. Keep it short and natural.
- You never say or imply that you are an AI, a bot, an assistant, or any automated \
system. You never mention WhatsApp support. You are just a person.
- Match the chat type from CHAT TYPE below: in a group you address everyone; in a \
private 1:1 chat you talk directly to that one person — never say "sab" (everyone) \
in a private chat.
- STYLE EXAMPLES show how your person (whose side you're on) writes. Mirror their \
length, Hinglish mix, emoji habits, and phrasing — as if the same hand wrote both \
messages. But you are still a separate presence: never claim to BE that person.
- In an ongoing chat it's natural to sometimes end with a short casual question back \
("kya socha?", "bana kya?") — but not every time, and never two replies in a row.
- If a message mentions eating something, acknowledge it naturally in your reply.
- If the message is pure noise (sticker text, forwarded junk, empty), reply with null.
- If the latest message is exactly __INITIATE__, YOU are starting the conversation. \
You must reply (never null for this): check YOU SUGGESTED RECENTLY for a pending \
dish and casually ask about it ("kal wala paratha banaya tha?"), else give a short \
natural greeting or a time-appropriate nudge ("chai ke liye utho"). One short \
message, like a friend pinging — don't force food talk.
"""


def _label_sender(sender: str, peer_bot_sender: str | None) -> str:
    if peer_bot_sender and sender == peer_bot_sender:
        return "Other meal-suggestion app"
    number = sender.split("@")[0]
    return f"Group member ({number[-4:]})"


def _season(month: int) -> str:
    if 3 <= month <= 6:
        return "garmi/summer (hot)"
    if 7 <= month <= 9:
        return "monsoon tail — garmi khatam ho rahi hai, warm, NOT thand yet"
    if month <= 11:
        return "pleasant post-monsoon"
    return "thand/winter (cold)"


def build_time_block(now: datetime) -> str:
    hour = now.hour
    tod = (
        "late night"
        if hour >= 23 or hour < 5
        else "morning"
        if hour < 12
        else "afternoon"
        if hour < 17
        else "evening"
        if hour < 21
        else "night"
    )
    tz = now.tzname() or "local"
    return (
        f"CURRENT TIME: {now.strftime('%A, %d %b %Y, %I:%M %p')} {tz} "
        f"({tod}; season: {_season(now.month)}) — India. Use this for anything "
        f"time or weather related; never guess times, seasons or weather."
    )


def build_conversation_block(history: list[MessageRecord], peer_bot_sender: str | None) -> str:
    lines = []
    for msg in history:
        if msg.direction == "out":
            lines.append(f"You (earlier): {msg.text}")
        else:
            lines.append(f"{_label_sender(msg.sender, peer_bot_sender)}: {msg.text}")
    return "\n".join(lines) if lines else "(no messages yet)"


def build_food_context(
    today: date,
    meals_today: list[MealEntry],
    eaten_recent: list[MealEntry],
    suggestions: list[SuggestionRow],
    pool: list[DishRow],
) -> str:
    meals_block = (
        ", ".join(f"{m.item} ({m.meal_type})" for m in meals_today)
        if meals_today
        else "nothing logged yet"
    )
    eaten_block = (
        "; ".join(f"{m.item} ({m.day})" for m in eaten_recent) if eaten_recent else "none"
    )
    suggestions_block = (
        "\n".join(f"  - {s.dish} ({s.status})" for s in suggestions)
        if suggestions
        else "  none yet"
    )
    pool_block = (
        "\n".join(f"  {d.name} [{d.meal_types}]" for d in pool) if pool else "  (database empty)"
    )
    return (
        f"TODAY'S MEAL LOG ({today.isoformat()}): {meals_block}\n\n"
        f"EATEN IN LAST FEW DAYS (do not suggest repeats): {eaten_block}\n\n"
        f"YOU SUGGESTED RECENTLY:\n{suggestions_block}\n\n"
        f"SUGGESTION POOL (from the food database — prefer these, use exact names):\n{pool_block}\n"
    )


def build_style_block(operator_examples: list[str]) -> str:
    if not operator_examples:
        return "STYLE EXAMPLES: none yet — write naturally."
    quoted = "\n".join(f'  • "{e}"' for e in operator_examples)
    return f"STYLE EXAMPLES (mirror this style):\n{quoted}"


def build_user_prompt(
    history: list[MessageRecord],
    meals_today: list[MealEntry],
    eaten_recent: list[MealEntry],
    suggestions: list[SuggestionRow],
    pool: list[DishRow],
    peer_bot_sender: str | None,
    today: date,
    incoming_text: str,
    now: datetime,
    chat_id: str = "",
    operator_examples: list[str] | None = None,
) -> str:
    chat_type = (
        "group chat" if chat_id.endswith("@g.us") else "PRIVATE 1:1 chat"
        if chat_id
        else "(unknown)"
    )
    return (
        f"{build_time_block(now)}\n\n"
        f"CHAT TYPE: {chat_type}\n\n"
        f"{build_style_block(operator_examples or [])}\n\n"
        f"{build_food_context(today, meals_today, eaten_recent, suggestions, pool)}\n"
        f"RECENT GROUP CHAT (oldest to newest):\n"
        f"{build_conversation_block(history, peer_bot_sender)}\n\n"
        f"Respond to the latest message above."
    )

"""List the models your Gemini API key can actually use (free tier view).

Run:  .venv/bin/python scripts/list_models.py
Requires GEMINI_API_KEY in .env.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google import genai

from agent.app.config import load_config

if __name__ == "__main__":
    config = load_config()
    if not config.gemini_api_key or config.gemini_api_key == "TODO":
        print("GEMINI_API_KEY not set in .env — add your free key first.")
        sys.exit(1)

    client = genai.Client(api_key=config.gemini_api_key)
    print("Models available to your key (generateContent):\n")
    for model in client.models.list():
        actions = getattr(model, "supported_actions", None) or []
        if actions and "generateContent" not in actions:
            continue
        context = getattr(model, "input_token_limit", None)
        print(f"  {model.name:<45} context: {context if context else '?'}")

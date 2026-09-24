"""Make the bot start a conversation itself — or send YOUR own opener.

Usage:
    .venv/bin/python scripts/initiate.py                     # model composes an opener (group)
    .venv/bin/python scripts/initiate.py dm                  # model-composed opener in DM (needs DM_JID)
    .venv/bin/python scripts/initiate.py dm aaj kya khaya?   # send YOUR text verbatim, then
                                                             # the model continues naturally
Requires the gateway to be running (it owns the WhatsApp socket).
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.app.config import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot-initiated messages")
    parser.add_argument("target", nargs="?", default="group", choices=["group", "dm"])
    parser.add_argument("text", nargs="*", help="your own opener (sent verbatim)")
    args = parser.parse_args()

    config = load_config()
    if args.target == "group":
        chat_id = config.group_jid
    else:
        chat_id = os.environ.get("DM_JID", "")
        if not chat_id:
            print("Set DM_JID=<person's jid, e.g. 9198xxx@s.whatsapp.net> in .env first.")
            sys.exit(1)

    port = os.environ.get("GATEWAY_PORT", "8090")
    custom_text = " ".join(args.text).strip()

    if custom_text:
        endpoint, body = "/send", {"chat_id": chat_id, "text": custom_text}
        print(f"Sending YOUR opener to {chat_id}:\n  {custom_text}")
    else:
        endpoint, body = "/initiate", {"chat_id": chat_id}
        print(f"Pinging bot to start a conversation in: {chat_id}")

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{endpoint}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
        print(json.dumps(body, ensure_ascii=False, indent=2))
    except Exception as error:
        print(f"Failed: {error!r}\n(Is the gateway running? bash scripts/run_all.sh)")


if __name__ == "__main__":
    main()

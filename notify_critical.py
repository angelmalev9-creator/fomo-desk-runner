"""Send one critical Telegram alert to the owner, bypassing quiet mode.

For desk-health restarts. Reads TG_TOKEN from env/.secrets; never prints it.
Usage: .venv/bin/python notify_critical.py "text"
"""

from __future__ import annotations

import sys

from config import Config
from telegram import TelegramNotifier
from util import load_secrets, secret


def main(argv: list[str]) -> int:
    text = " ".join(argv).strip()
    if not text:
        print("usage: notify_critical.py TEXT")
        return 2
    load_secrets()
    cfg = Config()
    owner = cfg.get("desk.owner_chat_id") or 0
    n = TelegramNotifier(token=secret("TG_TOKEN"), owner_chat_id=int(owner) if owner else None)
    ok = n.send_sync(text)
    print("sent" if ok else "not sent")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""
telegram_notify.py — posts an alert to your Telegram channel for every
plot that scraper.py just found newly reserved (diff_new_reservations.json).

Required environment variables (set as GitHub Secrets — see README.md):
  TELEGRAM_BOT_TOKEN   the bot token from @BotFather
  TELEGRAM_CHAT_ID     your channel's id or @username (bot must be admin there)

Run this AFTER scraper.py, in the same workflow step or job.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

DIFF_FILE = "diff_new_reservations.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def build_message(key):
    """key looks like: '<city>|<project>|<block>|<plot>'"""
    parts = key.split("|")
    if len(parts) != 4:
        return f"قطعة اتحجزت (تفاصيل ناقصة): {key}"
    city, project, block, plot = parts
    return (
        "🔴 قطعة جديدة اتحجزت — المرحلة 11\n\n"
        f"📍 المدينة: {city}\n"
        f"🏗 المشروع: {project}\n"
        f"🧱 المربع: {block}\n"
        f"📌 رقم القطعة: {plot}\n\n"
        "تابع باقي القطع المتاحة على موقعك."
    )


def send(token, chat_id, text):
    r = requests.post(
        TELEGRAM_API.format(token=token),
        data={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    if not r.ok:
        print(f"[telegram] failed to send: {r.status_code} {r.text}", file=sys.stderr)
    return r.ok


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping notify")
        return

    p = Path(DIFF_FILE)
    if not p.exists():
        print(f"[telegram] {DIFF_FILE} not found — did scraper.py run first?")
        return

    new_keys = json.loads(p.read_text(encoding="utf-8"))
    if not new_keys:
        print("[telegram] no new reservations this run — nothing to send")
        return

    print(f"[telegram] sending {len(new_keys)} notification(s)")
    for key in new_keys:
        send(token, chat_id, build_message(key))
        time.sleep(1)  # stay well under Telegram's rate limits


if __name__ == "__main__":
    main()

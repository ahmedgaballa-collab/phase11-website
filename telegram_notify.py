"""
telegram_notify.py — posts a branded alert to your Telegram channel for
every plot that scraper.py just found newly reserved
(diff_new_reservations.json).

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
SPAM_GUARD_THRESHOLD = 15  # more "new" reservations than this in one run is
                            # almost certainly a bug, not 15 real bookings
                            # between two 5-minute checks — see README.md

# --- Personalize these two -------------------------------------------------
SITE_URL = "https://YOUR-DOMAIN-HERE"  # TODO: put your live site link once deployed
AGENT_NAME = "أحمد جاب الله"
AGENT_PHONE_DISPLAY = "01009566779"
# -----------------------------------------------------------------------------

FEATURE_EMOJI = {"corner": "🔺 ناصية", "garden": "🌳 حديقة", "view": "🌊 إطلالة"}


def fmt_money(raw):
    """'25,188.48' -> '$25,188' (drop cents, keep thousands separator)."""
    try:
        n = float(str(raw).replace(",", "").strip() or 0)
        return f"${n:,.0f}"
    except ValueError:
        return f"${raw}"


def fmt_area(raw):
    try:
        n = float(str(raw).replace(",", "").strip() or 0)
        return f"{n:,.0f} م²"
    except ValueError:
        return f"{raw} م²"


def build_message(item, today_total, updated_at):
    features = [label for key, label in FEATURE_EMOJI.items() if item.get(key)]
    features_line = f"✨ {' · '.join(features)}\n" if features else ""

    return (
        "🏝️ قطعة جديدة اتحجزت — المرحلة 11\n\n"
        f"📍 {item['city']}\n"
        f"🏗️ {item['project']}\n"
        f"🧱 المربع: {item['block']}   |   🔢 القطعة: {item['plot']}\n"
        f"📐 المساحة: {fmt_area(item['area'])}\n"
        f"{features_line}"
        f"💰 المقدم: {fmt_money(item['down'])}\n\n"
        f"📊 إجمالي القطع اللي اتحجزت النهاردة: {today_total}\n"
        f"🕓 {updated_at}\n"
        "━━━━━━━━━━━━━━━\n"
        "عايز تشوف قطعة تناسب ميزانيتك من الباقي؟\n"
        f"🌐 {SITE_URL}\n"
        f"📲 {AGENT_NAME} — {AGENT_PHONE_DISPLAY}"
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

    data = json.loads(p.read_text(encoding="utf-8"))
    items = data.get("items", [])
    today_total = data.get("today_total", "?")
    updated_at = data.get("updatedAt", "")

    if not items:
        print("[telegram] no new reservations this run — nothing to send")
        return

    if len(items) > SPAM_GUARD_THRESHOLD:
        print(f"[telegram] {len(items)} 'new' reservations in one run — "
              f"over the sanity threshold ({SPAM_GUARD_THRESHOLD}), sending ONE "
              f"warning instead of flooding the channel")
        warning = (
            "⚠️ تنبيه فني — تم إيقاف إشعارات هذه الدفعة مؤقتًا\n\n"
            f"النظام رصد {len(items)} قطعة \"جديدة\" في تشغيلة واحدة، وهو رقم "
            "غير منطقي لفترة 5 دقائق. على الأغلب تغيير تقني في نظام المطابقة، "
            "مش حجوزات حقيقية بهذا الحجم. تم تجاهل الإرسال التفصيلي لحماية "
            "القناة — راجع status.json يدويًا قبل الوثوق في الأرقام القادمة."
        )
        send(token, chat_id, warning)
        return

    print(f"[telegram] sending {len(items)} notification(s)")
    for item in items:
        send(token, chat_id, build_message(item, today_total, updated_at))
        time.sleep(1)  # stay well under Telegram's rate limits


if __name__ == "__main__":
    main()

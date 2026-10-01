"""
telegram_notify.py — posts a branded alert to your Telegram channel for
every plot that scraper.py just found newly reserved
(diff_new_reservations.json).

Required environment variables (set as GitHub Secrets — see README.md):
  TELEGRAM_BOT_TOKEN   the bot token from @BotFather
  TELEGRAM_CHAT_ID     your channel's id or @username (bot must be admin there)

Run this AFTER scraper.py, in the same workflow step or job.
"""

import html
import json
import os
import sys
import time
from pathlib import Path

import requests

DIFF_FILE = "diff_new_reservations.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
SPAM_GUARD_THRESHOLD = 100  # more "new" reservations than this in one run is
                            # almost certainly a bug, not 15 real bookings
                            # between two 5-minute checks — see README.md

SITE_URL = "https://phase11ahmedgaballah-beta.vercel.app/"
DASHBOARD_URL = SITE_URL + "dashboard"

FEATURE_EMOJI = {"corner": "🔺 ناصية", "garden": "🌳 حديقة", "view": "🌊 إطلالة"}


def esc(s):
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(str(s or ""), quote=False)


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


def clean_project(project, city):
    """'شمال الحى العاشر - بنى سويف الجديدة' -> 'شمال الحى العاشر' (city is on its own line)."""
    fold = lambda x: str(x or "").replace("ى", "ي").replace("ة", "ه").replace("أ", "ا").replace("إ", "ا")
    p = str(project or "").strip()
    fp, fc = fold(p), fold(city).strip()
    if fc and fp.endswith(fc) and len(fp) > len(fc):
        p = p[: len(p) - len(fc)].rstrip(" -–")
    return p or project


def title_line(seq=None, late=False, alloc_label=None):
    """Normal: '(#12 النهارده)'. A late booking (after midnight / on a day off,
    before the next 11:00 allocation) counts for the last allocation day."""
    if late and alloc_label:
        tag = f"#{seq} · " if seq else ""
        return f"⏰ <b>حجز متأخر — المرحلة 11</b>  <i>({tag}تخصيص {esc(alloc_label)})</i>"
    return "🏝️ <b>قطعة جديدة اتحجزت — المرحلة 11</b>" + (f"  <i>(#{seq} النهارده)</i>" if seq else "")


def build_message(item, seq=None, late=False, alloc_label=None):
    """One booking. `seq` = this booking's number today (1, 2, 3...) so every
    message carries its own number instead of the same daily total.
    No timestamp: the check runs every ~30 min, so we don't know the exact
    booking minute — Telegram already shows when the message was posted."""
    features = [label for key, label in FEATURE_EMOJI.items() if item.get(key)]
    project = clean_project(item.get("project"), item.get("city", ""))
    lines = [
        title_line(seq, late, alloc_label),
        "",
        f"📍 <b>{esc(item['city'])}</b>",
        f"🏗️ {esc(project)}",
        f"🧱 المربع: {esc(item['block'])}   |   🔢 القطعة: {esc(item['plot'])}",
        f"📐 المساحة: {fmt_area(item['area'])}",
    ]
    if features:
        lines.append("✨ " + " · ".join(features))
    lines += [
        f"💰 المقدم: <b>{fmt_money(item['down'])}</b>",
        "",
        f'🔍 <a href="{SITE_URL}">شوف القطع المتاحة</a>   ·   📊 <a href="{DASHBOARD_URL}">متابعة الحجوزات</a>',
    ]
    return "\n".join(lines)


def send(token, chat_id, text):
    r = requests.post(
        TELEGRAM_API.format(token=token),
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
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
    # consume the file so the same bookings can never be posted twice
    try:
        p.replace(p.with_suffix(".sent.json"))
    except Exception:
        pass
    items = data.get("items", [])
    today_total = data.get("today_total", "?")

    if not items:
        print("[telegram] no new reservations this run — nothing to send")
        return

    if len(items) > SPAM_GUARD_THRESHOLD:
        # far too many for real bookings between two checks — almost certainly
        # a read glitch; the channel is public, so stay silent
        print(f"[telegram] {len(items)} 'new' reservations in one run — over "
              f"{SPAM_GUARD_THRESHOLD}, treating as a glitch and sending nothing")
        return

    print(f"[telegram] sending {len(items)} notification(s)")
    # number each booking within today: e.g. today_total=19 and 3 new -> #17, #18, #19
    first = today_total - len(items) + 1 if isinstance(today_total, int) else None
    for i, item in enumerate(items):
        seq = first + i if first and first > 0 else None
        send(token, chat_id, build_message(item, seq, late=bool(data.get("late")),
                                          alloc_label=data.get("alloc_label")))
        # Telegram allows ~20 posts/min in a channel — pace bigger batches
        time.sleep(1 if len(items) <= 15 else 3.2)


if __name__ == "__main__":
    main()

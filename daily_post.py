"""
daily_post.py — the 8 PM Facebook post kit.

On every allocation day at 20:00 (Cairo) it opens the site's daily.html card,
screenshots it, and sends Ahmed (privately, NOT the channel) the image plus a
ready-to-paste Facebook post. Ahmed posts it himself.

  python daily_post.py --whoami   find your private chat id with the bot (send the bot "hi" first)
  python daily_post.py --test     build and send it now (any day/time)
  python daily_post.py            what the sync loop runs: sends once per allocation day after 20:00

Needs in .env: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
SENT = HERE / "daily_post_sent.json"
LOCK = HERE / "daily_post.lock"
CARD = HERE / "daily_card.png"
SITE = os.environ.get("DAILY_SITE", "https://phase11ahmedgaballah-beta.vercel.app/")
POST_HOUR = int(os.environ.get("DAILY_POST_HOUR", "20"))
PHONE = "01009566779"


def load_env():
    import scraper
    scraper.load_dotenv()
    return scraper


def tg(method, **kw):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    files = kw.pop("files", None)
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=kw, files=files, timeout=60)
    if not r.ok:
        print(f"[daily] telegram {method} failed: {r.status_code} {r.text[:200]}")
    return r


def whoami():
    r = requests.get(f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN')}/getUpdates", timeout=30).json()
    chats = {}
    for u in r.get("result", []):
        m = u.get("message") or {}
        c = m.get("chat") or {}
        if c.get("type") == "private":
            chats[c["id"]] = (c.get("first_name") or "") + " " + (c.get("last_name") or "")
    if not chats:
        print("No private messages found. Open your bot in Telegram, send it 'hi', then run this again.")
        return
    for cid, name in chats.items():
        print(f"chat id: {cid}   ({name.strip()})")
    env = HERE / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    if len(chats) == 1 and "TELEGRAM_OWNER_CHAT_ID" not in text:
        cid = next(iter(chats))
        with env.open("a", encoding="utf-8") as f:
            f.write(("" if text.endswith("\n") or not text else "\n") + f"TELEGRAM_OWNER_CHAT_ID={cid}\n")
        print(f"saved TELEGRAM_OWNER_CHAT_ID={cid} to .env")


def capture():
    """Screenshot the live daily card. Returns the numbers shown on it."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p, tempfile.TemporaryDirectory() as prof:
        ch = os.environ.get("DAILY_CHROME_CHANNEL", "chrome") or None
        ctx = p.chromium.launch_persistent_context(prof, channel=ch, headless=True,
                                                   viewport={"width": 1080, "height": 1350},
                                                   device_scale_factor=1)
        try:
            page = ctx.new_page()
            page.goto(SITE + "daily.html", timeout=60000)
            page.wait_for_selector("body[data-ready]", timeout=60000)
            if page.get_attribute("body", "data-ready") != "1":
                raise RuntimeError("daily card could not load its data")
            page.wait_for_timeout(1500)          # fonts
            page.locator("#card").screenshot(path=str(CARD))
            return page.evaluate("""() => ({
                day: document.getElementById('day').textContent,
                today: document.getElementById('today').textContent,
                late: document.getElementById('todayNote').textContent,
                cities: [...document.querySelectorAll('#bars .bar')].map(b => [b.querySelector('.n').textContent, b.querySelector('.v').textContent])
            })""")
        finally:
            ctx.close()


def post_text(d):
    lines = [f"📊 ملخص حجوزات تخصيص {d['day']} — المرحلة 11 بيت الوطن", ""]
    if d["today"] not in ("0", "—"):
        lines.append(f"اتحجز النهارده {d['today']} قطعة 🔥")
        if d["cities"]:
            lines.append("أعلى المدن: " + " · ".join(f"{c} ({n})" for c, n in d["cities"][:3]))
    else:
        lines.append("لسه مفيش حجوزات النهارده")
    lines += ["",
              "🔍 ابحث عن قطعتك حسب المقدم بتاعك، وشوف الخرايط والحجوزات يوم بيوم:",
              SITE.rstrip("/"),
              "",
              "📢 تابع كل قطعة أول ما تتحجز على قناة التليجرام:",
              "t.me/BEITALWATANAG",
              "",
              f"📞 للاستشارة: {PHONE}",
              "",
              "#بيت_الوطن #المرحلة_11 #أراضي_بيت_الوطن"]
    return "\n".join(lines)


def send(force=False):
    scraper = load_env()
    owner = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not owner:
        print("[daily] TELEGRAM_OWNER_CHAT_ID is not set — run: python daily_post.py --whoami")
        return 1
    now = scraper.cairo_now()
    day = scraper.alloc_day(now)
    if not force:
        if now.hour < POST_HOUR or day != now.date():
            return 0                                   # not 8 PM yet, or not an allocation day
        sent = json.loads(SENT.read_text()) if SENT.exists() else {}
        if sent.get(day.isoformat()):
            return 0
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 600:
        return 0
    LOCK.write_text("1")
    try:
        d = capture()
        text = post_text(d)
        with CARD.open("rb") as f:
            r = tg("sendPhoto", chat_id=owner, caption="📸 صورة بوست النهارده — احفظها وانزل بيها مع النص اللي تحت 👇",
                   files={"photo": ("daily.png", f, "image/png")})
        r2 = tg("sendMessage", chat_id=owner, text=text, disable_web_page_preview="true")
        try:
            LOCK.unlink()
        except Exception:
            pass
        if r.ok and r2.ok and not force:
            sent = json.loads(SENT.read_text()) if SENT.exists() else {}
            sent[day.isoformat()] = True
            SENT.write_text(json.dumps(dict(sorted(sent.items())[-30:])))
        print(f"[daily] post kit for {day} sent to owner ({d['today']} bookings)")
        return 0
    except Exception as e:
        print(f"[daily] failed: {e} — will retry in 10 minutes")
        return 1                                       # lock stays -> retry after 10 min
    finally:
        pass


if __name__ == "__main__":
    if "--whoami" in sys.argv:
        load_env()
        whoami()
    else:
        sys.exit(send(force="--test" in sys.argv))

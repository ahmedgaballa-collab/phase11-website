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


# ---- extra numbers for the post (per-city totals, yesterday's ranks, down payments) ----
# Read-only queries to Supabase. If anything fails, the post still goes out,
# just without the ⚠️ / 💡 / 📍 lines.
SUPA_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im11cG1rZWN4Z25pY2ttYmR2bG5vIiwicm9sZSI6ImFub24i"
             "LCJpYXQiOjE3ODk5OTY3NTAsImV4cCI6MjEwNTU3Mjc1MH0.Y5fHBT7cRfYB-CG2v674l_FH7kYs7ZQjE5bd5bQ0AzI")


def _supa_get(path, params=None):
    url = os.environ.get("SUPABASE_URL", "https://mupmkecxgnickmbdvlno.supabase.co").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or SUPA_ANON
    r = requests.get(f"{url}/rest/v1/{path}", params=params,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=60)
    r.raise_for_status()
    return r.json()


def _supa_all(path, params):
    out, off = [], 0
    while True:
        page = _supa_get(path, dict(params, limit=1000, offset=off))
        out += page
        if len(page) < 1000:
            return out
        off += 1000


def _alloc_start_utc(day):
    """11:00 Cairo on `day`, as an ISO UTC string."""
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        t = datetime(day.year, day.month, day.day, 11, tzinfo=ZoneInfo("Africa/Cairo"))
        return t.astimezone(timezone.utc).isoformat()
    except Exception:                      # Windows without tzdata — same fallback as scraper.py
        import scraper
        noon_utc = datetime(day.year, day.month, day.day, 9, tzinfo=timezone.utc)
        offset = 3 if scraper._egypt_dst(noon_utc) else 2
        t = datetime(day.year, day.month, day.day, 11) - timedelta(hours=offset)
        return t.replace(tzinfo=timezone.utc).isoformat()


def _prev_alloc(scraper, day):
    from datetime import timedelta
    holidays = scraper.load_holidays()
    d = day - timedelta(days=1)
    for _ in range(60):
        if d.weekday() not in scraper.ALLOC_OFF_WEEKDAYS and d.isoformat() not in holidays:
            return d
        d -= timedelta(days=1)
    return d


def _city_counts_between(start_iso, end_iso):
    ch = _supa_all("plot_changes", {"select": "plot_id,created_at", "type": "eq.status",
                                    "to_value": "eq.unavailable",
                                    "and": f"(created_at.gte.{start_iso},created_at.lt.{end_iso})",
                                    "order": "created_at.desc"})
    ids = list(dict.fromkeys(c["plot_id"] for c in ch))
    counts = {}
    for i in range(0, len(ids), 40):
        lst = ",".join('"' + x.replace('"', '\\"') + '"' for x in ids[i:i + 40])
        for p in _supa_get("plots", {"select": "city", "id": f"in.({lst})"}):
            counts[p["city"]] = counts.get(p["city"], 0) + 1
    return counts


STATS_ERROR = None


def gather_stats(scraper, day):
    """Per-city numbers the post needs. Returns None if Supabase can't be read."""
    try:
        by_city = _supa_get("v_stats_by_city")
        today = {}
        for r in _supa_all("v_today_bookings", {"select": "city"}):
            today[r["city"]] = today.get(r["city"], 0) + 1
        prev = _prev_alloc(scraper, day)
        yest = _city_counts_between(_alloc_start_utc(prev), _alloc_start_utc(day))
        downs = {}
        for p in _supa_all("plots", {"select": "city,down_payment", "order": "id"}):
            if p.get("down_payment"):
                downs.setdefault(p["city"], []).append(float(p["down_payment"]))
        yrank = {c: i + 1 for i, (c, _) in enumerate(sorted(yest.items(), key=lambda x: -x[1]))}
        cities = []
        for c in by_city:
            name = c["city"]
            dl = downs.get(name) or []
            cities.append({"name": name, "today": today.get(name, 0),
                           "booked": int(c.get("booked") or 0), "total": int(c.get("total_plots") or 0),
                           "rank_yesterday": yrank.get(name, len(yrank) + 1),
                           "avg_down": sum(dl) / len(dl) if dl else None})
        return [c for c in cities if c["total"]]
    except Exception as e:
        global STATS_ERROR
        STATS_ERROR = f"{type(e).__name__}: {e}"
        print(f"[daily] extra stats unavailable ({STATS_ERROR}) — sending the short post")
        return None


def _pct(booked, total):
    return round(booked * 100 / total) if total else 0


RANK_WORD = {1: "الأول", 2: "التاني", 3: "التالت"}


def insight_lines(cities):
    """⚠️ almost sold out · 💡 climbed the ranking today · 📍 next city + whole offering."""
    lines = []
    open_c = sorted((c for c in cities if c["total"] > c["booked"]),
                    key=lambda c: c["booked"] / c["total"], reverse=True)
    alert = open_c[0] if open_c else None
    if alert:
        lines.append(f'⚠️ {alert["name"]} وصلت {_pct(alert["booked"], alert["total"])}%، '
                     f'وفاضل فيها {alert["total"] - alert["booked"]} قطعة بس، وممكن تخلص في أي يوم.')

    ranked = sorted((c for c in cities if c["today"]), key=lambda c: c["today"], reverse=True)
    risers = [(c["rank_yesterday"] - (i + 1), i + 1, c) for i, c in enumerate(ranked[:3])]
    risers = [r for r in risers if r[0] > 0]
    if risers:
        _, now_rank, c = max(risers, key=lambda r: r[0])
        line = f'💡 {c["name"]} طلعت للمركز {RANK_WORD[now_rank]} النهارده'
        avgs = sorted(x["avg_down"] for x in cities if x["avg_down"])
        if c["avg_down"] and avgs and c["avg_down"] <= avgs[len(avgs) // 3]:
            line += "، ومقدماتها من الأقل في الطرح كله"
        lines.append(line + ".")

    tb, tt = sum(c["booked"] for c in cities), sum(c["total"] for c in cities)
    total_txt = f"إجمالي الطرح وصل {_pct(tb, tt)}%: اتحجز {tb:,} قطعة، ولسه متاح {tt - tb:,}."
    if len(open_c) > 1:
        c = open_c[1]
        lines.append(f'📍 {c["name"]} فاضل فيها {c["total"] - c["booked"]} قطعة '
                     f'({_pct(c["booked"], c["total"])}%)، و{total_txt}')
    else:
        lines.append(f"📍 {total_txt}")
    return lines


def post_text(d, cities=None):
    """The Facebook post, in Ahmed's format. The links go in the first comment."""
    lines = [f"📊 ملخص حجوزات تخصيص {d['day']} — المرحلة 11 بيت الوطن"]
    if d["today"] not in ("0", "—"):
        lines.append(f"اتحجز النهارده {d['today']} قطعة 🔥")
        if d["cities"]:
            lines.append("أعلى المدن: " + " · ".join(f"{c} ({n})" for c, n in d["cities"][:3]))
    else:
        lines.append("لسه مفيش حجوزات النهارده")
    if cities:
        lines += insight_lines(cities)
    lines += [f"🔔 التقرير بينزل كل يوم تخصيص الساعة {POST_HOUR - 12 if POST_HOUR > 12 else POST_HOUR} بالليل، "
              "اعمل فولو للصفحة عشان يوصلك أول بأول.",
              f"📞 للاستشارة: {PHONE}",
              "#بيت_الوطن #المرحلة_11 #أراضي_بيت_الوطن"]
    return "\n".join(lines)


def comment_text():
    return "\n".join(["🔍 ابحث عن قطعتك حسب المقدم بتاعك، وشوف الخرايط والحجوزات يوم بيوم:",
                      SITE.rstrip("/"),
                      "",
                      "📢 تابع كل قطعة أول ما تتحجز على قناة التليجرام:",
                      "t.me/BEITALWATANAG"])


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
        text = post_text(d, gather_stats(scraper, day))
        with CARD.open("rb") as f:
            r = tg("sendPhoto", chat_id=owner, caption="📸 صورة بوست النهارده — احفظها وانزل بيها مع النص اللي تحت 👇",
                   files={"photo": ("daily.png", f, "image/png")})
        r2 = tg("sendMessage", chat_id=owner, text=text, disable_web_page_preview="true")
        if STATS_ERROR:
            tg("sendMessage", chat_id=owner, text=f"ℹ️ سطور ⚠️💡📍 مظهرتش النهارده: {STATS_ERROR[:300]}")
        tg("sendMessage", chat_id=owner, text="💬 أول كومنت 👇")
        tg("sendMessage", chat_id=owner, text=comment_text(), disable_web_page_preview="true")
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

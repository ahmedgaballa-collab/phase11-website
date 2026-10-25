"""
watchdog.py — runs on GitHub Actions every ~10 minutes (NOT on the laptop).

Checks the last successful sync in Supabase (scrape_runs). If it is older than
STALE_MIN minutes, Ahmed gets a PRIVATE Telegram message (never the channel):
  ⚠️ السحب واقف من الساعة ...
then a reminder every REMIND_MIN minutes while it stays stopped, and
  ✅ السحب رجع
when it recovers. Night messages (00:00–09:00 Cairo) arrive silently.

Only uses the public anon key (read-only). Python standard library only.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID (GitHub secrets),
     WATCHDOG_STATE (state file kept between runs by actions/cache).
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SUPABASE_URL = "https://mupmkecxgnickmbdvlno.supabase.co"
SUPABASE_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im11cG1rZWN4Z25pY2ttYmR2bG5vIiwicm9sZSI6ImFub24i"
                 "LCJpYXQiOjE3ODk5OTY3NTAsImV4cCI6MjEwNTU3Mjc1MH0.Y5fHBT7cRfYB-CG2v674l_FH7kYs7ZQjE5bd5bQ0AzI")
STALE_MIN = int(os.environ.get("STALE_MIN", "15"))
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60"))
STATE = os.environ.get("WATCHDOG_STATE", "watchdog_state.json")
DASH = "https://phase11ahmedgaballah-beta.vercel.app/dashboard"
CAIRO = timezone(timedelta(hours=3))      # Egypt summer time; good enough for display
try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    pass


def supa(path):
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}",
                                 headers={"apikey": SUPABASE_ANON, "Authorization": "Bearer " + SUPABASE_ANON})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    if "." in s:                                   # trim microseconds to 6 digits for fromisoformat
        head, rest = s.split(".", 1)
        n = 0
        while n < len(rest) and rest[n].isdigit():
            n += 1
        frac, tz = rest[:n], rest[n:]
        s = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    return datetime.fromisoformat(s)


def hm(dt):
    d = dt.astimezone(CAIRO)
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'م' if d.hour >= 12 else 'ص'}"


def dur(minutes):
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} دقيقة"
    h, m = divmod(minutes, 60)
    return f"{h} ساعة" + (f" و{m} دقيقة" if m else "")


def tell(text, now):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not token or not chat:
        print("missing TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_CHAT_ID secret — would send:\n" + text)
        return False
    quiet = now.astimezone(CAIRO).hour < 9
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true",
                                   "disable_notification": "true" if quiet else "false"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
            return r.status == 200
    except Exception as e:
        print(f"telegram failed: {e}")
        return False


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def decide(last_ok, last_any, st, now):
    """Pure logic (tested offline). Returns (message or None, new state)."""
    age = (now - last_ok).total_seconds() / 60
    st = dict(st)
    if age >= STALE_MIN:
        why = ""
        if last_any and last_any.get("status") == "fail" and parse_ts(last_any["finished_at"]) > last_ok:
            why = f"\nآخر محاولة فشلت {hm(parse_ts(last_any['finished_at']))}: {last_any.get('error_message') or ''}"[:300]
        else:
            why = "\nمفيش أي محاولات بتوصل — غالباً اللابتوب مقفول / نايم / النت فاصل."
        if not st.get("down"):
            st.update(down=True, since=last_ok.isoformat(), alerted=now.isoformat())
            return (f"⚠️ السحب واقف من الساعة {hm(last_ok)} ({dur(age)})" + why +
                    "\n\nاتأكد إن لابتوب البيت شغال وفيه نت وVeePN.\n" + DASH), st
        if (now - parse_ts(st["alerted"])).total_seconds() / 60 >= REMIND_MIN:
            st["alerted"] = now.isoformat()
            return f"⏳ لسه السحب واقف — بقاله {dur(age)} (من {hm(last_ok)})" + why, st
        return None, st
    if st.get("down"):
        since = parse_ts(st["since"])
        gap = (last_ok - since).total_seconds() / 60
        return f"✅ السحب رجع يشتغل — آخر تحديث {hm(last_ok)}\nكان واقف حوالي {dur(gap)}.", {}
    return None, {}


def main():
    now = datetime.now(timezone.utc)
    try:
        ok = supa("scrape_runs?select=finished_at&status=neq.fail&order=id.desc&limit=1")
        anyrun = supa("scrape_runs?select=finished_at,status,error_message&order=id.desc&limit=1")
    except Exception as e:
        print(f"could not reach Supabase: {e}")          # GitHub/Supabase hiccup — try again next run
        return 0
    if not ok:
        print("no runs in scrape_runs")
        return 0
    last_ok = parse_ts(ok[0]["finished_at"])
    old = load_state()
    msg, st = decide(last_ok, anyrun[0] if anyrun else None, old, now)
    print(f"last ok sync: {last_ok.isoformat()}  ({(now - last_ok).total_seconds() / 60:.1f} min ago)  down={bool(st.get('down'))}")
    if msg:
        sent = tell(msg, now)
        print(("sent:\n" if sent else "NOT sent:\n") + msg)
        if not sent:
            st = old                                     # retry the same message next run
    save_state(st)
    if "--test" in sys.argv:
        tell(f"🧪 تجربة المراقب — آخر تحديث للسحب {hm(last_ok)} (من {dur((now - last_ok).total_seconds() / 60)})", now)
    return 0


if __name__ == "__main__":
    sys.exit(main())

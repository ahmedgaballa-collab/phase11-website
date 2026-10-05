"""
nuca_news.py — NUCA announcements ("اخر الأخبار" on Home.aspx) + the daily
top-cities summary for the Telegram channel.

- Every news item on the home page links to NewsItemViewer.aspx?NID=<n>.
  A NID we have not seen before = a new announcement -> saved to Supabase
  (table nuca_news) and posted to the channel.
- First run on a machine only records what is already there (no posts).
- Daily summary: top cities of the allocation day, sent when NUCA announces
  that registration closed ("تم الانتهاء من التسجيل"), or at 23:00 Cairo as
  a fallback — once per allocation day.
"""
import json
import os
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup

HERE = Path(__file__).parent
SEEN_FILE = HERE / "news_seen.json"
CITY_COUNTS = HERE / "daily_cities.json"
SUMMARY_SENT = HERE / "summary_sent.json"
CHECK_EVERY = float(os.environ.get("NEWS_CHECK_SEC", "60"))
MAX_POSTS_PER_CHECK = 3
_last = {"t": 0.0}

DATE_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")
NID_RE = re.compile(r"NewsItemViewer\.aspx\?NID=(\d+)", re.I)


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_home(html, base="https://lands.nuca.gov.eg/ar/"):
    """[{nid, title, date, summary, url}] from the home page, newest first."""
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    anchors = soup.find_all("a", href=NID_RE)
    for a in anchors:                      # pass 1: titles (the longest link text per NID)
        nid = int(NID_RE.search(a["href"]).group(1))
        title = _norm(a.get_text(" "))
        it = items.setdefault(nid, {"nid": nid, "title": "", "date": "", "summary": "",
                                    "url": base + "NewsItemViewer.aspx?NID=%d" % nid})
        if len(title) > len(it["title"]):
            it["title"] = title
    for a in anchors:                      # pass 2: date + summary from the item's own box
        it = items[int(NID_RE.search(a["href"]).group(1))]
        box = a
        for _ in range(6):
            box = box.parent
            if box is None:
                break
            txt = _norm(box.get_text(" "))
            if DATE_RE.search(txt) and len(txt) < 2500:
                if len(set(NID_RE.findall(str(box)))) == 1:
                    m = DATE_RE.search(txt)
                    it["date"] = it["date"] or m.group(0)
                    rest = txt.replace(it["title"], "", 1).replace(m.group(0), "", 1) if it["title"] else txt
                    rest = _norm(rest.replace("...", "").replace("…", ""))
                    if len(rest) > len(it["summary"]):
                        it["summary"] = rest
                break
    return [items[k] for k in sorted(items, reverse=True) if items[k]["title"]]


def full_text(html, item):
    """Best effort: the news page's paragraph that starts like the summary."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    probe = _norm(item.get("summary") or item["title"])[:30]
    best = ""
    if probe:
        for el in soup.find_all(["div", "p", "span", "td"]):
            txt = _norm(el.get_text(" "))
            if probe in txt and len(txt) < 3000 and (not best or len(txt) < len(best)) and len(txt) >= len(probe):
                best = txt
    return best or item.get("summary") or item["title"]


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------- daily city counts ----------------
def add_bookings(alloc_day, cities):
    """Count newly booked plots per city for the allocation day."""
    if not cities:
        return
    data = _load(CITY_COUNTS, {})
    day = data.setdefault(alloc_day, {})
    for c in cities:
        day[c] = day.get(c, 0) + 1
    for k in sorted(data)[:-14]:          # keep two weeks
        data.pop(k, None)
    _save(CITY_COUNTS, data)


def summary_message(alloc_day, label, counts, site_url, dash_url):
    import html as _h
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = [f"📊 <b>ملخص حجوزات تخصيص {_h.escape(label)}</b>", "",
             f"إجمالي القطع اللي اتحجزت: <b>{total}</b>", "", "<b>أعلى المدن حجزاً:</b>"]
    for (city, n), m in zip(top, medals):
        lines.append(f"{m} {_h.escape(city)} — <b>{n}</b>")
    lines += ["", f'📊 <a href="{dash_url}">التفاصيل في متابعة الحجوزات</a>   ·   🔍 <a href="{site_url}">القطع المتاحة</a>']
    return "\n".join(lines)


def notice_message(item, dash_url):
    import html as _h
    body = item.get("body") or item.get("summary") or ""
    text = body if len(body) > len(item["title"]) else item["title"]
    lines = ["📢 <b>تنويه من هيئة المجتمعات العمرانية</b>", "", _h.escape(text)]
    if item.get("date"):
        lines += ["", f"🗓 {_h.escape(item['date'])}"]
    lines += ["", f'📊 <a href="{dash_url}">متابعة الحجوزات</a>']
    return "\n".join(lines)


def maybe_send_summary(scraper, telegram_notify, reason, log=print, alloc_day=None):
    """Once per allocation day."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    day, _late, label = scraper.alloc_info()
    day = alloc_day or day
    sent = _load(SUMMARY_SENT, {})
    if sent.get(day):
        return
    counts = _load(CITY_COUNTS, {}).get(day, {})
    if not counts:
        return
    if alloc_day:
        from datetime import date
        d = date.fromisoformat(day)
        label = f"{scraper.AR_WEEKDAYS[d.weekday()]} {d.day}/{d.month}"
    msg = summary_message(day, label, counts, telegram_notify.SITE_URL, telegram_notify.DASHBOARD_URL)
    if telegram_notify.send(token, chat, msg):
        sent[day] = reason
        _save(SUMMARY_SENT, dict(sorted(sent.items())[-30:]))
        log(f"[summary] daily summary for {day} posted ({reason})")


# ---------------- the check, called every pass ----------------
def check(sess, scraper, log=print, force=False):
    if not force and time.time() - _last["t"] < CHECK_EVERY:
        return
    _last["t"] = time.time()
    import telegram_notify
    import supabase_sync
    try:
        items = parse_home(sess.get("/ar/Home.aspx"))
    except Exception as e:
        log(f"[news] could not read the home page: {e}")
        items = []
    if not items:
        try:
            dbg = HERE / "debug"; dbg.mkdir(exist_ok=True)
            (dbg / "home.html").write_text(sess.get("/ar/Home.aspx"), encoding="utf-8")
        except Exception:
            pass
        log("[news] no announcements found on the home page (saved debug/home.html)")
        return
    seen = _load(SEEN_FILE, None)
    first = seen is None
    seen = set(seen or [])
    new = [it for it in items if it["nid"] not in seen]
    if new:
        for it in new:
            try:
                it["body"] = full_text(sess.get(f"/ar/NewsItemViewer.aspx?NID={it['nid']}"), it)
            except Exception:
                it["body"] = it.get("summary", "")
        try:
            supabase_sync.upsert_news(new, log=log)
        except Exception as e:
            log(f"[news] supabase: {e}")
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if first:
            log(f"[news] first check — recorded {len(new)} existing announcement(s), nothing posted")
        elif len(new) > MAX_POSTS_PER_CHECK:
            log(f"[news] {len(new)} 'new' announcements at once — looks like a page change; recorded, not posted")
        elif token and chat:
            for it in sorted(new, key=lambda x: x["nid"]):
                ok = telegram_notify.send(token, chat, notice_message(it, telegram_notify.DASHBOARD_URL))
                log(f"[news] posted announcement {it['nid']}: {it['title'][:60]}" if ok else f"[news] telegram failed for {it['nid']}")
                time.sleep(1)
        seen |= {it["nid"] for it in new}
        _save(SEEN_FILE, sorted(seen))
        if not first:
            closing = [it for it in new if "الانتهاء من التسجيل" in (it["title"] + it.get("body", ""))]
            for it in closing:
                txt = it["title"] + " " + it.get("body", "")
                now = scraper.cairo_now()
                if "امس" in txt or "أمس" in txt:
                    # "closed yesterday" — the day before today's allocation, if still unsent
                    prev = scraper.alloc_day(now.replace(hour=10, minute=0) if now.hour >= 11 else now)
                    maybe_send_summary(scraper, telegram_notify, "closing news", log, alloc_day=prev.isoformat())
                else:
                    maybe_send_summary(scraper, telegram_notify, "closing news", log)
    # fallback: 23:00 Cairo on an allocation day
    now = scraper.cairo_now()
    if now.hour >= 23 and scraper.alloc_day(now).isoformat() == now.date().isoformat():
        maybe_send_summary(scraper, telegram_notify, "23:00", log)

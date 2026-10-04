"""
scraper.py — Phase 11 (Bayt Al Watan) live status sync

What this does
---------------
1. DISCOVER: walks the public NUCA lands site (lands.nuca.gov.eg) — no login
   needed — starting from the 25 known cities, finds every project whose
   title contains "المرحلة الحادية عشر" (Phase 11), then finds every
   "Zone" page (ViewZone.aspx) under each of those projects.
2. HARVEST: for every Zone page, pages through the plots grid (an ASP.NET
   WebForms GridView that uses __doPostBack for pagination — handled here
   with the same raw POST field names ASP.NET itself uses, but sent as an
   in-page fetch() through a real Chrome browser — see the 2026-09-26 note
   above TIMEOUT for why a plain HTTP client no longer works here) and reads
   the "طلب الحجز" (booking) column for every plot.
3. OUTPUT: writes status.json in the exact shape the website's
   loadReservedStatus() expects:
       {"reserved": ["<city>|<project>|<area>|<block>|<plot>", ...],
        "updatedAt": "YYYY-MM-DD HH:MM"}
   Only RESERVED plots are listed (sparse file, fast to diff, fast to load).
4. DIFF: compares against the previous status.json (if present) and returns
   the list of plots that just flipped from available -> reserved, so the
   Telegram notifier can announce only what's new.

IMPORTANT — read before running unattended
--------------------------------------------
- This site is public but NOT an official API. Project-title matching
  ("المرحلة الحادية عشر") and the table's column order are inferred from
  what the site showed on 2026-09-22. If NUCA changes their page layout,
  this script's parsing will need updating — it is written defensively
  (skips + logs anything it can't parse rather than crashing or silently
  producing wrong data), but it is not immune to real site changes.
- Run with --test first (crawls ONE city only) and manually compare a
  handful of plots against the live site before trusting this in
  production / wiring it to Telegram.
- Be a good citizen: this now fetches multiple zones CONCURRENTLY (see
  DEFAULT_WORKERS) to keep a full scan fast enough to run every few minutes.
  That is already a meaningfully heavier load on a government server than a
  single visitor browsing casually. DEFAULT_DELAY and DEFAULT_WORKERS exist
  on purpose — do not push both up at once. If you start seeing timeouts,
  connection errors, or HTTP 429s in the logs, that is the site telling you
  to back off: lower DEFAULT_WORKERS and/or lengthen the schedule interval
  immediately, don't just retry harder.
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import supabase_sync


def load_dotenv(path=None):
    """Reads KEY=VALUE lines from .env next to this script (never committed —
    see .gitignore). Existing environment variables win."""
    p = Path(path) if path else Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Load .env BEFORE the constants below read os.environ (CHROME_PROFILE_DIR etc.)
load_dotenv()

BASE = "https://lands.nuca.gov.eg"
PHASE_MARKER = "المرحلة الحادية عشر"
# The site's REAL signal for "is this project part of the currently open
# phase" is a green/red ball icon next to each project on the city page —
# NOT the project's title text (confirmed 2026-09-23: many current-phase
# projects don't literally say "المرحلة الحادية عشر" in their title).
#   ball_green.png -> project is open in the current phase (what we want)
#   ball_red.png   -> project belonged to a previous phase (skip)
BALL_GREEN_MARKER = "ball_green"
BALL_RED_MARKER = "ball_red"
DEFAULT_DELAY = 1.5   # seconds between requests WITHIN one worker thread
DEFAULT_WORKERS = 1   # zones harvested in parallel. Was 6, then 3 (2026-09-22),
                      # then 2 with a 0.5s delay — but the full run on
                      # 2026-09-23 (the one that added the second,
                      # booked-filter pass per zone) still timed out on
                      # nearly every zone even at 2 workers, crawling at
                      # roughly 1 zone per 4+ minutes. Backing off further:
                      # 1 worker, 1.5s delay. Raise only after several clean
                      # runs with no "Read timed out" errors in the logs.
DAILY_SPAM_GUARD = 400  # NUCA now releases up to 300 plots/day; more "new" reservations than this in one 5-minute run
                        # is almost certainly a key/matching bug, not real
                        # bookings — don't let it inflate the daily counter
TIMEOUT = 50  # confirmed 2026-09-23: even 150s does not help (100% "Read timed
                # out" on the booked-filter pass from GitHub Actions specifically,
                # while the same request succeeds reliably from a regular browser) —
                # this looks like a network-level block/throttle on GitHub-hosted
                # runner IPs hitting lands.nuca.gov.eg, not marginal slowness. Do NOT
                # just keep raising this value; it will not fix the failure rate.

# 2026-09-26: confirmed the block is NOT specific to GitHub Actions — it also
# hit Ahmed's own home connection (plain ConnectTimeout with no VPN at all)
# AND a Python client with a full Chrome TLS/HTTP fingerprint (curl_cffi,
# impersonate=chrome124/120/110 — still "Read timed out" every time) AND an
# unrelated real Chromium browser with no VPN extension loaded. The ONE
# combination that reliably reaches the site is Ahmed's own everyday Chrome
# with the VeePN extension active. So this version drives an actual Chrome
# browser (via Playwright, reusing a copy of that Chrome profile so the
# VeePN extension + its login come along) instead of a raw HTTP client —
# every request below goes through page.evaluate()'s fetch(), i.e. through
# Chrome's own network stack, not Python's.
CHROME_PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", "")
CHROME_HEADLESS = os.environ.get("CHROME_HEADLESS", "0") == "1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}

# The 25 cities that list Phase 11 plots, with their NUCA city IDs.
# (Confirmed 2026-09-22 from lands.nuca.gov.eg/ar/Home.aspx — stable,
# NUCA rarely adds new cities, but re-check this list if a city goes missing.)
CITIES = {
    "القاهرة الجديدة": 1,
    "السادس من أكتوبر": 9,
    "الشيخ زايد": 5,
    "بدر": 2,
    "المنيا الجديدة": 7,
    "أسيوط الجديدة": 11,
    "السادات": 14,
    "الشروق": 15,
    "أسوان الجديدة": 8,
    "أكتوبر الجديدة": 24,
    "العبور الجديدة": 16,
    "العبور": 13,
    "سوهاج الجديدة": 19,
    "بني سويف الجديدة": 1026,
    "المنصورة الجديدة": 20,
    "العاشر من رمضان": 21,
    "العلمين الجديدة": 1024,
    "برج العرب الجديدة": 1025,
    "سفنكس الجديدة": 1027,
    "15 مايو": 22,
    "حدائق العاصمة": 23,
    "دمياط الجديدة": 6,
    "أخميم الجديدة": 2026,
    "الفيوم الجديدة": 2027,
    "حدائق العاشر": 2028,
}

# NOTE: we used to flag any طلب الحجز cell containing "حجز" (e.g. "حجز مبدئى")
# as reserved. That was WRONG: NUCA's own site still lists those plots under
# its "عرض القطع المتاحة فقط" (available) filter — a preliminary request is
# not a finalized allocation. The only authoritative signal is NUCA's own
# "عرض القطع المحجوزة فقط" filter (PlotType=rdShowBooked, see harvest_zone
# below), whose طلب الحجز cells read "غير متاحة". We query that filter
# directly instead of guessing from cell text.


def norm(s):
    """Collapse whitespace the same way the website's JS normKey() does."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", " ", s).strip()


def _egypt_dst(utc_dt):
    """Egypt summer time (since 2023): last Friday of April 00:00 -> last
    Thursday of October 24:00, local time. Used only if tzdata is missing."""
    y = utc_dt.year
    def last(month, weekday):  # weekday: Mon=0 .. Sun=6
        d = datetime(y, month + 1, 1) - timedelta(days=1)
        return d - timedelta(days=(d.weekday() - weekday) % 7)
    start = last(4, 4) - timedelta(hours=2)                     # 00:00 +02 in UTC
    end = last(10, 3) + timedelta(days=1) - timedelta(hours=3)  # 24:00 +03 in UTC
    naive = utc_dt.replace(tzinfo=None)
    return start <= naive < end


def cairo_now():
    """Current Cairo wall-clock time (naive datetime)."""
    utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo("Africa/Cairo")).replace(tzinfo=None)
    except Exception:
        return (utc + timedelta(hours=3 if _egypt_dst(utc) else 2)).replace(tzinfo=None)


# ---- allocation day ----------------------------------------------------
# NUCA releases the day's plots at 11:00 Cairo, Sunday–Thursday, except
# official holidays. A booking made after midnight (or on a Friday/Saturday/
# holiday) still belongs to the last allocation day.
ALLOC_START_HOUR = 11
ALLOC_OFF_WEEKDAYS = (4, 5)          # Friday, Saturday
HOLIDAYS_FALLBACK = {"2026-10-06"}   # used if Supabase can't be reached
HOLIDAYS_CACHE = "holidays_cache.json"
AR_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def load_holidays(max_age_hours=6):
    """Official holidays from Supabase table no_allocation_days, cached."""
    p = Path(HOLIDAYS_CACHE)
    try:
        if p.exists() and time.time() - p.stat().st_mtime < max_age_hours * 3600:
            return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        try:
            import requests
            r = requests.get(url.rstrip("/") + "/rest/v1/no_allocation_days",
                             params={"select": "day"},
                             headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=20)
            if r.ok:
                days = sorted({row["day"] for row in r.json()} | HOLIDAYS_FALLBACK)
                p.write_text(json.dumps(days), encoding="utf-8")
                return set(days)
        except Exception:
            pass
    try:
        if p.exists():
            return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set(HOLIDAYS_FALLBACK)


def alloc_day(now=None, holidays=None):
    """The allocation day (date) a booking made at Cairo time `now` counts for."""
    now = now or cairo_now()
    holidays = load_holidays() if holidays is None else holidays
    d = (now - timedelta(hours=ALLOC_START_HOUR)).date()
    for _ in range(60):
        if d.weekday() not in ALLOC_OFF_WEEKDAYS and d.isoformat() not in holidays:
            return d
        d -= timedelta(days=1)
    return d


def alloc_info(now=None, holidays=None):
    """(allocation date 'YYYY-MM-DD', is_late, label like 'الخميس 1/10')."""
    now = now or cairo_now()
    d = alloc_day(now, holidays)
    late = now.date() != d
    return d.isoformat(), late, f"{AR_WEEKDAYS[d.weekday()]} {d.day}/{d.month}"


def truthy_feature(cell):
    """The corner/garden/view columns hold a per-m² surcharge (e.g. '11'),
    not a literal true/false — any non-zero value means the feature applies."""
    c = (cell or "").strip().replace(",", "")
    return c not in ("", "0", "0.0", "0.00")


def strip_phase_wrapper(project_title, city_name):
    """
    NUCA shows project titles like:
      "المرحلة الحادية عشر - منطقة 1360 فدان بالامتداد - المنيا الجديدة"
    Ahmed's original dataset just has:
      "منطقة 1360 فدان بالامتداد"
    Strip the phase prefix and the trailing " - <city>" suffix.
    """
    t = norm(project_title)
    t = re.sub(rf"^{re.escape(PHASE_MARKER)}\s*-\s*", "", t)
    t = re.sub(rf"\s*-\s*{re.escape(city_name)}\s*$", "", t)
    return norm(t)


_FETCH_JS = """
async ({url, method, body, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const opts = {method, credentials: 'same-origin', signal: controller.signal};
        if (body !== null) {
            opts.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
            opts.body = body;
        }
        const res = await fetch(url, opts);
        const text = await res.text();
        return {ok: true, status: res.status, text: text};
    } catch (e) {
        return {ok: false, error: String(e && e.message ? e.message : e)};
    } finally {
        clearTimeout(timer);
    }
}
"""


class Session:
    """Drives a real Chrome browser (Ahmed's own profile + VeePN extension,
    see the 2026-09-26 note above TIMEOUT) and issues every GET/POST as an
    in-page fetch() — so it goes through Chrome's actual network stack, not
    a separate Python HTTP client. One real top-level navigation happens
    first (so any page-load-only checks the site does get to run and any
    cookie they set is in place) and every request after that is a
    same-origin fetch() from that page."""

    def __init__(self, delay=DEFAULT_DELAY, profile_dir=None):
        self.delay = delay
        profile_dir = profile_dir or CHROME_PROFILE_DIR
        if not profile_dir:
            raise RuntimeError(
                "CHROME_PROFILE_DIR is not set — Session needs a Chrome profile "
                "directory (a copy of Ahmed's real profile, so the VeePN "
                "extension is present and already logged in) to launch."
            )
        self._pw = sync_playwright().start()
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                profile_dir,
                channel="chrome",
                headless=CHROME_HEADLESS,
                # Off-screen by default so the window that opens every few
                # minutes can't be closed by accident (closing it kills the run).
                # Set CHROME_OFFSCREEN=0 in .env to see it again.
                args=["--lang=ar-EG"] + (["--window-position=-32000,-32000"]
                                         if os.environ.get("CHROME_OFFSCREEN", "1") == "1" else []),
                # Playwright passes --disable-extensions by default, which would
                # silently drop VeePN — keep extensions enabled.
                ignore_default_args=["--disable-extensions"],
            )
        except Exception:
            self._pw.stop()
            raise
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        # Real navigation, not fetch() — lets the site's own page-load checks
        # (and the VeePN-routed connection itself) run once, up front.
        # VeePN needs a few seconds after Chrome starts before it routes traffic.
        time.sleep(float(os.environ.get("VPN_WARMUP_SECONDS", "8")))
        self._open_home()

    def _open_home(self):
        last = None
        for attempt in range(3):
            try:
                self.page.goto(BASE + "/ar/Home.aspx", timeout=TIMEOUT * 1000)
                print(f"[session] opened {self.page.url}", flush=True)
                if "lands.nuca.gov.eg" in self.page.url:
                    return
                last = f"landed on {self.page.url}"
            except Exception as e:
                last = str(e).splitlines()[0]
            print(f"[session] home page not reachable yet ({last}) — retrying", flush=True)
            time.sleep(10)
        raise RuntimeError(
            f"Could not open lands.nuca.gov.eg ({last}). Is VeePN connected in "
            f"the Chrome window the script opened?")

    def _request(self, method, path, data=None):
        time.sleep(self.delay)
        body = urlencode(data) if data is not None else None
        # Relative URL: always same-origin as the page actually loaded (the
        # site may redirect http/https or www), so fetch() never hits CORS.
        arg = {"url": path, "method": method, "body": body, "timeoutMs": TIMEOUT * 1000}
        result = self.page.evaluate(_FETCH_JS, arg)
        if not result["ok"]:
            # One retry, with a real pause first — mirrors the old requests-based
            # retry: a slow/busy server benefits from backing off, not hammering.
            time.sleep(3.0)
            result = self.page.evaluate(_FETCH_JS, arg)
        if not result["ok"] and method == "GET":
            # Fallback: a real navigation instead of fetch().
            try:
                self.page.goto(BASE + path, timeout=TIMEOUT * 1000)
                result = {"ok": True, "status": 200, "text": self.page.content()}
            except Exception as e:
                result = {"ok": False, "error": f"{result['error']} / goto: {str(e).splitlines()[0]}"}
        if not result["ok"]:
            raise RuntimeError(f"fetch failed for {path}: {result['error']} "
                               f"(page is at {self.page.url})")
        if result["status"] >= 400:
            raise RuntimeError(f"HTTP {result['status']} for {path}")
        return result["text"]

    def request_many(self, reqs):
        """Several GET/POSTs at once (in-page Promise.all). reqs: list of
        (method, path, data). Returns a list of html strings / Exceptions in
        the same order. Anything that failed is retried one by one through
        _request (which has its own retry + fallback)."""
        if not reqs:
            return []
        time.sleep(self.delay)
        args = [{"url": path, "method": m, "body": urlencode(d) if d is not None else None,
                 "timeoutMs": TIMEOUT * 1000} for m, path, d in reqs]
        try:
            results = self.page.evaluate(_FETCH_MANY_JS, args)
        except Exception as e:
            results = [{"ok": False, "error": str(e)}] * len(reqs)
        out = []
        for (m, path, d), r in zip(reqs, results):
            if r.get("ok") and r.get("status", 500) < 400:
                out.append(r["text"])
                continue
            try:
                out.append(self._request(m, path, d))
            except Exception as e:
                out.append(e)
        return out

    def request_many_nc(self, reqs):
        """Parallel, cookieless. Returns html strings or None (failed) — no
        retries here; callers fall back to the normal cookie path."""
        if not reqs:
            return []
        args = [{"url": path, "method": m, "body": urlencode(d) if d is not None else None,
                 "timeoutMs": TIMEOUT * 1000} for m, path, d in reqs]
        try:
            results = self.page.evaluate(_FETCH_MANY_NC_JS, args)
        except Exception:
            return [None] * len(reqs)
        return [r["text"] if r.get("ok") and r.get("status", 500) < 400 else None for r in results]

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, data):
        return self._request("POST", path, data=data)

    def close(self):
        try:
            self.context.close()
        finally:
            self._pw.stop()


_FETCH_MANY_JS = "async (reqs) => { const one = " + _FETCH_JS.strip() + "; return await Promise.all(reqs.map(r => one(r))); }"

# Same, but WITHOUT the session cookie: measured 2026-10-05 on the live site,
# 8 zones' booked lists came back identical in 1.7s this way vs 16.9s one by
# one — the server handles cookieless requests in parallel instead of
# queueing them behind one session.
_FETCH_MANY_NC_JS = _FETCH_MANY_JS.replace("credentials: 'same-origin'", "credentials: 'omit'")


def find_project_container(a):
    """
    Walk up from the ViewProject link until we hit the block that also
    holds its icon/ball image — that's the container for this one project
    entry (the link's own text is just 'التفاصيل').
    """
    node = a
    for _ in range(4):
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", None):
            return None
        if node.find("img") is not None:
            return node
    return None


def ball_color(container):
    for img in container.find_all("img"):
        src = (img.get("src") or "").lower()
        if BALL_GREEN_MARKER in src:
            return "green"
        if BALL_RED_MARKER in src:
            return "red"
    return None


def clean_project_title(container_text, city_name):
    t = norm(container_text)
    t = re.sub(r"التفاصيل\s*$", "", t).strip()
    # the site frequently repeats the same title twice in one container
    # (once as a caption, once again right before the details link)
    half = len(t) // 2
    first, second = t[:half].strip(), t[half:].strip()
    if first and first == second:
        t = first
    # if a phase-11 labelled span is present, prefer that clean slice
    m = re.search(rf"{re.escape(PHASE_MARKER)}.*?{re.escape(city_name)}", t)
    if m:
        t = norm(m.group(0))
    return strip_phase_wrapper(t, city_name)


GENERIC_LINK_TEXT = {"", "التفاصيل", "تفاصيل", "عرض", "عرض القطع", "details", "view"}


def zone_link_name(a, city_name):
    """Human name of a zone from its link on the project page (e.g.
    'الحى الرابع - مجاورة 3'). Falls back to the link's title attribute or the
    text of its surrounding block when the link itself just says 'التفاصيل'."""
    cands = [norm(a.get_text()), norm(a.get("title", ""))]
    node = a
    for _ in range(3):
        node = node.parent
        if node is None:
            break
        cands.append(norm(node.get_text()))
    for c in cands:
        c = re.sub(r"(التفاصيل|تفاصيل)\s*$", "", c).strip(" -–")
        c = strip_phase_wrapper(c, city_name)
        if c and c not in GENERIC_LINK_TEXT and len(c) <= 120:
            return c
    return ""


def page_zone_name(soup):
    """Zone title printed on ViewZone.aspx itself, if the page has one."""
    for el in soup.find_all(id=re.compile(r"(lbl|lit|h)\w*Zone\w*Name|lblZone|lblTitle", re.I)):
        t = norm(el.get_text())
        if t and len(t) <= 120:
            return t
    return ""


def discover_zones(sess, cities=None, log=print):
    """
    Returns a list of dicts: {city, project, zone_id}
    covering every currently-open-phase project's zone pages, found by
    reading the green/red ball icon next to each project on the city page.
    """
    zones = []
    target_cities = {k: v for k, v in CITIES.items() if not cities or k in cities}
    first_city_debug_done = False

    for city_name, city_id in target_cities.items():
        log(f"[discover] city: {city_name}")
        html = sess.get(f"/ar/ViewCity.aspx?ID={city_id}")
        soup = BeautifulSoup(html, "html.parser")

        proj_anchors = soup.find_all("a", href=re.compile(r"ViewProject\.aspx\?ID=\d+"))
        if not first_city_debug_done:
            log(f"  [debug] {city_name}: {len(proj_anchors)} ViewProject link(s) found on page")

        project_links = []
        for i, a in enumerate(proj_anchors):
            want_debug = (not first_city_debug_done) and i < 6
            container = find_project_container(a)
            container_text = norm(container.get_text()) if container is not None else norm(a.get_text())
            color = ball_color(container) if container is not None else None
            if want_debug:
                log(f"    [debug] link #{i}: ball={color!r}, "
                    f"text='{container_text[:80]}'")

            # Primary, proven signal: the project's own text names the current
            # phase. Ball color (when we manage to detect it at all) is only
            # used to EXCLUDE on a confirmed red — never required for inclusion,
            # since how the site renders the ball isn't reliably detectable as
            # a plain <img src>.
            if color == "red":
                continue
            if PHASE_MARKER not in container_text:
                continue

            title = clean_project_title(container_text, city_name)
            m = re.search(r"ID=(\d+)", a["href"])
            if m:
                project_links.append((int(m.group(1)), title))
        first_city_debug_done = True

        # de-dup (the project can appear twice: icon + text link)
        seen_proj = {}
        for pid, title in project_links:
            seen_proj[pid] = title

        for project_id, project_title in seen_proj.items():
            log(f"  [discover] project {project_id}: {project_title}")
            phtml = sess.get(f"/ar/ViewProject.aspx?ID={project_id}")
            psoup = BeautifulSoup(phtml, "html.parser")

            zone_names = {}
            for a in psoup.find_all("a", href=re.compile(r"ViewZone\.aspx\?ID=\d+")):
                m = re.search(r"ID=(\d+)", a["href"])
                if m:
                    zid = int(m.group(1))
                    name = zone_link_name(a, city_name)
                    # keep the most descriptive text seen for this zone
                    if len(name) > len(zone_names.get(zid, "")):
                        zone_names[zid] = name
                    else:
                        zone_names.setdefault(zid, "")

            project_bare = strip_phase_wrapper(project_title, city_name)
            for zid, zname in zone_names.items():
                zones.append({
                    "city": city_name,
                    "project": project_bare,
                    "zone_id": zid,
                    "zone_name": zname,
                })
                log(f"    [zone] {zid}: {zname or '(no name on project page)'}")

    return zones


def parse_hidden_fields(soup):
    """Everything a real browser would post back for this form: all hidden
    inputs (incl. __VIEWSTATEENCRYPTED / __LASTFOCUS when present), text
    boxes, and the checked radio/checkbox values. Posting only the 3 core
    ViewState fields made ASP.NET reject the pager postback (page 2+ came
    back with no rows — seen 2026-09-28)."""
    fields = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ in ("submit", "button", "image", "reset", "file"):
            continue
        if typ in ("radio", "checkbox") and not inp.has_attr("checked"):
            continue
        fields[name] = inp.get("value", "on" if typ in ("radio", "checkbox") else "")
    for sel in soup.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        fields[name] = opt.get("value", opt.get_text()) if opt else ""
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        fields.setdefault(name, "")
    fields.setdefault("__LASTFOCUS", "")
    return fields


def parse_plots_table(soup):
    """
    Returns list of row dicts from the grdPlots table on the current page,
    or [] if the table isn't found (e.g. empty zone).
    Expected columns (as seen on the live site):
      المربع | القطعة | المساحة | سعر المتر الأساسى | تميز ناصية |
      تميز حدائق | تميز بحر أو نيل | سعر المتر الإجمالي | السعر الإجمالي |
      الدفعة المقدمة | طلب الحجز
    """
    table = soup.find(id=re.compile(r"grdPlots$"))
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:  # skip header row
        # The GridView pager is a row whose single cell holds a NESTED table
        # of page links (1 2 3 ... 10). A recursive td search used to count
        # those links as 10+ cells and turn the pager into a fake plot
        # (block "12345678910") on every page — seen 2026-09-28.
        if tr.find("table") is not None:
            continue
        if tr.find_parent("table") is not table:
            continue  # a row that belongs to some nested table
        cells = [norm(td.get_text()) for td in tr.find_all("td", recursive=False)]
        if len(cells) < 10:
            continue  # pager row or malformed row — skip, don't guess
        block, plot, area = cells[0], cells[1], cells[2]
        corner = truthy_feature(cells[4]) if len(cells) > 4 else False
        garden = truthy_feature(cells[5]) if len(cells) > 5 else False
        view = truthy_feature(cells[6]) if len(cells) > 6 else False
        down = cells[9] if len(cells) > 9 else ""
        rows.append({
            "block": block, "plot": plot, "area": area,
            "corner": corner, "garden": garden, "view": view,
            "down": down,
        })
    return rows


def max_page_number(soup):
    pages = set()
    for a in soup.find_all("a", href=re.compile(r"__doPostBack")):
        m = re.search(r"grdPlots','Page\$(\d+)'", a.get("href", ""))
        if m:
            pages.add(int(m.group(1)))
    return max(pages) if pages else 1


def _paginate_zone(sess, path, soup, zone_id, log=print, plot_type=None):
    """Yields row dicts from `soup` (a page already fetched for this zone),
    then follows the grdPlots pager for any further pages. When `plot_type`
    is set, it is resent on every page postback so the server keeps applying
    the same PlotType filter (available/booked) across pages."""
    rows = parse_plots_table(soup)
    for r in rows:
        yield r

    total_pages = max_page_number(soup)
    if total_pages <= 1:
        return

    hidden = parse_hidden_fields(soup)
    page_num = 1
    while page_num < total_pages and page_num < 500:
        page_num += 1
        form = dict(hidden)
        form["__EVENTTARGET"] = "ctl00$MainContent$grdPlots"
        form["__EVENTARGUMENT"] = f"Page${page_num}"
        if plot_type:
            form["ctl00$MainContent$PlotType"] = plot_type
        html = sess.post(path, form)
        soup = BeautifulSoup(html, "html.parser")
        rows = parse_plots_table(soup)
        if not rows:
            log(f"    [warn] zone {zone_id} page {page_num}: no rows parsed, stopping")
            try:
                dbg = Path(__file__).with_name("debug")
                dbg.mkdir(exist_ok=True)
                (dbg / f"zone{zone_id}_p{page_num}_{plot_type or 'available'}.html").write_text(
                    html, encoding="utf-8")
            except Exception:
                pass
            break
        for r in rows:
            yield r
        hidden = parse_hidden_fields(soup)  # refresh viewstate for the next postback
        # The pager only lists ~10 page numbers at a time (then "..."), so
        # re-read the highest page number from every page we land on.
        total_pages = max(total_pages, max_page_number(soup))


def harvest_zone(sess, zone_id, log=print, booked_only=False):
    """Yields dicts {block, plot, ..., reserved} for every plot in this zone.

    Two passes against ViewZone.aspx's own PlotType filter — NUCA's own
    authoritative signal, not a guess from cell text:
      1. "عرض القطع المتاحة فقط" (rdShowAvailable, the default) — every plot
         listed here is NOT finalized yet, even ones showing a "حجز مبدئى"
         (preliminary request) in the طلب الحجز column.
      2. "عرض القطع المحجوزة فقط" (rdShowBooked) — every plot listed here IS
         finalized (طلب الحجز reads "غير متاحة"). Confirmed live against the
         real site: a finalized plot never shows up under the available
         filter at all, so the two passes never overlap and never
         double-count plot_total.
    """
    path = f"/ar/ViewZone.aspx?ID={zone_id}"

    # Pass 1: available (default) filter — nothing here is finalized yet.
    html = sess.get(path)
    soup = BeautifulSoup(html, "html.parser")
    zname = page_zone_name(soup)
    if not booked_only:
        for r in _paginate_zone(sess, path, soup, zone_id, log=log):
            r["reserved"] = False
            r["page_zone_name"] = zname
            yield r

    # Pass 2: switch to the booked-only filter — same radio button / postback
    # mechanism the site's own UI uses — and page through it the same way.
    hidden = parse_hidden_fields(soup)
    form = dict(hidden)
    form["__EVENTTARGET"] = "ctl00$MainContent$rdShowBooked"
    form["__EVENTARGUMENT"] = ""
    form["__LASTFOCUS"] = ""
    form["__VIEWSTATEENCRYPTED"] = ""
    form["ctl00$MainContent$txtPlotNumber"] = ""
    form["ctl00$MainContent$PlotType"] = "rdShowBooked"
    html = sess.post(path, form)
    soup = BeautifulSoup(html, "html.parser")

    table_present = soup.find(id=re.compile(r"grdPlots$")) is not None
    no_results = "لا يوجد" in html
    if not table_present and not no_results:
        if booked_only:
            # fast mode only reads this pass — a bad response must count as a
            # failed zone, never as "0 booked" (that would look like frees)
            raise RuntimeError("booked-filter response looks wrong")
        log(f"    [error] zone {zone_id}: booked-filter response looks wrong "
            f"(no grdPlots table, no 'no results' message) — treating as 0 "
            f"booked plots for this zone, but this needs a manual look, not "
            f"blind trust")
        return

    for r in _paginate_zone(sess, path, soup, zone_id, log=log, plot_type="rdShowBooked"):
        r["reserved"] = True
        yield r


def _harvest_zone_task(z, sess, log, booked_only=False):
    """Runs against the shared Session (one real Chrome browser — launching
    a fresh one per zone would mean relaunching Chrome dozens of times per
    run). Safe because DEFAULT_WORKERS is 1: zones are processed one at a
    time, never concurrently, so there is no cross-thread use of the same
    Playwright page."""
    try:
        return _zone_result(z, harvest_zone(sess, z["zone_id"], log=log, booked_only=booked_only), log)
    except Exception as e:  # pragma: no cover — _zone_result catches already
        log(f"    [error] zone {z['zone_id']} failed: {e}")
        return z, [], 0, [], False


def _zone_result(z, row_iter, log=print):
    """Turns a zone's raw plot rows into (z, reserved_details, count, all_rows, ok)."""
    reserved_details = []
    all_rows = []
    plot_count = 0
    ok = True
    try:
        for row in row_iter:
            plot_count += 1
            city, project = norm(z["city"]), norm(z["project"])
            block, plot = norm(row["block"]), norm(row["plot"])
            key = "|".join([city, block, plot])  # stable: no derived/cleaned text
            detail = {
                "key": key, "city": city, "project": project,
                "zone_id": z["zone_id"],
                "zone_name": z.get("zone_name") or row.get("page_zone_name") or "",
                "block": block, "plot": plot, "area": row["area"],
                "corner": row["corner"], "garden": row["garden"],
                "view": row["view"], "down": row["down"],
                "reserved": row["reserved"],
            }
            all_rows.append(detail)
            if row["reserved"]:
                reserved_details.append(detail)
    except Exception as e:
        ok = False
        log(f"    [error] zone {z['zone_id']} ({z['city']}/{z['project']}) failed: {e}")
    return z, reserved_details, plot_count, all_rows, ok


def _booked_form(soup):
    form = dict(parse_hidden_fields(soup))
    form["__EVENTTARGET"] = "ctl00$MainContent$rdShowBooked"
    form["__EVENTARGUMENT"] = ""
    form["__LASTFOCUS"] = ""
    form["__VIEWSTATEENCRYPTED"] = ""
    form["ctl00$MainContent$txtPlotNumber"] = ""
    form["ctl00$MainContent$PlotType"] = "rdShowBooked"
    return form


def harvest_booked_batch(sess, zones, log=print):
    """Booked-only read of several zones at once: all the zone pages are
    fetched together, then all the booked-filter postbacks together. Extra
    pager pages (zones with many bookings) are followed one by one.
    Returns [(z, reserved_details, count, all_rows, ok)] in order."""
    paths = [f"/ar/ViewZone.aspx?ID={z['zone_id']}" for z in zones]
    pages = sess.request_many([("GET", pth, None) for pth in paths])
    soups, posts = {}, []
    for i, html in enumerate(pages):
        if isinstance(html, Exception):
            continue
        soup = BeautifulSoup(html, "html.parser")
        soups[i] = page_zone_name(soup)
        posts.append((i, ("POST", paths[i], _booked_form(soup))))
    booked = dict(zip([i for i, _ in posts], sess.request_many([r for _, r in posts])))
    out = []
    for i, z in enumerate(zones):
        html = booked.get(i)
        if html is None or isinstance(html, Exception):
            err = pages[i] if isinstance(pages[i], Exception) else html
            log(f"    [error] zone {z['zone_id']} ({z['city']}/{z['project']}) failed: {err}")
            out.append((z, [], 0, [], False))
            continue
        soup = BeautifulSoup(html, "html.parser")
        if soup.find(id=re.compile(r"grdPlots$")) is None and "لا يوجد" not in html:
            log(f"    [error] zone {z['zone_id']} ({z['city']}/{z['project']}) failed: booked-filter response looks wrong")
            out.append((z, [], 0, [], False))
            continue
        zname = soups.get(i)

        def rows(soup=soup, i=i, z=z, zname=zname):
            for r in _paginate_zone(sess, paths[i], soup, z["zone_id"], log=log, plot_type="rdShowBooked"):
                r["reserved"] = True
                r["page_zone_name"] = zname
                yield r
        out.append(_zone_result(z, rows(), log))
    return out


NUCA_PARALLEL = int(os.environ.get("NUCA_PARALLEL", "6"))
ZONE_FORMS = Path(__file__).with_name("zone_forms.json")


def _load_forms():
    try:
        return json.loads(ZONE_FORMS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _page_links(soup):
    out = set()
    for a in soup.find_all("a", href=re.compile(r"__doPostBack")):
        m = re.search(r"grdPlots','Page\$(\d+)'", a.get("href", ""))
        if m:
            out.add(int(m.group(1)))
    return out


def _booked_view(html):
    """Soup if `html` is really the BOOKED-only list (the 'booked' radio is the
    checked one), else None. Guards against a stale form silently giving back
    the default AVAILABLE list — that would look like hundreds of bookings."""
    if not html or not ("grdPlots" in html or "لا يوجد" in html):
        return None
    soup = BeautifulSoup(html, "html.parser")
    radio = soup.find("input", {"value": "rdShowBooked"})
    if radio is None or not radio.has_attr("checked"):
        return None
    return soup


def _page_form(soup, k):
    form = dict(parse_hidden_fields(soup))
    form["__EVENTTARGET"] = "ctl00$MainContent$grdPlots"
    form["__EVENTARGUMENT"] = f"Page${k}"
    form["ctl00$MainContent$PlotType"] = "rdShowBooked"
    return form


def fast_booked_chunk(sess, zones, forms, log=print):
    """Booked lists of `zones` (<= NUCA_PARALLEL) read in parallel without the
    session cookie, all pager pages included. Uses the saved booked-filter
    form of each zone (skips the GET); a zone whose saved form no longer works
    gets a fresh one. Returns {zone_id: (z, details, count, rows, ok)} — zones
    that still fail come back ok=False so the caller can retry them the slow way."""
    path = {z["zone_id"]: f"/ar/ViewZone.aspx?ID={z['zone_id']}" for z in zones}
    st = {z["zone_id"]: {"z": z, "pages": {}, "bad": False} for z in zones}

    def fetch_page1(zs):
        htmls = sess.request_many_nc([("POST", path[z["zone_id"]], forms[str(z["zone_id"])]["form"]) for z in zs])
        return list(zip(zs, htmls))

    # fresh forms for zones that have none
    missing = [z for z in zones if str(z["zone_id"]) not in forms]
    if missing:
        for z, html in zip(missing, sess.request_many_nc([("GET", path[z["zone_id"]], None) for z in missing])):
            if html:
                soup = BeautifulSoup(html, "html.parser")
                forms[str(z["zone_id"])] = {"form": _booked_form(soup), "name": page_zone_name(soup)}
    have = [z for z in zones if str(z["zone_id"]) in forms]
    retry = []
    for z, html in fetch_page1(have):
        soup = _booked_view(html)
        if soup is not None:
            st[z["zone_id"]]["pages"][1] = soup
        else:
            retry.append(z)
    if retry:  # saved form went stale — get a fresh one and try once more
        for z, html in zip(retry, sess.request_many_nc([("GET", path[z["zone_id"]], None) for z in retry])):
            if html:
                soup = BeautifulSoup(html, "html.parser")
                forms[str(z["zone_id"])] = {"form": _booked_form(soup), "name": page_zone_name(soup)}
        for z, html in fetch_page1([z for z in retry if str(z["zone_id"]) in forms]):
            soup = _booked_view(html)
            if soup is not None:
                st[z["zone_id"]]["pages"][1] = soup
    for zid, x in st.items():
        if 1 not in x["pages"]:
            x["bad"] = True
    # follow the pagers: from the highest page fetched so far, every linked page beyond it
    for _ in range(60):
        tasks = []
        for zid, x in st.items():
            if x["bad"]:
                continue
            top = max(x["pages"])
            for k in sorted(_page_links(x["pages"][top])):
                if k > top and k not in x["pages"]:
                    tasks.append((zid, k, _page_form(x["pages"][top], k)))
        if not tasks:
            break
        for i in range(0, len(tasks), NUCA_PARALLEL):
            part = tasks[i:i + NUCA_PARALLEL]
            for (zid, k, _), html in zip(part, sess.request_many_nc([("POST", path[zid], f) for zid, k, f in part])):
                soup = _booked_view(html)
                if soup is not None and parse_plots_table(soup):
                    st[zid]["pages"][k] = soup
                else:
                    st[zid]["bad"] = True
    out = {}
    for zid, x in st.items():
        z = x["z"]
        if x["bad"]:
            out[zid] = (z, [], 0, [], False)
            continue
        name = (forms.get(str(zid)) or {}).get("name", "")
        seen, rows = set(), []
        for k in sorted(x["pages"]):
            for r in parse_plots_table(x["pages"][k]):
                key = (r["block"], r["plot"])
                if key in seen:
                    continue
                seen.add(key)
                r["reserved"] = True
                r["page_zone_name"] = name
                rows.append(r)
        out[zid] = _zone_result(z, iter(rows), log)
    return out


ZONES_CACHE = Path(__file__).with_name("zones_cache.json")


ROLL_STATE = Path(__file__).with_name("rolling_state.json")
REFRESH_TS = Path(__file__).with_name("refresh_ts.txt")
_SHARED = {"session": None}   # loop mode keeps one Chrome open across passes


def load_zone_cache(max_age_hours=None):
    try:
        if max_age_hours is not None and time.time() - ZONES_CACHE.stat().st_mtime > max_age_hours * 3600:
            return None
        zones = json.loads(ZONES_CACHE.read_text(encoding="utf-8"))
        return zones if isinstance(zones, list) and zones else None
    except Exception:
        return None


def next_refresh_slice(zones, runs_per_cycle):
    """Zone ids to fully re-read this run, rotating so every zone gets a full
    refresh once every `runs_per_cycle` runs (18 x 10 min = every 3 hours)."""
    n = len(zones)
    k = max(1, -(-n // max(1, runs_per_cycle)))
    try:
        pos = int(json.loads(ROLL_STATE.read_text()).get("pos", 0)) % n
    except Exception:
        pos = 0
    ids = [zones[(pos + i) % n]["zone_id"] for i in range(min(k, n))]
    ROLL_STATE.write_text(json.dumps({"pos": (pos + k) % n}))
    return set(ids)


ACTIVITY_FILE = Path(__file__).with_name("zone_activity.json")


def load_activity():
    try:
        return json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_activity(zone_id):
    a = load_activity()
    a[str(zone_id)] = time.time()
    try:
        ACTIVITY_FILE.write_text(json.dumps(a))
    except Exception:
        pass


ZONE_SIZES = Path(__file__).with_name("zone_sizes.json")
HOT_ZONES = int(os.environ.get("HOT_ZONES", "0"))           # re-read this many busiest zones...
HOT_EVERY = int(os.environ.get("HOT_EVERY", "16"))          # ...after every N other zones
HOT_WINDOW_H = float(os.environ.get("HOT_WINDOW_HOURS", "72"))


def load_zone_sizes(log=print):
    """{zone_id: total plots (available + booked)} — a zone with 0 plots has
    nothing that can be booked, so booked-only passes skip it (it is still
    fully re-read in its rolling-refresh slot, so a zone that gets plots
    later is picked up). Bootstrapped once from Supabase."""
    try:
        return {int(k): v for k, v in json.loads(ZONE_SIZES.read_text(encoding="utf-8")).items()}
    except Exception:
        pass
    sizes = {}
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        try:
            import requests
            r = requests.get(url.rstrip("/") + "/rest/v1/v_stats_by_zone",
                             params={"select": "zone_id,total_plots"},
                             headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=30)
            if r.ok:
                sizes = {int(x["zone_id"]): int(x["total_plots"] or 0) for x in r.json() if x.get("zone_id") is not None}
                log(f"[zones] loaded plot counts for {len(sizes)} zone(s) from Supabase")
        except Exception as e:
            log(f"[zones] could not load zone sizes: {e}")
    return sizes


def save_zone_sizes(sizes):
    try:
        ZONE_SIZES.write_text(json.dumps({str(k): v for k, v in sizes.items()}), encoding="utf-8")
    except Exception:
        pass


def plan_booked_order(zones, refresh, sizes, act, log=print):
    """Booked-only pass order:
    - zones known to have 0 plots are skipped (unless due for a full refresh)
    - the busiest zones (most recent bookings) go first AND are re-read after
      every HOT_EVERY other zones, so a booking there is caught within ~a
      minute instead of waiting for the whole pass to come round again."""
    now = time.time()
    if sizes:
        # a zone missing from the counts has never shown a plot (Supabase only
        # lists zones that have plots); its refresh slot re-checks it anyway
        live = [z for z in zones if z["zone_id"] in refresh or sizes.get(z["zone_id"], 0) > 0]
        skipped = len(zones) - len(live)
        if skipped:
            log(f"[plan] skipping {skipped} zone(s) with no plots")
    else:
        live = list(zones)
    live.sort(key=lambda z: -float(act.get(str(z["zone_id"]), 0)))
    hot = [z for z in live if now - float(act.get(str(z["zone_id"]), 0)) < HOT_WINDOW_H * 3600][:HOT_ZONES]
    hot_ids = {z["zone_id"] for z in hot}
    cold = [z for z in live if z["zone_id"] not in hot_ids]
    order = [(z, False) for z in hot]
    for i in range(0, len(cold), HOT_EVERY):
        order += [(z, False) for z in cold[i:i + HOT_EVERY]]
        if hot and i + HOT_EVERY < len(cold):
            order += [(z, True) for z in hot]   # True = repeat read (booked-only)
    if hot:
        log(f"[plan] {len(hot)} busy zone(s) re-read every {HOT_EVERY} zones: {sorted(hot_ids)}")
    return order


def run(cities=None, delay=DEFAULT_DELAY, workers=DEFAULT_WORKERS, log=print, mode="full",
        on_zone=None):
    """mode="full": discover every zone and read available + booked plots.
    mode="fast": reuse the zone list from the last full run and read ONLY the
    booked-plots pass of each zone — ~1k plots instead of ~15k, so it can run
    every few minutes and catch new bookings quickly."""
    if workers != 1:
        log(f"[warn] workers={workers} requested, but the Chrome-driven Session "
            f"is single-browser — forcing workers=1 (one zone at a time).")
        workers = 1

    own = _SHARED["session"] is None
    sess = Session(delay=delay) if own else _SHARED["session"]
    sess.delay = delay
    try:
        booked_only = mode in ("fast", "rolling")
        zones = None
        if mode == "fast":
            zones = load_zone_cache()
        elif mode == "rolling":
            # re-discover once a day so new projects/zones get picked up
            zones = load_zone_cache(max_age_hours=float(os.environ.get("DISCOVER_EVERY_HOURS", "24")))
        if zones is None:
            if booked_only:
                log(f"[{mode}] zone list missing or old — discovering zones first")
            zones = discover_zones(sess, cities=cities, log=log)
            if not cities and zones:
                ZONES_CACHE.write_text(json.dumps(zones, ensure_ascii=False), encoding="utf-8")
        log(f"[discover] {len(zones)} zone(s) to harvest ({mode} mode)")

        reserved_details = []
        all_plots = []
        plot_total = 0
        errors = 0
        ok_zone_ids, failed_cities = set(), set()
        refresh = set()
        if mode == "rolling":
            # full re-reads are slow (whole available lists) — only every
            # REFRESH_EVERY_SEC, so the booked pass between them stays fast
            due = True
            try:
                due = time.time() - REFRESH_TS.stat().st_mtime >= float(os.environ.get("REFRESH_EVERY_SEC", "300"))
            except Exception:
                pass
            if due:
                refresh = next_refresh_slice(zones, int(os.environ.get("ROLL_RUNS", "18")))
                REFRESH_TS.write_text(str(time.time()))
        if refresh:
            log(f"[rolling] full refresh this run: zones {sorted(refresh)}")
        sizes = load_zone_sizes(log) if booked_only else {}
        if booked_only:
            order = plan_booked_order(zones, refresh, sizes, load_activity(), log=log)
        else:
            order = [(z, False) for z in zones]
        t0 = time.time()
        seen = set()
        conc = max(1, int(os.environ.get("ZONE_CONCURRENCY", "4"))) if booked_only else 1

        def handle(z, repeat, res):
            nonlocal plot_total, errors
            z, details, count, rows, ok = res
            zone_booked_only = booked_only and (repeat or z["zone_id"] not in refresh)
            if not repeat:
                seen.add(z["zone_id"])
                if not ok or (count == 0 and not booked_only):
                    errors += 1
                if ok:
                    ok_zone_ids.add(z["zone_id"])
                    if not zone_booked_only:
                        sizes[z["zone_id"]] = count
                    elif count and sizes.get(z["zone_id"], 0) < count:
                        sizes[z["zone_id"]] = count
                else:
                    failed_cities.add(norm(z["city"]))
                plot_total += count
                reserved_details.extend(details)
                if not zone_booked_only:
                    all_plots.extend(rows)
                log(f"[harvest] {z['city']} / {z['project']} / zone {z['zone_id']} — "
                    f"{count} plot(s), {len(details)} reserved")
            elif ok:
                # a re-read of a busy zone: only for faster alerts — never let a
                # flaky repeat mark the city as failed or double-count plots
                reserved_details.extend(details)
            if on_zone and ok:
                try:
                    on_zone(z, details, ok)
                except Exception as e:
                    log(f"    [instant] notify failed: {e}")

        if booked_only and NUCA_PARALLEL > 1:
            forms = _load_forms()
            firsts = [z for z, rep_ in order if not rep_]
            dead_chunks = 0
            for c in range(0, len(firsts), NUCA_PARALLEL):
                chunk = firsts[c:c + NUCA_PARALLEL]
                res = {}
                if dead_chunks < 2:
                    try:
                        res = fast_booked_chunk(sess, chunk, forms, log=log)
                    except Exception as e:
                        log(f"    [fast] chunk failed ({e}) — reading those zones the normal way")
                    if any(r[4] for r in res.values()):
                        dead_chunks = 0
                    else:
                        dead_chunks += 1
                        if dead_chunks == 2:
                            log("    [fast] parallel reading is not working right now — normal reading for the rest of this pass")
                for z in chunk:
                    r = res.get(z["zone_id"])
                    if not r or not r[4]:
                        r = _harvest_zone_task(z, sess, log, booked_only=True)   # slow, cookie path
                    if z["zone_id"] in refresh:
                        if on_zone and r[4]:
                            try:
                                on_zone(z, r[1], True)
                            except Exception as e:
                                log(f"    [instant] notify failed: {e}")
                        continue        # handled by the full read below
                    handle(z, False, r)
            try:
                ZONE_FORMS.write_text(json.dumps(forms, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            for z in zones:
                if z["zone_id"] in refresh:
                    handle(z, False, _harvest_zone_task(z, sess, log, booked_only=False))
            order = []
        i = 0
        while i < len(order):
            z, repeat = order[i]
            if not booked_only or (z["zone_id"] in refresh and not repeat):
                handle(z, repeat, _harvest_zone_task(z, sess, log, booked_only=booked_only and repeat))
                i += 1
                continue
            batch = []
            while (i < len(order) and len(batch) < conc
                   and (order[i][1] or order[i][0]["zone_id"] not in refresh)):
                batch.append(order[i])
                i += 1
            try:
                results = harvest_booked_batch(sess, [b[0] for b in batch], log=log)
            except Exception as e:
                log(f"    [error] batch failed ({e}) — reading those zones one by one")
                results = [_harvest_zone_task(b[0], sess, log, booked_only=True) for b in batch]
            for (z, repeat), res in zip(batch, results):
                handle(z, repeat, res)

        if sizes and not cities:
            save_zone_sizes(sizes)
        elapsed = time.time() - t0
        log(f"[harvest] done in {elapsed:.1f}s")
        if errors:
            log(f"[warn] {errors} zone(s) returned 0 plots — check the log above for "
                f"errors before trusting this run's numbers")
        log(f"[harvest] {plot_total} plot rows read, {len(reserved_details)} reserved")
        return (reserved_details, plot_total, all_plots, errors, ok_zone_ids, failed_cities,
                len(seen) or len(zones), refresh & ok_zone_ids)
    finally:
        if own:
            sess.close()


def load_previous(path):
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("reserved", []))
    except Exception:
        return set()


class InstantNotifier:
    """Posts a booking to Telegram as soon as its zone has been read, instead
    of waiting for the whole ~5-minute pass to finish. Whatever it doesn't
    send (first run, a suspicious burst) is left for the end-of-run diff."""
    ZONE_BURST_MAX = 120  # opening rush at 11:00 can book dozens in one zone

    def __init__(self, previous, enabled=True):
        self.prev = previous
        self.sent = set()
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat = os.environ.get("TELEGRAM_CHAT_ID")
        # no baseline yet (first run on a machine) -> everything looks new; stay quiet
        self.enabled = bool(enabled and previous and self.token and self.chat)

    def __call__(self, z, details, ok):
        if not (self.enabled and ok):
            return
        new = [d for d in details if d["key"] not in self.prev and d["key"] not in self.sent]
        if not new:
            return
        mark_activity(z["zone_id"])
        if len(new) > self.ZONE_BURST_MAX:
            print(f"    [instant] {len(new)} new in zone {z['zone_id']} at once — leaving to end-of-run checks")
            return
        import telegram_notify
        for d in new:
            seq = update_daily_count(1)
            _, late, label = alloc_info()
            msg = telegram_notify.build_message(d, seq, late=late, alloc_label=label)
            if telegram_notify.send(self.token, self.chat, msg):
                self.sent.add(d["key"])
                print(f"    [instant] posted {d['city']} / {d['block']} / {d['plot']} (#{seq})", flush=True)
            else:
                update_daily_count(-1)
            time.sleep(1)


def update_daily_count(new_count, path="daily_stats.json"):
    """Running total of new reservations for the current allocation day
    (starts 11:00 Cairo; Fri/Sat/holidays roll into the last working day)."""
    today = alloc_day().isoformat()
    data = {"date": today, "count": 0}
    p = Path(path)
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            if existing.get("date") == today:
                data = existing
        except Exception:
            pass
    data["date"] = today
    data["count"] = data.get("count", 0) + new_count
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data["count"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                     help="Only crawl one city (المنيا الجديدة) for a quick sanity check")
    ap.add_argument("--out", default="status.json")
    ap.add_argument("--delay", type=float, default=None)
    ap.add_argument("--mode", choices=["auto", "rolling", "fast", "full"], default="auto",
                    help="auto/rolling (default): every run reads all booked plots and fully "
                         "re-reads a rotating slice of zones — no long full runs")
    ap.add_argument("--loop-minutes", type=float, default=None,
                    help="keep one Chrome open and repeat passes for this many minutes "
                         "(default: LOOP_MINUTES from .env, else 0 = single pass)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                                             help="zones harvested in parallel (default 2 — raise cautiously)")
    args = ap.parse_args()

    cities = ["المنيا الجديدة"] if args.test else None
    if args.test and args.out == "status.json":
        # never let a one-city test overwrite the real status.json baseline
        args.out = "status_test.json"

    load_dotenv()
    started_at = datetime.now(timezone.utc)
    lock = Path(__file__).with_name("sync.lock")
    # a killed run leaves the lock behind — treat it as stale after 50 min
    # (a full run takes ~30 min)
    if lock.exists() and time.time() - lock.stat().st_mtime < 50 * 60:
        print("[lock] another run is still in progress — skipping this one")
        return 3  # non-zero: run_sync.bat must not send Telegram for a skipped run
    lock.write_text(started_at.isoformat())
    loop = args.loop_minutes if args.loop_minutes is not None else float(os.environ.get("LOOP_MINUTES", "0"))
    try:
        if loop > 0 and not args.test:
            return _loop(args, lock, loop)
        return _main_locked(args, started_at)
    finally:
        try:
            lock.unlink()
        except Exception:
            pass


def _loop(args, lock, minutes):
    """Back-to-back passes with one Chrome kept open (no relaunch + VPN warm-up
    every pass). Telegram leftovers are sent after each pass."""
    import telegram_notify
    end = time.time() + minutes * 60
    delay = args.delay if args.delay is not None else float(os.environ.get("FAST_DELAY", "0.6"))
    passes = 0
    try:
        while True:
            if _SHARED["session"] is None:
                try:
                    _SHARED["session"] = Session(delay=delay)
                except Exception as e:
                    print(f"[loop] could not open Chrome: {e}", flush=True)
                    return 1 if passes == 0 else 0
            started = datetime.now(timezone.utc)
            lock.write_text(started.isoformat())
            try:
                rc = _main_locked(args, started)
            except SystemExit as e:
                rc = e.code
            except Exception as e:
                print(f"[loop] pass failed: {e}", flush=True)
                rc = 1
            passes += 1
            if rc in (None, 0):
                try:
                    telegram_notify.main()
                except Exception as e:
                    print(f"[telegram] failed: {e}", flush=True)
            else:
                # a broken pass may mean Chrome / VPN died — start fresh next pass
                try:
                    _SHARED["session"].close()
                except Exception:
                    pass
                _SHARED["session"] = None
            if time.time() >= end:
                return 0
            time.sleep(float(os.environ.get("LOOP_PAUSE_SEC", "3")))
    finally:
        if _SHARED["session"] is not None:
            try:
                _SHARED["session"].close()
            except Exception:
                pass
            _SHARED["session"] = None


SYNC_TS = Path(__file__).with_name("last_sync.txt")


def _sync_recent(max_age=600):
    try:
        return time.time() - SYNC_TS.stat().st_mtime < max_age
    except Exception:
        return False


def _mark_synced():
    try:
        SYNC_TS.write_text(str(time.time()))
    except Exception:
        pass


def _main_locked(args, started_at):
    cities = ["المنيا الجديدة"] if args.test else None
    mode = "full" if args.test else args.mode
    last_full_file = Path(__file__).with_name("last_full.txt")
    if mode == "auto":
        mode = "rolling"
    delay = args.delay if args.delay is not None else (
        float(os.environ.get("FAST_DELAY", "0.6")) if mode in ("fast", "rolling") else DEFAULT_DELAY)
    print(f"[mode] {mode} (delay {delay}s)", flush=True)

    print(f"==== {cairo_now().strftime('%Y-%m-%d %H:%M:%S')} ====", flush=True)
    previous = load_previous(args.out)
    notifier = InstantNotifier(previous, enabled=not args.test)
    (reserved_details, plot_total, all_plots, zone_errors,
     ok_zone_ids, failed_cities, zone_count, refreshed_ok) = run(
        cities=cities, delay=delay, workers=args.workers, mode=mode, on_zone=notifier)

    if mode in ("fast", "rolling") and zone_count and zone_errors > 0.3 * zone_count:
        supabase_sync.log_failed_run(started_at, f"fast: {zone_errors}/{zone_count} zones failed")
        print(f"\n[abort] {zone_errors} of {zone_count} zones failed — not touching anything this run.")
        sys.exit(1)

    if plot_total == 0:
        supabase_sync.log_failed_run(started_at, "0 plot rows read")
        print(
            "\n[abort] 0 plot rows read this run — this is almost certainly a "
            "parsing/discovery failure, NOT 'zero plots exist'. Refusing to "
            "touch status.json or daily_stats.json so we don't corrupt the "
            "saved baseline (this is exactly what caused the inflated daily "
            "count on 2026-09-22 — a failed run wiped the previous state)."
        )
        sys.exit(1)

    current_map = {d["key"]: d for d in reserved_details}
    current = set(current_map.keys())
    # A zone that failed this run must not look like "all its plots got freed"
    # (and then "newly booked" again next run) — keep last run's bookings for
    # the cities that had a failed zone.
    if failed_cities:
        kept = {k for k in previous if k.split("|", 1)[0] in failed_cities}
        current |= kept
        print(f"[warn] kept {len(kept)} previous booking(s) for cities with failed zones: "
              f"{', '.join(sorted(failed_cities))}")

    if len(current) == 0 and plot_total > 500 and len(previous) > 0:
        print(
            f"\n[abort] {plot_total} plot rows read but 0 came back reserved — "
            "given the previous run found reservations, this is almost certainly "
            "a booked-filter request failure (e.g. every zone timing out on that "
            "specific request), NOT a real drop to zero. Refusing to touch "
            "status.json so we don't overwrite real reservation data with this."
        )
        supabase_sync.log_failed_run(started_at, "booked-filter returned 0 reserved")
        sys.exit(1)

    # Supabase: the live source for the dashboard. A failure here must not
    # stop status.json / Telegram from working, so it's isolated.
    quiet = (mode in ("fast", "rolling") and not refreshed_ok and not failed_cities
             and current == previous and _sync_recent())
    try:
        if quiet:
            # nothing changed since the last pass — don't re-download the plots
            # table (Supabase egress); just record that we checked
            supabase_sync.log_quiet_run(started_at, len(current), zone_errors)
        elif mode in ("fast", "rolling"):
            _mark_synced()
            supabase_sync.sync_booked(reserved_details, started_at, ok_zone_ids,
                                      zone_errors=zone_errors, run_note=mode)
            if refreshed_ok and all_plots:
                supabase_sync.sync(all_plots, started_at, zone_errors=0, full_run=False,
                                   zone_scope=refreshed_ok, record_run=False)
        else:
            supabase_sync.sync(all_plots, started_at, zone_errors=zone_errors,
                               full_run=not args.test)
            # only zones that threw count as failures here (a zone can legitimately
            # have 0 plots listed), otherwise every run would fall back to "full"
            if not args.test and not failed_cities:
                last_full_file.write_text(started_at.isoformat())
    except Exception as e:
        print(f"[supabase][error] sync failed: {e}")
        supabase_sync.log_failed_run(started_at, f"sync failed: {e}")

    newly_reserved = sorted(current - previous)
    newly_freed = sorted(previous - current)
    already_sent = notifier.sent
    if already_sent:
        print(f"[instant] {len(already_sent)} booking(s) were already posted to Telegram during the run")
    unsent = [k for k in newly_reserved if k not in already_sent]

    now = cairo_now()
    payload = {
        "reserved": sorted(current),
        "updatedAt": now.strftime("%Y-%m-%d %H:%M"),
    }
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8"
    )

    if len(newly_reserved) > DAILY_SPAM_GUARD:
        print(f"[guard] {len(newly_reserved)} newly-reserved plots in one run is over "
              f"the sanity threshold ({DAILY_SPAM_GUARD}) — treating this as a matching "
              f"glitch, NOT adding it to today's count, and not sending per-plot Telegram alerts")
        today_total = update_daily_count(0)
    else:
        today_total = update_daily_count(len(unsent))

    print(f"\n=== SUMMARY ===")
    print(f"total reserved now: {len(current)}")
    print(f"newly reserved since last run: {len(newly_reserved)}")
    print(f"newly freed since last run: {len(newly_freed)}")
    print(f"total reserved today: {today_total}")

    # Rich per-plot details for just the NEW reservations, for Telegram
    _alloc, _late, _label = alloc_info()
    diff_payload = {
        "today_total": today_total,
        "alloc_day": _alloc, "late": _late, "alloc_label": _label,
        "updatedAt": now.strftime("%Y-%m-%d %H:%M"),
        "items": [current_map[k] for k in unsent],
    }
    Path("diff_new_reservations.json").write_text(
        json.dumps(diff_payload, ensure_ascii=False), encoding="utf-8"
    )

    if args.test and len(current) == 0 and len(previous) == 0:
        print(
            "\n[test mode] No reserved plots found for المنيا الجديدة. "
            "Manually check a couple of plots on lands.nuca.gov.eg yourself "
            "before trusting this — this could mean 'genuinely none reserved "
            "right now' OR 'the parser missed the status column'."
        )


if __name__ == "__main__":
    sys.exit(main())

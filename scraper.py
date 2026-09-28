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
DAILY_SPAM_GUARD = 15  # more "new" reservations than this in one 5-minute run
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


def cairo_now():
    return datetime.now(timezone.utc) + timedelta(hours=3)


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
                args=["--lang=ar-EG"],
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

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, data):
        return self._request("POST", path, data=data)

    def close(self):
        try:
            self.context.close()
        finally:
            self._pw.stop()


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
    reserved_details = []
    all_rows = []
    plot_count = 0
    ok = True
    try:
        for row in harvest_zone(sess, z["zone_id"], log=log, booked_only=booked_only):
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


ZONES_CACHE = Path(__file__).with_name("zones_cache.json")


def load_zone_cache():
    try:
        zones = json.loads(ZONES_CACHE.read_text(encoding="utf-8"))
        return zones if isinstance(zones, list) and zones else None
    except Exception:
        return None


def run(cities=None, delay=DEFAULT_DELAY, workers=DEFAULT_WORKERS, log=print, mode="full"):
    """mode="full": discover every zone and read available + booked plots.
    mode="fast": reuse the zone list from the last full run and read ONLY the
    booked-plots pass of each zone — ~1k plots instead of ~15k, so it can run
    every few minutes and catch new bookings quickly."""
    if workers != 1:
        log(f"[warn] workers={workers} requested, but the Chrome-driven Session "
            f"is single-browser — forcing workers=1 (one zone at a time).")
        workers = 1

    sess = Session(delay=delay)
    try:
        booked_only = mode == "fast"
        zones = load_zone_cache() if booked_only else None
        if zones is None:
            if booked_only:
                log("[fast] no zone cache yet — discovering zones first")
            zones = discover_zones(sess, cities=cities, log=log)
            if not cities and zones:
                ZONES_CACHE.write_text(json.dumps(zones, ensure_ascii=False), encoding="utf-8")
        log(f"[discover] {len(zones)} zone(s) to harvest ({mode} mode)")

        reserved_details = []
        all_plots = []
        plot_total = 0
        errors = 0
        ok_zone_ids, failed_cities = set(), set()
        t0 = time.time()

        for z in zones:
            z, details, count, rows, ok = _harvest_zone_task(z, sess, log, booked_only=booked_only)
            if not ok or (count == 0 and not booked_only):
                errors += 1
            if ok:
                ok_zone_ids.add(z["zone_id"])
            else:
                failed_cities.add(norm(z["city"]))
            plot_total += count
            reserved_details.extend(details)
            all_plots.extend(rows)
            log(f"[harvest] {z['city']} / {z['project']} / zone {z['zone_id']} — "
                f"{count} plot(s), {len(details)} reserved")

        elapsed = time.time() - t0
        log(f"[harvest] done in {elapsed:.1f}s")
        if errors:
            log(f"[warn] {errors} zone(s) returned 0 plots — check the log above for "
                f"errors before trusting this run's numbers")
        log(f"[harvest] {plot_total} plot rows read, {len(reserved_details)} reserved")
        return reserved_details, plot_total, all_plots, errors, ok_zone_ids, failed_cities, len(zones)
    finally:
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


def update_daily_count(new_count, path="daily_stats.json"):
    """Keeps a running total of new reservations for 'today' (Cairo time),
    resetting automatically when the date rolls over."""
    today = cairo_now().strftime("%Y-%m-%d")
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
    ap.add_argument("--mode", choices=["auto", "fast", "full"], default="auto",
                    help="auto (default): full run if the last full one is older than "
                         "FULL_EVERY_HOURS, otherwise a fast booked-only run")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                                             help="zones harvested in parallel (default 2 — raise cautiously)")
    args = ap.parse_args()

    cities = ["المنيا الجديدة"] if args.test else None
    if args.test and args.out == "status.json":
        # never let a one-city test overwrite the real status.json baseline
        args.out = "status_test.json"

    load_dotenv()
    started_at = datetime.now(timezone.utc)
    mode = "full" if args.test else args.mode
    last_full_file = Path(__file__).with_name("last_full.txt")
    if mode == "auto":
        every_h = float(os.environ.get("FULL_EVERY_HOURS", "3"))
        try:
            last_full = datetime.fromisoformat(last_full_file.read_text().strip())
            mode = "full" if (started_at - last_full).total_seconds() > every_h * 3600 else "fast"
        except Exception:
            mode = "full"
    delay = args.delay if args.delay is not None else (
        float(os.environ.get("FAST_DELAY", "0.8")) if mode == "fast" else DEFAULT_DELAY)
    print(f"[mode] {mode} (delay {delay}s)", flush=True)

    previous = load_previous(args.out)
    (reserved_details, plot_total, all_plots, zone_errors,
     ok_zone_ids, failed_cities, zone_count) = run(
        cities=cities, delay=delay, workers=args.workers, mode=mode)

    if mode == "fast" and zone_count and zone_errors > 0.3 * zone_count:
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
    try:
        if mode == "fast":
            supabase_sync.sync_booked(reserved_details, started_at, ok_zone_ids,
                                      zone_errors=zone_errors)
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
        today_total = update_daily_count(len(newly_reserved))

    print(f"\n=== SUMMARY ===")
    print(f"total reserved now: {len(current)}")
    print(f"newly reserved since last run: {len(newly_reserved)}")
    print(f"newly freed since last run: {len(newly_freed)}")
    print(f"total reserved today: {today_total}")

    # Rich per-plot details for just the NEW reservations, for Telegram
    diff_payload = {
        "today_total": today_total,
        "updatedAt": now.strftime("%Y-%m-%d %H:%M"),
        "items": [current_map[k] for k in newly_reserved],
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

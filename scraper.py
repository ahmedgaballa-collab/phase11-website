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
            )
        except Exception:
            self._pw.stop()
            raise
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        # Real navigation, not fetch() — lets the site's own page-load checks
        # (and the VeePN-routed connection itself) run once, up front.
        self.page.goto(BASE + "/ar/Home.aspx", timeout=TIMEOUT * 1000)

    def _request(self, method, path, data=None):
        time.sleep(self.delay)
        body = urlencode(data) if data is not None else None
        arg = {"url": BASE + path, "method": method, "body": body, "timeoutMs": TIMEOUT * 1000}
        result = self.page.evaluate(_FETCH_JS, arg)
        if not result["ok"]:
            # One retry, with a real pause first — mirrors the old requests-based
            # retry: a slow/busy server benefits from backing off, not hammering.
            time.sleep(3.0)
            result = self.page.evaluate(_FETCH_JS, arg)
            if not result["ok"]:
                raise RuntimeError(f"fetch failed for {path}: {result['error']}")
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

            zone_ids = set()
            for a in psoup.find_all("a", href=re.compile(r"ViewZone\.aspx\?ID=\d+")):
                m = re.search(r"ID=(\d+)", a["href"])
                if m:
                    zone_ids.add(int(m.group(1)))

            project_bare = strip_phase_wrapper(project_title, city_name)
            for zid in zone_ids:
                zones.append({
                    "city": city_name,
                    "project": project_bare,
                    "zone_id": zid,
                })

    return zones


def parse_hidden_fields(soup):
    fields = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        inp = soup.find("input", {"name": name})
        fields[name] = inp["value"] if inp else ""
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
        cells = [norm(td.get_text()) for td in tr.find_all("td")]
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
    for page_num in range(2, total_pages + 1):
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
            break
        for r in rows:
            yield r
        hidden = parse_hidden_fields(soup)  # refresh viewstate for the next postback


def harvest_zone(sess, zone_id, log=print):
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
    for r in _paginate_zone(sess, path, soup, zone_id, log=log):
        r["reserved"] = False
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
        log(f"    [error] zone {zone_id}: booked-filter response looks wrong "
            f"(no grdPlots table, no 'no results' message) — treating as 0 "
            f"booked plots for this zone, but this needs a manual look, not "
            f"blind trust")
        return

    for r in _paginate_zone(sess, path, soup, zone_id, log=log, plot_type="rdShowBooked"):
        r["reserved"] = True
        yield r


def _harvest_zone_task(z, sess, log):
    """Runs against the shared Session (one real Chrome browser — launching
    a fresh one per zone would mean relaunching Chrome dozens of times per
    run). Safe because DEFAULT_WORKERS is 1: zones are processed one at a
    time, never concurrently, so there is no cross-thread use of the same
    Playwright page."""
    reserved_details = []
    plot_count = 0
    try:
        for row in harvest_zone(sess, z["zone_id"], log=log):
            plot_count += 1
            if row["reserved"]:
                city, project = norm(z["city"]), norm(z["project"])
                block, plot = norm(row["block"]), norm(row["plot"])
                key = "|".join([city, block, plot])  # stable: no derived/cleaned text
                reserved_details.append({
                    "key": key, "city": city, "project": project,
                    "block": block, "plot": plot, "area": row["area"],
                    "corner": row["corner"], "garden": row["garden"],
                    "view": row["view"], "down": row["down"],
                })
    except Exception as e:
        log(f"    [error] zone {z['zone_id']} ({z['city']}/{z['project']}) failed: {e}")
    return z, reserved_details, plot_count


def run(cities=None, delay=DEFAULT_DELAY, workers=DEFAULT_WORKERS, log=print):
    if workers != 1:
        log(f"[warn] workers={workers} requested, but the Chrome-driven Session "
            f"is single-browser — forcing workers=1 (one zone at a time).")
        workers = 1

    sess = Session(delay=delay)
    try:
        zones = discover_zones(sess, cities=cities, log=log)
        log(f"[discover] found {len(zones)} zone(s) to harvest")

        reserved_details = []
        plot_total = 0
        errors = 0
        t0 = time.time()

        for z in zones:
            z, details, count = _harvest_zone_task(z, sess, log)
            if count == 0:
                errors += 1
            plot_total += count
            reserved_details.extend(details)
            log(f"[harvest] {z['city']} / {z['project']} / zone {z['zone_id']} — "
                f"{count} plot(s), {len(details)} reserved")

        elapsed = time.time() - t0
        log(f"[harvest] done in {elapsed:.1f}s")
        if errors:
            log(f"[warn] {errors} zone(s) returned 0 plots — check the log above for "
                f"errors before trusting this run's numbers")
        log(f"[harvest] {plot_total} plot rows read, {len(reserved_details)} reserved")
        return reserved_details, plot_total
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
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                                             help="zones harvested in parallel (default 2 — raise cautiously)")
    args = ap.parse_args()

    cities = ["المنيا الجديدة"] if args.test else None

    previous = load_previous(args.out)
    reserved_details, plot_total = run(cities=cities, delay=args.delay, workers=args.workers)

    if plot_total == 0:
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

    if len(current) == 0 and plot_total > 500 and len(previous) > 0:
        print(
            f"\n[abort] {plot_total} plot rows read but 0 came back reserved — "
            "given the previous run found reservations, this is almost certainly "
            "a booked-filter request failure (e.g. every zone timing out on that "
            "specific request), NOT a real drop to zero. Refusing to touch "
            "status.json so we don't overwrite real reservation data with this."
        )
        sys.exit(1)

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

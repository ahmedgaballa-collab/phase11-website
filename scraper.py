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
   with plain POST requests, no browser needed) and reads the "طلب الحجز"
   (booking) column for every plot.
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
- Be a good citizen: the DEFAULT_DELAY between requests exists on purpose.
  Do not remove it. This is a government site with no rate-limit
  allowance published — assume none and be conservative.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://lands.nuca.gov.eg"
PHASE_MARKER = "المرحلة الحادية عشر"
DEFAULT_DELAY = 1.2  # seconds between requests — be polite to a government site
TIMEOUT = 25

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

RESERVED_MARKERS = ("حجز",)  # "حجز مبدئى" etc. — any cell containing "حجز" = reserved


def norm(s):
    """Collapse whitespace the same way the website's JS normKey() does."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", " ", s).strip()


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


class Session:
    def __init__(self, delay=DEFAULT_DELAY):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay

    def get(self, path):
        time.sleep(self.delay)
        r = self.s.get(BASE + path, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    def post(self, path, data):
        time.sleep(self.delay)
        r = self.s.post(BASE + path, data=data, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text


def discover_zones(sess, cities=None, log=print):
    """
    Returns a list of dicts: {city, project, zone_id, block_label}
    covering every Phase 11 zone page found.
    """
    zones = []
    target_cities = {k: v for k, v in CITIES.items() if not cities or k in cities}

    for city_name, city_id in target_cities.items():
        log(f"[discover] city: {city_name}")
        html = sess.get(f"/ar/ViewCity.aspx?ID={city_id}")
        soup = BeautifulSoup(html, "html.parser")

        project_links = []
        for a in soup.find_all("a", href=re.compile(r"ViewProject\.aspx\?ID=\d+")):
            title = norm(a.get_text())
            if PHASE_MARKER in title:
                m = re.search(r"ID=(\d+)", a["href"])
                if m:
                    project_links.append((int(m.group(1)), title))

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
        block, plot = cells[0], cells[1]
        status_cell = cells[10] if len(cells) > 10 else ""
        reserved = any(marker in status_cell for marker in RESERVED_MARKERS)
        rows.append({"block": block, "plot": plot, "reserved": reserved})
    return rows


def max_page_number(soup):
    pages = set()
    for a in soup.find_all("a", href=re.compile(r"__doPostBack")):
        m = re.search(r"grdPlots','Page\$(\d+)'", a.get("href", ""))
        if m:
            pages.add(int(m.group(1)))
    return max(pages) if pages else 1


def harvest_zone(sess, zone_id, log=print):
    """Yields dicts {block, plot, reserved} for every plot in this zone."""
    path = f"/ar/ViewZone.aspx?ID={zone_id}"
    html = sess.get(path)
    soup = BeautifulSoup(html, "html.parser")

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
        html = sess.post(path, form)
        soup = BeautifulSoup(html, "html.parser")
        rows = parse_plots_table(soup)
        if not rows:
            log(f"    [warn] zone {zone_id} page {page_num}: no rows parsed, stopping")
            break
        for r in rows:
            yield r
        hidden = parse_hidden_fields(soup)  # refresh viewstate for the next postback


def run(cities=None, delay=DEFAULT_DELAY, log=print):
    sess = Session(delay=delay)
    zones = discover_zones(sess, cities=cities, log=log)
    log(f"[discover] found {len(zones)} zone(s) to harvest")

    reserved_keys = []
    plot_total = 0
    for z in zones:
        log(f"[harvest] {z['city']} / {z['project']} / zone {z['zone_id']}")
        try:
            for row in harvest_zone(sess, z["zone_id"], log=log):
                plot_total += 1
                if row["reserved"]:
                    key = "|".join([
                        norm(z["city"]), norm(z["project"]),
                        norm(row["block"]), norm(row["plot"]),
                    ])
                    reserved_keys.append(key)
        except requests.RequestException as e:
            log(f"    [error] zone {z['zone_id']} failed: {e} — skipping")

    log(f"[harvest] {plot_total} plot rows read, {len(reserved_keys)} reserved")
    return reserved_keys


def load_previous(path):
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("reserved", []))
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                     help="Only crawl one city (المنيا الجديدة) for a quick sanity check")
    ap.add_argument("--out", default="status.json")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = ap.parse_args()

    cities = ["المنيا الجديدة"] if args.test else None

    previous = load_previous(args.out)
    reserved = run(cities=cities, delay=args.delay)
    current = set(reserved)

    newly_reserved = sorted(current - previous)
    newly_freed = sorted(previous - current)

    from datetime import datetime, timezone, timedelta
    cairo_now = datetime.now(timezone.utc) + timedelta(hours=3)
    payload = {
        "reserved": sorted(current),
        "updatedAt": cairo_now.strftime("%Y-%m-%d %H:%M"),
    }
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8"
    )

    print(f"\n=== SUMMARY ===")
    print(f"total reserved now: {len(current)}")
    print(f"newly reserved since last run: {len(newly_reserved)}")
    print(f"newly freed since last run: {len(newly_freed)}")

    # Write the diff for the Telegram notifier to pick up
    Path("diff_new_reservations.json").write_text(
        json.dumps(newly_reserved, ensure_ascii=False), encoding="utf-8"
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

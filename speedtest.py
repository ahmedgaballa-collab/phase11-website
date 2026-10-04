"""
speedtest.py — one-off measurement on the real NUCA site (read-only).
Answers: what actually makes reading a zone faster?
  A  normal: GET page + POST booked filter, one zone at a time (what we do today)
  B  parallel GETs with the session cookie (does the server serialize them?)
  C  parallel GET+POST WITHOUT the session cookie (separate server sessions)
  D  POST only, reusing the form saved from A (skip the GET) — 1 request per zone
  E  D in parallel without cookie
Waits for any running sync to finish (sync.lock), holds the lock while testing.
Run:  python speedtest.py
"""
import json, re, sys, time
from pathlib import Path
from urllib.parse import urlencode

import scraper
from bs4 import BeautifulSoup

scraper.load_dotenv()
HERE = Path(__file__).parent
LOCK = HERE / "sync.lock"
N = 8

JS = """
async ({reqs, cred, timeoutMs}) => {
  const one = async (r) => {
    const c = new AbortController(); const t = setTimeout(() => c.abort(), timeoutMs);
    const t0 = performance.now();
    try {
      const o = {method: r.method, credentials: cred, signal: c.signal};
      if (r.body !== null) { o.headers = {'Content-Type': 'application/x-www-form-urlencoded'}; o.body = r.body; }
      const res = await fetch(r.url, o); const text = await res.text();
      return {ok: true, status: res.status, text, ms: performance.now() - t0};
    } catch (e) { return {ok: false, error: String(e), ms: performance.now() - t0}; }
    finally { clearTimeout(t); }
  };
  return await Promise.all(reqs.map(one));
}
"""


def booked_summary(html):
    soup = BeautifulSoup(html, "html.parser")
    ok = soup.find(id=re.compile(r"grdPlots$")) is not None or "لا يوجد" in html
    return (len(scraper.parse_plots_table(soup)), scraper.max_page_number(soup)) if ok else None


def main():
    waited = 0
    while LOCK.exists() and time.time() - LOCK.stat().st_mtime < 50 * 60:
        if waited == 0:
            print("waiting for the running sync to finish…", flush=True)
        time.sleep(10); waited += 10
        if waited > 20 * 60:
            print("sync still running after 20 min — try again later"); return
    LOCK.write_text("speedtest")
    try:
        run_tests()
    finally:
        try: LOCK.unlink()
        except Exception: pass


def run_tests():
    zones = json.loads((HERE / "zones_cache.json").read_text(encoding="utf-8"))
    act = scraper.load_activity()
    zones.sort(key=lambda z: -float(act.get(str(z["zone_id"]), 0)))
    zones = zones[:N]
    paths = [f"/ar/ViewZone.aspx?ID={z['zone_id']}" for z in zones]
    s = scraper.Session(delay=0)
    page = s.page

    def many(reqs, cred):
        args = {"reqs": [{"url": p, "method": m, "body": urlencode(d) if d is not None else None} for m, p, d in reqs],
                "cred": cred, "timeoutMs": 60000}
        t0 = time.time(); r = page.evaluate(JS, args); return r, time.time() - t0

    try:
        # A — today's way, sequential with cookie
        t0 = time.time(); forms, base, per = [], [], []
        for p in paths:
            r, _ = many([("GET", p, None)], "same-origin"); g = r[0]
            form = scraper._booked_form(BeautifulSoup(g["text"], "html.parser")) if g["ok"] else None
            forms.append(form)
            r2, _ = many([("POST", p, form)], "same-origin") if form else ([{"ok": False}], 0)
            base.append(booked_summary(r2[0]["text"]) if r2[0]["ok"] else None)
            per.append(round(g.get("ms", 0) + r2[0].get("ms", 0)))
        ta = time.time() - t0
        print(f"\nA sequential GET+POST : {ta:5.1f}s for {N} zones  (per zone ms: {per})")
        print(f"  booked (rows on page 1, pages): {base}")

        # B — parallel GETs with the session cookie
        _, tseq = None, 0
        t0 = time.time()
        for p in paths: many([("GET", p, None)], "same-origin")
        tseq = time.time() - t0
        r, tpar = many([("GET", p, None) for p in paths], "same-origin")
        print(f"B GETs with cookie     : sequential {tseq:5.1f}s  vs parallel {tpar:5.1f}s  ok={sum(x['ok'] for x in r)}/{N}")

        # C — parallel GET + POST without cookie
        r, t1 = many([("GET", p, None) for p in paths], "omit")
        fc = [scraper._booked_form(BeautifulSoup(x["text"], "html.parser")) if x["ok"] else None for x in r]
        r2, t2 = many([("POST", p, f) for p, f in zip(paths, fc) if f], "omit")
        res = [booked_summary(x["text"]) if x["ok"] else None for x in r2]
        print(f"C parallel no-cookie   : {t1 + t2:5.1f}s   same as A: {res == base}   {res}")

        # D — POST only with the saved form (cookie), sequential
        t0 = time.time(); resd = []
        for p, f in zip(paths, forms):
            r, _ = many([("POST", p, f)], "same-origin")
            resd.append(booked_summary(r[0]["text"]) if r[0]["ok"] else None)
        print(f"D POST-only (saved form): {time.time() - t0:5.1f}s   same as A: {resd == base}   {resd}")

        # E — POST only, parallel, no cookie
        r, te = many([("POST", p, f) for p, f in zip(paths, forms)], "omit")
        rese = [booked_summary(x["text"]) if x["ok"] else None for x in r]
        print(f"E POST-only parallel no-cookie: {te:5.1f}s   same as A: {rese == base}   {rese}")
        print("\nابعت السطور اللي فوق (A لحد E) في سكرين.")
    finally:
        s.close()


if __name__ == "__main__":
    main()

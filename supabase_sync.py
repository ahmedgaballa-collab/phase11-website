"""
supabase_sync.py — يكتب نتيجة scraper.py في Supabase.

- plots        : كل قطعة بحالتها (upsert — بيكتب الجديد والمتغير بس)
- plot_changes : حجز جديد / قطعة اتفكت / قطعة جديدة / قطعة اختفت
- scrape_runs  : سجل لكل تشغيلة

المتغيرات المطلوبة (في ملف .env جنب السكربت — ماتترفعش على GitHub):
  SUPABASE_URL          https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  service_role key (من Project Settings → API)
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import requests

BIG_DOWN_PAYMENT_OVER = 40_000  # USD: لحد 40 ألف = مقدم صغير، أكتر = كبير
BATCH = 500
BASE_SITE = "https://lands.nuca.gov.eg"


def _num(v):
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s else None
    except ValueError:
        return None


def down_payment_type(down):
    n = _num(down)
    if n is None:
        return None
    return "big" if n > BIG_DOWN_PAYMENT_OVER else "low"


class Supa:
    def __init__(self, url, key):
        self.rest = url.rstrip("/") + "/rest/v1"
        self.h = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _check(self, r):
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {r.status_code}: {r.text[:500]}")
        return r

    def select_all(self, table, cols):
        out, offset, page = [], 0, 1000
        while True:
            r = self._check(requests.get(
                f"{self.rest}/{table}",
                params={"select": cols, "order": "id", "limit": page, "offset": offset},
                headers=self.h, timeout=60))
            rows = r.json()
            out.extend(rows)
            if len(rows) < page:
                return out
            offset += page

    def upsert(self, table, rows):
        for i in range(0, len(rows), BATCH):
            self._check(requests.post(
                f"{self.rest}/{table}", data=json.dumps(rows[i:i + BATCH], ensure_ascii=False).encode("utf-8"),
                headers={**self.h, "Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=120))

    def insert(self, table, rows):
        for i in range(0, len(rows), BATCH):
            self._check(requests.post(
                f"{self.rest}/{table}", data=json.dumps(rows[i:i + BATCH], ensure_ascii=False).encode("utf-8"),
                headers={**self.h, "Prefer": "return=minimal"}, timeout=120))

    def patch(self, table, match, values):
        self._check(requests.patch(
            f"{self.rest}/{table}", params=match,
            data=json.dumps(values, ensure_ascii=False).encode("utf-8"),
            headers={**self.h, "Prefer": "return=minimal"}, timeout=60))


def plot_id(p):
    # city|block|plot is NOT unique: two areas in the same city can reuse the
    # same block/plot numbers (seen 2026-09-28: 315 rows -> 297 ids). The NUCA
    # zone id makes it unique.
    return f"{p['city']}|{p['zone_id']}|{p['block']}|{p['plot']}"


def build_row(p, now_iso):
    status = "unavailable" if p["reserved"] else "available"
    row = {
        "id": plot_id(p),
        "city": p["city"],
        "project": p["project"],
        "zone_id": p["zone_id"],
        "zone_name": p.get("zone_name") or None,
        "district": p["project"],
        "block": p["block"],
        "plot_number": p["plot"],
        "area_sqm": _num(p["area"]),
        "down_payment": _num(p["down"]),
        "down_payment_type": down_payment_type(p["down"]),
        "currency": "USD",
        "status": status,
        "corner": bool(p["corner"]),
        "garden": bool(p["garden"]),
        "view": bool(p["view"]),
        "source_url": f"{BASE_SITE}/ar/ViewZone.aspx?ID={p['zone_id']}",
        "is_active": True,
    }
    row["content_hash"] = hashlib.md5(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    row["updated_at"] = now_iso
    return row


def sync(all_plots, started_at, zone_errors=0, full_run=True, log=print):
    """all_plots: list of dicts {key, city, project, zone_id, block, plot, area,
    down, corner, garden, view, reserved}. Returns a summary dict."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log("[supabase] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping")
        return None

    t0 = time.time()
    db = Supa(url, key)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = {r["id"]: r for r in db.select_all("plots", "id,status,is_active,content_hash")}
    first_sync = len(existing) == 0

    current, dupes = {}, []
    for p in all_plots:
        row = build_row(p, now_iso)
        if row["id"] in current:
            dupes.append(row["id"])
        current[row["id"]] = row
    if dupes:
        log(f"[supabase][warn] {len(dupes)} duplicate plot rows in this run "
            f"(same zone/block/plot seen twice), e.g. {dupes[:3]}")

    # Bootstrap: DB is empty or much smaller than this run (e.g. only a --test
    # city was synced before) — don't flood plot_changes with "new" rows.
    bootstrap = len(existing) < 0.5 * max(len(current), 1)

    new_rows, changed_rows, changes = [], [], []
    for pid, row in current.items():
        old = existing.get(pid)
        if old is None:
            row["first_seen_at"] = now_iso
            new_rows.append(row)
            # A plot we see for the first time is NOT a booking we observed —
            # never log it as a status change (would inflate "booked today").
            if not bootstrap:
                changes.append({"plot_id": pid, "type": "new", "from_value": None, "to_value": row["status"]})
            continue
        if old["content_hash"] != row["content_hash"] or not old.get("is_active", True):
            changed_rows.append(row)
        if old["status"] != row["status"]:
            changes.append({"plot_id": pid, "type": "status",
                            "from_value": old["status"], "to_value": row["status"]})

    # قطع اختفت من موقع الهيئة — بس في تشغيلة كاملة من غير أخطاء
    removed_ids = []
    if full_run and zone_errors == 0 and not first_sync:
        removed_ids = [pid for pid, r in existing.items()
                       if pid not in current and r.get("is_active", True)]
        if len(removed_ids) > 0.1 * max(len(existing), 1):
            log(f"[supabase][guard] {len(removed_ids)} plots would be marked removed "
                f"(>10%) — skipping removals this run")
            removed_ids = []

    db.upsert("plots", new_rows)
    db.upsert("plots", changed_rows)
    for i in range(0, len(removed_ids), 200):
        chunk = removed_ids[i:i + 200]
        ids = ",".join('"' + x.replace('"', '\\"') + '"' for x in chunk)
        db.patch("plots", {"id": f"in.({ids})"}, {"is_active": False, "updated_at": now_iso})
        changes += [{"plot_id": x, "type": "removed", "from_value": None, "to_value": None} for x in chunk]
    db.insert("plot_changes", changes)

    available = sum(1 for r in current.values() if r["status"] == "available")
    newly_booked = sum(1 for c in changes if c["type"] == "status" and c["to_value"] == "unavailable")
    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok" if zone_errors == 0 else "warn",
        "found": len(current),
        "available": available,
        "inserted": len(new_rows),
        "updated": len(changed_rows),
        "removed": len(removed_ids),
        "errors": zone_errors,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    db.insert("scrape_runs", [summary])
    log(f"[supabase] synced: {len(current)} plots, {len(new_rows)} new, "
        f"{len(changed_rows)} updated, {newly_booked} newly booked, "
        f"{len(removed_ids)} removed" + (" (bootstrap — new plots not logged)" if bootstrap else ""))
    return summary


def log_failed_run(started_at, message, log=print):
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return
    try:
        Supa(url, key).insert("scrape_runs", [{
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "fail", "error_message": message[:1000],
        }])
    except Exception as e:
        log(f"[supabase] could not log failed run: {e}")

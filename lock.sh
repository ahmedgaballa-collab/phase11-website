#!/usr/bin/env bash
# protect the data: booking history readable only for the last 8 days, answer only our own site, lower per-device limit
set -e
cd "$(dirname "$0")"
DB=agland
echo "== 1/3 booking history: public sees the last 8 days only"
sudo -u postgres psql -v ON_ERROR_STOP=1 -q -d $DB <<'SQL'
alter table public.plot_changes enable row level security;
do $$ declare p record; begin
  for p in select policyname from pg_policies where schemaname='public' and tablename='plot_changes' loop
    execute format('drop policy %I on public.plot_changes', p.policyname);
  end loop; end $$;
create policy read_recent on public.plot_changes for select to anon using (created_at >= now() - interval '8 days');
SQL
# keep it that way if setup.sh is ever run again
[ -d /home/ubuntu/selfhost ] && cp build_schema.py /home/ubuntu/selfhost/build_schema.py
echo "== 2/3 nginx: only our site + lower limit"
F=/etc/nginx/sites-available/agdata
cp $F $F.bak-lock
sed -i 's/rate=20r\/s;/rate=8r\/s;/' $F
if ! grep -q 'agok' $F; then
python3 - "$F" <<'PY'
import sys
f=sys.argv[1]; s=open(f).read()
a='    if ($request_method = OPTIONS) { return 204; }\n'
b=a+'''    set $agok 0;
    if ($http_origin ~* "^https://((www\\.)?ahmedgaballah\\.net|[a-z0-9-]*(ahmed|phase11)[a-z0-9-]*\\.vercel\\.app)$") { set $agok 1; }
    if ($http_referer ~* "^https://((www\\.)?ahmedgaballah\\.net|[a-z0-9-]*(ahmed|phase11)[a-z0-9-]*\\.vercel\\.app)/") { set $agok 1; }
    if ($uri ~ "^/rest/v1/scrape_runs") { set $agok 1; }
    if ($agok = 0) { return 403; }
'''
n=s.count(a)
if n==0: sys.exit("OPTIONS line not found")
s=s.replace(a,b); open(f,'w').write(s); print("origin check added in",n,"place(s)")
PY
fi
nginx -t && systemctl reload nginx
rm -rf /var/cache/nginx/agdata/* 2>/dev/null || true
sleep 1
echo "== 3/3 checks"
U=https://data.ahmedgaballah.net/rest/v1
O="Origin: https://www.ahmedgaballah.net"
echo -n "site works (200):            "; curl -s -o /dev/null -w "%{http_code}\n" -H "$O" "$U/v_stats_overall?select=booked"
echo -n "outsider blocked (403):      "; curl -s -o /dev/null -w "%{http_code}\n" "$U/plots?select=id&limit=1"
echo -n "old history hidden (0):      "; curl -s -H "$O" "$U/plot_changes?select=id&created_at=lt.2026-11-01&limit=5" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))"
echo -n "recent history visible (>0): "; curl -s -H "$O" "$U/plot_changes?select=id&limit=5" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))"
echo -n "full history still kept for you (t): "; sudo -u postgres psql -tAc "select count(*)>0 from plot_changes where created_at < '2026-11-01'" $DB
echo "===== DATA LOCKED ====="

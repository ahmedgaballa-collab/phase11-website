/* AG map viewer — shared by index.html and maps.html
   AGMaps.load()            -> Promise<items>  (maps/maps.json)
   AGMaps.find(city, area)  -> index or -1
   AGMaps.open(index, list?) opens the full-screen viewer (pinch / wheel / drag / double-tap zoom) */
(function () {
  var MANIFEST = "maps/maps.json?v=" + Math.floor(Date.now() / 600000), WA = "201009566779";
  var items = null, loading = null;

  function norm(s) {
    return String(s || "").normalize("NFKC").replace(/[إأآ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه")
      .replace(/[\s\-–_()\[\]]+/g, "");
  }
  function load() {
    if (items) return Promise.resolve(items);
    if (!loading) loading = fetch(MANIFEST, { cache: "force-cache" }).then(function (r) {
      if (!r.ok) throw new Error("maps " + r.status); return r.json();
    }).then(function (d) {
      items = d.items.map(function (it, i) { it.i = i; it._k = norm(it.city) + "|" + norm(it.area); return it; });
      return items;
    });
    return loading;
  }
  function find(city, area) {
    if (!items) return -1;
    var k = norm(city) + "|" + norm(area);
    for (var i = 0; i < items.length; i++) if (items[i]._k === k) return i;
    return -1;
  }

  /* ---------- styles ---------- */
  var css = "" +
    ".agmv{position:fixed;inset:0;z-index:9999;background:rgba(6,12,24,.985);display:flex;flex-direction:column;color:#F5F1E7;" +
    "font-family:'Tajawal',system-ui,sans-serif;touch-action:none;-webkit-user-select:none;user-select:none}" +
    ".agmv-top{display:flex;align-items:center;gap:10px;padding:10px 12px;padding-top:max(10px,env(safe-area-inset-top));background:rgba(6,12,24,.6)}" +
    ".agmv-t{min-width:0;flex:1}.agmv-t b{display:block;font-size:15px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
    ".agmv-t small{display:block;font-size:12px;color:#B7C2D9}" +
    ".agmv button,.agmv a.agmv-b{flex:none;height:40px;min-width:40px;padding:0 12px;border-radius:999px;border:1px solid rgba(255,255,255,.18);" +
    "background:rgba(255,255,255,.08);color:#fff;font:inherit;font-size:15px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none}" +
    ".agmv button:hover,.agmv a.agmv-b:hover{background:rgba(255,255,255,.16)}" +
    ".agmv a.agmv-wa{background:#1FA855;border-color:#1FA855}" +
    ".agmv-stage{position:relative;flex:1;overflow:hidden;cursor:grab}.agmv-stage.drag{cursor:grabbing}" +
    ".agmv-layer{position:absolute;left:0;top:0;transform-origin:0 0;will-change:transform;box-shadow:0 10px 40px rgba(0,0,0,.5);background:#fff}" +
    ".agmv-layer img{position:absolute;left:0;top:0;width:100%;height:100%;max-width:none}" +
    ".agmv-layer canvas{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}" +
    ".agmv-st{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:#F5F1E7;background:rgba(255,255,255,.08);border-radius:999px;padding:4px 10px}" +
    ".agmv-st i{width:13px;height:13px;border-radius:3px;background:#C8282C;display:inline-block;border:1px solid #fff}" +
    ".agmv.hide-res .agmv-layer canvas{display:none}" +
    ".agmv-lbl{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}" +
    ".agmv-bot{display:flex;align-items:center;justify-content:center;gap:8px;padding:10px 12px;padding-bottom:max(10px,env(safe-area-inset-bottom));background:rgba(6,12,24,.6);flex-wrap:wrap}" +
    ".agmv-hint{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);background:rgba(0,0,0,.6);padding:6px 12px;border-radius:999px;font-size:12.5px;pointer-events:none;transition:opacity .6s}" +
    ".agmv-load{position:absolute;top:12px;left:50%;transform:translateX(-50%);font-size:12.5px;background:rgba(0,0,0,.55);padding:4px 12px;border-radius:999px}" +
    "@media(max-width:560px){.agmv .agmv-txt{display:none}.agmv button,.agmv a.agmv-b{padding:0 11px}}";
  function injectCss() {
    if (document.getElementById("agmv-css")) return;
    var st = document.createElement("style"); st.id = "agmv-css"; st.textContent = css; document.head.appendChild(st);
  }

  /* ---------- viewer ---------- */
  var root, stage, layer, svg, stEl, resB, img, titleB, titleS, loadEl, hintEl, dlA, waA, prevB, nextB;
  var natW = 0, natH = 0, list = [], pos = 0, s = 1, tx = 0, ty = 0, minS = 1, pointers = {}, pinch = null, drag = null, lastTap = 0, pushed = false;

  function build() {
    injectCss();
    root = document.createElement("div"); root.className = "agmv"; root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true");
    root.innerHTML =
      '<div class="agmv-top"><button type="button" data-a="close" aria-label="إغلاق">✕</button>' +
      '<div class="agmv-t"><b></b><small></small></div>' +
      '<button type="button" data-a="prev" aria-label="الخريطة اللي قبلها">›</button><button type="button" data-a="next" aria-label="الخريطة اللي بعدها">‹</button></div>' +
      '<div class="agmv-stage"><div class="agmv-layer"><img alt=""><canvas></canvas></div><canvas class="agmv-lbl"></canvas><div class="agmv-load">جاري تحميل الخريطة بجودة عالية…</div><div class="agmv-hint">كبّر بصباعين أو دبل كليك</div></div>' +
      '<div class="agmv-bot"><button type="button" data-a="in" aria-label="تكبير">＋</button><button type="button" data-a="out" aria-label="تصغير">－</button>' +
      '<span class="agmv-st" hidden><i></i><span></span></span><button type="button" data-a="res" hidden>إخفاء المحجوز</button><button type="button" data-a="fit"><span>⤢</span><span class="agmv-txt">الخريطة كاملة</span></button>' +
      '<a class="agmv-b" data-a="dl" download><span>⬇</span><span class="agmv-txt">تحميل</span></a>' +
      '<a class="agmv-b agmv-wa" target="_blank" rel="noopener"><span>واتساب</span><span class="agmv-txt">— اسأل عن المنطقة دي</span></a></div>';
    document.body.appendChild(root);
    stage = root.querySelector(".agmv-stage"); layer = stage.querySelector(".agmv-layer"); img = layer.querySelector("img"); svg = layer.querySelector("canvas"); lbl = stage.querySelector(".agmv-lbl"); stEl = root.querySelector(".agmv-st"); resB = root.querySelector('[data-a="res"]');
    titleB = root.querySelector(".agmv-t b"); titleS = root.querySelector(".agmv-t small");
    loadEl = root.querySelector(".agmv-load"); hintEl = root.querySelector(".agmv-hint");
    dlA = root.querySelector('[data-a="dl"]'); waA = root.querySelector(".agmv-wa");
    prevB = root.querySelector('[data-a="prev"]'); nextB = root.querySelector('[data-a="next"]');
    root.addEventListener("click", function (e) {
      var b = e.target.closest("[data-a]"); if (!b) return;
      var a = b.getAttribute("data-a");
      if (a === "close") close();
      else if (a === "prev") go(-1);
      else if (a === "next") go(1);
      else if (a === "in") zoomAt(1.6, stage.clientWidth / 2, stage.clientHeight / 2);
      else if (a === "out") zoomAt(1 / 1.6, stage.clientWidth / 2, stage.clientHeight / 2);
      else if (a === "fit") fit();
      else if (a === "res") { var h = root.classList.toggle("hide-res"); resB.textContent = h ? "إظهار المحجوز" : "إخفاء المحجوز"; drawLabels(); }
    });
    stage.addEventListener("wheel", function (e) {
      e.preventDefault(); var r = stage.getBoundingClientRect();
      zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    stage.addEventListener("pointerdown", onDown);
    stage.addEventListener("pointermove", onMove);
    ["pointerup", "pointercancel", "pointerleave"].forEach(function (t) { stage.addEventListener(t, onUp); });
    window.addEventListener("resize", function () { if (root.style.display !== "none") fit(); });
    document.addEventListener("keydown", function (e) {
      if (!root || root.style.display === "none") return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") go(1);
      else if (e.key === "ArrowRight") go(-1);
      else if (e.key === "+" || e.key === "=") zoomAt(1.4, stage.clientWidth / 2, stage.clientHeight / 2);
      else if (e.key === "-") zoomAt(1 / 1.4, stage.clientWidth / 2, stage.clientHeight / 2);
    });
    window.addEventListener("popstate", function () { if (pushed) { pushed = false; hide(); } });
  }

  function apply() { layer.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + s + ")"; drawLabels(); }
  function clamp() {
    var W = stage.clientWidth, H = stage.clientHeight, w = natW * s, h = natH * s;
    tx = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, tx));
    ty = h <= H ? (H - h) / 2 : Math.min(0, Math.max(H - h, ty));
  }
  function fit() {
    if (!natW) return;
    var W = stage.clientWidth, H = stage.clientHeight;
    minS = Math.min(W / natW, H / natH); s = minS; clamp(); apply();
  }
  function zoomAt(f, x, y) {
    var ns = Math.max(minS, Math.min(Math.max(minS * 8, 4), s * f)); f = ns / s;
    tx = x - (x - tx) * f; ty = y - (y - ty) * f; s = ns; clamp(); apply();
  }
  function pt(e) { var r = stage.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  function onDown(e) {
    stage.setPointerCapture && stage.setPointerCapture(e.pointerId);
    pointers[e.pointerId] = pt(e);
    var ids = Object.keys(pointers);
    if (ids.length === 2) {
      var a = pointers[ids[0]], b = pointers[ids[1]];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), s: s, tx: tx, ty: ty, cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2 }; drag = null;
    } else if (ids.length === 1) {
      var now = Date.now(), p = pointers[ids[0]];
      if (now - lastTap < 300) { // double tap
        if (s > minS * 1.3) fit(); else zoomAt(2.6, p.x, p.y);
        lastTap = 0; return;
      }
      lastTap = now; drag = { x: p.x, y: p.y, tx: tx, ty: ty }; stage.classList.add("drag");
    }
    hintEl.style.opacity = 0;
  }
  function onMove(e) {
    if (!pointers[e.pointerId]) return;
    pointers[e.pointerId] = pt(e);
    var ids = Object.keys(pointers);
    if (pinch && ids.length >= 2) {
      var a = pointers[ids[0]], b = pointers[ids[1]], d = Math.hypot(a.x - b.x, a.y - b.y);
      var ns = Math.max(minS, Math.min(Math.max(minS * 8, 4), pinch.s * d / pinch.d)), f = ns / pinch.s;
      var cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
      tx = cx - (pinch.cx - pinch.tx) * f; ty = cy - (pinch.cy - pinch.ty) * f; s = ns; clamp(); apply();
    } else if (drag) {
      var p = pointers[ids[0]]; tx = drag.tx + p.x - drag.x; ty = drag.ty + p.y - drag.y; clamp(); apply();
    }
  }
  function onUp(e) {
    delete pointers[e.pointerId];
    if (Object.keys(pointers).length < 2) pinch = null;
    if (!Object.keys(pointers).length) { drag = null; stage.classList.remove("drag"); }
  }

    /* booked plots come live from Supabase (same source as /search and /dashboard, synced every 5 min); status.json is only a fallback */
  var STATUS = "status.json";
  var SB_URL = "https://mupmkecxgnickmbdvlno.supabase.co/rest/v1/";
  var SB_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im11cG1rZWN4Z25pY2ttYmR2bG5vIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk5OTY3NTAsImV4cCI6MjEwNTU3Mjc1MH0.Y5fHBT7cRfYB-CG2v674l_FH7kYs7ZQjE5bd5bQ0AzI";
  // Live plot data for a city from the DB: key "block|plot" -> {av: offered now, a: area m², dp: down payment}.
  // A plot is "available" only if the DB lists it as available & active; anything else is booked — this also covers
  // plots/zones NUCA already removed from the site (fully booked) that the DB never saw or marked inactive.
  var cityData = {};
  function sbCity(city) {
    var m = {};
    function page(from) {
      return fetch(SB_URL + "plots?select=block,plot_number,status,is_active,area_sqm,down_payment&order=id&city=eq." + encodeURIComponent(city), {
        cache: "no-store", headers: { apikey: SB_KEY, Authorization: "Bearer " + SB_KEY, "Range-Unit": "items", Range: from + "-" + (from + 999) } })
        .then(function (r) { if (!r.ok) throw new Error("sb " + r.status); return r.json(); })
        .then(function (rows) {
          rows.forEach(function (r) { var k = norm(r.block) + "|" + norm(r.plot_number), av = r.status === "available" && r.is_active !== false, o = m[k];
            if (!o || av) m[k] = { av: av || (o && o.av), a: r.area_sqm, dp: r.down_payment }; });
          return rows.length < 1000 || from > 50000 ? m : page(from + 1000);
        });
    }
    return page(0);
  }
  function fileKeys() { return fetch(STATUS + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) { return d.reserved || []; }); }
  // resolves to {booked(plot) -> bool, info(plot) -> {a, dp} | null}
  function loadStatus(it) {
    var c = it.city, hit = cityData[c];
    if (!hit || Date.now() - hit.at > 60000) {
      hit = cityData[c] = { at: Date.now(), p: sbCity(c).then(function (m) { return { db: m }; })
        .catch(function () { return fileKeys().then(function (list) { var s = new Set(); list.forEach(function (k) { var q = String(k).split("|"); if (q.length >= 3) s.add(norm(q[0]) + "|" + norm(q[q.length - 2]) + "|" + norm(q[q.length - 1])); }); return { res: s }; })
          .catch(function () { return { res: new Set() }; }); }) };
    }
    return hit.p.then(function (d) {
      function key(pl) { return norm(it.blocks[pl[1]]) + "|" + norm(pl[0]); }
      return {
        booked: function (pl) { if (d.db) { var o = d.db[key(pl)]; return !(o && o.av); } return d.res.has(norm(it.city) + "|" + key(pl)); },
        info: function (pl) { var o = d.db && d.db[key(pl)]; return o && o.a != null ? o : null; }
      };
    });
  }
  var resTimer = null, imgCache = {}, anaCache = {};
  function fullImg(src) {
    if (!imgCache[src]) imgCache[src] = new Promise(function (ok, no) { var im = new Image(); im.onload = function () { ok(im); }; im.onerror = no; im.src = src; });
    return imgCache[src];
  }

  /* One-time analysis of a map image: which pixels belong to which plot (region), and where each plot's printed
     label sits (the "holes" of text inside the region) with its size and angle. */
  function analyze(it, im) {
    if (anaCache[it.file]) return anaCache[it.file];
    var W = it.w, H = it.h, N = W * H;
    var t = document.createElement("canvas"); t.width = W; t.height = H; var tc = t.getContext("2d"); tc.drawImage(im, 0, 0, W, H);
    var src = tc.getImageData(0, 0, W, H).data, r = Math.max(8, it.r || 14);
    // the offered-plot yellow of this map = most common yellowish colour
    var hist = {}, Y = null, hb = 0;
    for (var i0 = 0; i0 < src.length; i0 += 4 * 7) { var R0 = src[i0], G0 = src[i0 + 1], B0 = src[i0 + 2];
      if (R0 > 190 && G0 > 140 && B0 < G0 - 40 && R0 - G0 < 90) { var hk = (R0 >> 3) + "," + (G0 >> 3) + "," + (B0 >> 3); hist[hk] = (hist[hk] || 0) + 1; if (hist[hk] > hb) { hb = hist[hk]; Y = [R0, G0, B0]; } } }
    var A = { W: W, H: H, Y: Y, own: new Int32Array(N).fill(-1), reg: [], lab: [] };
    if (!Y) return (anaCache[it.file] = A);
    function plotCol(j) { var dr = src[j] - Y[0], dg = src[j + 1] - Y[1], db = src[j + 2] - Y[2];
      return dr * dr + dg * dg + db * db < 2500 || (src[j] > 175 && src[j + 1] > 120 && src[j + 2] < src[j + 1] - 35 && src[j] - src[j + 1] < 100); }
    var own = A.own, P = it.plots, cap = Math.round(Math.max(r * r * 80, 40000)), B6 = Math.max(r * 6, 170);
    // pass 1: flood each plot from yellow pixels near its label, never crossing to pixels nearer another plot's centre
    P.forEach(function (p, id) {
      var cx = Math.round(p[2]), cy = Math.round(p[3]), rr = Math.round(Math.max(r * 1.2, 34)), cands = [];
      for (var dy = -rr; dy <= rr; dy++) for (var dx = -rr; dx <= rr; dx++) { var x = cx + dx, y = cy + dy;
        if (x >= 0 && y >= 0 && x < W && y < H && plotCol((y * W + x) * 4)) cands.push([dx * dx + dy * dy, y * W + x]); }
      cands.sort(function (u, v) { return u[0] - v[0]; });
      var nb = P.filter(function (o) { return o !== p && Math.abs(o[2] - cx) < B6 * 1.4 && Math.abs(o[3] - cy) < B6 * 1.4; });
      function mine(nx, ny) { var d0 = (nx - cx) * (nx - cx) + (ny - cy) * (ny - cy); for (var k = 0; k < nb.length; k++) { var ex = nx - nb[k][2], ey = ny - nb[k][3]; if (ex * ex + ey * ey < d0) return false; } return true; }
      var px = [], big = Math.max(r * r * 1.5, 600), tries = 0;
      for (var ci = 0; ci < cands.length && px.length < big && tries < 40; ci++) {
        var seed = cands[ci][1]; if (own[seed] !== -1 || !mine(seed % W, (seed / W) | 0)) continue; tries++;
        var st = [seed]; own[seed] = id;
        while (st.length && px.length < cap) { var q = st.pop(); px.push(q); var qx = q % W;
          for (var k = 0; k < 4; k++) { var n = k === 0 ? q - 1 : k === 1 ? q + 1 : k === 2 ? q - W : q + W;
            if ((k === 0 && qx === 0) || (k === 1 && qx === W - 1) || n < 0 || n >= N || own[n] !== -1) continue;
            var nx = n % W, ny = (n / W) | 0; if (nx < cx - B6 || nx > cx + B6 || ny < cy - B6 || ny > cy + B6 || !mine(nx, ny) || !plotCol(n * 4)) continue;
            own[n] = id; st.push(n); } }
      }
      if (px.length >= cap) { px.forEach(function (q) { own[q] = -2; }); px = null; }
      A.reg[id] = px;
    });
    // pass 2: unclaimed plot-coloured pixels next to a plot's region join that plot (corners cut off by the nearest-centre rule)
    P.forEach(function (p, id) { var px = A.reg[id]; if (!px) return; var st = px.slice(), lim = px.length * 2 + 2000;
      while (st.length && px.length < lim) { var q = st.pop(), qx = q % W;
        for (var k = 0; k < 4; k++) { var n = k === 0 ? q - 1 : k === 1 ? q + 1 : k === 2 ? q - W : q + W;
          if ((k === 0 && qx === 0) || (k === 1 && qx === W - 1) || n < 0 || n >= N || own[n] !== -1 || !plotCol(n * 4)) continue;
          own[n] = id; px.push(n); st.push(n); } } });
    // label = holes (text) inside each region: pixels enclosed by the same plot on all 4 sides
    P.forEach(function (p, id) { var px = A.reg[id]; if (!px || !px.length) return;
      var bx0 = W, bx1 = 0, by0 = H, by1 = 0; px.forEach(function (q) { var x = q % W, y = (q / W) | 0; if (x < bx0) bx0 = x; if (x > bx1) bx1 = x; if (y < by0) by0 = y; if (y > by1) by1 = y; });
      var cx = Math.round(p[2]), cy = Math.round(p[3]), lim = Math.round(Math.max(r * 2.5, 40)), holes = [];
      for (var y = Math.max(by0, cy - lim); y <= Math.min(by1, cy + lim); y++) for (var x = Math.max(bx0, cx - lim); x <= Math.min(bx1, cx + lim); x++) {
        var q = y * W + x; if (own[q] === id) continue; var k, l = 0, rt = 0, u = 0, d = 0;
        for (k = 1; k < 40 && x - k >= bx0; k++) { var o1 = own[q - k]; if (o1 === id) { l = 1; break; } if (o1 >= 0) break; }
        if (!l) continue;
        for (k = 1; k < 40 && x + k <= bx1; k++) { var o2 = own[q + k]; if (o2 === id) { rt = 1; break; } if (o2 >= 0) break; }
        if (!rt) continue;
        for (k = 1; k < 40 && y - k >= by0; k++) { var o3 = own[q - k * W]; if (o3 === id) { u = 1; break; } if (o3 >= 0) break; }
        if (!u) continue;
        for (k = 1; k < 40 && y + k <= by1; k++) { var o4 = own[q + k * W]; if (o4 === id) { d = 1; break; } if (o4 >= 0) break; }
        if (d) holes.push(q);
      }
      if (holes.length < 25) return;
      // text angle: the rotation that makes the 3 printed lines sharpest (max variance of the row histogram)
      var xs = holes.map(function (q) { return q % W; }), ys = holes.map(function (q) { return (q / W) | 0; }), mx = 0, my = 0;
      for (var i = 0; i < xs.length; i++) { mx += xs[i]; my += ys[i]; } mx /= xs.length; my /= ys.length;
      var best = -1, bestA = 0;
      for (var ang = -90; ang < 90; ang += 3) { var a = ang * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a), h = {}, sum = 0, s2 = 0, nb = 0;
        for (i = 0; i < xs.length; i++) { var v = Math.round((-(xs[i] - mx) * sa + (ys[i] - my) * ca) / 1.5); h[v] = (h[v] || 0) + 1; }
        for (var kk in h) { sum += h[kk]; s2 += h[kk] * h[kk]; nb++; }
        var sc = s2 / sum - (ang === 0 ? -1e-9 : 0); if (sc > best + 1e-9) { best = sc; bestA = a; } }
      // prefer horizontal unless another angle is clearly better
      var a0 = 0, c0 = 1, s0 = 0, h0 = {}, t0 = 0;
      for (i = 0; i < xs.length; i++) { var v0 = Math.round((ys[i] - my) / 1.5); h0[v0] = (h0[v0] || 0) + 1; }
      for (var k0 in h0) t0 += h0[k0] * h0[k0]; t0 /= xs.length;
      var ang2 = best > t0 * 1.5 ? bestA : a0, ca2 = Math.cos(ang2), sa2 = Math.sin(ang2);
      var us = [], vs = [];
      for (i = 0; i < xs.length; i++) { var dx = xs[i] - mx, dy = ys[i] - my; us.push(dx * ca2 + dy * sa2); vs.push(-dx * sa2 + dy * ca2); }
      us.sort(function (p1, p2) { return p1 - p2; }); vs.sort(function (p1, p2) { return p1 - p2; });
      // percentile extents: a few stray non-text pixels must not inflate the label box
      var pc = function (arr, f) { return arr[Math.min(arr.length - 1, Math.max(0, Math.round(f * (arr.length - 1))))]; };
      var u0 = pc(us, .03), u1 = pc(us, .97), v0_ = pc(vs, .03), v1 = pc(vs, .97);
      var uc = (u0 + u1) / 2, vc = (v0_ + v1) / 2;
      A.lab[id] = { x: mx + uc * ca2 - vc * sa2, y: my + uc * sa2 + vc * ca2, a: ang2, f: (v1 - v0_) / 2.45, w: u1 - u0, holes: holes };
    });
    // one text size per map (printed labels share a size); outliers come from holes that are not just text
    var fs = A.lab.filter(Boolean).map(function (L) { return L.f; }).sort(function (u, v) { return u - v; }), fm = fs.length ? fs[fs.length >> 1] : 0;
    A.lab.forEach(function (L) { if (L) L.f = Math.max(fm * .8, Math.min(fm * 1.15, L.f)); });
    return (anaCache[it.file] = A);
  }

  var LBL = [], mctx = document.createElement("canvas").getContext("2d");   // labels currently drawn crisp on the overlay: {x, y, a, f, t:[area, no, dp], b: booked}
  function fmt(n) { n = Number(n); if (!isFinite(n)) return ""; return n.toLocaleString("en-US", { maximumFractionDigits: 2 }); }
  function paintMap(it, im, st) {
    var A = analyze(it, im), W = A.W, H = A.H; svg.width = W; svg.height = H; LBL = [];
    var ctx = svg.getContext("2d"); ctx.clearRect(0, 0, W, H);
    if (!A.Y) return 0;
    var out = ctx.createImageData(W, H), od = out.data, Y = A.Y, n = 0;
    function put(q, c) { var j = q * 4; od[j] = c[0]; od[j + 1] = c[1]; od[j + 2] = c[2]; od[j + 3] = 255; }
    var RED = [200, 40, 44], WH = [255, 255, 255];
    it.plots.forEach(function (p, id) {
      var b = st.booked(p), inf = st.info(p), L = A.lab[id], px = A.reg[id]; if (b) n++;
      if (b) { if (px && px.length) px.forEach(function (q) { put(q, RED); });
        else { ctx.fillStyle = "rgba(200,40,44,.85)"; ctx.beginPath(); ctx.arc(p[2], p[3], Math.max(8, it.r || 14) * .9, 0, 7); ctx.fill(); } }
      if (!L) return;
      if (inf) { // printed text replaced by crisp live text
        L.holes.forEach(function (q) { put(q, b ? RED : Y); });
        if (!b && px) { // flatten the plot colour under the old text (its soft edges would show as a ghost)
          var ca = Math.cos(L.a), sa = Math.sin(L.a), hw = L.w / 2 + 3, hh = L.f * 1.35 + 3;
          var near = new Set(); L.holes.forEach(function (q) { for (var ey = -2; ey <= 2; ey++) for (var ex = -2; ex <= 2; ex++) near.add(q + ey * W + ex); });
          px.forEach(function (q) { if (!near.has(q)) return; var dx = q % W - L.x, dy = ((q / W) | 0) - L.y; if (Math.abs(dx * ca + dy * sa) <= hw && Math.abs(-dx * sa + dy * ca) <= hh) put(q, Y); }); }
        var tt = [fmt(inf.a), String(p[0]), fmt(inf.dp)], ff = L.f;
        // never wider than the printed label was (it was fitted to the plot)
        mctx.font = "bold 78px Helvetica, Arial, sans-serif"; var wmax = Math.max(mctx.measureText(tt[0]).width, mctx.measureText(tt[2]).width) / 100;
        mctx.font = "bold 100px Helvetica, Arial, sans-serif"; wmax = Math.max(wmax, mctx.measureText(tt[1]).width / 100);
        if (wmax > 0 && ff * wmax > L.w * 1.08) ff = L.w * 1.08 / wmax;
        LBL.push({ x: L.x, y: L.y, a: L.a, f: ff, b: b, t: tt });
      } else if (b) L.holes.forEach(function (q) { put(q, WH); });
    });
    var o = document.createElement("canvas"); o.width = W; o.height = H; o.getContext("2d").putImageData(out, 0, 0); ctx.drawImage(o, 0, 0);
    drawLabels();
    return n;
  }
  // crisp labels: redrawn in screen pixels on every pan/zoom, so they stay sharp at any zoom
  var lbl = null;
  function drawLabels() {
    if (!lbl || !root) return;
    var dpr = window.devicePixelRatio || 1, cw = stage.clientWidth, ch = stage.clientHeight;
    if (lbl.width !== Math.round(cw * dpr) || lbl.height !== Math.round(ch * dpr)) { lbl.width = Math.round(cw * dpr); lbl.height = Math.round(ch * dpr); }
    var c = lbl.getContext("2d"); c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, lbl.width, lbl.height);
    if (root.classList.contains("hide-res") || !LBL.length) return;
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.textAlign = "center"; c.textBaseline = "middle";
    for (var i = 0; i < LBL.length; i++) { var L = LBL[i], f = L.f * s; if (f < 2.2) continue;
      var x = tx + L.x * s, y = ty + L.y * s, m = f * 3; if (x < -m || y < -m || x > cw + m || y > ch + m) continue;
      c.save(); c.translate(x, y); if (L.a) c.rotate(L.a);
      c.fillStyle = L.b ? "#fff" : "#16325C"; c.font = "bold " + (f * .78).toFixed(2) + "px Helvetica, Arial, sans-serif"; c.fillText(L.t[0], 0, -f * .95);
      c.fillStyle = L.b ? "#fff" : "#C8102E"; c.font = "bold " + f.toFixed(2) + "px Helvetica, Arial, sans-serif"; c.fillText(L.t[1], 0, 0);
      c.fillStyle = L.b ? "#fff" : "#16325C"; c.font = "bold " + (f * .78).toFixed(2) + "px Helvetica, Arial, sans-serif"; c.fillText(L.t[2], 0, f * .95);
      c.restore(); }
  }
  function drawReserved(it, fresh) {
    if (!fresh) { svg.width = 1; svg.height = 1; LBL = []; drawLabels(); stEl.hidden = true; resB.hidden = true; }
    clearInterval(resTimer);
    if (!it.plots || !it.plots.length) return;
    // keep the map live while it's open: re-check every 2 minutes
    resTimer = setInterval(function () { if (list[pos] === it && root.style.display !== "none") { delete cityData[it.city]; drawReserved(it, true); } else clearInterval(resTimer); }, 120000);
    Promise.all([loadStatus(it), fullImg(it.file)]).then(function (v) {
      if (list[pos] !== it) return;
      var n = paintMap(it, v[1], v[0]);
      stEl.hidden = false; resB.hidden = !n;
      stEl.querySelector("span").textContent = n ? ("محجوز " + n + " من " + it.plots.length + " · متاح " + (it.plots.length - n)) : ("كل القطع متاحة (" + it.plots.length + ")");
    }).catch(function (e) { console.warn("map overlay", e); });
  }

  function show() {
    var it = list[pos];
    titleB.textContent = it.area; titleS.textContent = it.city + (list.length > 1 ? "  ·  " + (pos + 1) + " من " + list.length : "");
    prevB.style.visibility = nextB.style.visibility = list.length > 1 ? "visible" : "hidden";
    dlA.href = it.file; dlA.setAttribute("download", (it.city + " - " + it.area).replace(/[\\/:*?"<>|]/g, "") + ".webp");
    waA.href = "https://wa.me/" + WA + "?text=" + encodeURIComponent("مساء الخير، عايز أستفسر عن قطع " + it.area + " في " + it.city + " (المرحلة 11)");
    loadEl.style.display = "block"; hintEl.style.opacity = 1;
    setTimeout(function () { hintEl.style.opacity = 0; }, 2500);
    // low-res thumb first so something shows instantly, then the sharp file
    img.onload = null; img.src = it.thumb; layer.style.width = it.w + "px"; layer.style.height = it.h + "px";
    drawReserved(it);
    natW = it.w; natH = it.h;
    fit();
    var hi = new Image(), want = it.file;
    hi.onload = function () { if (list[pos] && list[pos].file === want) { img.src = want; loadEl.style.display = "none"; } };
    hi.onerror = function () { loadEl.textContent = "مقدرناش نحمّل الخريطة — جرّب تاني"; };
    hi.src = want;
  }
  function go(d) { if (list.length < 2) return; pos = (pos + d + list.length) % list.length; show(); }
  function open(index, subset) {
    return load().then(function (all) {
      if (!root) build();
      list = subset && subset.length ? subset : all;
      pos = Math.max(0, list.indexOf(all[index]));
      if (pos < 0) pos = 0;
      root.style.display = "flex"; document.documentElement.style.overflow = "hidden";
      if (!pushed) { try { history.pushState({ agmv: 1 }, ""); pushed = true; } catch (e) {} }
      show();
    });
  }
  function hide() { if (root) root.style.display = "none"; document.documentElement.style.overflow = ""; }
  function close() { if (pushed) { history.back(); } else hide(); }

  window.AGMaps = { load: load, find: find, open: open, norm: norm };
})();

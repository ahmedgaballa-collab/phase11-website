/* AG map viewer — shared by index.html and maps.html
   AGMaps.load()            -> Promise<items>  (maps/maps.json)
   AGMaps.find(city, area)  -> index or -1
   AGMaps.open(index, list?) opens the full-screen viewer (pinch / wheel / drag / double-tap zoom) */
(function () {
  var MANIFEST = "maps/maps.json", WA = "201009566779";
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
      '<div class="agmv-stage"><div class="agmv-layer"><img alt=""><canvas></canvas></div><div class="agmv-load">جاري تحميل الخريطة بجودة عالية…</div><div class="agmv-hint">كبّر بصباعين أو دبل كليك</div></div>' +
      '<div class="agmv-bot"><button type="button" data-a="in" aria-label="تكبير">＋</button><button type="button" data-a="out" aria-label="تصغير">－</button>' +
      '<span class="agmv-st" hidden><i></i><span></span></span><button type="button" data-a="res" hidden>إخفاء المحجوز</button><button type="button" data-a="fit"><span>⤢</span><span class="agmv-txt">الخريطة كاملة</span></button>' +
      '<a class="agmv-b" data-a="dl" download><span>⬇</span><span class="agmv-txt">تحميل</span></a>' +
      '<a class="agmv-b agmv-wa" target="_blank" rel="noopener"><span>واتساب</span><span class="agmv-txt">— اسأل عن المنطقة دي</span></a></div>';
    document.body.appendChild(root);
    stage = root.querySelector(".agmv-stage"); layer = stage.querySelector(".agmv-layer"); img = layer.querySelector("img"); svg = layer.querySelector("canvas"); stEl = root.querySelector(".agmv-st"); resB = root.querySelector('[data-a="res"]');
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
      else if (a === "res") { var h = root.classList.toggle("hide-res"); resB.textContent = h ? "إظهار المحجوز" : "إخفاء المحجوز"; }
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

  function apply() { layer.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + s + ")"; }
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
    var ns = Math.max(minS, Math.min(minS * 8, s * f)); f = ns / s;
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
      var ns = Math.max(minS, Math.min(minS * 8, pinch.s * d / pinch.d)), f = ns / pinch.s;
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

  /* ---------- live booked overlay (status.json, refreshed every 5 min by the sync job) ---------- */
  var STATUS = "status.json", resSet = null, resAt = 0, resLoading = null;
  function loadStatus() {
    if (resSet && Date.now() - resAt < 120000) return Promise.resolve(resSet);
    if (!resLoading) resLoading = fetch(STATUS + "?t=" + Math.floor(Date.now() / 60000), { cache: "no-store" }).then(function (r) { return r.json(); })
      .then(function (d) { var s = new Set(); (d.reserved || []).forEach(function (k) { var p = String(k).split("|"); if (p.length >= 3) s.add(norm(p[0]) + "|" + norm(p[p.length - 2]) + "|" + norm(p[p.length - 1])); });
        resSet = s; resAt = Date.now(); resLoading = null; return s; })
      .catch(function () { resLoading = null; return resSet || new Set(); });
    return resLoading;
  }
  var resTimer = null, imgCache = {};
  function fullImg(src) {
    if (!imgCache[src]) imgCache[src] = new Promise(function (ok, no) { var im = new Image(); im.onload = function () { ok(im); }; im.onerror = no; im.src = src; });
    return imgCache[src];
  }
  // colour the booked plots themselves: flood-fill each plot's yellow area from its centre
  function paintBooked(it, im, hits) {
    var W = it.w, H = it.h; svg.width = W; svg.height = H;
    var ctx = svg.getContext("2d"); ctx.clearRect(0, 0, W, H);
    if (!hits.length) return;
    var t = document.createElement("canvas"); t.width = W; t.height = H; var tc = t.getContext("2d"); tc.drawImage(im, 0, 0, W, H);
    var src = tc.getImageData(0, 0, W, H).data, out = ctx.createImageData(W, H), od = out.data, seen = new Uint8Array(W * H);
    var r = Math.max(8, it.r || 14);
    hits.forEach(function (p) {
      var cx = Math.round(p[2]), cy = Math.round(p[3]), seed = -1, best = 1e9;
      // seed = nearest yellow (offered-plot colour) pixel around the centre (skips the text)
      for (var dy = -Math.round(r * .6); dy <= r * .6; dy++) for (var dx = -Math.round(r * .6); dx <= r * .6; dx++) {
        var x = cx + dx, y = cy + dy; if (x < 0 || y < 0 || x >= W || y >= H) continue; var i = (y * W + x) * 4;
        var R = src[i], G = src[i + 1], B = src[i + 2], mx = Math.max(R, G, B), mn = Math.min(R, G, B);
        if (R > 190 && G > 140 && B < G - 40 && R - G < 90) { var d = dx * dx + dy * dy; if (d < best) { best = d; seed = y * W + x; } }
      }
      if (seed < 0) { ctx.fillStyle = "rgba(214,48,49,.55)"; ctx.beginPath(); ctx.arc(cx, cy, r * .7, 0, 7); ctx.fill(); return; }
      var s4 = seed * 4, sr = src[s4], sg = src[s4 + 1], sb = src[s4 + 2], cap = Math.round(r * r * 30), st = [seed], px = [], x0 = cx - r * 4, x1 = cx + r * 4, y0 = cy - r * 4, y1 = cy + r * 4;
      seen[seed] = 1;
      while (st.length && px.length < cap) {
        var q = st.pop(); px.push(q); var qx = q % W, qy = (q / W) | 0;
        [q - 1, q + 1, q - W, q + W].forEach(function (n, k) {
          if (k === 0 && qx === 0 || k === 1 && qx === W - 1 || n < 0 || n >= W * H || seen[n]) return;
          var nx = n % W, ny = (n / W) | 0; if (nx < x0 || nx > x1 || ny < y0 || ny > y1) return;
          var j = n * 4, dr = src[j] - sr, dg = src[j + 1] - sg, db = src[j + 2] - sb;
          if (dr * dr + dg * dg + db * db < 3200) { seen[n] = 1; st.push(n); }
        });
      }
      if (px.length >= cap) { px.forEach(function (q) { seen[q] = 0; }); ctx.fillStyle = "rgba(214,48,49,.55)"; ctx.beginPath(); ctx.arc(cx, cy, r * .7, 0, 7); ctx.fill(); return; }
      // booked plot -> solid red, and the text inside it -> white so it stays readable
      var bx0 = W, bx1 = 0, by0 = H, by1 = 0, mark = {};
      px.forEach(function (q) { var j = q * 4, qx = q % W, qy = (q / W) | 0; od[j] = 200; od[j + 1] = 40; od[j + 2] = 44; od[j + 3] = 255; mark[q] = 1;
        if (qx < bx0) bx0 = qx; if (qx > bx1) bx1 = qx; if (qy < by0) by0 = qy; if (qy > by1) by1 = qy; });
      for (var yy = by0; yy <= by1; yy++) { var L = -1, Rr = -1; for (var xx = bx0; xx <= bx1; xx++) if (mark[yy * W + xx]) { if (L < 0) L = xx; Rr = xx; }
        for (xx = L + 1; xx < Rr; xx++) { var q2 = yy * W + xx; if (mark[q2]) continue; var up = 0, dn = 0;
          for (var k2 = yy - 1; k2 >= by0; k2--) if (mark[k2 * W + xx]) { up = 1; break; } for (k2 = yy + 1; k2 <= by1; k2++) if (mark[k2 * W + xx]) { dn = 1; break; }
          if (up && dn) { var j2 = q2 * 4, lum = src[j2] * .3 + src[j2 + 1] * .59 + src[j2 + 2] * .11, a = Math.max(0, Math.min(255, (230 - lum) * 2.2));
            od[j2] = 200 + (55 * a / 255); od[j2 + 1] = 40 + (215 * a / 255); od[j2 + 2] = 44 + (211 * a / 255); od[j2 + 3] = 255; } } }
    });
    var o = document.createElement("canvas"); o.width = W; o.height = H; o.getContext("2d").putImageData(out, 0, 0); ctx.drawImage(o, 0, 0);
  }
  function drawReserved(it, fresh) {
    if (!fresh) { svg.width = 1; svg.height = 1; stEl.hidden = true; resB.hidden = true; }
    clearInterval(resTimer);
    if (!it.plots || !it.plots.length) return;
    // keep the map live while it's open: re-check status.json every 2 minutes
    resTimer = setInterval(function () { if (list[pos] === it && root.style.display !== "none") { resAt = 0; drawReserved(it, true); } else clearInterval(resTimer); }, 120000);
    Promise.all([loadStatus(), fullImg(it.file)]).then(function (v) {
      var set = v[0]; if (list[pos] !== it) return;
      var c = norm(it.city), hits = it.plots.filter(function (p) { return set.has(c + "|" + norm(it.blocks[p[1]]) + "|" + norm(p[0])); }), n = hits.length;
      paintBooked(it, v[1], hits);
      stEl.hidden = false; resB.hidden = !n;
      stEl.querySelector("span").textContent = n ? ("محجوز " + n + " من " + it.plots.length + " · متاح " + (it.plots.length - n)) : ("كل القطع متاحة (" + it.plots.length + ")");
    }).catch(function () {});
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

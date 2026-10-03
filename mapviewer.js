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
    ".agmv-stage img{position:absolute;left:0;top:0;transform-origin:0 0;max-width:none;will-change:transform;background:#fff;box-shadow:0 10px 40px rgba(0,0,0,.5)}" +
    ".agmv-bot{display:flex;align-items:center;justify-content:center;gap:8px;padding:10px 12px;padding-bottom:max(10px,env(safe-area-inset-bottom));background:rgba(6,12,24,.6);flex-wrap:wrap}" +
    ".agmv-hint{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);background:rgba(0,0,0,.6);padding:6px 12px;border-radius:999px;font-size:12.5px;pointer-events:none;transition:opacity .6s}" +
    ".agmv-load{position:absolute;top:12px;left:50%;transform:translateX(-50%);font-size:12.5px;background:rgba(0,0,0,.55);padding:4px 12px;border-radius:999px}" +
    "@media(max-width:560px){.agmv .agmv-txt{display:none}.agmv button,.agmv a.agmv-b{padding:0 11px}}";
  function injectCss() {
    if (document.getElementById("agmv-css")) return;
    var st = document.createElement("style"); st.id = "agmv-css"; st.textContent = css; document.head.appendChild(st);
  }

  /* ---------- viewer ---------- */
  var root, stage, img, titleB, titleS, loadEl, hintEl, dlA, waA, prevB, nextB;
  var natW = 0, natH = 0, list = [], pos = 0, s = 1, tx = 0, ty = 0, minS = 1, pointers = {}, pinch = null, drag = null, lastTap = 0, pushed = false;

  function build() {
    injectCss();
    root = document.createElement("div"); root.className = "agmv"; root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true");
    root.innerHTML =
      '<div class="agmv-top"><button type="button" data-a="close" aria-label="إغلاق">✕</button>' +
      '<div class="agmv-t"><b></b><small></small></div>' +
      '<button type="button" data-a="prev" aria-label="الخريطة اللي قبلها">›</button><button type="button" data-a="next" aria-label="الخريطة اللي بعدها">‹</button></div>' +
      '<div class="agmv-stage"><img alt=""><div class="agmv-load">جاري تحميل الخريطة بجودة عالية…</div><div class="agmv-hint">كبّر بصباعين أو دبل كليك</div></div>' +
      '<div class="agmv-bot"><button type="button" data-a="in" aria-label="تكبير">＋</button><button type="button" data-a="out" aria-label="تصغير">－</button>' +
      '<button type="button" data-a="fit"><span>⤢</span><span class="agmv-txt">الخريطة كاملة</span></button>' +
      '<a class="agmv-b" data-a="dl" download><span>⬇</span><span class="agmv-txt">تحميل</span></a>' +
      '<a class="agmv-b agmv-wa" target="_blank" rel="noopener"><span>واتساب</span><span class="agmv-txt">— اسأل عن المنطقة دي</span></a></div>';
    document.body.appendChild(root);
    stage = root.querySelector(".agmv-stage"); img = stage.querySelector("img");
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

  function apply() { img.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + s + ")"; }
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

  function show() {
    var it = list[pos];
    titleB.textContent = it.area; titleS.textContent = it.city + (list.length > 1 ? "  ·  " + (pos + 1) + " من " + list.length : "");
    prevB.style.visibility = nextB.style.visibility = list.length > 1 ? "visible" : "hidden";
    dlA.href = it.file; dlA.setAttribute("download", (it.city + " - " + it.area).replace(/[\\/:*?"<>|]/g, "") + ".webp");
    waA.href = "https://wa.me/" + WA + "?text=" + encodeURIComponent("مساء الخير، عايز أستفسر عن قطع " + it.area + " في " + it.city + " (المرحلة 11)");
    loadEl.style.display = "block"; hintEl.style.opacity = 1;
    setTimeout(function () { hintEl.style.opacity = 0; }, 2500);
    // low-res thumb first so something shows instantly, then the sharp file
    img.onload = null; img.src = it.thumb; img.style.width = it.w + "px"; img.style.height = it.h + "px";
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

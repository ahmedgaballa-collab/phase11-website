/* ag-ui.js — shared touches for every page (2026-11-08):
   1) the page menu fits on a phone (no hidden "الخرائط" / "عنّي")
   2) a floating WhatsApp button on every page (search.html already has its own) */
(function () {
  var PHONE = "201009566779";
  var MSG = "مساء الخير، عايز أستشير حضرتك في أراضي المرحلة 11";

  var css = document.createElement("style");
  css.textContent =
    "@media (max-width:560px){" +
    ".nav,.site-nav{gap:2px!important;padding:3px!important}" +
    ".nav a,.site-nav a{flex:1 1 auto!important;padding:7px 6px!important;font-size:12.5px!important;text-align:center}" +
    "}" +
    ".ag-wa{position:fixed;bottom:18px;left:18px;z-index:40;width:56px;height:56px;border-radius:50%;" +
    "background:#25D366;color:#fff;display:flex;align-items:center;justify-content:center;" +
    "box-shadow:0 8px 22px rgba(0,0,0,.28);text-decoration:none;transition:bottom .2s,transform .15s}" +
    ".ag-wa:hover{transform:scale(1.06)}" +
    ".ag-wa svg{width:30px;height:30px}" +
    ".ag-wa::after{content:'';position:absolute;inset:0;border-radius:50%;box-shadow:0 0 0 0 rgba(37,211,102,.55);animation:agwa 2.4s infinite}" +
    "@keyframes agwa{70%{box-shadow:0 0 0 14px rgba(37,211,102,0)}100%{box-shadow:0 0 0 0 rgba(37,211,102,0)}}" +
    "@media print{.ag-wa{display:none}}";
  document.head.appendChild(css);

  var mq = window.matchMedia("(max-width:560px)");
  function labels() {
    document.querySelectorAll('.nav a[href="/search"], .site-nav a[href="/search"]').forEach(function (a) {
      if (!a.dataset.full) a.dataset.full = a.textContent;
      a.textContent = mq.matches ? "البحث" : a.dataset.full;
    });
    var on = document.querySelector(".nav a.on, .site-nav a.on");
    if (on && on.scrollIntoView) try { on.scrollIntoView({ block: "nearest", inline: "center" }); } catch (e) {}
  }
  labels();
  if (mq.addEventListener) mq.addEventListener("change", labels);

  if (document.querySelector(".wa-float")) return;
  var a = document.createElement("a");
  a.className = "ag-wa";
  a.href = "https://wa.me/" + PHONE + "?text=" + encodeURIComponent(MSG);
  a.target = "_blank";
  a.rel = "noopener";
  a.title = "كلّمني واتساب";
  a.setAttribute("aria-label", "واتساب");
  a.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2C6.48 2 2 6.48 2 12c0 1.85.5 3.58 1.35 5.07L2 22l5.06-1.32A9.94 9.94 0 0012 22c5.52 0 10-4.48 10-10S17.52 2 12 2zm5.2 14.2c-.22.62-1.28 1.18-1.77 1.24-.45.06-1.02.09-1.65-.1-.38-.12-.87-.28-1.5-.55-2.64-1.14-4.36-3.8-4.5-3.980-.13-.18-1.08-1.44-1.08-2.75 0-1.3.68-1.94.92-2.2.24-.26.53-.33.7-.33.18 0 .35 0 .5.01.16.01.38-.06.6.46.22.53.75 1.83.82 1.96.07.13.11.29.02.47-.09.18-.13.29-.26.44-.13.16-.28.35-.4.47-.13.13-.27.28-.12.55.16.26.7 1.16 1.5 1.88 1.04.93 1.91 1.22 2.170 1.36.26.13.42.11.57-.07.16-.18.66-.77.84-1.04.18-.26.35-.22.6-.13.24.09 1.54.73 1.8.86.27.13.44.2.51.31.07.11.07.62-.15 1.24z"/></svg>';
  document.body.appendChild(a);

  function lift() {
    var fab = document.querySelector(".fab");
    var h = 0;
    if (fab && getComputedStyle(fab).display !== "none" && fab.offsetHeight) h = fab.offsetHeight + 14;
    a.style.bottom = (18 + h) + "px";
  }
  lift();
  window.addEventListener("resize", lift);
  setInterval(lift, 800);
})();

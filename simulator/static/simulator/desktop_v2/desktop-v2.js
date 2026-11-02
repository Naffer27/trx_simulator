/* Copia de Desktop V2 dentro de trx_sim. Ajustes mínimos de presentación respecto al diseño original:
   '—' para valores vacíos, totales de cuenta tomados del motor (account:update), gráfico espera precio real,
   window.MB_V2_REFRESH para refrescar cuando el motor envía cambios, actualización blindada por partes
   y margen del ticket tomado del motor (risk_preview). Sin lógica de trading. */
/* =====================================================================
   Money Broker — Desktop V2 · interfaz
   ---------------------------------------------------------------------
   Solo dibuja la pantalla y reacciona a clics. NO contiene lógica del
   motor: todo dato y toda acción pasa por window.MB_API (adapter.js).
   Funciona igual para las dos opciones (A y B): cada HTML marca con
   data-variant cómo quiere ver cada bloque.
   ===================================================================== */
(function () {
  'use strict';
  var API = window.MB_API;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ------------------------------ Estado ------------------------------ */
  var S = {
    sym: 'BTCUSD', tf: '15m', layout: 1, tool: 0, tab: 'pos', range: '1D',
    lot: 0.01, side: 'buy', otype: 'market', slTxt: '', tpTxt: '', priceTxt: '', search: ''
  };
  var D = { account: null, quotes: [], q: {}, positions: [], orders: [], history: [], journal: [], candles: {}, spark: {} };

  var TFS = ['1m', '5m', '15m', '1H', '4H', '1D'];
  var PRESETS = [0.01, 0.05, 0.10, 0.50, 1.00];
  var OTYPES = [['market', 'Mercado'], ['limit', 'Límite'], ['stop', 'Stop']];
  var TOOLS = [
    ['Cursor', 'M5 3l14 8-6 2-2 6z'],
    ['Línea de tendencia', 'M4 20L20 4'],
    ['Línea horizontal', 'M3 12h18M7 8v8M17 8v8'],
    ['Canal', 'M4 16L16 4M8 20L20 8'],
    ['Texto', 'M5 6h14M12 6v13M9 19h6'],
    ['Medir', 'M3 17l14-14 4 4-14 14zM7 13l2 2M10 10l2 2M13 7l2 2'],
    ['Borrar dibujos', 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3']
  ];
  var MES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];

  /* ------------------------------ Formatos ------------------------------ */
  function fmt(v, d) { return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function money(v) { if (v === null || v === undefined || isNaN(v)) return '—'; return (v < 0 ? '−$' : '+$') + fmt(Math.abs(v), 2); }
  function usd(v) { if (v === null || v === undefined || isNaN(v)) return '—'; return (v < 0 ? '−$' : '$') + fmt(Math.abs(v), 2); }
  function dg(sym) { return D.q[sym] ? D.q[sym].digits : 2; }
  function px(sym, v) { return v === null || v === undefined || v === '' ? '—' : fmt(v, dg(sym)); }
  function hhmm(t) { var d = new Date(t); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function parseNum(s) { if (!s) return null; var v = parseFloat(String(s).replace(/,/g, '')); return isFinite(v) ? v : null; }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

  function spreadTxt(q) {
    if (q.bid === null || q.ask === null || q.bid === undefined || q.ask === undefined) return '—';
    var d = q.ask - q.bid;
    if (q.kind === 'crypto') return d.toFixed(2);
    return (d / (q.kind === 'jpy' ? 0.01 : 0.0001)).toFixed(1);
  }
  function notional(sym, lots, price) {
    var q = D.q[sym];
    if (!q) return 0;
    if (q.kind === 'crypto') return price * lots;
    if (q.kind === 'jpy') return 100000 * lots;
    return 100000 * lots * price;
  }
  function marginFor(sym, lots, price) { return notional(sym, lots, price) / (D.account ? D.account.leverage : 50); }
  function pnlOf(p) { return API.pnl(p, D.q[p.sym]); }
  // Dinero que se gana/pierde si el precio llega a "target" (usa la regla del motor)
  function valueAt(side, lots, entry, sym, target) {
    var q = Object.assign({}, D.q[sym], { bid: target, ask: target });
    return API.pnl({ side: side, lots: lots, entry: entry }, q);
  }
  function totals() {
    var A = D.account || {};
    if (A.equity !== null && A.equity !== undefined) {
      var plM = A.pnl_unreal !== null && A.pnl_unreal !== undefined ? A.pnl_unreal : A.equity - A.balance;
      return { balance: A.balance, pl: plM, equity: A.equity, used: A.margin_used, free: A.free_margin, level: A.margin_level };
    }
    var pl = 0, used = 0;
    D.positions.forEach(function (p) { pl += pnlOf(p); used += marginFor(p.sym, p.lots, p.entry); });
    var bal = D.account ? D.account.balance : 0, eq = bal + pl;
    return { balance: bal, pl: pl, equity: eq, used: used, free: eq - used, level: used > 0 ? eq / used * 100 : null };
  }

  /* ------------------------------ Tema ------------------------------ */
  function getTheme() {
    var u = new URLSearchParams(location.search).get('tema');
    if (u === 'dia' || u === 'noche') return u;
    try { return localStorage.getItem('mb-tema') || 'noche'; } catch (e) { return 'noche'; }
  }
  function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('mb-tema', t); } catch (e) {}
    $$('[data-act="theme"]').forEach(function (b) { b.setAttribute('aria-label', t === 'dia' ? 'Cambiar a modo noche' : 'Cambiar a modo día'); });
    drawCharts();
  }

  /* ------------------------------ Aviso / diario ------------------------------ */
  var toastTimer;
  function toast(txt, val) {
    var el = $('#v2-toast'); if (!el) return;
    el.innerHTML = esc(txt) + (val !== undefined && val !== null ? ' <span class="num" style="color:' + (val < 0 ? 'var(--pl-neg-dark,#FF8A9A)' : 'var(--pl-pos-dark,#3DDC97)') + '">' + money(val) + '</span>' : '');
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('show'); }, 2800);
  }
  function log(txt) { D.journal.unshift({ t: Date.now(), txt: txt }); if (S.tab === 'jou') renderTabBody(); renderTabs(); }

  /* ------------------------------ Cuenta ------------------------------ */
  function renderAcct() {
    var el = $('#v2-acct'); if (!el) return;
    var T = totals(), hasPos = D.positions.length > 0;
    var items = [
      ['Balance', usd(T.balance), ''],
      ['Equity', usd(T.equity), ''],
      ['Profit / Loss', hasPos ? money(T.pl) : '$0.00', !hasPos ? '' : T.pl < 0 ? 'v2-sell' : 'v2-buy'],
      ['Margen usado', usd(T.used), ''],
      ['Margen libre', usd(T.free), ''],
      ['Nivel de margen', T.level === null ? '—' : Math.round(T.level).toLocaleString('en-US') + '%', 'accent']
    ];
    el.className = 'v2-acct--' + (el.dataset.variant || 'strip');
    el.innerHTML = items.map(function (i) { return '<div class="v2-metric"><span>' + i[0] + '</span><b class="num ' + i[2] + '">' + i[1] + '</b></div>'; }).join('');
    var a = $('#v2-account-label'); if (a && D.account) a.textContent = 'Cuenta ' + D.account.id + ' · ' + D.account.type;
    var c = $('#v2-conn'); if (c && D.account) { c.classList.toggle('off', !D.account.connected); }
  }

  /* ------------------------------ Watchlist ------------------------------ */
  function sparkPath(sym) {
    var arr = D.spark[sym]; if (!arr) return '';
    var v = arr.map(function (c) { return c.c; }), mn = Math.min.apply(null, v), mx = Math.max.apply(null, v);
    return v.map(function (x, i) { return (i ? 'L' : 'M') + (i * 56 / (v.length - 1)).toFixed(1) + ' ' + (2 + (mx - x) / ((mx - mn) || 1) * 18).toFixed(1); }).join(' ');
  }
  function renderWatch() {
    var el = $('#v2-watch'); if (!el) return;
    var list = D.quotes.filter(function (q) { return !S.search || q.sym.toLowerCase().indexOf(S.search.toLowerCase()) >= 0 || (q.name || '').toLowerCase().indexOf(S.search.toLowerCase()) >= 0; });
    var variant = el.dataset.variant || 'table';
    var chg = function (q) { if (q.bid === null || q.ask === null) return '<small class="v2-muted" title="Sin cotización reciente" style="white-space:nowrap">Sin precio</small>'; return '<small class="num ' + (q.chg >= 0 ? 'v2-buy' : 'v2-sell') + '">' + (q.chg >= 0 ? '+' : '') + q.chg.toFixed(2) + '%</small>'; };
    if (variant === 'table') {
      el.innerHTML = '<div class="v2-watch-head"><span>INSTRUMENTO</span><span>BID</span><span>ASK</span><span>SPR</span></div>' +
        list.map(function (q) {
          return '<button class="v2-watch-row" data-act="sym" data-sym="' + q.sym + '" aria-pressed="' + (q.sym === S.sym) + '">' +
            '<span class="who"><span class="v2-badge">' + esc(q.badge) + '</span><span><b>' + q.sym + '</b>' + chg(q) + '</span></span>' +
            '<span class="r num bid">' + px(q.sym, q.bid) + '</span><span class="r num ask">' + px(q.sym, q.ask) + '</span><span class="r num spr">' + spreadTxt(q) + '</span></button>';
        }).join('');
    } else {
      el.innerHTML = list.map(function (q) {
        return '<button class="v2-watch-item" data-act="sym" data-sym="' + q.sym + '" aria-pressed="' + (q.sym === S.sym) + '">' +
          '<span class="v2-badge">' + esc(q.badge) + '</span>' +
          '<span class="who"><b>' + q.sym + '</b>' + chg(q) + '</span>' +
          '<svg width="56" height="22" viewBox="0 0 56 22" fill="none" aria-hidden="true"><path d="' + sparkPath(q.sym) + '" stroke="' + (q.chg >= 0 ? 'var(--buy-text)' : 'var(--sell-text)') + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>' +
          '<span class="px"><b class="num">' + px(q.sym, q.bid) + '</b><small class="num">spr ' + spreadTxt(q) + '</small></span></button>';
      }).join('');
    }
    if (!list.length) el.innerHTML += '<div class="v2-empty"><b>Sin resultados</b><span>Prueba con otro símbolo</span></div>';
    var count = $('#v2-watch-count'); if (count) count.textContent = D.quotes.length + ' símbolos';
  }

  /* ------------------------------ Barra del gráfico ------------------------------ */
  function renderSymbol() {
    var el = $('#v2-symbol'); if (!el) return; var q = D.q[S.sym]; if (!q) return;
    if ((el.dataset.variant || 'button') === 'button') {
      // Preserve the native selector across price ticks so an open menu stays open.
      var select = $('#v2-symbol-select', el);
      var symbolsKey = JSON.stringify(D.quotes.map(function (item) { return item.sym; }));
      if (!select) {
        el.innerHTML = '<select id="v2-symbol-select" class="v2-symbol-btn" aria-label="Cambiar instrumento"></select>';
        select = $('#v2-symbol-select', el);
      }
      if (select.dataset.symbolsKey !== symbolsKey) {
        select.innerHTML = D.quotes.map(function (item) {
          return '<option value="' + esc(item.sym) + '">' + esc(item.sym) + '</option>';
        }).join('');
        select.dataset.symbolsKey = symbolsKey;
      }
      if (select.value !== S.sym) select.value = S.sym;
    } else {
      el.innerHTML = '<span class="v2-symbol-title"><span class="name">' + S.sym + '</span><span class="num price">' + px(S.sym, q.bid) + '</span><span class="num chg ' + (q.chg >= 0 ? 'v2-buy' : 'v2-sell') + '">' + (q.chg >= 0 ? '+' : '') + q.chg.toFixed(2) + '%</span></span>';
    }
  }
  function renderSeg(id, items, current, act) {
    var el = $(id); if (!el) return;
    el.innerHTML = items.map(function (it) {
      var v = Array.isArray(it) ? it[0] : it, label = Array.isArray(it) ? it[1] : it;
      return '<button class="num" data-act="' + act + '" data-v="' + v + '" aria-pressed="' + (String(v) === String(current)) + '"' + (act === 'layout' ? ' aria-label="' + v + ' gráficos"' : '') + '>' + label + '</button>';
    }).join('');
  }
  function renderTools() {
    var el = $('#v2-tools'); if (!el) return;
    el.innerHTML = TOOLS.map(function (t, i) {
      return '<button class="v2-tool" data-act="tool" data-v="' + i + '" aria-pressed="' + (i === S.tool) + '" aria-label="' + t[0] + '" title="' + t[0] + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="' + t[1] + '"/></svg></button>';
    }).join('');
  }
  function renderToolbar() {
    renderSymbol();
    renderSeg('#v2-tfs', TFS, S.tf, 'tf');
    renderSeg('#v2-layouts', [[1, '×1'], [2, '×2'], [3, '×3'], [4, '×4']], S.layout, 'layout');
    renderSeg('#v2-ranges', ['1D', 'ALL'], S.range, 'range');
    renderTools();
  }

  /* ------------------------------ Gráficos ------------------------------ */
  function leerCfg() { try { return JSON.parse(localStorage.getItem('mb-v2-pantalla') || '{}') || {}; } catch (e) { return {}; } }
  function guardarCfg(extra) { try { var c = leerCfg(); Object.keys(extra).forEach(function (k) { c[k] = extra[k]; }); localStorage.setItem('mb-v2-pantalla', JSON.stringify(c)); } catch (e) {} }
  var panels = [];
  // Orden estable de los gráficos: elegir otro símbolo cambia solo el gráfico activo, no reordena los demás
  function panelSyms() {
    var lista = (S.lista || []).filter(function (s, i, a) { return D.q[s] && a.indexOf(s) === i; });
    if (lista.indexOf(S.sym) < 0) { var ia = Math.max(0, Math.min(S.activo || 0, lista.length)); lista[ia] = S.sym; }
    var order = ['BTCUSD', 'ETHUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD'];
    Object.keys(D.q).forEach(function (s) { if (order.indexOf(s) < 0) order.push(s); });
    order.forEach(function (s) { if (lista.length < S.layout && lista.indexOf(s) < 0 && D.q[s]) lista.push(s); });
    var out = lista.slice(0, S.layout);
    if (out.indexOf(S.sym) < 0) { lista.splice(lista.indexOf(S.sym), 1); lista.unshift(S.sym); out = lista.slice(0, S.layout); }
    S.lista = lista; S.activo = out.indexOf(S.sym); guardarCfg({ lista: lista });
    return out;
  }
  // Divisores entre gráficos (×2/×3/×4): arrastrar para cambiar el ancho/alto de cada uno, como en el Panel 1
  function fracciones(tipo, n) { var f = (S.fr || {})[tipo + S.layout]; return (f && f.length === n) ? f.slice() : Array.apply(null, Array(n)).map(function () { return 1; }); }
  function plantilla(fr) { return fr.map(function (x) { return 'minmax(0, ' + x + 'fr)'; }).join(' '); }
  function arrastrable(el, inicio, mover, fin) {
    el.addEventListener('pointerdown', function (e) {
      e.preventDefault(); e.stopPropagation();
      var x0 = e.clientX, y0 = e.clientY, ini = inicio(); el.classList.add('activo'); document.body.classList.add('v2-redimensionando');
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      function mv(ev) { mover(ev.clientX - x0, ev.clientY - y0, ini); }
      function up() { el.classList.remove('activo'); document.body.classList.remove('v2-redimensionando'); el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up); if (fin) fin(); }
      el.addEventListener('pointermove', mv); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    });
  }
  function divisores(host, cols, rows) {
    $$('.v2-div', host).forEach(function (d) { d.remove(); });
    if (S.layout === 1) return;
    function crear(tipo, i) {
      var d = document.createElement('div'); d.className = 'v2-div v2-div-' + tipo; d.title = 'Arrastra para cambiar el tamaño';
      host.appendChild(d);
      arrastrable(d, function () {
        var a = panels[tipo === 'c' ? i : i * cols].el.getBoundingClientRect(), b = panels[tipo === 'c' ? i + 1 : (i + 1) * cols].el.getBoundingClientRect();
        return { fr: fracciones(tipo, tipo === 'c' ? cols : rows), a: tipo === 'c' ? a.width : a.height, b: tipo === 'c' ? b.width : b.height };
      }, function (dx, dy, ini) {
        var tot = ini.a + ini.b, na = Math.max(160, Math.min(tot - 160, ini.a + (tipo === 'c' ? dx : dy))), sum = ini.fr[i] + ini.fr[i + 1];
        var fr = ini.fr.slice(); fr[i] = sum * na / tot; fr[i + 1] = sum - fr[i];
        S.fr = S.fr || {}; S.fr[tipo + S.layout] = fr;
        if (tipo === 'c') host.style.gridTemplateColumns = plantilla(fr); else host.style.gridTemplateRows = plantilla(fr);
        ubicarDivisores(host, cols, rows);
      }, function () { guardarCfg({ fr: S.fr }); });
    }
    for (var c = 0; c < cols - 1; c++) crear('c', c);
    for (var r = 0; r < rows - 1; r++) crear('r', r);
    ubicarDivisores(host, cols, rows);
  }
  function ubicarDivisores(host, cols, rows) {
    var hr = host.getBoundingClientRect();
    $$('.v2-div-c', host).forEach(function (d, i) { var p = panels[i]; if (!p) return; var r = p.el.getBoundingClientRect(); d.style.left = (r.right - hr.left) + 'px'; });
    $$('.v2-div-r', host).forEach(function (d, i) { var p = panels[i * cols]; if (!p) return; var r = p.el.getBoundingClientRect(); d.style.top = (r.bottom - hr.top) + 'px'; });
  }
  // Divisores de la pantalla: alto del gráfico (vs. cajas de abajo) y ancho del lateral
  function divisoresPantalla() {
    var charts = $('#v2-charts'), side = $('.v2-side'); if (!charts || !side || $('.v2-res-y')) return;
    var cfg = leerCfg();
    if (cfg.alto) charts.style.height = cfg.alto + 'px';
    if (cfg.lado) side.style.flex = '0 0 ' + cfg.lado + 'px';
    var hy = document.createElement('div'); hy.className = 'v2-res-y'; hy.title = 'Arrastra para cambiar el alto del gráfico';
    charts.parentNode.insertBefore(hy, charts.nextSibling);
    arrastrable(hy, function () { return { h: charts.getBoundingClientRect().height }; }, function (dx, dy, ini) {
      charts.style.height = Math.max(240, Math.min(window.innerHeight - 120, ini.h + dy)) + 'px';
      if (panels.length > 1) ubicarDivisores(charts, S.layout === 1 ? 1 : S.layout === 3 ? 3 : 2, S.layout === 4 ? 2 : 1);
    }, function () { guardarCfg({ alto: Math.round(charts.getBoundingClientRect().height) }); });
    var hx = document.createElement('div'); hx.className = 'v2-res-x'; hx.title = 'Arrastra para cambiar el ancho del lateral';
    side.parentNode.insertBefore(hx, side);
    arrastrable(hx, function () { return { w: side.getBoundingClientRect().width }; }, function (dx, dy, ini) {
      side.style.flex = '0 0 ' + Math.max(280, Math.min(620, ini.w - dx)) + 'px';
      if (panels.length > 1) ubicarDivisores(charts, S.layout === 1 ? 1 : S.layout === 3 ? 3 : 2, S.layout === 4 ? 2 : 1);
    }, function () { guardarCfg({ lado: Math.round(side.getBoundingClientRect().width) }); });
  }
  function buildCharts() {
    var host = $('#v2-charts'); if (!host) return;
    var cols = S.layout === 1 ? 1 : S.layout === 3 ? 3 : 2, rows = S.layout === 4 ? 2 : 1;
    host.style.gridTemplateColumns = plantilla(fracciones('c', cols));
    host.style.gridTemplateRows = plantilla(fracciones('r', rows));
    host.style.position = 'relative';
    var headMode = host.dataset.head || 'always', oneclick = host.dataset.oneclick === 'true';
    (panels || []).forEach(function (P) { try { P.ro && P.ro.disconnect(); P.lw && P.lw.remove(); } catch (e) {} });
    host.innerHTML = '';
    panels = panelSyms().map(function (sym, i) {
      var sec = document.createElement('section');
      sec.className = 'v2-panel';
      sec.setAttribute('aria-label', 'Gráfico ' + sym);
      var showHead = headMode === 'always' || (headMode === 'multi' && S.layout > 1);
      var activo = sym === S.sym;
      if (S.layout > 1) sec.classList.add(activo ? 'activo' : 'pasivo');
      sec.innerHTML = (showHead ? '<div class="v2-panel-head"' + (activo ? '' : ' data-act="sym" data-sym="' + sym + '" title="Operar ' + sym + ' (elegir este gráfico)"') + '><span class="sym">' + sym + '</span><span class="num px"></span><span class="num chg"></span><span class="v2-spacer"></span><span class="num v2-muted" style="font-size:11px">' + S.tf + '</span></div>' : '') +
        '<div class="v2-plot"><canvas></canvas><div class="v2-overlay"></div></div>';
      host.appendChild(sec);
      var P = { sym: sym, el: sec, plot: $('.v2-plot', sec), canvas: $('canvas', sec), overlay: $('.v2-overlay', sec), oneclick: oneclick && i === 0, pillsRight: host.dataset.pills === 'right' };
      if (P.oneclick) {
        var oc = document.createElement('div');
        oc.className = 'v2-oneclick'; oc.setAttribute('role', 'group'); oc.setAttribute('aria-label', 'Operar en un clic');
        oc.innerHTML = '<button class="sell" data-act="market" data-side="sell"><small>VENDER</small><b class="num" data-f="bid"></b></button>' +
          '<span class="lot"><button data-act="lot-down" aria-label="Bajar lote">−</button><input id="v2-lot" class="num" data-f="lot" type="text" inputmode="decimal" autocomplete="off" aria-label="Volumen en lotes"><button data-act="lot-up" aria-label="Subir lote">+</button></span>' +
          '<button class="buy" data-act="market" data-side="buy"><small>COMPRAR</small><b class="num" data-f="ask"></b></button>';
        P.plot.appendChild(oc); P.oneclickEl = oc;
      }
      return P;
    });
    // Gráfico activo = conexión principal (la que opera). Los demás: conexiones de solo lectura.
    if (API.setPanelFeeds) API.setPanelFeeds(panels.filter(function (P) { return P.sym !== S.sym; }).map(function (P) { return P.sym; }), S.tf);
    var requestedTf = S.tf;
    panels.forEach(function (P) {
      var pr = (P.sym === S.sym || !API.getPanelCandles) ? API.getCandles(P.sym, requestedTf, 220) : API.getPanelCandles(P.sym, requestedTf);
      pr.then(function (c) {
        D.candles[P.sym + '|' + requestedTf] = c;
        if (S.tf !== requestedTf || panels.indexOf(P) < 0) return;
        seguro('grafico ' + P.sym, function () { drawPanel(P); });
      });
    });
    divisores(host, cols, rows);
    if (!host._ro && window.ResizeObserver) { host._ro = new ResizeObserver(function () { if (panels.length > 1) ubicarDivisores(host, S.layout === 3 ? 3 : 2, S.layout === 4 ? 2 : 1); }); host._ro.observe(host); }
  }
  function drawCharts() { panels.forEach(function (P) { seguro('grafico ' + P.sym, function () { drawPanel(P); }); }); }
  /* ---------- Gráfico con Lightweight Charts (misma librería que el escritorio clásico) ----------
     Zoom con la rueda, arrastrar para ver historia, cruz de precio/hora.
     Las líneas de entrada/SL/TP/órdenes son líneas reales del gráfico; las etiquetas HTML
     encima permiten cerrar, mover SL/TP arrastrando y crear SL/TP con "+SL" / "+TP". */
  function tema() { return document.documentElement.getAttribute('data-theme') || 'noche'; }
  function coloresGrafico() {
    return { fondo: cssVar('--panel') || '#0B0E13', texto: cssVar('--muted') || '#8A94A6', grid: cssVar('--grid') || '#141A22',
      linea: cssVar('--line') || '#1A212C', up: cssVar('--up') || '#2EBD85', down: cssVar('--down') || '#F6465D',
      brand: cssVar('--brand') || '#F5C518', buy: cssVar('--buy') || '#2EBD85', sell: cssVar('--sell') || '#F6465D',
      sellText: cssVar('--sell-text') || '#F6465D', entry: cssVar('--entry-line') || cssVar('--text') || '#E8ECF2' };
  }
  function horaLocal(ts) { var dd = new Date(ts * 1000); return String(dd.getHours()).padStart(2, '0') + ':' + String(dd.getMinutes()).padStart(2, '0'); }
  function crearGrafico(P) {
    var C = coloresGrafico();
    if (P.canvas) P.canvas.style.display = 'none';
    var cont = document.createElement('div'); cont.className = 'v2-lw'; cont.style.cssText = 'position:absolute;inset:0';
    P.plot.insertBefore(cont, P.overlay);
    var lw = LightweightCharts.createChart(cont, {
      width: P.plot.clientWidth, height: P.plot.clientHeight,
      layout: { background: { type: 'solid', color: C.fondo }, textColor: C.texto, fontFamily: 'Geist Mono, ui-monospace, monospace', fontSize: 11 },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: 0 },
      rightPriceScale: { borderColor: C.linea, scaleMargins: { top: 0.08, bottom: 0.18 } },
      timeScale: { borderColor: C.linea, timeVisible: true, secondsVisible: false, rightOffset: 6,
        tickMarkFormatter: function (t, tipo) { var dd = new Date(t * 1000); return tipo < 3 ? dd.getDate() + ' ' + MES[dd.getMonth()] : horaLocal(t); } },
      localization: { locale: 'es-US', timeFormatter: function (t) { var dd = new Date(t * 1000); return dd.getDate() + ' ' + MES[dd.getMonth()] + ' ' + horaLocal(t); } }
    });
    P.lw = lw; P.tema = tema(); P.lineas = [];
    P.velas = lw.addCandlestickSeries({ upColor: C.up, downColor: C.down, borderVisible: false, wickUpColor: C.up, wickDownColor: C.down, priceLineVisible: false, lastValueVisible: false,
      autoscaleInfoProvider: function (base) {
        var r = base(); if (!r || !r.priceRange) return r;
        var lo = r.priceRange.minValue, hi = r.priceRange.maxValue;
        var extra = [];   // las velas mandan la escala; un TP/SL lejano ya no aplasta el gráfico (su etiqueta queda en el borde)
        if (P.temp) extra.push(P.temp.precio);
        extra.forEach(function (v) { if (v !== null && v !== undefined && isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
        r.priceRange.minValue = lo; r.priceRange.maxValue = hi; return r;
      } });
    P.vol = lw.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
    lw.priceScale('vol').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 }, visible: false });
    lw.timeScale().subscribeVisibleLogicalRangeChange(function () { if (!arrastre) seguro('etiquetas', function () { etiquetas(P); }); });
    if (window.ResizeObserver) { P.ro = new ResizeObserver(function () { lw.resize(P.plot.clientWidth, P.plot.clientHeight); seguro('etiquetas', function () { etiquetas(P); }); }); P.ro.observe(P.plot); }
  }
  function aplicarTema(P) {
    var C = coloresGrafico(); P.tema = tema();
    P.lw.applyOptions({ layout: { background: { type: 'solid', color: C.fondo }, textColor: C.texto }, grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      rightPriceScale: { borderColor: C.linea }, timeScale: { borderColor: C.linea } });
    P.velas.applyOptions({ upColor: C.up, downColor: C.down, wickUpColor: C.up, wickDownColor: C.down });
    P.dKey = null;
  }
  function barra(c) { return { time: Math.floor(c.t / 1000), open: c.o, high: c.h, low: c.l, close: c.c }; }
  function barraVol(c, C) { return { time: Math.floor(c.t / 1000), value: c.v || 0, color: (c.c >= c.o ? C.up : C.down) + '55' }; }

  function drawPanel(P) {
    var all = D.candles[P.sym + '|' + S.tf], q = D.q[P.sym];
    if (!all || !q || !all.length || !window.LightweightCharts) return;
    if (q.bid === null || q.bid === undefined) { var lc = all[all.length - 1]; q = Object.assign({}, q, { bid: lc.c, ask: lc.c }); }
    if (!P.lw) crearGrafico(P);
    if (P.tema !== tema()) aplicarTema(P);
    var C = coloresGrafico(), d = q.digits, key = P.sym + '|' + S.tf;
    P.dg = d; P.q = q;
    P.velas.applyOptions({ priceFormat: { type: 'price', precision: d, minMove: Math.pow(10, -d) } });

    // Velas: carga completa cuando cambia la historia; si solo cambia la última, se actualiza esa
    var ult = all[all.length - 1];
    if (P.dKey !== key || P.dFirst !== all[0].t || Math.abs(P.dLen - all.length) > 1) {
      P.velas.setData(all.map(barra)); P.vol.setData(all.map(function (c) { return barraVol(c, C); }));
      if (P.dKey !== key) { var tsx = P.lw.timeScale(); tsx.setVisibleLogicalRange({ from: Math.max(0, all.length - 120), to: all.length + 5 }); }
      P.dKey = key; P.dFirst = all[0].t;
    } else {
      P.velas.update(barra(ult)); P.vol.update(barraVol(ult, C));
    }
    P.dLen = all.length;

    // Líneas reales del gráfico
    P.lineas.forEach(function (l) { try { P.velas.removePriceLine(l); } catch (e) {} }); P.lineas = [];
    function linea(precio, color, estilo, titulo, ancho) {
      if (precio === null || precio === undefined || !isFinite(precio)) return;
      P.lineas.push(P.velas.createPriceLine({ price: precio, color: color, lineWidth: ancho || 1, lineStyle: estilo, axisLabelVisible: true, title: titulo || '' }));
    }
    var pos = D.positions.filter(function (p) { return p.sym === P.sym; });
    var ords = D.orders.filter(function (o) { return o.sym === P.sym; });
    ords.forEach(function (o) { linea(o.price, C.brand, 1, (o.type === 'limit' ? 'Límite' : 'Stop')); });
    pos.forEach(function (p) {
      if (p.tp) linea(p.tp, C.buy, 2, '', 1);
      if (p.sl) linea(p.sl, C.sellText, 2, '', 1);
      linea(p.entry, C.entry, 0, '', 1);
    });
    if (P.temp) linea(P.temp.precio, P.temp.k === 'tp' ? C.buy : C.sellText, 2, P.temp.k.toUpperCase() + ' nuevo', 2);
    linea(q.bid, C.brand, 3, '', 1);

    P.aPrecio = function (yy) { var v = P.velas.coordinateToPrice(yy); return v === null ? null : v; };
    etiquetas(P);

    // Cabecera del panel y botones en un clic
    var head = $('.v2-panel-head', P.el);
    if (head) {
      $('.px', head).textContent = px(P.sym, q.bid);
      var ch = $('.chg', head); var cg = Number(q.chg) || 0; ch.textContent = (cg >= 0 ? '+' : '') + cg.toFixed(2) + '%'; ch.className = 'num chg ' + (cg >= 0 ? 'v2-buy' : 'v2-sell');
    }
    if (P.oneclickEl) {
      $('[data-f="bid"]', P.oneclickEl).textContent = px(P.sym, q.bid);
      $('[data-f="ask"]', P.oneclickEl).textContent = px(P.sym, q.ask);
      $('[data-f="lot"]', P.oneclickEl).textContent = S.lot.toFixed(2);
    }
  }

  /* Etiquetas HTML sobre el gráfico (cerrar, arrastrar SL/TP, crear SL/TP) */
  function etiquetas(P) {
    if (!P.velas || (arrastre && arrastre.P === P)) return;
    var side = P.pillsRight ? 'right:86px' : 'left:10px';
    var Hp = P.plot.clientHeight, yMin = 12, yMax = Math.max(yMin + 1, Hp - 40);
    var crudo = function (v) { var c = P.velas.priceToCoordinate(v); return c === null ? null : c; };
    // Fuera de la vista: la etiqueta se queda pegada al borde (con flecha) y se puede seguir arrastrando
    var Y = function (v) { var c = crudo(v); return c === null ? null : Math.max(yMin, Math.min(yMax, c)); };
    var flecha = function (v) { var c = crudo(v); return c === null ? '' : c < yMin ? '↑ ' : c > yMax ? '↓ ' : ''; };
    var pos = D.positions.filter(function (p) { return p.sym === P.sym; });
    var items = [];   // cada etiqueta: { y: centro deseado, h: alto, html: función(top) } — luego se apilan para no encimarse
    pos.forEach(function (p) {
      var L = Number(p.lots) || 0, pl = pnlOf(p), yE = Y(p.entry);
      if (p.tp && Y(p.tp) !== null) items.push({ y: Y(p.tp), h: 20, html: function (t) { return '<div class="v2-pill tp num" data-drag="tp" data-id="' + p.id + '" title="Arrastra para mover el TP" style="' + side + ';top:' + t + 'px">' + flecha(p.tp) + 'TP ' + px(p.sym, p.tp) + ' <span>' + money(Math.abs(valueAt(p.side, L, p.entry, p.sym, p.tp))) + '</span></div>'; } });
      if (p.sl && Y(p.sl) !== null) items.push({ y: Y(p.sl), h: 20, html: function (t) { return '<div class="v2-pill sl num" data-drag="sl" data-id="' + p.id + '" title="Arrastra para mover el SL" style="' + side + ';top:' + t + 'px">' + flecha(p.sl) + 'SL ' + px(p.sym, p.sl) + ' <span>' + money(-Math.abs(valueAt(p.side, L, p.entry, p.sym, p.sl))) + '</span></div>'; } });
      if (yE === null) return;
      var mas = (p.sl ? '' : '<button class="v2-mas sl" data-act="nuevo" data-k="sl" data-id="' + p.id + '" title="Agregar Stop Loss">+SL</button>') +
                (p.tp ? '' : '<button class="v2-mas tp" data-act="nuevo" data-k="tp" data-id="' + p.id + '" title="Agregar Take Profit">+TP</button>');
      items.push({ y: yE, h: 24, html: function (t) { return '<div class="v2-entry" data-pos="' + p.id + '" style="' + side + ';top:' + t + 'px">' +
        '<span class="side ' + p.side + '">' + flecha(p.entry) + (p.side === 'buy' ? 'COMPRA' : 'VENTA') + '</span>' +
        '<span class="info num">' + L.toFixed(2) + ' · <b style="color:' + (pl < 0 ? 'var(--pl-neg-dark,#FF8A9A)' : 'var(--pl-pos-dark,#3DDC97)') + '">' + money(pl) + '</b></span>' + mas +
        '<button class="x" data-act="close" data-id="' + p.id + '" aria-label="Cerrar posición ' + p.sym + '"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#F5F7FA" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>'; } });
    });
    // Apilar: ordenadas de arriba a abajo; si una choca con la anterior, se corre hacia abajo
    items.sort(function (a, b) { return a.y - b.y; });
    var fondo = -Infinity, html = '';
    items.forEach(function (it) { it.top = Math.max(it.y - it.h / 2, fondo + 3); fondo = it.top + it.h; });
    // Si las de abajo se salen del gráfico, se apilan hacia arriba
    var techo = Hp - 30;
    for (var ii = items.length - 1; ii >= 0; ii--) { items[ii].top = Math.min(items[ii].top, techo - items[ii].h); techo = items[ii].top - 3; }
    items.forEach(function (it) { html += it.html(Math.round(it.top)); });
    if (P.temp && Y(P.temp.precio) !== null) {
      html += '<div class="v2-pill ' + P.temp.k + ' num" data-drag="' + P.temp.k + '" data-id="' + P.temp.id + '" data-temp="1" title="Arrastra y suelta para confirmar" style="' + side + ';top:' + (Y(P.temp.precio) - 10) + 'px;outline:2px dashed currentColor;outline-offset:2px">' +
        P.temp.k.toUpperCase() + ' ' + px(P.sym, P.temp.precio) + ' <span>arrastra y suelta</span></div>';
    }
    P.overlay.innerHTML = html;
  }


  /* ------------------------------ Pestañas inferiores ------------------------------ */
  function renderTabs() {
    var el = $('#v2-tabs'); if (!el) return;
    var defs = [['pos', 'Posiciones', D.positions.length], ['ord', 'Órdenes', D.orders.length], ['his', 'Historial', D.history.length], ['jou', 'Diario', D.journal.length]];
    el.innerHTML = defs.map(function (t) {
      return '<button role="tab" data-act="tab" data-v="' + t[0] + '" aria-selected="' + (S.tab === t[0]) + '">' + t[1] + ' <span class="v2-count num' + (t[2] ? ' has' : '') + '">' + t[2] + '</span></button>';
    }).join('');
  }
  function sideTag(s) { return '<span class="v2-tag ' + s + '">' + (s === 'buy' ? 'COMPRA' : 'VENTA') + '</span>'; }
  function empty(t, s) { return '<div class="v2-empty"><b>' + t + '</b><span>' + s + '</span></div>'; }
  function renderTabBody() {
    var el = $('#v2-tab-body'); if (!el) return;
    if (S.tab === 'pos') {
      if (!D.positions.length) { el.innerHTML = empty('No hay posiciones abiertas', 'Usa Vender o Comprar para abrir una'); return; }
      el.innerHTML = '<div class="v2-table-wrap"><table class="v2-table"><thead><tr><th>Símbolo</th><th>Lado</th><th>Lote</th><th>Entrada</th><th>Actual</th><th>SL</th><th>TP</th><th>P/L</th><th>Hora</th><th class="right">Acción</th></tr></thead><tbody>' +
        D.positions.map(function (p) {
          var q = D.q[p.sym] || {}, now = p.side === 'buy' ? q.bid : q.ask, pl = pnlOf(p);
          return '<tr><td class="sym">' + p.sym + '</td><td>' + sideTag(p.side) + '</td><td>' + (p.lots === null || p.lots === undefined ? '—' : Number(p.lots).toFixed(2)) + '</td><td>' + px(p.sym, p.entry) + '</td><td>' + px(p.sym, now) + '</td>' +
            '<td class="v2-sell">' + px(p.sym, p.sl) + '</td><td class="v2-buy">' + px(p.sym, p.tp) + '</td><td class="' + (pl < 0 ? 'v2-sell' : 'v2-buy') + '" style="font-weight:600">' + money(pl) + '</td><td>' + hhmm(p.time) + '</td>' +
            '<td class="right" style="white-space:nowrap"><button class="v2-row-btn" data-act="edit" data-id="' + p.id + '" style="margin-right:6px;color:var(--text-2);border-color:var(--border);background:transparent">SL/TP</button><button class="v2-row-btn" data-act="close" data-id="' + p.id + '">Cerrar</button></td></tr>';
        }).join('') + '</tbody></table></div>';
    } else if (S.tab === 'ord') {
      if (!D.orders.length) { el.innerHTML = empty('No hay órdenes pendientes', 'Las órdenes Límite y Stop aparecen aquí'); return; }
      el.innerHTML = '<div class="v2-table-wrap"><table class="v2-table"><thead><tr><th>Símbolo</th><th>Lado</th><th>Tipo</th><th>Lote</th><th>Precio</th><th>SL</th><th>TP</th><th>Hora</th><th class="right">Acción</th></tr></thead><tbody>' +
        D.orders.map(function (o) {
          return '<tr><td class="sym">' + o.sym + '</td><td>' + sideTag(o.side) + '</td><td style="font-family:Geist,sans-serif">' + (o.type === 'limit' ? 'Límite' : 'Stop') + '</td><td>' + (o.lots === null || o.lots === undefined ? '—' : Number(o.lots).toFixed(2)) + '</td><td>' + px(o.sym, o.price) + '</td>' +
            '<td class="v2-sell">' + px(o.sym, o.sl) + '</td><td class="v2-buy">' + px(o.sym, o.tp) + '</td><td>' + hhmm(o.time) + '</td>' +
            '<td class="right"><button class="v2-row-btn" data-act="cancel" data-id="' + o.id + '">Cancelar</button></td></tr>';
        }).join('') + '</tbody></table></div>';
    } else if (S.tab === 'his') {
      if (!D.history.length) { el.innerHTML = empty('Sin operaciones cerradas', 'Cuando cierres una posición aparecerá aquí'); return; }
      el.innerHTML = '<div class="v2-table-wrap"><table class="v2-table"><thead><tr><th>Símbolo</th><th>Lado</th><th>Lote</th><th>Entrada</th><th>Salida</th><th>P/L</th><th class="right">Hora</th></tr></thead><tbody>' +
        D.history.map(function (h) {
          return '<tr><td class="sym">' + h.sym + '</td><td>' + sideTag(h.side) + '</td><td>' + h.lots.toFixed(2) + '</td><td>' + px(h.sym, h.entry) + '</td><td>' + px(h.sym, h.exit) + '</td>' +
            '<td class="' + (h.pnl < 0 ? 'v2-sell' : 'v2-buy') + '" style="font-weight:600">' + money(h.pnl) + '</td><td class="right">' + hhmm(h.time) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    } else {
      el.innerHTML = D.journal.length ? '<ul class="v2-journal">' + D.journal.map(function (j) { return '<li><time class="num">' + hhmm(j.t) + '</time><span>' + esc(j.txt) + '</span></li>'; }).join('') + '</ul>' : empty('Diario vacío', 'Aquí aparecen los eventos de tu cuenta');
    }
  }

  /* ------------------------------ Ticket de orden ------------------------------ */
  var lotDraft = null;
  function parseLot(value) {
    var text = String(value).trim().replace(',', '.');
    if (!/^(?:\d+(?:\.\d{0,2})?|\.\d{1,2})$/.test(text)) return null;
    var value = Number(text);
    return Number.isFinite(value) && value >= 0.01 && value <= 100 ? value : null;
  }
  function fieldVolume() {
    return '<div class="v2-field"><div class="v2-field-head">Volumen (lotes)<small class="num" data-f="nominal"></small></div>' +
      '<div class="v2-stepper"><button data-act="lot-down" aria-label="Bajar volumen">−</button><input id="v2-lot" class="num" data-f="lot" type="text" inputmode="decimal" autocomplete="off" aria-label="Volumen en lotes"><button data-act="lot-up" aria-label="Subir volumen">+</button></div>' +
      '<div class="v2-presets">' + PRESETS.map(function (v) { return '<button class="num" data-act="lot-set" data-v="' + v + '" aria-pressed="false">' + v.toFixed(2) + '</button>'; }).join('') + '</div></div>';
  }
  function fieldInput(id, label, val, f, ph) {
    return '<label class="v2-field">' + label + '<span class="v2-input"><input id="' + id + '" inputmode="decimal" autocomplete="off" placeholder="' + (ph || 'Opcional') + '" value="' + esc(val) + '" aria-label="' + label + '"><small data-f="' + f + '"></small></span></label>';
  }
  function renderTicket() {
    var el = $('#v2-ticket'); if (!el) return;
    var variant = el.dataset.variant || 'buttons', lev = D.account ? D.account.leverage : 50;
    var types = '<div class="v2-otype' + (variant === 'ticket' ? ' v2-otype--tabs' : '') + '" role="tablist" aria-label="Tipo de orden">' +
      OTYPES.map(function (t) { return '<button role="tab" data-act="otype" data-v="' + t[0] + '" aria-selected="' + (S.otype === t[0]) + '">' + t[1] + '</button>'; }).join('') + '</div>';
    var price = S.otype !== 'market' ? fieldInput('v2-price', S.otype === 'limit' ? 'Precio límite' : 'Precio stop', S.priceTxt, 'price', 'Precio') : '';
    var sltp = fieldInput('v2-sl', 'Stop Loss', S.slTxt, 'risk') + fieldInput('v2-tp', 'Take Profit', S.tpTxt, 'reward');
    if (variant === 'buttons') {
      el.innerHTML = '<div class="v2-card-title"><b>Nueva orden</b><small class="num">' + S.sym + ' · 1:' + lev + '</small></div>' + types + price +
        '<div class="v2-sides"><button class="sell" data-act="send" data-side="sell"><small>VENDER</small><b class="num" data-f="bid"></b></button><button class="buy" data-act="send" data-side="buy"><small>COMPRAR</small><b class="num" data-f="ask"></b></button></div>' +
        fieldVolume() + '<div class="v2-sltp">' + sltp + '</div>' +
        '<div class="v2-summary num"><div><span>Margen requerido</span><span data-f="margin"></span></div><div><span>Riesgo hasta SL</span><span class="v2-sell" data-f="risk2"></span></div><div><span>Objetivo en TP</span><span class="v2-buy" data-f="reward2"></span></div></div>';
    } else {
      el.innerHTML = '<div class="v2-card-title"><b style="font-size:15px">Ticket de orden</b><small class="num">' + S.sym + ' · 1:' + lev + '</small></div>' +
        '<div class="v2-side-tabs" role="tablist" aria-label="Lado"><button role="tab" data-act="side" data-side="sell" aria-selected="' + (S.side === 'sell') + '"><b>Vender</b><small class="num" data-f="bid"></small></button><button role="tab" data-act="side" data-side="buy" aria-selected="' + (S.side === 'buy') + '"><b>Comprar</b><small class="num" data-f="ask"></small></button></div>' +
        types + price + fieldVolume() + sltp +
        '<div class="v2-summary v2-summary--box num"><div><span>Margen requerido</span><span data-f="margin"></span></div><div><span>Riesgo : beneficio</span><span data-f="rr" style="font-weight:700"></span></div><div><span>Margen libre después</span><span data-f="free"></span></div></div>' +
        '<span class="v2-spacer"></span><button class="v2-cta" data-act="submit" data-f="cta"></button>';
    }
    updateTicket();
  }
  function ticketEntry(side) {
    var q = D.q[S.sym];
    if (S.otype !== 'market') { var p = parseNum(S.priceTxt); if (p) return p; }
    return side === 'buy' ? q.ask : q.bid;
  }
  function setF(root, f, txt, cls) { $$('[data-f="' + f + '"]', root).forEach(function (n) { n.textContent = txt; if (cls !== undefined) n.className = cls; }); }
  function updateTicket() {
    var q = D.q[S.sym]; if (!q) return;
    var root = document;
    setF(root, 'bid', px(S.sym, q.bid)); setF(root, 'ask', px(S.sym, q.ask)); $$('[data-f="lot"]').forEach(function (n) {
      if (document.activeElement !== n) n.value = lotDraft === null ? S.lot.toFixed(2) : lotDraft;
    });
    $$('[data-act="lot-set"]').forEach(function (b) { b.setAttribute('aria-pressed', String(Math.abs(parseFloat(b.dataset.v) - S.lot) < 1e-9)); });
    var el = $('#v2-ticket'); if (!el) return;
    var sl = parseNum(S.slTxt), tp = parseNum(S.tpTxt);
    // Opción A (botones Vender/Comprar): deduce el lado por dónde está el SL/TP
    var side = S.side;
    if (el.dataset.variant !== 'ticket') {
      var mid = (q.bid + q.ask) / 2;
      side = (sl !== null && sl > mid) || (sl === null && tp !== null && tp < mid) ? 'sell' : 'buy';
    }
    var entry = ticketEntry(side);
    var nomM = API.notionalFor ? API.notionalFor(S.sym, S.lot) : notional(S.sym, S.lot, entry);   // nominal del MOTOR
    setF(el, 'nominal', nomM === null || nomM === undefined ? '' : '≈ ' + usd(nomM));
    // Margen: SOLO el que calcula el MOTOR (risk_preview). Mientras no responde se muestra "—", nunca un cálculo propio.
    var req = API.marginFor ? API.marginFor(S.sym, S.lot) : marginFor(S.sym, S.lot, entry);
    setF(el, 'margin', usd(req));
    var slOk = sl === null || (side === 'buy' ? sl < entry : sl > entry);
    var tpOk = tp === null || (side === 'buy' ? tp > entry : tp < entry);
    var risk = sl !== null && slOk ? Math.abs(valueAt(side, S.lot, entry, S.sym, sl)) : null;
    var rew = tp !== null && tpOk ? Math.abs(valueAt(side, S.lot, entry, S.sym, tp)) : null;
    setF(el, 'risk', sl === null ? '' : slOk ? money(-risk) : 'inválido', 'v2-sell');
    setF(el, 'reward', tp === null ? '' : tpOk ? money(rew) : 'inválido', tpOk ? 'v2-buy' : 'v2-sell');
    setF(el, 'risk2', risk === null ? '—' : money(-risk), 'v2-sell');
    setF(el, 'reward2', rew === null ? '—' : money(rew), 'v2-buy');
    setF(el, 'rr', risk && rew ? '1 : ' + (rew / risk).toFixed(2) : '—');
    setF(el, 'free', req === null || req === undefined ? '—' : usd(totals().free - req));
    var cta = $('[data-f="cta"]', el);
    if (cta) {
      var verb = side === 'buy' ? 'Comprar' : 'Vender';
      cta.textContent = (S.otype === 'market' ? verb : verb + ' ' + (S.otype === 'limit' ? 'límite' : 'stop')) + ' ' + S.lot.toFixed(2) + ' ' + S.sym;
      cta.className = 'v2-cta ' + side;
    }
  }

  /* ------------------------------ Acciones ------------------------------ */
  function refreshTrading() {
    return Promise.all([API.getAccount(), API.getPositions(), API.getOrders(), API.getHistory()]).then(function (r) {
      D.account = r[0]; D.positions = r[1]; D.orders = r[2]; D.history = r[3];
      seguro('cuenta', renderAcct); seguro('pestañas', renderTabs); seguro('tabla', renderTabBody);
      seguro('graficos', drawCharts); seguro('ticket', updateTicket);
    });
  }
  var enviando = false;
  function send(side) {
    if (lotDraft !== null && parseLot(lotDraft) === null) {
      toast('Escribe un volumen entre 0,01 y 100 lotes, con hasta dos decimales.');
      var field = $('#v2-lot'); if (field) field.focus();
      return;
    }
    var sl = parseNum(S.slTxt), tp = parseNum(S.tpTxt), entry = ticketEntry(side);
    if (sl !== null && (side === 'buy' ? sl >= entry : sl <= entry)) { toast('El Stop Loss está del lado equivocado'); return; }
    if (tp !== null && (side === 'buy' ? tp <= entry : tp >= entry)) { toast('El Take Profit está del lado equivocado'); return; }
    var o = { sym: S.sym, side: side, type: S.otype, lots: S.lot, sl: sl, tp: tp };
    if (S.otype !== 'market') { o.price = parseNum(S.priceTxt); if (!o.price) { toast('Escribe el precio de la orden'); return; } }
    // Anti doble clic: los botones de operar quedan bloqueados mientras el servidor responde y 2.5 s más
    if (enviando) { toast('Espera: la orden anterior se está procesando'); return; }
    enviando = true; document.body.classList.add('v2-enviando');
    var soltar = function () { setTimeout(function () { enviando = false; document.body.classList.remove('v2-enviando'); }, 2500); };
    API.placeOrder(o).finally(soltar).then(function (res) {
      var verb = side === 'buy' ? 'Compra' : 'Venta';
      if (res.kind === 'position') { S.tab = 'pos'; toast(verb + ' abierta · ' + S.lot.toFixed(2) + ' ' + S.sym); log(verb + ' ' + S.lot.toFixed(2) + ' ' + S.sym + ' a ' + px(S.sym, res.item.entry)); }
      else { S.tab = 'ord'; toast('Orden ' + (o.type === 'limit' ? 'límite' : 'stop') + ' colocada'); log('Orden ' + o.type + ' ' + verb.toLowerCase() + ' ' + S.lot.toFixed(2) + ' ' + S.sym + ' @ ' + px(S.sym, o.price)); }
      return refreshTrading();
    }).catch(function (e) { toast(e && e.message ? e.message : 'No se pudo enviar la orden'); });
  }
  function closeOne(id) {
    var p = D.positions.find(function (x) { return x.id === id; });
    API.closePosition(id).then(function (r) { toast('Posición cerrada', r.pnl); log('Cerrada ' + (p ? p.sym : '') + ' ' + money(r.pnl)); return refreshTrading(); })
      .catch(function (e) { toast(e.message || 'No se pudo cerrar'); });
  }

  var stopEditor = null;
  function openStopEditor(position, draft) {
    if (stopEditor) return;
    var id = position.id, originalSL = position.sl, originalTP = position.tp;
    var previousFocus = document.activeElement, pending = false;
    var dialog = document.createElement('dialog');
    dialog.className = 'v2-stop-editor';
    dialog.setAttribute('aria-labelledby', 'v2-stop-title');
    dialog.innerHTML = '<form><h2 id="v2-stop-title">Modificar SL / TP</h2>' +
      '<p>' + esc(position.sym) + ' · ' + (position.side === 'buy' ? 'Compra' : 'Venta') + ' · Posición #' + esc(id) + '</p>' +
      '<label>Stop Loss<input name="sl" type="number" step="any" min="0" inputmode="decimal"></label>' +
      '<label>Take Profit<input name="tp" type="number" step="any" min="0" inputmode="decimal"></label>' +
      '<p class="v2-muted">Un campo vacío conserva el valor actual.</p>' +
      '<p class="v2-stop-status" role="status" aria-live="polite"></p>' +
      '<div class="v2-stop-actions"><button type="button">Cancelar</button><button type="submit">Guardar cambios</button></div></form>';
    document.body.appendChild(dialog); stopEditor = dialog;
    var form = $('form', dialog), sl = $('[name="sl"]', dialog), tp = $('[name="tp"]', dialog);
    sl.value = draft && draft.k === 'sl' ? draft.price : (position.sl == null ? '' : position.sl);
    tp.value = draft && draft.k === 'tp' ? draft.price : (position.tp == null ? '' : position.tp);
    var status = $('.v2-stop-status', dialog), save = $('[type="submit"]', dialog);
    function close() { if (!pending) dialog.close(); }
    $('[type="button"]', dialog).onclick = close;
    dialog.addEventListener('cancel', function (e) { if (pending) e.preventDefault(); });
    dialog.addEventListener('close', function () {
      dialog.remove(); stopEditor = null; drawCharts();
      if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault(); if (pending) return;
      var latest = D.positions.find(function (p) { return String(p.id) === String(id); });
      if (!latest) { status.textContent = 'Esta posición ya no está abierta.'; return; }
      if (latest.sl !== originalSL || latest.tp !== originalTP) {
        status.textContent = 'La posición cambió. Cierra y vuelve a abrir el editor para revisar los valores actuales.'; return;
      }
      var nsl = sl.value.trim() ? Number(sl.value) : latest.sl, ntp = tp.value.trim() ? Number(tp.value) : latest.tp;
      if ((sl.value.trim() && (!isFinite(nsl) || nsl <= 0)) || (tp.value.trim() && (!isFinite(ntp) || ntp <= 0))) {
        status.textContent = 'Escribe precios válidos mayores que cero.'; return;
      }
      pending = true; save.disabled = true; sl.disabled = tp.disabled = true;
      status.textContent = 'Esperando confirmación del servidor…';
      API.modifyPosition(id, nsl, ntp).then(function () {
        pending = false; close(); toast('SL/TP confirmado por el servidor'); log('SL/TP actualizado · ' + position.sym);
        return refreshTrading();
      }).catch(function (error) {
        pending = false; save.disabled = false; sl.disabled = tp.disabled = false;
        status.textContent = error.message || 'No se pudo confirmar el cambio. Revisa la posición.';
      });
    });
    dialog.showModal(); (draft && draft.k === 'tp' ? tp : sl).focus();
  }

  function selectInstrument(sym) {
    if (!D.q[sym] || S.sym === sym) return;
    S.sym = sym;
    renderWatch(); renderToolbar(); buildCharts(); renderTicket();
  }
  document.addEventListener('change', function (e) {
    if (e.target.id === 'v2-symbol-select') selectInstrument(e.target.value);
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-act]'); if (!b) return;
    var a = b.dataset.act, v = b.dataset.v;
    if (a === 'theme') setTheme(document.documentElement.getAttribute('data-theme') === 'dia' ? 'noche' : 'dia');
    else if (a === 'sym') selectInstrument(b.dataset.sym);
    else if (a === 'tf') { S.tf = v; renderToolbar(); buildCharts(); }
    else if (a === 'layout') { S.layout = parseInt(v, 10); guardarCfg({ layout: S.layout }); renderToolbar(); buildCharts(); }
    else if (a === 'range') { S.range = v; renderToolbar(); }
    else if (a === 'tool') { S.tool = parseInt(v, 10); renderTools(); }
    else if (a === 'tab') { S.tab = v; renderTabs(); renderTabBody(); }
    else if (a === 'otype') { S.otype = v; renderTicket(); }
    else if (a === 'side') { S.side = b.dataset.side; renderTicket(); }
    else if (a === 'lot-down') { lotDraft = null; S.lot = Math.max(0.01, Math.round((S.lot - 0.01) * 100) / 100); updateTicket(); drawCharts(); }
    else if (a === 'lot-up') { lotDraft = null; S.lot = Math.min(100, Math.round((S.lot + 0.01) * 100) / 100); updateTicket(); drawCharts(); }
    else if (a === 'lot-set') { lotDraft = null; S.lot = parseFloat(v); updateTicket(); drawCharts(); }
    else if (a === 'market') { var keep = S.otype; S.otype = 'market'; send(b.dataset.side); S.otype = keep; }
    else if (a === 'send') send(b.dataset.side);
    else if (a === 'submit') send(S.side);
    else if (a === 'close') closeOne(parseInt(b.dataset.id, 10));
    else if (a === 'nuevo') {
      var pn = D.positions.find(function (x) { return String(x.id) === String(b.dataset.id); }); if (!pn) return;
      var Pn = panels.find(function (x) { return x.sym === pn.sym; }); if (!Pn) return;
      var qn = D.q[pn.sym] || {}, refn = pn.side === 'buy' ? qn.bid : qn.ask; if (refn === null || refn === undefined) refn = pn.entry;
      var arriba = (b.dataset.k === 'tp') === (pn.side === 'buy');
      var yRef = Pn.velas ? Pn.velas.priceToCoordinate(refn) : null, Hn = Pn.plot.clientHeight, pInicial = null;
      if (yRef !== null && isFinite(yRef)) { pInicial = Pn.aPrecio(arriba ? Math.max(14, yRef * 0.5) : Math.min(Hn - 40, yRef + (Hn - 30 - yRef) * 0.5)); }
      if (pInicial === null || !isFinite(pInicial) || (arriba ? pInicial <= refn : pInicial >= refn)) { var dist = Math.abs(refn) * 0.002; pInicial = refn + (arriba ? dist : -dist); }
      Pn.temp = { k: b.dataset.k, id: pn.id, precio: Number(pInicial.toFixed(Pn.dg || 2)) };
      toast('Arrastra la etiqueta ' + b.dataset.k.toUpperCase() + ' al precio que quieras y suéltala');
      drawCharts();
    }
    else if (a === 'cancel') { API.cancelOrder(parseInt(b.dataset.id, 10)).then(function () { toast('Orden cancelada'); log('Orden cancelada'); return refreshTrading(); }).catch(function (e) { toast(e.message || 'No se pudo cancelar'); }); }
    else if (a === 'edit') {
      var pe = D.positions.find(function (x) { return String(x.id) === String(b.dataset.id); }); if (!pe) return;
      openStopEditor(pe);
    }
    else if (a === 'close-all') {
      if (!D.positions.length) { toast('No hay posiciones abiertas'); return; }
      API.closeAll().then(function (r) { toast(r.count + (r.count === 1 ? ' posición cerrada' : ' posiciones cerradas'), r.pnl); log('Cerrar todo: ' + r.count + ' · ' + money(r.pnl)); return refreshTrading(); });
    }
    else if (a === 'cancel-all') {
      API.cancelAll().then(function (r) { toast(r.count ? r.count + ' órdenes canceladas' : 'No hay órdenes pendientes'); if (r.count) log('Cancelar todo: ' + r.count); return refreshTrading(); });
    }
  });
  document.addEventListener('input', function (e) {
    var id = e.target.id;
    if (id === 'v2-lot') {
      lotDraft = e.target.value;
      var lot = parseLot(lotDraft);
      e.target.setAttribute('aria-invalid', String(lot === null));
      if (lot !== null) S.lot = lot;
      updateTicket(); return;
    }
    if (id === 'v2-sl') S.slTxt = e.target.value;
    else if (id === 'v2-tp') S.tpTxt = e.target.value;
    else if (id === 'v2-price') S.priceTxt = e.target.value;
    else if (id === 'v2-search') { S.search = e.target.value; renderWatch(); return; }
    else return;
    updateTicket();
  });

  /* ------------------------------ Arranque ------------------------------ */
  // Cada parte se actualiza por separado: si una falla con un dato raro, las demás siguen (error en window.__V2_ERRORES).
  function seguro(nombre, fn) { try { fn(); } catch (e) { (window.__V2_ERRORES = window.__V2_ERRORES || []).push(nombre + ': ' + (e && e.message)); if (window.console) console.error('[V2] ' + nombre, e); } }
  function renderTick(qs) {
    D.quotes = qs; qs.forEach(function (q) { D.q[q.sym] = q; });
    seguro('watchlist', renderWatch); seguro('cuenta', renderAcct); seguro('simbolo', renderSymbol);
    seguro('graficos', drawCharts); seguro('ticket', updateTicket);
    // La tabla de posiciones se refresca máx. 1 vez/seg y nunca con el mouse presionado (para no perder clics).
    if (S.tab === 'pos' && D.positions.length && !punteroAbajo && Date.now() - tablaUlt > 1000) { tablaUlt = Date.now(); seguro('posiciones', renderTabBody); }
  }
  // Batch visual refreshes; never replace controls during an active pointer gesture.
  var queuedQuotes = null, tickTimer = null;
  function onTick(qs) {
    queuedQuotes = qs;
    if (tickTimer !== null) return;
    function flush() {
      if (punteroAbajo || arrastre) { tickTimer = setTimeout(flush, 100); return; }
      tickTimer = null;
      var latest = queuedQuotes; queuedQuotes = null;
      if (latest) renderTick(latest);
    }
    tickTimer = setTimeout(flush, 100);
  }
  var punteroAbajo = false, tablaUlt = 0, arrastre = null;
  document.addEventListener('pointerdown', function (e) {
    var pill = e.target.closest && e.target.closest('[data-drag]'); if (!pill) return;
    var P = panels.find(function (x) { return x.overlay.contains(pill); }); if (!P || !P.aPrecio) return;
    e.preventDefault();
    arrastre = { P: P, el: pill, k: pill.dataset.drag, id: pill.dataset.id, precio: null, movio: false };
    try { pill.setPointerCapture(e.pointerId); } catch (_) {}
  });
  document.addEventListener('pointermove', function (e) {
    if (!arrastre) return;
    var r = arrastre.P.plot.getBoundingClientRect(), yy = Math.max(0, Math.min(r.height, e.clientY - r.top));
    var crudo = arrastre.P.aPrecio(yy); if (crudo === null || !isFinite(crudo)) return;
    var precio = Number(crudo.toFixed(arrastre.P.dg));
    if (arrastre.el.dataset.temp === '1' && arrastre.P.temp) arrastre.P.temp.precio = precio;
    arrastre.precio = precio; arrastre.movio = true;
    arrastre.el.style.top = (yy - 10) + 'px';
    arrastre.el.firstChild && (arrastre.el.firstChild.textContent = arrastre.k.toUpperCase() + ' ' + px(D.positions.length ? (D.positions.find(function (p) { return String(p.id) === String(arrastre.id); }) || {}).sym : S.sym, precio) + ' ');
  });
  document.addEventListener('pointerup', function () {
    if (!arrastre) return;
    var a = arrastre; arrastre = null;
    var esTemp = a.el.dataset.temp === '1';
    if (!a.movio || a.precio === null) { drawCharts(); return; }
    if (esTemp) a.P.temp = null;
    var p = D.positions.find(function (x) { return String(x.id) === String(a.id); });
    if (!p) { drawCharts(); return; }
    var q = D.q[p.sym] || {}, ref = p.side === 'buy' ? q.bid : q.ask;
    if (ref !== null && ref !== undefined) {
      var mal = (a.k === 'sl' && (p.side === 'buy' ? a.precio >= ref : a.precio <= ref)) || (a.k === 'tp' && (p.side === 'buy' ? a.precio <= ref : a.precio >= ref));
      if (mal) { toast((a.k === 'sl' ? 'El Stop Loss' : 'El Take Profit') + ' quedaría del lado equivocado del precio'); drawCharts(); return; }
    }
    drawCharts();
    openStopEditor(p, { k: a.k, price: a.precio });
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { var q = false; panels.forEach(function (P) { if (P.temp) { P.temp = null; q = true; } }); if (q) drawCharts(); } });
  document.addEventListener('pointerdown', function () { punteroAbajo = true; }, true);
  document.addEventListener('pointerup', function () { setTimeout(function () { punteroAbajo = false; }, 50); }, true);
  function clock() { var c = $('#v2-clock'); if (c) c.textContent = new Date().toLocaleTimeString('es-US'); }

  function init() {
    document.documentElement.setAttribute('data-theme', getTheme());
    API.getQuotes().then(function (qs) {
      D.quotes = qs; qs.forEach(function (q) { D.q[q.sym] = q; });
      return Promise.all(qs.map(function (q) { return API.getCandles(q.sym, '1H', 30).then(function (c) { D.spark[q.sym] = c; }); }));
    }).then(refreshTrading).then(function () {
      setTheme(getTheme());
      var cfg = leerCfg(); if ([1, 2, 3, 4].indexOf(cfg.layout) >= 0) S.layout = cfg.layout; if (Array.isArray(cfg.lista)) S.lista = cfg.lista; if (cfg.fr) S.fr = cfg.fr;
      divisoresPantalla();
      renderWatch(); renderToolbar(); renderTicket(); buildCharts();
      window.MB_V2_REFRESH = refreshTrading;
      window.MB_V2_TOAST = function (txt, val) { toast(txt, val); log(txt + (typeof val === 'number' ? ' ' + money(val) : '')); };
      window.MB_V2_REFRESH_TICKET = function () { seguro('ticket', updateTicket); };
      API.onTick(onTick);
      clock(); setInterval(clock, 1000);
    });
    var rt; window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(drawCharts, 80); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();

/* =====================================================================
   Money Broker — Desktop V2 · adapter_ws.js  (ETAPA 2: ÓRDENES CONECTADAS)
   ---------------------------------------------------------------------
   Puente entre la interfaz V2 (desktop-v2.js) y el motor ACTUAL.
   Usa exactamente el mismo WebSocket que el escritorio actual
   (/ws/trading/<cuenta>/) y los mismos mensajes que ya existen:
     envía:   change_symbol, change_timeframe, get_positions,
              get_closed_trades, account:get, ping
     recibe:  tick/price, history, candle_update/new, positions,
              pending_orders, closed_trades_snapshot, account:update/snapshot
   ÓRDENES (etapa 2): envía EXACTAMENTE los mismos mensajes que el escritorio actual:
     order:new {symbol, side, type:'market', qty, sl, tp[, risk_confirmed]}
     order:pending:new {symbol, side, order_type, qty, trigger_price, sl, tp}
     order:close {id}   ·   order:update {id, symbol, sl, tp}
     order:pending:cancel {id}   ·   order:risk_preview {symbol, qty}
   Todas las validaciones (margen, riesgo, límites) las hace el MOTOR; este archivo
   solo envía y espera su respuesta (order_ack/order_fill, error, risk_warning, ...).
   NO calcula P&L, margen ni equity propios: muestra los del motor.
   Requiere: trading_core.js (cargado antes) y window.MB_V2_BOOT del template.
   ===================================================================== */
window.MB_API = (function () {
  'use strict';
  var BOOT = window.MB_V2_BOOT || {};
  var LABELS = { max_positions: 'Límite de posiciones alcanzado', insufficient_margin: 'Margen insuficiente',
    min_qty_violation: 'Tamaño mínimo no alcanzado', rate_limited: 'Demasiadas órdenes', extreme_risk: 'Riesgo extremo',
    max_lot_size: 'Tamaño de lote excedido', trigger_already_met: 'El precio de disparo ya fue alcanzado',
    invalid_trigger_price: 'Precio de disparo inválido', close_failed: 'No se pudo cerrar la posición' };
  function textoError(m) { return (m && (LABELS[m.code] || m.message || m.code)) || 'Error del motor'; }

  /* --------------------------- Símbolos --------------------------- */
  var NOMBRES = { 'BTC/USD': ['Bitcoin', '₿'], 'ETH/USD': ['Ethereum', 'Ξ'], 'EUR/USD': ['Euro / Dólar', '€'],
    'GBP/USD': ['Libra / Dólar', '£'], 'USD/JPY': ['Dólar / Yen', '¥'], 'AUD/USD': ['Aussie / Dólar', 'A$'],
    'XAU/USD': ['Oro', 'Au'], 'USD/CAD': ['Dólar / CAD', 'C$'], 'USD/CHF': ['Dólar / Franco', 'Fr'],
    'NZD/USD': ['Kiwi / Dólar', 'NZ$'] };
  var simbolos = (BOOT.symbols || []).map(function (s) { return s.symbol; });
  if (!simbolos.length) simbolos = ['BTC/USD'];
  function v2(sym) { return String(sym).replace('/', ''); }          // 'BTC/USD' -> 'BTCUSD'
  var real = {}; simbolos.forEach(function (s) { real[v2(s)] = s; });  // 'BTCUSD' -> 'BTC/USD'
  function kindOf(s) { return /BTC|ETH|SOL|XAU/.test(s) ? 'crypto' : /\/JPY$/.test(s) ? 'jpy' : 'fx'; }
  function digitsOf(s) { try { return priceFormatFor(s).precision; } catch (e) { return kindOf(s) === 'fx' ? 5 : kindOf(s) === 'jpy' ? 3 : 2; } }

  var quotes = simbolos.map(function (s) {
    var nm = NOMBRES[s] || [s, s.slice(0, 2)];
    return { sym: v2(s), name: nm[0], badge: nm[1], kind: kindOf(s), digits: digitsOf(s), bid: null, ask: null, chg: 0 };
  });
  var bySym = {}; quotes.forEach(function (q) { bySym[q.sym] = q; });

  /* --------------------------- Estado --------------------------- */
  var TF = { '1m': '1m', '5m': '5m', '15m': '15m', '1H': '1h', '4H': '4h', '1D': '1d' };
  var inicial = simbolos.indexOf('BTC/USD') >= 0 ? 'BTC/USD' : simbolos[0];
  var cur = { sym: inicial, tf: '15m' };
  var liveMid = null;
  var account = { id: BOOT.accountId || null, type: BOOT.accountType || '', balance: Number(BOOT.balance || 0),
    leverage: BOOT.leverage || null, server: 'LIVE', connected: false,
    equity: null, margin_used: null, free_margin: null, margin_level: null, pnl_unreal: null };
  var positions = [], orders = [], history = [];
  var candles = {};            // 'BTCUSD|15m' -> [{t,o,h,l,c,v}]
  var esperando = {};          // key -> [resolve]
  var tickCbs = [];

  /* Historial sembrado por el servidor al cargar la página (el mismo que usa el escritorio actual) */
  try { (BOOT.closedTrades || []).forEach(function (t) { var c = mapClosed(t); if (c) history.push(c); }); } catch (e) {}

  /* --------------------------- Conversores --------------------------- */
  function num(v) { var n = Number(v); return v === null || v === undefined || v === '' || !isFinite(n) ? null : n; }
  function ms(t) { if (t === null || t === undefined) return null; if (typeof t === 'string' && isNaN(Number(t))) { var d = Date.parse(t); return isNaN(d) ? null : d; } var n = Number(t); return n > 1e12 ? n : n * 1000; }
  function mapCandle(c) { return { t: ms(c.time), o: num(c.open), h: num(c.high), l: num(c.low), c: num(c.close), v: num(c.volume) || 0 }; }
  function mapPos(p) {
    return { id: p.id, sym: v2(p.symbol), side: String(p.side).toLowerCase() === 'sell' ? 'sell' : 'buy', lots: num(p.qty),
      entry: num(p.avg !== undefined ? p.avg : p.entry_price), sl: num(p.sl), tp: num(p.tp), time: ms(p.opened_at) || Date.now(),
      pnl: num(p.pnl) };   // P&L del MOTOR (null si el motor no tiene precio fresco)
  }
  function mapOrder(o) {
    return { id: o.id, sym: v2(o.symbol), side: String(o.side).toLowerCase() === 'sell' ? 'sell' : 'buy',
      type: String(o.order_type || '').toLowerCase().indexOf('stop') >= 0 ? 'stop' : 'limit', lots: num(o.qty),
      price: num(o.trigger_price), sl: num(o.sl), tp: num(o.tp), time: ms(o.created_ts) || Date.now() };
  }
  function mapClosed(t) {
    if (!t) return null;
    return { id: t.id, sym: v2(t.symbol || ''), side: String(t.side || '').toLowerCase() === 'sell' ? 'sell' : 'buy', lots: num(t.qty),
      entry: num(t.entry), exit: num(t.close !== undefined ? t.close : t.exit), pnl: num(t.pnl) || 0, time: ms(t.ts) || Date.now() };
  }

  /* --------------------------- WebSocket (el mismo del escritorio actual) --------------------------- */
  var ws = null, hb = null, reintento = 1000;
  function wsUrl() {
    var path = account.id ? '/ws/trading/' + account.id + '/' : '/ws/trading/';
    var u = new URL(path, window.location.href); u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'; return u.toString();
  }
  function enviar(o) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(o)); }
  function conectar() {
    try { ws = new WebSocket(wsUrl()); } catch (e) { setTimeout(conectar, reintento); return; }
    ws.onopen = function () {
      reintento = 1000; account.connected = true; setConn(true);
      clearInterval(hb); hb = setInterval(function () { try { ws.send('{"action":"ping"}'); } catch (e) {} }, 15000);
      enviar({ action: 'change_symbol', symbol: cur.sym });
      enviar({ action: 'change_timeframe', timeframe: TF[cur.tf] || '15m' });
      enviar({ action: 'get_positions' });
      enviar({ action: 'get_closed_trades' });
      enviar({ action: 'account:get' });
    };
    ws.onclose = function () {
      account.connected = false; liveMid = null; preview = {}; previewAt = {}; nominal = {};
      if (modificacion) { var failed = modificacion; modificacion = null; terminar(failed); failed.reject(new Error("Conexión perdida. Verifica SL/TP antes de reintentar.")); }
      setConn(false); clearInterval(hb);
      reintento = Math.min(reintento * 2, 15000); setTimeout(conectar, reintento);
    };
    ws.onmessage = function (ev) { var m; try { m = JSON.parse(ev.data); } catch (e) { return; } try { manejar(m); } catch (e) { if (window.console) console.warn('[V2] mensaje no procesado', m && m.type, e); } };
  }
  function setConn(ok) {
    var d = document.getElementById('v2-conn'); if (d) d.style.background = ok ? '' : 'var(--sell, #e5484d)';
    // Aviso visible y botones de operar bloqueados mientras no hay conexión (las órdenes igual las rechaza placeOrder)
    clearTimeout(setConn.t);
    if (ok) { document.body.classList.remove('v2-offline'); if (setConn.caida) { setConn.caida = false; if (window.MB_V2_TOAST) window.MB_V2_TOAST('Conexión restablecida'); refrescar(); } }
    else { setConn.caida = true; setConn.t = setTimeout(function () { if (!conectado()) document.body.classList.add('v2-offline'); }, 1500); }
  }

  var refrescoPend = null;
  function refrescar() { clearTimeout(refrescoPend); refrescoPend = setTimeout(function () { if (window.MB_V2_REFRESH) window.MB_V2_REFRESH(); }, 120); }
  var tickPend = false;
  function emitirTick() {
    if (tickPend) return; tickPend = true;
    var correr = function () { tickPend = false; var snap = quotes.map(function (q) { return Object.assign({}, q); }); tickCbs.forEach(function (cb) { try { cb(snap); } catch (e) {} }); };
    if (window.requestAnimationFrame && !document.hidden) window.requestAnimationFrame(correr); else setTimeout(correr, 50);
  }

  /* --------------------------- Órdenes en curso --------------------------- */
  var enCurso = null;          // orden de mercado o pendiente: { tipo, resolve, reject, payload, timer, ack }
  var cierres = [];            // [{ id, resolve, reject, timer }]
  var cancelaciones = [];      // [{ id, resolve, reject, timer }]
  var modificacion = null;
  var previewAt = {};
  var preview = {};            // 'BTC/USD|0.01' -> margin_required (del motor)
  var nominal = {};            // 'BTC/USD|0.01' -> notional (del motor)
  function terminar(o) { if (o && o.timer) clearTimeout(o.timer); }

  function respuestaOrden(m) {
    var t = m.type;
    if (modificacion && (t === 'error' || (t === 'warn' && m.message === 'order_update_not_found'))) {
      var failed = modificacion; modificacion = null; terminar(failed); failed.reject(new Error(textoError(m))); return true;
    }
    if (enCurso && enCurso.tipo === 'mercado') {
      if (t === 'order_fill') { var o = enCurso; enCurso = null; terminar(o); o.resolve({ kind: 'position', item: { id: m.order_id, sym: v2(m.symbol || cur.sym), entry: num(m.price) } }); return false; }
      if (t === 'order_ack') {    // si el fill no llega en 1.5s, se da por aceptada con el ack del motor
        enCurso.ack = m; var o2 = enCurso;
        setTimeout(function () { if (enCurso === o2) { enCurso = null; terminar(o2); o2.resolve({ kind: 'position', item: { id: m.order_id, sym: v2(m.symbol || cur.sym), entry: liveMid } }); } }, 1500);
        return false;
      }
      if (t === 'risk_warning') {
        var o3 = enCurso;
        var ok = window.confirm('Aviso de riesgo del motor:\n\nEsta operación usa ' + (m.exposure_pct || 0) + '% de tu equity.\n' +
          'Margen requerido: $' + Number(m.margin_required || 0).toFixed(2) + '\nUn movimiento del 2% en contra: −$' + Number(m.est_adverse_loss || 0).toFixed(2) +
          '\n\n¿Confirmar igualmente?');
        if (ok) { var p = Object.assign({}, o3.payload, { risk_confirmed: true }); o3.payload = p; enviar(p); }
        else { enCurso = null; terminar(o3); o3.reject(new Error('Orden cancelada')); }
        return true;
      }
      if (t === 'order_rejected') { var o4 = enCurso; enCurso = null; terminar(o4); o4.reject(new Error(m.code === 'extreme_risk' ? 'Riesgo extremo: exposición ' + m.exposure_pct + '%. Reduce el tamaño.' : textoError(m))); return true; }
      if (t === 'error') { var o5 = enCurso; enCurso = null; terminar(o5); o5.reject(new Error('Orden rechazada: ' + textoError(m))); return true; }
    }
    if (enCurso && enCurso.tipo === 'pendiente') {
      if (t === 'order_pending_new') { var o6 = enCurso; enCurso = null; terminar(o6); o6.resolve({ kind: 'order', item: { id: m.id, sym: v2(cur.sym), price: num(m.trigger_price) } }); return true; }
      if (t === 'error_pending' || t === 'error') { var o7 = enCurso; enCurso = null; terminar(o7); o7.reject(new Error('Orden pendiente rechazada: ' + textoError(m))); return true; }
    }
    if (t === 'order_close' && cierres.length) {
      var idx = -1;
      for (var i = 0; i < cierres.length; i++) if (m.id !== undefined && String(cierres[i].id) === String(m.id)) { idx = i; break; }
      if (idx < 0) return false; // Another device may close a different position.
      var c = cierres.splice(idx, 1)[0]; terminar(c);
      var pnlC = m.realized_pnl !== undefined ? m.realized_pnl : (m.pnl !== undefined ? m.pnl : m.profit);
      c.resolve({ pnl: num(pnlC) });
      m.__propio = true;
      return false;   // también se procesa como evento normal (refresca posiciones)
    }
    if (t === 'error' && cierres.length && !enCurso) { var c2 = cierres.shift(); terminar(c2); c2.reject(new Error(textoError(m))); return true; }
    if (t === 'order_pending_cancel' && cancelaciones.length) {
      var j = 0; for (var k = 0; k < cancelaciones.length; k++) if (m.id !== undefined && String(cancelaciones[k].id) === String(m.id)) { j = k; break; }
      var cc = cancelaciones.splice(j, 1)[0]; terminar(cc); cc.resolve(true); return false;
    }
    if (t === 'error_pending' && cancelaciones.length && !enCurso) { var cc2 = cancelaciones.shift(); terminar(cc2); cc2.reject(new Error(textoError(m))); return true; }
    if (t === 'risk_preview' && m.symbol) { var mr = m.margin_required !== undefined ? m.margin_required : m.new_margin_required; preview[m.symbol + '|' + Number(m.qty)] = num(mr); previewAt[m.symbol + '|' + Number(m.qty)] = Date.now(); nominal[m.symbol + '|' + Number(m.qty)] = num(m.notional); if (window.MB_V2_REFRESH_TICKET) window.MB_V2_REFRESH_TICKET(); return true; }
    return false;
  }

  function manejar(m) {
    var t = m.type;
    if (respuestaOrden(m)) return;
    if (t === 'price' || t === 'tick') {
      var sym = m.symbol || cur.sym; if (sym !== cur.sym) return;
      var b = num(m.bid !== undefined ? m.bid : m.best_bid), a = num(m.ask !== undefined ? m.ask : m.best_ask);
      // Misma compuerta que el escritorio actual (FIX-05C): solo precios con fuente real, nunca "sim".
      var st = (typeof applyPriceTickState === 'function') ? applyPriceTickState(liveMid, b, a, m.source) : (b && a && m.source && m.source !== 'sim' ? { bid: b, ask: a, liveMid: (a + b) / 2 } : null);
      if (!st) return;
      liveMid = st.liveMid;
      var q = bySym[v2(sym)]; if (!q) return;
      q.bid = st.bid; q.ask = st.ask;
      emitirTick(); return;
    }
    if (t === 'history' && Array.isArray(m.data)) {
      if (m.symbol && m.symbol !== cur.sym) return;
      if (m.timeframe && m.timeframe !== TF[cur.tf]) return;
      var key = v2(cur.sym) + '|' + cur.tf;
      var arr = m.data.map(mapCandle).filter(function (c) { return c.t && c.o !== null && c.h !== null && c.l !== null && c.c !== null; });
      arr.sort(function (x, y) { return x.t - y.t; });
      if (!candles[key]) candles[key] = [];
      var lista0 = candles[key]; lista0.length = 0; Array.prototype.push.apply(lista0, arr);   // mismo arreglo: el gráfico se actualiza solo
      calcularCambio(v2(cur.sym), lista0);
      (esperando[key] || []).forEach(function (r) { r(lista0); }); esperando[key] = [];
      emitirTick(); return;
    }
    if ((t === 'candle_update' || t === 'candle_new') && m.data) {
      if (m.symbol && m.symbol !== cur.sym) return;
      var k = v2(cur.sym) + '|' + cur.tf, lista = candles[k]; if (!lista) return;
      var c = mapCandle(m.data); if (!c.t || c.c === null) return;
      var ult = lista[lista.length - 1];
      if (!ult || c.t > ult.t) { if (!c.v) c.v = 0; lista.push(c); }
      else if (c.t === ult.t) { ult.o = c.o; ult.h = c.h; ult.l = c.l; ult.c = c.c; if (c.v) ult.v = c.v; }   // se modifica en el mismo objeto
      emitirTick(); return;
    }
    // Volumen de la vela en formación: el motor lo manda aparte (volume_update), igual que al escritorio clásico
    if (t === 'volume_update') {
      if (m.symbol && m.symbol !== cur.sym) return;
      var kv = v2(cur.sym) + '|' + cur.tf, lv = candles[kv], tv = ms(m.time), vv = num(m.value); if (!lv || !tv || vv === null) return;
      for (var iv = lv.length - 1; iv >= 0 && iv >= lv.length - 3; iv--) { if (lv[iv].t === tv) { lv[iv].v = vv; break; } }
      return;
    }
    if (t === 'positions' && Array.isArray(m.items)) { positions = m.items.map(mapPos);
      if (modificacion) {
        var update = modificacion, match = positions.filter(function (p) { return String(p.id) === String(update.id); })[0];
        if (match && match.sl === update.sl && match.tp === update.tp) {
          modificacion = null; terminar(update); update.resolve(copia(match));
        }
      }
      refrescar(); return; }
    if (t === 'pending_orders' && Array.isArray(m.items)) { orders = m.items.map(mapOrder); refrescar(); return; }
    if (t === 'closed_trades_snapshot' && Array.isArray(m.trades)) {
      history = m.trades.map(mapClosed).filter(Boolean).sort(function (x, y) { return y.time - x.time; }); refrescar(); return;
    }
    if (t === 'account:update' || t === 'account:snapshot') {
      ['balance', 'equity', 'margin_used', 'free_margin', 'margin_level', 'leverage', 'pnl_unreal'].forEach(function (f) { if (m[f] !== undefined) account[f] = num(m[f]); });
      if (m.account_type) account.type = m.account_type;
      refrescar(); return;
    }
    if (t === 'order_close' && !m.__propio) {   // cierre hecho por el servidor: SL, TP o stop-out
      var pc = m.realized_pnl !== undefined ? m.realized_pnl : (m.pnl !== undefined ? m.pnl : m.profit);
      aviso((m.partial ? 'Cierre parcial' : 'Posición cerrada por el servidor') + (m.symbol ? ' · ' + v2(m.symbol) : '') + (m.reason ? ' (' + m.reason + ')' : ''), num(pc));
    }
    if (t === 'order_close' || t === 'order_ack' || t === 'order_fill') { enviar({ action: 'get_positions' }); enviar({ action: 'get_closed_trades' }); return; }
    if (t === 'order_pending_new' || t === 'order_pending_cancel' || t === 'order_pending_update') { enviar({ action: 'get_positions' }); return; }
    /* Eventos de cuenta: nunca pasan en silencio (mismos textos que el escritorio clásico) */
    if (t === 'account:suspended') {
      var R = { stopout: '⛔ STOPOUT — Cuenta suspendida', daily_loss_limit: '⚠️ Límite de pérdida diaria alcanzado', max_drawdown: '⚠️ Drawdown máximo alcanzado', max_lot_size: '⚠️ Tamaño de lote excedido' };
      alerta(R[m.reason] || ('⛔ Cuenta suspendida: ' + String(m.reason || ''))); enviar({ action: 'get_positions' }); return;
    }
    if (t === 'account:stopout') {
      var nivel = m.stopout_level !== undefined && m.stopout_level !== null ? Number(m.stopout_level).toFixed(0) + '%' : 'el límite';
      alerta(m.partial ? '⛔ STOP-OUT PARCIAL — se liquidaron ' + (m.closed_count !== undefined ? m.closed_count : '?') + ' posición(es); ' + (m.remaining_count !== undefined ? m.remaining_count : '?') + ' sigue(n) abierta(s) (margen bajo ' + nivel + ')'
        : '⛔ STOP-OUT — posiciones liquidadas (nivel de margen bajo ' + nivel + ')');
      enviar({ action: 'get_positions' }); enviar({ action: 'get_closed_trades' }); return;
    }
    if (t === 'account:violated') { alerta('🚫 Cuenta violada: ' + String(m.reason || m.status || '')); return; }
    if (t === 'info' || t === 'warn') { if (m.message) aviso((t === 'warn' ? '⚠️ ' : '') + m.message); return; }
    if (t === 'error' || t === 'error_pending') { aviso('⚠️ ' + textoError(m)); return; }
  }
  function aviso(txt, val) { if (window.MB_V2_TOAST) window.MB_V2_TOAST(txt, val); else if (window.console) console.warn('[V2]', txt); }
  function alerta(txt) {
    var bar = document.getElementById('v2-alerta');
    if (!bar) {
      bar = document.createElement('div'); bar.id = 'v2-alerta'; bar.setAttribute('role', 'alert');
      bar.style.cssText = 'display:flex;align-items:center;gap:12px;padding:10px 14px;font-weight:600;font-size:13px;background:#3A0D12;color:#FFD5DA;border-bottom:1px solid #8A1F2B';
      var app = document.querySelector('.v2-app'); if (app) app.insertBefore(bar, app.firstChild); else document.body.insertBefore(bar, document.body.firstChild);
    }
    bar.textContent = txt;
    var x = document.createElement('button'); x.textContent = 'Entendido'; x.style.cssText = 'margin-left:auto;background:transparent;color:inherit;border:1px solid currentColor;border-radius:6px;padding:4px 10px;cursor:pointer';
    x.onclick = function () { bar.remove(); }; bar.appendChild(x);
    aviso(txt);
  }

  function calcularCambio(sym, arr) {
    // Cambio aprox. de 24h con las velas que ya cargó el gráfico (solo informativo).
    var q = bySym[sym]; if (!q || !arr.length) return;
    var ultimo = arr[arr.length - 1], objetivo = ultimo.t - 864e5, base = null;
    for (var i = arr.length - 1; i >= 0; i--) { if (arr[i].t <= objetivo) { base = arr[i]; break; } }
    if (base && base.c) q.chg = (ultimo.c / base.c - 1) * 100;
  }

  /* Una sola conexión = un símbolo a la vez (igual que el escritorio actual).
     Si en el mismo instante se piden varios símbolos (gráficos ×2/×3/×4),
     se atiende el primero y los demás reciben vacío hasta que los elijas. */
  var turno = null;
  function getCandles(sym, tf, n) {
    var symReal = real[sym]; if (!symReal) return Promise.resolve([]);
    var key = sym + '|' + tf;
    var esActual = symReal === cur.sym && tf === cur.tf;
    if (n < 100 && !esActual) return Promise.resolve(candles[key] ? candles[key].slice(-n) : []);   // mini-gráficos: sin cambiar de símbolo
    if (esActual && candles[key] && candles[key].length) return Promise.resolve(candles[key]);   // referencia viva
    if (turno && turno !== key) return Promise.resolve(candles[key] ? candles[key].slice(-n) : []);
    turno = key; setTimeout(function () { turno = null; }, 0);
    if (!esActual) {
      var cambioSym = symReal !== cur.sym;
      cur.sym = symReal; cur.tf = tf; liveMid = null;
      if (cambioSym) enviar({ action: 'change_symbol', symbol: cur.sym });
      enviar({ action: 'change_timeframe', timeframe: TF[tf] || '15m' });
    }
    if (!candles[key]) candles[key] = [];
    return new Promise(function (resolve) {
      (esperando[key] = esperando[key] || []).push(function (arr) { resolve(arr); });
      setTimeout(function () { resolve(candles[key]); }, 9000);   // aunque tarde, es el mismo arreglo que se llenará después
    });
  }

  /* --------------------------- Gráficos adicionales (×2/×3/×4) ---------------------------
     Igual que el escritorio actual: cada gráfico extra abre SU PROPIA conexión al mismo
     WebSocket (/ws/trading/<cuenta>/) y solo pide change_symbol + change_timeframe.
     Es de SOLO LECTURA: nunca envía órdenes; lee precio (misma compuerta FIX-05C) y velas.
     Ignora posiciones/cuenta/órdenes: eso lo maneja únicamente la conexión principal. */
  var lectores = {};   // 'ETH/USD' -> lector
  function crearLector(symReal, tf) {
    var L = { sym: symReal, tf: tf, ws: null, hb: null, retry: 1000, cerrado: false, mid: null };
    function env(o) { if (L.ws && L.ws.readyState === WebSocket.OPEN) L.ws.send(JSON.stringify(o)); }
    function abrir() {
      if (L.cerrado) return;
      try { L.ws = new WebSocket(wsUrl()); } catch (e) { setTimeout(abrir, L.retry); return; }
      L.ws.onopen = function () {
        if (L.cerrado) { try { L.ws.close(); } catch (e) {} return; }
        L.retry = 1000; clearInterval(L.hb); L.hb = setInterval(function () { env({ action: 'ping' }); }, 15000);
        env({ action: 'change_symbol', symbol: L.sym }); env({ action: 'change_timeframe', timeframe: TF[L.tf] || '15m' });
      };
      L.ws.onclose = function () {
        clearInterval(L.hb); L.mid = null;
        var q = bySym[v2(L.sym)]; if (q && L.sym !== cur.sym) { q.bid = null; q.ask = null; emitirTick(); }   // sin conexión = sin precio (FIX-05C)
        if (!L.cerrado) { L.retry = Math.min(L.retry * 2, 15000); setTimeout(abrir, L.retry); }
      };
      L.ws.onmessage = function (ev) { if (L.cerrado) return; var m; try { m = JSON.parse(ev.data); } catch (e) { return; } try { lectorMensaje(L, m); } catch (e) {} };
    }
    L.setTF = function (t2) { if (t2 === L.tf) return; L.tf = t2; env({ action: 'change_timeframe', timeframe: TF[t2] || '15m' }); };
    L.cerrar = function () { L.cerrado = true; clearInterval(L.hb); try { L.ws && L.ws.close(); } catch (e) {} };
    abrir(); return L;
  }
  function lectorMensaje(L, m) {
    var t = m.type, key = v2(L.sym) + '|' + L.tf;
    if (L.sym === cur.sym) return;   // ese símbolo ya lo atiende la conexión principal
    if (t === 'price' || t === 'tick') {
      if ((m.symbol || L.sym) !== L.sym) return;
      var b = num(m.bid !== undefined ? m.bid : m.best_bid), a = num(m.ask !== undefined ? m.ask : m.best_ask);
      var st = (typeof applyPriceTickState === 'function') ? applyPriceTickState(L.mid, b, a, m.source) : (b && a && m.source && m.source !== 'sim' ? { bid: b, ask: a, liveMid: (a + b) / 2 } : null);
      if (!st) return;
      L.mid = st.liveMid; var q = bySym[v2(L.sym)]; if (!q) return;
      q.bid = st.bid; q.ask = st.ask; emitirTick(); return;
    }
    if (t === 'history' && Array.isArray(m.data)) {
      if (m.symbol && m.symbol !== L.sym) return;
      if (m.timeframe && m.timeframe !== TF[L.tf]) return;
      var arr = m.data.map(mapCandle).filter(function (c) { return c.t && c.o !== null && c.h !== null && c.l !== null && c.c !== null; });
      arr.sort(function (x, y) { return x.t - y.t; });
      if (!candles[key]) candles[key] = [];
      var lista0 = candles[key]; lista0.length = 0; Array.prototype.push.apply(lista0, arr);
      calcularCambio(v2(L.sym), lista0);
      (esperando[key] || []).forEach(function (r) { r(lista0); }); esperando[key] = [];
      emitirTick(); return;
    }
    if ((t === 'candle_update' || t === 'candle_new') && m.data) {
      if (m.symbol && m.symbol !== L.sym) return;
      var lista = candles[key]; if (!lista) return;
      var c = mapCandle(m.data); if (!c.t || c.c === null) return;
      var ult = lista[lista.length - 1];
      if (!ult || c.t > ult.t) { if (!c.v) c.v = 0; lista.push(c); }
      else if (c.t === ult.t) { ult.o = c.o; ult.h = c.h; ult.l = c.l; ult.c = c.c; if (c.v) ult.v = c.v; }
      emitirTick(); return;
    }
    if (t === 'volume_update') {
      if (m.symbol && m.symbol !== L.sym) return;
      var lv = candles[key], tv = ms(m.time), vv = num(m.value); if (!lv || !tv || vv === null) return;
      for (var iv = lv.length - 1; iv >= 0 && iv >= lv.length - 3; iv--) { if (lv[iv].t === tv) { lv[iv].v = vv; break; } }
    }
  }
  /* La interfaz indica qué símbolos tienen gráfico extra; se abren/cierran las conexiones necesarias. */
  function setPanelFeeds(syms, tf) {
    var quiero = {};
    (syms || []).forEach(function (s) { var r = real[s]; if (r) quiero[r] = 1; });   // la interfaz nunca incluye el gráfico activo
    Object.keys(lectores).forEach(function (r) { if (!quiero[r]) { lectores[r].cerrar(); delete lectores[r]; } });
    Object.keys(quiero).forEach(function (r) { if (lectores[r]) lectores[r].setTF(tf); else lectores[r] = crearLector(r, tf); });
  }
  function getPanelCandles(sym, tf) {
    var r = real[sym]; if (!r) return Promise.resolve([]);
    if (!lectores[r]) lectores[r] = crearLector(r, tf); else lectores[r].setTF(tf);
    var key = sym + '|' + tf;
    if (candles[key] && candles[key].length) return Promise.resolve(candles[key]);
    if (!candles[key]) candles[key] = [];
    return new Promise(function (resolve) {
      (esperando[key] = esperando[key] || []).push(function (arr) { resolve(arr); });
      setTimeout(function () { resolve(candles[key]); }, 9000);
    });
  }

  function copia(o) { return JSON.parse(JSON.stringify(o)); }

  /* --------------------------- Acciones (mismos mensajes del escritorio actual) --------------------------- */
  function conectado() { return ws && ws.readyState === WebSocket.OPEN; }
  function placeOrder(o) {
    if (!conectado()) return Promise.reject(new Error('Sin conexión con el servidor. Reintentando…'));
    if (modificacion) return Promise.reject(new Error('Espera la confirmación de SL/TP.'));
    if (enCurso) return Promise.reject(new Error('Ya hay una orden en curso. Espera la respuesta.'));
    var symReal = real[o.sym];
    if (!symReal || symReal !== cur.sym) return Promise.reject(new Error('Abre primero el gráfico de ' + o.sym + ' para operarlo.'));
    var lots = num(o.lots);
    if (!lots || lots <= 0) return Promise.reject(new Error('Volumen inválido'));
    var sl = num(o.sl), tp = num(o.tp), payload, tipo;
    if (o.type === 'market') {
      if (liveMid === null) return Promise.reject(new Error('Precio no disponible todavía para ' + o.sym));
      payload = { action: 'order:new', symbol: symReal, side: o.side, type: 'market', qty: lots, sl: sl, tp: tp };
      tipo = 'mercado';
    } else {
      var trig = num(o.price);
      if (!trig || trig <= 0) return Promise.reject(new Error('Precio de disparo inválido'));
      payload = { action: 'order:pending:new', symbol: symReal, side: o.side, order_type: o.type, qty: lots, trigger_price: trig, sl: sl, tp: tp };
      tipo = 'pendiente';
    }
    return new Promise(function (resolve, reject) {
      enCurso = { tipo: tipo, resolve: resolve, reject: reject, payload: payload };
      enCurso.timer = setTimeout(function () {
        if (!enCurso) return; var e = enCurso; enCurso = null;
        e.reject(new Error('El servidor no respondió a tiempo. Revisa Posiciones antes de reintentar.'));
        enviar({ action: 'get_positions' });
      }, 12000);
      enviar(payload);
    });
  }
  function cerrar(id, sinConfirmar) {
    if (modificacion) return Promise.reject(new Error("Espera la confirmación de SL/TP."));
    if (!conectado()) return Promise.reject(new Error('Sin conexión con el servidor'));
    if (!sinConfirmar && !window.confirm('¿Confirmas el cierre de esta posición?')) return Promise.reject(new Error('Cierre cancelado'));
    return new Promise(function (resolve, reject) {
      var c = { id: id, resolve: resolve, reject: reject };
      c.timer = setTimeout(function () { var i = cierres.indexOf(c); if (i >= 0) { cierres.splice(i, 1); reject(new Error('Sin respuesta al cerrar. Revisa Posiciones.')); enviar({ action: 'get_positions' }); } }, 10000);
      cierres.push(c);
      enviar({ action: 'order:close', id: String(id) });
    });
  }
  function closeAll() {
    if (!positions.length) return Promise.resolve({ pnl: 0, count: 0 });
    if (!window.confirm('¿Confirmas el cierre de TODAS las posiciones abiertas (' + positions.length + ')?')) return Promise.reject(new Error('Cierre cancelado'));
    var ids = positions.map(function (p) { return p.id; }), total = 0, n = 0;
    return Promise.all(ids.map(function (id) {
      return cerrar(id, true).then(function (r) { n++; if (typeof r.pnl === 'number') total += r.pnl; }, function () {});
    })).then(function () { return { pnl: total, count: n }; });
  }
  function modifyPosition(id, sl, tp) {
    if (!conectado()) return Promise.reject(new Error('Sin conexión con el servidor'));
    var p = positions.filter(function (x) { return String(x.id) === String(id); })[0];
    if (!p) return Promise.reject(new Error('Posición no encontrada'));
    if (modificacion || enCurso || cierres.length || cancelaciones.length) return Promise.reject(new Error('Espera a que termine la operación anterior.'));
    // The existing server treats null as unchanged, never as removal.
    var nextSl = num(sl) === null ? p.sl : num(sl), nextTp = num(tp) === null ? p.tp : num(tp);
    return new Promise(function (resolve, reject) {
      var update = { id: id, sl: nextSl, tp: nextTp, resolve: resolve, reject: reject };
      modificacion = update;
      update.timer = setTimeout(function () {
        if (modificacion !== update) return;
        modificacion = null;
        reject(new Error('SL/TP sin confirmar. Revisa la posición antes de reintentar.'));
        enviar({ action: 'get_positions' });
      }, 10000);
      enviar({ action: 'order:update', id: String(id), symbol: real[p.sym] || p.sym, sl: num(sl), tp: num(tp) });
    });
  }

  function cancelOrder(id) {
    if (modificacion) return Promise.reject(new Error("Espera la confirmación de SL/TP."));
    if (!conectado()) return Promise.reject(new Error('Sin conexión con el servidor'));
    return new Promise(function (resolve, reject) {
      var c = { id: id, resolve: resolve, reject: reject };
      c.timer = setTimeout(function () { var i = cancelaciones.indexOf(c); if (i >= 0) { cancelaciones.splice(i, 1); reject(new Error('Sin respuesta al cancelar. Revisa Órdenes.')); } }, 10000);
      cancelaciones.push(c);
      enviar({ action: 'order:pending:cancel', id: String(id) });
    });
  }
  function cancelAll() {
    if (!orders.length) return Promise.resolve({ count: 0 });
    if (!window.confirm('¿Cancelar TODAS las órdenes pendientes (' + orders.length + ')?')) return Promise.reject(new Error('Cancelación anulada'));
    var n = 0;
    return Promise.all(orders.map(function (o) { return cancelOrder(o.id).then(function () { n++; }, function () {}); }))
      .then(function () { return { count: n }; });
  }
  /* Margen requerido: lo calcula el MOTOR (order:risk_preview), igual que el escritorio actual */
  var pidiendo = null;
  function marginFor(sym, lots) {
    var symReal = real[sym]; if (!symReal || symReal !== cur.sym || !lots) return null;
    var key = symReal + '|' + Number(lots);
    if (preview[key] !== undefined && Date.now() - previewAt[key] < 2000) return preview[key];
    if (pidiendo !== key) { pidiendo = key; setTimeout(function () { if (pidiendo === key) { enviar({ action: 'order:risk_preview', symbol: symReal, qty: Number(lots) }); pidiendo = null; } }, 200); }
    return null;
  }
  /* Estimación SOLO para el ticket (riesgo hasta SL / objetivo en TP), con el tamaño de contrato
     que sirve el backend (CONTRACT_SIZE). Las posiciones reales usan el P&L del MOTOR. */
  function estimar(p, q) {
    if (!q || q.bid === null || q.bid === undefined) return null;
    var symReal = real[q.sym], cs = 1;
    try { cs = getContractSize(symReal); } catch (e) {}
    var px = p.side === 'buy' ? q.bid : q.ask;
    var diff = p.side === 'buy' ? px - p.entry : p.entry - px;
    var v = diff * p.lots * cs;
    if (/^USD\//.test(symReal || '')) v = v / px;     // cotización en otra moneda (ej. USD/JPY)
    return v;
  }

  conectar();

  return {
    getAccount: function () { return Promise.resolve(copia(account)); },
    getQuotes: function () { return Promise.resolve(quotes.map(function (q) { return Object.assign({}, q); })); },
    getCandles: getCandles,
    getPanelCandles: getPanelCandles, setPanelFeeds: setPanelFeeds,
    getPositions: function () { return Promise.resolve(copia(positions)); },
    getOrders: function () { return Promise.resolve(copia(orders)); },
    getHistory: function () { return Promise.resolve(copia(history)); },
    placeOrder: placeOrder, closePosition: function (id) { return cerrar(id, false); }, closeAll: closeAll,
    modifyPosition: modifyPosition, cancelOrder: cancelOrder, cancelAll: cancelAll,
    marginFor: marginFor,
    notionalFor: function (sym, lots) { var k = (real[sym] || '') + '|' + Number(lots); return nominal[k] !== undefined && Date.now() - previewAt[k] < 2000 ? nominal[k] : null; },
    onTick: function (cb) { tickCbs.push(cb); },
    /* P&L: el de la posición lo da el MOTOR (p.pnl). Para posiciones sin ese dato
       (o estimaciones del ticket) se devuelve null: no se inventa un número. */
    pnl: function (p, q) {
      if (p && p.id !== undefined) return typeof p.pnl === 'number' ? p.pnl : null;   // posición real: P&L del motor
      return estimar(p, q);                                                           // ticket: estimación informativa
    }
  };
})();

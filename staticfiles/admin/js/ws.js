// =====================================================
// path: static/js/ws.js
// =====================================================
// Por qué: aislar reconexión/heartbeat para no duplicar en vistas.
export function buildWsUrl({ provider, token }) {
    const u = new URL('/ws/trading/', window.location.href);
    u.protocol = (u.protocol === 'https:') ? 'wss:' : 'ws:';
    if (provider) u.searchParams.set('provider', provider);
    if (token) u.searchParams.set('token', token);
    return u.toString();
  }
  
  export function connectWS({ buildUrl, onOpen, onMsg, onClose, onError }) {
    let ws=null, connecting=false, delay=800, hb=null;
    const HEARTBEAT_MS=15000, RECONNECT_MAX=5000;
  
    const open=()=>{
      if (connecting || (ws && (ws.readyState===WebSocket.OPEN || ws.readyState===WebSocket.CONNECTING))) return;
      const url=buildUrl();
      try{ ws=new WebSocket(url); }catch(e){ onError?.(e); schedule(); return; }
      connecting=true;
      ws.onopen=()=>{ connecting=false; delay=800; clearInterval(hb);
        hb=setInterval(()=>{ try{ ws.send('{"action":"ping"}'); }catch(_e){} }, HEARTBEAT_MS);
        onOpen?.(ws);
      };
      ws.onmessage=(ev)=>{ try{ onMsg?.(JSON.parse(ev.data)); }catch(e){ console.warn('ws parse',e); } };
      ws.onerror=(e)=> onError?.(e);
      ws.onclose=(ev)=>{ onClose?.(ev); schedule(); };
    };
  
    const schedule=()=>{ connecting=false; clearInterval(hb);
      setTimeout(open, Math.min(delay, RECONNECT_MAX));
      delay=Math.min(RECONNECT_MAX, delay*1.7);
    };
  
    open();
    return {
      get instance(){ return ws; },
      send: (o)=>{ if(ws?.readyState===WebSocket.OPEN) ws.send(JSON.stringify(o)); },
      close: (code=1000)=>{ try{ ws?.close(code); }catch{} }
    };
  }
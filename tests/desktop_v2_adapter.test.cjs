const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('simulator/static/simulator/desktop_v2/adapter_ws.js','utf8');
function fixture(symbols=['BTC/USD']){
 let socket, now=1700000000000;const sent=[],timers=[],sockets=[],intervals=[];
 class WS {static OPEN=1;constructor(url){this.url=url;if(!socket)socket=this;sockets.push(this);this.readyState=1;this.sent=[];}send(s){this.sent.push(JSON.parse(s));sent.push(JSON.parse(s));}close(){this.readyState=3;if(this.onclose)this.onclose();}}
 const document={hidden:true,getElementById(){return null},body:{classList:{remove(){},add(){}}}};
 const ctx={URL,WebSocket:WS,console,Date:class extends Date {static now(){return now}},setTimeout:(fn,ms)=>{const t={fn,ms};timers.push(t);return t},clearTimeout:t=>{if(t)t.off=true},setInterval:(fn,ms)=>{intervals.push({fn,ms})},clearInterval(){},document};
 ctx.window={MB_V2_BOOT:{accountId:55,symbols:symbols.map(symbol=>({symbol}))},confirm:()=>true,location:{href:'http://localhost/'},console};
 vm.createContext(ctx);vm.runInContext(source,ctx);
 const receive=m=>socket.onmessage({data:JSON.stringify(m)});
 const pos=(sl=80000,tp=100000)=>receive({type:'positions',items:[{id:7,symbol:'BTC/USD',qty:0.01,sl,tp}]});
 return {api:ctx.window.MB_API,sent,sockets,interval:ms=>intervals.filter(t=>t.ms===ms).forEach(t=>t.fn()),receive,pos,advance:n=>{now+=n},close:()=>socket.onclose(),run:ms=>{timers.filter(t=>t.ms===ms&&!t.off).forEach(t=>{t.off=true;t.fn()})}};
}
test('expired margin is hidden and requested again',()=>{const f=fixture();f.receive({type:'risk_preview',symbol:'BTC/USD',qty:0.01,margin_required:10,notional:100});assert.equal(f.api.marginFor('BTCUSD',.01),10);f.advance(2001);assert.equal(f.api.marginFor('BTCUSD',.01),null);assert.equal(f.api.notionalFor('BTCUSD',.01),null);f.run(200);assert.equal(f.sent.at(-1).action,'order:risk_preview')});
test('SL/TP waits for matching authoritative position snapshot',async()=>{const f=fixture();f.pos();let settled=false;const p=f.api.modifyPosition(7,85000,100000).then(()=>settled=true);f.pos();await Promise.resolve();assert.equal(settled,false);f.pos(85000);await p;assert.equal(settled,true)});
test('SL/TP rejection does not report success',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);f.receive({type:'error',message:'invalid_sl'});await assert.rejects(p,/invalid_sl/)});
test('SL/TP timeout is unknown, not success',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);f.run(10000);await assert.rejects(p,/sin confirmar/)});
test('disconnect rejects pending edit and clears margin',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);f.close();await assert.rejects(p,/Conexión perdida/)});
test('blank keeps existing stop values',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,null,110000);f.pos(80000,110000);const result=await p;assert.equal(result.sl,80000);assert.equal(result.tp,110000)});
test('another write cannot overlap pending SL/TP',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);await assert.rejects(f.api.placeOrder({}),/confirmación/);await assert.rejects(f.api.closePosition(7),/confirmación/);f.pos(85000);await p});

test('mobile snapshots update positions and balances in Panel 2',async()=>{const f=fixture();f.pos(85000);f.receive({type:'account:update',balance:9999,equity:10001,margin_used:20});assert.equal((await f.api.getPositions())[0].sl,85000);assert.equal((await f.api.getAccount()).balance,9999);f.receive({type:'positions',items:[]});assert.equal((await f.api.getPositions()).length,0)});
test('another device close cannot acknowledge a different pending close',async()=>{const f=fixture();f.pos();let settled=false;const p=f.api.closePosition(7).then(()=>settled=true);f.receive({type:'order_close',id:8});await Promise.resolve();assert.equal(settled,false);f.receive({type:'order_close',id:7});await p;assert.equal(settled,true)});
test('empty history snapshot clears old rows',async()=>{const f=fixture();f.receive({type:'closed_trades_snapshot',trades:[{id:1,symbol:'BTC/USD',pnl:1}]});assert.equal((await f.api.getHistory()).length,1);f.receive({type:'closed_trades_snapshot',trades:[]});assert.equal((await f.api.getHistory()).length,0)});

test('watchlist subscribes every instrument without chart selection or order messages',async()=>{
 const f=fixture(['BTC/USD','ETH/USD','EUR/USD','GBP/USD','USD/JPY','AUD/USD']);
 assert.equal(f.sockets.length,6);
 f.sockets.forEach(s=>s.onopen());
 const readers=f.sockets.slice(1);
 assert.deepEqual(readers.map(s=>s.sent.find(m=>m.action==='change_symbol').symbol).sort(),['ETH/USD','EUR/USD','GBP/USD','USD/JPY','AUD/USD'].sort());
 readers.forEach((s,i)=>s.onmessage({data:JSON.stringify({type:'tick',symbol:s.sent[0].symbol,bid:100+i,ask:101+i,source:'massive',time:1700000000})}));
 const quotes=await f.api.getQuotes();assert.equal(quotes.filter(q=>q.bid!==null).length,5);
 assert.equal(readers.flatMap(s=>s.sent).filter(m=>m.action.startsWith('order:')).length,0);
 f.api.setPanelFeeds(['ETHUSD'],'15m');f.api.setPanelFeeds([],'15m');assert.equal(f.sockets.length,6);
});
test('reader disconnect clears only its own quote',async()=>{const f=fixture(['BTC/USD','EUR/USD']);const r=f.sockets[1];r.onmessage({data:JSON.stringify({type:'tick',symbol:'EUR/USD',bid:1.1,ask:1.2,source:'massive'})});assert.equal((await f.api.getQuotes())[1].bid,1.1);r.close();assert.equal((await f.api.getQuotes())[1].bid,null)});
test('stale and simulated quotes are not presented as live',async()=>{const f=fixture(['BTC/USD','EUR/USD']);const r=f.sockets[1];r.onmessage({data:JSON.stringify({type:'tick',symbol:'EUR/USD',bid:1.1,ask:1.2,source:'sim'})});assert.equal((await f.api.getQuotes())[1].bid,null);r.onmessage({data:JSON.stringify({type:'tick',symbol:'EUR/USD',bid:1.1,ask:1.2,source:'massive',time:1700000000})});f.advance(60001);f.interval(1000);assert.equal((await f.api.getQuotes())[1].bid,null)});

test('four panels keep separate candle histories and ignore secondary account updates',async()=>{
 const f=fixture(['BTC/USD','ETH/USD','EUR/USD','GBP/USD']); f.sockets.forEach(s=>s.onopen());
 f.api.setPanelFeeds(['ETHUSD','EURUSD','GBPUSD'],'4H');
 const active=f.api.getCandles('BTCUSD','4H',220);
 const others=['ETHUSD','EURUSD','GBPUSD'].map(s=>f.api.getPanelCandles(s,'4H'));
 const symbols=['BTC/USD','ETH/USD','EUR/USD','GBP/USD'];
 f.sockets.forEach((s,i)=>s.onmessage({data:JSON.stringify({type:'history',symbol:symbols[i],timeframe:'4h',data:[{time:1700000000,open:i+1,high:i+2,low:i,close:i+1,volume:10}]})}));
 f.run(9000);
 const arrays=await Promise.all([active,...others]);
 assert.deepEqual(arrays.map(a=>a[0].c),[1,2,3,4]);
 f.receive({type:'account:update',balance:1000,equity:1000});
 f.sockets[1].onmessage({data:JSON.stringify({type:'account:update',balance:999,equity:999})});
 assert.equal((await f.api.getAccount()).balance,1000);
 assert.equal(f.sockets.slice(1).flatMap(s=>s.sent).filter(m=>m.action.startsWith('order:')).length,0);
});

test('late candle from previous timeframe must not alter the current chart',async()=>{
 const f=fixture(['BTC/USD','ETH/USD']);const r=f.sockets[1];
 const pending=f.api.getPanelCandles('ETHUSD','4H');
 r.onmessage({data:JSON.stringify({type:'history',symbol:'ETH/USD',timeframe:'4h',data:[{time:1700000000,open:1,high:2,low:1,close:2}]})});
 f.run(9000);const candles=await pending;
 r.onmessage({data:JSON.stringify({type:'candle_update',symbol:'ETH/USD',timeframe:'15m',data:{time:1700000000,open:1,high:999,low:1,close:999}})});
 assert.equal(candles[0].c,2);
});
for (const secondary of [false,true]) test((secondary?'secondary':'active')+' chart waits for matching history after timeframe change',async()=>{
 const f=fixture(['BTC/USD','ETH/USD']);f.sockets.forEach(s=>s.onopen());
 const sym=secondary?'ETHUSD':'BTCUSD', symbol=secondary?'ETH/USD':'BTC/USD';
 const send=m=>(secondary?f.sockets[1].onmessage({data:JSON.stringify(m)}):f.receive(m));
 const get=tf=>secondary?f.api.getPanelCandles(sym,tf):f.api.getCandles(sym,tf,220);
 const history=(tf,close)=>({type:'history',symbol,timeframe:tf,data:[{time:1700000000,open:1,high:close,low:1,close,volume:10}]});
 let p=get('4H');send(history('4h',4));f.run(9000);const four=await p;f.run(0);
 p=get('15m');send(history('15m',15));f.run(9000);await p;f.run(0);
 p=get('4H');
 send({type:'candle_update',symbol,data:{time:1700000000,open:1,high:99,low:1,close:99}});
 send({type:'volume_update',symbol,time:1700000000,value:99});
 assert.equal(four[0].c,4);assert.equal(four[0].v,10);
 send(history('15m',99));assert.equal(four[0].c,4);
 send(history('4h',5));f.run(9000);await p;
 send({type:'candle_update',symbol,data:{time:1700000000,open:1,high:6,low:1,close:6}});
 assert.equal(four[0].c,6);
 send({type:'volume_update',symbol,timeframe:'15m',time:1700000000,value:99});assert.equal(four[0].v,10);
});

test('changing timeframe retains a fresh executable quote for the same symbol',async()=>{
 const f=fixture();f.receive({type:'tick',symbol:'BTC/USD',bid:100,ask:101,source:'massive',time:1700000000});
 f.api.getCandles('BTCUSD','4H',220);
 const pending=f.api.placeOrder({sym:'BTCUSD',side:'buy',type:'market',lots:.01});
 f.receive({type:'order_fill',order_id:9,symbol:'BTC/USD',price:101});
 await pending;assert.ok(f.sent.some(m=>m.action==='order:new'));
});
test('timeframe change cannot reuse an expired execution quote',async()=>{
 const f=fixture();f.receive({type:'tick',symbol:'BTC/USD',bid:100,ask:101,source:'massive',time:1700000000});f.advance(60001);f.api.getCandles('BTCUSD','4H',220);
 await assert.rejects(f.api.placeOrder({sym:'BTCUSD',type:'market',side:'buy',lots:.01}),/Precio no disponible/);
 assert.equal(f.sent.filter(m=>m.action==='order:new').length,0);
});
test('switching instruments requires a fresh primary quote',async()=>{
 const f=fixture(['BTC/USD','ETH/USD']);f.sockets[1].onmessage({data:JSON.stringify({type:'tick',symbol:'ETH/USD',bid:100,ask:101,source:'massive'})});
 f.api.getCandles('ETHUSD','15m',220);
 await assert.rejects(f.api.placeOrder({sym:'ETHUSD',type:'market',side:'buy',lots:.01}),/Precio no disponible/);
 assert.equal((await f.api.getQuotes()).find(q=>q.sym==='ETHUSD').bid,null);
 f.receive({type:'tick',symbol:'ETH/USD',bid:100,ask:101,source:'massive'});
 const p=f.api.placeOrder({sym:'ETHUSD',type:'market',side:'buy',lots:.01});f.receive({type:'order_fill',order_id:1,symbol:'ETH/USD',price:101});await p;
});

test('startup requests history once per connection with timeframe in URL',()=>{
 const f=fixture(['BTC/USD','ETH/USD']);f.sockets.forEach(s=>s.onopen());
 assert.equal(new URL(f.sockets[0].url).searchParams.get('tf'),'15m');
 f.sockets.forEach(s=>assert.equal(s.sent.filter(m=>m.action==='change_timeframe').length,0));
});
test('4H changes only visible charts while hidden quotes remain connected',()=>{
 const f=fixture(['BTC/USD','ETH/USD','EUR/USD','GBP/USD','USD/JPY','AUD/USD']);f.sockets.forEach(s=>s.onopen());
 f.api.setPanelFeeds(['ETHUSD','EURUSD','GBPUSD'],'4H');f.api.getCandles('BTCUSD','4H',220);
 assert.equal(f.sockets.length,6);
 const changed=f.sockets.filter(s=>s.sent.some(m=>m.action==='change_timeframe'&&m.timeframe==='4h'));
 assert.equal(changed.length,4);assert.ok(f.sockets.every(s=>s.readyState===1));
});
test('returning to a loaded timeframe paints cached history without timeout',async()=>{
 const f=fixture();const first=f.api.getCandles('BTCUSD','4H',220);
 f.receive({type:'history',symbol:'BTC/USD',timeframe:'4h',data:[{time:1700000000,open:1,high:2,low:1,close:2}]});await first;f.run(0);
 f.api.getCandles('BTCUSD','15m',220);f.run(0);
 let done=false;f.api.getCandles('BTCUSD','4H',220).then(c=>{done=true;assert.equal(c[0].c,2)});
 await Promise.resolve();assert.equal(done,true);
});

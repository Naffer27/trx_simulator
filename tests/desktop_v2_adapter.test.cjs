const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('simulator/static/simulator/desktop_v2/adapter_ws.js','utf8');
function fixture(){
 let socket, now=10000;const sent=[],timers=[];
 class WS {static OPEN=1;constructor(){socket=this;this.readyState=1;}send(s){sent.push(JSON.parse(s));}}
 const document={hidden:true,getElementById(){return null},body:{classList:{remove(){},add(){}}}};
 const ctx={URL,WebSocket:WS,console,Date:class extends Date {static now(){return now}},setTimeout:(fn,ms)=>{const t={fn,ms};timers.push(t);return t},clearTimeout:t=>{if(t)t.off=true},setInterval(){},clearInterval(){},document};
 ctx.window={MB_V2_BOOT:{accountId:55,symbols:[{symbol:'BTC/USD'}]},location:{href:'http://localhost/'},console};
 vm.createContext(ctx);vm.runInContext(source,ctx);
 const receive=m=>socket.onmessage({data:JSON.stringify(m)});
 const pos=(sl=80000,tp=100000)=>receive({type:'positions',items:[{id:7,symbol:'BTC/USD',qty:0.01,sl,tp}]});
 return {api:ctx.window.MB_API,sent,receive,pos,advance:n=>{now+=n},close:()=>socket.onclose(),run:ms=>{timers.filter(t=>t.ms===ms&&!t.off).forEach(t=>{t.off=true;t.fn()})}};
}
test('expired margin is hidden and requested again',()=>{const f=fixture();f.receive({type:'risk_preview',symbol:'BTC/USD',qty:0.01,margin_required:10,notional:100});assert.equal(f.api.marginFor('BTCUSD',.01),10);f.advance(2001);assert.equal(f.api.marginFor('BTCUSD',.01),null);assert.equal(f.api.notionalFor('BTCUSD',.01),null);f.run(200);assert.equal(f.sent.at(-1).action,'order:risk_preview')});
test('SL/TP waits for matching authoritative position snapshot',async()=>{const f=fixture();f.pos();let settled=false;const p=f.api.modifyPosition(7,85000,100000).then(()=>settled=true);f.pos();await Promise.resolve();assert.equal(settled,false);f.pos(85000);await p;assert.equal(settled,true)});
test('SL/TP rejection does not report success',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);f.receive({type:'error',message:'invalid_sl'});await assert.rejects(p,/invalid_sl/)});
test('SL/TP timeout is unknown, not success',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);f.run(10000);await assert.rejects(p,/sin confirmar/)});
test('disconnect rejects pending edit and clears margin',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);f.close();await assert.rejects(p,/Conexión perdida/)});
test('blank keeps existing stop values',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,null,110000);f.pos(80000,110000);const result=await p;assert.equal(result.sl,80000);assert.equal(result.tp,110000)});
test('another write cannot overlap pending SL/TP',async()=>{const f=fixture();f.pos();const p=f.api.modifyPosition(7,85000,100000);await assert.rejects(f.api.placeOrder({}),/confirmación/);await assert.rejects(f.api.closePosition(7),/confirmación/);f.pos(85000);await p});

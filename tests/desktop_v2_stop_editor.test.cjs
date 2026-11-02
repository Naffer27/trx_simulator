const {test}=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
const source=fs.readFileSync('simulator/static/simulator/desktop_v2/desktop-v2.js','utf8');
function fixture(){
 const events={},submit={},calls=[];let resolve,reject;
 const position={id:7,sym:'BTCUSD',side:'buy',sl:90,tp:120};
 const controls={form:{addEventListener:(n,fn)=>submit[n]=fn},'[name="sl"]':{value:'',focus(){}},'[name="tp"]':{value:'',focus(){}},'.v2-stop-status':{},'[type="submit"]':{},'[type="button"]':{}};
 const dialog={setAttribute(){},addEventListener:(n,fn)=>events[n]=fn,showModal(){},remove(){this.removed=true},close(){events.close()}};
 const ctx={document:{activeElement:null,createElement:()=>dialog,body:{appendChild(){}}},$:s=>controls[s],esc:String,D:{positions:[position]},drawCharts(){},toast(){},log(){},refreshTrading:()=>Promise.resolve(),API:{modifyPosition:(...args)=>{calls.push(args);return new Promise((a,b)=>{resolve=a;reject=b})}}};
 vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('  var stopEditor ='),source.indexOf('  function selectInstrument(')),ctx);
 ctx.openStopEditor(position,{k:'sl',price:95});
 controls['[name="sl"]'].value=String(controls['[name="sl"]'].value);controls['[name="tp"]'].value=String(controls['[name="tp"]'].value);
 return {controls,dialog,position,calls,send:()=>submit.submit({preventDefault(){}}),resolve:()=>resolve(),reject:()=>reject(new Error('rechazado'))};
}
test('editor opens without sending; saves once and waits for confirmation',async()=>{const f=fixture();assert.equal(f.calls.length,0);f.send();f.send();assert.deepEqual(f.calls,[[7,95,120]]);assert.equal(f.dialog.removed,undefined);f.resolve();await new Promise(setImmediate);assert.equal(f.dialog.removed,true)});
test('server rejection keeps editor open and shows error',async()=>{const f=fixture();f.send();f.reject();await new Promise(setImmediate);assert.equal(f.dialog.removed,undefined);assert.equal(f.controls['.v2-stop-status'].textContent,'rechazado');assert.equal(f.controls['[type="submit"]'].disabled,false)});
test('editor does not overwrite a position changed elsewhere',()=>{const f=fixture();f.position.tp=130;f.send();assert.equal(f.calls.length,0);assert.match(f.controls['.v2-stop-status'].textContent,/posición cambió/)});
test('quote rendering batches bursts and waits until pointer gesture ends',()=>{
 const timers=[],paint=[];const ctx={setTimeout:fn=>{timers.push(fn);return timers.length},punteroAbajo:false,arrastre:null,renderTick:q=>paint.push(q)};vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('  var queuedQuotes ='),source.indexOf('  var punteroAbajo =')),ctx);
 ctx.onTick([1]);ctx.onTick([2]);assert.equal(timers.length,1);ctx.punteroAbajo=true;timers.shift()();assert.equal(paint.length,0);ctx.punteroAbajo=false;timers.shift()();assert.deepEqual(paint,[[2]]);
});

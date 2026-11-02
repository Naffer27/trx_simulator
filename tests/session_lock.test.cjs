const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('simulator/static/simulator/session_lock.js', 'utf8');
function harness() {
  let now = 100000, cleared = false;
  const handlers = {}, intervals = [], calls = [], redirects = [], storage = new Map();
  const script = {dataset: {timeout:'60', remaining:'60', session:'one',activity:'/session/activity/',lock:'/session/lock/',login:'/login/'},
    parentElement:{querySelector:()=>({value:'csrf'})}};
  const document = {currentScript:script,hidden:false,addEventListener:(n,f)=>handlers[n]=f,
    body:{replaceChildren:()=>{cleared=true},appendChild:()=>{}},createElement:()=>({})};
  const context = {document,window:{addEventListener:(n,f)=>handlers[n]=f},
    localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    Date:{now:()=>now},setInterval:(f,t)=>intervals.push({f,t}),
    URLSearchParams,AbortSignal,location:{replace:u=>redirects.push(u)},
    fetch:async(u,opts)=>{calls.push({u,opts});return {ok:true,status:200}}};
  vm.runInNewContext(source,context);
  return {handlers,intervals,calls,redirects,document,storage,setNow:v=>now=v,isCleared:()=>cleared};
}
const flush = () => new Promise(resolve=>setImmediate(resolve));
test('prices and background polling cannot renew session; timeout clears account',async()=>{
 const h=harness();h.setNow(159000);h.intervals.find(x=>x.t===3000).f();await flush();assert.equal(h.calls.length,0);
 h.setNow(160001);h.intervals.find(x=>x.t===500).f();await flush();assert.ok(h.isCleared());assert.equal(h.calls[0].u,'/session/lock/');assert.deepEqual(h.redirects,['/login/']);
});
test('trusted activity renews; programmatic events and hidden tabs do not',async()=>{
 const h=harness();h.setNow(130000);h.handlers.pointerdown({isTrusted:false});h.intervals.find(x=>x.t===3000).f();await flush();assert.equal(h.calls.length,0);
 h.handlers.pointerdown({isTrusted:true});h.setNow(132000);h.intervals.find(x=>x.t===3000).f();await flush();assert.equal(h.calls[0].u,'/session/activity/');assert.equal(h.calls[0].opts.body.get('idle_seconds'),'2');
 h.document.hidden=true;h.setNow(180000);h.handlers.pointerdown({isTrusted:true});h.setNow(190001);h.handlers.visibilitychange();await flush();assert.ok(h.isCleared());
});
test('returning from suspended/bfcache page checks wall-clock expiration',async()=>{
 const h=harness();h.setNow(300000);h.handlers.pageshow();await flush();assert.ok(h.isCleared());
});
test('expired page cannot revive itself with a click',async()=>{
 const h=harness();h.setNow(160001);h.handlers.pointerdown({isTrusted:true});await flush();assert.ok(h.isCleared());assert.ok(!h.calls.some(x=>x.u==='/session/activity/'));
});
test('tabs share human activity within the same session',async()=>{
 const h=harness();h.setNow(159000);h.storage.set('mb.activity.one','150000');h.handlers.storage();h.setNow(170000);h.intervals.find(x=>x.t===500).f();assert.ok(!h.isCleared());
});

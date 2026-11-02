const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('simulator/static/simulator/desktop_v2/desktop-v2.js','utf8');
const parser=source.slice(source.indexOf('  function parseLot('),source.indexOf('  function fieldVolume('));
const ctx={};vm.createContext(ctx);vm.runInContext(parser,ctx);
test('accepts comma and dot without silently accepting invalid lots',()=>{
 for(const [raw,value] of [['0,10',.1],['.25',.25],['1',1],['100',100]]) assert.equal(ctx.parseLot(raw),value);
 for(const raw of ['', '0','-1','101','1abc','NaN','0.001','1,000.00']) assert.equal(ctx.parseLot(raw),null);
});
test('invalid draft never reaches the order API',()=>{
 const block=source.slice(source.indexOf('  function send(side) {'),source.indexOf('    var sl = parseNum(S.slTxt)',source.indexOf('  function send(side) {')));
 let focused=false,notice=false;
 const c={lotDraft:'',parseLot:ctx.parseLot,toast:()=>notice=true,$:()=>({focus:()=>focused=true})};
 vm.createContext(c);vm.runInContext(block+'throw new Error("Should not send");\n}',c);
 c.send('buy');assert.ok(focused && notice);
});

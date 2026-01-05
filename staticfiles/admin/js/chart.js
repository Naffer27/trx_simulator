// =====================================================
// path: static/js/chart.js
// =====================================================
// Por qué: un punto para LWC + líneas + drag. Evita globals/duplicados.
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const priceFormatFor = (sym) =>
  (sym.includes('BTC')||sym.includes('ETH'))?{precision:2,minMove:0.01}:
  (sym.endsWith('/JPY')?{precision:3,minMove:0.001}:{precision:5,minMove:0.00001});

export function initChart({ containerEl, symbol, onLineChange }) {
  const chart = LightweightCharts.createChart(containerEl,{
    layout:{backgroundColor:css('--panel'),textColor:css('--text')},
    grid:{vertLines:{color:css('--grid')},horLines:{color:css('--grid')}},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    timeScale:{borderColor:css('--accent')},
    rightPriceScale:{borderColor:css('--accent'),scaleMargins:{top:0.12,bottom:0.25}}
  });

  const candles = chart.addCandlestickSeries({
    upColor:css('--bull'),downColor:css('--bear'),
    wickUpColor:css('--bull'),wickDownColor:css('--bear'),
    borderUpColor:css('--bull'),borderDownColor:css('--bear'),
    priceFormat:priceFormatFor(symbol)
  });
  const volume = chart.addHistogramSeries({
    priceScaleId:'', priceFormat:{type:'volume'}, priceLineVisible:false, lastValueVisible:false,
    color:'rgba(127,189,255,.20)'
  });
  const priceLine = candles.createPriceLine({
    price:0,color:css('--price'),lineWidth:2,lineStyle:0,axisLabelVisible:true,title:symbol.replace('/','')
  });

  const applyBarSpacing = ()=>{ const s=Math.max(10, Math.min(26, Math.floor(containerEl.clientWidth/32)));
    chart.timeScale().applyOptions({barSpacing:s,rightOffset:8}); };
  const resize = ()=>{ chart.resize(containerEl.clientWidth, containerEl.clientHeight); applyBarSpacing(); };
  new ResizeObserver(resize).observe(containerEl);

  const linesById = new Map();
  const setSymbol = (sym)=>{ symbol=sym; candles.applyOptions({priceFormat:priceFormatFor(symbol)}); priceLine.applyOptions({title:symbol.replace('/','')}); };
  const setHistory=(bars)=>{ candles.setData(bars); volume.setData(bars.map(b=>({time:b.time,value:Math.max(1,Math.floor((b.high-b.low)*1e6))})));
    chart.timeScale().fitContent(); chart.timeScale().scrollToRealTime(); };
  const updateCandle=(b)=>{ candles.update(b); chart.timeScale().scrollToRealTime(); };
  const setPrice=(px)=> priceLine.applyOptions({price:px});

  const drawLinesFor=(id,{side='buy',entry,sl=null,tp=null})=>{
    const bull=css('--bull'), bear=css('--bear'); id=String(id);
    let rec=linesById.get(id); const title=side==='buy'?'BUY':'SELL';
    if(!rec){ rec={id,side,entry,sl,tp,plEntry:null,plSL:null,plTP:null}; linesById.set(id,rec);
      rec.plEntry=candles.createPriceLine({price:entry,color:side==='buy'?bull:bear,lineWidth:2,lineStyle:2,axisLabelVisible:true,title});
    }else{ rec.side=side; rec.entry=entry; rec.plEntry?.applyOptions({price:entry,color:side==='buy'?bull:bear,title}); }
    if(sl!=null) rec.plSL ? rec.plSL.applyOptions({price:sl}) :
      (rec.plSL=candles.createPriceLine({price:sl,color:'#ff6b6b',lineWidth:1,lineStyle:1,axisLabelVisible:true,title:'SL'}));
    if(tp!=null) rec.plTP ? rec.plTP.applyOptions({price:tp}) :
      (rec.plTP=candles.createPriceLine({price:tp,color:'#6ad59f',lineWidth:1,lineStyle:1,axisLabelVisible:true,title:'TP'}));
  };
  const removeLinesFor=(id)=>{ const rec=linesById.get(String(id)); if(!rec)return;
    [rec.plEntry,rec.plSL,rec.plTP].forEach(l=>l&&candles.removePriceLine(l)); linesById.delete(String(id)); };
  const clearLines=()=>{ Array.from(linesById.keys()).forEach(removeLinesFor); };

  // Drag SL/TP (por qué: UX rápida; callback notifica al server)
  const DRAG_TOL_PX=18; let dragging=null;
  const yFromEvent=e=> (e.offsetY ?? (e.clientY - containerEl.getBoundingClientRect().top));
  const nearLineY=(price,yPx)=>{ const y=candles.priceToCoordinate(price); return y!=null && Math.abs(y-yPx)<=DRAG_TOL_PX; };
  containerEl.addEventListener('pointerdown',(e)=>{
    const y=yFromEvent(e); if(!linesById.size) return;
    // detect SL/TP cercanos de la última posición
    for(const [id,rec] of linesById.entries()){
      if(rec.sl!=null && nearLineY(rec.sl,y)){ dragging={id,which:'sl'}; break; }
      if(rec.tp!=null && nearLineY(rec.tp,y)){ dragging={id,which:'tp'}; break; }
    }
    if(dragging){ e.preventDefault(); containerEl.classList.add('dragging'); }
  },{passive:false});
  containerEl.addEventListener('pointermove',(e)=>{
    if(!dragging) return; const rec=linesById.get(dragging.id); if(!rec) return;
    const raw=candles.coordinateToPrice( yFromEvent(e) ); if(raw==null) return;
    const {minMove,precision}=priceFormatFor(symbol);
    const px=Number((Math.round(raw/minMove)*minMove).toFixed(precision));
    if(dragging.which==='sl'){ rec.sl=px; rec.plSL?.applyOptions({price:px}); }
    else{ rec.tp=px; rec.plTP?.applyOptions({price:px}); }
  });
  const endDrag=()=>{
    if(!dragging){ containerEl.classList.remove('dragging'); return; }
    const rec=linesById.get(dragging.id);
    if(rec && onLineChange){ onLineChange(rec.id, { sl: rec.sl??null, tp: rec.tp??null }); }
    dragging=null; containerEl.classList.remove('dragging');
  };
  containerEl.addEventListener('pointerup', endDrag);
  containerEl.addEventListener('pointerleave', endDrag);

  return { chart, candles, volume, priceLine, resize,
    setSymbol, setHistory, updateCandle, setPrice,
    drawLinesFor, removeLinesFor, clearLines
  };
}
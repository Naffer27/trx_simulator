"""Real Lightweight Charts regression tests. No database, server or account."""
import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

BODY = r"""
(async () => {
 const results=[];
 const check=(ok,label)=>results.push({ok:!!ok,label});
 const wait=()=>new Promise(r=>setTimeout(r,100));
 const bars=(base,count=180)=>Array.from({length:count},(_,i)=>({time:1700000000+i*900,open:base+i*.001*base,high:base+(i+2)*.001*base,low:base+(i-2)*.001*base,close:base+(i+1)*.001*base,volume:10}));
 try {
 const c=new MobileTradingChart(document.getElementById('chart'));
 await c.initialize();
 for(const [sym,base] of [['EUR/USD',1.1],['BTCUSD',76000],['USD/JPY',150],['ETHUSD',2500]]){
   c.clear();
   c.chart.priceScale('right').applyOptions({autoScale:false});
   const data=bars(base);
   // Live tick races ahead of the initial complete history.
   c.updateLastCandle(data[data.length-1]);
   c.applyHistory(data,'complete',sym);
   await wait();
   check(c.chart.priceScale('right').options().autoScale,sym+' resets manual scale after live/history race');
   check(c.isSlTpEditableFor(sym),sym+' history generation becomes ready');
   const y=c.candleSeries.priceToCoordinate(data[data.length-1].close);
   check(y>0 && y<500,sym+' latest candle visible');
   const range=c.chart.timeScale().getVisibleLogicalRange();
   check(range.to-range.from<100 && range.to>=179,sym+' opens on readable recent candles');
 }
 const scale=c.chart.timeScale();
 scale.setVisibleLogicalRange({from:130,to:160});
 c.chart.priceScale('right').applyOptions({autoScale:false});
 c.updateLastCandle(bars(2500).at(-1));
 c.applyHistory([{...bars(2500)[0],time:1699999100}],'complete','ETHUSD');
 await wait();
 check(!c.chart.priceScale('right').options().autoScale,'live/deeper history respects manual vertical zoom');
 const range=scale.getVisibleLogicalRange();
 check(Math.abs((range.to-range.from)-30)<.1,'deeper history preserves horizontal zoom');
 c.clear();
 c.applyHistory([], 'complete','ETHUSD');
 check(!c.isSlTpEditableFor('ETHUSD'),'empty history does not unlock editing');
 c.applyHistory(bars(2500),'complete','ETHUSD');
 await wait();
 check(c.chart.priceScale('right').options().autoScale,'timeframe switch resets scale');
 const late=new MobileTradingChart(document.getElementById('late'));
 late.applyHistory(bars(76000),'complete','BTCUSD');
 await late.initialize(); await wait();
 check(late.candleSeries.options().priceFormat.precision===2,'history before initialization preserves instrument format');
 check(late.chart.timeScale().getVisibleLogicalRange().to-late.chart.timeScale().getVisibleLogicalRange().from<100,'late initialization uses readable framing');
 } catch(e){results.push({ok:false,label:String(e.stack||e)});}
 document.getElementById('result').textContent=JSON.stringify(results);
})();
"""

class MobileChartFramingTests(unittest.TestCase):
    def test_real_chart_initial_framing_and_live_history_race(self):
        self.assertTrue(Path(CHROME).exists(), "Chrome required for chart regression")
        sources = [ROOT / "simulator/tests/fixtures/lightweight-charts.4.1.1.standalone.production.js",
                   ROOT / "simulator/static/simulator/trade/trading_core.js",
                   ROOT / "simulator/static/simulator/trade/mobile_chart.js"]
        page = '<div id="chart" style="width:390px;height:500px"></div><div id="late" style="width:390px;height:500px"></div><pre id="result"></pre>'
        page += ''.join('<script>'+p.read_text()+'</script>' for p in sources)
        page += '<script>'+BODY+'</script>'
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'chart.html'
            path.write_text(page)
            proc=subprocess.run([CHROME,'--headless','--disable-gpu','--no-sandbox',
                '--user-data-dir='+d+'/profile','--virtual-time-budget=8000','--dump-dom',path.as_uri()],
                capture_output=True,text=True,timeout=35)
        match=re.search(r'<pre id="result">(.*?)</pre>',proc.stdout,re.S)
        self.assertIsNotNone(match,proc.stderr[-2000:])
        results=json.loads(html.unescape(match.group(1)))
        self.assertTrue(results)
        failures=[r['label'] for r in results if not r['ok']]
        self.assertEqual(failures,[])

if __name__ == '__main__':
    unittest.main()

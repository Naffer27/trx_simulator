# simulator/orders.py
import itertools
from dataclasses import dataclass, field
from typing import Dict, Optional, List

_id_gen = itertools.count(1)

@dataclass
class Order:
    id: int
    symbol: str
    side: str              # "buy" | "sell"
    type: str              # "market" | "limit"
    qty: float
    entry: float           # precio objetivo (para market = último close que enviaste)
    sl: Optional[float] = None
    tp: Optional[float] = None
    status: str = "open"   # "open" | "closed" | "canceled"
    avg_fill_price: Optional[float] = None
    pnl: float = 0.0

class OrderBook:
    def __init__(self):
        self.orders: Dict[int, Order] = {}

    def create(self, symbol: str, side: str, typ: str, qty: float, entry: float,
               sl: Optional[float], tp: Optional[float]) -> Order:
        oid = next(_id_gen)
        o = Order(id=oid, symbol=symbol, side=side, type=typ, qty=qty, entry=entry, sl=sl, tp=tp)
        # para MARKET, consideramos ejecutada al precio de entrada
        if typ == "market":
            o.avg_fill_price = entry
        self.orders[oid] = o
        return o

    def snapshot(self) -> List[dict]:
        out = []
        for o in self.orders.values():
            out.append({
                "id": o.id, "symbol": o.symbol, "side": o.side, "type": o.type,
                "qty": o.qty, "entry": o.entry, "sl": o.sl, "tp": o.tp,
                "status": o.status, "avg_fill_price": o.avg_fill_price, "pnl": o.pnl
            })
        return out

    def on_tick(self, symbol: str, price: float):
        # chequeo SL/TP muy simple
        for o in list(self.orders.values()):
            if o.symbol != symbol or o.status != "open":
                continue
            if o.side == "buy":
                if o.tp and price >= o.tp: self._close(o, price, reason="tp")
                elif o.sl and price <= o.sl: self._close(o, price, reason="sl")
            else:  # sell
                if o.tp and price <= o.tp: self._close(o, price, reason="tp")
                elif o.sl and price >= o.sl: self._close(o, price, reason="sl")

    def _close(self, o: Order, price: float, reason: str):
        o.status = "closed"
        if o.avg_fill_price is None: o.avg_fill_price = o.entry
        # PnL lineal simple
        delta = (price - o.avg_fill_price) if o.side == "buy" else (o.avg_fill_price - price)
        o.pnl = delta * o.qty
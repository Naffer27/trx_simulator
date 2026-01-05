# simulator/order_engine.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Optional


@dataclass
class Pos:
    id: int
    symbol: str
    side: str            # 'buy' | 'sell'
    qty: float
    avg: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    opened_at: int = 0
    trail_dist: Optional[float] = None
    best: Optional[float] = None  # mejor precio alcanzado (para trailing)

    def to_dict(self) -> Dict:
        return asdict(self)


class OrderEngine:
    """
    Motor de posiciones con soporte de modos:
      - netting_mode=True  -> consolida por (symbol, side)
      - netting_mode=False -> hedging (una posición por orden)
    Mantiene el mismo "shape" que usa el front: dicts con keys id,symbol,side,qty,avg,sl,tp,opened_at,trail_dist,best.
    """
    def __init__(self, netting_mode: bool = False):
        self.netting_mode = bool(netting_mode)
        self._seq = 1
        self._positions: List[Pos] = []

    # ---------- API pública ----------
    @property
    def positions(self) -> List[Dict]:
        return [p.to_dict() for p in self._positions]

    def open_market(self, symbol: str, side: str, qty: float, fill_px: float,
                    sl: Optional[float] = None, tp: Optional[float] = None,
                    position_id: Optional[int] = None) -> Pos:
        """
        Abre o actualiza una posición según modo.
        Devuelve la Pos afectada/creada.
        """
        now = int(datetime.utcnow().timestamp())
        side = side.lower()

        if self.netting_mode:
            # consolidar por symbol+side
            for p in self._positions:
                if p.symbol == symbol and p.side == side:
                    new_qty = p.qty + qty
                    p.avg = round(((p.avg * p.qty) + (fill_px * qty)) / new_qty, 10)
                    p.qty = new_qty
                    if sl is not None: p.sl = sl
                    if tp is not None: p.tp = tp
                    return p
        # hedging (o no existe neteada)
        pid = position_id if position_id is not None else self._next_id()
        pos = Pos(
            id=pid, symbol=symbol, side=side, qty=qty, avg=round(fill_px, 10),
            sl=sl, tp=tp, opened_at=now
        )
        self._positions.append(pos)
        return pos

    def update_sl_tp(self, pid: Optional[int], symbol: str,
                     sl: Optional[float], tp: Optional[float]) -> bool:
        """
        Actualiza SL/TP. Si pid viene None o 'tmp-*', toma última del símbolo.
        """
        # 1) por id
        if pid is not None and not (isinstance(pid, str) and str(pid).startswith("tmp-")):
            for p in self._positions:
                if p.id == pid and p.symbol == symbol:
                    if sl is not None: p.sl = sl
                    if tp is not None: p.tp = tp
                    return True

        # 2) fallback: última del símbolo
        for p in reversed(self._positions):
            if p.symbol == symbol:
                if sl is not None: p.sl = sl
                if tp is not None: p.tp = tp
                return True
        return False

    def set_trailing(self, pid: int, symbol: str, distance: float) -> bool:
        for p in self._positions:
            if p.id == pid and p.symbol == symbol:
                p.trail_dist = distance
                p.best = p.best if p.best is not None else p.avg
                return True
        return False

    def close_by_id(self, pid: int, symbol: str, close_px: float) -> Optional[Dict]:
        """
        Cierra una posición por id y devuelve un dict con info de cierre.
        """
        remaining = []
        closed_info = None
        for p in self._positions:
            if p.id == pid and p.symbol == symbol and closed_info is None:
                realized = self.realized_pnl(p, close_px)
                closed_info = {
                    "id": p.id, "symbol": p.symbol, "side": p.side,
                    "qty": p.qty, "avg": p.avg, "close_px": close_px,
                    "reason": "manual", "realized_pnl": realized,
                    "ts": int(datetime.utcnow().timestamp()),
                }
            else:
                remaining.append(p)
        self._positions = remaining
        return closed_info

    def check_tp_sl_and_trailing(self, symbol: str, last_price: float, decimals: int) -> List[Dict]:
        """
        Evalúa SL/TP y trailing; devuelve lista de cierres ejecutados.
        """
        closed = []
        keep: List[Pos] = []
        now = int(datetime.utcnow().timestamp())

        for p in self._positions:
            if p.symbol != symbol:
                keep.append(p)
                continue

            # trailing
            if p.trail_dist and p.trail_dist > 0:
                if p.side == "buy":
                    p.best = max(p.best or p.avg, last_price)
                    p.sl = round((p.best or last_price) - p.trail_dist, decimals)
                else:
                    p.best = min(p.best or p.avg, last_price)
                    p.sl = round((p.best or last_price) + p.trail_dist, decimals)

            sl_hit = (p.sl is not None) and (
                (p.side == "buy" and last_price <= p.sl) or
                (p.side == "sell" and last_price >= p.sl)
            )
            tp_hit = (p.tp is not None) and (
                (p.side == "buy" and last_price >= p.tp) or
                (p.side == "sell" and last_price <= p.tp)
            )

            if sl_hit or tp_hit:
                realized = self.realized_pnl(p, last_price)
                closed.append({
                    "id": p.id, "symbol": p.symbol, "side": p.side, "qty": p.qty,
                    "avg": p.avg, "close_px": last_price,
                    "reason": "tp" if tp_hit else "sl",
                    "realized_pnl": realized, "ts": now,
                })
            else:
                keep.append(p)

        if closed:
            self._positions = keep
        return closed

    # ---------- PnL / margen ----------
    @staticmethod
    def unrealized_pnl(pos: Pos, last_price: float) -> float:
        if pos.side == "buy":
            return (last_price - pos.avg) * pos.qty
        return (pos.avg - last_price) * pos.qty

    def unrealized_total(self, price_state_fn) -> float:
        total = 0.0
        for p in self._positions:
            last = price_state_fn(p.symbol)
            total += self.unrealized_pnl(p, last)
        return total

    def margin_used_total(self, price_state_fn, leverage: int) -> float:
        lev = max(1, int(leverage))
        total = 0.0
        for p in self._positions:
            last = price_state_fn(p.symbol)
            notional = abs(last * p.qty)
            total += notional / lev
        return total

    @staticmethod
    def realized_pnl(pos: Pos, close_price: float) -> float:
        return OrderEngine.unrealized_pnl(pos, close_price)

    # ---------- util ----------
    def _next_id(self) -> int:
        v = self._seq
        self._seq += 1
        return v
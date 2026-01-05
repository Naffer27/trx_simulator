from dataclasses import dataclass

@dataclass
class CandleDTO:
    symbol: str   # "BTCUSDT"
    tf: str       # "1m", "5m", "1h", "1d"
    t: int        # epoch ms UTC
    o: float; h: float; l: float; c: float; v: float
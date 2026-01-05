from .interfaces import IMarketDataProvider, CandleCb
from .dto import CandleDTO

class FinnhubAdapter(IMarketDataProvider):
    def subscribe(self, symbol: str, tf: str, callback: CandleCb) -> None:
        # TODO: conectar a tu flujo Finnhub actual y en cada vela:
        # callback(CandleDTO(symbol, tf, t, o, h, l, c, v))
        pass

    def get_history(self, symbol: str, tf: str, since_ms: int, until_ms: int):
        # TODO: pedir histórico y renderear CandleDTO
        return []
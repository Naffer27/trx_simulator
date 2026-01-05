from typing import Protocol, Callable, Iterable
from .dto import CandleDTO

CandleCb = Callable[[CandleDTO], None]

class IMarketDataProvider(Protocol):
    def subscribe(self, symbol: str, tf: str, callback: CandleCb) -> None: ...
    def get_history(self, symbol: str, tf: str, since_ms: int, until_ms: int) -> Iterable[CandleDTO]: ...
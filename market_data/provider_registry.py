REGISTRY = {
    "EURUSD": "finnhub",
    "XAUUSD": "finnhub",
    "BTCUSDT": "binance",
}

def provider_for(symbol: str) -> str:
    return REGISTRY.get(symbol.replace("/", "").upper(), "binance")
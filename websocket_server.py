# websocket_server.py
# --- Simulador WS (FastAPI) con velas, history, volumen y lista de precios ---
# Mantiene tu comportamiento original + integración OPCIONAL con Django Channels.

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import random
import time
import asyncio
import json
from typing import Dict, List

# ---- Indicadores (seguimos usando pandas/pandas_ta si están instalados) ----
try:
    import pandas as pd
    import pandas_ta as ta
    HAS_PANDAS = True
except Exception:
    HAS_PANDAS = False
    pd = None
    ta = None

# ---- Opcional: espejo al hub de Django Channels (no rompe si no existe) ----
MIRROR_TO_CHANNELS = True   # pon False si no quieres reflejar al consumer
def mirror_publish(candle_dict: dict):
    """Refleja la vela al grupo 'prices' del consumer de Django si el hub existe."""
    if not MIRROR_TO_CHANNELS:
        return
    try:
        from market_data.hub import publish  # import lazy para no fallar si no está
        publish(candle_dict)  # {"symbol","tf","t","o","h","l","c","v"}
    except Exception:
        # No hacemos ruido en producción si no está configurado Channels
        pass

# -----------------------------------------------------------------------------

app = FastAPI()

# CORS para desarrollo (ajusta en producción)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Estado por cliente (clave = objeto WebSocket) --------------------------
user_timeframes: Dict[WebSocket, str] = {}
user_symbols: Dict[WebSocket, str] = {}
user_open_prices: Dict[WebSocket, float] = {}
user_last_candle_time: Dict[WebSocket, int] = {}
user_candle_cache: Dict[WebSocket, List[dict]] = {}

# ---- Instrumentos con precios iniciales -------------------------------------
instruments: Dict[str, float] = {
    "EUR/USD": 1.1020,
    "USD/JPY": 146.20,
    "GBP/USD": 1.2840,
    "AUD/USD": 0.6720,
    "BTC/USDT": 68000.0,      # añadí una cripto para pruebas
    "ETH/USDT": 3500.0,
}

# ---- Map de timeframes a segundos -------------------------------------------
timeframe_seconds = {
    "1s": 1,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
    "1w": 604800,
}

# ---- Utilidades --------------------------------------------------------------
def now_s() -> int:
    return int(time.time())

def align_time(ts: int, interval: int) -> int:
    """Alinea el timestamp al borde del timeframe."""
    if interval <= 1:
        return ts
    return ts - (ts % interval)

def generate_candle(open_price: float) -> (dict, float):
    """Genera una vela OHLC pseudoaleatoria a partir de un open dado."""
    close = open_price + random.uniform(-0.001, 0.001)
    high = max(open_price, close) + random.uniform(0.0001, 0.0005)
    low = min(open_price, close) - random.uniform(0.0001, 0.0005)
    candle = {
        "time": now_s(),
        "open": round(open_price, 5),
        "high": round(high, 5),
        "low": round(low, 5),
        "close": round(close, 5),
    }
    return candle, close

def generate_historical_candles(last_price: float, count: int = 300, interval: int = 60) -> List[dict]:
    """Crea historial simple hacia atrás, respetando el intervalo."""
    candles: List[dict] = []
    timestamp = align_time(now_s(), interval) - count * interval
    price = last_price
    for _ in range(count):
        candle, price = generate_candle(price)
        candle["time"] = timestamp
        candles.append(candle)
        timestamp += interval
    return candles

def calculate_indicators(candles: List[dict]) -> dict:
    """Calcula SMA/RSI/Bollinger para la última vela, si pandas está disponible."""
    if not HAS_PANDAS or not candles:
        return {"rsi": None, "sma": None, "bb_upper": None, "bb_lower": None}

    try:
        df = pd.DataFrame(candles)
        df.set_index("time", inplace=True)

        df["sma"] = df["close"].rolling(20).mean()
        df["rsi"] = ta.rsi(df["close"], length=14)

        bb = ta.bbands(df["close"], length=20)
        if bb is not None:
            df["bb_upper"] = bb["BBU_20_2.0"]
            df["bb_lower"] = bb["BBL_20_2.0"]
        else:
            df["bb_upper"] = None
            df["bb_lower"] = None

        latest = df.iloc[-1]
        return {
            "rsi": None if pd.isna(latest.get("rsi")) else round(float(latest["rsi"]), 2),
            "sma": None if pd.isna(latest.get("sma")) else round(float(latest["sma"]), 5),
            "bb_upper": None if pd.isna(latest.get("bb_upper")) else round(float(latest["bb_upper"]), 5),
            "bb_lower": None if pd.isna(latest.get("bb_lower")) else round(float(latest["bb_lower"]), 5),
        }
    except Exception:
        # Si falla pandas, devolvemos nulos y seguimos
        return {"rsi": None, "sma": None, "bb_upper": None, "bb_lower": None}

# ---- Endpoints auxiliares ----------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "instruments": list(instruments.keys())}

@app.get("/config")
def config():
    return {"timeframes": list(timeframe_seconds.keys()), "mirror_to_channels": MIRROR_TO_CHANNELS}

# ---- WebSocket principal -----------------------------------------------------
@app.websocket("/ws/trading/")
async def websocket_trading(websocket: WebSocket):
    await websocket.accept()

    # Estado por defecto del usuario/cliente
    user_timeframes[websocket] = "1s"
    user_symbols[websocket] = "EUR/USD"
    user_open_prices[websocket] = instruments["EUR/USD"]
    user_last_candle_time[websocket] = now_s()
    user_candle_cache[websocket] = []

    try:
        while True:
            # 1) Procesar mensajes entrantes, con timeout corto
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "invalid_json"})
                    msg = {}

                action = msg.get("action")

                if action == "change_timeframe":
                    tf = msg.get("timeframe")
                    if tf in timeframe_seconds:
                        user_timeframes[websocket] = tf
                        await websocket.send_json({"type": "ack", "action": "change_timeframe", "timeframe": tf})
                    else:
                        await websocket.send_json({"type": "error", "message": "invalid_timeframe"})

                elif action == "change_symbol":
                    sym = msg.get("symbol")
                    if sym in instruments:
                        user_symbols[websocket] = sym
                        user_open_prices[websocket] = instruments[sym]
                        user_last_candle_time[websocket] = now_s()
                        user_candle_cache[websocket] = []
                        await websocket.send_json({"type": "ack", "action": "change_symbol", "symbol": sym})
                    else:
                        await websocket.send_json({"type": "error", "message": "invalid_symbol"})

                elif action == "load_history":
                    sym = msg.get("symbol") or user_symbols[websocket]
                    tf = msg.get("timeframe") or user_timeframes[websocket]
                    if sym in instruments:
                        price = instruments[sym]
                        interval = timeframe_seconds.get(tf, 60)
                        count = int(msg.get("count", 100))
                        candles = generate_historical_candles(price, count, interval)
                        await websocket.send_json({
                            "type": "history",
                            "symbol": sym,
                            "timeframe": tf,
                            "data": candles,
                        })
                    else:
                        await websocket.send_json({"type": "error", "message": "invalid_symbol"})

                elif action == "ping":
                    await websocket.send_json({"type": "pong", "ts": now_s()})

                # (deja abierto para acciones futuras: subscribe positions, etc.)

            except asyncio.TimeoutError:
                # No llegaron mensajes: seguimos con el loop
                pass
            except Exception as e:
                # Si hay un error raro al leer, no tumba el WS
                await websocket.send_json({"type": "error", "message": f"recv_error: {e}"})

            # 2) Generar vela si ya tocó de acuerdo al timeframe del usuario
            sym = user_symbols[websocket]
            tf = user_timeframes[websocket]
            interval = timeframe_seconds.get(tf, 1)
            now = now_s()

            if (now - user_last_candle_time[websocket]) >= interval:
                open_price = user_open_prices[websocket]
                candle, new_price = generate_candle(open_price)

                # Actualizar estado
                instruments[sym] = new_price
                user_open_prices[websocket] = new_price
                user_last_candle_time[websocket] = align_time(now, interval)

                # Cache de usuario para indicadores
                cache = user_candle_cache[websocket]
                cache.append(candle)
                if len(cache) > 300:
                    cache.pop(0)

                indicators = calculate_indicators(cache)

                # Enviar vela al cliente
                await websocket.send_json({
                    "type": "candle",
                    "symbol": sym,
                    "timeframe": tf,
                    "data": candle,
                    "indicators": indicators,
                })

                # Espejo opcional al hub de Django → mismo front puede recibirlo por el consumer
                # Convertimos al formato que espera el hub: {symbol, tf, t, o, h, l, c, v}
                try:
                    mirror_publish({
                        "symbol": sym.replace("/", ""),
                        "tf": tf,
                        "t": candle["time"] * 1000,   # epoch ms
                        "o": candle["open"],
                        "h": candle["high"],
                        "l": candle["low"],
                        "c": candle["close"],
                        "v": 0.0,  # simulador simple
                    })
                except Exception:
                    # No importan errores del espejo
                    pass

                # Volumen sintético (como ya tenías)
                volume = random.randint(3000, 10000)
                color = "#26a69a" if candle["close"] >= candle["open"] else "#ef5350"
                await websocket.send_json({
                    "type": "volume_update",
                    "symbol": sym,
                    "time": candle["time"],
                    "value": volume,
                    "color": color,
                })

            # 3) Ticker board de todos los símbolos (bid/ask sintético)
            for s, base_price in instruments.items():
                bid = base_price + random.uniform(-0.0005, 0.0005)
                ask = bid + random.uniform(0.0002, 0.0004)
                await websocket.send_json({
                    "type": "price",
                    "symbol": s,
                    "bid": round(bid, 5),
                    "ask": round(ask, 5),
                })

            # 4) Ritmo del loop
            await asyncio.sleep(1)

    except Exception as e:
        print("WebSocket desconectado:", e)

    finally:
        # Limpieza de estado por cliente
        user_timeframes.pop(websocket, None)
        user_symbols.pop(websocket, None)
        user_open_prices.pop(websocket, None)
        user_last_candle_time.pop(websocket, None)
        user_candle_cache.pop(websocket, None)

# ---- Runner local ------------------------------------------------------------
if __name__ == "__main__":
    # Mantengo el host/puerto que estás usando
    uvicorn.run("websocket_server:app", host="127.0.0.1", port=8001, reload=False)
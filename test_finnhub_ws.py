import websocket, json
import os

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")

SYMBOLS = [
    "OANDA:EUR_USD",
    "OANDA:GBP_USD",
    "OANDA:USD_JPY",
    "OANDA:AUD_USD",
]

def on_message(ws, message):
    data = json.loads(message)
    print("📈 Mensaje:", data)

def on_error(ws, error):
    print("❌ Error:", error)

def on_close(ws, close_status_code, close_msg):
    print("⚠️ Conexión cerrada")

def on_open(ws):
    for sym in SYMBOLS:
        sub_msg = {"type": "subscribe", "symbol": sym}
        ws.send(json.dumps(sub_msg))
        print("✅ Suscrito a", sym)

url = f"wss://ws.finnhub.io?token={FINNHUB_KEY}"
ws = websocket.WebSocketApp(
    url,
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close
)
ws.run_forever()
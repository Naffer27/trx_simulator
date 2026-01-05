# puente hacia tu WebSocket existente (Django Channels)
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

GROUP = "prices"  # si tu TradingConsumer usa otro nombre, dímelo y lo cambio

def publish(candle: dict) -> None:
    """
    Enviar una vela al grupo WS que tu dashboard ya consume.
    'candle' debe ser {symbol, tf, t, o, h, l, c, v}
    """
    layer = get_channel_layer()
    if layer:
        async_to_sync(layer.group_send)(GROUP, {"type": "price.tick", "data": candle})
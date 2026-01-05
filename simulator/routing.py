# =========================
# path: simulator/routing.py
# =========================
from django.urls import re_path
from .consumers import TradingConsumer

# Por qué: aceptar /ws/trading y /ws/trading/
websocket_urlpatterns = [
    re_path(r"^ws/trading/?$", TradingConsumer.as_asgi()),
]
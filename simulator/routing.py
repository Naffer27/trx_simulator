# =========================
# path: simulator/routing.py
# =========================
from django.urls import re_path
from .consumers import TradingConsumer

websocket_urlpatterns = [
    # Primary: account_id in URL path — used by /dashboard/<account_id>/
    re_path(r"^ws/trading/(?P<account_id>\d+)/?$", TradingConsumer.as_asgi()),
    # Legacy: no account_id — resolved via session / querystring fallback
    re_path(r"^ws/trading/?$", TradingConsumer.as_asgi()),
]
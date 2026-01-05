# ==========================
# path: trx_simulator/asgi.py
# ==========================
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "trx_simulator.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator

import simulator.routing  # requiere simulator/routing.py con websocket_urlpatterns

# Nota: AllowedHostsOriginValidator usa ALLOWED_HOSTS. En dev añade "*" o tu dominio ngrok.
django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(simulator.routing.websocket_urlpatterns)
        )
    ),
})

# Log útil en consola (para confirmar carga ASGI/rutas)
try:
    from django.conf import settings
    print(f"⚡ ASGI listo | hosts: {settings.ALLOWED_HOSTS} | rutas WS: {[p.pattern.regex.pattern for p in simulator.routing.websocket_urlpatterns]}")
except Exception as _e:
    # Evita romper si cambia el objeto pattern
    print("⚡ ASGI listo | rutas WS cargadas")
# trx_simulator/asgi.py
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "trx_simulator.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
import simulator.routing

django_asgi_app = get_asgi_application()

_routed_application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(simulator.routing.websocket_urlpatterns)
        )
    ),
})

# O.6c-1v — OPEN POSITION FEED COVERAGE, restart recovery.
#
# Daphne 4.2.1 (the ASGI server this project deploys — see
# deploy/systemd/daphne.service) does not implement the ASGI lifespan
# protocol (verified: no "lifespan" reference anywhere in the installed
# daphne package), so there is no true "on server start" hook available.
# TradingConsumer.connect() already starts the same idempotent
# reconciliation loop (FeedManager.ensure_position_feed_reconciliation_
# started()) on the first WebSocket connection, but that alone would
# leave open Positions unpriced until some user's browser reconnects.
#
# This wrapper fires the identical idempotent starter on the first ASGI
# scope of ANY type — http or websocket — that Daphne dispatches after a
# (re)start. In this project's own deploy runbook (DEPLOY.md), every
# `systemctl restart daphne` is immediately followed by
# deploy/scripts/healthcheck.sh hitting GET /api/health/ — an HTTP
# request through this exact application — so in the deployed case this
# closes the gap within the restart script's own healthcheck call, not
# after some indefinite wait for real user traffic. Idempotent and
# fail-safe: any error here is logged and swallowed, never blocks or
# breaks the real request it wraps.
_position_feed_bootstrap_done = False


async def _application_with_position_feed_bootstrap(scope, receive, send):
    global _position_feed_bootstrap_done
    if not _position_feed_bootstrap_done:
        _position_feed_bootstrap_done = True
        try:
            import asyncio
            from market_data.feeds import get_feed_manager
            asyncio.get_running_loop().create_task(
                get_feed_manager().ensure_position_feed_reconciliation_started()
            )
        except Exception:
            import logging
            logging.getLogger("simulator.ws").exception(
                "[startup] failed to bootstrap position-feed reconciliation "
                "(non-fatal — TradingConsumer.connect() still starts it on "
                "the first WebSocket connection)"
            )
    return await _routed_application(scope, receive, send)


application = _application_with_position_feed_bootstrap
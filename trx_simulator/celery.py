"""
trx_simulator/celery.py
Celery application entry point.
Import this module ONLY from __init__.py — everywhere else use:
    from celery import current_app  or  @shared_task
"""
import os
from celery import Celery
from celery.signals import worker_ready, worker_shutdown
import logging

logger = logging.getLogger("celery.worker")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "trx_simulator.settings")

app = Celery("trx_simulator")

# Pull all CELERY_* keys from Django settings
app.config_from_object("django.conf:settings", namespace="CELERY")

# Explicit package list — more reliable than lambda form in all envs
app.autodiscover_tasks(["simulator"])


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    logger.info("✅ Celery worker ready — broker: %s", app.conf.broker_url)


@worker_shutdown.connect
def on_worker_shutdown(sender, **kwargs):
    logger.info("🛑 Celery worker shutting down")

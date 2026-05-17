# Ensure the Celery app is always initialized when Django starts,
# so @shared_task decorators work in every installed app.
from .celery import app as celery_app  # noqa: F401

__all__ = ("celery_app",)

from django.apps import AppConfig


class SimulatorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'simulator'

    def ready(self):
        from . import ib_network_signals  # noqa: F401
        from . import session_security  # noqa: F401

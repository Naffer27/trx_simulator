"""
simulator/management/commands/reset_peaks.py

Clears all stress peak values from Redis (trx:metrics:peaks).
Run after a load test to start tracking fresh from zero.

Usage:
    python manage.py reset_peaks
    python manage.py reset_peaks --confirm
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Clear all stress peak metrics from Redis (run after load tests)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required flag to actually delete peaks (prevents accidental wipes)",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            self.stderr.write(
                self.style.ERROR(
                    "Pass --confirm to reset peaks. "
                    "This deletes the trx:metrics:peaks Redis hash."
                )
            )
            return

        from django.conf import settings
        from simulator.observability import reset_peaks

        redis_url = getattr(settings, "REDIS_URL", "") or "redis://127.0.0.1:6379/0"
        reset_peaks(redis_url)
        self.stdout.write(self.style.SUCCESS("Stress peaks cleared from Redis."))

"""Print proposed demo economics; no account mutations or feed subscriptions."""
import json
from django.core.management.base import BaseCommand, CommandError
from market_data.instruments.pilot_rules import PILOT_RULES, preview_economics


class Command(BaseCommand):
    help = 'Preview proposed instrument demo rules. Does not enable trading.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--symbol', choices=tuple(PILOT_RULES), required=True)
        parser.add_argument('--lots', required=True)
        parser.add_argument('--price', required=True)
        parser.add_argument('--leverage', default='500')
        parser.add_argument('--tier', choices=('ECN', 'VIP'), default='ECN')

    def handle(self, *args, **options):
        try:
            result = preview_economics(options['symbol'], options['lots'], options['price'],
                                       options['leverage'], options['tier'])
        except (ValueError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, default=str, indent=2))

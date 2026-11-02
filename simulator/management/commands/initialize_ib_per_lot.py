"""Initialize the global per-lot tariff prospectively; never settle commissions."""
from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand, CommandError
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from simulator.models import IBCommissionRule


class Command(BaseCommand):
    help = 'Preview or initialize the global IB per-lot tariff. Existing rules are never overwritten.'

    def add_arguments(self, parser):
        parser.add_argument('--amount', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            amount = Decimal(options['amount'])
            if not amount.is_finite() or amount <= 0 or amount > Decimal('9999999999.99') or amount != amount.quantize(Decimal('.01')):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise CommandError('Amount must be positive USD with at most two decimal places.')
        with transaction.atomic():
            # Serialize initializations even when no global rule exists yet.
            ContentType.objects.select_for_update().get(app_label='simulator', model='ibcommissionrule')
            rules = list(IBCommissionRule.objects.filter(rule_type='PER_LOT', referral__isnull=True))
            if rules:
                if (len(rules) == 1 and rules[0].enabled and rules[0].effective_until is None
                        and rules[0].effective_from <= timezone.now()
                        and rules[0].fixed_amount == amount):
                    self.stdout.write('Tariff already configured; no changes.')
                    return
                raise CommandError('A global tariff already exists. Review/version it in admin; no changes made.')
            if not options['apply']:
                self.stdout.write(f'PREVIEW: {amount:.2f} USD/lot; new eligible executions only; no payouts.')
                return
            rule = IBCommissionRule(
                rule_type='PER_LOT', referral=None, enabled=True,
                fixed_amount=amount, effective_from=timezone.now(),
                notes='Owner-authorized initial USD per-lot tariff. Cash-funded accounts only; prospective; manual treasury approval.',
            )
            rule.full_clean()
            rule.save()
            self.stdout.write(f'Created global rule #{rule.pk}: {amount:.2f} USD/lot, effective {rule.effective_from.isoformat()}. No payouts executed.')

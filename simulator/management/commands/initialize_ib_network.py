from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from simulator.models import IBCommissionRule
from simulator.ib_network import _lock_network

class Command(BaseCommand):
    help = 'Prepare prospective sub-IB rates; never pays commissions or rewrites existing rates.'
    def add_arguments(self, parser):
        parser.add_argument('--first-level', default='0.50')
        parser.add_argument('--budget', default='1.00')
        parser.add_argument('--apply', action='store_true')
    def handle(self, **options):
        try:
            first, budget = (Decimal(options[k]) for k in ('first_level', 'budget'))
            if any(not n.is_finite() or n <= 0 or n != n.quantize(Decimal('.01')) for n in (first,budget)):
                raise ValueError()
            if first > budget: raise ValueError()
        except (ValueError, InvalidOperation):
            raise CommandError('Importes positivos en USD, máximo 2 decimales; tarifa <= presupuesto.')
        with transaction.atomic():
            _lock_network()
            pairs = [(IBCommissionRule.RULE_SUB_IB_PER_LOT, first), (IBCommissionRule.RULE_SUB_IB_BUDGET, budget)]
            now = timezone.now()
            for kind, amount in pairs:
                rows = list(IBCommissionRule.objects.filter(rule_type=kind, referral=None))
                if rows and not (len(rows)==1 and rows[0].enabled and rows[0].effective_until is None and rows[0].fixed_amount==amount):
                    raise CommandError('Hay reglas existentes diferentes: cambiar mediante administración con vigencia futura.')
            if not options['apply']:
                self.stdout.write(f'Vista previa: primer superior ${first}/lote; presupuesto superiores ${budget}/lote. Sin cambios.')
                return
            for kind, amount in pairs:
                if not IBCommissionRule.objects.filter(rule_type=kind, referral=None).exists():
                    rule = IBCommissionRule(rule_type=kind, fixed_amount=amount, effective_from=now,
                        notes='Red multinivel: niveles sucesivos a la mitad, presupuesto máximo por lote.')
                    rule.full_clean(); rule.save()
            self.stdout.write('Red configurada para operaciones futuras. No se movieron fondos.')

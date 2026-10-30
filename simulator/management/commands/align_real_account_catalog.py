"""Explicit, backed-up catalogue release. Never rewrites trading accounts."""
import json
import os
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from simulator.models import AccountProduct

ECN_BEFORE = dict(min_deposit=Decimal('100'), max_leverage=100,
                  commission_per_lot=Decimal('7'), typical_spread_pips=Decimal('0'),
                  commission_pct=Decimal('0'), family='REAL', product_type='ECN',
                  margin_call_level=Decimal('100'), stopout_level=Decimal('70'), is_active=True)
ECN_AFTER = dict(ECN_BEFORE, min_deposit=Decimal('10'), max_leverage=500)
ECN_DESCRIPTION = 'Cuenta ECN. Depósito mínimo USD 10. Comisión USD 7 por lote al abrir. Spread variable; gastos de spread aparte. Hasta 1:500, sujeto al límite de cada instrumento.'
VIP = dict(ECN_AFTER, name='Real VIP', code='real-vip',
           min_deposit=Decimal('100000'), commission_per_lot=Decimal('3.50'),
           platform_label='Money Broker', default_balance=Decimal('0'),
           spread_markup=Decimal('0'), sort_order=30, is_popular=False,
           description='Cuenta VIP. Depósito mínimo USD 100.000. Comisión USD 3,50 por lote al abrir. Spread variable; gastos de spread aparte. Hasta 1:500, sujeto al límite de cada instrumento.')


def matches(obj, fields):
    return all(getattr(obj, key) == value for key, value in fields.items())


class Command(BaseCommand):
    help = 'Preview the audited ECN/VIP catalogue change; --apply requires a new private backup file.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--backup')

    @transaction.atomic
    def handle(self, *args, **options):
        rows = {p.code: p for p in AccountProduct.objects.select_for_update().filter(
            code__in=['real-ecn', 'real-standard', 'real-vip'])}
        ecn, standard, vip = (rows.get(k) for k in ['real-ecn', 'real-standard', 'real-vip'])
        if not ecn or not standard:
            raise CommandError('Missing audited ECN/Standard products; nothing changed.')
        if not (matches(ecn, ECN_BEFORE) or matches(ecn, ECN_AFTER)):
            raise CommandError('ECN differs from the audited configuration; nothing changed.')
        if standard.family != 'REAL' or standard.product_type != 'STANDARD':
            raise CommandError('Standard identity differs; nothing changed.')
        if vip and not matches(vip, VIP):
            raise CommandError('Existing VIP has different conditions; nothing changed.')
        if matches(ecn, ECN_AFTER) and ecn.description == ECN_DESCRIPTION and not standard.is_active and vip:
            self.stdout.write('Already aligned. No writes.')
            return
        self.stdout.write('ECN: USD 10 / up to 1:500 / USD 7 per lot at open. VIP: USD 100000 / up to 1:500 / USD 3.50. Standard: unavailable for new accounts. Existing accounts unchanged.')
        if not options['apply']:
            self.stdout.write('PREVIEW ONLY. No writes.')
            return
        if not options['backup']:
            raise CommandError('--backup is required; nothing changed.')
        before = list(AccountProduct.objects.filter(pk__in=[p.pk for p in rows.values()]).values())
        payload = {'version': 1, 'before': before, 'vip_created': vip is None}
        path = Path(options['backup'])
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                json.dump(payload, out, default=str, indent=2)
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise CommandError('Cannot create a new backup; nothing changed.') from exc
        # Only update product rows. No account/save/ledger/payment routines run.
        AccountProduct.objects.filter(pk=ecn.pk).update(
            min_deposit=ECN_AFTER['min_deposit'], max_leverage=500, description=ECN_DESCRIPTION)
        AccountProduct.objects.filter(pk=standard.pk).update(is_active=False)
        if vip is None:
            candidate = AccountProduct(**VIP)
            candidate.full_clean()
            candidate.save()
        self.stdout.write(self.style.SUCCESS('Catalogue aligned. Existing accounts, balances and instrument settings untouched.'))

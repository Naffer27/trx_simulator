"""Multilevel commissions. Exact arithmetic, immutable links, existing Treasury settlement."""
from decimal import Decimal
from fractions import Fraction
import logging
import time
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from .models import (Referral, ReferralAttribution, IBCommissionRule, IBCommissionObligation,
                     IBNetworkLink, IBNetworkExecution, IBNetworkCarry, IBNetworkAccrual,
                     LotExecutionEvent, TradingAccount)
from .ib_commission import generate_per_lot_obligation, resolve_applicable_rule

logger = logging.getLogger(__name__)


def _lock_network():
    # One global lock serializes hierarchy changes and accruals, preventing cycles
    # and overlapping ancestor lock deadlocks. Never acquires a trading account lock.
    ct = ContentType.objects.get_for_model(IBNetworkLink)
    ContentType.objects.select_for_update().get(pk=ct.pk)


def ancestors(referral, at_time=None):
    seen = {referral.pk}
    links = IBNetworkLink.objects.select_related('parent')
    if at_time is not None:
        links = links.filter(created_at__lte=at_time)
    current = referral
    result = []
    while True:
        link = links.filter(child=current).first()
        if link is None:
            return result
        if link.parent_id in seen:
            raise ValidationError('La red contiene un ciclo; no se calcularán comisiones.')
        seen.add(link.parent_id)
        current = link.parent
        result.append(current)


def link_sub_ib(child, parent, *, actor=None, reason):
    if not reason.strip():
        raise ValidationError('Indica el motivo del vínculo.')
    with transaction.atomic():
        _lock_network()
        existing = IBNetworkLink.objects.filter(child=child).first()
        if existing:
            if existing.parent_id == parent.pk:
                return existing
            raise ValidationError('Este IB ya tiene un superior. El vínculo histórico no se reemplaza.')
        if child.pk == parent.pk or child.pk in {r.pk for r in ancestors(parent)}:
            raise ValidationError('No se permiten autorreferencias ni ciclos.')
        return IBNetworkLink.objects.create(child=child, parent=parent, created_by=actor, reason=reason)


def attach_from_attribution(referral):
    """When a referred customer becomes an IB, preserve their original introducer."""
    attribution = ReferralAttribution.objects.filter(referred_user_id=referral.user_id).first()
    if attribution:
        return link_sub_ib(referral, attribution.referral, reason='Registro mediante enlace de referido')


def _fraction(value):
    return Fraction(str(value))


def process_execution(event):
    with transaction.atomic():
        _lock_network()
        done = IBNetworkExecution.objects.filter(event=event).first()
        if done:
            return done
        # Lock beneficiaries in ascending PK order, matching risk/settlement locks.
        attr = ReferralAttribution.objects.select_related('referral').filter(referred_user_id=event.account.user_id).first()
        chain = ancestors(attr.referral, event.created_at) if attr else []
        beneficiaries = [attr.referral] + chain if attr else []
        refs = {r.pk: r for r in Referral.objects.select_for_update().filter(pk__in=[r.pk for r in beneficiaries]).order_by('pk')}
        done = IBNetworkExecution.objects.create(event=event)
        if event.account.account_type not in TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES or not attr:
            return done
        if refs[attr.referral_id].risk_status != Referral.RISK_ACTIVE:
            return done
        # Surface ambiguous rules as retryable rather than permanently skipping.
        resolve_applicable_rule(attr.referral, IBCommissionRule.RULE_PER_LOT, event.created_at)
        direct = generate_per_lot_obligation(event)
        if not direct:
            return done
        budget_rule = resolve_applicable_rule(attr.referral, IBCommissionRule.RULE_SUB_IB_BUDGET, event.created_at)
        if not budget_rule or budget_rule.fixed_amount is None:
            return done
        remaining = _fraction(budget_rule.fixed_amount) * _fraction(event.qty)
        for level, ancestor in enumerate(chain, start=1):
            if refs[ancestor.pk].risk_status != Referral.RISK_ACTIVE:
                continue  # no compression: subsequent levels keep their original depth
            rule = resolve_applicable_rule(ancestor, IBCommissionRule.RULE_SUB_IB_PER_LOT, event.created_at)
            if not rule or rule.fixed_amount is None:
                continue
            amount = min(remaining, _fraction(rule.fixed_amount) * _fraction(event.qty) / (2 ** (level - 1)))
            if amount <= 0:
                continue
            remaining -= amount
            carry, _ = IBNetworkCarry.objects.get_or_create(referral=ancestor)
            before = _fraction(carry.remainder)
            total = before + amount
            cents = (total * 100).numerator // (total * 100).denominator
            after = total - Fraction(cents, 100)
            obligation = None
            if cents:
                obligation = IBCommissionObligation.objects.create(
                    referral=ancestor, attribution=attr, rule=rule,
                    rule_type=IBCommissionRule.RULE_SUB_IB_PER_LOT,
                    source_event_type='sub_ib_accrual', source_event_id=done.pk,
                    source_reference=f'Red IB evento {event.pk}, nivel {level}, incluye fracciones acumuladas',
                    basis_quantity=event.qty, calculated_amount=Decimal(cents)/100,
                    metadata={'level': level, 'direct_referral_id': attr.referral_id,
                              'exact_increment': str(amount), 'carry_before': str(before),
                              'carry_after': str(after), 'budget_rule_id': budget_rule.pk,
                              'base_rate': str(rule.fixed_amount), 'divisor': str(2 ** (level - 1)),
                              'includes_prior_subcent_accruals': bool(before)})
            IBNetworkAccrual.objects.create(execution=done, referral=ancestor, rule=rule, level=level,
                exact_amount=str(amount), carry_before=str(before), carry_after=str(after), obligation=obligation)
            carry.remainder = str(after)
            carry.save(update_fields=['remainder'])
        return done


def sweep_network(batch_size=500):
    # Durable receipts replace a moving lookback: a long outage or >500 events
    # cannot silently discard commissions. Activation has no retroactive payouts.
    first = IBCommissionRule.objects.filter(rule_type=IBCommissionRule.RULE_SUB_IB_BUDGET).order_by('effective_from').first()
    if not first:
        return {'scanned': 0, 'generated': 0, 'failed': 0}
    events = LotExecutionEvent.objects.filter(created_at__gte=first.effective_from).annotate(
        done=Exists(IBNetworkExecution.objects.filter(event_id=OuterRef('pk')))
    ).filter(done=False).select_related('account').order_by('pk')[:batch_size]
    result = {'scanned': 0, 'generated': 0, 'failed': 0}
    deadline = time.monotonic() + 8
    for event in events:
        if time.monotonic() >= deadline:
            break  # leave durable backlog for the next scheduled run
        result['scanned'] += 1
        try:
            process_execution(event)
            result['generated'] += 1
        except Exception:
            logger.exception('IB network failed event=%s; retained for retry', event.pk)
            result['failed'] += 1
    return result

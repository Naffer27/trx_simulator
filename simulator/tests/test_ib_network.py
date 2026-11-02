from decimal import Decimal
from fractions import Fraction
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone
from simulator.models import *
from simulator.ib_network import link_sub_ib, process_execution, sweep_network
from simulator.ib_admin_ops import change_commission_rate
from simulator.tests.factories import make_user, make_account
from simulator.tests.test_ib_commission_engine_01 import _make_referral, _make_attribution, _make_lot_event, _make_rule

class NetworkTests(TestCase):
    def setUp(self):
        self.direct = _make_referral()
        self.parents = [_make_referral() for _ in range(4)]
        child = self.direct
        for parent in self.parents:
            link_sub_ib(child, parent, reason='Test network')
            child = parent
        self.trader = make_user()
        _make_attribution(self.trader, self.direct)
        self.account = make_account(user=self.trader, account_type='STANDARD')
        _make_rule(fixed_amount=Decimal('5'))
        call_command('initialize_ib_network', apply=True, stdout=StringIO())

    def event(self, qty='1'):
        return _make_lot_event(self.account, qty=qty)

    def test_exact_amounts_retry_and_budget(self):
        event = self.event()
        done = process_execution(event)
        self.assertEqual(process_execution(event).pk, done.pk)
        amounts = list(done.accruals.order_by('level').values_list('exact_amount',flat=True))
        self.assertEqual(list(map(Fraction, amounts)), [Fraction(1,2),Fraction(1,4),Fraction(1,8),Fraction(1,16)])
        self.assertLess(sum(map(Fraction, amounts)), 1)
        self.assertEqual(IBCommissionObligation.objects.get(rule_type='PER_LOT').calculated_amount, Decimal('5'))
        self.assertEqual(IBNetworkExecution.objects.count(),1)
        self.assertFalse(WalletTransaction.objects.exists())

    def test_small_lots_accumulate_without_rounding_up(self):
        for _ in range(2): process_execution(self.event('.01'))
        obligations=IBCommissionObligation.objects.filter(referral=self.parents[0])
        self.assertEqual(obligations.count(),1)
        self.assertEqual(obligations.get().calculated_amount,Decimal('.01'))
        self.assertEqual(Fraction(IBNetworkCarry.objects.get(referral=self.parents[0]).remainder),0)
        self.assertEqual(Fraction(IBNetworkCarry.objects.get(referral=self.parents[1]).remainder),Fraction(1,200))

    def test_override_future_rate_and_budget_cap(self):
        old=process_execution(self.event())
        admin=make_user(is_staff=True,is_superuser=True)
        req=SimpleNamespace(user=admin)
        change_commission_rate(self.direct,Decimal('7'),request=req)
        change_commission_rate(self.parents[0],Decimal('2'),request=req,rule_type='SUB_IB_PER_LOT')
        new=process_execution(self.event())
        self.assertEqual(IBCommissionObligation.objects.filter(rule_type='PER_LOT').order_by('pk').last().calculated_amount,Decimal('7'))
        self.assertEqual(sum(Fraction(x) for x in new.accruals.values_list('exact_amount',flat=True)),1)
        self.assertEqual(old.accruals.get(level=1).exact_amount,'1/2')

    def test_no_cycles_no_reparenting_and_wide_network(self):
        with self.assertRaises(ValidationError): link_sub_ib(self.parents[-1],self.direct,reason='cycle')
        with self.assertRaises(ValidationError): link_sub_ib(self.direct,self.direct,reason='self')
        with self.assertRaises(ValidationError): link_sub_ib(self.direct,self.parents[-1],reason='move')
        for _ in range(30): link_sub_ib(_make_referral(),self.parents[0],reason='wide')
        self.assertEqual(self.parents[0].network_children.count(),31)

    def test_registration_link_preserved(self):
        newcomer=make_user()
        _make_attribution(newcomer,self.direct)
        child=_make_referral(newcomer)
        self.assertEqual(child.network_parent.parent_id,self.direct.pk)

    def test_demo_excluded_and_no_retroactive_link(self):
        self.account.account_type='DEMO';self.account.save()
        process_execution(self.event())
        self.assertFalse(IBCommissionObligation.objects.exists())
        self.account.account_type='STANDARD';self.account.save()
        event=self.event()
        IBNetworkLink.objects.all().update(created_at=timezone.now()+timezone.timedelta(minutes=1))
        self.assertEqual(process_execution(event).accruals.count(),0)

    def test_frozen_level_not_compressed(self):
        self.parents[0].risk_status=Referral.RISK_FROZEN;self.parents[0].save()
        done=process_execution(self.event())
        self.assertFalse(done.accruals.filter(level=1).exists())
        self.assertEqual(done.accruals.get(level=2).exact_amount,'1/4')

    def test_backlog_batch_and_outage(self):
        for _ in range(5): self.event()
        self.assertEqual(sweep_network(2)['scanned'],2)
        self.assertEqual(sweep_network(2)['scanned'],2)
        self.assertEqual(sweep_network(2)['scanned'],1)
        self.assertEqual(sweep_network(2)['scanned'],0)

    def test_failure_rolls_back_direct_and_retries(self):
        event=self.event()
        with patch('simulator.ib_network.IBNetworkAccrual.objects.create',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError): process_execution(event)
        self.assertFalse(IBNetworkExecution.objects.exists())
        self.assertFalse(IBCommissionObligation.objects.exists())
        self.assertEqual(process_execution(event).accruals.count(),4)

    def test_full_treasury_cycle_for_both_beneficiaries(self):
        from simulator.tests.test_ib_treasury_credit_03 import _settle_fully
        done=process_execution(self.event())
        direct=IBCommissionObligation.objects.get(rule_type='PER_LOT')
        upper=done.accruals.get(level=1).obligation
        for ob in (direct,upper):
            settled,treasury=_settle_fully({'obligation':ob})
            self.assertEqual(settled.status,'CREDITED')
            self.assertEqual(treasury.wallet.user_id,ob.referral.user_id)
            self.assertEqual(treasury.wallet.available_balance,ob.calculated_amount)

    def test_deep_chain_exact_conservation(self):
        child=self.parents[-1]
        for _ in range(26):
            parent=_make_referral();link_sub_ib(child,parent,reason='deep');child=parent
        done=process_execution(self.event())
        amounts=[Fraction(a.exact_amount) for a in done.accruals.all()]
        self.assertEqual(len(amounts),30)
        self.assertEqual(sum(amounts),1-Fraction(1,2**30))
        paid=sum((a.obligation.calculated_amount if a.obligation_id else Decimal(0)) for a in done.accruals.select_related('obligation'))
        carry=sum(Fraction(c.remainder) for c in IBNetworkCarry.objects.all())
        self.assertEqual(Fraction(paid)+carry,sum(amounts))

    def test_portal_is_scoped_to_owner(self):
        from django.urls import reverse
        self.client.force_login(self.parents[0].user)
        response=self.client.get(reverse('simulator:associates_program'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['network_children'].paginator.count,1)
        self.assertEqual(response.context['network_children'][0].child_id,self.direct.pk)
        self.assertIsNotNone(response.context['network_rule'])

    def test_admin_rate_form_for_network_and_permission(self):
        from django.urls import reverse
        admin=make_user(is_staff=True,is_superuser=True)
        self.client.force_login(admin)
        url=reverse('admin:ib_referral_change_rate',args=[self.parents[0].pk])
        response=self.client.get(url,{'rule_type':'SUB_IB_PER_LOT'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'SUB_IB_PER_LOT')
        response=self.client.post(url,{'rule_type':'SUB_IB_PER_LOT','new_fixed_amount':'0.75'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(IBCommissionRule.objects.get(referral=self.parents[0],rule_type='SUB_IB_PER_LOT').fixed_amount,Decimal('.75'))

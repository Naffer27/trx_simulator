"""
simulator/tests/test_audit02_payments_trail.py — AUDIT-02

Audits the Payments & Payout audit trail integration added on top of
AUDIT-01 (simulator/broker_audit.py): funded payout events (H.2/H.3, all
6 call sites in simulator/funded_payouts.py) and the deposit.credited
mirror into BrokerAuditEvent (simulator/views.py::deposit_callback).

No HTTP for the funded payout side — calls the service-layer functions
directly, same convention as test_funded_payout_sim_approval.py /
test_funded_payout_internal_approval.py. Deposit lifecycle events are
tested at the AuditLog level directly (log_audit call sites), and the
deposit.credited mirror is tested at the BrokerAuditEvent level via the
Client (since deposit_callback is a view).

Scope: correct event_type/category/severity/actor per call site,
       funded_payout_request/deposit/correlation_id linkage,
       fail-open (audit write failure never affects the financial result),
       privacy whitelist (metadata never leaks wallet_address/tokens),
       events_for_funded_payout / events_for_deposit / events_by_correlation_id.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from simulator.broker_audit import (
    ActorType,
    Category,
    EV_DEPOSIT_CREDITED,
    EV_FUNDED_PAYOUT_INTERNAL_APPROVED,
    EV_FUNDED_PAYOUT_INTERNAL_COMPLETED,
    EV_FUNDED_PAYOUT_INTERNAL_FAILED,
    EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_FAILED,
    EV_FUNDED_PAYOUT_INTERNAL_SUBMITTED,
    EV_FUNDED_PAYOUT_SIM_APPROVED,
    Severity,
    events_by_correlation_id,
    events_for_deposit,
    events_for_funded_payout,
)
from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.funded_payouts import (
    FundedPayoutAlreadyProcessed,
    approve_internal_payout,
    approve_sim_payout,
    handle_internal_payout_webhook,
)
from simulator.models import (
    BrokerAuditEvent,
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    FundedPayoutRequest,
    LedgerEntry,
    WithdrawalRequest,
)
from .factories import make_deposit, make_user as _make_factory_user

User = get_user_model()

_DEPOSIT_CALLBACK_URL = "/deposit/callback/"


def _ipn_body(payment_id: str, payment_status: str, order_id: str = "",
              actually_paid_amount: str = "50.00") -> str:
    return json.dumps({
        "payment_id":           payment_id,
        "payment_status":       payment_status,
        "order_id":             order_id,
        "actually_paid_amount": actually_paid_amount,
        "pay_currency":         "btc",
        "price_currency":       "usd",
        "price_amount":         float(actually_paid_amount),
    })

_NP_ESTIMATE = "simulator.funded_payouts._np.estimate_price"
_NP_PAYOUT   = "simulator.funded_payouts._np.create_payout"

_NP_ESTIMATE_RET = Decimal("0.000125")
_NP_PAYOUT_RET   = {
    "id": "batch-audit02",
    "status": "CREATED",
    "withdrawals": [{"id": "wd-audit02", "status": "CREATED"}],
}

_seq = 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — same shape as test_funded_payout_{sim,internal}_approval.py
# ─────────────────────────────────────────────────────────────────────────────

def _make_user(role="trader"):
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"a02_{role}_{_seq}",
        email=f"a02_{role}_{_seq}@example.com",
        password="testpass",
    )


def _make_admin():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"a02_admin_{_seq}",
        email=f"a02_admin_{_seq}@example.com",
        password="adminpass",
        is_staff=True,
    )


def _make_product():
    global _seq
    return ChallengeProduct.objects.create(
        name=f"A02-Test-{_seq}",
        account_size=Decimal("10000.00"),
        price_usd=Decimal("99.00"),
        is_active=True,
        p1_profit_target_pct=Decimal("8.00"),
        p1_max_drawdown_pct=Decimal("10.00"),
        p1_max_daily_loss_pct=Decimal("5.00"),
        p1_min_trading_days=0,
        p1_max_duration_days=30,
        p2_profit_target_pct=Decimal("5.00"),
        p2_max_drawdown_pct=Decimal("10.00"),
        p2_max_daily_loss_pct=Decimal("5.00"),
        p2_min_trading_days=0,
        p2_max_duration_days=60,
        max_lot_size=Decimal("5.00"),
        max_open_positions=5,
        profit_split_pct=Decimal("80.00"),
    )


def _make_funded_enrollment(user):
    product    = _make_product()
    enrollment = ChallengeEnrollment.objects.create(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    advance_to_funded(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _make_pending_fpr(user, enrollment, funded_account, funded_config, *, funded_type=FundedConfig.FUNDED_SIM,
                       profit_usd=Decimal("1000.00"), split_pct=Decimal("80.00")):
    initial = Decimal(str(funded_account.initial_balance or funded_account.balance))
    funded_account.balance = initial + profit_usd
    funded_account.equity  = funded_account.balance
    funded_account.save(update_fields=["balance", "equity"])

    cycle_profit = profit_usd
    trader_cut   = (cycle_profit * split_pct / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut   = (cycle_profit - trader_cut).quantize(Decimal("0.01"))

    kwargs = dict(
        user=user, enrollment=enrollment, funded_account=funded_account,
        funded_config=funded_config, funded_type=funded_type,
        cycle_profit=cycle_profit, trader_cut=trader_cut, broker_cut=broker_cut,
        profit_split_pct=split_pct, balance_snapshot=funded_account.balance,
        initial_balance_snapshot=initial, status=FundedPayoutRequest.ST_PENDING,
    )
    if funded_type == FundedConfig.FUNDED_INTERNAL:
        kwargs["crypto_currency"] = "btc"
        kwargs["wallet_address"]  = "bc1qtestaddressforaudit02"
    return FundedPayoutRequest.objects.create(**kwargs)


def _setup_approved_internal_state(user, enrollment, funded_account, funded_config,
                                    *, profit_usd=Decimal("1000.00")):
    """Simulate Phase 1 result of approve_internal_payout without calling NP."""
    initial      = Decimal(str(funded_account.initial_balance or funded_account.balance))
    pre_debit    = initial + profit_usd
    trader_cut   = (profit_usd * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut   = profit_usd - trader_cut
    post_debit   = pre_debit - trader_cut

    funded_account.balance = post_debit
    funded_account.equity  = post_debit
    funded_account.save(update_fields=["balance", "equity"])

    ledger = LedgerEntry.objects.create(
        account=funded_account, event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        amount=-trader_cut, balance_after=post_debit,
    )
    wr = WithdrawalRequest.objects.create(
        user=user, amount_usd=trader_cut, crypto_currency="btc",
        wallet_address="bc1qtestaddressforaudit02",
        status=WithdrawalRequest.STATUS_APPROVED, debit_tx=None,
    )
    fpr = FundedPayoutRequest.objects.create(
        user=user, enrollment=enrollment, funded_account=funded_account,
        funded_config=funded_config, funded_type=FundedConfig.FUNDED_INTERNAL,
        cycle_profit=profit_usd, trader_cut=trader_cut, broker_cut=broker_cut,
        profit_split_pct=Decimal("80.00"), balance_snapshot=pre_debit,
        initial_balance_snapshot=initial, crypto_currency="btc",
        wallet_address="bc1qtestaddressforaudit02",
        status=FundedPayoutRequest.ST_APPROVED, withdrawal_request=wr, ledger_entry=ledger,
    )
    return fpr, wr


class _FundedFixtureMixin:
    def setUp(self):
        self.admin           = _make_admin()
        self.user            = _make_user()
        self.enrollment      = _make_funded_enrollment(self.user)
        self.funded_account  = self.enrollment.funded_account
        self.funded_config   = FundedConfig.objects.get(enrollment=self.enrollment)


# ─────────────────────────────────────────────────────────────────────────────
# H.2 — SIM approval
# ─────────────────────────────────────────────────────────────────────────────

class SimPayoutAuditTests(_FundedFixtureMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.fpr = _make_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config,
        )

    def test_creates_exactly_one_event(self):
        approve_sim_payout(self.fpr, self.admin)
        events = BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr)
        self.assertEqual(events.count(), 1)

    def test_event_shape(self):
        approve_sim_payout(self.fpr, self.admin)
        event = BrokerAuditEvent.objects.get(funded_payout_request=self.fpr)
        self.assertEqual(event.event_type, EV_FUNDED_PAYOUT_SIM_APPROVED)
        self.assertEqual(event.category, Category.PAYMENTS)
        self.assertEqual(event.severity, Severity.INFO)
        self.assertEqual(event.actor_type, ActorType.STAFF)
        self.assertEqual(event.actor_id, self.admin.pk)
        self.assertEqual(event.account_id, self.funded_account.pk)
        self.assertEqual(event.event_version, 1)

    def test_correlation_id_matches_fpr(self):
        approve_sim_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        event = BrokerAuditEvent.objects.get(funded_payout_request=self.fpr)
        self.assertIsNotNone(self.fpr.correlation_id)
        self.assertEqual(event.correlation_id, self.fpr.correlation_id)

    def test_metadata_whitelist(self):
        approve_sim_payout(self.fpr, self.admin)
        event = BrokerAuditEvent.objects.get(funded_payout_request=self.fpr)
        allowed = {"trader_cut", "broker_cut", "wallet_tx_id"}
        self.assertTrue(set(event.metadata.keys()).issubset(allowed))

    def test_fail_open_does_not_affect_payout(self):
        with patch(
            "simulator.models.BrokerAuditEvent.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            approve_sim_payout(self.fpr, self.admin)  # must not raise
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)
        self.assertEqual(BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count(), 0)

    def test_events_for_funded_payout_helper(self):
        approve_sim_payout(self.fpr, self.admin)
        found = events_for_funded_payout(self.fpr.pk)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].event_type, EV_FUNDED_PAYOUT_SIM_APPROVED)

    def test_double_approval_does_not_duplicate_event(self):
        """
        A second approve_sim_payout() call on an already-COMPLETED FPR must
        raise FundedPayoutAlreadyProcessed *before* reaching record_payment_event()
        — the pre-existing status guard, not a new AUDIT-02 dedup mechanism,
        is what protects this. This test proves that guarantee holds for the
        audit trail specifically, not just for the financial state.
        """
        approve_sim_payout(self.fpr, self.admin)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count(), 1,
        )

        with self.assertRaises(FundedPayoutAlreadyProcessed):
            approve_sim_payout(self.fpr, self.admin)

        self.assertEqual(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# H.3 — INTERNAL approval (Phase 1 + Phase 2)
# ─────────────────────────────────────────────────────────────────────────────

class InternalPayoutApprovalAuditTests(_FundedFixtureMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.fpr = _make_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config,
            funded_type=FundedConfig.FUNDED_INTERNAL,
        )

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_full_success_creates_two_events(self, _est, _pay):
        approve_internal_payout(self.fpr, self.admin, callback_url="https://example.test/cb")
        events = list(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).order_by("id")
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, EV_FUNDED_PAYOUT_INTERNAL_APPROVED)
        self.assertEqual(events[1].event_type, EV_FUNDED_PAYOUT_INTERNAL_SUBMITTED)

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_same_correlation_id_across_both_events(self, _est, _pay):
        approve_internal_payout(self.fpr, self.admin, callback_url="https://example.test/cb")
        self.fpr.refresh_from_db()
        events = list(BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].correlation_id, self.fpr.correlation_id)
        self.assertEqual(events[1].correlation_id, self.fpr.correlation_id)

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_failure_creates_high_severity_event(self, _est, _pay):
        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin, callback_url="https://example.test/cb")
        events = list(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).order_by("id")
        )
        # APPROVED (phase 1) + SUBMIT_FAILED (compensating reversal)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, EV_FUNDED_PAYOUT_INTERNAL_APPROVED)
        self.assertEqual(events[1].event_type, EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_FAILED)
        self.assertEqual(events[1].severity, Severity.HIGH)
        self.assertEqual(events[1].actor_type, ActorType.SYSTEM)

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_double_approval_does_not_duplicate_event(self, _est, _pay):
        """
        Same guarantee as SimPayoutAuditTests.test_double_approval_does_not_
        duplicate_event, for the FUNDED_INTERNAL flow: a second
        approve_internal_payout() call on an already-APPROVED FPR raises
        FundedPayoutAlreadyProcessed before any new audit event is written.
        """
        approve_internal_payout(self.fpr, self.admin, callback_url="https://example.test/cb")
        before_count = BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count()
        self.assertEqual(before_count, 2)  # APPROVED + SUBMITTED

        with self.assertRaises(FundedPayoutAlreadyProcessed):
            approve_internal_payout(self.fpr, self.admin, callback_url="https://example.test/cb")

        self.assertEqual(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count(), before_count,
        )

    @patch(_NP_PAYOUT,   side_effect=RuntimeError("NP down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_fail_open_does_not_block_reversal(self, _est, _pay):
        with patch(
            "simulator.models.BrokerAuditEvent.objects.create",
            side_effect=RuntimeError("audit boom"),
        ):
            with self.assertRaises(RuntimeError):
                approve_internal_payout(self.fpr, self.admin, callback_url="https://example.test/cb")
        self.fpr.refresh_from_db()
        # The NP RuntimeError must still be the one that propagates, and the
        # compensating reversal (tested elsewhere) must still have run —
        # an audit-write failure must never mask or replace the real error.
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_FAILED)


# ─────────────────────────────────────────────────────────────────────────────
# H.3 — webhook (completed / failed)
# ─────────────────────────────────────────────────────────────────────────────

class InternalPayoutWebhookAuditTests(_FundedFixtureMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.fpr, self.wr = _setup_approved_internal_state(
            self.user, self.enrollment, self.funded_account, self.funded_config,
        )

    def test_webhook_completed_creates_event(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-audit02"
        )
        event = BrokerAuditEvent.objects.get(funded_payout_request=self.fpr)
        self.assertEqual(event.event_type, EV_FUNDED_PAYOUT_INTERNAL_COMPLETED)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)
        self.assertEqual(event.severity, Severity.INFO)

    def test_webhook_completed_idempotent_no_duplicate_event(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-audit02"
        )
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-audit02"
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count(), 1,
        )

    def test_webhook_failed_creates_high_severity_event(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-audit02"
        )
        event = BrokerAuditEvent.objects.get(funded_payout_request=self.fpr)
        self.assertEqual(event.event_type, EV_FUNDED_PAYOUT_INTERNAL_FAILED)
        self.assertEqual(event.severity, Severity.HIGH)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)

    def test_webhook_failed_idempotent_no_duplicate_event(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-audit02"
        )
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-audit02"
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).count(), 1,
        )

    def test_fail_open_webhook_completed(self):
        with patch(
            "simulator.models.BrokerAuditEvent.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            handle_internal_payout_webhook(
                self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-audit02"
            )  # must not raise
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)


# ─────────────────────────────────────────────────────────────────────────────
# correlation_id spans the whole H.3 lifecycle (approve → NP submit → webhook)
# ─────────────────────────────────────────────────────────────────────────────

class CorrelationAcrossLifecycleTests(_FundedFixtureMixin, TestCase):

    @patch(_NP_PAYOUT,   return_value=_NP_PAYOUT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_correlation_id_unifies_approval_and_webhook(self, _est, _pay):
        fpr = _make_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config,
            funded_type=FundedConfig.FUNDED_INTERNAL,
        )
        approve_internal_payout(fpr, self.admin, callback_url="https://example.test/cb")
        fpr.refresh_from_db()
        wr = fpr.withdrawal_request

        # Simulate the async webhook arriving later — a separate "process"
        # in reality, here just a separate call — reading correlation_id
        # back from the DB row, not from any in-memory state.
        handle_internal_payout_webhook(fpr, wr, WithdrawalRequest.STATUS_COMPLETED, "wd-audit02")

        events = events_by_correlation_id(fpr.correlation_id)
        event_types = {e.event_type for e in events}
        self.assertEqual(
            event_types,
            {
                EV_FUNDED_PAYOUT_INTERNAL_APPROVED,
                EV_FUNDED_PAYOUT_INTERNAL_SUBMITTED,
                EV_FUNDED_PAYOUT_INTERNAL_COMPLETED,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Deposit lifecycle — AuditLog activation + BrokerAuditEvent mirror
# ─────────────────────────────────────────────────────────────────────────────

class DepositAuditTrailTests(TestCase):
    """
    Convention matches test_deposit.py: @csrf_exempt view, verify_ipn_signature
    mocked (its own correctness is tested elsewhere), self.client from TestCase
    (no login needed — deposit_callback is a webhook, not a user-facing view).
    """

    def test_deposit_has_correlation_id_on_creation(self):
        deposit = make_deposit(user=_make_factory_user(), amount_usd=Decimal("50.00"),
                                payment_id="np-audit02-corr")
        self.assertIsNotNone(deposit.correlation_id)

    @patch("simulator.views._np.create_payment", return_value={
        "payment_id": "np-audit02-created", "invoice_url": "https://np.test/inv",
        "pay_address": "addr", "pay_amount": "0.001",
    })
    def test_deposit_view_post_writes_ev_deposit_created(self, _mock_create):
        from simulator.models import AuditLog
        user = _make_factory_user()
        self.client.force_login(user)

        self.client.post(
            "/deposit/", {"amount_usd": "50", "crypto_currency": "btc"},
        )

        self.assertTrue(
            AuditLog.objects.filter(event_type="deposit.created", user=user).exists()
        )

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_deposit_callback_credits_and_mirrors_event(self, _mock_sig):
        deposit = make_deposit(user=_make_factory_user(), amount_usd=Decimal("50.00"),
                                payment_id="np-audit02-test")

        body = _ipn_body("np-audit02-test", "finished", str(deposit.pk))
        resp = self.client.post(_DEPOSIT_CALLBACK_URL, body, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        deposit.refresh_from_db()
        self.assertTrue(deposit.credited)

        event = BrokerAuditEvent.objects.get(deposit=deposit, event_type=EV_DEPOSIT_CREDITED)
        self.assertEqual(event.category, Category.PAYMENTS)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)
        self.assertEqual(event.correlation_id, deposit.correlation_id)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_events_for_deposit_helper(self, _mock_sig):
        deposit = make_deposit(user=_make_factory_user(), amount_usd=Decimal("25.00"),
                                payment_id="np-audit02-test-2")
        body = _ipn_body("np-audit02-test-2", "finished", str(deposit.pk),
                          actually_paid_amount="25.00")
        self.client.post(_DEPOSIT_CALLBACK_URL, body, content_type="application/json")

        found = events_for_deposit(deposit.pk)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].event_type, EV_DEPOSIT_CREDITED)

    @patch("simulator.nowpayments.verify_ipn_signature", return_value=True)
    def test_duplicate_callback_records_second_auditlog_entry_but_not_second_mirror(self, _mock_sig):
        """
        EV_DEPOSIT_CALLBACK (AuditLog) fires on every attempt, including
        duplicates — that's the point (§2 of the design). But the
        BrokerAuditEvent mirror of deposit.credited must NOT duplicate,
        since the idempotency gate in deposit_callback returns before
        reaching the credit block on the second call.
        """
        from simulator.models import AuditLog
        deposit = make_deposit(user=_make_factory_user(), amount_usd=Decimal("10.00"),
                                payment_id="np-audit02-dup")
        body = _ipn_body("np-audit02-dup", "finished", str(deposit.pk),
                          actually_paid_amount="10.00")

        self.client.post(_DEPOSIT_CALLBACK_URL, body, content_type="application/json")
        self.client.post(_DEPOSIT_CALLBACK_URL, body, content_type="application/json")  # duplicate IPN

        self.assertEqual(
            AuditLog.objects.filter(event_type="deposit.callback").count(), 2,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(deposit=deposit, event_type=EV_DEPOSIT_CREDITED).count(), 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Append-only protection already covers the new fields (AUDIT-01's
# BrokerAuditEventAdmin has_add/change/delete_permission = False applies
# unchanged — no new test needed there since no permission logic changed).
# ─────────────────────────────────────────────────────────────────────────────

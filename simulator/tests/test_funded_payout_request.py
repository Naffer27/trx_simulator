"""
simulator/tests/test_funded_payout_request.py — Bloque H.1

Audits the funded payout request endpoint (H.1 foundation):
  POST /funded/payout/request/

Scope: gate enforcement + FundedPayoutRequest creation only.
No funds move in H.1. H.2/H.3 cover approval flows.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    EmailVerification,
    FundedConfig,
    FundedPayoutRequest,
    KYCProfile,
    TermsAcceptance,
    TOTPDevice,
    TradingAccount,
    TERMS_VERSION,
    RISK_DISCLOSURE_VERSION,
)

User = get_user_model()

_ZERO  = Decimal("0")
_PENNY = Decimal("0.01")
_URL   = "simulator:funded_payout_request"

_seq = 0


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_user(password="testpass"):
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h1_{_seq}",
        email=f"h1_{_seq}@example.com",
        password=password,
    )


def _add_compliance(user):
    """Attach email-verified, terms-accepted, KYC-approved, and TOTP-confirmed records."""
    EmailVerification.objects.create(user=user, verified=True)
    TermsAcceptance.objects.create(
        user=user,
        terms_version=TERMS_VERSION,
        risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
    )
    KYCProfile.objects.create(user=user, status=KYCProfile.STATUS_APPROVED)
    TOTPDevice.objects.create(user=user, secret="FAKESECRETFORTEST", confirmed=True)


def _make_product():
    global _seq
    return ChallengeProduct.objects.create(
        name=f"H1-Test-{_seq}",
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
    """Force-advance enrollment through both phases to ST_FUNDED."""
    product = _make_product()
    enrollment = ChallengeEnrollment.objects.create(user=user, product=product)
    activate_challenge_enrollment(enrollment)
    enrollment.refresh_from_db()
    advance_to_phase2(enrollment)
    enrollment.refresh_from_db()
    advance_to_funded(enrollment)
    enrollment.refresh_from_db()
    return enrollment


def _set_profit(account, profit_usd):
    """Set funded account balance so cycle_profit == profit_usd."""
    initial = Decimal(str(account.initial_balance or account.balance))
    account.balance = initial + profit_usd
    account.save()


# ─────────────────────────────────────────────────────────────────────────────
# Compliance + eligibility gate tests
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(LOAD_TEST_MODE=True)
class TestFundedPayoutRequestGates(TestCase):
    """
    Each test removes exactly one gate condition and verifies the request is blocked.
    setUp builds a fully compliant, eligible scenario as the baseline.

    LOAD_TEST_MODE=True bypasses the Redis rate limiter (which uses persisted keys
    that collide across test runs since the test DB resets auto-increment PKs).
    """

    def setUp(self):
        self.user = _make_user()
        _add_compliance(self.user)
        self.enrollment = _make_funded_enrollment(self.user)
        self.account = self.enrollment.funded_account
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)

        # Relax payout cycle gates so only the gate under test can fail
        self.fc.min_payout_usd    = Decimal("50.00")
        self.fc.min_trading_days  = 0
        self.fc.save()

        # Profitable balance: cycle_profit = $100 > min_payout $50
        _set_profit(self.account, Decimal("100.00"))

        self.client.login(username=self.user.username, password="testpass")
        self.url = reverse(_URL)

    def _post(self, **extra):
        data = {"enrollment_id": self.enrollment.pk, "otp_code": "000000"}
        data.update(extra)
        return self.client.post(self.url, data)

    # ── Email gate ────────────────────────────────────────────────────────────

    def test_blocked_no_email(self):
        EmailVerification.objects.filter(user=self.user).delete()
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── Terms gate ────────────────────────────────────────────────────────────

    def test_blocked_no_terms(self):
        TermsAcceptance.objects.filter(user=self.user).delete()
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── 2FA device gate ───────────────────────────────────────────────────────

    def test_blocked_no_2fa_device(self):
        TOTPDevice.objects.filter(user=self.user).delete()
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── 2FA OTP gate ──────────────────────────────────────────────────────────

    @patch("simulator.two_factor.verify_totp", return_value=False)
    def test_blocked_wrong_otp(self, _mock):
        resp = self._post(otp_code="999999")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── KYC gate ─────────────────────────────────────────────────────────────

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_blocked_no_kyc(self, _mock):
        self.user.kyc_profile.status = KYCProfile.STATUS_PENDING
        self.user.kyc_profile.save()
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── Account status gate ───────────────────────────────────────────────────

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_blocked_account_suspended(self, _mock):
        self.account.status = "Suspendido"
        self.account.save()
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── Profit gate ───────────────────────────────────────────────────────────

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_blocked_profit_below_min(self, _mock):
        self.fc.min_payout_usd = Decimal("500.00")
        self.fc.save()
        # cycle_profit is still $100, well below $500
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertIn("profit", resp.json()["error"].lower())
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── Trading days gate ─────────────────────────────────────────────────────

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_blocked_days_below_min(self, _mock):
        self.fc.min_trading_days = 5  # no closed trades exist → 0 days
        self.fc.save()
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertIn("trading", resp.json()["error"].lower())
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── Duplicate request gate ────────────────────────────────────────────────

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_blocked_pending_already_exists(self, _mock):
        FundedPayoutRequest.objects.create(
            enrollment=self.enrollment,
            funded_account=self.account,
            funded_config=self.fc,
            user=self.user,
            cycle_profit=Decimal("100.00"),
            trader_cut=Decimal("80.00"),
            broker_cut=Decimal("20.00"),
            profit_split_pct=Decimal("80.00"),
            balance_snapshot=Decimal("10100.00"),
            initial_balance_snapshot=Decimal("10000.00"),
            funded_type=self.fc.funded_type,
            status=FundedPayoutRequest.ST_PENDING,
        )
        resp = self._post()
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 1)  # still only 1

    # ── Non-funded enrollment ─────────────────────────────────────────────────

    def test_non_funded_enrollment_returns_400(self):
        """Enrollment in PHASE_1 (not FUNDED) must be rejected."""
        user2 = _make_user()
        _add_compliance(user2)
        product2 = _make_product()
        enr2 = ChallengeEnrollment.objects.create(user=user2, product=product2)
        activate_challenge_enrollment(enr2)
        enr2.refresh_from_db()
        # enr2 is still in PHASE_1 — not funded

        self.client.login(username=user2.username, password="testpass")
        resp = self.client.post(
            self.url,
            {"enrollment_id": enr2.pk, "otp_code": "000000"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)

    # ── Unauthenticated ───────────────────────────────────────────────────────

    def test_unauthenticated_is_redirected(self):
        self.client.logout()
        resp = self._post()
        self.assertIn(resp.status_code, [302, 403])
        self.assertEqual(FundedPayoutRequest.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Happy path: FundedPayoutRequest creation and snapshot correctness
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(LOAD_TEST_MODE=True)
class TestFundedPayoutRequestCreation(TestCase):
    """Verify that a passing request creates a correct FPR snapshot."""

    def setUp(self):
        self.user = _make_user()
        _add_compliance(self.user)
        self.enrollment = _make_funded_enrollment(self.user)
        self.account = self.enrollment.funded_account
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fc.min_payout_usd   = Decimal("50.00")
        self.fc.min_trading_days = 0
        self.fc.save()
        _set_profit(self.account, Decimal("200.00"))
        self.client.login(username=self.user.username, password="testpass")
        self.url = reverse(_URL)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_returns_201_with_fpr_id(self, _mock):
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("id", data)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_fpr_status_is_pending(self, _mock):
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        fpr = FundedPayoutRequest.objects.get(pk=resp.json()["id"])
        self.assertEqual(fpr.status, FundedPayoutRequest.ST_PENDING)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_fpr_cycle_profit_snapshot(self, _mock):
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        fpr = FundedPayoutRequest.objects.get(pk=resp.json()["id"])
        self.assertEqual(fpr.cycle_profit, Decimal("200.00"))

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_fpr_balance_and_initial_snapshot(self, _mock):
        self.account.refresh_from_db()
        expected_balance = Decimal(str(self.account.balance))
        expected_initial = Decimal(str(self.account.initial_balance))
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        fpr = FundedPayoutRequest.objects.get(pk=resp.json()["id"])
        self.assertEqual(fpr.balance_snapshot,         expected_balance)
        self.assertEqual(fpr.initial_balance_snapshot, expected_initial)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_fpr_funded_type_snapshot(self, _mock):
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        fpr = FundedPayoutRequest.objects.get(pk=resp.json()["id"])
        self.assertEqual(fpr.funded_type, self.fc.funded_type)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_no_funds_moved(self, _mock):
        """H.1: account balance and wallet are untouched after request creation."""
        balance_before = Decimal(str(self.account.balance))
        self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        self.account.refresh_from_db()
        self.assertEqual(Decimal(str(self.account.balance)), balance_before)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_ledger_fields_null_at_creation(self, _mock):
        """H.1: ledger_entry, wallet_credit_tx, withdrawal_request are null until H.2/H.3."""
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "123456"})
        fpr = FundedPayoutRequest.objects.get(pk=resp.json()["id"])
        self.assertIsNone(fpr.ledger_entry)
        self.assertIsNone(fpr.wallet_credit_tx)
        self.assertIsNone(fpr.withdrawal_request)
        self.assertIsNone(fpr.cycle_reset_at)


# ─────────────────────────────────────────────────────────────────────────────
# Decimal split coherence (pure math — no DB required)
# ─────────────────────────────────────────────────────────────────────────────

class TestDecimalSplitCoherence(TestCase):
    """
    trader_cut + broker_cut must always equal cycle_profit, with no float
    intermediate values. Uses the same formula as funded_payout_request_view.
    """

    def _split(self, cycle_profit, split_pct):
        _HUNDRED = Decimal("100")
        _PENNY   = Decimal("0.01")
        trader_cut = (cycle_profit * split_pct / _HUNDRED).quantize(_PENNY)
        broker_cut = (cycle_profit - trader_cut).quantize(_PENNY)
        return trader_cut, broker_cut

    def test_80_20_split_sums_to_cycle_profit(self):
        cp = Decimal("200.00")
        tc, bc = self._split(cp, Decimal("80.00"))
        self.assertEqual(tc + bc, cp)

    def test_trader_cut_80_pct_of_200(self):
        tc, _ = self._split(Decimal("200.00"), Decimal("80.00"))
        self.assertEqual(tc, Decimal("160.00"))

    def test_broker_cut_20_pct_of_200(self):
        _, bc = self._split(Decimal("200.00"), Decimal("80.00"))
        self.assertEqual(bc, Decimal("40.00"))

    def test_70_30_split_sums_to_cycle_profit(self):
        cp = Decimal("333.33")
        tc, bc = self._split(cp, Decimal("70.00"))
        self.assertEqual(tc + bc, cp)

    def test_90_10_split_sums_to_cycle_profit(self):
        cp = Decimal("1000.00")
        tc, bc = self._split(cp, Decimal("90.00"))
        self.assertEqual(tc + bc, cp)

    def test_zero_profit_gives_zero_split(self):
        tc, bc = self._split(Decimal("0.00"), Decimal("80.00"))
        self.assertEqual(tc, Decimal("0.00"))
        self.assertEqual(bc, Decimal("0.00"))

    def test_odd_amount_still_sums_correctly(self):
        cp = Decimal("100.01")
        tc, bc = self._split(cp, Decimal("80.00"))
        self.assertEqual(tc + bc, cp)

    def test_cycle_profit_negative_balance_floors_at_zero(self):
        balance  = Decimal("9800.00")
        initial  = Decimal("10000.00")
        cycle_profit = max(Decimal("0"), balance - initial)
        self.assertEqual(cycle_profit, Decimal("0"))

    def test_all_snapshot_fields_are_decimal_not_float(self):
        """Verify the view formula never introduces float values."""
        cycle_profit = Decimal("200.00")
        split_pct    = Decimal("80.00")
        _HUNDRED     = Decimal("100")
        _PENNY       = Decimal("0.01")
        trader_cut   = (cycle_profit * split_pct / _HUNDRED).quantize(_PENNY)
        broker_cut   = (cycle_profit - trader_cut).quantize(_PENNY)
        self.assertIsInstance(trader_cut, Decimal)
        self.assertIsInstance(broker_cut, Decimal)
        self.assertIsInstance(cycle_profit, Decimal)


# ─────────────────────────────────────────────────────────────────────────────
# Model constants
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedPayoutRequestConstants(TestCase):
    """Verify that H.1 constants are correctly defined on the model."""

    def test_ev_funded_payout_exists_on_ledger_entry(self):
        from simulator.models import LedgerEntry
        self.assertEqual(LedgerEntry.EV_FUNDED_PAYOUT, "FUNDED_PAYOUT")

    def test_ev_funded_payout_in_event_choices(self):
        from simulator.models import LedgerEntry
        codes = [c[0] for c in LedgerEntry.EVENT_CHOICES]
        self.assertIn("FUNDED_PAYOUT", codes)

    def test_tx_funded_payout_exists_on_wallet_transaction(self):
        from simulator.models import WalletTransaction
        self.assertEqual(WalletTransaction.TX_FUNDED_PAYOUT, "FUNDED_PAYOUT")

    def test_tx_funded_payout_in_tx_choices(self):
        from simulator.models import WalletTransaction
        codes = [c[0] for c in WalletTransaction.TX_CHOICES]
        self.assertIn("FUNDED_PAYOUT", codes)

    def test_funded_payout_request_status_constants(self):
        self.assertEqual(FundedPayoutRequest.ST_PENDING,    "pending")
        self.assertEqual(FundedPayoutRequest.ST_APPROVED,   "approved")
        self.assertEqual(FundedPayoutRequest.ST_PROCESSING, "processing")
        self.assertEqual(FundedPayoutRequest.ST_COMPLETED,  "completed")
        self.assertEqual(FundedPayoutRequest.ST_REJECTED,   "rejected")
        self.assertEqual(FundedPayoutRequest.ST_FAILED,     "failed")
        self.assertEqual(FundedPayoutRequest.ST_CANCELLED,  "cancelled")


# ─────────────────────────────────────────────────────────────────────────────
# BBOOK-CLOSE-02 (F-01) — fpr_one_active_request_per_enrollment
#
# migration 0095 adds a partial UniqueConstraint on (enrollment) where
# status IN (pending, approved, processing) — DB-level defense-in-depth on
# top of the pre-existing select_for_update()+.exists() guard in
# funded_payout_request_view. These tests prove the constraint itself,
# independent of the view, then prove the real view-level race under real
# threads, then prove no downstream monetary side effect ever double-fires.
# ─────────────────────────────────────────────────────────────────────────────

import random
import threading
import time

from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import TransactionTestCase

from simulator.funded_payouts import approve_sim_payout
from simulator.models import BrokerLedger, LedgerEntry, Wallet, WalletTransaction, WithdrawalRequest
from simulator.wallet_ledger import get_or_create_wallet

_ACTIVE_STATUSES = [
    FundedPayoutRequest.ST_PENDING,
    FundedPayoutRequest.ST_APPROVED,
    FundedPayoutRequest.ST_PROCESSING,
]
_TERMINAL_STATUSES = [
    FundedPayoutRequest.ST_COMPLETED,
    FundedPayoutRequest.ST_REJECTED,
    FundedPayoutRequest.ST_CANCELLED,
    FundedPayoutRequest.ST_FAILED,
]


def _make_fpr_row(enrollment, account, fc, user, status):
    """Direct ORM creation — bypasses the view entirely, for constraint-only proofs."""
    return FundedPayoutRequest.objects.create(
        enrollment=enrollment,
        funded_account=account,
        funded_config=fc,
        user=user,
        cycle_profit=Decimal("100.00"),
        trader_cut=Decimal("80.00"),
        broker_cut=Decimal("20.00"),
        profit_split_pct=Decimal("80.00"),
        balance_snapshot=Decimal("10100.00"),
        initial_balance_snapshot=Decimal("10000.00"),
        funded_type=fc.funded_type,
        status=status,
    )


class FPRConstraintDirectTests(TestCase):
    """Items: constraint proven directly via ORM, every active×active
    combination blocked, every terminal status allows a new request —
    none of this goes through funded_payout_request_view."""

    def setUp(self):
        self.user = _make_user()
        self.enrollment = _make_funded_enrollment(self.user)
        self.account = self.enrollment.funded_account
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)

    def _assert_second_blocked(self, first_status, second_status):
        _make_fpr_row(self.enrollment, self.account, self.fc, self.user, first_status)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_fpr_row(self.enrollment, self.account, self.fc, self.user, second_status)
        self.assertEqual(FundedPayoutRequest.objects.filter(enrollment=self.enrollment).count(), 1)

    def test_pending_plus_pending_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_PENDING, FundedPayoutRequest.ST_PENDING)

    def test_pending_plus_approved_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_PENDING, FundedPayoutRequest.ST_APPROVED)

    def test_pending_plus_processing_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_PENDING, FundedPayoutRequest.ST_PROCESSING)

    def test_approved_plus_pending_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_APPROVED, FundedPayoutRequest.ST_PENDING)

    def test_approved_plus_approved_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_APPROVED, FundedPayoutRequest.ST_APPROVED)

    def test_approved_plus_processing_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_APPROVED, FundedPayoutRequest.ST_PROCESSING)

    def test_processing_plus_pending_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_PROCESSING, FundedPayoutRequest.ST_PENDING)

    def test_processing_plus_approved_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_PROCESSING, FundedPayoutRequest.ST_APPROVED)

    def test_processing_plus_processing_blocked(self):
        self._assert_second_blocked(FundedPayoutRequest.ST_PROCESSING, FundedPayoutRequest.ST_PROCESSING)

    def _assert_new_request_allowed_after(self, terminal_status):
        _make_fpr_row(self.enrollment, self.account, self.fc, self.user, terminal_status)
        # Must NOT raise — a terminal-status prior request never blocks a new one.
        _make_fpr_row(self.enrollment, self.account, self.fc, self.user, FundedPayoutRequest.ST_PENDING)
        self.assertEqual(FundedPayoutRequest.objects.filter(enrollment=self.enrollment).count(), 2)

    def test_completed_allows_new_request(self):
        self._assert_new_request_allowed_after(FundedPayoutRequest.ST_COMPLETED)

    def test_rejected_allows_new_request(self):
        self._assert_new_request_allowed_after(FundedPayoutRequest.ST_REJECTED)

    def test_cancelled_allows_new_request(self):
        self._assert_new_request_allowed_after(FundedPayoutRequest.ST_CANCELLED)

    def test_failed_allows_new_request(self):
        # ST_FAILED is verified to exist on the model (TestFundedPayoutRequestConstants,
        # above) before being used here.
        self._assert_new_request_allowed_after(FundedPayoutRequest.ST_FAILED)

    def test_different_enrollment_unaffected(self):
        """Sanity: the constraint is scoped per-enrollment, not global."""
        user2 = _make_user()
        enrollment2 = _make_funded_enrollment(user2)
        fc2 = FundedConfig.objects.get(enrollment=enrollment2)
        _make_fpr_row(self.enrollment, self.account, self.fc, self.user, FundedPayoutRequest.ST_PENDING)
        # Must NOT raise — unrelated enrollment.
        _make_fpr_row(enrollment2, enrollment2.funded_account, fc2, user2, FundedPayoutRequest.ST_PENDING)
        self.assertEqual(FundedPayoutRequest.objects.count(), 2)


@override_settings(LOAD_TEST_MODE=True)
class FPRSequentialGuardStillWorksTests(TestCase):
    """Confirms the pre-existing app-level fast-path (select_for_update()+
    .exists(), returning a graceful 409) still works unmodified — the new
    DB constraint is defense-in-depth, not a replacement."""

    def setUp(self):
        self.user = _make_user()
        _add_compliance(self.user)
        self.enrollment = _make_funded_enrollment(self.user)
        self.account = self.enrollment.funded_account
        self.fc = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fc.min_payout_usd = Decimal("50.00")
        self.fc.min_trading_days = 0
        self.fc.save()
        _set_profit(self.account, Decimal("100.00"))
        self.client.login(username=self.user.username, password="testpass")
        self.url = reverse(_URL)

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_pending_already_exists_still_returns_409_not_500(self, _mock):
        _make_fpr_row(self.enrollment, self.account, self.fc, self.user, FundedPayoutRequest.ST_PENDING)
        resp = self.client.post(self.url, {"enrollment_id": self.enrollment.pk, "otp_code": "000000"})
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(FundedPayoutRequest.objects.filter(enrollment=self.enrollment).count(), 1)


def _run_concurrent(fn, barrier, results, index, max_retries=60):
    """Vendor-aware concurrency helper. BBOOK-CLOSE-02 FASE A found the
    repo's existing PRAGMA-only retry helper (test_challenge_wallet_purchase.py,
    test_funded_economics.py) silently vacuous under PostgreSQL: it issues
    'PRAGMA busy_timeout' unconditionally, which PostgreSQL rejects with a
    SyntaxError that none of that helper's except clauses catch — the
    background thread dies uncaught, results[index] is never set, and a
    test that only checks aggregate state passes without ever having
    exercised the real path. This version:
      1. only issues the SQLite PRAGMA on SQLite;
      2. always records a definite outcome for every thread, on every
         engine, via a catch-all fallback — no exception can leave
         results[index] silently unset.
    """
    if connection.vendor == "sqlite3":
        with connection.cursor() as cur:
            cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = ("ok", fn())
                return
            except IntegrityError as exc:
                results[index] = ("integrity_error", exc)
                return
            except OperationalError as exc:
                if (
                    connection.vendor == "sqlite3"
                    and "locked" in str(exc).lower()
                    and attempt < max_retries
                ):
                    time.sleep(random.uniform(0.005, 0.03))
                    continue
                results[index] = ("operational_error", exc)
                return
            except Exception as exc:  # noqa: BLE001 — deliberate catch-all, see docstring
                results[index] = (f"unexpected_{type(exc).__name__}", exc)
                return
    finally:
        connection.close()


def _skip_unless_postgres(fn):
    """BBOOK-CLOSE-02 SQLITE STABILIZATION — same precedent already
    accepted for PGFIX01SqlShapeTests (test_challenge_engine.py): a real
    concurrent-timing race is only ever meaningfully certifiable under
    PostgreSQL's real row-level locking. On SQLite this decorator makes
    the test show as explicitly SKIPPED — never a silent/vacuous PASS,
    never a crash from artificial django_session write contention."""
    import unittest
    return unittest.skipUnless(
        connection.vendor == "postgresql",
        "real concurrent HTTP race requires PostgreSQL's real row-level "
        "locking — SQLite's own table-level write lock on django_session "
        "makes two genuinely concurrent Client().login() calls contend "
        "with each other independent of the FundedPayoutRequest business "
        "logic under test (see BBOOK-CLOSE-02 SQLite Stabilization FASE A)",
    )(fn)


def _make_ready_fpr_user():
    """Shared setup for both Test A (deterministic) and Test B (real
    concurrency) — a fully compliant, eligible user ready to request a
    funded payout."""
    user = _make_user()
    _add_compliance(user)
    enrollment = _make_funded_enrollment(user)
    account = enrollment.funded_account
    fc = FundedConfig.objects.get(enrollment=enrollment)
    fc.min_payout_usd = Decimal("50.00")
    fc.min_trading_days = 0
    fc.save()
    _set_profit(account, Decimal("100.00"))
    return user, enrollment, account, fc


@override_settings(LOAD_TEST_MODE=True)
class FPRSequentialDuplicateGuardTests(TestCase):
    """Test A — BBOOK-CLOSE-02 SQLite Stabilization. Deterministic, single
    client/session, no threading at all: proves the exact same economic
    invariant FPRRealConcurrencyTests proves under real concurrency — at
    most one active FundedPayoutRequest per enrollment — with zero
    flakiness, on every engine. Complements, does not replace, the real
    race in Test B below."""

    @patch("simulator.two_factor.verify_totp", return_value=True)
    def test_second_request_after_first_rejected_deterministic(self, _mock):
        user, enrollment, account, fc = _make_ready_fpr_user()
        balance_before = Decimal(str(account.balance))
        self.client.login(username=user.username, password="testpass")
        url = reverse(_URL)
        data = {"enrollment_id": enrollment.pk, "otp_code": "000000"}

        r1 = self.client.post(url, data)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(FundedPayoutRequest.objects.filter(enrollment=enrollment).count(), 1)

        r2 = self.client.post(url, data)
        self.assertEqual(r2.status_code, 409)
        self.assertFalse(r2.json()["ok"])

        active_qs = FundedPayoutRequest.objects.filter(
            enrollment=enrollment, status__in=_ACTIVE_STATUSES,
        )
        self.assertEqual(active_qs.count(), 1, "exactly one active request must survive")
        self.assertEqual(FundedPayoutRequest.objects.filter(enrollment=enrollment).count(), 1)

        # No double economic effect from the second (rejected) request.
        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before, "request creation must not touch the balance")
        self.assertEqual(BrokerLedger.objects.filter(source_account=account).count(), 0)
        self.assertEqual(WithdrawalRequest.objects.filter(user=user).count(), 0)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FUNDED_PAYOUT).count(), 0,
        )

        # The single survivor's full downstream approval still fires
        # exactly once — same end-to-end proof as the real-race test,
        # here demonstrated deterministically.
        survivor = active_qs.get()
        admin = User.objects.create_user(username="fpr_seq_admin", password="x", is_staff=True)
        approve_sim_payout(survivor, admin)

        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FUNDED_PAYOUT).count(), 1,
        )
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet__user=user, tx_type=WalletTransaction.TX_FUNDED_PAYOUT,
            ).count(), 1,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(
                revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE, source_funded_payout=survivor,
            ).count(), 1,
        )
        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before - survivor.trader_cut)


@override_settings(LOAD_TEST_MODE=True)
class FPRRealConcurrencyTests(TransactionTestCase):
    """Test B — real threads, real DB connections, real HTTP POSTs to
    funded_payout_request_view — the actual race the constraint defends
    against, followed by a full approval of the survivor to prove no
    downstream monetary effect ever double-fires. PostgreSQL-only (see
    _skip_unless_postgres) — SQLite's own django_session write contention
    makes this specific real-thread shape unreliable independent of the
    business logic under test; the same invariant is proven deterministically,
    on every engine, by FPRSequentialDuplicateGuardTests above."""

    def _make_ready_user(self):
        return _make_ready_fpr_user()

    @_skip_unless_postgres
    def test_two_concurrent_requests_same_enrollment_exactly_one_survives(self):
        from django.test import Client

        user, enrollment, account, fc = self._make_ready_user()
        url = reverse(_URL)
        balance_before = Decimal(str(account.balance))

        barrier = threading.Barrier(2)
        results = [None, None]

        def _attempt():
            client = Client()
            client.login(username=user.username, password="testpass")
            with patch("simulator.two_factor.verify_totp", return_value=True):
                resp = client.post(url, {"enrollment_id": enrollment.pk, "otp_code": "000000"})
            return resp.status_code

        threads = [
            threading.Thread(target=_run_concurrent, args=(_attempt, barrier, results, i))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        # No silent vacuous pass: both threads must have recorded a real outcome.
        for r in results:
            self.assertIsNotNone(r, "a thread never completed — see _run_concurrent docstring")
            self.assertNotIn("unexpected_", r[0], f"unhandled exception in a race thread: {r}")
            # BBOOK-CLOSE-02 SQLITE STABILIZATION — this test only runs
            # under real PostgreSQL (see _skip_unless_postgres above), where
            # real row-level locking blocks-then-proceeds rather than
            # raising "database is locked" the way SQLite does. An
            # "operational_error" outcome here would mean the retry path
            # _run_concurrent only needs for SQLite's own contention was
            # exercised on PostgreSQL too — explicit, named, never silent.
            self.assertNotEqual(
                r[0], "operational_error",
                f"real PostgreSQL should never hit SQLite's lock-retry path: {r}",
            )

        # Exactly one 201 (created) and one controlled 409 (loser) — never a 500.
        active_qs = FundedPayoutRequest.objects.filter(
            enrollment=enrollment, status__in=_ACTIVE_STATUSES,
        )
        self.assertEqual(active_qs.count(), 1, f"expected exactly 1 active FPR, results={results}")
        self.assertEqual(FundedPayoutRequest.objects.filter(enrollment=enrollment).count(), 1)

        # No 500s: every recorded HTTP result (for the "ok" outcomes) must be
        # 201 or 409, and no thread died with an uncaught server error.
        http_codes = [r[1] for r in results if r[0] == "ok"]
        for code in http_codes:
            self.assertIn(code, (201, 409), f"unexpected status code {code}, results={results}")

        # Creation itself moves no money — confirmed unchanged.
        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before, "FPR creation must not touch the balance")
        self.assertEqual(BrokerLedger.objects.filter(source_account=account).count(), 0)
        self.assertEqual(WithdrawalRequest.objects.filter(user=user).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FUNDED_PAYOUT).count(), 0)

        # Now actually approve the survivor and prove the full monetary
        # cycle fires exactly once — end to end, not just at creation.
        survivor = active_qs.get()
        admin = User.objects.create_user(username="fpr_race_admin", password="x", is_staff=True)
        approve_sim_payout(survivor, admin)

        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FUNDED_PAYOUT).count(), 1,
        )
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet__user=user, tx_type=WalletTransaction.TX_FUNDED_PAYOUT,
            ).count(), 1,
        )
        self.assertEqual(
            BrokerLedger.objects.filter(
                revenue_type=BrokerLedger.REV_FUNDED_PROFIT_SHARE, source_funded_payout=survivor,
            ).count(), 1,
        )
        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before - survivor.trader_cut)


class FPRPostgresVendorEvidenceTests(TestCase):
    """Cheap, always-on sanity check — fails loudly (not silently) if a
    PostgreSQL-targeted run ever accidentally falls back to SQLite."""

    def test_vendor_matches_configured_engine(self):
        engine = connection.settings_dict["ENGINE"]
        if "postgresql" in engine:
            self.assertEqual(connection.vendor, "postgresql")
        else:
            self.assertEqual(connection.vendor, "sqlite")

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

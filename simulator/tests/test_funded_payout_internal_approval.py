"""
simulator/tests/test_funded_payout_internal_approval.py — Bloque H.3

Audits approve_internal_payout() and handle_internal_payout_webhook()
in simulator/funded_payouts.py (FUNDED_INTERNAL flow).

No HTTP — all functions called directly. NowPayments is always mocked.

Coverage:
  - Phase 1 DB writes (debit, ledger, WR, FPR links)
  - Phase 2 NP success (WR → processing, FPR → processing)
  - Phase 2 NP failure compensating transaction (reversal, EV_ADJUST)
  - Webhook COMPLETED (cycle reset, idempotency)
  - Webhook FAILED (reversal, idempotency)
  - Guard: plain WR has no linked FPR
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from simulator.challenge_engine import (
    activate_challenge_enrollment,
    advance_to_funded,
    advance_to_phase2,
)
from simulator.funded_payouts import (
    FundedPayoutAlreadyProcessed,
    InsufficientFundedBalance,
    approve_internal_payout,
    handle_internal_payout_webhook,
)
from simulator.models import (
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    FundedPayoutRequest,
    LedgerEntry,
    TradingAccount,
    WithdrawalRequest,
    Wallet,
)
from simulator.wallet_ledger import get_or_create_wallet

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# NP mock constants
# ─────────────────────────────────────────────────────────────────────────────

# FIX-FUNDED-INTERNAL-PAYOUT-AMBIGUOUS-FAILURE-01 — approve_internal_payout()
# now calls _np._get_jwt_token() and _np.create_payout_with_token() as two
# separately-classifiable steps instead of the single _np.create_payout()
# wrapper, so failures can be attributed to auth (pre-send-safe) vs. the
# POST itself (ambiguous) — mirrors payout_providers.py's retail pattern.
_NP_ESTIMATE = "simulator.funded_payouts._np.estimate_price"
_NP_JWT      = "simulator.funded_payouts._np._get_jwt_token"
_NP_POST     = "simulator.funded_payouts._np.create_payout_with_token"

_NP_ESTIMATE_RET = Decimal("0.000125")
_NP_JWT_RET      = "fake-jwt-token"
_NP_PAYOUT_RET   = {
    "id": "batch-h3test",
    "status": "CREATED",
    "withdrawals": [{"id": "wd-h3test", "status": "CREATED"}],
}

_seq = 0

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_user(role="trader"):
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h3_{role}_{_seq}",
        email=f"h3_{role}_{_seq}@example.com",
        password="testpass",
    )


def _make_admin():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f"h3_admin_{_seq}",
        email=f"h3_admin_{_seq}@example.com",
        password="adminpass",
        is_staff=True,
    )


def _make_product():
    global _seq
    return ChallengeProduct.objects.create(
        name=f"H3-Test-{_seq}",
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


def _make_internal_pending_fpr(
    user,
    enrollment,
    funded_account,
    funded_config,
    *,
    profit_usd=Decimal("1000.00"),
):
    """Create FPR in ST_PENDING with funded_type=FUNDED_INTERNAL and crypto fields."""
    initial = Decimal(str(funded_account.initial_balance or funded_account.balance))
    funded_account.balance = initial + profit_usd
    funded_account.equity  = funded_account.balance
    funded_account.save(update_fields=["balance", "equity"])

    cycle_profit = profit_usd
    trader_cut   = (cycle_profit * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut   = cycle_profit - trader_cut

    return FundedPayoutRequest.objects.create(
        user=user,
        enrollment=enrollment,
        funded_account=funded_account,
        funded_config=funded_config,
        funded_type=FundedConfig.FUNDED_INTERNAL,
        cycle_profit=cycle_profit,
        trader_cut=trader_cut,
        broker_cut=broker_cut,
        profit_split_pct=Decimal("80.00"),
        balance_snapshot=funded_account.balance,
        initial_balance_snapshot=initial,
        crypto_currency="btc",
        wallet_address="bc1qtestaddressforh3",
        status=FundedPayoutRequest.ST_PENDING,
    )


def _setup_approved_state(
    user,
    enrollment,
    funded_account,
    funded_config,
    *,
    profit_usd=Decimal("1000.00"),
):
    """
    Simulate Phase 1 result of approve_internal_payout without calling NP.
    Returns (fpr, wr) with:
      - funded_account.balance already debited
      - funded_account.initial_balance unchanged (not yet reset)
      - FPR.status = ST_APPROVED
      - WR.status = STATUS_APPROVED, WR.debit_tx = None
      - LedgerEntry(EV_FUNDED_PAYOUT) linked on FPR
    """
    initial       = Decimal(str(funded_account.initial_balance or funded_account.balance))
    pre_debit     = initial + profit_usd
    cycle_profit  = profit_usd
    trader_cut    = (cycle_profit * Decimal("80") / Decimal("100")).quantize(Decimal("0.01"))
    broker_cut    = cycle_profit - trader_cut
    post_debit    = pre_debit - trader_cut

    # Set funded account to post-debit state (initial_balance stays at initial)
    funded_account.balance = post_debit
    funded_account.equity  = post_debit
    funded_account.save(update_fields=["balance", "equity"])

    ledger = LedgerEntry.objects.create(
        account=funded_account,
        event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        amount=-trader_cut,
        balance_after=post_debit,
    )

    wr = WithdrawalRequest.objects.create(
        user=user,
        amount_usd=trader_cut,
        crypto_currency="btc",
        wallet_address="bc1qtestaddressforh3",
        status=WithdrawalRequest.STATUS_APPROVED,
        debit_tx=None,
    )

    fpr = FundedPayoutRequest.objects.create(
        user=user,
        enrollment=enrollment,
        funded_account=funded_account,
        funded_config=funded_config,
        funded_type=FundedConfig.FUNDED_INTERNAL,
        cycle_profit=cycle_profit,
        trader_cut=trader_cut,
        broker_cut=broker_cut,
        profit_split_pct=Decimal("80.00"),
        balance_snapshot=pre_debit,
        initial_balance_snapshot=initial,
        crypto_currency="btc",
        wallet_address="bc1qtestaddressforh3",
        status=FundedPayoutRequest.ST_APPROVED,
        withdrawal_request=wr,
        ledger_entry=ledger,
    )

    return fpr, wr


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 DB operations (NP mocked to succeed for full-flow tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveInternalPayoutDB(TestCase):
    """Phase 1 DB writes — debit, ledger, WR, FPR links, guard errors."""

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    # ── Funded account debited, wallet unchanged ──────────────────────────────

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_debits_funded_account_not_wallet(self, _est, _jwt, _post):
        wallet, _ = get_or_create_wallet(self.user)
        wallet_balance_before = Decimal(str(wallet.available_balance))
        account_balance_before = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))

        approve_internal_payout(self.fpr, self.admin)

        self.funded_account.refresh_from_db()
        self.assertEqual(
            self.funded_account.balance,
            account_balance_before - trader_cut,
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, wallet_balance_before)

    # AUDIT-02 — full approval (Phase 1 + Phase 2 NP success) produces
    # BrokerAuditEvents linked to this FPR. Full shape/correlation_id/
    # fail-open coverage lives in test_audit02_payments_trail.py; this is
    # the "no regression on the happy path" check alongside the pre-existing
    # assertions in this class.
    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_approval_creates_broker_audit_events(self, _est, _jwt, _post):
        from simulator.models import BrokerAuditEvent
        approve_internal_payout(self.fpr, self.admin)
        self.assertTrue(
            BrokerAuditEvent.objects.filter(funded_payout_request=self.fpr).exists()
        )

    # ── LedgerEntry EV_FUNDED_PAYOUT created ─────────────────────────────────

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_creates_ev_funded_payout_ledger(self, _est, _jwt, _post):
        account_balance_before = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))

        approve_internal_payout(self.fpr, self.admin)

        ledger = LedgerEntry.objects.get(
            account=self.funded_account,
            event_type=LedgerEntry.EV_FUNDED_PAYOUT,
        )
        self.assertEqual(ledger.amount,        -trader_cut)
        self.assertEqual(ledger.balance_after,  account_balance_before - trader_cut)

    # ── WithdrawalRequest created and linked ──────────────────────────────────

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_creates_withdrawal_request_linked(self, _est, _jwt, _post):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertIsNotNone(self.fpr.withdrawal_request_id)

    # ── WR debit_tx is None (no wallet debit) ────────────────────────────────

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_wr_debit_tx_is_none(self, _est, _jwt, _post):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.withdrawal_request.debit_tx)

    # ── No cycle reset on approval ────────────────────────────────────────────

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_no_cycle_reset_on_approval(self, _est, _jwt, _post):
        original_initial = Decimal(str(self.funded_account.initial_balance))

        approve_internal_payout(self.fpr, self.admin)

        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.cycle_reset_at)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, original_initial)

    # ── reviewed_by / reviewed_at set on FPR ─────────────────────────────────

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_sets_review_fields(self, _est, _jwt, _post):
        before = now()
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.reviewed_by_id, self.admin.pk)
        self.assertIsNotNone(self.fpr.reviewed_at)
        self.assertGreaterEqual(self.fpr.reviewed_at, before)

    # ── Guard: non-pending FPR raises FundedPayoutAlreadyProcessed ───────────

    def test_rejects_non_pending(self):
        FundedPayoutRequest.objects.filter(pk=self.fpr.pk).update(
            status=FundedPayoutRequest.ST_APPROVED
        )
        self.fpr.refresh_from_db()
        with self.assertRaises(FundedPayoutAlreadyProcessed):
            approve_internal_payout(self.fpr, self.admin)

    # ── Guard: insufficient balance raises InsufficientFundedBalance ──────────

    def test_revalidates_balance(self):
        self.funded_account.balance = Decimal("0.01")
        self.funded_account.equity  = Decimal("0.01")
        self.funded_account.save(update_fields=["balance", "equity"])
        with self.assertRaises(InsufficientFundedBalance):
            approve_internal_payout(self.fpr, self.admin)

    # ── Guard: missing crypto fields raises ValueError ────────────────────────

    def test_requires_crypto_fields(self):
        FundedPayoutRequest.objects.filter(pk=self.fpr.pk).update(
            crypto_currency="", wallet_address=""
        )
        self.fpr.refresh_from_db()
        with self.assertRaises(ValueError):
            approve_internal_payout(self.fpr, self.admin)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: NP success — WR and FPR move to processing
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveInternalPayoutNPSuccess(TestCase):

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_success_moves_wr_to_processing(self, _est, _jwt, _post):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        wr = self.fpr.withdrawal_request
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(wr.np_batch_id,  "batch-h3test")
        self.assertEqual(wr.np_payout_id, "wd-h3test")

    @patch(_NP_POST, return_value=_NP_PAYOUT_RET)
    @patch(_NP_JWT,  return_value=_NP_JWT_RET)
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_np_success_moves_fpr_to_processing(self, _est, _jwt, _post):
        approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_PROCESSING)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-FUNDED-INTERNAL-PAYOUT-AMBIGUOUS-FAILURE-01
#
# Phase 2 pre-send-safe failures (estimate_price / auth before the POST) —
# compensating reversal is UNCHANGED. Ambiguous post-send failures (POST
# itself, unparseable body, or a post-success local write) NEVER reverse —
# this is the entire point of the block. The old TestApproveInternalPayoutNPFailure
# class mocked _np.create_payout (the combined auth+POST wrapper) with a
# bare RuntimeError, which is exactly the now-ambiguous case — its 5 tests
# asserted the OLD (bug) behavior (reverse + FAILED) and are replaced below
# by TestApproveInternalPayoutAmbiguousPostFailure asserting the NEW,
# correct behavior for that same failure shape.
# ─────────────────────────────────────────────────────────────────────────────

class TestApproveInternalPayoutPreSendSafeFailure(TestCase):
    """Item A: estimate_price() fails. Item B: auth (_get_jwt_token) fails
    before the /v1/payout POST. Both provably pre-send-safe — reverse +
    FAILED, byte-identical to the pre-existing behavior."""

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def _assert_reversed_and_failed(self, balance_before):
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before)
        self.assertEqual(self.funded_account.equity,  balance_before)
        self.assertTrue(
            LedgerEntry.objects.filter(
                account=self.funded_account, event_type=LedgerEntry.EV_ADJUST,
            ).exists()
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_FAILED)
        self.assertEqual(self.fpr.withdrawal_request.status, WithdrawalRequest.STATUS_FAILED)
        self.assertIsNone(self.fpr.cycle_reset_at)

    # ── A: estimate_price() fails ────────────────────────────────────────

    @patch(_NP_ESTIMATE, side_effect=RuntimeError("estimate down"))
    def test_estimate_failure_reverses_and_fails(self, _est):
        balance_before = Decimal(str(self.funded_account.balance))
        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)
        self._assert_reversed_and_failed(balance_before)

    @patch(_NP_ESTIMATE, side_effect=RuntimeError("estimate down"))
    def test_estimate_failure_never_calls_jwt_or_post(self, _est):
        with patch(_NP_JWT) as jwt_mock, patch(_NP_POST) as post_mock:
            with self.assertRaises(RuntimeError):
                approve_internal_payout(self.fpr, self.admin)
            jwt_mock.assert_not_called()
            post_mock.assert_not_called()

    # ── B: auth (_get_jwt_token) fails before the POST ───────────────────

    @patch(_NP_JWT,      side_effect=RuntimeError("auth down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_auth_failure_reverses_and_fails(self, _est, _jwt):
        balance_before = Decimal(str(self.funded_account.balance))
        with self.assertRaises(RuntimeError):
            approve_internal_payout(self.fpr, self.admin)
        self._assert_reversed_and_failed(balance_before)

    @patch(_NP_JWT,      side_effect=RuntimeError("auth down"))
    @patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET)
    def test_auth_failure_never_calls_post(self, _est, _jwt):
        with patch(_NP_POST) as post_mock:
            with self.assertRaises(RuntimeError):
                approve_internal_payout(self.fpr, self.admin)
            post_mock.assert_not_called()


class TestApproveInternalPayoutAmbiguousPostFailure(TestCase):
    """Items C/D/E: timeout, connection error, 5xx (and a generic/
    unclassified exception, the same shape the old buggy behavior was
    tested with) on the /v1/payout POST itself, with a valid auth token
    already obtained — NowPayments may have already accepted the payout.
    NEVER reversed, NEVER marked FAILED."""

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def _assert_left_ambiguous(self, expected_balance):
        """expected_balance is the balance AFTER Phase 1's debit (trader_cut
        already deducted) — the ambiguous failure must leave it exactly
        there, never restoring the pre-approval balance."""
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, expected_balance,
                          "ambiguous failure must NEVER reverse the funded-account debit")
        self.assertEqual(self.funded_account.equity, expected_balance)
        self.assertFalse(
            LedgerEntry.objects.filter(
                account=self.funded_account, event_type=LedgerEntry.EV_ADJUST,
            ).exists(),
            "no compensating EV_ADJUST for an ambiguous failure",
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_APPROVED)
        self.assertEqual(self.fpr.withdrawal_request.status, WithdrawalRequest.STATUS_APPROVED)
        self.assertIn("AMBIGUOUS_SUBMIT_FAILURE", self.fpr.admin_note)

    def _run(self, exc):
        # Captured BEFORE approve_internal_payout runs Phase 1 — the
        # in-memory objects are still fresh/unmutated at this point.
        balance_before_approval = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))
        with patch(_NP_POST, side_effect=exc), \
             patch(_NP_JWT, return_value=_NP_JWT_RET), \
             patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET):
            with self.assertRaises(type(exc)):
                approve_internal_payout(self.fpr, self.admin)
        self._assert_left_ambiguous(balance_before_approval - trader_cut)

    # ── C: timeout ─────────────────────────────────────────────────────

    def test_timeout_leaves_ambiguous(self):
        import requests
        self._run(requests.exceptions.Timeout("payout POST timed out"))

    # ── D: connection error ───────────────────────────────────────────

    def test_connection_error_leaves_ambiguous(self):
        import requests
        self._run(requests.exceptions.ConnectionError("connection reset"))

    # ── E: 5xx ─────────────────────────────────────────────────────────

    def test_5xx_leaves_ambiguous(self):
        import requests
        resp = requests.Response()
        resp.status_code = 502
        self._run(requests.exceptions.HTTPError("502 Server Error", response=resp))

    # ── Generic/unclassified exception — same shape the pre-fix test
    #    suite exercised (RuntimeError), now correctly ambiguous ────────

    def test_generic_exception_leaves_ambiguous(self):
        self._run(RuntimeError("NP down"))

    def test_ambiguous_failure_fires_audit_event(self):
        from simulator.models import BrokerAuditEvent
        from simulator import broker_audit as _audit
        with patch(_NP_POST, side_effect=RuntimeError("NP down")), \
             patch(_NP_JWT, return_value=_NP_JWT_RET), \
             patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET):
            with self.assertRaises(RuntimeError):
                approve_internal_payout(self.fpr, self.admin)
        self.assertTrue(
            BrokerAuditEvent.objects.filter(
                funded_payout_request=self.fpr,
                event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_AMBIGUOUS,
            ).exists()
        )

    def test_unparseable_response_body_leaves_ambiguous(self):
        """Response received (POST itself succeeded) but .get()-style
        access on it fails — still ambiguous, the POST may have landed."""
        balance_before_approval = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))
        with patch(_NP_POST, return_value="not-a-dict"), \
             patch(_NP_JWT, return_value=_NP_JWT_RET), \
             patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET):
            with self.assertRaises(AttributeError):
                approve_internal_payout(self.fpr, self.admin)
        self._assert_left_ambiguous(balance_before_approval - trader_cut)


class TestApproveInternalPayoutPersistenceFailureAfterSuccess(TestCase):
    """Item F: the POST succeeds (real batch_id/payout_id obtained) but the
    local WithdrawalRequest/FundedPayoutRequest write itself fails. The
    provider DEFINITELY accepted this payout — must never reverse, and
    the ids already known must be preserved for manual reconciliation."""

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def test_post_success_then_db_write_failure_never_reverses_and_preserves_ids(self):
        balance_before_approval = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))
        with patch(_NP_POST, return_value=_NP_PAYOUT_RET), \
             patch(_NP_JWT, return_value=_NP_JWT_RET), \
             patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET), \
             patch(
                 "simulator.funded_payouts.WithdrawalRequest.objects.filter",
                 side_effect=RuntimeError("db write failed"),
             ):
            with self.assertRaises(RuntimeError):
                approve_internal_payout(self.fpr, self.admin)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before_approval - trader_cut,
                          "a confirmed-accepted payout must never be reversed")
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_APPROVED)
        self.assertIn("AMBIGUOUS_SUBMIT_FAILURE", self.fpr.admin_note)
        self.assertIn("batch-h3test", self.fpr.admin_note)
        self.assertIn("wd-h3test", self.fpr.admin_note)


class TestManualReconciliationOfAmbiguousPayout(TestCase):
    """Items G/H: once an ambiguous payout is manually confirmed (owner
    checks the NowPayments dashboard directly, same procedure already
    used for WithdrawalRequest #4/#9), resolving it reuses
    handle_internal_payout_webhook() UNCHANGED — no new reconciliation
    code was written, and it must apply exactly once."""

    def setUp(self):
        self.admin          = _make_admin()
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr = _make_internal_pending_fpr(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )
        with patch(_NP_POST, side_effect=RuntimeError("NP down")), \
             patch(_NP_JWT, return_value=_NP_JWT_RET), \
             patch(_NP_ESTIMATE, return_value=_NP_ESTIMATE_RET):
            with self.assertRaises(RuntimeError):
                approve_internal_payout(self.fpr, self.admin)
        self.fpr.refresh_from_db()
        self.funded_account.refresh_from_db()
        self.wr = self.fpr.withdrawal_request
        assert self.fpr.status == FundedPayoutRequest.ST_APPROVED
        assert self.wr.status == WithdrawalRequest.STATUS_APPROVED

    def test_manual_resolve_to_completed_exactly_once(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "manual-confirm-1",
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)
        self.assertIsNotNone(self.fpr.cycle_reset_at)
        first_reset = self.fpr.cycle_reset_at

        # Idempotent — a second call (duplicate webhook/manual replay) is a no-op.
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "manual-confirm-1",
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.cycle_reset_at, first_reset)

    def test_manual_resolve_to_failed_refunds_exactly_once(self):
        balance_before = Decimal(str(self.funded_account.balance))
        trader_cut = Decimal(str(self.fpr.trader_cut))

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "",
        )
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before + trader_cut)
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_FAILED)

        # Idempotent — a second call must NOT refund a second time.
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "",
        )
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before + trader_cut)
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=self.funded_account, event_type=LedgerEntry.EV_ADJUST,
            ).count(),
            1,
            "duplicate manual/webhook resolution must not double-refund",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Webhook COMPLETED
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookCompleted(TestCase):
    """handle_internal_payout_webhook with STATUS_COMPLETED."""

    def setUp(self):
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr, self.wr   = _setup_approved_state(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def test_webhook_completed_resets_cycle(self):
        post_debit_balance = Decimal(str(self.funded_account.balance))
        before             = now()

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, post_debit_balance)

        self.fpr.refresh_from_db()
        self.assertIsNotNone(self.fpr.cycle_reset_at)
        self.assertGreaterEqual(self.fpr.cycle_reset_at, before)

    def test_webhook_completed_sets_fpr_completed(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_COMPLETED)

    def test_webhook_completed_idempotent(self):
        """Second COMPLETED webhook is a no-op — no double reset."""
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )
        self.fpr.refresh_from_db()
        first_cycle_reset_at  = self.fpr.cycle_reset_at
        post_debit_initial    = self.funded_account.balance  # captured before second call

        # Second call — must be idempotent
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_COMPLETED, "wd-h3test"
        )

        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status,        FundedPayoutRequest.ST_COMPLETED)
        self.assertEqual(self.fpr.cycle_reset_at, first_cycle_reset_at)

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, post_debit_initial)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook FAILED
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookFailed(TestCase):
    """handle_internal_payout_webhook with STATUS_FAILED."""

    def setUp(self):
        self.user           = _make_user()
        self.enrollment     = _make_funded_enrollment(self.user)
        self.funded_account = self.enrollment.funded_account
        self.funded_config  = FundedConfig.objects.get(enrollment=self.enrollment)
        self.fpr, self.wr   = _setup_approved_state(
            self.user, self.enrollment, self.funded_account, self.funded_config
        )

    def test_webhook_failed_reverses_funded_account(self):
        trader_cut     = Decimal(str(self.fpr.trader_cut))
        balance_before = Decimal(str(self.funded_account.balance))  # post-debit state

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )

        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_before + trader_cut)
        self.assertEqual(self.funded_account.equity,  balance_before + trader_cut)

    def test_webhook_failed_creates_ev_adjust(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.assertTrue(
            LedgerEntry.objects.filter(
                account=self.funded_account,
                event_type=LedgerEntry.EV_ADJUST,
            ).exists()
        )

    def test_webhook_failed_no_cycle_reset(self):
        original_initial = Decimal(str(self.funded_account.initial_balance))

        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )

        self.fpr.refresh_from_db()
        self.assertIsNone(self.fpr.cycle_reset_at)
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.initial_balance, original_initial)

    def test_webhook_failed_marks_fpr_failed(self):
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.fpr.refresh_from_db()
        self.assertEqual(self.fpr.status, FundedPayoutRequest.ST_FAILED)

    def test_webhook_failed_idempotent(self):
        """Second FAILED webhook must not double-reverse the funded account."""
        trader_cut     = Decimal(str(self.fpr.trader_cut))
        balance_before = Decimal(str(self.funded_account.balance))  # post-debit

        # First call — reverses debit
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.funded_account.refresh_from_db()
        balance_after_first = self.funded_account.balance
        self.assertEqual(balance_after_first, balance_before + trader_cut)

        # Second call — must be a no-op (FPR is already ST_FAILED)
        handle_internal_payout_webhook(
            self.fpr, self.wr, WithdrawalRequest.STATUS_FAILED, "wd-h3test"
        )
        self.funded_account.refresh_from_db()
        self.assertEqual(self.funded_account.balance, balance_after_first)


# ─────────────────────────────────────────────────────────────────────────────
# Regular WithdrawalRequest not affected by funded logic
# ─────────────────────────────────────────────────────────────────────────────

class TestRegularWRNotAffected(TestCase):
    """Plain WR (no linked FPR) has no funded_payout_internal relation."""

    def setUp(self):
        self.user = _make_user()

    def test_regular_wr_has_no_funded_payout_internal(self):
        wallet, _ = get_or_create_wallet(self.user)
        wr = WithdrawalRequest.objects.create(
            user=self.user,
            amount_usd=Decimal("50.00"),
            crypto_currency="btc",
            wallet_address="bc1qplainaddress",
            status=WithdrawalRequest.STATUS_PENDING,
        )
        with self.assertRaises(FundedPayoutRequest.DoesNotExist):
            _ = wr.funded_payout_internal

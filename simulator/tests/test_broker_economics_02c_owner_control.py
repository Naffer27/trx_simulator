# simulator/tests/test_broker_economics_02c_owner_control.py
"""
BROKER-ECONOMICS-02C — Owner Control Plane views.

The FIRST Owner-only user-facing surface in this application:
  - owner_broker_adjustment_view          (preview, GET/POST, read-only)
  - owner_broker_adjustment_confirm_view  (confirm, POST-only, TOTP-gated)

Both drive the REAL Django test Client against the REAL URLs — no
internal helper is called directly, so a bypass would have to survive
the actual request/response cycle to go undetected (same discipline
BROKER-ECONOMICS-02's own rate-limit test suite uses).

Covers every scenario BROKER-ECONOMICS-02C FASE A's design required:
non-Owner denied, TOTP wrong/missing denied, preview creates zero
writes, a valid confirm creates exactly one economic effect, a
double-submit with the SAME idempotency_key produces exactly one effect
and returns the existing result, a DIFFERENT key produces two
independent operations, reversal amount is always server-derived (the
view never reads a client amount for reversal mode), blank reason /
invalid amount / nonexistent references are all rejected with zero
writes, rate limiting engages, CSRF is enforced, the new view code is
structurally isolated from Wallet/Treasury/IB, and exactly one
AuditLog/BrokerAuditEvent pair is written per real operation.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from simulator.audit import EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT
from simulator.models import (
    AuditLog, BrokerAuditEvent, BrokerEconomicAdjustment, BrokerLedger,
    IBCommissionObligation, OwnerRoot, TreasuryOperationRequest, WalletTransaction,
)
from simulator.ratelimit import _RL_PREFIX, _get_rl_redis
from simulator.tests.factories import make_account, make_broker_ledger, make_user
from simulator.wallet_ledger import get_or_create_wallet

_PATCH_TOTP = patch("simulator.owner_actions.verify_totp", return_value=True)
_PATCH_TOTP_FAIL = patch("simulator.owner_actions.verify_totp", return_value=False)


class _RedisRateLimitCleanupMixin:
    """The real, external Redis-backed rate limiter is NOT reset by
    Django's per-test transaction rollback (only the DB is) — and
    SQLite's autoincrement PK is effectively reused across test methods
    once each test's transaction rolls back, so distinct tests can
    collide on the SAME `u{user_id}` rate-limit key. Same cleanup
    pattern as test_o4d2_totp_verify_rate_limiting.py's own
    _RedisKeyCleanupMixin — scoped to this file's two endpoint keys."""

    def tearDown(self):
        r = _get_rl_redis()
        keys = r.keys(f"{_RL_PREFIX}owner_broker_adjustment_*")
        if keys:
            r.delete(*keys)
        super().tearDown()


def _make_owner():
    owner_user = make_user(is_superuser=True, is_staff=True)
    OwnerRoot.objects.create(user=owner_user, established_by="test")
    return owner_user


def _key():
    return uuid.uuid4().hex


class PreviewViewTests(_RedisRateLimitCleanupMixin, TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.preview_url = reverse("simulator:owner_broker_adjustment")

    def test_non_owner_gets_403(self):
        plain = make_user()
        self.client.force_login(plain)
        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_anonymous_redirected_not_allowed_through(self):
        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
        })
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_get_renders_blank_form_zero_writes(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)
        self.assertEqual(BrokerLedger.objects.count(), 0)

    def test_valid_preview_creates_zero_writes_of_any_kind(self):
        """The core PREVIEW guarantee: no BrokerEconomicAdjustment, no
        BrokerLedger, no Wallet/Treasury/IB/TradingAccount write."""
        account = make_account()
        trader = account.user
        wallet, _ = get_or_create_wallet(trader)
        self.client.force_login(self.owner)

        ledger_before = BrokerLedger.objects.count()
        sidecar_before = BrokerEconomicAdjustment.objects.count()
        wallet_tx_before = WalletTransaction.objects.count()
        treasury_before = TreasuryOperationRequest.objects.count()
        ib_before = IBCommissionObligation.objects.count()
        balance_before, equity_before = account.balance, account.equity

        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "-20.00", "reason": "correction",
            "source_account_id": str(account.id),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(BrokerLedger.objects.count(), ledger_before)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), sidecar_before)
        self.assertEqual(WalletTransaction.objects.count(), wallet_tx_before)
        self.assertEqual(TreasuryOperationRequest.objects.count(), treasury_before)
        self.assertEqual(IBCommissionObligation.objects.count(), ib_before)
        account.refresh_from_db()
        self.assertEqual(account.balance, balance_before)
        self.assertEqual(account.equity, equity_before)
        self.assertIn("idempotency_key", response.context)
        self.assertTrue(response.context["idempotency_key"])

    def test_blank_reason_rejected_zero_writes(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "10.00", "reason": "   ",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_invalid_amount_rejected_zero_writes(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "not-a-number", "reason": "x",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_zero_amount_rejected_zero_writes(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "0.00", "reason": "x",
        })
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_nonexistent_source_ledger_rejected_zero_writes(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.preview_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
            "source_ledger_id": "999999",
        })
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_nonexistent_reverses_target_rejected_zero_writes(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.preview_url, {
            "mode": "reversal", "reason": "x", "reverses_id": "999999",
        })
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_reversal_preview_derives_amount_server_side(self):
        """Even at preview time, the shown amount is server-computed —
        the form never lets the Owner type a reversal amount."""
        self.client.force_login(self.owner)
        with _PATCH_TOTP:
            original = self._create_via_confirm(amount="-20.00")
        response = self.client.post(self.preview_url, {
            "mode": "reversal", "reason": "reverse it",
            "reverses_id": str(original.pk),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["amount"], Decimal("20.00"))

    def _create_via_confirm(self, amount):
        """Test-only helper: drives preview -> confirm for setup fixtures."""
        preview = self.client.post(reverse("simulator:owner_broker_adjustment"), {
            "mode": "new", "amount": amount, "reason": "setup",
        })
        key = preview.context["idempotency_key"]
        confirm = self.client.post(reverse("simulator:owner_broker_adjustment_confirm"), {
            "mode": "new", "amount": amount, "reason": "setup",
            "idempotency_key": key, "totp_code": "000000",
        })
        return confirm.context["adjustment"]


class ConfirmViewTests(_RedisRateLimitCleanupMixin, TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.preview_url = reverse("simulator:owner_broker_adjustment")
        self.confirm_url = reverse("simulator:owner_broker_adjustment_confirm")

    def _preview(self, **fields):
        base = {"mode": "new", "amount": "10.00", "reason": "x"}
        base.update(fields)
        return self.client.post(self.preview_url, base)

    def test_get_not_allowed(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.confirm_url)
        self.assertEqual(response.status_code, 405)

    def test_non_owner_gets_403(self):
        plain = make_user()
        self.client.force_login(plain)
        response = self.client.post(self.confirm_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
            "idempotency_key": _key(), "totp_code": "000000",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP_FAIL
    def test_wrong_totp_rejected_zero_writes(self, _mock):
        self.client.force_login(self.owner)
        preview = self._preview()
        key = preview.context["idempotency_key"]
        response = self.client.post(self.confirm_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
            "idempotency_key": key, "totp_code": "999999",
        })
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP_FAIL
    def test_owner_without_confirmed_totp_rejected_zero_writes(self, _mock):
        """verify_totp() returning False for 'no confirmed device' is
        indistinguishable from 'wrong code' at this layer — both
        exercised, per the required scenario list."""
        self.client.force_login(self.owner)
        preview = self._preview()
        key = preview.context["idempotency_key"]
        response = self.client.post(self.confirm_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
            "idempotency_key": key, "totp_code": "",
        })
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_missing_idempotency_key_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.confirm_url, {
            "mode": "new", "amount": "10.00", "reason": "x", "totp_code": "000000",
        })
        self.assertIn("errors", response.context)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    @_PATCH_TOTP
    def test_valid_confirm_creates_exactly_one_effect(self, _mock):
        self.client.force_login(self.owner)
        preview = self._preview(amount="-20.00")
        key = preview.context["idempotency_key"]
        response = self.client.post(self.confirm_url, {
            "mode": "new", "amount": "-20.00", "reason": "x",
            "idempotency_key": key, "totp_code": "000000",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)
        adjustment = response.context["adjustment"]
        self.assertEqual(adjustment.amount, Decimal("-20.00"))

    @_PATCH_TOTP
    def test_double_submit_same_key_exactly_one_effect_returns_existing(self, _mock):
        """THE key adversarial test: submit the identical confirm POST
        (same idempotency_key) twice — simulating a double-click or
        back-button resubmit — and prove exactly one economic effect,
        with the second response returning the SAME existing result."""
        self.client.force_login(self.owner)
        preview = self._preview(amount="-20.00")
        key = preview.context["idempotency_key"]
        payload = {
            "mode": "new", "amount": "-20.00", "reason": "x",
            "idempotency_key": key, "totp_code": "000000",
        }
        response1 = self.client.post(self.confirm_url, payload)
        response2 = self.client.post(self.confirm_url, payload)

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        adj1 = response1.context["adjustment"]
        adj2 = response2.context["adjustment"]
        self.assertEqual(adj1.pk, adj2.pk)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 1)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 1)
        self.assertEqual(
            AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), 1,
        )

    @_PATCH_TOTP
    def test_different_key_creates_independent_operations(self, _mock):
        """Two SEPARATE preview->confirm flows (two different keys)
        must produce two REAL, independent adjustments — proving the
        key, not the view, is what prevents duplication."""
        self.client.force_login(self.owner)

        preview1 = self._preview(amount="-20.00")
        key1 = preview1.context["idempotency_key"]
        self.client.post(self.confirm_url, {
            "mode": "new", "amount": "-20.00", "reason": "x",
            "idempotency_key": key1, "totp_code": "000000",
        })

        preview2 = self._preview(amount="-20.00")
        key2 = preview2.context["idempotency_key"]
        self.assertNotEqual(key1, key2)
        self.client.post(self.confirm_url, {
            "mode": "new", "amount": "-20.00", "reason": "x",
            "idempotency_key": key2, "totp_code": "000000",
        })

        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 2)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 2)

    @_PATCH_TOTP
    def test_reversal_flow_amount_always_server_derived(self, _mock):
        """Full preview->confirm reversal flow, AND prove the confirm
        endpoint ignores a malicious/incorrect client-supplied amount
        for reversal mode entirely."""
        self.client.force_login(self.owner)

        preview1 = self._preview(amount="-20.00")
        key1 = preview1.context["idempotency_key"]
        confirm1 = self.client.post(self.confirm_url, {
            "mode": "new", "amount": "-20.00", "reason": "x",
            "idempotency_key": key1, "totp_code": "000000",
        })
        original = confirm1.context["adjustment"]

        preview2 = self.client.post(self.preview_url, {
            "mode": "reversal", "reason": "reverse it", "reverses_id": str(original.pk),
        })
        key2 = preview2.context["idempotency_key"]
        self.assertEqual(preview2.context["amount"], Decimal("20.00"))

        # Adversarial: try to smuggle a wrong amount into the confirm
        # POST for a reversal. The view must never even read it.
        confirm2 = self.client.post(self.confirm_url, {
            "mode": "reversal", "amount": "999999.00",  # must be ignored
            "reason": "reverse it", "reverses_id": str(original.pk),
            "idempotency_key": key2, "totp_code": "000000",
        })
        reversal = confirm2.context["adjustment"]
        self.assertEqual(reversal.amount, Decimal("20.00"))
        self.assertEqual(reversal.reverses_id, original.pk)
        self.assertEqual(BrokerLedger.objects.filter(revenue_type=BrokerLedger.REV_ADJUSTMENT).count(), 2)

    @_PATCH_TOTP
    def test_exactly_one_audit_pair_per_real_confirm(self, _mock):
        self.client.force_login(self.owner)
        before_al = AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count()
        before_be = BrokerAuditEvent.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count()

        preview = self._preview(amount="10.00")
        key = preview.context["idempotency_key"]
        self.client.post(self.confirm_url, {
            "mode": "new", "amount": "10.00", "reason": "x",
            "idempotency_key": key, "totp_code": "000000",
        })

        self.assertEqual(
            AuditLog.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), before_al + 1,
        )
        self.assertEqual(
            BrokerAuditEvent.objects.filter(event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT).count(), before_be + 1,
        )


class RateLimitTests(_RedisRateLimitCleanupMixin, TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.confirm_url = reverse("simulator:owner_broker_adjustment_confirm")

    def test_confirm_endpoint_rate_limited_after_repeated_attempts(self):
        """The confirm endpoint is limited to 10 attempts / 300s / user.
        Drive it 11 times with intentionally-invalid data (no need for
        valid TOTP — the rate limiter runs before any service logic) and
        confirm the 11th response is blocked (429)."""
        self.client.force_login(self.owner)
        last_status = None
        for _ in range(11):
            response = self.client.post(reverse("simulator:owner_broker_adjustment_confirm"), {
                "mode": "new", "amount": "10.00", "reason": "x",
                "idempotency_key": _key(), "totp_code": "000000",
            })
            last_status = response.status_code
        self.assertEqual(last_status, 429)
        # No real adjustment should have been created via the blocked attempt.
        self.assertLessEqual(BrokerEconomicAdjustment.objects.count(), 10)


class CSRFTests(_RedisRateLimitCleanupMixin, TestCase):
    def setUp(self):
        self.owner = _make_owner()
        self.csrf_client = Client(enforce_csrf_checks=True)

    def test_confirm_without_csrf_token_rejected(self):
        self.csrf_client.force_login(self.owner)
        response = self.csrf_client.post(reverse("simulator:owner_broker_adjustment_confirm"), {
            "mode": "new", "amount": "10.00", "reason": "x",
            "idempotency_key": _key(), "totp_code": "000000",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)

    def test_preview_without_csrf_token_rejected(self):
        self.csrf_client.force_login(self.owner)
        response = self.csrf_client.post(reverse("simulator:owner_broker_adjustment"), {
            "mode": "new", "amount": "10.00", "reason": "x",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(BrokerEconomicAdjustment.objects.count(), 0)


class StructuralIsolationTests(TestCase):
    """AST-based, scoped ONLY to the two new 02C view functions — not
    the entire views.py module, which legitimately imports Wallet/
    Treasury/IB-adjacent names for hundreds of unrelated views."""

    def test_new_views_import_nothing_from_wallet_treasury_or_ib(self):
        import ast
        import inspect

        from simulator import views as views_module

        forbidden = {
            "wallet_ledger", "ib_treasury_settlement", "ib_commission_triggers",
            "ib_commission", "ib_admin_ops", "ib_risk_holds",
            "broker_economic_adjustment",  # views must go through owner_actions only
        }

        for fn in (views_module.owner_broker_adjustment_view, views_module.owner_broker_adjustment_confirm_view):
            source = inspect.getsource(fn)
            tree = ast.parse(source)
            imported_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported_names.add(alias.name)
                        imported_names.add(alias.name.rsplit(".", 1)[-1])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported_names.add(node.module)
                        imported_names.add(node.module.rsplit(".", 1)[-1])
                    for alias in node.names:
                        imported_names.add(alias.name)

            overlap = forbidden & imported_names
            self.assertEqual(
                overlap, set(),
                msg=f"{fn.__name__} must not import any of {sorted(forbidden)} — found: {sorted(overlap)}",
            )

    def test_new_views_never_write_brokerledger_or_adjustment_directly(self):
        import inspect

        from simulator import views as views_module

        forbidden_calls = (
            "BrokerLedger.objects.create", "BrokerLedger.objects.update",
            "BrokerEconomicAdjustment.objects.create", "BrokerEconomicAdjustment.objects.update",
        )
        for fn in (views_module.owner_broker_adjustment_view, views_module.owner_broker_adjustment_confirm_view):
            source = inspect.getsource(fn)
            for pattern in forbidden_calls:
                self.assertNotIn(
                    pattern, source,
                    msg=f"{fn.__name__} must never write BrokerLedger/BrokerEconomicAdjustment directly",
                )

    def test_confirm_view_only_calls_owner_actions_wrapper(self):
        import inspect

        from simulator import views as views_module

        source = inspect.getsource(views_module.owner_broker_adjustment_confirm_view)
        self.assertIn("owner_broker_economic_adjustment(", source)

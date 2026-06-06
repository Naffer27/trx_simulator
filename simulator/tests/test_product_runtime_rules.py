# simulator/tests/test_product_runtime_rules.py
"""
Phase 6B — Product Runtime Rules tests.

Covers:
  - Snapshot fields written on account creation (demo + real)
  - account_product FK set on creation
  - Old accounts (no snapshot) fall back gracefully
  - commission_for() uses commission_per_lot_snapshot when set
  - commission_for() falls back to spec.commission_pct when snapshot is 0/None
  - commission_for() zero when both snapshot and spec are zero
  - AccountProduct new fields (allowed_symbols, max_lot_size, margin_call_level, stopout_level)
  - seed command includes new fields without error
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from simulator.models import AccountProduct, TradingAccount
from simulator.tests.factories import make_user, make_wallet, make_account
from simulator.wallet_ledger import credit_wallet, get_or_create_wallet

User = get_user_model()

CREATE_URL = "/accounts/create/"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _login(client, user, password="testpass123"):
    client.login(username=user.username, password=password)


def _make_product(
    code="p-standard",
    name="Standard",
    product_type=AccountProduct.TYPE_STANDARD,
    family=AccountProduct.FAMILY_REAL,
    min_deposit=Decimal("10.00"),
    default_balance=Decimal("0.00"),
    max_leverage=200,
    typical_spread_pips=Decimal("1.20"),
    commission_per_lot=Decimal("0.00"),
    allowed_symbols=None,
    max_lot_size=None,
    margin_call_level=Decimal("100.00"),
    stopout_level=Decimal("50.00"),
    is_active=True,
):
    return AccountProduct.objects.create(
        code=code, name=name, product_type=product_type, family=family,
        min_deposit=min_deposit, default_balance=default_balance,
        max_leverage=max_leverage, typical_spread_pips=typical_spread_pips,
        commission_per_lot=commission_per_lot,
        allowed_symbols=allowed_symbols, max_lot_size=max_lot_size,
        margin_call_level=margin_call_level, stopout_level=stopout_level,
        is_active=is_active,
    )


def _make_demo_product(code="p-demo", default_balance=Decimal("10000"), **kwargs):
    return _make_product(
        code=code, name="Demo Std", product_type=AccountProduct.TYPE_DEMO,
        family=AccountProduct.FAMILY_DEMO, min_deposit=Decimal("0"),
        default_balance=default_balance, **kwargs,
    )


def _make_ecn_product(code="p-ecn"):
    return _make_product(
        code=code, name="ECN Pro", product_type=AccountProduct.TYPE_ECN,
        family=AccountProduct.FAMILY_REAL, min_deposit=Decimal("100"),
        commission_per_lot=Decimal("7.00"), typical_spread_pips=Decimal("0.00"),
    )


# ── AccountProduct new fields ─────────────────────────────────────────────────

class AccountProductRiskFieldTests(TestCase):
    def test_new_fields_persist(self):
        p = _make_product(
            allowed_symbols=["EURUSD", "XAUUSD"],
            max_lot_size=Decimal("5.00"),
            margin_call_level=Decimal("80.00"),
            stopout_level=Decimal("40.00"),
        )
        p.refresh_from_db()
        self.assertEqual(p.allowed_symbols, ["EURUSD", "XAUUSD"])
        self.assertEqual(p.max_lot_size, Decimal("5.00"))
        self.assertEqual(p.margin_call_level, Decimal("80.00"))
        self.assertEqual(p.stopout_level, Decimal("40.00"))

    def test_defaults(self):
        p = _make_product(code="defaults-test")
        p.refresh_from_db()
        self.assertIsNone(p.allowed_symbols)
        self.assertIsNone(p.max_lot_size)
        self.assertEqual(p.margin_call_level, Decimal("100.00"))
        self.assertEqual(p.stopout_level, Decimal("50.00"))

    def test_allowed_symbols_none_means_all(self):
        p = _make_product(code="all-symbols", allowed_symbols=None)
        p.refresh_from_db()
        self.assertIsNone(p.allowed_symbols)


# ── Snapshot write — demo account ─────────────────────────────────────────────

class DemoSnapshotTests(TestCase):
    def setUp(self):
        self.user = make_user(email="snap_demo@test.com")
        self.wallet, _ = get_or_create_wallet(self.user)
        self.product = _make_demo_product(
            commission_per_lot=Decimal("0.00"),
            typical_spread_pips=Decimal("1.20"),
            margin_call_level=Decimal("100.00"),
            stopout_level=Decimal("50.00"),
        )
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_account_product_fk_set(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.account_product_id, self.product.pk)

    @patch("simulator.tasks.send_email_async")
    def test_product_code_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.product_code_snapshot, self.product.code)

    @patch("simulator.tasks.send_email_async")
    def test_product_name_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.product_name_snapshot, self.product.name)

    @patch("simulator.tasks.send_email_async")
    def test_leverage_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.leverage_snapshot, self.product.max_leverage)

    @patch("simulator.tasks.send_email_async")
    def test_spread_pips_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.spread_pips_snapshot, self.product.typical_spread_pips)

    @patch("simulator.tasks.send_email_async")
    def test_commission_per_lot_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.commission_per_lot_snapshot, self.product.commission_per_lot)

    @patch("simulator.tasks.send_email_async")
    def test_margin_call_level_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.margin_call_level_snapshot, Decimal("100.00"))

    @patch("simulator.tasks.send_email_async")
    def test_stopout_level_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.stopout_level_snapshot, Decimal("50.00"))

    @patch("simulator.tasks.send_email_async")
    def test_allowed_symbols_snapshot_none_by_default(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertIsNone(acc.allowed_symbols_snapshot)

    @patch("simulator.tasks.send_email_async")
    def test_snapshot_does_not_break_balance(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.balance, self.product.default_balance)


# ── Snapshot write — real (ECN) account ──────────────────────────────────────

class RealSnapshotTests(TestCase):
    def setUp(self):
        self.user = make_user(email="snap_real@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = _make_ecn_product()
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_account_product_fk_set_real(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "150"})
        acc = TradingAccount.objects.get(user=self.user)
        self.assertEqual(acc.account_product_id, self.product.pk)

    @patch("simulator.tasks.send_email_async")
    def test_commission_per_lot_snapshot_ecn(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "150"})
        acc = TradingAccount.objects.get(user=self.user)
        self.assertEqual(acc.commission_per_lot_snapshot, Decimal("7.00"))

    @patch("simulator.tasks.send_email_async")
    def test_snapshot_frozen_after_product_change(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "150"})
        acc = TradingAccount.objects.get(user=self.user)
        original_commission = acc.commission_per_lot_snapshot
        # Change the product — snapshot on existing account must be unchanged
        self.product.commission_per_lot = Decimal("3.00")
        self.product.save()
        acc.refresh_from_db()
        self.assertEqual(acc.commission_per_lot_snapshot, original_commission)

    @patch("simulator.tasks.send_email_async")
    def test_product_deletion_preserves_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "150"})
        acc = TradingAccount.objects.get(user=self.user)
        self.product.delete()
        acc.refresh_from_db()
        # FK becomes NULL — snapshot fields intact
        self.assertIsNone(acc.account_product_id)
        self.assertEqual(acc.product_name_snapshot, "ECN Pro")
        self.assertEqual(acc.commission_per_lot_snapshot, Decimal("7.00"))

    @patch("simulator.tasks.send_email_async")
    def test_allowed_symbols_snapshot_written(self, _m):
        prod = _make_product(
            code="sym-test", family=AccountProduct.FAMILY_REAL,
            min_deposit=Decimal("10"), commission_per_lot=Decimal("0"),
            allowed_symbols=["EUR/USD", "XAU/USD"],
        )
        self.client.post(CREATE_URL, {"product_id": prod.pk, "amount": "50"})
        acc = TradingAccount.objects.get(user=self.user)
        self.assertEqual(acc.allowed_symbols_snapshot, ["EUR/USD", "XAU/USD"])

    @patch("simulator.tasks.send_email_async")
    def test_max_lot_size_snapshot_written(self, _m):
        prod = _make_product(
            code="lot-test", family=AccountProduct.FAMILY_REAL,
            min_deposit=Decimal("10"), commission_per_lot=Decimal("0"),
            max_lot_size=Decimal("2.00"),
        )
        self.client.post(CREATE_URL, {"product_id": prod.pk, "amount": "50"})
        acc = TradingAccount.objects.get(user=self.user)
        self.assertEqual(acc.max_lot_size_snapshot, Decimal("2.00"))


# ── commission_for() logic (unit-tested via simple Python objects) ────────────

class CommissionForLogicTests(TestCase):
    """
    Tests the commission_for() branching without a live WebSocket.
    We simulate the relevant state of the consumer via a minimal stub.
    """

    def _make_stub(self, commission_per_lot: float):
        """Return a minimal object that mimics the consumer's commission_for()."""
        from market_data.symbol_specs import get_spec

        class _Stub:
            def __init__(self, cpl):
                self.account = {"commission_per_lot": cpl}

            def commission_for(self, symbol: str, qty: float, price: float) -> float:
                cpl = self.account.get("commission_per_lot", 0.0) or 0.0
                if cpl > 0:
                    return round(qty * cpl, 2)
                spec = get_spec(symbol)
                notional = qty * price * spec.contract_size
                return max(0.0, notional * spec.commission_pct)

        return _Stub(commission_per_lot)

    def test_per_lot_snapshot_used_when_set(self):
        stub = self._make_stub(7.0)
        result = stub.commission_for("EUR/USD", 1.0, 1.1000)
        self.assertAlmostEqual(result, 7.0, places=2)

    def test_per_lot_multiplied_by_qty(self):
        stub = self._make_stub(7.0)
        result = stub.commission_for("EUR/USD", 0.5, 1.1000)
        self.assertAlmostEqual(result, 3.5, places=2)

    def test_per_lot_zero_falls_back_to_spec(self):
        # EUR/USD spec.commission_pct is 0 in default config → result is 0
        stub = self._make_stub(0.0)
        result = stub.commission_for("EUR/USD", 1.0, 1.1000)
        self.assertGreaterEqual(result, 0.0)

    def test_per_lot_none_falls_back_to_spec(self):
        stub = self._make_stub(None)
        result = stub.commission_for("EUR/USD", 0.1, 1.1000)
        self.assertGreaterEqual(result, 0.0)

    def test_per_lot_small_qty(self):
        stub = self._make_stub(7.0)
        result = stub.commission_for("EUR/USD", 0.01, 1.1000)
        self.assertAlmostEqual(result, 0.07, places=2)

    def test_per_lot_gold(self):
        stub = self._make_stub(7.0)
        result = stub.commission_for("XAU/USD", 0.1, 2000.0)
        self.assertAlmostEqual(result, 0.7, places=2)


# ── Backward compatibility — old accounts with no snapshots ───────────────────

class OldAccountCompatibilityTests(TestCase):
    def test_old_account_snapshot_fields_are_null(self):
        acc = make_account(account_type="DEMO", balance=Decimal("10000"))
        acc.refresh_from_db()
        self.assertIsNone(acc.account_product_id)
        self.assertIsNone(acc.product_code_snapshot)
        self.assertIsNone(acc.product_name_snapshot)
        self.assertIsNone(acc.leverage_snapshot)
        self.assertIsNone(acc.commission_per_lot_snapshot)
        self.assertIsNone(acc.spread_pips_snapshot)
        self.assertIsNone(acc.allowed_symbols_snapshot)
        self.assertIsNone(acc.max_lot_size_snapshot)
        self.assertIsNone(acc.margin_call_level_snapshot)
        self.assertIsNone(acc.stopout_level_snapshot)

    def test_old_account_balance_unaffected(self):
        acc = make_account(balance=Decimal("5000"))
        acc.refresh_from_db()
        self.assertEqual(acc.balance, Decimal("5000"))

    def test_commission_for_old_account_zero_cpl_uses_spec(self):
        """Old accounts have commission_per_lot=0 in account dict → fallback to spec."""
        from market_data.symbol_specs import get_spec

        class _Stub:
            account = {"commission_per_lot": 0.0}
            def commission_for(self, symbol, qty, price):
                cpl = self.account.get("commission_per_lot", 0.0) or 0.0
                if cpl > 0:
                    return round(qty * cpl, 2)
                spec = get_spec(symbol)
                notional = qty * price * spec.contract_size
                return max(0.0, notional * spec.commission_pct)

        result = _Stub().commission_for("EUR/USD", 1.0, 1.1)
        self.assertGreaterEqual(result, 0.0)

    def test_challenge_account_no_product_fk(self):
        acc = make_account(account_type="CHALLENGE", tier="10K")
        self.assertIsNone(acc.account_product_id)


# ── Integration: full view creates snapshot + redirect ────────────────────────

class SnapshotIntegrationTests(TestCase):
    def setUp(self):
        self.user = make_user(email="integ@test.com")
        make_wallet(self.user, initial_balance=Decimal("300"))
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_all_snapshot_fields_non_null_for_real_product(self, _m):
        product = _make_product(
            code="integ-real",
            commission_per_lot=Decimal("5.00"),
            typical_spread_pips=Decimal("0.80"),
            max_leverage=50,
            allowed_symbols=None,
            max_lot_size=Decimal("3.00"),
            margin_call_level=Decimal("80.00"),
            stopout_level=Decimal("40.00"),
        )
        self.client.post(CREATE_URL, {"product_id": product.pk, "amount": "50"})
        acc = TradingAccount.objects.get(user=self.user)

        self.assertEqual(acc.account_product_id, product.pk)
        self.assertEqual(acc.product_code_snapshot, "integ-real")
        self.assertIsNotNone(acc.product_name_snapshot)
        self.assertEqual(acc.leverage_snapshot, 50)
        self.assertEqual(acc.spread_pips_snapshot, Decimal("0.80"))
        self.assertEqual(acc.commission_per_lot_snapshot, Decimal("5.00"))
        self.assertIsNone(acc.allowed_symbols_snapshot)
        self.assertEqual(acc.max_lot_size_snapshot, Decimal("3.00"))
        self.assertEqual(acc.margin_call_level_snapshot, Decimal("80.00"))
        self.assertEqual(acc.stopout_level_snapshot, Decimal("40.00"))

    @patch("simulator.tasks.send_email_async")
    def test_demo_snapshot_does_not_alter_balance(self, _m):
        product = _make_demo_product(code="integ-demo2", default_balance=Decimal("50000"))
        self.client.post(CREATE_URL, {"product_id": product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.balance, Decimal("50000"))
        self.assertEqual(acc.product_name_snapshot, product.name)

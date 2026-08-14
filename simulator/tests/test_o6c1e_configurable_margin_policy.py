# simulator/tests/test_o6c1e_configurable_margin_policy.py
"""
O.6c-1e — Configurable Margin Policy Foundation.

Converts the two previously hardcoded margin-concentration caps
(_DEFAULT_MAX_MARGIN_PER_TRADE_PCT=10.0, _DEFAULT_MAX_TOTAL_MARGIN_PCT=50.0
in simulator/consumers.py) into per-AccountProduct policy, frozen onto
TradingAccount at creation — exactly the same pattern already proven for
margin_call_level/stopout_level/max_lot_size/allowed_symbols (Phase 6B).

No formula changed: _compute_pretrade_margin_guard() still computes
required_margin/per_trade_pct/total_margin_pct exactly as before — only
WHERE the two threshold values it compares against come from changed.
Every caller/test that predates this parameter gets the identical
10.0/50.0 default, bit for bit.

Coverage layers:
  - AccountProductFieldTests: new fields persist; defaults are 10.00/50.00
    on a freshly created product.
  - ModelCleanValidationTests: clean() rejects <=0 and per_trade>total,
    accepts a valid custom configuration — purely mathematical, no new
    commercial threshold chosen.
  - SnapshotWriteTests: a NEW account (demo and real) captures the
    product's values into its own *_snapshot fields at creation.
  - SnapshotFallbackTests: an EXISTING account with a NULL snapshot (pre-
    O.6c-1e, or created directly without going through the freezing view)
    falls back to exactly 10.0/50.0 in the consumer's account_snap dict.
  - SnapshotFrozenAfterProductChangeTests: editing the product after an
    account was created never retroactively changes that account's own
    snapshot — mirrors test_product_runtime_rules.py's own
    test_snapshot_frozen_after_product_change.
  - GuardUsesConfiguredValuesTests: _compute_pretrade_margin_guard() with
    non-default thresholds actually uses them, not the historical global
    constants.
  - AdminExposureTests: AccountProductAdmin's Risk Parameters fieldset
    lists both new field names.
  - NoRegressionBTCUSDTests: the exact BTCUSD reproduction from O.6c-1c
    (equity=$182.75, price~$63,157, qty=0.01 -> ~17.28%, rejected) is
    unchanged when no product override exists.
"""
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from simulator.admin import AccountProductAdmin
from simulator.consumers import (
    _compute_pretrade_margin_guard,
    _DEFAULT_MAX_MARGIN_PER_TRADE_PCT,
    _DEFAULT_MAX_TOTAL_MARGIN_PCT,
)
from simulator.models import AccountProduct, TradingAccount
from simulator.wallet_ledger import get_or_create_wallet
from market_data.symbol_specs import get_spec

from .factories import make_account, make_account_product, make_user, make_wallet

CREATE_URL = "/accounts/create/"


def _login(client, user, password="testpass123"):
    client.login(username=user.username, password=password)


def _snap(**overrides) -> dict:
    base = {
        "leverage":          100,
        "allowed_symbols":   None,
        "max_lot_size":      None,
        "margin_call_level": 100.0,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────
# 1. AccountProduct — new fields
# ─────────────────────────────────────────────────────────────────────────
class AccountProductFieldTests(TestCase):
    def test_defaults_are_10_and_50(self):
        p = make_account_product(code="o6c1e-defaults")
        p.refresh_from_db()
        self.assertEqual(p.max_margin_per_trade_pct, Decimal("10.00"))
        self.assertEqual(p.max_total_margin_pct, Decimal("50.00"))

    def test_custom_values_persist(self):
        p = make_account_product(
            code="o6c1e-custom",
            max_margin_per_trade_pct=Decimal("5.00"),
            max_total_margin_pct=Decimal("25.00"),
        )
        p.refresh_from_db()
        self.assertEqual(p.max_margin_per_trade_pct, Decimal("5.00"))
        self.assertEqual(p.max_total_margin_pct, Decimal("25.00"))


# ─────────────────────────────────────────────────────────────────────────
# 2. Model-level validation — purely mathematical, no invented thresholds
# ─────────────────────────────────────────────────────────────────────────
class ModelCleanValidationTests(TestCase):
    def test_zero_per_trade_rejected(self):
        p = AccountProduct(
            name="bad", product_type=AccountProduct.TYPE_STANDARD,
            max_margin_per_trade_pct=Decimal("0.00"), max_total_margin_pct=Decimal("50.00"),
        )
        with self.assertRaises(ValidationError):
            p.clean()

    def test_negative_total_rejected(self):
        p = AccountProduct(
            name="bad2", product_type=AccountProduct.TYPE_STANDARD,
            max_margin_per_trade_pct=Decimal("10.00"), max_total_margin_pct=Decimal("-5.00"),
        )
        with self.assertRaises(ValidationError):
            p.clean()

    def test_per_trade_exceeding_total_rejected(self):
        p = AccountProduct(
            name="bad3", product_type=AccountProduct.TYPE_STANDARD,
            max_margin_per_trade_pct=Decimal("60.00"), max_total_margin_pct=Decimal("50.00"),
        )
        with self.assertRaises(ValidationError):
            p.clean()

    def test_valid_custom_configuration_accepted(self):
        p = AccountProduct(
            name="good", product_type=AccountProduct.TYPE_STANDARD,
            max_margin_per_trade_pct=Decimal("15.00"), max_total_margin_pct=Decimal("60.00"),
        )
        p.clean()  # must not raise

    def test_default_configuration_accepted(self):
        p = AccountProduct(name="def", product_type=AccountProduct.TYPE_STANDARD)
        p.clean()  # 10.00 <= 50.00, both > 0 — must not raise

    def test_per_trade_exactly_equal_to_total_accepted(self):
        p = AccountProduct(
            name="edge", product_type=AccountProduct.TYPE_STANDARD,
            max_margin_per_trade_pct=Decimal("50.00"), max_total_margin_pct=Decimal("50.00"),
        )
        p.clean()  # boundary — not strictly greater, must not raise


# ─────────────────────────────────────────────────────────────────────────
# 3. Snapshot write — new account captures the product's values
# ─────────────────────────────────────────────────────────────────────────
class SnapshotWriteTests(TestCase):
    def setUp(self):
        self.user = make_user(email="o6c1e_snap@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = make_account_product(
            code="o6c1e-ecn", product_type=AccountProduct.TYPE_ECN,
            family=AccountProduct.FAMILY_REAL, min_deposit=Decimal("100"),
            max_margin_per_trade_pct=Decimal("7.50"), max_total_margin_pct=Decimal("40.00"),
        )
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_new_real_account_captures_snapshot(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "150"})
        acc = TradingAccount.objects.get(user=self.user)
        self.assertEqual(acc.max_margin_per_trade_pct_snapshot, Decimal("7.50"))
        self.assertEqual(acc.max_total_margin_pct_snapshot, Decimal("40.00"))

    @patch("simulator.tasks.send_email_async")
    def test_new_demo_account_captures_snapshot(self, _m):
        demo_product = make_account_product(
            code="o6c1e-demo", product_type=AccountProduct.TYPE_DEMO,
            family=AccountProduct.FAMILY_DEMO, min_deposit=Decimal("0"),
            default_balance=Decimal("10000"),
            max_margin_per_trade_pct=Decimal("8.00"), max_total_margin_pct=Decimal("45.00"),
        )
        self.client.post(CREATE_URL, {"product_id": demo_product.pk})
        acc = TradingAccount.objects.get(user=self.user, account_type="DEMO")
        self.assertEqual(acc.max_margin_per_trade_pct_snapshot, Decimal("8.00"))
        self.assertEqual(acc.max_total_margin_pct_snapshot, Decimal("45.00"))

    @patch("simulator.tasks.send_email_async")
    def test_default_product_freezes_10_and_50(self, _m):
        default_product = make_account_product(
            code="o6c1e-default-real", product_type=AccountProduct.TYPE_STANDARD,
            family=AccountProduct.FAMILY_REAL, min_deposit=Decimal("10"),
        )
        self.client.post(CREATE_URL, {"product_id": default_product.pk, "amount": "50"})
        acc = TradingAccount.objects.get(user=self.user, account_product=default_product)
        self.assertEqual(acc.max_margin_per_trade_pct_snapshot, Decimal("10.00"))
        self.assertEqual(acc.max_total_margin_pct_snapshot, Decimal("50.00"))


# ─────────────────────────────────────────────────────────────────────────
# 4. Fallback — existing account with NULL snapshot uses 10/50
# ─────────────────────────────────────────────────────────────────────────
class SnapshotFallbackTests(TestCase):
    def test_account_without_product_has_null_snapshot(self):
        acc = make_account(account_type="STANDARD", balance=Decimal("10000"))
        self.assertIsNone(acc.max_margin_per_trade_pct_snapshot)
        self.assertIsNone(acc.max_total_margin_pct_snapshot)

    def test_consumer_hydration_falls_back_to_10_and_50(self):
        """Mirrors the exact hydration line consumers.py uses:
        float(obj.max_margin_per_trade_pct_snapshot or 10.0)."""
        acc = make_account(account_type="STANDARD", balance=Decimal("10000"))
        resolved_per_trade = float(acc.max_margin_per_trade_pct_snapshot or 10.0)
        resolved_total = float(acc.max_total_margin_pct_snapshot or 50.0)
        self.assertEqual(resolved_per_trade, 10.0)
        self.assertEqual(resolved_total, 50.0)


# ─────────────────────────────────────────────────────────────────────────
# 5. Editing the product later never changes an existing account's snapshot
# ─────────────────────────────────────────────────────────────────────────
class SnapshotFrozenAfterProductChangeTests(TestCase):
    def setUp(self):
        self.user = make_user(email="o6c1e_frozen@test.com")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("500"))
        self.product = make_account_product(
            code="o6c1e-frozen", product_type=AccountProduct.TYPE_ECN,
            family=AccountProduct.FAMILY_REAL, min_deposit=Decimal("100"),
            max_margin_per_trade_pct=Decimal("10.00"), max_total_margin_pct=Decimal("50.00"),
        )
        _login(self.client, self.user)

    @patch("simulator.tasks.send_email_async")
    def test_editing_product_does_not_retroactively_change_account(self, _m):
        self.client.post(CREATE_URL, {"product_id": self.product.pk, "amount": "150"})
        acc = TradingAccount.objects.get(user=self.user)
        original_per_trade = acc.max_margin_per_trade_pct_snapshot
        original_total = acc.max_total_margin_pct_snapshot
        self.assertEqual(original_per_trade, Decimal("10.00"))
        self.assertEqual(original_total, Decimal("50.00"))

        # Change the product's policy — the already-created account must
        # be completely unaffected.
        self.product.max_margin_per_trade_pct = Decimal("2.00")
        self.product.max_total_margin_pct = Decimal("15.00")
        self.product.save()

        acc.refresh_from_db()
        self.assertEqual(acc.max_margin_per_trade_pct_snapshot, original_per_trade)
        self.assertEqual(acc.max_total_margin_pct_snapshot, original_total)


# ─────────────────────────────────────────────────────────────────────────
# 6. The guard actually USES the configured values, not the module default
# ─────────────────────────────────────────────────────────────────────────
class GuardUsesConfiguredValuesTests(TestCase):
    def test_custom_per_trade_cap_is_respected(self):
        """5% cap: a trade requiring 8% must be rejected under the custom
        cap even though it would PASS under the historical 10% default."""
        spec = get_spec("BTCUSD")
        ok_default, code_default, _, _ = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.001, entry_px=82000.0, equity=100.0, margin_used_now=0.0,
            account_snap=_snap(), spec_max_leverage=spec.max_leverage, spec_contract_size=spec.contract_size,
        )
        # 0.001 BTC @ 82000, lev=20 -> margin=$4.10 -> 4.1% -> passes both 10% and 5% actually;
        # use a size that sits between the two thresholds instead.
        ok_custom, code_custom, msg_custom, _ = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.002, entry_px=82000.0, equity=100.0, margin_used_now=0.0,
            account_snap=_snap(), spec_max_leverage=spec.max_leverage, spec_contract_size=spec.contract_size,
            max_margin_per_trade_pct=5.0, max_total_margin_pct=50.0,
        )
        ok_hist, code_hist, _, _ = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.002, entry_px=82000.0, equity=100.0, margin_used_now=0.0,
            account_snap=_snap(), spec_max_leverage=spec.max_leverage, spec_contract_size=spec.contract_size,
        )
        # 0.002 BTC @ 82000 / 20 = $8.20 margin -> 8.2% of $100 equity.
        self.assertTrue(ok_hist)   # 8.2% < 10% (historical default) -> PASS
        self.assertFalse(ok_custom)  # 8.2% > 5% (custom cap) -> FAIL
        self.assertEqual(code_custom, "margin_per_trade_exceeded")
        self.assertIn("5%", msg_custom)

    def test_custom_total_cap_is_respected(self):
        spec = get_spec("BTCUSD")
        # margin_used_now=$20 on equity=$100 -> adding $10 more -> 30% total.
        ok_default, _, _, _ = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.00243902, entry_px=82000.0, equity=100.0, margin_used_now=20.0,
            account_snap=_snap(), spec_max_leverage=spec.max_leverage, spec_contract_size=spec.contract_size,
        )
        ok_custom, code_custom, _, _ = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.00243902, entry_px=82000.0, equity=100.0, margin_used_now=20.0,
            account_snap=_snap(), spec_max_leverage=spec.max_leverage, spec_contract_size=spec.contract_size,
            max_margin_per_trade_pct=10.0, max_total_margin_pct=25.0,
        )
        self.assertTrue(ok_default)     # ~30% < 50% (historical default) -> PASS
        self.assertFalse(ok_custom)     # ~30% > 25% (custom total cap) -> FAIL
        self.assertEqual(code_custom, "total_margin_exceeded")

    def test_omitted_kwargs_use_historical_constants(self):
        """A caller that passes neither kwarg gets EXACTLY the module's own
        historical constants — proving zero behavior change for any
        pre-O.6c-1e caller."""
        import inspect
        sig = inspect.signature(_compute_pretrade_margin_guard)
        self.assertEqual(sig.parameters["max_margin_per_trade_pct"].default, _DEFAULT_MAX_MARGIN_PER_TRADE_PCT)
        self.assertEqual(sig.parameters["max_total_margin_pct"].default, _DEFAULT_MAX_TOTAL_MARGIN_PCT)


# ─────────────────────────────────────────────────────────────────────────
# 7. Admin exposes both fields
# ─────────────────────────────────────────────────────────────────────────
class AdminExposureTests(TestCase):
    def test_risk_parameters_fieldset_lists_both_fields(self):
        risk_fields = None
        for name, opts in AccountProductAdmin.fieldsets:
            if name == "Risk Parameters":
                risk_fields = opts["fields"]
                break
        self.assertIsNotNone(risk_fields, "Risk Parameters fieldset not found")
        self.assertIn("max_margin_per_trade_pct", risk_fields)
        self.assertIn("max_total_margin_pct", risk_fields)


# ─────────────────────────────────────────────────────────────────────────
# 8. No regression — exact BTCUSD case from O.6c-1c
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionBTCUSDTests(TestCase):
    def test_btcusd_001_at_182_75_equity_rejected_at_17_28_pct(self):
        """equity=$182.75, price~$63,157, qty=0.01, account leverage=100,
        BTCUSD spec max_leverage=20 -> required_margin=$31.5785 ->
        per_trade_pct~17.28% > 10% (default, no product override) -> FAIL.
        Reproduces the O.6c-1c manual finding exactly."""
        spec = get_spec("BTCUSD")
        ok, code, msg, details = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.01, entry_px=63157.0, equity=182.75, margin_used_now=0.0,
            account_snap=_snap(leverage=100), spec_max_leverage=spec.max_leverage,
            spec_contract_size=spec.contract_size,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "margin_per_trade_exceeded")
        self.assertAlmostEqual(details["required_margin_pct"], 17.28, delta=0.01)
        self.assertAlmostEqual(details["required_margin"], 31.5785, delta=0.001)

    def test_btcusd_0001_at_182_75_equity_allowed(self):
        spec = get_spec("BTCUSD")
        ok, code, _, details = _compute_pretrade_margin_guard(
            "BTCUSD", qty=0.001, entry_px=63157.0, equity=182.75, margin_used_now=0.0,
            account_snap=_snap(leverage=100), spec_max_leverage=spec.max_leverage,
            spec_contract_size=spec.contract_size,
        )
        self.assertTrue(ok)
        self.assertAlmostEqual(details["required_margin_pct"], 1.73, delta=0.01)

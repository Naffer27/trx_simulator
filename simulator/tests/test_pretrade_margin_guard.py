# simulator/tests/test_pretrade_margin_guard.py
"""
Phase 6B.1 — Pre-Trade Margin Guard unit tests.

All tests exercise _compute_pretrade_margin_guard() directly — a pure function
with no I/O, no DB, no WebSocket. SymbolSpec values from the live registry are
used so the tests stay honest about real instrument parameters.

BTC spec:  contract_size=1.0, max_leverage=20, base_price~82000
ETH spec:  contract_size=1.0, max_leverage=20
EUR/USD spec: contract_size=100_000, max_leverage=500

Account snap defaults used in helpers:
  leverage          = 100 (account-level; effective = min(100, spec.max_leverage))
  allowed_symbols   = None  (all allowed)
  max_lot_size      = None  (no product cap)
  margin_call_level = 100.0 (standard broker level)
"""
from django.test import SimpleTestCase

from simulator.consumers import (
    _compute_pretrade_margin_guard,
    _DEFAULT_MAX_MARGIN_PER_TRADE_PCT,
    _DEFAULT_MAX_TOTAL_MARGIN_PCT,
)
from market_data.symbol_specs import get_spec


# ── Helpers ───────────────────────────────────────────────────────────────────

def _snap(**overrides) -> dict:
    """Return a minimal account_snap dict with safe defaults."""
    base = {
        "leverage":          100,
        "allowed_symbols":   None,
        "max_lot_size":      None,
        "margin_call_level": 100.0,
    }
    base.update(overrides)
    return base


def _guard(symbol, qty, entry_px, equity, margin_used_now=0.0, **snap_overrides):
    spec = get_spec(symbol)
    snap = _snap(**snap_overrides)
    return _compute_pretrade_margin_guard(
        symbol, qty, entry_px, equity, margin_used_now,
        snap, spec.max_leverage, spec.contract_size,
    )


# ── BTCUSD — per-trade margin cap ─────────────────────────────────────────────

class BTCUSDPerTradeCapTests(SimpleTestCase):
    """
    BTCUSD: contract_size=1.0, spec.max_leverage=20.
    Account leverage=100 → effective=min(100,20)=20.

    required_margin = entry_px * qty * 1.0 / 20
    """

    def test_btcusd_001_on_100_account_rejected(self):
        """0.01 BTC on $100: required_margin ≈ $41 → 41% > 10% → rejected."""
        ok, code, msg = _guard("BTCUSD", qty=0.01, entry_px=82_000.0, equity=100.0)
        self.assertFalse(ok)
        self.assertEqual(code, "margin_per_trade_exceeded")
        self.assertIn("Prueba con un lote menor", msg)

    def test_btcusd_0001_on_100_account_allowed(self):
        """0.001 BTC on $100: required_margin ≈ $4.10 → 4.1% < 10% → allowed."""
        ok, code, _ = _guard("BTCUSD", qty=0.001, entry_px=82_000.0, equity=100.0)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_btcusd_boundary_exactly_at_10pct_allowed(self):
        """required_margin = exactly 10% of equity → allowed (≤ not <)."""
        # required_margin = entry_px * qty / 20 = 10% of equity
        # → entry_px * qty / 20 = 0.10 * equity
        # → qty = 0.10 * 200 * 20 / 82000 ≈ 0.004878
        equity = 200.0
        qty = (0.10 * equity * 20) / 82_000.0   # exactly 10%
        ok, code, _ = _guard("BTCUSD", qty=qty, entry_px=82_000.0, equity=equity)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_btcusd_slightly_over_10pct_rejected(self):
        equity = 200.0
        qty = (0.101 * equity * 20) / 82_000.0  # 10.1%
        ok, code, _ = _guard("BTCUSD", qty=qty, entry_px=82_000.0, equity=equity)
        self.assertFalse(ok)
        self.assertEqual(code, "margin_per_trade_exceeded")


# ── EUR/USD — valid micro trade must pass ─────────────────────────────────────

class EURUSDMicroTradeTests(SimpleTestCase):
    """
    EUR/USD: contract_size=100_000, spec.max_leverage=500.
    Account leverage=100 → effective=min(100,500)=100.

    required_margin = 1.08 * 0.01 * 100_000 / 100 = $10.80
    """

    def test_eurusd_001_on_1000_account_allowed(self):
        """0.01 EUR/USD on $1,000: required_margin ≈ $10.80 → 1.08% < 10% → allowed."""
        ok, code, _ = _guard("EUR/USD", qty=0.01, entry_px=1.08, equity=1_000.0)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_eurusd_001_on_200_account_allowed(self):
        """0.01 EUR/USD on $200: required_margin ≈ $10.80 → 5.4% < 10% → allowed."""
        ok, code, _ = _guard("EUR/USD", qty=0.01, entry_px=1.08, equity=200.0)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_eurusd_1lot_on_1000_account_rejected(self):
        """1.0 EUR/USD on $1,000: required_margin ≈ $1,080 → 108% > 10% → rejected."""
        ok, code, _ = _guard("EUR/USD", qty=1.0, entry_px=1.08, equity=1_000.0)
        self.assertFalse(ok)
        self.assertEqual(code, "margin_per_trade_exceeded")


# ── Total margin cap (50%) ────────────────────────────────────────────────────

class TotalMarginCapTests(SimpleTestCase):
    """
    Even if per-trade % is fine, total margin after open must stay ≤ 50%.
    """

    def test_total_margin_exceeded_when_existing_positions_heavy(self):
        """
        Already using 45% margin, new trade adds another 10% → total 55% > 50%.
        """
        equity = 1_000.0
        margin_used_now = 450.0   # 45% already used
        # BTCUSD 0.001: required_margin = 82000 * 0.001 / 20 = $4.10 → 0.41% per-trade → passes guard 3
        # total = 454.10 / 1000 = 45.41% → still under 50% → allowed
        ok, code, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=equity,
            margin_used_now=margin_used_now,
        )
        self.assertTrue(ok, "Small BTCUSD lot should pass when margin_used=45%")

        # Now push existing margin to 495 → adding $4.10 → total 499.10 / 1000 = 49.9% → OK
        # Push to 497 → total 501.1 → 50.11% → REJECTED
        ok2, code2, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=equity,
            margin_used_now=497.0,
        )
        self.assertFalse(ok2)
        self.assertEqual(code2, "total_margin_exceeded")

    def test_total_margin_exactly_at_50pct_allowed(self):
        """required_margin brings total to exactly 50% → allowed."""
        equity = 1_000.0
        # We want (margin_used_now + required_margin) / equity = 0.50
        # BTCUSD 0.001 → required = $4.10 → margin_used_now = 500 - 4.10 = 495.90
        required = 82_000.0 * 0.001 * 1.0 / 20   # = $4.10
        margin_used_now = equity * 0.50 - required
        ok, code, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=equity,
            margin_used_now=margin_used_now,
        )
        self.assertTrue(ok)
        self.assertEqual(code, "ok")


# ── required_margin > free_margin ────────────────────────────────────────────

class InsufficientFreeMarginTests(SimpleTestCase):
    """
    When margin_used_now is already very high, free_margin < required_margin.
    The total_margin_exceeded check catches this (total > 50% long before free_margin=0).
    """

    def test_rejects_when_required_exceeds_free_margin(self):
        """
        equity=100, margin_used=95. free_margin=5.
        Any trade requiring > $5 margin → total > 50% of equity → rejected.
        """
        ok, code, _ = _guard(
            "EUR/USD", qty=0.01, entry_px=1.08, equity=100.0,
            margin_used_now=95.0,
        )
        # required=$10.80, total=105.80 → 105.8% > 50% → rejected
        self.assertFalse(ok)
        # Could be total_margin_exceeded OR margin_per_trade_exceeded (10.80/100=10.8%)
        self.assertIn(code, ("margin_per_trade_exceeded", "total_margin_exceeded"))


# ── Margin level projection vs margin_call_level_snapshot ────────────────────

class MarginCallLevelBreachTests(SimpleTestCase):
    """
    margin_level_after = equity / total_margin_after * 100
    If this falls below margin_call_level_snapshot → rejected.
    """

    def test_rejects_when_margin_level_would_breach_snapshot(self):
        """
        Account has margin_call_level=200% (tight product rule).
        If margin_level_after = 150% < 200% → rejected.
        """
        # equity / (margin_used + required) < 200%
        # → (margin_used + required) > equity / 2.0
        # Use BTCUSD 0.001 (required=$4.10) and set margin_used so total > equity/2
        # equity=100, total > 50 → margin_used=47, required=4.10 → total=51.10
        # margin_level = 100/51.10*100 = 195.7% < 200% → rejected
        equity = 100.0
        required = 82_000.0 * 0.001 / 20   # $4.10
        margin_used_now = 47.0
        # Verify per-trade % first: 4.10/100=4.1% < 10% → passes guard 3
        # Total: 51.10/100 = 51.1% > 50% → actually caught by guard 4 first

        # Use a scenario where per-trade and total are fine but margin_level is too low
        # equity=1000, per-trade = 4.10/1000 = 0.41% (fine)
        # total = 450+4.10 = 454.10/1000 = 45.41% < 50% (fine)
        # margin_level = 1000/454.10*100 = 220.2% — need margin_call=250%
        ok, code, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=1_000.0,
            margin_used_now=450.0,
            margin_call_level=250.0,   # very tight: requires 250% margin level
        )
        # margin_level = 1000/454.10*100 ≈ 220.2% < 250% → rejected
        self.assertFalse(ok)
        self.assertEqual(code, "margin_call_level_breach")

    def test_allows_when_margin_level_above_snapshot(self):
        """margin_level_after > margin_call_level_snapshot → allowed."""
        ok, code, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=1_000.0,
            margin_used_now=0.0,
            margin_call_level=100.0,   # standard
        )
        # margin_level = 1000/4.10*100 ≈ 24390% >> 100% → allowed
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_no_margin_call_check_when_no_margin_used(self):
        """With margin_used=0 and tiny lot → no margin_call_level breach."""
        ok, code, _ = _guard(
            "EUR/USD", qty=0.01, entry_px=1.08, equity=1_000.0,
            margin_used_now=0.0,
            margin_call_level=150.0,
        )
        # margin_level = 1000/10.80*100 ≈ 9259% >> 150% → allowed
        self.assertTrue(ok)
        self.assertEqual(code, "ok")


# ── max_lot_size_snapshot ─────────────────────────────────────────────────────

class MaxLotSizeSnapshotTests(SimpleTestCase):

    def test_rejects_qty_above_snapshot(self):
        ok, code, msg = _guard(
            "EUR/USD", qty=1.0, entry_px=1.08, equity=100_000.0,
            max_lot_size=0.5,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "lot_size_exceeds_product_limit")
        self.assertIn("0.500", msg)

    def test_allows_qty_equal_to_snapshot(self):
        ok, code, _ = _guard(
            "EUR/USD", qty=0.5, entry_px=1.08, equity=100_000.0,
            max_lot_size=0.5,
        )
        # 0.5 = 0.5 → not >, so allowed (per-trade margin is trivial on $100k)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_allows_qty_below_snapshot(self):
        ok, code, _ = _guard(
            "EUR/USD", qty=0.1, entry_px=1.08, equity=100_000.0,
            max_lot_size=0.5,
        )
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_no_cap_when_snapshot_is_none(self):
        """max_lot_size=None means no product-level cap."""
        ok, code, _ = _guard(
            "EUR/USD", qty=0.5, entry_px=1.08, equity=100_000.0,
            max_lot_size=None,
        )
        self.assertTrue(ok)
        self.assertEqual(code, "ok")


# ── allowed_symbols_snapshot ─────────────────────────────────────────────────

class AllowedSymbolsSnapshotTests(SimpleTestCase):

    def test_rejects_symbol_not_in_whitelist(self):
        ok, code, msg = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=100_000.0,
            allowed_symbols=["EUR/USD", "GBP/USD"],
        )
        self.assertFalse(ok)
        self.assertEqual(code, "symbol_not_allowed")
        self.assertIn("símbolo no permitido", msg)

    def test_allows_symbol_in_whitelist(self):
        ok, code, _ = _guard(
            "EUR/USD", qty=0.01, entry_px=1.08, equity=100_000.0,
            allowed_symbols=["EUR/USD", "GBP/USD"],
        )
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_allows_any_symbol_when_whitelist_is_none(self):
        ok, code, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=100_000.0,
            allowed_symbols=None,
        )
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_rejects_unknown_symbol_if_not_in_whitelist(self):
        ok, code, _ = _guard(
            "XAU/USD", qty=0.01, entry_px=2400.0, equity=100_000.0,
            allowed_symbols=["EUR/USD"],
        )
        self.assertFalse(ok)
        self.assertEqual(code, "symbol_not_allowed")


# ── Check ordering: symbol check fires before margin check ───────────────────

class CheckOrderTests(SimpleTestCase):
    """allowed_symbols is checked first — margin is never computed."""

    def test_symbol_check_fires_before_margin_check(self):
        """Even if margin would be fine, symbol block comes first."""
        ok, code, _ = _guard(
            "BTCUSD", qty=0.001, entry_px=82_000.0, equity=100_000.0,
            allowed_symbols=["EUR/USD"],
        )
        self.assertFalse(ok)
        self.assertEqual(code, "symbol_not_allowed")

    def test_lot_check_fires_before_margin_check(self):
        """max_lot_size block fires before margin is computed."""
        ok, code, _ = _guard(
            "EUR/USD", qty=2.0, entry_px=1.08, equity=100_000.0,
            max_lot_size=0.5,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "lot_size_exceeds_product_limit")


# ── Reconnect does not close healthy position ─────────────────────────────────

class ReconnectSafetyTests(SimpleTestCase):
    """
    _compute_pretrade_margin_guard() is only called inside _order_new().
    It is a pure function — it never closes, suspends, or mutates positions.
    This test verifies the function never modifies the account_snap it receives.
    """

    def test_guard_does_not_mutate_account_snap(self):
        snap = _snap(leverage=50, margin_call_level=100.0)
        original = dict(snap)
        _compute_pretrade_margin_guard(
            "EUR/USD", 0.01, 1.08, 1_000.0, 0.0,
            snap,
            500,    # spec_max_leverage
            100_000.0,  # spec_contract_size
        )
        self.assertEqual(snap, original)

    def test_guard_returns_ok_for_healthy_position_params(self):
        """Parameters representing a healthy existing position scenario → ok."""
        ok, code, _ = _guard(
            "EUR/USD", qty=0.01, entry_px=1.08, equity=5_000.0,
            margin_used_now=50.0,   # 1% margin used (one open position)
        )
        self.assertTrue(ok)
        self.assertEqual(code, "ok")


# ── Constants are exported ────────────────────────────────────────────────────

class ConstantTests(SimpleTestCase):
    def test_per_trade_default_is_10(self):
        self.assertEqual(_DEFAULT_MAX_MARGIN_PER_TRADE_PCT, 10.0)

    def test_total_margin_default_is_50(self):
        self.assertEqual(_DEFAULT_MAX_TOTAL_MARGIN_PCT, 50.0)

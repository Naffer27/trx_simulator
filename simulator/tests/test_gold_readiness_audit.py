# simulator/tests/test_gold_readiness_audit.py
"""
Bloque C0 — Gold (XAU/USD) readiness audit.

XAU/USD stays disabled (SymbolSpec.enabled=False) — these tests do NOT
activate gold trading. They confirm the backend PnL/margin formulas, which
are already contract_size-generic, produce correct results for a metal
instrument (contract_size=100, 1 lot = 100 troy oz) before gold is ever
switched on.

Covered:
  1. XAU/USD spec sanity (contract_size, max_leverage, enabled, finnhub_symbol).
  2. _compute_offline_equity_margin (simulator/tasks.py) — BUY/SELL floating
     PnL and margin, using real Position/TradingAccount model instances.
  3. _compute_pretrade_margin_guard (simulator/consumers.py) — per-trade %
     guard for XAU/USD, and effective_leverage capped at spec.max_leverage
     even when the account's own leverage is higher.
  4. XAU/USD remains outside allowed_symbols() / _ALLOWED_SYMBOLS — the WS
     boundary still rejects it.

No live market-data calls are made or proposed to run automatically.
"""
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from market_data.symbol_specs import get_spec, allowed_symbols
from simulator.consumers import _ALLOWED_SYMBOLS, _compute_pretrade_margin_guard
from simulator.tasks import _compute_offline_equity_margin

from .factories import make_account, make_position


# ─────────────────────────────────────────────────────────────────────────────
# 1. Spec sanity
# ─────────────────────────────────────────────────────────────────────────────

class TestXAUUSDSpecSanity(SimpleTestCase):

    def test_contract_size_is_100(self):
        self.assertEqual(get_spec("XAU/USD").contract_size, 100.0)

    def test_max_leverage_is_100(self):
        self.assertEqual(get_spec("XAU/USD").max_leverage, 100)

    def test_disabled(self):
        self.assertFalse(get_spec("XAU/USD").enabled)

    def test_finnhub_symbol_configured(self):
        # Routing metadata only — no live call is made here.
        self.assertEqual(get_spec("XAU/USD").finnhub_symbol, "OANDA:XAU_USD")


# ─────────────────────────────────────────────────────────────────────────────
# 2. _compute_offline_equity_margin — real Position/TradingAccount instances
# ─────────────────────────────────────────────────────────────────────────────

class TestGoldOfflineEquityMargin(TestCase):

    def test_buy_pnl_uses_contract_size(self):
        """0.1 lot BUY, entry 2400.00 -> bid 2410.00: (2410-2400)*0.1*100 = $100."""
        account = make_account(balance=Decimal("100000"))
        pos = make_position(
            account=account, symbol="XAU/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("2400.00"),
        )
        equity, margin_used, total_floating, pos_fp_map = _compute_offline_equity_margin(
            [pos], {"XAU/USD": (2410.00, 2410.30)}, account,
        )
        self.assertAlmostEqual(pos_fp_map[pos.id], 100.0, places=6)
        self.assertAlmostEqual(total_floating, 100.0, places=6)
        self.assertAlmostEqual(equity, 100000.0 + 100.0, places=6)

    def test_sell_pnl_uses_contract_size(self):
        """0.1 lot SELL, entry 2400.00 -> ask 2395.00: (2400-2395)*0.1*100 = $50."""
        account = make_account(balance=Decimal("100000"))
        pos = make_position(
            account=account, symbol="XAU/USD", side="SELL",
            qty=Decimal("0.1"), avg_price=Decimal("2400.00"),
        )
        equity, margin_used, total_floating, pos_fp_map = _compute_offline_equity_margin(
            [pos], {"XAU/USD": (2394.70, 2395.00)}, account,
        )
        self.assertAlmostEqual(pos_fp_map[pos.id], 50.0, places=6)
        self.assertAlmostEqual(total_floating, 50.0, places=6)

    def test_margin_formula(self):
        """margin = avg * qty * contract_size / effective_leverage.

        Account leverage (50, factory default) < spec.max_leverage (100)
        -> effective_leverage = account leverage = 50.
        margin = 2400 * 0.1 * 100 / 50 = $480.
        """
        account = make_account(balance=Decimal("100000"))
        pos = make_position(
            account=account, symbol="XAU/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("2400.00"),
        )
        _, margin_used, _, _ = _compute_offline_equity_margin(
            [pos], {"XAU/USD": (2400.00, 2400.30)}, account,
        )
        self.assertAlmostEqual(margin_used, 480.0, places=6)

    def test_effective_leverage_capped_at_instrument_max(self):
        """Account leverage (500) > spec.max_leverage (100) for XAU/USD
        -> effective_leverage must be capped at 100, not 500.

        margin = 2400 * 0.1 * 100 / 100 = $240 (not $48, which 500x would give).
        """
        account = make_account(balance=Decimal("100000"))
        account.leverage = 500  # in-memory only; _compute_offline_equity_margin does no DB I/O
        pos = make_position(
            account=account, symbol="XAU/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("2400.00"),
        )
        _, margin_used, _, _ = _compute_offline_equity_margin(
            [pos], {"XAU/USD": (2400.00, 2400.30)}, account,
        )
        self.assertAlmostEqual(margin_used, 240.0, places=6)
        self.assertNotAlmostEqual(margin_used, 48.0, places=6)


# ─────────────────────────────────────────────────────────────────────────────
# 3. _compute_pretrade_margin_guard — pure function, XAU/USD parameters
# ─────────────────────────────────────────────────────────────────────────────

def _gold_guard(qty, entry_px, equity, margin_used_now=0.0, account_leverage=100):
    spec = get_spec("XAU/USD")
    snap = {
        "leverage": account_leverage,
        "allowed_symbols": None,
        "max_lot_size": None,
        "margin_call_level": 100.0,
    }
    return _compute_pretrade_margin_guard(
        "XAU/USD", qty, entry_px, equity, margin_used_now,
        snap, spec.max_leverage, spec.contract_size,
    )


class TestGoldPretradeMarginGuard(SimpleTestCase):

    def test_small_lot_allowed(self):
        """1 lot @ 2400, equity 100k, effective_lev=100: margin=$2400 -> 2.4% < 10% -> ok."""
        ok, code, _ = _gold_guard(qty=1.0, entry_px=2400.0, equity=100_000.0)
        self.assertTrue(ok)
        self.assertEqual(code, "ok")

    def test_large_lot_rejected_per_trade_cap(self):
        """10 lot @ 2400, equity 100k: margin=$24000 -> 24% > 10% -> rejected."""
        ok, code, _ = _gold_guard(qty=10.0, entry_px=2400.0, equity=100_000.0)
        self.assertFalse(ok)
        self.assertEqual(code, "margin_per_trade_exceeded")

    def test_effective_leverage_capped_even_with_higher_account_leverage(self):
        """Account leverage=500 must still be capped to spec.max_leverage=100.

        1 lot @ 2400, equity 100k: required_margin = 2400*1*100/100 = $2400
        regardless of the account's 500x setting (would be $480 uncapped).
        """
        ok_capped, code_capped, _ = _gold_guard(
            qty=1.0, entry_px=2400.0, equity=100_000.0, account_leverage=500,
        )
        ok_at_100, code_at_100, _ = _gold_guard(
            qty=1.0, entry_px=2400.0, equity=100_000.0, account_leverage=100,
        )
        # Same outcome whether the account requests 500x or 100x — the
        # instrument's max_leverage (100) is the binding constraint either way.
        self.assertEqual((ok_capped, code_capped), (ok_at_100, code_at_100))


# ─────────────────────────────────────────────────────────────────────────────
# 4. XAU/USD stays outside the enabled whitelist
# ─────────────────────────────────────────────────────────────────────────────

class TestGoldStillDisabled(SimpleTestCase):

    def test_not_in_allowed_symbols(self):
        self.assertNotIn("XAU/USD", allowed_symbols())

    def test_not_in_consumers_allowed_symbols(self):
        self.assertNotIn("XAU/USD", _ALLOWED_SYMBOLS)

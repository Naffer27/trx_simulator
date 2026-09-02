"""
simulator/tests/test_pnl_engine.py — MARGIN-02.

Pure unit tests for simulator/pnl_engine.py: the root-cause fix for the
USD/JPY quote-currency PnL bug (a live, exploitable bug before this
block — USD/JPY is enabled — see docs comments in pnl_engine.py).
"""
from decimal import Decimal

from django.test import SimpleTestCase

from simulator import pnl_engine as pe


class CalculateQuotePnlTests(SimpleTestCase):
    """The formula itself was never the bug — this is unchanged math,
    just now explicitly labeled as being in the QUOTE currency."""

    def test_buy_profit(self):
        pnl = pe.calculate_quote_pnl("buy", 1.10000, 1.10100, 1.0, 100000)
        self.assertEqual(pnl, Decimal("100.00000"))

    def test_buy_loss(self):
        pnl = pe.calculate_quote_pnl("buy", 1.10100, 1.10000, 1.0, 100000)
        self.assertEqual(pnl, Decimal("-100.00000"))

    def test_sell_profit(self):
        pnl = pe.calculate_quote_pnl("sell", 1.10100, 1.10000, 1.0, 100000)
        self.assertEqual(pnl, Decimal("100.00000"))

    def test_case_insensitive_side(self):
        a = pe.calculate_quote_pnl("BUY", 1.1, 1.2, 1.0, 100000)
        b = pe.calculate_quote_pnl("buy", 1.1, 1.2, 1.0, 100000)
        self.assertEqual(a, b)

    def test_usd_jpy_10_pips_is_10000_jpy(self):
        """The exact example from the MARGIN-02 spec: 1 lot BUY, entry
        155.000, close 155.100, contract_size 100000 -> 10000 (JPY, not
        yet converted)."""
        pnl = pe.calculate_quote_pnl("buy", 155.000, 155.100, 1.0, 100000)
        self.assertEqual(pnl, Decimal("10000.00000"))

    def test_invalid_input_returns_zero_not_raise(self):
        pnl = pe.calculate_quote_pnl("buy", "garbage", 1.1, 1.0, 100000)
        self.assertEqual(pnl, Decimal("0"))


class ConvertPnlNoConversionTests(SimpleTestCase):
    """Rule 1: quote_currency == account_currency -> no conversion."""

    def test_usd_quote_usd_account_passthrough(self):
        result = pe.convert_pnl_to_account_currency(
            Decimal("100"), base_currency="EUR", quote_currency="USD", account_currency="USD",
        )
        self.assertTrue(result.converted)
        self.assertEqual(result.pnl_account, Decimal("100"))
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_NONE)
        self.assertEqual(result.conversion_rate, Decimal("1"))
        self.assertIsNone(result.error_code)


class ConvertPnlBaseAccountInverseTests(SimpleTestCase):
    """Rule 2: base_currency == account_currency -> divide by close price."""

    def test_usd_jpy_10000_jpy_converts_to_64_47_usd(self):
        """The MARGIN-02 spec's exact worked example."""
        result = pe.convert_pnl_to_account_currency(
            Decimal("10000"), base_currency="USD", quote_currency="JPY", account_currency="USD",
            conversion_prices={"USD/JPY": 155.100}, conversion_symbol="USD/JPY",
        )
        self.assertTrue(result.converted)
        self.assertAlmostEqual(float(result.pnl_account), 64.47, places=2)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_BASE_ACCOUNT_INVERSE)
        self.assertEqual(float(result.conversion_rate), 155.100)

    def test_missing_rate_is_safe_failure_not_fabricated(self):
        result = pe.convert_pnl_to_account_currency(
            Decimal("10000"), base_currency="USD", quote_currency="JPY", account_currency="USD",
            conversion_prices={}, conversion_symbol="USD/JPY",
        )
        self.assertFalse(result.converted)
        self.assertIsNone(result.pnl_account)
        self.assertEqual(result.error_code, pe.ERROR_NO_CONVERSION_RATE)

    def test_zero_rate_is_safe_failure(self):
        result = pe.convert_pnl_to_account_currency(
            Decimal("10000"), base_currency="USD", quote_currency="JPY", account_currency="USD",
            conversion_prices={"USD/JPY": 0}, conversion_symbol="USD/JPY",
        )
        self.assertFalse(result.converted)
        self.assertIsNone(result.pnl_account)


class ConvertPnlUnsupportedCrossTests(SimpleTestCase):
    """Rule 3: neither quote nor base matches account currency — only
    convert with an explicit rate; never fabricate one."""

    def test_no_rate_available_is_safe_failure(self):
        result = pe.convert_pnl_to_account_currency(
            Decimal("100"), base_currency="EUR", quote_currency="GBP", account_currency="USD",
        )
        self.assertFalse(result.converted)
        self.assertIsNone(result.pnl_account)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_UNSUPPORTED)
        self.assertEqual(result.error_code, pe.ERROR_NO_CONVERSION_RATE)

    def test_explicit_rate_is_honored(self):
        result = pe.convert_pnl_to_account_currency(
            Decimal("100"), base_currency="EUR", quote_currency="GBP", account_currency="USD",
            conversion_prices={"GBP/USD": 1.30},
        )
        self.assertTrue(result.converted)
        self.assertEqual(result.pnl_account, Decimal("130.0"))
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_EXPLICIT_RATE)

    def test_never_fabricates_even_with_unrelated_prices_present(self):
        result = pe.convert_pnl_to_account_currency(
            Decimal("100"), base_currency="EUR", quote_currency="GBP", account_currency="USD",
            conversion_prices={"USD/JPY": 155.0},
        )
        self.assertFalse(result.converted)
        self.assertIsNone(result.pnl_account)


class CalculatePositionPnlTests(SimpleTestCase):
    """The single entry point real call sites use — reads SymbolSpec via
    the pure, DB-free profile_from_symbol_spec() bridge."""

    def test_eurusd_usd_account_no_conversion(self):
        result = pe.calculate_position_pnl("buy", 1.10000, 1.10100, 1.0, "EUR/USD", "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_NONE)
        self.assertAlmostEqual(float(result.pnl_account), 100.0, places=2)

    def test_btcusd_usd_account_no_conversion(self):
        result = pe.calculate_position_pnl("buy", 82000.00, 82100.00, 1.0, "BTCUSD", "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_NONE)
        self.assertAlmostEqual(float(result.pnl_account), 100.0, places=2)

    def test_usd_jpy_buy_10_pips_is_64_47_usd(self):
        result = pe.calculate_position_pnl("buy", 155.000, 155.100, 1.0, "USD/JPY", "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_BASE_ACCOUNT_INVERSE)
        self.assertAlmostEqual(float(result.pnl_account), 64.47, places=2)
        self.assertEqual(result.quote_currency, "JPY")

    def test_usd_jpy_sell_10_pips_is_symmetric(self):
        """SELL entry 155.100, close 155.000 (price fell 10 pips, profit
        for a SELL). pnl_quote = +10000 JPY, converted using the close
        price (155.000) as the rate -> 10000/155.000 = 64.52 USD."""
        result = pe.calculate_position_pnl("sell", 155.100, 155.000, 1.0, "USD/JPY", "USD")
        self.assertTrue(result.converted)
        self.assertAlmostEqual(float(result.pnl_account), 64.52, places=2)
        self.assertGreater(result.pnl_account, 0)

    def test_usd_jpy_loss_correctly_converted(self):
        """BUY entry 155.100, close 155.000 (price fell -> loss for a
        BUY). pnl_quote = -10000 JPY, /155.000 = -64.52 USD."""
        result = pe.calculate_position_pnl("buy", 155.100, 155.000, 1.0, "USD/JPY", "USD")
        self.assertTrue(result.converted)
        self.assertLess(float(result.pnl_account), 0)
        self.assertAlmostEqual(float(result.pnl_account), -64.52, places=2)

    def test_unknown_symbol_is_safe_failure(self):
        result = pe.calculate_position_pnl("buy", 1.0, 1.1, 1.0, "NOTASYMBOL", "USD")
        self.assertFalse(result.converted)
        self.assertIsNone(result.pnl_account)


class PositionPnlFloatTests(SimpleTestCase):
    """position_pnl_float() — the one function WS and Celery both call."""

    def test_eurusd_matches_old_formula(self):
        val = pe.position_pnl_float("buy", 1.10000, 1.10100, 1.0, "EUR/USD", "USD")
        self.assertAlmostEqual(val, 100.0, places=2)

    def test_usd_jpy_matches_spec_example(self):
        val = pe.position_pnl_float("buy", 155.000, 155.100, 1.0, "USD/JPY", "USD")
        self.assertAlmostEqual(val, 64.47, places=2)

    def test_unsupported_conversion_returns_zero_not_fabricated(self):
        """Cuenta no USD sin conversión disponible: falla segura, no
        número inventado — proven at the engine level (unreachable via
        real account data today, since every account is USD). GBP/USD
        (base=GBP, quote=USD) into an EUR account matches neither the
        no-conversion nor the base-account-inverse rule, and no explicit
        rate is supplied — the only safe outcome is 0.0, loudly logged,
        never a guessed number."""
        val = pe.position_pnl_float("buy", 1.0, 1.1, 1.0, "GBP/USD", "EUR")
        self.assertEqual(val, 0.0)


# ─────────────────────────────────────────────────────────────────────────
# FIX-USDJPY-MARGIN-01-B — calculate_margin_notional_account_currency() /
# calculate_required_margin(). Root cause: every margin-required formula
# in this codebase computed price*qty*contract_size and treated it as
# already-account-currency — true only for quote_currency==account_
# currency. USD/JPY (base=USD) overstated required_margin by ~the
# USD/JPY rate (~160x, confirmed live — GOLDEN-USDJPY-MARGIN-01).
# ─────────────────────────────────────────────────────────────────────────
class CalculateMarginNotionalTests(SimpleTestCase):
    """Case A/B/C, delegating to convert_pnl_to_account_currency()
    UNCHANGED — no conversion rule duplicated here."""

    def test_case_a_eurusd_quote_equals_account_no_conversion(self):
        result = pe.calculate_margin_notional_account_currency("EUR/USD", 1.15921, 0.01, "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_NONE)
        self.assertAlmostEqual(float(result.notional_account), 1159.21, places=2)

    def test_case_a_gbpusd_unchanged(self):
        result = pe.calculate_margin_notional_account_currency("GBP/USD", 1.35162, 0.01, "USD")
        self.assertAlmostEqual(float(result.notional_account), 1351.62, places=2)

    def test_case_a_audusd_unchanged(self):
        result = pe.calculate_margin_notional_account_currency("AUD/USD", 0.71448, 0.01, "USD")
        self.assertAlmostEqual(float(result.notional_account), 714.48, places=2)

    def test_case_b_usdjpy_notional_is_qty_times_contract_size_only(self):
        """The core fix: base_currency(USD) == account_currency(USD) ->
        notional_account = qty*contract_size, the price is NEVER
        multiplied in — 0.01 * 100000 = 1000, independent of price."""
        result = pe.calculate_margin_notional_account_currency("USD/JPY", 160.19, 0.01, "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_BASE_ACCOUNT_INVERSE)
        self.assertAlmostEqual(float(result.notional_account), 1000.0, places=2)

    def test_case_b_usdjpy_notional_independent_of_price_150(self):
        result = pe.calculate_margin_notional_account_currency("USD/JPY", 150.0, 0.01, "USD")
        self.assertAlmostEqual(float(result.notional_account), 1000.0, places=2)

    def test_case_b_usdjpy_notional_independent_of_price_170(self):
        result = pe.calculate_margin_notional_account_currency("USD/JPY", 170.0, 0.01, "USD")
        self.assertAlmostEqual(float(result.notional_account), 1000.0, places=2)

    def test_case_b_usdchf_latent(self):
        """USD/CHF is enabled=False today (not tradeable) but the PURE
        helper is testable directly regardless — same Case B shape as
        USD/JPY (base=USD=account_currency)."""
        result = pe.calculate_margin_notional_account_currency("USD/CHF", 0.90000, 0.01, "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_BASE_ACCOUNT_INVERSE)
        self.assertAlmostEqual(float(result.notional_account), 1000.0, places=2)

    def test_case_b_usdcad_latent(self):
        result = pe.calculate_margin_notional_account_currency("USD/CAD", 1.37000, 0.01, "USD")
        self.assertTrue(result.converted)
        self.assertEqual(result.conversion_mode, pe.CONVERSION_MODE_BASE_ACCOUNT_INVERSE)
        self.assertAlmostEqual(float(result.notional_account), 1000.0, places=2)

    def test_case_c_fails_closed_no_invented_rate(self):
        """Neither base (GBP) nor quote (USD) matches account_currency
        (EUR), and no explicit rate is supplied — must fail closed, never
        assume 1:1, never fabricate a rate."""
        result = pe.calculate_margin_notional_account_currency("GBP/USD", 1.35, 0.01, "EUR")
        self.assertFalse(result.converted)
        self.assertIsNone(result.notional_account)
        self.assertEqual(result.error_code, pe.ERROR_NO_CONVERSION_RATE)

    def test_invalid_currency_inputs_safe_not_raise(self):
        result = pe.calculate_margin_notional_account_currency("NOTASYMBOL", 1.0, 0.01, "USD")
        self.assertFalse(result.converted)
        self.assertIsNone(result.notional_account)
        self.assertIsNotNone(result.error_code)

    def test_contract_size_preserved(self):
        """contract_size is read from the real SymbolSpec registry, never
        hardcoded — EUR/USD's 100,000 flows through unchanged."""
        result = pe.calculate_margin_notional_account_currency("EUR/USD", 1.0, 1.0, "USD")
        self.assertAlmostEqual(float(result.notional_quote), 100000.0, places=2)

    def test_helper_has_no_db_side_effects(self):
        """Pure — no Django ORM import anywhere in the call chain."""
        import inspect
        source = inspect.getsource(pe.calculate_margin_notional_account_currency)
        self.assertNotIn("django.db", source)
        self.assertNotIn(".objects.", source)
        self.assertNotIn(".save(", source)


class CalculateRequiredMarginTests(SimpleTestCase):
    """calculate_required_margin() — the float convenience wrapper every
    real margin call site (consumers.py, risk_engine.py, tasks.py) uses."""

    def test_usdjpy_100x_leverage_is_10_dollars(self):
        margin, error = pe.calculate_required_margin("USD/JPY", 160.19, 0.01, 100, "USD")
        self.assertIsNone(error)
        self.assertAlmostEqual(margin, 10.0, places=2)

    def test_leverage_applied_after_currency_conversion(self):
        """50x leverage on the SAME (already-converted) $1000 notional
        must give exactly double the margin of 100x — proving leverage
        divides the converted account-currency notional, not a
        pre-conversion quote-currency figure."""
        margin_100x, _ = pe.calculate_required_margin("USD/JPY", 160.19, 0.01, 100, "USD")
        margin_50x, _ = pe.calculate_required_margin("USD/JPY", 160.19, 0.01, 50, "USD")
        self.assertAlmostEqual(margin_50x, margin_100x * 2, places=6)

    def test_eurusd_unchanged_at_100x(self):
        margin, error = pe.calculate_required_margin("EUR/USD", 1.15921, 0.01, 100, "USD")
        self.assertIsNone(error)
        self.assertAlmostEqual(margin, 11.5921, places=4)

    def test_case_c_returns_none_and_error_code(self):
        margin, error = pe.calculate_required_margin("GBP/USD", 1.35, 0.01, 100, "EUR")
        self.assertIsNone(margin)
        self.assertIsNotNone(error)

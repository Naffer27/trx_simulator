# simulator/tests/test_o6c1ae_forex_provider_symbol_and_failover.py
"""
O.6c-1ae — FOREX LIVE PROVIDER SYMBOL MAPPING + FAILOVER FIX.

Root cause demonstrated in O.6c-1ab-2: EUR/USD's Finnhub WS symbol was
"FX:EURUSD", which Finnhub's realtime WS trade stream rejects
({"msg":"Invalid symbol FX:EURUSD","type":"error"} — verified live
against the account's real FINNHUB_API_KEY). _finnhub_loop() silently
dropped any non-"trade" message (`if msg.get("type") != "trade":
continue`), so that error was swallowed forever: no exception, no
_broadcast(), no fallback to _sim_loop, EUR/USD's feed task parked
indefinitely with zero ticks — order:new stuck on price_unavailable
regardless of how long the chart stayed open.

Two independent fixes, both covered here:

  1. symbol_specs.py — finnhub_symbol corrected to the verified-live
     "OANDA:<BASE>_<QUOTE>" form for all 7 forex majors currently
     registered (EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF,
     NZD/USD). Every value was independently confirmed by connecting to
     wss://ws.finnhub.io with the real FINNHUB_API_KEY and observing an
     actual "trade" message — never assumed from documentation/comments
     (see O.6c-1ae's pre-commit report for the raw transcript).

  2. feeds.py::_finnhub_loop() — a `type=="error"` message now logs and
     raises, reusing the SAME consecutive_failures/MAX_FAILURES/
     on_terminal_failure machinery that already governs real connection
     failures, so a permanently-invalid symbol/auth/rate-limit error
     gives up within a bounded number of attempts and lets
     _try_live_legacy() fall through to _feed_loop()'s existing sim
     fallback — never a zombie task, never a fabricated price outside
     the normal pipeline.

Nothing about get_validated_quote()/_validate_quote_values() (O.6c-1w),
_raw_exec_price()/spread fee (O.6c-1aa), or the position-feed
reconciliation lifecycle (O.6c-1v/O.6c-1ac) is touched by this block —
see test_o6c1ac_feed_reconciliation_recovery.py and
test_o6c1w_price_integrity_gate.py, run unmodified in the same suite,
for that non-regression coverage.
"""
import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from django.test import SimpleTestCase, TransactionTestCase

from market_data.contracts import OrderPolicy
from market_data.feeds import FeedManager, _binance_sym, _finnhub_sym
from market_data.symbol_specs import get_spec
from simulator.models import Position

from .factories import make_account
from .test_o6c1aa_unified_raw_execution_spread_fee import _clear_symbol, _seed_raw
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run
from .test_router_failure_feedback import make_connect_mock

FINNHUB_ERROR_INVALID_SYMBOL = json.dumps(
    {"msg": "Invalid symbol FX:EURUSD", "type": "error"}
)
FINNHUB_ERROR_RATE_LIMIT = json.dumps(
    {"msg": "API limit reached. Please try again later. Remaining Limit: 0", "type": "error"}
)
# EUR/USD-scale trade (base_price=1.17) — the shared FINNHUB_TICK fixture
# from test_router_failure_feedback.py is BTC-scale (p=82000.3) and would
# be correctly rejected by get_validated_quote()'s Capa A plausibility gate
# (O.6c-1w) if used against EUR/USD's registry base_price.
FINNHUB_TICK_EURUSD = json.dumps(
    {"type": "trade", "data": [{"p": 1.17050, "t": 1700000000000, "v": 0.01}]}
)


# ─────────────────────────────────────────────────────────────────────────
# A — symbol_specs.py mapping, verified live against the real Finnhub key
# ─────────────────────────────────────────────────────────────────────────
class ForexFinnhubMappingTests(SimpleTestCase):
    """Every expected value below was independently confirmed live (see
    O.6c-1ae's pre-commit report) — a real "trade" message was received
    for each OANDA:<BASE>_<QUOTE> symbol using the account's real
    FINNHUB_API_KEY. This test only guards the registry against silently
    drifting back to the old, Finnhub-rejected "FX:" form — it does not
    re-hit the network itself."""

    EXPECTED = {
        "EUR/USD": "OANDA:EUR_USD",
        "GBP/USD": "OANDA:GBP_USD",
        "USD/JPY": "OANDA:USD_JPY",
        "AUD/USD": "OANDA:AUD_USD",
        "USD/CAD": "OANDA:USD_CAD",
        "USD/CHF": "OANDA:USD_CHF",
        "NZD/USD": "OANDA:NZD_USD",
    }

    def test_registry_values(self):
        for symbol, expected in self.EXPECTED.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(get_spec(symbol).finnhub_symbol, expected)

    def test_finnhub_sym_helper_matches_registry(self):
        for symbol, expected in self.EXPECTED.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(_finnhub_sym(symbol), expected)

    def test_no_forex_spec_still_uses_the_rejected_fx_prefix(self):
        for symbol in self.EXPECTED:
            with self.subTest(symbol=symbol):
                self.assertFalse(get_spec(symbol).finnhub_symbol.startswith("FX:"))


# ─────────────────────────────────────────────────────────────────────────
# B — _finnhub_loop() no longer swallows type=="error" silently
# ─────────────────────────────────────────────────────────────────────────
class FinnhubProtocolErrorHandlingTests(SimpleTestCase):
    def setUp(self):
        self.fm = FeedManager()
        self.channel_layer = MagicMock()
        self.channel_layer.group_send = AsyncMock()
        self._write_cache_patch = patch("market_data.feeds._write_price_cache", new=AsyncMock())
        self._write_cache_patch.start()
        self.addCleanup(self._write_cache_patch.stop)
        self._sleep_patch = patch("market_data.feeds.asyncio.sleep", new=AsyncMock())
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)
        self._key_patch = patch("market_data.feeds.FINNHUB_API_KEY", "fake-key-for-test")
        self._key_patch.start()
        self.addCleanup(self._key_patch.stop)

    def test_invalid_symbol_error_raises_instead_of_hanging_forever(self):
        """(1) a type=="error" message must break the `async for raw in
        ws` wait and propagate as an exception — never a silent continue
        that parks the loop forever."""
        connect_mock = make_connect_mock([
            [FINNHUB_ERROR_INVALID_SYMBOL],
            [FINNHUB_ERROR_INVALID_SYMBOL],
            [FINNHUB_ERROR_INVALID_SYMBOL],
        ])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(RuntimeError):
                _run(self.fm._finnhub_loop("EUR/USD", self.channel_layer))
        # (2) bounded: exactly MAX_FAILURES=3 connection attempts, never
        # an unbounded retry and never a task stuck mid-attempt.
        self.assertEqual(connect_mock.call_count, 3)

    def test_error_is_logged_not_ignored(self):
        connect_mock = make_connect_mock([[FINNHUB_ERROR_INVALID_SYMBOL]] * 3)
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertLogs("simulator.ws", level="ERROR") as cm:
                with self.assertRaises(RuntimeError):
                    _run(self.fm._finnhub_loop("EUR/USD", self.channel_layer))
        self.assertIn("Invalid symbol FX:EURUSD", "\n".join(cm.output))

    def test_auth_or_rate_limit_error_also_raises_not_ignored(self):
        """(10) any protocol-level error type — not just invalid-symbol —
        must be handled the same way, never silently ignored."""
        connect_mock = make_connect_mock([[FINNHUB_ERROR_RATE_LIMIT]] * 3)
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(RuntimeError) as ctx:
                _run(self.fm._finnhub_loop("EUR/USD", self.channel_layer))
        self.assertIn("API limit reached", str(ctx.exception))

    def test_on_terminal_failure_hook_fires_for_protocol_errors_too(self):
        on_terminal_failure = MagicMock()
        connect_mock = make_connect_mock([[FINNHUB_ERROR_INVALID_SYMBOL]] * 3)
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(RuntimeError):
                _run(self.fm._finnhub_loop(
                    "EUR/USD", self.channel_layer, on_terminal_failure=on_terminal_failure,
                ))
        on_terminal_failure.assert_called_once()

    def test_valid_trade_still_broadcasts_after_error_handling_change(self):
        """(3)(4)(5) a real "trade" message (what the corrected
        OANDA:EUR_USD mapping now produces in production) still reaches
        _broadcast(): in-memory state updates, the Redis write (mocked
        here) fires, and get_validated_quote() returns a fresh Quote —
        none of that is affected by the new error branch above."""
        connect_mock = make_connect_mock([[FINNHUB_TICK_EURUSD, asyncio.CancelledError()]])
        with patch("market_data.feeds.websockets.connect", connect_mock):
            with self.assertRaises(asyncio.CancelledError):
                _run(self.fm._finnhub_loop("EUR/USD", self.channel_layer))
        self.channel_layer.group_send.assert_awaited()
        quote = self.fm.get_validated_quote("EUR/USD")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, "finnhub")


# ─────────────────────────────────────────────────────────────────────────
# C — B2-FOREX-PROVIDER-CLEANUP-01: Finnhub is no longer a Forex runtime
#     fallback for any of the 4 active pairs — removed. Massive is the
#     sole runtime provider; a Massive failure for EUR/USD now goes
#     straight to "unpriced" (get_validated_quote() fail-closed), never a
#     Finnhub hand-off. Coverage of that dispatch behavior now lives in
#     simulator/tests/test_fix05b3_massive_forex_live.py
#     (DispatchPriorityTests.test_massive_failure_never_falls_through_to_
#     finnhub). _finnhub_loop()'s own protocol-error handling (raises
#     instead of hanging, logs, on_terminal_failure fires) remains real,
#     working code — still covered above by
#     FinnhubProtocolErrorHandlingTests, which tests the function
#     directly rather than its (now removed) role in Forex dispatch.
# ─────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────
# D — order:new works end-to-end once the quote is valid (regression guard)
# ─────────────────────────────────────────────────────────────────────────
class OrderNewWithValidEurUsdQuoteTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_order_new_succeeds_once_feed_has_a_validated_quote(self):
        """(6) end-to-end: once the feed cache holds a fresh, valid
        EUR/USD quote — exactly what the corrected OANDA:EUR_USD mapping
        now produces in production via _broadcast() — order:new opens
        normally instead of price_unavailable."""
        consumer = _consumer(self.account.pk)
        _seed_raw("EUR/USD", 1.17000, 1.17020)
        # TEST-INFRA — market-session policy is real-clock-driven
        # (FIX-05A); this test is a provider-failover regression guard,
        # not market-session behavior, so pin it open.
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=Mock(order_policy=OrderPolicy.OPEN_NORMAL),
        ):
            _run(consumer._order_new({"symbol": "EUR/USD", "side": "buy", "qty": 0.01}))
        self.assertIsNone(_first_error(consumer))
        self.assertTrue(Position.objects.filter(account=self.account, symbol="EUR/USD").exists())


# ─────────────────────────────────────────────────────────────────────────
# E — crypto routing / BTCUSD / ETHUSD untouched by this block
# ─────────────────────────────────────────────────────────────────────────
class CryptoRoutingUnaffectedTests(SimpleTestCase):
    """(11) this block only touches forex finnhub_symbol values and
    _finnhub_loop()'s error handling — BTCUSD/ETHUSD never reached Finnhub
    before this change and still don't. What DID change, in
    GOLDEN-MARKETDATA-CRYPTO-01: Massive is now checked BEFORE Binance in
    _try_live_legacy for these 2 symbols, making Binance/Kraken
    functionally unreachable for them — see
    test_golden_marketdata_crypto_01_massive_crypto_live.py for the full
    Massive-crypto dispatch/shared-connection coverage. This class keeps
    asserting the dormant Binance/Kraken metadata (symbol_specs.py's
    exchange_symbol/kraken_symbol) is unchanged — GOLDEN-MARKETDATA-
    CRYPTO-01 §I classifies it "KEEP AS DORMANT", not removed — plus a
    direct check that _try_live_legacy no longer reaches Binance for
    them."""

    def test_btcusd_binance_kraken_metadata_kept_dormant(self):
        self.assertEqual(_binance_sym("BTCUSD"), "BTCUSDT")
        self.assertIsNone(get_spec("BTCUSD").finnhub_symbol)

    def test_ethusd_binance_kraken_metadata_kept_dormant(self):
        self.assertEqual(_binance_sym("ETHUSD"), "ETHUSDT")
        self.assertIsNone(get_spec("ETHUSD").finnhub_symbol)

    def test_btcusd_never_reaches_binance_loop_at_runtime(self):
        async def _scenario():
            fm = FeedManager()
            with patch("market_data.feeds.MASSIVE_API_KEY", "FAKEtestONLYnotREALkey1234567xx"), \
                 patch.object(fm, "_massive_crypto_loop", new=AsyncMock(return_value=None)), \
                 patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance:
                await fm._try_live_legacy("BTCUSD", MagicMock())
                mock_binance.assert_not_called()
        _run(_scenario())

    def test_ethusd_never_reaches_binance_loop_at_runtime(self):
        async def _scenario():
            fm = FeedManager()
            with patch("market_data.feeds.MASSIVE_API_KEY", "FAKEtestONLYnotREALkey1234567xx"), \
                 patch.object(fm, "_massive_crypto_loop", new=AsyncMock(return_value=None)), \
                 patch.object(fm, "_binance_loop", new=AsyncMock()) as mock_binance:
                await fm._try_live_legacy("ETHUSD", MagicMock())
                mock_binance.assert_not_called()
        _run(_scenario())

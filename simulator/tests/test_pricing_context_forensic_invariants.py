"""
simulator/tests/test_pricing_context_forensic_invariants.py — SPREAD-02b,
updated SPREAD-03 FASE A.

Two forensic invariants requested after review of the first SPREAD-02 pass:

INVARIANTE 1 — snapshot exacto del tick. base_spread_pips/account_markup_pips/
effective_spread_pips must reflect the BrokerSpreadConfig that was active
WHEN price_tick() computed the executable price actually used for the fill
— never a fresh re-read of BrokerSpreadConfig at order time, which could
have changed in between. _capture_pricing_context() must read the frozen
per-tick snapshot (self._pricing_snapshot_state), never re-query.

INVARIANTE 2 — daemon honesto. The Celery daemon never calls broker_price()
— it uses the raw cached price directly. Its persisted context must say so
explicitly: executable == raw, base/account/effective spread == 0.0 (not a
BrokerSpreadConfig read), even when a BrokerSpreadConfig row exists.

UPDATE (SPREAD-03 FASE A): verifying invariant 1 originally surfaced a
THIRD, independent bug — price_tick() calling broker_price() (and this
block's own tick_pricing_snapshot()) synchronously from an async method
raised Django's SynchronousOnlyOperation on every DB read, silently
swallowed, always returning passthrough. SPREAD-03 FASE A fixed this at
the root: BrokerSpreadConfig is now read from
simulator.spread_config_cache, a process-wide, async-safe, explicitly-
refreshed in-memory cache (see simulator/spread_config_cache.py and
simulator/tests/test_spread_config_cache.py for the fix itself and its
dedicated tests). Because of that fix, the invariant-1 tests below now
exercise the REAL price_tick() coroutine directly (previously they had to
call tick_pricing_snapshot()/broker_price() synchronously to work around
the bug) — this file's AsyncSafeBrokerSpreadConfigReadTests class replaces
the old "documents the bug" class with tests proving the fix.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TestCase

from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer
from simulator.models import BrokerSpreadConfig, Position, Trade
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests
from simulator.spread_engine import broker_price
from simulator.tasks import _daemon_pricing_context, scan_positions_task
from simulator import pricing_context as pc

from .factories import make_account, make_spread_config


def _run(coro):
    return asyncio.run(coro)


def _bare_consumer() -> TradingConsumer:
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = 1
    c.symbol = "EUR/USD"
    c._price_state = {}
    c._bid_state = {}
    c._ask_state = {}
    c._raw_bid_state = {}
    c._raw_ask_state = {}
    c._pricing_ts_state = {}
    c._pricing_snapshot_state = {}
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.account = {"balance": 10000.0, "spread_pips": 0.0}
    c.send_json = AsyncMock()
    c._on_tick = AsyncMock()
    c._check_tp_sl = AsyncMock()
    c._recalc_account_and_push = AsyncMock()
    return c


def _tick(bid: float, ask: float, ts: int = 1_700_000_000) -> dict:
    mid = round((bid + ask) / 2, 5)
    return {"symbol": "EUR/USD", "bid": bid, "ask": ask, "mid": mid, "time": ts}


class TickSnapshotIsExactInvariantTests(TestCase):
    """INVARIANTE 1 — exercised through the REAL async price_tick()
    coroutine now that SPREAD-03 FASE A makes BrokerSpreadConfig reads
    async-safe. The cache is warmed explicitly via refresh_cache_sync()
    (sync, safe outside the event loop) before each tick — exactly what
    ensure_background_refresh_started() does in production at connect()."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_config_change_after_tick_does_not_alter_captured_context(self):
        """1) tick llega con config X. 2) se calculan executable prices.
        3) BrokerSpreadConfig cambia a Y antes de abrir. 4) la orden debe
        guardar X, porque X produjo el precio ejecutado."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()
        c = _bare_consumer()

        # Step 1 & 2 — tick arrives under config X=2.00; price_tick() computes
        # and freezes both the executable price and the snapshot together.
        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))
        self.assertEqual(c._pricing_snapshot_state["EUR/USD"]["base_spread_pips"], 2.0)
        executable_bid_at_tick = c._bid_state["EUR/USD"]
        executable_ask_at_tick = c._ask_state["EUR/USD"]

        # Step 3 — config changes to Y=9.00 (e.g. an admin edit), and the
        # cache is refreshed (simulating the next periodic refresh cycle)
        # BEFORE the order — this must still not leak into the captured context.
        BrokerSpreadConfig.objects.filter(symbol="EUR/USD").update(spread_pips=Decimal("9.00"))
        refresh_cache_sync()
        self.assertEqual(pc.spread_pips_for("EUR/USD", 0.0)[0], 9.0)  # cache really sees Y now

        # Step 4 — capture at "order time": must still show X, not Y.
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["base_spread_pips"], 2.0)
        self.assertNotEqual(ctx["base_spread_pips"], 9.0)
        self.assertEqual(ctx["effective_spread_pips"], 2.0)
        self.assertEqual(ctx["executable_bid"], executable_bid_at_tick)
        self.assertEqual(ctx["executable_ask"], executable_ask_at_tick)

    def test_executable_price_is_mathematically_coherent_with_effective_pips(self):
        """executable_bid = raw_bid - effective_pips*pip_size/2 (mismo redondeo
        que broker_price()); simétrico para ask."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("1.50"), enabled=True)
        refresh_cache_sync()
        c = _bare_consumer()
        c.account["spread_pips"] = 0.50  # account markup on top of the 1.50 base

        raw_bid, raw_ask = 1.09990, 1.10010
        _run(c.price_tick(_tick(bid=raw_bid, ask=raw_ask)))

        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        spec = get_spec("EUR/USD")
        effective_pips = ctx["effective_spread_pips"]
        self.assertEqual(effective_pips, 2.0)  # 1.50 base + 0.50 markup

        extra = effective_pips * spec.pip_size / 2
        expected_bid = round(raw_bid - extra, spec.price_decimals)
        expected_ask = round(raw_ask + extra, spec.price_decimals)

        self.assertEqual(ctx["executable_bid"], expected_bid)
        self.assertEqual(ctx["executable_ask"], expected_ask)
        self.assertEqual(ctx["executable_bid"], c._bid_state["EUR/USD"])
        self.assertEqual(ctx["executable_ask"], c._ask_state["EUR/USD"])

    def test_capture_never_re_reads_broker_spread_config(self):
        """Structural guarantee: _capture_pricing_context must not touch
        spread_engine at all — only tick_pricing_snapshot (inside
        price_tick) is allowed to."""
        from unittest.mock import patch
        c = _bare_consumer()
        _run(c.price_tick(_tick(bid=1.0999, ask=1.1001)))
        with patch("simulator.pricing_context.spread_pips_for") as mock_read:
            c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_CLOSE)
        mock_read.assert_not_called()

    def test_price_tick_stores_snapshot_alongside_raw_and_executable(self):
        c = _bare_consumer()
        c.account["spread_pips"] = 0.75
        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))
        snapshot = c._pricing_snapshot_state["EUR/USD"]
        self.assertEqual(snapshot["account_markup_pips"], 0.75)
        self.assertEqual(c._raw_bid_state["EUR/USD"], 1.09990)
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["effective_spread_pips"], 0.75)  # no config row → base=None → 0 + markup


class DaemonHonestPricingInvariantTests(TestCase):
    """INVARIANTE 2 — unaffected by SPREAD-03 FASE A: the daemon path never
    called spread_pips_for()/BrokerSpreadConfig at all (SPREAD-02b fix),
    so it has nothing to do with the async-safe cache."""

    def test_daemon_context_has_zeroed_spread_even_with_config_present(self):
        """A BrokerSpreadConfig row exists with a real value — the daemon
        context must still report 0.0, because that spread never
        participated in this fill."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("5.00"), enabled=True)
        account = make_account(balance=Decimal("10000"))
        account.spread_pips_snapshot = Decimal("1.25")
        account.save(update_fields=["spread_pips_snapshot"])

        ctx = _daemon_pricing_context("EUR/USD", 1.09990, 1.10010, profile=pc.PROFILE_DAEMON_TP)

        self.assertEqual(ctx["raw_bid"], ctx["executable_bid"])
        self.assertEqual(ctx["raw_ask"], ctx["executable_ask"])
        self.assertEqual(ctx["base_spread_pips"], 0.0)
        self.assertEqual(ctx["account_markup_pips"], 0.0)
        self.assertEqual(ctx["effective_spread_pips"], 0.0)
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_DAEMON_TP)

    def test_no_broker_spread_config_read_at_all(self):
        """Structural guarantee: the daemon path must not call
        spread_pips_for()/BrokerSpreadConfig at all — not just report zero."""
        from unittest.mock import patch
        with patch("simulator.pricing_context.spread_pips_for") as mock_read:
            _daemon_pricing_context("EUR/USD", 1.1, 1.1002, profile=pc.PROFILE_DAEMON_SL)
        mock_read.assert_not_called()

    def test_end_to_end_daemon_close_persists_zeroed_context(self):
        """Full scan_positions_task run: a real config exists, a real TP
        fires, and the persisted Trade.pricing_context_close still shows
        zeroed spread fields, proving the daemon fill was honestly
        unmarked-up end to end."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("5.00"), enabled=True)
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"), tp=Decimal("1.10500"),
        )

        from unittest.mock import patch

        def _price_mock(symbol):
            return (1.10600, 1.10620) if symbol == "EUR/USD" else (None, None)

        with patch("simulator.tasks._read_cached_price", side_effect=_price_mock):
            result = scan_positions_task.apply().get()

        self.assertEqual(result["closed"], 1)
        trade = Trade.objects.get(account=account, symbol="EUR/USD")
        close_ctx = trade.pricing_context_close
        self.assertEqual(close_ctx["raw_bid"], close_ctx["executable_bid"])
        self.assertEqual(close_ctx["base_spread_pips"], 0.0)
        self.assertEqual(close_ctx["account_markup_pips"], 0.0)
        self.assertEqual(close_ctx["effective_spread_pips"], 0.0)
        self.assertEqual(close_ctx["pricing_profile"], pc.PROFILE_DAEMON_TP)


class AsyncSafeBrokerSpreadConfigReadTests(TestCase):
    """SPREAD-03 FASE A fix, proven directly: broker_price() and
    tick_pricing_snapshot(), called from a REAL running asyncio event loop
    (asyncio.run, exactly how Daphne runs TradingConsumer coroutines), now
    correctly apply/observe BrokerSpreadConfig — once the cache has been
    warmed via refresh_cache_sync() (sync, called outside the loop here;
    in production, via ensure_background_refresh_started() at connect()).
    No DJANGO_ALLOW_ASYNC_UNSAFE, no ORM call inside the coroutine."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_broker_price_applies_broker_spread_config_from_async_context(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()

        async def _call():
            return broker_price("EUR/USD", 1.09990, 1.10010, markup_pips=0.0)

        bid, ask = _run(_call())
        # extra = 2.00 * 0.0001 / 2 = 0.0001 per side — no longer passthrough.
        self.assertAlmostEqual(bid, 1.09990 - 0.0001, places=5)
        self.assertAlmostEqual(ask, 1.10010 + 0.0001, places=5)

    def test_tick_pricing_snapshot_sees_the_same_warmed_config(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()

        async def _call():
            return pc.tick_pricing_snapshot("EUR/USD", 0.0)

        snapshot = _run(_call())
        self.assertEqual(snapshot["base_spread_pips"], 2.0)

    def test_cold_cache_before_any_refresh_is_a_safe_passthrough_not_an_exception(self):
        """Before the first refresh ever runs (e.g. the very first tick of
        a freshly-started process, before connect()'s warm-up completes),
        reads must degrade safely — never raise, never block."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        # Deliberately do NOT call refresh_cache_sync() — cold cache.

        async def _call():
            return broker_price("EUR/USD", 1.09990, 1.10010, markup_pips=0.0)

        bid, ask = _run(_call())
        self.assertEqual(bid, 1.09990)
        self.assertEqual(ask, 1.10010)

    def test_zero_orm_calls_inside_the_event_loop(self):
        """Structural guarantee: broker_price(), called from within a
        running event loop, must not touch the DB at all — not even a
        failed/caught attempt."""
        from unittest.mock import patch
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        refresh_cache_sync()

        async def _call():
            with patch("simulator.models.BrokerSpreadConfig.objects") as mock_manager:
                result = broker_price("EUR/USD", 1.09990, 1.10010, markup_pips=0.0)
                mock_manager.filter.assert_not_called()
                return result

        bid, ask = _run(_call())
        self.assertAlmostEqual(bid, 1.09990 - 0.0001, places=5)

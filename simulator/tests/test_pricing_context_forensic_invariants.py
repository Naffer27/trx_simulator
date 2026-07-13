"""
simulator/tests/test_pricing_context_forensic_invariants.py — SPREAD-02b.

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

A THIRD finding surfaced while verifying invariant 1 (documented, NOT
fixed — fixing it would require touching broker_price()/spread_engine.py,
explicitly out of scope for this block): price_tick() calls broker_price()
— and, necessarily, this block's own tick_pricing_snapshot() — directly
(synchronously) from an async method. Django raises SynchronousOnlyOperation
for ORM access done that way (DJANGO_ALLOW_ASYNC_UNSAFE is not set
anywhere in this project), and spread_engine._get_config()'s broad
`except Exception` swallows it, always returning cfg=None. In practice
this means the BrokerSpreadConfig base spread has likely never actually
applied via the live WebSocket price_tick() path — only account-level
markup (a plain dict read, no DB) reliably applies there. See
PreExistingAsyncUnsafeBrokerSpreadConfigReadTests below: this is a
pre-existing, independent bug this block surfaces but does not fix.
Because of it, this block's own captured base_spread_pips will correctly
read None from the live async path today — an honest reflection of what
actually happened to the executed price, not a defect in the capture
mechanism itself. Tests below that need a *working* BrokerSpreadConfig
read call tick_pricing_snapshot()/broker_price() synchronously (no event
loop) — exactly matching how price_tick() would behave if that separate
bug were ever fixed — to validate the snapshot-freeze mechanism on its own
terms, isolated from that unrelated defect.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from django.test import TestCase

from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer
from simulator.models import BrokerSpreadConfig, Position, Trade
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
    """INVARIANTE 1.

    Snapshot capture is exercised synchronously (calling
    tick_pricing_snapshot()/broker_price() directly, not through
    price_tick()'s coroutine) — this is deliberate: it isolates "does the
    freeze-at-tick mechanism work" from the separate, pre-existing
    async-unsafe DB read bug documented at module level and in
    PreExistingAsyncUnsafeBrokerSpreadConfigReadTests below. A dedicated
    test further down proves the two mechanisms compose correctly even
    though the underlying async read is currently broken.
    """

    def setUp(self):
        from simulator import spread_engine as _spread_mod
        _spread_mod._cache.clear()

    def tearDown(self):
        from simulator import spread_engine as _spread_mod
        _spread_mod._cache.clear()

    def test_config_change_after_tick_does_not_alter_captured_context(self):
        """1) tick llega con config X. 2) se calculan executable prices.
        3) BrokerSpreadConfig cambia a Y antes de abrir. 4) la orden debe
        guardar X, porque X produjo el precio ejecutado."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        c = _bare_consumer()
        markup_pips = 0.0
        raw_bid, raw_ask = 1.09990, 1.10010

        # Step 1 & 2 — tick arrives under config X=2.00; capture + executable
        # computed together, exactly as price_tick() does internally.
        snapshot_at_tick = pc.tick_pricing_snapshot("EUR/USD", markup_pips)
        bid, ask = broker_price("EUR/USD", raw_bid, raw_ask, markup_pips=markup_pips)
        c._pricing_snapshot_state["EUR/USD"] = snapshot_at_tick
        c._raw_bid_state["EUR/USD"] = raw_bid
        c._raw_ask_state["EUR/USD"] = raw_ask
        c._bid_state["EUR/USD"] = bid
        c._ask_state["EUR/USD"] = ask
        self.assertEqual(snapshot_at_tick["base_spread_pips"], 2.0)

        # Step 3 — config changes to Y=9.00 (e.g. an admin edit) before the order.
        BrokerSpreadConfig.objects.filter(symbol="EUR/USD").update(spread_pips=Decimal("9.00"))
        from simulator import spread_engine as _spread_mod
        _spread_mod._cache.clear()  # force the next live read (if any) to see Y

        # Step 4 — capture at "order time": must still show X, not Y.
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["base_spread_pips"], 2.0)
        self.assertNotEqual(ctx["base_spread_pips"], 9.0)
        self.assertEqual(ctx["effective_spread_pips"], 2.0)
        self.assertEqual(ctx["executable_bid"], bid)
        self.assertEqual(ctx["executable_ask"], ask)

    def test_executable_price_is_mathematically_coherent_with_effective_pips(self):
        """executable_bid = raw_bid - effective_pips*pip_size/2 (mismo redondeo
        que broker_price()); simétrico para ask."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("1.50"), enabled=True)
        c = _bare_consumer()
        markup_pips = 0.50  # account markup on top of the 1.50 base
        raw_bid, raw_ask = 1.09990, 1.10010

        snapshot = pc.tick_pricing_snapshot("EUR/USD", markup_pips)
        bid, ask = broker_price("EUR/USD", raw_bid, raw_ask, markup_pips=markup_pips)
        c._pricing_snapshot_state["EUR/USD"] = snapshot
        c._raw_bid_state["EUR/USD"] = raw_bid
        c._raw_ask_state["EUR/USD"] = raw_ask
        c._bid_state["EUR/USD"] = bid
        c._ask_state["EUR/USD"] = ask

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
        c._pricing_snapshot_state["EUR/USD"] = pc.tick_pricing_snapshot("EUR/USD", 0.0)
        c._raw_bid_state["EUR/USD"] = 1.0999
        c._raw_ask_state["EUR/USD"] = 1.1001
        with patch("simulator.pricing_context.spread_pips_for") as mock_read:
            c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_CLOSE)
        mock_read.assert_not_called()

    def test_price_tick_stores_snapshot_alongside_raw_and_executable(self):
        """End-to-end through the real price_tick() coroutine — account
        markup only (no BrokerSpreadConfig row), which is DB-free and so
        unaffected by the separate async-unsafe issue documented below."""
        c = _bare_consumer()
        c.account["spread_pips"] = 0.75
        _run(c.price_tick(_tick(bid=1.09990, ask=1.10010)))
        snapshot = c._pricing_snapshot_state["EUR/USD"]
        self.assertEqual(snapshot["account_markup_pips"], 0.75)
        self.assertEqual(c._raw_bid_state["EUR/USD"], 1.09990)
        ctx = c._capture_pricing_context("EUR/USD", profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["effective_spread_pips"], 0.75)  # base=None → 0 + markup


class DaemonHonestPricingInvariantTests(TestCase):
    """INVARIANTE 2."""

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


class PreExistingAsyncUnsafeBrokerSpreadConfigReadTests(TestCase):
    """Discovered while verifying invariant 1 — pre-existing, independent
    of SPREAD-02, NOT fixed here (would require modifying broker_price()/
    spread_engine.py, explicitly out of scope for this block). Documented
    so the finding is not silently lost; see module docstring.
    """

    def setUp(self):
        from simulator import spread_engine as _spread_mod
        _spread_mod._cache.clear()

    def tearDown(self):
        from simulator import spread_engine as _spread_mod
        _spread_mod._cache.clear()

    def test_broker_price_silently_ignores_broker_spread_config_from_async_context(self):
        """Pre-existing bug, reproduced with pre-existing, untouched code
        (spread_engine.broker_price) — not something this block introduced."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)

        async def _call():
            return broker_price("EUR/USD", 1.09990, 1.10010, markup_pips=0.0)

        bid, ask = _run(_call())
        # Passthrough — the 2.00-pip config never actually applied, because
        # the DB read inside it raised SynchronousOnlyOperation and was
        # silently swallowed by _get_config()'s broad except Exception.
        self.assertEqual(bid, 1.09990)
        self.assertEqual(ask, 1.10010)

    def test_tick_pricing_snapshot_matches_that_same_swallowed_read(self):
        """This block's own capture stays coherent with the ACTUAL (buggy)
        executable price it did not create — not a corrected value. Fixing
        the read in isolation (without fixing broker_price() itself) would
        make the captured base_spread_pips describe a markup that was
        never really applied to executable_bid/executable_ask — which
        would break, not satisfy, the mathematical-coherence requirement."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)

        async def _call():
            return pc.tick_pricing_snapshot("EUR/USD", 0.0)

        snapshot = _run(_call())
        self.assertIsNone(snapshot["base_spread_pips"])

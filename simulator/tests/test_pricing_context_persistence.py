"""
simulator/tests/test_pricing_context_persistence.py — SPREAD-02.

Covers the DB-layer integrity guarantees for pricing-context persistence:
  - Position.pricing_context captured at open (manual WS open).
  - Trade.pricing_context_open copied verbatim from Position at close
    (manual WS close, and the sync daemon path) — never recomputed.
  - A BrokerSpreadConfig change between open and close cannot alter the
    already-captured open context.
  - Netting merges do not overwrite the original position's context.
  - A broken pricing-context capture never blocks the open/close itself.
  - Historical/uncovered rows (pricing_context=None) read back cleanly.

Uses the same direct-unwrap harness as test_netting_merge_deadlock_guard.py
(TradingConsumer._db_open_position_atomic.__wrapped__ / .../_db_close_...)
so no Channels/WebSocket infrastructure is needed to exercise the exact
production code path.
"""
from decimal import Decimal

from django.test import TestCase

from simulator.consumers import TradingConsumer
from simulator.models import Position, Trade, TradingAccount
from simulator.tasks import _close_position_sync
from simulator import pricing_context as pc

from .factories import make_account, make_position, make_spread_config

_db_open_sync  = TradingConsumer._db_open_position_atomic.__wrapped__
_db_close_sync = TradingConsumer._db_close_position_atomic.__wrapped__


class _FakeConsumer:
    """Minimal consumer stub — only the attributes _db_open/_close_position_atomic touch."""
    def __init__(self, account_id, netting_mode=False, spread_pips=0.0):
        self._db_account_id = account_id
        self.account = {"netting_mode": netting_mode, "spread_pips": spread_pips}


def _pos_mem(pos) -> dict:
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": float(pos.sl) if pos.sl is not None else None,
        "tp": float(pos.tp) if pos.tp is not None else None,
        "opened_at": pos.opened_at.timestamp(),
    }


class OpenCapturesPricingContextTests(TestCase):
    def test_position_stores_the_passed_context(self):
        account = make_account(balance=Decimal("10000"))
        consumer = _FakeConsumer(account.pk)
        ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001, executable_bid=1.0997, executable_ask=1.1003,
            base_spread_pips=1.0, account_markup_pips=0.5, pricing_profile=pc.PROFILE_WS_OPEN,
        )
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=10000.0, pricing_context=ctx,
        )
        pos = Position.objects.get(pk=result["position_id"])
        self.assertEqual(pos.pricing_context["effective_spread_pips"], 1.5)
        self.assertEqual(pos.pricing_context["pricing_profile"], pc.PROFILE_WS_OPEN)

    def test_omitting_context_leaves_it_null(self):
        account = make_account(balance=Decimal("10000"))
        consumer = _FakeConsumer(account.pk)
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=10000.0,
        )
        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNone(pos.pricing_context)

    def test_netting_merge_does_not_overwrite_original_context(self):
        account = make_account(balance=Decimal("10000"))
        consumer = _FakeConsumer(account.pk, netting_mode=True)
        first_ctx = pc.build_pricing_context(raw_bid=1.0999, raw_ask=1.1001, pricing_profile="first")
        result1 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=10000.0, pricing_context=first_ctx,
        )
        second_ctx = pc.build_pricing_context(raw_bid=1.2, raw_ask=1.2002, pricing_profile="second")
        result2 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.20000, None, None,
            commission=0.0, new_balance=10000.0, pricing_context=second_ctx,
        )
        self.assertEqual(result1["position_id"], result2["position_id"])  # merged into same row
        pos = Position.objects.get(pk=result2["position_id"])
        self.assertEqual(pos.pricing_context["pricing_profile"], "first")


class CloseCopiesOpenContextVerbatimTests(TestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.open_ctx = pc.build_pricing_context(
            raw_bid=1.0999, raw_ask=1.1001, executable_bid=1.0997, executable_ask=1.1003,
            base_spread_pips=2.0, account_markup_pips=0.0, pricing_profile=pc.PROFILE_WS_OPEN,
        )
        self.pos = Position.objects.create(
            account=self.account, symbol="EUR/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.10000"),
            pricing_context=self.open_ctx,
        )

    def test_trade_open_context_matches_position_context_exactly(self):
        close_ctx = pc.build_pricing_context(
            raw_bid=1.1099, raw_ask=1.1101, pricing_profile=pc.PROFILE_WS_CLOSE,
        )
        result = _db_close_sync(
            _FakeConsumer(self.account.pk), _pos_mem(self.pos), 1.11000, "manual",
            100.0, 10100.0, 10100.0, pricing_context_close=close_ctx,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.pricing_context_open, self.open_ctx)
        self.assertEqual(trade.pricing_context_close["pricing_profile"], pc.PROFILE_WS_CLOSE)

    def test_broker_spread_config_change_after_open_does_not_alter_trade_open_context(self):
        """The single most important guarantee of this block: base_spread_pips
        captured at open must survive a later admin edit to BrokerSpreadConfig."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"), enabled=True)
        # Simulate an admin changing the config AFTER open, BEFORE close.
        from simulator.models import BrokerSpreadConfig
        BrokerSpreadConfig.objects.filter(symbol="EUR/USD").update(spread_pips=Decimal("9.00"))
        from simulator import spread_engine as _spread_mod
        _spread_mod._cache.clear()  # force a fresh read reflecting the changed config

        close_ctx = pc.build_pricing_context(
            base_spread_pips=9.0, account_markup_pips=0.0, pricing_profile=pc.PROFILE_WS_CLOSE,
        )
        result = _db_close_sync(
            _FakeConsumer(self.account.pk), _pos_mem(self.pos), 1.11000, "manual",
            100.0, 10100.0, 10100.0, pricing_context_close=close_ctx,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        # Open context still shows the value captured at open (2.0), not the
        # post-open edit (9.0) — even though close context (fresh) shows 9.0.
        self.assertEqual(trade.pricing_context_open["base_spread_pips"], 2.0)
        self.assertEqual(trade.pricing_context_close["base_spread_pips"], 9.0)

    def test_omitting_close_context_leaves_it_null_but_open_still_copied(self):
        result = _db_close_sync(
            _FakeConsumer(self.account.pk), _pos_mem(self.pos), 1.11000, "manual",
            100.0, 10100.0, 10100.0,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertIsNone(trade.pricing_context_close)
        self.assertEqual(trade.pricing_context_open, self.open_ctx)

    def test_position_with_no_context_copies_null_to_trade(self):
        pos2 = Position.objects.create(
            account=self.account, symbol="GBP/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.30000"),
        )
        result = _db_close_sync(
            _FakeConsumer(self.account.pk), _pos_mem(pos2), 1.31000, "manual",
            100.0, 10100.0, 10100.0,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertIsNone(trade.pricing_context_open)


class DaemonClosePersistenceTests(TestCase):
    """Same integrity guarantees, via the sync Celery path (_close_position_sync)."""

    def setUp(self):
        self.account = make_account(balance=Decimal("10000"))
        self.open_ctx = pc.build_pricing_context(
            raw_bid=82000.0, raw_ask=82015.0, base_spread_pips=15.0,
            pricing_profile=pc.PROFILE_WS_OPEN,
        )
        self.pos = Position.objects.create(
            account=self.account, symbol="BTCUSD", side="BUY",
            qty=Decimal("0.01"), avg_price=Decimal("82000.00"),
            pricing_context=self.open_ctx,
        )

    def _pos_mem(self):
        return {
            "id": self.pos.pk, "symbol": "BTCUSD", "side": "buy",
            "qty": 0.01, "avg": 82000.0, "sl": None, "tp": None,
            "opened_at": self.pos.opened_at.timestamp(),
        }

    def test_daemon_close_copies_open_context_and_stores_close_context(self):
        close_ctx = pc.build_pricing_context(
            raw_bid=82100.0, raw_ask=82100.0, executable_bid=82100.0, executable_ask=82100.0,
            pricing_profile=pc.PROFILE_DAEMON_STOPOUT,
        )
        result = _close_position_sync(
            self._pos_mem(), self.account.pk, 82100.0, "daemon_stopout",
            1.0, 10001.0, 10001.0, pricing_context=close_ctx,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertEqual(trade.pricing_context_open, self.open_ctx)
        self.assertEqual(trade.pricing_context_close["pricing_profile"], pc.PROFILE_DAEMON_STOPOUT)

    def test_daemon_close_omitting_context_stays_null_but_open_still_copied(self):
        result = _close_position_sync(
            self._pos_mem(), self.account.pk, 82100.0, "daemon_stopout",
            1.0, 10001.0, 10001.0,
        )
        trade = Trade.objects.get(pk=result["trade_id"])
        self.assertIsNone(trade.pricing_context_close)
        self.assertEqual(trade.pricing_context_open, self.open_ctx)


class NeverBlocksOperationTests(TestCase):
    """REGLA E: fallos de observabilidad/provider metadata nunca deben impedir
    una operación — pero precios/spread deben capturarse cuando disponibles."""

    def test_broken_provider_state_read_does_not_block_open(self):
        from unittest.mock import patch
        account = make_account(balance=Decimal("10000"))
        consumer = _FakeConsumer(account.pk)
        with patch("market_data.observability.get_symbol_state", side_effect=RuntimeError("boom")):
            base, markup = pc.spread_pips_for("EUR/USD", 0.0)
            provider_id, source_state = pc.provider_state_for("EUR/USD")
            ctx = pc.build_pricing_context(
                raw_bid=1.0999, raw_ask=1.1001, base_spread_pips=base,
                account_markup_pips=markup, provider_id=provider_id,
                source_state=source_state, pricing_profile=pc.PROFILE_WS_OPEN,
            )
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=10000.0, pricing_context=ctx,
        )
        pos = Position.objects.get(pk=result["position_id"])
        # Operation succeeded; prices were captured; provider metadata is null.
        self.assertEqual(pos.pricing_context["raw_bid"], 1.0999)
        self.assertIsNone(pos.pricing_context["provider_id"])

    def test_totally_broken_context_build_still_lets_open_succeed(self):
        from unittest.mock import patch
        account = make_account(balance=Decimal("10000"))
        consumer = _FakeConsumer(account.pk)
        with patch("simulator.pricing_context.PricingContext", side_effect=RuntimeError("boom")):
            ctx = pc.build_pricing_context(raw_bid=1.1, pricing_profile=pc.PROFILE_WS_OPEN)
        self.assertEqual(ctx["pricing_profile"], pc.PROFILE_CAPTURE_FAILED)
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=10000.0, pricing_context=ctx,
        )
        # Position still created successfully despite a totally broken capture.
        self.assertTrue(Position.objects.filter(pk=result["position_id"]).exists())


class SchemaVersionTests(TestCase):
    def test_persisted_context_carries_schema_version(self):
        account = make_account(balance=Decimal("10000"))
        consumer = _FakeConsumer(account.pk)
        ctx = pc.build_pricing_context(raw_bid=1.1, pricing_profile=pc.PROFILE_WS_OPEN)
        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.10000, None, None,
            commission=0.0, new_balance=10000.0, pricing_context=ctx,
        )
        pos = Position.objects.get(pk=result["position_id"])
        self.assertEqual(pos.pricing_context["schema_version"], pc.SCHEMA_VERSION)


class HistoricalRowsCompatibilityTests(TestCase):
    def test_position_created_without_pricing_context_field_reads_none(self):
        account = make_account(balance=Decimal("10000"))
        pos = make_position(account=account, symbol="EUR/USD")
        pos.refresh_from_db()
        self.assertIsNone(pos.pricing_context)

    def test_trade_created_without_pricing_context_fields_reads_none(self):
        from .factories import make_trade
        account = make_account(balance=Decimal("10000"))
        trade = make_trade(account=account)
        trade.refresh_from_db()
        self.assertIsNone(trade.pricing_context_open)
        self.assertIsNone(trade.pricing_context_close)

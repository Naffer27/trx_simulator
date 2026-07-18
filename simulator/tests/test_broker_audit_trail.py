"""
simulator/tests/test_broker_audit_trail.py — AUDIT-01 (post-review
correction).

Broker Event Audit Trail Foundation (simulator/broker_audit.py) — the
platform's institutional, cross-engine chronological event record.
Not a logger, not a log file: every recorded event is a durable,
queryable BrokerAuditEvent row.

Coverage layers:
  - ModelTests: BrokerAuditEvent fields, event_id uniqueness, ordering.
  - EngineTests: record_event() and its four category wrappers, never
    raises on a DB failure, writes exactly one row per call.
  - CloseReasonMappingTests: close_reason_event_type().
  - AlertIntegrationTests: record_alert_event/record_active_alerts —
    severity filter, dedup window, future-timestamp safety.
  - QueryTests: every FASE 7 query function — filtering, limit,
    chronological order.
  - IntegrationTests: position open (consumers.py), position close
    (broker_ledger.py, all four real writers), RISK-02 rejection
    (consumers.py) — plus confirming the admin dashboard is read-only.
  - ServiceLayerTests (Correction 1): observe_broker_alerts() is the
    only sanctioned write path for alert observations; the dashboard
    never writes; concurrent/atomic dedup under genuine thread
    contention.
  - AdminForceCloseActorTests (Correction 2): the real staff actor is
    preserved via a distinct EV_ADMIN_POSITION_FORCE_CLOSE event,
    without duplicating the single canonical financial event.
  - AppendOnlyProtectionTests (Correction 3): FK SET_NULL on delete,
    and the admin registration truly forbids add/change/delete.
  - NoRegressionTests: BOOK-02/BOOK-03/RISK-01/RISK-02/RISK-03 unaffected.
"""
import random
import threading
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.utils import OperationalError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from market_data.feeds import get_feed_manager

import simulator.broker_audit as ba
import simulator.broker_risk as br
from simulator.broker_audit import (
    Category, Severity, ActorType,
    record_event, record_trade_event, record_risk_event,
    record_admin_event, record_system_event,
    record_alert_event, record_active_alerts, observe_broker_alerts,
    close_reason_event_type,
    recent_events, events_for_account, events_for_trade,
    events_for_symbol, events_by_category, events_by_severity,
    EV_POSITION_OPENED, EV_POSITION_CLOSED, EV_POSITION_CLOSED_MANUAL,
    EV_POSITION_CLOSED_STOP_LOSS, EV_POSITION_CLOSED_TAKE_PROFIT,
    EV_POSITION_CLOSED_STOPOUT, EV_POSITION_CLOSED_MARGIN_CALL,
    EV_POSITION_CLOSED_ADMIN, EV_RISK_ORDER_REJECTED, EV_RISK_ALERT_OBSERVED,
    EV_ADMIN_POSITION_FORCE_CLOSE,
)
from simulator.broker_ledger import create_broker_counterparty_entry
from simulator.broker_alerts import Severity as AlertSeverity, Category as AlertCategory, BrokerRiskAlert
from simulator.consumers import TradingConsumer
from simulator.models import BrokerAuditEvent, BrokerAuditObservationLock, Position, Trade

from .factories import make_account, make_position, make_user

_run = lambda coro: __import__("asyncio").run(coro)
_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__
User = get_user_model()


def _seed_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


class _CleanFeedMixin:
    SYMBOLS = ("EUR/USD", "BTCUSD", "ETHUSD", "GBP/USD")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


def _consumer(account_id, netting_mode=False):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0, "leverage": 50,
        "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


def _make_alert(severity, *, alert_id="TEST:ALERT", category=AlertCategory.EXPOSURE, now=None):
    return BrokerRiskAlert(
        alert_id=alert_id, severity=severity, category=category,
        title="Test alert", description="Test alert description",
        status="ACTIVE", created_at=now or timezone.now(),
        source_module="simulator.tests", affected_symbol="EURUSD",
        affected_account=None, metric="test_metric",
        current_value=Decimal("1"), threshold=Decimal("2"), metadata={},
    )


# ─────────────────────────────────────────────────────────────────────────
# 1. Model
# ─────────────────────────────────────────────────────────────────────────
class ModelTests(TestCase):
    def test_event_id_auto_generated_and_unique(self):
        e1 = record_system_event(event_type="test.a", description="a")
        e2 = record_system_event(event_type="test.b", description="b")
        self.assertIsNotNone(e1.event_id)
        self.assertIsNotNone(e2.event_id)
        self.assertNotEqual(e1.event_id, e2.event_id)

    def test_str_contains_severity_and_event_type(self):
        e = record_system_event(event_type="test.str", description="x", severity=Severity.CRITICAL)
        self.assertIn("CRITICAL", str(e))
        self.assertIn("test.str", str(e))

    def test_default_ordering_is_newest_first(self):
        e1 = record_system_event(event_type="test.order1", description="first")
        e2 = record_system_event(event_type="test.order2", description="second")
        ids = list(BrokerAuditEvent.objects.filter(event_type__startswith="test.order").values_list("id", flat=True))
        self.assertEqual(ids, [e2.id, e1.id])

    def test_metadata_defaults_to_empty_dict(self):
        e = record_system_event(event_type="test.meta", description="x")
        self.assertEqual(e.metadata, {})


# ─────────────────────────────────────────────────────────────────────────
# 2. Engine — record_event() and category wrappers
# ─────────────────────────────────────────────────────────────────────────
class EngineTests(TestCase):
    def test_record_event_writes_exactly_one_row(self):
        before = BrokerAuditEvent.objects.count()
        record_event(
            event_type="test.generic", category=Category.SYSTEM, severity=Severity.INFO,
            actor_type=ActorType.SYSTEM, description="generic event",
        )
        self.assertEqual(BrokerAuditEvent.objects.count(), before + 1)

    def test_record_trade_event_category_is_trading(self):
        e = record_trade_event(event_type=EV_POSITION_OPENED, description="opened")
        self.assertEqual(e.category, Category.TRADING)
        self.assertEqual(e.actor_type, ActorType.TRADER)

    def test_record_risk_event_category_is_risk(self):
        e = record_risk_event(event_type=EV_RISK_ORDER_REJECTED, description="rejected")
        self.assertEqual(e.category, Category.RISK)
        self.assertEqual(e.severity, Severity.HIGH)

    def test_record_admin_event_category_and_actor(self):
        e = record_admin_event(event_type="admin.force_close", description="force close")
        self.assertEqual(e.category, Category.ADMIN)
        self.assertEqual(e.actor_type, ActorType.STAFF)

    def test_record_system_event_default_category(self):
        e = record_system_event(event_type="system.boot", description="boot")
        self.assertEqual(e.category, Category.SYSTEM)
        self.assertEqual(e.actor_type, ActorType.SYSTEM)

    def test_account_and_trade_fk_shortcuts_resolve_to_ids(self):
        account = make_account(balance=Decimal("1000"))
        e = record_trade_event(
            event_type=EV_POSITION_OPENED, description="x", account=account,
        )
        self.assertEqual(e.account_id, account.id)

    def test_never_raises_on_db_failure(self):
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            result = record_system_event(event_type="test.fail", description="x")
        self.assertIsNone(result)

    def test_failure_does_not_create_a_row(self):
        before = BrokerAuditEvent.objects.count()
        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            record_system_event(event_type="test.fail2", description="x")
        self.assertEqual(BrokerAuditEvent.objects.count(), before)


# ─────────────────────────────────────────────────────────────────────────
# 3. Close reason mapping
# ─────────────────────────────────────────────────────────────────────────
class CloseReasonMappingTests(TestCase):
    def test_manual(self):
        self.assertEqual(close_reason_event_type("manual"), EV_POSITION_CLOSED_MANUAL)

    def test_sl_and_daemon_sl(self):
        self.assertEqual(close_reason_event_type("sl"), EV_POSITION_CLOSED_STOP_LOSS)
        self.assertEqual(close_reason_event_type("daemon_sl"), EV_POSITION_CLOSED_STOP_LOSS)

    def test_tp_and_daemon_tp(self):
        self.assertEqual(close_reason_event_type("tp"), EV_POSITION_CLOSED_TAKE_PROFIT)
        self.assertEqual(close_reason_event_type("daemon_tp"), EV_POSITION_CLOSED_TAKE_PROFIT)

    def test_stopout_and_daemon_stopout(self):
        self.assertEqual(close_reason_event_type("stopout"), EV_POSITION_CLOSED_STOPOUT)
        self.assertEqual(close_reason_event_type("daemon_stopout"), EV_POSITION_CLOSED_STOPOUT)

    def test_daemon_margin_call(self):
        self.assertEqual(close_reason_event_type("daemon_margin_call"), EV_POSITION_CLOSED_MARGIN_CALL)

    def test_admin_force_close(self):
        self.assertEqual(close_reason_event_type("admin_force_close"), EV_POSITION_CLOSED_ADMIN)

    def test_unknown_reason_falls_back_to_generic(self):
        self.assertEqual(close_reason_event_type("something_new"), EV_POSITION_CLOSED)

    def test_none_reason_falls_back_to_generic(self):
        self.assertEqual(close_reason_event_type(None), EV_POSITION_CLOSED)


# ─────────────────────────────────────────────────────────────────────────
# 4. RISK-03 alert integration
# ─────────────────────────────────────────────────────────────────────────
class AlertIntegrationTests(TestCase):
    def test_critical_alert_is_recorded(self):
        alert = _make_alert(AlertSeverity.CRITICAL, alert_id="A:CRIT")
        e = record_alert_event(alert)
        self.assertIsNotNone(e)
        self.assertEqual(e.event_type, EV_RISK_ALERT_OBSERVED)
        self.assertEqual(e.severity, Severity.CRITICAL)
        self.assertEqual(e.metadata["alert_id"], "A:CRIT")

    def test_high_alert_is_recorded(self):
        alert = _make_alert(AlertSeverity.HIGH, alert_id="A:HIGH")
        e = record_alert_event(alert)
        self.assertIsNotNone(e)
        self.assertEqual(e.severity, Severity.HIGH)

    def test_medium_low_info_are_not_recorded(self):
        for sev in (AlertSeverity.MEDIUM, AlertSeverity.LOW, AlertSeverity.INFO):
            alert = _make_alert(sev, alert_id=f"A:{sev}")
            self.assertIsNone(record_alert_event(alert))

    def test_dedup_within_window_skips_second_write(self):
        alert = _make_alert(AlertSeverity.CRITICAL, alert_id="A:DEDUP")
        first = record_alert_event(alert, dedup_window_seconds=900)
        second = record_alert_event(alert, dedup_window_seconds=900)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(metadata__alert_id="A:DEDUP").count(), 1
        )

    def test_dedup_window_zero_always_records(self):
        alert = _make_alert(AlertSeverity.CRITICAL, alert_id="A:NODEDUP")
        first = record_alert_event(alert, dedup_window_seconds=0)
        second = record_alert_event(alert, dedup_window_seconds=0)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(metadata__alert_id="A:NODEDUP").count(), 2
        )

    def test_outside_window_records_again(self):
        from datetime import timedelta
        alert = _make_alert(AlertSeverity.CRITICAL, alert_id="A:OLD")
        first = record_alert_event(alert, dedup_window_seconds=900)
        self.assertIsNotNone(first)
        # Backdate the just-recorded event beyond the dedup window —
        # timestamp is auto_now_add, so it can only be moved via a
        # direct .update(), which (correctly) bypasses that auto-set.
        BrokerAuditEvent.objects.filter(pk=first.pk).update(
            timestamp=timezone.now() - timedelta(seconds=1000)
        )
        second = record_alert_event(alert, dedup_window_seconds=900)
        self.assertIsNotNone(second)

    def test_record_active_alerts_returns_written_count(self):
        alerts = [
            _make_alert(AlertSeverity.CRITICAL, alert_id="BATCH:1"),
            _make_alert(AlertSeverity.HIGH, alert_id="BATCH:2"),
            _make_alert(AlertSeverity.MEDIUM, alert_id="BATCH:3"),
        ]
        written = record_active_alerts(alerts)
        self.assertEqual(written, 2)

    def test_record_active_alerts_second_batch_dedups(self):
        alerts = [_make_alert(AlertSeverity.CRITICAL, alert_id="BATCH:DEDUP")]
        first = record_active_alerts(alerts)
        second = record_active_alerts(alerts)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)


# ─────────────────────────────────────────────────────────────────────────
# 5. Queries
# ─────────────────────────────────────────────────────────────────────────
class QueryTests(TestCase):
    def setUp(self):
        super().setUp()
        self.account_a = make_account(balance=Decimal("1000"))
        self.account_b = make_account(balance=Decimal("1000"))

    def test_recent_events_respects_limit(self):
        for i in range(5):
            record_system_event(event_type=f"test.recent{i}", description="x")
        self.assertEqual(len(recent_events(3)), 3)

    def test_recent_events_newest_first(self):
        e1 = record_system_event(event_type="test.r1", description="x")
        e2 = record_system_event(event_type="test.r2", description="x")
        events = recent_events(2)
        self.assertEqual(events[0].id, e2.id)
        self.assertEqual(events[1].id, e1.id)

    def test_events_for_account_filters_correctly(self):
        record_trade_event(event_type=EV_POSITION_OPENED, description="a", account=self.account_a)
        record_trade_event(event_type=EV_POSITION_OPENED, description="b", account=self.account_b)
        results = events_for_account(self.account_a.id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].account_id, self.account_a.id)

    def test_events_for_trade_filters_correctly(self):
        account = make_account(balance=Decimal("1000"))
        pos = make_position(account, symbol="EUR/USD")
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        other_trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        record_trade_event(event_type=EV_POSITION_CLOSED, description="closed", trade=trade)
        record_trade_event(event_type=EV_POSITION_CLOSED, description="closed other", trade=other_trade)
        results = events_for_trade(trade.id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].trade_id, trade.id)

    def test_events_for_symbol_filters_correctly(self):
        record_trade_event(event_type=EV_POSITION_OPENED, description="a", symbol="EURUSD")
        record_trade_event(event_type=EV_POSITION_OPENED, description="b", symbol="BTCUSD")
        results = events_for_symbol("EURUSD")
        self.assertTrue(all(r.symbol == "EURUSD" for r in results))
        self.assertEqual(len(results), 1)

    def test_events_by_category_filters_correctly(self):
        record_trade_event(event_type=EV_POSITION_OPENED, description="trading event")
        record_risk_event(event_type=EV_RISK_ORDER_REJECTED, description="risk event")
        results = events_by_category(Category.RISK)
        self.assertTrue(all(r.category == Category.RISK for r in results))
        self.assertGreaterEqual(len(results), 1)

    def test_events_by_severity_filters_correctly(self):
        record_system_event(event_type="test.crit", description="x", severity=Severity.CRITICAL)
        record_system_event(event_type="test.info", description="y", severity=Severity.INFO)
        results = events_by_severity(Severity.CRITICAL)
        self.assertTrue(all(r.severity == Severity.CRITICAL for r in results))


# ─────────────────────────────────────────────────────────────────────────
# 6. Integration — real FASE 6 call sites
# ─────────────────────────────────────────────────────────────────────────
class IntegrationTests(_CleanFeedMixin, TestCase):
    def test_position_open_creates_audit_event(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        before = BrokerAuditEvent.objects.filter(event_type=EV_POSITION_OPENED).count()
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        self.assertTrue(result["ok"] if "ok" in result else result["position_id"] is not None)
        after = BrokerAuditEvent.objects.filter(event_type=EV_POSITION_OPENED, account_id=account.id).count()
        self.assertEqual(after, 1)

    def test_position_open_event_carries_symbol_and_metadata(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        event = BrokerAuditEvent.objects.filter(
            event_type=EV_POSITION_OPENED, account_id=account.id
        ).latest()
        self.assertEqual(event.symbol, "EUR/USD")
        self.assertEqual(event.metadata["side"], "buy")
        self.assertFalse(event.metadata["merged"])

    def test_rejected_order_does_not_create_position_opened_event(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("0.5")):
            result = _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
                commission=0.0, new_balance=1000000.0,
            )
        self.assertFalse(result["ok"])
        count = BrokerAuditEvent.objects.filter(event_type=EV_POSITION_OPENED, account_id=account.id).count()
        self.assertEqual(count, 0)

    def test_risk02_rejection_creates_audit_event(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("0.5")):
            result = _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
                commission=0.0, new_balance=1000000.0,
            )
        self.assertFalse(result["ok"])
        event = BrokerAuditEvent.objects.filter(
            event_type=EV_RISK_ORDER_REJECTED, account_id=account.id
        ).latest()
        self.assertEqual(event.category, Category.RISK)
        self.assertEqual(event.metadata["reason_code"], result["error_code"])

    def test_broker_ledger_close_creates_audit_event(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1010"), profit_loss=Decimal("10.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        create_broker_counterparty_entry(trade, account, Decimal("10.00"), "manual")
        event = BrokerAuditEvent.objects.filter(trade_id=trade.id).latest()
        self.assertEqual(event.event_type, EV_POSITION_CLOSED_MANUAL)
        self.assertEqual(event.category, Category.TRADING)
        self.assertEqual(event.account_id, account.id)

    def test_broker_ledger_close_idempotent_replay_does_not_duplicate_audit_event(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1010"), profit_loss=Decimal("10.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        create_broker_counterparty_entry(trade, account, Decimal("10.00"), "manual")
        create_broker_counterparty_entry(trade, account, Decimal("10.00"), "manual")  # replay
        count = BrokerAuditEvent.objects.filter(trade_id=trade.id).count()
        self.assertEqual(count, 1)

    def test_admin_force_close_reason_maps_to_admin_event_type(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        create_broker_counterparty_entry(trade, account, Decimal("0.00"), "admin_force_close")
        event = BrokerAuditEvent.objects.filter(trade_id=trade.id).latest()
        self.assertEqual(event.event_type, EV_POSITION_CLOSED_ADMIN)
        self.assertEqual(event.actor_type, ActorType.STAFF)

    def test_daemon_close_reason_maps_to_system_actor(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        create_broker_counterparty_entry(trade, account, Decimal("0.00"), "daemon_sl")
        event = BrokerAuditEvent.objects.filter(trade_id=trade.id).latest()
        self.assertEqual(event.event_type, EV_POSITION_CLOSED_STOP_LOSS)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)

    def test_admin_dashboard_payload_includes_recent_audit_events(self):
        from simulator.admin import _compute_control_data
        record_system_event(event_type="test.dashboard", description="visible on dashboard")
        data = _compute_control_data()
        self.assertIn("recent_audit_events", data)
        self.assertTrue(any(e["event_type"] == "test.dashboard" for e in data["recent_audit_events"]))


# ─────────────────────────────────────────────────────────────────────────
# 6b. Correction 1 — the dashboard must never write; observe_broker_alerts()
# is the only sanctioned write path; dedup is atomic under real concurrency.
# ─────────────────────────────────────────────────────────────────────────
class ServiceLayerTests(TestCase):
    def test_dashboard_load_does_not_change_audit_event_count(self):
        from simulator.admin import _compute_control_data
        with patch(
            "simulator.broker_alerts.collect_risk_alerts",
            return_value=[_make_alert(AlertSeverity.CRITICAL, alert_id="NOWRITE:1")],
        ):
            before = BrokerAuditEvent.objects.count()
            _compute_control_data()
            after_first = BrokerAuditEvent.objects.count()
            _compute_control_data()  # a second "poll"
            after_second = BrokerAuditEvent.objects.count()
        self.assertEqual(before, after_first)
        self.assertEqual(after_first, after_second)
        self.assertFalse(BrokerAuditEvent.objects.filter(metadata__alert_id="NOWRITE:1").exists())

    def test_repeated_dashboard_polling_never_writes_regardless_of_alert_count(self):
        from simulator.admin import _compute_control_data
        alerts = [
            _make_alert(AlertSeverity.CRITICAL, alert_id=f"POLL:{i}") for i in range(5)
        ]
        with patch("simulator.broker_alerts.collect_risk_alerts", return_value=alerts):
            before = BrokerAuditEvent.objects.count()
            for _ in range(10):  # simulate ten dashboard polls
                _compute_control_data()
            after = BrokerAuditEvent.objects.count()
        self.assertEqual(before, after)

    def test_observe_broker_alerts_persists_without_any_dashboard_call(self):
        with patch(
            "simulator.broker_alerts.collect_risk_alerts",
            return_value=[_make_alert(AlertSeverity.CRITICAL, alert_id="SERVICE:1")],
        ):
            written = observe_broker_alerts()
        self.assertEqual(written, 1)
        self.assertTrue(BrokerAuditEvent.objects.filter(metadata__alert_id="SERVICE:1").exists())

    def test_observe_broker_alerts_is_what_the_celery_task_calls(self):
        from simulator.tasks import observe_broker_risk_alerts_task
        with patch(
            "simulator.broker_alerts.collect_risk_alerts",
            return_value=[_make_alert(AlertSeverity.CRITICAL, alert_id="TASK:1")],
        ):
            result = observe_broker_risk_alerts_task.apply().get()
        self.assertEqual(result["written"], 1)
        self.assertTrue(BrokerAuditEvent.objects.filter(metadata__alert_id="TASK:1").exists())

    def test_beat_schedule_registers_the_periodic_task(self):
        from django.conf import settings as dj_settings
        schedule = dj_settings.CELERY_BEAT_SCHEDULE
        matching = [v for v in schedule.values() if v["task"] == "simulator.observe_broker_risk_alerts"]
        self.assertEqual(len(matching), 1)


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Same technique as test_broker_risk_limits_engine.py's helper —
    real thread contention on every attempt, retried only on SQLite's
    shared-cache SQLITE_LOCKED (not a substitute for busy_timeout, which
    only covers SQLITE_BUSY)."""
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = fn()
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    raise
                time.sleep(random.uniform(0.005, 0.03))
    finally:
        connection.close()


class ConcurrentObservationDedupTests(TransactionTestCase):
    """Correction 1 — 'deduplicación concurrente/atómica'. Genuine OS
    threads (not sequential-call simulation), both racing to record an
    observation for the SAME alert_id at the same instant. Without
    BrokerAuditObservationLock serializing the check-then-create, both
    threads could pass the 'not yet recorded' check before either
    commits, producing two rows. TransactionTestCase — real cross-thread
    DB visibility is required, same reasoning as every other genuine
    concurrency test in this codebase."""

    def test_two_threads_recording_the_same_alert_id_produce_exactly_one_row(self):
        alert = _make_alert(AlertSeverity.CRITICAL, alert_id="RACE:1")

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n

        def _record():
            return record_alert_event(alert, dedup_window_seconds=900)

        threads = [
            threading.Thread(
                target=_run_locked_retry, args=(_record, barrier, results, i)
            )
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        count = BrokerAuditEvent.objects.filter(metadata__alert_id="RACE:1").count()
        self.assertEqual(count, 1, f"results={results}")
        # Exactly one of the two calls should have returned an event,
        # the other None (deduped) — never both non-None.
        non_none = [r for r in results if r is not None]
        self.assertEqual(len(non_none), 1)


class ObservationLockStructuralTests(TestCase):
    """Plain TestCase (not TransactionTestCase) — deliberately, same
    reasoning as RISK-02's LockOrderStructuralTests: TransactionTestCase
    flushes tables between tests without re-running data migrations,
    which would wipe the migration-seeded singleton row before this
    assertion ever runs. TestCase wraps each test in a rolled-back
    transaction instead, so the migration-seeded row survives."""

    def test_observation_lock_singleton_row_exists(self):
        self.assertEqual(BrokerAuditObservationLock.objects.filter(pk=1).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 6c. Correction 2 — financial vs. administrative events. Exercises the
# REAL admin view end-to-end (Client + force_login), the only code path
# that actually holds request.user, matching the established
# AdminForceCloseCounterpartyIntegrationTests pattern from BOOK-02.
# ─────────────────────────────────────────────────────────────────────────
class AdminForceCloseActorTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = make_user(username="audit01_staff", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_force_close_preserves_the_real_staff_actor(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})
        self.assertEqual(resp.status_code, 302)

        admin_event = BrokerAuditEvent.objects.get(event_type=EV_ADMIN_POSITION_FORCE_CLOSE, account=account)
        self.assertEqual(admin_event.actor_type, ActorType.STAFF)
        self.assertEqual(admin_event.actor_id, self.staff.id)
        self.assertEqual(admin_event.category, Category.ADMIN)

    def test_normal_close_does_not_create_an_administrative_event(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1010"), profit_loss=Decimal("10.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        create_broker_counterparty_entry(trade, account, Decimal("10.00"), "manual")
        self.assertFalse(
            BrokerAuditEvent.objects.filter(
                event_type=EV_ADMIN_POSITION_FORCE_CLOSE, trade=trade
            ).exists()
        )

    def test_force_close_creates_exactly_one_financial_event_and_one_administrative_event(self):
        account = make_account(account_type="STANDARD", balance=Decimal("100000"))
        Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("100"),
        )
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        self.client.post(url, {"action": "force_close", "symbol": "BTCUSD", "price": "110"})

        trade = Trade.objects.get(account=account)
        financial_events = BrokerAuditEvent.objects.filter(
            trade=trade, category=Category.TRADING, event_type=EV_POSITION_CLOSED_ADMIN,
        )
        admin_events = BrokerAuditEvent.objects.filter(
            trade=trade, category=Category.ADMIN, event_type=EV_ADMIN_POSITION_FORCE_CLOSE,
        )
        self.assertEqual(financial_events.count(), 1)
        self.assertEqual(admin_events.count(), 1)
        # Two distinct events, not a duplicate of the same fact.
        self.assertNotEqual(financial_events.first().event_id, admin_events.first().event_id)

    def test_financial_close_event_stays_unique_on_retry(self):
        # broker_ledger.py's get_or_create() idempotency already covers
        # this (see NoRegressionTests), but re-verify specifically in
        # the dual-event world introduced by this correction: replaying
        # the financial writer must not multiply EITHER event.
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        create_broker_counterparty_entry(trade, account, Decimal("0.00"), "admin_force_close")
        create_broker_counterparty_entry(trade, account, Decimal("0.00"), "admin_force_close")  # replay
        financial_count = BrokerAuditEvent.objects.filter(
            trade=trade, event_type=EV_POSITION_CLOSED_ADMIN
        ).count()
        self.assertEqual(financial_count, 1)


# ─────────────────────────────────────────────────────────────────────────
# 6d. Correction 3 — real append-only protection.
# ─────────────────────────────────────────────────────────────────────────
class AppendOnlyProtectionTests(TestCase):
    def test_deleting_account_does_not_delete_the_event(self):
        account = make_account(balance=Decimal("1000"))
        event = record_trade_event(event_type=EV_POSITION_OPENED, description="x", account=account)
        account.delete()
        event.refresh_from_db()
        self.assertIsNone(event.account_id)
        self.assertTrue(BrokerAuditEvent.objects.filter(pk=event.pk).exists())

    def test_deleting_trade_does_not_delete_the_event(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        event = record_trade_event(event_type=EV_POSITION_CLOSED, description="x", trade=trade)
        trade.delete()
        event.refresh_from_db()
        self.assertIsNone(event.trade_id)
        self.assertTrue(BrokerAuditEvent.objects.filter(pk=event.pk).exists())

    def test_admin_cannot_add(self):
        from simulator.admin import BrokerAuditEventAdmin
        from django.contrib.admin.sites import site
        ma = BrokerAuditEventAdmin(BrokerAuditEvent, site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_admin_cannot_change(self):
        from simulator.admin import BrokerAuditEventAdmin
        from django.contrib.admin.sites import site
        ma = BrokerAuditEventAdmin(BrokerAuditEvent, site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_admin_cannot_delete(self):
        from simulator.admin import BrokerAuditEventAdmin
        from django.contrib.admin.sites import site
        ma = BrokerAuditEventAdmin(BrokerAuditEvent, site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_action_is_not_available(self):
        from simulator.admin import BrokerAuditEventAdmin
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        ma = BrokerAuditEventAdmin(BrokerAuditEvent, site)
        request = RequestFactory().get("/")
        request.user = make_user(
            username="audit01_appendonly_staff", is_staff=True, is_superuser=True
        )
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_admin_view_rejects_add_via_permission_check(self):
        staff = make_user(username="audit01_ro_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_brokerauditevent_add")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_admin_changelist_has_no_delete_selected_option(self):
        staff = make_user(username="audit01_ro_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        record_system_event(event_type="test.visible_in_admin", description="x")
        url = reverse("admin:simulator_brokerauditevent_changelist")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)


# ─────────────────────────────────────────────────────────────────────────
# 7. No regression — BOOK-02/BOOK-03/RISK-01/RISK-02/RISK-03
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionTests(TransactionTestCase):
    def test_create_broker_counterparty_entry_still_returns_entry(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1010"), profit_loss=Decimal("10.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        entry = create_broker_counterparty_entry(trade, account, Decimal("10.00"), "manual")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.amount, Decimal("-10.00"))

    def test_broker_ledger_idempotency_still_enforced_at_db_level(self):
        account = make_account(balance=Decimal("1000"))
        trade = Trade.objects.create(
            account=account, symbol="EUR/USD", trade_type="BUY", lot_size=Decimal("1"),
            entry_price=Decimal("1.1"), exit_price=Decimal("1.1"), profit_loss=Decimal("0.00"),
            opened_at=timezone.now(), closed_at=timezone.now(),
        )
        e1 = create_broker_counterparty_entry(trade, account, Decimal("0.00"), "manual")
        e2 = create_broker_counterparty_entry(trade, account, Decimal("0.00"), "manual")
        self.assertEqual(e1.id, e2.id)

    def test_risk02_order_open_path_still_functions(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        self.assertTrue(result["ok"] if "ok" in result else result["position_id"] is not None)
        _clear_price("EUR/USD")

    def test_broker_alerts_collector_remains_pure_read(self):
        # RISK-03's own documented contract: collect_risk_alerts() itself
        # must never write anything — AUDIT-01's alert recording happens
        # exclusively in admin.py's _compute_control_data(), never inside
        # broker_alerts.py. Calling the collector directly must create
        # zero BrokerAuditEvent rows.
        from simulator.broker_alerts import collect_risk_alerts
        before = BrokerAuditEvent.objects.count()
        collect_risk_alerts()
        after = BrokerAuditEvent.objects.count()
        self.assertEqual(before, after)

    def test_broker_monitoring_full_report_unaffected(self):
        from simulator.broker_monitoring import full_report
        report = full_report()
        self.assertIn("broker_alerts", report)
        self.assertNotIn("broker_alerts", report.get("errors", {}))

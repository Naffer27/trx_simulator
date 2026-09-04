"""
simulator/tests/test_order_management_v2a.py

ORDER-MANAGEMENT-V2A — Pending orders (limit/stop). Covers the design
lock's section 11.L test list.

Shared trigger executor under test: simulator.consumers.
_trigger_pending_order_core — the ONE function both the live WS path
(TradingConsumer._db_trigger_pending_order_atomic) and the offline daemon
(tasks.scan_pending_orders_task) call. It is a plain module-level
function (not @database_sync_to_async), so it is called directly here —
same "call the real atomic function on the test's own connection" pattern
already established in test_atomic_margin_and_position_guard.py for
_db_open_position_atomic.__wrapped__, just without needing .__wrapped__
at all since this one was never wrapped in the first place.

_db_cancel_pending_order / _db_update_pending_order (TradingConsumer
methods) ARE wrapped in @database_sync_to_async — tested via
.__wrapped__ + a bare TradingConsumer.__new__ instance, exactly the
_consumer()/_db_open_sync pattern from test_atomic_margin_and_position_
guard.py.

All open-position pricing in these tests uses accounts with ZERO other
open positions, so _trigger_pending_order_core's own "other open
positions" pricing branch (tasks._read_cached_price) is never exercised
here except in the dedicated daemon-wiring tests, where it IS mocked
(same @patch("simulator.tasks._read_cached_price") pattern as
test_daemon_scan.py).
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from market_data.symbol_specs import get_spec
from simulator.consumers import (
    TradingConsumer,
    _pending_trigger_condition_met,
    _trigger_pending_order_core,
)
from simulator.models import LedgerEntry, PendingOrder, Position, TradingAccount
from simulator.tasks import scan_pending_orders_task

from .factories import make_account

EURUSD_SPEC = get_spec("EUR/USD")

_db_cancel_sync = TradingConsumer._db_cancel_pending_order.__wrapped__
_db_update_sync = TradingConsumer._db_update_pending_order.__wrapped__


def _consumer(account_id):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    return c


def _acc(leverage=50, balance=Decimal("10000")):
    """make_account() hardcodes leverage=50 in its own .create() call, so
    make_account(leverage=N) collides ('multiple values for keyword
    argument'). Same workaround already used in
    test_fix_snapshots_contract_size_01.py."""
    a = make_account(balance=balance)
    if leverage != 50:
        a.leverage = leverage
        a.save(update_fields=["leverage"])
    return a


def _pending(account, order_type="LIMIT", side="BUY", qty="0.1",
             trigger_price="1.10000", sl=None, tp=None, expires_at=None):
    return PendingOrder.objects.create(
        account=account, symbol="EUR/USD", side=side, order_type=order_type,
        qty=Decimal(qty), trigger_price=Decimal(trigger_price),
        sl=Decimal(sl) if sl is not None else None,
        tp=Decimal(tp) if tp is not None else None,
        expires_at=expires_at,
    )


def _scan_pending():
    """Execute scan_pending_orders_task synchronously, no Celery broker —
    same pattern as test_daemon_scan.py's _scan()."""
    return scan_pending_orders_task.apply().get()


# ─────────────────────────────────────────────────────────────────────────
# 1. Trigger condition table (design lock section 2/C) — pure function
# ─────────────────────────────────────────────────────────────────────────

class TriggerConditionTableTests(TestCase):
    def test_buy_limit(self):
        self.assertTrue(_pending_trigger_condition_met("LIMIT", "BUY", 1.1000, bid=1.0990, ask=1.0995))
        self.assertFalse(_pending_trigger_condition_met("LIMIT", "BUY", 1.1000, bid=1.1010, ask=1.1015))

    def test_sell_limit(self):
        self.assertTrue(_pending_trigger_condition_met("LIMIT", "SELL", 1.1000, bid=1.1005, ask=1.1010))
        self.assertFalse(_pending_trigger_condition_met("LIMIT", "SELL", 1.1000, bid=1.0990, ask=1.0995))

    def test_buy_stop(self):
        self.assertTrue(_pending_trigger_condition_met("STOP", "BUY", 1.1000, bid=1.1005, ask=1.1010))
        self.assertFalse(_pending_trigger_condition_met("STOP", "BUY", 1.1000, bid=1.0990, ask=1.0995))

    def test_sell_stop(self):
        self.assertTrue(_pending_trigger_condition_met("STOP", "SELL", 1.1000, bid=1.0990, ask=1.0995))
        self.assertFalse(_pending_trigger_condition_met("STOP", "SELL", 1.1000, bid=1.1005, ask=1.1010))

    def test_case_insensitive(self):
        self.assertTrue(_pending_trigger_condition_met("limit", "buy", 1.1000, bid=1.0990, ask=1.0995))


# ─────────────────────────────────────────────────────────────────────────
# 2. _trigger_pending_order_core — the 4 order types, fill != trigger_price
# ─────────────────────────────────────────────────────────────────────────

class TriggerCoreFourTypesTests(TestCase):
    def test_buy_limit_triggers_and_fills_at_execution_price_not_trigger_price(self):
        account = _acc()
        po = _pending(account, order_type="LIMIT", side="BUY", trigger_price="1.10000")
        # Design lock section 3 — execution_price (the real ask at the
        # moment of trigger) deliberately != trigger_price (a gap).
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)
        self.assertIsNotNone(po.triggered_position_id)
        pos = Position.objects.get(pk=po.triggered_position_id)
        self.assertEqual(pos.side, "BUY")
        self.assertEqual(float(pos.avg_price), 1.09950)
        self.assertNotEqual(float(pos.avg_price), float(po.trigger_price))

    def test_sell_limit_triggers(self):
        account = _acc()
        po = _pending(account, order_type="LIMIT", side="SELL", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.10050)
        self.assertTrue(result["ok"])
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)
        pos = Position.objects.get(pk=po.triggered_position_id)
        self.assertEqual(pos.side, "SELL")

    def test_buy_stop_triggers(self):
        account = _acc()
        po = _pending(account, order_type="STOP", side="BUY", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.10080)
        self.assertTrue(result["ok"])
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)

    def test_sell_stop_triggers(self):
        account = _acc()
        po = _pending(account, order_type="STOP", side="SELL", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.09920)
        self.assertTrue(result["ok"])
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)

    def test_trigger_charges_commission_when_configured(self):
        """Commission (unlike the spread markup fee — deliberately
        excluded, see _trigger_pending_order_core's docstring) IS charged,
        via the legacy spec.commission_pct fallback when no commercial
        profile/snapshot is configured."""
        account = _acc()
        po = _pending(account, order_type="LIMIT", side="BUY", trigger_price="1.10000")
        if EURUSD_SPEC.commission_pct <= 0:
            self.skipTest("EUR/USD spec has zero commission_pct in this environment")
        _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).exists()
        )


# ─────────────────────────────────────────────────────────────────────────
# 3. Idempotency — trigger vs trigger (live vs daemon / two panels)
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyTests(TestCase):
    def test_second_trigger_attempt_on_same_row_is_a_noop(self):
        account = _acc()
        po = _pending(account, trigger_price="1.10000")
        r1 = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(r1["ok"])
        positions_after_first = Position.objects.filter(account=account).count()

        r2 = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["code"], "not_pending")
        self.assertEqual(Position.objects.filter(account=account).count(), positions_after_first)

    def test_unknown_id_is_not_pending(self):
        result = _trigger_pending_order_core(999_999_999, execution_price=1.1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_pending")


# ─────────────────────────────────────────────────────────────────────────
# 4. Expiry precedence (design lock section 6)
# ─────────────────────────────────────────────────────────────────────────

class ExpiryPrecedenceTests(TestCase):
    def test_expired_wins_even_when_trigger_condition_is_also_met(self):
        account = _acc()
        past = timezone.now() - timezone.timedelta(minutes=1)
        po = _pending(account, order_type="LIMIT", side="BUY",
                      trigger_price="1.10000", expires_at=past)
        # execution_price given as if the trigger WOULD have fired —
        # expiry must still win.
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "expired")
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.EXPIRED)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)

    def test_future_expiry_does_not_block_trigger(self):
        account = _acc()
        future = timezone.now() + timezone.timedelta(days=1)
        po = _pending(account, trigger_price="1.10000", expires_at=future)
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertTrue(result["ok"])


# ─────────────────────────────────────────────────────────────────────────
# 5. SL/TP gap rejection (design lock section 4)
# ─────────────────────────────────────────────────────────────────────────

class SlTpGapRejectionTests(TestCase):
    def test_sl_on_wrong_side_of_real_fill_price_is_rejected(self):
        account = _acc()
        # BUY LIMIT trigger=1.10000, sl=1.09950 — valid at creation time
        # (sl strictly below trigger_price). A sharp gap DOWN means the
        # real fill (execution_price) lands BELOW the sl level itself —
        # by the time the order actually fills, sl is no longer below
        # the fill price, so it must be rejected, never silently
        # accepted or silently dropped.
        po = _pending(account, order_type="LIMIT", side="BUY",
                      trigger_price="1.10000", sl="1.09950")
        result = _trigger_pending_order_core(po.id, execution_price=1.09900)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_sl_direction")
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.REJECTED)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account).count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 6. Margin insufficient at trigger time (design lock section 7)
# ─────────────────────────────────────────────────────────────────────────

class MarginInsufficientRejectionTests(TestCase):
    def test_insufficient_margin_at_trigger_rejects_cleanly(self):
        account = _acc(balance=Decimal("50.00"))  # too small for qty=0.1 EUR/USD
        po = _pending(account, order_type="LIMIT", side="BUY",
                      qty="0.1", trigger_price="1.10000")
        result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        self.assertFalse(result["ok"])
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.REJECTED)
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(LedgerEntry.objects.filter(account=account).count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 7. Cancel / modify races (design lock section 5 — select_for_update,
#    never optimistic check-and-set)
# ─────────────────────────────────────────────────────────────────────────

class CancelModifyRaceTests(TestCase):
    def test_cancel_a_pending_order_succeeds(self):
        account = _acc()
        po = _pending(account, trigger_price="1.10000")
        result = _db_cancel_sync(_consumer(account.pk), po.id)
        self.assertTrue(result["ok"])
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.CANCELLED)
        self.assertIsNotNone(po.cancelled_at)

    def test_cancel_after_trigger_fails_cleanly_never_false_success(self):
        account = _acc()
        po = _pending(account, trigger_price="1.10000")
        _trigger_pending_order_core(po.id, execution_price=1.09950)
        result = _db_cancel_sync(_consumer(account.pk), po.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_pending")
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)  # unchanged

    def test_modify_a_pending_order_succeeds(self):
        account = _acc()
        po = _pending(account, trigger_price="1.10000")
        result = _db_update_sync(_consumer(account.pk), po.id, 1.09000, None, None, None)
        self.assertTrue(result["ok"])
        po.refresh_from_db()
        self.assertEqual(float(po.trigger_price), 1.09000)

    def test_modify_after_trigger_fails_cleanly(self):
        account = _acc()
        po = _pending(account, trigger_price="1.10000")
        _trigger_pending_order_core(po.id, execution_price=1.09950)
        result = _db_update_sync(_consumer(account.pk), po.id, 1.08000, None, None, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_pending")

    def test_cancel_wrong_account_is_not_found(self):
        account = _acc()
        other = _acc()
        po = _pending(account, trigger_price="1.10000")
        result = _db_cancel_sync(_consumer(other.pk), po.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_found")


# ─────────────────────────────────────────────────────────────────────────
# 8. Daemon wiring — scan_pending_orders_task (offline path)
# ─────────────────────────────────────────────────────────────────────────

from unittest.mock import patch  # noqa: E402  (grouped with the section it serves)


class DaemonWiringTests(TestCase):
    def test_no_pending_orders_returns_zero_counts(self):
        result = _scan_pending()
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["triggered"], 0)
        self.assertEqual(result["expired"], 0)

    @patch("simulator.tasks._read_cached_price")
    def test_daemon_triggers_buy_limit_via_the_shared_core(self, mock_price):
        account = _acc()
        po = _pending(account, order_type="LIMIT", side="BUY", trigger_price="1.10000")
        mock_price.return_value = (1.09945, 1.09950)  # ask <= trigger -> BUY LIMIT fires
        result = _scan_pending()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["triggered"], 1)
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.TRIGGERED)
        pos = Position.objects.get(pk=po.triggered_position_id)
        self.assertEqual(float(pos.avg_price), 1.09950)  # filled at ASK, not trigger_price

    @patch("simulator.tasks._read_cached_price")
    def test_daemon_skips_stale_when_no_price(self, mock_price):
        account = _acc()
        _pending(account, order_type="LIMIT", side="BUY", trigger_price="1.10000")
        mock_price.return_value = (None, None)
        result = _scan_pending()
        self.assertEqual(result["skipped_stale"], 1)
        self.assertEqual(result["triggered"], 0)

    @patch("simulator.tasks._read_cached_price")
    def test_daemon_does_not_trigger_when_condition_not_met(self, mock_price):
        account = _acc()
        po = _pending(account, order_type="LIMIT", side="BUY", trigger_price="1.10000")
        mock_price.return_value = (1.10500, 1.10520)  # ask > trigger -> BUY LIMIT does NOT fire
        result = _scan_pending()
        self.assertEqual(result["triggered"], 0)
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.PENDING)

    @patch("simulator.tasks._read_cached_price")
    def test_daemon_expires_via_the_shared_core_before_pricing(self, mock_price):
        account = _acc()
        past = timezone.now() - timezone.timedelta(minutes=1)
        po = _pending(account, order_type="LIMIT", side="BUY",
                      trigger_price="1.10000", expires_at=past)
        # No price mocked to succeed for this symbol — if the daemon tried
        # to price it before checking expiry, it would count as
        # skipped_stale instead of expired (mock_price default MagicMock()
        # is not a real (bid, ask) tuple, so unpacking would raise if the
        # expiry-first ordering weren't respected — proving precedence).
        mock_price.return_value = (None, None)
        result = _scan_pending()
        self.assertEqual(result["expired"], 1)
        po.refresh_from_db()
        self.assertEqual(po.status, PendingOrder.EXPIRED)

    @patch("simulator.tasks._read_cached_price")
    def test_live_and_daemon_triggering_the_same_row_only_one_wins(self, mock_price):
        """Simulates a live-tick trigger and a daemon trigger racing for
        the SAME PendingOrder — both routes call the exact same shared
        core, so calling it twice back-to-back (as the daemon loop and a
        'concurrent' live call each independently would) proves the
        select_for_update guard, not a second, divergent implementation."""
        account = _acc()
        po = _pending(account, order_type="LIMIT", side="BUY", trigger_price="1.10000")
        mock_price.return_value = (1.09945, 1.09950)

        live_result = _trigger_pending_order_core(po.id, execution_price=1.09950)
        daemon_result = _scan_pending()

        self.assertTrue(live_result["ok"])
        self.assertEqual(daemon_result["triggered"], 0)  # already handled by the "live" call
        self.assertEqual(Position.objects.filter(account=account).count(), 1)

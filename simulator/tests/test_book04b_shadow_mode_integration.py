"""
BOOK-04b — Shadow Mode Integration tests.

Covers the single real integration point authorized for this block:
TradingConsumer._db_open_position_atomic(), gated by
settings.ROUTING_ENGINE_ENABLED. No test here touches Trade,
BrokerLedger revenue rows beyond what already existed pre-BOOK-04b,
BrokerAuditEvent, risk rules, or any classification of the trader —
that is explicitly out of scope for this block (see
docs/BOOK_04_IMPLEMENTATION_PLAN.md, BOOK-04b).

Same low-level pattern already used by
simulator/tests/test_netting_merge_deadlock_guard.py and
simulator/tests/test_broker_risk_limits_engine.py: call the underlying
sync function directly (`.__wrapped__`), bypassing
`database_sync_to_async`, with a minimal `_FakeConsumer` stub.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.models import BrokerLedger, LedgerEntry, Position, RoutingDecision, TradingAccount
from simulator.routing_engine import (
    Book,
    ENGINE_VERSION,
    REASON_TRIVIAL_INTERNAL_DEFAULT,
    SCHEMA_VERSION,
)
from simulator.tasks import _close_position_sync

from .factories import make_account

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


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


class _FakeConsumer:
    """Minimal consumer stub: only the attributes _db_open_position_atomic reads."""
    def __init__(self, account_id, netting_mode=False):
        self._db_account_id = account_id
        self.account = {"netting_mode": netting_mode, "spread_pips": 0.0}
        self._feed = get_feed_manager()


class _CleanPriceMixin:
    """Ensures EUR/USD's global feed price is deterministic per test, not
    dependent on whatever another test file happened to seed earlier —
    the exact ambient-state problem observed in
    test_netting_merge_deadlock_guard.py when run in isolation."""

    def setUp(self):
        super().setUp()
        _seed_price("EUR/USD", 1.0800)
        self.addCleanup(_clear_price, "EUR/USD")


# ─────────────────────────────────────────────────────────────────────────
# Flag apagado — ROUTING_ENGINE_ENABLED=False (default)
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=False)
class FlagOffTests(_CleanPriceMixin, TestCase):

    def test_new_position_creates_no_routing_decision(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )

        self.assertTrue(result["ok"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)
        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNone(pos.routing_decision_id)

    def test_merge_creates_no_routing_decision(self):
        account = make_account(balance=Decimal("50000"))
        existing = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.0800"),
        )
        consumer = _FakeConsumer(account.pk, netting_mode=True)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0900, None, None,
            commission=0.0, new_balance=50000.0,
        )

        self.assertTrue(result["merged"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)
        existing.refresh_from_db()
        self.assertIsNone(existing.routing_decision_id)

    def test_rejected_order_also_carries_routing_decision_id_none(self):
        """Every return path — not just the success one — carries the new
        key, so no future caller hits a KeyError on a rejected order."""
        account = make_account(balance=Decimal("100"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1000.0, 1.0800, None, None,
            commission=0.0, new_balance=100.0,
        )

        self.assertFalse(result["ok"])
        self.assertIn("routing_decision_id", result)
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)


def _normalize_broker_ledger_meta(meta):
    """db_pos_id is a legitimate cross-reference identifier (differs
    between the two accounts/positions used in the comparison below by
    construction, same category as a PK) — excluded, everything else in
    meta must match exactly."""
    m = dict(meta or {})
    m.pop("db_pos_id", None)
    return m


# ─────────────────────────────────────────────────────────────────────────
# Flag on/off comparison — everything except routing_decision_id /
# RoutingDecision creation must be bit-for-bit identical.
# ─────────────────────────────────────────────────────────────────────────
class FlagComparisonTests(_CleanPriceMixin, TestCase):

    def test_balance_margin_commission_ledger_result_identical_across_flag_states(self):
        account_off = make_account(balance=Decimal("50000"))
        consumer_off = _FakeConsumer(account_off.pk, netting_mode=False)
        with override_settings(ROUTING_ENGINE_ENABLED=False):
            result_off = _db_open_sync(
                consumer_off, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=10.0, new_balance=49990.0,
            )

        account_on = make_account(balance=Decimal("50000"))
        consumer_on = _FakeConsumer(account_on.pk, netting_mode=False)
        with override_settings(ROUTING_ENGINE_ENABLED=True):
            result_on = _db_open_sync(
                consumer_on, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=10.0, new_balance=49990.0,
            )

        # ── result dict — every pre-existing key except routing_decision_id
        # and position_id (legitimately different PKs across two accounts).
        shared_keys = (
            "merged", "new_balance", "ok",
            "required_margin", "required_margin_pct",
            "projected_total_margin", "projected_total_margin_pct",
            "max_total_margin_pct", "current_open_positions", "max_open_positions",
        )
        for key in shared_keys:
            self.assertEqual(result_off[key], result_on[key], key)

        # ── TradingAccount.balance
        account_off.refresh_from_db()
        account_on.refresh_from_db()
        self.assertEqual(account_off.balance, account_on.balance)

        # ── Position — every persisted operational field except account
        # (FK, different accounts by construction), id/opened_at
        # (identifiers/timestamps), and routing_decision (the one field
        # this whole block exists to make differ).
        pos_off = Position.objects.get(pk=result_off["position_id"])
        pos_on = Position.objects.get(pk=result_on["position_id"])
        position_fields = ("symbol", "side", "qty", "avg_price", "sl", "tp",
                            "external_id", "pricing_context")
        for field in position_fields:
            self.assertEqual(getattr(pos_off, field), getattr(pos_on, field), field)
        # The one field that MUST differ, by design.
        self.assertIsNone(pos_off.routing_decision_id)
        self.assertIsNotNone(pos_on.routing_decision_id)

        # ── LedgerEntry (trader-facing ledger)
        ledger_off = list(
            LedgerEntry.objects.filter(account=account_off)
            .order_by("id")
            .values("amount", "balance_after", "event_type")
        )
        ledger_on = list(
            LedgerEntry.objects.filter(account=account_on)
            .order_by("id")
            .values("amount", "balance_after", "event_type")
        )
        self.assertTrue(ledger_off)   # non-trivial: commission=10.0 must have produced a row
        self.assertEqual(ledger_off, ledger_on)

        # ── BrokerLedger (broker-side revenue ledger — commission here;
        # no spread config exists in this test's fixtures, so this is the
        # only BrokerLedger row either run produces).
        broker_ledger_off = [
            {
                "revenue_type": bl.revenue_type,
                "amount": bl.amount,
                "symbol": bl.symbol,
                "meta": _normalize_broker_ledger_meta(bl.meta),
            }
            for bl in BrokerLedger.objects.filter(source_account=account_off).order_by("id")
        ]
        broker_ledger_on = [
            {
                "revenue_type": bl.revenue_type,
                "amount": bl.amount,
                "symbol": bl.symbol,
                "meta": _normalize_broker_ledger_meta(bl.meta),
            }
            for bl in BrokerLedger.objects.filter(source_account=account_on).order_by("id")
        ]
        self.assertTrue(broker_ledger_off)   # non-trivial: at least the commission row
        self.assertEqual(broker_ledger_off, broker_ledger_on)
        for bl in broker_ledger_off:
            self.assertEqual(bl["revenue_type"], BrokerLedger.REV_COMMISSION)
            self.assertEqual(bl["amount"], Decimal("10.00"))

        # ── Only the flag-on run produced a routing decision.
        self.assertIsNone(result_off["routing_decision_id"])
        self.assertIsNotNone(result_on["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.filter(position=pos_off).count(), 0)
        self.assertEqual(RoutingDecision.objects.filter(position=pos_on).count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# Flag activo — ROUTING_ENGINE_ENABLED=True
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class FlagOnTests(_CleanPriceMixin, TestCase):

    def test_new_position_creates_internal_decision_with_full_contract(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )

        self.assertEqual(RoutingDecision.objects.count(), 1)
        decision = RoutingDecision.objects.get()

        self.assertEqual(decision.book, Book.INTERNAL)
        self.assertEqual(decision.reason_code, REASON_TRIVIAL_INTERNAL_DEFAULT)
        self.assertTrue(decision.reason_message)
        self.assertIn("Shadow Mode", decision.reason_message)
        self.assertEqual(decision.engine_version, ENGINE_VERSION)
        self.assertEqual(decision.schema_version, SCHEMA_VERSION)
        self.assertEqual(decision.external_reference, "")
        self.assertIsNone(decision.parent_decision_id)
        self.assertIsNone(decision.override_by_id)
        self.assertEqual(decision.override_reason, "")
        self.assertIsNone(decision.correlation_id)

        # No trader classification anywhere in the snapshot.
        self.assertEqual(
            decision.inputs_snapshot,
            {"symbol": "EUR/USD", "side": "BUY", "qty": 1.0, "merged": False},
        )
        self.assertNotIn("trader_score", str(decision.inputs_snapshot).lower())
        self.assertNotIn("routing_profile", str(decision.inputs_snapshot).lower())

        self.assertEqual(result["routing_decision_id"], decision.decision_id)

    def test_principal_decision_linked_on_position(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )

        decision = RoutingDecision.objects.get()
        pos = Position.objects.get(pk=result["position_id"])
        self.assertEqual(pos.routing_decision_id, decision.id)
        self.assertEqual(decision.position_id, pos.id)

    def test_merge_creates_additional_decision_without_overwriting_principal(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=True)

        r1 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        pos = Position.objects.get(pk=r1["position_id"])
        principal = RoutingDecision.objects.get(pk=pos.routing_decision_id)

        r2 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0900, None, None,
            commission=0.0, new_balance=50000.0,
        )

        self.assertTrue(r2["merged"])
        self.assertEqual(r2["position_id"], pos.id)
        self.assertEqual(RoutingDecision.objects.count(), 2)

        pos.refresh_from_db()
        # Principal untouched by the merge.
        self.assertEqual(pos.routing_decision_id, principal.id)

        increment_decision_id = r2["routing_decision_id"]
        self.assertNotEqual(increment_decision_id, principal.decision_id)
        increment = RoutingDecision.objects.get(decision_id=increment_decision_id)
        self.assertEqual(increment.position_id, pos.id)

    def test_two_increments_produce_two_additional_decisions(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=True)

        r1 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        r2 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0900, None, None,
            commission=0.0, new_balance=50000.0,
        )
        r3 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.1000, None, None,
            commission=0.0, new_balance=50000.0,
        )

        self.assertEqual(RoutingDecision.objects.count(), 3)
        decision_ids = {r1["routing_decision_id"], r2["routing_decision_id"], r3["routing_decision_id"]}
        self.assertEqual(len(decision_ids), 3)   # all three distinct

        pos = Position.objects.get(pk=r1["position_id"])
        self.assertEqual(
            set(RoutingDecision.objects.filter(position=pos).values_list("decision_id", flat=True)),
            decision_ids,
        )
        self.assertEqual(pos.routing_decision.decision_id, r1["routing_decision_id"])

    def test_contract_build_failure_is_fail_open(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        with patch(
            "simulator.routing_engine.build_shadow_mode_decision_contract",
            side_effect=RuntimeError("simulated contract-build failure"),
        ):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["position_id"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)
        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNone(pos.routing_decision_id)

    def test_writer_failure_is_fail_open(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        with patch(
            "simulator.routing_engine.record_routing_decision",
            return_value=None,
        ):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["position_id"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)

    def test_principal_link_failure_rolls_back_decision_leaves_no_ambiguous_row(self):
        """
        The writer succeeds (RoutingDecision would exist), but the
        follow-up Position.routing_decision link raises. The decision
        and the link are one logical unit (explicit nested
        transaction.atomic() savepoint) — a link failure must roll back
        the just-created RoutingDecision too, so no unlinked-but-
        presented-as-successful "principal" row survives to confuse a
        later netting merge. The Position itself still opens normally
        (fail-open preserved at the integration-block level), and the
        outer transaction remains fully usable afterward.
        """
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        original_save = Position.save

        def _save_side_effect(self_pos, *args, **kwargs):
            if kwargs.get("update_fields") == ["routing_decision"]:
                raise RuntimeError("simulated link failure")
            return original_save(self_pos, *args, **kwargs)

        with patch.object(Position, "save", autospec=True, side_effect=_save_side_effect):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        # Position opened fine — fail-open holds.
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["position_id"])

        # No ambiguous RoutingDecision survives this call.
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)

        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNone(pos.routing_decision_id)

        # The outer transaction is still usable — prove it by opening a
        # second, unrelated position right after, outside the patch.
        second = _db_open_sync(
            consumer, "GBP/USD", "BUY", 1.0, 1.2500, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertTrue(second["ok"])

    def test_merge_decision_survives_even_if_a_later_principal_link_would_fail(self):
        """
        Sanity check that the new atomic wrapper does not over-reach:
        a merge's RoutingDecision (no link step at all) is never rolled
        back by this change — only failures inside the wrapped unit
        itself (writer + principal link) can trigger a rollback of that
        SAME call's decision.
        """
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=True)

        r1 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertIsNotNone(r1["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 1)

        r2 = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0900, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertTrue(r2["merged"])
        self.assertIsNotNone(r2["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 2)

    def test_later_trading_flow_failure_rolls_back_decision_atomically(self):
        """
        A failure AFTER the RoutingDecision was already created (here:
        the pre-existing, unrelated LedgerEntry commission write) must
        roll back the ENTIRE transaction — Position and RoutingDecision
        together — never leaving an orphaned decision. This is ordinary
        transaction atomicity, distinct from fail-open.
        """
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        with patch(
            "simulator.consumers.LedgerEntry.objects.create",
            side_effect=RuntimeError("simulated later trading-flow failure"),
        ):
            with self.assertRaises(RuntimeError):
                _db_open_sync(
                    consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                    commission=10.0, new_balance=49990.0,
                )

        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(RoutingDecision.objects.count(), 0)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("50000"))

    def test_legacy_null_position_still_mergeable_principal_stays_null(self):
        """A Position created before this block (routing_decision=NULL,
        the honest backfill value) must still merge normally — the merge
        gets its own new RoutingDecision, but the legacy Position's
        principal is never retroactively fabricated."""
        account = make_account(balance=Decimal("50000"))
        legacy = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY",
            qty=Decimal("1.0"), avg_price=Decimal("1.0800"),
        )
        self.assertIsNone(legacy.routing_decision_id)
        consumer = _FakeConsumer(account.pk, netting_mode=True)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0900, None, None,
            commission=0.0, new_balance=50000.0,
        )

        self.assertTrue(result["merged"])
        self.assertIsNotNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 1)

        legacy.refresh_from_db()
        self.assertIsNone(legacy.routing_decision_id)   # never fabricated retroactively

        increment = RoutingDecision.objects.get()
        self.assertEqual(increment.position_id, legacy.id)

    def test_closing_position_deletes_position_keeps_decision_nulls_link(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        open_result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        decision_pk = RoutingDecision.objects.get().pk
        pos_id = open_result["position_id"]

        pos_mem = {
            "id": pos_id, "symbol": "EUR/USD", "side": "BUY",
            "qty": 1.0, "avg": 1.0800, "sl": None, "tp": None,
            "opened_at": int(time.time()),
        }
        close_result = _close_position_sync(
            pos_mem, account.pk, 1.0900, "manual", 10.0, 50010.0, 50010.0,
        )
        self.assertFalse(close_result.get("already_closed"))

        self.assertFalse(Position.objects.filter(pk=pos_id).exists())
        # Evidence survives — never deleted.
        self.assertTrue(RoutingDecision.objects.filter(pk=decision_pk).exists())
        decision = RoutingDecision.objects.get(pk=decision_pk)
        self.assertIsNone(decision.position_id)   # SET_NULL fired on Position.delete()

    def test_lock_order_unchanged_and_routing_write_happens_after_position(self):
        """
        Structural, backend-independent proof (same technique as
        test_atomic_guard_lock_order.py's QueryOrderStructuralTests — NOT
        by grepping for "FOR UPDATE" in the SQL text, which SQLite never
        emits at all for select_for_update(); the existing lock-order
        suite documents this limitation explicitly). Two things must
        both hold with the flag on:
          1. TradingAccount is still queried before Position — the
             pre-existing BOOK-02/RISK-02 lock order, untouched by this
             block.
          2. The RoutingDecision write happens strictly AFTER the
             Position write — proving the integration runs post-step-9,
             per the approved sequence, and that routing_engine.py never
             pre-empts the existing lock acquisition order.
        """
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        with CaptureQueriesContext(connection) as ctx:
            _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        account_idx = position_idx = routing_idx = None
        for i, q in enumerate(ctx.captured_queries):
            sql = q["sql"]
            sql_upper = sql.strip().upper()
            if account_idx is None and "simulator_tradingaccount" in sql and sql_upper.startswith("SELECT"):
                account_idx = i
            if position_idx is None and "simulator_position" in sql and sql_upper.startswith(("SELECT", "INSERT")):
                position_idx = i
            if routing_idx is None and "simulator_routingdecision" in sql and sql_upper.startswith("INSERT"):
                routing_idx = i

        self.assertIsNotNone(account_idx, "no SELECT against simulator_tradingaccount was captured")
        self.assertIsNotNone(position_idx, "no query against simulator_position was captured")
        self.assertIsNotNone(routing_idx, "no INSERT against simulator_routingdecision was captured")

        self.assertLess(account_idx, position_idx, "TradingAccount must still be locked before Position")
        self.assertLess(position_idx, routing_idx, "RoutingDecision must be written after Position, not before")

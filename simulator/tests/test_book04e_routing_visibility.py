"""
BOOK-04e — Routing Decision Visibility tests.

Covers the denormalized `RoutingDecision.account` field (FASE 2's
approved Option B), the now-mandatory `account_id` on
record_routing_decision(), the single real call site inside
_db_open_position_atomic(), the two query helpers
(routing_decisions_for_account / routing_decisions_for_position), and
the read-only RoutingDecisionAdmin surface.

No test here changes book/reason_code/engine_version/inputs_snapshot/
position/external_reference behavior, locks, atomicity, netting, or
fail-open semantics — all of that is BOOK-04a/04b's scope, unchanged,
and already covered by their own test files.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import site as admin_site
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from market_data.feeds import get_feed_manager
from simulator.admin import RoutingDecisionAdmin
from simulator.consumers import TradingConsumer
from simulator.models import Position, RoutingDecision, TradingAccount
from simulator.routing_engine import (
    Book,
    record_routing_decision,
    routing_decisions_for_account,
    routing_decisions_for_position,
)
from simulator.tasks import _close_position_sync

from .factories import make_account, make_user

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
    """Same minimal stub used by test_book04b_shadow_mode_integration.py."""
    def __init__(self, account_id, netting_mode=False):
        self._db_account_id = account_id
        self.account = {"netting_mode": netting_mode, "spread_pips": 0.0}
        self._feed = get_feed_manager()


class _CleanPriceMixin:
    def setUp(self):
        super().setUp()
        _seed_price("EUR/USD", 1.0800)
        self.addCleanup(_clear_price, "EUR/USD")


# ─────────────────────────────────────────────────────────────────────────
# 1. Model / migration-level behavior
# ─────────────────────────────────────────────────────────────────────────
class ModelFieldTests(TestCase):

    def test_new_decision_saves_account(self):
        account = make_account()
        decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="X", account=account,
        )
        self.assertEqual(decision.account_id, account.id)

    def test_account_can_be_none(self):
        decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X")
        self.assertIsNone(decision.account_id)

    def test_deleting_account_set_nulls_not_deletes_decision(self):
        account = make_account()
        decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="X", account=account,
        )
        account.delete()
        decision.refresh_from_db()
        self.assertIsNone(decision.account_id)
        self.assertTrue(RoutingDecision.objects.filter(pk=decision.pk).exists())

    def test_historical_decision_with_account_none_remains_valid(self):
        """Simulates a row created before BOOK-04e (account never set) —
        no backfill, no automatic population, still a fully usable row."""
        decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="PRE_04E")
        decision.refresh_from_db()
        self.assertIsNone(decision.account_id)
        self.assertEqual(decision.reason_code, "PRE_04E")


# ─────────────────────────────────────────────────────────────────────────
# 2. record_routing_decision() — account_id now mandatory
# ─────────────────────────────────────────────────────────────────────────
class RecordRoutingDecisionAccountTests(TestCase):

    def test_account_id_is_mandatory(self):
        with self.assertRaises(TypeError):
            record_routing_decision(book=Book.INTERNAL, reason_code="X")

    def test_persists_the_given_account_id(self):
        account = make_account()
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="X", account_id=account.id,
        )
        self.assertEqual(decision.account_id, account.id)

    def test_no_extra_query_to_resolve_tradingaccount(self):
        """account_id is stored as-is — no SELECT against TradingAccount
        to validate or resolve it."""
        account = make_account()
        with CaptureQueriesContext(connection) as ctx:
            record_routing_decision(book=Book.INTERNAL, reason_code="X", account_id=account.id)

        queries = [q["sql"].lower() for q in ctx.captured_queries]
        self.assertFalse(any("simulator_tradingaccount" in q for q in queries))

    def test_rest_of_contract_unchanged(self):
        """book/reason_code/engine_version/schema_version/inputs_snapshot/
        position all persist exactly as before — account_id is additive,
        not a replacement for any existing kwarg."""
        account = make_account()
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="X", reason_message="msg",
            inputs_snapshot={"a": 1}, account_id=account.id,
        )
        self.assertEqual(decision.book, Book.INTERNAL)
        self.assertEqual(decision.reason_code, "X")
        self.assertEqual(decision.reason_message, "msg")
        self.assertEqual(decision.inputs_snapshot, {"a": 1})
        self.assertIsNone(decision.position_id)


# ─────────────────────────────────────────────────────────────────────────
# 3. Call site — _db_open_position_atomic()
# ─────────────────────────────────────────────────────────────────────────
@override_settings(ROUTING_ENGINE_ENABLED=True)
class CallSiteTests(_CleanPriceMixin, TestCase):

    def test_new_position_creates_decision_with_correct_account(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )

        decision = RoutingDecision.objects.get(decision_id=result["routing_decision_id"])
        self.assertEqual(decision.account_id, account.id)

    def test_merge_creates_new_decision_same_account(self):
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

        self.assertTrue(r2["merged"])
        d1 = RoutingDecision.objects.get(decision_id=r1["routing_decision_id"])
        d2 = RoutingDecision.objects.get(decision_id=r2["routing_decision_id"])
        self.assertNotEqual(d1.pk, d2.pk)
        self.assertEqual(d1.account_id, account.id)
        self.assertEqual(d2.account_id, account.id)

    @override_settings(ROUTING_ENGINE_ENABLED=False)
    def test_flag_off_creates_no_decision(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        result = _db_open_sync(
            consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
            commission=0.0, new_balance=50000.0,
        )
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)

    def test_writer_failure_is_still_fail_open(self):
        """Same fail-open guarantee as BOOK-04b — adding account_id to
        the call must not change what happens when the writer fails."""
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)

        with patch("simulator.routing_engine.record_routing_decision", side_effect=Exception("boom")):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        self.assertTrue(result["ok"])
        self.assertIsNone(result["routing_decision_id"])
        self.assertEqual(RoutingDecision.objects.count(), 0)
        pos = Position.objects.get(pk=result["position_id"])
        self.assertIsNotNone(pos)


# ─────────────────────────────────────────────────────────────────────────
# 4. Query helpers
# ─────────────────────────────────────────────────────────────────────────
class QueryHelperTests(_CleanPriceMixin, TestCase):

    def test_routing_decisions_for_account_returns_open_position_decisions(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)
        with override_settings(ROUTING_ENGINE_ENABLED=True):
            result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        decisions = routing_decisions_for_account(account.id)
        self.assertEqual(decisions.count(), 1)
        self.assertEqual(decisions.get().decision_id, result["routing_decision_id"])

    def test_routing_decisions_for_account_survives_position_close(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)
        with override_settings(ROUTING_ENGINE_ENABLED=True):
            open_result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )
        pos_id = open_result["position_id"]

        before_close = list(routing_decisions_for_account(account.id).values_list("decision_id", flat=True))

        pos_mem = {
            "id": pos_id, "symbol": "EUR/USD", "side": "BUY",
            "qty": 1.0, "avg": 1.0800, "sl": None, "tp": None,
            "opened_at": int(time.time()),
        }
        _close_position_sync(pos_mem, account.pk, 1.0900, "manual", 10.0, 50010.0, 50010.0)

        after_close = list(routing_decisions_for_account(account.id).values_list("decision_id", flat=True))
        self.assertEqual(before_close, after_close)
        self.assertFalse(Position.objects.filter(pk=pos_id).exists())

    def test_routing_decisions_for_account_excludes_other_accounts(self):
        account_a = make_account(balance=Decimal("50000"))
        account_b = make_account(balance=Decimal("50000"))
        consumer_a = _FakeConsumer(account_a.pk, netting_mode=False)
        with override_settings(ROUTING_ENGINE_ENABLED=True):
            _db_open_sync(
                consumer_a, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )

        self.assertEqual(routing_decisions_for_account(account_a.id).count(), 1)
        self.assertEqual(routing_decisions_for_account(account_b.id).count(), 0)

    def test_routing_decisions_for_position_works_while_open_empty_after_close(self):
        account = make_account(balance=Decimal("50000"))
        consumer = _FakeConsumer(account.pk, netting_mode=False)
        with override_settings(ROUTING_ENGINE_ENABLED=True):
            open_result = _db_open_sync(
                consumer, "EUR/USD", "BUY", 1.0, 1.0800, None, None,
                commission=0.0, new_balance=50000.0,
            )
        pos_id = open_result["position_id"]

        self.assertEqual(routing_decisions_for_position(pos_id).count(), 1)

        pos_mem = {
            "id": pos_id, "symbol": "EUR/USD", "side": "BUY",
            "qty": 1.0, "avg": 1.0800, "sl": None, "tp": None,
            "opened_at": int(time.time()),
        }
        _close_position_sync(pos_mem, account.pk, 1.0900, "manual", 10.0, 50010.0, 50010.0)

        # Documented design (BOOK-04e): SET_NULL on Position.delete()
        # empties this — durable lookups go through
        # routing_decisions_for_account() instead, not a defect.
        self.assertEqual(routing_decisions_for_position(pos_id).count(), 0)

    def test_ordering_is_stable_newest_first(self):
        account = make_account()
        d1 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="A", account=account)
        d2 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="B", account=account)
        results = list(routing_decisions_for_account(account.id))
        self.assertEqual(results[0].pk, d2.pk)
        self.assertEqual(results[1].pk, d1.pk)


# ─────────────────────────────────────────────────────────────────────────
# 5. Admin — read-only surface
# ─────────────────────────────────────────────────────────────────────────
class RoutingDecisionAdminTests(TestCase):

    def test_registered(self):
        self.assertIn(RoutingDecision, admin_site._registry)

    def test_cannot_add(self):
        ma = RoutingDecisionAdmin(RoutingDecision, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = RoutingDecisionAdmin(RoutingDecision, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = RoutingDecisionAdmin(RoutingDecision, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_not_available(self):
        ma = RoutingDecisionAdmin(RoutingDecision, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="book04e_ro_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_account_present_in_list_display(self):
        ma = RoutingDecisionAdmin(RoutingDecision, admin_site)
        self.assertIn("account", ma.list_display)

    def test_all_fields_readonly(self):
        ma = RoutingDecisionAdmin(RoutingDecision, admin_site)
        model_fields = {f.name for f in RoutingDecision._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_changelist_loads_without_error(self):
        account = make_account()
        RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X", account=account)
        staff = make_user(username="book04e_ro_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_routingdecision_changelist")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected_via_permission_check(self):
        staff = make_user(username="book04e_ro_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_routingdecision_add")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_search_by_account_username_does_not_error(self):
        account = make_account()
        RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X", account=account)
        staff = make_user(username="book04e_ro_staff4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_routingdecision_changelist")
        resp = client.get(url, {"q": account.user.username})
        self.assertEqual(resp.status_code, 200)

"""
BOOK-06a — Dealing Desk Foundation tests.

Foundation only: DealingDeskDecision (the future decision record, empty
table until BOOK-06b/06c). No test here builds a decision engine, reads
TraderScore, or touches consumers.py/routing_engine.py/broker_risk.py/
broker_audit.py — that is explicitly out of scope for this block (see
BOOK-06 FASE 0, approved 2026-07-26).
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from simulator.admin import DealingDeskDecisionAdmin
from simulator.models import DealingDeskDecision, LiquidityDecision, Position, RoutingDecision
from simulator.routing_engine import Book

from .factories import make_account, make_user


# ─────────────────────────────────────────────────────────────────────────
# 1. DealingDeskDecision — model
# ─────────────────────────────────────────────────────────────────────────
class DealingDeskDecisionModelTests(TestCase):

    def test_creates_with_minimal_fields(self):
        decision = DealingDeskDecision.objects.create(symbol="EUR/USD")
        self.assertIsNotNone(decision.decision_id)
        self.assertEqual(decision.symbol, "EUR/USD")
        self.assertFalse(decision.is_simulated_hedge)
        self.assertEqual(decision.routing_profile_snapshot, "")
        self.assertEqual(decision.engine_version, 1)
        self.assertEqual(decision.schema_version, 1)
        self.assertIsNotNone(decision.decided_at)
        self.assertIsNone(decision.routing_decision_id)
        self.assertIsNone(decision.position_id)
        self.assertIsNone(decision.liquidity_decision_id)

    def test_creates_with_all_fields(self):
        account = make_account()
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
        )
        position = Position.objects.create(
            account=account, symbol="BTCUSD", side="BUY", avg_price=Decimal("100.00"),
        )
        liquidity_decision = LiquidityDecision.objects.create(
            symbol="BTCUSD", routing_decision=routing_decision,
        )
        decision = DealingDeskDecision.objects.create(
            symbol="BTCUSD",
            routing_decision=routing_decision,
            position=position,
            liquidity_decision=liquidity_decision,
            is_simulated_hedge=True,
            routing_profile_snapshot="HEDGE_CANDIDATE",
        )
        self.assertTrue(decision.is_simulated_hedge)
        self.assertEqual(decision.routing_profile_snapshot, "HEDGE_CANDIDATE")
        self.assertEqual(decision.routing_decision_id, routing_decision.id)
        self.assertEqual(decision.position_id, position.id)
        self.assertEqual(decision.liquidity_decision_id, liquidity_decision.id)

    def test_decision_id_auto_generated_and_unique(self):
        d1 = DealingDeskDecision.objects.create(symbol="EUR/USD")
        d2 = DealingDeskDecision.objects.create(symbol="BTCUSD")
        self.assertIsNotNone(d1.decision_id)
        self.assertIsNotNone(d2.decision_id)
        self.assertNotEqual(d1.decision_id, d2.decision_id)

    def test_no_inputs_snapshot_field_on_model(self):
        """BOOK-06a design review: deliberately deferred until the
        decision engine (BOOK-06b) that would populate it exists — its
        shape is unknown today, so no field is added speculatively."""
        self.assertNotIn("inputs_snapshot", [f.name for f in DealingDeskDecision._meta.fields])

    def test_is_simulated_hedge_is_a_real_boolean_field(self):
        field = DealingDeskDecision._meta.get_field("is_simulated_hedge")
        self.assertEqual(field.get_internal_type(), "BooleanField")

    def test_default_ordering_is_newest_first(self):
        d1 = DealingDeskDecision.objects.create(symbol="A")
        d2 = DealingDeskDecision.objects.create(symbol="B")
        ordered = list(DealingDeskDecision.objects.all())
        self.assertEqual(ordered[0].id, d2.id)
        self.assertEqual(ordered[1].id, d1.id)

    def test_expected_indexes(self):
        index_names = {idx.name for idx in DealingDeskDecision._meta.indexes}
        self.assertIn("ddesk_dec_symbol_ts_idx", index_names)
        self.assertIn("ddesk_dec_routing_ts_idx", index_names)

    def test_linked_to_real_routing_decision_via_fk(self):
        account = make_account()
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
        )
        decision = DealingDeskDecision.objects.create(
            symbol="EUR/USD", routing_decision=routing_decision,
        )
        self.assertEqual(decision.routing_decision_id, routing_decision.id)
        self.assertEqual(list(routing_decision.dealing_desk_decisions.all()), [decision])

    def test_no_unique_constraint_two_calls_create_two_rows(self):
        """Option A, same precedent already closed in BOOK-05d: no
        UniqueConstraint here — the real writer's call pattern is not
        known yet (BOOK-06b/06c)."""
        account = make_account()
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
        )
        DealingDeskDecision.objects.create(symbol="EUR/USD", routing_decision=routing_decision)
        DealingDeskDecision.objects.create(symbol="EUR/USD", routing_decision=routing_decision)
        self.assertEqual(
            DealingDeskDecision.objects.filter(routing_decision=routing_decision).count(), 2,
        )


# ─────────────────────────────────────────────────────────────────────────
# 2. Delete semantics — SET_NULL, evidence survives
# ─────────────────────────────────────────────────────────────────────────
class DeleteSemanticsTests(TestCase):

    def test_deleting_routing_decision_set_nulls_not_deletes_dealing_desk_decision(self):
        routing_decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X")
        decision = DealingDeskDecision.objects.create(symbol="EUR/USD", routing_decision=routing_decision)

        routing_decision.delete()
        decision.refresh_from_db()

        self.assertIsNone(decision.routing_decision_id)
        self.assertTrue(DealingDeskDecision.objects.filter(pk=decision.pk).exists())

    def test_deleting_position_set_nulls_not_deletes_dealing_desk_decision(self):
        account = make_account()
        position = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY", avg_price=Decimal("1.0800"),
        )
        decision = DealingDeskDecision.objects.create(symbol="EUR/USD", position=position)

        position.delete()
        decision.refresh_from_db()

        self.assertIsNone(decision.position_id)
        self.assertTrue(DealingDeskDecision.objects.filter(pk=decision.pk).exists())

    def test_deleting_liquidity_decision_set_nulls_not_deletes_dealing_desk_decision(self):
        liquidity_decision = LiquidityDecision.objects.create(symbol="EUR/USD")
        decision = DealingDeskDecision.objects.create(
            symbol="EUR/USD", liquidity_decision=liquidity_decision,
        )

        liquidity_decision.delete()
        decision.refresh_from_db()

        self.assertIsNone(decision.liquidity_decision_id)
        self.assertTrue(DealingDeskDecision.objects.filter(pk=decision.pk).exists())


# ─────────────────────────────────────────────────────────────────────────
# 3. Architectural rule — RoutingDecision/LiquidityDecision never modified
# ─────────────────────────────────────────────────────────────────────────
class UpstreamDecisionsNeverModifiedTests(TestCase):

    def test_creating_dealing_desk_decision_does_not_change_routing_decision(self):
        account = make_account()
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT",
            reason_message="msg", account=account, engine_version=1, schema_version=1,
            inputs_snapshot={"symbol": "EUR/USD"},
        )
        before = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}

        DealingDeskDecision.objects.create(
            symbol="EUR/USD", routing_decision=routing_decision, is_simulated_hedge=True,
        )

        routing_decision.refresh_from_db()
        after = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}
        self.assertEqual(before, after)

    def test_creating_dealing_desk_decision_does_not_change_liquidity_decision(self):
        liquidity_decision = LiquidityDecision.objects.create(symbol="EUR/USD")
        before = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}

        DealingDeskDecision.objects.create(
            symbol="EUR/USD", liquidity_decision=liquidity_decision, is_simulated_hedge=True,
        )

        liquidity_decision.refresh_from_db()
        after = {f.name: getattr(liquidity_decision, f.name) for f in LiquidityDecision._meta.fields}
        self.assertEqual(before, after)

    def test_book_all_unchanged(self):
        """Book.ALL remains (INTERNAL,) — BOOK-06a does not touch
        routing_engine.py in any way."""
        self.assertEqual(Book.ALL, (Book.INTERNAL,))


# ─────────────────────────────────────────────────────────────────────────
# 4. Admin — DealingDeskDecision (read-only)
# ─────────────────────────────────────────────────────────────────────────
class DealingDeskDecisionAdminTests(TestCase):

    def test_registered(self):
        self.assertIn(DealingDeskDecision, admin_site._registry)

    def test_cannot_add(self):
        ma = DealingDeskDecisionAdmin(DealingDeskDecision, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = DealingDeskDecisionAdmin(DealingDeskDecision, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = DealingDeskDecisionAdmin(DealingDeskDecision, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_not_available(self):
        ma = DealingDeskDecisionAdmin(DealingDeskDecision, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="book06a_ddesk_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_all_fields_readonly(self):
        ma = DealingDeskDecisionAdmin(DealingDeskDecision, admin_site)
        model_fields = {f.name for f in DealingDeskDecision._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_changelist_loads_without_error(self):
        DealingDeskDecision.objects.create(symbol="EUR/USD")
        staff = make_user(username="book06a_ddesk_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_dealingdeskdecision_changelist")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected_via_permission_check(self):
        staff = make_user(username="book06a_ddesk_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_dealingdeskdecision_add")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_detail_view_loads(self):
        decision = DealingDeskDecision.objects.create(symbol="EUR/USD")
        staff = make_user(username="book06a_ddesk_staff4", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_dealingdeskdecision_change", args=[decision.pk])
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)

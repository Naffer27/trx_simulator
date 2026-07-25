"""
BOOK-05a — Liquidity Provider Foundation tests.

Foundation only: LiquidityProvider (plain CRUD config) and
LiquidityDecision (the future simulation record, empty table until
BOOK-05c). No test here builds a hedge calculation, selects a provider,
or touches consumers.py/routing_engine.py/broker_ledger.py/
broker_audit.py — that is explicitly out of scope for this block (see
docs/BOOK_05_IMPLEMENTATION_PLAN.md, BOOK-05a).
"""
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from simulator.admin import LiquidityDecisionAdmin, LiquidityProviderAdmin
from simulator.models import (
    LiquidityDecision,
    LiquidityProvider,
    Position,
    RoutingDecision,
)
from simulator.routing_engine import Book

from .factories import make_account, make_user


# ─────────────────────────────────────────────────────────────────────────
# 1. LiquidityProvider — model
# ─────────────────────────────────────────────────────────────────────────
class LiquidityProviderModelTests(TestCase):

    def test_creates_with_minimal_fields(self):
        provider = LiquidityProvider.objects.create(name="LP-A")
        self.assertEqual(provider.name, "LP-A")
        self.assertEqual(provider.symbols_covered, [])
        self.assertEqual(provider.simulated_spread_markup_pips, Decimal("0.00"))
        self.assertEqual(provider.max_capacity_usd, Decimal("0.00"))
        self.assertFalse(provider.enabled)
        self.assertIsNotNone(provider.created_at)
        self.assertIsNotNone(provider.updated_at)

    def test_symbols_covered_accepts_list(self):
        provider = LiquidityProvider.objects.create(
            name="LP-B", symbols_covered=["EUR/USD", "BTCUSD"],
        )
        provider.refresh_from_db()
        self.assertEqual(provider.symbols_covered, ["EUR/USD", "BTCUSD"])

    def test_enabled_defaults_false(self):
        """Off until explicit activation — same discipline as every flag
        in this project (ROUTING_ENGINE_ENABLED, MARKET_DATA_ROUTER_ENABLED)."""
        provider = LiquidityProvider.objects.create(name="LP-C")
        self.assertFalse(provider.enabled)

    def test_name_unique_constraint(self):
        LiquidityProvider.objects.create(name="LP-Dup")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LiquidityProvider.objects.create(name="LP-Dup")

    def test_updated_at_changes_on_save_created_at_does_not(self):
        provider = LiquidityProvider.objects.create(name="LP-D")
        created_at = provider.created_at
        provider.enabled = True
        provider.save()
        provider.refresh_from_db()
        self.assertEqual(provider.created_at, created_at)
        self.assertGreaterEqual(provider.updated_at, created_at)


# ─────────────────────────────────────────────────────────────────────────
# 2. LiquidityDecision — model
# ─────────────────────────────────────────────────────────────────────────
class LiquidityDecisionModelTests(TestCase):

    def test_creates_with_minimal_fields(self):
        decision = LiquidityDecision.objects.create(symbol="EUR/USD")
        self.assertIsNotNone(decision.decision_id)
        self.assertEqual(decision.symbol, "EUR/USD")
        self.assertEqual(decision.exposure_usd, Decimal("0.00"))
        self.assertEqual(decision.simulated_spread, Decimal("0.00"))
        self.assertEqual(decision.simulated_cost, Decimal("0.00"))
        self.assertEqual(decision.inputs_snapshot, {})
        self.assertEqual(decision.engine_version, 1)
        self.assertEqual(decision.schema_version, 1)
        self.assertIsNotNone(decision.decided_at)
        self.assertIsNone(decision.routing_decision_id)
        self.assertIsNone(decision.provider_id)
        self.assertIsNone(decision.position_id)

    def test_decision_id_auto_generated_and_unique(self):
        d1 = LiquidityDecision.objects.create(symbol="EUR/USD")
        d2 = LiquidityDecision.objects.create(symbol="BTCUSD")
        self.assertIsNotNone(d1.decision_id)
        self.assertIsNotNone(d2.decision_id)
        self.assertNotEqual(d1.decision_id, d2.decision_id)

    def test_no_account_field_on_model(self):
        """FASE 1 resolution: routing_decision.account (BOOK-04e) is
        already durable — no redundant denormalized account field here."""
        self.assertNotIn("account", [f.name for f in LiquidityDecision._meta.fields])

    def test_no_parent_decision_field_on_model(self):
        """No confirmed use case for chaining simulations to each other —
        not replicated speculatively from RoutingDecision.parent_decision."""
        self.assertNotIn("parent_decision", [f.name for f in LiquidityDecision._meta.fields])

    def test_default_ordering_is_newest_first(self):
        d1 = LiquidityDecision.objects.create(symbol="A")
        d2 = LiquidityDecision.objects.create(symbol="B")
        ordered = list(LiquidityDecision.objects.all())
        self.assertEqual(ordered[0].id, d2.id)
        self.assertEqual(ordered[1].id, d1.id)

    def test_linked_to_real_routing_decision_via_fk(self):
        account = make_account()
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
        )
        decision = LiquidityDecision.objects.create(
            symbol="EUR/USD", routing_decision=routing_decision,
        )
        self.assertEqual(decision.routing_decision_id, routing_decision.id)
        self.assertEqual(list(routing_decision.liquidity_decisions.all()), [decision])


# ─────────────────────────────────────────────────────────────────────────
# 3. Delete semantics — SET_NULL, evidence survives
# ─────────────────────────────────────────────────────────────────────────
class DeleteSemanticsTests(TestCase):

    def test_deleting_routing_decision_set_nulls_not_deletes_liquidity_decision(self):
        routing_decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="X")
        decision = LiquidityDecision.objects.create(symbol="EUR/USD", routing_decision=routing_decision)

        routing_decision.delete()
        decision.refresh_from_db()

        self.assertIsNone(decision.routing_decision_id)
        self.assertTrue(LiquidityDecision.objects.filter(pk=decision.pk).exists())

    def test_deleting_provider_set_nulls_not_deletes_liquidity_decision(self):
        provider = LiquidityProvider.objects.create(name="LP-Del")
        decision = LiquidityDecision.objects.create(symbol="EUR/USD", provider=provider)

        provider.delete()
        decision.refresh_from_db()

        self.assertIsNone(decision.provider_id)
        self.assertTrue(LiquidityDecision.objects.filter(pk=decision.pk).exists())

    def test_deleting_position_set_nulls_not_deletes_liquidity_decision(self):
        account = make_account()
        position = Position.objects.create(
            account=account, symbol="EUR/USD", side="BUY", avg_price=Decimal("1.0800"),
        )
        decision = LiquidityDecision.objects.create(symbol="EUR/USD", position=position)

        position.delete()
        decision.refresh_from_db()

        self.assertIsNone(decision.position_id)
        self.assertTrue(LiquidityDecision.objects.filter(pk=decision.pk).exists())


# ─────────────────────────────────────────────────────────────────────────
# 4. Architectural rule — RoutingDecision is never modified
# ─────────────────────────────────────────────────────────────────────────
class RoutingDecisionNeverModifiedTests(TestCase):

    def test_creating_liquidity_decision_does_not_change_routing_decision(self):
        account = make_account()
        routing_decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT",
            reason_message="msg", account=account, engine_version=1, schema_version=1,
            inputs_snapshot={"symbol": "EUR/USD"},
        )
        before = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}

        LiquidityDecision.objects.create(
            symbol="EUR/USD", routing_decision=routing_decision, exposure_usd=Decimal("5000.00"),
        )

        routing_decision.refresh_from_db()
        after = {f.name: getattr(routing_decision, f.name) for f in RoutingDecision._meta.fields}
        self.assertEqual(before, after)


# ─────────────────────────────────────────────────────────────────────────
# 5. Admin — LiquidityProvider (CRUD) / LiquidityDecision (read-only)
# ─────────────────────────────────────────────────────────────────────────
class LiquidityProviderAdminTests(TestCase):

    def test_registered(self):
        self.assertIn(LiquidityProvider, admin_site._registry)

    def test_default_permissions_allow_add_change_delete(self):
        """Plain CRUD — no permission overrides, unlike the read-only admins."""
        ma = LiquidityProviderAdmin(LiquidityProvider, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="book05a_lp_perm_staff", is_staff=True, is_superuser=True)
        self.assertTrue(ma.has_add_permission(request))
        self.assertTrue(ma.has_delete_permission(request))

    def test_changelist_loads_without_error(self):
        LiquidityProvider.objects.create(name="LP-Admin")
        staff = make_user(username="book05a_lp_staff", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_liquidityprovider_changelist")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)


class LiquidityDecisionAdminTests(TestCase):

    def test_registered(self):
        self.assertIn(LiquidityDecision, admin_site._registry)

    def test_cannot_add(self):
        ma = LiquidityDecisionAdmin(LiquidityDecision, admin_site)
        self.assertFalse(ma.has_add_permission(request=None))

    def test_cannot_change(self):
        ma = LiquidityDecisionAdmin(LiquidityDecision, admin_site)
        self.assertFalse(ma.has_change_permission(request=None))

    def test_cannot_delete(self):
        ma = LiquidityDecisionAdmin(LiquidityDecision, admin_site)
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_not_available(self):
        ma = LiquidityDecisionAdmin(LiquidityDecision, admin_site)
        request = RequestFactory().get("/")
        request.user = make_user(username="book05a_ld_staff", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_all_fields_readonly(self):
        ma = LiquidityDecisionAdmin(LiquidityDecision, admin_site)
        model_fields = {f.name for f in LiquidityDecision._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_changelist_loads_without_error(self):
        LiquidityDecision.objects.create(symbol="EUR/USD")
        staff = make_user(username="book05a_ld_staff2", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_liquiditydecision_changelist")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)

    def test_add_view_rejected_via_permission_check(self):
        staff = make_user(username="book05a_ld_staff3", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(staff)
        url = reverse("admin:simulator_liquiditydecision_add")
        resp = client.get(url)
        self.assertEqual(resp.status_code, 403)

"""
BOOK-06h.4 — Rollback Validation & Release Candidate Closure.

This subphase adds ZERO production code — no engine file changes, no
new algorithms, no new queries, no new resolver behavior. It exists
purely to demonstrate, with executable evidence, that the canary
mechanism built across BOOK-06g/06h.1/06h.2/06h.3 can be activated,
operated, and rolled back to the exact official behavior with no side
effects of any kind — the empirical backbone for
docs/BOOK06_CANARY_ROLLBACK_RUNBOOK.md and
docs/BOOK06_RELEASE_CANDIDATE_CLOSEOUT.md.

No test here modifies broker_exposure.py, dealing_desk.py,
broker_risk_shadow.py, models.py, consumers.py, or tasks.py — all of
BOOK-06's engine files remain exactly as BOOK-06h.3 left them. Every
test uses only @override_settings / `with override_settings(...)`
toggling plus reads of already-existing, already-tested surfaces
(validate_new_order(), _resolve_broker_exposure_for_validation(),
broker_exposure_snapshot(), the BOOK-06e admin view).
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import site as admin_site
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from market_data.feeds import get_feed_manager
from simulator import broker_exposure as _exposure
from simulator import broker_risk
from simulator.models import DealingDeskDecision, Position, RoutingDecision
from simulator.routing_engine import Book

from .factories import make_account, make_user


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


def _make_position(account, symbol="BTCUSD", side="BUY", qty="1.0", avg_price="100.00",
                    is_simulated_hedge=None):
    routing_decision = RoutingDecision.objects.create(
        book=Book.INTERNAL, reason_code="TRIVIAL_INTERNAL_DEFAULT", account=account,
    )
    pos = Position.objects.create(
        account=account, symbol=symbol, side=side,
        qty=Decimal(qty), avg_price=Decimal(avg_price),
        routing_decision=routing_decision,
    )
    if is_simulated_hedge is not None:
        DealingDeskDecision.objects.create(
            routing_decision=routing_decision, position=pos, symbol=symbol,
            is_simulated_hedge=is_simulated_hedge, routing_profile_snapshot="HEDGE_CANDIDATE",
        )
    return pos, routing_decision


def _snapshot_fields(snap):
    """The subset of BrokerExposureBreakdown fields relevant to
    identity comparison across a rollback — excludes `generated_at`
    (a timestamp, expected to differ on every call)."""
    return (
        snap.open_position_count, snap.account_count, snap.symbol_count,
        snap.long_quantity, snap.short_quantity, snap.gross_quantity, snap.net_quantity,
        snap.long_notional, snap.short_notional, snap.gross_notional, snap.net_notional,
        snap.margin_used, snap.priced_position_count, snap.unpriced_position_count,
        snap.pricing_coverage_pct,
    )


class Book06h4TestBase(TestCase):
    def setUp(self):
        _seed_price("BTCUSD", 100.0)
        self.addCleanup(_clear_price, "BTCUSD")
        self.canary_account = make_account(balance=Decimal("1000000"))
        self.other_account = make_account(balance=Decimal("1000000"))


# ─────────────────────────────────────────────────────────────────────────
# 1 & 2. OFF -> ON -> OFF produces exactly the same behavior + official
#        snapshot identical before/during/after.
# ─────────────────────────────────────────────────────────────────────────
class RollbackOffOnOffTests(Book06h4TestBase):

    def test_off_on_off_restores_identical_decision(self):
        _make_position(self.other_account, qty="5.0", is_simulated_hedge=False)
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        def _decide():
            return broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
            )

        # OFF (initial)
        decision_before = _decide()

        # ON — canary active for this account, adjusted book used
        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            decision_during = _decide()

        # OFF again (rollback)
        decision_after = _decide()

        self.assertEqual(decision_before.allowed, decision_after.allowed)
        self.assertEqual(decision_before.reason_code, decision_after.reason_code)
        self.assertEqual(decision_before.exposure_after, decision_after.exposure_after)
        self.assertEqual(decision_before.margin_after, decision_after.margin_after)
        self.assertEqual(
            [c.status for c in decision_before.risk_checks],
            [c.status for c in decision_after.risk_checks],
        )
        # The ON call, meanwhile, really did use the adjusted book —
        # confirms this isn't a vacuous "nothing ever changes" test.
        self.assertNotEqual(decision_before.exposure_after, decision_during.exposure_after)

    def test_official_snapshot_identical_before_during_after_rollback(self):
        _make_position(self.other_account, qty="5.0", is_simulated_hedge=False)
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        snapshot_before = _exposure.broker_exposure_snapshot()

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
            )
            snapshot_during = _exposure.broker_exposure_snapshot()

        snapshot_after = _exposure.broker_exposure_snapshot()

        self.assertEqual(_snapshot_fields(snapshot_before), _snapshot_fields(snapshot_during))
        self.assertEqual(_snapshot_fields(snapshot_before), _snapshot_fields(snapshot_after))
        self.assertEqual(_snapshot_fields(snapshot_during), _snapshot_fields(snapshot_after))


# ─────────────────────────────────────────────────────────────────────────
# 3. No caches, no temporary objects, no persistent attributes, no
#    global state survive a rollback.
# ─────────────────────────────────────────────────────────────────────────
class NoResidualStateTests(Book06h4TestBase):

    def test_no_django_cache_touched_during_resolver_calls(self):
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            with patch("django.core.cache.cache.get") as mocked_get, \
                 patch("django.core.cache.cache.set") as mocked_set:
                broker_risk.validate_new_order(
                    account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                    qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
                )
                mocked_get.assert_not_called()
                mocked_set.assert_not_called()

    def test_settings_revert_exactly_after_override_block_exits(self):
        from django.conf import settings

        original_enabled = settings.DEALING_DESK_EXPOSURE_ENABLED
        original_allowlist = settings.DEALING_DESK_EXPOSURE_ACCOUNT_IDS

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({999})):
            self.assertTrue(settings.DEALING_DESK_EXPOSURE_ENABLED)
            self.assertEqual(settings.DEALING_DESK_EXPOSURE_ACCOUNT_IDS, frozenset({999}))

        self.assertEqual(settings.DEALING_DESK_EXPOSURE_ENABLED, original_enabled)
        self.assertEqual(settings.DEALING_DESK_EXPOSURE_ACCOUNT_IDS, original_allowlist)

    def test_official_path_book_carries_no_observability_attribute(self):
        """The `_dealing_desk_observability` attribute BOOK-06h.3
        stashes on the adjusted book is per-instance, never global —
        a book returned by the official path (flag OFF) must never
        carry it, proving there is no leakage across calls."""
        official_book = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertFalse(hasattr(official_book, "_dealing_desk_observability"))

    def test_adjusted_book_attribute_does_not_survive_into_a_fresh_official_call(self):
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            adjusted_book = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertTrue(hasattr(adjusted_book, "_dealing_desk_observability"))

        # Rollback — a brand-new call must return a brand-new object,
        # never the same instance decorated a moment ago.
        official_book_after = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertIsNot(official_book_after, adjusted_book)
        self.assertFalse(hasattr(official_book_after, "_dealing_desk_observability"))

    def test_module_namespace_unchanged_by_flag_toggling(self):
        """No module-level mutable state is introduced anywhere in
        broker_risk.py by toggling the canary — the module's own
        callable/constant surface is identical before and after."""
        before = {k: v for k, v in vars(broker_risk).items() if not k.startswith("__")}

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
            )

        after = {k: v for k, v in vars(broker_risk).items() if not k.startswith("__")}
        self.assertEqual(set(before.keys()), set(after.keys()))


# ─────────────────────────────────────────────────────────────────────────
# 4. allowlist vacío -> cuenta dentro -> cuenta fuera -> OFF, en
#    distintas secuencias.
# ─────────────────────────────────────────────────────────────────────────
class AllowlistSequenceTests(Book06h4TestBase):

    def _official_gross(self):
        return _exposure.broker_exposure_snapshot().gross_quantity

    def test_sequence_empty_inside_outside_off(self):
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)
        official_gross = self._official_gross()

        # 1) allowlist vacío, flag ON
        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset()):
            book1 = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book1.gross_quantity, official_gross)

        # 2) cuenta dentro del allowlist
        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            book2 = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book2.gross_quantity, official_gross - Decimal("3.0"))

        # 3) cuenta fuera del allowlist (allowlist apunta a otra cuenta)
        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.other_account.pk})):
            book3 = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book3.gross_quantity, official_gross)

        # 4) OFF
        book4 = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book4.gross_quantity, official_gross)

    def test_sequence_outside_inside_empty_off(self):
        """Same four states, different order — result must depend only
        on the CURRENT configuration, never on history/order."""
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)
        official_gross = self._official_gross()

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.other_account.pk})):
            book_outside = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book_outside.gross_quantity, official_gross)

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            book_inside = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book_inside.gross_quantity, official_gross - Decimal("3.0"))

        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset()):
            book_empty = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book_empty.gross_quantity, official_gross)

        book_off = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(book_off.gross_quantity, official_gross)


# ─────────────────────────────────────────────────────────────────────────
# 5. OFF -> ON -> OFF -> ON -> OFF, misma decision/límites/resultados
#    en cada estado equivalente.
# ─────────────────────────────────────────────────────────────────────────
class MultipleToggleConsistencyTests(Book06h4TestBase):

    def test_five_step_toggle_yields_consistent_results(self):
        _make_position(self.other_account, qty="5.0", is_simulated_hedge=False)
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)

        def _decide():
            return broker_risk.validate_new_order(
                account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
                qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
            )

        results = []
        sequence = [False, True, False, True, False]
        for enabled in sequence:
            if enabled:
                with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                        DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
                    results.append(_decide())
            else:
                results.append(_decide())

        off_results = [results[0], results[2], results[4]]
        on_results = [results[1], results[3]]

        off_exposure_after = {r.exposure_after for r in off_results}
        on_exposure_after = {r.exposure_after for r in on_results}

        self.assertEqual(len(off_exposure_after), 1, "las 3 corridas OFF deben coincidir exactamente entre sí")
        self.assertEqual(len(on_exposure_after), 1, "las 2 corridas ON deben coincidir exactamente entre sí")
        self.assertNotEqual(off_exposure_after, on_exposure_after)

        for r in results:
            self.assertTrue(r.allowed)
            self.assertIsNone(r.reason_code)


# ─────────────────────────────────────────────────────────────────────────
# 6. fallback -> rollback -> funcionamiento normal.
# ─────────────────────────────────────────────────────────────────────────
class FallbackThenRollbackTests(Book06h4TestBase):

    def test_fallback_then_rollback_then_normal_operation(self):
        _make_position(self.canary_account, qty="3.0", is_simulated_hedge=True)
        official_gross = _exposure.broker_exposure_snapshot().gross_quantity

        # Step 1 — canary ON, but the resolver's own query explodes ->
        # fallback to official, exactly as designed since BOOK-06g.
        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True,
                                DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({self.canary_account.pk})):
            with patch("simulator.models.DealingDeskDecision.objects.filter",
                       side_effect=RuntimeError("simulated failure")):
                fallback_book = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(fallback_book.gross_quantity, official_gross)

        # Step 2 — rollback: flag OFF.
        rollback_book = broker_risk._resolve_broker_exposure_for_validation(self.canary_account.pk)
        self.assertEqual(rollback_book.gross_quantity, official_gross)

        # Step 3 — normal operation resumes: a genuine order validates
        # exactly as it would have before any of this happened.
        decision = broker_risk.validate_new_order(
            account_id=self.canary_account.pk, symbol="BTCUSD", side="BUY",
            qty=Decimal("0.1"), price=Decimal("100.0"), contract_size=Decimal("1"),
        )
        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.reason_code)


# ─────────────────────────────────────────────────────────────────────────
# 7. BOOK-06e — estado visual ON -> OFF sin inconsistencias.
# ─────────────────────────────────────────────────────────────────────────
class CanaryAdminIndicatorToggleTests(TestCase):

    def setUp(self):
        self.superuser = make_user(username="book06h4_super", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)
        self.url = reverse("admin:broker_shadow_exposure")

    def test_indicator_toggles_on_then_off_without_stale_state(self):
        with override_settings(DEALING_DESK_EXPOSURE_ENABLED=True, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=frozenset({1, 2})):
            resp_on = self.client.get(self.url)
        self.assertEqual(resp_on.status_code, 200)
        self.assertTrue(resp_on.context["canary_enabled"])
        self.assertEqual(resp_on.context["canary_account_count"], 2)
        self.assertContains(resp_on, ">ON<")

        # Rollback — next GET, fresh process-level settings (no
        # override active), must show OFF immediately — no cache, no
        # leftover state from the previous request.
        resp_off = self.client.get(self.url)
        self.assertEqual(resp_off.status_code, 200)
        self.assertFalse(resp_off.context["canary_enabled"])
        self.assertContains(resp_off, ">OFF<")

    def test_indicator_multiple_toggles_always_reflects_current_state(self):
        states = [
            (False, frozenset(), "OFF", 0),
            (True, frozenset({5}), "ON", 1),
            (False, frozenset(), "OFF", 0),
            (True, frozenset({5, 6, 7}), "ON", 3),
            (False, frozenset(), "OFF", 0),
        ]
        for enabled, allowlist, expected_label, expected_count in states:
            with override_settings(DEALING_DESK_EXPOSURE_ENABLED=enabled, DEALING_DESK_EXPOSURE_ACCOUNT_IDS=allowlist):
                resp = self.client.get(self.url)
            self.assertEqual(resp.context["canary_enabled"], enabled)
            self.assertEqual(resp.context["canary_account_count"], expected_count)
            self.assertContains(resp, f">{expected_label}<")

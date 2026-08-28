# simulator/tests/test_fix03_margin_call_stopout_policy.py
"""
FIX-03 — Margin Call / Stop-Out Policy + UI Alignment.

Money Broker V1 policy: margin_call_level=100 (order-entry gate, never
closes positions), stopout_level=70 (the only RETAIL liquidation
trigger). Both boundaries are strict '<' in the real runtime
(consumers.py/risk_engine.py, unmodified by this block) — every test
here respects that exactly, never silently switching to '<='.

Reuses the existing async-consumer test harness from
test_close_path_concurrency_parity.py (_consumer/_run/_sent_types/
_pos_entry) rather than re-inventing one — same TransactionTestCase
discipline for genuine cross-call DB visibility.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase

from simulator.admin import TradingAccountAdmin
from simulator.broker_audit import (
    EV_POSITION_CLOSED, EV_POSITION_CLOSED_MARGIN_CALL, EV_POSITION_CLOSED_STOPOUT,
    close_reason_event_type,
)
from simulator.consumers import _compute_pretrade_margin_guard
from simulator.models import AccountProduct, BrokerAuditEvent, Position, Trade, TradingAccount
from simulator.risk_engine import check_equity_stopout
from simulator.tasks import _close_position_sync

from .factories import make_account, make_position, make_user
from .test_close_path_concurrency_parity import _consumer, _pos_entry, _pos_mem_for_task, _run, _sent_types


# ── 1. Margin Call — order-entry gate, exact boundary ───────────────────────

class MarginCallOrderGateBoundaryTests(TestCase):
    """
    _compute_pretrade_margin_guard() check #5 (margin_call_level_breach)
    only evaluates margin_level_after against margin_call_level once
    checks #3/#4 (per-trade / total-margin caps) already passed. Under
    the DEFAULT O.6c-1e caps (max_total_margin_pct=50.0), the algebra
    makes check #5 structurally unreachable: total_margin_pct <= 50
    implies margin_level_after >= 200, which can never breach a 100
    margin_call_level. This is confirmed, not assumed — real finding,
    not a test artifact. To test check #5's OWN boundary in isolation
    (the only honest way to test it at all), max_total_margin_pct is
    passed permissively high here so checks #3/#4 never fire first —
    exactly what a real account/product configured with a looser total-
    margin cap than the O.6c-1e default would experience.
    """

    def test_exactly_100_00_margin_level_after_does_not_breach(self):
        # required_margin = qty*entry_px*contract_size/leverage. Account
        # leverage=1 gives exact control: required_margin = qty*contract_size.
        # Solving qty = equity/contract_size makes required_margin == equity
        # exactly -> margin_level_after = equity/equity*100 = 100.00.
        equity = 1000.0
        qty = equity / 100000.0
        account_snap = {"leverage": 1, "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0}
        ok, code, msg, details = _compute_pretrade_margin_guard(
            "EUR/USD", qty, 1.0, equity, 0.0, account_snap,
            spec_max_leverage=500, spec_contract_size=100000.0,
            max_margin_per_trade_pct=1000.0, max_total_margin_pct=1000.0,
        )
        self.assertAlmostEqual(details["projected_total_margin"], equity, places=6)
        self.assertTrue(ok, f"exactly 100.00 margin_level_after must NOT breach (strict '<'): {code}/{msg}")

    def test_just_below_100_margin_level_after_breaches(self):
        equity = 1000.0
        # required_margin slightly ABOVE equity -> margin_level_after slightly BELOW 100.
        qty = (equity * 1.001) / 100000.0
        account_snap = {"leverage": 1, "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0}
        ok, code, msg, details = _compute_pretrade_margin_guard(
            "EUR/USD", qty, 1.0, equity, 0.0, account_snap,
            spec_max_leverage=500, spec_contract_size=100000.0,
            max_margin_per_trade_pct=1000.0, max_total_margin_pct=1000.0,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "margin_call_level_breach")

    def test_margin_call_never_closes_positions_only_rejects_order(self):
        """Structural — the guard's return contract is (ok, code, message,
        details); it has no DB write, no Position/Trade access at all."""
        import inspect
        src = inspect.getsource(_compute_pretrade_margin_guard)
        self.assertNotIn("_db_close_position_atomic", src)
        self.assertNotIn("Position.objects", src)
        self.assertNotIn(".delete(", src)


# ── 2. Stop-Out — the only RETAIL liquidation trigger, exact boundary ───────

class StopoutTriggerBoundaryTests(TestCase):
    def test_exactly_70_00_does_not_trigger(self):
        # margin_level = equity/margin_used*100 = 70.00 exactly
        margin_used = 100.0
        equity = 70.0
        triggered = check_equity_stopout(
            equity=equity, peak_balance=10000.0, tier="10K",
            account_type="RETAIL", margin_used=margin_used, stopout_level=70.0,
        )
        self.assertFalse(triggered, "exactly 70.00 must NOT trigger stop-out (strict '<')")

    def test_just_below_70_triggers(self):
        margin_used = 100.0
        equity = 69.99
        triggered = check_equity_stopout(
            equity=equity, peak_balance=10000.0, tier="10K",
            account_type="RETAIL", margin_used=margin_used, stopout_level=70.0,
        )
        self.assertTrue(triggered)

    def test_zero_margin_used_never_stops_out(self):
        triggered = check_equity_stopout(
            equity=0.0, peak_balance=10000.0, tier="10K",
            account_type="RETAIL", margin_used=0.0, stopout_level=70.0,
        )
        self.assertFalse(triggered, "no open positions -> no margin call/stop-out possible")


# ── 3. Event semantics — WS type, close reason, audit ───────────────────────

class RetailLiquidationEventSemanticsTests(TransactionTestCase):
    def test_ws_event_is_account_stopout_not_margin_call(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        consumer.account["stopout_level"] = 70.0
        _run(consumer._do_retail_liquidation())

        types = _sent_types(consumer)
        self.assertIn("account:stopout", types)
        self.assertNotIn("account:margin_call", types)

    def test_close_reason_is_stopout(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        _run(consumer._do_retail_liquidation())

        # Trade has no `reason` field — the close reason is carried in
        # BrokerAuditEvent.metadata["reason"] (broker_ledger.py's single
        # record_trade_event() writer) and BrokerLedger.meta["close_reason"].
        event = BrokerAuditEvent.objects.get(event_type=EV_POSITION_CLOSED_STOPOUT, account=account)
        self.assertEqual(event.metadata["reason"], "stopout")

    def test_audit_event_is_stopout_not_generic_not_margin_call(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        _run(consumer._do_retail_liquidation())

        self.assertTrue(
            BrokerAuditEvent.objects.filter(
                event_type=EV_POSITION_CLOSED_STOPOUT, account=account,
            ).exists(),
            "expected EV_POSITION_CLOSED_STOPOUT audit row",
        )
        self.assertFalse(
            BrokerAuditEvent.objects.filter(
                event_type=EV_POSITION_CLOSED_MARGIN_CALL, account=account,
            ).exists(),
        )
        self.assertFalse(
            BrokerAuditEvent.objects.filter(
                event_type=EV_POSITION_CLOSED, account=account,
            ).exists(),
            "must not fall back to the generic close event",
        )

    def test_close_reason_event_type_mapping_direct(self):
        self.assertEqual(close_reason_event_type("stopout"), EV_POSITION_CLOSED_STOPOUT)

    def test_retail_account_stays_active_after_liquidation(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        _run(consumer._do_retail_liquidation())
        self.assertEqual(consumer.account["status"], "Activo")


# ── 4. Payload — margin_call_level/stopout_level are snapshot-authoritative ─

class AccountUpdatePayloadThresholdTests(TransactionTestCase):
    def test_liquidation_payload_carries_real_account_thresholds_not_hardcoded(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        # Simulate a LEGACY account whose frozen snapshot is still 100/50
        # (pre-V1), while the product currently on file is 100/70 — the
        # payload must reflect the account's OWN snapshot, never the
        # live product.
        consumer.account["margin_call_level"] = 100.0
        consumer.account["stopout_level"] = 50.0
        _run(consumer._do_retail_liquidation())

        updates = [c.args[0] for c in consumer.send_json.call_args_list if c.args[0].get("type") == "account:update"]
        self.assertTrue(updates)
        last = updates[-1]
        self.assertEqual(last["margin_call_level"], 100.0)
        self.assertEqual(last["stopout_level"], 50.0)
        self.assertNotEqual(last["stopout_level"], 70.0, "must reflect THIS account's snapshot, not the live product's current 70")


# ── 5. Snapshot / no-retroactivity ───────────────────────────────────────────

class SnapshotNoRetroactivityTests(TestCase):
    def test_legacy_account_snapshot_50_untouched_by_new_product_default(self):
        product = AccountProduct.objects.create(
            code="fix03-test-legacy", name="Legacy Test", product_type=AccountProduct.TYPE_STANDARD,
            margin_call_level=Decimal("100.00"), stopout_level=Decimal("70.00"),
        )
        account = make_account(
            balance=Decimal("10000.00"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("50.00"),  # frozen BEFORE the product moved to 70
            account_product=product,
        )
        account.refresh_from_db()
        self.assertEqual(account.stopout_level_snapshot, Decimal("50.00"))
        # Product now says 70 — must have zero effect on the already-created account.
        self.assertEqual(product.stopout_level, Decimal("70.00"))

    def test_new_account_from_v1_product_gets_70(self):
        product = AccountProduct.objects.create(
            code="fix03-test-v1", name="V1 Test", product_type=AccountProduct.TYPE_STANDARD,
            margin_call_level=Decimal("100.00"), stopout_level=Decimal("70.00"),
        )
        account = make_account(
            balance=Decimal("10000.00"),
            margin_call_level_snapshot=product.margin_call_level,
            stopout_level_snapshot=product.stopout_level,
            account_product=product,
        )
        self.assertEqual(account.stopout_level_snapshot, Decimal("70.00"))

    def test_model_default_is_70_for_product_without_explicit_value(self):
        product = AccountProduct.objects.create(
            code="fix03-test-default", name="Default Test", product_type=AccountProduct.TYPE_STANDARD,
        )
        self.assertEqual(product.stopout_level, Decimal("70.00"))
        self.assertEqual(product.margin_call_level, Decimal("100.00"))


class SeedCommandSnapshotTests(TestCase):
    """Uses the real management command against the TEST DB only — never
    --force-update against real data (not applicable here: this IS the
    ephemeral test database)."""

    def test_fresh_seed_creates_products_with_stopout_70(self):
        from django.core.management import call_command
        call_command("seed_account_products")
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            product = AccountProduct.objects.get(code=code)
            self.assertEqual(product.stopout_level, Decimal("70.00"), code)
            self.assertEqual(product.margin_call_level, Decimal("100.00"), code)

    def test_reseed_without_force_update_does_not_touch_existing_row(self):
        from django.core.management import call_command
        call_command("seed_account_products")
        product = AccountProduct.objects.get(code="demo-standard")
        AccountProduct.objects.filter(pk=product.pk).update(stopout_level=Decimal("50.00"))
        call_command("seed_account_products")  # no --force-update
        product.refresh_from_db()
        self.assertEqual(product.stopout_level, Decimal("50.00"), "must be left alone without --force-update")

    def test_reseed_with_force_update_updates_existing_row_in_test_db(self):
        from django.core.management import call_command
        call_command("seed_account_products")
        product = AccountProduct.objects.get(code="demo-standard")
        AccountProduct.objects.filter(pk=product.pk).update(stopout_level=Decimal("50.00"))
        call_command("seed_account_products", force_update=True)
        product.refresh_from_db()
        self.assertEqual(product.stopout_level, Decimal("70.00"))

    def test_account_created_before_reseed_keeps_its_own_snapshot(self):
        from django.core.management import call_command
        call_command("seed_account_products")
        product = AccountProduct.objects.get(code="real-standard")
        account = make_account(
            balance=Decimal("10000.00"),
            margin_call_level_snapshot=product.margin_call_level,
            stopout_level_snapshot=product.stopout_level,
            account_product=product,
        )
        self.assertEqual(account.stopout_level_snapshot, Decimal("70.00"))

        # Product changes again (simulating a future policy change) —
        # already-created account must not move.
        call_command("seed_account_products", force_update=True)
        AccountProduct.objects.filter(pk=product.pk).update(stopout_level=Decimal("80.00"))
        account.refresh_from_db()
        self.assertEqual(account.stopout_level_snapshot, Decimal("70.00"))


# ── 6. Golden scenario — EURUSD, $10,000, 1:500, MCL=100/SOL=70 ─────────────

class GoldenScenarioMCL100SOL70Tests(TransactionTestCase):
    """Deterministic — no external market data. Uses the same
    _close_position_sync (daemon primitive) the rest of the repo's
    concurrency tests already rely on for a fully synchronous, real-DB
    close, avoiding any dependency on FeedManager/live prices."""

    def test_above_100_normal_below_100_order_blocked_below_70_stopout(self):
        balance = Decimal("10000.00")
        account = make_account(
            balance=balance, account_type="RETAIL",
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        # make_account() hardcodes leverage=50 internally (not an override
        # param) — set the golden scenario's 1:500 directly.
        TradingAccount.objects.filter(pk=account.pk).update(leverage=500)
        account.refresh_from_db()
        # 1 lot EUR/USD notional at leverage 500: required_margin = 1*entry*100000/500 = 220 @ 1.1000
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("1.1000"))
        equity_at_open = float(balance)  # no floating PnL yet
        margin_used = 1.0 * 1.1000 * 100000.0 / 500.0  # 220.0
        margin_level_at_open = equity_at_open / margin_used * 100.0
        self.assertGreater(margin_level_at_open, 100.0, "sanity: fresh position, well above margin call")

        # ── Order-entry gate check at various equity levels (no DB close needed) ──
        account_snap = {
            "leverage": 500, "allowed_symbols": None, "max_lot_size": None,
            "margin_call_level": 100.0,
        }
        # Below 100%, above 70%: new order blocked, existing position untouched.
        ok, code, _msg, _d = _compute_pretrade_margin_guard(
            "EUR/USD", 0.01, 1.1000, equity=margin_used * 0.90, margin_used_now=margin_used,
            account_snap=account_snap, spec_max_leverage=500, spec_contract_size=100000.0,
            max_margin_per_trade_pct=1000.0, max_total_margin_pct=1000.0,
        )
        self.assertFalse(ok, "new order must be rejected below margin_call_level")
        pos.refresh_from_db()
        self.assertIsNotNone(pos.pk, "existing position must remain open — gate never touches it")

        # ── Below 70%: stop-out, liquidation total ──
        consumer = _consumer(account.pk, balance=float(balance), positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        consumer.account["margin_call_level"] = 100.0
        consumer.account["stopout_level"] = 70.0
        # Force equity below the 70% stop-out line directly (deterministic,
        # no price feed dependency) — mirrors how consumers.py itself
        # computes equity before calling check_equity_stopout().
        consumer.account["equity"] = margin_used * 0.60
        consumer.account["margin_used"] = margin_used
        triggered = check_equity_stopout(
            equity=consumer.account["equity"], peak_balance=float(balance), tier="10K",
            account_type="RETAIL", margin_used=margin_used, stopout_level=70.0,
        )
        self.assertTrue(triggered)

        _run(consumer._do_retail_liquidation())

        # ── After: closed, no double close, margin_used=0, Activo, stopout everywhere ──
        self.assertEqual(Position.objects.filter(account=account).count(), 0)
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertEqual(consumer.account["margin_used"], 0.0)
        self.assertEqual(consumer.account["status"], "Activo")
        event = BrokerAuditEvent.objects.get(event_type=EV_POSITION_CLOSED_STOPOUT, account=account)
        self.assertEqual(event.metadata["reason"], "stopout")
        self.assertIn("account:stopout", _sent_types(consumer))
        self.assertNotIn("account:margin_call", _sent_types(consumer))


# ── 7. Concurrency regression — daemon vs WS-live, corrected reason ─────────

class RetailLiquidationVsDaemonReasonRegressionTests(TransactionTestCase):
    """FIX-03 variant of the existing RetailLiquidationVsDaemonConcurrentTests
    (test_close_path_concurrency_parity.py) — same already_closed guard,
    now asserting the CORRECTED reason/event survive the race too."""

    def test_daemon_closes_first_ws_liquidation_is_noop_reason_still_correct(self):
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.1000"))
        pos_mem = _pos_mem_for_task(pos)
        # Daemon closes it first (simulates scan_positions_task beating the WS tick).
        _close_position_sync(pos_mem, account.pk, 1.0800, "daemon_margin_call", -20.0, 9980.0, 9980.0)
        account.refresh_from_db()

        consumer = _consumer(account.pk, balance=10000.0, positions=[_pos_entry(pos)],
                              account_type="RETAIL")
        _run(consumer._do_retail_liquidation())

        # No second Trade, no fabricated order_close, balance authoritative from the daemon's close.
        self.assertEqual(Trade.objects.filter(account=account).count(), 1)
        self.assertNotIn("order_close", _sent_types(consumer))
        self.assertEqual(account.balance, Decimal("9980.00"))
        # The one real close audit event still carries the daemon's own
        # reason ("daemon_margin_call" — tasks.py, out of FIX-03's
        # authorized scope) — confirms the WS-side reason fix never
        # overwrote it; this reason maps to EV_POSITION_CLOSED_MARGIN_CALL,
        # unaffected by this block (the fix only touched the WS-live
        # "margin_call" -> "stopout" reason string, never "daemon_margin_call").
        event = BrokerAuditEvent.objects.get(event_type=EV_POSITION_CLOSED_MARGIN_CALL, account=account)
        self.assertEqual(event.metadata["reason"], "daemon_margin_call")


# ── 8. Admin — read-only snapshot visibility ────────────────────────────────

class AdminSnapshotVisibilityTests(TestCase):
    def test_readonly_fields_include_both_snapshots(self):
        from django.contrib.admin.sites import site
        ma = TradingAccountAdmin(TradingAccount, site)
        self.assertIn("margin_call_level_snapshot", ma.readonly_fields)
        self.assertIn("stopout_level_snapshot", ma.readonly_fields)

    def test_change_fieldsets_include_both_snapshots(self):
        from django.contrib.admin.sites import site
        ma = TradingAccountAdmin(TradingAccount, site)
        all_fields = [f for _, opts in ma._CHANGE_FIELDSETS for f in opts["fields"]]
        self.assertIn("margin_call_level_snapshot", all_fields)
        self.assertIn("stopout_level_snapshot", all_fields)

    def test_add_fieldsets_do_not_include_snapshots(self):
        """New account has no snapshot yet (frozen later, at creation
        time, from the chosen product) — must not appear on the add form."""
        from django.contrib.admin.sites import site
        ma = TradingAccountAdmin(TradingAccount, site)
        add_fields = [f for _, opts in ma._ADD_FIELDSETS for f in opts["fields"]]
        self.assertNotIn("margin_call_level_snapshot", add_fields)
        self.assertNotIn("stopout_level_snapshot", add_fields)

    def test_get_readonly_fields_on_change_form_still_includes_snapshots(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        ma = TradingAccountAdmin(TradingAccount, site)
        request = RequestFactory().get("/")
        request.user = make_user(username="fix03_admin_super", is_staff=True, is_superuser=True)
        account = make_account(balance=Decimal("1000.00"))
        fields = ma.get_readonly_fields(request, obj=account)
        self.assertIn("margin_call_level_snapshot", fields)
        self.assertIn("stopout_level_snapshot", fields)


# ── 9. Frontend contract — textual, same pattern already used in the repo
#      (test_o3d6_treasury_operational_hardening_end_to_end.py::
#      test_no_mutating_action_reachable_from_the_dashboard_html) ──────────

class DashboardTemplateContractTests(TestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_no_leftover_150_300_margin_level_thresholds(self):
        src = self._template_source()
        self.assertNotIn("ml<150", src)
        self.assertNotIn("ml < 100 ? ' danger' : ml < 200", src)

    def test_js_reads_authoritative_thresholds_from_payload(self):
        src = self._template_source()
        self.assertIn("msg.margin_call_level", src)
        self.assertIn("msg.stopout_level", src)

    def test_account_stopout_handler_present(self):
        src = self._template_source()
        self.assertIn("account:stopout", src)

    def test_no_account_margin_call_handler_expected(self):
        src = self._template_source()
        self.assertNotIn("account:margin_call", src)

    def test_visual_boundary_matches_backend_contract(self):
        """stopout_level <= ml <= margin_call_level -> warning band;
        ml < stopout_level -> critical; both use '<'/'<=' consistent with
        the Design Lock's exact boundary table, not backend-inequivalent '<=' everywhere."""
        src = self._template_source()
        self.assertIn("ml<_sol", src.replace(" ", ""))
        self.assertIn("ml<=_mcl", src.replace(" ", ""))

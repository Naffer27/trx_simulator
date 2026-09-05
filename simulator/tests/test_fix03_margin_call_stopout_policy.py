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
from unittest.mock import AsyncMock, patch

from django.db import connection
from django.test import TestCase, TransactionTestCase

from simulator.admin import TradingAccountAdmin
from simulator.broker_audit import (
    EV_POSITION_CLOSED, EV_POSITION_CLOSED_MARGIN_CALL, EV_POSITION_CLOSED_STOPOUT,
    close_reason_event_type,
)
from simulator.consumers import TradingConsumer, _compute_pretrade_margin_guard
from simulator.models import (
    AccountProduct, BrokerAuditEvent, LedgerEntry, Position, Trade, TradingAccount,
)
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


# ── 5b. GOLDEN-STOPOUT-FIX-01 — data migration 0074 (product drift fix) ─────
#
# 0072 changed AccountProduct.stopout_level's model FIELD DEFAULT to 70 —
# but that only affects new rows created without an explicit value; the 4
# already-seeded MVP rows kept their pre-0072 value (50) because
# seed_account_products is deliberately idempotent (skips existing `code`s
# unless --force-update). Migration 0074 is the one-time, narrowly-scoped
# correction: only AccountProduct.stopout_level, only the 4 known MVP
# codes, only rows currently at exactly 50.00 — TradingAccount is never
# referenced. Tested here by importing the migration's own forward/reverse
# functions directly (RunPython callables take only (apps, schema_editor);
# passing the live app registry and schema_editor=None is safe for a pure
# data migration with no schema operations) rather than driving a full
# MigrationExecutor — same shape, far less test machinery.
class Migration0074StopoutDriftFixTests(TestCase):
    def setUp(self):
        import importlib
        from django.apps import apps as live_apps
        self._forward = importlib.import_module(
            "simulator.migrations.0074_fix_account_product_stopout_70"
        )._set_stopout_70
        self._reverse = importlib.import_module(
            "simulator.migrations.0074_fix_account_product_stopout_70"
        )._revert_stopout_50
        self._apps = live_apps

    def _make_product(self, code, **overrides):
        fields = dict(
            code=code, name=code, product_type=AccountProduct.TYPE_STANDARD,
            margin_call_level=Decimal("100.00"), stopout_level=Decimal("50.00"),
            max_leverage=100, typical_spread_pips=Decimal("1.20"),
            commission_per_lot=Decimal("0.00"),
            max_margin_per_trade_pct=Decimal("10.00"),
            max_total_margin_pct=Decimal("50.00"),
        )
        fields.update(overrides)
        return AccountProduct.objects.create(**fields)

    def test_forward_moves_all_four_mvp_products_50_to_70(self):
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self._make_product(code)
        self._forward(self._apps, None)
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self.assertEqual(
                AccountProduct.objects.get(code=code).stopout_level, Decimal("70.00"), code
            )

    def test_forward_does_not_touch_other_fields(self):
        p = self._make_product("real-ecn", commission_per_lot=Decimal("7.00"))
        before = (
            p.margin_call_level, p.max_leverage, p.typical_spread_pips,
            p.commission_per_lot, p.max_margin_per_trade_pct, p.max_total_margin_pct,
        )
        self._forward(self._apps, None)
        p.refresh_from_db()
        after = (
            p.margin_call_level, p.max_leverage, p.typical_spread_pips,
            p.commission_per_lot, p.max_margin_per_trade_pct, p.max_total_margin_pct,
        )
        self.assertEqual(before, after)
        self.assertEqual(p.stopout_level, Decimal("70.00"))  # sanity: the intended field DID move

    def test_forward_does_not_touch_custom_product(self):
        custom = self._make_product("premium-custom")
        self._forward(self._apps, None)
        custom.refresh_from_db()
        self.assertEqual(custom.stopout_level, Decimal("50.00"))

    def test_forward_does_not_touch_mvp_product_already_at_non_50_value(self):
        manually_corrected = self._make_product("demo-standard", stopout_level=Decimal("65.00"))
        self._forward(self._apps, None)
        manually_corrected.refresh_from_db()
        self.assertEqual(manually_corrected.stopout_level, Decimal("65.00"))

    def test_reverse_moves_all_four_mvp_products_70_to_50(self):
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self._make_product(code, stopout_level=Decimal("70.00"))
        self._reverse(self._apps, None)
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self.assertEqual(
                AccountProduct.objects.get(code=code).stopout_level, Decimal("50.00"), code
            )

    def test_reverse_does_not_touch_custom_product_at_70(self):
        custom = self._make_product("premium-custom", stopout_level=Decimal("70.00"))
        self._reverse(self._apps, None)
        custom.refresh_from_db()
        self.assertEqual(custom.stopout_level, Decimal("70.00"))

    def test_existing_account_snapshot_50_unaffected_by_forward(self):
        product = self._make_product("real-standard")
        account = make_account(
            balance=Decimal("10000.00"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("50.00"),
            account_product=product,
        )
        self._forward(self._apps, None)
        account.refresh_from_db()
        self.assertEqual(account.stopout_level_snapshot, Decimal("50.00"))

    def test_existing_account_snapshot_none_unaffected_by_forward(self):
        # GOLDEN-STOPOUT-FIX-01 boundary — the None-fallback finding is
        # explicitly out of scope here; this only confirms the migration
        # itself (which never references TradingAccount at all) leaves it
        # exactly as-is, not that None is somehow "correct".
        product = self._make_product("real-ecn")
        account = make_account(balance=Decimal("10000.00"), account_product=product)
        TradingAccount.objects.filter(pk=account.pk).update(stopout_level_snapshot=None)
        self._forward(self._apps, None)
        account.refresh_from_db()
        self.assertIsNone(account.stopout_level_snapshot)

    def test_new_account_after_forward_gets_snapshot_70(self):
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self._make_product(code)
        self._forward(self._apps, None)
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            product = AccountProduct.objects.get(code=code)
            account = make_account(
                balance=Decimal("10000.00"),
                margin_call_level_snapshot=product.margin_call_level,
                stopout_level_snapshot=product.stopout_level,
                account_product=product,
            )
            self.assertEqual(account.stopout_level_snapshot, Decimal("70.00"), code)

    def test_seed_command_still_idempotent_after_forward(self):
        from django.core.management import call_command
        call_command("seed_account_products")  # real 4 rows, real seed source (already 70)
        for p in AccountProduct.objects.filter(code__in=(
            "demo-standard", "demo-ecn", "real-standard", "real-ecn",
        )):
            p.stopout_level = Decimal("50.00")
            p.save(update_fields=["stopout_level"])
        self._forward(self._apps, None)
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self.assertEqual(AccountProduct.objects.get(code=code).stopout_level, Decimal("70.00"))
        call_command("seed_account_products")  # no --force-update: must skip, not touch anything
        for code in ("demo-standard", "demo-ecn", "real-standard", "real-ecn"):
            self.assertEqual(
                AccountProduct.objects.get(code=code).stopout_level, Decimal("70.00"),
                f"{code} must remain untouched by a non-forced reseed",
            )


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
        # ORDER-MANAGEMENT-V2B TEST HARNESS ALIGNMENT — realized_pnl is now
        # recomputed authoritatively under lock (see consumers.py::
        # _db_close_position_atomic's docstring); qty=0.01 (not the
        # original 0.1) so the REAL formula ((1.08-1.10)*qty*100000)
        # produces exactly the -20.00 this test already asserts, instead
        # of the synthetic -20.0 the old "trust the caller" contract
        # allowed regardless of qty. Guarantee under test (already_closed
        # no-op preserves the daemon's own reason/audit event) unchanged.
        account = make_account(balance=Decimal("10000.00"), status="Activo")
        pos = make_position(account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("1.1000"))
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


# ── 10. GOLDEN-STOPOUT-01C — Account 55's REAL config (10%/50%), deterministic ──
#
# GOLDEN-STOPOUT-01A/01B found the earlier Golden Scenario design (2
# positions at 25% each) had used real-standard's max_margin_per_trade_pct
# (25%) while account 55 actually runs demo-standard (10%/50%) — that
# 2-position design would be rejected outright by gate 3 for this account.
# This section re-derives and certifies the scenario against account 55's
# OWN verified live snapshot (margin_call=100/stopout=70/max_per_trade=10/
# max_total=50/leverage=100), and against a genuinely deterministic
# mechanism — 01B found a direct Redis price injection unsafe (the
# trx:price:*:{symbol} keys are global by symbol, not scoped by account or
# provider; the daemon's scan_positions_task would see it too; and the
# Finnhub feed for EUR/USD reactivates and can overwrite it the moment a
# real position is opened, per feeds.py's position-symbol keepalive) — so
# the Stop-Out TRIGGER here is exercised via the account's own forced
# equity/margin_used, the SAME backend-controlled mechanism
# GoldenScenarioMCL100SOL70Tests (section 6, above) already uses and this
# repo has already relied on.
#
# Positions are opened through the REAL, authoritative order-open path
# (TradingConsumer._db_open_position_atomic — the same primitive every
# other atomic-guard/concurrency test in this repo drives directly via
# its .__wrapped__ escape from @database_sync_to_async), never the
# make_position() factory — so every one of the 5 opens is actually
# gated by _compute_atomic_open_guard exactly as production would be.

_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


def _seed_global_feed_price(symbol, price):
    """RISK-02 (broker_risk.validate_new_order) reads broker-wide price
    coverage from the REAL process-global FeedManager singleton
    (get_feed_manager()), never from a per-connection self._feed stub —
    confirmed empirically (test_broker_risk_limits_engine.py's own
    _seed_price/_clear_price pair uses this exact mechanism, and this
    section's first draft failed with RISK_PRICING_INCOMPLETE without
    it). Must be paired with _clear_global_feed_price in tearDown — the
    singleton outlives this TransactionTestCase's DB flush."""
    import time as _time
    from market_data.feeds import get_feed_manager
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = _time.time()


def _clear_global_feed_price(symbol):
    from market_data.feeds import get_feed_manager
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


class _FlatFeed116:
    """Fixed EUR/USD bid=ask=1.16000 — no spread, no movement. Needed so
    _db_open_position_atomic's existing-position repricing and
    _do_retail_liquidation's close price are well-defined; the Stop-Out
    TRIGGER itself is exercised via the account's own forced equity/
    margin_used (see the module note above), never by this feed moving —
    01B found a real ~150-pip market move is not practical to reproduce
    deterministically in a test. Same interface as
    test_close_path_concurrency_parity._FakeFeed, fixed at 1.16000
    instead of 1.1000 to match this scenario's entry price."""
    def has_price(self, symbol): return True
    def last_bid(self, symbol): return 1.16000
    def last_ask(self, symbol): return 1.16000
    def mark_position_symbol(self, symbol): pass
    def sync_position_symbol_from_db(self, symbol): pass
    def get_validated_quote(self, symbol):
        from market_data.feeds import Quote
        return Quote(symbol=symbol, bid=1.16000, ask=1.16000, mid=1.16000,
                     timestamp=0.0, source="fake")


def _golden55_consumer(account):
    """Bare TradingConsumer stub carrying account 55's REAL verified
    snapshot values (margin_call=100/stopout=70/max_per_trade=10/
    max_total=50/leverage=100/allowed_symbols=None/max_lot_size=None) —
    never this module's permissive defaults. Mirrors
    test_close_path_concurrency_parity._consumer()'s field set (proven
    sufficient for both _db_open_position_atomic and
    _do_retail_liquidation) with EUR/USD fixed at 1.16000."""
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account.pk
    c._last_db_sync = 0.0
    c._positions = []
    c._daily_realized_pnl = 0.0
    c._daily_pnl_date = None
    c.symbol = "EUR/USD"
    c._bid_state, c._ask_state = {"EUR/USD": 1.16000}, {"EUR/USD": 1.16000}
    c._raw_bid_state, c._raw_ask_state = {}, {}
    c._pricing_snapshot_state, c._pricing_ts_state = {}, {}
    c._feed = _FlatFeed116()
    balance = float(account.balance)
    c.account = {
        "balance": balance, "equity": balance,
        "peak_balance": balance, "pnl_unreal": 0.0, "margin_used": 0.0,
        "leverage": 100, "currency": "USD", "netting_mode": False,
        "status": "Activo", "account_type": "DEMO", "tier": "10K",
        "profit_target": 0.0, "initial_balance": balance,
        "product_name": "demo-standard", "commission_per_lot": 0.0, "commission_pct": 0.0,
        "spread_pips": 0.0, "allowed_symbols": None, "max_lot_size": None,
        "margin_call_level": 100.0, "stopout_level": 70.0,
        "max_margin_per_trade_pct": 10.0, "max_total_margin_pct": 50.0,
        "commercial_pricing_fields": {},
    }
    c.send_json = AsyncMock()
    return c


class GoldenStopout01CAccount55RealConfigTests(TransactionTestCase):
    """GOLDEN-STOPOUT-01C — deterministic, account-55-faithful Stop-Out
    scenario: 5 x BUY EUR/USD, qty=0.86 each, entry=1.16000, under the
    account's REAL max_margin_per_trade_pct=10%/max_total_margin_pct=50%
    (not the inapplicable real-standard 25% the earlier design used)."""

    ENTRY_PX = 1.16000
    QTY = Decimal("0.86")

    def setUp(self):
        super().setUp()
        _seed_global_feed_price("EUR/USD", self.ENTRY_PX)

    def tearDown(self):
        _clear_global_feed_price("EUR/USD")
        super().tearDown()

    def _make_golden_account(self):
        # ── 2. AccountProduct / snapshots — demo-standard equivalent ──
        product = AccountProduct.objects.create(
            code="golden55-demo-standard-equiv", name="Golden Demo Standard Equivalent",
            product_type=AccountProduct.TYPE_STANDARD,
            margin_call_level=Decimal("100.00"), stopout_level=Decimal("70.00"),
            max_leverage=100, typical_spread_pips=Decimal("1.20"),
            commission_per_lot=Decimal("0.00"),
            max_margin_per_trade_pct=Decimal("10.00"),
            max_total_margin_pct=Decimal("50.00"),
        )
        account = make_account(
            balance=Decimal("10000.62"), account_type="DEMO", status="Activo",
            leverage_snapshot=100,
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
            max_margin_per_trade_pct_snapshot=Decimal("10.00"),
            max_total_margin_pct_snapshot=Decimal("50.00"),
            allowed_symbols_snapshot=None, max_lot_size_snapshot=None,
            account_product=product,
        )
        # make_account() hardcodes leverage=50 internally (not an override
        # param, same limitation GoldenScenarioMCL100SOL70Tests already
        # worked around) — set account 55's real leverage=100 directly.
        TradingAccount.objects.filter(pk=account.pk).update(leverage=100)
        account.refresh_from_db()
        return account

    def test_full_golden_scenario_open_mc100_stopout70_boundary_and_full_close(self):
        account = self._make_golden_account()
        consumer = _golden55_consumer(account)

        # ── 3. Opening the 5 positions through the REAL atomic path ──
        results = []
        for i in range(5):
            result = _db_open_sync(
                consumer, "EUR/USD", "buy", float(self.QTY), self.ENTRY_PX,
                None, None, commission=0.0, new_balance=float(account.balance),
            )
            self.assertTrue(
                result["ok"],
                f"open #{i + 1} rejected: {result.get('error_code')}/{result.get('message')}",
            )
            self.assertLess(
                result["required_margin_pct"], 10.00,
                f"open #{i + 1} per-trade %% must stay under the account's real 10%% cap",
            )
            self.assertLessEqual(
                result["projected_total_margin_pct"], 50.00,
                f"open #{i + 1} total %% must stay at/under the account's real 50%% cap",
            )
            results.append(result)

        positions = list(Position.objects.filter(account=account).order_by("id"))
        self.assertEqual(len(positions), 5, "netting_mode=False -> 5 separate positions, no merge")
        for p in positions:
            self.assertEqual(p.qty, self.QTY)
            self.assertEqual(p.avg_price, Decimal(str(self.ENTRY_PX)))

        # ── D. margin after each open — required_margin ~997.60/position ──
        for i, result in enumerate(results, start=1):
            self.assertAlmostEqual(result["required_margin"], 997.60, delta=0.01)
            self.assertAlmostEqual(result["projected_total_margin"], 997.60 * i, delta=0.05)

        final_margin_used = results[-1]["projected_total_margin"]
        self.assertAlmostEqual(final_margin_used, 4988.00, delta=0.5)

        account.refresh_from_db()
        self.assertEqual(
            account.balance, Decimal("10000.62"),
            "demo-standard commission_per_lot=0.00 and no BrokerSpreadConfig/account "
            "markup exist in this isolated test DB -> the real fee formula correctly "
            "yields $0.00; balance must be untouched by the 5 opens",
        )
        margin_level_at_open = float(account.balance) / final_margin_used * 100.0
        self.assertGreater(margin_level_at_open, 200.0, "sanity: ~200.5%% at open, matches GOLDEN-STOPOUT-01A's calc")

        # ── 4. Margin Call 100%% — order-entry gate only, never closes ──
        equity_at_mc = final_margin_used  # equity == margin_used -> margin_level == 100.00%
        account_snap = {
            "leverage": 100, "allowed_symbols": None, "max_lot_size": None,
            "margin_call_level": 100.0,
        }
        ok, code, _msg, _details = _compute_pretrade_margin_guard(
            "EUR/USD", 0.01, self.ENTRY_PX, equity=equity_at_mc, margin_used_now=final_margin_used,
            account_snap=account_snap, spec_max_leverage=500, spec_contract_size=100000.0,
            max_margin_per_trade_pct=10.0, max_total_margin_pct=50.0,
        )
        self.assertFalse(ok, "a new order at the Margin-Call-equivalent equity must be rejected")
        # Account 55's own 50%% total-margin cap is structurally tighter
        # than margin_call_level=100 — same finding
        # MarginCallOrderGateBoundaryTests' docstring already proved in
        # general, now confirmed for this account's OWN real config: the
        # rejection fires at gate 4 (total_margin_exceeded), never gate 5
        # (margin_call_level_breach). Margin Call is not separately
        # observable as its own distinct trigger for this account — it is
        # a gate, never a liquidation event, and this asserts exactly
        # which gate fires rather than assuming one.
        self.assertEqual(code, "total_margin_exceeded")

        self.assertEqual(
            Position.objects.filter(account=account).count(), 5,
            "Margin Call is an order-entry gate only — it must never close a position",
        )
        self.assertEqual(Trade.objects.filter(account=account).count(), 0)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 0,
            "no REALIZED_PNL must be created by a rejected order / Margin-Call-equivalent state",
        )

        # ── 5. Stop-Out 70%% — exact boundary, strict '<' ──
        equity_exactly_70 = round(final_margin_used * 0.70, 2)
        triggered_at_70 = check_equity_stopout(
            equity=equity_exactly_70, peak_balance=float(account.balance), tier="10K",
            account_type="DEMO", margin_used=final_margin_used, stopout_level=70.0,
        )
        self.assertFalse(triggered_at_70, "exactly 70.00%% must NOT trigger stop-out (strict '<')")

        equity_just_below_70 = round(final_margin_used * 0.6999, 2)
        triggered_below_70 = check_equity_stopout(
            equity=equity_just_below_70, peak_balance=float(account.balance), tier="10K",
            account_type="DEMO", margin_used=final_margin_used, stopout_level=70.0,
        )
        self.assertTrue(triggered_below_70, "69.99%% must trigger stop-out")

        # ── 6/7/8. Full liquidation — deterministic, forced equity, real close path ──
        consumer.account["margin_used"] = final_margin_used
        consumer.account["equity"] = equity_just_below_70
        # Same order positions were opened in -> exercises whatever
        # liquidation order the engine actually has today (none/FIFO by
        # list order); this test asserts the EXISTING behavior, it does
        # not introduce a new "close biggest loss first" policy.
        consumer._positions = [_pos_entry(p) for p in positions]

        _run(consumer._do_retail_liquidation())

        self.assertEqual(Position.objects.filter(account=account).count(), 0, "FULL stop-out: 0 positions remain")
        # Trade carries no Position FK (a fill record, not a position
        # link) — verified by count/values instead: one Trade per closed
        # Position, in the same order they were closed (ascending id,
        # matching creation order — no reordering policy exists or is
        # introduced here).
        trades = list(Trade.objects.filter(account=account).order_by("id"))
        self.assertEqual(len(trades), 5, "exactly one Trade per closed Position")
        for t in trades:
            self.assertEqual(t.symbol, "EUR/USD")
            self.assertEqual(t.trade_type, Trade.BUY)
            self.assertEqual(t.lot_size, self.QTY)
            self.assertEqual(t.entry_price, Decimal(str(self.ENTRY_PX)))
            self.assertAlmostEqual(float(t.profit_loss), 0.0, places=2)

        realized_entries = list(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED))
        self.assertEqual(len(realized_entries), 5, "one REALIZED_PNL LedgerEntry per close, no duplication")
        total_realized = sum(float(e.amount) for e in realized_entries)
        # Deterministic, backend-controlled scenario: close price == entry
        # price (flat _FlatFeed116, per the module note above) -> every
        # position closes at exactly zero realized P&L.
        self.assertAlmostEqual(total_realized, 0.0, places=2)

        account.refresh_from_db()
        self.assertEqual(
            account.balance, Decimal("10000.62"),
            "balance = initial (10000.62) + $0.00 commission + $0.00 spread fee + "
            "sum(realized_pnl=0.00 x5) -- the real, unmocked engine formula, not hardcoded",
        )
        self.assertEqual(consumer.account["margin_used"], 0.0)
        self.assertEqual(consumer.account["equity"], float(account.balance))
        self.assertEqual(consumer.account["status"], "Activo", "DEMO/RETAIL-family never suspends on stop-out")
        self.assertEqual(account.status, "Activo")

        stopout_events = [
            c.args[0] for c in consumer.send_json.call_args_list
            if c.args[0].get("type") == "account:stopout"
        ]
        self.assertEqual(len(stopout_events), 1)
        ev = stopout_events[0]
        self.assertFalse(ev["partial"])
        self.assertEqual(ev["closed_count"], 5)
        self.assertEqual(ev["remaining_count"], 0)

        audit_events = BrokerAuditEvent.objects.filter(
            event_type=EV_POSITION_CLOSED_STOPOUT, account=account,
        )
        self.assertEqual(audit_events.count(), 5)

        # ── 9. No Redis / no global side effects ──
        import inspect
        src_liq = inspect.getsource(TradingConsumer._do_retail_liquidation)
        self.assertNotIn("trx:price", src_liq)
        self.assertNotIn("import redis", src_liq)
        src_open = inspect.getsource(TradingConsumer._db_open_position_atomic.__wrapped__)
        self.assertNotIn("trx:price", src_open)
        # This test never imports celery/Daphne — the async paths under
        # test run via asyncio.run() (see _run, imported above) directly
        # against a real DB, exactly like every other test in this file.

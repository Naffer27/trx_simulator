# simulator/tests/test_fix04_maintenance_margin.py
"""
FIX-04 — Maintenance Margin Hardcoded + Staff UI Alignment.

compute_margin_state() no longer hardcodes maintenance_margin = margin_after
* 0.5 — it now derives it from the account's real stopout_level (the equity
required exactly at the Stop-Out threshold), and stopout_level is a
keyword-only, mandatory parameter (no silent default) so no future caller
can recreate the old bug by omission.

Pure numeric contract tests (boundary equality, zero margin, negative
equity, mandatory-kwarg TypeError) live in test_risk_engine.py::
TestComputeMarginState — this file covers integration: the 4 real callers
(consumers.py per-tick payload, risk_engine.py::evaluate_position_risk,
admin.py::margin_panel, admin.py::dealing_desk_view), RETAIL vs DD
treatment, legacy-vs-new snapshot policy authority, and the staff UI
contract (explicit Margin Call/Stop-Out %, boundary colors, dealing desk
markers no longer hardcoded at 50).
"""
from decimal import Decimal

from django.contrib.admin.sites import site
from django.test import TestCase, TransactionTestCase

from simulator.admin import TradingAccountAdmin
from simulator.models import TradingAccount
from simulator.risk_engine import compute_margin_state, evaluate_position_risk

from market_data.feeds import get_feed_manager

from .factories import make_account, make_position
from .test_account_balance_concurrency import _bare_consumer_for_recalc, _run


# ── 1. RETAIL vs DD — caller A (consumers.py per-tick account:update) ───────

class RetailVsDdPayloadTests(TransactionTestCase):
    def test_retail_account_gets_real_maintenance_margin_in_payload(self):
        account = make_account(
            balance=Decimal("10000.00"), account_type="RETAIL",
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        panel = _bare_consumer_for_recalc(account.pk, balance=10000.0)
        panel._feed = get_feed_manager()
        panel.account["account_type"] = "RETAIL"
        panel.account["stopout_level"] = 70.0
        # leverage=50 (bare-consumer default), EUR/USD contract_size=100000:
        # notional = 1.0*0.5*100000 = 50000 -> margin_used = 50000/50 = 1000.
        panel._positions = [{"symbol": "EUR/USD", "side": "buy", "qty": 0.5, "avg": 1.0,
                              "sl": None, "tp": None, "opened_at": 0.0, "id": 1}]
        _run(panel._recalc_account_and_push())

        sent = [c.args[0] for c in panel.send_json.call_args_list
                if c.args[0].get("type") == "account:update"]
        self.assertTrue(sent)
        payload = sent[-1]
        self.assertIn("maintenance_margin", payload)
        self.assertIn("liquidation_distance", payload)
        # equity=10000 (no floating pnl), margin_used=1000, stopout=70 -> maintenance=700
        self.assertAlmostEqual(payload["maintenance_margin"], 700.0, places=2)

    def test_dd_account_gets_zero_maintenance_margin_no_call(self):
        """CHALLENGE/FUNDED never adopt the RETAIL maintenance-margin metric
        — compute_margin_state must not even run for them (FIX-04 gating)."""
        account = make_account(balance=Decimal("10000.00"), account_type="CHALLENGE")
        panel = _bare_consumer_for_recalc(account.pk, balance=10000.0)
        panel._feed = get_feed_manager()
        panel.account["account_type"] = "CHALLENGE"
        # Real open exposure (margin_used would be 1000 if this were RETAIL) —
        # proves the DD branch is truly gated off, not just coincidentally 0.
        panel._positions = [{"symbol": "EUR/USD", "side": "buy", "qty": 0.5, "avg": 1.0,
                              "sl": None, "tp": None, "opened_at": 0.0, "id": 1}]
        _run(panel._recalc_account_and_push())

        sent = [c.args[0] for c in panel.send_json.call_args_list
                if c.args[0].get("type") == "account:update"]
        payload = sent[-1]
        self.assertEqual(payload["maintenance_margin"], 0.0)
        self.assertEqual(payload["liquidation_distance"], 0.0)
        self.assertEqual(payload["used_margin_pct"], 0.0)


# ── 2. Payload policy authority — legacy snapshot 50 vs new snapshot 70 ─────

class PayloadSnapshotAuthorityTests(TransactionTestCase):
    def test_two_accounts_same_margin_different_snapshot_different_maintenance(self):
        legacy = make_account(
            balance=Decimal("10000.00"), account_type="RETAIL",
            stopout_level_snapshot=Decimal("50.00"),
        )
        modern = make_account(
            balance=Decimal("10000.00"), account_type="RETAIL",
            stopout_level_snapshot=Decimal("70.00"),
        )

        _pos = [{"symbol": "EUR/USD", "side": "buy", "qty": 0.5, "avg": 1.0,
                 "sl": None, "tp": None, "opened_at": 0.0, "id": 1}]

        panel_legacy = _bare_consumer_for_recalc(legacy.pk, balance=10000.0)
        panel_legacy._feed = get_feed_manager()
        panel_legacy.account["account_type"] = "RETAIL"
        panel_legacy.account["stopout_level"] = 50.0
        panel_legacy._positions = list(_pos)
        _run(panel_legacy._recalc_account_and_push())

        panel_modern = _bare_consumer_for_recalc(modern.pk, balance=10000.0)
        panel_modern._feed = get_feed_manager()
        panel_modern.account["account_type"] = "RETAIL"
        panel_modern.account["stopout_level"] = 70.0
        panel_modern._positions = list(_pos)
        _run(panel_modern._recalc_account_and_push())

        legacy_payload = [c.args[0] for c in panel_legacy.send_json.call_args_list
                           if c.args[0].get("type") == "account:update"][-1]
        modern_payload = [c.args[0] for c in panel_modern.send_json.call_args_list
                           if c.args[0].get("type") == "account:update"][-1]

        self.assertAlmostEqual(legacy_payload["maintenance_margin"], 500.0, places=2)
        self.assertAlmostEqual(modern_payload["maintenance_margin"], 700.0, places=2)
        self.assertNotEqual(legacy_payload["maintenance_margin"], modern_payload["maintenance_margin"])


# ── 3. risk_preview — caller D (risk_engine.py::evaluate_position_risk) ─────

class RiskPreviewPolicyAuthorityTests(TestCase):
    def test_retail_risk_preview_uses_account_snapshot_not_hardcoded_50(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("10000"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        result = evaluate_position_risk(
            account, "EUR/USD", lot_size=0.0, current_equity=700.0,
            current_margin_used=1000.0, leverage=50,
        )
        self.assertAlmostEqual(result["maintenance_margin"], 700.0, places=2)

    def test_legacy_null_snapshot_falls_back_to_50(self):
        account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        TradingAccount.objects.filter(pk=account.pk).update(stopout_level_snapshot=None)
        account.refresh_from_db()
        result = evaluate_position_risk(
            account, "EUR/USD", lot_size=0.0, current_equity=500.0,
            current_margin_used=1000.0, leverage=50,
        )
        self.assertAlmostEqual(result["maintenance_margin"], 500.0, places=2)

    def test_dd_account_risk_preview_unaffected_no_maintenance_margin_key(self):
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        result = evaluate_position_risk(
            account, "EUR/USD", lot_size=0.01, current_equity=10000.0,
            current_margin_used=0.0, leverage=50,
        )
        self.assertNotIn("maintenance_margin", result)
        self.assertNotIn("engine", result)


# ── 4. Admin margin_panel — explicit MCL/SOL, exact boundary colors ─────────

class AdminMarginPanelTests(TestCase):
    def _panel_html(self, account):
        ma = TradingAccountAdmin(TradingAccount, site)
        return str(ma.margin_panel(account))

    def test_explicit_margin_call_and_stopout_labels(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("1000"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        html = self._panel_html(account)
        self.assertIn("Margin Call", html)
        self.assertIn("Stop-Out", html)
        self.assertIn("100%", html)
        self.assertIn("70%", html)

    def test_no_literal_100_150_policy_thresholds_in_color_logic(self):
        """Structural — the color function must reference the real snapshot
        variables, not the old 100/150 literals."""
        import inspect
        from simulator import admin as admin_module
        src = inspect.getsource(admin_module.TradingAccountAdmin.margin_panel)
        self.assertNotIn("mlevel < 100", src)
        self.assertNotIn("mlevel < 150", src)
        self.assertIn("mlevel < sol", src)
        self.assertIn("mlevel <= mcl", src)

    def test_boundary_exactly_at_stopout_is_warning_not_critical(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("70"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        TradingAccount.objects.filter(pk=account.pk).update(equity=Decimal("70.00"), leverage=1)
        account.refresh_from_db()
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("100.00"))
        html = self._panel_html(account)
        # margin_level = equity/margin_after*100 = 70/100*100 = 70.00 == SOL exactly
        self.assertIn('color:#e67e22;">70%</span></div>', html.replace(" ", "").replace("\n", ""),
                      "exactly at SOL must be WARNING (#e67e22), not CRITICAL")

    def test_boundary_exactly_at_margin_call_is_warning_not_normal(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("100"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        TradingAccount.objects.filter(pk=account.pk).update(equity=Decimal("100.00"), leverage=1)
        account.refresh_from_db()
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("100.00"))
        html = self._panel_html(account)
        # margin_level = 100/100*100 = 100.00 == MCL exactly
        self.assertIn('color:#e67e22;">100%</span></div>', html.replace(" ", "").replace("\n", ""),
                      "exactly at MCL must still be WARNING (#e67e22), not NORMAL")

    def test_boundary_below_stopout_is_critical(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("69"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        TradingAccount.objects.filter(pk=account.pk).update(equity=Decimal("69.99"), leverage=1)
        account.refresh_from_db()
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("100.00"))
        html = self._panel_html(account)
        self.assertIn('color:#e74c3c;">70%</span></div>', html.replace(" ", "").replace("\n", ""),
                      "just below SOL must be CRITICAL (#e74c3c)")

    def test_boundary_above_margin_call_is_normal(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("150"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        TradingAccount.objects.filter(pk=account.pk).update(equity=Decimal("150.00"), leverage=1)
        account.refresh_from_db()
        make_position(account, symbol="EUR/USD", side="BUY",
                      qty=Decimal("1.0"), avg_price=Decimal("100.00"))
        html = self._panel_html(account)
        self.assertIn('color:#27ae60;">150%</span></div>', html.replace(" ", "").replace("\n", ""),
                      "above MCL must be NORMAL (#27ae60)")


# ── 5. Dealing desk — no hardcoded 50, real snapshot drives markers ─────────

class DealingDeskContextTests(TestCase):
    """Real GET through the admin view (Client + force_login + reverse —
    same pattern as test_book05d3c_liquidity_ledger_admin_force_close.py),
    not a bare context-dict simulation — exercises caller C for real."""

    def setUp(self):
        from django.test import Client
        from .factories import make_user
        self.superuser = make_user(username="fix04_dd_admin", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)

    def test_retail_account_dealing_desk_shows_real_snapshot(self):
        account = make_account(
            account_type="RETAIL", balance=Decimal("10000"),
            margin_call_level_snapshot=Decimal("100.00"),
            stopout_level_snapshot=Decimal("70.00"),
        )
        from django.urls import reverse
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        retail_margin = resp.context["retail_margin"]
        self.assertEqual(retail_margin["margin_call_level"], 100.0)
        self.assertEqual(retail_margin["stopout_level"], 70.0)
        self.assertAlmostEqual(retail_margin["maintenance_margin"], 0.0, places=2)  # no open positions

    def test_dd_account_dealing_desk_has_no_retail_margin(self):
        account = make_account(account_type="CHALLENGE", tier="10K", balance=Decimal("10000"))
        from django.urls import reverse
        url = reverse("admin:simulator_tradingaccount_dealing_desk", args=[account.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context["retail_margin"])


class DealingDeskTemplateContractTests(TestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("admin/dealing_desk_inline.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_no_hardcoded_50_stopout_zone(self):
        src = self._template_source()
        self.assertNotIn("widthratio 50 300 100", src)

    def test_no_hardcoded_100_warn_label(self):
        src = self._template_source()
        self.assertNotIn('>100% WARN<', src)

    def test_markers_derived_from_real_snapshot(self):
        src = self._template_source()
        self.assertIn("retail_margin.stopout_level", src)
        self.assertIn("retail_margin.margin_call_level", src)

    def test_150_kept_only_as_labeled_buffer_reference(self):
        src = self._template_source()
        self.assertIn("buffer reference", src)


# ── 6. Mandatory kwarg reaches every caller — no silent 50.0 default leak ───

class MandatoryStopoutLevelIntegrationTests(TestCase):
    def test_direct_call_without_stopout_level_raises(self):
        with self.assertRaises(TypeError):
            compute_margin_state(1000.0, 500.0)

    def test_50_and_70_both_work_explicitly(self):
        s50 = compute_margin_state(1000.0, 500.0, stopout_level=50.0)
        s70 = compute_margin_state(1000.0, 500.0, stopout_level=70.0)
        self.assertNotEqual(s50["maintenance_margin"], s70["maintenance_margin"])


# ── 7. Regression — check_equity_stopout / Margin Call gate untouched ───────

class NoEnforcementRegressionTests(TestCase):
    def test_check_equity_stopout_signature_unchanged(self):
        import inspect
        from simulator.risk_engine import check_equity_stopout
        params = list(inspect.signature(check_equity_stopout).parameters)
        self.assertIn("stopout_level", params)
        # default preserved — FIX-04 explicitly does not touch this function
        self.assertEqual(
            inspect.signature(check_equity_stopout).parameters["stopout_level"].default,
            50.0,
        )

    def test_pretrade_margin_guard_untouched(self):
        import inspect
        from simulator.consumers import _compute_pretrade_margin_guard
        src = inspect.getsource(_compute_pretrade_margin_guard)
        self.assertIn("margin_call_level_breach", src)

# simulator/tests/test_dashboard_panel_mode.py
"""
Phase 6B dashboard panel-mode tests.

Verifies that trading_dashboard sends the correct panel-mode context keys
so the template can show Challenge Progress vs Account Rules conditionally.

Covers:
  - show_challenge_panel True/False for each account_type
  - show_account_rules_panel True/False for each account_type
  - acct_rules dict present and populated for margin accounts
  - acct_rules uses snapshot values when available
  - acct_rules falls back to live account fields when no snapshot
  - Challenge / Funded accounts: no acct_rules product_name leaking challenge data
  - Context keys: show_challenge_panel, show_account_rules_panel, acct_rules
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from simulator.models import TradingAccount
from simulator.tests.factories import make_account, make_user


MARGIN_TYPES = ("DEMO", "RETAIL", "STANDARD", "ECN", "CRYPTO")
DD_TYPES     = ("CHALLENGE", "FUNDED")


def _url(account_id):
    return reverse("simulator:dashboard_account", args=[account_id])


class PanelModeFlagTests(TestCase):
    """show_challenge_panel and show_account_rules_panel are mutually exclusive."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _ctx(self, account):
        r = self.client.get(_url(account.pk))
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_challenge_shows_challenge_panel(self):
        acc = make_account(self.user, account_type="CHALLENGE", tier="10K")
        ctx = self._ctx(acc)
        self.assertTrue(ctx["show_challenge_panel"])
        self.assertFalse(ctx["show_account_rules_panel"])

    def test_funded_shows_challenge_panel(self):
        acc = make_account(self.user, account_type="FUNDED", tier="10K")
        ctx = self._ctx(acc)
        self.assertTrue(ctx["show_challenge_panel"])
        self.assertFalse(ctx["show_account_rules_panel"])

    def test_demo_shows_account_rules_panel(self):
        acc = make_account(self.user, account_type="DEMO", tier=None)
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_challenge_panel"])
        self.assertTrue(ctx["show_account_rules_panel"])

    def test_standard_shows_account_rules_panel(self):
        acc = make_account(self.user, account_type="STANDARD", tier=None)
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_challenge_panel"])
        self.assertTrue(ctx["show_account_rules_panel"])

    def test_ecn_shows_account_rules_panel(self):
        acc = make_account(self.user, account_type="ECN", tier=None)
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_challenge_panel"])
        self.assertTrue(ctx["show_account_rules_panel"])

    def test_retail_shows_account_rules_panel(self):
        acc = make_account(self.user, account_type="RETAIL", tier=None)
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_challenge_panel"])
        self.assertTrue(ctx["show_account_rules_panel"])

    def test_crypto_shows_account_rules_panel(self):
        acc = make_account(self.user, account_type="CRYPTO", tier=None)
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_challenge_panel"])
        self.assertTrue(ctx["show_account_rules_panel"])


class AcctRulesContextTests(TestCase):
    """acct_rules dict is present and has the right structure for margin accounts."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _ctx(self, account):
        r = self.client.get(_url(account.pk))
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_acct_rules_key_present_for_standard(self):
        acc = make_account(self.user, account_type="STANDARD")
        ctx = self._ctx(acc)
        self.assertIn("acct_rules", ctx)

    def test_acct_rules_account_type_correct(self):
        acc = make_account(self.user, account_type="ECN")
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["account_type"], "ECN")

    def test_acct_rules_leverage_fallback_to_account(self):
        acc = make_account(self.user, account_type="STANDARD")
        # make_account sets leverage=50; verify fallback when no snapshot
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["leverage"], 50)

    def test_acct_rules_leverage_snapshot_preferred(self):
        acc = make_account(self.user, account_type="STANDARD")
        TradingAccount.objects.filter(pk=acc.pk).update(leverage_snapshot=300)
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["leverage"], 300)

    def test_acct_rules_product_name_fallback_to_account_type(self):
        acc = make_account(self.user, account_type="DEMO")
        ctx = self._ctx(acc)
        # No snapshot set → falls back to account_type string
        self.assertEqual(ctx["acct_rules"]["product_name"], "DEMO")

    def test_acct_rules_product_name_from_snapshot(self):
        acc = make_account(self.user, account_type="STANDARD")
        TradingAccount.objects.filter(pk=acc.pk).update(product_name_snapshot="Real Standard")
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["product_name"], "Real Standard")

    def test_acct_rules_commission_snapshot(self):
        acc = make_account(self.user, account_type="ECN")
        TradingAccount.objects.filter(pk=acc.pk).update(commission_per_lot_snapshot=Decimal("7.00"))
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["commission_per_lot"], Decimal("7.00"))

    def test_acct_rules_spread_pips_snapshot(self):
        acc = make_account(self.user, account_type="STANDARD")
        TradingAccount.objects.filter(pk=acc.pk).update(spread_pips_snapshot=Decimal("1.20"))
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["spread_pips"], Decimal("1.20"))

    def test_acct_rules_max_lot_size_none_when_not_set(self):
        acc = make_account(self.user, account_type="STANDARD")
        ctx = self._ctx(acc)
        self.assertIsNone(ctx["acct_rules"]["max_lot_size"])

    def test_acct_rules_max_lot_size_from_snapshot(self):
        acc = make_account(self.user, account_type="STANDARD")
        TradingAccount.objects.filter(pk=acc.pk).update(max_lot_size_snapshot=Decimal("3.00"))
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["max_lot_size"], Decimal("3.00"))

    def test_acct_rules_currency_defaults_to_usd(self):
        acc = make_account(self.user, account_type="DEMO")
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["currency"], "USD")

    def test_acct_rules_margin_call_stopout_snapshot(self):
        acc = make_account(self.user, account_type="STANDARD")
        TradingAccount.objects.filter(pk=acc.pk).update(
            margin_call_level_snapshot=Decimal("80.00"),
            stopout_level_snapshot=Decimal("40.00"),
        )
        ctx = self._ctx(acc)
        self.assertEqual(ctx["acct_rules"]["margin_call_level"], Decimal("80.00"))
        self.assertEqual(ctx["acct_rules"]["stopout_level"], Decimal("40.00"))


class ChallengePanelIsolationTests(TestCase):
    """Challenge accounts must not expose acct_rules or account_rules_panel."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _ctx(self, account):
        r = self.client.get(_url(account.pk))
        self.assertEqual(r.status_code, 200)
        return r.context

    def test_challenge_no_account_rules_panel(self):
        acc = make_account(self.user, account_type="CHALLENGE", tier="10K")
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_account_rules_panel"])

    def test_funded_no_account_rules_panel(self):
        acc = make_account(self.user, account_type="FUNDED", tier="10K")
        ctx = self._ctx(acc)
        self.assertFalse(ctx["show_account_rules_panel"])

    def test_challenge_still_has_challenge_context_keys(self):
        acc = make_account(self.user, account_type="CHALLENGE", tier="10K")
        ctx = self._ctx(acc)
        self.assertIn("realized_dd_pct", ctx)
        self.assertIn("daily_realized_dd_pct", ctx)
        self.assertIn("has_profit_target", ctx)


class SidebarPanelTitleTests(TestCase):
    """K.5 — sidebar renders correct title/subtitle for each account type."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _html(self, account):
        r = self.client.get(_url(account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_demo_shows_demo_account_title(self):
        acc = make_account(self.user, account_type="DEMO", tier=None)
        html = self._html(acc)
        self.assertIn("Demo Account", html)

    def test_demo_shows_practice_environment_subtitle(self):
        acc = make_account(self.user, account_type="DEMO", tier=None)
        html = self._html(acc)
        self.assertIn("Practice environment", html)

    def test_demo_does_not_show_real_account_title(self):
        acc = make_account(self.user, account_type="DEMO", tier=None)
        html = self._html(acc)
        self.assertNotIn("Real Account", html)

    def test_retail_shows_real_account_title(self):
        acc = make_account(self.user, account_type="RETAIL", tier=None)
        html = self._html(acc)
        self.assertIn("Real Account", html)

    def test_retail_shows_live_trading_subtitle(self):
        acc = make_account(self.user, account_type="RETAIL", tier=None)
        html = self._html(acc)
        self.assertIn("Live trading account", html)

    def test_ecn_shows_real_account_title(self):
        acc = make_account(self.user, account_type="ECN", tier=None)
        html = self._html(acc)
        self.assertIn("Real Account", html)

    def test_challenge_shows_neither_demo_nor_real_panel_title(self):
        acc = make_account(self.user, account_type="CHALLENGE", tier="10K")
        html = self._html(acc)
        self.assertNotIn("Demo Account", html)
        self.assertNotIn("Real Account", html)

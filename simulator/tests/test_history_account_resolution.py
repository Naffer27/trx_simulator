"""
simulator/tests/test_history_account_resolution.py — ACCOUNT-03a.

Root cause fixed: history_view() used a divergent, inline account-resolution
lookup (`TradingAccount.objects.filter(pk=session["account_id"], user=...)`)
with no status='Activo' filter and no fallback — while /home/ and
/dashboard/ both resolve the "current" account via the shared
_resolve_account() helper (session -> requires status='Activo' -> falls
back to the user's most-recent active account, self-healing the session).
A session pointing at a suspended/violated account would make dashboard/
home silently switch to a different (active) account while History kept
showing the stale one — the two surfaces could disagree about "the"
current account for the same user in the same session.

Fixed by making history_view() call the same _resolve_account(request)
used by home_view()/trading_dashboard(). No model/migration changes; no
change to trading, PnL, balance, margin, spread, or ledger computation —
purely which TradingAccount row gets used to filter Trade/LedgerEntry for
display.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_ledger_entry, make_trade, make_user


def _set_session_account(client, account_id):
    session = client.session
    session["account_id"] = account_id
    session.save()


class HistoryHomeDashboardAgreeTests(TestCase):
    """history/home/dashboard must resolve to the same account_id."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, status="Activo")
        self.client.force_login(self.user)
        _set_session_account(self.client, self.account.pk)

    def test_all_three_surfaces_resolve_same_account(self):
        r_home = self.client.get(reverse("simulator:home"))
        r_dash = self.client.get(reverse("simulator:dashboard"))
        r_hist = self.client.get(reverse("simulator:history"))

        self.assertEqual(r_home.status_code, 200)
        self.assertEqual(r_dash.status_code, 200)
        self.assertEqual(r_hist.status_code, 200)

        self.assertEqual(r_home.context["account"].pk, self.account.pk)
        self.assertEqual(r_dash.context["account"].pk, self.account.pk)
        self.assertEqual(r_hist.context["account"].pk, self.account.pk)


class SessionActiveAccountValidTests(TestCase):
    def test_session_points_at_valid_active_account(self):
        user = make_user()
        account = make_account(user, status="Activo")
        self.client.force_login(user)
        _set_session_account(self.client, account.pk)

        r = self.client.get(reverse("simulator:history"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["account"].pk, account.pk)


class SuspendedFallbackConsistencyTests(TestCase):
    """Session points at a suspended account while another active one exists
    for the same user — history must fall back exactly like home/dashboard,
    not keep showing the suspended account's history."""

    def setUp(self):
        self.user = make_user()
        self.suspended = make_account(self.user, status="Suspendido")
        self.active = make_account(self.user, status="Activo")
        self.client.force_login(self.user)
        _set_session_account(self.client, self.suspended.pk)

    def test_history_falls_back_to_active_account_like_home_and_dashboard(self):
        r_home = self.client.get(reverse("simulator:home"))
        r_hist = self.client.get(reverse("simulator:history"))

        self.assertEqual(r_home.context["account"].pk, self.active.pk)
        self.assertEqual(r_hist.context["account"].pk, self.active.pk)
        self.assertNotEqual(r_hist.context["account"].pk, self.suspended.pk)

    def test_fallback_self_heals_session_for_subsequent_requests(self):
        self.client.get(reverse("simulator:history"))
        session = self.client.session
        self.assertEqual(session.get("account_id"), self.active.pk)


class ForeignAccountSessionTests(TestCase):
    """Session account_id belongs to a different user — must never be used,
    and must not leak that other user's account/trades."""

    def setUp(self):
        self.owner = make_user()
        self.other_account = make_account(self.owner, status="Activo")

        self.user = make_user()
        self.own_active = make_account(self.user, status="Activo")
        self.client.force_login(self.user)
        _set_session_account(self.client, self.other_account.pk)

    def test_history_ignores_foreign_account_and_falls_back_to_own(self):
        r = self.client.get(reverse("simulator:history"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["account"].pk, self.own_active.pk)
        self.assertNotEqual(r.context["account"].pk, self.other_account.pk)

    def test_no_foreign_trades_leak_into_history(self):
        make_trade(self.other_account, profit_loss="50.00")
        r = self.client.get(reverse("simulator:history"))
        for t in r.context["trades"]:
            self.assertEqual(t.account_id, self.own_active.pk)


class NoActiveAccountRedirectTests(TestCase):
    def test_user_with_no_active_accounts_redirects_to_accounts_page(self):
        user = make_user()
        make_account(user, status="Suspendido")
        self.client.force_login(user)

        r_home = self.client.get(reverse("simulator:home"))
        r_hist = self.client.get(reverse("simulator:history"))

        self.assertRedirects(r_home, reverse("simulator:accounts"))
        self.assertRedirects(r_hist, reverse("simulator:accounts"))

    def test_user_with_zero_accounts_redirects_to_accounts_page(self):
        user = make_user()
        self.client.force_login(user)

        r = self.client.get(reverse("simulator:history"))
        self.assertRedirects(r, reverse("simulator:accounts"))


class HistoryShowsOnlyResolvedAccountTests(TestCase):
    def test_history_excludes_trades_from_other_accounts_of_same_user(self):
        user = make_user()
        account_a = make_account(user, status="Activo")
        account_b = make_account(user, status="Activo")
        self.client.force_login(user)
        _set_session_account(self.client, account_a.pk)

        make_trade(account_a, symbol="EUR/USD", profit_loss="12.00")
        make_trade(account_b, symbol="GBP/USD", profit_loss="-99.00")
        make_ledger_entry(account_a, amount="12.00", balance_after="10012.00")
        make_ledger_entry(account_b, amount="-99.00", balance_after="9901.00")

        r = self.client.get(reverse("simulator:history"))
        self.assertEqual(r.context["account"].pk, account_a.pk)
        symbols = {t.symbol for t in r.context["trades"]}
        self.assertEqual(symbols, {"EUR/USD"})
        for entry in r.context["ledger"]:
            self.assertEqual(entry.account_id, account_a.pk)


class HistoryExistingBehaviorRegressionTests(TestCase):
    """No regression in the pre-existing stats/aggregation behavior."""

    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, status="Activo")
        self.client.force_login(self.user)
        _set_session_account(self.client, self.account.pk)

    def test_win_loss_totals_and_aggregates_unchanged(self):
        make_trade(self.account, profit_loss="10.00")
        make_trade(self.account, profit_loss="-4.00")
        make_trade(self.account, profit_loss="6.00")

        r = self.client.get(reverse("simulator:history"))
        self.assertEqual(r.context["total"], 3)
        self.assertEqual(r.context["wins"], 2)
        self.assertEqual(r.context["losses"], 1)
        self.assertEqual(r.context["total_pnl"], Decimal("12.00"))
        self.assertEqual(r.context["best_trade"], Decimal("10.00"))
        self.assertEqual(r.context["worst_trade"], Decimal("-4.00"))

    def test_empty_history_renders_zeroed_stats(self):
        r = self.client.get(reverse("simulator:history"))
        self.assertEqual(r.context["total"], 0)
        self.assertEqual(r.context["wins"], 0)
        self.assertEqual(r.context["losses"], 0)
        self.assertEqual(r.context["win_rate"], 0)

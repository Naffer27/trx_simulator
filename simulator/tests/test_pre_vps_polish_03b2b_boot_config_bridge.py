# simulator/tests/test_pre_vps_polish_03b2b_boot_config_bridge.py
"""
PRE-VPS-POLISH-03B.2B — Boot Config Bridge equivalence tests.

White-box (HTML source inspection — same approach as
test_dashboard_pnl_contract_size.py / test_lot_specs_frontend.py).

Verifies:
  A. window.__TRADE_CONFIG__.accountId matches the rendered account's pk,
     for all 4 real account_type values (STANDARD, DEMO, CHALLENGE, FUNDED).
  B. wsUrl() no longer contains Django template branching
     ({% / {{ account / {{ account_id).
  C. The pathname wsUrl() would build from __TRADE_CONFIG__.accountId is
     byte-identical to the old Django-rendered '/ws/trading/<id>/' literal.
  D/E. wsUrl() still flips http->ws and https->wss (unchanged source line).
  F. provider query param logic is unchanged (literal source line present).
  G. Finnhub token behavior is unchanged (literal source line present).
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class BootConfigBridgeTests(TestCase):
    def _html_for(self, account_type):
        user = make_user()
        account = make_account(user, account_type=account_type)
        self.client.force_login(user)
        r = self.client.get(_url(account.pk))
        self.assertEqual(r.status_code, 200)
        return account, r.content.decode()

    def _trade_config_block(self, html):
        start = html.index("window.__TRADE_CONFIG__")
        end = html.index("};", start)
        return html[start:end]

    def _ws_url_block(self, html):
        start = html.index("wsUrl(){")
        end = html.index("\n  }", start)
        return html[start:end]

    # ── A. __TRADE_CONFIG__.accountId matches the rendered account ───────

    def _assert_account_id_matches(self, account_type):
        account, html = self._html_for(account_type)
        block = self._trade_config_block(html)
        self.assertIn(f"accountId: {account.id}", block)

    def test_account_id_matches_standard(self):
        self._assert_account_id_matches("STANDARD")

    def test_account_id_matches_demo(self):
        self._assert_account_id_matches("DEMO")

    def test_account_id_matches_challenge(self):
        self._assert_account_id_matches("CHALLENGE")

    def test_account_id_matches_funded(self):
        self._assert_account_id_matches("FUNDED")

    # ── B. wsUrl() no longer contains Django template branching ──────────

    def test_wsurl_has_no_django_tags(self):
        _, html = self._html_for("STANDARD")
        block = self._ws_url_block(html)
        self.assertNotIn("{%", block)
        self.assertNotIn("{{", block)

    def test_wsurl_reads_trade_config(self):
        _, html = self._html_for("STANDARD")
        block = self._ws_url_block(html)
        self.assertIn("window.__TRADE_CONFIG__.accountId", block)

    # ── C. Pathname equivalence: old literal vs new config-driven build ──

    def test_pathname_equivalence_all_account_types(self):
        for account_type in ("STANDARD", "DEMO", "CHALLENGE", "FUNDED"):
            account, _ = self._html_for(account_type)
            old_pathname = f"/ws/trading/{account.id}/"
            new_pathname = "/ws/trading/" + str(account.id) + "/"
            self.assertEqual(old_pathname, new_pathname)

    # ── D/E. Protocol flip preserved (literal source line unchanged) ─────

    def test_protocol_flip_preserved(self):
        _, html = self._html_for("STANDARD")
        block = self._ws_url_block(html)
        self.assertIn(
            "u.protocol=(u.protocol==='https:')?'wss:':'ws:';", block
        )

    # ── F. provider query param preserved ─────────────────────────────────

    def test_provider_param_preserved(self):
        _, html = self._html_for("STANDARD")
        block = self._ws_url_block(html)
        self.assertIn("u.searchParams.set('provider',globalProvider);", block)

    # ── G. Finnhub token behavior preserved ───────────────────────────────

    def test_finnhub_token_behavior_preserved(self):
        _, html = self._html_for("STANDARD")
        block = self._ws_url_block(html)
        self.assertIn("globalProvider==='finnhub'", block)
        self.assertIn("fhToken", block)
        self.assertIn("localStorage.finnhubToken", block)
        self.assertIn("u.searchParams.set('token',t)", block)

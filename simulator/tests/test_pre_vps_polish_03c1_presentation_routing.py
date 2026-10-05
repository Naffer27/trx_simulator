# simulator/tests/test_pre_vps_polish_03c1_presentation_routing.py
"""
PRE-VPS-POLISH-03C.1 — Presentation routing foundation.

Verifies that trading_dashboard() selects between the existing Desktop
presentation (simulator/trade/shell.html -> simulator/trade/desktop.html,
completely unchanged) and a new, minimal Mobile placeholder
(simulator/trade/mobile.html) based on a small, conservative, dependency-
free User-Agent check — with zero change to the URL, account resolution,
authorization, or financial context computation.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
ANDROID_PHONE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
ANDROID_TABLET_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-T870) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UNKNOWN_UA = "SomeUnknownBot/1.0"


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


def _template_names(response):
    return {t.name for t in response.templates if t.name}


class PresentationRoutingTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="CHALLENGE")
        self.client.force_login(self.user)

    # A. Desktop UA -> Desktop presentation
    def test_desktop_ua_gets_desktop_presentation(self):
        r = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulator/trade/shell.html", _template_names(r))
        self.assertIn("class TradingPanel", r.content.decode())

    # B. iPhone UA -> Mobile presentation
    def test_iphone_ua_gets_mobile_presentation(self):
        r = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulator/trade/mobile.html", _template_names(r))
        self.assertIn("03C Mobile Presentation Foundation", r.content.decode())

    # C. Android phone UA -> Mobile presentation
    def test_android_phone_ua_gets_mobile_presentation(self):
        r = self.client.get(
            _url(self.account.pk), HTTP_USER_AGENT=ANDROID_PHONE_UA
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulator/trade/mobile.html", _template_names(r))

    # D. Unknown/generic UA -> Desktop fallback
    def test_unknown_ua_falls_back_to_desktop(self):
        r = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=UNKNOWN_UA)
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulator/trade/shell.html", _template_names(r))

    def test_missing_ua_falls_back_to_desktop(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulator/trade/shell.html", _template_names(r))

    def test_android_tablet_ua_without_mobile_token_falls_back_to_desktop(self):
        # Conservative heuristic: no "Mobile" token (real Android tablets
        # typically omit it) -> Desktop, not aggressive tablet detection.
        r = self.client.get(
            _url(self.account.pk), HTTP_USER_AGENT=ANDROID_TABLET_UA
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulator/trade/shell.html", _template_names(r))

    # E. Same account_id reaches both presentations
    def test_same_account_id_in_desktop_and_mobile(self):
        r_desktop = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        r_mobile = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r_desktop.context["account_id"], self.account.pk)
        self.assertEqual(r_mobile.context["account_id"], self.account.pk)
        self.assertIn(str(self.account.pk), r_mobile.content.decode())

    # F. Account resolution still belongs to request.user, regardless of UA
    def test_foreign_account_still_redirects_on_mobile_ua(self):
        other_user = make_user()
        other_account = make_account(other_user, account_type="STANDARD")
        r = self.client.get(_url(other_account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertRedirects(r, reverse("simulator:accounts"))

    def test_foreign_account_still_redirects_on_desktop_ua(self):
        other_user = make_user()
        other_account = make_account(other_user, account_type="STANDARD")
        r = self.client.get(_url(other_account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        self.assertRedirects(r, reverse("simulator:accounts"))

    # G/H. Mobile does not change the URL or add a redirect
    def test_mobile_request_is_not_redirected(self):
        r = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(r.status_code, (301, 302, 303, 307, 308))
        self.assertFalse(r.has_header("Location"))

    # I. All 4 account types work on both presentations
    def _assert_both_presentations_work(self, account_type):
        account = make_account(self.user, account_type=account_type)
        r_desktop = self.client.get(_url(account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        r_mobile = self.client.get(_url(account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertEqual(r_desktop.status_code, 200)
        self.assertEqual(r_mobile.status_code, 200)
        self.assertIn("simulator/trade/shell.html", _template_names(r_desktop))
        self.assertIn("simulator/trade/mobile.html", _template_names(r_mobile))

    def test_standard_account_both_presentations(self):
        self._assert_both_presentations_work("STANDARD")

    def test_demo_account_both_presentations(self):
        self._assert_both_presentations_work("DEMO")

    def test_challenge_account_both_presentations(self):
        self._assert_both_presentations_work("CHALLENGE")

    def test_funded_account_both_presentations(self):
        self._assert_both_presentations_work("FUNDED")

    # J. desktop.html needs no change for routing — still the exact same
    # include target, rendered the exact same way as before this block.
    def test_desktop_path_still_includes_desktop_html_unchanged(self):
        r = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        names = _template_names(r)
        self.assertIn("simulator/trade/shell.html", names)
        self.assertIn("simulator/trade/desktop.html", names)

    # K. Mobile and Desktop are distinct templates
    def test_mobile_and_desktop_are_distinct_templates(self):
        r_desktop = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        r_mobile = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=IPHONE_UA)
        self.assertNotEqual(
            _template_names(r_desktop), _template_names(r_mobile)
        )
        self.assertNotIn("simulator/trade/mobile.html", _template_names(r_desktop))
        self.assertNotIn("simulator/trade/desktop.html", _template_names(r_mobile))

    # L. Financial/account context is computed identically regardless of
    # which presentation renders it — only the template selection differs.
    def test_context_values_identical_across_presentations(self):
        r_desktop = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=DESKTOP_UA)
        r_mobile = self.client.get(_url(self.account.pk), HTTP_USER_AGENT=IPHONE_UA)
        for key in (
            "account_id",
            "acct_rules",
            "contract_size_json",
            "show_challenge_panel",
            "show_account_rules_panel",
            "closed_trades_json",
        ):
            self.assertEqual(
                r_desktop.context[key], r_mobile.context[key], msg=key
            )

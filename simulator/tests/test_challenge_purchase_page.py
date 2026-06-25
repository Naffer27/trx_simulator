# simulator/tests/test_challenge_purchase_page.py
"""
Bloque K.2G.2 — Challenge Checkout UI.

Verifies that the redesigned challenge_purchase.html renders a professional
checkout page showing all product rules dynamically from ChallengeProduct.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_challenge_product, make_user


def _url(pk):
    return reverse("simulator:challenge_purchase", args=[pk])


class CheckoutPageStructureTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(
            name="Pro Trader 25K",
            tier="25K",
            price_usd=Decimal("199.00"),
            account_size=Decimal("25000.00"),
            p1_profit_target_pct=Decimal("8.00"),
            p1_max_daily_loss_pct=Decimal("5.00"),
            p1_max_drawdown_pct=Decimal("10.00"),
            p1_min_trading_days=5,
            p1_max_duration_days=30,
            p2_profit_target_pct=Decimal("5.00"),
            p2_max_daily_loss_pct=Decimal("5.00"),
            p2_max_drawdown_pct=Decimal("10.00"),
            p2_min_trading_days=5,
            p2_max_duration_days=60,
            profit_split_pct=Decimal("80.00"),
            max_lot_size=Decimal("5.00"),
            max_open_positions=30,
            is_active=True,
        )
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.product.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    # ── Title / hero ──────────────────────────────────────────────────────
    def test_checkout_title_present(self):
        self.assertIn("Checkout Challenge", self._html())

    def test_checkout_subtitle_present(self):
        self.assertIn("Confirma tu programa de fondeo", self._html())

    # ── Product identity ──────────────────────────────────────────────────
    def test_product_name_present(self):
        self.assertIn("Pro Trader 25K", self._html())

    def test_product_tier_present(self):
        self.assertIn("25K", self._html())

    def test_product_account_size_present(self):
        self.assertIn("25000", self._html())

    def test_product_price_present(self):
        self.assertIn("199", self._html())

    # ── Phase 1 rules ─────────────────────────────────────────────────────
    def test_phase1_section_present(self):
        self.assertIn("Phase 1", self._html())

    def test_phase1_profit_target_present(self):
        self.assertIn("8", self._html())

    def test_phase1_daily_loss_present(self):
        self.assertIn("5", self._html())

    def test_phase1_drawdown_present(self):
        self.assertIn("10", self._html())

    def test_phase1_min_days_present(self):
        self.assertIn("5", self._html())

    def test_phase1_max_duration_present(self):
        self.assertIn("30", self._html())

    # ── Phase 2 rules ─────────────────────────────────────────────────────
    def test_phase2_section_present(self):
        self.assertIn("Phase 2", self._html())

    def test_phase2_profit_target_present(self):
        html = self._html()
        self.assertIn("Phase 2", html)
        self.assertIn("5", html)

    def test_phase2_max_duration_present(self):
        self.assertIn("60", self._html())

    # ── Funded terms ──────────────────────────────────────────────────────
    def test_profit_split_present(self):
        self.assertIn("80", self._html())

    def test_profit_split_label_present(self):
        self.assertIn("Profit split", self._html())

    def test_max_lot_size_present(self):
        self.assertIn("5.00", self._html())

    def test_max_open_positions_present(self):
        self.assertIn("30", self._html())

    # ── Payment section ───────────────────────────────────────────────────
    def test_currency_selector_present(self):
        html = self._html()
        self.assertIn('name="crypto_currency"', html)

    def test_pay_button_present(self):
        self.assertIn("Pagar Challenge", self._html())

    def test_activation_note_present(self):
        self.assertIn("activará automáticamente", self._html())

    # ── Form integrity ────────────────────────────────────────────────────
    def test_form_method_is_post(self):
        self.assertIn('method="post"', self._html())

    def test_csrf_token_present(self):
        self.assertIn("csrfmiddlewaretoken", self._html())

    def test_submit_button_is_in_form(self):
        html = self._html()
        form_start = html.index('<form')
        form_end = html.index('</form>', form_start)
        form_html = html[form_start:form_end]
        self.assertIn('type="submit"', form_html)

    def test_crypto_currency_field_in_form(self):
        html = self._html()
        form_start = html.index('<form')
        form_end = html.index('</form>', form_start)
        form_html = html[form_start:form_end]
        self.assertIn('name="crypto_currency"', form_html)

    # ── Crypto choices ────────────────────────────────────────────────────
    def test_btc_option_present(self):
        self.assertIn('value="btc"', self._html())

    def test_multiple_crypto_options_present(self):
        html = self._html()
        self.assertIn('value="btc"', html)
        self.assertIn('value="eth"', html)
        self.assertIn('value="sol"', html)

    # ── Steps footer ──────────────────────────────────────────────────────
    def test_steps_section_present(self):
        self.assertIn("Después del pago", self._html())

    def test_phase1_active_step_present(self):
        self.assertIn("Phase 1 activa", self._html())


class CheckoutPageDynamicDataTests(TestCase):
    """Changing product values should immediately change what the page shows."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_custom_price_shown(self):
        p = make_challenge_product(price_usd=Decimal("349.00"))
        html = self.client.get(_url(p.pk)).content.decode()
        self.assertIn("349", html)

    def test_custom_profit_target_shown(self):
        p = make_challenge_product(p1_profit_target_pct=Decimal("12.00"))
        html = self.client.get(_url(p.pk)).content.decode()
        self.assertIn("12", html)

    def test_custom_profit_split_shown(self):
        p = make_challenge_product(profit_split_pct=Decimal("90.00"))
        html = self.client.get(_url(p.pk)).content.decode()
        self.assertIn("90", html)

    def test_custom_account_size_shown(self):
        p = make_challenge_product(account_size=Decimal("50000.00"))
        html = self.client.get(_url(p.pk)).content.decode()
        self.assertIn("50000", html)


class CheckoutPageSecurityTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(is_active=True)
        self.client.force_login(self.user)

    def test_inactive_product_redirects_to_catalog(self):
        p = make_challenge_product(is_active=False)
        r = self.client.get(_url(p.pk))
        self.assertRedirects(r, reverse("simulator:challenge_catalog"))

    def test_nonexistent_product_redirects_to_catalog(self):
        r = self.client.get(_url(99999))
        self.assertRedirects(r, reverse("simulator:challenge_catalog"))

    def test_requires_login(self):
        self.client.logout()
        r = self.client.get(_url(self.product.pk))
        self.assertNotEqual(r.status_code, 200)


class CheckoutPageErrorTests(TestCase):
    """Error message renders elegantly when present in context."""

    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(is_active=True)
        self.client.force_login(self.user)

    def test_error_shown_for_invalid_currency(self):
        r = self.client.post(_url(self.product.pk), {
            "crypto_currency": "INVALID_COIN",
        })
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("no soportada", html)

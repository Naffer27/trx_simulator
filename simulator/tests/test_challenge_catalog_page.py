# simulator/tests/test_challenge_catalog_page.py
"""
Bloque K.2F — Challenge Catalog Page Visual Upgrade.

Verifies that /challenges/ renders the redesigned page with:
- Hero section with title and subtitle
- "Cómo funciona" section
- Dynamic product cards from ChallengeProduct admin data
- All key rule fields shown per product
- Purchase button per product
- Graceful empty state
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_challenge_product, make_user


def _url():
    return reverse("simulator:challenge_catalog")


class ChallengeCatalogPageHeroTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url())
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_hero_title_present(self):
        self.assertIn("Programas de Fondeo", self._html())

    def test_hero_subtitle_present(self):
        self.assertIn("Demuestra tu habilidad", self._html())

    def test_como_funciona_section_present(self):
        self.assertIn("Cómo funciona", self._html())

    def test_como_funciona_steps_present(self):
        html = self._html()
        self.assertIn("Elige tu challenge", html)
        self.assertIn("Pasa Phase 1 y Phase 2", html)
        self.assertIn("cuenta funded", html)
        self.assertIn("payout", html)


class ChallengeCatalogPageProductCardTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.product = make_challenge_product(
            name="Pro 10K",
            tier="10K",
            price_usd=Decimal("99.00"),
            account_size=Decimal("10000.00"),
            p1_profit_target_pct=Decimal("8.00"),
            p2_profit_target_pct=Decimal("5.00"),
            p1_max_daily_loss_pct=Decimal("5.00"),
            p2_max_daily_loss_pct=Decimal("5.00"),
            p1_max_drawdown_pct=Decimal("10.00"),
            p2_max_drawdown_pct=Decimal("10.00"),
            p1_min_trading_days=5,
            p2_min_trading_days=5,
            profit_split_pct=Decimal("80.00"),
            max_lot_size=Decimal("5.00"),
            max_open_positions=30,
            is_active=True,
        )
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url())
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_product_name_shown(self):
        self.assertIn("Pro 10K", self._html())

    def test_product_tier_shown(self):
        self.assertIn("10K", self._html())

    def test_product_account_size_shown(self):
        self.assertIn("10000", self._html())

    def test_product_price_shown(self):
        self.assertIn("99", self._html())

    def test_phase1_section_present(self):
        self.assertIn("Phase 1", self._html())

    def test_phase2_section_present(self):
        self.assertIn("Phase 2", self._html())

    def test_profit_split_shown(self):
        self.assertIn("80", self._html())

    def test_profit_split_label_shown(self):
        self.assertIn("profit split", self._html())

    def test_purchase_button_present(self):
        html = self._html()
        self.assertIn("Empezar Challenge", html)

    def test_purchase_url_correct(self):
        html = self._html()
        expected = f"/challenges/{self.product.pk}/buy/"
        self.assertIn(expected, html)

    def test_p1_profit_target_shown(self):
        self.assertIn("8", self._html())

    def test_p2_profit_target_shown(self):
        self.assertIn("5", self._html())

    def test_max_lot_size_shown(self):
        self.assertIn("5.00", self._html())

    def test_max_open_positions_shown(self):
        self.assertIn("30", self._html())


class ChallengeCatalogDynamicDataTests(TestCase):
    """Verify that changing admin values changes what /challenges/ shows."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_custom_price_shown(self):
        make_challenge_product(price_usd=Decimal("249.00"), is_active=True)
        html = self.client.get(_url()).content.decode()
        self.assertIn("249", html)

    def test_custom_profit_split_shown(self):
        make_challenge_product(profit_split_pct=Decimal("90.00"), is_active=True)
        html = self.client.get(_url()).content.decode()
        self.assertIn("90", html)

    def test_inactive_product_not_shown(self):
        p = make_challenge_product(name="Hidden Product", is_active=False)
        html = self.client.get(_url()).content.decode()
        self.assertNotIn("Hidden Product", html)

    def test_active_product_shown(self):
        p = make_challenge_product(name="Visible Product", is_active=True)
        html = self.client.get(_url()).content.decode()
        self.assertIn("Visible Product", html)

    def test_multiple_products_all_shown(self):
        make_challenge_product(name="Product Alpha", tier="10K", is_active=True)
        make_challenge_product(name="Product Beta",  tier="25K", is_active=True)
        html = self.client.get(_url()).content.decode()
        self.assertIn("Product Alpha", html)
        self.assertIn("Product Beta", html)

    def test_single_product_renders_without_error(self):
        make_challenge_product(name="Solo Product", is_active=True)
        r = self.client.get(_url())
        self.assertEqual(r.status_code, 200)
        self.assertIn("Solo Product", r.content.decode())


class ChallengeCatalogEmptyStateTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_empty_catalog_returns_200(self):
        r = self.client.get(_url())
        self.assertEqual(r.status_code, 200)

    def test_empty_catalog_shows_fallback_message(self):
        html = self.client.get(_url()).content.decode()
        self.assertIn("No hay programas de fondeo", html)

    def test_empty_catalog_still_shows_hero(self):
        html = self.client.get(_url()).content.decode()
        self.assertIn("Programas de Fondeo", html)

    def test_empty_catalog_still_shows_como_funciona(self):
        html = self.client.get(_url()).content.decode()
        self.assertIn("Cómo funciona", html)

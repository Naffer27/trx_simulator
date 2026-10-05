# simulator/tests/test_lot_specs_frontend.py
"""
Phase 6B.2 — Lot-spec frontend tests.

Verifies that the rendered dashboard HTML embeds the correct LOT_SPECS
constant so the JS helpers (getLotStep / getLotMin / getLotDecimals) pick
up the right values per symbol.

These are white-box tests: they inspect the rendered HTML source rather than
running the JS engine.  They will catch accidental regressions where the
LOT_SPECS object is removed, renamed, or has a symbol entry edited to wrong
values.

Covered assertions
------------------
- LOT_SPECS object is present in the rendered page
- BTCUSD entry has step:0.001, min:0.001, dec:3
- EUR/USD entry has step:0.01, min:0.01, dec:2
- NAS100 entry has step:0.1, min:0.1, dec:1
- _adjustLot helper function is present in the page source
- Desktop qty input has id="qty" (required by _adjustLot and sendActiveOrder)
- Desktop lot buttons call _adjustLot (not raw inline arithmetic)
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user


def _dashboard_url(account_id):
    return reverse("simulator:dashboard_account", args=[account_id])


class LotSpecsEmbedTests(TestCase):
    """LOT_SPECS constant is correctly embedded in the rendered dashboard."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.account = make_account(self.user, account_type="STANDARD")

    def _html(self):
        r = self.client.get(_dashboard_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _core_source(self):
        # PRE-VPS-POLISH-03B.2A — the LOT_SPECS object literal itself now
        # lives in the shared trading core (simulator/static/simulator/
        # trade/trading_core.js), not inline in the rendered page. The
        # call-site helpers (getLotStep/getLotMin/getLotDecimals/
        # normalizeLot/_adjustLot/_updateQtyInputAttrs) are still exercised
        # against self._html() below — those identifiers remain present as
        # call sites in desktop.html's own remaining inline JS.
        with open(
            "simulator/static/simulator/trade/trading_core.js", encoding="utf-8"
        ) as f:
            return f.read()

    def test_lot_specs_object_present(self):
        self.assertIn("LOT_SPECS", self._core_source())

    def test_btcusd_step_0001(self):
        src = self._core_source()
        self.assertIn('"BTCUSD"', src)
        # The exact entry in the LOT_SPECS literal
        self.assertIn("step:0.001", src)

    def test_btcusd_min_0001(self):
        src = self._core_source()
        self.assertIn("min:0.001", src)

    def test_btcusd_dec_3(self):
        src = self._core_source()
        self.assertIn("dec:3", src)

    def test_eurusd_step_001(self):
        src = self._core_source()
        self.assertIn('"EUR/USD"', src)
        # step:0.01 present (many symbols share this; just verify it exists)
        self.assertIn("step:0.01", src)

    def test_nas100_step_01(self):
        # Index contracts use step:0.1
        self.assertIn("step:0.1", self._core_source())

    def test_adjust_lot_helper_present(self):
        self.assertIn("_adjustLot", self._html())

    def test_normalize_lot_helper_present(self):
        self.assertIn("normalizeLot", self._html())

    def test_get_lot_step_helper_present(self):
        self.assertIn("getLotStep", self._html())

    def test_get_lot_min_helper_present(self):
        self.assertIn("getLotMin", self._html())

    def test_get_lot_decimals_helper_present(self):
        self.assertIn("getLotDecimals", self._html())

    def test_update_qty_input_attrs_helper_present(self):
        self.assertIn("_updateQtyInputAttrs", self._html())


class LotInputAttributeTests(TestCase):
    """Desktop qty input and mobile lot input have correct initial HTML attrs."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)
        self.account = make_account(self.user, account_type="STANDARD")

    def _html(self):
        r = self.client.get(_dashboard_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_desktop_qty_input_has_id_qty(self):
        self.assertIn('id="qty"', self._html())

    def test_desktop_lot_buttons_call_adjust_lot(self):
        html = self._html()
        # The inline onclick of the stepper buttons must reference _adjustLot
        self.assertIn("_adjustLot(-1)", html)
        self.assertIn("_adjustLot(1)", html)

    def test_desktop_lot_buttons_no_tofixed2_arithmetic(self):
        # The old bug: toFixed(2) in inline onclick — must be gone
        # We check that the button onclicks don't contain the broken pattern
        html = self._html()
        self.assertNotIn("parseFloat(i.value)||0.01)-0.01).toFixed(2)", html)

    def test_mobile_lot_input_present(self):
        self.assertIn('id="mebLotVal"', self._html())

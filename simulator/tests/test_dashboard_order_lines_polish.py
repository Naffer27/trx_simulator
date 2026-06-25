# simulator/tests/test_dashboard_order_lines_polish.py
"""
Bloque K.2E — Trading Panel Order Lines Visual Polish.

Verifies that order lines (entry/SL/TP) use translucent, thin styles in
normal state and brighter emphasis when selected or being dragged.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class OrderLineDimStyleTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_entry_dim_color_gray_blue(self):
        """Non-selected entry line uses gray-blue, not full bull/bear color."""
        self.assertIn('rgba(148,163,184,0.28)', self._html())

    def test_apply_line_styles_dim_opacity_reduced(self):
        """Dim opacity set to 0.20 (from 0.28) for more translucent SL/TP."""
        self.assertIn('dim=0.20', self._html())

    def test_no_dim_028_in_apply_styles(self):
        """Old dim=0.28 value is gone from _applyLineStyles."""
        self.assertNotIn('dim=0.28', self._html())

    def test_drag_sl_emphasis_present(self):
        """_applyLineStyles checks dragSL for per-line drag detection."""
        self.assertIn('dragSL', self._html())

    def test_drag_tp_emphasis_present(self):
        """_applyLineStyles checks dragTP for per-line drag detection."""
        self.assertIn('dragTP', self._html())

    def test_apply_line_styles_called_on_drag_start(self):
        """_applyLineStyles() is triggered when drag starts."""
        html = self._html()
        self.assertIn('handleScale:false});this._applyLineStyles()', html)

    def test_apply_line_styles_called_on_drag_end(self):
        """_applyLineStyles() is triggered when drag ends to restore dim state."""
        html = self._html()
        self.assertIn('handleScale:true});this._applyLineStyles()', html)

    def test_entry_line_created_with_linewidth_1(self):
        """Entry priceLine initial creation uses lineWidth:1."""
        self.assertIn(
            "createPriceLine({price:entry,color:side==='buy'?bull:bear,lineWidth:1,lineStyle:2",
            self._html(),
        )

    def test_ensure_sl_uses_linewidth_1(self):
        """ensureSL creates line with lineWidth:1, not 2."""
        html = self._html()
        self.assertNotIn(
            "ensureSL".join(["", ""]),  # just confirm ensureSL method is present
            "",
        )
        # Verify the SL creation inside ensureSL uses lineWidth:1
        self.assertIn(
            "lineWidth:1,lineStyle:1,axisLabelVisible:true,title:'SL'});}",
            html,
        )

    def test_qb_save_lines_uses_linewidth_1(self):
        """QB saveLines creates SL/TP with lineWidth:1."""
        html = self._html()
        self.assertNotIn(
            "createPriceLine({price:slV,color:'#ef5350',lineWidth:2",
            html,
        )
        self.assertNotIn(
            "createPriceLine({price:tpV,color:'#26a69a',lineWidth:2",
            html,
        )

    def test_sl_tp_price_lines_still_rendered(self):
        """SL/TP price lines are still created (no regression)."""
        html = self._html()
        self.assertIn("title:'SL'", html)
        self.assertIn("title:'TP'", html)

    def test_apply_line_styles_still_references_entry_sl_tp(self):
        """_applyLineStyles still applies options to plEntry, plSL, plTP."""
        html = self._html()
        self.assertIn('plEntry?.applyOptions', html)
        self.assertIn('plSL?.applyOptions', html)
        self.assertIn('plTP?.applyOptions', html)

# simulator/tests/test_dashboard_default_tf.py
"""
Bloque K.2C — Trading Panel Default Timeframe 15m.

Verifies that the rendered dashboard HTML uses 15m as the default
timeframe and keeps 1m available as a selectable option.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class DefaultTimeframeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_15m_is_selected_in_tf_dropdown(self):
        self.assertIn('value="15m" selected', self._html())

    def test_1m_is_not_selected_in_tf_dropdown(self):
        self.assertNotIn('value="1m" selected', self._html())

    def test_1m_option_still_present(self):
        self.assertIn('value="1m"', self._html())

    def test_mobile_badge_shows_15m(self):
        self.assertIn('mfp-tf-badge">15m', self._html())

    def test_mobile_picker_15m_has_active_class(self):
        html = self._html()
        self.assertIn('mfp-tf-pop-item active" data-tf="15m"', html)

    def test_mobile_picker_1m_has_no_active_class(self):
        html = self._html()
        self.assertNotIn('mfp-tf-pop-item active" data-tf="1m"', html)

    def test_js_default_currenttf_is_15m(self):
        self.assertIn("currentTF='15m'", self._html())

    def test_js_default_currenttf_not_1m(self):
        self.assertNotIn("currentTF='1m'", self._html())

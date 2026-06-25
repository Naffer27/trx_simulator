# simulator/tests/test_dashboard_close_modal.py
"""
Bloque K.2B — Close Position Confirm Modal.

Verifies that the native confirm() calls were replaced by the custom
dark/gold modal (#closeConfirmModal) in the rendered dashboard HTML.
JS interaction is not testable via the Django test client.
"""
from django.test import TestCase
from django.urls import reverse

from simulator.tests.factories import make_account, make_user


def _url(pk):
    return reverse("simulator:dashboard_account", args=[pk])


class CloseConfirmModalTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = make_account(self.user, account_type="DEMO")
        self.client.force_login(self.user)

    def _html(self):
        r = self.client.get(_url(self.account.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_close_confirm_modal_present(self):
        self.assertIn('id="closeConfirmModal"', self._html())

    def test_cancel_button_present(self):
        self.assertIn('id="ccmCancel"', self._html())

    def test_confirm_button_present(self):
        self.assertIn('id="ccmConfirm"', self._html())

    def test_show_close_confirm_function_present(self):
        self.assertIn('_showCloseConfirm', self._html())

    def test_show_close_all_confirm_function_present(self):
        self.assertIn('showCloseAllConfirm', self._html())

    def test_native_confirm_not_used_for_single_close(self):
        html = self._html()
        self.assertNotIn("confirm('¿Cerrar esta posición?')", html)
        self.assertNotIn('confirm("¿Cerrar esta posición?")', html)

    def test_native_confirm_not_used_for_close_all(self):
        self.assertNotIn("confirm('Close ALL positions?')", self._html())

# simulator/tests/test_deposit_status_challenge.py
"""
Bloque K.2G.3/K.2G.4 — Challenge-aware Deposit Status + Direct Account Link.

Verifies that deposit_status.html and deposit_status_json are aware of
whether a Deposit belongs to a challenge, showing appropriate messages,
buttons, and a direct link to the activated Phase 1 account when available.
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from simulator.models import Deposit
from simulator.tests.factories import (
    make_account,
    make_challenge_enrollment,
    make_challenge_product,
    make_user,
    make_wallet,
)


def _status_url(pk):
    return reverse("simulator:deposit_status", args=[pk])


def _json_url(pk):
    return reverse("simulator:deposit_status_json", args=[pk])


def _make_wallet_deposit(user, status=Deposit.STATUS_WAITING, credited=False):
    """Regular wallet top-up — no challenge_product."""
    d = Deposit.objects.create(
        user=user,
        amount_usd=Decimal("100.00"),
        crypto_currency="btc",
        status=status,
        credited=credited,
        nowpayments_payment_id="wallet_pay_001",
    )
    return d


def _make_challenge_deposit(user, product, status=Deposit.STATUS_WAITING, credited=False):
    """Challenge purchase deposit — tagged with challenge_product."""
    d = Deposit.objects.create(
        user=user,
        amount_usd=product.price_usd,
        crypto_currency="btc",
        status=status,
        credited=credited,
        nowpayments_payment_id="challenge_pay_001",
        challenge_product=product,
    )
    return d


class DepositStatusWalletTests(TestCase):
    """Normal wallet deposits must not be affected by challenge changes."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        self.client.force_login(self.user)

    def _html(self, deposit):
        r = self.client.get(_status_url(deposit.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_wallet_deposit_shows_deposit_acreditado_when_credited(self):
        d = _make_wallet_deposit(self.user, status=Deposit.STATUS_FINISHED, credited=True)
        self.assertIn("Depósito acreditado", self._html(d))

    def test_wallet_deposit_shows_wallet_credited_sub(self):
        d = _make_wallet_deposit(self.user, status=Deposit.STATUS_FINISHED, credited=True)
        self.assertIn("añadidos a tu wallet", self._html(d))

    def test_wallet_deposit_shows_ver_mis_cuentas_button(self):
        d = _make_wallet_deposit(self.user, status=Deposit.STATUS_FINISHED, credited=True)
        self.assertIn("Ver Mis Cuentas", self._html(d))

    def test_wallet_deposit_does_not_show_challenge_activado(self):
        d = _make_wallet_deposit(self.user, status=Deposit.STATUS_FINISHED, credited=True)
        self.assertNotIn("Challenge activado", self._html(d))

    def test_wallet_deposit_shows_historial_link(self):
        d = _make_wallet_deposit(self.user)
        self.assertIn("Historial", self._html(d))

    def test_wallet_deposit_shows_nuevo_deposito_link(self):
        d = _make_wallet_deposit(self.user)
        self.assertIn("Nuevo depósito", self._html(d))

    def test_wallet_deposit_is_challenge_false_in_context(self):
        d = _make_wallet_deposit(self.user)
        r = self.client.get(_status_url(d.pk))
        self.assertFalse(r.context["is_challenge"])

    def test_wallet_deposit_challenge_product_none_in_context(self):
        d = _make_wallet_deposit(self.user)
        r = self.client.get(_status_url(d.pk))
        self.assertIsNone(r.context["challenge_product"])


class DepositStatusChallengePendingTests(TestCase):
    """Challenge deposit while waiting for payment."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        self.product = make_challenge_product(name="Pro 10K")
        self.client.force_login(self.user)

    def _html(self, deposit):
        r = self.client.get(_status_url(deposit.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_challenge_pending_shows_esperando_confirmacion(self):
        d = _make_challenge_deposit(self.user, self.product, status=Deposit.STATUS_WAITING)
        self.assertIn("Esperando confirmación del pago", self._html(d))

    def test_challenge_pending_shows_activara_automaticamente(self):
        d = _make_challenge_deposit(self.user, self.product, status=Deposit.STATUS_WAITING)
        self.assertIn("activará automáticamente", self._html(d))

    def test_challenge_pending_does_not_show_fondos_wallet(self):
        d = _make_challenge_deposit(self.user, self.product, status=Deposit.STATUS_WAITING)
        self.assertNotIn("añadidos a tu wallet", self._html(d))

    def test_challenge_pending_is_challenge_true_in_context(self):
        d = _make_challenge_deposit(self.user, self.product)
        r = self.client.get(_status_url(d.pk))
        self.assertTrue(r.context["is_challenge"])

    def test_challenge_pending_challenge_product_in_context(self):
        d = _make_challenge_deposit(self.user, self.product)
        r = self.client.get(_status_url(d.pk))
        self.assertEqual(r.context["challenge_product"], self.product)

    def test_challenge_pending_shows_ver_challenges_link(self):
        d = _make_challenge_deposit(self.user, self.product)
        html = self._html(d)
        self.assertIn("Ver Challenges", html)

    def test_challenge_pending_no_wallet_action_links(self):
        # "Nuevo depósito" appears only in the wallet action-buttons block.
        # It must be absent on challenge pages (sidebar doesn't have it).
        d = _make_challenge_deposit(self.user, self.product)
        self.assertNotIn("Nuevo depósito", self._html(d))


class DepositStatusChallengeCreditedTests(TestCase):
    """Challenge deposit after payment is confirmed and enrollment activated."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        self.product = make_challenge_product(name="Elite 50K")
        self.client.force_login(self.user)

    def _html(self, deposit):
        r = self.client.get(_status_url(deposit.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_challenge_credited_shows_challenge_activado(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertIn("Challenge activado", self._html(d))

    def test_challenge_credited_shows_phase1_lista(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertIn("Phase 1 está lista", self._html(d))

    def test_challenge_credited_without_enrollment_shows_fallback_button(self):
        # No enrollment → no account_url → fallback text "Ir al Panel de Trading"
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertIn("Ir al Panel de Trading", self._html(d))

    def test_challenge_credited_does_not_show_deposito_acreditado(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertNotIn("Depósito acreditado", self._html(d))

    def test_challenge_credited_does_not_show_fondos_wallet(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertNotIn("añadidos a tu wallet", self._html(d))

    def test_challenge_credited_does_not_show_ver_mis_cuentas(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertNotIn("Ver Mis Cuentas", self._html(d))

    def test_challenge_credited_js_constants_present(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        html = self._html(d)
        self.assertIn("IS_CHALLENGE  = true", html)
        self.assertIn("Elite 50K", html)


class DepositStatusJsonChallengeTests(TestCase):
    """deposit_status_json returns is_challenge and challenge_name."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        self.product = make_challenge_product(name="Trader 25K")
        self.client.force_login(self.user)

    def _json(self, deposit):
        r = self.client.get(_json_url(deposit.pk))
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_json_challenge_deposit_is_challenge_true(self):
        d = _make_challenge_deposit(self.user, self.product)
        self.assertTrue(self._json(d)["is_challenge"])

    def test_json_challenge_deposit_challenge_name(self):
        d = _make_challenge_deposit(self.user, self.product)
        self.assertEqual(self._json(d)["challenge_name"], "Trader 25K")

    def test_json_wallet_deposit_is_challenge_false(self):
        d = _make_wallet_deposit(self.user)
        self.assertFalse(self._json(d)["is_challenge"])

    def test_json_wallet_deposit_challenge_name_empty(self):
        d = _make_wallet_deposit(self.user)
        self.assertEqual(self._json(d)["challenge_name"], "")

    def test_json_has_status_and_credited(self):
        d = _make_challenge_deposit(self.user, self.product)
        data = self._json(d)
        self.assertIn("status", data)
        self.assertIn("credited", data)

    def test_json_account_id_none_when_not_credited(self):
        d = _make_challenge_deposit(self.user, self.product)
        self.assertIsNone(self._json(d)["account_id"])

    def test_json_account_id_none_for_wallet_deposit(self):
        d = _make_wallet_deposit(self.user)
        self.assertIsNone(self._json(d)["account_id"])

    def test_json_account_url_none_when_not_credited(self):
        d = _make_challenge_deposit(self.user, self.product)
        self.assertIsNone(self._json(d)["account_url"])

    def test_json_account_url_none_for_wallet_deposit(self):
        d = _make_wallet_deposit(self.user)
        self.assertIsNone(self._json(d)["account_url"])


class DepositStatusDirectLinkTests(TestCase):
    """K.2G.4 — Direct link to activated Phase 1 account after challenge confirmation."""

    def setUp(self):
        self.user = make_user()
        make_wallet(self.user)
        self.product = make_challenge_product(name="Pro 10K")
        self.client.force_login(self.user)

    def _make_credited_challenge_with_account(self):
        """Helper: credited challenge deposit + enrollment with a Phase 1 account."""
        account = make_account(user=self.user, tier="10K")
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        make_challenge_enrollment(
            user=self.user,
            product=self.product,
            deposit=d,
            phase1_account=account,
        )
        return d, account

    def _html(self, deposit):
        r = self.client.get(_status_url(deposit.pk))
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def _json(self, deposit):
        r = self.client.get(_json_url(deposit.pk))
        self.assertEqual(r.status_code, 200)
        return r.json()

    # ── HTML button ───────────────────────────────────────────────────────

    def test_credited_with_account_shows_ir_a_mi_cuenta_challenge(self):
        d, _ = self._make_credited_challenge_with_account()
        self.assertIn("Ir a mi cuenta Challenge", self._html(d))

    def test_credited_with_account_does_not_show_ir_al_panel_fallback(self):
        d, _ = self._make_credited_challenge_with_account()
        self.assertNotIn("Ir al Panel de Trading", self._html(d))

    def test_credited_with_account_links_to_dashboard(self):
        d, account = self._make_credited_challenge_with_account()
        expected = f"/dashboard/{account.id}/"
        self.assertIn(expected, self._html(d))

    def test_credited_without_account_shows_fallback_text(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertIn("Ir al Panel de Trading", self._html(d))

    def test_credited_without_account_does_not_show_mi_cuenta_challenge(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertNotIn("Ir a mi cuenta Challenge", self._html(d))

    def test_wallet_deposit_credited_does_not_show_ir_a_mi_cuenta(self):
        d = _make_wallet_deposit(self.user, status=Deposit.STATUS_FINISHED, credited=True)
        self.assertNotIn("Ir a mi cuenta Challenge", self._html(d))

    # ── Context ───────────────────────────────────────────────────────────

    def test_context_account_url_present_when_enrollment_exists(self):
        d, account = self._make_credited_challenge_with_account()
        r = self.client.get(_status_url(d.pk))
        self.assertEqual(r.context["account_url"], f"/dashboard/{account.id}/")

    def test_context_account_url_none_without_enrollment(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        r = self.client.get(_status_url(d.pk))
        self.assertIsNone(r.context["account_url"])

    def test_context_account_id_present_when_enrollment_exists(self):
        d, account = self._make_credited_challenge_with_account()
        r = self.client.get(_status_url(d.pk))
        self.assertEqual(r.context["account_id"], account.id)

    # ── JSON endpoint ─────────────────────────────────────────────────────

    def test_json_account_url_present_when_enrollment_exists(self):
        d, account = self._make_credited_challenge_with_account()
        data = self._json(d)
        self.assertEqual(data["account_url"], f"/dashboard/{account.id}/")

    def test_json_account_id_present_when_enrollment_exists(self):
        d, account = self._make_credited_challenge_with_account()
        self.assertEqual(self._json(d)["account_id"], account.id)

    def test_json_account_url_none_without_enrollment(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertIsNone(self._json(d)["account_url"])

    # ── JS constant ───────────────────────────────────────────────────────

    def test_account_url_js_constant_present_when_enrollment_exists(self):
        d, account = self._make_credited_challenge_with_account()
        html = self._html(d)
        self.assertIn(f"ACCOUNT_URL   = \"/dashboard/{account.id}/\"", html)

    def test_account_url_js_constant_empty_without_enrollment(self):
        d = _make_challenge_deposit(
            self.user, self.product,
            status=Deposit.STATUS_FINISHED, credited=True,
        )
        self.assertIn('ACCOUNT_URL   = ""', self._html(d))

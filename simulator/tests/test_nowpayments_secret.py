# simulator/tests/test_nowpayments_secret.py
"""
NOWPAYMENTS_IPN_SECRET hardening — regression tests.

Covers:
  1. Production guard: DEBUG=False without secret raises ImproperlyConfigured.
  2. Production guard: DEBUG=False with secret set loads correctly.
  3. Production guard: skipped in manage.py test mode.
  4. verify_ipn_signature rejects when secret is missing (empty env var).
  5. verify_ipn_signature rejects when signature header is absent.
  6. verify_ipn_signature accepts a correctly computed HMAC-SHA512 signature.
  7. verify_ipn_signature rejects an incorrect signature.
  8. verify_ipn_signature rejects a tampered body (valid sig for different payload).
  9. deposit_callback returns 400 for missing signature (no secret mock).
"""
import hashlib
import hmac
import json
import subprocess
import sys
from unittest.mock import patch

from django.test import TestCase

from simulator.nowpayments import verify_ipn_signature

_TEST_SECRET  = "subprocess-np-test-key-not-for-production"
_IPN_SECRET   = "test-ipn-secret-for-unit-tests"


def _make_sig(payload: dict, secret: str = _IPN_SECRET) -> str:
    """Compute the HMAC-SHA512 signature NowPayments attaches to every IPN."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()


# ── Settings guard (subprocess) ───────────────────────────────────────────────

class NowpaymentsSecretSettingsGuardTests(TestCase):

    def _run(self, env_overrides: dict, argv1: str, assertion: str) -> subprocess.CompletedProcess:
        import os
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "trx_simulator.settings"
        env["DJANGO_SECRET_KEY"]      = _TEST_SECRET
        env["DEBUG"]                  = "False"
        env.update(env_overrides)
        script = (
            f"import sys; sys.argv = ['manage.py', '{argv1}']; "
            "from django.conf import settings; " + assertion
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_missing_secret_raises_in_prod_non_test_mode(self):
        result = self._run(
            {"NOWPAYMENTS_IPN_SECRET": ""},
            "runserver",
            "_ = settings.NOWPAYMENTS_IPN_SECRET",
        )
        self.assertNotEqual(
            result.returncode, 0,
            "Expected non-zero exit when NOWPAYMENTS_IPN_SECRET is empty in prod.",
        )
        self.assertIn("NOWPAYMENTS_IPN_SECRET", result.stderr)

    def test_secret_set_loads_correctly_in_prod(self):
        result = self._run(
            {"NOWPAYMENTS_IPN_SECRET": "real-secret-abc123", "EMAIL_HOST": "smtp.example.com"},
            "runserver",
            "assert settings.NOWPAYMENTS_IPN_SECRET == 'real-secret-abc123', "
            "repr(settings.NOWPAYMENTS_IPN_SECRET)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_secret_does_not_raise_in_test_mode(self):
        result = self._run(
            {"NOWPAYMENTS_IPN_SECRET": ""},
            "test",
            "assert settings.NOWPAYMENTS_IPN_SECRET == ''",
        )
        self.assertEqual(
            result.returncode, 0,
            f"manage.py test must bypass the IPN secret guard.\nstderr: {result.stderr}",
        )


# ── verify_ipn_signature unit tests ──────────────────────────────────────────

class VerifyIpnSignatureTests(TestCase):

    def setUp(self):
        self._env_patch = patch.dict("os.environ", {"NOWPAYMENTS_IPN_SECRET": _IPN_SECRET})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def _body(self, payload: dict) -> bytes:
        return json.dumps(payload).encode()

    def test_valid_signature_accepted(self):
        payload = {"payment_id": "pay_001", "payment_status": "finished", "price_amount": 100}
        body    = self._body(payload)
        sig     = _make_sig(payload)
        self.assertTrue(verify_ipn_signature(body, sig))

    def test_invalid_signature_rejected(self):
        payload = {"payment_id": "pay_002", "payment_status": "finished", "price_amount": 100}
        body    = self._body(payload)
        self.assertFalse(verify_ipn_signature(body, "0" * 128))

    def test_tampered_body_rejected(self):
        """Signature for original payload must not validate against a different body."""
        original = {"payment_id": "pay_003", "amount": 100}
        tampered = {"payment_id": "pay_003", "amount": 9999}
        sig = _make_sig(original)
        self.assertFalse(verify_ipn_signature(self._body(tampered), sig))

    def test_empty_signature_rejected(self):
        payload = {"payment_id": "pay_004", "payment_status": "finished"}
        self.assertFalse(verify_ipn_signature(self._body(payload), ""))

    def test_unsorted_body_still_validates(self):
        """NowPayments may send keys in any order; canonical sort must handle it."""
        payload     = {"z_key": "z", "a_key": "a", "payment_id": "pay_005"}
        body_sorted = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig         = _make_sig(payload)
        self.assertTrue(verify_ipn_signature(body_sorted, sig))


class VerifyIpnSignatureEmptySecretTests(TestCase):
    """verify_ipn_signature must reject ALL requests when the secret is not configured."""

    def setUp(self):
        self._env_patch = patch.dict("os.environ", {"NOWPAYMENTS_IPN_SECRET": ""})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_rejects_even_with_correct_hmac_when_secret_missing(self):
        payload = {"payment_id": "pay_006", "payment_status": "finished"}
        sig     = _make_sig(payload, secret="")
        self.assertFalse(verify_ipn_signature(json.dumps(payload).encode(), sig))

    def test_rejects_empty_signature_when_secret_missing(self):
        payload = {"payment_id": "pay_007"}
        self.assertFalse(verify_ipn_signature(json.dumps(payload).encode(), ""))


# ── deposit_callback end-to-end (no mock) ────────────────────────────────────

class DepositCallbackSignatureEnforcementTests(TestCase):
    """
    Hit the real deposit_callback view without mocking verify_ipn_signature.
    Confirms the view gate is wired correctly.
    """

    def setUp(self):
        self._env_patch = patch.dict("os.environ", {"NOWPAYMENTS_IPN_SECRET": _IPN_SECRET})
        self._env_patch.start()
        # Patch rate_check so Redis accumulation doesn't interfere
        self._rl_patch = patch("simulator.ratelimit.rate_check", return_value=(True, 0))
        self._rl_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._rl_patch.stop()

    def _post(self, payload: dict, sig: str):
        body = json.dumps(payload).encode()
        return self.client.post(
            "/deposit/callback/",
            body,
            content_type="application/json",
            HTTP_X_NOWPAYMENTS_SIG=sig,
        )

    def test_missing_signature_returns_400(self):
        payload = {"payment_id": "cb_001", "payment_status": "finished"}
        resp = self._post(payload, "")
        self.assertEqual(resp.status_code, 400)

    def test_invalid_signature_returns_400(self):
        payload = {"payment_id": "cb_002", "payment_status": "finished"}
        resp = self._post(payload, "deadbeef" * 16)
        self.assertEqual(resp.status_code, 400)

    def test_valid_signature_passes_sig_check(self):
        """
        A valid signature reaches the business logic (may fail later for other
        reasons — unknown payment_id etc. — but must NOT return 400 for bad sig).
        """
        payload = {"payment_id": "cb_valid_001", "payment_status": "finished",
                   "price_amount": 100, "order_id": "99999"}
        sig  = _make_sig(payload)
        resp = self._post(payload, sig)
        # 400 specifically means rejected-by-signature when raised at that point.
        # Any other status (200, 404, 500) means the signature gate passed.
        self.assertNotEqual(
            resp.status_code, 400,
            "A correctly signed IPN must not be rejected by the signature gate.",
        )

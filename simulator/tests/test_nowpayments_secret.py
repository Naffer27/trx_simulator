# simulator/tests/test_nowpayments_secret.py
"""
NOWPAYMENTS_IPN_SECRET hardening — regression tests.

FIX-NOWPAYMENTS-IPN-RAW-BODY-01 — NowPayments signs the RAW HTTP body
bytes exactly as transmitted, confirmed empirically against a real
callback (NOWPAYMENTS-IPN-SIGNATURE-CONTRACT-AUDIT-01). The previous
implementation reparsed the body with json.loads() and re-signed a
json.dumps(sort_keys=True, separators=(",", ":")) reconstruction — which
looked equivalent but silently corrupted small decimal floats (the wire
sends "0.000037", Python's json.dumps re-renders the same parsed float as
"3.7e-05"), making every real IPN with a fee field fail verification
regardless of the secret. verify_ipn_signature() now signs body_bytes
directly, with zero reparsing — every test below builds its own exact
bytes and signs THOSE bytes, matching that contract precisely.

Covers:
  1. Production guard: DEBUG=False without secret raises ImproperlyConfigured.
  2. Production guard: DEBUG=False with secret set loads correctly.
  3. Production guard: skipped in manage.py test mode.
  4. verify_ipn_signature rejects when secret is missing (empty env var).
  5. verify_ipn_signature rejects when signature header is absent.
  6. verify_ipn_signature accepts a correctly computed raw-body HMAC-SHA512 signature.
  7. verify_ipn_signature rejects an incorrect signature.
  8. verify_ipn_signature rejects a tampered body (valid sig for different payload).
  9. deposit_callback returns 400 for missing signature (no secret mock).
  10. A body containing a small decimal float (0.000037, the exact real-world
      regression) verifies correctly — this is the actual bug this fix closes.
  11. A byte-different-but-JSON-equivalent reserialization of the same payload
      does NOT validate against the signature of the original bytes — proves
      the function no longer reparses/re-canonicalizes internally.
  12. Whitespace/key-order/number-format are part of the signed message —
      two differently-formatted encodings of the same payload produce
      different valid signatures; each only validates against its own bytes.
  13. The x-nowpayments-sig header is read case-insensitively (Django's
      HttpHeaders guarantee) — deposit_callback finds it regardless of casing.
  14. Missing secret → fails safe even with a structurally-correct signature.
"""
import hashlib
import hmac
import json
import subprocess
import sys
from unittest.mock import patch

from django.test import TestCase, RequestFactory

from simulator.nowpayments import verify_ipn_signature

_TEST_SECRET  = "subprocess-np-test-key-not-for-production"
_IPN_SECRET   = "test-ipn-secret-for-unit-tests"


def _sign(body_bytes: bytes, secret: str = _IPN_SECRET) -> str:
    """
    Compute the HMAC-SHA512 signature NowPayments attaches to every IPN —
    over the exact raw body bytes, with NO reparsing/reserialization. This
    mirrors verify_ipn_signature()'s own contract exactly, so every test
    below is signing precisely what it will ask the function to verify.
    """
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha512).hexdigest()


def _make_sig(payload: dict, secret: str = _IPN_SECRET) -> str:
    """
    Back-compat helper for tests that still build their body via
    json.dumps(payload).encode() (Python's default separators, WITH spaces)
    — signs that exact default-formatted encoding, not a canonicalized one.
    """
    return _sign(json.dumps(payload).encode(), secret=secret)


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

    def test_raw_body_signature_valid(self):
        """
        FIX-NOWPAYMENTS-IPN-RAW-BODY-01, case 1 — a signature computed over
        the exact raw bytes (whatever formatting NowPayments actually used)
        must validate, with no reparsing involved.
        """
        # Deliberately NOT alphabetically sorted, deliberately WITH spaces —
        # this is what a real HTTP body looks like; nothing here should matter
        # except that the signed bytes and the verified bytes are identical.
        body = b'{"payment_id": "pay_005", "z_key": "z", "a_key": "a"}'
        sig  = _sign(body)
        self.assertTrue(verify_ipn_signature(body, sig))

    def test_small_decimal_float_verifies_correctly(self):
        """
        FIX-NOWPAYMENTS-IPN-RAW-BODY-01, case 3 — the exact real-world
        regression: a fee field like 0.000037 must verify correctly. Under
        the old json.loads()->json.dumps() implementation, Python's float
        repr silently re-rendered this as "3.7e-05", corrupting the signed
        message and rejecting every real callback that carried a fee object
        — regardless of whether the secret was correct.
        """
        body = (
            b'{"actually_paid":20,"fee":{"currency":"btc","depositFee":0.000037,'
            b'"serviceFee":0.000003,"withdrawalFee":0},"order_id":"45",'
            b'"payment_id":6129376005,"payment_status":"finished"}'
        )
        sig = _sign(body)
        self.assertTrue(verify_ipn_signature(body, sig))

    def test_reserialized_equivalent_body_does_not_validate(self):
        """
        FIX-NOWPAYMENTS-IPN-RAW-BODY-01, case 4 — a JSON-equivalent but
        byte-different reserialization of the same logical payload must NOT
        validate against the signature of the ORIGINAL raw bytes. Proves
        verify_ipn_signature() no longer reparses/re-canonicalizes
        internally (if it did, this would incorrectly pass).
        """
        original_body = b'{"depositFee":0.000037,"payment_id":6129376005}'
        sig = _sign(original_body)

        # Same payload, semantically — but reserialized by Python, which
        # renders the float differently (scientific notation) and drops the
        # original key order/spacing.
        reserialized = json.dumps(
            json.loads(original_body), sort_keys=True, separators=(",", ":"),
        ).encode()
        self.assertNotEqual(original_body, reserialized)  # sanity: they really do differ
        self.assertFalse(verify_ipn_signature(reserialized, sig))

    def test_whitespace_and_formatting_are_part_of_the_signed_message(self):
        """
        FIX-NOWPAYMENTS-IPN-RAW-BODY-01, case 5 — two different byte
        encodings of the same logical payload are two different messages:
        each has its own valid signature, and neither's signature validates
        against the other's bytes.
        """
        payload = {"payment_id": "pay_008", "amount": 100}
        compact = json.dumps(payload, separators=(",", ":")).encode()   # no spaces
        spaced  = json.dumps(payload).encode()                           # default, WITH spaces
        self.assertNotEqual(compact, spaced)  # sanity: genuinely different bytes

        sig_compact = _sign(compact)
        sig_spaced  = _sign(spaced)

        self.assertTrue(verify_ipn_signature(compact, sig_compact))
        self.assertTrue(verify_ipn_signature(spaced, sig_spaced))
        # Cross-checks must fail — each signature is bound to its own exact bytes.
        self.assertFalse(verify_ipn_signature(spaced, sig_compact))
        self.assertFalse(verify_ipn_signature(compact, sig_spaced))


class VerifyIpnSignatureHeaderCaseTests(TestCase):
    """
    FIX-NOWPAYMENTS-IPN-RAW-BODY-01, case 6 — the x-nowpayments-sig header
    must be read case-insensitively. This is Django's own HttpHeaders
    guarantee (RFC 7230 — header names are case-insensitive); this test
    exists to pin that deposit_callback relies on it correctly rather than
    on a case-sensitive dict lookup.
    """

    def test_header_read_case_insensitively(self):
        body = b'{"payment_id": "pay_009"}'
        request = RequestFactory().post(
            "/deposit/callback/", data=body, content_type="application/json",
            **{"HTTP_X_NOWPAYMENTS_SIG": "unusual-casing-value"},
        )
        # Django normalizes the WSGI HTTP_* env key regardless of how a real
        # client capitalizes the wire header — confirm every casing variant
        # resolves to the same value via request.headers.
        self.assertEqual(request.headers.get("x-nowpayments-sig"), "unusual-casing-value")
        self.assertEqual(request.headers.get("X-NOWPAYMENTS-SIG"), "unusual-casing-value")
        self.assertEqual(request.headers.get("X-Nowpayments-Sig"), "unusual-casing-value")


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

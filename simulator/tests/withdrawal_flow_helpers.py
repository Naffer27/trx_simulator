"""
simulator/tests/withdrawal_flow_helpers.py

Shared helpers for driving the WITHDRAWAL-SECURITY-EXTENSION-01 two-step
withdrawal flow (POST /withdraw/ -> creates WithdrawalEmailOTPChallenge ->
POST /withdraw/otp/ -> creates WithdrawalRequest) in tests, without every
test file re-deriving the same plumbing.

generate_code() lives in simulator/withdrawal_otp.py and is called as a
bare name from within that module (module-global lookup at call time), so
patching "simulator.withdrawal_otp.generate_code" makes both
create_challenge() and resend_challenge() deterministic.
"""
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

from simulator.models import WithdrawalEmailOTPChallenge

FIXED_OTP_CODE = "123456"

PATCH_TOTP      = patch("simulator.two_factor.verify_totp_code", return_value=True)
PATCH_EMAIL     = patch("simulator.tasks.send_email_async.delay")
PATCH_RATELIMIT = patch("simulator.ratelimit.rate_check", return_value=(True, 0))


# WITHDRAWAL-POLICY-CORRECTION-01 — withdrawals <=$1,000 now trigger
# submit_withdrawal_to_provider() automatically from inside
# withdraw_otp_verify_view._finalize() (no admin action in between). Every
# test that drives the full two-step flow through this helper must NOT hit
# the real NowPayments API — same fake-adapter shape already used by
# test_withdrawal_second_approval.py's admin-triggered path.
class _FakeAutoPayoutAdapter:
    provider_name = "nowpayments"

    def estimate(self, amount_usd, asset):
        return Decimal("0.001")

    def create_payout(self, attempt, *, callback_url=""):
        class _Result:
            accepted = True
            provider_reference = "wd-auto-test"
            provider_batch_id = "batch-auto-test"
            provider_amount = Decimal("0.001")
            raw_status = "CREATED"
        return _Result()


PATCH_AUTO_PAYOUT_ADAPTER = patch(
    "simulator.payout_providers.NowPaymentsAdapter", return_value=_FakeAutoPayoutAdapter()
)


@contextmanager
def fixed_otp_code(code: str = FIXED_OTP_CODE):
    with patch("simulator.withdrawal_otp.generate_code", return_value=code):
        yield code


def latest_challenge(user, purpose=WithdrawalEmailOTPChallenge.PURPOSE_WITHDRAWAL):
    return (
        WithdrawalEmailOTPChallenge.objects
        .filter(user=user, purpose=purpose)
        .order_by("-created_at")
        .first()
    )


def submit_withdraw_request(
    client, user, *, verified_wallet, amount_usd=None, withdraw_all=False,
    crypto_currency="usdttrc20", totp_code="000000",
):
    """POST /withdraw/ — step 1. Returns (response, challenge_or_None)."""
    payload = {
        "crypto_currency": crypto_currency,
        "wallet_address": str(verified_wallet.pk),
        "otp_code": totp_code,
    }
    if withdraw_all:
        payload["withdraw_all"] = "on"
    if amount_usd is not None:
        payload["amount_usd"] = str(amount_usd)
    resp = client.post("/withdraw/", payload)
    challenge = latest_challenge(user)
    return resp, challenge


def submit_withdraw_otp(client, challenge, code: str):
    """
    POST /withdraw/otp/ — step 2. For required_approvals==1 challenges this
    now also triggers the automatic payout submission (WITHDRAWAL-POLICY-
    CORRECTION-01) — wrapped in PATCH_AUTO_PAYOUT_ADAPTER so it never hits
    the real NowPayments API.
    """
    with PATCH_AUTO_PAYOUT_ADAPTER:
        return client.post("/withdraw/otp/", {"challenge_id": challenge.id, "code": code})


def full_withdraw_flow(
    client, user, *, verified_wallet, amount_usd=None, withdraw_all=False,
    crypto_currency="usdttrc20", totp_code="000000", otp_code=FIXED_OTP_CODE,
):
    """
    Drive the complete two-step flow with a fixed, known OTP code.
    Returns (step1_response, step2_response_or_None, challenge_or_None).
    """
    with fixed_otp_code(otp_code):
        r1, challenge = submit_withdraw_request(
            client, user, verified_wallet=verified_wallet, amount_usd=amount_usd,
            withdraw_all=withdraw_all, crypto_currency=crypto_currency, totp_code=totp_code,
        )
        if challenge is None:
            return r1, None, None
        r2 = submit_withdraw_otp(client, challenge, otp_code)
    return r1, r2, challenge

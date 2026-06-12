"""
simulator/readiness.py
Reusable helper for computing user withdrawal/onboarding readiness.
"""
from decimal import Decimal


def get_user_readiness(user) -> dict:
    """
    Return a readiness dict for *user*.

    Keys:
      email_verified          bool
      terms_accepted          bool
      kyc_status              str  ('none'|'pending'|'approved'|'rejected')
      kyc_approved            bool
      totp_enabled            bool
      wallet_available_balance Decimal
      can_withdraw            bool  (all four gates pass)
      missing_requirements    list[dict]  each: {key, label, url_name}
    """
    from .models import KYCProfile, TOTPDevice, TermsAcceptance, TERMS_VERSION, RISK_DISCLOSURE_VERSION

    # ── Email verification ────────────────────────────────────────────────────
    try:
        email_verified = bool(user.email_verification.verified)
    except Exception:
        email_verified = False

    # ── Terms acceptance ──────────────────────────────────────────────────────
    try:
        terms_accepted = TermsAcceptance.objects.filter(
            user=user,
            terms_version=TERMS_VERSION,
            risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
        ).exists()
    except Exception:
        terms_accepted = False

    # ── KYC ──────────────────────────────────────────────────────────────────
    try:
        kyc = KYCProfile.objects.get(user=user)
        kyc_status = kyc.status
        kyc_approved = kyc.status == KYCProfile.STATUS_APPROVED
    except KYCProfile.DoesNotExist:
        kyc_status = "none"
        kyc_approved = False

    # ── 2FA ──────────────────────────────────────────────────────────────────
    totp_enabled = TOTPDevice.objects.filter(user=user, confirmed=True).exists()

    # ── Wallet balance (read-only, best-effort) ───────────────────────────────
    try:
        wallet_available_balance = user.wallet.available_balance
    except Exception:
        wallet_available_balance = Decimal("0")

    # ── Missing requirements list ─────────────────────────────────────────────
    missing = []
    if not email_verified:
        missing.append({
            "key": "email",
            "label": "Verificar email",
            "url_name": "simulator:resend_verification",
        })
    if not terms_accepted:
        missing.append({
            "key": "terms",
            "label": "Aceptar términos y condiciones",
            "url_name": "simulator:accept_terms",
        })
    if not kyc_approved:
        if kyc_status == KYCProfile.STATUS_PENDING:
            label = "KYC en revisión — aguarda aprobación"
        elif kyc_status == KYCProfile.STATUS_REJECTED:
            label = "KYC rechazado — vuelve a enviar documentos"
        else:
            label = "Completar verificación KYC"
        missing.append({"key": "kyc", "label": label, "url_name": "simulator:kyc"})
    if not totp_enabled:
        missing.append({
            "key": "totp",
            "label": "Activar autenticación 2FA",
            "url_name": "simulator:totp_setup",
        })

    can_withdraw = email_verified and terms_accepted and kyc_approved and totp_enabled

    return {
        "email_verified": email_verified,
        "terms_accepted": terms_accepted,
        "kyc_status": kyc_status,
        "kyc_approved": kyc_approved,
        "totp_enabled": totp_enabled,
        "wallet_available_balance": wallet_available_balance,
        "can_withdraw": can_withdraw,
        "missing_requirements": missing,
    }

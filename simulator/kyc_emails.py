"""
simulator/kyc_emails.py

Notification emails for KYC review outcomes.
Caller always wraps with try/except so a queuing failure never aborts the
admin action.
"""
import logging

logger = logging.getLogger("simulator.kyc_emails")

_BRAND = "Money Brokers"


def send_kyc_approved_email(kyc_profile) -> None:
    """Queue an approval notification to the KYC profile owner."""
    from django.utils import timezone
    from .tasks import send_email_async

    username = kyc_profile.user.username
    now_str  = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"Money Brokers — KYC aprobado"
    body = (
        f"Hola {username},\n\n"
        f"Tu verificación KYC fue aprobada.\n"
        f"Ya puedes solicitar retiros si cumples los demás requisitos.\n\n"
        f"  Estado:  Aprobado\n"
        f"  Fecha:   {now_str}\n\n"
        f"— {_BRAND}"
    )

    send_email_async.delay(
        subject=subject,
        message=body,
        recipient_list=[kyc_profile.user.email],
    )
    logger.info("[kyc_email] queued approved to=%s", kyc_profile.user.email)


def send_kyc_rejected_email(kyc_profile) -> None:
    """Queue a rejection notification to the KYC profile owner."""
    from django.utils import timezone
    from .tasks import send_email_async

    username = kyc_profile.user.username
    now_str  = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    reason   = (getattr(kyc_profile, "rejection_reason", "") or "").strip()

    reason_line = f"  Motivo:  {reason}\n" if reason else ""

    subject = f"Money Brokers — KYC requiere revisión"
    body = (
        f"Hola {username},\n\n"
        f"Tu verificación KYC requiere revisión.\n\n"
        f"  Estado:  Rechazado\n"
        f"  Fecha:   {now_str}\n"
        f"{reason_line}"
        f"\nPuedes volver a subir tus documentos desde /kyc/.\n\n"
        f"— {_BRAND}"
    )

    send_email_async.delay(
        subject=subject,
        message=body,
        recipient_list=[kyc_profile.user.email],
    )
    logger.info("[kyc_email] queued rejected to=%s", kyc_profile.user.email)

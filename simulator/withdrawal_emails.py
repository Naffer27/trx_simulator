"""
simulator/withdrawal_emails.py

Central helper for user-facing withdrawal status notifications.
One function, one place — keeps subject/body format consistent across
the request view, admin approve/reject actions, and the NP callback.

Caller is always responsible for wrapping with try/except so a queuing
failure never aborts the parent transaction.
"""
import logging
from django.utils import timezone

logger = logging.getLogger("simulator.withdrawal_emails")

_BRAND = "Money Broker"

EVENT_REQUESTED = "requested"
EVENT_APPROVED  = "approved"
EVENT_REJECTED  = "rejected"
EVENT_COMPLETED = "completed"
EVENT_FAILED    = "failed"


def _mask(addr: str) -> str:
    if not addr or len(addr) <= 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def send_withdrawal_status_email(wr, event: str) -> None:
    """
    Queue a withdrawal status notification to the withdrawal owner's email.

    wr    — WithdrawalRequest instance (must have .user, .amount_usd,
             .crypto_currency, .wallet_address, .id, .admin_note loaded)
    event — one of EVENT_* constants

    Returns silently if Celery is down (caller wraps with try/except).
    """
    from .tasks import send_email_async

    username = wr.user.username
    wr_id    = wr.id
    amount   = wr.amount_usd
    currency = wr.crypto_currency.upper()
    addr     = _mask(wr.wallet_address)
    now_str  = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    note     = getattr(wr, "admin_note", "") or ""

    if event == EVENT_REQUESTED:
        subject = f"Solicitud de retiro #{wr_id} recibida — {_BRAND}"
        body = (
            f"Hola {username},\n\n"
            f"Tu solicitud de retiro #{wr_id} fue recibida y está siendo revisada.\n\n"
            f"  Monto:        ${amount} USD\n"
            f"  Criptomoneda: {currency}\n"
            f"  Dirección:    {addr}\n"
            f"  Estado:       Pendiente\n"
            f"  Fecha:        {now_str}\n\n"
            f"Te notificaremos cuando sea procesada (24-48h).\n\n"
            f"— {_BRAND}"
        )

    elif event == EVENT_APPROVED:
        subject = f"Retiro #{wr_id} aprobado — en proceso — {_BRAND}"
        body = (
            f"Hola {username},\n\n"
            f"Tu retiro #{wr_id} fue aprobado y está siendo enviado.\n\n"
            f"  Monto:     ${amount} USD\n"
            f"  Moneda:    {currency}\n"
            f"  Dirección: {addr}\n"
            f"  Estado:    Aprobado\n"
            f"  Fecha:     {now_str}\n\n"
            f"El pago llegará en los próximos minutos según la red.\n\n"
            f"— {_BRAND}"
        )

    elif event == EVENT_REJECTED:
        note_line = f"  Motivo:    {note}\n" if note else ""
        subject = f"Retiro #{wr_id} rechazado — fondos devueltos — {_BRAND}"
        body = (
            f"Hola {username},\n\n"
            f"Tu solicitud de retiro #{wr_id} fue rechazada por el equipo.\n"
            f"${amount} USD fueron devueltos a tu wallet.\n\n"
            f"  Monto:     ${amount} USD\n"
            f"  Moneda:    {currency}\n"
            f"  Estado:    Rechazado\n"
            f"  Fecha:     {now_str}\n"
            f"{note_line}"
            f"\nContacta a soporte para más información.\n\n"
            f"— {_BRAND}"
        )

    elif event == EVENT_COMPLETED:
        subject = f"Retiro #{wr_id} completado — {_BRAND}"
        body = (
            f"Hola {username},\n\n"
            f"Tu retiro #{wr_id} fue enviado exitosamente.\n\n"
            f"  Monto:     ${amount} USD\n"
            f"  Moneda:    {currency}\n"
            f"  Dirección: {addr}\n"
            f"  Estado:    Completado\n"
            f"  Fecha:     {now_str}\n\n"
            f"— {_BRAND}"
        )

    elif event == EVENT_FAILED:
        subject = f"Retiro #{wr_id} fallido — fondos devueltos — {_BRAND}"
        body = (
            f"Hola {username},\n\n"
            f"El pago de tu retiro #{wr_id} no pudo completarse.\n"
            f"${amount} USD fueron devueltos a tu wallet automáticamente.\n\n"
            f"  Monto:     ${amount} USD\n"
            f"  Moneda:    {currency}\n"
            f"  Estado:    Fallido\n"
            f"  Fecha:     {now_str}\n\n"
            f"Contacta a soporte si tienes dudas.\n\n"
            f"— {_BRAND}"
        )

    else:
        logger.warning(
            "[withdrawal_email] unknown event=%r wr=%d — skipped", event, wr_id
        )
        return

    send_email_async.delay(
        subject=subject,
        message=body,
        recipient_list=[wr.user.email],
    )
    logger.info(
        "[withdrawal_email] queued event=%s wr=%d to=%s", event, wr_id, wr.user.email
    )

"""
simulator/deposit_emails.py

Notification email sent to the user when a deposit is confirmed/credited.
Caller always wraps with try/except so a queuing failure never aborts the
IPN callback.
"""
import logging
from django.utils import timezone

logger = logging.getLogger("simulator.deposit_emails")

_BRAND = "Money Broker"


def send_deposit_confirmed_email(deposit) -> None:
    """
    Queue a deposit-confirmed notification to the deposit owner.

    deposit — Deposit instance (must have .user, .amount_usd,
              .crypto_currency, .id accessible).

    Subject and body contain amount, currency, status, and date.
    No API keys, IPN secrets, or raw NowPayments payload are included.
    """
    from .tasks import send_email_async

    username = deposit.user.username
    amount   = deposit.amount_usd
    currency = deposit.crypto_currency.upper() if deposit.crypto_currency else "CRYPTO"
    now_str  = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"Money Broker — Depósito confirmado"
    body = (
        f"Hola {username},\n\n"
        f"Tu depósito fue confirmado y acreditado en tu wallet.\n\n"
        f"  Monto:    ${amount} USD\n"
        f"  Moneda:   {currency}\n"
        f"  Estado:   Confirmado\n"
        f"  Fecha:    {now_str}\n\n"
        f"Ya puedes usar los fondos en tu cuenta.\n\n"
        f"— {_BRAND}"
    )

    send_email_async.delay(
        subject=subject,
        message=body,
        recipient_list=[deposit.user.email],
    )
    logger.info(
        "[deposit_email] queued confirmed deposit_id=%d to=%s",
        deposit.id, deposit.user.email,
    )

"""
simulator/support_emails.py

Email notifications for support ticket lifecycle events.
Callers always wrap with try/except so queuing failures never abort
ticket creation.
"""
import logging
from django.utils import timezone

logger = logging.getLogger("simulator.support_emails")

_BRAND = "Money Brokers"


def send_support_ticket_created_email(ticket) -> None:
    """
    Queue a confirmation to the user who opened the ticket.

    Includes: ticket id, category, subject, status, and UTC date.
    Does not include the full message body (avoids exposing any
    sensitive content the user may have typed).
    """
    from .tasks import send_email_async

    username  = ticket.user.username
    ticket_id = ticket.id
    category  = ticket.get_category_display()
    subject   = ticket.subject
    now_str   = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    email_subject = f"Money Brokers — Ticket recibido"
    body = (
        f"Hola {username},\n\n"
        f"Recibimos tu solicitud de soporte. El equipo la revisará pronto.\n\n"
        f"  Ticket #:   {ticket_id}\n"
        f"  Categoría:  {category}\n"
        f"  Asunto:     {subject}\n"
        f"  Estado:     Abierto\n"
        f"  Fecha:      {now_str}\n\n"
        f"Te notificaremos sobre el progreso de tu ticket.\n\n"
        f"— {_BRAND}"
    )

    send_email_async.delay(
        subject=email_subject,
        message=body,
        recipient_list=[ticket.user.email],
    )
    logger.info(
        "[support_email] queued ticket_created ticket=%d to=%s",
        ticket_id, ticket.user.email,
    )


def send_support_ticket_admin_email(ticket) -> None:
    """
    Queue a new-ticket alert to the SUPPORT_EMAIL address.

    Includes: user, email, category, subject, priority, and UTC date.
    Skipped silently when SUPPORT_EMAIL is not configured.
    Does not include the full message body to avoid accidental exposure
    in notification pipelines.
    """
    from django.conf import settings
    from .tasks import send_email_async

    support_email = getattr(settings, "SUPPORT_EMAIL", "").strip()
    if not support_email:
        logger.debug("[support_email] SUPPORT_EMAIL not set — admin alert skipped")
        return

    ticket_id = ticket.id
    username  = ticket.user.username
    user_email = ticket.user.email
    category  = ticket.get_category_display()
    subject   = ticket.subject
    priority  = ticket.get_priority_display()
    now_str   = timezone.now().strftime("%Y-%m-%d %H:%M UTC")

    email_subject = f"Money Brokers — Nuevo ticket de soporte #{ticket_id}"
    body = (
        f"Nuevo ticket de soporte abierto.\n\n"
        f"  Ticket #:   {ticket_id}\n"
        f"  Usuario:    {username}\n"
        f"  Email:      {user_email}\n"
        f"  Categoría:  {category}\n"
        f"  Asunto:     {subject}\n"
        f"  Prioridad:  {priority}\n"
        f"  Fecha:      {now_str}\n\n"
        f"— {_BRAND} (notificación automática)"
    )

    send_email_async.delay(
        subject=email_subject,
        message=body,
        recipient_list=[support_email],
    )
    logger.info(
        "[support_email] queued admin alert ticket=%d to=%s",
        ticket_id, support_email,
    )

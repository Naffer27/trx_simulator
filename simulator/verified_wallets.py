# simulator/verified_wallets.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — VerifiedWithdrawalWallet lifecycle.

A row is created ONLY after its own WithdrawalEmailOTPChallenge
(purpose=ADDRESS_CHANGE) has been verified (TOTP + email OTP + local
address/network validation already passed by the caller) — it is born
PENDING_COOLDOWN, never "unverified".

Activation (PENDING_COOLDOWN -> ACTIVE once cooldown_until has passed,
deactivating any prior ACTIVE row for the same user+asset+network) is
lazy-on-read (get_active_wallet(), called from the withdrawal address
lookup) PLUS a periodic Celery sweep (sweep_activate_due_wallets(), wired
into tasks.py) as a defensive backup — same dual pattern this codebase
already uses for PendingOrder expiry (consumers.py lazy check + tasks.py
daemon).

_try_activate() is the single choke point for the PENDING_COOLDOWN->ACTIVE
transition — it always locks the pending row first, then (if present) the
prior ACTIVE row for the same route, inside one atomic block. Since this is
the ONLY function that ever performs this transition, that fixed lock order
cannot deadlock against itself.
"""
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger("simulator.verified_wallets")


class AddressChangeAlreadyPending(Exception):
    """User already has a PENDING_COOLDOWN row for this exact (asset, network)."""


def create_pending_wallet(user, *, asset: str, network: str, address: str, challenge=None):
    """
    Register a new withdrawal address for (user, asset, network), entering
    cooldown immediately. Caller is responsible for having already verified
    TOTP + email OTP + local address format/checksum before calling this.

    Raises AddressChangeAlreadyPending if a PENDING_COOLDOWN row already
    exists for this exact route (DB constraint vww_one_pending_per_route).
    """
    from .models import VerifiedWithdrawalWallet

    now = timezone.now()
    cooldown_until = now + timezone.timedelta(hours=settings.WALLET_ADDRESS_CHANGE_COOLDOWN_HOURS)
    try:
        wallet = VerifiedWithdrawalWallet.objects.create(
            user=user,
            asset=asset,
            network=network,
            address=address,
            status=VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN,
            verified_at=now,
            cooldown_until=cooldown_until,
            created_by_challenge=challenge,
        )
    except IntegrityError as exc:
        raise AddressChangeAlreadyPending(
            f"User #{user.pk} already has a pending {asset}/{network} address change."
        ) from exc

    logger.info(
        "[verified_wallets] created pending wallet_id=%d user=%s %s/%s cooldown_until=%s",
        wallet.id, user.username, asset, network, cooldown_until,
    )
    return wallet


def _try_activate(pending_wallet_id: int) -> bool:
    """
    Attempt to activate one PENDING_COOLDOWN row whose cooldown has
    elapsed, deactivating any prior ACTIVE row for the same route.
    Returns True if it activated, False if there was nothing to do
    (already handled, not due yet, or not found).
    """
    from .models import VerifiedWithdrawalWallet

    with transaction.atomic():
        try:
            pending = VerifiedWithdrawalWallet.objects.select_for_update().get(pk=pending_wallet_id)
        except VerifiedWithdrawalWallet.DoesNotExist:
            return False

        if pending.status != VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN:
            return False
        if pending.cooldown_until is None or pending.cooldown_until > timezone.now():
            return False

        old_active = (
            VerifiedWithdrawalWallet.objects
            .select_for_update()
            .filter(
                user_id=pending.user_id, asset=pending.asset, network=pending.network,
                status=VerifiedWithdrawalWallet.STATUS_ACTIVE,
            )
            .first()
        )

        now = timezone.now()
        if old_active is not None:
            VerifiedWithdrawalWallet.objects.filter(pk=old_active.pk).update(
                status=VerifiedWithdrawalWallet.STATUS_DEACTIVATED, deactivated_at=now,
            )
        VerifiedWithdrawalWallet.objects.filter(pk=pending.pk).update(
            status=VerifiedWithdrawalWallet.STATUS_ACTIVE, activated_at=now,
        )

    logger.info(
        "[verified_wallets] activated wallet_id=%d (deactivated old_id=%s)",
        pending_wallet_id, old_active.pk if old_active else None,
    )
    return True


def get_active_wallet(user, *, asset: str, network: str):
    """
    Return the ACTIVE VerifiedWithdrawalWallet for (user, asset, network),
    or None. Always checks for a due PENDING_COOLDOWN row FIRST and
    activates it if found — even when an ACTIVE row already exists for
    this route (that's exactly the "changing wallet" case: the new row
    becoming due must replace the old ACTIVE one here, not just on a
    first-ever registration where no ACTIVE row exists yet). See module
    docstring for the lazy+sweep dual pattern.
    """
    from .models import VerifiedWithdrawalWallet

    pending = VerifiedWithdrawalWallet.objects.filter(
        user=user, asset=asset, network=network,
        status=VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN,
    ).first()
    if pending is not None and pending.cooldown_until and pending.cooldown_until <= timezone.now():
        _try_activate(pending.pk)

    return VerifiedWithdrawalWallet.objects.filter(
        user=user, asset=asset, network=network, status=VerifiedWithdrawalWallet.STATUS_ACTIVE,
    ).first()


def sweep_activate_due_wallets(*, batch_size: int = 200) -> int:
    """
    Periodic sweep: activate every PENDING_COOLDOWN row whose cooldown_until
    has already elapsed. Defensive backup to get_active_wallet()'s lazy
    activation — same role tasks.py's PendingOrder expiry daemon plays
    relative to consumers.py's lazy expiry check. Returns count activated.
    """
    from .models import VerifiedWithdrawalWallet

    due_ids = list(
        VerifiedWithdrawalWallet.objects
        .filter(
            status=VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN,
            cooldown_until__lte=timezone.now(),
        )
        .values_list("pk", flat=True)[:batch_size]
    )
    activated = 0
    for pk in due_ids:
        if _try_activate(pk):
            activated += 1
    return activated

# simulator/wallet_ledger.py
"""
Single entry point for all Wallet balance mutations.

RULES:
  1. Wallet.available_balance is NEVER modified directly anywhere in the codebase.
  2. Every balance change goes through credit_wallet() or debit_wallet().
  3. Both functions run inside transaction.atomic() with select_for_update().
  4. Every call appends a WalletTransaction row — the ledger IS the audit trail.

Reconciliation invariant (verified by reconcile_wallet()):
  SUM(WalletTransaction.amount WHERE wallet_id=W) == Wallet.available_balance

Callers that already hold an outer atomic() block are safe — Django's atomic()
is re-entrant (savepoint semantics). The inner atomic() here adds a savepoint,
not a new transaction, so the caller's ROLLBACK still covers everything.
"""

from decimal import Decimal
from django.db import transaction
from django.utils.timezone import now


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class InsufficientFunds(Exception):
    """Raised by debit_wallet when available_balance < requested amount."""


class WalletIntegrityError(Exception):
    """Raised by reconcile_wallet when stored balance != ledger sum."""


# ─────────────────────────────────────────────
# Core primitives
# ─────────────────────────────────────────────

def credit_wallet(
    wallet_id: int,
    amount,
    tx_type: str,
    *,
    deposit=None,
    internal_transfer=None,
    note: str = "",
    initiated_by=None,
):
    """
    Atomically add *amount* to Wallet.available_balance and append a ledger entry.

    Returns the created WalletTransaction instance.
    Raises ValueError if amount <= 0.

    Safe to call from inside an outer transaction.atomic() block.
    """
    from .models import Wallet, WalletTransaction

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"credit_wallet: amount must be > 0, got {amount}")

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=wallet_id)
        new_balance = wallet.available_balance + amount
        # Use filter().update() — single SQL UPDATE, no ORM overhead, no signal firing
        Wallet.objects.filter(pk=wallet_id).update(
            available_balance=new_balance,
            updated_at=now(),
        )
        return WalletTransaction.objects.create(
            wallet_id=wallet_id,
            tx_type=tx_type,
            amount=amount,            # positive = credit
            balance_after=new_balance,
            deposit=deposit,
            internal_transfer=internal_transfer,
            note=note or None,
            initiated_by=initiated_by,
        )


def debit_wallet(
    wallet_id: int,
    amount,
    tx_type: str,
    *,
    internal_transfer=None,
    note: str = "",
    initiated_by=None,
):
    """
    Atomically subtract *amount* from Wallet.available_balance and append a ledger entry.

    Returns the created WalletTransaction instance.
    Raises InsufficientFunds if available_balance < amount.
    Raises ValueError if amount <= 0.

    Safe to call from inside an outer transaction.atomic() block.
    """
    from .models import Wallet, WalletTransaction

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"debit_wallet: amount must be > 0, got {amount}")

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=wallet_id)
        if wallet.available_balance < amount:
            raise InsufficientFunds(
                f"Wallet #{wallet_id}: available={wallet.available_balance}, requested={amount}"
            )
        new_balance = wallet.available_balance - amount
        Wallet.objects.filter(pk=wallet_id).update(
            available_balance=new_balance,
            updated_at=now(),
        )
        return WalletTransaction.objects.create(
            wallet_id=wallet_id,
            tx_type=tx_type,
            amount=-amount,           # negative = debit
            balance_after=new_balance,
            internal_transfer=internal_transfer,
            note=note or None,
            initiated_by=initiated_by,
        )


# ─────────────────────────────────────────────
# Transfer engine (wallet ↔ trading account)
# ─────────────────────────────────────────────

def transfer_to_account(wallet_id: int, trading_account_id: int, amount, *, note: str = "", initiated_by=None):
    """
    Move *amount* from Wallet → TradingAccount.

    Atomically:
      1. Mark InternalTransfer PROCESSING
      2. Debit wallet (WalletTransaction TX_TRANSFER_OUT)
      3. Credit trading account balance (LedgerEntry DEPOSIT)
      4. Mark InternalTransfer COMPLETED

    Raises InsufficientFunds if wallet balance is too low.
    On any exception the entire transaction rolls back and InternalTransfer → FAILED.
    """
    from .models import (
        Wallet, TradingAccount, InternalTransfer, LedgerEntry, WalletTransaction
    )

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"transfer_to_account: amount must be > 0, got {amount}")

    # Create the transfer record BEFORE the atomic block so we can set FAILED on error
    xfer = InternalTransfer.objects.create(
        wallet_id=wallet_id,
        trading_account_id=trading_account_id,
        direction=InternalTransfer.DIR_TO_ACCOUNT,
        amount=amount,
        status=InternalTransfer.ST_PENDING,
        note=note or None,
        initiated_by=initiated_by,
    )

    try:
        with transaction.atomic():
            # Lock both rows for the duration of this atomic block
            xfer_locked = InternalTransfer.objects.select_for_update().get(pk=xfer.pk)
            xfer_locked.status = InternalTransfer.ST_PROCESSING
            xfer_locked.save(update_fields=["status", "updated_at"])

            account = TradingAccount.objects.select_for_update().get(pk=trading_account_id)

            # 1. Debit wallet (also does select_for_update internally)
            wallet_tx = debit_wallet(
                wallet_id, amount,
                WalletTransaction.TX_TRANSFER_OUT,
                internal_transfer=xfer_locked,
                note=note,
                initiated_by=initiated_by,
            )

            # 2. Credit trading account
            new_acc_balance = account.balance + amount
            TradingAccount.objects.filter(pk=trading_account_id).update(
                balance=new_acc_balance,
                equity=new_acc_balance,      # equity = balance when no open positions
                peak_balance=new_acc_balance if account.peak_balance < new_acc_balance else account.peak_balance,
            )
            LedgerEntry.objects.create(
                account_id=trading_account_id,
                event_type=LedgerEntry.EV_DEPOSIT,
                amount=amount,
                balance_after=new_acc_balance,
                meta={
                    "source": "wallet_transfer",
                    "wallet_id": wallet_id,
                    "internal_transfer_id": xfer.pk,
                },
            )

            # 3. Complete transfer
            InternalTransfer.objects.filter(pk=xfer.pk).update(
                status=InternalTransfer.ST_COMPLETED,
                completed_at=now(),
                updated_at=now(),
            )

    except Exception as exc:
        InternalTransfer.objects.filter(pk=xfer.pk).update(
            status=InternalTransfer.ST_FAILED,
            failure_reason=str(exc)[:256],
            updated_at=now(),
        )
        raise

    xfer.refresh_from_db()
    return xfer


def transfer_to_wallet(wallet_id: int, trading_account_id: int, amount, *, note: str = "", initiated_by=None):
    """
    Move *amount* from TradingAccount → Wallet.

    Atomically:
      1. Mark InternalTransfer PROCESSING
      2. Debit trading account balance (LedgerEntry WITHDRAW)
      3. Credit wallet (WalletTransaction TX_TRANSFER_IN)
      4. Mark InternalTransfer COMPLETED

    Raises ValueError if account balance < amount or account has open positions
    that would make the withdrawal reduce equity below margin requirements.
    """
    from .models import (
        Wallet, TradingAccount, InternalTransfer, LedgerEntry, WalletTransaction, Position
    )

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError(f"transfer_to_wallet: amount must be > 0, got {amount}")

    xfer = InternalTransfer.objects.create(
        wallet_id=wallet_id,
        trading_account_id=trading_account_id,
        direction=InternalTransfer.DIR_TO_WALLET,
        amount=amount,
        status=InternalTransfer.ST_PENDING,
        note=note or None,
        initiated_by=initiated_by,
    )

    try:
        with transaction.atomic():
            xfer_locked = InternalTransfer.objects.select_for_update().get(pk=xfer.pk)
            xfer_locked.status = InternalTransfer.ST_PROCESSING
            xfer_locked.save(update_fields=["status", "updated_at"])

            account = TradingAccount.objects.select_for_update().get(pk=trading_account_id)
            open_positions = Position.objects.filter(account=account).count()

            if open_positions > 0:
                raise ValueError(
                    f"Account #{trading_account_id} has {open_positions} open position(s). "
                    "Close all positions before withdrawing to wallet."
                )
            if account.balance < amount:
                raise InsufficientFunds(
                    f"Account #{trading_account_id}: balance={account.balance}, requested={amount}"
                )

            # 1. Debit trading account
            new_acc_balance = account.balance - amount
            TradingAccount.objects.filter(pk=trading_account_id).update(
                balance=new_acc_balance,
                equity=new_acc_balance,
            )
            LedgerEntry.objects.create(
                account_id=trading_account_id,
                event_type=LedgerEntry.EV_WITHDRAW,
                amount=-amount,
                balance_after=new_acc_balance,
                meta={
                    "destination": "wallet_transfer",
                    "wallet_id": wallet_id,
                    "internal_transfer_id": xfer.pk,
                },
            )

            # 2. Credit wallet
            credit_wallet(
                wallet_id, amount,
                WalletTransaction.TX_TRANSFER_IN,
                internal_transfer=xfer_locked,
                note=note,
                initiated_by=initiated_by,
            )

            # 3. Complete transfer
            InternalTransfer.objects.filter(pk=xfer.pk).update(
                status=InternalTransfer.ST_COMPLETED,
                completed_at=now(),
                updated_at=now(),
            )

    except Exception as exc:
        InternalTransfer.objects.filter(pk=xfer.pk).update(
            status=InternalTransfer.ST_FAILED,
            failure_reason=str(exc)[:256],
            updated_at=now(),
        )
        raise

    xfer.refresh_from_db()
    return xfer


# ─────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────

def reconcile_wallet(wallet_id: int) -> dict:
    """
    Verify that Wallet.available_balance equals the sum of all WalletTransaction.amount.

    Returns a dict:
      { wallet_id, stored, computed, drift, ok }

    drift != 0 means someone modified available_balance without going through the ledger.
    """
    from django.db.models import Sum
    from .models import Wallet, WalletTransaction

    wallet = Wallet.objects.get(pk=wallet_id)
    computed = (
        WalletTransaction.objects
        .filter(wallet_id=wallet_id)
        .aggregate(total=Sum("amount"))["total"]
    ) or Decimal("0")

    drift = computed - wallet.available_balance
    return {
        "wallet_id": wallet_id,
        "stored":    wallet.available_balance,
        "computed":  computed,
        "drift":     drift,
        "ok":        drift == Decimal("0"),
    }


# ─────────────────────────────────────────────
# Convenience: get-or-create wallet for a user
# ─────────────────────────────────────────────

def get_or_create_wallet(user):
    """Return (wallet, created). Thread-safe via get_or_create."""
    from .models import Wallet
    wallet, created = Wallet.objects.get_or_create(user=user)
    return wallet, created

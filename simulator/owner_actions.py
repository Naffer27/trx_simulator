"""
simulator/owner_actions.py

MONEY-INTEGRITY-FIX-02 — Owner Root's dedicated, auditable financial and
role-management actions. These are the ONLY sanctioned entry points for:

  - owner_trading_account_adjustment() — correcting TradingAccount.balance
    directly (closes Finding #2 of MONEY-INTEGRITY-AND-FRAUD-SURFACE-AUDIT-01
    — balance/equity/initial_balance are readonly in TradingAccountAdmin now).
  - owner_wallet_adjustment() — an extraordinary Wallet credit/debit that
    Owner Root can perform alone, structurally SEPARATE from
    TreasuryOperationRequest (which keeps its existing 3-distinct-actor
    segregation of duties unchanged for all Ops/staff use — never touched
    by this module).
  - replace_ops_admin() — the only way OpsAdminProfile's single row can
    ever change hands; Owner Root only.
  - owner_broker_economic_adjustment() — BROKER-ECONOMICS-02C. The only
    sanctioned way an Owner-facing surface may originate a
    BrokerEconomicAdjustment / BrokerLedger.REV_ADJUSTMENT correction.
    Calls simulator.broker_economic_adjustment.create_broker_economic_
    adjustment() (BROKER-ECONOMICS-02B, unmodified) exclusively — never
    writes either model itself. For a reversal, amount is ALWAYS
    server-derived from the target being reversed; a caller-supplied
    reversal amount is never read.

Every function here:
  - Verifies is_owner_root(actor) FIRST, before anything else.
  - Re-verifies TOTP (simulator.two_factor.verify_totp) — a fresh
    reauthentication, not reuse of session state.
  - Is idempotent via a persisted, DB-unique idempotency_key.
  - Writes AuditLog + BrokerAuditEvent exactly once per completed call.
  - Runs entirely inside transaction.atomic() — a denied/failed attempt
    leaves zero partial writes.
"""
import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from .permission_levels import is_owner_root
from .two_factor import verify_totp

logger = logging.getLogger("simulator.owner_actions")


class InvalidAdjustment(Exception):
    """Raised for a structurally invalid request (amount==0, missing reason, wrong TOTP)."""


class OpenPositionsExist(Exception):
    """Raised when a TradingAccount adjustment is attempted while positions are open."""


def _new_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def owner_trading_account_adjustment(
    trading_account_id: int, amount, *, actor, reason: str, totp_code: str,
    idempotency_key: str,
):
    """
    Correct a TradingAccount's .balance by a signed amount. Owner Root only.

    Requires open_positions == 0 (Design Lock Correction #3 / final Design
    Lock decision — eliminates any ambiguity about recalculating floating
    equity mid-adjustment: with zero open positions, equity_after ==
    balance_after always, by definition, since equity = balance + floating
    PnL and floating PnL is 0 with no open positions).

    Returns the created ManualBalanceAdjustment on success, or the
    PRE-EXISTING one if idempotency_key was already used (no-op, no
    second write of any kind).

    Raises PermissionDenied, InvalidAdjustment, OpenPositionsExist,
    simulator.wallet_ledger.InsufficientFunds-equivalent (ValueError) if
    the resulting balance would be negative.
    """
    from .models import LedgerEntry, ManualBalanceAdjustment, Position, TradingAccount
    from .audit import EV_OWNER_TRADING_ADJUSTMENT, log_audit
    from . import broker_audit as _audit

    if not is_owner_root(actor):
        raise PermissionDenied("Only Owner Root can perform a manual TradingAccount adjustment.")

    amount = Decimal(str(amount))
    if amount == 0:
        raise InvalidAdjustment("amount must not be zero.")
    if not reason or not reason.strip():
        raise InvalidAdjustment("reason is required.")
    if not verify_totp(actor, totp_code):
        raise InvalidAdjustment("Invalid TOTP code.")

    with transaction.atomic():
        existing = ManualBalanceAdjustment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

        account = TradingAccount.objects.select_for_update().get(pk=trading_account_id)

        open_positions = Position.objects.filter(account=account).count()
        if open_positions > 0:
            raise OpenPositionsExist(
                f"Account #{trading_account_id} has {open_positions} open position(s). "
                "Manual adjustment requires zero open positions."
            )

        balance_before = account.balance
        equity_before = account.equity
        balance_after = balance_before + amount
        if balance_after < 0:
            raise InvalidAdjustment(
                f"Resulting balance would be negative: {balance_before} + {amount} = {balance_after}."
            )
        equity_after = balance_after  # open_positions == 0, so equity == balance exactly

        TradingAccount.objects.filter(pk=account.pk).update(
            balance=balance_after, equity=equity_after,
        )
        LedgerEntry.objects.create(
            account_id=account.pk,
            event_type=LedgerEntry.EV_OWNER_CORRECTION,
            amount=amount,
            balance_after=balance_after,
            meta={
                "actor_id": actor.pk,
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
        )

        reference = _new_reference("OWN-TA")
        adjustment = ManualBalanceAdjustment.objects.create(
            trading_account=account,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
            actor=actor,
            balance_before=balance_before,
            balance_after=balance_after,
            equity_before=equity_before,
            equity_after=equity_after,
            reference=reference,
        )

        detail = {
            "reference": reference,
            "trading_account_id": account.pk,
            "actor_id": actor.pk,
            "amount": str(amount),
            "reason": reason,
            "balance_before": str(balance_before),
            "balance_after": str(balance_after),
            "equity_before": str(equity_before),
            "equity_after": str(equity_after),
        }
        log_audit(
            None, EV_OWNER_TRADING_ADJUSTMENT,
            f"Owner manual adjustment on TradingAccount #{account.pk}: {amount:+} "
            f"({balance_before} -> {balance_after}) — ref {reference}",
            detail=detail,
        )
        _audit.record_admin_event(
            event_type=EV_OWNER_TRADING_ADJUSTMENT,
            severity=_audit.Severity.WARNING,
            description=f"Owner manual TradingAccount adjustment — ref {reference}",
            actor_id=actor.pk,
            account_id=account.pk,
            source_module="simulator.owner_actions",
            metadata=detail,
        )

    return adjustment


def owner_wallet_adjustment(
    wallet_id: int, amount, *, actor, reason: str, totp_code: str, idempotency_key: str,
):
    """
    Extraordinary Wallet credit/debit performed by Owner Root alone —
    structurally separate from TreasuryOperationRequest (untouched by this
    function). Reuses wallet_ledger.credit_wallet()/debit_wallet()
    unmodified.

    Returns the created OwnerWalletAdjustment on success, or the
    PRE-EXISTING one if idempotency_key was already used (no-op — never a
    second credit/debit, never a second WalletTransaction).
    """
    from .models import OwnerWalletAdjustment, Wallet, WalletTransaction
    from .wallet_ledger import credit_wallet, debit_wallet, InsufficientFunds
    from .audit import EV_OWNER_WALLET_ADJUSTMENT, log_audit
    from . import broker_audit as _audit

    if not is_owner_root(actor):
        raise PermissionDenied("Only Owner Root can perform an Owner Wallet Adjustment.")

    amount = Decimal(str(amount))
    if amount == 0:
        raise InvalidAdjustment("amount must not be zero.")
    if not reason or not reason.strip():
        raise InvalidAdjustment("reason is required.")
    if not verify_totp(actor, totp_code):
        raise InvalidAdjustment("Invalid TOTP code.")

    with transaction.atomic():
        existing = OwnerWalletAdjustment.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

        wallet = Wallet.objects.select_for_update().get(pk=wallet_id)
        balance_before = wallet.available_balance

        note = f"Owner wallet adjustment — {reason}"
        if amount > 0:
            tx = credit_wallet(
                wallet.id, amount, WalletTransaction.TX_CORRECTION,
                note=note, initiated_by=actor,
            )
        else:
            try:
                tx = debit_wallet(
                    wallet.id, -amount, WalletTransaction.TX_CORRECTION,
                    note=note, initiated_by=actor,
                )
            except InsufficientFunds:
                raise InvalidAdjustment(
                    f"Wallet #{wallet_id}: insufficient funds for debit of {-amount}."
                )

        wallet.refresh_from_db()
        balance_after = wallet.available_balance

        reference = _new_reference("OWN-WA")
        adjustment = OwnerWalletAdjustment.objects.create(
            wallet=wallet,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
            actor=actor,
            balance_before=balance_before,
            balance_after=balance_after,
            wallet_transaction=tx,
            reference=reference,
        )

        detail = {
            "reference": reference,
            "wallet_id": wallet.pk,
            "actor_id": actor.pk,
            "amount": str(amount),
            "reason": reason,
            "balance_before": str(balance_before),
            "balance_after": str(balance_after),
            "wallet_transaction_id": tx.pk,
        }
        log_audit(
            None, EV_OWNER_WALLET_ADJUSTMENT,
            f"Owner wallet adjustment on Wallet #{wallet.pk}: {amount:+} "
            f"({balance_before} -> {balance_after}) — ref {reference}",
            detail=detail,
        )
        _audit.record_admin_event(
            event_type=EV_OWNER_WALLET_ADJUSTMENT,
            severity=_audit.Severity.WARNING,
            description=f"Owner Wallet Adjustment — ref {reference}",
            actor_id=actor.pk,
            source_module="simulator.owner_actions",
            metadata=detail,
        )

    return adjustment


def replace_ops_admin(new_user, *, actor, reason: str = ""):
    """
    The only way OpsAdminProfile's single row can change hands. Owner Root
    only. Safe against the initial-assignment race (table empty, two
    concurrent callers both see no existing row) — see Design Lock
    Correction #3: the nested atomic()/savepoint around the CREATE path
    lets a genuine IntegrityError (lost the race) be caught without
    poisoning the outer transaction, and the losing call re-reads under
    lock and applies its own intent as a normal replace on top of
    whatever just landed. Deterministic either way: exactly one row,
    audited exactly once per logical call.
    """
    from .models import OpsAdminProfile
    from .audit import EV_OPS_ADMIN_REPLACED, log_audit
    from . import broker_audit as _audit

    if not is_owner_root(actor):
        raise PermissionDenied("Only Owner Root can replace the OPS_ADMIN.")

    with transaction.atomic():
        existing = OpsAdminProfile.objects.select_for_update().filter(singleton_enforcer=True).first()

        if existing is not None:
            old_user_id = existing.user_id
            OpsAdminProfile.objects.filter(pk=existing.pk).update(
                user=new_user, assigned_by=actor, assigned_at=timezone.now(), reason=reason,
            )
        else:
            old_user_id = None
            try:
                with transaction.atomic():
                    OpsAdminProfile.objects.create(
                        user=new_user, singleton_enforcer=True,
                        assigned_by=actor, reason=reason,
                    )
            except IntegrityError:
                # Another concurrent call won the initial-creation race.
                # Re-read under lock now that the row exists, and apply
                # THIS call's intent as a normal replace.
                existing = OpsAdminProfile.objects.select_for_update().get(singleton_enforcer=True)
                old_user_id = existing.user_id
                OpsAdminProfile.objects.filter(pk=existing.pk).update(
                    user=new_user, assigned_by=actor, assigned_at=timezone.now(), reason=reason,
                )

        detail = {
            "old_user_id": old_user_id,
            "new_user_id": new_user.pk,
            "actor_id": actor.pk,
            "reason": reason,
        }
        log_audit(
            None, EV_OPS_ADMIN_REPLACED,
            f"OPS_ADMIN replaced: user #{old_user_id} -> user #{new_user.pk} by actor #{actor.pk}",
            detail=detail,
        )
        _audit.record_admin_event(
            event_type=EV_OPS_ADMIN_REPLACED,
            severity=_audit.Severity.WARNING,
            description=f"OPS_ADMIN replaced: #{old_user_id} -> #{new_user.pk}",
            actor_id=actor.pk,
            source_module="simulator.owner_actions",
            metadata=detail,
        )

    return OpsAdminProfile.objects.get(singleton_enforcer=True)


def owner_broker_economic_adjustment(
    *, reason: str, actor, totp_code: str, idempotency_key: str,
    amount=None, source_ledger_id=None, source_account_id=None,
    symbol: str | None = None, reverses_id=None,
):
    """
    BROKER-ECONOMICS-02C — Owner Root's dedicated entry point for an
    extraordinary broker economic correction. The ONLY function a
    user-facing surface may call to originate a BrokerEconomicAdjustment
    / BrokerLedger.REV_ADJUSTMENT row — it never writes either model
    itself, only through simulator.broker_economic_adjustment.
    create_broker_economic_adjustment() (BROKER-ECONOMICS-02B,
    unmodified, untouched by this function).

    For a REVERSAL (reverses_id given), `amount` is ALWAYS derived here
    as -reverses.amount — any caller-supplied `amount` argument is
    completely ignored in that case, never read, never trusted from a
    browser. BROKER-ECONOMICS-02B's own service independently
    re-validates this exact invariant again as the last, authoritative,
    DB/service-backed barrier (REVERSAL-AMOUNT-INTEGRITY-01) — this
    function's server-side derivation is a second, earlier line of
    defense, not a replacement for it.

    idempotency_key must be generated exactly once by the caller (at
    preview time, per BROKER-ECONOMICS-02C's closed design decision) and
    supplied unchanged on every call representing the same logical
    operation, including retries. This function never generates one
    itself. The real, atomic, DB-constraint-backed duplicate-prevention
    guarantee remains entirely inside create_broker_economic_adjustment()
    — this function adds no second correctness mechanism, per design.

    Returns the created BrokerEconomicAdjustment, or the PRE-EXISTING one
    if idempotency_key was already used. Writes AuditLog +
    BrokerAuditEvent exactly once — only when this call is the one that
    actually created the row, never on an idempotent-retry return
    (matching owner_wallet_adjustment()'s own established behavior). The
    pre-check used to decide this is purely informational (worst case: a
    genuinely concurrent duplicate might log audit twice) — it is NOT
    the economic-correctness mechanism, which is 02B's alone.
    """
    if not is_owner_root(actor):
        raise PermissionDenied("Only Owner Root can perform a Broker Economic Adjustment.")
    if not reason or not reason.strip():
        raise InvalidAdjustment("reason is required.")
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidAdjustment("idempotency_key is required.")

    from .models import BrokerEconomicAdjustment, BrokerLedger, TradingAccount
    from .broker_economic_adjustment import (
        InvalidEconomicAdjustment, create_broker_economic_adjustment,
    )

    reverses = None
    if reverses_id is not None:
        reverses = BrokerEconomicAdjustment.objects.filter(pk=reverses_id).first()
        if reverses is None:
            raise InvalidAdjustment(f"reverses target #{reverses_id} does not exist.")
        # SERVER-DERIVED — see docstring. The browser's `amount`, if any
        # was even submitted for this request, is never read past this
        # point for a reversal.
        amount = -reverses.amount
    else:
        if amount is None or (isinstance(amount, str) and not amount.strip()):
            raise InvalidAdjustment("amount is required for a non-reversal adjustment.")
        try:
            amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            raise InvalidAdjustment("amount must be a valid decimal value.")
        if amount == 0:
            raise InvalidAdjustment("amount must not be zero.")

    source_ledger = None
    if source_ledger_id is not None:
        source_ledger = BrokerLedger.objects.filter(pk=source_ledger_id).first()
        if source_ledger is None:
            raise InvalidAdjustment(f"source_ledger #{source_ledger_id} does not exist.")

    source_account = None
    if source_account_id is not None:
        source_account = TradingAccount.objects.filter(pk=source_account_id).first()
        if source_account is None:
            raise InvalidAdjustment(f"source_account #{source_account_id} does not exist.")

    if not verify_totp(actor, totp_code):
        raise InvalidAdjustment("Invalid TOTP code.")

    # Audit-decision pre-check ONLY (see docstring) — never the
    # correctness mechanism.
    was_already_recorded = BrokerEconomicAdjustment.objects.filter(
        idempotency_key=idempotency_key,
    ).exists()

    try:
        adjustment = create_broker_economic_adjustment(
            amount=amount, reason=reason, actor=actor,
            idempotency_key=idempotency_key, source_ledger=source_ledger,
            source_account=source_account, symbol=symbol, reverses=reverses,
        )
    except InvalidEconomicAdjustment as exc:
        raise InvalidAdjustment(str(exc)) from exc

    if not was_already_recorded:
        from .audit import EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT, log_audit
        from . import broker_audit as _audit

        detail = {
            "reference": adjustment.reference,
            "actor_id": actor.pk,
            "amount": str(adjustment.amount),
            "reason": reason,
            "source_ledger_id": source_ledger.pk if source_ledger else None,
            "source_account_id": source_account.pk if source_account else None,
            "reverses_id": reverses.pk if reverses else None,
            "created_ledger_entry_id": adjustment.created_ledger_entry_id,
        }
        log_audit(
            None, EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT,
            f"Owner broker economic adjustment {adjustment.reference}: "
            f"{adjustment.amount:+}"
            + (f" (reverses {reverses.reference})" if reverses else ""),
            detail=detail,
        )
        _audit.record_admin_event(
            event_type=EV_OWNER_BROKER_ECONOMIC_ADJUSTMENT,
            severity=_audit.Severity.WARNING,
            description=f"Owner Broker Economic Adjustment — ref {adjustment.reference}",
            actor_id=actor.pk,
            source_module="simulator.owner_actions",
            metadata=detail,
        )

    return adjustment

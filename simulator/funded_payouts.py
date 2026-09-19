"""
simulator/funded_payouts.py

Financial service layer for funded account payouts.
Imported by admin.py (approval actions) and views.py (webhook handler).
No dependency on admin.py or views.py.

H.2: FUNDED_SIM      — atomic approval + wallet credit + immediate cycle reset.
H.3: FUNDED_INTERNAL — bifásic approval (DB debit then NP call) + webhook handler.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils.timezone import now

from . import nowpayments as _np
from .models import (
    FundedConfig,
    FundedPayoutRequest,
    LedgerEntry,
    TradingAccount,
    WithdrawalRequest,
    WalletTransaction,
)
from .wallet_ledger import credit_wallet, get_or_create_wallet

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class FundedPayoutAlreadyProcessed(Exception):
    """Raised when trying to approve a non-pending FundedPayoutRequest."""


class InsufficientFundedBalance(Exception):
    """Raised when funded account balance < trader_cut at approval time."""


# ─────────────────────────────────────────────────────────────────────────────
# H.2 — FUNDED_SIM approval
# ─────────────────────────────────────────────────────────────────────────────

def approve_sim_payout(fpr: FundedPayoutRequest, admin_user) -> None:
    """
    Atomically approve a FUNDED_SIM FundedPayoutRequest.

    Steps (all inside transaction.atomic()):
      1. Lock FPR — validate status == pending and funded_type == FUNDED_SIM.
      2. Lock funded_account — re-validate balance >= trader_cut.
      3. Debit funded_account (balance, equity).
      4. Create LedgerEntry(EV_FUNDED_PAYOUT, amount=-trader_cut).
      5. Credit user wallet via credit_wallet(TX_FUNDED_PAYOUT).
      6. Reset cycle: initial_balance = post-debit balance.
      7. Mark FPR completed with all references and timestamps.

    Raises:
        FundedPayoutAlreadyProcessed — status != pending.
        ValueError                   — funded_type != FUNDED_SIM.
        InsufficientFundedBalance    — balance < trader_cut at approval time.
    """
    with transaction.atomic():
        fpr_locked = FundedPayoutRequest.objects.select_for_update().get(pk=fpr.pk)

        if fpr_locked.status != FundedPayoutRequest.ST_PENDING:
            raise FundedPayoutAlreadyProcessed(
                f"FundedPayoutRequest #{fpr.pk} is not pending (status={fpr_locked.status})."
            )
        if fpr_locked.funded_type != FundedConfig.FUNDED_SIM:
            raise ValueError(
                f"FundedPayoutRequest #{fpr.pk} funded_type={fpr_locked.funded_type} — "
                "use H.3 flow for FUNDED_INTERNAL."
            )

        account = TradingAccount.objects.select_for_update().get(
            pk=fpr_locked.funded_account_id
        )
        trader_cut      = Decimal(str(fpr_locked.trader_cut))
        current_balance = Decimal(str(account.balance))

        if current_balance < trader_cut:
            raise InsufficientFundedBalance(
                f"Account #{account.pk} balance={current_balance} < "
                f"trader_cut={trader_cut} at approval time."
            )

        new_balance = current_balance - trader_cut

        TradingAccount.objects.filter(pk=account.pk).update(
            balance=new_balance,
            equity=new_balance,
        )

        ledger = LedgerEntry.objects.create(
            account_id=account.pk,
            event_type=LedgerEntry.EV_FUNDED_PAYOUT,
            amount=-trader_cut,
            balance_after=new_balance,
            meta={
                "funded_payout_request_id": fpr.pk,
                "broker_cut":   str(fpr_locked.broker_cut),
                "cycle_profit": str(fpr_locked.cycle_profit),
            },
        )

        wallet, _ = get_or_create_wallet(fpr_locked.user)
        wallet_tx = credit_wallet(
            wallet.id,
            trader_cut,
            WalletTransaction.TX_FUNDED_PAYOUT,
            note=f"Funded SIM payout #{fpr.pk}",
            initiated_by=admin_user,
        )

        TradingAccount.objects.filter(pk=account.pk).update(initial_balance=new_balance)

        _now = now()
        FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
            status=FundedPayoutRequest.ST_COMPLETED,
            ledger_entry=ledger,
            wallet_credit_tx=wallet_tx,
            reviewed_by=admin_user,
            reviewed_at=_now,
            cycle_reset_at=_now,
            updated_at=_now,
        )

        # AUDIT-02 — fail-open: never allowed to affect the payout above,
        # win or lose. See broker_audit.record_event()'s own contract.
        from . import broker_audit as _audit
        _audit.record_payment_event(
            event_type=_audit.EV_FUNDED_PAYOUT_SIM_APPROVED,
            actor_id=getattr(admin_user, "pk", None),
            account=account, funded_payout_request=fpr_locked,
            correlation_id=fpr_locked.correlation_id,
            source_module="simulator.funded_payouts",
            description=f"FUNDED_SIM payout #{fpr.pk} approved — trader_cut {trader_cut}",
            metadata={
                "trader_cut": float(trader_cut),
                "broker_cut": float(fpr_locked.broker_cut),
                "wallet_tx_id": wallet_tx.id,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# H.3 — FUNDED_INTERNAL approval (bifásic)
# ─────────────────────────────────────────────────────────────────────────────

def approve_internal_payout(
    fpr: FundedPayoutRequest,
    admin_user,
    callback_url: str = "",
) -> None:
    """
    Bifásic approval for a FUNDED_INTERNAL FundedPayoutRequest.

    Phase 1 — transaction.atomic() (all DB writes committed before NP call):
      - Lock + validate FPR: status == pending, funded_type == FUNDED_INTERNAL,
        crypto_currency and wallet_address non-empty.
      - Lock + validate funded_account: balance >= trader_cut.
      - Debit funded_account (balance, equity -= trader_cut).
      - Create LedgerEntry(EV_FUNDED_PAYOUT, -trader_cut).
      - Create WithdrawalRequest(STATUS_APPROVED, debit_tx=None) — no wallet debit.
      - Link WR + ledger in FPR; FPR → ST_APPROVED, reviewed_by/at.
      - NO cycle reset, NO initial_balance change.

    Phase 2 — outside atomic (external HTTP call):
      FIX-FUNDED-INTERNAL-PAYOUT-AMBIGUOUS-FAILURE-01 — classifies Phase 2
      failures by WHICH call raised them (same discipline
      payout_providers.py::NowPaymentsAdapter.create_payout() already uses
      for retail — exception class alone cannot tell a pre-send auth
      failure from a post-send POST failure, since both go through the
      same requests/HTTPError machinery):
        - _np.estimate_price() fails            -> pre-send-safe, nothing
          was ever sent to NowPayments. Reverse + FAILED (unchanged).
        - _np._get_jwt_token() fails             -> pre-send-safe, the
          /v1/payout POST is structurally impossible to have happened.
          Reverse + FAILED (unchanged).
        - _np.create_payout_with_token() fails, OR its response body
          can't be parsed                        -> AMBIGUOUS. NowPayments
          may have already accepted the payout. NEVER reversed, NEVER
          marked FAILED — FPR/WR are left exactly at ST_APPROVED/
          STATUS_APPROVED (the state Phase 1 already committed them to),
          stamped with a structured admin_note marker and an
          EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_AMBIGUOUS audit event, pending
          manual reconciliation (mirrors retail's PayoutAttempt.UNKNOWN
          contract — see simulator/payout_orchestrator.py).
        - The POST succeeds (batch_id/payout_id obtained) but the local
          WithdrawalRequest/FundedPayoutRequest persistence write itself
          fails -> also AMBIGUOUS, never reversed (the payout is real —
          reversing it risks a genuine double payment on a later manual
          retry). The already-known batch_id/payout_id are preserved in
          the admin_note marker so a human has a concrete lead.
      Every branch re-raises the original exception so the admin action
      still surfaces an error to the caller — only the compensating side
      effect changes, never the raise-through-to-caller contract.

    Raises:
        FundedPayoutAlreadyProcessed — FPR status != pending.
        ValueError                   — funded_type != FUNDED_INTERNAL
                                       or missing crypto fields.
        InsufficientFundedBalance    — balance < trader_cut at approval time.
        Any NowPayments/persistence exception — after the appropriate
                                       compensating action above.
    """
    # ── Phase 1: DB claims ────────────────────────────────────────────────────
    with transaction.atomic():
        fpr_locked = FundedPayoutRequest.objects.select_for_update().get(pk=fpr.pk)

        if fpr_locked.status != FundedPayoutRequest.ST_PENDING:
            raise FundedPayoutAlreadyProcessed(
                f"FundedPayoutRequest #{fpr.pk} is not pending (status={fpr_locked.status})."
            )
        if fpr_locked.funded_type != FundedConfig.FUNDED_INTERNAL:
            raise ValueError(
                f"FundedPayoutRequest #{fpr.pk} funded_type={fpr_locked.funded_type} — "
                "use H.2 flow for FUNDED_SIM."
            )
        if not fpr_locked.crypto_currency or not fpr_locked.wallet_address:
            raise ValueError(
                f"FundedPayoutRequest #{fpr.pk} requires crypto_currency and wallet_address "
                "for FUNDED_INTERNAL."
            )

        account = TradingAccount.objects.select_for_update().get(
            pk=fpr_locked.funded_account_id
        )
        trader_cut      = Decimal(str(fpr_locked.trader_cut))
        current_balance = Decimal(str(account.balance))

        if current_balance < trader_cut:
            raise InsufficientFundedBalance(
                f"Account #{account.pk} balance={current_balance} < "
                f"trader_cut={trader_cut} at approval time."
            )

        new_balance = current_balance - trader_cut

        TradingAccount.objects.filter(pk=account.pk).update(
            balance=new_balance,
            equity=new_balance,
        )

        ledger = LedgerEntry.objects.create(
            account_id=account.pk,
            event_type=LedgerEntry.EV_FUNDED_PAYOUT,
            amount=-trader_cut,
            balance_after=new_balance,
            meta={
                "funded_payout_request_id": fpr.pk,
                "broker_cut":   str(fpr_locked.broker_cut),
                "cycle_profit": str(fpr_locked.cycle_profit),
                "funded_type":  FundedConfig.FUNDED_INTERNAL,
            },
        )

        _now = now()
        wr = WithdrawalRequest.objects.create(
            user=fpr_locked.user,
            amount_usd=trader_cut,
            crypto_currency=fpr_locked.crypto_currency,
            wallet_address=fpr_locked.wallet_address,
            status=WithdrawalRequest.STATUS_APPROVED,
            reviewed_by=admin_user,
            reviewed_at=_now,
            debit_tx=None,
        )

        FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
            status=FundedPayoutRequest.ST_APPROVED,
            withdrawal_request=wr,
            ledger_entry=ledger,
            reviewed_by=admin_user,
            reviewed_at=_now,
            updated_at=_now,
        )

        # AUDIT-02 — fail-open, same discipline as H.2 above.
        from . import broker_audit as _audit
        _audit.record_payment_event(
            event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_APPROVED,
            actor_id=getattr(admin_user, "pk", None),
            account=account, funded_payout_request=fpr_locked,
            correlation_id=fpr_locked.correlation_id,
            source_module="simulator.funded_payouts",
            description=f"FUNDED_INTERNAL payout #{fpr.pk} approved (Phase 1) — trader_cut {trader_cut}",
            metadata={
                "trader_cut": float(trader_cut),
                "broker_cut": float(fpr_locked.broker_cut),
                "withdrawal_request_id": wr.id,
                "crypto_currency": fpr_locked.crypto_currency,
            },
        )

    # Phase 1 is committed. fpr_locked holds snapshot data; wr has its DB PK.

    # ── Phase 2: NowPayments API call ─────────────────────────────────────────
    # Each step is its own try/except so a failure is classified by WHICH
    # call raised it, not by exception class alone (see the docstring above
    # and payout_providers.py::NowPaymentsAdapter.create_payout(), the
    # retail counterpart this mirrors).
    try:
        crypto_amount = _np.estimate_price(trader_cut, fpr_locked.crypto_currency)
    except Exception as exc:
        _reverse_and_fail(
            fpr_locked, wr, trader_cut, admin_user,
            reason=f"estimate_price failed — nothing was sent to NowPayments: {exc}",
        )
        raise

    try:
        token = _np._get_jwt_token()
    except Exception as exc:
        _reverse_and_fail(
            fpr_locked, wr, trader_cut, admin_user,
            reason=f"NowPayments auth failed — /v1/payout was never attempted: {exc}",
        )
        raise

    try:
        data = _np.create_payout_with_token(
            fpr_locked.wallet_address,
            fpr_locked.crypto_currency,
            crypto_amount,
            wr.id,
            callback_url,
            token,
        )
    except Exception as exc:
        # Ambiguous — the POST was genuinely attempted with a valid token;
        # NowPayments may have already accepted it. Never reversed.
        _mark_ambiguous(
            fpr_locked, wr,
            reason=f"payout POST failed after auth succeeded — provider outcome unknown: {exc}",
        )
        raise

    try:
        batch_wds  = data.get("withdrawals", [])
        batch_id   = str(data.get("id", ""))
        payout_id  = str(batch_wds[0].get("id", "")) if batch_wds else ""
        raw_status = str(data.get("status", ""))
    except (AttributeError, TypeError, KeyError, IndexError) as exc:
        # A response was received but couldn't be parsed — still ambiguous,
        # the POST itself may well have been accepted.
        _mark_ambiguous(
            fpr_locked, wr,
            reason=f"payout POST returned an unparseable body: {exc}",
        )
        raise

    try:
        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            status=WithdrawalRequest.STATUS_PROCESSING,
            np_batch_id=batch_id,
            np_payout_id=payout_id,
            np_payout_status=raw_status,
            crypto_amount=crypto_amount,
        )
        FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
            status=FundedPayoutRequest.ST_PROCESSING,
            updated_at=now(),
        )
    except Exception as exc:
        # The provider DEFINITELY accepted this payout (we have real
        # batch_id/payout_id) — a local persistence failure must never
        # trigger a reversal, that would risk a genuine double payment on
        # a later manual retry. Preserve the ids we already have.
        _mark_ambiguous(
            fpr_locked, wr,
            reason=f"payout succeeded at provider but local persistence failed: {exc}",
            batch_id=batch_id, payout_id=payout_id,
        )
        raise

    # AUDIT-02 — fail-open. Outside transaction.atomic() (Phase 2 is an
    # external HTTP call, same as the rest of this block) but the audit
    # write itself still opens its own nested savepoint inside
    # record_event() — a failure here cannot roll back the NP call that
    # already succeeded.
    from . import broker_audit as _audit
    _audit.record_payment_event(
        event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_SUBMITTED,
        actor_id=getattr(admin_user, "pk", None),
        account_id=fpr_locked.funded_account_id, funded_payout_request=fpr_locked,
        correlation_id=fpr_locked.correlation_id,
        source_module="simulator.funded_payouts",
        description=f"FUNDED_INTERNAL payout #{fpr.pk} submitted to NowPayments",
        metadata={
            "np_batch_id": batch_id, "np_payout_id": payout_id,
            "crypto_amount": str(crypto_amount),
        },
    )


def _reverse_and_fail(fpr_locked, wr, trader_cut, admin_user, *, reason):
    """Pre-send-safe failure (estimate_price or auth failed BEFORE the
    /v1/payout POST could have been attempted) — safe to reverse. Byte-
    identical compensating transaction to what this block used to do
    unconditionally for every Phase 2 exception."""
    with transaction.atomic():
        account_rev = TradingAccount.objects.select_for_update().get(
            pk=fpr_locked.funded_account_id
        )
        restored = Decimal(str(account_rev.balance)) + trader_cut
        TradingAccount.objects.filter(pk=account_rev.pk).update(
            balance=restored,
            equity=restored,
        )
        LedgerEntry.objects.create(
            account_id=account_rev.pk,
            event_type=LedgerEntry.EV_ADJUST,
            amount=trader_cut,
            balance_after=restored,
            meta={
                "reversa_funded_payout": fpr_locked.pk,
                "reason": reason,
            },
        )
        FundedPayoutRequest.objects.filter(pk=fpr_locked.pk).update(
            status=FundedPayoutRequest.ST_FAILED,
            updated_at=now(),
        )
        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            status=WithdrawalRequest.STATUS_FAILED,
        )

        # AUDIT-02 — fail-open. actor_type=SYSTEM: this is an automated
        # NowPayments failure + compensating reversal, not a staff decision.
        from . import broker_audit as _audit
        _audit.record_payment_event(
            event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_FAILED,
            severity=_audit.Severity.HIGH, actor_type=_audit.ActorType.SYSTEM,
            account_id=fpr_locked.funded_account_id, funded_payout_request=fpr_locked,
            correlation_id=fpr_locked.correlation_id,
            source_module="simulator.funded_payouts",
            description=f"FUNDED_INTERNAL payout #{fpr_locked.pk} submit FAILED — reversed",
            metadata={
                "trader_cut": float(trader_cut),
                "withdrawal_request_id": wr.id,
                "reason": reason,
            },
        )


def _mark_ambiguous(fpr_locked, wr, *, reason, batch_id="", payout_id=""):
    """Ambiguous post-send outcome — NowPayments may already have accepted
    the payout. NEVER reverses the funded-account debit, NEVER marks FPR/WR
    FAILED. Leaves them exactly at ST_APPROVED/STATUS_APPROVED (the state
    Phase 1 already committed) — a durable, monitored resting state
    pending manual reconciliation, mirroring retail's
    PayoutAttempt.STATUS_UNKNOWN contract. Both the admin_note write and
    the audit event are individually best-effort (wrapped so a failure
    here can never mask or replace the original exception that triggered
    this call)."""
    marker = f"AMBIGUOUS_SUBMIT_FAILURE @ {now().isoformat()} — {reason}"
    if batch_id or payout_id:
        marker += f" [batch_id={batch_id!r} payout_id={payout_id!r}]"

    try:
        FundedPayoutRequest.objects.filter(pk=fpr_locked.pk).update(
            admin_note=marker, updated_at=now(),
        )
    except Exception:
        logger.exception(
            "[funded_payouts] failed to persist ambiguous-failure marker for FPR #%d",
            fpr_locked.pk,
        )

    try:
        from . import broker_audit as _audit
        _audit.record_payment_event(
            event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_SUBMIT_AMBIGUOUS,
            severity=_audit.Severity.HIGH, actor_type=_audit.ActorType.SYSTEM,
            account_id=fpr_locked.funded_account_id, funded_payout_request=fpr_locked,
            correlation_id=fpr_locked.correlation_id,
            source_module="simulator.funded_payouts",
            description=(
                f"FUNDED_INTERNAL payout #{fpr_locked.pk} submission outcome AMBIGUOUS "
                "— held for manual reconciliation, NOT reversed"
            ),
            metadata={
                "withdrawal_request_id": wr.id, "reason": reason,
                "np_batch_id": batch_id, "np_payout_id": payout_id,
            },
        )
    except Exception:
        logger.exception(
            "[funded_payouts] failed to record ambiguous-failure audit event for FPR #%d",
            fpr_locked.pk,
        )


# ─────────────────────────────────────────────────────────────────────────────
# H.3 — FUNDED_INTERNAL webhook handler
# ─────────────────────────────────────────────────────────────────────────────

_TERMINAL_FPR = (
    FundedPayoutRequest.ST_COMPLETED,
    FundedPayoutRequest.ST_FAILED,
    FundedPayoutRequest.ST_REJECTED,
    FundedPayoutRequest.ST_CANCELLED,
)


def handle_internal_payout_webhook(
    fpr: FundedPayoutRequest,
    wr: WithdrawalRequest,
    new_status: str,
    payout_id: str,
) -> None:
    """
    Process a NowPayments IPN event for a FUNDED_INTERNAL FundedPayoutRequest.

    Called from withdraw_payout_callback when wr.funded_payout_internal is set.
    Safe to call inside an outer transaction.atomic() — uses savepoint semantics.

    On STATUS_COMPLETED (NP FINISHED):
      - Idempotent if FPR is already in a terminal state.
      - Reset funded_account.initial_balance = current balance (post-debit).
      - Mark FPR completed + set cycle_reset_at.
      - No wallet touch.

    On STATUS_FAILED (NP FAILED):
      - Idempotent if FPR is already in a terminal state.
      - Reverse funded_account debit: balance/equity += trader_cut.
      - Create LedgerEntry(EV_ADJUST, +trader_cut).
      - Mark FPR failed.
      - No wallet touch, no cycle reset.
    """
    with transaction.atomic():
        fpr_locked = FundedPayoutRequest.objects.select_for_update().get(pk=fpr.pk)

        if fpr_locked.status in _TERMINAL_FPR:
            return  # idempotent — already in terminal state

        if new_status == WithdrawalRequest.STATUS_COMPLETED:
            account = TradingAccount.objects.select_for_update().get(
                pk=fpr_locked.funded_account_id
            )
            # initial_balance resets to current post-debit balance
            TradingAccount.objects.filter(pk=account.pk).update(
                initial_balance=account.balance,
            )
            _now = now()
            FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
                status=FundedPayoutRequest.ST_COMPLETED,
                cycle_reset_at=_now,
                updated_at=_now,
            )

            # AUDIT-02 — fail-open. actor_type=SYSTEM: triggered by the
            # NowPayments webhook, no staff in the loop at this instant.
            # This is the ONLY durable record of this transition today —
            # withdraw_payout_callback's own log_audit() calls are skipped
            # entirely for FUNDED_INTERNAL rows (see views.py:2522-2525).
            from . import broker_audit as _audit
            _audit.record_payment_event(
                event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_COMPLETED,
                actor_type=_audit.ActorType.SYSTEM,
                account=account, funded_payout_request=fpr_locked,
                correlation_id=fpr_locked.correlation_id,
                source_module="simulator.funded_payouts",
                description=f"FUNDED_INTERNAL payout #{fpr.pk} completed via NowPayments webhook",
                metadata={"cycle_reset_at": _now.isoformat()},
            )

        elif new_status == WithdrawalRequest.STATUS_FAILED:
            account = TradingAccount.objects.select_for_update().get(
                pk=fpr_locked.funded_account_id
            )
            trader_cut       = Decimal(str(fpr_locked.trader_cut))
            restored_balance = Decimal(str(account.balance)) + trader_cut
            TradingAccount.objects.filter(pk=account.pk).update(
                balance=restored_balance,
                equity=restored_balance,
            )
            LedgerEntry.objects.create(
                account_id=account.pk,
                event_type=LedgerEntry.EV_ADJUST,
                amount=trader_cut,
                balance_after=restored_balance,
                meta={
                    "reversa_funded_payout": fpr.pk,
                    "payout_id": payout_id,
                    "reason": "NowPayments payout FAILED",
                },
            )
            FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
                status=FundedPayoutRequest.ST_FAILED,
                updated_at=now(),
            )

            # AUDIT-02 — fail-open. Same rationale as STATUS_COMPLETED above:
            # this is the only durable record of this transition today —
            # withdraw_payout_callback's own log_audit() calls are skipped
            # entirely for FUNDED_INTERNAL rows (see views.py:2522-2525).
            from . import broker_audit as _audit
            _audit.record_payment_event(
                event_type=_audit.EV_FUNDED_PAYOUT_INTERNAL_FAILED,
                severity=_audit.Severity.HIGH, actor_type=_audit.ActorType.SYSTEM,
                account=account, funded_payout_request=fpr_locked,
                correlation_id=fpr_locked.correlation_id,
                source_module="simulator.funded_payouts",
                description=f"FUNDED_INTERNAL payout #{fpr.pk} FAILED via NowPayments webhook — reversed",
                metadata={"payout_id": payout_id, "trader_cut": float(trader_cut)},
            )

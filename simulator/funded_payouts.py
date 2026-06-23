"""
simulator/funded_payouts.py

Financial service layer for funded account payouts.
Imported by admin.py (approval actions) and views.py (webhook handler).
No dependency on admin.py or views.py.

H.2: FUNDED_SIM      — atomic approval + wallet credit + immediate cycle reset.
H.3: FUNDED_INTERNAL — bifásic approval (DB debit then NP call) + webhook handler.
"""
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
      - _np.estimate_price then _np.create_payout.
      - On success: WR → STATUS_PROCESSING + NP batch/payout IDs;
        FPR → ST_PROCESSING.
      - On any exception: compensating transaction — reverse funded_account
        debit, create EV_ADJUST, mark FPR + WR failed. Then re-raises so
        the admin action can surface the error.

    Raises:
        FundedPayoutAlreadyProcessed — FPR status != pending.
        ValueError                   — funded_type != FUNDED_INTERNAL
                                       or missing crypto fields.
        InsufficientFundedBalance    — balance < trader_cut at approval time.
        Any NowPayments exception    — after compensating reversal.
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

    # Phase 1 is committed. fpr_locked holds snapshot data; wr has its DB PK.

    # ── Phase 2: NowPayments API call ─────────────────────────────────────────
    try:
        crypto_amount = _np.estimate_price(trader_cut, fpr_locked.crypto_currency)
        data      = _np.create_payout(
            fpr_locked.wallet_address,
            fpr_locked.crypto_currency,
            crypto_amount,
            wr.id,
            callback_url,
        )
        batch_wds = data.get("withdrawals", [])
        batch_id  = str(data.get("id", ""))
        payout_id = str(batch_wds[0].get("id", "")) if batch_wds else ""

        WithdrawalRequest.objects.filter(pk=wr.pk).update(
            status=WithdrawalRequest.STATUS_PROCESSING,
            np_batch_id=batch_id,
            np_payout_id=payout_id,
            np_payout_status=str(data.get("status", "")),
            crypto_amount=crypto_amount,
        )
        FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
            status=FundedPayoutRequest.ST_PROCESSING,
            updated_at=now(),
        )

    except Exception:
        # ── Compensating transaction: reverse the funded account debit ─────────
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
                    "reversa_funded_payout": fpr.pk,
                    "reason": "NowPayments create_payout failed",
                },
            )
            FundedPayoutRequest.objects.filter(pk=fpr.pk).update(
                status=FundedPayoutRequest.ST_FAILED,
                updated_at=now(),
            )
            WithdrawalRequest.objects.filter(pk=wr.pk).update(
                status=WithdrawalRequest.STATUS_FAILED,
            )
        raise  # re-raise so admin action surfaces the error


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

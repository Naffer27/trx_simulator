"""
simulator/tests/factories.py
Helpers for creating test data. Each returns a fully-saved Django model instance.

Rules:
  - Every helper generates unique usernames via uuid so tests never collide.
  - No global state. Callers compose exactly what they need per test.
  - make_wallet() uses credit_wallet() to establish initial_balance so the
    WalletTransaction ledger is consistent from the start (reconcile_wallet passes).
  - Amounts are always Decimal, never float.
"""
import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model

from simulator.models import (
    BrokerLedger, LedgerEntry, Position, Trade, TradingAccount, Wallet, WalletTransaction,
)
from simulator.wallet_ledger import credit_wallet, get_or_create_wallet

User = get_user_model()


def make_user(username: str | None = None, password: str = "testpass123", **kwargs) -> User:
    """Create and return a unique User."""
    if username is None:
        username = f"user_{uuid.uuid4().hex[:8]}"
    return User.objects.create_user(username=username, password=password, **kwargs)


def make_wallet(user=None, initial_balance: Decimal = Decimal("0")) -> Wallet:
    """
    Return the Wallet for *user* (creates one if missing).
    If initial_balance > 0, seeds it with a TX_DEPOSIT so the ledger is clean.
    """
    if user is None:
        user = make_user()
    wallet, _ = get_or_create_wallet(user)
    if initial_balance > Decimal("0"):
        credit_wallet(
            wallet.id,
            initial_balance,
            WalletTransaction.TX_DEPOSIT,
            note="test setup",
        )
        wallet.refresh_from_db()
    return wallet


def make_account(
    user=None,
    account_type: str = "CHALLENGE",
    tier: str = "10K",
    balance: Decimal = Decimal("10000"),
    status: str = "Activo",
    **kwargs,
) -> TradingAccount:
    """Create a TradingAccount with sensible defaults for tests."""
    if user is None:
        user = make_user()
    balance = Decimal(str(balance))
    return TradingAccount.objects.create(
        user=user,
        account_type=account_type,
        tier=tier,
        balance=balance,
        equity=balance,
        peak_balance=balance,
        initial_balance=balance,
        status=status,
        leverage=50,
        **kwargs,
    )


def make_spread_config(
    symbol: str = "EUR/USD",
    spread_pips: Decimal = Decimal("2.00"),
    enabled: bool = True,
) -> "BrokerSpreadConfig":
    """
    Create a BrokerSpreadConfig. Symbol is auto-normalized by the model's save()
    (e.g. 'EURUSD' → 'EUR/USD'), so tests can pass either form.
    """
    from simulator.models import BrokerSpreadConfig

    return BrokerSpreadConfig.objects.create(
        symbol=symbol,
        spread_pips=spread_pips,
        enabled=enabled,
    )


def make_deposit(
    user,
    amount_usd: Decimal = Decimal("100.00"),
    crypto_currency: str = "btc",
    payment_id: str | None = "pay_test_001",
    status: str = "pending",
    credited: bool = False,
) -> "Deposit":
    """Create a Deposit record. Does NOT touch the wallet — tests control that."""
    from simulator.models import Deposit

    return Deposit.objects.create(
        user=user,
        amount_usd=amount_usd,
        crypto_currency=crypto_currency,
        nowpayments_payment_id=payment_id,
        status=status,
        credited=credited,
    )


def make_trade(
    account: TradingAccount,
    symbol: str = "EUR/USD",
    trade_type: str = "BUY",
    lot_size: Decimal = Decimal("0.1"),
    entry_price: Decimal = Decimal("1.10000"),
    exit_price: Decimal | None = None,
    profit_loss: Decimal | None = None,
) -> Trade:
    """Create a Trade (fill) on *account*."""
    return Trade.objects.create(
        account=account,
        symbol=symbol,
        trade_type=trade_type,
        lot_size=Decimal(str(lot_size)),
        entry_price=Decimal(str(entry_price)),
        exit_price=Decimal(str(exit_price)) if exit_price is not None else None,
        profit_loss=Decimal(str(profit_loss)) if profit_loss is not None else None,
    )


def make_ledger_entry(
    account: TradingAccount,
    event_type: str = LedgerEntry.EV_REALIZED,
    amount: Decimal = Decimal("100.00"),
    balance_after: Decimal = Decimal("10100.00"),
) -> LedgerEntry:
    """Create a LedgerEntry for *account*."""
    return LedgerEntry.objects.create(
        account=account,
        event_type=event_type,
        amount=Decimal(str(amount)),
        balance_after=Decimal(str(balance_after)),
    )


def make_broker_ledger(
    revenue_type: str = BrokerLedger.REV_SPREAD,
    amount: Decimal = Decimal("1.00"),
    source_account: TradingAccount | None = None,
    source_trade: Trade | None = None,
    source_ledger: LedgerEntry | None = None,
    symbol: str | None = "EUR/USD",
    meta: dict | None = None,
) -> BrokerLedger:
    """Create a BrokerLedger revenue entry."""
    return BrokerLedger.objects.create(
        revenue_type=revenue_type,
        amount=Decimal(str(amount)),
        source_account=source_account,
        source_trade=source_trade,
        source_ledger=source_ledger,
        symbol=symbol,
        meta=meta if meta is not None else {},
    )


def make_position(
    account: TradingAccount,
    symbol: str = "EUR/USD",
    side: str = "BUY",
    qty: Decimal = Decimal("0.1"),
    avg_price: Decimal = Decimal("1.1000"),
    sl: Decimal | None = None,
    tp: Decimal | None = None,
) -> Position:
    """Create an open Position on *account*."""
    return Position.objects.create(
        account=account,
        symbol=symbol,
        side=side,
        qty=Decimal(str(qty)),
        avg_price=Decimal(str(avg_price)),
        sl=Decimal(str(sl)) if sl is not None else None,
        tp=Decimal(str(tp)) if tp is not None else None,
    )

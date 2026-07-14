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
    AccountProduct, BrokerLedger, ChallengeEnrollment, ChallengeProduct, Deposit,
    EmailVerification, FundedConfig, LedgerEntry, Position, Trade, TradingAccount,
    TermsAcceptance, TERMS_VERSION, RISK_DISCLOSURE_VERSION,
    Wallet, WalletTransaction,
)
from simulator.wallet_ledger import credit_wallet, get_or_create_wallet

User = get_user_model()


def make_user(
    username: str | None = None,
    password: str = "testpass123",
    email_verified: bool = True,
    terms_accepted: bool = True,
    **kwargs,
) -> User:
    """
    Create and return a unique User.

    email_verified=True  — creates a confirmed EmailVerification so money-action
                           email gates pass in tests not focused on email verification.
    terms_accepted=True  — creates a TermsAcceptance for current versions so
                           legal gates pass in tests not focused on terms acceptance.
    Pass False for either to exercise the blocked-user path.
    """
    from django.utils import timezone as _tz

    if username is None:
        username = f"user_{uuid.uuid4().hex[:8]}"
    user = User.objects.create_user(username=username, password=password, **kwargs)
    EmailVerification.objects.create(
        user=user,
        verified=email_verified,
        verified_at=_tz.now() if email_verified else None,
    )
    if terms_accepted:
        TermsAcceptance.objects.create(
            user=user,
            terms_version=TERMS_VERSION,
            risk_disclaimer_version=RISK_DISCLOSURE_VERSION,
            ip_address=None,
            user_agent="",
        )
    return user


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
    peak_balance: Decimal | None = None,
    **kwargs,
) -> TradingAccount:
    """Create a TradingAccount with sensible defaults for tests."""
    if user is None:
        user = make_user()
    balance = Decimal(str(balance))
    _peak = Decimal(str(peak_balance)) if peak_balance is not None else balance
    return TradingAccount.objects.create(
        user=user,
        account_type=account_type,
        tier=tier,
        balance=balance,
        equity=balance,
        peak_balance=_peak,
        initial_balance=balance,
        status=status,
        leverage=50,
        **kwargs,
    )


def make_spread_config(
    symbol: str = "EUR/USD",
    spread_pips: Decimal = Decimal("2.00"),
    enabled: bool = True,
    min_spread: Decimal | None = None,
    max_spread: Decimal | None = None,
    bounds_enabled: bool = False,
    is_dynamic: bool = False,
    manual_multiplier: Decimal | None = None,
    manual_reason: str = "",
    manual_expires_at=None,
) -> "BrokerSpreadConfig":
    """
    Create a BrokerSpreadConfig. Symbol is auto-normalized by the model's save()
    (e.g. 'EURUSD' → 'EUR/USD'), so tests can pass either form.

    min_spread/max_spread are null (no floor/ceiling) unless passed
    explicitly. bounds_enabled defaults to False — matching the model's
    own default — meaning even an explicit min_spread/max_spread has no
    effect on broker_price()/build_commercial_pricing_profile() unless
    the caller also passes bounds_enabled=True. This is the opt-in design
    from the pre-commit floor/ceiling correction: a symbol's spread is
    never silently clamped just because a row happens to carry min/max
    values.

    is_dynamic defaults to False (SPREAD-05's opt-in, unchanged from
    SPREAD-04 behavior). manual_multiplier defaults to the model's own
    1.000 (no override) unless passed explicitly.
    """
    from simulator.models import BrokerSpreadConfig

    kwargs = dict(symbol=symbol, spread_pips=spread_pips, enabled=enabled,
                   spread_bounds_enabled=bounds_enabled, is_dynamic=is_dynamic,
                   manual_reason=manual_reason)
    if min_spread is not None:
        kwargs["min_spread"] = min_spread
    if max_spread is not None:
        kwargs["max_spread"] = max_spread
    if manual_multiplier is not None:
        kwargs["manual_multiplier"] = manual_multiplier
    if manual_expires_at is not None:
        kwargs["manual_expires_at"] = manual_expires_at
    return BrokerSpreadConfig.objects.create(**kwargs)


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


def make_account_product(
    name: str | None = None,
    product_type: str = AccountProduct.TYPE_RETAIL,
    family: str = AccountProduct.FAMILY_REAL,
    min_deposit: Decimal = Decimal("100.00"),
    default_balance: Decimal = Decimal("0.00"),
    max_leverage: int = 50,
    typical_spread_pips: Decimal = Decimal("0.00"),
    commission_per_lot: Decimal = Decimal("0.00"),
    commission_pct: Decimal = Decimal("0.0000"),
    spread_markup: Decimal = Decimal("0.0000"),
    features: dict | None = None,
    is_active: bool = True,
) -> AccountProduct:
    """Create an AccountProduct (non-challenge account catalog entry)."""
    if name is None:
        name = f"Product {product_type} {uuid.uuid4().hex[:4]}"
    return AccountProduct.objects.create(
        name=name,
        product_type=product_type,
        family=family,
        min_deposit=Decimal(str(min_deposit)),
        default_balance=Decimal(str(default_balance)),
        max_leverage=max_leverage,
        typical_spread_pips=Decimal(str(typical_spread_pips)),
        commission_per_lot=Decimal(str(commission_per_lot)),
        commission_pct=Decimal(str(commission_pct)),
        spread_markup=Decimal(str(spread_markup)),
        features=features if features is not None else {},
        is_active=is_active,
    )


def make_challenge_product(
    name: str | None = None,
    tier: str = ChallengeProduct.TIER_10K,
    price_usd: Decimal = Decimal("99.00"),
    account_size: Decimal = Decimal("10000.00"),
    p1_profit_target_pct: Decimal = Decimal("8.00"),
    p1_max_drawdown_pct: Decimal = Decimal("10.00"),
    p1_max_daily_loss_pct: Decimal = Decimal("5.00"),
    p1_min_trading_days: int = 5,
    p1_max_duration_days: int = 30,
    p2_profit_target_pct: Decimal = Decimal("5.00"),
    p2_max_drawdown_pct: Decimal = Decimal("10.00"),
    p2_max_daily_loss_pct: Decimal = Decimal("5.00"),
    p2_min_trading_days: int = 5,
    p2_max_duration_days: int = 60,
    profit_split_pct: Decimal = Decimal("80.00"),
    max_lot_size: Decimal = Decimal("5.00"),
    max_open_positions: int = 30,
    is_active: bool = True,
    external_code: str | None = None,
    spread_markup_pips: Decimal = Decimal("0.00"),
    commission_per_lot: Decimal = Decimal("0.00"),
    commission_pct: Decimal = Decimal("0.0000"),
    min_spread_pips: Decimal | None = None,
    max_spread_pips: Decimal | None = None,
) -> ChallengeProduct:
    """Create a ChallengeProduct with sensible defaults for the 10K tier."""
    if name is None:
        name = f"Challenge {tier} {uuid.uuid4().hex[:4]}"
    return ChallengeProduct.objects.create(
        name=name,
        tier=tier,
        price_usd=Decimal(str(price_usd)),
        account_size=Decimal(str(account_size)),
        p1_profit_target_pct=Decimal(str(p1_profit_target_pct)),
        p1_max_drawdown_pct=Decimal(str(p1_max_drawdown_pct)),
        p1_max_daily_loss_pct=Decimal(str(p1_max_daily_loss_pct)),
        p1_min_trading_days=p1_min_trading_days,
        p1_max_duration_days=p1_max_duration_days,
        p2_profit_target_pct=Decimal(str(p2_profit_target_pct)),
        p2_max_drawdown_pct=Decimal(str(p2_max_drawdown_pct)),
        p2_max_daily_loss_pct=Decimal(str(p2_max_daily_loss_pct)),
        p2_min_trading_days=p2_min_trading_days,
        p2_max_duration_days=p2_max_duration_days,
        profit_split_pct=Decimal(str(profit_split_pct)),
        max_lot_size=Decimal(str(max_lot_size)),
        max_open_positions=max_open_positions,
        is_active=is_active,
        external_code=external_code,
        spread_markup_pips=Decimal(str(spread_markup_pips)),
        commission_per_lot=Decimal(str(commission_per_lot)),
        commission_pct=Decimal(str(commission_pct)),
        min_spread_pips=Decimal(str(min_spread_pips)) if min_spread_pips is not None else None,
        max_spread_pips=Decimal(str(max_spread_pips)) if max_spread_pips is not None else None,
    )


def make_challenge_enrollment(
    user=None,
    product: ChallengeProduct | None = None,
    deposit: Deposit | None = None,
    phase1_account: TradingAccount | None = None,
    status: str = ChallengeEnrollment.ST_PHASE_1,
) -> ChallengeEnrollment:
    """Create a ChallengeEnrollment. Deposit=None is valid (admin-issued)."""
    if user is None:
        user = make_user()
    if product is None:
        product = make_challenge_product()
    return ChallengeEnrollment.objects.create(
        user=user,
        product=product,
        deposit=deposit,
        phase1_account=phase1_account,
        status=status,
    )


def make_kyc_approved(user) -> "KYCProfile":
    """Create or update a KYCProfile for *user* with status=approved."""
    from simulator.models import KYCProfile
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    kyc.status = KYCProfile.STATUS_APPROVED
    kyc.legal_name = "Test User"
    kyc.country = "Test Country"
    kyc.document_type = "national_id"
    kyc.save()
    return kyc


def make_funded_config(
    enrollment: ChallengeEnrollment | None = None,
    funded_type: str = FundedConfig.FUNDED_SIM,
    profit_split_pct: Decimal = Decimal("80.00"),
    min_payout_usd: Decimal = Decimal("50.00"),
    min_trading_days: int = 5,
    payout_cycle_days: int = 14,
    max_monthly_drawdown_pct: Decimal = Decimal("5.00"),
) -> FundedConfig:
    """Create a FundedConfig for a given enrollment."""
    if enrollment is None:
        enrollment = make_challenge_enrollment()
    return FundedConfig.objects.create(
        enrollment=enrollment,
        funded_type=funded_type,
        profit_split_pct=Decimal(str(profit_split_pct)),
        min_payout_usd=Decimal(str(min_payout_usd)),
        min_trading_days=min_trading_days,
        payout_cycle_days=payout_cycle_days,
        max_monthly_drawdown_pct=Decimal(str(max_monthly_drawdown_pct)),
    )

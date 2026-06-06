from decimal import Decimal

from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()

# ─────────────────────────────────────────────
# Engine type classification
# Single source of truth — used in risk_engine, consumers, admin, wallet_ledger
# ─────────────────────────────────────────────
MARGIN_ENGINE_TYPES = frozenset({"RETAIL", "ECN", "STANDARD", "DEMO", "CRYPTO"})
DD_ENGINE_TYPES     = frozenset({"CHALLENGE", "FUNDED"})


# ─────────────────────────────────────────────
# Wallet system
# ─────────────────────────────────────────────

class Wallet(models.Model):
    """
    One per user. The single source of unallocated funds.

    available_balance is a MATERIALIZED sum of WalletTransaction.amount.
    It must NEVER be updated outside wallet_ledger.credit_wallet() /
    wallet_ledger.debit_wallet(). All writes are atomic and append a ledger row.

    pending_balance is informational (in-flight deposits, display only).
    It does NOT enter the ledger until the deposit is confirmed.
    """
    user              = models.OneToOneField(User, on_delete=models.PROTECT, related_name="wallet")
    currency          = models.CharField(max_length=6, default="USD")
    available_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    pending_balance   = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user"], name="wallet_user_idx")]

    def __str__(self):
        return f"Wallet({self.user_id}) {self.currency} avail={self.available_balance}"


class TradingAccount(models.Model):
    # Nivel de cuenta (fondo)
    ACCOUNT_TIERS = [
        ('10K',  'Cuenta 10 000'),
        ('50K',  'Cuenta 50 000'),
        ('100K', 'Cuenta 100 000'),
    ]

    ACCOUNT_TYPES = [
        # Margin engine (broker real) — risk_engine uses margin_level / liquidation
        ('RETAIL',    'Retail'),
        ('ECN',       'ECN'),
        ('STANDARD',  'Standard'),
        ('DEMO',      'Demo'),
        ('CRYPTO',    'Crypto'),
        # DD engine (prop firm) — risk_engine uses drawdown / violations
        ('CHALLENGE', 'Challenge'),
        ('FUNDED',    'Funded'),
    ]

    user         = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    wallet       = models.ForeignKey(
        "Wallet", on_delete=models.PROTECT,
        null=True, blank=True, related_name="trading_accounts",
        help_text="Wallet from which this account was funded",
    )
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPES, default='CHALLENGE')
    tier         = models.CharField(max_length=4, choices=ACCOUNT_TIERS, null=True, blank=True, help_text="Elige el plan de fondeo (Challenge/Funded only)")

    phase = models.CharField(
        max_length=20,
        choices=[('Fase 1', 'Fase 1'), ('Fase 2', 'Fase 2'), ('Funded', 'Funded')],
        default='Fase 1'
    )

    # Saldos / métricas
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=10000.00)
    equity = models.DecimalField(max_digits=12, decimal_places=2, default=10000.00)
    peak_balance = models.DecimalField(max_digits=12, decimal_places=2, default=10000.00)
    drawdown = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    initial_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    profit_target   = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    max_drawdown    = models.DecimalField(max_digits=12, decimal_places=2, default=1200.00)

    # Config cuenta
    currency = models.CharField(max_length=6, default='USD')  # presente hasta 0007
    leverage = models.PositiveIntegerField(default=50)
    netting_mode = models.BooleanField(
        default=False,
        help_text='True=Netting (consolidar por símbolo); False=Hedging (varias posiciones).'
    )

    # ── Product link + frozen rule snapshots (Phase 6B) ───────────────────────
    account_product = models.ForeignKey(
        'AccountProduct', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='trading_accounts',
    )
    product_code_snapshot       = models.CharField(max_length=32, null=True, blank=True)
    product_name_snapshot       = models.CharField(max_length=64, null=True, blank=True)
    leverage_snapshot           = models.PositiveIntegerField(null=True, blank=True)
    spread_pips_snapshot        = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    commission_per_lot_snapshot = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    allowed_symbols_snapshot    = models.JSONField(null=True, blank=True)
    max_lot_size_snapshot       = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    margin_call_level_snapshot  = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    stopout_level_snapshot      = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    STATUS_ACTIVE    = 'Activo'
    STATUS_SUSPENDED = 'Suspendido'
    STATUS_VIOLATED  = 'Violado'
    STATUS_CLOSED    = 'Cerrado'
    STATUS_FUNDED    = 'Completado'

    STATUS_CHOICES = [
        ('Activo',     'Active'),
        ('Suspendido', 'Suspended'),
        ('Violado',    'Violated'),
        ('Cerrado',    'Closed'),
        ('Completado', 'Funded/Completed'),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Activo')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"], name="acc_status_idx"),
            models.Index(fields=["user", "phase"], name="acc_user_phase_idx"),
        ]

    def __str__(self):
        usr = getattr(self.user, 'username', self.user_id)
        if self.account_type in MARGIN_ENGINE_TYPES:
            bal = self.initial_balance or self.balance
            return f"{usr} — {self.account_type} ${bal} — {self.status}"
        return f"{usr} — {self.account_type}/{self.tier} — {self.phase}"

    # ------------------------------
    # 🆕 Validador de reglas de fondeo
    # ------------------------------
    def check_rules(self):
        from .models import LedgerEntry

        # New accounts have no PK yet — skip suspension/ledger checks entirely.
        # Balance at $0 on creation is intentional (wallet-funded flow), not a wipeout.
        if self._state.adding:
            return

        def _d(v):
            return Decimal(str(v)) if not isinstance(v, Decimal) else v

        # ---- Profit Target (Challenge/Funded only) ----
        if (
            self.account_type in ('CHALLENGE', 'FUNDED')
            and self.profit_target is not None
            and _d(self.balance) >= (_d(self.profit_target) + _d(self.equity or 0))
        ):
            if self.phase == 'Fase 1':
                self.phase = 'Fase 2'
                LedgerEntry.objects.create(
                    account=self,
                    event_type=LedgerEntry.EV_ADJUST,
                    amount=0,
                    balance_after=self.balance,
                    meta={"msg": "Avanzó a Fase 2"},
                )
            elif self.phase == 'Fase 2':
                self.phase = 'Funded'
                LedgerEntry.objects.create(
                    account=self,
                    event_type=LedgerEntry.EV_ADJUST,
                    amount=0,
                    balance_after=self.balance,
                    meta={"msg": "Cuenta ahora es Funded"},
                )

        # ---- Max Drawdown ----
        # DD engine (CHALLENGE/FUNDED): drawdown breach suspends account.
        # Margin engine (RETAIL/ECN/STANDARD/DEMO/CRYPTO): only full wipeout suspends;
        # real stopout is handled by the consumer's margin-level check.
        if self.account_type not in MARGIN_ENGINE_TYPES:
            if _d(self.drawdown) >= _d(self.max_drawdown) or _d(self.balance) <= 0:
                self.status = 'Suspendido'
                LedgerEntry.objects.create(
                    account=self,
                    event_type=LedgerEntry.EV_ADJUST,
                    amount=0,
                    balance_after=self.balance,
                    meta={"msg": "Cuenta suspendida por drawdown"},
                )
        else:
            if _d(self.balance) <= 0:
                self.status = 'Suspendido'
                LedgerEntry.objects.create(
                    account=self,
                    event_type=LedgerEntry.EV_ADJUST,
                    amount=0,
                    balance_after=self.balance,
                    meta={"msg": "Cuenta suspendida — balance agotado"},
                )

        # ---- Equity Check ----
        if _d(self.equity) <= 0:
            self.status = 'Completado'

    # Tier → starting balance used when initial_balance is omitted (challenge/funded path)
    _TIER_INITIAL = {
        '10K':  Decimal('10000'),
        '50K':  Decimal('50000'),
        '100K': Decimal('100000'),
    }

    def save(self, *args, **kwargs):
        def _d(v):
            return Decimal(str(v)) if not isinstance(v, Decimal) else v

        if self._state.adding:
            # ── Determine the canonical starting balance ───────────────────
            # Priority order:
            #  1. initial_balance — set explicitly (RETAIL admin form, or API)
            #  2. tier default    — challenge/funded accounts without explicit initial_balance
            #  3. balance field   — population_engine / anything that set balance directly
            if self.initial_balance is not None:
                ib = _d(self.initial_balance)
            elif self.tier and self.tier in self._TIER_INITIAL:
                ib = self._TIER_INITIAL[self.tier]
            else:
                ib = _d(self.balance)

            # Sync all three balance fields to the canonical starting value
            self.initial_balance = ib
            self.balance         = ib
            self.equity          = ib
            self.peak_balance    = ib
        else:
            # Existing account: ensure initial_balance is populated (pre-migration safety)
            if self.initial_balance is None:
                self.initial_balance = _d(self.balance)

        self.check_rules()
        super().save(*args, **kwargs)


class Position(models.Model):
    """Posiciones abiertas (hedging soportado: múltiples por símbolo)."""
    BUY = 'BUY'
    SELL = 'SELL'
    SIDE_CHOICES = [(BUY, BUY), (SELL, SELL)]

    account = models.ForeignKey(TradingAccount, on_delete=models.CASCADE, related_name='positions')
    symbol = models.CharField(max_length=12)                       # p.ej. 'EUR/USD', 'BTCUSD'
    side = models.CharField(max_length=4, choices=SIDE_CHOICES)
    qty = models.DecimalField(max_digits=18, decimal_places=6, default=0)   # tamaño
    avg_price = models.DecimalField(max_digits=18, decimal_places=6)        # precio medio de entrada
    sl = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    tp = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    external_id = models.CharField(max_length=64, null=True, blank=True)    # id externo (LP/exchange)
    opened_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['account', 'symbol']),
            # Supports ORDER BY opened_at in simulation tick + force-close queries
            models.Index(fields=["account", "opened_at"], name="pos_acc_opened_idx"),
        ]

    def __str__(self):
        return f"{self.account_id} {self.symbol} {self.side} qty={self.qty} @ {self.avg_price}"


class LedgerEntry(models.Model):
    """Libro mayor: cada evento contable."""
    EV_DEPOSIT = 'DEPOSIT'
    EV_WITHDRAW = 'WITHDRAW'
    EV_COMMISSION = 'COMMISSION'
    EV_REALIZED = 'REALIZED_PNL'
    EV_FEE = 'FEE'
    EV_ADJUST = 'ADJUSTMENT'

    EVENT_CHOICES = [
        (EV_DEPOSIT, EV_DEPOSIT),
        (EV_WITHDRAW, EV_WITHDRAW),
        (EV_COMMISSION, EV_COMMISSION),
        (EV_REALIZED, EV_REALIZED),
        (EV_FEE, EV_FEE),
        (EV_ADJUST, EV_ADJUST),
    ]

    account = models.ForeignKey(TradingAccount, on_delete=models.CASCADE, related_name='ledger')
    event_type = models.CharField(max_length=16, choices=EVENT_CHOICES)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    balance_after = models.DecimalField(max_digits=18, decimal_places=2)
    meta = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            # Covering index for daily PnL aggregate (account + event_type + range on created_at)
            models.Index(fields=["account", "event_type", "created_at"], name="ledger_acc_type_ts_idx"),
            # Standalone index for broker-wide date-range scans in exposure_engine
            models.Index(fields=["created_at"], name="ledger_created_at_idx"),
        ]

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {self.account_id} {self.event_type} {self.amount}"


class Trade(models.Model):
    """Ejecuciones (fills)."""
    BUY = 'BUY'
    SELL = 'SELL'
    SIDE_CHOICES = [(BUY, BUY), (SELL, SELL)]

    account = models.ForeignKey(TradingAccount, on_delete=models.CASCADE)
    symbol = models.CharField(max_length=12, default='EUR/USD')
    trade_type = models.CharField(max_length=4, choices=SIDE_CHOICES)
    lot_size = models.DecimalField(max_digits=10, decimal_places=2)

    entry_price = models.DecimalField(max_digits=18, decimal_places=6)
    exit_price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    stop_loss = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    profit_loss = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "closed_at"], name="trade_acc_closed_idx"),
        ]

    def __str__(self):
        return f"{self.trade_type} {self.symbol} — {self.lot_size} lotes"


class BrokerLedger(models.Model):
    """Broker-side revenue ledger. Additive only — records are never modified or deleted."""

    REV_COMMISSION   = 'COMMISSION'
    REV_SPREAD       = 'SPREAD'
    REV_CHALLENGE_FEE = 'CHALLENGE_FEE'
    REV_WITHDRAW_FEE = 'WITHDRAW_FEE'
    REV_ADJUSTMENT   = 'ADJUSTMENT'

    REVENUE_CHOICES = [
        (REV_COMMISSION,    'Commission'),
        (REV_SPREAD,        'Spread'),
        (REV_CHALLENGE_FEE, 'Challenge Fee'),
        (REV_WITHDRAW_FEE,  'Withdrawal Fee'),
        (REV_ADJUSTMENT,    'Adjustment'),
    ]

    revenue_type   = models.CharField(max_length=16, choices=REVENUE_CHOICES, db_index=True)
    amount         = models.DecimalField(max_digits=18, decimal_places=2)
    source_account = models.ForeignKey(
        TradingAccount, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='broker_ledger',
    )
    source_trade   = models.ForeignKey(
        'Trade', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='broker_ledger',
    )
    source_ledger  = models.ForeignKey(
        LedgerEntry, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='broker_ledger',
    )
    symbol         = models.CharField(max_length=12, null=True, blank=True)
    meta           = models.JSONField(default=dict, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {self.revenue_type} {self.amount}"


class BrokerSpreadConfig(models.Model):
    """Per-symbol broker spread configuration. Controls the price markup seen by clients."""
    symbol      = models.CharField(max_length=12, unique=True, db_index=True)
    spread_pips = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("1.00"),
        help_text="Additional markup per side in pips (1 pip = spec.pip_size for the symbol).",
    )
    is_dynamic  = models.BooleanField(
        default=False,
        help_text="Reserved for future dynamic/session spread logic.",
    )
    min_spread  = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.50"))
    max_spread  = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("5.00"))
    enabled     = models.BooleanField(default=True, db_index=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["symbol"]

    def save(self, *args, **kwargs):
        from market_data.symbol_specs import normalize_symbol
        self.symbol = normalize_symbol(self.symbol)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.symbol} spread={self.spread_pips}pip enabled={self.enabled}"


class Deposit(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_WAITING = 'waiting'
    STATUS_CONFIRMING = 'confirming'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_SENDING = 'sending'
    STATUS_PARTIALLY_PAID = 'partially_paid'
    STATUS_FINISHED = 'finished'
    STATUS_FAILED = 'failed'
    STATUS_REFUNDED = 'refunded'
    STATUS_EXPIRED = 'expired'

    CREDITED_STATUSES = {STATUS_FINISHED, STATUS_CONFIRMED}

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pendiente'),
        (STATUS_WAITING, 'Esperando pago'),
        (STATUS_CONFIRMING, 'Confirmando'),
        (STATUS_CONFIRMED, 'Confirmado'),
        (STATUS_SENDING, 'Enviando'),
        (STATUS_PARTIALLY_PAID, 'Pago parcial'),
        (STATUS_FINISHED, 'Completado'),
        (STATUS_FAILED, 'Fallido'),
        (STATUS_REFUNDED, 'Reembolsado'),
        (STATUS_EXPIRED, 'Expirado'),
    ]

    from .currencies import CRYPTO_CHOICES  # single source of truth in currencies.py

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deposits')
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    crypto_currency = models.CharField(max_length=20, choices=CRYPTO_CHOICES)
    nowpayments_payment_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    nowpayments_invoice_url = models.URLField(max_length=512, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    # Payment details returned by NowPayments on creation
    pay_address  = models.CharField(max_length=128, null=True, blank=True)
    pay_amount   = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    expires_at   = models.DateTimeField(null=True, blank=True)

    # Idempotency gate — set to True atomically with the wallet credit.
    # Once True, NO subsequent callback, retry, or replay can credit funds again.
    # Only an admin correction can reset this (with a corresponding reversal ledger entry).
    credited              = models.BooleanField(default=False, db_index=True)
    credited_at           = models.DateTimeField(null=True, blank=True)
    confirmed_amount_usd  = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    # Set when the Deposit is for purchasing a ChallengeProduct (not a wallet top-up).
    # Null = regular wallet deposit; non-null = challenge purchase.
    # The callback bifurcates on this field: wallet credit vs enrollment creation.
    challenge_product = models.ForeignKey(
        'ChallengeProduct',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='deposits',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} — ${self.amount_usd} {self.crypto_currency} [{self.status}]"


class WithdrawalRequest(models.Model):
    """
    User-initiated crypto withdrawal.

    Lifecycle:
      pending    — created; wallet already debited (funds reserved)
      approved   — admin approved, NP payout submitted
      processing — NP confirms the batch is in flight
      completed  — NP payout finished; crypto delivered to user
      rejected   — admin rejected; wallet refunded via TX_CORRECTION
      failed     — NP payout failed; wallet refunded automatically

    The wallet debit happens ATOMICALLY with the creation of this row.
    Rejection / failure credits back via credit_wallet(TX_CORRECTION).
    """

    STATUS_PENDING    = "pending"
    STATUS_APPROVED   = "approved"
    STATUS_REJECTED   = "rejected"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED  = "completed"
    STATUS_FAILED     = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING,    "Pending"),
        (STATUS_APPROVED,   "Approved"),
        (STATUS_REJECTED,   "Rejected"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED,  "Completed"),
        (STATUS_FAILED,     "Failed"),
    ]

    from .currencies import WITHDRAWAL_CHOICES

    user            = models.ForeignKey(User, on_delete=models.CASCADE, related_name="withdrawals")
    amount_usd      = models.DecimalField(max_digits=12, decimal_places=2)
    crypto_currency = models.CharField(max_length=20, choices=WITHDRAWAL_CHOICES)
    wallet_address  = models.CharField(max_length=200)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    # NowPayments payout references
    np_batch_id      = models.CharField(max_length=100, blank=True, default="", db_index=True)
    np_payout_id     = models.CharField(max_length=100, blank=True, default="", db_index=True)
    np_payout_status = models.CharField(max_length=40,  blank=True, default="")
    crypto_amount    = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)

    # Admin review
    admin_note  = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_withdrawals",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # Wallet ledger reference (the TX_WITHDRAW row created at request time)
    debit_tx = models.OneToOneField(
        "WalletTransaction", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="withdrawal_request",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"], name="wr_user_status_idx"),
            models.Index(fields=["created_at"],     name="wr_created_at_idx"),
        ]

    def __str__(self):
        return f"Withdrawal #{self.id} {self.user} ${self.amount_usd} {self.crypto_currency} [{self.status}]"


class Purchase(models.Model):
    """
    Código de compra del challenge para validar acceso/creación de cuenta.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    code = models.CharField(max_length=64, unique=True)
    tier = models.CharField(
        max_length=4,
        choices=TradingAccount.ACCOUNT_TIERS,
        help_text="El plan que el usuario compró"
    )
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        estado = "usado" if self.used else "nuevo"
        return f"{self.user} – {self.code} ({estado})"


# ─────────────────────────────────────────────
# Product catalog (Phase 4B)
# ─────────────────────────────────────────────

class AccountProduct(models.Model):
    """
    Catalog entry for standard (non-challenge) trading accounts.
    Completely separate from ChallengeProduct — challenges are NOT sold via this model.
    Created by admin; referenced when opening DEMO/RETAIL/ECN/STANDARD/CRYPTO accounts.

    family:       user-facing grouping — DEMO or REAL.
    product_type: internal TradingAccount.account_type to create (DEMO/RETAIL/ECN/STANDARD/CRYPTO).
    code:         unique slug used by the creation flow and seed command.
    """
    TYPE_DEMO     = 'DEMO'
    TYPE_RETAIL   = 'RETAIL'
    TYPE_ECN      = 'ECN'
    TYPE_STANDARD = 'STANDARD'
    TYPE_CRYPTO   = 'CRYPTO'

    PRODUCT_TYPES = [
        (TYPE_DEMO,     'Demo'),
        (TYPE_RETAIL,   'Retail / USDT'),
        (TYPE_ECN,      'ECN'),
        (TYPE_STANDARD, 'Standard'),
        (TYPE_CRYPTO,   'Crypto'),
    ]

    FAMILY_DEMO = 'DEMO'
    FAMILY_REAL = 'REAL'
    FAMILY_CHOICES = [
        (FAMILY_DEMO, 'Demo'),
        (FAMILY_REAL, 'Real'),
    ]

    # ── Identity ───────────────────────────────────────────────────────────────
    name           = models.CharField(max_length=64)
    code           = models.CharField(max_length=32, unique=True, null=True, blank=True, db_index=True)
    product_type   = models.CharField(max_length=12, choices=PRODUCT_TYPES)
    family         = models.CharField(max_length=4, choices=FAMILY_CHOICES, default=FAMILY_REAL)
    platform_label = models.CharField(max_length=64, default='Money Broker')
    description    = models.TextField(blank=True)

    # ── Economics ──────────────────────────────────────────────────────────────
    min_deposit          = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('100.00'))
    default_balance      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    max_leverage         = models.PositiveIntegerField(default=100)
    typical_spread_pips  = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal('0.00'))
    commission_per_lot   = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.00'))
    commission_pct       = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.0000'))
    spread_markup        = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.0000'))

    # ── Risk parameters (Phase 6B) ─────────────────────────────────────────────
    allowed_symbols   = models.JSONField(null=True, blank=True)
    max_lot_size      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    margin_call_level = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('100.00'))
    stopout_level     = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('50.00'))

    # ── Display ────────────────────────────────────────────────────────────────
    features    = models.JSONField(default=dict)
    is_popular  = models.BooleanField(default=False)
    sort_order  = models.PositiveIntegerField(default=0)
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['family', 'sort_order', 'name']
        verbose_name = "Account Product"

    def __str__(self):
        return f"{self.name} ({self.product_type})"


class ChallengeProduct(models.Model):
    """
    Catalog entry for prop-firm challenge programs (2-phase evaluation).
    Completely separate from AccountProduct — challenges have their own purchase flow.
    Challenges are NOT created from the normal New Account modal.

    Tiers here are independent of TradingAccount.ACCOUNT_TIERS so that 25K can exist
    without migrating TradingAccount.
    """
    TIER_10K  = '10K'
    TIER_25K  = '25K'
    TIER_50K  = '50K'
    TIER_100K = '100K'

    TIERS = [
        (TIER_10K,  '$10,000'),
        (TIER_25K,  '$25,000'),
        (TIER_50K,  '$50,000'),
        (TIER_100K, '$100,000'),
    ]

    name          = models.CharField(max_length=64)
    # Stable slug used by external sales platforms to reference this product.
    # e.g. "challenge_100k", "challenge_10k_v2". Null = not sold externally.
    external_code = models.CharField(max_length=64, unique=True, null=True, blank=True, db_index=True)
    tier         = models.CharField(max_length=6, choices=TIERS)
    price_usd    = models.DecimalField(max_digits=10, decimal_places=2)
    account_size = models.DecimalField(max_digits=12, decimal_places=2)  # virtual capital assigned

    # Phase 1 evaluation rules
    p1_profit_target_pct  = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('8.00'))
    p1_max_drawdown_pct   = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10.00'))
    p1_max_daily_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('5.00'))
    p1_min_trading_days   = models.PositiveIntegerField(default=5)
    p1_max_duration_days  = models.PositiveIntegerField(default=30)

    # Phase 2 evaluation rules
    p2_profit_target_pct  = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('5.00'))
    p2_max_drawdown_pct   = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10.00'))
    p2_max_daily_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('5.00'))
    p2_min_trading_days   = models.PositiveIntegerField(default=5)
    p2_max_duration_days  = models.PositiveIntegerField(default=60)

    # Funded account terms (copied to FundedConfig at promotion time)
    profit_split_pct   = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('80.00'))
    max_lot_size       = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('5.00'))
    max_open_positions = models.PositiveIntegerField(default=30)

    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['tier', 'price_usd']
        verbose_name = "Challenge Product"

    def __str__(self):
        return f"{self.name} ({self.tier}) — ${self.price_usd}"

    def p1_profit_target_amount(self) -> Decimal:
        """Absolute profit target in USD for Phase 1."""
        return (self.account_size * self.p1_profit_target_pct / Decimal('100')).quantize(Decimal('0.01'))

    def p2_profit_target_amount(self) -> Decimal:
        """Absolute profit target in USD for Phase 2."""
        return (self.account_size * self.p2_profit_target_pct / Decimal('100')).quantize(Decimal('0.01'))


class ChallengeEnrollment(models.Model):
    """
    Connects a verified payment (Deposit) to the sequence of TradingAccounts
    created across Phase 1 → Phase 2 → Funded.

    One enrollment per paid challenge. Each phase creates a new TradingAccount
    (account_type='CHALLENGE' for phases, 'FUNDED' for funded stage); the
    previous phase account is marked Completado when the next phase starts.

    deposit=None is allowed for admin-issued enrollments (no payment required).
    The UniqueConstraint on deposit prevents double-enrollment from the same payment.
    """
    ST_PHASE_1   = 'PHASE_1'
    ST_PHASE_2   = 'PHASE_2'
    ST_FUNDED    = 'FUNDED'
    ST_FAILED    = 'FAILED'
    ST_WITHDRAWN = 'WITHDRAWN'

    STATUS_CHOICES = [
        (ST_PHASE_1,   'Phase 1 — Active'),
        (ST_PHASE_2,   'Phase 2 — Active'),
        (ST_FUNDED,    'Funded'),
        (ST_FAILED,    'Failed'),
        (ST_WITHDRAWN, 'Withdrawn'),
    ]

    FAILED_AT_PHASE_1 = 'PHASE_1'
    FAILED_AT_PHASE_2 = 'PHASE_2'

    user    = models.ForeignKey(User, on_delete=models.CASCADE, related_name='challenge_enrollments')
    product = models.ForeignKey(ChallengeProduct, on_delete=models.PROTECT, related_name='enrollments')
    deposit = models.ForeignKey(
        'Deposit', on_delete=models.PROTECT,
        null=True, blank=True, related_name='challenge_enrollments',
    )

    # One TradingAccount per phase — null until that phase is reached
    phase1_account = models.OneToOneField(
        TradingAccount, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='enrollment_phase1',
    )
    phase2_account = models.OneToOneField(
        TradingAccount, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='enrollment_phase2',
    )
    funded_account = models.OneToOneField(
        TradingAccount, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='enrollment_funded',
    )

    status          = models.CharField(max_length=12, choices=STATUS_CHOICES, default=ST_PHASE_1)
    failed_at_phase = models.CharField(max_length=8, null=True, blank=True)
    failure_reason  = models.CharField(max_length=256, null=True, blank=True)

    # External webhook idempotency — set when enrollment originates from an external platform.
    # event_id: unique per webhook delivery (use for strict dedup).
    # external_payment_id: payment reference from the external platform.
    external_event_id   = models.CharField(max_length=128, unique=True, null=True, blank=True, db_index=True)
    external_payment_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)

    enrolled_at      = models.DateTimeField(auto_now_add=True)
    phase1_passed_at = models.DateTimeField(null=True, blank=True)
    phase2_passed_at = models.DateTimeField(null=True, blank=True)
    funded_at        = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-enrolled_at']
        indexes = [
            models.Index(fields=['user', 'status'], name='enrollment_user_status_idx'),
        ]
        constraints = [
            # One enrollment per paid deposit; multiple admin-issued (deposit=None) are allowed.
            models.UniqueConstraint(
                fields=['deposit'],
                condition=models.Q(deposit__isnull=False),
                name='unique_enrollment_per_deposit',
            ),
        ]
        verbose_name = "Challenge Enrollment"

    def __str__(self):
        return f"Enrollment #{self.pk} {self.user} {self.product.tier} [{self.status}]"

    @property
    def active_account(self):
        """Returns the TradingAccount currently active in this enrollment."""
        if self.status == self.ST_PHASE_1:
            return self.phase1_account
        if self.status == self.ST_PHASE_2:
            return self.phase2_account
        if self.status == self.ST_FUNDED:
            return self.funded_account
        return None


class FundedConfig(models.Model):
    """
    Payout rules and classification for the funded account produced after passing Phase 2.

    FUNDED_SIM:      virtual capital — simulated payouts, internal simulation only.
    FUNDED_INTERNAL: firm allocates internal capital; real payout process begins.

    All monetary terms are copied from ChallengeProduct at promotion time so that
    future product changes never retroactively affect existing funded traders.
    """
    FUNDED_SIM      = 'FUNDED_SIM'
    FUNDED_INTERNAL = 'FUNDED_INTERNAL'

    FUNDED_TYPES = [
        (FUNDED_SIM,      'Simulation Funded — virtual capital'),
        (FUNDED_INTERNAL, 'Internal Funded — firm capital assigned'),
    ]

    enrollment  = models.OneToOneField(
        ChallengeEnrollment, on_delete=models.CASCADE, related_name='funded_config',
    )
    funded_type = models.CharField(max_length=16, choices=FUNDED_TYPES, default=FUNDED_SIM)

    # Snapshot of product terms at the moment of funding — immutable after creation
    profit_split_pct         = models.DecimalField(max_digits=5,  decimal_places=2)
    min_payout_usd           = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('50.00'))
    min_trading_days         = models.PositiveIntegerField(default=5)
    payout_cycle_days        = models.PositiveIntegerField(default=14)
    max_monthly_drawdown_pct = models.DecimalField(max_digits=5,  decimal_places=2, default=Decimal('5.00'))

    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Funded Account Config"

    def __str__(self):
        return f"FundedConfig #{self.enrollment_id} {self.funded_type} split={self.profit_split_pct}%"


# ─────────────────────────────────────────────
# Wallet ledger models
# ─────────────────────────────────────────────

class InternalTransfer(models.Model):
    """
    Records every movement of funds between a Wallet and a TradingAccount.

    Lifecycle:
      PENDING    — created, locks have not been applied yet
      PROCESSING — atomic section started (select_for_update held)
      COMPLETED  — both wallet debit/credit and account credit/debit committed
      FAILED     — atomic section failed; balances untouched
      REVERSED   — a COMPLETED transfer was administratively reversed

    The matching WalletTransaction row(s) reference this record via FK.
    """
    DIR_TO_ACCOUNT = "WALLET_TO_ACCOUNT"
    DIR_TO_WALLET  = "ACCOUNT_TO_WALLET"
    DIRECTION_CHOICES = [
        (DIR_TO_ACCOUNT, "Wallet → Trading Account"),
        (DIR_TO_WALLET,  "Trading Account → Wallet"),
    ]

    ST_PENDING    = "PENDING"
    ST_PROCESSING = "PROCESSING"
    ST_COMPLETED  = "COMPLETED"
    ST_FAILED     = "FAILED"
    ST_REVERSED   = "REVERSED"
    STATUS_CHOICES = [
        (ST_PENDING,    "Pending"),
        (ST_PROCESSING, "Processing"),
        (ST_COMPLETED,  "Completed"),
        (ST_FAILED,     "Failed"),
        (ST_REVERSED,   "Reversed"),
    ]

    wallet          = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="transfers")
    trading_account = models.ForeignKey(TradingAccount, on_delete=models.PROTECT, related_name="wallet_transfers")
    direction       = models.CharField(max_length=20, choices=DIRECTION_CHOICES)
    amount          = models.DecimalField(max_digits=18, decimal_places=2)
    status          = models.CharField(max_length=12, choices=STATUS_CHOICES, default=ST_PENDING)
    note            = models.TextField(null=True, blank=True)
    initiated_by    = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="initiated_transfers"
    )
    failure_reason  = models.CharField(max_length=256, null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)
    completed_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["wallet", "status"],          name="itx_wallet_status_idx"),
            models.Index(fields=["trading_account", "status"], name="itx_acc_status_idx"),
            models.Index(fields=["created_at"],                name="itx_created_at_idx"),
        ]

    def __str__(self):
        return f"InternalTransfer #{self.id} {self.direction} ${self.amount} [{self.status}]"


class WalletTransaction(models.Model):
    """
    Append-only ledger for Wallet.available_balance.

    INVARIANT (verified by reconcile_wallet()):
        SUM(WalletTransaction.amount WHERE wallet=W) == W.available_balance

    amount sign convention:
        positive (+) = credit  (DEPOSIT, TRANSFER_IN, BONUS, REBATE, CORRECTION+)
        negative (-) = debit   (WITHDRAW, TRANSFER_OUT, COMMISSION, CORRECTION-)

    NEVER modify Wallet.available_balance directly.
    ALWAYS go through wallet_ledger.credit_wallet() or wallet_ledger.debit_wallet().
    """
    TX_DEPOSIT      = "DEPOSIT"       # + depósito crypto confirmado
    TX_WITHDRAW     = "WITHDRAW"      # - retiro al usuario
    TX_TRANSFER_OUT = "TRANSFER_OUT"  # - wallet → trading account
    TX_TRANSFER_IN  = "TRANSFER_IN"   # + trading account → wallet
    TX_BONUS        = "BONUS"         # + bono promocional
    TX_REBATE       = "REBATE"        # + rebate / cashback
    TX_COMMISSION   = "COMMISSION"    # - comisión de plataforma
    TX_CORRECTION   = "CORRECTION"    # ± corrección admin (firmada)

    TX_CHOICES = [
        (TX_DEPOSIT,      "Deposit"),
        (TX_WITHDRAW,     "Withdraw"),
        (TX_TRANSFER_OUT, "Transfer Out (→ Account)"),
        (TX_TRANSFER_IN,  "Transfer In (← Account)"),
        (TX_BONUS,        "Bonus"),
        (TX_REBATE,       "Rebate"),
        (TX_COMMISSION,   "Commission"),
        (TX_CORRECTION,   "Correction"),
    ]

    wallet            = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="transactions")
    tx_type           = models.CharField(max_length=20, choices=TX_CHOICES)
    amount            = models.DecimalField(max_digits=18, decimal_places=2)   # signed
    balance_after     = models.DecimalField(max_digits=18, decimal_places=2)   # snapshot post-tx

    # At most one reference is set per transaction
    deposit           = models.ForeignKey(
        Deposit, null=True, blank=True, on_delete=models.SET_NULL, related_name="wallet_txs"
    )
    internal_transfer = models.ForeignKey(
        InternalTransfer, null=True, blank=True, on_delete=models.SET_NULL, related_name="wallet_txs"
    )

    initiated_by      = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="wallet_transactions"
    )
    note              = models.TextField(null=True, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["wallet", "created_at"], name="wtx_wallet_ts_idx"),
            models.Index(fields=["tx_type", "created_at"], name="wtx_type_ts_idx"),
        ]

    def __str__(self):
        sign = "+" if self.amount >= 0 else ""
        return f"WalletTx #{self.id} {self.tx_type} {sign}{self.amount} → bal={self.balance_after}"


# ─────────────────────────────────────────────
# Risk Engine models
# ─────────────────────────────────────────────

class RiskRule(models.Model):
    """Per-account risk limits. Auto-created with tier defaults if missing."""
    account = models.OneToOneField(
        TradingAccount, on_delete=models.CASCADE, related_name="risk_rule"
    )
    max_daily_loss_pct = models.DecimalField(max_digits=5, decimal_places=2, default=5.00)
    max_drawdown_pct   = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)
    max_lot_size       = models.DecimalField(max_digits=8, decimal_places=2, default=5.00)
    max_open_positions = models.PositiveIntegerField(default=10)
    max_exposure_usd   = models.DecimalField(max_digits=12, decimal_places=2, default=5000.00)
    consistency_min_trades = models.PositiveIntegerField(default=5)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"RiskRule #{self.account_id} dd={self.max_drawdown_pct}% daily={self.max_daily_loss_pct}%"


class DrawdownSnapshot(models.Model):
    """Daily balance + drawdown snapshot. One row per account per calendar day."""
    account          = models.ForeignKey(TradingAccount, on_delete=models.CASCADE, related_name="dd_snapshots")
    date             = models.DateField()
    balance_start    = models.DecimalField(max_digits=12, decimal_places=2)
    balance_end      = models.DecimalField(max_digits=12, decimal_places=2)
    daily_pnl        = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    daily_pnl_pct    = models.DecimalField(max_digits=7,  decimal_places=2, default=0)
    peak_balance     = models.DecimalField(max_digits=12, decimal_places=2)
    drawdown_from_peak = models.DecimalField(max_digits=7, decimal_places=2, default=0)

    class Meta:
        unique_together = [("account", "date")]
        indexes = [models.Index(fields=["account", "date"], name="dd_snap_acc_date_idx")]
        ordering = ["-date"]

    def __str__(self):
        return f"DD #{self.account_id} {self.date} pnl={self.daily_pnl}"


class TradingViolation(models.Model):
    """Recorded whenever a risk rule is breached."""
    MAX_DRAWDOWN   = "MAX_DRAWDOWN"
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    MAX_LOT_SIZE   = "MAX_LOT_SIZE"
    MAX_EXPOSURE   = "MAX_EXPOSURE"
    RATE_LIMITED   = "RATE_LIMITED"
    MARTINGALE     = "MARTINGALE_PATTERN"

    VIOLATION_CHOICES = [
        (MAX_DRAWDOWN,   "Max Drawdown"),
        (MAX_DAILY_LOSS, "Max Daily Loss"),
        (MAX_LOT_SIZE,   "Max Lot Size"),
        (MAX_EXPOSURE,   "Max Exposure"),
        (RATE_LIMITED,   "Rate Limited"),
        (MARTINGALE,     "Martingale Pattern"),
    ]

    account            = models.ForeignKey(TradingAccount, on_delete=models.CASCADE, related_name="violations")
    violation_type     = models.CharField(max_length=24, choices=VIOLATION_CHOICES)
    value_at_violation = models.DecimalField(max_digits=12, decimal_places=4)
    limit_value        = models.DecimalField(max_digits=12, decimal_places=4)
    meta               = models.JSONField(null=True, blank=True)
    created_at         = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["account", "created_at"], name="violation_acc_ts_idx")]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Violation #{self.account_id} {self.violation_type} {self.value_at_violation}"


class TraderScore(models.Model):
    """Current trader classification + behavioral metrics. Updated after each trade close."""
    NORMAL      = "NORMAL"
    RISKY       = "RISKY"
    MARTINGALE  = "MARTINGALE"
    TOXIC       = "TOXIC"
    CONSISTENT  = "CONSISTENT"
    ELITE       = "ELITE"
    GAMBLER     = "GAMBLER"
    SCALPER     = "SCALPER"

    CLASS_CHOICES = [
        (NORMAL,     "Normal"),
        (RISKY,      "Risky"),
        (MARTINGALE, "Martingale"),
        (TOXIC,      "Toxic"),
        (CONSISTENT, "Consistent"),
        (ELITE,      "Elite"),
        (GAMBLER,    "Gambler"),
        (SCALPER,    "Scalper"),
    ]

    ROUTING_INTERNAL        = "INTERNAL"
    ROUTING_REVIEW          = "REVIEW"
    ROUTING_HEDGE_CANDIDATE = "HEDGE_CANDIDATE"
    ROUTING_ELITE           = "ELITE"

    ROUTING_CHOICES = [
        (ROUTING_INTERNAL,        "Internal"),
        (ROUTING_REVIEW,          "Review"),
        (ROUTING_HEDGE_CANDIDATE, "Hedge Candidate"),
        (ROUTING_ELITE,           "Elite"),
    ]

    account           = models.OneToOneField(TradingAccount, on_delete=models.CASCADE, related_name="trader_score")

    # Classification
    trader_class      = models.CharField(max_length=12, choices=CLASS_CHOICES, default=NORMAL)
    routing_profile   = models.CharField(max_length=20, choices=ROUTING_CHOICES, default=ROUTING_INTERNAL)

    # Basic performance
    win_rate          = models.DecimalField(max_digits=5,  decimal_places=2, default=0)
    profit_factor     = models.DecimalField(max_digits=8,  decimal_places=2, default=0)
    avg_lot_size      = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    consistency_score = models.DecimalField(max_digits=5,  decimal_places=2, default=0)
    avg_rr            = models.DecimalField(max_digits=8,  decimal_places=3, default=0)
    pnl_volatility    = models.DecimalField(max_digits=8,  decimal_places=3, default=0)

    # Behavioral signals
    martingale_rate        = models.DecimalField(max_digits=5, decimal_places=3, default=0)
    lot_growth_rate        = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    scalping_ratio         = models.DecimalField(max_digits=5, decimal_places=3, default=0)
    avg_hold_time_seconds  = models.DecimalField(max_digits=10, decimal_places=1, default=0)
    toxicity_score         = models.DecimalField(max_digits=5,  decimal_places=2, default=0)
    gambler_score          = models.DecimalField(max_digits=5,  decimal_places=2, default=0)
    trade_frequency        = models.DecimalField(max_digits=8,  decimal_places=2, default=0)
    max_consecutive_losses = models.PositiveIntegerField(default=0)
    max_consecutive_wins   = models.PositiveIntegerField(default=0)

    last_evaluated    = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Score #{self.account_id} {self.trader_class} win={self.win_rate}%"


# ─────────────────────────────────────────────
# Exposure / Dealer Analytics models
# ─────────────────────────────────────────────

class BrokerSnapshot(models.Model):
    """Point-in-time broker-wide exposure analytics. Created on demand or periodically."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Global exposure
    total_accounts           = models.PositiveIntegerField(default=0)
    total_open_positions     = models.PositiveIntegerField(default=0)
    total_long_usd           = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_short_usd          = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_exposure_usd         = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_unrealized_pnl     = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_realized_pnl_today = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Routing-profile breakdown
    internal_exposure_usd    = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    review_exposure_usd      = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    hedge_candidate_usd      = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Broker's simulated P&L (counter-party to INTERNAL traders)
    broker_pnl_unrealized    = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    broker_pnl_today         = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Risk flags as JSON list of {type, severity, msg}
    risk_flags               = models.JSONField(default=list)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Broker Snapshot"
        verbose_name_plural = "Broker Snapshots"

    def __str__(self):
        return f"Snapshot {self.created_at:%Y-%m-%d %H:%M} | net=${self.net_exposure_usd}"


class SymbolExposure(models.Model):
    """Per-symbol exposure breakdown within a BrokerSnapshot."""

    snapshot          = models.ForeignKey(
        BrokerSnapshot, on_delete=models.CASCADE, related_name="symbol_exposures"
    )
    symbol            = models.CharField(max_length=12)
    long_qty          = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    short_qty         = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    net_qty           = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    long_usd          = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    short_usd         = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_usd           = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    trader_count      = models.PositiveIntegerField(default=0)
    concentration_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    unrealized_pnl    = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    current_price     = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    is_high_risk      = models.BooleanField(default=False)

    class Meta:
        unique_together = [("snapshot", "symbol")]
        ordering = ["-concentration_pct"]

    def __str__(self):
        return f"{self.symbol} net=${self.net_usd}"


class TraderClassExposure(models.Model):
    """Per-trader-class exposure breakdown within a BrokerSnapshot."""

    snapshot         = models.ForeignKey(
        BrokerSnapshot, on_delete=models.CASCADE, related_name="class_exposures"
    )
    trader_class     = models.CharField(max_length=12)
    routing_profile  = models.CharField(max_length=20, default="INTERNAL")
    account_count    = models.PositiveIntegerField(default=0)
    long_usd         = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    short_usd        = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_usd          = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    unrealized_pnl   = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        unique_together = [("snapshot", "trader_class")]
        ordering = ["trader_class"]

    def __str__(self):
        return f"{self.trader_class} ({self.account_count} accts) net=${self.net_usd}"


# ─────────────────────────────────────────────
# Equity / Financial time-series snapshots
# Separate from BrokerSnapshot (dealer analytics).
# These are lightweight minute-by-minute financial state rows
# written by the Celery snapshot task and kept for SNAPSHOT_RETENTION_DAYS.
# ─────────────────────────────────────────────

class BrokerEquitySnapshot(models.Model):
    """
    Broker-wide financial state at a point in time.
    Written every minute by simulator.take_snapshots task.
    Pruned by simulator.cleanup_snapshots after SNAPSHOT_RETENTION_DAYS.
    """
    taken_at          = models.DateTimeField(db_index=True)
    active_accounts   = models.PositiveIntegerField(default=0)
    open_positions    = models.PositiveIntegerField(default=0)
    total_balance     = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_equity      = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    floating_pnl      = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_margin_used = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_free_margin = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    gross_long_usd    = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    gross_short_usd   = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    net_exposure_usd  = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    class Meta:
        indexes = [models.Index(fields=["taken_at"], name="brokerequitysnap_ts_idx")]
        ordering = ["-taken_at"]
        verbose_name = "Broker Equity Snapshot"

    def __str__(self):
        return f"BrokerEquitySnap {self.taken_at:%Y-%m-%d %H:%M} equity={self.total_equity}"


class AccountEquitySnapshot(models.Model):
    """
    Per-account financial state at a point in time.
    Written every minute (alongside BrokerEquitySnapshot) for every active account.
    Pruned alongside BrokerEquitySnapshot.
    """
    account        = models.ForeignKey(
        TradingAccount, on_delete=models.CASCADE, related_name="equity_snapshots"
    )
    taken_at       = models.DateTimeField()
    balance        = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    equity         = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    floating_pnl   = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    margin_used    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    free_margin    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    drawdown       = models.DecimalField(max_digits=7,  decimal_places=2, default=Decimal("0"))
    open_positions = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["account", "taken_at"], name="accequitysnap_acc_ts_idx"),
            models.Index(fields=["taken_at"],             name="accequitysnap_ts_idx"),
        ]
        ordering = ["-taken_at"]
        verbose_name = "Account Equity Snapshot"

    def __str__(self):
        return f"AccEquitySnap #{self.account_id} {self.taken_at:%Y-%m-%d %H:%M} eq={self.equity}"


class BrokerRevenueSnapshot(models.Model):
    """
    Point-in-time snapshot of cumulative broker revenue + operational state.
    Written every 5 minutes by simulator.take_revenue_snapshot task.
    Source of truth for equity-curve rendering and trend analytics.

    Design:
    - `total_*` fields are monotonically increasing cumulative sums (equity curve points).
    - `period_*` fields are the incremental delta since the PREVIOUS snapshot (trend rate).
    - Exposure fields are copied from the latest BrokerEquitySnapshot to avoid
      recomputing live analytics on every 5-min tick.
    - No JSON blobs — top-N breakdowns are always queried live from BrokerLedger
      (table stays small: ~1 row/trade).

    Retention: REVENUE_SNAPSHOT_RETENTION_DAYS (default 90).
    At 288 snapshots/day × 90 days = 25,920 rows max — trivial storage.

    Future cold-storage path: export rows older than N days to a data warehouse
    (S3 Parquet, BigQuery, Redshift) using the `taken_at` index as the cursor.
    The table never needs to grow beyond the configured retention window.
    """
    taken_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # ── Cumulative all-time totals ─────────────────────────────────────
    # These form the broker's revenue equity curve. Each row is one point.
    total_revenue    = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_commission = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_spread     = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_challenge  = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_withdraw   = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    total_adjustment = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    # ── Incremental delta since previous snapshot ─────────────────────
    # Used for trend rate and sparklines without arithmetic in templates.
    period_revenue    = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    period_commission = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    period_spread     = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    # ── Operational state at snapshot time ────────────────────────────
    active_accounts = models.PositiveIntegerField(default=0)
    open_positions  = models.PositiveIntegerField(default=0)

    # ── Lightweight exposure summary ──────────────────────────────────
    # Copied from latest BrokerEquitySnapshot — no recomputation needed.
    net_exposure_usd = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    gross_long_usd   = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    gross_short_usd  = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-taken_at"]
        indexes  = [models.Index(fields=["taken_at"], name="brokerrevsnap_ts_idx")]
        verbose_name = "Broker Revenue Snapshot"
        verbose_name_plural = "Broker Revenue Snapshots"

    def __str__(self):
        return f"RevSnap {self.taken_at:%Y-%m-%d %H:%M} total=${self.total_revenue}"


# ─────────────────────────────────────────────
# Audit Trail
# ─────────────────────────────────────────────

class TOTPDevice(models.Model):
    """
    One TOTP device per user. Secrets are stored encrypted (see two_factor.py).
    confirmed=False means setup started but QR not yet scanned and verified.
    Only confirmed=True devices are enforced.
    """
    user        = models.OneToOneField(User, on_delete=models.CASCADE, related_name="totp_device")
    secret      = models.CharField(max_length=512)   # encrypted; never raw base32 in prod
    confirmed   = models.BooleanField(default=False)
    created_at  = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user"], name="totpdevice_user_idx")]

    def __str__(self):
        return f"TOTPDevice(user={self.user_id}, confirmed={self.confirmed})"


class AuditLog(models.Model):
    """
    Append-only operational audit log.
    Records who did what, from where, and the before/after state for every
    sensitive action (deposits, withdrawals, account funding, admin actions).

    Rules:
    - NEVER update or delete rows (append-only)
    - NEVER call from model save() — only from views/tasks
    - All FK fields are nullable so log survives user/account deletion
    """
    # Event classification
    event_type  = models.CharField(max_length=80, db_index=True)   # e.g. "deposit.credited"
    action      = models.CharField(max_length=120)                  # human-readable label

    # Who
    user        = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    account     = models.ForeignKey(TradingAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    # Where / how
    ip          = models.GenericIPAddressField(null=True, blank=True)
    endpoint    = models.CharField(max_length=200, blank=True)
    method      = models.CharField(max_length=10, blank=True)
    request_id  = models.CharField(max_length=40, blank=True, db_index=True)

    # Payload — flexible JSON (before/after state, params, IDs)
    detail      = models.JSONField(default=dict, blank=True)

    created_at  = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "created_at"], name="auditlog_type_ts_idx"),
            models.Index(fields=["request_id"],               name="auditlog_reqid_idx"),
            models.Index(fields=["user", "created_at"],       name="auditlog_user_ts_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"AuditLog[{self.event_type}] user={self.user_id} {self.created_at:%Y-%m-%d %H:%M:%S}"


# ─────────────────────────────────────────────
# Broker Ecosystem Modules
# ─────────────────────────────────────────────

class CalendarEvent(models.Model):
    IMPACT_CHOICES = [
        ('LOW',    'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH',   'High'),
    ]
    title      = models.CharField(max_length=200)
    currency   = models.CharField(max_length=8)
    country    = models.CharField(max_length=60, blank=True)
    event_date = models.DateTimeField()
    impact     = models.CharField(max_length=10, choices=IMPACT_CHOICES, default='MEDIUM')
    actual     = models.CharField(max_length=40, blank=True)
    forecast   = models.CharField(max_length=40, blank=True)
    previous   = models.CharField(max_length=40, blank=True)
    published  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['event_date']
        indexes  = [models.Index(fields=['event_date', 'published'], name='calendar_date_pub_idx')]
        verbose_name        = 'Calendar Event'
        verbose_name_plural = 'Calendar Events'

    def __str__(self):
        return f"[{self.impact}] {self.title} {self.event_date:%Y-%m-%d %H:%M}"

    @property
    def is_past(self):
        return timezone.now() > self.event_date


class Referral(models.Model):
    user                 = models.OneToOneField(User, on_delete=models.CASCADE, related_name='referral')
    code                 = models.CharField(max_length=20, unique=True)
    clicks               = models.PositiveIntegerField(default=0)
    registrations        = models.PositiveIntegerField(default=0)
    estimated_commission = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    created_at           = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Referral / IB Link'

    def __str__(self):
        return f"Referral({self.user_id}) code={self.code}"


class Bonus(models.Model):
    BONUS_TYPES = [
        ('CREDIT',     'Bono de crédito'),
        ('PERCENTAGE', 'Porcentaje sobre depósito'),
        ('REBATE',     'Rebate / cashback'),
    ]
    title       = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    bonus_type  = models.CharField(max_length=20, choices=BONUS_TYPES, default='CREDIT')
    value       = models.DecimalField(max_digits=12, decimal_places=2)
    active      = models.BooleanField(default=True)
    expires_at  = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering            = ['-created_at']
        verbose_name        = 'Bonus'
        verbose_name_plural = 'Bonuses'

    def __str__(self):
        status = 'ON' if self.active else 'OFF'
        return f"[{status}] {self.title}"

    @property
    def is_expired(self):
        return bool(self.expires_at and timezone.now() > self.expires_at)


class BrokerDocument(models.Model):
    CATEGORIES = [
        ('CONTRACT',    'Contratos'),
        ('GUIDE',       'Guías'),
        ('CERTIFICATE', 'Certificados'),
        ('REPORT',      'Reportes'),
    ]
    title       = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    file        = models.FileField(upload_to='broker_documents/')
    category    = models.CharField(max_length=20, choices=CATEGORIES, default='GUIDE')
    public      = models.BooleanField(default=True, help_text="Visible to all logged-in users")
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering            = ['category', 'title']
        verbose_name        = 'Broker Document'
        verbose_name_plural = 'Broker Documents'

    def __str__(self):
        return f"[{self.category}] {self.title}"

    @property
    def filename(self):
        import os
        return os.path.basename(self.file.name) if self.file else ''

    @property
    def extension(self):
        name = self.filename
        return name.rsplit('.', 1)[-1].upper() if '.' in name else ''


class ExpertAdvisor(models.Model):
    EA_CATEGORIES = [
        ('TREND',      'Tendencia'),
        ('SCALPING',   'Scalping'),
        ('GRID',       'Grid'),
        ('HEDGING',    'Cobertura'),
        ('CUSTOM',     'Personalizado'),
    ]
    name         = models.CharField(max_length=120)
    description  = models.TextField(blank=True)
    category     = models.CharField(max_length=20, choices=EA_CATEGORIES, default='TREND')
    version      = models.CharField(max_length=20, blank=True)
    download_url = models.URLField(blank=True, help_text="External link or leave blank for future upload")
    active       = models.BooleanField(default=True)
    coming_soon  = models.BooleanField(default=False)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering            = ['category', 'name']
        verbose_name        = 'Expert Advisor'
        verbose_name_plural = 'Expert Advisors'

    def __str__(self):
        v = f" v{self.version}" if self.version else ""
        return f"{self.name}{v} [{self.category}]"
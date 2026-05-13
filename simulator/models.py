from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()


class TradingAccount(models.Model):
    # Nivel de cuenta (fondo)
    ACCOUNT_TIERS = [
        ('10K', 'Cuenta 10 000'),
        ('50K', 'Cuenta 50 000'),
        ('100K', 'Cuenta 100 000'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    tier = models.CharField(max_length=4, choices=ACCOUNT_TIERS, default='10K', help_text="Elige el plan de fondeo")

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
    profit_target = models.DecimalField(max_digits=12, decimal_places=2, default=800.00)
    max_drawdown = models.DecimalField(max_digits=12, decimal_places=2, default=1200.00)

    # Config cuenta
    currency = models.CharField(max_length=6, default='USD')  # presente hasta 0007
    leverage = models.PositiveIntegerField(default=50)
    netting_mode = models.BooleanField(
        default=True,
        help_text='True=Netting (consolidar por símbolo); False=Hedging (varias posiciones).'
    )

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

    def __str__(self):
        usr = getattr(self.user, 'username', self.user_id)
        return f"{usr} — {self.tier} — {self.phase}"

    # ------------------------------
    # 🆕 Validador de reglas de fondeo
    # ------------------------------
    def check_rules(self):
        from .models import LedgerEntry

        # ---- Profit Target ----
        if self.balance >= (self.profit_target + (self.equity or 0)):
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
        if self.drawdown >= self.max_drawdown or self.balance <= 0:
            self.status = 'Suspendido'
            LedgerEntry.objects.create(
                account=self,
                event_type=LedgerEntry.EV_ADJUST,
                amount=0,
                balance_after=self.balance,
                meta={"msg": "Cuenta suspendida por drawdown"},
            )

        # ---- Equity Check ----
        if self.equity <= 0:
            self.status = 'Completado'

    def save(self, *args, **kwargs):
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

    CRYPTO_CHOICES = [
        ('btc', 'Bitcoin (BTC)'),
        ('eth', 'Ethereum (ETH)'),
        ('usdttrc20', 'USDT TRC-20'),
        ('usdterc20', 'USDT ERC-20'),
        ('sol', 'Solana (SOL)'),
        ('bnbmainnet', 'BNB (BSC)'),
        ('usdcsol', 'USDC (Solana)'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='deposits')
    amount_usd = models.DecimalField(max_digits=12, decimal_places=2)
    crypto_currency = models.CharField(max_length=20, choices=CRYPTO_CHOICES)
    nowpayments_payment_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    nowpayments_invoice_url = models.URLField(max_length=512, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} — ${self.amount_usd} {self.crypto_currency} [{self.status}]"


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
        indexes = [models.Index(fields=["account", "date"])]
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
        indexes = [models.Index(fields=["account", "created_at"])]
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
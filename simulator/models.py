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

    status = models.CharField(
        max_length=20,
        choices=[('Activo', 'Activo'), ('Suspendido', 'Suspendido'), ('Completado', 'Completado')],
        default='Activo'
    )
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

    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.trade_type} {self.symbol} — {self.lot_size} lotes"


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
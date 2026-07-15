"""
simulator/tests/test_ledger_invariant_audit.py — Bloque E

Audita las invariantes financieras críticas del sistema:
  1. Commission: EV_COMMISSION coherente con account.balance y BrokerLedger REV_COMMISSION
  2. Realized PnL (consumer path): Trade.profit_loss == LedgerEntry.amount == delta de balance
  3. LedgerEntry secuencial: último balance_after == account.balance tras N operaciones
  4. WithdrawalRequest.debit_tx: vinculado y monto negativo exacto
  5. pending_balance: no negativo, exactamente drenado en ciclo confirming→finished
  6. Fondos conservados en round-trip wallet ↔ account (transfer_to_account + transfer_to_wallet)

Reglas:
  - Ningún test modifica lógica productiva.
  - Todos los amounts en Decimal. Float solo donde la API lo exige (realized_pnl, close_px).
  - _db_open_position_atomic y _db_close_position_atomic se invocan via async_to_sync.
  - _close_position_sync (daemon path) se invoca directamente (ya es síncrona).
"""
import json
from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.db.models import Sum
from django.test import TestCase

from simulator.models import (
    BrokerLedger, LedgerEntry, Position, Trade,
    Wallet, WalletTransaction, WithdrawalRequest,
)
from simulator.tasks import _close_position_sync
from simulator.wallet_ledger import (
    credit_wallet, debit_wallet, reconcile_wallet,
    transfer_to_account, transfer_to_wallet,
)

from .factories import make_account, make_deposit, make_position, make_user, make_wallet


# ─────────────────────────────────────────────────────────────────────────────
# Helpers locales
# ─────────────────────────────────────────────────────────────────────────────

def _make_consumer(account):
    """Crea un TradingConsumer mínimo para tests sincrónicos."""
    from market_data.feeds import get_feed_manager
    from simulator.consumers import TradingConsumer
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account.id
    c.account = {
        "status":       account.status,
        "peak_balance": float(account.peak_balance),
        "netting_mode": False,
        "spread_pips":  0.0,
    }
    c._feed = get_feed_manager()
    return c


def _pos_mem_from(pos: Position) -> dict:
    """Construye el dict pos_mem que esperan los métodos de cierre."""
    return {
        "id":        pos.id,
        "symbol":    pos.symbol,
        "side":      pos.side.lower(),
        "qty":       float(pos.qty),
        "avg":       float(pos.avg_price),
        "sl":        None,
        "tp":        None,
        "opened_at": pos.opened_at.timestamp(),
    }


DEPOSIT_CB_URL = "/deposit/callback/"
_PATCH_SIG_OK  = patch("simulator.views._np.verify_ipn_signature", return_value=True)


def _deposit_ipn(payment_id, status, deposit_pk, amount="100.00"):
    return json.dumps({
        "payment_id":     payment_id,
        "payment_status": status,
        "order_id":       str(deposit_pk),
        "price_amount":   amount,
    })


# ─────────────────────────────────────────────────────────────────────────────
# 1. Commission: coherencia EV_COMMISSION y BrokerLedger REV_COMMISSION
# ─────────────────────────────────────────────────────────────────────────────

class TestCommissionLedgerCoherence(TestCase):
    """
    _db_open_position_atomic debe crear LedgerEntry(EV_COMMISSION) y
    BrokerLedger(REV_COMMISSION) coherentes cuando commission > 0,
    y NO crearlos cuando commission == 0.
    """

    COMMISSION = 7.0   # float, como lo devuelve commission_for()

    def setUp(self):
        # PANEL-02 — balance bumped from 10 000 to 50 000 so the fixture's
        # 1.0-lot EUR/USD open stays under the atomic guard's 10% per-trade
        # margin cap (required_margin=$2200 → 4.4% of $50 000); the
        # commission/ledger invariants under test are unaffected.
        self.account  = make_account(balance=Decimal("50000"))
        self.consumer = _make_consumer(self.account)

    def _open(self, commission=None):
        if commission is None:
            commission = self.COMMISSION
        return async_to_sync(self.consumer._db_open_position_atomic)(
            symbol      = "EUR/USD",
            side        = "buy",
            qty         = 1.0,
            price       = 1.10000,
            sl          = None,
            tp          = None,
            commission  = commission,
            new_balance = float(self.account.balance) - commission,
        )

    def test_commission_creates_ev_commission_ledger_entry(self):
        """commission > 0 → exactamente un LedgerEntry EV_COMMISSION creado."""
        self._open()
        count = LedgerEntry.objects.filter(
            account=self.account, event_type=LedgerEntry.EV_COMMISSION
        ).count()
        self.assertEqual(count, 1)

    def test_commission_ledger_amount_is_negative(self):
        """LedgerEntry.amount == -commission (débito al trader = valor negativo)."""
        self._open()
        le = LedgerEntry.objects.get(
            account=self.account, event_type=LedgerEntry.EV_COMMISSION
        )
        self.assertEqual(le.amount, Decimal("-7.00"))

    def test_commission_ledger_balance_after_matches_account(self):
        """LedgerEntry.balance_after == TradingAccount.balance post-deducción."""
        self._open()
        le = LedgerEntry.objects.get(
            account=self.account, event_type=LedgerEntry.EV_COMMISSION
        )
        self.account.refresh_from_db()
        self.assertEqual(
            le.balance_after,
            self.account.balance,
            f"balance_after={le.balance_after} != account.balance={self.account.balance}",
        )

    def test_commission_creates_broker_ledger_rev_commission(self):
        """BrokerLedger REV_COMMISSION creado con monto positivo == commission."""
        self._open()
        bl = BrokerLedger.objects.filter(
            source_account=self.account,
            revenue_type=BrokerLedger.REV_COMMISSION,
        ).first()
        self.assertIsNotNone(bl, "BrokerLedger REV_COMMISSION debe existir")
        self.assertEqual(bl.amount, Decimal("7.00"))

    def test_zero_commission_no_ev_commission_entry(self):
        """commission == 0 → NO se crea LedgerEntry EV_COMMISSION."""
        self._open(commission=0.0)
        exists = LedgerEntry.objects.filter(
            account=self.account, event_type=LedgerEntry.EV_COMMISSION
        ).exists()
        self.assertFalse(
            exists, "commission=0 no debe generar LedgerEntry EV_COMMISSION"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Realized PnL — consumer path (_db_close_position_atomic)
# ─────────────────────────────────────────────────────────────────────────────

class TestRealizedPnLCoherenceConsumer(TestCase):
    """
    _db_close_position_atomic (ruta WS/consumer) debe mantener coherencia entre
    Trade.profit_loss, LedgerEntry(EV_REALIZED) y TradingAccount.balance.

    Esta ruta es distinta de _close_position_sync (daemon): ningún test anterior
    la auditaba a nivel de invariantes de ledger.
    """

    REALIZED_PNL = Decimal("100")

    def setUp(self):
        self.account     = make_account(balance=Decimal("10000"))
        self.pos         = make_position(
            account=self.account, symbol="EUR/USD",
            side="BUY", qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
        )
        self.consumer    = _make_consumer(self.account)
        self.new_balance = self.account.balance + self.REALIZED_PNL

    def _close(self):
        return async_to_sync(self.consumer._db_close_position_atomic)(
            pos_mem      = _pos_mem_from(self.pos),
            close_px     = 1.11000,
            reason       = "manual",
            realized_pnl = float(self.REALIZED_PNL),
            new_balance  = float(self.new_balance),
            new_equity   = float(self.new_balance),
        )

    def test_trade_profit_loss_equals_ledger_amount(self):
        """Trade.profit_loss == LedgerEntry(EV_REALIZED).amount — mismo cierre."""
        result = self._close()
        trade = Trade.objects.get(pk=result["trade_id"])
        le    = LedgerEntry.objects.get(
            account=self.account, event_type=LedgerEntry.EV_REALIZED
        )
        self.assertEqual(
            trade.profit_loss, le.amount,
            f"Trade.profit_loss={trade.profit_loss} != LedgerEntry.amount={le.amount}",
        )

    def test_ledger_balance_after_matches_account_balance(self):
        """LedgerEntry.balance_after == TradingAccount.balance post-cierre (consumer path)."""
        self._close()
        le = LedgerEntry.objects.get(
            account=self.account, event_type=LedgerEntry.EV_REALIZED
        )
        self.account.refresh_from_db()
        self.assertEqual(
            le.balance_after,
            self.account.balance,
            f"balance_after={le.balance_after} != account.balance={self.account.balance}",
        )

    def test_trade_profit_loss_is_decimal(self):
        """Trade.profit_loss almacenado en DB es Decimal, no float."""
        result = self._close()
        trade  = Trade.objects.get(pk=result["trade_id"])
        self.assertIsInstance(
            trade.profit_loss, Decimal,
            "Trade.profit_loss debe ser Decimal (no float) al leerlo de la DB",
        )

    def test_balance_conservation_realized_pnl(self):
        """balance_before + realized_pnl == account.balance post-cierre."""
        balance_before = self.account.balance
        self._close()
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.balance,
            balance_before + self.REALIZED_PNL,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. LedgerEntry sequential invariant
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerSequentialInvariant(TestCase):
    """
    Tras N operaciones financieras, el último LedgerEntry.balance_after
    debe coincidir con TradingAccount.balance,  y la suma de EV_REALIZED
    debe igualar el cambio neto de balance por PnL.
    """

    def test_last_entry_balance_after_equals_account_balance(self):
        """
        Secuencia: open (EV_COMMISSION) + close (EV_REALIZED).
        El último LedgerEntry.balance_after == account.balance.
        """
        # PANEL-02 — balance bumped from 10 000 to 50 000 (same reason as
        # TestCommissionLedgerCoherence.setUp above).
        account  = make_account(balance=Decimal("50000"))
        pos      = make_position(
            account=account, symbol="EUR/USD",
            side="BUY", qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
        )
        consumer = _make_consumer(account)

        # Paso 1: apertura con comisión → EV_COMMISSION
        async_to_sync(consumer._db_open_position_atomic)(
            symbol="EUR/USD", side="buy", qty=1.0,
            price=1.10000, sl=None, tp=None,
            commission=7.0, new_balance=49993.0,
        )

        # Paso 2: cierre con PnL → EV_REALIZED
        account.refresh_from_db()
        realized = Decimal("50")
        new_bal  = account.balance + realized
        async_to_sync(consumer._db_close_position_atomic)(
            pos_mem      = _pos_mem_from(pos),
            close_px     = 1.10500,
            reason       = "manual",
            realized_pnl = float(realized),
            new_balance  = float(new_bal),
            new_equity   = float(new_bal),
        )

        last_le = (
            LedgerEntry.objects
            .filter(account=account)
            .order_by("-id")
            .first()
        )
        account.refresh_from_db()
        self.assertIsNotNone(last_le)
        self.assertEqual(
            last_le.balance_after,
            account.balance,
            f"Último LedgerEntry.balance_after={last_le.balance_after} "
            f"!= account.balance={account.balance}",
        )

    def test_sum_realized_equals_pnl_net_change(self):
        """
        SUM(EV_REALIZED.amount) == cambio neto del balance atribuible a PnL.
        Dos cierres: +200 y -80 → net = +120.
        """
        account        = make_account(balance=Decimal("10000"))
        balance_before = account.balance

        # Cierre 1 (daemon path): BUY +200
        pos1 = make_position(
            account=account, side="BUY",
            qty=Decimal("0.1"), avg_price=Decimal("1.10000"),
        )
        _close_position_sync(
            pos_mem=_pos_mem_from(pos1), account_id=account.id,
            close_px=1.12000, reason="manual",
            realized_pnl=200.0, new_balance=10200.0, new_equity=10200.0,
        )

        # Cierre 2 (daemon path): SELL -80
        account.refresh_from_db()
        pos2 = make_position(
            account=account, side="SELL",
            qty=Decimal("0.1"), avg_price=Decimal("1.12000"),
        )
        _close_position_sync(
            pos_mem=_pos_mem_from(pos2), account_id=account.id,
            close_px=1.12800, reason="manual",
            realized_pnl=-80.0, new_balance=10120.0, new_equity=10120.0,
        )

        total_realized = (
            LedgerEntry.objects
            .filter(account=account, event_type=LedgerEntry.EV_REALIZED)
            .aggregate(total=Sum("amount"))["total"]
        )
        account.refresh_from_db()
        self.assertEqual(total_realized, Decimal("120"))
        self.assertEqual(account.balance, balance_before + total_realized)


# ─────────────────────────────────────────────────────────────────────────────
# 4. WithdrawalRequest.debit_tx linkage
# ─────────────────────────────────────────────────────────────────────────────

class TestWithdrawalDebitTxLink(TestCase):
    """
    El debit_tx de un WithdrawalRequest debe apuntar a la WalletTransaction
    del débito y tener amount == -wr.amount_usd (negativo exacto en Decimal).
    """

    AMOUNT = Decimal("50.00")

    def _create_wr(self):
        user     = make_user()
        wallet   = make_wallet(user=user, initial_balance=Decimal("200"))
        debit_tx = debit_wallet(
            wallet.id, self.AMOUNT, WalletTransaction.TX_WITHDRAW,
            note="retiro test",
        )
        wr = WithdrawalRequest.objects.create(
            user            = user,
            amount_usd      = self.AMOUNT,
            crypto_currency = "btc",
            wallet_address  = "bc1qtestaddress",
            status          = WithdrawalRequest.STATUS_PENDING,
            debit_tx        = debit_tx,
        )
        return wr, debit_tx, wallet

    def test_debit_tx_is_not_none(self):
        """WithdrawalRequest.debit_tx_id debe estar poblado (no None)."""
        wr, _, _ = self._create_wr()
        wr.refresh_from_db()
        self.assertIsNotNone(
            wr.debit_tx_id,
            "debit_tx debe estar vinculado al WalletTransaction del débito",
        )

    def test_debit_tx_amount_equals_negative_wr_amount(self):
        """
        debit_tx.amount == -wr.amount_usd
        El débito en wallet es exactamente el monto del retiro, negativo.
        """
        wr, debit_tx, _ = self._create_wr()
        debit_tx.refresh_from_db()
        self.assertEqual(
            debit_tx.amount,
            -wr.amount_usd,
            f"debit_tx.amount={debit_tx.amount} debe ser -{wr.amount_usd}",
        )
        self.assertIsInstance(debit_tx.amount, Decimal)


# ─────────────────────────────────────────────────────────────────────────────
# 5. pending_balance invariant
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingBalanceInvariant(TestCase):
    """
    pending_balance debe ser ≥ 0 en todo momento.
    Se drena exactamente cuando confirming → finished.
    Si el IPN llega directo a finished (sin confirming previo), no se toca.
    """

    @_PATCH_SIG_OK
    def test_pending_balance_exactly_zero_after_confirming_then_finished(self, _sig):
        """
        confirming → pending_balance += amount.
        finished   → pending_balance -= amount → queda en exactamente 0.
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(
            user=user, amount_usd=Decimal("150.00"), payment_id="pay_pb_drain"
        )

        self.client.post(
            DEPOSIT_CB_URL,
            _deposit_ipn("pay_pb_drain", "confirming", deposit.pk, "150.00"),
            content_type="application/json",
        )
        wallet.refresh_from_db()
        self.assertEqual(wallet.pending_balance, Decimal("150.00"))

        self.client.post(
            DEPOSIT_CB_URL,
            _deposit_ipn("pay_pb_drain", "finished", deposit.pk, "150.00"),
            content_type="application/json",
        )
        wallet.refresh_from_db()
        self.assertEqual(
            wallet.pending_balance, Decimal("0.00"),
            f"pending_balance={wallet.pending_balance} debe ser 0 tras drain",
        )
        self.assertGreaterEqual(
            wallet.pending_balance, Decimal("0"),
            "pending_balance no debe ser negativo",
        )

    @_PATCH_SIG_OK
    def test_direct_finished_does_not_touch_pending_balance(self, _sig):
        """
        IPN directo a 'finished' sin pasar por 'confirming':
        pending_balance permanece en 0 (no se drena ni incrementa).
        """
        user    = make_user()
        wallet  = make_wallet(user=user)
        deposit = make_deposit(
            user=user, amount_usd=Decimal("80.00"), payment_id="pay_pb_direct"
        )

        self.client.post(
            DEPOSIT_CB_URL,
            _deposit_ipn("pay_pb_direct", "finished", deposit.pk, "80.00"),
            content_type="application/json",
        )
        wallet.refresh_from_db()
        self.assertEqual(
            wallet.pending_balance, Decimal("0.00"),
            "pending_balance debe permanecer en 0 para IPN directo a 'finished'",
        )
        self.assertGreaterEqual(wallet.pending_balance, Decimal("0"))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fund conservation round-trip wallet ↔ account
# ─────────────────────────────────────────────────────────────────────────────

class TestFundConservationRoundTrip(TestCase):
    """
    transfer_to_account + transfer_to_wallet deben restaurar exactamente
    los saldos originales y dejar reconcile_wallet() sin drift.
    """

    TRANSFER_AMT = Decimal("300.00")

    def setUp(self):
        self.user    = make_user()
        self.account = make_account(user=self.user, balance=Decimal("10000"))
        self.wallet  = make_wallet(user=self.user, initial_balance=Decimal("500"))

    def test_transfer_to_account_and_back_conserves_wallet(self):
        """
        Wallet: 500 → (→account 300) → 200 → (←account 300) → 500.
        Account: 10000 → 10300 → 10000.
        reconcile_wallet() pasa sin drift.
        """
        wallet_before  = self.wallet.available_balance
        account_before = self.account.balance

        transfer_to_account(self.wallet.id, self.account.id, self.TRANSFER_AMT)
        self.wallet.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, wallet_before - self.TRANSFER_AMT)
        self.assertEqual(self.account.balance, account_before + self.TRANSFER_AMT)

        transfer_to_wallet(self.wallet.id, self.account.id, self.TRANSFER_AMT)
        self.wallet.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, wallet_before)
        self.assertEqual(self.account.balance, account_before)

        r = reconcile_wallet(self.wallet.id)
        self.assertTrue(r["ok"], f"Wallet drift tras round-trip: {r['drift']}")

    def test_zero_net_drift_across_multiple_round_trips(self):
        """
        3 transferencias ida y vuelta → drift acumulado == 0 y balance restaurado.
        """
        for _ in range(3):
            transfer_to_account(self.wallet.id, self.account.id, Decimal("100"))
            transfer_to_wallet(self.wallet.id, self.account.id, Decimal("100"))

        r = reconcile_wallet(self.wallet.id)
        self.assertTrue(r["ok"], f"Drift acumulado tras 3 round-trips: {r['drift']}")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("500.00"))

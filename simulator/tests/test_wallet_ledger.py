"""
simulator/tests/test_wallet_ledger.py — Bloque 1 (parte A)

Cubre: credit_wallet, debit_wallet, reconcile_wallet

Convenciones:
  - Django TestCase: cada test corre en su propia transacción que se hace rollback.
  - Cada test crea sus propios datos (sin setUp compartido).
  - Todos los montos son Decimal, nunca float.
  - Se verifica tanto el balance del modelo como el ledger (WalletTransaction).
"""
from decimal import Decimal

from django.test import TestCase

from simulator.models import WalletTransaction
from simulator.wallet_ledger import (
    InsufficientFunds,
    credit_wallet,
    debit_wallet,
    reconcile_wallet,
)

from .factories import make_wallet


# ─────────────────────────────────────────────
# credit_wallet
# ─────────────────────────────────────────────

class TestCreditWallet(TestCase):

    def test_credit_increases_balance(self):
        """credit_wallet sube available_balance exactamente en el monto."""
        wallet = make_wallet()  # balance = 0
        credit_wallet(wallet.id, Decimal("100.00"), WalletTransaction.TX_DEPOSIT)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))

    def test_credit_appends_wallet_transaction(self):
        """credit_wallet escribe exactamente 1 WalletTransaction con amount positivo."""
        wallet = make_wallet()
        credit_wallet(wallet.id, Decimal("250.00"), WalletTransaction.TX_DEPOSIT)
        txs = WalletTransaction.objects.filter(wallet=wallet)
        self.assertEqual(txs.count(), 1)
        tx = txs.first()
        self.assertEqual(tx.amount, Decimal("250.00"))
        self.assertEqual(tx.tx_type, WalletTransaction.TX_DEPOSIT)

    def test_credit_balance_after_matches_wallet(self):
        """WalletTransaction.balance_after debe coincidir con Wallet.available_balance."""
        wallet = make_wallet()
        credit_wallet(wallet.id, Decimal("300.00"), WalletTransaction.TX_DEPOSIT)
        wallet.refresh_from_db()
        tx = WalletTransaction.objects.filter(wallet=wallet).first()
        self.assertEqual(tx.balance_after, wallet.available_balance)

    def test_credit_zero_raises_value_error(self):
        """amount=0 debe levantar ValueError antes de tocar la DB."""
        wallet = make_wallet()
        with self.assertRaises(ValueError):
            credit_wallet(wallet.id, Decimal("0"), WalletTransaction.TX_DEPOSIT)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0"))

    def test_credit_negative_raises_value_error(self):
        """amount negativo debe levantar ValueError."""
        wallet = make_wallet()
        with self.assertRaises(ValueError):
            credit_wallet(wallet.id, Decimal("-50.00"), WalletTransaction.TX_DEPOSIT)

    def test_credit_accumulates_correctly(self):
        """Dos créditos sucesivos acumulan correctamente."""
        wallet = make_wallet()
        credit_wallet(wallet.id, Decimal("100.00"), WalletTransaction.TX_DEPOSIT)
        credit_wallet(wallet.id, Decimal("50.00"), WalletTransaction.TX_BONUS)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("150.00"))
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 2)


# ─────────────────────────────────────────────
# debit_wallet
# ─────────────────────────────────────────────

class TestDebitWallet(TestCase):

    def test_debit_decreases_balance(self):
        """debit_wallet baja available_balance exactamente en el monto."""
        wallet = make_wallet(initial_balance=Decimal("200.00"))
        debit_wallet(wallet.id, Decimal("80.00"), WalletTransaction.TX_WITHDRAW)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("120.00"))

    def test_debit_appends_negative_wallet_transaction(self):
        """El WalletTransaction de un debit tiene amount negativo."""
        wallet = make_wallet(initial_balance=Decimal("200.00"))
        debit_wallet(wallet.id, Decimal("80.00"), WalletTransaction.TX_WITHDRAW)
        # La factory ya creó 1 TX_DEPOSIT; el debit añade el segundo
        tx = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_WITHDRAW
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal("-80.00"))

    def test_debit_exact_balance_leaves_zero(self):
        """Retirar exactamente el balance disponible deja el wallet en 0."""
        wallet = make_wallet(initial_balance=Decimal("150.00"))
        debit_wallet(wallet.id, Decimal("150.00"), WalletTransaction.TX_WITHDRAW)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("0.00"))

    def test_debit_insufficient_funds_raises(self):
        """InsufficientFunds cuando amount > available_balance."""
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        with self.assertRaises(InsufficientFunds):
            debit_wallet(wallet.id, Decimal("100.00"), WalletTransaction.TX_WITHDRAW)

    def test_debit_insufficient_funds_balance_untouched(self):
        """Cuando falla por fondos insuficientes, el balance no cambia."""
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        try:
            debit_wallet(wallet.id, Decimal("999.00"), WalletTransaction.TX_WITHDRAW)
        except InsufficientFunds:
            pass
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("50.00"))

    def test_debit_insufficient_funds_no_transaction_written(self):
        """Cuando falla, no se escribe ningún WalletTransaction de debit."""
        wallet = make_wallet(initial_balance=Decimal("50.00"))
        count_before = WalletTransaction.objects.filter(wallet=wallet).count()
        try:
            debit_wallet(wallet.id, Decimal("999.00"), WalletTransaction.TX_WITHDRAW)
        except InsufficientFunds:
            pass
        count_after = WalletTransaction.objects.filter(wallet=wallet).count()
        self.assertEqual(count_before, count_after)

    def test_debit_zero_raises_value_error(self):
        """amount=0 levanta ValueError antes de cualquier DB write."""
        wallet = make_wallet(initial_balance=Decimal("100.00"))
        with self.assertRaises(ValueError):
            debit_wallet(wallet.id, Decimal("0"), WalletTransaction.TX_WITHDRAW)

    def test_debit_balance_after_matches_wallet(self):
        """WalletTransaction.balance_after coincide con Wallet.available_balance."""
        wallet = make_wallet(initial_balance=Decimal("200.00"))
        debit_wallet(wallet.id, Decimal("75.00"), WalletTransaction.TX_WITHDRAW)
        wallet.refresh_from_db()
        tx = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_WITHDRAW
        ).first()
        self.assertEqual(tx.balance_after, wallet.available_balance)


# ─────────────────────────────────────────────
# reconcile_wallet
# ─────────────────────────────────────────────

class TestReconcileWallet(TestCase):

    def test_reconcile_clean_wallet_is_ok(self):
        """Wallet recién creado sin operaciones: drift=0, ok=True."""
        wallet = make_wallet()
        result = reconcile_wallet(wallet.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["drift"], Decimal("0"))

    def test_reconcile_invariant_holds_after_operations(self):
        """N créditos + M débitos → la invariante siempre se cumple."""
        wallet = make_wallet()
        credit_wallet(wallet.id, Decimal("500.00"), WalletTransaction.TX_DEPOSIT)
        credit_wallet(wallet.id, Decimal("250.00"), WalletTransaction.TX_BONUS)
        debit_wallet(wallet.id, Decimal("100.00"), WalletTransaction.TX_WITHDRAW)
        debit_wallet(wallet.id, Decimal("75.00"), WalletTransaction.TX_COMMISSION)

        result = reconcile_wallet(wallet.id)

        self.assertTrue(result["ok"], f"Drift inesperado: {result['drift']}")
        self.assertEqual(result["drift"], Decimal("0"))
        self.assertEqual(result["stored"], Decimal("575.00"))
        self.assertEqual(result["computed"], Decimal("575.00"))

    def test_reconcile_detects_direct_balance_mutation(self):
        """Si Wallet.available_balance se modifica directamente (bypass del ledger),
        reconcile_wallet detecta el drift."""
        from simulator.models import Wallet

        wallet = make_wallet(initial_balance=Decimal("100.00"))

        # Mutación directa que viola la invariante (nadie debería hacer esto)
        Wallet.objects.filter(pk=wallet.id).update(available_balance=Decimal("999.00"))

        result = reconcile_wallet(wallet.id)

        self.assertFalse(result["ok"])
        self.assertNotEqual(result["drift"], Decimal("0"))
        # computed (ledger sum) = 100, stored (DB) = 999 → drift = 100 - 999 = -899
        self.assertEqual(result["drift"], Decimal("-899.00"))


# ─────────────────────────────────────────────
# transfer_to_account  (wallet → trading account)
# ─────────────────────────────────────────────

class TestTransferToAccount(TestCase):

    def test_debits_wallet_and_credits_account(self):
        """La transferencia mueve fondos de wallet a cuenta en un solo bloque."""
        from simulator.wallet_ledger import transfer_to_account
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("500.00"))
        account = make_account(user=user, balance=Decimal("0"))

        transfer_to_account(wallet.id, account.id, Decimal("300.00"))

        wallet.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("200.00"))
        self.assertEqual(account.balance, Decimal("300.00"))

    def test_creates_internal_transfer_completed(self):
        """InternalTransfer queda en status COMPLETED."""
        from simulator.wallet_ledger import transfer_to_account
        from simulator.models import InternalTransfer
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("500.00"))
        account = make_account(user=user, balance=Decimal("0"))

        xfer = transfer_to_account(wallet.id, account.id, Decimal("200.00"))

        self.assertEqual(xfer.status, InternalTransfer.ST_COMPLETED)
        self.assertIsNotNone(xfer.completed_at)

    def test_creates_wallet_tx_transfer_out(self):
        """Se escribe un WalletTransaction TX_TRANSFER_OUT con amount negativo."""
        from simulator.wallet_ledger import transfer_to_account
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("500.00"))
        account = make_account(user=user, balance=Decimal("0"))

        transfer_to_account(wallet.id, account.id, Decimal("150.00"))

        tx = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_TRANSFER_OUT
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal("-150.00"))

    def test_creates_ledger_entry_deposit_on_account(self):
        """Se escribe un LedgerEntry EV_DEPOSIT en la cuenta destino."""
        from simulator.wallet_ledger import transfer_to_account
        from simulator.models import LedgerEntry
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("500.00"))
        account = make_account(user=user, balance=Decimal("0"))

        transfer_to_account(wallet.id, account.id, Decimal("200.00"))

        entry = LedgerEntry.objects.filter(
            account=account, event_type=LedgerEntry.EV_DEPOSIT
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.amount, Decimal("200.00"))
        self.assertEqual(entry.balance_after, Decimal("200.00"))

    def test_insufficient_wallet_raises_and_marks_failed(self):
        """InsufficientFunds cuando wallet < amount; InternalTransfer queda FAILED."""
        from simulator.wallet_ledger import transfer_to_account, InsufficientFunds
        from simulator.models import InternalTransfer
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("100.00"))
        account = make_account(user=user, balance=Decimal("0"))

        with self.assertRaises(InsufficientFunds):
            transfer_to_account(wallet.id, account.id, Decimal("999.00"))

        # Balances sin cambio
        wallet.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(wallet.available_balance, Decimal("100.00"))
        self.assertEqual(account.balance, Decimal("0"))

        # InternalTransfer marcado FAILED
        xfer = InternalTransfer.objects.filter(
            wallet=wallet, trading_account=account
        ).first()
        self.assertIsNotNone(xfer)
        self.assertEqual(xfer.status, InternalTransfer.ST_FAILED)

    def test_reconcile_holds_after_transfer(self):
        """La invariante del wallet se mantiene después de una transferencia."""
        from simulator.wallet_ledger import transfer_to_account
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("400.00"))
        account = make_account(user=user, balance=Decimal("0"))

        transfer_to_account(wallet.id, account.id, Decimal("250.00"))

        result = reconcile_wallet(wallet.id)
        self.assertTrue(result["ok"], f"Drift: {result['drift']}")


# ─────────────────────────────────────────────
# transfer_to_wallet  (trading account → wallet)
# ─────────────────────────────────────────────

class TestTransferToWallet(TestCase):

    def test_debits_account_and_credits_wallet(self):
        """La transferencia mueve fondos de cuenta a wallet."""
        from simulator.wallet_ledger import transfer_to_wallet
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("0"))
        account = make_account(user=user, balance=Decimal("500.00"))

        transfer_to_wallet(wallet.id, account.id, Decimal("300.00"))

        wallet.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("200.00"))
        self.assertEqual(wallet.available_balance, Decimal("300.00"))

    def test_creates_internal_transfer_completed(self):
        from simulator.wallet_ledger import transfer_to_wallet
        from simulator.models import InternalTransfer
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user)
        account = make_account(user=user, balance=Decimal("300.00"))

        xfer = transfer_to_wallet(wallet.id, account.id, Decimal("100.00"))

        self.assertEqual(xfer.status, InternalTransfer.ST_COMPLETED)

    def test_creates_wallet_tx_transfer_in(self):
        from simulator.wallet_ledger import transfer_to_wallet
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user)
        account = make_account(user=user, balance=Decimal("300.00"))

        transfer_to_wallet(wallet.id, account.id, Decimal("120.00"))

        tx = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_TRANSFER_IN
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal("120.00"))

    def test_blocks_with_open_positions(self):
        """No se puede retirar si la cuenta tiene posiciones abiertas."""
        from simulator.wallet_ledger import transfer_to_wallet
        from .factories import make_account, make_position, make_user
        user = make_user()
        wallet = make_wallet(user=user)
        account = make_account(user=user, balance=Decimal("500.00"))
        make_position(account)  # posición abierta

        with self.assertRaises(ValueError):
            transfer_to_wallet(wallet.id, account.id, Decimal("100.00"))

        # Balances intactos
        account.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(account.balance, Decimal("500.00"))
        self.assertEqual(wallet.available_balance, Decimal("0"))

    def test_blocks_with_open_positions_marks_failed(self):
        """InternalTransfer queda FAILED cuando hay posiciones abiertas."""
        from simulator.wallet_ledger import transfer_to_wallet
        from simulator.models import InternalTransfer
        from .factories import make_account, make_position, make_user
        user = make_user()
        wallet = make_wallet(user=user)
        account = make_account(user=user, balance=Decimal("500.00"))
        make_position(account)

        try:
            transfer_to_wallet(wallet.id, account.id, Decimal("100.00"))
        except ValueError:
            pass

        xfer = InternalTransfer.objects.filter(
            wallet=wallet, trading_account=account
        ).first()
        self.assertEqual(xfer.status, InternalTransfer.ST_FAILED)

    def test_insufficient_account_balance_raises(self):
        from simulator.wallet_ledger import transfer_to_wallet, InsufficientFunds
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user)
        account = make_account(user=user, balance=Decimal("50.00"))

        with self.assertRaises(InsufficientFunds):
            transfer_to_wallet(wallet.id, account.id, Decimal("999.00"))

        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("50.00"))

    def test_reconcile_holds_after_transfer(self):
        from simulator.wallet_ledger import transfer_to_wallet
        from .factories import make_account, make_user
        user = make_user()
        wallet = make_wallet(user=user, initial_balance=Decimal("50.00"))
        account = make_account(user=user, balance=Decimal("400.00"))

        transfer_to_wallet(wallet.id, account.id, Decimal("200.00"))

        result = reconcile_wallet(wallet.id)
        self.assertTrue(result["ok"], f"Drift: {result['drift']}")
        self.assertEqual(result["stored"], Decimal("250.00"))  # 50 inicial + 200 recibidos

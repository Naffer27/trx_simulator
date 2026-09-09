# simulator/tests/test_verified_withdrawal_wallet.py
"""
WITHDRAWAL-SECURITY-EXTENSION-01 — simulator/verified_wallets.py +
VerifiedWithdrawalWallet lifecycle, exercised directly.

Covers:
  1.  create_pending_wallet() creates a PENDING_COOLDOWN row with
      cooldown_until = now + settings.WALLET_ADDRESS_CHANGE_COOLDOWN_HOURS.
  2.  A second pending row for the same (user, asset, network) raises
      AddressChangeAlreadyPending.
  3.  get_active_wallet() returns None while still in cooldown.
  4.  get_active_wallet() lazily activates a row once cooldown_until has passed.
  5.  sweep_activate_due_wallets() activates due rows without needing a read.
  6.  Activating a new wallet deactivates the OLD active wallet for the
      SAME (user, asset, network) route.
  7.  A different route (different asset/network) is unaffected by another
      route's activation/deactivation.
  8.  Only one ACTIVE row can exist per (user, asset, network) — DB constraint.
  9.  Only one PENDING_COOLDOWN row can exist per route — DB constraint.
  10. Old wallet stays ACTIVE and usable while the new one is still cooling down.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from simulator.models import VerifiedWithdrawalWallet
from simulator.tests.factories import make_user, make_verified_withdrawal_wallet
from simulator import verified_wallets as vw


@override_settings(WALLET_ADDRESS_CHANGE_COOLDOWN_HOURS=24)
class CreatePendingWalletTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_creates_pending_cooldown_row(self):
        wallet = vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        self.assertEqual(wallet.status, VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN)
        self.assertIsNotNone(wallet.verified_at)
        expected = wallet.verified_at + timedelta(hours=24)
        self.assertAlmostEqual(wallet.cooldown_until, expected, delta=timedelta(seconds=2))

    def test_duplicate_pending_route_raises(self):
        vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        with self.assertRaises(vw.AddressChangeAlreadyPending):
            vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr2")


class GetActiveWalletTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_returns_none_while_in_cooldown(self):
        vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        self.assertIsNone(vw.get_active_wallet(self.user, asset="USDT", network="TRC20"))

    def test_lazily_activates_due_row(self):
        pending = vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        VerifiedWithdrawalWallet.objects.filter(pk=pending.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1),
        )
        active = vw.get_active_wallet(self.user, asset="USDT", network="TRC20")
        self.assertIsNotNone(active)
        self.assertEqual(active.pk, pending.pk)
        self.assertEqual(active.status, VerifiedWithdrawalWallet.STATUS_ACTIVE)
        self.assertIsNotNone(active.activated_at)

    def test_returns_already_active_directly(self):
        active_wallet = make_verified_withdrawal_wallet(self.user, asset="USDT", network="TRC20")
        found = vw.get_active_wallet(self.user, asset="USDT", network="TRC20")
        self.assertEqual(found.pk, active_wallet.pk)

    def test_no_wallet_at_all_returns_none(self):
        self.assertIsNone(vw.get_active_wallet(self.user, asset="BTC", network="BTC_MAINNET"))


class SweepActivateDueWalletsTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_sweep_activates_due_rows(self):
        pending = vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        VerifiedWithdrawalWallet.objects.filter(pk=pending.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1),
        )
        activated = vw.sweep_activate_due_wallets()
        self.assertEqual(activated, 1)
        pending.refresh_from_db()
        self.assertEqual(pending.status, VerifiedWithdrawalWallet.STATUS_ACTIVE)

    def test_sweep_skips_rows_not_yet_due(self):
        vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        activated = vw.sweep_activate_due_wallets()
        self.assertEqual(activated, 0)


class ActivationDeactivatesOldWalletTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_activation_deactivates_old_active_same_route(self):
        old = make_verified_withdrawal_wallet(self.user, asset="USDT", network="TRC20", address="TaddrOLD")
        new_pending = vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="TaddrNEW")
        VerifiedWithdrawalWallet.objects.filter(pk=new_pending.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1),
        )
        active = vw.get_active_wallet(self.user, asset="USDT", network="TRC20")
        self.assertEqual(active.pk, new_pending.pk)

        old.refresh_from_db()
        self.assertEqual(old.status, VerifiedWithdrawalWallet.STATUS_DEACTIVATED)
        self.assertIsNotNone(old.deactivated_at)

    def test_old_wallet_stays_active_during_new_cooldown(self):
        old = make_verified_withdrawal_wallet(self.user, asset="USDT", network="TRC20", address="TaddrOLD")
        vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="TaddrNEW")

        active = vw.get_active_wallet(self.user, asset="USDT", network="TRC20")
        self.assertEqual(active.pk, old.pk)
        self.assertEqual(active.status, VerifiedWithdrawalWallet.STATUS_ACTIVE)

    def test_different_route_unaffected(self):
        usdt_old = make_verified_withdrawal_wallet(self.user, asset="USDT", network="TRC20", address="TaddrOLD")
        btc_active = make_verified_withdrawal_wallet(self.user, asset="BTC", network="BTC_MAINNET", address="bc1qold")

        new_pending = vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="TaddrNEW")
        VerifiedWithdrawalWallet.objects.filter(pk=new_pending.pk).update(
            cooldown_until=timezone.now() - timedelta(seconds=1),
        )
        vw.get_active_wallet(self.user, asset="USDT", network="TRC20")

        btc_active.refresh_from_db()
        self.assertEqual(btc_active.status, VerifiedWithdrawalWallet.STATUS_ACTIVE)


class DbConstraintTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_only_one_active_per_route(self):
        make_verified_withdrawal_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VerifiedWithdrawalWallet.objects.create(
                    user=self.user, asset="USDT", network="TRC20", address="Taddr2",
                    status=VerifiedWithdrawalWallet.STATUS_ACTIVE,
                )

    def test_only_one_pending_per_route(self):
        vw.create_pending_wallet(self.user, asset="USDT", network="TRC20", address="Taddr1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VerifiedWithdrawalWallet.objects.create(
                    user=self.user, asset="USDT", network="TRC20", address="Taddr2",
                    status=VerifiedWithdrawalWallet.STATUS_PENDING_COOLDOWN,
                )

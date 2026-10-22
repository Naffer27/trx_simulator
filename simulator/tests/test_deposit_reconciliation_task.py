from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from simulator.models import Deposit
from simulator.tasks import reconcile_deposits_task


class DepositReconciliationTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="deposit-audit-test")
        self.now = timezone.now()

    def deposit(self, hours_old, credited=False):
        deposit = Deposit.objects.create(
            user=self.user,
            amount_usd="100.00",
            crypto_currency="usdttrc20",
            credited=credited,
        )
        Deposit.objects.filter(pk=deposit.pk).update(
            created_at=self.now - timedelta(hours=hours_old)
        )
        return deposit

    def audit(self, hours_back=24):
        # The audit must never write to deposits, wallets, or ledgers.
        def read_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)
            return execute(sql, params, many, context)

        with patch("django.utils.timezone.now", return_value=self.now):
            with connection.execute_wrapper(read_only):
                return reconcile_deposits_task.run(hours_back=hours_back)

    def test_empty_database_returns_no_stale_deposits(self):
        self.assertEqual(self.audit(), {
            "checked": True, "stale_count": 0, "stale_ids": [],
        })

    def test_only_old_uncredited_deposits_are_reported_including_cutoff(self):
        stale = self.deposit(25)
        cutoff = self.deposit(24)
        self.deposit(23)
        self.deposit(48, credited=True)
        before = list(Deposit.objects.order_by("pk").values())

        result = self.audit()

        self.assertTrue(result["checked"])
        self.assertEqual(result["stale_count"], 2)
        self.assertCountEqual(result["stale_ids"], [stale.pk, cutoff.pk])
        self.assertEqual(list(Deposit.objects.order_by("pk").values()), before)

    def test_custom_window_is_respected(self):
        stale = self.deposit(49)
        self.deposit(25)
        self.assertEqual(self.audit(hours_back=48), {
            "checked": True, "stale_count": 1, "stale_ids": [stale.pk],
        })

    def test_repeating_audit_does_not_credit_deposit(self):
        stale = self.deposit(30)
        first = self.audit()
        self.assertEqual(self.audit(), first)
        stale.refresh_from_db()
        self.assertFalse(stale.credited)
        self.assertIsNone(stale.credited_at)
        self.assertEqual(first["stale_ids"], [stale.pk])

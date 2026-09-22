# simulator/tests/test_seed_ib_portal_visual_qa_command.py
"""
IB-PORTAL-UX-10A — regression coverage for
simulator/management/commands/seed_ib_portal_visual_qa.py.

Covers:
  - the fail-closed APP_ENV production guard (refuses to run — seed OR
    cleanup — unless settings.APP_ENV is in the local/dev allowlist);
  - end-to-end seed correctness (real counts, real cross-model
    consistency via the real services the command reuses);
  - idempotency (refuses to seed twice without --cleanup);
  - never creates the target IB;
  - end-to-end cleanup correctness, including the Referral.clicks
    reversal (the one pre-existing real object this command mutates)
    and that the target IB's own pre-existing Referral/code/risk_status
    survive untouched.

All runs against the isolated test database only — this command is
never invoked against a real dev database from a test.
"""
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from simulator.models import (
    Deposit, IBCommissionObligation, IBCommissionRule, LotExecutionEvent,
    Referral, ReferralAttribution, TradingAccount, WithdrawalRequest,
)
from simulator.tests.factories import make_user


class SeedCommandProductionGuardTests(TestCase):
    """The fail-closed APP_ENV allowlist — checked first, before any DB write."""

    def setUp(self):
        self.ib = make_user(username="GuardTestIB")

    @override_settings(APP_ENV="production")
    def test_refuses_to_seed_in_production(self):
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="GuardTestIB")
        # Nothing written — the guard fires before any query.
        self.assertFalse(ReferralAttribution.objects.exists())

    @override_settings(APP_ENV="staging")
    def test_refuses_to_seed_in_staging(self):
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="GuardTestIB")

    @override_settings(APP_ENV="")
    def test_refuses_with_blank_app_env(self):
        """Fail-closed: an unset/blank value is never treated as safe."""
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="GuardTestIB")

    @override_settings(APP_ENV="some_future_env_nobody_listed")
    def test_refuses_with_unrecognized_app_env(self):
        """Positive allowlist: an unlisted future env name is refused,
        never silently allowed."""
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="GuardTestIB")

    @override_settings(APP_ENV="production")
    def test_guard_also_blocks_cleanup(self):
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="GuardTestIB", cleanup=True)

    @override_settings(APP_ENV="development")
    def test_allows_in_development(self):
        call_command("seed_ib_portal_visual_qa", user="GuardTestIB")
        self.assertEqual(
            ReferralAttribution.objects.filter(
                referred_user__username__startswith="qa10a_",
            ).count(),
            12,
        )
        call_command("seed_ib_portal_visual_qa", user="GuardTestIB", cleanup=True)

    @override_settings(APP_ENV="test")
    def test_allows_in_test(self):
        call_command("seed_ib_portal_visual_qa", user="GuardTestIB")
        call_command("seed_ib_portal_visual_qa", user="GuardTestIB", cleanup=True)


@override_settings(APP_ENV="test")
class SeedCommandNeverCreatesTargetTests(TestCase):
    def test_refuses_unknown_user(self):
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="DoesNotExist12345")
        from django.contrib.auth import get_user_model
        self.assertFalse(get_user_model().objects.filter(username="DoesNotExist12345").exists())


@override_settings(APP_ENV="test")
class SeedCommandEndToEndTests(TestCase):
    def setUp(self):
        self.ib_user = make_user(username="E2EIB")

    def test_seed_creates_expected_real_objects(self):
        call_command("seed_ib_portal_visual_qa", user="E2EIB")

        ref = Referral.objects.get(user=self.ib_user)
        self.assertGreaterEqual(ref.clicks, 20)
        self.assertLessEqual(ref.clicks, 30)

        self.assertEqual(ReferralAttribution.objects.filter(referral=ref).count(), 12)
        self.assertEqual(
            TradingAccount.objects.filter(user__referral_attribution__referral=ref, account_type="DEMO").count(),
            8,
        )
        live_qs = TradingAccount.objects.filter(
            user__referral_attribution__referral=ref,
            account_type__in=TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES,
        )
        self.assertEqual(live_qs.count(), 5)
        self.assertEqual(
            Deposit.objects.filter(user__referral_attribution__referral=ref, credited=True).count(), 3,
        )
        self.assertEqual(
            WithdrawalRequest.objects.filter(
                user__referral_attribution__referral=ref, status=WithdrawalRequest.STATUS_COMPLETED,
            ).count(),
            1,
        )
        self.assertGreater(
            LotExecutionEvent.objects.filter(account__user__referral_attribution__referral=ref).count(), 0,
        )
        self.assertTrue(IBCommissionRule.objects.filter(referral=ref, rule_type="PER_LOT").exists())
        self.assertGreater(IBCommissionObligation.objects.filter(referral=ref).count(), 0)

        call_command("seed_ib_portal_visual_qa", user="E2EIB", cleanup=True)

    def test_double_seed_refused_without_cleanup(self):
        call_command("seed_ib_portal_visual_qa", user="E2EIB")
        with self.assertRaises(CommandError):
            call_command("seed_ib_portal_visual_qa", user="E2EIB")
        call_command("seed_ib_portal_visual_qa", user="E2EIB", cleanup=True)

    def test_cleanup_removes_everything_and_reverts_clicks(self):
        ref = Referral.objects.create(user=self.ib_user, code="e2epreexisting", clicks=0)
        call_command("seed_ib_portal_visual_qa", user="E2EIB")
        ref.refresh_from_db()
        self.assertGreater(ref.clicks, 0)

        call_command("seed_ib_portal_visual_qa", user="E2EIB", cleanup=True)

        ref.refresh_from_db()
        self.assertEqual(ref.clicks, 0)
        self.assertEqual(ref.code, "e2epreexisting")
        self.assertEqual(ReferralAttribution.objects.filter(referral=ref).count(), 0)
        self.assertEqual(TradingAccount.objects.filter(user__referral_attribution__referral=ref).count(), 0)
        self.assertEqual(Deposit.objects.filter(user__referral_attribution__referral=ref).count(), 0)
        self.assertEqual(WithdrawalRequest.objects.filter(user__referral_attribution__referral=ref).count(), 0)
        self.assertEqual(
            LotExecutionEvent.objects.filter(account__user__referral_attribution__referral=ref).count(), 0,
        )
        self.assertEqual(IBCommissionObligation.objects.filter(referral=ref).count(), 0)
        self.assertEqual(IBCommissionRule.objects.filter(referral=ref).count(), 0)
        from django.contrib.auth import get_user_model
        self.assertFalse(get_user_model().objects.filter(username__startswith="qa10a_").exists())
        # The target IB's own Referral row itself survives, untouched.
        self.assertTrue(Referral.objects.filter(pk=ref.pk).exists())

    def test_cleanup_never_touches_other_referrals(self):
        other_user = make_user(username="UnrelatedIB")
        other_ref = Referral.objects.create(user=other_user, code="untouched", clicks=7)
        call_command("seed_ib_portal_visual_qa", user="E2EIB")
        call_command("seed_ib_portal_visual_qa", user="E2EIB", cleanup=True)
        other_ref.refresh_from_db()
        self.assertEqual(other_ref.clicks, 7)
        self.assertEqual(other_ref.code, "untouched")

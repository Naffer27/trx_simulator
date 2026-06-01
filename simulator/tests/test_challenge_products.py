"""
simulator/tests/test_challenge_products.py — Phase 4B.1

Cubre los cuatro modelos del catálogo de productos:
  - AccountProduct
  - ChallengeProduct
  - ChallengeEnrollment
  - FundedConfig

Convenciones:
  - Todos los amounts son Decimal, nunca float.
  - Cada test crea sus propios datos.
  - No se toca lógica productiva: solo ORM directo y factories.
  - No se prueban dashboard, views, consumers ni WebSocket.
"""
from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase

from simulator.models import (
    AccountProduct,
    ChallengeEnrollment,
    ChallengeProduct,
    FundedConfig,
    TradingAccount,
)
from simulator.tests.factories import (
    make_account,
    make_account_product,
    make_challenge_enrollment,
    make_challenge_product,
    make_funded_config,
    make_user,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. AccountProduct
# ─────────────────────────────────────────────────────────────────────────────

class TestAccountProductCreation(TestCase):

    def test_creates_with_correct_fields(self):
        """AccountProduct guarda name, product_type, min_deposit y leverage correctamente."""
        ap = make_account_product(
            name="ECN Pro",
            product_type=AccountProduct.TYPE_ECN,
            min_deposit=Decimal("200.00"),
            max_leverage=100,
        )
        ap.refresh_from_db()
        self.assertEqual(ap.name, "ECN Pro")
        self.assertEqual(ap.product_type, AccountProduct.TYPE_ECN)
        self.assertEqual(ap.min_deposit, Decimal("200.00"))
        self.assertEqual(ap.max_leverage, 100)

    def test_is_active_defaults_to_true(self):
        ap = make_account_product()
        self.assertTrue(ap.is_active)

    def test_features_defaults_to_empty_dict(self):
        ap = make_account_product()
        self.assertEqual(ap.features, {})

    def test_commission_pct_stored_as_decimal(self):
        ap = make_account_product(commission_pct=Decimal("0.0025"))
        ap.refresh_from_db()
        self.assertIsInstance(ap.commission_pct, Decimal)
        self.assertEqual(ap.commission_pct, Decimal("0.0025"))

    def test_spread_markup_stored_as_decimal(self):
        ap = make_account_product(spread_markup=Decimal("0.0010"))
        ap.refresh_from_db()
        self.assertEqual(ap.spread_markup, Decimal("0.0010"))

    def test_features_jsonfield_stored_and_retrieved(self):
        features = {"no_commission": True, "swap_free": False, "crypto_only": False}
        ap = make_account_product(features=features)
        ap.refresh_from_db()
        self.assertEqual(ap.features["no_commission"], True)
        self.assertEqual(ap.features["swap_free"], False)

    def test_all_product_types_can_be_saved(self):
        """Los 5 tipos de AccountProduct se persisten sin error."""
        for pt in [
            AccountProduct.TYPE_DEMO,
            AccountProduct.TYPE_RETAIL,
            AccountProduct.TYPE_ECN,
            AccountProduct.TYPE_STANDARD,
            AccountProduct.TYPE_CRYPTO,
        ]:
            with self.subTest(product_type=pt):
                ap = make_account_product(product_type=pt)
                self.assertEqual(ap.product_type, pt)

    def test_str_contains_name_and_type(self):
        ap = make_account_product(name="Retail Basic", product_type=AccountProduct.TYPE_RETAIL)
        self.assertIn("Retail Basic", str(ap))
        self.assertIn("RETAIL", str(ap))


# ─────────────────────────────────────────────────────────────────────────────
# 2. ChallengeProduct
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeProductCreation(TestCase):

    def test_creates_with_correct_base_fields(self):
        """ChallengeProduct guarda tier, price_usd y account_size correctamente."""
        cp = make_challenge_product(
            tier=ChallengeProduct.TIER_10K,
            price_usd=Decimal("99.00"),
            account_size=Decimal("10000.00"),
        )
        cp.refresh_from_db()
        self.assertEqual(cp.tier, ChallengeProduct.TIER_10K)
        self.assertEqual(cp.price_usd, Decimal("99.00"))
        self.assertEqual(cp.account_size, Decimal("10000.00"))

    def test_25k_tier_is_independent_of_trading_account_tiers(self):
        """25K existe en ChallengeProduct sin tocar TradingAccount.ACCOUNT_TIERS."""
        cp = make_challenge_product(
            tier=ChallengeProduct.TIER_25K,
            price_usd=Decimal("199.00"),
            account_size=Decimal("25000.00"),
        )
        self.assertEqual(cp.tier, ChallengeProduct.TIER_25K)
        # Confirmar que 25K NO existe en TradingAccount
        ta_tiers = [t[0] for t in TradingAccount.ACCOUNT_TIERS]
        self.assertNotIn("25K", ta_tiers)

    def test_all_tiers_can_be_saved(self):
        for tier, account_size in [
            (ChallengeProduct.TIER_10K,  Decimal("10000")),
            (ChallengeProduct.TIER_25K,  Decimal("25000")),
            (ChallengeProduct.TIER_50K,  Decimal("50000")),
            (ChallengeProduct.TIER_100K, Decimal("100000")),
        ]:
            with self.subTest(tier=tier):
                cp = make_challenge_product(tier=tier, account_size=account_size)
                self.assertEqual(cp.tier, tier)

    def test_phase1_rules_stored_correctly(self):
        cp = make_challenge_product(
            p1_profit_target_pct=Decimal("8.00"),
            p1_max_drawdown_pct=Decimal("10.00"),
            p1_max_daily_loss_pct=Decimal("5.00"),
            p1_min_trading_days=5,
            p1_max_duration_days=30,
        )
        cp.refresh_from_db()
        self.assertEqual(cp.p1_profit_target_pct,  Decimal("8.00"))
        self.assertEqual(cp.p1_max_drawdown_pct,   Decimal("10.00"))
        self.assertEqual(cp.p1_max_daily_loss_pct, Decimal("5.00"))
        self.assertEqual(cp.p1_min_trading_days,   5)
        self.assertEqual(cp.p1_max_duration_days,  30)

    def test_phase2_rules_stored_correctly(self):
        cp = make_challenge_product(
            p2_profit_target_pct=Decimal("5.00"),
            p2_max_drawdown_pct=Decimal("10.00"),
            p2_max_daily_loss_pct=Decimal("5.00"),
            p2_min_trading_days=5,
            p2_max_duration_days=60,
        )
        cp.refresh_from_db()
        self.assertEqual(cp.p2_profit_target_pct,  Decimal("5.00"))
        self.assertEqual(cp.p2_max_drawdown_pct,   Decimal("10.00"))
        self.assertEqual(cp.p2_max_daily_loss_pct, Decimal("5.00"))
        self.assertEqual(cp.p2_min_trading_days,   5)
        self.assertEqual(cp.p2_max_duration_days,  60)

    def test_p1_profit_target_amount_computed_correctly(self):
        """account_size=10000, p1_target_pct=8% → p1_profit_target_amount=$800.00"""
        cp = make_challenge_product(
            account_size=Decimal("10000.00"),
            p1_profit_target_pct=Decimal("8.00"),
        )
        self.assertEqual(cp.p1_profit_target_amount(), Decimal("800.00"))

    def test_p2_profit_target_amount_computed_correctly(self):
        """account_size=10000, p2_target_pct=5% → p2_profit_target_amount=$500.00"""
        cp = make_challenge_product(
            account_size=Decimal("10000.00"),
            p2_profit_target_pct=Decimal("5.00"),
        )
        self.assertEqual(cp.p2_profit_target_amount(), Decimal("500.00"))

    def test_profit_split_pct_defaults_to_80(self):
        cp = make_challenge_product()
        self.assertEqual(cp.profit_split_pct, Decimal("80.00"))

    def test_is_active_defaults_to_true(self):
        cp = make_challenge_product()
        self.assertTrue(cp.is_active)

    def test_str_contains_tier_and_price(self):
        cp = make_challenge_product(tier=ChallengeProduct.TIER_10K, price_usd=Decimal("99.00"))
        self.assertIn("10K", str(cp))
        self.assertIn("99", str(cp))


# ─────────────────────────────────────────────────────────────────────────────
# 3. ChallengeEnrollment
# ─────────────────────────────────────────────────────────────────────────────

class TestChallengeEnrollmentCreation(TestCase):

    def test_status_defaults_to_phase_1(self):
        enr = make_challenge_enrollment()
        self.assertEqual(enr.status, ChallengeEnrollment.ST_PHASE_1)

    def test_timestamps_null_on_creation(self):
        enr = make_challenge_enrollment()
        self.assertIsNone(enr.phase1_passed_at)
        self.assertIsNone(enr.phase2_passed_at)
        self.assertIsNone(enr.funded_at)

    def test_failed_fields_null_on_creation(self):
        enr = make_challenge_enrollment()
        self.assertIsNone(enr.failed_at_phase)
        self.assertIsNone(enr.failure_reason)

    def test_phase_accounts_null_on_creation(self):
        enr = make_challenge_enrollment()
        self.assertIsNone(enr.phase1_account)
        self.assertIsNone(enr.phase2_account)
        self.assertIsNone(enr.funded_account)

    def test_deposit_optional_for_admin_issued_enrollment(self):
        """deposit=None es válido — enrollments creados por admin sin pago."""
        enr = make_challenge_enrollment(deposit=None)
        self.assertIsNone(enr.deposit)

    def test_multiple_admin_enrollments_without_deposit_allowed(self):
        """Dos enrollments con deposit=None no violan el UniqueConstraint."""
        user = make_user()
        product = make_challenge_product()
        e1 = make_challenge_enrollment(user=user, product=product, deposit=None)
        e2 = make_challenge_enrollment(user=user, product=product, deposit=None)
        self.assertNotEqual(e1.pk, e2.pk)

    def test_duplicate_enrollment_same_deposit_raises_integrity_error(self):
        """Un mismo Deposit no puede usarse en dos enrollments distintos."""
        from simulator.models import Deposit as Dep
        user = make_user()
        deposit = Dep.objects.create(
            user=user, amount_usd=Decimal("99.00"),
            crypto_currency="btc",
            nowpayments_payment_id=f"pay_{user.pk}_unique",
            status="completed", credited=True,
        )
        product = make_challenge_product()
        make_challenge_enrollment(user=user, product=product, deposit=deposit)
        with self.assertRaises(IntegrityError):
            make_challenge_enrollment(user=user, product=product, deposit=deposit)


class TestChallengeEnrollmentActiveAccount(TestCase):
    """active_account property returns the correct TradingAccount per status."""

    def setUp(self):
        self.user    = make_user()
        self.product = make_challenge_product()
        self.acc1    = make_account(user=self.user, account_type="CHALLENGE")
        self.acc2    = make_account(user=self.user, account_type="CHALLENGE")
        self.acc_f   = make_account(user=self.user, account_type="FUNDED")

    def test_active_account_phase1_returns_phase1_account(self):
        enr = make_challenge_enrollment(user=self.user, product=self.product)
        enr.phase1_account = self.acc1
        enr.status = ChallengeEnrollment.ST_PHASE_1
        enr.save()
        self.assertEqual(enr.active_account, self.acc1)

    def test_active_account_phase2_returns_phase2_account(self):
        enr = make_challenge_enrollment(user=self.user, product=self.product)
        enr.phase2_account = self.acc2
        enr.status = ChallengeEnrollment.ST_PHASE_2
        enr.save()
        self.assertEqual(enr.active_account, self.acc2)

    def test_active_account_funded_returns_funded_account(self):
        enr = make_challenge_enrollment(user=self.user, product=self.product)
        enr.funded_account = self.acc_f
        enr.status = ChallengeEnrollment.ST_FUNDED
        enr.save()
        self.assertEqual(enr.active_account, self.acc_f)

    def test_active_account_failed_returns_none(self):
        enr = make_challenge_enrollment(user=self.user, product=self.product)
        enr.status = ChallengeEnrollment.ST_FAILED
        enr.save()
        self.assertIsNone(enr.active_account)

    def test_active_account_withdrawn_returns_none(self):
        enr = make_challenge_enrollment(user=self.user, product=self.product)
        enr.status = ChallengeEnrollment.ST_WITHDRAWN
        enr.save()
        self.assertIsNone(enr.active_account)


class TestChallengeEnrollmentStatusTransitions(TestCase):
    """Status field accepts all valid choices and stores failure context."""

    def test_all_statuses_can_be_saved(self):
        for st in [
            ChallengeEnrollment.ST_PHASE_1,
            ChallengeEnrollment.ST_PHASE_2,
            ChallengeEnrollment.ST_FUNDED,
            ChallengeEnrollment.ST_FAILED,
            ChallengeEnrollment.ST_WITHDRAWN,
        ]:
            with self.subTest(status=st):
                enr = make_challenge_enrollment()
                enr.status = st
                enr.save()
                enr.refresh_from_db()
                self.assertEqual(enr.status, st)

    def test_failed_fields_can_be_set(self):
        enr = make_challenge_enrollment()
        enr.status = ChallengeEnrollment.ST_FAILED
        enr.failed_at_phase = ChallengeEnrollment.FAILED_AT_PHASE_1
        enr.failure_reason = "Max drawdown breached on day 3"
        enr.save()
        enr.refresh_from_db()
        self.assertEqual(enr.failed_at_phase, "PHASE_1")
        self.assertEqual(enr.failure_reason, "Max drawdown breached on day 3")

    def test_enrollment_linked_to_correct_user_and_product(self):
        user    = make_user()
        product = make_challenge_product(tier=ChallengeProduct.TIER_50K,
                                         account_size=Decimal("50000"))
        enr = make_challenge_enrollment(user=user, product=product)
        self.assertEqual(enr.user, user)
        self.assertEqual(enr.product, product)
        self.assertEqual(enr.product.tier, ChallengeProduct.TIER_50K)

    def test_reverse_relation_from_trading_account(self):
        """TradingAccount.enrollment_phase1 points back to the enrollment."""
        user    = make_user()
        product = make_challenge_product()
        account = make_account(user=user, account_type="CHALLENGE")
        enr = make_challenge_enrollment(user=user, product=product,
                                        phase1_account=account)
        enr.phase1_account = account
        enr.save()
        account.refresh_from_db()
        self.assertEqual(account.enrollment_phase1, enr)


# ─────────────────────────────────────────────────────────────────────────────
# 4. FundedConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestFundedConfig(TestCase):

    def test_creates_with_correct_fields(self):
        """FundedConfig guarda funded_type y profit_split_pct correctamente."""
        enr = make_challenge_enrollment()
        cfg = make_funded_config(
            enrollment=enr,
            funded_type=FundedConfig.FUNDED_SIM,
            profit_split_pct=Decimal("80.00"),
        )
        cfg.refresh_from_db()
        self.assertEqual(cfg.funded_type, FundedConfig.FUNDED_SIM)
        self.assertEqual(cfg.profit_split_pct, Decimal("80.00"))
        self.assertEqual(cfg.enrollment, enr)

    def test_funded_type_defaults_to_funded_sim(self):
        """Si no se especifica, funded_type=FUNDED_SIM (no REAL, no regulada)."""
        cfg = make_funded_config()
        self.assertEqual(cfg.funded_type, FundedConfig.FUNDED_SIM)

    def test_funded_internal_can_be_set(self):
        cfg = make_funded_config(funded_type=FundedConfig.FUNDED_INTERNAL)
        self.assertEqual(cfg.funded_type, FundedConfig.FUNDED_INTERNAL)

    def test_is_active_defaults_to_true(self):
        cfg = make_funded_config()
        self.assertTrue(cfg.is_active)

    def test_profit_split_pct_is_decimal(self):
        cfg = make_funded_config(profit_split_pct=Decimal("75.00"))
        cfg.refresh_from_db()
        self.assertIsInstance(cfg.profit_split_pct, Decimal)
        self.assertEqual(cfg.profit_split_pct, Decimal("75.00"))

    def test_payout_defaults_are_correct(self):
        cfg = make_funded_config()
        self.assertEqual(cfg.min_payout_usd, Decimal("50.00"))
        self.assertEqual(cfg.min_trading_days, 5)
        self.assertEqual(cfg.payout_cycle_days, 14)
        self.assertEqual(cfg.max_monthly_drawdown_pct, Decimal("5.00"))

    def test_funded_config_is_onetoone_with_enrollment(self):
        """No se pueden crear dos FundedConfig para el mismo enrollment."""
        enr = make_challenge_enrollment()
        make_funded_config(enrollment=enr)
        with self.assertRaises(IntegrityError):
            make_funded_config(enrollment=enr)

    def test_str_contains_funded_type_and_split(self):
        cfg = make_funded_config(
            funded_type=FundedConfig.FUNDED_SIM,
            profit_split_pct=Decimal("80.00"),
        )
        self.assertIn("FUNDED_SIM", str(cfg))
        self.assertIn("80.00", str(cfg))

    def test_profit_split_copied_from_product_value(self):
        """
        Confirma el patrón de diseño: profit_split_pct en FundedConfig debe
        coincidir con el valor del producto en el momento de la creación,
        simulando la copia inmutable definida en el diseño.
        """
        product = make_challenge_product(profit_split_pct=Decimal("85.00"))
        enr = make_challenge_enrollment(product=product)
        cfg = make_funded_config(
            enrollment=enr,
            profit_split_pct=product.profit_split_pct,  # copied at promotion time
        )
        self.assertEqual(cfg.profit_split_pct, Decimal("85.00"))
        # Cambiar el producto no afecta la config existente
        product.profit_split_pct = Decimal("70.00")
        product.save()
        cfg.refresh_from_db()
        self.assertEqual(cfg.profit_split_pct, Decimal("85.00"))  # inmutable

"""
simulator/tests/test_commercial_pricing.py — SPREAD-04.

Covers simulator/commercial_pricing.py: the single commercial pricing
resolver for Demo, Real, Challenge, and Funded accounts.
"""
from decimal import Decimal

from django.test import TestCase

from simulator import commercial_pricing as cp
from simulator.models import BrokerSpreadConfig, TradingAccount
from simulator.spread_config_cache import refresh_cache_sync, reset_for_tests

from .factories import (
    make_account,
    make_account_product,
    make_challenge_enrollment,
    make_challenge_product,
    make_spread_config,
    make_user,
)


class CommercialPricingProfileValidationTests(TestCase):
    def test_min_greater_than_max_raises(self):
        with self.assertRaises(ValueError):
            cp.CommercialPricingProfile(
                profile_version=1, profile_id="x", account_type=None, product_type=None,
                spread_markup_pips=0.0, commission_per_lot=0.0, commission_pct=0.0,
                min_spread_pips=5.0, max_spread_pips=2.0, enabled=True, source="x",
            )

    def test_min_equal_max_is_valid(self):
        profile = cp.CommercialPricingProfile(
            profile_version=1, profile_id="x", account_type=None, product_type=None,
            spread_markup_pips=0.0, commission_per_lot=0.0, commission_pct=0.0,
            min_spread_pips=3.0, max_spread_pips=3.0, enabled=True, source="x",
        )
        self.assertEqual(profile.min_spread_pips, profile.max_spread_pips)

    def test_none_bounds_are_valid(self):
        profile = cp.CommercialPricingProfile(
            profile_version=1, profile_id="x", account_type=None, product_type=None,
            spread_markup_pips=0.0, commission_per_lot=0.0, commission_pct=0.0,
            min_spread_pips=None, max_spread_pips=None, enabled=True, source="x",
        )
        self.assertIsNone(profile.min_spread_pips)


class ResolveDemoAndRealAccountProductTests(TestCase):
    """1) Demo con AccountProduct. 2) Real Standard. 3) Real ECN."""

    def test_demo_account_resolves_from_frozen_snapshot(self):
        product = make_account_product(
            product_type="DEMO", family="DEMO", typical_spread_pips=Decimal("1.20"),
            commission_per_lot=Decimal("0.00"),
        )
        account = make_account(account_type="DEMO", balance=Decimal("10000"))
        account.account_product = product
        account.commercial_profile_snapshot = cp.commercial_pricing_fields_from_account_product(product)
        account.save(update_fields=["account_product", "commercial_profile_snapshot"])

        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["spread_markup_pips"], 1.20)
        self.assertEqual(fields["source"], cp.SOURCE_ACCOUNT_PRODUCT)

    def test_real_standard_resolves_spread_only(self):
        product = make_account_product(
            product_type="STANDARD", family="REAL", typical_spread_pips=Decimal("1.20"),
            commission_per_lot=Decimal("0.00"),
        )
        fields = cp.commercial_pricing_fields_from_account_product(product)
        self.assertEqual(fields["spread_markup_pips"], 1.20)
        self.assertEqual(fields["commission_per_lot"], 0.0)

    def test_real_ecn_resolves_commission_only(self):
        product = make_account_product(
            product_type="ECN", family="REAL", typical_spread_pips=Decimal("0.00"),
            commission_per_lot=Decimal("7.00"),
        )
        fields = cp.commercial_pricing_fields_from_account_product(product)
        self.assertEqual(fields["spread_markup_pips"], 0.0)
        self.assertEqual(fields["commission_per_lot"], 7.00)

    def test_account_product_fallback_branch_b_no_snapshot(self):
        """An account with a live account_product FK but no snapshot at all
        (defensive path — every real creation flow already freezes one)."""
        product = make_account_product(typical_spread_pips=Decimal("1.5"))
        account = make_account(account_type="STANDARD", balance=Decimal("10000"))
        account.account_product = product
        account.save(update_fields=["account_product"])

        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["source"], cp.SOURCE_ACCOUNT_PRODUCT)
        self.assertEqual(fields["spread_markup_pips"], 1.5)


class ResolveChallengeAndFundedTests(TestCase):
    """4) Challenge. 5) Funded heredando política."""

    def test_challenge_account_resolves_via_real_enrollment_relation(self):
        product = make_challenge_product(
            spread_markup_pips=Decimal("0.80"), commission_per_lot=Decimal("3.50"),
        )
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)
        account = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        enrollment.phase1_account = account
        enrollment.save(update_fields=["phase1_account"])

        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["source"], cp.SOURCE_CHALLENGE_PRODUCT)
        self.assertEqual(fields["spread_markup_pips"], 0.80)
        self.assertEqual(fields["commission_per_lot"], 3.50)

    def test_funded_account_inherits_same_product_policy(self):
        """Funded is resolved from the SAME ChallengeProduct as the
        challenge that produced it — 'heredando política'."""
        product = make_challenge_product(
            spread_markup_pips=Decimal("0.80"), commission_per_lot=Decimal("3.50"),
        )
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)
        funded_account = make_account(account_type="FUNDED", balance=Decimal("10000"))
        enrollment.funded_account = funded_account
        enrollment.save(update_fields=["funded_account"])

        fields = cp.resolve_commercial_pricing_fields(funded_account)
        self.assertEqual(fields["source"], cp.SOURCE_CHALLENGE_PRODUCT)
        self.assertEqual(fields["spread_markup_pips"], 0.80)
        self.assertEqual(fields["commission_per_lot"], 3.50)

    def test_phase2_account_also_resolves(self):
        product = make_challenge_product(spread_markup_pips=Decimal("0.50"))
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)
        phase2_account = make_account(account_type="CHALLENGE", balance=Decimal("10000"))
        enrollment.phase2_account = phase2_account
        enrollment.save(update_fields=["phase2_account"])

        fields = cp.resolve_commercial_pricing_fields(phase2_account)
        self.assertEqual(fields["spread_markup_pips"], 0.50)


class RealCreationFlowSnapshotFrozenTests(TestCase):
    """6) snapshot congelado. 7) cambio posterior de producto no altera cuenta."""

    def test_activate_challenge_enrollment_freezes_snapshot(self):
        from simulator import challenge_engine

        product = make_challenge_product(spread_markup_pips=Decimal("1.00"), commission_per_lot=Decimal("5.00"))
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)

        account = challenge_engine.activate_challenge_enrollment(enrollment)
        account.refresh_from_db()

        self.assertIsNotNone(account.commercial_profile_snapshot)
        self.assertEqual(account.commercial_profile_snapshot["spread_markup_pips"], 1.00)
        self.assertEqual(account.commercial_profile_snapshot["source"], cp.SOURCE_CHALLENGE_PRODUCT)

    def test_product_change_after_creation_does_not_alter_existing_account(self):
        from simulator import challenge_engine

        product = make_challenge_product(spread_markup_pips=Decimal("1.00"), commission_per_lot=Decimal("5.00"))
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)
        account = challenge_engine.activate_challenge_enrollment(enrollment)

        # Admin edits the product AFTER the account was created.
        product.spread_markup_pips = Decimal("9.00")
        product.commission_per_lot = Decimal("99.00")
        product.save(update_fields=["spread_markup_pips", "commission_per_lot"])

        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["spread_markup_pips"], 1.00)  # still the frozen value
        self.assertEqual(fields["commission_per_lot"], 5.00)

    def test_advance_to_phase2_freezes_its_own_snapshot(self):
        from simulator import challenge_engine

        product = make_challenge_product(spread_markup_pips=Decimal("2.00"))
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)
        challenge_engine.activate_challenge_enrollment(enrollment)
        phase2_account = challenge_engine.advance_to_phase2(enrollment)
        phase2_account.refresh_from_db()

        self.assertIsNotNone(phase2_account.commercial_profile_snapshot)
        self.assertEqual(phase2_account.commercial_profile_snapshot["spread_markup_pips"], 2.00)

    def test_advance_to_funded_freezes_its_own_snapshot(self):
        from simulator import challenge_engine

        product = make_challenge_product(spread_markup_pips=Decimal("2.50"), commission_per_lot=Decimal("4.00"))
        user = make_user()
        enrollment = make_challenge_enrollment(user=user, product=product)
        challenge_engine.activate_challenge_enrollment(enrollment)
        challenge_engine.advance_to_phase2(enrollment)
        funded_account = challenge_engine.advance_to_funded(enrollment)
        funded_account.refresh_from_db()

        self.assertIsNotNone(funded_account.commercial_profile_snapshot)
        self.assertEqual(funded_account.commercial_profile_snapshot["spread_markup_pips"], 2.50)
        self.assertEqual(funded_account.commercial_profile_snapshot["commission_per_lot"], 4.00)


class LegacyFallbackTests(TestCase):
    """14) legacy fallback. 15) perfil faltante warning."""

    def test_account_with_nothing_resolvable_gets_legacy_fallback(self):
        account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["source"], cp.SOURCE_LEGACY_FALLBACK)
        self.assertEqual(fields["spread_markup_pips"], 0.0)
        self.assertEqual(fields["commission_per_lot"], 0.0)

    def test_missing_profile_logs_structured_warning(self):
        account = make_account(account_type="RETAIL", balance=Decimal("10000"))
        with self.assertLogs("simulator.spread", level="WARNING") as captured:
            cp.resolve_commercial_pricing_fields(account)
        joined = "\n".join(captured.output)
        self.assertIn("event=commercial_pricing_profile_missing", joined)
        self.assertIn(str(account.pk), joined)

    def test_resolved_profile_never_logs(self):
        product = make_account_product(typical_spread_pips=Decimal("1.0"))
        account = make_account(account_type="STANDARD", balance=Decimal("10000"))
        account.commercial_profile_snapshot = cp.commercial_pricing_fields_from_account_product(product)
        account.save(update_fields=["commercial_profile_snapshot"])
        with self.assertNoLogs("simulator.spread", level="WARNING"):
            cp.resolve_commercial_pricing_fields(account)


class HistoricalAccountCompatibilityTests(TestCase):
    """16) cuenta histórica compatible — old flat snapshot fields, no
    commercial_profile_snapshot (pre-SPREAD-04 AccountProduct-linked
    account), no destructive backfill needed."""

    def test_legacy_flat_snapshot_reconstructed_without_backfill(self):
        account = make_account(account_type="STANDARD", balance=Decimal("10000"))
        account.spread_pips_snapshot = Decimal("1.20")
        account.commission_per_lot_snapshot = Decimal("0.00")
        account.save(update_fields=["spread_pips_snapshot", "commission_per_lot_snapshot"])
        self.assertIsNone(account.commercial_profile_snapshot)  # never backfilled

        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["source"], cp.SOURCE_ACCOUNT_SNAPSHOT_LEGACY)
        self.assertEqual(fields["spread_markup_pips"], 1.20)

    def test_new_json_snapshot_preferred_over_legacy_flat_fields(self):
        """If both exist (shouldn't normally happen), the richer JSON wins."""
        account = make_account(account_type="STANDARD", balance=Decimal("10000"))
        account.spread_pips_snapshot = Decimal("1.20")
        account.commercial_profile_snapshot = cp._build_fields(
            profile_id="account_product:99", account_type=None, product_type="STANDARD",
            spread_markup_pips=3.30, commission_per_lot=0.0, commission_pct=0.0,
            min_spread_pips=None, max_spread_pips=None, enabled=True, source=cp.SOURCE_ACCOUNT_PRODUCT,
        )
        account.save(update_fields=["spread_pips_snapshot", "commercial_profile_snapshot"])

        fields = cp.resolve_commercial_pricing_fields(account)
        self.assertEqual(fields["spread_markup_pips"], 3.30)
        self.assertEqual(fields["source"], cp.SOURCE_ACCOUNT_PRODUCT)


class BuildCommercialPricingProfileTests(TestCase):
    """11) floor. 12) ceiling. 13) base + markup."""

    def setUp(self):
        reset_for_tests()

    def tearDown(self):
        reset_for_tests()

    def test_combines_account_markup_with_symbol_base(self):
        """min/max only flow from the symbol's own BrokerSpreadConfig into
        the profile when that row has spread_bounds_enabled=True — the
        opt-in correction made before SPREAD-04's commit."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"),
                            bounds_enabled=True)
        refresh_cache_sync()
        fields = {"spread_markup_pips": 1.0, "commission_per_lot": 0.0, "commission_pct": 0.0,
                  "min_spread_pips": None, "max_spread_pips": None, "profile_id": "x", "source": "x"}
        profile = cp.build_commercial_pricing_profile(fields, "EUR/USD")
        self.assertEqual(profile.spread_markup_pips, 1.0)
        # base_spread_pips is NOT part of the commercial profile itself —
        # it's resolved separately by spread_engine/pricing_context — but
        # min/max come from the symbol's own BrokerSpreadConfig here.
        self.assertEqual(profile.min_spread_pips, 0.50)
        self.assertEqual(profile.max_spread_pips, 5.00)

    def test_symbol_bounds_disabled_profile_min_max_are_none(self):
        """Default (opt-in, not yet enabled): a symbol config with real
        min/max numbers but spread_bounds_enabled=False must not leak into
        the profile — this is the exact bug the pre-commit correction
        fixed (BTCUSD's 15-pip spread silently narrowed to 5)."""
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"))
        refresh_cache_sync()
        fields = {"spread_markup_pips": 1.0, "commission_per_lot": 0.0, "commission_pct": 0.0,
                  "min_spread_pips": None, "max_spread_pips": None, "profile_id": "x", "source": "x"}
        profile = cp.build_commercial_pricing_profile(fields, "EUR/USD")
        self.assertIsNone(profile.min_spread_pips)
        self.assertIsNone(profile.max_spread_pips)

    def test_account_level_override_wins_over_symbol_default(self):
        make_spread_config(symbol="EUR/USD", spread_pips=Decimal("2.00"),
                            min_spread=Decimal("0.50"), max_spread=Decimal("5.00"))
        refresh_cache_sync()
        fields = {"spread_markup_pips": 0.0, "commission_per_lot": 0.0, "commission_pct": 0.0,
                  "min_spread_pips": 1.5, "max_spread_pips": 20.0, "profile_id": "x", "source": "x"}
        profile = cp.build_commercial_pricing_profile(fields, "EUR/USD")
        self.assertEqual(profile.min_spread_pips, 1.5)   # override, not the symbol default 0.50
        self.assertEqual(profile.max_spread_pips, 20.0)  # override, not the symbol default 5.00

    def test_no_config_no_override_min_max_are_none(self):
        fields = {"spread_markup_pips": 0.5, "commission_per_lot": 0.0, "commission_pct": 0.0,
                  "min_spread_pips": None, "max_spread_pips": None, "profile_id": "x", "source": "x"}
        profile = cp.build_commercial_pricing_profile(fields, "GBP/USD")
        self.assertIsNone(profile.min_spread_pips)
        self.assertIsNone(profile.max_spread_pips)

    def test_never_raises_on_malformed_fields(self):
        profile = cp.build_commercial_pricing_profile("not-a-dict", "EUR/USD")
        self.assertEqual(profile.source, cp.SOURCE_LEGACY_FALLBACK)
        self.assertEqual(profile.profile_id, cp.SOURCE_CAPTURE_FAILED)

    def test_empty_fields_dict_is_legacy_fallback(self):
        profile = cp.build_commercial_pricing_profile({}, "EUR/USD")
        self.assertEqual(profile.source, cp.SOURCE_LEGACY_FALLBACK)
        self.assertEqual(profile.spread_markup_pips, 0.0)

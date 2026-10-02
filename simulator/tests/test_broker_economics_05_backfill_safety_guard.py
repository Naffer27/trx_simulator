# simulator/tests/test_broker_economics_05_backfill_safety_guard.py
"""
BROKER-ECONOMICS-05 — Backfill Safety Guard.

Regression coverage for:
  - simulator/models.py::BrokerLedger.economic_date
  - simulator/ib_commission_triggers.py::sweep_trading_commission_revenue_share()
  - simulator/ib_commission_triggers.py::sweep_spread_revenue_share()
  - simulator/ib_commission.py::generate_trading_commission_revenue_share_obligation()
  - simulator/ib_commission.py::generate_spread_revenue_share_obligation()
  - simulator/migrations/0099_broker_economics_05_ledger_economic_date.py
  - all 9 production BrokerLedger writers

Design, per BROKER-ECONOMICS-05 (design) / 05A (migration-safety preflight,
Option B, frozen by the Owner): economic_date is set explicitly by every
writer going forward (never an implicit model default — see models.py's
own field definition, no default= kwarg). Existing/legacy rows are left
with economic_date=NULL permanently — no backfill, no RunPython. NULL is
excluded from both sweeps' economic_date__gte filter by ordinary SQL
NULL-comparison semantics (NULL >= x is never true, identical on SQLite
and PostgreSQL) — no special-case code needed for that exclusion.

Deliberate deviation from a literal reading of the authorization, flagged
explicitly in the 05B implementation report: rule resolution in both
generators still passes at_time=broker_ledger.created_at (full datetime
precision), NOT broker_ledger.economic_date (date-only). Switching to the
date-only value would compare against midnight of that date for
IBCommissionRule.effective_from/effective_until (DateTimeFields),
breaking same-day rate-versioning precision — confirmed by this file's
own test mirroring test_ib_commission_parity_09b.py's existing same-day
rate-change test. Since no backfill tool exists (Option B), economic_date
always equals created_at.date() for every row that can currently reach
either generator, so this loses no real behavior today. The actual safety
property BROKER-ECONOMICS-05 delivers — a row with no economic_date never
generates an obligation, and never passes either sweep's filter — is
fully present and is what this file tests.
"""
import importlib
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.ib_commission import (
    generate_spread_revenue_share_obligation,
    generate_trading_commission_revenue_share_obligation,
)
from simulator.ib_commission_triggers import (
    sweep_spread_revenue_share,
    sweep_trading_commission_revenue_share,
)
from simulator.models import (
    BrokerLedger, IBCommissionObligation, IBCommissionRule, Referral, ReferralAttribution,
)
from simulator.tasks import sweep_ib_commission_triggers_task
from simulator.tests.factories import (
    make_account, make_broker_ledger, make_challenge_enrollment, make_trade, make_user,
)

# ─────────────────────────────────────────────────────────────────────────
# Helpers — same shape as test_ib_commission_triggers_02c.py, duplicated
# locally so this file has no import dependency on that module.
# ─────────────────────────────────────────────────────────────────────────

_seq = 0


def _code():
    global _seq
    _seq += 1
    return f"beg05{_seq}"


def _make_referral(owner=None):
    owner = owner or make_user()
    return Referral.objects.create(user=owner, code=_code())


def _make_attribution(referred_user, referral):
    return ReferralAttribution.objects.create(
        referred_user=referred_user, referral=referral,
        source=ReferralAttribution.SOURCE_SESSION,
    )


def _make_rule(rule_type, referral=None, enabled=True, percentage=None,
               effective_from=None, effective_until=None):
    return IBCommissionRule.objects.create(
        rule_type=rule_type, referral=referral, enabled=enabled,
        fixed_amount=None, percentage=percentage,
        effective_from=effective_from or (timezone.now() - timezone.timedelta(minutes=5)),
        effective_until=effective_until,
    )


_UNSET = object()


def _referred_setup(rule_type, revenue_type, percentage="20.00", amount="7.00", economic_date=_UNSET):
    """Standard attributed trader + global rule + one REV_COMMISSION/REV_SPREAD
    row. economic_date defaults to today via the factory unless overridden
    (pass economic_date=None explicitly to simulate a legacy/undated row)."""
    ib_owner = make_user()
    referral = _make_referral(ib_owner)
    trader = make_user()
    _make_attribution(trader, referral)
    account = make_account(user=trader, balance=Decimal("10000"))
    rule = _make_rule(rule_type, percentage=Decimal(percentage))
    ledger_kwargs = dict(revenue_type=revenue_type, amount=Decimal(amount), source_account=account, symbol="EUR/USD")
    if economic_date is not _UNSET:
        ledger_kwargs["economic_date"] = economic_date
    row = make_broker_ledger(**ledger_kwargs)
    return {"ib_owner": ib_owner, "referral": referral, "trader": trader, "account": account, "rule": rule, "row": row}


# ─────────────────────────────────────────────────────────────────────────
# 1 — recent ledger, economic_date=NULL -> excluded from the sweep
# ─────────────────────────────────────────────────────────────────────────

class NullEconomicDateExcludedFromSweepTests(TestCase):
    def test_recent_commission_row_with_null_economic_date_not_swept(self):
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION,
                               economic_date=None)
        self.assertIsNone(ctx["row"].economic_date)
        result = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(result["generated"], 0)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_recent_spread_row_with_null_economic_date_not_swept(self):
        ctx = _referred_setup(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, BrokerLedger.REV_SPREAD,
                               economic_date=None)
        self.assertIsNone(ctx["row"].economic_date)
        result = sweep_spread_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(result["generated"], 0)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_generator_called_directly_on_null_economic_date_row_returns_none(self):
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION,
                               economic_date=None)
        self.assertIsNone(generate_trading_commission_revenue_share_obligation(ctx["row"]))
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_generator_called_directly_on_null_economic_date_spread_row_returns_none(self):
        ctx = _referred_setup(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, BrokerLedger.REV_SPREAD,
                               economic_date=None)
        self.assertIsNone(generate_spread_revenue_share_obligation(ctx["row"]))
        self.assertEqual(IBCommissionObligation.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 2 — recent ledger, valid economic_date -> normal behavior unaffected
# ─────────────────────────────────────────────────────────────────────────

class ValidEconomicDateNormalBehaviorTests(TestCase):
    def test_recent_commission_row_with_valid_economic_date_swept_normally(self):
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION)
        self.assertEqual(ctx["row"].economic_date, timezone.now().date())
        result = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(result["generated"], 1)
        self.assertEqual(IBCommissionObligation.objects.count(), 1)

    def test_recent_spread_row_with_valid_economic_date_swept_normally(self):
        ctx = _referred_setup(IBCommissionRule.RULE_SPREAD_REVENUE_SHARE, BrokerLedger.REV_SPREAD)
        self.assertEqual(ctx["row"].economic_date, timezone.now().date())
        result = sweep_spread_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(result["generated"], 1)
        self.assertEqual(IBCommissionObligation.objects.count(), 1)


# ─────────────────────────────────────────────────────────────────────────
# 3 — historical ledger cannot become eligible solely via created_at
# ─────────────────────────────────────────────────────────────────────────

class HistoricalRowCannotBecomeEligibleTests(TestCase):
    def test_backdated_created_at_with_null_economic_date_still_excluded(self):
        """Simulates the exact backfill scenario BROKER-ECONOMICS-05
        exists to guard against: a row inserted with a historical
        created_at (as a careless/legacy backfill script might attempt)
        but with economic_date left unset. Even though created_at is
        deliberately set OUTSIDE the window (proving this isn't just
        passing by accident), the row must never be swept."""
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION,
                               economic_date=None)
        old_created_at = timezone.now() - timezone.timedelta(days=90)
        BrokerLedger.objects.filter(pk=ctx["row"].pk).update(created_at=old_created_at)
        ctx["row"].refresh_from_db()
        self.assertIsNone(ctx["row"].economic_date)
        # A wide-enough window that WOULD catch it by created_at alone.
        result = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(days=100))
        self.assertEqual(result["generated"], 0, "a NULL-economic_date row must never be swept, regardless of created_at")
        self.assertEqual(IBCommissionObligation.objects.count(), 0)

    def test_recent_created_at_cannot_substitute_for_missing_economic_date(self):
        """The inverse: created_at IS inside the window (this is what the
        OLD, pre-05 code would have swept), but economic_date is NULL —
        must still be excluded. This is the row shape a future careless
        backfill script using plain .create() would actually produce."""
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION,
                               economic_date=None)
        self.assertIsNone(ctx["row"].economic_date)
        result = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=5))
        self.assertEqual(result["generated"], 0)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 4 — all 9 writers populate economic_date
# ─────────────────────────────────────────────────────────────────────────

class AllWritersPopulateEconomicDateTests(TestCase):
    """Two complementary techniques, matched to each writer's test-setup
    cost: a direct functional call for the three writers with cheap,
    self-contained setup (BOOK-02, broker_economic_adjustment,
    challenge_revenue), and a source-inspection check — the same
    technique test_o6c1aa_unified_raw_execution_spread_fee.py already
    uses elsewhere in this suite — for the remaining writers, whose
    realistic setup (wallet debits, funded accounts/configs, provider
    cost normalization gating) is already exercised end-to-end by each
    writer's own dedicated test file; duplicating that setup here would
    test the fixture, not the economic_date line."""

    def test_book02_counterparty_pnl_writer_sets_economic_date(self):
        from simulator.broker_ledger import create_broker_counterparty_entry
        account = make_account(balance=Decimal("10000"))
        trade = make_trade(account, profit_loss=Decimal("10.00"))
        entry = create_broker_counterparty_entry(trade, account, Decimal("-10.00"), "manual", book_mode="B_BOOK")
        self.assertEqual(entry.economic_date, timezone.now().date())

    def test_consumers_commission_and_spread_writers_set_economic_date(self):
        import inspect

        from simulator import consumers
        src = inspect.getsource(consumers)
        # All 3 consumers.py BrokerLedger.objects.create(...) call sites
        # (REV_COMMISSION manual-open, REV_COMMISSION live-open,
        # REV_SPREAD live-open) must each set economic_date explicitly.
        occurrences = src.count("economic_date=timezone.now().date()")
        self.assertGreaterEqual(occurrences, 3, "all 3 consumers.py BrokerLedger writers must set economic_date")

    def test_broker_economic_adjustment_writer_sets_economic_date(self):
        from simulator.broker_economic_adjustment import create_broker_economic_adjustment
        account = make_account(balance=Decimal("10000"))
        actor = make_user(is_staff=True, is_superuser=True)
        adjustment = create_broker_economic_adjustment(
            amount=Decimal("5.00"), reason="test", actor=actor,
            idempotency_key="beg05-test-1", source_account=account,
        )
        ledger_row = adjustment.created_ledger_entry
        self.assertIsNotNone(ledger_row)
        self.assertEqual(ledger_row.economic_date, timezone.now().date())

    def test_challenge_revenue_writer_sets_economic_date(self):
        from simulator.challenge_revenue import record_challenge_fee_revenue
        enrollment = make_challenge_enrollment()
        row = record_challenge_fee_revenue(enrollment)
        self.assertEqual(row.economic_date, timezone.now().date())

    def test_withdrawal_economics_writer_sets_economic_date_by_source(self):
        import inspect

        from simulator import withdrawal_economics
        src = inspect.getsource(withdrawal_economics.record_withdrawal_fee_revenue)
        self.assertIn("economic_date=timezone.now().date()", src)

    def test_funded_economics_writer_sets_economic_date_by_source(self):
        import inspect

        from simulator import funded_economics
        src = inspect.getsource(funded_economics.record_funded_broker_cut_revenue)
        self.assertIn("economic_date=timezone.now().date()", src)

    def test_provider_cost_economics_writer_sets_economic_date_by_source(self):
        import inspect

        from simulator import provider_cost_economics
        src = inspect.getsource(provider_cost_economics)
        self.assertIn("economic_date=timezone.now().date()", src)


# ─────────────────────────────────────────────────────────────────────────
# 5 — NULL excluded by the economic filter (direct SQL-semantics pin)
# ─────────────────────────────────────────────────────────────────────────

class NullSqlSemanticsPinnedTests(TestCase):
    def test_null_economic_date_never_matches_gte_filter(self):
        """Pins the exact SQL behavior this whole design depends on, so a
        future Django/DB upgrade that somehow changed NULL-filter
        semantics would be caught here first, not in production."""
        account = make_account(balance=Decimal("10000"))
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("1.00"),
                            source_account=account, economic_date=None)
        make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("1.00"),
                            source_account=account, economic_date=timezone.now().date())
        matching = BrokerLedger.objects.filter(
            revenue_type=BrokerLedger.REV_COMMISSION, economic_date__gte=timezone.now().date() - timezone.timedelta(days=365),
        )
        self.assertEqual(matching.count(), 1, "NULL economic_date must never satisfy a __gte filter")


# ─────────────────────────────────────────────────────────────────────────
# 6 — existing idempotency continues to function
# ─────────────────────────────────────────────────────────────────────────

class IdempotencyUnaffectedTests(TestCase):
    def test_repeated_sweep_of_valid_row_still_no_duplicate(self):
        _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION)
        r1 = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        r2 = sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        self.assertEqual(r1["generated"], 1)
        self.assertEqual(r2["generated"], 0)
        self.assertEqual(IBCommissionObligation.objects.count(), 1)

    def test_repeated_generator_call_on_null_row_stays_none_no_duplicate(self):
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION,
                               economic_date=None)
        ob1 = generate_trading_commission_revenue_share_obligation(ctx["row"])
        ob2 = generate_trading_commission_revenue_share_obligation(ctx["row"])
        self.assertIsNone(ob1)
        self.assertIsNone(ob2)
        self.assertEqual(IBCommissionObligation.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────
# 7 — existing obligations are never modified
# ─────────────────────────────────────────────────────────────────────────

class ExistingObligationsUntouchedTests(TestCase):
    def test_pre_existing_obligation_fields_unchanged_after_this_guard_runs(self):
        ctx = _referred_setup(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE, BrokerLedger.REV_COMMISSION)
        obligation = generate_trading_commission_revenue_share_obligation(ctx["row"])
        snapshot = (obligation.calculated_amount, obligation.applied_percentage_rate, obligation.status)
        # Re-running the sweep (as Celery Beat would every 5 minutes) must
        # not alter the existing obligation in any way.
        sweep_trading_commission_revenue_share(timezone.now() - timezone.timedelta(minutes=60))
        obligation.refresh_from_db()
        self.assertEqual(
            (obligation.calculated_amount, obligation.applied_percentage_rate, obligation.status),
            snapshot,
        )


# ─────────────────────────────────────────────────────────────────────────
# 8 — rate-versioning precision preserved (confirms the deliberate
#     created_at-for-rule-resolution deviation documented at module top)
# ─────────────────────────────────────────────────────────────────────────

class SameDayRateVersioningPrecisionPreservedTests(TestCase):
    def test_same_day_rate_change_still_resolves_precisely(self):
        """Mirrors test_ib_commission_parity_09b.py's own same-day
        rate-change test. If rule resolution had been switched to
        economic_date (date-only) instead of created_at, this would
        fail: a new rule with effective_from=now (this afternoon, say)
        would compare against midnight-today and be incorrectly
        excluded. Proves the 05B implementation's documented deviation
        is correct and necessary."""
        from django.db import transaction
        trader = make_user()
        referral = _make_referral()
        _make_attribution(trader, referral)
        account = make_account(user=trader, balance=Decimal("10000"))
        rule_v1 = _make_rule(IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
                              referral=referral, percentage=Decimal("20.00"))
        row1 = make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("8.00"),
                                   source_account=account, symbol="EUR/USD")
        ob1 = generate_trading_commission_revenue_share_obligation(row1)
        self.assertEqual(ob1.applied_percentage_rate, Decimal("20.00"))

        now = timezone.now()
        with transaction.atomic():
            rule_v1.effective_until = now
            rule_v1.save(update_fields=["effective_until"])
            IBCommissionRule.objects.create(
                rule_type=IBCommissionRule.RULE_TRADING_COMMISSION_REVENUE_SHARE,
                referral=referral, enabled=True, fixed_amount=None, percentage=Decimal("50.00"),
                effective_from=now, effective_until=None,
            )

        row2 = make_broker_ledger(revenue_type=BrokerLedger.REV_COMMISSION, amount=Decimal("8.00"),
                                   source_account=account, symbol="EUR/USD")
        ob2 = generate_trading_commission_revenue_share_obligation(row2)
        self.assertIsNotNone(ob2, "same-day rate change must still resolve correctly post-05")
        self.assertEqual(ob2.applied_percentage_rate, Decimal("50.00"))


# ─────────────────────────────────────────────────────────────────────────
# 9 — migration contains only AddField, no RunPython / data migration
# ─────────────────────────────────────────────────────────────────────────

class MigrationContainsOnlyAddFieldTests(TestCase):
    def test_broker_economics_05_migration_has_no_data_migration(self):
        module = importlib.import_module(
            "simulator.migrations.0099_broker_economics_05_ledger_economic_date"
        )
        operations = module.Migration.operations
        self.assertEqual(len(operations), 1, "must be exactly one operation")
        from django.db import migrations as dj_migrations
        self.assertIsInstance(operations[0], dj_migrations.AddField)
        self.assertEqual(operations[0].model_name, "brokerledger")
        self.assertEqual(operations[0].name, "economic_date")
        self.assertTrue(operations[0].field.null)
        # No RunPython anywhere in this migration file.
        self.assertFalse(any(isinstance(op, dj_migrations.RunPython) for op in operations))


# ─────────────────────────────────────────────────────────────────────────
# 10 — existing broker-economics / IB-trigger tests still pass unmodified
#      (not re-asserted here — see the 05B implementation report for the
#      full-suite + targeted-suite run results; this file only adds new,
#      additive coverage, it does not change any existing test's
#      expectations other than the two raw .create() call sites in
#      test_ib_commission_parity_09b.py that needed economic_date added
#      to keep simulating a real writer under the new guard.)
# ─────────────────────────────────────────────────────────────────────────

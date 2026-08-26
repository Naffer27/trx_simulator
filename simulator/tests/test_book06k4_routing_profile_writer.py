"""
BOOK-06k.4 — Routing Profile V2 Writer tests.

DB-level tests (TestCase, real TraderScore fixtures) — the writer is
deliberately impure (persists to DB). Query-count assertions are
architectural invariants of this block — see
routing_profile_writer.py's own docstrings.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from simulator.models import TraderScore
from simulator.routing_profile_writer import (
    RoutingProfileWriterError,
    apply_routing_profile_evaluation,
)

from .factories import make_account


def _make_trader_score(**overrides):
    account = make_account()
    defaults = dict(account=account)
    defaults.update(overrides)
    return TraderScore.objects.create(**defaults)


def _evaluation_result(**overrides):
    base = {
        "candidate_routing_profile": "INTERNAL",
        "candidate_streak": 1,
        "v2_routing_profile_proposed": None,
        "evidence_sufficient": True,
        "reason_code": "STREAK_RESET",
        "engine_version": 1,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────
class ValidationTests(TestCase):

    def test_trader_score_wrong_type_raises(self):
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score="not a TraderScore", evaluation_result=_evaluation_result(),
                evidence_snapshot={},
            )

    def test_evaluation_result_wrong_type_raises(self):
        ts = _make_trader_score()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=["not", "a", "dict"], evidence_snapshot={},
            )

    def test_missing_key_raises(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        del result["reason_code"]
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})

    def test_invalid_candidate_profile_rejected(self):
        ts = _make_trader_score()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts,
                evaluation_result=_evaluation_result(candidate_routing_profile="NOT_REAL"),
                evidence_snapshot={},
            )

    def test_none_candidate_profile_accepted(self):
        ts = _make_trader_score()
        result = _evaluation_result(candidate_routing_profile=None, evidence_sufficient=False,
                                     reason_code="INSUFFICIENT_EVIDENCE_TRADE_COUNT")
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        self.assertIsInstance(r, dict)  # must not raise

    def test_none_proposed_profile_accepted(self):
        ts = _make_trader_score()
        result = _evaluation_result(v2_routing_profile_proposed=None)
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        self.assertIsInstance(r, dict)

    def test_invalid_proposed_profile_rejected(self):
        ts = _make_trader_score()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts,
                evaluation_result=_evaluation_result(v2_routing_profile_proposed="NOT_REAL"),
                evidence_snapshot={},
            )

    def test_negative_streak_rejected(self):
        ts = _make_trader_score()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=_evaluation_result(candidate_streak=-1),
                evidence_snapshot={},
            )

    def test_bool_streak_rejected(self):
        ts = _make_trader_score()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=_evaluation_result(candidate_streak=True),
                evidence_snapshot={},
            )

    def test_zero_streak_accepted(self):
        ts = _make_trader_score()
        result = _evaluation_result(candidate_streak=0, candidate_routing_profile=None,
                                     evidence_sufficient=False, reason_code="INSUFFICIENT_EVIDENCE_WINDOW")
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        self.assertIsInstance(r, dict)

    def test_engine_version_zero_rejected(self):
        ts = _make_trader_score()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=_evaluation_result(engine_version=0),
                evidence_snapshot={},
            )


# ─────────────────────────────────────────────────────────────────────────
# Candidate change
# ─────────────────────────────────────────────────────────────────────────
class CandidateChangeTests(TestCase):

    def test_candidate_change_persisted(self):
        ts = _make_trader_score()
        result = _evaluation_result(candidate_routing_profile="HEDGE_CANDIDATE", candidate_streak=1)
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertEqual(ts.candidate_routing_profile, "HEDGE_CANDIDATE")
        self.assertEqual(ts.candidate_streak, 1)
        self.assertTrue(r["candidate_changed"])
        self.assertFalse(r["profile_changed"])

    def test_candidate_only_does_not_touch_changed_at(self):
        ts = _make_trader_score(v2_routing_profile="INTERNAL")
        before = ts.v2_routing_profile_changed_at
        result = _evaluation_result(candidate_routing_profile="REVIEW", v2_routing_profile_proposed="INTERNAL")
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertEqual(ts.v2_routing_profile_changed_at, before)


# ─────────────────────────────────────────────────────────────────────────
# Profile transition / changed_at
# ─────────────────────────────────────────────────────────────────────────
class ProfileTransitionTests(TestCase):

    def test_first_none_to_profile_transition(self):
        ts = _make_trader_score()  # v2_routing_profile defaults to None
        self.assertIsNone(ts.v2_routing_profile)
        self.assertIsNone(ts.v2_routing_profile_changed_at)
        result = _evaluation_result(v2_routing_profile_proposed="INTERNAL",
                                     reason_code="STREAK_CONFIRMED_TRANSITION")
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertEqual(ts.v2_routing_profile, "INTERNAL")
        self.assertIsNotNone(ts.v2_routing_profile_changed_at)
        self.assertTrue(r["profile_changed"])

    def test_none_to_none_no_transition(self):
        ts = _make_trader_score()
        result = _evaluation_result(v2_routing_profile_proposed=None)
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertIsNone(ts.v2_routing_profile)
        self.assertIsNone(ts.v2_routing_profile_changed_at)
        self.assertFalse(r["profile_changed"])

    def test_real_transition_between_profiles(self):
        ts = _make_trader_score(v2_routing_profile="INTERNAL")
        result = _evaluation_result(v2_routing_profile_proposed="HEDGE_CANDIDATE")
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertEqual(ts.v2_routing_profile, "HEDGE_CANDIDATE")
        self.assertIsNotNone(ts.v2_routing_profile_changed_at)
        self.assertTrue(r["profile_changed"])

    def test_changed_at_only_on_profile_transition_not_on_reconfirmation(self):
        ts = _make_trader_score(v2_routing_profile="INTERNAL")
        ts.v2_routing_profile_changed_at = timezone.now() - timedelta(days=5)
        ts.save(update_fields=["v2_routing_profile_changed_at"])
        stamp = ts.v2_routing_profile_changed_at
        result = _evaluation_result(v2_routing_profile_proposed="INTERNAL",
                                     reason_code="STREAK_CONFIRMED_NO_CHANGE")
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertEqual(ts.v2_routing_profile_changed_at, stamp)


# ─────────────────────────────────────────────────────────────────────────
# Evidence freeze (null-state) — locked design case
# ─────────────────────────────────────────────────────────────────────────
class EvidenceFreezeTests(TestCase):

    def test_initial_none_freeze_with_new_snapshot_writes_only_snapshot_and_version(self):
        ts = _make_trader_score()
        self.assertIsNone(ts.candidate_routing_profile)
        self.assertEqual(ts.candidate_streak, 0)
        self.assertIsNone(ts.v2_routing_profile)
        self.assertEqual(ts.routing_profile_evidence_snapshot, {})
        self.assertEqual(ts.routing_profile_engine_version, 0)

        result = _evaluation_result(
            candidate_routing_profile=None, candidate_streak=0, v2_routing_profile_proposed=None,
            evidence_sufficient=False, reason_code="INSUFFICIENT_EVIDENCE_ACCOUNT_AGE",
        )
        snapshot = {"evidence": {"lifetime_trade_count": 1}}
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)

        self.assertTrue(r["written"])
        self.assertFalse(r["candidate_changed"])
        self.assertFalse(r["profile_changed"])
        self.assertTrue(r["snapshot_changed"])
        self.assertTrue(r["engine_version_changed"])
        self.assertEqual(set(r["fields_written"]),
                          {"routing_profile_evidence_snapshot", "routing_profile_engine_version"})

        ts.refresh_from_db()
        self.assertIsNone(ts.candidate_routing_profile)
        self.assertIsNone(ts.v2_routing_profile)
        self.assertEqual(ts.routing_profile_evidence_snapshot, {"evidence": {"lifetime_trade_count": 1}})
        self.assertEqual(ts.routing_profile_engine_version, 1)


# ─────────────────────────────────────────────────────────────────────────
# Exact replay / idempotency
# ─────────────────────────────────────────────────────────────────────────
class ExactReplayTests(TestCase):

    def test_exact_replay_is_a_true_noop(self):
        ts = _make_trader_score()
        result = _evaluation_result(
            candidate_routing_profile=None, candidate_streak=0, v2_routing_profile_proposed=None,
            evidence_sufficient=False, reason_code="INSUFFICIENT_EVIDENCE_ACCOUNT_AGE",
        )
        snapshot = {"evidence": {"lifetime_trade_count": 1}}
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        ts.refresh_from_db()

        r2 = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        self.assertFalse(r2["written"])
        self.assertEqual(r2["fields_written"], ())

    def test_exact_replay_query_count_zero(self):
        ts = _make_trader_score()
        result = _evaluation_result(candidate_routing_profile="INTERNAL", candidate_streak=1)
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={"a": 1})
        ts.refresh_from_db()
        with self.assertNumQueries(0):
            apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={"a": 1})


# ─────────────────────────────────────────────────────────────────────────
# Query contracts
# ─────────────────────────────────────────────────────────────────────────
class QueryContractTests(TestCase):

    def test_confirmed_transition_one_query(self):
        ts = _make_trader_score(v2_routing_profile="INTERNAL")
        result = _evaluation_result(v2_routing_profile_proposed="HEDGE_CANDIDATE")
        with self.assertNumQueries(1):
            apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})

    def test_candidate_streak_only_one_query(self):
        ts = _make_trader_score(candidate_routing_profile="INTERNAL", candidate_streak=1)
        result = _evaluation_result(candidate_routing_profile="INTERNAL", candidate_streak=2)
        with self.assertNumQueries(1):
            apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})

    def test_snapshot_only_one_query(self):
        ts = _make_trader_score(candidate_routing_profile="INTERNAL", candidate_streak=1,
                                 routing_profile_engine_version=1, routing_profile_evidence_snapshot={"a": 1})
        result = _evaluation_result(candidate_routing_profile="INTERNAL", candidate_streak=1)
        with self.assertNumQueries(1):
            r = apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=result, evidence_snapshot={"a": 2},
            )
        self.assertEqual(r["fields_written"], ("routing_profile_evidence_snapshot",))

    def test_engine_version_only_one_query(self):
        ts = _make_trader_score(candidate_routing_profile="INTERNAL", candidate_streak=1,
                                 routing_profile_engine_version=1)
        result = _evaluation_result(candidate_routing_profile="INTERNAL", candidate_streak=1, engine_version=2)
        with self.assertNumQueries(1):
            r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        self.assertEqual(r["fields_written"], ("routing_profile_engine_version",))

    def test_initial_freeze_one_query(self):
        ts = _make_trader_score()
        result = _evaluation_result(
            candidate_routing_profile=None, candidate_streak=0, v2_routing_profile_proposed=None,
            evidence_sufficient=False, reason_code="INSUFFICIENT_EVIDENCE_TRADE_COUNT",
        )
        with self.assertNumQueries(1):
            apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={"x": 1})


# ─────────────────────────────────────────────────────────────────────────
# Normalization
# ─────────────────────────────────────────────────────────────────────────
class NormalizationTests(TestCase):

    def test_decimal_normalized_to_str(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        snapshot = {"exposure": {"gross_notional": Decimal("1234.56")}}
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        ts.refresh_from_db()
        self.assertEqual(ts.routing_profile_evidence_snapshot["exposure"]["gross_notional"], "1234.56")

    def test_nested_dict_normalized(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        snapshot = {"a": {"b": {"c": Decimal("1")}}}
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        ts.refresh_from_db()
        self.assertEqual(ts.routing_profile_evidence_snapshot, {"a": {"b": {"c": "1"}}})

    def test_tuple_normalized_to_list(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        snapshot = {"pair": (Decimal("1"), Decimal("2"))}
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        ts.refresh_from_db()
        self.assertEqual(ts.routing_profile_evidence_snapshot, {"pair": ["1", "2"]})

    def test_input_snapshot_not_mutated(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        snapshot = {"x": Decimal("1"), "nested": {"y": Decimal("2")}}
        snapshot_copy = {"x": Decimal("1"), "nested": {"y": Decimal("2")}}
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        self.assertEqual(snapshot, snapshot_copy)
        self.assertIsInstance(snapshot["x"], Decimal)  # still Decimal, never mutated in place

    def test_datetime_rejected(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=result, evidence_snapshot={"x": datetime.now()},
            )

    def test_date_rejected(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=result, evidence_snapshot={"x": date.today()},
            )

    def test_set_rejected(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=result, evidence_snapshot={"x": {1, 2, 3}},
            )

    def test_custom_object_rejected(self):
        class _Custom:
            pass

        ts = _make_trader_score()
        result = _evaluation_result()
        with self.assertRaises(RoutingProfileWriterError):
            apply_routing_profile_evaluation(
                trader_score=ts, evaluation_result=result, evidence_snapshot={"x": _Custom()},
            )

    def test_none_bool_int_str_preserved(self):
        ts = _make_trader_score()
        result = _evaluation_result()
        snapshot = {"a": None, "b": True, "c": 5, "d": "text"}
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot=snapshot)
        ts.refresh_from_db()
        self.assertEqual(ts.routing_profile_evidence_snapshot, snapshot)


# ─────────────────────────────────────────────────────────────────────────
# Legacy untouched, DB error propagation
# ─────────────────────────────────────────────────────────────────────────
class LegacyUntouchedTests(TestCase):

    def test_legacy_routing_profile_never_modified(self):
        ts = _make_trader_score(routing_profile="INTERNAL", trader_class="NORMAL")
        result = _evaluation_result(candidate_routing_profile="HEDGE_CANDIDATE",
                                     v2_routing_profile_proposed="HEDGE_CANDIDATE")
        apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        ts.refresh_from_db()
        self.assertEqual(ts.routing_profile, "INTERNAL")
        self.assertEqual(ts.trader_class, "NORMAL")

    def test_db_error_propagates(self):
        ts = _make_trader_score()
        ts.pk = 999999999  # forces the UPDATE to match zero rows... use delete instead for a real DB error
        ts_real = _make_trader_score()
        ts_real.delete()
        result = _evaluation_result()
        # Saving a deleted instance's stale pk still issues an UPDATE affecting 0 rows (no exception in
        # Django by default) — to force a genuine DB-level exception, break constraint via an invalid FK.
        ts_real.account_id = 999999999
        with self.assertRaises(Exception):
            apply_routing_profile_evaluation(trader_score=ts_real, evaluation_result=result, evidence_snapshot={})


# ─────────────────────────────────────────────────────────────────────────
# fields_written accuracy
# ─────────────────────────────────────────────────────────────────────────
class FieldsWrittenTests(TestCase):

    def test_fields_written_exact_for_candidate_only(self):
        ts = _make_trader_score(routing_profile_engine_version=1)
        result = _evaluation_result(candidate_routing_profile="REVIEW", candidate_streak=1)
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        self.assertEqual(set(r["fields_written"]), {"candidate_routing_profile", "candidate_streak"})

    def test_fields_written_includes_both_profile_and_changed_at_on_transition(self):
        ts = _make_trader_score()
        result = _evaluation_result(v2_routing_profile_proposed="INTERNAL")
        r = apply_routing_profile_evaluation(trader_score=ts, evaluation_result=result, evidence_snapshot={})
        self.assertIn("v2_routing_profile", r["fields_written"])
        self.assertIn("v2_routing_profile_changed_at", r["fields_written"])

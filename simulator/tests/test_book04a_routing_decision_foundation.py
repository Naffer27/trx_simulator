"""
BOOK-04a — Routing Decision Foundation tests.

Foundation only: the RoutingDecision model, its writer
(record_routing_decision), and the Book/version constants in
simulator/routing_engine.py. No integration with the Trading Engine
exists yet, so there is deliberately no test here that opens or closes
a position, touches consumers.py, or asserts anything about
Position/Trade/BrokerLedger/BrokerAuditEvent — that is BOOK-04b/04c/04d
scope, not this block's.
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from unittest.mock import patch

from simulator.models import RoutingDecision
from simulator.routing_engine import (
    Book,
    ENGINE_VERSION,
    SCHEMA_VERSION,
    record_routing_decision,
)

User = get_user_model()


class RoutingDecisionModelTests(TestCase):
    """Model creation, defaults, and field behavior."""

    def test_creates_with_all_fields(self):
        parent = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="PARENT")
        staff = User.objects.create_user(username="staff1", password="x")
        corr = uuid.uuid4()

        decision = RoutingDecision.objects.create(
            book=Book.INTERNAL,
            reason_code="TRIVIAL_INTERNAL_DEFAULT",
            reason_message="100% internal book, no rule active yet.",
            engine_version=1,
            schema_version=1,
            inputs_snapshot={"symbol": "EURUSD", "qty": 1.0},
            external_reference="ext-ref-123",
            parent_decision=parent,
            override_by=staff,
            override_reason="manual test override",
            correlation_id=corr,
        )

        self.assertIsNotNone(decision.decision_id)
        self.assertEqual(decision.book, Book.INTERNAL)
        self.assertEqual(decision.reason_code, "TRIVIAL_INTERNAL_DEFAULT")
        self.assertEqual(decision.parent_decision_id, parent.id)
        self.assertEqual(decision.override_by_id, staff.id)
        self.assertEqual(decision.correlation_id, corr)
        self.assertIsNotNone(decision.decided_at)

    def test_minimal_required_fields(self):
        decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="MINIMAL")

        self.assertEqual(decision.reason_message, "")
        self.assertEqual(decision.engine_version, 1)
        self.assertEqual(decision.schema_version, 1)
        self.assertEqual(decision.inputs_snapshot, {})
        self.assertEqual(decision.external_reference, "")
        self.assertIsNone(decision.parent_decision)
        self.assertIsNone(decision.override_by)
        self.assertEqual(decision.override_reason, "")
        self.assertIsNone(decision.correlation_id)

    def test_decision_id_auto_generated_and_unique(self):
        d1 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="A")
        d2 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="B")

        self.assertIsNotNone(d1.decision_id)
        self.assertIsNotNone(d2.decision_id)
        self.assertNotEqual(d1.decision_id, d2.decision_id)

    def test_schema_version_and_engine_version_are_independent_axes(self):
        same_engine_diff_schema = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="X", engine_version=1, schema_version=2,
        )
        diff_engine_same_schema = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="X", engine_version=2, schema_version=2,
        )

        self.assertEqual(same_engine_diff_schema.engine_version, 1)
        self.assertEqual(same_engine_diff_schema.schema_version, 2)
        self.assertEqual(diff_engine_same_schema.engine_version, 2)
        self.assertEqual(diff_engine_same_schema.schema_version, 2)

    def test_parent_decision_self_fk_set_null_on_delete(self):
        parent = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="PARENT")
        child = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="CHILD", parent_decision=parent,
        )
        self.assertEqual(list(parent.child_decisions.all()), [child])

        parent.delete()
        child.refresh_from_db()
        self.assertIsNone(child.parent_decision_id)

    def test_override_by_fk_set_null_on_user_delete(self):
        staff = User.objects.create_user(username="staff2", password="x")
        decision = RoutingDecision.objects.create(
            book=Book.INTERNAL, reason_code="OVERRIDDEN", override_by=staff,
        )
        staff.delete()
        decision.refresh_from_db()
        self.assertIsNone(decision.override_by_id)

    def test_correlation_id_optional(self):
        decision = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="NO_CORR")
        self.assertIsNone(decision.correlation_id)

    def test_inputs_snapshot_default_is_empty_dict_not_shared_mutable(self):
        d1 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="A")
        d2 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="B")
        d1.inputs_snapshot["mutated"] = True
        d1.save(update_fields=["inputs_snapshot"])
        d2.refresh_from_db()
        self.assertEqual(d2.inputs_snapshot, {})

    def test_default_ordering_is_newest_first(self):
        d1 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="FIRST")
        d2 = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="SECOND")
        ordered = list(RoutingDecision.objects.all())
        self.assertEqual(ordered[0].id, d2.id)
        self.assertEqual(ordered[1].id, d1.id)

    def test_book_is_plain_string_no_db_level_choices(self):
        field = RoutingDecision._meta.get_field("book")
        self.assertIsNone(field.choices)


class BookAndVersionConstantsTests(TestCase):
    """Book/ENGINE_VERSION/SCHEMA_VERSION constants in routing_engine.py."""

    def test_book_internal_value(self):
        self.assertEqual(Book.INTERNAL, "INTERNAL")

    def test_book_all_contains_internal(self):
        self.assertIn(Book.INTERNAL, Book.ALL)

    def test_engine_and_schema_version_start_at_one(self):
        self.assertEqual(ENGINE_VERSION, 1)
        self.assertEqual(SCHEMA_VERSION, 1)


class RecordRoutingDecisionWriterTests(TestCase):
    """record_routing_decision() — same contract as broker_audit.record_event()."""

    def test_creates_row_and_returns_it(self):
        decision = record_routing_decision(
            book=Book.INTERNAL,
            reason_code="TRIVIAL_INTERNAL_DEFAULT",
            reason_message="always internal today",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(RoutingDecision.objects.count(), 1)
        self.assertEqual(decision.book, Book.INTERNAL)
        self.assertEqual(decision.reason_code, "TRIVIAL_INTERNAL_DEFAULT")

    def test_defaults_engine_and_schema_version_when_not_specified(self):
        decision = record_routing_decision(book=Book.INTERNAL, reason_code="X")
        self.assertEqual(decision.engine_version, ENGINE_VERSION)
        self.assertEqual(decision.schema_version, SCHEMA_VERSION)

    def test_inputs_snapshot_persisted_verbatim(self):
        snapshot = {"symbol": "EURUSD", "side": "BUY", "qty": 2.5}
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="X", inputs_snapshot=snapshot,
        )
        decision.refresh_from_db()
        self.assertEqual(decision.inputs_snapshot, snapshot)

    def test_accepts_parent_decision_object(self):
        parent = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="PARENT")
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="CHILD", parent_decision=parent,
        )
        self.assertEqual(decision.parent_decision_id, parent.id)

    def test_accepts_parent_decision_id(self):
        parent = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="PARENT")
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="CHILD", parent_decision_id=parent.id,
        )
        self.assertEqual(decision.parent_decision_id, parent.id)

    def test_accepts_override_by_object(self):
        staff = User.objects.create_user(username="staff3", password="x")
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="X", override_by=staff,
        )
        self.assertEqual(decision.override_by_id, staff.id)

    def test_accepts_override_by_id(self):
        staff = User.objects.create_user(username="staff4", password="x")
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="X", override_by_id=staff.id,
        )
        self.assertEqual(decision.override_by_id, staff.id)

    def test_correlation_id_passed_through(self):
        corr = uuid.uuid4()
        decision = record_routing_decision(
            book=Book.INTERNAL, reason_code="X", correlation_id=corr,
        )
        self.assertEqual(decision.correlation_id, corr)

    def test_missing_required_kwargs_raises(self):
        with self.assertRaises(TypeError):
            record_routing_decision(reason_code="X")   # missing `book`
        with self.assertRaises(TypeError):
            record_routing_decision(book=Book.INTERNAL)   # missing `reason_code`

    def test_fail_open_returns_none_on_write_failure(self):
        with patch(
            "simulator.models.RoutingDecision.objects.create",
            side_effect=Exception("simulated DB failure"),
        ):
            result = record_routing_decision(book=Book.INTERNAL, reason_code="WILL_FAIL")
        self.assertIsNone(result)
        self.assertEqual(RoutingDecision.objects.count(), 0)

    def test_fail_open_never_raises(self):
        with patch(
            "simulator.models.RoutingDecision.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            try:
                record_routing_decision(book=Book.INTERNAL, reason_code="WILL_FAIL")
            except Exception as exc:   # pragma: no cover - test fails via assertion below
                self.fail(f"record_routing_decision() raised unexpectedly: {exc!r}")

    def test_writer_failure_does_not_poison_a_live_outer_atomic_block(self):
        """
        Automated reproduction of the scenario manually verified before
        BOOK-04a's commit review: a writer failure must be contained by
        its own nested savepoint and must never mark a caller's own
        already-open transaction.atomic() block as broken — the same
        guarantee record_event() relies on when called from inside
        _db_open_position_atomic()'s transaction.

        TestCase already wraps this whole test in its own outer atomic
        block (rolled back automatically in tearDown), so the explicit
        `with transaction.atomic():` below reproduces a THIRD level of
        nesting on top of that: [TestCase's transaction [this test's own
        "caller" transaction [the writer's internal savepoint]]] — the
        same shape a future real caller (e.g. BOOK-04b, inside its own
        already-open transaction) would have around this writer.
        """
        with transaction.atomic():
            before = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="OUTER_BEFORE")

            with patch(
                "simulator.models.RoutingDecision.objects.create",
                side_effect=Exception("simulated immediate DB failure"),
            ):
                # No try/except here on purpose: if the writer let this
                # exception propagate, it would abort this `with
                # transaction.atomic():` block right here and the test
                # would ERROR (not just fail an assertion) — the absence
                # of an error is itself proof requirement (2) holds.
                result = record_routing_decision(book=Book.INTERNAL, reason_code="WILL_FAIL_INSIDE_OUTER")

            # (1) the writer reports failure unambiguously.
            self.assertIsNone(result)

            # (3)/(4) — if the writer's internal savepoint had left this
            # outer transaction.atomic() marked as needing a rollback,
            # ANY further ORM write here would raise
            # TransactionManagementError instead of succeeding.
            after = RoutingDecision.objects.create(book=Book.INTERNAL, reason_code="OUTER_AFTER")

        # The outer `with transaction.atomic():` above committed cleanly
        # (no exception reached this point) — both markers exist, and no
        # row was ever created for the failed call in between them.
        self.assertTrue(RoutingDecision.objects.filter(pk=before.pk).exists())
        self.assertTrue(RoutingDecision.objects.filter(pk=after.pk).exists())
        self.assertEqual(
            RoutingDecision.objects.filter(reason_code="WILL_FAIL_INSIDE_OUTER").count(), 0
        )
        # (5) — no manual cleanup here: TestCase's own outer transaction
        # rolls back everything this test wrote (before/after included)
        # once it ends, leaving the database exactly as it was.


# Structural guardrail class removed — BOOK-04b (Position.routing_decision,
# RoutingDecision.position) and BOOK-04c (Trade.routing_decision) have both
# since landed; see test_book04b_shadow_mode_integration.py and
# test_book04c_close_symmetry.py for the tests covering those fields'
# actual behavior. The guardrails here only ever asserted those fields'
# ABSENCE "until those blocks are implemented" — nothing left to guard.

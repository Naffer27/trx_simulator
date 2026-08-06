# simulator/tests/test_o3d2_treasury_stuck_execution_observation_service.py
"""
Bloque O.3d-2 — Treasury Stuck Execution Observation Service.

Covers ONLY simulator/broker_audit.py::observe_stuck_treasury_executions()
and its private helper record_treasury_stuck_execution_observation() —
a strictly read-only-of-Treasury / write-only-of-BrokerAuditEvent
observer. It reuses inspect_stuck_treasury_execution() (O.3c-4b,
unmodified) exactly as built, and reuses the very same
BrokerAuditObservationLock singleton RISK-03's record_alert_event()
already uses (no new model, no new migration).

This module NEVER calls mark_treasury_execution_failed(), NEVER
imports wallet_ledger.py, NEVER creates or modifies a WalletTransaction,
NEVER touches a Wallet balance, and NEVER modifies the
TreasuryOperationRequest it observes. No Celery task, no Celery Beat
schedule entry, no dashboard, no view, no template and no migration
exist yet — those are later blocks (O.3d-3/4).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from simulator import audit as _audit
from simulator import broker_audit as _broker_audit
from simulator.broker_audit import (
    ActorType,
    Severity,
    observe_stuck_treasury_executions,
    record_treasury_stuck_execution_observation,
)
from simulator.models import (
    AuditLog, BrokerAuditEvent, BrokerAuditObservationLock,
    InternalTransfer, TreasuryOperationRequest, Wallet, WalletTransaction,
)
from simulator.treasury_execution_recovery import inspect_stuck_treasury_execution

from .factories import make_user, make_wallet

RECOVERY_MIN_AGE_SECONDS = 600  # settings.TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS default


def _make_executing_request(wallet=None, executed_by=None, wallet_transaction=None,
                             requested_by=None, approved_by=None, **overrides):
    if wallet is None:
        wallet = make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=Decimal("10.00"),
        reason="O.3d-2 observation service test",
        status=TreasuryOperationRequest.ST_EXECUTING,
        executed_by=executed_by,
        wallet_transaction=wallet_transaction,
        requested_by=requested_by,
        approved_by=approved_by,
    )
    data.update(overrides)
    return TreasuryOperationRequest.objects.create(**data)


def _started_audit_log(pk, age_seconds):
    created_at = timezone.now() - timedelta(seconds=age_seconds)
    return AuditLog.objects.create(
        event_type=_audit.EV_TREASURY_REQUEST_EXECUTION_STARTED,
        action=f"Treasury request #{pk} execution started",
        detail={"treasury_request_id": pk},
        created_at=created_at,
    )


def _eligible_candidate_for(tor):
    """CASE_A — old enough, clean, eligible=True."""
    _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
    candidates = inspect_stuck_treasury_execution()
    return next(c for c in candidates if c.instance.pk == tor.pk)


class UsesInspectionServiceTests(TestCase):

    def test_calls_inspect_stuck_treasury_execution(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        with patch(
            "simulator.treasury_execution_recovery.inspect_stuck_treasury_execution",
        ) as mock_inspect:
            mock_inspect.return_value = []
            observe_stuck_treasury_executions()

        mock_inspect.assert_called_once()

    def test_passes_min_age_seconds_through(self):
        with patch(
            "simulator.treasury_execution_recovery.inspect_stuck_treasury_execution",
        ) as mock_inspect:
            mock_inspect.return_value = []
            observe_stuck_treasury_executions(min_age_seconds=123)

        mock_inspect.assert_called_once_with(min_age_seconds=123)

    def test_no_candidates_returns_zero(self):
        self.assertEqual(observe_stuck_treasury_executions(), 0)


class SkipsCaseCTests(TestCase):

    def test_case_c_candidate_creates_no_event_and_is_not_counted(self):
        # executed_by must be active, or executed_by_missing_or_inactive
        # takes precedence in _classify()'s case ordering and this
        # would land in CASE_F instead of CASE_C.
        tor = _make_executing_request(executed_by=make_user(username="o3d2_case_c_exec", is_staff=True))
        _started_audit_log(tor.pk, age_seconds=50)  # below 600s threshold -> CASE_C

        written = observe_stuck_treasury_executions()

        self.assertEqual(written, 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=_broker_audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED,
            ).count(),
            0,
        )

    def test_record_helper_returns_none_if_somehow_called_with_case_c(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_case_c_helper_exec", is_staff=True))
        _started_audit_log(tor.pk, age_seconds=50)
        candidate = next(c for c in inspect_stuck_treasury_execution() if c.instance.pk == tor.pk)
        self.assertEqual(candidate.case, "CASE_C")

        result = record_treasury_stuck_execution_observation(candidate)
        self.assertIsNone(result)
        self.assertEqual(BrokerAuditEvent.objects.count(), 0)


class SeverityMappingTests(TestCase):

    def test_case_a_maps_to_warning(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_sev_a_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)
        self.assertEqual(candidate.case, "CASE_A")

        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.severity, Severity.WARNING)

    def test_case_b_maps_to_high(self):
        from simulator.wallet_ledger import credit_wallet

        wallet = make_wallet()
        wtx = credit_wallet(wallet.id, Decimal("5.00"), WalletTransaction.TX_BONUS, note="anomaly")
        tor = _make_executing_request(wallet=wallet, wallet_transaction=wtx)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        candidate = next(c for c in inspect_stuck_treasury_execution() if c.instance.pk == tor.pk)
        self.assertEqual(candidate.case, "CASE_B")

        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.severity, Severity.HIGH)

    def test_case_d_maps_to_warning(self):
        # executed_by must be active, or executed_by_missing_or_inactive
        # takes precedence in _classify()'s case ordering and this
        # would land in CASE_F instead of CASE_D.
        tor = _make_executing_request(executed_by=make_user(username="o3d2_sev_d_exec", is_staff=True))
        # No EXECUTION_STARTED audit event at all -> age UNKNOWN -> CASE_D.
        candidate = next(c for c in inspect_stuck_treasury_execution() if c.instance.pk == tor.pk)
        self.assertEqual(candidate.case, "CASE_D")

        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.severity, Severity.WARNING)

    def test_case_e_maps_to_high(self):
        tor = _make_executing_request()
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        AuditLog.objects.create(
            event_type=_audit.EV_TREASURY_REQUEST_EXECUTED,
            action="probe EXECUTED event while still EXECUTING",
            detail={"treasury_request_id": tor.pk},
        )
        candidate = next(c for c in inspect_stuck_treasury_execution() if c.instance.pk == tor.pk)
        self.assertEqual(candidate.case, "CASE_E")

        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.severity, Severity.HIGH)

    def test_case_f_maps_to_info(self):
        inactive_executor = make_user(username="o3d2_sev_f_exec", is_staff=True, is_active=False)
        tor = _make_executing_request(executed_by=inactive_executor)
        _started_audit_log(tor.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)
        candidate = next(c for c in inspect_stuck_treasury_execution() if c.instance.pk == tor.pk)
        self.assertEqual(candidate.case, "CASE_F")

        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.severity, Severity.INFO)

    def test_observe_written_count_matches_number_of_mapped_candidates(self):
        tor_a = _make_executing_request(executed_by=make_user(username="o3d2_batch_a", is_staff=True))
        _started_audit_log(tor_a.pk, age_seconds=RECOVERY_MIN_AGE_SECONDS + 100)

        tor_c = _make_executing_request(executed_by=make_user(username="o3d2_batch_c", is_staff=True))
        _started_audit_log(tor_c.pk, age_seconds=50)  # CASE_C, skipped

        written = observe_stuck_treasury_executions()
        self.assertEqual(written, 1)


class CreatesExactlyOneEventTests(TestCase):

    def test_creates_exactly_one_broker_audit_event(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_one_exec", is_staff=True))
        _eligible_candidate_for(tor)

        written = observe_stuck_treasury_executions()

        self.assertEqual(written, 1)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                event_type=_broker_audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED,
            ).count(),
            1,
        )

    def test_event_type_is_the_approved_constant(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_evtype_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)
        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.event_type, "treasury.stuck_execution_observed")
        self.assertEqual(event.event_type, _broker_audit.EV_TREASURY_STUCK_EXECUTION_OBSERVED)

    def test_actor_type_is_system(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_actor_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)
        event = record_treasury_stuck_execution_observation(candidate)
        self.assertEqual(event.actor_type, ActorType.SYSTEM)


class MetadataCompletenessTests(TestCase):

    _REQUIRED_KEYS = (
        "treasury_operation_request_id", "wallet_id", "wallet_user_id",
        "operation_type", "amount", "status", "case", "eligible",
        "block_reason", "age_seconds", "age_confidence", "executed_by_id",
        "executed_by_is_active", "has_wallet_transaction",
        "has_started_event", "has_executed_event", "has_failed_event",
        "observed_at", "dedup_window_seconds",
    )

    def test_metadata_contains_every_required_key(self):
        executor = make_user(username="o3d2_meta_exec", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        candidate = _eligible_candidate_for(tor)

        event = record_treasury_stuck_execution_observation(candidate)

        for key in self._REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, event.metadata)

    def test_metadata_values_match_the_candidate_and_instance(self):
        executor = make_user(username="o3d2_meta_val_exec", is_staff=True)
        wallet = make_wallet()
        tor = _make_executing_request(wallet=wallet, executed_by=executor)
        candidate = _eligible_candidate_for(tor)

        event = record_treasury_stuck_execution_observation(candidate)
        m = event.metadata

        self.assertEqual(m["treasury_operation_request_id"], tor.pk)
        self.assertEqual(m["wallet_id"], wallet.pk)
        self.assertEqual(m["wallet_user_id"], wallet.user_id)
        self.assertEqual(m["operation_type"], TreasuryOperationRequest.OP_BONUS_CREDIT)
        self.assertEqual(m["amount"], "10.00")
        self.assertEqual(m["status"], TreasuryOperationRequest.ST_EXECUTING)
        self.assertEqual(m["case"], "CASE_A")
        self.assertTrue(m["eligible"])
        self.assertIsNone(m["block_reason"])
        self.assertGreaterEqual(m["age_seconds"], RECOVERY_MIN_AGE_SECONDS)
        self.assertEqual(m["age_confidence"], "AUDIT_LOG")
        self.assertEqual(m["executed_by_id"], executor.pk)
        self.assertTrue(m["executed_by_is_active"])
        self.assertFalse(m["has_wallet_transaction"])
        self.assertTrue(m["has_started_event"])
        self.assertFalse(m["has_executed_event"])
        self.assertFalse(m["has_failed_event"])
        self.assertIsInstance(m["observed_at"], str)
        self.assertEqual(m["dedup_window_seconds"], 900)

    def test_dedup_window_seconds_reflects_override(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_meta_window_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)
        event = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=120)
        self.assertEqual(event.metadata["dedup_window_seconds"], 120)


class DedupTests(TestCase):

    def test_second_call_within_window_is_skipped(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_dedup_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        first = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=900)
        second = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=900)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            1,
        )

    def test_dedup_window_zero_always_records(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_dedup_zero_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        first = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=0)
        second = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=0)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            2,
        )

    def test_observe_second_tick_within_window_writes_zero_new(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_dedup_tick_exec", is_staff=True))
        _eligible_candidate_for(tor)

        first_written = observe_stuck_treasury_executions()
        second_written = observe_stuck_treasury_executions()

        self.assertEqual(first_written, 1)
        self.assertEqual(second_written, 0)

    def test_outside_window_records_again(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_dedup_old_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        first = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=900)
        self.assertIsNotNone(first)
        # Backdate the just-recorded event beyond the dedup window —
        # timestamp is auto_now_add, only movable via a direct .update().
        BrokerAuditEvent.objects.filter(pk=first.pk).update(
            timestamp=timezone.now() - timedelta(seconds=1000),
        )

        second = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=900)
        self.assertIsNotNone(second)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            2,
        )


class ConcurrentObservationDedupTests(TestCase):
    """
    Deliberately NOT a real-thread race (see below for why), but a
    deterministic equivalent that exercises the exact same
    check-then-create dedup path record_treasury_stuck_execution_
    observation() takes under BrokerAuditObservationLock, via two
    consecutive calls for the SAME candidate inside one dedup window —
    the same sequential-call technique test_broker_audit_trail.py's own
    DedupTests (record_alert_event) already uses for RISK-03, applied
    here to Treasury.

    Why not genuine threading, deliberately: a true two-thread race
    against SQLite (this project's dev/test database) is NOT a
    reliable way to verify this lock in this codebase. record_event()
    — the single writer every BrokerAuditEvent goes through, including
    this one — is fail-open by design (its own documented contract:
    an audit-write failure must never block or raise into the caller).
    Under genuine concurrent writers, SQLite's shared-cache table-level
    locking can raise "OperationalError: database table is locked" —
    distinct from SQLITE_BUSY, and NOT covered by PRAGMA busy_timeout —
    from inside BrokerAuditEvent.objects.create(). record_event()
    catches that internally and returns None, exactly as it would for a
    genuine dedup hit, indistinguishably from outside. A test-level
    retry wrapper can only retry an exception it can see propagate; it
    never sees this one, because record_event() already swallowed it.
    The result is a coin-flip test: BOTH racing threads can silently
    fail their write and the assertion "exactly one row" fails with
    zero rows instead — confirmed empirically against this file's
    original threaded version (4/8 failures across repeated isolated
    runs) AND, independently, against the pre-existing, already-merged
    RISK-03 precedent (test_broker_audit_trail.py's own
    ConcurrentObservationDedupTests, untouched by this block: 3/8
    failures across repeated isolated runs on the same machine) — this
    flakiness is a property of testing fail-open, SQLite-backed audit
    writes under real thread contention in this environment, not a
    defect O.3d-2 introduced, and not something this block's own tests
    should paper over with real threading here.

    Production locking correctness does not rely on this test: it runs
    on PostgreSQL, whose MVCC row-level locking does not exhibit
    SQLite's rollback-journal "table is locked" failure mode for this
    access pattern — select_for_update() there blocks and waits rather
    than raising. Genuine concurrent-writer correctness for this exact
    lock+dedup shape is already covered by the pre-existing RISK-03
    precedent test noted above; this class does not duplicate that
    coverage, only the sequential contract Treasury's own dedup must
    satisfy.

    Not touched, not weakened, not skipped, not xfailed: RISK-03's own
    ConcurrentObservationDedupTests in test_broker_audit_trail.py.
    """

    def test_first_call_creates_and_second_call_within_window_dedups(self):
        executor = make_user(username="o3d2_seqrace_exec", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        candidate = _eligible_candidate_for(tor)

        wallet = tor.wallet
        balance_before = wallet.available_balance
        wtx_count_before = WalletTransaction.objects.count()

        # 1/2 — first call creates exactly one BrokerAuditEvent.
        first = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=900)
        self.assertIsNotNone(first)
        self.assertIsInstance(first, BrokerAuditEvent)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            1,
        )

        # 3 — second call, same candidate, same window: no new event.
        second = record_treasury_stuck_execution_observation(candidate, dedup_window_seconds=900)

        # 5 — the return value itself distinguishes creation (a
        # BrokerAuditEvent instance) from dedup (None) — the service's
        # own contract, not an inference from row counts.
        self.assertIsNone(second)

        # 4 — exactly one row total after both calls.
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            1,
        )

        # 6 — metadata on the one row that WAS created remains correct.
        first.refresh_from_db()
        self.assertEqual(first.metadata["treasury_operation_request_id"], tor.pk)
        self.assertEqual(first.metadata["case"], "CASE_A")
        self.assertEqual(first.metadata["dedup_window_seconds"], 900)
        self.assertTrue(first.metadata["eligible"])

        # 7 — TreasuryOperationRequest untouched by either call.
        tor.refresh_from_db()
        self.assertEqual(tor.status, TreasuryOperationRequest.ST_EXECUTING)
        self.assertIsNone(tor.wallet_transaction_id)
        self.assertEqual(tor.executed_by_id, executor.pk)

        # 8 — no balance moved, no WalletTransaction created, by either call.
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, balance_before)
        self.assertEqual(WalletTransaction.objects.count(), wtx_count_before)

    def test_observe_stuck_treasury_executions_two_consecutive_ticks_write_once(self):
        """Same guarantee, exercised through the public entry point
        (the shape a Celery Beat tick will actually call) rather than
        the private helper directly."""
        executor = make_user(username="o3d2_seqrace_tick_exec", is_staff=True)
        tor = _make_executing_request(executed_by=executor)
        _eligible_candidate_for(tor)

        first_written = observe_stuck_treasury_executions()
        second_written = observe_stuck_treasury_executions()

        self.assertEqual(first_written, 1)
        self.assertEqual(second_written, 0)
        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            1,
        )


class FailOpenAuditTests(TestCase):
    """
    Documented contract: a BrokerAuditEvent write failure must never
    raise out of observe_stuck_treasury_executions() or
    record_treasury_stuck_execution_observation() — inherited
    unmodified from record_event()'s own fail-open guarantee (this
    module never bypasses it), same technique already used by
    test_broker_audit_trail.py's own record_event() fail-open tests.
    """

    def test_record_helper_never_raises_on_db_failure(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_failopen_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            result = record_treasury_stuck_execution_observation(candidate)

        self.assertIsNone(result)

    def test_observe_never_raises_and_does_not_count_the_failed_write(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_failopen_obs_exec", is_staff=True))
        _eligible_candidate_for(tor)

        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            written = observe_stuck_treasury_executions()

        self.assertEqual(written, 0)

    def test_failure_does_not_create_a_row(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_failopen_norow_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        with patch("simulator.models.BrokerAuditEvent.objects.create", side_effect=RuntimeError("boom")):
            record_treasury_stuck_execution_observation(candidate)

        self.assertEqual(
            BrokerAuditEvent.objects.filter(
                metadata__treasury_operation_request_id=tor.pk,
            ).count(),
            0,
        )


class NoSideEffectsOutsideBrokerAuditEventTests(TestCase):

    def setUp(self):
        self.executor = make_user(username="o3d2_noeffect_exec", is_staff=True)
        self.wallet = make_wallet(initial_balance=Decimal("75.00"))
        self.tor = _make_executing_request(wallet=self.wallet, executed_by=self.executor)
        _eligible_candidate_for(self.tor)

    def test_never_creates_auditlog(self):
        before = AuditLog.objects.count()
        observe_stuck_treasury_executions()
        after = AuditLog.objects.count()
        self.assertEqual(before, after)

    def test_never_modifies_the_treasury_operation_request(self):
        status_before = self.tor.status
        wallet_transaction_before = self.tor.wallet_transaction_id
        executed_at_before = self.tor.executed_at
        failure_reason_before = self.tor.failure_reason

        observe_stuck_treasury_executions()

        self.tor.refresh_from_db()
        self.assertEqual(self.tor.status, status_before)
        self.assertEqual(self.tor.wallet_transaction_id, wallet_transaction_before)
        self.assertEqual(self.tor.executed_at, executed_at_before)
        self.assertEqual(self.tor.failure_reason, failure_reason_before)

    def test_never_touches_wallet_balance(self):
        balance_before = self.wallet.available_balance
        observe_stuck_treasury_executions()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)

    def test_never_creates_wallet_transaction(self):
        before = WalletTransaction.objects.filter(wallet=self.wallet).count()
        observe_stuck_treasury_executions()
        after = WalletTransaction.objects.filter(wallet=self.wallet).count()
        self.assertEqual(before, after)

    def test_never_creates_internal_transfer(self):
        observe_stuck_treasury_executions()
        self.assertEqual(InternalTransfer.objects.count(), 0)

    def test_never_calls_mark_treasury_execution_failed(self):
        with patch(
            "simulator.treasury_execution_recovery.mark_treasury_execution_failed",
        ) as mock_mark_failed:
            observe_stuck_treasury_executions()
        mock_mark_failed.assert_not_called()


class ScopeAndSafetyTests(SimpleTestCase):

    def test_ast_confirms_no_financial_functions_used_by_either_function(self):
        import ast
        import inspect

        forbidden_calls = {
            "credit_wallet", "debit_wallet", "reconcile_wallet",
            "transfer_to_account", "transfer_to_wallet",
            "mark_treasury_execution_failed",
        }
        forbidden_imports = {"wallet_ledger"}

        for fn in (
            _broker_audit.observe_stuck_treasury_executions,
            _broker_audit.record_treasury_stuck_execution_observation,
        ):
            tree = ast.parse(inspect.getsource(fn))
            imported = set()
            called = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.rsplit(".", 1)[-1])
                    imported.update(a.name for a in node.names)
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    if name:
                        called.add(name)

            with self.subTest(fn=fn.__name__):
                self.assertFalse(forbidden_calls & called, f"found: {forbidden_calls & called}")
                self.assertFalse(forbidden_imports & imported, f"found: {forbidden_imports & imported}")

    def test_no_direct_status_assignment_on_treasury_operation_request(self):
        import ast
        import inspect

        for fn in (
            _broker_audit.observe_stuck_treasury_executions,
            _broker_audit.record_treasury_stuck_execution_observation,
        ):
            tree = ast.parse(inspect.getsource(fn))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ("status", "wallet_transaction"):
                    self.assertNotIsInstance(
                        node.ctx, ast.Store,
                        f"{fn.__name__} must not assign .{node.attr} directly",
                    )

    def test_no_save_call_anywhere_in_either_function(self):
        import ast
        import inspect

        for fn in (
            _broker_audit.observe_stuck_treasury_executions,
            _broker_audit.record_treasury_stuck_execution_observation,
        ):
            tree = ast.parse(inspect.getsource(fn))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    self.assertNotEqual(name, "save", f"{fn.__name__} must not call .save()")


class ObservationLockReuseTests(TestCase):
    """Confirms O.3d-2 reuses the existing BrokerAuditObservationLock
    singleton rather than introducing a new model — the migration-seeded
    row (id=1) already exists before this block ever runs."""

    def test_observation_lock_singleton_row_exists(self):
        self.assertEqual(BrokerAuditObservationLock.objects.filter(pk=1).count(), 1)

    def test_no_new_lock_model_was_introduced(self):
        from simulator import models as _models
        self.assertFalse(hasattr(_models, "TreasuryObservationLock"))

    def test_uses_select_for_update(self):
        from django.db.models import QuerySet

        tor = _make_executing_request(executed_by=make_user(username="o3d2_lock_spy_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        original = QuerySet.select_for_update
        calls = []

        def spy(self, *args, **kwargs):
            calls.append(kwargs)
            return original(self, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", spy):
            record_treasury_stuck_execution_observation(candidate)

        self.assertTrue(len(calls) >= 1)


class ReturnContractTests(TestCase):

    def test_return_value_is_plain_int(self):
        result = observe_stuck_treasury_executions()
        self.assertIsInstance(result, int)

    def test_return_value_never_negative(self):
        self.assertGreaterEqual(observe_stuck_treasury_executions(), 0)

    def test_record_helper_returns_broker_audit_event_or_none(self):
        tor = _make_executing_request(executed_by=make_user(username="o3d2_contract_exec", is_staff=True))
        candidate = _eligible_candidate_for(tor)

        result = record_treasury_stuck_execution_observation(candidate)
        self.assertIsInstance(result, BrokerAuditEvent)

        deduped_result = record_treasury_stuck_execution_observation(candidate)
        self.assertIsNone(deduped_result)

"""
simulator/tests/test_pgfix02_log_audit_isolation.py — BBOOK-PGFIX02

Certifies two independent fixes, both scoped strictly to AuditLog/log_audit:

1. AuditLog.action is now TextField() (migration 0096) — no more
   StringDataRightTruncation under PostgreSQL for legitimate, long,
   free-form audit narratives (previously CharField(max_length=120)).

2. log_audit()'s AuditLog.objects.create() is isolated in its own
   transaction.atomic() (a SAVEPOINT when already nested). BBOOK-PGFIX02
   FASE A proved, against real PostgreSQL, that without this isolation a
   failed audit insert — for ANY reason, not just a long action string —
   leaves the connection aborted, and a caller's own transaction.atomic()
   silently rolls back every real write made earlier in it, with zero
   exception ever raised. These tests force that exact failure (mocking
   AuditLog.objects.create to raise) and prove the caller's business
   transaction is now completely unaffected.

Does not touch, and is not affected by, BBOOK-CLOSE-02's
FundedPayoutRequest/fpr_one_active_request_per_enrollment constraint
(migration 0095) — entirely separate model, separate migration.
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.utils import DataError
from django.test import TestCase, TransactionTestCase

from simulator.audit import log_audit
from simulator.models import AuditLog
from simulator.tests.factories import make_user, make_wallet
from simulator.wallet_ledger import credit_wallet, debit_wallet

# The exact action string BBOOK-PGFIX02 FASE A proved always exceeds the
# old 120-char CharField limit — used here as the canonical "long action"
# fixture, now expected to succeed under the new TextField.
_LONG_ACTION = (
    "PayoutWebhookEvent #1 could not be correlated on first delivery — "
    "Orphan: provider_reference presente, 0 matches tras reintentos acotados"
)
assert len(_LONG_ACTION) > 120, "fixture must exceed the old CharField(120) limit"


class ActionFieldTests(TestCase):
    """Item 1/9 — normal action; item 2/9 — long action."""

    def test_normal_action_creates_row(self):
        log_audit(None, "test.pgfix02.normal", "short action", detail={"k": "v"})
        row = AuditLog.objects.get(event_type="test.pgfix02.normal")
        self.assertEqual(row.action, "short action")

    def test_long_action_creates_row_without_truncation(self):
        log_audit(None, "test.pgfix02.long", _LONG_ACTION, detail={})
        row = AuditLog.objects.get(event_type="test.pgfix02.long")
        self.assertEqual(row.action, _LONG_ACTION, "must be stored verbatim, not truncated")

    def test_action_field_is_textfield(self):
        field = AuditLog._meta.get_field("action")
        self.assertEqual(type(field).__name__, "TextField")


class BusinessWriteSurroundingTests(TestCase):
    """Item 3/9 — a real business write both BEFORE and AFTER log_audit(),
    inside the same transaction.atomic(), both survive."""

    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("100.00"))

    def test_business_writes_before_and_after_log_audit_both_survive(self):
        with transaction.atomic():
            tx_before = credit_wallet(
                self.wallet.id, Decimal("10.00"), "TEST_BEFORE", note="before",
            )
            log_audit(None, "test.pgfix02.sandwich", _LONG_ACTION, detail={})
            tx_after = credit_wallet(
                self.wallet.id, Decimal("5.00"), "TEST_AFTER", note="after",
            )

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("115.00"))
        self.assertIsNotNone(tx_before.pk)
        self.assertIsNotNone(tx_after.pk)
        self.assertEqual(
            AuditLog.objects.filter(event_type="test.pgfix02.sandwich").count(), 1,
        )


class ForcedAuditFailureInsideAtomicTests(TestCase):
    """Items 4, 5, 6, 9/9 — a forced audit-write failure (not just the long
    action string — any exception) inside transaction.atomic() must never
    touch the caller's business write, must leave the connection healthy
    (needs_rollback False), must work when nested another level deep, and
    repeated failures must never duplicate the business effect."""

    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("100.00"))

    def test_forced_failure_does_not_touch_business_write(self):
        with patch(
            "simulator.models.AuditLog.objects.create",
            side_effect=DataError("value too long for type character varying(120)"),
        ):
            with transaction.atomic():
                tx = credit_wallet(self.wallet.id, Decimal("10.00"), "TEST_FORCED", note="x")
                log_audit(None, "test.pgfix02.forced", "irrelevant", detail={})
                # nothing else here — this is exactly the silent-loss shape
                # BBOOK-PGFIX02 FASE A reproduced and this fix must close.

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("110.00"))
        self.assertIsNotNone(tx.pk)
        self.assertEqual(
            AuditLog.objects.filter(event_type="test.pgfix02.forced").count(), 0,
            "the audit row itself is correctly NOT created — that's the one, isolated loss",
        )

    def test_needs_rollback_is_clean_after_forced_failure(self):
        with patch(
            "simulator.models.AuditLog.objects.create",
            side_effect=DataError("boom"),
        ):
            with transaction.atomic():
                log_audit(None, "test.pgfix02.needs_rollback", "x", detail={})
                # A plain read right after — this is exactly what raised
                # TransactionManagementError before this fix.
                self.assertEqual(AuditLog.objects.count(), 0)
        self.assertFalse(
            connection.needs_rollback,
            "connection must not be left needing rollback after log_audit()'s internal failure",
        )

    def test_nested_atomic_savepoint_still_isolates(self):
        with transaction.atomic():  # outer
            tx_outer = credit_wallet(self.wallet.id, Decimal("1.00"), "OUTER", note="x")
            with transaction.atomic():  # inner — the shape most real callers use
                with patch(
                    "simulator.models.AuditLog.objects.create",
                    side_effect=DataError("boom"),
                ):
                    log_audit(None, "test.pgfix02.nested", "x", detail={})
                tx_inner = credit_wallet(self.wallet.id, Decimal("2.00"), "INNER", note="x")

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("103.00"))
        self.assertIsNotNone(tx_outer.pk)
        self.assertIsNotNone(tx_inner.pk)

    def test_repeated_audit_failures_never_duplicate_the_business_effect(self):
        """Item 9/9 — no duplication of credits/debits/ledger rows under
        repeated audit failures, even several in the same transaction."""
        with patch(
            "simulator.models.AuditLog.objects.create",
            side_effect=DataError("boom"),
        ):
            with transaction.atomic():
                for _ in range(5):
                    log_audit(None, "test.pgfix02.repeated", "x", detail={})
                tx = credit_wallet(self.wallet.id, Decimal("7.00"), "TEST_REPEATED", note="x")

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("107.00"))
        from simulator.models import WalletTransaction
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=self.wallet, tx_type="TEST_REPEATED").count(),
            1,
            "5 forced audit failures must not produce 5 (or 0) credits — exactly 1",
        )


class BareAutocommitTests(TransactionTestCase):
    """Item 7/9 — log_audit() called with no surrounding transaction.atomic()
    (Django's default per-request mode in this project — ATOMIC_REQUESTS is
    not set) continues to work exactly as before. Uses TransactionTestCase,
    not TestCase: TestCase always wraps every test in its own ambient
    atomic() block for rollback-at-teardown, which would make
    connection.get_autocommit() report False regardless of what this test
    does — genuinely exercising autocommit mode requires TransactionTestCase."""

    def test_bare_call_creates_row_and_does_not_affect_later_queries(self):
        self.assertTrue(connection.get_autocommit())
        log_audit(None, "test.pgfix02.bare", "bare call", detail={})
        self.assertEqual(AuditLog.objects.filter(event_type="test.pgfix02.bare").count(), 1)

    def test_bare_call_with_forced_failure_is_fully_contained(self):
        with patch(
            "simulator.models.AuditLog.objects.create",
            side_effect=DataError("boom"),
        ):
            log_audit(None, "test.pgfix02.bare_forced", "x", detail={})
        # connection must be immediately usable — no lingering state at all
        self.assertEqual(AuditLog.objects.count(), 0)


class GenuineBusinessRollbackTests(TestCase):
    """Item 8/9 — a real, unrelated business exception raised AFTER a
    successful log_audit() call must still roll back the whole transaction
    correctly — the new internal savepoint must not weaken real rollback
    semantics."""

    def setUp(self):
        self.user = make_user()
        self.wallet = make_wallet(self.user, initial_balance=Decimal("100.00"))

    def test_genuine_exception_after_log_audit_rolls_back_everything(self):
        class _Boom(Exception):
            pass

        with self.assertRaises(_Boom):
            with transaction.atomic():
                credit_wallet(self.wallet.id, Decimal("50.00"), "TEST_ROLLBACK", note="x")
                log_audit(None, "test.pgfix02.genuine_rollback", "x", detail={})
                raise _Boom("a real, unrelated business failure")

        self.wallet.refresh_from_db()
        self.assertEqual(
            self.wallet.available_balance, Decimal("100.00"),
            "the whole transaction, including the successfully-written audit row, must roll back",
        )
        self.assertEqual(
            AuditLog.objects.filter(event_type="test.pgfix02.genuine_rollback").count(), 0,
            "the audit row itself must also be rolled back — it was part of the same real transaction",
        )


class DirectConstraintUnaffectedTests(TestCase):
    """Confirms BBOOK-CLOSE-02's FundedPayoutRequest constraint (migration
    0095) is completely unaffected by this block — different model,
    different migration, no shared code path."""

    def test_fpr_constraint_still_present_and_independent(self):
        from simulator.models import FundedPayoutRequest
        names = [c.name for c in FundedPayoutRequest._meta.constraints]
        self.assertIn("fpr_one_active_request_per_enrollment", names)

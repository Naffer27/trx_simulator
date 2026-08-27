# simulator/tests/test_fix02a3_admin_hardening.py
"""
FIX-02A.3 — Admin Safety / Manual Transition Hardening.

Covers:
  1. WithdrawalRequestAdmin — read-only total (all model fields, including
     status/admin_note), no add, no delete, delete_selected unavailable,
     direct change-form POST bypass produces zero mutation.
  2. PayoutAttemptAdmin — newly registered, strictly view-only (no add,
     no change, no delete), delete_selected unavailable, direct
     change-form POST bypass produces zero mutation (403, before any
     form binding happens at all).
  3. reject_withdrawals — corrected lock order (Wallet -> WithdrawalRequest,
     matching the global Design Lock Correction #2 from FIX-02A.2/.3),
     refund-exactly-once idempotency, PROCESSING/UNKNOWN/terminal guard,
     no HTTP, audit/email preserved, and concurrency (reject/reject,
     approve/reject) proven with real threads.

DB BACKEND NOTE (see test_atomic_guard_lock_order.py's own module
docstring for the full explanation, not repeated here): this suite runs
against SQLite in shared-cache in-memory mode. SQLite ignores
`SELECT ... FOR UPDATE` syntax entirely (django.db.backends.sqlite3 has
has_select_for_update = False) — it takes no row lock, only a coarse
whole-database write lock once a write actually happens. That means SQL
query TEXT order is not proof of *lock acquisition* order on this
backend. Per FIX-02A.3's own design lock, this suite therefore keeps two
separate kinds of evidence, never conflating them:
  - a STRUCTURAL test (source-order inspection) proving the CODE issues
    Wallet.select_for_update() before WithdrawalRequest.select_for_update()
    — backend-independent, catches silent future reordering;
  - FUNCTIONAL concurrency tests (real threads, real separate DB
    connections) proving the actually observable safety property that
    matters regardless of backend: no double refund, no double economic
    resolution — under SQLite's coarser locking, this proves at least as
    much serialization as a fine-grained backend would, never less.
"""
import random
import threading
import time
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.admin.sites import site as admin_site
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import OperationalError, connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from simulator.admin import PayoutAttemptAdmin, WithdrawalRequestAdmin, approve_withdrawals, reject_withdrawals
from simulator.models import PayoutAttempt, Wallet, WalletTransaction, WithdrawalRequest
from simulator.payout_orchestrator import WithdrawalAlreadyClaimed, submit_withdrawal_to_provider
from simulator.wallet_ledger import credit_wallet, debit_wallet, get_or_create_wallet

from .factories import make_user, make_wallet


# ── Helpers ──────────────────────────────────────────────────────────────

def _admin_request(admin_user):
    req = RequestFactory().post("/admin/")
    req.user = admin_user
    req._messages = CookieStorage(req)
    return req


def _make_pending_wr(user, wallet, amount="80.00"):
    """Debit wallet and create a PENDING WithdrawalRequest — mirrors withdraw_view."""
    debit_tx = debit_wallet(
        wallet.id, Decimal(amount), WalletTransaction.TX_WITHDRAW, note="fix02a3 test wr"
    )
    return WithdrawalRequest.objects.create(
        user=user,
        amount_usd=Decimal(amount),
        crypto_currency="btc",
        wallet_address="bc1qtest000000000000000000000000000000000",
        status=WithdrawalRequest.STATUS_PENDING,
        debit_tx=debit_tx,
    )


def _make_payout_attempt(wr, **overrides):
    return PayoutAttempt.objects.create(
        withdrawal_request=wr,
        provider=overrides.get("provider", "nowpayments"),
        attempt_number=overrides.get("attempt_number", 1),
        idempotency_key=overrides.get("idempotency_key", f"fix02a3-{wr.pk}-1"),
        requested_amount_usd=overrides.get("requested_amount_usd", wr.amount_usd),
        requested_asset=overrides.get("requested_asset", wr.crypto_currency),
        destination_address=overrides.get("destination_address", wr.wallet_address),
        provider_reference=overrides.get("provider_reference", "wd-fix02a3"),
        provider_batch_id=overrides.get("provider_batch_id", "batch-fix02a3"),
        status=overrides.get("status", PayoutAttempt.STATUS_PROCESSING),
    )


class _FakeAdapter:
    """Minimal stand-in matching NowPaymentsAdapter's contract — same
    pattern already used in test_fix02a2_submission.py. No real HTTP."""
    provider_name = "nowpayments"

    def __init__(self, *, estimate_result=None, create_result=None):
        self.estimate_result = estimate_result if estimate_result is not None else Decimal("0.001")
        self.create_result = create_result

    def estimate(self, amount_usd, asset):
        return self.estimate_result

    def create_payout(self, attempt, *, callback_url=""):
        return self.create_result


class _FakeSubmissionResult:
    def __init__(self):
        self.accepted = True
        self.provider_reference = "wd-1"
        self.provider_batch_id = "batch-1"
        self.provider_amount = Decimal("0.001")
        self.raw_status = "CREATED"


def _run_locked_retry(fn, barrier, results, index, max_retries=40):
    """Shared thread body — identical retry-on-SQLITE_LOCKED pattern
    already used in test_atomic_guard_lock_order.py and
    test_fix02a2_submission.py. Not a new invention."""
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                results[index] = ("ok", fn())
                return
            except WithdrawalAlreadyClaimed as exc:
                results[index] = ("claimed", exc)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= max_retries:
                    results[index] = ("error", exc)
                    return
                time.sleep(random.uniform(0.005, 0.03))
            except Exception as exc:  # pragma: no cover - diagnostic safety net
                results[index] = ("error", exc)
                return
    finally:
        connection.close()


def _run_reject_until_resolved(ma, admin_user, wr_pk, barrier, results, index, max_retries=60):
    """reject_withdrawals catches its own per-row OperationalError
    internally (existing, unchanged behavior — see its outer `except
    Exception` and admin.py's error-message pattern) and does NOT
    propagate it to the caller — unlike submit_withdrawal_to_provider.
    Under SQLite's coarse table-level locking (see module docstring),
    a losing thread's single call can therefore return normally having
    silently failed to mutate anything (same as an operator whose click
    hit a transient DB error and would just press the action again).
    This retries the ACTION ITSELF, not an exception, until the row is
    observably no longer PENDING — proving the real property under
    test (exactly one winner, exactly one refund) without depending on
    SQLite offering row-level lock contention it structurally cannot."""
    from unittest.mock import patch
    with connection.cursor() as cur:
        cur.execute("PRAGMA busy_timeout = 30000;")
    barrier.wait(timeout=5)
    try:
        with patch("simulator.tasks.send_email_async.delay"):
            for _ in range(max_retries):
                reject_withdrawals(
                    ma, _admin_request(admin_user),
                    WithdrawalRequest.objects.filter(pk=wr_pk),
                )
                status = WithdrawalRequest.objects.values_list("status", flat=True).get(pk=wr_pk)
                if status != WithdrawalRequest.STATUS_PENDING:
                    results[index] = ("resolved", status)
                    return
                time.sleep(random.uniform(0.005, 0.03))
        results[index] = ("exhausted", None)
    finally:
        connection.close()


# ── 1. WithdrawalRequestAdmin — read-only total ────────────────────────────

class WithdrawalRequestAdminReadOnlyTests(TestCase):
    def test_registered(self):
        self.assertIn(WithdrawalRequest, admin_site._registry)

    def test_all_model_fields_readonly(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        model_fields = {f.name for f in WithdrawalRequest._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_status_is_readonly(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        self.assertIn("status", ma.readonly_fields)

    def test_admin_note_is_readonly(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        self.assertIn("admin_note", ma.readonly_fields)

    def test_financial_and_provider_fields_readonly(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        for field in (
            "amount_usd", "wallet_address", "crypto_currency", "user",
            "np_payout_id", "np_batch_id", "np_payout_status", "crypto_amount",
            "debit_tx", "reviewed_by", "reviewed_at",
        ):
            self.assertIn(field, ma.readonly_fields, f"{field} must be readonly")

    def test_has_add_permission_false(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        self.assertFalse(ma.has_add_permission(request=None))

    def test_has_delete_permission_false(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_not_available(self):
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())
        request = RequestFactory().get("/")
        request.user = make_user(username="f23a_wr_super", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)


class WithdrawalRequestAdminChangeFormBypassTests(TestCase):
    def setUp(self):
        self.superuser = make_user(username="f23a_wr_bypass_super", is_staff=True, is_superuser=True)
        self.user = make_user(username="f23a_wr_bypass_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.wr = _make_pending_wr(self.user, self.wallet, "80.00")

    def _change_url(self):
        return reverse("admin:simulator_withdrawalrequest_change", args=[self.wr.pk])

    def test_post_status_completed_does_not_mutate_status(self):
        client = Client()
        client.force_login(self.superuser)
        client.post(self._change_url(), data={
            "status": WithdrawalRequest.STATUS_COMPLETED,
            "amount_usd": "999999.00",
            "wallet_address": "attacker-controlled-address",
            "crypto_currency": "eth",
            "np_payout_id": "forged-id",
            "_continue": "Save and continue editing",
        })
        self.wr.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(self.wr.amount_usd, Decimal("80.00"))
        self.assertEqual(self.wr.wallet_address, "bc1qtest000000000000000000000000000000000")
        self.assertEqual(self.wr.crypto_currency, "btc")
        self.assertEqual(self.wr.np_payout_id, "")

    def test_post_status_completed_creates_no_payout_attempt(self):
        client = Client()
        client.force_login(self.superuser)
        client.post(self._change_url(), data={
            "status": WithdrawalRequest.STATUS_COMPLETED,
            "_continue": "Save and continue editing",
        })
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=self.wr).count(), 0)

    def test_post_status_completed_does_not_move_wallet(self):
        client = Client()
        client.force_login(self.superuser)
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        client.post(self._change_url(), data={
            "status": WithdrawalRequest.STATUS_COMPLETED,
            "_continue": "Save and continue editing",
        })
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_before)

    def test_delete_view_does_not_delete(self):
        client = Client()
        client.force_login(self.superuser)
        delete_url = reverse("admin:simulator_withdrawalrequest_delete", args=[self.wr.pk])
        client.post(delete_url, data={"post": "yes"})
        self.assertTrue(WithdrawalRequest.objects.filter(pk=self.wr.pk).exists())

    def test_add_view_forbidden(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.get(reverse("admin:simulator_withdrawalrequest_add"))
        self.assertEqual(resp.status_code, 403)

    def test_changelist_has_no_delete_selected(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.get(reverse("admin:simulator_withdrawalrequest_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)


# ── 2. PayoutAttemptAdmin — strictly view-only ─────────────────────────────

class PayoutAttemptAdminPermissionTests(TestCase):
    def test_registered(self):
        self.assertIn(PayoutAttempt, admin_site._registry)

    def test_all_model_fields_readonly(self):
        ma = PayoutAttemptAdmin(PayoutAttempt, AdminSite())
        model_fields = {f.name for f in PayoutAttempt._meta.fields}
        self.assertEqual(set(ma.readonly_fields), model_fields)

    def test_has_add_permission_false(self):
        ma = PayoutAttemptAdmin(PayoutAttempt, AdminSite())
        self.assertFalse(ma.has_add_permission(request=None))

    def test_has_change_permission_false(self):
        ma = PayoutAttemptAdmin(PayoutAttempt, AdminSite())
        self.assertFalse(ma.has_change_permission(request=None))

    def test_has_delete_permission_false(self):
        ma = PayoutAttemptAdmin(PayoutAttempt, AdminSite())
        self.assertFalse(ma.has_delete_permission(request=None))

    def test_delete_selected_not_available(self):
        ma = PayoutAttemptAdmin(PayoutAttempt, AdminSite())
        request = RequestFactory().get("/")
        request.user = make_user(username="f23a_pa_super", is_staff=True, is_superuser=True)
        actions = ma.get_actions(request)
        self.assertNotIn("delete_selected", actions)

    def test_superuser_can_view_changelist(self):
        superuser = make_user(username="f23a_pa_viewer_super", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(superuser)
        resp = client.get(reverse("admin:simulator_payoutattempt_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"delete_selected", resp.content)


class PayoutAttemptAdminChangeFormBypassTests(TestCase):
    def setUp(self):
        self.superuser = make_user(username="f23a_pa_bypass_super", is_staff=True, is_superuser=True)
        self.user = make_user(username="f23a_pa_bypass_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.wr = _make_pending_wr(self.user, self.wallet, "80.00")
        self.attempt = _make_payout_attempt(self.wr)

    def _change_url(self):
        return reverse("admin:simulator_payoutattempt_change", args=[self.attempt.pk])

    def test_get_change_view_is_viewable_read_only(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.get(self._change_url())
        # has_change_permission=False + default has_view_permission=True
        # -> Django's native read-only detail rendering (200, no crash),
        # never a 403 on GET.
        self.assertEqual(resp.status_code, 200)

    def test_post_to_change_view_is_rejected(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.post(self._change_url(), data={
            "status": PayoutAttempt.STATUS_COMPLETED,
            "provider_reference": "forged-ref",
            "provider_batch_id": "forged-batch",
        })
        # has_change_permission=False rejects the POST outright — no form
        # binding happens at all. Valid per FIX-02A.3 design lock.
        self.assertEqual(resp.status_code, 403)

    def test_post_to_change_view_does_not_mutate_db(self):
        client = Client()
        client.force_login(self.superuser)
        client.post(self._change_url(), data={
            "status": PayoutAttempt.STATUS_COMPLETED,
            "provider_reference": "forged-ref",
            "provider_batch_id": "forged-batch",
            "provider": "forged-provider",
            "requested_amount_usd": "999999.00",
            "destination_address": "attacker-address",
            "idempotency_key": "forged-key",
        })
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PayoutAttempt.STATUS_PROCESSING)
        self.assertEqual(self.attempt.provider_reference, "wd-fix02a3")
        self.assertEqual(self.attempt.provider_batch_id, "batch-fix02a3")
        self.assertEqual(self.attempt.provider, "nowpayments")
        self.assertEqual(self.attempt.requested_amount_usd, self.wr.amount_usd)
        self.assertEqual(self.attempt.destination_address, self.wr.wallet_address)
        self.assertEqual(self.attempt.idempotency_key, f"fix02a3-{self.wr.pk}-1")

    def test_delete_view_rejected_and_does_not_delete(self):
        client = Client()
        client.force_login(self.superuser)
        delete_url = reverse("admin:simulator_payoutattempt_delete", args=[self.attempt.pk])
        resp = client.post(delete_url, data={"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(PayoutAttempt.objects.filter(pk=self.attempt.pk).exists())

    def test_add_view_forbidden(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.get(reverse("admin:simulator_payoutattempt_add"))
        self.assertEqual(resp.status_code, 403)


# ── 3. reject_withdrawals — lock order (structural) ────────────────────────

class RejectWithdrawalsLockOrderStructuralTests(TestCase):
    """See module docstring re: SQLite ignores FOR UPDATE — this is the
    backend-independent half of the evidence, proving the CODE issues
    Wallet.select_for_update() before WithdrawalRequest.select_for_update().
    The functional half (real concurrency, real serialization) lives in
    RejectWithdrawalsConcurrencyTests below."""

    def test_reject_locks_wallet_before_withdrawal_request_source_order(self):
        import inspect
        src = inspect.getsource(reject_withdrawals.__wrapped__)
        wallet_idx = src.find("Wallet.objects.select_for_update()")
        wr_idx = src.find("WithdrawalRequest.objects.select_for_update()")
        self.assertGreater(wallet_idx, -1, "Wallet.objects.select_for_update() not found in reject_withdrawals")
        self.assertGreater(wr_idx, -1, "WithdrawalRequest.objects.select_for_update() not found in reject_withdrawals")
        self.assertLess(
            wallet_idx, wr_idx,
            "reject_withdrawals must lock Wallet before WithdrawalRequest — "
            "matching the global lock order (Wallet -> WithdrawalRequest -> "
            "PayoutAttempt) closed in FIX-02A.2/.3.",
        )

    def test_reject_credit_wallet_call_is_inside_the_same_atomic_block_as_the_status_update(self):
        """Structural guard against the original bug class: the refund and
        the status transition must be able to roll back together. Checked
        by confirming credit_wallet( appears after the WithdrawalRequest
        .update(...) call and before the function's next top-level
        statement following the `with transaction.atomic()` block ends —
        approximated here by confirming both calls are textually inside
        the same `with _tx.atomic():` block (single occurrence in the
        function, no second atomic() opened between them)."""
        import inspect
        src = inspect.getsource(reject_withdrawals.__wrapped__)
        self.assertEqual(src.count("_tx.atomic()"), 1, "reject_withdrawals must use exactly one atomic block")
        atomic_idx = src.find("with _tx.atomic():")
        update_idx = src.find("WithdrawalRequest.objects.filter(pk=wr_locked.pk).update(")
        # rfind, not find — "credit_wallet(" also appears once in this
        # function's own docstring (describing the pre-FIX-02A.3 bug it
        # replaces); the real call site is the LAST occurrence.
        credit_idx = src.rfind("credit_wallet(")
        self.assertGreater(atomic_idx, -1)
        self.assertGreater(update_idx, atomic_idx)
        self.assertGreater(credit_idx, update_idx)


# ── 4. reject_withdrawals — functional (sequential) ─────────────────────────

class RejectWithdrawalsFunctionalTests(TestCase):
    def setUp(self):
        self.admin = make_user(username="f23a_reject_admin", is_staff=True, is_superuser=True)
        self.user = make_user(username="f23a_reject_user")
        self.wallet = make_wallet(self.user, initial_balance=Decimal("200"))
        self.wr = _make_pending_wr(self.user, self.wallet, "80.00")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, Decimal("120.00"))

    def _ma(self):
        return WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())

    def test_reject_pending_transitions_to_rejected_and_refunds_exactly_once(self):
        from unittest.mock import patch
        with patch("simulator.tasks.send_email_async.delay"):
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
        self.wr.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_REJECTED)
        self.assertEqual(self.wallet.available_balance, Decimal("200.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION,
            ).count(),
            1,
        )

    def test_reject_already_rejected_cannot_refund_again(self):
        from unittest.mock import patch
        with patch("simulator.tasks.send_email_async.delay"):
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
            balance_after_first = Wallet.objects.get(pk=self.wallet.pk).available_balance
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, balance_after_first)
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION,
            ).count(),
            1,
        )

    def _assert_status_guard(self, status):
        from unittest.mock import patch
        self.wr.status = status
        self.wr.save(update_fields=["status"])
        balance_before = Wallet.objects.get(pk=self.wallet.pk).available_balance
        with patch("simulator.tasks.send_email_async.delay"):
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
        self.wr.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wr.status, status, f"{status} must not be rejectable")
        self.assertEqual(self.wallet.available_balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=self.wallet, tx_type=WalletTransaction.TX_CORRECTION,
            ).count(),
            0,
        )

    def test_reject_processing_cannot_be_rejected(self):
        self._assert_status_guard(WithdrawalRequest.STATUS_PROCESSING)

    def test_reject_unknown_cannot_be_rejected(self):
        # WithdrawalRequest has no STATUS_UNKNOWN of its own — UNKNOWN is a
        # PayoutAttempt-only status (see payout_state_machine.py); the
        # WithdrawalRequest-level equivalent while a PayoutAttempt is
        # UNKNOWN is STATUS_PROCESSING (derive_withdrawal_status() maps
        # SUBMITTED/PROCESSING/UNKNOWN -> "processing"). Covered by
        # test_reject_processing_cannot_be_rejected above; this test
        # additionally confirms an attached UNKNOWN PayoutAttempt survives
        # untouched.
        self.wr.status = WithdrawalRequest.STATUS_PROCESSING
        self.wr.save(update_fields=["status"])
        attempt = _make_payout_attempt(self.wr, status=PayoutAttempt.STATUS_UNKNOWN)
        from unittest.mock import patch
        with patch("simulator.tasks.send_email_async.delay"):
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
        self.wr.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(self.wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(attempt.status, PayoutAttempt.STATUS_UNKNOWN)

    def test_reject_completed_cannot_be_rejected(self):
        self._assert_status_guard(WithdrawalRequest.STATUS_COMPLETED)

    def test_reject_failed_cannot_be_rejected(self):
        self._assert_status_guard(WithdrawalRequest.STATUS_FAILED)

    def test_reject_makes_no_http_call(self):
        from unittest.mock import patch
        with patch("simulator.tasks.send_email_async.delay"), \
             patch("requests.post") as post_mock, \
             patch("requests.get") as get_mock:
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
        post_mock.assert_not_called()
        get_mock.assert_not_called()

    def test_reject_preserves_audit_and_email(self):
        from unittest.mock import patch
        with patch("simulator.tasks.send_email_async.delay") as email_mock:
            reject_withdrawals(self._ma(), _admin_request(self.admin),
                                WithdrawalRequest.objects.filter(pk=self.wr.pk))
        from simulator.models import AuditLog
        from simulator.audit import EV_WITHDRAW_REJECTED
        self.assertTrue(
            AuditLog.objects.filter(
                event_type=EV_WITHDRAW_REJECTED,
            ).exists(),
            "reject must still write an EV_WITHDRAW_REJECTED audit log entry",
        )
        self.assertTrue(email_mock.called, "reject must still attempt to queue the status email")


# ── 5. reject_withdrawals — concurrency (functional) ────────────────────────

class RejectWithdrawalsConcurrencyTests(TransactionTestCase):
    def test_two_concurrent_rejects_same_wr_exactly_one_wins_one_refund(self):
        user = make_user(username="f23a_conc_reject_user")
        wallet = make_wallet(user, initial_balance=Decimal("200"))
        wr = _make_pending_wr(user, wallet, "80.00")
        admin_user = make_user(username="f23a_conc_reject_admin", is_staff=True, is_superuser=True)
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_run_reject_until_resolved, args=(ma, admin_user, wr.pk, barrier, results, i))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        for r in results:
            self.assertEqual(r[0], "resolved", f"unexpected thread outcome: {results}")

        wr.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_REJECTED)
        self.assertEqual(wallet.available_balance, Decimal("200.00"), "exactly one refund must have landed")
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=wallet, tx_type=WalletTransaction.TX_CORRECTION,
            ).count(),
            1,
            "exactly one refund WalletTransaction — no double refund",
        )

    def test_approve_vs_reject_concurrent_only_one_economic_resolution_wins(self):
        user = make_user(username="f23a_conc_ar_user")
        wallet = make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_pending_wr(user, wallet, "500.00")
        admin_user = make_user(username="f23a_conc_ar_admin", is_staff=True, is_superuser=True)
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())

        def _do_approve():
            adapter = _FakeAdapter(create_result=_FakeSubmissionResult())
            return submit_withdrawal_to_provider(wr, adapter=adapter, actor=admin_user, callback_url="cb")

        n = 2
        barrier = threading.Barrier(n)
        results = [None] * n
        threads = [
            threading.Thread(target=_run_locked_retry, args=(_do_approve, barrier, results, 0)),
            threading.Thread(target=_run_reject_until_resolved, args=(ma, admin_user, wr.pk, barrier, results, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        wr.refresh_from_db()
        wallet.refresh_from_db()
        correction_txs = WalletTransaction.objects.filter(
            wallet=wallet, tx_type=WalletTransaction.TX_CORRECTION,
        ).count()
        attempts = PayoutAttempt.objects.filter(withdrawal_request=wr).count()

        # Caso A (approve gana): wr.status == processing, 1 PayoutAttempt,
        #   0 refund correction — reject debe haber revalidado != pending.
        # Caso B (reject gana): wr.status == rejected, 0 PayoutAttempt,
        #   1 refund correction — approve debe haber levantado
        #   WithdrawalAlreadyClaimed al revalidar != pending.
        # Cualquier otro resultado (ambos efectos, o ninguno) es un fallo.
        if wr.status == WithdrawalRequest.STATUS_PROCESSING:
            self.assertEqual(attempts, 1, results)
            self.assertEqual(correction_txs, 0, results)
            self.assertEqual(wallet.available_balance, Decimal("500.00"), results)
        elif wr.status == WithdrawalRequest.STATUS_REJECTED:
            self.assertEqual(attempts, 0, results)
            self.assertEqual(correction_txs, 1, results)
            self.assertEqual(wallet.available_balance, Decimal("1000.00"), results)
        else:
            self.fail(f"unexpected terminal wr.status={wr.status!r} — results={results}")


# ── 6. approve_withdrawals — untouched, still functioning ──────────────────

class ApproveWithdrawalsRegressionSanityTests(TestCase):
    """Not a full re-test of FIX-02A.2 (that lives in its own dedicated
    suite, run as a regression below) — just confirms admin.py's edits in
    this block did not disturb approve_withdrawals's own behavior."""

    def test_approve_still_processes_pending(self):
        user = make_user(username="f23a_approve_sanity_user")
        wallet = make_wallet(user, initial_balance=Decimal("1000"))
        wr = _make_pending_wr(user, wallet, "100.00")
        admin_user = make_user(username="f23a_approve_sanity_admin", is_staff=True, is_superuser=True)
        ma = WithdrawalRequestAdmin(WithdrawalRequest, AdminSite())

        from unittest.mock import patch
        with patch("simulator.payout_providers.NowPaymentsAdapter") as AdapterCls:
            AdapterCls.return_value = _FakeAdapter(create_result=_FakeSubmissionResult())
            with patch("simulator.tasks.send_email_async.delay"):
                approve_withdrawals(ma, _admin_request(admin_user),
                                     WithdrawalRequest.objects.filter(pk=wr.pk))

        wr.refresh_from_db()
        self.assertEqual(wr.status, WithdrawalRequest.STATUS_PROCESSING)
        self.assertEqual(PayoutAttempt.objects.filter(withdrawal_request=wr).count(), 1)

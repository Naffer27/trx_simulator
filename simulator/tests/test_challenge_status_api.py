# simulator/tests/test_challenge_status_api.py
"""
Phase 5G — Internal Challenge Status API tests.

Endpoint: GET /api/internal/challenge/status/<external_event_id>/
Auth: Authorization: Bearer <CHALLENGE_STATUS_API_TOKEN>
"""
import json
from decimal import Decimal

from django.test import TestCase, override_settings

from simulator.models import ChallengeEnrollment, TradingAccount
from simulator.tests.factories import (
    make_challenge_enrollment,
    make_challenge_product,
    make_user,
    make_account,
)

TEST_TOKEN   = "test-status-api-token-32-bytes!!"
ENDPOINT_TPL = "/api/internal/challenge/status/{}/"


def _get(client, event_id, token=TEST_TOKEN):
    headers = {}
    if token is not None:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return client.get(ENDPOINT_TPL.format(event_id), **headers)


def _make_enrolled(
    status=ChallengeEnrollment.ST_PHASE_1,
    event_id="evt_status_001",
    tier="10K",
    balance=Decimal("10000"),
    failure_reason=None,
    failed_at_phase=None,
):
    """Create a minimal enrollment with the given status and a phase1_account."""
    user    = make_user(email=f"{event_id}@test.com")
    product = make_challenge_product(
        external_code=f"code_{event_id}",
        tier=tier,
        p1_min_trading_days=5,
        p2_min_trading_days=5,
    )
    p1 = make_account(
        user=user,
        account_type="CHALLENGE",
        tier=tier,
        balance=balance,
        status=TradingAccount.STATUS_ACTIVE,
    )
    enrollment = ChallengeEnrollment.objects.create(
        user=user,
        product=product,
        phase1_account=p1,
        status=status,
        external_event_id=event_id,
        external_payment_id=f"pay_{event_id}",
        failure_reason=failure_reason,
        failed_at_phase=failed_at_phase,
    )
    return enrollment, p1


# ── Authentication ─────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class AuthTests(TestCase):
    def setUp(self):
        self.enrollment, _ = _make_enrolled(event_id="evt_auth_001")

    def test_missing_token_returns_401(self):
        r = self.client.get(ENDPOINT_TPL.format("evt_auth_001"))
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_returns_401(self):
        r = _get(self.client, "evt_auth_001", token="wrong-token")
        self.assertEqual(r.status_code, 401)

    def test_valid_token_returns_200(self):
        r = _get(self.client, "evt_auth_001")
        self.assertEqual(r.status_code, 200)

    def test_post_returns_405(self):
        r = self.client.post(
            ENDPOINT_TPL.format("evt_auth_001"),
            HTTP_AUTHORIZATION=f"Bearer {TEST_TOKEN}",
        )
        self.assertEqual(r.status_code, 405)

    def test_unconfigured_token_returns_401(self):
        with self.settings(CHALLENGE_STATUS_API_TOKEN=""):
            r = _get(self.client, "evt_auth_001")
            self.assertEqual(r.status_code, 401)


# ── Lookup ─────────────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class LookupTests(TestCase):
    def test_unknown_event_id_returns_404(self):
        r = _get(self.client, "nonexistent_event_id")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not found", json.loads(r.content)["error"].lower())

    def test_known_event_id_returns_200(self):
        _make_enrolled(event_id="evt_lookup_001")
        r = _get(self.client, "evt_lookup_001")
        self.assertEqual(r.status_code, 200)


# ── Phase 1 fields ─────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class Phase1Tests(TestCase):
    def setUp(self):
        self.enrollment, self.account = _make_enrolled(
            event_id="evt_p1_001",
            status=ChallengeEnrollment.ST_PHASE_1,
            balance=Decimal("10500"),
        )
        r = _get(self.client, "evt_p1_001")
        self.data = json.loads(r.content)

    def test_ok_true(self):
        self.assertTrue(self.data["ok"])

    def test_status_is_phase1(self):
        self.assertEqual(self.data["status"], "PHASE_1")

    def test_evaluation_status_in_progress(self):
        self.assertEqual(self.data["evaluation_status"], "IN_PROGRESS")

    def test_is_passing_true(self):
        self.assertTrue(self.data["is_passing"])

    def test_account_id_present(self):
        self.assertEqual(self.data["account_id"], self.account.pk)

    def test_account_type_challenge(self):
        self.assertEqual(self.data["account_type"], "CHALLENGE")

    def test_balance_present(self):
        self.assertAlmostEqual(self.data["balance"], 10500.0, places=2)

    def test_product_name_present(self):
        self.assertIsNotNone(self.data["product_name"])

    def test_enrollment_id_present(self):
        self.assertEqual(self.data["enrollment_id"], self.enrollment.pk)

    def test_external_event_id_echoed(self):
        self.assertEqual(self.data["external_event_id"], "evt_p1_001")

    def test_login_url_present(self):
        self.assertIn("/login/", self.data["login_url"])

    def test_trading_url_contains_account_id(self):
        self.assertIn(str(self.account.pk), self.data["trading_url"])

    def test_fail_reason_null(self):
        self.assertIsNone(self.data["fail_reason"])


# ── Phase 2 fields ─────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class Phase2Tests(TestCase):
    def setUp(self):
        user    = make_user(email="p2user@test.com")
        product = make_challenge_product(
            external_code="code_p2",
            p1_min_trading_days=5,
            p2_min_trading_days=5,
        )
        p1 = make_account(user=user, account_type="CHALLENGE", status="Completado")
        p2 = make_account(
            user=user,
            account_type="CHALLENGE",
            tier="10K",
            balance=Decimal("10300"),
            status=TradingAccount.STATUS_ACTIVE,
        )
        self.enrollment = ChallengeEnrollment.objects.create(
            user=user,
            product=product,
            phase1_account=p1,
            phase2_account=p2,
            status=ChallengeEnrollment.ST_PHASE_2,
            external_event_id="evt_p2_001",
        )
        self.p2_account = p2
        r = _get(self.client, "evt_p2_001")
        self.data = json.loads(r.content)

    def test_status_is_phase2(self):
        self.assertEqual(self.data["status"], "PHASE_2")

    def test_account_id_is_phase2_account(self):
        self.assertEqual(self.data["account_id"], self.p2_account.pk)

    def test_is_passing_true(self):
        self.assertTrue(self.data["is_passing"])

    def test_evaluation_status_in_progress(self):
        self.assertEqual(self.data["evaluation_status"], "IN_PROGRESS")

    def test_balance_from_phase2_account(self):
        self.assertAlmostEqual(self.data["balance"], 10300.0, places=2)


# ── Funded fields ──────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class FundedTests(TestCase):
    def setUp(self):
        user    = make_user(email="funded@test.com")
        product = make_challenge_product(external_code="code_funded")
        p1 = make_account(user=user, account_type="CHALLENGE", status="Completado")
        p2 = make_account(user=user, account_type="CHALLENGE", status="Completado")
        funded = make_account(
            user=user,
            account_type="FUNDED",
            balance=Decimal("10000"),
            status=TradingAccount.STATUS_ACTIVE,
        )
        self.enrollment = ChallengeEnrollment.objects.create(
            user=user,
            product=product,
            phase1_account=p1,
            phase2_account=p2,
            funded_account=funded,
            status=ChallengeEnrollment.ST_FUNDED,
            external_event_id="evt_funded_001",
        )
        self.funded_account = funded
        r = _get(self.client, "evt_funded_001")
        self.data = json.loads(r.content)

    def test_status_is_funded(self):
        self.assertEqual(self.data["status"], "FUNDED")

    def test_is_passing_true(self):
        self.assertTrue(self.data["is_passing"])

    def test_evaluation_status_passed(self):
        self.assertEqual(self.data["evaluation_status"], "PASSED")

    def test_account_id_is_funded_account(self):
        self.assertEqual(self.data["account_id"], self.funded_account.pk)

    def test_fail_reason_null(self):
        self.assertIsNone(self.data["fail_reason"])


# ── Failed fields ──────────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class FailedTests(TestCase):
    def setUp(self):
        self.enrollment, self.account = _make_enrolled(
            event_id="evt_failed_001",
            status=ChallengeEnrollment.ST_FAILED,
            failure_reason="Max drawdown exceeded: 10.50% >= 10.00%",
            failed_at_phase="PHASE_1",
        )
        r = _get(self.client, "evt_failed_001")
        self.data = json.loads(r.content)

    def test_status_is_failed(self):
        self.assertEqual(self.data["status"], "FAILED")

    def test_is_passing_false(self):
        self.assertFalse(self.data["is_passing"])

    def test_evaluation_status_failed(self):
        self.assertEqual(self.data["evaluation_status"], "FAILED")

    def test_fail_reason_returned(self):
        self.assertEqual(
            self.data["fail_reason"],
            "Max drawdown exceeded: 10.50% >= 10.00%",
        )

    def test_failed_at_phase_returned(self):
        self.assertEqual(self.data["failed_at_phase"], "PHASE_1")

    def test_account_id_present(self):
        self.assertEqual(self.data["account_id"], self.account.pk)


# ── Computed metrics ───────────────────────────────────────────────────────────

@override_settings(CHALLENGE_STATUS_API_TOKEN=TEST_TOKEN)
class ComputedMetricsTests(TestCase):
    """Verify derived fields are computed correctly from account state."""

    def _enroll(self, balance, peak_balance=None, profit_target=None, event_id="evt_metrics_001"):
        user    = make_user(email=f"{event_id}@test.com")
        product = make_challenge_product(
            external_code=f"code_{event_id}",
            p1_profit_target_pct=Decimal("8.00"),
            p1_max_drawdown_pct=Decimal("10.00"),
            p1_max_daily_loss_pct=Decimal("5.00"),
            p1_min_trading_days=5,
            p1_max_duration_days=30,
        )
        # TradingAccount.save() synchronises balance/peak_balance to initial_balance
        # on creation — use update() to set mid-challenge state without triggering save().
        account = make_account(
            user=user,
            account_type="CHALLENGE",
            tier="10K",
            status=TradingAccount.STATUS_ACTIVE,
        )
        update = {
            "initial_balance": Decimal("10000"),
            "balance":         Decimal(str(balance)),
            "equity":          Decimal(str(balance)),
            "peak_balance":    Decimal(str(peak_balance or balance)),
        }
        if profit_target is not None:
            update["profit_target"] = Decimal(str(profit_target))
        TradingAccount.objects.filter(pk=account.pk).update(**update)
        account.refresh_from_db()
        enrollment = ChallengeEnrollment.objects.create(
            user=user,
            product=product,
            phase1_account=account,
            status=ChallengeEnrollment.ST_PHASE_1,
            external_event_id=event_id,
        )
        return enrollment, account

    def test_profit_target_progress_pct_computed(self):
        """Balance 10500 on 10K account, profit_target=800 (8%) → gained=500 → progress=500/800=62.5%"""
        self._enroll(
            balance=Decimal("10500"),
            profit_target=Decimal("800"),
            event_id="evt_m_ptp",
        )
        r = _get(self.client, "evt_m_ptp")
        data = json.loads(r.content)
        self.assertAlmostEqual(data["profit_target_progress_pct"], 62.5, places=1)

    def test_max_drawdown_pct_computed(self):
        """peak=11000, balance=10000 → drawdown = 1000/11000 ≈ 9.09%"""
        self._enroll(
            balance=Decimal("10000"),
            peak_balance=Decimal("11000"),
            event_id="evt_m_dd",
        )
        r = _get(self.client, "evt_m_dd")
        data = json.loads(r.content)
        self.assertAlmostEqual(data["max_drawdown_pct"], 9.09, places=1)

    def test_days_remaining_computed(self):
        """A brand-new account: days_elapsed=0 → days_remaining=max_duration_days(30)."""
        self._enroll(balance=Decimal("10000"), event_id="evt_m_days")
        r = _get(self.client, "evt_m_days")
        data = json.loads(r.content)
        self.assertEqual(data["days_remaining"], 30)

    def test_min_trading_days_required_from_product(self):
        self._enroll(balance=Decimal("10000"), event_id="evt_m_td")
        r = _get(self.client, "evt_m_td")
        data = json.loads(r.content)
        self.assertEqual(data["min_trading_days_required"], 5)

    def test_min_trading_days_current_zero_no_trades(self):
        self._enroll(balance=Decimal("10000"), event_id="evt_m_td0")
        r = _get(self.client, "evt_m_td0")
        data = json.loads(r.content)
        self.assertEqual(data["min_trading_days_current"], 0)

    def test_response_contains_all_required_fields(self):
        self._enroll(balance=Decimal("10000"), event_id="evt_m_fields")
        r = _get(self.client, "evt_m_fields")
        data = json.loads(r.content)
        required = {
            "ok", "external_event_id", "product_external_code", "product_name",
            "enrollment_id", "account_id", "account_type", "phase", "status",
            "balance", "equity", "account_size",
            "profit_target_progress_pct", "daily_drawdown_pct", "max_drawdown_pct",
            "min_trading_days_current", "min_trading_days_required", "days_remaining",
            "evaluation_status", "is_passing", "fail_reason", "login_url", "trading_url",
        }
        missing = required - data.keys()
        self.assertSetEqual(missing, set(), msg=f"Missing fields: {missing}")

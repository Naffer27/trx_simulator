"""
load_tests/locustfile.py
HTTP load tests for trx_sim using Locust.

Scenarios:
  AnonymousUser  — health check, landing page (no auth)
  AuthUser       — login → dashboard → wallet balance → history
  StaffUser      — login as staff → ops panel → metrics → monitoring

Prerequisites:
  1. Daphne running (real ASGI server, NOT runserver)
  2. .env with LOAD_TEST_MODE=True (bypasses HTTP rate limiting)
  3. Test users created:
       python manage.py shell -c "
       from django.contrib.auth import get_user_model
       U = get_user_model()
       for i in range(1, 21):
           u, _ = U.objects.get_or_create(username=f'loadtest_{i}')
           u.set_password('LoadTest123!')
           u.save()
       staff, _ = U.objects.get_or_create(username='loadtest_staff')
       staff.set_password('LoadTest123!')
       staff.is_staff = True
       staff.save()
       print('Done')
       "

Run:
  locust -f load_tests/locustfile.py \
    --host http://127.0.0.1:8001 \
    --users 50 --spawn-rate 5 --run-time 5m \
    --headless --html load_tests/results/http_report.html

Web UI:
  locust -f load_tests/locustfile.py --host http://127.0.0.1:8001
  Then open http://localhost:8089
"""
import random
from locust import HttpUser, task, between, events
import logging

log = logging.getLogger("locust.trx_sim")

_TEST_PASSWORD = "LoadTest123!"
_STAFF_USER = "loadtest_staff"


# ── Helper: extract CSRF token ────────────────────────────────

def _get_csrf(client) -> str:
    """GET a page and return the CSRF token from the cookie."""
    return client.cookies.get("csrftoken", "")


# ═══════════════════════════════════════════════════════════════
# Scenario 1: Anonymous user — no auth required
# ═══════════════════════════════════════════════════════════════

class AnonymousUser(HttpUser):
    """
    Simulates public traffic: health checks, landing page.
    Fastest scenario — no auth, no session.
    """
    wait_time = between(1, 3)
    weight = 2  # 2x more anonymous traffic than authenticated

    @task(5)
    def health_check(self):
        with self.client.get("/api/health/", catch_response=True) as r:
            if r.status_code == 200:
                r.success()
            else:
                r.failure(f"Health check returned {r.status_code}")

    @task(2)
    def landing_page(self):
        self.client.get("/", name="/landing")

    @task(1)
    def login_page_get(self):
        self.client.get("/login/", name="/login [GET]")


# ═══════════════════════════════════════════════════════════════
# Scenario 2: Authenticated user — full user flow
# ═══════════════════════════════════════════════════════════════

class AuthUser(HttpUser):
    """
    Simulates an authenticated trader.
    Login → dashboard → wallet → history → logout.
    """
    wait_time = between(2, 6)
    weight = 3

    def on_start(self):
        """Called once per simulated user at start."""
        n = random.randint(1, 20)
        self._username = f"loadtest_{n}"
        self._logged_in = False
        self._do_login()

    def _do_login(self):
        # Step 1: GET login to seed CSRF cookie
        self.client.get("/login/", name="/login [GET]")
        csrf = _get_csrf(self.client)

        # Step 2: POST credentials
        with self.client.post(
            "/login/",
            data={
                "username": self._username,
                "password": _TEST_PASSWORD,
                "csrfmiddlewaretoken": csrf,
            },
            headers={"Referer": f"{self.host}/login/"},
            name="/login [POST]",
            allow_redirects=True,
            catch_response=True,
        ) as r:
            if r.status_code in (200, 302):
                self._logged_in = True
                r.success()
            else:
                r.failure(f"Login failed: {r.status_code}")

    def on_stop(self):
        """Logout when the simulated user stops."""
        if self._logged_in:
            self.client.get("/logout/", name="/logout", allow_redirects=False)

    @task(4)
    def home_dashboard(self):
        if not self._logged_in:
            return
        with self.client.get("/home/", name="/home", catch_response=True) as r:
            if r.status_code in (200, 302):
                r.success()
            else:
                r.failure(f"Home returned {r.status_code}")

    @task(3)
    def wallet_balance(self):
        if not self._logged_in:
            return
        with self.client.get(
            "/api/wallet-balance/",
            name="/api/wallet-balance",
            catch_response=True,
        ) as r:
            if r.status_code == 200:
                r.success()
            elif r.status_code == 403:
                r.success()  # expected if no wallet yet
            else:
                r.failure(f"Wallet balance returned {r.status_code}")

    @task(2)
    def history(self):
        if not self._logged_in:
            return
        self.client.get("/history/", name="/history")

    @task(1)
    def accounts_page(self):
        if not self._logged_in:
            return
        self.client.get("/accounts/", name="/accounts")

    @task(1)
    def deposit_page(self):
        if not self._logged_in:
            return
        self.client.get("/deposit/", name="/deposit")

    @task(1)
    def health_check(self):
        """Auth users also check health."""
        self.client.get("/api/health/", name="/api/health")


# ═══════════════════════════════════════════════════════════════
# Scenario 3: Staff user — operational endpoints
# ═══════════════════════════════════════════════════════════════

class StaffUser(HttpUser):
    """
    Simulates a staff operator using internal dashboards.
    Lower frequency, higher response time tolerance.
    """
    wait_time = between(5, 15)
    weight = 1

    def on_start(self):
        self._logged_in = False
        self.client.get("/login/", name="/login [GET]")
        csrf = _get_csrf(self.client)
        with self.client.post(
            "/login/",
            data={
                "username": _STAFF_USER,
                "password": _TEST_PASSWORD,
                "csrfmiddlewaretoken": csrf,
            },
            headers={"Referer": f"{self.host}/login/"},
            name="/login [POST staff]",
            allow_redirects=True,
            catch_response=True,
        ) as r:
            if r.status_code in (200, 302):
                self._logged_in = True
                r.success()
            else:
                r.failure(f"Staff login failed: {r.status_code}")

    @task(3)
    def ops_panel(self):
        if not self._logged_in:
            return
        with self.client.get("/staff/ops/", name="/staff/ops", catch_response=True) as r:
            if r.status_code == 200:
                r.success()
            elif r.status_code == 302:
                r.success()  # redirect to login if not staff
            else:
                r.failure(f"Ops panel returned {r.status_code}")

    @task(2)
    def metrics(self):
        if not self._logged_in:
            return
        with self.client.get(
            "/api/metrics/",
            name="/api/metrics",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 503):
                r.success()  # 503 = degraded but alive
            else:
                r.failure(f"Metrics returned {r.status_code}")

    @task(1)
    def broker_monitoring(self):
        if not self._logged_in:
            return
        with self.client.get(
            "/api/broker/monitoring/",
            name="/api/broker/monitoring",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 503):
                r.success()
            else:
                r.failure(f"Monitoring returned {r.status_code}")


# ── Event hooks ───────────────────────────────────────────────

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    log.info("=== Load test starting — host=%s ===", environment.host)
    log.info("REMINDER: ensure LOAD_TEST_MODE=True in .env to bypass rate limiting")

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    stats = environment.stats.total
    log.info(
        "=== Load test complete — requests=%d failures=%d p95=%.0fms ===",
        stats.num_requests, stats.num_failures,
        stats.get_response_time_percentile(0.95) or 0,
    )

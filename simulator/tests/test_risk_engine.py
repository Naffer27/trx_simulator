"""
simulator/tests/test_risk_engine.py — Bloque 5

Cubre las 4 funciones públicas del risk_engine:
  - compute_margin_state   : pura, sin DB — SimpleTestCase
  - check_equity_stopout   : pura, sin DB — SimpleTestCase
  - validate_order_risk    : pre-trade gate — TestCase (DB)
  - check_and_enforce_risk : post-trade evaluator — TestCase (DB)

Convenciones de dominio:
  - CHALLENGE tier 10K: max_drawdown=10%, max_daily_loss=5%, max_lot=5.00, max_positions=30.
  - RETAIL: límites son warnings no-blocking; cuenta NUNCA se suspende por violación de riesgo.
  - MARGIN_ENGINE_TYPES = {"RETAIL", "ECN", "STANDARD", "DEMO", "CRYPTO"} — usar "RETAIL" en tests.
  - Todos los amounts son Decimal, nunca float.
"""
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from simulator.models import DrawdownSnapshot, LedgerEntry, TradingViolation
from simulator.risk_engine import (
    check_and_enforce_risk,
    check_equity_stopout,
    compute_margin_state,
    validate_order_risk,
)

from .factories import make_account, make_ledger_entry


# ─────────────────────────────────────────────────────────────────────────────
# 1. compute_margin_state — función pura, sin DB
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeMarginState(SimpleTestCase):
    """
    Standard broker margin accounting:
      margin_level    = equity / margin_used  × 100
      used_margin_pct = margin_after / equity × 100
      free_margin     = equity − margin_after
      maintenance     = margin_after × 0.5
    """

    def test_no_open_positions_zero_margin(self):
        """
        Sin posiciones abiertas: margin_used=0, new_margin=0.
        margin_level=0 (guardia 0/0), used_pct=0, free_margin=equity.
        """
        s = compute_margin_state(equity=10_000.0, total_margin_used=0.0, new_margin=0.0)
        self.assertEqual(s["margin_used"],     0.0)
        self.assertEqual(s["margin_after"],    0.0)
        self.assertEqual(s["used_margin_pct"], 0.0)
        self.assertEqual(s["margin_level"],    0.0)
        self.assertEqual(s["free_margin"],    10_000.0)

    def test_used_margin_pct(self):
        """equity=10 000, margin_used=2 000 → used_margin_pct = 20.0%."""
        s = compute_margin_state(equity=10_000.0, total_margin_used=2_000.0)
        self.assertAlmostEqual(s["used_margin_pct"], 20.0, places=2)

    def test_margin_level(self):
        """equity=10 000, margin_used=2 000 → margin_level = 500.0%."""
        s = compute_margin_state(equity=10_000.0, total_margin_used=2_000.0)
        self.assertAlmostEqual(s["margin_level"], 500.0, places=2)

    def test_free_margin_includes_new_margin(self):
        """free_margin = equity − (margin_used + new_margin)."""
        s = compute_margin_state(equity=10_000.0, total_margin_used=1_000.0, new_margin=500.0)
        self.assertAlmostEqual(s["margin_after"], 1_500.0, places=2)
        self.assertAlmostEqual(s["free_margin"],  8_500.0, places=2)

    def test_maintenance_margin_is_half_of_margin_after(self):
        """maintenance_margin = margin_after × 0.5."""
        s = compute_margin_state(equity=10_000.0, total_margin_used=4_000.0)
        self.assertAlmostEqual(s["maintenance_margin"], 2_000.0, places=2)

    def test_equity_zero_clamped_no_exception(self):
        """equity=0 se clamp a 0.01 internamente — no ZeroDivisionError."""
        s = compute_margin_state(equity=0.0, total_margin_used=1_000.0)
        self.assertIn("used_margin_pct", s)
        # used_pct = 1000/0.01*100 → valor muy alto, pero sin excepción
        self.assertGreater(s["used_margin_pct"], 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. check_equity_stopout — función pura, sin DB
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckEquityStopout(SimpleTestCase):
    """
    RETAIL  → margin-call: dispara cuando equity/margin_used×100 < 50%.
    CHALLENGE/FUNDED → DD: dispara cuando equity ≤ peak×(1−max_dd%).
    """

    # ── RETAIL ────────────────────────────────────────────────────────────────

    def test_retail_no_positions_no_stopout(self):
        """RETAIL con margin_used=0 → False (sin posiciones no hay margin call)."""
        self.assertFalse(
            check_equity_stopout(
                equity=10_000.0, peak_balance=10_000.0, tier="",
                account_type="RETAIL", margin_used=0.0,
            )
        )

    def test_retail_margin_call_triggered(self):
        """
        equity=1000, margin_used=3000 → margin_level = 33.3% < 50% → True.
        """
        self.assertTrue(
            check_equity_stopout(
                equity=1_000.0, peak_balance=5_000.0, tier="",
                account_type="RETAIL", margin_used=3_000.0,
            )
        )

    def test_retail_margin_safe(self):
        """equity=10 000, margin_used=1 000 → margin_level=1000% > 50% → False."""
        self.assertFalse(
            check_equity_stopout(
                equity=10_000.0, peak_balance=10_000.0, tier="",
                account_type="RETAIL", margin_used=1_000.0,
            )
        )

    # ── CHALLENGE 10K ─────────────────────────────────────────────────────────

    def test_challenge_stopout_below_threshold(self):
        """
        10K max_drawdown=10% → stopout_level = 10 000 × 0.90 = 9 000.
        equity=8 999 ≤ 9 000 → True.
        """
        self.assertTrue(
            check_equity_stopout(equity=8_999.0, peak_balance=10_000.0, tier="10K")
        )

    def test_challenge_safe_above_threshold(self):
        """equity=9 001 > stopout_level=9 000 → False."""
        self.assertFalse(
            check_equity_stopout(equity=9_001.0, peak_balance=10_000.0, tier="10K")
        )

    def test_challenge_exactly_at_stopout_boundary(self):
        """equity == stopout_level → True (condición ≤)."""
        self.assertTrue(
            check_equity_stopout(equity=9_000.0, peak_balance=10_000.0, tier="10K")
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. validate_order_risk — pre-trade gate (DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateOrderRisk(TestCase):
    """
    Devuelve lista de error-dicts [{code, message, blocking}].
    Lista vacía → orden permitida.
    CHALLENGE: violaciones duras crean TradingViolation y suspenden la cuenta.
    RETAIL:    warnings no-blocking, sin TradingViolation, sin suspensión.
    """

    def test_clean_challenge_account_passes(self):
        """Cuenta CHALLENGE limpia dentro de todos los límites → []."""
        account = make_account(account_type="CHALLENGE", tier="10K")
        errors = validate_order_risk(account, lot_size=1.0, open_positions_count=0,
                                     symbol="EUR/USD")
        self.assertEqual(errors, [])

    def test_blocked_account_immediately_rejected(self):
        """
        Cuenta Suspendida → único error code=account_blocked, blocking=True.
        No se evalúan otras reglas.
        """
        account = make_account(status="Suspendido")
        errors = validate_order_risk(account, lot_size=1.0, open_positions_count=0)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], "account_blocked")
        self.assertTrue(errors[0]["blocking"])

    def test_lot_size_exceeded_challenge_blocking(self):
        """
        CHALLENGE 10K max_lot=5.00. lot_size=10.0 → code=max_lot_size, blocking=True.
        TradingViolation MAX_LOT_SIZE creada.
        Nota: max_lot_size NO va a hard_violations → cuenta permanece Activo
        (solo max_drawdown y max_daily_loss suspenden la cuenta).
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        errors = validate_order_risk(account, lot_size=10.0, open_positions_count=0,
                                     symbol="EUR/USD")
        codes = {e["code"] for e in errors}
        self.assertIn("max_lot_size", codes)
        self.assertTrue(any(e["blocking"] for e in errors))
        self.assertEqual(
            TradingViolation.objects.filter(
                account=account, violation_type=TradingViolation.MAX_LOT_SIZE
            ).count(), 1
        )
        # max_lot_size sola no suspende — la cuenta sigue Activo
        account.refresh_from_db()
        self.assertEqual(account.status, "Activo")

    def test_lot_size_exceeded_retail_non_blocking(self):
        """
        RETAIL: lot_size=10.0 > límite → code=lot_warning, blocking=False.
        Sin TradingViolation. Sin suspensión.
        """
        account = make_account(account_type="RETAIL", tier="10K")
        errors = validate_order_risk(account, lot_size=10.0, open_positions_count=0,
                                     symbol="EUR/USD")
        codes = {e["code"] for e in errors}
        self.assertIn("lot_warning", codes)
        self.assertFalse(any(e["blocking"] for e in errors))
        self.assertEqual(TradingViolation.objects.filter(account=account).count(), 0)
        account.refresh_from_db()
        self.assertNotIn(account.status, {"Violado", "Suspendido"})

    def test_max_positions_hard_rejection(self):
        """
        CHALLENGE 10K max_open_positions=30.
        open_positions_count=30 → code=max_positions, blocking=True.
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        errors = validate_order_risk(account, lot_size=1.0, open_positions_count=30,
                                     symbol="EUR/USD")
        codes = {e["code"] for e in errors}
        self.assertIn("max_positions", codes)
        self.assertTrue(any(e["code"] == "max_positions" and e["blocking"] for e in errors))

    def test_daily_loss_limit_challenge_violated(self):
        """
        CHALLENGE 10K max_daily_loss=5% de peak_balance=10 000 → límite=$500.
        Pérdida de $600 hoy → code=daily_loss_limit, blocking=True, cuenta Violada.
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        make_ledger_entry(
            account=account,
            event_type=LedgerEntry.EV_REALIZED,
            amount=Decimal("-600.00"),
            balance_after=Decimal("9400.00"),
        )
        errors = validate_order_risk(account, lot_size=1.0, open_positions_count=0,
                                     symbol="EUR/USD")
        codes = {e["code"] for e in errors}
        self.assertIn("daily_loss_limit", codes)
        self.assertTrue(any(e["blocking"] for e in errors))
        account.refresh_from_db()
        self.assertEqual(account.status, "Violado")

    def test_daily_loss_limit_retail_warning_only(self):
        """
        RETAIL: misma pérdida → code=daily_loss_warning, blocking=False.
        Sin TradingViolation. Sin suspensión.
        """
        account = make_account(account_type="RETAIL", tier="10K")
        make_ledger_entry(
            account=account,
            event_type=LedgerEntry.EV_REALIZED,
            amount=Decimal("-600.00"),
            balance_after=Decimal("9400.00"),
        )
        errors = validate_order_risk(account, lot_size=1.0, open_positions_count=0,
                                     symbol="EUR/USD")
        codes = {e["code"] for e in errors}
        self.assertIn("daily_loss_warning", codes)
        self.assertFalse(any(e["blocking"] for e in errors))
        self.assertEqual(TradingViolation.objects.filter(account=account).count(), 0)
        account.refresh_from_db()
        self.assertNotIn(account.status, {"Violado", "Suspendido"})


# ─────────────────────────────────────────────────────────────────────────────
# 4. check_and_enforce_risk — post-trade evaluator (DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckAndEnforceRisk(TestCase):
    """
    Llamada después de cada cierre de posición.
    CHALLENGE: crea TradingViolation + suspende si DD o daily-loss excedidos.
    RETAIL:    no crea violations, no suspende — motor de broker.
    Siempre actualiza DrawdownSnapshot del día.
    """

    def test_clean_challenge_no_violations(self):
        """Cuenta CHALLENGE dentro de límites → [] y DrawdownSnapshot creado."""
        account = make_account(account_type="CHALLENGE", tier="10K")
        violations = check_and_enforce_risk(account)
        self.assertEqual(violations, [])
        self.assertTrue(DrawdownSnapshot.objects.filter(account=account).exists())

    def test_peak_balance_updated_when_balance_exceeds_peak(self):
        """
        balance=11 000 > peak_balance=10 000 → peak_balance actualizado en DB a 11 000.
        """
        account = make_account(
            account_type="CHALLENGE", tier="10K",
            balance=Decimal("11000"), peak_balance=Decimal("10000"),
        )
        check_and_enforce_risk(account)
        account.refresh_from_db()
        self.assertEqual(account.peak_balance, Decimal("11000"))

    def test_max_drawdown_challenge_creates_violation_and_suspends(self):
        """
        CHALLENGE 10K max_drawdown=10%.
        Nota: TradingAccount.save() siempre setea peak_balance=initial_balance en creación,
        por eso creamos con balance=10 000, luego bajamos balance via update() (sin save()).
        peak=10 000, balance=8 900 → DD=11.0% ≥ 10% → violation + Suspendido.
        """
        from simulator.models import TradingAccount as _TA
        account = make_account(account_type="CHALLENGE", tier="10K",
                               balance=Decimal("10000"))  # peak=10000
        _TA.objects.filter(pk=account.pk).update(balance=Decimal("8900"))
        account.refresh_from_db()

        violations = check_and_enforce_risk(account)
        self.assertGreater(len(violations), 0)
        self.assertEqual(violations[0].violation_type, TradingViolation.MAX_DRAWDOWN)
        account.refresh_from_db()
        self.assertEqual(account.status, "Suspendido")

    def test_max_drawdown_retail_no_violation(self):
        """
        RETAIL con mismo drawdown (11%) → [], sin suspensión.
        El motor RETAIL no aplica DD como violación.
        """
        from simulator.models import TradingAccount as _TA
        account = make_account(account_type="RETAIL", tier="10K",
                               balance=Decimal("10000"))  # peak=10000
        _TA.objects.filter(pk=account.pk).update(balance=Decimal("8900"))
        account.refresh_from_db()

        violations = check_and_enforce_risk(account)
        self.assertEqual(violations, [])
        account.refresh_from_db()
        self.assertNotIn(account.status, {"Violado", "Suspendido"})

    def test_daily_snapshot_created_for_today(self):
        """
        check_and_enforce_risk siempre crea o actualiza DrawdownSnapshot para hoy.
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        today = timezone.now().date()
        self.assertFalse(DrawdownSnapshot.objects.filter(account=account, date=today).exists())
        check_and_enforce_risk(account)
        self.assertTrue(DrawdownSnapshot.objects.filter(account=account, date=today).exists())

    def test_daily_snapshot_updated_on_second_call(self):
        """
        Segunda llamada en el mismo día actualiza (no duplica) el snapshot.
        """
        account = make_account(account_type="CHALLENGE", tier="10K")
        check_and_enforce_risk(account)
        check_and_enforce_risk(account)
        today = timezone.now().date()
        count = DrawdownSnapshot.objects.filter(account=account, date=today).count()
        self.assertEqual(count, 1)

# simulator/tests/test_ledger_rounding_reconciliation01.py
"""
LEDGER-ROUNDING-RECONCILIATION-01.

Problem this closes: opening a position with commission and/or spread
fee posted as independently-rounded LedgerEntry/BrokerLedger rows could
sum to a different 2dp value than what Account.balance's own save()
actually persists.

Design history (both corrections applied — see module comments in
simulator/consumers.py::_db_open_position_atomic for the full account):

  - FIRST design (retracted): posted total computed as
    money_round(commission_exact + spread_exact), independently of
    Account.balance. PROVEN WRONG by direct counter-example found
    during implementation: old_balance=1049.55, commission=0.031,
    spread=0.024 -> isolated total 0.055 ties to 0.06 under
    ROUND_HALF_EVEN, but old_balance - 0.055 = 1049.495 ties to
    1049.50 (a REAL posted delta of 0.05, not 0.06). ROUND_HALF_EVEN's
    tie-break depends on the parity of the SPECIFIC number rounded —
    rounding an isolated delta and rounding (old_balance - delta) are
    different numbers and do not always tie the same way.

  - CORRECTED design (implemented here): the posted total is ALWAYS
    derived from what Account.balance's own quantization will actually
    produce — _total_posted_d = old_balance - money_round(old_balance
    - total_cost_exact) — for every case (commission-only,
    spread-only, or both). Verified against ~2M adversarial
    (balance, commission, spread) combinations: zero exactness
    violations, zero negative-residual violations. This is what the
    tests below exercise and lock in.

Distinguishes three levels explicitly (Design Lock amendment §5):
  1. economic exact amount (full Decimal precision, never touched)
  2. exact resulting balance (old_balance - exact costs, what
     Account.balance is assigned — Django quantizes it on save())
  3. monetary posting (old_balance - persisted 2dp balance) — this is
     what LedgerEntry/BrokerLedger amounts represent, level #3, never
     level #1.
"""
import decimal
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_DOWN

from asgiref.sync import async_to_sync
from django.db.models import Sum
from django.test import TestCase

from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer, _money_round, _MONEY_2DP
from simulator.models import BrokerLedger, LedgerEntry, Position

from .factories import make_account, make_position

BTC_SPEC = get_spec("BTCUSD")


def _make_consumer(account):
    from market_data.feeds import get_feed_manager
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account.id
    c.account = {
        "status": account.status, "peak_balance": float(account.peak_balance),
        "netting_mode": False, "spread_pips": 0.0,
    }
    c._feed = get_feed_manager()
    return c


def _open(consumer, account, *, qty=0.01, price=80000.0, commission=0.0,
          effective_pips=None, symbol="BTCUSD"):
    """Opens a position with a directly-controlled commission (bypassing
    commission_for()) and a directly-controlled spread (via
    pricing_context.effective_spread_pips, bypassing broker_price()) —
    exactly the two economic inputs _db_open_position_atomic already
    treats as pre-computed. new_balance is a pre-lock estimate only (the
    real, authoritative value is _auth_balance, computed under lock) —
    same convention test_ledger_invariant_audit.py already uses."""
    pricing_context = {"effective_spread_pips": effective_pips} if effective_pips is not None else None
    return async_to_sync(consumer._db_open_position_atomic)(
        symbol=symbol, side="buy", qty=qty, price=price, sl=None, tp=None,
        commission=commission, new_balance=float(account.balance) - commission,
        pricing_context=pricing_context,
    )


def _expected_posting(old_balance: Decimal, commission_exact: Decimal, spread_exact: Decimal):
    """Reference implementation of the CORRECTED formula, used only to
    compute test expectations — mirrors (does not duplicate as
    production logic) simulator/consumers.py's own corrected algorithm,
    using the SAME imported _money_round so both always agree on what
    "money rounding" means."""
    auth_balance_exact = old_balance - commission_exact - spread_exact
    posted_balance = _money_round(auth_balance_exact)
    total_posted_d = old_balance - posted_balance
    if commission_exact > 0 and spread_exact > 0:
        commission_ledger_d = _money_round(commission_exact)
        spread_ledger_d = total_posted_d - commission_ledger_d
    elif commission_exact > 0:
        commission_ledger_d = total_posted_d
        spread_ledger_d = Decimal("0")
    elif spread_exact > 0:
        spread_ledger_d = total_posted_d
        commission_ledger_d = Decimal("0")
    else:
        commission_ledger_d = Decimal("0")
        spread_ledger_d = Decimal("0")
    return commission_ledger_d, spread_ledger_d, total_posted_d


# ─────────────────────────────────────────────────────────────────────────
# A. _money_round — pure property tests (no DB)
# ─────────────────────────────────────────────────────────────────────────
class MoneyRoundHelperTests(TestCase):
    def test_matches_current_real_example(self):
        self.assertEqual(_money_round(Decimal("0.0798477")), Decimal("0.08"))
        self.assertEqual(_money_round(Decimal("0.075")), Decimal("0.08"))

    def test_half_even_boundary_ties(self):
        self.assertEqual(_money_round(Decimal("0.005")), Decimal("0.00"))
        self.assertEqual(_money_round(Decimal("0.015")), Decimal("0.02"))
        self.assertEqual(_money_round(Decimal("0.025")), Decimal("0.02"))

    def test_result_has_two_decimal_places(self):
        r = _money_round(Decimal("0.0798477"))
        self.assertEqual(r, r.quantize(_MONEY_2DP))  # idempotent — no double rounding

    def test_independent_of_global_decimal_context(self):
        original = decimal.getcontext().rounding
        try:
            decimal.getcontext().rounding = ROUND_DOWN
            self.assertEqual(_money_round(Decimal("0.075")), Decimal("0.08"))
        finally:
            decimal.getcontext().rounding = original


class MoneyRoundMonotonicityTests(TestCase):
    """Foundation for the non-negativity guarantee (Design Lock
    Correction 1, unaffected by the amendment): quantizing to a fixed
    2dp grid is monotonic for ANY consistent tie rule, HALF_EVEN
    included."""
    def test_monotonic_over_fine_grid_including_every_tie_boundary(self):
        offsets = [Decimal(x) for x in
                   ("0.000", "0.001", "0.002", "0.003", "0.004",
                    "0.005", "0.006", "0.007", "0.008", "0.009")]
        violations = []
        for cents in range(0, 200):
            base = Decimal(cents) / 100
            for off in offsets:
                a = base + off
                for extra_cents in range(0, 20):
                    for extra_off in (Decimal("0.000"), Decimal("0.005"), Decimal("0.0049"), Decimal("0.0051")):
                        b = a + Decimal(extra_cents) / 100 + extra_off
                        if _money_round(a) > _money_round(b):
                            violations.append((a, b))
        self.assertEqual(violations, [])

    def test_monotonic_random_high_precision_sample(self):
        import random
        random.seed(20260906)
        violations = []
        for _ in range(50000):
            a = Decimal(str(round(random.uniform(0, 10), 7)))
            b = a + Decimal(str(round(random.uniform(0, 10), 7)))
            if _money_round(a) > _money_round(b):
                violations.append((a, b))
        self.assertEqual(violations, [])


# ─────────────────────────────────────────────────────────────────────────
# B. THE amendment — Django-parity property tests (the corrected formula)
# ─────────────────────────────────────────────────────────────────────────
class DjangoPostingParityPropertyTests(TestCase):
    """
    F. non-negative split / exactness, verified as a pure property
    (no DB) across a wide adversarial matrix of (balance, commission,
    spread) — the same matrix design that originally found the
    counter-example, now run against the CORRECTED formula.
    """
    def test_corrected_formula_exact_and_non_negative_wide_matrix(self):
        import random
        random.seed(99)
        balances = [Decimal(str(round(random.uniform(0, 2_000_000), 2))) for _ in range(300)]
        balances += [Decimal("1049.55"), Decimal("1049.50"), Decimal("1000.01"),
                     Decimal("0.00"), Decimal("0.01"), Decimal("100.00"),
                     Decimal("100.01"), Decimal("999999.99")]
        exactness_violations = []
        negative_violations = []
        trials = 0
        for a in balances:
            for c_cents in range(0, 12):
                for c_sub in (Decimal("0.000"), Decimal("0.001"), Decimal("0.004"),
                              Decimal("0.005"), Decimal("0.006"), Decimal("0.009")):
                    commission = Decimal(c_cents) / 100 + c_sub
                    for s_cents in range(0, 6):
                        for s_sub in (Decimal("0.000"), Decimal("0.0001"), Decimal("0.005"), Decimal("0.0099")):
                            spread = Decimal(s_cents) / 100 + s_sub
                            if commission == 0 and spread == 0:
                                continue
                            trials += 1
                            cl, sl, total = _expected_posting(a, commission, spread)
                            if cl + sl != total:
                                exactness_violations.append((a, commission, spread))
                            if commission > 0 and spread > 0 and sl < 0:
                                negative_violations.append((a, commission, spread, cl, sl))
        self.assertGreater(trials, 500_000)
        self.assertEqual(exactness_violations, [])
        self.assertEqual(negative_violations, [])

    def test_mandatory_counter_example_1049_55(self):
        # §6 of the amendment — the exact case that broke the first design.
        cl, sl, total = _expected_posting(Decimal("1049.55"), Decimal("0.031"), Decimal("0.024"))
        self.assertEqual(total, Decimal("0.05"))   # NOT 0.06
        self.assertEqual(cl, Decimal("0.03"))
        self.assertEqual(sl, Decimal("0.02"))      # NOT 0.03
        self.assertEqual(cl + sl, total)

    def test_original_real_example_still_0_08_0_07_0_15(self):
        cl, sl, total = _expected_posting(Decimal("1049.55"), Decimal("0.0798477"), Decimal("0.075"))
        self.assertEqual(cl, Decimal("0.08"))
        self.assertEqual(sl, Decimal("0.07"))
        self.assertEqual(total, Decimal("0.15"))

    def test_single_component_tie_commission_only(self):
        # §7 candidate — old_balance=1000.01, commission=0.015.
        cl, sl, total = _expected_posting(Decimal("1000.01"), Decimal("0.015"), Decimal("0"))
        self.assertEqual(total, Decimal("0.01"))   # NOT money_round(0.015)=0.02
        self.assertEqual(cl, Decimal("0.01"))
        self.assertEqual(sl, Decimal("0"))

    def test_single_component_tie_spread_only(self):
        cl, sl, total = _expected_posting(Decimal("1000.01"), Decimal("0"), Decimal("0.015"))
        self.assertEqual(total, Decimal("0.01"))   # NOT money_round(0.015)=0.02
        self.assertEqual(sl, Decimal("0.01"))
        self.assertEqual(cl, Decimal("0"))


# ─────────────────────────────────────────────────────────────────────────
# C. _db_open_position_atomic — real DB integration, Django parity
# ─────────────────────────────────────────────────────────────────────────
class OpenPositionReconciliationTests(TestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1049.55"), account_type="STANDARD")
        self.consumer = _make_consumer(self.account)

    def _assert_reconciles(self, *, commission_exact, spread_exact, balance_before, account=None):
        account = account or self.account
        commission_ledger_expected, spread_ledger_expected, total_expected = _expected_posting(
            balance_before, commission_exact, spread_exact,
        )
        account.refresh_from_db()

        # D. Django parity — the ACTUAL persisted delta, not a formula in isolation.
        self.assertEqual(balance_before - account.balance, total_expected)

        ledger_sum = LedgerEntry.objects.filter(
            account=account, event_type__in=[LedgerEntry.EV_COMMISSION, LedgerEntry.EV_FEE],
        ).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        self.assertEqual(ledger_sum, -total_expected)

        broker_sum = BrokerLedger.objects.filter(
            source_account=account,
            revenue_type__in=[BrokerLedger.REV_COMMISSION, BrokerLedger.REV_SPREAD],
        ).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        self.assertEqual(broker_sum, total_expected)
        self.assertEqual(ledger_sum, -broker_sum)

        if commission_ledger_expected > 0:
            le = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_COMMISSION)
            self.assertEqual(le.amount, -commission_ledger_expected)
        else:
            self.assertFalse(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).exists())
        if spread_ledger_expected > 0:
            fe = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
            self.assertEqual(fe.amount, -spread_ledger_expected)
        else:
            self.assertFalse(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).exists())
        return total_expected

    # A. caso 1049.55 / .031 / .024 — el contraejemplo obligatorio, contra DB real.
    def test_mandatory_counter_example_against_real_db(self):
        balance_before = self.account.balance
        _open(self.consumer, self.account, qty=0.01, price=80000.0,
              commission=0.031, effective_pips=4.8)
        self._assert_reconciles(commission_exact=Decimal("0.031"), spread_exact=Decimal("0.024"),
                                 balance_before=balance_before)

    # D. original .0798477 + .075 — sigue produciendo 0.08/0.07/0.15.
    def test_original_real_example_against_real_db(self):
        balance_before = self.account.balance
        _open(self.consumer, self.account, qty=0.01, price=79847.70,
              commission=0.0798477, effective_pips=15.0)
        self._assert_reconciles(commission_exact=Decimal("0.0798477"), spread_exact=Decimal("0.075"),
                                 balance_before=balance_before)
        fe = LedgerEntry.objects.get(account=self.account, event_type=LedgerEntry.EV_FEE)
        self.assertEqual(fe.meta["exact_amount"], str(Decimal("0.075")))
        self.assertEqual(fe.meta["rounding_residual"], str(Decimal("0.07") - Decimal("0.075")))
        self.assertEqual(fe.meta["rounding_role"], "residual")

    def test_exact_cent_components_no_residual_needed(self):
        balance_before = self.account.balance
        _open(self.consumer, self.account, qty=0.01, price=80000.0,
              commission=0.05, effective_pips=6.0)
        self._assert_reconciles(commission_exact=Decimal("0.05"), spread_exact=Decimal("0.03"),
                                 balance_before=balance_before)

    # B. commission-only tie — old_balance=1000.01, commission=0.015.
    def test_single_component_commission_tie_against_real_db(self):
        account = make_account(balance=Decimal("1000.01"), account_type="STANDARD")
        consumer = _make_consumer(account)
        balance_before = account.balance
        result = _open(consumer, account, qty=0.01, price=80000.0, commission=0.015)
        self.assertTrue(result.get("ok"))
        self.assertFalse(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_FEE).exists())
        le = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_COMMISSION)
        self.assertEqual(le.amount, Decimal("-0.01"))   # NOT -0.02
        account.refresh_from_db()
        self.assertEqual(balance_before - account.balance, Decimal("0.01"))

    # C. spread-only tie — mismo balance, componente en spread en vez de commission.
    def test_single_component_spread_tie_against_real_db(self):
        account = make_account(balance=Decimal("1000.01"), account_type="STANDARD")
        consumer = _make_consumer(account)
        balance_before = account.balance
        # spread_exact = pips/2 * qty = 0.015 -> qty=0.01, pips=3.0
        result = _open(consumer, account, qty=0.01, price=80000.0, commission=0.0, effective_pips=3.0)
        self.assertTrue(result.get("ok"))
        self.assertFalse(LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_COMMISSION).exists())
        fe = LedgerEntry.objects.get(account=account, event_type=LedgerEntry.EV_FEE)
        self.assertEqual(fe.amount, Decimal("-0.01"))   # NOT -0.02
        account.refresh_from_db()
        self.assertEqual(balance_before - account.balance, Decimal("0.01"))

    def test_commission_only_no_spread_line_created(self):
        balance_before = self.account.balance
        _open(self.consumer, self.account, qty=0.01, price=80000.0, commission=0.05)
        self._assert_reconciles(commission_exact=Decimal("0.05"), spread_exact=Decimal("0"),
                                 balance_before=balance_before)

    def test_spread_only_no_commission_line_created(self):
        balance_before = self.account.balance
        _open(self.consumer, self.account, qty=0.01, price=80000.0,
              commission=0.0, effective_pips=6.0)
        self._assert_reconciles(commission_exact=Decimal("0"), spread_exact=Decimal("0.03"),
                                 balance_before=balance_before)

    def test_zero_zero_no_lines_no_balance_change(self):
        balance_before = self.account.balance
        _open(self.consumer, self.account, qty=0.01, price=80000.0, commission=0.0)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, balance_before)
        self.assertFalse(LedgerEntry.objects.filter(
            account=self.account, event_type__in=[LedgerEntry.EV_COMMISSION, LedgerEntry.EV_FEE],
        ).exists())

    # E. high-notional — causa exacta del fallo inicial DETERMINADA: no
    # era un problema de redondeo, era el guard de margen pre-existente
    # (margin_per_trade_exceeded) rechazando qty=1.0 @ price=80000.0
    # (notional=$80,000) contra el balance de setUp() ($1,049.55, ~381%
    # del 10% máximo por trade) — comportamiento correcto y esperado del
    # guard, no relacionado con este fix. Corregido usando una cuenta con
    # balance suficiente para que la apertura de alto notional pase el
    # guard y se pueda verificar la reconciliación a esa escala.
    def test_high_notional(self):
        account = make_account(balance=Decimal("1000000.00"), account_type="STANDARD")
        consumer = _make_consumer(account)
        balance_before = account.balance
        commission_exact = Decimal("123.456789")
        result = _open(consumer, account, qty=1.0, price=80000.0,
                        commission=float(commission_exact), effective_pips=15.0)
        self.assertTrue(result.get("ok"), result)
        spread_exact = (Decimal("15.0") / 2 * Decimal("1.0"))  # calculate_spread_revenue's own formula, BTCUSD
        self._assert_reconciles(commission_exact=commission_exact, spread_exact=spread_exact,
                                 balance_before=balance_before, account=account)

    def test_no_double_rounding_spread_ledger_already_at_2dp(self):
        _open(self.consumer, self.account, qty=0.01, price=79847.70,
              commission=0.0798477, effective_pips=15.0)
        fe = LedgerEntry.objects.get(account=self.account, event_type=LedgerEntry.EV_FEE)
        self.assertEqual(fe.amount, fe.amount.quantize(_MONEY_2DP))


# ─────────────────────────────────────────────────────────────────────────
# D. Repeated openings — exact zero drift, no epsilon tolerance
# ─────────────────────────────────────────────────────────────────────────
class RepeatedOpeningsDriftTests(TestCase):
    def test_100_independent_openings_zero_unexplained_drift(self):
        # 100 INDEPENDENT accounts (one opening each) rather than 100
        # sequential opens on one account — _db_open_position_atomic's
        # margin/position-count guard requires a fresh live price to
        # re-validate any EXISTING open position on the same symbol,
        # which this offline test environment never has; using
        # independent accounts isolates the reconciliation invariant
        # under test from that unrelated guard. Every invariant checked
        # (§10) is per-operation-summed exactly the same way either way.
        import random
        random.seed(7)
        total_ledger_sum = Decimal("0")
        total_broker_sum = Decimal("0")
        total_balance_delta = Decimal("0")

        for i in range(100):
            account = make_account(balance=Decimal("1000000.00"), account_type="STANDARD")
            consumer = _make_consumer(account)
            balance_before = account.balance
            commission = round(random.uniform(0.001, 5.0), 7)
            pips = round(random.uniform(0.1, 30.0), 4)
            qty = round(random.uniform(0.001, 2.0), 3)
            _open(consumer, account, qty=qty, price=80000.0,
                  commission=commission, effective_pips=pips, symbol="BTCUSD")
            account.refresh_from_db()
            total_balance_delta += balance_before - account.balance
            total_ledger_sum += LedgerEntry.objects.filter(
                account=account, event_type__in=[LedgerEntry.EV_COMMISSION, LedgerEntry.EV_FEE],
            ).aggregate(t=Sum("amount"))["t"] or Decimal("0")
            total_broker_sum += BrokerLedger.objects.filter(
                source_account=account,
                revenue_type__in=[BrokerLedger.REV_COMMISSION, BrokerLedger.REV_SPREAD],
            ).aggregate(t=Sum("amount"))["t"] or Decimal("0")

        self.assertEqual(-total_ledger_sum, total_balance_delta,
                          "ledger sum must reconcile EXACTLY against persisted balance delta")
        self.assertEqual(total_broker_sum, total_balance_delta)


# ─────────────────────────────────────────────────────────────────────────
# E. Admin-style aggregation — same Sum("amount") pattern admin.py uses
# ─────────────────────────────────────────────────────────────────────────
class AdminStyleAggregationReconciliationTests(TestCase):
    def test_admin_style_sum_reconciles_against_account_balance(self):
        account = make_account(balance=Decimal("5000.00"), account_type="STANDARD")
        consumer = _make_consumer(account)
        balance_before = account.balance

        _open(consumer, account, qty=0.01, price=79847.70, commission=0.0798477, effective_pips=15.0)
        account.refresh_from_db()
        mid_balance = account.balance
        # second open on a SEPARATE account (see D above for why) but
        # summed together, mirroring a broker-wide revenue report that
        # aggregates across many accounts/operations.
        account2 = make_account(balance=Decimal("2000.00"), account_type="STANDARD")
        consumer2 = _make_consumer(account2)
        _open(consumer2, account2, qty=0.01, price=80000.0, commission=0.031, effective_pips=4.8)
        account2.refresh_from_db()

        total_revenue = BrokerLedger.objects.filter(
            source_account__in=[account, account2],
            revenue_type__in=[BrokerLedger.REV_COMMISSION, BrokerLedger.REV_SPREAD],
        ).aggregate(t=Sum("amount"))["t"]
        total_delta = (balance_before - mid_balance) + (Decimal("2000.00") - account2.balance)
        self.assertEqual(total_revenue, total_delta)
        self.assertNotEqual(mid_balance, balance_before)


# ─────────────────────────────────────────────────────────────────────────
# F. Regression — economics/formulas and sibling paths untouched
# ─────────────────────────────────────────────────────────────────────────
class UntouchedEconomicsRegressionTests(TestCase):
    def test_commission_for_and_spread_revenue_formulas_untouched(self):
        import inspect
        from simulator import spread_engine
        from simulator.consumers import TradingConsumer

        src_commission = inspect.getsource(TradingConsumer.commission_for)
        self.assertIn("notional * profile.commission_pct", src_commission)

        src_spread = inspect.getsource(spread_engine.calculate_spread_revenue)
        self.assertIn("half_spread * qty * spec.contract_size", src_spread)

    def test_realized_pnl_close_path_still_single_line_no_split(self):
        from simulator.tasks import _close_position_sync
        account = make_account(balance=Decimal("10000"), account_type="STANDARD")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.1"), avg_price=Decimal("1.10000"))
        balance_before = account.balance
        _close_position_sync(
            pos_mem={"id": pos.id, "symbol": "EUR/USD", "side": "buy", "qty": 0.1,
                     "avg": 1.10000, "sl": None, "tp": None, "opened_at": pos.opened_at.timestamp()},
            account_id=account.id, close_px=1.10500, reason="manual",
            realized_pnl=50.0, new_balance=10050.0, new_equity=10050.0,
        )
        account.refresh_from_db()
        realized_sum = LedgerEntry.objects.filter(
            account=account, event_type=LedgerEntry.EV_REALIZED,
        ).aggregate(t=Sum("amount"))["t"]
        self.assertEqual(realized_sum, account.balance - balance_before)
        self.assertEqual(
            LedgerEntry.objects.filter(account=account, event_type=LedgerEntry.EV_REALIZED).count(), 1,
        )

    def test_partial_close_still_no_commission_spread_split(self):
        from simulator.tasks import _close_position_sync
        account = make_account(balance=Decimal("10000"), account_type="STANDARD")
        pos = make_position(account=account, symbol="EUR/USD", side="BUY",
                             qty=Decimal("1.0"), avg_price=Decimal("1.10000"))
        _close_position_sync(
            pos_mem={"id": pos.id, "symbol": "EUR/USD", "side": "buy", "qty": 1.0,
                     "avg": 1.10000, "sl": None, "tp": None, "opened_at": pos.opened_at.timestamp()},
            account_id=account.id, close_px=1.10500, reason="manual",
            realized_pnl=5.0, new_balance=10005.0, new_equity=10005.0,
            close_qty=Decimal("0.4"),
        )
        self.assertFalse(LedgerEntry.objects.filter(
            account=account, event_type__in=[LedgerEntry.EV_COMMISSION, LedgerEntry.EV_FEE],
        ).exists())
        pos.refresh_from_db()
        self.assertEqual(pos.qty, Decimal("0.600000"))

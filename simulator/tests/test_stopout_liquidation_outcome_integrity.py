# simulator/tests/test_stopout_liquidation_outcome_integrity.py
"""
STOPOUT LIQUIDATION OUTCOME INTEGRITY.

Closes the bug audited in the STOPOUT EMPTY-CLOSE NOTIFICATION
INTEGRITY design lock: _do_retail_liquidation()/_do_stopout() used to
report a completed Stop-Out (account:stopout / account:suspended,
TradingAccount.status="Suspendido", zeroed account:update) even when
ZERO positions actually closed (no financial quote available) or only
SOME did — most severely, _do_stopout() (CHALLENGE/FUNDED, the DD
engine) unconditionally suspended the account and wrote a real
LedgerEntry(EV_ADJUST, reason="stopout") regardless of outcome, and
because _recalc_account_and_push()'s own retry gate is
`status=="Activo" and self._positions`, that incorrect suspension
permanently blocked any further retry — the position never closes,
ever, and the account is unusable.

Three outcomes, now handled distinctly (both functions, same pattern):
  EMPTY   (closed_count==0, remaining>0): no account:stopout, no
          suspension, no account:suspended, no LedgerEntry(EV_ADJUST).
          account:update recalculated truthfully from whatever remains
          (self._unrealized_pnl_total()/self._margin_used_total() — the
          same pure helpers the per-tick path already uses, never
          hardcoded to 0.0). Account stays Activo — next real-priced
          tick retries naturally (already-existing per-tick mechanism,
          untouched).
  PARTIAL (closed_count>0, remaining>0): the real closes stand.
          account:stopout IS sent (something real happened) with
          additive fields partial=True/closed_count/remaining_count —
          existing consumers reading only reason/stopout_level/balance
          are unaffected. DD: still no suspension yet (only once
          remaining==0). account:update reflects the real remaining
          exposure, not zero.
  FULL    (closed_count>0, remaining==0): unchanged from prior
          behavior — RETAIL sends account:stopout (partial=False,
          remaining_count=0); DD suspends and sends account:suspended,
          exactly as before.

Reuses the established test_o6c1w_price_integrity_gate.py harness
(_bare_consumer/_seed_raw/_clear_symbol) rather than duplicating it.
"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase

from market_data.contracts import OrderPolicy
from simulator.models import LedgerEntry, Position, TradingAccount

from .factories import make_account, make_position
from .test_o6c1w_price_integrity_gate import _bare_consumer, _clear_symbol, _seed_raw
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run


def _pos_entry(pos, sl=None, tp=None):
    return {
        "id": pos.pk, "symbol": pos.symbol, "side": pos.side.lower(),
        "qty": float(pos.qty), "avg": float(pos.avg_price),
        "sl": sl, "tp": tp, "opened_at": time.time(),
    }


def _sent(panel, msg_type):
    return [c.args[0] for c in panel.send_json.call_args_list if c.args[0].get("type") == msg_type]


# ── EMPTY — RETAIL ────────────────────────────────────────────────────────

class EmptyRetailStopoutTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def test_no_price_for_either_position_no_close_no_fake_event(self):
        pos1 = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        pos2 = make_position(self.account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.25000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(pos1), _pos_entry(pos2)]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        _seed_raw("GBP/USD", 1.25000, 1.25020, source="sim")

        _run(panel._do_retail_liquidation())

        self.assertTrue(Position.objects.filter(pk=pos1.pk).exists())
        self.assertTrue(Position.objects.filter(pk=pos2.pk).exists())
        self.assertEqual(len(panel._positions), 2)
        self.assertEqual(_sent(panel, "account:stopout"), [])  # no fake liquidation event
        self.assertEqual(panel.account["status"], "Activo")

        upd = _sent(panel, "account:update")
        self.assertEqual(len(upd), 1)
        # margin_used must reflect the 2 real remaining positions, not 0 —
        # EUR/USD: 1.10000*0.01*100000/50=22.0; GBP/USD: 1.25*0.01*100000/50=25.0
        self.assertAlmostEqual(upd[0]["margin_used"], 47.0, places=2)
        self.assertNotEqual(upd[0]["margin_used"], 0.0)

    def test_retry_succeeds_once_real_price_available(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(pos)]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        _run(panel._do_retail_liquidation())
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(panel.account["status"], "Activo")  # still Activo -> retry possible

        # Next real tick: valid source now available.
        _seed_raw("EUR/USD", 1.05000, 1.05020, source="finnhub")
        _run(panel._do_retail_liquidation())
        self.assertFalse(Position.objects.filter(pk=pos.pk).exists())
        self.assertEqual(len(panel._positions), 0)
        stopout_msgs = _sent(panel, "account:stopout")
        self.assertEqual(len(stopout_msgs), 1)
        self.assertFalse(stopout_msgs[0]["partial"])
        self.assertEqual(stopout_msgs[0]["remaining_count"], 0)


# ── EMPTY — DD (CHALLENGE/FUNDED) — the severe bug ───────────────────────

class EmptyDdStopoutTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_no_price_never_suspends_never_fake_ledger(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(pos)]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")

        _run(panel._do_stopout())

        # The exact bug: must NOT suspend, NOT persist a fake stopout ledger.
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Activo")
        self.assertEqual(panel.account["status"], "Activo")
        self.assertEqual(
            LedgerEntry.objects.filter(account=self.account, event_type=LedgerEntry.EV_ADJUST, meta__reason="stopout").count(),
            0,
        )
        self.assertEqual(_sent(panel, "account:suspended"), [])
        self.assertTrue(Position.objects.filter(pk=pos.pk).exists())

        upd = _sent(panel, "account:update")
        self.assertEqual(upd[0]["status"], "Activo")
        self.assertAlmostEqual(upd[0]["margin_used"], 22.0, places=2)  # real, not 0

    def test_retry_then_full_suspends_as_before(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(pos)]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        _run(panel._do_stopout())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Activo")  # not suspended yet

        _seed_raw("EUR/USD", 1.05000, 1.05020, source="finnhub")
        _run(panel._do_stopout())

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Suspendido")
        self.assertEqual(panel.account["status"], "Suspendido")
        self.assertEqual(
            LedgerEntry.objects.filter(account=self.account, event_type=LedgerEntry.EV_ADJUST, meta__reason="stopout").count(),
            1,
        )
        self.assertEqual(len(_sent(panel, "account:suspended")), 1)


# ── PARTIAL — RETAIL ──────────────────────────────────────────────────────

class PartialRetailStopoutTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def test_one_closes_one_remains_truthful_partial_event(self):
        closes = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        remains = make_position(self.account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.25000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(closes), _pos_entry(remains)]
        _seed_raw("EUR/USD", 1.09000, 1.09020, source="finnhub")  # valid — will close
        _seed_raw("GBP/USD", 1.25000, 1.25020, source="sim")       # untrusted — stays open

        _run(panel._do_retail_liquidation())

        self.assertFalse(Position.objects.filter(pk=closes.pk).exists())
        self.assertTrue(Position.objects.filter(pk=remains.pk).exists())
        self.assertEqual(len(panel._positions), 1)
        self.assertEqual(panel._positions[0]["symbol"], "GBP/USD")

        stopout_msgs = _sent(panel, "account:stopout")
        self.assertEqual(len(stopout_msgs), 1)
        self.assertTrue(stopout_msgs[0]["partial"])
        self.assertEqual(stopout_msgs[0]["closed_count"], 1)
        self.assertEqual(stopout_msgs[0]["remaining_count"], 1)

        upd = _sent(panel, "account:update")
        # GBP/USD alone: 1.25*0.01*100000/50 = 25.0 — not 0.
        self.assertAlmostEqual(upd[0]["margin_used"], 25.0, places=2)
        self.assertEqual(upd[0]["status"], "Activo")

    def test_remaining_position_can_close_on_next_retry(self):
        closes = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        remains = make_position(self.account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.25000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(closes), _pos_entry(remains)]
        _seed_raw("EUR/USD", 1.09000, 1.09020, source="finnhub")
        _seed_raw("GBP/USD", 1.25000, 1.25020, source="sim")
        _run(panel._do_retail_liquidation())
        self.assertEqual(len(panel._positions), 1)

        _seed_raw("GBP/USD", 1.20000, 1.20020, source="finnhub")
        _run(panel._do_retail_liquidation())

        self.assertFalse(Position.objects.filter(pk=remains.pk).exists())
        self.assertEqual(len(panel._positions), 0)


# ── PARTIAL — DD ──────────────────────────────────────────────────────────

class PartialDdStopoutTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def test_partial_close_does_not_suspend_yet(self):
        closes = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        remains = make_position(self.account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.25000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(closes), _pos_entry(remains)]
        _seed_raw("EUR/USD", 1.09000, 1.09020, source="finnhub")
        _seed_raw("GBP/USD", 1.25000, 1.25020, source="sim")

        _run(panel._do_stopout())

        self.assertFalse(Position.objects.filter(pk=closes.pk).exists())
        self.assertTrue(Position.objects.filter(pk=remains.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Activo")
        self.assertEqual(
            LedgerEntry.objects.filter(account=self.account, event_type=LedgerEntry.EV_ADJUST, meta__reason="stopout").count(),
            0,
        )
        self.assertEqual(_sent(panel, "account:suspended"), [])

    def test_second_attempt_closes_remaining_and_now_suspends(self):
        closes = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        remains = make_position(self.account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.25000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(closes), _pos_entry(remains)]
        _seed_raw("EUR/USD", 1.09000, 1.09020, source="finnhub")
        _seed_raw("GBP/USD", 1.25000, 1.25020, source="sim")
        _run(panel._do_stopout())

        _seed_raw("GBP/USD", 1.20000, 1.20020, source="finnhub")
        _run(panel._do_stopout())

        self.assertFalse(Position.objects.filter(pk=remains.pk).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Suspendido")
        self.assertEqual(len(_sent(panel, "account:suspended")), 1)


# ── FULL — regression, both engines ──────────────────────────────────────

class FullRetailStopoutRegressionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")
        _clear_symbol("GBP/USD")

    def test_all_close_existing_behavior_preserved(self):
        p1 = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        p2 = make_position(self.account, symbol="GBP/USD", qty=Decimal("0.01"), avg_price=Decimal("1.25000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(p1), _pos_entry(p2)]
        _seed_raw("EUR/USD", 1.09000, 1.09020, source="finnhub")
        _seed_raw("GBP/USD", 1.20000, 1.20020, source="finnhub")

        _run(panel._do_retail_liquidation())

        self.assertEqual(Position.objects.filter(account=self.account).count(), 0)
        self.assertEqual(len(panel._positions), 0)
        stopout_msgs = _sent(panel, "account:stopout")
        self.assertEqual(len(stopout_msgs), 1)
        self.assertFalse(stopout_msgs[0]["partial"])
        self.assertEqual(stopout_msgs[0]["remaining_count"], 0)
        upd = _sent(panel, "account:update")
        self.assertEqual(upd[0]["margin_used"], 0.0)
        self.assertEqual(upd[0]["pnl_unreal"], 0.0)
        self.assertEqual(upd[0]["status"], "Activo")


class FullDdStopoutRegressionTests(TransactionTestCase):
    def setUp(self):
        self.account = make_account(balance=Decimal("1000"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def test_all_close_still_suspends_as_before(self):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(pos)]
        _seed_raw("EUR/USD", 1.05000, 1.05020, source="finnhub")

        _run(panel._do_stopout())

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Suspendido")
        self.assertEqual(
            LedgerEntry.objects.filter(account=self.account, event_type=LedgerEntry.EV_ADJUST, meta__reason="stopout").count(),
            1,
        )
        self.assertEqual(len(_sent(panel, "account:suspended")), 1)
        upd = _sent(panel, "account:update")
        self.assertEqual(upd[0]["margin_used"], 0.0)
        self.assertEqual(upd[0]["status"], "Suspendido")


# ── Frontend contract — textual, same pattern used elsewhere in the repo ──

class FrontendStopoutWordingContractTests(SimpleTestCase):
    def _template_source(self):
        from django.template.loader import get_template
        path = get_template("simulator/dashboard.html").origin.name
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_partial_wording_present(self):
        src = self._template_source()
        self.assertIn("msg.partial", src)
        self.assertIn("STOP-OUT PARCIAL", src)

    def test_full_wording_unchanged(self):
        src = self._template_source()
        self.assertIn("STOP-OUT — Posiciones liquidadas", src)


# ── Residual risk investigation (report only, per design lock §22) ──────

class NewOrderDuringPendingLiquidationInvestigationTests(TransactionTestCase):
    """Investigates (does not fix) whether a new order can open while an
    EMPTY/PARTIAL stop-out attempt leaves the account Activo with a
    still-below-threshold equity. Per the design lock: report, don't
    silently expand scope. No new gate introduced here — these tests
    assert the ACTUAL observed outcome of the existing guards, so a
    future change to those guards will fail this file loudly instead of
    silently changing the answer to "can a new order slip through?"."""

    def setUp(self):
        self.account = make_account(balance=Decimal("100"), account_type="CHALLENGE")
        _clear_symbol("EUR/USD")

    def tearDown(self):
        _clear_symbol("EUR/USD")

    def _order_new_after_empty_attempt(self, qty):
        pos = make_position(self.account, symbol="EUR/USD", qty=Decimal("0.01"), avg_price=Decimal("1.10000"))
        panel = _bare_consumer(self.account.pk)
        panel.account["status"] = "Activo"
        panel._positions = [_pos_entry(pos)]
        _seed_raw("EUR/USD", 1.10000, 1.10020, source="sim")
        _run(panel._do_stopout())  # EMPTY — account stays Activo
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "Activo")

        order_panel = _consumer(self.account.pk)
        with patch(
            "market_data.sessions.service.evaluate_market_session_for_symbol",
            return_value=type("S", (), {"order_policy": OrderPolicy.OPEN_NORMAL})(),
        ):
            with order_panel._feed._lock:
                order_panel._feed._bids["EUR/USD"] = 1.10000
                order_panel._feed._asks["EUR/USD"] = 1.10020
                order_panel._feed._prices["EUR/USD"] = 1.10010
                order_panel._feed._price_ts["EUR/USD"] = time.time()
                order_panel._feed._price_source["EUR/USD"] = "finnhub"
            _run(order_panel._order_new({"symbol": "EUR/USD", "side": "buy", "qty": qty}))
        return order_panel

    def test_normal_sized_new_order_blocked_by_existing_margin_guard(self):
        """qty=0.01 on a $100-equity account requesting 22% of equity as
        margin (>10% per-trade cap) — the EXISTING margin_per_trade_
        exceeded guard rejects it. Confirms no new gate is required for
        this shape of order."""
        order_panel = self._order_new_after_empty_attempt(qty=0.01)
        err = _first_error(order_panel)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "margin_per_trade_exceeded")
        self.assertEqual(Position.objects.filter(account=self.account, symbol="EUR/USD").count(), 1)  # only the original

    def test_below_min_lot_new_order_blocked_by_existing_min_qty_guard(self):
        """RESIDUAL RISK PROBE, reported per design lock §22 — tried the
        smallest possible qty to look for a gap between the min-lot
        guard and the per-trade margin cap. EUR/USD's min_lot==lot_step
        ==0.01 (symbol_specs.py) — there is no valid lot size smaller
        than the one already rejected by margin_per_trade_exceeded in
        test_normal_sized_new_order_blocked_by_existing_margin_guard
        above, so this qty is instead rejected by min_qty_violation.
        Empirically, for this realistic account shape (equity critically
        low precisely because Stop-Out triggered), the two existing
        guards leave NO valid order size — no new gate demonstrated as
        necessary. Not proven for every possible equity/leverage/symbol
        combination — see STOPOUT LIQUIDATION-PENDING ORDER GATE in the
        final report for the residual caveat."""
        order_panel = self._order_new_after_empty_attempt(qty=0.001)
        err = _first_error(order_panel)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "min_qty_violation")
        self.assertEqual(Position.objects.filter(account=self.account, symbol="EUR/USD").count(), 1)

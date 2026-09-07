"""
simulator/tests/test_fix_sltp_update_validation01_order_update_guard.py
FIX-SLTP-UPDATE-VALIDATION-01.

GOLDEN-AUTO-CLOSE-UNEXPECTED-01 found that _order_new()/
_order_pending_new()/_trigger_pending_order_core() all reject a
wrong-side SL/TP at position-creation time via _validate_sl_tp(), but
_order_update() — the one path that lets a client attach/change SL/TP
on an ALREADY-OPEN position (used by both the position-sheet editor and
the chart's draggable SL/TP lines, which both send the exact same
{"action":"order:update", ...} WS message and are dispatched to this
one handler — see TradingConsumer._handle_action's single
`elif act == "order:update": await self._order_update(data)` branch,
so there is only one backend to cover both UI affordances by
construction) — never validated at all. A SELL position could receive
TP above entry / SL below entry (the BUY convention, backwards for a
SELL), which _check_tp_sl()'s (correct, untouched) side-aware trigger
condition then found already satisfied on the very next tick, with no
real price movement.

This file proves _order_update() now reuses the SAME _validate_sl_tp()
every creation path already trusts (no second, independently-invented
validation), with _feed_close_price() as the reference price — the
same side-aware price authority _check_tp_sl()/_unrealized_pnl_total()
already use for every SL/TP/close decision on an open position.

Uses TransactionTestCase, not TestCase: _order_update awaits
_db_mirror_update_sl_tp, a @database_sync_to_async method that spins up
a separate thread — TestCase's uncommitted per-test transaction is
invisible to (and deadlocks against) that thread on SQLite, same
reasoning as test_order_ticket_sl_tp_validation.py's
OrderNewIntegrationTests and test_o6c1aa_unified_raw_execution_
spread_fee.py.
"""
import time
from decimal import Decimal

from django.test import TransactionTestCase

from market_data.feeds import get_feed_manager
from simulator.consumers import TradingConsumer
from simulator.models import Position

from .factories import make_account, make_position
from .test_order_ticket_sl_tp_validation import _consumer, _first_error, _run


def _seed_raw(symbol: str, bid: float, ask: float):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids[symbol] = bid
        feed._asks[symbol] = ask
        feed._prices[symbol] = (bid + ask) / 2.0
        feed._price_ts[symbol] = time.time()


def _clear_symbol(symbol: str):
    feed = get_feed_manager()
    with feed._lock:
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._prices.pop(symbol, None)
        feed._price_ts.pop(symbol, None)
        feed._price_source.pop(symbol, None)
        feed._last_valid_quote.pop(symbol, None)


class _OrderUpdateBase(TransactionTestCase):
    """Shared fixture: one real open Position (DB) mirrored into the
    consumer's in-memory self._positions, exactly the shape
    _order_update() expects to find. side is stored uppercase in DB
    (Position.side, matches production) and lowercase in the in-memory
    dict (matches every other in-memory position dict in this file —
    see _order_update()'s own `side = p["side"]` == "buy"/"sell")."""

    SYMBOL = "EUR/USD"

    def setUp(self):
        self.account = make_account(balance=Decimal("10000.00"))
        _clear_symbol(self.SYMBOL)

    def tearDown(self):
        _clear_symbol(self.SYMBOL)

    def _make(self, side="BUY", avg_price="1.16227", sl=None, tp=None,
              bid=None, ask=None, no_quote=False):
        """bid/ask are seeded AFTER _consumer() on purpose — _consumer()
        (shared helper, ~10 other test files) unconditionally reseeds
        EUR/USD to 1.1000/1.1000 as part of its own setup, so seeding
        the desired quote before calling it would be silently clobbered.
        no_quote=True clears the symbol after _consumer() instead, for
        tests that need NO valid quote at all."""
        pos = make_position(
            self.account, symbol=self.SYMBOL, side=side,
            qty=Decimal("0.01"), avg_price=Decimal(avg_price),
            sl=sl, tp=tp,
        )
        consumer = _consumer(self.account.pk)
        if no_quote:
            _clear_symbol(self.SYMBOL)
        elif bid is not None and ask is not None:
            _seed_raw(self.SYMBOL, bid, ask)
        consumer._positions = [{
            "id": pos.id, "symbol": self.SYMBOL, "side": side.lower(),
            "qty": 0.01, "avg": float(avg_price),
            "sl": float(sl) if sl is not None else None,
            "tp": float(tp) if tp is not None else None,
        }]
        return consumer, pos

    def _update(self, consumer, pos, **kwargs):
        payload = {"action": "order:update", "id": str(pos.id), "symbol": self.SYMBOL}
        payload.update(kwargs)
        _run(consumer._order_update(payload))

    def _refresh(self, pos):
        pos.refresh_from_db()
        return pos


# ─────────────────────────────────────────────────────────────────────
# 1-8 — BUY/SELL x SL/TP x valid/invalid
# ─────────────────────────────────────────────────────────────────────
class BuySellDirectionTests(_OrderUpdateBase):

    def test_1_buy_update_sl_valid_accepts(self):
        # BUY, ref price (bid) = 1.16227 -> SL below is valid
        consumer, pos = self._make(side="BUY", avg_price="1.16260", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16200)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.16200"))
        self.assertEqual(consumer._positions[0]["sl"], 1.16200)

    def test_2_buy_update_sl_invalid_rejects(self):
        # BUY, ref price (bid) = 1.16227 -> SL ABOVE is invalid (wrong side)
        consumer, pos = self._make(side="BUY", avg_price="1.16227", sl=None, tp=None, bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16300)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_sl_direction")
        self._refresh(pos)
        self.assertIsNone(pos.sl)
        self.assertIsNone(consumer._positions[0]["sl"])

    def test_3_buy_update_tp_valid_accepts(self):
        consumer, pos = self._make(side="BUY", avg_price="1.16200", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, tp=1.16300)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.tp, Decimal("1.163"))

    def test_4_buy_update_tp_invalid_rejects(self):
        # BUY, ref price (bid) = 1.16227 -> TP BELOW is invalid (wrong side)
        consumer, pos = self._make(side="BUY", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, tp=1.16100)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_tp_direction")
        self._refresh(pos)
        self.assertIsNone(pos.tp)

    def test_5_sell_update_sl_valid_accepts(self):
        # SELL, ref price (ask) = 1.16229 -> SL ABOVE is valid
        consumer, pos = self._make(side="SELL", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16300)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.163"))

    def test_6_sell_update_sl_invalid_rejects(self):
        # Real audit case (trade 304 pattern): SL below entry — wrong side for SELL
        consumer, pos = self._make(side="SELL", avg_price="1.16226", bid=1.16226, ask=1.16228)
        self._update(consumer, pos, sl=1.16142)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_sl_direction")
        self._refresh(pos)
        self.assertIsNone(pos.sl)

    def test_7_sell_update_tp_valid_accepts(self):
        # SELL, ref price (ask) = 1.16229 -> TP BELOW is valid
        consumer, pos = self._make(side="SELL", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, tp=1.16150)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.tp, Decimal("1.1615"))

    def test_8_sell_update_tp_invalid_rejects(self):
        # Real audit case (trade 308 pattern): TP above entry — wrong side for SELL
        consumer, pos = self._make(side="SELL", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, tp=1.16372)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_tp_direction")
        self._refresh(pos)
        self.assertIsNone(pos.tp)


# ─────────────────────────────────────────────────────────────────────
# 9-16 — partial updates, clearing, atomicity, reject leaves state intact
# ─────────────────────────────────────────────────────────────────────
class PartialUpdateAndAtomicityTests(_OrderUpdateBase):

    def test_9_modify_only_sl_leaves_tp_untouched(self):
        consumer, pos = self._make(side="BUY", avg_price="1.16227", tp=Decimal("1.16400"), bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16100)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.161"))
        self.assertEqual(pos.tp, Decimal("1.16400"))

    def test_10_modify_only_tp_leaves_sl_untouched(self):
        consumer, pos = self._make(side="BUY", avg_price="1.16227", sl=Decimal("1.16000"), bid=1.16227, ask=1.16229)
        self._update(consumer, pos, tp=1.16400)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.16000"))
        self.assertEqual(pos.tp, Decimal("1.164"))

    def test_11_send_sl_null_is_a_documented_noop_not_a_clear(self):
        """PRE-EXISTING semantics (not introduced or changed by this fix):
        _order_update only ever does `if sl is not None: ...` — sending
        sl=None (JSON null) is indistinguishable from omitting the key,
        so it is a no-op, not a "clear SL". This test locks that exact
        pre-existing behavior in place (still true after adding
        validation) rather than claiming a clear feature that was never
        implemented — see the accompanying report's deviation note."""
        consumer, pos = self._make(side="BUY", avg_price="1.16227", sl=Decimal("1.16000"), bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=None)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.16000"))  # unchanged, not cleared

    def test_12_send_tp_null_is_a_documented_noop_not_a_clear(self):
        consumer, pos = self._make(side="BUY", avg_price="1.16227", tp=Decimal("1.16500"), bid=1.16227, ask=1.16229)
        self._update(consumer, pos, tp=None)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.tp, Decimal("1.16500"))  # unchanged, not cleared

    def test_13_modify_both_valid_applies_both(self):
        consumer, pos = self._make(side="BUY", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16100, tp=1.16400)
        self.assertIsNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.161"))
        self.assertEqual(pos.tp, Decimal("1.164"))

    def test_14_one_valid_one_invalid_persists_neither(self):
        # BUY: sl=1.16100 (valid, below ref) but tp=1.16100 (invalid, below ref)
        consumer, pos = self._make(side="BUY", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16100, tp=1.16100)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_tp_direction")
        self._refresh(pos)
        self.assertIsNone(pos.sl)   # the valid SL was NOT persisted either
        self.assertIsNone(pos.tp)
        self.assertIsNone(consumer._positions[0]["sl"])
        self.assertIsNone(consumer._positions[0]["tp"])

    def test_15_position_stays_intact_after_reject(self):
        consumer, pos = self._make(
            side="SELL", avg_price="1.16227",
            sl=Decimal("1.16300"), tp=Decimal("1.16100"),
            bid=1.16227, ask=1.16229,
        )
        self._update(consumer, pos, tp=1.16372)  # wrong side for SELL
        self.assertIsNotNone(_first_error(consumer))
        self._refresh(pos)
        self.assertEqual(pos.sl, Decimal("1.16300"))
        self.assertEqual(pos.tp, Decimal("1.16100"))
        self.assertEqual(consumer._positions[0]["sl"], 1.16300)
        self.assertEqual(consumer._positions[0]["tp"], 1.16100)

    def test_16_db_mirror_unchanged_after_reject(self):
        consumer, pos = self._make(side="SELL", avg_price="1.16227", sl=None, tp=None, bid=1.16227, ask=1.16229)
        before = Position.objects.get(id=pos.id)
        self._update(consumer, pos, sl=1.16142)  # wrong side for SELL
        self.assertIsNotNone(_first_error(consumer))
        after = Position.objects.get(id=pos.id)
        self.assertEqual(before.sl, after.sl)
        self.assertEqual(before.tp, after.tp)
        self.assertIsNone(after.sl)


# ─────────────────────────────────────────────────────────────────────
# 18 — a valid update must not itself trigger an immediate close
# ─────────────────────────────────────────────────────────────────────
class NoImmediateAutoCloseAfterValidUpdateTests(_OrderUpdateBase):

    def test_18_valid_update_then_tick_does_not_close(self):
        """The bug was: a wrong-side SL/TP made _check_tp_sl()'s
        condition trivially true on the next tick. A validated update
        (correct side, real distance from the reference price) must not
        reproduce that — the next tick at the SAME price must leave the
        position open."""
        consumer, pos = self._make(side="SELL", avg_price="1.16227", bid=1.16227, ask=1.16229)
        self._update(consumer, pos, sl=1.16400, tp=1.15900)
        self.assertIsNone(_first_error(consumer))

        _run(consumer._check_tp_sl(self.SYMBOL, bid=1.16227, ask=1.16229))
        self.assertEqual(len(consumer._positions), 1)
        self.assertEqual(consumer._positions[0]["id"], pos.id)
        pos.refresh_from_db()
        self.assertEqual(pos.sl, Decimal("1.164"))
        self.assertEqual(pos.tp, Decimal("1.159"))


# ─────────────────────────────────────────────────────────────────────
# 19-22 — real cases from GOLDEN-AUTO-CLOSE-UNEXPECTED-01
# ─────────────────────────────────────────────────────────────────────
class RealAuditCaseTests(_OrderUpdateBase):

    def test_19_sell_entry_162270_tp_163720_rejects(self):
        """Trade 308's exact pattern: SELL, entry~1.16227, TP=1.163720
        (14.5 pips ABOVE entry) — the audit found _check_tp_sl()'s
        ask<=tp condition was already true at open. Must now be rejected
        before it ever reaches a Position."""
        consumer, pos = self._make(side="SELL", avg_price="1.162270", bid=1.16227, ask=1.16233)
        self._update(consumer, pos, tp=1.163720)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_tp_direction")
        pos.refresh_from_db()
        self.assertIsNone(pos.tp)

    def test_20_sell_entry_162260_sl_161420_rejects(self):
        """Trade 306's exact pattern: SELL, entry~1.16226, SL=1.161420
        (8.4 pips BELOW entry) — wrong side for a SELL SL."""
        consumer, pos = self._make(side="SELL", avg_price="1.162260", bid=1.16226, ask=1.16238)
        self._update(consumer, pos, sl=1.161420)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "invalid_sl_direction")
        pos.refresh_from_db()
        self.assertIsNone(pos.sl)

    def test_21_sell_entry_162270_tp_below_correct_side_accepts(self):
        consumer, pos = self._make(side="SELL", avg_price="1.162270", bid=1.16227, ask=1.16233)
        self._update(consumer, pos, tp=1.161500)  # correctly below ref (ask)
        self.assertIsNone(_first_error(consumer))
        pos.refresh_from_db()
        self.assertEqual(pos.tp, Decimal("1.1615"))

    def test_22_sell_entry_162270_sl_above_correct_side_accepts(self):
        consumer, pos = self._make(side="SELL", avg_price="1.162270", bid=1.16227, ask=1.16233)
        self._update(consumer, pos, sl=1.163000)  # correctly above ref (ask)
        self.assertIsNone(_first_error(consumer))
        pos.refresh_from_db()
        self.assertEqual(pos.sl, Decimal("1.163"))


# ─────────────────────────────────────────────────────────────────────
# Price-unavailable guard — no reference price to validate against
# ─────────────────────────────────────────────────────────────────────
class PriceUnavailableGuardTests(_OrderUpdateBase):

    def test_no_valid_quote_rejects_rather_than_skip_validation(self):
        """No live quote for the symbol -> cannot establish a reference
        price -> must reject (fail-safe), never silently persist an
        unvalidated SL/TP."""
        consumer, pos = self._make(side="BUY", avg_price="1.16227", no_quote=True)
        self._update(consumer, pos, sl=1.16100)
        err = _first_error(consumer)
        self.assertIsNotNone(err)
        self.assertEqual(err["code"], "price_unavailable")
        pos.refresh_from_db()
        self.assertIsNone(pos.sl)

    def test_no_op_update_skips_validation_and_reference_price_lookup(self):
        """sl and tp both None (nothing to change) must remain a true
        no-op — including not requiring a live quote at all, since
        there's nothing to validate."""
        consumer, pos = self._make(side="BUY", avg_price="1.16227", sl=Decimal("1.16000"), no_quote=True)
        self._update(consumer, pos)  # neither sl nor tp in the payload
        self.assertIsNone(_first_error(consumer))
        pos.refresh_from_db()
        self.assertEqual(pos.sl, Decimal("1.16000"))

"""
simulator/tests/test_fix01_risk_preview_contract_size.py — FIX-01

Regression coverage for the Risk Preview contract_size bug audited in
FIX-01 (dashboard.html::computeRiskLocal omitted contract_size, subestimando
notional/margin_required por 100,000x en forex) and its fix: the panel now
asks the backend's authoritative simulator/risk_engine.py::evaluate_
position_risk() via the 'order:risk_preview' WS action
(simulator/consumers.py::_handle_risk_preview), which already multiplied
by contract_size before this fix — so the backend formula itself is
unchanged here. What FIX-01 actually touches on the backend is the
symbol/qty echo added to _handle_risk_preview's response (needed for the
frontend's stale-response guard) — that echo is what's under test in
TestRiskPreviewEchoesRequestParams.

Prices are forced deterministic via the same pattern already established
in test_stale_price_audit.py (mock exposure_engine._get_current_price to
fail so evaluate_position_risk falls back to SymbolSpec.base_price) — no
network/FeedManager dependency, no flakiness.
"""
import asyncio
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from market_data.symbol_specs import get_spec
from simulator.consumers import TradingConsumer
from simulator.risk_engine import evaluate_position_risk

from .factories import make_account


def _force_price_fallback():
    """evaluate_position_risk's own except-block routes to spec.base_price
    when exposure_engine._get_current_price raises — see risk_engine.py:161-168
    and test_stale_price_audit.py's TestEvaluatePositionRiskFallback."""
    return patch(
        "simulator.exposure_engine._get_current_price",
        side_effect=RuntimeError("forced — deterministic test price"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. evaluate_position_risk — Forex, contract_size=100,000
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluatePositionRiskForexContractSize(TestCase):
    """
    The exact scenario from the FIX-01 audit and design lock item #2:
    $10,000 CHALLENGE account, 0.01 lot EUR/USD, leverage=50.

    Buggy JS (pre-fix) computed notional=qty*price=0.0117 → margin≈$0.00.
    Authoritative backend formula (risk_engine.py:181, unchanged by
    FIX-01): notional = lot_size * price * contract_size.
    """

    def test_eurusd_001_lot_10k_account_notional_and_margin(self):
        with _force_price_fallback():
            spec = get_spec("EUR/USD")
            account = make_account(account_type="CHALLENGE", tier="10K",
                                    balance=Decimal("10000"))

            result = evaluate_position_risk(
                account, "EUR/USD",
                lot_size=0.01,
                current_equity=10000.0,
                current_margin_used=0.0,
                leverage=50,
            )

        expected_notional = round(0.01 * spec.base_price * spec.contract_size, 2)
        expected_margin = round(expected_notional / min(50, spec.max_leverage), 2)

        self.assertEqual(spec.contract_size, 100_000)
        self.assertEqual(expected_notional, 1170.0,
                          "base_price=1.17 fixture assumption changed — update the test")
        self.assertEqual(result["notional"], expected_notional)
        self.assertEqual(result["margin_required"], expected_margin)
        # The bug this test guards against: notional/margin computed as if
        # contract_size were 1 (qty*price only) instead of 100,000.
        self.assertNotAlmostEqual(result["notional"], 0.01 * spec.base_price, places=2)
        self.assertGreater(result["notional"], 1000.0)

    def test_eurusd_exposure_pct_matches_notional_over_equity(self):
        with _force_price_fallback():
            account = make_account(account_type="CHALLENGE", tier="10K",
                                    balance=Decimal("10000"))
            result = evaluate_position_risk(
                account, "EUR/USD", lot_size=0.01,
                current_equity=10000.0, current_margin_used=0.0, leverage=50,
            )
        self.assertAlmostEqual(result["exposure_pct"], result["notional"] / 10000 * 100, places=1)
        self.assertEqual(result["risk_level"], "LOW")  # 11.7% < 25% LOW threshold


# ─────────────────────────────────────────────────────────────────────────────
# 2. evaluate_position_risk — Crypto, contract_size=1 (behavior unchanged)
# ─────────────────────────────────────────────────────────────────────────────

class TestEvaluatePositionRiskCryptoUnchanged(TestCase):
    """
    FIX-01 must not change crypto math: contract_size=1 means
    notional = qty*price whether or not contract_size is applied, so this
    locks the pre-existing behavior as a regression guard.
    """

    def test_btcusd_notional_and_margin_use_contract_size_one(self):
        with _force_price_fallback():
            spec = get_spec("BTCUSD")
            account = make_account(account_type="CHALLENGE", tier="10K",
                                    balance=Decimal("10000"))
            result = evaluate_position_risk(
                account, "BTCUSD", lot_size=0.01,
                current_equity=10000.0, current_margin_used=0.0, leverage=50,
            )

        self.assertEqual(spec.contract_size, 1.0)
        expected_notional = round(0.01 * spec.base_price * 1.0, 2)
        expected_margin = round(expected_notional / min(50, spec.max_leverage), 2)  # spec.max_leverage=20

        self.assertEqual(expected_notional, 820.0,
                          "base_price=82000 fixture assumption changed — update the test")
        self.assertEqual(result["notional"], expected_notional)
        self.assertEqual(result["margin_required"], expected_margin)
        # contract_size=1 → applying it is a no-op; confirms no accidental
        # double-multiplication was introduced anywhere in the FIX-01 change.
        self.assertEqual(result["notional"], round(0.01 * spec.base_price, 2))


# ─────────────────────────────────────────────────────────────────────────────
# 3. consumers.py::_handle_risk_preview — echoes symbol/qty (FIX-01 stale guard)
# ─────────────────────────────────────────────────────────────────────────────

class _RiskPreviewConsumerStub:
    """Exposes exactly the surface _handle_risk_preview touches, avoiding a
    full Channels WebsocketCommunicator (not used elsewhere for consumer
    unit tests in this suite — see test_spread_config_cache.py's comments).
    _db_evaluate_risk is inherited unbound from TradingConsumer itself, so
    the real DB lookup + evaluate_position_risk() call both run for real."""

    def __init__(self, account_id, balance, leverage=50):
        self._db_account_id = account_id
        self.account = {"balance": balance, "leverage": leverage}
        self.symbol = "EUR/USD"  # default arg to data.get("symbol", self.symbol) is eager
        self.sent = []

    def _unrealized_pnl_total(self):
        return 0.0

    def _margin_used_total(self):
        return 0.0

    async def send_json(self, payload):
        self.sent.append(payload)

    # __dict__ (not the bare attribute) — TradingConsumer._db_evaluate_risk
    # would trigger asgiref's descriptor __get__(None, cls) and bind self=None
    # (see asgiref.sync.SyncToAsync.__get__). Going through __dict__ keeps the
    # raw descriptor so normal instance attribute lookup binds it correctly.
    _db_evaluate_risk = TradingConsumer.__dict__["_db_evaluate_risk"]


class TestRiskPreviewEchoesRequestParams(TransactionTestCase):
    """TransactionTestCase — _db_evaluate_risk runs the DB query on a
    separate thread via database_sync_to_async; plain TestCase's wrapping
    transaction deadlocks against that thread on SQLite ('database table
    is locked'), same reason the rest of this suite's async-consumer tests
    use TransactionTestCase (see test_atomic_guard_lock_order.py etc.)."""

    def test_response_echoes_symbol_and_qty_alongside_real_assessment(self):
        account = make_account(account_type="CHALLENGE", tier="10K",
                                balance=Decimal("10000"))
        stub = _RiskPreviewConsumerStub(account.id, balance=10000.0, leverage=50)

        with _force_price_fallback():
            asyncio.run(TradingConsumer._handle_risk_preview(
                stub, {"symbol": "EUR/USD", "qty": 0.01},
            ))

        self.assertEqual(len(stub.sent), 1)
        msg = stub.sent[0]
        self.assertEqual(msg["type"], "risk_preview")
        self.assertEqual(msg["symbol"], "EUR/USD")
        self.assertEqual(msg["qty"], 0.01)
        # The real evaluate_position_risk() output must still be present —
        # the echo must not shadow or replace any assessment field.
        spec = get_spec("EUR/USD")
        self.assertEqual(msg["notional"], round(0.01 * spec.base_price * spec.contract_size, 2))
        self.assertIn("margin_required", msg)
        self.assertIn("risk_level", msg)

    def test_qty_zero_or_missing_sends_nothing(self):
        """Pre-existing guard (unchanged by FIX-01): qty<=0 short-circuits
        before any send_json call — the echo must not bypass it."""
        account = make_account(account_type="CHALLENGE", tier="10K",
                                balance=Decimal("10000"))
        stub = _RiskPreviewConsumerStub(account.id, balance=10000.0)
        asyncio.run(TradingConsumer._handle_risk_preview(stub, {"symbol": "EUR/USD", "qty": 0}))
        self.assertEqual(stub.sent, [])

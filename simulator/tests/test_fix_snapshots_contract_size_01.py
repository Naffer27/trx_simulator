# simulator/tests/test_fix_snapshots_contract_size_01.py
"""
FIX-SNAPSHOTS-CONTRACT-SIZE-01 — two bugs fixed in
simulator/snapshots.py::_position_data() / take_all_snapshots():

BUG 1: notional was `qty * price`, missing contract_size — silently
correct only for contract_size==1 instruments (crypto), wrong by
100,000x for every Forex pair (contract_size=100000). Documented prior
art: simulator/broker_exposure.py's RISK-01 FASE 1 audit flagged this
exact defect as a known, unfixed follow-up.

BUG 2: margin_used was reconstructed by summing ALL positions' notional
first, then dividing the total ONCE by account.leverage — ignoring each
symbol's own max_leverage cap. Silently correct only when every open
position's symbol.max_leverage >= account.leverage; wrong for any mixed
portfolio (e.g. Forex + crypto) where a lower-cap symbol is capped below
the account's own leverage. Fixed by computing margin PER POSITION:
effective_leverage = max(1, min(account.leverage, symbol.max_leverage)),
then summing — mirroring the canonical formula every real margin path
(consumers.py's calculate_required_margin() call sites, broker_risk.py)
already uses.

Scope: snapshots/reporting only — AccountEquitySnapshot/
BrokerEquitySnapshot, both INSERT-only. Never touches TradingAccount,
Position, Trade, LedgerEntry, execution, runtime margin, risk, stopout,
liquidation, or broker_monitoring.py. net_exposure_usd's sign convention
and currency conversion are explicitly out of scope for this block (both
confirmed follow-ups).

Reference example (Design Lock, both instruments in ONE account,
leverage=100):
  EUR/USD: qty=0.01, contract_size=100000, price=1.16, symbol max
           leverage=500 -> effective=min(100,500)=100
           notional=1160.00, margin=11.60
  BTCUSD:  qty=0.01, contract_size=1,      price=82000, symbol max
           leverage=20  -> effective=min(100,20)=20 (SYMBOL CAP BINDS)
           notional=820.00, margin=41.00
  margin_used (account) = 11.60 + 41.00 = 52.60
"""
from decimal import Decimal

from django.test import TestCase

from .factories import make_account, make_position
from simulator.models import AccountEquitySnapshot, BrokerEquitySnapshot, Position, Trade, LedgerEntry
from simulator.snapshots import take_all_snapshots, _position_data


def _acc(leverage=100, balance=Decimal("10000")):
    # make_account() hardcodes leverage=50 in its own .create() call, so
    # `leverage` can't be passed through its **kwargs (would collide) —
    # create normally, then set the leverage this test actually needs.
    acc = make_account(balance=balance)
    acc.leverage = leverage
    acc.save(update_fields=["leverage"])
    return acc


class ForexContractSizeTests(TestCase):
    """1. Forex contract_size=100000 applied to notional/margin."""

    def test_eurusd_notional_and_margin(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        pd = _position_data([acc.id], {acc.id: 100})
        self.assertEqual(pd[acc.id]["total"], Decimal("1160.00000"))
        self.assertAlmostEqual(float(pd[acc.id]["margin_used"]), 11.60, places=2)


class CryptoContractSizeTests(TestCase):
    """2. Crypto contract_size=1 — unaffected by BUG 1 (regression)."""

    def test_btcusd_notional_unaffected_by_contract_size(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="BTCUSD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("82000"))
        pd = _position_data([acc.id], {acc.id: 100})
        self.assertEqual(pd[acc.id]["total"], Decimal("820.0000"))


class EffectiveLeverageTests(TestCase):
    """3. effective_leverage = min(account.leverage, symbol.max_leverage)."""

    def test_symbol_cap_binds_when_lower_than_account_leverage(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="BTCUSD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("82000"))
        pd = _position_data([acc.id], {acc.id: 100})
        # notional=820.00, symbol cap=20 (< account leverage 100) -> margin = 820/20 = 41.00
        self.assertAlmostEqual(float(pd[acc.id]["margin_used"]), 41.00, places=2)

    def test_account_leverage_binds_when_lower_than_symbol_cap(self):
        acc = _acc(leverage=10)
        make_position(account=acc, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        pd = _position_data([acc.id], {acc.id: 10})
        # notional=1160.00, symbol cap=500 (>> account leverage 10) -> margin = 1160/10 = 116.00
        self.assertAlmostEqual(float(pd[acc.id]["margin_used"]), 116.00, places=2)


class MixedPortfolioTests(TestCase):
    """4/5. Mixed Forex+crypto account: margin_used = SUM of per-position
    margin, never a single account-wide leverage applied to the total."""

    def test_mixed_forex_crypto_margin_used_is_per_position_sum(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        make_position(account=acc, symbol="BTCUSD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("82000"))
        pd = _position_data([acc.id], {acc.id: 100})
        # 11.60 (EUR/USD, cap doesn't bind) + 41.00 (BTCUSD, cap DOES bind)
        self.assertAlmostEqual(float(pd[acc.id]["margin_used"]), 52.60, places=2)
        # The WRONG (bug 2) formula would have been:
        # total_notional=1980.00, /account.leverage(100) = 19.80 — provably different.
        self.assertNotAlmostEqual(float(pd[acc.id]["margin_used"]), 19.80, places=2)
        self.assertEqual(pd[acc.id]["count"], 2)


class GrossNotionalUnchangedTests(TestCase):
    """6. gross long/short = pure notional, never divided by leverage."""

    def test_long_and_short_notional_never_divided_by_leverage(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        make_position(account=acc, symbol="EUR/USD", side="SELL",
                       qty=Decimal("0.02"), avg_price=Decimal("1.16000"))
        pd = _position_data([acc.id], {acc.id: 100})
        self.assertEqual(pd[acc.id]["long"], Decimal("1160.00000"))
        self.assertEqual(pd[acc.id]["short"], Decimal("2320.00000"))
        self.assertEqual(pd[acc.id]["total"], Decimal("3480.00000"))


class FreeMarginTests(TestCase):
    """7. free_margin = max(0, equity - margin_used), using the corrected
    margin_used."""

    def test_free_margin_uses_corrected_margin(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        take_all_snapshots()
        snap = AccountEquitySnapshot.objects.filter(account=acc).latest("taken_at")
        self.assertAlmostEqual(float(snap.margin_used), 11.60, places=2)
        self.assertAlmostEqual(float(snap.free_margin), float(acc.equity) - 11.60, places=2)


class BrokerTotalMarginTests(TestCase):
    """8. BrokerEquitySnapshot.total_margin_used = sum across accounts,
    each using its own corrected per-position margin."""

    def test_broker_total_margin_sums_corrected_account_margins(self):
        acc1 = _acc(leverage=100)
        acc2 = _acc(leverage=100)
        make_position(account=acc1, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        make_position(account=acc2, symbol="BTCUSD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("82000"))
        take_all_snapshots()
        broker_snap = BrokerEquitySnapshot.objects.latest("taken_at")
        self.assertAlmostEqual(float(broker_snap.total_margin_used), 52.60, places=2)
        self.assertAlmostEqual(float(broker_snap.gross_long_usd), 1980.00, places=2)


class UnknownSymbolFallbackTests(TestCase):
    """9. Unknown/deregistered symbol never crashes the snapshot task;
    falls back to contract_size=1.0, symbol_max_leverage=account.leverage
    (no additional cap invented)."""

    def test_unknown_symbol_uses_neutral_fallback(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="ZZZFAKE", side="BUY",
                       qty=Decimal("10"), avg_price=Decimal("5.00"))
        pd = _position_data([acc.id], {acc.id: 100})
        # contract_size defaults to 1 -> notional = 10*1*5 = 50.00
        self.assertEqual(pd[acc.id]["total"], Decimal("50.00"))
        # symbol_max_leverage defaults to account.leverage(100) -> effective=100
        self.assertAlmostEqual(float(pd[acc.id]["margin_used"]), 0.50, places=2)

    def test_unknown_symbol_does_not_raise(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="ZZZFAKE", side="BUY",
                       qty=Decimal("10"), avg_price=Decimal("5.00"))
        take_all_snapshots()  # must not raise


class PositionCountUnchangedTests(TestCase):
    """10. count/open_positions unaffected by the formula change."""

    def test_count_matches_number_of_positions(self):
        acc = _acc(leverage=100)
        make_position(account=acc, symbol="EUR/USD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        make_position(account=acc, symbol="BTCUSD", side="BUY",
                       qty=Decimal("0.01"), avg_price=Decimal("82000"))
        pd = _position_data([acc.id], {acc.id: 100})
        self.assertEqual(pd[acc.id]["count"], 2)
        take_all_snapshots()
        snap = AccountEquitySnapshot.objects.filter(account=acc).latest("taken_at")
        self.assertEqual(snap.open_positions, 2)


class InsertOnlyScopeTests(TestCase):
    """11/12. take_all_snapshots() remains INSERT-only on the two snapshot
    tables — zero writes to TradingAccount/Position/Trade/LedgerEntry."""

    def test_no_mutation_of_financial_models(self):
        acc = _acc(leverage=100)
        pos = make_position(account=acc, symbol="EUR/USD", side="BUY",
                             qty=Decimal("0.01"), avg_price=Decimal("1.16000"))
        balance_before, equity_before = acc.balance, acc.equity
        pos_qty_before, pos_price_before = pos.qty, pos.avg_price
        trade_count_before = Trade.objects.count()
        ledger_count_before = LedgerEntry.objects.count()

        take_all_snapshots()

        acc.refresh_from_db()
        pos.refresh_from_db()
        self.assertEqual(acc.balance, balance_before)
        self.assertEqual(acc.equity, equity_before)
        self.assertEqual(pos.qty, pos_qty_before)
        self.assertEqual(pos.avg_price, pos_price_before)
        self.assertEqual(Trade.objects.count(), trade_count_before)
        self.assertEqual(LedgerEntry.objects.count(), ledger_count_before)
        # The only writes: exactly one row in each snapshot table.
        self.assertEqual(AccountEquitySnapshot.objects.filter(account=acc).count(), 1)
        self.assertEqual(BrokerEquitySnapshot.objects.count(), 1)


class NoMigrationsSourceTests(TestCase):
    """13. contract_size/max_leverage are read from market_data.symbol_specs
    (pure in-code registry, not a new DB field) — no migration required.
    (Gate-level confirmation is `manage.py makemigrations --check`.)"""

    def test_position_data_reads_contract_size_from_symbol_specs(self):
        import inspect
        from simulator import snapshots as snapshots_module
        module_src = inspect.getsource(snapshots_module)
        self.assertIn("from market_data.symbol_specs import get_spec", module_src)
        fn_src = inspect.getsource(snapshots_module._position_data)
        self.assertIn("get_spec(", fn_src)
        self.assertIn("spec.contract_size", fn_src)
        self.assertIn("spec.max_leverage", fn_src)

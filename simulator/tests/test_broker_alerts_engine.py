"""
simulator/tests/test_broker_alerts_engine.py — RISK-03.

Broker Risk Alerts & Dealing Dashboard Foundation (simulator/broker_alerts.py)
— pure operational intelligence: OBSERVE, EVALUATE, CLASSIFY, REPORT. Never
blocks an order. Built on RISK-01 (broker_exposure.py) and RISK-02
(broker_risk.py) as the single sources of truth for exposure/pricing/limits
— never re-derives those numbers.

Coverage layers:
  - SeverityCategoryTests: enum ordering/rank, category/status contents.
  - AlertModelTests: BrokerRiskAlert.to_dict(), deterministic alert_id.
  - ExposureAlertTests: items 1-3, mutually exclusive tiers, strict boundaries.
  - ConcentrationAlertTests: items 6-7, per-symbol, strict boundaries.
  - PricingAlertTests: items 4-5, strict boundaries, metadata.
  - ConfigurationAlertTests: items 11-12, all 9 RISK-02 limits, no false
    positives at defaults.
  - SystemAlertTests: items 8-9-10 (BrokerRiskLock existence/recreation,
    risk engine kill switch).
  - OrderingAndDedupTests: collect_risk_alerts() ordering, no duplicates,
    deterministic, per-collector failure isolation.
  - BrokerHealthSummaryTests: FASE 8 dashboard payload.
  - IntegrationTests: admin._compute_control_data() + broker_monitoring
    full_report() both consume broker_alerts.py.
  - NoRegressionTests: RISK-02's order-open path (incl. the new
    BrokerRiskLock.last_recreated_at stamping) and RISK-01's exposure
    snapshot are unaffected.
"""
import json
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from market_data.feeds import get_feed_manager

import simulator.broker_alerts as ba
import simulator.broker_risk as br
from simulator.broker_alerts import (
    Severity, Category, Status, BrokerRiskAlert,
    collect_risk_alerts, collect_exposure_alerts, collect_pricing_alerts,
    collect_configuration_alerts, collect_system_alerts, broker_health_summary,
    _CONFIG_LIMITS,
)
from simulator.broker_exposure import broker_exposure_snapshot
from simulator.consumers import TradingConsumer
from simulator.models import BrokerRiskLock, Position, TradingAccount

from .factories import make_account, make_position

_run = lambda coro: __import__("asyncio").run(coro)
_db_open_sync = TradingConsumer._db_open_position_atomic.__wrapped__


def _seed_price(symbol, price):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices[symbol] = price
        feed._bids[symbol] = price
        feed._asks[symbol] = price
        feed._price_ts[symbol] = time.time()


def _clear_price(symbol):
    feed = get_feed_manager()
    with feed._lock:
        feed._prices.pop(symbol, None)
        feed._bids.pop(symbol, None)
        feed._asks.pop(symbol, None)
        feed._price_ts.pop(symbol, None)


class _CleanFeedMixin:
    SYMBOLS = ("EUR/USD", "BTCUSD", "ETHUSD", "USD/JPY", "GBP/USD")

    def setUp(self):
        super().setUp()
        for s in self.SYMBOLS:
            _clear_price(s)

    def tearDown(self):
        for s in self.SYMBOLS:
            _clear_price(s)
        super().tearDown()


def _consumer(account_id, netting_mode=False):
    c = TradingConsumer.__new__(TradingConsumer)
    c._db_account_id = account_id
    c.account = {
        "netting_mode": netting_mode, "spread_pips": 0.0, "leverage": 50,
        "allowed_symbols": None, "max_lot_size": None, "margin_call_level": 100.0,
    }
    c._feed = get_feed_manager()
    return c


# ─────────────────────────────────────────────────────────────────────────
# 1. Severity / Category / Status
# ─────────────────────────────────────────────────────────────────────────
class SeverityCategoryTests(TestCase):
    def test_severity_order_ascending(self):
        self.assertEqual(
            Severity.ORDER,
            (Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL),
        )

    def test_severity_rank_monotonic(self):
        ranks = [Severity.rank(s) for s in Severity.ORDER]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(Severity.rank(Severity.HIGH), Severity.rank(Severity.CRITICAL))

    def test_category_all_contains_required_set(self):
        required = {"EXPOSURE", "CONCENTRATION", "PRICING", "MARGIN",
                    "LIQUIDITY", "EXECUTION", "SYSTEM", "CONFIGURATION"}
        self.assertEqual(set(Category.ALL), required)

    def test_status_active(self):
        self.assertEqual(Status.ACTIVE, "ACTIVE")


# ─────────────────────────────────────────────────────────────────────────
# 2. BrokerRiskAlert dataclass
# ─────────────────────────────────────────────────────────────────────────
class AlertModelTests(_CleanFeedMixin, TestCase):
    def test_to_dict_is_json_serializable_and_converts_decimal(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("85"), avg_price=Decimal("1"))
            alerts = collect_exposure_alerts()
        self.assertEqual(len(alerts), 1)
        d = alerts[0].to_dict()
        json.dumps(d)  # must not raise
        self.assertIsInstance(d["current_value"], float)
        self.assertIsInstance(d["threshold"], float)
        self.assertEqual(d["status"], Status.ACTIVE)

    def test_alert_id_deterministic_across_calls(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("85"), avg_price=Decimal("1"))
            ids_1 = [a.alert_id for a in collect_exposure_alerts()]
            ids_2 = [a.alert_id for a in collect_exposure_alerts()]
        self.assertEqual(ids_1, ids_2)
        self.assertTrue(ids_1[0].startswith("EXPOSURE:"))


# ─────────────────────────────────────────────────────────────────────────
# 3. Exposure alerts — items 1-3
# ─────────────────────────────────────────────────────────────────────────
class ExposureAlertTests(_CleanFeedMixin, TestCase):
    def _make_gross_qty(self, qty):
        account = make_account(balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal(str(qty)), avg_price=Decimal("1"))

    def test_no_alert_below_medium_threshold(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(79)
            self.assertEqual(collect_exposure_alerts(), [])

    def test_no_alert_at_exact_80_boundary(self):
        # spec says "> 80%" — exactly 80% must NOT alert.
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(80)
            exposure_only = [a for a in collect_exposure_alerts() if a.category == Category.EXPOSURE]
            self.assertEqual(exposure_only, [])

    def test_medium_alert_above_80(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(81)
            alerts = [a for a in collect_exposure_alerts() if a.category == Category.EXPOSURE]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.MEDIUM)

    def test_high_alert_above_90(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(91)
            alerts = [a for a in collect_exposure_alerts() if a.category == Category.EXPOSURE]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.HIGH)

    def test_critical_alert_above_100(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(101)
            alerts = [a for a in collect_exposure_alerts() if a.category == Category.EXPOSURE]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.CRITICAL)

    def test_exactly_100_is_high_not_critical(self):
        # 100% is > 90 (HIGH) but not > 100 (CRITICAL) — strict boundary.
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(100)
            alerts = [a for a in collect_exposure_alerts() if a.category == Category.EXPOSURE]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.HIGH)

    def test_mutually_exclusive_never_multiple_tiers_at_once(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            self._make_gross_qty(150)
            alerts = [a for a in collect_exposure_alerts() if a.category == Category.EXPOSURE]
        self.assertEqual(len(alerts), 1)


# ─────────────────────────────────────────────────────────────────────────
# 4. Concentration alerts — items 6-7 (BTCUSD/ETHUSD: contract_size=1,
# so notional == qty*price, making exact percentage splits easy).
# ─────────────────────────────────────────────────────────────────────────
class ConcentrationAlertTests(_CleanFeedMixin, TestCase):
    def setUp(self):
        super().setUp()
        _seed_price("BTCUSD", 1)
        _seed_price("ETHUSD", 1)

    def test_no_alert_at_exact_70_boundary(self):
        account = make_account(balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("70"), avg_price=Decimal("1"))
        make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("30"), avg_price=Decimal("1"))
        alerts = [a for a in collect_exposure_alerts() if a.category == Category.CONCENTRATION]
        self.assertEqual(alerts, [])

    def test_high_alert_above_70(self):
        account = make_account(balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("75"), avg_price=Decimal("1"))
        make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("25"), avg_price=Decimal("1"))
        alerts = [a for a in collect_exposure_alerts() if a.category == Category.CONCENTRATION]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.HIGH)
        self.assertEqual(alerts[0].affected_symbol, "BTCUSD")

    def test_critical_alert_above_85_single_symbol(self):
        account = make_account(balance=Decimal("1000000"))
        make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1"))
        alerts = [a for a in collect_exposure_alerts() if a.category == Category.CONCENTRATION]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.CRITICAL)

    def test_concentration_and_exposure_alert_ids_never_collide(self):
        # A single dominant symbol can trigger BOTH a broker-wide EXPOSURE
        # alert and a per-symbol CONCENTRATION alert at once — the two
        # alert_ids must stay distinct (different category prefix), and
        # the concentration one must be scoped to its symbol.
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("90"), avg_price=Decimal("1"))
            make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("10"), avg_price=Decimal("1"))
            alerts = collect_exposure_alerts()
        ids = [a.alert_id for a in alerts]
        self.assertEqual(len(ids), len(set(ids)))
        conc = [a for a in alerts if a.category == Category.CONCENTRATION]
        self.assertEqual(len(conc), 1)
        self.assertEqual(conc[0].affected_symbol, "BTCUSD")
        self.assertEqual(conc[0].alert_id, "CONCENTRATION:CONCENTRATION_SYMBOL_CRITICAL:BTCUSD")
        expo = [a for a in alerts if a.category == Category.EXPOSURE]
        self.assertEqual(len(expo), 1)
        self.assertTrue(expo[0].alert_id.startswith("EXPOSURE:"))


# ─────────────────────────────────────────────────────────────────────────
# 5. Pricing alerts — items 4-5
# ─────────────────────────────────────────────────────────────────────────
class PricingAlertTests(_CleanFeedMixin, TestCase):
    def _make_positions(self, priced_count, unpriced_count):
        account = make_account(balance=Decimal("1000000"))
        _seed_price("BTCUSD", 1)
        for _ in range(priced_count):
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1"))
        for _ in range(unpriced_count):
            make_position(account, symbol="ETHUSD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1"))

    def test_full_coverage_no_alert(self):
        self._make_positions(priced_count=10, unpriced_count=0)
        self.assertEqual(collect_pricing_alerts(), [])

    def test_exactly_95_pct_is_medium_not_high(self):
        # 19/20 priced = 95.00% exactly — "<100%" (MEDIUM) fires,
        # "<95%" (HIGH) does not (strict boundary).
        self._make_positions(priced_count=19, unpriced_count=1)
        alerts = collect_pricing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.MEDIUM)

    def test_below_95_is_high(self):
        self._make_positions(priced_count=9, unpriced_count=1)  # 90%
        alerts = collect_pricing_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.HIGH)

    def test_metadata_lists_unpriced_symbols(self):
        self._make_positions(priced_count=9, unpriced_count=1)
        alerts = collect_pricing_alerts()
        self.assertIn("ETHUSD", alerts[0].metadata["stale_or_missing_symbols"])
        self.assertEqual(alerts[0].metadata["unpriced_position_count"], 1)


# ─────────────────────────────────────────────────────────────────────────
# 6. Configuration alerts — items 11-12
# ─────────────────────────────────────────────────────────────────────────
class ConfigurationAlertTests(TestCase):
    def test_defaults_produce_no_alerts(self):
        self.assertEqual(collect_configuration_alerts(), [])

    def test_too_low_produces_high_alert(self):
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("0.0001")):
            alerts = collect_configuration_alerts()
        matching = [a for a in alerts if a.metric == "max_symbol_exposure_lots_value"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, Severity.HIGH)
        self.assertEqual(matching[0].category, Category.CONFIGURATION)

    def test_too_high_produces_medium_alert(self):
        with patch.object(br, "MAX_GROSS_NOTIONAL", Decimal("9999999999999999999")):
            alerts = collect_configuration_alerts()
        matching = [a for a in alerts if a.metric == "max_gross_notional_value"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, Severity.MEDIUM)

    def test_all_nine_limits_are_covered(self):
        self.assertEqual(len(_CONFIG_LIMITS), 9)
        attrs = {name for name, _, _ in _CONFIG_LIMITS}
        self.assertEqual(attrs, {
            "MAX_SYMBOL_EXPOSURE_LOTS", "MAX_ACCOUNT_EXPOSURE_LOTS",
            "MAX_TOTAL_BROKER_EXPOSURE_LOTS", "MAX_LONG_EXPOSURE_LOTS",
            "MAX_SHORT_EXPOSURE_LOTS", "MAX_GROSS_NOTIONAL", "MAX_NET_NOTIONAL",
            "MAX_POSITION_SIZE_LOTS", "MAX_OPEN_POSITIONS_BROKER_WIDE",
        })
        # Every configured attribute must actually exist on broker_risk.
        for attr, _, _ in _CONFIG_LIMITS:
            self.assertTrue(hasattr(br, attr), f"broker_risk.{attr} missing")

    def test_each_limit_individually_flagged_too_low(self):
        for attr, sane_min, _sane_max in _CONFIG_LIMITS:
            with patch.object(br, attr, sane_min - Decimal("0.001")):
                alerts = collect_configuration_alerts()
            rule_hit = [a for a in alerts if a.alert_id == f"CONFIGURATION:{attr}_TOO_LOW"]
            self.assertEqual(len(rule_hit), 1, f"{attr} did not raise a TOO_LOW alert")


# ─────────────────────────────────────────────────────────────────────────
# 7. System alerts — items 8-9-10
# ─────────────────────────────────────────────────────────────────────────
class SystemAlertTests(TestCase):
    def test_lock_missing_is_critical(self):
        BrokerRiskLock.objects.filter(pk=1).delete()
        alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_MISSING"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, Severity.CRITICAL)

    def test_lock_present_never_recreated_no_alert(self):
        BrokerRiskLock.objects.filter(pk=1).update(last_recreated_at=None)
        alerts = collect_system_alerts()
        lock_alerts = [a for a in alerts if a.category == Category.SYSTEM
                       and "LOCK" in a.alert_id]
        self.assertEqual(lock_alerts, [])

    def test_lock_recreated_recently_produces_high_alert(self):
        from django.utils import timezone
        BrokerRiskLock.objects.filter(pk=1).update(last_recreated_at=timezone.now())
        alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_RECREATED"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, Severity.HIGH)
        self.assertIn("last_recreated_at", matching[0].metadata)

    def test_lock_recreated_beyond_window_no_alert(self):
        # Correction 1 — an old recreation must not alert indefinitely,
        # but last_recreated_at itself must stay on the row (never
        # cleared) — verified via a second, direct DB read below.
        from django.utils import timezone
        from datetime import timedelta
        old_ts = timezone.now() - timedelta(
            seconds=ba.BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS + 60
        )
        BrokerRiskLock.objects.filter(pk=1).update(last_recreated_at=old_ts)
        alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_RECREATED"]
        self.assertEqual(matching, [])
        self.assertEqual(BrokerRiskLock.objects.get(pk=1).last_recreated_at, old_ts)

    def test_lock_recreated_exact_boundary_still_alerts(self):
        # delta == window seconds exactly — the comparison is inclusive
        # (<=), so this must still alert. "now" is frozen so real
        # wall-clock/DB-roundtrip drift can't push the computed delta
        # past the window between setup and the call under test.
        from django.utils import timezone
        from datetime import timedelta
        fixed_now = timezone.now()
        boundary_ts = fixed_now - timedelta(
            seconds=ba.BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS
        )
        BrokerRiskLock.objects.filter(pk=1).update(last_recreated_at=boundary_ts)
        with patch.object(ba.timezone, "now", return_value=fixed_now):
            alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_RECREATED"]
        self.assertEqual(len(matching), 1)

    def test_lock_recreated_timestamp_in_future_is_anomaly(self):
        from django.utils import timezone
        from datetime import timedelta
        future_ts = timezone.now() + timedelta(hours=1)
        BrokerRiskLock.objects.filter(pk=1).update(last_recreated_at=future_ts)
        alerts = collect_system_alerts()
        anomaly = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_TIMESTAMP_ANOMALY"]
        self.assertEqual(len(anomaly), 1)
        self.assertEqual(anomaly[0].category, Category.SYSTEM)
        self.assertIn(anomaly[0].severity, (Severity.HIGH, Severity.CRITICAL))
        # A future timestamp must never ALSO be reported as a plain
        # "recently recreated" alert — exactly one alert for this row.
        recreated = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_RECREATED"]
        self.assertEqual(recreated, [])

    def test_custom_recreation_window_is_respected(self):
        from django.utils import timezone
        from datetime import timedelta
        ts = timezone.now() - timedelta(seconds=30)
        BrokerRiskLock.objects.filter(pk=1).update(last_recreated_at=ts)
        with patch.object(ba, "BROKER_RISK_LOCK_RECREATION_ALERT_WINDOW_SECONDS", 10):
            alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_RECREATED"]
        self.assertEqual(matching, [])  # 30s ago > 10s window

    def test_lock_missing_message_does_not_imply_irreversible_loss(self):
        BrokerRiskLock.objects.filter(pk=1).delete()
        alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_MISSING"]
        text = (matching[0].title + " " + matching[0].description).lower()
        # Must explain the self-healing nature and explicitly reassure
        # this is NOT a permanent/irreversible loss — not merely avoid
        # the word "irreversible" outright (a negated mention like "NOT
        # ... irreversible" is exactly the correct, reassuring phrasing).
        self.assertIn("self-heal", text)
        self.assertIn("not a permanent", text)
        self.assertIn("not", text)
        self.assertIn("irreversible", text)  # present, but negated

    def test_risk_engine_enabled_default_no_alert(self):
        alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:RISK_ENGINE_DISABLED"]
        self.assertEqual(matching, [])

    def test_risk_engine_disabled_is_critical(self):
        with patch.object(ba, "RISK_ENGINE_ENABLED", False):
            alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:RISK_ENGINE_DISABLED"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].severity, Severity.CRITICAL)


# ─────────────────────────────────────────────────────────────────────────
# 8. Ordering, dedup, isolation
# ─────────────────────────────────────────────────────────────────────────
class OrderingAndDedupTests(_CleanFeedMixin, TestCase):
    def test_sorted_by_severity_descending(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")), \
             patch.object(ba, "RISK_ENGINE_ENABLED", False):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("101"), avg_price=Decimal("1"))
            _seed_price("BTCUSD", 1)
            alerts = collect_risk_alerts()
        ranks = [Severity.rank(a.severity) for a in alerts]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(alerts[0].severity, Severity.CRITICAL)

    def test_no_duplicate_alert_ids(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("150"), avg_price=Decimal("1"))
            alerts = collect_risk_alerts()
        ids = [a.alert_id for a in alerts]
        self.assertEqual(len(ids), len(set(ids)))

    def test_deterministic_id_set_across_calls(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("150"), avg_price=Decimal("1"))
            ids_1 = sorted(a.alert_id for a in collect_risk_alerts())
            ids_2 = sorted(a.alert_id for a in collect_risk_alerts())
        self.assertEqual(ids_1, ids_2)

    def test_no_alerts_on_clean_state(self):
        # No positions, default config, lock present/never recreated,
        # engine enabled — must be perfectly quiet (no false positives).
        self.assertEqual(collect_risk_alerts(), [])

    def test_one_collector_failing_does_not_block_others(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("150"), avg_price=Decimal("1"))
            with patch.object(ba, "collect_configuration_alerts", side_effect=RuntimeError("boom")):
                alerts = collect_risk_alerts()
        self.assertTrue(any(a.category == Category.EXPOSURE for a in alerts))


# ─────────────────────────────────────────────────────────────────────────
# 8b. Correction 2 — fail-visible collector failures (dedicated coverage
# for all 7 required scenarios).
# ─────────────────────────────────────────────────────────────────────────
class CollectorFailureTests(_CleanFeedMixin, TestCase):
    def test_exposure_collector_failing_still_returns_other_categories(self):
        # 1. exposure collector falla; 2. los demás siguen ejecutándose.
        with patch.object(ba, "RISK_ENGINE_ENABLED", False), \
             patch.object(ba, "collect_exposure_alerts", side_effect=RuntimeError("sensitive-secret-value")):
            alerts = collect_risk_alerts()
        self.assertTrue(any(a.category == Category.SYSTEM and a.alert_id != "SYSTEM:ALERT_COLLECTOR_FAILURE_EXPOSURE_COLLECTOR"
                             for a in alerts))  # RISK_ENGINE_DISABLED still fired

    def test_failure_produces_alert_collector_failure_entry(self):
        # 3. aparece ALERT_COLLECTOR_FAILURE.
        with patch.object(ba, "collect_pricing_alerts", side_effect=RuntimeError("boom")):
            alerts = collect_risk_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:ALERT_COLLECTOR_FAILURE_PRICING_COLLECTOR"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].category, Category.SYSTEM)
        self.assertIn(matching[0].severity, (Severity.HIGH, Severity.CRITICAL))

    def test_health_is_not_healthy_when_a_collector_fails(self):
        # 4. health queda Warning/Critical.
        with patch.object(ba, "collect_configuration_alerts", side_effect=RuntimeError("boom")):
            summary = broker_health_summary()
        self.assertIn(summary["status"], ("warning", "critical"))
        self.assertNotEqual(summary["status"], "healthy")

    def test_repeated_failure_does_not_duplicate_alert(self):
        # 5. no hay duplicados.
        with patch.object(ba, "collect_system_alerts", side_effect=RuntimeError("boom")):
            ids_1 = [a.alert_id for a in collect_risk_alerts()]
            ids_2 = [a.alert_id for a in collect_risk_alerts()]
        self.assertEqual(len(ids_1), len(set(ids_1)))
        self.assertEqual(ids_1, ids_2)

    def test_exception_message_never_reaches_public_output(self):
        # 6. exception text sensible no aparece en la salida pública.
        secret = "db_password=hunter2_super_secret"
        with patch.object(ba, "collect_exposure_alerts", side_effect=RuntimeError(secret)):
            alerts = collect_risk_alerts()
            summary = broker_health_summary()
        for a in alerts:
            d = a.to_dict()
            self.assertNotIn(secret, json.dumps(d))
        self.assertNotIn(secret, json.dumps(summary))

    def test_logger_receives_the_exception(self):
        # 7. logger sí recibe la excepción.
        with patch.object(ba, "collect_exposure_alerts", side_effect=RuntimeError("boom")):
            with self.assertLogs("simulator.broker_alerts", level="ERROR") as cm:
                collect_risk_alerts()
        self.assertTrue(any("collect_exposure_alerts" in line for line in cm.output))

    def test_metadata_carries_only_type_and_timestamp_not_message(self):
        with patch.object(ba, "collect_exposure_alerts", side_effect=ValueError("top secret payload")):
            alerts = collect_risk_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:ALERT_COLLECTOR_FAILURE_EXPOSURE_COLLECTOR"]
        self.assertEqual(matching[0].metadata["exception_type"], "ValueError")
        self.assertNotIn("top secret payload", json.dumps(matching[0].metadata))
        self.assertIn("failed_at", matching[0].metadata)


# ─────────────────────────────────────────────────────────────────────────
# 9. Broker Health summary (FASE 8)
# ─────────────────────────────────────────────────────────────────────────
class BrokerHealthSummaryTests(_CleanFeedMixin, TestCase):
    def test_healthy_when_no_alerts(self):
        summary = broker_health_summary()
        self.assertEqual(summary["status"], "healthy")
        self.assertEqual(summary["alert_count"], 0)

    def test_warning_status_on_medium_or_high(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("100")):
            account = make_account(balance=Decimal("1000000"))
            make_position(account, symbol="BTCUSD", side="BUY", qty=Decimal("81"), avg_price=Decimal("1"))
            summary = broker_health_summary()
        self.assertEqual(summary["status"], "warning")

    def test_critical_status_on_critical_alert(self):
        with patch.object(ba, "RISK_ENGINE_ENABLED", False):
            summary = broker_health_summary()
        self.assertEqual(summary["status"], "critical")
        self.assertGreaterEqual(summary["critical_count"], 1)
        self.assertTrue(all(a["severity"] == Severity.CRITICAL for a in summary["top_critical_alerts"]))

    def test_top_critical_alerts_capped_at_five(self):
        with patch.object(br, "MAX_TOTAL_BROKER_EXPOSURE_LOTS", Decimal("1")), \
             patch.object(ba, "RISK_ENGINE_ENABLED", False):
            account = make_account(balance=Decimal("1000000"))
            for i, sym in enumerate(["BTCUSD", "ETHUSD", "EUR/USD", "USD/JPY", "GBP/USD"]):
                _seed_price(sym, 1)
                make_position(account, symbol=sym, side="BUY", qty=Decimal("5"), avg_price=Decimal("1"))
            summary = broker_health_summary()
        self.assertLessEqual(len(summary["top_critical_alerts"]), 5)


# ─────────────────────────────────────────────────────────────────────────
# 10. Integration — admin.py + broker_monitoring.py
# ─────────────────────────────────────────────────────────────────────────
class IntegrationTests(_CleanFeedMixin, TestCase):
    def test_admin_compute_control_data_includes_broker_health(self):
        from simulator.admin import _compute_control_data
        data = _compute_control_data()
        self.assertIn("broker_health", data)
        for key in ("status", "alert_count", "critical_count", "top_critical_alerts",
                    "exposure_gross_quantity", "pricing_coverage_pct", "largest_symbol"):
            self.assertIn(key, data["broker_health"])

    def test_broker_monitoring_full_report_includes_broker_alerts(self):
        from simulator.broker_monitoring import full_report
        report = full_report()
        self.assertIn("broker_alerts", report)
        self.assertIsInstance(report["broker_alerts"], list)
        self.assertNotIn("broker_alerts", report["errors"])
        # Legacy alerts_summary() untouched, still present alongside.
        self.assertIn("alerts", report)
        self.assertIn("overall_status", report["alerts"])


# ─────────────────────────────────────────────────────────────────────────
# 11. No-regression — RISK-02 order-open path + RISK-01 snapshot
# ─────────────────────────────────────────────────────────────────────────
class NoRegressionTests(TestCase):
    """TestCase (not TransactionTestCase): _db_open_sync is called
    directly/synchronously on the same connection (no threads, no
    asyncio.run() around a real database_sync_to_async wrapper), and
    test_normal_order_open_does_not_touch_last_recreated_at specifically
    needs the migration-seeded BrokerRiskLock row to survive setUp — a
    TransactionTestCase's per-test flush() truncates it, which would
    make every test see a missing row and force a spurious self-heal."""

    def test_normal_order_open_does_not_touch_last_recreated_at(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        self.assertTrue(result["ok"])
        lock = BrokerRiskLock.objects.get(pk=1)
        self.assertIsNone(lock.last_recreated_at)
        self.assertEqual(collect_system_alerts(), [])
        _clear_price("EUR/USD")

    def test_lock_self_heal_stamps_last_recreated_at_and_alert_fires(self):
        _seed_price("EUR/USD", 1.1)
        BrokerRiskLock.objects.filter(pk=1).delete()
        account = make_account(balance=Decimal("1000000.00"))
        result = _db_open_sync(
            _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
            commission=0.0, new_balance=1000000.0,
        )
        self.assertTrue(result["ok"])
        lock = BrokerRiskLock.objects.get(pk=1)
        self.assertIsNotNone(lock.last_recreated_at)
        alerts = collect_system_alerts()
        matching = [a for a in alerts if a.alert_id == "SYSTEM:BROKER_RISK_LOCK_RECREATED"]
        self.assertEqual(len(matching), 1)
        _clear_price("EUR/USD")

    def test_risk02_limit_still_rejects_orders_normally(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        with patch.object(br, "MAX_SYMBOL_EXPOSURE_LOTS", Decimal("0.5")):
            result = _db_open_sync(
                _consumer(account.pk), "EUR/USD", "buy", 1.0, 1.1, None, None,
                commission=0.0, new_balance=1000000.0,
            )
        self.assertFalse(result["ok"])
        _clear_price("EUR/USD")

    def test_broker_exposure_snapshot_unaffected(self):
        _seed_price("EUR/USD", 1.1)
        account = make_account(balance=Decimal("1000000.00"))
        make_position(account, symbol="EUR/USD", side="BUY", qty=Decimal("1"), avg_price=Decimal("1.1"))
        snapshot = broker_exposure_snapshot()
        self.assertEqual(snapshot.open_position_count, 1)
        _clear_price("EUR/USD")
